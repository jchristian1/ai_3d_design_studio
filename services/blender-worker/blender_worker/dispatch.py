"""Route a job to the executor that understands it.

Two execution paths coexist deliberately:

* :class:`~blender_worker.executor.WorkerExecutor` — Spec 001's ``move_object`` path,
  driving Blender through the project's own subprocess scripts. Retained unchanged so
  the Spec 001 regression suite keeps passing and remains a working fallback.
* :class:`~blender_worker.capability_executor.CapabilityPlanExecutor` — the
  ``apply_capabilities`` path, driving Blender through the official MCP behind
  ``BlenderCapabilityProvider``.

Keeping them separate rather than merging them was a deliberate choice: the Spec 001
executor encodes hard-won durability behaviour that is verified by a large suite, and
folding a second, differently-shaped operation into it would have put that at risk for
no benefit. They share the journal, the locks, the project registry and the recovery
root, so nothing is duplicated where it matters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .capability_executor import JOB_TYPE as CAPABILITY_JOB_TYPE
from .capability_executor import CapabilityPlanExecutor, PlanOutcome
from .executor import WorkerExecutor, WorkerOutcome

_log = logging.getLogger(__name__)


@dataclass
class DispatchingExecutor:
    """Presents one ``execute`` surface to the worker link client."""

    legacy: WorkerExecutor
    capabilities: CapabilityPlanExecutor

    @property
    def supported_job_types(self) -> tuple[str, ...]:
        from .executor import SUPPORTED_JOB_TYPES

        return (*SUPPORTED_JOB_TYPES, CAPABILITY_JOB_TYPE)

    def execute(self, job: Any) -> Any:
        job_type = _job_type_of(job)
        if job_type == CAPABILITY_JOB_TYPE:
            return _as_worker_outcome(self.capabilities.execute(job))
        return self.legacy.execute(job)


def _job_type_of(job: Any) -> Optional[str]:
    if isinstance(job, dict):
        value = job.get("job_type")
    else:
        value = getattr(job, "job_type", None)
    return str(value) if value is not None else None


def _as_worker_outcome(outcome: PlanOutcome) -> WorkerOutcome:
    """Adapt a plan outcome to the shape the worker link already reports.

    The link client, the protocol and the control plane's reconciler all speak
    ``WorkerOutcome``. Adapting here keeps the capability executor free of transport
    concerns and means the whole reporting path stays unchanged.
    """
    return WorkerOutcome(
        job_id=outcome.job_id,
        project_id=outcome.project_id,
        job_status=outcome.job_status,
        phase=outcome.phase,
        result=_result_with_scene(outcome),
        error=outcome.error,
        applied=outcome.applied_steps > 0,
        already_applied=outcome.already_applied,
        duplicate=outcome.duplicate,
        recovery_path=outcome.recovery_path,
        preview=outcome.preview,
        preview_error=outcome.preview_error,
    )


def _result_with_scene(outcome: PlanOutcome) -> Optional[dict[str, Any]]:
    """Carry the scene and model alongside the result.

    ``result`` is unconstrained at the contract level, which is the sanctioned place for
    operation-specific output. The scene travels here so the control plane can ground the
    next agent turn without a second read, and the model artifact travels here so the
    browser learns about a new GLB through the job it already polls.
    """
    if outcome.result is None and outcome.scene is None and outcome.model is None:
        return None
    result = dict(outcome.result or {})
    if outcome.scene is not None:
        result["scene"] = outcome.scene
    if outcome.model is not None:
        result["model"] = outcome.model
    if outcome.model_error is not None:
        result["model_error"] = outcome.model_error
    if outcome.applied_steps or outcome.skipped_steps:
        result.setdefault("applied", outcome.applied_steps)
        result.setdefault("already_applied", outcome.skipped_steps)
    return result
