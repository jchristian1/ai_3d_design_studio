"""Provider selection.

The one place that knows which concrete providers exist, so the rest of the platform
never imports a concrete provider class. Selection is configuration
(``STUDIO_API_AGENT_PROVIDER``), never code.

Two generations of provider coexist:

* Spec 001 :class:`AgentProvider` implementations, which interpret one sentence.
* :class:`DesignAgentProvider` implementations, which reason about a whole project turn.

:func:`get_design_provider` returns the second kind, wrapping the first kind in
:class:`LegacyProviderAdapter` when needed. That is what lets ``rule_based`` remain
selectable — and the Spec 001 regression suite remain green — while Astra is the default
for the design workspace.
"""

from __future__ import annotations

from typing import Callable

from ..design_provider import DesignAgentProvider, LegacyProviderAdapter
from ..provider import AgentProvider, AgentProviderError
from .astra import AstraProvider
from .codex import CodexProvider
from .codex_astra import CodexAstraProvider
from .fake_llm import FakeLlmProvider
from .rule_based import RuleBasedProvider

#: Deterministic, so Spec 001's CI and E2E runs stay reproducible.
DEFAULT_PROVIDER_NAME = "rule_based"

#: What the design workspace uses unless configured otherwise.
DEFAULT_DESIGN_PROVIDER_NAME = "codex_astra"

_FACTORIES: dict[str, Callable[[], AgentProvider]] = {
    "rule_based": RuleBasedProvider,
    "astra": AstraProvider,
    "codex": CodexProvider,
}

_DESIGN_FACTORIES: dict[str, Callable[[], DesignAgentProvider]] = {
    "codex_astra": CodexAstraProvider,
    "fake_llm": FakeLlmProvider,
}


def available_provider_names() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def available_design_provider_names() -> tuple[str, ...]:
    return tuple(sorted(set(_DESIGN_FACTORIES) | set(_FACTORIES)))


def get_provider(name: str = DEFAULT_PROVIDER_NAME) -> AgentProvider:
    """Return a Spec 001 provider by name.

    Raises AgentProviderError for an unknown name — that is a configuration mistake by an
    operator, not an interpretation outcome, so it is not an AgentResult.
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise AgentProviderError(
            f"unknown agent provider {name!r}; available: "
            f"{', '.join(available_provider_names())}"
        )
    return factory()


def get_design_provider(
    name: str = DEFAULT_DESIGN_PROVIDER_NAME,
) -> DesignAgentProvider:
    """Return a design provider by name, adapting a legacy provider when needed."""
    design_factory = _DESIGN_FACTORIES.get(name)
    if design_factory is not None:
        return design_factory()

    legacy_factory = _FACTORIES.get(name)
    if legacy_factory is None:
        raise AgentProviderError(
            f"unknown agent provider {name!r}; available: "
            f"{', '.join(available_design_provider_names())}"
        )
    return LegacyProviderAdapter(inner=legacy_factory())
