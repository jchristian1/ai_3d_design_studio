"""Control-plane side of the worker link.

Spec 001, Task 8.

``WorkerConnectionManager`` decides which workers are allowed to exist and which
jobs they may be offered. It is transport-agnostic: it consumes and produces
protocol messages, so the same logic serves the local development server today and
a FastAPI/Starlette WebSocket endpoint in the API task.

    (transport: local WS server now, FastAPI endpoint later)
            v
    WorkerConnectionManager    <- authentication, registration, liveness, offers
            v
    canonical Jobs

Boundary note: the FastAPI application and its `/ws/workers` route binding belong
to the API task. This module is the part that does not depend on the web framework,
so that binding will be thin.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from studio_contracts import worker_protocol as protocol
from studio_contracts.jobs import validate_job

#: Liveness cadence handed to workers at registration. Configurable so tests never
#: depend on wall-clock timing.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
DEFAULT_LIVENESS_GRACE_MULTIPLIER = 3.0

# Worker liveness, as seen by the control plane.
HEALTHY = "healthy"
BUSY = "busy"
LOST = "lost"


@dataclass
class RegisteredWorker:
    """What the control plane keeps about a connected worker.

    Note what is absent: the token. It is verified at hello and then discarded, so
    it cannot leak through logs, an API response, or a serialized snapshot.
    """

    worker_id: str
    connection_id: str
    protocol_version: int
    capabilities: dict[str, Any]
    registered_at_seconds: float
    last_heartbeat_seconds: float
    worker_state: str = "ready"
    #: job_id currently assigned, when any.
    current_job_id: Optional[str] = None

    def snapshot(self) -> dict[str, Any]:
        """A safe, serializable view. Contains no credentials by construction."""
        return {
            "worker_id": self.worker_id,
            "connection_id": self.connection_id,
            "protocol_version": self.protocol_version,
            "capabilities": dict(self.capabilities),
            "worker_state": self.worker_state,
            "current_job_id": self.current_job_id,
            "last_heartbeat_seconds": self.last_heartbeat_seconds,
        }


@dataclass
class WorkerConnectionManager:
    """Authenticates workers and routes jobs and results."""

    #: Pre-shared expected token. Supplied by configuration/environment, never
    #: committed. Compared in constant time.
    expected_token: str = field(repr=False, default="")
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    liveness_grace_multiplier: float = DEFAULT_LIVENESS_GRACE_MULTIPLIER
    #: Injected clock (monotonic seconds) so liveness is testable without sleeping.
    now_seconds: Any = None

    workers: dict[str, RegisteredWorker] = field(default_factory=dict)
    #: Results received, in arrival order.
    results: list[dict[str, Any]] = field(default_factory=list)
    accepted_jobs: list[dict[str, Any]] = field(default_factory=list)
    rejected_jobs: list[dict[str, Any]] = field(default_factory=list)
    protocol_errors: list[str] = field(default_factory=list)

    def _clock(self) -> float:
        if self.now_seconds is None:
            import time

            return time.monotonic()
        return float(self.now_seconds())

    # -- inbound -----------------------------------------------------------

    def handle_message(self, raw: Any) -> Optional[dict[str, Any]]:
        """Handle one untrusted inbound message, returning a reply if any.

        Validation happens before anything is trusted or recorded.
        """
        try:
            message = protocol.parse(raw)
        except protocol.ProtocolError as exc:
            self.protocol_errors.append(str(exc))
            return protocol.worker_rejected(
                "VALIDATION_ERROR", f"malformed protocol message: {exc}"
            )

        kind = message["type"]
        if kind == protocol.WORKER_HELLO:
            return self._handle_hello(message)
        if kind == protocol.HEARTBEAT:
            return self._handle_heartbeat(message)
        if kind == protocol.JOB_ACCEPTED:
            return self._handle_job_accepted(message)
        if kind == protocol.JOB_REJECTED:
            return self._handle_job_rejected(message)
        if kind == protocol.JOB_RESULT:
            return self._handle_job_result(message)
        if kind == protocol.JOB_PROGRESS:
            return None
        if kind == protocol.PING:
            return protocol.pong()
        if kind == protocol.PONG:
            return None

        self.protocol_errors.append(f"unexpected message type {kind!r}")
        return protocol.worker_rejected(
            "VALIDATION_ERROR", f"unexpected message type {kind!r}"
        )

    def _handle_hello(self, message: dict[str, Any]) -> dict[str, Any]:
        """Authenticate, check the protocol version, then register.

        Order matters: an unsupported protocol version is reported clearly, and an
        unauthenticated worker is never recorded at all.
        """
        worker_id = message["worker_id"]

        if not protocol.protocol_version_supported(message["protocol_version"]):
            return protocol.worker_rejected(
                "VALIDATION_ERROR",
                f"unsupported protocol version {message['protocol_version']}; "
                f"this control plane speaks "
                f"{', '.join(str(v) for v in protocol.SUPPORTED_PROTOCOL_VERSIONS)}",
                worker_id=worker_id,
            )

        if not protocol.tokens_match(message.get("token"), self.expected_token):
            # Deliberately vague: no hint about which part was wrong.
            return protocol.worker_rejected(
                "VALIDATION_ERROR", "worker authentication failed", worker_id=worker_id
            )

        now = self._clock()
        connection_id = f"conn_{uuid.uuid4().hex[:12]}"
        # The token is NOT stored.
        self.workers[worker_id] = RegisteredWorker(
            worker_id=worker_id,
            connection_id=connection_id,
            protocol_version=message["protocol_version"],
            capabilities=dict(message["capabilities"]),
            registered_at_seconds=now,
            last_heartbeat_seconds=now,
        )
        return protocol.worker_registered(
            worker_id=worker_id,
            connection_id=connection_id,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
        )

    def _handle_heartbeat(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        worker = self.workers.get(message["worker_id"])
        if worker is None:
            return protocol.worker_rejected(
                "VALIDATION_ERROR", "worker is not registered"
            )
        worker.last_heartbeat_seconds = self._clock()
        worker.worker_state = message["worker_state"]
        return protocol.heartbeat_ack(worker.worker_id)

    def _handle_job_accepted(self, message: dict[str, Any]) -> None:
        self.accepted_jobs.append(message)
        worker = self.workers.get(message["worker_id"])
        if worker is not None:
            worker.current_job_id = message["job_id"]
            worker.worker_state = "busy"
        return None

    def _handle_job_rejected(self, message: dict[str, Any]) -> None:
        self.rejected_jobs.append(message)
        return None

    def _handle_job_result(self, message: dict[str, Any]) -> None:
        self.results.append(message)
        worker = self.workers.get(message["worker_id"])
        if worker is not None:
            worker.current_job_id = None
            worker.worker_state = "ready"
        return None

    # -- outbound ----------------------------------------------------------

    def build_job_offer(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """Wrap a canonical Job in an offer.

        The control plane only ever offers canonical Jobs: no natural language and
        no instructions reach the worker over this link.
        """
        validation = validate_job(job)
        if not validation.valid:
            raise protocol.ProtocolError(
                "refusing to offer a job that fails the canonical contract: "
                + "; ".join(e.message for e in validation.errors)
            )
        return protocol.job_offer(job)

    def can_offer_to(self, worker_id: str, job: Mapping[str, Any]) -> Optional[str]:
        """Why this worker cannot take this job, or None if it can."""
        worker = self.workers.get(worker_id)
        if worker is None:
            return "worker is not registered"
        if self.liveness_of(worker_id) == LOST:
            return "worker is not alive"
        if worker.worker_state == "busy" or worker.current_job_id is not None:
            return "worker is busy"
        supported = worker.capabilities.get("supported_job_types", [])
        if job.get("job_type") not in supported:
            return f"worker does not support job_type {job.get('job_type')!r}"
        return None

    # -- liveness ----------------------------------------------------------

    def liveness_of(self, worker_id: str) -> str:
        """Distinguish healthy, busy, and lost."""
        worker = self.workers.get(worker_id)
        if worker is None:
            return LOST
        deadline = self.heartbeat_interval_seconds * self.liveness_grace_multiplier
        if self._clock() - worker.last_heartbeat_seconds > deadline:
            return LOST
        return BUSY if worker.worker_state == "busy" else HEALTHY

    def available_workers(self) -> list[str]:
        return sorted(
            worker_id
            for worker_id in self.workers
            if self.liveness_of(worker_id) == HEALTHY
        )

    def forget(self, worker_id: str) -> None:
        """Drop a worker, e.g. after its connection closes."""
        self.workers.pop(worker_id, None)

    def snapshot(self) -> list[dict[str, Any]]:
        return [worker.snapshot() for worker in self.workers.values()]


__all__ = [
    "BUSY",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "HEALTHY",
    "LOST",
    "RegisteredWorker",
    "WorkerConnectionManager",
]
