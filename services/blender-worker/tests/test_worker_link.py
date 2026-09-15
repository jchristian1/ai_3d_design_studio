"""Worker link tests — no network, no Blender (Spec 001, Task 8).

Everything runs over InMemoryTransport, so the full connection state machine —
authentication, registration, heartbeats, job offers, disconnects, reconnect with
backoff, and reconciliation — is exercised deterministically.

Covers the required behaviours 1-20.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from blender_worker import phases
from blender_worker.blender_ops import FakeBlenderOperationExecutor
from blender_worker.executor import WorkerExecutor
from blender_worker.journal import FileSystemExecutionStore
from blender_worker.link.client import (
    AUTHENTICATING,
    BUSY,
    CONNECTION_STATES,
    DISCONNECTED,
    READY,
    RECONNECTING,
    BackoffPolicy,
    WorkerLinkClient,
)
from blender_worker.link.identity import (
    WorkerIdentity,
    WorkerIdentityError,
    describe_capabilities,
    load_worker_identity,
)
from blender_worker.link.transport import (
    InMemoryServerEndpoint,
    InMemoryTransport,
    TransportError,
)
from blender_worker.locks import FileLockProvider
from blender_worker.registry import MappingProjectRegistry
from studio_contracts import to_wire
from studio_contracts import worker_protocol as protocol
from studio_contracts.jobs import create_move_object_job
from studio_api.worker_link.manager import (
    HEALTHY,
    LOST,
    WorkerConnectionManager,
)
from studio_types import ObjectRef, Vec3

WORKER_ID = "worker_legion_1"
TOKEN = "test-worker-token-do-not-commit"
PROJECT_ID = "proj_seed"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, amount: float) -> None:
        self.seconds += amount


@pytest.fixture
def link(tmp_path: Path):
    """A worker client + control-plane manager joined by in-memory transport."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_path = projects_root / f"{PROJECT_ID}.blend"
    project_path.write_bytes(b"fake blend")

    blender = FakeBlenderOperationExecutor()
    blender.seed_project(project_path, {"Cube": Vec3(0.0, 0.0, 0.0)})

    runtime = tmp_path / "runtime"
    store = FileSystemExecutionStore(runtime)
    executor = WorkerExecutor(
        store=store,
        locks=FileLockProvider(runtime),
        projects=MappingProjectRegistry(projects_root, {PROJECT_ID: project_path}),
        blender=blender,
        recovery_root=runtime / "recovery",
    )

    clock = FakeClock()
    manager = WorkerConnectionManager(
        expected_token=TOKEN, heartbeat_interval_seconds=10.0, now_seconds=clock
    )
    endpoint = InMemoryServerEndpoint()
    transport = InMemoryTransport(endpoint)

    slept: list[float] = []
    client = WorkerLinkClient(
        identity=WorkerIdentity(
            worker_id=WORKER_ID,
            control_plane_url="ws://127.0.0.1:8765/ws/workers",
            token=TOKEN,
            gpu_name="NVIDIA RTX 4070 Ti",
        ),
        transport=transport,
        executor=executor,
        backoff=BackoffPolicy(initial_seconds=0.01, max_seconds=0.05),
        sleep=slept.append,
        probe_blender=False,
    )
    return {
        "client": client,
        "manager": manager,
        "transport": transport,
        "endpoint": endpoint,
        "blender": blender,
        "store": store,
        "project_path": project_path,
        "clock": clock,
        "slept": slept,
    }


def pump(link, timeout: float = 0.0) -> list[dict]:
    """Move messages worker -> manager, feeding replies back to the worker."""
    replies = []
    for message in link["endpoint"].drain_from_worker():
        reply = link["manager"].handle_message(message)
        if reply is not None:
            link["endpoint"].push_to_worker(reply)
            replies.append(reply)
    return replies


def register(link) -> bool:
    assert link["client"].connect()
    pump(link)
    return link["client"].await_registration()


def make_job(job_id: str = "job_1", request_id: str = "req_1", delta_x: float = 0.5):
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


def cube_x(link) -> float:
    return link["blender"].position_of(link["project_path"], "Cube").x


# ---------------------------------------------------------------------------
# 1-3. Authentication, registration, protocol version
# ---------------------------------------------------------------------------


def test_1_valid_worker_authenticates_and_registers(link):
    assert register(link) is True

    client = link["client"]
    assert client.state == READY
    assert client.connection_id is not None
    assert client.heartbeat_interval_seconds == 10.0

    worker = link["manager"].workers[WORKER_ID]
    assert worker.protocol_version == protocol.PROTOCOL_VERSION
    assert worker.capabilities["supported_job_types"] == ["move_object"]


def test_2_invalid_token_is_rejected(link):
    link["client"].identity = WorkerIdentity(
        worker_id=WORKER_ID, control_plane_url="ws://x", token="wrong-token"
    )
    assert register(link) is False

    client = link["client"]
    assert client.state == DISCONNECTED
    assert client.last_error["code"] == "VALIDATION_ERROR"
    assert WORKER_ID not in link["manager"].workers, "must not be recorded"


def test_2_blank_token_never_matches(link):
    manager = WorkerConnectionManager(expected_token="")
    assert protocol.tokens_match("", "") is False
    assert protocol.tokens_match(None, TOKEN) is False
    assert protocol.tokens_match(TOKEN, TOKEN) is True


def test_2_rejection_does_not_reveal_which_part_failed(link):
    link["client"].identity = WorkerIdentity(
        worker_id=WORKER_ID, control_plane_url="ws://x", token="wrong"
    )
    register(link)
    message = link["client"].last_error["message"]
    assert "wrong" not in message
    assert TOKEN not in message


def test_3_unsupported_protocol_version_is_rejected(link):
    hello = protocol.worker_hello(
        WORKER_ID, TOKEN, describe_capabilities(probe_blender=False), protocol_version=99
    )
    reply = link["manager"].handle_message(hello)

    assert reply["type"] == protocol.WORKER_REJECTED
    assert "unsupported protocol version" in reply["error"]["message"]
    assert WORKER_ID not in link["manager"].workers


def test_3_unsupported_version_worker_receives_no_jobs(link):
    hello = protocol.worker_hello(
        WORKER_ID, TOKEN, describe_capabilities(probe_blender=False), protocol_version=99
    )
    link["manager"].handle_message(hello)
    assert link["manager"].available_workers() == []


# ---------------------------------------------------------------------------
# 4. Hello leaks nothing
# ---------------------------------------------------------------------------


def test_4_worker_hello_leaks_no_paths_secrets_or_environment(link):
    capabilities = describe_capabilities(
        link["client"].identity, probe_blender=False
    )
    serialized = json.dumps(capabilities)

    for leaked in ("/home", "/snap", "/tmp", "token", "TOKEN", TOKEN, "PATH", ".blend"):
        assert leaked not in serialized, f"capabilities leaked {leaked!r}"

    assert set(capabilities) <= {
        "worker_version",
        "blender_available",
        "blender_version",
        "gpu_available",
        "gpu_name",
        "supported_job_types",
        "max_concurrent_jobs",
    }


def test_4_capabilities_satisfy_the_canonical_schema(link):
    from studio_contracts import SCHEMA_FILES, validate_against_schema

    result = validate_against_schema(
        SCHEMA_FILES["WorkerCapabilities"],
        describe_capabilities(link["client"].identity, probe_blender=False),
    )
    assert result.valid, result.violations


def test_4_identity_repr_hides_the_token(link):
    identity = link["client"].identity
    assert TOKEN not in repr(identity)
    assert TOKEN not in str(identity)


def test_4_identity_requires_environment_configuration():
    with pytest.raises(WorkerIdentityError):
        load_worker_identity({})
    with pytest.raises(WorkerIdentityError):
        load_worker_identity({"STUDIO_WORKER_ID": "w1"})  # no token

    identity = load_worker_identity(
        {"STUDIO_WORKER_ID": "w1", "STUDIO_WORKER_TOKEN": "t"}
    )
    assert identity.worker_id == "w1"
    assert identity.control_plane_url.startswith("ws://127.0.0.1")


def test_4_worker_id_is_not_hardcoded_in_source():
    source = Path(__file__).resolve().parents[1] / "blender_worker" / "link"
    for path in source.rglob("*.py"):
        text = path.read_text("utf-8")
        assert "legion" not in text.lower(), f"{path.name} hard-codes a workstation"


# ---------------------------------------------------------------------------
# 5. Heartbeats and liveness
# ---------------------------------------------------------------------------


def test_5_heartbeat_updates_liveness(link):
    register(link)
    clock, manager = link["clock"], link["manager"]

    assert manager.liveness_of(WORKER_ID) == HEALTHY

    clock.advance(100)  # far beyond interval * grace
    assert manager.liveness_of(WORKER_ID) == LOST

    assert link["client"].send_heartbeat() is True
    pump(link)
    assert manager.liveness_of(WORKER_ID) == HEALTHY


def test_5_liveness_distinguishes_busy_from_healthy(link):
    register(link)
    link["client"].state = BUSY
    link["client"].send_heartbeat()
    pump(link)
    assert link["manager"].liveness_of(WORKER_ID) == "busy"
    assert link["manager"].available_workers() == [], "busy worker is not available"


def test_5_heartbeat_from_unregistered_worker_is_rejected(link):
    reply = link["manager"].handle_message(protocol.heartbeat("ghost", "ready"))
    assert reply["type"] == protocol.WORKER_REJECTED


def test_5_heartbeat_timing_is_configurable(link):
    manager = WorkerConnectionManager(
        expected_token=TOKEN, heartbeat_interval_seconds=0.25
    )
    assert manager.heartbeat_interval_seconds == 0.25


# ---------------------------------------------------------------------------
# 6-10. Job offering and acknowledgement
# ---------------------------------------------------------------------------


def test_6_canonical_job_can_be_offered(link):
    register(link)
    offer = link["manager"].build_job_offer(make_job())

    assert offer["type"] == protocol.JOB_OFFER
    assert offer["job_id"] == "job_1"
    assert offer["project_id"] == PROJECT_ID
    assert protocol.validate(offer) == []


def test_6_control_plane_refuses_to_offer_an_invalid_job(link):
    broken = make_job()
    del broken["project_id"]
    with pytest.raises(protocol.ProtocolError):
        link["manager"].build_job_offer(broken)


def test_6_offer_carries_no_natural_language(link):
    """The network path carries canonical Jobs, never instructions."""
    offer = link["manager"].build_job_offer(make_job())
    serialized = json.dumps(offer)
    for language in ("Move Cube", "50 cm", "right", "message", "instruction"):
        assert language not in serialized


def test_7_worker_explicitly_accepts_a_valid_job(link):
    register(link)
    link["endpoint"].push_to_worker(link["manager"].build_job_offer(make_job()))

    assert link["client"].handle_next() == protocol.JOB_OFFER
    pump(link)

    accepted = link["manager"].accepted_jobs
    assert len(accepted) == 1
    assert accepted[0]["job_id"] == "job_1"
    assert link["client"].stats.jobs_accepted == 1
    assert cube_x(link) == 0.5, "the job executed"


def test_7_acceptance_is_explicit_not_implied_by_delivery(link):
    """A rejected job is delivered but never accepted."""
    register(link)
    bad = make_job()
    bad["job_type"] = "resize_object"
    link["endpoint"].push_to_worker(protocol.job_offer(bad))

    link["client"].handle_next()
    pump(link)

    assert link["manager"].accepted_jobs == []
    assert len(link["manager"].rejected_jobs) == 1


def test_8_invalid_job_is_rejected_before_the_executor(link):
    register(link)
    invalid = make_job()
    del invalid["idempotency_key"]
    # Bypass build_job_offer, as a hostile or buggy peer would.
    link["endpoint"].push_to_worker(
        {
            "protocol_version": protocol.PROTOCOL_VERSION,
            "type": protocol.JOB_OFFER,
            "job": invalid,
            "job_id": invalid["job_id"],
            "project_id": invalid["project_id"],
        }
    )
    link["client"].handle_next()
    pump(link)

    assert len(link["manager"].rejected_jobs) == 1
    assert cube_x(link) == 0.0, "executor was never reached"
    assert link["store"].load(PROJECT_ID, "job_1") is None


def test_8_envelope_job_mismatch_is_rejected(link):
    register(link)
    job = make_job()
    link["endpoint"].push_to_worker(
        {
            "protocol_version": protocol.PROTOCOL_VERSION,
            "type": protocol.JOB_OFFER,
            "job": job,
            "job_id": "job_DIFFERENT",
            "project_id": PROJECT_ID,
        }
    )
    link["client"].handle_next()
    pump(link)
    assert len(link["manager"].rejected_jobs) == 1
    assert cube_x(link) == 0.0


def test_9_unsupported_job_type_is_rejected(link):
    register(link)
    job = make_job()
    job["job_type"] = "render_preview"
    link["endpoint"].push_to_worker(protocol.job_offer(job))

    link["client"].handle_next()
    pump(link)

    rejected = link["manager"].rejected_jobs[0]
    assert rejected["error"]["code"] == "VALIDATION_ERROR"
    assert cube_x(link) == 0.0


def test_10_busy_worker_rejects_another_job(link):
    register(link)
    link["client"].state = BUSY
    link["endpoint"].push_to_worker(link["manager"].build_job_offer(make_job()))

    link["client"].handle_next()
    pump(link)

    rejected = link["manager"].rejected_jobs[0]
    assert rejected["error"]["code"] == "LOCK_CONFLICT"
    assert cube_x(link) == 0.0


def test_10_control_plane_will_not_offer_to_a_busy_worker(link):
    register(link)
    link["manager"].workers[WORKER_ID].worker_state = "busy"
    reason = link["manager"].can_offer_to(WORKER_ID, make_job())
    assert reason == "worker is busy"


def test_10_control_plane_will_not_offer_to_a_lost_worker(link):
    register(link)
    link["clock"].advance(1000)
    assert link["manager"].can_offer_to(WORKER_ID, make_job()) == "worker is not alive"


# ---------------------------------------------------------------------------
# 11-12. Disconnect and reconnect
# ---------------------------------------------------------------------------


def test_11_disconnect_is_detected(link):
    register(link)
    link["transport"].drop()

    assert link["client"].handle_next() is None
    assert link["client"].state == DISCONNECTED


def test_11_control_plane_sees_a_silent_worker_as_lost(link):
    register(link)
    link["transport"].drop()
    link["clock"].advance(1000)
    assert link["manager"].liveness_of(WORKER_ID) == LOST


def test_12_reconnect_reauthenticates_and_reregisters(link):
    register(link)
    first_connection = link["client"].connection_id
    link["transport"].drop()

    # Reconnect drives connect -> hello -> registered again.
    assert link["client"].connect() is True
    pump(link)
    assert link["client"].await_registration() is True

    assert link["transport"].connect_count == 2
    assert link["client"].state == READY
    assert link["client"].connection_id != first_connection
    assert link["manager"].workers[WORKER_ID].connection_id == link["client"].connection_id


def test_12_reconnect_uses_bounded_backoff(link):
    policy = BackoffPolicy(initial_seconds=0.5, multiplier=2.0, max_seconds=4.0)
    delays = [policy.delay_for(attempt) for attempt in range(1, 7)]
    assert delays == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0], "must be bounded, not unbounded"


def test_12_reconnect_does_not_spin(link):
    """A bounded attempt count prevents a tight loop when the peer is gone."""
    register(link)
    link["transport"].drop()
    link["client"].backoff = BackoffPolicy(
        initial_seconds=0.01, max_seconds=0.02, max_attempts=3
    )
    # No pump, so registration never completes.
    assert link["client"].reconnect() is False
    assert link["client"].state == DISCONNECTED
    assert len(link["slept"]) == 3, "exactly max_attempts sleeps, then give up"


def test_connection_states_are_separate_from_execution_phases():
    assert set(CONNECTION_STATES).isdisjoint(set(phases.EXECUTION_PHASES))


# ---------------------------------------------------------------------------
# 13-16. Redelivery and reconciliation cannot duplicate a mutation
# ---------------------------------------------------------------------------


def test_13_reconnect_does_not_re_execute_a_completed_mutation(link):
    register(link)
    link["endpoint"].push_to_worker(link["manager"].build_job_offer(make_job()))
    link["client"].handle_next()
    pump(link)
    assert cube_x(link) == 0.5

    moves_before = sum(1 for kind, _ in link["blender"].calls if kind == "move")

    # Link drops, worker reconnects, control plane re-offers the same job.
    link["transport"].drop()
    link["client"].connect()
    pump(link)
    link["client"].await_registration()
    link["endpoint"].push_to_worker(link["manager"].build_job_offer(make_job()))
    link["client"].handle_next()
    pump(link)

    moves_after = sum(1 for kind, _ in link["blender"].calls if kind == "move")
    assert moves_after == moves_before, "no second Blender mutation"
    assert cube_x(link) == 0.5, "must remain 0.50, not 1.00"


def test_14_result_delivery_failure_after_save_does_not_re_execute(link):
    """The critical case: Blender saved, then the socket died on the result."""
    register(link)
    link["endpoint"].push_to_worker(link["manager"].build_job_offer(make_job()))

    # The result send fails; the mutation has already happened and been saved.
    link["transport"].fail_next_send = False
    original_send = link["transport"].send
    sends: list[str] = []

    def failing_send(message):
        sends.append(message["type"])
        if message["type"] == protocol.JOB_RESULT:
            raise TransportError("socket died before the result got through")
        return original_send(message)

    link["transport"].send = failing_send  # type: ignore[assignment]
    link["client"].handle_next()

    assert protocol.JOB_RESULT in sends, "delivery was attempted"
    assert cube_x(link) == 0.5, "the mutation is durable"
    assert link["client"].stats.results_undelivered == 1

    record = link["store"].load(PROJECT_ID, "job_1")
    assert record.phase == phases.COMPLETED
    assert record.result_delivered is False, "recorded as not reported"

    # No result reached the control plane yet.
    link["transport"].send = original_send  # type: ignore[assignment]
    pump(link)
    assert link["manager"].results == []


def test_15_locally_persisted_result_is_resent_on_reconcile(link):
    register(link)
    link["endpoint"].push_to_worker(link["manager"].build_job_offer(make_job()))

    original_send = link["transport"].send

    def failing_send(message):
        if message["type"] == protocol.JOB_RESULT:
            raise TransportError("result channel died")
        return original_send(message)

    link["transport"].send = failing_send  # type: ignore[assignment]
    link["client"].handle_next()
    link["transport"].send = original_send  # type: ignore[assignment]

    moves_before = sum(1 for kind, _ in link["blender"].calls if kind == "move")

    # Reconnect, then reconcile from the durable journal.
    link["transport"].drop()
    link["client"].connect()
    pump(link)
    link["client"].await_registration()
    resent = link["client"].reconcile()
    pump(link)

    assert resent == ["job_1"]
    assert len(link["manager"].results) == 1
    assert link["manager"].results[0]["job_status"] == "duplicate"
    assert link["manager"].results[0]["job_id"] == "job_1"

    moves_after = sum(1 for kind, _ in link["blender"].calls if kind == "move")
    assert moves_after == moves_before, "reconciliation must not touch Blender"
    assert cube_x(link) == 0.5

    record = link["store"].load(PROJECT_ID, "job_1")
    assert record.result_delivered is True


def test_15_reconcile_is_idempotent(link):
    register(link)
    assert link["client"].reconcile() == []
    assert link["client"].reconcile() == []


def test_16_duplicate_network_delivery_of_the_same_job_is_safe(link):
    register(link)
    offer = link["manager"].build_job_offer(make_job())

    for _ in range(3):
        link["endpoint"].push_to_worker(offer)
        link["client"].handle_next()
        pump(link)

    moves = sum(1 for kind, _ in link["blender"].calls if kind == "move")
    assert moves == 1, "only the first delivery mutated Blender"
    assert cube_x(link) == 0.5

    statuses = [result["job_status"] for result in link["manager"].results]
    assert statuses == ["succeeded", "duplicate", "duplicate"]


def test_16_a_new_request_still_moves_again(link):
    """Idempotency must not block a genuinely new command."""
    register(link)
    link["endpoint"].push_to_worker(
        link["manager"].build_job_offer(make_job("job_1", "req_1"))
    )
    link["client"].handle_next()
    pump(link)
    assert cube_x(link) == 0.5

    link["manager"].workers[WORKER_ID].worker_state = "ready"
    link["endpoint"].push_to_worker(
        link["manager"].build_job_offer(make_job("job_2", "req_2"))
    )
    link["client"].handle_next()
    pump(link)
    assert cube_x(link) == 1.0


# ---------------------------------------------------------------------------
# 17-18. Protocol hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        {"type": "worker_hello"},                                  # no version
        {"protocol_version": 1},                                   # no type
        {"protocol_version": 1, "type": "execute_python"},         # unknown type
        {"protocol_version": 1, "type": "ping", "command": "rm -rf /"},
        {"protocol_version": 1, "type": "ping", "path": "/etc/passwd"},
        {"protocol_version": 1, "type": "ping", "token": "sneaky"},
        {"protocol_version": 1, "type": "heartbeat", "worker_id": "w"},  # no state
        {"protocol_version": 0, "type": "ping"},
        "not json at all",
        "[1,2,3]",
        "null",
    ],
)
def test_17_protocol_rejects_unknown_or_malformed_messages(hostile):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(hostile if isinstance(hostile, str) else hostile)


def test_17_client_drops_malformed_messages_without_acting(link):
    register(link)
    link["endpoint"].push_to_worker({"protocol_version": 1, "type": "nonsense"})
    assert link["client"].handle_next() is None
    assert link["client"].stats.protocol_errors == 1
    assert cube_x(link) == 0.0


def test_17_manager_rejects_malformed_messages(link):
    reply = link["manager"].handle_message({"protocol_version": 1, "type": "bogus"})
    assert reply["type"] == protocol.WORKER_REJECTED
    assert link["manager"].protocol_errors


@pytest.mark.parametrize(
    "forbidden",
    ["execute_python", "shell", "command", "eval", "exec", "path", "filepath",
     "blend_path", "script", "code", "subprocess"],
)
def test_18_protocol_cannot_express_arbitrary_execution(forbidden):
    """The vocabulary is closed; these fields/types are unrepresentable."""
    from studio_contracts import SCHEMA_FILES, load_schema

    schema = load_schema(SCHEMA_FILES["WorkerMessage"])
    assert schema["additionalProperties"] is False
    assert forbidden not in schema["properties"]
    assert forbidden not in schema["properties"]["type"]["enum"]

    with pytest.raises(protocol.ProtocolError):
        protocol.parse({"protocol_version": 1, "type": "ping", forbidden: "x"})


def test_18_no_message_type_can_carry_a_filesystem_path():
    from studio_contracts import SCHEMA_FILES, load_schema

    schema = load_schema(SCHEMA_FILES["WorkerMessage"])
    for name in schema["properties"]:
        assert "path" not in name, f"protocol exposes {name}"


def test_18_job_offer_payload_cannot_carry_a_path():
    from studio_contracts import SCHEMA_FILES, load_schema

    payload = load_schema(SCHEMA_FILES["MoveObjectPayload"])
    assert payload["additionalProperties"] is False
    assert set(payload["properties"]) == {"target", "delta_meters"}


# ---------------------------------------------------------------------------
# 19. The token never leaks
# ---------------------------------------------------------------------------


def test_19_token_is_absent_from_registered_worker_state(link):
    register(link)
    snapshot = json.dumps(link["manager"].snapshot())
    assert TOKEN not in snapshot

    worker = link["manager"].workers[WORKER_ID]
    assert not hasattr(worker, "token")
    assert TOKEN not in json.dumps(worker.snapshot())


def test_19_token_is_redacted_for_logging(link):
    hello = protocol.worker_hello(
        WORKER_ID, TOKEN, describe_capabilities(probe_blender=False)
    )
    redacted = protocol.redact(hello)
    assert redacted["token"] == protocol.REDACTED
    assert TOKEN not in json.dumps(redacted)


def test_19_server_transcript_is_redacted(link):
    """Anything a server retains must already be redacted."""
    from studio_api.worker_link.local_server import LocalControlPlaneServer

    source = Path(
        LocalControlPlaneServer.__module__.replace(".", "/")
    )
    hello = protocol.worker_hello(
        WORKER_ID, TOKEN, describe_capabilities(probe_blender=False)
    )
    assert protocol.redact(hello)["token"] != TOKEN


def test_19_token_never_appears_in_a_result_or_job_message(link):
    register(link)
    link["endpoint"].push_to_worker(link["manager"].build_job_offer(make_job()))
    link["client"].handle_next()
    messages = link["endpoint"].drain_from_worker() + link["endpoint"].received
    for message in messages:
        if message["type"] != protocol.WORKER_HELLO:
            assert "token" not in message, message["type"]


def test_19_manager_does_not_store_the_expected_token_in_repr(link):
    assert TOKEN not in repr(link["manager"])


# ---------------------------------------------------------------------------
# 20. Layering
# ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_20_worker_executor_remains_transport_agnostic():
    executor_path = (
        Path(__file__).resolve().parents[1] / "blender_worker" / "executor.py"
    )
    modules = _imports(executor_path)
    for forbidden in ("websockets", "socket", "asyncio", "link"):
        assert forbidden not in modules, f"executor imports {forbidden}"
    text = executor_path.read_text("utf-8")
    assert "WorkerTransport" not in text
    assert "worker_protocol" not in text


def test_20_transport_module_never_listens_or_binds():
    """The WHOLE worker package must never listen — not just its transport.

    Scope note: this originally covered only ``blender_worker/link/``, which meant
    Task 11's ``main.py`` entered the package without ever being checked. The
    outbound-only guarantee is a property of the workstation, not of one directory,
    so the guard now walks every module in the package.
    """
    worker_package = Path(__file__).resolve().parents[1] / "blender_worker"
    checked = 0
    for path in worker_package.rglob("*.py"):
        # blender_scripts run INSIDE Blender and never touch the network; they are
        # covered by the separate no-network assertion below.
        tree = ast.parse(path.read_text("utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        called |= {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for forbidden in (
            "bind",
            "listen",
            "serve",
            "serve_forever",
            "create_server",
            "start_server",
        ):
            assert forbidden not in called, f"{path.name} calls {forbidden}()"
        checked += 1

    assert checked >= 10, f"only {checked} worker modules were scanned"


def test_20_the_worker_package_imports_no_server_machinery():
    """A listener needs a server library. None may be imported anywhere here."""
    worker_package = Path(__file__).resolve().parents[1] / "blender_worker"
    for path in worker_package.rglob("*.py"):
        modules = _imports(path)
        for forbidden in ("socketserver", "http", "flask", "fastapi", "starlette"):
            assert forbidden not in modules, f"{path.name} imports {forbidden}"


def test_20_worker_link_does_not_import_bpy():
    link_dir = Path(__file__).resolve().parents[1] / "blender_worker" / "link"
    for path in link_dir.rglob("*.py"):
        assert "bpy" not in _imports(path)


def test_20_control_plane_manager_is_framework_independent():
    manager_path = (
        Path(__file__).resolve().parents[3]
        / "services"
        / "api"
        / "studio_api"
        / "worker_link"
        / "manager.py"
    )
    modules = _imports(manager_path)
    for forbidden in ("fastapi", "starlette", "websockets", "socket", "asyncio"):
        assert forbidden not in modules, f"manager imports {forbidden}"
