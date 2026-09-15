"""Direction mapping tests — Python (Spec 001, Task 2).

Covers the required mapping (right -> axis X, positive), totality over the
canonical Direction enum, independence from Blender, and the shared corpus.
_Requirements: 3.3_
"""

from __future__ import annotations

import math
import sys

import pytest
from spatial_test_support import decode_value, load_spatial_cases
from studio_spatial import (
    DIRECTION_MESSAGES,
    DIRECTION_TO_AXIS,
    direction_delta_meters,
    is_direction,
    resolve_direction,
    to_meters,
    zero_delta,
)
from studio_types import AXES, DIRECTIONS, AxisDirection, Vec3

CASES = load_spatial_cases()


# ---------------------------------------------------------------------------
# Required mapping from Spec 001
# ---------------------------------------------------------------------------


def test_required_right_maps_to_positive_x():
    result = resolve_direction("right")
    assert result.ok is True
    assert result.axis_direction.axis == "x"
    assert result.axis_direction.sign == 1


def test_required_right_produces_positive_x_delta():
    result = direction_delta_meters("right", {"value": 50, "unit": "cm"})
    assert result.ok is True
    assert result.delta == Vec3(0.5, 0.0, 0.0)
    assert result.axis == "x"
    assert result.meters == 0.5


def test_world_space_table_is_exactly_as_specified():
    assert dict(DIRECTION_TO_AXIS) == {
        "right": AxisDirection(axis="x", sign=1),
        "left": AxisDirection(axis="x", sign=-1),
        "forward": AxisDirection(axis="y", sign=1),
        "back": AxisDirection(axis="y", sign=-1),
        "up": AxisDirection(axis="z", sign=1),
        "down": AxisDirection(axis="z", sign=-1),
    }


# ---------------------------------------------------------------------------
# Totality and structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_every_canonical_direction_resolves(direction):
    result = resolve_direction(direction)
    assert result.ok is True
    assert result.axis_direction.axis in AXES
    assert result.axis_direction.sign in (1, -1)


@pytest.mark.parametrize(
    "a,b", [("right", "left"), ("forward", "back"), ("up", "down")]
)
def test_directions_form_opposing_pairs_on_same_axis(a, b):
    ra, rb = DIRECTION_TO_AXIS[a], DIRECTION_TO_AXIS[b]
    assert ra.axis == rb.axis
    assert ra.sign == -rb.sign


def test_each_axis_used_by_exactly_two_directions():
    counts: dict[str, int] = {}
    for direction in DIRECTIONS:
        axis = DIRECTION_TO_AXIS[direction].axis
        counts[axis] = counts.get(axis, 0) + 1
    assert sorted(counts.items()) == [("x", 2), ("y", 2), ("z", 2)]


def test_direction_table_is_read_only():
    with pytest.raises(TypeError):
        DIRECTION_TO_AXIS["right"] = AxisDirection(axis="y", sign=-1)  # type: ignore[index]
    assert DIRECTION_TO_AXIS["right"] == AxisDirection(axis="x", sign=1)


def test_axis_direction_is_immutable():
    with pytest.raises(Exception):
        DIRECTION_TO_AXIS["right"].axis = "y"  # type: ignore[misc]


def test_resolution_is_deterministic_across_repeated_calls():
    first = resolve_direction("right")
    for _ in range(100):
        assert resolve_direction("right") == first


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_is_direction_accepts_canonical_tokens(direction):
    assert is_direction(direction) is True


@pytest.mark.parametrize(
    "value", ["Right", "to the right", "", None, 1, True, [], {}]
)
def test_is_direction_rejects_everything_else(value):
    assert is_direction(value) is False


def test_zero_delta_is_fresh_each_call():
    a = zero_delta()
    b = zero_delta()
    assert a == Vec3(0.0, 0.0, 0.0)
    assert a is not b


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "direction", ["Right", "sideways", "toward camera", "", None, 1]
)
def test_unknown_directions_rejected(direction):
    result = resolve_direction(direction)
    assert result.ok is False
    assert result.error.code == "VALIDATION_ERROR"
    assert result.error.message == DIRECTION_MESSAGES["UNSUPPORTED_DIRECTION"]


@pytest.mark.parametrize(
    "direction", ["toward camera", "away from camera", "screen left"]
)
def test_camera_relative_wording_not_accepted(direction):
    """Camera-relative interpretation is explicitly deferred for this milestone."""
    assert resolve_direction(direction).ok is False


def test_delta_rejects_bad_direction_before_converting_units():
    result = direction_delta_meters("sideways", {"value": float("nan"), "unit": "mm"})
    assert result.ok is False
    assert result.error.code == "VALIDATION_ERROR"


def test_delta_rejects_bad_measurement():
    result = direction_delta_meters("right", {"value": 50, "unit": "mm"})
    assert result.ok is False
    assert result.error.code == "INVALID_UNITS"


def test_zero_distance_delta_has_no_negative_zero():
    result = direction_delta_meters("left", {"value": 0, "unit": "cm"})
    assert result.ok is True
    assert math.copysign(1.0, result.meters) == 1.0


# ---------------------------------------------------------------------------
# Signed measurement semantics at the composition boundary
# ---------------------------------------------------------------------------


def test_positive_magnitude_accepted_direction_from_token():
    result = direction_delta_meters("left", {"value": 25, "unit": "cm"})
    assert result.ok is True
    assert result.delta == Vec3(-0.25, 0.0, 0.0)
    assert result.meters == -0.25


@pytest.mark.parametrize("unit", ["cm", "m"])
def test_zero_magnitude_accepted(unit):
    result = direction_delta_meters("right", {"value": 0, "unit": unit})
    assert result.ok is True
    assert result.delta == Vec3(0.0, 0.0, 0.0)
    assert result.meters == 0


def test_negative_magnitude_rejected():
    result = direction_delta_meters("left", {"value": -25, "unit": "cm"})
    assert result.ok is False
    assert result.error.code == "VALIDATION_ERROR"
    assert result.error.message == DIRECTION_MESSAGES["NEGATIVE_MAGNITUDE"]


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("unit", ["cm", "m"])
def test_negative_magnitude_rejected_for_every_direction_and_unit(direction, unit):
    result = direction_delta_meters(direction, {"value": -1, "unit": unit})
    assert result.ok is False
    assert result.error.code == "VALIDATION_ERROR"


def test_direction_never_encoded_twice_no_negative_reversal():
    """Previously -25 cm silently reversed the direction. That is now an error."""
    result = direction_delta_meters("right", {"value": -25, "unit": "cm"})
    assert result.ok is False


def test_negative_zero_is_not_a_negative_magnitude():
    result = direction_delta_meters("left", {"value": -0.0, "unit": "cm"})
    assert result.ok is True
    assert result.meters == 0


def test_ordinary_signed_conversion_still_accepted():
    """The generic converter must remain signed; only composition is restricted."""
    result = to_meters({"value": -25, "unit": "cm"})
    assert result.ok is True
    assert result.meters == -0.25


def test_unit_error_takes_precedence_over_magnitude_rule():
    result = direction_delta_meters("right", {"value": -50, "unit": "mm"})
    assert result.ok is False
    assert result.error.code == "INVALID_UNITS"


def test_negative_infinity_rejected_as_non_finite_not_magnitude():
    result = direction_delta_meters("right", {"value": float("-inf"), "unit": "m"})
    assert result.ok is False
    assert result.error.code == "INVALID_UNITS"


# ---------------------------------------------------------------------------
# Independence from Blender
# ---------------------------------------------------------------------------


def test_no_blender_dependency_is_imported():
    """These utilities must be usable with no Blender runtime present."""
    import studio_spatial  # noqa: F401

    assert "bpy" not in sys.modules
    assert "mathutils" not in sys.modules


# ---------------------------------------------------------------------------
# Shared language-neutral corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    CASES["directions"]["valid"],
    ids=[c["name"] for c in CASES["directions"]["valid"]],
)
def test_corpus_directions_valid(case):
    result = resolve_direction(case["direction"])
    assert result.ok is True
    assert result.axis_direction.axis == case["axis"]
    assert result.axis_direction.sign == case["sign"]


@pytest.mark.parametrize(
    "case",
    CASES["directions"]["invalid"],
    ids=[c["name"] for c in CASES["directions"]["invalid"]],
)
def test_corpus_directions_invalid(case):
    result = resolve_direction(decode_value(case["direction"]))
    assert result.ok is False
    assert result.error.code == case["error_code"]


@pytest.mark.parametrize(
    "case",
    CASES["deltas"]["valid"],
    ids=[c["name"] for c in CASES["deltas"]["valid"]],
)
def test_corpus_deltas_valid(case):
    result = direction_delta_meters(
        case["direction"],
        {"value": decode_value(case.get("value")), "unit": case["unit"]},
    )
    assert result.ok is True, f"expected success, got {result.error}"
    assert result.delta == Vec3(**case["delta"])
    assert result.axis == case["axis"]
    assert result.meters == case["meters"]


@pytest.mark.parametrize(
    "case",
    CASES["deltas"]["invalid"],
    ids=[c["name"] for c in CASES["deltas"]["invalid"]],
)
def test_corpus_deltas_invalid(case):
    result = direction_delta_meters(
        case["direction"],
        {"value": decode_value(case.get("value")), "unit": case["unit"]},
    )
    assert result.ok is False
    assert result.error.code == case["error_code"]
    if "error_key" in case:
        assert result.error.message == DIRECTION_MESSAGES[case["error_key"]]
