"""Artifact identity and storage tests — no Blender (Spec 001, Task 10).

Covers required behaviours 3, 4, 5, 6, 9 and 14: stable derived metadata,
checksums that match the bytes, project scoping, rejection of traversal
identifiers, a new artifact per new job, and the guarantee that the store cannot
reach a .blend, a journal record, or a recovery snapshot.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from studio_preview.artifacts import (
    ARTIFACT_ID_PATTERN,
    ArtifactError,
    LocalArtifactStore,
    UnsafeArtifactIdError,
    artifact_from_wire,
    assert_safe_artifact_id,
    checksum_of,
    derive_artifact_id,
    to_artifact_wire,
)
from studio_preview.generator import PNG_SIGNATURE

PROJECT_ID = "proj_seed"
OTHER_PROJECT = "proj_other"

PNG_BYTES = PNG_SIGNATURE + b"fake-png-payload"


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


def put(store: LocalArtifactStore, artifact_id: str, data: bytes = PNG_BYTES, **kw):
    return store.put(
        project_id=kw.pop("project_id", PROJECT_ID),
        artifact_id=artifact_id,
        data=data,
        media_type="image/png",
        width=kw.pop("width", 640),
        height=kw.pop("height", 360),
        **kw,
    )


# ---------------------------------------------------------------------------
# Derived identity (9: a new job produces a new artifact)
# ---------------------------------------------------------------------------


def test_artifact_id_is_deterministic_for_the_same_job():
    first = derive_artifact_id(PROJECT_ID, "job_req_1_0")
    second = derive_artifact_id(PROJECT_ID, "job_req_1_0")

    assert first == second, "a retry must resolve to the same artifact"
    assert ARTIFACT_ID_PATTERN.match(first), first


def test_9_a_different_job_derives_a_different_artifact_id():
    first = derive_artifact_id(PROJECT_ID, "job_req_1_0")
    second = derive_artifact_id(PROJECT_ID, "job_req_2_0")

    assert first != second, "a new intentional change is a new artifact"


def test_artifact_id_is_project_scoped():
    """The same job_id in two projects must not collide."""
    assert derive_artifact_id(PROJECT_ID, "job_1") != derive_artifact_id(
        OTHER_PROJECT, "job_1"
    )


def test_artifact_id_derivation_rejects_blank_inputs():
    for project_id, job_id in (("", "job_1"), ("  ", "job_1"), (PROJECT_ID, "")):
        with pytest.raises(ArtifactError):
            derive_artifact_id(project_id, job_id)


def test_artifact_id_derivation_rejects_an_unknown_artifact_type():
    with pytest.raises(ArtifactError):
        derive_artifact_id(PROJECT_ID, "job_1", artifact_type="glb_scene")


def test_derived_ids_satisfy_the_canonical_pattern():
    """The derived id must be usable on the wire and as a path segment."""
    from studio_contracts import SCHEMA_FILES, validate_against_schema

    artifact_id = derive_artifact_id(PROJECT_ID, "job_req_1_0")
    result = validate_against_schema(
        SCHEMA_FILES["PreviewArtifact"],
        {
            "artifact_id": artifact_id,
            "project_id": PROJECT_ID,
            "artifact_type": "preview_image",
            "media_type": "image/png",
            "created_at": "2026-09-15T04:00:00Z",
            "width": 640,
            "height": 360,
            "size_bytes": 10,
            "checksum": "sha256:" + "a" * 64,
        },
    )
    assert result.valid, result.violations


# ---------------------------------------------------------------------------
# 3. Stable metadata  /  4. Checksum matches the bytes
# ---------------------------------------------------------------------------


def test_3_stored_artifact_has_stable_metadata(store):
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    artifact = put(
        store, artifact_id, job_id="job_1", engine="BLENDER_WORKBENCH",
        created_at="2026-09-15T04:00:00Z",
    )

    assert artifact.artifact_id == artifact_id
    assert artifact.project_id == PROJECT_ID
    assert artifact.artifact_type == "preview_image"
    assert artifact.media_type == "image/png"
    assert artifact.width == 640
    assert artifact.height == 360
    assert artifact.size_bytes == len(PNG_BYTES)
    assert artifact.job_id == "job_1"
    assert artifact.engine == "BLENDER_WORKBENCH"
    assert artifact.created_at == "2026-09-15T04:00:00Z"

    # And it survives a round trip through the store unchanged.
    assert store.get(PROJECT_ID, artifact_id) == artifact


def test_3_metadata_satisfies_the_canonical_schema(store):
    from studio_contracts import SCHEMA_FILES, validate_against_schema

    artifact = put(store, derive_artifact_id(PROJECT_ID, "job_1"), job_id="job_1")
    result = validate_against_schema(
        SCHEMA_FILES["PreviewArtifact"], to_artifact_wire(artifact)
    )
    assert result.valid, result.violations


def test_4_checksum_matches_the_stored_bytes(store):
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    artifact = put(store, artifact_id)

    expected = "sha256:" + hashlib.sha256(PNG_BYTES).hexdigest()
    assert artifact.checksum == expected
    assert checksum_of(PNG_BYTES) == expected

    # Verified against what actually came back out of the store.
    data = store.read_bytes(PROJECT_ID, artifact_id)
    assert data == PNG_BYTES
    assert "sha256:" + hashlib.sha256(data).hexdigest() == artifact.checksum


def test_4_different_bytes_produce_a_different_checksum(store):
    a = put(store, derive_artifact_id(PROJECT_ID, "job_a"), data=PNG_SIGNATURE + b"A")
    b = put(store, derive_artifact_id(PROJECT_ID, "job_b"), data=PNG_SIGNATURE + b"B")

    assert a.checksum != b.checksum


def test_an_empty_artifact_is_refused(store):
    """An empty file is a failed render, not an artifact."""
    with pytest.raises(ArtifactError):
        put(store, derive_artifact_id(PROJECT_ID, "job_1"), data=b"")


def test_nonpositive_dimensions_are_refused(store):
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    with pytest.raises(ArtifactError):
        put(store, artifact_id, width=0)
    with pytest.raises(ArtifactError):
        put(store, artifact_id, height=-1)


def test_an_unsupported_media_type_is_refused(store):
    with pytest.raises(ArtifactError):
        store.put(
            project_id=PROJECT_ID,
            artifact_id=derive_artifact_id(PROJECT_ID, "job_1"),
            data=PNG_BYTES,
            media_type="application/x-blender",
            width=1,
            height=1,
        )


# ---------------------------------------------------------------------------
# 5. Project scoping
# ---------------------------------------------------------------------------


def test_5_artifact_storage_is_project_scoped(store):
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    put(store, artifact_id)

    assert store.get(PROJECT_ID, artifact_id) is not None
    assert store.get(OTHER_PROJECT, artifact_id) is None
    assert store.read_bytes(OTHER_PROJECT, artifact_id) is None
    assert store.exists(OTHER_PROJECT, artifact_id) is False


def test_5_listing_is_project_scoped(store):
    put(store, derive_artifact_id(PROJECT_ID, "job_1"))
    put(store, derive_artifact_id(OTHER_PROJECT, "job_1"), project_id=OTHER_PROJECT)

    assert len(store.list_for_project(PROJECT_ID)) == 1
    assert len(store.list_for_project(OTHER_PROJECT)) == 1
    assert store.list_for_project("proj_nothing") == ()


def test_5_each_project_gets_its_own_directory(store, tmp_path):
    put(store, derive_artifact_id(PROJECT_ID, "job_1"))
    put(store, derive_artifact_id(OTHER_PROJECT, "job_1"), project_id=OTHER_PROJECT)

    directories = sorted(p.name for p in store.root.iterdir() if p.is_dir())
    assert directories == sorted([PROJECT_ID, OTHER_PROJECT])


# ---------------------------------------------------------------------------
# 6. Traversal identifiers rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "preview_../../etc/passwd",
        "preview_..",
        "..",
        ".",
        "/etc/passwd",
        "preview_/etc/passwd",
        "preview_a1b2/../../x",
        "preview_a1b2\x00",
        "preview_A1B2C3D4",
        "preview_short",
        "journal_a1b2c3d4e5f60718",
        "seed_project",
        "",
        "   ",
    ],
)
def test_6_traversal_and_malformed_identifiers_are_rejected(store, hostile):
    with pytest.raises(UnsafeArtifactIdError):
        assert_safe_artifact_id(hostile)

    # A lookup reports "not found" rather than raising, so the reason cannot be
    # used as a probing oracle.
    assert store.get(PROJECT_ID, hostile) is None
    assert store.read_bytes(PROJECT_ID, hostile) is None
    assert store.exists(PROJECT_ID, hostile) is False


@pytest.mark.parametrize(
    "hostile_project",
    ["../../etc", "..", ".", "/etc", "proj/../other", "proj\x00", "", "   "],
)
def test_6_unsafe_project_ids_are_rejected(store, hostile_project):
    assert store.get(hostile_project, derive_artifact_id(PROJECT_ID, "job_1")) is None
    assert store.list_for_project(hostile_project) == ()


def test_6_a_hostile_id_writes_nothing_to_disk(store):
    with pytest.raises(UnsafeArtifactIdError):
        put(store, "preview_../../escape")

    # Nothing was created anywhere.
    assert not list(store.root.rglob("*escape*"))


# ---------------------------------------------------------------------------
# 14. The store cannot reach project or worker files
# ---------------------------------------------------------------------------


def test_14_artifact_lookup_cannot_reach_a_blend_journal_or_recovery_file(
    store, tmp_path
):
    """Files that are not registered artifacts must be unreachable."""
    # Plant exactly the kinds of file the store must never serve, inside the very
    # project directory it is allowed to read.
    project_dir = store.root / PROJECT_ID
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "seed_project.blend").write_bytes(b"BLENDER-secret-scene")
    (project_dir / "job_req_1_0.json").write_text('{"phase": "completed"}')
    (project_dir / ".env").write_text("STUDIO_WORKER_TOKEN=super-secret")
    (project_dir / "recovery.blend").write_bytes(b"recovery copy")

    for name in (
        "seed_project.blend",
        "seed_project",
        "job_req_1_0",
        "job_req_1_0.json",
        ".env",
        "recovery.blend",
        "recovery",
    ):
        assert store.get(PROJECT_ID, name) is None, name
        assert store.read_bytes(PROJECT_ID, name) is None, name

    # And none of them are listed as artifacts.
    assert store.list_for_project(PROJECT_ID) == ()


def test_14_listing_ignores_unregistered_json_files(store):
    project_dir = store.root / PROJECT_ID
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "not_an_artifact.json").write_text("{}")
    (project_dir / "preview_deadbeefdeadbeef.json").write_text("not json at all")

    put(store, derive_artifact_id(PROJECT_ID, "job_1"))

    listed = store.list_for_project(PROJECT_ID)
    assert len(listed) == 1, "only genuinely registered artifacts appear"


def test_14_metadata_without_bytes_is_not_an_artifact(store):
    """An interrupted write must read as absent, never as a truncated image."""
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    put(store, artifact_id)

    # Simulate losing the bytes while the sidecar survives.
    (store.root / PROJECT_ID / f"{artifact_id}.png").unlink()

    assert store.get(PROJECT_ID, artifact_id) is None
    assert store.read_bytes(PROJECT_ID, artifact_id) is None
    assert store.exists(PROJECT_ID, artifact_id) is False


def test_14_bytes_without_metadata_are_not_an_artifact(store):
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    put(store, artifact_id)
    (store.root / PROJECT_ID / f"{artifact_id}.json").unlink()

    assert store.get(PROJECT_ID, artifact_id) is None


def test_14_corrupt_metadata_reads_as_absent(store):
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    put(store, artifact_id)
    (store.root / PROJECT_ID / f"{artifact_id}.json").write_text("{not json")

    assert store.get(PROJECT_ID, artifact_id) is None


def test_14_a_symlink_escape_is_refused(store, tmp_path):
    """Containment is re-checked after symlink resolution."""
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    put(store, artifact_id)

    secret = tmp_path / "outside.png"
    secret.write_bytes(b"secret bytes outside the store")

    image = store.root / PROJECT_ID / f"{artifact_id}.png"
    image.unlink()
    image.symlink_to(secret)

    # The metadata still exists and the symlink resolves outside the project dir.
    assert store.read_bytes(PROJECT_ID, artifact_id) is None


# ---------------------------------------------------------------------------
# Idempotent writes and round trips
# ---------------------------------------------------------------------------


def test_writing_the_same_artifact_twice_does_not_create_a_second_one(store):
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    put(store, artifact_id)
    put(store, artifact_id)

    assert len(store.list_for_project(PROJECT_ID)) == 1
    files = sorted(p.name for p in (store.root / PROJECT_ID).iterdir())
    assert files == [f"{artifact_id}.json", f"{artifact_id}.png"]


def test_artifact_wire_round_trip(store):
    artifact = put(
        store, derive_artifact_id(PROJECT_ID, "job_1"), job_id="job_1", engine="X"
    )
    assert artifact_from_wire(to_artifact_wire(artifact)) == artifact


def test_artifact_wire_never_contains_a_path(store):
    artifact = put(store, derive_artifact_id(PROJECT_ID, "job_1"), job_id="job_1")
    serialized = json.dumps(to_artifact_wire(artifact))

    for forbidden in ("path", "url", str(store.root), ".png", "/tmp", "filename"):
        assert forbidden not in serialized, f"artifact wire leaked {forbidden!r}"


def test_the_metadata_sidecar_on_disk_contains_no_path(store):
    artifact_id = derive_artifact_id(PROJECT_ID, "job_1")
    put(store, artifact_id, job_id="job_1")

    sidecar = (store.root / PROJECT_ID / f"{artifact_id}.json").read_text()
    assert str(store.root) not in sidecar
    assert ".png" not in sidecar
