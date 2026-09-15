"""Angle conversion tests — Python (Spec 002, Task 2).

Radians are the one canonical internal angular unit. These tests pin:
  - the five required spec vectors, by EXACT equality against math.pi fractions,
    which is legitimate here because deg -> rad is a single multiplication by one
    constant (no transcendental evaluation at conversion time);
  - that conversion is NOT normalization: 360°, 720° and 405° keep their excess;
  - the closed token-alias set, and that anything outside it is refused;
  - non-finite and non-numeric refusals, with fixed messages so the parity test
    can compare them byte for byte;
  - purity: no Blender, no I/O, no environment, no scene state.

The shared corpus (../spatial-cases.json) drives the same cases in TypeScript.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from spatial_test_support import decode_value, load_spatial_cases
from studio_spatial import (
    ANGLE_MESSAGES,
    ANGLE_UNIT_ALIASES,
    DEGREES_PER_HALF_TURN,
    RADIANS_PER_DEGREE,
    convert_to_radians,
    degrees_to_radians,
    is_finite_angle,
    normalize_angle_unit,
    radians_to_degrees,
    radians_to_radians,
    to_radians,
)
from studio_types import ANGLE_UNITS, AngleMeasurement

CASES = load_spatial_cases()
MODULE = Path(__file__).resolve().parent / "studio_spatial" / "angles.py"


# ---------------------------------------------------------------------------
# The required spec vectors, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "degrees,expected",
    [
        (0, 0.0),
        (45, math.pi / 4),
        (90, math.pi / 2),
        (180, math.pi),
        (-45, -math.pi / 4),
    ],
)
def test_spec_degree_vectors_are_exact(degrees, expected):
    """Exact equality is correct here: one multiplication by pi/180, so the result
    is a single rounding away from the true value and identical in both languages."""
    result = degrees_to_radians(degrees)
    assert result.ok, result.error
    assert result.radians == expected


def test_spec_one_radian_passes_through():
    result = radians_to_radians(1)
    assert result.ok
    assert result.radians == 1.0


def test_the_conversion_constant_is_a_single_precomputed_value():
    assert RADIANS_PER_DEGREE == math.pi / DEGREES_PER_HALF_TURN
    assert DEGREES_PER_HALF_TURN == 180


def test_conversion_agrees_with_the_reference_implementation():
    """math.radians is the reference, but is deliberately not used in the module:
    it has no JavaScript counterpart, so parity would rest on a C library detail."""
    for degrees in (0, 1, 22.5, 45, 90, 180, 270, 360, -45, -720, 1e6):
        assert degrees_to_radians(degrees).radians == pytest.approx(
            math.radians(degrees), rel=1e-15, abs=1e-15
        )


# ---------------------------------------------------------------------------
# Conversion is not normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("degrees", [360, 720, 405, -360, 1080])
def test_full_turns_are_never_collapsed(degrees):
    """720 degrees is a legitimate two-turn instruction. Reducing it modulo 2pi
    would silently destroy intent, so nothing here does that."""
    result = degrees_to_radians(degrees)
    assert result.ok
    assert result.radians == degrees * RADIANS_PER_DEGREE
    # The give-away of a hidden normalization would be a result inside (-2pi, 2pi)
    # for an input that is a whole turn or more.
    if abs(degrees) >= 360:
        assert abs(result.radians) >= 2 * math.pi - 1e-12


def test_a_full_turn_is_distinguishable_from_zero():
    assert degrees_to_radians(360).radians != degrees_to_radians(0).radians


def test_module_contains_no_modulo_normalization():
    """A future 'principal value' helper must be asked for explicitly, not applied
    behind a caller's back."""
    source = MODULE.read_text("utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            pytest.fail("angles.py performs modulo arithmetic")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("fmod", "remainder"), "modulo helper used"


# ---------------------------------------------------------------------------
# Negative zero
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["deg", "rad"])
def test_negative_zero_is_normalized(unit):
    result = convert_to_radians(-0.0, unit)
    assert result.ok
    assert result.radians == 0.0
    assert math.copysign(1.0, result.radians) > 0


# ---------------------------------------------------------------------------
# Unit tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [
        ("deg", "deg"),
        ("DEG", "deg"),
        ("  deg ", "deg"),
        ("degree", "deg"),
        ("degrees", "deg"),
        ("Degrees", "deg"),
        ("°", "deg"),
        ("rad", "rad"),
        ("radian", "rad"),
        ("radians", "rad"),
        ("RADIANS", "rad"),
    ],
)
def test_accepted_unit_tokens_normalize(token, expected):
    assert normalize_angle_unit(token) == expected


@pytest.mark.parametrize(
    "token", ["grad", "gradian", "turn", "arcmin", "cm", "m", "", "  ", None, 180, "d"]
)
def test_unrecognised_unit_tokens_are_refused(token):
    assert normalize_angle_unit(token) is None
    result = convert_to_radians(45, token)
    assert not result.ok
    assert result.error.code == "INVALID_UNITS"
    assert result.error.message == ANGLE_MESSAGES["UNSUPPORTED_UNIT"]


def test_the_alias_table_only_maps_to_canonical_values():
    assert set(ANGLE_UNIT_ALIASES.values()) == set(ANGLE_UNITS)


def test_unknown_unit_beats_invalid_value():
    """An unknown unit makes the angle uninterpretable, so the value is not even
    examined — the same precedence units.py uses."""
    result = convert_to_radians(float("nan"), "grad")
    assert result.error.message == ANGLE_MESSAGES["UNSUPPORTED_UNIT"]


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), "45", None, True, [45], {}]
)
def test_non_finite_and_non_numeric_values_are_refused(value):
    assert not is_finite_angle(value)
    for unit in ("deg", "rad"):
        result = convert_to_radians(value, unit)
        assert not result.ok
        assert result.error.code == "INVALID_UNITS"
        assert result.error.message == ANGLE_MESSAGES["NON_FINITE_VALUE"]


def test_booleans_are_not_numbers():
    """True is an int in Python; accepting it would silently mean 1 degree."""
    assert not is_finite_angle(True)
    assert not is_finite_angle(False)


@pytest.mark.parametrize("measurement", [None, 45, "45 deg", [45, "deg"], object()])
def test_malformed_measurements_are_refused(measurement):
    result = to_radians(measurement)
    assert not result.ok
    assert result.error.message == ANGLE_MESSAGES["NOT_A_MEASUREMENT"]


def test_to_radians_accepts_a_mapping():
    result = to_radians({"value": 90, "unit": "deg"})
    assert result.ok
    assert result.radians == math.pi / 2


def test_to_radians_accepts_the_canonical_dataclass():
    assert to_radians(AngleMeasurement(value=45, unit="deg")).radians == math.pi / 4


# ---------------------------------------------------------------------------
# Diagnostics inverse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("degrees", [0, 45, 90, 180, -45, 123.456])
def test_radians_to_degrees_round_trips(degrees):
    radians = degrees_to_radians(degrees).radians
    assert radians_to_degrees(radians) == pytest.approx(degrees, rel=1e-12, abs=1e-12)


def test_radians_to_degrees_refuses_non_finite():
    assert radians_to_degrees(float("nan")) is None
    assert radians_to_degrees("1") is None


# ---------------------------------------------------------------------------
# Purity
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
        "sys",
        "subprocess",
        "pathlib",
        "requests",
        "httpx",
        "socket",
        "random",
        "time",
        "datetime",
        "studio_agent",
        "studio_api",
        "blender_mcp",
        "blender_worker",
    ):
        assert forbidden not in modules, f"impure import: {forbidden}"


def test_conversion_is_deterministic():
    first = [degrees_to_radians(d).radians for d in range(-360, 361)]
    for _ in range(5):
        assert [degrees_to_radians(d).radians for d in range(-360, 361)] == first


# ---------------------------------------------------------------------------
# Shared corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case", CASES["angles"]["valid"], ids=lambda c: c["name"]
)
def test_corpus_valid_angles(case):
    result = to_radians(
        {"value": decode_value(case["value"]), "unit": case["unit"]}
    )
    assert result.ok, result.error
    assert result.radians == case["radians"]


@pytest.mark.parametrize(
    "case", CASES["angles"]["invalid"], ids=lambda c: c["name"]
)
def test_corpus_invalid_angles(case):
    result = to_radians(
        {"value": decode_value(case["value"]), "unit": case["unit"]}
    )
    assert not result.ok
    assert result.error.code == case["error_code"]
    assert result.error.message == ANGLE_MESSAGES[case["error_key"]]


@pytest.mark.parametrize(
    "case", CASES["angles"]["malformed"], ids=lambda c: c["name"]
)
def test_corpus_malformed_angles(case):
    result = to_radians(decode_value(case["measurement"]))
    assert not result.ok
    assert result.error.code == case["error_code"]
    assert result.error.message == ANGLE_MESSAGES[case["error_key"]]
