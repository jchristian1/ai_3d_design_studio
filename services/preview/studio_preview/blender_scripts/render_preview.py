"""Renders a realistic preview image. Runs INSIDE Blender.

Spec 001, Task 10. Invoked through the centralized runtime helper, never by a
hand-typed Blender path:

    blender --background --factory-startup --python render_preview.py

Environment:
    PREVIEW_BLEND       path of the .blend to render (required, server-resolved)
    PREVIEW_OUTPUT      path of the PNG to write (required, server-chosen)
    PREVIEW_WIDTH       pixel width (required)
    PREVIEW_HEIGHT      pixel height (required)
    STUDIO_PYTHONPATH   os.pathsep-joined import roots for the studio packages

THE CENTRAL RULE: THIS SCRIPT NEVER SAVES THE PROJECT
-----------------------------------------------------
A preview is a READ of a saved design. This script opens the .blend, adds a
temporary camera and (only when the scene has none of its own) temporary lighting
to the IN-MEMORY scene, renders, and exits. There is no ``save_as_mainfile`` and no
``save_mainfile`` anywhere in it, so the design file on disk is byte-identical
afterwards.

That is what makes "add a camera and a sun" safe. The alternative — permanently
adding them to the user's scene so previews have something to render through —
would mean every preview silently edits the design, and the user would find objects
they never asked for. A test asserts the .blend's SHA-256 is unchanged by rendering.

CAMERA STRATEGY
---------------
The camera is COMPUTED FROM THE SCENE, not hard-coded. A fixed viewpoint framed a
small scene near the origin and nothing else, which meant a house or a landscape
was mostly out of frame — a perfectly lit render of the wrong thing.

So the framing is derived:

  - the scene's renderable bounding box gives a centre and a radius;
  - the camera sits on a fixed three-quarter DIRECTION from that centre;
  - the DISTANCE is solved from the camera's vertical field of view so the whole
    bounding sphere fits, with a margin.

The direction is deliberately off-axis on X, so a movement along world +X still
appears as a clear horizontal displacement — the original property this preview
was built to show. An empty scene falls back to a fixed framing near the origin.
Everything is derived from geometry, so it is deterministic and contains no magic
Euler angles.

RENDER SETTINGS
---------------
EEVEE, at a fixed small resolution, with sun-dominant sky lighting and the Khronos
PBR Neutral view transform.

The lighting and tone mapping are CALIBRATED against known textures rather than
chosen by eye — see ``SUN_ENERGY`` and ``VIEW_TRANSFORMS``, both of which record
the measurements. Getting this wrong is not subtle: the first version rendered an
oak floor as pale cream and red brick as pink, which would have made the material
library useless for judging a design.

Why EEVEE rather than Workbench: Workbench is a solid-shading rasteriser that
IGNORES materials and textures by design. It cannot show a wood floor, a brick
wall, a reflection or a real shadow, so it could never answer "does this look
real?". EEVEE renders actual materials, shadows and raytraced reflections, which is
the entire point of the preview now.

What that costs, stated plainly:

  - The first render in a process pays shader compilation (~10 s observed);
    subsequent renders are a fraction of a second.
  - EEVEE needs more GL capability than Workbench. It is verified working headless
    on the pinned Blender, and a preview failure is already a value that can never
    fail a mutation, so the worst case is "no picture", not "lost work".

DETERMINISM
-----------
``use_taa_reprojection`` is disabled and the sample count is pinned, which makes
the rendered PIXELS reproducible: two renders of one unchanged scene were measured
identical to 0.0 mean absolute difference. The PNG *container* bytes may still
differ between runs (compression is not required to be bit-stable), so the
preview's stability is asserted on pixels, not on a file checksum.
"""

from __future__ import annotations

import json
import math
import os
import sys
import traceback

for entry in os.environ.get("STUDIO_PYTHONPATH", "").split(os.pathsep):
    if entry and entry not in sys.path:
        sys.path.insert(0, entry)

# Angle conversion goes through the shared implementation, never through
# ``math.radians``. There is exactly one degree<->radian conversion site in this
# repository so that the Python and TypeScript halves cannot drift, and a guard test
# enforces it — see tests/spatial/test_conversion_site_guard.py. This is why
# STUDIO_PYTHONPATH exists.
from studio_spatial import degrees_to_radians, radians_to_degrees

RESULT_PREFIX = "RESULT_JSON:"


def _radians(degrees: float) -> float:
    """Degrees to radians through the shared converter.

    The shared function returns a validated result object; the camera and lamp code
    wants a plain float, and an invalid constant here would be a programming error
    rather than bad user input, so it fails loudly.
    """
    result = degrees_to_radians(degrees)
    if not result.ok or result.radians is None:
        raise RuntimeError(f"invalid angle constant: {degrees!r}")
    return result.radians

#: Temporary object names. Prefixed so they are obviously not user content, and
#: they only ever exist in memory.
PREVIEW_CAMERA_NAME = "__studio_preview_camera"
PREVIEW_SUN_NAME = "__studio_preview_sun"
PREVIEW_FILL_NAME = "__studio_preview_fill"
PREVIEW_WORLD_NAME = "__studio_preview_world"

#: Every name this script may introduce. Used to exclude them from the framing
#: bounding box and from the reported object list.
PREVIEW_OBJECT_NAMES = (
    PREVIEW_CAMERA_NAME,
    PREVIEW_SUN_NAME,
    PREVIEW_FILL_NAME,
)

CAMERA_LENS_MM = 50.0

#: Fixed three-quarter viewing DIRECTION (unnormalised). Off-axis on X so a
#: movement along world +X reads as a horizontal displacement.
CAMERA_DIRECTION = (1.0, -1.0, 0.62)

#: Breathing room around the fitted framing.
#:
#: Now genuinely cosmetic. It used to carry real weight, absorbing the worst-case
#: off-centre error from centre snapping when the camera fit a bounding SPHERE.
#: ``fit_distance`` projects the measured corners about the snapped centre, so that
#: drift is accounted for exactly and this is only padding.
CAMERA_MARGIN = 1.08

#: Mantissas for the radius ladder, in roughly 25% steps.
#:
#: Coarser is better for stability but wastes frame: a 1–2–5 ladder puts a scene of
#: radius 3.01 into a frame sized for 5, leaving the subject small. These steps cap
#: that waste at about 1.25x while still being coarse enough that the zoom only
#: changes when the design has genuinely grown.
RADIUS_LADDER = (1.0, 1.25, 1.6, 2.0, 2.5, 3.2, 4.0, 5.0, 6.4, 8.0, 10.0)

#: Centre snapping grid, as a fraction of the framed radius. Coarse on purpose —
#: this is what stops the camera chasing the object that just moved.
CENTRE_GRID_FRACTION = 0.5

#: Framing used when the scene has nothing renderable in it.
EMPTY_SCENE_RADIUS_METERS = 4.0
EMPTY_SCENE_CENTRE = (0.0, 0.0, 1.0)

#: Never get closer than this, so a single tiny object cannot put the camera
#: inside its own geometry.
MIN_CAMERA_DISTANCE_METERS = 1.5

#: Smallest radius the framing will consider, so a single tiny object does not
#: produce an absurdly tight frame.
MIN_FRAMED_RADIUS_METERS = 0.5

#: Sun/sky placement. Azimuth is shared by the sky texture and the sun lamp so the
#: bright part of the sky and the direction of the shadows agree.
SUN_ELEVATION_DEGREES = 38.0
SUN_AZIMUTH_DEGREES = -125.0

#: Sun and sky strength, CALIBRATED rather than chosen by eye.
#:
#: The method: light a surface whose texture's own mean colour is known, render it
#: filling the frame, and compare. A correctly exposed mid-tone should land near
#: its own albedo. The first attempt (sun 3.0, sky strength 1.0, sky sun 0.55) put
#: an oak floor at (207, 198, 187) against a real albedo of (158, 113, 69) — a
#: pale cream, with the wood's colour destroyed rather than merely brightened.
#:
#: Two separate mistakes were behind that:
#:
#: 1. The sky's own ``sun_intensity`` draws a sun disc INTO the environment, so a
#:    scene with a sun lamp as well was lit by two suns. It is now 0: the lamp is
#:    the only direct light, and the sky is purely ambient.
#: 2. The sky was far too strong relative to the sun, so most light arrived as blue
#:    ambient and every material drifted grey-blue. A daylight scene is
#:    sun-dominant.
SUN_ENERGY = 3.0
#: Angular diameter of the sun disc: gives shadows a soft edge instead of a
#: hard-clipped one, which is most of what makes a render stop looking synthetic.
SUN_ANGLE_DEGREES = 1.5
SKY_SUN_INTENSITY = 0.0

#: Visible sky brightness. Only the camera sees this (see :func:`install_sky_world`).
SKY_BACKGROUND_STRENGTH = 1.0

#: Neutral ambient used for LIGHTING, and the grey it is tinted.
#:
#: A physical blue sky is the correct thing to look at and the wrong thing to light
#: with, for a tool whose job is showing what a material looks like. Measured on
#: neutral white plaster (texture mean 231, 229, 224) lit only by ambient:
#:
#:     environment used for lighting     rendered        blue cast
#:     full sky texture                  115,151,188      +73
#:     sky mixed 50% toward grey          132,152,173      +41
#:     neutral grey                       132,131,127       -5
#:
#: A white wall coming out as (115, 151, 188) is not a subtle tint — it is blue. So
#: lighting is neutral, and the sky is kept for the backdrop only.
AMBIENT_STRENGTH = 0.55
AMBIENT_COLOUR = (0.5, 0.5, 0.5, 1.0)

#: A shadow-less wide light opposite the sun: the arch-viz bounce card. Without it
#: shaded faces read as flat dark shapes, because a neutral ambient alone has no
#: direction. It casts no shadows, so it cannot contradict the sun.
FILL_ENERGY = 1.0
FILL_ELEVATION_DEGREES = 20.0
FILL_ANGLE_DEGREES = 60.0

#: Pinned sampling. High enough to be clean, low enough to stay fast.
RENDER_SAMPLES = 64
SHADOW_RAY_COUNT = 2
SHADOW_STEP_COUNT = 6

ENGINE = "BLENDER_EEVEE"

#: View transforms in order of preference. Set explicitly rather than inherited, so
#: one design does not look different on two machines.
#:
#: Khronos PBR Neutral is first for two reasons, and it was measured rather than
#: assumed. Rendering known textures and comparing against their real albedo:
#:
#:     transform              oak err   oak saturation
#:     Khronos PBR Neutral      45          0.37
#:     Standard                 63          0.31
#:     AgX                      65          0.28
#:     Filmic                   81          0.26
#:
#: It is both the most accurate and the least desaturating, because that is what it
#: was designed for — Khronos specified it for viewing PBR assets, where the job is
#: to show a material honestly rather than cinematically. AgX is a film emulation;
#: it deliberately pulls saturation out of bright regions, which is the wrong
#: trade-off when the user is trying to judge whether they like the brick.
#:
#: The second reason matters just as much: glTF viewers use this same transform, so
#: the still preview and the interactive model in the browser agree instead of the
#: user seeing two different-looking versions of one design.
#:
#: Older Blender builds may not ship it, hence the ordered fallback.
VIEW_TRANSFORMS = ("Khronos PBR Neutral", "AgX", "Standard")

#: Stops of exposure compensation, also calibrated. The lighting above lands a
#: mid-tone slightly hot; half a stop down puts it close to its true albedo.
EXPOSURE_STOPS = -0.5


def emit(phase: str, payload: dict) -> None:
    print(f"{RESULT_PREFIX}{json.dumps({'phase': phase, **payload})}", flush=True)


# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------


def _renderable_objects(scene):
    """Objects that contribute geometry to the picture.

    Lights, cameras and empties have no visible surface, so including them would
    drag the bounding box toward things the user cannot see.
    """
    import bpy  # noqa: F401  (imported by caller; kept local for clarity)

    renderable = []
    for obj in scene.objects:
        if obj.name in PREVIEW_OBJECT_NAMES:
            continue
        if obj.type not in {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME"}:
            continue
        if obj.hide_render:
            continue
        renderable.append(obj)
    return renderable


def scene_corners(scene):
    """World-space corners of the renderable scene's bounding box, or ``[]``.

    The eight corners rather than a radius, because the camera fit projects them
    individually — an elongated building cannot be framed well from a single
    scalar.
    """
    import mathutils

    points = []
    for obj in _renderable_objects(scene):
        matrix = obj.matrix_world
        # bound_box is in LOCAL space; transforming all eight corners is what makes
        # the result correct for a rotated or scaled object.
        for corner in obj.bound_box:
            points.append(matrix @ mathutils.Vector(corner))

    if not points:
        return []

    minimum = mathutils.Vector(
        (
            min(p.x for p in points),
            min(p.y for p in points),
            min(p.z for p in points),
        )
    )
    maximum = mathutils.Vector(
        (
            max(p.x for p in points),
            max(p.y for p in points),
            max(p.z for p in points),
        )
    )
    return [
        mathutils.Vector((x, y, z))
        for x in (minimum.x, maximum.x)
        for y in (minimum.y, maximum.y)
        for z in (minimum.z, maximum.z)
    ]


def scene_bounds(scene):
    """World-space ``(centre, radius)`` of everything renderable.

    Returns the empty-scene fallback when there is no geometry, so the caller
    never has to special-case an empty project.
    """
    import mathutils

    corners = []
    for obj in _renderable_objects(scene):
        matrix = obj.matrix_world
        # bound_box is in LOCAL space; transforming all eight corners is what makes
        # the result correct for a rotated or scaled object.
        for corner in obj.bound_box:
            corners.append(matrix @ mathutils.Vector(corner))

    if not corners:
        return mathutils.Vector(EMPTY_SCENE_CENTRE), EMPTY_SCENE_RADIUS_METERS

    minimum = mathutils.Vector(
        (
            min(c.x for c in corners),
            min(c.y for c in corners),
            min(c.z for c in corners),
        )
    )
    maximum = mathutils.Vector(
        (
            max(c.x for c in corners),
            max(c.y for c in corners),
            max(c.z for c in corners),
        )
    )
    centre = (minimum + maximum) * 0.5
    radius = (maximum - centre).length
    if radius <= 0.0:
        # A single point or a zero-size object: give it something to look at.
        radius = EMPTY_SCENE_RADIUS_METERS
    return centre, radius


def vertical_fov(camera_data, width: int, height: int) -> float:
    """The camera's vertical field of view in radians.

    The vertical axis is the binding constraint at preview aspect ratios (the
    image is wider than it is tall), so fitting the bounding sphere to THIS angle
    is what guarantees nothing is cropped off the top or bottom.
    """
    sensor_width = getattr(camera_data, "sensor_width", 36.0) or 36.0
    lens = camera_data.lens or CAMERA_LENS_MM
    aspect = (width / height) if height else 1.0
    sensor_height = sensor_width / aspect if aspect else sensor_width
    return 2.0 * math.atan((sensor_height * 0.5) / lens)


def snap_radius_up(radius: float) -> float:
    """Round a radius UP onto a coarse ladder of nice values.

    Snapping, rather than using the measured radius, is half of what keeps the
    camera still between turns: a wall growing by five centimetres must not
    silently rescale the whole picture.
    """
    radius = max(radius, MIN_FRAMED_RADIUS_METERS)
    decade = 10.0 ** math.floor(math.log10(radius))
    for mantissa in RADIUS_LADDER:
        candidate = mantissa * decade
        if candidate >= radius - 1e-9:
            return candidate
    return 10.0 * decade  # pragma: no cover - ladder ends at 10.0


def framed_bounds(scene):
    """The centre and radius the camera should actually frame.

    THIS IS THE SUBTLE PART, and it is worth being explicit about why it is not
    just the bounding box.

    Framing the exact bounding box every render would mean the camera FOLLOWS the
    geometry. In a scene whose only object is a cube, moving that cube 0.5 m would
    move the camera 0.5 m with it and produce a byte-for-byte identical picture —
    the preview would faithfully report "nothing changed" about the one thing that
    did. That is not hypothetical: it is precisely the guarantee Spec 001 built
    this preview around ("a move along +X must be visibly horizontal").

    Framing a FIXED viewpoint instead — what this script used to do — keeps
    movement visible but puts a house, or anything not sitting on the origin,
    outside the frame entirely.

    So the bounds are quantised rather than exact:

      - the radius is snapped UP onto a coarse ladder;
      - the centre is snapped to a grid derived from that radius.

    Small changes then leave the framing bit-identical, so they show up as
    movement within a stable frame, while a design that genuinely outgrows the
    frame crosses a grid boundary and gets re-framed. The camera behaves like a
    tripod that is only occasionally repositioned, which is also what a person
    would do.

    The cost is that the subject can sit off-centre, so ``CAMERA_MARGIN`` has to
    absorb that. The bound is exact rather than hopeful:

        grid            g = 0.5 R
        max axis error  g / 2            = 0.25 R
        max 3D drift    sqrt(3) · 0.25 R ~ 0.433 R
        max extent      r + drift        <= 1.433 R      (since r <= R)

    which is why the margin is 1.5 and not something rounder. Note what is
    deliberately NOT done here: the framed radius is not grown by the measured
    drift. Doing that would make the zoom level a function of position again, so
    nudging an object would gently rescale the whole image — the wobble this
    quantisation exists to remove.
    """
    import mathutils

    centre, radius = scene_bounds(scene)

    framed_radius = snap_radius_up(radius)
    grid = framed_radius * CENTRE_GRID_FRACTION

    snapped = mathutils.Vector(
        (
            round(centre.x / grid) * grid,
            round(centre.y / grid) * grid,
            round(centre.z / grid) * grid,
        )
    )
    return snapped, framed_radius


def horizontal_fov(camera_data, width: int, height: int) -> float:
    """The camera's horizontal field of view in radians."""
    sensor_width = getattr(camera_data, "sensor_width", 36.0) or 36.0
    lens = camera_data.lens or CAMERA_LENS_MM
    aspect = (width / height) if height else 1.0
    if aspect < 1.0:
        # A portrait frame fits to height, so the horizontal sensor shrinks.
        sensor_width = sensor_width * aspect
    return 2.0 * math.atan((sensor_width * 0.5) / lens)


def fit_distance(corners, centre, direction, tan_h: float, tan_v: float) -> float:
    """Distance along ``direction`` that just contains every corner in frame.

    WHY A BOX AND NOT A SPHERE

    Fitting the bounding SPHERE is the obvious approach and it is badly wrong for
    architecture. A real clinic measured 81 x 21 x 4.3 m: long, wide and flat. Its
    bounding sphere is 84 m across, nearly all of it empty air above and below the
    building, so a camera that fits the sphere vertically renders the building as a
    speck across the middle of the frame. The user could not see their own design.

    So each of the eight box corners is projected into the camera's own basis and
    the distance is solved so that all of them fall inside both the horizontal and
    the vertical field of view:

        a point at offset v from the centre sits at depth  D - v·direction
        and must satisfy  |v·right| <= depth · tan(h/2)
                          |v·up|    <= depth · tan(v/2)

    Taking the maximum over the corners gives the exact distance. An elongated
    scene is then framed along its length, which is what a person would do.

    Because the corners are MEASURED but the centre is QUANTISED, the snapping
    drift is inside this calculation rather than absorbed by a safety margin —
    nothing can be cropped by it.
    """
    import mathutils

    forward = -mathutils.Vector(direction)
    # Any two vectors perpendicular to forward will do; world up is the natural
    # reference, and the fallback covers a camera looking straight down.
    world_up = mathutils.Vector((0.0, 0.0, 1.0))
    if abs(forward.dot(world_up)) > 0.999:
        world_up = mathutils.Vector((0.0, 1.0, 0.0))
    right = forward.cross(world_up).normalized()
    up = right.cross(forward).normalized()

    required = MIN_CAMERA_DISTANCE_METERS
    for corner in corners:
        offset = corner - centre
        along = offset.dot(mathutils.Vector(direction))
        horizontal = abs(offset.dot(right)) / max(tan_h, 1e-6)
        vertical = abs(offset.dot(up)) / max(tan_v, 1e-6)
        required = max(required, max(horizontal, vertical) + along)
    return required


def place_camera(scene, camera, width: int, height: int) -> dict:
    """Point the camera at the scene and back off far enough to contain it."""
    import mathutils

    measured_centre, measured_radius = scene_bounds(scene)
    centre, radius = framed_bounds(scene)

    fov = vertical_fov(camera.data, width, height)
    fov_h = horizontal_fov(camera.data, width, height)
    tan_v = math.tan(max(fov * 0.5, 1e-4))
    tan_h = math.tan(max(fov_h * 0.5, 1e-4))

    direction = mathutils.Vector(CAMERA_DIRECTION).normalized()

    corners = scene_corners(scene)
    if corners:
        distance = fit_distance(corners, centre, direction, tan_h, tan_v)
        # Pad, then snap the distance onto the same coarse ladder the radius uses.
        # Quantising here is what keeps the camera still between turns: without it
        # the zoom would drift by a fraction of a percent every time a wall moved.
        distance = snap_radius_up(distance * CAMERA_MARGIN)
    else:
        distance = max(
            (radius * CAMERA_MARGIN) / tan_v, MIN_CAMERA_DISTANCE_METERS
        )
    distance = max(distance, MIN_CAMERA_DISTANCE_METERS)

    camera.location = centre + direction * distance

    # Derive the look-at rotation from the geometry rather than hard-coding Euler
    # angles, so the framing stays correct when the viewpoint moves.
    to_target = centre - camera.location
    camera.rotation_euler = to_target.to_track_quat("-Z", "Y").to_euler()

    # Clip planes scaled to the scene: a fixed near/far plane either clipped a
    # large building or destroyed depth precision on a small one.
    camera.data.clip_start = max(distance * 0.001, 0.01)
    camera.data.clip_end = max(distance * 10.0, radius * 20.0, 100.0)

    return {
        "centre_meters": [round(v, 6) for v in centre],
        "radius_meters": round(radius, 6),
        "measured_centre_meters": [round(v, 6) for v in measured_centre],
        "measured_radius_meters": round(measured_radius, 6),
        "distance_meters": round(distance, 6),
        "horizontal_fov_degrees": round(radians_to_degrees(fov_h) or 0.0, 4),
        # Diagnostic only. The shared converter returns None for a non-finite input,
        # which cannot happen for a computed field of view, but a reported number must
        # not be the thing that breaks a render.
        "vertical_fov_degrees": round(radians_to_degrees(fov) or 0.0, 4),
    }


# ---------------------------------------------------------------------------
# lighting
# ---------------------------------------------------------------------------


def scene_has_own_lights(scene) -> bool:
    """True when the design brings its own lights."""
    return any(
        obj.type == "LIGHT" and obj.name not in PREVIEW_OBJECT_NAMES
        for obj in scene.objects
    )


def scene_has_own_world_lighting(scene) -> bool:
    """True when the scene's world would actually emit light.

    A world that exists but is black lights nothing, and treating it as "the user's
    choice" is how a scene renders pitch dark. So the test is whether it emits,
    not whether it is present.
    """
    world = scene.world
    if world is None:
        return False
    if not world.use_nodes or world.node_tree is None:
        return any(channel > 0.0 for channel in world.color)

    for node in world.node_tree.nodes:
        if node.bl_idname in {"ShaderNodeTexSky", "ShaderNodeTexEnvironment"}:
            return True
        if node.bl_idname in {"ShaderNodeBackground", "ShaderNodeEmission"}:
            strength = node.inputs.get("Strength")
            colour = node.inputs.get("Color")
            if strength is not None and strength.default_value <= 0.0:
                continue
            if colour is not None and not any(
                channel > 0.0 for channel in colour.default_value[:3]
            ):
                continue
            return True
    return False


def install_sky_world(scene) -> None:
    """Sky for the camera, neutral grey for the lighting. In memory only.

    The world is split by ``Light Path > Is Camera Ray``, which EEVEE honours:

        camera rays    -> Sky Texture      the backdrop the user sees
        light rays     -> neutral grey     what actually illuminates the design

    That split is the resolution of a genuine conflict. A real sky is what makes an
    exterior look like an exterior, but lighting a scene with it tints every
    surface: a white wall in shade comes out (115, 151, 188), and the user cannot
    tell whether the plaster they picked is white or blue. Desaturating the sky
    fixes the tint and throws away the backdrop with it.

    Splitting the two keeps a sky to look at AND materials that look like
    themselves — verified: backdrop blueness +49, white wall cast -4.

    A Sky Texture is used rather than a shipped HDRI so there is no asset to
    install and nothing to licence.
    """
    import bpy

    world = bpy.data.worlds.new(PREVIEW_WORLD_NAME)
    scene.world = world
    world.use_nodes = True
    tree = world.node_tree

    for node in list(tree.nodes):
        tree.nodes.remove(node)

    output = tree.nodes.new("ShaderNodeOutputWorld")

    sky = tree.nodes.new("ShaderNodeTexSky")
    sky.sky_type = "MULTIPLE_SCATTERING"
    sky.sun_elevation = _radians(SUN_ELEVATION_DEGREES)
    sky.sun_rotation = _radians(SUN_AZIMUTH_DEGREES)
    # The sky's own sun disc is off: the sun lamp is the only direct light, and
    # having both meant the scene was lit by two suns.
    sky.sun_intensity = SKY_SUN_INTENSITY

    visible = tree.nodes.new("ShaderNodeBackground")
    visible.inputs["Strength"].default_value = SKY_BACKGROUND_STRENGTH
    tree.links.new(sky.outputs[0], visible.inputs[0])

    ambient = tree.nodes.new("ShaderNodeBackground")
    ambient.inputs["Color"].default_value = AMBIENT_COLOUR
    ambient.inputs["Strength"].default_value = AMBIENT_STRENGTH

    light_path = tree.nodes.new("ShaderNodeLightPath")
    mix = tree.nodes.new("ShaderNodeMixShader")
    # Fac = 1 for camera rays, which selects the SECOND shader input.
    tree.links.new(light_path.outputs["Is Camera Ray"], mix.inputs["Fac"])
    tree.links.new(ambient.outputs[0], mix.inputs[1])
    tree.links.new(visible.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs[0])


def _aim_along(elevation_degrees: float, azimuth_degrees: float):
    """Rotation that points a lamp's -Z along the direction light TRAVELS.

    Derived from elevation and azimuth rather than hard-coded, so the lamp and the
    sky texture cannot disagree about where the sun is — shadows falling towards
    the bright part of the sky is the sort of wrongness everyone feels and nobody
    can name.
    """
    import mathutils

    elevation = _radians(elevation_degrees)
    azimuth = _radians(azimuth_degrees)
    towards_source = mathutils.Vector(
        (
            math.cos(elevation) * math.cos(azimuth),
            math.cos(elevation) * math.sin(azimuth),
            math.sin(elevation),
        )
    )
    return (-towards_source).to_track_quat("-Z", "Y").to_euler()


def install_sun(scene) -> None:
    """Add the temporary key sun and its fill, matched to the sky's sun position."""
    import bpy

    data = bpy.data.lights.new(PREVIEW_SUN_NAME, type="SUN")
    data.energy = SUN_ENERGY
    if hasattr(data, "angle"):
        data.angle = _radians(SUN_ANGLE_DEGREES)

    sun = bpy.data.objects.new(PREVIEW_SUN_NAME, data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = _aim_along(SUN_ELEVATION_DEGREES, SUN_AZIMUTH_DEGREES)

    fill_data = bpy.data.lights.new(PREVIEW_FILL_NAME, type="SUN")
    fill_data.energy = FILL_ENERGY
    if hasattr(fill_data, "angle"):
        fill_data.angle = _radians(FILL_ANGLE_DEGREES)
    # No shadows: a fill that cast them would invent a second, contradictory sun.
    fill_data.use_shadow = False

    fill = bpy.data.objects.new(PREVIEW_FILL_NAME, fill_data)
    scene.collection.objects.link(fill)
    fill.rotation_euler = _aim_along(
        FILL_ELEVATION_DEGREES, SUN_AZIMUTH_DEGREES + 180.0
    )


def configure_lighting(scene) -> dict:
    """Supplement the scene's lighting; never override what it already has.

    Astra can author its own lights and world, and a preview that replaced them
    would hide the very thing the user asked for. So each half is added ONLY when
    the scene has nothing of its own, and what was added is reported.
    """
    added_world = not scene_has_own_world_lighting(scene)
    if added_world:
        install_sky_world(scene)

    added_sun = not scene_has_own_lights(scene)
    if added_sun:
        install_sun(scene)

    return {"added_sky_world": added_world, "added_sun": added_sun}


# ---------------------------------------------------------------------------
# render settings
# ---------------------------------------------------------------------------


def configure_render(scene, width: int, height: int, output_path: str) -> str:
    render = scene.render
    render.engine = ENGINE
    render.resolution_x = width
    render.resolution_y = height
    render.resolution_percentage = 100
    render.film_transparent = False
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    render.image_settings.compression = 15
    render.filepath = output_path
    # Motion blur needs a time dimension a still preview does not have, and it is
    # a source of variation between runs.
    if hasattr(render, "use_motion_blur"):
        render.use_motion_blur = False

    strip_metadata(render)
    configure_eevee(scene)
    return configure_colour_management(scene)


def strip_metadata(render) -> None:
    """Remove burned-in and embedded render metadata.

    Blender stamps render metadata into PNG tEXt chunks. Left enabled, every
    served preview embeds:

        File\0/abs/path/to/the/project.blend      <- absolute server path
        Date\02026/09/15 05:40:12                 <- wall-clock timestamp
        RenderTime\000:00.24                      <- measured duration
        Camera, Scene, Frame, Time, ...

    The `File` chunk is a genuine information leak: the artifact is served to a
    browser, so the absolute path of the design file — and the project filename,
    which may be a client's name — would travel with the image. JSON responses are
    carefully path-free; the image bytes must be too.
    """
    render.use_stamp = False  # no burn-in overlay
    for stamp_flag in (
        "use_stamp_filename",
        "use_stamp_date",
        "use_stamp_time",
        "use_stamp_render_time",
        "use_stamp_frame",
        "use_stamp_frame_range",
        "use_stamp_scene",
        "use_stamp_camera",
        "use_stamp_lens",
        "use_stamp_hostname",
        "use_stamp_memory",
        "use_stamp_note",
        "use_stamp_marker",
        "use_stamp_sequencer_strip",
    ):
        # setattr rather than direct assignment: the exact set of stamp flags
        # varies slightly between Blender versions, and an unknown flag must not
        # break rendering.
        if hasattr(render, stamp_flag):
            setattr(render, stamp_flag, False)


def configure_eevee(scene) -> None:
    """Quality and reproducibility settings for EEVEE.

    Raytracing is the setting that buys realism: without it EEVEE has no
    reflections and only a crude ambient term, so glass, polished floors and
    interior corners all look wrong. ``use_taa_reprojection`` is disabled because
    reusing samples across frames makes a still render depend on history, which is
    exactly the kind of variation a preview must not have.
    """
    eevee = getattr(scene, "eevee", None)
    if eevee is None:  # pragma: no cover - EEVEE is always present on 5.x
        return

    _set(eevee, "taa_render_samples", RENDER_SAMPLES)
    _set(eevee, "use_taa_reprojection", False)
    _set(eevee, "use_shadows", True)
    _set(eevee, "shadow_ray_count", SHADOW_RAY_COUNT)
    _set(eevee, "shadow_step_count", SHADOW_STEP_COUNT)
    _set(eevee, "use_raytracing", True)

    options = getattr(eevee, "ray_tracing_options", None)
    if options is not None:
        # Full-resolution tracing: the preview is small, so the cost is minor and
        # half-resolution reflections read as smeared.
        _set(options, "resolution_scale", "1")


def configure_colour_management(scene) -> str:
    """Pin the view transform and exposure. Returns the transform actually used.

    The transform enum is populated from the OCIO config at runtime, so an
    unavailable name raises rather than being silently ignored — hence trying the
    preferences in order instead of asserting one exists.
    """
    view = getattr(scene, "view_settings", None)
    if view is None:  # pragma: no cover
        return ""

    chosen = ""
    for candidate in VIEW_TRANSFORMS:
        try:
            view.view_transform = candidate
        except (TypeError, AttributeError):
            continue
        chosen = candidate
        break

    # A "look" adds contrast on top of the transform. None is applied: the
    # measured transform is already the accurate one, and a contrast look would
    # push saturation back out of the materials.
    try:
        view.look = "None"
    except (TypeError, AttributeError):
        pass

    try:
        view.exposure = EXPOSURE_STOPS
    except (TypeError, AttributeError):
        pass

    return chosen


def _set(target, attribute: str, value) -> None:
    """Assign an optional Blender property, tolerating version differences.

    Blender renames and removes render properties between versions. A preview must
    degrade in quality rather than fail outright when a setting is not present.
    """
    if not hasattr(target, attribute):
        return
    try:
        setattr(target, attribute, value)
    except (TypeError, AttributeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    import bpy

    blend_path = os.environ["PREVIEW_BLEND"]
    output_path = os.environ["PREVIEW_OUTPUT"]
    width = int(os.environ["PREVIEW_WIDTH"])
    height = int(os.environ["PREVIEW_HEIGHT"])

    if not os.path.exists(blend_path):
        raise RuntimeError("the project file to preview does not exist")

    # ---- open the SAVED project (read-only intent) ---------------------
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    scene = bpy.context.scene

    # ---- temporary camera, added in memory only ------------------------
    camera_data = bpy.data.cameras.new(PREVIEW_CAMERA_NAME)
    camera_data.lens = CAMERA_LENS_MM
    camera = bpy.data.objects.new(PREVIEW_CAMERA_NAME, camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera

    # ---- temporary lighting, only where the scene has none -------------
    lighting = configure_lighting(scene)

    # ---- render settings ------------------------------------------------
    view_transform = configure_render(scene, width, height, output_path)

    # The framing depends on object world matrices, so make sure they are current
    # before measuring the bounding box.
    bpy.context.view_layer.update()
    framing = place_camera(scene, camera, width, height)
    bpy.context.view_layer.update()

    visible = sorted(
        obj.name for obj in scene.objects if obj.name not in PREVIEW_OBJECT_NAMES
    )
    emit(
        "scene",
        {
            "objects": visible,
            "camera": PREVIEW_CAMERA_NAME,
            "engine": ENGINE,
            "width": width,
            "height": height,
            "framing": framing,
            "lighting": lighting,
            "view_transform": view_transform,
        },
    )

    # ---- render ---------------------------------------------------------
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    bpy.ops.render.render(write_still=True)

    # Blender may append a frame number depending on the path; verify the exact
    # file we promised actually exists rather than assuming it does.
    if not os.path.exists(output_path):
        raise RuntimeError(f"render did not produce {output_path}")
    size = os.path.getsize(output_path)
    if size <= 0:
        raise RuntimeError("render produced an empty file")

    emit("rendered", {"output": output_path, "size_bytes": size, "engine": ENGINE})

    # NOTE: no save of any kind. The .blend is left exactly as it was found.
    emit("done", {"ok": True})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        emit("error", {"traceback": traceback.format_exc()})
        sys.exit(1)
