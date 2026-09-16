"use client";

/**
 * The application: either a project, or the screen for choosing one.
 *
 *   projects still loading  ->  nothing (a flash of the wrong screen is worse)
 *   a project to resume     ->  the workspace, on that project
 *   otherwise               ->  the project screen
 *
 * The workspace is keyed by project id, so switching projects remounts it. That is
 * deliberate: a workspace holds a conversation, a selection, attachments and a tracked
 * job, and none of it means anything in a different project. Remounting is how "switch
 * project" cannot leak state, without every reducer having to remember to reset.
 */

import { useEffect, useState } from "react";

import { useProjects, type UseProjectsOptions } from "../hooks/useProjects.ts";
import type { ConnectionStatusView, WorkspaceClient } from "../lib/api/workspace.ts";
import { workspaceClient as defaultClient } from "../lib/api/workspace.ts";
import type { UseWorkspaceOptions } from "../hooks/useWorkspace.ts";
import { ProjectHome } from "./ProjectHome.tsx";
import { ProjectSwitcher } from "./ProjectSwitcher.tsx";
import { WorkspaceShell } from "./WorkspaceShell.tsx";

export interface StudioAppProps {
  /** Injected by tests; the app uses the real clients. */
  projectOptions?: UseProjectsOptions;
  sessionOptions?: Omit<UseWorkspaceOptions, "projectId">;
  client?: WorkspaceClient;
}

export function StudioApp({ projectOptions, sessionOptions, client }: StudioAppProps) {
  const workspaceApi = client ?? projectOptions?.client ?? defaultClient;
  const projects = useProjects({ client: workspaceApi, ...projectOptions });

  // Status is shown on the project screen too: knowing Astra needs a sign-in BEFORE
  // opening a project saves a confusing first message.
  const [astra, setAstra] = useState<ConnectionStatusView | null>(null);
  const [blender, setBlender] = useState<ConnectionStatusView | null>(null);

  useEffect(() => {
    if (projects.current) return;
    let cancelled = false;
    void (async () => {
      const [nextAstra, nextBlender] = await Promise.all([
        workspaceApi.getAstraStatus().catch(() => null),
        workspaceApi.getBlenderStatus().catch(() => null),
      ]);
      if (cancelled) return;
      if (nextAstra) setAstra(nextAstra);
      if (nextBlender) setBlender(nextBlender);
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceApi, projects.current]);

  if (!projects.ready) return <div aria-busy="true" />;

  if (!projects.current) {
    return (
      <ProjectHome
        projects={projects.projects}
        loading={projects.loading}
        error={projects.error}
        onOpen={(projectId) => void projects.open(projectId)}
        onCreate={(name) => void projects.create(name)}
        astraLabel={astra?.label ?? "Astra"}
        astraConnected={astra?.connected ?? false}
        blenderConnected={blender?.connected ?? false}
      />
    );
  }

  const projectId = projects.current.project_id;
  return (
    <WorkspaceShell
      key={projectId}
      projectName={projects.current.display_name}
      sessionOptions={{ ...sessionOptions, client: workspaceApi, projectId }}
      projectControl={
        <ProjectSwitcher
          current={projects.current}
          projects={projects.projects}
          onOpen={(id) => void projects.open(id)}
          onRename={(id, name) => void projects.rename(id, name)}
          onClose={projects.close}
        />
      }
    />
  );
}
