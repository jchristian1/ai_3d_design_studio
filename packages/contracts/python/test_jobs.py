"""Job layer tests — Python (Spec 001, Task 3).

Covers the exact spec example (Cube delta X = +0.50 m), project isolation,
payload validation, the unresolved-language guard, lifecycle states, idempotency
derivation, and serialization round trips.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import pytest
from studio_contracts import SCHEMA_FILES, validate_against_schema
from studio_contracts.jobs import (
    ALLOWED_TRANSITIONS,
    JOB_MESSAGES,
    UNRESOLVED_PAYLOAD_KEYS,
    can_transition,
    canonicalize,
    classify_delivery,
    create_move_object_job,
    derive_content_fingerprint,
    derive_idempotency_key,
    encode_float64,
    is_duplicate_delivery,
    is_terminal,
    is_valid_object_ref,
    job_from_wire,
    parse_job,
    serialize_job,
    to_job_wire,
    validate_job,
)
from studio_spatial import direction_delta_meters
from studio_types import (
    JOB_STATUSES,
    MoveObjectPayload,
    ObjectRef,
    RequestOrigin,
    Vec3,
)

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "job-cases.json").read_text("utf-8")
)

BASE = {
    "job_id": "job_1",
    "project_id": "proj_1",
    "session_id": "sess_1",
    "user_id": "user_1",
    "request_id": "req_abc123",
    "created_at": "2026-09-15T04:00:00Z",
}


def decode(value):
    if value == "__NAN__":
        return float("nan")
    if value == "__INF__":
        return float("inf")
    if value == "__NEG_INF__":
        return float("-inf")
    return value


def valid_job():
    result = create_move_object_job(
        **BASE,
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    assert result.ok is True, result.errors
    return result.job


def wire(**overrides):
    """A valid job as a mutable wire document, with optional overrides."""
    document = to_job_wire(valid_job())
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------
# The exact Spec 001 example
# ---------------------------------------------------------------------------


def test_exact_example_cube_delta_x_positive_half_meter():
    job = valid_job()
    assert job.job_type == "move_object"
    assert job.payload.target.name == "Cube"
    assert job.payload.delta_meters.x == 0.5
    assert job.payload.delta_meters.y == 0
    assert job.payload.delta_meters.z == 0
    assert job.status == "queued"
    assert job.project_id == "proj_1"


def test_resolved_spatial_delta_feeds_the_job_unchanged():
    """packages/spatial resolves '50 cm to the right'; the job stores meters."""
    resolved = direction_delta_meters("right", {"value": 50, "unit": "cm"})
    assert resolved.ok is True
    result = create_move_object_job(
        **BASE, target=ObjectRef(name="Cube"), delta_meters=resolved.delta
    )
    assert result.ok is True
    assert result.job.payload.delta_meters.x == 0.5


def test_valid_move_job_passes_schema_validation():
    job = valid_job()
    result = validate_against_schema(SCHEMA_FILES["Job"], to_job_wire(job))
    assert result.valid, result.violations
    assert validate_job(job).valid is True


# ---------------------------------------------------------------------------
# Project isolation
# ---------------------------------------------------------------------------


def test_missing_project_id_rejected():
    document = wire()
    del document["project_id"]
    result = validate_job(document)
    assert result.valid is False
    assert any(
        JOB_MESSAGES["MISSING_PROJECT_ID"] in e.message for e in result.errors
    ), "expected an explicit project_id error"


def test_blank_project_id_rejected():
    assert validate_job(wire(project_id="   ")).valid is False


@pytest.mark.parametrize("field", ["user_id", "session_id", "job_id"])
def test_required_identity_fields_rejected_when_missing(field):
    document = wire()
    del document[field]
    assert validate_job(document).valid is False


def test_job_cannot_be_built_without_project_id():
    result = create_move_object_job(
        job_id="job_1",
        project_id="",
        session_id="sess_1",
        user_id="user_1",
        request_id="req_abc123",
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
        created_at="2026-09-15T04:00:00Z",
    )
    assert result.ok is False


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------


def test_object_ref_validity():
    assert is_valid_object_ref({"name": "Cube"}) is True
    assert is_valid_object_ref({"object_id": "obj_8d83f"}) is True
    assert is_valid_object_ref(ObjectRef(name="Cube")) is True
    assert is_valid_object_ref({}) is False
    assert is_valid_object_ref({"name": "  "}) is False
    assert is_valid_object_ref(None) is False
    assert is_valid_object_ref("Cube") is False


@pytest.mark.parametrize(
    "target", [ObjectRef(), ObjectRef(name="   "), ObjectRef(object_id="")]
)
def test_invalid_target_rejected(target):
    result = create_move_object_job(
        **BASE, target=target, delta_meters=Vec3(0.5, 0.0, 0.0)
    )
    assert result.ok is False


def test_missing_target_rejected():
    document = wire()
    del document["payload"]["target"]
    assert validate_job(document).valid is False


# ---------------------------------------------------------------------------
# Delta validation: canonical meters, finite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_non_finite_delta_rejected_with_invalid_units(bad):
    result = create_move_object_job(
        **BASE, target=ObjectRef(name="Cube"), delta_meters=Vec3(bad, 0.0, 0.0)
    )
    assert result.ok is False
    assert any(e.code == "INVALID_UNITS" for e in result.errors)


@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_non_finite_delta_rejected_on_every_axis(axis):
    components = {"x": 0.0, "y": 0.0, "z": 0.0}
    components[axis] = float("nan")
    result = create_move_object_job(
        **BASE, target=ObjectRef(name="Cube"), delta_meters=Vec3(**components)
    )
    assert result.ok is False


def test_incomplete_delta_rejected():
    document = wire()
    del document["payload"]["delta_meters"]["z"]
    assert validate_job(document).valid is False


def test_unit_string_instead_of_number_rejected():
    document = wire()
    document["payload"]["delta_meters"]["x"] = "50 cm"
    assert validate_job(document).valid is False


# ---------------------------------------------------------------------------
# Unresolved language must never reach the worker
# ---------------------------------------------------------------------------


def test_unresolved_unit_field_rejected():
    document = wire()
    document["payload"]["unit"] = "cm"
    result = validate_job(document)
    assert result.valid is False
    assert any("unresolved language" in e.message for e in result.errors)


def test_unresolved_direction_field_rejected():
    document = wire()
    document["payload"]["direction"] = "right"
    result = validate_job(document)
    assert result.valid is False
    assert any("unresolved language" in e.message for e in result.errors)


@pytest.mark.parametrize("key", UNRESOLVED_PAYLOAD_KEYS)
def test_every_guarded_key_rejected(key):
    document = wire()
    document["payload"][key] = "anything"
    assert validate_job(document).valid is False


def test_raw_user_language_cannot_be_attached():
    document = wire(raw_user_message="Move Cube 50 cm to the right")
    assert validate_job(document).valid is False


def test_builder_has_no_unit_or_direction_parameter():
    """The construction signature must not accept unresolved concepts."""
    params = create_move_object_job.__code__.co_varnames[
        : create_move_object_job.__code__.co_argcount
    ]
    for forbidden in ("unit", "direction", "distance", "phrase", "message"):
        assert forbidden not in params, f"builder must not accept '{forbidden}'"


# ---------------------------------------------------------------------------
# Job type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_type", ["resize_object", "chat", "render_preview", "nope", ""]
)
def test_unsupported_job_type_rejected(job_type):
    assert validate_job(wire(job_type=job_type)).valid is False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "cancelled", "QUEUED", ""])
def test_invalid_status_rejected(status):
    assert validate_job(wire(status=status)).valid is False


@pytest.mark.parametrize("status", JOB_STATUSES)
def test_all_statuses_covered_by_transition_table(status):
    assert status in ALLOWED_TRANSITIONS


def test_terminal_states_allow_no_transitions():
    assert is_terminal("succeeded") is True
    assert is_terminal("failed") is True
    assert is_terminal("queued") is False
    assert ALLOWED_TRANSITIONS["succeeded"] == ()
    assert ALLOWED_TRANSITIONS["failed"] == ()


@pytest.mark.parametrize(
    "transition",
    CASES["transitions"]["allowed"],
    ids=[f"{t['from']}->{t['to']}" for t in CASES["transitions"]["allowed"]],
)
def test_corpus_allowed_transitions(transition):
    assert can_transition(transition["from"], transition["to"]) is True


@pytest.mark.parametrize(
    "transition",
    CASES["transitions"]["rejected"],
    ids=[f"{t['from']}->{t['to']}" for t in CASES["transitions"]["rejected"]],
)
def test_corpus_rejected_transitions(transition):
    assert can_transition(transition["from"], transition["to"]) is False


def test_failed_job_must_carry_structured_error():
    assert validate_job(wire(status="failed")).valid is False
    with_error = wire(
        status="failed",
        error={"code": "LOCK_CONFLICT", "message": "project is locked"},
    )
    assert validate_job(with_error).valid is True


def test_non_failed_job_must_not_carry_error():
    document = wire(error={"code": "INTERNAL_ERROR", "message": "x"})
    assert validate_job(document).valid is False


@pytest.mark.parametrize("status", ["claimed", "running"])
def test_claimed_and_running_require_worker_ownership(status):
    assert validate_job(wire(status=status)).valid is False
    with_claim = wire(
        status=status,
        claim={
            "worker_id": "worker_1",
            "claimed_at": "2026-09-15T04:00:01Z",
            "lease_expires_at": "2026-09-15T04:05:01Z",
        },
    )
    result = validate_job(with_claim)
    assert result.valid is True, result.errors


def test_queued_job_must_not_be_owned():
    document = wire(
        claim={
            "worker_id": "worker_1",
            "claimed_at": "2026-09-15T04:00:01Z",
            "lease_expires_at": "2026-09-15T04:05:01Z",
        }
    )
    assert validate_job(document).valid is False


def test_only_succeeded_job_may_carry_result():
    assert validate_job(wire(result={"ok": True})).valid is False


# ---------------------------------------------------------------------------
# Canonical encoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    CASES["canonicalize"]["cases"],
    ids=[c["name"] for c in CASES["canonicalize"]["cases"]],
)
def test_corpus_canonicalize(case):
    assert canonicalize(case["value"]) == case["expected"]


@pytest.mark.parametrize(
    "case",
    CASES["canonicalize"]["rejected"],
    ids=[c["name"] for c in CASES["canonicalize"]["rejected"]],
)
def test_corpus_canonicalize_rejected(case):
    with pytest.raises(ValueError):
        canonicalize(decode(case["value"]))


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_encode_float64_rejects_non_finite(value):
    with pytest.raises(ValueError):
        encode_float64(value)


def test_canonicalize_is_key_order_independent():
    assert canonicalize({"a": 1, "b": 2}) == canonicalize({"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# Idempotency: mutation identity comes from the originating request
# ---------------------------------------------------------------------------


def _derive(base, **overrides):
    merged = {**base, **overrides}
    return derive_idempotency_key(
        project_id=merged["project_id"],
        request_id=merged["request_id"],
        operation_index=merged["operation_index"],
    )


def test_corpus_idempotency_key_matches_recorded_value():
    assert (
        _derive(CASES["idempotency"]["base"])
        == CASES["idempotency"]["expected_key"]
    )


def test_a_same_request_operation_project_gives_same_key():
    base = CASES["idempotency"]["base"]
    assert _derive(base) == _derive(dict(base))


def test_a_same_origin_yields_same_job_identity_via_builder():
    first = valid_job()
    again = create_move_object_job(
        **BASE, target=ObjectRef(name="Cube"), delta_meters=Vec3(0.5, 0.0, 0.0)
    )
    assert again.ok is True
    assert again.job.idempotency_key == first.idempotency_key


def test_b_retry_of_same_submission_keeps_same_key():
    """A retry is a fresh attempt record reusing the same request_id."""
    original = valid_job()
    retry_input = {**BASE, "job_id": "job_RETRY", "created_at": "2026-09-15T04:09:00Z"}
    retry = create_move_object_job(
        **retry_input, target=ObjectRef(name="Cube"), delta_meters=Vec3(0.5, 0.0, 0.0)
    )
    assert retry.ok is True
    assert retry.job.idempotency_key == original.idempotency_key
    assert retry.job.job_id != original.job_id


def test_c_different_request_id_with_identical_payload_differs():
    original = valid_job()
    new_command = create_move_object_job(
        **{**BASE, "job_id": "job_2", "request_id": "req_xyz789"},
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    assert new_command.ok is True
    assert new_command.job.idempotency_key != original.idempotency_key
    # The payloads are byte-identical; only the origin differs.
    assert new_command.job.payload == original.payload


def test_d_operation_index_0_vs_1_differ():
    base = CASES["idempotency"]["base"]
    assert _derive(base, operation_index=1) != _derive(base)

    first = create_move_object_job(
        **{**BASE, "request_id": "req_multi"},
        operation_index=0,
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    second = create_move_object_job(
        **{**BASE, "job_id": "job_2", "request_id": "req_multi"},
        operation_index=1,
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    assert first.ok and second.ok
    assert first.job.idempotency_key != second.job.idempotency_key


def test_e_same_request_id_in_different_projects_is_isolated():
    base = CASES["idempotency"]["base"]
    assert _derive(base, project_id="proj_2") != _derive(base)


def test_f_two_intentional_identical_commands_are_two_jobs():
    """'Move Cube 50 cm right.' then 'Move Cube 50 cm right again.'"""
    first = create_move_object_job(
        **{**BASE, "job_id": "job_first", "request_id": "req_first"},
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    second = create_move_object_job(
        **{**BASE, "job_id": "job_second", "request_id": "req_second"},
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    assert first.ok and second.ok

    assert first.job.job_id != second.job.job_id, "distinct job records"
    assert (
        first.job.idempotency_key != second.job.idempotency_key
    ), "distinct mutations: both must execute"
    # Same content, by design.
    assert first.job.content_fingerprint == second.job.content_fingerprint
    assert first.job.payload == second.job.payload


@pytest.mark.parametrize(
    "variant",
    CASES["idempotency"]["different_key_variants"],
    ids=[v["name"] for v in CASES["idempotency"]["different_key_variants"]],
)
def test_corpus_key_changes_for_origin_variants(variant):
    base = CASES["idempotency"]["base"]
    assert _derive(base, **{variant["field"]: variant["value"]}) != _derive(base)


@pytest.mark.parametrize(
    "variant",
    CASES["idempotency"]["rejected"],
    ids=[v["name"] for v in CASES["idempotency"]["rejected"]],
)
def test_corpus_idempotency_rejects_invalid_operation_index(variant):
    base = CASES["idempotency"]["base"]
    with pytest.raises(ValueError):
        _derive(base, **{variant["field"]: variant["value"]})


def test_derived_key_shape():
    key = valid_job().idempotency_key
    assert key.startswith("idem_")
    assert len(key) == len("idem_") + 64
    int(key[len("idem_") :], 16)  # must be hex


def test_origin_is_preserved_for_auditability():
    job = valid_job()
    assert job.origin == RequestOrigin(request_id="req_abc123", operation_index=0)


@pytest.mark.parametrize("request_id", ["", "   "])
def test_job_cannot_be_built_without_request_id(request_id):
    result = create_move_object_job(
        **{**BASE, "request_id": request_id},
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    assert result.ok is False


@pytest.mark.parametrize("operation_index", [-1, 0.5, True])
def test_job_cannot_be_built_with_invalid_operation_index(operation_index):
    result = create_move_object_job(
        **BASE,
        operation_index=operation_index,
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(0.5, 0.0, 0.0),
    )
    assert result.ok is False


def test_missing_origin_rejected():
    document = wire()
    del document["origin"]
    assert validate_job(document).valid is False


# ---------------------------------------------------------------------------
# Content fingerprint is diagnostics only
# ---------------------------------------------------------------------------


def test_corpus_content_fingerprint_matches_recorded_value():
    fp = CASES["content_fingerprint"]
    assert (
        derive_content_fingerprint(fp["job_type"], fp["payload"])
        == fp["expected_fingerprint"]
    )


def test_identical_content_shares_fingerprint_but_not_identity():
    fp = CASES["content_fingerprint"]
    assert derive_content_fingerprint(
        fp["job_type"], fp["payload"]
    ) == derive_content_fingerprint(fp["job_type"], fp["payload"])
    assert derive_idempotency_key("proj_1", "req_A", 0) != derive_idempotency_key(
        "proj_1", "req_B", 0
    )


def test_fingerprint_and_key_use_distinct_namespaces():
    job = valid_job()
    assert job.content_fingerprint.startswith("fp_")
    assert job.idempotency_key.startswith("idem_")
    assert job.content_fingerprint != job.idempotency_key


@pytest.mark.parametrize(
    "variant",
    CASES["content_fingerprint"]["different_payloads"],
    ids=[v["name"] for v in CASES["content_fingerprint"]["different_payloads"]],
)
def test_corpus_fingerprint_changes_for_different_payloads(variant):
    fp = CASES["content_fingerprint"]
    assert (
        derive_content_fingerprint(fp["job_type"], variant["payload"])
        != fp["expected_fingerprint"]
    )


@pytest.mark.parametrize(
    "variant",
    CASES["content_fingerprint"]["equivalent_payloads"],
    ids=[v["name"] for v in CASES["content_fingerprint"]["equivalent_payloads"]],
)
def test_corpus_fingerprint_unchanged_for_equivalent_payloads(variant):
    """-0.0 and +0.0 are semantically equivalent and must not change the hash."""
    fp = CASES["content_fingerprint"]
    assert (
        derive_content_fingerprint(fp["job_type"], variant["payload"])
        == fp["expected_fingerprint"]
    )


def test_dataclass_and_wire_payloads_derive_the_same_fingerprint():
    payload = MoveObjectPayload(
        target=ObjectRef(name="Cube"), delta_meters=Vec3(0.5, 0.0, 0.0)
    )
    from studio_contracts import to_wire

    assert derive_content_fingerprint(
        "move_object", payload
    ) == derive_content_fingerprint("move_object", to_wire(payload))


# ---------------------------------------------------------------------------
# Duplicate queue delivery of the same job_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    CASES["delivery"]["cases"],
    ids=[c["name"] for c in CASES["delivery"]["cases"]],
)
def test_corpus_delivery_decisions(case):
    assert classify_delivery(case["recorded_status"]) == case["decision"]


def test_only_unstarted_job_may_execute_on_delivery():
    assert is_duplicate_delivery(None) is False
    assert is_duplicate_delivery("queued") is False
    for status in ("claimed", "running", "succeeded", "failed"):
        assert is_duplicate_delivery(status) is True, status


@pytest.mark.parametrize("status", JOB_STATUSES)
def test_every_status_has_a_delivery_decision(status):
    assert classify_delivery(status) in (
        "execute",
        "already_owned",
        "reuse_result",
    )


# ---------------------------------------------------------------------------
# Serialization round trip
# ---------------------------------------------------------------------------


def test_serialize_parse_round_trip_preserves_job():
    job = valid_job()
    parsed = parse_job(serialize_job(job))
    assert parsed.ok is True, parsed.errors
    assert parsed.job == to_job_wire(job)


def test_round_trip_preserves_exact_delta():
    parsed = parse_job(serialize_job(valid_job()))
    assert parsed.job["payload"]["delta_meters"]["x"] == 0.5


def test_typed_round_trip_via_job_from_wire():
    job = valid_job()
    rebuilt = job_from_wire(to_job_wire(job))
    assert rebuilt == job
    assert isinstance(rebuilt.payload, MoveObjectPayload)
    assert isinstance(rebuilt.payload.delta_meters, Vec3)
    assert rebuilt.payload.delta_meters.x == 0.5


def test_job_from_wire_rejects_invalid_document():
    document = wire()
    del document["project_id"]
    with pytest.raises(ValueError):
        job_from_wire(document)


def test_parsing_malformed_json_fails_cleanly():
    result = parse_job("{not json")
    assert result.ok is False
    assert result.errors[0].code == "VALIDATION_ERROR"


def test_parsing_structurally_invalid_job_fails():
    result = parse_job(json.dumps({"job_id": "j", "job_type": "move_object"}))
    assert result.ok is False


def test_serialized_job_is_schema_valid_after_round_trip():
    parsed = parse_job(serialize_job(valid_job()))
    assert parsed.ok is True
    result = validate_against_schema(SCHEMA_FILES["Job"], parsed.job)
    assert result.valid, result.violations


# ---------------------------------------------------------------------------
# Persistence boundary is interface-only in this task
# ---------------------------------------------------------------------------


def test_no_persistence_or_queue_implementation_exists():
    """Task 3 defines the boundary only; Redis/DB wiring belongs to a later task."""
    import studio_contracts.jobs as jobs_module

    source = Path(jobs_module.__file__).read_text("utf-8")
    for forbidden in ("import redis", "import psycopg", "sqlalchemy", "asyncpg"):
        assert forbidden not in source, f"unexpected infrastructure: {forbidden}"


def test_job_store_is_a_protocol_without_implementation():
    from studio_contracts.jobs import JobClaimer, JobStore

    assert getattr(JobStore, "_is_protocol", False) is True
    assert getattr(JobClaimer, "_is_protocol", False) is True
