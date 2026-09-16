"""THE MANDATORY SPEC 002 ACCEPTANCE TEST.

Offline: real HTTP, real ASGI server, real WebSocket, real routes, real SQLite, real
PDF ingestion, real context construction, real validation, real job contracts, real
project lock, real journal, real artifact store — with a scripted model
(``FakeLlmProvider``) and an in-memory Blender (``FakeBlenderCapabilityProvider``).
The tier that uses the OFFICIAL Blender MCP and real Blender is ``tests/mcp``, opt-in
with ``-m mcp``; the tier that uses real Astra is ``-m codex``.

What this file decides is whether the PRODUCT works: a non-technical user uploads a
plan, talks, and ends up with a model they can see.

    upload a floor plan (PDF)
        -> "build this in 3D"
        -> the assistant asks the one thing it cannot know
        -> the user answers
        -> walls and a floor are built, in metres
        -> the project is saved and a GLB is produced
        -> the browser can restore all of it after a reload

VERIFICATION DISCIPLINE
-----------------------
Per .kiro/steering/testing.md, nothing here concludes success from a 2xx or from the
absence of an exception. Geometry is read back out of the scene, the GLB is fetched
over HTTP and checked for the glTF magic, and the restore path is asserted through the
same endpoint the browser calls.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("websockets", reason="websockets not installed")

from studio_agent.providers.fake_llm import operation, response_body  # noqa: E402
from studio_fixtures.design_stack import DesignStack, build_design_stack  # noqa: E402
from studio_fixtures.sample_files import PDF_SAMPLE_TEXT, pdf_bytes  # noqa: E402

#: A 4 m x 3 m room. Canonical unit is the metre (see .kiro/steering/blender.md).
ROOM_WIDTH = 4.0
ROOM_DEPTH = 3.0
CEILING_HEIGHT = 2.70
WALL_THICKNESS = 0.12
TOLERANCE = 1e-9


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[DesignStack]:
    with build_design_stack(tmp_path) as running:
        yield running


def _wall(name: str, start: tuple[float, float], end: tuple[float, float]) -> dict:
    return operation(
        "create_wall",
        {
            "display_name": name,
            "object_id": f"obj_{name.lower()}",
            # A wall runs along the ground, so its ends are 2D by contract.
            "start_meters": {"x": start[0], "y": start[1]},
            "end_meters": {"x": end[0], "y": end[1]},
            "height_meters": CEILING_HEIGHT,
            "thickness_meters": WALL_THICKNESS,
        },
        label=f"build {name}",
    )


def _floor() -> dict:
    half_w, half_d = ROOM_WIDTH / 2, ROOM_DEPTH / 2
    return operation(
        "create_floor",
        {
            "display_name": "Floor",
            "object_id": "obj_floor",
            "footprint_meters": [
                {"x": -half_w, "y": -half_d},
                {"x": half_w, "y": -half_d},
                {"x": half_w, "y": half_d},
                {"x": -half_w, "y": half_d},
            ],
            "thickness_meters": 0.20,
            "elevation_meters": 0.0,
        },
        label="lay the floor",
    )


def _room_plan() -> list[dict]:
    half_w, half_d = ROOM_WIDTH / 2, ROOM_DEPTH / 2
    return [
        _wall("North", (-half_w, half_d), (half_w, half_d)),
        _wall("East", (half_w, half_d), (half_w, -half_d)),
        _wall("South", (half_w, -half_d), (-half_w, -half_d)),
        _wall("West", (-half_w, -half_d), (-half_w, half_d)),
        _floor(),
    ]


# ===========================================================================
# THE MANDATORY HAPPY PATH
# ===========================================================================


def test_mandatory_mvp_upload_a_plan_and_get_a_model(stack: DesignStack):
    """MANDATORY: a floor plan becomes a 3D model through conversation alone.

    Asserts, in order:

      1. a PDF plan uploads and is readable as pages
      2. the plan's text reaches the model as context
      3. the assistant asks for the one dimension the drawing does not give
      4. the answer is remembered as a durable project fact
      5. the plan is proposed, dispatched as ONE job, and applied
      6. four walls and a floor exist, with correct metric geometry
      7. the project was saved
      8. a GLB model artifact was produced and indexed
      9. the GLB is downloadable over HTTP and really is a GLB
     10. the browser can restore the whole workspace after a reload
     11. no filesystem path leaks through any of it
    """
    # ---- 1. upload the plan -----------------------------------------------
    upload = stack.upload("ground-floor.pdf", pdf_bytes(pages=2))
    assert upload.status_code == 201, upload.text
    reference = upload.json()
    assert reference["kind"] == "pdf"
    assert reference["page_count"] == 2
    assert reference["pages"], "a PDF must be expanded into page images"

    # ---- 2. the plan's text reaches the model -----------------------------
    stack.llm.queue_clarification(
        "What ceiling height should I use?", missing=["ceiling_height_m"]
    )
    asked = stack.chat(
        "req_mvp_001",
        "Build this floor plan in 3D.",
        attached_reference_ids=[reference["reference_id"]],
    )
    assert asked.status_code == 200, asked.text

    seen = stack.llm.last_input
    assert seen is not None
    assert any(PDF_SAMPLE_TEXT in document.text for document in seen.documents), (
        "the drawing's extracted text never reached the model"
    )
    assert seen.images, "the drawing's page images never reached the model"

    # ---- 3. the assistant asked, rather than guessing ---------------------
    body = asked.json()
    assert body["kind"] == "clarification"
    assert body["job_id"] is None, "a question must never mutate the project"
    assert stack.backend.invocations == [], "Blender was touched before the plan existed"
    clarification_id = body["clarification"]["clarification_id"]

    # ---- 4. the answer becomes a durable fact -----------------------------
    stack.llm.queue_response(
        response_body(
            "plan",
            message="Building a 4.00 x 3.00 m room with a 2.70 m ceiling.",
            operations=_room_plan(),
            design_facts=[{"key": "ceiling_height_m", "value": "2.70"}],
            assumptions=["Interior dimensions, measured to the inside face."],
        )
    )
    planned = stack.chat("req_mvp_002", "2.7 metres.")
    assert planned.status_code == 202, planned.text
    plan = planned.json()
    assert plan["kind"] == "plan"
    assert "ceiling_height_m" in plan["facts_recorded"]
    assert (
        stack.repositories.facts.as_mapping(stack.project_id)["ceiling_height_m"] == "2.70"
    )
    # The question it asked has been answered, so it is no longer open.
    assert stack.repositories.clarifications.open_for_project(stack.project_id) is None
    assert stack.repositories.clarifications.get(
        stack.project_id, clarification_id
    ).resolved_at

    # ---- 5. ONE job carries the whole plan --------------------------------
    assert plan["operation_count"] == 5
    job_id = plan["job_id"]
    assert job_id
    assert stack.pump_until(), "the worker never received the plan"
    final = stack.await_status(job_id, "succeeded")
    assert final["result"]["applied"] == 5, final["result"]

    # ---- 6. the geometry is right, in metres ------------------------------
    assert sorted(stack.backend.objects) == [
        "East",
        "Floor",
        "North",
        "South",
        "West",
    ]
    north = stack.object_named("North")
    assert north.dimensions[0] == pytest.approx(ROOM_WIDTH, abs=TOLERANCE)
    assert north.dimensions[1] == pytest.approx(WALL_THICKNESS, abs=TOLERANCE)
    assert north.dimensions[2] == pytest.approx(CEILING_HEIGHT, abs=TOLERANCE)
    # A wall stands ON the floor: its centre is at half its height.
    assert north.position[2] == pytest.approx(CEILING_HEIGHT / 2, abs=TOLERANCE)
    east = stack.object_named("East")
    assert east.rotation[2] == pytest.approx(-math.pi / 2, abs=1e-9), (
        "the east wall should run along Y, not X"
    )
    floor = stack.object_named("Floor")
    assert floor.dimensions[0] == pytest.approx(ROOM_WIDTH, abs=TOLERANCE)
    assert floor.dimensions[1] == pytest.approx(ROOM_DEPTH, abs=TOLERANCE)

    # ---- 7. the project was saved ----------------------------------------
    assert stack.backend.saves >= 5, "the project was not saved after modelling"

    # ---- 8. a GLB was produced and indexed -------------------------------
    model = final["result"]["model"]
    assert model["artifact_type"] == "model_glb"
    assert model["media_type"] == "model/gltf-binary"
    indexed = stack.repositories.artifacts.latest(stack.project_id, "model_glb")
    assert indexed is not None
    assert indexed["artifact_id"] == model["artifact_id"]

    # ---- 9. and it is downloadable, and really is a GLB ------------------
    latest = stack.http.get(f"/api/projects/{stack.project_id}/model/latest")
    assert latest.status_code == 200, latest.text
    url = latest.json()["model"]["url"]
    assert url.startswith(f"/api/projects/{stack.project_id}/artifacts/"), url
    fetched = stack.http.get(url)
    assert fetched.status_code == 200, fetched.text
    assert fetched.headers["content-type"].startswith("model/gltf-binary")
    assert fetched.content.startswith(b"glTF"), "that is not a glTF binary"

    # ---- 10. the browser can restore everything --------------------------
    workspace = stack.workspace()
    assert workspace["scene"]["scene_version"]
    assert len(workspace["scene"]["objects"]) == 5
    assert workspace["model"]["artifact_id"] == model["artifact_id"]
    assert [reference["reference_id"] for reference in workspace["references"]] == [
        reference["reference_id"]
    ]
    assert {fact["key"] for fact in workspace["facts"]} == {"ceiling_height_m"}
    roles = [turn["role"] for turn in workspace["conversation"]]
    assert roles == ["user", "assistant", "user", "assistant"], roles
    assert workspace["clarification"] is None
    assert workspace["approvals"] == []

    # ---- 11. nothing leaks a path ----------------------------------------
    for payload in (upload.json(), asked.json(), planned.json(), final, workspace):
        text = repr(payload)
        assert str(stack.project_path) not in text
        assert str(stack.runtime_root) not in text
        assert "/tmp/" not in text, text[:400]


# ===========================================================================
# The parts of the promise that deserve their own scenario
# ===========================================================================


def test_a_question_is_answerable_without_any_upload_or_mutation(stack: DesignStack):
    """Talking about the project must not require a job."""
    stack.llm.queue_answer("A 2.70 m ceiling is normal for a living space.")
    response = stack.chat("req_mvp_010", "Is 2.7 m a normal ceiling height?")

    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "answer"
    assert response.json()["job_id"] is None
    assert stack.backend.invocations == []
    assert stack.repositories.scenes.get(stack.project_id) is None


def test_the_same_plan_uploaded_twice_is_stored_once(stack: DesignStack):
    """Uploading the same drawing again is a mistake, not a second reference."""
    data = pdf_bytes(pages=1)
    first = stack.upload("plan.pdf", data)
    second = stack.upload("plan.pdf", data)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["deduplicated"] is True
    assert second.json()["reference_id"] == first.json()["reference_id"]
    assert len(stack.repositories.references.list_for_project(stack.project_id)) == 1


def test_progress_is_reported_for_every_step_of_a_long_plan(stack: DesignStack):
    """A five-step plan must not look frozen in the browser."""
    stack.llm.queue_plan(_room_plan(), message="Building the room.")
    response = stack.chat("req_mvp_020", "Build a 4 by 3 room, 2.7 m tall.")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert stack.pump_until()
    stack.await_status(job_id, "succeeded")

    record = stack.dependencies.store.get(stack.project_id, job_id)
    assert record is not None
    # The last progress report the control plane saw for this job.
    assert record.progress is not None, "no step progress ever reached the API"
    assert record.progress["step_count"] == 5
    assert record.progress["label"]


def test_a_project_with_no_worker_connected_refuses_instead_of_pretending(
    tmp_path: Path,
):
    """Blender unavailable is the single most likely real failure, so it is explicit."""
    with build_design_stack(tmp_path, connect=False) as stack:
        stack.llm.queue_plan(_room_plan())
        response = stack.chat("req_mvp_030", "Build a 4 by 3 room.")

        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "BLENDER_UNAVAILABLE"
        assert stack.backend.invocations == []
        # The user's message is still in the transcript: their words are never lost
        # because the machine was offline.
        assert stack.repositories.conversation.count(stack.project_id) >= 1
