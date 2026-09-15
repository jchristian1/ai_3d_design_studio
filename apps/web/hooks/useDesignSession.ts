"use client";

/**
 * The one hook the UI uses.
 *
 * Binds the pure reducer to the API client and a `JobUpdateSource`, and owns the
 * things React must own: identity that survives re-renders, and cleanup.
 *
 *     submit(message)
 *         v
 *     POST /api/chat  -> 202 { job_id }
 *         v
 *     JobUpdateSource.subscribe(project_id, job_id)
 *         v
 *     queued -> claimed -> running -> succeeded | failed
 *         v
 *     preview.url -> <img>
 *
 * Cleanup discipline: exactly one subscription can exist at a time, it is stored in
 * a ref, and it is stopped on unmount and before any new submission. A poll loop
 * that outlives its component would dispatch into a dead tree and, worse, keep
 * hitting the API forever in a tab nobody is looking at.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  ApiFailure,
  TRANSPORT_MESSAGES,
  failureFromCode,
  previewUnavailableMessage,
} from "../lib/api/index.ts";
import type { ApiClient, HealthView, PreviewView } from "../lib/api/index.ts";
import { apiClient } from "../lib/api/index.ts";
import {
  HEALTH_POLL_INTERVAL_MS,
  JOB_POLL_INTERVAL_MS,
  JOB_TIMEOUT_MS,
  PROJECT_ID,
} from "../lib/config.ts";
import { newRequestId, newSessionId } from "../lib/ids.ts";
import {
  createPollingJobUpdates,
  type JobSubscription,
  type JobUpdateSource,
} from "../lib/session/jobUpdates.ts";
import { initialSessionState, isBusy, sessionReducer } from "../lib/session/reducer.ts";
import type { SessionState } from "../lib/session/types.ts";

/** What the connection indicator shows. */
export interface ConnectionState {
  /** Have we successfully reached the API at least once recently? */
  apiReachable: boolean;
  /**
   * Is a Blender-capable worker connected and ready?
   *
   * Derived from `blender_capable_workers`/`ready_workers`, NOT from the API
   * answering 200. The API being alive says nothing about the design machine.
   */
  designMachineReady: boolean;
  checked: boolean;
}

export interface UseDesignSessionOptions {
  client?: ApiClient;
  jobUpdates?: JobUpdateSource;
  sessionId?: string;
  projectId?: string;
  /** Disable background health polling (used by tests). */
  pollHealth?: boolean;
}

export interface DesignSession {
  state: SessionState;
  connection: ConnectionState;
  busy: boolean;
  canSubmit(draft: string): boolean;
  /** Send a NEW user command. Generates a fresh request_id. */
  submit(message: string): Promise<void>;
  /** Retry the last uncertain submission, REUSING its original request_id. */
  retry(): Promise<void>;
  /** The absolute image URL to display, or null. */
  previewSrc: string | null;
  /** True while showing the previous image because a change is in flight. */
  previewIsStale: boolean;
}

export function useDesignSession(
  options: UseDesignSessionOptions = {},
): DesignSession {
  const client = options.client ?? apiClient;
  const projectId = options.projectId ?? PROJECT_ID;

  // Generated ONCE per browser session and stable across re-renders. A session_id
  // that changed on render would fragment one conversation into many.
  const sessionIdRef = useRef<string | null>(options.sessionId ?? null);
  if (sessionIdRef.current === null) sessionIdRef.current = newSessionId();
  const sessionId = sessionIdRef.current;

  const [state, dispatch] = useReducer(
    sessionReducer,
    { sessionId, projectId },
    initialSessionState,
  );

  const [connection, setConnection] = useState<ConnectionState>({
    apiReachable: false,
    designMachineReady: false,
    checked: false,
  });

  const jobUpdates = useMemo(
    () =>
      options.jobUpdates ??
      createPollingJobUpdates(client, {
        intervalMs: JOB_POLL_INTERVAL_MS,
        timeoutMs: JOB_TIMEOUT_MS,
      }),
    [client, options.jobUpdates],
  );

  const subscriptionRef = useRef<JobSubscription | null>(null);
  const stopTracking = useCallback(() => {
    subscriptionRef.current?.stop();
    subscriptionRef.current = null;
  }, []);

  // ---- health / connection indicator ---------------------------------

  useEffect(() => {
    if (options.pollHealth === false) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const check = async () => {
      try {
        const health: HealthView = await client.getHealth();
        if (cancelled) return;
        setConnection({
          apiReachable: true,
          // Blender readiness is reported by the worker, never inferred from the
          // API responding.
          designMachineReady:
            health.ready_workers > 0 && health.blender_capable_workers > 0,
          checked: true,
        });
      } catch {
        if (cancelled) return;
        setConnection({
          apiReachable: false,
          designMachineReady: false,
          checked: true,
        });
      } finally {
        if (!cancelled) {
          timer = setTimeout(() => void check(), HEALTH_POLL_INTERVAL_MS);
        }
      }
    };

    void check();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [client, options.pollHealth]);

  // ---- initial preview -----------------------------------------------

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const preview: PreviewView | null = await client.getLatestPreview(projectId);
        // 404 becomes null, which the panel renders as its empty state rather
        // than as an error.
        if (!cancelled && preview) {
          dispatch({ type: "preview_loaded", preview });
        }
      } catch {
        // A failure here is not worth interrupting the user for; the empty state
        // is a perfectly good starting point.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, projectId]);

  // ---- cleanup on unmount --------------------------------------------

  useEffect(() => stopTracking, [stopTracking]);

  // ---- tracking ------------------------------------------------------

  const track = useCallback(
    (jobId: string) => {
      // Never allow two loops. Any previous subscription is stopped first.
      stopTracking();
      subscriptionRef.current = jobUpdates.subscribe(projectId, jobId, {
        onUpdate(status) {
          dispatch({ type: "job_status", jobStatus: status.job_status });
        },
        onSettled(status) {
          if (status.job_status === "succeeded") {
            dispatch({
              type: "job_succeeded",
              preview: status.preview,
              previewWarning:
                status.preview === null
                  ? previewUnavailableMessage(status.preview_error?.code ?? null)
                  : null,
            });
          } else {
            dispatch({
              type: "job_failed",
              message: status.error
                // The same translation table as request failures, so a job
                // failure and a request failure describe one condition one way.
                ? failureFromCode(status.error.code).message
                : "The change could not be applied.",
            });
          }
        },
        onError(failure) {
          dispatch({ type: "job_failed", message: failure.message });
        },
        onTimeout() {
          dispatch({
            type: "job_timed_out",
            message: TRANSPORT_MESSAGES.jobTimeout,
          });
        },
      });
    },
    [jobUpdates, projectId, stopTracking],
  );

  // ---- submission ----------------------------------------------------

  const send = useCallback(
    async (message: string, requestId: string) => {
      const trimmed = message.trim();
      if (!trimmed) return;

      stopTracking();
      dispatch({
        type: "submission_started",
        requestId,
        message: trimmed,
        messageId: `user_${requestId}`,
      });

      try {
        const submission = await client.submitChat({
          request_id: requestId,
          project_id: projectId,
          session_id: sessionId,
          message: trimmed,
        });
        dispatch({
          type: "submission_accepted",
          jobId: submission.job_id,
          jobStatus: submission.job_status,
          duplicate: submission.duplicate,
        });
        track(submission.job_id);
      } catch (cause) {
        const failure =
          cause instanceof ApiFailure
            ? cause
            : new ApiFailure("Something went wrong.", "unknown");
        dispatch({
          type: "submission_failed",
          message: failure.message,
          // Only offer a retry when it is genuinely unclear whether the request
          // arrived. A rejected instruction must not invite the same instruction
          // again under the same identity.
          retryable: failure.kind === "offline" || failure.kind === "timeout",
        });
      }
    },
    [client, projectId, sessionId, stopTracking, track],
  );

  const submit = useCallback(
    // A NEW user command is a NEW mutation, so it gets a new request_id.
    (message: string) => send(message, newRequestId()),
    [send],
  );

  const retry = useCallback(async () => {
    const retryable = state.retryable;
    if (!retryable) return;
    // The SAME request_id: the backend derives mutation identity from it, so this
    // resolves to the existing change instead of applying it twice.
    await send(retryable.message, retryable.requestId);
  }, [send, state.retryable]);

  const busy = isBusy(state);

  const canSubmit = useCallback(
    (draft: string) => {
      if (!draft.trim()) return false;
      if (busy) return false;
      // Once we know the API is unreachable, sending cannot succeed.
      if (connection.checked && !connection.apiReachable) return false;
      return true;
    },
    [busy, connection.apiReachable, connection.checked],
  );

  const displayedPreview = state.preview ?? state.stalePreview;
  const previewSrc = displayedPreview
    ? client.getArtifactUrl(displayedPreview.url)
    : null;

  return {
    state,
    connection,
    busy,
    canSubmit,
    submit,
    retry,
    previewSrc,
    previewIsStale: state.preview === null && state.stalePreview !== null,
  };
}
