"""The platform capability vocabulary, re-exported for worker-side use.

The names themselves are a CONTRACT, derived from
``packages/contracts/schemas/capability-name.schema.json`` and defined once in
``studio_contracts.capabilities``. Both the agent and the worker import that module, so
neither service owns the vocabulary and the two cannot drift.

Two families exist:

* **Platform capabilities** — a named operation with typed arguments the platform fully
  understands (``move_object``, ``create_wall``, ``export_glb``). The platform owns the
  Blender Python, so these never require user approval.
* **``execute_blender_python``** — a body authored by the model. The platform does not
  pretend to understand it; it is classified before execution (see ``classifier.py``) and
  may require explicit, per-operation user approval.
"""

from __future__ import annotations

from studio_contracts.capabilities import (  # noqa: F401  (re-exported vocabulary)
    ARCHITECTURE_CAPABILITIES,
    ARTIFACT_CAPABILITIES,
    CAPABILITY_NAMES,
    CREATE_CEILING,
    CREATE_DOOR_PLACEHOLDER,
    CREATE_FLOOR,
    CREATE_OBJECT,
    CREATE_OPENING,
    CREATE_WALL,
    CREATE_WINDOW_PLACEHOLDER,
    DELETE_OBJECT,
    DUPLICATE_OBJECT,
    EXECUTE_BLENDER_PYTHON,
    EXPORT_GLB,
    INSPECT_OBJECT,
    INSPECT_SCENE,
    MATERIAL_CAPABILITIES,
    MOVE_OBJECT,
    MUTATING_CAPABILITIES,
    OBJECT_CAPABILITIES,
    PLATFORM_CAPABILITIES,
    PROPOSABLE_CAPABILITIES,
    READ_CAPABILITIES,
    RENDER_PREVIEW,
    ROTATE_OBJECT,
    SCALE_OBJECT,
    SET_MATERIAL_COLOR,
    SET_OBJECT_DIMENSIONS,
    TRANSFORM_CAPABILITIES,
    is_known_capability,
    is_mutating_capability,
    is_platform_capability,
    is_proposable_capability,
)

#: Every capability, including the model-authored code path. Kept as an alias because
#: the worker reads more naturally as "all capabilities".
ALL_CAPABILITIES = CAPABILITY_NAMES

#: Capabilities that only read, and so never save or create a recovery point.
READ_ONLY_CAPABILITIES = (*READ_CAPABILITIES, *ARTIFACT_CAPABILITIES)

#: Retained spelling for worker-side callers that predate the shared module.
SEMANTIC_CAPABILITIES = PLATFORM_CAPABILITIES


def is_semantic_capability(name: str) -> bool:
    """Deprecated alias for :func:`is_platform_capability`."""
    return is_platform_capability(name)
