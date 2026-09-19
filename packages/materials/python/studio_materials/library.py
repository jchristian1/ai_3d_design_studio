"""The on-disk texture library, and where it lives.

    catalogue.py   what materials exist
    synthesize.py  how their maps are drawn
    library.py     where the PNGs are cached      <- this module

WHY A CACHE RATHER THAN COMMITTED FILES

The maps are generated, not authored, so committing them would put ~50 binary
files in git that no one reviews and that any catalogue tweak invalidates. Instead
they are written once into a gitignored runtime directory, exactly like the Blender
seed fixture: the committed source of truth is the generator, and
:func:`ensure_library` makes the output exist before it is needed.

Generation is idempotent and cheap to skip — a manifest records the catalogue
fingerprint, so an unchanged catalogue is a directory listing rather than a
re-render, and a changed one regenerates without anybody remembering to.

WHY THE PATH NEVER REACHES THE MODEL

Astra's authored code is risk-classified before it runs, and an absolute path
literal is one of the things that forces a user approval prompt. If Astra had to
write ``bpy.data.images.load("/…/oak_floor_base_color.png")``, every textured
material would stop and ask permission — which would make texturing unusable.

So the path is resolved HERE, in platform code, and injected into the Blender
runtime as a helper the model calls by material NAME. The model never writes a
path, so the classifier never sees one. See
``blender_worker.backends.official.materials``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping, Optional

from . import catalogue, synthesize
from .catalogue import MaterialSpec

_log = logging.getLogger(__name__)

#: Overrides the cache location. Used by tests so they never touch the real one.
ENV_VAR: Final = "STUDIO_TEXTURE_DIR"

#: Repository root, derived from this file's location:
#: packages/materials/python/studio_materials/library.py -> four levels up.
_REPO_ROOT: Final = Path(__file__).resolve().parents[4]

#: Default cache. ``/runtime/`` is gitignored, so nothing generated is committed.
DEFAULT_DIRECTORY: Final = _REPO_ROOT / "runtime" / "textures"

MANIFEST_NAME: Final = "manifest.json"


@dataclass(frozen=True)
class MaterialMaps:
    """Where one material's maps ended up."""

    name: str
    spec: MaterialSpec
    base_color: Path
    roughness: Path
    normal: Path

    def as_paths(self) -> dict[str, str]:
        return {
            "base_color": str(self.base_color),
            "roughness": str(self.roughness),
            "normal": str(self.normal),
        }


def directory() -> Path:
    """The cache directory in force."""
    override = os.environ.get(ENV_VAR)
    return Path(override) if override else DEFAULT_DIRECTORY


def fingerprint() -> str:
    """Identity of the catalogue AND the synthesiser settings.

    Included in the manifest so a regenerated library is invalidated by a change to
    either the material definitions or the texture resolution. Without this, an
    edited palette would silently keep serving the old PNGs.
    """
    digest = hashlib.sha256()
    digest.update(str(synthesize.TEXTURE_SIZE).encode("utf-8"))
    for material in catalogue.MATERIALS:
        digest.update(repr(material).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _map_path(root: Path, name: str, kind: str) -> Path:
    return root / f"{name}_{kind}.png"


def _manifest_path(root: Path) -> Path:
    return root / MANIFEST_NAME


def _is_current(root: Path) -> bool:
    """True when the cache already holds this exact catalogue."""
    manifest = _manifest_path(root)
    if not manifest.is_file():
        return False
    try:
        recorded = json.loads(manifest.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    if recorded.get("fingerprint") != fingerprint():
        return False
    # A manifest is not evidence the images survived, so verify them.
    for material in catalogue.MATERIALS:
        for kind in catalogue.MAP_KINDS:
            path = _map_path(root, material.name, kind)
            if not path.is_file() or path.stat().st_size == 0:
                return False
    return True


def ensure_library(force: bool = False) -> Path:
    """Make the texture library exist, and return its directory.

    Safe to call repeatedly and from more than one process: each PNG is written to
    a temporary name and then atomically replaced, so a concurrent reader sees
    either the old file or the new one and never a half-written image.
    """
    root = directory()
    if not force and _is_current(root):
        return root

    root.mkdir(parents=True, exist_ok=True)
    _log.info("generating %d studio materials into %s", len(catalogue.MATERIALS), root)

    for material in catalogue.MATERIALS:
        maps = synthesize.synthesize(material)
        for kind, image in maps.items():
            destination = _map_path(root, material.name, kind)
            temporary = destination.with_suffix(f".{os.getpid()}.tmp")
            # Colour maps are sRGB data; roughness and normal are technical data
            # that must NOT be colour-managed, which is handled at load time in
            # Blender. Nothing here embeds a colour profile either way.
            image.save(temporary, format="PNG", optimize=True)
            os.replace(temporary, destination)

    payload = {
        "fingerprint": fingerprint(),
        "texture_size": synthesize.TEXTURE_SIZE,
        "materials": {
            material.name: {
                "label": material.label,
                "tile_meters": material.tile_meters,
                "metallic": material.metallic,
                "roughness": material.roughness,
                "maps": {
                    kind: _map_path(root, material.name, kind).name
                    for kind in catalogue.MAP_KINDS
                },
            }
            for material in catalogue.MATERIALS
        },
    }
    temporary = _manifest_path(root).with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), "utf-8")
    os.replace(temporary, _manifest_path(root))

    return root


def maps_for(name: str, *, ensure: bool = True) -> Optional[MaterialMaps]:
    """Resolved paths for one material, or ``None`` if it is not in the catalogue."""
    spec = catalogue.get(name)
    if spec is None:
        return None
    root = ensure_library() if ensure else directory()
    return MaterialMaps(
        name=name,
        spec=spec,
        base_color=_map_path(root, name, "base_color"),
        roughness=_map_path(root, name, "roughness"),
        normal=_map_path(root, name, "normal"),
    )


def library_index(*, ensure: bool = True) -> dict[str, dict[str, object]]:
    """Everything Blender needs to build every material, keyed by name.

    This is the structure injected into the Blender runtime. It carries paths, so
    it is platform data and never goes anywhere near a prompt or the browser.
    """
    if ensure:
        ensure_library()
    root = directory()
    index: dict[str, dict[str, object]] = {}
    for material in catalogue.MATERIALS:
        index[material.name] = {
            "label": material.label,
            "tile_meters": material.tile_meters,
            "roughness": material.roughness,
            "metallic": material.metallic,
            "maps": {
                kind: str(_map_path(root, material.name, kind))
                for kind in catalogue.MAP_KINDS
            },
        }
    return index


__all__ = [
    "DEFAULT_DIRECTORY",
    "ENV_VAR",
    "MANIFEST_NAME",
    "MaterialMaps",
    "directory",
    "ensure_library",
    "fingerprint",
    "library_index",
    "maps_for",
]
