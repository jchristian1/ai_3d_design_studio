"""What a turn costs, and why.

Astra runs on a finite ChatGPT allowance, and a session that "eats tokens without doing
anything" is a product defect rather than an accounting detail. Images dominate: a page
rendered at 1700x2200 is twenty 512-pixel tiles, and six of those re-sent on every message
is most of a bill.

Three controls, each asserted here:

* an image is DOWNSCALED before it is sent, and the original is kept for the browser;
* only a few images go in one turn;
* an image this session has already been shown is not paid for again.

Each has an override: what the user explicitly attaches is always sent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio_api.context_builder import ContextBuilder
from studio_api.ingest import ReferenceIngestService
from studio_api.ingest.model_copies import MODEL_LONG_EDGE, ModelCopyMaker
from studio_api.storage import ReferenceFileStore, StudioDatabase, StudioRepositories
from studio_api.storage.models import ClarificationRecord

from studio_fixtures.sample_files import pdf_bytes, png_bytes

PROJECT = "proj_cost"
SESSION = "sess_1"


@pytest.fixture()
def parts(tmp_path: Path):
    database = StudioDatabase(tmp_path / "studio.sqlite3")
    repositories = StudioRepositories(database)
    repositories.projects.ensure(PROJECT, "Cost")
    files = ReferenceFileStore(tmp_path / "references")
    ingest = ReferenceIngestService(repositories=repositories, files=files, max_pdf_pages=4)
    builder = ContextBuilder(repositories=repositories, files=files)
    return builder, ingest, repositories


def build(builder, text: str = "here are the heights", **kwargs):
    base = {
        "user_text": text,
        "user_id": "user_1",
        "project_id": PROJECT,
        "session_id": SESSION,
    }
    base.update(kwargs)
    return builder.build(**base)


def _tiles(path: Path) -> int:
    """How many 512-pixel tiles an image occupies — the usual unit of vision cost."""
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    return ((width + 511) // 512) * ((height + 511) // 512)


# --- downscaling ----------------------------------------------------------


def test_a_big_photo_is_downscaled_before_it_is_sent(parts) -> None:
    builder, ingest, _ = parts
    reference = ingest.ingest(PROJECT, "plan.png", png_bytes(width=1700, height=2200)).reference

    context = build(builder, attached_reference_ids=[reference.reference_id])

    assert len(context.images) == 1
    sent = Path(context.images[0].path)
    assert max(_size(sent)) <= MODEL_LONG_EDGE
    # The saving that matters: twenty tiles becomes six.
    assert _tiles(sent) <= 8, _size(sent)


def test_the_original_is_untouched_so_the_browser_still_shows_it_sharp(parts) -> None:
    builder, ingest, repositories = parts
    reference = ingest.ingest(PROJECT, "plan.png", png_bytes(width=1700, height=2200)).reference

    context = build(builder, attached_reference_ids=[reference.reference_id])

    stored = ingest.bytes_for(PROJECT, reference.reference_id)
    assert stored is not None
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(stored[0])) as full:
        assert full.size == (1700, 2200), "the stored upload must not be modified"

    # And what was sent is a different file, not the original.
    original_path = builder.files.path_for(PROJECT, reference.stored_name)
    assert original_path is not None
    assert Path(context.images[0].path) != original_path


def test_an_already_small_jpeg_is_sent_as_is(tmp_path: Path) -> None:
    """Re-encoding a small photo would only lose detail for no saving."""
    from studio_fixtures.sample_files import jpeg_bytes

    original = tmp_path / "small.jpeg"
    original.write_bytes(jpeg_bytes(width=600, height=800))

    assert ModelCopyMaker().copy_of(original) == original


def test_a_copy_is_made_once_and_reused(tmp_path: Path) -> None:
    original = tmp_path / "plan.png"
    original.write_bytes(png_bytes(width=1700, height=2200))
    maker = ModelCopyMaker()

    first = maker.copy_of(original)
    stamp = first.stat().st_mtime_ns
    second = maker.copy_of(original)

    assert first == second
    assert second.stat().st_mtime_ns == stamp, "the copy was rebuilt for no reason"


def test_an_unreadable_file_falls_back_to_the_original(tmp_path: Path) -> None:
    """A turn that cannot be made cheaper must still happen."""
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"this is not an image")

    assert ModelCopyMaker().copy_of(broken) == broken


# --- how many -------------------------------------------------------------


def test_a_long_pdf_does_not_send_every_page(parts) -> None:
    builder, ingest, _ = parts
    result = ingest.ingest(PROJECT, "set.pdf", pdf_bytes(pages=4))
    assert len(result.pages) == 4, "the pages exist"

    context = build(builder, attached_reference_ids=[result.reference.reference_id])

    assert len(context.images) <= builder.max_images
    assert builder.max_images == 3


# --- not twice ------------------------------------------------------------


def test_a_sketch_is_not_re_sent_once_the_model_has_seen_it(parts) -> None:
    """The expensive mistake: six pages re-sent on every message of a conversation."""
    builder, ingest, repositories = parts
    ingest.ingest(PROJECT, "sketch.png", png_bytes(width=1200, height=900))
    repositories.clarifications.add(
        ClarificationRecord(
            clarification_id="clr_1",
            project_id=PROJECT,
            session_id=SESSION,
            question="What is the ceiling height?",
            missing_information=("ceiling_height",),
        )
    )

    first = build(builder, "model this room")
    assert len(first.images) == 1, "the model must see it once"

    second = build(builder, "117")
    assert second.images == (), "the same pixels must not be paid for twice"
    # It is still described, so the agent knows it exists and what it was.
    assert any("sketch.png" in summary for summary in second.reference_summaries)


def test_attaching_it_again_sends_it_again(parts) -> None:
    """The user is in charge: an explicit attachment overrides every saving."""
    builder, ingest, _ = parts
    reference = ingest.ingest(PROJECT, "sketch.png", png_bytes(width=1200, height=900)).reference

    build(builder, "model this room", attached_reference_ids=[reference.reference_id])
    again = build(builder, "look at it again", attached_reference_ids=[reference.reference_id])

    assert len(again.images) == 1


def test_a_new_session_starts_from_scratch(parts) -> None:
    """A new conversation has a model that has never seen the drawing."""
    builder, ingest, _ = parts
    ingest.ingest(PROJECT, "sketch.png", png_bytes(width=1200, height=900))

    first = build(builder, "model this room")
    assert len(first.images) == 1

    later = build(builder, "model this room", session_id="sess_2")
    assert len(later.images) == 1


def test_a_second_upload_is_still_shown(parts) -> None:
    """Suppressing what was seen must not suppress what is new."""
    builder, ingest, _ = parts
    ingest.ingest(PROJECT, "first.png", png_bytes(width=1200, height=900))
    build(builder, "model this room")

    ingest.ingest(PROJECT, "second.png", png_bytes(width=1201, height=900))
    context = build(builder, "and this one")

    assert [image.label for image in context.images] == ["second.png"]


def _size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        return image.size
