"""Conformance: spatial utilities vs. CANONICAL schemas — Python.

Task 2 introduced new cross-boundary vocabulary (LengthUnit, Measurement, Axis,
Direction, AxisDirection). Per the Task 1 architecture, the schemas in
packages/contracts/schemas are canonical and this representation must conform.
These tests read those schema files at runtime.
"""

from __future__ import annotations

import dataclasses

import pytest
from studio_contracts import (
    SCHEMA_FILES,
    schema_enum,
    schema_properties,
    schema_required,
    to_wire,
    validate_against_schema,
)
from studio_spatial import (
    DIRECTION_TO_AXIS,
    direction_delta_meters,
    resolve_direction,
)
from studio_types import (
    AXES,
    AXIS_SIGNS,
    DIRECTIONS,
    LENGTH_UNITS,
    AxisDirection,
    Measurement,
)


# ---------------------------------------------------------------------------
# Enum parity with canonical schemas
# ---------------------------------------------------------------------------


def test_length_units_equal_canonical_enum():
    assert list(LENGTH_UNITS) == schema_enum(SCHEMA_FILES["LengthUnit"])


def test_axes_equal_canonical_enum():
    assert list(AXES) == schema_enum(SCHEMA_FILES["Axis"])


def test_directions_equal_canonical_enum():
    assert list(DIRECTIONS) == schema_enum(SCHEMA_FILES["Direction"])


def test_axis_signs_equal_canonical_enum():
    assert list(AXIS_SIGNS) == schema_enum(SCHEMA_FILES["AxisDirection"], ["sign"])


# ---------------------------------------------------------------------------
# Field parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls,schema_key",
    [(Measurement, "Measurement"), (AxisDirection, "AxisDirection")],
)
def test_dataclass_fields_match_canonical_properties(cls, schema_key):
    names = sorted(f.name for f in dataclasses.fields(cls))
    assert names == schema_properties(SCHEMA_FILES[schema_key])


@pytest.mark.parametrize(
    "schema_key,expected",
    [("Measurement", ["unit", "value"]), ("AxisDirection", ["axis", "sign"])],
)
def test_canonical_required_fields(schema_key, expected):
    assert schema_required(SCHEMA_FILES[schema_key]) == expected


@pytest.mark.parametrize("cls", [Measurement, AxisDirection])
def test_required_fields_have_no_defaults(cls):
    for f in dataclasses.fields(cls):
        assert (
            f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ), f"{cls.__name__}.{f.name} is canonically required but optional"


# ---------------------------------------------------------------------------
# Outputs are schema-valid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_resolved_axis_direction_is_schema_valid(direction):
    result = resolve_direction(direction)
    assert result.ok is True
    validated = validate_against_schema(
        SCHEMA_FILES["AxisDirection"], to_wire(result.axis_direction)
    )
    assert validated.valid, validated.violations


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_direction_token_is_schema_valid(direction):
    validated = validate_against_schema(SCHEMA_FILES["Direction"], direction)
    assert validated.valid, validated.violations


def test_whole_direction_table_is_schema_valid():
    for direction, axis_direction in DIRECTION_TO_AXIS.items():
        assert validate_against_schema(SCHEMA_FILES["Direction"], direction).valid
        assert validate_against_schema(
            SCHEMA_FILES["AxisDirection"], to_wire(axis_direction)
        ).valid


def test_composed_delta_is_schema_valid_vec3():
    result = direction_delta_meters("right", {"value": 50, "unit": "cm"})
    assert result.ok is True
    validated = validate_against_schema(SCHEMA_FILES["Vec3"], to_wire(result.delta))
    assert validated.valid, validated.violations


def test_measurement_instance_is_schema_valid():
    validated = validate_against_schema(
        SCHEMA_FILES["Measurement"], to_wire(Measurement(value=50, unit="cm"))
    )
    assert validated.valid, validated.violations


def test_conversion_error_is_schema_valid_structured_error():
    result = direction_delta_meters("right", {"value": 50, "unit": "mm"})
    assert result.ok is False
    validated = validate_against_schema(
        SCHEMA_FILES["ErrorResponse"], to_wire(result.error)
    )
    assert validated.valid, validated.violations


def test_direction_error_is_schema_valid_structured_error():
    result = resolve_direction("sideways")
    assert result.ok is False
    validated = validate_against_schema(
        SCHEMA_FILES["ErrorResponse"], to_wire(result.error)
    )
    assert validated.valid, validated.violations
