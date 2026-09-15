"""Reference ingestion: uploads, PDFs and images the agent can actually use."""

from .documents import (
    DocumentError,
    ImageFacts,
    PdfIngestion,
    RenderedPage,
    decode_document_text,
    ingest_pdf,
    read_image_facts,
)
from .service import IngestResult, ReferenceIngestService, new_reference_id
from .validation import (
    MAX_DOCUMENT_BYTES,
    MAX_IMAGE_BYTES,
    MAX_PDF_BYTES,
    SUPPORTED_DESCRIPTION,
    SUPPORTED_MEDIA_TYPES,
    AcceptedUpload,
    UploadRejected,
    sniff_media_type,
    validate_upload,
)

__all__ = [
    "AcceptedUpload",
    "DocumentError",
    "ImageFacts",
    "IngestResult",
    "MAX_DOCUMENT_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_PDF_BYTES",
    "PdfIngestion",
    "ReferenceIngestService",
    "RenderedPage",
    "SUPPORTED_DESCRIPTION",
    "SUPPORTED_MEDIA_TYPES",
    "UploadRejected",
    "decode_document_text",
    "ingest_pdf",
    "new_reference_id",
    "read_image_facts",
    "sniff_media_type",
    "validate_upload",
]
