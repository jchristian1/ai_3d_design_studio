"""The real Blender scene adapter — the ONLY module that touches bpy.

Spec 001, Task 4.

Import is lazy and guarded: importing this module outside Blender must not blow
up, so the rest of the service (and its unit tests) stay runnable on a machine
without Blender.

World space
-----------
Positions are read and written through ``matrix_world.translation`` rather than
``object.location``. ``location`` is parent-relative; for an unparented object the
two agree, but reading ``location`` would silently return the wrong number for a
parented object. Writing ``matrix_world.translation`` lets Blender recompute the
local transform, so the contract ("world-space meters") holds either way.

Units
-----
Blender's internal unit is already meters, so no conversion happens here. The
scene's unit *display* setting does not affect the stored values.
"""

from __future__ import annotations

from typing import Any, Optional

from studio_types import ObjectRef, Vec3

from .scene import SceneAdapterError, SceneObjectState

#: Custom property that carries the platform's stable object id inside a .blend
#: (see .kiro/steering/blender.md object identity).
OBJECT_ID_PROPERTY = "studio_object_id"


def bpy_module() -> Any:
    """Return the bpy module, or raise a clear error when not inside Blender."""
    try:
        import bpy  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on runtime
        raise SceneAdapterError(
            "bpy is not available; this adapter must run inside Blender"
        ) from exc
    return bpy


def is_blender_runtime() -> bool:
    """True when running inside Blender (bpy importable)."""
    try:
        import bpy  # noqa: F401  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return False
    return True


class BlenderSceneAdapter:
    """Semantic, narrowly-scoped access to the live Blender scene."""

    def __init__(self, bpy: Optional[Any] = None) -> None:
        self._bpy = bpy if bpy is not None else bpy_module()

    # -- internals ---------------------------------------------------------

    def _object_id_of(self, obj: Any) -> Optional[str]:
        try:
            value = obj.get(OBJECT_ID_PROPERTY)
        except Exception:  # pragma: no cover - defensive
            return None
        return str(value) if value else None

    def _immovable_reason(self, obj: Any) -> Optional[str]:
        """Why this object cannot be moved, or None when it can.

        Linked (library) objects cannot be edited in this file, and a locked
        location axis is an explicit user instruction not to move it. Neither is
        retryable, so the service reports OBJECT_NOT_MOVABLE rather than failing
        the write later.
        """
        if getattr(obj, "library", None) is not None:
            return "object is linked from an external library"
        lock = getattr(obj, "lock_location", None)
        if lock is not None and any(bool(axis) for axis in lock):
            locked = [
                axis
                for axis, is_locked in zip("xyz", lock)
                if bool(is_locked)
            ]
            return f"location is locked on {', '.join(locked)}"
        return None

    def _state_of(self, obj: Any) -> SceneObjectState:
        translation = obj.matrix_world.translation
        reason = self._immovable_reason(obj)
        return SceneObjectState(
            handle=obj.name,
            name=obj.name,
            position_meters=Vec3(
                float(translation.x), float(translation.y), float(translation.z)
            ),
            movable=reason is None,
            object_id=self._object_id_of(obj),
            immovable_reason=reason,
        )

    def _get(self, handle: str) -> Optional[Any]:
        return self._bpy.data.objects.get(handle)

    def _update(self) -> None:
        """Flush dependency-graph changes so a re-read sees the new transform."""
        try:
            self._bpy.context.view_layer.update()
        except Exception:  # pragma: no cover - headless contexts vary
            pass

    # -- SceneAdapter ------------------------------------------------------

    def find_object(self, ref: ObjectRef) -> Optional[SceneObjectState]:
        """Exact-match resolution: stable id first, then name.

        No fuzzy matching and no broad scene search — an ambiguous or approximate
        match would risk mutating the wrong object.
        """
        object_id = getattr(ref, "object_id", None)
        if object_id:
            for obj in self._bpy.data.objects:
                if self._object_id_of(obj) == object_id:
                    return self._state_of(obj)
            return None

        name = getattr(ref, "name", None)
        if name:
            obj = self._get(name)
            return None if obj is None else self._state_of(obj)
        return None

    def set_world_position(self, handle: str, position: Vec3) -> None:
        obj = self._get(handle)
        if obj is None:
            raise SceneAdapterError(f"object '{handle}' is no longer in the scene")
        try:
            obj.matrix_world.translation = (
                float(position.x),
                float(position.y),
                float(position.z),
            )
            self._update()
        except SceneAdapterError:
            raise
        except Exception as exc:
            raise SceneAdapterError(
                f"failed to set world position of '{handle}': {exc}"
            ) from exc

    def read_world_position(self, handle: str) -> Optional[Vec3]:
        obj = self._get(handle)
        if obj is None:
            return None
        self._update()
        translation = obj.matrix_world.translation
        return Vec3(
            float(translation.x), float(translation.y), float(translation.z)
        )
