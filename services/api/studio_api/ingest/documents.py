"""Turn an uploaded file into something the agent can actually use.

Two derivations happen here:

* **PDF → text and page images.** Native text and vector text are extracted FIRST;
  OCR is deliberately not the default path, because architectural PDFs usually carry
  real text and dimension labels, and OCR would be slower, lossier and often wrong on
  exactly the numbers that matter. If a page yields no text, that is recorded as
  "no text" rather than guessed at.
* **Image → dimensions.** So the inspector and the agent know the pixel size, and so a
  corrupt image is detected on upload rather than when the model is asked to look at it.

Page images matter: they are what gets attached to a multimodal request. A PDF the model
cannot see is a PDF the model will make things up about.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Final, Optional

_log = logging.getLogger(__name__)

#: Rendered pages are sized so a plan's dimension labels stay legible without the
#: image becoming enormous. Measured against real A3 plans.
TARGET_LONG_EDGE_PIXELS: Final = 2200
MIN_RENDER_SCALE: Final = 1.0
MAX_RENDER_SCALE: Final = 4.0

#: A bound on cost: a 300-page document would otherwise render 300 images and blow the
#: prompt budget. The first pages of an architectural set are the useful ones.
MAX_RENDERED_PAGES: Final = 20

#: Extracted text is trimmed per page so one dense page cannot dominate a prompt.
MAX_TEXT_CHARACTERS_PER_PAGE: Final = 20_000
MAX_TEXT_CHARACTERS_TOTAL: Final = 120_000


class DocumentError(ValueError):
    """Raised when a document cannot be read."""


@dataclass
class RenderedPage:
    """One page of a PDF, rendered to PNG bytes."""

    page_number: int
    png_bytes: bytes
    width: int
    height: int
    text: Optional[str] = None


@dataclass
class PdfIngestion:
    """Everything derived from one PDF."""

    page_count: int
    pages: list[RenderedPage] = field(default_factory=list)
    #: Concatenated native text, or None when the document carries none.
    text: Optional[str] = None
    truncated: bool = False


def _render_scale(width_points: float, height_points: float) -> float:
    long_edge = max(width_points, height_points)
    if long_edge <= 0:
        return MIN_RENDER_SCALE
    scale = TARGET_LONG_EDGE_PIXELS / long_edge
    return max(MIN_RENDER_SCALE, min(MAX_RENDER_SCALE, scale))


def ingest_pdf(data: bytes, *, max_pages: int = MAX_RENDERED_PAGES) -> PdfIngestion:
    """Extract native text and render page images from a PDF."""
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except Exception as error:  # pypdfium2 raises a variety of types
        raise DocumentError(f"This PDF could not be opened: {error}") from error

    try:
        page_count = len(document)
    except Exception as error:  # pragma: no cover - defensive
        raise DocumentError(f"This PDF could not be read: {error}") from error

    ingestion = PdfIngestion(page_count=page_count)
    collected: list[str] = []
    total_characters = 0

    render_limit = min(page_count, max(0, max_pages))
    ingestion.truncated = render_limit < page_count

    for index in range(render_limit):
        try:
            page = document[index]
        except Exception as error:  # pragma: no cover - defensive
            raise DocumentError(f"Page {index + 1} could not be read: {error}") from error

        page_text: Optional[str] = None
        try:
            text_page = page.get_textpage()
            raw_text = text_page.get_text_range() or ""
            text_page.close()
            stripped = raw_text.strip()
            if stripped:
                page_text = stripped[:MAX_TEXT_CHARACTERS_PER_PAGE]
        except Exception as error:  # text extraction is best effort
            _log.debug("text extraction failed on page %d: %s", index + 1, error)

        if page_text and total_characters < MAX_TEXT_CHARACTERS_TOTAL:
            collected.append(f"[page {index + 1}]\n{page_text}")
            total_characters += len(page_text)

        try:
            width_points, height_points = page.get_size()
            bitmap = page.render(scale=_render_scale(width_points, height_points))
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            png_bytes = buffer.getvalue()
            ingestion.pages.append(
                RenderedPage(
                    page_number=index + 1,
                    png_bytes=png_bytes,
                    width=image.width,
                    height=image.height,
                    text=page_text,
                )
            )
        except Exception as error:
            raise DocumentError(
                f"Page {index + 1} of this PDF could not be rendered: {error}"
            ) from error
        finally:
            try:
                page.close()
            except Exception:  # pragma: no cover
                pass

    try:
        document.close()
    except Exception:  # pragma: no cover
        pass

    if collected:
        ingestion.text = "\n\n".join(collected)
    return ingestion


@dataclass(frozen=True)
class ImageFacts:
    width: int
    height: int


def read_image_facts(data: bytes) -> ImageFacts:
    """Read an image's dimensions, and prove it decodes."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        # verify() consumes the file object, so reopen to read the size.
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise DocumentError(f"This image could not be read: {error}") from error
    return ImageFacts(width=int(width), height=int(height))


def decode_document_text(data: bytes) -> str:
    """Decode a text or Markdown upload, bounded."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:  # pragma: no cover - validation catches this
        raise DocumentError("This document is not valid UTF-8 text.") from error
    return text[:MAX_TEXT_CHARACTERS_TOTAL]
