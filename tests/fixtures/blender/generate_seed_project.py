"""Generates the deterministic seed Blender project. Runs INSIDE Blender.

Spec 001, Task 5. Invoked through the centralized runtime helper, never by a
hand-typed Blender path:

    blender --background --factory-startup --python generate_seed_project.py

Environment:
    SEED_SPEC          path to seed_project.spec.json (required)
    SEED_OUTPUT        path of the .blend to write (required)
    STUDIO_PYTHONPATH  os.pathsep-joined import roots for the studio packages

Everything the scene contains comes from the spec file, so the fixture and its
documented contract cannot drift apart. Nothing is hand-placed.

Determinism: the scene starts from factory settings with an EMPTY scene, so no
default cube/camera/light appears and no user preference or add-on can influence
the result. The .blend container itself is not byte-reproducible (Blender embeds
its version and paths); the reproducible part is the scene state, which
inspect_blend.py verifies.
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


def emit(phase: str, payload: dict) -> None:
    print(f"{RESULT_PREFIX}{json.dumps({'phase': phase, **payload})}", flush=True)


def main() -> int:
    import bpy

    # The stable-id mechanism comes from the adapter, so the generator and the
    # runtime read/write the same custom property by construction.
    from blender_mcp.adapters.blender_scene import OBJECT_ID_PROPERTY

    spec_path = os.environ["SEED_SPEC"]
    output_path = os.environ["SEED_OUTPUT"]

    with open(spec_path, "r", encoding="utf-8") as handle:
        spec = json.load(handle)

    if spec["object_id_property"] != OBJECT_ID_PROPERTY:
        raise RuntimeError(
            "spec object_id_property "
            f"{spec['object_id_property']!r} does not match the adapter's "
            f"{OBJECT_ID_PROPERTY!r}"
        )

    scene_spec = spec["scene"]

    # ---- factory startup, empty scene -----------------------------------
    bpy.ops.wm.read_factory_settings(use_empty=bool(scene_spec["start_empty"]))

    # ---- configure units -------------------------------------------------
    units = bpy.context.scene.unit_settings
    units.system = scene_spec["unit_system"]
    units.scale_length = float(scene_spec["scale_length"])
    units.length_unit = scene_spec["length_unit"]

    # ---- create objects --------------------------------------------------
    for object_spec in spec["objects"]:
        if object_spec["primitive"] != "cube":
            raise RuntimeError(
                f"unsupported primitive {object_spec['primitive']!r}; the seed "
                "fixture intentionally supports only what Spec 001 needs"
            )

        location = object_spec["location_meters"]
        bpy.ops.mesh.primitive_cube_add(
            size=float(object_spec["size_meters"]),
            location=(
                float(location["x"]),
                float(location["y"]),
                float(location["z"]),
            ),
        )
        obj = bpy.context.active_object
        obj.name = object_spec["name"]

        # ---- assign the stable machine object id ------------------------
        obj[OBJECT_ID_PROPERTY] = object_spec["object_id"]

        # ---- explicit world transform ----------------------------------
        # Set explicitly rather than relying on primitive_cube_add defaults, so
        # the fixture does not depend on Blender's default values.
        rotation = object_spec["rotation_euler_radians"]
        scale = object_spec["scale"]
        obj.rotation_euler = (
            float(rotation["x"]),
            float(rotation["y"]),
            float(rotation["z"]),
        )
        obj.scale = (float(scale["x"]), float(scale["y"]), float(scale["z"]))
        obj.matrix_world.translation = (
            float(location["x"]),
            float(location["y"]),
            float(location["z"]),
        )

    bpy.context.view_layer.update()

    # ---- verify before saving -------------------------------------------
    # A generator that saves an incorrect scene is worse than one that fails.
    actual_names = sorted(o.name for o in bpy.data.objects)
    expected_names = sorted(scene_spec["expected_object_names"])
    if actual_names != expected_names:
        raise RuntimeError(
            f"scene contains {actual_names}, expected {expected_names}"
        )

    # ---- save -------------------------------------------------------------
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=output_path, compress=False)

    emit(
        "generated",
        {
            "output": output_path,
            "fixture_version": spec["fixture_version"],
            "objects": actual_names,
        },
    )
    emit("done", {"ok": True})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        emit("error", {"traceback": traceback.format_exc()})
        sys.exit(1)
