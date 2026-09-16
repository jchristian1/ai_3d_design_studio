"use client";

/**
 * The one hook the workspace runs on.
 *
 * Owns: loading a project, polling connection status, sending a message, tracking the
 * job a message produced, uploading references, and deciding approvals. Everything it
 * knows is in `WorkspaceState`; everything it does goes through the reducer, so the UI
 * has no local state to fall out of step with.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import { ApiFailure } from "../lib/api/errors.ts";
import type { ApiClient } from "../lib/api/index.ts";
import { apiClient } from "../lib/api/index.ts";
import type { ArtifactRefView, DesignTurnView, WorkspaceClient } from "../lib/api/workspace.ts";
import { isFinishedTurn, workspaceClient as defaultWorkspaceClient } from "../lib/api/workspace.ts";
import {
  HEALTH_POLL_INTERVAL_MS,
  JOB_POLL_INTERVAL_MS,
  PROJECT_ID,
  TURN_POLL_INTERVAL_MS,
  TURN_TIMEOUT_MS,
} from "../lib/config.ts";
import { newRequestId, newSessionId, uuid } from "../lib/ids.ts";
import { initialWorkspaceState, workspaceReducer } from "../lib/workspace/reducer.ts";
import type { WorkspaceState } from "../lib/workspace/types.ts";
import { isBusy } from "../lib/workspace/types.ts";

/** How a job's internal phase reads to a designer. */
const PHASE_LABELS: Record<string, string> = {
  received: "Getting started…",
  plan_persisted: "Preparing the changes…",
  recovery_created: "Saving a restore point…",
  executing: "Making the changes…",
  mutation_verified: "Checking the result…",
  project_saved: "Saving your project…",
  scene_inspected: "Reading the updated scene…",
  preview_generated: "Rendering a preview…",
  model_exported: "Building the 3D model…",
  completed: "Done.",
  failed: "That did not work.",
};

export interface UseWorkspaceOptions {
  projectId?: string;
  sessionId?: string;
  client?: WorkspaceClient;
  jobClient?: ApiClient;
  /** Disable background polling in tests. */
  poll?: boolean;
  pollIntervalMs?: number;
  /** How long to wait for a model answer before saying so. */
  turnTimeoutMs?: number;
}

export interface WorkspaceSession {
  state: WorkspaceState;
  busy: boolean;
  canSubmit(draft: string): boolean;
  send(message: string): Promise<void>;
  upload(files: File[] | FileList): Promise<void>;
  removeReference(referenceId: string): Promise<void>;
  toggleAttachment(referenceId: string): void;
  selectObject(objectId: string | null): void;
  decide(approvalId: string, approved: boolean): Promise<void>;
  saveFact(key: string, value: string): Promise<void>;
  dismissNotice(): void;
  modelUrl: string | null;
  previewUrl: string | null;
  referenceUrl(referenceId: string): string;
  refresh(): Promise<void>;
  /** Exposed so the connect panel can drive the official Codex sign-in. */
  client: WorkspaceClient;
  refreshStatus(): Promise<void>;
}

export function useWorkspace(options: UseWorkspaceOptions = {}): WorkspaceSession {
  const client = options.client ?? defaultWorkspaceClient;
  const jobs = options.jobClient ?? apiClient;
  const projectId = options.projectId ?? PROJECT_ID;
  const sessionIdRef = useRef<string>(options.sessionId ?? "");
  if (!sessionIdRef.current) sessionIdRef.current = newSessionId();

  const [state, dispatch] = useReducer(
    workspaceReducer,
    { projectId, sessionId: sessionIdRef.current },
    initialWorkspaceState,
  );

  const tracking = useRef<{ stop: boolean } | null>(null);

  // --- loading -----------------------------------------------------------
  const refresh = useCallback(async () => {
    try {
      const workspace = await client.getWorkspace(projectId);
      dispatch({
        type: "workspace_loaded",
        displayName: workspace.project?.display_name ?? projectId,
        references: workspace.references ?? [],
        facts: workspace.facts ?? [],
        conversation: workspace.conversation ?? [],
        clarification: workspace.clarification ?? null,
        approvals: workspace.approvals ?? [],
        scene: workspace.scene ?? null,
        model: workspace.model ?? null,
        preview: workspace.preview ?? null,
      });
    } catch (error) {
      dispatch({
        type: "workspace_load_failed",
        message:
          error instanceof ApiFailure
            ? error.message
            : "This project could not be opened.",
      });
    }
  }, [client, projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // --- connection status -------------------------------------------------
  const refreshStatus = useCallback(async () => {
    const [astra, blender] = await Promise.all([
      client.getAstraStatus().catch(() => undefined),
      client.getBlenderStatus().catch(() => undefined),
    ]);
    if (astra || blender) dispatch({ type: "status_updated", astra, blender });
  }, [client]);

  useEffect(() => {
    void refreshStatus();
    if (options.poll === false) return;
    const timer = setInterval(() => void refreshStatus(), HEALTH_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refreshStatus, options.poll]);

  // --- job tracking ------------------------------------------------------
  const track = useCallback(
    async (requestId: string, jobId: string) => {
      tracking.current?.stop && (tracking.current.stop = true);
      const handle = { stop: false };
      tracking.current = handle;
      const interval = options.pollIntervalMs ?? JOB_POLL_INTERVAL_MS;

      // Poll until terminal. A dropped poll is retried rather than treated as failure:
      // the mutation is durable on the worker regardless of what this browser saw.
      for (;;) {
        if (handle.stop) return;
        await new Promise((resolve) => setTimeout(resolve, interval));
        if (handle.stop) return;

        let status;
        try {
          status = await jobs.getJobStatus(projectId, jobId);
        } catch (error) {
          if (error instanceof ApiFailure && (error.kind === "offline" || error.kind === "timeout")) {
            continue;
          }
          dispatch({
            type: "job_settled",
            requestId,
            succeeded: false,
            message:
              error instanceof ApiFailure ? error.message : "The change could not be tracked.",
            model: null,
            scene: null,
          });
          return;
        }

        const progress = (status as { progress?: { step_index: number; step_count: number; label: string } | null })
          .progress;
        if (progress && !handle.stop) {
          dispatch({
            type: "job_progress",
            requestId,
            label: progress.label,
            stepIndex: progress.step_index,
            stepCount: progress.step_count,
          });
        } else if (status.execution_phase && PHASE_LABELS[status.execution_phase]) {
          dispatch({
            type: "job_progress",
            requestId,
            label: PHASE_LABELS[status.execution_phase]!,
            stepIndex: 0,
            stepCount: 1,
          });
        }

        if (status.job_status === "succeeded" || status.job_status === "failed") {
          const succeeded = status.job_status === "succeeded";
          const result = (status.result ?? {}) as {
            model?: ArtifactRefView | null;
            scene?: WorkspaceState["scene"];
            applied?: number;
          };
          // The model reference travels on the job result, but its browser URL is
          // assembled by the workspace endpoint, so re-read the authoritative pointer.
          const [model, scene] = await Promise.all([
            client.getLatestModel(projectId).catch(() => null),
            client.getScene(projectId).catch(() => null),
          ]);
          dispatch({
            type: "job_settled",
            requestId,
            succeeded,
            message: succeeded
              ? applianceMessage(result.applied)
              : status.error?.message ?? "That change could not be completed.",
            model,
            scene,
          });
          return;
        }
      }
    },
    [client, jobs, options.pollIntervalMs, projectId],
  );

  useEffect(() => () => {
    if (tracking.current) tracking.current.stop = true;
  }, []);

  // --- actions -----------------------------------------------------------

  /**
   * Wait for a started turn to be answered.
   *
   * The model is slow — tens of seconds for a question, minutes for a floor plan — so the
   * server hands back a turn id immediately and the answer is polled. Each poll is a
   * short request, so nothing depends on one long-lived connection, and a reload can pick
   * the answer up again.
   */
  const awaitTurn = useCallback(
    async (projectId: string, turnId: string): Promise<DesignTurnView> => {
      const interval = options.pollIntervalMs ?? TURN_POLL_INTERVAL_MS;
      const deadline = Date.now() + (options.turnTimeoutMs ?? TURN_TIMEOUT_MS);

      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, interval));
        let update;
        try {
          update = await client.getTurn(projectId, turnId);
        } catch (error) {
          // A poll that fails is not a turn that failed. Keep asking through anything
          // that looks like the connection rather than the answer: a dropped request, a
          // timeout, or a gateway error with no canonical code from our API (a proxy
          // hiccup, or the API restarting). A structured failure IS the answer, so it
          // ends the wait.
          const transportish =
            error instanceof ApiFailure &&
            (error.kind === "offline" ||
              error.kind === "timeout" ||
              (error.code === null && (error.status ?? 0) >= 502));
          if (transportish) {
            if (Date.now() > deadline) throw error;
            continue;
          }
          throw error;
        }
        if (isFinishedTurn(update)) return update;
        if (Date.now() > deadline) {
          throw new ApiFailure(
            "Astra is taking longer than expected. Your message is still being worked on — check back in a moment.",
            "timeout",
          );
        }
      }
    },
    [client, options.pollIntervalMs, options.turnTimeoutMs],
  );

  const send = useCallback(
    async (message: string) => {
      const text = message.trim();
      if (!text) return;
      const requestId = newRequestId();
      dispatch({ type: "message_sent", requestId, text });

      try {
        const started = await client.sendMessage(projectId, {
          requestId,
          sessionId: sessionIdRef.current,
          message: text,
          attachedReferenceIds: state.attachedReferenceIds,
          selectedObjectId: state.selectedObjectId,
        });

        const turn = isFinishedTurn(started)
          ? started
          : await (async () => {
              dispatch({
                type: "job_progress",
                requestId,
                label: "Astra is thinking…",
                stepIndex: 0,
                stepCount: 1,
              });
              return awaitTurn(projectId, started.turn_id);
            })();

        // Keyed by the id THIS message was sent with. One user message owns exactly one
        // reply slot, and that must not depend on the server echoing the id back
        // unchanged — a mismatch would leave the "thinking" line orphaned above the
        // answer instead of being replaced by it.
        dispatch({ type: "turn_received", turn: { ...turn, request_id: requestId } });
        if (turn.job_id) void track(requestId, turn.job_id);
      } catch (error) {
        dispatch({
          type: "turn_failed",
          requestId,
          message:
            error instanceof ApiFailure ? error.message : "That message could not be sent.",
        });
      }
    },
    [awaitTurn, client, projectId, state.attachedReferenceIds, state.selectedObjectId, track],
  );

  const upload = useCallback(
    async (files: File[] | FileList) => {
      const list = Array.from(files);
      for (const file of list) {
        const id = uuid();
        dispatch({ type: "upload_started", id, name: file.name });
        try {
          const reference = await client.uploadReference(projectId, file);
          dispatch({
            type: "upload_succeeded",
            id,
            reference,
            notice: reference.notice ?? null,
          });
        } catch (error) {
          dispatch({
            type: "upload_failed",
            id,
            message:
              error instanceof ApiFailure ? error.message : "That file could not be uploaded.",
          });
        }
      }
    },
    [client, projectId],
  );

  const removeReference = useCallback(
    async (referenceId: string) => {
      try {
        await client.deleteReference(projectId, referenceId);
        dispatch({ type: "reference_removed", referenceId });
      } catch (error) {
        dispatch({
          type: "notice",
          message:
            error instanceof ApiFailure ? error.message : "That reference could not be removed.",
        });
      }
    },
    [client, projectId],
  );

  const decide = useCallback(
    async (approvalId: string, approved: boolean) => {
      try {
        const started = await client.decideApproval(
          projectId,
          approvalId,
          approved,
          sessionIdRef.current,
        );
        dispatch({ type: "approval_resolved", approvalId });
        const turn: DesignTurnView = isFinishedTurn(started)
          ? started
          : await awaitTurn(projectId, started.turn_id);
        dispatch({ type: "turn_received", turn });
        if (turn.job_id) void track(turn.request_id, turn.job_id);
      } catch (error) {
        dispatch({
          type: "notice",
          message:
            error instanceof ApiFailure ? error.message : "That decision could not be recorded.",
        });
      }
    },
    [awaitTurn, client, projectId, track],
  );

  const saveFact = useCallback(
    async (key: string, value: string) => {
      try {
        await client.setFact(projectId, key, value);
        await refresh();
      } catch (error) {
        dispatch({
          type: "notice",
          message: error instanceof ApiFailure ? error.message : "That could not be saved.",
        });
      }
    },
    [client, projectId, refresh],
  );

  const canSubmit = useCallback(
    (draft: string) => draft.trim().length > 0 && !isBusy(state),
    [state],
  );

  const modelUrl = useMemo(
    () => (state.model ? client.absoluteUrl(state.model.url) : null),
    [client, state.model],
  );
  const previewUrl = useMemo(
    () => (state.preview ? client.absoluteUrl(state.preview.url) : null),
    [client, state.preview],
  );

  return {
    state,
    busy: isBusy(state),
    canSubmit,
    send,
    upload,
    removeReference,
    toggleAttachment: (referenceId) => dispatch({ type: "attachment_toggled", referenceId }),
    selectObject: (objectId) => dispatch({ type: "object_selected", objectId }),
    decide,
    saveFact,
    dismissNotice: () => dispatch({ type: "notice", message: null }),
    modelUrl,
    previewUrl,
    referenceUrl: (referenceId) => client.referenceContentUrl(projectId, referenceId),
    refresh,
    client,
    refreshStatus,
  };
}

function applianceMessage(applied: number | undefined): string {
  if (!applied || applied <= 0) return "Everything was already up to date.";
  if (applied === 1) return "Done — one change applied.";
  return `Done — ${applied} changes applied.`;
}
