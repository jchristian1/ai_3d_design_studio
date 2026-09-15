"""Shared API/event contracts for AI 3D Design Studio — Python representation.

Direction of truth::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
             |
             +--> TypeScript representation  (packages/contracts/src/index.ts)
             +--> Python representation      (this module)

The dataclasses below are a REPRESENTATION of the canonical schemas. They are
not the source of truth and neither is the TypeScript module. When a contract
changes, the schema in ../../schemas changes first; conformance tests
(test_conformance.py and conformance.test.ts) fail until both language
representations are realigned.

Canonical schemas:
    ChatRequest    -> chat-request.schema.json
    ChatResponse   -> chat-response.schema.json
    ChatError      -> error-response.schema.json
    ErrorCode      -> error-code.schema.json
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal, Optional

from studio_types import Vec3

from .schema import (  # re-exported for callers/tests
    SCHEMA_DIR,
    SchemaViolation,
    list_schema_names,
    load_schema,
    schema_enum,
    schema_properties,
    schema_required,
    validate_against_schema,
)

ErrorCode = Literal[
    "VALIDATION_ERROR",
    "UNSUPPORTED_INSTRUCTION",
    "OBJECT_NOT_FOUND",
    "OBJECT_NOT_MOVABLE",
    "INVALID_UNITS",
    "PRECONDITION_MISMATCH",
    "LOCK_CONFLICT",
    "PROVIDER_UNAVAILABLE",
    "BLENDER_UNAVAILABLE",
    "MUTATION_FAILED",
    "VERIFY_FAILED",
    "INTERNAL_ERROR",
]

ERROR_CODES: tuple[ErrorCode, ...] = (
    "VALIDATION_ERROR",
    "UNSUPPORTED_INSTRUCTION",
    "OBJECT_NOT_FOUND",
    "OBJECT_NOT_MOVABLE",
    "INVALID_UNITS",
    "PRECONDITION_MISMATCH",
    "LOCK_CONFLICT",
    "PROVIDER_UNAVAILABLE",
    "BLENDER_UNAVAILABLE",
    "MUTATION_FAILED",
    "VERIFY_FAILED",
    "INTERNAL_ERROR",
)

ChatStatus = Literal["success", "error"]

#: Canonical schema file names, so callers and tests never hardcode paths.
SCHEMA_FILES: dict[str, str] = {
    "Vec3": "vec3.schema.json",
    "Job": "job.schema.json",
    "JobType": "job-type.schema.json",
    "JobClaim": "job-claim.schema.json",
    "RequestOrigin": "request-origin.schema.json",
    "MoveObjectPlan": "move-object-plan.schema.json",
    "MoveObjectResult": "move-object-result.schema.json",
    "WorkerMessage": "worker-message.schema.json",
    "WorkerCapabilities": "worker-capabilities.schema.json",
    "ObjectRef": "object-ref.schema.json",
    "MoveObjectPayload": "move-object-payload.schema.json",
    "ChatRequest": "chat-request.schema.json",
    "ChatResponse": "chat-response.schema.json",
    "ErrorResponse": "error-response.schema.json",
    "ErrorCode": "error-code.schema.json",
    "LengthUnit": "length-unit.schema.json",
    "Measurement": "measurement.schema.json",
    "Axis": "axis.schema.json",
    "Direction": "direction.schema.json",
    "AxisDirection": "axis-direction.schema.json",
}


@dataclass
class ChatRequest:
    """A natural-language design request from the browser.

    ``project_id`` is REQUIRED: every request is explicitly project-scoped
    (see .kiro/steering/security.md project isolation).

    ``request_id`` is REQUIRED and is the stable identity of this submission:
    reused verbatim on retry, regenerated for a new intentional command.

    Canonical schema: ``chat-request.schema.json``
    """

    request_id: str
    project_id: str
    session_id: str
    message: str
    selected_object_id: Optional[str] = None


@dataclass
class ChatError:
    """Canonical schema: ``error-response.schema.json``"""

    code: ErrorCode
    message: str


@dataclass
class ChatResponse:
    """Result returned to the browser after processing a ChatRequest.

    ``object_position`` is in canonical meters (used by UI and verification).

    Canonical schema: ``chat-response.schema.json``
    """

    status: ChatStatus
    summary: str
    object_position: Optional[Vec3] = None
    preview_url: Optional[str] = None
    error: Optional[ChatError] = None


def to_wire(value: Any) -> Any:
    """Convert a representation object into a canonical wire document.

    Drops ``None``-valued optional fields so the result is directly comparable
    against the canonical JSON Schemas (JSON has no ``undefined``/absent-vs-null
    distinction here; absent is the contract's representation of "not set").
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(value):
            item = getattr(value, f.name)
            if item is None:
                continue
            out[f.name] = to_wire(item)
        return out
    if isinstance(value, (list, tuple)):
        return [to_wire(v) for v in value]
    if isinstance(value, dict):
        return {k: to_wire(v) for k, v in value.items() if v is not None}
    return value


__all__ = [
    "ChatError",
    "ChatRequest",
    "ChatResponse",
    "ChatStatus",
    "ERROR_CODES",
    "ErrorCode",
    "SCHEMA_DIR",
    "SCHEMA_FILES",
    "SchemaViolation",
    "list_schema_names",
    "load_schema",
    "schema_enum",
    "schema_properties",
    "schema_required",
    "to_wire",
    "validate_against_schema",
]
