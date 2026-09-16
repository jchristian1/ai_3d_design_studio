"""Opening a project that does not have a file yet.

Spec 002. A user creating a project in the browser cannot be expected to place a
``.blend`` on the workstation first, so the worker creates one the first time a project
is used. It stays a SECURITY boundary in exactly the way
:mod:`blender_worker.registry` is:

* the id is validated as a single, non-traversing path segment;
* the path is derived by the WORKER from its own root — a job still never carries one;
* the result is re-checked for containment inside that root after resolution.

What changes compared to ``MappingProjectRegistry`` is only the allow-list. That registry
refuses any id it was not told about at startup, which is right for a fixed set of
projects and wrong for a studio where the user makes new ones. Authorisation moved to the
control plane, which is the component that knows which projects exist and who owns them;
the worker's job is to locate and, when asked for a project it has never seen, create.

    control plane: "may this user use proj_8d83f?"   (authorisation)
    worker:        "where does proj_8d83f live?"     (location)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional

from .registry import (
    ProjectResolutionError,
    UnsafeProjectIdError,
    assert_safe_project_id,
)

_log = logging.getLogger(__name__)

PROJECT_SUFFIX = ".blend"

RESULT_PREFIX = "STUDIO_RESULT:"

_SCRIPT = Path(__file__).resolve().parent / "blender_scripts" / "create_project.py"


class ProjectProvisioningError(ProjectResolutionError):
    """A new project file could not be created."""


def create_empty_project(destination: Path, timeout_seconds: int = 300) -> None:
    """Save an empty, metric ``.blend`` at ``destination`` using headless Blender."""
    from blender_mcp.blender_runtime import find_blender_executable, run_blender_script

    if find_blender_executable() is None:
        raise ProjectProvisioningError(
            "Blender is not available on this machine, so a new project cannot be created"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temporary name and move into place, so an interrupted creation cannot
    # leave a half-written project that later looks like a real one.
    handle, staged_name = tempfile.mkstemp(
        prefix=".creating_", suffix=PROJECT_SUFFIX, dir=str(destination.parent)
    )
    os.close(handle)
    staged = Path(staged_name)
    try:
        completed = run_blender_script(
            str(_SCRIPT),
            env={"STUDIO_PROJECT_PATH": str(staged)},
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            raise ProjectProvisioningError(
                f"Blender could not create the project: {completed.stderr.strip()[-400:]}"
            )
        payload = _parse(completed.stdout)
        if payload.get("error"):
            raise ProjectProvisioningError(str(payload["error"]))
        if not staged.exists() or staged.stat().st_size == 0:
            raise ProjectProvisioningError("Blender reported success but wrote no file")
        staged.replace(destination)
    finally:
        if staged.exists():
            staged.unlink(missing_ok=True)


def _parse(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                return json.loads(line[len(RESULT_PREFIX) :])
            except json.JSONDecodeError as error:
                raise ProjectProvisioningError(
                    f"Blender returned unreadable output: {error}"
                ) from error
    raise ProjectProvisioningError("Blender returned no result")


class ProvisioningProjectRegistry:
    """Locates ``<root>/<project_id>.blend``, creating it the first time.

    Satisfies ``ProjectLocator``, so the executors are unchanged.
    """

    def __init__(
        self,
        root: Path,
        *,
        create: Optional[Callable[[Path], None]] = None,
        provision: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self._create = create or create_empty_project
        #: When False the registry behaves like a read-only locator: an unknown project
        #: is refused rather than created. Used where creation would be surprising.
        self.provision = provision

    # -- introspection -----------------------------------------------------
    def known_project_ids(self) -> tuple[str, ...]:
        if not self.root.exists():
            return ()
        return tuple(
            sorted(
                path.stem
                for path in self.root.glob(f"*{PROJECT_SUFFIX}")
                if path.is_file()
            )
        )

    def path_of(self, project_id: str) -> Path:
        """The path this registry WOULD use, without touching the filesystem."""
        safe_id = assert_safe_project_id(project_id)
        candidate = (self.root / f"{safe_id}{PROJECT_SUFFIX}").resolve()
        if not self._within_root(candidate):
            raise UnsafeProjectIdError(
                f"project {safe_id!r} would resolve outside the allowed root"
            )
        return candidate

    def _within_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    # -- ProjectLocator ----------------------------------------------------
    def blend_path_for(self, project_id: str) -> Path:
        path = self.path_of(project_id)
        if path.exists():
            return path
        if not self.provision:
            raise ProjectResolutionError(
                f"project {project_id!r} has no project file at {path}"
            )
        _log.info("creating a new project file for %s", project_id)
        self._create(path)
        if not path.exists():
            raise ProjectProvisioningError(
                f"project {project_id!r} still has no file after creation"
            )
        return path


__all__ = [
    "PROJECT_SUFFIX",
    "ProjectProvisioningError",
    "ProvisioningProjectRegistry",
    "create_empty_project",
]
