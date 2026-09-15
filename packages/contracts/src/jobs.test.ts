/**
 * Job layer tests — TypeScript (Spec 001, Task 3).
 *
 * Covers the exact spec example (Cube delta X = +0.50 m), project isolation,
 * payload validation, the unresolved-language guard, lifecycle states,
 * idempotency derivation, and serialization round trips.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  ALLOWED_TRANSITIONS,
  JOB_MESSAGES,
  SCHEMA_FILES,
  UNRESOLVED_PAYLOAD_KEYS,
  canTransition,
  canonicalize,
  classifyDelivery,
  createMoveObjectJob,
  deriveContentFingerprint,
  deriveIdempotencyKey,
  encodeFloat64,
  isDuplicateDelivery,
  isTerminal,
  isValidObjectRef,
  parseJob,
  serializeJob,
  toJobWire,
  validateAgainstSchema,
  validateJob,
} from "./index.ts";
import type { Job, MoveObjectJob } from "@studio/types";
import { JOB_STATUSES } from "@studio/types";
import { directionDeltaMeters } from "@studio/spatial";

const HERE = dirname(fileURLToPath(import.meta.url));
const CASES = JSON.parse(
  readFileSync(join(HERE, "..", "job-cases.json"), "utf8"),
) as any;

function decode(value: unknown): unknown {
  if (value === "__NAN__") return NaN;
  if (value === "__INF__") return Infinity;
  if (value === "__NEG_INF__") return -Infinity;
  return value;
}

const BASE = {
  job_id: "job_1",
  project_id: "proj_1",
  session_id: "sess_1",
  user_id: "user_1",
  request_id: "req_abc123",
  created_at: "2026-09-15T04:00:00Z",
};

function validJob(): MoveObjectJob {
  const r = createMoveObjectJob({
    ...BASE,
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
  });
  assert.equal(r.ok, true, r.ok ? "" : JSON.stringify(r.errors));
  return (r as { ok: true; job: MoveObjectJob }).job;
}

// ---------------------------------------------------------------------------
// The exact Spec 001 example
// ---------------------------------------------------------------------------

test("exact example: Cube delta X = +0.50 m", () => {
  const job = validJob();
  assert.equal(job.job_type, "move_object");
  assert.equal(job.payload.target.name, "Cube");
  assert.equal(job.payload.delta_meters.x, 0.5);
  assert.equal(job.payload.delta_meters.y, 0);
  assert.equal(job.payload.delta_meters.z, 0);
  assert.equal(job.status, "queued");
  assert.equal(job.project_id, "proj_1");
});

test("the resolved spatial delta feeds the job unchanged", () => {
  // packages/spatial resolves "50 cm to the right"; the job stores only meters.
  const resolved = directionDeltaMeters("right", { value: 50, unit: "cm" });
  assert.equal(resolved.ok, true);
  const r = createMoveObjectJob({
    ...BASE,
    target: { name: "Cube" },
    delta_meters: resolved.ok ? resolved.delta : { x: 0, y: 0, z: 0 },
  });
  assert.equal(r.ok, true);
  assert.equal(r.ok && r.job.payload.delta_meters.x, 0.5);
});

test("a valid move job passes schema validation", () => {
  const job = validJob();
  const result = validateAgainstSchema(SCHEMA_FILES.Job, toJobWire(job));
  assert.equal(result.valid, true, JSON.stringify(result.violations));
  assert.equal(validateJob(job).valid, true);
});

// ---------------------------------------------------------------------------
// Project isolation
// ---------------------------------------------------------------------------

test("missing project_id is rejected", () => {
  const job = validJob() as Record<string, unknown>;
  delete job.project_id;
  const r = validateJob(job);
  assert.equal(r.valid, false);
  assert.ok(
    r.errors.some((e) => e.message.includes(JOB_MESSAGES.MISSING_PROJECT_ID)),
    "expected an explicit project_id error",
  );
});

test("blank project_id is rejected", () => {
  const job = { ...validJob(), project_id: "   " };
  assert.equal(validateJob(job).valid, false);
});

test("missing user_id and session_id are rejected", () => {
  for (const field of ["user_id", "session_id"]) {
    const job = validJob() as Record<string, unknown>;
    delete job[field];
    assert.equal(validateJob(job).valid, false, `${field} must be required`);
  }
});

// ---------------------------------------------------------------------------
// Target validation
// ---------------------------------------------------------------------------

test("target must supply a non-blank object_id or name", () => {
  assert.equal(isValidObjectRef({ name: "Cube" }), true);
  assert.equal(isValidObjectRef({ object_id: "obj_8d83f" }), true);
  assert.equal(isValidObjectRef({}), false);
  assert.equal(isValidObjectRef({ name: "  " }), false);
  assert.equal(isValidObjectRef(null), false);
  assert.equal(isValidObjectRef("Cube"), false);
});

test("invalid target is rejected by the job validator", () => {
  for (const target of [{}, { name: "   " }, { object_id: "" }] as unknown[]) {
    const r = createMoveObjectJob({
      ...BASE,
      target: target as { name?: string },
      delta_meters: { x: 0.5, y: 0, z: 0 },
    });
    assert.equal(r.ok, false, `expected rejection for ${JSON.stringify(target)}`);
  }
});

test("missing target is rejected", () => {
  const job = validJob() as any;
  delete job.payload.target;
  assert.equal(validateJob(job).valid, false);
});

// ---------------------------------------------------------------------------
// Delta validation: canonical meters, finite
// ---------------------------------------------------------------------------

test("NaN and Infinity deltas are rejected with INVALID_UNITS", () => {
  for (const bad of [NaN, Infinity, -Infinity]) {
    const r = createMoveObjectJob({
      ...BASE,
      target: { name: "Cube" },
      delta_meters: { x: bad, y: 0, z: 0 },
    });
    assert.equal(r.ok, false, `expected rejection for ${bad}`);
    assert.ok(
      !r.ok && r.errors.some((e) => e.code === "INVALID_UNITS"),
      "expected INVALID_UNITS",
    );
  }
});

test("non-finite delta is rejected on every axis", () => {
  for (const axis of ["x", "y", "z"] as const) {
    const delta = { x: 0, y: 0, z: 0 };
    delta[axis] = NaN;
    const r = createMoveObjectJob({
      ...BASE,
      target: { name: "Cube" },
      delta_meters: delta,
    });
    assert.equal(r.ok, false, `axis ${axis} must be validated`);
  }
});

test("incomplete delta is rejected", () => {
  const job = validJob() as any;
  delete job.payload.delta_meters.z;
  assert.equal(validateJob(job).valid, false);
});

test("a unit string instead of a number is rejected", () => {
  const job = validJob() as any;
  job.payload.delta_meters.x = "50 cm";
  assert.equal(validateJob(job).valid, false);
});

// ---------------------------------------------------------------------------
// Unresolved language must never reach the worker
// ---------------------------------------------------------------------------

test("payload carrying an unresolved unit field is rejected", () => {
  const job = validJob() as any;
  job.payload.unit = "cm";
  const r = validateJob(job);
  assert.equal(r.valid, false);
  assert.ok(
    r.errors.some((e) => e.message.includes("unresolved language")),
    "expected the unresolved-language guard to fire",
  );
});

test("payload carrying an unresolved direction field is rejected", () => {
  const job = validJob() as any;
  job.payload.direction = "right";
  const r = validateJob(job);
  assert.equal(r.valid, false);
  assert.ok(r.errors.some((e) => e.message.includes("unresolved language")));
});

test("every guarded key is rejected in a payload", () => {
  for (const key of UNRESOLVED_PAYLOAD_KEYS) {
    const job = validJob() as any;
    job.payload[key] = "anything";
    assert.equal(validateJob(job).valid, false, `key '${key}' must be rejected`);
  }
});

test("raw user language cannot be attached to a job", () => {
  const job = validJob() as any;
  job.raw_user_message = "Move Cube 50 cm to the right";
  assert.equal(validateJob(job).valid, false);
});

test("there is no code path that accepts a unit or direction", () => {
  // createMoveObjectJob's input type has no unit/direction field; passing them
  // is both a type error and rejected at runtime by additionalProperties.
  const r = createMoveObjectJob({
    ...BASE,
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
    // @ts-expect-error unit is not part of the job input by design
    unit: "cm",
  });
  assert.equal(r.ok, true, "extra input keys are ignored, not stored");
  assert.equal(r.ok && "unit" in r.job.payload, false);
});

// ---------------------------------------------------------------------------
// Job type
// ---------------------------------------------------------------------------

test("unsupported job_type is rejected", () => {
  for (const jobType of ["resize_object", "chat", "render_preview", "nope"]) {
    const job = { ...validJob(), job_type: jobType } as unknown as Job;
    assert.equal(validateJob(job).valid, false, `${jobType} must be rejected`);
  }
});

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

test("invalid status is rejected", () => {
  for (const status of ["pending", "cancelled", "QUEUED", ""]) {
    const job = { ...validJob(), status } as unknown as Job;
    assert.equal(validateJob(job).valid, false, `${status} must be rejected`);
  }
});

test("all canonical statuses are covered by the transition table", () => {
  for (const status of JOB_STATUSES) {
    assert.ok(status in ALLOWED_TRANSITIONS, `${status} missing from table`);
  }
});

test("terminal states allow no transitions", () => {
  assert.equal(isTerminal("succeeded"), true);
  assert.equal(isTerminal("failed"), true);
  assert.equal(isTerminal("queued"), false);
  assert.deepEqual(ALLOWED_TRANSITIONS.succeeded, []);
  assert.deepEqual(ALLOWED_TRANSITIONS.failed, []);
});

for (const t of CASES.transitions.allowed) {
  test(`[corpus] transition allowed: ${t.from} -> ${t.to}`, () => {
    assert.equal(canTransition(t.from, t.to), true);
  });
}

for (const t of CASES.transitions.rejected) {
  test(`[corpus] transition rejected: ${t.from} -> ${t.to}`, () => {
    assert.equal(canTransition(t.from, t.to), false);
  });
}

test("failed job must carry a structured error", () => {
  const job = { ...validJob(), status: "failed" } as unknown as Job;
  assert.equal(validateJob(job).valid, false, "failed without error is invalid");
  const withError = {
    ...job,
    error: { code: "LOCK_CONFLICT", message: "project is locked" },
  };
  assert.equal(validateJob(withError).valid, true);
});

test("non-failed job must not carry an error", () => {
  const job = {
    ...validJob(),
    error: { code: "INTERNAL_ERROR", message: "x" },
  } as unknown as Job;
  assert.equal(validateJob(job).valid, false);
});

test("claimed and running jobs require worker ownership", () => {
  const claim = {
    worker_id: "worker_1",
    claimed_at: "2026-09-15T04:00:01Z",
    lease_expires_at: "2026-09-15T04:05:01Z",
  };
  for (const status of ["claimed", "running"] as const) {
    const without = { ...validJob(), status } as unknown as Job;
    assert.equal(validateJob(without).valid, false, `${status} needs a claim`);
    const withClaim = { ...without, claim };
    assert.equal(
      validateJob(withClaim).valid,
      true,
      JSON.stringify(validateJob(withClaim).errors),
    );
  }
});

test("queued job must not be owned by a worker", () => {
  const job = {
    ...validJob(),
    claim: {
      worker_id: "worker_1",
      claimed_at: "2026-09-15T04:00:01Z",
      lease_expires_at: "2026-09-15T04:05:01Z",
    },
  } as unknown as Job;
  assert.equal(validateJob(job).valid, false);
});

test("only a succeeded job may carry a result", () => {
  const job = { ...validJob(), result: { ok: true } } as unknown as Job;
  assert.equal(validateJob(job).valid, false);
});

// ---------------------------------------------------------------------------
// Canonical encoding
// ---------------------------------------------------------------------------

for (const c of CASES.canonicalize.cases) {
  test(`[corpus] canonicalize: ${c.name}`, () => {
    assert.equal(canonicalize(c.value), c.expected);
  });
}

for (const c of CASES.canonicalize.rejected) {
  test(`[corpus] canonicalize rejects: ${c.name}`, () => {
    assert.throws(() => canonicalize(decode(c.value)));
  });
}

test("encodeFloat64 rejects non-finite values", () => {
  for (const v of [NaN, Infinity, -Infinity]) {
    assert.throws(() => encodeFloat64(v));
  }
});

test("canonicalize is order-independent for object keys", () => {
  assert.equal(
    canonicalize({ a: 1, b: 2 }),
    canonicalize({ b: 2, a: 1 }),
  );
});

// ---------------------------------------------------------------------------
// Idempotency: mutation identity comes from the originating request
// ---------------------------------------------------------------------------

test("[corpus] idempotency key matches the recorded value", () => {
  assert.equal(
    deriveIdempotencyKey(CASES.idempotency.base),
    CASES.idempotency.expected_key,
  );
});

test("A: same request_id + operation_index + project -> same key", () => {
  const base = CASES.idempotency.base;
  assert.equal(deriveIdempotencyKey(base), deriveIdempotencyKey({ ...base }));
});

test("A: same origin yields the same job identity via the builder", () => {
  const first = validJob();
  const again = createMoveObjectJob({
    ...BASE,
    request_id: "req_abc123",
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
  });
  assert.equal(again.ok, true);
  assert.equal(again.ok && again.job.idempotency_key, first.idempotency_key);
});

test("B: a retry of the same submission keeps the same key", () => {
  // A retry differs in job_id and created_at (a fresh attempt record) but reuses
  // request_id verbatim, so the mutation identity is unchanged.
  const original = validJob();
  const retry = createMoveObjectJob({
    ...BASE,
    job_id: "job_RETRY",
    created_at: "2026-09-15T04:09:00Z",
    request_id: "req_abc123",
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
  });
  assert.equal(retry.ok, true);
  assert.equal(retry.ok && retry.job.idempotency_key, original.idempotency_key);
  assert.notEqual(retry.ok && retry.job.job_id, original.job_id);
});

test("C: a different request_id with an identical payload -> different key", () => {
  const original = validJob();
  const newCommand = createMoveObjectJob({
    ...BASE,
    job_id: "job_2",
    request_id: "req_xyz789",
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
  });
  assert.equal(newCommand.ok, true);
  assert.notEqual(
    newCommand.ok && newCommand.job.idempotency_key,
    original.idempotency_key,
  );
  // The payloads are byte-identical; only the origin differs.
  assert.deepEqual(
    newCommand.ok && newCommand.job.payload,
    original.payload,
  );
});

test("D: operation_index 0 vs 1 within one request -> different keys", () => {
  const base = CASES.idempotency.base;
  assert.notEqual(
    deriveIdempotencyKey({ ...base, operation_index: 1 }),
    deriveIdempotencyKey(base),
  );
  const first = createMoveObjectJob({
    ...BASE,
    request_id: "req_multi",
    operation_index: 0,
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
  });
  const second = createMoveObjectJob({
    ...BASE,
    job_id: "job_2",
    request_id: "req_multi",
    operation_index: 1,
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
  });
  assert.equal(first.ok && second.ok, true);
  assert.notEqual(
    first.ok && first.job.idempotency_key,
    second.ok && second.job.idempotency_key,
  );
});

test("E: the same request_id in different projects is isolated", () => {
  const base = CASES.idempotency.base;
  assert.notEqual(
    deriveIdempotencyKey({ ...base, project_id: "proj_2" }),
    deriveIdempotencyKey(base),
  );
});

test("F: two intentional 'Move Cube 50 cm right' commands are two jobs", () => {
  // "Move Cube 50 cm right." then later "Move Cube 50 cm right again."
  // Identical resolved payloads, distinct submissions -> both must execute.
  const first = createMoveObjectJob({
    ...BASE,
    job_id: "job_first",
    request_id: "req_first",
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
  });
  const second = createMoveObjectJob({
    ...BASE,
    job_id: "job_second",
    request_id: "req_second",
    target: { name: "Cube" },
    delta_meters: { x: 0.5, y: 0, z: 0 },
  });
  assert.equal(first.ok && second.ok, true);
  if (!first.ok || !second.ok) return;

  assert.notEqual(first.job.job_id, second.job.job_id, "distinct job records");
  assert.notEqual(
    first.job.idempotency_key,
    second.job.idempotency_key,
    "distinct mutations: both must execute",
  );
  // Same content, by design.
  assert.equal(first.job.content_fingerprint, second.job.content_fingerprint);
  assert.deepEqual(first.job.payload, second.job.payload);
});

for (const v of CASES.idempotency.different_key_variants) {
  test(`[corpus] key changes for ${v.name}`, () => {
    const base = CASES.idempotency.base;
    assert.notEqual(
      deriveIdempotencyKey({ ...base, [v.field]: v.value }),
      deriveIdempotencyKey(base),
    );
  });
}

for (const v of CASES.idempotency.rejected) {
  test(`[corpus] idempotency rejects ${v.name}`, () => {
    const base = CASES.idempotency.base;
    assert.throws(() =>
      deriveIdempotencyKey({ ...base, [v.field]: v.value }),
    );
  });
}

test("derived key has the documented shape", () => {
  assert.match(validJob().idempotency_key, /^idem_[0-9a-f]{64}$/);
});

test("origin is preserved on the job for auditability", () => {
  const job = validJob();
  assert.deepEqual(job.origin, { request_id: "req_abc123", operation_index: 0 });
});

test("a job cannot be built without a request_id", () => {
  for (const request_id of ["", "   "]) {
    const r = createMoveObjectJob({
      ...BASE,
      request_id,
      target: { name: "Cube" },
      delta_meters: { x: 0.5, y: 0, z: 0 },
    });
    assert.equal(r.ok, false);
  }
});

test("a job cannot be built with an invalid operation_index", () => {
  for (const operation_index of [-1, 0.5, NaN]) {
    const r = createMoveObjectJob({
      ...BASE,
      request_id: "req_abc123",
      operation_index,
      target: { name: "Cube" },
      delta_meters: { x: 0.5, y: 0, z: 0 },
    });
    assert.equal(r.ok, false, `${operation_index} must be rejected`);
  }
});

test("missing origin is rejected", () => {
  const job = validJob() as Record<string, unknown>;
  delete job.origin;
  assert.equal(validateJob(job).valid, false);
});

// ---------------------------------------------------------------------------
// Content fingerprint is diagnostics only
// ---------------------------------------------------------------------------

test("[corpus] content fingerprint matches the recorded value", () => {
  const fp = CASES.content_fingerprint;
  assert.equal(
    deriveContentFingerprint(fp.job_type, fp.payload),
    fp.expected_fingerprint,
  );
});

test("identical content shares a fingerprint but not an identity", () => {
  const fp = CASES.content_fingerprint;
  const a = deriveContentFingerprint(fp.job_type, fp.payload);
  const b = deriveContentFingerprint(fp.job_type, fp.payload);
  assert.equal(a, b);
  assert.notEqual(
    deriveIdempotencyKey({
      project_id: "proj_1",
      request_id: "req_A",
      operation_index: 0,
    }),
    deriveIdempotencyKey({
      project_id: "proj_1",
      request_id: "req_B",
      operation_index: 0,
    }),
  );
});

test("fingerprint and idempotency key use distinct namespaces", () => {
  const job = validJob();
  assert.match(job.content_fingerprint ?? "", /^fp_/);
  assert.match(job.idempotency_key, /^idem_/);
  assert.notEqual(job.content_fingerprint, job.idempotency_key);
});

for (const v of CASES.content_fingerprint.different_payloads) {
  test(`[corpus] fingerprint changes for ${v.name}`, () => {
    const fp = CASES.content_fingerprint;
    assert.notEqual(
      deriveContentFingerprint(fp.job_type, v.payload),
      fp.expected_fingerprint,
    );
  });
}

for (const v of CASES.content_fingerprint.equivalent_payloads) {
  test(`[corpus] fingerprint unchanged for ${v.name}`, () => {
    const fp = CASES.content_fingerprint;
    assert.equal(
      deriveContentFingerprint(fp.job_type, v.payload),
      fp.expected_fingerprint,
    );
  });
}

// ---------------------------------------------------------------------------
// Duplicate queue delivery of the same job_id
// ---------------------------------------------------------------------------

for (const c of CASES.delivery.cases) {
  test(`[corpus] delivery of ${c.name} -> ${c.decision}`, () => {
    assert.equal(classifyDelivery(c.recorded_status), c.decision);
  });
}

test("only an unstarted job may execute on delivery", () => {
  assert.equal(isDuplicateDelivery(null), false);
  assert.equal(isDuplicateDelivery("queued"), false);
  for (const status of ["claimed", "running", "succeeded", "failed"] as const) {
    assert.equal(
      isDuplicateDelivery(status),
      true,
      `${status} delivery must not re-execute`,
    );
  }
});

test("every status has a delivery decision", () => {
  for (const status of JOB_STATUSES) {
    assert.ok(
      ["execute", "already_owned", "reuse_result"].includes(
        classifyDelivery(status),
      ),
      `${status} must classify`,
    );
  }
});

// ---------------------------------------------------------------------------
// Serialization round trip
// ---------------------------------------------------------------------------

test("serialize/parse round trip preserves the job exactly", () => {
  const job = validJob();
  const serialized = serializeJob(job);
  const parsed = parseJob(serialized);
  assert.equal(parsed.ok, true, parsed.ok ? "" : JSON.stringify(parsed.errors));
  assert.deepEqual(parsed.ok && parsed.job, toJobWire(job));
});

test("round trip preserves the exact delta value", () => {
  const job = validJob();
  const parsed = parseJob(serializeJob(job));
  assert.equal(
    parsed.ok && (parsed.job as MoveObjectJob).payload.delta_meters.x,
    0.5,
  );
});

test("parsing malformed JSON fails cleanly", () => {
  const r = parseJob("{not json");
  assert.equal(r.ok, false);
  assert.equal(!r.ok && r.errors[0]?.code, "VALIDATION_ERROR");
});

test("parsing a structurally invalid job fails", () => {
  const r = parseJob(JSON.stringify({ job_id: "j", job_type: "move_object" }));
  assert.equal(r.ok, false);
});

test("serialized job is schema-valid after a round trip", () => {
  const parsed = parseJob(serializeJob(validJob()));
  assert.equal(parsed.ok, true);
  const result = validateAgainstSchema(
    SCHEMA_FILES.Job,
    parsed.ok ? parsed.job : {},
  );
  assert.equal(result.valid, true, JSON.stringify(result.violations));
});
