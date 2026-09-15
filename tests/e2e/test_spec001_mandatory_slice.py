"""THE MANDATORY SPEC 001 ACCEPTANCE TEST (Task 12).

OPT-IN: marked ``blender``, because it runs real Blender.

    pytest -m blender tests/e2e/test_spec001_mandatory_slice.py -v

This is the single test that decides whether the vertical slice works. Everything is
the real implementation — real HTTP, a real ASGI server, a real outbound WebSocket,
the real agent, the real worker, real Blender, a real saved ``.blend``, and a real
PNG served over HTTP:

    POST /api/chat
         ↓  FastAPI control plane
         ↓  AgentProvider          → AgentPlan
         ↓  JobFactory             → canonical Job
         ↓  worker WebSocket
         ↓  WorkerExecutor         → lock → plan → recovery point
         ↓  Blender / MCP move_object
         ↓  saved .blend
         ↓  PreviewGenerator       → PNG → ArtifactStore
         ↓  GET /api/projects/{id}/artifacts/{artifact_id}

The happy path is ONE clearly named scenario asserting all eleven required
outcomes in order, so a failure points at a specific step rather than at "the slice".

VERIFICATION DISCIPLINE
-----------------------
Success is never inferred from the absence of an exception (see
.kiro/steering/testing.md). Every coordinate assertion reads the SAVED ``.blend``
back through a FRESH Blender process, so it proves durability rather than worker
memory. Every failure case does the same, to prove the project was not left
partially mutated or unreadable.

The stack is assembled by ``studio_fixtures.slice_stack``, shared with the other E2E
tests, so this file contains assertions and no parallel infrastructure.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("websockets", reason="websockets not installed")

from blender_mcp.blender_runtime import find_blender_executable  # noqa: E402
from blender_mcp.tolerance import coordinates_equal  # noqa: E402
from blender_worker import phases  # noqa: E402
from blender_worker.blender_ops import BlenderExecutionError  # noqa: E402
from studio_fixtures.slice_stack import (  # noqa: E402
    MOVE_COMMAND,
    PREVIEW_HEIGHT,
    PREVIEW_WIDTH,
    LocalSliceStack,
    build_slice_stack,
)
from studio_preview.generator import looks_like_png  # noqa: E402

PROJECT_ID = "proj_seed"

#: The scenario's starting and finishing coordinates, in canonical metres.
START_X = 0.0
EXPECTED_X = 0.50

pytestmark = pytest.mark.blender


@pytest.fixture
def slice_stack(tmp_path: Path) -> Iterator[LocalSliceStack]:
    """The full local slice: control plane + worker + isolated seed project."""
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")
    with build_slice_stack(tmp_path) as stack:
        yield stack


# ===========================================================================
# THE MANDATORY HAPPY PATH
# ===========================================================================


def test_mandatory_slice_move_cube_50cm_right_end_to_end(slice_stack: LocalSliceStack):
    """MANDATORY: "Move Cube 50 cm to the right." works end to end.

    Asserts, in order, the eleven required outcomes:

      1. POST /api/chat is accepted
      2. the job reaches succeeded
      3. the final ChatResponse reports success
      4. the saved .blend is reopened by a fresh Blender process
      5. Cube X = 0.50 m within the existing tolerance
      6. a preview artifact exists
      7. the preview URL is project-scoped
      8. GET on it returns HTTP 200
      9. Content-Type is image/png
     10. the PNG is valid and non-empty
     11. no local filesystem path leaks through the API or preview metadata
    """
    stack = slice_stack

    # Preconditions: a deterministic project, and a design machine that can work.
    assert coordinates_equal(stack.saved_cube_x(), START_X), (
        "the fixture must start with Cube X = 0.0"
    )
    health = stack.http.get("/health").json()
    assert health["ready_workers"] == 1
    assert health["blender_capable_workers"] == 1

    # ---- 1. POST /api/chat is accepted ------------------------------
    response = stack.submit("req_mandatory_001", MOVE_COMMAND)
    assert response.status_code == 202, response.text
    submission = response.json()
    job_id = submission["job_id"]

    assert submission["request_id"] == "req_mandatory_001"
    assert submission["project_id"] == PROJECT_ID
    assert submission["job_status"] == "queued"
    assert submission["provider"] == "rule_based", (
        "the change must be interpreted through the AgentProvider boundary"
    )
    assert submission["duplicate"] is False

    # The canonical Job the AgentProvider + JobFactory produced.
    record = stack.server.dependencies.store.get(PROJECT_ID, job_id)
    assert record is not None
    job = record.job
    assert job["job_type"] == "move_object"
    assert job["project_id"] == PROJECT_ID
    assert job["payload"]["target"]["name"] == "Cube"
    # "50 cm to the right" resolved to +0.50 m on world X, in canonical metres.
    assert job["payload"]["delta_meters"] == {"x": 0.5, "y": 0.0, "z": 0.0}
    assert job["origin"] == {
        "request_id": "req_mandatory_001",
        "operation_index": 0,
    }

    # ---- the worker does the real work ------------------------------
    assert stack.pump_until(), "the job offer never reached the worker"

    # ---- 2. the job reaches succeeded -------------------------------
    status = stack.await_status(job_id, "succeeded")
    assert status["job_status"] == "succeeded"
    assert status["execution_phase"] == phases.COMPLETED
    assert status["error"] is None
    assert status["result"]["applied"] is True
    assert status["result"]["verified"] is True

    # ---- 3. the final ChatResponse reports success ------------------
    chat = status["chat"]
    assert chat is not None, "a terminal job must carry a canonical ChatResponse"
    assert chat["status"] == "success"
    assert coordinates_equal(chat["object_position"]["x"], EXPECTED_X)
    assert chat.get("error") is None

    from studio_contracts import SCHEMA_FILES, validate_against_schema

    conformance = validate_against_schema(
        SCHEMA_FILES["ChatResponse"],
        {key: value for key, value in chat.items() if value is not None},
    )
    assert conformance.valid, conformance.violations

    # ---- 4 & 5. the SAVED project, read by a fresh Blender ----------
    digest = stack.inspect()["digest"]
    assert digest["scene"]["length_unit"] == "METERS", "metres remain canonical"
    assert stack.saved_object_names() == ["Cube"], (
        "the preview must not have added anything to the design"
    )
    saved_x = stack.saved_cube_x()
    assert coordinates_equal(saved_x, EXPECTED_X), (
        f"saved Cube X is {saved_x}, expected {EXPECTED_X}"
    )

    # ---- 6. a preview artifact exists -------------------------------
    preview = status["preview"]
    assert preview is not None, (
        f"no preview was produced: {status.get('preview_error')}"
    )
    assert status["preview_error"] is None
    stored = stack.stored_artifacts()
    assert len(stored) == 1
    assert stored[0].artifact_id == preview["artifact_id"]
    assert stored[0].job_id == job_id

    # ---- 7. the preview URL is project-scoped -----------------------
    assert preview["url"] == (
        f"/api/projects/{PROJECT_ID}/artifacts/{preview['artifact_id']}"
    )
    assert chat["preview_url"] == preview["url"]
    # There is no unscoped route, and another project cannot reach it.
    assert stack.http.get(f"/api/artifacts/{preview['artifact_id']}").status_code == 404
    assert (
        stack.http.get(
            f"/api/projects/proj_other/artifacts/{preview['artifact_id']}"
        ).status_code
        == 404
    )

    # ---- 8, 9, 10. the image is served, typed, valid and non-empty --
    image = stack.http.get(preview["url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert looks_like_png(image.content), "the served bytes are not a PNG"
    assert len(image.content) > 1000, "a rendered preview should not be trivially small"
    assert len(image.content) == preview["size_bytes"]
    assert (
        "sha256:" + hashlib.sha256(image.content).hexdigest() == preview["checksum"]
    ), "the served bytes do not match the recorded checksum"
    assert (preview["width"], preview["height"]) == (PREVIEW_WIDTH, PREVIEW_HEIGHT)

    # ---- 11. no filesystem path leaks anywhere ----------------------
    project_path = str(stack.project_path)
    artifact_root = str(stack.artifacts.root)

    for label, text in (
        ("submission", response.text),
        ("job status", stack.http.get(stack.job_path(job_id)).text),
        ("latest preview", stack.http.get(f"/api/projects/{PROJECT_ID}/preview/latest").text),
        ("workers", stack.http.get("/api/workers").text),
        ("health", stack.http.get("/health").text),
    ):
        for leaked in (
            project_path,
            artifact_root,
            ".blend",
            "/home/",
            "/tmp/",
            "seed_project",
            stack.token,
        ):
            assert leaked not in text, f"{label} leaked {leaked!r}"

    # The image BYTES must be as path-free as the JSON (Blender stamps the .blend
    # path into PNG metadata unless stamping is disabled).
    body = image.content.decode("latin-1")
    for leaked in (project_path, ".blend", "/home/", "/tmp/", "seed_project"):
        assert leaked not in body, f"the served PNG leaked {leaked!r}"


# ===========================================================================
# MANDATORY FAILURE CASE 1 — worker / Blender unavailable
# ===========================================================================


def test_failure_no_design_machine_connected_is_refused_without_touching_the_project(
    tmp_path: Path,
):
    """No worker connected: refused up front, nothing dispatched, nothing changed."""
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")

    # Deliberately do NOT connect the worker.
    with build_slice_stack(tmp_path, connect=False) as stack:
        assert coordinates_equal(stack.saved_cube_x(), START_X)

        health = stack.http.get("/health").json()
        assert health["api"] == "healthy", "the API itself is fine"
        assert health["ready_workers"] == 0
        assert health["blender_capable_workers"] == 0, (
            "Blender must never be reported ready just because the API answers"
        )

        response = stack.submit("req_no_machine")

        # Structured error, correct status, no false success.
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["error"]["code"] == "BLENDER_UNAVAILABLE"
        assert body["error"]["message"].strip()
        assert "nothing has been modified" in body["error"]["message"].lower()
        assert body["request_id"] == "req_no_machine"
        assert "job_id" not in body, "a refused submission must not claim a job"

        # Nothing was dispatched and no artifact exists.
        assert stack.stored_artifacts() == ()

        # NO CORRUPTION: the project is present, readable, and unchanged.
        assert_project_intact(stack, START_X)


def test_failure_blender_unavailable_on_the_worker_fails_structurally(tmp_path: Path):
    """The worker is connected but Blender cannot be run.

    Exercised through the real ``BlenderOperationExecutor`` boundary, so the
    executor's own error mapping is what produces the result.
    """
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")

    class UnavailableBlender:
        """Stands in for a workstation whose Blender cannot be started."""

        def read_object_position(self, project_path, target):
            raise BlenderExecutionError("Blender executable not found")

        def execute_move(self, project_path, plan):  # pragma: no cover - unreachable
            raise AssertionError("no mutation may be attempted without Blender")

    with build_slice_stack(tmp_path, blender=UnavailableBlender()) as stack:
        assert coordinates_equal(stack.saved_cube_x(), START_X)

        response = stack.submit("req_blender_down")
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        assert stack.pump_until()
        status = stack.await_status(job_id, "failed")

        # Structured error, no false success.
        assert status["job_status"] == "failed"
        assert status["error"]["code"] == "BLENDER_UNAVAILABLE"
        assert status["execution_phase"] == phases.FAILED
        assert status["chat"]["status"] == "error"
        assert status["result"] is None

        # No preview may accompany a change that never happened.
        assert status["preview"] is None
        assert stack.stored_artifacts() == ()

        # NO CORRUPTION.
        assert_project_intact(stack, START_X)


# ===========================================================================
# MANDATORY FAILURE CASE 2 — invalid object
# ===========================================================================


def test_failure_invalid_object_reaches_execution_and_reports_object_not_found(
    slice_stack: LocalSliceStack,
):
    """A well-formed instruction naming an object that does not exist.

    The instruction is interpretable, so it is accepted and REACHES execution — the
    failure is discovered by Blender, not guessed at by the agent.
    """
    stack = slice_stack
    assert coordinates_equal(stack.saved_cube_x(), START_X)

    response = stack.submit("req_missing_object", "Move Sofa 50 cm to the right.")

    # Accepted: the grammar is understood, so this is not a 422.
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    # It really reaches the worker and the real Blender scene read.
    assert stack.pump_until(), "the instruction never reached execution"
    status = stack.await_status(job_id, "failed")

    assert status["error"]["code"] == "OBJECT_NOT_FOUND"
    assert status["job_status"] == "failed"
    assert status["chat"]["status"] == "error"
    assert status["chat"]["error"]["code"] == "OBJECT_NOT_FOUND"

    # No false successful preview.
    assert status["preview"] is None
    assert status["preview_error"] is None
    assert stack.stored_artifacts() == (), (
        "a failed change must not leave a preview artifact behind"
    )
    assert stack.http.get(f"/api/projects/{PROJECT_ID}/preview/latest").status_code == 404

    # NO CORRUPTION: Cube untouched, and no Sofa was invented.
    assert_project_intact(stack, START_X)
    assert stack.saved_object_names() == ["Cube"]


# ===========================================================================
# MANDATORY FAILURE CASE 3 — lock conflict
# ===========================================================================


def test_failure_lock_conflict_prevents_concurrent_mutation(
    slice_stack: LocalSliceStack,
):
    """The project lock is held, so execution must refuse rather than mutate.

    The lock is acquired DETERMINISTICALLY through the Task 6 abstraction
    (``FileLockProvider``) — the same provider the worker uses. ``flock`` is
    associated with an open file description, so a second acquisition conflicts
    immediately and the test needs no sleep and no race window. No second locking
    mechanism is introduced.
    """
    stack = slice_stack
    assert coordinates_equal(stack.saved_cube_x(), START_X)

    response = stack.submit("req_locked")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    # Hold the project lock for the whole execution attempt.
    with stack.locks.hold(PROJECT_ID, timeout=0.0):
        assert stack.pump_until(), "the job never reached the worker"
        status = stack.await_status(job_id, "failed")

    # The existing structured lock error, not a generic failure.
    assert status["error"]["code"] == "LOCK_CONFLICT"
    assert status["job_status"] == "failed"
    assert status["chat"]["status"] == "error"

    # The mutation never started: no plan, no recovery copy, no preview.
    assert status["result"] is None
    assert status["preview"] is None
    assert stack.stored_artifacts() == ()
    assert stack.executor.store.load(PROJECT_ID, job_id) is None, (
        "nothing may be journalled for an execution that never acquired the lock"
    )

    # NO CORRUPTION: no concurrent mutation happened.
    assert_project_intact(stack, START_X)


def test_the_lock_is_released_so_a_later_change_still_succeeds(
    slice_stack: LocalSliceStack,
):
    """A lock conflict must be transient, not a poisoned project.

    Proves the refusal above left nothing behind: with the lock released, the same
    instruction under a NEW request_id applies normally.
    """
    stack = slice_stack

    # First attempt is refused while the lock is held.
    first = stack.submit("req_lock_then_ok_1")
    with stack.locks.hold(PROJECT_ID, timeout=0.0):
        assert stack.pump_until()
        blocked = stack.await_status(first.json()["job_id"], "failed")
    assert blocked["error"]["code"] == "LOCK_CONFLICT"
    assert coordinates_equal(stack.saved_cube_x(), START_X)

    # The lock is now free. A new intentional command must work.
    second = stack.submit("req_lock_then_ok_2")
    assert stack.pump_until()
    succeeded = stack.await_status(second.json()["job_id"], "succeeded")

    assert succeeded["preview"] is not None
    assert coordinates_equal(stack.saved_cube_x(), EXPECTED_X)
    assert_project_intact(stack, EXPECTED_X)


# ===========================================================================
# IDEMPOTENCY — the property the whole design exists to protect
# ===========================================================================


def test_retry_of_the_same_request_id_does_not_move_the_cube_twice(
    slice_stack: LocalSliceStack,
):
    """0.00 → 0.50, then a verbatim retry leaves it at 0.50; a new command → 1.00."""
    stack = slice_stack
    assert coordinates_equal(stack.saved_cube_x(), START_X)

    # ---- first intentional command --------------------------------
    first = stack.submit("req_idem_001")
    first_job = first.json()["job_id"]
    assert stack.pump_until()
    first_status = stack.await_status(first_job, "succeeded")
    assert coordinates_equal(stack.saved_cube_x(), EXPECTED_X)
    first_artifact = first_status["preview"]["artifact_id"]

    # ---- verbatim retry: same request_id --------------------------
    retry = stack.submit("req_idem_001")
    assert retry.status_code == 202, retry.text
    assert retry.json()["job_id"] == first_job, "a retry must resolve to the same job"
    assert retry.json()["duplicate"] is True

    assert coordinates_equal(stack.saved_cube_x(), EXPECTED_X), (
        "a retry must not move the cube a second time"
    )
    retried_status = stack.job_status(first_job)
    assert retried_status["preview"]["artifact_id"] == first_artifact, (
        "a retry must reuse the existing preview"
    )
    assert len(stack.stored_artifacts()) == 1

    # ---- a genuinely new command: 0.50 -> 1.00 --------------------
    second = stack.submit("req_idem_002")
    second_job = second.json()["job_id"]
    assert second_job != first_job
    assert stack.pump_until()
    second_status = stack.await_status(second_job, "succeeded")

    assert coordinates_equal(stack.saved_cube_x(), 1.00)
    assert second_status["preview"]["artifact_id"] != first_artifact, (
        "a new intentional change must produce a new artifact"
    )

    # The earlier preview is still retrievable: history is not overwritten.
    assert stack.http.get(f"/api/projects/{PROJECT_ID}/artifacts/{first_artifact}").status_code == 200
    assert len(stack.stored_artifacts()) == 2

    # ---- a stale retry after the second command -------------------
    stale = stack.submit("req_idem_001")
    assert stale.status_code == 202
    assert coordinates_equal(stack.saved_cube_x(), 1.00), (
        "a stale retry must not reapply an earlier step"
    )
    assert_project_intact(stack, 1.00)


# ===========================================================================
# Shared corruption check
# ===========================================================================


def assert_project_intact(stack: LocalSliceStack, expected_x: float) -> None:
    """Verify the .blend with a FRESH Blender process after any outcome.

    Four things, all of which a partial mutation or a crashed save would break:

      1. the file still exists and is non-empty
      2. Blender can open and read it
      3. Cube is exactly where it should be
      4. the scene still contains only what it should, with metres canonical
    """
    assert stack.project_path.exists(), "the project file disappeared"
    assert stack.project_path.stat().st_size > 0, "the project file is empty"

    # Opening it in a fresh Blender is the readability check: inspect_blend raises
    # if Blender cannot load the file.
    inspection = stack.inspect()
    digest = inspection["digest"]

    assert digest["objects"], "the saved scene has no objects"
    assert sorted(entry["name"] for entry in digest["objects"]) == ["Cube"]
    assert digest["scene"]["unit_system"] == "METRIC"
    assert digest["scene"]["length_unit"] == "METERS"

    cube = next(entry for entry in digest["objects"] if entry["name"] == "Cube")
    actual_x = float(cube["world_position_meters"]["x"])
    assert coordinates_equal(actual_x, expected_x), (
        f"Cube X is {actual_x}, expected {expected_x} — partial mutation?"
    )
    # The other axes must never have moved.
    assert coordinates_equal(float(cube["world_position_meters"]["y"]), 0.0)
    assert coordinates_equal(float(cube["world_position_meters"]["z"]), 0.0)
    # And the object identity survived.
    assert cube["object_id"] == "obj_cube001"
