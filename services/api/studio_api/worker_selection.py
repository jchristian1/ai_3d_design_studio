"""Choosing which worker gets a job.

Spec 001, Task 9.

Spec 001 has ONE workstation, so the selection rule is deliberately trivial: take
the first ready worker that can execute this job type. This is NOT a scheduler and
does not pretend to be one — there is no load balancing, no affinity, no queueing
across workers, no GPU-aware placement, and no fairness.

What matters is that the DECISION is isolated behind ``WorkerSelector``. Multi-
worker scheduling replaces this one class; ChatService, the routes, and the
protocol are untouched:

    ChatService  ->  WorkerSelector  ->  worker_id | structured reason
                     ^^^^^^^^^^^^^^
                     the only thing a scheduler has to replace

Eligibility is not re-derived here. ``WorkerConnectionManager.can_offer_to``
(Task 8) already owns the rules — registered, alive, not busy, supports the job
type — and this module only decides *which* of the eligible workers to pick.
Duplicating those checks would create a second, divergent source of truth for
whether a worker may be given work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from .worker_link.manager import WorkerConnectionManager

#: Returned when no worker could be chosen. Phrased for a user: they cannot act
#: on "no worker registered", but they can understand that the design machine is
#: offline.
NO_WORKER_MESSAGE = (
    "no Blender worker is currently available to perform this change; the "
    "design machine appears to be offline. Nothing has been modified — retry "
    "the same request once a worker is connected."
)


@dataclass(frozen=True)
class WorkerSelection:
    """The outcome of one selection attempt."""

    worker_id: Optional[str] = None
    #: Why no worker was chosen. Diagnostic, for logs and /api/workers, not for
    #: the end user.
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.worker_id is not None


@runtime_checkable
class WorkerSelector(Protocol):
    """Selects a worker for a canonical Job."""

    def select(self, job: Mapping[str, Any]) -> WorkerSelection: ...


@dataclass
class SingleReadyWorkerSelector:
    """Picks the first ready, compatible, registered worker.

    Deterministic: ``available_workers()`` is sorted, so repeated runs choose the
    same worker and tests do not depend on dictionary ordering.
    """

    manager: WorkerConnectionManager

    def select(self, job: Mapping[str, Any]) -> WorkerSelection:
        candidates = self.manager.available_workers()
        if not candidates:
            return WorkerSelection(reason="no worker is registered and alive")

        rejections: list[str] = []
        for worker_id in candidates:
            # Task 8 owns eligibility; this loop only chooses among the eligible.
            blocker = self.manager.can_offer_to(worker_id, job)
            if blocker is None:
                return WorkerSelection(worker_id=worker_id)
            rejections.append(f"{worker_id}: {blocker}")

        return WorkerSelection(reason="; ".join(rejections))


__all__ = [
    "NO_WORKER_MESSAGE",
    "SingleReadyWorkerSelector",
    "WorkerSelection",
    "WorkerSelector",
]
