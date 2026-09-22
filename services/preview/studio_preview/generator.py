"""The PreviewGenerator boundary.

Spec 001, Task 10.

    WorkerExecutor
        v
    PreviewGenerator          <- this Protocol: the ONLY thing the worker knows
        v
    Blender subprocess / bpy script      (blender_preview.py)
        v
    PNG bytes

The worker contains no rendering logic and no bpy. It hands over a trusted project
path and a target size, and receives pixels or a structured error.

Why the generator returns BYTES
-------------------------------
Rendering and persistence are separate concerns: the generator produces an image,
``ArtifactStore`` decides where bytes live and what they are called. Returning
bytes rather than a path means the generator never picks a storage location, so a
future object-storage backend needs no change here, and a caller can never be
handed a filesystem path to leak.

Preview images are small (tens to low hundreds of kilobytes at Spec 001
resolutions), so holding one in memory is cheaper than coordinating temporary
files. A future large-artifact type (a long viewport recording) would warrant a
streaming variant of this Protocol rather than bending this one.

Future implementations fit without touching WorkerExecutor
----------------------------------------------------------
  - a quick Eevee render: another ``PreviewGenerator``
  - a final Cycles render: another ``PreviewGenerator``, likely with its own
    request fields and a much longer timeout
  - a GLB export for interactive Three.js preview: another ``PreviewGenerator``
    returning ``model/gltf-binary``
  - a live viewport stream: NOT this Protocol — a stream is not a single artifact,
    and pretending otherwise would distort both

Failures are values, not exceptions
-----------------------------------
A preview failure is an expected outcome (Blender missing, render timeout, an
unreadable project), and it must never be able to make a durably-saved mutation
look failed. So every outcome is a structured ``PreviewOutcome`` and callers branch
on data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

#: Spec 001 preview size. Small and fixed: a preview exists to prove a change is
#: visible, not to be a deliverable render.
DEFAULT_PREVIEW_WIDTH = 640
DEFAULT_PREVIEW_HEIGHT = 360

PNG_MEDIA_TYPE = "image/png"

#: The 8-byte PNG signature. Used to verify a render really produced a PNG rather
#: than an error page, a truncated file, or an empty buffer.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def looks_like_png(data: bytes) -> bool:
    """True when the bytes begin with the PNG signature."""
    return isinstance(data, (bytes, bytearray)) and bytes(
        data[: len(PNG_SIGNATURE)]
    ) == PNG_SIGNATURE


@dataclass(frozen=True)
class PreviewRequest:
    """What to render.

    ``project_path`` is a TRUSTED, server-resolved path. It is produced by the
    worker's ``ProjectLocator`` from a ``project_id`` and never travels in a Job,
    a ChatRequest, or any network message — the canonical schemas have no path
    field at all. A generator must treat it as the only sanctioned way to reach a
    project file and must not accept a path from any other source.
    """

    project_id: str
    project_path: Path
    width: int = DEFAULT_PREVIEW_WIDTH
    height: int = DEFAULT_PREVIEW_HEIGHT
    #: The job whose mutation is being depicted, when there is one.
    job_id: Optional[str] = None
    #: Stable object ids the picture should be ABOUT, when the caller knows.
    #:
    #: Normally the objects a job just created or moved, which turns the preview from
    #: "here is the site" into "here is what changed". A whole building framed from
    #: above is honest and nearly useless for judging a room: warm downlights over a
    #: reception desk came out as a bright postage stamp eighty metres away.
    #:
    #: Advisory, not a demand. Ids that are not in the scene are ignored and the
    #: framing falls back to the whole scene, because a preview must never fail — or
    #: render nothing — over a stale identifier.
    focus_object_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreviewRender:
    """Rendered pixels plus what a store needs to describe them."""

    image_bytes: bytes
    media_type: str
    width: int
    height: int
    #: Coarse, non-revealing description of the renderer, e.g. BLENDER_WORKBENCH.
    engine: str

    @property
    def size_bytes(self) -> int:
        return len(self.image_bytes)


@dataclass(frozen=True)
class PreviewOutcome:
    """The result of one preview attempt.

    Structured rather than exception-based, so a caller can record "the change
    was applied but the picture of it is unavailable" without any error handling
    gymnastics.
    """

    ok: bool
    render: Optional[PreviewRender] = None
    #: Canonical ``{code, message}`` when generation failed.
    error: Optional[dict] = None

    @classmethod
    def success(cls, render: PreviewRender) -> "PreviewOutcome":
        return cls(ok=True, render=render)

    @classmethod
    def failure(cls, code: str, message: str) -> "PreviewOutcome":
        return cls(ok=False, error={"code": code, "message": message})


@runtime_checkable
class PreviewGenerator(Protocol):
    """Renders a visual preview of a saved project.

    An implementation MUST NOT:
      - modify the project it renders (a preview is a read of a saved design),
      - save the ``.blend``,
      - accept a filesystem path from anywhere but ``PreviewRequest``,
      - raise for an expected failure.
    """

    #: Stable identifier, safe to log and expose, e.g. "blender_workbench".
    name: str

    def generate(self, request: PreviewRequest) -> PreviewOutcome: ...


# ---------------------------------------------------------------------------
# Fake generator for fast tests
# ---------------------------------------------------------------------------

#: A minimal but genuinely valid 1x1 PNG, used so fast tests exercise real PNG
#: signature checks instead of an obviously fake sentinel.
_MINIMAL_PNG = (
    PNG_SIGNATURE
    + bytes.fromhex(
        "0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000050001"
        "0d0a2db4"
        "0000000049454e44ae426082"
    )
)


class FakePreviewGenerator:
    """Deterministic in-memory generator so worker orchestration is testable.

    Produces a valid PNG whose bytes DEPEND on the scene it is asked to depict, so
    a test can meaningfully assert that a different scene yields a different
    checksum — the same property the real Blender test asserts.
    """

    name = "fake_preview"

    def __init__(self) -> None:
        self.calls: list[PreviewRequest] = []
        #: Set to a message to make the next generation fail.
        self.fail_with: Optional[str] = None
        self.fail_code: str = "INTERNAL_ERROR"
        #: Optional hook returning a scene-dependent discriminator, so the fake's
        #: output changes when the fake scene changes.
        self.scene_probe = None

    def generate(self, request: PreviewRequest) -> PreviewOutcome:
        self.calls.append(request)

        if self.fail_with is not None:
            return PreviewOutcome.failure(self.fail_code, self.fail_with)

        # A valid PNG, plus a trailing comment chunk that varies with the scene so
        # two different scenes produce two different checksums.
        discriminator = ""
        if self.scene_probe is not None:
            discriminator = str(self.scene_probe(request))
        payload = _MINIMAL_PNG + discriminator.encode("utf-8")

        return PreviewOutcome.success(
            PreviewRender(
                image_bytes=payload,
                media_type=PNG_MEDIA_TYPE,
                width=request.width,
                height=request.height,
                engine="FAKE",
            )
        )


__all__ = [
    "DEFAULT_PREVIEW_HEIGHT",
    "DEFAULT_PREVIEW_WIDTH",
    "PNG_MEDIA_TYPE",
    "PNG_SIGNATURE",
    "FakePreviewGenerator",
    "PreviewGenerator",
    "PreviewOutcome",
    "PreviewRender",
    "PreviewRequest",
    "looks_like_png",
]
