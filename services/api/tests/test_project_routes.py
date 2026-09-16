"""Creating, listing, opening and renaming projects over HTTP.

The studio opens on the project the user was last in, so these routes carry the answer to
"what should I show first". They also decide what a project id IS, which makes them a
security boundary as much as a convenience.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402

from studio_api.app import create_app  # noqa: E402
from studio_api.dependencies import build_dependencies  # noqa: E402
from studio_api.settings import Settings  # noqa: E402
from studio_api.storage import StudioDatabase  # noqa: E402

SEED = "proj_seed"


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        environment="local",
        worker_token="project-routes-token-not-committed",
        project_ids=(SEED,),
        reference_root=str(tmp_path / "references"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    dependencies = build_dependencies(settings, database=StudioDatabase(Path(":memory:")))
    with TestClient(create_app(settings, dependencies)) as running:
        yield running


def _create(client: Any, name: str) -> dict[str, Any]:
    response = client.post("/api/projects", json={"display_name": name})
    assert response.status_code == 201, response.text
    return response.json()["project"]


# --- listing --------------------------------------------------------------


def test_the_configured_project_appears_even_before_it_is_opened(client: Any):
    """A fresh install must not show an empty studio when a project exists."""
    body = client.get("/api/projects").json()
    assert [project["project_id"] for project in body["projects"]] == [SEED]
    assert body["last_opened_project_id"] is None


def test_a_project_lists_enough_to_describe_it_without_opening_it(client: Any):
    project = _create(client, "Beach House Kitchen")
    assert project["display_name"] == "Beach House Kitchen"
    assert project["reference_count"] == 0
    assert project["message_count"] == 0
    assert project["has_model"] is False
    # And nothing about the machine it lives on.
    assert "path" not in project
    assert "/" not in repr(project)


def test_creating_a_project_makes_it_the_one_to_reopen(client: Any):
    created = _create(client, "Loft Conversion")
    body = client.get("/api/projects").json()

    assert body["last_opened_project_id"] == created["project_id"]
    # Newest first, so the project just made is at the top.
    assert body["projects"][0]["project_id"] == created["project_id"]


def test_opening_a_project_changes_which_one_reopens(client: Any):
    first = _create(client, "First")
    second = _create(client, "Second")
    assert client.get("/api/projects").json()["last_opened_project_id"] == second[
        "project_id"
    ]

    response = client.post(f"/api/projects/{first['project_id']}/open")
    assert response.status_code == 200, response.text
    assert response.json()["project"]["last_opened_at"]

    body = client.get("/api/projects").json()
    assert body["last_opened_project_id"] == first["project_id"]
    assert body["projects"][0]["project_id"] == first["project_id"]


def test_the_seed_project_can_be_opened_and_then_reopens(client: Any):
    response = client.post(f"/api/projects/{SEED}/open")
    assert response.status_code == 200, response.text
    assert client.get("/api/projects").json()["last_opened_project_id"] == SEED


# --- naming ---------------------------------------------------------------


def test_a_project_can_be_renamed_without_changing_its_id(client: Any):
    project = _create(client, "Untitled")
    response = client.post(
        f"/api/projects/{project['project_id']}/rename",
        json={"display_name": "Kitchen Refit"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["project"]["display_name"] == "Kitchen Refit"
    assert response.json()["project"]["project_id"] == project["project_id"]


def test_a_display_name_is_tidied_but_not_used_as_a_path(client: Any):
    project = _create(client, "   ../../etc/passwd   ")
    # Kept as a LABEL, harmless because it is never used to locate anything...
    assert project["display_name"] == "../../etc/passwd"
    # ...and the id, which IS used, is generated and safe.
    assert project["project_id"].startswith("proj_")
    assert "/" not in project["project_id"]
    assert ".." not in project["project_id"]


def test_a_blank_display_name_is_refused(client: Any):
    assert client.post("/api/projects", json={"display_name": "   "}).status_code == 422
    assert client.post("/api/projects", json={}).status_code == 422


def test_an_unexpected_field_is_refused(client: Any):
    response = client.post(
        "/api/projects", json={"display_name": "X", "project_id": "proj_chosen"}
    )
    assert response.status_code == 422, response.text


def test_a_client_cannot_choose_its_own_project_id(client: Any):
    """Ids are generated. A client naming one is how an id becomes a path."""
    first = _create(client, "One")
    second = _create(client, "One")
    assert first["project_id"] != second["project_id"]


# --- unknown projects -----------------------------------------------------


@pytest.mark.parametrize("hostile", ["proj_nope", "..", ".", "~", "proj%20x"])
def test_opening_an_unknown_or_hostile_project_is_refused(client: Any, hostile: str):
    response = client.post(f"/api/projects/{hostile}/open")
    assert response.status_code in (404, 405, 422), response.status_code
    # A traversal attempt collapses to a URL with no route at all, which answers with
    # the framework's own 404. Either way nothing is created and nothing is opened.
    body = response.json()
    if isinstance(body, dict) and "error" in body:
        assert body["error"]["code"] == "VALIDATION_ERROR"
    listed = client.get("/api/projects").json()
    assert listed["last_opened_project_id"] is None
    assert [project["project_id"] for project in listed["projects"]] == [SEED]


def test_renaming_an_unknown_project_is_refused(client: Any):
    response = client.post(
        "/api/projects/proj_nope/rename", json={"display_name": "New"}
    )
    assert response.status_code == 404, response.text


# --- the rest of the workspace still works --------------------------------


def test_a_created_project_is_immediately_usable_by_the_workspace_routes(client: Any):
    """Creating a project must not require a restart or a worker."""
    project = _create(client, "Studio Flat")
    project_id = project["project_id"]

    workspace = client.get(f"/api/projects/{project_id}/workspace")
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["project"]["display_name"] == "Studio Flat"
    assert workspace.json()["references"] == []
    assert workspace.json()["scene"] is None

    references = client.get(f"/api/projects/{project_id}/references")
    assert references.status_code == 200, references.text


def test_project_scoped_routes_still_refuse_an_unknown_project(client: Any):
    response = client.get("/api/projects/proj_nope/workspace")
    assert response.status_code == 404, response.text
