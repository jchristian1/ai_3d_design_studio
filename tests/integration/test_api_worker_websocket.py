"""API ↔ worker WebSocket integration tests (Spec 001, Task 9).

Proves the real Task 9 binding end to end on one machine, with real HTTP and real
WebSockets but a fake Blender:

    httpx  --POST /api/chat-->  uvicorn + FastAPI  --push-->  /ws/workers
                                                                  ^
                                            WorkerLinkClient outbound connection
                                                                  v
                                                 WorkerExecutor -> fake Blender

Everything except Blender itself is the production code path: the real ASGI server,
the real WebSocket route, the real protocol, the real ``WorkerConnectionManager``,
the real ``WorkerLinkClient``, and the real ``WorkerExecutor`` with its durable
journal and project lock. The Blender-backed tier lives in tests/e2e.

Skips cleanly when the FastAPI/uvicorn dependencies are not installed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("websockets", reason="websockets not installed")

from studio_fixtures.control_plane import ControlPlaneServer  # noqa: E402

from blender_worker.blender_ops import FakeBlenderOperationExecutor  # noqa: E402
from blender_worker.executor import WorkerExecutor  # noqa: E402
from blender_worker.journal import FileSystemExecutionStore  # noqa: E402
from blender_worker.link.client import (  # noqa: E402
    READY,
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
from studio_types import Vec3  # noqa: E402

TOKEN = "integration-api-token-not-committed"
WORKER_ID = "worker_api_integration_1"
PROJECT_ID = "proj_seed"
DEV_USER_ID = "user_dev_local"

MOVE_COMMAND = "Move Cube 50 cm to the right."


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def build_settings() -> Settings:
    return Settings(
        environment="local",
        worker_token=TOKEN,
        project_ids=(PROJECT_ID,),
        development_user_id=DEV_USER_ID,
        heartbeat_interval_seconds=60.0,
    )


def build_executor(tmp_path: Path, blender: Any, project_path: Path) -> WorkerExecutor:
    runtime = tmp_path / "runtime"
    return WorkerExecutor(
        store=FileSystemExecutionStore(runtime),
        locks=FileLockProvider(runtime),
        projects=MappingProjectRegistry(
            project_path.parent, {PROJECT_ID: project_path}
        ),
        blender=blender,
        recovery_root=runtime / "recovery",
    )


def build_worker(executor: WorkerExecutor, url: str, token: str = TOKEN):
    return WorkerLinkClient(
        identity=WorkerIdentity(
            worker_id=WORKER_ID, control_plane_url=url, token=token
        ),
        transport=WebSocketWorkerTransport(url, receive_timeout=5.0),
        executor=executor,
        backoff=BackoffPolicy(initial_seconds=0.01, max_seconds=0.05),
        probe_blender=False,
    )


@pytest.fixture
def stack(tmp_path: Path):
    """A running control plane plus a worker ready to connect to it."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_path = projects_root / f"{PROJECT_ID}.blend"
    project_path.write_bytes(b"fake blend")

    blender = FakeBlenderOperationExecutor()
    blender.seed_project(project_path, {"Cube": Vec3(0.0, 0.0, 0.0)})
    executor = build_executor(tmp_path, blender, project_path)

    with ControlPlaneServer.build(build_settings()) as server:
        with server.client() as http:
            yield {
                "server": server,
                "http": http,
                "dependencies": server.dependencies,
                "executor": executor,
                "blender": blender,
                "project_path": project_path,
            }


def chat_body(
    request_id: str,
    message: str = MOVE_COMMAND,
    project_id: str = PROJECT_ID,
    session_id: str = "sess_integration",
) -> dict:
    return {
        "request_id": request_id,
        "project_id": project_id,
        "session_id": session_id,
        "message": message,
    }


def cube_x(stack) -> float:
    return stack["blender"].position_of(stack["project_path"], "Cube").x


def job_path(job_id: str, project_id: str = PROJECT_ID) -> str:
    """The project-scoped job status path. There is no unscoped variant."""
    return f"/api/projects/{project_id}/jobs/{job_id}"


def move_count(stack) -> int:
    return sum(1 for kind, _ in stack["blender"].calls if kind == "move")


def handle_until(
    client: WorkerLinkClient,
    expected: str,
    deadline_seconds: float = 30.0,
    poll: float = 0.5,
) -> bool:
    """Process inbound messages until `expected` is seen, or the deadline passes.

    Bounded by wall-clock time so a deadlock fails fast and visibly instead of
    hanging the suite.
    """
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if client.handle_next(timeout=poll) == expected:
            return True
    return False


def receive_within(transport, deadline_seconds: float = 20.0, poll: float = 0.5):
    """Receive one raw message within a bounded deadline, or None."""
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        message = transport.receive(timeout=poll)
        if message is not None:
            return message
    return None


def await_status(http, job_id: str, expected: str, deadline_seconds: float = 30.0):
    """Poll the job status endpoint until it reaches a status."""
    end = time.monotonic() + deadline_seconds
    last: Optional[dict] = None
    while time.monotonic() < end:
        response = http.get(job_path(job_id))
        if response.status_code == 200:
            last = response.json()
            if last["job_status"] == expected:
                return last
        time.sleep(0.05)
    raise AssertionError(
        f"job {job_id} never reached {expected!r}; last seen: {last}"
    )


def await_worker_count(http, expected: int, deadline_seconds: float = 10.0) -> int:
    end = time.monotonic() + deadline_seconds
    count = -1
    while time.monotonic() < end:
        count = http.get("/api/workers").json()["registered_workers"]
        if count == expected:
            return count
        time.sleep(0.05)
    return count


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_health_is_served_over_real_http(stack):
    body = stack["http"].get("/health").json()
    assert body["api"] == "healthy"
    assert body["registered_workers"] == 0


def test_worker_connects_outbound_to_the_fastapi_endpoint(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0) is True
        assert worker.state == READY
        assert worker.connection_id is not None

        assert await_worker_count(stack["http"], 1) == 1
        body = stack["http"].get("/api/workers").json()
        assert body["workers"][0]["worker_id"] == WORKER_ID
        assert body["workers"][0]["connected"] is True
        assert body["ready_workers"] == 1
    finally:
        worker.disconnect()


def test_the_worker_endpoint_is_loopback_and_correctly_pathed(stack):
    url = stack["server"].worker_url
    assert url.startswith("ws://127.0.0.1:")
    assert url.endswith("/ws/workers")


def test_invalid_worker_token_is_rejected_over_real_websockets(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url, token="wrong")
    try:
        assert worker.connect_and_register(timeout=10.0) is False
        assert worker.last_error["code"] == "VALIDATION_ERROR"
        assert stack["http"].get("/api/workers").json()["registered_workers"] == 0
    finally:
        worker.disconnect()


def test_health_counts_a_registered_worker_over_real_websockets(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)
        body = stack["http"].get("/health").json()
        assert body["registered_workers"] == 1
        assert body["ready_workers"] == 1
    finally:
        worker.disconnect()


def test_a_disconnected_worker_is_deregistered(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    assert worker.connect_and_register(timeout=10.0)
    await_worker_count(stack["http"], 1)

    worker.disconnect()

    assert await_worker_count(stack["http"], 0) == 0, (
        "a closed connection must remove the worker so it cannot be offered work"
    )


# ---------------------------------------------------------------------------
# The full HTTP -> agent -> worker path
# ---------------------------------------------------------------------------


def test_chat_request_reaches_the_worker_and_completes(stack):
    """POST /api/chat -> plan -> job -> offer -> accept -> execute -> result."""
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        response = stack["http"].post("/api/chat", json=chat_body("req_int_001"))
        assert response.status_code == 202, response.text
        submission = response.json()
        job_id = submission["job_id"]
        assert submission["job_status"] == "queued"
        assert submission["worker_id"] == WORKER_ID
        assert submission["provider"] == "rule_based"

        # The worker processes the pushed offer and executes it.
        assert handle_until(worker, protocol.JOB_OFFER), "the offer never arrived"

        status = await_status(stack["http"], job_id, "succeeded")
        assert status["result"]["verified"] is True
        assert status["result"]["final_position_meters"]["x"] == 0.5
        assert status["chat"]["status"] == "success"
        assert status["chat"]["object_position"] == {"x": 0.5, "y": 0.0, "z": 0.0}

        assert cube_x(stack) == 0.5
        assert move_count(stack) == 1
    finally:
        worker.disconnect()


def test_an_idle_worker_is_pushed_the_offer_without_speaking_first(stack):
    """REGRESSION: dispatch must not depend on inbound worker traffic.

    Task 8 found a deadlock where offers were only flushed in reaction to an
    inbound message, so an idle worker and the server waited on each other. The
    FastAPI binding must preserve the fix: its sender task is independent of its
    receive loop.
    """
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        transcript = stack["dependencies"].gateway.transcript
        assert len(transcript) == 1, "only the hello has been received"
        assert transcript[0]["type"] == protocol.WORKER_HELLO

        # Deliberately NO heartbeat, NO ping, no worker traffic whatsoever.
        response = stack["http"].post("/api/chat", json=chat_body("req_int_push"))
        assert response.status_code == 202

        raw = receive_within(worker.transport)
        assert raw is not None, "an idle worker was never offered the job"
        offer = protocol.parse(raw)
        assert offer["type"] == protocol.JOB_OFFER
        assert offer["job_id"] == response.json()["job_id"]

        assert len(transcript) == 1, (
            "the worker had to speak first, which is the deadlock this guards"
        )
    finally:
        worker.disconnect()


def test_no_connected_worker_yields_503_over_real_http(stack):
    response = stack["http"].post("/api/chat", json=chat_body("req_int_no_worker"))

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "BLENDER_UNAVAILABLE"
    assert move_count(stack) == 0


def test_unknown_project_yields_404_over_real_http(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        response = stack["http"].post(
            "/api/chat", json=chat_body("req_int_bad_proj", project_id="proj_nope")
        )

        assert response.status_code == 404
        assert move_count(stack) == 0
    finally:
        worker.disconnect()


def test_unsupported_instruction_yields_422_and_never_reaches_blender(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        response = stack["http"].post(
            "/api/chat",
            json=chat_body("req_int_vague", message="Make the room feel cosier."),
        )

        assert response.status_code == 422
        assert move_count(stack) == 0
    finally:
        worker.disconnect()


# ---------------------------------------------------------------------------
# Idempotency across the real link
# ---------------------------------------------------------------------------


def test_retrying_the_same_request_id_does_not_move_the_cube_twice(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        first = stack["http"].post("/api/chat", json=chat_body("req_int_retry"))
        job_id = first.json()["job_id"]
        assert handle_until(worker, protocol.JOB_OFFER)
        await_status(stack["http"], job_id, "succeeded")
        assert cube_x(stack) == 0.5
        moves_after_first = move_count(stack)

        # The exact same submission, retried.
        retry = stack["http"].post("/api/chat", json=chat_body("req_int_retry"))

        assert retry.status_code == 202
        assert retry.json()["job_id"] == job_id, "same mutation identity"
        assert retry.json()["duplicate"] is True
        assert move_count(stack) == moves_after_first, "no second Blender mutation"
        assert cube_x(stack) == 0.5, "must remain 0.50, not 1.00"
    finally:
        worker.disconnect()


def test_a_new_request_id_moves_the_cube_again(stack):
    """Idempotency must not block a genuinely new intentional command."""
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        first = stack["http"].post("/api/chat", json=chat_body("req_int_move_a"))
        assert handle_until(worker, protocol.JOB_OFFER)
        await_status(stack["http"], first.json()["job_id"], "succeeded")
        assert cube_x(stack) == 0.5

        second = stack["http"].post("/api/chat", json=chat_body("req_int_move_b"))
        assert second.status_code == 202, second.text
        assert second.json()["job_id"] != first.json()["job_id"]
        assert handle_until(worker, protocol.JOB_OFFER)
        await_status(stack["http"], second.json()["job_id"], "succeeded")

        assert cube_x(stack) == 1.0
        assert move_count(stack) == 2
    finally:
        worker.disconnect()


def test_a_result_delivered_after_reconnect_reconciles_the_existing_job(stack):
    """A dropped result channel reports late; it never re-executes."""
    from blender_worker.link.transport import TransportError

    worker = build_worker(stack["executor"], stack["server"].worker_url)
    job_id = None
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        submission = stack["http"].post("/api/chat", json=chat_body("req_int_recon"))
        job_id = submission.json()["job_id"]

        # Break only the result channel: the mutation completes and saves.
        original_send = worker.transport.send

        def failing_send(message):
            if message["type"] == protocol.JOB_RESULT:
                raise TransportError("result channel interrupted")
            return original_send(message)

        worker.transport.send = failing_send  # type: ignore[assignment]
        assert handle_until(worker, protocol.JOB_OFFER)

        assert cube_x(stack) == 0.5, "the mutation is durable"
        assert worker.stats.results_undelivered == 1
        record = stack["executor"].store.load(PROJECT_ID, job_id)
        assert record.result_delivered is False
    finally:
        worker.disconnect()

    moves_before = move_count(stack)

    # Reconnect with a fresh transport and reconcile from the durable journal.
    reconnected = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert reconnected.connect_and_register(timeout=10.0) is True
        assert reconnected.reconcile() == [job_id]

        status = await_status(stack["http"], job_id, "succeeded")
        assert status["reconciled"] is True, "reported as a reconciliation"
        assert move_count(stack) == moves_before, "reconciliation touched nothing"
        assert cube_x(stack) == 0.5
    finally:
        reconnected.disconnect()


# ---------------------------------------------------------------------------
# Security posture of the exposed endpoint
# ---------------------------------------------------------------------------


def test_the_token_never_appears_in_any_http_response(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        for path in ("/health", "/api/workers", "/openapi.json"):
            assert TOKEN not in stack["http"].get(path).text, f"{path} leaked the token"
    finally:
        worker.disconnect()


def test_the_token_is_redacted_in_the_gateway_transcript(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        transcript = json.dumps(stack["dependencies"].gateway.transcript)
        assert TOKEN not in transcript
        assert protocol.REDACTED in transcript
    finally:
        worker.disconnect()


def test_garbage_on_the_worker_socket_is_rejected_without_registering(stack):
    """The endpoint accepts only the constrained protocol."""
    from websockets.sync.client import connect

    with connect(stack["server"].worker_url, open_timeout=10) as connection:
        connection.send('{"type": "execute_python", "code": "import os"}')
        reply = json.loads(connection.recv(timeout=10))

    assert reply["type"] == protocol.WORKER_REJECTED
    assert stack["http"].get("/api/workers").json()["registered_workers"] == 0


def test_the_worker_endpoint_cannot_be_used_to_run_code_or_read_files(stack):
    from websockets.sync.client import connect

    hostile_messages = [
        '{"protocol_version": 1, "type": "ping", "command": "rm -rf /"}',
        '{"protocol_version": 1, "type": "ping", "path": "/etc/passwd"}',
        '{"protocol_version": 1, "type": "worker_hello", "worker_id": "w", '
        '"token": "x", "capabilities": {}, "script": "print(1)"}',
        "not json at all",
    ]

    for hostile in hostile_messages:
        with connect(stack["server"].worker_url, open_timeout=10) as connection:
            connection.send(hostile)
            reply = json.loads(connection.recv(timeout=10))
            assert reply["type"] == protocol.WORKER_REJECTED, hostile

    assert stack["http"].get("/api/workers").json()["registered_workers"] == 0
    assert move_count(stack) == 0


def test_openapi_documents_no_path_or_identity_input(stack):
    """A client cannot even describe a filesystem path or a user to the API."""
    spec = stack["http"].get("/openapi.json").json()
    chat_schema = spec["components"]["schemas"]["ChatRequestModel"]

    assert set(chat_schema["properties"]) == {
        "request_id",
        "project_id",
        "session_id",
        "message",
        "selected_object_id",
    }
    assert chat_schema.get("additionalProperties") is False


# ---------------------------------------------------------------------------
# Project isolation over real HTTP
# ---------------------------------------------------------------------------


def test_job_retrieval_is_project_scoped_over_real_http(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        submission = stack["http"].post(
            "/api/chat", json=chat_body("req_int_scoped")
        ).json()
        job_id = submission["job_id"]

        # The status URL the API handed out is project-scoped and resolves.
        assert submission["status_url"] == f"/api/projects/{PROJECT_ID}/jobs/{job_id}"
        assert stack["http"].get(submission["status_url"]).status_code == 200

        # No unscoped route exists on the running server.
        assert stack["http"].get(f"/api/jobs/{job_id}").status_code == 404
        assert (
            stack["http"]
            .get(f"/api/jobs/{job_id}?project_id={PROJECT_ID}")
            .status_code
            == 404
        )
    finally:
        worker.disconnect()


def test_an_unknown_project_cannot_retrieve_a_real_job_over_real_http(stack):
    worker = build_worker(stack["executor"], stack["server"].worker_url)
    try:
        assert worker.connect_and_register(timeout=10.0)
        await_worker_count(stack["http"], 1)

        job_id = stack["http"].post(
            "/api/chat", json=chat_body("req_int_wrong_proj")
        ).json()["job_id"]

        wrong = stack["http"].get(f"/api/projects/proj_not_registered/jobs/{job_id}")
        assert wrong.status_code == 404
        assert job_id not in wrong.text
        assert "Traceback" not in wrong.text

        # And the job is still readable under its own project.
        assert stack["http"].get(job_path(job_id)).status_code == 200
    finally:
        worker.disconnect()


def test_the_openapi_schema_exposes_only_the_scoped_job_route(stack):
    paths = stack["http"].get("/openapi.json").json()["paths"]

    assert "/api/projects/{project_id}/jobs/{job_id}" in paths
    assert "/api/jobs/{job_id}" not in paths, "no unscoped job route may exist"
