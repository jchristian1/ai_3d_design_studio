/**
 * The design workspace's state shape and events.
 *
 * One invariant shapes the whole model: **one user message owns exactly one Studio
 * reply slot**, identified by its `request_id`. A reconstruction that runs thirty
 * internal operations updates that one slot in place ("Creating walls 3 of 9") rather
 * than appending thirty entries. The reducer enforces it by replacing by id.
 */

import type {
  ApprovalView,
  ArtifactRefView,
  ClarificationView,
  ConnectionStatusView,
  DesignTurnView,
  FactView,
  ReferenceView,
  SceneObjectView,
  SceneView,
  TurnKind,
} from "../api/workspace.ts";

export type EntryAuthor = "user" | "studio";

export type EntryTone = "info" | "progress" | "success" | "warning" | "error" | "question";

export interface TranscriptEntry {
  id: string;
  author: EntryAuthor;
  text: string;
  tone: EntryTone;
  requestId?: string;
  /** Rendered as an approval card when present. */
  approval?: ApprovalView | null;
  /** Unconfirmed assumptions Astra declared, shown so the user can correct them. */
  assumptions?: string[];
  /** Step progress for a running plan. */
  progress?: { stepIndex: number; stepCount: number; label: string } | null;
}

export interface UploadState {
  id: string;
  name: string;
  status: "uploading" | "failed";
  message?: string;
}

export type WorkspacePhase = "idle" | "sending" | "working";

export interface WorkspaceState {
  projectId: string;
  sessionId: string;
  displayName: string;
  entries: TranscriptEntry[];
  references: ReferenceView[];
  /** References explicitly attached to the next message. */
  attachedReferenceIds: string[];
  facts: FactView[];
  scene: SceneView | null;
  selectedObjectId: string | null;
  model: ArtifactRefView | null;
  preview: ArtifactRefView | null;
  clarification: ClarificationView | null;
  approvals: ApprovalView[];
  phase: WorkspacePhase;
  astra: ConnectionStatusView | null;
  blender: ConnectionStatusView | null;
  gpu: ConnectionStatusView | null;
  uploads: UploadState[];
  /** A transient message about the workspace itself, not part of the conversation. */
  notice: string | null;
  /** The job the current request is tracking, if any. */
  activeJobId: string | null;
  loaded: boolean;
}

export type WorkspaceEvent =
  | { type: "workspace_loaded"; displayName: string; references: ReferenceView[]; facts: FactView[]; conversation: { role: string; text: string }[]; clarification: ClarificationView | null; approvals: ApprovalView[]; scene: SceneView | null; model: ArtifactRefView | null; preview: ArtifactRefView | null }
  | { type: "workspace_load_failed"; message: string }
  | { type: "message_sent"; requestId: string; text: string }
  | { type: "turn_received"; turn: DesignTurnView }
  | { type: "turn_failed"; requestId: string; message: string }
  | { type: "job_progress"; requestId: string; label: string; stepIndex: number; stepCount: number }
  | { type: "job_settled"; requestId: string; succeeded: boolean; message: string; model: ArtifactRefView | null; scene: SceneView | null }
  | { type: "upload_started"; id: string; name: string }
  | { type: "upload_succeeded"; id: string; reference: ReferenceView; notice: string | null }
  | { type: "upload_failed"; id: string; message: string }
  | { type: "reference_removed"; referenceId: string }
  | { type: "attachment_toggled"; referenceId: string }
  | { type: "attachments_cleared" }
  | { type: "object_selected"; objectId: string | null }
  | { type: "status_updated"; astra?: ConnectionStatusView; blender?: ConnectionStatusView; gpu?: ConnectionStatusView }
  | { type: "approval_resolved"; approvalId: string }
  | { type: "notice"; message: string | null };

export const TONE_BY_KIND: Record<TurnKind, EntryTone> = {
  answer: "info",
  clarification: "question",
  approval_required: "warning",
  plan: "progress",
  error: "error",
};

/** The stable id of the single Studio reply slot owned by one request. */
export function studioEntryId(requestId: string): string {
  return `studio_${requestId}`;
}

export function userEntryId(requestId: string): string {
  return `user_${requestId}`;
}

export function selectedObject(state: WorkspaceState): SceneObjectView | null {
  if (!state.selectedObjectId || !state.scene) return null;
  return (
    state.scene.objects.find(
      (object) => object.studio_object_id === state.selectedObjectId,
    ) ?? null
  );
}

export function isBusy(state: WorkspaceState): boolean {
  return state.phase === "sending" || state.phase === "working";
}

export function canModel(state: WorkspaceState): boolean {
  return Boolean(state.blender?.connected);
}

export function pendingApproval(state: WorkspaceState): ApprovalView | null {
  return state.approvals.find((approval) => approval.decision === "pending") ?? null;
}

export function attachedReferences(state: WorkspaceState): ReferenceView[] {
  return state.references.filter((reference) =>
    state.attachedReferenceIds.includes(reference.reference_id),
  );
}
