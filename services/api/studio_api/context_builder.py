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

from .ingest.model_copies import ModelCopyMaker
from .storage.files import ReferenceFileStore
from .storage.models import PDF, PDF_PAGE, ReferenceRecord
from .storage.repositories import StudioRepositories

_log = logging.getLogger(__name__)

#: How many images one turn may attach. Images dominate cost by a wide margin — a page at
#: 1700x2200 is twenty 512-px tiles — so this is the tightest budget in the builder, and it
#: was lowered from six after a session burned through an allowance re-sending six pages on
#: every message. Three is enough for "the plan and these two photos"; the user can always
#: attach more explicitly.
MAX_IMAGES_PER_TURN = 3
#: How many characters of extracted document text one turn may carry.
MAX_DOCUMENT_CHARACTERS = 24_000
#: How many earlier turns to replay.
MAX_RECENT_TURNS = 12


@dataclass
class ContextBuilder:
    """Builds a bounded, grounded :class:`AgentInput` for one turn."""

    repositories: StudioRepositories
    files: ReferenceFileStore
    #: Makes the cheap copy of an image that is actually sent. Injected so a test can
    #: assert what was sent without running an image codec.
    model_copies: ModelCopyMaker = field(default_factory=ModelCopyMaker)
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
            already_shown=self.repositories.deliveries.delivered_ids(project_id, session_id),
            new_since=self._last_spoke_at(project_id),
        )

        images = self._images(project_id, selected.images)
        documents = self._documents(selected.documents)
        # Remember everything this turn covered, so the next message does not pay for it
        # again. `included_ids` rather than the images alone: a PDF's parent record and its
        # page images are one reference to a person, and recording only the pages would let
        # the parent look unseen and drag the pages back in next turn.
        self.repositories.deliveries.record(project_id, session_id, selected.included_ids)
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
            questions_already_asked=self.repositories.clarifications.count_for_project(
                project_id
            ),
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

    def _last_spoke_at(self, project_id: str) -> Optional[str]:
        """When the assistant last replied.

        Anything uploaded after that is new to the conversation, which is how "drag a
        sketch in and ask about it" works without the user attaching anything. Before the
        first reply there is no cutoff, so a brand-new project's uploads all count as new.
        """
        for record in reversed(
            self.repositories.conversation.recent(project_id, limit=self.max_recent_turns)
        ):
            if record.role == "assistant":
                return record.created_at
        return None

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
                    # The downscaled copy, not the original: a model reading a dimension
                    # off a sketch does not need 1700x2200, and the difference is most of
                    # what a turn costs.
                    path=self.model_copies.copy_of(path),
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
        already_shown: Optional[set[str]] = None,
        new_since: Optional[str] = None,
    ) -> "_Selection":
        """Choose what to send.

        In order:

        1. Everything the user explicitly attached. A PDF expands into its page images,
           because a PDF the model cannot see is a PDF it will invent details about.
        2. References whose display name is mentioned in the message.
        3. References the model has NOT been shown in this conversation and that arrived
           since it last spoke — i.e. the files the user just dropped in. This is what makes
           "drag a sketch in and ask" work without any attaching.
        4. If the message actually asks for something to be looked at ("does this match the
           plan?"), the newest references, even if they were shown before: the user asked.

        Otherwise nothing. Two failures shaped this, one in each direction:

        * Sending only what was attached, and clearing attachments on send, meant a bare
          answer like "117" arrived with no drawing and Astra asked for the sketch again —
          three times. Rule 3 fixes the common case, and what the model LEARNED from an
          image persists as text in the transcript and as recorded facts.
        * Sending the newest few whenever nothing was built yet meant every message paid
          for images again, and successive turns walked down the whole library. Images are
          twenty tiles a page; that is how an allowance disappears "without doing
          anything".
        """
        selection = _Selection()

        if attached:
            for record in attached:
                self._include(project_id, record, selection)
            return selection

        records = list(self.repositories.references.list_for_project(project_id))
        lowered = user_text.lower()
        mentioned = [
            record
            for record in records
            if record.display_name.lower() in lowered or _stem(record.display_name) in lowered
        ]
        if mentioned:
            for record in mentioned:
                self._include(project_id, record, selection)
            return selection

        seen = already_shown or set()

        arrived_since = [
            record
            for record in records
            if record.reference_id not in seen
            and (new_since is None or record.created_at > new_since)
        ]
        if arrived_since:
            for record in arrived_since[::-1][: self.max_images]:
                self._include(project_id, record, selection)
            return selection

        if _asks_to_look(lowered):
            for record in records[::-1][: self.max_images]:
                self._include(project_id, record, selection)
            return selection

        # Nothing has been built yet, and this project has drawings: the conversation is
        # still ABOUT them, so keep showing them even though they were shown before.
        #
        # This is where "shown once" is wrong. A real session went: upload a plan and ask
        # for it to be modelled; "invent the door heights, make it look great"; and Astra —
        # given no image that turn — replied "could you attach the floor plan?". The drawing
        # is the subject until something exists to look at instead. It is bounded: the cost
        # stops the moment the model is built, and each image is a downscaled copy.
        if scene_is_empty and records:
            for record in records[::-1][: self.max_images]:
                self._include(project_id, record, selection)

        return selection

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


#: Phrases that mean "look at the file again", as opposed to merely mentioning a room.
#:
#: Deliberately narrow. A broad list ("room", "build", "floor") matched almost every
#: message in a design conversation, which turned "send when asked" into "send always" —
#: and images are the most expensive thing in a prompt.
_LOOK_HINTS = (
    "look at",
    "see the",
    "see this",
    "can you see",
    "check the",
    "read the",
    "from the sketch",
    "from the plan",
    "from the drawing",
    "in the sketch",
    "in the plan",
    "in the drawing",
    "in the photo",
    "in the picture",
    "match the",
    "compare",
    "again",
)


def _asks_to_look(lowered_text: str) -> bool:
    return any(hint in lowered_text for hint in _LOOK_HINTS)
