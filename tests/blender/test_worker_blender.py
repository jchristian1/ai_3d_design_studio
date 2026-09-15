"""Real-Blender worker integration tests (Spec 001, Task 6).

Marked `blender` and excluded from the default suite. Run with:

    python3 -m pytest -m blender

Uses the Task 5 seed fixture through an isolated working copy, so the canonical
fixture is never mutated. Every position assertion is made by reopening the SAVED
.blend in a fresh Blender process, so "success" means durable on disk.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from blender_mcp.blender_runtime import find_blender_executable
from blender_mcp.tolerance import coordinates_equal
from blender_worker import phases
from blender_worker.blender_ops import SubprocessBlenderOperationExecutor
from blender_worker.executor import WorkerExecutor
from blender_worker.journal import FileSystemExecutionStore
from blender_worker.locks import FileLockProvider
from blender_worker.registry import MappingProjectRegistry
from studio_contracts.jobs import create_move_object_job
from studio_fixtures.seed_project import (
    ensure_seed_project,
    file_digest,
    inspect_blend,
    seed_blend_path,
)
from studio_types import ObjectRef, Vec3

pytestmark = pytest.mark.blender

PROJECT_ID = "proj_seed"


@pytest.fixture(scope="module")
def canonical_digest() -> str:
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")
    return file_digest(ensure_seed_project())


@pytest.fixture
def worker(tmp_path: Path, canonical_digest: str):
    """A worker whose registered project is an isolated copy of the seed."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_path = projects_root / "seed_project.blend"
    shutil.copy2(ensure_seed_project(), project_path)

    runtime_root = tmp_path / "runtime"
    executor = WorkerExecutor(
        store=FileSystemExecutionStore(runtime_root),
        locks=FileLockProvider(runtime_root),
        projects=MappingProjectRegistry(projects_root, {PROJECT_ID: project_path}),
        blender=SubprocessBlenderOperationExecutor(),
        recovery_root=runtime_root / "recovery",
    )
    return {
        "executor": executor,
        "store": FileSystemExecutionStore(runtime_root),
        "project_path": project_path,
        "runtime": runtime_root,
    }


def make_job(job_id: str, request_id: str, delta_x: float = 0.5):
    result = create_move_object_job(
        job_id=job_id,
        project_id=PROJECT_ID,
        session_id="sess_1",
        user_id="user_1",
        request_id=request_id,
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(delta_x, 0.0, 0.0),
        created_at="2026-09-15T04:00:00Z",
    )
    assert result.ok, result.errors
    return result.job


def saved_cube_x(project_path: Path) -> float:
    """Read Cube X from the SAVED file via a fresh Blender process."""
    digest = inspect_blend(project_path)["digest"]
    return digest["objects"][0]["world_position_meters"]["x"]


# ---------------------------------------------------------------------------
# A. Seed 0 -> worker executes +0.50 -> saved project has 0.50
# ---------------------------------------------------------------------------


def test_a_worker_moves_the_cube_and_the_save_is_durable(worker):
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.0)

    outcome = worker["executor"].execute(make_job("job_a", "req_a"))

    assert outcome.succeeded, outcome.error
    assert outcome.phase == phases.COMPLETED
    assert outcome.applied is True

    # Reopened in a fresh Blender process: this is durability, not memory.
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.5)


def test_a_execution_record_captures_the_plan_and_recovery(worker):
    worker["executor"].execute(make_job("job_a", "req_a"))
    record = worker["store"].load(PROJECT_ID, "job_a")

    assert record.phase == phases.COMPLETED
    assert record.job_status == "succeeded"
    assert record.plan["expected_before_meters"]["x"] == 0.0
    assert record.plan["desired_after_meters"]["x"] == 0.5
    assert Path(record.recovery["path"]).exists()


def test_a_recovery_copy_holds_the_pre_mutation_scene(worker):
    worker["executor"].execute(make_job("job_a", "req_a"))
    record = worker["store"].load(PROJECT_ID, "job_a")

    # The snapshot still has the Cube at the origin.
    assert coordinates_equal(saved_cube_x(Path(record.recovery["path"])), 0.0)
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.5)


# ---------------------------------------------------------------------------
# B. Same job delivered again -> still 0.50
# ---------------------------------------------------------------------------


def test_b_redelivering_the_same_job_leaves_the_cube_at_half_a_metre(worker):
    job = make_job("job_b", "req_b")
    assert worker["executor"].execute(job).succeeded
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.5)

    again = worker["executor"].execute(job)

    assert again.succeeded
    assert again.duplicate is True
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.5)


# ---------------------------------------------------------------------------
# C. Crash after save, before the completion record
# ---------------------------------------------------------------------------


def test_c_crash_after_save_before_completion_record_resolves_safely(worker):
    """The dangerous window: the mutation is durable but unrecorded."""
    job = make_job("job_c", "req_c")
    assert worker["executor"].execute(job).succeeded
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.5)

    # Rewind the journal to the executing phase, as a crash would have left it:
    # the plan is persisted, the mutation happened, completion was never written.
    store = worker["store"]
    record = store.load(PROJECT_ID, "job_c")
    plan_before = json.dumps(record.plan, sort_keys=True)
    record.phase = phases.EXECUTING
    record.job_status = "running"
    record.result = None
    store.save(record)

    retry = worker["executor"].execute(job)

    assert retry.succeeded, retry.error
    assert retry.already_applied is True, "must recognise the desired state"
    assert retry.applied is False, "must not mutate a second time"
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.5), (
        "must remain 0.50, not 1.00"
    )

    # The plan was reused verbatim, not recomputed from the current 0.50.
    reloaded = store.load(PROJECT_ID, "job_c")
    assert json.dumps(reloaded.plan, sort_keys=True) == plan_before
    assert reloaded.plan["expected_before_meters"]["x"] == 0.0


# ---------------------------------------------------------------------------
# D. A NEW intentional request moves again: 0.50 -> 1.00
# ---------------------------------------------------------------------------


def test_d_a_new_request_moves_the_cube_again_to_one_metre(worker):
    """Retry keeps 0.50; a genuinely new command reaches 1.00."""
    first = make_job("job_d1", "req_d1")
    assert worker["executor"].execute(first).succeeded
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.5)

    second = make_job("job_d2", "req_d2")
    assert second.idempotency_key != first.idempotency_key

    outcome = worker["executor"].execute(second)

    assert outcome.succeeded, outcome.error
    assert outcome.applied is True
    # The new plan was built from the then-current 0.50.
    assert outcome.plan["expected_before_meters"]["x"] == 0.5
    assert outcome.plan["desired_after_meters"]["x"] == 1.0
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 1.0)


def test_d_retry_versus_new_request_side_by_side(worker):
    """The distinction that matters, in one test.

    Same request retried  : 0.50 -> 0.50
    New intentional request: 0.50 -> 1.00
    """
    project_path = worker["project_path"]
    original = make_job("job_x", "req_x")

    assert worker["executor"].execute(original).succeeded
    assert coordinates_equal(saved_cube_x(project_path), 0.5)

    # Retry of the SAME request: no movement.
    retry = worker["executor"].execute(original)
    assert retry.succeeded
    assert coordinates_equal(saved_cube_x(project_path), 0.5), "retry must not move"

    # A NEW request with an identical payload: moves again.
    new_request = make_job("job_y", "req_y")
    assert worker["executor"].execute(new_request).succeeded
    assert coordinates_equal(saved_cube_x(project_path), 1.0), "new command must move"


# ---------------------------------------------------------------------------
# E. Isolation: a fresh copy always starts at 0
# ---------------------------------------------------------------------------


def test_e_a_fresh_working_copy_starts_at_zero(worker, tmp_path):
    assert worker["executor"].execute(make_job("job_e", "req_e")).succeeded
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.5)

    fresh = tmp_path / "fresh.blend"
    shutil.copy2(ensure_seed_project(), fresh)
    assert coordinates_equal(saved_cube_x(fresh), 0.0)


def test_e_canonical_fixture_is_unchanged_by_worker_runs(worker, canonical_digest):
    worker["executor"].execute(make_job("job_e2", "req_e2"))
    assert file_digest(seed_blend_path()) == canonical_digest


# ---------------------------------------------------------------------------
# Real-Blender failure handling
# ---------------------------------------------------------------------------


def test_external_scene_change_produces_a_precondition_mismatch(worker):
    """Window C in real Blender: the scene moved under a pending plan."""
    job = make_job("job_conflict", "req_conflict")
    store = worker["store"]
    executor = worker["executor"]

    # Produce a persisted plan (0 -> 0.5) without completing it.
    assert executor.execute(job).succeeded
    record = store.load(PROJECT_ID, "job_conflict")
    record.phase = phases.EXECUTING
    record.job_status = "running"
    store.save(record)

    # Something else moves the Cube to an unexpected place.
    conflicting = make_job("job_other", "req_other", delta_x=0.25)
    assert executor.execute(conflicting).succeeded
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.75)

    outcome = executor.execute(job)

    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "PRECONDITION_MISMATCH"
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.75), (
        "a conflict must not mutate"
    )


def test_unknown_project_is_rejected_without_touching_blender(worker):
    outcome = worker["executor"].execute(
        create_move_object_job(
            job_id="job_unknown",
            project_id="proj_not_registered",
            session_id="sess_1",
            user_id="user_1",
            request_id="req_unknown",
            target=ObjectRef(name="Cube"),
            delta_meters=Vec3(0.5, 0.0, 0.0),
            created_at="2026-09-15T04:00:00Z",
        ).job
    )
    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "VALIDATION_ERROR"
    assert coordinates_equal(saved_cube_x(worker["project_path"]), 0.0)
