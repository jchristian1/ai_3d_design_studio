"""Deterministic length-unit conversion — Python representation.

Spec 001, Task 2. Canonical internal unit is METERS.

Scope and deliberate non-goals:
  - Pure arithmetic only. No Blender, no ``bpy``, no I/O.
  - No natural-language parsing and no AI reasoning. Callers supply an
    already-structured Measurement; interpreting prose is the agent's job
    (Task 6) and is kept out of here on purpose.
  - Supported units are exactly the ones Spec 001 needs: cm and m.

Determinism: cm -> m is a single IEEE-754 division by 100, never a
multiplication by 0.01. Division is correctly rounded; multiplying by the inexact
constant 0.01 rounds twice and can land on a different float. Verified
counterexample: ``6537.04249344076 / 100`` is 65.3704249344076 whereas
``6537.04249344076 * 0.01`` is 65.37042493440761. Both Python and TypeScript use
IEEE-754 doubles and correctly-rounded division, so results are bit-identical
across the two representations.

Note: conversion returns the nearest float to the quotient of the *float* input,
which is not always the nearest float to the decimal you typed (``1.1 / 100`` is
0.011000000000000001, not 0.011). That is inherent to binary floating point, is
identical in both languages, and is pinned by the corpus.

Canonical schemas: ``measurement.schema.json``, ``length-unit.schema.json``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

from studio_contracts import ChatError
from studio_types import LENGTH_UNITS, LengthUnit
from studio_validation import is_finite_meters

#: Centimeters per meter. The only conversion factor this milestone needs.
CM_PER_METER = 100

#: Failure messages are fixed strings (no value interpolation) so they are
#: byte-identical across languages and safe to assert on in parity tests.
CONVERSION_MESSAGES: dict[str, str] = {
    "NON_FINITE_VALUE": "measurement value must be a finite number",
    "UNSUPPORTED_UNIT": (
        "unsupported length unit; expected one of: " + ", ".join(LENGTH_UNITS)
    ),
    "NOT_A_MEASUREMENT": "measurement must be an object with value and unit",
}


@dataclass(frozen=True)
class ConversionResult:
    """Result of a conversion: either ``meters`` or a structured ``error``."""

    ok: bool
    meters: Optional[float] = None
    error: Optional[ChatError] = None


def _invalid_units(message: str) -> ConversionResult:
    return ConversionResult(
        ok=False, error=ChatError(code="INVALID_UNITS", message=message)
    )


def _normalize_zero(value: float) -> float:
    """Normalize negative zero to positive zero.

    ``-0.0`` and ``0.0`` are numerically equal but serialize differently across
    languages (Python ``repr(-0.0)`` is "-0.0"; JS ``JSON.stringify(-0)`` is
    "0"). Collapsing the sign keeps output identical in both representations.
    """
    return 0.0 if value == 0 else value


def cm_to_meters(value: Any) -> ConversionResult:
    """Convert a finite centimeter value to canonical meters."""
    if not is_finite_meters(value):
        return _invalid_units(CONVERSION_MESSAGES["NON_FINITE_VALUE"])
    return ConversionResult(ok=True, meters=_normalize_zero(value / CM_PER_METER))


def meters_to_meters(value: Any) -> ConversionResult:
    """Pass a meter value through unchanged (identity), after validating it.

    Kept explicit rather than special-cased at the call site so every accepted
    unit goes through the same validate-then-convert path.
    """
    if not is_finite_meters(value):
        return _invalid_units(CONVERSION_MESSAGES["NON_FINITE_VALUE"])
    return ConversionResult(ok=True, meters=_normalize_zero(float(value)))


def convert_to_meters(value: Any, unit: Any) -> ConversionResult:
    """Convert a ``(value, unit)`` pair to canonical meters."""
    if unit == "cm":
        return cm_to_meters(value)
    if unit == "m":
        return meters_to_meters(value)
    # Unit is not in the canonical enum. Value validity is irrelevant here:
    # an unknown unit makes the measurement uninterpretable.
    return _invalid_units(CONVERSION_MESSAGES["UNSUPPORTED_UNIT"])


def to_meters(measurement: Any) -> ConversionResult:
    """Convert a Measurement to canonical meters, validating shape and value.

    Accepts either a mapping (raw wire document) or an object exposing ``value``
    and ``unit`` attributes (the ``Measurement`` dataclass).
    """
    if isinstance(measurement, Mapping):
        value = measurement.get("value")
        unit = measurement.get("unit")
    elif hasattr(measurement, "unit") and hasattr(measurement, "value"):
        value = measurement.value
        unit = measurement.unit
    else:
        return _invalid_units(CONVERSION_MESSAGES["NOT_A_MEASUREMENT"])

    if not isinstance(unit, str) or unit not in LENGTH_UNITS:
        return _invalid_units(CONVERSION_MESSAGES["UNSUPPORTED_UNIT"])

    return convert_to_meters(value, unit)


__all__ = [
    "CM_PER_METER",
    "CONVERSION_MESSAGES",
    "ConversionResult",
    "cm_to_meters",
    "convert_to_meters",
    "meters_to_meters",
    "to_meters",
]
