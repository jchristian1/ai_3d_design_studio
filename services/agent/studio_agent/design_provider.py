"""``DesignAgentProvider`` — the richer provider interface the MVP needs.

Spec 001's :class:`~studio_agent.provider.AgentProvider` takes a chat request and returns
a boolean-ish result. The design workspace needs more on both sides: multimodal input and
project context going in, and five possible outcomes coming out.

Rather than break Spec 001, this adds a second protocol and an adapter. ``RuleBasedProvider``
keeps working unchanged behind :class:`LegacyProviderAdapter`, which is what lets the Spec
001 regression suite stay green while the new path is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from studio_contracts import ChatRequest

from .agent_input import AgentInput
from .context import AgentContext
from .outcome import (
    AgentError,
    AgentOutcome,
    Answer,
    PlanProposal,
    ProviderMetadata,
    ValidatedOperation,
)
from .provider import AgentProvider, AgentProviderError


@runtime_checkable
class DesignAgentProvider(Protocol):
    """A provider that can reason about a project, not just a sentence."""

    name: str
    version: str

    def respond(self, agent_input: AgentInput) -> AgentOutcome:
        """Produce exactly one outcome for one turn."""
        ...


@dataclass
class LegacyProviderAdapter:
    """Expose a Spec 001 ``AgentProvider`` through the new interface.

    The translation is deliberately narrow: the legacy providers only ever emit
    ``move_object`` plans, so anything else they could produce would be a surprise worth
    surfacing rather than silently mapping.
    """

    inner: AgentProvider

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def version(self) -> str:
        return self.inner.version

    def respond(self, agent_input: AgentInput) -> AgentOutcome:
        request = ChatRequest(
            request_id="adapter",
            project_id=agent_input.project_id,
            session_id=agent_input.session_id,
            message=agent_input.user_text,
        )
        context = AgentContext(
            user_id=agent_input.user_id,
            project_id=agent_input.project_id,
            session_id=agent_input.session_id,
            selected_object_id=agent_input.selected_object_id,
        )
        metadata = ProviderMetadata(
            provider_name=self.inner.name, provider_version=self.inner.version
        )
        try:
            result = self.inner.interpret(request, context)
        except AgentProviderError as error:
            return AgentError(
                error=_chat_error("PROVIDER_UNAVAILABLE", str(error)), metadata=metadata
            )

        if not result.ok or result.plan is None:
            error = result.error
            code = error.code if error else "UNSUPPORTED_INSTRUCTION"
            message = error.message if error else "That instruction is not supported."
            if code == "UNSUPPORTED_INSTRUCTION":
                # A rule-based provider not recognising a sentence is not a failure of
                # the system; it is the honest answer "I cannot do that".
                return Answer(text=message, metadata=metadata)
            return AgentError(error=_chat_error(code, message), metadata=metadata)

        operations = tuple(
            ValidatedOperation(
                capability=str(operation.operation_type),
                arguments=_legacy_arguments(operation.payload),
                label=str(operation.operation_type).replace("_", " "),
                operation_index=index,
            )
            for index, operation in enumerate(result.plan.operations)
        )
        return PlanProposal(
            summary="Applying the requested change.",
            operations=operations,
            metadata=ProviderMetadata(
                provider_name=self.inner.name,
                provider_version=self.inner.version,
                operation_count=len(operations),
            ),
        )


def _chat_error(code: str, message: str):
    from studio_contracts import ChatError

    return ChatError(code=code, message=message)


def _legacy_arguments(payload: object) -> dict[str, object]:
    """Convert a Spec 001 payload dataclass into capability arguments."""
    target = getattr(payload, "target", None)
    delta = getattr(payload, "delta_meters", None)
    arguments: dict[str, object] = {}
    if target is not None:
        object_id = getattr(target, "object_id", None)
        name = getattr(target, "name", None)
        if object_id:
            arguments["object_id"] = object_id
        if name:
            arguments["name"] = name
    if delta is not None:
        # Spec 001 carries a delta; the capability layer wants an absolute target. The
        # worker resolves this against the observed position, so the delta is passed
        # through under its own key rather than being guessed at here.
        arguments["delta_meters"] = {
            "x": getattr(delta, "x", 0.0),
            "y": getattr(delta, "y", 0.0),
            "z": getattr(delta, "z", 0.0),
        }
    return arguments
