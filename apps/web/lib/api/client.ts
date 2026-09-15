/**
 * The typed API client.
 *
 * The ONLY place in the browser that performs HTTP. Components and hooks call
 * methods here and receive typed data or an `ApiFailure`; they never see a
 * `Response`, a status code, or an error body. That keeps backend response shapes
 * out of the component tree and means an API change has one place to land.
 *
 *     component -> hook -> ApiClient -> fetch
 *                          ^^^^^^^^^
 *                          error parsing, timeouts, URL building
 *
 * `fetch` and the base URL are injected, so tests exercise the real client against
 * a stub transport instead of mocking the global.
 */

import { API_BASE_URL, REQUEST_TIMEOUT_MS } from "../config.ts";
import { ApiFailure, TRANSPORT_MESSAGES, failureFromCode } from "./errors.ts";
import type {
  ApiErrorBody,
  ChatRequestBody,
  ChatSubmission,
  HealthView,
  JobStatusView,
  PreviewView,
  WorkersView,
} from "./types.ts";

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface ApiClient {
  readonly baseUrl: string;
  getHealth(): Promise<HealthView>;
  getWorkers(): Promise<WorkersView>;
  submitChat(body: ChatRequestBody): Promise<ChatSubmission>;
  getJobStatus(projectId: string, jobId: string): Promise<JobStatusView>;
  getLatestPreview(projectId: string): Promise<PreviewView | null>;
  /** Absolute URL for an artifact, built from the configured API base. */
  getArtifactUrl(relativeOrAbsoluteUrl: string): string;
}

/** Join a path onto the API base without producing a double slash. */
function joinUrl(baseUrl: string, path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

function isErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== "object" || value === null) return false;
  const error = (value as { error?: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string"
  );
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = (options.baseUrl ?? API_BASE_URL).replace(/\/+$/, "");
  const timeoutMs = options.timeoutMs ?? REQUEST_TIMEOUT_MS;
  // Captured rather than read from the global at call time, so an injected
  // transport cannot be bypassed.
  const doFetch = options.fetchImpl ?? globalThis.fetch;

  async function request<T>(
    path: string,
    init: RequestInit = {},
    { allow404 = false }: { allow404?: boolean } = {},
  ): Promise<T | null> {
    const url = joinUrl(baseUrl, path);

    // Every request is bounded. A hung control plane must degrade the UI, not
    // freeze it.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    let response: Response;
    try {
      response = await doFetch(url, {
        ...init,
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...(init.body ? { "Content-Type": "application/json" } : {}),
          ...(init.headers ?? {}),
        },
      });
    } catch (cause) {
      // An aborted request is a timeout; anything else at this layer means the
      // service could not be reached at all.
      const aborted =
        cause instanceof Error &&
        (cause.name === "AbortError" || cause.name === "TimeoutError");
      throw new ApiFailure(
        aborted ? TRANSPORT_MESSAGES.timeout : TRANSPORT_MESSAGES.offline,
        aborted ? "timeout" : "offline",
      );
    } finally {
      clearTimeout(timer);
    }

    if (response.status === 404 && allow404) return null;

    if (!response.ok) {
      // Parse the canonical error body when present; never surface raw text.
      let code: string | null = null;
      try {
        const body: unknown = await response.json();
        if (isErrorBody(body)) code = body.error.code;
      } catch {
        // A non-JSON error body tells the user nothing useful.
      }
      throw failureFromCode(code, response.status);
    }

    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiFailure(TRANSPORT_MESSAGES.malformed, "malformed", null, response.status);
    }
  }

  async function requestRequired<T>(
    path: string,
    init?: RequestInit,
  ): Promise<T> {
    const result = await request<T>(path, init);
    if (result === null) {
      throw new ApiFailure(TRANSPORT_MESSAGES.malformed, "malformed");
    }
    return result;
  }

  return {
    baseUrl,

    getHealth() {
      return requestRequired<HealthView>("/health");
    },

    getWorkers() {
      return requestRequired<WorkersView>("/api/workers");
    },

    submitChat(body: ChatRequestBody) {
      return requestRequired<ChatSubmission>("/api/chat", {
        method: "POST",
        body: JSON.stringify(body),
      });
    },

    getJobStatus(projectId: string, jobId: string) {
      // Project-scoped by path. There is no unscoped job route, by design: a
      // job_id is an identifier, not a capability.
      return requestRequired<JobStatusView>(
        `/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(jobId)}`,
      );
    },

    getLatestPreview(projectId: string) {
      // 404 is the normal "nothing rendered yet" answer, so it is a value here
      // rather than a failure — the UI shows an empty state, not an error.
      return request<PreviewView>(
        `/api/projects/${encodeURIComponent(projectId)}/preview/latest`,
        {},
        { allow404: true },
      );
    },

    getArtifactUrl(relativeOrAbsoluteUrl: string) {
      return joinUrl(baseUrl, relativeOrAbsoluteUrl);
    },
  };
}

/** The client the app runs with. */
export const apiClient: ApiClient = createApiClient();
