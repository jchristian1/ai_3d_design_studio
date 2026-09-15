"""blender-mcp: safe semantic Blender operations exposed through MCP.

Spec 001, Task 4 implements the first operation, move_object.

Layering (see .kiro/steering/security.md and tech.md):

    mcp_server.py        MCP exposure/registration — the only transport-aware layer
    tools/move_object.py domain logic — no bpy, no transport, fully unit-testable
    adapters/scene.py    the SceneAdapter Protocol (the seam)
    adapters/fake_scene  in-memory adapter for fast deterministic tests
    adapters/blender_*   the only module that imports bpy
    tolerance.py         the single position-comparison rule
    blender_runtime.py   locating/invoking the Blender executable

Focused semantic tools only. Arbitrary Python execution is not exposed.
"""
