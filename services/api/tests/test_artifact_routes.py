"""Preview artifact HTTP tests — no Blender (Spec 001, Task 10).

Covers required behaviours 10, 11, 12, 13 and 14 at the HTTP boundary: the job
status exposes a LOGICAL preview URL and never a local path, another project cannot
retrieve an artifact, there is no unscoped artifact route, the API imports no
Blender implementation, and artifact lookup cannot reach a .blend, a journal
record, or a recovery snapshot.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Optional

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")

from fastapi.testclient import TestClient  # noqa: E402

from studio_api.app import create_app  # noqa: E402
from studio_api.dependencies import build_dependencies  # noqa: E402
from studio_api.settings import Settings  # noqa: E402
from studio_api.worker_link.manager import WorkerConnectionManager  # noqa: E402
from studio_contracts import worker_protocol as protocol  # noqa: E402
from studio_preview.artifacts import (  # noqa: E402
    LocalArtifactStore,
    derive_artifact_id,
    to_artifact_wire,
)
from studio_preview.generator import PNG_SIGNATURE, looks_like_png  # noqa: E402

TOKEN = "artifact-test-token-not-committed"
PROJECT_ID = "proj_seed"
OTHER_PROJECT = "proj_other"
WORKER_ID = "worker_artifact_1"
DEV_USER_ID = "user_dev_local"

MOVE_COMMAND = "Move Cube 50 cm to the right."

PNG_BYTES = PNG_SIGNATURE + b"deterministic-fake-png-body"

API_SOURCE_DIR = Path(__file__).resolve().parents[1] / "studio_api"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def build_app(
    tmp_path: Path,
    project_ids: tuple[str, ...] = (PROJECT_ID,),
) -> tuple[TestClient, Any]:
    settings = Settings(
        environment="local",
        worker_token=TOKEN,
        project_ids=project_ids,
        development_user_id=DEV_USER_ID,
        heartbeat_interval_seconds=10.0,
        artifact_root=str(tmp_path / "artifacts"),
    )
    manager = WorkerConnectionManager(
        expected_token=TOKEN, heartbeat_interval_seconds=10.0
    )
    dependencies = build_dependencies(settings, manager=manager)
    app = create_app(settings, dependencies)
    return TestClient(app, raise_server_exceptions=False), dependencies


class FakeWorker:
    def __init__(self, worker_id: str = WORKER_ID) -> None:
        self.worker_id = worker_id
        self.received: list[dict] = []

    def sink(self, message: dict) -> bool:
        self.received.append(message)
        return True

    @property
    def offers(self) -> list[dict]:
        return [m for m in self.received if m["type"] == protocol.JOB_OFFER]


def connect_worker(dependencies: Any) -> FakeWorker:
    worker = FakeWorker()
    reply = dependencies.gateway.handle_inbound(
        protocol.worker_hello(
            worker_id=worker.worker_id,
            token=TOKEN,
            capabilities={
                "worker_version": "0.1.0",
                "blender_available": True,
                "supported_job_types": ["move_object"],
                "max_concurrent_jobs": 1,
            },
        )
    )
    assert reply["type"] == protocol.WORKER_REGISTERED, reply
    dependencies.gateway.attach(worker.worker_id, worker.sink)
    return worker


def chat_body(request_id: str, project_id: str = PROJECT_ID) -> dict:
    return {
        "request_id": request_id,
        "project_id": project_id,
        "session_id": "sess_artifact",
        "message": MOVE_COMMAND,
    }


def store_artifact(
    dependencies: Any,
    job_id: str,
    project_id: str = PROJECT_ID,
    data: bytes = PNG_BYTES,
):
    """Store an artifact exactly as the worker would."""
    artifact_id = derive_artifact_id(project_id, job_id)
    return dependencies.artifacts.put(
        project_id=project_id,
        artifact_id=artifact_id,
        data=data,
        media_type="image/png",
        width=640,
        height=360,
        job_id=job_id,
        engine="BLENDER_WORKBENCH",
    )


def report_success(
    dependencies: Any,
    worker: FakeWorker,
    job_id: str,
    project_id: str = PROJECT_ID,
    preview: Optional[dict] = None,
    preview_error: Optional[dict] = None,
    final_x: float = 0.5,
) -> None:
    dependencies.gateway.handle_inbound(
        protocol.job_result(
            worker_id=worker.worker_id,
            job_id=job_id,
            project_id=project_id,
            job_status="succeeded",
            result={
                "job_id": job_id,
                "target": {"name": "Cube"},
                "requested_delta_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
                "applied": True,
                "already_applied": False,
                "verified": True,
                "final_position_meters": {"x": final_x, "y": 0.0, "z": 0.0},
            },
            execution_phase="completed",
            preview=preview,
            preview_error=preview_error,
        )
    )


def job_path(job_id: str, project_id: str = PROJECT_ID) -> str:
    return f"/api/projects/{project_id}/jobs/{job_id}"


def artifact_path(artifact_id: str, project_id: str = PROJECT_ID) -> str:
    return f"/api/projects/{project_id}/artifacts/{artifact_id}"


def submit_and_complete(
    client: TestClient,
    dependencies: Any,
    worker: FakeWorker,
    request_id: str,
    final_x: float = 0.5,
) -> tuple[str, Any]:
    """Full happy path: submit, store the artifact, report success."""
    body = client.post("/api/chat", json=chat_body(request_id)).json()
    job_id = body["job_id"]
    artifact = store_artifact(dependencies, job_id)
    report_success(
        dependencies,
        worker,
        job_id,
        preview=to_artifact_wire(artifact),
        final_x=final_x,
    )
    return job_id, artifact


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


# ---------------------------------------------------------------------------
# 10. Job status exposes a LOGICAL preview URL, never a local path
# ---------------------------------------------------------------------------


def test_10_job_status_exposes_a_logical_preview_url(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    job_id, artifact = submit_and_complete(client, dependencies, worker, "req_prev_1")

    status = client.get(job_path(job_id)).json()

    assert status["job_status"] == "succeeded"
    preview = status["preview"]
    assert preview is not None
    assert preview["artifact_id"] == artifact.artifact_id
    assert preview["media_type"] == "image/png"
    assert preview["url"] == artifact_path(artifact.artifact_id)
    assert preview["width"] == 640
    assert preview["height"] == 360
    assert preview["size_bytes"] == len(PNG_BYTES)
    assert preview["checksum"] == artifact.checksum
    assert status["preview_error"] is None


def test_10_the_preview_url_from_the_job_status_resolves(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    job_id, _ = submit_and_complete(client, dependencies, worker, "req_prev_2")

    url = client.get(job_path(job_id)).json()["preview"]["url"]
    response = client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert looks_like_png(response.content)
    assert response.content == PNG_BYTES


def test_10_job_status_never_exposes_a_local_filesystem_path(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    job_id, _ = submit_and_complete(client, dependencies, worker, "req_prev_3")

    text = client.get(job_path(job_id)).text
    artifact_root = str(dependencies.artifacts.root)

    for leaked in (artifact_root, str(tmp_path), ".png", ".blend", "/home", "/tmp"):
        assert leaked not in text, f"job status leaked {leaked!r}"


def test_10_the_png_is_never_inlined_as_base64_in_job_json(tmp_path):
    """A status endpoint is POLLED; inlining an image would send it every time."""
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    job_id, _ = submit_and_complete(client, dependencies, worker, "req_prev_4")

    body = client.get(job_path(job_id)).json()
    text = json.dumps(body)

    assert "base64" not in text
    assert "data:image" not in text
    # The whole status document stays far smaller than the image it references.
    assert len(text) < len(PNG_BYTES) + 2000
    assert body["preview"]["size_bytes"] == len(PNG_BYTES)


def test_10_the_canonical_chat_response_carries_the_preview_url(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    job_id, artifact = submit_and_complete(client, dependencies, worker, "req_prev_5")

    chat = client.get(job_path(job_id)).json()["chat"]

    assert chat["status"] == "success"
    assert chat["preview_url"] == artifact_path(artifact.artifact_id)

    # It really is the canonical contract.
    from studio_contracts import SCHEMA_FILES, validate_against_schema

    result = validate_against_schema(
        SCHEMA_FILES["ChatResponse"],
        {k: v for k, v in chat.items() if v is not None},
    )
    assert result.valid, result.violations


def test_10_a_job_with_no_preview_reports_null_not_a_broken_url(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    body = client.post("/api/chat", json=chat_body("req_no_prev")).json()
    report_success(dependencies, worker, body["job_id"])  # no preview reported

    status = client.get(job_path(body["job_id"])).json()

    assert status["job_status"] == "succeeded"
    assert status["preview"] is None
    assert status["chat"]["preview_url"] is None


def test_10_a_degraded_preview_keeps_the_job_succeeded(tmp_path):
    """Option B: the change was applied; only the picture is missing."""
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    body = client.post("/api/chat", json=chat_body("req_degraded")).json()
    report_success(
        dependencies,
        worker,
        body["job_id"],
        preview_error={
            "code": "BLENDER_UNAVAILABLE",
            "message": "no preview could be generated",
        },
    )

    status = client.get(job_path(body["job_id"])).json()

    assert status["job_status"] == "succeeded", "the design change did succeed"
    assert status["preview"] is None
    assert status["preview_error"]["code"] == "BLENDER_UNAVAILABLE"
    assert status["chat"]["status"] == "success"
    assert "preview" in status["chat"]["summary"].lower()


def test_10_a_malformed_preview_report_degrades_to_no_preview(tmp_path):
    """A bad report must not produce a broken URL."""
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    body = client.post("/api/chat", json=chat_body("req_bad_prev")).json()

    # Bypass the protocol builder, as a buggy peer might.
    record = dependencies.store.get(PROJECT_ID, body["job_id"])
    record.job_status = "succeeded"
    record.preview = {"artifact_id": "not-a-valid-id"}
    dependencies.store.save(record)

    status = client.get(job_path(body["job_id"])).json()
    assert status["preview"] is None


def test_10_a_preview_attributed_to_another_project_is_ignored(tmp_path):
    """A worker must not be able to attribute an artifact to a different project."""
    client, dependencies = build_app(tmp_path, project_ids=(PROJECT_ID, OTHER_PROJECT))
    worker = connect_worker(dependencies)
    body = client.post("/api/chat", json=chat_body("req_cross_attr")).json()

    foreign = store_artifact(dependencies, "job_foreign", project_id=OTHER_PROJECT)
    record = dependencies.store.get(PROJECT_ID, body["job_id"])
    record.job_status = "succeeded"
    record.preview = to_artifact_wire(foreign)
    dependencies.store.save(record)

    status = client.get(job_path(body["job_id"])).json()
    assert status["preview"] is None, "cross-project attribution must be rejected"


# ---------------------------------------------------------------------------
# The artifact route itself
# ---------------------------------------------------------------------------


def test_artifact_route_returns_the_png_with_the_right_content_type(tmp_path):
    client, dependencies = build_app(tmp_path)
    artifact = store_artifact(dependencies, "job_direct")

    response = client.get(artifact_path(artifact.artifact_id))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(PNG_BYTES))
    assert response.headers["etag"] == f'"{artifact.checksum}"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == PNG_BYTES
    assert looks_like_png(response.content)


def test_artifact_route_is_cacheable_but_private(tmp_path):
    client, dependencies = build_app(tmp_path)
    artifact = store_artifact(dependencies, "job_cache")

    headers = client.get(artifact_path(artifact.artifact_id)).headers
    cache_control = headers["cache-control"]

    assert "private" in cache_control, "artifacts are project-scoped, not shared"
    assert "immutable" in cache_control


def test_artifact_route_404s_for_an_unknown_artifact(tmp_path):
    client, _ = build_app(tmp_path)

    response = client.get(artifact_path("preview_" + "0" * 32))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_artifact_route_404s_when_the_bytes_are_missing(tmp_path):
    """Metadata without bytes must not serve a truncated or empty image."""
    client, dependencies = build_app(tmp_path)
    artifact = store_artifact(dependencies, "job_partial")
    (dependencies.artifacts.root / PROJECT_ID / f"{artifact.artifact_id}.png").unlink()

    assert client.get(artifact_path(artifact.artifact_id)).status_code == 404


# ---------------------------------------------------------------------------
# 11. Another project cannot retrieve an artifact
# ---------------------------------------------------------------------------


def test_11_another_project_cannot_retrieve_an_artifact(tmp_path):
    client, dependencies = build_app(tmp_path, project_ids=(PROJECT_ID, OTHER_PROJECT))
    artifact = store_artifact(dependencies, "job_isolated")

    assert client.get(artifact_path(artifact.artifact_id, PROJECT_ID)).status_code == 200

    # OTHER_PROJECT is a real, authorized project — just not this artifact's.
    response = client.get(artifact_path(artifact.artifact_id, OTHER_PROJECT))
    assert response.status_code == 404


def test_11_a_cross_project_miss_is_indistinguishable_from_a_real_miss(tmp_path):
    """404 bodies must not reveal cross-project existence."""
    client, dependencies = build_app(tmp_path, project_ids=(PROJECT_ID, OTHER_PROJECT))
    artifact = store_artifact(dependencies, "job_probe")

    existing_elsewhere = client.get(
        artifact_path(artifact.artifact_id, OTHER_PROJECT)
    )
    never_existed = client.get(artifact_path("preview_" + "f" * 32, OTHER_PROJECT))

    assert existing_elsewhere.status_code == never_existed.status_code == 404
    assert existing_elsewhere.json() == never_existed.json()
    assert artifact.artifact_id not in existing_elsewhere.text


def test_11_an_unknown_project_cannot_retrieve_an_artifact(tmp_path):
    client, dependencies = build_app(tmp_path)
    artifact = store_artifact(dependencies, "job_unknown_proj")

    response = client.get(artifact_path(artifact.artifact_id, "proj_not_registered"))

    assert response.status_code == 404
    assert artifact.artifact_id not in response.text


@pytest.mark.parametrize(
    "hostile_project",
    ["../../etc", "..", ".", "proj_seed/../proj_other", "~"],
)
def test_11_a_path_shaped_project_cannot_retrieve_an_artifact(
    tmp_path, hostile_project
):
    client, dependencies = build_app(tmp_path)
    artifact = store_artifact(dependencies, "job_traversal_proj")

    response = client.get(
        f"/api/projects/{hostile_project}/artifacts/{artifact.artifact_id}"
    )

    assert response.status_code == 404
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# 12. No unscoped artifact route
# ---------------------------------------------------------------------------


def test_12_there_is_no_unscoped_artifact_route(tmp_path):
    client, dependencies = build_app(tmp_path)
    artifact = store_artifact(dependencies, "job_unscoped")

    for path in (
        f"/api/artifacts/{artifact.artifact_id}",
        f"/api/artifacts/{artifact.artifact_id}?project_id={PROJECT_ID}",
        f"/artifacts/{artifact.artifact_id}",
        f"/api/previews/{artifact.artifact_id}",
    ):
        assert client.get(path).status_code == 404, path


def test_12_only_the_scoped_artifact_route_is_documented(tmp_path):
    client, _ = build_app(tmp_path)
    paths = client.app.openapi()["paths"]

    assert "/api/projects/{project_id}/artifacts/{artifact_id}" in paths
    assert "/api/artifacts/{artifact_id}" not in paths


def test_12_there_is_no_artifact_listing_route(tmp_path):
    """Directory listings are not exposed."""
    client, dependencies = build_app(tmp_path)
    store_artifact(dependencies, "job_listing")

    for path in (
        f"/api/projects/{PROJECT_ID}/artifacts",
        f"/api/projects/{PROJECT_ID}/artifacts/",
    ):
        assert client.get(path).status_code in (404, 405), path

    paths = client.app.openapi()["paths"]
    assert f"/api/projects/{{project_id}}/artifacts" not in paths


# ---------------------------------------------------------------------------
# 13. The API imports no bpy and no preview Blender script
# ---------------------------------------------------------------------------


def test_13_api_imports_no_bpy_and_no_blender_implementation():
    forbidden = ("bpy", "blender_worker", "blender_mcp")
    for path in API_SOURCE_DIR.rglob("*.py"):
        modules = _imports(path)
        for module in forbidden:
            assert module not in modules, f"{path} imports {module}"


def test_13_api_never_imports_the_blender_preview_generator():
    """The control plane serves artifacts; it never renders them.

    AST-based: ``dependencies.py`` deliberately DOCUMENTS why it does not import
    the Blender generator, so a substring search would flag the explanation rather
    than a violation. Only real imports are inspected.
    """
    forbidden_modules = {"blender_preview", "blender_scripts", "render_preview"}
    forbidden_symbols = {"BlenderPreviewGenerator", "default_preview_generator"}

    for path in API_SOURCE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                parts = set((node.module or "").split("."))
                assert not (parts & forbidden_modules), (
                    f"{path.name} imports from {node.module}"
                )
                for alias in node.names:
                    assert alias.name not in forbidden_symbols, (
                        f"{path.name} imports {alias.name}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = set(alias.name.split("."))
                    assert not (parts & forbidden_modules), (
                        f"{path.name} imports {alias.name}"
                    )


def test_13_api_imports_only_the_artifact_boundary_from_the_preview_package():
    """Importing the store must not drag in rendering."""
    referenced: set[str] = set()
    for path in API_SOURCE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "studio_preview"
            ):
                referenced.add(node.module)

    assert referenced, "the API does use the preview package"
    for module in referenced:
        assert module in ("studio_preview.artifacts",), (
            f"the API imports {module}; only the artifact boundary is allowed"
        )


def test_13_api_still_executes_no_subprocess():
    for path in API_SOURCE_DIR.rglob("*.py"):
        assert "subprocess" not in _imports(path), f"{path} imports subprocess"


# ---------------------------------------------------------------------------
# 14. Artifact lookup cannot reach .blend / recovery / journal files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_artifact_id",
    [
        "../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "preview_../../etc/passwd",
        "..",
        ".",
        "seed_project.blend",
        "seed_project",
        "job_req_1_0.json",
        ".env",
        "recovery.blend",
        "preview_UPPERCASE1234567",
        "preview_short",
        "preview_" + "z" * 200,
    ],
)
def test_14_artifact_lookup_rejects_hostile_identifiers(tmp_path, hostile_artifact_id):
    client, dependencies = build_app(tmp_path)
    store_artifact(dependencies, "job_real")

    response = client.get(f"/api/projects/{PROJECT_ID}/artifacts/{hostile_artifact_id}")

    assert response.status_code in (404, 400), response.status_code
    assert "Traceback" not in response.text
    assert "passwd" not in response.text
    assert "root:" not in response.text


def test_14_artifact_lookup_cannot_reach_a_blend_journal_or_recovery_file(tmp_path):
    """Plant the exact files that must never be served, then try to fetch them."""
    client, dependencies = build_app(tmp_path)
    project_dir = dependencies.artifacts.root / PROJECT_ID
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "seed_project.blend").write_bytes(b"BLENDER-secret-scene")
    (project_dir / "job_req_1_0.json").write_text('{"phase": "completed"}')
    (project_dir / ".env").write_text(f"STUDIO_WORKER_TOKEN={TOKEN}")
    (project_dir / "recovery.blend").write_bytes(b"pre-mutation recovery copy")

    for name in (
        "seed_project.blend",
        "seed_project",
        "job_req_1_0",
        "job_req_1_0.json",
        ".env",
        "recovery.blend",
        "recovery",
    ):
        response = client.get(f"/api/projects/{PROJECT_ID}/artifacts/{name}")
        assert response.status_code == 404, name
        assert b"BLENDER" not in response.content
        assert TOKEN not in response.text
        assert "completed" not in response.text


def test_14_a_token_planted_in_the_artifact_directory_is_never_served(tmp_path):
    client, dependencies = build_app(tmp_path)
    project_dir = dependencies.artifacts.root / PROJECT_ID
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "preview_secrets.json").write_text(json.dumps({"token": TOKEN}))

    for path in (
        f"/api/projects/{PROJECT_ID}/artifacts/preview_secrets",
        f"/api/projects/{PROJECT_ID}/artifacts/preview_secrets.json",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert TOKEN not in response.text


def test_14_the_artifact_route_never_reveals_a_path_in_any_response(tmp_path):
    client, dependencies = build_app(tmp_path)
    artifact = store_artifact(dependencies, "job_paths")
    root = str(dependencies.artifacts.root)

    ok = client.get(artifact_path(artifact.artifact_id))
    assert root not in str(dict(ok.headers))

    missing = client.get(artifact_path("preview_" + "1" * 32))
    assert root not in missing.text
    assert str(tmp_path) not in missing.text


# ---------------------------------------------------------------------------
# Versioning across requests
# ---------------------------------------------------------------------------


def test_a_new_request_yields_a_new_artifact_and_keeps_the_old_one(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)

    first_job, first_artifact = submit_and_complete(
        client, dependencies, worker, "req_v1", final_x=0.5
    )
    second_job, second_artifact = submit_and_complete(
        client, dependencies, worker, "req_v2", final_x=1.0
    )

    assert first_artifact.artifact_id != second_artifact.artifact_id

    # Both remain retrievable: the earlier preview was not overwritten.
    assert client.get(artifact_path(first_artifact.artifact_id)).status_code == 200
    assert client.get(artifact_path(second_artifact.artifact_id)).status_code == 200

    first_status = client.get(job_path(first_job)).json()
    second_status = client.get(job_path(second_job)).json()
    assert first_status["preview"]["url"] != second_status["preview"]["url"]


def test_a_retry_reports_the_same_preview_url(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    job_id, artifact = submit_and_complete(client, dependencies, worker, "req_retry")

    before = client.get(job_path(job_id)).json()["preview"]["url"]

    retry = client.post("/api/chat", json=chat_body("req_retry"))
    assert retry.status_code == 202
    assert retry.json()["duplicate"] is True

    after = client.get(job_path(job_id)).json()["preview"]["url"]
    assert after == before
    assert len(dependencies.artifacts.list_for_project(PROJECT_ID)) == 1


def test_a_reconciled_duplicate_result_keeps_the_preview(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    job_id, artifact = submit_and_complete(client, dependencies, worker, "req_recon")

    # The worker reconnects and resends the stored result as a duplicate.
    dependencies.gateway.handle_inbound(
        protocol.job_result(
            worker_id=worker.worker_id,
            job_id=job_id,
            project_id=PROJECT_ID,
            job_status="duplicate",
            result={"job_id": job_id, "verified": True},
            execution_phase="completed",
            preview=to_artifact_wire(artifact),
        )
    )

    status = client.get(job_path(job_id)).json()
    assert status["job_status"] == "succeeded"
    assert status["reconciled"] is True
    assert status["preview"]["artifact_id"] == artifact.artifact_id
    assert len(dependencies.artifacts.list_for_project(PROJECT_ID)) == 1


def test_a_preview_learned_only_on_reconciliation_is_recorded(tmp_path):
    """The crash window: the first result had no preview, the resend does."""
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    body = client.post("/api/chat", json=chat_body("req_late_prev")).json()
    job_id = body["job_id"]

    report_success(dependencies, worker, job_id)  # no preview yet
    assert client.get(job_path(job_id)).json()["preview"] is None

    artifact = store_artifact(dependencies, job_id)
    dependencies.gateway.handle_inbound(
        protocol.job_result(
            worker_id=worker.worker_id,
            job_id=job_id,
            project_id=PROJECT_ID,
            job_status="duplicate",
            result={"job_id": job_id, "verified": True},
            execution_phase="completed",
            preview=to_artifact_wire(artifact),
        )
    )

    status = client.get(job_path(job_id)).json()
    assert status["preview"]["artifact_id"] == artifact.artifact_id
    assert client.get(status["preview"]["url"]).status_code == 200


def test_a_failed_job_reports_no_preview(tmp_path):
    client, dependencies = build_app(tmp_path)
    worker = connect_worker(dependencies)
    body = client.post("/api/chat", json=chat_body("req_failed")).json()

    dependencies.gateway.handle_inbound(
        protocol.job_result(
            worker_id=worker.worker_id,
            job_id=body["job_id"],
            project_id=PROJECT_ID,
            job_status="failed",
            error={"code": "OBJECT_NOT_FOUND", "message": "no such object"},
            execution_phase="failed",
        )
    )

    status = client.get(job_path(body["job_id"])).json()
    assert status["job_status"] == "failed"
    assert status["preview"] is None
    assert status["chat"]["status"] == "error"


def test_the_protocol_forbids_a_preview_on_a_failed_result():
    """Defence in depth: the contract itself rejects the combination."""
    message = protocol.job_result(
        worker_id=WORKER_ID,
        job_id="job_x",
        project_id=PROJECT_ID,
        job_status="failed",
        error={"code": "MUTATION_FAILED", "message": "it broke"},
        preview={
            "artifact_id": "preview_" + "a" * 32,
            "project_id": PROJECT_ID,
            "artifact_type": "preview_image",
            "media_type": "image/png",
            "created_at": "2026-09-15T04:00:00Z",
            "width": 640,
            "height": 360,
            "size_bytes": 10,
            "checksum": "sha256:" + "a" * 64,
        },
    )
    assert protocol.validate(message), "a failed result must not carry a preview"


def test_the_protocol_forbids_preview_fields_on_other_message_types():
    for message in (
        protocol.heartbeat(WORKER_ID, "ready"),
        protocol.job_accepted(WORKER_ID, "job_x", PROJECT_ID),
    ):
        message["preview_error"] = {"code": "INTERNAL_ERROR", "message": "nope"}
        assert protocol.validate(message), (
            f"{message['type']} must not carry preview fields"
        )
