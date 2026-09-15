"""The MCP exposure boundary.

Spec 001, Task 4.

This is the only layer that knows about MCP. It does three things and nothing
else: describe the tool, decode a wire request into typed domain objects, and
encode the typed result back to the wire. All decisions live in
``tools/move_object.py``.

    MCP handler (this file)
        v
    move_object service
        v
    SceneAdapter
        v
    bpy

No SDK dependency
-----------------
No MCP SDK is installed and none is added: the handler is a plain
dict-in/dict-out function. When a transport is wired up (stdio or SSE), the SDK
server calls ``handle_move_object`` and the domain layer stays untouched. Keeping
the boundary dependency-free also means the domain tests never import transport
code.

Security (see .kiro/steering/security.md)
-----------------------------------------
Exactly one semantic tool is exposed. There is deliberately no execute_python,
no eval, no scene query language, and no shell access. This module also does not
open a socket or bind a port — nothing here exposes Blender to a network.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Mapping, Optional

from studio_contracts import (
    SCHEMA_FILES,
    ChatError,
    load_schema,
    to_wire,
    validate_against_schema,
)
from studio_types import MoveObjectPlan, MoveObjectResult, ObjectRef, Vec3

from .adapters.scene import SceneAdapter
from .tools.move_object import move_object

#: The single tool this service exposes.
MOVE_OBJECT_TOOL_NAME = "move_object"

MOVE_OBJECT_TOOL_DESCRIPTION = (
    "Move a scene object to an absolute world-space position in meters, using a "
    "retry-safe plan. Requires expected_before_meters and desired_after_meters "
    "so a replay after a crash cannot move the object twice. Coordinates are "
    "canonical meters; units and directions must already be resolved."
)


def move_object_tool_descriptor() -> dict[str, Any]:
    """The MCP tool descriptor, with its input schema taken from the canonical
    contract rather than restated here (so the two cannot drift apart).
    """
    return {
        "name": MOVE_OBJECT_TOOL_NAME,
        "description": MOVE_OBJECT_TOOL_DESCRIPTION,
        "input_schema": load_schema(SCHEMA_FILES["MoveObjectPlan"]),
        "output_schema": load_schema(SCHEMA_FILES["MoveObjectResult"]),
    }


def _error_envelope(code: str, message: str, request: Mapping[str, Any]) -> dict:
    """A wire-shaped failure for requests too malformed to build a plan from."""
    delta = request.get("delta_meters")
    return {
        "job_id": str(request.get("job_id") or "unknown"),
        "target": request.get("target") if isinstance(request.get("target"), dict) else {},
        "requested_delta_meters": (
            delta if isinstance(delta, dict) else {"x": 0.0, "y": 0.0, "z": 0.0}
        ),
        "applied": False,
        "already_applied": False,
        "verified": False,
        "error": {"code": code, "message": message},
    }


def _vec3_from(value: Any) -> Optional[Vec3]:
    if not isinstance(value, Mapping):
        return None
    try:
        return Vec3(
            x=float(value["x"]), y=float(value["y"]), z=float(value["z"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def plan_from_wire(request: Mapping[str, Any]) -> Optional[MoveObjectPlan]:
    """Decode a wire request into a typed plan, or None if undecodable."""
    if not isinstance(request, Mapping):
        return None
    target = request.get("target")
    if not isinstance(target, Mapping):
        return None
    before = _vec3_from(request.get("expected_before_meters"))
    delta = _vec3_from(request.get("delta_meters"))
    after = _vec3_from(request.get("desired_after_meters"))
    job_id = request.get("job_id")
    if before is None or delta is None or after is None:
        return None
    if not isinstance(job_id, str) or not job_id.strip():
        return None
    return MoveObjectPlan(
        job_id=job_id,
        target=ObjectRef(
            object_id=target.get("object_id"), name=target.get("name")
        ),
        expected_before_meters=before,
        delta_meters=delta,
        desired_after_meters=after,
    )


def result_to_wire(result: MoveObjectResult) -> dict[str, Any]:
    """Encode a typed result as a canonical wire document."""
    wire = to_wire(result)
    # to_wire drops None fields, which is exactly what the schema expects
    # (absent rather than null).
    return wire


def handle_move_object(
    request: Mapping[str, Any],
    scene: SceneAdapter,
    validate: bool = True,
) -> dict[str, Any]:
    """Handle one move_object MCP call.

    Validates the request against the canonical plan schema, delegates the
    decision to the domain service, and returns a canonical result document.
    Never raises: transport-level failures are returned as structured errors so a
    caller always receives a machine-readable outcome.
    """
    if validate:
        schema_result = validate_against_schema(
            SCHEMA_FILES["MoveObjectPlan"], request
        )
        if not schema_result.valid:
            detail = "; ".join(
                f"{v.path or 'request'}: {v.message}"
                for v in schema_result.violations
            )
            return _error_envelope(
                "VALIDATION_ERROR",
                f"move_object request does not match its contract ({detail})",
                request if isinstance(request, Mapping) else {},
            )

    plan = plan_from_wire(request)
    if plan is None:
        return _error_envelope(
            "VALIDATION_ERROR",
            "move_object request could not be decoded into an execution plan",
            request if isinstance(request, Mapping) else {},
        )

    try:
        result = move_object(plan, scene)
    except Exception as exc:  # pragma: no cover - defensive boundary
        # An unexpected exception must not escape the MCP boundary as a stack
        # trace; convert it into a structured internal error.
        return _error_envelope(
            "INTERNAL_ERROR",
            f"unexpected failure during move_object: {exc}",
            request,
        )

    return result_to_wire(result)


#: The tool registry this service exposes. A transport binds these names to
#: handlers; nothing else is reachable.
TOOL_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    MOVE_OBJECT_TOOL_NAME: handle_move_object,
}


def list_tools() -> list[dict[str, Any]]:
    """Every tool this service exposes. Exactly one, by design."""
    return [move_object_tool_descriptor()]
