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
import type {
  ConnectionStatusView,
  ProjectSummaryView,
  WorkspaceClient,
} from "../lib/api/workspace.ts";
import { workspaceClient as defaultClient } from "../lib/api/workspace.ts";
import type { UseWorkspaceOptions } from "../hooks/useWorkspace.ts";
import { DeleteProjectDialog } from "./DeleteProjectDialog.tsx";
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
  //: The project a delete dialog is open for. Deletion is irreversible, so it is always
  //: behind this dialog — there is no path to it that does not involve typing the name.
  const [deleting, setDeleting] = useState<ProjectSummaryView | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

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

  const dialog = deleting ? (
    <DeleteProjectDialog
      project={deleting}
      busy={deleteBusy}
      // While the dialog is open it owns the error: a refused deletion has to be read
      // where the decision is being made, and announcing it twice makes a screen reader
      // repeat itself for no reason. The screen behind it stays quiet until it closes.
      error={projects.error}
      onCancel={() => {
        setDeleting(null);
        // A refused deletion's message belongs to that attempt. Without this it would
        // reappear on the project screen after cancelling, attached to nothing.
        projects.clearError();
      }}
      onConfirm={(typedName) => {
        setDeleteBusy(true);
        void projects.remove(deleting.project_id, typedName).then((done) => {
          setDeleteBusy(false);
          if (done) setDeleting(null);
        });
      }}
    />
  ) : null;

  if (!projects.ready) return <div aria-busy="true" />;

  if (!projects.current) {
    return (
      <>
        <ProjectHome
          projects={projects.projects}
          loading={projects.loading}
          error={deleting ? null : projects.error}
          onOpen={(projectId) => void projects.open(projectId)}
          onCreate={(name) => void projects.create(name)}
          onDelete={(project) => {
            projects.clearError();
            setDeleting(project);
          }}
          astraLabel={astra?.label ?? "Astra"}
          astraConnected={astra?.connected ?? false}
          blenderConnected={blender?.connected ?? false}
        />
        {dialog}
      </>
    );
  }

  const projectId = projects.current.project_id;
  const current = projects.current;
  return (
    <>
      <WorkspaceShell
        key={projectId}
        projectName={current.display_name}
        sessionOptions={{ ...sessionOptions, client: workspaceApi, projectId }}
        projectControl={
          <ProjectSwitcher
            current={current}
            projects={projects.projects}
            onOpen={(id) => void projects.open(id)}
            onRename={(id, name) => void projects.rename(id, name)}
            onDelete={() => {
              projects.clearError();
              setDeleting(current);
            }}
            onClose={projects.close}
          />
        }
      />
      {dialog}
    </>
  );
}
