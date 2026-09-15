"""What the agent sees, and what it must never see."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from studio_agent.providers.codex_astra import build_prompt
from studio_api.context_builder import ContextBuilder
from studio_api.ingest import ReferenceIngestService
from studio_api.storage import ReferenceFileStore, StudioDatabase, StudioRepositories

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


def test_a_non_visual_message_attaches_nothing(parts) -> None:
    builder, ingest, _ = parts
    ingest.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=1))
    agent_input = build(builder, user_text="thanks, that is all for today")
    assert agent_input.images == ()
    # But the agent is still told the reference exists, so it can ask for it.
    assert any("floor-plan.pdf" in summary for summary in agent_input.reference_summaries)


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
