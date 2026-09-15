"""Opens a .blend headlessly and reports its scene state. Runs INSIDE Blender.

Spec 001, Task 5.

Environment:
    INSPECT_BLEND      path of the .blend to open (required)
    STUDIO_PYTHONPATH  os.pathsep-joined import roots for the studio packages

Emits one RESULT_JSON line containing a machine-readable digest of exactly the
scene state Spec 001 depends on: units, object identities, world transforms and
dimensions. Deliberately not the whole file — the .blend container embeds a
Blender version and file paths and is not byte-reproducible, so comparing the
binary would be meaningless. Comparing this digest is meaningful.

Also used as the manual inspection tool: it is the fastest way to see what a
fixture actually contains without opening the Blender UI.
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


def num(value: float) -> float:
    """Round to 9 decimals and normalize -0.0 to 0.0.

    Blender reports an unset Euler component as -0.0. That is numerically equal
    to 0.0 but serializes differently, which would make the determinism digest
    spuriously unstable. Same normalization convention as packages/spatial.
    """
    rounded = round(float(value), 9)
    return 0.0 if rounded == 0 else rounded


def main() -> int:
    import bpy

    from blender_mcp.adapters.blender_scene import (
        OBJECT_ID_PROPERTY,
        BlenderSceneAdapter,
    )
    from studio_types import ObjectRef

    blend_path = os.environ["INSPECT_BLEND"]

    # Opening the file proves it can be read headlessly.
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    bpy.context.view_layer.update()

    scene = bpy.context.scene
    units = scene.unit_settings
    adapter = BlenderSceneAdapter(bpy)

    objects = []
    for obj in sorted(bpy.data.objects, key=lambda o: o.name):
        translation = obj.matrix_world.translation
        objects.append(
            {
                "name": obj.name,
                "object_id": obj.get(OBJECT_ID_PROPERTY),
                "type": obj.type,
                "world_position_meters": {
                    "x": num(translation.x),
                    "y": num(translation.y),
                    "z": num(translation.z),
                },
                "rotation_euler_radians": {
                    "x": num(obj.rotation_euler.x),
                    "y": num(obj.rotation_euler.y),
                    "z": num(obj.rotation_euler.z),
                },
                "scale": {
                    "x": num(obj.scale.x),
                    "y": num(obj.scale.y),
                    "z": num(obj.scale.z),
                },
                "dimensions_meters": {
                    "x": num(obj.dimensions.x),
                    "y": num(obj.dimensions.y),
                    "z": num(obj.dimensions.z),
                },
            }
        )

    digest = {
        "scene": {
            "name": scene.name,
            "unit_system": units.system,
            "length_unit": units.length_unit,
            "scale_length": num(units.scale_length),
        },
        "objects": objects,
    }

    emit("digest", {"digest": digest})

    # Prove the adapter resolves the object both ways, in a real .blend.
    by_name = adapter.find_object(ObjectRef(name="Cube"))
    by_id = adapter.find_object(ObjectRef(object_id="obj_cube001"))
    emit(
        "resolution",
        {
            "by_name": None
            if by_name is None
            else {
                "name": by_name.name,
                "object_id": by_name.object_id,
                "position": [
                    by_name.position_meters.x,
                    by_name.position_meters.y,
                    by_name.position_meters.z,
                ],
                "movable": by_name.movable,
            },
            "by_object_id": None
            if by_id is None
            else {"name": by_id.name, "object_id": by_id.object_id},
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
