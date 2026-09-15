"""The platform capability vocabulary.

These are the names the agent reasons about. They are deliberately independent of
the Blender backend: no official MCP tool name, and no Blender Python, appears
here or anywhere above ``BlenderCapabilityProvider``.

Two families exist:

* **Semantic capabilities** — a named operation with typed arguments the platform
  fully understands (``move_object``, ``create_wall``, ``export_glb``). The platform
  owns the Python for these, so they never require user approval.
* **The code capability** — ``execute_blender_python``, whose body is authored by
  the model. The platform does not pretend to understand it. It is classified
  before execution (see ``classifier.py``) and may require explicit user approval.

Adding a capability means adding it here, mapping it in the registry, and giving
it either platform-owned Python or a semantic argument contract.
"""

from __future__ import annotations

from typing import Final

# --- read -----------------------------------------------------------------
INSPECT_SCENE: Final = "inspect_scene"
INSPECT_OBJECT: Final = "inspect_object"

# --- transform ------------------------------------------------------------
MOVE_OBJECT: Final = "move_object"
ROTATE_OBJECT: Final = "rotate_object"
SCALE_OBJECT: Final = "scale_object"
SET_OBJECT_DIMENSIONS: Final = "set_object_dimensions"

# --- object ---------------------------------------------------------------
CREATE_OBJECT: Final = "create_object"
DUPLICATE_OBJECT: Final = "duplicate_object"
DELETE_OBJECT: Final = "delete_object"

# --- material -------------------------------------------------------------
SET_MATERIAL_COLOR: Final = "set_material_color"

# --- architecture ---------------------------------------------------------
CREATE_WALL: Final = "create_wall"
CREATE_FLOOR: Final = "create_floor"
CREATE_CEILING: Final = "create_ceiling"
CREATE_OPENING: Final = "create_opening"
CREATE_DOOR_PLACEHOLDER: Final = "create_door_placeholder"
CREATE_WINDOW_PLACEHOLDER: Final = "create_window_placeholder"

# --- artifact -------------------------------------------------------------
RENDER_PREVIEW: Final = "render_preview"
EXPORT_GLB: Final = "export_glb"

# --- model-authored code --------------------------------------------------
EXECUTE_BLENDER_PYTHON: Final = "execute_blender_python"

READ_CAPABILITIES: Final = (
    INSPECT_SCENE,
    INSPECT_OBJECT,
)

TRANSFORM_CAPABILITIES: Final = (
    MOVE_OBJECT,
    ROTATE_OBJECT,
    SCALE_OBJECT,
    SET_OBJECT_DIMENSIONS,
)

OBJECT_CAPABILITIES: Final = (
    CREATE_OBJECT,
    DUPLICATE_OBJECT,
    DELETE_OBJECT,
)

MATERIAL_CAPABILITIES: Final = (SET_MATERIAL_COLOR,)

ARCHITECTURE_CAPABILITIES: Final = (
    CREATE_WALL,
    CREATE_FLOOR,
    CREATE_CEILING,
    CREATE_OPENING,
    CREATE_DOOR_PLACEHOLDER,
    CREATE_WINDOW_PLACEHOLDER,
)

ARTIFACT_CAPABILITIES: Final = (
    RENDER_PREVIEW,
    EXPORT_GLB,
)

#: Capabilities whose Python the platform owns and reviewed.
SEMANTIC_CAPABILITIES: Final = (
    *READ_CAPABILITIES,
    *TRANSFORM_CAPABILITIES,
    *OBJECT_CAPABILITIES,
    *MATERIAL_CAPABILITIES,
    *ARCHITECTURE_CAPABILITIES,
    *ARTIFACT_CAPABILITIES,
)

#: Every capability the platform knows, including the model-authored code path.
ALL_CAPABILITIES: Final = (*SEMANTIC_CAPABILITIES, EXECUTE_BLENDER_PYTHON)

#: Capabilities that change the project and therefore need the durability wrapper.
MUTATING_CAPABILITIES: Final = (
    *TRANSFORM_CAPABILITIES,
    *OBJECT_CAPABILITIES,
    *MATERIAL_CAPABILITIES,
    *ARCHITECTURE_CAPABILITIES,
    EXECUTE_BLENDER_PYTHON,
)

#: Capabilities that only read, and therefore never save or create a recovery point.
READ_ONLY_CAPABILITIES: Final = (*READ_CAPABILITIES, *ARTIFACT_CAPABILITIES)


def is_known_capability(name: str) -> bool:
    return name in ALL_CAPABILITIES


def is_mutating_capability(name: str) -> bool:
    return name in MUTATING_CAPABILITIES


def is_semantic_capability(name: str) -> bool:
    return name in SEMANTIC_CAPABILITIES
