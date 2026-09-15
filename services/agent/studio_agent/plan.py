"""The provider-neutral semantic plan.

Spec 001, Task 7.

AgentPlan is the boundary between probabilistic language interpretation and
deterministic execution::

    language (any provider)  ->  AgentPlan  ->  canonical Job  ->  worker  ->  Blender
                             ^^^^^^^^^^^^^
                             everything downstream is deterministic

What an AgentPlan contains
--------------------------
Resolved canonical semantics only: an operation type and a typed payload in
canonical meters. It deliberately does NOT contain:

  - unresolved language such as "right" or "50 cm"
  - provider-specific tool calls or response envelopes
  - chain-of-thought or any private model reasoning
  - identity (project_id / session_id / user_id) — that comes from AgentContext,
    so a provider cannot name its own project

Multi-operation shape
---------------------
``operations`` is an ordered tuple, and a operation's index in that tuple IS its
``operation_index`` for job idempotency. Spec 001 produces exactly one operation,
but nothing here assumes that permanently. Dependency graphs and conditional
planning are explicitly out of scope.

Why no JSON Schema
------------------
AgentPlan is an internal Python boundary: the provider and the job factory are
both Python and currently in-process, and it never crosses to the browser (the
API returns ChatResponse). Giving it a canonical schema and a TypeScript mirror
would add contract machinery with no second consumer. If a provider is ever
hosted out-of-process, this becomes a genuine wire contract and should get the
full canonical schema treatment then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from studio_contracts import ChatError
from studio_types import JobType


@dataclass(frozen=True)
class PlannedOperation:
    """One resolved semantic operation.

    ``operation_type`` discriminates ``payload``, mirroring the canonical Job so
    the mapping to a Job is mechanical rather than interpretive. For
    ``move_object`` the payload is a ``MoveObjectPayload`` (target +
    delta_meters), already in canonical meters.
    """

    operation_type: JobType
    payload: Any


@dataclass(frozen=True)
class AgentPlan:
    """An ordered set of operations resolved from one request."""

    operations: tuple[PlannedOperation, ...] = field(default_factory=tuple)

    def __len__(self) -> int:
        return len(self.operations)

    @property
    def operation_count(self) -> int:
        return len(self.operations)

    def indexed(self) -> tuple[tuple[int, PlannedOperation], ...]:
        """Operations paired with the operation_index they will use in a Job."""
        return tuple(enumerate(self.operations))


@dataclass(frozen=True)
class ProviderMetadata:
    """Safe, non-sensitive information about the interpretation.

    Deliberately excludes any model reasoning. If a future provider returns tool
    traces, those are a separate concern from private chain-of-thought and must
    not be smuggled in here.
    """

    provider_name: str
    provider_version: str
    operation_count: int = 0


@dataclass(frozen=True)
class AgentResult:
    """The outcome of one interpretation attempt.

    Structured rather than exception-based: callers branch on ``ok`` and
    ``error.code``, never on a parsed message string.
    """

    ok: bool
    metadata: ProviderMetadata
    plan: Optional[AgentPlan] = None
    error: Optional[ChatError] = None

    @classmethod
    def success(cls, plan: AgentPlan, metadata: ProviderMetadata) -> "AgentResult":
        return cls(
            ok=True,
            plan=plan,
            metadata=ProviderMetadata(
                provider_name=metadata.provider_name,
                provider_version=metadata.provider_version,
                operation_count=plan.operation_count,
            ),
        )

    @classmethod
    def failure(
        cls, code: str, message: str, metadata: ProviderMetadata
    ) -> "AgentResult":
        return cls(
            ok=False,
            error=ChatError(code=code, message=message),
            metadata=ProviderMetadata(
                provider_name=metadata.provider_name,
                provider_version=metadata.provider_version,
                operation_count=0,
            ),
        )
