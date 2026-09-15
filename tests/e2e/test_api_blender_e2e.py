"""HTTP → Agent → Worker → Blender end-to-end test (Spec 001, Task 9).

OPT-IN: marked ``blender``, because it launches real Blender processes and is slow.

    pytest -m blender tests/e2e/test_api_blender_e2e.py -v

This is the closest approximation to the finished product that exists without a
browser. Every layer is the real implementation:

    httpx  --POST /api/chat-->  uvicorn + FastAPI          (real HTTP)
                                      v
                                RuleBasedProvider          (real AgentProvider)
                                      v
                                JobFactory -> canonical Job
                                      v
                                WorkerConnectionManager
                                      v  real WebSocket
                                WorkerLinkClient           (outbound connection)
                                      v
                                WorkerExecutor             (lock, journal, recovery)
                                      v
                                REAL BLENDER               (headless subprocess)
                                      v
                                Task 5 fixture copy        (isolated .blend)

Nothing is faked or stubbed. The only deviation from production is that the
control plane and the worker run on the same machine over loopback instead of
across TLS.

Verification discipline (.kiro/steering/testing.md): success is never inferred from
the absence of an exception. The cube's position is read back from the SAVED
``.blend`` by a fresh Blender process, so the assertion proves durability rather
than in-memory state.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("websockets", reason="websockets not installed")

from studio_fixtures.control_plane import ControlPlaneServer  # noqa: E402

from blender_mcp.blender_runtime import find_blender_executable  # noqa: E402
from blender_mcp.tolerance import coordinates_equal  # noqa: E402
from blender_worker import phases  # noqa: E402
from blender_worker.blender_ops import (  # noqa: E402
    SubprocessBlenderOperationExecutor,
)
from blender_worker.executor import WorkerExecutor  # noqa: E402
from blender_worker.journal import FileSystemExecutionStore  # noqa: E402
from blender_worker.link.client import (  # noqa: E402
    BackoffPolicy,
    WorkerLinkClient,
)
from blender_worker.link.identity import WorkerIdentity  # noqa: E402
from blender_worker.link.websocket_transport import (  # noqa: E402
    WebSocketWorkerTransport,
)
from blender_worker.locks import FileLockProvider  # noqa: E402
from blender_worker.registry import MappingProjectRegistry  # noqa: E402
from studio_api.settings import Settings  # noqa: E402
from studio_contracts import worker_protocol as protocol  # noqa: E402
from studio_preview.artifacts import LocalArtifactStore  # noqa: E402
from studio_preview.blender_preview import BlenderPreviewGenerator  # noqa: E402
from studio_preview.generator import looks_like_png  # noqa: E402

TOKEN = "e2e-api-token-not-committed"
WORKER_ID = "worker_e2e_api_1"
PROJECT_ID = "proj_seed"
DEV_USER_ID = "user_dev_local"

MOVE_COMMAND = "Move Cube 50 cm to the right."

#: Small preview so the E2E run stays quick.
PREVIEW_WIDTH = 320
PREVIEW_HEIGHT = 180

#: Blender is slow to start; every wait is bounded so a hang fails visibly.
BLENDER_DEADLINE_SECONDS = 300.0


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def blender_stack(tmp_path: Path):
    """A real control plane, a real worker, and a real isolated .blend."""
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")

    from studio_fixtures.seed_project import ensure_seed_project

    # An ISOLATED copy of the Task 5 fixture: the canonical file is never mutated,
    # so every run starts from Cube X = 0.
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_path = projects_root / "seed_project.blend"
    shutil.copy2(ensure_seed_project(), project_path)

    runtime = tmp_path / "runtime"
    artifact_root = tmp_path / "artifacts"

    # The worker renders and writes artifacts; the control plane reads them. For
    # Spec 001 both run on one machine and share this directory (see
    # services/api/README.md — replaced by object storage later).
    artifacts = LocalArtifactStore(artifact_root)

    executor = WorkerExecutor(
        store=FileSystemExecutionStore(runtime),
        locks=FileLockProvider(runtime),
        # The worker owns path resolution. The control plane never sees this path.
        projects=MappingProjectRegistry(projects_root, {PROJECT_ID: project_path}),
        blender=SubprocessBlenderOperationExecutor(),
        recovery_root=runtime / "recovery",
        previews=BlenderPreviewGenerator(),
        artifacts=artifacts,
        preview_width=PREVIEW_WIDTH,
        preview_height=PREVIEW_HEIGHT,
    )

    settings = Settings(
        environment="local",
        worker_token=TOKEN,
        project_ids=(PROJECT_ID,),
        development_user_id=DEV_USER_ID,
        heartbeat_interval_seconds=120.0,
        artifact_root=str(artifact_root),
    )

    with ControlPlaneServer.build(settings) as server:
        with server.client() as http:
            yield {
                "server": server,
                "http": http,
                "dependencies": server.dependencies,
                "executor": executor,
                "project_path": project_path,
                "artifacts": artifacts,
            }


def build_worker(executor: WorkerExecutor, url: str) -> WorkerLinkClient:
    return WorkerLinkClient(
        identity=WorkerIdentity(
            worker_id=WORKER_ID,
            control_plane_url=url,
            token=TOKEN,
            gpu_name="NVIDIA RTX 4070 Ti",
        ),
        transport=WebSocketWorkerTransport(url, receive_timeout=5.0),
        executor=executor,
        backoff=BackoffPolicy(initial_seconds=0.05, max_seconds=0.5),
        probe_blender=True,
    )


def chat_body(request_id: str, message: str = MOVE_COMMAND) -> dict:
    return {
        "request_id": request_id,
        "project_id": PROJECT_ID,
        "session_id": "sess_e2e",
        "message": message,
    }


def job_path(job_id: str, project_id: str = PROJECT_ID) -> str:
    """The project-scoped job status path. There is no unscoped variant."""
    return f"/api/projects/{project_id}/jobs/{job_id}"


def saved_cube_x(project_path: Path) -> float:
    """Read Cube's X from the SAVED .blend using a fresh Blender process.

    Reading the file back through an independent process is what makes this a
    durability assertion rather than a claim about worker memory.
    """
    from studio_fixtures.seed_project import inspect_blend

    digest = inspect_blend(project_path)["digest"]
    for entry in digest["objects"]:
        if entry["name"] == "Cube":
            return float(entry["world_position_meters"]["x"])
    raise AssertionError("no Cube in the saved project")


def handle_until(
    client: WorkerLinkClient,
    expected: str,
    deadline_seconds: float = BLENDER_DEADLINE_SECONDS,
    poll: float = 1.0,
) -> bool:
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if client.handle_next(timeout=poll) == expected:
            return True
    return False


def await_status(
    http: Any,
    job_id: str,
    expected: str,
    deadline_seconds: float = BLENDER_DEADLINE_SECONDS,
) -> dict:
    end = time.monotonic() + deadline_seconds
    last: Optional[dict] = None
    while time.monotonic() < end:
        response = http.get(job_path(job_id))
        if response.status_code == 200:
            last = response.json()
            if last["job_status"] == expected:
                return last
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached {expected!r}; last: {last}")


def await_worker_registered(http: Any, deadline_seconds: float = 30.0) -> None:
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if http.get("/api/workers").json()["registered_workers"] == 1:
            return
        time.sleep(0.05)
    raise AssertionError("the worker never registered with the control plane")


# ---------------------------------------------------------------------------
# The mandatory scenario
# ---------------------------------------------------------------------------


@pytest.mark.blender
def test_http_chat_moves_the_cube_in_real_blender_and_reports_success(blender_stack):
    """The full slice: HTTP request in, Blender mutation out, status reported.

    Proves, in order:
      - the fixture copy starts at Cube X = 0.0
      - POST /api/chat succeeds
      - the AgentProvider produced the canonical plan
      - JobFactory produced the canonical Job
      - the control plane offered it and the worker accepted
      - real Blender moved the cube 0.0 -> 0.50 and SAVED
      - the worker returned a structured result
      - control-plane job status became succeeded
      - GET /api/projects/{project_id}/jobs/{job_id} reports success
    """
    http = blender_stack["http"]
    project_path = blender_stack["project_path"]

    assert coordinates_equal(saved_cube_x(project_path), 0.0), "must start at origin"

    worker = build_worker(blender_stack["executor"], blender_stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=30.0) is True
        await_worker_registered(http)

        # The worker advertised a real Blender, and the API reports it honestly.
        health = http.get("/health").json()
        assert health["api"] == "healthy"
        assert health["ready_workers"] == 1
        assert health["blender_capable_workers"] == 1

        # ---- HTTP request succeeds ----------------------------------
        response = http.post("/api/chat", json=chat_body("req_e2e_001"))
        assert response.status_code == 202, response.text
        submission = response.json()
        job_id = submission["job_id"]

        # ---- AgentProvider created the canonical plan ---------------
        assert submission["provider"] == "rule_based"
        assert submission["request_id"] == "req_e2e_001"
        assert submission["worker_id"] == WORKER_ID
        assert submission["job_status"] == "queued"

        # ---- JobFactory created the canonical Job -------------------
        offered = blender_stack["dependencies"].store.get(PROJECT_ID, job_id).job
        assert offered["job_type"] == "move_object"
        assert offered["project_id"] == PROJECT_ID
        assert offered["user_id"] == DEV_USER_ID, "trusted identity, not client-supplied"
        assert offered["payload"]["target"]["name"] == "Cube"
        assert offered["payload"]["delta_meters"] == {"x": 0.5, "y": 0.0, "z": 0.0}
        assert offered["origin"] == {"request_id": "req_e2e_001", "operation_index": 0}

        # ---- the worker accepts and executes against real Blender ---
        assert handle_until(worker, protocol.JOB_OFFER), "the offer never arrived"

        # ---- worker returned a structured result --------------------
        status = await_status(http, job_id, "succeeded")
        assert status["result"]["applied"] is True
        assert status["result"]["verified"] is True
        assert coordinates_equal(status["result"]["final_position_meters"]["x"], 0.5)
        assert status["execution_phase"] == phases.COMPLETED
        assert status["error"] is None

        # ---- GET the project-scoped job route reports success -------
        assert status["chat"]["status"] == "success"
        assert coordinates_equal(status["chat"]["object_position"]["x"], 0.5)

        # The status URL is project-scoped, and no unscoped route exists.
        assert submission["status_url"] == f"/api/projects/{PROJECT_ID}/jobs/{job_id}"
        assert http.get(submission["status_url"]).status_code == 200
        assert http.get(f"/api/jobs/{job_id}").status_code == 404
        # A job cannot be reached through a project it does not belong to.
        assert http.get(f"/api/projects/proj_other/jobs/{job_id}").status_code == 404

        # ---- a real PNG preview was produced and is served -----------
        preview = status["preview"]
        assert preview is not None, f"no preview: {status.get('preview_error')}"
        assert preview["media_type"] == "image/png"
        assert preview["width"] == PREVIEW_WIDTH
        assert preview["height"] == PREVIEW_HEIGHT
        assert preview["url"] == (
            f"/api/projects/{PROJECT_ID}/artifacts/{preview['artifact_id']}"
        )

        image = http.get(preview["url"])
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert looks_like_png(image.content)
        assert len(image.content) == preview["size_bytes"]
        assert (
            "sha256:" + hashlib.sha256(image.content).hexdigest()
            == preview["checksum"]
        )

        # The canonical ChatResponse carries the same logical URL.
        assert status["chat"]["preview_url"] == preview["url"]

        # ---- Blender moved the cube and the .blend save persisted ---
        # Read by an independent Blender process from the saved file.
        assert coordinates_equal(saved_cube_x(project_path), 0.5)
    finally:
        worker.disconnect()


@pytest.mark.blender
def test_a_second_intentional_request_moves_the_cube_again_but_a_retry_does_not(
    blender_stack,
):
    """0.00 -> 0.50 -> 1.00, while retrying the FIRST request_id changes nothing.

    This is the property the whole idempotency design exists for:

      - request_id ``req_e2e_001`` moves the cube once, to 0.50
      - retrying ``req_e2e_001`` verbatim leaves it at 0.50
      - a NEW request_id with identical text moves it to 1.00
      - retrying ``req_e2e_001`` again STILL leaves it at 1.00 (no third mutation,
        and no attempt to "re-apply" the original 0.50 step)
    """
    http = blender_stack["http"]
    project_path = blender_stack["project_path"]
    assert coordinates_equal(saved_cube_x(project_path), 0.0)

    worker = build_worker(blender_stack["executor"], blender_stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=30.0)
        await_worker_registered(http)

        # ---- first intentional command: 0.00 -> 0.50 ----------------
        first = http.post("/api/chat", json=chat_body("req_e2e_001"))
        assert first.status_code == 202, first.text
        first_job_id = first.json()["job_id"]
        assert handle_until(worker, protocol.JOB_OFFER)
        await_status(http, first_job_id, "succeeded")
        assert coordinates_equal(saved_cube_x(project_path), 0.5)

        # ---- retry of the ORIGINAL request: still 0.50 --------------
        retry = http.post("/api/chat", json=chat_body("req_e2e_001"))
        assert retry.status_code == 202, retry.text
        assert retry.json()["job_id"] == first_job_id, "same mutation identity"
        assert retry.json()["duplicate"] is True
        assert coordinates_equal(
            saved_cube_x(project_path), 0.5
        ), "a retry must not move the cube a second time"

        # ---- second intentional command: 0.50 -> 1.00 ---------------
        second = http.post("/api/chat", json=chat_body("req_e2e_002"))
        assert second.status_code == 202, second.text
        second_job_id = second.json()["job_id"]
        assert second_job_id != first_job_id
        assert handle_until(worker, protocol.JOB_OFFER)
        await_status(http, second_job_id, "succeeded")
        assert coordinates_equal(
            saved_cube_x(project_path), 1.0
        ), "a genuinely new request must move the cube again"

        # ---- retry of the original AFTER the second command ---------
        # The dangerous case: a stale retry must not reapply an old step.
        late_retry = http.post("/api/chat", json=chat_body("req_e2e_001"))
        assert late_retry.status_code == 202
        assert late_retry.json()["job_id"] == first_job_id
        assert coordinates_equal(
            saved_cube_x(project_path), 1.0
        ), "a stale retry must leave the current state untouched"

        # Exactly two mutations were ever recorded.
        records = blender_stack["dependencies"].store.all_records()
        assert len({record.idempotency_key for record in records}) == 2
    finally:
        worker.disconnect()


@pytest.mark.blender
def test_preview_artifacts_are_versioned_per_request_and_reused_on_retry(
    blender_stack,
):
    """The full Task 10 preview story over real HTTP and real Blender.

    Proves, in order:
      - request 1 (Cube 0.00 -> 0.50) yields preview artifact A, served as PNG
      - a new request_id (0.50 -> 1.00) yields a DIFFERENT artifact B
      - artifact A is still retrievable, so nothing was overwritten
      - the two images differ, because the scene visibly differs
      - retrying request 1 creates no new mutation and no new artifact
    """
    http = blender_stack["http"]
    project_path = blender_stack["project_path"]
    assert coordinates_equal(saved_cube_x(project_path), 0.0)

    worker = build_worker(blender_stack["executor"], blender_stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=30.0)
        await_worker_registered(http)

        # ---- request 1: 0.00 -> 0.50, preview A ---------------------
        first = http.post("/api/chat", json=chat_body("req_e2e_p1"))
        first_job = first.json()["job_id"]
        assert handle_until(worker, protocol.JOB_OFFER)
        first_status = await_status(http, first_job, "succeeded")

        preview_a = first_status["preview"]
        assert preview_a is not None, first_status.get("preview_error")
        image_a = http.get(preview_a["url"])
        assert image_a.status_code == 200
        assert image_a.headers["content-type"] == "image/png"
        assert looks_like_png(image_a.content)
        assert coordinates_equal(saved_cube_x(project_path), 0.5)

        # ---- request 2: 0.50 -> 1.00, preview B ---------------------
        second = http.post("/api/chat", json=chat_body("req_e2e_p2"))
        second_job = second.json()["job_id"]
        assert second_job != first_job
        assert handle_until(worker, protocol.JOB_OFFER)
        second_status = await_status(http, second_job, "succeeded")

        preview_b = second_status["preview"]
        assert preview_b is not None, second_status.get("preview_error")
        assert preview_b["artifact_id"] != preview_a["artifact_id"], (
            "a new intentional change must produce a new artifact"
        )
        assert preview_b["url"] != preview_a["url"]

        image_b = http.get(preview_b["url"])
        assert image_b.status_code == 200
        assert looks_like_png(image_b.content)
        assert coordinates_equal(saved_cube_x(project_path), 1.0)

        # ---- the OLD preview is still retrievable -------------------
        still_there = http.get(preview_a["url"])
        assert still_there.status_code == 200, "artifact A was overwritten or removed"
        assert still_there.content == image_a.content

        # ---- the two images genuinely differ ------------------------
        assert image_a.content != image_b.content
        assert preview_a["checksum"] != preview_b["checksum"], (
            "a visibly different scene must produce a different image"
        )

        # ---- retry request 1: no mutation, no new artifact ----------
        artifacts_before = len(
            blender_stack["artifacts"].list_for_project(PROJECT_ID)
        )
        retry = http.post("/api/chat", json=chat_body("req_e2e_p1"))
        assert retry.status_code == 202
        assert retry.json()["job_id"] == first_job
        assert retry.json()["duplicate"] is True

        assert coordinates_equal(
            saved_cube_x(project_path), 1.0
        ), "a retry must not move the cube"

        retried_status = http.get(job_path(first_job)).json()
        assert retried_status["preview"]["artifact_id"] == preview_a["artifact_id"], (
            "a retry must reuse the existing preview"
        )
        assert (
            len(blender_stack["artifacts"].list_for_project(PROJECT_ID))
            == artifacts_before
            == 2
        ), "retrying must not accumulate preview artifacts"
    finally:
        worker.disconnect()


@pytest.mark.blender
def test_a_served_preview_leaks_no_filesystem_path(blender_stack):
    """The image BYTES must be as path-free as the JSON responses.

    Blender embeds the .blend path in PNG metadata unless stamping is disabled;
    this asserts the served artifact really is clean end to end.
    """
    http = blender_stack["http"]
    worker = build_worker(blender_stack["executor"], blender_stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=30.0)
        await_worker_registered(http)

        submission = http.post("/api/chat", json=chat_body("req_e2e_clean")).json()
        assert handle_until(worker, protocol.JOB_OFFER)
        status = await_status(http, submission["job_id"], "succeeded")

        image = http.get(status["preview"]["url"])
        assert image.status_code == 200

        body = image.content.decode("latin-1")
        project_path = str(blender_stack["project_path"])
        for leaked in (
            project_path,
            "seed_project",
            ".blend",
            "/home",
            "/tmp",
            TOKEN,
        ):
            assert leaked not in body, f"the served PNG leaked {leaked!r}"

        # And no response header discloses a path either.
        assert project_path not in str(dict(image.headers))
    finally:
        worker.disconnect()


@pytest.mark.blender
def test_another_project_cannot_fetch_a_preview_artifact(blender_stack):
    """Artifact retrieval is project-scoped, exactly like job retrieval."""
    http = blender_stack["http"]
    worker = build_worker(blender_stack["executor"], blender_stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=30.0)
        await_worker_registered(http)

        submission = http.post("/api/chat", json=chat_body("req_e2e_iso")).json()
        assert handle_until(worker, protocol.JOB_OFFER)
        status = await_status(http, submission["job_id"], "succeeded")
        artifact_id = status["preview"]["artifact_id"]

        assert http.get(status["preview"]["url"]).status_code == 200

        # Another project, an unknown project, and no scope at all.
        assert (
            http.get(f"/api/projects/proj_other/artifacts/{artifact_id}").status_code
            == 404
        )
        assert http.get(f"/api/artifacts/{artifact_id}").status_code == 404
        assert (
            http.get(f"/api/artifacts/{artifact_id}?project_id={PROJECT_ID}").status_code
            == 404
        )
    finally:
        worker.disconnect()


@pytest.mark.blender
def test_no_worker_connected_yields_503_and_never_touches_blender(blender_stack):
    """A failure case: the design machine is offline."""
    http = blender_stack["http"]
    project_path = blender_stack["project_path"]

    response = http.post("/api/chat", json=chat_body("req_e2e_offline"))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BLENDER_UNAVAILABLE"
    assert http.get("/health").json()["blender_capable_workers"] == 0
    assert coordinates_equal(saved_cube_x(project_path), 0.0), "nothing was modified"


@pytest.mark.blender
def test_an_invalid_object_fails_structurally_without_corrupting_the_project(
    blender_stack,
):
    """A failure case: the named object does not exist."""
    http = blender_stack["http"]
    project_path = blender_stack["project_path"]

    worker = build_worker(blender_stack["executor"], blender_stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=30.0)
        await_worker_registered(http)

        response = http.post(
            "/api/chat",
            json=chat_body(
                "req_e2e_missing", message="Move Sofa 50 cm to the right."
            ),
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        assert handle_until(worker, protocol.JOB_OFFER)
        status = await_status(http, job_id, "failed")

        assert status["error"]["code"] == "OBJECT_NOT_FOUND"
        assert status["chat"]["status"] == "error"
        # The project is untouched and still readable.
        assert coordinates_equal(saved_cube_x(project_path), 0.0)
    finally:
        worker.disconnect()


@pytest.mark.blender
def test_the_api_never_learns_the_project_path(blender_stack):
    """The control plane authorizes a logical project; the worker owns the path."""
    import json

    dependencies = blender_stack["dependencies"]
    project_path = str(blender_stack["project_path"])

    project = dependencies.projects.get(PROJECT_ID)
    assert json.dumps(project.snapshot()).find(".blend") == -1

    http = blender_stack["http"]
    worker = build_worker(blender_stack["executor"], blender_stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=30.0)
        await_worker_registered(http)
        submission = http.post("/api/chat", json=chat_body("req_e2e_paths")).json()

        offered = json.dumps(
            dependencies.store.get(PROJECT_ID, submission["job_id"]).job
        )
        assert project_path not in offered, "the job must not carry a path"
        assert ".blend" not in offered

        for path in ("/health", "/api/workers", f"/api/jobs/{submission['job_id']}"):
            body = http.get(path).text
            assert project_path not in body, f"{path} leaked the project path"
    finally:
        worker.disconnect()
