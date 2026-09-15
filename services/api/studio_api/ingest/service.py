"""``ReferenceIngestService`` — upload once, derive everything.

One entry point (:meth:`ingest`) takes raw bytes and a filename and produces durable,
project-scoped references the agent can be given. It is the only place that decides
where bytes live, so no caller can choose a storage location.

Deduplication is by content hash. Re-uploading a file that is already in the project
returns the existing reference and its derived pages instead of re-rendering a
40-page PDF, which is the difference between a responsive workspace and a slow one.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Optional

from ..storage.files import ReferenceFileStore
from ..storage.models import (
    DOCUMENT,
    IMAGE,
    PDF,
    PDF_PAGE,
    ReferenceRecord,
)
from ..storage.repositories import StudioRepositories
from .documents import (
    DocumentError,
    decode_document_text,
    ingest_pdf,
    read_image_facts,
)
from .validation import UploadRejected, validate_upload

_log = logging.getLogger(__name__)


def new_reference_id() -> str:
    """A generated identifier. Never derived from user input."""
    return f"ref_{secrets.token_hex(6)}"


@dataclass
class IngestResult:
    """What one upload produced."""

    reference: ReferenceRecord
    pages: tuple[ReferenceRecord, ...] = ()
    #: True when the bytes were already in the project and nothing was re-derived.
    deduplicated: bool = False
    #: Set when a long PDF was truncated to the rendered-page limit.
    notice: Optional[str] = None

    def snapshot(self) -> dict:
        return {
            **self.reference.snapshot(),
            "pages": [page.snapshot() for page in self.pages],
            "deduplicated": self.deduplicated,
            "notice": self.notice,
        }


@dataclass
class ReferenceIngestService:
    """Validates, stores and derives project references."""

    repositories: StudioRepositories
    files: ReferenceFileStore
    #: Injectable purely so a test can bound rendering without a huge fixture.
    max_pdf_pages: int = 20

    def ingest(
        self,
        project_id: str,
        filename: str,
        data: bytes,
        *,
        declared_media_type: Optional[str] = None,
    ) -> IngestResult:
        """Accept one upload. Raises :class:`UploadRejected` with a user-safe message."""
        accepted = validate_upload(filename, data, declared_media_type)

        from ..storage.files import checksum_of

        digest = checksum_of(accepted.data)
        existing = self.repositories.references.find_by_hash(project_id, digest)
        if existing is not None:
            return IngestResult(
                reference=existing,
                pages=self.repositories.references.pages_of(project_id, existing.reference_id),
                deduplicated=True,
            )

        reference_id = new_reference_id()
        stored_name = self.files.build_stored_name(reference_id, accepted.display_name)
        self.files.write(project_id, stored_name, accepted.data)

        record = ReferenceRecord(
            reference_id=reference_id,
            project_id=project_id,
            kind=accepted.kind,
            display_name=accepted.display_name,
            media_type=accepted.media_type,
            size_bytes=accepted.size_bytes,
            sha256=digest,
            stored_name=stored_name,
        )

        try:
            if accepted.kind == IMAGE:
                facts = read_image_facts(accepted.data)
                record.width = facts.width
                record.height = facts.height
                self.repositories.references.add(record)
                return IngestResult(reference=record)

            if accepted.kind == DOCUMENT:
                record.extracted_text = decode_document_text(accepted.data)
                self.repositories.references.add(record)
                return IngestResult(reference=record)

            return self._ingest_pdf(project_id, record, accepted.data)
        except DocumentError as error:
            # Nothing is left half-ingested: remove the bytes we just wrote.
            self.files.delete(project_id, stored_name)
            raise UploadRejected(str(error)) from error

    def _ingest_pdf(
        self, project_id: str, record: ReferenceRecord, data: bytes
    ) -> IngestResult:
        ingestion = ingest_pdf(data, max_pages=self.max_pdf_pages)
        record.page_count = ingestion.page_count
        record.extracted_text = ingestion.text
        self.repositories.references.add(record)

        pages: list[ReferenceRecord] = []
        for page in ingestion.pages:
            page_id = new_reference_id()
            page_stored_name = self.files.build_stored_name(page_id, "page.png")
            self.files.write(project_id, page_stored_name, page.png_bytes)
            page_record = ReferenceRecord(
                reference_id=page_id,
                project_id=project_id,
                kind=PDF_PAGE,
                display_name=record.display_name,
                media_type="image/png",
                size_bytes=len(page.png_bytes),
                sha256=_digest(page.png_bytes),
                stored_name=page_stored_name,
                parent_reference_id=record.reference_id,
                page_number=page.page_number,
                width=page.width,
                height=page.height,
                extracted_text=page.text,
            )
            self.repositories.references.add(page_record)
            pages.append(page_record)

        notice = None
        if ingestion.truncated:
            notice = (
                f"Only the first {len(pages)} of {ingestion.page_count} pages were "
                "prepared for viewing."
            )
        return IngestResult(reference=record, pages=tuple(pages), notice=notice)

    # -- reading back ------------------------------------------------------
    def bytes_for(self, project_id: str, reference_id: str) -> Optional[tuple[bytes, str]]:
        """Return ``(data, media_type)`` for a reference, or None."""
        record = self.repositories.references.get(project_id, reference_id)
        if record is None:
            return None
        data = self.files.read(project_id, record.stored_name)
        if data is None:
            return None
        return data, record.media_type

    def delete(self, project_id: str, reference_id: str) -> bool:
        """Remove a reference, its pages, and their bytes."""
        stored_names = self.repositories.references.delete(project_id, reference_id)
        if not stored_names:
            return False
        for stored_name in stored_names:
            self.files.delete(project_id, stored_name)
        return True

    def list_references(self, project_id: str) -> tuple[ReferenceRecord, ...]:
        return self.repositories.references.list_for_project(project_id)


def _digest(data: bytes) -> str:
    from ..storage.files import checksum_of

    return checksum_of(data)
