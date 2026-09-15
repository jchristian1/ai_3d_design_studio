"""HTTP request and response models.

Spec 001, Task 9.

These are the API's VIEW MODELS, not contracts. The canonical schemas in
``packages/contracts/schemas`` remain the source of truth for cross-boundary data;
these Pydantic models exist so FastAPI can parse, validate, and document HTTP.

    chat-request.schema.json   <-- CANONICAL
            |
            +--> studio_contracts.ChatRequest   (Python representation)
            +--> ChatRequestModel               (HTTP parsing, this module)

``ChatRequestModel`` mirrors the canonical ChatRequest field for field, including
``extra="forbid"`` for the schema's ``additionalProperties: false``. A conformance
test asserts the mirror, so the two cannot drift silently.

WHY THERE IS NO user_id FIELD
-----------------------------
Because the canonical schema has none, and ``extra="forbid"`` means a client that
sends one is rejected with 422 rather than quietly ignored. Identity is established
server-side (see identity.py). The same closure is what prevents a client from
supplying a ``blend_path`` or any other filesystem location.

WHY THE CHAT RESPONSE IS NOT ChatResponse
-----------------------------------------
The canonical ``ChatResponse`` describes the FINAL answer about a change and closes
its object, so it cannot carry a ``job_id``. Spec 001's flow is asynchronous:
``POST /api/chat`` answers "understood, recorded, dispatched" and the browser polls
for the outcome. That submission acknowledgement is a different message, so it gets
its own envelope, and the canonical ChatResponse is embedded verbatim under
``chat`` once a job is terminal.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Mirrors the canonical `"minLength": 1, "pattern": "\\S"` pair: a non-blank
#: string. minLength alone would admit "   ".
NonBlankStr = Field(min_length=1, pattern=r"\S")


class ChatRequestModel(BaseModel):
    """HTTP mirror of the canonical ChatRequest.

    ``extra="forbid"`` mirrors ``additionalProperties: false``, so unknown fields —
    notably ``user_id`` and anything path-shaped — are rejected, not ignored.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = NonBlankStr
    project_id: str = NonBlankStr
    session_id: str = NonBlankStr
    message: str = NonBlankStr
    selected_object_id: Optional[str] = Field(
        default=None, min_length=1, pattern=r"\S"
    )


class ErrorModel(BaseModel):
    """The canonical structured error payload (error-response.schema.json)."""

    code: str
    message: str


class ChatSubmissionModel(BaseModel):
    """The acknowledgement returned by POST /api/chat.

    Deliberately reports identity and status, not a finished outcome: Blender work
    is not complete when this is returned.
    """

    request_id: str
    project_id: str
    session_id: str
    job_id: str
    job_status: str
    #: Human-readable, safe to display.
    summary: str
    worker_id: Optional[str] = None
    #: True when this resolved to an existing job (a retry of the same request_id).
    duplicate: bool = False
    #: Which AgentProvider interpreted the request. Safe, non-sensitive.
    provider: Optional[str] = None
    #: Project-scoped URL to poll for the outcome. A job is never addressable by
    #: job_id alone, so this always carries the project.
    status_url: str


class ChatResponseModel(BaseModel):
    """The canonical ChatResponse, embedded once a job is terminal."""

    model_config = ConfigDict(extra="forbid")

    status: str
    summary: str
    object_position: Optional[dict[str, float]] = None
    preview_url: Optional[str] = None
    error: Optional[ErrorModel] = None


class JobStatusModel(BaseModel):
    """The response of GET /api/projects/{project_id}/jobs/{job_id}."""

    job_id: str
    project_id: str
    session_id: str
    request_id: str
    job_type: str
    #: Canonical Job lifecycle: queued | claimed | running | succeeded | failed.
    job_status: str
    created_at: str
    updated_at: str
    worker_id: Optional[str] = None
    #: Internal worker execution phase. Observability only, never a contract.
    execution_phase: Optional[str] = None
    #: True when the terminal state arrived as a worker resend of a result the
    #: control plane had not received.
    reconciled: bool = False
    result: Optional[dict[str, Any]] = None
    error: Optional[ErrorModel] = None
    #: The canonical ChatResponse, present only once the job is terminal.
    chat: Optional[ChatResponseModel] = None


class WorkerCapabilitiesModel(BaseModel):
    """Safe capability view. Mirrors worker-capabilities.schema.json."""

    worker_version: Optional[str] = None
    blender_available: Optional[bool] = None
    blender_version: Optional[str] = None
    gpu_available: Optional[bool] = None
    gpu_name: Optional[str] = None
    supported_job_types: list[str] = Field(default_factory=list)
    max_concurrent_jobs: Optional[int] = None


class WorkerModel(BaseModel):
    """Safe per-worker view for development and debugging.

    Contains no token, no filesystem path, no home directory, no environment
    variable, and no host detail beyond the coarse capabilities the worker itself
    chose to advertise.
    """

    worker_id: str
    connection_id: str
    protocol_version: int
    #: healthy | busy | lost, derived from heartbeats.
    liveness: str
    #: The worker's self-reported state: ready | busy.
    worker_state: str
    connected: bool
    current_job_id: Optional[str] = None
    pending_offers: int = 0
    capabilities: WorkerCapabilitiesModel


class WorkersResponseModel(BaseModel):
    workers: list[WorkerModel]
    registered_workers: int
    ready_workers: int


class HealthModel(BaseModel):
    """Control-plane process health.

    ``api`` describes THIS process only. Blender health is reported separately and
    is never inferred from the API being alive: ``blender_capable_workers`` counts
    workers that advertised a usable Blender, and it is 0 when none have connected.
    """

    api: str
    environment: str
    registered_workers: int
    ready_workers: int
    blender_capable_workers: int


__all__ = [
    "ChatRequestModel",
    "ChatResponseModel",
    "ChatSubmissionModel",
    "ErrorModel",
    "HealthModel",
    "JobStatusModel",
    "WorkerCapabilitiesModel",
    "WorkerModel",
    "WorkersResponseModel",
]
