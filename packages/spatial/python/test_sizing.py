"""Resize-factor tests — Python (Spec 002, Task 2).

These tests pin the boundary decisions, which are the whole point of the module:

  - "100% smaller" is REFUSED. It is not a zero-size object, it is a deletion, and
    letting it through would produce a valid-looking design operation that
    collapses geometry.
  - ">100% smaller" is REFUSED rather than inverting the object.
  - "-20% smaller" is REFUSED rather than read as "20% larger": the direction field
    already carries the sign, exactly as Spec 001 refuses `left` with `-25 cm`.
  - "larger" has NO invented ceiling, because no product requirement caps growth.
  - a semantic size factor is strictly positive, which is deliberately stricter
    than the constraint on observed `SceneObject.scale`.

The shared corpus (../spatial-cases.json) drives the same cases in TypeScript.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from spatial_test_support import decode_value, load_spatial_cases
from studio_spatial import (
    MAX_REDUCTION_PERCENT,
    PERCENT_WHOLE,
    SIZE_DIRECTIONS,
    SIZING_MESSAGES,
    is_size_direction,
    is_valid_size_factor,
    resize_factor,
    validate_size_factor,
)

CASES = load_spatial_cases()
MODULE = Path(__file__).resolve().parent / "studio_spatial" / "sizing.py"


# ---------------------------------------------------------------------------
# The required spec vectors, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "percent,direction,expected",
    [
        (0, "smaller", 1.0),
        (0, "larger", 1.0),
        (20, "smaller", 0.8),
        (25, "larger", 1.25),
        (99, "smaller", 0.01),
    ],
)
def test_spec_resize_vectors_are_exact(percent, direction, expected):
    result = resize_factor(percent, direction)
    assert result.ok, result.error
    assert result.factor == expected


def test_twenty_percent_smaller_is_the_documented_worked_example():
    """The design's worked example: 2 m dimensions x 0.8 -> 1.6 m. The factor is all
    this layer produces; the multiplication belongs to Task 10, with authoritative
    snapshot dimensions."""
    factor = resize_factor(20, "smaller").factor
    assert factor == 0.8
    assert 2.0 * factor == 1.6


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("percent", [100, 100.0, 100.1, 120, 1000])
def test_total_or_excess_reduction_is_refused(percent):
    result = resize_factor(percent, "smaller")
    assert not result.ok
    assert result.error.code == "VALIDATION_ERROR"
    assert result.error.message == SIZING_MESSAGES["TOTAL_REDUCTION"]


def test_just_under_total_reduction_is_allowed_and_still_positive():
    result = resize_factor(99.999, "smaller")
    assert result.ok
    assert result.factor > 0


@pytest.mark.parametrize("percent", [-0.0001, -1, -20, -100, -1e9])
@pytest.mark.parametrize("direction", ["smaller", "larger"])
def test_negative_percent_is_refused_not_reinterpreted(percent, direction):
    """"-20% smaller" must not become "20% larger": direction is already carried by
    the direction field, so a negative percent would encode it twice."""
    result = resize_factor(percent, direction)
    assert not result.ok
    assert result.error.code == "VALIDATION_ERROR"
    assert result.error.message == SIZING_MESSAGES["NEGATIVE_PERCENT"]


def test_negative_zero_percent_is_accepted_as_zero():
    """-0.0 is numerically zero, not negative; refusing it would be pedantry."""
    result = resize_factor(-0.0, "smaller")
    assert result.ok
    assert result.factor == 1.0


@pytest.mark.parametrize("percent", [100, 200, 1000, 10000, 1e6])
def test_growth_has_no_invented_ceiling(percent):
    result = resize_factor(percent, "larger")
    assert result.ok
    assert result.factor > 1


# ---------------------------------------------------------------------------
# Direction vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "direction",
    ["bigger", "Smaller", "SMALLER", "left", "up", "", "  ", None, 1, True, ["smaller"]],
)
def test_unsupported_directions_are_refused(direction):
    assert not is_size_direction(direction)
    result = resize_factor(20, direction)
    assert not result.ok
    assert result.error.message == SIZING_MESSAGES["UNSUPPORTED_DIRECTION"]


def test_direction_is_checked_before_the_percent():
    """An unknown direction makes the request uninterpretable, so the percent is not
    examined — the same precedence units.py applies to an unknown unit."""
    result = resize_factor(float("nan"), "bigger")
    assert result.error.message == SIZING_MESSAGES["UNSUPPORTED_DIRECTION"]


def test_the_direction_set_is_exactly_two_tokens():
    assert SIZE_DIRECTIONS == ("smaller", "larger")


# ---------------------------------------------------------------------------
# Percent validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "percent", [float("nan"), float("inf"), float("-inf"), "20", None, True, [20], {}]
)
def test_non_finite_and_non_numeric_percents_are_refused(percent):
    result = resize_factor(percent, "smaller")
    assert not result.ok
    assert result.error.code == "INVALID_UNITS"
    assert result.error.message == SIZING_MESSAGES["NON_FINITE_PERCENT"]


# ---------------------------------------------------------------------------
# The semantic size factor is stricter than SceneObject.scale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1.0, 0.8, 1e-9, 1000.0, 1])
def test_valid_size_factors(value):
    assert is_valid_size_factor(value)
    result = validate_size_factor(value)
    assert result.ok
    assert result.factor == float(value)


@pytest.mark.parametrize("value", [0, -0.0, -0.8, -1])
def test_zero_and_negative_factors_are_refused(value):
    assert not is_valid_size_factor(value)
    result = validate_size_factor(value)
    assert not result.ok
    assert result.error.code == "VALIDATION_ERROR"
    assert result.error.message == SIZING_MESSAGES["NON_POSITIVE_FACTOR"]


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), "0.8", None, True]
)
def test_non_finite_factors_are_refused(value):
    assert not is_valid_size_factor(value)
    result = validate_size_factor(value)
    assert not result.ok
    assert result.error.code == "INVALID_UNITS"
    assert result.error.message == SIZING_MESSAGES["NON_FINITE_FACTOR"]


def test_a_negative_scene_scale_is_not_a_valid_resize_factor():
    """Blender permits a mirrored object, so SceneObject.scale may be negative and
    the scene contract does not forbid it. A resize FACTOR may not be: describing
    reality is not the same as endorsing it as an operation."""
    assert not is_valid_size_factor(-1.0)


# ---------------------------------------------------------------------------
# Purity and single-site arithmetic
# ---------------------------------------------------------------------------


def test_percentage_arithmetic_uses_the_named_constant():
    source = MODULE.read_text("utf-8")
    tree = ast.parse(source)
    literal_100s = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.right, ast.Constant)
        and node.right.value == 100
    ]
    assert not literal_100s, "use PERCENT_WHOLE rather than a bare 100 in arithmetic"
    assert PERCENT_WHOLE == 100
    assert MAX_REDUCTION_PERCENT == 100


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


def test_module_never_sees_scene_state():
    """The factor is scene-independent by construction: no snapshot type is even
    referenced in CODE here.

    Checked over the AST rather than the raw text: the module docstring legitimately
    mentions SceneSnapshot to explain what it does *not* do, and a text scan would
    fail on its own documentation — exactly the brittleness worth avoiding.
    """
    tree = ast.parse(MODULE.read_text("utf-8"))
    identifiers = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    for forbidden in (
        "SceneSnapshot",
        "SceneObject",
        "dimensions_meters",
        "scene_version",
    ):
        assert forbidden not in identifiers, f"sizing.py uses {forbidden}"


def test_resize_is_deterministic():
    percents = [i / 4 for i in range(0, 400)]
    first = [resize_factor(p, "smaller").factor for p in percents]
    for _ in range(5):
        assert [resize_factor(p, "smaller").factor for p in percents] == first


def test_factor_is_never_zero_for_any_accepted_reduction():
    """The semantic guarantee, checked across the whole accepted domain rather than
    at a couple of sample points."""
    for i in range(0, 100000):
        percent = i / 1000
        if percent >= MAX_REDUCTION_PERCENT:
            continue
        result = resize_factor(percent, "smaller")
        assert result.ok and result.factor > 0, percent
        assert math.isfinite(result.factor)


# ---------------------------------------------------------------------------
# Shared corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES["resize"]["valid"], ids=lambda c: c["name"])
def test_corpus_valid_resizes(case):
    result = resize_factor(decode_value(case["percent"]), case["direction"])
    assert result.ok, result.error
    assert result.factor == case["factor"]


@pytest.mark.parametrize("case", CASES["resize"]["invalid"], ids=lambda c: c["name"])
def test_corpus_invalid_resizes(case):
    result = resize_factor(decode_value(case["percent"]), case["direction"])
    assert not result.ok
    assert result.error.code == case["error_code"]
    assert result.error.message == SIZING_MESSAGES[case["error_key"]]


@pytest.mark.parametrize(
    "case", CASES["resize"]["factors"]["valid"], ids=lambda c: c["name"]
)
def test_corpus_valid_factors(case):
    result = validate_size_factor(decode_value(case["value"]))
    assert result.ok, result.error
    assert result.factor == case["factor"]


@pytest.mark.parametrize(
    "case", CASES["resize"]["factors"]["invalid"], ids=lambda c: c["name"]
)
def test_corpus_invalid_factors(case):
    result = validate_size_factor(decode_value(case["value"]))
    assert not result.ok
    assert result.error.code == case["error_code"]
    assert result.error.message == SIZING_MESSAGES[case["error_key"]]
