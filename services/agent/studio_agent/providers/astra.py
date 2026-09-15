"""AstraProvider — adapter boundary, NOT implemented.

Spec 001, Task 7.

This file exists so the shape of a real model-backed provider is fixed and so
wiring one up later is an additive change rather than a refactor. It deliberately
does not work: there is no HTTP client, no API key handling, no prompt, and no
fabricated response.

Calling ``interpret`` returns a structured PROVIDER_UNAVAILABLE result. It never
pretends to have interpreted anything, because a provider that silently returned
an empty or invented plan would be far worse than one that fails loudly.

When implemented, the adapter's responsibilities will be:
  - build context from AgentContext (system rules, project brief, constraints,
    decisions, scene snapshot, session summary, recent conversation)
  - call the Astra API, reading credentials from environment variables only
    (see .kiro/steering/security.md — never committed)
  - translate the provider's tool calls into canonical PlannedOperations
  - surface a structured error for anything it cannot resolve

What it must NOT do, now or later: touch Blender, import bpy, call the worker,
read project files, or execute MCP tools. Those boundaries are downstream.
"""

from __future__ import annotations

from typing import Any, Optional

from ..context import AgentContext
from ..plan import AgentResult, ProviderMetadata

PROVIDER_NAME = "astra"
PROVIDER_VERSION = "unconfigured"

NOT_CONFIGURED_MESSAGE = (
    "the Astra provider is not configured in this deployment; no interpretation "
    "was attempted"
)


class AstraProvider:
    """Placeholder adapter for the Astra model provider."""

    name = PROVIDER_NAME
    version = PROVIDER_VERSION

    def __init__(self, api_key: Optional[str] = None) -> None:
        # Accepted for interface completeness only. Nothing is validated or used
        # yet, and no network client is created.
        self._api_key = api_key

    @property
    def configured(self) -> bool:
        """Always False: the adapter is not implemented."""
        return False

    def interpret(self, request: Any, context: AgentContext) -> AgentResult:
        return AgentResult.failure(
            "PROVIDER_UNAVAILABLE",
            NOT_CONFIGURED_MESSAGE,
            ProviderMetadata(provider_name=self.name, provider_version=self.version),
        )
