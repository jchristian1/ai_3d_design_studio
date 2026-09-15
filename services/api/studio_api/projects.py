"""The control plane's view of a project.

Spec 001, Task 9. This is a security boundary.

WHAT THE CONTROL PLANE KNOWS
----------------------------
A LOGICAL project id, and nothing else::

    proj_seed  ->  ControlPlaneProject(project_id="proj_seed", display_name="Seed Project")

It deliberately does NOT know, store, or accept a filesystem path. Path resolution
is the worker's job and happens on the machine that owns the file
(``blender_worker.registry.MappingProjectRegistry``). The two registries are
separate on purpose: the control plane authorizes a project, the worker locates it.

    browser  --project_id-->  control plane  --project_id-->  worker  --path-->  .blend
                              (no paths)                     (owns paths)

Three independent defences against a client naming a file:

  1. The canonical ChatRequest schema has no path field and closes the object, so
     a path cannot be sent.
  2. ``project_id`` must be a safe single path segment — ``../../etc/passwd`` is
     rejected here, before anything downstream sees it.
  3. The id must be present in an explicit allow-list. An unknown project is
     refused, never guessed at, and never used to search anything.

Deliberate duplication
----------------------
``is_safe_project_id`` restates a check that also exists in the worker's registry.
That is not an oversight: these are two different processes on two different trust
boundaries, and the API must not import worker internals to validate its own
input. Each boundary validates for itself.

Spec 001 uses an in-memory allow-list from configuration. A PostgreSQL-backed
registry with ownership and permissions replaces it later by satisfying
``ProjectRegistry`` — no route or service changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, runtime_checkable

#: Characters that must never appear in a project id, because they are the
#: building blocks of a path traversal.
_FORBIDDEN_CHARACTERS = ("/", "\\", "\0", ":")


@dataclass(frozen=True)
class ControlPlaneProject:
    """What the control plane records about a project.

    Note what is absent: any path, any Blender detail, any host information.
    """

    project_id: str
    display_name: str

    def snapshot(self) -> dict[str, str]:
        """A safe, serializable view. Cannot contain a path by construction."""
        return {"project_id": self.project_id, "display_name": self.display_name}


def is_safe_project_id(project_id: object) -> bool:
    """True when the value is usable as a single, non-traversing identifier."""
    if not isinstance(project_id, str):
        return False
    candidate = project_id.strip()
    if not candidate or candidate != project_id:
        return False
    if candidate in (".", ".."):
        return False
    if any(char in candidate for char in _FORBIDDEN_CHARACTERS):
        return False
    # Reject anything that looks like it addresses the filesystem.
    if candidate.startswith((".", "~")):
        return False
    return True


@runtime_checkable
class ProjectRegistry(Protocol):
    """Resolves a client-supplied project_id to a project the caller may use."""

    def get(self, project_id: str) -> Optional[ControlPlaneProject]:
        """Return the project, or None when it is unknown or unsafe."""
        ...

    def known_project_ids(self) -> tuple[str, ...]: ...


class InMemoryProjectRegistry:
    """An explicit allow-list of logical project ids.

    Sufficient for Spec 001, which has exactly one project: the seed fixture.
    """

    def __init__(self, projects: Iterable[ControlPlaneProject] = ()) -> None:
        self._projects: dict[str, ControlPlaneProject] = {}
        for project in projects:
            self.register(project)

    def register(self, project: ControlPlaneProject) -> ControlPlaneProject:
        if not is_safe_project_id(project.project_id):
            raise ValueError(
                f"refusing to register unsafe project_id {project.project_id!r}"
            )
        self._projects[project.project_id] = project
        return project

    # -- ProjectRegistry ---------------------------------------------------

    def get(self, project_id: str) -> Optional[ControlPlaneProject]:
        # The safety check runs BEFORE the lookup, so a traversal attempt is
        # rejected on its own merits rather than incidentally missing the map.
        if not is_safe_project_id(project_id):
            return None
        return self._projects.get(project_id)

    def known_project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._projects))


def _display_name_for(project_id: str) -> str:
    """A readable default label derived from the id."""
    stripped = project_id[5:] if project_id.startswith("proj_") else project_id
    return stripped.replace("_", " ").replace("-", " ").strip().title() or project_id


def registry_from_ids(project_ids: Iterable[str]) -> InMemoryProjectRegistry:
    """Build the Spec 001 registry from configured logical ids."""
    return InMemoryProjectRegistry(
        ControlPlaneProject(
            project_id=project_id, display_name=_display_name_for(project_id)
        )
        for project_id in project_ids
    )


__all__ = [
    "ControlPlaneProject",
    "InMemoryProjectRegistry",
    "ProjectRegistry",
    "is_safe_project_id",
    "registry_from_ids",
]
