/**
 * Reusable validation rules for AI 3D Design Studio — TypeScript representation.
 *
 * These rules implement the runtime enforcement of the canonical contract
 * schemas in packages/contracts/schemas. The canonical schema is authoritative;
 * conformance tests cross-check these rules against it over a shared,
 * language-neutral case corpus (schemas/conformance-cases.json) so the two
 * language implementations cannot drift apart or away from the schema.
 *
 * Rules kept small and pure so they can run on both the web/API edge and inside
 * services.
 *
 * Covered by this slice (Spec 001, Task 1):
 *   - project_id presence on inbound requests  (chat-request.schema.json)
 *   - finite meter values for distances/coordinates (vec3.schema.json + the
 *     finite-meters rule, which JSON Schema cannot express since JSON has no
 *     NaN/Infinity literals)
 */

import type { ChatRequest } from "@studio/contracts";
import type { Vec3 } from "@studio/types";

export interface ValidationError {
  field: string;
  message: string;
}

export interface ValidationResult {
  valid: boolean;
  errors: ValidationError[];
}

const ok: ValidationResult = { valid: true, errors: [] };

function fail(errors: ValidationError[]): ValidationResult {
  return { valid: false, errors };
}

/** A non-empty, non-whitespace string. */
export function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * A finite number in meters: a real, finite JS number.
 * Rejects NaN, Infinity, -Infinity, and non-numbers.
 */
export function isFiniteMeters(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Every component of a Vec3 must be finite meters. */
export function isFiniteVec3(value: unknown): value is Vec3 {
  if (value === null || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    isFiniteMeters(v.x) && isFiniteMeters(v.y) && isFiniteMeters(v.z)
  );
}

/**
 * The canonical ChatRequest property set (chat-request.schema.json declares
 * `additionalProperties: false`). Declared locally so this module stays pure and
 * browser-safe; conformance tests assert it equals the canonical schema's
 * property list, so it cannot drift.
 */
export const CHAT_REQUEST_FIELDS: readonly string[] = [
  "message",
  "project_id",
  "request_id",
  "selected_object_id",
  "session_id",
] as const;

/**
 * Validate an inbound ChatRequest against the canonical contract.
 *
 * Enforces Requirement 2.3: a request missing project_id is rejected.
 * Also requires request_id (the stable submission identity that makes retry
 * semantics possible), session_id, and a non-empty message, and rejects unknown
 * properties to match the canonical schema.
 */
export function validateChatRequest(input: unknown): ValidationResult {
  if (input === null || typeof input !== "object" || Array.isArray(input)) {
    return fail([{ field: "_root", message: "request body must be an object" }]);
  }
  const req = input as Partial<ChatRequest>;
  const errors: ValidationError[] = [];

  if (!isNonEmptyString(req.request_id)) {
    errors.push({ field: "request_id", message: "request_id is required" });
  }
  if (!isNonEmptyString(req.project_id)) {
    errors.push({ field: "project_id", message: "project_id is required" });
  }
  if (!isNonEmptyString(req.session_id)) {
    errors.push({ field: "session_id", message: "session_id is required" });
  }
  if (!isNonEmptyString(req.message)) {
    errors.push({ field: "message", message: "message is required" });
  }
  if (
    req.selected_object_id !== undefined &&
    !isNonEmptyString(req.selected_object_id)
  ) {
    errors.push({
      field: "selected_object_id",
      message: "selected_object_id, when provided, must be a non-empty string",
    });
  }
  for (const key of Object.keys(input)) {
    if (!CHAT_REQUEST_FIELDS.includes(key)) {
      errors.push({ field: key, message: `unknown property '${key}' is not allowed` });
    }
  }

  return errors.length === 0 ? ok : fail(errors);
}

/**
 * Validate a set of movement deltas in meters.
 * Enforces the "finite meters" rule used by the MCP move_object tool.
 */
export function validateMeterDeltas(deltas: {
  delta_x_m?: unknown;
  delta_y_m?: unknown;
  delta_z_m?: unknown;
}): ValidationResult {
  const errors: ValidationError[] = [];
  for (const key of ["delta_x_m", "delta_y_m", "delta_z_m"] as const) {
    const value = deltas[key];
    if (value !== undefined && !isFiniteMeters(value)) {
      errors.push({ field: key, message: `${key} must be a finite number of meters` });
    }
  }
  return errors.length === 0 ? ok : fail(errors);
}
