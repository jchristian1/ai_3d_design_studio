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

Stability is asserted on DECODED PIXELS, not on file checksums. The renderer is
EEVEE, which samples; its pixels are reproducible (reprojection off, samples
pinned) but its PNG bytes are not guaranteed to be, so comparing files would ask
the wrong question. See ``test_the_same_scene_renders_to_the_same_pixels``.
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


def decoded_pixels(data: bytes) -> tuple[tuple[int, int], bytes]:
    """The PNG's size and its RGB bytes.

    Decoding is what lets stability be asserted on the IMAGE rather than on the
    file: two encodings of identical pixels are not required to be identical
    files.
    """
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        rgb = image.convert("RGB")
        return rgb.size, rgb.tobytes()


def mean_abs_pixel_difference(first: bytes, second: bytes) -> float:
    """Mean absolute per-channel difference, normalised to 0.0–1.0.

    0.0 means pixel-identical. A visible change in a small preview moves this by
    more than a percent, so the tolerance and the change threshold below sit
    orders of magnitude apart.
    """
    (size_a, pixels_a) = decoded_pixels(first)
    (size_b, pixels_b) = decoded_pixels(second)
    assert size_a == size_b, "cannot compare renders of different sizes"

    total = sum(abs(a - b) for a, b in zip(pixels_a, pixels_b))
    return total / (len(pixels_a) * 255.0)


#: Allowed drift between two renders of one unchanged scene. Measured at exactly
#: 0.0 on the pinned Blender; the headroom is for driver-level rounding, not for
#: sampling noise, which is configured out.
PIXEL_NOISE_TOLERANCE = 0.002

#: A real design change must exceed this. A 0.50 m cube move measures ~0.03.
PIXEL_CHANGE_THRESHOLD = 0.005


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


def render(
    generator: BlenderPreviewGenerator,
    project: Path,
    job_id: str,
    focus_object_ids: tuple[str, ...] = (),
):
    outcome = generator.generate(
        PreviewRequest(
            project_id=PROJECT_ID,
            project_path=project,
            width=PREVIEW_WIDTH,
            height=PREVIEW_HEIGHT,
            job_id=job_id,
            focus_object_ids=focus_object_ids,
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


def tag_object(project: Path, name: str, object_id: str) -> None:
    """Give an object a stable studio id, the way the platform does.

    The seed fixture deliberately has none — it predates stable ids — so a test about
    focusing by id has to supply one rather than assume it.
    """
    import tempfile

    from blender_mcp.blender_runtime import run_blender_script

    script = Path(tempfile.mkdtemp()) / "tag.py"
    script.write_text(
        "import bpy, os\n"
        "bpy.ops.wm.open_mainfile(filepath=os.environ['TAG_BLEND'])\n"
        "bpy.data.objects[os.environ['TAG_NAME']]"
        "[os.environ['TAG_ID_KEY']] = os.environ['TAG_ID']\n"
        "bpy.ops.wm.save_mainfile()\n",
        "utf-8",
    )
    proc = run_blender_script(
        str(script),
        env={
            "TAG_BLEND": str(project),
            "TAG_NAME": name,
            "TAG_ID_KEY": "studio_object_id",
            "TAG_ID": object_id,
        },
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_focusing_on_an_object_frames_it_closer(generator, project):
    """A focused preview is a different picture, not the same one relabelled.

    This is what turns "here is the site" into "here is what changed". With one object
    the SUBJECT cannot change, but the viewpoint does: a focused view uses a steeper
    angle, to see over the walls of a room, and a tighter margin. Asserting the pixels
    differ is the honest check that the focus reached Blender and was acted on.
    """
    tag_object(project, "Cube", "obj_cube")

    wide = render(generator, project, "job_focus_wide")
    close = render(
        generator,
        project,
        "job_focus_close",
        focus_object_ids=("obj_cube",),
    )

    difference = mean_abs_pixel_difference(wide.image_bytes, close.image_bytes)
    assert difference > PIXEL_CHANGE_THRESHOLD, (
        "focusing on an object should visibly change the framing "
        f"(mean absolute difference {difference:.6f})"
    )


def test_an_unknown_focus_id_falls_back_to_the_whole_scene(generator, project):
    """A stale id must never cost the user a picture.

    Object ids travel from a previous turn's scene read, so one can refer to something
    that has since been deleted. The framing widens; nothing fails.
    """
    wide = render(generator, project, "job_focus_none")
    stale = render(
        generator,
        project,
        "job_focus_stale",
        focus_object_ids=("obj_does_not_exist",),
    )

    difference = mean_abs_pixel_difference(wide.image_bytes, stale.image_bytes)
    assert difference <= PIXEL_NOISE_TOLERANCE, (
        "an unmatched focus id should render exactly the unfocused view "
        f"(mean absolute difference {difference:.6f})"
    )


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


def test_the_same_scene_renders_to_the_same_pixels(generator, project):
    """An unchanged scene must produce the same IMAGE twice.

    Asserted on pixels rather than on a file checksum, and that distinction is the
    point. EEVEE samples, so the old byte-equality assertion would be the wrong
    question to ask of it — and measurement shows the bytes genuinely do differ
    between two runs while the decoded pixels are identical, because PNG
    compression is not required to be bit-stable.

    What actually has to hold is that the renderer is not noisy: nothing
    stochastic may leak into a still preview, or "the picture changed" would stop
    meaning "the design changed". ``use_taa_reprojection`` is disabled and the
    sample count is pinned to make that true, and this test fails if either is
    reverted.
    """
    first = render(generator, project, "job_det_1")
    second = render(generator, project, "job_det_2")

    difference = mean_abs_pixel_difference(first.image_bytes, second.image_bytes)
    assert difference <= PIXEL_NOISE_TOLERANCE, (
        f"the preview renderer is not stable for an unchanged scene "
        f"(mean absolute difference {difference:.6f})"
    )


def test_a_changed_scene_renders_to_visibly_different_pixels(generator, project):
    """The other half of stability: it must still RESPOND to a real change.

    A renderer could pass the stability test by emitting a constant image, so
    stability is only meaningful alongside sensitivity. Moving the cube half a
    metre has to move pixels by far more than the noise tolerance.
    """
    before = render(generator, project, "job_change_before")

    move_cube(project, 0.5)

    after = render(generator, project, "job_change_after")

    difference = mean_abs_pixel_difference(before.image_bytes, after.image_bytes)
    assert difference > PIXEL_CHANGE_THRESHOLD, (
        f"moving the cube 0.50 m barely changed the render "
        f"(mean absolute difference {difference:.6f})"
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

    assert rendered.engine == "BLENDER_EEVEE"
    assert rendered.width == PREVIEW_WIDTH
    assert rendered.height == PREVIEW_HEIGHT
    assert rendered.media_type == "image/png"
    assert png_dimensions(rendered.image_bytes) == (PREVIEW_WIDTH, PREVIEW_HEIGHT)


def test_a_preview_does_not_require_a_dedicated_gpu(generator, project):
    """CI correctness must not depend on RTX.

    EEVEE needs more GL capability than the Workbench renderer it replaced, which
    is the one real cost of rendering materials instead of flat shading. It is
    verified here with no GPU device configured, and it must still succeed — if
    this ever fails on a target machine, the answer is that a preview failure is
    already a value that cannot fail a mutation, not that the design is lost.
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
