/**
 * Direction mapping tests — TypeScript (Spec 001, Task 2).
 *
 * Covers the required mapping (right -> axis X, positive), totality over the
 * canonical Direction enum, independence from Blender, and the shared corpus.
 * _Requirements: 3.3_
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DIRECTION_MESSAGES,
  DIRECTION_TO_AXIS,
  directionDeltaMeters,
  isDirection,
  resolveDirection,
  toMeters,
  zeroDelta,
} from "./index.ts";
import { decodeValue, loadSpatialCases } from "./test-support.ts";
import { DIRECTIONS, AXES } from "@studio/types";

const cases = loadSpatialCases();

// ---------------------------------------------------------------------------
// Required mapping from Spec 001
// ---------------------------------------------------------------------------

test("required: right -> axis X, positive direction", () => {
  const r = resolveDirection("right");
  assert.equal(r.ok, true);
  assert.equal(r.ok && r.axisDirection.axis, "x");
  assert.equal(r.ok && r.axisDirection.sign, 1);
});

test("required: right produces a +X delta of the given distance", () => {
  const r = directionDeltaMeters("right", { value: 50, unit: "cm" });
  assert.equal(r.ok, true);
  assert.deepEqual(r.ok && r.delta, { x: 0.5, y: 0, z: 0 });
  assert.equal(r.ok && r.axis, "x");
  assert.equal(r.ok && r.meters, 0.5);
});

test("world-space table is exactly as specified", () => {
  assert.deepEqual(DIRECTION_TO_AXIS, {
    right: { axis: "x", sign: 1 },
    left: { axis: "x", sign: -1 },
    forward: { axis: "y", sign: 1 },
    back: { axis: "y", sign: -1 },
    up: { axis: "z", sign: 1 },
    down: { axis: "z", sign: -1 },
  });
});

// ---------------------------------------------------------------------------
// Totality and structure
// ---------------------------------------------------------------------------

test("every canonical direction resolves", () => {
  for (const d of DIRECTIONS) {
    const r = resolveDirection(d);
    assert.equal(r.ok, true, `direction '${d}' must resolve`);
    assert.ok(AXES.includes(r.ok ? r.axisDirection.axis : "x"));
    assert.ok([1, -1].includes(r.ok ? r.axisDirection.sign : 0));
  }
});

test("directions form opposing pairs on the same axis", () => {
  const pairs: [string, string][] = [
    ["right", "left"],
    ["forward", "back"],
    ["up", "down"],
  ];
  for (const [a, b] of pairs) {
    const ra = DIRECTION_TO_AXIS[a as "right"];
    const rb = DIRECTION_TO_AXIS[b as "left"];
    assert.equal(ra.axis, rb.axis, `${a}/${b} must share an axis`);
    assert.equal(ra.sign, -rb.sign, `${a}/${b} must have opposing signs`);
  }
});

test("each axis is used by exactly two directions", () => {
  const counts = new Map<string, number>();
  for (const d of DIRECTIONS) {
    const axis = DIRECTION_TO_AXIS[d].axis;
    counts.set(axis, (counts.get(axis) ?? 0) + 1);
  }
  assert.deepEqual([...counts.entries()].sort(), [
    ["x", 2],
    ["y", 2],
    ["z", 2],
  ]);
});

test("the direction table is frozen (mapping cannot be mutated)", () => {
  assert.equal(Object.isFrozen(DIRECTION_TO_AXIS), true);
  assert.throws(() => {
    // @ts-expect-error deliberately attempting an illegal mutation
    DIRECTION_TO_AXIS.right = { axis: "y", sign: -1 };
  });
  assert.deepEqual(DIRECTION_TO_AXIS.right, { axis: "x", sign: 1 });
});

test("resolution is deterministic across repeated calls", () => {
  const first = resolveDirection("right");
  for (let i = 0; i < 100; i += 1) {
    assert.deepEqual(resolveDirection("right"), first);
  }
});

test("isDirection recognizes only canonical tokens", () => {
  for (const d of DIRECTIONS) assert.equal(isDirection(d), true);
  for (const d of ["Right", "to the right", "", null, 1, undefined]) {
    assert.equal(isDirection(d), false);
  }
});

test("zeroDelta is a fresh zero vector each call", () => {
  const a = zeroDelta();
  const b = zeroDelta();
  assert.deepEqual(a, { x: 0, y: 0, z: 0 });
  a.x = 1;
  assert.equal(b.x, 0, "zeroDelta must not return a shared object");
});

// ---------------------------------------------------------------------------
// Rejection
// ---------------------------------------------------------------------------

test("unknown directions are rejected with VALIDATION_ERROR", () => {
  for (const d of ["Right", "sideways", "toward camera", "", null, 1]) {
    const r = resolveDirection(d);
    assert.equal(r.ok, false, `expected rejection for ${JSON.stringify(d)}`);
    assert.equal(!r.ok && r.error.code, "VALIDATION_ERROR");
    assert.equal(
      !r.ok && r.error.message,
      DIRECTION_MESSAGES.UNSUPPORTED_DIRECTION,
    );
  }
});

test("camera-relative wording is not accepted (deferred by design)", () => {
  for (const d of ["toward camera", "away from camera", "screen left"]) {
    assert.equal(resolveDirection(d).ok, false);
  }
});

test("delta composition rejects a bad direction before converting units", () => {
  const r = directionDeltaMeters("sideways", { value: NaN, unit: "mm" });
  assert.equal(r.ok, false);
  assert.equal(!r.ok && r.error.code, "VALIDATION_ERROR");
});

test("delta composition rejects a bad measurement", () => {
  const r = directionDeltaMeters("right", { value: 50, unit: "mm" });
  assert.equal(r.ok, false);
  assert.equal(!r.ok && r.error.code, "INVALID_UNITS");
});

// ---------------------------------------------------------------------------
// Signed measurement semantics at the composition boundary
// ---------------------------------------------------------------------------

test("positive magnitude is accepted and direction comes from the token", () => {
  const r = directionDeltaMeters("left", { value: 25, unit: "cm" });
  assert.equal(r.ok, true);
  assert.deepEqual(r.ok && r.delta, { x: -0.25, y: 0, z: 0 });
  assert.equal(r.ok && r.meters, -0.25);
});

test("zero magnitude is accepted and yields a zero delta", () => {
  for (const unit of ["cm", "m"] as const) {
    const r = directionDeltaMeters("right", { value: 0, unit });
    assert.equal(r.ok, true, `zero must be valid for ${unit}`);
    assert.deepEqual(r.ok && r.delta, { x: 0, y: 0, z: 0 });
    assert.equal(r.ok && r.meters, 0);
  }
});

test("negative magnitude is rejected with VALIDATION_ERROR", () => {
  const r = directionDeltaMeters("left", { value: -25, unit: "cm" });
  assert.equal(r.ok, false);
  assert.equal(!r.ok && r.error.code, "VALIDATION_ERROR");
  assert.equal(!r.ok && r.error.message, DIRECTION_MESSAGES.NEGATIVE_MAGNITUDE);
});

test("negative magnitude is rejected for every direction and unit", () => {
  for (const d of DIRECTIONS) {
    for (const unit of ["cm", "m"] as const) {
      const r = directionDeltaMeters(d, { value: -1, unit });
      assert.equal(r.ok, false, `${d} with -1 ${unit} must be rejected`);
      assert.equal(!r.ok && r.error.code, "VALIDATION_ERROR");
    }
  }
});

test("direction is never encoded twice: no negative distance reversal", () => {
  // Previously -25 cm silently reversed the direction. That is now an error.
  const reversed = directionDeltaMeters("right", { value: -25, unit: "cm" });
  assert.equal(reversed.ok, false, "negative distance must not reverse direction");
});

test("negative zero is not treated as a negative magnitude", () => {
  const r = directionDeltaMeters("left", { value: -0, unit: "cm" });
  assert.equal(r.ok, true);
  assert.equal(r.ok && r.meters, 0);
});

test("ordinary signed conversion is unaffected by the magnitude rule", () => {
  // The generic converter must remain signed.
  const r = toMeters({ value: -25, unit: "cm" });
  assert.equal(r.ok, true);
  assert.equal(r.ok && r.meters, -0.25);
});

test("unit errors take precedence over the magnitude rule", () => {
  const r = directionDeltaMeters("right", { value: -50, unit: "mm" });
  assert.equal(r.ok, false);
  assert.equal(!r.ok && r.error.code, "INVALID_UNITS");
});

test("negative infinity is rejected as non-finite, not as a magnitude", () => {
  const r = directionDeltaMeters("right", { value: -Infinity, unit: "m" });
  assert.equal(r.ok, false);
  assert.equal(!r.ok && r.error.code, "INVALID_UNITS");
});

// ---------------------------------------------------------------------------
// Independence from Blender
// ---------------------------------------------------------------------------

test("module graph contains no Blender/bpy dependency", async () => {
  // Importing the package must not require bpy or any Blender runtime; if it
  // did, this import would throw outside Blender.
  const mod = await import("./index.ts");
  assert.equal(typeof mod.resolveDirection, "function");
  assert.equal(typeof mod.toMeters, "function");
});

// ---------------------------------------------------------------------------
// Shared language-neutral corpus
// ---------------------------------------------------------------------------

for (const c of cases.directions.valid) {
  test(`[corpus] direction: ${c.name}`, () => {
    const r = resolveDirection(c.direction);
    assert.equal(r.ok, true);
    assert.equal(r.ok && r.axisDirection.axis, c.axis);
    assert.equal(r.ok && r.axisDirection.sign, c.sign);
  });
}

for (const c of cases.directions.invalid) {
  test(`[corpus] direction rejects: ${c.name}`, () => {
    const r = resolveDirection(decodeValue(c.direction));
    assert.equal(r.ok, false);
    assert.equal(!r.ok && r.error.code, c.error_code);
  });
}

for (const c of cases.deltas.valid) {
  test(`[corpus] delta: ${c.name}`, () => {
    const r = directionDeltaMeters(c.direction, {
      value: decodeValue(c.value),
      unit: c.unit,
    });
    assert.equal(r.ok, true, `expected success, got ${JSON.stringify(r)}`);
    assert.deepEqual(r.ok && r.delta, c.delta);
    assert.equal(r.ok && r.axis, c.axis);
    assert.equal(r.ok && r.meters, c.meters);
  });
}

for (const c of cases.deltas.invalid) {
  test(`[corpus] delta rejects: ${c.name}`, () => {
    const r = directionDeltaMeters(c.direction, {
      value: decodeValue(c.value),
      unit: c.unit,
    });
    assert.equal(r.ok, false);
    assert.equal(!r.ok && r.error.code, c.error_code);
    if (c.error_key) {
      assert.equal(
        !r.ok && r.error.message,
        DIRECTION_MESSAGES[c.error_key as keyof typeof DIRECTION_MESSAGES],
      );
    }
  });
}
