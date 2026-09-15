"""An in-memory scene adapter for deterministic tests.

Spec 001, Task 4. No bpy, no Blender, no I/O — so the entire domain behaviour of
move_object can be tested fast and without launching Blender.

It can also be told to fail a write, or to report a position that disagrees with
what was written, so the MUTATION_FAILED and VERIFY_FAILED paths are exercised
without needing a broken Blender.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from studio_types import ObjectRef, Vec3

from .scene import SceneAdapterError, SceneObjectState


@dataclass
class FakeSceneAdapter:
    """A scene held in a dict, addressed by object name."""

    objects: dict[str, SceneObjectState] = field(default_factory=dict)

    #: When set, ``set_world_position`` raises instead of writing.
    fail_write_with: Optional[str] = None

    #: When set, the position reported after a write, regardless of what was
    #: requested. Used to simulate a scene that silently disagrees.
    report_position_after_write: Optional[Vec3] = None

    #: Every write performed, in order. Lets tests assert that the
    #: already-applied and conflict paths mutate nothing at all.
    writes: list[tuple[str, Vec3]] = field(default_factory=list)

    # -- construction helpers ------------------------------------------------

    @classmethod
    def with_object(
        cls,
        name: str,
        position: Vec3,
        object_id: Optional[str] = None,
        movable: bool = True,
        immovable_reason: Optional[str] = None,
    ) -> "FakeSceneAdapter":
        adapter = cls()
        adapter.add_object(
            name,
            position,
            object_id=object_id,
            movable=movable,
            immovable_reason=immovable_reason,
        )
        return adapter

    def add_object(
        self,
        name: str,
        position: Vec3,
        object_id: Optional[str] = None,
        movable: bool = True,
        immovable_reason: Optional[str] = None,
    ) -> None:
        self.objects[name] = SceneObjectState(
            handle=name,
            name=name,
            position_meters=position,
            movable=movable,
            object_id=object_id,
            immovable_reason=immovable_reason,
        )

    # -- SceneAdapter ------------------------------------------------------

    def find_object(self, ref: ObjectRef) -> Optional[SceneObjectState]:
        object_id = getattr(ref, "object_id", None)
        if object_id:
            for state in self.objects.values():
                if state.object_id == object_id:
                    return state
            return None
        name = getattr(ref, "name", None)
        if name:
            return self.objects.get(name)
        return None

    def set_world_position(self, handle: str, position: Vec3) -> None:
        if self.fail_write_with is not None:
            raise SceneAdapterError(self.fail_write_with)
        state = self.objects.get(handle)
        if state is None:
            raise SceneAdapterError(f"object handle '{handle}' disappeared")
        stored = (
            self.report_position_after_write
            if self.report_position_after_write is not None
            else position
        )
        self.objects[handle] = replace(state, position_meters=stored)
        self.writes.append((handle, position))

    def read_world_position(self, handle: str) -> Optional[Vec3]:
        state = self.objects.get(handle)
        return None if state is None else state.position_meters
