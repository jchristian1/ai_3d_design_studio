/**
 * API client tests (Spec 001, Task 11).
 *
 * Exercises the real client against a stub transport, so request shapes, error
 * translation, and URL building are verified rather than assumed.
 *
 * Covers required behaviours 3, 12, 13, 14 and 15.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { createApiClient } from "../lib/api/client.ts";
import { ApiFailure } from "../lib/api/errors.ts";
import { resolveApiBaseUrl, DEFAULT_API_BASE_URL } from "../lib/config.ts";
import {
  API_BASE,
  MOVE_COMMAND,
  PROJECT_ID,
  StubTransport,
  health,
  jobPath,
  jobStatus,
  preview,
  submission,
} from "./support.ts";

function client(transport: StubTransport) {
  return createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });
}

// ---------------------------------------------------------------------------
// 3. The exact Spec 001 command produces the correct ChatRequest
// ---------------------------------------------------------------------------

test("3: submitChat sends the canonical ChatRequest fields and nothing else", async () => {
  const transport = new StubTransport().onPost("/api/chat", {
    status: 202,
    body: submission({ request_id: "req_abc" }),
  });

  await client(transport).submitChat({
    request_id: "req_abc",
    project_id: PROJECT_ID,
    session_id: "sess_xyz",
    message: MOVE_COMMAND,
  });

  const call = transport.calls[0];
  assert.ok(call);
  assert.equal(call.url, "/api/chat");
  assert.equal(call.method, "POST");
  assert.deepEqual(call.body, {
    request_id: "req_abc",
    project_id: PROJECT_ID,
    session_id: "sess_xyz",
    message: MOVE_COMMAND,
  });

  // The canonical ChatRequest has no user_id; sending one would be a 422.
  assert.ok(!Object.keys(call.body as object).includes("user_id"));
});

test("3: submitChat returns the typed submission", async () => {
  const transport = new StubTransport().onPost("/api/chat", {
    status: 202,
    body: submission({ request_id: "req_abc", job_status: "queued" }),
  });

  const result = await client(transport).submitChat({
    request_id: "req_abc",
    project_id: PROJECT_ID,
    session_id: "s",
    message: MOVE_COMMAND,
  });

  assert.equal(result.job_id, "job_req_abc_0");
  assert.equal(result.job_status, "queued");
  assert.equal(result.duplicate, false);
});

// ---------------------------------------------------------------------------
// Job status uses the project-scoped path
// ---------------------------------------------------------------------------

test("getJobStatus requests the project-scoped job URL", async () => {
  const transport = new StubTransport().onGet(jobPath("job_1"), {
    status: 200,
    body: jobStatus("running", { job_id: "job_1" }),
  });

  const status = await client(transport).getJobStatus(PROJECT_ID, "job_1");

  assert.equal(transport.paths("GET")[0], `/api/projects/${PROJECT_ID}/jobs/job_1`);
  assert.equal(status.job_status, "running");
});

test("getJobStatus encodes its path segments", async () => {
  const transport = new StubTransport().on(
    (path) => path.startsWith("/api/projects/"),
    { status: 404, body: { error: { code: "VALIDATION_ERROR", message: "no" } } },
  );

  await assert.rejects(
    () => client(transport).getJobStatus("proj/../other", "job 1"),
    ApiFailure,
  );
  const requested = transport.paths("GET")[0] ?? "";
  assert.ok(!requested.includes("/../"), `path was not encoded: ${requested}`);
  assert.ok(requested.includes("proj%2F..%2Fother"));
});

// ---------------------------------------------------------------------------
// 12 / 13. Backend codes become friendly messages
// ---------------------------------------------------------------------------

test("12: a 503 BLENDER_UNAVAILABLE becomes a friendly no-worker message", async () => {
  const transport = new StubTransport().onPost("/api/chat", {
    status: 503,
    body: {
      error: {
        code: "BLENDER_UNAVAILABLE",
        message: "no Blender worker is currently available…",
      },
    },
  });

  await assert.rejects(
    () =>
      client(transport).submitChat({
        request_id: "r",
        project_id: PROJECT_ID,
        session_id: "s",
        message: MOVE_COMMAND,
      }),
    (error: unknown) => {
      assert.ok(error instanceof ApiFailure);
      assert.equal(error.kind, "no_worker");
      assert.equal(error.code, "BLENDER_UNAVAILABLE");
      assert.match(error.message, /design machine is currently unavailable/i);
      assert.match(error.message, /nothing was changed/i);
      return true;
    },
  );
});

test("13: UNSUPPORTED_INSTRUCTION becomes a friendly, actionable message", async () => {
  const transport = new StubTransport().onPost("/api/chat", {
    status: 422,
    body: {
      error: { code: "UNSUPPORTED_INSTRUCTION", message: "I can only handle…" },
    },
  });

  await assert.rejects(
    () =>
      client(transport).submitChat({
        request_id: "r",
        project_id: PROJECT_ID,
        session_id: "s",
        message: "Make it prettier",
      }),
    (error: unknown) => {
      assert.ok(error instanceof ApiFailure);
      assert.equal(error.kind, "unsupported");
      assert.match(error.message, /isn't supported yet/i);
      // Suggests what DOES work.
      assert.match(error.message, /Move Cube 50 cm/);
      return true;
    },
  );
});

test("an unrecognised error code never leaks the raw code into the message", async () => {
  const transport = new StubTransport().onPost("/api/chat", {
    status: 500,
    body: { error: { code: "SOME_FUTURE_CODE", message: "internal detail" } },
  });

  await assert.rejects(
    () =>
      client(transport).submitChat({
        request_id: "r",
        project_id: PROJECT_ID,
        session_id: "s",
        message: MOVE_COMMAND,
      }),
    (error: unknown) => {
      assert.ok(error instanceof ApiFailure);
      assert.ok(!error.message.includes("SOME_FUTURE_CODE"));
      assert.ok(!error.message.includes("internal detail"));
      // Still recorded for a debug surface.
      assert.equal(error.code, "SOME_FUTURE_CODE");
      return true;
    },
  );
});

test("an unreachable API becomes an offline failure", async () => {
  const failing = (() => Promise.reject(new TypeError("fetch failed"))) as typeof fetch;
  const offline = createApiClient({ baseUrl: API_BASE, fetchImpl: failing });

  await assert.rejects(
    () => offline.getHealth(),
    (error: unknown) => {
      assert.ok(error instanceof ApiFailure);
      assert.equal(error.kind, "offline");
      assert.match(error.message, /Can't reach the design studio service/i);
      return true;
    },
  );
});

test("an aborted request becomes a timeout failure", async () => {
  const hanging = ((_url: string, init?: RequestInit) =>
    new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        const error = new Error("aborted");
        error.name = "AbortError";
        reject(error);
      });
    })) as unknown as typeof fetch;

  const slow = createApiClient({
    baseUrl: API_BASE,
    fetchImpl: hanging,
    timeoutMs: 5,
  });

  await assert.rejects(
    () => slow.getHealth(),
    (error: unknown) => {
      assert.ok(error instanceof ApiFailure);
      assert.equal(error.kind, "timeout");
      return true;
    },
  );
});

test("a malformed success body becomes a malformed failure", async () => {
  const transport = new StubTransport().onGet("/health", {
    status: 200,
    body: "not json at all",
  });

  await assert.rejects(
    () => client(transport).getHealth(),
    (error: unknown) => {
      assert.ok(error instanceof ApiFailure);
      assert.equal(error.kind, "malformed");
      return true;
    },
  );
});

test("a non-JSON error body still yields a clean failure", async () => {
  const transport = new StubTransport().onPost("/api/chat", {
    status: 500,
    body: "<html>Internal Server Error</html>",
  });

  await assert.rejects(
    () =>
      client(transport).submitChat({
        request_id: "r",
        project_id: PROJECT_ID,
        session_id: "s",
        message: MOVE_COMMAND,
      }),
    (error: unknown) => {
      assert.ok(error instanceof ApiFailure);
      assert.ok(!error.message.includes("<html>"));
      return true;
    },
  );
});

// ---------------------------------------------------------------------------
// 14 / 15. Artifact URLs use the API base and contain no filesystem path
// ---------------------------------------------------------------------------

test("14: getArtifactUrl builds an absolute URL from the configured API base", () => {
  const built = createApiClient({
    baseUrl: "http://127.0.0.1:8000",
    fetchImpl: new StubTransport().fetch,
  }).getArtifactUrl(`/api/projects/${PROJECT_ID}/artifacts/preview_abc123def456`);

  assert.equal(
    built,
    `http://127.0.0.1:8000/api/projects/${PROJECT_ID}/artifacts/preview_abc123def456`,
  );
});

test("14: a different configured base is honoured", () => {
  const built = createApiClient({
    baseUrl: "https://studio.example.com/",
    fetchImpl: new StubTransport().fetch,
  }).getArtifactUrl("/api/projects/proj_seed/artifacts/preview_abc123def456");

  // Trailing slash on the base must not produce a double slash.
  assert.equal(
    built,
    "https://studio.example.com/api/projects/proj_seed/artifacts/preview_abc123def456",
  );
});

test("14: an already-absolute URL is passed through unchanged", () => {
  const absolute = "https://cdn.example.com/preview.png";
  assert.equal(
    createApiClient({ baseUrl: API_BASE, fetchImpl: new StubTransport().fetch })
      .getArtifactUrl(absolute),
    absolute,
  );
});

test("15: an artifact URL is never a filesystem path", async () => {
  const transport = new StubTransport().onGet(jobPath("job_1"), {
    status: 200,
    body: jobStatus("succeeded", { job_id: "job_1" }),
  });

  const status = await client(transport).getJobStatus(PROJECT_ID, "job_1");
  const url = client(transport).getArtifactUrl(status.preview!.url);

  assert.ok(url.startsWith("http://"));
  for (const forbidden of [".png", ".blend", "/home", "/tmp", "runtime/artifacts"]) {
    assert.ok(!url.includes(forbidden), `artifact URL leaked ${forbidden}`);
  }
});

test("15: the job status payload carries no filesystem path", async () => {
  const transport = new StubTransport().onGet(jobPath("job_1"), {
    status: 200,
    body: jobStatus("succeeded", { job_id: "job_1" }),
  });

  const status = await client(transport).getJobStatus(PROJECT_ID, "job_1");
  const serialized = JSON.stringify(status);

  for (const forbidden of [".blend", "/home/", "/tmp/", "runtime/"]) {
    assert.ok(!serialized.includes(forbidden), `job payload leaked ${forbidden}`);
  }
});

// ---------------------------------------------------------------------------
// Latest preview and health
// ---------------------------------------------------------------------------

test("getLatestPreview returns null for 404 instead of failing", async () => {
  const transport = new StubTransport().onGet(
    `/api/projects/${PROJECT_ID}/preview/latest`,
    {
      status: 404,
      body: { error: { code: "VALIDATION_ERROR", message: "no preview yet" } },
    },
  );

  // The empty state is a normal condition, not an error to show the user.
  assert.equal(await client(transport).getLatestPreview(PROJECT_ID), null);
});

test("getLatestPreview returns the artifact when one exists", async () => {
  const transport = new StubTransport().onGet(
    `/api/projects/${PROJECT_ID}/preview/latest`,
    { status: 200, body: preview({ artifact_id: "preview_1111222233334444" }) },
  );

  const latest = await client(transport).getLatestPreview(PROJECT_ID);
  assert.equal(latest?.artifact_id, "preview_1111222233334444");
});

test("getHealth returns the typed health view", async () => {
  const transport = new StubTransport().onGet("/health", {
    status: 200,
    body: health({ ready_workers: 1, blender_capable_workers: 1 }),
  });

  const result = await client(transport).getHealth();
  assert.equal(result.api, "healthy");
  assert.equal(result.blender_capable_workers, 1);
});

// ---------------------------------------------------------------------------
// Base URL configuration
// ---------------------------------------------------------------------------

test("the API base URL falls back to loopback when unset", () => {
  assert.equal(resolveApiBaseUrl(undefined), DEFAULT_API_BASE_URL);
  assert.equal(resolveApiBaseUrl(""), DEFAULT_API_BASE_URL);
  assert.equal(resolveApiBaseUrl("   "), DEFAULT_API_BASE_URL);
});

test("the API base URL honours the environment and trims trailing slashes", () => {
  assert.equal(
    resolveApiBaseUrl("https://studio.example.com///"),
    "https://studio.example.com",
  );
});
