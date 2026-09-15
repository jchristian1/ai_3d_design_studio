"""Runs move_object against a .blend working copy. Runs INSIDE Blender.

Spec 001, Task 5. Proves the Task 4 MCP operation works against the seed
fixture, and that mutations land in the working copy only.

Environment:
    MOVE_BLEND         path of the .blend working copy to open and mutate
    MOVE_PLAN          JSON MoveObjectPlan document
    MOVE_SAVE          "1" to save the mutated file (default), "0" to leave it
    STUDIO_PYTHONPATH  os.pathsep-joined import roots for the studio packages
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

    from blender_mcp.adapters.blender_scene import BlenderSceneAdapter
    from blender_mcp.mcp_server import handle_move_object

    blend_path = os.environ["MOVE_BLEND"]
    plan = json.loads(os.environ["MOVE_PLAN"])
    should_save = os.environ.get("MOVE_SAVE", "1") == "1"

    bpy.ops.wm.open_mainfile(filepath=blend_path)
    bpy.context.view_layer.update()

    adapter = BlenderSceneAdapter(bpy)

    before = adapter.read_world_position("Cube")
    emit("before", {"position": [before.x, before.y, before.z]})

    result = handle_move_object(plan, adapter)

    after = adapter.read_world_position("Cube")
    emit(
        "moved",
        {"result": result, "position": [after.x, after.y, after.z]},
    )

    if should_save:
        bpy.ops.wm.save_mainfile()
        emit("saved", {"path": bpy.data.filepath})

    emit("done", {"ok": True})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        emit("error", {"traceback": traceback.format_exc()})
        sys.exit(1)
