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
 *   Vec3          -> vec3.schema.json
 *   Job           -> job.schema.json
 *   LengthUnit    -> length-unit.schema.json
 *   Measurement   -> measurement.schema.json
 *   Axis          -> axis.schema.json
 *   Direction     -> direction.schema.json
 *   AxisDirection -> axis-direction.schema.json
 */

/** A 3D vector in canonical meters. */
export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

/** Lifecycle states for a Job (see .kiro/steering/architecture.md). */
export type JobStatus = "queued" | "running" | "succeeded" | "failed";

/** Job types supported by the platform. Extend as new operations land. */
export type JobType = "chat";

/**
 * A unit of work explicitly bound to a project.
 *
 * Every job MUST identify its project (see .kiro/steering/security.md —
 * a worker must never accidentally modify another project's Blender file).
 */
export interface Job {
  id: string;
  project_id: string;
  session_id: string;
  type: JobType;
  status: JobStatus;
  created_at: string; // ISO-8601 timestamp
  result?: unknown;
}

export const JOB_STATUSES: readonly JobStatus[] = [
  "queued",
  "running",
  "succeeded",
  "failed",
] as const;

export const JOB_TYPES: readonly JobType[] = ["chat"] as const;

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
