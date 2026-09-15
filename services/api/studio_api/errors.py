"""Structured failures and their HTTP mapping.

Spec 001, Task 9.

Two vocabularies, deliberately kept separate::

    ChatError.code   canonical, cross-boundary, in the RESPONSE BODY
                     (error-code.schema.json — shared with MCP and the worker)

    FailureReason    API-level, decides the HTTP STATUS only
                     (never serialized, never a contract)

Why two
-------
The canonical error codes are a product vocabulary shared by the agent, MCP, and
the worker. They intentionally do not encode HTTP semantics — "unknown project" and
"malformed field" are both ``VALIDATION_ERROR`` to a worker, yet they are 404 and
422 to a browser. Rather than pollute the canonical enum with transport concerns
(or, worse, return 422 for a missing project), the service reports an API-level
reason alongside the canonical error, and this module maps the reason to a status.

Adding a canonical code such as ``PROJECT_NOT_FOUND`` would change a
cross-language contract for a purely HTTP concern, so it is not done here.

Leak discipline
---------------
Response bodies carry only a canonical code and a message written for a user. No
tracebacks, no exception class names, no filesystem paths, no configuration
values, no tokens. Unexpected exceptions are converted to a fixed
``INTERNAL_ERROR`` body and the detail goes to the server log instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from studio_contracts import ChatError

# ---------------------------------------------------------------------------
# API-level failure reasons
# ---------------------------------------------------------------------------

#: The request itself is malformed or internally inconsistent.
INVALID_REQUEST = "invalid_request"
#: The project_id is unsafe, unknown, or not accessible to this caller.
UNKNOWN_PROJECT = "unknown_project"
#: The agent understood the request format but cannot act on the instruction.
UNSUPPORTED_INSTRUCTION = "unsupported_instruction"
#: No worker is currently able to take the job.
NO_READY_WORKER = "no_ready_worker"
#: The configured AgentProvider cannot serve requests.
PROVIDER_UNAVAILABLE = "provider_unavailable"
#: The submission collides with existing state (e.g. a reused request_id that
#: carries a different instruction).
JOB_CONFLICT = "job_conflict"
#: The caller could not be identified.
UNAUTHENTICATED = "unauthenticated"
#: Something went wrong that the client cannot act on.
INTERNAL = "internal"

FAILURE_REASONS: tuple[str, ...] = (
    INVALID_REQUEST,
    UNKNOWN_PROJECT,
    UNSUPPORTED_INSTRUCTION,
    NO_READY_WORKER,
    PROVIDER_UNAVAILABLE,
    JOB_CONFLICT,
    UNAUTHENTICATED,
    INTERNAL,
)

#: reason -> HTTP status. The single source of truth for the mapping.
HTTP_STATUS_BY_REASON: dict[str, int] = {
    INVALID_REQUEST: 422,
    UNKNOWN_PROJECT: 404,
    UNSUPPORTED_INSTRUCTION: 422,
    NO_READY_WORKER: 503,
    PROVIDER_UNAVAILABLE: 503,
    JOB_CONFLICT: 409,
    UNAUTHENTICATED: 401,
    INTERNAL: 500,
}

#: Canonical error codes an AgentProvider or JobFactory may return, mapped to the
#: API reason that decides the status. Anything unlisted is treated as INTERNAL,
#: so a new canonical code can never silently become a 200.
REASON_BY_ERROR_CODE: dict[str, str] = {
    "VALIDATION_ERROR": INVALID_REQUEST,
    "UNSUPPORTED_INSTRUCTION": UNSUPPORTED_INSTRUCTION,
    "INVALID_UNITS": INVALID_REQUEST,
    "OBJECT_NOT_FOUND": INVALID_REQUEST,
    "OBJECT_NOT_MOVABLE": INVALID_REQUEST,
    "PRECONDITION_MISMATCH": JOB_CONFLICT,
    "LOCK_CONFLICT": JOB_CONFLICT,
    "PROVIDER_UNAVAILABLE": PROVIDER_UNAVAILABLE,
    "BLENDER_UNAVAILABLE": NO_READY_WORKER,
    "MUTATION_FAILED": INTERNAL,
    "VERIFY_FAILED": INTERNAL,
    "INTERNAL_ERROR": INTERNAL,
}

#: The only message a client ever sees for an unexpected exception.
GENERIC_INTERNAL_MESSAGE = (
    "the control plane could not complete this request; the failure has been "
    "logged"
)


@dataclass(frozen=True)
class ControlPlaneFailure:
    """A structured, transport-neutral failure.

    Carries the canonical error for the body and the API reason for the status.
    Services return this instead of raising, so callers branch on data.
    """

    reason: str
    error: ChatError

    @property
    def http_status(self) -> int:
        return HTTP_STATUS_BY_REASON.get(self.reason, 500)

    @property
    def code(self) -> str:
        return self.error.code

    @property
    def message(self) -> str:
        return self.error.message

    def body(self, request_id: Optional[str] = None) -> dict[str, Any]:
        """The consistent structured HTTP error body."""
        return error_body(self.error.code, self.error.message, request_id)


def failure(reason: str, code: str, message: str) -> ControlPlaneFailure:
    """Build a failure with an explicit reason and canonical code."""
    return ControlPlaneFailure(
        reason=reason, error=ChatError(code=code, message=message)  # type: ignore[arg-type]
    )


def failure_from_error(
    error: ChatError, fallback_reason: str = INTERNAL
) -> ControlPlaneFailure:
    """Derive the HTTP reason from a canonical error produced downstream.

    Used for AgentProvider and JobFactory outcomes, so their structured errors
    reach HTTP with the right status without the route interpreting them.
    """
    reason = REASON_BY_ERROR_CODE.get(error.code, fallback_reason)
    return ControlPlaneFailure(reason=reason, error=error)


def error_body(
    code: str, message: str, request_id: Optional[str] = None
) -> dict[str, Any]:
    """The one structured error body shape every failing endpoint returns.

    Keeping this in one function is what makes "no stack traces, no paths, no
    secrets" auditable: there is a single place where an error becomes JSON.
    """
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if request_id:
        body["request_id"] = request_id
    return body


def internal_error_body(request_id: Optional[str] = None) -> dict[str, Any]:
    """The body for an unexpected exception. Deliberately says nothing."""
    return error_body("INTERNAL_ERROR", GENERIC_INTERNAL_MESSAGE, request_id)


__all__ = [
    "FAILURE_REASONS",
    "GENERIC_INTERNAL_MESSAGE",
    "HTTP_STATUS_BY_REASON",
    "INTERNAL",
    "INVALID_REQUEST",
    "JOB_CONFLICT",
    "NO_READY_WORKER",
    "PROVIDER_UNAVAILABLE",
    "REASON_BY_ERROR_CODE",
    "UNAUTHENTICATED",
    "UNKNOWN_PROJECT",
    "UNSUPPORTED_INSTRUCTION",
    "ControlPlaneFailure",
    "error_body",
    "failure",
    "failure_from_error",
    "internal_error_body",
]
