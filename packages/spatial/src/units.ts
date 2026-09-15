/**
 * Deterministic length-unit conversion — TypeScript representation.
 *
 * Spec 001, Task 2. Canonical internal unit is METERS.
 *
 * Scope and deliberate non-goals:
 *   - Pure arithmetic only. No Blender, no bpy, no I/O.
 *   - No natural-language parsing and no AI reasoning. Callers supply an
 *     already-structured Measurement; interpreting prose is the agent's job
 *     (Task 6) and is kept out of here on purpose.
 *   - Supported units are exactly the ones Spec 001 needs: cm and m.
 *
 * Determinism: cm -> m is a single IEEE-754 division by 100, never a
 * multiplication by 0.01. Division is correctly rounded; multiplying by the
 * inexact constant 0.01 rounds twice and can land on a different double.
 * Verified counterexample: 6537.04249344076 / 100 === 65.37042493440759 whereas
 * 6537.04249344076 * 0.01 === 65.37042493440761. Both TypeScript and Python use
 * IEEE-754 doubles and correctly-rounded division, so results are bit-identical
 * across the two representations.
 *
 * Note: conversion returns the nearest double to the quotient of the *double*
 * input, which is not always the nearest double to the decimal you typed
 * (1.1 / 100 is 0.011000000000000001, not 0.011). That is inherent to binary
 * floating point, is identical in both languages, and is pinned by the corpus.
 *
 * Canonical schemas: measurement.schema.json, length-unit.schema.json
 */

import type { ChatError } from "@studio/contracts";
import type { LengthUnit, Measurement } from "@studio/types";
import { LENGTH_UNITS } from "@studio/types";
import { isFiniteMeters } from "@studio/validation";

/** Centimeters per meter. The only conversion factor this milestone needs. */
export const CM_PER_METER = 100;

export type ConversionResult =
  | { ok: true; meters: number }
  | { ok: false; error: ChatError };

/**
 * Failure messages are fixed strings (no value interpolation) so they are
 * byte-identical across languages and safe to assert on in parity tests.
 */
export const CONVERSION_MESSAGES = {
  NON_FINITE_VALUE: "measurement value must be a finite number",
  UNSUPPORTED_UNIT: `unsupported length unit; expected one of: ${LENGTH_UNITS.join(", ")}`,
  NOT_A_MEASUREMENT: "measurement must be an object with value and unit",
} as const;

function invalidUnits(message: string): ConversionResult {
  return { ok: false, error: { code: "INVALID_UNITS", message } };
}

/**
 * Normalize negative zero to positive zero.
 *
 * -0 and 0 are numerically equal but serialize differently across languages
 * (JS `JSON.stringify(-0)` is "0"; Python `repr(-0.0)` is "-0.0"). Collapsing
 * the sign keeps conversion output identical in both representations.
 */
function normalizeZero(value: number): number {
  return value === 0 ? 0 : value;
}

/** Convert a finite centimeter value to canonical meters. */
export function cmToMeters(value: number): ConversionResult {
  if (!isFiniteMeters(value)) {
    return invalidUnits(CONVERSION_MESSAGES.NON_FINITE_VALUE);
  }
  return { ok: true, meters: normalizeZero(value / CM_PER_METER) };
}

/**
 * Pass a meter value through unchanged (identity), after validating it.
 *
 * Kept explicit rather than special-cased at the call site so every accepted
 * unit goes through the same validate-then-convert path.
 */
export function metersToMeters(value: number): ConversionResult {
  if (!isFiniteMeters(value)) {
    return invalidUnits(CONVERSION_MESSAGES.NON_FINITE_VALUE);
  }
  return { ok: true, meters: normalizeZero(value) };
}

/** Convert a (value, unit) pair to canonical meters. */
export function convertToMeters(value: number, unit: LengthUnit): ConversionResult {
  switch (unit) {
    case "cm":
      return cmToMeters(value);
    case "m":
      return metersToMeters(value);
    default:
      // Unit is not in the canonical enum. Value validity is irrelevant here:
      // an unknown unit makes the measurement uninterpretable.
      return invalidUnits(CONVERSION_MESSAGES.UNSUPPORTED_UNIT);
  }
}

/** Convert a Measurement to canonical meters, validating shape and value. */
export function toMeters(measurement: unknown): ConversionResult {
  if (
    measurement === null ||
    typeof measurement !== "object" ||
    Array.isArray(measurement)
  ) {
    return invalidUnits(CONVERSION_MESSAGES.NOT_A_MEASUREMENT);
  }
  const m = measurement as Partial<Measurement>;
  if (typeof m.unit !== "string" || !LENGTH_UNITS.includes(m.unit as LengthUnit)) {
    return invalidUnits(CONVERSION_MESSAGES.UNSUPPORTED_UNIT);
  }
  return convertToMeters(m.value as number, m.unit as LengthUnit);
}
