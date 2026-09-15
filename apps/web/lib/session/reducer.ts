/**
 * The design session reducer.
 *
 * A pure function: `(state, event) => state`. No fetch, no timers, no React. That
 * is what lets the entire conversation, job-tracking, and preview lifecycle be
 * tested as data — the awkward parts of this UI are its state transitions, not its
 * markup, so they are kept where they can be examined directly.
 *
 * Two invariants the transitions exist to protect:
 *
 *   1. **A failed preview never reads as a failed change.** `job_succeeded` with no
 *      preview sets `previewWarning` and still lands in `succeeded`. The design
 *      change is on disk; only its picture is missing.
 *   2. **The last good preview stays visible.** Starting a new change moves the
 *      current preview to `stalePreview` rather than clearing it, so the panel
 *      never flashes empty mid-change.
 */

import type {
  ChatMessage,
  MessageTone,
  SessionEvent,
  SessionState,
} from "./types.ts";
import { STATUS_TEXT } from "./types.ts";

export interface InitialSessionOptions {
  sessionId: string;
  projectId: string;
}

export function initialSessionState(
  options: InitialSessionOptions,
): SessionState {
  return {
    sessionId: options.sessionId,
    projectId: options.projectId,
    messages: [],
    phase: "idle",
    submission: null,
    preview: null,
    stalePreview: null,
    previewWarning: null,
    retryable: null,
  };
}

/** Is a mutation currently in flight? Used to gate further submissions. */
export function isBusy(state: SessionState): boolean {
  return state.phase === "submitting" || state.phase === "tracking";
}

let noticeCounter = 0;

function studioMessage(
  text: string,
  tone: MessageTone,
  requestId?: string,
): ChatMessage {
  noticeCounter += 1;
  return {
    id: `studio_${noticeCounter}`,
    author: "studio",
    text,
    tone,
    ...(requestId ? { requestId } : {}),
  };
}

/**
 * Append a studio message, replacing the previous PROGRESS line for the same
 * submission.
 *
 * Progress updates supersede one another: a transcript reading "Queued…",
 * "Picked up…", "Applying…" as three separate lines is noise. Terminal messages
 * are appended and kept.
 */
function withStudioMessage(
  messages: ChatMessage[],
  message: ChatMessage,
): ChatMessage[] {
  if (message.tone !== "progress") return [...messages, message];

  const last = messages[messages.length - 1];
  if (
    last &&
    last.author === "studio" &&
    last.tone === "progress" &&
    last.requestId === message.requestId
  ) {
    return [...messages.slice(0, -1), message];
  }
  return [...messages, message];
}

export function sessionReducer(
  state: SessionState,
  event: SessionEvent,
): SessionState {
  switch (event.type) {
    case "submission_started": {
      const userMessage: ChatMessage = {
        id: event.messageId,
        author: "user",
        text: event.message,
        tone: "info",
        requestId: event.requestId,
      };
      return {
        ...state,
        phase: "submitting",
        submission: {
          requestId: event.requestId,
          message: event.message,
          jobId: null,
          jobStatus: null,
        },
        // Hold on to the current image while the new change is applied.
        stalePreview: state.preview ?? state.stalePreview,
        previewWarning: null,
        retryable: null,
        messages: [...state.messages, userMessage],
      };
    }

    case "submission_accepted": {
      if (!state.submission) return state;
      const text = event.duplicate
        ? "This change was already submitted \u2014 showing its existing result."
        : STATUS_TEXT[event.jobStatus];
      return {
        ...state,
        phase: "tracking",
        submission: {
          ...state.submission,
          jobId: event.jobId,
          jobStatus: event.jobStatus,
        },
        messages: withStudioMessage(
          state.messages,
          studioMessage(text, "progress", state.submission.requestId),
        ),
      };
    }

    case "submission_failed": {
      return {
        ...state,
        phase: "failed",
        // Offer an explicit retry of THIS submission, which must reuse the
        // original request_id so the backend treats it as the same mutation.
        retryable:
          event.retryable && state.submission
            ? {
                requestId: state.submission.requestId,
                message: state.submission.message,
              }
            : null,
        messages: withStudioMessage(
          state.messages,
          studioMessage(event.message, "error", state.submission?.requestId),
        ),
      };
    }

    case "job_status": {
      if (!state.submission) return state;
      if (state.submission.jobStatus === event.jobStatus) return state;
      return {
        ...state,
        submission: { ...state.submission, jobStatus: event.jobStatus },
        messages: withStudioMessage(
          state.messages,
          studioMessage(
            STATUS_TEXT[event.jobStatus],
            "progress",
            state.submission.requestId,
          ),
        ),
      };
    }

    case "job_succeeded": {
      const requestId = state.submission?.requestId;
      // A missing preview is a WARNING, never a failure: the change is saved.
      const messages = withStudioMessage(
        state.messages,
        studioMessage(
          event.previewWarning ?? STATUS_TEXT.succeeded,
          event.previewWarning ? "warning" : "success",
          requestId,
        ),
      );
      return {
        ...state,
        phase: "succeeded",
        submission: state.submission
          ? { ...state.submission, jobStatus: "succeeded" }
          : null,
        preview: event.preview ?? state.preview,
        stalePreview: null,
        previewWarning: event.previewWarning,
        retryable: null,
        messages,
      };
    }

    case "job_failed":
    case "job_timed_out": {
      return {
        ...state,
        phase: "failed",
        submission: state.submission
          ? {
              ...state.submission,
              jobStatus: event.type === "job_failed" ? "failed" : state.submission.jobStatus,
            }
          : null,
        // A timeout is genuinely uncertain, so it is retryable with the same
        // request_id — which is exactly what makes retrying safe. A reported
        // failure is a definite answer and is not offered as a retry.
        retryable:
          event.type === "job_timed_out" && state.submission
            ? {
                requestId: state.submission.requestId,
                message: state.submission.message,
              }
            : null,
        messages: withStudioMessage(
          state.messages,
          studioMessage(event.message, "error", state.submission?.requestId),
        ),
      };
    }

    case "preview_loaded": {
      return {
        ...state,
        preview: event.preview ?? state.preview,
        stalePreview: null,
      };
    }

    case "preview_unavailable": {
      return { ...state, previewWarning: event.message };
    }

    case "notice": {
      return {
        ...state,
        messages: withStudioMessage(
          state.messages,
          studioMessage(event.text, event.tone),
        ),
      };
    }

    default: {
      // Exhaustiveness: an unhandled event type becomes a compile error.
      const unreachable: never = event;
      return unreachable;
    }
  }
}
