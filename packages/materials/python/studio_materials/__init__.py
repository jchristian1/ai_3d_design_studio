"""studio_materials — the material library Astra designs with.

Three concerns, kept apart:

    catalogue.py   WHAT exists: names, palettes, real-world tile size, usage notes
    synthesize.py  HOW the maps are drawn, procedurally, with no shipped assets
    library.py     WHERE the PNGs are cached, and the paths Blender needs

The public surface is deliberately small: the catalogue (for the model's prompt)
and the resolved library index (for the Blender runtime). Nothing else needs to
know that textures are generated rather than shipped.
"""

from __future__ import annotations

from .catalogue import (
    MAP_KINDS,
    MATERIALS,
    MaterialSpec,
    describe_for_prompt,
    get,
    names,
)
from .library import (
    DEFAULT_DIRECTORY,
    ENV_VAR,
    MaterialMaps,
    directory,
    ensure_library,
    fingerprint,
    library_index,
    maps_for,
)
from .synthesize import TEXTURE_SIZE, synthesize

__all__ = [
    "DEFAULT_DIRECTORY",
    "ENV_VAR",
    "MAP_KINDS",
    "MATERIALS",
    "TEXTURE_SIZE",
    "MaterialMaps",
    "MaterialSpec",
    "describe_for_prompt",
    "directory",
    "ensure_library",
    "fingerprint",
    "get",
    "library_index",
    "maps_for",
    "names",
    "synthesize",
]
