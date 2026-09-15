/**
 * Unit conversion tests — TypeScript (Spec 001, Task 2).
 *
 * Covers the required examples explicitly, then replays the shared
 * language-neutral corpus so TS and Python are held to identical behaviour.
 * _Requirements: 3.2_
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CM_PER_METER,
  CONVERSION_MESSAGES,
  cmToMeters,
  convertToMeters,
  metersToMeters,
  toMeters,
} from "./index.ts";
import { decodeValue, loadSpatialCases } from "./test-support.ts";

const cases = loadSpatialCases();

// ---------------------------------------------------------------------------
// Required examples from Spec 001
// ---------------------------------------------------------------------------

test("required: 50 cm -> 0.50 m", () => {
  const r = cmToMeters(50);
  assert.equal(r.ok, true);
  assert.equal(r.ok && r.meters, 0.5);
});

test("required: 100 cm -> 1.00 m", () => {
  const r = cmToMeters(100);
  assert.equal(r.ok, true);
  assert.equal(r.ok && r.meters, 1.0);
});

test("required: -25 cm -> -0.25 m", () => {
  const r = cmToMeters(-25);
  assert.equal(r.ok, true);
  assert.equal(r.ok && r.meters, -0.25);
});

test("meters pass through unchanged (m -> m identity)", () => {
  for (const v of [0, 0.5, 1, 2.7, -0.25]) {
    const r = metersToMeters(v);
    assert.equal(r.ok, true);
    assert.equal(r.ok && r.meters, v);
  }
});

test("CM_PER_METER is 100", () => {
  assert.equal(CM_PER_METER, 100);
});

// ---------------------------------------------------------------------------
// Determinism
// ---------------------------------------------------------------------------

test("conversion is deterministic across repeated calls", () => {
  const first = cmToMeters(1.1);
  for (let i = 0; i < 100; i += 1) {
    const again = cmToMeters(1.1);
    assert.deepEqual(again, first);
  }
});

test("division by 100 is correctly rounded, unlike multiplying by 0.01", () => {
  // Verified counterexample: the two operations land on different doubles.
  const v = 6537.04249344076;
  const r = cmToMeters(v);
  assert.equal(r.ok && r.meters, v / 100);
  assert.notEqual(v / 100, v * 0.01, "counterexample must actually diverge");
  assert.notEqual(r.ok && r.meters, v * 0.01);
});

test("1.1 cm converts to the double nearest 1.1/100", () => {
  // Binary floating point: this is 0.011000000000000001, not 0.011. Identical
  // in TypeScript and Python.
  const r = cmToMeters(1.1);
  assert.equal(r.ok && r.meters, 0.011000000000000001);
});

test("negative zero is normalized to positive zero", () => {
  const r = cmToMeters(-0);
  assert.equal(r.ok, true);
  assert.equal(Object.is(r.ok && r.meters, 0), true, "expected +0, not -0");
});

// ---------------------------------------------------------------------------
// Rejection of invalid / non-finite measurements
// ---------------------------------------------------------------------------

test("non-finite cm values are rejected with INVALID_UNITS", () => {
  for (const v of [NaN, Infinity, -Infinity]) {
    const r = cmToMeters(v);
    assert.equal(r.ok, false);
    assert.equal(!r.ok && r.error.code, "INVALID_UNITS");
    assert.equal(!r.ok && r.error.message, CONVERSION_MESSAGES.NON_FINITE_VALUE);
  }
});

test("non-finite meter values are rejected with INVALID_UNITS", () => {
  for (const v of [NaN, Infinity, -Infinity]) {
    const r = metersToMeters(v);
    assert.equal(r.ok, false);
    assert.equal(!r.ok && r.error.code, "INVALID_UNITS");
  }
});

test("non-numeric values are rejected", () => {
  for (const v of ["50", null, undefined, true, {}, []] as unknown[]) {
    const r = cmToMeters(v as number);
    assert.equal(r.ok, false, `expected rejection for ${JSON.stringify(v)}`);
    assert.equal(!r.ok && r.error.code, "INVALID_UNITS");
  }
});

test("unsupported units are rejected with INVALID_UNITS", () => {
  for (const u of ["mm", "meters", "CM", "", "ft"]) {
    const r = convertToMeters(50, u as "cm");
    assert.equal(r.ok, false, `expected rejection for unit '${u}'`);
    assert.equal(!r.ok && r.error.code, "INVALID_UNITS");
    assert.equal(!r.ok && r.error.message, CONVERSION_MESSAGES.UNSUPPORTED_UNIT);
  }
});

test("malformed measurements are rejected with INVALID_UNITS", () => {
  for (const m of [null, 50, "50 cm", [50, "cm"], undefined] as unknown[]) {
    const r = toMeters(m);
    assert.equal(r.ok, false, `expected rejection for ${JSON.stringify(m)}`);
    assert.equal(!r.ok && r.error.code, "INVALID_UNITS");
  }
});

test("an unknown unit is rejected even when the value is also invalid", () => {
  const r = toMeters({ value: NaN, unit: "mm" });
  assert.equal(r.ok, false);
  assert.equal(!r.ok && r.error.message, CONVERSION_MESSAGES.UNSUPPORTED_UNIT);
});

// ---------------------------------------------------------------------------
// Shared language-neutral corpus
// ---------------------------------------------------------------------------

for (const c of cases.conversion.valid) {
  test(`[corpus] conversion: ${c.name}`, () => {
    const r = toMeters({ value: decodeValue(c.value), unit: c.unit });
    assert.equal(r.ok, true, `expected success, got ${JSON.stringify(r)}`);
    assert.equal(r.ok && r.meters, c.meters);
    assert.equal(
      Object.is(r.ok && r.meters, -0),
      false,
      "negative zero must be normalized",
    );
  });
}

for (const c of cases.conversion.invalid) {
  test(`[corpus] conversion rejects: ${c.name}`, () => {
    const r = toMeters({ value: decodeValue(c.value), unit: c.unit });
    assert.equal(r.ok, false, "expected rejection");
    assert.equal(!r.ok && r.error.code, c.error_code);
    assert.equal(
      !r.ok && r.error.message,
      CONVERSION_MESSAGES[c.error_key as keyof typeof CONVERSION_MESSAGES],
    );
  });
}

for (const c of cases.conversion.malformed) {
  test(`[corpus] conversion rejects malformed: ${c.name}`, () => {
    const r = toMeters(decodeValue(c.measurement));
    assert.equal(r.ok, false, "expected rejection");
    assert.equal(!r.ok && r.error.code, c.error_code);
    assert.equal(
      !r.ok && r.error.message,
      CONVERSION_MESSAGES[c.error_key as keyof typeof CONVERSION_MESSAGES],
    );
  });
}
