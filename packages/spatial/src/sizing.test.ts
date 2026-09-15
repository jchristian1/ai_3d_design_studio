/**
 * Resize-factor tests — TypeScript (Spec 002, Task 2).
 *
 * Mirrors test_sizing.py, including the four boundary decisions that are the point
 * of the module: 100% smaller is refused, >100% smaller is refused, a negative
 * percent is refused rather than reinterpreted, and growth has no invented ceiling.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MAX_REDUCTION_PERCENT,
  PERCENT_WHOLE,
  SIZE_DIRECTIONS,
  SIZING_MESSAGES,
  isSizeDirection,
  isValidSizeFactor,
  resizeFactor,
  validateSizeFactor,
} from "./index.ts";
import { decodeValue, loadSpatialCases } from "./test-support.ts";

const CASES = loadSpatialCases();

// ---------------------------------------------------------------------------
// The required spec vectors, exactly
// ---------------------------------------------------------------------------

for (const [percent, direction, expected] of [
  [0, "smaller", 1],
  [0, "larger", 1],
  [20, "smaller", 0.8],
  [25, "larger", 1.25],
  [99, "smaller", 0.01],
] as [number, string, number][]) {
  test(`spec: ${percent}% ${direction} -> ${expected}`, () => {
    const result = resizeFactor(percent, direction);
    assert.ok(result.ok, JSON.stringify(result));
    assert.equal(result.factor, expected);
  });
}

test("the documented worked example: 2 m x 0.8 -> 1.6 m", () => {
  const result = resizeFactor(20, "smaller");
  assert.ok(result.ok);
  assert.equal(result.factor, 0.8);
  assert.equal(2.0 * result.factor, 1.6);
});

// ---------------------------------------------------------------------------
// Boundaries
// ---------------------------------------------------------------------------

for (const percent of [100, 100.1, 120, 1000]) {
  test(`${percent}% smaller is refused as a total reduction`, () => {
    const result = resizeFactor(percent, "smaller");
    assert.ok(!result.ok);
    assert.equal(result.error.code, "VALIDATION_ERROR");
    assert.equal(result.error.message, SIZING_MESSAGES.TOTAL_REDUCTION);
  });
}

test("just under total reduction is allowed and still positive", () => {
  const result = resizeFactor(99.999, "smaller");
  assert.ok(result.ok);
  assert.ok(result.factor > 0);
});

for (const percent of [-0.0001, -1, -20, -100]) {
  for (const direction of ["smaller", "larger"]) {
    test(`${percent}% ${direction} is refused, not reinterpreted`, () => {
      const result = resizeFactor(percent, direction);
      assert.ok(!result.ok);
      assert.equal(result.error.code, "VALIDATION_ERROR");
      assert.equal(result.error.message, SIZING_MESSAGES.NEGATIVE_PERCENT);
    });
  }
}

test("negative zero percent is accepted as zero", () => {
  const result = resizeFactor(-0, "smaller");
  assert.ok(result.ok);
  assert.equal(result.factor, 1);
});

for (const percent of [100, 200, 1000, 10000, 1e6]) {
  test(`${percent}% larger has no invented ceiling`, () => {
    const result = resizeFactor(percent, "larger");
    assert.ok(result.ok);
    assert.ok(result.factor > 1);
  });
}

// ---------------------------------------------------------------------------
// Direction vocabulary
// ---------------------------------------------------------------------------

for (const direction of [
  "bigger",
  "Smaller",
  "SMALLER",
  "left",
  "up",
  "",
  "  ",
  null,
  1,
  true,
  ["smaller"],
]) {
  test(`direction ${JSON.stringify(direction)} is refused`, () => {
    assert.equal(isSizeDirection(direction), false);
    const result = resizeFactor(20, direction);
    assert.ok(!result.ok);
    assert.equal(result.error.message, SIZING_MESSAGES.UNSUPPORTED_DIRECTION);
  });
}

test("direction is checked before the percent", () => {
  const result = resizeFactor(NaN, "bigger");
  assert.ok(!result.ok);
  assert.equal(result.error.message, SIZING_MESSAGES.UNSUPPORTED_DIRECTION);
});

test("the direction set is exactly two tokens", () => {
  assert.deepEqual([...SIZE_DIRECTIONS], ["smaller", "larger"]);
});

// ---------------------------------------------------------------------------
// Percent validation
// ---------------------------------------------------------------------------

for (const percent of [NaN, Infinity, -Infinity, "20", null, true, [20], {}]) {
  test(`percent ${JSON.stringify(percent)} is refused`, () => {
    const result = resizeFactor(percent, "smaller");
    assert.ok(!result.ok);
    assert.equal(result.error.code, "INVALID_UNITS");
    assert.equal(result.error.message, SIZING_MESSAGES.NON_FINITE_PERCENT);
  });
}

// ---------------------------------------------------------------------------
// The semantic size factor is stricter than SceneObject.scale
// ---------------------------------------------------------------------------

for (const value of [1, 0.8, 1e-9, 1000]) {
  test(`${value} is a valid size factor`, () => {
    assert.ok(isValidSizeFactor(value));
    const result = validateSizeFactor(value);
    assert.ok(result.ok);
    assert.equal(result.factor, value);
  });
}

for (const value of [0, -0, -0.8, -1]) {
  test(`${value} is not a valid size factor`, () => {
    assert.equal(isValidSizeFactor(value), false);
    const result = validateSizeFactor(value);
    assert.ok(!result.ok);
    assert.equal(result.error.code, "VALIDATION_ERROR");
    assert.equal(result.error.message, SIZING_MESSAGES.NON_POSITIVE_FACTOR);
  });
}

for (const value of [NaN, Infinity, -Infinity, "0.8", null, true]) {
  test(`factor ${JSON.stringify(value)} is not finite`, () => {
    assert.equal(isValidSizeFactor(value), false);
    const result = validateSizeFactor(value);
    assert.ok(!result.ok);
    assert.equal(result.error.code, "INVALID_UNITS");
    assert.equal(result.error.message, SIZING_MESSAGES.NON_FINITE_FACTOR);
  });
}

test("a negative scene scale is not a valid resize factor", () => {
  // Blender permits a mirrored object, so SceneObject.scale may be negative and the
  // scene contract does not forbid it. A resize FACTOR may not be.
  assert.equal(isValidSizeFactor(-1), false);
});

test("the named constants are used rather than bare numbers", () => {
  assert.equal(PERCENT_WHOLE, 100);
  assert.equal(MAX_REDUCTION_PERCENT, 100);
});

test("the factor is never zero for any accepted reduction", () => {
  for (let i = 0; i < 100000; i += 1) {
    const percent = i / 1000;
    if (percent >= MAX_REDUCTION_PERCENT) continue;
    const result = resizeFactor(percent, "smaller");
    assert.ok(result.ok && result.factor > 0, `${percent}`);
  }
});

// ---------------------------------------------------------------------------
// Shared corpus
// ---------------------------------------------------------------------------

for (const c of CASES.resize.valid) {
  test(`[corpus] resize valid: ${c.name}`, () => {
    const result = resizeFactor(decodeValue(c.percent), c.direction);
    assert.ok(result.ok, JSON.stringify(result));
    assert.equal(result.factor, c.factor);
  });
}

for (const c of CASES.resize.invalid) {
  test(`[corpus] resize invalid: ${c.name}`, () => {
    const result = resizeFactor(decodeValue(c.percent), c.direction);
    assert.ok(!result.ok);
    assert.equal(result.error.code, c.error_code);
    assert.equal(
      result.error.message,
      SIZING_MESSAGES[c.error_key as keyof typeof SIZING_MESSAGES],
    );
  });
}

for (const c of CASES.resize.factors.valid) {
  test(`[corpus] factor valid: ${c.name}`, () => {
    const result = validateSizeFactor(decodeValue(c.value));
    assert.ok(result.ok, JSON.stringify(result));
    assert.equal(result.factor, c.factor);
  });
}

for (const c of CASES.resize.factors.invalid) {
  test(`[corpus] factor invalid: ${c.name}`, () => {
    const result = validateSizeFactor(decodeValue(c.value));
    assert.ok(!result.ok);
    assert.equal(result.error.code, c.error_code);
    assert.equal(
      result.error.message,
      SIZING_MESSAGES[c.error_key as keyof typeof SIZING_MESSAGES],
    );
  });
}
