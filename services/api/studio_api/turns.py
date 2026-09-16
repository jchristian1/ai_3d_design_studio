"""Running a design turn without holding an HTTP request open.

A turn asks a real model to think. `codex exec` with gpt-6-astra takes tens of seconds for
a question and minutes for a floor plan, and a browser will not wait that long — ours
aborts at 15 seconds, and any proxy in front of it would have its own opinion. Holding the
request open also meant a page reload threw the answer away, and an abandoned request left
the model's work orphaned.

So the turn is started and then polled:

    POST /api/projects/{id}/design-chat        -> 202 {turn_id, state: "thinking"}
    GET  /api/projects/{id}/design-chat/{tid}  -> {state: "thinking"}
                                               -> {state: "ready", ...the turn}
                                               -> the same error body as before

The work runs on a worker thread. Nothing about the turn itself changed — the same
`DesignChatService.submit` runs, in the same order, with the same guarantees. What changed
is only who waits.

Two properties this module is responsible for:

* **A model call is never duplicated.** Asking twice with one request id — a retry, a
  double click, a reload — joins the turn already running instead of paying for a second
  one. Astra runs on a finite ChatGPT allowance; duplicating calls spends it.
* **A turn is never lost silently.** A crashed worker thread becomes a failed turn with a
  real message, not a poll that says "thinking" forever.
"""

from __future__ import annotations

import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import errors
from .design_chat import DesignChatRequest, DesignTurnResult
from .identity import TrustedIdentity

_log = logging.getLogger(__name__)

THINKING = "thinking"
READY = "ready"
FAILED = "failed"

#: Finished turns are kept so a slow poll still finds its answer, and dropped after that
#: so a long session does not grow without bound. The transcript is the durable record;
#: this is only the delivery buffer.
DEFAULT_HISTORY = 64

#: One turn at a time per studio. This is a single-user studio and a model call is
#: expensive; serialising keeps the allowance predictable and the scene grounding sane.
DEFAULT_WORKERS = 2


def new_turn_id() -> str:
    return f"turn_{secrets.token_hex(6)}"


@dataclass
class TurnState:
    """Where one turn has got to."""

    turn_id: str
    project_id: str
    request_id: str
    state: str = THINKING
    result: Optional[DesignTurnResult] = None
    #: Set when the turn could not be completed at all (a crash, not a refusal).
    error: Optional[errors.ControlPlaneFailure] = None

    @property
    def finished(self) -> bool:
        return self.state in (READY, FAILED)


@dataclass
class TurnRunner:
    """Starts design turns in the background and reports on them."""

    submit: Callable[[DesignChatRequest, TrustedIdentity], DesignTurnResult]
    decide: Callable[..., DesignTurnResult]
    history: int = DEFAULT_HISTORY
    max_workers: int = DEFAULT_WORKERS

    _pool: ThreadPoolExecutor = field(init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _turns: dict[str, TurnState] = field(default_factory=dict, init=False)
    #: request_id -> turn_id, so a retry joins the turn already running.
    _by_request: dict[str, str] = field(default_factory=dict, init=False)
    _order: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="design-turn"
        )

    # -- starting ----------------------------------------------------------

    def start(
        self, request: DesignChatRequest, identity: TrustedIdentity
    ) -> TurnState:
        """Begin a turn, or join the one this request already has IN FLIGHT.

        Joining is scoped to a turn that has not finished yet, which is what a double
        click or a retry-while-thinking looks like: the caller wants that answer, and a
        second model call would spend the allowance twice for it.

        Once a turn has finished, the same request id runs again and goes through the
        service, so its own rules apply — job idempotency reports a duplicate, and a
        decision that was already made is refused. Joining a FINISHED turn would hide
        both of those behind a replayed answer.
        """
        key = f"{request.project_id}:{request.request_id}"
        with self._lock:
            joined = self._in_flight(key)
            if joined is not None:
                return joined

            state = TurnState(
                turn_id=new_turn_id(),
                project_id=request.project_id,
                request_id=request.request_id,
            )
            self._remember(state, key)

        self._pool.submit(self._run, state, lambda: self.submit(request, identity))
        return state

    def start_decision(
        self,
        project_id: str,
        approval_id: str,
        *,
        approved: bool,
        identity: TrustedIdentity,
        session_id: Optional[str] = None,
    ) -> TurnState:
        """Begin an approval decision. Running it can involve Blender, so it polls too."""
        key = f"{project_id}:approval:{approval_id}"
        with self._lock:
            joined = self._in_flight(key)
            if joined is not None:
                return joined
            state = TurnState(
                turn_id=new_turn_id(), project_id=project_id, request_id=approval_id
            )
            self._remember(state, key)

        self._pool.submit(
            self._run,
            state,
            lambda: self.decide(
                project_id,
                approval_id,
                approved=approved,
                identity=identity,
                session_id=session_id,
            ),
        )
        return state

    def _in_flight(self, key: str) -> Optional[TurnState]:
        """The unfinished turn for this key, if there is one. Caller holds the lock."""
        turn_id = self._by_request.get(key)
        if not turn_id:
            return None
        state = self._turns.get(turn_id)
        if state is None or state.finished:
            return None
        return state

    def _remember(self, state: TurnState, key: str) -> None:
        self._turns[state.turn_id] = state
        self._by_request[key] = state.turn_id
        self._order.append(state.turn_id)
        while len(self._order) > self.history:
            oldest = self._order.pop(0)
            dropped = self._turns.pop(oldest, None)
            if dropped is None:
                continue
            for request_key, turn_id in list(self._by_request.items()):
                if turn_id == oldest:
                    self._by_request.pop(request_key, None)

    def _run(self, state: TurnState, work: Callable[[], DesignTurnResult]) -> None:
        try:
            result = work()
        except Exception as error:  # a crash must not become a silent "thinking"
            _log.exception("design turn %s failed: %s", state.turn_id, error)
            with self._lock:
                state.state = FAILED
                state.error = errors.failure(
                    errors.INTERNAL,
                    "INTERNAL_ERROR",
                    "Something went wrong while working on that. Nothing was changed.",
                )
            return
        with self._lock:
            state.result = result
            state.state = READY

    # -- reading -----------------------------------------------------------

    def get(self, project_id: str, turn_id: str) -> Optional[TurnState]:
        with self._lock:
            state = self._turns.get(turn_id)
        if state is None or state.project_id != project_id:
            # Project-scoped on purpose: a turn id from another project is unknown here,
            # exactly like an artifact id from another project.
            return None
        return state

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def thinking_snapshot(state: TurnState, *, poll_url: str) -> dict[str, Any]:
    """What the browser gets while Astra is still working."""
    return {
        "turn_id": state.turn_id,
        "state": state.state,
        "request_id": state.request_id,
        "project_id": state.project_id,
        "poll_url": poll_url,
        # A plain sentence, because it is shown as the reply while the user waits.
        "message": "Astra is thinking…",
    }


__all__ = [
    "DEFAULT_HISTORY",
    "FAILED",
    "READY",
    "THINKING",
    "TurnRunner",
    "TurnState",
    "new_turn_id",
    "thinking_snapshot",
]
