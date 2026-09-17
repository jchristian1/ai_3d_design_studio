/**
 * Browser-safe configuration.
 *
 * Everything here is compiled into the client bundle, so it must contain ONLY
 * values that are safe to publish. Nothing secret may ever appear in a
 * `NEXT_PUBLIC_*` variable: the worker token, Blender paths, journal and recovery
 * locations, and API credentials all stay server-side and never reach this file.
 *
 * The API base URL is read from the environment with a loopback default, so a
 * fresh checkout runs with no configuration while a deployment can point the UI
 * at a real control plane without a rebuild of the source.
 */

/** Default control-plane origin for local development. */
export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

/**
 * The one project Spec 001 has.
 *
 * A LOGICAL id — never a filesystem path. The browser cannot name a `.blend`,
 * and the API would refuse a path-shaped project id anyway.
 */
export const PROJECT_ID = "proj_seed";

/** Human-readable label for the active project. */
export const PROJECT_LABEL = "Spec 001 — Seed Project";

/** How often to ask the API for job progress, in milliseconds. */
export const JOB_POLL_INTERVAL_MS = 750;

/** How often to refresh the connection indicator, in milliseconds. */
export const HEALTH_POLL_INTERVAL_MS = 5000;

/** Give up on a single HTTP request after this long. */
export const REQUEST_TIMEOUT_MS = 15000;

/**
 * Stop polling a job after this long and report a timeout.
 *
 * Generous, because a real Blender render legitimately takes tens of seconds; the
 * point is only that the UI cannot poll forever.
 */
export const JOB_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * How often to ask whether Astra has answered, and how long to keep asking.
 *
 * A design turn runs a real model: seconds for a question, minutes for a floor plan. The
 * server hands back a turn id immediately and this is the cadence for collecting the
 * answer, so no single request has to outlive `REQUEST_TIMEOUT_MS`.
 *
 * The ceiling is kept in step with the server-side `STUDIO_CODEX_TIMEOUT_SECONDS`
 * (default 1500 s = 25 min). If the browser gave up first, an answer the model actually
 * produced would be thrown away, so this must be >= the server timeout.
 */
export const TURN_POLL_INTERVAL_MS = 1000;
export const TURN_TIMEOUT_MS = 25 * 60 * 1000;

/**
 * Resolve the API base URL, trimming any trailing slash so URL joining is
 * unambiguous.
 */
export function resolveApiBaseUrl(
  raw: string | undefined = process.env.NEXT_PUBLIC_API_BASE_URL,
): string {
  const value = (raw ?? "").trim() || DEFAULT_API_BASE_URL;
  return value.replace(/\/+$/, "");
}

export const API_BASE_URL = resolveApiBaseUrl();

/**
 * The file types the upload endpoint accepts, as an `accept` attribute.
 *
 * A hint to the file picker only. The backend validates the extension, the declared
 * type AND the actual leading bytes, so this list being generous or stale can never
 * widen what is really accepted.
 */
export const SUPPORTED_UPLOAD_ACCEPT =
  ".png,.jpg,.jpeg,.webp,.pdf,.txt,.md,image/png,image/jpeg,image/webp,application/pdf,text/plain,text/markdown";
