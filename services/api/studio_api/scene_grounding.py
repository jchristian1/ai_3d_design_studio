"""Making sure the agent is never asked to design blind.

Requirement 1 says the agent reasons about the AUTHORITATIVE scene. The cached scene
arrives as a side effect of finishing a job, which is fine forever after — but it left a
hole at the start: on a brand-new project the cache is empty, the prompt honestly said
"the Blender project has not been read yet", and the model did the human thing. It
replied "let me check the scene first", which is an ANSWER: it changes nothing, so the
turn ended and the user was left waiting for work that was never dispatched.

The fix belongs here rather than in the prompt. Reading the project is the platform's
job, not something the model should have to ask for:

    first message about a project
        -> no cached scene?
        -> dispatch a one-step inspect_scene plan and WAIT for it
        -> build the agent's context with the real scene in it

Bounded and best-effort. If Blender is offline or slow the turn proceeds with no scene,
because "the design machine is not connected" is a better answer than a hung request.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from studio_contracts import worker_protocol as protocol
from studio_contracts.jobs import create_capability_job, to_job_wire
from studio_types import CapabilityOperation

from .job_records import record_from_job
from .storage.repositories import StudioRepositories

_log = logging.getLogger(__name__)

#: How long a first turn may wait for Blender to report its scene. A headless read
#: through the official MCP takes a couple of seconds; this is generous enough for a
#: cold start and short enough that a stuck worker does not hold an HTTP request open.
DEFAULT_TIMEOUT_SECONDS = 30.0

POLL_SECONDS = 0.05


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SceneGrounder:
    """Reads the project once, so the first turn is grounded like every later one."""

    repositories: StudioRepositories
    store: Any  # JobRecordStore
    selector: Any  # WorkerSelector
    offer_job: Callable[[str, dict], Any]
    clock: Callable[[], str] = field(default=_utc_now)
    #: How long ``ensure`` may block. Zero means "dispatch the read and return": the
    #: worker still reports and the cache still fills, the CALLER just does not wait.
    #: Production waits briefly so a user's very first message is not blind; test
    #: harnesses that drive the worker by hand set this to zero.
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    #: Projects to read as soon as a worker connects. Doing it then, rather than inside
    #: the first chat request, is what keeps that request fast.
    project_ids: tuple[str, ...] = ()
    #: Runs the ahead-of-time read off the WebSocket thread.
    spawn: Callable[[Callable[[], None]], None] = field(
        default=lambda work: threading.Thread(target=work, daemon=True).start()
    )
    #: Projects whose read has been dispatched and not yet reported. A first turn that
    #: arrives during one waits for it rather than asking for a second.
    _in_flight: set = field(default_factory=set)
    _lock: Any = field(default_factory=threading.RLock)

    def ensure(self, project_id: str, *, session_id: str, user_id: str) -> bool:
        """Ensure a cached scene exists. Returns whether one is now available.

        Never raises: grounding is an improvement to the turn, not a precondition for
        answering. A failure degrades to "no scene", which the prompt states plainly.
        """
        if self.repositories.scenes.get(project_id) is not None:
            return True
        try:
            if self._already_reading(project_id):
                # A read started at worker registration is already on its way. Waiting
                # for THAT is both faster and correct: a second read would be refused
                # anyway, because the worker is busy performing the first one.
                return self._await_scene(project_id)
            return self._read(project_id, session_id=session_id, user_id=user_id)
        except Exception as error:  # pragma: no cover - defensive
            _log.warning("could not ground the scene for %s: %s", project_id, error)
            return False

    def _already_reading(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._in_flight

    # -- ahead of time -----------------------------------------------------
    def observe(self, message: Any) -> None:
        """Read the configured projects when a worker registers.

        Registered as a gateway observer. The worker connects at startup, long before
        anyone types, so in practice the scene is already cached by the time it is
        needed and no request ever waits for Blender.
        """
        try:
            if not isinstance(message, Mapping):
                return
            if message.get("type") != protocol.WORKER_HELLO:
                return
            for project_id in self.project_ids:
                if self.repositories.scenes.get(project_id) is None:
                    self._spawn_read(project_id)
        except Exception as error:  # pragma: no cover - must never break the link
            _log.warning("could not schedule a scene read: %s", error)

    def _spawn_read(self, project_id: str) -> None:
        def work() -> None:
            try:
                self._read(
                    project_id,
                    session_id="session_startup",
                    user_id="user_system",
                    wait=False,
                )
            except Exception as error:  # pragma: no cover - background best effort
                _log.warning("startup scene read for %s failed: %s", project_id, error)

        self.spawn(work)

    # -- the read ----------------------------------------------------------
    def _read(
        self, project_id: str, *, session_id: str, user_id: str, wait: bool = True
    ) -> bool:
        # A fresh id per attempt: this is a READ, and two reads are not the same
        # mutation identity. Reusing one would collide on the idempotency key.
        request_id = f"scene_{secrets.token_hex(6)}"
        created = create_capability_job(
            job_id=f"job_{request_id}_0",
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            request_id=request_id,
            operations=[
                CapabilityOperation(
                    operation_index=0,
                    capability="inspect_scene",
                    label="Reading the project",
                    arguments={},
                )
            ],
            created_at=self.clock(),
            summary="Reading the project.",
        )
        if not created.ok or created.job is None:
            _log.warning(
                "could not build a scene read for %s: %s", project_id, created.errors
            )
            return False

        job_wire = to_job_wire(created.job)
        record, duplicate = self.store.submit(record_from_job(job_wire))
        if duplicate and (record.is_terminal or record.worker_id):
            return self.repositories.scenes.get(project_id) is not None

        selection = self.selector.select(job_wire)
        if not selection.ok:
            # No worker: the turn will say so, which is the honest answer.
            return False

        try:
            self.offer_job(selection.worker_id, job_wire)
        except Exception as error:
            _log.warning("could not dispatch a scene read for %s: %s", project_id, error)
            return False

        record.worker_id = selection.worker_id
        record.offer_count += 1
        record.touch()
        self.store.save(record)
        with self._lock:
            self._in_flight.add(project_id)

        if not wait or self.timeout_seconds <= 0:
            # Dispatched. The worker will report and the cache will fill; the caller
            # simply is not held open for it.
            return False
        return self._await_scene(project_id)

    def _await_scene(self, project_id: str) -> bool:
        """Wait for the worker's report to reach the scene cache."""
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self.repositories.scenes.get(project_id) is not None:
                with self._lock:
                    self._in_flight.discard(project_id)
                return True
            time.sleep(POLL_SECONDS)
        _log.warning("the scene read for %s did not report in time", project_id)
        with self._lock:
            self._in_flight.discard(project_id)
        return False


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "SceneGrounder"]
