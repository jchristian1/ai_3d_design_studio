/**
 * The API boundary.
 *
 * Components import from here, never from a module deeper in this folder, so the
 * surface the UI depends on is explicit and small.
 */

export { apiClient, createApiClient } from "./client.ts";
export type { ApiClient, ApiClientOptions } from "./client.ts";
export {
  ApiFailure,
  TRANSPORT_MESSAGES,
  failureFromCode,
  previewUnavailableMessage,
} from "./errors.ts";
export type { FailureKind } from "./errors.ts";
export { TERMINAL_JOB_STATUSES, isTerminalStatus } from "./types.ts";
export type {
  ArtifactType,
  ChatRequestBody,
  ChatResponseView,
  ChatSubmission,
  HealthView,
  JobStatus,
  JobStatusView,
  PreviewView,
  Vec3,
  WorkerView,
  WorkersView,
} from "./types.ts";
