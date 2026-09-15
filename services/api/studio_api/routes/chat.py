"""POST /api/chat — submit a natural-language design change.

Spec 001, Task 9.

This module is deliberately thin. It parses HTTP, obtains the trusted identity,
calls ``ChatService.submit_chat``, and renders the structured result. No
interpretation, no job construction, no worker selection, and no concrete agent
provider is named here — changing which language engine interprets a request does
not touch this file.

    HTTP  ->  ChatRequestModel  ->  ChatService.submit_chat  ->  ChatSubmission
                                                                      |
                                            HTTP status + structured body

Why 202 and not 200
-------------------
A successful submission means "understood, recorded with a stable identity, and
handed to a worker" — not "Blender is finished". Blender work takes seconds to
minutes, and an HTTP request must not own a mutation's lifetime. 202 Accepted says
exactly that, and ``status_url`` points at the record to poll.

Two paths, one service
----------------------
``POST /api/chat`` is the primary endpoint. ``POST /api/projects/{project_id}/chat``
is the project-scoped form named in the spec's task list; it delegates to the same
service and requires the path and body ``project_id`` to agree, so a request can
never be ambiguous about which project it mutates.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from studio_contracts import ChatRequest

from ..chat_service import ChatService, ChatSubmission
from ..errors import INVALID_REQUEST, HTTP_STATUS_BY_REASON, error_body
from ..identity import TrustedIdentity
from ..models import ChatRequestModel, ChatSubmissionModel
from .jobs import job_status_path
from .support import ControlPlaneHTTPError, get_chat_service, get_identity

router = APIRouter(prefix="/api", tags=["chat"])


def _to_contract(model: ChatRequestModel) -> ChatRequest:
    """Convert the HTTP model into the canonical ChatRequest representation.

    Note the absence of any identity field: the canonical ChatRequest has no
    ``user_id``, so identity cannot travel from the client into the service.
    """
    return ChatRequest(
        request_id=model.request_id,
        project_id=model.project_id,
        session_id=model.session_id,
        message=model.message,
        selected_object_id=model.selected_object_id,
    )


def _render(submission: ChatSubmission, response: Response) -> ChatSubmissionModel:
    """Turn a ChatSubmission into an HTTP response, or raise a structured error."""
    if submission.failure is not None or submission.job_id is None:
        failure = submission.failure
        status = failure.http_status if failure is not None else 500
        code = failure.code if failure is not None else "INTERNAL_ERROR"
        message = failure.message if failure is not None else submission.summary
        raise ControlPlaneHTTPError(
            status_code=status,
            body=error_body(code, message, submission.request_id),
        )

    response.status_code = submission.http_status
    return ChatSubmissionModel(
        request_id=submission.request_id,
        project_id=submission.project_id,
        session_id=submission.session_id,
        job_id=submission.job_id,
        job_status=submission.job_status or "queued",
        summary=submission.summary,
        worker_id=submission.worker_id,
        duplicate=submission.duplicate,
        provider=submission.provider_name,
        # Project-scoped, built by the jobs route itself so the two cannot
        # disagree. A job is never addressable by job_id alone.
        status_url=job_status_path(submission.project_id, submission.job_id),
    )


@router.post(
    "/chat",
    response_model=ChatSubmissionModel,
    status_code=202,
    summary="Submit a design change in natural language",
)
async def submit_chat(
    payload: ChatRequestModel,
    response: Response,
    identity: TrustedIdentity = Depends(get_identity),
    service: ChatService = Depends(get_chat_service),
) -> ChatSubmissionModel:
    submission = service.submit_chat(_to_contract(payload), identity)
    return _render(submission, response)


@router.post(
    "/projects/{project_id}/chat",
    response_model=ChatSubmissionModel,
    status_code=202,
    summary="Submit a design change scoped to a project path",
)
async def submit_project_chat(
    project_id: str,
    payload: ChatRequestModel,
    response: Response,
    identity: TrustedIdentity = Depends(get_identity),
    service: ChatService = Depends(get_chat_service),
) -> ChatSubmissionModel:
    # Ambiguity about WHICH project is being mutated is a project-isolation risk,
    # so a mismatch is refused rather than resolved by precedence.
    if project_id != payload.project_id:
        raise ControlPlaneHTTPError(
            status_code=HTTP_STATUS_BY_REASON[INVALID_REQUEST],
            body=error_body(
                "VALIDATION_ERROR",
                "the project in the URL does not match the project in the request "
                "body",
                payload.request_id,
            ),
        )
    submission = service.submit_chat(_to_contract(payload), identity)
    return _render(submission, response)


__all__ = ["router"]
