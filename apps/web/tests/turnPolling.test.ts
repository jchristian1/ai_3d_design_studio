/**
 * Waiting for Astra without holding a request open.
 *
 * The studio used to POST a message and wait for the model on that one request. The
 * browser aborts after 15 seconds and a real turn takes far longer, so every genuine
 * design request failed and the model's work was thrown away. Now the POST returns a turn
 * id and the answer is collected by polling.
 *
 * These tests drive that from the browser's side: the pending reply must say something
 * honest while waiting, the answer must land in the transcript when it arrives, and a
 * dropped poll must not be mistaken for a failure.
 */

import assert from "node:assert/strict";
import { after, before, beforeEach, describe, it } from "node:test";

import globalJsdom from "global-jsdom";

import { API_BASE, StubTransport } from "./support.ts";

const PROJECT = "proj_seed";
const TURN = "turn_abc123";

function workspaceBody() {
  return {
    project: { project_id: PROJECT, display_name: "Beach House Kitchen" },
    references: [],
    facts: [],
    conversation: [],
    clarification: null,
    approvals: [],
    scene: null,
    model: null,
    preview: null,
  };
}

function thinking(requestId = "req_1") {
  return {
    turn_id: TURN,
    state: "thinking",
    request_id: requestId,
    project_id: PROJECT,
    poll_url: `/api/projects/${PROJECT}/design-chat/${TURN}`,
    message: "Astra is thinking…",
  };
}

function ready(message: string, requestId = "req_1") {
  return {
    turn_id: TURN,
    state: "ready",
    kind: "answer",
    message,
    request_id: requestId,
    project_id: PROJECT,
    session_id: "sess_1",
    job_id: null,
    job_status: null,
    worker_id: null,
    duplicate: false,
    operation_count: 0,
    clarification: null,
    approval: null,
    assumptions: [],
    facts_recorded: [],
    provider: "codex_astra",
    status_url: null,
  };
}

function stubs() {
  const transport = new StubTransport();
  transport.onGet(`/api/projects/${PROJECT}/workspace`, {
    status: 200,
    body: workspaceBody(),
  });
  transport.onGet("/api/status/astra", {
    status: 200,
    body: {
      state: "connected",
      label: "Astra via Codex",
      message: "Connected via ChatGPT",
      connected: true,
    },
  });
  transport.onGet("/api/status/blender", {
    status: 200,
    body: { state: "connected", label: "Blender", message: "Connected", connected: true },
  });
  return transport;
}

describe("collecting an answer by polling", () => {
  before(() => {
    globalJsdom(undefined, { pretendToBeVisual: true, url: "http://localhost:3000" });
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });

  after(async () => {
    const { cleanup } = await import("@testing-library/react");
    cleanup();
  });

  beforeEach(async () => {
    const { cleanup } = await import("@testing-library/react");
    cleanup();
  });

  async function renderShell(transport: StubTransport) {
    const { createElement, StrictMode } = await import("react");
    const { render, screen, waitFor, fireEvent } = await import("@testing-library/react");
    const { WorkspaceShell } = await import("../components/WorkspaceShell.tsx");
    const { createWorkspaceClient } = await import("../lib/api/workspace.ts");
    const { createApiClient } = await import("../lib/api/index.ts");

    const client = createWorkspaceClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });
    const jobClient = createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });

    render(
      createElement(
        StrictMode,
        null,
        createElement(WorkspaceShell, {
          sessionOptions: {
            client,
            jobClient,
            projectId: PROJECT,
            poll: false,
            pollIntervalMs: 5,
            turnTimeoutMs: 4000,
          },
        }),
      ),
    );
    return { screen, waitFor, fireEvent };
  }

  async function send(transport: StubTransport, text = "what is in my project?") {
    const { screen, waitFor, fireEvent } = await renderShell(transport);
    await waitFor(() => screen.getByLabelText("Message Astra"));
    fireEvent.change(screen.getByLabelText("Message Astra"), { target: { value: text } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    return { screen, waitFor };
  }

  it("says Astra is thinking, then shows the answer when it arrives", async () => {
    const transport = stubs();
    // The real API echoes the request id it was given, so the stub does too.
    let requestId = "req_1";
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, (call) => {
      requestId = (call.body as { request_id: string }).request_id;
      return { status: 202, body: thinking(requestId) };
    });

    let polls = 0;
    transport.onGet(`/api/projects/${PROJECT}/design-chat/${TURN}`, () => {
      polls += 1;
      return polls < 3
        ? { status: 200, body: thinking(requestId) }
        : { status: 200, body: ready("There is one cube.", requestId) };
    });

    const { screen, waitFor } = await send(transport);

    // While waiting, the reply slot says something true rather than sitting blank.
    await waitFor(() => {
      assert.match(document.body.textContent ?? "", /Astra is thinking/);
    });

    await waitFor(() => {
      const list = screen.getByRole("list", { name: "Messages" });
      assert.match(list.textContent ?? "", /There is one cube\./);
    });

    // One user entry, one reply — the reply was updated in place, not appended to.
    assert.equal(screen.getAllByText("You").length, 1);
    assert.equal(screen.getAllByText("Astra").length, 1);
    assert.ok(polls >= 3, "the answer must be collected by polling");
  });

  it("keeps polling through a dropped poll", async () => {
    const transport = stubs();
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, {
      status: 202,
      body: thinking(),
    });

    let polls = 0;
    transport.on(
      (path, method) =>
        method === "GET" && path === `/api/projects/${PROJECT}/design-chat/${TURN}`,
      () => {
        polls += 1;
        // A connection blip in the middle: the model is still working.
        if (polls === 2) return { status: 502, body: "gateway hiccup" };
        return polls < 4
          ? { status: 200, body: thinking() }
          : { status: 200, body: ready("Two walls and a floor.") };
      },
    );

    const { screen, waitFor } = await send(transport);

    await waitFor(() => {
      const list = screen.getByRole("list", { name: "Messages" });
      assert.match(list.textContent ?? "", /Two walls and a floor\./);
    });
  });

  it("reports a turn that failed, as a sentence", async () => {
    const transport = stubs();
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, {
      status: 202,
      body: thinking(),
    });
    transport.onGet(`/api/projects/${PROJECT}/design-chat/${TURN}`, {
      status: 500,
      body: {
        error: {
          code: "INTERNAL_ERROR",
          message: "Something went wrong while working on that.",
        },
      },
    });

    const { screen, waitFor } = await send(transport);

    await waitFor(() => {
      const list = screen.getByRole("list", { name: "Messages" });
      assert.match(list.textContent ?? "", /[A-Z][a-z]+/);
    });
    const text = document.body.textContent ?? "";
    assert.doesNotMatch(text, /500/);
    assert.doesNotMatch(text, /INTERNAL_ERROR/);
  });

  it("says so when Astra takes longer than the studio will wait", async () => {
    const transport = stubs();
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, {
      status: 202,
      body: thinking(),
    });
    // Never finishes.
    transport.onGet(`/api/projects/${PROJECT}/design-chat/${TURN}`, {
      status: 200,
      body: thinking(),
    });

    const { createElement, StrictMode } = await import("react");
    const { render, screen, waitFor, fireEvent } = await import("@testing-library/react");
    const { WorkspaceShell } = await import("../components/WorkspaceShell.tsx");
    const { createWorkspaceClient } = await import("../lib/api/workspace.ts");
    const { createApiClient } = await import("../lib/api/index.ts");

    const client = createWorkspaceClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });
    const jobClient = createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });
    render(
      createElement(
        StrictMode,
        null,
        createElement(WorkspaceShell, {
          sessionOptions: {
            client,
            jobClient,
            projectId: PROJECT,
            poll: false,
            pollIntervalMs: 5,
            // A deliberately tiny budget, so the giving-up path is exercised in a test
            // rather than only in real life.
            turnTimeoutMs: 40,
          },
        }),
      ),
    );

    await waitFor(() => screen.getByLabelText("Message Astra"));
    fireEvent.change(screen.getByLabelText("Message Astra"), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      assert.match(document.body.textContent ?? "", /taking longer than expected/);
    });
    // And it says the work is not lost, because it is not.
    assert.match(document.body.textContent ?? "", /still being worked on/);
  });

  it("still accepts a turn that was answered immediately", async () => {
    // A refusal, or a cached answer, needs no polling at all.
    const transport = stubs();
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, {
      status: 200,
      body: ready("Astra is not connected yet."),
    });

    const { screen, waitFor } = await send(transport);

    await waitFor(() => {
      const list = screen.getByRole("list", { name: "Messages" });
      assert.match(list.textContent ?? "", /Astra is not connected yet\./);
    });
    assert.equal(
      transport.calls.filter((call) => call.url.includes("/design-chat/")).length,
      0,
      "nothing should have been polled",
    );
  });
});
