"""Real-Blender integration test for move_object (Spec 001, Task 4).

Marked `blender` and excluded from the default suite, because it launches a real
Blender process. Run explicitly with:

    python3 -m pytest -m blender

Proves, in an actual .blend scene:
  - Cube starts at (0, 0, 0)
  - the move operation requests +0.50 m on X and the Cube ends at (0.50, 0, 0)
  - replaying the SAME plan leaves it at (0.50, 0, 0), NOT 1.00
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from blender_mcp.blender_runtime import (
    blender_version,
    find_blender_executable,
    run_blender_script,
)
from blender_mcp.tolerance import coordinates_equal

pytestmark = pytest.mark.blender

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).parent / "blender_move_object_script.py"

STUDIO_PATHS = [
    REPO_ROOT / "packages" / "types" / "python",
    REPO_ROOT / "packages" / "contracts" / "python",
    REPO_ROOT / "packages" / "validation" / "python",
    REPO_ROOT / "packages" / "spatial" / "python",
    REPO_ROOT / "services" / "blender-mcp",
]

RESULT_PREFIX = "RESULT_JSON:"


@pytest.fixture(scope="module")
def blender_phases() -> dict:
    """Run the Blender script once and parse its emitted phases."""
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")

    proc = run_blender_script(
        str(SCRIPT),
        env={
            "STUDIO_PYTHONPATH": os.pathsep.join(str(p) for p in STUDIO_PATHS),
        },
        timeout=600,
    )

    phases: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            payload = json.loads(line[len(RESULT_PREFIX) :])
            phases[payload.pop("phase")] = payload

    if "done" not in phases:
        pytest.fail(
            "Blender run did not complete.\n"
            f"exit={proc.returncode}\n"
            f"stdout tail:\n{proc.stdout[-4000:]}\n"
            f"stderr tail:\n{proc.stderr[-4000:]}"
        )
    return phases


def test_blender_is_available_and_reports_a_version():
    assert find_blender_executable() is not None
    version = blender_version()
    assert version is not None and "Blender" in version


def test_cube_starts_at_the_origin(blender_phases):
    x, y, z = blender_phases["start"]["position"]
    assert coordinates_equal(x, 0.0)
    assert coordinates_equal(y, 0.0)
    assert coordinates_equal(z, 0.0)


def test_first_execution_moves_the_cube_to_half_a_metre_on_x(blender_phases):
    phase = blender_phases["first"]
    result = phase["result"]

    assert result["applied"] is True
    assert result["already_applied"] is False
    assert result["verified"] is True
    assert "error" not in result

    x, y, z = phase["scene_position"]
    assert coordinates_equal(x, 0.5), f"expected 0.50, got {x}"
    assert coordinates_equal(y, 0.0)
    assert coordinates_equal(z, 0.0)


def test_replaying_the_same_plan_does_not_move_the_cube_again(blender_phases):
    """The crash/retry case: must remain 0.50, never 1.00."""
    phase = blender_phases["second"]
    result = phase["result"]

    assert result["already_applied"] is True
    assert result["applied"] is False
    assert result["verified"] is True

    x, y, z = phase["scene_position"]
    assert coordinates_equal(x, 0.5), f"expected 0.50 after retry, got {x}"
    assert not coordinates_equal(x, 1.0), "double movement occurred"


def test_a_third_replay_is_still_stable(blender_phases):
    phase = blender_phases["third"]
    assert phase["result"]["already_applied"] is True
    x, _, _ = phase["scene_position"]
    assert coordinates_equal(x, 0.5)


def test_stable_object_id_resolves_in_a_real_blend(blender_phases):
    phase = blender_phases["resolve_by_id"]
    assert phase["found"] is True
    assert phase["name"] == "Cube"


def test_resubmitting_a_completed_plan_is_recognised_as_already_applied(
    blender_phases,
):
    """A second job carrying the same completed plan must not move anything."""
    result = blender_phases["conflict_after_move"]["result"]
    assert result["already_applied"] is True
    assert result["applied"] is False


def test_a_real_scene_conflict_refuses_to_mutate(blender_phases):
    """Something else moved the Cube to 0.25; the stale plan must not apply."""
    phase = blender_phases["true_conflict"]
    result = phase["result"]

    assert result["error"]["code"] == "PRECONDITION_MISMATCH"
    assert result["applied"] is False
    assert result["already_applied"] is False
    assert result["verified"] is False

    x, _, _ = phase["scene_position"]
    assert coordinates_equal(x, 0.25), "the scene must be left untouched"
    assert not coordinates_equal(x, 0.5)
    assert not coordinates_equal(x, 0.75), "delta must not be applied from 0.25"


def test_z_axis_movement_works_in_world_space(blender_phases):
    phase = blender_phases["z_move"]
    result = phase["result"]
    assert result["applied"] is True
    assert result["verified"] is True

    x, y, z = phase["scene_position"]
    assert coordinates_equal(x, 0.5)
    assert coordinates_equal(y, 0.0)
    assert coordinates_equal(z, 2.4), f"expected 2.40, got {z}"


def test_missing_object_reports_object_not_found_in_real_blender(blender_phases):
    result = blender_phases["missing_object"]["result"]
    assert result["error"]["code"] == "OBJECT_NOT_FOUND"
    assert result["applied"] is False
