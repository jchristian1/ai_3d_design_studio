"use client";

/**
 * Which project the studio is showing.
 *
 * Opening the app should feel like opening a document you were working on, so the
 * default is "carry on where I was". The server owns that answer — it records when a
 * project was last opened — and the browser only remembers a hint, so switching
 * machines or clearing storage loses nothing.
 *
 *   remembered locally  ->  still exists on the server?  ->  open it
 *   otherwise           ->  the server's last-opened project
 *   otherwise           ->  the project screen (create or pick)
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiFailure } from "../lib/api/errors.ts";
import type { ProjectSummaryView, WorkspaceClient } from "../lib/api/workspace.ts";
import { workspaceClient as defaultClient } from "../lib/api/workspace.ts";

/** Where the browser remembers the open project. A hint, never the source of truth. */
export const LAST_PROJECT_KEY = "studio.lastProjectId";

export interface UseProjectsOptions {
  client?: WorkspaceClient;
  /** Injected in tests; falls back to localStorage, and to nothing if unavailable. */
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  /** Start on this project rather than the remembered one. */
  initialProjectId?: string | null;
}

export interface ProjectsSession {
  projects: ProjectSummaryView[];
  current: ProjectSummaryView | null;
  loading: boolean;
  error: string | null;
  /** True once the first load settled, so the UI can avoid flashing an empty state. */
  ready: boolean;
  open(projectId: string): Promise<void>;
  create(displayName: string): Promise<ProjectSummaryView | null>;
  rename(projectId: string, displayName: string): Promise<void>;
  close(): void;
  refresh(): Promise<void>;
}

function safeStorage(
  provided?: UseProjectsOptions["storage"],
): UseProjectsOptions["storage"] | null {
  if (provided) return provided;
  try {
    // Private browsing and some embedded webviews throw on access rather than on use.
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function useProjects(options: UseProjectsOptions = {}): ProjectsSession {
  const client = options.client ?? defaultClient;
  const storage = useRef(safeStorage(options.storage));

  const [projects, setProjects] = useState<ProjectSummaryView[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(options.initialProjectId ?? null);
  const [loading, setLoading] = useState(true);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remember = useCallback((projectId: string | null) => {
    try {
      if (projectId) storage.current?.setItem(LAST_PROJECT_KEY, projectId);
      else storage.current?.removeItem(LAST_PROJECT_KEY);
    } catch {
      // Remembering is a convenience. Losing it must never break the studio.
    }
  }, []);

  const load = useCallback(
    async (chooseInitial: boolean) => {
      setLoading(true);
      try {
        const listing = await client.listProjects();
        setProjects(listing.projects);
        setError(null);

        if (!chooseInitial) return;

        const known = new Set(listing.projects.map((project) => project.project_id));
        let remembered: string | null = null;
        try {
          remembered = storage.current?.getItem(LAST_PROJECT_KEY) ?? null;
        } catch {
          remembered = null;
        }
        const resume =
          (remembered && known.has(remembered) ? remembered : null) ??
          (listing.last_opened_project_id && known.has(listing.last_opened_project_id)
            ? listing.last_opened_project_id
            : null);
        setCurrentId(resume);
        remember(resume);
      } catch (failure) {
        setError(
          failure instanceof ApiFailure
            ? failure.message
            : "Your projects could not be loaded.",
        );
      } finally {
        setLoading(false);
        setReady(true);
      }
    },
    [client, remember],
  );

  useEffect(() => {
    void load(options.initialProjectId == null);
  }, [load, options.initialProjectId]);

  const open = useCallback(
    async (projectId: string) => {
      // Optimistic: the workspace can start loading while the server records the visit.
      setCurrentId(projectId);
      remember(projectId);
      try {
        const project = await client.openProject(projectId);
        setProjects((existing) =>
          existing.map((entry) => (entry.project_id === project.project_id ? project : entry)),
        );
        setError(null);
      } catch (failure) {
        setError(
          failure instanceof ApiFailure ? failure.message : "That project could not be opened.",
        );
      }
    },
    [client, remember],
  );

  const create = useCallback(
    async (displayName: string) => {
      const name = displayName.trim();
      if (!name) return null;
      try {
        const project = await client.createProject(name);
        setProjects((existing) => [project, ...existing]);
        setCurrentId(project.project_id);
        remember(project.project_id);
        setError(null);
        return project;
      } catch (failure) {
        setError(
          failure instanceof ApiFailure ? failure.message : "That project could not be created.",
        );
        return null;
      }
    },
    [client, remember],
  );

  const rename = useCallback(
    async (projectId: string, displayName: string) => {
      const name = displayName.trim();
      if (!name) return;
      try {
        const project = await client.renameProject(projectId, name);
        setProjects((existing) =>
          existing.map((entry) => (entry.project_id === projectId ? project : entry)),
        );
        setError(null);
      } catch (failure) {
        setError(
          failure instanceof ApiFailure ? failure.message : "That project could not be renamed.",
        );
      }
    },
    [client],
  );

  const close = useCallback(() => {
    setCurrentId(null);
    remember(null);
  }, [remember]);

  return {
    projects,
    current: projects.find((project) => project.project_id === currentId) ?? null,
    loading,
    ready,
    error,
    open,
    create,
    rename,
    close,
    refresh: () => load(false),
  };
}
