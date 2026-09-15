"""studio_spatial — deterministic spatial utilities.

Concerns, all pure:
    units.py       length-unit conversion into canonical METRES   (Spec 001, Task 2)
    directions.py  named world-space direction -> axis + sign     (Spec 001, Task 2)
    angles.py      angle-unit conversion into canonical RADIANS   (Spec 002, Task 2)
    sizing.py      normalized resize intent -> absolute factor    (Spec 002, Task 2)
    colors.py      canonical linear sRGB colour + tiny palette    (Spec 002, Task 2)

Everything here is deterministic, independently testable, free of Blender calls,
and free of natural-language/AI reasoning. This package is also free of scene
state, camera/view state, provider code, HTTP, the filesystem and the environment
(``structure.md``), which is what lets one conversion site serve every service.

This is the ONE place unit and value conversion lives. cm->m, deg->rad,
percent->factor and encoded sRGB->linear each exist exactly once, here; a source
guard (``tests/spatial/test_conversion_site_guard.py``) fails if a second one
appears anywhere in runtime code.

Conversion is not normalization: nothing here reduces an angle modulo 2π, clamps a
colour into range, or clips a resize into a "reasonable" band. Those are different
decisions and belong to whoever is entitled to make them.

The behaviour is pinned by a shared language-neutral corpus
(``../../spatial-cases.json``) that the TypeScript representation
(``@studio/spatial``) executes as well.

Direction of truth for the vocabulary these functions speak::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL
             |
             +--> TypeScript representation (@studio/spatial)
             +--> Python representation (this package)
"""

from .angles import (
    ANGLE_MESSAGES,
    ANGLE_UNIT_ALIASES,
    DEGREES_PER_HALF_TURN,
    RADIANS_PER_DEGREE,
    AngleResult,
    convert_to_radians,
    degrees_to_radians,
    is_finite_angle,
    normalize_angle_unit,
    radians_to_degrees,
    radians_to_radians,
    to_radians,
)
from .colors import (
    COLOR_CHANNELS,
    COLOR_MESSAGES,
    COLOR_NAME_ALIASES,
    EIGHT_BIT_MAX,
    NAMED_COLORS,
    PALETTE_PROVENANCE_HEX,
    SRGB_ALPHA,
    SRGB_EXPONENT,
    SRGB_LINEAR_SLOPE,
    SRGB_LINEAR_THRESHOLD,
    SRGB_SCALE,
    TRANSFER_CHANNELS,
    ColorResult,
    color_from_srgb_8bit,
    is_named_color,
    linear_channel_to_srgb_encoded,
    normalize_color_name,
    resolve_named_color,
    srgb_8bit_to_linear,
    srgb_encoded_channel_to_linear,
    validate_material_color,
)
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
from .sizing import (
    MAX_REDUCTION_PERCENT,
    PERCENT_WHOLE,
    SIZE_DIRECTIONS,
    SIZING_MESSAGES,
    SizeDirection,
    SizeFactorResult,
    is_size_direction,
    is_valid_size_factor,
    resize_factor,
    validate_size_factor,
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
    "ANGLE_MESSAGES",
    "ANGLE_UNIT_ALIASES",
    "CM_PER_METER",
    "COLOR_CHANNELS",
    "COLOR_MESSAGES",
    "COLOR_NAME_ALIASES",
    "CONVERSION_MESSAGES",
    "ConversionResult",
    "DEGREES_PER_HALF_TURN",
    "DIRECTION_MESSAGES",
    "DIRECTION_TO_AXIS",
    "EIGHT_BIT_MAX",
    "MAX_REDUCTION_PERCENT",
    "NAMED_COLORS",
    "PALETTE_PROVENANCE_HEX",
    "PERCENT_WHOLE",
    "RADIANS_PER_DEGREE",
    "SIZE_DIRECTIONS",
    "SIZING_MESSAGES",
    "SRGB_ALPHA",
    "SRGB_EXPONENT",
    "SRGB_LINEAR_SLOPE",
    "SRGB_LINEAR_THRESHOLD",
    "SRGB_SCALE",
    "TRANSFER_CHANNELS",
    "AngleResult",
    "ColorResult",
    "DeltaResult",
    "DirectionResult",
    "SizeDirection",
    "SizeFactorResult",
    "cm_to_meters",
    "color_from_srgb_8bit",
    "convert_to_meters",
    "convert_to_radians",
    "degrees_to_radians",
    "direction_delta_meters",
    "is_direction",
    "is_finite_angle",
    "is_named_color",
    "is_size_direction",
    "is_valid_size_factor",
    "linear_channel_to_srgb_encoded",
    "meters_to_meters",
    "normalize_angle_unit",
    "normalize_color_name",
    "radians_to_degrees",
    "radians_to_radians",
    "resize_factor",
    "resolve_direction",
    "resolve_named_color",
    "srgb_8bit_to_linear",
    "srgb_encoded_channel_to_linear",
    "to_meters",
    "to_radians",
    "validate_material_color",
    "validate_size_factor",
    "zero_delta",
]
