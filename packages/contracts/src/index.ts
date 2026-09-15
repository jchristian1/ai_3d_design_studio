/**
 * Shared API/event contracts for AI 3D Design Studio — TypeScript representation.
 *
 * Direction of truth:
 *
 *   packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
 *            |
 *            +--> TypeScript representation  (this file)
 *            +--> Python representation      (studio_contracts)
 *
 * The interfaces below are a REPRESENTATION of the canonical schemas. They are
 * not the source of truth and neither is the Python module. When a contract
 * changes, the schema in ../schemas changes first; conformance tests
 * (./conformance.test.ts and test_conformance.py) fail until both language
 * representations are realigned.
 *
 * Canonical schemas:
 *   ChatRequest    -> chat-request.schema.json
 *   ChatResponse   -> chat-response.schema.json
 *   ChatError      -> error-response.schema.json
 *   ErrorCode      -> error-code.schema.json
 */

import type { Vec3 } from "@studio/types";

export {
  SCHEMA_DIR,
  loadSchema,
  listSchemaNames,
  schemaEnum,
  schemaRequired,
  schemaProperties,
  validateAgainstSchema,
} from "./schema.ts";
export type { JsonSchema, SchemaViolation } from "./schema.ts";

export {
  ALLOWED_TRANSITIONS,
  CONTENT_FINGERPRINT_VERSION,
  IDEMPOTENCY_VERSION,
  JOB_MESSAGES,
  UNRESOLVED_PAYLOAD_KEYS,
  canTransition,
  canonicalize,
  classifyDelivery,
  createMoveObjectJob,
  deriveContentFingerprint,
  deriveIdempotencyKey,
  encodeFloat64,
  isDuplicateDelivery,
  isTerminal,
  isValidObjectRef,
  parseJob,
  serializeJob,
  toJobWire,
  validateJob,
} from "./jobs.ts";
export type {
  CreateJobResult,
  CreateMoveObjectJobInput,
  DeliveryDecision,
  IdempotencyInput,
  JobClaimer,
  JobStore,
  JobValidationResult,
  ParseJobResult,
} from "./jobs.ts";

/**
 * A natural-language design request from the browser.
 *
 * `project_id` is REQUIRED: every request must be explicitly scoped to a
 * project (see .kiro/steering/security.md project isolation).
 */
export interface ChatRequest {
  /**
   * Stable identity of this submission. Assigned once by the client and reused
   * verbatim when retrying, so a retry executes the mutation once while a
   * genuinely new command executes again.
   */
  request_id: string;
  project_id: string;
  session_id: string;
  message: string;
  /** Optional browser-selected object the user is referring to. */
  selected_object_id?: string;
}

/** Structured, machine-readable error codes surfaced across boundaries. */
export type ErrorCode =
  | "VALIDATION_ERROR"
  | "UNSUPPORTED_INSTRUCTION"
  | "OBJECT_NOT_FOUND"
  | "OBJECT_NOT_MOVABLE"
  | "INVALID_UNITS"
  | "PRECONDITION_MISMATCH"
  | "LOCK_CONFLICT"
  | "PROVIDER_UNAVAILABLE"
  | "BLENDER_UNAVAILABLE"
  | "MUTATION_FAILED"
  | "VERIFY_FAILED"
  | "INTERNAL_ERROR";

export interface ChatError {
  code: ErrorCode;
  message: string;
}

export type ChatStatus = "success" | "error";

/**
 * The result returned to the browser after processing a ChatRequest.
 *
 * `object_position` is expressed in canonical meters and is used both by the
 * UI and by verification (see .kiro/steering/testing.md).
 */
export interface ChatResponse {
  status: ChatStatus;
  summary: string;
  object_position?: Vec3;
  preview_url?: string;
  error?: ChatError;
}

export const ERROR_CODES: readonly ErrorCode[] = [
  "VALIDATION_ERROR",
  "UNSUPPORTED_INSTRUCTION",
  "OBJECT_NOT_FOUND",
  "OBJECT_NOT_MOVABLE",
  "INVALID_UNITS",
  "PRECONDITION_MISMATCH",
  "LOCK_CONFLICT",
  "PROVIDER_UNAVAILABLE",
  "BLENDER_UNAVAILABLE",
  "MUTATION_FAILED",
  "VERIFY_FAILED",
  "INTERNAL_ERROR",
] as const;

/**
 * Canonical schema file names, so callers and tests never hardcode paths.
 * Each entry maps a representation type to its language-neutral definition.
 */
export const SCHEMA_FILES = {
  Vec3: "vec3.schema.json",
  Job: "job.schema.json",
  JobType: "job-type.schema.json",
  JobClaim: "job-claim.schema.json",
  RequestOrigin: "request-origin.schema.json",
  MoveObjectPlan: "move-object-plan.schema.json",
  MoveObjectResult: "move-object-result.schema.json",
  ArtifactType: "artifact-type.schema.json",
  PreviewArtifact: "preview-artifact.schema.json",
  WorkerMessage: "worker-message.schema.json",
  WorkerCapabilities: "worker-capabilities.schema.json",
  ObjectRef: "object-ref.schema.json",
  MoveObjectPayload: "move-object-payload.schema.json",
  ChatRequest: "chat-request.schema.json",
  ChatResponse: "chat-response.schema.json",
  ErrorResponse: "error-response.schema.json",
  ErrorCode: "error-code.schema.json",
  LengthUnit: "length-unit.schema.json",
  Measurement: "measurement.schema.json",
  Axis: "axis.schema.json",
  Direction: "direction.schema.json",
  AxisDirection: "axis-direction.schema.json",
} as const;

/**
 * Strip undefined-valued optional fields so a representation object becomes a
 * wire document comparable to the canonical schema (JSON has no `undefined`).
 */
export function toWire<T extends object>(value: T): Record<string, unknown> {
  return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
}
