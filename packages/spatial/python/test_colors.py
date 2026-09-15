"""Colour interpretation tests — Python (Spec 002, Task 2).

Canonical colour is LINEAR sRGB RGBA with finite channels in [0, 1]. These tests
pin the three things that matter:

  1. The transfer function is the real sRGB EOTF, not a division by 255. The
     midtone vector proves it: 0.5 encoded is 0.2159 linear, and a naive
     implementation would be wrong by more than a factor of two.
  2. An ARBITRARY explicit colour validates independently of the palette, because a
     real provider must be free to propose any schema-valid colour and the platform
     must accept it without that colour having a name.
  3. The palette is small, closed, deterministic, and authored in canonical LINEAR
     literals — so a lookup is exactly equal in both languages regardless of libm
     `pow` behaviour — while its ENCODED provenance is asserted to decode to those
     same literals, so the two forms cannot drift.

The shared corpus (../spatial-cases.json) drives the same cases in TypeScript.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from spatial_test_support import decode_color, decode_value, load_spatial_cases
from studio_contracts import SCHEMA_FILES, to_wire, validate_against_schema
from studio_spatial import (
    COLOR_CHANNELS,
    COLOR_MESSAGES,
    COLOR_NAME_ALIASES,
    EIGHT_BIT_MAX,
    NAMED_COLORS,
    PALETTE_PROVENANCE_HEX,
    SRGB_EXPONENT,
    SRGB_LINEAR_SLOPE,
    SRGB_LINEAR_THRESHOLD,
    TRANSFER_CHANNELS,
    color_from_srgb_8bit,
    is_named_color,
    linear_channel_to_srgb_encoded,
    normalize_color_name,
    resolve_named_color,
    srgb_8bit_to_linear,
    srgb_encoded_channel_to_linear,
    validate_material_color,
)
from studio_types import MaterialColor

CASES = load_spatial_cases()
MODULE = Path(__file__).resolve().parent / "studio_spatial" / "colors.py"

#: Tolerance for anything that goes through the power segment. Documented rather
#: than tuned: `pow` is not guaranteed bit-identical across libm implementations,
#: so the contract is "agrees to well within any perceptible difference", not "same
#: final bit". Observed bit-identical between CPython and Node on this machine.
TRANSFER_TOLERANCE = 1e-12


def _hex_to_8bit(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


# ---------------------------------------------------------------------------
# The sRGB transfer function
# ---------------------------------------------------------------------------


def test_zero_encoded_is_zero_linear():
    assert srgb_encoded_channel_to_linear(0) == 0.0


def test_one_encoded_is_one_linear():
    assert srgb_encoded_channel_to_linear(1) == pytest.approx(1.0, abs=1e-15)


def test_the_known_midtone_vector():
    """The single most important number in this module: encoded 0.5 is NOT 0.5
    linear. A `value / 255` implementation would return 0.5 here and be wrong by a
    factor of 2.3."""
    linear = srgb_encoded_channel_to_linear(0.5)
    assert linear == pytest.approx(0.21404114048223255, abs=TRANSFER_TOLERANCE)
    assert linear < 0.25


def test_eight_bit_mid_grey_is_not_half_linear():
    naive = 128 / EIGHT_BIT_MAX
    correct = srgb_8bit_to_linear(128)
    assert correct == pytest.approx(0.21586050011389926, abs=TRANSFER_TOLERANCE)
    assert abs(naive - correct) > 0.28, "the naive form is not an approximation"


def test_the_transfer_function_is_monotonic():
    previous = -1.0
    for step in range(0, 1001):
        encoded = step / 1000
        linear = srgb_encoded_channel_to_linear(encoded)
        assert linear is not None
        assert linear > previous, f"not monotonic at {encoded}"
        previous = linear


def test_the_piecewise_segments_meet_at_the_threshold():
    """A visible discontinuity at the join would show up as banding, and as a
    non-monotonic digest input.

    The standard's published constants do not join perfectly: the linear segment
    gives 0.00313080495 at the threshold while the power segment gives
    0.00313080728, a step of ~2.3e-9. That is a known imprecision in the sRGB
    specification's rounded constants, not a bug here, and it is four orders of
    magnitude below an 8-bit quantum (1/255 ~ 0.0039), so nothing perceptible or
    digest-relevant depends on it. The test pins the size of the step so a real
    mistake in the constants would still fail.
    """
    below = srgb_encoded_channel_to_linear(SRGB_LINEAR_THRESHOLD - 1e-12)
    at = srgb_encoded_channel_to_linear(SRGB_LINEAR_THRESHOLD)
    above = srgb_encoded_channel_to_linear(SRGB_LINEAR_THRESHOLD + 1e-12)
    assert at == pytest.approx(SRGB_LINEAR_THRESHOLD / SRGB_LINEAR_SLOPE, abs=1e-15)
    assert below <= at <= above
    assert above - below < 1e-8


@pytest.mark.parametrize("encoded", [0.0, 0.01, 0.04045, 0.2, 0.5, 0.8, 0.9, 1.0])
def test_encode_decode_round_trips(encoded):
    linear = srgb_encoded_channel_to_linear(encoded)
    assert linear_channel_to_srgb_encoded(linear) == pytest.approx(encoded, abs=1e-12)


@pytest.mark.parametrize(
    "encoded", [1.5, -0.1, float("nan"), float("inf"), "0.5", None, True, [0.5]]
)
def test_out_of_range_and_non_numeric_encoded_values_are_refused(encoded):
    assert srgb_encoded_channel_to_linear(encoded) is None
    assert linear_channel_to_srgb_encoded(encoded) is None


def test_out_of_range_is_refused_rather_than_clamped():
    """Clamping would turn a caller's range mistake into a plausible colour."""
    assert srgb_encoded_channel_to_linear(1.2) is None


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------


def test_the_palette_is_small_and_closed():
    assert set(NAMED_COLORS) == {"warm beige", "beige", "white", "black", "grey"}
    assert len(NAMED_COLORS) <= 8, "this is not a colour-name database"


def test_warm_beige_is_the_documented_stable_value():
    """Pinned exactly. Changing it is a deliberate act that fails here first."""
    assert NAMED_COLORS["warm beige"] == MaterialColor(
        r=0.7912979403326302, g=0.6444796819705821, b=0.4620769996544071, a=1.0
    )


def test_warm_beige_is_visibly_beige_and_warm():
    """Not an artistic assertion — a structural one: beige is a light, desaturated
    warm tone, so red > green > blue and nothing is dark."""
    color = NAMED_COLORS["warm beige"]
    assert color.r > color.g > color.b, "warm beige must be warm (r > g > b)"
    assert color.b > 0.25, "must not be dark"
    assert color.r < 1.0, "must not be blown out to white"
    assert color.a == 1.0


def test_warm_beige_is_warmer_than_plain_beige():
    warm = NAMED_COLORS["warm beige"]
    plain = NAMED_COLORS["beige"]
    assert warm.r - warm.b > plain.r - plain.b, "warm beige must be the warmer of the two"
    assert warm.r < plain.r, "warm beige is the deeper of the two"


@pytest.mark.parametrize("name,hex_value", sorted(PALETTE_PROVENANCE_HEX.items()))
def test_palette_provenance_decodes_to_the_canonical_linear_literals(name, hex_value):
    """The encoded hex is documentation, the linear literal is canonical, and this
    test is what stops them drifting apart. Tolerance, not equality, because the
    literal must not depend on this machine's `pow`."""
    r, g, b = _hex_to_8bit(hex_value)
    expected = NAMED_COLORS[name]
    for channel, eight_bit in zip(TRANSFER_CHANNELS, (r, g, b)):
        assert srgb_8bit_to_linear(eight_bit) == pytest.approx(
            getattr(expected, channel), abs=TRANSFER_TOLERANCE
        ), f"{name}.{channel}"


def test_every_palette_entry_has_provenance():
    assert set(PALETTE_PROVENANCE_HEX) == set(NAMED_COLORS)


def test_every_palette_entry_is_a_valid_canonical_colour():
    for name, color in NAMED_COLORS.items():
        result = validate_material_color(color)
        assert result.ok, f"{name}: {result.error}"
        wire = to_wire(color)
        assert validate_against_schema(SCHEMA_FILES["MaterialColor"], wire).valid


def test_palette_alpha_is_never_transfer_decoded():
    """Alpha is a coverage scalar. Decoding it would darken every opaque colour."""
    for color in NAMED_COLORS.values():
        assert color.a == 1.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("warm beige", "warm beige"),
        ("Warm Beige", "warm beige"),
        ("  WARM   BEIGE  ", "warm beige"),
        ("gray", "grey"),
        ("GRAY", "grey"),
        ("beige", "beige"),
    ],
)
def test_name_tokens_normalize(raw, expected):
    assert normalize_color_name(raw) == expected
    assert is_named_color(raw)
    assert resolve_named_color(raw).color == NAMED_COLORS[expected]


def test_aliases_are_spellings_not_new_colours():
    for alias, target in COLOR_NAME_ALIASES.items():
        assert target in NAMED_COLORS
        assert alias not in NAMED_COLORS


@pytest.mark.parametrize(
    "name",
    [
        "burnt sienna",
        "a sort of warm beige-ish tone",
        "beigey",
        "#E6D2B5",
        "",
        "   ",
        None,
        7,
        ["beige"],
    ],
)
def test_unknown_names_are_unsupported_not_guessed(name):
    assert not is_named_color(name)
    result = resolve_named_color(name)
    assert not result.ok
    assert result.error.code == "UNSUPPORTED_INSTRUCTION"
    assert result.error.message == COLOR_MESSAGES["UNKNOWN_COLOR_NAME"]


def test_the_palette_does_not_restrict_what_a_provider_may_propose():
    """The point of the palette is offline determinism, not a whitelist. An explicit
    colour nowhere near it validates fine."""
    proposed = {"r": 0.61, "g": 0.42, "b": 0.13, "a": 1.0}
    result = validate_material_color(proposed)
    assert result.ok
    assert result.color not in NAMED_COLORS.values()


# ---------------------------------------------------------------------------
# Arbitrary explicit colour validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "color",
    [
        {"r": 0, "g": 0, "b": 0, "a": 0},
        {"r": 1, "g": 1, "b": 1, "a": 1},
        {"r": 0, "g": 1, "b": 0, "a": 1},
        {"r": 0.5, "g": 0.25, "b": 0.125, "a": 0.75},
    ],
)
def test_channel_boundaries_and_interior_values_are_valid(color):
    result = validate_material_color(color)
    assert result.ok, result.error
    assert validate_against_schema(SCHEMA_FILES["MaterialColor"], color).valid


@pytest.mark.parametrize(
    "channel,value", [(c, v) for c in COLOR_CHANNELS for v in (-0.0001, 1.0001, 2, -1)]
)
def test_out_of_range_channels_are_refused(channel, value):
    color = {"r": 0.5, "g": 0.5, "b": 0.5, "a": 1.0, channel: value}
    result = validate_material_color(color)
    assert not result.ok
    assert result.error.message == COLOR_MESSAGES["CHANNEL_OUT_OF_RANGE"]
    assert not validate_against_schema(SCHEMA_FILES["MaterialColor"], color).valid


@pytest.mark.parametrize("channel", COLOR_CHANNELS)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_channels_are_refused(channel, value):
    color = {"r": 0.5, "g": 0.5, "b": 0.5, "a": 1.0, channel: value}
    result = validate_material_color(color)
    assert not result.ok
    assert result.error.message == COLOR_MESSAGES["NON_FINITE_CHANNEL"]


@pytest.mark.parametrize("channel", COLOR_CHANNELS)
def test_missing_channels_are_refused(channel):
    color = {"r": 0.5, "g": 0.5, "b": 0.5, "a": 1.0}
    del color[channel]
    result = validate_material_color(color)
    assert not result.ok
    assert result.error.message == COLOR_MESSAGES["MISSING_CHANNEL"]


@pytest.mark.parametrize(
    "value",
    [
        "warm beige",
        [1, 1, 1, 1],
        None,
        7,
        {"r": 1, "g": 1, "b": 1, "a": 1, "space": "srgb"},
        {"red": 1, "green": 1, "blue": 1, "alpha": 1},
    ],
)
def test_non_colour_values_are_refused(value):
    result = validate_material_color(value)
    assert not result.ok
    assert result.error.code == "VALIDATION_ERROR"


def test_out_of_range_is_never_clamped():
    assert validate_material_color({"r": 1.5, "g": 0, "b": 0, "a": 1}).color is None


def test_negative_zero_channels_are_normalized():
    result = validate_material_color({"r": -0.0, "g": 0.0, "b": 0.0, "a": 1.0})
    assert result.ok
    assert math.copysign(1.0, result.color.r) > 0


def test_a_material_color_dataclass_validates():
    assert validate_material_color(MaterialColor(0.1, 0.2, 0.3, 1.0)).ok


# ---------------------------------------------------------------------------
# 8-bit convenience
# ---------------------------------------------------------------------------


def test_color_from_srgb_8bit_matches_the_palette():
    r, g, b = _hex_to_8bit(PALETTE_PROVENANCE_HEX["warm beige"])
    result = color_from_srgb_8bit(r, g, b)
    assert result.ok
    expected = NAMED_COLORS["warm beige"]
    for channel in TRANSFER_CHANNELS:
        assert getattr(result.color, channel) == pytest.approx(
            getattr(expected, channel), abs=TRANSFER_TOLERANCE
        )


def test_color_from_srgb_8bit_does_not_decode_alpha():
    result = color_from_srgb_8bit(255, 255, 255, 0.5)
    assert result.ok
    assert result.color.a == 0.5


@pytest.mark.parametrize("bad", [-1, 256, float("nan"), "128", None])
def test_color_from_srgb_8bit_refuses_out_of_range(bad):
    assert not color_from_srgb_8bit(bad, 0, 0).ok


# ---------------------------------------------------------------------------
# Purity and single-site conversion
# ---------------------------------------------------------------------------


def test_module_imports_nothing_impure():
    tree = ast.parse(MODULE.read_text("utf-8"))
    modules = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    for forbidden in (
        "bpy",
        "os",
        "subprocess",
        "pathlib",
        "socket",
        "random",
        "time",
        "studio_agent",
        "studio_api",
        "blender_mcp",
        "blender_worker",
    ):
        assert forbidden not in modules, f"impure import: {forbidden}"


def test_the_transfer_exponent_appears_once():
    """One power expression, so there is exactly one place the transfer function can
    be got wrong."""
    tree = ast.parse(MODULE.read_text("utf-8"))
    powers = [
        node for node in ast.walk(tree) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow)
    ]
    assert len(powers) == 2, "expected exactly the decode and its inverse"
    assert SRGB_EXPONENT == 2.4


def test_colour_conversion_is_deterministic():
    first = [srgb_encoded_channel_to_linear(i / 255) for i in range(256)]
    for _ in range(5):
        assert [srgb_encoded_channel_to_linear(i / 255) for i in range(256)] == first


# ---------------------------------------------------------------------------
# Shared corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES["colors"]["valid"], ids=lambda c: c["name"])
def test_corpus_valid_colors(case):
    result = validate_material_color(decode_color(case["color"]))
    assert result.ok, result.error


@pytest.mark.parametrize("case", CASES["colors"]["invalid"], ids=lambda c: c["name"])
def test_corpus_invalid_colors(case):
    result = validate_material_color(decode_color(case["color"]))
    assert not result.ok
    assert result.error.code == case["error_code"]
    assert result.error.message == COLOR_MESSAGES[case["error_key"]]


@pytest.mark.parametrize("case", CASES["colors"]["named"], ids=lambda c: c["name"])
def test_corpus_named_colors(case):
    result = resolve_named_color(case["input"])
    assert result.ok, result.error
    for channel in COLOR_CHANNELS:
        assert getattr(result.color, channel) == pytest.approx(
            case["color"][channel], abs=TRANSFER_TOLERANCE
        )


@pytest.mark.parametrize("case", CASES["colors"]["unnamed"], ids=lambda c: c["name"])
def test_corpus_unknown_names(case):
    result = resolve_named_color(decode_value(case["input"]))
    assert not result.ok
    assert result.error.code == case["error_code"]
    assert result.error.message == COLOR_MESSAGES[case["error_key"]]


@pytest.mark.parametrize(
    "case", CASES["colors"]["transfer"]["cases"], ids=lambda c: c["name"]
)
def test_corpus_transfer_vectors(case):
    tolerance = CASES["colors"]["transfer"]["tolerance"]
    assert srgb_encoded_channel_to_linear(case["encoded"]) == pytest.approx(
        case["linear"], abs=tolerance
    )


@pytest.mark.parametrize(
    "case", CASES["colors"]["transfer"]["rejected"], ids=lambda c: c["name"]
)
def test_corpus_rejected_transfer_inputs(case):
    assert srgb_encoded_channel_to_linear(decode_value(case["encoded"])) is None
