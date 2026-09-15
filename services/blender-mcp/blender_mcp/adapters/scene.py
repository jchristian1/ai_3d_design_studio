"""The scene adapter boundary.

Spec 001, Task 4.

    MCP handler
        v
    move_object service      <- domain logic, no bpy
        v
    SceneAdapter (this file) <- the seam
        v
    bpy

The service depends only on this Protocol, so domain behaviour is testable with
an in-memory fake and Blender is needed only for a small number of integration
tests. Nothing in this module imports bpy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from studio_types import ObjectRef, Vec3


class SceneAdapterError(Exception):
    """Raised by an adapter when the underlying scene operation fails.

    The service converts this into a structured MUTATION_FAILED error rather than
    letting an exception escape across the MCP boundary.
    """


@dataclass(frozen=True)
class SceneObjectState:
    """A snapshot of one scene object, as seen through the adapter.

    ``handle`` is the adapter's own way of addressing the object again (the
    Blender object name for the real adapter). It is deliberately opaque to the
    service so the service never builds Blender identifiers itself.
    """

    handle: str
    name: str
    position_meters: Vec3
    movable: bool
    object_id: Optional[str] = None
    #: Why the object cannot be moved, when ``movable`` is False.
    immovable_reason: Optional[str] = None

    def as_object_ref(self) -> ObjectRef:
        return ObjectRef(object_id=self.object_id, name=self.name)


class SceneAdapter(Protocol):
    """Minimal, semantic access to a 3D scene.

    Deliberately narrow: exact-match lookup and a world-space position write.
    There is no query language, no scene search heuristic, and no arbitrary code
    execution (see .kiro/steering/security.md).
    """

    def find_object(self, ref: ObjectRef) -> Optional[SceneObjectState]:
        """Resolve a reference to exactly one object, or None.

        Resolution order is stable-id first, then name. Implementations must not
        guess, fuzzy-match, or search broadly.
        """
        ...

    def set_world_position(self, handle: str, position: Vec3) -> None:
        """Set an object's absolute world-space position, in meters.

        Absolute rather than relative on purpose: the retry-safe execution model
        writes a known destination, never an increment.

        Raises SceneAdapterError if the write fails.
        """
        ...

    def read_world_position(self, handle: str) -> Optional[Vec3]:
        """Re-read an object's world-space position for verification."""
        ...
