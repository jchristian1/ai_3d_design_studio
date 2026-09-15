"""The AgentProvider abstraction.

Spec 001, Task 7.

    AgentProvider              <- the ONLY thing the rest of the platform imports
    ├── RuleBasedProvider      deterministic, Spec 001 only, temporary
    ├── AstraProvider          boundary defined, not implemented
    └── CodexProvider          boundary defined, not implemented

Nothing outside ``studio_agent.providers`` should import a concrete provider. The
API, the job factory, and future orchestration depend on this Protocol, so
swapping the language engine is a configuration change rather than a refactor.

What a provider may and may not do
---------------------------------
A provider interprets language into a canonical AgentPlan. It must not mutate
Blender, import bpy, call the worker, touch project files, manage locks, create
recovery copies, or execute MCP tools. Those responsibilities live downstream and
are deliberately unreachable from here — enforced by tests that inspect the
provider modules' imports.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .context import AgentContext
from .plan import AgentResult


class AgentProviderError(RuntimeError):
    """Raised only for programming errors, never for interpretation outcomes.

    Interpretation failures — unsupported language, bad units, an unconfigured
    provider — are returned as a structured ``AgentResult``, so callers never have
    to catch exceptions to handle ordinary outcomes.
    """


@runtime_checkable
class AgentProvider(Protocol):
    """Turns a natural-language request into a canonical AgentPlan."""

    #: Stable provider identifier, e.g. "rule_based".
    name: str
    #: Version or configuration identifier, safe to log and expose.
    version: str

    def interpret(self, request: object, context: AgentContext) -> AgentResult:
        """Interpret one ChatRequest against trusted context.

        Must not raise for expected failures: every outcome, including "I cannot
        interpret this", is a structured AgentResult.

        ``request`` is a ChatRequest (mapping or dataclass). Identity is taken
        from ``context``, never from the request body or the message text.
        """
        ...
