"""Routing a job to the executor that understands it."""

from __future__ import annotations

from pathlib import Path

import pytest

from blender_worker.backends.fake import FakeBlenderCapabilityProvider
from blender_worker.capability_executor import CapabilityPlanExecutor
from blender_worker.dispatch import DispatchingExecutor
from blender_worker.journal import FileSystemExecutionStore
from blender_worker.locks import FileLockProvider
from blender_worker.registry import MappingProjectRegistry
from studio_contracts.jobs import create_capability_job, to_job_wire
from studio_types import CapabilityOperation

PROJECT = "proj_test"


class RecordingLegacy:
    """Stands in for the Spec 001 executor."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def execute(self, job):
        self.seen.append(job)
        from blender_worker.executor import WorkerOutcome

        return WorkerOutcome(
            job_id=job["job_id"],
            project_id=job["project_id"],
            job_status="succeeded",
            phase="completed",
        )


@pytest.fixture()
def dispatcher(tmp_path: Path):
    blend = tmp_path / "projects" / f"{PROJECT}.blend"
    blend.parent.mkdir(parents=True, exist_ok=True)
    blend.write_bytes(b"BLENDER-fake")

    provider = FakeBlenderCapabilityProvider()
    capabilities = CapabilityPlanExecutor(
        store=FileSystemExecutionStore(tmp_path / "journal"),
        locks=FileLockProvider(tmp_path / "locks"),
        projects=MappingProjectRegistry(tmp_path / "projects", {PROJECT: blend}),
        provider=provider,
        recovery_root=tmp_path / "recovery",
    )
    legacy = RecordingLegacy()
    return DispatchingExecutor(legacy=legacy, capabilities=capabilities), legacy, provider


def capability_job():
    created = create_capability_job(
        job_id="job_req_1_0",
        project_id=PROJECT,
        session_id="s",
        user_id="u",
        request_id="req_1",
        operations=[
            CapabilityOperation(
                operation_index=0,
                capability="create_wall",
                label="Creating a wall",
                arguments={
                    "display_name": "Wall_A",
                    "object_id": "obj_a",
                    "start_meters": {"x": 0.0, "y": 0.0},
                    "end_meters": {"x": 4.0, "y": 0.0},
                    "height_meters": 2.4,
                    "thickness_meters": 0.12,
                },
            )
        ],
        created_at="2026-01-01T00:00:00Z",
    )
    assert created.ok
    return to_job_wire(created.job)


def test_both_job_types_are_advertised(dispatcher) -> None:
    executor, _, _ = dispatcher
    assert set(executor.supported_job_types) == {"move_object", "apply_capabilities"}


def test_a_move_object_job_goes_to_the_spec_001_executor(dispatcher) -> None:
    executor, legacy, provider = dispatcher
    outcome = executor.execute(
        {"job_id": "job_x", "project_id": PROJECT, "job_type": "move_object"}
    )
    assert outcome.job_status == "succeeded"
    assert len(legacy.seen) == 1
    assert provider.invocations == [], "the capability path must not be touched"


def test_a_capability_job_goes_to_the_capability_executor(dispatcher) -> None:
    executor, legacy, provider = dispatcher
    outcome = executor.execute(capability_job())

    assert outcome.job_status == "succeeded", outcome.error
    assert legacy.seen == [], "the Spec 001 path must not be touched"
    assert "Wall_A" in provider.objects


def test_the_scene_and_model_travel_on_the_result(dispatcher, tmp_path: Path) -> None:
    """The browser learns about a new scene and model through the job it already polls."""
    executor, _, _ = dispatcher
    from studio_preview.artifacts import LocalArtifactStore

    executor.capabilities.artifacts = LocalArtifactStore(tmp_path / "artifacts")

    outcome = executor.execute(capability_job())
    assert outcome.result is not None
    assert outcome.result["scene"]["scene_version"].startswith("sha256:")
    assert outcome.result["model"]["artifact_type"] == "model_glb"
    assert outcome.result["applied"] == 1


def test_a_failed_plan_reports_the_error_through_the_same_shape(dispatcher) -> None:
    executor, _, provider = dispatcher
    provider.fail_capability("create_wall", "MUTATION_FAILED", "nope")
    outcome = executor.execute(capability_job())

    assert outcome.job_status == "failed"
    assert outcome.error["code"] == "MUTATION_FAILED"
    assert outcome.preview is None
