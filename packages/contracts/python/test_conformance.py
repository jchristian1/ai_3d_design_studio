"""Conformance tests: Python representation vs. CANONICAL schemas.

These tests are the drift guard. They read packages/contracts/schemas/*.json at
runtime and assert that:
  1. the shared, language-neutral case corpus validates identically here,
  2. the Python constants (ERROR_CODES, JOB_STATUSES, JOB_TYPES) equal the
     canonical enums,
  3. every canonical field is present in the Python dataclasses and vice versa,
  4. instances built from the Python types serialize to schema-valid documents,
  5. the runtime validation rules agree with the canonical schema.

conformance.test.ts runs the equivalent assertions against the SAME files.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from studio_contracts import (
    ERROR_CODES,
    SCHEMA_DIR,
    SCHEMA_FILES,
    ChatError,
    ChatRequest,
    ChatResponse,
    list_schema_names,
    schema_enum,
    schema_properties,
    schema_required,
    to_wire,
    validate_against_schema,
)
from studio_types import (
    ARTIFACT_TYPES,
    JOB_STATUSES,
    JOB_TYPES,
    Job,
    MoveObjectPayload,
    ObjectRef,
    PreviewArtifact,
    RequestOrigin,
    Vec3,
)
from studio_validation import CHAT_REQUEST_FIELDS, validate_chat_request

with (SCHEMA_DIR / "conformance-cases.json").open("r", encoding="utf-8") as fh:
    CORPUS = json.load(fh)


def _cases(kind: str) -> list:
    """Flatten the shared corpus into a list of parametrize cases.

    A list, not a generator: pytest deprecates non-Collection iterables in
    parametrize (removed in pytest 10).
    """
    return [
        pytest.param(
            suite["schema"],
            case["data"],
            id=f"{suite['schema']}::{case['name']}",
        )
        for suite in CORPUS["suites"]
        for case in suite[kind]
    ]


# ---------------------------------------------------------------------------
# 1. Shared language-neutral corpus
# ---------------------------------------------------------------------------


def test_canonical_schema_directory_contains_expected_schemas():
    names = list_schema_names()
    for file in SCHEMA_FILES.values():
        assert file in names, f"missing canonical schema: {file}"


@pytest.mark.parametrize("schema,data", _cases("valid"))
def test_corpus_valid_cases_accepted(schema, data):
    result = validate_against_schema(schema, data)
    assert result.valid, f"expected valid, got {result.violations}"


@pytest.mark.parametrize("schema,data", _cases("invalid"))
def test_corpus_invalid_cases_rejected(schema, data):
    result = validate_against_schema(schema, data)
    assert not result.valid, "expected schema violation"


# ---------------------------------------------------------------------------
# 2. Enum parity with canonical schemas
# ---------------------------------------------------------------------------


def test_error_codes_equal_canonical_enum():
    assert list(ERROR_CODES) == schema_enum(SCHEMA_FILES["ErrorCode"])


def test_job_statuses_equal_canonical_enum():
    assert list(JOB_STATUSES) == schema_enum(SCHEMA_FILES["Job"], ["status"])


def test_job_types_equal_canonical_enum():
    assert list(JOB_TYPES) == schema_enum(SCHEMA_FILES["JobType"])


def test_artifact_types_equal_canonical_enum():
    assert list(ARTIFACT_TYPES) == schema_enum(SCHEMA_FILES["ArtifactType"])


def test_preview_artifact_type_resolves_to_canonical_enum():
    assert schema_enum(
        SCHEMA_FILES["PreviewArtifact"], ["artifact_type"]
    ) == schema_enum(SCHEMA_FILES["ArtifactType"])


def test_job_job_type_resolves_to_canonical_enum():
    assert schema_enum(SCHEMA_FILES["Job"], ["job_type"]) == schema_enum(
        SCHEMA_FILES["JobType"]
    )


# ---------------------------------------------------------------------------
# 3. Field parity: canonical properties vs. Python dataclasses
# ---------------------------------------------------------------------------


def _field_names(cls) -> list[str]:
    return sorted(f.name for f in dataclasses.fields(cls))


@pytest.mark.parametrize(
    "cls,schema_key",
    [
        (ChatRequest, "ChatRequest"),
        (ChatResponse, "ChatResponse"),
        (ChatError, "ErrorResponse"),
        (Job, "Job"),
        (Vec3, "Vec3"),
        (PreviewArtifact, "PreviewArtifact"),
    ],
)
def test_dataclass_fields_match_canonical_properties(cls, schema_key):
    assert _field_names(cls) == schema_properties(SCHEMA_FILES[schema_key])


@pytest.mark.parametrize(
    "schema_key,expected_required",
    [
        ("ChatRequest", ["message", "project_id", "request_id", "session_id"]),
        ("ChatResponse", ["status", "summary"]),
        ("ErrorResponse", ["code", "message"]),
        (
            "Job",
            [
                "created_at",
                "idempotency_key",
                "job_id",
                "job_type",
                "origin",
                "payload",
                "project_id",
                "session_id",
                "status",
                "user_id",
            ],
        ),
        ("Vec3", ["x", "y", "z"]),
        (
            "PreviewArtifact",
            [
                "artifact_id",
                "artifact_type",
                "checksum",
                "created_at",
                "height",
                "media_type",
                "project_id",
                "size_bytes",
                "width",
            ],
        ),
    ],
)
def test_canonical_required_fields(schema_key, expected_required):
    assert schema_required(SCHEMA_FILES[schema_key]) == expected_required


def test_python_required_fields_have_no_defaults():
    """Fields the canonical schema marks required must not be optional in Python."""
    for cls, key in ((ChatRequest, "ChatRequest"), (ChatResponse, "ChatResponse")):
        required = set(schema_required(SCHEMA_FILES[key]))
        for f in dataclasses.fields(cls):
            if f.name in required:
                assert (
                    f.default is dataclasses.MISSING
                    and f.default_factory is dataclasses.MISSING
                ), f"{cls.__name__}.{f.name} is canonically required but optional"


# ---------------------------------------------------------------------------
# 4. Instances built from Python types are schema-valid on the wire
# ---------------------------------------------------------------------------


def test_python_chat_request_is_schema_valid():
    req = ChatRequest(
        request_id="req_abc123",
        project_id="proj_1",
        session_id="sess_1",
        message="Move Cube 50 cm to the right",
    )
    result = validate_against_schema(SCHEMA_FILES["ChatRequest"], to_wire(req))
    assert result.valid, result.violations


def test_python_success_chat_response_is_schema_valid():
    res = ChatResponse(
        status="success",
        summary="Moved Cube 0.50 m on +X",
        object_position=Vec3(0.5, 0.0, 0.0),
        preview_url="s3://previews/p.png",
    )
    result = validate_against_schema(SCHEMA_FILES["ChatResponse"], to_wire(res))
    assert result.valid, result.violations


def test_python_error_chat_response_is_schema_valid():
    res = ChatResponse(
        status="error",
        summary="Object not found",
        error=ChatError(code="OBJECT_NOT_FOUND", message="no object named 'Cube'"),
    )
    result = validate_against_schema(SCHEMA_FILES["ChatResponse"], to_wire(res))
    assert result.valid, result.violations


def _sample_job() -> Job:
    return Job(
        job_id="job_1",
        job_type="move_object",
        project_id="proj_1",
        session_id="sess_1",
        user_id="user_1",
        payload=MoveObjectPayload(
            target=ObjectRef(name="Cube"),
            delta_meters=Vec3(0.5, 0.0, 0.0),
        ),
        origin=RequestOrigin(request_id="req_abc123", operation_index=0),
        status="queued",
        idempotency_key="idem_abc",
    )


def test_python_job_is_schema_valid():
    result = validate_against_schema(SCHEMA_FILES["Job"], to_wire(_sample_job()))
    assert result.valid, result.violations


def test_python_preview_artifact_is_schema_valid():
    artifact = PreviewArtifact(
        artifact_id="preview_a1b2c3d4e5f60718",
        project_id="proj_seed",
        artifact_type="preview_image",
        media_type="image/png",
        created_at="2026-09-15T04:00:00Z",
        width=640,
        height=360,
        size_bytes=20481,
        checksum="sha256:" + "a" * 64,
        job_id="job_req_e2e_001_0",
        engine="BLENDER_WORKBENCH",
    )
    result = validate_against_schema(
        SCHEMA_FILES["PreviewArtifact"], to_wire(artifact)
    )
    assert result.valid, result.violations


def test_preview_artifact_contract_cannot_carry_a_path_or_url():
    """The artifact reference must never become a way to name a file."""
    properties = schema_properties(SCHEMA_FILES["PreviewArtifact"])
    for forbidden in ("path", "url", "filepath", "filename", "directory", "blend_path"):
        assert forbidden not in properties, f"PreviewArtifact exposes {forbidden}"


def test_python_default_created_at_matches_canonical_date_time_format():
    """The auto-generated created_at must satisfy the canonical date-time format."""
    result = validate_against_schema(SCHEMA_FILES["Job"], to_wire(_sample_job()))
    assert result.valid, result.violations


# ---------------------------------------------------------------------------
# 5. Runtime validation rules agree with the canonical schema
# ---------------------------------------------------------------------------


def test_validate_chat_request_agrees_with_canonical_schema():
    suite = next(
        s for s in CORPUS["suites"] if s["schema"] == SCHEMA_FILES["ChatRequest"]
    )
    for case in suite["valid"]:
        assert (
            validate_chat_request(case["data"]).valid is True
        ), f"validation rejected schema-valid case: {case['name']}"
    for case in suite["invalid"]:
        assert (
            validate_chat_request(case["data"]).valid is False
        ), f"validation accepted schema-invalid case: {case['name']}"


def test_chat_request_fields_equal_canonical_properties():
    assert list(CHAT_REQUEST_FIELDS) == schema_properties(SCHEMA_FILES["ChatRequest"])
