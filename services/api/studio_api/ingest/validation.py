"""Upload validation.

An upload is untrusted in three separate ways, and each is checked:

1. **What it claims to be.** The extension and the browser-declared content type are
   hints, not facts, so they are checked against an allow-list and then against the
   bytes.
2. **What it actually is.** The leading bytes are sniffed. A ``.png`` whose content is
   a PDF, or an executable renamed to ``.jpg``, is refused here rather than discovered
   by whatever opens it next.
3. **How big it is.** Per-kind limits, applied to the real byte length rather than a
   declared ``Content-Length``.

Archives are deliberately not supported at all: extracting one safely is a project of
its own, and the MVP does not need it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Optional

from ..storage.files import extension_of, media_type_for, sanitise_display_name
from ..storage.models import DOCUMENT, IMAGE, PDF

#: Per-kind byte ceilings. Architectural PDFs are legitimately large; text is not.
MAX_IMAGE_BYTES: Final = 25 * 1024 * 1024
MAX_PDF_BYTES: Final = 50 * 1024 * 1024
MAX_DOCUMENT_BYTES: Final = 2 * 1024 * 1024

#: Refuse an empty upload rather than storing a zero-byte reference.
MIN_BYTES: Final = 1

IMAGE_MEDIA_TYPES: Final = ("image/png", "image/jpeg", "image/webp")
DOCUMENT_MEDIA_TYPES: Final = ("text/plain", "text/markdown")
PDF_MEDIA_TYPE: Final = "application/pdf"

SUPPORTED_MEDIA_TYPES: Final = (*IMAGE_MEDIA_TYPES, PDF_MEDIA_TYPE, *DOCUMENT_MEDIA_TYPES)

#: Human-readable list for error messages and the UI.
SUPPORTED_DESCRIPTION: Final = "PNG, JPEG, WebP, PDF, plain text and Markdown"


class UploadRejected(ValueError):
    """Raised when an upload cannot be accepted. The message is shown to the user."""


@dataclass(frozen=True)
class AcceptedUpload:
    """A validated upload, ready to be stored."""

    display_name: str
    media_type: str
    kind: str
    data: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def sniff_media_type(data: bytes) -> Optional[str]:
    """Identify a file from its leading bytes, independently of its name."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return PDF_MEDIA_TYPE
    return None


def _looks_like_text(data: bytes) -> bool:
    """Text is anything that decodes as UTF-8 and carries no NUL byte."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def kind_for_media_type(media_type: str) -> str:
    if media_type in IMAGE_MEDIA_TYPES:
        return IMAGE
    if media_type == PDF_MEDIA_TYPE:
        return PDF
    return DOCUMENT


def limit_for_kind(kind: str) -> int:
    if kind == IMAGE:
        return MAX_IMAGE_BYTES
    if kind == PDF:
        return MAX_PDF_BYTES
    return MAX_DOCUMENT_BYTES


def _describe_limit(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB"


def validate_upload(
    filename: str, data: bytes, declared_media_type: Optional[str] = None
) -> AcceptedUpload:
    """Validate one upload, or explain why it cannot be accepted."""
    display_name = sanitise_display_name(filename)

    if len(data) < MIN_BYTES:
        raise UploadRejected(f"{display_name} is empty.")

    extension = extension_of(display_name)
    if not extension:
        raise UploadRejected(
            f"{display_name} is not a supported file type. "
            f"Supported types are {SUPPORTED_DESCRIPTION}."
        )

    expected = media_type_for(display_name)
    if expected is None:  # pragma: no cover - extension_of already constrains this
        raise UploadRejected(f"{display_name} is not a supported file type.")

    sniffed = sniff_media_type(data)
    if sniffed is not None:
        # A binary format identified itself. It must agree with the extension.
        if sniffed != expected:
            raise UploadRejected(
                f"{display_name} looks like a different kind of file than its name "
                "suggests, so it was not accepted."
            )
        media_type = sniffed
    else:
        # No recognised signature: only the text formats may take this route.
        if expected not in DOCUMENT_MEDIA_TYPES:
            raise UploadRejected(
                f"{display_name} does not look like a valid {expected} file."
            )
        if not _looks_like_text(data):
            raise UploadRejected(f"{display_name} is not readable as text.")
        media_type = expected

    # The browser's declared type is only ever a cross-check, never the decision.
    if declared_media_type:
        normalised = declared_media_type.split(";")[0].strip().lower()
        if normalised and normalised != media_type:
            allowed_alias = normalised in ("text/markdown", "text/plain") and (
                media_type in DOCUMENT_MEDIA_TYPES
            )
            if not allowed_alias:
                raise UploadRejected(
                    f"{display_name} was sent as {normalised} but its contents are "
                    f"{media_type}, so it was not accepted."
                )

    kind = kind_for_media_type(media_type)
    limit = limit_for_kind(kind)
    if len(data) > limit:
        raise UploadRejected(
            f"{display_name} is larger than the {_describe_limit(limit)} limit."
        )

    return AcceptedUpload(
        display_name=display_name, media_type=media_type, kind=kind, data=data
    )
