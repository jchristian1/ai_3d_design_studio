"""Builders for REAL sample uploads used by the ingestion and context tests.

Deliberately real files rather than stubs: a PNG that Pillow can actually decode and a
PDF that pypdfium2 can actually parse and extract text from. Testing ingestion against
fake bytes would prove nothing about the pipeline that matters.
"""

from __future__ import annotations

import io
from typing import Final

#: Text embedded in generated PDFs, so a test can prove NATIVE text extraction worked
#: rather than OCR or guesswork.
PDF_SAMPLE_TEXT: Final = "CEILING HEIGHT 2400"


def png_bytes(
    width: int = 8, height: int = 6, colour: tuple[int, int, int] = (200, 40, 40)
) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), colour)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int = 10, height: int = 10) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), (10, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def webp_bytes(width: int = 12, height: int = 9) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), (30, 200, 90))
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP")
    return buffer.getvalue()


def pdf_bytes(pages: int = 2, text: str = PDF_SAMPLE_TEXT) -> bytes:
    """A minimal but genuinely valid multi-page PDF carrying real, extractable text.

    Written by hand rather than with a PDF library so the test fixture does not depend
    on a writer whose output could drift, and so the byte layout stays inspectable.
    """
    objects: list[bytes] = []

    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Count {pages} /Kids [{kids}] >>".encode("ascii"))
    for index in range(pages):
        content = f"BT /F1 24 Tf 72 700 Td ({text} page {index + 1}) Tj ET".encode("ascii")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {3 + pages * 2} 0 R >> >> "
                f"/Contents {4 + index * 2} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)
