"""The material helper injected into every model-authored Blender run.

    studio_materials (catalogue + cached PNGs)
        v
    material_helper_source()        <- this module, runs on the HOST
        v
    prepended to the model's code inside _PERSIST_HEAD
        v
    studio_material(obj, "oak_floor")      <- what Astra actually writes

WHY THE HELPER EXISTS RATHER THAN LETTING THE MODEL WIRE NODES

Two hard constraints, neither of which the model can be asked to remember
reliably on every turn.

**1. Paths would force an approval prompt.** Astra's code is risk-classified
before it runs, and an absolute path literal is one of the things that stops and
asks the user for permission. Code containing
``bpy.data.images.load("/…/oak_floor_base_color.png")`` would therefore interrupt
the user for every textured material. So the paths are baked in HERE, in platform
code the classifier never inspects, and the model only ever names a material.

**2. A wrong node graph is invisible until it reaches the browser.** The preview
renders in Blender, but the interactive viewer renders an exported GLB, and glTF
can only carry image textures sampled through a UV map. A graph using Generated or
Object texture coordinates through a Mapping node renders perfectly in EEVEE and
arrives in the browser as untextured grey. Getting this right once, in one place,
is the difference between a material that works and one that only appears to.

So the helper owns three things the model should not have to: real UV coordinates,
a correct Principled BSDF graph, and the colour-space flags.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

#: Name of the helper the model calls. Documented in the prompt.
HELPER_NAME = "studio_material"

#: Custom property recording which catalogue material an object was given, so a
#: later turn (and the scene read) can tell what something is clad in.
MATERIAL_PROPERTY = "studio_material"


def material_helper_source(index: Mapping[str, Any]) -> str:
    """Python source defining the material helpers, with the library baked in.

    ``index`` comes from :func:`studio_materials.library_index` and carries the
    resolved PNG paths. It is embedded as a JSON literal rather than interpolated
    as code, so a path can never be executed as a program.
    """
    # json.dumps produces a Python-legal literal for dicts of str/float, and it
    # cannot emit anything executable. This is the only place paths enter Blender.
    #
    # The literal is embedded inside a triple-quoted string and parsed with
    # json.loads at runtime rather than pasted as a dict display, so even a path
    # containing quotes or a backslash cannot terminate the literal early and
    # become code. json.dumps escapes both.
    library_literal = json.dumps(index, sort_keys=True)
    if '"""' in library_literal or "\\" in library_literal.replace("\\\\", ""):
        # Defensive: a path that could break out of the triple-quoted literal is
        # refused outright rather than embedded and hoped about.
        library_literal = json.dumps({})

    return _HELPER_TEMPLATE.replace(
        "__STUDIO_LIBRARY_JSON__", library_literal
    ).replace("__STUDIO_MATERIAL_PROPERTY__", MATERIAL_PROPERTY)


#: The helper source. Written as a template with one substitution point so the
#: whole body stays readable as ordinary Python rather than as escaped fragments.
#:
#: Everything in here is platform-owned and fixed: the model cannot influence it,
#: and it only uses ``bpy``/``bmesh`` scene APIs.
_HELPER_TEMPLATE = '''
import json as _studio_json

_STUDIO_LIBRARY = _studio_json.loads("""__STUDIO_LIBRARY_JSON__""")

#: Records what an object is clad in, so a later turn can read it back.
_STUDIO_MATERIAL_PROPERTY = "__STUDIO_MATERIAL_PROPERTY__"


def studio_materials_available():
    """Names of every material in the studio library."""
    return sorted(_STUDIO_LIBRARY)


def _studio_load_image(path, non_colour):
    """Load a texture once and reuse it.

    ``check_existing`` makes repeated calls share one datablock, which matters:
    seventeen walls clad in the same brick should reference one image, not
    seventeen copies of it in the .blend and in the exported GLB.
    """
    image = _studio_bpy.data.images.load(path, check_existing=True)
    if non_colour:
        # Roughness and normal maps are DATA, not colour. Left as sRGB, Blender
        # applies a transfer curve to them and the surface comes out with the
        # wrong gloss and inverted-looking bumps.
        try:
            image.colorspace_settings.name = "Non-Color"
        except (TypeError, AttributeError):
            pass
    image.alpha_mode = "NONE"
    return image


#: Largest dimension, in metres, that ``space="auto"`` treats as furniture.
#:
#: Chosen against a real project: its reception desk parts are 0.1-3.2 m, while its
#: walls, floors and slabs are 7-47 m. Anything between is a judgement call, and
#: Astra can always say which it wants.
_STUDIO_FURNITURE_MAX_METERS = 4.0


def _studio_world_bounds(objects):
    """Combined world-space bounding-box centre of several objects, or None."""
    import mathutils

    lo = None
    hi = None
    for obj in objects:
        matrix = getattr(obj, "matrix_world", None)
        bound = getattr(obj, "bound_box", None)
        if matrix is None or bound is None:
            continue
        for corner in bound:
            point = matrix @ mathutils.Vector(corner)
            if lo is None:
                lo = point.copy()
                hi = point.copy()
                continue
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
    if lo is None:
        return None, 0.0
    centre = (lo + hi) * 0.5
    extent = max((hi[axis] - lo[axis]) for axis in range(3))
    return centre, extent


def studio_box_uv(obj, tile_meters=1.0, anchor=None):
    """Give a mesh real-world-scaled UVs by box projection.

    Each face is projected along whichever world axis it most faces, using world
    coordinates divided by the tile size. That produces brick that is the same size
    on every wall regardless of how the wall was modelled, and — unlike a Mapping
    node — it is genuine UV data, so it survives export to glTF and shows up in the
    browser.

    ``anchor`` is what the pattern is measured FROM, and it decides something
    visible. With no anchor the projection uses raw world coordinates, so a pattern
    is continuous across separate objects: a floor built from three slabs looks like
    one floor, which is right. But the same property put a reception desk's plank
    joints on the very same world grid as the oak floor beneath it, perfectly
    aligned, so the desk read as a raised piece of the floor rather than as a
    separate object. Passing the object's own bounding-box centre as the anchor
    gives the piece its own phase and breaks that false continuity.

    Scale is unaffected either way — the anchor only shifts the pattern, so a
    0.075 m brick stays 0.075 m.

    The projection is written into the mesh's ACTIVE UV layer, replacing whatever
    was there, and a layer is created only when the mesh has none. Adding a second
    layer instead would leave the primitive's original unit-square UVs in the file:
    the exporter then writes TEXCOORD_0 and TEXCOORD_1, doubles the UV data in the
    GLB, and leaves the material depending on the viewer honouring a non-zero
    ``texCoord`` index. One UV set is smaller and has one fewer thing to go wrong.
    """
    import bmesh
    import mathutils

    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "polygons"):
        return False

    tile = float(tile_meters) or 1.0
    origin = mathutils.Vector(anchor) if anchor is not None else mathutils.Vector(
        (0.0, 0.0, 0.0)
    )

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        if not bm.faces:
            return False
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            uv_layer = bm.loops.layers.uv.new("StudioUV")

        matrix = obj.matrix_world
        rotation = matrix.to_3x3()

        for face in bm.faces:
            normal = rotation @ face.normal
            axis = 0
            best = -1.0
            for candidate in (0, 1, 2):
                magnitude = abs(normal[candidate])
                if magnitude > best:
                    best = magnitude
                    axis = candidate
            for loop in face.loops:
                world = (matrix @ loop.vert.co) - origin
                if axis == 0:
                    u, v = world.y, world.z
                elif axis == 1:
                    u, v = world.x, world.z
                else:
                    u, v = world.x, world.y
                loop[uv_layer].uv = (u / tile, v / tile)

        bm.to_mesh(mesh)
    finally:
        bm.free()

    # Whichever layer was written is the one rendering and export must use.
    layers = getattr(mesh, "uv_layers", None)
    if layers is not None and layers.active is not None:
        try:
            layers.active.active_render = True
        except (AttributeError, TypeError):
            pass
    mesh.update()
    return True


def studio_material(target, name, tile_meters=None, space="auto", reuse=True):
    """Clad an object (or objects) in a library material.

    ``target`` may be one object or any iterable of objects. Returns the Blender
    material, or ``None`` when the name is not in the library — never raises, so a
    wrong name costs one untextured surface rather than the whole scene.

    ``space`` decides where the texture pattern is measured from:

    ``"world"``
        Continuous across objects. Correct for architecture — a floor made of
        three slabs, or a wall run split into segments, should look like one
        surface.
    ``"object"``
        Anchored to the piece's own bounding box. Correct for furniture and
        joinery, which are separate objects sitting on the architecture and must
        not inherit its grid.
    ``"auto"`` (default)
        World for anything larger than ``_STUDIO_FURNITURE_MAX_METERS``, object for
        anything smaller.

    Passing SEVERAL objects in one call makes them share one anchor, so a desk
    supplied as ``[top, body, plinth]`` is patterned as one piece of furniture
    rather than three. That is the reason to group a call rather than loop.
    """
    entry = _STUDIO_LIBRARY.get(name)
    if entry is None:
        print("studio_material: unknown material %r" % (name,))
        return None

    objects = [target] if hasattr(target, "data") else list(target or ())
    objects = [o for o in objects if getattr(o, "data", None) is not None]
    if not objects:
        return None

    tile = float(tile_meters) if tile_meters else float(entry.get("tile_meters", 1.0))

    centre, extent = _studio_world_bounds(objects)
    mode = str(space or "auto").lower()
    if mode == "auto":
        mode = "object" if extent and extent <= _STUDIO_FURNITURE_MAX_METERS else "world"
    anchor = centre if (mode == "object" and centre is not None) else None

    key = "StudioMat_%s_%g" % (name, tile)
    material = _studio_bpy.data.materials.get(key) if reuse else None
    if material is None:
        material = _studio_bpy.data.materials.new(key)
        material.use_nodes = True
        _studio_build_pbr(material, entry)
        if reuse:
            material.name = key

    for obj in objects:
        mesh = getattr(obj, "data", None)
        if mesh is None or not hasattr(mesh, "materials"):
            continue
        studio_box_uv(obj, tile, anchor=anchor)
        mesh.materials.clear()
        mesh.materials.append(material)
        obj[_STUDIO_MATERIAL_PROPERTY] = name

    return material


def _studio_build_pbr(material, entry):
    """Wire a Principled BSDF that both EEVEE and the glTF exporter understand.

    Only the inputs glTF has a representation for are textured: base colour,
    roughness and normal. Nothing here uses a Mapping or Texture Coordinate node,
    because the UVs are baked into the mesh instead — that is precisely what makes
    the material survive the trip to the browser.
    """
    tree = material.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)

    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (520, 0)
    principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (200, 0)
    tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    maps = entry.get("maps") or {}

    base_path = maps.get("base_color")
    if base_path:
        node = tree.nodes.new("ShaderNodeTexImage")
        node.location = (-360, 240)
        node.image = _studio_load_image(base_path, non_colour=False)
        tree.links.new(node.outputs["Color"], principled.inputs["Base Color"])

    rough_path = maps.get("roughness")
    if rough_path:
        node = tree.nodes.new("ShaderNodeTexImage")
        node.location = (-360, -40)
        node.image = _studio_load_image(rough_path, non_colour=True)
        _studio_link_if_present(tree, node.outputs["Color"], principled, "Roughness")
    else:
        _studio_set_if_present(principled, "Roughness", float(entry.get("roughness", 0.5)))

    normal_path = maps.get("normal")
    if normal_path:
        node = tree.nodes.new("ShaderNodeTexImage")
        node.location = (-360, -320)
        node.image = _studio_load_image(normal_path, non_colour=True)
        normal_map = tree.nodes.new("ShaderNodeNormalMap")
        normal_map.location = (-80, -320)
        # uv_map is deliberately left empty, meaning "the active UV map". One
        # material is shared by many objects, so naming a specific layer here would
        # break on any object whose layer is called something else.
        tree.links.new(node.outputs["Color"], normal_map.inputs["Color"])
        _studio_link_if_present(tree, normal_map.outputs["Normal"], principled, "Normal")

    _studio_set_if_present(principled, "Metallic", float(entry.get("metallic", 0.0)))


def _studio_link_if_present(tree, socket, node, input_name):
    """Link only if the input exists.

    Principled BSDF input names have changed across Blender versions, and a
    material that half-built is better than a scene that died on a KeyError.
    """
    target = node.inputs.get(input_name)
    if target is not None:
        tree.links.new(socket, target)


def _studio_set_if_present(node, input_name, value):
    target = node.inputs.get(input_name)
    if target is not None:
        try:
            target.default_value = value
        except (TypeError, ValueError):
            pass
'''


__all__ = ["HELPER_NAME", "MATERIAL_PROPERTY", "material_helper_source"]
