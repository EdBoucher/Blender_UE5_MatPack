# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

MatPack is a set of Blender 4.0+ addons for a Blender → Unreal Engine 5 pipeline. The core idea: instead of syncing many materials between tools, pack all material properties (colour, roughness, metallic) into a single texture atlas, and remap mesh UVs to address it. One UE5 material can then render many surface types.

## Installation (no build step)

The addons are plain Python files installed directly into Blender:  
`Edit → Preferences → Plugins → Install from Disk`

The three addon files are:
- `material_pack_addon.py` — main plugin (atlas generation + UV remapping + attribute encoding)
- `attribute_paint_addon.py` — convenience tool for setting face/edge/vertex attributes in edit mode
- `uv_auto_tile_addon.py` — UV tiling helper (coplanar face tiling, aspect-correct projection)

There is also `image-generators/hsl_grid.html` — a standalone browser tool, unrelated to the addons.

## Architecture: material_pack_addon.py

The addon has two responsibilities:

**1. Atlas generation** (`MATERIALPACK_OT_generate_image`)  
Reads materials from selected objects/collections, hashes each unique combination of `(metallic, base_color, roughness)` into a 12-char hex ID via `material_property_id()`, places them on a grid, and writes a PNG + JSON manifest. Non-metallic materials go on the left half of the atlas (U < 0.5), metallic on the right (U > 0.5). Roughness is packed into the alpha channel.

**2. Model processing** (`MATERIALPACK_OT_process_object` / `MATERIALPACK_OT_process_collection`)  
Duplicates objects, rewrites their UV maps so that `uv1` (second UV slot) addresses the atlas, optionally encodes arbitrary mesh attributes into `uv2` (third UV slot) or vertex colours, and optionally merges/cleans up the result.

### UV slot convention
- `uv0` / `UVMap` — original texture UVs, untouched
- `uv1` — atlas lookup (colour, roughness, metallic)
- `uv2` — custom attribute encoding (emission, semantic flags, etc.)

### uv2 encoding modes
- **Simple**: maps two scalar sources (face attributes or material properties) directly to U and V
- **Grid**: encodes four scalar sources via a nested grid (`map_four_values_to_grid()`), allowing a look-up texture to carry four independent values

Sources can be face/vertex/edge attributes from the mesh, or material properties: `roughness`, `metallic`, `emission`.

Range modes for normalisation: `NONE`, `CLAMP`, `WRAP`, `NORMALIZE` (auto min/max scan).

### JSON manifest (`output/material_pack.json`)
Generated alongside the PNG. Keys are 12-char hex hashes. Fields: `names`, `metallic`, `grid_pos`, `base_color`, `roughness`, `emission_color`, `emission_strength`. Can be loaded back to import materials into a new Blender file.

## Architecture: attribute_paint_addon.py

Provides a sidebar panel (`Attr Paint`) for editing FLOAT/INT/BOOLEAN attributes on selected geometry in edit mode. Three paint modes: fixed value, random, index (sequential or normalised). Syncs the UI list with the mesh's actual attributes via a `depsgraph_update_post` handler.

Works with FACE, EDGE, and POINT domains. Uses bmesh for most operations with an object-mode fallback for attributes not exposed in bmesh (e.g., BOOLEAN via the Python API directly).

## Architecture: uv_auto_tile_addon.py

Groups selected faces into connected components, optionally sub-splits by surface normal angle, then tiles UVs across each group. Supports aspect-correct projection. Separate from the material pipeline.

## Key patterns

- All operators use `bl_options = {'REGISTER', 'UNDO'}`
- `MaterialPackProperties` (registered on `bpy.types.Scene.mat_pack`) holds all main addon settings
- `AttrPaintProperties` (registered on `bpy.types.Scene.attr_paint`) holds attribute paint settings
- Visibility check: use `obj.hide_get()` not `obj.hide_viewport`
- Material identity is hashed, not name-based — two materials with the same BSDF properties collapse to the same atlas cell
