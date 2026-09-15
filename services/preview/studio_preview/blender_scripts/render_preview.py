"""Renders a deterministic preview image. Runs INSIDE Blender.

Spec 001, Task 10. Invoked through the centralized runtime helper, never by a
hand-typed Blender path:

    blender --background --factory-startup --python render_preview.py

Environment:
    PREVIEW_BLEND       path of the .blend to render (required, server-resolved)
    PREVIEW_OUTPUT      path of the PNG to write (required, server-chosen)
    PREVIEW_WIDTH       pixel width (required)
    PREVIEW_HEIGHT      pixel height (required)
    STUDIO_PYTHONPATH   os.pathsep-joined import roots for the studio packages

THE CENTRAL RULE: THIS SCRIPT NEVER SAVES THE PROJECT
-----------------------------------------------------
A preview is a READ of a saved design. This script opens the .blend, adds a
temporary camera to the in-memory scene, renders, and exits. There is no
``save_as_mainfile`` and no ``save_mainfile`` anywhere in it, so the design file on
disk is byte-identical afterwards.

That is what makes "add a camera" safe. The alternative — permanently adding a
camera to the user's scene so previews have something to render through — would
mean every preview silently edits the design, and the user would find objects they
never asked for. A test asserts the .blend's SHA-256 is unchanged by rendering.

CAMERA STRATEGY
---------------
A single fixed three-quarter camera, computed rather than hand-tuned:

    position  (7, -7, 5) metres, looking at the world origin

The look-at rotation is derived from the direction vector, so the framing is
deterministic and contains no magic Euler angles. The view is deliberately
off-axis on X so a movement along world +X appears as a clear horizontal
displacement — which is the entire point of the Spec 001 preview.

This is NOT automatic architectural camera composition. It frames a small scene
near the origin and nothing more. Framing a real room will need a bounding-box or
scene-aware strategy, and that is future work.

RENDER SETTINGS
---------------
Workbench, at a fixed small resolution, with a fixed flat background:

  - Workbench is a solid-shading rasteriser: no light sampling, so no noise, no
    seed dependence, and no denoiser version differences. Two runs of the same
    scene therefore produce the same image.
  - It uses its own studio lighting, independent of scene lights, so a preview
    works in a scene that has no lights at all — which the Spec 001 seed fixture
    does not.
  - It needs no GPU. A preview must not become a reason CI or a headless machine
    cannot verify the slice, so GPU is never required here. Cycles and RTX belong
    to the final-render path, not this one.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

for entry in os.environ.get("STUDIO_PYTHONPATH", "").split(os.pathsep):
    if entry and entry not in sys.path:
        sys.path.insert(0, entry)

RESULT_PREFIX = "RESULT_JSON:"

#: Temporary camera name. Prefixed so it is obviously not user content, and it
#: only ever exists in memory.
PREVIEW_CAMERA_NAME = "__studio_preview_camera"

#: Fixed three-quarter viewpoint, in metres.
CAMERA_LOCATION = (7.0, -7.0, 5.0)
CAMERA_TARGET = (0.0, 0.0, 0.0)
CAMERA_LENS_MM = 50.0

#: Flat, fixed background so the image is deterministic and obviously non-empty.
BACKGROUND_COLOR = (0.05, 0.05, 0.07)

ENGINE = "BLENDER_WORKBENCH"


def emit(phase: str, payload: dict) -> None:
    print(f"{RESULT_PREFIX}{json.dumps({'phase': phase, **payload})}", flush=True)


def main() -> int:
    import bpy
    import mathutils

    blend_path = os.environ["PREVIEW_BLEND"]
    output_path = os.environ["PREVIEW_OUTPUT"]
    width = int(os.environ["PREVIEW_WIDTH"])
    height = int(os.environ["PREVIEW_HEIGHT"])

    if not os.path.exists(blend_path):
        raise RuntimeError("the project file to preview does not exist")

    # ---- open the SAVED project (read-only intent) ---------------------
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    scene = bpy.context.scene

    # ---- temporary camera, added in memory only ------------------------
    camera_data = bpy.data.cameras.new(PREVIEW_CAMERA_NAME)
    camera_data.lens = CAMERA_LENS_MM
    camera = bpy.data.objects.new(PREVIEW_CAMERA_NAME, camera_data)
    scene.collection.objects.link(camera)

    camera.location = CAMERA_LOCATION
    # Derive the look-at rotation instead of hard-coding Euler angles, so the
    # framing follows from the geometry and stays correct if the viewpoint moves.
    direction = mathutils.Vector(CAMERA_TARGET) - mathutils.Vector(CAMERA_LOCATION)
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.camera = camera

    # ---- deterministic render settings ---------------------------------
    render = scene.render
    render.engine = ENGINE
    render.resolution_x = width
    render.resolution_y = height
    render.resolution_percentage = 100
    render.film_transparent = False
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    render.image_settings.compression = 15
    render.filepath = output_path

    # ---- strip embedded metadata (SECURITY + determinism) --------------
    # Blender stamps render metadata into PNG tEXt chunks. Left enabled, every
    # served preview embeds:
    #
    #     File\0/abs/path/to/the/project.blend      <- absolute server path
    #     Date\02026/09/15 05:40:12                 <- wall-clock timestamp
    #     RenderTime\000:00.24                      <- measured duration
    #     Camera, Scene, Frame, Time, ...
    #
    # The `File` chunk is a genuine information leak: the artifact is served to a
    # browser, so the absolute path of the design file — and the project filename,
    # which may be a client's name — would travel with the image. JSON responses
    # are carefully path-free; the image bytes must be too.
    #
    # Disabling stamping also makes the output byte-reproducible, because Date and
    # RenderTime are the only things that varied between two renders of an
    # unchanged scene. That turns the artifact checksum into a true content
    # identity, which is what makes it usable for caching and change detection.
    render.use_stamp = False  # no burn-in overlay
    for stamp_flag in (
        "use_stamp_filename",
        "use_stamp_date",
        "use_stamp_time",
        "use_stamp_render_time",
        "use_stamp_frame",
        "use_stamp_frame_range",
        "use_stamp_scene",
        "use_stamp_camera",
        "use_stamp_lens",
        "use_stamp_hostname",
        "use_stamp_memory",
        "use_stamp_note",
        "use_stamp_marker",
        "use_stamp_sequencer_strip",
    ):
        # setattr rather than direct assignment: the exact set of stamp flags
        # varies slightly between Blender versions, and an unknown flag must not
        # break rendering.
        if hasattr(render, stamp_flag):
            setattr(render, stamp_flag, False)

    # Workbench shading: solid, flat-lit, fixed background. No sampling.
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "MATERIAL"
    shading.background_type = "VIEWPORT"
    shading.background_color = BACKGROUND_COLOR
    # Anti-aliasing is fixed rather than adaptive so it cannot vary per run.
    scene.display.render_aa = "8"

    bpy.context.view_layer.update()

    visible = sorted(
        obj.name for obj in scene.objects if obj.name != PREVIEW_CAMERA_NAME
    )
    emit(
        "scene",
        {
            "objects": visible,
            "camera": PREVIEW_CAMERA_NAME,
            "engine": ENGINE,
            "width": width,
            "height": height,
        },
    )

    # ---- render ---------------------------------------------------------
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    bpy.ops.render.render(write_still=True)

    # Blender may append a frame number depending on the path; verify the exact
    # file we promised actually exists rather than assuming it does.
    if not os.path.exists(output_path):
        raise RuntimeError(f"render did not produce {output_path}")
    size = os.path.getsize(output_path)
    if size <= 0:
        raise RuntimeError("render produced an empty file")

    emit("rendered", {"output": output_path, "size_bytes": size, "engine": ENGINE})

    # NOTE: no save of any kind. The .blend is left exactly as it was found.
    emit("done", {"ok": True})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        emit("error", {"traceback": traceback.format_exc()})
        sys.exit(1)
