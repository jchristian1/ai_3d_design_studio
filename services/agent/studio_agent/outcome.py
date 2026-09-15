"""``AgentOutcome`` — what one agent turn can produce.

Spec 001 had a boolean ``AgentResult``: a plan, or a failure. The MVP needs four more
honest answers, and each is a first-class outcome rather than an error:

* :class:`Answer` — a grounded reply. Zero mutations.
* :class:`Clarification` — required information is missing. Zero mutations, and the
  question is a question rather than an error.
* :class:`ApprovalRequired` — the agent wants to run code that reaches beyond the scene.
  Zero mutations until the user decides.
* :class:`PlanProposal` — validated operations, ready to become jobs.
* :class:`AgentError` — something went wrong, structurally.

The important invariant: **only a PlanProposal can create mutation jobs.** Everything
else is terminal for the turn, which is what makes "a clarification blocks all modelling"
true by construction rather than by discipline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

from studio_contracts import ChatError


@dataclass(frozen=True)
class ProviderMetadata:
    """Non-sensitive provenance. Never carries prompts or reasoning."""

    provider_name: str
    provider_version: str
    operation_count: int = 0
    model: Optional[str] = None


@dataclass(frozen=True)
class ValidatedOperation:
    """One capability invocation the platform has validated and will execute."""

    capability: str
    arguments: Mapping[str, Any]
    #: Human-readable progress label. Contains no path or internal identifier.
    label: str
    operation_index: int = 0

    @property
    def is_model_authored_code(self) -> bool:
        return self.capability == "execute_blender_python"

    def code(self) -> Optional[str]:
        value = self.arguments.get("code")
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class Answer:
    """A grounded reply that changes nothing."""

    text: str
    metadata: ProviderMetadata
    #: Facts worth remembering, learned this turn. Persisted as project memory.
    design_facts: tuple[tuple[str, str], ...] = ()
    #: Unconfirmed assumptions, surfaced so the user can correct them.
    assumptions: tuple[str, ...] = ()

    kind: str = "answer"


@dataclass(frozen=True)
class Clarification:
    """Required information is missing, so nothing may be modified."""

    question: str
    metadata: ProviderMetadata
    #: Machine-readable names of the missing facts, for example ``ceiling_height_m``.
    missing_information: tuple[str, ...] = ()
    #: Candidate objects, when the ambiguity is "which one".
    options: tuple[str, ...] = ()
    design_facts: tuple[tuple[str, str], ...] = ()
    assumptions: tuple[str, ...] = ()

    kind: str = "clarification"


@dataclass(frozen=True)
class ApprovalRequired:
    """The agent proposed code that reaches beyond the scene."""

    #: Plain-language explanation of why this needs a decision.
    summary: str
    #: The ACTUAL code, shown to the user. Seeing it is the entire point.
    code: str
    #: One reason per finding, each already carrying a line number.
    reasons: tuple[str, ...]
    metadata: ProviderMetadata
    #: The operations to run once approved, including the flagged one.
    operations: tuple[ValidatedOperation, ...] = ()
    message: str = ""

    kind: str = "approval_required"


@dataclass(frozen=True)
class PlanProposal:
    """Validated operations, ready to become canonical jobs."""

    summary: str
    operations: tuple[ValidatedOperation, ...]
    metadata: ProviderMetadata
    #: The scene version the plan was reasoned against, when a scene was available.
    scene_version: Optional[str] = None
    design_facts: tuple[tuple[str, str], ...] = ()
    assumptions: tuple[str, ...] = ()

    kind: str = "plan"

    @property
    def operation_count(self) -> int:
        return len(self.operations)


@dataclass(frozen=True)
class AgentError:
    """A structured failure. Never a fabricated plan."""

    error: ChatError
    metadata: ProviderMetadata

    kind: str = "error"

    @property
    def code(self) -> str:
        return self.error.code

    @property
    def message(self) -> str:
        return self.error.message


AgentOutcome = Union[Answer, Clarification, ApprovalRequired, PlanProposal, AgentError]

#: Outcomes that must not produce a single mutation job.
NON_MUTATING_KINDS = ("answer", "clarification", "approval_required", "error")


def creates_mutations(outcome: AgentOutcome) -> bool:
    """Only a plan may mutate the project."""
    return isinstance(outcome, PlanProposal) and outcome.operation_count > 0


def error_outcome(
    code: str, message: str, metadata: ProviderMetadata
) -> AgentError:
    return AgentError(error=ChatError(code=code, message=message), metadata=metadata)
