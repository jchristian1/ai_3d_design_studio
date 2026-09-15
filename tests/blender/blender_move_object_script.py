"""Runs inside headless Blender to exercise the real move_object path.

Spec 001, Task 4. Invoked by tests/blender/test_move_object_blender.py via:

    blender --background --factory-startup --python <this file>

It builds a Cube at the origin, executes the SAME retry-safe plan twice through
the real BlenderSceneAdapter, and prints one JSON line per phase prefixed with
RESULT_JSON: so the test can parse results without depending on Blender's other
console output.

Import paths arrive through STUDIO_PYTHONPATH because Blender uses its own
bundled interpreter and does not see the repository's pytest configuration.
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

    from blender_mcp.adapters.blender_scene import (
        OBJECT_ID_PROPERTY,
        BlenderSceneAdapter,
    )
    from blender_mcp.mcp_server import handle_move_object
    from studio_types import ObjectRef, Vec3

    # A clean, reproducible scene containing exactly one Cube at the origin.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
    cube = bpy.context.active_object
    cube.name = "Cube"
    cube[OBJECT_ID_PROPERTY] = "obj_cube001"
    bpy.context.view_layer.update()

    adapter = BlenderSceneAdapter(bpy)

    start = adapter.read_world_position("Cube")
    emit("start", {"position": [start.x, start.y, start.z]})

    # The same plan is used for both attempts — this is the crash/retry scenario.
    plan_request = {
        "job_id": "job_blender_1",
        "target": {"name": "Cube"},
        "expected_before_meters": {"x": 0.0, "y": 0.0, "z": 0.0},
        "delta_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
        "desired_after_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
    }

    first = handle_move_object(plan_request, adapter)
    after_first = adapter.read_world_position("Cube")
    emit(
        "first",
        {
            "result": first,
            "scene_position": [after_first.x, after_first.y, after_first.z],
        },
    )

    # Replay the identical plan, exactly as a worker would after a crash.
    second = handle_move_object(plan_request, adapter)
    after_second = adapter.read_world_position("Cube")
    emit(
        "second",
        {
            "result": second,
            "scene_position": [after_second.x, after_second.y, after_second.z],
        },
    )

    # A third attempt, to show stability rather than a one-off.
    third = handle_move_object(plan_request, adapter)
    after_third = adapter.read_world_position("Cube")
    emit(
        "third",
        {
            "result": third,
            "scene_position": [after_third.x, after_third.y, after_third.z],
        },
    )

    # Resolution by stable object id must work against a real .blend datablock.
    by_id = adapter.find_object(ObjectRef(object_id="obj_cube001"))
    emit("resolve_by_id", {"found": by_id is not None, "name": getattr(by_id, "name", None)})

    # A conflicting plan must not mutate the scene.
    conflict_request = dict(plan_request, job_id="job_blender_2")
    conflict = handle_move_object(conflict_request, adapter)
    emit("conflict_after_move", {"result": conflict})

    # Move on Z as well, proving world-space behaviour on another axis.
    z_request = {
        "job_id": "job_blender_3",
        "target": {"name": "Cube"},
        "expected_before_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
        "delta_meters": {"x": 0.0, "y": 0.0, "z": 2.4},
        "desired_after_meters": {"x": 0.5, "y": 0.0, "z": 2.4},
    }
    z_result = handle_move_object(z_request, adapter)
    after_z = adapter.read_world_position("Cube")
    emit(
        "z_move",
        {"result": z_result, "scene_position": [after_z.x, after_z.y, after_z.z]},
    )

    # A true scene conflict: something else moved the Cube, so a plan that
    # expects the old position must refuse to touch it.
    cube.matrix_world.translation = (0.25, 0.0, 0.0)
    bpy.context.view_layer.update()
    conflict_plan = {
        "job_id": "job_blender_5",
        "target": {"name": "Cube"},
        "expected_before_meters": {"x": 0.0, "y": 0.0, "z": 0.0},
        "delta_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
        "desired_after_meters": {"x": 0.5, "y": 0.0, "z": 0.0},
    }
    true_conflict = handle_move_object(conflict_plan, adapter)
    after_conflict = adapter.read_world_position("Cube")
    emit(
        "true_conflict",
        {
            "result": true_conflict,
            "scene_position": [
                after_conflict.x,
                after_conflict.y,
                after_conflict.z,
            ],
        },
    )

    # A genuinely missing object must be reported, not invented.
    missing = handle_move_object(
        dict(plan_request, job_id="job_blender_4", target={"name": "NoSuchObject"}),
        adapter,
    )
    emit("missing_object", {"result": missing})

    emit("done", {"ok": True})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        emit("error", {"traceback": traceback.format_exc()})
        sys.exit(1)
