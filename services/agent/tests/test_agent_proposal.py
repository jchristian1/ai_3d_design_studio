"""Model output is untrusted. This is where it stops being dangerous."""

from __future__ import annotations

import json

import pytest

from studio_agent.outcome import (
    AgentError,
    Answer,
    Clarification,
    PlanProposal,
    ProviderMetadata,
    creates_mutations,
)
from studio_agent.proposal import (
    MAX_OPERATIONS,
    ArgumentError,
    parse_agent_response,
    validate_arguments,
)
from studio_agent.providers.fake_llm import operation, response_body

METADATA = ProviderMetadata(provider_name="test", provider_version="1")


def parse(body: object) -> object:
    return parse_agent_response(body, METADATA)


# --- the three kinds ------------------------------------------------------


def test_an_answer_becomes_an_answer_outcome() -> None:
    outcome = parse(response_body("answer", message="There are three walls."))
    assert isinstance(outcome, Answer)
    assert outcome.text == "There are three walls."
    assert not creates_mutations(outcome)


def test_a_clarification_carries_the_question_and_the_missing_facts() -> None:
    outcome = parse(
        response_body(
            "clarification",
            question="What is the ceiling height?",
            missing_information=["ceiling_height_m"],
        )
    )
    assert isinstance(outcome, Clarification)
    assert outcome.question == "What is the ceiling height?"
    assert outcome.missing_information == ("ceiling_height_m",)
    assert not creates_mutations(outcome)


def test_a_plan_becomes_ordered_validated_operations() -> None:
    outcome = parse(
        response_body(
            "plan",
            message="Building the room.",
            operations=[
                operation(
                    "create_floor",
                    {
                        "display_name": "Floor",
                        "footprint_meters": [
                            {"x": 0.0, "y": 0.0},
                            {"x": 4.0, "y": 0.0},
                            {"x": 4.0, "y": 3.0},
                        ],
                        "thickness_meters": 0.2,
                    },
                    label="Creating the floor",
                ),
                operation(
                    "create_wall",
                    {
                        "display_name": "Wall_North",
                        "start_meters": {"x": 0.0, "y": 0.0},
                        "end_meters": {"x": 4.0, "y": 0.0},
                        "height_meters": 2.4,
                        "thickness_meters": 0.12,
                    },
                ),
            ],
        )
    )
    assert isinstance(outcome, PlanProposal)
    assert creates_mutations(outcome)
    assert outcome.operation_count == 2
    assert [op.operation_index for op in outcome.operations] == [0, 1]
    assert outcome.operations[0].label == "Creating the floor"
    assert outcome.operations[0].capability == "create_floor"
    assert outcome.metadata.operation_count == 2


# --- malformed output fails closed ---------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "not an object",
        42,
        None,
        {},
        {"kind": "answer"},
        {"kind": "nonsense", "message": "", "question": "", "missing_information": [], "operations": []},
        {"kind": "answer", "message": "hi", "question": "", "missing_information": [], "operations": [], "extra": 1},
    ],
)
def test_malformed_output_becomes_a_structured_error(body: object) -> None:
    outcome = parse(body)
    assert isinstance(outcome, AgentError)
    assert outcome.code == "VALIDATION_ERROR"
    assert not creates_mutations(outcome)


def test_an_empty_answer_is_refused() -> None:
    assert isinstance(parse(response_body("answer", message="   ")), AgentError)


def test_a_plan_with_no_operations_is_refused() -> None:
    outcome = parse(response_body("plan", message="Done!"))
    assert isinstance(outcome, AgentError)
    assert "no operations" in outcome.message


def test_an_unknown_capability_is_refused() -> None:
    outcome = parse(
        response_body("plan", operations=[operation("delete_the_user_home", {})])
    )
    assert isinstance(outcome, AgentError)


def test_a_capability_the_agent_may_not_propose_is_refused() -> None:
    """export_glb is platform-initiated; the model must not spend a turn on it."""
    outcome = parse(
        response_body("plan", operations=[operation("export_glb", {"output_path": "/tmp/x"})])
    )
    assert isinstance(outcome, AgentError)


def test_arguments_that_are_not_json_are_refused() -> None:
    outcome = parse(
        response_body(
            "plan",
            operations=[
                {"capability": "inspect_scene", "label": "look", "arguments_json": "{oh no"}
            ],
        )
    )
    assert isinstance(outcome, AgentError)
    assert "valid JSON" in outcome.message


def test_a_runaway_plan_is_refused() -> None:
    many = [
        operation("inspect_object", {"name": f"Object_{index}"})
        for index in range(MAX_OPERATIONS + 1)
    ]
    outcome = parse(response_body("plan", operations=many))
    assert isinstance(outcome, AgentError)
    assert "exceeds" in outcome.message


def test_a_long_label_is_truncated_rather_than_rejected() -> None:
    outcome = parse(
        response_body(
            "plan",
            operations=[operation("inspect_scene", {}, label="x" * 500)],
        )
    )
    assert isinstance(outcome, PlanProposal)
    assert len(outcome.operations[0].label) <= 160


# --- argument validation -------------------------------------------------


def test_absolute_targets_are_validated_and_normalised() -> None:
    validated = validate_arguments(
        "move_object",
        {"object_id": "obj_wall_1", "desired_position_meters": {"x": 1, "y": 2, "z": 3}},
    )
    assert validated["desired_position_meters"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert validated["object_id"] == "obj_wall_1"


@pytest.mark.parametrize(
    "capability, arguments, fragment",
    [
        ("move_object", {"desired_position_meters": {"x": 0, "y": 0, "z": 0}}, "one of"),
        ("move_object", {"object_id": "o"}, "desired_position_meters is required"),
        (
            "move_object",
            {"object_id": "o", "desired_position_meters": {"x": 0, "y": 0}},
            "z must be a number",
        ),
        (
            "move_object",
            {"object_id": "o", "desired_position_meters": {"x": float("nan"), "y": 0, "z": 0}},
            "finite",
        ),
        (
            "move_object",
            {"object_id": "o", "desired_position_meters": {"x": 0, "y": 0, "z": 0}, "sneaky": 1},
            "unexpected arguments",
        ),
        ("create_wall", {"display_name": "W"}, "required"),
        (
            "create_wall",
            {
                "display_name": "W",
                "start_meters": {"x": 0, "y": 0},
                "end_meters": {"x": 1, "y": 0},
                "height_meters": 0.0,
                "thickness_meters": 0.12,
            },
            "greater than zero",
        ),
        (
            "create_floor",
            {
                "display_name": "F",
                "footprint_meters": [{"x": 0, "y": 0}, {"x": 1, "y": 0}],
                "thickness_meters": 0.2,
            },
            "three points",
        ),
        (
            "set_material_color",
            {"object_id": "o", "color_linear_srgb": {"r": 1.5, "g": 0, "b": 0, "a": 1}},
            "[0, 1]",
        ),
        ("execute_blender_python", {}, "code is required"),
        ("execute_blender_python", {"code": "   "}, "non-empty"),
    ],
)
def test_bad_arguments_are_refused(capability, arguments, fragment) -> None:
    with pytest.raises(ArgumentError) as error:
        validate_arguments(capability, arguments)
    assert fragment in str(error.value)


def test_optional_arguments_may_be_omitted_or_null() -> None:
    validated = validate_arguments(
        "create_wall",
        {
            "display_name": "Wall",
            "start_meters": {"x": 0, "y": 0},
            "end_meters": {"x": 4, "y": 0},
            "height_meters": 2.4,
            "thickness_meters": 0.12,
            "base_elevation_meters": None,
            "color_linear_srgb": None,
        },
    )
    assert "base_elevation_meters" not in validated
    assert "color_linear_srgb" not in validated


def test_a_colour_alpha_defaults_to_opaque() -> None:
    validated = validate_arguments(
        "set_material_color",
        {"name": "Cube", "color_linear_srgb": {"r": 0.5, "g": 0.4, "b": 0.3}},
    )
    assert validated["color_linear_srgb"]["a"] == 1.0


def test_every_proposable_capability_has_an_argument_spec() -> None:
    from studio_agent.proposal import SPECS
    from studio_contracts.capabilities import PROPOSABLE_CAPABILITIES

    assert set(SPECS) == set(PROPOSABLE_CAPABILITIES)


# --- injection attempts -------------------------------------------------


def test_a_model_cannot_smuggle_a_path_into_a_capability() -> None:
    outcome = parse(
        response_body(
            "plan",
            operations=[
                operation(
                    "move_object",
                    {
                        "object_id": "obj_1",
                        "desired_position_meters": {"x": 0, "y": 0, "z": 0},
                        "output_path": "/home/christian/.ssh/id_rsa",
                    },
                )
            ],
        )
    )
    assert isinstance(outcome, AgentError)
    assert "unexpected arguments" in outcome.message


def test_model_authored_code_is_accepted_as_data_not_executed() -> None:
    """Parsing must never run the code. It becomes an argument, nothing more."""
    dangerous = "import shutil\nshutil.rmtree('/home/christian')\n"
    outcome = parse(
        response_body("plan", operations=[operation("execute_blender_python", {"code": dangerous})])
    )
    assert isinstance(outcome, PlanProposal)
    step = outcome.operations[0]
    assert step.is_model_authored_code
    assert step.code() == dangerous
    # Classification and approval happen at the capability boundary, not here.
