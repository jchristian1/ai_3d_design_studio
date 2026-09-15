"""Provider selection.

Spec 001, Task 7. The one place that knows which concrete providers exist, so the
rest of the platform never imports a concrete provider class.
"""

from __future__ import annotations

from typing import Callable

from ..provider import AgentProvider, AgentProviderError
from .astra import AstraProvider
from .codex import CodexProvider
from .rule_based import RuleBasedProvider

#: The default for Spec 001: deterministic, so CI and E2E runs are reproducible.
DEFAULT_PROVIDER_NAME = "rule_based"

_FACTORIES: dict[str, Callable[[], AgentProvider]] = {
    "rule_based": RuleBasedProvider,
    "astra": AstraProvider,
    "codex": CodexProvider,
}


def available_provider_names() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def get_provider(name: str = DEFAULT_PROVIDER_NAME) -> AgentProvider:
    """Return a provider by name.

    Raises AgentProviderError for an unknown name — that is a configuration
    mistake by an operator, not an interpretation outcome, so it is not an
    AgentResult.
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise AgentProviderError(
            f"unknown agent provider {name!r}; available: "
            f"{', '.join(available_provider_names())}"
        )
    return factory()
