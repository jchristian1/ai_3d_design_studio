"""Structured job layer — Python representation (Spec 001, Task 3).

This module owns the durable contract that carries a FULLY RESOLVED semantic
operation from the control plane toward the Blender Worker.

Deliberate non-goals for this task:
  - No Blender and no ``bpy``.
  - No MCP execution.
  - No worker loop and no claiming logic (only the claim SHAPE is defined).
  - No agent provider.
  - No Redis and no persistence. ``JobStore`` below is a Protocol only; its
    implementation belongs to the queue/persistence task.

Direction of truth::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL
             |
             +--> TypeScript representation (packages/contracts/src/jobs.ts)
             +--> Python representation (this module)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import struct
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Protocol

from studio_types import (
    JOB_STATUSES,
    JOB_TYPES,
    TERMINAL_JOB_STATUSES,
    InspectScenePayload,
    Job,
    JobClaim,
    JobStatus,
    JobType,
    MoveObjectPayload,
    ObjectRef,
    RequestOrigin,
    Vec3,
)
from studio_validation import is_finite_vec3, is_non_empty_string

from . import SCHEMA_FILES, ChatError, to_wire, validate_against_schema

# ---------------------------------------------------------------------------
# Guard against unresolved natural language reaching the worker
# ---------------------------------------------------------------------------

#: Payload keys that would indicate semantic resolution did not happen.
#:
#: The canonical payload schemas already declare ``additionalProperties: false``,
#: so these keys cannot pass schema validation. This list is defense in depth: it
#: produces a precise, actionable error instead of a generic "additional property"
#: violation, and it documents exactly which concepts must never survive into a
#: job. Resolution belongs to packages/spatial and the agent.
UNRESOLVED_PAYLOAD_KEYS: tuple[str, ...] = (
    "unit",
    "units",
    "distance",
    "distance_cm",
    "direction",
    "axis",
    "measurement",
    "phrase",
    "text",
    "instruction",
    "message",
    "relative_to",
    "reference",
)

JOB_MESSAGES: dict[str, str] = {
    "MISSING_PROJECT_ID": (
        "project_id is required; a job is not executable without it"
    ),
    "UNRESOLVED_PAYLOAD": (
        "payload contains unresolved language or unit fields; units and "
        "directions must be resolved to canonical meters before a job is created"
    ),
    "NON_FINITE_DELTA": "delta_meters components must be finite numbers of meters",
    "INVALID_TARGET": "target must provide a non-blank object_id or name",
    "UNSUPPORTED_JOB_TYPE": (
        "unsupported job_type; expected one of: " + ", ".join(JOB_TYPES)
    ),
    "INVALID_STATUS": (
        "unsupported status; expected one of: " + ", ".join(JOB_STATUSES)
    ),
    "MISSING_REQUEST_ID": (
        "origin.request_id is required; mutation identity is derived from the "
        "originating request"
    ),
    "INVALID_OPERATION_INDEX": (
        "origin.operation_index must be a non-negative integer"
    ),
}


# ---------------------------------------------------------------------------
# Deterministic canonical encoding (for idempotency keys)
# ---------------------------------------------------------------------------


def encode_float64(value: float) -> str:
    """Encode a float as its exact big-endian IEEE-754 bit pattern in hex.

    Decimal text formatting differs between languages in edge cases (JS renders
    1e-7 as "1e-7", Python as "1e-07"), which would make idempotency keys
    diverge. Hashing the raw bits removes formatting from the equation entirely.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("cannot canonicalize a non-numeric value as a float")
    if not math.isfinite(value):
        raise ValueError("cannot canonicalize a non-finite number")
    # Normalize -0.0 to 0.0 so the two zeroes cannot produce different keys.
    normalized = 0.0 if value == 0 else float(value)
    return struct.pack(">d", normalized).hex()


def canonicalize(value: Any) -> str:
    """Deterministic, unambiguous encoding of a JSON-like value.

    Object keys are sorted, strings are length-prefixed (so no delimiter can be
    forged by content), and numbers use their exact bit pattern. The TypeScript
    implementation produces byte-identical output; parity tests assert it.
    """
    if value is None:
        return "n"
    if isinstance(value, bool):
        return "b:1" if value else "b:0"
    if isinstance(value, (int, float)):
        return f"f:{encode_float64(value)}"
    if isinstance(value, str):
        return f"s:{len(value)}:{value}"
    if isinstance(value, (list, tuple)):
        return "a:[" + ",".join(canonicalize(v) for v in value) + "]"
    if isinstance(value, Mapping):
        entries = [
            f"s:{len(k)}:{k}={canonicalize(v)}"
            for k, v in sorted(value.items())
            if v is not None
        ]
        return "o:{" + ",".join(entries) + "}"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return canonicalize(to_wire(value))
    raise ValueError(f"cannot canonicalize value of type {type(value).__name__}")


#: Version tag so the derivation can evolve without silently colliding.
#: v2 = origin-derived (v1 was content-derived and did not distinguish a retry
#: from a repeated intentional command).
IDEMPOTENCY_VERSION = "v2"

#: Version tag for the diagnostic content fingerprint.
CONTENT_FINGERPRINT_VERSION = "v1"


def derive_idempotency_key(
    project_id: str,
    request_id: str,
    operation_index: int,
) -> str:
    """Derive the deterministic MUTATION IDENTITY of a job.

    Deliberately excludes the payload. A content-derived key would conflate two
    different situations::

        "Move Cube 50 cm right."        (request A)
        "Move Cube 50 cm right again."  (request B, later)

    Both resolve to the identical payload {x: 0.5, y: 0, z: 0}, yet they are two
    intentional mutations and must both execute. Identity therefore comes from the
    originating submission, not from what the operation happens to look like.

    Scoped to a project, so the same ``request_id`` in two projects yields two
    identities and project isolation holds.

    Retry semantics: resubmitting ``req_abc123`` operation 0 derives the same key,
    so the queue layer resolves it to the existing job and Blender is mutated
    once. A new submission ``req_xyz789`` operation 0 derives a different key and
    executes again, even with a byte-identical payload.
    """
    if isinstance(operation_index, bool) or not isinstance(operation_index, int):
        raise ValueError("operation_index must be a non-negative integer")
    if operation_index < 0:
        raise ValueError("operation_index must be a non-negative integer")
    canonical = canonicalize(
        {
            "v": IDEMPOTENCY_VERSION,
            "project_id": project_id,
            "request_id": request_id,
            "operation_index": operation_index,
        }
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"idem_{digest}"


def derive_content_fingerprint(job_type: JobType, payload: Any) -> str:
    """Derive a DIAGNOSTIC fingerprint of what the job does.

    Useful for spotting repeated or accidentally duplicated operations in logs and
    tests. This must never be used as the mutation identity: identical content
    from two distinct requests is legitimate and has to execute twice.
    """
    canonical = canonicalize(
        {
            "v": CONTENT_FINGERPRINT_VERSION,
            "job_type": job_type,
            "payload": (
                to_wire(payload) if dataclasses.is_dataclass(payload) else payload
            ),
        }
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"fp_{digest}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class JobValidationResult:
    valid: bool
    errors: list[ChatError]


def _invalid(errors: list[ChatError]) -> JobValidationResult:
    return JobValidationResult(valid=False, errors=errors)


def is_valid_object_ref(target: Any) -> bool:
    """True when the object reference identifies something usable."""
    if isinstance(target, Mapping):
        object_id = target.get("object_id")
        name = target.get("name")
    elif hasattr(target, "object_id") or hasattr(target, "name"):
        object_id = getattr(target, "object_id", None)
        name = getattr(target, "name", None)
    else:
        return False
    return is_non_empty_string(object_id) or is_non_empty_string(name)


def validate_job(job: Any) -> JobValidationResult:
    """Validate a job against the canonical schema plus the rules JSON Schema
    cannot express (non-finite numbers, and the unresolved-language guard).
    """
    errors: list[ChatError] = []

    if isinstance(job, Mapping):
        wire: dict[str, Any] = dict(job)
    elif dataclasses.is_dataclass(job) and not isinstance(job, type):
        wire = to_wire(job)
    else:
        return _invalid(
            [ChatError(code="VALIDATION_ERROR", message="job must be an object")]
        )

    # Project isolation is checked explicitly so the failure is unambiguous.
    if not is_non_empty_string(wire.get("project_id")):
        errors.append(
            ChatError(
                code="VALIDATION_ERROR", message=JOB_MESSAGES["MISSING_PROJECT_ID"]
            )
        )

    schema_result = validate_against_schema(SCHEMA_FILES["Job"], to_job_wire(wire))
    if not schema_result.valid:
        for violation in schema_result.violations:
            errors.append(
                ChatError(
                    code="VALIDATION_ERROR",
                    message=f"{violation.path or 'job'}: {violation.message}",
                )
            )

    # Unresolved-language guard, reported precisely.
    payload = wire.get("payload")
    if isinstance(payload, Mapping):
        offending = sorted(k for k in payload if k in UNRESOLVED_PAYLOAD_KEYS)
        if offending:
            errors.append(
                ChatError(
                    code="VALIDATION_ERROR",
                    message=(
                        f"{JOB_MESSAGES['UNRESOLVED_PAYLOAD']} "
                        f"(found: {', '.join(offending)})"
                    ),
                )
            )

    # Operation-specific runtime rules.
    if wire.get("job_type") == "move_object" and isinstance(payload, Mapping):
        if not is_valid_object_ref(payload.get("target")):
            errors.append(
                ChatError(
                    code="VALIDATION_ERROR", message=JOB_MESSAGES["INVALID_TARGET"]
                )
            )
        if not is_finite_vec3(payload.get("delta_meters")):
            errors.append(
                ChatError(
                    code="INVALID_UNITS", message=JOB_MESSAGES["NON_FINITE_DELTA"]
                )
            )

    return JobValidationResult(valid=True, errors=[]) if not errors else _invalid(errors)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@dataclass
class CreateJobResult:
    ok: bool
    job: Optional[Job] = None
    errors: list[ChatError] = dataclasses.field(default_factory=list)


def create_move_object_job(
    job_id: str,
    project_id: str,
    session_id: str,
    user_id: str,
    request_id: str,
    target: ObjectRef,
    delta_meters: Vec3,
    created_at: str,
    operation_index: int = 0,
) -> CreateJobResult:
    """Assemble a queued move_object job.

    Deterministic by construction: ``job_id``, ``created_at``, and the originating
    ``request_id`` are supplied by the caller rather than generated here, so no
    clock or randomness leaks into the contract layer and tests are reproducible.

    The delta must already be resolved — this function accepts meters only. There
    is no code path here that takes a unit or a direction.
    """
    if not is_finite_vec3(delta_meters):
        return CreateJobResult(
            ok=False,
            errors=[
                ChatError(
                    code="INVALID_UNITS", message=JOB_MESSAGES["NON_FINITE_DELTA"]
                )
            ],
        )

    if not is_non_empty_string(request_id):
        return CreateJobResult(
            ok=False,
            errors=[
                ChatError(
                    code="VALIDATION_ERROR",
                    message=JOB_MESSAGES["MISSING_REQUEST_ID"],
                )
            ],
        )

    if (
        isinstance(operation_index, bool)
        or not isinstance(operation_index, int)
        or operation_index < 0
    ):
        return CreateJobResult(
            ok=False,
            errors=[
                ChatError(
                    code="VALIDATION_ERROR",
                    message=JOB_MESSAGES["INVALID_OPERATION_INDEX"],
                )
            ],
        )

    payload = MoveObjectPayload(target=target, delta_meters=delta_meters)
    origin = RequestOrigin(request_id=request_id, operation_index=operation_index)

    job = Job(
        job_id=job_id,
        job_type="move_object",
        project_id=project_id,
        session_id=session_id,
        user_id=user_id,
        payload=payload,
        origin=origin,
        status="queued",
        idempotency_key=derive_idempotency_key(
            project_id=project_id,
            request_id=origin.request_id,
            operation_index=origin.operation_index,
        ),
        created_at=created_at,
        content_fingerprint=derive_content_fingerprint("move_object", payload),
    )

    validation = validate_job(job)
    if not validation.valid:
        return CreateJobResult(ok=False, errors=validation.errors)
    return CreateJobResult(ok=True, job=job)


# ---------------------------------------------------------------------------
# Lifecycle transitions (pure; no queue, no persistence)
# ---------------------------------------------------------------------------

#: Allowed lifecycle transitions.
ALLOWED_TRANSITIONS: dict[JobStatus, tuple[JobStatus, ...]] = {
    "queued": ("claimed", "failed"),
    "claimed": ("running", "failed"),
    "running": ("succeeded", "failed"),
    "succeeded": (),
    "failed": (),
}


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_JOB_STATUSES


def can_transition(from_status: JobStatus, to_status: JobStatus) -> bool:
    return to_status in ALLOWED_TRANSITIONS.get(from_status, ())


# ---------------------------------------------------------------------------
# Duplicate queue delivery (pure decision logic; no queue, no persistence)
# ---------------------------------------------------------------------------

#: What a worker should do when a job is delivered to it.
#:
#: ``job_id`` is the execution identity. A queue may deliver the same job_id more
#: than once (at-least-once delivery, redelivery after a lost ack, a duplicated
#: message). The worker must decide from the job's recorded state, not from the
#: delivery itself, so a redelivery never mutates Blender twice.
DeliveryDecision = Literal["execute", "already_owned", "reuse_result"]


def classify_delivery(recorded_status: Optional[JobStatus]) -> DeliveryDecision:
    """Classify a delivery from the currently recorded state of that job_id.

      - not recorded / queued -> execute (this is the first real attempt)
      - claimed / running     -> already_owned (another worker/attempt holds it)
      - succeeded / failed    -> reuse_result (terminal; never re-execute)

    Pure and deterministic. Enforcement (lease expiry, reclaim of an abandoned
    claim) belongs to the worker/queue task; this fixes the decision rule.
    """
    if recorded_status is None:
        return "execute"
    if recorded_status == "queued":
        return "execute"
    if recorded_status in ("claimed", "running"):
        return "already_owned"
    return "reuse_result"


def is_duplicate_delivery(recorded_status: Optional[JobStatus]) -> bool:
    """True when a delivery must not cause a Blender mutation."""
    return classify_delivery(recorded_status) != "execute"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def to_job_wire(job: Any) -> dict[str, Any]:
    """Convert a job into a plain wire document, dropping unset optionals."""
    if dataclasses.is_dataclass(job) and not isinstance(job, type):
        return to_wire(job)
    if isinstance(job, Mapping):
        return {k: to_wire(v) for k, v in job.items() if v is not None}
    raise ValueError("job must be a dataclass or mapping")


def serialize_job(job: Job) -> str:
    """Serialize a job to a canonical JSON string."""
    return json.dumps(to_job_wire(job), sort_keys=True, separators=(",", ":"))


@dataclass
class ParseJobResult:
    ok: bool
    job: Optional[dict[str, Any]] = None
    errors: list[ChatError] = dataclasses.field(default_factory=list)


def parse_job(serialized: str) -> ParseJobResult:
    """Parse and validate a job from its wire representation."""
    try:
        parsed = json.loads(serialized)
    except (ValueError, TypeError):
        return ParseJobResult(
            ok=False,
            errors=[
                ChatError(code="VALIDATION_ERROR", message="job is not valid JSON")
            ],
        )
    validation = validate_job(parsed)
    if not validation.valid:
        return ParseJobResult(ok=False, errors=validation.errors)
    return ParseJobResult(ok=True, job=parsed)


def job_from_wire(wire: Mapping[str, Any]) -> Job:
    """Rebuild a typed Job (with typed payload) from a wire document.

    Raises if the wire document is not a valid job, so callers cannot construct a
    half-typed job by accident.
    """
    validation = validate_job(wire)
    if not validation.valid:
        raise ValueError(f"invalid job: {validation.errors}")

    payload_wire = wire["payload"]
    job_type = wire["job_type"]
    if job_type == "move_object":
        payload: Any = MoveObjectPayload(
            target=ObjectRef(**payload_wire["target"]),
            delta_meters=Vec3(**payload_wire["delta_meters"]),
        )
    elif job_type == "inspect_scene":
        # A READ job. Its payload is empty and closed by contract, so there is
        # nothing to rebuild; the schema has already refused any smuggled field.
        payload = InspectScenePayload()
    else:  # pragma: no cover - unreachable while the enum is exhausted above
        raise ValueError(JOB_MESSAGES["UNSUPPORTED_JOB_TYPE"])

    claim_wire = wire.get("claim")
    error_wire = wire.get("error")
    return Job(
        job_id=wire["job_id"],
        job_type=job_type,
        project_id=wire["project_id"],
        session_id=wire["session_id"],
        user_id=wire["user_id"],
        payload=payload,
        origin=RequestOrigin(**wire["origin"]),
        status=wire["status"],
        idempotency_key=wire["idempotency_key"],
        created_at=wire["created_at"],
        content_fingerprint=wire.get("content_fingerprint"),
        claim=JobClaim(**claim_wire) if claim_wire else None,
        error=ChatError(**error_wire) if error_wire else None,
        result=wire.get("result"),
    )


# ---------------------------------------------------------------------------
# Persistence boundary (INTERFACE ONLY — implemented in a later task)
# ---------------------------------------------------------------------------


class JobStore(Protocol):
    """The durable job store boundary.

    Deliberately a Protocol with no implementation: Redis/PostgreSQL wiring
    belongs to the queue/persistence task. Defining it now keeps the logical
    boundary explicit from the start (see .kiro/steering/structure.md).

    Duplicate-job protection contract that an implementation MUST honour:

      1. ``find_by_idempotency_key`` is scoped to a project. Keys are never
         compared across projects, preserving project isolation.
      2. ``submit`` must be atomic: if a job with the same (project_id,
         idempotency_key) already exists, return that job and enqueue nothing.
      3. A duplicate submission must never produce a second Blender mutation.
    """

    def get(self, project_id: str, job_id: str) -> Optional[Job]: ...

    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> Optional[Job]: ...

    def submit(self, job: Job) -> tuple[Job, bool]:
        """Atomically insert, or return the existing job for the same key.

        Returns ``(job, duplicate)``.
        """
        ...


class JobClaimer(Protocol):
    """The worker-facing claiming boundary.

    Protocol only. The claiming loop, leases, and reclaim-after-expiry behaviour
    belong to the worker task; this fixes the shape they must speak.
    """

    def claim(self, worker_id: str, project_id: str) -> Optional[Job]: ...

    def renew(self, project_id: str, job_id: str, claim: JobClaim) -> None: ...

    def release(self, project_id: str, job_id: str) -> None: ...


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CONTENT_FINGERPRINT_VERSION",
    "DeliveryDecision",
    "IDEMPOTENCY_VERSION",
    "JOB_MESSAGES",
    "UNRESOLVED_PAYLOAD_KEYS",
    "CreateJobResult",
    "JobClaimer",
    "JobStore",
    "JobValidationResult",
    "ParseJobResult",
    "can_transition",
    "canonicalize",
    "classify_delivery",
    "create_move_object_job",
    "derive_content_fingerprint",
    "derive_idempotency_key",
    "encode_float64",
    "is_duplicate_delivery",
    "is_terminal",
    "is_valid_object_ref",
    "job_from_wire",
    "parse_job",
    "serialize_job",
    "to_job_wire",
    "validate_job",
]
