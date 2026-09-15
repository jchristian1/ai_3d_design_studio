"""The worker-side link client.

Spec 001, Task 8.

Owns the connection lifecycle and the job offer/accept/result conversation. It sits
between the transport and the Task 6 executor, and deliberately keeps them apart:

    control plane
        ^  WorkerTransport (outbound only)
    WorkerLinkClient      <- connection state, protocol, reconnect, reconciliation
        v
    WorkerExecutor        <- knows nothing about sockets
        v
    Blender

Connection state is tracked separately from job execution state. The network is
never the source of truth for whether a Blender mutation happened — the durable
journal is. That separation is what makes a dropped result channel harmless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from studio_contracts import SCHEMA_FILES, validate_against_schema
from studio_contracts import worker_protocol as protocol
from studio_contracts.jobs import validate_job

from .. import phases
from ..executor import WorkerExecutor, WorkerOutcome
from .identity import WorkerIdentity, describe_capabilities
from .transport import TransportClosed, TransportError, WorkerTransport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection state — separate from Task 6 execution phases
# ---------------------------------------------------------------------------

DISCONNECTED = "disconnected"
CONNECTING = "connecting"
AUTHENTICATING = "authenticating"
READY = "ready"
BUSY = "busy"
RECONNECTING = "reconnecting"

CONNECTION_STATES: tuple[str, ...] = (
    DISCONNECTED,
    CONNECTING,
    AUTHENTICATING,
    READY,
    BUSY,
    RECONNECTING,
)


@dataclass
class BackoffPolicy:
    """Bounded exponential backoff.

    Deterministic and configurable: no jitter, so tests can assert the exact
    sequence, and a hard ceiling so reconnects never spin in a tight loop.
    """

    initial_seconds: float = 0.5
    multiplier: float = 2.0
    max_seconds: float = 30.0
    max_attempts: Optional[int] = None

    def delay_for(self, attempt: int) -> float:
        """Delay before attempt N (1-based)."""
        if attempt <= 1:
            return self.initial_seconds
        delay = self.initial_seconds * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_seconds)

    def should_retry(self, attempt: int) -> bool:
        return self.max_attempts is None or attempt <= self.max_attempts


@dataclass
class LinkStats:
    """Observability counters. Contains no credentials."""

    connect_attempts: int = 0
    registrations: int = 0
    heartbeats_sent: int = 0
    jobs_offered: int = 0
    jobs_accepted: int = 0
    jobs_rejected: int = 0
    results_sent: int = 0
    results_undelivered: int = 0
    reconciled: int = 0
    protocol_errors: int = 0


@dataclass
class WorkerLinkClient:
    """Connects outbound, registers, then serves job offers."""

    identity: WorkerIdentity
    transport: WorkerTransport
    executor: WorkerExecutor
    #: Job types this worker will accept.
    supported_job_types: tuple[str, ...] = ("move_object",)
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    sleep: Callable[[float], None] = lambda seconds: None
    probe_blender: bool = True

    state: str = DISCONNECTED
    connection_id: Optional[str] = None
    heartbeat_interval_seconds: Optional[float] = None
    stats: LinkStats = field(default_factory=LinkStats)
    last_error: Optional[dict[str, Any]] = None

    # -- connection --------------------------------------------------------

    def connect(self) -> bool:
        """Connect outbound, authenticate and register.

        Returns True once the control plane has confirmed registration.
        """
        self.state = CONNECTING
        self.stats.connect_attempts += 1
        try:
            self.transport.connect()
        except TransportError as exc:
            self.state = DISCONNECTED
            self.last_error = {"code": "PROVIDER_UNAVAILABLE", "message": str(exc)}
            return False

        self.state = AUTHENTICATING
        hello = protocol.worker_hello(
            worker_id=self.identity.worker_id,
            token=self.identity.token,
            capabilities=describe_capabilities(
                self.identity,
                supported_job_types=self.supported_job_types,
                probe_blender=self.probe_blender,
            ),
        )
        # Logged redacted; the token never reaches a log line.
        logger.debug("worker hello: %s", protocol.redact(hello))
        try:
            self.transport.send(hello)
        except TransportError as exc:
            self.state = DISCONNECTED
            self.last_error = {"code": "PROVIDER_UNAVAILABLE", "message": str(exc)}
            return False
        return True

    def await_registration(self, timeout: Optional[float] = None) -> bool:
        """Process the control plane's answer to our hello."""
        message = self._receive(timeout)
        if message is None:
            return False

        if message["type"] == protocol.WORKER_REGISTERED:
            self.connection_id = message["connection_id"]
            self.heartbeat_interval_seconds = message["heartbeat_interval_seconds"]
            self.state = READY
            self.stats.registrations += 1
            return True

        if message["type"] == protocol.WORKER_REJECTED:
            self.last_error = message["error"]
            self.state = DISCONNECTED
            self.transport.close()
            return False

        self.last_error = {
            "code": "VALIDATION_ERROR",
            "message": f"unexpected message {message['type']!r} during registration",
        }
        return False

    def connect_and_register(self, timeout: Optional[float] = None) -> bool:
        return self.connect() and self.await_registration(timeout)

    def reconnect(self) -> bool:
        """Re-establish the link with bounded backoff, then reconcile.

        Every reconnect re-authenticates and re-registers from scratch: the
        control plane never trusts a resumed connection.
        """
        self.state = RECONNECTING
        attempt = 1
        while self.backoff.should_retry(attempt):
            self.sleep(self.backoff.delay_for(attempt))
            if self.connect_and_register():
                self.reconcile()
                return True
            attempt += 1
        self.state = DISCONNECTED
        return False

    def disconnect(self) -> None:
        self.transport.close()
        self.state = DISCONNECTED
        self.connection_id = None

    # -- heartbeats --------------------------------------------------------

    def send_heartbeat(self) -> bool:
        """Report liveness, distinguishing ready from busy."""
        if self.state not in (READY, BUSY):
            return False
        message = protocol.heartbeat(
            worker_id=self.identity.worker_id,
            worker_state=BUSY if self.state == BUSY else READY,
        )
        try:
            self.transport.send(message)
        except TransportError:
            self.state = DISCONNECTED
            return False
        self.stats.heartbeats_sent += 1
        return True

    # -- job handling ------------------------------------------------------

    def handle_next(self, timeout: Optional[float] = None) -> Optional[str]:
        """Process one inbound message. Returns its type, or None."""
        message = self._receive(timeout)
        if message is None:
            return None
        kind = message["type"]

        if kind == protocol.JOB_OFFER:
            self._handle_job_offer(message)
        elif kind == protocol.PING:
            self._safe_send(protocol.pong())
        elif kind == protocol.HEARTBEAT_ACK:
            pass
        return kind

    def _handle_job_offer(self, message: dict[str, Any]) -> None:
        """Validate, accept or reject, execute, then report.

        Nothing reaches WorkerExecutor until the offered job has passed the
        canonical Job contract and this worker's own capability checks. Network
        input is untrusted.
        """
        self.stats.jobs_offered += 1
        job = message["job"]
        job_id = message["job_id"]
        project_id = message["project_id"]

        rejection = self._rejection_reason(job, job_id, project_id)
        if rejection is not None:
            code, reason = rejection
            self.stats.jobs_rejected += 1
            self._safe_send(
                protocol.job_rejected(
                    self.identity.worker_id, job_id, project_id, code, reason
                )
            )
            return

        # Acceptance is explicit: socket delivery is not acceptance.
        self.stats.jobs_accepted += 1
        self._safe_send(
            protocol.job_accepted(self.identity.worker_id, job_id, project_id)
        )

        previous_state = self.state
        self.state = BUSY
        try:
            outcome = self.executor.execute(job)
        finally:
            self.state = previous_state if previous_state == BUSY else READY

        self._deliver_result(outcome)

    def _rejection_reason(
        self, job: Any, job_id: str, project_id: str
    ) -> Optional[tuple[str, str]]:
        if not isinstance(job, dict):
            return ("VALIDATION_ERROR", "offered job is not an object")

        validation = validate_job(job)
        if not validation.valid:
            return (
                "VALIDATION_ERROR",
                "offered job does not satisfy the canonical contract: "
                + "; ".join(e.message for e in validation.errors),
            )
        if job.get("job_id") != job_id or job.get("project_id") != project_id:
            return (
                "VALIDATION_ERROR",
                "job envelope does not match the job it carries",
            )
        if job.get("job_type") not in self.supported_job_types:
            return (
                "VALIDATION_ERROR",
                f"this worker does not support job_type {job.get('job_type')!r}",
            )
        if self.state == BUSY:
            return ("LOCK_CONFLICT", "worker is busy with another job")
        return None

    # -- result delivery and reconciliation --------------------------------

    def _deliver_result(self, outcome: WorkerOutcome) -> bool:
        """Send a result, recording locally whether it actually got through.

        A delivery failure is only a *reporting* problem. The mutation is already
        durable in the .blend and recorded in the journal, so the fix is to resend
        later — never to execute again.
        """
        status = (
            "duplicate"
            if outcome.duplicate and outcome.succeeded
            else ("succeeded" if outcome.succeeded else "failed")
        )
        message = protocol.job_result(
            worker_id=self.identity.worker_id,
            job_id=outcome.job_id,
            project_id=outcome.project_id,
            job_status=status,
            result=outcome.result,
            error=outcome.error,
            execution_phase=outcome.phase,
        )
        delivered = self._safe_send(message)
        if delivered:
            self.stats.results_sent += 1
        else:
            self.stats.results_undelivered += 1
        self._mark_delivery(outcome, delivered)
        return delivered

    def _mark_delivery(self, outcome: WorkerOutcome, delivered: bool) -> None:
        """Record delivery in the durable journal so reconnect can reconcile."""
        try:
            record = self.executor.store.load(outcome.project_id, outcome.job_id)
        except Exception:  # pragma: no cover - journal errors surface elsewhere
            return
        if record is None:
            return
        record.result_delivered = delivered
        self.executor.store.save(record)

    def reconcile(self) -> list[str]:
        """Resend results the control plane may never have received.

        Reads the durable journal, not the network. Re-sending a stored result
        cannot mutate Blender, so reconciliation is inherently safe to repeat.
        """
        resent: list[str] = []
        for record in self.executor.store.undelivered_results():
            status = "duplicate" if record.job_status == "succeeded" else "failed"
            message = protocol.job_result(
                worker_id=self.identity.worker_id,
                job_id=record.job_id,
                project_id=record.project_id,
                job_status=status,
                result=record.result,
                error=record.error,
                execution_phase=record.phase,
            )
            if self._safe_send(message):
                record.result_delivered = True
                self.executor.store.save(record)
                resent.append(record.job_id)
                self.stats.reconciled += 1
        return resent

    # -- plumbing ----------------------------------------------------------

    def _receive(self, timeout: Optional[float]) -> Optional[dict[str, Any]]:
        try:
            raw = self.transport.receive(timeout)
        except TransportClosed:
            self.state = DISCONNECTED
            return None
        except TransportError:
            self.state = DISCONNECTED
            return None
        if raw is None:
            return None
        try:
            return protocol.parse(raw)
        except protocol.ProtocolError as exc:
            # Untrusted input that fails the contract is dropped, not acted on.
            self.stats.protocol_errors += 1
            self.last_error = {"code": "VALIDATION_ERROR", "message": str(exc)}
            logger.warning("rejected malformed protocol message: %s", exc)
            return None

    def _safe_send(self, message: dict[str, Any]) -> bool:
        try:
            self.transport.send(message)
            return True
        except TransportError:
            self.state = DISCONNECTED
            return False


def is_busy_state(state: str) -> bool:
    return state == BUSY


__all__ = [
    "AUTHENTICATING",
    "BUSY",
    "CONNECTING",
    "CONNECTION_STATES",
    "DISCONNECTED",
    "READY",
    "RECONNECTING",
    "BackoffPolicy",
    "LinkStats",
    "WorkerLinkClient",
    "is_busy_state",
]
