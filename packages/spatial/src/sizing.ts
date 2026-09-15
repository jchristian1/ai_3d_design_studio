/**
 * Deterministic resize interpretation — TypeScript representation.
 *
 * Spec 002, Task 2.
 *
 * Turns already-normalized semantic size intent into an absolute multiplicative
 * FACTOR:
 *
 *     percent=20, direction="smaller"  ->  0.8
 *     percent=25, direction="larger"   ->  1.25
 *
 * Scope and deliberate non-goals:
 *   - Pure arithmetic only. No Blender, no bpy, no I/O, no environment.
 *   - NO SCENE STATE. This module never sees a SceneSnapshot and cannot: it
 *     produces the mathematical factor and nothing else. Task 10 multiplies that
 *     factor by the AUTHORITATIVE snapshot dimensions to obtain absolute desired
 *     dimensions in metres, and the worker only ever receives those absolute
 *     metres — never a percentage.
 *   - No natural-language parsing. Callers supply `percent` and `direction`
 *     already extracted; "make it a fifth smaller" is the agent's problem.
 *
 * Two different quantities, deliberately not conflated:
 *
 * `SceneObject.scale` is OBSERVED Blender transform state. Blender permits negative
 * scale (a mirrored object) and even zero, so the scene contract does not constrain
 * it beyond finiteness — describing reality is not the same as endorsing it.
 *
 * A RESIZE FACTOR is a SEMANTIC size multiplier the platform is about to act on. It
 * must be finite and strictly greater than zero, which is why it is validated here
 * rather than being treated as just another scale number. The consequence that
 * matters: "100% smaller" does not quietly become a valid zero-size design
 * operation, and "120% smaller" is refused instead of silently inverting the object.
 */

import type { ChatError } from "@studio/contracts";

/**
 * Which way a proportional resize goes. A closed token set: the caller has already
 * decided, and an unrecognised direction is refused rather than guessed.
 */
export type SizeDirection = "smaller" | "larger";

export const SIZE_DIRECTIONS: readonly SizeDirection[] = [
  "smaller",
  "larger",
] as const;

/**
 * Percent points in a whole. Named so the arithmetic reads as proportion rather
 * than as a magic 100 — and so this module is the one place it appears.
 */
export const PERCENT_WHOLE = 100;

/**
 * The largest reduction that still leaves an object with size. Exclusive: 100%
 * smaller is nothing left, which is a deletion, not a resize.
 */
export const MAX_REDUCTION_PERCENT = 100;

/**
 * Failure messages are fixed strings (no value interpolation) so they are
 * byte-identical across languages and safe to assert on in parity tests.
 */
export const SIZING_MESSAGES = {
  NON_FINITE_PERCENT: "resize percent must be a finite number",
  NEGATIVE_PERCENT:
    "resize percent must not be negative; direction is carried by the " +
    "direction field, so a negative percent would encode it twice",
  TOTAL_REDUCTION:
    "a reduction of 100 percent or more would leave no object; resizing to " +
    "nothing is a deletion, not a resize",
  UNSUPPORTED_DIRECTION: `unsupported resize direction; expected one of: ${SIZE_DIRECTIONS.join(", ")}`,
  NON_FINITE_FACTOR: "size factor must be a finite number",
  NON_POSITIVE_FACTOR:
    "size factor must be greater than zero; a zero or negative factor " +
    "collapses or mirrors the object rather than resizing it",
} as const;

export type SizeFactorResult =
  | { ok: true; factor: number }
  | { ok: false; error: ChatError };

function invalid(
  message: string,
  code: ChatError["code"] = "VALIDATION_ERROR",
): SizeFactorResult {
  return { ok: false, error: { code, message } };
}

/**
 * True when `value` is a usable SEMANTIC size multiplier: finite and strictly
 * positive. Deliberately stricter than the constraint on `SceneObject.scale`: see
 * the module docstring.
 */
export function isValidSizeFactor(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

/**
 * Validate an explicit size factor supplied from elsewhere.
 *
 * Exists so a factor that did not come from `resizeFactor` — a value a model
 * proposed directly, for instance — passes through exactly the same rules rather
 * than being trusted because it is already a number.
 */
export function validateSizeFactor(value: unknown): SizeFactorResult {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return invalid(SIZING_MESSAGES.NON_FINITE_FACTOR, "INVALID_UNITS");
  }
  if (value <= 0) {
    return invalid(SIZING_MESSAGES.NON_POSITIVE_FACTOR);
  }
  return { ok: true, factor: value };
}

/** True when `direction` is one of the canonical resize directions. */
export function isSizeDirection(direction: unknown): direction is SizeDirection {
  return (
    typeof direction === "string" &&
    (SIZE_DIRECTIONS as readonly string[]).includes(direction)
  );
}

/**
 * Convert normalized (percent, direction) intent into an absolute factor.
 *
 *   smaller -> (100 - percent) / 100      larger -> (100 + percent) / 100
 *
 * Determinism: a single correctly-rounded division by PERCENT_WHOLE, never a
 * multiplication by an inexact reciprocal — the same reasoning that makes
 * `cmToMeters` a division. The exact vectors the corpus pins:
 * 20 smaller -> 0.8, 25 larger -> 1.25, 99 smaller -> 0.01, 0 -> 1.0 either way.
 *
 * Refusals, each deliberate:
 *   - non-finite percent -> INVALID_UNITS;
 *   - NEGATIVE percent -> VALIDATION_ERROR. "-20% smaller" is not read as
 *     "20% larger": the direction field already carries the sign, so a negative
 *     percent would encode direction twice — the identical rule Spec 001 applies to
 *     `left` plus `-25 cm`;
 *   - percent >= 100 with `smaller` -> VALIDATION_ERROR. 100% smaller leaves
 *     nothing, and more than 100% would invert the object;
 *   - unknown direction -> VALIDATION_ERROR.
 *
 * No upper bound is imposed on `larger`: no product requirement caps growth, and an
 * invented ceiling would refuse legitimate design intent. The factor is still
 * validated as finite, so an absurd request fails on its arithmetic rather than on a
 * guess.
 */
export function resizeFactor(
  percent: unknown,
  direction: unknown,
): SizeFactorResult {
  if (!isSizeDirection(direction)) {
    return invalid(SIZING_MESSAGES.UNSUPPORTED_DIRECTION);
  }
  if (typeof percent !== "number" || !Number.isFinite(percent)) {
    return invalid(SIZING_MESSAGES.NON_FINITE_PERCENT, "INVALID_UNITS");
  }
  if (percent < 0) {
    return invalid(SIZING_MESSAGES.NEGATIVE_PERCENT);
  }

  let factor: number;
  if (direction === "smaller") {
    if (percent >= MAX_REDUCTION_PERCENT) {
      return invalid(SIZING_MESSAGES.TOTAL_REDUCTION);
    }
    factor = (PERCENT_WHOLE - percent) / PERCENT_WHOLE;
  } else {
    factor = (PERCENT_WHOLE + percent) / PERCENT_WHOLE;
  }

  // A percent just under the limit can still round to zero for absurd inputs; the
  // semantic guarantee is enforced, not assumed.
  return validateSizeFactor(factor);
}
