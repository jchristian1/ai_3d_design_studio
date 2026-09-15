"""Read one object's world position from a saved .blend. Runs INSIDE Blender.

Spec 001, Task 6. Because it opens the file fresh, it necessarily reports
PERSISTED state — which is what the worker needs both to capture
expected_before and to confirm a save was durable.

Environment:
    WORKER_BLEND       path of the .blend to open
    WORKER_TARGET      JSON ObjectRef {"object_id": ..., "name": ...}
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
    from studio_types import ObjectRef

    blend_path = os.environ["WORKER_BLEND"]
    target = json.loads(os.environ["WORKER_TARGET"])

    bpy.ops.wm.open_mainfile(filepath=blend_path)
    bpy.context.view_layer.update()

    adapter = BlenderSceneAdapter(bpy)
    state = adapter.find_object(
        ObjectRef(object_id=target.get("object_id"), name=target.get("name"))
    )

    if state is None:
        emit("position", {"found": False})
    else:
        emit(
            "position",
            {
                "found": True,
                "name": state.name,
                "object_id": state.object_id,
                "movable": state.movable,
                "position": [
                    state.position_meters.x,
                    state.position_meters.y,
                    state.position_meters.z,
                ],
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
