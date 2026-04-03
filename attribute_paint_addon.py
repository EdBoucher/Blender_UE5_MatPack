bl_info = {
    "name": "Attribute Paint",
    "author": "Ed Boucher",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Attr Paint",
    "description": "Streamlined per-face/edge/vertex attribute editing in edit mode",
    "category": "Mesh",
}

import bpy
import bmesh
import random
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BUILTIN_ATTRIBUTES = frozenset({
    "position", "normal", "sharp_face", "sharp_edge",
    "crease", "material_index",
    "UVMap", "uv0", "uv1", "uv2",
})

_ALLOWED_DOMAINS = {'POINT', 'EDGE', 'FACE'}
_ALLOWED_TYPES = {'FLOAT', 'INT', 'BOOLEAN'}

_DOMAIN_LABEL = {'POINT': "Vertex", 'EDGE': "Edge", 'FACE': "Face"}
_TYPE_LABEL = {'FLOAT': "Float", 'INT': "Int", 'BOOLEAN': "Bool"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_edit_mesh(context):
    obj = context.active_object
    return obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT'


def _get_filtered_attributes(mesh):
    """Return {name: (domain, data_type)} for qualifying attributes."""
    color_names = {ca.name for ca in mesh.color_attributes}
    result = {}
    for attr in mesh.attributes:
        if attr.name in _BUILTIN_ATTRIBUTES:
            continue
        if attr.name.startswith('.'):
            continue
        if attr.name in color_names:
            continue
        if attr.domain not in _ALLOWED_DOMAINS:
            continue
        if attr.data_type not in _ALLOWED_TYPES:
            continue
        result[attr.name] = (attr.domain, attr.data_type)
    return result


def _sync_attribute_list(context):
    """Reconcile the UIList collection with the mesh's actual attributes."""
    obj = context.active_object
    if not obj or obj.type != 'MESH':
        return
    props = context.scene.attr_paint
    valid = _get_filtered_attributes(obj.data)

    # Remove stale items (backwards)
    for i in range(len(props.attr_list) - 1, -1, -1):
        item = props.attr_list[i]
        if item.name not in valid:
            props.attr_list.remove(i)
        else:
            # Update domain/type in case they changed
            domain, dtype = valid[item.name]
            item.domain = domain
            item.data_type = dtype

    # Add missing items
    existing_names = {item.name for item in props.attr_list}
    for name, (domain, dtype) in valid.items():
        if name not in existing_names:
            item = props.attr_list.add()
            item["_old_name"] = name
            item.name = name
            item.domain = domain
            item.data_type = dtype

    # Clamp index
    if props.active_index >= len(props.attr_list):
        props.active_index = max(0, len(props.attr_list) - 1)


def _get_bmesh_layer(bm, attr_domain, attr_dtype, attr_name):
    """Get the bmesh custom data layer for the given attribute."""
    if attr_domain == 'POINT':
        layers = bm.verts.layers
    elif attr_domain == 'EDGE':
        layers = bm.edges.layers
    else:
        layers = bm.faces.layers

    # BOOLEAN is stored as int in bmesh
    if attr_dtype in ('INT', 'BOOLEAN'):
        return layers.int.get(attr_name)
    else:
        return layers.float.get(attr_name)


def _resolve_targets(bm, attr_domain, context):
    """Return bmesh elements to write to, handling cross-domain mapping."""
    sel_vert, sel_edge, sel_face = context.tool_settings.mesh_select_mode

    if attr_domain == 'FACE':
        if sel_face:
            return [f for f in bm.faces if f.select]
        elif sel_edge:
            return [f for f in bm.faces if all(e.select for e in f.edges)]
        else:
            return [f for f in bm.faces if all(v.select for v in f.verts)]

    elif attr_domain == 'EDGE':
        if sel_edge:
            return [e for e in bm.edges if e.select]
        elif sel_face:
            edges = set()
            for f in bm.faces:
                if f.select:
                    edges.update(f.edges)
            return list(edges)
        else:
            return [e for e in bm.edges if all(v.select for v in e.verts)]

    elif attr_domain == 'POINT':
        if sel_vert:
            return [v for v in bm.verts if v.select]
        elif sel_face:
            verts = set()
            for f in bm.faces:
                if f.select:
                    verts.update(f.verts)
            return list(verts)
        else:
            verts = set()
            for e in bm.edges:
                if e.select:
                    verts.update(e.verts)
            return list(verts)

    return []


# ---------------------------------------------------------------------------
# Property Groups
# ---------------------------------------------------------------------------

def _on_attr_name_update(self, context):
    """Rename the mesh attribute when the user edits the name in the list."""
    print("Rename triggered")
    
    obj = context.active_object
    if not obj or obj.type != 'MESH':
        return
    old = self.get("_old_name", "")
    new = self.name

    mesh = obj.data
    attr = mesh.attributes.get(old)
    if attr is not None:
        attr.name = new
    self["_old_name"] = new


class AttrPaintAttrItem(PropertyGroup):
    name: StringProperty(update=_on_attr_name_update)
    domain: StringProperty()
    data_type: StringProperty()


class AttrPaintProperties(PropertyGroup):
    attr_list: CollectionProperty(type=AttrPaintAttrItem)
    active_index: IntProperty(default=0)
    val_float: FloatProperty(name="Value", default=0.0)
    val_int: IntProperty(name="Value", default=0)
    val_bool: BoolProperty(name="Value", default=False)

    # Paint Random
    rand_min_float: FloatProperty(name="Min", default=0.0)
    rand_max_float: FloatProperty(name="Max", default=1.0)
    rand_min_int: IntProperty(name="Min", default=0)
    rand_max_int: IntProperty(name="Max", default=1)

    # Paint Index
    index_mode: EnumProperty(
        name="Index Mode",
        items=[
            ('SELECTION', "Selection", "Index within current selection"),
            ('MODEL', "Model Index", "Element index in the mesh"),
        ],
        default='SELECTION',
    )
    index_step_float: FloatProperty(name="Step", default=1.0)
    index_step_int: IntProperty(name="Step", default=1)
    index_normalize: BoolProperty(name="Normalize", default=False)


# ---------------------------------------------------------------------------
# UIList
# ---------------------------------------------------------------------------

class ATTRPAINT_UL_attributes(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_property, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "name", text="", emboss=False)
            sub = row.row(align=True)
            sub.alignment = 'RIGHT'
            sub.label(text=_DOMAIN_LABEL.get(item.domain, item.domain))
            sub.label(text=_TYPE_LABEL.get(item.data_type, item.data_type))
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.name)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class ATTRPAINT_OT_add(Operator):
    bl_idname = "attr_paint.add"
    bl_label = "Add Attribute"
    bl_description = "Add a new attribute to the mesh"
    bl_options = {'REGISTER', 'UNDO'}

    attr_name: StringProperty(name="Name", default="Attribute")
    attr_domain: EnumProperty(
        name="Domain",
        items=[
            ('FACE', "Face", "One value per face"),
            ('EDGE', "Edge", "One value per edge"),
            ('POINT', "Vertex", "One value per vertex"),
        ],
        default='FACE',
    )
    attr_type: EnumProperty(
        name="Type",
        items=[
            ('INT', "Integer", "Integer values"),
            ('FLOAT', "Float", "Floating-point values"),
            ('BOOLEAN', "Boolean", "True/False values"),
        ],
        default='INT',
    )

    @classmethod
    def poll(cls, context):
        return _is_edit_mesh(context)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "attr_name")
        layout.prop(self, "attr_domain")
        layout.prop(self, "attr_type")

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data

        # Must be in object mode to add attributes
        bpy.ops.object.mode_set(mode='OBJECT')
        try:
            mesh.attributes.new(
                name=self.attr_name,
                type=self.attr_type,
                domain=self.attr_domain,
            )
        except RuntimeError as e:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"Added attribute '{self.attr_name}'")
        return {'FINISHED'}


class ATTRPAINT_OT_remove(Operator):
    bl_idname = "attr_paint.remove"
    bl_label = "Remove Attribute"
    bl_description = "Remove the selected attribute from the mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _is_edit_mesh(context):
            return False
        props = context.scene.attr_paint
        return 0 <= props.active_index < len(props.attr_list)

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data
        props = context.scene.attr_paint
        item = props.attr_list[props.active_index]
        attr_name = item.name

        attr = mesh.attributes.get(attr_name)
        if attr is None:
            self.report({'WARNING'}, f"Attribute '{attr_name}' not found")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='OBJECT')
        try:
            mesh.attributes.remove(attr)
        except RuntimeError as e:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"Removed attribute '{attr_name}'")
        return {'FINISHED'}


class ATTRPAINT_OT_apply(Operator):
    bl_idname = "attr_paint.apply"
    bl_label = "Apply"
    bl_description = "Apply the value to selected geometry"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _is_edit_mesh(context):
            return False
        props = context.scene.attr_paint
        return 0 <= props.active_index < len(props.attr_list)

    def execute(self, context):
        obj = context.active_object
        props = context.scene.attr_paint
        item = props.attr_list[props.active_index]

        attr_domain = item.domain
        attr_dtype = item.data_type
        attr_name = item.name

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        layer = _get_bmesh_layer(bm, attr_domain, attr_dtype, attr_name)
        if layer is None:
            # Fallback: try object-mode API
            return self._apply_object_mode(context, obj, props, item)

        if attr_dtype == 'FLOAT':
            value = props.val_float
        elif attr_dtype == 'INT':
            value = props.val_int
        else:
            value = int(props.val_bool)

        targets = _resolve_targets(bm, attr_domain, context)
        for elem in targets:
            elem[layer] = value

        bmesh.update_edit_mesh(obj.data)

        self.report({'INFO'}, f"Applied to {len(targets)} elements")
        return {'FINISHED'}

    def _apply_object_mode(self, context, obj, props, item):
        """Fallback for attributes not exposed in bmesh (e.g. BOOLEAN)."""
        attr_domain = item.domain
        attr_dtype = item.data_type
        attr_name = item.name

        # Gather target indices while still in edit mode
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        targets = _resolve_targets(bm, attr_domain, context)
        indices = {elem.index for elem in targets}

        bpy.ops.object.mode_set(mode='OBJECT')

        attr = obj.data.attributes.get(attr_name)
        if attr is None:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, f"Attribute '{attr_name}' not found")
            return {'CANCELLED'}

        if attr_dtype == 'FLOAT':
            value = props.val_float
        elif attr_dtype == 'INT':
            value = props.val_int
        else:
            value = props.val_bool

        for i in indices:
            attr.data[i].value = value

        bpy.ops.object.mode_set(mode='EDIT')
        self.report({'INFO'}, f"Applied to {len(indices)} elements")
        return {'FINISHED'}


class ATTRPAINT_OT_apply_random(Operator):
    bl_idname = "attr_paint.apply_random"
    bl_label = "Apply"
    bl_description = "Apply a random value to each selected element"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _is_edit_mesh(context):
            return False
        props = context.scene.attr_paint
        return 0 <= props.active_index < len(props.attr_list)

    def execute(self, context):
        obj = context.active_object
        props = context.scene.attr_paint
        item = props.attr_list[props.active_index]

        attr_domain = item.domain
        attr_dtype = item.data_type
        attr_name = item.name

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        layer = _get_bmesh_layer(bm, attr_domain, attr_dtype, attr_name)
        if layer is None:
            return self._apply_random_object_mode(context, obj, props, item)

        targets = _resolve_targets(bm, attr_domain, context)
        for elem in targets:
            if attr_dtype == 'FLOAT':
                elem[layer] = random.uniform(props.rand_min_float, props.rand_max_float)
            elif attr_dtype == 'INT':
                elem[layer] = random.randint(props.rand_min_int, props.rand_max_int)
            else:
                elem[layer] = random.choice([0, 1])

        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"Randomized {len(targets)} elements")
        return {'FINISHED'}

    def _apply_random_object_mode(self, context, obj, props, item):
        """Fallback for attributes not exposed in bmesh."""
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        targets = _resolve_targets(bm, item.domain, context)
        indices = {elem.index for elem in targets}

        bpy.ops.object.mode_set(mode='OBJECT')
        attr = obj.data.attributes.get(item.name)
        if attr is None:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, f"Attribute '{item.name}' not found")
            return {'CANCELLED'}

        for i in indices:
            if item.data_type == 'FLOAT':
                attr.data[i].value = random.uniform(props.rand_min_float, props.rand_max_float)
            elif item.data_type == 'INT':
                attr.data[i].value = random.randint(props.rand_min_int, props.rand_max_int)
            else:
                attr.data[i].value = random.choice([True, False])

        bpy.ops.object.mode_set(mode='EDIT')
        self.report({'INFO'}, f"Randomized {len(indices)} elements")
        return {'FINISHED'}


class ATTRPAINT_OT_apply_index(Operator):
    bl_idname = "attr_paint.apply_index"
    bl_label = "Apply"
    bl_description = "Write element indices to the selected geometry"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _is_edit_mesh(context):
            return False
        props = context.scene.attr_paint
        if not (0 <= props.active_index < len(props.attr_list)):
            return False
        return props.attr_list[props.active_index].data_type != 'BOOLEAN'

    def execute(self, context):
        obj = context.active_object
        props = context.scene.attr_paint
        item = props.attr_list[props.active_index]

        attr_domain = item.domain
        attr_dtype = item.data_type
        attr_name = item.name

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        layer = _get_bmesh_layer(bm, attr_domain, attr_dtype, attr_name)
        if layer is None:
            return self._apply_index_object_mode(context, obj, props, item)

        targets = _resolve_targets(bm, attr_domain, context)
        targets.sort(key=lambda e: e.index)

        count = max(len(targets) - 1, 1)
        use_normalize = props.index_normalize and attr_dtype == 'FLOAT'

        for sel_i, elem in enumerate(targets):
            raw_index = elem.index if props.index_mode == 'MODEL' else sel_i
            if use_normalize:
                value = raw_index / count
            elif attr_dtype == 'FLOAT':
                value = raw_index * props.index_step_float
            else:
                value = int(raw_index * props.index_step_int)
            elem[layer] = value

        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"Indexed {len(targets)} elements")
        return {'FINISHED'}

    def _apply_index_object_mode(self, context, obj, props, item):
        """Fallback for attributes not exposed in bmesh."""
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        targets = _resolve_targets(bm, item.domain, context)
        targets.sort(key=lambda e: e.index)
        index_list = [(e.index, sel_i) for sel_i, e in enumerate(targets)]

        bpy.ops.object.mode_set(mode='OBJECT')
        attr = obj.data.attributes.get(item.name)
        if attr is None:
            bpy.ops.object.mode_set(mode='EDIT')
            self.report({'ERROR'}, f"Attribute '{item.name}' not found")
            return {'CANCELLED'}

        count = max(len(index_list) - 1, 1)
        use_normalize = props.index_normalize and item.data_type == 'FLOAT'

        for elem_idx, sel_i in index_list:
            raw_index = elem_idx if props.index_mode == 'MODEL' else sel_i
            if use_normalize:
                value = raw_index / count
            elif item.data_type == 'FLOAT':
                value = raw_index * props.index_step_float
            else:
                value = int(raw_index * props.index_step_int)
            attr.data[elem_idx].value = value

        bpy.ops.object.mode_set(mode='EDIT')
        self.report({'INFO'}, f"Indexed {len(index_list)} elements")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

class ATTRPAINT_PT_main(Panel):
    bl_label = "Attribute Paint"
    bl_idname = "ATTRPAINT_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Attr Paint"

    @classmethod
    def poll(cls, context):
        return _is_edit_mesh(context)

    def draw(self, context):
        layout = self.layout
        props = context.scene.attr_paint

        row = layout.row()
        row.template_list(
            "ATTRPAINT_UL_attributes", "",
            props, "attr_list",
            props, "active_index",
            rows=3,
        )
        col = row.column(align=True)
        col.operator("attr_paint.add", icon='ADD', text="")
        col.operator("attr_paint.remove", icon='REMOVE', text="")


class ATTRPAINT_PT_value(Panel):
    bl_label = "Value"
    bl_idname = "ATTRPAINT_PT_value"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Attr Paint"
    bl_parent_id = "ATTRPAINT_PT_main"
    bl_options = set()

    @classmethod
    def poll(cls, context):
        if not _is_edit_mesh(context):
            return False
        props = context.scene.attr_paint
        return 0 <= props.active_index < len(props.attr_list)

    def draw(self, context):
        layout = self.layout
        props = context.scene.attr_paint
        item = props.attr_list[props.active_index]

        if item.data_type == 'FLOAT':
            layout.prop(props, "val_float")
        elif item.data_type == 'INT':
            layout.prop(props, "val_int")
        elif item.data_type == 'BOOLEAN':
            layout.prop(props, "val_bool")

        layout.operator("attr_paint.apply")


class ATTRPAINT_PT_random(Panel):
    bl_label = "Paint Random"
    bl_idname = "ATTRPAINT_PT_random"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Attr Paint"
    bl_parent_id = "ATTRPAINT_PT_main"
    bl_options = set()

    @classmethod
    def poll(cls, context):
        if not _is_edit_mesh(context):
            return False
        props = context.scene.attr_paint
        return 0 <= props.active_index < len(props.attr_list)

    def draw(self, context):
        layout = self.layout
        props = context.scene.attr_paint
        item = props.attr_list[props.active_index]

        if item.data_type == 'FLOAT':
            row = layout.row(align=True)
            row.prop(props, "rand_min_float")
            row.prop(props, "rand_max_float")
        elif item.data_type == 'INT':
            row = layout.row(align=True)
            row.prop(props, "rand_min_int")
            row.prop(props, "rand_max_int")
        else:
            layout.label(text="Random true / false")

        layout.operator("attr_paint.apply_random")


class ATTRPAINT_PT_index(Panel):
    bl_label = "Paint Index"
    bl_idname = "ATTRPAINT_PT_index"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Attr Paint"
    bl_parent_id = "ATTRPAINT_PT_main"
    bl_options = set()

    @classmethod
    def poll(cls, context):
        if not _is_edit_mesh(context):
            return False
        props = context.scene.attr_paint
        if not (0 <= props.active_index < len(props.attr_list)):
            return False
        return props.attr_list[props.active_index].data_type != 'BOOLEAN'

    def draw(self, context):
        layout = self.layout
        props = context.scene.attr_paint
        item = props.attr_list[props.active_index]

        layout.prop(props, "index_mode")

        if item.data_type == 'FLOAT':
            layout.prop(props, "index_normalize")

        use_normalize = props.index_normalize and item.data_type == 'FLOAT'

        if item.data_type == 'FLOAT':
            row = layout.row()
            row.enabled = not use_normalize
            row.prop(props, "index_step_float")
        else:
            layout.prop(props, "index_step_int")

        layout.operator("attr_paint.apply_index")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _on_depsgraph_update(scene, depsgraph):
    context = bpy.context
    if _is_edit_mesh(context):
        _sync_attribute_list(context)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    AttrPaintAttrItem,
    AttrPaintProperties,
    ATTRPAINT_UL_attributes,
    ATTRPAINT_OT_add,
    ATTRPAINT_OT_remove,
    ATTRPAINT_OT_apply,
    ATTRPAINT_OT_apply_random,
    ATTRPAINT_OT_apply_index,
    ATTRPAINT_PT_main,
    ATTRPAINT_PT_value,
    ATTRPAINT_PT_random,
    ATTRPAINT_PT_index,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.attr_paint = PointerProperty(type=AttrPaintProperties)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister():
    bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    del bpy.types.Scene.attr_paint
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
