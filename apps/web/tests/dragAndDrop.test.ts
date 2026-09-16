/**
 * Dropping a floor plan onto the conversation.
 *
 * This is how people actually add a reference: they drag it from a file manager onto the
 * chat. The whole chat column is the target — not a small dashed rectangle in a sidebar —
 * so these tests drop onto the conversation region itself.
 */

import assert from "node:assert/strict";
import { after, before, beforeEach, describe, it } from "node:test";

import globalJsdom from "global-jsdom";

import { API_BASE, StubTransport } from "./support.ts";

const PROJECT = "proj_seed";

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

function reference(overrides: Record<string, unknown> = {}) {
  return {
    reference_id: "ref_1",
    project_id: PROJECT,
    kind: "pdf",
    display_name: "ground-floor.pdf",
    label: "ground-floor.pdf",
    media_type: "application/pdf",
    size_bytes: 2048,
    created_at: "2026-09-15T10:00:00Z",
    page_count: 2,
    parent_reference_id: null,
    page_number: null,
    width: null,
    height: null,
    has_text: true,
    is_image: false,
    pages: [],
    deduplicated: false,
    notice: null,
    ...overrides,
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

/** A drop payload shaped like the real one: files plus the "Files" type flag. */
function filesPayload(files: File[]) {
  return {
    dataTransfer: {
      files,
      items: files.map((file) => ({ kind: "file", type: file.type })),
      types: ["Files"],
    },
  };
}

describe("adding files by dropping them on the chat", () => {
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
          sessionOptions: { client, jobClient, projectId: PROJECT, poll: false },
        }),
      ),
    );
    return { screen, waitFor, fireEvent };
  }

  it("uploads a file dropped anywhere on the conversation", async () => {
    const transport = stubs();
    transport.onPost(`/api/projects/${PROJECT}/references`, {
      status: 201,
      body: reference(),
    });

    const { screen, waitFor, fireEvent } = await renderShell(transport);
    await waitFor(() => screen.getByRole("region", { name: "Conversation" }));

    const column = screen.getByRole("region", { name: "Conversation" });
    const file = new File(["%PDF-1.4"], "ground-floor.pdf", { type: "application/pdf" });
    fireEvent.drop(column, filesPayload([file]));

    // It appears in the files list, and is attached to the next message: you dropped it
    // in order to have it looked at.
    await waitFor(() => screen.getByRole("list", { name: "Files in this project" }));
    assert.match(document.body.textContent ?? "", /ground-floor\.pdf/);
    assert.match(document.body.textContent ?? "", /2 pages/);

    const attach = screen.getByRole("checkbox", { name: "Attach" }) as HTMLInputElement;
    assert.equal(attach.checked, true, "a dropped file starts attached");

    const posted = transport.calls.find(
      (call) => call.method === "POST" && call.url === `/api/projects/${PROJECT}/references`,
    );
    assert.ok(posted, "the file must actually be uploaded");
    // Sent as multipart under the field the API expects, with the real filename.
    assert.deepEqual(posted?.files, [
      { field: "file", name: "ground-floor.pdf", type: "application/pdf" },
    ]);
  });

  it("shows a drop target while a file is over the conversation", async () => {
    const { screen, waitFor, fireEvent } = await renderShell(stubs());
    await waitFor(() => screen.getByRole("region", { name: "Conversation" }));

    const column = screen.getByRole("region", { name: "Conversation" });
    const file = new File(["x"], "plan.png", { type: "image/png" });

    fireEvent.dragEnter(column, filesPayload([file]));
    assert.match(document.body.textContent ?? "", /Drop to add to this project/);

    fireEvent.dragLeave(column, filesPayload([file]));
    assert.doesNotMatch(document.body.textContent ?? "", /Drop to add to this project/);
  });

  it("ignores a drag that carries no files", async () => {
    const { screen, waitFor, fireEvent } = await renderShell(stubs());
    await waitFor(() => screen.getByRole("region", { name: "Conversation" }));

    const column = screen.getByRole("region", { name: "Conversation" });
    // Dragging selected TEXT across the window must not arm the upload overlay.
    fireEvent.dragEnter(column, {
      dataTransfer: { files: [], items: [], types: ["text/plain"] },
    });

    assert.doesNotMatch(document.body.textContent ?? "", /Drop to add to this project/);
  });

  it("reports a rejected upload instead of losing it", async () => {
    const transport = stubs();
    transport.onPost(`/api/projects/${PROJECT}/references`, {
      status: 422,
      body: {
        error: { code: "VALIDATION_ERROR", message: "That file type is not supported." },
      },
    });

    const { screen, waitFor, fireEvent } = await renderShell(transport);
    await waitFor(() => screen.getByRole("region", { name: "Conversation" }));

    const column = screen.getByRole("region", { name: "Conversation" });
    const file = new File(["MZ"], "installer.exe", { type: "application/octet-stream" });
    fireEvent.drop(column, filesPayload([file]));

    await waitFor(() => {
      assert.match(document.body.textContent ?? "", /installer\.exe/);
    });
    // A sentence, never a status code.
    const text = document.body.textContent ?? "";
    assert.doesNotMatch(text, /422/);
    assert.match(text, /[A-Z][a-z]+.*\./);
  });

  it("the paperclip and the files button share one file input", async () => {
    const { screen, waitFor } = await renderShell(stubs());
    await waitFor(() => screen.getByRole("region", { name: "Conversation" }));

    const inputs = Array.from(
      document.querySelectorAll<HTMLInputElement>('input[type="file"]'),
    );
    assert.equal(inputs.length, 1, "one upload path, not two");
    assert.equal(inputs[0]?.multiple, true);
    assert.ok(screen.getByRole("button", { name: "Attach references" }));
    assert.ok(screen.getByRole("button", { name: "Add files" }));
  });
});
