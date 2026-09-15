/**
 * The design session reducer.
 *
 * A pure function: `(state, event) => state`. No fetch, no timers, no React. That
 * is what lets the entire conversation, job-tracking, and preview lifecycle be
 * tested as data — the awkward parts of this UI are its state transitions, not its
 * markup, so they are kept where they can be examined directly.
 *
 * Three invariants the transitions exist to protect:
 *
 *   1. **One studio reply per submission.** Every studio message carries an id
 *      DERIVED from the submission it belongs to, and appending is
 *      replace-by-id. A submission therefore occupies exactly one reply slot that
 *      transitions progress → terminal, so no sequence of events can produce two
 *      terminal lines for one command. See `withStudioMessage`.
 *   2. **A failed preview never reads as a failed change.** `job_succeeded` with no
 *      preview sets `previewWarning` and still lands in `succeeded`. The design
 *      change is on disk; only its picture is missing.
 *   3. **The last good preview stays visible.** Starting a new change moves the
 *      current preview to `stalePreview` rather than clearing it, so the panel
 *      never flashes empty mid-change.
 *
 * PURITY MATTERS HERE
 * -------------------
 * React invokes a reducer twice per dispatch in development to surface impurity.
 * Message ids are therefore derived from the event and the existing state, never
 * from a counter or a clock: an id that changed between the two invocations would
 * make React's own consistency check the source of duplicate messages.
 */

import type {
  ChatMessage,
  MessageTone,
  SessionEvent,
  SessionState,
} from "./types.ts";
import { STATUS_TEXT } from "./types.ts";
import { isTerminalStatus } from "../api/index.ts";

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

/**
 * The id of the single studio reply belonging to a submission.
 *
 * Stable across the whole lifecycle of that submission, which is what collapses
 * "queued", "applying", and "done" into one evolving line instead of a pile of
 * messages — and what makes a duplicate terminal message unrepresentable.
 */
function studioReplyId(requestId: string | undefined): string {
  return requestId ? `studio_${requestId}` : "studio_unattached";
}

function studioMessage(
  id: string,
  text: string,
  tone: MessageTone,
  requestId?: string,
): ChatMessage {
  return {
    id,
    author: "studio",
    text,
    tone,
    ...(requestId ? { requestId } : {}),
  };
}

/**
 * Insert a message, or REPLACE the existing one with the same id.
 *
 * Replace-by-id is the whole duplicate defence. The polling loop legitimately
 * reports a terminal state through two callbacks (`onUpdate` then `onSettled`), a
 * retry re-enters the flow with the same `request_id`, and a redelivered result can
 * arrive twice — none of which may add a second line. Because identity is derived
 * rather than generated, all of those land in the same slot.
 */
function withStudioMessage(
  messages: ChatMessage[],
  message: ChatMessage,
): ChatMessage[] {
  const index = messages.findIndex((existing) => existing.id === message.id);
  if (index === -1) return [...messages, message];
  return [
    ...messages.slice(0, index),
    message,
    ...messages.slice(index + 1),
  ];
}

/** Insert or replace any message by id, used for the user's own line too. */
function withMessage(
  messages: ChatMessage[],
  message: ChatMessage,
): ChatMessage[] {
  return withStudioMessage(messages, message);
}

export function sessionReducer(
  state: SessionState,
  event: SessionEvent,
): SessionState {
  switch (event.type) {
    case "submission_started": {
      const userMessage: ChatMessage = {
        // Derived from the request, so an explicit retry of the same submission
        // reuses this line instead of echoing the command twice.
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
        messages: withMessage(state.messages, userMessage),
      };
    }

    case "submission_accepted": {
      if (!state.submission) return state;
      const requestId = state.submission.requestId;
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
          studioMessage(studioReplyId(requestId), text, "progress", requestId),
        ),
      };
    }

    case "submission_failed": {
      const requestId = state.submission?.requestId;
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
          studioMessage(
            studioReplyId(requestId),
            event.message,
            "error",
            requestId,
          ),
        ),
      };
    }

    case "job_status": {
      if (!state.submission) return state;
      if (state.submission.jobStatus === event.jobStatus) return state;

      // A TERMINAL status carries no message here. The polling source reports a
      // terminal state through `onUpdate` AND `onSettled`, and the terminal wording
      // depends on context this event does not have (whether a preview arrived,
      // what the error was). So the status is recorded and the message is left to
      // `job_succeeded` / `job_failed`, which own terminal presentation.
      const submission = { ...state.submission, jobStatus: event.jobStatus };
      if (isTerminalStatus(event.jobStatus)) {
        return { ...state, submission };
      }

      const requestId = state.submission.requestId;
      return {
        ...state,
        submission,
        messages: withStudioMessage(
          state.messages,
          studioMessage(
            studioReplyId(requestId),
            STATUS_TEXT[event.jobStatus],
            "progress",
            requestId,
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
          studioReplyId(requestId),
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
      const requestId = state.submission?.requestId;
      return {
        ...state,
        phase: "failed",
        submission: state.submission
          ? {
              ...state.submission,
              jobStatus:
                event.type === "job_failed"
                  ? "failed"
                  : state.submission.jobStatus,
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
          studioMessage(
            studioReplyId(requestId),
            event.message,
            "error",
            requestId,
          ),
        ),
      };
    }

    case "preview_loaded": {
      // Preview metadata updates the IMAGE only. It appends no message, so a
      // preview arriving after success cannot restate the outcome.
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
      // Unattached notices are not part of a submission, so their id is derived
      // from position — still a pure function of the current state.
      return {
        ...state,
        messages: withStudioMessage(
          state.messages,
          studioMessage(
            `studio_notice_${state.messages.length}`,
            event.text,
            event.tone,
          ),
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
