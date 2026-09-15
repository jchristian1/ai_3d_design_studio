"""Local WebSocket transport integration tests (Spec 001, Task 8).

Proves the real outbound-connection architecture on one machine:

    worker  --outbound-->  ws://127.0.0.1:<port>/ws/workers  -->  control plane

Two tiers:
  - fast: real WebSockets, fake Blender executor (runs in the default suite)
  - `blender` marker: real WebSockets AND real Blender against a Task 5 fixture copy

Skips cleanly when `websockets` is unavailable, so the suite still runs in an
environment where project dependencies have not been installed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets", reason="websockets not installed")

from blender_mcp.blender_runtime import find_blender_executable  # noqa: E402
from blender_mcp.tolerance import coordinates_equal  # noqa: E402
from blender_worker import phases  # noqa: E402
from blender_worker.blender_ops import (  # noqa: E402
    FakeBlenderOperationExecutor,
    SubprocessBlenderOperationExecutor,
)
from blender_worker.executor import WorkerExecutor  # noqa: E402
from blender_worker.journal import FileSystemExecutionStore  # noqa: E402
from blender_worker.link.client import (  # noqa: E402
    READY,
    BackoffPolicy,
    WorkerLinkClient,
)
from blender_worker.link.identity import WorkerIdentity  # noqa: E402
from blender_worker.link.transport import TransportError  # noqa: E402
from blender_worker.link.websocket_transport import (  # noqa: E402
    WebSocketWorkerTransport,
)
from blender_worker.locks import FileLockProvider  # noqa: E402
from blender_worker.registry import MappingProjectRegistry  # noqa: E402
from studio_contracts import to_wire  # noqa: E402
from studio_contracts import worker_protocol as protocol  # noqa: E402
from studio_contracts.jobs import create_move_object_job  # noqa: E402
from studio_api.worker_link.local_server import LocalControlPlaneServer  # noqa: E402
from studio_api.worker_link.manager import WorkerConnectionManager  # noqa: E402
from studio_types import ObjectRef, Vec3  # noqa: E402

WORKER_ID = "worker_integration_1"
TOKEN = "integration-token-not-committed"
PROJECT_ID = "proj_seed"


def make_job(job_id: str, request_id: str, delta_x: float = 0.5) -> dict:
    result = create_move_object_job(
        job_id=job_id,
        project_id=PROJECT_ID,
        session_id="sess_1",
        user_id="user_1",
        request_id=request_id,
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(delta_x, 0.0, 0.0),
        created_at="2026-09-15T04:00:00Z",
    )
    assert result.ok, result.errors
    return to_wire(result.job)


def build_executor(tmp_path: Path, blender, project_path: Path) -> WorkerExecutor:
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


def build_client(executor: WorkerExecutor, url: str) -> WorkerLinkClient:
    return WorkerLinkClient(
        identity=WorkerIdentity(
            worker_id=WORKER_ID, control_plane_url=url, token=TOKEN
        ),
        transport=WebSocketWorkerTransport(url, receive_timeout=5.0),
        executor=executor,
        backoff=BackoffPolicy(initial_seconds=0.01, max_seconds=0.05),
        probe_blender=False,
    )


# ---------------------------------------------------------------------------
# Fast tier: real WebSockets, fake Blender
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_stack(tmp_path: Path):
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_path = projects_root / f"{PROJECT_ID}.blend"
    project_path.write_bytes(b"fake blend")

    blender = FakeBlenderOperationExecutor()
    blender.seed_project(project_path, {"Cube": Vec3(0.0, 0.0, 0.0)})
    executor = build_executor(tmp_path, blender, project_path)

    manager = WorkerConnectionManager(
        expected_token=TOKEN, heartbeat_interval_seconds=30.0
    )
    with LocalControlPlaneServer(manager=manager) as server:
        yield {
            "server": server,
            "manager": manager,
            "blender": blender,
            "executor": executor,
            "project_path": project_path,
            "url": server.url,
        }


def test_worker_connects_outbound_over_real_websockets(fake_stack):
    client = build_client(fake_stack["executor"], fake_stack["url"])
    try:
        assert client.connect_and_register(timeout=5.0) is True
        assert client.state == READY
        assert client.connection_id is not None
        assert WORKER_ID in fake_stack["manager"].workers
    finally:
        client.disconnect()


def test_server_url_is_loopback_only(fake_stack):
    assert fake_stack["url"].startswith("ws://127.0.0.1:")
    assert "/ws/workers" in fake_stack["url"]


def test_invalid_token_is_rejected_over_real_websockets(fake_stack):
    client = build_client(fake_stack["executor"], fake_stack["url"])
    client.identity = WorkerIdentity(
        worker_id=WORKER_ID, control_plane_url=fake_stack["url"], token="wrong"
    )
    try:
        assert client.connect_and_register(timeout=5.0) is False
        assert client.last_error["code"] == "VALIDATION_ERROR"
        assert WORKER_ID not in fake_stack["manager"].workers
    finally:
        client.disconnect()


def test_full_offer_accept_execute_result_cycle(fake_stack):
    client = build_client(fake_stack["executor"], fake_stack["url"])
    try:
        assert client.connect_and_register(timeout=5.0)
        fake_stack["server"].queue_offer(WORKER_ID, make_job("job_ws_1", "req_ws_1"))
        client.send_heartbeat()

        assert handle_until(client, protocol.JOB_OFFER, deadline_seconds=20.0)

        results = _await(fake_stack["manager"].results)
        assert results[0]["job_status"] == "succeeded"
        assert fake_stack["manager"].accepted_jobs[0]["job_id"] == "job_ws_1"
        assert (
            fake_stack["blender"].position_of(fake_stack["project_path"], "Cube").x
            == 0.5
        )
    finally:
        client.disconnect()


def test_token_never_appears_in_the_server_transcript(fake_stack):
    client = build_client(fake_stack["executor"], fake_stack["url"])
    try:
        assert client.connect_and_register(timeout=5.0)
        transcript = json.dumps(fake_stack["server"].transcript)
        assert TOKEN not in transcript
        assert protocol.REDACTED in transcript
    finally:
        client.disconnect()


def test_idle_worker_receives_a_pushed_offer_without_sending_anything_first(
    fake_stack,
):
    """REGRESSION: the control plane must push to an idle, silent worker.

    The original implementation only flushed queued offers in reaction to an
    inbound worker message, so an idle worker and the server deadlocked: the
    server waited to read while the worker waited to be offered a job. The first
    version of `test_full_offer_accept_execute_result_cycle` masked the defect
    because it happened to call `send_heartbeat()` before waiting, which gave the
    server an inbound message to react to.

    The invariant this pins: dispatch of a queued job to a connected ready worker
    MUST NOT depend on an inbound heartbeat or any other worker message.
    """
    client = build_client(fake_stack["executor"], fake_stack["url"])
    try:
        assert client.connect_and_register(timeout=5.0) is True

        # Exactly one inbound message so far: the hello. Nothing else.
        inbound_after_registration = _await_count(fake_stack["server"].transcript, 1)
        assert inbound_after_registration == 1
        assert fake_stack["server"].transcript[0]["type"] == protocol.WORKER_HELLO

        # Deliberately NO heartbeat, NO ping, NO other traffic from the worker.
        fake_stack["server"].queue_offer(WORKER_ID, make_job("job_push_1", "req_push_1"))

        # The offer must arrive purely because the control plane pushed it.
        raw = _receive_within(client.transport, deadline_seconds=20.0)
        assert raw is not None, "an idle worker was never offered the queued job"
        offer = protocol.parse(raw)
        assert offer["type"] == protocol.JOB_OFFER
        assert offer["job_id"] == "job_push_1"

        # Prove the worker still sent nothing beyond the original hello.
        assert len(fake_stack["server"].transcript) == 1, (
            "the worker had to speak first, which is the deadlock this guards"
        )
    finally:
        client.disconnect()


def test_offer_queued_before_the_worker_connects_is_delivered_on_registration(
    fake_stack,
):
    """The other ordering: queued while offline, flushed once registered."""
    fake_stack["server"].queue_offer(WORKER_ID, make_job("job_pre_1", "req_pre_1"))

    client = build_client(fake_stack["executor"], fake_stack["url"])
    try:
        assert client.connect_and_register(timeout=5.0) is True
        raw = _receive_within(client.transport, deadline_seconds=20.0)
        assert raw is not None
        assert protocol.parse(raw)["job_id"] == "job_pre_1"
    finally:
        client.disconnect()


def _receive_within(transport, deadline_seconds: float, poll: float = 0.5):
    """Receive one message within a bounded wall-clock deadline, or None."""
    import time

    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        message = transport.receive(timeout=poll)
        if message is not None:
            return message
    return None


def _await_count(collection, expected: int, deadline_seconds: float = 10.0) -> int:
    """Wait until a collection reaches a size, bounded by wall-clock time."""
    import time

    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if len(collection) >= expected:
            break
        time.sleep(0.05)
    return len(collection)


def test_secure_url_can_be_required():
    with pytest.raises(TransportError):
        WebSocketWorkerTransport("ws://127.0.0.1:1/ws/workers", require_secure=True)
    # wss:// is accepted at construction (no connection attempted).
    WebSocketWorkerTransport("wss://api.example.com/ws/workers", require_secure=True)


def handle_until(
    client, expected: str, deadline_seconds: float = 60.0, poll: float = 1.0
) -> bool:
    """Process inbound messages until `expected` is seen, or the deadline passes.

    Other traffic (heartbeat_ack, pong) may legitimately arrive first, so a test
    must not assume the next message is the one it wants.

    Bounded by WALL-CLOCK time rather than an attempt count: an earlier version
    used tries x timeout, which could block for hours if a message never arrived.
    A deadlock must fail fast and visibly, not hang.
    """
    import time

    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if client.handle_next(timeout=poll) == expected:
            return True
    return False


def _await(collection, attempts: int = 100, delay: float = 0.05):
    import time

    for _ in range(attempts):
        if collection:
            return collection
        time.sleep(delay)
    raise AssertionError("expected a message that never arrived")


# ---------------------------------------------------------------------------
# Blender tier: real WebSockets AND real Blender
# ---------------------------------------------------------------------------


@pytest.fixture
def blender_stack(tmp_path: Path):
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")
    from studio_fixtures.seed_project import ensure_seed_project

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_path = projects_root / "seed_project.blend"
    shutil.copy2(ensure_seed_project(), project_path)

    executor = build_executor(
        tmp_path, SubprocessBlenderOperationExecutor(), project_path
    )
    manager = WorkerConnectionManager(
        expected_token=TOKEN, heartbeat_interval_seconds=60.0
    )
    with LocalControlPlaneServer(manager=manager) as server:
        yield {
            "server": server,
            "manager": manager,
            "executor": executor,
            "project_path": project_path,
            "url": server.url,
            "runtime": tmp_path / "runtime",
        }


def saved_cube_x(project_path: Path) -> float:
    from studio_fixtures.seed_project import inspect_blend

    digest = inspect_blend(project_path)["digest"]
    return digest["objects"][0]["world_position_meters"]["x"]


@pytest.mark.blender
def test_end_to_end_over_websockets_moves_the_cube_and_reports_success(blender_stack):
    """Outbound connect -> auth -> register -> offer -> accept -> Blender -> result."""
    assert coordinates_equal(saved_cube_x(blender_stack["project_path"]), 0.0)

    client = build_client(blender_stack["executor"], blender_stack["url"])
    try:
        assert client.connect_and_register(timeout=10.0) is True
        blender_stack["server"].queue_offer(
            WORKER_ID, make_job("job_e2e_1", "req_e2e_1")
        )
        assert handle_until(client, protocol.JOB_OFFER, deadline_seconds=120.0)

        results = _await(blender_stack["manager"].results, attempts=200)
        assert results[0]["job_status"] == "succeeded"
        assert results[0]["job_id"] == "job_e2e_1"
        assert results[0]["execution_phase"] == phases.COMPLETED

        # Durable on disk, read by a fresh Blender process.
        assert coordinates_equal(saved_cube_x(blender_stack["project_path"]), 0.5)
    finally:
        client.disconnect()


@pytest.mark.blender
def test_result_channel_interruption_then_reconnect_does_not_move_twice(
    blender_stack,
):
    """The critical case, over a real socket and real Blender."""
    client = build_client(blender_stack["executor"], blender_stack["url"])
    try:
        assert client.connect_and_register(timeout=10.0)
        blender_stack["server"].queue_offer(
            WORKER_ID, make_job("job_e2e_2", "req_e2e_2")
        )

        # Break the result channel: the mutation will succeed and save, but the
        # result send fails.
        transport = client.transport
        original_send = transport.send

        def failing_send(message):
            if message["type"] == protocol.JOB_RESULT:
                raise TransportError("result channel interrupted")
            return original_send(message)

        transport.send = failing_send  # type: ignore[assignment]
        assert handle_until(client, protocol.JOB_OFFER, deadline_seconds=120.0)

        assert coordinates_equal(saved_cube_x(blender_stack["project_path"]), 0.5)
        assert client.stats.results_undelivered == 1
        assert blender_stack["manager"].results == [], "no result got through"

        record = blender_stack["executor"].store.load(PROJECT_ID, "job_e2e_2")
        assert record.phase == phases.COMPLETED
        assert record.result_delivered is False
    finally:
        client.disconnect()

    # Reconnect with a fresh transport and reconcile from the durable journal.
    reconnected = build_client(blender_stack["executor"], blender_stack["url"])
    try:
        assert reconnected.connect_and_register(timeout=10.0) is True
        resent = reconnected.reconcile()
        assert resent == ["job_e2e_2"]

        results = _await(blender_stack["manager"].results, attempts=200)
        assert results[0]["job_status"] == "duplicate"

        # Still 0.50 — reconciliation reported, it did not re-execute.
        assert coordinates_equal(saved_cube_x(blender_stack["project_path"]), 0.5)
    finally:
        reconnected.disconnect()


@pytest.mark.blender
def test_redelivering_the_same_job_over_the_socket_does_not_move_twice(blender_stack):
    client = build_client(blender_stack["executor"], blender_stack["url"])
    try:
        assert client.connect_and_register(timeout=10.0)
        job = make_job("job_e2e_3", "req_e2e_3")

        blender_stack["server"].queue_offer(WORKER_ID, job)
        assert handle_until(client, protocol.JOB_OFFER, deadline_seconds=120.0)
        _await(blender_stack["manager"].results, attempts=200)
        assert coordinates_equal(saved_cube_x(blender_stack["project_path"]), 0.5)

        blender_stack["manager"].workers[WORKER_ID].worker_state = "ready"
        blender_stack["server"].queue_offer(WORKER_ID, job)
        assert handle_until(client, protocol.JOB_OFFER, deadline_seconds=120.0)

        results = _await(blender_stack["manager"].results, attempts=200)
        assert len(results) == 2
        assert results[1]["job_status"] == "duplicate"
        assert coordinates_equal(
            saved_cube_x(blender_stack["project_path"]), 0.5
        ), "must remain 0.50, not 1.00"
    finally:
        client.disconnect()
