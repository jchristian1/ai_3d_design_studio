"""``FakeLlmProvider`` — a scripted design provider for offline tests.

This is the model-side counterpart to ``FakeBlenderCapabilityProvider``. Together they
let the entire pipeline — context, validation, jobs, durability, orchestration, routes,
browser — be tested with no network, no Codex and no Blender.

Two ways to script it:

* ``queue_response`` pushes a RAW response body, so the real validation path runs. Use
  this to test malformed output, unknown capabilities and injection attempts.
* ``queue_outcome`` pushes an already-built outcome, for when the test is about what
  happens downstream rather than about parsing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from studio_contracts import ChatError

from ..agent_input import AgentInput
from ..outcome import (
    AgentError,
    AgentOutcome,
    Answer,
    ProviderMetadata,
)
from ..proposal import parse_agent_response

PROVIDER_NAME = "fake_llm"
PROVIDER_VERSION = "test.1"


def response_body(
    kind: str,
    *,
    message: str = "",
    question: str = "",
    missing_information: Sequence[str] = (),
    operations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a schema-shaped response body, so tests read like the contract."""
    return {
        "kind": kind,
        "message": message,
        "question": question,
        "missing_information": list(missing_information),
        "operations": [dict(operation) for operation in operations],
    }


def operation(
    capability: str, arguments: Mapping[str, Any], *, label: str = ""
) -> dict[str, Any]:
    """Build one operation entry, encoding arguments the way the contract requires."""
    return {
        "capability": capability,
        "label": label or capability.replace("_", " "),
        "arguments_json": json.dumps(dict(arguments)),
    }


@dataclass
class FakeLlmProvider:
    """A deterministic stand-in for a real model."""

    name: str = PROVIDER_NAME
    version: str = PROVIDER_VERSION
    model: Optional[str] = "fake-model"

    #: Raw response bodies, consumed in order.
    responses: list[Any] = field(default_factory=list)
    #: Pre-built outcomes, consumed in order, taking precedence over responses.
    outcomes: list[AgentOutcome] = field(default_factory=list)
    #: Every input the provider was given, for assertions about context.
    seen: list[AgentInput] = field(default_factory=list)
    #: When set, every call fails with this message.
    unavailable: Optional[str] = None

    def queue_response(self, body: Any) -> "FakeLlmProvider":
        self.responses.append(body)
        return self

    def queue_outcome(self, outcome: AgentOutcome) -> "FakeLlmProvider":
        self.outcomes.append(outcome)
        return self

    def queue_answer(self, text: str) -> "FakeLlmProvider":
        return self.queue_response(response_body("answer", message=text))

    def queue_clarification(
        self, question: str, missing: Sequence[str] = ()
    ) -> "FakeLlmProvider":
        return self.queue_response(
            response_body("clarification", question=question, missing_information=missing)
        )

    def queue_plan(
        self, operations: Sequence[Mapping[str, Any]], *, message: str = "Working on it."
    ) -> "FakeLlmProvider":
        return self.queue_response(
            response_body("plan", message=message, operations=operations)
        )

    def _metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_name=self.name,
            provider_version=self.version,
            model=self.model,
        )

    def respond(self, agent_input: AgentInput) -> AgentOutcome:
        self.seen.append(agent_input)
        metadata = self._metadata()

        if self.unavailable is not None:
            return AgentError(
                error=ChatError(code="PROVIDER_UNAVAILABLE", message=self.unavailable),
                metadata=metadata,
            )

        if self.outcomes:
            return self.outcomes.pop(0)

        if not self.responses:
            return Answer(
                text="No scripted response was queued for this turn.", metadata=metadata
            )

        scene_version = agent_input.scene.scene_version if agent_input.scene else None
        return parse_agent_response(
            self.responses.pop(0), metadata, scene_version=scene_version
        )

    @property
    def last_input(self) -> Optional[AgentInput]:
        return self.seen[-1] if self.seen else None
