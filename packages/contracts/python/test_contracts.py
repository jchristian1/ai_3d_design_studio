"""Tests for the Python contract/type mirrors (Spec 001, Task 1)."""

from studio_contracts import (
    ERROR_CODES,
    ChatError,
    ChatRequest,
    ChatResponse,
)
from studio_types import (
    JOB_STATUSES,
    JOB_TYPES,
    Job,
    MoveObjectPayload,
    ObjectRef,
    RequestOrigin,
    Vec3,
)


def test_chat_request_shape():
    req = ChatRequest(
        request_id="req_abc123",
        project_id="proj_1",
        session_id="sess_1",
        message="Move Cube 50 cm to the right",
    )
    assert req.request_id == "req_abc123"
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
        job_id="job_1",
        job_type="move_object",
        project_id="proj_1",
        session_id="sess_1",
        user_id="user_1",
        payload=MoveObjectPayload(
            target=ObjectRef(name="Cube"), delta_meters=Vec3(0.5, 0.0, 0.0)
        ),
        origin=RequestOrigin(request_id="req_abc123", operation_index=0),
        status="queued",
        idempotency_key="idem_abc",
    )
    assert job.status in JOB_STATUSES
    assert job.job_type in JOB_TYPES
    assert isinstance(job.project_id, str)
    assert job.created_at  # auto-populated ISO timestamp
