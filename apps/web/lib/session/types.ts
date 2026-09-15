/**
 * The design session's vocabulary.
 *
 * Kept separate from React so the whole conversation/job/preview lifecycle is a
 * pure data transformation that can be tested without a DOM or a browser.
 */

import type { JobStatus, PreviewView } from "../api/index.ts";

/** Who produced a line in the transcript. */
export type MessageAuthor = "user" | "studio";

/** How a studio message should read. */
export type MessageTone = "info" | "progress" | "success" | "warning" | "error";

export interface ChatMessage {
  id: string;
  author: MessageAuthor;
  text: string;
  tone: MessageTone;
  /** The submission this message belongs to, when any. */
  requestId?: string;
}

/** What the UI is currently doing. */
export type SessionPhase =
  /** Nothing in flight. */
  | "idle"
  /** The submission is being sent. */
  | "submitting"
  /** The job exists and is being followed. */
  | "tracking"
  /** The last submission finished successfully. */
  | "succeeded"
  /** The last submission failed. */
  | "failed";

/** The submission currently being tracked. */
export interface ActiveSubmission {
  /** Reused verbatim when the user explicitly retries THIS submission. */
  requestId: string;
  /** The original message text, so an explicit retry resends the same thing. */
  message: string;
  jobId: string | null;
  jobStatus: JobStatus | null;
}

export interface SessionState {
  sessionId: string;
  projectId: string;
  messages: ChatMessage[];
  phase: SessionPhase;
  submission: ActiveSubmission | null;
  /** The preview currently displayed. */
  preview: PreviewView | null;
  /**
   * Kept while a new change is in flight, so the panel shows the last known
   * image instead of flashing empty.
   */
  stalePreview: PreviewView | null;
  /**
   * Set when a change succeeded but its preview is unavailable. Rendered as a
   * warning, never as a failure — the design change did happen.
   */
  previewWarning: string | null;
  /** A submission the user may retry with its ORIGINAL request_id. */
  retryable: { requestId: string; message: string } | null;
}

export type SessionEvent =
  | { type: "submission_started"; requestId: string; message: string; messageId: string }
  | { type: "submission_accepted"; jobId: string; jobStatus: JobStatus; duplicate: boolean }
  | { type: "submission_failed"; message: string; retryable: boolean }
  | { type: "job_status"; jobStatus: JobStatus }
  | {
      type: "job_succeeded";
      preview: PreviewView | null;
      previewWarning: string | null;
    }
  | { type: "job_failed"; message: string }
  | { type: "job_timed_out"; message: string }
  | { type: "preview_loaded"; preview: PreviewView | null }
  | { type: "preview_unavailable"; message: string }
  | { type: "notice"; text: string; tone: MessageTone };

/**
 * User-facing text for each job status.
 *
 * Deliberately free of implementation vocabulary. The user is told what is
 * happening to THEIR change, not how the system is structured: no worker, no
 * executor, no MCP, no plan, no job identifier. `claimed` is the contract's word
 * for "a machine has taken this on", and it is phrased that way rather than
 * exposed as-is.
 */
export const STATUS_TEXT: Record<JobStatus, string> = {
  queued: "Change understood. Sending to the design machine\u2026",
  claimed: "The design machine has picked up this change\u2026",
  running: "Applying the change\u2026",
  succeeded: "Done \u2014 the change has been applied and saved.",
  failed: "The change could not be applied.",
};

/** Short label for the status bar. */
export const STATUS_LABEL: Record<JobStatus, string> = {
  queued: "Queued",
  claimed: "Accepted",
  running: "Applying",
  succeeded: "Succeeded",
  failed: "Failed",
};
