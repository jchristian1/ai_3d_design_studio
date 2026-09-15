"""Tests for the Python contract/type mirrors (Spec 001, Task 1)."""

from studio_contracts import (
    ERROR_CODES,
    ChatError,
    ChatRequest,
    ChatResponse,
)
from studio_types import JOB_STATUSES, JOB_TYPES, Job, Vec3


def test_chat_request_shape():
    req = ChatRequest(
        project_id="proj_1",
        session_id="sess_1",
        message="Move Cube 50 cm to the right",
    )
    assert req.project_id == "proj_1"
    assert req.selected_object_id is None


def test_chat_response_success_with_meters():
    res = ChatResponse(
        status="success",
        summary="Moved Cube 0.50 m on +X",
        object_position=Vec3(0.5, 0.0, 0.0),
        preview_url="s3://previews/p.png",
    )
    assert res.status == "success"
    assert res.object_position.x == 0.5


def test_chat_response_error_uses_structured_code():
    res = ChatResponse(
        status="error",
        summary="Object not found",
        error=ChatError(code="OBJECT_NOT_FOUND", message="no object 'Cube'"),
    )
    assert res.error.code in ERROR_CODES


def test_job_is_project_scoped():
    job = Job(
        id="job_1",
        project_id="proj_1",
        session_id="sess_1",
        type="chat",
        status="queued",
    )
    assert job.status in JOB_STATUSES
    assert job.type in JOB_TYPES
    assert isinstance(job.project_id, str)
    assert job.created_at  # auto-populated ISO timestamp
