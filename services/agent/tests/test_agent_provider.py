"""Agent provider and job-factory tests (Spec 001, Task 7).

Covers the required behaviours 1-16: the exact Spec 001 command, delegation to
packages/spatial, determinism, refusal of vague or unsupported language, identity
discipline, Task 3 idempotency preservation, the unimplemented provider
boundaries, and the layering guarantees.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from studio_agent import AgentContext, AgentPlan, AgentProvider, JobFactory
from studio_agent.plan import PlannedOperation, ProviderMetadata
from studio_agent.provider import AgentProviderError
from studio_agent.providers import AstraProvider, CodexProvider, RuleBasedProvider
from studio_agent.providers.registry import (
    DEFAULT_PROVIDER_NAME,
    available_provider_names,
    get_provider,
)
from studio_contracts import ChatRequest, to_wire
from studio_contracts.jobs import derive_idempotency_key, validate_job
from studio_types import MoveObjectPayload, ObjectRef, Vec3

SPEC_COMMAND = "Move Cube 50 cm to the right."
CREATED_AT = "2026-09-15T04:00:00Z"

AGENT_PACKAGE = Path(__file__).resolve().parents[1] / "studio_agent"


def context(
    user_id: str = "user_1",
    project_id: str = "proj_1",
    session_id: str = "sess_1",
) -> AgentContext:
    return AgentContext(
        user_id=user_id, project_id=project_id, session_id=session_id
    )


def chat_request(
    message: str = SPEC_COMMAND,
    request_id: str = "req_1",
    project_id: str = "proj_1",
) -> ChatRequest:
    return ChatRequest(
        request_id=request_id,
        project_id=project_id,
        session_id="sess_1",
        message=message,
    )


def interpret(message: str = SPEC_COMMAND, **kwargs):
    return RuleBasedProvider().interpret(chat_request(message, **kwargs), context())


# ---------------------------------------------------------------------------
# 1. The exact Spec 001 command
# ---------------------------------------------------------------------------


def test_1_exact_spec_command_produces_the_expected_operation():
    result = interpret(SPEC_COMMAND)

    assert result.ok, result.error
    assert result.plan.operation_count == 1

    operation = result.plan.operations[0]
    assert operation.operation_type == "move_object"
    assert operation.payload.target == ObjectRef(name="Cube")
    assert operation.payload.delta_meters == Vec3(0.5, 0.0, 0.0)


@pytest.mark.parametrize(
    "message",
    [
        "Move Cube 50 cm to the right.",
        "move Cube 50 cm to the right",
        "move Cube 50 centimeters right",
        "Move Cube 50cm right",
        "MOVE CUBE 50 CM TO THE RIGHT",
        "please move Cube 50 cm to the right",
        "Move Cube 0.5 m to the right",
        "move Cube 0.5 meters right",
    ],
)
def test_1_documented_variants_all_resolve_identically(message):
    result = interpret(message)
    assert result.ok, (message, result.error)
    payload = result.plan.operations[0].payload
    assert payload.delta_meters == Vec3(0.5, 0.0, 0.0)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("move Cube 50 cm left", Vec3(-0.5, 0.0, 0.0)),
        ("move Cube 1 m up", Vec3(0.0, 0.0, 1.0)),
        ("move Cube 100 cm down", Vec3(0.0, 0.0, -1.0)),
        ("move Cube 2.4 m forward", Vec3(0.0, 2.4, 0.0)),
        ("move Cube 240 cm back", Vec3(0.0, -2.4, 0.0)),
    ],
)
def test_1_other_world_directions_resolve_correctly(message, expected):
    result = interpret(message)
    assert result.ok, result.error
    assert result.plan.operations[0].payload.delta_meters == expected


def test_1_target_name_is_taken_verbatim():
    result = interpret("move KitchenIsland 40 cm right")
    assert result.plan.operations[0].payload.target == ObjectRef(name="KitchenIsland")


# ---------------------------------------------------------------------------
# 2. Spatial conversion is delegated, not duplicated
# ---------------------------------------------------------------------------


def test_2_provider_delegates_conversion_to_packages_spatial():
    source = (AGENT_PACKAGE / "providers" / "rule_based.py").read_text("utf-8")
    tree = ast.parse(source)
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "studio_spatial" in imported, "must use the approved spatial utilities"


def test_2_provider_contains_no_unit_arithmetic():
    """A second source of truth for cm->m would be a safety hazard."""
    source = (AGENT_PACKAGE / "providers" / "rule_based.py").read_text("utf-8")
    code_lines = [
        line.split("#")[0]
        for line in source.splitlines()
        if not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    for forbidden in ("/ 100", "* 0.01", "/100", "*0.01", "CM_PER_METER ="):
        assert forbidden not in code, f"unit arithmetic {forbidden!r} duplicated"


def test_2_provider_contains_no_direction_to_axis_table():
    """Axis mapping belongs to packages/spatial only."""
    source = (AGENT_PACKAGE / "providers" / "rule_based.py").read_text("utf-8")
    assert '"axis"' not in source
    assert "sign=" not in source


def test_2_matches_spatial_directly():
    from studio_spatial import direction_delta_meters

    expected = direction_delta_meters("right", {"value": 50, "unit": "cm"})
    assert interpret().plan.operations[0].payload.delta_meters == expected.delta


# ---------------------------------------------------------------------------
# 3. Determinism
# ---------------------------------------------------------------------------


def test_3_same_message_and_context_yield_identical_plans():
    first = interpret()
    for _ in range(25):
        again = interpret()
        assert again.plan == first.plan
        assert again.metadata == first.metadata


def test_3_provider_generates_no_identifiers():
    """job_id is assigned at the job boundary, not during interpretation."""
    source = (AGENT_PACKAGE / "providers" / "rule_based.py").read_text("utf-8")
    tree = ast.parse(source)
    modules = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    for forbidden in ("uuid", "random", "secrets", "time", "datetime"):
        assert forbidden not in modules, f"non-deterministic import: {forbidden}"


# ---------------------------------------------------------------------------
# 4-6. Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "move it over there",
        "shift it a little",
        "move Cube toward the window",
        "make it look better",
        "nudge the cube",
        "move Cube a bit to the right",
        "put Cube on the left",
        "move Cube right",            # no measurement
        "move Cube 50 to the right",  # no unit
        "Cube 50 cm right",           # no verb
        "delete Cube",
        "",
        "   ",
    ],
)
def test_4_vague_or_unsupported_instructions_are_refused(message):
    result = interpret(message)
    assert result.ok is False, f"must not silently succeed: {message!r}"
    assert result.error.code in ("UNSUPPORTED_INSTRUCTION", "VALIDATION_ERROR")
    assert result.plan is None
    assert result.metadata.operation_count == 0


@pytest.mark.parametrize(
    "message",
    [
        "move Cube 50 cm sideways",
        "move Cube 50 cm toward the camera",
        "move Cube 50 cm inward",
        "move Cube 50 cm north",
    ],
)
def test_5_unsupported_directions_are_never_guessed(message):
    result = interpret(message)
    assert result.ok is False
    assert result.error.code == "UNSUPPORTED_INSTRUCTION"


def test_5_camera_relative_language_is_refused():
    """Camera-relative interpretation is deferred; it must not be faked."""
    assert interpret("move Cube 50 cm toward the camera").ok is False


@pytest.mark.parametrize(
    "message",
    [
        "move Cube 50 mm right",
        "move Cube 50 inches right",
        "move Cube 50 feet right",
        "move Cube 50 furlongs right",
    ],
)
def test_6_unsupported_units_are_refused(message):
    result = interpret(message)
    assert result.ok is False
    assert result.error.code in ("INVALID_UNITS", "UNSUPPORTED_INSTRUCTION")


def test_6_unsupported_unit_reports_invalid_units():
    result = interpret("move Cube 50 mm right")
    assert result.error.code == "INVALID_UNITS"
    assert "centimeters" in result.error.message


def test_malformed_request_is_reported_as_validation_error():
    result = RuleBasedProvider().interpret({"no_message": True}, context())
    assert result.ok is False
    assert result.error.code == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 7-9. Job creation and identity
# ---------------------------------------------------------------------------


def build_job(
    message: str = SPEC_COMMAND,
    request_id: str = "req_1",
    ctx: AgentContext | None = None,
    request_project_id: str = "proj_1",
):
    result = RuleBasedProvider().interpret(
        chat_request(message, request_id=request_id, project_id=request_project_id),
        ctx or context(),
    )
    assert result.ok, result.error
    return JobFactory().build(
        result.plan,
        chat_request(message, request_id=request_id, project_id=request_project_id),
        ctx or context(),
        created_at=CREATED_AT,
    )


def test_7_missing_user_id_prevents_job_creation():
    built = build_job(ctx=context(user_id=""))
    assert built.ok is False
    assert built.jobs == []
    assert any("user_id" in e.message for e in built.errors)


@pytest.mark.parametrize("field", ["project_id", "session_id"])
def test_7_missing_trusted_identity_prevents_job_creation(field):
    built = build_job(ctx=context(**{field: ""}))
    assert built.ok is False


def test_8_request_id_is_preserved_into_job_origin():
    built = build_job(request_id="req_abc123")
    assert built.ok, built.errors
    job = built.jobs[0]
    assert job.origin.request_id == "req_abc123"


def test_9_first_operation_uses_operation_index_zero():
    job = build_job().jobs[0]
    assert job.origin.operation_index == 0


def test_job_is_canonically_valid_and_carries_the_resolved_delta():
    job = build_job().jobs[0]
    validation = validate_job(job)
    assert validation.valid, validation.errors
    assert job.job_type == "move_object"
    assert job.payload.target == ObjectRef(name="Cube")
    assert job.payload.delta_meters == Vec3(0.5, 0.0, 0.0)
    assert job.status == "queued"


def test_job_identity_comes_from_trusted_context():
    ctx = AgentContext(
        user_id="user_trusted", project_id="proj_1", session_id="sess_trusted"
    )
    job = build_job(ctx=ctx).jobs[0]
    assert job.user_id == "user_trusted"
    assert job.session_id == "sess_trusted"
    assert job.project_id == "proj_1"


# ---------------------------------------------------------------------------
# 10-11. Task 3 idempotency preserved
# ---------------------------------------------------------------------------


def test_10_retry_of_the_same_request_yields_the_same_mutation_identity():
    first = build_job(request_id="req_same").jobs[0]
    retry = build_job(request_id="req_same").jobs[0]
    assert retry.idempotency_key == first.idempotency_key
    assert retry.job_id == first.job_id, "same request reuses the job record"


def test_11_a_new_request_with_the_same_message_yields_a_different_identity():
    first = build_job(request_id="req_first").jobs[0]
    second = build_job(request_id="req_second").jobs[0]

    assert to_wire(first.payload) == to_wire(second.payload), "identical content"
    assert first.idempotency_key != second.idempotency_key, "distinct mutations"
    assert first.job_id != second.job_id


def test_10_idempotency_hashing_is_not_reimplemented():
    """The key must come from the Task 3 derivation, not a local copy."""
    job = build_job(request_id="req_check").jobs[0]
    assert job.idempotency_key == derive_idempotency_key(
        project_id="proj_1", request_id="req_check", operation_index=0
    )


def test_10_job_factory_does_not_hash_anything_itself():
    source = (AGENT_PACKAGE / "job_factory.py").read_text("utf-8")
    for forbidden in ("hashlib", "sha256", "canonicalize"):
        assert forbidden not in source, f"{forbidden} must not appear here"


# ---------------------------------------------------------------------------
# 12. A provider cannot choose the project
# ---------------------------------------------------------------------------


def test_12_agent_plan_carries_no_identity_fields():
    """Structural guarantee: a plan cannot name a project, user or session."""
    for cls in (AgentPlan, PlannedOperation):
        names = {f.name for f in dataclasses.fields(cls)}
        for forbidden in ("project_id", "user_id", "session_id", "request_id"):
            assert forbidden not in names, f"{cls.__name__} exposes {forbidden}"


def test_12_request_project_mismatch_is_refused():
    built = build_job(request_project_id="proj_ATTACKER", ctx=context())
    assert built.ok is False
    assert any("does not match" in e.message for e in built.errors)


def test_12_job_project_always_comes_from_trusted_context():
    ctx = AgentContext(user_id="u", project_id="proj_trusted", session_id="s")
    built = build_job(ctx=ctx, request_project_id="proj_trusted")
    assert built.jobs[0].project_id == "proj_trusted"


def test_12_provider_never_reads_identity_from_the_message():
    """A message that mentions another project must not change identity."""
    result = RuleBasedProvider().interpret(
        chat_request("move Cube 50 cm right in project proj_other"), context()
    )
    # It simply fails to parse; nothing about identity is taken from text.
    assert result.ok is False


# ---------------------------------------------------------------------------
# 13. Layering
# ---------------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


#: Modules permitted to reach the operating system, with the reason.
#:
#: Spec 001 asserted that NO module in the agent package could import subprocess, os,
#: shutil or pathlib. That was an airtight proxy for the property that actually matters —
#: "interpreting language must never become executing something" — for as long as the
#: agent made no external calls at all.
#:
#: Reaching GPT-6 Astra through the locally installed Codex CLI makes the literal form of
#: that rule impossible: running a child process IS the integration. Rather than delete
#: the invariant, it is narrowed to the two modules whose entire job is to invoke one
#: fixed binary, and the real property is asserted directly below instead.
OS_ACCESS_ALLOWED = {
    "codex.py": "runs the fixed `codex` binary; the prompt goes on stdin, never in argv",
    "codex_astra.py": "passes platform-chosen image paths to the Codex client",
    "agent_input.py": (
        "types ReferenceImage.path, a platform-chosen location the provider attaches "
        "bytes from; it imports pathlib for the annotation and performs no I/O"
    ),
}

#: Forbidden everywhere, without exception. The agent still cannot touch Blender.
ALWAYS_FORBIDDEN = ["bpy", "blender_worker", "blender_mcp", "fcntl"]

#: Forbidden except in the allow-listed modules above.
OS_MODULES = ["subprocess", "shutil", "os", "pathlib"]


@pytest.mark.parametrize("forbidden", ALWAYS_FORBIDDEN)
def test_13_agent_package_never_reaches_blender_or_locks(forbidden):
    """The agent interprets language; it must never touch Blender or a lock."""
    for source in sorted(AGENT_PACKAGE.rglob("*.py")):
        assert forbidden not in _imported_modules(source), (
            f"{source.name} imports {forbidden}"
        )


@pytest.mark.parametrize("forbidden", OS_MODULES)
def test_13_only_the_codex_bridge_may_reach_the_operating_system(forbidden):
    """Every other agent module stays pure, so language cannot become execution."""
    for source in sorted(AGENT_PACKAGE.rglob("*.py")):
        if source.name in OS_ACCESS_ALLOWED:
            continue
        assert forbidden not in _imported_modules(source), (
            f"{source.name} imports {forbidden}; if that is deliberate, add it to "
            "OS_ACCESS_ALLOWED with a reason"
        )


def test_13_the_allowances_are_still_needed():
    """An allowance that stops being used must be removed, not left open."""
    for name in OS_ACCESS_ALLOWED:
        matches = list(AGENT_PACKAGE.rglob(name))
        assert matches, f"{name} is allow-listed but no longer exists"


def test_13_the_input_carrier_performs_no_io():
    """`agent_input.py` is allow-listed for a type annotation only; hold it to that."""
    source = AGENT_PACKAGE / "agent_input.py"
    tree = ast.parse(source.read_text("utf-8"))
    io_methods = {
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "unlink",
        "mkdir",
        "rglob",
        "glob",
        "exists",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            name = getattr(callee, "attr", None) or getattr(callee, "id", None)
            assert name not in io_methods, f"agent_input.py performs I/O via {name}()"


def test_13_only_one_module_may_start_a_process():
    """Process invocation is concentrated in a single reviewable place."""
    importers = [
        source.name
        for source in sorted(AGENT_PACKAGE.rglob("*.py"))
        if "subprocess" in _imported_modules(source)
    ]
    assert importers == ["codex.py"], (
        f"exactly one module may start a process, found {importers}"
    )


def test_13_the_prompt_is_never_built_into_a_command_line():
    """The property the old blanket rule was protecting, asserted directly.

    Untrusted text reaches Codex on stdin. If it were ever interpolated into argv it
    could influence how the process is invoked, so the command list must contain no
    f-string and no concatenation.
    """
    source = AGENT_PACKAGE / "codex.py"
    tree = ast.parse(source.read_text("utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        is_run = (
            isinstance(callee, ast.Attribute)
            and callee.attr == "run"
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "subprocess"
        )
        if not is_run:
            continue
        keywords = {keyword.arg for keyword in node.keywords}
        assert "input" in keywords, "the prompt must be passed via input=, not argv"
        assert "shell" not in keywords, "a shell must never be used"

    # The argument list is assembled from literals and configuration only.
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            for element in node.elts:
                assert not isinstance(element, ast.JoinedStr), (
                    "a command element must not be an f-string"
                )


def test_13_agent_does_not_execute_mcp_tools():
    for source in sorted(AGENT_PACKAGE.rglob("*.py")):
        text = source.read_text("utf-8")
        assert "handle_move_object" not in text
        assert "mcp_server" not in text


# ---------------------------------------------------------------------------
# 14. Unimplemented provider boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_cls", [AstraProvider, CodexProvider])
def test_14_unimplemented_providers_fail_clearly(provider_cls):
    provider = provider_cls()
    assert provider.configured is False

    result = provider.interpret(chat_request(), context())
    assert result.ok is False
    assert result.error.code == "PROVIDER_UNAVAILABLE"
    assert result.plan is None, "must not fabricate a plan"
    assert result.metadata.operation_count == 0


@pytest.mark.parametrize("provider_cls", [AstraProvider, CodexProvider])
def test_14_unimplemented_providers_satisfy_the_interface(provider_cls):
    """The boundary exists and is substitutable, it just does not work yet."""
    provider = provider_cls()
    assert isinstance(provider, AgentProvider)
    assert isinstance(provider.name, str) and provider.name
    assert provider.version == "unconfigured"


@pytest.mark.parametrize("module_name", ["astra", "codex"])
def test_14_unimplemented_providers_make_no_network_calls(module_name):
    source = AGENT_PACKAGE / "providers" / f"{module_name}.py"
    modules = _imported_modules(source)
    for forbidden in ("requests", "httpx", "urllib", "http", "socket", "openai"):
        assert forbidden not in modules


def test_14_registry_exposes_all_three_providers():
    assert available_provider_names() == ("astra", "codex", "rule_based")
    assert DEFAULT_PROVIDER_NAME == "rule_based"


def test_14_registry_rejects_an_unknown_provider():
    with pytest.raises(AgentProviderError):
        get_provider("gpt_9")


def test_14_rule_based_is_the_default_and_satisfies_the_protocol():
    provider = get_provider()
    assert isinstance(provider, RuleBasedProvider)
    assert isinstance(provider, AgentProvider)


# ---------------------------------------------------------------------------
# 15-16. Plan contents and multi-operation shape
# ---------------------------------------------------------------------------


def test_15_plan_contains_canonical_meters_not_raw_language():
    result = interpret()
    payload = result.plan.operations[0].payload

    assert isinstance(payload, MoveObjectPayload)
    assert isinstance(payload.delta_meters, Vec3)

    wire = to_wire(result.plan)
    text = repr(wire)
    for leaked in ("right", "50 cm", "cm", "centimeter", SPEC_COMMAND):
        assert leaked not in text, f"unresolved language leaked: {leaked!r}"


def test_15_plan_has_no_provider_internals_or_reasoning():
    names = {f.name for f in dataclasses.fields(PlannedOperation)}
    assert names == {"operation_type", "payload"}
    for forbidden in ("reasoning", "thoughts", "chain_of_thought", "raw_response"):
        assert forbidden not in names


def test_15_metadata_is_safe_and_carries_no_reasoning():
    metadata = interpret().metadata
    names = {f.name for f in dataclasses.fields(ProviderMetadata)}
    assert names == {"provider_name", "provider_version", "operation_count"}
    assert metadata.provider_name == "rule_based"
    assert metadata.operation_count == 1


def test_16_plan_can_represent_multiple_ordered_operations():
    """Spec 001 produces one, but the shape is not limited to one."""
    operations = tuple(
        PlannedOperation(
            operation_type="move_object",
            payload=MoveObjectPayload(
                target=ObjectRef(name="Cube"),
                delta_meters=Vec3(float(index) / 10, 0.0, 0.0),
            ),
        )
        for index in range(3)
    )
    plan = AgentPlan(operations=operations)

    assert plan.operation_count == 3
    assert [index for index, _ in plan.indexed()] == [0, 1, 2]


def test_16_job_factory_builds_one_job_per_operation_with_distinct_identities():
    operations = tuple(
        PlannedOperation(
            operation_type="move_object",
            payload=MoveObjectPayload(
                target=ObjectRef(name="Cube"),
                delta_meters=Vec3(0.5, 0.0, 0.0),
            ),
        )
        for _ in range(3)
    )
    built = JobFactory().build(
        AgentPlan(operations=operations),
        chat_request(request_id="req_multi"),
        context(),
        created_at=CREATED_AT,
    )

    assert built.ok, built.errors
    assert len(built.jobs) == 3
    assert [job.origin.operation_index for job in built.jobs] == [0, 1, 2]
    # Same request, different operations -> different mutation identities.
    assert len({job.idempotency_key for job in built.jobs}) == 3
    assert len({job.job_id for job in built.jobs}) == 3


def test_16_empty_plan_produces_no_jobs_without_failing():
    built = JobFactory().build(
        AgentPlan(), chat_request(), context(), created_at=CREATED_AT
    )
    assert built.ok
    assert built.jobs == []


def test_job_factory_refuses_an_unsupported_operation_type():
    plan = AgentPlan(
        operations=(
            PlannedOperation(operation_type="resize_object", payload=object()),
        )
    )
    built = JobFactory().build(
        plan, chat_request(), context(), created_at=CREATED_AT
    )
    assert built.ok is False
    assert built.errors[0].code == "VALIDATION_ERROR"
