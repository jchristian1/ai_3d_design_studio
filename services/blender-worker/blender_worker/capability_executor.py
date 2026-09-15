"""Execute an ordered capability plan with per-step durability.

One user intent ("reconstruct this plan") becomes ONE job holding the project lock
once, creating ONE recovery point, and reporting as ONE reply. Inside it, every step is
journalled individually, which is what makes a reconstruction **resumable rather than
repeatable**: a retry after a crash skips the walls that already exist instead of
building nine more.

The order of checks per step is deliberate and mirrors Spec 001:

1. **already satisfied?** — read the authoritative scene and ask whether the step's
   desired end state is already true. If so, record it and do NOT invoke.
2. **invoke** the capability through ``BlenderCapabilityProvider``.
3. **verify** by reading the scene back. A returned ``status: ok`` is not evidence.
4. **persist** the step's state before moving on.

Checking "already satisfied" FIRST is what makes a verbatim retry succeed instead of
failing as stale — the same reasoning as Spec 001's already-applied rule.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from studio_contracts import to_wire
from studio_contracts.jobs import validate_job

from . import phases
from .capability import names, normalize
from .capability.provider import (
    BackendHealth,
    BlenderCapabilityProvider,
    CapabilityRequest,
    CapabilityResult,
)
from .journal import ExecutionRecord, JournalCorruptError, WorkerExecutionStore, utc_now
from .locks import LockConflictError, ProjectLockProvider
from .registry import (
    ProjectLocator,
    ProjectResolutionError,
    UnknownProjectError,
    UnsafeProjectIdError,
)

_log = logging.getLogger(__name__)

JOB_TYPE = "apply_capabilities"

#: Tolerance for comparing observed geometry with a desired target. Matches the
#: contract's digest quantum, so "already applied" and "unchanged scene version" agree.
TOLERANCE_METERS = 1e-6
TOLERANCE_RADIANS = 1e-6

APPLIED = "applied"
ALREADY_APPLIED = "already_applied"
FAILED = "failed"


def _error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


@dataclass
class StepProgress:
    """What the browser shows while a plan runs."""

    step_index: int
    step_count: int
    label: str

    def snapshot(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "step_count": self.step_count,
            "label": self.label,
        }


@dataclass
class PlanOutcome:
    """The result of executing one capability plan."""

    job_id: str
    project_id: str
    job_status: str
    phase: str
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    applied_steps: int = 0
    skipped_steps: int = 0
    duplicate: bool = False
    already_applied: bool = False
    recovery_path: Optional[str] = None
    scene: Optional[dict[str, Any]] = None
    preview: Optional[dict[str, Any]] = None
    preview_error: Optional[dict[str, Any]] = None
    model: Optional[dict[str, Any]] = None
    model_error: Optional[dict[str, Any]] = None

    @property
    def succeeded(self) -> bool:
        return self.job_status == "succeeded"


@dataclass
class CapabilityPlanExecutor:
    """Applies capability plans durably."""

    store: WorkerExecutionStore
    locks: ProjectLockProvider
    projects: ProjectLocator
    provider: BlenderCapabilityProvider
    recovery_root: Path
    lock_timeout: float = 0.0
    #: Called after each step so the control plane can report progress.
    on_progress: Optional[Callable[[StepProgress], None]] = None
    #: Optional artifact production, injected so the executor stays testable offline.
    artifacts: Optional[Any] = None
    previews: Optional[Any] = None
    preview_width: int = 640
    preview_height: int = 360

    # -- entry point -------------------------------------------------------
    def execute(self, job: Any) -> PlanOutcome:
        wire = to_wire(job) if not isinstance(job, dict) else dict(job)
        validation = validate_job(wire)
        if not validation.valid:
            first = validation.errors[0]
            return PlanOutcome(
                job_id=str(wire.get("job_id") or "unknown"),
                project_id=str(wire.get("project_id") or "unknown"),
                job_status="failed",
                phase=phases.FAILED,
                error=_error(first.code, first.message),
            )

        job_id = str(wire["job_id"])
        project_id = str(wire["project_id"])
        if wire.get("job_type") != JOB_TYPE:
            return PlanOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error(
                    "VALIDATION_ERROR",
                    f"this executor handles {JOB_TYPE}, not {wire.get('job_type')!r}",
                ),
            )

        try:
            project_path = self.projects.blend_path_for(project_id)
        except (UnknownProjectError, UnsafeProjectIdError) as exc:
            return PlanOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error("VALIDATION_ERROR", str(exc)),
            )
        except ProjectResolutionError as exc:
            return PlanOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error("INTERNAL_ERROR", str(exc)),
            )

        try:
            with self.locks.hold(project_id, timeout=self.lock_timeout):
                return self._execute_locked(wire, job_id, project_id, project_path)
        except LockConflictError as exc:
            return PlanOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error("LOCK_CONFLICT", str(exc)),
            )

    # -- locked execution --------------------------------------------------
    def _execute_locked(
        self,
        wire: Mapping[str, Any],
        job_id: str,
        project_id: str,
        project_path: Path,
    ) -> PlanOutcome:
        payload = wire.get("payload") or {}
        operations = list(payload.get("operations") or ())
        required_version = payload.get("required_scene_version")
        idempotency_key = str(wire["idempotency_key"])

        try:
            record = self.store.load(project_id, job_id)
        except JournalCorruptError as exc:
            return PlanOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error(
                    "INTERNAL_ERROR",
                    f"execution journal is unreadable, refusing to execute: {exc}",
                ),
            )

        # A completed plan is never re-run. Artifacts may still be (re)produced: they
        # render from the already-saved project, so they cannot change the design.
        if record is not None and record.phase == phases.COMPLETED:
            self._attach_artifacts(record, project_id, project_path)
            self.store.save(record)
            return PlanOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="succeeded",
                phase=phases.COMPLETED,
                result=record.result,
                duplicate=True,
                already_applied=True,
                recovery_path=(record.recovery or {}).get("path"),
                scene=record.scene,
                preview=record.preview,
                preview_error=record.preview_error,
                model=record.model,
                model_error=record.model_error,
                applied_steps=self._count(record, APPLIED),
                skipped_steps=self._count(record, ALREADY_APPLIED),
            )

        owner = self.store.bind_idempotency(project_id, idempotency_key, job_id)
        if owner != job_id:
            return PlanOutcome(
                job_id=job_id,
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error(
                    "PRECONDITION_MISMATCH",
                    f"another job ({owner}) already owns this mutation identity",
                ),
            )

        if record is None:
            record = ExecutionRecord(
                job_id=job_id,
                project_id=project_id,
                idempotency_key=idempotency_key,
                job_type=JOB_TYPE,
                phase=phases.RECEIVED,
            )
        record.attempts += 1
        record.updated_at = utc_now()
        record.job_status = "running"
        self.store.save(record)

        # ---- read the authoritative scene -------------------------------
        readout = self._read_scene(project_id, project_path)
        if isinstance(readout, PlanOutcome):
            return self._fail_from(record, readout)

        # ---- scene-version precondition, in-lock, before any mutation ---
        nothing_applied = not any(
            entry.get("status") in (APPLIED, ALREADY_APPLIED)
            for entry in (record.steps or {}).values()
        )
        if required_version and nothing_applied:
            observed = readout["scene_version"]
            if observed != required_version:
                return self._fail(
                    record,
                    "SCENE_VERSION_MISMATCH",
                    "The project changed since this was planned, so nothing was modified.",
                )

        # ---- one recovery point for the whole plan ----------------------
        if not record.recovery:
            recovery_path = self._create_recovery_copy(record, project_path)
            record.recovery = {
                "path": str(recovery_path),
                "created_at": utc_now(),
            }
            record.phase = phases.RECOVERY_CREATED
            self.store.save(record)

        # ---- steps ------------------------------------------------------
        record.phase = phases.EXECUTING
        self.store.save(record)

        step_count = len(operations)
        for position, operation in enumerate(operations):
            index = int(operation.get("operation_index", position))
            key = str(index)
            existing = (record.steps or {}).get(key)
            if existing and existing.get("status") in (APPLIED, ALREADY_APPLIED):
                continue

            capability = str(operation.get("capability") or "")
            label = str(operation.get("label") or capability)
            arguments = dict(operation.get("arguments") or {})
            self._report(StepProgress(step_index=index, step_count=step_count, label=label))

            # 1. Already satisfied? This is what makes a retry safe.
            if self._already_satisfied(capability, arguments, readout):
                record.steps[key] = {
                    "status": ALREADY_APPLIED,
                    "capability": capability,
                    "label": label,
                }
                record.updated_at = utc_now()
                self.store.save(record)
                continue

            # 2. Invoke.
            result = self.provider.invoke(
                CapabilityRequest(
                    capability=capability,
                    project_id=project_id,
                    project_path=project_path,
                    arguments=arguments,
                    approval_token=operation.get("approval_token"),
                )
            )
            if not result.ok:
                record.steps[key] = {
                    "status": FAILED,
                    "capability": capability,
                    "label": label,
                    "error": _error(
                        result.error_code or "MUTATION_FAILED",
                        result.error_message or "The step failed.",
                    ),
                }
                self.store.save(record)
                return self._fail(
                    record,
                    result.error_code or "MUTATION_FAILED",
                    result.error_message or f"{label} could not be completed.",
                    partial=True,
                )

            # 3. Verify by reading the scene back.
            readout = self._read_scene(project_id, project_path)
            if isinstance(readout, PlanOutcome):
                return self._fail_from(record, readout)

            verified = self._verify(capability, arguments, readout)
            if not verified:
                record.steps[key] = {
                    "status": FAILED,
                    "capability": capability,
                    "label": label,
                    "error": _error("VERIFY_FAILED", "The change could not be confirmed."),
                }
                self.store.save(record)
                return self._fail(
                    record,
                    "VERIFY_FAILED",
                    f"{label} reported success but the change could not be confirmed.",
                    partial=True,
                )

            # 4. Persist per-step state, immediately.
            record.steps[key] = {
                "status": APPLIED,
                "capability": capability,
                "label": label,
                "result": dict(result.data),
                "scene_version": readout["scene_version"],
            }
            record.scene = readout
            record.phase = phases.PROJECT_SAVED
            record.updated_at = utc_now()
            self.store.save(record)

        record.scene = readout
        record.phase = phases.SCENE_INSPECTED
        self.store.save(record)

        # ---- artifacts --------------------------------------------------
        self._attach_artifacts(record, project_id, project_path)

        record.result = {
            "applied": self._count(record, APPLIED),
            "already_applied": self._count(record, ALREADY_APPLIED),
            "step_count": step_count,
            "scene_version": readout["scene_version"],
            "summary": payload.get("summary"),
        }
        record.phase = phases.COMPLETED
        record.job_status = "succeeded"
        record.error = None
        self.store.save(record)

        return PlanOutcome(
            job_id=job_id,
            project_id=project_id,
            job_status="succeeded",
            phase=phases.COMPLETED,
            result=record.result,
            applied_steps=self._count(record, APPLIED),
            skipped_steps=self._count(record, ALREADY_APPLIED),
            recovery_path=(record.recovery or {}).get("path"),
            scene=record.scene,
            preview=record.preview,
            preview_error=record.preview_error,
            model=record.model,
            model_error=record.model_error,
        )

    # -- scene reading -----------------------------------------------------
    def _read_scene(
        self, project_id: str, project_path: Path
    ) -> Any:
        """Read and normalise the authoritative scene, or return a failure outcome."""
        result = self.provider.invoke(
            CapabilityRequest(
                capability=names.INSPECT_SCENE,
                project_id=project_id,
                project_path=project_path,
            )
        )
        if not result.ok:
            return PlanOutcome(
                job_id="",
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error(
                    result.error_code or "BLENDER_UNAVAILABLE",
                    result.error_message or "The scene could not be read.",
                ),
            )
        try:
            readout = normalize.scene_snapshot_from_backend(project_id, result.data)
        except normalize.NormalisationError as exc:
            return PlanOutcome(
                job_id="",
                project_id=project_id,
                job_status="failed",
                phase=phases.FAILED,
                error=_error("VALIDATION_ERROR", f"the scene could not be understood: {exc}"),
            )
        return to_wire(readout.snapshot)

    @staticmethod
    def _find(scene: Mapping[str, Any], arguments: Mapping[str, Any]) -> Optional[dict]:
        object_id = arguments.get("object_id")
        name = arguments.get("name") or arguments.get("display_name")
        objects = scene.get("objects") or ()
        if object_id:
            for entry in objects:
                if entry.get("studio_object_id") == object_id:
                    return dict(entry)
        if name:
            for entry in objects:
                if entry.get("name") == name:
                    return dict(entry)
        return None

    # -- already-applied and verification ---------------------------------
    def _already_satisfied(
        self, capability: str, arguments: Mapping[str, Any], scene: Mapping[str, Any]
    ) -> bool:
        """Would this step be a no-op against the current scene?

        For transforms the test is "is the object already where it should end up".
        For creation the test is "does an object with this stable id already exist" —
        which is exactly what stops a retried reconstruction duplicating its walls.
        """
        target = self._find(scene, arguments)

        if capability in (
            names.CREATE_WALL,
            names.CREATE_FLOOR,
            names.CREATE_CEILING,
            names.CREATE_OBJECT,
            names.CREATE_DOOR_PLACEHOLDER,
            names.CREATE_WINDOW_PLACEHOLDER,
        ):
            object_id = arguments.get("object_id")
            if not object_id:
                # Without a stable id we cannot tell a duplicate from a second wall
                # that genuinely belongs there, so we do not guess.
                return False
            return any(
                entry.get("studio_object_id") == object_id
                for entry in scene.get("objects") or ()
            )

        if capability == names.DELETE_OBJECT:
            return target is None

        if target is None:
            return False

        if capability == names.MOVE_OBJECT:
            return _vec_matches(
                target.get("world_position_meters"),
                arguments.get("desired_position_meters"),
                TOLERANCE_METERS,
            )
        if capability == names.ROTATE_OBJECT:
            return _vec_matches(
                target.get("rotation_euler_radians"),
                arguments.get("desired_rotation_radians"),
                TOLERANCE_RADIANS,
            )
        if capability == names.SCALE_OBJECT:
            return _vec_matches(target.get("scale"), arguments.get("desired_scale"), TOLERANCE_METERS)
        if capability == names.SET_OBJECT_DIMENSIONS:
            return _vec_matches(
                target.get("dimensions_meters"),
                arguments.get("desired_dimensions_meters"),
                TOLERANCE_METERS,
            )
        if capability == names.SET_MATERIAL_COLOR:
            material = target.get("material") or {}
            return _colour_matches(material.get("base_color"), arguments.get("color_linear_srgb"))

        # An opening, a duplicate, or model-authored code cannot be judged from the
        # scene alone, so it is always attempted. Its own idempotency is the plan's
        # per-step journal, which prevents a second attempt after success.
        return False

    def _verify(
        self, capability: str, arguments: Mapping[str, Any], scene: Mapping[str, Any]
    ) -> bool:
        """Confirm the step's intended end state is now true.

        Deliberately the same predicate as ``_already_satisfied`` for every capability
        whose end state is expressible: if a step's desired state would have made it a
        no-op beforehand, then after running it that state must hold.
        """
        if capability in (names.EXECUTE_BLENDER_PYTHON, names.CREATE_OPENING, names.DUPLICATE_OBJECT):
            # No declared end state to check. The capability's own read-back inside
            # Blender is the only evidence available, and it already succeeded.
            return True
        return self._already_satisfied(capability, arguments, scene)

    # -- artifacts ---------------------------------------------------------
    def _attach_artifacts(
        self, record: ExecutionRecord, project_id: str, project_path: Path
    ) -> None:
        """Produce the PNG preview and the GLB model. Never fails the mutation."""
        self._attach_preview(record, project_id, project_path)
        self._attach_model(record, project_id, project_path)

    def _attach_preview(
        self, record: ExecutionRecord, project_id: str, project_path: Path
    ) -> None:
        if record.preview is not None or self.previews is None or self.artifacts is None:
            return
        try:
            from studio_preview.artifacts import derive_artifact_id, to_artifact_wire
            from studio_preview.generator import PreviewRequest

            artifact_id = derive_artifact_id(project_id, record.job_id)
            existing = self.artifacts.get(project_id, artifact_id)
            if existing is not None:
                record.preview = to_artifact_wire(existing)
                record.phase = phases.PREVIEW_GENERATED
                return

            outcome = self.previews.generate(
                PreviewRequest(
                    project_id=project_id,
                    project_path=project_path,
                    width=self.preview_width,
                    height=self.preview_height,
                    job_id=record.job_id,
                )
            )
            if not outcome.ok or outcome.render is None:
                record.preview_error = _error(
                    "MUTATION_FAILED", "The preview could not be generated."
                )
                return
            artifact = self.artifacts.put(
                project_id=project_id,
                artifact_id=artifact_id,
                data=outcome.render.image_bytes,
                media_type=outcome.render.media_type,
                width=outcome.render.width,
                height=outcome.render.height,
                job_id=record.job_id,
                engine=outcome.render.engine,
            )
            record.preview = to_artifact_wire(artifact)
            record.phase = phases.PREVIEW_GENERATED
        except Exception as error:  # a preview must never fail a durable mutation
            _log.warning("preview generation failed: %s", error)
            record.preview_error = _error("MUTATION_FAILED", "The preview could not be generated.")

    def _attach_model(
        self, record: ExecutionRecord, project_id: str, project_path: Path
    ) -> None:
        if record.model is not None or self.artifacts is None:
            return
        try:
            from studio_preview.artifacts import derive_artifact_id, to_artifact_wire

            artifact_id = derive_artifact_id(
                project_id, record.job_id, artifact_type="model_glb"
            )
            existing = self.artifacts.get(project_id, artifact_id)
            if existing is not None:
                record.model = to_artifact_wire(existing)
                record.phase = phases.MODEL_EXPORTED
                return

            destination = self.recovery_root.parent / "exports" / project_id / f"{artifact_id}.glb"
            destination.parent.mkdir(parents=True, exist_ok=True)

            result = self.provider.invoke(
                CapabilityRequest(
                    capability=names.EXPORT_GLB,
                    project_id=project_id,
                    project_path=project_path,
                    arguments={"output_path": str(destination)},
                )
            )
            if not result.ok or not destination.exists():
                record.model_error = _error(
                    "MUTATION_FAILED", "The 3D model could not be exported."
                )
                return

            data = destination.read_bytes()
            if not data.startswith(b"glTF"):
                record.model_error = _error(
                    "VERIFY_FAILED", "The exported model was not a valid glTF file."
                )
                return
            artifact = self.artifacts.put(
                project_id=project_id,
                artifact_id=artifact_id,
                data=data,
                media_type="model/gltf-binary",
                width=0,
                height=0,
                artifact_type="model_glb",
                job_id=record.job_id,
            )
            record.model = to_artifact_wire(artifact)
            record.phase = phases.MODEL_EXPORTED
            destination.unlink(missing_ok=True)
        except Exception as error:  # an export must never fail a durable mutation
            _log.warning("model export failed: %s", error)
            record.model_error = _error(
                "MUTATION_FAILED", "The 3D model could not be exported."
            )

    # -- helpers -----------------------------------------------------------
    def _report(self, progress: StepProgress) -> None:
        if self.on_progress is None:
            return
        try:
            self.on_progress(progress)
        except Exception as error:  # progress is cosmetic; never fail on it
            _log.debug("progress callback raised: %s", error)

    @staticmethod
    def _count(record: ExecutionRecord, status: str) -> int:
        return sum(
            1 for entry in (record.steps or {}).values() if entry.get("status") == status
        )

    def _create_recovery_copy(self, record: ExecutionRecord, project_path: Path) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        directory = self.recovery_root / record.project_id / record.job_id
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"attempt{record.attempts:03d}-{stamp}-{project_path.name}"
        shutil.copy2(project_path, destination)
        return destination

    def _fail(
        self,
        record: ExecutionRecord,
        code: str,
        message: str,
        *,
        partial: bool = False,
    ) -> PlanOutcome:
        record.phase = phases.FAILED
        record.job_status = "failed"
        record.error = _error(code, message)
        self.store.save(record)
        return PlanOutcome(
            job_id=record.job_id,
            project_id=record.project_id,
            job_status="failed",
            phase=phases.FAILED,
            error=record.error,
            applied_steps=self._count(record, APPLIED),
            skipped_steps=self._count(record, ALREADY_APPLIED),
            recovery_path=(record.recovery or {}).get("path"),
            # A partial failure keeps the scene it observed, so the control plane can
            # still ground the next turn on what actually exists now.
            scene=record.scene if partial else None,
        )

    def _fail_from(self, record: ExecutionRecord, failure: PlanOutcome) -> PlanOutcome:
        error = failure.error or _error("INTERNAL_ERROR", "execution failed")
        return self._fail(record, error["code"], error["message"])


def _vec_matches(
    observed: Optional[Mapping[str, Any]],
    desired: Optional[Mapping[str, Any]],
    tolerance: float,
) -> bool:
    if not isinstance(observed, Mapping) or not isinstance(desired, Mapping):
        return False
    for axis in ("x", "y", "z"):
        if axis not in desired:
            continue
        try:
            left = float(observed.get(axis))  # type: ignore[arg-type]
            right = float(desired.get(axis))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if abs(left - right) > tolerance:
            return False
    return True


def _colour_matches(
    observed: Optional[Mapping[str, Any]], desired: Optional[Mapping[str, Any]]
) -> bool:
    if not isinstance(observed, Mapping) or not isinstance(desired, Mapping):
        return False
    for channel in ("r", "g", "b", "a"):
        if channel not in desired:
            continue
        try:
            left = float(observed.get(channel))  # type: ignore[arg-type]
            right = float(desired.get(channel))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        # Colour is stored as float32 inside Blender, so an exact comparison would
        # spuriously fail. 1e-4 is far tighter than any visible difference.
        if abs(left - right) > 1e-4:
            return False
    return True
