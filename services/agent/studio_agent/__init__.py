"""Agent orchestration for AI 3D Design Studio.

Spec 001, Task 7.

    ChatRequest
        v
    AgentProvider          <- the replaceable AI boundary
        v
    AgentPlan              <- provider-neutral canonical semantics
        v
    JobFactory
        v
    canonical Job          <- deterministic from here on
        v
    Blender Worker

Modules:
    provider.py    the AgentProvider Protocol (import this, not a concrete class)
    providers/     RuleBasedProvider, AstraProvider, CodexProvider, registry
    context.py     AgentContext — trusted identity from the control plane
    plan.py        AgentPlan / PlannedOperation / AgentResult
    job_factory.py AgentPlan -> canonical Job via the Task 3 builder

RuleBasedProvider is TEMPORARY: it exists to prove the architecture end to end and
to keep CI/E2E deterministic. It is not the intended long-term language engine.
"""

from .context import AgentContext
from .job_factory import JobCreationResult, JobFactory
from .plan import (
    AgentPlan,
    AgentResult,
    PlannedOperation,
    ProviderMetadata,
)
from .provider import AgentProvider, AgentProviderError

__all__ = [
    "AgentContext",
    "AgentPlan",
    "AgentProvider",
    "AgentProviderError",
    "AgentResult",
    "JobCreationResult",
    "JobFactory",
    "PlannedOperation",
    "ProviderMetadata",
]
