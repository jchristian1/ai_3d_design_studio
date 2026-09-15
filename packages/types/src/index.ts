/**
 * Shared data types for AI 3D Design Studio — TypeScript representation.
 *
 * Canonical unit is METERS (see .kiro/steering/blender.md).
 *
 * Direction of truth:
 *
 *   packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
 *            |
 *            +--> TypeScript representation  (this file)
 *            +--> Python representation      (studio_types)
 *
 * Canonical schemas:
 *   Vec3              -> vec3.schema.json
 *   Job               -> job.schema.json
 *   JobType           -> job-type.schema.json
 *   JobClaim          -> job-claim.schema.json
 *   ObjectRef         -> object-ref.schema.json
 *   MoveObjectPayload -> move-object-payload.schema.json
 *   LengthUnit        -> length-unit.schema.json
 *   Measurement       -> measurement.schema.json
 *   Axis              -> axis.schema.json
 *   Direction         -> direction.schema.json
 *   AxisDirection     -> axis-direction.schema.json
 */

/** A 3D vector in canonical meters. */
export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

/** Lifecycle states for a Job (see .kiro/steering/architecture.md). */
export type JobStatus = "queued" | "claimed" | "running" | "succeeded" | "failed";

/**
 * The operation a Job carries. This is the discriminator of the job union:
 * each job_type binds to exactly one payload type.
 *
 * Reserved for future milestones (each needs a payload schema + a conditional in
 * job.schema.json before being added): resize_object, set_material, create_wall,
 * create_opening, set_light, create_camera, render_preview, save_version.
 *
 * Canonical schema: job-type.schema.json
 */
export type JobType = "move_object";

/**
 * A reference to a scene object. At least one of object_id or name is present.
 * Canonical schema: object-ref.schema.json
 */
export interface ObjectRef {
  object_id?: string;
  name?: string;
}

/**
 * A fully resolved translation. There is no unit field and no direction field,
 * so unresolved language ("50 cm", "right") is unrepresentable.
 * Canonical schema: move-object-payload.schema.json
 */
export interface MoveObjectPayload {
  target: ObjectRef;
  delta_meters: Vec3;
}

/** Maps each job_type to its payload type. Extend alongside JobType. */
export interface JobPayloadByType {
  move_object: MoveObjectPayload;
}

/**
 * Worker ownership metadata, set when a worker claims a job.
 * Canonical schema: job-claim.schema.json
 */
export interface JobClaim {
  worker_id: string;
  claimed_at: string;
  lease_expires_at: string;
}

/**
 * The originating user/API submission a job came from.
 *
 * This is the source of MUTATION IDENTITY. A retry reuses the same request_id
 * (so it executes once); a genuinely new command gets a new request_id (so it
 * executes again) even when the resolved payload is byte-identical.
 *
 * Canonical schema: request-origin.schema.json
 */
export interface RequestOrigin {
  request_id: string;
  /** Zero-based position of this operation within the originating request. */
  operation_index: number;
}

/**
 * The durable unit of work carrying a fully resolved operation toward the
 * Blender Worker.
 *
 * Generic over job_type so `payload` is typed per operation rather than being an
 * untyped dictionary. `Job` (the default) is the discriminated union over all
 * job types.
 *
 * project_id is mandatory: a worker must never modify another project's Blender
 * file (see .kiro/steering/security.md).
 *
 * Two identities are distinguished:
 *   - `job_id` identifies THIS job record, and is what a worker deduplicates
 *     duplicate queue deliveries on.
 *   - `idempotency_key` is the mutation identity derived from `origin`.
 *
 * Canonical schema: job.schema.json
 */
export interface JobOf<T extends JobType> {
  job_id: string;
  job_type: T;
  project_id: string;
  session_id: string;
  user_id: string;
  payload: JobPayloadByType[T];
  /** Where this job came from; the basis of its idempotency identity. */
  origin: RequestOrigin;
  status: JobStatus;
  created_at: string; // ISO-8601 timestamp
  /** Mutation identity: derived from project + origin, NOT from payload. */
  idempotency_key: string;
  /** Diagnostics only: canonical hash of (job_type, payload). */
  content_fingerprint?: string;
  claim?: JobClaim;
  error?: { code: string; message: string };
  result?: unknown;
}

export type MoveObjectJob = JobOf<"move_object">;

/** The discriminated union over every supported job type. */
export type Job = MoveObjectJob;

export const JOB_STATUSES: readonly JobStatus[] = [
  "queued",
  "claimed",
  "running",
  "succeeded",
  "failed",
] as const;

export const JOB_TYPES: readonly JobType[] = ["move_object"] as const;

/** Statuses in which a worker owns the job. */
export const OWNED_JOB_STATUSES: readonly JobStatus[] = [
  "claimed",
  "running",
] as const;

/** Statuses from which no further transition is allowed. */
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = [
  "succeeded",
  "failed",
] as const;

// ---------------------------------------------------------------------------
// Spatial vocabulary (Spec 001, Task 2)
// ---------------------------------------------------------------------------

/**
 * Units accepted at an input boundary. Meters is the canonical internal unit;
 * centimeters exist so user-facing measurements are converted once, at the edge.
 * Canonical schema: length-unit.schema.json
 */
export type LengthUnit = "m" | "cm";

/**
 * A distance with an explicit unit, before conversion to canonical meters.
 * Canonical schema: measurement.schema.json
 */
export interface Measurement {
  value: number;
  unit: LengthUnit;
}

/**
 * A Blender WORLD-SPACE axis. World-space only for this milestone;
 * camera-relative interpretation is explicitly deferred.
 * Canonical schema: axis.schema.json
 */
export type Axis = "x" | "y" | "z";

/**
 * A named WORLD-SPACE direction. Not camera-relative.
 * Canonical schema: direction.schema.json
 */
export type Direction = "right" | "left" | "forward" | "back" | "up" | "down";

/** +1 along the axis, -1 against it. */
export type AxisSign = 1 | -1;

/**
 * The resolved result of interpreting a Direction: axis plus sign.
 * Carries no distance and never touches Blender.
 * Canonical schema: axis-direction.schema.json
 */
export interface AxisDirection {
  axis: Axis;
  sign: AxisSign;
}

export const LENGTH_UNITS: readonly LengthUnit[] = ["m", "cm"] as const;

export const AXES: readonly Axis[] = ["x", "y", "z"] as const;

export const DIRECTIONS: readonly Direction[] = [
  "right",
  "left",
  "forward",
  "back",
  "up",
  "down",
] as const;

export const AXIS_SIGNS: readonly AxisSign[] = [-1, 1] as const;
