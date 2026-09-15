"""Resolving project_id to a trusted .blend path.

Spec 001, Task 6. This is a security boundary.

A Job must never carry or influence a filesystem path. The canonical Job schema
has no path field at all and declares ``additionalProperties: false``, so a path
cannot even be smuggled in. This module closes the remaining gap: the worker maps
``project_id`` to a location it chose itself.

Two independent defences:

  1. ``project_id`` must be a safe single path segment. A value like
     ``../../etc/passwd`` is rejected before it touches the filesystem.
  2. The resolved path must sit inside the registry's allowed root, verified
     after symlink resolution. A registry entry that escapes the root is refused
     even if the worker itself configured it wrongly.

An unknown project_id is refused rather than guessed at — the worker never
searches the filesystem for a plausible project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Protocol


class ProjectResolutionError(RuntimeError):
    """The project_id could not be resolved to a trusted project path."""


class UnknownProjectError(ProjectResolutionError):
    """No project is registered under that project_id."""


class UnsafeProjectIdError(ProjectResolutionError):
    """The project_id is not a usable, safe identifier."""


class ProjectLocator(Protocol):
    """Maps a project_id to the authoritative .blend for that project."""

    def blend_path_for(self, project_id: str) -> Path:
        """Return the trusted path, or raise ProjectResolutionError."""
        ...


def assert_safe_project_id(project_id: object) -> str:
    """Validate a project_id as a safe single path segment."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise UnsafeProjectIdError("project_id must be a non-blank string")
    if project_id in (".", ".."):
        raise UnsafeProjectIdError(f"project_id {project_id!r} is not allowed")
    if project_id != Path(project_id).name:
        raise UnsafeProjectIdError(
            f"project_id {project_id!r} must be a single path segment"
        )
    if any(char in project_id for char in ("/", "\\", "\0")):
        raise UnsafeProjectIdError(
            f"project_id {project_id!r} contains a path separator"
        )
    return project_id


class MappingProjectRegistry:
    """An explicit, worker-controlled project_id -> .blend mapping.

    Sufficient for Spec 001. A database-backed registry can replace it without
    changing WorkerExecutor, since both satisfy ProjectLocator.
    """

    def __init__(self, root: Path, mapping: Optional[Mapping[str, Path]] = None):
        self.root = Path(root).resolve()
        self._mapping: dict[str, Path] = {}
        for project_id, path in (mapping or {}).items():
            self.register(project_id, path)

    def register(self, project_id: str, blend_path: Path) -> Path:
        """Register a project, enforcing that it lives inside the allowed root."""
        assert_safe_project_id(project_id)
        resolved = Path(blend_path).resolve()
        if not self._within_root(resolved):
            raise ProjectResolutionError(
                f"project path {resolved} is outside the allowed root {self.root}"
            )
        self._mapping[project_id] = resolved
        return resolved

    def _within_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    def known_project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._mapping))

    # -- ProjectLocator ----------------------------------------------------

    def blend_path_for(self, project_id: str) -> Path:
        safe_id = assert_safe_project_id(project_id)
        path = self._mapping.get(safe_id)
        if path is None:
            raise UnknownProjectError(f"no project registered as {safe_id!r}")

        # Re-check containment at resolution time: a symlink could have been
        # swapped underneath a previously valid entry.
        resolved = path.resolve()
        if not self._within_root(resolved):
            raise ProjectResolutionError(
                f"project {safe_id!r} resolves outside the allowed root"
            )
        if not resolved.exists():
            raise ProjectResolutionError(
                f"project {safe_id!r} has no project file at {resolved}"
            )
        return resolved
