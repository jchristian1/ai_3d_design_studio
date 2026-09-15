import { test } from "node:test";
import assert from "node:assert/strict";

import { ERROR_CODES } from "./index.ts";
import type { ChatRequest, ChatResponse } from "./index.ts";
import { JOB_STATUSES, JOB_TYPES } from "@studio/types";
import type { Job, Vec3 } from "@studio/types";

test("ChatRequest requires project_id/session_id/message shape", () => {
  const req: ChatRequest = {
    project_id: "proj_1",
    session_id: "sess_1",
    message: "Move Cube 50 cm to the right",
  };
  assert.equal(req.project_id, "proj_1");
  assert.equal(req.selected_object_id, undefined);
});

test("ChatResponse carries success + position in meters", () => {
  const pos: Vec3 = { x: 0.5, y: 0, z: 0 };
  const res: ChatResponse = {
    status: "success",
    summary: "Moved Cube 0.50 m on +X",
    object_position: pos,
    preview_url: "s3://previews/p.png",
  };
  assert.equal(res.status, "success");
  assert.equal(res.object_position?.x, 0.5);
});

test("ChatResponse error uses structured code", () => {
  const res: ChatResponse = {
    status: "error",
    summary: "Object not found",
    error: { code: "OBJECT_NOT_FOUND", message: "no object 'Cube'" },
  };
  assert.ok(ERROR_CODES.includes(res.error!.code));
});

test("Job is project-scoped with valid status/type", () => {
  const job: Job = {
    id: "job_1",
    project_id: "proj_1",
    session_id: "sess_1",
    type: "chat",
    status: "queued",
    created_at: new Date().toISOString(),
  };
  assert.ok(JOB_STATUSES.includes(job.status));
  assert.ok(JOB_TYPES.includes(job.type));
  assert.equal(typeof job.project_id, "string");
});
