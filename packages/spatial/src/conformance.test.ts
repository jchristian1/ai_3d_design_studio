/**
 * Conformance: spatial utilities vs. CANONICAL schemas — TypeScript.
 *
 * Task 2 introduced new cross-boundary vocabulary (LengthUnit, Measurement,
 * Axis, Direction, AxisDirection). Per the Task 1 architecture, the schemas in
 * packages/contracts/schemas are canonical and this representation must conform.
 * These tests read those schema files at runtime.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  SCHEMA_FILES,
  schemaEnum,
  schemaProperties,
  schemaRequired,
  toWire,
  validateAgainstSchema,
} from "@studio/contracts";
import {
  AXES,
  AXIS_SIGNS,
  DIRECTIONS,
  LENGTH_UNITS,
} from "@studio/types";
import type { AxisDirection, Measurement } from "@studio/types";

import { DIRECTION_TO_AXIS, directionDeltaMeters, resolveDirection } from "./index.ts";

// ---------------------------------------------------------------------------
// Enum parity with canonical schemas
// ---------------------------------------------------------------------------

test("LENGTH_UNITS equals canonical length-unit enum", () => {
  assert.deepEqual([...LENGTH_UNITS], schemaEnum(SCHEMA_FILES.LengthUnit));
});

test("AXES equals canonical axis enum", () => {
  assert.deepEqual([...AXES], schemaEnum(SCHEMA_FILES.Axis));
});

test("DIRECTIONS equals canonical direction enum", () => {
  assert.deepEqual([...DIRECTIONS], schemaEnum(SCHEMA_FILES.Direction));
});

test("AXIS_SIGNS equals canonical axis-direction sign enum", () => {
  assert.deepEqual(
    [...AXIS_SIGNS],
    schemaEnum(SCHEMA_FILES.AxisDirection, ["sign"]) as unknown as number[],
  );
});

// ---------------------------------------------------------------------------
// Field parity
// ---------------------------------------------------------------------------

test("Measurement field set matches canonical schema", () => {
  const maximal: Required<Measurement> = { value: 50, unit: "cm" };
  assert.deepEqual(
    Object.keys(maximal).sort(),
    schemaProperties(SCHEMA_FILES.Measurement),
  );
  assert.deepEqual(["unit", "value"], schemaRequired(SCHEMA_FILES.Measurement));
});

test("AxisDirection field set matches canonical schema", () => {
  const maximal: Required<AxisDirection> = { axis: "x", sign: 1 };
  assert.deepEqual(
    Object.keys(maximal).sort(),
    schemaProperties(SCHEMA_FILES.AxisDirection),
  );
  assert.deepEqual(["axis", "sign"], schemaRequired(SCHEMA_FILES.AxisDirection));
});

// ---------------------------------------------------------------------------
// Outputs are schema-valid
// ---------------------------------------------------------------------------

test("every resolved AxisDirection is schema-valid", () => {
  for (const d of DIRECTIONS) {
    const r = resolveDirection(d);
    assert.equal(r.ok, true);
    const result = validateAgainstSchema(
      SCHEMA_FILES.AxisDirection,
      toWire(r.ok ? r.axisDirection : {}),
    );
    assert.equal(result.valid, true, `${d}: ${JSON.stringify(result.violations)}`);
  }
});

test("the whole direction table is schema-valid", () => {
  for (const [direction, axisDirection] of Object.entries(DIRECTION_TO_AXIS)) {
    const dirResult = validateAgainstSchema(SCHEMA_FILES.Direction, direction);
    assert.equal(dirResult.valid, true, `direction token '${direction}' not canonical`);
    const adResult = validateAgainstSchema(
      SCHEMA_FILES.AxisDirection,
      toWire(axisDirection),
    );
    assert.equal(adResult.valid, true, JSON.stringify(adResult.violations));
  }
});

test("composed deltas are schema-valid Vec3 documents in meters", () => {
  const r = directionDeltaMeters("right", { value: 50, unit: "cm" });
  assert.equal(r.ok, true);
  const result = validateAgainstSchema(
    SCHEMA_FILES.Vec3,
    toWire(r.ok ? r.delta : {}),
  );
  assert.equal(result.valid, true, JSON.stringify(result.violations));
});

test("conversion errors are schema-valid structured errors", () => {
  const r = directionDeltaMeters("right", { value: 50, unit: "mm" });
  assert.equal(r.ok, false);
  const result = validateAgainstSchema(
    SCHEMA_FILES.ErrorResponse,
    toWire(r.ok ? {} : r.error),
  );
  assert.equal(result.valid, true, JSON.stringify(result.violations));
});

test("direction errors are schema-valid structured errors", () => {
  const r = resolveDirection("sideways");
  assert.equal(r.ok, false);
  const result = validateAgainstSchema(
    SCHEMA_FILES.ErrorResponse,
    toWire(r.ok ? {} : r.error),
  );
  assert.equal(result.valid, true, JSON.stringify(result.violations));
});
