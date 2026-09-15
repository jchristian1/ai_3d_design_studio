/**
 * Conformance tests: TypeScript representation vs. CANONICAL schemas.
 *
 * These tests are the drift guard. They read packages/contracts/schemas/*.json
 * at runtime and assert that:
 *   1. the shared, language-neutral case corpus validates identically here,
 *   2. the TS constants (ERROR_CODES, JOB_STATUSES, JOB_TYPES) equal the
 *      canonical enums,
 *   3. every canonical field is present in the TS representation and vice versa,
 *   4. instances built from the TS types serialize to schema-valid documents,
 *   5. the runtime validation rules agree with the canonical schema.
 *
 * test_conformance.py runs the equivalent assertions against the SAME files.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  ERROR_CODES,
  SCHEMA_FILES,
  SCHEMA_DIR,
  listSchemaNames,
  schemaEnum,
  schemaProperties,
  schemaRequired,
  toWire,
  validateAgainstSchema,
} from "./index.ts";
import type { ChatRequest, ChatResponse } from "./index.ts";
import { JOB_STATUSES, JOB_TYPES } from "@studio/types";
import type { Job, Vec3 } from "@studio/types";
import { CHAT_REQUEST_FIELDS, validateChatRequest } from "@studio/validation";

interface Case {
  name: string;
  data: unknown;
}
interface Suite {
  schema: string;
  valid: Case[];
  invalid: Case[];
}

const corpus = JSON.parse(
  readFileSync(join(SCHEMA_DIR, "conformance-cases.json"), "utf8"),
) as { suites: Suite[] };

// ---------------------------------------------------------------------------
// 1. Shared language-neutral corpus
// ---------------------------------------------------------------------------

test("canonical schema directory contains the expected schemas", () => {
  const names = listSchemaNames();
  for (const file of Object.values(SCHEMA_FILES)) {
    assert.ok(names.includes(file), `missing canonical schema: ${file}`);
  }
});

for (const suite of corpus.suites) {
  for (const c of suite.valid) {
    test(`[corpus] ${suite.schema} accepts: ${c.name}`, () => {
      const r = validateAgainstSchema(suite.schema, c.data);
      assert.equal(
        r.valid,
        true,
        `expected valid, got violations: ${JSON.stringify(r.violations)}`,
      );
    });
  }
  for (const c of suite.invalid) {
    test(`[corpus] ${suite.schema} rejects: ${c.name}`, () => {
      const r = validateAgainstSchema(suite.schema, c.data);
      assert.equal(r.valid, false, "expected schema violation");
    });
  }
}

// ---------------------------------------------------------------------------
// 2. Enum parity with canonical schemas
// ---------------------------------------------------------------------------

test("ERROR_CODES equals canonical error-code enum", () => {
  assert.deepEqual([...ERROR_CODES], schemaEnum(SCHEMA_FILES.ErrorCode));
});

test("JOB_STATUSES equals canonical job.status enum", () => {
  assert.deepEqual([...JOB_STATUSES], schemaEnum(SCHEMA_FILES.Job, ["status"]));
});

test("JOB_TYPES equals canonical job.type enum", () => {
  assert.deepEqual([...JOB_TYPES], schemaEnum(SCHEMA_FILES.Job, ["type"]));
});

// ---------------------------------------------------------------------------
// 3. Field parity: canonical properties vs. TS representation
// ---------------------------------------------------------------------------

/**
 * The TS structural types are erased at runtime, so field parity is asserted by
 * building a maximal instance (every property populated) and checking that its
 * key set equals the canonical property set. If a schema property is added and
 * the interface is not updated, this test fails to compile or to match.
 */
test("ChatRequest field set matches canonical schema", () => {
  const maximal: Required<ChatRequest> = {
    project_id: "proj_1",
    session_id: "sess_1",
    message: "Move Cube 50 cm to the right",
    selected_object_id: "obj_8d83f",
  };
  assert.deepEqual(
    Object.keys(maximal).sort(),
    schemaProperties(SCHEMA_FILES.ChatRequest),
  );
  assert.deepEqual(
    ["message", "project_id", "session_id"],
    schemaRequired(SCHEMA_FILES.ChatRequest),
  );
});

test("ChatResponse field set matches canonical schema", () => {
  const maximal: Required<ChatResponse> = {
    status: "error",
    summary: "Object not found",
    object_position: { x: 0, y: 0, z: 0 },
    preview_url: "s3://previews/p.png",
    error: { code: "OBJECT_NOT_FOUND", message: "no object named 'Cube'" },
  };
  assert.deepEqual(
    Object.keys(maximal).sort(),
    schemaProperties(SCHEMA_FILES.ChatResponse),
  );
  assert.deepEqual(["status", "summary"], schemaRequired(SCHEMA_FILES.ChatResponse));
});

test("Job field set matches canonical schema", () => {
  const maximal: Required<Job> = {
    id: "job_1",
    project_id: "proj_1",
    session_id: "sess_1",
    type: "chat",
    status: "queued",
    created_at: "2026-09-15T04:00:00Z",
    result: null,
  };
  assert.deepEqual(Object.keys(maximal).sort(), schemaProperties(SCHEMA_FILES.Job));
  assert.deepEqual(
    ["created_at", "id", "project_id", "session_id", "status", "type"],
    schemaRequired(SCHEMA_FILES.Job),
  );
});

test("Vec3 field set matches canonical schema", () => {
  const maximal: Required<Vec3> = { x: 0, y: 0, z: 0 };
  assert.deepEqual(Object.keys(maximal).sort(), schemaProperties(SCHEMA_FILES.Vec3));
});

// ---------------------------------------------------------------------------
// 4. Instances built from TS types are schema-valid on the wire
// ---------------------------------------------------------------------------

test("TS-built ChatRequest serializes to a schema-valid document", () => {
  const req: ChatRequest = {
    project_id: "proj_1",
    session_id: "sess_1",
    message: "Move Cube 50 cm to the right",
  };
  const r = validateAgainstSchema(SCHEMA_FILES.ChatRequest, toWire(req));
  assert.equal(r.valid, true, JSON.stringify(r.violations));
});

test("TS-built success ChatResponse serializes to a schema-valid document", () => {
  const res: ChatResponse = {
    status: "success",
    summary: "Moved Cube 0.50 m on +X",
    object_position: { x: 0.5, y: 0, z: 0 },
    preview_url: "s3://previews/p.png",
  };
  const r = validateAgainstSchema(SCHEMA_FILES.ChatResponse, toWire(res));
  assert.equal(r.valid, true, JSON.stringify(r.violations));
});

test("TS-built error ChatResponse serializes to a schema-valid document", () => {
  const res: ChatResponse = {
    status: "error",
    summary: "Object not found",
    error: { code: "OBJECT_NOT_FOUND", message: "no object named 'Cube'" },
  };
  const r = validateAgainstSchema(SCHEMA_FILES.ChatResponse, toWire(res));
  assert.equal(r.valid, true, JSON.stringify(r.violations));
});

test("TS-built Job serializes to a schema-valid document", () => {
  const job: Job = {
    id: "job_1",
    project_id: "proj_1",
    session_id: "sess_1",
    type: "chat",
    status: "queued",
    created_at: new Date().toISOString(),
  };
  const r = validateAgainstSchema(SCHEMA_FILES.Job, toWire(job));
  assert.equal(r.valid, true, JSON.stringify(r.violations));
});

// ---------------------------------------------------------------------------
// 5. Runtime validation rules agree with the canonical schema
// ---------------------------------------------------------------------------

test("validateChatRequest agrees with canonical schema over the corpus", () => {
  const suite = corpus.suites.find((s) => s.schema === SCHEMA_FILES.ChatRequest);
  assert.ok(suite, "chat-request suite present in corpus");
  for (const c of suite.valid) {
    assert.equal(
      validateChatRequest(c.data).valid,
      true,
      `validation rejected schema-valid case: ${c.name}`,
    );
  }
  for (const c of suite.invalid) {
    assert.equal(
      validateChatRequest(c.data).valid,
      false,
      `validation accepted schema-invalid case: ${c.name}`,
    );
  }
});

test("CHAT_REQUEST_FIELDS equals canonical ChatRequest properties", () => {
  assert.deepEqual(
    [...CHAT_REQUEST_FIELDS],
    schemaProperties(SCHEMA_FILES.ChatRequest),
  );
});
