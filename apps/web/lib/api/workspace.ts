/**
 * The design workspace API client.
 *
 * Deliberately separate from `client.ts` rather than bolted onto it. The Spec 001
 * client has its own well-tested request pipeline built around JSON, and multipart
 * uploads need different header handling; adding a second code path inside it would
 * have put 31 passing tests at risk for no gain. Both share `errors.ts`, so failures
 * reach the UI as the same `ApiFailure` with the same user-facing messages.
 *
 * Nothing here ever surfaces a raw status code, canonical error code, or path to the
 * user: `failureFromCode` maps them to sentences a designer can act on.
 */

import { API_BASE_URL, REQUEST_TIMEOUT_MS } from "../config.ts";
import { ApiFailure, TRANSPORT_MESSAGES, failureFromCode } from "./errors.ts";
import type { PreviewView } from "./types.ts";

/** A reference the user uploaded, or a page derived from one. */
export interface ReferenceView {
  reference_id: string;
  project_id: string;
  kind: "image" | "pdf" | "document" | "pdf_page";
  display_name: string;
  label: string;
  media_type: string;
  size_bytes: number;
  created_at: string;
  page_count: number | null;
  parent_reference_id: string | null;
  page_number: number | null;
  width: number | null;
  height: number | null;
  has_text: boolean;
  is_image: boolean;
  /** Present on an upload response: the page images derived from a PDF. */
  pages?: ReferenceView[];
  /** Present on an upload response when identical bytes were already stored. */
  deduplicated?: boolean;
  /** Present when a long PDF had only its first pages prepared. */
  notice?: string | null;
}

export interface ClarificationView {
  clarification_id: string;
  question: string;
  missing_information: string[];
  created_at: string;
  resolved: boolean;
}

export interface ApprovalView {
  approval_id: string;
  /** The actual code. Showing it to the user IS the feature. */
  code: string;
  summary: string;
  reasons: string[];
  decision: "pending" | "approved" | "rejected";
  created_at: string;
  decided_at: string | null;
  operation_count: number;
}

export interface FactView {
  key: string;
  value: string;
  source: string;
  updated_at: string;
}

export interface SceneObjectView {
  studio_object_id: string | null;
  name: string;
  object_type: string;
  world_position_meters: { x: number; y: number; z: number };
  dimensions_meters: { x: number; y: number; z: number };
  rotation_euler_radians: { x: number; y: number; z: number };
  scale: { x: number; y: number; z: number };
  visible: boolean;
  material: { name: string; base_color: { r: number; g: number; b: number; a: number } | null } | null;
}

export interface SceneView {
  project_id: string;
  scene_version: string;
  captured_at: string;
  units: { unit_system: string; length_unit: string; scale_length: number };
  objects: SceneObjectView[];
}

export interface ArtifactRefView {
  artifact_id: string;
  artifact_type: string;
  media_type: string;
  created_at: string;
  size_bytes: number | null;
  scene_version: string | null;
  url: string;
}

export type TurnKind = "answer" | "clarification" | "approval_required" | "plan" | "error";

export interface DesignTurnView {
  kind: TurnKind;
  message: string;
  request_id: string;
  project_id: string;
  session_id: string;
  job_id: string | null;
  job_status: string | null;
  worker_id: string | null;
  duplicate: boolean;
  operation_count: number;
  clarification: ClarificationView | null;
  approval: ApprovalView | null;
  assumptions: string[];
  facts_recorded: string[];
  provider: string | null;
  status_url: string | null;
}

export interface WorkspaceView {
  project: { project_id?: string; display_name: string; blend_ready?: boolean };
  references: ReferenceView[];
  facts: FactView[];
  conversation: { role: string; text: string; created_at: string }[];
  clarification: ClarificationView | null;
  approvals: ApprovalView[];
  scene: SceneView | null;
  model: ArtifactRefView | null;
  preview: ArtifactRefView | null;
}

export interface ConnectionStatusView {
  state: string;
  label: string;
  message: string;
  connected: boolean;
  /** The exact command the user can run, when there is one. */
  action?: string | null;
  model?: string | null;
  codex_version?: string | null;
  provider?: string | null;
  blender_version?: string | null;
  supports_modelling?: boolean;
  worker_count?: number;
}

/** An in-progress official Codex sign-in. */
export interface LoginSessionView {
  supported: boolean;
  state: "idle" | "starting" | "waiting" | "complete" | "failed" | "cancelled";
  /** Where the user authorises. Always an official OpenAI URL. */
  verification_url: string | null;
  /** Present only in the device-code flow. */
  user_code: string | null;
  device_auth?: boolean;
  detail?: string;
  waiting: boolean;
  message?: string;
  status?: ConnectionStatusView;
}

export interface DesignChatInput {
  requestId: string;
  sessionId: string;
  message: string;
  attachedReferenceIds?: string[];
  selectedObjectId?: string | null;
}

/** One project, as the project screen and the switcher show it. */
export interface ProjectSummaryView {
  project_id: string;
  display_name: string;
  created_at: string;
  updated_at: string;
  last_opened_at: string | null;
  blend_ready: boolean;
  latest_scene_version: string | null;
  reference_count: number;
  message_count: number;
  has_model: boolean;
}

export interface ProjectListView {
  projects: ProjectSummaryView[];
  /** Which project to reopen, or null on a fresh install. */
  last_opened_project_id: string | null;
}

export interface WorkspaceClient {
  readonly baseUrl: string;
  listProjects(): Promise<ProjectListView>;
  createProject(displayName: string): Promise<ProjectSummaryView>;
  openProject(projectId: string): Promise<ProjectSummaryView>;
  renameProject(projectId: string, displayName: string): Promise<ProjectSummaryView>;
  getWorkspace(projectId: string): Promise<WorkspaceView>;
  listReferences(projectId: string): Promise<ReferenceView[]>;
  uploadReference(projectId: string, file: File): Promise<ReferenceView>;
  deleteReference(projectId: string, referenceId: string): Promise<void>;
  referenceContentUrl(projectId: string, referenceId: string): string;
  sendMessage(projectId: string, input: DesignChatInput): Promise<DesignTurnView>;
  decideApproval(
    projectId: string,
    approvalId: string,
    approved: boolean,
    sessionId?: string,
  ): Promise<DesignTurnView>;
  getScene(projectId: string): Promise<SceneView | null>;
  getLatestModel(projectId: string): Promise<ArtifactRefView | null>;
  getAstraStatus(): Promise<ConnectionStatusView>;
  beginAstraLogin(deviceAuth?: boolean): Promise<LoginSessionView>;
  getAstraLogin(): Promise<LoginSessionView>;
  cancelAstraLogin(): Promise<LoginSessionView>;
  getBlenderStatus(): Promise<ConnectionStatusView>;
  setFact(projectId: string, key: string, value: string): Promise<FactView>;
  absoluteUrl(url: string): string;
}

export interface WorkspaceClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

function joinUrl(baseUrl: string, path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

export function createWorkspaceClient(options: WorkspaceClientOptions = {}): WorkspaceClient {
  const baseUrl = (options.baseUrl ?? API_BASE_URL).replace(/\/+$/, "");
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const timeoutMs = options.timeoutMs ?? REQUEST_TIMEOUT_MS;

  async function send<T>(path: string, init: RequestInit = {}, allowNotFound = false): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await fetchImpl(joinUrl(baseUrl, path), {
        ...init,
        signal: controller.signal,
        headers: { Accept: "application/json", ...(init.headers ?? {}) },
      });
    } catch (error) {
      const aborted = (error as { name?: string } | null)?.name === "AbortError";
      throw new ApiFailure(
        aborted ? TRANSPORT_MESSAGES.timeout : TRANSPORT_MESSAGES.offline,
        aborted ? "timeout" : "offline",
      );
    } finally {
      clearTimeout(timer);
    }

    if (response.status === 404 && allowNotFound) {
      return null as T;
    }

    if (!response.ok) {
      let code: string | null = null;
      try {
        const body = (await response.json()) as { error?: { code?: string } };
        code = body?.error?.code ?? null;
      } catch {
        code = null;
      }
      throw failureFromCode(code, response.status);
    }

    if (response.status === 204) return undefined as T;
    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiFailure(TRANSPORT_MESSAGES.malformed, "malformed");
    }
  }

  function json(body: unknown): RequestInit {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  return {
    baseUrl,

    listProjects() {
      return send<ProjectListView>("/api/projects");
    },

    async createProject(displayName) {
      const body = await send<{ project: ProjectSummaryView }>(
        "/api/projects",
        json({ display_name: displayName }),
      );
      return body.project;
    },

    async openProject(projectId) {
      const body = await send<{ project: ProjectSummaryView }>(
        `/api/projects/${projectId}/open`,
        { method: "POST" },
      );
      return body.project;
    },

    async renameProject(projectId, displayName) {
      const body = await send<{ project: ProjectSummaryView }>(
        `/api/projects/${projectId}/rename`,
        json({ display_name: displayName }),
      );
      return body.project;
    },

    getWorkspace(projectId) {
      return send<WorkspaceView>(`/api/projects/${projectId}/workspace`);
    },

    async listReferences(projectId) {
      const body = await send<{ references: ReferenceView[] }>(
        `/api/projects/${projectId}/references`,
      );
      return body.references;
    },

    uploadReference(projectId, file) {
      // FormData sets its own multipart boundary, so Content-Type is deliberately
      // NOT set here: setting it by hand would omit the boundary and break parsing.
      const form = new FormData();
      form.append("file", file, file.name);
      return send<ReferenceView>(`/api/projects/${projectId}/references`, {
        method: "POST",
        body: form,
      });
    },

    async deleteReference(projectId, referenceId) {
      await send<unknown>(`/api/projects/${projectId}/references/${referenceId}`, {
        method: "DELETE",
      });
    },

    referenceContentUrl(projectId, referenceId) {
      return joinUrl(baseUrl, `/api/projects/${projectId}/references/${referenceId}/content`);
    },

    sendMessage(projectId, input) {
      return send<DesignTurnView>(
        `/api/projects/${projectId}/design-chat`,
        json({
          request_id: input.requestId,
          session_id: input.sessionId,
          message: input.message,
          attached_reference_ids: input.attachedReferenceIds ?? [],
          ...(input.selectedObjectId ? { selected_object_id: input.selectedObjectId } : {}),
        }),
      );
    },

    decideApproval(projectId, approvalId, approved, sessionId) {
      return send<DesignTurnView>(
        `/api/projects/${projectId}/approvals/${approvalId}`,
        json({ approved, ...(sessionId ? { session_id: sessionId } : {}) }),
      );
    },

    async getScene(projectId) {
      const body = await send<{ scene: SceneView | null }>(`/api/projects/${projectId}/scene`);
      return body.scene;
    },

    async getLatestModel(projectId) {
      const body = await send<{ model: ArtifactRefView | null }>(
        `/api/projects/${projectId}/model/latest`,
      );
      return body.model;
    },

    getAstraStatus() {
      return send<ConnectionStatusView>("/api/status/astra");
    },

    getBlenderStatus() {
      return send<ConnectionStatusView>("/api/status/blender");
    },

    beginAstraLogin(deviceAuth = false) {
      return send<LoginSessionView>(
        "/api/status/astra/login",
        json({ device_auth: deviceAuth }),
      );
    },

    getAstraLogin() {
      return send<LoginSessionView>("/api/status/astra/login");
    },

    cancelAstraLogin() {
      return send<LoginSessionView>("/api/status/astra/login", { method: "DELETE" });
    },

    async setFact(projectId, key, value) {
      const body = await send<{ fact: FactView }>(`/api/projects/${projectId}/facts`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, value }),
      });
      return body.fact;
    },

    absoluteUrl(url) {
      return joinUrl(baseUrl, url);
    },
  };
}

export const workspaceClient: WorkspaceClient = createWorkspaceClient();

export type { PreviewView };
