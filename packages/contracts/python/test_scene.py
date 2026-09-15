"""Scene contract + scene-version digest tests — Python (Spec 002, Task 1).

Four things are proven here:

  1. the ``studio-scene-v1`` digest projection is DETERMINISTIC for unchanged
     semantic state and SENSITIVE to every planning-relevant change;
  2. what the digest excludes really is excluded (capture metadata, project
     identity, object enumeration order);
  3. the canonical schemas make unsafe fields UNREPRESENTABLE rather than merely
     absent from an example — the security assertions build documents that carry a
     path, a hostname, a token, a script, and prove the schema rejects them;
  4. the projection is an ALLOW-LIST, so a future informational SceneSnapshot
     field cannot silently change concurrency semantics.

The equivalent assertions run in TypeScript in ``src/scene.test.ts``, and
``tests/contracts/test_scene_digest_cross_language_parity.py`` proves both
implementations produce byte-identical canonical JSON and identical digests.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from studio_contracts import (
    SCHEMA_FILES,
    load_schema,
    schema_properties,
    schema_required,
    to_wire,
    validate_against_schema,
)
from studio_contracts.scene import (
    DIGEST_DECIMALS,
    FORBIDDEN_SCENE_FIELDS,
    SCENE_DIGEST_VERSION,
    SCENE_OBJECT_DIGEST_FIELDS,
    SCENE_OBJECT_INFORMATIONAL_FIELDS,
    SCENE_SNAPSHOT_DIGEST_FIELDS,
    SCENE_SNAPSHOT_INFORMATIONAL_FIELDS,
    SCENE_UNITS_DIGEST_FIELDS,
    SceneDigestError,
    canonical_digest_json,
    compute_scene_version,
    format_digest_number,
    object_sort_key,
    scene_digest_projection,
    scene_versions_match,
    validate_scene_snapshot,
)
from studio_types import (
    JOB_TYPES,
    MUTATING_JOB_TYPES,
    READ_JOB_TYPES,
    EulerRadians,
    InspectScenePayload,
    MaterialColor,
    MaterialSummary,
    Scale3,
    SceneObject,
    SceneSnapshot,
    SceneUnits,
    Vec3,
    is_mutating_job_type,
)

FAKE_VERSION = "sha256:" + "a" * 64


# ---------------------------------------------------------------------------
# Builders — plain wire documents, because that is what crosses the boundary
# ---------------------------------------------------------------------------


def units(**over: Any) -> dict[str, Any]:
    base = {"unit_system": "METRIC", "length_unit": "METERS", "scale_length": 1.0}
    base.update(over)
    return base


def cube(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "studio_object_id": "obj_cube001",
        "name": "Cube",
        "object_type": "MESH",
        "world_position_meters": {"x": 0.0, "y": 0.0, "z": 0.0},
        "dimensions_meters": {"x": 2.0, "y": 2.0, "z": 2.0},
        "rotation_euler_radians": {"x": 0.0, "y": 0.0, "z": 0.0},
        "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
        "visible": True,
    }
    base.update(over)
    return base


def scene(*objects: dict[str, Any], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"units": units(), "objects": list(objects) or [cube()]}
    base.update(over)
    return base


def snapshot(*objects: dict[str, Any], **over: Any) -> dict[str, Any]:
    body = scene(*objects)
    base: dict[str, Any] = {
        "project_id": "proj_seed",
        "scene_version": compute_scene_version(body),
        "units": body["units"],
        "objects": body["objects"],
        "captured_at": "2026-09-15T04:00:00Z",
    }
    base.update(over)
    return base


def raw_snapshot(*objects: dict[str, Any], **over: Any) -> dict[str, Any]:
    """A snapshot-shaped document with a PLACEHOLDER version.

    Needed for cases whose scene state cannot be digested at all (a NaN
    coordinate, an ambiguous ordering): the point of those tests is that the
    validation layer REPORTS the problem, so the builder must not raise first.
    """
    body = scene(*objects)
    base: dict[str, Any] = {
        "project_id": "proj_seed",
        "scene_version": FAKE_VERSION,
        "units": body["units"],
        "objects": body["objects"],
        "captured_at": "2026-09-15T04:00:00Z",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Numeric representation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0.000000"),
        (-0.0, "0.000000"),
        (0.5, "0.500000"),
        (1, "1.000000"),
        (2.0, "2.000000"),
        (1.6, "1.600000"),
        (-0.25, "-0.250000"),
        (math.pi / 4, "0.785398"),
        (math.pi / 2, "1.570796"),
        (1e-9, "0.000000"),
        (-1e-9, "0.000000"),
        (1234.567891, "1234.567891"),
    ],
)
def test_digest_numbers_are_fixed_decimal(value, expected):
    assert format_digest_number(value) == expected


def test_digest_numbers_always_carry_the_documented_decimals():
    text = format_digest_number(1)
    assert text.split(".")[1] == "0" * DIGEST_DECIMALS


def test_negative_zero_cannot_produce_a_different_version():
    """Blender reports an unset Euler component as -0.0."""
    positive = scene(cube(rotation_euler_radians={"x": 0.0, "y": 0.0, "z": 0.0}))
    negative = scene(cube(rotation_euler_radians={"x": -0.0, "y": -0.0, "z": -0.0}))
    assert compute_scene_version(positive) == compute_scene_version(negative)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused_not_serialized(value):
    with pytest.raises(SceneDigestError):
        format_digest_number(value)
    with pytest.raises(SceneDigestError):
        compute_scene_version(scene(cube(world_position_meters={"x": value, "y": 0, "z": 0})))


@pytest.mark.parametrize("value", ["0.5", None, True, [0.5]])
def test_non_numeric_values_are_refused(value):
    with pytest.raises(SceneDigestError):
        format_digest_number(value)


# ---------------------------------------------------------------------------
# The projection shape
# ---------------------------------------------------------------------------


def test_projection_carries_the_digest_version_inside_the_payload():
    projection = scene_digest_projection(scene())
    assert projection["digest_version"] == SCENE_DIGEST_VERSION
    assert SCENE_DIGEST_VERSION in canonical_digest_json(projection)


def test_projection_needs_only_units_and_objects():
    """A snapshot cannot exist before its version, so the producer digests the
    scene body. That this works is the clearest proof the digest is not 'the
    snapshot'."""
    assert compute_scene_version({"units": units(), "objects": [cube()]}).startswith(
        "sha256:"
    )


def test_projection_emits_explicit_null_for_absent_optionals():
    projection = scene_digest_projection(scene(cube(studio_object_id=None)))
    entry = projection["objects"][0]
    assert entry["studio_object_id"] is None
    assert entry["material"] is None


def test_projection_covers_exactly_the_declared_object_fields():
    entry = scene_digest_projection(scene())["objects"][0]
    assert sorted(entry) == sorted(SCENE_OBJECT_DIGEST_FIELDS)


def test_projection_covers_exactly_the_declared_unit_fields():
    projection = scene_digest_projection(scene())
    assert sorted(projection["units"]) == sorted(SCENE_UNITS_DIGEST_FIELDS)
    assert sorted(projection) == sorted(
        ("digest_version", *SCENE_SNAPSHOT_DIGEST_FIELDS)
    )


def test_canonical_json_is_sorted_and_compact():
    text = canonical_digest_json({"b": 1, "a": {"d": 2, "c": 3}})
    assert text == '{"a":{"c":3,"d":2},"b":1}'


def test_canonical_json_does_not_escape_non_ascii():
    """Python's default ensure_ascii=True would escape 'Küche' while
    JSON.stringify would not, and the two languages would disagree."""
    assert canonical_digest_json({"name": "Küche"}) == '{"name":"Küche"}'


def test_projection_serializes_numbers_as_fixed_decimal_strings():
    """Not JSON numbers: Python renders 1.0 as '1.0' and JavaScript as '1'."""
    entry = scene_digest_projection(scene())["objects"][0]
    assert entry["world_position_meters"] == ["0.000000", "0.000000", "0.000000"]
    assert entry["dimensions_meters"] == ["2.000000", "2.000000", "2.000000"]


# ---------------------------------------------------------------------------
# The eleven required digest properties
# ---------------------------------------------------------------------------


def test_1_repeated_inspection_of_an_unchanged_scene_gives_the_same_version():
    first = snapshot()
    second = snapshot(captured_at="2026-09-15T05:30:00Z")
    assert compute_scene_version(first) == compute_scene_version(second)


def test_2_captured_at_does_not_change_the_scene_version():
    base = snapshot()
    later = dict(base, captured_at="2027-01-01T00:00:00Z")
    assert compute_scene_version(later) == compute_scene_version(base)


def test_3_object_enumeration_order_does_not_change_the_scene_version():
    table = cube(studio_object_id="obj_table001", name="Table")
    chair = cube(studio_object_id="obj_chair001", name="Chair")
    forwards = scene(table, chair)
    backwards = scene(chair, table)
    assert compute_scene_version(forwards) == compute_scene_version(backwards)


def test_4_position_change_changes_the_scene_version():
    before = scene()
    after = scene(cube(world_position_meters={"x": 0.5, "y": 0.0, "z": 0.0}))
    assert compute_scene_version(after) != compute_scene_version(before)


def test_5_dimensions_change_changes_the_scene_version():
    after = scene(cube(dimensions_meters={"x": 1.6, "y": 1.6, "z": 1.6}))
    assert compute_scene_version(after) != compute_scene_version(scene())


def test_6_rotation_change_changes_the_scene_version():
    after = scene(cube(rotation_euler_radians={"x": 0.0, "y": 0.0, "z": math.pi / 4}))
    assert compute_scene_version(after) != compute_scene_version(scene())


def test_7_scale_change_changes_the_scene_version():
    after = scene(cube(scale={"x": 0.8, "y": 0.8, "z": 0.8}))
    assert compute_scene_version(after) != compute_scene_version(scene())


def test_8_exposed_material_colour_change_changes_the_scene_version():
    before = scene(
        cube(material={"name": "Beige", "base_color": {"r": 0.76, "g": 0.66, "b": 0.5, "a": 1.0}})
    )
    after = scene(
        cube(material={"name": "Beige", "base_color": {"r": 0.2, "g": 0.66, "b": 0.5, "a": 1.0}})
    )
    assert compute_scene_version(after) != compute_scene_version(before)


def test_8b_material_presence_and_name_participate_too():
    plain = scene(cube())
    named = scene(cube(material={"name": "Beige"}))
    renamed = scene(cube(material={"name": "Oak"}))
    versions = {
        compute_scene_version(plain),
        compute_scene_version(named),
        compute_scene_version(renamed),
    }
    assert len(versions) == 3


def test_8c_absent_base_colour_is_not_the_same_as_black():
    absent = scene(cube(material={"name": "Procedural"}))
    black = scene(
        cube(material={"name": "Procedural", "base_color": {"r": 0, "g": 0, "b": 0, "a": 1}})
    )
    assert compute_scene_version(absent) != compute_scene_version(black)


def test_9_visibility_change_changes_the_scene_version():
    assert compute_scene_version(scene(cube(visible=False))) != compute_scene_version(
        scene(cube(visible=True))
    )


def test_10_adding_or_removing_an_object_changes_the_scene_version():
    one = scene(cube())
    two = scene(cube(), cube(studio_object_id="obj_chair001", name="Chair"))
    empty = scene(*[], objects=[])
    versions = {
        compute_scene_version(one),
        compute_scene_version(two),
        compute_scene_version(empty),
    }
    assert len(versions) == 3


def test_11_excluded_metadata_change_does_not_change_the_scene_version():
    base = snapshot()
    noisy = dict(
        base,
        captured_at="2030-06-01T12:00:00Z",
        project_id="proj_completely_different",
        scene_version="sha256:" + "f" * 64,
    )
    assert compute_scene_version(noisy) == compute_scene_version(base)


def test_11b_repeated_digest_computation_is_byte_identical():
    body = scene(
        cube(),
        cube(studio_object_id="obj_chair001", name="Chair", visible=False),
    )
    canonical = {canonical_digest_json(scene_digest_projection(body)) for _ in range(25)}
    versions = {compute_scene_version(body) for _ in range(25)}
    assert len(canonical) == 1
    assert len(versions) == 1


def test_11c_project_id_exclusion_is_deliberate_and_documented():
    """The digest answers 'is this the same scene state?', not 'whose scene is
    this?'. Authorization and cache keying are project-scoped elsewhere; domain
    separation comes from digest_version, which is inside the hashed payload."""
    assert "project_id" in SCENE_SNAPSHOT_INFORMATIONAL_FIELDS
    here = snapshot(project_id="proj_a")
    there = snapshot(project_id="proj_b")
    assert compute_scene_version(here) == compute_scene_version(there)
    assert "project_id" not in canonical_digest_json(scene_digest_projection(here))


def test_the_version_is_a_prefixed_sha256():
    version = compute_scene_version(scene())
    assert validate_against_schema(SCHEMA_FILES["SceneVersion"], version).valid
    assert scene_versions_match(version, compute_scene_version(scene()))
    assert not scene_versions_match(version, version.removeprefix("sha256:"))


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_objects_with_stable_ids_sort_before_objects_without():
    with_id = cube(studio_object_id="obj_zzz", name="Aaa")
    without_id = cube(name="Bbb")
    without_id.pop("studio_object_id")
    ordered = scene_digest_projection(scene(without_id, with_id))["objects"]
    assert [entry["name"] for entry in ordered] == ["Aaa", "Bbb"]


def test_sort_key_prefers_the_stable_id_then_falls_back_to_name():
    assert object_sort_key(cube()) == (0, "obj_cube001")
    anonymous = cube()
    anonymous.pop("studio_object_id")
    assert object_sort_key(anonymous) == (1, "Cube")


def test_blank_stable_id_falls_back_to_name_rather_than_sorting_as_empty():
    assert object_sort_key(cube(studio_object_id="   ")) == (1, "Cube")


def test_duplicate_sort_keys_are_refused_rather_than_ordered_arbitrarily():
    twins = scene(cube(), cube(name="Cube.001"))
    with pytest.raises(SceneDigestError, match="ambiguous"):
        compute_scene_version(twins)


def test_duplicate_names_without_ids_are_refused():
    a, b = cube(), cube()
    a.pop("studio_object_id")
    b.pop("studio_object_id")
    with pytest.raises(SceneDigestError, match="ambiguous"):
        compute_scene_version(scene(a, b))


def test_an_object_without_a_usable_order_key_is_refused():
    nameless = cube(name="   ")
    nameless.pop("studio_object_id")
    with pytest.raises(SceneDigestError):
        compute_scene_version(scene(nameless))


# ---------------------------------------------------------------------------
# Malformed input is refused, never digested
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"objects": []},
        {"units": units()},
        {"units": units(), "objects": {"Cube": {}}},
        {"units": units(), "objects": "Cube"},
        {"units": "METRIC", "objects": []},
        {"units": units(unit_system=""), "objects": []},
        {"units": units(length_unit=""), "objects": []},
    ],
)
def test_malformed_scene_bodies_are_refused(body):
    with pytest.raises(SceneDigestError):
        compute_scene_version(body)


@pytest.mark.parametrize(
    "over",
    [
        {"visible": "yes"},
        {"object_type": ""},
        {"name": ""},
        {"studio_object_id": 7},
        {"world_position_meters": {"x": 0, "y": 0}},
        {"scale": [1, 1, 1]},
        {"material": "Oak"},
    ],
)
def test_malformed_objects_are_refused(over):
    with pytest.raises(SceneDigestError):
        compute_scene_version(scene(cube(**over)))


# ---------------------------------------------------------------------------
# Future-schema rule: the projection is an allow-list
# ---------------------------------------------------------------------------


def test_every_snapshot_field_is_classified():
    """A new SceneSnapshot field must be declared either digest-relevant or
    informational. An unclassified field fails HERE rather than silently changing
    concurrency semantics."""
    declared = set(SCENE_SNAPSHOT_DIGEST_FIELDS) | set(
        SCENE_SNAPSHOT_INFORMATIONAL_FIELDS
    )
    assert set(schema_properties(SCHEMA_FILES["SceneSnapshot"])) == declared


def test_every_scene_object_field_is_classified():
    declared = set(SCENE_OBJECT_DIGEST_FIELDS) | set(
        SCENE_OBJECT_INFORMATIONAL_FIELDS
    )
    assert set(schema_properties(SCHEMA_FILES["SceneObject"])) == declared


def test_every_unit_field_participates_in_the_digest():
    assert set(schema_properties(SCHEMA_FILES["SceneUnits"])) == set(
        SCENE_UNITS_DIGEST_FIELDS
    )


def test_an_unclassified_informational_field_would_not_change_the_version():
    """Simulates the future case directly: an extra key outside the projection is
    ignored by the digest (the SCHEMA still rejects the document, which is the
    other half of the guarantee)."""
    base = snapshot()
    with_extra = dict(base, thumbnail_hint="dark")
    assert compute_scene_version(with_extra) == compute_scene_version(base)
    assert not validate_against_schema(SCHEMA_FILES["SceneSnapshot"], with_extra).valid


# ---------------------------------------------------------------------------
# Security contract: unsafe fields are UNREPRESENTABLE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema_key", ["SceneSnapshot", "SceneObject"])
def test_scene_contracts_are_closed(schema_key):
    assert load_schema(SCHEMA_FILES[schema_key])["additionalProperties"] is False


@pytest.mark.parametrize("schema_key", ["SceneSnapshot", "SceneObject", "MaterialSummary"])
@pytest.mark.parametrize("forbidden", FORBIDDEN_SCENE_FIELDS)
def test_forbidden_fields_are_not_declared(schema_key, forbidden):
    assert forbidden not in schema_properties(SCHEMA_FILES[schema_key])


@pytest.mark.parametrize("forbidden", FORBIDDEN_SCENE_FIELDS)
def test_schema_rejects_a_forbidden_field_on_a_scene_object(forbidden):
    """Not a scan of example output: the document really carries the field, and the
    canonical schema really refuses it."""
    document = cube(**{forbidden: "/srv/projects/seed.blend"})
    result = validate_against_schema(SCHEMA_FILES["SceneObject"], document)
    assert not result.valid
    assert any(forbidden in v.path for v in result.violations)


@pytest.mark.parametrize("forbidden", FORBIDDEN_SCENE_FIELDS)
def test_schema_rejects_a_forbidden_field_on_a_snapshot(forbidden):
    document = dict(snapshot(), **{forbidden: "legion-t7"})
    result = validate_against_schema(SCHEMA_FILES["SceneSnapshot"], document)
    assert not result.valid
    assert any(forbidden in v.path for v in result.violations)


def test_a_snapshot_cannot_smuggle_a_path_inside_a_material():
    document = snapshot(cube(material={"name": "Oak", "texture_path": "/tmp/oak.png"}))
    assert not validate_against_schema(SCHEMA_FILES["SceneSnapshot"], document).valid


def test_a_snapshot_cannot_carry_a_nested_metadata_bag():
    document = snapshot(cube(metadata={"blend_path": "/srv/a.blend"}))
    assert not validate_against_schema(SCHEMA_FILES["SceneSnapshot"], document).valid


def test_inspect_scene_payload_cannot_carry_anything():
    assert validate_against_schema(SCHEMA_FILES["InspectScenePayload"], {}).valid
    for smuggled in (
        {"blend_path": "/srv/a.blend"},
        {"target": {"name": "Cube"}},
        {"python": "import bpy"},
        {"project_id": "proj_other"},
    ):
        assert not validate_against_schema(
            SCHEMA_FILES["InspectScenePayload"], smuggled
        ).valid


# ---------------------------------------------------------------------------
# Validation layer (the rules JSON Schema cannot express)
# ---------------------------------------------------------------------------


def test_a_well_formed_snapshot_validates():
    result = validate_scene_snapshot(snapshot())
    assert result.valid, result.errors


def test_scene_version_can_be_verified_against_the_state_it_labels():
    good = snapshot()
    assert validate_scene_snapshot(good, verify_scene_version=True).valid

    lying = dict(good, scene_version="sha256:" + "b" * 64)
    result = validate_scene_snapshot(lying, verify_scene_version=True)
    assert not result.valid
    assert [error.code for error in result.errors] == ["SCENE_VERSION_MISMATCH"]


def test_version_verification_is_off_by_default_because_producers_build_in_order():
    lying = dict(snapshot(), scene_version="sha256:" + "b" * 64)
    assert validate_scene_snapshot(lying).valid


@pytest.mark.parametrize("scale_length", [0, -1.0])
def test_non_positive_scale_length_is_invalid_units(scale_length):
    document = dict(snapshot(), units=units(scale_length=scale_length))
    result = validate_scene_snapshot(document)
    assert not result.valid
    assert any(error.code == "INVALID_UNITS" for error in result.errors)


def test_unknown_unit_system_is_reported_as_invalid_units():
    document = dict(snapshot(), units=units(unit_system="GALACTIC"))
    result = validate_scene_snapshot(document)
    assert not result.valid
    assert any(error.code == "INVALID_UNITS" for error in result.errors)


def test_non_finite_numbers_are_reported_by_the_validation_layer():
    """JSON has no NaN literal, so the schema cannot express this rule; the
    validation layer carries it, exactly as with finite meters in Spec 001."""
    document = raw_snapshot(
        cube(world_position_meters={"x": float("nan"), "y": 0.0, "z": 0.0})
    )
    result = validate_scene_snapshot(document)
    assert not result.valid
    assert any(error.code == "INVALID_UNITS" for error in result.errors)


def test_zero_scale_is_rejected_as_degenerate():
    result = validate_scene_snapshot(snapshot(cube(scale={"x": 0.0, "y": 1.0, "z": 1.0})))
    assert not result.valid
    assert any("non-zero" in error.message for error in result.errors)


def test_ambiguous_ordering_is_reported_rather_than_raised():
    document = raw_snapshot(cube(), cube(name="Cube.001"))
    result = validate_scene_snapshot(document)
    assert not result.valid
    assert any("sort key" in error.message for error in result.errors)


def test_a_non_object_snapshot_is_rejected():
    assert not validate_scene_snapshot(["not", "a", "snapshot"]).valid


# ---------------------------------------------------------------------------
# Representation parity: Python dataclasses vs canonical schemas
# ---------------------------------------------------------------------------


def _fields(cls) -> list[str]:
    import dataclasses

    return sorted(f.name for f in dataclasses.fields(cls))


@pytest.mark.parametrize(
    "cls,schema_key",
    [
        (SceneSnapshot, "SceneSnapshot"),
        (SceneObject, "SceneObject"),
        (SceneUnits, "SceneUnits"),
        (MaterialSummary, "MaterialSummary"),
        (MaterialColor, "MaterialColor"),
        (EulerRadians, "EulerRadians"),
        (Scale3, "Scale3"),
    ],
)
def test_dataclass_fields_match_canonical_properties(cls, schema_key):
    assert _fields(cls) == schema_properties(SCHEMA_FILES[schema_key])


@pytest.mark.parametrize(
    "schema_key,expected",
    [
        ("SceneSnapshot", ["captured_at", "objects", "project_id", "scene_version", "units"]),
        (
            "SceneObject",
            [
                "dimensions_meters",
                "name",
                "object_type",
                "rotation_euler_radians",
                "scale",
                "visible",
                "world_position_meters",
            ],
        ),
        ("SceneUnits", ["length_unit", "scale_length", "unit_system"]),
        ("MaterialSummary", ["name"]),
        ("MaterialColor", ["a", "b", "g", "r"]),
        ("EulerRadians", ["x", "y", "z"]),
        ("Scale3", ["x", "y", "z"]),
    ],
)
def test_canonical_required_fields(schema_key, expected):
    assert schema_required(SCHEMA_FILES[schema_key]) == expected


def test_a_python_built_snapshot_is_schema_valid_and_digestible():
    built = SceneSnapshot(
        project_id="proj_seed",
        scene_version=FAKE_VERSION,
        units=SceneUnits(unit_system="METRIC", length_unit="METERS", scale_length=1.0),
        objects=(
            SceneObject(
                name="Cube",
                object_type="MESH",
                world_position_meters=Vec3(0.0, 0.0, 0.0),
                dimensions_meters=Vec3(2.0, 2.0, 2.0),
                rotation_euler_radians=EulerRadians(0.0, 0.0, 0.0),
                scale=Scale3(1.0, 1.0, 1.0),
                visible=True,
                studio_object_id="obj_cube001",
                material=MaterialSummary(
                    name="Beige", base_color=MaterialColor(0.76, 0.66, 0.5, 1.0)
                ),
            ),
        ),
        captured_at="2026-09-15T04:00:00Z",
    )
    wire = to_wire(built)
    assert validate_against_schema(SCHEMA_FILES["SceneSnapshot"], wire).valid

    # The dataclass and its wire form must digest identically: the projection
    # accepts either, and a representation must never change a version.
    assert compute_scene_version(built) == compute_scene_version(wire)


def test_a_python_built_object_without_optionals_is_schema_valid():
    built = SceneObject(
        name="Light",
        object_type="LIGHT",
        world_position_meters=Vec3(0.0, 0.0, 3.0),
        dimensions_meters=Vec3(0.0, 0.0, 0.0),
        rotation_euler_radians=EulerRadians(0.0, 0.0, 0.0),
        scale=Scale3(1.0, 1.0, 1.0),
        visible=True,
    )
    assert validate_against_schema(SCHEMA_FILES["SceneObject"], to_wire(built)).valid


# ---------------------------------------------------------------------------
# inspect_scene job contract
# ---------------------------------------------------------------------------


def test_inspect_scene_is_a_canonical_job_type():
    assert "inspect_scene" in JOB_TYPES


def test_read_and_mutating_classifications_partition_the_job_types():
    assert set(MUTATING_JOB_TYPES) | set(READ_JOB_TYPES) == set(JOB_TYPES)
    assert not set(MUTATING_JOB_TYPES) & set(READ_JOB_TYPES)
    assert is_mutating_job_type("move_object")
    assert not is_mutating_job_type("inspect_scene")


def test_inspect_scene_payload_representation_is_empty():
    assert to_wire(InspectScenePayload()) == {}
    assert validate_against_schema(
        SCHEMA_FILES["InspectScenePayload"], to_wire(InspectScenePayload())
    ).valid


def test_an_inspect_scene_job_is_schema_valid():
    job = {
        "job_id": "job_inspect_1",
        "job_type": "inspect_scene",
        "project_id": "proj_seed",
        "session_id": "sess_1",
        "user_id": "user_1",
        "payload": {},
        "origin": {"request_id": "req_read_1", "operation_index": 0},
        "status": "queued",
        "created_at": "2026-09-15T04:00:00Z",
        "idempotency_key": "idem_read_1",
    }
    assert validate_against_schema(SCHEMA_FILES["Job"], job).valid

    with_selector = dict(job, payload={"target": {"name": "Cube"}})
    assert not validate_against_schema(SCHEMA_FILES["Job"], with_selector).valid


def test_the_move_object_payload_binding_still_holds():
    """Adding a job type must not loosen the existing discrimination."""
    job = {
        "job_id": "job_1",
        "job_type": "move_object",
        "project_id": "proj_seed",
        "session_id": "sess_1",
        "user_id": "user_1",
        "payload": {},
        "origin": {"request_id": "req_1", "operation_index": 0},
        "status": "queued",
        "created_at": "2026-09-15T04:00:00Z",
        "idempotency_key": "idem_1",
    }
    assert not validate_against_schema(SCHEMA_FILES["Job"], job).valid


# ---------------------------------------------------------------------------
# Shared vector corpus (the same file the TypeScript tests read)
# ---------------------------------------------------------------------------

CASES = json.loads(
    (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "scene-cases.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "case", CASES["numbers"]["cases"], ids=lambda c: c["name"]
)
def test_shared_number_vectors(case):
    assert format_digest_number(case["value"]) == case["expected"]


@pytest.mark.parametrize(
    "case", CASES["numbers"]["rejected"], ids=lambda c: c["name"]
)
def test_shared_rejected_number_vectors(case):
    with pytest.raises(SceneDigestError):
        format_digest_number(case["value"])


@pytest.mark.parametrize("case", CASES["scenes"], ids=lambda c: c["name"])
def test_shared_scene_vectors_are_digestible(case):
    version = compute_scene_version(case["scene"])
    assert validate_against_schema(SCHEMA_FILES["SceneVersion"], version).valid


@pytest.mark.parametrize("case", CASES["rejected_scenes"], ids=lambda c: c["name"])
def test_shared_rejected_scene_vectors(case):
    with pytest.raises(SceneDigestError):
        compute_scene_version(case["scene"])


def test_shared_scene_vectors_are_all_distinct():
    """Different scenes must not collide, and the corpus must not contain two
    cases that are secretly the same scene."""
    versions = {
        case["name"]: compute_scene_version(case["scene"]) for case in CASES["scenes"]
    }
    assert len(set(versions.values())) == len(versions), versions
