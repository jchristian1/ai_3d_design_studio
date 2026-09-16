"""The local SQLite database behind the design workspace.

SQLite rather than PostgreSQL because this is a local, single-user MVP: no server to
run, no migration tooling to install, and the file is trivially inspectable and
backed up. The schema is applied through numbered migrations tracked in
``PRAGMA user_version``, so upgrading an existing database is a defined operation
rather than a delete.

Concurrency: FastAPI runs synchronous handlers in a thread pool, so the connection is
opened with ``check_same_thread=False`` and every statement is serialised behind one
reentrant lock. WAL is enabled so a reader is never blocked by the writer. For one
user on one machine this is more than sufficient, and it avoids a connection pool
whose failure modes would be harder to reason about than the contention it removes.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATABASE_PATH = REPO_ROOT / "runtime" / "studio.sqlite3"

#: Numbered migrations. APPEND ONLY — never edit a statement that has shipped, because
#: an existing database has already applied it. ``PRAGMA user_version`` records how far
#: a database has come.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id           TEXT PRIMARY KEY,
                display_name         TEXT NOT NULL,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL,
                blend_ready          INTEGER NOT NULL DEFAULT 0,
                latest_scene_version TEXT
            )
            """,
            # Named project_references, not references: REFERENCES is SQL syntax.
            """
            CREATE TABLE IF NOT EXISTS project_references (
                reference_id        TEXT PRIMARY KEY,
                project_id          TEXT NOT NULL,
                kind                TEXT NOT NULL,
                display_name        TEXT NOT NULL,
                media_type          TEXT NOT NULL,
                size_bytes          INTEGER NOT NULL,
                sha256              TEXT NOT NULL,
                stored_name         TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                page_count          INTEGER,
                parent_reference_id TEXT,
                page_number         INTEGER,
                width               INTEGER,
                height              INTEGER,
                extracted_text      TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_references_project ON project_references (project_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_references_parent ON project_references (parent_reference_id, page_number)",
            "CREATE INDEX IF NOT EXISTS idx_references_hash ON project_references (project_id, sha256)",
            """
            CREATE TABLE IF NOT EXISTS design_facts (
                project_id TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                source     TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS conversation_turns (
                turn_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                text       TEXT NOT NULL,
                request_id TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_turns_project ON conversation_turns (project_id, turn_id)",
            """
            CREATE TABLE IF NOT EXISTS clarifications (
                clarification_id    TEXT PRIMARY KEY,
                project_id          TEXT NOT NULL,
                session_id          TEXT NOT NULL,
                question            TEXT NOT NULL,
                missing_information TEXT NOT NULL,
                request_id          TEXT,
                created_at          TEXT NOT NULL,
                resolved_at         TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_clarifications_open ON clarifications (project_id, resolved_at)",
            """
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                code        TEXT NOT NULL,
                summary     TEXT NOT NULL,
                reasons     TEXT NOT NULL,
                operations  TEXT NOT NULL,
                request_id  TEXT,
                decision    TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                decided_at  TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_approvals_open ON approvals (project_id, decision)",
            """
            CREATE TABLE IF NOT EXISTS reference_analyses (
                fingerprint TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                analysis    TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS job_records (
                project_id               TEXT NOT NULL,
                job_id                   TEXT NOT NULL,
                session_id               TEXT NOT NULL,
                user_id                  TEXT NOT NULL,
                request_id               TEXT NOT NULL,
                operation_index          INTEGER NOT NULL,
                job_type                 TEXT NOT NULL,
                idempotency_key          TEXT NOT NULL,
                job_status               TEXT NOT NULL,
                created_at               TEXT NOT NULL,
                updated_at               TEXT NOT NULL,
                job                      TEXT NOT NULL,
                content_fingerprint      TEXT,
                worker_id                TEXT,
                execution_phase          TEXT,
                result                   TEXT,
                error                    TEXT,
                preview                  TEXT,
                preview_error            TEXT,
                reconciled               INTEGER NOT NULL DEFAULT 0,
                adopted_after_state_loss INTEGER NOT NULL DEFAULT 0,
                offer_count              INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (project_id, job_id)
            )
            """,
            # The atomic "insert or return existing" contract depends on this being a
            # real uniqueness constraint rather than a check the application performs.
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
                ON job_records (project_id, idempotency_key)
            """,
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE IF NOT EXISTS project_artifacts (
                artifact_id   TEXT NOT NULL,
                project_id    TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                media_type    TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                job_id        TEXT,
                size_bytes    INTEGER NOT NULL DEFAULT 0,
                checksum      TEXT NOT NULL DEFAULT '',
                scene_version TEXT,
                PRIMARY KEY (project_id, artifact_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_artifacts_latest
                ON project_artifacts (project_id, artifact_type, created_at)
            """,
        ),
    ),
    (
        3,
        (
            # The latest authoritative scene, as reported by the worker. This is a
            # CACHE for grounding the agent, never the source of truth: the .blend
            # remains authoritative and every mutation re-verifies against it.
            """
            CREATE TABLE IF NOT EXISTS project_scenes (
                project_id    TEXT PRIMARY KEY,
                scene_version TEXT NOT NULL,
                snapshot      TEXT NOT NULL,
                captured_at   TEXT NOT NULL
            )
            """,
        ),
    ),
    (
        4,
        (
            # Step progress inside a plan, so a reload mid-reconstruction still shows
            # where it had got to.
            "ALTER TABLE job_records ADD COLUMN progress TEXT",
        ),
    ),
    (
        5,
        (
            # When the user last opened a project, so the studio can reopen the one they
            # were working on. Distinct from updated_at, which moves whenever anything
            # about the project changes — including work done by a background read.
            "ALTER TABLE projects ADD COLUMN last_opened_at TEXT",
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1][0]


class StudioDatabase:
    """A serialised SQLite connection with migrations applied on open."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_DATABASE_PATH
        self._lock = threading.RLock()
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()
        self.migrate()

    @property
    def path(self) -> Path:
        return self._path

    def _configure(self) -> None:
        with self._lock:
            # WAL keeps a reader from blocking on the writer. It is unavailable for
            # :memory:, where it is also unnecessary.
            if str(self._path) != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")

    def migrate(self) -> int:
        """Apply every migration the database has not yet seen."""
        with self._lock:
            current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                self._connection.execute("BEGIN")
                try:
                    for statement in statements:
                        self._connection.execute(statement)
                    self._connection.execute(f"PRAGMA user_version={version}")
                    self._connection.execute("COMMIT")
                except Exception:
                    self._connection.execute("ROLLBACK")
                    raise
                current = version
            return current

    @property
    def version(self) -> int:
        with self._lock:
            return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    # -- statement helpers -------------------------------------------------
    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(sql, tuple(parameters))

    def query_one(self, sql: str, parameters: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, tuple(parameters)).fetchone()

    def query_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, tuple(parameters)).fetchall())

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run several statements atomically, holding the lock throughout."""
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "StudioDatabase":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_database(path: Optional[Path] = None) -> StudioDatabase:
    return StudioDatabase(path)
