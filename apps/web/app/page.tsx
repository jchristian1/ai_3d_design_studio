import { StudioApp } from "../components/StudioApp.tsx";

/**
 * The studio.
 *
 * One route on purpose. Which project is open is application state, not a URL: the studio
 * reopens what you were working on, and a project id in the address bar would be a
 * technical detail the user never asked to see. When projects become shareable that
 * changes, and this becomes `/projects/[projectId]`.
 */
export default function Page() {
  return <StudioApp />;
}
