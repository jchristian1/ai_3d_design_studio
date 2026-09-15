/**
 * The workspace reducer and client, tested without a DOM.
 *
 * The reducer is where the "one message, one reply slot" promise is kept, so it gets the
 * most attention here.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createWorkspaceClient } from "../lib/api/workspace.ts";
import { initialWorkspaceState, workspaceReducer } from "../lib/workspace/reducer.ts";
import {
  attachedReferences,
  canModel,
  isBusy,
  pendingApproval,
  selectedObject,
  studioEntryId,
  userEntryId,
} from "../lib/workspace/types.ts";
import type { WorkspaceState } from "../lib/workspace/types.ts";
import type { ApprovalView, DesignTurnView, ReferenceView } from "../lib/api/workspace.ts";
import { StubTransport } from "./support.ts";

const PROJECT = "proj_seed";

function state(): WorkspaceState {
  return initialWorkspaceState({ projectId: PROJECT, sessionId: "sess_1" });
}

function reduce(initial: WorkspaceState, ...events: Parameters<typeof workspaceReducer>[1][]) {
  return events.reduce(workspaceReducer, initial);
}

function turn(overrides: Partial<DesignTurnView> = {}): DesignTurnView {
  return {
    kind: "answer",
    message: "Here is what I see.",
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

function reference(overrides: Partial<ReferenceView> = {}): ReferenceView {
  return {
    reference_id: "ref_a",
    project_id: PROJECT,
    kind: "image",
    display_name: "room.jpg",
    label: "room.jpg",
    media_type: "image/jpeg",
    size_bytes: 1024,
    created_at: "2026-01-01T00:00:00Z",
    page_count: null,
    parent_reference_id: null,
    page_number: null,
    width: 40,
    height: 30,
    has_text: false,
    is_image: true,
    ...overrides,
  };
}

const APPROVAL: ApprovalView = {
  approval_id: "apr_1",
  code: "import shutil\nshutil.rmtree('/home/christian')\n",
  summary: "This step imports shutil.",
  reasons: ["line 1: imports shutil, which is outside ordinary Blender scene work"],
  decision: "pending",
  created_at: "2026-01-01T00:00:00Z",
  decided_at: null,
  operation_count: 1,
};

describe("one message owns one reply slot", () => {
  it("pairs a user entry with exactly one studio entry", () => {
    const next = reduce(
      state(),
      { type: "message_sent", requestId: "req_1", text: "build a wall" },
      { type: "turn_received", turn: turn({ message: "Building." }) },
    );
    assert.deepEqual(
      next.entries.map((entry) => entry.id),
      [userEntryId("req_1"), studioEntryId("req_1")],
    );
  });

  it("replaces the studio entry in place as work progresses", () => {
    const next = reduce(
      state(),
      { type: "message_sent", requestId: "req_1", text: "reconstruct this" },
      { type: "turn_received", turn: turn({ kind: "plan", message: "Building.", job_id: "job_1" }) },
      { type: "job_progress", requestId: "req_1", label: "Creating Wall_A", stepIndex: 0, stepCount: 9 },
      { type: "job_progress", requestId: "req_1", label: "Creating Wall_B", stepIndex: 1, stepCount: 9 },
      {
        type: "job_settled",
        requestId: "req_1",
        succeeded: true,
        message: "Done — 9 changes applied.",
        model: null,
        scene: null,
      },
    );

    assert.equal(next.entries.length, 2, "nine walls must not become nine entries");
    const studio = next.entries.find((entry) => entry.id === studioEntryId("req_1"));
    assert.equal(studio?.text, "Done — 9 changes applied.");
    assert.equal(studio?.tone, "success");
    assert.equal(studio?.progress, null);
  });

  it("keeps progress on the single slot while running", () => {
    const next = reduce(
      state(),
      { type: "message_sent", requestId: "req_1", text: "reconstruct" },
      { type: "turn_received", turn: turn({ kind: "plan", job_id: "job_1" }) },
      { type: "job_progress", requestId: "req_1", label: "Creating Wall_C", stepIndex: 2, stepCount: 9 },
    );
    const studio = next.entries.find((entry) => entry.id === studioEntryId("req_1"));
    assert.deepEqual(studio?.progress, { stepIndex: 2, stepCount: 9, label: "Creating Wall_C" });
    assert.equal(next.phase, "working");
    assert.ok(isBusy(next));
  });

  it("a retry of the same request reuses both entries", () => {
    const next = reduce(
      state(),
      { type: "message_sent", requestId: "req_1", text: "move it" },
      { type: "turn_received", turn: turn({ kind: "plan", job_id: "job_1" }) },
      { type: "message_sent", requestId: "req_1", text: "move it" },
      { type: "turn_received", turn: turn({ kind: "plan", job_id: "job_1", duplicate: true }) },
    );
    assert.equal(next.entries.length, 2);
  });
});

describe("outcome kinds", () => {
  it("a clarification is a question, not an error", () => {
    const next = workspaceReducer(state(), {
      type: "turn_received",
      turn: turn({
        kind: "clarification",
        message: "What is the ceiling height?",
        clarification: {
          clarification_id: "clr_1",
          question: "What is the ceiling height?",
          missing_information: ["ceiling_height_m"],
          created_at: "2026-01-01T00:00:00Z",
          resolved: false,
        },
      }),
    });
    const studio = next.entries[0];
    assert.equal(studio?.tone, "question");
    assert.equal(next.clarification?.missing_information[0], "ceiling_height_m");
    assert.equal(next.phase, "idle", "a question is not work in progress");
  });

  it("an approval carries the actual code onto the entry", () => {
    const next = workspaceReducer(state(), {
      type: "turn_received",
      turn: turn({ kind: "approval_required", message: "Needs approval.", approval: APPROVAL }),
    });
    assert.equal(next.entries[0]?.approval?.code, APPROVAL.code);
    assert.equal(pendingApproval(next)?.approval_id, "apr_1");
    assert.equal(next.phase, "idle", "nothing runs while a decision is pending");
  });

  it("deciding an approval clears it from state and from the entry", () => {
    const next = reduce(
      state(),
      { type: "turn_received", turn: turn({ kind: "approval_required", approval: APPROVAL }) },
      { type: "approval_resolved", approvalId: "apr_1" },
    );
    assert.equal(pendingApproval(next), null);
    assert.equal(next.entries[0]?.approval, null);
  });

  it("assumptions are surfaced on the entry", () => {
    const next = workspaceReducer(state(), {
      type: "turn_received",
      turn: turn({ assumptions: ["I assumed interior walls are 0.12 m thick"] }),
    });
    assert.deepEqual(next.entries[0]?.assumptions, [
      "I assumed interior walls are 0.12 m thick",
    ]);
  });

  it("a failure reads as an error and stops the busy state", () => {
    const next = reduce(
      state(),
      { type: "message_sent", requestId: "req_1", text: "build" },
      { type: "turn_failed", requestId: "req_1", message: "The design machine is offline." },
    );
    assert.equal(next.entries[1]?.tone, "error");
    assert.equal(next.phase, "idle");
  });
});

describe("the model is never blanked mid-update", () => {
  it("keeps the previous model when a settle reports none", () => {
    const model = {
      artifact_id: "model_1",
      artifact_type: "model_glb",
      media_type: "model/gltf-binary",
      created_at: "2026-01-01T00:00:00Z",
      size_bytes: 2048,
      scene_version: "sha256:a",
      url: "/api/projects/proj_seed/artifacts/model_1",
    };
    const withModel = { ...state(), model };
    const next = workspaceReducer(withModel, {
      type: "job_settled",
      requestId: "req_1",
      succeeded: true,
      message: "Done.",
      model: null,
      scene: null,
    });
    assert.equal(next.model?.artifact_id, "model_1");
  });

  it("replaces the model when a new one arrives", () => {
    const next = workspaceReducer(state(), {
      type: "job_settled",
      requestId: "req_1",
      succeeded: true,
      message: "Done.",
      model: {
        artifact_id: "model_2",
        artifact_type: "model_glb",
        media_type: "model/gltf-binary",
        created_at: "2026-01-01T00:01:00Z",
        size_bytes: 4096,
        scene_version: "sha256:b",
        url: "/api/projects/proj_seed/artifacts/model_2",
      },
      scene: null,
    });
    assert.equal(next.model?.artifact_id, "model_2");
  });
});

describe("references and attachments", () => {
  it("a newly uploaded reference is attached automatically", () => {
    const next = workspaceReducer(state(), {
      type: "upload_succeeded",
      id: "u1",
      reference: reference(),
      notice: null,
    });
    assert.deepEqual(next.attachedReferenceIds, ["ref_a"]);
    assert.equal(attachedReferences(next).length, 1);
  });

  it("sending clears attachments so images are not silently re-sent", () => {
    const next = reduce(
      state(),
      { type: "upload_succeeded", id: "u1", reference: reference(), notice: null },
      { type: "message_sent", requestId: "req_1", text: "what do you see?" },
      { type: "turn_received", turn: turn() },
    );
    assert.deepEqual(next.attachedReferenceIds, []);
  });

  it("attachment can be toggled off and on", () => {
    const next = reduce(
      state(),
      { type: "upload_succeeded", id: "u1", reference: reference(), notice: null },
      { type: "attachment_toggled", referenceId: "ref_a" },
    );
    assert.deepEqual(next.attachedReferenceIds, []);
    const again = workspaceReducer(next, { type: "attachment_toggled", referenceId: "ref_a" });
    assert.deepEqual(again.attachedReferenceIds, ["ref_a"]);
  });

  it("removing a reference also detaches it", () => {
    const next = reduce(
      state(),
      { type: "upload_succeeded", id: "u1", reference: reference(), notice: null },
      { type: "reference_removed", referenceId: "ref_a" },
    );
    assert.deepEqual(next.references, []);
    assert.deepEqual(next.attachedReferenceIds, []);
  });

  it("a failed upload is shown rather than lost", () => {
    const next = reduce(
      state(),
      { type: "upload_started", id: "u1", name: "huge.pdf" },
      { type: "upload_failed", id: "u1", message: "That file is larger than the 50 MB limit." },
    );
    assert.equal(next.uploads[0]?.status, "failed");
    assert.match(next.uploads[0]?.message ?? "", /50 MB/);
  });

  it("a truncation notice is surfaced", () => {
    const next = workspaceReducer(state(), {
      type: "upload_succeeded",
      id: "u1",
      reference: reference({ kind: "pdf", display_name: "big.pdf" }),
      notice: "Only the first 20 of 60 pages were prepared for viewing.",
    });
    assert.match(next.notice ?? "", /first 20 of 60/);
  });
});

describe("selection", () => {
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

  it("resolves the selected object from the scene", () => {
    const next = reduce(
      { ...state(), scene },
      { type: "object_selected", objectId: "obj_wall_north" },
    );
    assert.equal(selectedObject(next)?.name, "Wall_North");
  });

  it("an unknown selection resolves to nothing rather than throwing", () => {
    const next = workspaceReducer({ ...state(), scene }, {
      type: "object_selected",
      objectId: "obj_missing",
    });
    assert.equal(selectedObject(next), null);
  });

  it("selection can be cleared", () => {
    const next = reduce(
      { ...state(), scene },
      { type: "object_selected", objectId: "obj_wall_north" },
      { type: "object_selected", objectId: null },
    );
    assert.equal(selectedObject(next), null);
  });
});

describe("connection status", () => {
  it("modelling is gated on Blender being connected", () => {
    assert.equal(canModel(state()), false);
    const next = workspaceReducer(state(), {
      type: "status_updated",
      blender: {
        state: "connected",
        label: "Blender",
        message: "Connected",
        connected: true,
        supports_modelling: true,
      },
    });
    assert.equal(canModel(next), true);
  });

  it("status updates do not disturb the transcript", () => {
    const next = reduce(
      state(),
      { type: "message_sent", requestId: "req_1", text: "hello" },
      {
        type: "status_updated",
        astra: {
          state: "login_required",
          label: "Astra via Codex",
          message: "Sign in to ChatGPT to connect Astra.",
          connected: false,
          action: "codex login",
        },
      },
    );
    assert.equal(next.entries.length, 1);
    assert.equal(next.astra?.action, "codex login");
  });
});

describe("restoring a project", () => {
  it("loads history, references, facts and a pending approval", () => {
    const next = workspaceReducer(state(), {
      type: "workspace_loaded",
      displayName: "Beach House",
      references: [reference()],
      facts: [
        { key: "ceiling_height_m", value: "2.4", source: "user", updated_at: "2026-01-01T00:00:00Z" },
      ],
      conversation: [
        { role: "user", text: "reconstruct this" },
        { role: "assistant", text: "What is the ceiling height?" },
      ],
      clarification: null,
      approvals: [APPROVAL],
      scene: null,
      model: null,
      preview: null,
    });

    assert.equal(next.displayName, "Beach House");
    assert.equal(next.loaded, true);
    assert.equal(next.references.length, 1);
    assert.equal(next.facts[0]?.value, "2.4");
    // Two restored turns plus the pending approval card.
    assert.equal(next.entries.length, 3);
    assert.equal(next.entries[2]?.approval?.approval_id, "apr_1");
    // Restored references are NOT auto-attached: the user did not just upload them.
    assert.deepEqual(next.attachedReferenceIds, []);
  });

  it("a load failure still marks the workspace loaded, with a notice", () => {
    const next = workspaceReducer(state(), {
      type: "workspace_load_failed",
      message: "This project could not be opened.",
    });
    assert.equal(next.loaded, true);
    assert.match(next.notice ?? "", /could not be opened/);
  });
});

describe("the workspace client", () => {
  it("sends a message with attachments and selection", async () => {
    const transport = new StubTransport();
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, { status: 200, body: turn() });
    const client = createWorkspaceClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: transport.fetch,
    });

    await client.sendMessage(PROJECT, {
      requestId: "req_1",
      sessionId: "sess_1",
      message: "make this taller",
      attachedReferenceIds: ["ref_a"],
      selectedObjectId: "obj_wall_north",
    });

    const body = transport.calls.at(-1)?.body as Record<string, unknown>;
    assert.equal(body.request_id, "req_1");
    assert.deepEqual(body.attached_reference_ids, ["ref_a"]);
    assert.equal(body.selected_object_id, "obj_wall_north");
  });

  it("omits the selected object when nothing is selected", async () => {
    const transport = new StubTransport();
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, { status: 200, body: turn() });
    const client = createWorkspaceClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: transport.fetch,
    });

    await client.sendMessage(PROJECT, {
      requestId: "req_1",
      sessionId: "sess_1",
      message: "hello",
    });
    const body = transport.calls.at(-1)?.body as Record<string, unknown>;
    assert.equal("selected_object_id" in body, false);
  });

  it("turns a canonical error code into a sentence, never a code", async () => {
    const transport = new StubTransport();
    transport.onPost(`/api/projects/${PROJECT}/design-chat`, {
      status: 503,
      body: { error: { code: "BLENDER_UNAVAILABLE", message: "internal wording" } },
    });
    const client = createWorkspaceClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: transport.fetch,
    });

    await assert.rejects(
      () => client.sendMessage(PROJECT, { requestId: "r", sessionId: "s", message: "build" }),
      (error: Error) => {
        assert.doesNotMatch(error.message, /BLENDER_UNAVAILABLE/);
        assert.doesNotMatch(error.message, /503/);
        return true;
      },
    );
  });

  it("builds reference content URLs against the configured base", () => {
    const client = createWorkspaceClient({ baseUrl: "http://127.0.0.1:8000/" });
    assert.equal(
      client.referenceContentUrl(PROJECT, "ref_a"),
      `http://127.0.0.1:8000/api/projects/${PROJECT}/references/ref_a/content`,
    );
  });

  it("passes an absolute artifact URL through unchanged", () => {
    const client = createWorkspaceClient({ baseUrl: "http://127.0.0.1:8000" });
    assert.equal(client.absoluteUrl("https://cdn.example/model.glb"), "https://cdn.example/model.glb");
    assert.equal(
      client.absoluteUrl("/api/projects/p/artifacts/model_1"),
      "http://127.0.0.1:8000/api/projects/p/artifacts/model_1",
    );
  });
});
