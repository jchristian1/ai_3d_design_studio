"""studio_spatial — deterministic spatial utilities (Spec 001, Task 2).

Two concerns, both pure:
    units.py       length-unit conversion into canonical meters
    directions.py  named world-space direction -> axis + sign

Everything here is deterministic, independently testable, free of Blender calls,
and free of natural-language/AI reasoning. The behaviour is pinned by a shared
language-neutral corpus (``../../spatial-cases.json``) that the TypeScript
representation (``@studio/spatial``) executes as well.

Direction of truth for the vocabulary these functions speak::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL
             |
             +--> TypeScript representation (@studio/spatial)
             +--> Python representation (this package)
"""

from .directions import (
    DIRECTION_MESSAGES,
    DIRECTION_TO_AXIS,
    DeltaResult,
    DirectionResult,
    direction_delta_meters,
    is_direction,
    resolve_direction,
    zero_delta,
)
from .units import (
    CM_PER_METER,
    CONVERSION_MESSAGES,
    ConversionResult,
    cm_to_meters,
    convert_to_meters,
    meters_to_meters,
    to_meters,
)

__all__ = [
    "CM_PER_METER",
    "CONVERSION_MESSAGES",
    "ConversionResult",
    "DIRECTION_MESSAGES",
    "DIRECTION_TO_AXIS",
    "DeltaResult",
    "DirectionResult",
    "cm_to_meters",
    "convert_to_meters",
    "direction_delta_meters",
    "is_direction",
    "meters_to_meters",
    "resolve_direction",
    "to_meters",
    "zero_delta",
]
