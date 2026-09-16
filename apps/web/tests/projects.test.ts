/**
 * Opening the studio: which project do you get, and how do you get another one.
 *
 * The promise being tested is small but load-bearing: opening the app should feel like
 * reopening a document. If nothing has been opened yet you get a screen for starting
 * something, not an empty workspace pointed at a project you have never heard of.
 */

import assert from "node:assert/strict";
import { after, before, beforeEach, describe, it } from "node:test";

import globalJsdom from "global-jsdom";

import { API_BASE, StubTransport } from "./support.ts";

const SEED = "proj_seed";
const OTHER = "proj_a1b2c3d4";

function project(overrides: Record<string, unknown> = {}) {
  return {
    project_id: SEED,
    display_name: "Beach House Kitchen",
    created_at: "2026-09-01T10:00:00Z",
    updated_at: "2026-09-01T10:00:00Z",
    last_opened_at: null,
    blend_ready: true,
    latest_scene_version: null,
    reference_count: 0,
    message_count: 0,
    has_model: false,
    ...overrides,
  };
}

function workspaceBody(overrides: Record<string, unknown> = {}) {
  return {
    project: { project_id: SEED, display_name: "Beach House Kitchen" },
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

/** A transport with the status endpoints and any project list a test needs. */
function stubs(listing: Record<string, unknown>) {
  const transport = new StubTransport();
  transport.onGet("/api/projects", { status: 200, body: listing });
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
  transport.onGet(`/api/projects/${SEED}/workspace`, {
    status: 200,
    body: workspaceBody(),
  });
  transport.onGet(`/api/projects/${OTHER}/workspace`, {
    status: 200,
    body: workspaceBody({
      project: { project_id: OTHER, display_name: "Loft Conversion" },
    }),
  });
  return transport;
}

/** localStorage that a test controls, so "what did the browser remember" is explicit. */
function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    removeItem: (key: string) => void values.delete(key),
    values,
  };
}

describe("opening the studio", () => {
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

  async function renderApp(
    transport: StubTransport,
    storage: ReturnType<typeof memoryStorage> = memoryStorage(),
  ) {
    const { createElement, StrictMode } = await import("react");
    const { render, screen, waitFor, fireEvent } = await import("@testing-library/react");
    const { StudioApp } = await import("../components/StudioApp.tsx");
    const { createWorkspaceClient } = await import("../lib/api/workspace.ts");
    const { createApiClient } = await import("../lib/api/index.ts");

    const client = createWorkspaceClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });
    const jobClient = createApiClient({ baseUrl: API_BASE, fetchImpl: transport.fetch });

    render(
      createElement(
        StrictMode,
        null,
        createElement(StudioApp, {
          client,
          projectOptions: { client, storage },
          sessionOptions: { client, jobClient, poll: false },
        }),
      ),
    );
    return { screen, waitFor, fireEvent, storage };
  }

  // --- nothing opened yet -------------------------------------------------

  it("shows the project screen when nothing has been opened", async () => {
    const transport = stubs({ projects: [project()], last_opened_project_id: null });
    const { screen, waitFor } = await renderApp(transport);

    await waitFor(() => screen.getByRole("heading", { name: "What are we designing?" }));

    // The existing project is offered rather than opened.
    assert.ok(screen.getByRole("button", { name: /Beach House Kitchen/ }));
    assert.ok(screen.getByRole("button", { name: "Create project" }));
    // And no workspace is mounted, so nothing was loaded behind the screen.
    assert.equal(screen.queryByLabelText("Message Astra"), null);
  });

  it("invites a first project when there are none", async () => {
    const transport = stubs({ projects: [], last_opened_project_id: null });
    const { screen, waitFor } = await renderApp(transport);

    await waitFor(() => screen.getByRole("heading", { name: "Your projects" }));
    assert.match(document.body.textContent ?? "", /Nothing yet/);
  });

  it("shows whether Astra and Blender are ready before a project is opened", async () => {
    const transport = stubs({ projects: [], last_opened_project_id: null });
    const { screen, waitFor } = await renderApp(transport);

    await waitFor(() => screen.getByText("Astra via Codex"));
    assert.ok(screen.getByText("Blender"));
  });

  // --- resuming -----------------------------------------------------------

  it("reopens the project the server says was last opened", async () => {
    const transport = stubs({
      projects: [project({ last_opened_at: "2026-09-15T09:00:00Z" })],
      last_opened_project_id: SEED,
    });
    const { screen, waitFor } = await renderApp(transport);

    await waitFor(() => screen.getByLabelText("Message Astra"));
    assert.ok(screen.getByRole("button", { name: /Beach House Kitchen/ }));
    assert.equal(screen.queryByRole("heading", { name: "What are we designing?" }), null);
  });

  it("prefers what this browser remembers", async () => {
    const transport = stubs({
      projects: [
        project({ last_opened_at: "2026-09-15T09:00:00Z" }),
        project({ project_id: OTHER, display_name: "Loft Conversion" }),
      ],
      last_opened_project_id: SEED,
    });
    const storage = memoryStorage({ "studio.lastProjectId": OTHER });
    const { screen, waitFor } = await renderApp(transport, storage);

    await waitFor(() => screen.getByLabelText("Message Astra"));
    assert.ok(screen.getByRole("button", { name: /Loft Conversion/ }));
  });

  it("falls back to the project screen when the remembered project is gone", async () => {
    const transport = stubs({ projects: [], last_opened_project_id: null });
    const storage = memoryStorage({ "studio.lastProjectId": "proj_deleted" });
    const { screen, waitFor } = await renderApp(transport, storage);

    await waitFor(() => screen.getByRole("heading", { name: "What are we designing?" }));
    assert.equal(storage.values.get("studio.lastProjectId"), undefined);
  });

  // --- creating and switching --------------------------------------------

  it("creates a project and opens it straight away", async () => {
    const transport = stubs({ projects: [], last_opened_project_id: null });
    transport.onPost("/api/projects", (call) => ({
      status: 201,
      body: {
        project: project({
          project_id: OTHER,
          display_name: (call.body as { display_name: string }).display_name,
        }),
      },
    }));

    const { screen, waitFor, fireEvent, storage } = await renderApp(transport);
    await waitFor(() => screen.getByLabelText("New project"));

    fireEvent.change(screen.getByLabelText("New project"), {
      target: { value: "Loft Conversion" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => screen.getByLabelText("Message Astra"));
    assert.ok(screen.getByRole("button", { name: /Loft Conversion/ }));
    // Remembered, so the next launch resumes here.
    assert.equal(storage.values.get("studio.lastProjectId"), OTHER);
    // The name the user typed is what was sent.
    const created = transport.calls.find(
      (call) => call.method === "POST" && call.url === "/api/projects",
    );
    assert.deepEqual(created?.body, { display_name: "Loft Conversion" });
  });

  it("will not create a project with a blank name", async () => {
    const transport = stubs({ projects: [], last_opened_project_id: null });
    const { screen, waitFor, fireEvent } = await renderApp(transport);
    await waitFor(() => screen.getByLabelText("New project"));

    const button = screen.getByRole("button", { name: "Create project" });
    assert.equal((button as HTMLButtonElement).disabled, true);

    fireEvent.change(screen.getByLabelText("New project"), { target: { value: "   " } });
    assert.equal((button as HTMLButtonElement).disabled, true);
    assert.equal(
      transport.calls.filter((call) => call.method === "POST").length,
      0,
      "nothing should have been sent",
    );
  });

  it("opens a project from the screen and records the visit", async () => {
    const transport = stubs({ projects: [project()], last_opened_project_id: null });
    transport.onPost(`/api/projects/${SEED}/open`, {
      status: 200,
      body: { project: project({ last_opened_at: "2026-09-15T10:00:00Z" }) },
    });

    const { screen, waitFor, fireEvent, storage } = await renderApp(transport);
    await waitFor(() => screen.getByRole("button", { name: /Beach House Kitchen/ }));

    fireEvent.click(screen.getByRole("button", { name: /Beach House Kitchen/ }));

    await waitFor(() => screen.getByLabelText("Message Astra"));
    assert.equal(storage.values.get("studio.lastProjectId"), SEED);
    assert.ok(
      transport.calls.some(
        (call) => call.method === "POST" && call.url === `/api/projects/${SEED}/open`,
      ),
      "the server must be told which project is open",
    );
  });

  it("switches project from the top bar, and can go back to all projects", async () => {
    const transport = stubs({
      projects: [
        project({ last_opened_at: "2026-09-15T09:00:00Z" }),
        project({ project_id: OTHER, display_name: "Loft Conversion" }),
      ],
      last_opened_project_id: SEED,
    });
    transport.onPost(`/api/projects/${OTHER}/open`, {
      status: 200,
      body: { project: project({ project_id: OTHER, display_name: "Loft Conversion" }) },
    });

    const { screen, waitFor, fireEvent } = await renderApp(transport);
    await waitFor(() => screen.getByLabelText("Message Astra"));

    fireEvent.click(screen.getByRole("button", { name: /Beach House Kitchen/ }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Loft Conversion/ }));

    await waitFor(() => screen.getByRole("button", { name: /Loft Conversion/ }));

    // And the menu offers a way back to the project screen.
    fireEvent.click(screen.getByRole("button", { name: /Loft Conversion/ }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /All projects/ }));
    await waitFor(() => screen.getByRole("heading", { name: "What are we designing?" }));
  });

  it("renames the open project", async () => {
    const transport = stubs({
      projects: [project({ last_opened_at: "2026-09-15T09:00:00Z" })],
      last_opened_project_id: SEED,
    });
    transport.onPost(`/api/projects/${SEED}/rename`, (call) => ({
      status: 200,
      body: {
        project: project({
          display_name: (call.body as { display_name: string }).display_name,
          last_opened_at: "2026-09-15T09:00:00Z",
        }),
      },
    }));

    const { screen, waitFor, fireEvent } = await renderApp(transport);
    await waitFor(() => screen.getByLabelText("Message Astra"));

    fireEvent.click(screen.getByRole("button", { name: /Beach House Kitchen/ }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Rename this project/ }));

    const field = screen.getByLabelText("Project name");
    fireEvent.change(field, { target: { value: "Kitchen Refit" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => screen.getByRole("button", { name: /Kitchen Refit/ }));
  });

  it("reports a project that cannot be loaded, without a blank screen", async () => {
    const transport = new StubTransport();
    transport.onGet("/api/projects", {
      status: 503,
      body: { error: { code: "BLENDER_UNAVAILABLE", message: "no" } },
    });
    transport.onGet("/api/status/astra", { status: 500, body: {} });
    transport.onGet("/api/status/blender", { status: 500, body: {} });

    const { screen, waitFor } = await renderApp(transport);
    await waitFor(() => screen.getByRole("alert"));
    assert.match(screen.getByRole("alert").textContent ?? "", /\S/);
    // Still usable: a new project can be started even if the list failed.
    assert.ok(screen.getByRole("button", { name: "Create project" }));
  });
});
