import { test } from "node:test";
import assert from "node:assert/strict";

import {
  validateChatRequest,
  validateMeterDeltas,
  isFiniteMeters,
  isFiniteVec3,
  isNonEmptyString,
} from "./index.ts";

test("valid ChatRequest passes", () => {
  const r = validateChatRequest({
    request_id: "req_abc123",
    project_id: "proj_1",
    session_id: "sess_1",
    message: "Move Cube 50 cm to the right",
  });
  assert.equal(r.valid, true);
  assert.equal(r.errors.length, 0);
});

test("missing project_id is rejected (Req 2.3)", () => {
  const r = validateChatRequest({
    session_id: "sess_1",
    message: "hi",
  });
  assert.equal(r.valid, false);
  assert.ok(r.errors.some((e) => e.field === "project_id"));
});

test("empty/whitespace project_id is rejected", () => {
  const r = validateChatRequest({
    project_id: "   ",
    session_id: "sess_1",
    message: "hi",
  });
  assert.equal(r.valid, false);
  assert.ok(r.errors.some((e) => e.field === "project_id"));
});

test("non-object body is rejected", () => {
  assert.equal(validateChatRequest(null).valid, false);
  assert.equal(validateChatRequest("nope").valid, false);
});

test("selected_object_id must be non-empty when present", () => {
  const r = validateChatRequest({
    project_id: "p",
    session_id: "s",
    message: "m",
    selected_object_id: "",
  });
  assert.equal(r.valid, false);
  assert.ok(r.errors.some((e) => e.field === "selected_object_id"));
});

test("isFiniteMeters rejects NaN/Infinity/non-numbers", () => {
  assert.equal(isFiniteMeters(0.5), true);
  assert.equal(isFiniteMeters(0), true);
  assert.equal(isFiniteMeters(-1.25), true);
  assert.equal(isFiniteMeters(NaN), false);
  assert.equal(isFiniteMeters(Infinity), false);
  assert.equal(isFiniteMeters(-Infinity), false);
  assert.equal(isFiniteMeters("0.5"), false);
  assert.equal(isFiniteMeters(undefined), false);
});

test("isFiniteVec3 requires all finite components", () => {
  assert.equal(isFiniteVec3({ x: 0, y: 0, z: 0 }), true);
  assert.equal(isFiniteVec3({ x: 0.5, y: 1, z: -2 }), true);
  assert.equal(isFiniteVec3({ x: 0, y: NaN, z: 0 }), false);
  assert.equal(isFiniteVec3({ x: 0, y: 0 }), false);
  assert.equal(isFiniteVec3(null), false);
});

test("validateMeterDeltas accepts finite, rejects non-finite", () => {
  assert.equal(validateMeterDeltas({ delta_x_m: 0.5 }).valid, true);
  assert.equal(validateMeterDeltas({}).valid, true);
  const bad = validateMeterDeltas({ delta_x_m: Infinity, delta_z_m: NaN });
  assert.equal(bad.valid, false);
  assert.equal(bad.errors.length, 2);
});

test("isNonEmptyString basics", () => {
  assert.equal(isNonEmptyString("x"), true);
  assert.equal(isNonEmptyString("  "), false);
  assert.equal(isNonEmptyString(5), false);
});
