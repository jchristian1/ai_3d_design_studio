"""Opening a project that has no file yet.

The user creates a project in the browser; nobody puts a ``.blend`` on the workstation.
So the worker creates one — while keeping the property that made the old registry safe:
the path is derived by the WORKER, inside its own root, from a validated id.

Blender itself is not run here. Creation is injected, so these tests are about the
registry's decisions; ``tests/mcp`` covers a real file being created by real Blender.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blender_worker.provisioning import (
    PROJECT_SUFFIX,
    ProjectProvisioningError,
    ProvisioningProjectRegistry,
)
from blender_worker.registry import ProjectResolutionError, UnsafeProjectIdError


def _recording_create(created: list[Path]):
    def create(path: Path) -> None:
        created.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"BLENDER-created")

    return create


@pytest.fixture
def root(tmp_path: Path) -> Path:
    projects = tmp_path / "projects"
    projects.mkdir()
    return projects


def test_an_existing_project_is_returned_without_creating_anything(root: Path):
    existing = root / f"proj_seed{PROJECT_SUFFIX}"
    existing.write_bytes(b"BLENDER-existing")
    created: list[Path] = []
    registry = ProvisioningProjectRegistry(root, create=_recording_create(created))

    assert registry.blend_path_for("proj_seed") == existing.resolve()
    assert created == [], "an existing project must never be recreated"
    assert existing.read_bytes() == b"BLENDER-existing"


def test_a_new_project_is_created_on_first_use(root: Path):
    created: list[Path] = []
    registry = ProvisioningProjectRegistry(root, create=_recording_create(created))

    path = registry.blend_path_for("proj_8d83f")

    assert path == (root / f"proj_8d83f{PROJECT_SUFFIX}").resolve()
    assert created == [path]
    assert path.exists()

    # Second use finds the file rather than creating a second one.
    assert registry.blend_path_for("proj_8d83f") == path
    assert len(created) == 1


def test_the_file_name_comes_from_the_id_and_stays_inside_the_root(root: Path):
    registry = ProvisioningProjectRegistry(root, create=_recording_create([]))
    path = registry.path_of("proj_abc")
    assert path.parent == root.resolve()
    assert path.name == f"proj_abc{PROJECT_SUFFIX}"


@pytest.mark.parametrize(
    "hostile",
    [
        "../escape",
        "..",
        ".",
        "/etc/passwd",
        "sub/dir",
        "back\\slash",
        "with\0null",
        "",
        "   ",
    ],
)
def test_a_hostile_project_id_creates_nothing(root: Path, hostile: str):
    created: list[Path] = []
    registry = ProvisioningProjectRegistry(root, create=_recording_create(created))

    with pytest.raises(ProjectResolutionError):
        registry.blend_path_for(hostile)

    assert created == [], "a rejected id must not reach the filesystem"
    assert list(root.iterdir()) == []


def test_a_non_string_project_id_is_refused(root: Path):
    registry = ProvisioningProjectRegistry(root, create=_recording_create([]))
    with pytest.raises(UnsafeProjectIdError):
        registry.blend_path_for(None)  # type: ignore[arg-type]


def test_provisioning_can_be_switched_off(root: Path):
    """A read-only locator refuses an unknown project instead of inventing one."""
    created: list[Path] = []
    registry = ProvisioningProjectRegistry(
        root, create=_recording_create(created), provision=False
    )

    with pytest.raises(ProjectResolutionError, match="no project file"):
        registry.blend_path_for("proj_unknown")
    assert created == []


def test_a_creation_that_writes_nothing_is_reported_not_ignored(root: Path):
    def create(path: Path) -> None:  # pretends to work, writes nothing
        return None

    registry = ProvisioningProjectRegistry(root, create=create)
    with pytest.raises(ProjectProvisioningError, match="still has no file"):
        registry.blend_path_for("proj_silent")


def test_known_project_ids_lists_what_exists(root: Path):
    (root / f"proj_a{PROJECT_SUFFIX}").write_bytes(b"x")
    (root / f"proj_b{PROJECT_SUFFIX}").write_bytes(b"x")
    (root / "notes.txt").write_text("ignored")
    registry = ProvisioningProjectRegistry(root)

    assert registry.known_project_ids() == ("proj_a", "proj_b")


def test_the_registry_satisfies_the_locator_protocol(root: Path):
    """Structural, because ``ProjectLocator`` is a plain Protocol: the executors only
    ever call ``blend_path_for``, and swapping registries must not need a base class."""
    registry = ProvisioningProjectRegistry(root)
    assert callable(getattr(registry, "blend_path_for", None))
