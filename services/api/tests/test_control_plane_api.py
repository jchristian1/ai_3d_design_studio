"""Fast control-plane API tests — no sockets, no Blender (Spec 001, Task 9).

Everything runs through FastAPI's TestClient against an application built with
explicit fake collaborators, so the full path

    HTTP -> identity -> project registry -> AgentProvider -> JobFactory
         -> canonical Job -> job record -> worker selection -> offer

is exercised deterministically.

The "worker" here is a list that captures pushed protocol messages. That is enough
to prove dispatch and reconciliation, because the protocol is pure data; real
sockets and real Blender are covered by tests/integration and tests/e2e.

Covers required behaviours 1-18.
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

from studio_agent import AgentContext  # noqa: E402
from studio_agent.plan import AgentResult, ProviderMetadata  # noqa: E402
from studio_api.app import create_app  # noqa: E402
from studio_api.dependencies import build_dependencies  # noqa: E402
from studio_api.identity import (  # noqa: E402
    SOURCE_DEVELOPMENT_DEPENDENCY,
    DevelopmentIdentityResolver,
    IdentityError,
)
from studio_api.job_records import InMemoryJobRecordStore  # noqa: E402
from studio_api.settings import (  # noqa: E402
    ConfigurationError,
    Settings,
    load_settings,
)
from studio_api.worker_link.manager import WorkerConnectionManager  # noqa: E402
from studio_contracts import SCHEMA_FILES, validate_against_schema  # noqa: E402
from studio_contracts import worker_protocol as protocol  # noqa: E402
from studio_contracts.jobs import derive_idempotency_key, validate_job  # noqa: E402

TOKEN = "fast-test-worker-token-not-committed"
PROJECT_ID = "proj_seed"
WORKER_ID = "worker_fast_1"
DEV_USER_ID = "user_dev_local"

MOVE_COMMAND = "Move Cube 50 cm to the right."

API_SOURCE_DIR = Path(__file__).resolve().parents[1] / "studio_api"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock the manager reads, so liveness never needs sleeping."""

    def __init__(self) -> None:
        self.seconds = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, amount: float) -> None:
        self.seconds += amount


class FakeWorker:
    """A protocol-speaking worker with no transport.

    Captures what the control plane pushed, and can send protocol messages back.
    """

    def __init__(self, worker_id: str = WORKER_ID) -> None:
        self.worker_id = worker_id
        self.received: list[dict] = []

    def sink(self, message: dict) -> bool:
        self.received.append(message)
        return True

    # -- outbound (worker -> control plane) --------------------------------

    def capabilities(
        self,
        supported_job_types: tuple[str, ...] = ("move_object",),
        blender_available: bool = True,
    ) -> dict:
        capabilities: dict[str, Any] = {
            "worker_version": "0.1.0",
            "blender_available": blender_available,
            "supported_job_types": list(supported_job_types),
            "max_concurrent_jobs": 1,
            "gpu_available": True,
            "gpu_name": "NVIDIA RTX 4070 Ti",
        }
        if blender_available:
            capabilities["blender_version"] = "4.2.1"
        return capabilities

    @property
    def offers(self) -> list[dict]:
        return [m for m in self.received if m["type"] == protocol.JOB_OFFER]

    @property
    def offered_jobs(self) -> list[dict]:
        return [offer["job"] for offer in self.offers]


def build_app(
    settings: Optional[Settings] = None,
    provider: Any = None,
    store: Any = None,
    clock: Optional[FakeClock] = None,
    identity_resolver: Any = None,
) -> tuple[TestClient, Any]:
    """Build an application plus its dependency container."""
    resolved_settings = settings or Settings(
        environment="local",
        worker_token=TOKEN,
        project_ids=(PROJECT_ID,),
        development_user_id=DEV_USER_ID,
        heartbeat_interval_seconds=10.0,
    )
    manager = WorkerConnectionManager(
        expected_token=resolved_settings.worker_token,
        heartbeat_interval_seconds=resolved_settings.heartbeat_interval_seconds,
        now_seconds=clock,
    )
    dependencies = build_dependencies(
        resolved_settings,
        provider=provider,
        store=store,
        manager=manager,
        identity_resolver=identity_resolver,
    )
    app = create_app(resolved_settings, dependencies)
    client = TestClient(app, raise_server_exceptions=False)
    return client, dependencies


def connect_worker(
    dependencies: Any,
    worker: Optional[FakeWorker] = None,
    supported_job_types: tuple[str, ...] = ("move_object",),
    blender_available: bool = True,
) -> FakeWorker:
    """Authenticate and register a fake worker through the real protocol path."""
    resolved = worker or FakeWorker()
    hello = protocol.worker_hello(
        worker_id=resolved.worker_id,
        token=TOKEN,
        capabilities=resolved.capabilities(supported_job_types, blender_available),
    )
    reply = dependencies.gateway.handle_inbound(hello)
    assert reply is not None and reply["type"] == protocol.WORKER_REGISTERED, reply
    dependencies.gateway.attach(resolved.worker_id, resolved.sink)
    return resolved


def chat_body(
    request_id: str = "req_fast_001",
    message: str = MOVE_COMMAND,
    project_id: str = PROJECT_ID,
    session_id: str = "sess_fast",
    **extra: Any,
) -> dict:
    body = {
        "request_id": request_id,
        "project_id": project_id,
        "session_id": session_id,
        "message": message,
    }
    body.update(extra)
    return body


def job_path(job_id: str, project_id: str = PROJECT_ID) -> str:
    """The project-scoped job status path. There is no unscoped variant."""
    return f"/api/projects/{project_id}/jobs/{job_id}"


def send_result(
    dependencies: Any,
    worker: FakeWorker,
    job_id: str,
    job_status: str = "succeeded",
    final_x: float = 0.5,
    project_id: str = PROJECT_ID,
) -> None:
    """Have the worker report a structured result."""
    dependencies.gateway.handle_inbound(
        protocol.job_result(
            worker_id=worker.worker_id,
            job_id=job_id,
            project_id=project_id,
            job_status=job_status,
            result={
                "job_id": job_id,
                "target": {"name": "Cube"},
                "requested_delta_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
                "applied": job_status == "succeeded",
                "already_applied": job_status == "duplicate",
                "verified": True,
                "final_position_meters": {"x": final_x, "y": 0.0, "z": 0.0},
            },
            execution_phase="completed",
        )
    )


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
# 1. Health
# ---------------------------------------------------------------------------


def test_1_health_succeeds():
    client, _ = build_app()
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["api"] == "healthy"
    assert body["registered_workers"] == 0
    assert body["ready_workers"] == 0


def test_1_health_does_not_claim_blender_is_healthy_just_because_the_api_is():
    """The API being alive says nothing about Blender."""
    client, _ = build_app()
    body = client.get("/health").json()

    assert body["api"] == "healthy"
    assert body["blender_capable_workers"] == 0, (
        "no worker has connected, so no Blender can be claimed healthy"
    )


def test_1_health_reports_worker_counts_once_a_worker_registers():
    client, dependencies = build_app()
    connect_worker(dependencies)

    body = client.get("/health").json()
    assert body["registered_workers"] == 1
    assert body["ready_workers"] == 1
    assert body["blender_capable_workers"] == 1


def test_1_health_stops_counting_a_worker_that_stopped_heartbeating():
    clock = FakeClock()
    client, dependencies = build_app(clock=clock)
    connect_worker(dependencies)
    assert client.get("/health").json()["ready_workers"] == 1

    clock.advance(10_000)  # far beyond interval * grace
    assert client.get("/health").json()["ready_workers"] == 0


# ---------------------------------------------------------------------------
# 2. /api/workers leaks nothing
# ---------------------------------------------------------------------------


def test_2_workers_endpoint_reports_safe_development_information():
    client, dependencies = build_app()
    connect_worker(dependencies)

    body = client.get("/api/workers").json()
    assert body["registered_workers"] == 1
    assert body["ready_workers"] == 1

    worker = body["workers"][0]
    assert worker["worker_id"] == WORKER_ID
    assert worker["liveness"] == "healthy"
    assert worker["worker_state"] == "ready"
    assert worker["connected"] is True
    assert worker["capabilities"]["supported_job_types"] == ["move_object"]
    assert worker["capabilities"]["blender_available"] is True
    assert worker["capabilities"]["blender_version"] == "4.2.1"
    assert worker["capabilities"]["gpu_name"] == "NVIDIA RTX 4070 Ti"


def test_2_workers_endpoint_does_not_leak_secrets_paths_or_environment():
    client, dependencies = build_app()
    connect_worker(dependencies)

    serialized = json.dumps(client.get("/api/workers").json())
    for leaked in (
        TOKEN,
        "token",
        "/home",
        "/snap",
        "/tmp",
        ".blend",
        "PATH",
        "STUDIO_WORKER_TOKEN",
        "environ",
        "password",
        "secret",
    ):
        assert leaked not in serialized, f"/api/workers leaked {leaked!r}"


def test_2_workers_endpoint_projects_only_allow_listed_capability_fields():
    """A field the control plane did not declare cannot be echoed back."""
    client, dependencies = build_app()
    worker = FakeWorker()
    # Register directly through the manager so an unexpected key bypasses the
    # protocol schema, as a compromised or buggy worker might attempt.
    dependencies.manager.workers.clear()
    hello = protocol.worker_hello(
        worker_id=worker.worker_id, token=TOKEN, capabilities=worker.capabilities()
    )
    dependencies.manager.handle_message(hello)
    dependencies.manager.workers[worker.worker_id].capabilities["home_directory"] = (
        "/home/christian"
    )
    dependencies.manager.workers[worker.worker_id].capabilities["token"] = TOKEN

    serialized = json.dumps(client.get("/api/workers").json())
    assert "home_directory" not in serialized
    assert "/home/christian" not in serialized
    assert TOKEN not in serialized


def test_2_registered_worker_state_never_holds_the_token():
    _, dependencies = build_app()
    connect_worker(dependencies)

    registered = dependencies.manager.workers[WORKER_ID]
    assert not hasattr(registered, "token")
    assert TOKEN not in json.dumps(registered.snapshot())


# ---------------------------------------------------------------------------
# 3. Malformed requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"request_id": "r", "project_id": PROJECT_ID, "session_id": "s"},  # no message
        {"request_id": "r", "project_id": PROJECT_ID, "message": "m"},  # no session
        {"project_id": PROJECT_ID, "session_id": "s", "message": "m"},  # no request_id
        {"request_id": "r", "session_id": "s", "message": "m"},  # no project_id
        chat_body(request_id=""),  # blank
        chat_body(session_id="   "),  # whitespace only
        chat_body(message=""),
        chat_body(project_id=""),
        {**chat_body(), "unexpected_field": "x"},  # additionalProperties: false
    ],
)
def test_3_malformed_chat_request_is_rejected(body):
    client, dependencies = build_app()
    connect_worker(dependencies)

    response = client.post("/api/chat", json=body)

    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert payload["error"]["message"]


def test_3_malformed_request_creates_no_job():
    client, dependencies = build_app()
    connect_worker(dependencies)

    client.post("/api/chat", json=chat_body(message=""))

    assert dependencies.store.all_records() == ()


def test_3_validation_errors_do_not_echo_submitted_values():
    """A rejected value must not be reflected back — it might be a secret."""
    client, _ = build_app()
    secret = "sk-live-do-not-reflect-this"

    response = client.post("/api/chat", json={**chat_body(), "api_key": secret})

    assert response.status_code == 422
    assert secret not in response.text


def test_3_non_json_body_is_rejected_structurally():
    client, _ = build_app()
    response = client.post(
        "/api/chat", content=b"not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 4. Unknown project
# ---------------------------------------------------------------------------


def test_4_unknown_project_is_rejected_with_404():
    client, dependencies = build_app()
    connect_worker(dependencies)

    response = client.post("/api/chat", json=chat_body(project_id="proj_does_not_exist"))

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_4_unknown_project_creates_no_job_and_offers_nothing():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    client.post("/api/chat", json=chat_body(project_id="proj_other"))

    assert dependencies.store.all_records() == ()
    assert worker.offers == [], "nothing may be dispatched for an unknown project"


def test_4_unknown_project_error_does_not_reveal_known_projects():
    client, _ = build_app()
    response = client.post("/api/chat", json=chat_body(project_id="proj_other"))

    assert PROJECT_ID not in response.text, "must not enumerate other projects"


# ---------------------------------------------------------------------------
# 5. The exact Spec 001 command produces a canonical move job
# ---------------------------------------------------------------------------


def test_5_exact_command_produces_one_canonical_move_object_job():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    response = client.post("/api/chat", json=chat_body(request_id="req_move_1"))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_status"] == "queued"
    assert body["job_id"]

    assert len(worker.offered_jobs) == 1, "exactly one job for one instruction"
    job = worker.offered_jobs[0]

    # It satisfies the canonical Job contract (Task 3).
    validation = validate_job(job)
    assert validation.valid, validation.errors

    assert job["job_type"] == "move_object"
    assert job["project_id"] == PROJECT_ID
    assert job["payload"]["target"]["name"] == "Cube"
    # "50 cm to the right" resolved to +0.50 m on world X.
    assert job["payload"]["delta_meters"] == {"x": 0.5, "y": 0.0, "z": 0.0}
    assert job["status"] == "queued"


def test_5_offered_job_carries_no_unresolved_language():
    """Units and directions are resolved before anything leaves the API."""
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    client.post("/api/chat", json=chat_body())

    serialized = json.dumps(worker.offered_jobs[0])
    for language in ("50 cm", "right", "Move Cube", "message", "instruction", "unit"):
        assert language not in serialized, f"job leaked unresolved {language!r}"


def test_5_offered_job_matches_the_worker_protocol_schema():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    client.post("/api/chat", json=chat_body())

    assert protocol.validate(worker.offers[0]) == []


def test_5_a_new_request_id_with_identical_text_creates_a_second_job():
    """Idempotency must not block a genuinely repeated intentional command."""
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    first = client.post("/api/chat", json=chat_body(request_id="req_a")).json()
    send_result(dependencies, worker, first["job_id"], final_x=0.5)

    second = client.post("/api/chat", json=chat_body(request_id="req_b")).json()

    assert first["job_id"] != second["job_id"]
    assert len(worker.offered_jobs) == 2
    assert (
        worker.offered_jobs[0]["idempotency_key"]
        != worker.offered_jobs[1]["idempotency_key"]
    ), "two intentional commands are two mutations"


# ---------------------------------------------------------------------------
# 6. Trusted identity comes from the application, not the body
# ---------------------------------------------------------------------------


def test_6_user_id_comes_from_the_application_dependency():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    client.post("/api/chat", json=chat_body())

    job = worker.offered_jobs[0]
    assert job["user_id"] == DEV_USER_ID, "identity must come from the resolver"


def test_6_user_id_in_the_request_body_is_rejected_not_trusted():
    """The canonical ChatRequest has no user_id, so sending one is an error."""
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    response = client.post(
        "/api/chat", json={**chat_body(), "user_id": "user_attacker"}
    )

    assert response.status_code == 422, "a body user_id must not be silently ignored"
    assert worker.offers == []
    assert dependencies.store.all_records() == ()


def test_6_a_different_configured_development_user_changes_the_job_identity():
    """Proves the identity really is read from the dependency, not hardcoded."""
    settings = Settings(
        environment="local",
        worker_token=TOKEN,
        project_ids=(PROJECT_ID,),
        development_user_id="user_someone_else",
    )
    client, dependencies = build_app(settings=settings)
    worker = connect_worker(dependencies)

    client.post("/api/chat", json=chat_body())

    assert worker.offered_jobs[0]["user_id"] == "user_someone_else"


def test_6_identity_records_how_it_was_established():
    resolver = DevelopmentIdentityResolver(user_id=DEV_USER_ID)
    identity = resolver.resolve()

    assert identity.user_id == DEV_USER_ID
    assert identity.source == SOURCE_DEVELOPMENT_DEPENDENCY
    assert identity.is_development is True


def test_6_development_identity_refuses_to_run_outside_local():
    """The temporary resolver cannot be shipped by accident."""
    resolver = DevelopmentIdentityResolver(
        user_id=DEV_USER_ID, environment="production"
    )
    with pytest.raises(IdentityError):
        resolver.resolve()


def test_6_a_refusing_identity_resolver_yields_401_and_no_job():
    class RefusingResolver:
        def resolve(self, credentials=None):
            raise IdentityError("authentication is not configured")

    client, dependencies = build_app(identity_resolver=RefusingResolver())
    connect_worker(dependencies)

    response = client.post("/api/chat", json=chat_body())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert dependencies.store.all_records() == ()


# ---------------------------------------------------------------------------
# 7. request_id is preserved
# ---------------------------------------------------------------------------


def test_7_request_id_is_preserved_through_the_whole_path():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    request_id = "req_preserve_me_123"

    response = client.post("/api/chat", json=chat_body(request_id=request_id))
    body = response.json()

    # In the HTTP acknowledgement...
    assert body["request_id"] == request_id
    # ...in the canonical Job's origin...
    job = worker.offered_jobs[0]
    assert job["origin"]["request_id"] == request_id
    assert job["origin"]["operation_index"] == 0
    # ...in the derived mutation identity...
    assert job["idempotency_key"] == derive_idempotency_key(
        project_id=PROJECT_ID, request_id=request_id, operation_index=0
    )
    # ...and in the job status projection.
    status = client.get(job_path(body['job_id'])).json()
    assert status["request_id"] == request_id


def test_7_request_id_is_echoed_on_error_responses():
    client, _ = build_app()
    response = client.post(
        "/api/chat", json=chat_body(request_id="req_err_1", project_id="proj_nope")
    )

    assert response.status_code == 404
    assert response.json()["request_id"] == "req_err_1"


# ---------------------------------------------------------------------------
# 8. RuleBasedProvider stays behind the AgentProvider boundary
# ---------------------------------------------------------------------------


def test_8_routes_and_service_never_name_a_concrete_provider():
    """Switching to AstraProvider must not require editing a route."""
    for path in list((API_SOURCE_DIR / "routes").rglob("*.py")) + [
        API_SOURCE_DIR / "chat_service.py"
    ]:
        text = path.read_text("utf-8")
        for concrete in ("RuleBasedProvider", "AstraProvider", "CodexProvider"):
            assert concrete not in text, f"{path.name} names {concrete}"


def test_8_only_the_dependency_container_resolves_a_provider():
    container = (API_SOURCE_DIR / "dependencies.py").read_text("utf-8")
    assert "get_provider" in container, "the registry is the resolution point"

    for path in (API_SOURCE_DIR / "routes").rglob("*.py"):
        assert "get_provider" not in path.read_text("utf-8")


def test_8_the_service_accepts_any_agent_provider():
    """A stub provider drives the whole flow, proving the seam is real."""
    from studio_types import MoveObjectPayload, ObjectRef, Vec3
    from studio_agent.plan import AgentPlan, PlannedOperation

    class StubProvider:
        name = "stub"
        version = "test"
        seen: list[AgentContext] = []

        def interpret(self, request, context):
            StubProvider.seen.append(context)
            return AgentResult.success(
                AgentPlan(
                    operations=(
                        PlannedOperation(
                            operation_type="move_object",
                            payload=MoveObjectPayload(
                                target=ObjectRef(name="Cube"),
                                delta_meters=Vec3(0.25, 0.0, 0.0),
                            ),
                        ),
                    )
                ),
                ProviderMetadata(provider_name=self.name, provider_version=self.version),
            )

    client, dependencies = build_app(provider=StubProvider())
    worker = connect_worker(dependencies)

    body = client.post("/api/chat", json=chat_body(message="anything at all")).json()

    assert body["provider"] == "stub"
    assert worker.offered_jobs[0]["payload"]["delta_meters"]["x"] == 0.25
    # The provider received trusted identity, not the request body's claims.
    assert StubProvider.seen[0].user_id == DEV_USER_ID
    assert StubProvider.seen[0].project_id == PROJECT_ID


def test_8_provider_is_selected_by_configuration_name():
    settings = Settings(
        environment="local",
        worker_token=TOKEN,
        project_ids=(PROJECT_ID,),
        agent_provider="astra",
    )
    _, dependencies = build_app(settings=settings)
    assert dependencies.provider.name == "astra"


def test_8_provider_unavailable_yields_503():
    settings = Settings(
        environment="local",
        worker_token=TOKEN,
        project_ids=(PROJECT_ID,),
        agent_provider="astra",  # boundary exists, not implemented
    )
    client, dependencies = build_app(settings=settings)
    worker = connect_worker(dependencies)

    response = client.post("/api/chat", json=chat_body())

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert worker.offers == [], "an unavailable provider must never dispatch work"


# ---------------------------------------------------------------------------
# 9. Unsupported instruction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Make the kitchen look nicer.",
        "Delete everything.",
        "Move the cube a little to the left.",
        "Rotate Cube 45 degrees.",
        "Move Cube 50 cm toward the window.",
    ],
)
def test_9_unsupported_instruction_yields_a_structured_error(message):
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    response = client.post("/api/chat", json=chat_body(message=message))

    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["error"]["code"] in ("UNSUPPORTED_INSTRUCTION", "VALIDATION_ERROR")
    assert payload["error"]["message"]
    assert worker.offers == [], "an uninterpreted request must not reach a worker"


def test_9_unsupported_units_yield_a_structured_error():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    response = client.post(
        "/api/chat", json=chat_body(message="Move Cube 50 furlongs to the right.")
    )

    assert response.status_code == 422
    assert worker.offers == []


def test_9_unsupported_instruction_creates_no_job_record():
    client, dependencies = build_app()
    connect_worker(dependencies)

    client.post("/api/chat", json=chat_body(message="Do something clever."))

    assert dependencies.store.all_records() == ()


# ---------------------------------------------------------------------------
# 10. No ready worker
# ---------------------------------------------------------------------------


def test_10_no_worker_yields_503():
    client, _ = build_app()  # no worker connected

    response = client.post("/api/chat", json=chat_body(request_id="req_no_worker"))

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "BLENDER_UNAVAILABLE"


def test_10_no_worker_preserves_the_mutation_identity_without_duplicating_it():
    """The job is kept, so a retry resolves to it instead of minting a new one."""
    client, dependencies = build_app()
    request_id = "req_kept_identity"

    assert client.post("/api/chat", json=chat_body(request_id=request_id)).status_code == 503

    records = dependencies.store.all_records()
    assert len(records) == 1, "the mutation identity is not lost"
    kept = records[0]
    assert kept.job_status == "queued"
    assert kept.request_id == request_id
    assert kept.idempotency_key == derive_idempotency_key(
        project_id=PROJECT_ID, request_id=request_id, operation_index=0
    )
    assert kept.offer_count == 0, "nothing was dispatched"

    # Retrying while still no worker must not create a second record.
    assert client.post("/api/chat", json=chat_body(request_id=request_id)).status_code == 503
    assert len(dependencies.store.all_records()) == 1


def test_10_retry_after_a_worker_appears_dispatches_the_same_job():
    client, dependencies = build_app()
    request_id = "req_retry_after_worker"

    assert client.post("/api/chat", json=chat_body(request_id=request_id)).status_code == 503
    first_job_id = dependencies.store.all_records()[0].job_id

    worker = connect_worker(dependencies)
    response = client.post("/api/chat", json=chat_body(request_id=request_id))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job_id"] == first_job_id, "the same mutation, not a new one"
    assert body["duplicate"] is True
    assert len(dependencies.store.all_records()) == 1
    assert len(worker.offered_jobs) == 1


def test_10_a_busy_worker_is_not_offered_a_second_job():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    first = client.post("/api/chat", json=chat_body(request_id="req_busy_1")).json()
    dependencies.gateway.handle_inbound(
        protocol.job_accepted(worker.worker_id, first["job_id"], PROJECT_ID)
    )

    response = client.post("/api/chat", json=chat_body(request_id="req_busy_2"))

    assert response.status_code == 503
    assert len(worker.offered_jobs) == 1, "the busy worker got no second job"


def test_10_a_worker_that_cannot_do_move_object_is_not_selected():
    """The capability check is real, even though the protocol makes it hard to hit.

    A worker cannot even ADVERTISE an unsupported job type: the capabilities schema
    constrains ``supported_job_types`` to the canonical JobType enum and requires at
    least one entry. So the state is forced directly onto the registered worker to
    prove the selector still refuses it.
    """
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    dependencies.manager.workers[worker.worker_id].capabilities[
        "supported_job_types"
    ] = []

    response = client.post("/api/chat", json=chat_body())

    assert response.status_code == 503
    assert worker.offers == []


def test_10_a_worker_cannot_advertise_an_unsupported_job_type():
    """Defence in depth: the protocol rejects the advertisement itself."""
    _, dependencies = build_app()
    reply = dependencies.gateway.handle_inbound(
        protocol.worker_hello(
            worker_id="worker_bogus",
            token=TOKEN,
            capabilities={
                "worker_version": "0.1.0",
                "blender_available": True,
                "supported_job_types": ["render_preview"],
                "max_concurrent_jobs": 1,
            },
        )
    )

    assert reply["type"] == protocol.WORKER_REJECTED
    assert "worker_bogus" not in dependencies.manager.workers


def test_10_a_lost_worker_is_not_selected():
    clock = FakeClock()
    client, dependencies = build_app(clock=clock)
    worker = connect_worker(dependencies)
    clock.advance(10_000)

    response = client.post("/api/chat", json=chat_body())

    assert response.status_code == 503
    assert worker.offers == []


# ---------------------------------------------------------------------------
# 11. A ready worker receives the job
# ---------------------------------------------------------------------------


def test_11_ready_worker_receives_the_canonical_job():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    body = client.post("/api/chat", json=chat_body(request_id="req_dispatch")).json()

    assert len(worker.offers) == 1
    offer = worker.offers[0]
    assert offer["type"] == protocol.JOB_OFFER
    assert offer["job_id"] == body["job_id"]
    assert offer["project_id"] == PROJECT_ID
    assert offer["job"]["job_id"] == body["job_id"]
    assert body["worker_id"] == WORKER_ID


def test_11_offer_is_pushed_without_the_worker_speaking_first():
    """Anti-deadlock invariant: dispatch must not require inbound traffic."""
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    inbound_after_registration = len(dependencies.gateway.transcript)

    client.post("/api/chat", json=chat_body())

    assert worker.offers, "an idle, silent worker was never offered the job"
    assert len(dependencies.gateway.transcript) == inbound_after_registration, (
        "the worker had to speak first, which is the deadlock this guards"
    )


def test_11_an_offer_for_a_disconnected_worker_is_queued_then_flushed():
    _, dependencies = build_app()
    worker = FakeWorker()
    # Register (so it is selectable) but do not attach a connection yet.
    dependencies.gateway.handle_inbound(
        protocol.worker_hello(worker.worker_id, TOKEN, worker.capabilities())
    )
    from studio_contracts.jobs import create_move_object_job
    from studio_types import ObjectRef, Vec3

    built = create_move_object_job(
        job_id="job_queued_1",
        project_id=PROJECT_ID,
        session_id="s",
        user_id="u",
        request_id="req_queued_1",
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
        created_at="2026-09-15T04:00:00Z",
    )
    from studio_contracts import to_wire

    dependencies.gateway.offer(worker.worker_id, to_wire(built.job))
    assert worker.received == [], "not connected yet"
    assert dependencies.gateway.pending_offer_count(worker.worker_id) == 1

    dependencies.gateway.attach(worker.worker_id, worker.sink)
    assert len(worker.offers) == 1, "queued offer flushed at registration"


def test_11_control_plane_refuses_to_offer_a_job_failing_the_canonical_contract():
    _, dependencies = build_app()
    worker = connect_worker(dependencies)

    with pytest.raises(protocol.ProtocolError):
        dependencies.gateway.offer(worker.worker_id, {"job_id": "nope"})


# ---------------------------------------------------------------------------
# 12. Job status endpoint
# ---------------------------------------------------------------------------


def test_12_job_status_reports_queued_then_claimed_then_running_then_succeeded():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    body = client.post("/api/chat", json=chat_body(request_id="req_lifecycle")).json()
    job_id = body["job_id"]

    # queued: offered, not yet accepted.
    assert client.get(job_path(job_id)).json()["job_status"] == "queued"

    # claimed: the worker explicitly accepted (the protocol calls this "accepted").
    dependencies.gateway.handle_inbound(
        protocol.job_accepted(worker.worker_id, job_id, PROJECT_ID)
    )
    assert client.get(job_path(job_id)).json()["job_status"] == "claimed"

    # running: the worker reported progress.
    dependencies.gateway.handle_inbound(
        protocol.job_progress(worker.worker_id, job_id, PROJECT_ID, "executing")
    )
    status = client.get(job_path(job_id)).json()
    assert status["job_status"] == "running"
    assert status["execution_phase"] == "executing"

    # succeeded.
    send_result(dependencies, worker, job_id)
    status = client.get(job_path(job_id)).json()
    assert status["job_status"] == "succeeded"


def test_12_job_status_reports_failed_with_a_structured_error():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    dependencies.gateway.handle_inbound(
        protocol.job_result(
            worker_id=worker.worker_id,
            job_id=job_id,
            project_id=PROJECT_ID,
            job_status="failed",
            error={"code": "OBJECT_NOT_FOUND", "message": "no such object"},
            execution_phase="failed",
        )
    )

    status = client.get(job_path(job_id)).json()
    assert status["job_status"] == "failed"
    assert status["error"]["code"] == "OBJECT_NOT_FOUND"
    assert status["chat"]["status"] == "error"


def test_12_terminal_status_embeds_a_canonical_chat_response():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]
    send_result(dependencies, worker, job_id, final_x=0.5)

    chat = client.get(job_path(job_id)).json()["chat"]

    assert chat["status"] == "success"
    assert chat["object_position"] == {"x": 0.5, "y": 0.0, "z": 0.0}
    # It really is the canonical contract, not an API-shaped variant.
    result = validate_against_schema(
        SCHEMA_FILES["ChatResponse"], {k: v for k, v in chat.items() if v is not None}
    )
    assert result.valid, result.violations


def test_12_non_terminal_status_has_no_chat_response_yet():
    client, dependencies = build_app()
    connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    assert client.get(job_path(job_id)).json()["chat"] is None


def test_12_unknown_job_id_is_404_and_starts_no_work():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    response = client.get(job_path("job_never_existed"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert worker.offers == [], "a status read must never dispatch work"


def test_12_job_lookup_is_project_scoped_and_has_no_unscoped_variant():
    """A job_id is an identifier, not a capability."""
    client, dependencies = build_app()
    connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    # The correct project succeeds.
    assert client.get(job_path(job_id, PROJECT_ID)).status_code == 200

    # There is no unscoped route at all.
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}?project_id={PROJECT_ID}").status_code == 404


def test_12_a_job_from_another_project_is_not_revealed():
    """Requirement: prefer 404 over exposing cross-project existence."""
    settings = Settings(
        environment="local",
        worker_token=TOKEN,
        # Two projects, both legitimately known to this control plane.
        project_ids=(PROJECT_ID, "proj_other"),
        development_user_id=DEV_USER_ID,
        heartbeat_interval_seconds=10.0,
    )
    client, dependencies = build_app(settings=settings)
    connect_worker(dependencies)

    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]
    assert dependencies.store.get(PROJECT_ID, job_id) is not None

    # proj_other is a REAL, authorized project — but not this job's project.
    response = client.get(job_path(job_id, "proj_other"))

    assert response.status_code == 404
    # The body must be indistinguishable from a job that never existed, so the
    # response cannot be used to probe another project for a known job_id.
    missing = client.get(job_path("job_never_existed", "proj_other"))
    assert missing.status_code == 404
    assert response.json() == missing.json()
    assert job_id not in response.text


def test_12_an_unknown_project_is_404_even_with_a_valid_job_id():
    client, dependencies = build_app()
    connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    response = client.get(job_path(job_id, "proj_does_not_exist"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert job_id not in response.text


@pytest.mark.parametrize(
    "hostile_project_id",
    ["../../etc/passwd", "..", ".", "~", "proj_seed/../proj_other"],
)
def test_12_a_path_shaped_project_id_cannot_reach_a_job(hostile_project_id):
    client, dependencies = build_app()
    connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    response = client.get(f"/api/projects/{hostile_project_id}/jobs/{job_id}")

    assert response.status_code == 404, response.text
    assert "Traceback" not in response.text


def test_12_the_store_itself_refuses_an_unscoped_lookup():
    """Isolation is structural, not a check a route could forget."""
    client, dependencies = build_app()
    connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    assert dependencies.store.get(PROJECT_ID, job_id) is not None
    assert dependencies.store.get("proj_other", job_id) is None

    # project_id is a required leading argument; it cannot be omitted.
    with pytest.raises(TypeError):
        dependencies.store.get(job_id)  # type: ignore[call-arg]


def test_12_status_url_from_the_submission_resolves_and_carries_the_project():
    client, dependencies = build_app()
    connect_worker(dependencies)
    body = client.post("/api/chat", json=chat_body()).json()

    assert body["status_url"] == (
        f"/api/projects/{PROJECT_ID}/jobs/{body['job_id']}"
    ), "the status URL must be project-scoped"
    assert client.get(body["status_url"]).status_code == 200


# ---------------------------------------------------------------------------
# 13. job_result updates status
# ---------------------------------------------------------------------------


def test_13_worker_job_result_updates_the_control_plane_record():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    send_result(dependencies, worker, job_id, final_x=0.5)

    status = client.get(job_path(job_id)).json()
    assert status["job_status"] == "succeeded"
    assert status["worker_id"] == WORKER_ID
    assert status["result"]["final_position_meters"]["x"] == 0.5
    assert status["result"]["verified"] is True
    assert status["error"] is None


def test_13_job_rejection_by_the_worker_is_recorded_as_failed():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    dependencies.gateway.handle_inbound(
        protocol.job_rejected(
            worker.worker_id, job_id, PROJECT_ID, "LOCK_CONFLICT", "worker is busy"
        )
    )

    status = client.get(job_path(job_id)).json()
    assert status["job_status"] == "failed"
    assert status["error"]["code"] == "LOCK_CONFLICT"


def test_13_a_terminal_job_never_regresses_to_running():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]
    send_result(dependencies, worker, job_id)

    # A late progress report arrives after completion.
    dependencies.gateway.handle_inbound(
        protocol.job_progress(worker.worker_id, job_id, PROJECT_ID, "executing")
    )

    assert client.get(job_path(job_id)).json()["job_status"] == "succeeded"


# ---------------------------------------------------------------------------
# 14. Duplicate / reconciled result does not create another job
# ---------------------------------------------------------------------------


def test_14_duplicate_result_does_not_create_another_job():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    send_result(dependencies, worker, job_id, job_status="succeeded")
    records_after_first = len(dependencies.store.all_records())

    # The worker reconnects and resends the stored result as `duplicate`.
    send_result(dependencies, worker, job_id, job_status="duplicate")

    assert len(dependencies.store.all_records()) == records_after_first == 1
    status = client.get(job_path(job_id)).json()
    assert status["job_status"] == "succeeded", "duplicate means it happened once"
    assert status["reconciled"] is True, "the resend is visible as reconciliation"


def test_14_reconciled_result_arriving_first_is_recorded_as_succeeded():
    """A `duplicate` may be the FIRST result the control plane ever sees."""
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    send_result(dependencies, worker, job_id, job_status="duplicate")

    status = client.get(job_path(job_id)).json()
    assert status["job_status"] == "succeeded"
    assert status["reconciled"] is True
    assert len(dependencies.store.all_records()) == 1


def test_14_repeated_identical_results_are_idempotent():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    job_id = client.post("/api/chat", json=chat_body()).json()["job_id"]

    for _ in range(4):
        send_result(dependencies, worker, job_id, job_status="duplicate")

    assert len(dependencies.store.all_records()) == 1
    assert client.get(job_path(job_id)).json()["job_status"] == "succeeded"


def test_14_retrying_a_completed_request_does_not_re_offer_the_job():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    request_id = "req_completed_then_retried"

    body = client.post("/api/chat", json=chat_body(request_id=request_id)).json()
    send_result(dependencies, worker, body["job_id"])
    offers_after_completion = len(worker.offers)

    retry = client.post("/api/chat", json=chat_body(request_id=request_id))

    assert retry.status_code == 202
    retried = retry.json()
    assert retried["job_id"] == body["job_id"]
    assert retried["duplicate"] is True
    assert retried["job_status"] == "succeeded"
    assert len(worker.offers) == offers_after_completion, "no second dispatch"
    assert len(dependencies.store.all_records()) == 1


def test_14_reusing_a_request_id_for_a_different_instruction_is_409():
    """Retry is idempotent; reuse for different content is a client error."""
    client, dependencies = build_app()
    connect_worker(dependencies)
    request_id = "req_reused_wrongly"

    assert client.post(
        "/api/chat", json=chat_body(request_id=request_id, message=MOVE_COMMAND)
    ).status_code == 202

    response = client.post(
        "/api/chat",
        json=chat_body(request_id=request_id, message="Move Cube 100 cm to the left."),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PRECONDITION_MISMATCH"
    assert len(dependencies.store.all_records()) == 1


# ---------------------------------------------------------------------------
# 15. API state loss must not cause re-execution
# ---------------------------------------------------------------------------


def test_15_api_restart_derives_the_same_mutation_identity():
    """Identity is DERIVED, so losing memory cannot mint a second mutation."""
    request_id = "req_survives_restart"

    client_a, deps_a = build_app()
    worker_a = connect_worker(deps_a)
    before = client_a.post("/api/chat", json=chat_body(request_id=request_id)).json()

    # A brand-new process: fresh in-memory store, nothing remembered.
    client_b, deps_b = build_app(store=InMemoryJobRecordStore())
    worker_b = connect_worker(deps_b)
    after = client_b.post("/api/chat", json=chat_body(request_id=request_id)).json()

    assert after["job_id"] == before["job_id"], "same job identity after a restart"
    assert (
        worker_b.offered_jobs[0]["idempotency_key"]
        == worker_a.offered_jobs[0]["idempotency_key"]
    ), "the mutation identity is derived, not stored"


def test_15_api_restart_does_not_tell_the_worker_to_rerun_a_completed_mutation():
    """The re-offer after a restart is recognisable as the SAME mutation.

    The control plane cannot know the job finished — it lost that memory. What it
    must guarantee is that it never asks for a NEW mutation: the offered job
    carries the identical job_id and idempotency_key, which is exactly what lets
    the worker's durable journal answer `duplicate` and mutate nothing (Task 6).
    """
    request_id = "req_no_rerun"

    client_a, deps_a = build_app()
    worker_a = connect_worker(deps_a)
    first = client_a.post("/api/chat", json=chat_body(request_id=request_id)).json()
    send_result(deps_a, worker_a, first["job_id"], job_status="succeeded")

    client_b, deps_b = build_app(store=InMemoryJobRecordStore())
    worker_b = connect_worker(deps_b)
    client_b.post("/api/chat", json=chat_body(request_id=request_id))

    reoffered = worker_b.offered_jobs[0]
    original = worker_a.offered_jobs[0]
    assert reoffered["job_id"] == original["job_id"]
    assert reoffered["idempotency_key"] == original["idempotency_key"]
    assert reoffered["origin"] == original["origin"]
    assert reoffered["payload"] == original["payload"]

    # And when the worker answers `duplicate`, the fresh control plane learns the
    # truth instead of insisting on a rerun.
    send_result(deps_b, worker_b, reoffered["job_id"], job_status="duplicate")
    status = client_b.get(job_path(reoffered["job_id"])).json()
    assert status["job_status"] == "succeeded"
    assert status["reconciled"] is True


def test_15_a_result_for_an_unknown_job_is_adopted_not_re_executed():
    """After state loss, a worker's reconciliation is LEARNED, not discarded."""
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    # No submission at all: this control plane has never seen this job.
    send_result(dependencies, worker, "job_from_a_previous_process", job_status="duplicate")

    status = client.get(job_path("job_from_a_previous_process")).json()
    assert status["job_status"] == "succeeded"
    assert status["reconciled"] is True
    assert worker.offers == [], "adoption must never dispatch work"

    record = dependencies.store.get(PROJECT_ID, "job_from_a_previous_process")
    assert record.adopted_after_state_loss is True


def test_15_progress_for_an_unknown_job_creates_nothing():
    """Only a terminal result is evidence; progress is not."""
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    dependencies.gateway.handle_inbound(
        protocol.job_progress(worker.worker_id, "job_ghost", PROJECT_ID, "executing")
    )

    assert dependencies.store.all_records() == ()
    assert client.get(job_path("job_ghost")).status_code == 404


# ---------------------------------------------------------------------------
# 16. Layering: the API imports no Blender
# ---------------------------------------------------------------------------


def test_16_api_package_never_imports_bpy():
    for path in API_SOURCE_DIR.rglob("*.py"):
        assert "bpy" not in _imports(path), f"{path} imports bpy"


def test_16_api_package_never_imports_a_blender_implementation():
    forbidden = ("bpy", "blender_worker", "blender_mcp")
    for path in API_SOURCE_DIR.rglob("*.py"):
        modules = _imports(path)
        for module in forbidden:
            assert module not in modules, f"{path} imports {module}"


def test_16_routes_do_not_import_blender_or_the_worker_by_name():
    for path in (API_SOURCE_DIR / "routes").rglob("*.py"):
        text = path.read_text("utf-8")
        for forbidden in ("import bpy", "blender_worker", "blender_mcp", "subprocess"):
            assert forbidden not in text, f"{path.name} references {forbidden}"


def test_16_api_never_executes_a_subprocess_or_arbitrary_code():
    for path in API_SOURCE_DIR.rglob("*.py"):
        modules = _imports(path)
        for forbidden in ("subprocess", "shutil"):
            assert forbidden not in modules, f"{path} imports {forbidden}"
        text = path.read_text("utf-8")
        for forbidden in ("eval(", "exec(", "os.system", "execute_python"):
            assert forbidden not in text, f"{path} uses {forbidden}"


def test_16_the_task_8_manager_is_still_framework_independent():
    """Task 9 must not have leaked FastAPI into the reusable decision layer."""
    modules = _imports(API_SOURCE_DIR / "worker_link" / "manager.py")
    for forbidden in ("fastapi", "starlette", "websockets", "socket", "asyncio"):
        assert forbidden not in modules, f"manager imports {forbidden}"


def test_16_the_gateway_is_framework_independent():
    modules = _imports(API_SOURCE_DIR / "worker_link" / "gateway.py")
    for forbidden in ("fastapi", "starlette", "websockets"):
        assert forbidden not in modules, f"gateway imports {forbidden}"


def test_16_only_the_websocket_route_touches_starlette_websockets():
    service_layer = (
        "chat_service.py",
        "job_records.py",
        "reconciliation.py",
        "worker_selection.py",
        "projects.py",
        "identity.py",
        "errors.py",
        "settings.py",
    )
    for name in service_layer:
        modules = _imports(API_SOURCE_DIR / name)
        for forbidden in ("fastapi", "starlette"):
            assert forbidden not in modules, f"{name} imports {forbidden}"


# ---------------------------------------------------------------------------
# 17. No filesystem path can be supplied through the API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_project_id",
    [
        "../../etc/passwd",
        "/home/christian/secret.blend",
        "..",
        ".",
        "proj_seed/../proj_other",
        "proj\x00seed",
        "~/projects/seed.blend",
        "C:\\projects\\seed.blend",
        "./proj_seed",
    ],
)
def test_17_a_path_shaped_project_id_is_refused(hostile_project_id):
    client, dependencies = build_app()
    worker = connect_worker(dependencies)

    response = client.post("/api/chat", json=chat_body(project_id=hostile_project_id))

    assert response.status_code in (404, 422), response.text
    assert dependencies.store.all_records() == ()
    assert worker.offers == []


@pytest.mark.parametrize(
    "path_field",
    ["blend_path", "project_path", "path", "file", "filepath", "blend_file"],
)
def test_17_a_path_field_cannot_be_added_to_the_request(path_field):
    client, dependencies = build_app()
    connect_worker(dependencies)

    response = client.post(
        "/api/chat", json={**chat_body(), path_field: "/home/christian/x.blend"}
    )

    assert response.status_code == 422, f"{path_field} was not rejected"
    assert dependencies.store.all_records() == ()


def test_17_the_chat_request_model_has_no_path_or_identity_fields():
    from studio_api.models import ChatRequestModel

    fields = set(ChatRequestModel.model_fields)
    assert fields == {
        "request_id",
        "project_id",
        "session_id",
        "message",
        "selected_object_id",
    }
    assert ChatRequestModel.model_config["extra"] == "forbid"


def test_17_the_chat_request_model_mirrors_the_canonical_schema():
    """The HTTP model and the canonical contract must not drift."""
    from studio_api.models import ChatRequestModel
    from studio_contracts import load_schema

    schema = load_schema(SCHEMA_FILES["ChatRequest"])
    assert set(ChatRequestModel.model_fields) == set(schema["properties"])
    assert schema["additionalProperties"] is False

    required_in_model = {
        name
        for name, field in ChatRequestModel.model_fields.items()
        if field.is_required()
    }
    assert required_in_model == set(schema["required"])


def test_17_the_control_plane_stores_no_project_path():
    _, dependencies = build_app()
    project = dependencies.projects.get(PROJECT_ID)

    assert set(project.snapshot()) == {"project_id", "display_name"}
    assert ".blend" not in json.dumps(project.snapshot())


def test_17_offered_jobs_never_carry_a_path():
    client, dependencies = build_app()
    worker = connect_worker(dependencies)
    client.post("/api/chat", json=chat_body())

    serialized = json.dumps(worker.offered_jobs[0])
    for leaked in (".blend", "/home", "/tmp", "path"):
        assert leaked not in serialized, f"the job leaked {leaked!r}"


# ---------------------------------------------------------------------------
# 18. Errors leak nothing
# ---------------------------------------------------------------------------


def test_18_an_unexpected_exception_does_not_leak_a_stack_trace():
    class ExplodingProvider:
        name = "exploding"
        version = "test"

        def interpret(self, request, context):
            raise RuntimeError(
                "internal detail: /home/christian/secret.blend and "
                f"token {TOKEN}"
            )

    client, _ = build_app(provider=ExplodingProvider())

    response = client.post("/api/chat", json=chat_body())

    assert response.status_code == 500
    text = response.text
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    for leaked in (
        "Traceback",
        "RuntimeError",
        "/home/christian",
        ".blend",
        TOKEN,
        "chat_service",
        "line ",
    ):
        assert leaked not in text, f"the 500 response leaked {leaked!r}"


@pytest.mark.parametrize(
    "request_body,expected_status",
    [
        (chat_body(project_id="proj_unknown"), 404),
        (chat_body(message="Do something vague."), 422),
        ({}, 422),
        (chat_body(request_id="req_503"), 503),  # no worker connected
    ],
)
def test_18_every_error_response_uses_the_same_structured_shape(
    request_body, expected_status
):
    client, _ = build_app()  # deliberately no worker

    response = client.post("/api/chat", json=request_body)

    assert response.status_code == expected_status, response.text
    payload = response.json()
    assert set(payload) <= {"error", "request_id"}
    assert set(payload["error"]) == {"code", "message"}
    assert isinstance(payload["error"]["code"], str)
    assert payload["error"]["message"].strip()
    # FastAPI's default error key must never appear.
    assert "detail" not in payload


def test_18_error_codes_are_canonical():
    from studio_contracts import ERROR_CODES

    client, _ = build_app()
    for body in (
        chat_body(project_id="proj_unknown"),
        chat_body(message="nonsense"),
        chat_body(),
    ):
        payload = client.post("/api/chat", json=body).json()
        assert payload["error"]["code"] in ERROR_CODES, payload


def test_18_no_error_response_reveals_a_filesystem_path_or_token():
    client, _ = build_app()
    for body in (
        chat_body(project_id="../../etc/passwd"),
        chat_body(message="Move Cube 50 parsecs to the right."),
        chat_body(),
    ):
        text = client.post("/api/chat", json=body).text
        for leaked in ("/home", "/etc", "/tmp", ".blend", TOKEN, "Traceback"):
            assert leaked not in text, f"error leaked {leaked!r}: {text}"


def test_18_a_404_on_an_unknown_route_does_not_leak_internals():
    client, _ = build_app()
    response = client.get("/api/not-a-real-endpoint")

    assert response.status_code == 404
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# Application factory, configuration and CORS
# ---------------------------------------------------------------------------


def test_create_app_registers_the_expected_routes():
    client, _ = build_app()
    paths = client.app.openapi()["paths"]

    assert "/health" in paths
    assert "/api/chat" in paths
    assert "/api/projects/{project_id}/jobs/{job_id}" in paths
    assert "/api/jobs/{job_id}" not in paths, "no unscoped job route may exist"
    assert "/api/workers" in paths
    assert "/api/projects/{project_id}/chat" in paths


def test_create_app_takes_explicit_dependencies_and_shares_no_global_state():
    client_a, deps_a = build_app()
    client_b, deps_b = build_app()

    assert deps_a.store is not deps_b.store
    assert deps_a.manager is not deps_b.manager

    connect_worker(deps_a)
    assert client_a.get("/health").json()["registered_workers"] == 1
    assert client_b.get("/health").json()["registered_workers"] == 0


def test_there_is_no_module_level_app_instance():
    """A factory is required, so configuration is validated per application."""
    import studio_api.app as app_module

    with pytest.raises(AttributeError):
        _ = app_module.app


def test_settings_come_from_the_environment_only_through_load_settings():
    settings = load_settings(
        {
            "STUDIO_API_ENVIRONMENT": "local",
            "STUDIO_API_PORT": "9001",
            "STUDIO_API_ALLOWED_ORIGINS": "http://localhost:3000, http://127.0.0.1:3000",
            "STUDIO_API_AGENT_PROVIDER": "rule_based",
            "STUDIO_API_DEVELOPMENT_USER_ID": "user_env",
            "STUDIO_API_PROJECT_IDS": "proj_seed,proj_two",
            "STUDIO_WORKER_TOKEN": "env-token",
        }
    )

    assert settings.port == 9001
    assert settings.allowed_origins == (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )
    assert settings.development_user_id == "user_env"
    assert settings.project_ids == ("proj_seed", "proj_two")
    assert settings.worker_token == "env-token"


def test_settings_defaults_are_local_and_conservative():
    settings = load_settings({})

    assert settings.environment == "local"
    assert settings.host == "127.0.0.1", "loopback by default"
    assert settings.allowed_origins == (), "no CORS until a browser client exists"
    assert settings.agent_provider == "rule_based"
    assert settings.project_ids == ("proj_seed",)


def test_settings_never_expose_the_token_in_repr():
    settings = load_settings({"STUDIO_WORKER_TOKEN": TOKEN})
    assert TOKEN not in repr(settings)


def test_no_cors_middleware_is_installed_when_no_origins_are_configured():
    client, _ = build_app()
    response = client.get(
        "/health", headers={"Origin": "https://evil.example.com"}
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in {
        k.lower() for k in response.headers
    }, "no origin may be granted access by default"


def test_cors_allows_only_explicitly_configured_origins():
    settings = Settings(
        environment="local",
        worker_token=TOKEN,
        project_ids=(PROJECT_ID,),
        allowed_origins=("http://localhost:3000",),
    )
    client, _ = build_app(settings=settings)

    allowed = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"

    denied = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert denied.headers.get("access-control-allow-origin") != (
        "https://evil.example.com"
    )


def test_wildcard_cors_is_refused_outside_local():
    settings = Settings(
        environment="production", worker_token="t", allowed_origins=("*",)
    )
    with pytest.raises(ConfigurationError):
        create_app(settings)


def test_a_missing_worker_token_is_refused_outside_local():
    with pytest.raises(ConfigurationError):
        create_app(Settings(environment="production", worker_token=""))


def test_project_scoped_chat_route_requires_a_matching_project():
    client, dependencies = build_app()
    connect_worker(dependencies)

    ok = client.post(
        f"/api/projects/{PROJECT_ID}/chat",
        json=chat_body(request_id="req_scoped_ok"),
    )
    assert ok.status_code == 202, ok.text

    mismatch = client.post(
        "/api/projects/proj_other/chat",
        json=chat_body(request_id="req_scoped_bad"),
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "VALIDATION_ERROR"


def test_chat_returns_202_not_200_so_the_flow_stays_asynchronous():
    """HTTP must not own a Blender mutation's lifetime."""
    client, dependencies = build_app()
    connect_worker(dependencies)

    response = client.post("/api/chat", json=chat_body())

    assert response.status_code == 202
    body = response.json()
    assert body["job_status"] == "queued", "not finished when the response returns"
    assert body["status_url"] == f"/api/projects/{PROJECT_ID}/jobs/{body['job_id']}"
