"""``ContextBuilder`` — the only place that decides what the agent sees.

Two jobs, both of which matter for a usable product:

**Grounding.** Assemble the confirmed facts, the authoritative scene, the selected
object, the pending question and the relevant references, so the agent reasons about
reality instead of guessing.

**Bounding.** Astra runs on Christian's ChatGPT allowance, so every turn has a budget.
Unchanged references are not re-sent every message: explicitly attached references always
go, and beyond those a small, deliberately simple relevance rule selects what else to
include. There is no vector database, and none is needed for a single-user studio with a
handful of plans.

What is asserted absent, by test: credentials, filesystem paths, worker tokens, MCP
transport detail, backend tool names, platform script source, and any other project's
data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from studio_agent.agent_input import (
    AgentInput,
    ConversationTurn,
    PendingClarification,
    ReferenceDocument,
    ReferenceImage,
)
from studio_types import SceneSnapshot

from .storage.files import ReferenceFileStore
from .storage.models import PDF, PDF_PAGE, ReferenceRecord
from .storage.repositories import StudioRepositories

_log = logging.getLogger(__name__)

#: How many images one turn may attach. Images dominate cost, so this is the tightest
#: budget in the builder. Four is enough for "the plan and these two photos".
MAX_IMAGES_PER_TURN = 6
#: How many characters of extracted document text one turn may carry.
MAX_DOCUMENT_CHARACTERS = 24_000
#: How many earlier turns to replay.
MAX_RECENT_TURNS = 12


@dataclass
class ContextBuilder:
    """Builds a bounded, grounded :class:`AgentInput` for one turn."""

    repositories: StudioRepositories
    files: ReferenceFileStore
    max_images: int = MAX_IMAGES_PER_TURN
    max_document_characters: int = MAX_DOCUMENT_CHARACTERS
    max_recent_turns: int = MAX_RECENT_TURNS

    def build(
        self,
        *,
        user_text: str,
        user_id: str,
        project_id: str,
        session_id: str,
        attached_reference_ids: Sequence[str] = (),
        selected_object_id: Optional[str] = None,
        scene: Optional[SceneSnapshot] = None,
        blender_available: bool = True,
    ) -> AgentInput:
        facts = self.repositories.facts.as_mapping(project_id)
        pending = self._pending_clarification(project_id)
        attached = self.repositories.references.resolve_many(project_id, attached_reference_ids)
        selected = self._select_references(
            project_id,
            attached,
            user_text,
            clarification_open=pending is not None,
            scene_is_empty=scene is None or not scene.objects,
        )

        images = self._images(project_id, selected.images)
        documents = self._documents(selected.documents)
        summaries = self._summaries(project_id, selected.included_ids)

        return AgentInput(
            user_text=user_text,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            images=images,
            documents=documents,
            project_facts=facts,
            reference_summaries=summaries,
            scene=scene,
            selected_object_id=selected_object_id,
            pending_clarification=pending,
            recent_turns=self._recent_turns(project_id),
            blender_available=blender_available,
        )

    # -- pieces ------------------------------------------------------------
    def _pending_clarification(self, project_id: str) -> Optional[PendingClarification]:
        record = self.repositories.clarifications.open_for_project(project_id)
        if record is None:
            return None
        return PendingClarification(
            clarification_id=record.clarification_id,
            question=record.question,
            missing_information=record.missing_information,
        )

    def _recent_turns(self, project_id: str) -> tuple[ConversationTurn, ...]:
        records = self.repositories.conversation.recent(project_id, limit=self.max_recent_turns)
        return tuple(
            ConversationTurn(role=record.role, text=record.text) for record in records
        )

    def _images(
        self, project_id: str, records: Sequence[ReferenceRecord]
    ) -> tuple[ReferenceImage, ...]:
        images: list[ReferenceImage] = []
        for record in records:
            path = self.files.path_for(project_id, record.stored_name)
            if path is None:
                # The record exists but its bytes do not. Skipping is better than
                # handing the provider a path that will fail mid-call.
                _log.warning("reference %s has no stored bytes", record.reference_id)
                continue
            images.append(
                ReferenceImage(
                    reference_id=record.reference_id,
                    path=path,
                    media_type=record.media_type,
                    label=record.display_name,
                    page_number=record.page_number,
                )
            )
            if len(images) >= self.max_images:
                break
        return tuple(images)

    def _documents(
        self, records: Sequence[ReferenceRecord]
    ) -> tuple[ReferenceDocument, ...]:
        documents: list[ReferenceDocument] = []
        budget = self.max_document_characters
        for record in records:
            text = (record.extracted_text or "").strip()
            if not text:
                continue
            if budget <= 0:
                break
            excerpt = text[:budget]
            budget -= len(excerpt)
            documents.append(
                ReferenceDocument(
                    reference_id=record.reference_id,
                    label=record.label,
                    text=excerpt,
                )
            )
        return tuple(documents)

    def _summaries(self, project_id: str, included_ids: set[str]) -> tuple[str, ...]:
        """Describe references NOT sent this turn, so the agent knows to ask for them."""
        summaries: list[str] = []
        for record in self.repositories.references.list_for_project(project_id):
            if record.reference_id in included_ids:
                continue
            detail = record.media_type
            if record.kind == PDF and record.page_count:
                detail = f"PDF, {record.page_count} pages"
            summaries.append(f"{record.display_name} ({detail})")
        return tuple(summaries)

    # -- relevance ---------------------------------------------------------
    def _select_references(
        self,
        project_id: str,
        attached: Sequence[ReferenceRecord],
        user_text: str,
        *,
        clarification_open: bool = False,
        scene_is_empty: bool = True,
    ) -> "_Selection":
        """Choose what to send.

        The rule, in order:

        1. Everything the user explicitly attached, always. Expanding a PDF into its
           page images, because a PDF the model cannot see is a PDF it will invent
           details about.
        2. If nothing was attached, references whose display name is mentioned in the
           message.
        3. If still nothing, the project's most recent references — as long as the turn
           is plausibly about them (see :meth:`_should_offer_recent`).

        Rule 3 used to require the MESSAGE to look visual, and to send only the last two
        references. That failed in the most ordinary conversation there is:

            user:   "model this room"        + a sketch attached
            Astra:  "what is the ceiling height?"
            user:   "117"                    <- nothing attached, nothing visual
            Astra:  "could you attach the sketch again?"

        The answer to a question is a bare number, so every word-matching heuristic
        missed, the sketch was withheld, and Astra asked for what it had never been shown
        — three times. An open question is exactly when the references in play must stay
        in play.
        """
        selection = _Selection()

        if attached:
            for record in attached:
                self._include(project_id, record, selection)
            return selection

        lowered = user_text.lower()
        mentioned = [
            record
            for record in self.repositories.references.list_for_project(project_id)
            if record.display_name.lower() in lowered
            or _stem(record.display_name) in lowered
        ]
        if mentioned:
            for record in mentioned:
                self._include(project_id, record, selection)
            return selection

        if self._should_offer_recent(
            lowered, clarification_open=clarification_open, scene_is_empty=scene_is_empty
        ):
            # Newest first, and bounded by the image budget: what a person means by "the
            # sketch" is almost always the last thing they uploaded.
            recent = list(self.repositories.references.list_for_project(project_id))[::-1]
            for record in recent[: self.max_images]:
                self._include(project_id, record, selection)

        return selection

    def _should_offer_recent(
        self, lowered_text: str, *, clarification_open: bool, scene_is_empty: bool
    ) -> bool:
        """Whether an un-attached reference is worth the tokens this turn.

        Yes while the conversation is still ABOUT the references: a question is open, or
        nothing has been built yet, or the message itself asks for something to be looked
        at. No once there is a model and the user is editing it — "make this 20 cm taller"
        does not need the floor plan re-sent, and images are the most expensive thing in
        the prompt.
        """
        if clarification_open:
            return True
        if scene_is_empty:
            return True
        return _looks_visual(lowered_text)

    def _include(
        self, project_id: str, record: ReferenceRecord, selection: "_Selection"
    ) -> None:
        selection.included_ids.add(record.reference_id)
        if record.kind == PDF:
            # Attach the rendered pages, and the PDF's own extracted text.
            if record.extracted_text:
                selection.documents.append(record)
            for page in self.repositories.references.pages_of(project_id, record.reference_id):
                selection.included_ids.add(page.reference_id)
                selection.images.append(page)
            return
        if record.is_image:
            selection.images.append(record)
            return
        selection.documents.append(record)


@dataclass
class _Selection:
    images: list[ReferenceRecord] = field(default_factory=list)
    documents: list[ReferenceRecord] = field(default_factory=list)
    included_ids: set[str] = field(default_factory=set)


def _stem(display_name: str) -> str:
    return display_name.rsplit(".", 1)[0].lower()


#: Words that suggest the user wants the agent to look at something.
_VISUAL_HINTS = (
    "see",
    "look",
    "plan",
    "drawing",
    "photo",
    "image",
    "picture",
    "reference",
    "reconstruct",
    "build",
    "model",
    "recreate",
    "scale",
    "dimension",
    "layout",
    "room",
    "floor",
)


def _looks_visual(lowered_text: str) -> bool:
    return any(hint in lowered_text for hint in _VISUAL_HINTS)
