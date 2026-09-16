"""Step progress reaches the transport, scoped to the job that produced it."""

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


def wall(index: int, name: str) -> CapabilityOperation:
    return CapabilityOperation(
        operation_index=index,
        capability="create_wall",
        label=f"Creating {name}",
        arguments={
            "display_name": name,
            "object_id": f"obj_{name.lower()}",
            "start_meters": {"x": 0.0, "y": 0.0},
            "end_meters": {"x": 4.0, "y": 0.0},
            "height_meters": 2.4,
            "thickness_meters": 0.12,
        },
    )


@pytest.fixture()
def executor(tmp_path: Path) -> DispatchingExecutor:
    blend = tmp_path / "projects" / f"{PROJECT}.blend"
    blend.parent.mkdir(parents=True, exist_ok=True)
    blend.write_bytes(b"BLENDER-fake")

    # ONE journal, shared by both paths, exactly as `python -m blender_worker` wires it.
    # The dispatching executor enforces this: a second journal would hold results that
    # reconciliation never resends.
    store = FileSystemExecutionStore(tmp_path / "journal")

    capabilities = CapabilityPlanExecutor(
        store=store,
        locks=FileLockProvider(tmp_path / "locks"),
        projects=MappingProjectRegistry(tmp_path / "projects", {PROJECT: blend}),
        provider=FakeBlenderCapabilityProvider(),
        recovery_root=tmp_path / "recovery",
    )

    class Legacy:
        store = capabilities.store

        def execute(self, job):  # pragma: no cover - not exercised here
            raise AssertionError("the legacy path should not be used")

    return DispatchingExecutor(legacy=Legacy(), capabilities=capabilities)


def job(request_id: str = "req_1"):
    created = create_capability_job(
        job_id=f"job_{request_id}_0",
        project_id=PROJECT,
        session_id="s",
        user_id="u",
        request_id=request_id,
        operations=[wall(0, "Wall_A"), wall(1, "Wall_B"), wall(2, "Wall_C")],
        created_at="2026-01-01T00:00:00Z",
    )
    assert created.ok
    return to_job_wire(created.job)


def test_progress_is_reported_for_each_step(executor) -> None:
    reported: list[dict] = []
    executor.set_progress_sink(lambda progress: reported.append(progress.snapshot()))

    outcome = executor.execute(job())
    assert outcome.job_status == "succeeded"

    assert [entry["step_index"] for entry in reported] == [0, 1, 2]
    assert all(entry["step_count"] == 3 for entry in reported)
    assert [entry["label"] for entry in reported] == [
        "Creating Wall_A",
        "Creating Wall_B",
        "Creating Wall_C",
    ]


def test_progress_labels_are_written_for_a_person(executor) -> None:
    reported: list[dict] = []
    executor.set_progress_sink(lambda progress: reported.append(progress.snapshot()))
    executor.execute(job())

    for entry in reported:
        label = entry["label"]
        assert "obj_" not in label, "an internal id must not reach the user"
        assert "/" not in label, "a path must not reach the user"
        assert "create_wall" not in label, "a capability name must not reach the user"


def test_detaching_the_sink_stops_reporting(executor) -> None:
    reported: list[dict] = []
    executor.set_progress_sink(lambda progress: reported.append(progress.snapshot()))
    executor.set_progress_sink(None)

    executor.execute(job())
    assert reported == []


def test_a_failing_sink_never_breaks_the_plan(executor) -> None:
    """Progress is cosmetic. A reporting failure must not lose a design change."""

    def explode(_progress):
        raise RuntimeError("the socket went away")

    executor.set_progress_sink(explode)
    outcome = executor.execute(job())
    assert outcome.job_status == "succeeded", "a broken progress channel is not a failure"


def test_a_resumed_plan_only_reports_the_steps_it_actually_runs(executor) -> None:
    first = job()
    executor.execute(first)

    reported: list[dict] = []
    executor.set_progress_sink(lambda progress: reported.append(progress.snapshot()))
    executor.execute(first)

    # Everything was already applied, so the replay short-circuits before any step.
    assert reported == []
