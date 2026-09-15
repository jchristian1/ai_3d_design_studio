"""The control plane's worker gateway.

Spec 001, Task 9.

``WorkerConnectionManager`` (Task 8) decides *whether* a worker may exist and
*whether* it may be offered a job. It holds no connections, because it must stay
framework-independent. This module adds the missing piece — knowing where a
connected worker actually is, so the control plane can PUSH to it — while itself
remaining free of any web framework:

    FastAPI /ws/workers route  (routes/worker_ws.py — the only async/Starlette part)
            v
    WorkerGateway              <- live connection registry + offer dispatch (here)
            v
    WorkerConnectionManager    <- authentication, liveness, eligibility (Task 8)
            v
    canonical Jobs

A "connection" is just a ``sink``: a callable that delivers one protocol message to
one worker. The gateway therefore works with an asyncio send queue, a synchronous
socket, or a list in a test, and nothing about it presumes FastAPI.

THE ANTI-DEADLOCK INVARIANT
---------------------------
Task 8 found and fixed a deadlock: an implementation that only flushed offers *in
reaction to* an inbound worker message would hang forever against an idle worker,
because the server waited to read while the worker waited to be offered work.

    Dispatch of a job to a connected ready worker MUST NOT depend on an inbound
    heartbeat or any other worker message.

``offer()`` therefore writes to the sink immediately and unconditionally, and the
FastAPI binding drains that sink from a task that is independent of its receive
loop. A regression test pins this.

Offers for a worker that is not connected yet are queued and flushed at
registration, so the two orderings (offer-then-connect, connect-then-offer) behave
identically.

Protocol reuse
--------------
No protocol logic is reimplemented. Message construction, parsing, validation, and
redaction all come from ``studio_contracts.worker_protocol``; registration,
authentication, liveness, and eligibility all come from the Task 8 manager. This
module only routes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from studio_contracts import worker_protocol as protocol

from .manager import WorkerConnectionManager

logger = logging.getLogger(__name__)

#: Delivers one protocol message to one worker. Returns False when the message
#: could not be handed to the transport.
MessageSink = Callable[[dict], bool]

#: Notified after the manager has handled a valid inbound message. Used to project
#: worker reports onto control-plane job records without the gateway knowing what
#: a job record is.
MessageObserver = Callable[[dict], None]


@dataclass
class WorkerGateway:
    """Routes messages between live worker connections and the manager."""

    manager: WorkerConnectionManager
    observers: list[MessageObserver] = field(default_factory=list)

    #: Live sinks by worker_id.
    _sinks: dict[str, MessageSink] = field(default_factory=dict)
    #: Offers built before a worker connected, flushed at registration.
    _pending_offers: dict[str, list[dict]] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.RLock)

    #: Redacted record of inbound traffic, for local debugging. Bounded so a
    #: long-running process cannot grow it without limit.
    transcript: list[dict] = field(default_factory=list)
    transcript_limit: int = 200

    # -- connections -------------------------------------------------------

    def attach(self, worker_id: str, sink: MessageSink) -> None:
        """Record a live connection and flush anything queued for it."""
        with self._lock:
            self._sinks[worker_id] = sink
            queued = self._pending_offers.pop(worker_id, [])
        for message in queued:
            sink(message)

    def detach(self, worker_id: str, sink: Optional[MessageSink] = None) -> None:
        """Drop a connection and deregister the worker.

        ``sink`` guards against a stale connection removing a newer one: after a
        reconnect the same worker_id has a different sink, and only the current
        one may be removed.
        """
        with self._lock:
            current = self._sinks.get(worker_id)
            if current is None:
                return
            if sink is not None and current is not sink:
                return
            del self._sinks[worker_id]
        # A worker with no connection cannot be offered work.
        self.manager.forget(worker_id)

    def is_connected(self, worker_id: str) -> bool:
        with self._lock:
            return worker_id in self._sinks

    def connected_worker_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._sinks))

    # -- inbound -----------------------------------------------------------

    def handle_inbound(self, raw: Any) -> Optional[dict]:
        """Handle one untrusted inbound message, returning the reply if any.

        Validation is the manager's (and therefore the canonical schema's) job.
        Observers only see messages that passed it.
        """
        parsed: Optional[dict] = None
        try:
            parsed = protocol.parse(raw)
        except protocol.ProtocolError:
            # Let the manager produce the canonical rejection; nothing is
            # observed or recorded from an invalid message.
            return self.manager.handle_message(raw)

        self._record(parsed)
        reply = self.manager.handle_message(parsed)
        self._notify(parsed)
        return reply

    def _record(self, message: Mapping[str, Any]) -> None:
        # Redacted BEFORE it is retained: the token never enters this list.
        if len(self.transcript) >= self.transcript_limit:
            del self.transcript[: len(self.transcript) - self.transcript_limit + 1]
        self.transcript.append(protocol.redact(message))

    def _notify(self, message: dict) -> None:
        for observe in tuple(self.observers):
            try:
                observe(message)
            except Exception:  # pragma: no cover - an observer must not break the link
                logger.exception("worker message observer failed")

    # -- outbound ----------------------------------------------------------

    def offer(self, worker_id: str, job: Mapping[str, Any]) -> dict:
        """Offer a canonical Job, pushing immediately when the worker is live.

        Raises ``protocol.ProtocolError`` when the job fails the canonical
        contract — the control plane refuses to offer an invalid job rather than
        letting the worker discover the problem.
        """
        offer = self.manager.build_job_offer(job)
        with self._lock:
            sink = self._sinks.get(worker_id)
            if sink is None:
                self._pending_offers.setdefault(worker_id, []).append(offer)
                return offer
        # Pushed unconditionally: never contingent on inbound traffic.
        if not sink(offer):
            with self._lock:
                self._pending_offers.setdefault(worker_id, []).append(offer)
        return offer

    def pending_offer_count(self, worker_id: str) -> int:
        with self._lock:
            return len(self._pending_offers.get(worker_id, ()))

    # -- reporting ---------------------------------------------------------

    def worker_snapshots(self) -> list[dict[str, Any]]:
        """Safe per-worker views, enriched with liveness and connection state.

        Built from ``RegisteredWorker.snapshot()``, which cannot contain a token
        because the manager never stores one.
        """
        snapshots: list[dict[str, Any]] = []
        for snapshot in self.manager.snapshot():
            worker_id = snapshot["worker_id"]
            snapshots.append(
                {
                    **snapshot,
                    "liveness": self.manager.liveness_of(worker_id),
                    "connected": self.is_connected(worker_id),
                    "pending_offers": self.pending_offer_count(worker_id),
                }
            )
        return snapshots


__all__ = ["MessageObserver", "MessageSink", "WorkerGateway"]
