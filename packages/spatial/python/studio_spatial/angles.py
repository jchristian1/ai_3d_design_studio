"""Deterministic angle-unit conversion — Python representation.

Spec 002, Task 2. Canonical internal angular unit is RADIANS.

Why radians: Blender's ``rotation_euler`` is radians, and the Task 5 fixture
digest already records ``rotation_euler_radians``, so choosing radians means ZERO
conversion at the Blender boundary — the place a conversion bug would be most
expensive. Degrees are a *language-edge* unit: "45 degrees" is converted exactly
once, here, above the worker, exactly as "50 cm" is.

Scope and deliberate non-goals:
  - Pure arithmetic only. No Blender, no ``bpy``, no I/O, no environment.
  - No natural-language parsing and no AI reasoning. Callers supply an
    already-structured AngleMeasurement; interpreting prose belongs to the agent.
  - CONVERSION IS NOT NORMALIZATION. Nothing here reduces an angle modulo 2π.
    Those are different concerns: 720° is a legitimate two-full-turn instruction
    and silently collapsing it to 0 would destroy intent. If a future operation
    needs a principal value it must ask for it explicitly.

Determinism: deg -> rad multiplies by ONE precomputed constant,
``RADIANS_PER_DEGREE = pi / 180``. Both Python and TypeScript compute that
constant from the same double ``pi`` with correctly-rounded division, then perform
one correctly-rounded multiplication, so results are bit-identical across the two
representations. ``math.radians`` is deliberately NOT used: it would hide the
formula behind a C library call that JavaScript has no counterpart for, and the
parity contract would then rest on an implementation detail instead of on a
documented expression.

The chosen formula also lands exactly on the expected constants:
``45 deg`` is exactly ``pi / 4``, ``90 deg`` exactly ``pi / 2``, ``180 deg``
exactly ``pi`` (asserted in the tests, not assumed).

Canonical schemas: ``angle-measurement.schema.json``, ``angle-unit.schema.json``
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from studio_contracts import ChatError
from studio_types import ANGLE_UNITS, AngleUnit

#: Degrees in a half turn. Named so the conversion reads as geometry rather than
#: as a magic number.
DEGREES_PER_HALF_TURN = 180

#: The single conversion constant. Computed once from ``math.pi`` so a value is
#: converted with exactly one multiplication (one rounding).
RADIANS_PER_DEGREE = math.pi / DEGREES_PER_HALF_TURN

#: Narrow, CLOSED token aliases accepted by :func:`normalize_angle_unit`.
#:
#: These are TOKENS, not prose: the same nature as the direction tokens in
#: ``directions.py``, which Spec 001's audit recorded as "tokens, not prose". No
#: sentence is parsed here, and the canonical schema stays closed to the two
#: normalized values, so only ``rad``/``deg`` ever cross a boundary.
ANGLE_UNIT_ALIASES: dict[str, AngleUnit] = {
    "rad": "rad",
    "radian": "rad",
    "radians": "rad",
    "deg": "deg",
    "degree": "deg",
    "degrees": "deg",
    "°": "deg",
}

#: Failure messages are fixed strings (no value interpolation) so they are
#: byte-identical across languages and safe to assert on in parity tests.
ANGLE_MESSAGES: dict[str, str] = {
    "NON_FINITE_VALUE": "angle value must be a finite number",
    "UNSUPPORTED_UNIT": (
        "unsupported angle unit; expected one of: " + ", ".join(ANGLE_UNITS)
    ),
    "NOT_A_MEASUREMENT": "angle must be an object with value and unit",
}


@dataclass(frozen=True)
class AngleResult:
    """Result of a conversion: either ``radians`` or a structured ``error``."""

    ok: bool
    radians: Optional[float] = None
    error: Optional[ChatError] = None


def _invalid_units(message: str) -> AngleResult:
    return AngleResult(ok=False, error=ChatError(code="INVALID_UNITS", message=message))


def is_finite_angle(value: Any) -> bool:
    """True when ``value`` is a real, finite number.

    Booleans are rejected explicitly: ``True`` is an ``int`` in Python and would
    otherwise convert to 0.017... radians, which is never what a caller meant.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _normalize_zero(value: float) -> float:
    """Normalize negative zero to positive zero.

    ``-0.0`` and ``0.0`` are numerically equal but serialize differently across
    languages (Python ``repr(-0.0)`` is "-0.0"; JS ``JSON.stringify(-0)`` is
    "0"). Collapsing the sign keeps output identical in both representations, and
    matches the Task 1 digest rule.
    """
    return 0.0 if value == 0 else value


def normalize_angle_unit(unit: Any) -> Optional[AngleUnit]:
    """Normalize a unit TOKEN to its canonical wire value, or ``None``.

    Case-insensitive and whitespace-trimmed. Accepts only the closed alias set in
    :data:`ANGLE_UNIT_ALIASES`; anything else is unrecognised rather than guessed.
    """
    if not isinstance(unit, str):
        return None
    return ANGLE_UNIT_ALIASES.get(unit.strip().lower())


def degrees_to_radians(value: Any) -> AngleResult:
    """Convert a finite degree value to canonical radians."""
    if not is_finite_angle(value):
        return _invalid_units(ANGLE_MESSAGES["NON_FINITE_VALUE"])
    return AngleResult(ok=True, radians=_normalize_zero(value * RADIANS_PER_DEGREE))


def radians_to_radians(value: Any) -> AngleResult:
    """Pass a radian value through unchanged (identity), after validating it.

    Kept explicit rather than special-cased at the call site so every accepted
    unit goes through the same validate-then-convert path, exactly as
    ``meters_to_meters`` does for lengths.
    """
    if not is_finite_angle(value):
        return _invalid_units(ANGLE_MESSAGES["NON_FINITE_VALUE"])
    return AngleResult(ok=True, radians=_normalize_zero(float(value)))


def convert_to_radians(value: Any, unit: Any) -> AngleResult:
    """Convert a ``(value, unit)`` pair to canonical radians.

    ``unit`` may be any accepted token; it is normalized first.
    """
    normalized = normalize_angle_unit(unit)
    if normalized == "deg":
        return degrees_to_radians(value)
    if normalized == "rad":
        return radians_to_radians(value)
    # Unit is not in the canonical vocabulary. Value validity is irrelevant here:
    # an unknown unit makes the angle uninterpretable.
    return _invalid_units(ANGLE_MESSAGES["UNSUPPORTED_UNIT"])


def to_radians(measurement: Any) -> AngleResult:
    """Convert an AngleMeasurement to canonical radians, validating shape first.

    Accepts either a mapping (raw wire document) or an object exposing ``value``
    and ``unit`` attributes.
    """
    if isinstance(measurement, Mapping):
        value = measurement.get("value")
        unit = measurement.get("unit")
    elif hasattr(measurement, "unit") and hasattr(measurement, "value"):
        value = measurement.value
        unit = measurement.unit
    else:
        return _invalid_units(ANGLE_MESSAGES["NOT_A_MEASUREMENT"])

    return convert_to_radians(value, unit)


def radians_to_degrees(value: Any) -> Optional[float]:
    """Convert canonical radians back to degrees, or ``None`` if not finite.

    For DIAGNOSTICS and user-facing display only — never for anything crossing a
    boundary, where radians are canonical. Deliberately returns a bare float
    rather than an :class:`AngleResult`: that result type's field is named
    ``radians``, and reusing it to carry degrees would be exactly the kind of
    unit-mislabelling this module exists to prevent.

    Division by the same single constant, so the round trip is as tight as binary
    floating point allows.
    """
    if not is_finite_angle(value):
        return None
    return _normalize_zero(value / RADIANS_PER_DEGREE)


__all__ = [
    "ANGLE_MESSAGES",
    "ANGLE_UNIT_ALIASES",
    "DEGREES_PER_HALF_TURN",
    "RADIANS_PER_DEGREE",
    "AngleResult",
    "convert_to_radians",
    "degrees_to_radians",
    "is_finite_angle",
    "normalize_angle_unit",
    "radians_to_degrees",
    "radians_to_radians",
    "to_radians",
]
