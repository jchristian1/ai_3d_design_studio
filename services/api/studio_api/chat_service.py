"""The control-plane chat orchestrator.

Spec 001, Task 9.

This is where the vertical slice is actually assembled. The FastAPI route does
almost nothing: it parses HTTP, calls ``submit_chat``, and renders the structured
result. All sequencing lives here, so the workflow is testable without a web
framework and reusable from a future WebSocket or queue entry point.

    ChatRequest + TrustedIdentity
            v
    1. authorize the project        (ProjectRegistry — logical ids only)
    2. build AgentContext           (identity from the server, never the body)
    3. provider.interpret()         (AgentProvider — the replaceable AI boundary)
            v  AgentPlan
    4. JobFactory.build()           (Task 3 canonical Job + derived idempotency)
            v  canonical Job
    5. store.submit()               (insert-or-return-existing, atomic)
    6. selector.select()            (WorkerSelector boundary)
    7. gateway.offer()              (WorkerConnectionManager builds the offer)
            v
    ChatSubmission

Provider access
---------------
The provider arrives as an injected ``AgentProvider``. No concrete provider class is
imported or named anywhere in this module, so changing which language engine
interprets requests is a configuration change in the dependency container.

Asynchrony
----------
``submit_chat`` returns as soon as the job has been offered. It does not wait for
Blender, because Blender is slow and an HTTP request must not own a mutation's
lifetime — the browser polls ``GET /api/projects/{project_id}/jobs/{job_id}``.
``await_terminal`` exists for tests and local scripting; it is a bounded convenience
over the same records, not a second architecture.

Idempotency
-----------
Nothing is hashed here. Mutation identity is derived by Task 3 from
``(project_id, request_id, operation_index)``, so resubmitting a ``request_id``
returns the SAME job instead of creating a second mutation, while a new
``request_id`` with identical text legitimately executes again.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from studio_agent import AgentContext, JobFactory
from studio_agent.provider import AgentProvider, AgentProviderError
from studio_contracts import ChatError, ChatRequest, ChatResponse, to_wire
from studio_contracts import worker_protocol as protocol
from studio_types import Vec3

from . import errors
from .identity import TrustedIdentity
from .job_records import JobRecord, JobRecordStore, record_from_job
from .projects import ProjectRegistry
from .worker_selection import NO_WORKER_MESSAGE, WorkerSelector

logger = logging.getLogger(__name__)

#: Spec 001 interprets one instruction into one operation. A plan with more
#: operations is refused rather than partially executed: partial application of a
#: multi-step design change would leave the project in a state the user never
#: asked for, and cross-operation rollback does not exist yet.
MAX_OPERATIONS_PER_REQUEST = 1

MESSAGES: dict[str, str] = {
    "UNKNOWN_PROJECT": (
        "that project does not exist or is not available to you; no change was "
        "made"
    ),
    "MULTI_OPERATION": (
        "this request resolves to more than one change, which is not supported "
        "yet; nothing was modified"
    ),
    "NO_PLAN": "the request could not be turned into a design change",
    "OFFER_FAILED": (
        "the change could not be handed to the design machine; nothing was "
        "modified"
    ),
    "PROVIDER_MISCONFIGURED": (
        "the design agent is not available; nothing was modified"
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ChatSubmission:
    """The structured outcome of one chat submission.

    Success does NOT mean Blender finished — it means the change was interpreted,
    recorded with a stable identity, and handed to a worker. ``job_status`` carries
    the current canonical lifecycle position.
    """

    ok: bool
    request_id: str
    project_id: str
    session_id: str
    summary: str
    job_id: Optional[str] = None
    job_status: Optional[str] = None
    worker_id: Optional[str] = None
    #: True when this submission resolved to an existing job (a retry of the same
    #: request_id) rather than creating a new one.
    duplicate: bool = False
    provider_name: Optional[str] = None
    failure: Optional[errors.ControlPlaneFailure] = None

    @property
    def http_status(self) -> int:
        if self.failure is not None:
            return self.failure.http_status
        # 202: interpreted and accepted for execution, not yet complete.
        return 202


@dataclass
class ChatService:
    """Orchestrates one chat request into an offered canonical Job."""

    provider: AgentProvider
    projects: ProjectRegistry
    store: JobRecordStore
    selector: WorkerSelector
    #: Offers a job to a worker. ``WorkerGateway.offer`` in the application;
    #: a fake in unit tests.
    offer_job: Callable[[str, dict[str, Any]], Any]
    job_factory: JobFactory = None  # type: ignore[assignment]
    #: Injected so tests are reproducible and no clock is read in business logic.
    clock: Callable[[], str] = _utc_now

    def __post_init__(self) -> None:
        if self.job_factory is None:
            self.job_factory = JobFactory()

    # -- public entry point ------------------------------------------------

    def submit_chat(
        self, request: ChatRequest, identity: TrustedIdentity
    ) -> ChatSubmission:
        """Interpret, record, and dispatch one request.

        Never raises for expected failures: every outcome is a ChatSubmission, so
        the route maps data to HTTP instead of catching exceptions.
        """
        # ---- 1. authorize the project --------------------------------
        project = self.projects.get(request.project_id)
        if project is None:
            return self._failed(
                request,
                errors.failure(
                    errors.UNKNOWN_PROJECT,
                    "VALIDATION_ERROR",
                    MESSAGES["UNKNOWN_PROJECT"],
                ),
            )

        # ---- 2. trusted context --------------------------------------
        # user_id comes from the identity the server established, never from the
        # request body — which cannot carry one at all.
        context = AgentContext(
            user_id=identity.user_id,
            project_id=project.project_id,
            session_id=request.session_id,
            selected_object_id=request.selected_object_id,
        )

        # ---- 3. interpret through the AgentProvider boundary ---------
        try:
            result = self.provider.interpret(request, context)
        except AgentProviderError as exc:
            # A configuration mistake, not an interpretation outcome.
            logger.error("agent provider is misconfigured: %s", exc)
            return self._failed(
                request,
                errors.failure(
                    errors.PROVIDER_UNAVAILABLE,
                    "PROVIDER_UNAVAILABLE",
                    MESSAGES["PROVIDER_MISCONFIGURED"],
                ),
            )

        provider_name = getattr(result.metadata, "provider_name", None)

        if not result.ok or result.plan is None:
            error = result.error or ChatError(
                code="UNSUPPORTED_INSTRUCTION", message=MESSAGES["NO_PLAN"]
            )
            return self._failed(
                request,
                errors.failure_from_error(
                    error, fallback_reason=errors.UNSUPPORTED_INSTRUCTION
                ),
                provider_name=provider_name,
            )

        if result.plan.operation_count > MAX_OPERATIONS_PER_REQUEST:
            return self._failed(
                request,
                errors.failure(
                    errors.INVALID_REQUEST,
                    "UNSUPPORTED_INSTRUCTION",
                    MESSAGES["MULTI_OPERATION"],
                ),
                provider_name=provider_name,
            )

        # ---- 4. canonical Job ----------------------------------------
        created = self.job_factory.build(
            plan=result.plan,
            request=request,
            context=context,
            created_at=self.clock(),
        )
        if not created.ok or not created.jobs:
            error = (
                created.errors[0]
                if created.errors
                else ChatError(code="INTERNAL_ERROR", message=MESSAGES["NO_PLAN"])
            )
            return self._failed(
                request,
                errors.failure_from_error(error, fallback_reason=errors.INVALID_REQUEST),
                provider_name=provider_name,
            )

        job_wire = to_wire(created.jobs[0])

        # ---- 5. record with a stable mutation identity ---------------
        record, duplicate = self.store.submit(record_from_job(job_wire))

        conflict = self._conflict(record, job_wire)
        if conflict is not None:
            return self._failed(request, conflict, provider_name=provider_name)

        if duplicate and record.is_terminal:
            # Already finished. Report the existing outcome; do not offer again.
            return ChatSubmission(
                ok=True,
                request_id=request.request_id,
                project_id=record.project_id,
                session_id=record.session_id,
                summary=self._summary_for(record, duplicate=True),
                job_id=record.job_id,
                job_status=record.job_status,
                worker_id=record.worker_id,
                duplicate=True,
                provider_name=provider_name,
            )

        if duplicate and record.job_status in ("claimed", "running"):
            # In flight. A retry must not offer a second copy.
            return ChatSubmission(
                ok=True,
                request_id=request.request_id,
                project_id=record.project_id,
                session_id=record.session_id,
                summary=self._summary_for(record, duplicate=True),
                job_id=record.job_id,
                job_status=record.job_status,
                worker_id=record.worker_id,
                duplicate=True,
                provider_name=provider_name,
            )

        # ---- 6. select a worker --------------------------------------
        # Reached either for a new job or for a retry of one that is still
        # queued because no worker was available earlier.
        selection = self.selector.select(record.job or job_wire)
        if not selection.ok:
            logger.info(
                "no worker available for job %s: %s", record.job_id, selection.reason
            )
            # The record is deliberately KEPT. Its identity is already derived and
            # stored, so retrying the same request_id resolves to this same job
            # rather than minting a second mutation.
            return ChatSubmission(
                ok=False,
                request_id=request.request_id,
                project_id=record.project_id,
                session_id=record.session_id,
                summary=NO_WORKER_MESSAGE,
                job_id=record.job_id,
                job_status=record.job_status,
                duplicate=duplicate,
                provider_name=provider_name,
                failure=errors.failure(
                    errors.NO_READY_WORKER, "BLENDER_UNAVAILABLE", NO_WORKER_MESSAGE
                ),
            )

        # ---- 7. offer ------------------------------------------------
        worker_id = str(selection.worker_id)
        try:
            self.offer_job(worker_id, record.job or job_wire)
        except protocol.ProtocolError as exc:
            # The control plane refuses to offer a job that fails the canonical
            # contract. This is a bug, not user error, so nothing is leaked.
            logger.error("refusing to offer job %s: %s", record.job_id, exc)
            return self._failed(
                request,
                errors.failure(
                    errors.INTERNAL, "INTERNAL_ERROR", MESSAGES["OFFER_FAILED"]
                ),
                provider_name=provider_name,
            )

        record.worker_id = worker_id
        record.offer_count += 1
        record.touch()
        self.store.save(record)

        return ChatSubmission(
            ok=True,
            request_id=request.request_id,
            project_id=record.project_id,
            session_id=record.session_id,
            summary=self._summary_for(record, duplicate=duplicate),
            job_id=record.job_id,
            job_status=record.job_status,
            worker_id=worker_id,
            duplicate=duplicate,
            provider_name=provider_name,
        )

    # -- status ------------------------------------------------------------

    def job_record(self, project_id: str, job_id: str) -> Optional[JobRecord]:
        """Read one job WITHIN a project.

        ``project_id`` is required: a job_id alone must never resolve to a record.
        """
        return self.store.get(project_id, job_id)

    def await_terminal(
        self,
        project_id: str,
        job_id: str,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 0.05,
    ) -> Optional[JobRecord]:
        """Block until a job reaches a terminal state, or the deadline passes.

        A bounded convenience for tests and local scripting. The ARCHITECTURE is
        asynchronous: HTTP never depends on this, and no route calls it.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            record = self.store.get(project_id, job_id)
            if record is not None and record.is_terminal:
                return record
            time.sleep(poll_seconds)
        return self.store.get(project_id, job_id)

    # -- internals ---------------------------------------------------------

    def _conflict(
        self, record: JobRecord, job_wire: dict[str, Any]
    ) -> Optional[errors.ControlPlaneFailure]:
        """Detect a request_id reused for a DIFFERENT instruction.

        Retrying a request is idempotent and welcome. Reusing the same
        ``request_id`` for different content is a client error: mutation identity
        would claim the two are the same change when they are not, so it is
        refused instead of silently ignoring the new instruction.
        """
        incoming = job_wire.get("content_fingerprint")
        if (
            incoming
            and record.content_fingerprint
            and incoming != record.content_fingerprint
        ):
            return errors.failure(
                errors.JOB_CONFLICT,
                "PRECONDITION_MISMATCH",
                "this request_id was already used for a different change; use a "
                "new request_id for a new instruction",
            )
        return None

    def _summary_for(self, record: JobRecord, duplicate: bool) -> str:
        """A human-readable summary that never overstates what happened."""
        if record.job_status == "succeeded":
            return "The change has been applied and the project was saved."
        if record.job_status == "failed":
            return "The change could not be applied."
        if duplicate:
            return (
                "This request was already submitted; reporting the existing "
                "change rather than repeating it."
            )
        return "The change was understood and sent to the design machine."

    def _failed(
        self,
        request: ChatRequest,
        control_failure: errors.ControlPlaneFailure,
        provider_name: Optional[str] = None,
    ) -> ChatSubmission:
        return ChatSubmission(
            ok=False,
            request_id=request.request_id,
            project_id=request.project_id,
            session_id=request.session_id,
            summary=control_failure.message,
            provider_name=provider_name,
            failure=control_failure,
        )


# ---------------------------------------------------------------------------
# Canonical ChatResponse projection
# ---------------------------------------------------------------------------


def chat_response_for(
    record: JobRecord, preview_url: Optional[str] = None
) -> ChatResponse:
    """Render a terminal job record as the canonical ChatResponse.

    The canonical ``ChatResponse`` (``chat-response.schema.json``) is the contract
    the browser ultimately consumes, so it is produced from the recorded worker
    result rather than reinvented. It closes its object, which is exactly why job
    identity is NOT crammed into it: identity lives in the surrounding API
    envelope.

    ``preview_url`` is passed in by the HTTP layer rather than derived here: only
    the route knows the URL shape, and this module must not.
    """
    if record.job_status == "succeeded":
        return ChatResponse(
            status="success",
            summary=(
                "The change has been applied and the project was saved."
                if preview_url
                # Honest about the degraded case: the design change really did
                # happen, and only the picture of it is missing.
                else "The change has been applied and the project was saved, "
                "but a preview image could not be generated."
                if record.preview_error
                else "The change has been applied and the project was saved."
            ),
            object_position=_final_position(record.result),
            preview_url=preview_url,
        )

    error = record.error or {}
    return ChatResponse(
        status="error",
        summary="The change could not be applied.",
        error=ChatError(
            code=error.get("code", "INTERNAL_ERROR"),  # type: ignore[arg-type]
            message=error.get("message", "the change could not be applied"),
        ),
    )


def _final_position(result: Optional[Any]) -> Optional[Vec3]:
    """Extract the verified final position in canonical meters, when reported."""
    if not isinstance(result, dict):
        return None
    position = result.get("final_position_meters")
    if not isinstance(position, dict):
        return None
    try:
        return Vec3(
            x=float(position["x"]), y=float(position["y"]), z=float(position["z"])
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "MAX_OPERATIONS_PER_REQUEST",
    "ChatService",
    "ChatSubmission",
    "chat_response_for",
]
