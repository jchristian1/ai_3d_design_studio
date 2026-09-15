"""Canonical schema access + a dependency-free JSON Schema validator.

Direction of truth::

    packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
             |
             +--> TypeScript representation (packages/*/src)
             +--> Python representation (this package)

The Python dataclasses in ``studio_contracts``/``studio_types`` are a
*representation* of the canonical schemas, not the source of truth. Conformance
tests load the same schema files the TypeScript side loads and verify the
representation still matches.

This validator implements only the JSON Schema subset the canonical contracts
use, deliberately avoiding a code-generation framework or a validation
dependency at this milestone. It mirrors packages/contracts/src/schema.ts
keyword-for-keyword.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    """Load a canonical schema by file name, e.g. ``vec3.schema.json``."""
    with (SCHEMA_DIR / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def list_schema_names() -> list[str]:
    """All canonical schema file names present in the schema directory."""
    return sorted(p.name for p in SCHEMA_DIR.glob("*.schema.json"))


def _resolve(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return _resolve(load_schema(ref))
    return schema


def schema_enum(name: str, property_path: Optional[Sequence[str]] = None) -> list[str]:
    """Read the canonical ``enum`` list for a schema (optionally at a property)."""
    node: Mapping[str, Any] = load_schema(name)
    for key in property_path or ():
        props = node.get("properties") or {}
        if key not in props:
            raise KeyError(f"no property {key!r} in {name}")
        node = _resolve(props[key])
    values = node.get("enum")
    if not isinstance(values, list):
        path = ".".join(property_path or ())
        raise KeyError(f"schema {name} has no enum at {path!r}")
    return list(values)


def schema_required(name: str) -> list[str]:
    """Read the canonical ``required`` list for a schema."""
    return sorted(load_schema(name).get("required", []))


def schema_properties(name: str) -> list[str]:
    """Read the canonical property names for a schema."""
    return sorted((load_schema(name).get("properties") or {}).keys())


@dataclass(frozen=True)
class SchemaViolation:
    path: str
    message: str


def _type_matches(type_name: str, value: Any) -> bool:
    if type_name == "object":
        return isinstance(value, Mapping)
    if type_name == "array":
        return isinstance(value, (list, tuple))
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "number":
        if isinstance(value, bool):
            return False
        return isinstance(value, (int, float)) and _is_finite(value)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    raise ValueError(f"unsupported type keyword: {type_name}")


def _is_finite(value: Any) -> bool:
    import math

    return math.isfinite(value)


def _check(
    schema: Mapping[str, Any],
    value: Any,
    path: str,
    out: list[SchemaViolation],
) -> None:
    s = _resolve(schema)

    if "type" in s:
        types = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_type_matches(t, value) for t in types):
            out.append(SchemaViolation(path, f"expected type {'|'.join(types)}"))
            return

    if isinstance(s.get("enum"), list) and not any(
        _deep_equal(v, value) for v in s["enum"]
    ):
        out.append(SchemaViolation(path, "value not in enum"))

    if "const" in s and not _deep_equal(s["const"], value):
        out.append(SchemaViolation(path, "value must equal const"))

    if isinstance(value, str):
        min_length = s.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            out.append(SchemaViolation(path, f"shorter than minLength {min_length}"))
        max_length = s.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            out.append(SchemaViolation(path, f"longer than maxLength {max_length}"))
        pattern = s.get("pattern")
        if isinstance(pattern, str) and not re.search(pattern, value):
            out.append(SchemaViolation(path, f"does not match pattern {pattern}"))
        if s.get("format") == "date-time" and not _DATE_TIME.match(value):
            out.append(SchemaViolation(path, "not an ISO-8601 date-time"))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = s.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            out.append(SchemaViolation(path, f"below minimum {minimum}"))
        maximum = s.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            out.append(SchemaViolation(path, f"above maximum {maximum}"))

    if isinstance(value, Mapping):
        props = s.get("properties") or {}

        for key in s.get("required", []):
            if key not in value:
                out.append(SchemaViolation(f"{path}.{key}" if path else key, "required"))

        for key, sub in props.items():
            if key in value:
                _check(sub, value[key], f"{path}.{key}" if path else key, out)

        if s.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    out.append(
                        SchemaViolation(
                            f"{path}.{key}" if path else key,
                            "additional property not allowed",
                        )
                    )

    if isinstance(value, (list, tuple)) and "items" in s:
        for i, item in enumerate(value):
            _check(s["items"], item, f"{path}[{i}]", out)

    for sub in s.get("allOf", []):
        _check(sub, value, path, out)

    any_of = s.get("anyOf")
    if any_of is not None and not any(_is_valid(sub, value) for sub in any_of):
        out.append(SchemaViolation(path, "does not match anyOf"))

    one_of = s.get("oneOf")
    if one_of is not None and sum(1 for sub in one_of if _is_valid(sub, value)) != 1:
        out.append(SchemaViolation(path, "must match exactly one of oneOf"))

    if "not" in s and _is_valid(s["not"], value):
        out.append(SchemaViolation(path, "must not match 'not' schema"))

    if "if" in s:
        branch = s.get("then") if _is_valid(s["if"], value) else s.get("else")
        if branch is not None:
            _check(branch, value, path, out)


def _deep_equal(a: Any, b: Any) -> bool:
    # bool/int must not compare equal (True == 1 in Python).
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _is_valid(schema: Mapping[str, Any], value: Any) -> bool:
    out: list[SchemaViolation] = []
    _check(schema, value, "", out)
    return not out


@dataclass
class SchemaValidationResult:
    valid: bool
    violations: list[SchemaViolation]


def validate_against_schema(schema_name: str, value: Any) -> SchemaValidationResult:
    """Validate a value against a canonical schema file."""
    violations: list[SchemaViolation] = []
    _check(load_schema(schema_name), value, "", violations)
    return SchemaValidationResult(valid=not violations, violations=violations)
