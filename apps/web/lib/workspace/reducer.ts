/**
 * The workspace reducer. Pure, so it runs safely under React StrictMode's double
 * invocation and can be tested without a DOM.
 *
 * The load-bearing rule: a Studio reply is REPLACED by id, never appended. That is what
 * makes "one user message, one reply slot" true no matter how many internal operations
 * a request produces.
 */

import type { ArtifactRefView, ReferenceView } from "../api/workspace.ts";
import {
  TONE_BY_KIND,
  type TranscriptEntry,
  type WorkspaceEvent,
  type WorkspaceState,
  studioEntryId,
  userEntryId,
} from "./types.ts";

export interface InitialWorkspaceOptions {
  projectId: string;
  sessionId: string;
  displayName?: string;
}

export function initialWorkspaceState(options: InitialWorkspaceOptions): WorkspaceState {
  return {
    projectId: options.projectId,
    sessionId: options.sessionId,
    displayName: options.displayName ?? options.projectId,
    entries: [],
    references: [],
    attachedReferenceIds: [],
    facts: [],
    scene: null,
    selectedObjectId: null,
    model: null,
    preview: null,
    clarification: null,
    approvals: [],
    phase: "idle",
    astra: null,
    blender: null,
    uploads: [],
    notice: null,
    activeJobId: null,
    loaded: false,
  };
}

/** Insert or replace an entry by id, preserving position on replace. */
function withEntry(entries: TranscriptEntry[], entry: TranscriptEntry): TranscriptEntry[] {
  const index = entries.findIndex((candidate) => candidate.id === entry.id);
  if (index === -1) return [...entries, entry];
  const next = entries.slice();
  next[index] = entry;
  return next;
}

function sortReferences(references: ReferenceView[]): ReferenceView[] {
  return references
    .slice()
    .sort((left, right) => left.created_at.localeCompare(right.created_at));
}

export function workspaceReducer(
  state: WorkspaceState,
  event: WorkspaceEvent,
): WorkspaceState {
  switch (event.type) {
    case "workspace_loaded": {
      // A restored conversation has no request ids, so entries are keyed by position.
      // They are history: nothing will update them in place.
      const restored: TranscriptEntry[] = event.conversation.map((turn, index) => ({
        id: `history_${index}`,
        author: turn.role === "user" ? "user" : "studio",
        text: turn.text,
        tone: turn.role === "user" ? "info" : "info",
      }));
      const pending = event.approvals.find((approval) => approval.decision === "pending");
      const entries = pending
        ? withEntry(restored, {
            id: `approval_${pending.approval_id}`,
            author: "studio",
            text: pending.summary,
            tone: "warning",
            approval: pending,
          })
        : restored;
      return {
        ...state,
        displayName: event.displayName || state.displayName,
        entries,
        references: sortReferences(event.references),
        facts: event.facts,
        clarification: event.clarification,
        approvals: event.approvals,
        scene: event.scene,
        model: event.model,
        preview: event.preview,
        loaded: true,
      };
    }

    case "workspace_load_failed":
      return { ...state, loaded: true, notice: event.message };

    case "message_sent":
      return {
        ...state,
        phase: "sending",
        activeJobId: null,
        entries: withEntry(state.entries, {
          id: userEntryId(event.requestId),
          author: "user",
          text: event.text,
          tone: "info",
          requestId: event.requestId,
        }),
      };

    case "turn_received": {
      const { turn } = event;
      const entry: TranscriptEntry = {
        id: studioEntryId(turn.request_id),
        author: "studio",
        text: turn.message,
        tone: TONE_BY_KIND[turn.kind] ?? "info",
        requestId: turn.request_id,
        approval: turn.approval,
        assumptions: turn.assumptions.length ? turn.assumptions : undefined,
      };
      const planning = turn.kind === "plan";
      return {
        ...state,
        entries: withEntry(state.entries, entry),
        phase: planning ? "working" : "idle",
        activeJobId: turn.job_id,
        clarification: turn.kind === "clarification" ? turn.clarification : null,
        approvals: turn.approval
          ? [...state.approvals.filter((a) => a.approval_id !== turn.approval!.approval_id), turn.approval]
          : state.approvals,
        // A sent message consumes its attachments; keeping them would silently
        // re-send the same images on the next turn and spend credits twice.
        attachedReferenceIds: planning || turn.kind !== "error" ? [] : state.attachedReferenceIds,
      };
    }

    case "turn_failed":
      return {
        ...state,
        phase: "idle",
        entries: withEntry(state.entries, {
          id: studioEntryId(event.requestId),
          author: "studio",
          text: event.message,
          tone: "error",
          requestId: event.requestId,
        }),
      };

    case "job_progress": {
      const existing = state.entries.find(
        (entry) => entry.id === studioEntryId(event.requestId),
      );
      return {
        ...state,
        phase: "working",
        entries: withEntry(state.entries, {
          id: studioEntryId(event.requestId),
          author: "studio",
          text: existing?.text ?? "Working on it.",
          tone: "progress",
          requestId: event.requestId,
          approval: existing?.approval ?? null,
          assumptions: existing?.assumptions,
          progress: {
            stepIndex: event.stepIndex,
            stepCount: event.stepCount,
            label: event.label,
          },
        }),
      };
    }

    case "job_settled": {
      const existing = state.entries.find(
        (entry) => entry.id === studioEntryId(event.requestId),
      );
      return {
        ...state,
        phase: "idle",
        activeJobId: null,
        entries: withEntry(state.entries, {
          id: studioEntryId(event.requestId),
          author: "studio",
          text: event.message || existing?.text || "Done.",
          tone: event.succeeded ? "success" : "error",
          requestId: event.requestId,
          assumptions: existing?.assumptions,
          progress: null,
        }),
        // Keep the previous model until a new one actually arrives, so the viewer
        // never blanks out mid-update.
        model: event.model ?? state.model,
        scene: event.scene ?? state.scene,
      };
    }

    case "upload_started":
      return {
        ...state,
        uploads: [...state.uploads, { id: event.id, name: event.name, status: "uploading" }],
      };

    case "upload_succeeded": {
      const references = sortReferences([
        ...state.references.filter(
          (reference) => reference.reference_id !== event.reference.reference_id,
        ),
        event.reference,
      ]);
      return {
        ...state,
        uploads: state.uploads.filter((upload) => upload.id !== event.id),
        references,
        // A freshly uploaded reference is attached automatically: the user uploaded it
        // because they want it looked at.
        attachedReferenceIds: state.attachedReferenceIds.includes(event.reference.reference_id)
          ? state.attachedReferenceIds
          : [...state.attachedReferenceIds, event.reference.reference_id],
        notice: event.notice ?? state.notice,
      };
    }

    case "upload_failed":
      return {
        ...state,
        uploads: state.uploads.map((upload) =>
          upload.id === event.id
            ? { ...upload, status: "failed", message: event.message }
            : upload,
        ),
      };

    case "reference_removed":
      return {
        ...state,
        references: state.references.filter(
          (reference) => reference.reference_id !== event.referenceId,
        ),
        attachedReferenceIds: state.attachedReferenceIds.filter(
          (id) => id !== event.referenceId,
        ),
      };

    case "attachment_toggled": {
      const attached = state.attachedReferenceIds.includes(event.referenceId);
      return {
        ...state,
        attachedReferenceIds: attached
          ? state.attachedReferenceIds.filter((id) => id !== event.referenceId)
          : [...state.attachedReferenceIds, event.referenceId],
      };
    }

    case "attachments_cleared":
      return { ...state, attachedReferenceIds: [] };

    case "object_selected":
      return { ...state, selectedObjectId: event.objectId };

    case "status_updated":
      return {
        ...state,
        astra: event.astra ?? state.astra,
        blender: event.blender ?? state.blender,
      };

    case "approval_resolved":
      return {
        ...state,
        approvals: state.approvals.filter(
          (approval) => approval.approval_id !== event.approvalId,
        ),
        entries: state.entries.map((entry) =>
          entry.approval?.approval_id === event.approvalId
            ? { ...entry, approval: null }
            : entry,
        ),
      };

    case "notice":
      return { ...state, notice: event.message };

    default: {
      // Exhaustiveness: a new event without a case is a compile error, not a silent
      // no-op at runtime.
      const unreachable: never = event;
      return state;
    }
  }
}

export function latestModelUrl(
  state: WorkspaceState,
  toAbsolute: (url: string) => string,
): string | null {
  const model: ArtifactRefView | null = state.model;
  return model ? toAbsolute(model.url) : null;
}
