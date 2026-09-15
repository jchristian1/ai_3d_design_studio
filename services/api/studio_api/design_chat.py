"""``DesignChatService`` — one user message in, one outcome out.

This is where the product's promises are kept structurally rather than by discipline:

* **A required clarification blocks all modelling.** Only a ``PlanProposal`` reaches job
  creation. A ``Clarification`` returns before that point, so "zero mutations while a
  question is open" is a property of the control flow, not a check someone could forget.
* **Model-authored code that reaches beyond the scene is never executed unasked.** The
  plan is classified here, before any job exists. A flagged step becomes an
  ``ApprovalRequired`` outcome and the plan is parked until the user decides.
* **One user message produces one reply.** A reconstruction becomes ONE
  ``apply_capabilities`` job, so the browser has one thing to track and update in place.
* **Facts learned are remembered.** Anything Astra records is persisted before the reply
  is returned, so the next turn does not ask again — even across a restart.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from studio_agent.design_provider import DesignAgentProvider
from studio_agent.outcome import (
    AgentError,
    AgentOutcome,
    Answer,
    ApprovalRequired,
    Clarification,
    PlanProposal,
    ValidatedOperation,
)
from studio_contracts import ChatError
from studio_contracts.jobs import create_capability_job, to_job_wire
from studio_types import CapabilityOperation, SceneSnapshot
from studio_validation import code_risk

from . import errors
from .context_builder import ContextBuilder
from .identity import TrustedIdentity
from .job_records import JobRecord, record_from_job
from .projects import ProjectRegistry
from .storage.models import ApprovalRecord, ClarificationRecord, ConversationTurnRecord
from .storage.repositories import StudioRepositories

_log = logging.getLogger(__name__)

#: Outcome kinds the browser understands.
ANSWER = "answer"
CLARIFICATION = "clarification"
APPROVAL_REQUIRED = "approval_required"
PLAN = "plan"
ERROR = "error"


def new_request_scoped_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


@dataclass(frozen=True)
class DesignChatRequest:
    """One message from the browser."""

    request_id: str
    project_id: str
    session_id: str
    message: str
    attached_reference_ids: tuple[str, ...] = ()
    selected_object_id: Optional[str] = None


@dataclass
class DesignTurnResult:
    """Everything the browser needs about one turn."""

    kind: str
    message: str
    request_id: str
    project_id: str
    session_id: str
    job_id: Optional[str] = None
    job_status: Optional[str] = None
    worker_id: Optional[str] = None
    duplicate: bool = False
    operation_count: int = 0
    clarification: Optional[Mapping[str, Any]] = None
    approval: Optional[Mapping[str, Any]] = None
    assumptions: tuple[str, ...] = ()
    facts_recorded: tuple[str, ...] = ()
    provider_name: Optional[str] = None
    failure: Optional[errors.ControlPlaneFailure] = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def http_status(self) -> int:
        if self.failure is not None:
            return self.failure.http_status
        return 202 if self.kind == PLAN else 200

    def snapshot(self, *, status_url: Optional[str] = None) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "request_id": self.request_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "job_id": self.job_id,
            "job_status": self.job_status,
            "worker_id": self.worker_id,
            "duplicate": self.duplicate,
            "operation_count": self.operation_count,
            "clarification": dict(self.clarification) if self.clarification else None,
            "approval": dict(self.approval) if self.approval else None,
            "assumptions": list(self.assumptions),
            "facts_recorded": list(self.facts_recorded),
            "provider": self.provider_name,
            "status_url": status_url,
        }


@dataclass
class DesignChatService:
    """Orchestrates one design conversation turn."""

    provider: DesignAgentProvider
    repositories: StudioRepositories
    context_builder: ContextBuilder
    projects: ProjectRegistry
    store: Any  # JobRecordStore
    selector: Any  # WorkerSelector
    offer_job: Callable[[str, dict], Any]
    clock: Callable[[], str] = field(default=lambda: __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat())
    #: Injected so a test can assert behaviour without a worker.
    blender_available: Callable[[], bool] = field(default=lambda: True)

    # -- the turn ----------------------------------------------------------
    def submit(
        self, request: DesignChatRequest, identity: TrustedIdentity
    ) -> DesignTurnResult:
        project = self.projects.get(request.project_id)
        if project is None:
            return self._failed(
                request,
                errors.failure(
                    errors.UNKNOWN_PROJECT, "VALIDATION_ERROR", "That project does not exist."
                ),
            )

        self.repositories.projects.ensure(project.project_id, project.display_name)

        # The user's own words are recorded first, so a crash mid-turn still leaves a
        # transcript that makes sense.
        self.repositories.conversation.append(
            ConversationTurnRecord(
                project_id=request.project_id,
                session_id=request.session_id,
                role="user",
                text=request.message,
                request_id=request.request_id,
            )
        )

        pending = self.repositories.clarifications.open_for_project(request.project_id)

        agent_input = self.context_builder.build(
            user_text=request.message,
            user_id=identity.user_id,
            project_id=request.project_id,
            session_id=request.session_id,
            attached_reference_ids=request.attached_reference_ids,
            selected_object_id=request.selected_object_id,
            scene=self._scene(request.project_id),
            blender_available=self.blender_available(),
        )

        outcome = self.provider.respond(agent_input)

        # Whatever else happened, remember what was learned.
        recorded = self._record_facts(request.project_id, outcome)

        if pending is not None and not isinstance(outcome, Clarification):
            # The agent moved on, so the question it asked has been answered.
            self.repositories.clarifications.resolve(
                request.project_id, pending.clarification_id
            )

        return self._dispatch(request, identity, outcome, recorded)

    def _dispatch(
        self,
        request: DesignChatRequest,
        identity: TrustedIdentity,
        outcome: AgentOutcome,
        recorded: tuple[str, ...],
    ) -> DesignTurnResult:
        if isinstance(outcome, AgentError):
            failure = errors.failure_from_error(outcome.error)
            return self._failed(request, failure, provider_name=outcome.metadata.provider_name)

        if isinstance(outcome, Answer):
            self._record_assistant(request, outcome.text)
            return DesignTurnResult(
                kind=ANSWER,
                message=outcome.text,
                request_id=request.request_id,
                project_id=request.project_id,
                session_id=request.session_id,
                assumptions=outcome.assumptions,
                facts_recorded=recorded,
                provider_name=outcome.metadata.provider_name,
            )

        if isinstance(outcome, Clarification):
            record = ClarificationRecord(
                clarification_id=new_request_scoped_id("clr"),
                project_id=request.project_id,
                session_id=request.session_id,
                question=outcome.question,
                missing_information=outcome.missing_information,
                request_id=request.request_id,
            )
            self.repositories.clarifications.add(record)
            self._record_assistant(request, outcome.question)
            return DesignTurnResult(
                kind=CLARIFICATION,
                message=outcome.question,
                request_id=request.request_id,
                project_id=request.project_id,
                session_id=request.session_id,
                clarification=record.snapshot(),
                assumptions=outcome.assumptions,
                facts_recorded=recorded,
                provider_name=outcome.metadata.provider_name,
            )

        if isinstance(outcome, ApprovalRequired):
            return self._park_for_approval(
                request,
                code=outcome.code,
                summary=outcome.summary,
                reasons=outcome.reasons,
                operations=outcome.operations,
                provider_name=outcome.metadata.provider_name,
                recorded=recorded,
            )

        assert isinstance(outcome, PlanProposal)
        return self._plan(request, identity, outcome, recorded)

    # -- plans -------------------------------------------------------------
    def _plan(
        self,
        request: DesignChatRequest,
        identity: TrustedIdentity,
        proposal: PlanProposal,
        recorded: tuple[str, ...],
    ) -> DesignTurnResult:
        if not self.blender_available():
            return self._failed(
                request,
                errors.failure(
                    errors.NO_READY_WORKER,
                    "BLENDER_UNAVAILABLE",
                    "The design machine is not connected, so nothing can be modelled yet.",
                ),
                provider_name=proposal.metadata.provider_name,
            )

        # Classify before any job exists. The worker cannot pause to ask a human, so the
        # question has to be raised here.
        flagged = self._first_step_needing_approval(proposal.operations)
        if flagged is not None:
            step, assessment = flagged
            code = step.code() or ""
            return self._park_for_approval(
                request,
                code=code,
                summary=assessment.summary(),
                reasons=assessment.reasons(),
                operations=proposal.operations,
                provider_name=proposal.metadata.provider_name,
                recorded=recorded,
            )

        refused = self._first_refused_step(proposal.operations)
        if refused is not None:
            return self._failed(
                request,
                errors.failure(
                    errors.INVALID_REQUEST,
                    "VALIDATION_ERROR",
                    "The assistant proposed a step that could not be understood, so "
                    "nothing was changed.",
                ),
                provider_name=proposal.metadata.provider_name,
            )

        return self._create_job(request, identity, proposal, recorded)

    def _create_job(
        self,
        request: DesignChatRequest,
        identity: TrustedIdentity,
        proposal: PlanProposal,
        recorded: tuple[str, ...],
        *,
        approvals: Mapping[int, str] = {},
    ) -> DesignTurnResult:
        operations = [
            CapabilityOperation(
                operation_index=index,
                capability=step.capability,
                label=step.label,
                arguments=dict(step.arguments),
                approval_token=approvals.get(index),
            )
            for index, step in enumerate(proposal.operations)
        ]

        created = create_capability_job(
            job_id=f"job_{request.request_id}_0",
            project_id=request.project_id,
            session_id=request.session_id,
            user_id=identity.user_id,
            request_id=request.request_id,
            operations=operations,
            created_at=self.clock(),
            required_scene_version=proposal.scene_version,
            summary=proposal.summary,
        )
        if not created.ok or created.job is None:
            first = created.errors[0] if created.errors else ChatError(
                code="VALIDATION_ERROR", message="The plan could not be prepared."
            )
            return self._failed(
                request,
                errors.failure_from_error(first),
                provider_name=proposal.metadata.provider_name,
            )

        job_wire = to_job_wire(created.job)
        record, duplicate = self.store.submit(record_from_job(job_wire))

        result = DesignTurnResult(
            kind=PLAN,
            message=proposal.summary,
            request_id=request.request_id,
            project_id=request.project_id,
            session_id=request.session_id,
            job_id=record.job_id,
            job_status=record.job_status,
            duplicate=duplicate,
            operation_count=len(operations),
            assumptions=proposal.assumptions,
            facts_recorded=recorded,
            provider_name=proposal.metadata.provider_name,
        )

        if duplicate and (record.is_terminal or record.worker_id):
            # Already running or finished: report it rather than offering it twice.
            result.worker_id = record.worker_id
            return result

        selection = self.selector.select(job_wire)
        if not selection.ok:
            self._record_assistant(request, proposal.summary)
            return self._failed(
                request,
                errors.failure(
                    errors.NO_READY_WORKER,
                    "BLENDER_UNAVAILABLE",
                    "The design machine is not connected, so nothing was changed.",
                ),
                provider_name=proposal.metadata.provider_name,
            )

        try:
            self.offer_job(selection.worker_id, job_wire)
        except Exception as error:  # protocol errors are reported, never raised at HTTP
            _log.exception("offering the capability job failed: %s", error)
            return self._failed(
                request,
                errors.failure(
                    errors.INTERNAL, "INTERNAL_ERROR", "The plan could not be dispatched."
                ),
                provider_name=proposal.metadata.provider_name,
            )

        record.worker_id = selection.worker_id
        record.offer_count += 1
        record.touch()
        self.store.save(record)

        result.worker_id = selection.worker_id
        self._record_assistant(request, proposal.summary)
        return result

    # -- approvals ---------------------------------------------------------
    def _park_for_approval(
        self,
        request: DesignChatRequest,
        *,
        code: str,
        summary: str,
        reasons: Sequence[str],
        operations: Sequence[ValidatedOperation],
        provider_name: Optional[str],
        recorded: tuple[str, ...],
    ) -> DesignTurnResult:
        record = ApprovalRecord(
            approval_id=new_request_scoped_id("apr"),
            project_id=request.project_id,
            session_id=request.session_id,
            code=code,
            summary=summary,
            reasons=tuple(reasons),
            operations=tuple(
                {
                    "operation_index": index,
                    "capability": step.capability,
                    "label": step.label,
                    "arguments": dict(step.arguments),
                }
                for index, step in enumerate(operations)
            ),
            request_id=request.request_id,
        )
        self.repositories.approvals.add(record)

        message = (
            "Before I continue I need your approval for one step. " + summary
        )
        self._record_assistant(request, message)
        return DesignTurnResult(
            kind=APPROVAL_REQUIRED,
            message=message,
            request_id=request.request_id,
            project_id=request.project_id,
            session_id=request.session_id,
            approval=record.snapshot(),
            facts_recorded=recorded,
            provider_name=provider_name,
        )

    def decide_approval(
        self,
        project_id: str,
        approval_id: str,
        *,
        approved: bool,
        identity: TrustedIdentity,
        session_id: Optional[str] = None,
    ) -> DesignTurnResult:
        """Record the user's decision, and run the plan if they approved it."""
        record = self.repositories.approvals.get(project_id, approval_id)
        if record is None:
            return DesignTurnResult(
                kind=ERROR,
                message="That approval request no longer exists.",
                request_id=approval_id,
                project_id=project_id,
                session_id=session_id or "",
                failure=errors.failure(
                    errors.UNKNOWN_PROJECT, "VALIDATION_ERROR", "Unknown approval request."
                ),
            )

        decided = self.repositories.approvals.decide(project_id, approval_id, approved)
        if decided is None:
            return DesignTurnResult(
                kind=ERROR,
                message="That request has already been decided.",
                request_id=approval_id,
                project_id=project_id,
                session_id=record.session_id,
                failure=errors.failure(
                    errors.JOB_CONFLICT,
                    "PRECONDITION_MISMATCH",
                    "That request has already been decided.",
                ),
            )

        request = DesignChatRequest(
            # A distinct request id, because approving is a new intention. Reusing the
            # original would make the approved run look like a retry of the parked one.
            request_id=f"{record.request_id or approval_id}_approved",
            project_id=project_id,
            session_id=record.session_id,
            message="(approved)",
        )

        if not approved:
            message = "Understood — I did not run that step."
            self.repositories.conversation.append(
                ConversationTurnRecord(
                    project_id=project_id,
                    session_id=record.session_id,
                    role="assistant",
                    text=message,
                )
            )
            return DesignTurnResult(
                kind=ANSWER,
                message=message,
                request_id=request.request_id,
                project_id=project_id,
                session_id=record.session_id,
                approval=decided.snapshot(),
            )

        from studio_agent.outcome import ProviderMetadata

        operations = tuple(
            ValidatedOperation(
                capability=str(entry.get("capability")),
                arguments=dict(entry.get("arguments") or {}),
                label=str(entry.get("label") or ""),
                operation_index=int(entry.get("operation_index") or 0),
            )
            for entry in decided.operations
        )
        proposal = PlanProposal(
            summary="Applying the approved changes.",
            operations=operations,
            metadata=ProviderMetadata(
                provider_name=self.provider.name, provider_version=self.provider.version
            ),
            scene_version=self.repositories.scenes.version(project_id),
        )

        approvals = {
            index: _approval_token(step.code() or "")
            for index, step in enumerate(operations)
            if step.is_model_authored_code
        }
        return self._create_job(request, identity, proposal, (), approvals=approvals)

    # -- helpers -----------------------------------------------------------
    def _first_step_needing_approval(
        self, operations: Sequence[ValidatedOperation]
    ) -> Optional[tuple[ValidatedOperation, code_risk.RiskAssessment]]:
        for step in operations:
            if not step.is_model_authored_code:
                continue
            assessment = code_risk.classify_python(step.code() or "")
            if assessment.requires_approval:
                return step, assessment
        return None

    def _first_refused_step(
        self, operations: Sequence[ValidatedOperation]
    ) -> Optional[ValidatedOperation]:
        for step in operations:
            if not step.is_model_authored_code:
                continue
            if code_risk.classify_python(step.code() or "").refused:
                return step
        return None

    def _scene(self, project_id: str) -> Optional[SceneSnapshot]:
        raw = self.repositories.scenes.get(project_id)
        if raw is None:
            return None
        try:
            return _snapshot_from_wire(raw)
        except (KeyError, TypeError, ValueError) as error:
            _log.warning("cached scene for %s is unusable: %s", project_id, error)
            return None

    def _record_facts(self, project_id: str, outcome: AgentOutcome) -> tuple[str, ...]:
        facts = getattr(outcome, "design_facts", ()) or ()
        recorded: list[str] = []
        for key, value in facts:
            self.repositories.facts.set(project_id, key, value, source="agent")
            recorded.append(key)
        return tuple(recorded)

    def _record_assistant(self, request: DesignChatRequest, text: str) -> None:
        self.repositories.conversation.append(
            ConversationTurnRecord(
                project_id=request.project_id,
                session_id=request.session_id,
                role="assistant",
                text=text,
                request_id=request.request_id,
            )
        )

    def _failed(
        self,
        request: DesignChatRequest,
        failure: errors.ControlPlaneFailure,
        *,
        provider_name: Optional[str] = None,
    ) -> DesignTurnResult:
        return DesignTurnResult(
            kind=ERROR,
            message=failure.message,
            request_id=request.request_id,
            project_id=request.project_id,
            session_id=request.session_id,
            failure=failure,
            provider_name=provider_name,
        )


def _approval_token(code: str) -> str:
    import hashlib

    return f"approved:{hashlib.sha256(code.encode('utf-8')).hexdigest()}"


def _snapshot_from_wire(wire: Mapping[str, Any]) -> SceneSnapshot:
    """Rebuild a snapshot from its stored wire document."""
    from studio_types import (
        EulerRadians,
        MaterialColor,
        MaterialSummary,
        Scale3,
        SceneObject,
        SceneUnits,
        Vec3,
    )

    def vec(raw: Mapping[str, Any]) -> Vec3:
        return Vec3(x=float(raw["x"]), y=float(raw["y"]), z=float(raw["z"]))

    objects = []
    for entry in wire.get("objects") or ():
        material = None
        raw_material = entry.get("material")
        if isinstance(raw_material, Mapping):
            base = raw_material.get("base_color")
            colour = None
            if isinstance(base, Mapping):
                colour = MaterialColor(
                    r=float(base["r"]), g=float(base["g"]), b=float(base["b"]), a=float(base["a"])
                )
            material = MaterialSummary(name=str(raw_material["name"]), base_color=colour)
        rotation = entry["rotation_euler_radians"]
        scale = entry["scale"]
        objects.append(
            SceneObject(
                name=str(entry["name"]),
                object_type=str(entry["object_type"]),
                world_position_meters=vec(entry["world_position_meters"]),
                dimensions_meters=vec(entry["dimensions_meters"]),
                rotation_euler_radians=EulerRadians(
                    x=float(rotation["x"]), y=float(rotation["y"]), z=float(rotation["z"])
                ),
                scale=Scale3(
                    x=float(scale["x"]), y=float(scale["y"]), z=float(scale["z"])
                ),
                visible=bool(entry["visible"]),
                studio_object_id=entry.get("studio_object_id"),
                material=material,
            )
        )

    units = wire["units"]
    return SceneSnapshot(
        project_id=str(wire["project_id"]),
        scene_version=str(wire["scene_version"]),
        units=SceneUnits(
            unit_system=str(units["unit_system"]),
            length_unit=str(units["length_unit"]),
            scale_length=float(units["scale_length"]),
        ),
        objects=tuple(objects),
        captured_at=str(wire["captured_at"]),
    )
