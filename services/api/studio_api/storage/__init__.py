"""Durable local state for the design workspace.

SQLite plus a byte store on disk. The ``.blend`` file remains the authoritative 3D
scene, and the worker's journal remains authoritative for whether Blender was mutated;
this package makes the *workspace* survive a restart — projects, references, confirmed
facts, conversation, pending questions and approvals, and job reporting.
"""

from .database import (
    DEFAULT_DATABASE_PATH,
    LATEST_VERSION,
    MIGRATIONS,
    StudioDatabase,
    open_database,
)
from .files import (
    EXTENSION_MEDIA_TYPES,
    ReferenceFileStore,
    ReferenceStorageError,
    checksum_of,
    media_type_for,
    sanitise_display_name,
)
from .job_store import SqliteJobRecordStore
from .models import (
    APPROVAL_DECISIONS,
    APPROVED,
    DOCUMENT,
    IMAGE,
    PDF,
    PDF_PAGE,
    PENDING,
    REFERENCE_KINDS,
    REJECTED,
    ApprovalRecord,
    ClarificationRecord,
    ConversationTurnRecord,
    DesignFact,
    ProjectRecord,
    ReferenceAnalysisRecord,
    ReferenceRecord,
    utc_now,
)
from .repositories import StudioRepositories

__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVED",
    "ApprovalRecord",
    "ClarificationRecord",
    "ConversationTurnRecord",
    "DEFAULT_DATABASE_PATH",
    "DOCUMENT",
    "DesignFact",
    "EXTENSION_MEDIA_TYPES",
    "IMAGE",
    "LATEST_VERSION",
    "MIGRATIONS",
    "PDF",
    "PDF_PAGE",
    "PENDING",
    "ProjectRecord",
    "REFERENCE_KINDS",
    "REJECTED",
    "ReferenceAnalysisRecord",
    "ReferenceFileStore",
    "ReferenceRecord",
    "ReferenceStorageError",
    "SqliteJobRecordStore",
    "StudioDatabase",
    "StudioRepositories",
    "checksum_of",
    "media_type_for",
    "open_database",
    "sanitise_display_name",
    "utc_now",
]
