"""Creating, listing, opening and renaming projects.

The studio opens on the project the user was last working in, so "which project" has to
be a first-class question the browser can ask and answer without the user thinking about
identifiers. These routes are what the project home screen and the project switcher talk
to.

Every response is built from ``ProjectRecord.snapshot()``, which carries no path, no
Blender detail and no host information — the same discipline as the rest of the workspace
surface.
"""

from __future__ import annotations

import logging

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import errors
from ..identity import TrustedIdentity
from ..projects import clean_display_name
from .support import ControlPlaneHTTPError, get_dependencies, get_identity

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S", max_length=200)]


class CreateProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: NonBlank


class RenameProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: NonBlank


def _unknown_project() -> ControlPlaneHTTPError:
    failure = errors.failure(
        errors.UNKNOWN_PROJECT, "VALIDATION_ERROR", "That project does not exist."
    )
    return ControlPlaneHTTPError(failure.http_status, failure.body())


class DeleteProjectBody(BaseModel):
    """Deleting a project is irreversible, so the user types its name to confirm.

    A typed name is the right gate here: it cannot be produced by a stray click, a double
    submit, or a script that guessed a project id, and it forces the person to look at WHICH
    project they are about to lose. The server checks it too — a browser that forgot to ask
    must not be able to delete anything.
    """

    model_config = ConfigDict(extra="forbid")

    confirm_display_name: NonBlank


def _view(dependencies: Any, record: Any) -> dict[str, Any]:
    """One project, as the browser shows it in a list."""
    project_id = record.project_id
    repositories = dependencies.repositories
    return {
        **record.snapshot(),
        # Enough to describe a project at a glance without opening it.
        "reference_count": len(repositories.references.list_for_project(project_id)),
        "message_count": repositories.conversation.count(project_id),
        "has_model": repositories.artifacts.latest(project_id, "model_glb") is not None,
    }


@router.get("")
def list_projects(
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Every project, newest activity first, plus which one to reopen."""
    dependencies = get_dependencies(request)
    repositories = dependencies.repositories

    # Adopt any configured-but-unused project so the list is complete on a fresh install.
    for project_id in dependencies.projects.known_project_ids():
        dependencies.projects.get(project_id)

    records = list(repositories.projects.list_all())
    records.sort(
        key=lambda record: (record.last_opened_at or "", record.updated_at),
        reverse=True,
    )
    last = repositories.projects.most_recently_opened()
    return {
        "projects": [_view(dependencies, record) for record in records],
        "last_opened_project_id": last.project_id if last else None,
    }


@router.post("", status_code=201)
def create_project(
    body: CreateProjectBody,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Make a new project. Its Blender file is created on first use by the worker."""
    dependencies = get_dependencies(request)
    creator = getattr(dependencies.projects, "create", None)
    if creator is None:
        failure = errors.failure(
            errors.INVALID_REQUEST,
            "VALIDATION_ERROR",
            "This studio is configured with a fixed set of projects.",
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())

    project = creator(clean_display_name(body.display_name))
    dependencies.repositories.projects.mark_opened(project.project_id)
    record = dependencies.repositories.projects.get(project.project_id)
    return {"project": _view(dependencies, record)}


@router.post("/{project_id}/open")
def open_project(
    project_id: str,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Record that this is the project the user is working in."""
    dependencies = get_dependencies(request)
    project = dependencies.projects.get(project_id)
    if project is None:
        raise _unknown_project()
    dependencies.repositories.projects.ensure(project.project_id, project.display_name)
    dependencies.repositories.projects.mark_opened(project.project_id)
    record = dependencies.repositories.projects.get(project.project_id)
    return {"project": _view(dependencies, record)}


@router.post("/{project_id}/rename")
def rename_project(
    project_id: str,
    body: RenameProjectBody,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Rename a project.

    Deliberately ``/{project_id}/rename`` rather than ``PUT /{project_id}``: a bare
    ``/{project_id}`` route would give a URL that path traversal collapses onto
    (``/api/projects/p/artifacts/..``), turning a clean 404 for a hostile identifier into
    a 405. Nothing would leak either way, but the guarantee is easier to keep when the
    namespace has no bare segment route at all.
    """
    dependencies = get_dependencies(request)
    project = dependencies.projects.get(project_id)
    if project is None:
        raise _unknown_project()
    dependencies.repositories.projects.ensure(project.project_id, project.display_name)
    record = dependencies.repositories.projects.rename(
        project_id, clean_display_name(body.display_name)
    )
    if record is None:  # pragma: no cover - ensured above
        raise _unknown_project()
    return {"project": _view(dependencies, record)}



@router.post("/{project_id}/delete")
def delete_project(
    project_id: str,
    body: DeleteProjectBody,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Delete a project, its uploads, its conversation and its artifacts.

    Irreversible, and gated on the user typing the project's name. The check happens HERE
    rather than only in the browser, so the guarantee does not depend on the interface.

    ``POST /delete`` rather than ``DELETE /{project_id}``: a body is required (the typed
    name), and a bare ``/{project_id}`` route is deliberately absent from this namespace —
    it gives path traversal a URL to collapse onto, which turns a clean 404 for a hostile
    artifact id into a 405.

    One thing this does NOT remove: the ``.blend`` file on the design machine. The control
    plane does not know worker paths — that is the boundary that keeps a job from carrying a
    filesystem path — so the file is left behind. It is inert: project ids are generated, so
    nothing will ever resolve to it again. Reclaiming it is a worker-side task.
    """
    dependencies = get_dependencies(request)
    project = dependencies.projects.get(project_id)
    if project is None:
        raise _unknown_project()

    typed = " ".join(body.confirm_display_name.split())
    expected = " ".join(project.display_name.split())
    if typed != expected:
        failure = errors.failure(
            errors.INVALID_REQUEST,
            "VALIDATION_ERROR",
            "That name does not match this project, so nothing was deleted.",
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())

    repositories = dependencies.repositories
    artifacts = repositories.artifacts.list_for_project(project_id)

    deletion = repositories.delete_project(project_id)
    if deletion is None:  # pragma: no cover - checked above
        raise _unknown_project()

    # Bytes last, and never in a way that can fail the request: the database no longer
    # references any of this, so a file that resists deletion is wasted disk rather than a
    # project that half exists. Each store removes its own files — the control plane does no
    # filesystem work of its own, which a layering test enforces.
    try:
        dependencies.reference_files.delete_project(project_id)
    except Exception as error:  # pragma: no cover - defensive
        _log.warning("could not delete the uploads for %s: %s", project_id, error)

    for artifact in artifacts:
        try:
            dependencies.artifacts.delete(project_id, artifact["artifact_id"])
        except Exception as error:  # pragma: no cover - defensive
            _log.warning("could not delete an artifact for %s: %s", project_id, error)

    _log.info(
        "deleted project %s (%s references, %s messages, %s artifacts)",
        project_id,
        deletion.references,
        deletion.messages,
        deletion.artifacts,
    )
    return deletion.snapshot()
