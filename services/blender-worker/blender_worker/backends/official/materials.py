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


def studio_box_uv(obj, tile_meters=1.0):
    """Give a mesh real-world-scaled UVs by box projection.

    Each face is projected along whichever world axis it most faces, using WORLD
    coordinates divided by the tile size. That produces brick that is the same
    size on every wall regardless of how the wall was modelled, and — unlike a
    Mapping node — it is genuine UV data, so it survives export to glTF and shows
    up in the browser.

    The projection is written into the mesh's ACTIVE UV layer, replacing whatever
    was there, and a layer is created only when the mesh has none. Adding a second
    layer instead would leave the primitive's original unit-square UVs in the file:
    the exporter then writes TEXCOORD_0 and TEXCOORD_1, doubles the UV data in the
    GLB, and leaves the material depending on the viewer honouring a non-zero
    ``texCoord`` index. One UV set is smaller and has one fewer thing to go wrong.
    """
    import bmesh

    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "polygons"):
        return False

    tile = float(tile_meters) or 1.0

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
                world = matrix @ loop.vert.co
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


def studio_material(target, name, tile_meters=None, reuse=True):
    """Clad an object (or objects) in a library material.

    ``target`` may be one object or any iterable of objects. Returns the Blender
    material, or ``None`` when the name is not in the library — never raises, so a
    wrong name costs one untextured surface rather than the whole scene.
    """
    entry = _STUDIO_LIBRARY.get(name)
    if entry is None:
        print("studio_material: unknown material %r" % (name,))
        return None

    objects = [target] if hasattr(target, "data") else list(target or ())
    if not objects:
        return None

    tile = float(tile_meters) if tile_meters else float(entry.get("tile_meters", 1.0))

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
        studio_box_uv(obj, tile)
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
