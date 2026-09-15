/**
 * Deterministic angle-unit conversion — TypeScript representation.
 *
 * Spec 002, Task 2. Canonical internal angular unit is RADIANS.
 *
 * Why radians: Blender's `rotation_euler` is radians, and the Task 5 fixture
 * digest already records `rotation_euler_radians`, so choosing radians means ZERO
 * conversion at the Blender boundary — the place a conversion bug would be most
 * expensive. Degrees are a *language-edge* unit: "45 degrees" is converted exactly
 * once, here, above the worker, exactly as "50 cm" is.
 *
 * Scope and deliberate non-goals:
 *   - Pure arithmetic only. No Blender, no bpy, no I/O, no environment.
 *   - No natural-language parsing and no AI reasoning. Callers supply an
 *     already-structured AngleMeasurement; interpreting prose belongs to the agent.
 *   - CONVERSION IS NOT NORMALIZATION. Nothing here reduces an angle modulo 2π.
 *     Those are different concerns: 720° is a legitimate two-full-turn instruction
 *     and silently collapsing it to 0 would destroy intent. If a future operation
 *     needs a principal value it must ask for it explicitly.
 *
 * Determinism: deg -> rad multiplies by ONE precomputed constant,
 * `RADIANS_PER_DEGREE = Math.PI / 180`. Both TypeScript and Python compute that
 * constant from the same double π with correctly-rounded division, then perform one
 * correctly-rounded multiplication, so results are bit-identical across the two
 * representations. The chosen formula also lands exactly on the expected constants:
 * 45° is exactly π/4, 90° exactly π/2, 180° exactly π (asserted in the tests, not
 * assumed).
 *
 * Canonical schemas: angle-measurement.schema.json, angle-unit.schema.json
 */

import type { ChatError } from "@studio/contracts";
import type { AngleUnit } from "@studio/types";
import { ANGLE_UNITS } from "@studio/types";

/**
 * Degrees in a half turn. Named so the conversion reads as geometry rather than as
 * a magic number.
 */
export const DEGREES_PER_HALF_TURN = 180;

/**
 * The single conversion constant. Computed once from Math.PI so a value is
 * converted with exactly one multiplication (one rounding).
 */
export const RADIANS_PER_DEGREE = Math.PI / DEGREES_PER_HALF_TURN;

/**
 * Narrow, CLOSED token aliases accepted by `normalizeAngleUnit`.
 *
 * These are TOKENS, not prose: the same nature as the direction tokens in
 * directions.ts, which Spec 001's audit recorded as "tokens, not prose". No
 * sentence is parsed here, and the canonical schema stays closed to the two
 * normalized values, so only `rad`/`deg` ever cross a boundary.
 */
export const ANGLE_UNIT_ALIASES: Readonly<Record<string, AngleUnit>> = {
  rad: "rad",
  radian: "rad",
  radians: "rad",
  deg: "deg",
  degree: "deg",
  degrees: "deg",
  "°": "deg",
};

/**
 * Failure messages are fixed strings (no value interpolation) so they are
 * byte-identical across languages and safe to assert on in parity tests.
 */
export const ANGLE_MESSAGES = {
  NON_FINITE_VALUE: "angle value must be a finite number",
  UNSUPPORTED_UNIT: `unsupported angle unit; expected one of: ${ANGLE_UNITS.join(", ")}`,
  NOT_A_MEASUREMENT: "angle must be an object with value and unit",
} as const;

export type AngleResult =
  | { ok: true; radians: number }
  | { ok: false; error: ChatError };

function invalidUnits(message: string): AngleResult {
  return { ok: false, error: { code: "INVALID_UNITS", message } };
}

/** True when `value` is a real, finite number. */
export function isFiniteAngle(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * Normalize negative zero to positive zero.
 *
 * -0 and 0 are numerically equal but serialize differently across languages
 * (JS `JSON.stringify(-0)` is "0"; Python `repr(-0.0)` is "-0.0"). Collapsing the
 * sign keeps output identical in both representations, and matches the Task 1
 * digest rule.
 */
function normalizeZero(value: number): number {
  return value === 0 ? 0 : value;
}

/**
 * Normalize a unit TOKEN to its canonical wire value, or `undefined`.
 *
 * Case-insensitive and whitespace-trimmed. Accepts only the closed alias set in
 * ANGLE_UNIT_ALIASES; anything else is unrecognised rather than guessed.
 */
export function normalizeAngleUnit(unit: unknown): AngleUnit | undefined {
  if (typeof unit !== "string") return undefined;
  return ANGLE_UNIT_ALIASES[unit.trim().toLowerCase()];
}

/** Convert a finite degree value to canonical radians. */
export function degreesToRadians(value: unknown): AngleResult {
  if (!isFiniteAngle(value)) {
    return invalidUnits(ANGLE_MESSAGES.NON_FINITE_VALUE);
  }
  return { ok: true, radians: normalizeZero(value * RADIANS_PER_DEGREE) };
}

/**
 * Pass a radian value through unchanged (identity), after validating it.
 *
 * Kept explicit rather than special-cased at the call site so every accepted unit
 * goes through the same validate-then-convert path, exactly as `metersToMeters`
 * does for lengths.
 */
export function radiansToRadians(value: unknown): AngleResult {
  if (!isFiniteAngle(value)) {
    return invalidUnits(ANGLE_MESSAGES.NON_FINITE_VALUE);
  }
  return { ok: true, radians: normalizeZero(value) };
}

/**
 * Convert a (value, unit) pair to canonical radians. `unit` may be any accepted
 * token; it is normalized first.
 */
export function convertToRadians(value: unknown, unit: unknown): AngleResult {
  const normalized = normalizeAngleUnit(unit);
  if (normalized === "deg") return degreesToRadians(value);
  if (normalized === "rad") return radiansToRadians(value);
  // Unit is not in the canonical vocabulary. Value validity is irrelevant here:
  // an unknown unit makes the angle uninterpretable.
  return invalidUnits(ANGLE_MESSAGES.UNSUPPORTED_UNIT);
}

/** Convert an AngleMeasurement to canonical radians, validating shape first. */
export function toRadians(measurement: unknown): AngleResult {
  if (
    measurement === null ||
    typeof measurement !== "object" ||
    Array.isArray(measurement)
  ) {
    return invalidUnits(ANGLE_MESSAGES.NOT_A_MEASUREMENT);
  }
  const m = measurement as { value?: unknown; unit?: unknown };
  return convertToRadians(m.value, m.unit);
}

/**
 * Convert canonical radians back to degrees, or `undefined` if not finite.
 *
 * For DIAGNOSTICS and user-facing display only — never for anything crossing a
 * boundary, where radians are canonical. Deliberately returns a bare number rather
 * than an AngleResult: that result type's field is named `radians`, and reusing it
 * to carry degrees would be exactly the kind of unit-mislabelling this module
 * exists to prevent.
 */
export function radiansToDegrees(value: unknown): number | undefined {
  if (!isFiniteAngle(value)) return undefined;
  return normalizeZero(value / RADIANS_PER_DEGREE);
}
