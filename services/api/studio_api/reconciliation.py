"""Projecting worker reports onto control-plane job records.

Spec 001, Task 9.

The worker reports; the control plane records. Nothing here executes, retries, or
re-offers anything, which is precisely why it is safe:

    worker_protocol message  ->  JobReconciler  ->  JobRecord.job_status
    (job_accepted / job_progress / job_rejected / job_result)

Lifecycle mapping
-----------------
The worker's protocol vocabulary is translated into the canonical Job lifecycle
(Task 3: ``queued -> claimed -> running -> succeeded | failed``) rather than
inventing a parallel set of statuses:

| worker message                | canonical status |
|-------------------------------|------------------|
| ``job_accepted``              | ``claimed``      |
| ``job_progress``              | ``running``      |
| ``job_rejected``              | ``failed``       |
| ``job_result`` succeeded      | ``succeeded``    |
| ``job_result`` duplicate      | ``succeeded``    |
| ``job_result`` failed         | ``failed``       |

``duplicate`` is a *reporting* status, not an outcome: it means the worker already
had this job completed in its durable journal and resent the stored result without
touching Blender. It therefore maps to ``succeeded`` — the mutation did happen,
once — and sets ``reconciled`` so the distinction remains visible.

Two properties this module must preserve
----------------------------------------
1. **Reconciliation never creates work.** A resent result updates the existing
   record. If the record is unknown (the API restarted and lost its in-memory
   projection), the terminal state is ADOPTED into the store — never turned into a
   new job and never answered with a re-offer. The worker's journal is
   authoritative for what happened to Blender; the control plane is catching up.

2. **Terminal states do not regress.** A late ``job_progress`` or a second
   ``job_result`` cannot move a ``succeeded`` record back to ``running``, so the
   browser never sees a completed change appear to un-complete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from studio_contracts import worker_protocol as protocol
from studio_types import TERMINAL_JOB_STATUSES

from .job_records import JobRecord, JobRecordStore, utc_now

logger = logging.getLogger(__name__)

#: Worker-reported job_status -> canonical Job status.
CANONICAL_STATUS_BY_WORKER_STATUS: dict[str, str] = {
    "succeeded": "succeeded",
    # The mutation happened exactly once; this delivery merely reported it again.
    "duplicate": "succeeded",
    "failed": "failed",
}


@dataclass
class JobReconciler:
    """Applies worker reports to control-plane job records."""

    store: JobRecordStore

    # -- observer entry point ---------------------------------------------

    def observe(self, message: Mapping[str, Any]) -> None:
        """Handle one validated worker protocol message.

        Registered as a ``WorkerGateway`` observer, so it sees only messages that
        already satisfied the canonical protocol schema.
        """
        kind = message.get("type")
        if kind == protocol.JOB_ACCEPTED:
            self._apply_status(message, "claimed")
        elif kind == protocol.JOB_PROGRESS:
            self._apply_status(
                message, "running", execution_phase=message.get("execution_phase")
            )
        elif kind == protocol.JOB_REJECTED:
            self._apply_rejection(message)
        elif kind == protocol.JOB_RESULT:
            self._apply_result(message)

    # -- individual transitions -------------------------------------------

    def _locate(self, message: Mapping[str, Any]) -> Optional[JobRecord]:
        job_id = message.get("job_id")
        project_id = message.get("project_id")
        if not isinstance(job_id, str) or not isinstance(project_id, str):
            return None
        # Project-scoped: a worker report can only ever touch its own project's
        # record, so project isolation holds here too.
        return self.store.get(project_id, job_id)

    def _apply_status(
        self,
        message: Mapping[str, Any],
        status: str,
        execution_phase: Optional[str] = None,
    ) -> None:
        record = self._locate(message)
        if record is None:
            # Nothing to update, and deliberately nothing created: a progress or
            # acceptance report is not evidence of a completed mutation.
            return
        if record.is_terminal:
            # Never regress a finished job.
            return
        record.job_status = status
        record.worker_id = message.get("worker_id") or record.worker_id
        if execution_phase:
            record.execution_phase = execution_phase
        record.touch()
        self.store.save(record)

    def _apply_rejection(self, message: Mapping[str, Any]) -> None:
        record = self._locate(message)
        if record is None or record.is_terminal:
            return
        record.job_status = "failed"
        record.worker_id = message.get("worker_id") or record.worker_id
        error = message.get("error")
        record.error = dict(error) if isinstance(error, Mapping) else None
        record.touch()
        self.store.save(record)

    def _apply_result(self, message: Mapping[str, Any]) -> None:
        worker_status = message.get("job_status")
        canonical = CANONICAL_STATUS_BY_WORKER_STATUS.get(str(worker_status))
        if canonical is None:  # pragma: no cover - schema restricts the values
            logger.warning("unknown worker job_status %r", worker_status)
            return

        is_resend = worker_status == "duplicate"
        record = self._locate(message)

        if record is None:
            self._adopt_orphan_result(message, canonical, is_resend)
            return

        if record.is_terminal:
            # A duplicate or repeated result for an already-terminal job updates
            # bookkeeping only. It must not create a second record and must not
            # trigger any new work.
            if is_resend:
                record.reconciled = True
                record.touch()
                self.store.save(record)
            return

        record.job_status = canonical
        record.worker_id = message.get("worker_id") or record.worker_id
        record.execution_phase = message.get("execution_phase") or record.execution_phase
        result = message.get("result")
        record.result = dict(result) if isinstance(result, Mapping) else result
        error = message.get("error")
        record.error = dict(error) if isinstance(error, Mapping) else None
        record.reconciled = bool(is_resend)
        record.touch()
        self.store.save(record)

    def _adopt_orphan_result(
        self, message: Mapping[str, Any], canonical: str, is_resend: bool
    ) -> None:
        """Adopt a terminal result for a job this process never recorded.

        This is the API-restart case. The worker's durable journal is
        authoritative for whether Blender was mutated, so the correct response is
        to LEARN the outcome, not to discard it and certainly not to re-offer the
        job. Adoption creates a reporting record for a mutation that already
        happened; it starts no work.

        ``idempotency_key`` is intentionally left as a marker rather than
        fabricated: the derivation needs ``(project_id, request_id,
        operation_index)`` and a result message carries no ``request_id``. Guessing
        one would risk colliding with a real mutation identity, so the record is
        keyed only by its job_id.
        """
        job_id = message.get("job_id")
        project_id = message.get("project_id")
        if not isinstance(job_id, str) or not isinstance(project_id, str):
            return

        now = utc_now()
        result = message.get("result")
        error = message.get("error")
        record = JobRecord(
            job_id=job_id,
            project_id=project_id,
            session_id="",
            user_id="",
            request_id="",
            operation_index=0,
            job_type="move_object",
            idempotency_key=f"adopted:{project_id}:{job_id}",
            job_status=canonical,
            created_at=now,
            updated_at=now,
            job={},
            worker_id=message.get("worker_id"),
            execution_phase=message.get("execution_phase"),
            result=dict(result) if isinstance(result, Mapping) else result,
            error=dict(error) if isinstance(error, Mapping) else None,
            reconciled=bool(is_resend),
            adopted_after_state_loss=True,
        )
        self.store.save(record)
        logger.info(
            "adopted worker-reported terminal state for unknown job %s (%s)",
            job_id,
            canonical,
        )


__all__ = ["CANONICAL_STATUS_BY_WORKER_STATUS", "TERMINAL_JOB_STATUSES", "JobReconciler"]
