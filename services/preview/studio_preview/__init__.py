"""Preview and artifact generation.

Spec 001, Task 10.

    canonical Job
        v
    WorkerExecutor          (mutate -> verify -> save)
        v
    PreviewGenerator        generator.py — the abstraction
        v
    Blender subprocess      blender_preview.py -> blender_scripts/render_preview.py
        v
    PNG bytes
        v
    ArtifactStore           artifacts.py — durable, project-scoped
        v
    control plane           reports (project_id, artifact_id); NEVER a path
        v
    GET /api/projects/{project_id}/artifacts/{artifact_id}

Modules:
    generator.py       PreviewGenerator Protocol, PreviewRequest/Render/Outcome,
                       FakePreviewGenerator
    artifacts.py       ArtifactStore Protocol, LocalArtifactStore, derived
                       artifact identity, checksums
    blender_preview.py BlenderPreviewGenerator — host side, subprocess, no bpy
    blender_scripts/   the only code that runs inside Blender and imports bpy

DELIBERATE IMPORT LAYERING
--------------------------
This package exports the artifact and generator BOUNDARIES only.
``BlenderPreviewGenerator`` is deliberately NOT re-exported here: it must be
imported from ``studio_preview.blender_preview`` explicitly.

That is not a style preference. The control plane needs ``ArtifactStore`` to serve
previews over HTTP, but it must never acquire a dependency on Blender, on
subprocess execution, or on the render scripts. Keeping the Blender generator
behind an explicit module import means the API's import graph stays clean and a
test can assert it — rather than relying on nobody noticing.
"""

from .artifacts import (
    ARTIFACT_ID_PATTERN,
    DEFAULT_ARTIFACT_ROOT,
    ArtifactError,
    ArtifactStore,
    LocalArtifactStore,
    UnsafeArtifactIdError,
    artifact_from_wire,
    assert_safe_artifact_id,
    checksum_of,
    derive_artifact_id,
    to_artifact_wire,
)
from .generator import (
    DEFAULT_PREVIEW_HEIGHT,
    DEFAULT_PREVIEW_WIDTH,
    PNG_MEDIA_TYPE,
    PNG_SIGNATURE,
    FakePreviewGenerator,
    PreviewGenerator,
    PreviewOutcome,
    PreviewRender,
    PreviewRequest,
    looks_like_png,
)

__all__ = [
    "ARTIFACT_ID_PATTERN",
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_PREVIEW_HEIGHT",
    "DEFAULT_PREVIEW_WIDTH",
    "PNG_MEDIA_TYPE",
    "PNG_SIGNATURE",
    "ArtifactError",
    "ArtifactStore",
    "FakePreviewGenerator",
    "LocalArtifactStore",
    "PreviewGenerator",
    "PreviewOutcome",
    "PreviewRender",
    "PreviewRequest",
    "UnsafeArtifactIdError",
    "artifact_from_wire",
    "assert_safe_artifact_id",
    "checksum_of",
    "derive_artifact_id",
    "looks_like_png",
    "to_artifact_wire",
]
