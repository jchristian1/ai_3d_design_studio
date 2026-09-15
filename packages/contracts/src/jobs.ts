/**
 * Structured job layer — TypeScript representation (Spec 001, Task 3).
 *
 * This module owns the durable contract that carries a FULLY RESOLVED semantic
 * operation from the control plane toward the Blender Worker.
 *
 * Deliberate non-goals for this task:
 *   - No Blender and no bpy.
 *   - No MCP execution.
 *   - No worker loop and no claiming logic (only the claim SHAPE is defined).
 *   - No agent provider.
 *   - No Redis and no persistence. `JobStore` below is an interface only; its
 *     implementation belongs to the queue/persistence task.
 *
 * Direction of truth:
 *
 *   packages/contracts/schemas/*.schema.json   <-- CANONICAL
 *            |
 *            +--> TypeScript representation (this module)
 *            +--> Python representation (studio_contracts.jobs)
 */

import { createHash } from "node:crypto";

import type {
  Job,
  JobClaim,
  JobStatus,
  JobType,
  MoveObjectJob,
  MoveObjectPayload,
  ObjectRef,
  RequestOrigin,
  Vec3,
} from "@studio/types";
import { JOB_STATUSES, JOB_TYPES, TERMINAL_JOB_STATUSES } from "@studio/types";
import { isFiniteVec3, isNonEmptyString } from "@studio/validation";

import type { ChatError } from "./index.ts";
import { SCHEMA_FILES, validateAgainstSchema } from "./index.ts";

// ---------------------------------------------------------------------------
// Guard against unresolved natural language reaching the worker
// ---------------------------------------------------------------------------

/**
 * Payload keys that would indicate semantic resolution did not happen.
 *
 * The canonical payload schemas already declare `additionalProperties: false`,
 * so these keys cannot pass schema validation. This list is defense in depth: it
 * produces a precise, actionable error instead of a generic "additional property"
 * violation, and it documents exactly which concepts must never survive into a
 * job. Resolution belongs to packages/spatial and the agent.
 */
export const UNRESOLVED_PAYLOAD_KEYS: readonly string[] = [
  "unit",
  "units",
  "distance",
  "distance_cm",
  "direction",
  "axis",
  "measurement",
  "phrase",
  "text",
  "instruction",
  "message",
  "relative_to",
  "reference",
] as const;

export const JOB_MESSAGES = {
  MISSING_PROJECT_ID: "project_id is required; a job is not executable without it",
  UNRESOLVED_PAYLOAD:
    "payload contains unresolved language or unit fields; units and directions must be resolved to canonical meters before a job is created",
  NON_FINITE_DELTA:
    "delta_meters components must be finite numbers of meters",
  INVALID_TARGET:
    "target must provide a non-blank object_id or name",
  UNSUPPORTED_JOB_TYPE: `unsupported job_type; expected one of: ${JOB_TYPES.join(", ")}`,
  INVALID_STATUS: `unsupported status; expected one of: ${JOB_STATUSES.join(", ")}`,
  MISSING_REQUEST_ID:
    "origin.request_id is required; mutation identity is derived from the originating request",
  INVALID_OPERATION_INDEX:
    "origin.operation_index must be a non-negative integer",
} as const;

// ---------------------------------------------------------------------------
// Deterministic canonical encoding (for idempotency keys)
// ---------------------------------------------------------------------------

/**
 * Encode a double as its exact big-endian IEEE-754 bit pattern in hex.
 *
 * Decimal text formatting differs between languages in edge cases (JS renders
 * 1e-7 as "1e-7", Python as "1e-07"), which would make idempotency keys diverge.
 * Hashing the raw bits removes formatting from the equation entirely.
 */
export function encodeFloat64(value: number): string {
  if (!Number.isFinite(value)) {
    throw new Error("cannot canonicalize a non-finite number");
  }
  // Normalize -0 to 0 so the two zeroes cannot produce different keys.
  const normalized = value === 0 ? 0 : value;
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, normalized, false);
  let hex = "";
  for (let i = 0; i < 8; i += 1) {
    hex += view.getUint8(i).toString(16).padStart(2, "0");
  }
  return hex;
}

/**
 * Deterministic, unambiguous encoding of a JSON-like value.
 *
 * Object keys are sorted, strings are length-prefixed (so no delimiter can be
 * forged by content), and numbers use their exact bit pattern. The Python
 * implementation produces byte-identical output; parity tests assert it.
 */
export function canonicalize(value: unknown): string {
  if (value === null) return "n";
  if (value === undefined) return "u";
  if (typeof value === "boolean") return value ? "b:1" : "b:0";
  if (typeof value === "number") return `f:${encodeFloat64(value)}`;
  if (typeof value === "string") return `s:${value.length}:${value}`;
  if (Array.isArray(value)) {
    return `a:[${value.map((v) => canonicalize(v)).join(",")}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([k, v]) => `s:${k.length}:${k}=${canonicalize(v)}`);
    return `o:{${entries.join(",")}}`;
  }
  throw new Error(`cannot canonicalize value of type ${typeof value}`);
}

/**
 * The identity of a MUTATION, for duplicate protection.
 *
 * Deliberately excludes the payload. A content-derived key would conflate two
 * different situations:
 *
 *   - "Move Cube 50 cm right."        (request A)
 *   - "Move Cube 50 cm right again."  (request B, later)
 *
 * Both resolve to the identical payload {x: 0.5, y: 0, z: 0}, yet they are two
 * intentional mutations and must both execute. Identity therefore comes from the
 * originating submission, not from what the operation happens to look like.
 */
export interface IdempotencyInput {
  project_id: string;
  request_id: string;
  operation_index: number;
}

/**
 * Version tag so the derivation can evolve without silently colliding.
 * v2 = origin-derived (v1 was content-derived and did not distinguish a retry
 * from a repeated intentional command).
 */
export const IDEMPOTENCY_VERSION = "v2";

/** Version tag for the diagnostic content fingerprint. */
export const CONTENT_FINGERPRINT_VERSION = "v1";

/**
 * Derive the deterministic MUTATION IDENTITY of a job.
 *
 * Scoped to a project, so the same request_id in two projects yields two
 * identities and project isolation holds.
 *
 * Retry semantics: resubmitting request_id `req_abc123` operation 0 derives the
 * same key, so the queue layer resolves it to the existing job and Blender is
 * mutated once. A new submission `req_xyz789` operation 0 derives a different
 * key and executes again, even with a byte-identical payload.
 */
export function deriveIdempotencyKey(input: IdempotencyInput): string {
  if (!Number.isInteger(input.operation_index) || input.operation_index < 0) {
    throw new Error("operation_index must be a non-negative integer");
  }
  const canonical = canonicalize({
    v: IDEMPOTENCY_VERSION,
    project_id: input.project_id,
    request_id: input.request_id,
    operation_index: input.operation_index,
  });
  return `idem_${createHash("sha256").update(canonical, "utf8").digest("hex")}`;
}

/**
 * Derive a DIAGNOSTIC fingerprint of what the job does.
 *
 * Useful for spotting repeated or accidentally duplicated operations in logs and
 * tests. This must never be used as the mutation identity: identical content
 * from two distinct requests is legitimate and has to execute twice.
 */
export function deriveContentFingerprint(
  job_type: JobType,
  payload: unknown,
): string {
  const canonical = canonicalize({
    v: CONTENT_FINGERPRINT_VERSION,
    job_type,
    payload,
  });
  return `fp_${createHash("sha256").update(canonical, "utf8").digest("hex")}`;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

export interface JobValidationResult {
  valid: boolean;
  errors: ChatError[];
}

function invalid(errors: ChatError[]): JobValidationResult {
  return { valid: false, errors };
}

/** True when the object reference identifies something usable. */
export function isValidObjectRef(target: unknown): target is ObjectRef {
  if (target === null || typeof target !== "object" || Array.isArray(target)) {
    return false;
  }
  const t = target as ObjectRef;
  return isNonEmptyString(t.object_id) || isNonEmptyString(t.name);
}

/**
 * Validate a job against the canonical schema plus the rules JSON Schema cannot
 * express (non-finite numbers, and the unresolved-language guard).
 */
export function validateJob(job: unknown): JobValidationResult {
  const errors: ChatError[] = [];

  if (job === null || typeof job !== "object" || Array.isArray(job)) {
    return invalid([
      { code: "VALIDATION_ERROR", message: "job must be an object" },
    ]);
  }
  const j = job as Partial<Job>;

  // Project isolation is checked explicitly so the failure is unambiguous.
  if (!isNonEmptyString(j.project_id)) {
    errors.push({
      code: "VALIDATION_ERROR",
      message: JOB_MESSAGES.MISSING_PROJECT_ID,
    });
  }

  const schemaResult = validateAgainstSchema(SCHEMA_FILES.Job, toJobWire(j));
  if (!schemaResult.valid) {
    for (const violation of schemaResult.violations) {
      errors.push({
        code: "VALIDATION_ERROR",
        message: `${violation.path || "job"}: ${violation.message}`,
      });
    }
  }

  // Unresolved-language guard, reported precisely.
  const payload = j.payload as Record<string, unknown> | undefined;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const offending = Object.keys(payload).filter((k) =>
      UNRESOLVED_PAYLOAD_KEYS.includes(k),
    );
    if (offending.length > 0) {
      errors.push({
        code: "VALIDATION_ERROR",
        message: `${JOB_MESSAGES.UNRESOLVED_PAYLOAD} (found: ${offending.sort().join(", ")})`,
      });
    }
  }

  // Operation-specific runtime rules.
  if (j.job_type === "move_object" && payload) {
    const movePayload = payload as unknown as MoveObjectPayload;
    if (!isValidObjectRef(movePayload.target)) {
      errors.push({
        code: "VALIDATION_ERROR",
        message: JOB_MESSAGES.INVALID_TARGET,
      });
    }
    if (!isFiniteVec3(movePayload.delta_meters)) {
      errors.push({
        code: "INVALID_UNITS",
        message: JOB_MESSAGES.NON_FINITE_DELTA,
      });
    }
  }

  return errors.length === 0 ? { valid: true, errors: [] } : invalid(errors);
}

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

export interface CreateMoveObjectJobInput {
  job_id: string;
  project_id: string;
  session_id: string;
  user_id: string;
  /** Identity of the originating submission; basis of the idempotency key. */
  request_id: string;
  /** Position of this operation within that submission. Defaults to 0. */
  operation_index?: number;
  target: ObjectRef;
  /** Already resolved to canonical meters. */
  delta_meters: Vec3;
  created_at: string;
}

export type CreateJobResult =
  | { ok: true; job: MoveObjectJob }
  | { ok: false; errors: ChatError[] };

/**
 * Assemble a queued move_object job.
 *
 * Deterministic by construction: `job_id`, `created_at`, and the originating
 * `request_id` are supplied by the caller rather than generated here, so no clock
 * or randomness leaks into the contract layer and tests are reproducible.
 *
 * The delta must already be resolved — this function accepts meters only. There
 * is no code path here that takes a unit or a direction.
 */
export function createMoveObjectJob(
  input: CreateMoveObjectJobInput,
): CreateJobResult {
  const payload: MoveObjectPayload = {
    target: input.target,
    delta_meters: input.delta_meters,
  };

  // Derive keys only once the payload is known-finite, since canonicalize
  // refuses non-finite numbers.
  if (!isFiniteVec3(input.delta_meters)) {
    return {
      ok: false,
      errors: [
        { code: "INVALID_UNITS", message: JOB_MESSAGES.NON_FINITE_DELTA },
      ],
    };
  }

  const operation_index = input.operation_index ?? 0;
  if (!Number.isInteger(operation_index) || operation_index < 0) {
    return {
      ok: false,
      errors: [
        { code: "VALIDATION_ERROR", message: JOB_MESSAGES.INVALID_OPERATION_INDEX },
      ],
    };
  }
  if (!isNonEmptyString(input.request_id)) {
    return {
      ok: false,
      errors: [
        { code: "VALIDATION_ERROR", message: JOB_MESSAGES.MISSING_REQUEST_ID },
      ],
    };
  }

  const origin: RequestOrigin = {
    request_id: input.request_id,
    operation_index,
  };

  const job: MoveObjectJob = {
    job_id: input.job_id,
    job_type: "move_object",
    project_id: input.project_id,
    session_id: input.session_id,
    user_id: input.user_id,
    payload,
    origin,
    status: "queued",
    created_at: input.created_at,
    idempotency_key: deriveIdempotencyKey({
      project_id: input.project_id,
      request_id: origin.request_id,
      operation_index: origin.operation_index,
    }),
    content_fingerprint: deriveContentFingerprint("move_object", payload),
  };

  const validation = validateJob(job);
  if (!validation.valid) return { ok: false, errors: validation.errors };
  return { ok: true, job };
}

// ---------------------------------------------------------------------------
// Lifecycle transitions (pure; no queue, no persistence)
// ---------------------------------------------------------------------------

/** Allowed lifecycle transitions. */
export const ALLOWED_TRANSITIONS: Readonly<Record<JobStatus, readonly JobStatus[]>> =
  Object.freeze({
    queued: Object.freeze(["claimed", "failed"]),
    claimed: Object.freeze(["running", "failed"]),
    running: Object.freeze(["succeeded", "failed"]),
    succeeded: Object.freeze([]),
    failed: Object.freeze([]),
  }) as Readonly<Record<JobStatus, readonly JobStatus[]>>;

export function isTerminal(status: JobStatus): boolean {
  return TERMINAL_JOB_STATUSES.includes(status);
}

export function canTransition(from: JobStatus, to: JobStatus): boolean {
  const allowed = ALLOWED_TRANSITIONS[from];
  return allowed !== undefined && allowed.includes(to);
}

// ---------------------------------------------------------------------------
// Duplicate queue delivery (pure decision logic; no queue, no persistence)
// ---------------------------------------------------------------------------

/**
 * What a worker should do when a job is delivered to it.
 *
 * `job_id` is the execution identity. A queue may deliver the same job_id more
 * than once (at-least-once delivery, redelivery after a lost ack, a duplicated
 * message). The worker must decide from the job's recorded state, not from the
 * delivery itself, so a redelivery never mutates Blender twice.
 */
export type DeliveryDecision =
  | "execute"
  | "already_owned"
  | "reuse_result";

/**
 * Classify a delivery from the currently recorded state of that job_id.
 *
 *   - not recorded / queued -> execute (this is the first real attempt)
 *   - claimed / running     -> already_owned (another worker or attempt holds it)
 *   - succeeded / failed    -> reuse_result (terminal; never re-execute)
 *
 * Pure and deterministic. Enforcement (lease expiry, reclaim of an abandoned
 * claim) belongs to the worker/queue task; this fixes the decision rule.
 */
export function classifyDelivery(
  recordedStatus: JobStatus | null | undefined,
): DeliveryDecision {
  if (recordedStatus === null || recordedStatus === undefined) return "execute";
  if (recordedStatus === "queued") return "execute";
  if (recordedStatus === "claimed" || recordedStatus === "running") {
    return "already_owned";
  }
  return "reuse_result";
}

/** True when a delivery must not cause a Blender mutation. */
export function isDuplicateDelivery(
  recordedStatus: JobStatus | null | undefined,
): boolean {
  return classifyDelivery(recordedStatus) !== "execute";
}

// ---------------------------------------------------------------------------
// Serialization
// ---------------------------------------------------------------------------

/** Strip undefined optional fields so a job becomes a wire document. */
export function toJobWire(job: unknown): Record<string, unknown> {
  return JSON.parse(JSON.stringify(job)) as Record<string, unknown>;
}

/** Serialize a job to a canonical JSON string. */
export function serializeJob(job: Job): string {
  return JSON.stringify(toJobWire(job));
}

export type ParseJobResult =
  | { ok: true; job: Job }
  | { ok: false; errors: ChatError[] };

/** Parse and validate a job from its wire representation. */
export function parseJob(serialized: string): ParseJobResult {
  let parsed: unknown;
  try {
    parsed = JSON.parse(serialized);
  } catch {
    return {
      ok: false,
      errors: [{ code: "VALIDATION_ERROR", message: "job is not valid JSON" }],
    };
  }
  const validation = validateJob(parsed);
  if (!validation.valid) return { ok: false, errors: validation.errors };
  return { ok: true, job: parsed as Job };
}

// ---------------------------------------------------------------------------
// Persistence boundary (INTERFACE ONLY — implemented in a later task)
// ---------------------------------------------------------------------------

/**
 * The durable job store boundary.
 *
 * Deliberately an interface with no implementation: Redis/PostgreSQL wiring
 * belongs to the queue/persistence task. Defining it now keeps the logical
 * boundary explicit from the start (see .kiro/steering/structure.md).
 *
 * Duplicate-job protection contract that an implementation MUST honour:
 *
 *   1. `findByIdempotencyKey` is scoped to a project. Keys are never compared
 *      across projects, preserving project isolation.
 *   2. `submit` must be atomic: if a job with the same (project_id,
 *      idempotency_key) already exists, return that job with duplicate=true and
 *      enqueue nothing.
 *   3. Because the key is derived from (project_id, request_id, operation_index),
 *      a RETRY of the same originating request resolves to the existing job,
 *      while a NEW request with an identical payload creates a second job. Both
 *      behaviours are required: the first prevents double mutation, the second
 *      allows a user to legitimately repeat a command.
 *   4. A duplicate submission must never produce a second Blender mutation.
 */
export interface JobStore {
  get(project_id: string, job_id: string): Promise<Job | null>;
  findByIdempotencyKey(
    project_id: string,
    idempotency_key: string,
  ): Promise<Job | null>;
  /** Atomically insert, or return the existing job for the same key. */
  submit(job: Job): Promise<{ job: Job; duplicate: boolean }>;
}

/**
 * The worker-facing claiming boundary.
 *
 * Interface only. The claiming loop, leases, and reclaim-after-expiry behaviour
 * belong to the worker task; this fixes the shape they must speak.
 *
 * Duplicate-delivery contract that an implementation MUST honour: before
 * executing a delivered job, consult the recorded status for its `job_id` and
 * apply `classifyDelivery`. A job already `claimed`/`running` must not be
 * executed again, and a terminal job must return its recorded outcome instead of
 * re-running.
 */
export interface JobClaimer {
  claim(worker_id: string, project_id: string): Promise<Job | null>;
  renew(project_id: string, job_id: string, claim: JobClaim): Promise<void>;
  release(project_id: string, job_id: string): Promise<void>;
}
