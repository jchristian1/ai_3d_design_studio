"""Worker workflow tests — no Blender required (Spec 001, Task 6).

Uses FakeBlenderOperationExecutor, which runs the REAL Task 4 move_object service
against an in-memory scene, so the retry semantics exercised here are the
production semantics with only bpy replaced.

Covers the required behaviours 1-18, including the four crash windows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from blender_worker import phases
from blender_worker.blender_ops import (
    BlenderExecutionError,
    FakeBlenderOperationExecutor,
)
from blender_worker.executor import WorkerExecutor
from blender_worker.journal import (
    ExecutionRecord,
    FileSystemExecutionStore,
    JournalCorruptError,
)
from blender_worker.locks import FileLockProvider, LockConflictError
from blender_worker.registry import (
    MappingProjectRegistry,
    UnknownProjectError,
    UnsafeProjectIdError,
    assert_safe_project_id,
)
from studio_contracts import SCHEMA_FILES, load_schema, to_wire
from studio_contracts.jobs import create_move_object_job
from studio_types import ObjectRef, Vec3

PROJECT_ID = "proj_seed"
OTHER_PROJECT_ID = "proj_other"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def harness(tmp_path: Path):
    """A worker wired to fakes, with isolated runtime directories."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    blend_paths = {}
    for project_id in (PROJECT_ID, OTHER_PROJECT_ID):
        path = projects_root / f"{project_id}.blend"
        path.write_bytes(b"fake blend contents")
        blend_paths[project_id] = path

    registry = MappingProjectRegistry(projects_root, blend_paths)
    store = FileSystemExecutionStore(tmp_path / "runtime")
    locks = FileLockProvider(tmp_path / "runtime")
    blender = FakeBlenderOperationExecutor()
    for project_id, path in blend_paths.items():
        blender.seed_project(
            path, {"Cube": Vec3(0.0, 0.0, 0.0)}, {"Cube": "obj_cube001"}
        )

    executor = WorkerExecutor(
        store=store,
        locks=locks,
        projects=registry,
        blender=blender,
        recovery_root=tmp_path / "runtime" / "recovery",
    )
    return {
        "executor": executor,
        "store": store,
        "locks": locks,
        "registry": registry,
        "blender": blender,
        "paths": blend_paths,
        "tmp": tmp_path,
    }


def make_job(
    job_id: str = "job_1",
    request_id: str = "req_1",
    project_id: str = PROJECT_ID,
    delta: Vec3 = Vec3(0.5, 0.0, 0.0),
    operation_index: int = 0,
):
    result = create_move_object_job(
        job_id=job_id,
        project_id=project_id,
        session_id="sess_1",
        user_id="user_1",
        request_id=request_id,
        operation_index=operation_index,
        target=ObjectRef(name="Cube"),
        delta_meters=delta,
        created_at="2026-09-15T04:00:00Z",
    )
    assert result.ok, result.errors
    return result.job


def cube(harness, project_id: str = PROJECT_ID) -> Vec3:
    return harness["blender"].position_of(harness["paths"][project_id], "Cube")


# ---------------------------------------------------------------------------
# 1-3. First execution: plan, ordering, success
# ---------------------------------------------------------------------------


def test_1_first_execution_creates_and_persists_a_plan(harness):
    outcome = harness["executor"].execute(make_job())
    assert outcome.succeeded

    record = harness["store"].load(PROJECT_ID, "job_1")
    assert record is not None
    assert record.plan is not None
    assert record.plan["expected_before_meters"] == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert record.plan["desired_after_meters"] == {"x": 0.5, "y": 0.0, "z": 0.0}
    assert record.plan["delta_meters"] == {"x": 0.5, "y": 0.0, "z": 0.0}


def test_1_persisted_plan_satisfies_the_canonical_schema(harness):
    from studio_contracts import validate_against_schema

    harness["executor"].execute(make_job())
    record = harness["store"].load(PROJECT_ID, "job_1")
    validated = validate_against_schema(SCHEMA_FILES["MoveObjectPlan"], record.plan)
    assert validated.valid, validated.violations


def test_2_plan_is_persisted_before_the_mutation_executor_is_called(harness):
    """Ordering is the whole safety argument; assert it directly."""
    observed: list[str] = []
    store = harness["store"]
    blender = harness["blender"]

    original_save = store.save

    def tracking_save(record):
        if record.plan is not None:
            observed.append(f"plan_saved:{record.phase}")
        original_save(record)

    original_move = blender.execute_move

    def tracking_move(project_path, plan):
        observed.append("mutation")
        return original_move(project_path, plan)

    store.save = tracking_save  # type: ignore[assignment]
    blender.execute_move = tracking_move  # type: ignore[assignment]

    harness["executor"].execute(make_job())

    assert "mutation" in observed
    assert observed.index(f"plan_saved:{phases.PLAN_PERSISTED}") < observed.index(
        "mutation"
    ), observed


def test_3_successful_execution_reaches_completed_and_succeeded(harness):
    outcome = harness["executor"].execute(make_job())
    assert outcome.job_status == "succeeded"
    assert outcome.phase == phases.COMPLETED
    assert outcome.applied is True
    assert outcome.already_applied is False
    assert cube(harness) == Vec3(0.5, 0.0, 0.0)

    record = harness["store"].load(PROJECT_ID, "job_1")
    assert record.phase == phases.COMPLETED
    assert record.job_status == "succeeded"
    assert record.error is None


def test_3_internal_phase_is_separate_from_public_job_status(harness):
    """The public Job lifecycle must not be overloaded with worker internals."""
    harness["executor"].execute(make_job())
    record = harness["store"].load(PROJECT_ID, "job_1")
    job_statuses = set(load_schema(SCHEMA_FILES["Job"])["properties"]["status"]["enum"])
    assert record.phase not in job_statuses
    assert record.job_status in job_statuses


# ---------------------------------------------------------------------------
# 4. WINDOW A — crash after plan persisted, before mutation
# ---------------------------------------------------------------------------


def test_4_window_a_retry_reuses_exactly_the_same_plan(harness):
    """Plan persisted, then the process dies before any mutation."""
    job = make_job()
    store, blender = harness["store"], harness["blender"]

    # Simulate the crash: the mutation raises, leaving the plan on disk.
    blender.fail_move_with = "simulated crash before mutation"
    first = harness["executor"].execute(job)
    assert first.job_status == "failed"

    record_after_crash = store.load(PROJECT_ID, "job_1")
    plan_before = json.dumps(record_after_crash.plan, sort_keys=True)
    assert cube(harness) == Vec3(0.0, 0.0, 0.0), "no mutation occurred"

    # Retry succeeds and must reuse the identical plan.
    blender.fail_move_with = None
    retry = harness["executor"].execute(job)
    assert retry.succeeded

    record_after_retry = store.load(PROJECT_ID, "job_1")
    assert json.dumps(record_after_retry.plan, sort_keys=True) == plan_before
    assert cube(harness) == Vec3(0.5, 0.0, 0.0)


def test_4_retry_never_re_reads_the_scene_to_replan(harness):
    """Once a plan exists, read_object_position must not be used for planning."""
    job = make_job()
    blender = harness["blender"]

    blender.fail_move_with = "crash"
    harness["executor"].execute(job)

    blender.fail_move_with = None
    blender.calls.clear()
    harness["executor"].execute(job)

    reads_before_move = []
    for kind, _ in blender.calls:
        if kind == "move":
            break
        reads_before_move.append(kind)
    assert reads_before_move == [], (
        "a retry must not read the scene before mutating; it would replan"
    )


# ---------------------------------------------------------------------------
# 5. WINDOW B — mutation and save succeeded, completion not recorded
# ---------------------------------------------------------------------------


def test_5_window_b_retry_after_unrecorded_success_does_not_double_move(harness):
    job = make_job()
    blender = harness["blender"]

    # The mutation lands and the fake "saves", then the worker dies.
    blender.crash_after_move = True
    first = harness["executor"].execute(job)
    assert first.job_status == "failed"
    assert cube(harness) == Vec3(0.5, 0.0, 0.0), "the mutation did land"

    record = harness["store"].load(PROJECT_ID, "job_1")
    assert record.phase == phases.FAILED
    assert record.plan is not None

    # Retry: Task 4 must see current == desired_after.
    blender.crash_after_move = False
    retry = harness["executor"].execute(job)

    assert retry.succeeded
    assert retry.already_applied is True
    assert retry.applied is False
    assert cube(harness) == Vec3(0.5, 0.0, 0.0), "must remain 0.50, not 1.00"


def test_5_repeated_retries_after_window_b_stay_stable(harness):
    job = make_job()
    harness["blender"].crash_after_move = True
    harness["executor"].execute(job)
    harness["blender"].crash_after_move = False

    for _ in range(3):
        outcome = harness["executor"].execute(job)
        assert outcome.succeeded
        assert cube(harness) == Vec3(0.5, 0.0, 0.0)


# ---------------------------------------------------------------------------
# 6. WINDOW C — external scene change
# ---------------------------------------------------------------------------


def test_6_window_c_external_change_yields_precondition_mismatch(harness):
    job = make_job()
    blender = harness["blender"]

    blender.fail_move_with = "crash before mutation"
    harness["executor"].execute(job)
    blender.fail_move_with = None

    # Something else moved the Cube while the plan was pending.
    blender.scenes[str(harness["paths"][PROJECT_ID])]["Cube"] = Vec3(0.25, 0.0, 0.0)

    outcome = harness["executor"].execute(job)

    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "PRECONDITION_MISMATCH"
    assert cube(harness) == Vec3(0.25, 0.0, 0.0), "no guessing, no mutation"


def test_6_conflict_does_not_apply_the_delta_from_the_unexpected_position(harness):
    job = make_job()
    harness["blender"].fail_move_with = "crash"
    harness["executor"].execute(job)
    harness["blender"].fail_move_with = None
    harness["blender"].scenes[str(harness["paths"][PROJECT_ID])]["Cube"] = Vec3(
        0.25, 0.0, 0.0
    )

    harness["executor"].execute(job)
    assert cube(harness) != Vec3(0.75, 0.0, 0.0)


# ---------------------------------------------------------------------------
# 7-9. Idempotency
# ---------------------------------------------------------------------------


def test_7_same_job_delivered_twice_executes_once(harness):
    job = make_job()
    first = harness["executor"].execute(job)
    assert first.succeeded and first.duplicate is False

    move_calls_before = sum(1 for kind, _ in harness["blender"].calls if kind == "move")
    second = harness["executor"].execute(job)

    assert second.succeeded
    assert second.duplicate is True
    move_calls_after = sum(1 for kind, _ in harness["blender"].calls if kind == "move")
    assert move_calls_after == move_calls_before, "no second mutation"
    assert cube(harness) == Vec3(0.5, 0.0, 0.0)


def test_8_different_job_id_with_same_mutation_identity_does_not_mutate_twice(
    harness,
):
    """The worker does not trust the queue to have deduplicated."""
    first_job = make_job(job_id="job_1", request_id="req_same")
    second_job = make_job(job_id="job_2", request_id="req_same")
    assert first_job.idempotency_key == second_job.idempotency_key

    assert harness["executor"].execute(first_job).succeeded
    moves_before = sum(1 for kind, _ in harness["blender"].calls if kind == "move")

    outcome = harness["executor"].execute(second_job)

    assert outcome.duplicate is True
    moves_after = sum(1 for kind, _ in harness["blender"].calls if kind == "move")
    assert moves_after == moves_before, "the same mutation must not run twice"
    assert cube(harness) == Vec3(0.5, 0.0, 0.0)


def test_9_two_request_identities_with_identical_payloads_both_execute(harness):
    """A user may legitimately repeat a command."""
    first = make_job(job_id="job_1", request_id="req_first")
    second = make_job(job_id="job_2", request_id="req_second")
    assert first.idempotency_key != second.idempotency_key
    assert to_wire(first.payload) == to_wire(second.payload)

    assert harness["executor"].execute(first).succeeded
    assert cube(harness) == Vec3(0.5, 0.0, 0.0)

    outcome = harness["executor"].execute(second)
    assert outcome.succeeded
    assert outcome.applied is True
    # The second plan was made from the then-current 0.50.
    assert outcome.plan["expected_before_meters"]["x"] == 0.5
    assert outcome.plan["desired_after_meters"]["x"] == 1.0
    assert cube(harness) == Vec3(1.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# 10-11. Locking
# ---------------------------------------------------------------------------


def test_10_same_project_cannot_be_mutated_concurrently(harness):
    locks = harness["locks"]
    with locks.hold(PROJECT_ID):
        outcome = harness["executor"].execute(make_job())
    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "LOCK_CONFLICT"
    assert cube(harness) == Vec3(0.0, 0.0, 0.0), "locked out, so unchanged"


def test_11_different_projects_do_not_block_each_other(harness):
    locks = harness["locks"]
    with locks.hold(OTHER_PROJECT_ID):
        outcome = harness["executor"].execute(make_job(project_id=PROJECT_ID))
    assert outcome.succeeded
    assert cube(harness, PROJECT_ID) == Vec3(0.5, 0.0, 0.0)


def test_10_lock_is_released_after_execution(harness):
    harness["executor"].execute(make_job())
    with harness["locks"].hold(PROJECT_ID):
        pass  # must not raise


def test_10_lock_is_released_even_when_execution_fails(harness):
    harness["blender"].fail_move_with = "boom"
    harness["executor"].execute(make_job())
    with harness["locks"].hold(PROJECT_ID):
        pass


def test_10_lock_conflict_raises_for_a_second_holder(harness):
    with harness["locks"].hold(PROJECT_ID):
        with pytest.raises(LockConflictError):
            with harness["locks"].hold(PROJECT_ID):
                pass


# ---------------------------------------------------------------------------
# 12-13. Recovery
# ---------------------------------------------------------------------------


def test_12_recovery_copy_exists_before_mutation(harness):
    recovery_paths: list[str] = []
    blender = harness["blender"]
    original_move = blender.execute_move

    def check_then_move(project_path, plan):
        record = harness["store"].load(PROJECT_ID, "job_1")
        assert record.recovery is not None, "recovery must precede mutation"
        assert Path(record.recovery["path"]).exists()
        recovery_paths.append(record.recovery["path"])
        return original_move(project_path, plan)

    blender.execute_move = check_then_move  # type: ignore[assignment]
    assert harness["executor"].execute(make_job()).succeeded
    assert recovery_paths


def test_12_recovery_copy_matches_the_pre_mutation_project(harness):
    project_path = harness["paths"][PROJECT_ID]
    original_bytes = project_path.read_bytes()
    harness["executor"].execute(make_job())

    record = harness["store"].load(PROJECT_ID, "job_1")
    assert Path(record.recovery["path"]).read_bytes() == original_bytes


def test_13_failure_preserves_recovery_evidence(harness):
    harness["blender"].fail_move_with = "mutation exploded"
    outcome = harness["executor"].execute(make_job())

    assert outcome.job_status == "failed"
    assert outcome.recovery_path is not None
    assert Path(outcome.recovery_path).exists(), "recovery must not be erased"

    record = harness["store"].load(PROJECT_ID, "job_1")
    assert record.recovery is not None
    assert record.error["code"] == "MUTATION_FAILED"


def test_13_earlier_recovery_evidence_is_not_overwritten(harness):
    """Each attempt keeps its own snapshot."""
    job = make_job()
    harness["blender"].fail_move_with = "crash 1"
    harness["executor"].execute(job)
    first_path = harness["store"].load(PROJECT_ID, "job_1").recovery["path"]

    # A different job on the same project produces a separate snapshot.
    harness["blender"].fail_move_with = "crash 2"
    harness["executor"].execute(make_job(job_id="job_2", request_id="req_2"))
    second_path = harness["store"].load(PROJECT_ID, "job_2").recovery["path"]

    assert first_path != second_path
    assert Path(first_path).exists() and Path(second_path).exists()


def test_recovery_files_live_outside_the_committed_fixture_tree(harness):
    harness["executor"].execute(make_job())
    record = harness["store"].load(PROJECT_ID, "job_1")
    assert "tests/fixtures" not in record.recovery["path"]


# ---------------------------------------------------------------------------
# 14-16. Rejections
# ---------------------------------------------------------------------------


def test_14_unknown_project_id_is_rejected_safely(harness):
    outcome = harness["executor"].execute(make_job(project_id="proj_missing"))
    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "VALIDATION_ERROR"
    assert "no project registered" in outcome.error["message"]
    # Nothing was locked, planned or mutated.
    assert harness["store"].load("proj_missing", "job_1") is None


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "..", ".", "/etc/passwd", "proj/../../x", "a/b", ""],
)
def test_15_job_cannot_inject_a_filesystem_path(harness, hostile):
    """A hostile project_id must never reach the filesystem."""
    with pytest.raises((UnsafeProjectIdError, UnknownProjectError)):
        harness["registry"].blend_path_for(hostile)


def test_15_job_schema_has_no_path_field():
    """The contract itself makes a path unrepresentable."""
    schema = load_schema(SCHEMA_FILES["Job"])
    assert schema["additionalProperties"] is False
    for field in schema["properties"]:
        assert "path" not in field
    payload = load_schema(SCHEMA_FILES["MoveObjectPayload"])
    assert payload["additionalProperties"] is False
    assert set(payload["properties"]) == {"target", "delta_meters"}


def test_15_registry_refuses_to_register_outside_its_root(tmp_path):
    registry = MappingProjectRegistry(tmp_path / "allowed")
    (tmp_path / "allowed").mkdir()
    outside = tmp_path / "outside.blend"
    outside.write_bytes(b"x")
    with pytest.raises(Exception):
        registry.register("proj_x", outside)


def test_15_safe_project_id_helper_accepts_only_plain_segments():
    assert assert_safe_project_id("proj_seed") == "proj_seed"
    for hostile in ["../x", "a/b", "..", ".", "", "  "]:
        with pytest.raises(UnsafeProjectIdError):
            assert_safe_project_id(hostile)


def test_16_unsupported_job_type_is_rejected(harness):
    job = to_wire(make_job())
    job["job_type"] = "resize_object"
    outcome = harness["executor"].execute(job)
    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "VALIDATION_ERROR"
    assert cube(harness) == Vec3(0.0, 0.0, 0.0)


def test_16_invalid_job_is_rejected_before_any_work(harness):
    job = to_wire(make_job())
    del job["project_id"]
    outcome = harness["executor"].execute(job)
    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 17-18. Journal integrity
# ---------------------------------------------------------------------------


def test_17_corrupt_execution_record_is_refused_not_ignored(harness):
    """Treating a corrupt record as absent would cause a replan and double move."""
    job = make_job()
    harness["blender"].fail_move_with = "crash"
    harness["executor"].execute(job)
    harness["blender"].fail_move_with = None

    path = harness["store"]._execution_path(PROJECT_ID, "job_1")
    path.write_text("{ this is not valid json", encoding="utf-8")

    outcome = harness["executor"].execute(job)
    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "INTERNAL_ERROR"
    assert "journal" in outcome.error["message"]
    assert cube(harness) == Vec3(0.0, 0.0, 0.0), "must not mutate on a corrupt journal"


def test_17_truncated_record_is_detected(harness):
    store = harness["store"]
    record = ExecutionRecord(
        job_id="job_x",
        project_id=PROJECT_ID,
        idempotency_key="idem_x",
        job_type="move_object",
        phase=phases.RECEIVED,
    )
    store.save(record)
    path = store._execution_path(PROJECT_ID, "job_x")
    wire = json.loads(path.read_text())
    del wire["phase"]
    path.write_text(json.dumps(wire), encoding="utf-8")

    with pytest.raises(JournalCorruptError):
        store.load(PROJECT_ID, "job_x")


def test_17_unknown_record_version_is_detected(harness):
    store = harness["store"]
    record = ExecutionRecord(
        job_id="job_v",
        project_id=PROJECT_ID,
        idempotency_key="idem_v",
        job_type="move_object",
        phase=phases.RECEIVED,
    )
    store.save(record)
    path = store._execution_path(PROJECT_ID, "job_v")
    wire = json.loads(path.read_text())
    wire["record_version"] = 999
    path.write_text(json.dumps(wire), encoding="utf-8")

    with pytest.raises(JournalCorruptError):
        store.load(PROJECT_ID, "job_v")


def test_17_unknown_phase_is_detected(harness):
    store = harness["store"]
    record = ExecutionRecord(
        job_id="job_p",
        project_id=PROJECT_ID,
        idempotency_key="idem_p",
        job_type="move_object",
        phase=phases.RECEIVED,
    )
    store.save(record)
    path = store._execution_path(PROJECT_ID, "job_p")
    wire = json.loads(path.read_text())
    wire["phase"] = "teleporting"
    path.write_text(json.dumps(wire), encoding="utf-8")
    with pytest.raises(JournalCorruptError):
        store.load(PROJECT_ID, "job_p")


def test_18_journal_writes_are_atomic_and_leave_no_temp_files(harness):
    store = harness["store"]
    record = ExecutionRecord(
        job_id="job_atomic",
        project_id=PROJECT_ID,
        idempotency_key="idem_atomic",
        job_type="move_object",
        phase=phases.RECEIVED,
    )
    store.save(record)
    directory = store._execution_path(PROJECT_ID, "job_atomic").parent
    assert [p.name for p in directory.iterdir()] == ["job_atomic.json"]


def test_18_failed_write_does_not_corrupt_an_existing_record(harness, monkeypatch):
    """A crash mid-write must leave the previous record intact."""
    store = harness["store"]
    record = ExecutionRecord(
        job_id="job_keep",
        project_id=PROJECT_ID,
        idempotency_key="idem_keep",
        job_type="move_object",
        phase=phases.PLAN_PERSISTED,
    )
    store.save(record)
    good = store._execution_path(PROJECT_ID, "job_keep").read_text()

    import blender_worker.journal as journal_module

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(journal_module.os, "replace", explode)
    record.phase = phases.EXECUTING
    with pytest.raises(OSError):
        store.save(record)

    # Old content survives, and no temp debris remains.
    assert store._execution_path(PROJECT_ID, "job_keep").read_text() == good
    directory = store._execution_path(PROJECT_ID, "job_keep").parent
    assert [p.name for p in directory.iterdir()] == ["job_keep.json"]


def test_journal_survives_a_new_store_instance(harness):
    """Durability across "process restart": a fresh store reads the record."""
    harness["blender"].fail_move_with = "crash"
    harness["executor"].execute(make_job())

    reopened = FileSystemExecutionStore(harness["tmp"] / "runtime")
    record = reopened.load(PROJECT_ID, "job_1")
    assert record is not None
    assert record.plan is not None


def test_idempotency_binding_is_project_scoped(harness):
    store = harness["store"]
    assert store.bind_idempotency(PROJECT_ID, "idem_shared", "job_a") == "job_a"
    assert store.bind_idempotency(OTHER_PROJECT_ID, "idem_shared", "job_b") == "job_b"
    assert store.job_id_for_idempotency(PROJECT_ID, "idem_shared") == "job_a"
    assert store.job_id_for_idempotency(OTHER_PROJECT_ID, "idem_shared") == "job_b"


# ---------------------------------------------------------------------------
# Durability of the save
# ---------------------------------------------------------------------------


def test_success_requires_the_saved_project_to_contain_the_new_position(harness):
    """In-memory success is not success."""
    blender = harness["blender"]
    original_read = blender.read_object_position
    calls = {"n": 0}

    def stale_read(project_path, target):
        calls["n"] += 1
        # First read (planning) is honest; the post-save read lies.
        if calls["n"] == 1:
            return original_read(project_path, target)
        return Vec3(0.0, 0.0, 0.0)

    blender.read_object_position = stale_read  # type: ignore[assignment]
    outcome = harness["executor"].execute(make_job())

    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "VERIFY_FAILED"


def test_object_missing_from_project_fails_before_planning(harness):
    harness["blender"].scenes[str(harness["paths"][PROJECT_ID])].clear()
    outcome = harness["executor"].execute(make_job())
    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "OBJECT_NOT_FOUND"
    assert harness["store"].load(PROJECT_ID, "job_1").plan is None


def test_worker_orchestration_contains_no_blender_or_lock_internals():
    """Layering check: orchestration must stay free of bpy and fcntl."""
    import ast

    source = (
        Path(__file__).resolve().parents[1] / "blender_worker" / "executor.py"
    ).read_text("utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert "bpy" not in modules
    assert "fcntl" not in modules
