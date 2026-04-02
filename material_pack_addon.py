bl_info = {
    "name": "MatPack",
    "author": "Ed Boucher",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > MatPack",
    "description": "MatPack extracts material properties into a texture atlas, and remaps the UVs of models to match. It also allows encoding arbitrary mesh attributes to another UV map.",
    "category": "Material",
}

import bpy
import bmesh
import hashlib
import json
import math
import os
from bpy.props import (
    BoolProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, value))


def next_power_of_two(n):
    """Return the smallest power of two >= n (minimum 1)."""
    if n <= 1:
        return 1
    return 1 << (n - 1).bit_length()


def calculate_grid_size(material_count, min_grid_size):
    """Smallest power-of-two k >= min_grid_size where k*k >= material_count."""
    k = max(next_power_of_two(min_grid_size), 1)
    while k * k < material_count:
        k *= 2
    return k


def linearToArrayIndex(value, numElements):
    """Quantize a 0-1 float to an integer grid index."""
    value = clamp(value)
    return max(math.floor(value * numElements - 0.5), 0)


def map_four_values_to_grid(numCells, outer_x, outer_y, inner_x, inner_y):
    """Encode 4 float values (0-1) into a UV coordinate via nested grid."""
    mainCellWidth = 1.0 / numCells
    innerCellWidth = mainCellWidth / numCells
    toCentre = innerCellWidth * 0.5

    ox = linearToArrayIndex(clamp(outer_x), numCells)
    oy = linearToArrayIndex(clamp(outer_y), numCells)
    ix = linearToArrayIndex(clamp(inner_x), numCells)
    iy = linearToArrayIndex(clamp(inner_y), numCells)

    u = ox * mainCellWidth + ix * innerCellWidth + toCentre
    v = oy * mainCellWidth + iy * innerCellWidth + toCentre

    return (u, 1.0 - v)


def material_property_id(metallic, base_color, roughness, precision=4):
    """Return a deterministic 12-char hex ID from material properties."""
    canonical = (
        metallic,
        round(base_color[0], precision),
        round(base_color[1], precision),
        round(base_color[2], precision),
        round(roughness, precision),
    )
    digest = hashlib.md5(str(canonical).encode()).hexdigest()
    return digest[:12]


# ---------------------------------------------------------------------------
# Attribute / Property Resolution
# ---------------------------------------------------------------------------

_MATERIAL_PROPS = {"roughness", "metallic", "emission"}


def resolve_source_value(mesh, poly, obj, source_name, do_clamp=True):
    """Return a float for the given source on this face.

    When *do_clamp* is True the result is clamped to [0, 1].
    """
    source_name = source_name.strip()
    if not source_name:
        return 0.0

    _clamp = clamp if do_clamp else lambda v: v

    key = source_name.lower()
    if key in _MATERIAL_PROPS:
        mat_idx = poly.material_index
        if mat_idx >= len(obj.material_slots):
            return 0.0
        mat = obj.material_slots[mat_idx].material
        if not mat or not mat.use_nodes:
            return 0.0
        principled = mat.node_tree.nodes.get("Principled BSDF")
        if not principled:
            return 0.0
        if key == "roughness":
            return _clamp(principled.inputs["Roughness"].default_value)
        elif key == "metallic":
            return _clamp(principled.inputs["Metallic"].default_value)
        elif key == "emission":
            return _clamp(principled.inputs["Emission Strength"].default_value)

    attr = mesh.attributes.get(source_name)
    if attr and attr.data_type == 'FLOAT':
        if attr.domain == 'FACE':
            return _clamp(attr.data[poly.index].value)
        elif attr.domain == 'POINT':
            verts = [mesh.loops[li].vertex_index for li in poly.loop_indices]
            return _clamp(sum(attr.data[vi].value for vi in verts) / len(verts))
    return 0.0


# ---------------------------------------------------------------------------
# UV2 Encoding
# ---------------------------------------------------------------------------

def encode_uv2(obj, props, ignore_name=""):
    """Write uv2 based on the selected encoding mode. Returns loop count."""
    if props.uv2_mode == 'NONE':
        return 0

    mesh = obj.data
    if "uv2" not in mesh.uv_layers:
        mesh.uv_layers.new(name="uv2")
    uv2 = mesh.uv_layers["uv2"]
    count = 0

    for poly in mesh.polygons:
        # Skip ignored material
        if ignore_name:
            mat_idx = poly.material_index
            if mat_idx < len(obj.material_slots):
                mat = obj.material_slots[mat_idx].material
                if mat and mat.name == ignore_name:
                    continue

        if props.uv2_mode == 'SIMPLE':
            u = resolve_source_value(mesh, poly, obj, props.uv2_source_u)
            v = resolve_source_value(mesh, poly, obj, props.uv2_source_v)
            for li in poly.loop_indices:
                uv2.data[li].uv = (u, v)
                count += 1

        elif props.uv2_mode == 'GRID':
            outer_x = resolve_source_value(mesh, poly, obj, props.uv2_source_u, do_clamp=False)
            outer_y = resolve_source_value(mesh, poly, obj, props.uv2_source_v, do_clamp=False)
            inner_x = resolve_source_value(mesh, poly, obj, props.uv2_source_inner_x)
            inner_y = resolve_source_value(mesh, poly, obj, props.uv2_source_inner_y)
            grid_uv = map_four_values_to_grid(32, outer_x, outer_y, inner_x, inner_y)
            for li in poly.loop_indices:
                uv2.data[li].uv = grid_uv
                count += 1

    return count


# ---------------------------------------------------------------------------
# Material Collection
# ---------------------------------------------------------------------------

def get_material_properties(mat):
    """Extract (metallic_bool, [r, g, b], roughness) from a Principled BSDF.

    Returns None if the material has no Principled BSDF.
    """
    if mat is None or not mat.use_nodes:
        return None
    principled = mat.node_tree.nodes.get("Principled BSDF")
    if principled is None:
        return None
    bc = principled.inputs["Base Color"].default_value
    return (
        principled.inputs["Metallic"].default_value >= 0.5,
        [bc[0], bc[1], bc[2]],
        principled.inputs["Roughness"].default_value,
    )


def collect_materials_from_objects(objects, ignore_name=""):
    """Extract Principled BSDF properties from all materials on the given objects.

    Returns (materials, name_to_id):
        materials: dict keyed by property ID:
            {id: {"metallic": bool, "base_color": [r,g,b], "roughness": float, "names": [...]}}
        name_to_id: dict mapping material name -> property ID
    """
    materials = {}
    name_to_id = {}
    seen_names = set()
    for obj in objects:
        if obj.type != 'MESH' or not obj.material_slots:
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in seen_names:
                continue
            if mat.name == ignore_name:
                continue
            props = get_material_properties(mat)
            if props is None:
                continue
            seen_names.add(mat.name)
            metallic, base_color, roughness = props
            prop_id = material_property_id(metallic, base_color, roughness)
            name_to_id[mat.name] = prop_id
            if prop_id in materials:
                if mat.name not in materials[prop_id]["names"]:
                    materials[prop_id]["names"].append(mat.name)
            else:
                materials[prop_id] = {
                    "metallic": metallic,
                    "base_color": base_color,
                    "roughness": roughness,
                    "names": [mat.name],
                }
    return materials, name_to_id


# ---------------------------------------------------------------------------
# Additive Merge
# ---------------------------------------------------------------------------

def load_existing_json(json_path):
    """Load an existing material-pack JSON manifest. Returns dict or None."""
    path = bpy.path.abspath(json_path)
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def merge_material_data(existing_data, new_materials):
    """Merge new materials into existing manifest data (keyed by property ID)."""
    merged = {}
    if existing_data and "materials" in existing_data:
        for mat_id, info in existing_data["materials"].items():
            merged[mat_id] = {
                "metallic": info["metallic"],
                "base_color": info["base_color"],
                "roughness": info["roughness"],
                "names": list(info.get("names", [])),
            }
    for mat_id, info in new_materials.items():
        if mat_id in merged:
            existing_names = merged[mat_id]["names"]
            for n in info.get("names", []):
                if n not in existing_names:
                    existing_names.append(n)
        else:
            merged[mat_id] = {
                "metallic": info["metallic"],
                "base_color": list(info["base_color"]),
                "roughness": info["roughness"],
                "names": list(info.get("names", [])),
            }
    return merged


# ---------------------------------------------------------------------------
# Cell Assignment
# ---------------------------------------------------------------------------

def assign_cells(materials, min_grid_size):
    """Separate materials into metallic/non-metallic, calculate grid sizes,
    assign grid positions.

    Returns (materials_with_pos, grid_size_nm, grid_size_m).
    """
    non_metallic = sorted([mid for mid, m in materials.items() if not m["metallic"]])
    metallic = sorted([mid for mid, m in materials.items() if m["metallic"]])

    grid_nm = calculate_grid_size(max(len(non_metallic), 1), min_grid_size)
    grid_m = calculate_grid_size(max(len(metallic), 1), min_grid_size)

    for i, mat_id in enumerate(non_metallic):
        materials[mat_id]["grid_pos"] = [i % grid_nm, i // grid_nm]

    for i, mat_id in enumerate(metallic):
        materials[mat_id]["grid_pos"] = [i % grid_m, i // grid_m]

    return materials, grid_nm, grid_m


# ---------------------------------------------------------------------------
# Image Generation
# ---------------------------------------------------------------------------

def generate_image(materials, image_width, image_height, grid_nm, grid_m):
    """Create a Blender image with material cells packed into a grid.

    Left half = non-metallic, right half = metallic.
    RGBA = (base_color.r, base_color.g, base_color.b, max(roughness, 0.01))
    Returns the bpy.types.Image.
    """
    img_name = "MaterialPack"
    if img_name in bpy.data.images:
        bpy.data.images.remove(bpy.data.images[img_name])

    image = bpy.data.images.new(img_name, image_width, image_height, alpha=True)
    image.colorspace_settings.name = 'Linear Rec.709'

    pixel_count = image_width * image_height
    pixels = [0.0, 0.0, 0.0, 0.0] * pixel_count

    half_w = image_width // 2
    cell_w_nm = half_w // grid_nm
    cell_h_nm = image_height // grid_nm
    cell_w_m = half_w // grid_m
    cell_h_m = image_height // grid_m

    for name, info in materials.items():
        gp = info.get("grid_pos")
        if gp is None:
            continue
        col, row = gp
        r, g, b = info["base_color"]
        a = max(info["roughness"], 0.01)

        if not info["metallic"]:
            x_start = col * cell_w_nm
            y_start = row * cell_h_nm
            cw, ch = cell_w_nm, cell_h_nm
        else:
            x_start = half_w + col * cell_w_m
            y_start = row * cell_h_m
            cw, ch = cell_w_m, cell_h_m

        for py in range(y_start, y_start + ch):
            for px in range(x_start, x_start + cw):
                idx = (py * image_width + px) * 4
                pixels[idx] = r
                pixels[idx + 1] = g
                pixels[idx + 2] = b
                pixels[idx + 3] = a

    image.pixels[:] = pixels
    image.pack()
    return image


def save_image(image, output_path):
    """Save image to disk as 16-bit RGBA PNG."""
    path = bpy.path.abspath(output_path)
    image.filepath_raw = path
    image.file_format = 'PNG'
    image.save()


# ---------------------------------------------------------------------------
# JSON Output
# ---------------------------------------------------------------------------

def save_manifest(output_path, image_width, image_height, grid_nm, grid_m, materials):
    """Write the material-pack JSON manifest alongside the image."""
    base, _ = os.path.splitext(bpy.path.abspath(output_path))
    json_path = base + ".json"

    data = {
        "image_size": [image_width, image_height],
        "grid_size_non_metallic": grid_nm,
        "grid_size_metallic": grid_m,
        "materials": {},
    }
    for mat_id in sorted(materials.keys()):
        info = materials[mat_id]
        data["materials"][mat_id] = {
            "names": sorted(info.get("names", [])),
            "metallic": info["metallic"],
            "grid_pos": info.get("grid_pos", [0, 0]),
            "base_color": [round(c, 6) for c in info["base_color"]],
            "roughness": round(info["roughness"], 6),
        }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return json_path


# ---------------------------------------------------------------------------
# UV Remapping
# ---------------------------------------------------------------------------

def remap_uvs(obj, materials_data, grid_nm, grid_m, ignore_name=""):
    """Set uv1 on each face to point at the center of its material's cell.

    Faces with the ignore material are skipped.
    Returns face-loop count processed.
    """
    if obj.type != 'MESH' or not obj.material_slots:
        return 0

    mesh = obj.data

    # Ensure uv0 exists
    if "UVMap" in mesh.uv_layers:
        mesh.uv_layers["UVMap"].name = "uv0"
    elif "uv0" not in mesh.uv_layers:
        mesh.uv_layers.new(name="uv0")

    # Ensure uv1 exists
    if "uv1" not in mesh.uv_layers:
        mesh.uv_layers.new(name="uv1")

    uv1 = mesh.uv_layers["uv1"]
    face_loops = 0

    for poly in mesh.polygons:
        mat_index = poly.material_index
        if mat_index >= len(obj.material_slots):
            continue
        mat = obj.material_slots[mat_index].material
        if mat is None:
            continue
        if mat.name == ignore_name:
            continue
        props = get_material_properties(mat)
        if props is None:
            continue
        prop_id = material_property_id(*props)
        if prop_id not in materials_data:
            continue

        info = materials_data[prop_id]
        gp = info.get("grid_pos")
        if gp is None:
            continue

        col, row = gp
        if not info["metallic"]:
            u = (col + 0.5) / grid_nm * 0.5
            v = (row + 0.5) / grid_nm
        else:
            u = 0.5 + (col + 0.5) / grid_m * 0.5
            v = (row + 0.5) / grid_m

        for loop_index in poly.loop_indices:
            uv1.data[loop_index].uv = (u, v)
            face_loops += 1

    return face_loops


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _run_cleanup(context, obj, props):
    """Run mesh cleanup operations on *obj* (must be in Object mode)."""
    override = dict(active_object=obj, object=obj, selected_objects=[obj])

    ignore_mat = props.ignore_material

    # Remove faces with ignored material (before other cleanup so
    # delete-loose can clean up the leftover verts/edges)
    if props.remove_ignored_faces and ignore_mat is not None:
        ignore_slot = None
        for i, slot in enumerate(obj.material_slots):
            if slot.material is not None and slot.material.name == ignore_mat.name:
                ignore_slot = i
                break

        if ignore_slot is not None:
            with context.temp_override(**override):
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='FACE')
                bpy.ops.mesh.select_all(action='DESELECT')

                bm = bmesh.from_edit_mesh(obj.data)
                for f in bm.faces:
                    if f.material_index == ignore_slot:
                        f.select = True
                bmesh.update_edit_mesh(obj.data)

                bpy.ops.mesh.delete(type='ONLY_FACE')
                bpy.ops.object.mode_set(mode='OBJECT')

    # Pre-modifier cleanup: delete loose
    if props.delete_loose:
        with context.temp_override(**override):
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)
            bpy.ops.object.mode_set(mode='OBJECT')

    if props.set_sharpness_by_angle:
        with context.temp_override(**override):
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.set_sharpness_by_angle(angle=0.523599)
            bpy.ops.object.mode_set(mode='OBJECT')

    # Post-modifier cleanup
    post_cleanup = (props.merge_by_distance or props.set_sharpness_by_angle
                    or props.mark_sharp_as_seams
                    or props.limited_dissolve or props.delete_loose)
    if not post_cleanup:
        # Still need to handle ignore material removal
        if props.remove_ignored_faces and ignore_mat is not None:
            _remove_ignore_material_slot(obj, ignore_mat)
        return

    with context.temp_override(**override):
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='VERT')
        bpy.ops.mesh.select_all(action='SELECT')

        if props.delete_loose:
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.delete_loose(use_verts=True, use_edges=True, use_faces=False)

        if props.merge_by_distance:
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(
                threshold=0.0001,
                use_unselected=True,
                use_sharp_edge_from_normals=True,
            )

        if props.set_sharpness_by_angle:
            bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='EDGE')
            bpy.ops.mesh.set_sharpness_by_angle(angle=0.523599)

        if props.mark_sharp_as_seams:
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='EDGE')
            bpy.ops.mesh.edges_select_sharp()
            bpy.ops.mesh.mark_seam(clear=False)
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='VERT')

        if props.limited_dissolve:
            bm = bmesh.from_edit_mesh(obj.data)

            for mat_idx in range(len(obj.material_slots)):
                for f in bm.faces:
                    f.select = False
                for e in bm.edges:
                    e.select = False
                for v in bm.verts:
                    v.select = False

                mat_faces = [f for f in bm.faces if f.material_index == mat_idx]
                if not mat_faces:
                    continue
                for f in mat_faces:
                    f.select = True

                mat_verts = list({v for f in mat_faces for v in f.verts})
                mat_edges = list({e for f in mat_faces for e in f.edges})

                bmesh.ops.dissolve_limit(
                    bm,
                    angle_limit=math.radians(5),
                    use_dissolve_boundaries=False,
                    verts=mat_verts,
                    edges=mat_edges,
                    delimit={'NORMAL', 'SEAM', 'SHARP', 'UV', 'MATERIAL'},
                )

            bmesh.update_edit_mesh(obj.data)

        bpy.ops.object.mode_set(mode='OBJECT')

    # After all cleanup, remove the ignore material slot if its faces were deleted
    if props.remove_ignored_faces and ignore_mat is not None:
        _remove_ignore_material_slot(obj, ignore_mat)


def _remove_ignore_material_slot(obj, ignore_mat):
    """Remove the ignore material's slot from the object, leaving only the target."""
    for i, slot in enumerate(obj.material_slots):
        if slot.material is not None and slot.material.name == ignore_mat.name:
            obj.active_material_index = i
            with bpy.context.temp_override(object=obj):
                bpy.ops.object.material_slot_remove()
            break


# ---------------------------------------------------------------------------
# Object Processing
# ---------------------------------------------------------------------------

def _get_or_create_output_collection(context, col_name):
    """Get or create the output collection by name."""
    if col_name in bpy.data.collections:
        return bpy.data.collections[col_name]
    col = bpy.data.collections.new(col_name)
    context.scene.collection.children.link(col)
    return col


def _reassign_materials(obj, ignore_mat, target_mat):
    """Replace all material slots with just ignore_mat and/or target_mat,
    reassigning face material indices accordingly.

    Faces that had ignore_mat keep it; all others get target_mat.
    """
    mesh = obj.data

    # Build set of face indices that have the ignore material
    ignore_faces = set()
    if ignore_mat is not None:
        for poly in mesh.polygons:
            idx = poly.material_index
            if idx < len(obj.material_slots):
                mat = obj.material_slots[idx].material
                if mat is not None and mat.name == ignore_mat.name:
                    ignore_faces.add(poly.index)

    # Clear all material slots
    mesh.materials.clear()

    # Re-add materials and build slot index map
    has_ignore = ignore_mat is not None
    has_target = target_mat is not None

    if has_target:
        mesh.materials.append(target_mat)
        target_slot = 0
    if has_ignore:
        mesh.materials.append(ignore_mat)
        ignore_slot = 1 if has_target else 0

    # Reassign face material indices
    for poly in mesh.polygons:
        if poly.index in ignore_faces and has_ignore:
            poly.material_index = ignore_slot
        elif has_target:
            poly.material_index = target_slot
        else:
            poly.material_index = 0


def process_single_object(context, props, source, materials_data, grid_nm, grid_m):
    """Duplicate source, apply cleanup, remap UVs, manage materials.

    Returns (duplicate, face_loop_count) or (None, 0).
    """
    if not source.material_slots:
        return None, 0

    ignore_mat = props.ignore_material
    ignore_name = ignore_mat.name if ignore_mat else ""

    output_name = source.name + props.suffix

    # Overwrite existing
    if props.overwrite_existing and output_name in bpy.data.objects:
        old_obj = bpy.data.objects[output_name]
        bpy.data.objects.remove(old_obj, do_unlink=True)

    # Duplicate with deep mesh copy
    duplicate = source.copy()
    duplicate.data = source.data.copy()
    duplicate.name = output_name
    duplicate.data.name = output_name

    # Link to output collection
    col_name = props.output_collection.strip()
    col = _get_or_create_output_collection(context, col_name)
    col.objects.link(duplicate)

    # Transfer viewport focus to duplicate
    source.select_set(False)
    context.view_layer.objects.active = duplicate
    duplicate.select_set(True)

    # Apply modifiers
    if props.apply_modifiers and duplicate.modifiers:
        with context.temp_override(
            active_object=duplicate, object=duplicate, selected_objects=[duplicate]
        ):
            for mod in list(duplicate.modifiers):
                try:
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                except RuntimeError:
                    pass

    # Apply transform
    if props.apply_transform:
        with context.temp_override(
            active_object=duplicate, object=duplicate, selected_objects=[duplicate]
        ):
            bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    # Cleanup (before UV remapping so merge-by-distance etc. happen first)
    _run_cleanup(context, duplicate, props)

    # Remap UVs (must happen while materials still exist)
    face_loops = remap_uvs(duplicate, materials_data, grid_nm, grid_m, ignore_name)

    # Encode uv2 (attribute encoding)
    encode_uv2(duplicate, props, ignore_name)

    # Material management
    if props.delete_materials:
        _reassign_materials(duplicate, ignore_mat, props.target_material)
    elif props.target_material is not None:
        _reassign_materials(duplicate, ignore_mat, props.target_material)

    duplicate.select_set(False)
    return duplicate, face_loops


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class MaterialPackProperties(PropertyGroup):
    # Image settings
    image_width: IntProperty(
        name="Image Width",
        description="Width of the output texture atlas in pixels",
        default=512,
        min=64,
        max=4096,
    )
    image_height: IntProperty(
        name="Image Height",
        description="Height of the output texture atlas in pixels",
        default=512,
        min=64,
        max=4096,
    )
    min_grid_size: IntProperty(
        name="Min Grid Size",
        description="Minimum grid subdivisions per half (will round up to power of two)",
        default=2,
        min=1,
        max=64,
    )

    # Paths
    output_path: StringProperty(
        name="Output Path",
        description="File path for the output PNG image (JSON will be saved alongside)",
        subtype='FILE_PATH',
        default="//material_pack.png",
    )
    json_path: StringProperty(
        name="Load JSON",
        description="Path to an existing manifest JSON to merge with (additive mode). Leave empty to start fresh",
        subtype='FILE_PATH',
        default="",
    )

    # Processing
    output_collection: StringProperty(
        name="Output Collection",
        description="Name of the collection to place processed objects in",
        default="Output",
    )
    suffix: StringProperty(
        name="Suffix",
        description="Suffix appended to duplicate object names",
        default="_PACK",
    )
    overwrite_existing: BoolProperty(
        name="Overwrite Existing",
        description="Delete existing object with the same output name before creating a new one",
        default=True,
    )
    ignore_hidden: BoolProperty(
        name="Ignore Hidden",
        description="Skip objects that are hidden in the viewport",
        default=True,
    )
    apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="Apply all modifiers on the duplicate",
        default=True,
    )
    apply_transform: BoolProperty(
        name="Apply Transform",
        description="Apply location, rotation, and scale on the duplicate",
        default=True,
    )
    delete_materials: BoolProperty(
        name="Delete Materials",
        description="Remove all materials from the duplicate after UV encoding",
        default=True,
    )
    ignore_material: PointerProperty(
        type=bpy.types.Material,
        name="Ignore Material",
        description="Material to exclude from processing. Faces with this material are left untouched",
    )
    target_material: PointerProperty(
        type=bpy.types.Material,
        name="Target Material",
        description="Material to assign to the duplicate after processing",
    )
    input_collection: StringProperty(
        name="Input Collection",
        description="Name of the collection containing source objects to process",
        default="",
    )
    merge_result: BoolProperty(
        name="Merge Result",
        description="Join all processed objects into a single mesh after processing",
        default=True,
    )

    # Cleanup
    delete_loose: BoolProperty(
        name="Delete Loose",
        description="Remove loose vertices and edges",
        default=True,
    )
    merge_by_distance: BoolProperty(
        name="Merge by Distance",
        description="Merge overlapping vertices after applying modifiers",
        default=True,
    )
    set_sharpness_by_angle: BoolProperty(
        name="Set Sharpness by Angle",
        description="Auto-mark sharp edges by angle",
        default=True,
    )
    mark_sharp_as_seams: BoolProperty(
        name="Mark Sharp as Seams",
        description="Convert sharp edges to UV seams",
        default=True,
    )
    remove_ignored_faces: BoolProperty(
        name="Remove Ignored Faces",
        description="Delete faces with the ignored material after merging, then remove the material slot",
        default=False,
    )
    limited_dissolve: BoolProperty(
        name="Limited Dissolve",
        description="Dissolve flat geometry per material group",
        default=False,
    )

    # UV2 Encoding
    uv2_mode: EnumProperty(
        name="UV2 Mode",
        items=[
            ('NONE', "None", "Don't write uv2"),
            ('SIMPLE', "Simple", "2 attributes written directly as U, V"),
            ('GRID', "Grid", "4 attributes encoded as nested grid UV (RGBA texture lookup)"),
        ],
        default='NONE',
    )
    uv2_source_u: StringProperty(
        name="U Source / Outer X",
        description="Float attribute name or material property (roughness/metallic/emission) for U axis / outer grid X",
        default="",
    )
    uv2_source_v: StringProperty(
        name="V Source / Outer Y",
        description="Float attribute name or material property (roughness/metallic/emission) for V axis / outer grid Y",
        default="",
    )
    uv2_source_inner_x: StringProperty(
        name="Inner X",
        description="Float attribute name or material property for inner grid X axis",
        default="",
    )
    uv2_source_inner_y: StringProperty(
        name="Inner Y",
        description="Float attribute name or material property for inner grid Y axis",
        default="",
    )


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class MATERIALPACK_OT_generate_image(Operator):
    bl_idname = "materialpack.generate_image"
    bl_label = "Generate Image"
    bl_description = (
        "Collect materials from the input collection (or active object), "
        "generate the texture atlas PNG and JSON manifest"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.material_pack
        col_name = props.input_collection.strip()
        has_collection = col_name and col_name in bpy.data.collections
        has_active = context.active_object is not None and context.active_object.type == 'MESH'
        return has_collection or has_active

    def execute(self, context):
        props = context.scene.material_pack

        if not props.output_path.strip():
            self.report({'ERROR'}, "Output path cannot be empty")
            return {'CANCELLED'}

        # Gather source objects
        col_name = props.input_collection.strip()
        if col_name and col_name in bpy.data.collections:
            objects = [o for o in bpy.data.collections[col_name].objects if o.type == 'MESH']
        elif context.active_object and context.active_object.type == 'MESH':
            objects = [context.active_object]
        else:
            self.report({'WARNING'}, "No mesh objects to collect materials from")
            return {'CANCELLED'}

        # Collect materials (excluding ignored material)
        ignore_name = props.ignore_material.name if props.ignore_material else ""
        new_materials, _name_to_id = collect_materials_from_objects(objects, ignore_name)
        if not new_materials:
            self.report({'WARNING'}, "No Principled BSDF materials found")
            return {'CANCELLED'}

        # Additive merge
        existing_data = load_existing_json(props.json_path) if props.json_path.strip() else None
        materials = merge_material_data(existing_data, new_materials)

        # Assign cells
        materials, grid_nm, grid_m = assign_cells(materials, props.min_grid_size)

        # Generate image
        image = generate_image(
            materials, props.image_width, props.image_height, grid_nm, grid_m
        )
        save_image(image, props.output_path)

        # Save JSON
        json_out = save_manifest(
            props.output_path, props.image_width, props.image_height,
            grid_nm, grid_m, materials,
        )

        # Auto-populate Load JSON with the newly created manifest
        props.json_path = json_out

        self.report(
            {'INFO'},
            f"Material Pack: {len(materials)} materials → "
            f"{grid_nm}x{grid_nm} / {grid_m}x{grid_m} grid. "
            f"Saved to {bpy.path.abspath(props.output_path)}",
        )
        return {'FINISHED'}


class MATERIALPACK_OT_process_object(Operator):
    bl_idname = "materialpack.process_object"
    bl_label = "Process Object"
    bl_description = "Duplicate the active object and remap UVs to the material atlas"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        props = context.scene.material_pack

        # Load manifest
        manifest = self._load_manifest(props)
        if manifest is None:
            self.report({'ERROR'}, "No manifest found. Generate the image first")
            return {'CANCELLED'}

        materials_data = manifest["materials"]
        grid_nm = manifest["grid_size_non_metallic"]
        grid_m = manifest["grid_size_metallic"]

        source = context.active_object
        if not source.material_slots:
            self.report({'WARNING'}, "Source object has no material slots")
            return {'CANCELLED'}

        if not props.output_collection.strip():
            self.report({'ERROR'}, "Output collection name cannot be empty")
            return {'CANCELLED'}

        if props.overwrite_existing and not props.suffix.strip():
            out_col_name = props.output_collection.strip()
            if out_col_name in bpy.data.collections:
                if source.name in bpy.data.collections[out_col_name].objects:
                    self.report({'ERROR'}, "Overwrite with blank suffix would delete the source object. Add a suffix or change the output collection")
                    return {'CANCELLED'}

        duplicate, face_loops = process_single_object(
            context, props, source, materials_data, grid_nm, grid_m
        )
        if duplicate is None:
            self.report({'WARNING'}, "Processing failed")
            return {'CANCELLED'}

        context.view_layer.objects.active = source
        source.select_set(True)

        self.report({'INFO'}, f"Material Pack: processed {face_loops} face-loops on '{duplicate.name}'")
        return {'FINISHED'}

    def _load_manifest(self, props):
        """Try to load the manifest JSON from the output path."""
        base, _ = os.path.splitext(bpy.path.abspath(props.output_path))
        json_path = base + ".json"
        if os.path.isfile(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None


class MATERIALPACK_OT_process_collection(Operator):
    bl_idname = "materialpack.process_collection"
    bl_label = "Process Collection"
    bl_description = "Process all mesh objects in the input collection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.material_pack
        col_name = props.input_collection.strip()
        if not col_name or col_name not in bpy.data.collections:
            return False
        return any(o.type == 'MESH' for o in bpy.data.collections[col_name].objects)

    def execute(self, context):
        props = context.scene.material_pack

        # Load manifest
        manifest = self._load_manifest(props)
        if manifest is None:
            self.report({'ERROR'}, "No manifest found. Generate the image first")
            return {'CANCELLED'}

        materials_data = manifest["materials"]
        grid_nm = manifest["grid_size_non_metallic"]
        grid_m = manifest["grid_size_metallic"]

        col_name = props.input_collection.strip()
        if not col_name or col_name not in bpy.data.collections:
            self.report({'ERROR'}, f"Input collection '{col_name}' not found")
            return {'CANCELLED'}

        if not props.output_collection.strip():
            self.report({'ERROR'}, "Output collection name cannot be empty")
            return {'CANCELLED'}

        if props.overwrite_existing and not props.suffix.strip() and props.output_collection.strip() == col_name:
            self.report({'ERROR'}, "Overwrite with blank suffix and same input/output collection would delete source objects. Add a suffix or change the output collection")
            return {'CANCELLED'}

        input_col = bpy.data.collections[col_name]
        mesh_objects = [o for o in input_col.objects if o.type == 'MESH']
        if props.ignore_hidden:
            mesh_objects = [o for o in mesh_objects if not o.hide_get()]

        if not mesh_objects:
            self.report({'WARNING'}, "No mesh objects in input collection")
            return {'CANCELLED'}

        total_face_loops = 0
        duplicates = []

        for source in mesh_objects:
            duplicate, face_loops = process_single_object(
                context, props, source, materials_data, grid_nm, grid_m
            )
            if duplicate is not None:
                duplicates.append(duplicate)
                total_face_loops += face_loops

        if not duplicates:
            self.report({'WARNING'}, "No objects were processed")
            return {'CANCELLED'}

        # Overwrite existing merged output
        if props.merge_result and props.overwrite_existing:
            merged_name = col_name + props.suffix
            if merged_name in bpy.data.objects:
                old_obj = bpy.data.objects[merged_name]
                bpy.data.objects.remove(old_obj, do_unlink=True)

        # Merge if requested
        if props.merge_result and len(duplicates) > 1:
            for obj in context.view_layer.objects:
                obj.select_set(False)
            for dup in duplicates:
                dup.select_set(True)
            context.view_layer.objects.active = duplicates[0]

            with context.temp_override(
                active_object=duplicates[0],
                object=duplicates[0],
                selected_objects=duplicates,
                selected_editable_objects=duplicates,
            ):
                bpy.ops.object.join()

            merged = duplicates[0]
            merged.name = col_name + props.suffix
            merged.data.name = col_name + props.suffix

            # Run cleanup again on the merged result
            _run_cleanup(context, merged, props)

            merged.select_set(False)

        for obj in context.view_layer.objects:
            obj.select_set(False)

        self.report(
            {'INFO'},
            f"Material Pack: processed {total_face_loops} face-loops "
            f"across {len(mesh_objects)} objects",
        )
        return {'FINISHED'}

    def _load_manifest(self, props):
        """Try to load the manifest JSON from the output path."""
        base, _ = os.path.splitext(bpy.path.abspath(props.output_path))
        json_path = base + ".json"
        if os.path.isfile(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------

def _get_json_material_counts(props):
    """Read the loaded JSON and return (total, non_metallic, metallic) counts."""
    json_path = props.json_path.strip()
    if not json_path:
        return None
    data = load_existing_json(json_path)
    if data is None or "materials" not in data:
        return None
    mats = data["materials"]
    nm = sum(1 for m in mats.values() if not m.get("metallic", False))
    mt = sum(1 for m in mats.values() if m.get("metallic", False))
    return (nm + mt, nm, mt)


# ---------------------------------------------------------------------------
# Panels (collapsible sub-panels)
# ---------------------------------------------------------------------------

class MATERIALPACK_PT_main(Panel):
    bl_label = "Material Pack"
    bl_idname = "MATERIALPACK_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MatPack"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj and obj.type == 'MESH':
            layout.label(text=f"Active Object: {obj.name}")
        else:
            layout.label(text="Active Object: None")


class MATERIALPACK_PT_input(Panel):
    bl_label = "Input"
    bl_idname = "MATERIALPACK_PT_input"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MatPack"
    bl_parent_id = "MATERIALPACK_PT_main"
    bl_options = {'HIDE_HEADER'}

    def draw(self, context):
        layout = self.layout
        props = context.scene.material_pack
        layout.prop_search(props, "input_collection", bpy.data, "collections")


class MATERIALPACK_PT_image(Panel):
    bl_label = "Image Settings"
    bl_idname = "MATERIALPACK_PT_image"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MatPack"
    bl_parent_id = "MATERIALPACK_PT_main"
    bl_options = set()

    def draw(self, context):
        layout = self.layout
        props = context.scene.material_pack

        layout.prop(props, "image_width")
        layout.prop(props, "image_height")
        layout.prop(props, "min_grid_size")
        grid_k = calculate_grid_size(1, props.min_grid_size)
        per_half = grid_k * grid_k
        layout.separator(type="LINE")
        layout.label(text=f" {per_half * 2} materials ({per_half} metallic)")


class MATERIALPACK_PT_texgen(Panel):
    bl_label = "Texture Generation"
    bl_idname = "MATERIALPACK_PT_texgen"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MatPack"
    bl_parent_id = "MATERIALPACK_PT_main"
    bl_options = set()

    def draw(self, context):
        layout = self.layout
        props = context.scene.material_pack

        layout.prop(props, "output_path")
        layout.prop(props, "json_path")

        # Show material counts from loaded JSON
        counts = _get_json_material_counts(props)
        if counts is not None:
            total, nm, mt = counts
            layout.label(text=f"{total} Materials: {nm} non-metallic, {mt} metallic")

        layout.operator("materialpack.generate_image", icon='IMAGE_DATA')


class MATERIALPACK_PT_processing(Panel):
    bl_label = "Processing"
    bl_idname = "MATERIALPACK_PT_processing"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MatPack"
    bl_parent_id = "MATERIALPACK_PT_main"
    bl_options = set()

    def draw(self, context):
        layout = self.layout
        props = context.scene.material_pack

        layout.prop(props, "output_collection")
        layout.prop(props, "suffix")
        layout.prop(props, "overwrite_existing")
        layout.prop(props, "ignore_hidden")
        layout.prop(props, "apply_modifiers")
        layout.prop(props, "apply_transform")
        layout.prop(props, "delete_materials")
        layout.prop(props, "ignore_material")
        layout.prop(props, "target_material")
        layout.prop(props, "merge_result")


class MATERIALPACK_PT_cleanup(Panel):
    bl_label = "Cleanup"
    bl_idname = "MATERIALPACK_PT_cleanup"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MatPack"
    bl_parent_id = "MATERIALPACK_PT_main"
    bl_options = set()

    def draw(self, context):
        layout = self.layout
        props = context.scene.material_pack

        layout.prop(props, "delete_loose")
        layout.prop(props, "merge_by_distance")
        layout.prop(props, "set_sharpness_by_angle")
        layout.prop(props, "mark_sharp_as_seams")
        layout.prop(props, "remove_ignored_faces")
        layout.prop(props, "limited_dissolve")


class MATERIALPACK_PT_uv2(Panel):
    bl_label = "Attribute UV Encoding"
    bl_idname = "MATERIALPACK_PT_uv2"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MatPack"
    bl_parent_id = "MATERIALPACK_PT_main"
    bl_options = set()

    def draw(self, context):
        layout = self.layout
        props = context.scene.material_pack

        layout.prop(props, "uv2_mode")

        if props.uv2_mode == 'SIMPLE':
            layout.prop(props, "uv2_source_u", text="U Source")
            layout.prop(props, "uv2_source_v", text="V Source")
            layout.label(text="Attribute name, or: roughness / metallic / emission", icon='INFO')

        elif props.uv2_mode == 'GRID':
            col = layout.column(align=True)
            col.label(text="Outer Grid (texture R, G)")
            col.prop(props, "uv2_source_u", text="X Axis")
            col.prop(props, "uv2_source_v", text="Y Axis")
            col.separator()
            col.label(text="Inner Grid (texture B, A)")
            col.prop(props, "uv2_source_inner_x", text="X Axis")
            col.prop(props, "uv2_source_inner_y", text="Y Axis")
            layout.label(text="Attribute name, or: roughness / metallic / emission", icon='INFO')


class MATERIALPACK_PT_actions(Panel):
    bl_label = "Process"
    bl_idname = "MATERIALPACK_PT_actions"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MatPack"
    bl_parent_id = "MATERIALPACK_PT_main"
    bl_options = {'HIDE_HEADER'}

    def draw(self, context):
        layout = self.layout
        layout.operator("materialpack.process_collection", icon='PLAY')
        layout.operator("materialpack.process_object", icon='PLAY')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    MaterialPackProperties,
    MATERIALPACK_OT_generate_image,
    MATERIALPACK_OT_process_object,
    MATERIALPACK_OT_process_collection,
    MATERIALPACK_PT_main,
    MATERIALPACK_PT_input,
    MATERIALPACK_PT_image,
    MATERIALPACK_PT_texgen,
    MATERIALPACK_PT_processing,
    MATERIALPACK_PT_cleanup,
    MATERIALPACK_PT_uv2,
    MATERIALPACK_PT_actions,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.material_pack = PointerProperty(type=MaterialPackProperties)


def unregister():
    del bpy.types.Scene.material_pack
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
