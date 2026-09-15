"""Editing the object the user selected in the browser, over and over.

Spec 002, Task 13. OFFLINE: real HTTP, real ASGI server, real WebSocket, real routes,
real context construction, real validation, real job contracts, real locks and journal
— with a scripted model and an in-memory Blender (see ``studio_fixtures.design_stack``).

The product claim under test is the iterative loop, which is what makes this a design
tool rather than a one-shot generator:

    click an object  ->  "make this taller"  ->  it changes  ->  "a bit more"

For that to work, four things must hold, and each has its own scenario below:

1. the selected object reaches the model as an ID, so "this" is unambiguous;
2. the mutation lands on THAT object and on nothing else;
3. the next turn is grounded in the CURRENT scene, so a relative instruction
   ("20 cm taller") measures from what Blender now holds, not from what the user
   originally asked for;
4. repeating a turn does not apply it twice.

VERIFICATION DISCIPLINE
-----------------------
No scenario concludes anything from a 202 or from an absent exception. Every geometric
claim is read back out of the backend's own state after the worker reported, and the
control plane's cached scene is checked separately, because those are two different
facts: what Blender holds, and what the browser will be told.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")
pytest.importorskip("httpx", reason="httpx not installed")
pytest.importorskip("websockets", reason="websockets not installed")

from studio_agent.providers.fake_llm import operation  # noqa: E402
from studio_fixtures.design_stack import (  # noqa: E402
    DesignStack,
    FakeObject,
    build_design_stack,
)

NORTH_ID = "obj_wall_north"
SOUTH_ID = "obj_wall_south"

#: The walls start at the common 2.40 m ceiling height.
START_HEIGHT = 2.40
TOLERANCE = 1e-9


def _walls() -> dict[str, FakeObject]:
    """Two walls, 2.40 m tall, each with a stable studio id."""
    return {
        "Wall_North": FakeObject(
            name="Wall_North",
            position=(0.0, 2.0, START_HEIGHT / 2),
            dimensions=(4.0, 0.12, START_HEIGHT),
            studio_object_id=NORTH_ID,
        ),
        "Wall_South": FakeObject(
            name="Wall_South",
            position=(0.0, -2.0, START_HEIGHT / 2),
            dimensions=(4.0, 0.12, START_HEIGHT),
            studio_object_id=SOUTH_ID,
        ),
    }


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[DesignStack]:
    with build_design_stack(tmp_path, objects=_walls()) as running:
        yield running


def _raise_to(object_id: str, height: float, *, label: str) -> dict:
    """The plan a model would propose to set one wall's height."""
    return operation(
        "set_object_dimensions",
        {
            "object_id": object_id,
            "desired_dimensions_meters": {"x": 4.0, "y": 0.12, "z": height},
        },
        label=label,
    )


def _height_of(stack: DesignStack, object_id: str) -> float:
    return stack.object_with_id(object_id).dimensions[2]


# ===========================================================================
# 1. the selection reaches the model
# ===========================================================================


def test_the_selected_object_id_reaches_the_model_as_context(stack: DesignStack):
    """"Make this taller" is only answerable because the ID travelled with it."""
    stack.llm.queue_plan(
        [_raise_to(NORTH_ID, 2.70, label="raise the north wall")],
        message="Raising the selected wall to 2.70 m.",
    )

    stack.run_turn("req_sel_001", "Make this 30 cm taller.", selected_object_id=NORTH_ID)

    seen = stack.llm.last_input
    assert seen is not None
    assert seen.selected_object_id == NORTH_ID, (
        "the model was asked to interpret 'this' without being told what is selected"
    )
    # The user's words are preserved exactly; the platform adds context, it does not
    # rewrite the request.
    assert seen.user_text == "Make this 30 cm taller."


# ===========================================================================
# 2. the mutation lands on that object only
# ===========================================================================


def test_editing_the_selected_wall_changes_only_that_wall(stack: DesignStack):
    stack.llm.queue_plan(
        [_raise_to(NORTH_ID, 2.70, label="raise the north wall")],
        message="Raising the selected wall to 2.70 m.",
    )

    final = stack.run_turn(
        "req_sel_010", "Make this 30 cm taller.", selected_object_id=NORTH_ID
    )

    assert final["job_status"] == "succeeded", final
    assert _height_of(stack, NORTH_ID) == pytest.approx(2.70, abs=TOLERANCE)
    assert _height_of(stack, SOUTH_ID) == pytest.approx(START_HEIGHT, abs=TOLERANCE), (
        "the unselected wall was modified"
    )

    # The mutation was addressed by STABLE ID, not by display name: renaming the wall in
    # Blender must not break the next edit.
    mutations = [
        request
        for request in stack.backend.invocations
        if request.capability == "set_object_dimensions"
    ]
    assert len(mutations) == 1
    assert mutations[0].argument("object_id") == NORTH_ID
    assert mutations[0].argument("name") is None


def test_the_browser_is_told_the_new_height(stack: DesignStack):
    """The cached scene is a separate fact from Blender's state, so it is checked too."""
    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.70, label="raise the north wall")])

    stack.run_turn("req_sel_020", "Raise this to 2.7 m.", selected_object_id=NORTH_ID)

    cached = stack.cached_object(NORTH_ID)
    assert cached is not None, "the workspace never learned about the selected object"
    assert cached["dimensions_meters"]["z"] == pytest.approx(2.70, abs=TOLERANCE)

    # And it is reachable the way the browser actually reads it.
    scene = stack.scene()
    assert scene is not None and scene["scene_version"]
    workspace = stack.workspace()
    assert workspace["scene"]["scene_version"] == scene["scene_version"]


# ===========================================================================
# 3. the next turn measures from the CURRENT scene
# ===========================================================================


def test_a_second_relative_edit_builds_on_the_first(stack: DesignStack):
    """"20 cm taller" after "raise to 2.70" must reach 2.90, not 2.60."""
    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.70, label="raise the north wall")])
    stack.run_turn("req_sel_030", "Raise this to 2.7 m.", selected_object_id=NORTH_ID)

    # The model is given the scene, so it can add 0.20 to what is actually there. This
    # test asserts the PLATFORM's half of that contract: the grounding it receives.
    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.90, label="raise the north wall again")])
    stack.run_turn("req_sel_031", "Make it 20 cm taller.", selected_object_id=NORTH_ID)

    second_input = stack.llm.seen[-1]
    assert second_input.scene is not None, (
        "the second turn was planned blind: no scene was supplied"
    )
    observed = {
        entry.studio_object_id: entry.dimensions_meters.z
        for entry in second_input.scene.objects
        if entry.studio_object_id
    }
    assert observed[NORTH_ID] == pytest.approx(2.70, abs=TOLERANCE), (
        "the model was shown the wall's ORIGINAL height, so a relative edit would "
        "silently undo the first one"
    )

    assert _height_of(stack, NORTH_ID) == pytest.approx(2.90, abs=TOLERANCE)
    assert _height_of(stack, SOUTH_ID) == pytest.approx(START_HEIGHT, abs=TOLERANCE)


def test_the_selection_survives_across_turns_without_being_resent_by_the_model(
    stack: DesignStack,
):
    """The browser owns the selection; the model never has to remember it."""
    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.70, label="raise")])
    stack.run_turn("req_sel_040", "Raise this to 2.7 m.", selected_object_id=NORTH_ID)

    # Second turn: the user has since selected the OTHER wall.
    stack.llm.queue_plan([_raise_to(SOUTH_ID, 2.70, label="raise the other one")])
    stack.run_turn("req_sel_041", "This one too.", selected_object_id=SOUTH_ID)

    assert stack.llm.seen[-1].selected_object_id == SOUTH_ID
    assert _height_of(stack, SOUTH_ID) == pytest.approx(2.70, abs=TOLERANCE)


def test_a_plan_made_against_a_stale_scene_is_refused_rather_than_applied(
    stack: DesignStack,
):
    """The scene version the model planned against is enforced in-lock.

    This is the protection that makes the cached scene safe to use for grounding: if
    anything moved in between, the plan does not run.
    """
    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.70, label="raise")])
    stack.run_turn("req_sel_050", "Raise this to 2.7 m.", selected_object_id=NORTH_ID)

    # Something outside this conversation changes the project — a second worker, the
    # user in Blender, an earlier job finishing late.
    stack.object_with_id(SOUTH_ID).dimensions = (4.0, 0.12, 3.10)

    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.90, label="raise again")])
    response = stack.chat(
        "req_sel_051", "Make it 20 cm taller.", selected_object_id=NORTH_ID
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert stack.pump_until()
    final = stack.await_status(job_id, "failed")

    assert final["error"]["code"] == "SCENE_VERSION_MISMATCH", final
    assert _height_of(stack, NORTH_ID) == pytest.approx(2.70, abs=TOLERANCE), (
        "the stale plan was applied anyway"
    )


# ===========================================================================
# 4. repeating a turn does not apply it twice
# ===========================================================================


def test_resubmitting_the_same_request_does_not_edit_twice(stack: DesignStack):
    """The browser retrying a request must not raise the wall a second time."""
    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.70, label="raise")])
    first = stack.run_turn("req_sel_060", "Raise this to 2.7 m.", selected_object_id=NORTH_ID)
    assert _height_of(stack, NORTH_ID) == pytest.approx(2.70, abs=TOLERANCE)
    mutations_after_first = len(
        [r for r in stack.backend.invocations if r.capability == "set_object_dimensions"]
    )

    # The SAME request id, as a browser retry would send.
    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.70, label="raise")])
    again = stack.chat("req_sel_060", "Raise this to 2.7 m.", selected_object_id=NORTH_ID)
    assert again.status_code == 202, again.text
    body = again.json()
    assert body["job_id"] == first["job_id"], "a retry created a second job"
    assert body["duplicate"] is True

    assert _height_of(stack, NORTH_ID) == pytest.approx(2.70, abs=TOLERANCE)
    assert (
        len([r for r in stack.backend.invocations if r.capability == "set_object_dimensions"])
        == mutations_after_first
    ), "the retry re-invoked the mutation"


def test_an_identical_plan_under_a_new_request_id_is_recognised_as_already_applied(
    stack: DesignStack,
):
    """The user asking for the same thing twice is not an error, and not a double edit.

    A NEW request id means a new job, so idempotency cannot help here. What protects the
    project is the per-step "already satisfied?" check, which reads the real scene first.
    """
    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.70, label="raise")])
    stack.run_turn("req_sel_070", "Raise this to 2.7 m.", selected_object_id=NORTH_ID)
    saves_after_first = stack.backend.saves

    stack.llm.queue_plan([_raise_to(NORTH_ID, 2.70, label="raise")])
    final = stack.run_turn(
        "req_sel_071", "Raise this to 2.7 m please.", selected_object_id=NORTH_ID
    )

    assert final["job_status"] == "succeeded", final
    assert final["result"]["already_applied"] == 1, final["result"]
    assert final["result"]["applied"] == 0, final["result"]
    assert _height_of(stack, NORTH_ID) == pytest.approx(2.70, abs=TOLERANCE)
    assert stack.backend.saves == saves_after_first, (
        "the project was saved again for a change that was already true"
    )
