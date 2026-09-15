"""Control-plane job records and their store.

Spec 001, Task 9.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This is the control plane's OBSERVATION of a job: what it submitted, which worker
it went to, and what the worker last reported. It is a projection for the browser,
not the durability mechanism.

    worker's durable journal   <- AUTHORITATIVE for whether Blender was mutated
    control-plane job record   <- reporting/coordination view (this module)

That ordering is what makes an in-memory implementation acceptable for Spec 001.
Losing this state loses *reporting*, never *durability*, and it can never cause a
second Blender mutation:

  - mutation identity is DERIVED, not stored: ``(project_id, request_id,
    operation_index)`` hashes to the same ``idempotency_key`` after a restart
    (Task 3), so a resubmitted request cannot become a new mutation;
  - the worker independently refuses to re-execute a completed ``job_id`` from its
    own journal (Task 6), so even a re-offer after a restart mutates nothing.

An empty store therefore means "I do not remember", never "it did not happen" —
which is why ``reconcile_result`` ADOPTS a result for an unknown job instead of
discarding it or creating new work.

Project isolation
-----------------
Records are keyed by ``(project_id, job_id)`` and ``get`` takes ``project_id`` as a
required, leading argument. A ``job_id`` is an identifier, NOT a capability: knowing
one grants no access without the matching project. There is deliberately no
unscoped lookup, so cross-project retrieval is impossible by construction rather
than prevented by a check somebody could forget.

Persistence boundary
--------------------
``JobRecordStore`` is a Protocol and ``InMemoryJobRecordStore`` is one
implementation. The Task 3 ``JobStore`` (Redis/PostgreSQL) implements the same
concepts later; ``submit`` already honours the atomic
"insert-or-return-existing keyed by (project_id, idempotency_key)" contract that
Protocol requires, so swapping it in does not change ChatService.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable

from studio_types import JOB_STATUSES, TERMINAL_JOB_STATUSES

#: How a worker's protocol-level job_status maps onto the canonical Job lifecycle
#: (Task 3: queued -> claimed -> running -> succeeded | failed).
#:
#: The worker's ``job_accepted`` is what the canonical lifecycle calls *claimed*:
#: a worker has taken ownership. "accepted" is the protocol word, "claimed" is the
#: contract word; they are the same event and the contract word is what is
#: reported, so the API never invents a status outside JOB_STATUSES.
CLAIMED_BY_ACCEPTANCE = "claimed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    """The control plane's record of one submitted job."""

    job_id: str
    project_id: str
    session_id: str
    user_id: str
    request_id: str
    operation_index: int
    job_type: str
    idempotency_key: str
    job_status: str
    created_at: str
    updated_at: str
    #: The canonical Job wire document that was offered. Kept so a queued job can
    #: be re-offered when a worker becomes available, without re-interpreting the
    #: user's language.
    job: dict[str, Any] = field(default_factory=dict)
    content_fingerprint: Optional[str] = None
    worker_id: Optional[str] = None
    #: Internal worker execution phase, for observability only. Never a contract.
    execution_phase: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    #: PreviewArtifact wire document reported by the worker (Task 10). Present only
    #: when a preview is durable. Contains no filesystem path by contract.
    preview: Optional[dict[str, Any]] = None
    #: Why the preview is unavailable, when the mutation itself succeeded. Reported
    #: separately so a degraded preview never reads as a failed design change.
    preview_error: Optional[dict[str, Any]] = None
    #: True when the terminal state arrived as a worker reconciliation (a resend
    #: of a result the control plane had not received) rather than a first report.
    reconciled: bool = False
    #: True when this record was adopted from a worker report for a job this
    #: process had never seen — the API-restart case.
    adopted_after_state_loss: bool = False
    offer_count: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.job_status in TERMINAL_JOB_STATUSES

    def touch(self) -> None:
        self.updated_at = utc_now()

    def snapshot(self) -> dict[str, Any]:
        """A safe view for an HTTP response.

        Excludes the full canonical Job document: it is internal, and echoing it
        would widen the response contract for no consumer.
        """
        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "operation_index": self.operation_index,
            "job_type": self.job_type,
            "job_status": self.job_status,
            "worker_id": self.worker_id,
            "execution_phase": self.execution_phase,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
            "preview": self.preview,
            "preview_error": self.preview_error,
            "reconciled": self.reconciled,
        }


@runtime_checkable
class JobRecordStore(Protocol):
    """The control plane's job-record boundary.

    Contract an implementation MUST honour (mirrors Task 3's ``JobStore``):

      1. ``submit`` is atomic and keyed by ``(project_id, idempotency_key)``.
         A second submission with the same key returns the EXISTING record and
         reports ``duplicate=True``; it never creates a second record.
      2. EVERY lookup is project-scoped. ``project_id`` is a required, leading
         parameter on ``get`` so it cannot be omitted by accident, and keys are
         never compared across projects. A caller that knows only a ``job_id``
         cannot reach a record: job identifiers are NOT a tenancy boundary.
      3. A duplicate submission must never produce a second Blender mutation.
    """

    def submit(self, record: JobRecord) -> tuple[JobRecord, bool]:
        """Insert, or return the existing record for the same mutation identity.

        Returns ``(record, duplicate)``.
        """
        ...

    def get(self, project_id: str, job_id: str) -> Optional[JobRecord]: ...

    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> Optional[JobRecord]: ...

    def save(self, record: JobRecord) -> JobRecord: ...

    def all_records(self) -> tuple[JobRecord, ...]: ...


class InMemoryJobRecordStore:
    """A process-local job-record store.

    Thread-safe because the worker WebSocket endpoint and HTTP handlers can touch
    it from different tasks/threads.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], JobRecord] = {}
        self._by_job_id: dict[tuple[str, str], JobRecord] = {}
        self._lock = threading.RLock()

    # -- JobRecordStore ----------------------------------------------------

    def submit(self, record: JobRecord) -> tuple[JobRecord, bool]:
        identity = (record.project_id, record.idempotency_key)
        with self._lock:
            existing = self._by_key.get(identity)
            if existing is not None:
                return existing, True
            self._by_key[identity] = record
            self._by_job_id[(record.project_id, record.job_id)] = record
            return record, False

    def get(self, project_id: str, job_id: str) -> Optional[JobRecord]:
        """Look up a job WITHIN a project.

        There is deliberately no unscoped variant. Records are keyed by
        ``(project_id, job_id)``, so a request carrying the right ``job_id`` but
        the wrong ``project_id`` simply misses — cross-project access is
        impossible by construction rather than by a check that could be forgotten.
        """
        with self._lock:
            return self._by_job_id.get((project_id, job_id))

    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> Optional[JobRecord]:
        with self._lock:
            return self._by_key.get((project_id, idempotency_key))

    def save(self, record: JobRecord) -> JobRecord:
        with self._lock:
            self._by_key[(record.project_id, record.idempotency_key)] = record
            self._by_job_id[(record.project_id, record.job_id)] = record
            return record

    def all_records(self) -> tuple[JobRecord, ...]:
        with self._lock:
            return tuple(self._by_job_id.values())


def record_from_job(job: dict[str, Any]) -> JobRecord:
    """Build a control-plane record from a canonical Job wire document.

    Every field is copied from the canonical Job, so the record cannot drift from
    the job that was actually created.
    """
    origin = job["origin"]
    now = utc_now()
    return JobRecord(
        job_id=job["job_id"],
        project_id=job["project_id"],
        session_id=job["session_id"],
        user_id=job["user_id"],
        request_id=origin["request_id"],
        operation_index=int(origin["operation_index"]),
        job_type=job["job_type"],
        idempotency_key=job["idempotency_key"],
        job_status=job.get("status", "queued"),
        created_at=job.get("created_at", now),
        updated_at=now,
        job=dict(job),
        content_fingerprint=job.get("content_fingerprint"),
    )


__all__ = [
    "CLAIMED_BY_ACCEPTANCE",
    "JOB_STATUSES",
    "InMemoryJobRecordStore",
    "JobRecord",
    "JobRecordStore",
    "record_from_job",
    "utc_now",
]
