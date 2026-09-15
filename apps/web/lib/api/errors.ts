/**
 * Turning backend failures into something a designer can act on.
 *
 * Components never see an error body, an HTTP status, or an exception. They see an
 * `ApiFailure` with a `message` written for a non-technical user, plus a `code` they
 * may branch on. Keeping the translation in one module is what makes "no stack
 * traces, no jargon" auditable: there is a single place where a failure becomes
 * words.
 *
 * The backend's canonical error codes (`error-code.schema.json`) are the stable
 * thing to key on — not messages, and not statuses, since the same code can arrive
 * with different statuses.
 */

/** Why a request failed, in terms the UI can reason about. */
export type FailureKind =
  /** The API could not be reached at all. */
  | "offline"
  /** The request took too long and was abandoned. */
  | "timeout"
  /** No Blender worker is available to do the work. */
  | "no_worker"
  /** The agent cannot act on what the user asked for. */
  | "unsupported"
  /** The request itself was rejected as invalid. */
  | "invalid"
  /** The project or job could not be found. */
  | "not_found"
  /** The submission collided with existing state. */
  | "conflict"
  /** The response did not look like the API contract. */
  | "malformed"
  /** Anything else. */
  | "unknown";

export class ApiFailure extends Error {
  readonly kind: FailureKind;
  /** Canonical backend error code, when the API supplied one. */
  readonly code: string | null;
  readonly status: number | null;

  constructor(
    message: string,
    kind: FailureKind,
    code: string | null = null,
    status: number | null = null,
  ) {
    super(message);
    this.name = "ApiFailure";
    this.kind = kind;
    this.code = code;
    this.status = status;
  }
}

/**
 * Canonical backend code -> user-facing message.
 *
 * Written for someone who does not know what Blender, MCP, or a worker is. Each
 * message says what happened and, where useful, that nothing was changed — the
 * most important thing a designer needs to know after a failure.
 */
const MESSAGE_BY_CODE: Record<string, { kind: FailureKind; message: string }> = {
  BLENDER_UNAVAILABLE: {
    kind: "no_worker",
    message:
      "The design machine is currently unavailable, so nothing was changed. " +
      "Try again once it is back online.",
  },
  PROVIDER_UNAVAILABLE: {
    kind: "unsupported",
    message:
      "The design assistant is not available right now, so nothing was changed.",
  },
  UNSUPPORTED_INSTRUCTION: {
    kind: "unsupported",
    message:
      "That instruction isn't supported yet. Try something like " +
      "\u201cMove Cube 50 cm to the right.\u201d",
  },
  INVALID_UNITS: {
    kind: "unsupported",
    message:
      "That measurement wasn't understood. Use centimetres or metres, " +
      "for example \u201c50 cm\u201d.",
  },
  OBJECT_NOT_FOUND: {
    kind: "invalid",
    message: "That object isn't in this project, so nothing was changed.",
  },
  OBJECT_NOT_MOVABLE: {
    kind: "invalid",
    message: "That object can't be moved.",
  },
  VALIDATION_ERROR: {
    kind: "invalid",
    message: "That request couldn't be processed, so nothing was changed.",
  },
  PRECONDITION_MISMATCH: {
    kind: "conflict",
    message:
      "The project changed since this instruction was prepared, so nothing " +
      "was changed. Try again.",
  },
  LOCK_CONFLICT: {
    kind: "conflict",
    message: "Another change is already in progress on this project.",
  },
  MUTATION_FAILED: {
    kind: "unknown",
    message: "The change could not be applied.",
  },
  VERIFY_FAILED: {
    kind: "unknown",
    message:
      "The change could not be confirmed, so it was not accepted. The project " +
      "is unchanged.",
  },
  INTERNAL_ERROR: {
    kind: "unknown",
    message: "Something went wrong on the server. Nothing was changed.",
  },
};

/** Messages for failures that never reach the API at all. */
export const TRANSPORT_MESSAGES = {
  offline:
    "Can't reach the design studio service. Check that it is running, then try " +
    "again.",
  timeout: "The request took too long and was stopped. Nothing was changed.",
  malformed: "The service sent an unexpected response.",
  jobTimeout:
    "This change is taking longer than expected. It may still be running on " +
    "the design machine \u2014 reload to check its result.",
} as const;

/**
 * Translate a canonical backend code into a user-facing failure.
 *
 * The status is consulted first for one specific ambiguity: the canonical error
 * enum has no `PROJECT_NOT_FOUND` or `JOB_NOT_FOUND` code (deliberately — see
 * services/api/README.md), so the backend answers 404 with the generic
 * `VALIDATION_ERROR`. That means "not found" and "malformed request" arrive with the
 * same code and are only distinguishable by status. Reading the status here is what
 * lets a missing job say so instead of claiming the request was invalid.
 */
export function failureFromCode(
  code: string | null | undefined,
  status: number | null = null,
): ApiFailure {
  if (status === 404 && (!code || code === "VALIDATION_ERROR")) {
    return new ApiFailure(
      "That project or change could not be found.",
      "not_found",
      code ?? null,
      status,
    );
  }

  const known = code ? MESSAGE_BY_CODE[code] : undefined;
  if (known) {
    return new ApiFailure(known.message, known.kind, code ?? null, status);
  }

  // An unrecognised code must not leak the raw value into the interface, but it
  // is still recorded on the failure so a debug panel or a log can show it.
  if (status === 404) {
    return new ApiFailure(
      "That project or change could not be found.",
      "not_found",
      code ?? null,
      status,
    );
  }
  if (status === 503) {
    return new ApiFailure(
      TRANSPORT_MESSAGES.offline,
      "no_worker",
      code ?? null,
      status,
    );
  }
  return new ApiFailure(
    "Something went wrong. Nothing was changed.",
    "unknown",
    code ?? null,
    status,
  );
}

/**
 * A message describing why a change succeeded but has no picture.
 *
 * Deliberately never says the change failed: at this point the design change is
 * applied and saved on disk, and only the preview is missing. Telling the user
 * otherwise would be a lie about their project.
 */
export function previewUnavailableMessage(code: string | null): string {
  if (code === "BLENDER_UNAVAILABLE") {
    return "The change was applied and saved, but no preview image could be produced.";
  }
  return "The change was applied and saved, but its preview image isn't available.";
}
