"""Real-Blender seed fixture tests (Spec 001, Task 5).

Marked `blender` and excluded from the default suite. Run explicitly with:

    python3 -m pytest -m blender

Proves the ten required properties: the fixture generates, opens headlessly, is
configured in meters, contains the expected Cube identity and stable object_id at
the origin, that move_object works against an isolated working copy, that a fresh
copy starts at X=0 again, that the canonical fixture is never mutated, and that
generation is deterministic with respect to the scene state Spec 001 relies on.
"""

from __future__ import annotations

import pytest
from blender_mcp.blender_runtime import find_blender_executable
from blender_mcp.tolerance import coordinates_equal
from studio_fixtures.seed_project import (
    expected_object,
    file_digest,
    generate_seed_project,
    inspect_blend,
    load_spec,
    move_on_blend,
    scene_state_digest,
    seed_blend_path,
    spec_mismatches,
    working_copy,
)

pytestmark = pytest.mark.blender

SPEC = load_spec()
CUBE = expected_object("Cube")


def spec_001_plan(job_id: str = "job_seed_1") -> dict:
    """The Spec 001 operation: Cube 0 -> +0.50 m on X."""
    return {
        "job_id": job_id,
        "target": {"name": "Cube"},
        "expected_before_meters": {"x": 0.0, "y": 0.0, "z": 0.0},
        "delta_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
        "desired_after_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
    }


@pytest.fixture(scope="module")
def canonical_fixture():
    """Generate the canonical fixture once and record its digest."""
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")
    path = generate_seed_project()
    return {"path": path, "sha256": file_digest(path)}


@pytest.fixture(scope="module")
def inspection(canonical_fixture):
    return inspect_blend(canonical_fixture["path"])


# ---------------------------------------------------------------------------
# 1. Generation  2. Opens headlessly
# ---------------------------------------------------------------------------


def test_1_seed_project_can_be_generated(canonical_fixture):
    path = canonical_fixture["path"]
    assert path.exists()
    assert path.name == SPEC["blend_filename"]
    assert path.stat().st_size > 0


def test_2_generated_project_opens_headlessly(inspection):
    """inspect_blend opens the file with bpy.ops.wm.open_mainfile."""
    assert inspection["digest"]["scene"]["name"] == "Scene"


def test_2_fixture_matches_its_machine_readable_spec(inspection):
    problems = spec_mismatches(inspection["digest"])
    assert problems == [], problems


# ---------------------------------------------------------------------------
# 3. Units are meters
# ---------------------------------------------------------------------------


def test_3_scene_units_are_meters(inspection):
    scene = inspection["digest"]["scene"]
    assert scene["unit_system"] == "METRIC"
    assert scene["length_unit"] == "METERS"
    assert scene["scale_length"] == 1.0


# ---------------------------------------------------------------------------
# 4. Cube exists  5. Stable object_id  6. Starts at the origin
# ---------------------------------------------------------------------------


def test_4_scene_contains_exactly_the_cube(inspection):
    names = [entry["name"] for entry in inspection["digest"]["objects"]]
    assert names == ["Cube"]


def test_5_cube_has_the_expected_stable_object_id(inspection):
    cube = inspection["digest"]["objects"][0]
    assert cube["object_id"] == "obj_cube001"
    assert cube["object_id"] == CUBE["object_id"]


def test_5_cube_resolves_by_name_and_by_object_id(inspection):
    resolution = inspection["resolution"]
    assert resolution["by_name"]["name"] == "Cube"
    assert resolution["by_name"]["object_id"] == "obj_cube001"
    assert resolution["by_object_id"]["name"] == "Cube"


def test_5_cube_is_movable(inspection):
    assert inspection["resolution"]["by_name"]["movable"] is True


def test_6_cube_starts_at_world_origin(inspection):
    position = inspection["digest"]["objects"][0]["world_position_meters"]
    assert coordinates_equal(position["x"], 0.0)
    assert coordinates_equal(position["y"], 0.0)
    assert coordinates_equal(position["z"], 0.0)


def test_6_cube_has_identity_transform_and_expected_size(inspection):
    cube = inspection["digest"]["objects"][0]
    for axis in ("x", "y", "z"):
        assert coordinates_equal(cube["rotation_euler_radians"][axis], 0.0)
        assert coordinates_equal(cube["scale"][axis], 1.0)
        assert coordinates_equal(
            cube["dimensions_meters"][axis],
            CUBE["expected_dimensions_meters"][axis],
        )


# ---------------------------------------------------------------------------
# 7. move_object works against a working copy
# ---------------------------------------------------------------------------


def test_7_move_object_moves_the_cube_in_a_working_copy(canonical_fixture):
    with working_copy() as copy_path:
        phases = move_on_blend(copy_path, spec_001_plan())

        before = phases["before"]["position"]
        assert coordinates_equal(before[0], 0.0), "copy must start at X=0"

        result = phases["moved"]["result"]
        assert result["applied"] is True
        assert result["verified"] is True
        assert "error" not in result

        after = phases["moved"]["position"]
        assert coordinates_equal(after[0], 0.5), f"expected 0.50, got {after[0]}"
        assert coordinates_equal(after[1], 0.0)
        assert coordinates_equal(after[2], 0.0)

        assert "saved" in phases, "the working copy was saved"


def test_7_saved_working_copy_persists_the_move(canonical_fixture):
    """Re-open the saved copy to prove the mutation was written to disk."""
    with working_copy() as copy_path:
        move_on_blend(copy_path, spec_001_plan())
        reopened = inspect_blend(copy_path)
        position = reopened["digest"]["objects"][0]["world_position_meters"]
        assert coordinates_equal(position["x"], 0.5)


def test_7_retry_on_the_saved_copy_is_still_safe(canonical_fixture):
    """The Task 4 retry guarantee, now across a save/reopen boundary."""
    with working_copy() as copy_path:
        move_on_blend(copy_path, spec_001_plan())
        phases = move_on_blend(copy_path, spec_001_plan())

        result = phases["moved"]["result"]
        assert result["already_applied"] is True
        assert result["applied"] is False

        position = phases["moved"]["position"]
        assert coordinates_equal(position[0], 0.5), "must stay 0.50, not 1.00"


# ---------------------------------------------------------------------------
# 8. A fresh working copy starts at X=0 again
# ---------------------------------------------------------------------------


def test_8_a_fresh_working_copy_starts_at_x_zero_again(canonical_fixture):
    """No state may leak between runs; each copy starts from the seed state."""
    with working_copy() as first:
        moved = move_on_blend(first, spec_001_plan("job_run_1"))
        assert coordinates_equal(moved["moved"]["position"][0], 0.5)

    with working_copy() as second:
        fresh = inspect_blend(second)
        position = fresh["digest"]["objects"][0]["world_position_meters"]
        assert coordinates_equal(position["x"], 0.0), "fresh copy must be X=0"


def test_8_three_sequential_runs_each_start_from_zero(canonical_fixture):
    for index in range(3):
        with working_copy() as copy_path:
            phases = move_on_blend(copy_path, spec_001_plan(f"job_run_{index}"))
            assert coordinates_equal(phases["before"]["position"][0], 0.0)
            assert coordinates_equal(phases["moved"]["position"][0], 0.5)


def test_8_working_copies_are_isolated_from_each_other(canonical_fixture):
    with working_copy() as first:
        move_on_blend(first, spec_001_plan("job_a"))
        with working_copy() as second:
            other = inspect_blend(second)
            position = other["digest"]["objects"][0]["world_position_meters"]
            assert coordinates_equal(position["x"], 0.0)


def test_8_working_copy_is_removed_afterwards(canonical_fixture):
    with working_copy() as copy_path:
        assert copy_path.exists()
        recorded = copy_path
    assert not recorded.exists(), "temporary working copy must be cleaned up"


# ---------------------------------------------------------------------------
# 9. The canonical fixture is never mutated
# ---------------------------------------------------------------------------


def test_9_canonical_fixture_is_unchanged_after_mutating_copies(
    canonical_fixture,
):
    before = canonical_fixture["sha256"]

    with working_copy() as copy_path:
        move_on_blend(copy_path, spec_001_plan("job_isolation"))

    after = file_digest(canonical_fixture["path"])
    assert after == before, "the canonical fixture was modified by a test"


def test_9_canonical_fixture_still_matches_its_spec(canonical_fixture):
    """Byte equality plus a semantic re-check of the canonical file."""
    problems = spec_mismatches(inspect_blend(canonical_fixture["path"])["digest"])
    assert problems == [], problems


def test_9_canonical_cube_is_still_at_the_origin(canonical_fixture):
    inspected = inspect_blend(canonical_fixture["path"])
    position = inspected["digest"]["objects"][0]["world_position_meters"]
    assert coordinates_equal(position["x"], 0.0)


# ---------------------------------------------------------------------------
# 10. Generation is deterministic for the state that matters
# ---------------------------------------------------------------------------


def test_10_two_generations_produce_the_same_scene_state(tmp_path):
    """The .blend binary is not byte-reproducible; the scene state is."""
    first_path = generate_seed_project(tmp_path / "first.blend")
    second_path = generate_seed_project(tmp_path / "second.blend")

    first = inspect_blend(first_path)
    second = inspect_blend(second_path)

    assert first["digest"] == second["digest"]
    assert scene_state_digest(first) == scene_state_digest(second)


def test_10_regeneration_restores_the_expected_state(tmp_path):
    """Regenerating after a mutation resets the scene, which is how a fixture
    is reset if it is ever left dirty."""
    target = tmp_path / "reset.blend"
    generate_seed_project(target)
    move_on_blend(target, spec_001_plan("job_dirty"))

    dirty = inspect_blend(target)["digest"]["objects"][0]["world_position_meters"]
    assert coordinates_equal(dirty["x"], 0.5)

    generate_seed_project(target)
    clean = inspect_blend(target)["digest"]["objects"][0]["world_position_meters"]
    assert coordinates_equal(clean["x"], 0.0), "regeneration must reset to X=0"


def test_10_generated_copy_elsewhere_matches_the_canonical_state(
    canonical_fixture, tmp_path
):
    elsewhere = generate_seed_project(tmp_path / "elsewhere.blend")
    assert scene_state_digest(inspect_blend(elsewhere)) == scene_state_digest(
        inspect_blend(canonical_fixture["path"])
    )
