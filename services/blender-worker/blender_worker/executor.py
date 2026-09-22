"""The Blender Worker execution workflow.

Spec 001, Task 6; extended with preview generation in Task 10.

Connects the canonical Job (Task 3) to the retry-safe MoveObjectPlan (Task 4), the
seed project (Task 5), and the preview artifact pipeline (Task 10).

Orchestration only: no bpy, no fcntl, no rendering, no filesystem layout decisions.
Those live behind ``BlenderOperationExecutor``, ``ProjectLockProvider``,
``WorkerExecutionStore``, ``ProjectLocator``, ``PreviewGenerator`` and
``ArtifactStore`` so each can be replaced independently.

THE CENTRAL RULE
----------------
A retry MUST reuse the persisted MoveObjectPlan. It must never re-read the
current position to build a fresh ``expected_before``.

    Unsafe: attempt 1 moves 0.0 -> 0.5 and saves; the worker crashes before
            recording completion; the retry reads 0.5, plans 0.5 -> 1.0, and the
            object is moved twice.

    Safe:   attempt 1 persists {expected_before 0.0, desired_after 0.5} BEFORE
            mutating; the retry loads that same plan; Task 4 sees current ==
            desired_after and reports already_applied without moving anything.

That is why the plan is written to the journal before the mutation, and why this
module never calls ``read_object_position`` once a plan exists.

PREVIEW IS REPORTING, NOT MUTATION
----------------------------------
The lifecycle is::

    mutate -> verify -> save -> generate preview -> record preview -> complete
                             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                             everything here is REPORTING

Once ``PROJECT_SAVED`` is reached the design change is durable on disk. Preview
generation therefore cannot fail the job: ``_attach_preview`` never raises and
never touches ``job_status``, and a failure is recorded in ``preview_error`` while
the outcome stays ``succeeded``. Reporting a saved change as failed because a
picture of it could not be produced would be a lie about the user's design.

Preview generation is also idempotent and safe to repeat, because artifact identity
is derived from ``(project_id, job_id)`` and rendering reads the already-saved
project. That is what lets the duplicate-delivery path regenerate a preview lost to
a crash without any risk of a second mutation.

Scope note on planning
----------------------
``expected_before`` is captured at first worker execution, under the project lock.
That is sufficient for a single-worker local slice. It is NOT collaborative
scene-version planning: a future architecture should build the plan against an
explicit scene version in the control plane, so a plan can be rejected if the
scene advanced between planning and execution. This implementation does not
provide that and does not pretend to.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from blender_mcp.tools.move_object import plan_from_delta
from studio_contracts import to_wire
from studio_contracts.jobs import validate_job
from studio_types import MoveObjectPlan, ObjectRef, Vec3

from . import phases
from .blender_ops import BlenderExecutionError, BlenderOperationExecutor
from .journal import (
    ExecutionRecord,
    JournalCorruptError,
    WorkerExecutionStore,
    utc_now,
)
from .locks import LockConflictError, ProjectLockProvider
from .registry import (
    ProjectLocator,
    ProjectResolutionError,
    UnknownProjectError,
    UnsafeProjectIdError,
)

SUPPORTED_JOB_TYPES = ("move_object",)


@dataclass
class WorkerOutcome:
    """What one worker execution concluded.

    ``job_status`` is the PUBLIC Job lifecycle value; ``phase`` is the internal
    worker phase. They are reported separately on purpose.
    """

    job_id: str
    project_id: str
    job_status: str
    phase: str
    plan: Optional[dict[str, Any]] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    applied: bool = False
    already_applied: bool = False
    #: True when this delivery was recognised as a duplicate and no mutation was
    #: attempted.
    duplicate: bool = False
    recovery_path: Optional[str] = None
    #: PreviewArtifact wire document, when a preview is durable for this
    #: execution (Task 10).
    preview: Optional[dict[str, Any]] = None
    #: Why preview generation failed, when the mutation itself succeeded. Kept
    #: separate from ``error`` so a degraded preview never reads as a failed
    #: design change.
    preview_error: Optional[dict[str, Any]] = None

    @property
    def succeeded(self) -> bool:
        return self.job_status == "succeeded"

    @property
    def preview_available(self) -> bool:
        return self.preview is not None


def _error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WorkerExecutor:
    """Executes one job at a time, safely and repeatably."""

    def __init__(
        self,
        store: WorkerExecutionStore,
        locks: ProjectLockProvider,
        projects: ProjectLocator,
        blender: BlenderOperationExecutor,
        recovery_root: Path,
        lock_timeout: float = 0.0,
        previews: Optional[Any] = None,
        artifacts: Optional[Any] = None,
        preview_width: int = 1280,
        preview_height: int = 720,
    ) -> None:
        self.store = store
        self.locks = locks
        self.projects = projects
        self.blender = blender
        self.recovery_root = Path(recovery_root)
        self.lock_timeout = lock_timeout
        #: Optional PreviewGenerator (Task 10). When absent, executions simply
        #: produce no preview — a worker without a preview capability is degraded,
        #: not broken.
        self.previews = previews
        #: Optional ArtifactStore (Task 10). Required alongside ``previews``.
        self.artifacts = artifacts
        self.preview_width = preview_width
        self.preview_height = preview_height

    # -- public entry point ------------------------------------------------

    def execute(self, job: Any) -> WorkerOutcome:
        """Run the workflow for one canonical Job.

        Never raises for expected failures: every outcome is a structured
        WorkerOutcome so a caller reacts to codes rather than exceptions.
        """
        wire = to_wire(job) if not isinstance(job, dict) else dict(job)

        # ---- 2. validate job / project identity -------------------------
        validation = validate_job(wire)
        if not validation.valid:
            return WorkerOutcome(
                job_id=str(wire.get("job_id") or "unknown"),
                project_id=str(wire.get("project_id") or "unknown"),
                job_status="failed",
                phase=phases.FAILED,
                error=_error(
                    "VALIDATION_ERROR",
                    "job does not satisfy the canonical contract: "
                    + "; ".join(e.message for e in validation.errors),
                ),
            )

        job_id = wire["job_id"]
        project_id = wire["project_id"]
        job_type = wire["job_type"]
        idempotency_key = wire["idempotency_key"]

        if job_type not in SUPPORTED_JOB_TYPES:
            return WorkerOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error(
                    "VALIDATION_ERROR",
                    f"unsupported job_type {job_type!r}; this worker executes "
                    f"{', '.join(SUPPORTED_JOB_TYPES)}",
                ),
            )

        # ---- project resolution (security boundary) ---------------------
        try:
            project_path = self.projects.blend_path_for(project_id)
        except (UnknownProjectError, UnsafeProjectIdError) as exc:
            return WorkerOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error("VALIDATION_ERROR", str(exc)),
            )
        except ProjectResolutionError as exc:
            return WorkerOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error("INTERNAL_ERROR", str(exc)),
            )

        # ---- 3. project-scoped lock -------------------------------------
        try:
            with self.locks.hold(project_id, timeout=self.lock_timeout):
                return self._execute_locked(
                    wire, job_id, project_id, job_type, idempotency_key, project_path
                )
        except LockConflictError as exc:
            return WorkerOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error("LOCK_CONFLICT", str(exc)),
            )

    # -- locked section ----------------------------------------------------

    def _execute_locked(
        self,
        wire: dict[str, Any],
        job_id: str,
        project_id: str,
        job_type: str,
        idempotency_key: str,
        project_path: Path,
    ) -> WorkerOutcome:
        # ---- 5. load any existing execution record ----------------------
        # A corrupt record must NOT be treated as "no record": that would make
        # the worker replan from an already-mutated scene.
        try:
            record = self.store.load(project_id, job_id)
        except JournalCorruptError as exc:
            return WorkerOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error(
                    "INTERNAL_ERROR",
                    f"execution journal is unreadable, refusing to execute: {exc}",
                ),
            )

        # ---- 4. duplicate delivery of the same job ----------------------
        if record is not None and record.phase == phases.COMPLETED:
            # The mutation is done and will NOT be repeated. A preview, however,
            # may legitimately be (re)generated here: it renders from the
            # already-saved project, so it cannot change the design, and this is
            # the window where a crash between rendering and recording left the
            # artifact missing.
            self._attach_preview(record, project_path)
            record.phase = phases.COMPLETED
            record.job_status = "succeeded"
            self.store.save(record)
            return WorkerOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="succeeded",
                phase=phases.COMPLETED,
                plan=record.plan,
                result=record.result,
                duplicate=True,
                already_applied=True,
                recovery_path=(record.recovery or {}).get("path"),
                preview=record.preview,
                preview_error=record.preview_error,
            )

        # ---- 4b. same mutation identity under a different job_id --------
        # The worker does not trust the queue or control plane to have enforced
        # idempotency. Binding is project-scoped.
        try:
            owner_job_id = self.store.bind_idempotency(
                project_id, idempotency_key, job_id
            )
        except JournalCorruptError as exc:
            return WorkerOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error("INTERNAL_ERROR", str(exc)),
            )

        if owner_job_id != job_id:
            owner = None
            try:
                owner = self.store.load(project_id, owner_job_id)
            except JournalCorruptError:
                owner = None
            return WorkerOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="succeeded" if owner and owner.phase == phases.COMPLETED
                else "failed",
                phase=owner.phase if owner else phases.FAILED,
                plan=owner.plan if owner else None,
                result=owner.result if owner else None,
                duplicate=True,
                error=None
                if owner and owner.phase == phases.COMPLETED
                else _error(
                    "PRECONDITION_MISMATCH",
                    f"mutation identity {idempotency_key} is already owned by job "
                    f"{owner_job_id}; refusing to apply the same mutation twice",
                ),
                # Report the OWNER's preview: the artifact depicts the mutation,
                # which this job_id did not perform.
                preview=owner.preview if owner else None,
                preview_error=owner.preview_error if owner else None,
            )

        if record is None:
            record = ExecutionRecord(
                job_id=job_id,
                project_id=project_id,
                idempotency_key=idempotency_key,
                job_type=job_type,
                phase=phases.RECEIVED,
                job_status="running",
            )
        record.attempts += 1
        self.store.save(record)

        payload = wire["payload"]
        target = ObjectRef(
            object_id=payload["target"].get("object_id"),
            name=payload["target"].get("name"),
        )

        # ---- 6. plan: create once, then always reuse --------------------
        if record.plan is None:
            delta = payload["delta_meters"]
            try:
                current = self.blender.read_object_position(project_path, target)
            except BlenderExecutionError as exc:
                return self._fail(
                    record, "BLENDER_UNAVAILABLE", f"could not read scene: {exc}"
                )

            if current is None:
                return self._fail(
                    record,
                    "OBJECT_NOT_FOUND",
                    "the job target does not exist in the project",
                )

            plan = plan_from_delta(
                job_id=job_id,
                target=target,
                expected_before_meters=current,
                delta_meters=Vec3(
                    float(delta["x"]), float(delta["y"]), float(delta["z"])
                ),
            )
            # DURABLE BEFORE MUTATION. Everything downstream depends on this.
            record.plan = to_wire(plan)
            record.phase = phases.PLAN_PERSISTED
            self.store.save(record)
        else:
            # RETRY PATH: reuse the persisted plan verbatim. Deliberately no
            # read_object_position call here.
            plan = self._plan_from_record(record)
            if plan is None:
                return self._fail(
                    record,
                    "INTERNAL_ERROR",
                    "persisted execution plan is unusable; refusing to replan",
                )

        # ---- 7. recovery copy before mutation ---------------------------
        if record.recovery is None:
            try:
                recovery_path = self._create_recovery_copy(record, project_path)
            except OSError as exc:
                return self._fail(
                    record, "INTERNAL_ERROR", f"could not create recovery copy: {exc}"
                )
            record.recovery = {
                "path": str(recovery_path),
                "created_at": utc_now(),
                "source_sha256": _sha256(project_path),
            }
            record.phase = phases.RECOVERY_CREATED
            self.store.save(record)

        # ---- 8. execute the Task 4 operation ----------------------------
        record.phase = phases.EXECUTING
        self.store.save(record)

        try:
            result = self.blender.execute_move(project_path, plan)
        except BlenderExecutionError as exc:
            # Recovery evidence is intentionally preserved.
            return self._fail(record, "MUTATION_FAILED", str(exc))

        record.result = result
        if "error" in result and result["error"]:
            return self._fail(
                record,
                str(result["error"].get("code", "MUTATION_FAILED")),
                str(result["error"].get("message", "move_object failed")),
            )

        if not result.get("verified"):
            return self._fail(
                record, "VERIFY_FAILED", "move_object did not report verification"
            )

        record.phase = phases.MUTATION_VERIFIED
        self.store.save(record)

        # ---- 9/10. durability: confirm the SAVED project ----------------
        # In-memory success is not success. Read the project back.
        try:
            persisted = self.blender.read_object_position(project_path, target)
        except BlenderExecutionError as exc:
            return self._fail(
                record, "VERIFY_FAILED", f"could not re-read saved project: {exc}"
            )

        desired = plan.desired_after_meters
        if persisted is None or not self._positions_match(persisted, desired):
            return self._fail(
                record,
                "VERIFY_FAILED",
                "the saved project does not contain the desired position",
            )

        record.phase = phases.PROJECT_SAVED
        self.store.save(record)

        # ---- 10b. preview (Task 10) — NON-FATAL by design ---------------
        # The mutation is already durable in the saved .blend. Everything from
        # here on is REPORTING, so a preview failure degrades the response and
        # never changes the outcome of the design change.
        self._attach_preview(record, project_path)

        # ---- 11/12. complete ------------------------------------------
        record.phase = phases.COMPLETED
        record.job_status = "succeeded"
        record.error = None
        self.store.save(record)

        return WorkerOutcome(
            job_id=job_id,
            project_id=project_id,
            job_status="succeeded",
            phase=phases.COMPLETED,
            plan=record.plan,
            result=result,
            applied=bool(result.get("applied")),
            already_applied=bool(result.get("already_applied")),
            recovery_path=(record.recovery or {}).get("path"),
            preview=record.preview,
            preview_error=record.preview_error,
        )

    # -- preview (Task 10) -------------------------------------------------

    def _attach_preview(self, record: ExecutionRecord, project_path: Path) -> None:
        """Ensure a preview artifact exists for this execution, if possible.

        Contract, in order of importance:

          1. This method NEVER raises and never changes ``job_status``. The
             mutation succeeded before it was called.
          2. It is idempotent. Artifact identity is DERIVED from
             ``(project_id, job_id)``, so re-running it rewrites the same artifact
             rather than accumulating duplicates.
          3. It REUSES an existing artifact when the journal already references one
             and that artifact is still durably present.
          4. It regenerates when the journal references an artifact that is NOT
             present — the crash-between-render-and-record window. Regenerating is
             safe because it renders from the already-saved project and cannot
             touch the design.
        """
        if self.previews is None or self.artifacts is None:
            return

        from studio_preview.artifacts import derive_artifact_id
        from studio_preview.generator import PreviewRequest

        try:
            artifact_id = derive_artifact_id(record.project_id, record.job_id)
        except Exception as exc:  # pragma: no cover - defensive
            record.preview_error = _error(
                "INTERNAL_ERROR", f"could not derive a preview identity: {exc}"
            )
            record.phase = phases.PREVIEW_GENERATED
            self.store.save(record)
            return

        # ---- reuse ------------------------------------------------------
        # Trust the STORE, not the journal, for whether the artifact exists. A
        # journal entry written before the bytes became durable would otherwise
        # make us report a preview that cannot be served.
        existing = self._existing_artifact(record.project_id, artifact_id)
        if existing is not None:
            record.preview = existing
            record.preview_error = None
            record.phase = phases.PREVIEW_GENERATED
            self.store.save(record)
            return

        # ---- generate ---------------------------------------------------
        try:
            outcome = self.previews.generate(
                PreviewRequest(
                    project_id=record.project_id,
                    # TRUSTED path, resolved by the worker's ProjectLocator from a
                    # project_id. It never came from a Job or a network message.
                    project_path=project_path,
                    width=self.preview_width,
                    height=self.preview_height,
                    job_id=record.job_id,
                )
            )
        except Exception as exc:
            # A generator that raises is a bug, but it must still not be able to
            # fail the job.
            record.preview = None
            record.preview_error = _error(
                "INTERNAL_ERROR", f"preview generation failed: {exc}"
            )
            record.phase = phases.PREVIEW_GENERATED
            self.store.save(record)
            return

        if not outcome.ok or outcome.render is None:
            record.preview = None
            record.preview_error = dict(
                outcome.error
                or _error("INTERNAL_ERROR", "preview generation reported no result")
            )
            record.phase = phases.PREVIEW_GENERATED
            self.store.save(record)
            return

        # ---- persist ----------------------------------------------------
        try:
            from studio_preview.artifacts import to_artifact_wire

            artifact = self.artifacts.put(
                project_id=record.project_id,
                artifact_id=artifact_id,
                data=outcome.render.image_bytes,
                media_type=outcome.render.media_type,
                width=outcome.render.width,
                height=outcome.render.height,
                job_id=record.job_id,
                engine=outcome.render.engine,
            )
            record.preview = to_artifact_wire(artifact)
            record.preview_error = None
        except Exception as exc:
            record.preview = None
            record.preview_error = _error(
                "INTERNAL_ERROR", f"preview could not be stored: {exc}"
            )

        record.phase = phases.PREVIEW_GENERATED
        self.store.save(record)

    def _existing_artifact(
        self, project_id: str, artifact_id: str
    ) -> Optional[dict[str, Any]]:
        """The stored artifact wire document, when it is genuinely present."""
        if self.artifacts is None:
            return None
        try:
            from studio_preview.artifacts import to_artifact_wire

            artifact = self.artifacts.get(project_id, artifact_id)
        except Exception:  # pragma: no cover - a store failure is not fatal
            return None
        return to_artifact_wire(artifact) if artifact is not None else None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _positions_match(actual: Vec3, desired: Vec3) -> bool:
        from blender_mcp.tolerance import positions_equal

        return positions_equal(actual, desired)

    @staticmethod
    def _plan_from_record(record: ExecutionRecord) -> Optional[MoveObjectPlan]:
        wire = record.plan
        if not isinstance(wire, dict):
            return None
        try:
            return MoveObjectPlan(
                job_id=wire["job_id"],
                target=ObjectRef(
                    object_id=wire["target"].get("object_id"),
                    name=wire["target"].get("name"),
                ),
                expected_before_meters=Vec3(**wire["expected_before_meters"]),
                delta_meters=Vec3(**wire["delta_meters"]),
                desired_after_meters=Vec3(**wire["desired_after_meters"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _create_recovery_copy(
        self, record: ExecutionRecord, project_path: Path
    ) -> Path:
        """Snapshot the project as it is BEFORE this execution mutates it.

        The filename includes the attempt number and a timestamp so an earlier
        attempt's recovery evidence is never overwritten.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        directory = self.recovery_root / record.project_id / record.job_id
        directory.mkdir(parents=True, exist_ok=True)
        destination = (
            directory / f"attempt{record.attempts:03d}-{stamp}-{project_path.name}"
        )
        shutil.copy2(project_path, destination)
        return destination

    def _fail(
        self, record: ExecutionRecord, code: str, message: str
    ) -> WorkerOutcome:
        record.phase = phases.FAILED
        record.job_status = "failed"
        record.error = _error(code, message)
        self.store.save(record)
        return WorkerOutcome(
            job_id=record.job_id,
            project_id=record.project_id,
            job_status="failed",
            phase=phases.FAILED,
            plan=record.plan,
            result=record.result,
            error=record.error,
            recovery_path=(record.recovery or {}).get("path"),
            # A failed execution reports no preview. Attaching one would imply a
            # picture of a change that was never applied.
            preview=None,
            preview_error=record.preview_error,
        )
