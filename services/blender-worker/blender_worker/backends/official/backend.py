"""``OfficialBlenderLabBackend`` — capabilities served by the official Blender MCP.

This is the only module in the project that knows the official MCP exists. It maps
platform capability names onto official tool calls, and it is the single place where
a stable ``studio_object_id`` is translated into something Blender understands.

Transport choice, and why
-------------------------
Mutations and reads both go through the official ``execute_blender_code_for_cli``
tool, which runs ``blender --background <blend_file> --python-expr …``. That was
chosen over the interactive ``execute_blender_code`` path because it:

* needs no GUI Blender session and no manually installed add-on, so the product
  works on a fresh machine with nothing to click;
* takes the project path explicitly, which preserves Spec 001's project isolation
  (a worker cannot touch another project's file by accident);
* is a fresh process per call, so a crash cannot leave a half-mutated session.

The cost is ~1.5 s of Blender startup per call, and the requirement that every
mutating script saves — an unsaved change in a background process is discarded.
The interactive transport remains a future option behind this same class.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ...capability import classifier, names
from ...capability.provider import (
    APPROVAL_REQUIRED,
    BLENDER_UNAVAILABLE,
    CAPABILITY_UNAVAILABLE,
    MUTATION_FAILED,
    VALIDATION_ERROR,
    BackendHealth,
    CapabilityRequest,
    CapabilityResult,
)
from . import scripts
from .session import McpInstall, McpSessionError, OfficialMcpSession

_log = logging.getLogger(__name__)

BACKEND_NAME = "official_blender_lab_mcp"

#: The official tool used for every capability. Recorded as a constant so the
#: compatibility test can assert the backend depends on exactly one tool today.
CLI_EXECUTE_TOOL = "execute_blender_code_for_cli"

#: Official tools we rely on existing. A missing one is a compatibility failure.
REQUIRED_TOOLS = (CLI_EXECUTE_TOOL,)

APPROVAL_PREFIX = "approved:"


def approval_token_for(code: str) -> str:
    """The token the control plane issues once a user approves this exact code.

    Binding the token to a digest of the code is what stops "approve harmless code,
    execute something else": the backend re-derives the digest and refuses a
    mismatch.
    """
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return f"{APPROVAL_PREFIX}{digest}"


class OfficialBlenderLabBackend:
    """Serve platform capabilities through the pinned official Blender MCP."""

    name = BACKEND_NAME

    def __init__(
        self,
        session: Optional[OfficialMcpSession] = None,
        *,
        allow_unapproved_code: bool = False,
    ) -> None:
        self._session = session or OfficialMcpSession()
        # Only ever true in tests that assert the classifier's own behaviour.
        self._allow_unapproved_code = allow_unapproved_code
        self._handlers: dict[str, Callable[[CapabilityRequest], CapabilityResult]] = {
            names.INSPECT_SCENE: self._inspect_scene,
            names.INSPECT_OBJECT: self._inspect_object,
            names.MOVE_OBJECT: self._move_object,
            names.ROTATE_OBJECT: self._rotate_object,
            names.SCALE_OBJECT: self._scale_object,
            names.SET_OBJECT_DIMENSIONS: self._set_object_dimensions,
            names.CREATE_OBJECT: self._create_object,
            names.DUPLICATE_OBJECT: self._duplicate_object,
            names.DELETE_OBJECT: self._delete_object,
            names.SET_MATERIAL_COLOR: self._set_material_color,
            names.CREATE_WALL: self._create_wall,
            names.CREATE_FLOOR: self._create_floor,
            names.CREATE_CEILING: self._create_ceiling,
            names.CREATE_OPENING: self._create_opening,
            names.CREATE_DOOR_PLACEHOLDER: self._create_door_placeholder,
            names.CREATE_WINDOW_PLACEHOLDER: self._create_window_placeholder,
            names.EXPORT_GLB: self._export_glb,
            names.EXECUTE_BLENDER_PYTHON: self._execute_python,
        }

    # -- provider protocol -------------------------------------------------
    def capabilities(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def health(self) -> BackendHealth:
        try:
            install = self._session.install
        except McpSessionError as error:
            return BackendHealth(available=False, backend=self.name, detail=str(error))
        try:
            tools = self._session.list_tools()
        except McpSessionError as error:
            return BackendHealth(
                available=False,
                backend=self.name,
                detail=str(error),
                server_version=install.server_version,
                pinned_commit=install.pinned_commit,
            )
        missing = [tool for tool in REQUIRED_TOOLS if tool not in tools]
        if missing:
            return BackendHealth(
                available=False,
                backend=self.name,
                detail=f"pinned official MCP is missing required tools: {missing}",
                server_version=install.server_version,
                pinned_commit=install.pinned_commit,
            )
        return BackendHealth(
            available=True,
            backend=self.name,
            detail=f"{len(tools)} official tools available",
            blender_version=_blender_version(),
            server_version=install.server_version,
            pinned_commit=install.pinned_commit,
        )

    def invoke(self, request: CapabilityRequest) -> CapabilityResult:
        handler = self._handlers.get(request.capability)
        if handler is None:
            return CapabilityResult.failure(
                request.capability,
                CAPABILITY_UNAVAILABLE,
                f"This backend cannot perform {request.capability}.",
            )
        try:
            return handler(request)
        except scripts.ScriptParameterError as error:
            return CapabilityResult.failure(
                request.capability, VALIDATION_ERROR, str(error)
            )
        except McpSessionError as error:
            return CapabilityResult.failure(
                request.capability, BLENDER_UNAVAILABLE, str(error)
            )

    def close(self) -> None:
        self._session.close()

    # -- shared plumbing ---------------------------------------------------
    def _run_script(
        self, request: CapabilityRequest, script_name: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        code = scripts.render(script_name, params)
        return self._run_code(request, code)

    def _run_code(self, request: CapabilityRequest, code: str) -> Mapping[str, Any]:
        return self._session.call_tool(
            CLI_EXECUTE_TOOL,
            {"blend_file": str(request.project_path), "code": code},
        )

    @staticmethod
    def _object_spec(request: CapabilityRequest) -> dict[str, Any]:
        """The identity half of a request: stable id preferred, name as fallback."""
        return {
            "object_id": request.argument("object_id"),
            "name": request.argument("name"),
        }

    @staticmethod
    def _settled(
        request: CapabilityRequest, payload: Mapping[str, Any]
    ) -> CapabilityResult:
        status = str(payload.get("status", "ok"))
        if status == "object_not_found":
            return CapabilityResult.failure(
                request.capability, "OBJECT_NOT_FOUND", "That object is not in the scene."
            )
        if status != "ok":
            return CapabilityResult.failure(
                request.capability,
                MUTATION_FAILED,
                f"Blender reported {status} for {request.capability}.",
            )
        return CapabilityResult.success(request.capability, payload)

    # -- reads -------------------------------------------------------------
    def _inspect_scene(self, request: CapabilityRequest) -> CapabilityResult:
        payload = self._run_script(request, "read_scene", {})
        if "objects" not in payload:
            return CapabilityResult.failure(
                request.capability, VALIDATION_ERROR, "The scene read returned no objects."
            )
        return CapabilityResult.success(request.capability, payload)

    def _inspect_object(self, request: CapabilityRequest) -> CapabilityResult:
        payload = self._run_script(request, "read_object", self._object_spec(request))
        return self._settled(request, payload)

    # -- transforms --------------------------------------------------------
    def _move_object(self, request: CapabilityRequest) -> CapabilityResult:
        params = self._object_spec(request)
        params["desired_position_meters"] = _vec3(request.argument("desired_position_meters"))
        return self._settled(request, self._run_script(request, "move_object", params))

    def _rotate_object(self, request: CapabilityRequest) -> CapabilityResult:
        params = self._object_spec(request)
        params["desired_rotation_radians"] = _vec3(
            request.argument("desired_rotation_radians")
        )
        return self._settled(request, self._run_script(request, "rotate_object", params))

    def _scale_object(self, request: CapabilityRequest) -> CapabilityResult:
        params = self._object_spec(request)
        params["desired_scale"] = _vec3(request.argument("desired_scale"))
        return self._settled(request, self._run_script(request, "scale_object", params))

    def _set_object_dimensions(self, request: CapabilityRequest) -> CapabilityResult:
        params = self._object_spec(request)
        params["desired_dimensions_meters"] = _vec3(
            request.argument("desired_dimensions_meters")
        )
        return self._settled(
            request, self._run_script(request, "set_object_dimensions", params)
        )

    # -- objects -----------------------------------------------------------
    def _create_object(self, request: CapabilityRequest) -> CapabilityResult:
        params = {
            "primitive": str(request.argument("primitive", "cube")),
            "display_name": str(request.argument("display_name", "Object")),
            "object_id": request.argument("object_id"),
            "position_meters": _vec3(request.argument("position_meters")),
            "dimensions_meters": _optional_vec3(request.argument("dimensions_meters")),
        }
        return self._settled(request, self._run_script(request, "create_object", params))

    def _duplicate_object(self, request: CapabilityRequest) -> CapabilityResult:
        params = self._object_spec(request)
        params["display_name"] = str(request.argument("display_name", "Copy"))
        params["object_id"] = request.argument("new_object_id")
        params["offset_meters"] = _optional_vec3(request.argument("offset_meters"))
        return self._settled(request, self._run_script(request, "duplicate_object", params))

    def _delete_object(self, request: CapabilityRequest) -> CapabilityResult:
        return self._settled(
            request, self._run_script(request, "delete_object", self._object_spec(request))
        )

    # -- material ----------------------------------------------------------
    def _set_material_color(self, request: CapabilityRequest) -> CapabilityResult:
        params = self._object_spec(request)
        params["color_linear_srgb"] = _rgba(request.argument("color_linear_srgb"))
        return self._settled(
            request, self._run_script(request, "set_material_color", params)
        )

    # -- architecture ------------------------------------------------------
    def _create_wall(self, request: CapabilityRequest) -> CapabilityResult:
        params = {
            "display_name": str(request.argument("display_name", "Wall")),
            "object_id": request.argument("object_id"),
            "start_meters": _vec2(request.argument("start_meters")),
            "end_meters": _vec2(request.argument("end_meters")),
            "height_meters": _positive(request.argument("height_meters"), "height_meters"),
            "thickness_meters": _positive(
                request.argument("thickness_meters"), "thickness_meters"
            ),
            "base_elevation_meters": _number(
                request.argument("base_elevation_meters", 0.0), "base_elevation_meters"
            ),
            "color_linear_srgb": _optional_rgba(request.argument("color_linear_srgb")),
        }
        return self._settled(request, self._run_script(request, "create_wall", params))

    def _create_floor(self, request: CapabilityRequest) -> CapabilityResult:
        return self._create_slab(request, default_name="Floor", grow="down")

    def _create_ceiling(self, request: CapabilityRequest) -> CapabilityResult:
        return self._create_slab(request, default_name="Ceiling", grow="up")

    def _create_slab(
        self, request: CapabilityRequest, *, default_name: str, grow: str
    ) -> CapabilityResult:
        footprint = request.argument("footprint_meters") or []
        points = [_vec2(point) for point in footprint]
        if len(points) < 3:
            return CapabilityResult.failure(
                request.capability,
                VALIDATION_ERROR,
                "A floor or ceiling needs at least three footprint points.",
            )
        params = {
            "display_name": str(request.argument("display_name", default_name)),
            "object_id": request.argument("object_id"),
            "footprint_meters": points,
            "thickness_meters": _positive(
                request.argument("thickness_meters"), "thickness_meters"
            ),
            "elevation_meters": _number(
                request.argument("elevation_meters", 0.0), "elevation_meters"
            ),
            "grow": grow,
            "color_linear_srgb": _optional_rgba(request.argument("color_linear_srgb")),
        }
        return self._settled(request, self._run_script(request, "create_slab", params))

    def _create_opening(self, request: CapabilityRequest) -> CapabilityResult:
        params = {
            "wall": {
                "object_id": request.argument("wall_object_id"),
                "name": request.argument("wall_name"),
            },
            "centre_meters": _vec3(request.argument("centre_meters")),
            "width_meters": _positive(request.argument("width_meters"), "width_meters"),
            "height_meters": _positive(request.argument("height_meters"), "height_meters"),
            "wall_thickness_meters": _number(
                request.argument("wall_thickness_meters", 0.2), "wall_thickness_meters"
            ),
        }
        return self._settled(request, self._run_script(request, "create_opening", params))

    def _create_door_placeholder(self, request: CapabilityRequest) -> CapabilityResult:
        return self._create_placeholder(request, "door", "Door")

    def _create_window_placeholder(self, request: CapabilityRequest) -> CapabilityResult:
        return self._create_placeholder(request, "window", "Window")

    def _create_placeholder(
        self, request: CapabilityRequest, element_type: str, default_name: str
    ) -> CapabilityResult:
        params = {
            "display_name": str(request.argument("display_name", default_name)),
            "object_id": request.argument("object_id"),
            "element_type": element_type,
            "centre_meters": _vec3(request.argument("centre_meters")),
            "width_meters": _positive(request.argument("width_meters"), "width_meters"),
            "height_meters": _positive(request.argument("height_meters"), "height_meters"),
            "depth_meters": _positive(
                request.argument("depth_meters", 0.05), "depth_meters"
            ),
            "rotation_z_radians": _number(
                request.argument("rotation_z_radians", 0.0), "rotation_z_radians"
            ),
            "color_linear_srgb": _optional_rgba(request.argument("color_linear_srgb")),
        }
        return self._settled(
            request, self._run_script(request, "create_placeholder", params)
        )

    # -- artifacts ---------------------------------------------------------
    def _export_glb(self, request: CapabilityRequest) -> CapabilityResult:
        # The destination is platform-derived. It is never taken from the model.
        output_path = request.argument("output_path")
        if not output_path:
            return CapabilityResult.failure(
                request.capability, VALIDATION_ERROR, "No export destination was provided."
            )
        payload = self._run_script(request, "export_glb", {"output_path": str(output_path)})
        return self._settled(request, payload)

    # -- model-authored code ----------------------------------------------
    def _execute_python(self, request: CapabilityRequest) -> CapabilityResult:
        code = request.argument("code")
        if not isinstance(code, str) or not code.strip():
            return CapabilityResult.failure(
                request.capability, VALIDATION_ERROR, "No code was supplied."
            )

        assessment = classifier.classify_python(code)
        if assessment.refused:
            return CapabilityResult.failure(
                request.capability,
                VALIDATION_ERROR,
                assessment.summary(),
                assessment=assessment,
            )

        if assessment.requires_approval and not self._allow_unapproved_code:
            expected = approval_token_for(code)
            if request.approval_token != expected:
                return CapabilityResult.failure(
                    request.capability,
                    APPROVAL_REQUIRED,
                    assessment.summary(),
                    assessment=assessment,
                )

        payload = self._run_code(request, code)
        result = CapabilityResult.success(request.capability, payload)
        return result


def _blender_version() -> Optional[str]:
    try:
        from blender_mcp.blender_runtime import blender_version

        return blender_version()
    except Exception:  # pragma: no cover - discovery is best effort
        return None


# --------------------------------------------------------------------------
# argument coercion
#
# Every value crossing into a script passes through here, so a malformed or
# hostile argument fails before any Blender process is started.
# --------------------------------------------------------------------------


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise scripts.ScriptParameterError(f"{field} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise scripts.ScriptParameterError(f"{field} must be finite")
    return number


def _positive(value: Any, field: str) -> float:
    number = _number(value, field)
    if number <= 0.0:
        raise scripts.ScriptParameterError(f"{field} must be greater than zero")
    return number


def _vec3(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise scripts.ScriptParameterError("expected an {x, y, z} position")
    return {
        "x": _number(value.get("x"), "x"),
        "y": _number(value.get("y"), "y"),
        "z": _number(value.get("z"), "z"),
    }


def _optional_vec3(value: Any) -> Optional[dict[str, float]]:
    return None if value is None else _vec3(value)


def _vec2(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise scripts.ScriptParameterError("expected an {x, y} point")
    return {"x": _number(value.get("x"), "x"), "y": _number(value.get("y"), "y")}


def _rgba(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise scripts.ScriptParameterError("expected a linear sRGB {r, g, b, a} colour")
    channels = {}
    for channel in ("r", "g", "b", "a"):
        number = _number(value.get(channel, 1.0 if channel == "a" else None), channel)
        if not 0.0 <= number <= 1.0:
            raise scripts.ScriptParameterError(f"colour channel {channel} must be in [0, 1]")
        channels[channel] = number
    return channels


def _optional_rgba(value: Any) -> Optional[dict[str, float]]:
    return None if value is None else _rgba(value)
