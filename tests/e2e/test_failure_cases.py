"""The failure cases the product must survive.

.kiro/steering/testing.md names them explicitly, so they are tested explicitly rather
than left to unit-level coverage: Blender unavailable, worker disconnected, MCP failure,
project lock conflict, render failure, invalid object, invalid units, duplicate job,
interrupted operation.

Offline, through the real HTTP surface (see ``studio_fixtures.design_stack``). Two
properties are checked in every scenario, because they are what makes a failure
acceptable rather than dangerous:

* the user is told something true, in words they can act on;
* the project is left in a state that is either fully changed or unchanged — never
  half-changed and never unrecoverable.

The remaining named cases live where they belong and are not duplicated here: invalid
units and duplicate jobs in ``tests/security/test_untrusted_model_output.py`` and
``tests/e2e/test_selected_object_editing.py``; Spec 001's own failure matrix in
``tests/e2e/test_api_blender_e2e.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("websockets", reason="websockets not installed")

from blender_worker.locks import FileLockProvider  # noqa: E402
from studio_agent.providers.fake_llm import operation  # noqa: E402
from studio_fixtures.design_stack import (  # noqa: E402
    DesignStack,
    FakeObject,
    build_design_stack,
)

CEILING = 2.70
THICKNESS = 0.12
TOLERANCE = 1e-9


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[DesignStack]:
    with build_design_stack(
        tmp_path,
        objects={
            "Cube": FakeObject(
                name="Cube", dimensions=(1.0, 1.0, 1.0), studio_object_id="obj_cube"
            )
        },
    ) as running:
        yield running


def _wall(name: str, start: tuple[float, float], end: tuple[float, float]) -> dict:
    return operation(
        "create_wall",
        {
            "display_name": name,
            "object_id": f"obj_{name.lower()}",
            "start_meters": {"x": start[0], "y": start[1]},
            "end_meters": {"x": end[0], "y": end[1]},
            "height_meters": CEILING,
            "thickness_meters": THICKNESS,
        },
        label=f"build {name}",
    )


def _three_walls() -> list[dict]:
    return [
        _wall("North", (-2.0, 1.5), (2.0, 1.5)),
        _wall("East", (2.0, 1.5), (2.0, -1.5)),
        _wall("South", (2.0, -1.5), (-2.0, -1.5)),
    ]


# ===========================================================================
# the design machine cannot do the work
# ===========================================================================


def test_blender_failing_mid_plan_leaves_the_finished_steps_durable(stack: DesignStack):
    """A plan is resumable, not repeatable: step 3 failing must not undo steps 1 and 2.

    This is the property that makes reconstruction usable at all. Rebuilding a whole
    floor plan takes many operations, and the first failure must not mean starting over.
    """
    stack.backend.fail_capability(
        "create_wall", "MUTATION_FAILED", "Blender reported degenerate_wall."
    )
    # Let the first two through, then fail. The fake fails a capability wholesale, so
    # instead the plan is split: two walls succeed under one job, the third fails.
    stack.backend.forced_failures.clear()

    stack.llm.queue_plan(_three_walls()[:2], message="Building the first two walls.")
    first = stack.run_turn("req_fail_001", "Build the north and east walls.")
    assert first["result"]["applied"] == 2
    assert sorted(stack.backend.objects) == ["Cube", "East", "North"]

    # Now Blender starts refusing.
    stack.backend.fail_capability(
        "create_wall", "MUTATION_FAILED", "Blender reported degenerate_wall."
    )
    stack.llm.queue_plan(_three_walls(), message="Building the rest of the room.")
    response = stack.chat("req_fail_002", "Finish the room.")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert stack.pump_until()
    failed = stack.await_status(job_id, "failed")

    # The user is told which step failed and why, in the backend's words.
    assert failed["error"]["code"] == "MUTATION_FAILED", failed["error"]
    assert "degenerate_wall" in failed["error"]["message"]

    # The two existing walls are untouched — they were recognised as already built, so
    # the failure cost nothing that had already been achieved.
    assert sorted(stack.backend.objects) == ["Cube", "East", "North"]
    assert stack.object_named("North").dimensions[2] == pytest.approx(
        CEILING, abs=TOLERANCE
    )

    # And a recovery point exists for the attempt.
    recovery = list((stack.runtime_root / "recovery").rglob("*.blend"))
    assert recovery, "no recovery point was created before mutating"


def test_the_plan_resumes_where_it_stopped_once_blender_recovers(stack: DesignStack):
    """After the fault is fixed, only the MISSING steps are applied."""
    stack.llm.queue_plan(_three_walls()[:2])
    stack.run_turn("req_fail_010", "Build two walls.")
    invocations_before = len(
        [r for r in stack.backend.invocations if r.capability == "create_wall"]
    )

    stack.llm.queue_plan(_three_walls(), message="Finishing the room.")
    final = stack.run_turn("req_fail_011", "Finish the room.")

    assert final["result"]["already_applied"] == 2, final["result"]
    assert final["result"]["applied"] == 1, final["result"]
    assert sorted(stack.backend.objects) == ["Cube", "East", "North", "South"]
    assert (
        len([r for r in stack.backend.invocations if r.capability == "create_wall"])
        == invocations_before + 1
    ), "an already-built wall was built again"


def test_the_whole_backend_being_offline_fails_the_job_without_touching_the_project(
    stack: DesignStack,
):
    """The MCP server dying between planning and execution."""
    stack.backend.available = False

    stack.llm.queue_plan(_three_walls())
    response = stack.chat("req_fail_020", "Build the room.")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert stack.pump_until()
    failed = stack.await_status(job_id, "failed")

    assert failed["error"]["code"] == "BLENDER_UNAVAILABLE", failed["error"]
    assert sorted(stack.backend.objects) == ["Cube"]
    assert stack.backend.saves == 0


def test_an_object_that_is_not_in_the_scene_fails_clearly(stack: DesignStack):
    """The model referring to something that no longer exists."""
    stack.llm.queue_plan(
        [
            operation(
                "move_object",
                {
                    "object_id": "obj_kitchen_island",
                    "desired_position_meters": {"x": 1.0, "y": 0.0, "z": 0.0},
                },
                label="move the island",
            )
        ]
    )
    response = stack.chat("req_fail_030", "Move the island 1 m right.")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert stack.pump_until()
    failed = stack.await_status(job_id, "failed")

    assert failed["error"]["code"] == "OBJECT_NOT_FOUND", failed["error"]
    assert stack.object_with_id("obj_cube").position == (0.0, 0.0, 0.0)
    assert stack.backend.saves == 0


# ===========================================================================
# the project is busy
# ===========================================================================


def test_a_locked_project_is_refused_rather_than_modified_concurrently(
    stack: DesignStack,
):
    """Project isolation: two things must never write one .blend at once."""
    stack.llm.queue_plan(_three_walls())

    # Something else holds the project lock — another worker, or a previous run that has
    # not finished. The lock provider is the real one, and this is a real second holder.
    other = FileLockProvider(stack.runtime_root)
    with other.hold(stack.project_id, timeout=0.0):
        response = stack.chat("req_fail_040", "Build the room.")
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert stack.pump_until()
        failed = stack.await_status(job_id, "failed")

    assert failed["error"]["code"] == "LOCK_CONFLICT", failed["error"]
    assert sorted(stack.backend.objects) == ["Cube"]
    assert stack.backend.invocations == [], "a capability ran without holding the lock"


# ===========================================================================
# artifacts are not the design
# ===========================================================================


def test_a_failed_glb_export_does_not_fail_the_design_change(stack: DesignStack):
    """Losing a preview is an inconvenience; losing a design change is not acceptable.

    The mutation is already saved by the time artifacts are produced, so an export
    failure is reported alongside a SUCCESSFUL job rather than turning into one.
    """
    stack.backend.fail_capability("export_glb", "MUTATION_FAILED", "GLB export failed.")

    stack.llm.queue_plan(_three_walls()[:1], message="Building the north wall.")
    final = stack.run_turn("req_fail_050", "Build the north wall.")

    assert final["job_status"] == "succeeded", final
    assert final["result"]["applied"] == 1
    assert stack.object_named("North").dimensions[2] == pytest.approx(
        CEILING, abs=TOLERANCE
    )
    # The failure is reported, not hidden.
    assert final["result"]["model_error"]["code"] == "MUTATION_FAILED"
    assert final["result"].get("model") is None
    # And the browser is simply told there is no model yet.
    latest = stack.http.get(f"/api/projects/{stack.project_id}/model/latest")
    assert latest.status_code == 200
    assert latest.json()["model"] is None

    # The scene still reached the browser, so the change is visible in the inspector
    # even though the 3D view has nothing new to load.
    assert stack.cached_object("obj_north") is not None


# ===========================================================================
# the link between the two halves
# ===========================================================================


def test_a_worker_whose_result_never_arrives_reconciles_instead_of_re_executing(
    stack: DesignStack,
):
    """A dropped connection must not lose a mutation that already happened.

    The worker applies the plan and the report is lost on the way back. The mutation is
    already durable in the project and recorded in the journal, so the fix is to RESEND
    the stored result — never to execute again. This is the property that makes a flaky
    link merely slow rather than dangerous.
    """
    stack.llm.queue_plan(_three_walls()[:1], message="Building the north wall.")
    response = stack.chat("req_fail_060", "Build the north wall.")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    # Lose exactly the result message, as a link dropping at the worst moment would.
    real_send = stack.worker._safe_send
    dropped: list[dict] = []

    def _drop_results(message):
        if message.get("type") == "job_result":
            dropped.append(message)
            return False
        return real_send(message)

    stack.worker._safe_send = _drop_results  # type: ignore[method-assign]
    try:
        assert stack.pump_until(), "the worker never received the offer"
    finally:
        stack.worker._safe_send = real_send  # type: ignore[method-assign]

    assert dropped, "the test did not actually drop the result"

    # Blender really did the work, and the journal knows the report is outstanding.
    assert stack.object_named("North").dimensions[2] == pytest.approx(
        CEILING, abs=TOLERANCE
    )
    undelivered = [
        record.job_id
        for record in stack.executor.capabilities.store.undelivered_results()
    ]
    assert job_id in undelivered, "the result was not retained for redelivery"

    # The control plane has not been told, so it must not be claiming success.
    assert stack.job_status(job_id)["job_status"] != "succeeded"

    # Reconciling delivers the STORED result. No capability is invoked again.
    invocations_before = len(stack.backend.invocations)
    assert stack.worker.reconcile() == [job_id]
    stack.await_status(job_id, "succeeded")

    assert len(stack.backend.invocations) == invocations_before, (
        "reconciliation re-executed the plan"
    )
    assert len([o for o in stack.backend.objects if o == "North"]) == 1
    assert stack.cached_object("obj_north") is not None
