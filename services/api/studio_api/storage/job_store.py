"""A durable ``JobRecordStore``.

Implements the existing protocol exactly, so it drops into
``build_dependencies(store=...)`` with no change to any route or service. The contract
that matters is clause 1: ``submit`` is atomic and keyed by
``(project_id, idempotency_key)``. Here that is enforced by a real UNIQUE index rather
than by an application-level check, so two concurrent submissions of the same mutation
identity cannot both insert.

The worker's journal remains authoritative for "was Blender mutated". This store makes
the control plane's *reporting* survive a restart, which is what the MVP needs: after
restarting the backend, a project still shows its history instead of appearing empty.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Optional

from ..job_records import JobRecord
from .database import StudioDatabase

_COLUMNS = (
    "project_id",
    "job_id",
    "session_id",
    "user_id",
    "request_id",
    "operation_index",
    "job_type",
    "idempotency_key",
    "job_status",
    "created_at",
    "updated_at",
    "job",
    "content_fingerprint",
    "worker_id",
    "execution_phase",
    "result",
    "error",
    "preview",
    "preview_error",
    "reconciled",
    "adopted_after_state_loss",
    "offer_count",
)


def _dump(value: Optional[Mapping[str, Any]]) -> Optional[str]:
    return None if value is None else json.dumps(value, sort_keys=True)


def _load(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - written by us
        return None
    return parsed if isinstance(parsed, dict) else None


class SqliteJobRecordStore:
    """A job-record store backed by the local database."""

    def __init__(self, database: StudioDatabase) -> None:
        self._db = database

    # -- protocol ----------------------------------------------------------
    def submit(self, record: JobRecord) -> tuple[JobRecord, bool]:
        """Insert, or return the existing record for the same mutation identity."""
        try:
            with self._db.transaction() as connection:
                connection.execute(
                    f"INSERT INTO job_records ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                    self._to_row(record),
                )
        except sqlite3.IntegrityError:
            # Either the same job_id or the same idempotency key already exists. The
            # idempotency key is the mutation identity, so that is what is returned.
            existing = self.find_by_idempotency_key(
                record.project_id, record.idempotency_key
            )
            if existing is None:
                existing = self.get(record.project_id, record.job_id)
            if existing is None:  # pragma: no cover - only reachable on real corruption
                raise
            return existing, True
        return record, False

    def get(self, project_id: str, job_id: str) -> Optional[JobRecord]:
        row = self._db.query_one(
            "SELECT * FROM job_records WHERE project_id = ? AND job_id = ?",
            (project_id, job_id),
        )
        return self._from_row(row) if row else None

    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> Optional[JobRecord]:
        row = self._db.query_one(
            "SELECT * FROM job_records WHERE project_id = ? AND idempotency_key = ?",
            (project_id, idempotency_key),
        )
        return self._from_row(row) if row else None

    def save(self, record: JobRecord) -> JobRecord:
        """Persist a record, inserting it if it does not exist yet.

        ``save`` is an UPSERT, not an update. That is the protocol's real contract, and
        it is load-bearing: the reconciler's adoption path (a terminal result arriving
        for a job this process never recorded, after an API restart) constructs a record
        and calls ``save`` on it directly. A plain UPDATE would match zero rows and lose
        the worker's report, which is exactly the outcome adoption exists to prevent.
        """
        updatable = [column for column in _COLUMNS if column not in ("project_id", "job_id")]
        assignments = ", ".join(f"{column} = excluded.{column}" for column in updatable)
        self._db.execute(
            f"INSERT INTO job_records ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
            f"ON CONFLICT(project_id, job_id) DO UPDATE SET {assignments}",
            self._to_row(record),
        )
        return record

    def all_records(self) -> tuple[JobRecord, ...]:
        rows = self._db.query_all("SELECT * FROM job_records ORDER BY created_at, rowid")
        return tuple(self._from_row(row) for row in rows)

    # -- extras the workspace uses ----------------------------------------
    def for_project(self, project_id: str, limit: int = 50) -> tuple[JobRecord, ...]:
        rows = self._db.query_all(
            "SELECT * FROM job_records WHERE project_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (project_id, max(0, limit)),
        )
        records = [self._from_row(row) for row in rows]
        records.reverse()
        return tuple(records)

    def for_request(self, project_id: str, request_id: str) -> tuple[JobRecord, ...]:
        """Every job created by one user request, in execution order."""
        rows = self._db.query_all(
            "SELECT * FROM job_records WHERE project_id = ? AND request_id = ? "
            "ORDER BY operation_index",
            (project_id, request_id),
        )
        return tuple(self._from_row(row) for row in rows)

    # -- mapping -----------------------------------------------------------
    @staticmethod
    def _to_row(record: JobRecord) -> tuple[Any, ...]:
        return (
            record.project_id,
            record.job_id,
            record.session_id,
            record.user_id,
            record.request_id,
            record.operation_index,
            record.job_type,
            record.idempotency_key,
            record.job_status,
            record.created_at,
            record.updated_at,
            json.dumps(record.job, sort_keys=True),
            record.content_fingerprint,
            record.worker_id,
            record.execution_phase,
            _dump(record.result),
            _dump(record.error),
            _dump(record.preview),
            _dump(record.preview_error),
            1 if record.reconciled else 0,
            1 if record.adopted_after_state_loss else 0,
            record.offer_count,
        )

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            request_id=row["request_id"],
            operation_index=row["operation_index"],
            job_type=row["job_type"],
            idempotency_key=row["idempotency_key"],
            job_status=row["job_status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            job=_load(row["job"]) or {},
            content_fingerprint=row["content_fingerprint"],
            worker_id=row["worker_id"],
            execution_phase=row["execution_phase"],
            result=_load(row["result"]),
            error=_load(row["error"]),
            preview=_load(row["preview"]),
            preview_error=_load(row["preview_error"]),
            reconciled=bool(row["reconciled"]),
            adopted_after_state_loss=bool(row["adopted_after_state_loss"]),
            offer_count=row["offer_count"],
        )
