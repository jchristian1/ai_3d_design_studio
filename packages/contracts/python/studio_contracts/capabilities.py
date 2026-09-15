"""The platform capability vocabulary, read from the canonical schema.

The schema is the single source of truth: this module derives its constants from
``capability-name.schema.json`` rather than restating the list, so the two cannot
drift. Both the agent and the worker import from here, which is why it lives in
``studio_contracts`` and not inside either service.
"""

from __future__ import annotations

from typing import Final

from .schema import schema_enum

SCHEMA_NAME: Final = "capability-name.schema.json"

#: Every capability the platform knows.
CAPABILITY_NAMES: Final[tuple[str, ...]] = tuple(schema_enum(SCHEMA_NAME))

# --- individual names, so callers get an import error rather than a typo ---
INSPECT_SCENE: Final = "inspect_scene"
INSPECT_OBJECT: Final = "inspect_object"
MOVE_OBJECT: Final = "move_object"
ROTATE_OBJECT: Final = "rotate_object"
SCALE_OBJECT: Final = "scale_object"
SET_OBJECT_DIMENSIONS: Final = "set_object_dimensions"
CREATE_OBJECT: Final = "create_object"
DUPLICATE_OBJECT: Final = "duplicate_object"
DELETE_OBJECT: Final = "delete_object"
SET_MATERIAL_COLOR: Final = "set_material_color"
CREATE_WALL: Final = "create_wall"
CREATE_FLOOR: Final = "create_floor"
CREATE_CEILING: Final = "create_ceiling"
CREATE_OPENING: Final = "create_opening"
CREATE_DOOR_PLACEHOLDER: Final = "create_door_placeholder"
CREATE_WINDOW_PLACEHOLDER: Final = "create_window_placeholder"
RENDER_PREVIEW: Final = "render_preview"
EXPORT_GLB: Final = "export_glb"
EXECUTE_BLENDER_PYTHON: Final = "execute_blender_python"

READ_CAPABILITIES: Final = (INSPECT_SCENE, INSPECT_OBJECT)

TRANSFORM_CAPABILITIES: Final = (
    MOVE_OBJECT,
    ROTATE_OBJECT,
    SCALE_OBJECT,
    SET_OBJECT_DIMENSIONS,
)

OBJECT_CAPABILITIES: Final = (CREATE_OBJECT, DUPLICATE_OBJECT, DELETE_OBJECT)

MATERIAL_CAPABILITIES: Final = (SET_MATERIAL_COLOR,)

ARCHITECTURE_CAPABILITIES: Final = (
    CREATE_WALL,
    CREATE_FLOOR,
    CREATE_CEILING,
    CREATE_OPENING,
    CREATE_DOOR_PLACEHOLDER,
    CREATE_WINDOW_PLACEHOLDER,
)

ARTIFACT_CAPABILITIES: Final = (RENDER_PREVIEW, EXPORT_GLB)

#: Capabilities whose Blender Python the platform owns.
PLATFORM_CAPABILITIES: Final = (
    *READ_CAPABILITIES,
    *TRANSFORM_CAPABILITIES,
    *OBJECT_CAPABILITIES,
    *MATERIAL_CAPABILITIES,
    *ARCHITECTURE_CAPABILITIES,
    *ARTIFACT_CAPABILITIES,
)

#: Capabilities that change the project, and so need the durability wrapper.
MUTATING_CAPABILITIES: Final = (
    *TRANSFORM_CAPABILITIES,
    *OBJECT_CAPABILITIES,
    *MATERIAL_CAPABILITIES,
    *ARCHITECTURE_CAPABILITIES,
    EXECUTE_BLENDER_PYTHON,
)

#: Capabilities the agent is allowed to propose. ``render_preview`` and ``export_glb``
#: are platform-initiated: the pipeline runs them after a successful mutation, so the
#: model never asks for them and cannot spend time on them.
PROPOSABLE_CAPABILITIES: Final = (
    *READ_CAPABILITIES,
    *TRANSFORM_CAPABILITIES,
    *OBJECT_CAPABILITIES,
    *MATERIAL_CAPABILITIES,
    *ARCHITECTURE_CAPABILITIES,
    EXECUTE_BLENDER_PYTHON,
)


def is_known_capability(name: str) -> bool:
    return name in CAPABILITY_NAMES


def is_mutating_capability(name: str) -> bool:
    return name in MUTATING_CAPABILITIES


def is_platform_capability(name: str) -> bool:
    return name in PLATFORM_CAPABILITIES


def is_proposable_capability(name: str) -> bool:
    return name in PROPOSABLE_CAPABILITIES
