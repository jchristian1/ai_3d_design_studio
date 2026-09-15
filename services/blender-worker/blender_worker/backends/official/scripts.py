"""Platform-owned Blender Python for the semantic capabilities.

Each entry is a fixed source literal plus a single parameter substitution site. The
parameters are validated canonical values encoded with :func:`repr`, so a value can
never change the structure of the program it is passed to — it arrives as one Python
literal in one place. This is the same convention the official Blender MCP uses for
its own tools (``*_toolcode.py`` + ``__BLMCP_PARAMS__``), adopted deliberately.

These scripts exist because the platform, not the model, initiates them: reading the
scene for a snapshot, exporting a GLB, and the architectural primitives whose maths
we want to be exact and repeatable. Model-authored Python takes a different route
(``execute_blender_python``) and is classified before it runs.

Every mutating script saves the project. That is not optional: the official MCP's
``execute_blender_code_for_cli`` runs ``blender --background <blend_file>`` as a
fresh process per call, so an unsaved change would simply be discarded.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

PARAMS_PLACEHOLDER: Final = "__STUDIO_PARAMS__"

#: Shared helpers injected into every script. Kept small and readable on purpose:
#: this is reviewed code, and it runs inside the user's Blender.
_PREAMBLE: Final = '''
import bpy
import math

STUDIO_ID = "studio_object_id"


def _tag(obj, object_id):
    if object_id:
        obj[STUDIO_ID] = object_id
    return obj


def _find(spec):
    """Resolve an object by stable studio id first, then by exact name."""
    object_id = spec.get("object_id")
    if object_id:
        for obj in bpy.data.objects:
            if obj.get(STUDIO_ID) == object_id:
                return obj
    name = spec.get("name")
    if name:
        return bpy.data.objects.get(name)
    return None


def _describe(obj):
    t = obj.matrix_world.translation
    d = obj.dimensions
    r = obj.rotation_euler
    material = None
    if obj.data is not None and getattr(obj.data, "materials", None):
        slot = obj.data.materials[0] if len(obj.data.materials) else None
        if slot is not None:
            base = None
            if slot.use_nodes:
                for node in slot.node_tree.nodes:
                    if node.type == "BSDF_PRINCIPLED":
                        c = node.inputs["Base Color"].default_value
                        base = {"r": c[0], "g": c[1], "b": c[2], "a": c[3]}
                        break
            material = {"name": slot.name, "base_color": base}
    return {
        "studio_object_id": obj.get(STUDIO_ID),
        "name": obj.name,
        "object_type": obj.type,
        "world_position_meters": {"x": t[0], "y": t[1], "z": t[2]},
        "dimensions_meters": {"x": d[0], "y": d[1], "z": d[2]},
        "rotation_euler_radians": {"x": r[0], "y": r[1], "z": r[2]},
        "scale": {"x": obj.scale[0], "y": obj.scale[1], "z": obj.scale[2]},
        "visible": not obj.hide_viewport,
        "material": material,
    }


def _save():
    bpy.ops.wm.save_mainfile()


def _deselect_all():
    for obj in bpy.data.objects:
        obj.select_set(False)


def _new_cube(name, object_id, size_xyz, centre, rotation_z=0.0):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=centre)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (size_xyz[0], size_xyz[1], size_xyz[2])
    obj.rotation_euler = (0.0, 0.0, rotation_z)
    bpy.context.view_layer.update()
    _deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return _tag(obj, object_id)


def _set_base_color(obj, colour):
    material = bpy.data.materials.new(name=obj.name + "_material")
    material.use_nodes = True
    for node in material.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            node.inputs["Base Color"].default_value = (
                colour["r"], colour["g"], colour["b"], colour["a"],
            )
            break
    material.diffuse_color = (colour["r"], colour["g"], colour["b"], colour["a"])
    if obj.data.materials:
        obj.data.materials[0] = material
    else:
        obj.data.materials.append(material)
    return material


def _carve(target, cutter):
    """Boolean-difference *cutter* out of *target*, then remove the cutter."""
    _deselect_all()
    target.select_set(True)
    bpy.context.view_layer.objects.active = target
    modifier = target.modifiers.new(name="studio_opening", type="BOOLEAN")
    modifier.operation = "DIFFERENCE"
    modifier.object = cutter
    modifier.solver = "EXACT"
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.data.objects.remove(cutter, do_unlink=True)
'''

_FOOTER: Final = "\n\nresult = main(" + PARAMS_PLACEHOLDER + ")\n"


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------

READ_SCENE: Final = '''
def main(p):
    scene = bpy.context.scene
    units = scene.unit_settings
    objects = [_describe(obj) for obj in bpy.data.objects]
    objects.sort(key=lambda entry: entry["name"])
    return {
        "units": {
            "unit_system": units.system,
            "length_unit": "m",
            "scale_length": units.scale_length,
        },
        "objects": objects,
        "scene_name": scene.name,
    }
'''

READ_OBJECT: Final = '''
def main(p):
    obj = _find(p)
    if obj is None:
        return {"status": "object_not_found"}
    return {"status": "ok", "object": _describe(obj)}
'''


# --------------------------------------------------------------------------
# transforms
# --------------------------------------------------------------------------

MOVE_OBJECT: Final = '''
def main(p):
    obj = _find(p)
    if obj is None:
        return {"status": "object_not_found"}
    target = p["desired_position_meters"]
    obj.location = (target["x"], target["y"], target["z"])
    bpy.context.view_layer.update()
    _save()
    return {"status": "ok", "object": _describe(obj)}
'''

ROTATE_OBJECT: Final = '''
def main(p):
    obj = _find(p)
    if obj is None:
        return {"status": "object_not_found"}
    target = p["desired_rotation_radians"]
    obj.rotation_euler = (target["x"], target["y"], target["z"])
    bpy.context.view_layer.update()
    _save()
    return {"status": "ok", "object": _describe(obj)}
'''

SCALE_OBJECT: Final = '''
def main(p):
    obj = _find(p)
    if obj is None:
        return {"status": "object_not_found"}
    target = p["desired_scale"]
    obj.scale = (target["x"], target["y"], target["z"])
    bpy.context.view_layer.update()
    _save()
    return {"status": "ok", "object": _describe(obj)}
'''

SET_OBJECT_DIMENSIONS: Final = '''
def main(p):
    obj = _find(p)
    if obj is None:
        return {"status": "object_not_found"}
    target = p["desired_dimensions_meters"]
    obj.dimensions = (target["x"], target["y"], target["z"])
    bpy.context.view_layer.update()
    _save()
    return {"status": "ok", "object": _describe(obj)}
'''


# --------------------------------------------------------------------------
# objects
# --------------------------------------------------------------------------

CREATE_OBJECT: Final = '''
PRIMITIVES = {
    "cube": bpy.ops.mesh.primitive_cube_add,
    "plane": bpy.ops.mesh.primitive_plane_add,
    "cylinder": bpy.ops.mesh.primitive_cylinder_add,
    "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
    "cone": bpy.ops.mesh.primitive_cone_add,
}


def main(p):
    kind = p["primitive"]
    add = PRIMITIVES.get(kind)
    if add is None:
        return {"status": "unsupported_primitive"}
    centre = p["position_meters"]
    add(location=(centre["x"], centre["y"], centre["z"]))
    obj = bpy.context.active_object
    obj.name = p["display_name"]
    size = p.get("dimensions_meters")
    if size:
        obj.dimensions = (size["x"], size["y"], size["z"])
    bpy.context.view_layer.update()
    _tag(obj, p.get("object_id"))
    _save()
    return {"status": "ok", "object": _describe(obj)}
'''

DUPLICATE_OBJECT: Final = '''
def main(p):
    obj = _find(p)
    if obj is None:
        return {"status": "object_not_found"}
    copy = obj.copy()
    copy.data = obj.data.copy()
    bpy.context.collection.objects.link(copy)
    copy.name = p["display_name"]
    offset = p.get("offset_meters") or {"x": 0.0, "y": 0.0, "z": 0.0}
    copy.location = (
        obj.location[0] + offset["x"],
        obj.location[1] + offset["y"],
        obj.location[2] + offset["z"],
    )
    _tag(copy, p.get("object_id"))
    bpy.context.view_layer.update()
    _save()
    return {"status": "ok", "object": _describe(copy)}
'''

DELETE_OBJECT: Final = '''
def main(p):
    obj = _find(p)
    if obj is None:
        return {"status": "object_not_found"}
    name = obj.name
    bpy.data.objects.remove(obj, do_unlink=True)
    _save()
    return {"status": "ok", "removed": name}
'''


# --------------------------------------------------------------------------
# material
# --------------------------------------------------------------------------

SET_MATERIAL_COLOR: Final = '''
def main(p):
    obj = _find(p)
    if obj is None:
        return {"status": "object_not_found"}
    if obj.data is None or not hasattr(obj.data, "materials"):
        return {"status": "object_not_paintable"}
    material = _set_base_color(obj, p["color_linear_srgb"])
    bpy.context.view_layer.update()
    _save()
    return {"status": "ok", "material_name": material.name, "object": _describe(obj)}
'''


# --------------------------------------------------------------------------
# architecture
# --------------------------------------------------------------------------

CREATE_WALL: Final = '''
def main(p):
    start = p["start_meters"]
    end = p["end_meters"]
    dx = end["x"] - start["x"]
    dy = end["y"] - start["y"]
    length = math.hypot(dx, dy)
    if length <= 0.0:
        return {"status": "degenerate_wall"}
    height = p["height_meters"]
    thickness = p["thickness_meters"]
    base = p.get("base_elevation_meters", 0.0)
    centre = (
        (start["x"] + end["x"]) / 2.0,
        (start["y"] + end["y"]) / 2.0,
        base + height / 2.0,
    )
    obj = _new_cube(
        p["display_name"], p.get("object_id"),
        (length, thickness, height), centre, math.atan2(dy, dx),
    )
    colour = p.get("color_linear_srgb")
    if colour:
        _set_base_color(obj, colour)
    _save()
    return {"status": "ok", "length_meters": length, "object": _describe(obj)}
'''

CREATE_SLAB: Final = '''
def main(p):
    footprint = p["footprint_meters"]
    if len(footprint) < 3:
        return {"status": "degenerate_footprint"}
    thickness = p["thickness_meters"]
    elevation = p["elevation_meters"]

    mesh = bpy.data.meshes.new(p["display_name"] + "_mesh")
    obj = bpy.data.objects.new(p["display_name"], mesh)
    bpy.context.collection.objects.link(obj)

    verts = [(point["x"], point["y"], 0.0) for point in footprint]
    faces = [tuple(range(len(verts)))]
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    _deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    modifier = obj.modifiers.new(name="studio_thickness", type="SOLIDIFY")
    modifier.thickness = thickness
    modifier.offset = -1.0 if p.get("grow") == "down" else 1.0
    bpy.ops.object.modifier_apply(modifier=modifier.name)

    obj.location = (0.0, 0.0, elevation)
    bpy.context.view_layer.update()
    _tag(obj, p.get("object_id"))
    colour = p.get("color_linear_srgb")
    if colour:
        _set_base_color(obj, colour)
    _save()
    return {"status": "ok", "object": _describe(obj)}
'''

CREATE_OPENING: Final = '''
def main(p):
    wall = _find(p["wall"])
    if wall is None:
        return {"status": "object_not_found"}
    centre = p["centre_meters"]
    width = p["width_meters"]
    height = p["height_meters"]
    # The cutter is deliberately deeper than the wall so the boolean cuts cleanly
    # through both faces rather than leaving a coplanar sliver.
    depth = max(wall.dimensions[1], p.get("wall_thickness_meters", 0.2)) * 4.0
    cutter = _new_cube(
        wall.name + "_cutter", None,
        (width, depth, height),
        (centre["x"], centre["y"], centre["z"]),
        wall.rotation_euler[2],
    )
    _carve(wall, cutter)
    bpy.context.view_layer.update()
    _save()
    return {"status": "ok", "object": _describe(wall)}
'''

CREATE_PLACEHOLDER: Final = '''
def main(p):
    centre = p["centre_meters"]
    obj = _new_cube(
        p["display_name"], p.get("object_id"),
        (p["width_meters"], p["depth_meters"], p["height_meters"]),
        (centre["x"], centre["y"], centre["z"]),
        p.get("rotation_z_radians", 0.0),
    )
    obj["studio_element_type"] = p["element_type"]
    colour = p.get("color_linear_srgb")
    if colour:
        _set_base_color(obj, colour)
    _save()
    return {"status": "ok", "object": _describe(obj)}
'''


# --------------------------------------------------------------------------
# artifacts
# --------------------------------------------------------------------------

EXPORT_GLB: Final = '''
def main(p):
    destination = p["output_path"]
    bpy.ops.export_scene.gltf(
        filepath=destination,
        export_format="GLB",
        export_apply=True,
        export_extras=True,
        export_yup=True,
        use_selection=False,
    )
    return {"status": "ok", "object_count": len(bpy.data.objects)}
'''


SCRIPTS: Final[Mapping[str, str]] = {
    "read_scene": READ_SCENE,
    "read_object": READ_OBJECT,
    "move_object": MOVE_OBJECT,
    "rotate_object": ROTATE_OBJECT,
    "scale_object": SCALE_OBJECT,
    "set_object_dimensions": SET_OBJECT_DIMENSIONS,
    "create_object": CREATE_OBJECT,
    "duplicate_object": DUPLICATE_OBJECT,
    "delete_object": DELETE_OBJECT,
    "set_material_color": SET_MATERIAL_COLOR,
    "create_wall": CREATE_WALL,
    "create_slab": CREATE_SLAB,
    "create_opening": CREATE_OPENING,
    "create_placeholder": CREATE_PLACEHOLDER,
    "export_glb": EXPORT_GLB,
}


class ScriptParameterError(ValueError):
    """Raised when a parameter value is not a safely encodable canonical value."""


def _check_encodable(value: Any, path: str = "params") -> None:
    """Reject anything whose ``repr`` is not a plain Python literal.

    This is what makes ``repr`` a safe encoder: only scalars and containers of
    scalars are allowed through, so no object's ``__repr__`` can inject syntax.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ScriptParameterError(f"{path}: non-finite float")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_encodable(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ScriptParameterError(f"{path}: non-string key {key!r}")
            _check_encodable(item, f"{path}.{key}")
        return
    raise ScriptParameterError(f"{path}: unsupported type {type(value).__name__}")


def render(script_name: str, params: Mapping[str, Any]) -> str:
    """Render a platform script with validated parameters.

    The source text is fixed. Parameters are substituted at exactly one site, as a
    single ``repr``-encoded literal, so a hostile value cannot alter the program's
    structure.
    """
    if script_name not in SCRIPTS:
        raise KeyError(f"unknown platform script: {script_name}")
    _check_encodable(dict(params))
    body = _PREAMBLE + SCRIPTS[script_name] + _FOOTER
    return body.replace(PARAMS_PLACEHOLDER, repr(dict(params)))
