/**
 * Test scaffolding.
 *
 * A stub API transport and a controllable clock, so the real client, the real
 * reducer, and the real polling logic are exercised without a network or wall-clock
 * waits. Nothing here re-implements production behaviour — it only supplies the two
 * things the browser normally provides.
 */

import type {
  ChatSubmission,
  HealthView,
  JobStatus,
  JobStatusView,
  PreviewView,
} from "../lib/api/index.ts";
import type { Scheduler } from "../lib/session/jobUpdates.ts";

export const PROJECT_ID = "proj_seed";
export const MOVE_COMMAND = "Move Cube 50 cm to the right.";
export const API_BASE = "http://127.0.0.1:8000";

/** One recorded HTTP call. */
export interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
}

export interface StubRoute {
  status: number;
  /** JSON body, or a string for a malformed response. */
  body: unknown;
}

/**
 * A `fetch` stand-in driven by exact-path handlers.
 *
 * Deliberately matches on the request path, so a test asserting "polls the
 * project-scoped job URL" fails if the client builds a different URL.
 */
export class StubTransport {
  readonly calls: RecordedCall[] = [];
  private handlers: Array<{
    match: (path: string, method: string) => boolean;
    respond: (call: RecordedCall) => StubRoute;
  }> = [];

  on(
    match: (path: string, method: string) => boolean,
    respond: StubRoute | ((call: RecordedCall) => StubRoute),
  ): this {
    this.handlers.push({
      match,
      respond: typeof respond === "function" ? respond : () => respond,
    });
    return this;
  }

  onGet(path: string, respond: StubRoute | ((call: RecordedCall) => StubRoute)) {
    return this.on((p, m) => m === "GET" && p === path, respond);
  }

  onPost(path: string, respond: StubRoute | ((call: RecordedCall) => StubRoute)) {
    return this.on((p, m) => m === "POST" && p === path, respond);
  }

  /** Paths that were requested, in order. */
  paths(method?: string): string[] {
    return this.calls
      .filter((call) => (method ? call.method === method : true))
      .map((call) => call.url);
  }

  countOf(path: string): number {
    return this.calls.filter((call) => call.url === path).length;
  }

  get fetch(): typeof fetch {
    return (async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === "string" ? input : String(input);
      const path = raw.startsWith(API_BASE) ? raw.slice(API_BASE.length) : raw;
      const method = (init?.method ?? "GET").toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      const call: RecordedCall = { url: path, method, body };
      this.calls.push(call);

      const handler = this.handlers.find((h) => h.match(path, method));
      if (!handler) {
        return jsonResponse(404, {
          error: { code: "VALIDATION_ERROR", message: "no stub for this path" },
        });
      }
      const route = handler.respond(call);
      return typeof route.body === "string"
        ? textResponse(route.status, route.body)
        : jsonResponse(route.status, route.body);
    }) as typeof fetch;
  }
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function textResponse(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/plain" },
  });
}

/**
 * A manual scheduler.
 *
 * Timers only fire when a test says so, which is what makes polling assertions
 * deterministic instead of dependent on real delays.
 */
export class ManualScheduler implements Scheduler {
  private time = 0;
  private pending = new Map<number, { at: number; callback: () => void }>();
  private nextHandle = 1;

  setTimeout(callback: () => void, ms: number): unknown {
    const handle = this.nextHandle++;
    this.pending.set(handle, { at: this.time + ms, callback });
    return handle;
  }

  clearTimeout(handle: unknown): void {
    this.pending.delete(handle as number);
  }

  now(): number {
    return this.time;
  }

  get pendingCount(): number {
    return this.pending.size;
  }

  /** Advance time and run everything that comes due. */
  advance(ms: number): void {
    this.time += ms;
    const due = [...this.pending.entries()]
      .filter(([, entry]) => entry.at <= this.time)
      .sort((a, b) => a[1].at - b[1].at);
    for (const [handle, entry] of due) {
      this.pending.delete(handle);
      entry.callback();
    }
  }
}

/** Let queued promise callbacks settle. */
export async function flush(times = 6): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
  }
}

// ---------------------------------------------------------------------------
// Response builders
// ---------------------------------------------------------------------------

export function health(overrides: Partial<HealthView> = {}): HealthView {
  return {
    api: "healthy",
    environment: "local",
    registered_workers: 1,
    ready_workers: 1,
    blender_capable_workers: 1,
    ...overrides,
  };
}

export function preview(overrides: Partial<PreviewView> = {}): PreviewView {
  const artifactId = overrides.artifact_id ?? "preview_aaaabbbbccccdddd11112222";
  return {
    artifact_id: artifactId,
    artifact_type: "preview_image",
    media_type: "image/png",
    created_at: "2026-09-15T04:00:00Z",
    width: 640,
    height: 360,
    size_bytes: 47002,
    checksum: `sha256:${"a".repeat(64)}`,
    url: `/api/projects/${PROJECT_ID}/artifacts/${artifactId}`,
    engine: "BLENDER_WORKBENCH",
    ...overrides,
  };
}

export function submission(
  overrides: Partial<ChatSubmission> = {},
): ChatSubmission {
  const requestId = overrides.request_id ?? "req_test_1";
  const jobId = overrides.job_id ?? `job_${requestId}_0`;
  return {
    request_id: requestId,
    project_id: PROJECT_ID,
    session_id: "sess_test",
    job_id: jobId,
    job_status: "queued",
    summary: "The change was understood and sent to the design machine.",
    worker_id: "worker_local_1",
    duplicate: false,
    provider: "rule_based",
    status_url: `/api/projects/${PROJECT_ID}/jobs/${jobId}`,
    ...overrides,
  };
}

export function jobStatus(
  status: JobStatus,
  overrides: Partial<JobStatusView> = {},
): JobStatusView {
  const jobId = overrides.job_id ?? "job_req_test_1_0";
  return {
    job_id: jobId,
    project_id: PROJECT_ID,
    session_id: "sess_test",
    request_id: "req_test_1",
    job_type: "move_object",
    job_status: status,
    created_at: "2026-09-15T04:00:00Z",
    updated_at: "2026-09-15T04:00:01Z",
    worker_id: "worker_local_1",
    execution_phase: status === "succeeded" ? "completed" : null,
    reconciled: false,
    result:
      status === "succeeded"
        ? { applied: true, verified: true, final_position_meters: { x: 0.5, y: 0, z: 0 } }
        : null,
    error: null,
    preview: status === "succeeded" ? preview() : null,
    preview_error: null,
    chat: null,
    ...overrides,
  };
}

export function jobPath(jobId: string, projectId = PROJECT_ID): string {
  return `/api/projects/${projectId}/jobs/${jobId}`;
}
