"""Uploads and ingestion: validation, PDF pages, and real multimodal inputs."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from studio_api.ingest import (
    SUPPORTED_DESCRIPTION,
    ReferenceIngestService,
    UploadRejected,
    ingest_pdf,
    read_image_facts,
    sniff_media_type,
    validate_upload,
)
from studio_api.ingest.documents import DocumentError
from studio_fixtures.sample_files import jpeg_bytes, pdf_bytes, png_bytes, webp_bytes
from studio_api.storage import (
    DOCUMENT,
    IMAGE,
    PDF,
    PDF_PAGE,
    ReferenceFileStore,
    StudioDatabase,
    StudioRepositories,
)

PROJECT = "proj_a"


@pytest.fixture()
def service(tmp_path: Path) -> ReferenceIngestService:
    database = StudioDatabase(tmp_path / "studio.sqlite3")
    repositories = StudioRepositories(database)
    repositories.projects.ensure(PROJECT, "Beach House")
    return ReferenceIngestService(
        repositories=repositories,
        files=ReferenceFileStore(tmp_path / "references"),
        max_pdf_pages=3,
    )


# --- sniffing ------------------------------------------------------------


def test_real_files_are_identified_from_their_bytes() -> None:
    assert sniff_media_type(png_bytes()) == "image/png"
    assert sniff_media_type(jpeg_bytes()) == "image/jpeg"
    assert sniff_media_type(webp_bytes()) == "image/webp"
    assert sniff_media_type(pdf_bytes(1)) == "application/pdf"
    assert sniff_media_type(b"just some text") is None


# --- validation ----------------------------------------------------------


def test_each_supported_type_is_accepted() -> None:
    assert validate_upload("plan.png", png_bytes()).kind == IMAGE
    assert validate_upload("room.jpg", jpeg_bytes()).kind == IMAGE
    assert validate_upload("shot.webp", webp_bytes()).kind == IMAGE
    assert validate_upload("plan.pdf", pdf_bytes(1)).kind == PDF
    assert validate_upload("notes.txt", b"ceiling 2.4 m").kind == DOCUMENT
    assert validate_upload("notes.md", b"# Brief").kind == DOCUMENT


def test_a_renamed_executable_is_refused() -> None:
    """The whole point of sniffing: the extension is a claim, not a fact."""
    with pytest.raises(UploadRejected) as error:
        validate_upload("harmless.png", b"MZ\x90\x00" + b"\x00" * 200)
    assert "not accepted" in str(error.value) or "does not look like" in str(error.value)


def test_a_pdf_pretending_to_be_a_png_is_refused() -> None:
    with pytest.raises(UploadRejected) as error:
        validate_upload("plan.png", pdf_bytes(1))
    assert "different kind of file" in str(error.value)


@pytest.mark.parametrize("filename", ["payload.exe", "archive.zip", "script.sh", "noextension"])
def test_unsupported_types_are_refused_with_a_helpful_message(filename: str) -> None:
    with pytest.raises(UploadRejected) as error:
        validate_upload(filename, b"whatever" * 10)
    assert SUPPORTED_DESCRIPTION in str(error.value)


def test_an_archive_is_never_extracted() -> None:
    """Zip support is deliberately absent rather than partially implemented."""
    payload = zlib.compress(b"contents")
    with pytest.raises(UploadRejected):
        validate_upload("references.zip", b"PK\x03\x04" + payload)


def test_an_empty_upload_is_refused() -> None:
    with pytest.raises(UploadRejected) as error:
        validate_upload("plan.png", b"")
    assert "empty" in str(error.value)


def test_an_oversized_upload_is_refused() -> None:
    with pytest.raises(UploadRejected) as error:
        validate_upload("notes.txt", b"x" * (3 * 1024 * 1024))
    assert "larger than" in str(error.value)


def test_binary_content_in_a_text_file_is_refused() -> None:
    with pytest.raises(UploadRejected) as error:
        validate_upload("notes.txt", b"text\x00with a nul")
    assert "readable as text" in str(error.value)


def test_a_mismatched_declared_content_type_is_refused() -> None:
    with pytest.raises(UploadRejected) as error:
        validate_upload("plan.png", png_bytes(), declared_media_type="application/pdf")
    assert "not accepted" in str(error.value)


def test_a_charset_suffix_on_the_declared_type_is_tolerated() -> None:
    accepted = validate_upload(
        "notes.md", b"# Brief", declared_media_type="text/markdown; charset=utf-8"
    )
    assert accepted.kind == DOCUMENT


def test_the_uploaded_filename_is_sanitised_for_display() -> None:
    accepted = validate_upload("../../etc/plan.png", png_bytes())
    assert accepted.display_name == "plan.png"


# --- image facts ---------------------------------------------------------


def test_image_dimensions_are_read() -> None:
    facts = read_image_facts(png_bytes(width=64, height=32))
    assert (facts.width, facts.height) == (64, 32)


def test_a_corrupt_image_is_rejected_at_upload_time() -> None:
    with pytest.raises(DocumentError):
        read_image_facts(b"\x89PNG\r\n\x1a\n" + b"garbage")


# --- PDF ingestion -------------------------------------------------------


def test_a_pdf_yields_native_text_and_page_images() -> None:
    ingestion = ingest_pdf(pdf_bytes(pages=2))
    assert ingestion.page_count == 2
    assert len(ingestion.pages) == 2
    assert ingestion.text is not None
    # Native text extraction, not OCR: the label comes back exactly.
    assert "CEILING HEIGHT 2400" in ingestion.text
    assert "[page 1]" in ingestion.text and "[page 2]" in ingestion.text

    first = ingestion.pages[0]
    assert first.page_number == 1
    assert first.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert first.width > 800, "a plan page should render large enough to read"
    assert first.height > first.width, "A4 portrait should stay portrait"


def test_page_rendering_is_bounded_and_the_truncation_is_reported() -> None:
    ingestion = ingest_pdf(pdf_bytes(pages=5), max_pages=2)
    assert ingestion.page_count == 5
    assert len(ingestion.pages) == 2
    assert ingestion.truncated is True


def test_an_unreadable_pdf_is_reported_rather_than_crashing() -> None:
    with pytest.raises(DocumentError):
        ingest_pdf(b"%PDF-1.4\nnot really a pdf")


# --- the service ---------------------------------------------------------


def test_an_image_becomes_a_reference_with_dimensions(service) -> None:
    result = service.ingest(PROJECT, "room.jpg", jpeg_bytes(width=40, height=30))
    assert result.reference.kind == IMAGE
    assert (result.reference.width, result.reference.height) == (40, 30)
    assert result.reference.is_image
    assert result.pages == ()


def test_a_document_becomes_a_reference_with_its_text(service) -> None:
    result = service.ingest(PROJECT, "brief.md", b"# Brief\nCeiling 2.4 m")
    assert result.reference.kind == DOCUMENT
    assert "Ceiling 2.4 m" in (result.reference.extracted_text or "")


def test_a_pdf_becomes_a_reference_plus_page_references(service) -> None:
    result = service.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=2))
    assert result.reference.kind == PDF
    assert result.reference.page_count == 2
    assert [page.page_number for page in result.pages] == [1, 2]
    assert all(page.kind == PDF_PAGE for page in result.pages)
    assert all(page.is_image for page in result.pages)
    assert all(page.parent_reference_id == result.reference.reference_id for page in result.pages)
    # Pages carry a real label a human and a model can both use.
    assert result.pages[1].label == "floor-plan.pdf (page 2)"


def test_page_bytes_are_retrievable_as_png(service) -> None:
    result = service.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=1))
    page = result.pages[0]
    fetched = service.bytes_for(PROJECT, page.reference_id)
    assert fetched is not None
    data, media_type = fetched
    assert media_type == "image/png"
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


def test_the_same_file_uploaded_twice_is_not_re_derived(service) -> None:
    data = pdf_bytes(pages=3)
    first = service.ingest(PROJECT, "floor-plan.pdf", data)
    second = service.ingest(PROJECT, "floor-plan-copy.pdf", data)

    assert second.deduplicated is True
    assert second.reference.reference_id == first.reference.reference_id
    assert len(second.pages) == len(first.pages)
    everything = service.repositories.references.list_for_project(PROJECT, include_pages=True)
    assert len(everything) == 1 + len(first.pages), "no duplicate references were created"


def test_a_long_pdf_reports_that_it_was_truncated(service) -> None:
    result = service.ingest(PROJECT, "big.pdf", pdf_bytes(pages=6))
    assert len(result.pages) == 3, "the service's max_pdf_pages bound applies"
    assert result.notice is not None
    assert "first 3 of 6" in result.notice


def test_a_rejected_upload_leaves_nothing_behind(service) -> None:
    with pytest.raises(UploadRejected):
        service.ingest(PROJECT, "broken.png", b"\x89PNG\r\n\x1a\n" + b"garbage")
    assert service.list_references(PROJECT) == ()
    directory = service.files.project_directory(PROJECT)
    leftovers = list(directory.glob("*")) if directory.exists() else []
    assert leftovers == [], f"a failed ingest left files behind: {leftovers}"


def test_deleting_a_pdf_removes_its_pages_and_their_bytes(service) -> None:
    result = service.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=2))
    page_id = result.pages[0].reference_id

    assert service.delete(PROJECT, result.reference.reference_id) is True
    assert service.list_references(PROJECT) == ()
    assert service.bytes_for(PROJECT, page_id) is None
    directory = service.files.project_directory(PROJECT)
    assert list(directory.glob("*")) == []


def test_deleting_something_that_does_not_exist_is_reported(service) -> None:
    assert service.delete(PROJECT, "ref_nothing") is False


def test_references_from_another_project_are_invisible(service) -> None:
    service.repositories.projects.ensure("proj_b", "Other")
    result = service.ingest(PROJECT, "room.jpg", jpeg_bytes())
    assert service.bytes_for("proj_b", result.reference.reference_id) is None
    assert service.delete("proj_b", result.reference.reference_id) is False


def test_a_reference_id_is_generated_and_never_taken_from_the_filename(service) -> None:
    result = service.ingest(PROJECT, "my lovely plan.png", png_bytes())
    assert result.reference.reference_id.startswith("ref_")
    assert "lovely" not in result.reference.reference_id
    assert result.reference.stored_name == f"{result.reference.reference_id}.png"


def test_a_reference_snapshot_exposes_no_path(service) -> None:
    result = service.ingest(PROJECT, "floor-plan.pdf", pdf_bytes(pages=1))
    snapshot = result.snapshot()
    import json

    serialized = json.dumps(snapshot)
    assert "stored_name" not in serialized
    assert str(service.files.root) not in serialized
    assert "/home" not in serialized
    # But it does carry what the browser needs.
    assert snapshot["page_count"] == 1
    assert snapshot["pages"][0]["page_number"] == 1
