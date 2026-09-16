"""The first turn about a project must be grounded, not blind.

Offline, through the real HTTP surface (see ``studio_fixtures.design_stack``).

This suite exists because of a real dead end found while using the studio. On a fresh
project the scene cache was empty, so the prompt said the project had not been read, and
the model replied "I'll check the current scene first" — an ANSWER, which changes nothing.
The user was left watching a chat message for work that was never dispatched.

Reading the project is the platform's job. These scenarios assert it happens, that it
happens once, that it is not required for the turn to work, and that the model is never
handed a prompt inviting it to go and look.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("websockets", reason="websockets not installed")

from studio_agent.providers.codex_astra import build_prompt  # noqa: E402
from studio_agent.providers.fake_llm import operation  # noqa: E402
from studio_fixtures.design_stack import (  # noqa: E402
    DesignStack,
    FakeObject,
    build_design_stack,
)


def _scene() -> dict[str, FakeObject]:
    return {
        "Cube": FakeObject(
            name="Cube", dimensions=(2.0, 2.0, 2.0), studio_object_id="obj_cube"
        )
    }


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[DesignStack]:
    # A real budget, because these scenarios are ABOUT the first turn waiting for the
    # project to be read. Every other suite leaves it at zero.
    with build_design_stack(
        tmp_path, objects=_scene(), scene_grounding_timeout=20.0
    ) as running:
        yield running


def _pump_scene_read(stack: DesignStack) -> None:
    """Serve the grounding job the API dispatches during the first turn.

    The harness pumps the worker by hand, so the read has to be answered from another
    thread while the chat request is still open — which is exactly what a real worker
    does, and what makes this a test of the real handshake.
    """
    import threading

    thread = threading.Thread(target=stack.pump_until, daemon=True)
    thread.start()
    return thread


def test_the_first_turn_is_given_the_real_scene(stack: DesignStack):
    """The platform reads the project before the model is asked anything about it."""
    assert stack.repositories.scenes.get(stack.project_id) is None

    stack.llm.queue_answer("There is one cube in the project.")
    worker = _pump_scene_read(stack)
    response = stack.chat("req_ground_001", "What is in this project?")
    worker.join(timeout=10)

    assert response.status_code == 200, response.text

    seen = stack.llm.last_input
    assert seen is not None
    assert seen.scene is not None, "the model was asked about a project it could not see"
    assert [obj.studio_object_id for obj in seen.scene.objects] == ["obj_cube"]

    # And the read really came from Blender, not from an assumption.
    assert "inspect_scene" in stack.invoked_capabilities()


def test_the_prompt_never_invites_the_model_to_go_and_look(stack: DesignStack):
    """A prompt that says "not read yet" gets "let me check first", which does nothing."""
    stack.llm.queue_answer("One cube.")
    worker = _pump_scene_read(stack)
    stack.chat("req_ground_010", "What is in this project?")
    worker.join(timeout=10)

    prompt = build_prompt(stack.llm.last_input)
    assert "has not been read yet" not in prompt
    assert "cannot look at anything between turns" in prompt.lower()
    assert "never say you will check" in prompt.lower()
    # The scene it CAN see is stated instead.
    assert "Cube" in prompt


def test_the_project_is_read_once_not_before_every_message(stack: DesignStack):
    """Grounding is a cold-start step; after that the cache follows every job."""
    stack.llm.queue_answer("One cube.")
    worker = _pump_scene_read(stack)
    stack.chat("req_ground_020", "What is in this project?")
    worker.join(timeout=10)

    reads = stack.invoked_capabilities().count("inspect_scene")
    assert reads >= 1

    stack.llm.queue_answer("Still one cube.")
    second = stack.chat("req_ground_021", "And now?")
    assert second.status_code == 200, second.text

    assert stack.invoked_capabilities().count("inspect_scene") == reads, (
        "the project was read again for a turn that already had a scene"
    )
    assert stack.llm.seen[-1].scene is not None


def test_a_first_turn_that_asks_for_work_reaches_blender(stack: DesignStack):
    """The end-to-end shape of the bug: first message, real request, real change."""
    stack.llm.queue_plan(
        [
            operation(
                "create_object",
                {
                    "primitive": "cube",
                    "display_name": "Coffee_Table_Top",
                    "object_id": "obj_table_top",
                    "position_meters": {"x": 0.0, "y": 0.0, "z": 0.45},
                    "dimensions_meters": {"x": 1.2, "y": 1.2, "z": 0.08},
                },
                label="build the tabletop",
            )
        ],
        message="Building the tabletop.",
    )

    worker = _pump_scene_read(stack)
    response = stack.chat("req_ground_030", "Make a round coffee table 1.2 m wide.")
    worker.join(timeout=10)

    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert stack.pump_until(), "the plan was never offered to the worker"
    final = stack.await_status(job_id, "succeeded")

    assert final["result"]["applied"] == 1
    top = stack.object_named("Coffee_Table_Top")
    assert top.dimensions == pytest.approx((1.2, 1.2, 0.08))
    assert top.position[2] == pytest.approx(0.45)


def test_a_scene_read_creates_no_recovery_point(stack: DesignStack):
    """Reading is not mutating, so it must not copy the .blend."""
    stack.llm.queue_answer("One cube.")
    worker = _pump_scene_read(stack)
    stack.chat("req_ground_040", "What is in this project?")
    worker.join(timeout=10)

    assert stack.repositories.scenes.get(stack.project_id) is not None
    recovery = list((stack.runtime_root / "recovery").rglob("*"))
    copies = [path for path in recovery if path.is_file()]
    assert copies == [], f"a read-only plan made recovery copies: {copies}"


def test_a_turn_still_works_when_the_project_cannot_be_read(tmp_path: Path):
    """Grounding is an improvement to the turn, never a precondition for answering."""
    with build_design_stack(
        tmp_path, objects=_scene(), scene_grounding_timeout=20.0, connect=False
    ) as stack:
        stack.llm.queue_answer("The design machine is not connected right now.")
        response = stack.chat("req_ground_050", "What is in this project?")

        assert response.status_code == 200, response.text
        seen = stack.llm.last_input
        assert seen is not None
        assert seen.scene is None
        assert seen.blender_available is False
        # And the prompt tells the model what that means, rather than offering a look.
        prompt = build_prompt(seen)
        assert "could NOT be read" in prompt
