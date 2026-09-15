"""Worker preview integration — no Blender (Spec 001, Task 10).

Uses the real ``WorkerExecutor`` with a fake Blender operation executor, a fake
preview generator, and a real ``LocalArtifactStore``, so the orchestration under
test is the production orchestration: only bpy and the renderer are replaced.

Covers required behaviours 1, 2, 7, 8 and 9, plus the crash/retry window.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from blender_worker import phases
from blender_worker.blender_ops import FakeBlenderOperationExecutor
from blender_worker.executor import WorkerExecutor
from blender_worker.journal import FileSystemExecutionStore
from blender_worker.locks import FileLockProvider
from blender_worker.registry import MappingProjectRegistry
from studio_contracts import to_wire
from studio_contracts.jobs import create_move_object_job
from studio_preview.artifacts import LocalArtifactStore, derive_artifact_id
from studio_preview.generator import FakePreviewGenerator, looks_like_png
from studio_types import ObjectRef, Vec3

PROJECT_ID = "proj_seed"


@pytest.fixture
def stack(tmp_path: Path):
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_path = projects_root / f"{PROJECT_ID}.blend"
    project_path.write_bytes(b"fake blend")

    blender = FakeBlenderOperationExecutor()
    blender.seed_project(project_path, {"Cube": Vec3(0.0, 0.0, 0.0)})

    runtime = tmp_path / "runtime"
    previews = FakePreviewGenerator()
    # Make the fake's output depend on the fake scene, so a moved cube really does
    # produce different bytes — the same property the real Blender test asserts.
    previews.scene_probe = lambda request: blender.position_of(
        request.project_path, "Cube"
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")

    executor = WorkerExecutor(
        store=FileSystemExecutionStore(runtime),
        locks=FileLockProvider(runtime),
        projects=MappingProjectRegistry(projects_root, {PROJECT_ID: project_path}),
        blender=blender,
        recovery_root=runtime / "recovery",
        previews=previews,
        artifacts=artifacts,
    )
    return {
        "executor": executor,
        "blender": blender,
        "previews": previews,
        "artifacts": artifacts,
        "project_path": project_path,
        "projects_root": projects_root,
        "runtime": runtime,
    }


def make_job(job_id: str = "job_1", request_id: str = "req_1", delta_x: float = 0.5):
    built = create_move_object_job(
        job_id=job_id,
        project_id=PROJECT_ID,
        session_id="sess_1",
        user_id="user_1",
        request_id=request_id,
        target=ObjectRef(name="Cube"),
        delta_meters=Vec3(delta_x, 0.0, 0.0),
        created_at="2026-09-15T04:00:00Z",
    )
    assert built.ok, built.errors
    return to_wire(built.job)


def cube_x(stack) -> float:
    return stack["blender"].position_of(stack["project_path"], "Cube").x


def move_count(stack) -> int:
    return sum(1 for kind, _ in stack["blender"].calls if kind == "move")


# ---------------------------------------------------------------------------
# 1. A successful execution requests a preview after saving
# ---------------------------------------------------------------------------


def test_1_successful_execution_generates_a_preview_after_save(stack):
    outcome = stack["executor"].execute(make_job())

    assert outcome.succeeded
    assert outcome.phase == phases.COMPLETED
    assert outcome.preview is not None
    assert outcome.preview_error is None
    assert cube_x(stack) == 0.5

    assert len(stack["previews"].calls) == 1, "exactly one preview was requested"


def test_1_preview_is_requested_after_the_mutation_not_before(stack):
    """Ordering matters: a preview depicts a SAVED change."""
    observed: list[str] = []

    original_move = stack["blender"].execute_move

    def tracking_move(project_path, plan):
        observed.append("move")
        return original_move(project_path, plan)

    stack["blender"].execute_move = tracking_move  # type: ignore[assignment]

    original_generate = stack["previews"].generate

    def tracking_generate(request):
        observed.append("preview")
        return original_generate(request)

    stack["previews"].generate = tracking_generate  # type: ignore[assignment]

    stack["executor"].execute(make_job())

    assert observed == ["move", "preview"], observed


def test_1_the_artifact_is_durable_and_retrievable(stack):
    outcome = stack["executor"].execute(make_job())
    artifact_id = outcome.preview["artifact_id"]

    stored = stack["artifacts"].get(PROJECT_ID, artifact_id)
    assert stored is not None
    data = stack["artifacts"].read_bytes(PROJECT_ID, artifact_id)
    assert data and looks_like_png(data)
    assert stored.size_bytes == len(data)
    assert stored.job_id == "job_1"


def test_1_the_journal_records_the_preview(stack):
    stack["executor"].execute(make_job())

    record = stack["executor"].store.load(PROJECT_ID, "job_1")
    assert record.phase == phases.COMPLETED
    assert record.preview is not None
    assert record.preview_error is None


def test_1_a_worker_without_a_preview_generator_still_succeeds(stack, tmp_path):
    """Preview is a capability, not a requirement."""
    executor = WorkerExecutor(
        store=FileSystemExecutionStore(tmp_path / "runtime2"),
        locks=FileLockProvider(tmp_path / "runtime2"),
        projects=MappingProjectRegistry(
            stack["projects_root"], {PROJECT_ID: stack["project_path"]}
        ),
        blender=stack["blender"],
        recovery_root=tmp_path / "runtime2" / "recovery",
        # No previews, no artifacts.
    )
    outcome = executor.execute(make_job())

    assert outcome.succeeded
    assert outcome.preview is None
    assert outcome.preview_error is None
    assert cube_x(stack) == 0.5


# ---------------------------------------------------------------------------
# 2. The generator receives a TRUSTED project path
# ---------------------------------------------------------------------------


def test_2_preview_generator_receives_the_trusted_resolved_project_path(stack):
    stack["executor"].execute(make_job())

    request = stack["previews"].calls[0]
    assert Path(request.project_path) == stack["project_path"].resolve()
    assert request.project_id == PROJECT_ID
    assert request.job_id == "job_1"


def test_2_a_job_cannot_influence_the_previewed_path(stack):
    """The path comes from the ProjectLocator, never from the job document."""
    hostile = make_job()
    # The canonical Job schema has no path field, so a hostile path can only be
    # smuggled as an unknown property — which must make the job invalid outright.
    hostile["payload"]["blend_path"] = "/etc/passwd"

    outcome = stack["executor"].execute(hostile)

    assert not outcome.succeeded
    assert outcome.error["code"] == "VALIDATION_ERROR"
    assert stack["previews"].calls == [], "no preview for a rejected job"


def test_2_an_unknown_project_never_reaches_the_preview_generator(stack):
    job = make_job()
    job["project_id"] = "proj_not_registered"

    outcome = stack["executor"].execute(job)

    assert not outcome.succeeded
    assert stack["previews"].calls == []


# ---------------------------------------------------------------------------
# 7. Preview failure does not reapply or undo the mutation
# ---------------------------------------------------------------------------


def test_7_preview_failure_leaves_the_job_succeeded(stack):
    """Option B: the mutation is durable, only the picture is missing."""
    stack["previews"].fail_with = "renderer exploded"
    stack["previews"].fail_code = "INTERNAL_ERROR"

    outcome = stack["executor"].execute(make_job())

    assert outcome.succeeded, "a failed preview must not fail a saved change"
    assert outcome.job_status == "succeeded"
    assert outcome.preview is None
    assert outcome.preview_error == {
        "code": "INTERNAL_ERROR",
        "message": "renderer exploded",
    }
    # The design change is intact and happened exactly once.
    assert cube_x(stack) == 0.5
    assert move_count(stack) == 1


def test_7_preview_failure_does_not_reapply_the_mutation_on_retry(stack):
    stack["previews"].fail_with = "renderer unavailable"
    stack["executor"].execute(make_job())
    assert cube_x(stack) == 0.5
    moves_after_first = move_count(stack)

    # Retry the same job while the preview is still broken.
    outcome = stack["executor"].execute(make_job())

    assert outcome.succeeded
    assert outcome.duplicate is True
    assert move_count(stack) == moves_after_first, "no second Blender mutation"
    assert cube_x(stack) == 0.5, "must remain 0.50, not 1.00"


def test_7_a_generator_that_raises_still_cannot_fail_the_job(stack):
    def exploding(request):
        raise RuntimeError("unexpected renderer crash")

    stack["previews"].generate = exploding  # type: ignore[assignment]

    outcome = stack["executor"].execute(make_job())

    assert outcome.succeeded
    assert outcome.preview is None
    assert outcome.preview_error["code"] == "INTERNAL_ERROR"
    assert cube_x(stack) == 0.5


def test_7_blender_unavailable_for_preview_is_reported_distinctly(stack):
    stack["previews"].fail_with = "Blender is not available on this worker"
    stack["previews"].fail_code = "BLENDER_UNAVAILABLE"

    outcome = stack["executor"].execute(make_job())

    assert outcome.succeeded
    assert outcome.preview_error["code"] == "BLENDER_UNAVAILABLE"


def test_7_a_failed_mutation_produces_no_preview(stack):
    """A picture of a change that never happened would be a lie."""
    stack["blender"].fail_move_with = "Blender crashed during the mutation"

    outcome = stack["executor"].execute(make_job())

    assert not outcome.succeeded
    assert outcome.preview is None
    assert stack["previews"].calls == [], "no preview was even attempted"


def test_7_a_store_failure_degrades_the_preview_only(stack):
    class BrokenStore:
        def get(self, project_id, artifact_id):
            return None

        def put(self, **kwargs):
            raise OSError("disk full")

    stack["executor"].artifacts = BrokenStore()

    outcome = stack["executor"].execute(make_job())

    assert outcome.succeeded
    assert outcome.preview is None
    assert outcome.preview_error["code"] == "INTERNAL_ERROR"
    assert cube_x(stack) == 0.5


# ---------------------------------------------------------------------------
# 8. A retry reuses the existing preview
# ---------------------------------------------------------------------------


def test_8_retry_of_the_same_job_reuses_the_existing_preview(stack):
    first = stack["executor"].execute(make_job())
    assert len(stack["previews"].calls) == 1

    second = stack["executor"].execute(make_job())

    assert second.duplicate is True
    assert second.preview == first.preview, "the same artifact is reported again"
    assert len(stack["previews"].calls) == 1, "no second render"
    assert len(stack["artifacts"].list_for_project(PROJECT_ID)) == 1


def test_8_many_retries_never_accumulate_artifacts(stack):
    stack["executor"].execute(make_job())
    for _ in range(5):
        stack["executor"].execute(make_job())

    assert len(stack["previews"].calls) == 1
    assert len(stack["artifacts"].list_for_project(PROJECT_ID)) == 1
    assert move_count(stack) == 1


def test_8_artifact_id_is_derived_from_the_job(stack):
    outcome = stack["executor"].execute(make_job(job_id="job_derived"))

    assert outcome.preview["artifact_id"] == derive_artifact_id(
        PROJECT_ID, "job_derived"
    )


def test_8_a_preview_lost_after_completion_is_regenerated_from_the_saved_project(
    stack,
):
    """The crash window: mutation saved, preview rendered, then artifact lost.

    Regenerating is safe and correct: it renders from the ALREADY-SAVED project, so
    it cannot move anything, and it writes the same derived artifact_id.
    """
    outcome = stack["executor"].execute(make_job())
    artifact_id = outcome.preview["artifact_id"]

    # Lose the artifact bytes, as an interrupted or cleaned-up write would.
    (stack["artifacts"].root / PROJECT_ID / f"{artifact_id}.png").unlink()
    assert stack["artifacts"].get(PROJECT_ID, artifact_id) is None
    moves_before = move_count(stack)

    retried = stack["executor"].execute(make_job())

    assert retried.succeeded
    assert retried.preview is not None
    assert retried.preview["artifact_id"] == artifact_id, "same derived identity"
    assert stack["artifacts"].get(PROJECT_ID, artifact_id) is not None
    assert len(stack["previews"].calls) == 2, "regenerated exactly once"
    assert move_count(stack) == moves_before, "Blender was NOT touched again"
    assert cube_x(stack) == 0.5


def test_8_a_preview_that_failed_first_time_is_retried_later(stack):
    """A degraded preview should not be permanent if the renderer recovers."""
    stack["previews"].fail_with = "renderer temporarily down"
    first = stack["executor"].execute(make_job())
    assert first.preview is None
    assert first.preview_error is not None

    stack["previews"].fail_with = None
    second = stack["executor"].execute(make_job())

    assert second.succeeded
    assert second.preview is not None
    assert second.preview_error is None
    assert move_count(stack) == 1, "still exactly one mutation"


# ---------------------------------------------------------------------------
# 9. A new intentional job produces a new artifact
# ---------------------------------------------------------------------------


def test_9_a_new_request_produces_a_new_artifact_and_keeps_the_old_one(stack):
    first = stack["executor"].execute(make_job("job_1", "req_1"))
    second = stack["executor"].execute(make_job("job_2", "req_2"))

    assert cube_x(stack) == 1.0, "two intentional moves"
    assert first.preview["artifact_id"] != second.preview["artifact_id"]

    # The earlier preview was NOT overwritten — this is the start of history.
    stored = stack["artifacts"].list_for_project(PROJECT_ID)
    assert len(stored) == 2
    ids = {artifact.artifact_id for artifact in stored}
    assert ids == {first.preview["artifact_id"], second.preview["artifact_id"]}

    for artifact_id in ids:
        assert stack["artifacts"].read_bytes(PROJECT_ID, artifact_id)


def test_9_previews_of_different_scenes_have_different_checksums(stack):
    first = stack["executor"].execute(make_job("job_1", "req_1"))
    second = stack["executor"].execute(make_job("job_2", "req_2"))

    assert first.preview["checksum"] != second.preview["checksum"], (
        "a visibly different scene must produce different bytes"
    )


def test_9_each_artifact_records_the_job_it_depicts(stack):
    stack["executor"].execute(make_job("job_1", "req_1"))
    stack["executor"].execute(make_job("job_2", "req_2"))

    by_job = {
        artifact.job_id: artifact
        for artifact in stack["artifacts"].list_for_project(PROJECT_ID)
    }
    assert set(by_job) == {"job_1", "job_2"}


# ---------------------------------------------------------------------------
# Layering
# ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _attributes(path: Path) -> set[str]:
    """Every attribute name accessed in a module.

    Deliberately AST-based rather than a substring search: these modules DISCUSS
    the things they must not do, at length, so matching raw text would flag the
    documentation explaining the rule. Only real code is inspected.
    """
    tree = ast.parse(path.read_text("utf-8"))
    return {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def test_the_worker_executor_contains_no_rendering_logic():
    """Rendering lives behind PreviewGenerator, not in the orchestrator."""
    executor_path = (
        Path(__file__).resolve().parents[1] / "blender_worker" / "executor.py"
    )

    assert "bpy" not in _imports(executor_path)

    attributes = _attributes(executor_path)
    # bpy-only rendering attributes. `outcome.render.engine` is deliberately NOT
    # in this list: reading the engine LABEL off a generator result is metadata
    # bookkeeping, not rendering.
    for forbidden in (
        "resolution_x",
        "resolution_y",
        "resolution_percentage",
        "film_transparent",
        "image_settings",
        "rotation_euler",
        "background_color",
        "render_aa",
        "lens",
        "ops",
    ):
        assert forbidden not in attributes, (
            f"executor touches rendering detail {forbidden!r}"
        )


def test_the_preview_package_keeps_bpy_inside_blender_scripts():
    preview_root = (
        Path(__file__).resolve().parents[3] / "services" / "preview" / "studio_preview"
    )
    for path in preview_root.rglob("*.py"):
        if "blender_scripts" in path.parts:
            continue
        assert "bpy" not in _imports(path), f"{path} imports bpy outside a script"


def test_the_preview_generator_never_saves_the_project():
    """A preview is a READ. Saving would silently edit the user's design."""
    script = (
        Path(__file__).resolve().parents[3]
        / "services"
        / "preview"
        / "studio_preview"
        / "blender_scripts"
        / "render_preview.py"
    )

    attributes = _attributes(script)
    for forbidden in ("save_as_mainfile", "save_mainfile", "save_homefile"):
        assert forbidden not in attributes, f"the preview script calls {forbidden}"

    # It must open the project and render it, and nothing more destructive.
    assert "open_mainfile" in attributes
    assert "render" in attributes


def test_blender_path_discovery_is_not_duplicated():
    """There must remain exactly one place that finds Blender."""
    preview_root = (
        Path(__file__).resolve().parents[3] / "services" / "preview" / "studio_preview"
    )
    for path in preview_root.rglob("*.py"):
        text = path.read_text("utf-8")
        for forbidden in ("/snap/bin/blender", "/usr/bin/blender", 'which("blender")'):
            assert forbidden not in text, f"{path} duplicates Blender discovery"
