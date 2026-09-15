"""Deterministic colour interpretation — Python representation.

Spec 002, Task 2.

Canonical colour: **linear sRGB, RGBA, finite floats in [0, 1]**.

What "linear sRGB" means here, precisely
----------------------------------------

sRGB **primaries** and white point, with the sRGB **transfer function decoded**,
so each of R, G and B is a linear-light value. It is NOT an encoded/"gamma" sRGB
value of the kind written as ``#E6D2B5`` or ``rgb(230 210 181)`` in CSS. Those are
transfer-encoded; calling them linear is the exact ambiguity this module exists to
remove.

Alpha is a plain unitless [0, 1] scalar. It carries no transfer function and is
never gamma transformed — decoding it would be a category error.

Linear is canonical because it is what Blender's Principled BSDF ``base_color``
expects, so no colour-space conversion happens at the Blender boundary — the same
reasoning that makes METRES and RADIANS canonical.

Scope and deliberate non-goals:
  - Pure arithmetic and one small table. No Blender, no ``bpy``, no I/O.
  - No prose parsing: :func:`resolve_named_color` matches a normalized NAME
    token, it does not read "a sort of warm sandy colour".
  - The named palette is a CONVENIENCE above the canonical numeric
    representation, for offline determinism and fallback interpretation. It is
    NOT a restriction: a real provider may propose any schema-valid explicit
    RGBA, which is why :func:`validate_material_color` exists independently of the
    palette and is the function the mutation path will actually depend on.

Canonical schema: ``material-color.schema.json``
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from studio_contracts import ChatError
from studio_types import MaterialColor

#: Channel names, in canonical order.
COLOR_CHANNELS: tuple[str, ...] = ("r", "g", "b", "a")

#: The three channels that carry a transfer function. Alpha is excluded on
#: purpose: it is a unitless coverage scalar, not a light intensity.
TRANSFER_CHANNELS: tuple[str, ...] = ("r", "g", "b")

# ---------------------------------------------------------------------------
# sRGB transfer function — the SINGLE conversion site
# ---------------------------------------------------------------------------

#: Piecewise threshold of the sRGB electro-optical transfer function, on the
#: ENCODED side.
SRGB_LINEAR_THRESHOLD = 0.04045

#: Slope of the near-black linear segment.
SRGB_LINEAR_SLOPE = 12.92

#: Offset and scale of the power segment.
SRGB_ALPHA = 0.055
SRGB_SCALE = 1.055

#: Exponent of the power segment.
SRGB_EXPONENT = 2.4

#: Maximum value of an 8-bit channel, for the ``#RRGGBB`` helper.
EIGHT_BIT_MAX = 255

COLOR_MESSAGES: dict[str, str] = {
    "NOT_A_COLOR": (
        "colour must be an object with finite r, g, b and a channels in [0, 1]"
    ),
    "MISSING_CHANNEL": "colour is missing one or more of the channels r, g, b, a",
    "NON_FINITE_CHANNEL": "colour channels must be finite numbers",
    "CHANNEL_OUT_OF_RANGE": "colour channels must be within [0, 1]",
    "UNKNOWN_COLOR_NAME": (
        "unknown colour name; the platform interprets only a small deterministic "
        "palette, and any other colour must be supplied as explicit numeric "
        "linear sRGB channels"
    ),
    "ENCODED_OUT_OF_RANGE": "encoded sRGB channel must be within [0, 1]",
}


def _is_real_number(value: Any) -> bool:
    """True for a real int/float, excluding booleans (``True`` is an ``int``)."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _normalize_zero(value: float) -> float:
    """Normalize negative zero to positive zero, as everywhere else."""
    return 0.0 if value == 0 else value


def srgb_encoded_channel_to_linear(encoded: Any) -> Optional[float]:
    """Decode ONE encoded sRGB channel in [0, 1] to linear light.

    The standard sRGB EOTF::

        e <= 0.04045 :  e / 12.92
        otherwise    :  ((e + 0.055) / 1.055) ** 2.4

    THIS IS THE ONLY sRGB transfer conversion in the repository. Dividing an
    8-bit value by 255 and calling the result linear is wrong by roughly a factor
    of two in the midtones (0.5 encoded is 0.214 linear, not 0.5), which is why
    the naive form is forbidden by the source guard rather than merely discouraged.

    Returns ``None`` for a non-finite or out-of-range input rather than clamping:
    silently clamping would turn a caller's mistake into a plausible colour.

    Note on cross-language parity: the power segment uses ``pow``, which is not
    guaranteed bit-identical across libm implementations. Observed identical
    between CPython and Node on the development machine, and the parity corpus
    compares transfer-function output within a documented tolerance rather than
    asserting bit equality. The PALETTE itself does not depend on this: it is
    authored in canonical linear literals (below), so palette lookups are exactly
    equal in both languages regardless of ``pow``.
    """
    if not _is_real_number(encoded) or not math.isfinite(encoded):
        return None
    if encoded < 0 or encoded > 1:
        return None
    if encoded <= SRGB_LINEAR_THRESHOLD:
        return encoded / SRGB_LINEAR_SLOPE
    return ((encoded + SRGB_ALPHA) / SRGB_SCALE) ** SRGB_EXPONENT


def linear_channel_to_srgb_encoded(linear: Any) -> Optional[float]:
    """Encode ONE linear-light channel in [0, 1] back to sRGB encoding.

    The exact inverse of :func:`srgb_encoded_channel_to_linear`. Provided for
    diagnostics and for the round-trip test that proves the pair really are
    inverses; nothing on the mutation path needs it, because Blender wants linear.
    """
    if not _is_real_number(linear) or not math.isfinite(linear):
        return None
    if linear < 0 or linear > 1:
        return None
    if linear <= SRGB_LINEAR_THRESHOLD / SRGB_LINEAR_SLOPE:
        return linear * SRGB_LINEAR_SLOPE
    return SRGB_SCALE * (linear ** (1 / SRGB_EXPONENT)) - SRGB_ALPHA


def srgb_8bit_to_linear(value: Any) -> Optional[float]:
    """Decode one 8-bit encoded channel (0-255) to linear light.

    A thin convenience over :func:`srgb_encoded_channel_to_linear` used to DOCUMENT
    and TEST the provenance of the palette below. It divides by 255 to reach the
    encoded [0, 1] domain and then applies the transfer function — the division
    alone is explicitly not the conversion.
    """
    if not _is_real_number(value) or not math.isfinite(value):
        return None
    return srgb_encoded_channel_to_linear(value / EIGHT_BIT_MAX)


# ---------------------------------------------------------------------------
# Named palette — small, closed, authored in CANONICAL LINEAR values
# ---------------------------------------------------------------------------

#: The encoded sRGB hex each palette entry was derived from. PROVENANCE ONLY:
#: these values are never used at runtime. Storing them makes the artistic choice
#: reviewable, and ``test_colors.py`` asserts that decoding them reproduces the
#: canonical linear literals below, so the two forms cannot drift apart.
#:
#: The canonical representation is the linear one; this is documentation with a
#: test attached, not a second source of truth.
PALETTE_PROVENANCE_HEX: dict[str, str] = {
    "warm beige": "#E6D2B5",
    "beige": "#F5F5DC",
    "white": "#FFFFFF",
    "black": "#000000",
    "grey": "#808080",
}

#: Deliberately TINY deterministic vocabulary. Not a colour-name database: it
#: exists so offline tests and the rule-based fallback have stable colours, and it
#: places no limit on what a real provider may propose numerically.
#:
#: Values are canonical LINEAR sRGB with alpha 1.0 (fully opaque), written as
#: literals so both languages parse identical doubles.
NAMED_COLORS: dict[str, MaterialColor] = {
    # #E6D2B5 — a warm, slightly deepened beige: clearly beige, clearly warmer
    # than CSS "beige", and stable forever because it is pinned by tests.
    "warm beige": MaterialColor(
        r=0.7912979403326302, g=0.6444796819705821, b=0.4620769996544071, a=1.0
    ),
    # #F5F5DC — CSS "beige", so the plain name matches the widely understood colour.
    "beige": MaterialColor(
        r=0.9130986517934192, g=0.9130986517934192, b=0.7156935005064807, a=1.0
    ),
    "white": MaterialColor(r=1.0, g=1.0, b=1.0, a=1.0),
    "black": MaterialColor(r=0.0, g=0.0, b=0.0, a=1.0),
    # #808080 — mid grey by ENCODING, which is 0.2159 linear, not 0.5. The gap is
    # the whole reason the transfer function is not optional.
    "grey": MaterialColor(
        r=0.21586050011389926, g=0.21586050011389926, b=0.21586050011389926, a=1.0
    ),
}

#: Spelling aliases only, never new colours. "off white" is deliberately absent:
#: it is a different colour claim, not another way to spell one of these.
COLOR_NAME_ALIASES: dict[str, str] = {"gray": "grey"}


@dataclass(frozen=True)
class ColorResult:
    """Result of a colour interpretation: a ``color`` or a structured ``error``."""

    ok: bool
    color: Optional[MaterialColor] = None
    error: Optional[ChatError] = None


def _invalid(message: str, code: str = "VALIDATION_ERROR") -> ColorResult:
    return ColorResult(ok=False, error=ChatError(code=code, message=message))


def normalize_color_name(name: Any) -> Optional[str]:
    """Normalize a colour NAME token: trim, lowercase, collapse inner whitespace.

    Token normalization, not prose parsing: "  Warm   Beige " resolves,
    "something warm and beige-ish" does not.
    """
    if not isinstance(name, str):
        return None
    collapsed = " ".join(name.split()).lower()
    if not collapsed:
        return None
    return COLOR_NAME_ALIASES.get(collapsed, collapsed)


def is_named_color(name: Any) -> bool:
    """True when ``name`` is in the deterministic palette."""
    normalized = normalize_color_name(name)
    return normalized is not None and normalized in NAMED_COLORS


def resolve_named_color(name: Any) -> ColorResult:
    """Resolve a palette name to its canonical linear sRGB colour.

    An unknown name is ``UNSUPPORTED_INSTRUCTION``, not ``VALIDATION_ERROR``: the
    request was well formed, the platform simply cannot interpret that phrase
    deterministically. Once a real provider exists it will usually have proposed
    explicit numbers already, and beige-like phrasing must NOT be forced back into
    this palette.
    """
    normalized = normalize_color_name(name)
    if normalized is None or normalized not in NAMED_COLORS:
        return _invalid(COLOR_MESSAGES["UNKNOWN_COLOR_NAME"], "UNSUPPORTED_INSTRUCTION")
    return ColorResult(ok=True, color=NAMED_COLORS[normalized])


def validate_material_color(value: Any) -> ColorResult:
    """Validate an ARBITRARY explicit canonical colour.

    This is the function the mutation path depends on, and it is deliberately
    independent of the palette: a real model must be free to propose any
    schema-valid explicit colour, and the platform must be able to accept it
    without that colour having a name.

    Accepts a mapping or a :class:`MaterialColor`. Rejects a missing channel, a
    non-numeric or non-finite channel, and any channel outside [0, 1]. Never
    clamps: a caller who sent 1.5 was wrong about the range, and quietly turning
    that into white would hide the mistake.
    """
    if isinstance(value, Mapping):
        channels = {key: value.get(key) for key in COLOR_CHANNELS}
        unknown = sorted(set(value) - set(COLOR_CHANNELS))
        if unknown:
            return _invalid(COLOR_MESSAGES["NOT_A_COLOR"])
    elif all(hasattr(value, key) for key in COLOR_CHANNELS):
        channels = {key: getattr(value, key) for key in COLOR_CHANNELS}
    else:
        return _invalid(COLOR_MESSAGES["NOT_A_COLOR"])

    if any(channels[key] is None for key in COLOR_CHANNELS):
        return _invalid(COLOR_MESSAGES["MISSING_CHANNEL"])
    for key in COLOR_CHANNELS:
        channel = channels[key]
        if not _is_real_number(channel) or not math.isfinite(channel):
            return _invalid(COLOR_MESSAGES["NON_FINITE_CHANNEL"])
    for key in COLOR_CHANNELS:
        channel = channels[key]
        if channel < 0 or channel > 1:
            return _invalid(COLOR_MESSAGES["CHANNEL_OUT_OF_RANGE"])

    return ColorResult(
        ok=True,
        color=MaterialColor(
            r=_normalize_zero(float(channels["r"])),
            g=_normalize_zero(float(channels["g"])),
            b=_normalize_zero(float(channels["b"])),
            a=_normalize_zero(float(channels["a"])),
        ),
    )


def color_from_srgb_8bit(
    r: Any, g: Any, b: Any, a: float = 1.0
) -> ColorResult:
    """Build a canonical colour from 8-bit ENCODED sRGB channels.

    The one supported way to bring a familiar ``#RRGGBB`` value into the canonical
    representation, so no caller is tempted to divide by 255 and stop there. Alpha
    is taken as an already-linear [0, 1] scalar and is NOT transfer-decoded.
    """
    decoded = [srgb_8bit_to_linear(channel) for channel in (r, g, b)]
    if any(channel is None for channel in decoded):
        return _invalid(COLOR_MESSAGES["ENCODED_OUT_OF_RANGE"])
    return validate_material_color(
        {"r": decoded[0], "g": decoded[1], "b": decoded[2], "a": a}
    )


__all__ = [
    "COLOR_CHANNELS",
    "COLOR_MESSAGES",
    "COLOR_NAME_ALIASES",
    "EIGHT_BIT_MAX",
    "NAMED_COLORS",
    "PALETTE_PROVENANCE_HEX",
    "SRGB_ALPHA",
    "SRGB_EXPONENT",
    "SRGB_LINEAR_SLOPE",
    "SRGB_LINEAR_THRESHOLD",
    "SRGB_SCALE",
    "TRANSFER_CHANNELS",
    "ColorResult",
    "color_from_srgb_8bit",
    "is_named_color",
    "linear_channel_to_srgb_encoded",
    "normalize_color_name",
    "resolve_named_color",
    "srgb_8bit_to_linear",
    "srgb_encoded_channel_to_linear",
    "validate_material_color",
]
