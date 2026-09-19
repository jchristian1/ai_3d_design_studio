# `studio_materials` — the material library Astra designs with

A design in flat colours looks like a diagram. The same design in oak, brick and
plaster looks like a building. This package is what makes the second possible.

```
catalogue.py   WHAT exists: names, palettes, real-world tile size, usage notes
synthesize.py  HOW the maps are drawn, procedurally, with no shipped assets
library.py     WHERE the PNGs are cached, and the paths Blender needs
```

Seventeen materials, each with three maps (`base_color`, `roughness`, `normal`).

## Why a fixed catalogue when Astra writes its own code

Astra authors Blender Python directly, so nothing here limits what it can build.
The catalogue solves a narrower problem: **a material has to survive the trip to the
browser.**

The still preview renders in Blender, but the interactive viewer renders an exported
GLB, and glTF can only carry image textures sampled through a UV map. A procedural
node graph — Musgrave noise into a bump, a Voronoi brick pattern — renders
beautifully in EEVEE and arrives in the browser as **flat grey**. That asymmetry is
invisible while authoring and infuriating afterwards: the picture looks right and the
3D view looks broken.

So the library is real image textures, and one platform-owned helper wires them up
correctly every time.

## Why the textures are generated, not vendored

| | cost |
|---|---|
| third-party texture pack | licence terms to honour and propagate, megabytes of binaries in git |
| **generated from code** | a few hundred reviewable lines; output is deterministic; no licence question |

Everything is drawn with Pillow's whole-image C operations — `effect_noise`,
`offset`, `subtract`, `resize`, `point`, `composite`. There is not one per-pixel
Python loop, which is why seventeen materials take about eight seconds rather than
minutes. The output is cached in `runtime/textures/` (gitignored), keyed by a
fingerprint of the catalogue, so an unchanged catalogue is a directory listing and a
changed one regenerates without anyone remembering to.

Stated plainly: these are *convincing* architectural materials, not photographic
scans. They read correctly at walkthrough distance. A hero render wanting real
scanned oak needs a proper asset pipeline, and that is future work.

## Real-world scale is part of the material

`tile_meters` is the load-bearing field. A texture is a square image; what makes it
read as brick rather than wallpaper is how much of the WORLD it covers. Baking that
into the catalogue means Astra never reasons about UV scale, and a wall is never clad
in three-metre bricks.

The brick entry is 16 courses by 5 bricks across 1.2 m — a 0.24 x 0.075 m brick,
within a few millimetres of a real one including its mortar joint.

## How it reaches Blender, and why Astra never sees a path

Astra's code is risk-classified before it runs, and **an absolute path literal forces
a user approval prompt**. Code containing `bpy.data.images.load("/…/oak.png")` would
interrupt the user for every textured material, which would make texturing unusable.

So paths are resolved on the host and injected into the Blender runtime as a helper,
in `blender_worker.backends.official.materials`. Astra writes only a name:

```python
studio_material(floor, "oak_floor")
studio_material([wall_a, wall_b], "red_brick")
studio_materials_available()
```

The helper loads the three maps, sets the colour spaces (`base_color` is sRGB;
roughness and normal are `Non-Color` data — get this wrong and gloss and bumps are
both wrong), builds a Principled BSDF, and box-projects real UVs at the material's
true scale.

Classification runs on Astra's raw code *before* the platform wraps it, so paths in
the wrapper are never inspected. Note also that Blender's bundled Python has no
Pillow: generation can only happen on the host, which is exactly where it happens.

## What is verified, in real Blender

- UV spans are real-world exact: a 6 m floor with a 2.4 m oak tile spans 2.500
- colour spaces correct on all three maps
- the exported GLB carries `baseColorTexture`, `metallicRoughnessTexture` and
  `normalTexture` for every material, all at `texCoord=0` (one UV set, not two)
- `studio_object_id` and `studio_material` survive into the GLB, so click-to-select
  and "what is this clad in" both still work

## Two bugs worth remembering

Both were found by looking at the output rather than by a failing test, which is the
argument for rendering a contact sheet when you change this code.

1. **Bricks, tiles and roofs had no pattern at all.** Joints were drawn at mid-grey,
   which landed inside the range of the faces' own autocontrast noise and vanished.
   Fixed by compressing faces into an upper band so joints are unambiguously darker.
2. **The fabric was split corner to corner by a hard diagonal.** Drawing each warp
   thread and then immediately its weft meant the higher index always won at
   crossings. Fixed by laying all warp, then all weft, then restoring warp on
   alternating crossings — which is what weaving actually is.
