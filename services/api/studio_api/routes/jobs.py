"""GET /api/projects/{project_id}/jobs/{job_id} — the outcome of a change.

Spec 001, Task 9.

This is the other half of the asynchronous submission model: ``POST /api/chat``
returns an identity, and this endpoint reports what happened to it.

    queued  ->  claimed  ->  running  ->  succeeded | failed
    ^^^^^^      ^^^^^^^      ^^^^^^^
    offered     worker       worker reported
                accepted     progress

The statuses are the canonical Job lifecycle from Task 3, not an API-specific
vocabulary. The worker's protocol word "accepted" is reported as the contract word
``claimed``; they are the same event, and ``accepted`` is deliberately NOT a
canonical job state.

Once a job is terminal the response embeds the canonical ``ChatResponse``
(``chat-response.schema.json``) under ``chat``, so a browser consumes the contract
rather than an API-shaped variant of it.

PROJECT ISOLATION
-----------------
The route is project-scoped in its PATH, and there is no unscoped variant. A
``job_id`` is an identifier, not a capability — knowing one must never be enough to
read a job:

  1. ``project_id`` is authorized through the trusted project registry first. An
     unknown or unsafe project is 404 and no lookup happens.
  2. The store is then queried with ``(project_id, job_id)``. A job belonging to a
     different project simply is not found.
  3. A job that exists in another project returns exactly the same 404 as a job
     that does not exist at all, so the response cannot be used to probe for
     cross-project existence.

What this endpoint does NOT do
------------------------------
It never triggers, retries, or re-offers work. It is a pure read of the
control-plane projection. If the projection has no record — the API restarted, or
the job_id was never issued — the answer is 404, not a re-execution.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from studio_contracts import to_wire

from ..chat_service import chat_response_for
from ..errors import error_body
from ..models import ChatResponseModel, ErrorModel, JobStatusModel
from .support import AppDependencies, ControlPlaneHTTPError, get_dependencies

router = APIRouter(prefix="/api", tags=["jobs"])

#: The one place the job status path is built, so the submission response and the
#: route itself can never disagree.
JOB_PATH_TEMPLATE = "/api/projects/{project_id}/jobs/{job_id}"

#: Deliberately identical for "no such job" and "that job is another project's".
#: Distinguishing them would leak cross-project existence.
JOB_NOT_FOUND_MESSAGE = "no such job exists in this project"

PROJECT_NOT_FOUND_MESSAGE = (
    "that project does not exist or is not available to you"
)


def job_status_path(project_id: str, job_id: str) -> str:
    """Build the project-scoped status URL for a job."""
    return JOB_PATH_TEMPLATE.format(project_id=project_id, job_id=job_id)


@router.get(
    "/projects/{project_id}/jobs/{job_id}",
    response_model=JobStatusModel,
    summary="Status and result of a submitted change",
)
async def get_project_job(
    project_id: str,
    job_id: str,
    dependencies: AppDependencies = Depends(get_dependencies),
) -> JobStatusModel:
    # ---- 1. authorize the project through the trusted boundary -------
    # This runs BEFORE any job lookup, so an unsafe or unknown project_id is
    # rejected on its own merits rather than incidentally missing the store.
    if dependencies.projects.get(project_id) is None:
        raise ControlPlaneHTTPError(
            status_code=404,
            body=error_body("VALIDATION_ERROR", PROJECT_NOT_FOUND_MESSAGE),
        )

    # ---- 2. project-scoped lookup ------------------------------------
    record = dependencies.store.get(project_id, job_id)
    if record is None:
        # Same answer whether the job is unknown or belongs to another project.
        raise ControlPlaneHTTPError(
            status_code=404,
            body=error_body("VALIDATION_ERROR", JOB_NOT_FOUND_MESSAGE),
        )

    chat: Optional[ChatResponseModel] = None
    if record.is_terminal:
        chat = ChatResponseModel(**to_wire(chat_response_for(record)))

    error = record.error
    return JobStatusModel(
        job_id=record.job_id,
        project_id=record.project_id,
        session_id=record.session_id,
        request_id=record.request_id,
        job_type=record.job_type,
        job_status=record.job_status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        worker_id=record.worker_id,
        execution_phase=record.execution_phase,
        reconciled=record.reconciled,
        result=record.result,
        error=(
            ErrorModel(
                code=str(error.get("code", "INTERNAL_ERROR")),
                message=str(error.get("message", "the change could not be applied")),
            )
            if isinstance(error, dict) and error
            else None
        ),
        chat=chat,
    )


__all__ = [
    "JOB_NOT_FOUND_MESSAGE",
    "JOB_PATH_TEMPLATE",
    "PROJECT_NOT_FOUND_MESSAGE",
    "job_status_path",
    "router",
]
