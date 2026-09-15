"""AgentProvider implementations.

    AgentProvider              (studio_agent.provider — the abstraction)
    ├── RuleBasedProvider      deterministic, Spec 001 only, TEMPORARY
    ├── AstraProvider          boundary defined, not implemented
    └── CodexProvider          boundary defined, not implemented

Callers should depend on ``studio_agent.provider.AgentProvider`` and obtain an
instance through ``studio_agent.providers.registry.get_provider``, not by
importing a concrete class.
"""

from .astra import AstraProvider
from .codex import CodexProvider
from .rule_based import RuleBasedProvider

__all__ = ["AstraProvider", "CodexProvider", "RuleBasedProvider"]
