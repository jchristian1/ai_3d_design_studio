"""Apply one MoveObjectPlan and save the project. Runs INSIDE Blender.

Spec 001, Task 6. The decision logic lives in the Task 4 MCP handler; this script
only opens the project, delegates, and saves.

Environment:
    WORKER_BLEND       path of the .blend to open and mutate
    WORKER_PLAN        JSON MoveObjectPlan
    STUDIO_PYTHONPATH  import roots
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

    blend_path = os.environ["WORKER_BLEND"]
    plan = json.loads(os.environ["WORKER_PLAN"])

    bpy.ops.wm.open_mainfile(filepath=blend_path)
    bpy.context.view_layer.update()

    adapter = BlenderSceneAdapter(bpy)
    result = handle_move_object(plan, adapter)

    # Save only when the operation succeeded. A failure must not persist a
    # partially mutated scene.
    saved = False
    if "error" not in result:
        bpy.ops.wm.save_mainfile()
        saved = True

    emit("result", {"result": result, "saved": saved})
    emit("done", {"ok": True})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        emit("error", {"traceback": traceback.format_exc()})
        sys.exit(1)
