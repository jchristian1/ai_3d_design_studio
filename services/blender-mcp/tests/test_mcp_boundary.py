"""MCP boundary tests (Spec 001, Task 4).

Covers request decoding, canonical-contract validation at the boundary, result
encoding, and the security constraints: exactly one semantic tool, no arbitrary
Python execution, no network exposure.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from blender_mcp.adapters.fake_scene import FakeSceneAdapter
from blender_mcp.mcp_server import (
    MOVE_OBJECT_TOOL_NAME,
    TOOL_HANDLERS,
    handle_move_object,
    list_tools,
    move_object_tool_descriptor,
    plan_from_wire,
    result_to_wire,
)
from blender_mcp.tools.move_object import move_object
from studio_contracts import SCHEMA_FILES, validate_against_schema
from studio_types import ObjectRef, Vec3

ORIGIN = Vec3(0.0, 0.0, 0.0)

VALID_REQUEST = {
    "job_id": "job_1",
    "target": {"name": "Cube"},
    "expected_before_meters": {"x": 0.0, "y": 0.0, "z": 0.0},
    "delta_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
    "desired_after_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
}


# ---------------------------------------------------------------------------
# Wire round trip
# ---------------------------------------------------------------------------


def test_handler_applies_and_returns_a_schema_valid_result():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = handle_move_object(VALID_REQUEST, scene)

    assert result["applied"] is True
    assert result["verified"] is True
    assert result["already_applied"] is False
    assert result["final_position_meters"] == {"x": 0.5, "y": 0.0, "z": 0.0}
    validated = validate_against_schema(SCHEMA_FILES["MoveObjectResult"], result)
    assert validated.valid, validated.violations


def test_handler_retry_returns_already_applied():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    first = handle_move_object(VALID_REQUEST, scene)
    second = handle_move_object(VALID_REQUEST, scene)

    assert first["applied"] is True
    assert second["already_applied"] is True
    assert second["applied"] is False
    assert second["final_position_meters"]["x"] == 0.5
    assert len(scene.writes) == 1


def test_every_handler_outcome_is_schema_valid():
    """Success, already-applied, conflict and not-found must all validate."""
    scenarios = [
        FakeSceneAdapter.with_object("Cube", ORIGIN),               # applies
        FakeSceneAdapter.with_object("Cube", Vec3(0.5, 0.0, 0.0)),  # already
        FakeSceneAdapter.with_object("Cube", Vec3(0.25, 0.0, 0.0)),  # conflict
        FakeSceneAdapter(),                                         # not found
    ]
    for scene in scenarios:
        result = handle_move_object(VALID_REQUEST, scene)
        validated = validate_against_schema(
            SCHEMA_FILES["MoveObjectResult"], result
        )
        assert validated.valid, (result, validated.violations)


def test_request_is_validated_against_the_canonical_contract():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    bad = dict(VALID_REQUEST)
    del bad["desired_after_meters"]
    result = handle_move_object(bad, scene)

    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert result["applied"] is False
    assert scene.writes == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"direction": "right"},
        {"unit": "cm"},
        {"distance": 50},
        {"message": "Move Cube 50 cm to the right"},
    ],
)
def test_unresolved_language_is_rejected_at_the_boundary(mutation):
    """The MCP layer must never accept units or directions."""
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = handle_move_object({**VALID_REQUEST, **mutation}, scene)
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert scene.writes == []


def test_unit_string_as_coordinate_is_rejected():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    bad = {**VALID_REQUEST, "delta_meters": {"x": "50 cm", "y": 0, "z": 0}}
    result = handle_move_object(bad, scene)
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert scene.writes == []


@pytest.mark.parametrize("request_value", [None, "move it", 42, [], {}])
def test_malformed_requests_return_structured_errors(request_value):
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    result = handle_move_object(request_value, scene)
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert result["applied"] is False
    assert result["verified"] is False


def test_plan_from_wire_decodes_a_typed_plan():
    plan = plan_from_wire(VALID_REQUEST)
    assert plan is not None
    assert plan.job_id == "job_1"
    assert plan.target == ObjectRef(object_id=None, name="Cube")
    assert plan.desired_after_meters == Vec3(0.5, 0.0, 0.0)


@pytest.mark.parametrize(
    "broken",
    [
        {**VALID_REQUEST, "target": "Cube"},
        {**VALID_REQUEST, "job_id": ""},
        {**VALID_REQUEST, "delta_meters": {"x": 0.5}},
    ],
)
def test_plan_from_wire_refuses_undecodable_requests(broken):
    assert plan_from_wire(broken) is None


def test_result_to_wire_omits_unset_optionals():
    scene = FakeSceneAdapter()
    result = move_object(plan_from_wire(VALID_REQUEST), scene)
    wire = result_to_wire(result)
    assert "previous_position_meters" not in wire
    assert "final_position_meters" not in wire
    assert wire["error"]["code"] == "OBJECT_NOT_FOUND"


def test_handler_never_raises_for_expected_failures():
    scene = FakeSceneAdapter.with_object("Cube", ORIGIN)
    scene.fail_write_with = "read-only scene"
    result = handle_move_object(VALID_REQUEST, scene)
    assert result["error"]["code"] == "MUTATION_FAILED"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_exactly_one_tool_is_exposed():
    tools = list_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == MOVE_OBJECT_TOOL_NAME
    assert list(TOOL_HANDLERS) == [MOVE_OBJECT_TOOL_NAME]


def test_tool_descriptor_uses_the_canonical_schemas():
    """The descriptor must not restate the schema, or it would drift."""
    descriptor = move_object_tool_descriptor()
    assert descriptor["input_schema"]["$id"] == "move-object-plan.schema.json"
    assert descriptor["output_schema"]["$id"] == "move-object-result.schema.json"


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "blender_mcp"


def _sources() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _parsed() -> list[tuple[Path, ast.Module]]:
    """Parse every source file.

    The security checks below inspect the AST rather than raw text: text matching
    would trip over the words "socket" or "execute_python" appearing in a
    docstring that explains why they are absent, and would miss a real call
    written with unusual spacing.
    """
    return [(p, ast.parse(p.read_text("utf-8"))) for p in _sources()]


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
            modules.add(node.module)
    return modules


def _called_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_no_arbitrary_python_execution_tool_exists():
    for name in TOOL_HANDLERS:
        assert "python" not in name.lower()
        assert "exec" not in name.lower()
        assert "eval" not in name.lower()
    assert "execute_python" not in TOOL_HANDLERS


@pytest.mark.parametrize("forbidden", ["eval", "exec", "compile", "execute_python"])
def test_no_arbitrary_code_execution_is_called(forbidden):
    """No source may call eval/exec/compile or expose execute_python."""
    for path, tree in _parsed():
        assert forbidden not in _called_names(tree), f"{path.name} calls {forbidden}"
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert forbidden not in functions, f"{path.name} defines {forbidden}"


@pytest.mark.parametrize(
    "forbidden",
    ["socket", "socketserver", "http", "flask", "uvicorn", "fastapi", "asyncio"],
)
def test_mcp_layer_does_not_import_a_network_listener(forbidden):
    """Blender must never be reachable from the network."""
    for path, tree in _parsed():
        assert forbidden not in _imported_modules(tree), (
            f"{path.name} imports {forbidden}"
        )


def test_no_mcp_sdk_dependency_was_added():
    """The transport boundary stays dependency-free at this milestone."""
    for path, tree in _parsed():
        assert "mcp" not in _imported_modules(tree), f"{path.name} imports mcp"


def test_only_the_blender_adapter_imports_bpy():
    """bpy must stay isolated so the domain layer is testable without Blender."""
    importers = [
        path.name for path, tree in _parsed() if "bpy" in _imported_modules(tree)
    ]
    assert importers == ["blender_scene.py"], importers


def test_domain_layer_does_not_import_transport():
    """The service must not depend on MCP transport details."""
    domain_path = PACKAGE_ROOT / "tools" / "move_object.py"
    modules = _imported_modules(ast.parse(domain_path.read_text("utf-8")))
    assert not any("mcp" in module for module in modules)
    assert "bpy" not in modules
