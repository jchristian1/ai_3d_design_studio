/**
 * Session logic tests (Spec 001, Task 11).
 *
 * The reducer, the identifier generation, and the job-polling loop — the parts of
 * this UI where the interesting decisions live. Tested as data and with a manual
 * clock, so no DOM or real delay is involved.
 *
 * Covers required behaviours 4, 5, 6, 7, 8, 9, 10, 11, 16 and 17.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiFailure } from "../lib/api/errors.ts";
import { createApiClient } from "../lib/api/client.ts";
import { newRequestId, newSessionId, uuid } from "../lib/ids.ts";
import { createPollingJobUpdates } from "../lib/session/jobUpdates.ts";
import {
  initialSessionState,
  isBusy,
  sessionReducer,
} from "../lib/session/reducer.ts";
import type { SessionState } from "../lib/session/types.ts";
import { STATUS_TEXT } from "../lib/session/types.ts";
import {
  API_BASE,
  MOVE_COMMAND,
  ManualScheduler,
  PROJECT_ID,
  StubTransport,
  flush,
  jobPath,
  jobStatus,
  preview,
} from "./support.ts";

function start(): SessionState {
  return initialSessionState({ sessionId: "sess_1", projectId: PROJECT_ID });
}

function submitted(requestId = "req_1"): SessionState {
  return sessionReducer(start(), {
    type: "submission_started",
    requestId,
    message: MOVE_COMMAND,
    messageId: `user_${requestId}`,
  });
}

function accepted(requestId = "req_1"): SessionState {
  return sessionReducer(submitted(requestId), {
    type: "submission_accepted",
    jobId: `job_${requestId}_0`,
    jobStatus: "queued",
    duplicate: false,
  });
}

function studioTexts(state: SessionState): string[] {
  return state.messages.filter((m) => m.author === "studio").map((m) => m.text);
}

// ---------------------------------------------------------------------------
// 4 / 5 / 16. Identifiers
// ---------------------------------------------------------------------------

test("4: a request id is generated, prefixed, and unique", () => {
  const first = newRequestId();
  const second = newRequestId();

  assert.match(first, /^req_[0-9a-f]{32}$/);
  assert.notEqual(first, second);
});

test("4: identifiers come from a cryptographic UUID source", () => {
  // v4 UUID: version nibble 4, variant nibble in 8..b.
  assert.match(
    uuid(),
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );

  const seen = new Set<string>();
  for (let i = 0; i < 2000; i += 1) seen.add(newRequestId());
  assert.equal(seen.size, 2000, "request ids must not collide");
});

test("5: a session id is generated once and is distinct from request ids", () => {
  const sessionId = newSessionId();
  assert.match(sessionId, /^sess_[0-9a-f]{32}$/);
  assert.notEqual(sessionId, newRequestId());
});

test("5: the session id is fixed for the lifetime of the state", () => {
  let state = start();
  const sessionId = state.sessionId;

  state = accepted("req_1");
  state = sessionReducer(state, { type: "job_status", jobStatus: "running" });
  state = sessionReducer(state, {
    type: "job_succeeded",
    preview: preview(),
    previewWarning: null,
  });

  assert.equal(state.sessionId, sessionId);
});

test("16: a second intentional command uses a different request id", () => {
  const first = accepted("req_1");
  const firstId = first.submission?.requestId;

  // A new user message is a new mutation: a new id, generated fresh.
  const secondId = newRequestId();
  const second = sessionReducer(first, {
    type: "submission_started",
    requestId: secondId,
    message: MOVE_COMMAND,
    messageId: `user_${secondId}`,
  });

  assert.notEqual(secondId, firstId);
  assert.equal(second.submission?.requestId, secondId);
  // Both user messages remain in the transcript.
  assert.equal(second.messages.filter((m) => m.author === "user").length, 2);
});

// ---------------------------------------------------------------------------
// 7. Progress states display correctly
// ---------------------------------------------------------------------------

test("7: queued, claimed and running each produce user-facing progress text", () => {
  let state = accepted();
  assert.equal(state.submission?.jobStatus, "queued");
  assert.ok(studioTexts(state).at(-1)?.includes("Sending to the design machine"));

  state = sessionReducer(state, { type: "job_status", jobStatus: "claimed" });
  assert.equal(state.submission?.jobStatus, "claimed");
  assert.equal(studioTexts(state).at(-1), STATUS_TEXT.claimed);

  state = sessionReducer(state, { type: "job_status", jobStatus: "running" });
  assert.equal(state.submission?.jobStatus, "running");
  assert.equal(studioTexts(state).at(-1), STATUS_TEXT.running);

  assert.equal(state.phase, "tracking");
  assert.equal(isBusy(state), true);
});

test("7: progress updates replace one another instead of piling up", () => {
  let state = accepted();
  state = sessionReducer(state, { type: "job_status", jobStatus: "claimed" });
  state = sessionReducer(state, { type: "job_status", jobStatus: "running" });

  // One user line, one live progress line.
  assert.equal(state.messages.length, 2);
});

test("7: no status text exposes implementation vocabulary", () => {
  const forbidden = [
    "WorkerExecutor",
    "MCP",
    "MoveObjectPlan",
    "idempotency",
    "job_id",
    "bpy",
    "Blender",
    "worker",
  ];
  for (const text of Object.values(STATUS_TEXT)) {
    for (const word of forbidden) {
      assert.ok(
        !text.toLowerCase().includes(word.toLowerCase()),
        `status text leaks "${word}": ${text}`,
      );
    }
  }
});

test("a repeated status does not add a duplicate message", () => {
  let state = accepted();
  const before = state.messages.length;
  state = sessionReducer(state, { type: "job_status", jobStatus: "queued" });
  assert.equal(state.messages.length, before);
});

test("a duplicate submission says so plainly", () => {
  const state = sessionReducer(submitted(), {
    type: "submission_accepted",
    jobId: "job_req_1_0",
    jobStatus: "succeeded",
    duplicate: true,
  });
  assert.match(studioTexts(state).at(-1) ?? "", /already submitted/i);
});

// ---------------------------------------------------------------------------
// 9 / 10. Success, preview, and the degraded preview case
// ---------------------------------------------------------------------------

test("9: a succeeded job with a preview updates the displayed image", () => {
  const artifact = preview({ artifact_id: "preview_1234abcd5678efab" });
  const state = sessionReducer(accepted(), {
    type: "job_succeeded",
    preview: artifact,
    previewWarning: null,
  });

  assert.equal(state.phase, "succeeded");
  assert.equal(state.preview?.artifact_id, "preview_1234abcd5678efab");
  assert.equal(state.stalePreview, null);
  assert.equal(state.previewWarning, null);
  assert.equal(isBusy(state), false);
  assert.equal(studioTexts(state).at(-1), STATUS_TEXT.succeeded);
});

test("9: a new change keeps the old preview on screen until the new one arrives", () => {
  const first = sessionReducer(accepted("req_1"), {
    type: "job_succeeded",
    preview: preview({ artifact_id: "preview_aaaa1111bbbb2222" }),
    previewWarning: null,
  });

  const second = sessionReducer(first, {
    type: "submission_started",
    requestId: "req_2",
    message: MOVE_COMMAND,
    messageId: "user_req_2",
  });

  // The panel still has something to display rather than flashing empty.
  assert.equal(second.stalePreview?.artifact_id, "preview_aaaa1111bbbb2222");

  const done = sessionReducer(second, {
    type: "job_succeeded",
    preview: preview({ artifact_id: "preview_cccc3333dddd4444" }),
    previewWarning: null,
  });
  assert.equal(done.preview?.artifact_id, "preview_cccc3333dddd4444");
  assert.equal(done.stalePreview, null);
});

test("10: a missing preview is a warning and the change still reads as applied", () => {
  const state = sessionReducer(accepted(), {
    type: "job_succeeded",
    preview: null,
    previewWarning:
      "The change was applied and saved, but no preview image could be produced.",
  });

  // The critical assertion: succeeded, not failed.
  assert.equal(state.phase, "succeeded");
  assert.equal(state.submission?.jobStatus, "succeeded");

  const message = studioTexts(state).at(-1) ?? "";
  assert.match(message, /applied and saved/i);
  assert.ok(
    !/could not be applied|failed/i.test(message),
    `a degraded preview must not claim the change failed: ${message}`,
  );

  const warned = state.messages.at(-1);
  assert.equal(warned?.tone, "warning");
});

// ---------------------------------------------------------------------------
// 11 / 17. Failures and explicit retry
// ---------------------------------------------------------------------------

test("11: a failed job records the error and is not offered as a retry", () => {
  const state = sessionReducer(accepted(), {
    type: "job_failed",
    message: "That object isn't in this project, so nothing was changed.",
  });

  assert.equal(state.phase, "failed");
  assert.equal(state.submission?.jobStatus, "failed");
  assert.equal(state.messages.at(-1)?.tone, "error");
  // A definite answer must not invite the same instruction again.
  assert.equal(state.retryable, null);
  assert.equal(isBusy(state), false);
});

test("17: a submission whose outcome is uncertain is retryable with its original id", () => {
  const state = sessionReducer(submitted("req_keep_me"), {
    type: "submission_failed",
    message: "Can't reach the design studio service.",
    retryable: true,
  });

  assert.equal(state.phase, "failed");
  assert.deepEqual(state.retryable, {
    requestId: "req_keep_me",
    message: MOVE_COMMAND,
  });
});

test("17: a job timeout is retryable, and retrying reuses the same request id", () => {
  const timedOut = sessionReducer(accepted("req_slow"), {
    type: "job_timed_out",
    message: "This change is taking longer than expected.",
  });
  assert.equal(timedOut.retryable?.requestId, "req_slow");

  // The retry re-enters submission with the ORIGINAL id, so the backend derives
  // the same mutation identity and applies the change at most once.
  const retried = sessionReducer(timedOut, {
    type: "submission_started",
    requestId: timedOut.retryable!.requestId,
    message: timedOut.retryable!.message,
    messageId: "user_retry",
  });
  assert.equal(retried.submission?.requestId, "req_slow");
});

test("17: a rejected instruction is not retryable", () => {
  const state = sessionReducer(submitted(), {
    type: "submission_failed",
    message: "That instruction isn't supported yet.",
    retryable: false,
  });
  assert.equal(state.retryable, null);
});

test("starting a new submission clears a stale retry offer", () => {
  const failed = sessionReducer(submitted("req_1"), {
    type: "submission_failed",
    message: "offline",
    retryable: true,
  });
  const next = sessionReducer(failed, {
    type: "submission_started",
    requestId: "req_2",
    message: MOVE_COMMAND,
    messageId: "user_req_2",
  });
  assert.equal(next.retryable, null);
});

// ---------------------------------------------------------------------------
// 6 / 8. Polling the project-scoped job URL, and stopping
// ---------------------------------------------------------------------------

function pollingSetup(responses: Array<ReturnType<typeof jobStatus>>) {
  let index = 0;
  const transport = new StubTransport().onGet(jobPath("job_1"), () => {
    const body = responses[Math.min(index, responses.length - 1)];
    index += 1;
    return { status: 200, body: body! };
  });
  const scheduler = new ManualScheduler();
  const client = createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });
  const source = createPollingJobUpdates(client, {
    intervalMs: 750,
    timeoutMs: 60_000,
    scheduler,
  });
  return { transport, scheduler, source };
}

test("6: polling requests the project-scoped job URL", async () => {
  const { transport, source } = pollingSetup([jobStatus("succeeded", { job_id: "job_1" })]);
  const seen: string[] = [];

  source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: (status) => seen.push(status.job_status),
    onSettled: () => {},
    onError: () => assert.fail("unexpected error"),
    onTimeout: () => assert.fail("unexpected timeout"),
  });
  await flush();

  assert.equal(transport.paths("GET")[0], `/api/projects/${PROJECT_ID}/jobs/job_1`);
  assert.deepEqual(seen, ["succeeded"]);
});

test("6: the first poll happens immediately, not after one interval", async () => {
  const { transport, source } = pollingSetup([jobStatus("queued", { job_id: "job_1" })]);
  source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: () => {},
    onSettled: () => {},
    onError: () => {},
    onTimeout: () => {},
  });
  await flush();
  assert.equal(transport.calls.length, 1);
});

test("8: polling stops once the job succeeds", async () => {
  const { transport, scheduler, source } = pollingSetup([
    jobStatus("queued", { job_id: "job_1" }),
    jobStatus("running", { job_id: "job_1" }),
    jobStatus("succeeded", { job_id: "job_1" }),
  ]);

  const seen: string[] = [];
  let settled: string | null = null;
  source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: (status) => seen.push(status.job_status),
    onSettled: (status) => {
      settled = status.job_status;
    },
    onError: () => assert.fail("unexpected error"),
    onTimeout: () => assert.fail("unexpected timeout"),
  });

  await flush();
  scheduler.advance(750);
  await flush();
  scheduler.advance(750);
  await flush();

  assert.deepEqual(seen, ["queued", "running", "succeeded"]);
  assert.equal(settled, "succeeded");

  const callsAtSettle = transport.calls.length;
  assert.equal(scheduler.pendingCount, 0, "no timer may remain scheduled");

  // Advancing further must produce no additional requests.
  scheduler.advance(10_000);
  await flush();
  assert.equal(transport.calls.length, callsAtSettle);
});

test("11: polling stops once the job fails", async () => {
  const { transport, scheduler, source } = pollingSetup([
    jobStatus("failed", {
      job_id: "job_1",
      error: { code: "OBJECT_NOT_FOUND", message: "no such object" },
    }),
  ]);

  let settled: ReturnType<typeof jobStatus> | null = null;
  source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: () => {},
    onSettled: (status) => {
      settled = status;
    },
    onError: () => assert.fail("a reported failure is not a transport error"),
    onTimeout: () => assert.fail("unexpected timeout"),
  });
  await flush();

  assert.equal(settled!.job_status, "failed");
  assert.equal(settled!.error?.code, "OBJECT_NOT_FOUND");

  const calls = transport.calls.length;
  scheduler.advance(10_000);
  await flush();
  assert.equal(transport.calls.length, calls);
});

test("polling never overlaps: one request in flight at a time", async () => {
  let resolveFirst: ((value: Response) => void) | null = null;
  let calls = 0;

  const slowFetch = ((_url: string) => {
    calls += 1;
    if (calls === 1) {
      return new Promise<Response>((resolve) => {
        resolveFirst = resolve;
      });
    }
    return Promise.resolve(
      new Response(JSON.stringify(jobStatus("succeeded", { job_id: "job_1" })), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }) as unknown as typeof fetch;

  const scheduler = new ManualScheduler();
  const source = createPollingJobUpdates(
    createApiClient({ baseUrl: API_BASE, fetchImpl: slowFetch }),
    { intervalMs: 750, timeoutMs: 60_000, scheduler },
  );

  source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: () => {},
    onSettled: () => {},
    onError: () => {},
    onTimeout: () => {},
  });
  await flush();

  assert.equal(calls, 1);
  // While the first request is unresolved, no timer exists, so time passing
  // cannot start a second request.
  assert.equal(scheduler.pendingCount, 0);
  scheduler.advance(10_000);
  await flush();
  assert.equal(calls, 1, "a second poll started before the first finished");

  resolveFirst!(
    new Response(JSON.stringify(jobStatus("running", { job_id: "job_1" })), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  await flush();
  assert.equal(scheduler.pendingCount, 1, "the next poll is scheduled after settling");
});

test("stopping a subscription prevents any further callback or request", async () => {
  const { transport, scheduler, source } = pollingSetup([
    jobStatus("running", { job_id: "job_1" }),
  ]);

  let updates = 0;
  const subscription = source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: () => {
      updates += 1;
    },
    onSettled: () => assert.fail("must not settle after stop"),
    onError: () => assert.fail("must not error after stop"),
    onTimeout: () => assert.fail("must not time out after stop"),
  });

  await flush();
  assert.equal(updates, 1);

  subscription.stop();
  const calls = transport.calls.length;
  scheduler.advance(10_000);
  await flush();

  assert.equal(transport.calls.length, calls, "no request after stop");
  assert.equal(updates, 1);
  assert.equal(scheduler.pendingCount, 0, "timers were cleaned up");
});

test("a transient network failure is retried rather than abandoning the job", async () => {
  let calls = 0;
  const flaky = ((_url: string) => {
    calls += 1;
    if (calls === 1) return Promise.reject(new TypeError("fetch failed"));
    return Promise.resolve(
      new Response(JSON.stringify(jobStatus("succeeded", { job_id: "job_1" })), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }) as unknown as typeof fetch;

  const scheduler = new ManualScheduler();
  const source = createPollingJobUpdates(
    createApiClient({ baseUrl: API_BASE, fetchImpl: flaky }),
    { intervalMs: 750, timeoutMs: 60_000, scheduler },
  );

  let settled = false;
  source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: () => {},
    onSettled: () => {
      settled = true;
    },
    onError: () => assert.fail("a transient blip must not end the subscription"),
    onTimeout: () => assert.fail("unexpected timeout"),
  });

  await flush();
  scheduler.advance(750);
  await flush();

  assert.equal(settled, true);
});

test("a definitive lookup failure ends the subscription", async () => {
  const transport = new StubTransport().onGet(jobPath("job_1"), {
    status: 404,
    body: { error: { code: "VALIDATION_ERROR", message: "no such job" } },
  });
  const scheduler = new ManualScheduler();
  const source = createPollingJobUpdates(
    createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch }),
    { intervalMs: 750, timeoutMs: 60_000, scheduler },
  );

  const failures: ApiFailure[] = [];
  source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: () => {},
    onSettled: () => assert.fail("must not settle"),
    onError: (error) => failures.push(error),
    onTimeout: () => assert.fail("unexpected timeout"),
  });
  await flush();

  assert.equal(failures.length, 1);
  assert.equal(failures[0]!.kind, "not_found");
  assert.equal(scheduler.pendingCount, 0);
});

test("a job that never terminates times out instead of polling forever", async () => {
  const { scheduler, source } = pollingSetup([jobStatus("running", { job_id: "job_1" })]);

  let timedOut = false;
  source.subscribe(PROJECT_ID, "job_1", {
    onUpdate: () => {},
    onSettled: () => assert.fail("must not settle"),
    onError: () => assert.fail("must not error"),
    onTimeout: () => {
      timedOut = true;
    },
  });

  // Drive well past the configured timeout.
  for (let i = 0; i < 200 && !timedOut; i += 1) {
    scheduler.advance(750);
    await flush(2);
  }

  assert.equal(timedOut, true);
  assert.equal(scheduler.pendingCount, 0);
});
