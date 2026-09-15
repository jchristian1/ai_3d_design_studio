"""Applying a capability plan: per-step durability, retry safety, verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from blender_worker import phases
from blender_worker.backends.fake import FakeBlenderCapabilityProvider
from blender_worker.capability_executor import (
    ALREADY_APPLIED,
    APPLIED,
    CapabilityPlanExecutor,
    StepProgress,
)
from blender_worker.journal import FileSystemExecutionStore
from blender_worker.locks import FileLockProvider
from blender_worker.registry import MappingProjectRegistry
from studio_contracts.jobs import create_capability_job, to_job_wire
from studio_types import CapabilityOperation

PROJECT = "proj_test"

SAFE_CODE = "import bpy\nbpy.ops.mesh.primitive_cube_add(size=1.0)\nbpy.ops.wm.save_mainfile()\n"
RISKY_CODE = "import shutil\nshutil.rmtree('/home/christian')\n"


def wall(index: int, name: str, *, object_id: str | None = None, x_end: float = 4.0) -> CapabilityOperation:
    arguments: dict[str, Any] = {
        "display_name": name,
        "start_meters": {"x": 0.0, "y": 0.0},
        "end_meters": {"x": x_end, "y": 0.0},
        "height_meters": 2.4,
        "thickness_meters": 0.12,
    }
    if object_id:
        arguments["object_id"] = object_id
    return CapabilityOperation(
        operation_index=index, capability="create_wall", label=f"Creating {name}", arguments=arguments
    )


def move(index: int, *, object_id: str, x: float) -> CapabilityOperation:
    return CapabilityOperation(
        operation_index=index,
        capability="move_object",
        label="Moving it",
        arguments={"object_id": object_id, "desired_position_meters": {"x": x, "y": 0.0, "z": 0.0}},
    )


def code_step(index: int, code: str, *, token: str | None = None) -> CapabilityOperation:
    return CapabilityOperation(
        operation_index=index,
        capability="execute_blender_python",
        label="Running a custom step",
        arguments={"code": code},
        approval_token=token,
    )


@pytest.fixture()
def parts(tmp_path: Path):
    blend = tmp_path / "projects" / f"{PROJECT}.blend"
    blend.parent.mkdir(parents=True, exist_ok=True)
    blend.write_bytes(b"BLENDER-fake-project")

    provider = FakeBlenderCapabilityProvider()
    progress: list[StepProgress] = []
    executor = CapabilityPlanExecutor(
        store=FileSystemExecutionStore(tmp_path / "journal"),
        locks=FileLockProvider(tmp_path / "locks"),
        projects=MappingProjectRegistry(tmp_path / "projects", {PROJECT: blend}),
        provider=provider,
        recovery_root=tmp_path / "recovery",
        on_progress=progress.append,
    )
    return executor, provider, progress, tmp_path


def make_job(operations, *, request_id: str = "req_1", required_scene_version=None):
    created = create_capability_job(
        job_id=f"job_{request_id}_0",
        project_id=PROJECT,
        session_id="sess_1",
        user_id="user_dev_local",
        request_id=request_id,
        operations=operations,
        created_at="2026-01-01T00:00:00Z",
        required_scene_version=required_scene_version,
        summary="Building",
    )
    assert created.ok, [error.message for error in created.errors]
    return to_job_wire(created.job)


# --- the happy path -------------------------------------------------------


def test_a_plan_applies_every_step_and_completes(parts) -> None:
    executor, provider, progress, _ = parts
    outcome = executor.execute(
        make_job([wall(0, "Wall_A", object_id="obj_a"), wall(1, "Wall_B", object_id="obj_b")])
    )

    assert outcome.succeeded, outcome.error
    assert outcome.phase == phases.COMPLETED
    assert outcome.applied_steps == 2
    assert outcome.skipped_steps == 0
    assert set(provider.objects) == {"Wall_A", "Wall_B"}
    assert outcome.result["step_count"] == 2


def test_progress_is_reported_per_step_for_the_browser(parts) -> None:
    executor, _, progress, _ = parts
    executor.execute(make_job([wall(0, "Wall_A", object_id="obj_a"), wall(1, "Wall_B", object_id="obj_b")]))

    assert [(p.step_index, p.step_count) for p in progress] == [(0, 2), (1, 2)]
    assert progress[0].label == "Creating Wall_A"
    assert progress[0].snapshot()["label"] == "Creating Wall_A"


def test_the_resulting_scene_is_reported_so_the_agent_can_be_grounded(parts) -> None:
    executor, _, _, _ = parts
    outcome = executor.execute(make_job([wall(0, "Wall_A", object_id="obj_a")]))

    assert outcome.scene is not None
    assert outcome.scene["scene_version"].startswith("sha256:")
    assert [obj["name"] for obj in outcome.scene["objects"]] == ["Wall_A"]


def test_one_recovery_point_is_created_for_the_whole_plan(parts) -> None:
    executor, _, _, tmp_path = parts
    outcome = executor.execute(
        make_job([wall(0, "Wall_A", object_id="obj_a"), wall(1, "Wall_B", object_id="obj_b")])
    )
    assert outcome.recovery_path is not None
    copies = list((tmp_path / "recovery").rglob("*.blend"))
    assert len(copies) == 1, "a plan takes one snapshot, not one per step"


# --- retry safety ---------------------------------------------------------


def test_replaying_a_completed_plan_changes_nothing(parts) -> None:
    executor, provider, _, _ = parts
    job = make_job([wall(0, "Wall_A", object_id="obj_a")])

    first = executor.execute(job)
    assert first.applied_steps == 1

    second = executor.execute(job)
    assert second.succeeded
    assert second.duplicate is True
    assert second.already_applied is True
    assert len(provider.objects) == 1, "a replay must not build a second wall"


def test_a_retry_after_a_crash_resumes_instead_of_repeating(parts) -> None:
    """The property that stops "reconstruct this plan" producing eighteen walls."""
    executor, provider, _, _ = parts
    operations = [
        wall(0, "Wall_A", object_id="obj_a"),
        wall(1, "Wall_B", object_id="obj_b"),
        wall(2, "Wall_C", object_id="obj_c"),
    ]
    job = make_job(operations)

    # Crash partway: the third step fails.
    provider.fail_capability("create_wall", "MUTATION_FAILED", "Blender went away")
    provider.forced_failures.pop("create_wall")  # applied selectively below

    calls: list[str] = []
    original_invoke = provider.invoke

    def failing_invoke(request):
        if request.capability == "create_wall":
            calls.append(str(request.argument("display_name")))
            if len(calls) == 3:
                from blender_worker.capability.provider import CapabilityResult

                return CapabilityResult.failure(
                    request.capability, "MUTATION_FAILED", "Blender went away"
                )
        return original_invoke(request)

    provider.invoke = failing_invoke  # type: ignore[method-assign]
    crashed = executor.execute(job)
    assert not crashed.succeeded
    assert crashed.applied_steps == 2
    assert set(provider.objects) == {"Wall_A", "Wall_B"}

    # Recover: the same job runs again and only the missing wall is built.
    provider.invoke = original_invoke  # type: ignore[method-assign]
    resumed = executor.execute(job)
    assert resumed.succeeded, resumed.error
    assert set(provider.objects) == {"Wall_A", "Wall_B", "Wall_C"}

    # The load-bearing assertion: each wall was BUILT exactly once. Steps 0 and 1
    # were already journalled as applied, so the resume never invoked them again.
    # (They stay counted as `applied` rather than `already_applied`, which is
    # reserved for a step whose desired state was already true in the scene.)
    built = [
        request.argument("display_name")
        for request in provider.invocations
        if request.capability == "create_wall"
    ]
    assert built == ["Wall_A", "Wall_B", "Wall_C"], (
        f"each wall must be built exactly once across both attempts, got {built}"
    )
    assert resumed.applied_steps == 3


def test_a_step_whose_result_already_holds_is_skipped(parts) -> None:
    executor, provider, _, _ = parts
    provider.seed_cube(object_id="obj_cube")
    provider.objects["Cube"].position = (0.5, 0.0, 0.0)

    outcome = executor.execute(make_job([move(0, object_id="obj_cube", x=0.5)]))
    assert outcome.succeeded
    assert outcome.skipped_steps == 1
    assert outcome.applied_steps == 0
    # The capability was never invoked for that step.
    assert not any(
        request.capability == "move_object" for request in provider.invocations
    )


def test_an_absolute_move_applied_twice_does_not_double(parts) -> None:
    executor, provider, _, _ = parts
    provider.seed_cube(object_id="obj_cube")

    executor.execute(make_job([move(0, object_id="obj_cube", x=0.5)], request_id="req_a"))
    executor.execute(make_job([move(0, object_id="obj_cube", x=0.5)], request_id="req_b"))

    assert provider.objects["Cube"].position[0] == pytest.approx(0.5)


def test_a_creation_step_without_a_stable_id_is_not_assumed_duplicate(parts) -> None:
    """We do not guess: two walls with no ids may legitimately both belong."""
    executor, provider, _, _ = parts
    executor.execute(make_job([wall(0, "Wall_A")], request_id="req_a"))
    executor.execute(make_job([wall(0, "Wall_A")], request_id="req_b"))
    # Same display name overwrites in the fake scene, but the capability DID run twice.
    creations = [r for r in provider.invocations if r.capability == "create_wall"]
    assert len(creations) == 2


# --- scene-version precondition ------------------------------------------


def test_a_plan_reasoned_against_a_stale_scene_is_refused(parts) -> None:
    executor, provider, _, _ = parts
    provider.seed_cube(object_id="obj_cube")

    outcome = executor.execute(
        make_job([move(0, object_id="obj_cube", x=1.0)], required_scene_version="sha256:stale")
    )
    assert not outcome.succeeded
    assert outcome.error["code"] == "SCENE_VERSION_MISMATCH"
    assert provider.objects["Cube"].position[0] == pytest.approx(0.0), "nothing was modified"


def test_a_plan_with_a_matching_scene_version_proceeds(parts) -> None:
    executor, provider, _, _ = parts
    provider.seed_cube(object_id="obj_cube")

    from blender_worker.capability import normalize
    from blender_worker.capability.provider import CapabilityRequest

    read = provider.invoke(
        CapabilityRequest(
            capability="inspect_scene", project_id=PROJECT, project_path=Path("/tmp/x.blend")
        )
    )
    version = normalize.scene_snapshot_from_backend(PROJECT, read.data).snapshot.scene_version

    outcome = executor.execute(
        make_job([move(0, object_id="obj_cube", x=1.0)], required_scene_version=version)
    )
    assert outcome.succeeded, outcome.error


def test_a_resumed_plan_does_not_re_check_the_original_scene_version(parts) -> None:
    """Once a step has applied, the scene has legitimately moved on."""
    executor, provider, _, _ = parts
    from blender_worker.capability import normalize
    from blender_worker.capability.provider import CapabilityRequest, CapabilityResult

    read = provider.invoke(
        CapabilityRequest(
            capability="inspect_scene", project_id=PROJECT, project_path=Path("/tmp/x.blend")
        )
    )
    version = normalize.scene_snapshot_from_backend(PROJECT, read.data).snapshot.scene_version
    job = make_job(
        [wall(0, "Wall_A", object_id="obj_a"), wall(1, "Wall_B", object_id="obj_b")],
        required_scene_version=version,
    )

    original_invoke = provider.invoke
    seen: list[str] = []

    def failing(request):
        if request.capability == "create_wall":
            seen.append(str(request.argument("display_name")))
            if len(seen) == 2:
                return CapabilityResult.failure(request.capability, "MUTATION_FAILED", "boom")
        return original_invoke(request)

    provider.invoke = failing  # type: ignore[method-assign]
    assert not executor.execute(job).succeeded

    provider.invoke = original_invoke  # type: ignore[method-assign]
    resumed = executor.execute(job)
    assert resumed.succeeded, resumed.error


# --- verification ---------------------------------------------------------


def test_a_step_that_reports_success_but_did_nothing_fails_verification(parts) -> None:
    executor, provider, _, _ = parts
    provider.seed_cube(object_id="obj_cube")

    from blender_worker.capability.provider import CapabilityResult

    def lying(request):
        if request.capability == "move_object":
            return CapabilityResult.success(request.capability, {"status": "ok"})
        return FakeBlenderCapabilityProvider.invoke(provider, request)

    provider.invoke = lying  # type: ignore[method-assign]
    outcome = executor.execute(make_job([move(0, object_id="obj_cube", x=1.0)]))

    assert not outcome.succeeded
    assert outcome.error["code"] == "VERIFY_FAILED"
    assert "could not be confirmed" in outcome.error["message"]


# --- failure handling -----------------------------------------------------


def test_a_failing_step_stops_the_plan_and_leaves_earlier_steps_applied(parts) -> None:
    executor, provider, _, _ = parts
    provider.fail_capability("create_floor", "MUTATION_FAILED", "no good")

    outcome = executor.execute(
        make_job(
            [
                wall(0, "Wall_A", object_id="obj_a"),
                CapabilityOperation(
                    operation_index=1,
                    capability="create_floor",
                    label="Creating the floor",
                    arguments={
                        "display_name": "Floor",
                        "footprint_meters": [
                            {"x": 0.0, "y": 0.0},
                            {"x": 4.0, "y": 0.0},
                            {"x": 4.0, "y": 3.0},
                        ],
                        "thickness_meters": 0.2,
                    },
                ),
                wall(2, "Wall_C", object_id="obj_c"),
            ]
        )
    )
    assert not outcome.succeeded
    assert outcome.applied_steps == 1
    assert "Wall_A" in provider.objects
    assert "Wall_C" not in provider.objects, "later steps are not attempted"
    assert outcome.recovery_path is not None, "the recovery point is preserved"


def test_an_unavailable_backend_reports_cleanly(parts) -> None:
    executor, provider, _, _ = parts
    provider.available = False
    outcome = executor.execute(make_job([wall(0, "Wall_A", object_id="obj_a")]))
    assert not outcome.succeeded
    assert outcome.error["code"] == "BLENDER_UNAVAILABLE"


def test_an_unknown_project_is_refused(parts) -> None:
    executor, _, _, _ = parts
    job = make_job([wall(0, "Wall_A", object_id="obj_a")])
    job["project_id"] = "proj_unknown"
    job["payload"] = job["payload"]
    outcome = executor.execute(job)
    assert not outcome.succeeded
    assert outcome.error["code"] == "VALIDATION_ERROR"


def test_a_lock_conflict_is_reported_without_mutating(parts) -> None:
    executor, provider, _, tmp_path = parts
    lock = FileLockProvider(tmp_path / "locks")
    with lock.hold(PROJECT, timeout=0.0):
        outcome = executor.execute(make_job([wall(0, "Wall_A", object_id="obj_a")]))
    assert not outcome.succeeded
    assert outcome.error["code"] == "LOCK_CONFLICT"
    assert provider.objects == {}


def test_a_job_of_the_wrong_type_is_refused(parts) -> None:
    executor, _, _, _ = parts
    job = make_job([wall(0, "Wall_A", object_id="obj_a")])
    job["job_type"] = "move_object"
    outcome = executor.execute(job)
    assert not outcome.succeeded


# --- model-authored code -------------------------------------------------


def test_scene_only_code_runs(parts) -> None:
    executor, provider, _, _ = parts
    outcome = executor.execute(make_job([code_step(0, SAFE_CODE)]))
    assert outcome.succeeded, outcome.error
    assert provider.executed_code == [SAFE_CODE]


def test_risky_code_without_a_token_is_refused_at_the_worker_too(parts) -> None:
    """Defence in depth: the control plane asks, and the worker still refuses."""
    executor, provider, _, _ = parts
    outcome = executor.execute(make_job([code_step(0, RISKY_CODE)]))

    assert not outcome.succeeded
    assert outcome.error["code"] == "APPROVAL_REQUIRED"
    assert provider.executed_code == []


def test_risky_code_with_a_matching_token_runs(parts) -> None:
    executor, provider, _, _ = parts
    from blender_worker.backends.official.backend import approval_token_for

    outcome = executor.execute(
        make_job([code_step(0, RISKY_CODE, token=approval_token_for(RISKY_CODE))])
    )
    assert outcome.succeeded, outcome.error
    assert provider.executed_code == [RISKY_CODE]


def test_a_token_for_different_code_is_refused(parts) -> None:
    executor, provider, _, _ = parts
    from blender_worker.backends.official.backend import approval_token_for

    outcome = executor.execute(
        make_job([code_step(0, RISKY_CODE, token=approval_token_for(SAFE_CODE))])
    )
    assert not outcome.succeeded
    assert outcome.error["code"] == "APPROVAL_REQUIRED"
    assert provider.executed_code == []


# --- artifacts ------------------------------------------------------------


def test_a_glb_model_is_exported_and_recorded(parts, tmp_path: Path) -> None:
    executor, provider, _, _ = parts
    from studio_preview.artifacts import LocalArtifactStore

    executor.artifacts = LocalArtifactStore(tmp_path / "artifacts")
    outcome = executor.execute(make_job([wall(0, "Wall_A", object_id="obj_a")]))

    assert outcome.succeeded, outcome.error
    assert outcome.model is not None
    assert outcome.model["artifact_type"] == "model_glb"
    assert outcome.model["media_type"] == "model/gltf-binary"
    assert outcome.model["artifact_id"].startswith("model_")
    assert outcome.model_error is None

    stored = executor.artifacts.read_bytes(PROJECT, outcome.model["artifact_id"])
    assert stored is not None and stored.startswith(b"glTF")


def test_a_failed_export_never_fails_the_mutation(parts, tmp_path: Path) -> None:
    executor, provider, _, _ = parts
    from studio_preview.artifacts import LocalArtifactStore

    executor.artifacts = LocalArtifactStore(tmp_path / "artifacts")
    provider.fail_capability("export_glb", "MUTATION_FAILED", "export blew up")

    outcome = executor.execute(make_job([wall(0, "Wall_A", object_id="obj_a")]))
    assert outcome.succeeded, "a design change stays durable even if its model does not"
    assert outcome.model is None
    assert outcome.model_error is not None


def test_the_model_artifact_is_reused_on_a_replay(parts, tmp_path: Path) -> None:
    executor, _, _, _ = parts
    from studio_preview.artifacts import LocalArtifactStore

    executor.artifacts = LocalArtifactStore(tmp_path / "artifacts")
    job = make_job([wall(0, "Wall_A", object_id="obj_a")])

    first = executor.execute(job)
    second = executor.execute(job)
    assert second.model is not None
    assert second.model["artifact_id"] == first.model["artifact_id"]
    assert len(executor.artifacts.list_for_project(PROJECT)) == 1


def test_no_export_file_is_left_behind(parts, tmp_path: Path) -> None:
    executor, _, _, _ = parts
    from studio_preview.artifacts import LocalArtifactStore

    executor.artifacts = LocalArtifactStore(tmp_path / "artifacts")
    executor.execute(make_job([wall(0, "Wall_A", object_id="obj_a")]))

    exports = tmp_path / "exports"
    leftovers = list(exports.rglob("*.glb")) if exports.exists() else []
    assert leftovers == []
