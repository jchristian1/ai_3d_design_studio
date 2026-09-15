"""Turning an AgentPlan into canonical Jobs.

Spec 001, Task 7.

This is the deterministic side of the boundary. Interpretation is probabilistic and
lives in a provider; job construction is mechanical and lives here::

    AgentPlan (semantic)  ->  JobFactory  ->  canonical Job (Task 3)

Identity discipline
-------------------
Every identity on the resulting Job comes from the trusted ChatRequest and
AgentContext, never from the plan: an AgentPlan has no identity fields at all, so
a provider cannot name its own project, user, or session. The factory additionally
refuses a request whose ``project_id`` disagrees with the context's.

Idempotency
-----------
Nothing is reimplemented here. ``create_move_object_job`` from Task 3 derives the
mutation identity from ``(project_id, request_id, operation_index)``. An
operation's position in ``AgentPlan.operations`` IS its ``operation_index``, so a
retry of the same request produces the same identity while a genuinely new request
produces a different one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from studio_contracts import ChatError
from studio_contracts.jobs import create_move_object_job
from studio_types import MoveObjectPayload

from .context import AgentContext
from .plan import AgentPlan, PlannedOperation

MESSAGES: dict[str, str] = {
    "MISSING_USER": (
        "trusted context is missing user_id; a job cannot be created without an "
        "authenticated user"
    ),
    "MISSING_PROJECT": "trusted context is missing project_id",
    "MISSING_SESSION": "trusted context is missing session_id",
    "MISSING_REQUEST_ID": (
        "the request is missing request_id; mutation identity is derived from it"
    ),
    "PROJECT_MISMATCH": (
        "the request's project_id does not match the trusted context; refusing to "
        "create a job for a different project"
    ),
    "UNSUPPORTED_OPERATION": "cannot build a job for operation type {operation_type!r}",
    "BAD_PAYLOAD": "operation payload is not usable for {operation_type!r}",
}


@dataclass
class JobCreationResult:
    """Jobs built from a plan, or the first structured reason it was refused."""

    ok: bool
    jobs: list[Any] = field(default_factory=list)
    errors: list[ChatError] = field(default_factory=list)


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def _request_field(request: Any, name: str) -> Any:
    if isinstance(request, Mapping):
        return request.get(name)
    return getattr(request, name, None)


def _job_id_for(request_id: str, operation_index: int) -> str:
    """Derive a deterministic job_id from the originating request.

    Deterministic rather than random so a retry of the same request reuses the
    same job record instead of creating a second one. Task 3 treats ``job_id`` as
    the job-record identity and derives mutation identity separately, so this only
    needs to be stable and unique per (request, operation).
    """
    return f"job_{request_id}_{operation_index}"


class JobFactory:
    """Builds canonical Jobs from a provider-neutral AgentPlan."""

    def build(
        self,
        plan: AgentPlan,
        request: Any,
        context: AgentContext,
        created_at: str,
    ) -> JobCreationResult:
        """Create one Job per planned operation, in order.

        ``created_at`` is supplied by the caller so no clock is read here and the
        result is reproducible in tests.
        """
        errors = self._identity_errors(request, context)
        if errors:
            return JobCreationResult(ok=False, errors=errors)

        request_id = str(_request_field(request, "request_id"))
        jobs: list[Any] = []

        for operation_index, operation in plan.indexed():
            built = self._build_one(
                operation, operation_index, request_id, context, created_at
            )
            if not built.ok:
                return built
            jobs.extend(built.jobs)

        return JobCreationResult(ok=True, jobs=jobs)

    # -- internals ---------------------------------------------------------

    def _identity_errors(self, request: Any, context: AgentContext) -> list[ChatError]:
        errors: list[ChatError] = []

        if _blank(getattr(context, "user_id", None)):
            errors.append(
                ChatError(code="VALIDATION_ERROR", message=MESSAGES["MISSING_USER"])
            )
        if _blank(getattr(context, "project_id", None)):
            errors.append(
                ChatError(code="VALIDATION_ERROR", message=MESSAGES["MISSING_PROJECT"])
            )
        if _blank(getattr(context, "session_id", None)):
            errors.append(
                ChatError(code="VALIDATION_ERROR", message=MESSAGES["MISSING_SESSION"])
            )

        request_id = _request_field(request, "request_id")
        if _blank(request_id):
            errors.append(
                ChatError(
                    code="VALIDATION_ERROR", message=MESSAGES["MISSING_REQUEST_ID"]
                )
            )

        request_project = _request_field(request, "project_id")
        if (
            not _blank(request_project)
            and not _blank(getattr(context, "project_id", None))
            and request_project != context.project_id
        ):
            errors.append(
                ChatError(
                    code="VALIDATION_ERROR", message=MESSAGES["PROJECT_MISMATCH"]
                )
            )
        return errors

    def _build_one(
        self,
        operation: PlannedOperation,
        operation_index: int,
        request_id: str,
        context: AgentContext,
        created_at: str,
    ) -> JobCreationResult:
        if operation.operation_type != "move_object":
            return JobCreationResult(
                ok=False,
                errors=[
                    ChatError(
                        code="VALIDATION_ERROR",
                        message=MESSAGES["UNSUPPORTED_OPERATION"].format(
                            operation_type=operation.operation_type
                        ),
                    )
                ],
            )

        payload: Optional[MoveObjectPayload] = operation.payload
        if not isinstance(payload, MoveObjectPayload):
            return JobCreationResult(
                ok=False,
                errors=[
                    ChatError(
                        code="VALIDATION_ERROR",
                        message=MESSAGES["BAD_PAYLOAD"].format(
                            operation_type=operation.operation_type
                        ),
                    )
                ],
            )

        result = create_move_object_job(
            job_id=_job_id_for(request_id, operation_index),
            project_id=context.project_id,
            session_id=context.session_id,
            user_id=context.user_id,
            request_id=request_id,
            operation_index=operation_index,
            target=payload.target,
            delta_meters=payload.delta_meters,
            created_at=created_at,
        )
        if not result.ok:
            return JobCreationResult(ok=False, errors=list(result.errors))
        return JobCreationResult(ok=True, jobs=[result.job])
