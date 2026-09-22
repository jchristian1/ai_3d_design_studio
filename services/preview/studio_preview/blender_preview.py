"""The real preview generator: a headless Blender subprocess.

Spec 001, Task 10.

    PreviewGenerator (generator.py)
        v
    BlenderPreviewGenerator   (this module — host side, no bpy)
        v
    blender --background --factory-startup --python render_preview.py
        v
    PNG bytes

Blender is located through ``blender_mcp.blender_runtime`` (Task 4). No executable
path appears here, and no new discovery logic is introduced — there is exactly one
place in the repository that knows how to find Blender.

Isolation
---------
This module runs on the host and imports no bpy. Only ``blender_scripts/`` runs
inside Blender. A subprocess per preview is simple and crash-isolated: a render
that hangs or segfaults cannot take the worker down, and the timeout is enforced by
the parent. The cost is process startup (roughly a second), which is acceptable for
a preview and is the same trade-off Task 6 made for mutations.

Failure is a value
------------------
Every failure path — Blender absent, non-zero exit, timeout, missing or empty
output, an unreadable project — returns a structured ``PreviewOutcome`` with a
canonical error code. Nothing raises, because a failed preview must never be able
to make a durably-saved mutation look like a failed one.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from blender_mcp.blender_runtime import find_blender_executable, run_blender_script

from .generator import (
    PNG_MEDIA_TYPE,
    PreviewOutcome,
    PreviewRender,
    PreviewRequest,
    looks_like_png,
)

PREVIEW_SCRIPTS = Path(__file__).resolve().parent / "blender_scripts"
RENDER_SCRIPT = PREVIEW_SCRIPTS / "render_preview.py"

RESULT_PREFIX = "RESULT_JSON:"

#: Import roots Blender's bundled interpreter needs. It does not read this
#: repository's pytest configuration or the editable install.
REPO_ROOT = Path(__file__).resolve().parents[3]
STUDIO_PATHS = (
    REPO_ROOT / "packages" / "types" / "python",
    REPO_ROOT / "packages" / "contracts" / "python",
    REPO_ROOT / "packages" / "validation" / "python",
    REPO_ROOT / "packages" / "spatial" / "python",
    REPO_ROOT / "services" / "blender-mcp",
)

#: A preview must be fast. This bound exists so a wedged Blender degrades the
#: preview rather than stalling the job that already succeeded.
#:
#: EEVEE compiles shaders on its first render in a process (~10 s observed), and
#: every render after that is a fraction of a second. The bound covers the cold
#: case with room to spare rather than tracking the warm one.
DEFAULT_TIMEOUT_SECONDS = 180

#: The engine the render script sets. On the pinned Blender 5.2 the identifier is
#: ``BLENDER_EEVEE`` — the "Next" rewrite became the default EEVEE and the
#: ``_NEXT`` suffix was dropped, so ``BLENDER_EEVEE_NEXT`` does not exist there.
ENGINE = "BLENDER_EEVEE"


class BlenderPreviewGenerator:
    """Renders a preview PNG in a fresh headless Blender process."""

    name = "blender_eevee"

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    # -- PreviewGenerator --------------------------------------------------

    def generate(self, request: PreviewRequest) -> PreviewOutcome:
        project_path = Path(request.project_path)
        if not project_path.exists():
            return PreviewOutcome.failure(
                "INTERNAL_ERROR", "the project to preview is not available"
            )

        if find_blender_executable() is None:
            return PreviewOutcome.failure(
                "BLENDER_UNAVAILABLE",
                "Blender is not available on this worker, so no preview could be "
                "generated",
            )

        # The output location is chosen HERE, on the server, in a temporary
        # directory this process owns. It is never derived from a request field,
        # and it never leaves this method.
        with tempfile.TemporaryDirectory(prefix="studio-preview-") as temp_dir:
            output_path = Path(temp_dir) / "preview.png"
            try:
                proc = run_blender_script(
                    str(RENDER_SCRIPT),
                    env=self._env(request, output_path),
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return PreviewOutcome.failure(
                    "INTERNAL_ERROR",
                    "preview rendering took too long and was stopped",
                )
            except OSError as exc:
                return PreviewOutcome.failure(
                    "BLENDER_UNAVAILABLE", f"Blender could not be started: {exc}"
                )

            phases = self._parse_phases(proc.stdout)
            if "done" not in phases:
                # Blender's stdout can contain absolute paths, so it is logged by
                # the caller at most and never placed in a user-facing message.
                return PreviewOutcome.failure(
                    "INTERNAL_ERROR",
                    "preview rendering did not complete successfully",
                )

            if not output_path.exists():
                return PreviewOutcome.failure(
                    "INTERNAL_ERROR", "preview rendering produced no image"
                )

            data = output_path.read_bytes()

        if not data:
            return PreviewOutcome.failure(
                "INTERNAL_ERROR", "preview rendering produced an empty image"
            )
        if not looks_like_png(data):
            # Verify the bytes rather than trusting the exit code: a file that
            # exists is not necessarily a PNG.
            return PreviewOutcome.failure(
                "INTERNAL_ERROR", "preview rendering did not produce a PNG image"
            )

        rendered = phases.get("rendered", {})
        scene = phases.get("scene", {})
        return PreviewOutcome.success(
            PreviewRender(
                image_bytes=data,
                media_type=PNG_MEDIA_TYPE,
                width=int(scene.get("width", request.width)),
                height=int(scene.get("height", request.height)),
                engine=str(rendered.get("engine", ENGINE)),
            )
        )

    # -- internals ---------------------------------------------------------

    def _env(self, request: PreviewRequest, output_path: Path) -> dict[str, str]:
        return {
            "STUDIO_PYTHONPATH": os.pathsep.join(
                str(path) for path in STUDIO_PATHS
            ),
            "PREVIEW_BLEND": str(request.project_path),
            "PREVIEW_OUTPUT": str(output_path),
            "PREVIEW_WIDTH": str(int(request.width)),
            "PREVIEW_HEIGHT": str(int(request.height)),
            # JSON rather than a delimited list: an object id is platform-generated
            # and safe, but encoding a collection by hand is how a stray separator
            # silently becomes two ids.
            "PREVIEW_FOCUS": json.dumps(list(request.focus_object_ids)),
        }

    @staticmethod
    def _parse_phases(stdout: str) -> dict[str, dict[str, Any]]:
        phases: dict[str, dict[str, Any]] = {}
        for line in (stdout or "").splitlines():
            if line.startswith(RESULT_PREFIX):
                try:
                    payload = json.loads(line[len(RESULT_PREFIX) :])
                except ValueError:  # pragma: no cover - malformed line
                    continue
                phase = payload.pop("phase", None)
                if isinstance(phase, str):
                    phases[phase] = payload
        return phases


def default_preview_generator(
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> BlenderPreviewGenerator:
    """The generator Spec 001 runs with."""
    return BlenderPreviewGenerator(timeout_seconds=timeout_seconds)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ENGINE",
    "RENDER_SCRIPT",
    "BlenderPreviewGenerator",
    "default_preview_generator",
]
