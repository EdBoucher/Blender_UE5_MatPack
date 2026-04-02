bl_info = {
    "name": "UV Auto Tile",
    "author": "Ed Boucher",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > UV Tile",
    "description": "Automatically tile UVs across selected coplanar faces, as well as doing simple aspect-correct UV projection for sets of coplanar faces",
    "category": "UV",
}

import bpy
import bmesh
import math
from mathutils import Vector
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup


# ---------------------------------------------------------------------------
# Face grouping
# ---------------------------------------------------------------------------

def _build_adjacency(faces):
    """Build face adjacency dict from shared edges, limited to *faces*."""
    face_set = set(faces)
    adj = {f: [] for f in face_set}
    for f in face_set:
        for edge in f.edges:
            for neighbor in edge.link_faces:
                if neighbor is not f and neighbor in face_set:
                    adj[f].append(neighbor)
    return adj


def _find_connected_components(faces):
    """Partition *faces* into connected components via shared edges."""
    adj = _build_adjacency(faces)
    visited = set()
    components = []
    for f in faces:
        if f in visited:
            continue
        component = []
        queue = [f]
        visited.add(f)
        while queue:
            current = queue.pop()
            component.append(current)
            for neighbor in adj[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


def _split_by_normal(component, threshold_rad):
    """Sub-split a connected component where adjacent normals exceed *threshold_rad*."""
    adj = _build_adjacency(component)
    visited = set()
    groups = []
    for f in component:
        if f in visited:
            continue
        group = []
        queue = [f]
        visited.add(f)
        while queue:
            current = queue.pop()
            group.append(current)
            for neighbor in adj[current]:
                if neighbor not in visited:
                    if current.normal.angle(neighbor.normal) <= threshold_rad:
                        visited.add(neighbor)
                        queue.append(neighbor)
        groups.append(group)
    return groups


# ---------------------------------------------------------------------------
# Arc-length measurement
# ---------------------------------------------------------------------------

def _measure_arc_length_dims(faces, right, local_up):
    """Measure group width/height as arc-length (sum of per-face extents).

    Returns (FW_arc, FH_arc).  For a strip of quads on a curve this gives
    the true surface distance rather than the chord-length bounding box.
    """
    if not faces:
        return 0.0, 0.0

    fw_sum = 0.0
    fh_sum = 0.0
    all_u = []
    all_v = []

    for f in faces:
        us = [v.co.dot(right) for v in f.verts]
        vs = [v.co.dot(local_up) for v in f.verts]
        fw_sum += max(us) - min(us)
        fh_sum += max(vs) - min(vs)
        all_u.extend(us)
        all_v.extend(vs)

    bbox_u = max(all_u) - min(all_u) if all_u else 0.0
    bbox_v = max(all_v) - min(all_v) if all_v else 0.0

    # Estimate how many faces deep the selection is along each axis
    # to avoid overcounting in grid-like selections.
    rows = max(1, round(fw_sum / bbox_u)) if bbox_u > 1e-6 else 1
    cols = max(1, round(fh_sum / bbox_v)) if bbox_v > 1e-6 else 1

    return fw_sum / rows, fh_sum / cols


# ---------------------------------------------------------------------------
# Bleed offset computation
# ---------------------------------------------------------------------------

def _compute_bleed_offsets(FW, FH, NH, NV, bleed):
    """Compute UV offsets to equalise visible bleed on all edges and seams.

    Returns (offset_u, offset_v).
    """
    has_u_div = NH > 1
    has_v_div = NV > 1

    if has_u_div and has_v_div:
        return bleed, bleed

    if has_u_div:
        offset_u = bleed
        u_span = NH + 2 * bleed
        target = 2 * bleed * FW / u_span

        denom = FH - 2 * target
        if abs(denom) > 1e-8:
            offset_v = (target - bleed * FH) / denom
            offset_v = max(-bleed, min(bleed, offset_v))
        else:
            offset_v = bleed
        return offset_u, offset_v

    if has_v_div:
        offset_v = bleed
        v_span = NV + 2 * bleed
        target = 2 * bleed * FH / v_span

        denom = FW - 2 * target
        if abs(denom) > 1e-8:
            offset_u = (target - bleed * FW) / denom
            offset_u = max(-bleed, min(bleed, offset_u))
        else:
            offset_u = bleed
        return offset_u, offset_v

    # No divisions (NH == 1, NV == 1)
    bv_u = bleed * FW
    bv_v = bleed * FH

    if abs(bv_u - bv_v) < 1e-8:
        return 0.0, 0.0

    if bv_u > bv_v:
        target = bv_v
        denom = FW - 2 * target
        if abs(denom) > 1e-8:
            offset_u = (target - bleed * FW) / denom
            offset_u = max(-bleed, min(bleed, offset_u))
        else:
            offset_u = 0.0
        return offset_u, 0.0
    else:
        target = bv_u
        denom = FH - 2 * target
        if abs(denom) > 1e-8:
            offset_v = (target - bleed * FH) / denom
            offset_v = max(-bleed, min(bleed, offset_v))
        else:
            offset_v = 0.0
        return 0.0, offset_v


# ---------------------------------------------------------------------------
# Per-group UV processing
# ---------------------------------------------------------------------------

def _process_face_group(faces, uv_layer, repetitions, auto_mode, tile_axis,
                        texture_size, bleed_px, arc_length):
    """Tile UVs on a single group of faces.

    Returns (face_count, N_used).
    """
    if not faces:
        return 0, 0

    # Average normal
    avg_normal = Vector((0, 0, 0))
    for f in faces:
        avg_normal += f.normal
    avg_normal.normalize()

    # Build local 2D coordinate frame
    up = Vector((0, 0, 1))
    right = up.cross(avg_normal)

    if right.length < 1e-4:
        right = Vector((1, 0, 0))
    else:
        right.normalize()

    local_up = avg_normal.cross(right)
    local_up.normalize()

    # Project verts to 2D
    verts = set()
    for f in faces:
        for v in f.verts:
            verts.add(v)

    coords = {}
    for v in verts:
        coords[v.index] = (v.co.dot(right), v.co.dot(local_up))

    # Bounding box (always needed for UV normalisation)
    all_x = [c[0] for c in coords.values()]
    all_y = [c[1] for c in coords.values()]
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    bbox_w = max_x - min_x
    bbox_h = max_y - min_y

    if bbox_w < 1e-6 and bbox_h < 1e-6:
        return 0, 0

    # Face dimensions for tiling / bleed calculations
    if arc_length and len(faces) > 1:
        FW, FH = _measure_arc_length_dims(faces, right, local_up)
    else:
        FW, FH = bbox_w, bbox_h

    # Determine tiling direction
    if tile_axis == 'AUTO':
        tile_horizontal = FW >= FH
    elif tile_axis == 'HORIZONTAL':
        tile_horizontal = True
    else:
        tile_horizontal = False

    # Determine repetitions
    if auto_mode:
        if tile_horizontal:
            N = max(1, round(FW / FH)) if FH > 1e-6 else 1
        else:
            N = max(1, round(FH / FW)) if FW > 1e-6 else 1
    else:
        N = repetitions

    if tile_horizontal:
        NH, NV = N, 1
    else:
        NH, NV = 1, N

    bleed = bleed_px / texture_size if texture_size > 0 and bleed_px > 0 else 0.0

    # Compute bleed offsets
    if bleed > 0 and FW > 1e-6 and FH > 1e-6:
        offset_u, offset_v = _compute_bleed_offsets(FW, FH, NH, NV, bleed)
    else:
        offset_u, offset_v = 0.0, 0.0

    u_span = NH + 2 * offset_u
    v_span = NV + 2 * offset_v

    # Set UVs
    face_count = 0
    for f in faces:
        for loop in f.loops:
            lx, ly = coords[loop.vert.index]
            t_x = (lx - min_x) / bbox_w if bbox_w > 1e-6 else 0.0
            t_y = (ly - min_y) / bbox_h if bbox_h > 1e-6 else 0.0
            u = -offset_u + t_x * u_span
            v = -offset_v + t_y * v_span
            loop[uv_layer].uv = (u, v)
        face_count += 1

    return face_count, N


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def auto_tile_uvs(bm, uv_layer, repetitions, auto_mode, tile_axis,
                  texture_size, bleed_px, normal_threshold, arc_length):
    """Tile UVs across all selected faces, grouping by connectivity and normals.

    Returns (total_face_count, max_N_used).
    """
    selected_faces = [f for f in bm.faces if f.select]
    if not selected_faces:
        return 0, 0

    # Step 1: connected components
    components = _find_connected_components(selected_faces)

    # Step 2: split each component by normal threshold
    groups = []
    for comp in components:
        groups.extend(_split_by_normal(comp, normal_threshold))

    # Step 3: process each group independently
    total_faces = 0
    max_n = 0
    for group in groups:
        fc, n = _process_face_group(
            group, uv_layer, repetitions, auto_mode, tile_axis,
            texture_size, bleed_px, arc_length,
        )
        total_faces += fc
        max_n = max(max_n, n)

    return total_faces, max_n


# ---------------------------------------------------------------------------
# Aspect-correct projection
# ---------------------------------------------------------------------------

def _process_aspect_group(faces, uv_layer, flip_u, flip_v, rotation_deg, infer_local):
    """Project UVs for a face group preserving aspect ratio, centred at (0.5, 0.5)."""
    if not faces:
        return 0

    # Average normal
    avg_normal = Vector((0, 0, 0))
    for f in faces:
        avg_normal += f.normal
    avg_normal.normalize()

    # Build local 2D coordinate frame
    up = Vector((0, 0, 1))
    right = up.cross(avg_normal)
    if right.length < 1e-4:
        right = Vector((1, 0, 0))
    else:
        right.normalize()
    local_up = avg_normal.cross(right)
    local_up.normalize()

    # Project verts to 2D
    verts = set()
    for f in faces:
        for v in f.verts:
            verts.add(v)

    coords = {}
    for v in verts:
        coords[v.index] = (v.co.dot(right), v.co.dot(local_up))

    # PCA: rotate basis to align with vertex distribution
    if infer_local and len(verts) >= 2:
        cx = sum(c[0] for c in coords.values()) / len(coords)
        cy = sum(c[1] for c in coords.values()) / len(coords)

        cxx = cyy = cxy = 0.0
        for x, y in coords.values():
            dx, dy = x - cx, y - cy
            cxx += dx * dx
            cyy += dy * dy
            cxy += dx * dy

        angle = 0.5 * math.atan2(2.0 * cxy, cxx - cyy)
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        new_right = right * cos_a + local_up * sin_a
        new_up = -right * sin_a + local_up * cos_a
        new_right.normalize()
        new_up.normalize()
        right = new_right
        local_up = new_up

        coords = {}
        for v in verts:
            coords[v.index] = (v.co.dot(right), v.co.dot(local_up))

    all_x = [c[0] for c in coords.values()]
    all_y = [c[1] for c in coords.values()]
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)
    bbox_w = max_x - min_x
    bbox_h = max_y - min_y

    # Ensure right = longest dimension
    if infer_local and bbox_h > bbox_w:
        right, local_up = local_up, right
        coords = {}
        for v in verts:
            coords[v.index] = (v.co.dot(right), v.co.dot(local_up))
        all_x = [c[0] for c in coords.values()]
        all_y = [c[1] for c in coords.values()]
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
        bbox_w = max_x - min_x
        bbox_h = max_y - min_y

    if bbox_w < 1e-6 and bbox_h < 1e-6:
        return 0

    # Normalise to [0,1] preserving aspect ratio, centred at (0.5, 0.5)
    if bbox_w >= bbox_h:
        u_scale = 1.0
        v_scale = bbox_h / bbox_w if bbox_w > 1e-6 else 1.0
    else:
        v_scale = 1.0
        u_scale = bbox_w / bbox_h if bbox_h > 1e-6 else 1.0

    u_offset = (1.0 - u_scale) / 2.0
    v_offset = (1.0 - v_scale) / 2.0

    # Precompute rotation (sin/cos for 0/90/180/270)
    angle_rad = math.radians(rotation_deg)
    cos_a = round(math.cos(angle_rad))
    sin_a = round(math.sin(angle_rad))

    face_count = 0
    for f in faces:
        for loop in f.loops:
            lx, ly = coords[loop.vert.index]
            t_x = (lx - min_x) / bbox_w if bbox_w > 1e-6 else 0.5
            t_y = (ly - min_y) / bbox_h if bbox_h > 1e-6 else 0.5

            u = u_offset + t_x * u_scale
            v = v_offset + t_y * v_scale

            # Flip
            if flip_u:
                u = 1.0 - u
            if flip_v:
                v = 1.0 - v

            # Rotate around (0.5, 0.5)
            if rotation_deg != 0:
                cu, cv = u - 0.5, v - 0.5
                u = cos_a * cu - sin_a * cv + 0.5
                v = sin_a * cu + cos_a * cv + 0.5

            loop[uv_layer].uv = (u, v)
        face_count += 1

    return face_count


def aspect_correct_uvs(bm, uv_layer, flip_u, flip_v, rotation_deg, normal_threshold, infer_local):
    """Run aspect-correct projection on all selected faces, grouped by connectivity and normals."""
    selected_faces = [f for f in bm.faces if f.select]
    if not selected_faces:
        return 0

    components = _find_connected_components(selected_faces)

    groups = []
    for comp in components:
        groups.extend(_split_by_normal(comp, normal_threshold))

    total_faces = 0
    for group in groups:
        total_faces += _process_aspect_group(
            group, uv_layer, flip_u, flip_v, rotation_deg, infer_local,
        )

    return total_faces


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class UVAutoTileProperties(PropertyGroup):
    repetitions: IntProperty(
        name="Repetitions",
        description="Number of tile repetitions along the tiling axis",
        default=1,
        min=1,
    )
    auto_mode: BoolProperty(
        name="Auto",
        description="Auto-calculate repetitions from the aspect ratio",
        default=False,
    )
    tile_axis: EnumProperty(
        name="Tile Axis",
        description="Which axis to tile along",
        items=[
            ('AUTO', "Auto", "Tile along the longer face dimension"),
            ('HORIZONTAL', "Horizontal", "Tile along U"),
            ('VERTICAL', "Vertical", "Tile along V"),
        ],
        default='AUTO',
    )
    uv_layer: StringProperty(
        name="UV Layer",
        description="UV layer to write to (empty = active layer)",
        default="",
    )
    texture_size: IntProperty(
        name="Texture Size",
        description="Texture dimension in pixels (assumes square texture)",
        default=1024,
        min=1,
    )
    bleed: IntProperty(
        name="Bleed",
        description="Bleed pixels per side of the texture",
        default=64,
        min=0,
    )
    normal_threshold: FloatProperty(
        name="Normal Threshold",
        description="Max angle between adjacent face normals to keep them in the same group",
        default=math.radians(30),
        min=0.0,
        max=math.pi,
        subtype='ANGLE',
    )
    arc_length_mode: BoolProperty(
        name="Arc Length",
        description="Measure face dimensions by summing individual face extents (arc length) "
                    "instead of bounding box (chord length)",
        default=True,
    )

    # Aspect Correct Projection
    flip_u: BoolProperty(
        name="Flip U",
        description="Mirror UV coordinates along the U axis",
        default=False,
    )
    flip_v: BoolProperty(
        name="Flip V",
        description="Mirror UV coordinates along the V axis",
        default=False,
    )
    aspect_rotation: EnumProperty(
        name="Rotation",
        description="Rotate UVs around the centre of UV space after projection",
        items=[
            ('0', "0", "No rotation"),
            ('90', "90", "Rotate 90 degrees"),
            ('180', "180", "Rotate 180 degrees"),
            ('270', "270", "Rotate 270 degrees"),
        ],
        default='0',
    )
    infer_local: BoolProperty(
        name="Infer Local Coordinates",
        description="Determine projection axes from vertex distribution instead of world axes",
        default=True,
    )


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class UVAUTOTILE_OT_apply(Operator):
    bl_idname = "uv_auto_tile.apply"
    bl_label = "Apply UV Auto Tile"
    bl_description = "Set UVs on selected faces to tile a texture with uniform bleed"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        if obj.mode != 'EDIT':
            return False
        ts = context.tool_settings
        if not ts.mesh_select_mode[2]:
            return False
        return True

    def execute(self, context):
        obj = context.active_object
        props = context.scene.uv_auto_tile

        bm = bmesh.from_edit_mesh(obj.data)

        layer_name = props.uv_layer.strip()
        if layer_name:
            uv_layer = bm.loops.layers.uv.get(layer_name)
            if uv_layer is None:
                self.report({'ERROR'}, f"UV layer '{layer_name}' not found")
                return {'CANCELLED'}
        else:
            uv_layer = bm.loops.layers.uv.active
            if uv_layer is None:
                uv_layer = bm.loops.layers.uv.new("UVMap")

        face_count, n_used = auto_tile_uvs(
            bm, uv_layer, props.repetitions, props.auto_mode,
            props.tile_axis, props.texture_size, props.bleed,
            props.normal_threshold, props.arc_length_mode,
        )

        if face_count == 0:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class UVAUTOTILE_PT_main(Panel):
    bl_label = "UV Auto Tile"
    bl_idname = "UVAUTOTILE_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Tile"
    bl_context = "mesh_edit"

    def draw(self, context):
        layout = self.layout
        props = context.scene.uv_auto_tile
        obj = context.active_object

        if obj and obj.type == 'MESH':
            layout.prop_search(props, "uv_layer", obj.data, "uv_layers",
                               text="UV Layer")

        layout.prop(props, "tile_axis")
        layout.prop(props, "auto_mode")

        row = layout.row()
        row.enabled = not props.auto_mode
        row.prop(props, "repetitions")

        layout.separator()

        box = layout.box()
        box.label(text="Grouping")
        box.prop(props, "normal_threshold")
        box.prop(props, "arc_length_mode")

        layout.separator()

        box = layout.box()
        box.label(text="Texture")
        box.prop(props, "texture_size")
        box.prop(props, "bleed")

        layout.separator()
        layout.operator("uv_auto_tile.apply", icon='UV')


# ---------------------------------------------------------------------------
# Aspect Correct Projection operator + panel
# ---------------------------------------------------------------------------

class UVAUTOTILE_OT_aspect_correct(Operator):
    bl_idname = "uv_auto_tile.aspect_correct"
    bl_label = "Process UVs"
    bl_description = "Set UVs to match the aspect ratio of each face group, centred in UV space"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False
        if obj.mode != 'EDIT':
            return False
        ts = context.tool_settings
        if not ts.mesh_select_mode[2]:
            return False
        return True

    def execute(self, context):
        obj = context.active_object
        props = context.scene.uv_auto_tile

        bm = bmesh.from_edit_mesh(obj.data)

        layer_name = props.uv_layer.strip()
        if layer_name:
            uv_layer = bm.loops.layers.uv.get(layer_name)
            if uv_layer is None:
                self.report({'ERROR'}, f"UV layer '{layer_name}' not found")
                return {'CANCELLED'}
        else:
            uv_layer = bm.loops.layers.uv.active
            if uv_layer is None:
                uv_layer = bm.loops.layers.uv.new("UVMap")

        rotation_deg = int(props.aspect_rotation)

        face_count = aspect_correct_uvs(
            bm, uv_layer, props.flip_u, props.flip_v,
            rotation_deg, props.normal_threshold, props.infer_local,
        )

        if face_count == 0:
            self.report({'WARNING'}, "No faces selected")
            return {'CANCELLED'}

        bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


class UVAUTOTILE_PT_aspect_correct(Panel):
    bl_label = "Aspect Correct Projection"
    bl_idname = "UVAUTOTILE_PT_aspect_correct"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Tile"
    bl_context = "mesh_edit"

    def draw(self, context):
        layout = self.layout
        props = context.scene.uv_auto_tile

        layout.prop(props, "infer_local")
        layout.prop(props, "flip_u")
        layout.prop(props, "flip_v")
        layout.prop(props, "aspect_rotation")

        layout.separator()
        layout.operator("uv_auto_tile.aspect_correct", icon='UV')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    UVAutoTileProperties,
    UVAUTOTILE_OT_apply,
    UVAUTOTILE_PT_main,
    UVAUTOTILE_OT_aspect_correct,
    UVAUTOTILE_PT_aspect_correct,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.uv_auto_tile = PointerProperty(type=UVAutoTileProperties)


def unregister():
    del bpy.types.Scene.uv_auto_tile
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
