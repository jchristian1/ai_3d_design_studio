"""RuleBasedProvider — deterministic interpretation for Spec 001.

TEMPORARY BY DESIGN. This is not the intended long-term language engine. It exists
so the whole vertical slice can be proven end to end, and so CI and E2E runs are
byte-for-byte reproducible without calling a model API. Broad natural language is
the job of AstraProvider / CodexProvider; this provider deliberately understands
almost nothing.

The grammar
-----------
One sentence shape, matched case-insensitively::

    move <target> <number> <unit> [to the|to|toward the] <direction>

  target      a single bare word, used verbatim as an object name ("Cube")
  number      a non-negative integer or decimal ("50", "0.5")
  unit        cm | centimeter(s) | centimetre(s) | m | meter(s) | metre(s)
  direction   right | left | up | down | forward(s) | back(ward(s))
  filler      an optional leading "please", a trailing "."

Anything else is refused with UNSUPPORTED_INSTRUCTION. There is no fuzzy matching,
no pronoun resolution, no landmark reasoning ("toward the window"), and no
guessing at vague magnitudes ("a little"). Refusing is the correct behaviour: a
wrong guess would silently mutate a user's design.

Division of labour
------------------
This module converts *words* into canonical tokens. It performs no arithmetic and
owns no spatial knowledge: ``packages/spatial`` resolves "right" to world +X and
"50 cm" to 0.50 m. Duplicating that math here would create a second source of
truth for the most safety-critical conversion in the system.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from studio_spatial import direction_delta_meters
from studio_types import MoveObjectPayload, ObjectRef

from ..context import AgentContext
from ..plan import AgentPlan, AgentResult, PlannedOperation, ProviderMetadata

PROVIDER_NAME = "rule_based"
PROVIDER_VERSION = "spec001.1"

#: Words that map onto a canonical Direction token from packages/spatial.
#: Only synonyms of the six world-space directions; nothing view-relative.
DIRECTION_WORDS: dict[str, str] = {
    "right": "right",
    "left": "left",
    "up": "up",
    "upward": "up",
    "upwards": "up",
    "down": "down",
    "downward": "down",
    "downwards": "down",
    "forward": "forward",
    "forwards": "forward",
    "back": "back",
    "backward": "back",
    "backwards": "back",
}

#: Words that map onto a canonical LengthUnit from packages/spatial.
UNIT_WORDS: dict[str, str] = {
    "cm": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "centimetre": "cm",
    "centimetres": "cm",
    "m": "m",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
}

MESSAGES: dict[str, str] = {
    "NOT_A_MOVE": (
        "I can only handle move commands of the form "
        "'move <object> <distance> <unit> to the <direction>', "
        "for example 'Move Cube 50 cm to the right'."
    ),
    "UNKNOWN_DIRECTION": (
        "I do not understand that direction. Supported directions are: "
        "right, left, up, down, forward, back. Directions relative to a camera "
        "or to another object are not supported yet."
    ),
    "UNKNOWN_UNIT": (
        "I do not understand that unit. Supported units are centimeters (cm) "
        "and meters (m)."
    ),
    "MISSING_MESSAGE": "the request has no message to interpret",
    "MALFORMED_REQUEST": "the request could not be read",
}

#: The single supported sentence shape.
_MOVE_PATTERN = re.compile(
    r"""
    ^\s*(?:please\s+)?
    move\s+
    (?P<target>[A-Za-z][\w.\-]*)\s+
    (?P<value>\d+(?:\.\d+)?)\s*
    (?P<unit>[A-Za-z]+)\s+
    (?:to\s+the\s+|to\s+|toward\s+the\s+|towards\s+the\s+)?
    (?P<direction>[A-Za-z]+)
    \s*\.?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _message_of(request: Any) -> Optional[str]:
    if isinstance(request, Mapping):
        value = request.get("message")
    else:
        value = getattr(request, "message", None)
    return value if isinstance(value, str) else None


class RuleBasedProvider:
    """Deterministic, tiny-grammar provider.

    Deterministic in the strict sense: the same message and context always yield
    the same plan. No clock, no randomness, no identifier generation — ``job_id``
    is assigned later, at the job-creation boundary.
    """

    name = PROVIDER_NAME
    version = PROVIDER_VERSION

    @property
    def _metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_name=self.name, provider_version=self.version
        )

    def interpret(self, request: Any, context: AgentContext) -> AgentResult:
        message = _message_of(request)
        if message is None:
            return AgentResult.failure(
                "VALIDATION_ERROR", MESSAGES["MALFORMED_REQUEST"], self._metadata
            )
        if not message.strip():
            return AgentResult.failure(
                "VALIDATION_ERROR", MESSAGES["MISSING_MESSAGE"], self._metadata
            )

        match = _MOVE_PATTERN.match(message)
        if match is None:
            return AgentResult.failure(
                "UNSUPPORTED_INSTRUCTION", MESSAGES["NOT_A_MOVE"], self._metadata
            )

        # ---- words -> canonical tokens (no arithmetic here) -------------
        direction_word = match.group("direction").lower()
        direction = DIRECTION_WORDS.get(direction_word)
        if direction is None:
            return AgentResult.failure(
                "UNSUPPORTED_INSTRUCTION",
                MESSAGES["UNKNOWN_DIRECTION"],
                self._metadata,
            )

        unit_word = match.group("unit").lower()
        unit = UNIT_WORDS.get(unit_word)
        if unit is None:
            return AgentResult.failure(
                "INVALID_UNITS", MESSAGES["UNKNOWN_UNIT"], self._metadata
            )

        try:
            value = float(match.group("value"))
        except ValueError:  # pragma: no cover - the pattern guarantees digits
            return AgentResult.failure(
                "INVALID_UNITS", MESSAGES["UNKNOWN_UNIT"], self._metadata
            )

        # ---- canonical tokens -> meters, via packages/spatial ----------
        resolved = direction_delta_meters(direction, {"value": value, "unit": unit})
        if not resolved.ok:
            return AgentResult.failure(
                resolved.error.code, resolved.error.message, self._metadata
            )

        operation = PlannedOperation(
            operation_type="move_object",
            payload=MoveObjectPayload(
                target=ObjectRef(name=match.group("target")),
                delta_meters=resolved.delta,
            ),
        )
        return AgentResult.success(
            AgentPlan(operations=(operation,)), self._metadata
        )
