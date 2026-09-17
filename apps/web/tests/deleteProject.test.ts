/**
 * Deleting a project, and the gate in front of it.
 *
 * There is no bin to recover from, so the confirmation is typing the project's name. These
 * tests are about that gate holding: a wrong name cannot delete, the button is unavailable
 * until the name matches, cancelling does nothing at all, and the request only goes when
 * the user has actually typed it.
 *
 * The server enforces the same rule (services/api/tests/test_project_routes.py). This is
 * the courtesy; that is the guarantee.
 */

import assert from "node:assert/strict";
import { after, before, beforeEach, describe, it } from "node:test";

import globalJsdom from "global-jsdom";

import { API_BASE, StubTransport } from "./support.ts";

const KEEP = "proj_keep0001";
const DOOMED = "proj_doom0001";

function project(overrides: Record<string, unknown> = {}) {
  return {
    project_id: DOOMED,
    display_name: "Beach House Kitchen",
    created_at: "2026-09-01T10:00:00Z",
    updated_at: "2026-09-01T10:00:00Z",
    last_opened_at: null,
    blend_ready: true,
    latest_scene_version: null,
    reference_count: 3,
    message_count: 12,
    has_model: true,
    ...overrides,
  };
}

function workspaceBody(projectId: string, name: string) {
  return {
    project: { project_id: projectId, display_name: name },
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

function stubs(listing: Record<string, unknown>) {
  const transport = new StubTransport();
  transport.onGet("/api/projects", { status: 200, body: listing });
  transport.onGet("/api/status/astra", {
    status: 200,
    body: { state: "connected", label: "Astra via Codex", message: "ok", connected: true },
  });
  transport.onGet("/api/status/blender", {
    status: 200,
    body: { state: "connected", label: "Blender", message: "ok", connected: true },
  });
  transport.onGet(`/api/projects/${DOOMED}/workspace`, {
    status: 200,
    body: workspaceBody(DOOMED, "Beach House Kitchen"),
  });
  transport.onGet(`/api/projects/${KEEP}/workspace`, {
    status: 200,
    body: workspaceBody(KEEP, "Keep This"),
  });
  return transport;
}

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    removeItem: (key: string) => void values.delete(key),
    values,
  };
}

describe("deleting a project", () => {
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

  // --- the gate -----------------------------------------------------------

  it("asks for the project name, and says what will be lost", async () => {
    const transport = stubs({ projects: [project()], last_opened_project_id: null });
    const { screen, waitFor, fireEvent } = await renderApp(transport);

    await waitFor(() => screen.getByRole("button", { name: "Delete Beach House Kitchen" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Beach House Kitchen" }));

    const dialog = await screen.findByRole("dialog", {
      name: /Delete .Beach House Kitchen/,
    });
    assert.match(dialog.textContent ?? "", /cannot be undone/);
    // Counted from the project, so the decision is informed.
    assert.match(dialog.textContent ?? "", /3 uploaded files/);
    assert.match(dialog.textContent ?? "", /12 messages/);
    assert.match(dialog.textContent ?? "", /the 3D model/);
    assert.ok(screen.getByLabelText("Type the project name to confirm"));
  });

  it("will not delete until the name is typed exactly", async () => {
    const transport = stubs({ projects: [project()], last_opened_project_id: null });
    const { screen, waitFor, fireEvent } = await renderApp(transport);

    await waitFor(() => screen.getByRole("button", { name: "Delete Beach House Kitchen" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Beach House Kitchen" }));

    const confirm = () =>
      screen.getByRole("button", { name: "Delete project" }) as HTMLButtonElement;
    const field = await screen.findByLabelText("Type the project name to confirm");

    assert.equal(confirm().disabled, true, "nothing typed yet");

    fireEvent.change(field, { target: { value: "Beach House" } });
    assert.equal(confirm().disabled, true, "a prefix is not the name");

    fireEvent.change(field, { target: { value: "beach house kitchen" } });
    assert.equal(confirm().disabled, true, "the wrong case is not the name");

    fireEvent.change(field, { target: { value: "Beach House Kitchen" } });
    assert.equal(confirm().disabled, false, "the exact name unlocks it");

    // Nothing was sent while it was locked.
    assert.equal(
      transport.calls.filter((call) => call.url.includes("/delete")).length,
      0,
    );
  });

  it("forgives whitespace, because a copy-paste often carries it", async () => {
    const transport = stubs({ projects: [project()], last_opened_project_id: null });
    const { screen, waitFor, fireEvent } = await renderApp(transport);
    await waitFor(() => screen.getByRole("button", { name: "Delete Beach House Kitchen" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Beach House Kitchen" }));

    const field = await screen.findByLabelText("Type the project name to confirm");
    fireEvent.change(field, { target: { value: "  Beach House Kitchen " } });

    assert.equal(
      (screen.getByRole("button", { name: "Delete project" }) as HTMLButtonElement).disabled,
      false,
    );
  });

  it("cancelling does nothing at all", async () => {
    const transport = stubs({ projects: [project()], last_opened_project_id: null });
    const { screen, waitFor, fireEvent } = await renderApp(transport);
    await waitFor(() => screen.getByRole("button", { name: "Delete Beach House Kitchen" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Beach House Kitchen" }));

    const field = await screen.findByLabelText("Type the project name to confirm");
    fireEvent.change(field, { target: { value: "Beach House Kitchen" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // Compared before asserting, never handed to the assertion: a retrying waitFor that
    // passes a live DOM node to assert.equal makes Node format the whole element tree on
    // every attempt, which is enough to exhaust the heap.
    await waitFor(() => assert.ok(screen.queryByRole("dialog") === null));
    assert.equal(transport.calls.filter((call) => call.url.includes("/delete")).length, 0);
    assert.ok(screen.getByRole("button", { name: "Open Beach House Kitchen" }));
  });

  // --- doing it -----------------------------------------------------------

  it("deletes the project and removes it from the list", async () => {
    const transport = stubs({
      projects: [project(), project({ project_id: KEEP, display_name: "Keep This" })],
      last_opened_project_id: null,
    });
    transport.onPost(`/api/projects/${DOOMED}/delete`, {
      status: 200,
      body: {
        project_id: DOOMED,
        display_name: "Beach House Kitchen",
        deleted: true,
        references: 3,
        messages: 12,
        facts: 1,
        artifacts: 2,
      },
    });

    const { screen, waitFor, fireEvent } = await renderApp(transport);
    await waitFor(() => screen.getByRole("button", { name: "Delete Beach House Kitchen" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Beach House Kitchen" }));

    const field = await screen.findByLabelText("Type the project name to confirm");
    fireEvent.change(field, { target: { value: "Beach House Kitchen" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    await waitFor(() => {
      assert.ok(
        screen.queryByRole("button", { name: "Open Beach House Kitchen" }) === null,
      );
    });
    // The other project is untouched.
    assert.ok(screen.getByRole("button", { name: "Open Keep This" }));

    // The typed name went with the request: the server checks it too.
    const sent = transport.calls.find((call) => call.url.includes("/delete"));
    assert.deepEqual(sent?.body, { confirm_display_name: "Beach House Kitchen" });
  });

  it("deleting the project you are in returns you to the project screen", async () => {
    const transport = stubs({
      projects: [project({ last_opened_at: "2026-09-15T09:00:00Z" })],
      last_opened_project_id: DOOMED,
    });
    transport.onPost(`/api/projects/${DOOMED}/delete`, {
      status: 200,
      body: {
        project_id: DOOMED,
        display_name: "Beach House Kitchen",
        deleted: true,
        references: 0,
        messages: 0,
        facts: 0,
        artifacts: 0,
      },
    });

    const storage = memoryStorage({ "studio.lastProjectId": DOOMED });
    const { screen, waitFor, fireEvent } = await renderApp(transport, storage);

    // It opened straight into the workspace, so the delete has to be reachable from
    // inside the project — not from the project screen.
    await waitFor(() => screen.getByLabelText("Message Astra"));

    // The only route in is the project title in the top bar.
    fireEvent.click(screen.getByRole("button", { name: /Switch project/ }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /Delete this project/ }));

    const field = await screen.findByLabelText("Type the project name to confirm");
    fireEvent.change(field, { target: { value: "Beach House Kitchen" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    // Deleting the project you are in has nothing left to show, so it lands back on the
    // project screen rather than a workspace whose every request would fail.
    await waitFor(() => screen.getByRole("heading", { name: "What are we designing?" }));
    // And the project it deleted is gone from the list.
    assert.ok(screen.queryByRole("button", { name: "Open Beach House Kitchen" }) === null);
    // And it is not remembered as the project to reopen.
    assert.equal(storage.values.get("studio.lastProjectId"), undefined);
  });

  it("a refused deletion is reported and changes nothing", async () => {
    const transport = stubs({ projects: [project()], last_opened_project_id: null });
    transport.onPost(`/api/projects/${DOOMED}/delete`, {
      status: 422,
      body: {
        error: {
          code: "VALIDATION_ERROR",
          message: "That name does not match this project, so nothing was deleted.",
        },
      },
    });

    const { screen, waitFor, fireEvent } = await renderApp(transport);
    await waitFor(() => screen.getByRole("button", { name: "Delete Beach House Kitchen" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Beach House Kitchen" }));

    const field = await screen.findByLabelText("Type the project name to confirm");
    fireEvent.change(field, { target: { value: "Beach House Kitchen" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    await waitFor(() => screen.getByRole("alert"));
    assert.match(screen.getByRole("alert").textContent ?? "", /nothing was deleted/);
    // Still listed, and still deletable once the name is right.
    assert.ok(screen.getByRole("dialog"));
  });

  it("a refused deletion's message does not outlive its dialog", async () => {
    const transport = stubs({ projects: [project()], last_opened_project_id: null });
    transport.onPost(`/api/projects/${DOOMED}/delete`, {
      status: 422,
      body: {
        error: {
          code: "VALIDATION_ERROR",
          message: "That name does not match this project, so nothing was deleted.",
        },
      },
    });

    const { screen, waitFor, fireEvent } = await renderApp(transport);
    await waitFor(() => screen.getByRole("button", { name: "Delete Beach House Kitchen" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Beach House Kitchen" }));

    const field = await screen.findByLabelText("Type the project name to confirm");
    fireEvent.change(field, { target: { value: "Beach House Kitchen" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));
    await waitFor(() => screen.getByRole("alert"));

    // Backing out takes the refusal with it: the message described that attempt, and there
    // is nothing on the project screen for it to refer to.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => assert.ok(screen.queryByRole("dialog") === null));
    assert.ok(screen.queryByRole("alert") === null);
    // And the project is untouched, so it can still be deleted properly.
    assert.ok(screen.getByRole("button", { name: "Open Beach House Kitchen" }));
  });
});
