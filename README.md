# MatPack*: A Blender plugin for packing materials into an atlas and encoding mesh attributes into UV maps

MatPack reduces the number of materials you need to sync between Unreal and Blender by creating a material atlas. This is like a texture atlas, but for BSDF properties. There's a blog post about the motivation and approach here - https://jeanpaulsoftware.com/2026/03/29/blender-material-packing/ - but in short / TLDR: this is good because you only need one actual UE5 material to render a lot of different surface types.

There are two parts the the process: 

1. Atlas generation: you give the plugin a model or collection, it reads the materials on the models, extracts the properties, matches them to those in a JSON file, and then produces a texture. 

2. Model processing: copies of the models / collections you give it are created, with UVs changed to match the atlas. It can also merge the collection into a single model, and will optionally perform common clean-up operations on the result.

Colour, and roughness and metallic are placed in one texture, and the second UV map slot of the output model is used to address these.

- The RGB values of the materials' base colour are placed in the RGB
- Roughness is placed in alpha
- Metallic materials are placed on the right of the image, non-metallic are placed on the left. In your shader, you set metallic as the value of `uv1.u > 0.5`.

![How the materials are laid out in the atlas](/readme-images/layout.png)

The third UV slot is used for any other properties you want to encode- emission is the main candidate, but if you've got other semantic information you want to preserve when exporting your model, you can do that too. 

There's two different encoding schemes:

- Simple - this writes the UV coordinates out directly, based on attributes that are on the faces of the mesh, or on the properties of the materials assigned to those faces. 

- Grid - this writes the UV coordinates so that they access an RGBA look-up texture, so you can encode four different values.

For example: say you wanted to render a bunch of objects that had different colours via instanced rendering. If you put a boolean attribute on the faces that change colour, and run it through this plugin, in your shader you can use whether `uv2.x > 0.5` to mask which areas should change colour, and which shouldn't.

![The coloured parts have a 'HasColours' flag written to them in Blender, which is then encoded to the second UV map as a 0 or a 1 on the U axis. This is then used in the UE5 shader graph](/readme-images/colour-change.png)

Writing to vertex colours is also supported, just in case you want to use those.

You can write attributes either using Geometry Nodes (which is the intended usage), but there's a second plugin- Attribute Paint - that makes it a lot easier to just manually select the bits of the mesh you want an attribute on and set a value.

![Just set the value you want and click 'apply' to have it on your selection; set random values for the selection; or write the face index to use as a seed](/readme-images/attribute-paint.png)

## Installation

In Blender, go to edit -> preferences -> plugins, then click the small arrow in the top right and select 'Install from Disk', and select the plugin you want to install. 

There's three plugins:

- `material_pack_addon.py` - this is the main plugin file
- `attribute_paint_addon.py` - this is a convenience tool to make it easier 
- `uv_auto_tile_addon.py` - allows you to repeat a pattern a number of times over a face or faces. Isn't actually related but it's in the repo now so whatever

## Demos

There's a Blender file with an example Geometry Nodes setup, and an Unreal Engine 5 project with a couple of PCG graphs and Unreal materials that should give you some idea how to use this. I will be writing proper documentation at some point.

*if you can think of a better name then I'm all ears