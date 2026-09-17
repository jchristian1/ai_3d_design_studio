/**
 * The workspace rendered in jsdom.
 *
 * Queries are accessibility-first, so a control without an accessible name fails here.
 * The 3D viewer degrades to its honest fallback because jsdom has no WebGL — the same
 * path a real browser without WebGL takes, so this is real behaviour rather than a
 * test-only branch.
 */

import assert from "node:assert/strict";
import { after, before, beforeEach, describe, it } from "node:test";

import globalJsdom from "global-jsdom";

import { API_BASE, StubTransport } from "./support.ts";

const PROJECT = "proj_seed";

function turn(overrides: Record<string, unknown> = {}) {
  return {
    kind: "answer",
    message: "There is nothing in the scene yet.",
    request_id: "req_1",
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
    provider: "fake_llm",
    status_url: null,
    ...overrides,
  };
}

function workspaceBody(overrides: Record<string, unknown> = {}) {
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
    ...overrides,
  };
}

const APPROVAL = {
  approval_id: "apr_1",
  code: "import shutil\nshutil.rmtree('/home/christian')\n",
  summary: "This step imports shutil.",
  reasons: ["line 1: imports shutil, which is outside ordinary Blender scene work"],
  decision: "pending",
  created_at: "2026-01-01T00:00:00Z",
  decided_at: null,
  operation_count: 1,
};

const CONNECTED_ASTRA = {
  state: "connected",
  label: "Astra via Codex",
  message: "Connected via ChatGPT",
  connected: true,
  model: "gpt-6-astra",
};

/**
 * Build a stub transport. `astra` is a parameter rather than something a test adds
 * afterwards because the stub matches the FIRST registered handler, so a later
 * registration for the same path would be silently shadowed.
 */
function stubs(
  overrides: Record<string, unknown> = {},
  astra: Record<string, unknown> = CONNECTED_ASTRA,
) {
  const transport = new StubTransport();
  transport.onGet(`/api/projects/${PROJECT}/workspace`, {
    status: 200,
    body: workspaceBody(overrides),
  });
  transport.onGet("/api/status/astra", { status: 200, body: astra });
  transport.onGet("/api/status/blender", {
    status: 200,
    body: { state: "connected", label: "Blender", message: "Connected", connected: true },
  });
  return transport;
}

describe("the workspace", () => {
  let cleanupDom: () => void;

  before(() => {
    cleanupDom = globalJsdom(undefined, { pretendToBeVisual: true, url: "http://localhost:3000" });
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });

  after(async () => {
    const { cleanup } = await import("@testing-library/react");
    cleanup();
    // Unmounting queues work on React's scheduler, which runs on a setImmediate. Let it
    // drain BEFORE the window goes away: react-dom dereferences `window` when that work
    // runs, so tearing jsdom down first surfaces an uncaught "window is not defined"
    // blamed on whichever test happened to be active.
    await new Promise((resolve) => setImmediate(resolve));
    // Tear down the jsdom window/document installed on the global. Without this
    // the whole environment leaks for the life of the process, which is what
    // let a full test run accumulate enough heap to be OOM-killed.
    cleanupDom?.();
  });

  beforeEach(async () => {
    const { cleanup } = await import("@testing-library/react");
    cleanup();
  });

  async function renderShell(transport: StubTransport) {
    const { createElement, StrictMode } = await import("react");
    const { render, screen, waitFor } = await import("@testing-library/react");
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
          sessionOptions: { client, jobClient, projectId: PROJECT, poll: false },
        }),
      ),
    );
    return { screen, waitFor };
  }

  it("shows the project, both connection states and the model status", async () => {
    const { screen, waitFor } = await renderShell(stubs());

    await waitFor(() => {
      assert.ok(screen.getByRole("heading", { level: 1, name: "Beach House Kitchen" }));
    });
    assert.ok(screen.getByText("Astra via Codex"));
    assert.ok(screen.getByText("Blender"));
    assert.ok(screen.getByText("No model yet"));
  });

  it("anchors a composer with an accessible name and a hint", async () => {
    const { screen, waitFor } = await renderShell(stubs());
    await waitFor(() => screen.getByLabelText("Message Astra"));

    const input = screen.getByLabelText("Message Astra");
    assert.equal(input.tagName, "TEXTAREA");
    assert.ok(screen.getByRole("button", { name: "Send" }));
    assert.ok(screen.getByRole("button", { name: "Attach references" }));
    assert.match(document.body.textContent ?? "", /Shift\+Enter/);
  });

  it("puts the conversation in a left column with files above it", async () => {
    const { screen, waitFor } = await renderShell(stubs());
    await waitFor(() => screen.getByRole("heading", { name: "Files" }));

    // The three columns: chat, model, inspector. Chat and inspector both collapse.
    const chatToggle = screen.getByRole("button", { name: "Chat" });
    assert.equal(chatToggle.getAttribute("aria-pressed"), "true");
    const inspectorToggle = screen.getByRole("button", { name: "Inspector" });
    assert.equal(inspectorToggle.getAttribute("aria-pressed"), "true");
    assert.ok(screen.getByRole("heading", { name: "Inspector" }));

    // The composer sits at the bottom of the chat column, after the transcript.
    const column = screen.getByRole("region", { name: "Conversation" });
    const transcript = screen.getByRole("list", { name: "Messages" });
    const composer = screen.getByLabelText("Message Astra");
    assert.ok(column.contains(transcript), "the transcript belongs to the chat column");
    assert.ok(column.contains(composer), "the composer belongs to the chat column");
    assert.ok(
      transcript.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING,
      "the composer must come after the transcript",
    );
  });

  it("collapsing the chat leaves the model on screen", async () => {
    const { screen, waitFor } = await renderShell(stubs());
    const { fireEvent } = await import("@testing-library/react");
    await waitFor(() => screen.getByRole("button", { name: "Chat" }));

    fireEvent.click(screen.getByRole("button", { name: "Chat" }));

    assert.equal(screen.getByRole("button", { name: "Chat" }).getAttribute("aria-pressed"), "false");
    assert.ok(screen.queryByLabelText("Message Astra") === null);
    assert.ok(screen.getByRole("heading", { name: "Inspector" }));
  });

  it("falls back honestly when the 3D view cannot run", async () => {
    const { waitFor } = await renderShell(stubs());
    // jsdom has no WebGL, so the viewer reports that rather than showing a blank box.
    await waitFor(() => {
      const text = document.body.textContent ?? "";
      assert.ok(
        /No model yet|Interactive 3D is unavailable/.test(text),
        `expected a viewer fallback, got: ${text.slice(0, 200)}`,
      );
    });
  });

  it("sends a message and shows one user entry and one reply", async () => {
    const transport = stubs();
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, {
      status: 200,
      body: turn({ message: "There is nothing in the scene yet." }),
    });

    const { screen, waitFor } = await renderShell(transport);
    const { fireEvent } = await import("@testing-library/react");
    await waitFor(() => screen.getByLabelText("Message Astra"));

    const input = screen.getByLabelText("Message Astra");
    fireEvent.change(input, { target: { value: "what is in my project?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    // The reply text appears twice on purpose: once visibly, and once inside the
    // aria-live region that announces only the latest line. Scope to the transcript.
    await waitFor(() => {
      const list = screen.getByRole("list", { name: "Messages" });
      assert.match(list.textContent ?? "", /There is nothing in the scene yet\./);
    });
    const list = screen.getByRole("list", { name: "Messages" });
    assert.match(list.textContent ?? "", /what is in my project\?/);
    assert.equal(screen.getAllByText("You").length, 1);
    assert.equal(screen.getAllByText("Astra").length, 1);
  });

  it("renders an approval card with the actual code and both decisions", async () => {
    const transport = stubs({ approvals: [APPROVAL] });
    const { screen, waitFor } = await renderShell(transport);

    await waitFor(() => screen.getByText("This step needs your approval"));
    assert.match(document.body.textContent ?? "", /shutil\.rmtree/);
    assert.match(document.body.textContent ?? "", /imports shutil/);
    assert.ok(screen.getByRole("button", { name: "Approve and run" }));
    assert.ok(screen.getByRole("button", { name: "Reject" }));
    assert.match(document.body.textContent ?? "", /Nothing runs until you decide/);
  });

  it("shows the inspector properties of a selected object", async () => {
    const scene = {
      project_id: PROJECT,
      scene_version: "sha256:a",
      captured_at: "2026-01-01T00:00:00Z",
      units: { unit_system: "METRIC", length_unit: "m", scale_length: 1 },
      objects: [
        {
          studio_object_id: "obj_wall_north",
          name: "Wall_North",
          object_type: "MESH",
          world_position_meters: { x: 2, y: 0, z: 1.2 },
          dimensions_meters: { x: 4, y: 0.12, z: 2.4 },
          rotation_euler_radians: { x: 0, y: 0, z: 0 },
          scale: { x: 1, y: 1, z: 1 },
          visible: true,
          material: null,
        },
      ],
    };
    const transport = stubs({ scene });
    const { waitFor } = await renderShell(transport);
    const { screen } = await import("@testing-library/react");
    await waitFor(() => screen.getByRole("heading", { name: "Inspector" }));

    // Nothing selected yet: the inspector invites a click rather than showing nothing.
    assert.match(document.body.textContent ?? "", /Click something in the 3D view/);
  });

  it("shows project facts and offers to change them", async () => {
    const transport = stubs({
      facts: [
        {
          key: "ceiling_height_m",
          value: "2.4",
          source: "user",
          updated_at: "2026-01-01T00:00:00Z",
        },
      ],
    });
    const { screen, waitFor } = await renderShell(transport);
    await waitFor(() => screen.getByText("Ceiling height"));
    assert.ok(screen.getByText("2.4"));
    assert.ok(screen.getByRole("button", { name: "Change Ceiling height" }));
  });

  it("restores a conversation on load", async () => {
    const transport = stubs({
      conversation: [
        { role: "user", text: "reconstruct this plan", created_at: "2026-01-01T00:00:00Z" },
        {
          role: "assistant",
          text: "What is the ceiling height?",
          created_at: "2026-01-01T00:00:01Z",
        },
      ],
    });
    const { screen, waitFor } = await renderShell(transport);
    await waitFor(() => {
      assert.match(screen.getByRole("list", { name: "Messages" }).textContent ?? "", /reconstruct this plan/);
    });
    assert.match(
      screen.getByRole("list", { name: "Messages" }).textContent ?? "",
      /What is the ceiling height\?/,
    );
  });

  it("offers a ChatGPT sign-in when Astra needs a login", async () => {
    const transport = stubs({}, {
      state: "login_required",
      label: "Astra via Codex",
      message: "Sign in to ChatGPT to connect Astra. Run: codex login",
      connected: false,
      action: "codex login",
    });

    const { screen, waitFor } = await renderShell(transport);
    await waitFor(() => screen.getByRole("heading", { name: "Connect Astra" }));

    assert.ok(screen.getByRole("button", { name: "Sign in with ChatGPT" }));
    assert.ok(screen.getByRole("button", { name: "Use a device code instead" }));
    // The exact command is still shown for anyone who prefers a terminal.
    assert.match(document.body.textContent ?? "", /codex login/);

    // The property that matters is that there is nowhere to TYPE a secret. The panel
    // does mention passwords and API keys, to say it never uses them.
    assert.ok(document.querySelector('input[type="password"]') === null);
    const secretish = Array.from(document.querySelectorAll("input")).filter((input) => {
      const descriptor = `${input.getAttribute("name") ?? ""} ${input.getAttribute("id") ?? ""} ${
        input.getAttribute("placeholder") ?? ""
      }`.toLowerCase();
      return /password|api[-_ ]?key|secret|token/.test(descriptor);
    });
    assert.deepEqual(secretish, [], "there must be nowhere to type a secret");
  });

  it("shows the verification link and one-time code from the device flow", async () => {
    const transport = stubs({}, {
      state: "login_required",
      label: "Astra via Codex",
      message: "Sign in to ChatGPT to connect Astra.",
      connected: false,
      action: "codex login",
    });
    transport.onPost("/api/status/astra/login", {
      status: 200,
      body: {
        supported: true,
        state: "waiting",
        verification_url: "https://auth.openai.com/codex/device",
        user_code: "FIZI-U80Z5",
        device_auth: true,
        waiting: true,
      },
    });
    transport.onGet("/api/status/astra/login", {
      status: 200,
      body: {
        supported: true,
        state: "waiting",
        verification_url: "https://auth.openai.com/codex/device",
        user_code: "FIZI-U80Z5",
        device_auth: true,
        waiting: true,
      },
    });

    const { screen, waitFor } = await renderShell(transport);
    const { fireEvent } = await import("@testing-library/react");
    await waitFor(() => screen.getByRole("button", { name: "Use a device code instead" }));

    fireEvent.click(screen.getByRole("button", { name: "Use a device code instead" }));

    await waitFor(() => {
      assert.match(document.body.textContent ?? "", /FIZI-U80Z5/);
    });
    const link = screen.getByRole("link", { name: /auth\.openai\.com/ });
    assert.equal(link.getAttribute("href"), "https://auth.openai.com/codex/device");
    assert.equal(link.getAttribute("rel"), "noreferrer noopener");
    assert.match(document.body.textContent ?? "", /Waiting for you to authorise/);
  });

  it("hides the connect panel once Astra is connected", async () => {
    const { screen, waitFor } = await renderShell(stubs());
    await waitFor(() => screen.getByText("Astra via Codex"));
    assert.ok(screen.queryByRole("heading", { name: "Connect Astra" }) === null);
  });

  it("never renders a job id, path, or capability name", async () => {
    const transport = stubs({
      conversation: [{ role: "assistant", text: "Done — 3 changes applied.", created_at: "x" }],
    });
    const { screen, waitFor } = await renderShell(transport);
    await waitFor(() => {
      assert.match(screen.getByRole("list", { name: "Messages" }).textContent ?? "", /3 changes applied/);
    });

    const text = document.body.textContent ?? "";
    assert.doesNotMatch(text, /job_/);
    assert.doesNotMatch(text, /\/home\//);
    assert.doesNotMatch(text, /execute_blender_code/);
    assert.doesNotMatch(text, /apply_capabilities/);
    assert.doesNotMatch(text, /create_wall/);
  });
});
