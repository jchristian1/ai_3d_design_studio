"""Turn backend read output into a canonical ``SceneSnapshot``.

Backend output is UNTRUSTED integration data. It is validated here before a snapshot
exists: a missing field, a wrong type, or a non-finite number is a structured failure
rather than a defaulted value. Absent stays absent, so "no material" and "black"
remain distinguishable — a normaliser that blurs that distinction would make the
agent confidently wrong.

The digest is the single Task 1 implementation (``compute_scene_version``). There is
deliberately no second digest anywhere in the project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from studio_contracts.scene import compute_scene_version
from studio_types import (
    EulerRadians,
    MaterialColor,
    MaterialSummary,
    Scale3,
    SceneObject,
    SceneSnapshot,
    SceneUnits,
    Vec3,
)

UNIT_SYSTEMS = ("NONE", "METRIC", "IMPERIAL")


class NormalisationError(ValueError):
    """Raised when backend output cannot be trusted as a scene description."""


@dataclass(frozen=True)
class SceneReadout:
    """A validated snapshot plus the raw scene name, for diagnostics."""

    snapshot: SceneSnapshot
    scene_name: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _finite(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NormalisationError(f"{path} is not a number: {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise NormalisationError(f"{path} is not finite")
    return number


def _triple(raw: Any, path: str) -> tuple[float, float, float]:
    if not isinstance(raw, Mapping):
        raise NormalisationError(f"{path} is not an object")
    return (
        _finite(raw.get("x"), f"{path}.x"),
        _finite(raw.get("y"), f"{path}.y"),
        _finite(raw.get("z"), f"{path}.z"),
    )


def _material(raw: Any, path: str) -> Optional[MaterialSummary]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise NormalisationError(f"{path} is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise NormalisationError(f"{path}.name is missing")
    base_raw = raw.get("base_color")
    base: Optional[MaterialColor] = None
    if base_raw is not None:
        if not isinstance(base_raw, Mapping):
            raise NormalisationError(f"{path}.base_color is not an object")
        channels = []
        for channel in ("r", "g", "b", "a"):
            value = _finite(base_raw.get(channel), f"{path}.base_color.{channel}")
            if not 0.0 <= value <= 1.0:
                raise NormalisationError(
                    f"{path}.base_color.{channel} is outside [0, 1]"
                )
            channels.append(value)
        base = MaterialColor(r=channels[0], g=channels[1], b=channels[2], a=channels[3])
    return MaterialSummary(name=name, base_color=base)


def _scene_object(raw: Any, index: int) -> SceneObject:
    path = f"objects[{index}]"
    if not isinstance(raw, Mapping):
        raise NormalisationError(f"{path} is not an object")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise NormalisationError(f"{path}.name is missing")

    object_type = raw.get("object_type")
    if not isinstance(object_type, str) or not object_type:
        raise NormalisationError(f"{path}.object_type is missing")

    visible = raw.get("visible")
    if not isinstance(visible, bool):
        raise NormalisationError(f"{path}.visible is not a boolean")

    studio_object_id = raw.get("studio_object_id")
    if studio_object_id is not None and not isinstance(studio_object_id, str):
        raise NormalisationError(f"{path}.studio_object_id is not a string")

    position = _triple(raw.get("world_position_meters"), f"{path}.world_position_meters")
    dimensions = _triple(raw.get("dimensions_meters"), f"{path}.dimensions_meters")
    rotation = _triple(raw.get("rotation_euler_radians"), f"{path}.rotation_euler_radians")
    scale = _triple(raw.get("scale"), f"{path}.scale")

    return SceneObject(
        name=name,
        object_type=object_type,
        world_position_meters=Vec3(*position),
        dimensions_meters=Vec3(*dimensions),
        rotation_euler_radians=EulerRadians(*rotation),
        scale=Scale3(*scale),
        visible=visible,
        studio_object_id=studio_object_id or None,
        material=_material(raw.get("material"), path),
    )


def _units(raw: Any) -> SceneUnits:
    if not isinstance(raw, Mapping):
        raise NormalisationError("units is not an object")
    unit_system = raw.get("unit_system")
    if unit_system not in UNIT_SYSTEMS:
        raise NormalisationError(f"units.unit_system is not recognised: {unit_system!r}")
    length_unit = raw.get("length_unit")
    if length_unit != "m":
        # The platform's canonical length unit is metres. A backend reporting
        # anything else must be converted at the boundary, not accepted here.
        raise NormalisationError(
            f"units.length_unit must be metres at this boundary, got {length_unit!r}"
        )
    return SceneUnits(
        unit_system=unit_system,
        length_unit="m",
        scale_length=_finite(raw.get("scale_length"), "units.scale_length"),
    )


def scene_snapshot_from_backend(
    project_id: str,
    payload: Mapping[str, Any],
    *,
    captured_at: Optional[str] = None,
) -> SceneReadout:
    """Validate backend read output and build a canonical ``SceneSnapshot``."""
    if not isinstance(payload, Mapping):
        raise NormalisationError("backend payload is not an object")

    raw_objects = payload.get("objects")
    if not isinstance(raw_objects, Sequence) or isinstance(raw_objects, (str, bytes)):
        raise NormalisationError("objects is not a list")

    objects = tuple(_scene_object(raw, index) for index, raw in enumerate(raw_objects))
    units = _units(payload.get("units"))

    # The digest is computed over the projection, then carried on the snapshot.
    provisional = SceneSnapshot(
        project_id=project_id,
        scene_version="",
        units=units,
        objects=objects,
        captured_at=captured_at or _utc_now(),
    )
    version = compute_scene_version(provisional)
    snapshot = SceneSnapshot(
        project_id=project_id,
        scene_version=version,
        units=units,
        objects=objects,
        captured_at=provisional.captured_at,
    )

    scene_name = payload.get("scene_name")
    return SceneReadout(
        snapshot=snapshot,
        scene_name=scene_name if isinstance(scene_name, str) else "",
    )


def find_object(
    snapshot: SceneSnapshot, *, object_id: Optional[str] = None, name: Optional[str] = None
) -> Optional[SceneObject]:
    """Resolve an object in a snapshot by stable id first, then by exact name."""
    if object_id:
        for candidate in snapshot.objects:
            if candidate.studio_object_id == object_id:
                return candidate
    if name:
        for candidate in snapshot.objects:
            if candidate.name == name:
                return candidate
    return None
