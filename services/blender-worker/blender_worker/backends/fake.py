"""An in-memory ``BlenderCapabilityProvider`` for offline tests.

This is the load-bearing test seam. Because it implements the same protocol as the
official backend, the entire stack above it — jobs, durability, orchestration,
routes, the browser — is testable with no Blender, no MCP server and no network.

It models the scene faithfully enough to exercise real behaviour: geometry maths for
walls and slabs, stable-id resolution, verification read-back, and the same approval
rule for model-authored Python.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from ..capability import classifier, names
from ..capability.provider import (
    APPROVAL_REQUIRED,
    CAPABILITY_UNAVAILABLE,
    MUTATION_FAILED,
    VALIDATION_ERROR,
    BackendHealth,
    CapabilityRequest,
    CapabilityResult,
)
from .official.backend import approval_token_for

BACKEND_NAME = "fake_blender_capability_provider"


@dataclass
class FakeObject:
    name: str
    object_type: str = "MESH"
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    dimensions: tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    visible: bool = True
    studio_object_id: Optional[str] = None
    material_name: Optional[str] = None
    base_color: Optional[tuple[float, float, float, float]] = None
    element_type: Optional[str] = None
    openings: int = 0

    def describe(self) -> dict[str, Any]:
        material: Optional[dict[str, Any]] = None
        if self.material_name is not None:
            base = None
            if self.base_color is not None:
                base = {
                    "r": self.base_color[0],
                    "g": self.base_color[1],
                    "b": self.base_color[2],
                    "a": self.base_color[3],
                }
            material = {"name": self.material_name, "base_color": base}
        return {
            "studio_object_id": self.studio_object_id,
            "name": self.name,
            "object_type": self.object_type,
            "world_position_meters": {
                "x": self.position[0],
                "y": self.position[1],
                "z": self.position[2],
            },
            "dimensions_meters": {
                "x": self.dimensions[0],
                "y": self.dimensions[1],
                "z": self.dimensions[2],
            },
            "rotation_euler_radians": {
                "x": self.rotation[0],
                "y": self.rotation[1],
                "z": self.rotation[2],
            },
            "scale": {"x": self.scale[0], "y": self.scale[1], "z": self.scale[2]},
            "visible": self.visible,
            "material": material,
        }


class FakeBlenderCapabilityProvider:
    """A deterministic capability provider backed by a dictionary."""

    name = BACKEND_NAME

    def __init__(
        self,
        objects: Optional[Mapping[str, FakeObject]] = None,
        *,
        available: bool = True,
        allow_unapproved_code: bool = False,
    ) -> None:
        self.objects: dict[str, FakeObject] = dict(objects or {})
        self.available = available
        self.allow_unapproved_code = allow_unapproved_code
        #: Every invocation, in order. Tests assert against this.
        self.invocations: list[CapabilityRequest] = []
        #: Model-authored code that reached execution.
        self.executed_code: list[str] = []
        #: Capability name -> forced failure, for failure-path tests.
        self.forced_failures: dict[str, tuple[str, str]] = {}
        #: Optional side effects applied when model code executes.
        self.code_effects: list[Callable[["FakeBlenderCapabilityProvider", str], None]] = []
        self.saves = 0
        self.exports: list[str] = []
        self._unit_system = "METRIC"
        self._scale_length = 1.0

    # -- helpers -----------------------------------------------------------
    def seed_cube(self, name: str = "Cube", *, object_id: Optional[str] = None) -> FakeObject:
        obj = FakeObject(
            name=name, dimensions=(2.0, 2.0, 2.0), studio_object_id=object_id
        )
        self.objects[name] = obj
        return obj

    def find(
        self, *, object_id: Optional[str] = None, name: Optional[str] = None
    ) -> Optional[FakeObject]:
        if object_id:
            for obj in self.objects.values():
                if obj.studio_object_id == object_id:
                    return obj
        if name:
            return self.objects.get(name)
        return None

    def fail_capability(self, capability: str, code: str, message: str) -> None:
        self.forced_failures[capability] = (code, message)

    # -- provider protocol -------------------------------------------------
    def capabilities(self) -> tuple[str, ...]:
        return names.ALL_CAPABILITIES

    def health(self) -> BackendHealth:
        return BackendHealth(
            available=self.available,
            backend=self.name,
            detail="in-memory test backend",
            blender_version="fake",
            server_version="fake",
            pinned_commit="fake",
        )

    def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        self.invocations.append(request)
        if not self.available:
            return CapabilityResult.failure(
                request.capability, "BLENDER_UNAVAILABLE", "The design machine is offline."
            )
        forced = self.forced_failures.get(request.capability)
        if forced is not None:
            return CapabilityResult.failure(request.capability, forced[0], forced[1])

        handler = getattr(self, f"_{request.capability}", None)
        if handler is None:
            return CapabilityResult.failure(
                request.capability,
                CAPABILITY_UNAVAILABLE,
                f"This backend cannot perform {request.capability}.",
            )
        return handler(request)

    # -- reads -------------------------------------------------------------
    def _inspect_scene(self, request: CapabilityRequest) -> CapabilityResult:
        described = [obj.describe() for obj in self.objects.values()]
        described.sort(key=lambda entry: entry["name"])
        return CapabilityResult.success(
            request.capability,
            {
                "units": {
                    "unit_system": self._unit_system,
                    "length_unit": "m",
                    "scale_length": self._scale_length,
                },
                "objects": described,
                "scene_name": "Scene",
            },
        )

    def _inspect_object(self, request: CapabilityRequest) -> CapabilityResult:
        obj = self.find(
            object_id=request.argument("object_id"), name=request.argument("name")
        )
        if obj is None:
            return self._not_found(request)
        return CapabilityResult.success(
            request.capability, {"status": "ok", "object": obj.describe()}
        )

    # -- transforms --------------------------------------------------------
    def _move_object(self, request: CapabilityRequest) -> CapabilityResult:
        obj = self._target(request)
        if obj is None:
            return self._not_found(request)
        target = request.argument("desired_position_meters") or {}
        obj.position = (
            float(target["x"]),
            float(target["y"]),
            float(target["z"]),
        )
        return self._saved(request, obj)

    def _rotate_object(self, request: CapabilityRequest) -> CapabilityResult:
        obj = self._target(request)
        if obj is None:
            return self._not_found(request)
        target = request.argument("desired_rotation_radians") or {}
        obj.rotation = (float(target["x"]), float(target["y"]), float(target["z"]))
        return self._saved(request, obj)

    def _scale_object(self, request: CapabilityRequest) -> CapabilityResult:
        obj = self._target(request)
        if obj is None:
            return self._not_found(request)
        target = request.argument("desired_scale") or {}
        obj.scale = (float(target["x"]), float(target["y"]), float(target["z"]))
        return self._saved(request, obj)

    def _set_object_dimensions(self, request: CapabilityRequest) -> CapabilityResult:
        obj = self._target(request)
        if obj is None:
            return self._not_found(request)
        target = request.argument("desired_dimensions_meters") or {}
        obj.dimensions = (float(target["x"]), float(target["y"]), float(target["z"]))
        return self._saved(request, obj)

    # -- objects -----------------------------------------------------------
    def _create_object(self, request: CapabilityRequest) -> CapabilityResult:
        centre = request.argument("position_meters") or {}
        dimensions = request.argument("dimensions_meters") or {"x": 1.0, "y": 1.0, "z": 1.0}
        obj = FakeObject(
            name=str(request.argument("display_name", "Object")),
            position=(float(centre["x"]), float(centre["y"]), float(centre["z"])),
            dimensions=(
                float(dimensions["x"]),
                float(dimensions["y"]),
                float(dimensions["z"]),
            ),
            studio_object_id=request.argument("object_id"),
        )
        self.objects[obj.name] = obj
        return self._saved(request, obj)

    def _duplicate_object(self, request: CapabilityRequest) -> CapabilityResult:
        source = self._target(request)
        if source is None:
            return self._not_found(request)
        offset = request.argument("offset_meters") or {"x": 0.0, "y": 0.0, "z": 0.0}
        copy = FakeObject(
            name=str(request.argument("display_name", source.name + "_copy")),
            object_type=source.object_type,
            position=(
                source.position[0] + float(offset["x"]),
                source.position[1] + float(offset["y"]),
                source.position[2] + float(offset["z"]),
            ),
            dimensions=source.dimensions,
            rotation=source.rotation,
            scale=source.scale,
            studio_object_id=request.argument("new_object_id"),
        )
        self.objects[copy.name] = copy
        return self._saved(request, copy)

    def _delete_object(self, request: CapabilityRequest) -> CapabilityResult:
        obj = self._target(request)
        if obj is None:
            return self._not_found(request)
        del self.objects[obj.name]
        self.saves += 1
        return CapabilityResult.success(
            request.capability, {"status": "ok", "removed": obj.name}
        )

    # -- material ----------------------------------------------------------
    def _set_material_color(self, request: CapabilityRequest) -> CapabilityResult:
        obj = self._target(request)
        if obj is None:
            return self._not_found(request)
        colour = request.argument("color_linear_srgb") or {}
        obj.material_name = f"{obj.name}_material"
        obj.base_color = (
            float(colour["r"]),
            float(colour["g"]),
            float(colour["b"]),
            float(colour.get("a", 1.0)),
        )
        result = self._saved(request, obj)
        return CapabilityResult.success(
            request.capability,
            {**result.data, "material_name": obj.material_name},
        )

    # -- architecture ------------------------------------------------------
    def _create_wall(self, request: CapabilityRequest) -> CapabilityResult:
        start = request.argument("start_meters") or {}
        end = request.argument("end_meters") or {}
        dx = float(end["x"]) - float(start["x"])
        dy = float(end["y"]) - float(start["y"])
        length = math.hypot(dx, dy)
        if length <= 0.0:
            return CapabilityResult.failure(
                request.capability, MUTATION_FAILED, "Blender reported degenerate_wall."
            )
        height = float(request.argument("height_meters"))
        thickness = float(request.argument("thickness_meters"))
        base = float(request.argument("base_elevation_meters", 0.0) or 0.0)
        obj = FakeObject(
            name=str(request.argument("display_name", "Wall")),
            position=(
                (float(start["x"]) + float(end["x"])) / 2.0,
                (float(start["y"]) + float(end["y"])) / 2.0,
                base + height / 2.0,
            ),
            dimensions=(length, thickness, height),
            rotation=(0.0, 0.0, math.atan2(dy, dx)),
            studio_object_id=request.argument("object_id"),
        )
        colour = request.argument("color_linear_srgb")
        if colour:
            obj.material_name = f"{obj.name}_material"
            obj.base_color = (
                float(colour["r"]),
                float(colour["g"]),
                float(colour["b"]),
                float(colour.get("a", 1.0)),
            )
        self.objects[obj.name] = obj
        result = self._saved(request, obj)
        return CapabilityResult.success(
            request.capability, {**result.data, "length_meters": length}
        )

    def _create_floor(self, request: CapabilityRequest) -> CapabilityResult:
        return self._slab(request, "Floor")

    def _create_ceiling(self, request: CapabilityRequest) -> CapabilityResult:
        return self._slab(request, "Ceiling")

    def _slab(self, request: CapabilityRequest, default_name: str) -> CapabilityResult:
        footprint = request.argument("footprint_meters") or []
        if len(footprint) < 3:
            return CapabilityResult.failure(
                request.capability,
                VALIDATION_ERROR,
                "A floor or ceiling needs at least three footprint points.",
            )
        xs = [float(point["x"]) for point in footprint]
        ys = [float(point["y"]) for point in footprint]
        thickness = float(request.argument("thickness_meters"))
        elevation = float(request.argument("elevation_meters", 0.0) or 0.0)
        obj = FakeObject(
            name=str(request.argument("display_name", default_name)),
            position=(
                (min(xs) + max(xs)) / 2.0,
                (min(ys) + max(ys)) / 2.0,
                elevation,
            ),
            dimensions=(max(xs) - min(xs), max(ys) - min(ys), thickness),
            studio_object_id=request.argument("object_id"),
        )
        self.objects[obj.name] = obj
        return self._saved(request, obj)

    def _create_opening(self, request: CapabilityRequest) -> CapabilityResult:
        wall = self.find(
            object_id=request.argument("wall_object_id"),
            name=request.argument("wall_name"),
        )
        if wall is None:
            return self._not_found(request)
        wall.openings += 1
        return self._saved(request, wall)

    def _create_door_placeholder(self, request: CapabilityRequest) -> CapabilityResult:
        return self._placeholder(request, "door", "Door")

    def _create_window_placeholder(self, request: CapabilityRequest) -> CapabilityResult:
        return self._placeholder(request, "window", "Window")

    def _placeholder(
        self, request: CapabilityRequest, element_type: str, default_name: str
    ) -> CapabilityResult:
        centre = request.argument("centre_meters") or {}
        obj = FakeObject(
            name=str(request.argument("display_name", default_name)),
            position=(float(centre["x"]), float(centre["y"]), float(centre["z"])),
            dimensions=(
                float(request.argument("width_meters")),
                float(request.argument("depth_meters", 0.05) or 0.05),
                float(request.argument("height_meters")),
            ),
            rotation=(0.0, 0.0, float(request.argument("rotation_z_radians", 0.0) or 0.0)),
            studio_object_id=request.argument("object_id"),
            element_type=element_type,
        )
        self.objects[obj.name] = obj
        return self._saved(request, obj)

    # -- artifacts ---------------------------------------------------------
    def _export_glb(self, request: CapabilityRequest) -> CapabilityResult:
        destination = request.argument("output_path")
        if not destination:
            return CapabilityResult.failure(
                request.capability, VALIDATION_ERROR, "No export destination was provided."
            )
        from pathlib import Path

        path = Path(str(destination))
        path.parent.mkdir(parents=True, exist_ok=True)
        # A real GLB begins with the "glTF" magic; emit a minimal valid-looking header
        # so artifact validation is exercised rather than bypassed.
        path.write_bytes(b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 12)
        self.exports.append(str(path))
        return CapabilityResult.success(
            request.capability,
            {"status": "ok", "object_count": len(self.objects)},
        )

    # -- model-authored code ----------------------------------------------
    def _execute_blender_python(self, request: CapabilityRequest) -> CapabilityResult:
        code = request.argument("code")
        if not isinstance(code, str) or not code.strip():
            return CapabilityResult.failure(
                request.capability, VALIDATION_ERROR, "No code was supplied."
            )
        assessment = classifier.classify_python(code)
        if assessment.refused:
            return CapabilityResult.failure(
                request.capability, VALIDATION_ERROR, assessment.summary(), assessment=assessment
            )
        if assessment.requires_approval and not self.allow_unapproved_code:
            if request.approval_token != approval_token_for(code):
                return CapabilityResult.failure(
                    request.capability,
                    APPROVAL_REQUIRED,
                    assessment.summary(),
                    assessment=assessment,
                )
        self.executed_code.append(code)
        for effect in self.code_effects:
            effect(self, code)
        self.saves += 1
        return CapabilityResult.success(
            request.capability, {"status": "ok", "executed": True}
        )

    # -- shared ------------------------------------------------------------
    def _target(self, request: CapabilityRequest) -> Optional[FakeObject]:
        return self.find(
            object_id=request.argument("object_id"), name=request.argument("name")
        )

    def _not_found(self, request: CapabilityRequest) -> CapabilityResult:
        return CapabilityResult.failure(
            request.capability, "OBJECT_NOT_FOUND", "That object is not in the scene."
        )

    def _saved(self, request: CapabilityRequest, obj: FakeObject) -> CapabilityResult:
        self.saves += 1
        return CapabilityResult.success(
            request.capability, {"status": "ok", "object": obj.describe()}
        )
