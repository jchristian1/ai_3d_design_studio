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
from typing import Any, Mapping, Optional, Protocol

from . import phases

#: Bumped when the record layout changes INCOMPATIBLY, so an old record is
#: detected rather than silently misread.
#:
#: Task 10 added the optional ``preview`` / ``preview_error`` fields and did NOT
#: bump this, deliberately. ``from_wire`` filters to known fields and every new
#: field has a default, so a v1 record loads correctly (previews simply absent)
#: and a v1 reader ignores the new keys. Bumping would instead make every existing
#: local journal record unreadable and refuse to execute those jobs — a real cost
#: for no safety gain. Contrast the worker PROTOCOL, which did bump: there,
#: ``additionalProperties: false`` means an older peer actively REJECTS an unknown
#: field, so the same additive change is breaking on the wire but not on disk.
RECORD_VERSION = 1


class JournalError(RuntimeError):
    """Base class for journal failures."""


class JournalCorruptError(JournalError):
    """A record exists but cannot be read as a valid execution record."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Step states, mirrored from ``capability_executor``. Duplicated as literals rather
#: than imported to keep the journal free of a dependency on the executor.
_APPLIED = "applied"
_ALREADY_APPLIED = "already_applied"


def merge_report_result(
    result: Optional[dict[str, Any]],
    *,
    scene: Optional[dict[str, Any]] = None,
    model: Optional[dict[str, Any]] = None,
    model_error: Optional[dict[str, Any]] = None,
    steps: Optional[Mapping[str, Any]] = None,
    applied: Optional[int] = None,
    already_applied: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Combine an operation result with the scene and artifacts that accompany it.

    ``result`` is unconstrained at the contract level, which is the sanctioned place for
    operation-specific output. The scene travels there so the control plane can ground the
    next agent turn without a second Blender read, and the model artifact travels there so
    the browser learns about a new GLB through the job it already polls.

    ONE implementation, used by both the live report and any resent one, so a reconciled
    result cannot carry less than the original.
    """
    if applied is None or already_applied is None:
        counted = _count_steps(steps or {})
        applied = counted[0] if applied is None else applied
        already_applied = counted[1] if already_applied is None else already_applied

    if result is None and scene is None and model is None:
        return None

    merged = dict(result or {})
    if scene is not None:
        merged["scene"] = scene
    if model is not None:
        merged["model"] = model
    if model_error is not None:
        merged["model_error"] = model_error
    if applied or already_applied:
        merged.setdefault("applied", applied)
        merged.setdefault("already_applied", already_applied)
    return merged


def _count_steps(steps: Mapping[str, Any]) -> tuple[int, int]:
    applied = 0
    already = 0
    for entry in steps.values():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("status") == _APPLIED:
            applied += 1
        elif entry.get("status") == _ALREADY_APPLIED:
            already += 1
    return applied, already


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
    #: The PreviewArtifact wire document for this execution, once one is durable.
    #: Its presence is what makes a retry REUSE the existing preview instead of
    #: rendering another. It is deliberately separate from ``result``: a preview
    #: describes a picture of the mutation, not the mutation itself.
    preview: Optional[dict[str, Any]] = None
    #: Why preview generation failed, when the mutation itself succeeded. Recorded
    #: separately from ``error`` so a failed preview can never be mistaken for a
    #: failed design change.
    preview_error: Optional[dict[str, Any]] = None
    #: Structured error when the phase is failed.
    error: Optional[dict[str, Any]] = None
    #: PER-STEP state for an ``apply_capabilities`` plan, keyed by the step's
    #: ``operation_index`` as a string (JSON object keys are always strings).
    #:
    #: This is what makes a multi-step reconstruction resumable rather than
    #: repeatable. Each entry records the step's terminal state and its observed
    #: result, so a retry after a crash skips what is already applied instead of
    #: creating nine more walls. Written after EVERY step, not at the end.
    steps: dict[str, Any] = field(default_factory=dict)
    #: The authoritative SceneSnapshot wire document observed after the last
    #: successful step, used to chain scene versions across steps and reported to
    #: the control plane so the agent can be grounded without a second read.
    scene: Optional[dict[str, Any]] = None
    #: The model artifact (GLB) wire document, once one is durable. Separate from
    #: ``preview`` for the same reason ``preview`` is separate from ``result``.
    model: Optional[dict[str, Any]] = None
    #: Why model export failed, when the mutation itself succeeded.
    model_error: Optional[dict[str, Any]] = None
    #: The public Job status this execution maps to.
    job_status: str = "running"
    #: Whether the result reached the control plane. False after a dropped
    #: result channel, which is what reconciliation resends. Purely a REPORTING
    #: flag: it never affects whether the mutation happened.
    result_delivered: bool = False
    attempts: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_wire(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def result_for_report(self) -> Optional[dict[str, Any]]:
        """The result as the CONTROL PLANE should receive it.

        The scene and the model artifact are journalled in their own fields, because a
        retry needs them independently of the operation result. When the result is
        reported, though, they belong on it — that is how the control plane learns the
        new scene and the new GLB.

        This exists so a RESENT result carries the same information as the original one.
        Without it, a report lost to a dropped connection would come back as a bare
        success and the browser would never see the model it just produced.
        """
        return merge_report_result(
            self.result,
            scene=self.scene,
            model=self.model,
            model_error=self.model_error,
            steps=self.steps,
        )

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

    def undelivered_results(self) -> list[ExecutionRecord]:
        """Terminal executions whose result has not reached the control plane.

        Used by link reconciliation after a reconnect. Resending a stored result
        cannot mutate Blender, so this is safe to call repeatedly.
        """
        ...

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

    def undelivered_results(self) -> list[ExecutionRecord]:
        """Scan the journal for terminal executions still awaiting delivery.

        A corrupt record is skipped here rather than raising: reconciliation is a
        best-effort reporting sweep, and refusing to report every other job
        because one record is damaged would be worse. Corruption still blocks
        EXECUTION of that job, which is where it matters.
        """
        pending: list[ExecutionRecord] = []
        root = self.root / "executions"
        if not root.exists():
            return pending
        for path in sorted(root.glob("*/*.json")):
            try:
                wire = json.loads(path.read_text("utf-8"))
                record = ExecutionRecord.from_wire(wire)
            except (ValueError, OSError, JournalCorruptError):
                continue
            if record.phase in phases.TERMINAL_PHASES and not record.result_delivered:
                pending.append(record)
        return pending

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
