/**
 * Scene description contracts and the AUTHORITATIVE scene-version digest.
 *
 * Spec 002, Task 1.
 *
 * This module mirrors studio_contracts/scene.py rule for rule. There is exactly
 * one definition of "scene state digest" in the repository; a second, subtly
 * different one would mean two answers to "did the scene change?", which is the
 * one question the Spec 002 execution precondition depends on.
 *
 * Direction of truth:
 *
 *   packages/contracts/schemas/*.schema.json   <-- CANONICAL
 *            |
 *            +--> TypeScript representation (this file)
 *            +--> Python representation      (studio_contracts.scene)
 *
 * Why the digest is a PROJECTION and not a hash of the snapshot:
 *
 *   1. `captured_at` changes on every read, so hashing the document would make two
 *      reads of an unchanged scene disagree and every plan would be refused.
 *   2. Worse, a deny-list ("hash everything except captured_at") makes concurrency
 *      semantics change SILENTLY: a future informational field would change every
 *      project's scene_version and invalidate in-flight plans for no design reason.
 *
 * So the digest input is an explicit allow-list keyed by a digest version.
 */

import { createHash } from "node:crypto";

import type { ChatError } from "./index.ts";
import { SCHEMA_FILES, validateAgainstSchema } from "./index.ts";

// ---------------------------------------------------------------------------
// Digest version and projection membership
// ---------------------------------------------------------------------------

/**
 * The digest projection identity. It is hashed INSIDE the payload, so it is real
 * domain separation rather than a comment. Bumping it invalidates every recorded
 * scene_version exactly once and refuses every in-flight plan, which is why it is
 * only bumped deliberately.
 */
export const SCENE_DIGEST_VERSION = "studio-scene-v1";

/** Snapshot fields that ARE part of the digest. */
export const SCENE_SNAPSHOT_DIGEST_FIELDS: readonly string[] = [
  "units",
  "objects",
] as const;

/**
 * Snapshot fields deliberately EXCLUDED from the digest.
 *
 *   captured_at   — capture metadata; changes on every read.
 *   scene_version — the digest itself; cannot be its own input.
 *   project_id    — the digest answers "is this the same scene state?", not "whose
 *                   scene is this?". Authorization and cache keying are already
 *                   project-scoped elsewhere, and excluding it keeps the useful
 *                   property that an identical scene digests identically
 *                   regardless of which project holds it. Domain separation comes
 *                   from digest_version, which is what it is for.
 */
export const SCENE_SNAPSHOT_INFORMATIONAL_FIELDS: readonly string[] = [
  "captured_at",
  "project_id",
  "scene_version",
] as const;

/** Object fields that ARE part of the digest. */
export const SCENE_OBJECT_DIGEST_FIELDS: readonly string[] = [
  "studio_object_id",
  "name",
  "object_type",
  "world_position_meters",
  "dimensions_meters",
  "rotation_euler_radians",
  "scale",
  "visible",
  "material",
] as const;

/**
 * Object fields excluded from the digest. Empty today: every field the snapshot
 * exposes about an object is something the model may plan against.
 */
export const SCENE_OBJECT_INFORMATIONAL_FIELDS: readonly string[] = [] as const;

/** Unit fields that ARE part of the digest. All of them. */
export const SCENE_UNITS_DIGEST_FIELDS: readonly string[] = [
  "unit_system",
  "length_unit",
  "scale_length",
] as const;

/**
 * Names that must never become representable in a scene contract. Asserted
 * against the canonical schemas by the security contract tests, so this is
 * documentation of an enforced property rather than a hopeful convention.
 */
export const FORBIDDEN_SCENE_FIELDS: readonly string[] = [
  "blend_path",
  "blendfile",
  "code",
  "command",
  "directory",
  "env",
  "environment",
  "expression",
  "filename",
  "filepath",
  "host",
  "hostname",
  "metadata",
  "path",
  "pointer",
  "python",
  "script",
  "secret",
  "session_id",
  "shell",
  "token",
  "url",
  "worker_id",
  "worker_token",
] as const;

// ---------------------------------------------------------------------------
// Deterministic numeric representation
// ---------------------------------------------------------------------------

/**
 * Decimal places every quantised value is formatted with. One quantum for all
 * quantities, chosen so the rule is impossible to misapply: 1e-6 is 1 micrometre
 * for a length, ~0.2 arcseconds for an angle, and one part in a million for a
 * unitless factor or a colour channel.
 *
 * Quantisation removes REPRESENTATION noise, not change. What the shared
 * fixed-decimal form buys is that TypeScript and Python agree exactly, with no
 * dependence on either language's float repr (Python renders 1.0 as "1.0",
 * JavaScript as "1") — which is why quantised values are serialised as
 * fixed-decimal STRINGS below rather than as JSON numbers.
 */
export const DIGEST_DECIMALS = 6;

/** The quantum implied by DIGEST_DECIMALS, stated explicitly. */
export const DIGEST_QUANTUM = 1e-6;

/**
 * Raised when scene state cannot be digested deterministically. A hard error
 * rather than a serialised token: an unreadable scene must be refused, never
 * hashed into a value that looks authoritative.
 */
export class SceneDigestError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SceneDigestError";
  }
}

/**
 * Round half to even at `decimals` places.
 *
 * Spelled out rather than using `toFixed`, which rounds half away from zero: the
 * two languages must apply the same tie rule or a value sitting exactly on a
 * quantum boundary would digest differently.
 */
function roundHalfEven(value: number, decimals: number): number {
  const scale = 10 ** decimals;
  const scaled = value * scale;
  const floor = Math.floor(scaled);
  const diff = scaled - floor;
  let rounded: number;
  if (diff > 0.5) {
    rounded = floor + 1;
  } else if (diff < 0.5) {
    rounded = floor;
  } else {
    rounded = floor % 2 === 0 ? floor : floor + 1;
  }
  return rounded / scale;
}

/**
 * Quantise a number and format it as a fixed-decimal string.
 *
 * Rules, all of which exist to make two implementations agree byte for byte:
 *   - round half to even at the quantum;
 *   - always exactly DIGEST_DECIMALS decimals, never exponent notation;
 *   - negative zero normalises to "0.000000", so -0.0 and 0.0 cannot produce
 *     different scene versions (Blender reports unset Euler components as -0.0);
 *   - non-finite values throw.
 */
export function formatDigestNumber(value: unknown): string {
  if (typeof value !== "number") {
    throw new SceneDigestError(
      `scene state contains a non-numeric value where a number is required: ${typeof value}`,
    );
  }
  if (!Number.isFinite(value)) {
    throw new SceneDigestError(
      "scene state contains a non-finite number (NaN or Infinity); an unreadable " +
        "scene is refused rather than digested",
    );
  }
  const quantised = roundHalfEven(value, DIGEST_DECIMALS);
  const text = quantised.toFixed(DIGEST_DECIMALS);
  // Normalise every representation of zero, including the "-0.000000" a tiny
  // negative value produces once quantised.
  return Number(text) === 0 ? `0.${"0".repeat(DIGEST_DECIMALS)}` : text;
}

// ---------------------------------------------------------------------------
// Canonical JSON
// ---------------------------------------------------------------------------

function sortDeep(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sortDeep);
  }
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(source).sort()) {
      out[key] = sortDeep(source[key]);
    }
    return out;
  }
  return value;
}

/**
 * Serialize the digest projection deterministically: UTF-8 text, object keys
 * sorted lexicographically, no insignificant whitespace, JSON literals for
 * booleans and null, and no non-ASCII escaping.
 *
 * All numbers in the projection are already fixed-decimal STRINGS produced by
 * `formatDigestNumber`, so this function never has to render a float.
 */
export function canonicalDigestJson(value: unknown): string {
  return JSON.stringify(sortDeep(value));
}

// ---------------------------------------------------------------------------
// The projection
// ---------------------------------------------------------------------------

type Dict = Record<string, unknown>;

function asDict(value: unknown, what: string): Dict {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new SceneDigestError(`${what} must be an object`);
  }
  return value as Dict;
}

function triple(
  source: unknown,
  field: string,
  keys: readonly string[] = ["x", "y", "z"],
): string[] {
  const node = asDict(source, field);
  return keys.map((key) => {
    if (!(key in node)) {
      throw new SceneDigestError(`${field} is missing component '${key}'`);
    }
    return formatDigestNumber(node[key]);
  });
}

/**
 * The documented TOTAL order objects are sorted by before digesting:
 * `(0, studio_object_id)` when a stable id is present, else `(1, name)`.
 *
 * Blender guarantees object names are unique within a file, so the fallback is a
 * genuine total order rather than a tie-break. The caller still verifies keys are
 * strictly increasing: a duplicate key means an ambiguous snapshot, which is
 * refused rather than digested into an arbitrary order.
 */
export function objectSortKey(obj: unknown): [number, string] {
  const wire = asDict(obj, "scene object");
  const objectId = wire.studio_object_id;
  if (typeof objectId === "string" && objectId.trim() !== "") {
    return [0, objectId];
  }
  const name = wire.name;
  if (typeof name !== "string" || name.trim() === "") {
    throw new SceneDigestError(
      "scene object must have a non-blank name to be ordered deterministically",
    );
  }
  return [1, name];
}

function compareSortKeys(a: [number, string], b: [number, string]): number {
  if (a[0] !== b[0]) return a[0] - b[0];
  // Codepoint ordering, matching Python's default string sort. `localeCompare`
  // is locale-sensitive and would disagree.
  return a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0;
}

function materialProjection(material: unknown): Dict | null {
  if (material === null || material === undefined) return null;
  const wire = asDict(material, "material");
  const baseColor = wire.base_color;
  return {
    name: wire.name ?? null,
    base_color:
      baseColor === null || baseColor === undefined
        ? null
        : triple(baseColor, "material.base_color", ["r", "g", "b", "a"]),
  };
}

function objectProjection(obj: unknown): Dict {
  const wire = asDict(obj, "scene object");

  const name = wire.name;
  if (typeof name !== "string" || name.trim() === "") {
    throw new SceneDigestError("scene object requires a non-blank name");
  }
  const objectType = wire.object_type;
  if (typeof objectType !== "string" || objectType.trim() === "") {
    throw new SceneDigestError(`${name}: object_type is required`);
  }
  if (typeof wire.visible !== "boolean") {
    throw new SceneDigestError(`${name}: visible must be a boolean`);
  }
  const studioObjectId = wire.studio_object_id;
  if (
    studioObjectId !== undefined &&
    studioObjectId !== null &&
    typeof studioObjectId !== "string"
  ) {
    throw new SceneDigestError(
      `${name}: studio_object_id must be a string or absent`,
    );
  }

  return {
    // Absent optionals become explicit null so "no material" and "material
    // removed" cannot alias into different projection SHAPES.
    studio_object_id: studioObjectId ?? null,
    name,
    object_type: objectType,
    world_position_meters: triple(
      wire.world_position_meters,
      `${name}.world_position_meters`,
    ),
    dimensions_meters: triple(wire.dimensions_meters, `${name}.dimensions_meters`),
    rotation_euler_radians: triple(
      wire.rotation_euler_radians,
      `${name}.rotation_euler_radians`,
    ),
    scale: triple(wire.scale, `${name}.scale`),
    visible: wire.visible,
    material: materialProjection(wire.material),
  };
}

function unitsProjection(units: unknown): Dict {
  const wire = asDict(units, "units");
  if (typeof wire.unit_system !== "string" || wire.unit_system === "") {
    throw new SceneDigestError("units.unit_system is required");
  }
  if (typeof wire.length_unit !== "string" || wire.length_unit === "") {
    throw new SceneDigestError("units.length_unit is required");
  }
  return {
    unit_system: wire.unit_system,
    length_unit: wire.length_unit,
    scale_length: formatDigestNumber(wire.scale_length),
  };
}

/**
 * Build the exact `studio-scene-v1` digest input.
 *
 * Accepts a SceneSnapshot or any object carrying `units` and `objects`. The last
 * form matters: a snapshot cannot be constructed before its `scene_version` is
 * known, so the producer digests `{units, objects}` first. That the projection is
 * happy without `project_id`, `captured_at` or `scene_version` is the clearest
 * demonstration that it is not "the snapshot".
 */
export function sceneDigestProjection(scene: unknown): Dict {
  const wire = asDict(scene, "scene");
  if (!("units" in wire) || !("objects" in wire)) {
    throw new SceneDigestError("scene must carry 'units' and 'objects'");
  }
  if (!Array.isArray(wire.objects)) {
    throw new SceneDigestError("scene.objects must be an array");
  }

  const ordered = [...(wire.objects as unknown[])].sort((a, b) =>
    compareSortKeys(objectSortKey(a), objectSortKey(b)),
  );
  const keys = ordered.map(objectSortKey);
  for (let i = 1; i < keys.length; i += 1) {
    const previous = keys[i - 1];
    const current = keys[i];
    if (previous && current && compareSortKeys(previous, current) === 0) {
      throw new SceneDigestError(
        `ambiguous scene: two objects share the sort key ${JSON.stringify(current)}; ` +
          "refusing to digest an arbitrary order",
      );
    }
  }

  return {
    digest_version: SCENE_DIGEST_VERSION,
    units: unitsProjection(wire.units),
    objects: ordered.map(objectProjection),
  };
}

/**
 * The authoritative `scene_version` of a semantic scene state.
 *
 * `sha256:<64 hex chars>`, prefixed with its algorithm so a future migration is
 * unambiguous. Deterministic for unchanged semantic state and insensitive to
 * `captured_at`, `project_id`, object enumeration order, and any field outside the
 * projection.
 */
export function computeSceneVersion(scene: unknown): string {
  const canonical = canonicalDigestJson(sceneDigestProjection(scene));
  return `sha256:${createHash("sha256").update(canonical, "utf8").digest("hex")}`;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

export const SCENE_MESSAGES = {
  NON_FINITE:
    "scene state contains a non-finite number; JSON cannot encode NaN or " +
    "Infinity, so the finite rule is enforced here",
  SCALE_LENGTH: "units.scale_length must be a finite number greater than zero",
  UNKNOWN_UNIT_SYSTEM: "units.unit_system must be one of: NONE, METRIC, IMPERIAL",
  DEGENERATE_SCALE:
    "scale components must be non-zero; a zero scale collapses the object and " +
    "is not a recoverable state",
  AMBIGUOUS_ORDER:
    "two objects share the same digest sort key, so the scene cannot be " +
    "digested deterministically",
  VERSION_MISMATCH: "scene_version does not match the scene state it accompanies",
} as const;

export interface SceneValidationResult {
  valid: boolean;
  errors: ChatError[];
}

const UNIT_SYSTEM_VALUES = ["NONE", "METRIC", "IMPERIAL"];

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * Validate a snapshot against the canonical schema plus the rules JSON Schema
 * cannot express: finite numbers, strictly positive `scale_length`, non-zero
 * scale components, deterministic ordering, and optionally that `scene_version`
 * actually matches the state it accompanies.
 */
export function validateSceneSnapshot(
  snapshot: unknown,
  options: { verifySceneVersion?: boolean } = {},
): SceneValidationResult {
  const errors: ChatError[] = [];
  if (snapshot === null || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    return {
      valid: false,
      errors: [
        { code: "VALIDATION_ERROR", message: "scene snapshot must be an object" },
      ],
    };
  }
  const wire = snapshot as Dict;

  const schemaResult = validateAgainstSchema(SCHEMA_FILES.SceneSnapshot, wire);
  for (const violation of schemaResult.violations) {
    errors.push({
      code: "VALIDATION_ERROR",
      message: `${violation.path || "scene_snapshot"}: ${violation.message}`,
    });
  }

  const units = wire.units;
  if (units !== null && typeof units === "object" && !Array.isArray(units)) {
    const u = units as Dict;
    const scaleLength = u.scale_length;
    if (!isFiniteNumber(scaleLength) || scaleLength <= 0) {
      errors.push({ code: "INVALID_UNITS", message: SCENE_MESSAGES.SCALE_LENGTH });
    }
    if (!UNIT_SYSTEM_VALUES.includes(u.unit_system as string)) {
      errors.push({
        code: "INVALID_UNITS",
        message: SCENE_MESSAGES.UNKNOWN_UNIT_SYSTEM,
      });
    }
  }

  const objects = wire.objects;
  if (Array.isArray(objects)) {
    for (const entry of objects) {
      if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
        continue;
      }
      const obj = entry as Dict;
      const label = typeof obj.name === "string" ? obj.name : "<unnamed>";
      for (const field of [
        "world_position_meters",
        "dimensions_meters",
        "rotation_euler_radians",
        "scale",
      ]) {
        const vector = obj[field];
        if (vector === null || typeof vector !== "object" || Array.isArray(vector)) {
          continue;
        }
        for (const axis of ["x", "y", "z"]) {
          if (!isFiniteNumber((vector as Dict)[axis])) {
            errors.push({
              code: "INVALID_UNITS",
              message: `${label}.${field}.${axis}: ${SCENE_MESSAGES.NON_FINITE}`,
            });
          }
        }
      }
      const scale = obj.scale;
      if (scale !== null && typeof scale === "object" && !Array.isArray(scale)) {
        for (const axis of ["x", "y", "z"]) {
          const component = (scale as Dict)[axis];
          if (isFiniteNumber(component) && component === 0) {
            errors.push({
              code: "VALIDATION_ERROR",
              message: `${label}.scale.${axis}: ${SCENE_MESSAGES.DEGENERATE_SCALE}`,
            });
          }
        }
      }
      const material = obj.material as Dict | undefined;
      const baseColor = material?.base_color as Dict | undefined;
      if (baseColor && typeof baseColor === "object") {
        for (const channel of ["r", "g", "b", "a"]) {
          if (!isFiniteNumber(baseColor[channel])) {
            errors.push({
              code: "VALIDATION_ERROR",
              message: `${label}.material.base_color.${channel}: ${SCENE_MESSAGES.NON_FINITE}`,
            });
          }
        }
      }
    }

    let keys: [number, string][] = [];
    try {
      keys = (objects as unknown[]).map(objectSortKey).sort(compareSortKeys);
    } catch {
      keys = [];
    }
    for (let i = 1; i < keys.length; i += 1) {
      const previous = keys[i - 1];
      const current = keys[i];
      if (previous && current && compareSortKeys(previous, current) === 0) {
        errors.push({
          code: "VALIDATION_ERROR",
          message: SCENE_MESSAGES.AMBIGUOUS_ORDER,
        });
        break;
      }
    }
  }

  if (options.verifySceneVersion && errors.length === 0) {
    try {
      const expected = computeSceneVersion(wire);
      if (wire.scene_version !== expected) {
        errors.push({
          code: "SCENE_VERSION_MISMATCH",
          message: SCENE_MESSAGES.VERSION_MISMATCH,
        });
      }
    } catch (exc) {
      errors.push({
        code: "VALIDATION_ERROR",
        message: exc instanceof Error ? exc.message : String(exc),
      });
    }
  }

  return { valid: errors.length === 0, errors };
}

/**
 * Compare two scene versions.
 *
 * A named function rather than `===` at call sites, so the comparison has one
 * place to live if the digest ever gains a migration path, and so a caller can
 * never accidentally compare a version against a truncated or prefixed form.
 */
export function sceneVersionsMatch(left: unknown, right: unknown): boolean {
  if (typeof left !== "string" || typeof right !== "string") return false;
  return left === right;
}
