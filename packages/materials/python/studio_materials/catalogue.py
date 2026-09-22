"""The studio's material catalogue — the vocabulary Astra designs with.

WHY A FIXED CATALOGUE AT ALL

Astra authors its own Blender code, so nothing here restricts what it can build.
What this catalogue solves is a narrower problem: a material has to survive the
trip to the browser.

The interactive viewer renders a GLB, and glTF can only carry what it has a
representation for — image textures and scalar PBR inputs. A procedural Blender
node graph (Musgrave noise into a bump, a Voronoi brick pattern) renders
beautifully in the EEVEE preview and arrives in the browser as **flat grey**,
because the exporter has nothing to write it to. That asymmetry is invisible
while authoring and infuriating afterwards: the still image looks right and the
3D view looks broken.

So the catalogue is a set of real IMAGE textures. Astra names one, the platform
wires up a correct Principled BSDF, and the result looks the same in the preview
and in the browser.

WHY THE TEXTURES ARE SYNTHESISED, NOT SHIPPED

No third-party texture packs are vendored: they carry licence terms the project
would have to honour and propagate, and they would add megabytes of binaries to
git. Everything here is generated procedurally from code (see
:mod:`studio_materials.synthesize`) into a gitignored cache, which means the
committed source of truth is a few hundred lines of Python, the output is
deterministic, and the licence question never arises.

The trade-off, stated plainly: these are *convincing* architectural materials,
not photographic scans. They read correctly at preview and walkthrough distance.
A hero render that needs real scanned oak wants a proper asset pipeline, and that
is future work rather than something this module pretends to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Optional


@dataclass(frozen=True)
class MaterialSpec:
    """One catalogue entry.

    ``tile_meters`` is the load-bearing field for realism. A texture is a square
    image; what makes it read as brick rather than as wallpaper is how much of the
    WORLD it covers. Baking that into the catalogue means Astra does not have to
    reason about UV scale, and a wall is not accidentally clad in three-metre
    bricks.
    """

    name: str
    label: str
    #: Which synthesiser draws it. See :mod:`studio_materials.synthesize`.
    pattern: str
    #: Dominant colours, linear-ish sRGB 0-255. The synthesiser interpolates
    #: between them to produce variation.
    palette: tuple[tuple[int, int, int], ...]
    #: Mid roughness, 0 = mirror, 1 = fully diffuse.
    roughness: float
    #: How much the roughness map varies around that value.
    roughness_variation: float = 0.12
    #: 1.0 only for genuine metals: it changes how light behaves, not just colour.
    metallic: float = 0.0
    #: Real-world size of one texture repeat, in metres.
    tile_meters: float = 1.0
    #: Normal-map intensity. Surface relief, not displacement.
    bump: float = 1.0
    #: What this is for, in the words the model should think in.
    use_for: str = ""


#: Chosen for architectural coverage rather than variety for its own sake: every
#: entry answers "what is this surface actually made of" for a common building
#: element. Fourteen is enough to clad a house inside and out without becoming a
#: list the model has to search.
MATERIALS: Final[tuple[MaterialSpec, ...]] = (
    MaterialSpec(
        name="oak_floor",
        label="Oak floorboards",
        pattern="planks",
        palette=((150, 105, 62), (178, 132, 84), (120, 82, 48)),
        roughness=0.45,
        tile_meters=2.4,
        bump=0.7,
        use_for="warm interior floors, decking",
    ),
    MaterialSpec(
        name="pine_floor",
        label="Pale pine floorboards",
        pattern="planks",
        palette=((206, 172, 124), (224, 196, 154), (180, 146, 100)),
        roughness=0.5,
        tile_meters=2.2,
        bump=0.6,
        use_for="light Scandinavian interiors",
    ),
    MaterialSpec(
        name="dark_wood",
        label="Dark stained wood",
        pattern="planks",
        palette=((72, 48, 34), (94, 64, 44), (52, 34, 24)),
        roughness=0.35,
        tile_meters=2.0,
        bump=0.7,
        use_for="joinery, furniture, dark feature floors",
    ),
    MaterialSpec(
        name="red_brick",
        label="Red brick",
        pattern="bricks",
        palette=((150, 74, 56), (176, 92, 68), (122, 60, 46)),
        roughness=0.72,
        tile_meters=1.2,
        bump=1.4,
        use_for="exterior and feature walls",
    ),
    MaterialSpec(
        name="pale_brick",
        label="Pale sand brick",
        pattern="bricks",
        palette=((196, 178, 148), (214, 198, 170), (170, 152, 124)),
        roughness=0.7,
        tile_meters=1.2,
        bump=1.3,
        use_for="lighter exterior masonry",
    ),
    MaterialSpec(
        name="white_plaster",
        label="White plaster",
        pattern="plaster",
        palette=((232, 230, 225), (244, 243, 239), (218, 216, 210)),
        roughness=0.82,
        roughness_variation=0.06,
        tile_meters=2.5,
        bump=0.35,
        use_for="interior walls and ceilings — the default indoor surface",
    ),
    MaterialSpec(
        name="grey_render",
        label="Grey render",
        pattern="plaster",
        palette=((168, 168, 166), (186, 186, 183), (148, 148, 146)),
        roughness=0.8,
        roughness_variation=0.08,
        tile_meters=2.5,
        bump=0.45,
        use_for="modern exterior render",
    ),
    MaterialSpec(
        name="concrete",
        label="Board-marked concrete",
        pattern="concrete",
        palette=((150, 150, 148), (170, 170, 167), (128, 128, 126)),
        roughness=0.75,
        tile_meters=2.0,
        bump=0.8,
        use_for="structure, floors, brutalist surfaces",
    ),
    MaterialSpec(
        name="ceramic_tile",
        label="Ceramic floor tile",
        pattern="tiles",
        palette=((226, 224, 218), (238, 237, 232), (204, 202, 196)),
        roughness=0.22,
        tile_meters=0.9,
        bump=0.9,
        use_for="bathrooms, kitchens, wet areas",
    ),
    MaterialSpec(
        name="slate_tile",
        label="Dark slate tile",
        pattern="tiles",
        palette=((70, 74, 78), (88, 92, 96), (54, 58, 62)),
        roughness=0.42,
        tile_meters=0.9,
        bump=1.0,
        use_for="dark floors, hearths, bathrooms",
    ),
    MaterialSpec(
        name="terracotta_roof",
        label="Terracotta roof tiles",
        pattern="shingles",
        palette=((162, 82, 52), (188, 102, 68), (134, 66, 42)),
        roughness=0.68,
        tile_meters=1.4,
        bump=1.6,
        use_for="pitched roofs, Mediterranean and traditional",
    ),
    MaterialSpec(
        name="slate_roof",
        label="Slate roof",
        pattern="shingles",
        palette=((66, 70, 74), (84, 88, 92), (50, 54, 58)),
        roughness=0.55,
        tile_meters=1.4,
        bump=1.4,
        use_for="pitched roofs, northern European",
    ),
    MaterialSpec(
        name="lawn_grass",
        label="Lawn grass",
        pattern="grass",
        palette=((72, 108, 48), (96, 136, 62), (54, 84, 38)),
        roughness=0.88,
        tile_meters=1.6,
        bump=1.1,
        use_for="gardens and site ground",
    ),
    MaterialSpec(
        name="gravel",
        label="Gravel",
        pattern="gravel",
        palette=((138, 132, 122), (160, 154, 144), (110, 105, 98)),
        roughness=0.86,
        tile_meters=1.0,
        bump=1.5,
        use_for="paths, drives, flat roofs",
    ),
    MaterialSpec(
        name="grey_fabric",
        label="Grey upholstery fabric",
        pattern="fabric",
        palette=((122, 124, 128), (140, 142, 146), (104, 106, 110)),
        roughness=0.92,
        roughness_variation=0.05,
        tile_meters=0.6,
        bump=0.6,
        use_for="sofas, chairs, curtains, bedding",
    ),
    MaterialSpec(
        name="brushed_metal",
        label="Brushed metal",
        pattern="metal",
        palette=((170, 172, 176), (196, 198, 202), (146, 148, 152)),
        roughness=0.32,
        roughness_variation=0.08,
        metallic=1.0,
        tile_meters=1.0,
        bump=0.4,
        use_for="handles, frames, appliances, railings",
    ),
    MaterialSpec(
        name="marble",
        label="Veined marble",
        pattern="marble",
        palette=((228, 226, 222), (242, 241, 238), (198, 196, 192)),
        roughness=0.18,
        tile_meters=2.0,
        bump=0.3,
        use_for="worktops, bathrooms, luxury floors",
    ),
    # --- added for lobby and reception work -------------------------------
    #
    # These three came from comparing a real reception render against a
    # photograph the user wanted it to look like. The gap was not lighting alone:
    # the library simply had no polished pale floor, no jointless timber, and no
    # dark cladding, so the closest available choices were a ceramic tile, an oak
    # floorboard and a plaster — and a desk clad in floorboards next to a
    # floorboard floor is what made the render look wrong.
    MaterialSpec(
        name="polished_stone",
        label="Polished pale stone",
        pattern="stone_slab",
        palette=((216, 212, 205), (236, 233, 227), (194, 190, 183)),
        roughness=0.08,
        roughness_variation=0.05,
        tile_meters=1.4,
        bump=0.25,
        use_for="lobby and reception floors — large-format, glossy, reflective",
    ),
    MaterialSpec(
        name="pale_veneer",
        label="Pale timber veneer",
        pattern="veneer",
        palette=((196, 162, 120), (218, 188, 150), (168, 136, 98)),
        roughness=0.3,
        roughness_variation=0.08,
        tile_meters=1.6,
        bump=0.25,
        use_for="reception desks, joinery, wall panelling — smooth, no plank joints",
    ),
    MaterialSpec(
        name="dark_panel",
        label="Dark panelled cladding",
        pattern="panel",
        palette=((60, 58, 58), (82, 79, 78), (42, 40, 40)),
        roughness=0.5,
        tile_meters=1.2,
        bump=0.9,
        use_for="feature walls behind reception, dark accent cladding",
    ),
)


BY_NAME: Final[Mapping[str, MaterialSpec]] = {
    material.name: material for material in MATERIALS
}

#: Map kinds a material provides. ``base_color`` is the only one that is strictly
#: required; the others refine it.
MAP_KINDS: Final[tuple[str, ...]] = ("base_color", "roughness", "normal")


def get(name: str) -> Optional[MaterialSpec]:
    """Look a material up, or ``None``. Never raises on unknown input."""
    return BY_NAME.get(name)


def names() -> tuple[str, ...]:
    return tuple(material.name for material in MATERIALS)


def describe_for_prompt() -> str:
    """The catalogue as the model should read it.

    Generated from the same table the loader uses, so the prompt cannot drift from
    what actually exists — the failure mode being a model that confidently asks
    for ``walnut_parquet`` and gets nothing.
    """
    lines = [
        "MATERIAL LIBRARY. These are real image textures with colour, roughness and "
        "normal maps already prepared. Apply one with studio_material(obj, "
        '"<name>") — see the helper documentation above. Each already knows its own '
        "real-world scale, so do not compute UVs yourself.",
        "",
    ]
    width = max(len(material.name) for material in MATERIALS)
    for material in MATERIALS:
        lines.append(
            f"- {material.name.ljust(width)}  {material.label} — {material.use_for}"
        )
    return "\n".join(lines)


__all__ = [
    "BY_NAME",
    "MAP_KINDS",
    "MATERIALS",
    "MaterialSpec",
    "describe_for_prompt",
    "get",
    "names",
]
