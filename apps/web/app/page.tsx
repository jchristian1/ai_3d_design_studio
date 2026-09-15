import { WorkspaceShell } from "../components/WorkspaceShell.tsx";

/**
 * The studio.
 *
 * Spec 001 has exactly one project, so there is no project picker yet and this
 * route is the whole application. When multiple projects exist this becomes
 * `/projects/[projectId]`, and the shell takes its project from the route rather
 * than from configuration.
 */
export default function Page() {
  return <WorkspaceShell />;
}
