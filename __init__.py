# ボーンウェイトでメッシュを分離するアドオン(Extension形式)
# アクティブメッシュの各頂点の最大ウェイトボーンを求め、面を多数決で割り当てて
# ボーンごとに別オブジェクトへ分離する。

import bpy
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 型チェック専用(実行時のBlenderには存在しないスタブ内部モジュール)
    from bpy.stub_internal.rna_enums import OperatorReturnItems

ATTR_NAME = "sbw_bone_id"


def find_armature(obj: bpy.types.Object):
    """アーマチュアモディファイア優先でアーマチュアを探す"""
    for m in obj.modifiers:
        if isinstance(m, bpy.types.ArmatureModifier) and m.object is not None:
            return m.object
    return obj.find_armature()


class OBJECT_OT_separate_by_bone_weights(bpy.types.Operator):
    bl_idname = "object.separate_by_bone_weights"
    bl_label = "ボーンウェイトで分離"
    bl_description = "アクティブメッシュを、各頂点の最大ウェイトボーンごとに別オブジェクトへ分離する(面は3頂点の多数決で割当)"
    bl_options = {'REGISTER', 'UNDO'}

    rename_to_bone: bpy.props.BoolProperty(
        name="オブジェクト名をボーン名にする",
        description="分離後の各オブジェクト(元オブジェクト含む)をボーン名にリネームする",
        default=True,
    )
    cleanup_materials: bpy.props.BoolProperty(
        name="未使用マテリアルスロットを削除",
        description="分離後の各オブジェクトから使われていないマテリアルスロットを取り除く",
        default=True,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        return (obj is not None and obj.type == 'MESH'
                and context.mode == 'OBJECT')

    def execute(self, context: bpy.types.Context) -> set["OperatorReturnItems"]:
        obj = context.active_object
        # pollによりメッシュのアクティブオブジェクトが保証されている
        assert obj is not None
        me = obj.data
        assert isinstance(me, bpy.types.Mesh)

        # ボーン由来の頂点グループを特定する。
        # アーマチュアがあればボーン名に一致するグループのみ対象とし、
        # mmd_edge_scale等の制御用グループを除外する。
        # アーマチュアが無い場合は全頂点グループを対象とする。
        arm = find_armature(obj)
        group_names = [g.name for g in obj.vertex_groups]
        if arm is not None:
            arm_data = arm.data
            assert isinstance(arm_data, bpy.types.Armature)
            bone_names = {b.name for b in arm_data.bones}
            bone_group_ids = {i for i, n in enumerate(group_names) if n in bone_names}
        else:
            bone_group_ids = set(range(len(group_names)))

        if not bone_group_ids:
            self.report({'ERROR'}, "ボーンに対応する頂点グループがありません")
            return {'CANCELLED'}

        # 頂点ごとの主ボーン(最大ウェイトのグループ)。無所属は先頭グループへ。
        fallback = min(bone_group_ids)
        vert_primary = []
        for v in me.vertices:
            cands = [g for g in v.groups if g.group in bone_group_ids and g.weight > 0.0]
            if cands:
                vert_primary.append(max(cands, key=lambda g: g.weight).group)
            else:
                vert_primary.append(fallback)

        # 面ごとの割当を多数決で決定(同数なら先頭頂点のグループ)
        if ATTR_NAME in me.attributes:
            me.attributes.remove(me.attributes[ATTR_NAME])
        attr = me.attributes.new(name=ATTR_NAME, type='INT', domain='FACE')
        assert isinstance(attr, bpy.types.IntAttribute)
        for p in me.polygons:
            gs = [vert_primary[vi] for vi in p.vertices]
            counts = Counter(gs)
            best_n = max(counts.values())
            winners = [g for g, n in counts.items() if n == best_n]
            attr.data[p.index].value = gs[0] if len(winners) > 1 else winners[0]

        used_groups = sorted({attr.data[i].value for i in range(len(me.polygons))})
        if len(used_groups) < 2:
            me.attributes.remove(me.attributes[ATTR_NAME])
            self.report({'WARNING'}, "全ての面が同一ボーンに属しているため分離しませんでした")
            return {'CANCELLED'}

        total_faces = len(me.polygons)

        # グループごとに分離(最後の1グループは元オブジェクトに残す)
        view_layer = context.view_layer
        tool_settings = context.tool_settings
        assert view_layer is not None and tool_settings is not None
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        view_layer.objects.active = obj

        bpy.ops.object.mode_set(mode='EDIT')
        tool_settings.mesh_select_mode = (False, False, True)
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        result_objs = {}
        for gi in used_groups[:-1]:
            # separate後にメッシュデータが再構築されるため毎回取得し直す
            od = obj.data
            assert isinstance(od, bpy.types.Mesh)
            a = od.attributes[ATTR_NAME]
            assert isinstance(a, bpy.types.IntAttribute)
            for p in od.polygons:
                p.select = (a.data[p.index].value == gi)
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.separate(type='SELECTED')
            bpy.ops.object.mode_set(mode='OBJECT')
            new_objs = [o for o in context.selected_objects
                        if o != obj and o.type == 'MESH']
            if len(new_objs) != 1:
                self.report({'ERROR'}, f"分離結果が想定外です: {[o.name for o in new_objs]}")
                return {'CANCELLED'}
            result_objs[gi] = new_objs[0]
            new_objs[0].select_set(False)
        result_objs[used_groups[-1]] = obj

        # 後処理: リネーム・属性削除・マテリアルスロット掃除
        face_sum = 0
        for gi, o in result_objs.items():
            od = o.data
            assert isinstance(od, bpy.types.Mesh)
            face_sum += len(od.polygons)
            if ATTR_NAME in od.attributes:
                od.attributes.remove(od.attributes[ATTR_NAME])
            if self.rename_to_bone:
                name = group_names[gi]
                o.name = name
                od.name = name
            if self.cleanup_materials:
                bpy.ops.object.select_all(action='DESELECT')
                o.select_set(True)
                view_layer.objects.active = o
                bpy.ops.object.material_slot_remove_unused()

        # 分離結果を全選択状態にして終了
        bpy.ops.object.select_all(action='DESELECT')
        for o in result_objs.values():
            o.select_set(True)
        view_layer.objects.active = obj

        if face_sum != total_faces:
            self.report({'ERROR'}, f"面数が一致しません: {face_sum} / {total_faces}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"{len(result_objs)}個のオブジェクトに分離しました(面数{face_sum}、欠損なし)")
        return {'FINISHED'}


class VIEW3D_PT_separate_by_bone_weights(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "ボーン分割"
    bl_label = "ボーンウェイトで分離"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        assert layout is not None
        obj = context.active_object
        if obj is not None and obj.type == 'MESH':
            layout.label(text=f"対象: {obj.name}", icon='MESH_DATA')
        else:
            layout.label(text="メッシュを選択してください", icon='INFO')
        layout.operator(OBJECT_OT_separate_by_bone_weights.bl_idname,
                        icon='MOD_ARMATURE')


classes = (
    OBJECT_OT_separate_by_bone_weights,
    VIEW3D_PT_separate_by_bone_weights,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
