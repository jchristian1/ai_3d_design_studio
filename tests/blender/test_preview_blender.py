"""Real Blender preview rendering (Spec 001, Task 10).

OPT-IN: marked ``blender``, because it launches real Blender processes.

    pytest -m blender tests/blender/test_preview_blender.py -v

Renders a Task 5 fixture working copy at Cube X = 0.0, applies the normal Spec 001
move to X = 0.50, renders again, and asserts what can be asserted robustly:

  - both PNG files exist and are non-empty
  - both begin with the real PNG signature
  - the encoded dimensions match the requested dimensions
  - the checksums DIFFER, because the visible scene differs
  - the .blend is byte-identical before and after rendering

Deliberately NOT asserted: pixel-perfect equality against a stored reference image.
Blender versions, GPU drivers, and colour-management defaults all shift individual
pixel values, so a golden-image comparison would fail for reasons that have nothing
to do with this code. What matters is that the render happened, is a valid image of
the requested size, and CHANGES when the design changes.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
from pathlib import Path

import pytest
from blender_mcp.blender_runtime import find_blender_executable
from blender_mcp.tolerance import coordinates_equal
from blender_mcp.tools.move_object import plan_from_delta
from blender_worker.blender_ops import SubprocessBlenderOperationExecutor
from studio_preview.artifacts import LocalArtifactStore, derive_artifact_id
from studio_preview.blender_preview import BlenderPreviewGenerator
from studio_preview.generator import (
    PNG_SIGNATURE,
    PreviewRequest,
    looks_like_png,
)
from studio_types import ObjectRef, Vec3

PROJECT_ID = "proj_seed"

PREVIEW_WIDTH = 320
PREVIEW_HEIGHT = 180


pytestmark = pytest.mark.blender


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def png_chunks(data: bytes) -> list[tuple[str, bytes]]:
    """Every PNG chunk as (type, payload).

    Parsed directly so a test can assert exactly which ancillary chunks a served
    image carries — the path leak lived in a tEXt chunk.
    """
    assert looks_like_png(data), "not a PNG"
    out: list[tuple[str, bytes]] = []
    offset = 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        ctype = data[offset + 4 : offset + 8].decode("ascii", "replace")
        out.append((ctype, data[offset + 8 : offset + 8 + length]))
        offset += 8 + length + 4
    return out


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG IHDR chunk.

    Parsed from the bytes themselves rather than trusted from metadata, so the
    dimension assertion really checks the produced image.
    """
    assert looks_like_png(data), "not a PNG"
    # 8-byte signature, 4-byte length, 4-byte "IHDR", then width and height.
    assert data[12:16] == b"IHDR", "first chunk is not IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_of(path.read_bytes())


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """An isolated working copy of the Task 5 seed fixture."""
    if find_blender_executable() is None:
        pytest.skip("Blender executable not found")
    from studio_fixtures.seed_project import ensure_seed_project

    destination = tmp_path / "seed_project.blend"
    shutil.copy2(ensure_seed_project(), destination)
    return destination


@pytest.fixture
def generator() -> BlenderPreviewGenerator:
    return BlenderPreviewGenerator()


def render(generator: BlenderPreviewGenerator, project: Path, job_id: str):
    outcome = generator.generate(
        PreviewRequest(
            project_id=PROJECT_ID,
            project_path=project,
            width=PREVIEW_WIDTH,
            height=PREVIEW_HEIGHT,
            job_id=job_id,
        )
    )
    assert outcome.ok, outcome.error
    assert outcome.render is not None
    return outcome.render


def move_cube(project: Path, delta_x: float) -> None:
    """Apply the normal Spec 001 move through the real worker Blender path."""
    executor = SubprocessBlenderOperationExecutor()
    target = ObjectRef(name="Cube")
    before = executor.read_object_position(project, target)
    assert before is not None

    plan = plan_from_delta(
        job_id="job_preview_move",
        target=target,
        expected_before_meters=before,
        delta_meters=Vec3(delta_x, 0.0, 0.0),
    )
    result = executor.execute_move(project, plan)
    assert result.get("verified"), result
    assert result.get("applied"), result


def cube_x(project: Path) -> float:
    from studio_fixtures.seed_project import inspect_blend

    for entry in inspect_blend(project)["digest"]["objects"]:
        if entry["name"] == "Cube":
            return float(entry["world_position_meters"]["x"])
    raise AssertionError("no Cube in the project")


# ---------------------------------------------------------------------------
# The required scenario
# ---------------------------------------------------------------------------


def test_preview_before_and_after_the_move_differ_visibly(
    generator, project, tmp_path
):
    """Render at X=0.0, move to X=0.50, render again, and compare."""
    store = LocalArtifactStore(tmp_path / "artifacts")

    assert coordinates_equal(cube_x(project), 0.0), "must start at the origin"

    # ---- preview at X = 0.0 -----------------------------------------
    before_render = render(generator, project, "job_before")
    before = store.put(
        project_id=PROJECT_ID,
        artifact_id=derive_artifact_id(PROJECT_ID, "job_before"),
        data=before_render.image_bytes,
        media_type=before_render.media_type,
        width=before_render.width,
        height=before_render.height,
        job_id="job_before",
        engine=before_render.engine,
    )

    # ---- the normal Spec 001 move -----------------------------------
    move_cube(project, 0.5)
    assert coordinates_equal(cube_x(project), 0.5)

    # ---- preview at X = 0.50 ----------------------------------------
    after_render = render(generator, project, "job_after")
    after = store.put(
        project_id=PROJECT_ID,
        artifact_id=derive_artifact_id(PROJECT_ID, "job_after"),
        data=after_render.image_bytes,
        media_type=after_render.media_type,
        width=after_render.width,
        height=after_render.height,
        job_id="job_after",
        engine=after_render.engine,
    )

    # ---- PNG files exist, are non-empty, and are valid PNGs ---------
    for artifact in (before, after):
        data = store.read_bytes(PROJECT_ID, artifact.artifact_id)
        assert data, f"{artifact.artifact_id} has no bytes"
        assert data[:8] == PNG_SIGNATURE, "not a real PNG signature"
        assert len(data) > 1000, "a rendered image should not be trivially small"
        assert artifact.size_bytes == len(data)
        assert artifact.media_type == "image/png"

    # ---- metadata dimensions match the actual image -----------------
    for artifact in (before, after):
        data = store.read_bytes(PROJECT_ID, artifact.artifact_id)
        width, height = png_dimensions(data)
        assert (width, height) == (PREVIEW_WIDTH, PREVIEW_HEIGHT), (
            "the encoded image size must match what was requested"
        )
        assert (artifact.width, artifact.height) == (width, height), (
            "reported metadata must match the encoded image"
        )

    # ---- checksums differ because the scene differs ------------------
    assert before.checksum != after.checksum, (
        "moving the cube 0.50 m must change the rendered image"
    )
    # And the recorded checksum really is the checksum of the stored bytes.
    for artifact in (before, after):
        data = store.read_bytes(PROJECT_ID, artifact.artifact_id)
        assert artifact.checksum == "sha256:" + sha256_of(data)

    # ---- the earlier preview was not overwritten --------------------
    assert before.artifact_id != after.artifact_id
    assert len(store.list_for_project(PROJECT_ID)) == 2


def test_rendering_a_preview_never_modifies_the_project(generator, project):
    """A preview is a READ. The .blend must be byte-identical afterwards."""
    digest_before = file_sha256(project)

    render(generator, project, "job_readonly")

    assert file_sha256(project) == digest_before, (
        "rendering a preview modified the design file"
    )
    assert coordinates_equal(cube_x(project), 0.0)


def test_rendering_adds_no_camera_to_the_saved_project(generator, project):
    """The temporary preview camera must exist only in memory."""
    from studio_fixtures.seed_project import inspect_blend

    render(generator, project, "job_no_camera")

    names = sorted(
        entry["name"] for entry in inspect_blend(project)["digest"]["objects"]
    )
    assert names == ["Cube"], f"the saved project gained objects: {names}"


def test_the_same_scene_renders_deterministically(generator, project):
    """Workbench has no sampling, so two renders of one scene must agree exactly.

    This is why the engine choice matters: with a path tracer this assertion would
    be flaky, and a checksum could not be used to detect a real visual change.

    Byte-level equality is only achievable because render stamping is disabled.
    Blender otherwise embeds ``Date`` and ``RenderTime`` tEXt chunks, which vary
    per run — see ``test_rendered_previews_embed_no_metadata`` for the security
    reason those are stripped.
    """
    first = render(generator, project, "job_det_1")
    second = render(generator, project, "job_det_2")

    assert sha256_of(first.image_bytes) == sha256_of(second.image_bytes), (
        "the preview renderer is not deterministic for an unchanged scene"
    )


def test_rendered_previews_embed_no_filesystem_path(generator, tmp_path):
    """REGRESSION: Blender embeds the .blend path in PNG metadata by default.

    Left enabled, every served preview carried a ``tEXt`` chunk like::

        File\\0/tmp/xyz/SECRET_CLIENT_NAME_seed.blend

    The artifact is served straight to a browser, so that leaked the absolute
    server path AND the project filename — which may be a client's name. JSON
    responses are carefully path-free; the image bytes must be too. The render
    script disables all stamp metadata, and this test fails if that is reverted.
    """
    from studio_fixtures.seed_project import ensure_seed_project

    # A distinctive name, so a leak would be unmistakable.
    project = tmp_path / "SECRET_CLIENT_NAME_seed.blend"
    shutil.copy2(ensure_seed_project(), project)

    data = render(generator, project, "job_no_metadata").image_bytes
    text = data.decode("latin-1")

    for leaked in (
        "SECRET_CLIENT_NAME",
        str(tmp_path),
        ".blend",
        "/home",
        "/tmp",
        "seed_project",
    ):
        assert leaked not in text, f"the rendered PNG leaked {leaked!r}"


def test_rendered_previews_embed_no_metadata_chunks(generator, project):
    """Only benign, non-identifying ancillary chunks may remain."""
    data = render(generator, project, "job_chunks").image_bytes

    allowed = {
        "IHDR",
        "IDAT",
        "IEND",
        "sRGB",
        "gAMA",
        "cHRM",
        "eXIf",  # resolution only
        "oFFs",
        "pHYs",
        "PLTE",
        "tRNS",
        "bKGD",
    }
    present = {ctype for ctype, _ in png_chunks(data)}

    # Text chunks are exactly what carried the path and timestamps.
    for text_chunk in ("tEXt", "iTXt", "zTXt", "tIME"):
        assert text_chunk not in present, (
            f"the rendered PNG still embeds a {text_chunk} chunk"
        )
    assert present <= allowed, f"unexpected chunks: {sorted(present - allowed)}"


def test_the_hostname_and_camera_name_are_not_embedded(generator, project):
    """Stamping would otherwise disclose the host and the internal camera name."""
    import socket

    data = render(generator, project, "job_hostname").image_bytes
    text = data.decode("latin-1")

    assert socket.gethostname() not in text
    assert "__studio_preview_camera" not in text


def test_the_render_reports_the_engine_and_requested_size(generator, project):
    rendered = render(generator, project, "job_meta")

    assert rendered.engine == "BLENDER_WORKBENCH"
    assert rendered.width == PREVIEW_WIDTH
    assert rendered.height == PREVIEW_HEIGHT
    assert rendered.media_type == "image/png"
    assert png_dimensions(rendered.image_bytes) == (PREVIEW_WIDTH, PREVIEW_HEIGHT)


def test_a_preview_does_not_require_a_gpu(generator, project):
    """CI correctness must not depend on RTX.

    Workbench rasterises on the CPU. The render is simply performed with no GPU
    device configured, and it must still succeed.
    """
    rendered = render(generator, project, "job_no_gpu")
    assert looks_like_png(rendered.image_bytes)


def test_a_missing_project_degrades_instead_of_raising(generator, tmp_path):
    outcome = generator.generate(
        PreviewRequest(
            project_id=PROJECT_ID,
            project_path=tmp_path / "does_not_exist.blend",
            width=PREVIEW_WIDTH,
            height=PREVIEW_HEIGHT,
        )
    )

    assert outcome.ok is False
    assert outcome.render is None
    assert outcome.error["code"] in ("INTERNAL_ERROR", "BLENDER_UNAVAILABLE")
    # The message must not disclose a filesystem path.
    assert str(tmp_path) not in outcome.error["message"]
    assert ".blend" not in outcome.error["message"]


def test_a_corrupt_project_degrades_instead_of_raising(generator, tmp_path):
    """A render failure is a value, never an exception."""
    broken = tmp_path / "broken.blend"
    broken.write_bytes(b"this is definitely not a blend file")

    outcome = generator.generate(
        PreviewRequest(
            project_id=PROJECT_ID,
            project_path=broken,
            width=PREVIEW_WIDTH,
            height=PREVIEW_HEIGHT,
        )
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert str(tmp_path) not in outcome.error["message"]
