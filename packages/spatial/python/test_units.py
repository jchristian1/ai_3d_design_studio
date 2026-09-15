"""Unit conversion tests — Python (Spec 001, Task 2).

Covers the required examples explicitly, then replays the shared
language-neutral corpus so Python and TypeScript are held to identical behaviour.
_Requirements: 3.2_
"""

from __future__ import annotations

import math

import pytest
from spatial_test_support import decode_value, load_spatial_cases
from studio_spatial import (
    CM_PER_METER,
    CONVERSION_MESSAGES,
    cm_to_meters,
    convert_to_meters,
    meters_to_meters,
    to_meters,
)
from studio_types import Measurement

CASES = load_spatial_cases()


# ---------------------------------------------------------------------------
# Required examples from Spec 001
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cm,expected",
    [(50, 0.5), (100, 1.0), (-25, -0.25)],
    ids=["50cm->0.50m", "100cm->1.00m", "-25cm->-0.25m"],
)
def test_required_cm_to_meters(cm, expected):
    result = cm_to_meters(cm)
    assert result.ok is True
    assert result.meters == expected


@pytest.mark.parametrize("value", [0, 0.5, 1, 2.7, -0.25])
def test_meters_pass_through_unchanged(value):
    result = meters_to_meters(value)
    assert result.ok is True
    assert result.meters == value


def test_cm_per_meter_is_100():
    assert CM_PER_METER == 100


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_conversion_is_deterministic_across_repeated_calls():
    first = cm_to_meters(1.1)
    for _ in range(100):
        again = cm_to_meters(1.1)
        assert again == first


def test_division_is_correctly_rounded_unlike_multiplication():
    # Verified counterexample: the two operations land on different floats.
    v = 6537.04249344076
    result = cm_to_meters(v)
    assert result.meters == v / 100
    assert v / 100 != v * 0.01, "counterexample must actually diverge"
    assert result.meters != v * 0.01


def test_1_1_cm_converts_to_float_nearest_quotient():
    # Binary floating point: this is 0.011000000000000001, not 0.011.
    # Identical in Python and TypeScript.
    assert cm_to_meters(1.1).meters == 0.011000000000000001


def test_negative_zero_is_normalized():
    result = cm_to_meters(-0.0)
    assert result.ok is True
    assert math.copysign(1.0, result.meters) == 1.0, "expected +0.0, not -0.0"


# ---------------------------------------------------------------------------
# Rejection of invalid / non-finite measurements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_non_finite_cm_rejected(value):
    result = cm_to_meters(value)
    assert result.ok is False
    assert result.error.code == "INVALID_UNITS"
    assert result.error.message == CONVERSION_MESSAGES["NON_FINITE_VALUE"]


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_non_finite_meters_rejected(value):
    result = meters_to_meters(value)
    assert result.ok is False
    assert result.error.code == "INVALID_UNITS"


@pytest.mark.parametrize("value", ["50", None, True, {}, [], object()])
def test_non_numeric_values_rejected(value):
    result = cm_to_meters(value)
    assert result.ok is False
    assert result.error.code == "INVALID_UNITS"


@pytest.mark.parametrize("unit", ["mm", "meters", "CM", "", "ft", None, 1])
def test_unsupported_units_rejected(unit):
    result = convert_to_meters(50, unit)
    assert result.ok is False
    assert result.error.code == "INVALID_UNITS"
    assert result.error.message == CONVERSION_MESSAGES["UNSUPPORTED_UNIT"]


@pytest.mark.parametrize("measurement", [None, 50, "50 cm", [50, "cm"], object()])
def test_malformed_measurements_rejected(measurement):
    result = to_meters(measurement)
    assert result.ok is False
    assert result.error.code == "INVALID_UNITS"


def test_unknown_unit_rejected_even_when_value_also_invalid():
    result = to_meters({"value": float("nan"), "unit": "mm"})
    assert result.ok is False
    assert result.error.message == CONVERSION_MESSAGES["UNSUPPORTED_UNIT"]


def test_accepts_measurement_dataclass():
    result = to_meters(Measurement(value=50, unit="cm"))
    assert result.ok is True
    assert result.meters == 0.5


# ---------------------------------------------------------------------------
# Shared language-neutral corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    CASES["conversion"]["valid"],
    ids=[c["name"] for c in CASES["conversion"]["valid"]],
)
def test_corpus_conversion_valid(case):
    result = to_meters({"value": decode_value(case.get("value")), "unit": case["unit"]})
    assert result.ok is True, f"expected success, got {result.error}"
    assert result.meters == case["meters"]
    assert math.copysign(1.0, result.meters) == 1.0 or result.meters != 0


@pytest.mark.parametrize(
    "case",
    CASES["conversion"]["invalid"],
    ids=[c["name"] for c in CASES["conversion"]["invalid"]],
)
def test_corpus_conversion_invalid(case):
    result = to_meters({"value": decode_value(case.get("value")), "unit": case["unit"]})
    assert result.ok is False
    assert result.error.code == case["error_code"]
    assert result.error.message == CONVERSION_MESSAGES[case["error_key"]]


@pytest.mark.parametrize(
    "case",
    CASES["conversion"]["malformed"],
    ids=[c["name"] for c in CASES["conversion"]["malformed"]],
)
def test_corpus_conversion_malformed(case):
    result = to_meters(decode_value(case["measurement"]))
    assert result.ok is False
    assert result.error.code == case["error_code"]
    assert result.error.message == CONVERSION_MESSAGES[case["error_key"]]
