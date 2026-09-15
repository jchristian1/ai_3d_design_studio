/**
 * Angle conversion tests — TypeScript (Spec 002, Task 2).
 *
 * Mirrors test_angles.py. Radians are the one canonical internal angular unit; the
 * five required spec vectors are asserted by EXACT equality against Math.PI
 * fractions, which is legitimate because deg -> rad is a single multiplication by
 * one constant. Conversion is NOT normalization: full turns keep their excess.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ANGLE_MESSAGES,
  ANGLE_UNIT_ALIASES,
  DEGREES_PER_HALF_TURN,
  RADIANS_PER_DEGREE,
  convertToRadians,
  degreesToRadians,
  isFiniteAngle,
  normalizeAngleUnit,
  radiansToDegrees,
  radiansToRadians,
  toRadians,
} from "./index.ts";
import { decodeValue, loadSpatialCases } from "./test-support.ts";
import { ANGLE_UNITS } from "@studio/types";

const CASES = loadSpatialCases();

// ---------------------------------------------------------------------------
// The required spec vectors, exactly
// ---------------------------------------------------------------------------

for (const [degrees, expected] of [
  [0, 0],
  [45, Math.PI / 4],
  [90, Math.PI / 2],
  [180, Math.PI],
  [-45, -Math.PI / 4],
] as [number, number][]) {
  test(`spec: ${degrees} deg is exactly ${expected}`, () => {
    const result = degreesToRadians(degrees);
    assert.ok(result.ok);
    assert.equal(result.radians, expected);
  });
}

test("spec: 1 rad passes through", () => {
  const result = radiansToRadians(1);
  assert.ok(result.ok);
  assert.equal(result.radians, 1);
});

test("the conversion constant is a single precomputed value", () => {
  assert.equal(RADIANS_PER_DEGREE, Math.PI / DEGREES_PER_HALF_TURN);
  assert.equal(DEGREES_PER_HALF_TURN, 180);
});

// ---------------------------------------------------------------------------
// Conversion is not normalization
// ---------------------------------------------------------------------------

for (const degrees of [360, 720, 405, -360, 1080]) {
  test(`${degrees} deg is never collapsed modulo a full turn`, () => {
    const result = degreesToRadians(degrees);
    assert.ok(result.ok);
    assert.equal(result.radians, degrees * RADIANS_PER_DEGREE);
    if (Math.abs(degrees) >= 360) {
      assert.ok(Math.abs(result.radians) >= 2 * Math.PI - 1e-12);
    }
  });
}

test("a full turn is distinguishable from zero", () => {
  const full = degreesToRadians(360);
  const none = degreesToRadians(0);
  assert.ok(full.ok && none.ok);
  assert.notEqual(full.radians, none.radians);
});

// ---------------------------------------------------------------------------
// Negative zero
// ---------------------------------------------------------------------------

for (const unit of ["deg", "rad"]) {
  test(`negative zero ${unit} normalizes`, () => {
    const result = convertToRadians(-0, unit);
    assert.ok(result.ok);
    assert.equal(result.radians, 0);
    assert.ok(!Object.is(result.radians, -0));
  });
}

// ---------------------------------------------------------------------------
// Unit tokens
// ---------------------------------------------------------------------------

for (const [token, expected] of [
  ["deg", "deg"],
  ["DEG", "deg"],
  ["  deg ", "deg"],
  ["degree", "deg"],
  ["degrees", "deg"],
  ["Degrees", "deg"],
  ["°", "deg"],
  ["rad", "rad"],
  ["radian", "rad"],
  ["radians", "rad"],
  ["RADIANS", "rad"],
] as [string, string][]) {
  test(`unit token '${token}' normalizes to ${expected}`, () => {
    assert.equal(normalizeAngleUnit(token), expected);
  });
}

for (const token of [
  "grad",
  "gradian",
  "turn",
  "arcmin",
  "cm",
  "m",
  "",
  "  ",
  null,
  180,
  "d",
]) {
  test(`unit token ${JSON.stringify(token)} is refused`, () => {
    assert.equal(normalizeAngleUnit(token), undefined);
    const result = convertToRadians(45, token);
    assert.ok(!result.ok);
    assert.equal(result.error.code, "INVALID_UNITS");
    assert.equal(result.error.message, ANGLE_MESSAGES.UNSUPPORTED_UNIT);
  });
}

test("the alias table only maps to canonical values", () => {
  assert.deepEqual(
    [...new Set(Object.values(ANGLE_UNIT_ALIASES))].sort(),
    [...ANGLE_UNITS].sort(),
  );
});

test("an unknown unit beats an invalid value", () => {
  const result = convertToRadians(NaN, "grad");
  assert.ok(!result.ok);
  assert.equal(result.error.message, ANGLE_MESSAGES.UNSUPPORTED_UNIT);
});

// ---------------------------------------------------------------------------
// Value validation
// ---------------------------------------------------------------------------

for (const value of [NaN, Infinity, -Infinity, "45", null, true, [45], {}]) {
  test(`value ${JSON.stringify(value)} is refused`, () => {
    assert.equal(isFiniteAngle(value), false);
    for (const unit of ["deg", "rad"]) {
      const result = convertToRadians(value, unit);
      assert.ok(!result.ok);
      assert.equal(result.error.code, "INVALID_UNITS");
      assert.equal(result.error.message, ANGLE_MESSAGES.NON_FINITE_VALUE);
    }
  });
}

for (const measurement of [null, 45, "45 deg", [45, "deg"]]) {
  test(`malformed measurement ${JSON.stringify(measurement)} is refused`, () => {
    const result = toRadians(measurement);
    assert.ok(!result.ok);
    assert.equal(result.error.message, ANGLE_MESSAGES.NOT_A_MEASUREMENT);
  });
}

test("toRadians accepts an object with value and unit", () => {
  const result = toRadians({ value: 90, unit: "deg" });
  assert.ok(result.ok);
  assert.equal(result.radians, Math.PI / 2);
});

// ---------------------------------------------------------------------------
// Diagnostics inverse
// ---------------------------------------------------------------------------

for (const degrees of [0, 45, 90, 180, -45, 123.456]) {
  test(`radiansToDegrees round-trips ${degrees}`, () => {
    const forward = degreesToRadians(degrees);
    assert.ok(forward.ok);
    const back = radiansToDegrees(forward.radians);
    assert.ok(back !== undefined);
    assert.ok(Math.abs(back - degrees) < 1e-12);
  });
}

test("radiansToDegrees refuses non-finite input", () => {
  assert.equal(radiansToDegrees(NaN), undefined);
  assert.equal(radiansToDegrees("1"), undefined);
});

test("conversion is deterministic", () => {
  const run = () => {
    const out: number[] = [];
    for (let d = -360; d <= 360; d += 1) {
      const r = degreesToRadians(d);
      assert.ok(r.ok);
      out.push(r.radians);
    }
    return out;
  };
  const first = run();
  for (let i = 0; i < 5; i += 1) assert.deepEqual(run(), first);
});

// ---------------------------------------------------------------------------
// Shared corpus
// ---------------------------------------------------------------------------

for (const c of CASES.angles.valid) {
  test(`[corpus] angle valid: ${c.name}`, () => {
    const result = toRadians({ value: decodeValue(c.value), unit: c.unit });
    assert.ok(result.ok, JSON.stringify(result));
    assert.equal(result.radians, c.radians);
  });
}

for (const c of CASES.angles.invalid) {
  test(`[corpus] angle invalid: ${c.name}`, () => {
    const result = toRadians({ value: decodeValue(c.value), unit: c.unit });
    assert.ok(!result.ok);
    assert.equal(result.error.code, c.error_code);
    assert.equal(
      result.error.message,
      ANGLE_MESSAGES[c.error_key as keyof typeof ANGLE_MESSAGES],
    );
  });
}

for (const c of CASES.angles.malformed) {
  test(`[corpus] angle malformed: ${c.name}`, () => {
    const result = toRadians(decodeValue(c.measurement));
    assert.ok(!result.ok);
    assert.equal(result.error.code, c.error_code);
    assert.equal(
      result.error.message,
      ANGLE_MESSAGES[c.error_key as keyof typeof ANGLE_MESSAGES],
    );
  });
}
