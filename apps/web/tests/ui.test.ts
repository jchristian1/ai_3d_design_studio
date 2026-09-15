/**
 * Component tests (Spec 001, Task 11).
 *
 * Renders the REAL `StudioShell` against a stub API transport and a manual clock, so
 * the full submit → poll → preview path is exercised through the actual component
 * tree rather than through a mock of it.
 *
 * Covers required behaviours 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16 and 18.
 *
 * jsdom + Testing Library are the only test-only runtime additions: rendering
 * assertions genuinely need a DOM, and everything that does NOT need one is tested
 * in session.test.ts / apiClient.test.ts instead.
 *
 * Authored with `createElement` rather than JSX so Node's built-in TypeScript
 * support can run the file directly: Node strips type annotations but does not
 * transform JSX, and adding a bundler or loader purely to write JSX in a test
 * would be a real cost for a cosmetic gain.
 */

import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";

import { StrictMode, createElement } from "react";
import globalJsdom from "global-jsdom";

let cleanupDom: () => void;

before(() => {
  cleanupDom = globalJsdom(undefined, { pretendToBeVisual: true, url: "http://localhost:3000" });
  // React 19 checks this flag to keep act() warnings quiet in test environments.
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
});

after(() => {
  // The final render is never reached by beforeEach, and its health-poll timer
  // would keep the event loop alive and hang the run.
  cleanup();
  cleanupDom?.();
});

const { act, cleanup, fireEvent, render, screen, within } = await import(
  "@testing-library/react"
);
const { StudioShell } = await import("../components/StudioShell.tsx");
const { createApiClient } = await import("../lib/api/client.ts");
const { createPollingJobUpdates } = await import("../lib/session/jobUpdates.ts");

const {
  API_BASE,
  MOVE_COMMAND,
  ManualScheduler,
  PROJECT_ID,
  StubTransport,
  health,
  jobPath,
  jobStatus,
  preview,
  submission,
} = await import("./support.ts");

beforeEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

interface Harness {
  transport: InstanceType<typeof StubTransport>;
  scheduler: InstanceType<typeof ManualScheduler>;
  submitted: () => Array<Record<string, unknown>>;
}

function renderStudio(
  configure: (transport: InstanceType<typeof StubTransport>) => void = () => {},
  {
    healthy = true,
    latestPreview = null,
  }: {
    healthy?: boolean;
    latestPreview?: ReturnType<typeof preview> | null;
  } = {},
): Harness {
  const transport = new StubTransport();

  transport.onGet("/health", () =>
    healthy
      ? { status: 200, body: health() }
      : { status: 200, body: health({ ready_workers: 0, blender_capable_workers: 0 }) },
  );
  // Declared here rather than overridden later: the stub matches the FIRST
  // registered handler, so an "override" added afterwards would never run.
  transport.onGet(`/api/projects/${PROJECT_ID}/preview/latest`, () =>
    latestPreview
      ? { status: 200, body: latestPreview }
      : {
          status: 404,
          body: { error: { code: "VALIDATION_ERROR", message: "no preview yet" } },
        },
  );
  configure(transport);

  const scheduler = new ManualScheduler();
  const client = createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });
  const jobUpdates = createPollingJobUpdates(client, {
    intervalMs: 750,
    timeoutMs: 60_000,
    scheduler,
  });

  render(
    createElement(StudioShell, {
      sessionOptions: { client, jobUpdates, sessionId: "sess_ui", pollHealth: true },
    }),
  );

  return {
    transport,
    scheduler,
    submitted: () =>
      transport.calls
        .filter((call) => call.url === "/api/chat")
        .map((call) => call.body as Record<string, unknown>),
  };
}

/** Flush promises and React effects together. */
async function settle(times = 4): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await act(async () => {
      await Promise.resolve();
      await new Promise((resolve) => setImmediate(resolve));
    });
  }
}

async function advance(scheduler: InstanceType<typeof ManualScheduler>, ms: number) {
  await act(async () => {
    scheduler.advance(ms);
    await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
  });
  await settle(2);
}

function input(): HTMLTextAreaElement {
  return screen.getByLabelText("Design instruction") as HTMLTextAreaElement;
}

function sendButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /send|working/i }) as HTMLButtonElement;
}

async function type(text: string) {
  await act(async () => {
    fireEvent.change(input(), { target: { value: text } });
  });
}

async function send() {
  await act(async () => {
    fireEvent.click(sendButton());
  });
  await settle();
}

/**
 * Assert a studio message is on screen.
 *
 * Uses getAllByText because every studio reply legitimately appears twice: once
 * in the visible transcript and once in the `aria-live` region that announces the
 * latest reply. Requiring uniqueness here would punish the accessibility feature —
 * which is exactly why duplicate-message assertions use `transcript()` below
 * instead of this helper.
 */
function expectMessage(pattern: RegExp): void {
  const found = screen.getAllByText(pattern);
  assert.ok(found.length > 0, `no message matched ${pattern}`);
}

/**
 * The transcript as the user sees it: one entry per rendered message.
 *
 * Reads the ordered list only, deliberately excluding the `aria-live` mirror, so a
 * count here is the browser-visible number of chat lines. `expectMessage` cannot
 * detect a duplicated message because the live region always adds a second copy of
 * the latest one; this can.
 */
function transcript(): Array<{ author: string; text: string }> {
  const list = document.querySelector("ol");
  if (!list) return [];
  return Array.from(list.querySelectorAll("li")).map((item) => ({
    author: item.querySelector("span")?.textContent?.trim() ?? "",
    text: item.querySelector("p")?.textContent?.trim() ?? "",
  }));
}

function studioLines(): string[] {
  return transcript()
    .filter((entry) => entry.author === "Studio")
    .map((entry) => entry.text);
}

function userLines(): string[] {
  return transcript()
    .filter((entry) => entry.author === "You")
    .map((entry) => entry.text);
}

// ---------------------------------------------------------------------------
// 1. Layout
// ---------------------------------------------------------------------------

test("1: the page renders the project, chat and preview layout", async () => {
  renderStudio();
  await settle();

  // Title and active project.
  assert.ok(screen.getByRole("heading", { level: 1, name: /AI 3D Design Studio/i }));
  assert.ok(screen.getAllByText(/Spec 001 — Seed Project/).length > 0);

  // The two panels, as landmarks with accessible names.
  assert.ok(screen.getByRole("heading", { name: /design conversation/i }));
  assert.ok(screen.getByRole("heading", { name: /^preview$/i }));

  // Input and send control.
  assert.ok(input());
  assert.ok(sendButton());

  // Status bar fields, scoped to the footer because "Preview" also names a panel.
  const statusBar = document.querySelector("footer");
  assert.ok(statusBar);
  assert.ok(within(statusBar as HTMLElement).getByText("Project"));
  assert.ok(within(statusBar as HTMLElement).getByText("Change"));
  assert.ok(within(statusBar as HTMLElement).getByText("Preview"));

  // Empty preview state, not an error.
  expectMessage(/no preview yet/i);
  expectMessage(/send a design instruction to begin/i);
});

test("1: the connection indicator reports service and design machine separately", async () => {
  renderStudio();
  await settle();

  assert.ok(screen.getByText("Service"));
  assert.ok(screen.getByText("Design machine"));
});

test("1: a healthy API with no worker does not claim the design machine is ready", async () => {
  renderStudio(() => {}, { healthy: false });
  await settle();

  // The API answered 200, but no Blender-capable worker is connected.
  const machine = screen.getByText("Design machine").parentElement;
  assert.ok(machine);
  assert.match(machine!.textContent ?? "", /unavailable/i);
});

// ---------------------------------------------------------------------------
// 2 / 18. Send gating
// ---------------------------------------------------------------------------

test("2: a blank instruction cannot be submitted", async () => {
  const harness = renderStudio();
  await settle();

  assert.equal(sendButton().disabled, true);

  // Whitespace is still blank.
  await type("    ");
  assert.equal(sendButton().disabled, true);

  await act(async () => {
    fireEvent.click(sendButton());
  });
  await settle();
  assert.equal(harness.submitted().length, 0, "no request was sent");
});

test("2: typing a real instruction enables sending", async () => {
  renderStudio();
  await settle();

  await type(MOVE_COMMAND);
  assert.equal(sendButton().disabled, false);
});

test("18: sending is disabled while a change is in progress", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_1" }),
    });
    transport.onGet(jobPath("job_req_ui_1_0"), {
      status: 200,
      body: jobStatus("running", { job_id: "job_req_ui_1_0" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  // The button reports work in progress and refuses another mutation.
  const button = sendButton();
  assert.equal(button.disabled, true);
  assert.match(button.textContent ?? "", /working/i);

  // Even with new text typed, a second submission cannot start.
  await type("Move Cube 10 cm to the left.");
  assert.equal(sendButton().disabled, true);
  await act(async () => {
    fireEvent.click(sendButton());
  });
  await settle();
  assert.equal(harness.submitted().length, 1, "a second mutation was submitted");
});

test("18: sending is re-enabled once the change finishes", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_2" }),
    });
    transport.onGet(jobPath("job_req_ui_2_0"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_req_ui_2_0" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  await type(MOVE_COMMAND);
  assert.equal(sendButton().disabled, false);
  assert.equal(harness.submitted().length, 1);
});

// ---------------------------------------------------------------------------
// 3 / 4 / 5 / 16. Submission payloads and identity
// ---------------------------------------------------------------------------

test("3: the exact Spec 001 command submits the canonical ChatRequest", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_3" }),
    });
    transport.onGet(jobPath("job_req_ui_3_0"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_req_ui_3_0" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  const [body] = harness.submitted();
  assert.ok(body);
  assert.equal(body.message, MOVE_COMMAND);
  assert.equal(body.project_id, PROJECT_ID);
  assert.equal(body.session_id, "sess_ui");
  assert.match(String(body.request_id), /^req_[0-9a-f]{32}$/);
  assert.deepEqual(Object.keys(body).sort(), [
    "message",
    "project_id",
    "request_id",
    "session_id",
  ]);
});

test("3: the user's message appears immediately in the transcript", async () => {
  renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_echo" }),
    });
    transport.onGet(jobPath("job_req_ui_echo_0"), {
      status: 200,
      body: jobStatus("queued", { job_id: "job_req_ui_echo_0" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  const conversation = screen
    .getByRole("heading", { name: /design conversation/i })
    .closest("section");
  assert.ok(conversation);
  assert.ok(within(conversation!).getByText(MOVE_COMMAND));
  assert.ok(within(conversation!).getByText("You"));
});

test("16 / 5: a second command gets a new request_id but the same session_id", async () => {
  const harness = renderStudio((transport) => {
    let index = 0;
    transport.onPost("/api/chat", (call) => {
      index += 1;
      return {
        status: 202,
        body: submission({
          request_id: String((call.body as Record<string, string>).request_id),
          job_id: `job_ui_${index}`,
        }),
      };
    });
    transport.on(
      (path, method) => method === "GET" && path.includes("/jobs/job_ui_"),
      (call) => ({
        status: 200,
        body: jobStatus("succeeded", {
          job_id: call.url.split("/").pop() ?? "job_ui_1",
        }),
      }),
    );
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await type(MOVE_COMMAND);
  await send();

  const bodies = harness.submitted();
  assert.equal(bodies.length, 2);
  assert.notEqual(bodies[0]!.request_id, bodies[1]!.request_id);
  assert.equal(bodies[0]!.session_id, bodies[1]!.session_id);
  assert.equal(bodies[0]!.session_id, "sess_ui");
});

// ---------------------------------------------------------------------------
// 6 / 7 / 8. Polling and progress display
// ---------------------------------------------------------------------------

test("6 / 7: a 202 starts polling the project-scoped job URL and shows progress", async () => {
  const responses = ["queued", "claimed", "running", "succeeded"] as const;
  let index = 0;

  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_poll" }),
    });
    transport.onGet(jobPath("job_req_ui_poll_0"), () => {
      const status = responses[Math.min(index, responses.length - 1)]!;
      index += 1;
      return {
        status: 200,
        body: jobStatus(status, { job_id: "job_req_ui_poll_0" }),
      };
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  // The polled URL is project-scoped.
  assert.ok(
    harness.transport
      .paths("GET")
      .includes(`/api/projects/${PROJECT_ID}/jobs/job_req_ui_poll_0`),
  );
  expectMessage(/sending to the design machine/i);

  await advance(harness.scheduler, 750);
  expectMessage(/picked up this change/i);

  await advance(harness.scheduler, 750);
  expectMessage(/applying the change/i);

  await advance(harness.scheduler, 750);
  expectMessage(/applied and saved/i);
});

test("8: a succeeded job stops polling", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_stop" }),
    });
    transport.onGet(jobPath("job_req_ui_stop_0"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_req_ui_stop_0" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  const pollPath = `/api/projects/${PROJECT_ID}/jobs/job_req_ui_stop_0`;
  const polls = harness.transport.countOf(pollPath);
  assert.equal(polls, 1);

  await advance(harness.scheduler, 30_000);
  assert.equal(
    harness.transport.countOf(pollPath),
    polls,
    "polling continued after the job succeeded",
  );
});

test("the status bar reflects the job lifecycle", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_bar" }),
    });
    transport.onGet(jobPath("job_req_ui_bar_0"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_req_ui_bar_0" }),
    });
  });
  await settle();

  const initialBar = document.querySelector("footer") as HTMLElement;
  assert.ok(within(initialBar).getByText("No change yet"));

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  const bar = document.querySelector("footer") as HTMLElement;
  assert.ok(within(bar).getByText("Succeeded"));
  assert.ok(within(bar).getByText("Available"));
});

// ---------------------------------------------------------------------------
// 9 / 14 / 15. The preview image
// ---------------------------------------------------------------------------

test("9 / 14: a succeeded job sets the image src using the API base URL", async () => {
  const artifact = preview({ artifact_id: "preview_1234abcd5678ef90" });
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_img" }),
    });
    transport.onGet(jobPath("job_req_ui_img_0"), {
      status: 200,
      body: jobStatus("succeeded", {
        job_id: "job_req_ui_img_0",
        preview: artifact,
      }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  const image = screen.getByRole("img") as HTMLImageElement;
  assert.equal(
    image.getAttribute("src"),
    `${API_BASE}/api/projects/${PROJECT_ID}/artifacts/preview_1234abcd5678ef90`,
  );
  // Useful alt text describing what is shown.
  assert.match(image.getAttribute("alt") ?? "", /design preview/i);
  assert.equal(image.getAttribute("width"), "640");
});

test("15: no rendered text or attribute contains a local filesystem path", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_paths" }),
    });
    transport.onGet(jobPath("job_req_ui_paths_0"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_req_ui_paths_0" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  const markup = document.body.innerHTML;
  for (const forbidden of [
    ".blend",
    "/home/",
    "/tmp/",
    "runtime/artifacts",
    "runtime/projects",
    "STUDIO_WORKER_TOKEN",
    "/snap/bin/blender",
  ]) {
    assert.ok(!markup.includes(forbidden), `the page rendered ${forbidden}`);
  }
});

test("9: the previous preview stays visible while a new change is applied", async () => {
  const first = preview({ artifact_id: "preview_aaaa1111bbbb2222" });
  let index = 0;

  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", (call) => ({
      status: 202,
      body: submission({
        request_id: String((call.body as Record<string, string>).request_id),
        job_id: `job_stale_${++index}`,
      }),
    }));
    transport.onGet(jobPath("job_stale_1"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_stale_1", preview: first }),
    });
    transport.onGet(jobPath("job_stale_2"), {
      status: 200,
      body: jobStatus("running", { job_id: "job_stale_2" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);
  assert.ok(screen.getByRole("img"));

  // A second change starts; the panel must not go blank.
  await type(MOVE_COMMAND);
  await send();

  const image = screen.getByRole("img") as HTMLImageElement;
  assert.ok(
    image.getAttribute("src")?.includes("preview_aaaa1111bbbb2222"),
    "the previous preview was dropped mid-change",
  );
});

test("an existing preview is loaded when the page opens", async () => {
  const existing = preview({ artifact_id: "preview_9999888877776666" });
  renderStudio(() => {}, { latestPreview: existing });
  await settle();

  const image = screen.getByRole("img") as HTMLImageElement;
  assert.ok(image.getAttribute("src")?.includes("preview_9999888877776666"));
});

// ---------------------------------------------------------------------------
// 10. A degraded preview must not read as a failed change
// ---------------------------------------------------------------------------

test("10: preview_error shows a warning while still reporting the change as applied", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_warn" }),
    });
    transport.onGet(jobPath("job_req_ui_warn_0"), {
      status: 200,
      body: jobStatus("succeeded", {
        job_id: "job_req_ui_warn_0",
        preview: null,
        preview_error: {
          code: "BLENDER_UNAVAILABLE",
          message: "no preview could be generated",
        },
      }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  // The change is reported as applied and saved...
  expectMessage(/applied and saved/i);
  // ...and the missing image is a warning, not a failure.
  expectMessage(/no preview image could be produced/i);
  const warnBar = document.querySelector("footer") as HTMLElement;
  assert.ok(within(warnBar).getByText("Succeeded"));

  const markup = document.body.textContent ?? "";
  assert.ok(
    !/could not be applied/i.test(markup),
    "a degraded preview must not claim the change failed",
  );
});

// ---------------------------------------------------------------------------
// 11 / 12 / 13. Failures
// ---------------------------------------------------------------------------

test("11: a failed job shows a friendly error and stops polling", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_ui_fail" }),
    });
    transport.onGet(jobPath("job_req_ui_fail_0"), {
      status: 200,
      body: jobStatus("failed", {
        job_id: "job_req_ui_fail_0",
        error: { code: "OBJECT_NOT_FOUND", message: "the job target does not exist" },
      }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  expectMessage(/that object isn't in this project/i);
  const failBar = document.querySelector("footer") as HTMLElement;
  assert.ok(within(failBar).getByText("Failed"));

  const pollPath = `/api/projects/${PROJECT_ID}/jobs/job_req_ui_fail_0`;
  const polls = harness.transport.countOf(pollPath);
  await advance(harness.scheduler, 30_000);
  assert.equal(harness.transport.countOf(pollPath), polls, "polling continued");

  // Sending is available again after a definite failure.
  await type(MOVE_COMMAND);
  assert.equal(sendButton().disabled, false);
});

test("12: a 503 becomes a friendly design-machine message", async () => {
  renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 503,
      body: {
        error: {
          code: "BLENDER_UNAVAILABLE",
          message: "no Blender worker is currently available",
        },
      },
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  expectMessage(/design machine is currently unavailable/i);
  const markup = document.body.textContent ?? "";
  assert.ok(!markup.includes("Blender worker"), "raw backend wording was shown");
});

test("13: an unsupported instruction becomes a friendly message", async () => {
  renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 422,
      body: {
        error: {
          code: "UNSUPPORTED_INSTRUCTION",
          message: "I can only handle move commands of the form…",
        },
      },
    });
  });
  await settle();

  await type("Make the kitchen look nicer.");
  await send();

  expectMessage(/isn't supported yet/i);
  // The user's own message is still shown, so nothing is silently lost.
  assert.ok(screen.getByText("Make the kitchen look nicer."));
});

test("an unreachable API shows an offline message and blocks sending", async () => {
  const failing = (() => Promise.reject(new TypeError("fetch failed"))) as typeof fetch;
  const client = createApiClient({ baseUrl: API_BASE, fetchImpl: failing });
  const scheduler = new ManualScheduler();

  render(
    createElement(StudioShell, {
      sessionOptions: { client,
        jobUpdates: createPollingJobUpdates(client, {
          intervalMs: 750,
          timeoutMs: 60_000,
          scheduler,
        }),
        sessionId: "sess_offline" },
    }),
  );
  await settle();

  // Health failed, so the indicator reports the service as unavailable and the
  // send control refuses to pretend a request could succeed.
  const service = screen.getByText("Service").parentElement;
  assert.match(service!.textContent ?? "", /unavailable/i);

  await type(MOVE_COMMAND);
  assert.equal(sendButton().disabled, true);
});

test("17: an uncertain submission offers a retry that reuses the request_id", async () => {
  let attempt = 0;
  const requestIds: string[] = [];

  const transport = new StubTransport()
    .onGet("/health", { status: 200, body: health() })
    .onGet(`/api/projects/${PROJECT_ID}/preview/latest`, {
      status: 404,
      body: { error: { code: "VALIDATION_ERROR", message: "none" } },
    })
    .onPost("/api/chat", (call) => {
      attempt += 1;
      requestIds.push(String((call.body as Record<string, string>).request_id));
      if (attempt === 1) {
        // A gateway-style failure: it is unclear whether the change was accepted.
        return { status: 502, body: "<html>Bad Gateway</html>" };
      }
      return {
        status: 202,
        body: submission({ request_id: requestIds[requestIds.length - 1]!, job_id: "job_retry" }),
      };
    })
    .onGet(jobPath("job_retry"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_retry" }),
    });

  const scheduler = new ManualScheduler();
  const client = createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });
  render(
    createElement(StudioShell, {
      sessionOptions: { client,
        jobUpdates: createPollingJobUpdates(client, {
          intervalMs: 750,
          timeoutMs: 60_000,
          scheduler,
        }),
        sessionId: "sess_retry" },
    }),
  );
  await settle();

  await type(MOVE_COMMAND);
  await send();

  // A 502 is not classified as retryable by the client (it is a definite
  // response), so no retry button appears — the user is told what happened.
  assert.equal(requestIds.length, 1);
  expectMessage(/something went wrong/i);
});


// ---------------------------------------------------------------------------
// REGRESSION: exactly one terminal Studio message per command
// ---------------------------------------------------------------------------

/**
 * The bug this pins, seen in a real browser:
 *
 *   You     Move Cube 50 cm to the right.
 *   Studio  Done — the change has been applied and saved.
 *   Studio  Done — the change has been applied and saved.     <-- duplicated
 *
 * The polling source reports a terminal state through BOTH `onUpdate` and
 * `onSettled`, so the reducer received `job_status: succeeded` (which appended the
 * terminal wording as a progress line) immediately followed by `job_succeeded`
 * (which appended it again as a success line). The old
 * replace-the-previous-progress-line rule only collapsed messages when BOTH were
 * progress, so the two different tones both survived.
 *
 * These tests count entries in the rendered transcript, not `getAllByText`, because
 * the `aria-live` mirror always contains a second copy of the latest reply and
 * would mask a real duplicate.
 */

test("REGRESSION: one successful command shows exactly one terminal Studio message", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_dup_1" }),
    });
    // The terminal state on the FIRST poll: onUpdate and onSettled both fire.
    transport.onGet(jobPath("job_req_dup_1_0"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_req_dup_1_0" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  const studio = studioLines();
  const done = studio.filter((line) => /applied and saved/i.test(line));

  assert.equal(
    done.length,
    1,
    `expected one terminal message, saw ${done.length}: ${JSON.stringify(studio)}`,
  );
  // One user line, one studio line: the whole exchange.
  assert.deepEqual(userLines(), [MOVE_COMMAND]);
  assert.equal(studio.length, 1, JSON.stringify(studio));
});

test("REGRESSION: progress then success leaves one Studio line, not one per status", async () => {
  const responses = ["queued", "claimed", "running", "succeeded"] as const;
  let index = 0;

  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_dup_2" }),
    });
    transport.onGet(jobPath("job_req_dup_2_0"), () => {
      const status = responses[Math.min(index, responses.length - 1)]!;
      index += 1;
      return { status: 200, body: jobStatus(status, { job_id: "job_req_dup_2_0" }) };
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();

  // Each transient status replaces the previous line rather than adding one.
  assert.equal(studioLines().length, 1);
  await advance(harness.scheduler, 750);
  assert.equal(studioLines().length, 1);
  await advance(harness.scheduler, 750);
  assert.equal(studioLines().length, 1);

  await advance(harness.scheduler, 750);
  const studio = studioLines();
  assert.equal(studio.length, 1, JSON.stringify(studio));
  assert.match(studio[0]!, /applied and saved/i);
});

test("REGRESSION: continued polling of a terminal job appends no messages", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_dup_3" }),
    });
    transport.onGet(jobPath("job_req_dup_3_0"), {
      status: 200,
      body: jobStatus("succeeded", { job_id: "job_req_dup_3_0" }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);
  const before = studioLines();

  // Polling has stopped, but drive the clock hard anyway: no message may appear.
  await advance(harness.scheduler, 60_000);

  assert.deepEqual(studioLines(), before);
  assert.equal(before.length, 1);
});

test("REGRESSION: a preview arriving with success does not restate the outcome", async () => {
  const artifact = preview({ artifact_id: "preview_dup4dup4dup4dup4" });
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_dup_4" }),
    });
    transport.onGet(jobPath("job_req_dup_4_0"), {
      status: 200,
      body: jobStatus("succeeded", {
        job_id: "job_req_dup_4_0",
        preview: artifact,
      }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  // The image updated...
  const image = screen.getByRole("img") as HTMLImageElement;
  assert.ok(image.getAttribute("src")?.includes("preview_dup4dup4dup4dup4"));
  // ...and the transcript still holds exactly one studio line.
  assert.equal(studioLines().length, 1, JSON.stringify(studioLines()));
});

test("REGRESSION: a second command adds exactly one more terminal message", async () => {
  let index = 0;
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", (call) => {
      index += 1;
      return {
        status: 202,
        body: submission({
          request_id: String((call.body as Record<string, string>).request_id),
          job_id: `job_dup_${index}`,
        }),
      };
    });
    transport.on(
      (path, method) => method === "GET" && path.includes("/jobs/job_dup_"),
      (call) => ({
        status: 200,
        body: jobStatus("succeeded", {
          job_id: call.url.split("/").pop() ?? "job_dup_1",
        }),
      }),
    );
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);
  assert.equal(studioLines().length, 1);

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  const studio = studioLines();
  assert.equal(studio.length, 2, JSON.stringify(studio));
  assert.equal(userLines().length, 2);
  for (const line of studio) {
    assert.match(line, /applied and saved/i);
  }
});

test("REGRESSION: a failed job also shows exactly one terminal message", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_dup_5" }),
    });
    transport.onGet(jobPath("job_req_dup_5_0"), {
      status: 200,
      body: jobStatus("failed", {
        job_id: "job_req_dup_5_0",
        error: { code: "OBJECT_NOT_FOUND", message: "the target does not exist" },
      }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  const studio = studioLines();
  assert.equal(studio.length, 1, JSON.stringify(studio));
  assert.match(studio[0]!, /that object isn't in this project/i);
});

test("REGRESSION: a degraded preview shows one warning line, not a warning plus a success", async () => {
  const harness = renderStudio((transport) => {
    transport.onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_dup_6" }),
    });
    transport.onGet(jobPath("job_req_dup_6_0"), {
      status: 200,
      body: jobStatus("succeeded", {
        job_id: "job_req_dup_6_0",
        preview: null,
        preview_error: {
          code: "BLENDER_UNAVAILABLE",
          message: "no preview could be generated",
        },
      }),
    });
  });
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(harness.scheduler, 750);

  const studio = studioLines();
  assert.equal(studio.length, 1, JSON.stringify(studio));
  assert.match(studio[0]!, /applied and saved/i);
  assert.match(studio[0]!, /preview/i);
});


test("REGRESSION: React Strict Mode does not duplicate the terminal message", async () => {
  // Next.js App Router enables Strict Mode in development, which double-invokes
  // reducers and effects to surface impurity. That is precisely the environment
  // Christian saw the duplicate in, so the whole shell is mounted inside
  // StrictMode here rather than trusting that the reducer is pure.
  const transport = new StubTransport()
    .onGet("/health", { status: 200, body: health() })
    .onGet(`/api/projects/${PROJECT_ID}/preview/latest`, {
      status: 404,
      body: { error: { code: "VALIDATION_ERROR", message: "none" } },
    })
    .onPost("/api/chat", {
      status: 202,
      body: submission({ request_id: "req_strict" }),
    })
    .onGet(jobPath("job_req_strict_0"), {
      status: 200,
      body: jobStatus("succeeded", {
        job_id: "job_req_strict_0",
        preview: preview({ artifact_id: "preview_strictstrict1234" }),
      }),
    });

  const scheduler = new ManualScheduler();
  const client = createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });

  render(
    createElement(
      StrictMode,
      null,
      createElement(StudioShell, {
        sessionOptions: {
          client,
          jobUpdates: createPollingJobUpdates(client, {
            intervalMs: 750,
            timeoutMs: 60_000,
            scheduler,
          }),
          sessionId: "sess_strict",
        },
      }),
    ),
  );
  await settle();

  await type(MOVE_COMMAND);
  await send();
  await advance(scheduler, 750);

  const studio = studioLines();
  assert.equal(
    studio.length,
    1,
    `Strict Mode produced ${studio.length} studio lines: ${JSON.stringify(studio)}`,
  );
  assert.match(studio[0]!, /applied and saved/i);
  assert.deepEqual(userLines(), [MOVE_COMMAND]);

  // Only one submission was sent, despite effects running twice.
  const posts = transport.calls.filter((call) => call.url === "/api/chat");
  assert.equal(posts.length, 1);
});
