"""The worker execution journal.

Spec 001, Task 6.

Purpose: survive a process restart. If the worker dies mid-execution, the journal
is the only thing that lets a retry know a plan was already committed — which is
what prevents a second mutation.

``WorkerExecutionStore`` is the abstraction; ``FileSystemExecutionStore`` is the
local implementation for this milestone. Redis/PostgreSQL are deliberately absent
and can replace the implementation without touching WorkerExecutor.

Durability
----------
Every write is atomic: a temp file in the same directory, ``fsync`` on the file,
``os.replace`` (atomic on POSIX), then ``fsync`` on the directory so the rename
itself is durable. A crash therefore leaves either the old record or the new
record, never a half-written one.

Corruption is surfaced, not swallowed
-------------------------------------
An unreadable record raises ``JournalCorruptError``. Treating a corrupt record as
"no record" would be the most dangerous possible behaviour: the worker would
conclude no plan exists, capture a fresh ``expected_before`` from an
already-mutated scene, and double-move the object.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from . import phases

#: Bumped when the record layout changes, so an old record is detected rather
#: than silently misread.
RECORD_VERSION = 1


class JournalError(RuntimeError):
    """Base class for journal failures."""


class JournalCorruptError(JournalError):
    """A record exists but cannot be read as a valid execution record."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExecutionRecord:
    """One worker execution attempt for one job.

    Contains everything a retry needs to proceed safely without re-deriving
    anything from the current scene.
    """

    job_id: str
    project_id: str
    idempotency_key: str
    job_type: str
    phase: str
    record_version: int = RECORD_VERSION
    #: The MoveObjectPlan wire document, persisted BEFORE any mutation. On a
    #: retry this is reused verbatim and never recomputed.
    plan: Optional[dict[str, Any]] = None
    #: Recovery snapshot metadata: {"path", "created_at", "source_sha256"}.
    recovery: Optional[dict[str, Any]] = None
    #: The MoveObjectResult wire document from the last execution attempt.
    result: Optional[dict[str, Any]] = None
    #: Structured error when the phase is failed.
    error: Optional[dict[str, Any]] = None
    #: The public Job status this execution maps to.
    job_status: str = "running"
    attempts: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_wire(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> "ExecutionRecord":
        version = wire.get("record_version")
        if version != RECORD_VERSION:
            raise JournalCorruptError(
                f"unsupported execution record version {version!r}; "
                f"expected {RECORD_VERSION}"
            )
        known = {f.name for f in dataclasses.fields(cls)}
        missing = {
            "job_id",
            "project_id",
            "idempotency_key",
            "job_type",
            "phase",
        } - set(wire)
        if missing:
            raise JournalCorruptError(
                f"execution record is missing required fields: {sorted(missing)}"
            )
        if wire["phase"] not in phases.EXECUTION_PHASES:
            raise JournalCorruptError(f"unknown phase {wire['phase']!r}")
        return cls(**{k: v for k, v in wire.items() if k in known})


class WorkerExecutionStore(Protocol):
    """Durable execution state, scoped per project.

    Every method is project-scoped so records and idempotency keys are never
    compared across projects (see .kiro/steering/security.md).
    """

    def load(self, project_id: str, job_id: str) -> Optional[ExecutionRecord]: ...

    def save(self, record: ExecutionRecord) -> None: ...

    def job_id_for_idempotency(
        self, project_id: str, idempotency_key: str
    ) -> Optional[str]: ...

    def bind_idempotency(
        self, project_id: str, idempotency_key: str, job_id: str
    ) -> str:
        """Claim an idempotency key for a job_id.

        Returns the job_id that owns the key — which may be a DIFFERENT job_id if
        one was already bound. That is how the worker detects two job records
        carrying the same mutation identity without trusting the queue.
        """
        ...


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON so a crash can never leave a partial record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        # fsync the directory so the rename survives a power loss too.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _safe_segment(value: str, label: str) -> str:
    """Reject anything that could escape the storage root.

    Journal paths are built from ids, so an id containing a path separator would
    be a directory-traversal primitive.
    """
    if not value or not value.strip():
        raise JournalError(f"{label} must not be blank")
    if value != Path(value).name or value in (".", ".."):
        raise JournalError(f"{label} {value!r} is not a safe path segment")
    return value


class FileSystemExecutionStore:
    """Local filesystem journal for the Spec 001 vertical slice.

    Layout under the runtime root (git-ignored, never inside a committed fixture
    directory)::

        executions/<project_id>/<job_id>.json
        idempotency/<project_id>/<idempotency_key>.json
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- paths -------------------------------------------------------------

    def _execution_path(self, project_id: str, job_id: str) -> Path:
        return (
            self.root
            / "executions"
            / _safe_segment(project_id, "project_id")
            / f"{_safe_segment(job_id, 'job_id')}.json"
        )

    def _idempotency_path(self, project_id: str, idempotency_key: str) -> Path:
        return (
            self.root
            / "idempotency"
            / _safe_segment(project_id, "project_id")
            / f"{_safe_segment(idempotency_key, 'idempotency_key')}.json"
        )

    # -- WorkerExecutionStore ---------------------------------------------

    def load(self, project_id: str, job_id: str) -> Optional[ExecutionRecord]:
        path = self._execution_path(project_id, job_id)
        if not path.exists():
            return None
        try:
            wire = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError) as exc:
            raise JournalCorruptError(
                f"execution record {path} could not be read: {exc}"
            ) from exc
        if not isinstance(wire, dict):
            raise JournalCorruptError(f"execution record {path} is not an object")
        return ExecutionRecord.from_wire(wire)

    def save(self, record: ExecutionRecord) -> None:
        record.updated_at = utc_now()
        _atomic_write_json(
            self._execution_path(record.project_id, record.job_id), record.to_wire()
        )

    def job_id_for_idempotency(
        self, project_id: str, idempotency_key: str
    ) -> Optional[str]:
        path = self._idempotency_path(project_id, idempotency_key)
        if not path.exists():
            return None
        try:
            wire = json.loads(path.read_text("utf-8"))
            return str(wire["job_id"])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise JournalCorruptError(
                f"idempotency binding {path} could not be read: {exc}"
            ) from exc

    def bind_idempotency(
        self, project_id: str, idempotency_key: str, job_id: str
    ) -> str:
        existing = self.job_id_for_idempotency(project_id, idempotency_key)
        if existing is not None:
            return existing
        _atomic_write_json(
            self._idempotency_path(project_id, idempotency_key),
            {
                "record_version": RECORD_VERSION,
                "project_id": project_id,
                "idempotency_key": idempotency_key,
                "job_id": job_id,
                "bound_at": utc_now(),
            },
        )
        return job_id
