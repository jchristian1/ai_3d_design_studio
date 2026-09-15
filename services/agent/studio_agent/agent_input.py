"""``AgentInput`` — the provider-neutral description of one agent turn.

This is what every provider receives. It is deliberately independent of any model
vendor: it carries *what* the agent should know, not *how* a particular API wants it
delivered. `CodexAstraProvider` decides that Codex wants images as `-i` file arguments;
a future provider might inline them as base64. Nothing above the provider changes.

Two rules this structure enforces by shape:

* An image is a real file the provider must attach. Putting a path in a prompt and
  hoping the model reads it is not multimodal input, so images are separated from text.
* Filesystem paths live here and go no further. They are used by the provider to attach
  bytes, and never serialised into a prompt, a contract, or the browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from studio_types import SceneSnapshot


@dataclass(frozen=True)
class ReferenceImage:
    """An image the model should actually see this turn."""

    reference_id: str
    #: Worker/control-plane side only. Never rendered, never sent to the model as text.
    path: Path
    media_type: str
    label: str
    #: Set for pages derived from a PDF, so the model can say "on page 2".
    page_number: Optional[int] = None

    def describe(self) -> str:
        if self.page_number is not None:
            return f"{self.label} (page {self.page_number})"
        return self.label


@dataclass(frozen=True)
class ReferenceDocument:
    """Text extracted from a reference, supplied inline."""

    reference_id: str
    label: str
    text: str


@dataclass(frozen=True)
class ConversationTurn:
    """One earlier exchange, bounded by the context builder."""

    role: str  # "user" | "assistant"
    text: str


@dataclass(frozen=True)
class PendingClarification:
    """A question already asked, which the user's current message may be answering."""

    clarification_id: str
    question: str
    missing_information: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentInput:
    """Everything the agent is allowed to know for one turn."""

    user_text: str
    user_id: str
    project_id: str
    session_id: str

    #: Images to attach, already selected and bounded by the context builder.
    images: tuple[ReferenceImage, ...] = ()
    #: Extracted document text, already bounded.
    documents: tuple[ReferenceDocument, ...] = ()
    #: Confirmed project facts, for example {"ceiling_height_m": "2.4"}.
    project_facts: Mapping[str, str] = field(default_factory=dict)
    #: Short descriptions of references NOT attached this turn, so the agent knows they
    #: exist and can ask for them.
    reference_summaries: tuple[str, ...] = ()
    #: The authoritative scene, or None when Blender has never been read.
    scene: Optional[SceneSnapshot] = None
    #: What the user has selected in the 3D viewer, if anything.
    selected_object_id: Optional[str] = None
    #: A question awaiting an answer.
    pending_clarification: Optional[PendingClarification] = None
    #: Bounded recent conversation.
    recent_turns: tuple[ConversationTurn, ...] = ()
    #: True when the design machine is reachable. A disconnected Blender still allows
    #: analysis and discussion, so the agent needs to know which it is.
    blender_available: bool = True

    @property
    def has_images(self) -> bool:
        return bool(self.images)

    @property
    def has_scene(self) -> bool:
        return self.scene is not None and bool(self.scene.objects)
