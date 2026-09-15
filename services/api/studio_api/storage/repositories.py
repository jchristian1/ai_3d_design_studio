"""Data access for the design workspace.

Every method is project-scoped, with ``project_id`` as a leading argument, for the same
reason the job store is: an identifier is not a capability. Knowing a reference id must
not be enough to read it out of somebody else's project.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional, Sequence

from .database import StudioDatabase
from .models import (
    APPROVED,
    PDF_PAGE,
    PENDING,
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


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _unjson(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - written by us, always valid
        return default


class ProjectRepository:
    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    def upsert(self, record: ProjectRecord) -> ProjectRecord:
        self._db.execute(
            """
            INSERT INTO projects
                (project_id, display_name, created_at, updated_at, blend_ready,
                 latest_scene_version)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = excluded.updated_at,
                blend_ready = excluded.blend_ready,
                latest_scene_version = excluded.latest_scene_version
            """,
            (
                record.project_id,
                record.display_name,
                record.created_at,
                record.updated_at,
                1 if record.blend_ready else 0,
                record.latest_scene_version,
            ),
        )
        return record

    def get(self, project_id: str) -> Optional[ProjectRecord]:
        row = self._db.query_one(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        )
        return self._from_row(row) if row else None

    def list_all(self) -> tuple[ProjectRecord, ...]:
        rows = self._db.query_all("SELECT * FROM projects ORDER BY created_at")
        return tuple(self._from_row(row) for row in rows)

    def ensure(self, project_id: str, display_name: str) -> ProjectRecord:
        existing = self.get(project_id)
        if existing is not None:
            return existing
        record = ProjectRecord(project_id=project_id, display_name=display_name)
        return self.upsert(record)

    def record_scene_version(self, project_id: str, scene_version: str) -> None:
        self._db.execute(
            "UPDATE projects SET latest_scene_version = ?, updated_at = ? WHERE project_id = ?",
            (scene_version, utc_now(), project_id),
        )

    def mark_blend_ready(self, project_id: str, ready: bool = True) -> None:
        self._db.execute(
            "UPDATE projects SET blend_ready = ?, updated_at = ? WHERE project_id = ?",
            (1 if ready else 0, utc_now(), project_id),
        )

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> ProjectRecord:
        return ProjectRecord(
            project_id=row["project_id"],
            display_name=row["display_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            blend_ready=bool(row["blend_ready"]),
            latest_scene_version=row["latest_scene_version"],
        )


class ReferenceRepository:
    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    def add(self, record: ReferenceRecord) -> ReferenceRecord:
        self._db.execute(
            """
            INSERT INTO project_references
                (reference_id, project_id, kind, display_name, media_type, size_bytes,
                 sha256, stored_name, created_at, page_count, parent_reference_id,
                 page_number, width, height, extracted_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.reference_id,
                record.project_id,
                record.kind,
                record.display_name,
                record.media_type,
                record.size_bytes,
                record.sha256,
                record.stored_name,
                record.created_at,
                record.page_count,
                record.parent_reference_id,
                record.page_number,
                record.width,
                record.height,
                record.extracted_text,
            ),
        )
        return record

    def get(self, project_id: str, reference_id: str) -> Optional[ReferenceRecord]:
        row = self._db.query_one(
            "SELECT * FROM project_references WHERE project_id = ? AND reference_id = ?",
            (project_id, reference_id),
        )
        return self._from_row(row) if row else None

    def list_for_project(
        self, project_id: str, *, include_pages: bool = False
    ) -> tuple[ReferenceRecord, ...]:
        if include_pages:
            rows = self._db.query_all(
                "SELECT * FROM project_references WHERE project_id = ? "
                "ORDER BY created_at, page_number",
                (project_id,),
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM project_references WHERE project_id = ? AND kind != ? "
                "ORDER BY created_at",
                (project_id, PDF_PAGE),
            )
        return tuple(self._from_row(row) for row in rows)

    def pages_of(self, project_id: str, reference_id: str) -> tuple[ReferenceRecord, ...]:
        rows = self._db.query_all(
            "SELECT * FROM project_references WHERE project_id = ? "
            "AND parent_reference_id = ? ORDER BY page_number",
            (project_id, reference_id),
        )
        return tuple(self._from_row(row) for row in rows)

    def find_by_hash(self, project_id: str, sha256: str) -> Optional[ReferenceRecord]:
        """Used to avoid re-ingesting identical bytes."""
        row = self._db.query_one(
            "SELECT * FROM project_references WHERE project_id = ? AND sha256 = ? "
            "AND kind != ? ORDER BY created_at LIMIT 1",
            (project_id, sha256, PDF_PAGE),
        )
        return self._from_row(row) if row else None

    def resolve_many(
        self, project_id: str, reference_ids: Iterable[str]
    ) -> tuple[ReferenceRecord, ...]:
        """Resolve ids to records, silently dropping ids from another project."""
        found: list[ReferenceRecord] = []
        for reference_id in reference_ids:
            record = self.get(project_id, reference_id)
            if record is not None:
                found.append(record)
        return tuple(found)

    def delete(self, project_id: str, reference_id: str) -> tuple[str, ...]:
        """Delete a reference and any pages derived from it.

        Returns the ``stored_name`` values whose bytes the caller should now remove.
        """
        pages = self.pages_of(project_id, reference_id)
        record = self.get(project_id, reference_id)
        if record is None:
            return ()
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM project_references WHERE project_id = ? "
                "AND (reference_id = ? OR parent_reference_id = ?)",
                (project_id, reference_id, reference_id),
            )
        return tuple([record.stored_name, *(page.stored_name for page in pages)])

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> ReferenceRecord:
        return ReferenceRecord(
            reference_id=row["reference_id"],
            project_id=row["project_id"],
            kind=row["kind"],
            display_name=row["display_name"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            stored_name=row["stored_name"],
            created_at=row["created_at"],
            page_count=row["page_count"],
            parent_reference_id=row["parent_reference_id"],
            page_number=row["page_number"],
            width=row["width"],
            height=row["height"],
            extracted_text=row["extracted_text"],
        )


class DesignFactRepository:
    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    def set(self, project_id: str, key: str, value: str, *, source: str = "user") -> DesignFact:
        fact = DesignFact(project_id=project_id, key=key, value=value, source=source)
        self._db.execute(
            """
            INSERT INTO design_facts (project_id, key, value, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, key) DO UPDATE SET
                value = excluded.value,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (project_id, key, value, source, fact.updated_at),
        )
        return fact

    def set_many(
        self, project_id: str, facts: Mapping[str, str], *, source: str = "user"
    ) -> tuple[DesignFact, ...]:
        return tuple(self.set(project_id, key, value, source=source) for key, value in facts.items())

    def get(self, project_id: str, key: str) -> Optional[DesignFact]:
        row = self._db.query_one(
            "SELECT * FROM design_facts WHERE project_id = ? AND key = ?", (project_id, key)
        )
        return self._from_row(row) if row else None

    def all_for_project(self, project_id: str) -> tuple[DesignFact, ...]:
        rows = self._db.query_all(
            "SELECT * FROM design_facts WHERE project_id = ? ORDER BY key", (project_id,)
        )
        return tuple(self._from_row(row) for row in rows)

    def as_mapping(self, project_id: str) -> dict[str, str]:
        return {fact.key: fact.value for fact in self.all_for_project(project_id)}

    def delete(self, project_id: str, key: str) -> None:
        self._db.execute(
            "DELETE FROM design_facts WHERE project_id = ? AND key = ?", (project_id, key)
        )

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> DesignFact:
        return DesignFact(
            project_id=row["project_id"],
            key=row["key"],
            value=row["value"],
            source=row["source"],
            updated_at=row["updated_at"],
        )


class ConversationRepository:
    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    def append(self, record: ConversationTurnRecord) -> ConversationTurnRecord:
        cursor = self._db.execute(
            """
            INSERT INTO conversation_turns
                (project_id, session_id, role, text, request_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.project_id,
                record.session_id,
                record.role,
                record.text,
                record.request_id,
                record.created_at,
            ),
        )
        record.turn_id = int(cursor.lastrowid or 0)
        return record

    def recent(self, project_id: str, limit: int = 12) -> tuple[ConversationTurnRecord, ...]:
        """The most recent turns, oldest first, bounded so prompts stay small."""
        rows = self._db.query_all(
            "SELECT * FROM conversation_turns WHERE project_id = ? "
            "ORDER BY turn_id DESC LIMIT ?",
            (project_id, max(0, limit)),
        )
        records = [self._from_row(row) for row in rows]
        records.reverse()
        return tuple(records)

    def count(self, project_id: str) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS total FROM conversation_turns WHERE project_id = ?",
            (project_id,),
        )
        return int(row["total"]) if row else 0

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> ConversationTurnRecord:
        return ConversationTurnRecord(
            project_id=row["project_id"],
            session_id=row["session_id"],
            role=row["role"],
            text=row["text"],
            created_at=row["created_at"],
            request_id=row["request_id"],
            turn_id=row["turn_id"],
        )


class ClarificationRepository:
    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    def add(self, record: ClarificationRecord) -> ClarificationRecord:
        self._db.execute(
            """
            INSERT INTO clarifications
                (clarification_id, project_id, session_id, question, missing_information,
                 request_id, created_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.clarification_id,
                record.project_id,
                record.session_id,
                record.question,
                _json(list(record.missing_information)),
                record.request_id,
                record.created_at,
                record.resolved_at,
            ),
        )
        return record

    def open_for_project(self, project_id: str) -> Optional[ClarificationRecord]:
        row = self._db.query_one(
            "SELECT * FROM clarifications WHERE project_id = ? AND resolved_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        return self._from_row(row) if row else None

    def get(self, project_id: str, clarification_id: str) -> Optional[ClarificationRecord]:
        row = self._db.query_one(
            "SELECT * FROM clarifications WHERE project_id = ? AND clarification_id = ?",
            (project_id, clarification_id),
        )
        return self._from_row(row) if row else None

    def resolve(self, project_id: str, clarification_id: str) -> None:
        self._db.execute(
            "UPDATE clarifications SET resolved_at = ? "
            "WHERE project_id = ? AND clarification_id = ? AND resolved_at IS NULL",
            (utc_now(), project_id, clarification_id),
        )

    def resolve_all(self, project_id: str) -> None:
        self._db.execute(
            "UPDATE clarifications SET resolved_at = ? "
            "WHERE project_id = ? AND resolved_at IS NULL",
            (utc_now(), project_id),
        )

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> ClarificationRecord:
        return ClarificationRecord(
            clarification_id=row["clarification_id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            question=row["question"],
            missing_information=tuple(_unjson(row["missing_information"], [])),
            request_id=row["request_id"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )


class ApprovalRepository:
    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    def add(self, record: ApprovalRecord) -> ApprovalRecord:
        self._db.execute(
            """
            INSERT INTO approvals
                (approval_id, project_id, session_id, code, summary, reasons, operations,
                 request_id, decision, created_at, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.approval_id,
                record.project_id,
                record.session_id,
                record.code,
                record.summary,
                _json(list(record.reasons)),
                _json([dict(operation) for operation in record.operations]),
                record.request_id,
                record.decision,
                record.created_at,
                record.decided_at,
            ),
        )
        return record

    def get(self, project_id: str, approval_id: str) -> Optional[ApprovalRecord]:
        row = self._db.query_one(
            "SELECT * FROM approvals WHERE project_id = ? AND approval_id = ?",
            (project_id, approval_id),
        )
        return self._from_row(row) if row else None

    def open_for_project(self, project_id: str) -> tuple[ApprovalRecord, ...]:
        rows = self._db.query_all(
            "SELECT * FROM approvals WHERE project_id = ? AND decision = ? "
            "ORDER BY created_at",
            (project_id, PENDING),
        )
        return tuple(self._from_row(row) for row in rows)

    def decide(self, project_id: str, approval_id: str, approved: bool) -> Optional[ApprovalRecord]:
        """Record a decision exactly once. A second decision is refused."""
        record = self.get(project_id, approval_id)
        if record is None or not record.is_pending:
            return None
        decision = APPROVED if approved else REJECTED
        decided_at = utc_now()
        self._db.execute(
            "UPDATE approvals SET decision = ?, decided_at = ? "
            "WHERE project_id = ? AND approval_id = ? AND decision = ?",
            (decision, decided_at, project_id, approval_id, PENDING),
        )
        record.decision = decision
        record.decided_at = decided_at
        return record

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=row["approval_id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            code=row["code"],
            summary=row["summary"],
            reasons=tuple(_unjson(row["reasons"], [])),
            operations=tuple(_unjson(row["operations"], [])),
            request_id=row["request_id"],
            decision=row["decision"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )


class AnalysisCacheRepository:
    """Caches structured analyses so unchanged references are not re-analysed."""

    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    @staticmethod
    def fingerprint(question: str, reference_hashes: Sequence[str]) -> str:
        digest = hashlib.sha256()
        digest.update(question.strip().encode("utf-8"))
        for value in sorted(reference_hashes):
            digest.update(b"\x00")
            digest.update(value.encode("utf-8"))
        return digest.hexdigest()

    def get(self, project_id: str, fingerprint: str) -> Optional[ReferenceAnalysisRecord]:
        row = self._db.query_one(
            "SELECT * FROM reference_analyses WHERE project_id = ? AND fingerprint = ?",
            (project_id, fingerprint),
        )
        if row is None:
            return None
        return ReferenceAnalysisRecord(
            fingerprint=row["fingerprint"],
            project_id=row["project_id"],
            analysis=_unjson(row["analysis"], {}),
            created_at=row["created_at"],
        )

    def put(
        self, project_id: str, fingerprint: str, analysis: Mapping[str, Any]
    ) -> ReferenceAnalysisRecord:
        record = ReferenceAnalysisRecord(
            fingerprint=fingerprint, project_id=project_id, analysis=dict(analysis)
        )
        self._db.execute(
            """
            INSERT INTO reference_analyses (fingerprint, project_id, analysis, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                analysis = excluded.analysis,
                created_at = excluded.created_at
            """,
            (fingerprint, project_id, _json(dict(analysis)), record.created_at),
        )
        return record


class ArtifactIndexRepository:
    """Records which artifacts exist, so "the latest GLB" is one query.

    The bytes stay in the existing artifact store; this is an index, not a second
    storage mechanism.
    """

    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    def record(
        self,
        project_id: str,
        artifact_id: str,
        artifact_type: str,
        media_type: str,
        *,
        job_id: Optional[str] = None,
        size_bytes: int = 0,
        checksum: str = "",
        scene_version: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO project_artifacts
                (artifact_id, project_id, artifact_type, media_type, created_at, job_id,
                 size_bytes, checksum, scene_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, artifact_id) DO UPDATE SET
                created_at = excluded.created_at,
                size_bytes = excluded.size_bytes,
                checksum = excluded.checksum,
                scene_version = excluded.scene_version
            """,
            (
                artifact_id,
                project_id,
                artifact_type,
                media_type,
                created_at or utc_now(),
                job_id,
                size_bytes,
                checksum,
                scene_version,
            ),
        )

    def latest(self, project_id: str, artifact_type: str) -> Optional[dict[str, Any]]:
        row = self._db.query_one(
            "SELECT * FROM project_artifacts WHERE project_id = ? AND artifact_type = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id, artifact_type),
        )
        return dict(row) if row else None

    def list_for_project(self, project_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._db.query_all(
            "SELECT * FROM project_artifacts WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        )
        return tuple(dict(row) for row in rows)


class StudioRepositories:
    """One handle carrying every repository, so wiring stays a single argument."""

    def __init__(self, database: StudioDatabase) -> None:
        self.database = database
        self.projects = ProjectRepository(database)
        self.references = ReferenceRepository(database)
        self.facts = DesignFactRepository(database)
        self.conversation = ConversationRepository(database)
        self.clarifications = ClarificationRepository(database)
        self.approvals = ApprovalRepository(database)
        self.analyses = AnalysisCacheRepository(database)
        self.artifacts = ArtifactIndexRepository(database)

    def close(self) -> None:
        self.database.close()
