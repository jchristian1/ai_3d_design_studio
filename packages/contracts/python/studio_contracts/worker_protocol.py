"""The worker <-> control-plane protocol.

Spec 001, Task 8. Shared by both ends of the link, so neither side can invent a
message the other does not understand.

Everything here is pure data: building, parsing, validating and redacting
messages. There is no socket, no asyncio, and no Blender — transport lives in the
worker and control-plane services.

Security posture
----------------
All network input is untrusted. ``parse`` validates against the canonical
``worker-message.schema.json`` before a caller sees a message, and the schema's
closed ``type`` enum plus ``additionalProperties: false`` at every level mean the
protocol simply cannot express a shell command, a Python expression, a filesystem
path, or a bpy call.

The token appears on exactly one message type (``worker_hello``) and is stripped
by ``redact`` before anything is logged. A schema conditional forbids a token on
any other message.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import SCHEMA_FILES, validate_against_schema

#: Current protocol version. Bump on any breaking vocabulary change.
#:
#: v2 added the optional ``preview`` / ``preview_error`` fields to ``job_result``
#: (Task 10). Because every message is validated with ``additionalProperties:
#: false``, a peer that predates a new field REJECTS messages carrying it — so an
#: additive optional field is still a vocabulary change and still bumps the
#: version. Silently relying on "optional means compatible" would produce
#: mysterious validation failures against an older peer.
PROTOCOL_VERSION = 2

#: Versions this build can speak. A worker outside this set is rejected.
#:
#: v1 remains supported: a v1 worker simply never reports a preview, which is a
#: degraded but entirely valid worker. The control plane is therefore free to be
#: upgraded before the workstations are.
SUPPORTED_PROTOCOL_VERSIONS: tuple[int, ...] = (1, 2)

TOKEN_FIELD = "token"
REDACTED = "***redacted***"

# Message types
WORKER_HELLO = "worker_hello"
WORKER_REGISTERED = "worker_registered"
WORKER_REJECTED = "worker_rejected"
HEARTBEAT = "heartbeat"
HEARTBEAT_ACK = "heartbeat_ack"
JOB_OFFER = "job_offer"
JOB_ACCEPTED = "job_accepted"
JOB_REJECTED = "job_rejected"
JOB_PROGRESS = "job_progress"
JOB_RESULT = "job_result"
PING = "ping"
PONG = "pong"


class ProtocolError(ValueError):
    """A message could not be parsed or does not satisfy the contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Validation and redaction
# ---------------------------------------------------------------------------


def validate(message: Mapping[str, Any]) -> list[str]:
    """Return contract violations for a message, empty when valid."""
    result = validate_against_schema(SCHEMA_FILES["WorkerMessage"], message)
    return [f"{v.path or 'message'}: {v.message}" for v in result.violations]


def parse(raw: Any) -> dict[str, Any]:
    """Parse and validate untrusted input into a message.

    Accepts a JSON string or a mapping. Raises ProtocolError for anything that is
    not a contract-valid message — callers never see a partially trusted message.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("message is not valid UTF-8") from exc
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ProtocolError("message is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise ProtocolError("message must be a JSON object")

    problems = validate(raw)
    if problems:
        raise ProtocolError(
            "message does not satisfy the worker protocol: " + "; ".join(problems)
        )
    return dict(raw)


def encode(message: Mapping[str, Any]) -> str:
    """Serialize a message for the wire, validating it first."""
    problems = validate(message)
    if problems:
        raise ProtocolError(
            "refusing to send an invalid message: " + "; ".join(problems)
        )
    return json.dumps(dict(message), sort_keys=True, separators=(",", ":"))


def redact(message: Mapping[str, Any]) -> dict[str, Any]:
    """A copy safe to log: the token is replaced, never included."""
    safe = dict(message)
    if TOKEN_FIELD in safe:
        safe[TOKEN_FIELD] = REDACTED
    return safe


def tokens_match(presented: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time token comparison.

    ``hmac.compare_digest`` avoids leaking the token's length or contents through
    timing. A missing or blank value on either side never matches.
    """
    if not isinstance(presented, str) or not isinstance(expected, str):
        return False
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


def protocol_version_supported(version: Any) -> bool:
    return isinstance(version, int) and version in SUPPORTED_PROTOCOL_VERSIONS


# ---------------------------------------------------------------------------
# Builders — the only sanctioned way to construct messages
# ---------------------------------------------------------------------------


def _base(message_type: str) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": message_type,
        "sent_at": utc_now(),
    }


def worker_hello(
    worker_id: str,
    token: str,
    capabilities: Mapping[str, Any],
    protocol_version: int = PROTOCOL_VERSION,
) -> dict[str, Any]:
    message = _base(WORKER_HELLO)
    message.update(
        {
            "protocol_version": protocol_version,
            "worker_id": worker_id,
            "token": token,
            "capabilities": dict(capabilities),
        }
    )
    return message


def worker_registered(
    worker_id: str, connection_id: str, heartbeat_interval_seconds: float
) -> dict[str, Any]:
    message = _base(WORKER_REGISTERED)
    message.update(
        {
            "worker_id": worker_id,
            "connection_id": connection_id,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
        }
    )
    return message


def worker_rejected(
    code: str, reason: str, worker_id: Optional[str] = None
) -> dict[str, Any]:
    message = _base(WORKER_REJECTED)
    message["error"] = {"code": code, "message": reason}
    if worker_id:
        message["worker_id"] = worker_id
    return message


def heartbeat(worker_id: str, worker_state: str) -> dict[str, Any]:
    message = _base(HEARTBEAT)
    message.update({"worker_id": worker_id, "worker_state": worker_state})
    return message


def heartbeat_ack(worker_id: str) -> dict[str, Any]:
    message = _base(HEARTBEAT_ACK)
    message["worker_id"] = worker_id
    return message


def job_offer(job: Mapping[str, Any]) -> dict[str, Any]:
    """Offer a canonical Job. The job document is passed through unchanged."""
    message = _base(JOB_OFFER)
    message.update(
        {
            "job": dict(job),
            "job_id": job["job_id"],
            "project_id": job["project_id"],
        }
    )
    return message


def job_accepted(worker_id: str, job_id: str, project_id: str) -> dict[str, Any]:
    message = _base(JOB_ACCEPTED)
    message.update(
        {"worker_id": worker_id, "job_id": job_id, "project_id": project_id}
    )
    return message


def job_rejected(
    worker_id: str, job_id: str, project_id: str, code: str, reason: str
) -> dict[str, Any]:
    message = _base(JOB_REJECTED)
    message.update(
        {
            "worker_id": worker_id,
            "job_id": job_id,
            "project_id": project_id,
            "error": {"code": code, "message": reason},
        }
    )
    return message


def job_progress(
    worker_id: str, job_id: str, project_id: str, execution_phase: str
) -> dict[str, Any]:
    message = _base(JOB_PROGRESS)
    message.update(
        {
            "worker_id": worker_id,
            "job_id": job_id,
            "project_id": project_id,
            "execution_phase": execution_phase,
        }
    )
    return message


def job_result(
    worker_id: str,
    job_id: str,
    project_id: str,
    job_status: str,
    result: Any = None,
    error: Optional[Mapping[str, Any]] = None,
    execution_phase: Optional[str] = None,
    preview: Optional[Mapping[str, Any]] = None,
    preview_error: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Report the outcome of one execution.

    ``preview`` and ``preview_error`` are deliberately SEPARATE from ``result``
    and ``error``. A preview is a reporting addition to a mutation that already
    happened, so a failed preview must never be able to make a successful,
    durably-saved design change look like a failure.
    """
    message = _base(JOB_RESULT)
    message.update(
        {
            "worker_id": worker_id,
            "job_id": job_id,
            "project_id": project_id,
            "job_status": job_status,
        }
    )
    if result is not None:
        message["result"] = result
    if error is not None:
        message["error"] = dict(error)
    if execution_phase is not None:
        message["execution_phase"] = execution_phase
    if preview is not None:
        message["preview"] = dict(preview)
    if preview_error is not None:
        message["preview_error"] = dict(preview_error)
    return message


def ping() -> dict[str, Any]:
    return _base(PING)


def pong() -> dict[str, Any]:
    return _base(PONG)


__all__ = [
    "HEARTBEAT",
    "HEARTBEAT_ACK",
    "JOB_ACCEPTED",
    "JOB_OFFER",
    "JOB_PROGRESS",
    "JOB_REJECTED",
    "JOB_RESULT",
    "PING",
    "PONG",
    "PROTOCOL_VERSION",
    "REDACTED",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOKEN_FIELD",
    "WORKER_HELLO",
    "WORKER_REGISTERED",
    "WORKER_REJECTED",
    "ProtocolError",
    "encode",
    "heartbeat",
    "heartbeat_ack",
    "job_accepted",
    "job_offer",
    "job_progress",
    "job_rejected",
    "job_result",
    "parse",
    "ping",
    "pong",
    "protocol_version_supported",
    "redact",
    "tokens_match",
    "validate",
    "worker_hello",
    "worker_registered",
    "worker_rejected",
]
