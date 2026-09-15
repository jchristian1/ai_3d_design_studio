"""Deterministic world-space direction mapping — Python representation.

Spec 001, Task 2.

Convention for this milestone (fixed, total, and table-driven)::

    right   -> +X        left    -> -X
    forward -> +Y        back    -> -Y
    up      -> +Z        down    -> -Z

"right" means Blender WORLD-SPACE +X, as required by Spec 001.

Scope and deliberate non-goals:
  - Camera-relative interpretation is explicitly DEFERRED. Nothing here reads a
    camera, a view matrix, or any scene state, so the mapping cannot become
    view-dependent by accident.
  - Direction interpretation is kept independent of Blender execution: this
    module produces vocabulary and plain numbers, never ``bpy`` calls. The worker
    applies the result (Task 4); it is not applied here.
  - No natural-language parsing. Mapping the phrase "to the right" onto the
    ``right`` token is the agent's job (Task 6).

Canonical schemas: ``direction.schema.json``, ``axis-direction.schema.json``
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional

from studio_contracts import ChatError
from studio_types import DIRECTIONS, Axis, AxisDirection, Direction, Vec3

from .units import to_meters

#: The complete, canonical direction table. Wrapped in a read-only mapping proxy
#: so the mapping cannot be mutated at runtime by a caller.
DIRECTION_TO_AXIS: Mapping[Direction, AxisDirection] = MappingProxyType(
    {
        "right": AxisDirection(axis="x", sign=1),
        "left": AxisDirection(axis="x", sign=-1),
        "forward": AxisDirection(axis="y", sign=1),
        "back": AxisDirection(axis="y", sign=-1),
        "up": AxisDirection(axis="z", sign=1),
        "down": AxisDirection(axis="z", sign=-1),
    }
)

DIRECTION_MESSAGES: dict[str, str] = {
    "UNSUPPORTED_DIRECTION": (
        "unsupported direction; expected one of: " + ", ".join(DIRECTIONS)
    ),
    "NEGATIVE_MAGNITUDE": (
        "distance must be a non-negative magnitude; express direction with the "
        "direction token, not a negative distance"
    ),
}


@dataclass(frozen=True)
class DirectionResult:
    ok: bool
    axis_direction: Optional[AxisDirection] = None
    error: Optional[ChatError] = None


@dataclass(frozen=True)
class DeltaResult:
    ok: bool
    delta: Optional[Vec3] = None
    axis: Optional[Axis] = None
    meters: Optional[float] = None
    error: Optional[ChatError] = None


def is_direction(value: Any) -> bool:
    """True when the value is one of the canonical direction tokens."""
    return isinstance(value, str) and value in DIRECTIONS


def resolve_direction(direction: Any) -> DirectionResult:
    """Resolve a named world-space direction to an axis and sign."""
    if not is_direction(direction):
        return DirectionResult(
            ok=False,
            error=ChatError(
                code="VALIDATION_ERROR",
                message=DIRECTION_MESSAGES["UNSUPPORTED_DIRECTION"],
            ),
        )
    return DirectionResult(ok=True, axis_direction=DIRECTION_TO_AXIS[direction])


def zero_delta() -> Vec3:
    """The zero delta, in canonical meters."""
    return Vec3(0.0, 0.0, 0.0)


def direction_delta_meters(direction: Any, measurement: Any) -> DeltaResult:
    """Compose a direction and a distance MAGNITUDE into a world-space delta.

    This is the shape the MCP ``move_object`` tool consumes (Task 3), produced
    without any Blender involvement. Only the resolved axis is non-zero.

    Direction is carried by the direction token ONLY. The measurement must be a
    non-negative magnitude, so direction is never encoded twice::

        direction_delta_meters("left",  25 cm) -> Vec3(-0.25, 0, 0)
        direction_delta_meters("left", -25 cm) -> VALIDATION_ERROR

    Zero is a valid magnitude and yields a zero delta.

    This restriction lives at the composition boundary only. The generic
    conversion utilities in ``units.py`` remain signed on purpose: -25 cm is a
    legitimate signed measurement in contexts such as coordinates and offsets.
    """
    resolved = resolve_direction(direction)
    if not resolved.ok:
        return DeltaResult(ok=False, error=resolved.error)

    converted = to_meters(measurement)
    if not converted.ok:
        return DeltaResult(ok=False, error=converted.error)

    # Reject a negative magnitude only here, never in the generic converter.
    if converted.meters < 0:
        return DeltaResult(
            ok=False,
            error=ChatError(
                code="VALIDATION_ERROR",
                message=DIRECTION_MESSAGES["NEGATIVE_MAGNITUDE"],
            ),
        )

    axis = resolved.axis_direction.axis
    sign = resolved.axis_direction.sign
    signed = converted.meters * sign
    # Normalize -0.0 so a zero-distance move is sign-stable across languages.
    magnitude = 0.0 if signed == 0 else signed

    components = {"x": 0.0, "y": 0.0, "z": 0.0}
    components[axis] = magnitude

    return DeltaResult(
        ok=True,
        delta=Vec3(**components),
        axis=axis,
        meters=magnitude,
    )


__all__ = [
    "DIRECTION_MESSAGES",
    "DIRECTION_TO_AXIS",
    "DeltaResult",
    "DirectionResult",
    "direction_delta_meters",
    "is_direction",
    "resolve_direction",
    "zero_delta",
]
