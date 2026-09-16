"""What the agent sees, and what it must never see."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio_agent.providers.codex_astra import build_prompt
from studio_api.context_builder import ContextBuilder
from studio_api.ingest import ReferenceIngestService
from studio_api.storage import ReferenceFileStore, StudioDatabase, StudioRepositories
from studio_api.storage.models import ClarificationRecord

from studio_fixtures.sample_files import jpeg_bytes, pdf_bytes, png_bytes

PROJECT = "proj_a"


@pytest.fixture()
def parts(tmp_path: Path):
    database = StudioDatabase(tmp_path / "studio.sqlite3")
    repositories = StudioRepositories(database)
    repositories.projects.ensure(PROJECT, "Beach House")
    files = ReferenceFileStore(tmp_path / "references")
    ingest = ReferenceIngestService(repositories=repositories, files=files, max_pdf_pages=3)
    builder = ContextBuilder(repositories=repositories, files=files)
    return builder, ingest, repositories


def build(builder, **kwargs):
    base = {
        "user_text": "what do you see?",
        "user_id": "user_dev_local",
        "project_id": PROJECT,
        "session_id": "sess_1",
    }
    base.update(kwargs)
    return builder.build(**base)


# --- attachment -----------------------------------------------------------


def test_an_attached_image_is_sent_as_a_real_file(parts) -> None:
    builder, ingest, _ = parts
    reference = ingest.ingest(PROJECT, "room.jpg", jpeg_bytes()).reference

    agent_input = build(builder, attached_reference_ids=[reference.reference_id])
    assert len(agent_input.images) == 1
    image = agent_input.images[0]
    assert image.reference_id == reference.reference_id
    assert image.path.exists(), "the provider must be handed bytes that exist"
    assert image.media_type == "image/jpeg"


def test_an_attached_pdf_is_expanded_into_its_page_images(parts) -> None:
    builder, ingest, _ = parts
    result = ingest.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=2))

    agent_input = build(builder, attached_reference_ids=[result.reference.reference_id])
    assert [image.page_number for image in agent_input.images] == [1, 2]
    # And its extracted text comes along, so dimension labels are readable as text too.
    assert any("CEILING HEIGHT 2400" in document.text for document in agent_input.documents)


def test_a_reference_from_another_project_cannot_be_attached(parts) -> None:
    builder, ingest, repositories = parts
    repositories.projects.ensure("proj_b", "Other")
    reference = ingest.ingest(PROJECT, "room.jpg", jpeg_bytes()).reference

    agent_input = builder.build(
        user_text="look",
        user_id="u",
        project_id="proj_b",
        session_id="s",
        attached_reference_ids=[reference.reference_id],
    )
    assert agent_input.images == ()


def test_the_image_budget_is_enforced(parts) -> None:
    builder, ingest, _ = parts
    builder.max_images = 2
    ids = [
        ingest.ingest(PROJECT, f"photo{index}.png", png_bytes(width=8 + index)).reference.reference_id
        for index in range(5)
    ]
    agent_input = build(builder, attached_reference_ids=ids)
    assert len(agent_input.images) == 2, "cost is bounded even when more is attached"


def test_a_document_is_sent_as_text_not_as_an_image(parts) -> None:
    builder, ingest, _ = parts
    reference = ingest.ingest(PROJECT, "brief.md", b"# Brief\nCeiling 2.4 m").reference
    agent_input = build(builder, attached_reference_ids=[reference.reference_id])
    assert agent_input.images == ()
    assert "Ceiling 2.4 m" in agent_input.documents[0].text


# --- relevance ------------------------------------------------------------


def test_a_reference_mentioned_by_name_is_included_without_being_attached(parts) -> None:
    builder, ingest, _ = parts
    ingest.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=1))
    ingest.ingest(PROJECT, "sofa.jpg", jpeg_bytes())

    agent_input = build(builder, user_text="use the floor-plan please")
    labels = [image.label for image in agent_input.images]
    assert labels and all("floor-plan" in label for label in labels)
    assert not any("sofa" in label for label in labels)


def test_a_reference_keeps_being_shown_until_something_is_built(parts) -> None:
    """While there is nothing to look at, the drawing IS the subject.

    "Shown once" was too thrifty here. A real session went: upload a plan, ask for it to be
    modelled, then "invent the door heights, make it look great" — and that turn arrived
    with no image, so Astra asked for the plan to be attached again. The cost is bounded:
    it stops the moment a model exists, and each image is a downscaled copy.
    """
    builder, ingest, _ = parts
    ingest.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=1))

    first = build(builder, user_text="build this")
    assert len(first.images) == 1

    still = build(builder, user_text="invent the door heights, make it look great")
    assert len(still.images) == 1, "the plan must still be visible while nothing is built"

    # Once there is a model, it stops: an edit is about the scene, not the drawing.
    later = build(
        builder,
        user_text="thanks, that is all for today",
        scene=_scene_with_one_wall(),
    )
    assert later.images == ()
    assert any("floor-plan.pdf" in summary for summary in later.reference_summaries)


def test_a_visual_message_falls_back_to_recent_references(parts) -> None:
    builder, ingest, _ = parts
    ingest.ingest(PROJECT, "old.png", png_bytes(width=8))
    ingest.ingest(PROJECT, "newer.png", png_bytes(width=9))
    agent_input = build(builder, user_text="reconstruct this room")
    assert agent_input.images, "a modelling request should look at something"


def test_references_not_sent_are_summarised_rather_than_hidden(parts) -> None:
    builder, ingest, _ = parts
    attached = ingest.ingest(PROJECT, "room.jpg", jpeg_bytes()).reference
    ingest.ingest(PROJECT, "elevation.pdf", pdf_bytes(pages=4))

    agent_input = build(builder, attached_reference_ids=[attached.reference_id])
    summaries = " ".join(agent_input.reference_summaries)
    assert "elevation.pdf" in summaries
    assert "4 pages" in summaries
    assert "room.jpg" not in summaries, "what was sent is not also summarised"


# --- grounding ------------------------------------------------------------


def test_confirmed_facts_and_recent_turns_are_carried(parts) -> None:
    builder, _, repositories = parts
    repositories.facts.set(PROJECT, "ceiling_height_m", "2.4")
    from studio_api.storage.models import ConversationTurnRecord

    repositories.conversation.append(
        ConversationTurnRecord(
            project_id=PROJECT, session_id="s", role="user", text="earlier message"
        )
    )
    agent_input = build(builder)
    assert agent_input.project_facts == {"ceiling_height_m": "2.4"}
    assert agent_input.recent_turns[-1].text == "earlier message"


def test_recent_turns_are_bounded(parts) -> None:
    builder, _, repositories = parts
    builder.max_recent_turns = 3
    from studio_api.storage.models import ConversationTurnRecord

    for index in range(10):
        repositories.conversation.append(
            ConversationTurnRecord(
                project_id=PROJECT, session_id="s", role="user", text=f"msg {index}"
            )
        )
    assert len(build(builder).recent_turns) == 3


def test_a_reference_whose_bytes_vanished_is_skipped_rather_than_sent(parts) -> None:
    builder, ingest, repositories = parts
    reference = ingest.ingest(PROJECT, "room.jpg", jpeg_bytes()).reference
    ingest.files.delete(PROJECT, reference.stored_name)

    agent_input = build(builder, attached_reference_ids=[reference.reference_id])
    assert agent_input.images == (), "a path that would fail mid-call is not handed over"


# --- absence --------------------------------------------------------------


def test_the_assembled_prompt_leaks_no_path_or_secret(parts, monkeypatch) -> None:
    builder, ingest, repositories = parts
    monkeypatch.setenv("STUDIO_WORKER_TOKEN", "super-secret-worker-token")
    result = ingest.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=1))
    repositories.facts.set(PROJECT, "ceiling_height_m", "2.4")

    agent_input = build(builder, attached_reference_ids=[result.reference.reference_id])
    prompt = build_prompt(agent_input)

    assert str(ingest.files.root) not in prompt
    assert "/home/christian" not in prompt
    assert ".sqlite3" not in prompt
    assert "super-secret-worker-token" not in prompt
    assert "stored_name" not in prompt
    # And nothing about the Blender backend.
    assert "execute_blender_code" not in prompt
    assert "blender-mcp" not in prompt
    assert "bpy.ops" not in prompt


def test_the_agent_input_carries_no_other_projects_data(parts) -> None:
    builder, ingest, repositories = parts
    repositories.projects.ensure("proj_b", "Other")
    repositories.facts.set("proj_b", "secret_fact", "should not appear")
    ingest.ingest("proj_b", "other.png", png_bytes())

    agent_input = build(builder)
    serialized = json.dumps(
        {
            "facts": dict(agent_input.project_facts),
            "summaries": list(agent_input.reference_summaries),
            "images": [image.label for image in agent_input.images],
        }
    )
    assert "secret_fact" not in serialized
    assert "other.png" not in serialized



# ---------------------------------------------------------------------------
# Answering a question must not lose the drawing the question was about
# ---------------------------------------------------------------------------
#
# From a real session, and the reason these tests exist:
#
#   user:   "i want you to model this room"     + a sketch attached
#   Astra:  "what are the inside dimensions and the ceiling height?"
#   user:   "inches"
#   Astra:  "could you attach the room sketch again?"
#   user:   "117"
#   Astra:  "could you attach the dimensioned sketch again?"
#
# The sketch was stored the whole time. It was withheld because the follow-up messages
# carried no attachment and contained no word that looked visual, so Astra was shown a
# LIST OF FILENAMES and asked for what it had never seen — three times.


_sketch_size = 10


def _sketch(ingest, name: str = "room-sketch.jpeg"):
    """Upload one image the way the browser does, and return its record.

    Each one gets distinct bytes: identical bytes are DEDUPLICATED into a single
    reference, which would quietly make a multi-file test a one-file test.
    """
    global _sketch_size
    _sketch_size += 1
    return ingest.ingest(PROJECT, name, jpeg_bytes(width=_sketch_size, height=_sketch_size)).reference


def test_a_bare_answer_to_an_open_question_still_sees_the_sketch(parts) -> None:
    builder, ingest, repositories = parts
    _sketch(ingest)

    # Astra asked something, so a question is open.
    repositories.clarifications.add(
        ClarificationRecord(
            clarification_id="clr_1",
            project_id=PROJECT,
            session_id="sess_1",
            question="What is the floor-to-ceiling height?",
            missing_information=("ceiling_height",),
        )
    )

    # The user answers with a bare number, and attaches nothing.
    context = builder.build(
        user_text="117",
        user_id="user_1",
        project_id=PROJECT,
        session_id="sess_1",
        attached_reference_ids=(),
    )

    assert len(context.images) == 1, "the sketch was withheld while its question was open"
    assert context.images[0].label == "room-sketch.jpeg"


def test_a_freshly_uploaded_sketch_is_shown_without_being_attached(parts) -> None:
    """Drag a sketch in, type a message: it is sent, with no attaching to think about."""
    builder, ingest, _ = parts
    _sketch(ingest)

    context = builder.build(
        user_text="inches",
        user_id="user_1",
        project_id=PROJECT,
        session_id="sess_1",
    )

    assert len(context.images) == 1


def test_the_newest_uploads_are_the_ones_offered(parts) -> None:
    """"The sketch" means the last thing the user uploaded."""
    builder, ingest, _ = parts
    builder.max_images = 2
    for index in range(4):
        _sketch(ingest, f"sketch-{index}.jpeg")

    context = builder.build(
        user_text="117",
        user_id="user_1",
        project_id=PROJECT,
        session_id="sess_1",
    )

    labels = [image.label for image in context.images]
    assert len(labels) == 2, "the image budget must still be respected"
    assert labels == ["sketch-3.jpeg", "sketch-2.jpeg"], labels


def test_editing_an_existing_model_does_not_re_send_the_plans(parts) -> None:
    """Images are the most expensive thing in a prompt, so an edit does not carry them.

    The sketch is shown once, when it arrives. After that the conversation carries what was
    learned from it, and "make this taller" is about the scene.
    """
    builder, ingest, _ = parts
    _sketch(ingest)
    build(builder, user_text="build this")  # shown here, and only here

    context = builder.build(
        user_text="make this 20 cm taller",
        user_id="user_1",
        project_id=PROJECT,
        session_id="sess_1",
        scene=_scene_with_one_wall(),
    )

    assert context.images == ()
    # And the agent is still told the reference exists.
    assert any("room-sketch" in summary for summary in context.reference_summaries)


def test_asking_about_a_drawing_still_works_once_a_model_exists(parts) -> None:
    builder, ingest, _ = parts
    _sketch(ingest)

    context = builder.build(
        user_text="does this match the floor plan I sent?",
        user_id="user_1",
        project_id=PROJECT,
        session_id="sess_1",
        scene=_scene_with_one_wall(),
    )

    assert len(context.images) == 1


def test_an_explicit_attachment_always_wins(parts) -> None:
    """Whatever the heuristics think, what the user attached is what gets sent."""
    builder, ingest, _ = parts
    first = _sketch(ingest, "old.jpeg")
    _sketch(ingest, "new.jpeg")

    context = builder.build(
        user_text="make this 20 cm taller",
        user_id="user_1",
        project_id=PROJECT,
        session_id="sess_1",
        attached_reference_ids=(first.reference_id,),
        scene=_scene_with_one_wall(),
    )

    assert [image.label for image in context.images] == ["old.jpeg"]


def _scene_with_one_wall():
    from studio_types import (
        EulerRadians,
        Scale3,
        SceneObject,
        SceneSnapshot,
        SceneUnits,
        Vec3,
    )

    return SceneSnapshot(
        project_id=PROJECT,
        scene_version="sha256:" + "a" * 64,
        captured_at="2026-01-01T00:00:00Z",
        units=SceneUnits(unit_system="METRIC", length_unit="m", scale_length=1.0),
        objects=(
            SceneObject(
                studio_object_id="obj_wall",
                name="Wall_North",
                object_type="MESH",
                world_position_meters=Vec3(0.0, 0.0, 1.35),
                dimensions_meters=Vec3(4.0, 0.12, 2.7),
                rotation_euler_radians=EulerRadians(0.0, 0.0, 0.0),
                scale=Scale3(1.0, 1.0, 1.0),
                visible=True,
                material=None,
            ),
        ),
    )
