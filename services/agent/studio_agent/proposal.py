"""Validate a model's structured response into an ``AgentOutcome``.

Model output is UNTRUSTED. This module is the only place it becomes something the
platform will act on, and it fails closed: an unknown capability, a missing argument, a
non-finite number, an unexpected key, or a malformed body all end the turn with a
structured error and zero jobs.

Capability arguments are declared here as a table rather than as ad-hoc checks, so adding
a capability means adding a row and cannot mean forgetting a validation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from studio_contracts import ChatError, validate_against_schema
from studio_contracts.capabilities import is_proposable_capability

from .outcome import (
    AgentError,
    AgentOutcome,
    Answer,
    Clarification,
    PlanProposal,
    ProviderMetadata,
    ValidatedOperation,
)

RESPONSE_SCHEMA = "agent-response.schema.json"

VALIDATION_ERROR = "VALIDATION_ERROR"

#: A plan larger than this is treated as a runaway rather than a design.
MAX_OPERATIONS = 200
#: Model-authored code larger than this is refused before classification.
MAX_CODE_CHARACTERS = 60_000
MAX_LABEL_CHARACTERS = 160


class ArgumentError(ValueError):
    """Raised when a capability's arguments are not usable."""


# --------------------------------------------------------------------------
# argument shapes
# --------------------------------------------------------------------------

VEC3 = "vec3"
VEC2 = "vec2"
RGBA = "rgba"
NUMBER = "number"
POSITIVE = "positive"
TEXT = "text"
CODE = "code"
POLYGON = "polygon"


@dataclass(frozen=True)
class ArgumentSpec:
    required: tuple[tuple[str, str], ...] = ()
    optional: tuple[tuple[str, str], ...] = ()
    #: At least one of these must be present (object identity).
    identity: tuple[str, ...] = ()


_IDENTITY = ("object_id", "name")

SPECS: Mapping[str, ArgumentSpec] = {
    "inspect_scene": ArgumentSpec(),
    "inspect_object": ArgumentSpec(identity=_IDENTITY),
    "move_object": ArgumentSpec(
        required=(("desired_position_meters", VEC3),), identity=_IDENTITY
    ),
    "rotate_object": ArgumentSpec(
        required=(("desired_rotation_radians", VEC3),), identity=_IDENTITY
    ),
    "scale_object": ArgumentSpec(required=(("desired_scale", VEC3),), identity=_IDENTITY),
    "set_object_dimensions": ArgumentSpec(
        required=(("desired_dimensions_meters", VEC3),), identity=_IDENTITY
    ),
    "create_object": ArgumentSpec(
        required=(("primitive", TEXT), ("display_name", TEXT), ("position_meters", VEC3)),
        optional=(("dimensions_meters", VEC3), ("object_id", TEXT)),
    ),
    "duplicate_object": ArgumentSpec(
        required=(("display_name", TEXT),),
        optional=(("offset_meters", VEC3), ("new_object_id", TEXT)),
        identity=_IDENTITY,
    ),
    "delete_object": ArgumentSpec(identity=_IDENTITY),
    "set_material_color": ArgumentSpec(
        required=(("color_linear_srgb", RGBA),), identity=_IDENTITY
    ),
    "create_wall": ArgumentSpec(
        required=(
            ("display_name", TEXT),
            ("start_meters", VEC2),
            ("end_meters", VEC2),
            ("height_meters", POSITIVE),
            ("thickness_meters", POSITIVE),
        ),
        optional=(
            ("base_elevation_meters", NUMBER),
            ("object_id", TEXT),
            ("color_linear_srgb", RGBA),
        ),
    ),
    "create_floor": ArgumentSpec(
        required=(
            ("display_name", TEXT),
            ("footprint_meters", POLYGON),
            ("thickness_meters", POSITIVE),
        ),
        optional=(
            ("elevation_meters", NUMBER),
            ("object_id", TEXT),
            ("color_linear_srgb", RGBA),
        ),
    ),
    "create_ceiling": ArgumentSpec(
        required=(
            ("display_name", TEXT),
            ("footprint_meters", POLYGON),
            ("thickness_meters", POSITIVE),
        ),
        optional=(
            ("elevation_meters", NUMBER),
            ("object_id", TEXT),
            ("color_linear_srgb", RGBA),
        ),
    ),
    "create_opening": ArgumentSpec(
        required=(
            ("centre_meters", VEC3),
            ("width_meters", POSITIVE),
            ("height_meters", POSITIVE),
        ),
        optional=(("wall_thickness_meters", POSITIVE),),
        identity=("wall_object_id", "wall_name"),
    ),
    "create_door_placeholder": ArgumentSpec(
        required=(
            ("display_name", TEXT),
            ("centre_meters", VEC3),
            ("width_meters", POSITIVE),
            ("height_meters", POSITIVE),
        ),
        optional=(
            ("depth_meters", POSITIVE),
            ("rotation_z_radians", NUMBER),
            ("object_id", TEXT),
            ("color_linear_srgb", RGBA),
        ),
    ),
    "create_window_placeholder": ArgumentSpec(
        required=(
            ("display_name", TEXT),
            ("centre_meters", VEC3),
            ("width_meters", POSITIVE),
            ("height_meters", POSITIVE),
        ),
        optional=(
            ("depth_meters", POSITIVE),
            ("rotation_z_radians", NUMBER),
            ("object_id", TEXT),
            ("color_linear_srgb", RGBA),
        ),
    ),
    "execute_blender_python": ArgumentSpec(required=(("code", CODE),)),
}


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArgumentError(f"{field} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ArgumentError(f"{field} must be finite")
    return number


def _coerce(kind: str, value: Any, field: str) -> Any:
    if kind == NUMBER:
        return _finite(value, field)
    if kind == POSITIVE:
        number = _finite(value, field)
        if number <= 0.0:
            raise ArgumentError(f"{field} must be greater than zero")
        return number
    if kind == TEXT:
        if not isinstance(value, str) or not value.strip():
            raise ArgumentError(f"{field} must be a non-empty string")
        return value
    if kind == CODE:
        if not isinstance(value, str) or not value.strip():
            raise ArgumentError(f"{field} must be a non-empty string")
        if len(value) > MAX_CODE_CHARACTERS:
            raise ArgumentError(f"{field} is too long")
        return value
    if kind in (VEC2, VEC3):
        if not isinstance(value, Mapping):
            raise ArgumentError(f"{field} must be an object")
        axes = ("x", "y") if kind == VEC2 else ("x", "y", "z")
        unexpected = set(value) - set(axes)
        if unexpected:
            raise ArgumentError(f"{field} has unexpected keys: {sorted(unexpected)}")
        return {axis: _finite(value.get(axis), f"{field}.{axis}") for axis in axes}
    if kind == RGBA:
        if not isinstance(value, Mapping):
            raise ArgumentError(f"{field} must be an object")
        unexpected = set(value) - {"r", "g", "b", "a"}
        if unexpected:
            raise ArgumentError(f"{field} has unexpected keys: {sorted(unexpected)}")
        channels: dict[str, float] = {}
        for channel in ("r", "g", "b", "a"):
            raw = value.get(channel, 1.0 if channel == "a" else None)
            number = _finite(raw, f"{field}.{channel}")
            if not 0.0 <= number <= 1.0:
                raise ArgumentError(f"{field}.{channel} must be in [0, 1]")
            channels[channel] = number
        return channels
    if kind == POLYGON:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ArgumentError(f"{field} must be a list of points")
        if len(value) < 3:
            raise ArgumentError(f"{field} needs at least three points")
        return [_coerce(VEC2, point, f"{field}[{index}]") for index, point in enumerate(value)]
    raise ArgumentError(f"unknown argument kind {kind}")  # pragma: no cover


def validate_arguments(capability: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise one capability's arguments.

    Raises :class:`ArgumentError` with a message safe to show a developer. Unknown keys
    are rejected rather than ignored, so a model cannot smuggle a field past us.
    """
    spec = SPECS.get(capability)
    if spec is None:
        raise ArgumentError(f"{capability} is not a proposable capability")
    if not isinstance(raw, Mapping):
        raise ArgumentError("arguments must be an object")

    known = {name for name, _ in spec.required} | {name for name, _ in spec.optional}
    known |= set(spec.identity)
    unexpected = set(raw) - known
    if unexpected:
        raise ArgumentError(f"unexpected arguments: {sorted(unexpected)}")

    validated: dict[str, Any] = {}

    if spec.identity:
        present = [key for key in spec.identity if raw.get(key)]
        if not present:
            raise ArgumentError(f"one of {list(spec.identity)} is required")
        for key in present:
            validated[key] = _coerce(TEXT, raw[key], key)

    for name, kind in spec.required:
        if name not in raw:
            raise ArgumentError(f"{name} is required")
        validated[name] = _coerce(kind, raw[name], name)

    for name, kind in spec.optional:
        if name in raw and raw[name] is not None:
            validated[name] = _coerce(kind, raw[name], name)

    return validated


# --------------------------------------------------------------------------
# response parsing
# --------------------------------------------------------------------------


#: A single turn may not record an unbounded number of facts.
MAX_DESIGN_FACTS = 40
MAX_FACT_KEY_CHARACTERS = 64
MAX_FACT_VALUE_CHARACTERS = 400
MAX_ASSUMPTIONS = 20
MAX_ASSUMPTION_CHARACTERS = 300

_FACT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _design_facts(raw: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Extract facts worth remembering, ignoring anything malformed.

    A malformed fact is dropped rather than failing the whole turn: the user's
    modelling request should not be refused because the model mislabelled a memo to
    itself. The key shape is enforced so project memory stays queryable.
    """
    entries = raw.get("design_facts") or ()
    facts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("key") or "").strip().lower()
        value = str(entry.get("value") or "").strip()
        if not key or not value or key in seen:
            continue
        if not _FACT_KEY_PATTERN.match(key):
            continue
        facts.append((key[:MAX_FACT_KEY_CHARACTERS], value[:MAX_FACT_VALUE_CHARACTERS]))
        seen.add(key)
        if len(facts) >= MAX_DESIGN_FACTS:
            break
    return tuple(facts)


def _assumptions(raw: Mapping[str, Any]) -> tuple[str, ...]:
    entries = raw.get("assumptions") or ()
    assumptions = [
        str(entry).strip()[:MAX_ASSUMPTION_CHARACTERS]
        for entry in entries
        if str(entry).strip()
    ]
    return tuple(assumptions[:MAX_ASSUMPTIONS])


def _error(message: str, metadata: ProviderMetadata) -> AgentError:
    return AgentError(
        error=ChatError(code=VALIDATION_ERROR, message=message), metadata=metadata
    )


def parse_agent_response(
    raw: Any,
    metadata: ProviderMetadata,
    *,
    scene_version: Optional[str] = None,
) -> AgentOutcome:
    """Turn a model's structured response into a validated outcome."""
    if not isinstance(raw, Mapping):
        return _error("The assistant returned a response that was not an object.", metadata)

    result = validate_against_schema(RESPONSE_SCHEMA, raw)
    if not result.valid:
        first = result.violations[0]
        return _error(
            f"The assistant's response did not match the required shape "
            f"({first.path or 'root'}: {first.message}).",
            metadata,
        )

    kind = raw["kind"]
    message = str(raw.get("message") or "").strip()
    design_facts = _design_facts(raw)
    assumptions = _assumptions(raw)

    if kind == "answer":
        if not message:
            return _error("The assistant returned an empty answer.", metadata)
        return Answer(
            text=message,
            metadata=metadata,
            design_facts=design_facts,
            assumptions=assumptions,
        )

    if kind == "clarification":
        question = str(raw.get("question") or "").strip() or message
        if not question:
            return _error("The assistant asked for clarification without a question.", metadata)
        missing = tuple(
            str(item).strip()
            for item in raw.get("missing_information") or ()
            if str(item).strip()
        )
        return Clarification(
            question=question,
            metadata=metadata,
            missing_information=missing,
            design_facts=design_facts,
            assumptions=assumptions,
        )

    operations_raw = raw.get("operations") or ()
    if not operations_raw:
        return _error("The assistant proposed a plan with no operations.", metadata)
    if len(operations_raw) > MAX_OPERATIONS:
        return _error(
            f"The assistant proposed {len(operations_raw)} operations, "
            f"which exceeds the {MAX_OPERATIONS} allowed in one request.",
            metadata,
        )

    operations: list[ValidatedOperation] = []
    for index, entry in enumerate(operations_raw):
        capability = str(entry.get("capability") or "")
        if not is_proposable_capability(capability):
            return _error(
                f"Operation {index} names {capability!r}, which is not something "
                "the studio can do.",
                metadata,
            )
        label = str(entry.get("label") or capability.replace("_", " "))[
            :MAX_LABEL_CHARACTERS
        ]
        try:
            arguments = json.loads(entry.get("arguments_json") or "{}")
        except json.JSONDecodeError as error:
            return _error(
                f"Operation {index} had arguments that were not valid JSON ({error.msg}).",
                metadata,
            )
        try:
            validated = validate_arguments(capability, arguments)
        except ArgumentError as error:
            return _error(f"Operation {index} ({capability}): {error}", metadata)

        operations.append(
            ValidatedOperation(
                capability=capability,
                arguments=validated,
                label=label,
                operation_index=index,
            )
        )

    return PlanProposal(
        summary=message or "Applying the requested changes.",
        operations=tuple(operations),
        metadata=ProviderMetadata(
            provider_name=metadata.provider_name,
            provider_version=metadata.provider_version,
            operation_count=len(operations),
            model=metadata.model,
        ),
        scene_version=scene_version,
        design_facts=design_facts,
        assumptions=assumptions,
    )
