"""The workspace HTTP surface, driven through the real application."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio_agent.providers.fake_llm import FakeLlmProvider, operation, response_body
from studio_api.app import create_app
from studio_api.dependencies import build_dependencies
from studio_api.settings import Settings
from studio_fixtures.sample_files import jpeg_bytes, pdf_bytes, png_bytes

PROJECT = "proj_seed"
SAFE_CODE = "import bpy\nbpy.ops.mesh.primitive_cube_add(size=1.0)\n"
RISKY_CODE = "import shutil\nshutil.rmtree('/home/christian')\n"


@pytest.fixture()
def harness(tmp_path: Path):
    settings = Settings(
        environment="local",
        design_provider="fake_llm",
        database_path=str(tmp_path / "studio.sqlite3"),
        reference_root=str(tmp_path / "references"),
        artifact_root=str(tmp_path / "artifacts"),
        project_ids=(PROJECT,),
    )
    provider = FakeLlmProvider()
    dependencies = build_dependencies(settings, design_provider=provider)
    app = create_app(settings, dependencies)
    return TestClient(app), provider, dependencies


def upload(client: TestClient, filename: str, data: bytes, media_type: str) -> Any:
    return client.post(
        f"/api/projects/{PROJECT}/references",
        files={"file": (filename, data, media_type)},
    )


def chat(client: TestClient, message: str, **kwargs: Any) -> Any:
    body = {
        "request_id": kwargs.pop("request_id", "req_1"),
        "session_id": "sess_1",
        "message": message,
    }
    body.update(kwargs)
    return client.post(f"/api/projects/{PROJECT}/design-chat", json=body)


# --- uploads --------------------------------------------------------------


def test_an_image_upload_returns_a_browser_safe_record(harness) -> None:
    client, _, _ = harness
    response = upload(client, "room.jpg", jpeg_bytes(width=40, height=30), "image/jpeg")

    assert response.status_code == 201
    body = response.json()
    assert body["display_name"] == "room.jpg"
    assert body["kind"] == "image"
    assert body["width"] == 40
    assert body["is_image"] is True

    serialized = json.dumps(body)
    assert "stored_name" not in serialized
    assert "/home" not in serialized
    assert "sha256" not in serialized


def test_a_pdf_upload_returns_its_pages(harness) -> None:
    client, _, _ = harness
    body = upload(client, "floor-plan.pdf", pdf_bytes(pages=2), "application/pdf").json()

    assert body["kind"] == "pdf"
    assert body["page_count"] == 2
    assert [page["page_number"] for page in body["pages"]] == [1, 2]
    assert body["pages"][0]["media_type"] == "image/png"


def test_page_bytes_are_served_for_the_browser(harness) -> None:
    client, _, _ = harness
    body = upload(client, "floor-plan.pdf", pdf_bytes(pages=1), "application/pdf").json()
    page_id = body["pages"][0]["reference_id"]

    response = client.get(f"/api/projects/{PROJECT}/references/{page_id}/content")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in response.headers["cache-control"]


def test_an_unsupported_upload_is_refused_with_a_readable_message(harness) -> None:
    client, _, _ = harness
    response = upload(client, "payload.exe", b"MZ\x90\x00" + b"\x00" * 100, "application/octet-stream")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "PNG" in body["error"]["message"]


def test_a_disguised_upload_is_refused(harness) -> None:
    client, _, _ = harness
    response = upload(client, "plan.png", pdf_bytes(pages=1), "image/png")
    assert response.status_code == 422
    assert "different kind of file" in response.json()["error"]["message"]


def test_references_are_listed_with_their_pages(harness) -> None:
    client, _, _ = harness
    upload(client, "floor-plan.pdf", pdf_bytes(pages=2), "application/pdf")
    upload(client, "room.jpg", jpeg_bytes(), "image/jpeg")

    body = client.get(f"/api/projects/{PROJECT}/references").json()
    names = [reference["display_name"] for reference in body["references"]]
    assert names == ["floor-plan.pdf", "room.jpg"]
    plan = body["references"][0]
    assert len(plan["pages"]) == 2


def test_a_reference_can_be_deleted(harness) -> None:
    client, _, _ = harness
    reference_id = upload(client, "room.jpg", jpeg_bytes(), "image/jpeg").json()["reference_id"]

    assert client.delete(f"/api/projects/{PROJECT}/references/{reference_id}").status_code == 200
    assert client.get(f"/api/projects/{PROJECT}/references").json()["references"] == []
    assert (
        client.get(f"/api/projects/{PROJECT}/references/{reference_id}/content").status_code == 404
    )


def test_an_unknown_project_is_refused_everywhere(harness) -> None:
    client, _, _ = harness
    assert client.get("/api/projects/proj_nope/references").status_code == 404
    assert client.get("/api/projects/proj_nope/workspace").status_code == 404
    assert (
        client.post(
            "/api/projects/proj_nope/design-chat",
            json={"request_id": "r", "session_id": "s", "message": "hi"},
        ).status_code
        == 404
    )


# --- the conversation -----------------------------------------------------


def test_an_answer_comes_back_as_a_single_reply(harness) -> None:
    client, provider, _ = harness
    provider.queue_answer("There is nothing in the scene yet.")

    response = chat(client, "what is in my project?")
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "answer"
    assert body["message"] == "There is nothing in the scene yet."
    assert body["job_id"] is None


def test_a_clarification_comes_back_as_a_question(harness) -> None:
    client, provider, _ = harness
    provider.queue_clarification("What is the ceiling height?", ["ceiling_height_m"])

    body = chat(client, "reconstruct this plan").json()
    assert body["kind"] == "clarification"
    assert body["clarification"]["question"] == "What is the ceiling height?"
    assert body["clarification"]["missing_information"] == ["ceiling_height_m"]
    assert body["job_id"] is None


def test_attached_references_reach_the_agent(harness) -> None:
    client, provider, _ = harness
    reference_id = upload(client, "room.jpg", jpeg_bytes(), "image/jpeg").json()["reference_id"]
    provider.queue_answer("I can see a room.")

    chat(client, "what do you see?", attached_reference_ids=[reference_id])
    assert provider.last_input is not None
    assert [image.reference_id for image in provider.last_input.images] == [reference_id]


def test_the_selected_object_reaches_the_agent(harness) -> None:
    client, provider, _ = harness
    provider.queue_answer("ok")
    chat(client, "make this taller", selected_object_id="obj_wall_3")
    assert provider.last_input.selected_object_id == "obj_wall_3"


def test_facts_recorded_by_the_agent_persist(harness) -> None:
    client, provider, _ = harness
    provider.queue_response(
        response_body(
            "answer",
            message="Noted.",
            design_facts=[{"key": "ceiling_height_m", "value": "2.4"}],
        )
    )
    body = chat(client, "the ceiling is 2.4 m").json()
    assert body["facts_recorded"] == ["ceiling_height_m"]

    workspace = client.get(f"/api/projects/{PROJECT}/workspace").json()
    assert {fact["key"]: fact["value"] for fact in workspace["facts"]} == {
        "ceiling_height_m": "2.4"
    }


def test_a_user_can_correct_a_fact_directly(harness) -> None:
    client, _, _ = harness
    response = client.put(
        f"/api/projects/{PROJECT}/facts", json={"key": "Ceiling_Height_M", "value": "2.7"}
    )
    assert response.status_code == 200
    assert response.json()["fact"]["key"] == "ceiling_height_m"
    assert response.json()["fact"]["source"] == "user"


def test_a_plan_without_a_worker_reports_that_nothing_changed(harness) -> None:
    client, provider, _ = harness
    provider.queue_plan(
        [
            operation(
                "create_wall",
                {
                    "display_name": "Wall",
                    "start_meters": {"x": 0.0, "y": 0.0},
                    "end_meters": {"x": 4.0, "y": 0.0},
                    "height_meters": 2.4,
                    "thickness_meters": 0.12,
                },
            )
        ]
    )
    response = chat(client, "build a wall")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BLENDER_UNAVAILABLE"


def test_a_malformed_chat_body_is_refused(harness) -> None:
    client, _, _ = harness
    assert client.post(f"/api/projects/{PROJECT}/design-chat", json={}).status_code == 422
    assert (
        client.post(
            f"/api/projects/{PROJECT}/design-chat",
            json={"request_id": "r", "session_id": "s", "message": "hi", "sneaky": 1},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/projects/{PROJECT}/design-chat",
            json={"request_id": "r", "session_id": "s", "message": "   "},
        ).status_code
        == 422
    )


# --- approvals ------------------------------------------------------------
#
# These simulate a connected design machine. Blender availability is checked BEFORE
# classification on purpose: if nothing can be modelled at all, asking the user to
# approve a step that cannot run would be a worse experience than saying so.


@pytest.fixture()
def connected(harness):
    """The harness with a design machine reported as available."""
    client, provider, dependencies = harness
    dependencies.design_chat.blender_available = lambda: True
    return client, provider, dependencies


def test_risky_code_returns_an_approval_request_with_the_code(connected) -> None:
    client, provider, _ = connected
    provider.queue_plan([operation("execute_blender_python", {"code": RISKY_CODE})])

    body = chat(client, "tidy up my files").json()
    assert body["kind"] == "approval_required"
    assert body["job_id"] is None
    approval = body["approval"]
    assert approval["code"] == RISKY_CODE
    assert any("shutil" in reason for reason in approval["reasons"])

    listed = client.get(f"/api/projects/{PROJECT}/approvals").json()
    assert [record["approval_id"] for record in listed["approvals"]] == [
        approval["approval_id"]
    ]


def test_rejecting_an_approval_runs_nothing(connected) -> None:
    client, provider, _ = connected
    provider.queue_plan([operation("execute_blender_python", {"code": RISKY_CODE})])
    approval_id = chat(client, "tidy up").json()["approval"]["approval_id"]

    response = client.post(
        f"/api/projects/{PROJECT}/approvals/{approval_id}", json={"approved": False}
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "answer"
    assert "did not run" in response.json()["message"]
    assert client.get(f"/api/projects/{PROJECT}/approvals").json()["approvals"] == []


def test_an_approval_cannot_be_decided_twice(connected) -> None:
    client, provider, _ = connected
    provider.queue_plan([operation("execute_blender_python", {"code": RISKY_CODE})])
    approval_id = chat(client, "tidy up").json()["approval"]["approval_id"]

    client.post(f"/api/projects/{PROJECT}/approvals/{approval_id}", json={"approved": False})
    again = client.post(
        f"/api/projects/{PROJECT}/approvals/{approval_id}", json={"approved": True}
    )
    assert again.status_code == 409


def test_an_unknown_approval_is_refused(harness) -> None:
    client, _, _ = harness
    response = client.post(
        f"/api/projects/{PROJECT}/approvals/apr_nothing", json={"approved": True}
    )
    assert response.status_code == 404


# --- workspace restore ----------------------------------------------------


def test_the_workspace_endpoint_restores_everything_in_one_request(harness) -> None:
    client, provider, dependencies = harness
    upload(client, "floor-plan.pdf", pdf_bytes(pages=1), "application/pdf")
    provider.queue_clarification("What is the ceiling height?", ["ceiling_height_m"])
    chat(client, "reconstruct this")
    client.put(f"/api/projects/{PROJECT}/facts", json={"key": "ceiling_height_m", "value": "2.4"})

    body = client.get(f"/api/projects/{PROJECT}/workspace").json()
    assert body["project"]["project_id"] == PROJECT
    assert [r["display_name"] for r in body["references"]] == ["floor-plan.pdf"]
    assert {f["key"] for f in body["facts"]} == {"ceiling_height_m"}
    assert [turn["role"] for turn in body["conversation"]] == ["user", "assistant"]
    assert body["clarification"]["question"] == "What is the ceiling height?"
    assert body["scene"] is None
    assert body["model"] is None


def test_the_workspace_survives_a_restart(harness, tmp_path: Path) -> None:
    client, provider, _ = harness
    upload(client, "room.jpg", jpeg_bytes(), "image/jpeg")
    client.put(f"/api/projects/{PROJECT}/facts", json={"key": "ceiling_height_m", "value": "2.4"})

    # A brand-new application over the same durable state.
    settings = Settings(
        environment="local",
        design_provider="fake_llm",
        database_path=str(tmp_path / "studio.sqlite3"),
        reference_root=str(tmp_path / "references"),
        artifact_root=str(tmp_path / "artifacts"),
        project_ids=(PROJECT,),
    )
    restarted = TestClient(
        create_app(settings, build_dependencies(settings, design_provider=FakeLlmProvider()))
    )
    body = restarted.get(f"/api/projects/{PROJECT}/workspace").json()
    assert [r["display_name"] for r in body["references"]] == ["room.jpg"]
    assert {f["key"]: f["value"] for f in body["facts"]} == {"ceiling_height_m": "2.4"}


def test_the_scene_and_model_endpoints_report_nothing_before_any_modelling(harness) -> None:
    client, _, _ = harness
    assert client.get(f"/api/projects/{PROJECT}/scene").json()["scene"] is None
    assert client.get(f"/api/projects/{PROJECT}/model/latest").json()["model"] is None


def test_a_reported_scene_and_model_become_visible(harness) -> None:
    """What the worker reports is what the browser sees."""
    client, _, dependencies = harness

    dependencies.scene_reporter.observe(
        {
            "type": "job_result",
            "project_id": PROJECT,
            "job_id": "job_1",
            "job_status": "succeeded",
            "result": {
                "scene": {
                    "project_id": PROJECT,
                    "scene_version": "sha256:abc",
                    "captured_at": "2026-01-01T00:00:00Z",
                    "units": {"unit_system": "METRIC", "length_unit": "m", "scale_length": 1.0},
                    "objects": [],
                },
                "model": {
                    "artifact_id": "model_abc12345",
                    "artifact_type": "model_glb",
                    "media_type": "model/gltf-binary",
                    "created_at": "2026-01-01T00:00:01Z",
                    "size_bytes": 2048,
                    "checksum": "sha256:" + "a" * 64,
                },
            },
        }
    )

    scene = client.get(f"/api/projects/{PROJECT}/scene").json()["scene"]
    assert scene["scene_version"] == "sha256:abc"

    model = client.get(f"/api/projects/{PROJECT}/model/latest").json()["model"]
    assert model["artifact_id"] == "model_abc12345"
    assert model["url"] == f"/api/projects/{PROJECT}/artifacts/model_abc12345"


def test_the_scene_reporter_ignores_malformed_reports(harness) -> None:
    client, _, dependencies = harness
    for message in (
        {"type": "job_result", "project_id": PROJECT, "result": {"scene": {"objects": "nope"}}},
        {"type": "job_result", "project_id": PROJECT, "result": {"scene": {}}},
        {"type": "heartbeat", "project_id": PROJECT},
        {"type": "job_result"},
    ):
        dependencies.scene_reporter.observe(message)
    assert client.get(f"/api/projects/{PROJECT}/scene").json()["scene"] is None


# --- status ---------------------------------------------------------------


def test_the_astra_status_reports_a_ready_offline_provider(harness) -> None:
    client, _, _ = harness
    body = client.get("/api/status/astra").json()
    assert body["connected"] is True
    assert body["provider"] == "fake_llm"


def test_the_blender_status_reports_disconnected_with_an_action(harness) -> None:
    client, _, _ = harness
    body = client.get("/api/status/blender").json()
    assert body["connected"] is False
    assert body["supports_modelling"] is False
    assert "worker" in body["message"].lower()


def test_no_status_response_contains_a_credential(harness, monkeypatch) -> None:
    monkeypatch.setenv("STUDIO_WORKER_TOKEN", "super-secret-token")
    client, _, _ = harness
    for path in ("/api/status/astra", "/api/status/blender"):
        serialized = json.dumps(client.get(path).json())
        assert "super-secret-token" not in serialized
        assert "OPENAI" not in serialized


def test_the_astra_status_of_a_codex_provider_never_claims_a_wrong_model(tmp_path: Path) -> None:
    """A logged-out Codex must report login required, not "connected"."""
    import subprocess

    from studio_agent.codex import CodexClient
    from studio_agent.providers.codex_astra import CodexAstraProvider

    def runner(command, *, stdin=None, timeout=None, cwd=None):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.154.0", "")
        return subprocess.CompletedProcess(command, 1, "Not logged in", "")

    settings = Settings(
        environment="local",
        design_provider="fake_llm",
        database_path=str(tmp_path / "studio.sqlite3"),
        reference_root=str(tmp_path / "refs"),
        artifact_root=str(tmp_path / "artifacts"),
        project_ids=(PROJECT,),
    )
    provider = CodexAstraProvider(client=CodexClient(executable="/usr/bin/true", runner=runner))
    client = TestClient(
        create_app(settings, build_dependencies(settings, design_provider=provider))
    )

    body = client.get("/api/status/astra").json()
    assert body["state"] == "login_required"
    assert body["connected"] is False
    assert body["label"] == "Astra via Codex"
    assert body["action"] == "codex login"
