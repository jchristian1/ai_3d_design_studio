"""Durable records for the design workspace.

These are the control plane's own persistent state: projects, the references a user
uploaded, what those references turned into, the facts the user confirmed, and the
questions and approvals still waiting on them.

What is deliberately NOT here:

* The detailed 3D scene. The ``.blend`` file remains authoritative, and the worker's
  journal remains authoritative for "was Blender actually mutated".
* Filesystem paths as a contract. A record stores a ``stored_name`` relative to a
  platform-derived directory; callers ask the store for bytes and never see a path.
* Chain-of-thought. Only structured results are persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

# --- reference kinds ------------------------------------------------------
IMAGE = "image"
PDF = "pdf"
DOCUMENT = "document"
#: A page image rendered from a PDF. Always has a parent.
PDF_PAGE = "pdf_page"

REFERENCE_KINDS = (IMAGE, PDF, DOCUMENT, PDF_PAGE)

# --- approval decisions ---------------------------------------------------
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

APPROVAL_DECISIONS = (PENDING, APPROVED, REJECTED)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProjectRecord:
    """A design project. Owns everything else in this module."""

    project_id: str
    display_name: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    #: Set once the project has a Blender file the worker knows how to reach.
    blend_ready: bool = False
    latest_scene_version: Optional[str] = None
    #: When the user last opened this project in the browser. Drives "reopen what I was
    #: working on", so it is set by opening — never by background work on the project.
    last_opened_at: Optional[str] = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "blend_ready": self.blend_ready,
            "last_opened_at": self.last_opened_at,
            "latest_scene_version": self.latest_scene_version,
        }


@dataclass
class ReferenceRecord:
    """One uploaded file, or one page derived from one."""

    reference_id: str
    project_id: str
    kind: str
    display_name: str
    media_type: str
    size_bytes: int
    sha256: str
    #: Filename inside the project's platform-derived reference directory.
    stored_name: str
    created_at: str = field(default_factory=utc_now)
    #: For a PDF, how many pages were rendered.
    page_count: Optional[int] = None
    #: For a page, which PDF it came from and which page it is.
    parent_reference_id: Optional[str] = None
    page_number: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    #: Extracted text, for documents and PDFs. Never a guess: absent means none found.
    extracted_text: Optional[str] = None

    @property
    def is_image(self) -> bool:
        return self.kind in (IMAGE, PDF_PAGE)

    @property
    def label(self) -> str:
        if self.page_number is not None:
            return f"{self.display_name} (page {self.page_number})"
        return self.display_name

    def snapshot(self) -> dict[str, Any]:
        """A browser-safe view. Carries no path, by construction."""
        return {
            "reference_id": self.reference_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "label": self.label,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "page_count": self.page_count,
            "parent_reference_id": self.parent_reference_id,
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "has_text": bool(self.extracted_text),
            "is_image": self.is_image,
        }


@dataclass
class DesignFact:
    """A confirmed fact about the project. Lightweight project memory."""

    project_id: str
    key: str
    value: str
    #: "user" when the user stated it, "agent" when the agent derived and recorded it.
    source: str = "user"
    updated_at: str = field(default_factory=utc_now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "updated_at": self.updated_at,
        }


@dataclass
class ConversationTurnRecord:
    """One line of conversation, kept so context survives a restart."""

    project_id: str
    session_id: str
    role: str  # "user" | "assistant"
    text: str
    created_at: str = field(default_factory=utc_now)
    request_id: Optional[str] = None
    turn_id: Optional[int] = None


@dataclass
class ClarificationRecord:
    """A question the agent asked, awaiting an answer."""

    clarification_id: str
    project_id: str
    session_id: str
    question: str
    missing_information: tuple[str, ...] = ()
    request_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    resolved_at: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None

    def snapshot(self) -> dict[str, Any]:
        return {
            "clarification_id": self.clarification_id,
            "question": self.question,
            "missing_information": list(self.missing_information),
            "created_at": self.created_at,
            "resolved": self.resolved,
        }


@dataclass
class ApprovalRecord:
    """Model-authored code the user must decide about before it can run.

    The code is stored verbatim because showing it to the user IS the feature. The
    decision is recorded so an approval cannot be replayed for different code: the
    token issued on approval is derived from a digest of exactly this text.
    """

    approval_id: str
    project_id: str
    session_id: str
    code: str
    summary: str
    reasons: tuple[str, ...]
    #: The full operation list to run once approved, as validated wire documents.
    operations: tuple[Mapping[str, Any], ...] = ()
    request_id: Optional[str] = None
    decision: str = PENDING
    created_at: str = field(default_factory=utc_now)
    decided_at: Optional[str] = None

    @property
    def is_pending(self) -> bool:
        return self.decision == PENDING

    def snapshot(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "code": self.code,
            "summary": self.summary,
            "reasons": list(self.reasons),
            "decision": self.decision,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "operation_count": len(self.operations),
        }


@dataclass
class ReferenceAnalysisRecord:
    """A cached structured analysis, keyed by the content it was derived from.

    Keyed by a fingerprint of the reference hashes plus the question asked, so
    re-analysing an unchanged set of references costs nothing. This is the main
    protection for Codex credits.
    """

    fingerprint: str
    project_id: str
    analysis: Mapping[str, Any]
    created_at: str = field(default_factory=utc_now)
