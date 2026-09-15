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
 *   Vec3 -> vec3.schema.json
 *   Job  -> job.schema.json
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
