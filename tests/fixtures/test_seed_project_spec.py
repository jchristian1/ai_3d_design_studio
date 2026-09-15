"""Seed fixture spec and helper tests — no Blender required (Spec 001, Task 5).

These cover everything that can be checked without launching Blender: that the
spec is well-formed and internally consistent, that it agrees with the contracts
and the adapter from earlier tasks, and that the isolation helpers behave. The
real-Blender behaviour lives in tests/blender/test_seed_project_blender.py behind
the `blender` marker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from blender_mcp.adapters.blender_scene import OBJECT_ID_PROPERTY
from studio_contracts import SCHEMA_FILES, validate_against_schema
from studio_fixtures.seed_project import (
    BUILD_DIR,
    FIXTURE_DIR,
    GENERATOR_SCRIPT,
    INSPECT_SCRIPT,
    MOVE_SCRIPT,
    SPEC_PATH,
    STUDIO_PATHS,
    expected_object,
    file_digest,
    load_spec,
    scene_state_digest,
    seed_blend_path,
    spec_mismatches,
    studio_pythonpath,
)

SPEC = load_spec()


# ---------------------------------------------------------------------------
# The spec exists and is well-formed
# ---------------------------------------------------------------------------


def test_spec_file_exists_and_is_json():
    assert SPEC_PATH.exists()
    json.loads(SPEC_PATH.read_text("utf-8"))


def test_spec_declares_a_fixture_version():
    assert SPEC["fixture_version"] == "1.0.0"


def test_spec_describes_exactly_one_object():
    """Deterministic contents: nothing beyond what Spec 001 needs."""
    assert len(SPEC["objects"]) == 1
    assert SPEC["scene"]["expected_object_names"] == ["Cube"]


def test_spec_cube_identity_matches_spec_001():
    cube = expected_object("Cube")
    assert cube["name"] == "Cube"
    assert cube["object_id"] == "obj_cube001"
    assert cube["type"] == "MESH"


def test_spec_cube_starts_at_the_world_origin():
    location = expected_object("Cube")["location_meters"]
    assert location == {"x": 0.0, "y": 0.0, "z": 0.0}


def test_spec_cube_has_identity_rotation_and_scale():
    cube = expected_object("Cube")
    assert cube["rotation_euler_radians"] == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert cube["scale"] == {"x": 1.0, "y": 1.0, "z": 1.0}


def test_spec_configures_meters_explicitly():
    scene = SPEC["scene"]
    assert scene["unit_system"] == "METRIC"
    assert scene["length_unit"] == "METERS"
    assert scene["scale_length"] == 1.0


def test_spec_starts_from_an_empty_scene():
    """No default cube/camera/light, so contents are fully determined."""
    assert SPEC["scene"]["start_empty"] is True


def test_spec_records_both_resolution_paths():
    assert sorted(expected_object("Cube")["resolvable_by"]) == ["name", "object_id"]


def test_spec_documents_why_binary_determinism_is_not_pursued():
    note = SPEC["determinism"]["note"]
    assert "byte-for-byte" in note
    assert SPEC["determinism"]["digest_fields"]


# ---------------------------------------------------------------------------
# The spec agrees with earlier tasks
# ---------------------------------------------------------------------------


def test_object_id_property_matches_the_blender_adapter():
    """The stable-id mechanism must be the adapter's, not a second convention."""
    assert SPEC["object_id_property"] == OBJECT_ID_PROPERTY


def test_cube_is_a_valid_object_ref_by_name():
    cube = expected_object("Cube")
    result = validate_against_schema(
        SCHEMA_FILES["ObjectRef"], {"name": cube["name"]}
    )
    assert result.valid, result.violations


def test_cube_is_a_valid_object_ref_by_object_id():
    cube = expected_object("Cube")
    result = validate_against_schema(
        SCHEMA_FILES["ObjectRef"], {"object_id": cube["object_id"]}
    )
    assert result.valid, result.violations


def test_the_spec_001_plan_for_this_fixture_is_contract_valid():
    """The plan an E2E run will use must validate against the canonical schema."""
    plan = {
        "job_id": "job_seed_1",
        "target": {"name": "Cube"},
        "expected_before_meters": expected_object("Cube")["location_meters"],
        "delta_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
        "desired_after_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
    }
    result = validate_against_schema(SCHEMA_FILES["MoveObjectPlan"], plan)
    assert result.valid, result.violations


# ---------------------------------------------------------------------------
# Generation is scripted, not manual
# ---------------------------------------------------------------------------


def test_generator_and_helper_scripts_exist():
    for script in (GENERATOR_SCRIPT, INSPECT_SCRIPT, MOVE_SCRIPT):
        assert script.exists(), script


def test_generated_blend_is_not_committed_to_git():
    """The generator is the source of truth; a 0.5 MB binary is not reviewable."""
    repo_root = FIXTURE_DIR.parents[2]
    gitignore = (repo_root / ".gitignore").read_text("utf-8")
    assert "tests/fixtures/blender/build/" in gitignore
    assert BUILD_DIR.name == "build"
    assert seed_blend_path().parent == BUILD_DIR


def test_generator_does_not_hardcode_a_blender_path():
    """Executable detection must stay centralized in blender_runtime (Task 4)."""
    for script in (GENERATOR_SCRIPT, INSPECT_SCRIPT, MOVE_SCRIPT):
        text = script.read_text("utf-8")
        assert "/snap/bin/blender" not in text
        assert "/usr/bin/blender" not in text


def test_helper_delegates_blender_lookup_to_the_runtime_module():
    helper = (
        Path(__file__).resolve().parent / "studio_fixtures" / "seed_project.py"
    ).read_text("utf-8")
    assert "from blender_mcp.blender_runtime import" in helper
    assert "/snap/bin/blender" not in helper


def test_generator_derives_everything_from_the_spec():
    """A hand-placed object would make the spec a lie."""
    text = GENERATOR_SCRIPT.read_text("utf-8")
    assert "read_factory_settings" in text
    assert "SEED_SPEC" in text
    assert 'spec["objects"]' in text


def test_generator_verifies_the_scene_before_saving():
    assert "expected" in GENERATOR_SCRIPT.read_text("utf-8")


# ---------------------------------------------------------------------------
# Import paths handed to Blender's bundled interpreter
# ---------------------------------------------------------------------------


def test_studio_paths_all_exist():
    for path in STUDIO_PATHS:
        assert path.exists(), path


def test_studio_pythonpath_is_os_pathsep_joined():
    import os

    joined = studio_pythonpath()
    assert os.pathsep in joined
    assert "services/blender-mcp" in joined


# ---------------------------------------------------------------------------
# Verification logic
# ---------------------------------------------------------------------------


def _digest_matching_spec() -> dict:
    cube = expected_object("Cube")
    return {
        "scene": {
            "name": "Scene",
            "unit_system": "METRIC",
            "length_unit": "METERS",
            "scale_length": 1.0,
        },
        "objects": [
            {
                "name": cube["name"],
                "object_id": cube["object_id"],
                "type": cube["type"],
                "world_position_meters": dict(cube["location_meters"]),
                "rotation_euler_radians": dict(cube["rotation_euler_radians"]),
                "scale": dict(cube["scale"]),
                "dimensions_meters": dict(cube["expected_dimensions_meters"]),
            }
        ],
    }


def test_spec_mismatches_accepts_a_conforming_digest():
    assert spec_mismatches(_digest_matching_spec()) == []


@pytest.mark.parametrize(
    "mutate,expected_text",
    [
        (lambda d: d["scene"].__setitem__("length_unit", "CENTIMETERS"), "length_unit"),
        (lambda d: d["scene"].__setitem__("unit_system", "IMPERIAL"), "unit_system"),
        (lambda d: d["scene"].__setitem__("scale_length", 0.01), "scale_length"),
        (lambda d: d["objects"][0].__setitem__("object_id", "obj_other"), "object_id"),
        (lambda d: d["objects"][0].__setitem__("name", "Cube.001"), "objects"),
        (
            lambda d: d["objects"][0]["world_position_meters"].__setitem__("x", 0.5),
            "position x",
        ),
        (
            lambda d: d["objects"][0]["dimensions_meters"].__setitem__("z", 4.0),
            "dimension z",
        ),
        (lambda d: d["objects"].clear(), "missing object"),
    ],
)
def test_spec_mismatches_detects_drift(mutate, expected_text):
    digest = _digest_matching_spec()
    mutate(digest)
    problems = spec_mismatches(digest)
    assert problems, "drift must be reported"
    assert any(expected_text in problem for problem in problems), problems


def test_scene_state_digest_ignores_key_order():
    a = {"digest": _digest_matching_spec()}
    reordered = json.loads(json.dumps(a["digest"]))
    reordered["objects"][0] = dict(
        reversed(list(reordered["objects"][0].items()))
    )
    assert scene_state_digest(a) == scene_state_digest({"digest": reordered})


def test_scene_state_digest_changes_when_position_changes():
    a = {"digest": _digest_matching_spec()}
    b = {"digest": _digest_matching_spec()}
    b["digest"]["objects"][0]["world_position_meters"]["x"] = 0.5
    assert scene_state_digest(a) != scene_state_digest(b)


def test_file_digest_detects_a_single_byte_change(tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"seed")
    before = file_digest(target)
    target.write_bytes(b"seee")
    assert file_digest(target) != before
