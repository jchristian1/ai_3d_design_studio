"""Create an empty project file for a new design project.

Runs inside headless Blender (``--background --factory-startup``), so the result does
not depend on the user's preferences or add-ons.

Contract, identical in shape to the other scripts here:

  in  : STUDIO_PROJECT_PATH  — where to save the new .blend
  out : one JSON line on stdout, prefixed with STUDIO_RESULT:

The scene deliberately starts EMPTY — no default cube, no default camera, no default
light. A design starts from what the user asks for, and a stray cube in every new
project is something they would have to notice and delete. Metric units are set here
because metres are the canonical unit of this platform (.kiro/steering/blender.md).
"""

import json
import os
import sys

import bpy

RESULT_PREFIX = "STUDIO_RESULT:"


def main() -> int:
    destination = os.environ.get("STUDIO_PROJECT_PATH")
    if not destination:
        print(f"{RESULT_PREFIX}{json.dumps({'error': 'STUDIO_PROJECT_PATH is not set'})}")
        return 2

    bpy.ops.wm.read_factory_settings(use_empty=True)

    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"
    scene.unit_settings.scale_length = 1.0

    bpy.ops.wm.save_as_mainfile(filepath=destination)

    print(
        RESULT_PREFIX
        + json.dumps(
            {
                "created": True,
                "object_count": len(bpy.data.objects),
                "unit_system": scene.unit_settings.system,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
