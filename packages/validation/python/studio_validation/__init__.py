"""Reusable validation rules for AI 3D Design Studio — Python representation.

These rules implement the runtime enforcement of the canonical contract schemas
in packages/contracts/schemas. The canonical schema is authoritative; conformance
tests cross-check these rules against it over a shared, language-neutral case
corpus (schemas/conformance-cases.json) so the two language implementations
cannot drift apart or away from the schema.

Covered by Spec 001, Task 1:
  - project_id presence on inbound requests  (chat-request.schema.json)
  - finite meter values for distances/coordinates (vec3.schema.json + the
    finite-meters rule, which JSON Schema cannot express since JSON has no
    NaN/Infinity literals)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ValidationError:
    field: str
    message: str


@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)


def _ok() -> ValidationResult:
    return ValidationResult(valid=True, errors=[])


def _fail(errors: list[ValidationError]) -> ValidationResult:
    return ValidationResult(valid=False, errors=errors)


def is_non_empty_string(value: Any) -> bool:
    """A non-empty, non-whitespace string."""
    return isinstance(value, str) and len(value.strip()) > 0


def is_finite_meters(value: Any) -> bool:
    """A real, finite number in meters.

    Rejects NaN, inf, -inf, bools, and non-numbers. ``bool`` is excluded
    because ``isinstance(True, int)`` is True in Python.
    """
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def is_finite_vec3(value: Any) -> bool:
    """Every component of a Vec3 must be finite meters."""
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    z = getattr(value, "z", None)
    if x is None and isinstance(value, Mapping):
        x, y, z = value.get("x"), value.get("y"), value.get("z")
    return is_finite_meters(x) and is_finite_meters(y) and is_finite_meters(z)


#: The canonical ChatRequest property set (chat-request.schema.json declares
#: ``additionalProperties: false``). Declared locally so this module stays pure;
#: conformance tests assert it equals the canonical schema's property list, so it
#: cannot drift.
CHAT_REQUEST_FIELDS: tuple[str, ...] = (
    "message",
    "project_id",
    "request_id",
    "selected_object_id",
    "session_id",
)


def validate_chat_request(input: Any) -> ValidationResult:
    """Validate an inbound chat request against the canonical contract.

    Enforces Requirement 2.3: missing project_id is rejected. Also requires
    request_id (the stable submission identity that makes retry semantics
    possible). Rejects unknown properties to match the canonical schema. Accepts
    either a mapping (raw JSON body) or an object with the expected attributes.
    """
    if isinstance(input, Mapping):
        get = input.get
        keys = list(input.keys())
    elif hasattr(input, "project_id"):
        get = lambda k, default=None: getattr(input, k, default)  # noqa: E731
        keys = []
    else:
        return _fail([ValidationError("_root", "request body must be an object")])

    errors: list[ValidationError] = []

    if not is_non_empty_string(get("request_id")):
        errors.append(ValidationError("request_id", "request_id is required"))
    if not is_non_empty_string(get("project_id")):
        errors.append(ValidationError("project_id", "project_id is required"))
    if not is_non_empty_string(get("session_id")):
        errors.append(ValidationError("session_id", "session_id is required"))
    if not is_non_empty_string(get("message")):
        errors.append(ValidationError("message", "message is required"))

    selected = get("selected_object_id")
    if selected is not None and not is_non_empty_string(selected):
        errors.append(
            ValidationError(
                "selected_object_id",
                "selected_object_id, when provided, must be a non-empty string",
            )
        )

    for key in keys:
        if key not in CHAT_REQUEST_FIELDS:
            errors.append(
                ValidationError(key, f"unknown property {key!r} is not allowed")
            )

    return _ok() if not errors else _fail(errors)


def validate_meter_deltas(
    delta_x_m: Any = None,
    delta_y_m: Any = None,
    delta_z_m: Any = None,
) -> ValidationResult:
    """Validate movement deltas in meters (finite-meters rule)."""
    errors: list[ValidationError] = []
    for name, value in (
        ("delta_x_m", delta_x_m),
        ("delta_y_m", delta_y_m),
        ("delta_z_m", delta_z_m),
    ):
        if value is not None and not is_finite_meters(value):
            errors.append(
                ValidationError(name, f"{name} must be a finite number of meters")
            )
    return _ok() if not errors else _fail(errors)

# --- model-authored code risk classification -------------------------------
#
# Lives here because both the control plane (which asks the user for approval) and the
# worker (which refuses unapproved code) need it, and neither service may import the
# other.
from .code_risk import (  # noqa: E402
    APPROVAL_REQUIRED,
    AUTO,
    REFUSED,
    RiskAssessment,
    RiskFinding,
    classify_python,
)
