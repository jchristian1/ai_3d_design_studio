"""The design workspace HTTP surface.

Every route is project-scoped, and every response is built from a ``snapshot()`` that
was designed to be browser-safe: no filesystem path, no stored filename, no hash, no
token. That is a property of the record types rather than of these handlers, so a new
route cannot leak by forgetting to filter.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from .. import errors
from ..design_chat import DesignChatRequest
from ..identity import TrustedIdentity
from ..ingest import SUPPORTED_DESCRIPTION, UploadRejected
from ..storage import ReferenceStorageError
from ..turns import thinking_snapshot
from .support import ControlPlaneHTTPError, get_dependencies, get_identity

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["workspace"])

#: Refuse an upload that would take an unreasonable amount of memory to buffer. The
#: per-kind limits in the ingest layer are the real policy; this is a coarse guard so a
#: hostile request cannot make us read gigabytes before that policy runs.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class DesignChatBody(BaseModel):
    """One message from the browser."""

    model_config = ConfigDict(extra="forbid")

    request_id: NonBlank
    session_id: NonBlank
    message: NonBlank
    #: References the user explicitly attached to this message.
    attached_reference_ids: list[str] = Field(default_factory=list)
    #: What the user has selected in the 3D viewer.
    selected_object_id: Optional[str] = None


class ApprovalDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    session_id: Optional[str] = None


class FactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: NonBlank
    value: NonBlank


def _require_project(dependencies: Any, project_id: str) -> Any:
    project = dependencies.projects.get(project_id)
    if project is None:
        failure = errors.failure(
            errors.UNKNOWN_PROJECT, "VALIDATION_ERROR", "That project does not exist."
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())
    dependencies.repositories.projects.ensure(project.project_id, project.display_name)
    return project


def _rejected(message: str) -> ControlPlaneHTTPError:
    failure = errors.failure(errors.INVALID_REQUEST, "VALIDATION_ERROR", message)
    return ControlPlaneHTTPError(failure.http_status, failure.body())


# --- references -----------------------------------------------------------


@router.get("/{project_id}/references")
def list_references(
    project_id: str,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)
    references = dependencies.repositories.references.list_for_project(project_id)
    return {
        "project_id": project_id,
        "references": [
            {
                **reference.snapshot(),
                "pages": [
                    page.snapshot()
                    for page in dependencies.repositories.references.pages_of(
                        project_id, reference.reference_id
                    )
                ],
            }
            for reference in references
        ],
        "supported": SUPPORTED_DESCRIPTION,
    }


@router.post("/{project_id}/references", status_code=201)
async def upload_reference(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise _rejected("That file is too large to upload.")

    try:
        result = dependencies.ingest.ingest(
            project_id,
            file.filename or "reference",
            data,
            declared_media_type=file.content_type,
        )
    except UploadRejected as error:
        raise _rejected(str(error)) from error
    except ReferenceStorageError as error:
        _log.warning("reference storage refused an upload: %s", error)
        raise _rejected("That file could not be stored.") from error

    return result.snapshot()


@router.delete("/{project_id}/references/{reference_id}")
def delete_reference(
    project_id: str,
    reference_id: str,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)
    removed = dependencies.ingest.delete(project_id, reference_id)
    if not removed:
        failure = errors.failure(
            errors.UNKNOWN_PROJECT, "VALIDATION_ERROR", "That reference does not exist."
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())
    return {"project_id": project_id, "reference_id": reference_id, "deleted": True}


@router.get("/{project_id}/references/{reference_id}/content")
def reference_content(
    project_id: str,
    reference_id: str,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> Response:
    """Serve reference bytes so the browser can show thumbnails and page images."""
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)
    found = dependencies.ingest.bytes_for(project_id, reference_id)
    if found is None:
        failure = errors.failure(
            errors.UNKNOWN_PROJECT, "VALIDATION_ERROR", "That reference does not exist."
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())
    data, media_type = found
    return Response(
        content=data,
        media_type=media_type,
        headers={
            # Reference bytes never change under their id, so they cache immutably.
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            # Never render an upload inline: an SVG or HTML payload would run in the
            # page's origin. Images are displayed via <img>, which ignores this.
            "Content-Disposition": "inline; filename=\"reference\"",
        },
    )


# --- the conversation -----------------------------------------------------


@router.post("/{project_id}/design-chat", status_code=202)
def design_chat(
    project_id: str,
    body: DesignChatBody,
    request: Request,
    response: Response,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Start a turn. The answer is collected by polling, not by waiting here.

    A real model takes tens of seconds to think, and minutes to plan a floor plan. A
    browser will not hold a request open that long — and if it tries, a reload throws the
    answer away and the model's work is orphaned.
    """
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)

    chat_request = DesignChatRequest(
        request_id=body.request_id,
        project_id=project_id,
        session_id=body.session_id,
        message=body.message,
        attached_reference_ids=tuple(body.attached_reference_ids),
        selected_object_id=body.selected_object_id,
    )
    state = dependencies.turns.start(chat_request, identity)

    # A turn that finished before the first poll (a cached answer, a refusal) is returned
    # immediately: making the browser poll for something already known is pointless.
    if state.finished:
        return _finished_turn(project_id, state, response)

    return thinking_snapshot(state, poll_url=_turn_url(project_id, state.turn_id))


@router.get("/{project_id}/design-chat/{turn_id}")
def design_turn(
    project_id: str,
    turn_id: str,
    request: Request,
    response: Response,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Where a turn has got to, and its result once there is one."""
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)

    state = dependencies.turns.get(project_id, turn_id)
    if state is None:
        failure = errors.failure(
            errors.UNKNOWN_PROJECT,
            "VALIDATION_ERROR",
            "That message is no longer being tracked. Send it again.",
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())

    if not state.finished:
        return thinking_snapshot(state, poll_url=_turn_url(project_id, state.turn_id))

    return _finished_turn(project_id, state, response)


def _finished_turn(project_id: str, state: Any, response: Response) -> dict[str, Any]:
    """Render a completed turn, failures included, exactly as the old route did."""
    if state.error is not None:
        raise ControlPlaneHTTPError(
            state.error.http_status, state.error.body(request_id=state.request_id)
        )

    turn = state.result
    if turn is None:  # pragma: no cover - finished implies one or the other
        failure = errors.failure(
            errors.INTERNAL, "INTERNAL_ERROR", "That turn produced no result."
        )
        raise ControlPlaneHTTPError(failure.http_status, failure.body())

    if turn.failure is not None:
        raise ControlPlaneHTTPError(
            turn.failure.http_status, turn.failure.body(request_id=turn.request_id)
        )

    response.status_code = turn.http_status
    return {
        **turn.snapshot(status_url=_status_url(project_id, turn.job_id)),
        "turn_id": state.turn_id,
        "state": "ready",
    }


@router.post("/{project_id}/approvals/{approval_id}", status_code=202)
def decide_approval(
    project_id: str,
    approval_id: str,
    body: ApprovalDecisionBody,
    request: Request,
    response: Response,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Approve or reject one model-authored step. Rejected code never runs.

    Polled like a message: approving runs the plan, which involves Blender.
    """
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)

    state = dependencies.turns.start_decision(
        project_id,
        approval_id,
        approved=body.approved,
        identity=identity,
        session_id=body.session_id,
    )
    if state.finished:
        return _finished_turn(project_id, state, response)
    return thinking_snapshot(state, poll_url=_turn_url(project_id, state.turn_id))


def _turn_url(project_id: str, turn_id: str) -> str:
    return f"/api/projects/{project_id}/design-chat/{turn_id}"


@router.get("/{project_id}/approvals")
def list_approvals(
    project_id: str,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)
    pending = dependencies.repositories.approvals.open_for_project(project_id)
    return {
        "project_id": project_id,
        "approvals": [record.snapshot() for record in pending],
    }


# --- project state --------------------------------------------------------


@router.get("/{project_id}/workspace")
def workspace(
    project_id: str,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Everything the browser needs to restore a project on load.

    One request rather than six, because the workspace is useless until all of it has
    arrived, and a restored project should not flash through six loading states.
    """
    dependencies = get_dependencies(request)
    project = _require_project(dependencies, project_id)
    repositories = dependencies.repositories

    record = repositories.projects.get(project_id)
    clarification = repositories.clarifications.open_for_project(project_id)
    latest_model = repositories.artifacts.latest(project_id, "model_glb")
    latest_preview = repositories.artifacts.latest(project_id, "preview_image")

    return {
        "project": {
            **(record.snapshot() if record else {}),
            "display_name": project.display_name,
        },
        "references": [
            reference.snapshot()
            for reference in repositories.references.list_for_project(project_id)
        ],
        "facts": [fact.snapshot() for fact in repositories.facts.all_for_project(project_id)],
        "conversation": [
            {"role": turn.role, "text": turn.text, "created_at": turn.created_at}
            for turn in repositories.conversation.recent(project_id, limit=50)
        ],
        "clarification": clarification.snapshot() if clarification else None,
        "approvals": [
            record.snapshot() for record in repositories.approvals.open_for_project(project_id)
        ],
        "scene": repositories.scenes.get(project_id),
        "model": _artifact_view(project_id, latest_model),
        "preview": _artifact_view(project_id, latest_preview),
    }


@router.get("/{project_id}/scene")
def scene(
    project_id: str,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)
    snapshot = dependencies.repositories.scenes.get(project_id)
    return {"project_id": project_id, "scene": snapshot}


@router.get("/{project_id}/model/latest")
def latest_model(
    project_id: str,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)
    latest = dependencies.repositories.artifacts.latest(project_id, "model_glb")
    return {"project_id": project_id, "model": _artifact_view(project_id, latest)}


@router.put("/{project_id}/facts")
def set_fact(
    project_id: str,
    body: FactBody,
    request: Request,
    identity: TrustedIdentity = Depends(get_identity),
) -> dict[str, Any]:
    """Let the user correct a fact directly, without going through the agent."""
    dependencies = get_dependencies(request)
    _require_project(dependencies, project_id)
    fact = dependencies.repositories.facts.set(
        project_id, body.key.strip().lower(), body.value, source="user"
    )
    return {"project_id": project_id, "fact": fact.snapshot()}


def _artifact_view(project_id: str, row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {
        "artifact_id": row["artifact_id"],
        "artifact_type": row["artifact_type"],
        "media_type": row["media_type"],
        "created_at": row["created_at"],
        "size_bytes": row.get("size_bytes"),
        "scene_version": row.get("scene_version"),
        "url": f"/api/projects/{project_id}/artifacts/{row['artifact_id']}",
    }


def _status_url(project_id: str, job_id: Optional[str]) -> Optional[str]:
    if not job_id:
        return None
    from .jobs import job_status_path

    return job_status_path(project_id, job_id)
