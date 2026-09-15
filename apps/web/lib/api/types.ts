/**
 * API view models.
 *
 * DIRECTION OF TRUTH
 * ------------------
 * The canonical vocabulary lives in `packages/contracts/schemas`, mirrored by
 * `@studio/types`. Anything shared with the backend is imported from there rather
 * than restated, so the browser cannot drift from the contract:
 *
 *     JobStatus     queued | claimed | running | succeeded | failed
 *     ArtifactType  preview_image
 *
 * The shapes below are the API's HTTP RESPONSES, which are deliberately not the
 * same thing as the canonical contracts. Two examples of why:
 *
 *   - `ChatSubmission` is a submission acknowledgement carrying a `job_id`. The
 *     canonical `ChatResponse` describes the FINAL answer about a change and closes
 *     its object, so it cannot carry one.
 *   - `PreviewView` adds `url`. The canonical `PreviewArtifact` deliberately has no
 *     URL, because a worker must not know the control plane's route shape; the URL
 *     is an API-layer projection.
 *
 * They are therefore defined explicitly here, as view models, and named so nobody
 * mistakes them for the contracts.
 */

import type { ArtifactType, JobStatus, Vec3 } from "@studio/types";

export type { ArtifactType, JobStatus, Vec3 };

/** Job statuses from which nothing further will happen. */
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = [
  "succeeded",
  "failed",
] as const;

export function isTerminalStatus(status: JobStatus | null): boolean {
  return status !== null && TERMINAL_JOB_STATUSES.includes(status);
}

/** The canonical structured error body every failing endpoint returns. */
export interface ApiErrorBody {
  error: { code: string; message: string };
  request_id?: string;
}

/** POST /api/chat request. Mirrors the canonical ChatRequest exactly. */
export interface ChatRequestBody {
  request_id: string;
  project_id: string;
  session_id: string;
  message: string;
  selected_object_id?: string;
}

/** POST /api/chat response (202). */
export interface ChatSubmission {
  request_id: string;
  project_id: string;
  session_id: string;
  job_id: string;
  job_status: JobStatus;
  summary: string;
  worker_id: string | null;
  duplicate: boolean;
  provider: string | null;
  /** Project-scoped URL to poll. Always relative to the API base. */
  status_url: string;
}

/** A preview artifact as the API presents it, including its logical URL. */
export interface PreviewView {
  artifact_id: string;
  artifact_type: ArtifactType;
  media_type: string;
  created_at: string;
  width: number;
  height: number;
  size_bytes: number;
  checksum: string;
  /** Project-scoped, relative to the API base. Never a filesystem path. */
  url: string;
  engine: string | null;
}

/** The canonical ChatResponse, embedded once a job is terminal. */
export interface ChatResponseView {
  status: "success" | "error";
  summary: string;
  object_position?: Vec3 | null;
  preview_url?: string | null;
  error?: { code: string; message: string } | null;
}

/** GET /api/projects/{project_id}/jobs/{job_id} response. */
export interface JobStatusView {
  job_id: string;
  project_id: string;
  session_id: string;
  request_id: string;
  job_type: string;
  job_status: JobStatus;
  created_at: string;
  updated_at: string;
  worker_id: string | null;
  /** Internal worker phase. Debug only — never shown as primary UI text. */
  execution_phase: string | null;
  reconciled: boolean;
  result: Record<string, unknown> | null;
  error: { code: string; message: string } | null;
  preview: PreviewView | null;
  /** Set when the change succeeded but its preview could not be produced. */
  preview_error: { code: string; message: string } | null;
  chat: ChatResponseView | null;
}

/** GET /health response. */
export interface HealthView {
  api: string;
  environment: string;
  registered_workers: number;
  ready_workers: number;
  /**
   * Workers that advertised a usable Blender. Zero means design changes will
   * fail even though the API itself is healthy — the UI must not imply otherwise.
   */
  blender_capable_workers: number;
}

/** One entry of GET /api/workers. Development diagnostics only. */
export interface WorkerView {
  worker_id: string;
  liveness: "healthy" | "busy" | "lost";
  worker_state: string;
  connected: boolean;
  capabilities: {
    blender_available?: boolean;
    blender_version?: string;
    gpu_available?: boolean;
    supported_job_types?: string[];
  };
}

export interface WorkersView {
  workers: WorkerView[];
  registered_workers: number;
  ready_workers: number;
}
