"""Procedural synthesis of the catalogue's texture maps.

Produces three images per material:

    base_color  RGB   what colour the surface is
    roughness   L     how sharp reflections are, per pixel
    normal      RGB   surface relief, in tangent space

DESIGN CONSTRAINTS, AND WHY THEY SHAPE THE CODE

*Speed.* Every operation here is a whole-image PIL call implemented in C —
``effect_noise``, ``offset``, ``subtract``, ``resize``, ``point``, ``composite``.
There is not a single per-pixel Python loop, because a 512x512 map has 262144
pixels and seventeen materials need fifty-one maps; done in Python that is minutes
of work, and done in C it is under a second.

*Determinism.* Each material seeds its own ``random.Random`` from its name, so a
regenerated cache is identical to the one it replaced. Textures feed a preview
whose stability is asserted on pixels, so a texture that changed on every startup
would make that assertion meaningless.

*Seamlessness.* A texture is tiled across a wall, so a visible seam is a visible
defect. Two mechanisms handle it: structured patterns (bricks, planks, tiles) are
drawn on a grid that divides the image exactly, so they tile by construction;
organic noise is made seamless by the wrap-and-blend trick in
:func:`_make_seamless`. ``ImageChops.offset`` wraps, which is what makes both that
trick and the normal-map derivation seam-free.
"""

from __future__ import annotations

import math
import random
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from .catalogue import MaterialSpec

#: Texture resolution. 512 is the point where architectural surfaces stop looking
#: blurry at walkthrough distance without making the cache large or generation slow.
TEXTURE_SIZE = 512


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def _noise(width: int, height: int, sigma: float, seed: int) -> Image.Image:
    """Gaussian noise. ``effect_noise`` is C-speed but ignores any seed, so the
    Python RNG is used to pick a deterministic offset into a larger field."""
    # effect_noise has no seed parameter, so determinism is achieved by generating
    # a larger field and cropping a seeded position out of it.
    pad = 24
    field = Image.effect_noise((width + pad, height + pad), sigma)
    rng = random.Random(seed)
    x = rng.randrange(0, pad)
    y = rng.randrange(0, pad)
    return field.crop((x, y, x + width, y + height))


def _edge_mask(size: int) -> Image.Image:
    """255 near the image border, 0 at the centre, built from gradients.

    ``abs(gradient - 128) * 2`` is the distance-from-centre ramp for one axis. The
    two axes are combined with a SCREEN blend rather than a per-pixel maximum:
    ``lighter`` switches abruptly from one ramp to the other along the square's
    diagonals, and that crease in the mask showed up as a visible diagonal line
    across any smooth texture (the fabric weave made it obvious). Screen is
    ``255 - (255-a)(255-b)/255`` — continuous everywhere, same values at the centre
    and the edges.
    """
    flat = Image.new("L", (size, size), 128)

    vertical = Image.linear_gradient("L").resize((size, size), Image.BILINEAR)
    horizontal = vertical.transpose(Image.ROTATE_90)

    ramp_y = ImageChops.difference(vertical, flat)
    ramp_x = ImageChops.difference(horizontal, flat)
    ramp_y = ImageChops.add(ramp_y, ramp_y)
    ramp_x = ImageChops.add(ramp_x, ramp_x)

    return ImageChops.screen(ramp_x, ramp_y)


def _make_seamless(image: Image.Image) -> Image.Image:
    """Remove the seam from a non-tiling image.

    The image is offset by half its size — ``ImageChops.offset`` wraps, so the
    copy's smooth interior now sits where the original's seams were, and the
    copy's own seams sit in the middle. Compositing the copy in near the border
    and the original in the middle therefore takes continuous pixels from both and
    never uses either one's discontinuity.
    """
    size = image.size[0]
    shifted = ImageChops.offset(image, size // 2, size // 2)
    return Image.composite(shifted, image, _edge_mask(size))


def _stretched_noise(
    size: int, detail_across: int, sigma: float, seed: int, blur: float = 0.0
) -> Image.Image:
    """Noise that varies quickly along Y and slowly along X.

    Generating a narrow image and scaling it up is what produces directional
    streaks — wood grain, brushed metal — rather than isotropic speckle, and the
    upscale is a C resize rather than any kind of filtering loop.
    """
    narrow = _noise(max(detail_across, 2), size, sigma, seed)
    wide = narrow.resize((size, size), Image.BICUBIC)
    if blur:
        wide = wide.filter(ImageFilter.GaussianBlur(blur))
    return wide


def _autocontrast(image: Image.Image, cutoff: float = 1.0) -> Image.Image:
    return ImageOps.autocontrast(image, cutoff=cutoff)


def _compress(image: Image.Image, low: int, high: int) -> Image.Image:
    """Squeeze an image's range into ``[low, high]``.

    Structured patterns need this before their joints are drawn. Autocontrast
    noise already spans 0-255, so a mortar line drawn at mid-grey lands in the
    middle of the brick faces' own variation and becomes invisible — which is
    exactly how the first version of this module produced "bricks" that were a
    flat red rectangle. Compressing the faces into an upper band leaves the
    bottom of the range free for joints to be unambiguously darker.
    """
    span = max(high - low, 1)
    return image.point(lambda pixel: low + (pixel * span) // 255)


def _blend(base: Image.Image, overlay: Image.Image, alpha: float) -> Image.Image:
    return Image.blend(base, overlay, alpha)


def _grid_lines(
    size: int,
    height_image: Image.Image,
    value_image: Image.Image,
    *,
    rows: int,
    columns: int,
    stagger: bool,
    joint_px: int,
    joint_value: int,
    joint_height: int,
    rng: random.Random,
) -> None:
    """Draw mortar/grout joints for a brick, plank or tile grid, in place.

    Rows and columns divide the image exactly, so the pattern tiles. Joints are
    drawn with wrapping (a line at x is also drawn at x - size) so a joint on the
    border is continuous with the next tile.
    """
    value_draw = ImageDraw.Draw(value_image)
    height_draw = ImageDraw.Draw(height_image)
    row_height = size / rows

    for row in range(rows):
        y = row * row_height
        for draw, colour in ((value_draw, joint_value), (height_draw, joint_height)):
            draw.rectangle([0, y - joint_px / 2, size, y + joint_px / 2], fill=colour)
            # The same joint, wrapped, so the top and bottom edges agree.
            if row == 0:
                draw.rectangle(
                    [0, size - joint_px / 2, size, size + joint_px / 2], fill=colour
                )

        if columns <= 0:
            continue

        column_width = size / columns
        offset = (column_width / 2) if (stagger and row % 2) else 0.0
        for column in range(columns + 1):
            x = column * column_width + offset
            if stagger:
                # A touch of irregularity so a wall does not look machine-printed.
                x += rng.uniform(-column_width * 0.02, column_width * 0.02)
            for draw, colour in (
                (value_draw, joint_value),
                (height_draw, joint_height),
            ):
                draw.rectangle(
                    [x - joint_px / 2, y, x + joint_px / 2, y + row_height], fill=colour
                )
                if x > size - joint_px:
                    draw.rectangle(
                        [
                            x - size - joint_px / 2,
                            y,
                            x - size + joint_px / 2,
                            y + row_height,
                        ],
                        fill=colour,
                    )


def _tint_cells(
    size: int,
    value_image: Image.Image,
    *,
    rows: int,
    columns: int,
    stagger: bool,
    spread: int,
    rng: random.Random,
) -> None:
    """Give each brick/plank/tile its own slight tone, in place.

    Uniform cells are the single clearest giveaway of a synthetic texture, so each
    one is nudged independently.
    """
    overlay = Image.new("L", (size, size), 128)
    draw = ImageDraw.Draw(overlay)
    row_height = size / rows
    column_width = size / max(columns, 1)

    for row in range(rows):
        y = row * row_height
        offset = (column_width / 2) if (stagger and row % 2) else 0.0
        for column in range(-1, columns + 1):
            x = column * column_width + offset
            tone = 128 + rng.randint(-spread, spread)
            draw.rectangle([x, y, x + column_width, y + row_height], fill=tone)

    # value + (overlay - 128): an overlay of a flat 128 is therefore a no-op, so
    # cells gain their own tone without the texture's overall brightness drifting.
    value_image.paste(ImageChops.add(value_image, overlay, scale=1.0, offset=-128))


# ---------------------------------------------------------------------------
# patterns
#
# Each returns (value, height): a tonal map used to pick colour from the palette,
# and a relief map used for the normal.
# ---------------------------------------------------------------------------


def _planks(spec: MaterialSpec, size: int, rng: random.Random):
    # 12 boards across a 2.4 m tile is a 0.20 m board — a real floorboard width.
    # Five boards (the first attempt) came out half a metre wide, which reads as
    # decking at best.
    rows, columns = 12, 2
    grain = _stretched_noise(size, 10, 42.0, rng.randrange(1 << 30), blur=0.6)
    grain = _autocontrast(_make_seamless(grain), cutoff=2.0)
    fine = _make_seamless(_stretched_noise(size, 90, 26.0, rng.randrange(1 << 30)))
    value = _compress(_blend(grain, fine, 0.35), 110, 255)

    height = value.copy()
    _tint_cells(size, value, rows=rows, columns=columns, stagger=True, spread=14, rng=rng)
    _grid_lines(
        size,
        height,
        value,
        rows=rows,
        columns=columns,
        stagger=True,
        joint_px=max(size // 256, 2),
        joint_value=40,
        joint_height=20,
        rng=rng,
    )
    return value, height


def _bricks(spec: MaterialSpec, size: int, rng: random.Random):
    # 16 courses and 5 bricks across a 1.2 m tile gives a 0.24 x 0.075 m brick,
    # which is within a few millimetres of a real one including its mortar joint.
    rows, columns = 16, 5
    noise = _make_seamless(_noise(size, size, 34.0, rng.randrange(1 << 30)))
    noise = noise.filter(ImageFilter.GaussianBlur(1.1))
    value = _compress(_autocontrast(noise, cutoff=2.0), 120, 255)

    height = Image.new("L", (size, size), 220)
    _tint_cells(size, value, rows=rows, columns=columns, stagger=True, spread=20, rng=rng)
    _grid_lines(
        size,
        height,
        value,
        rows=rows,
        columns=columns,
        stagger=True,
        joint_px=max(size // 170, 3),
        joint_value=58,
        joint_height=70,
        rng=rng,
    )
    # Mortar is recessed behind the brick face, so only the relief is softened.
    height = height.filter(ImageFilter.GaussianBlur(0.8))
    return value, height


def _tiles(spec: MaterialSpec, size: int, rng: random.Random):
    rows = columns = 4
    noise = _make_seamless(_noise(size, size, 12.0, rng.randrange(1 << 30)))
    noise = noise.filter(ImageFilter.GaussianBlur(2.0))
    value = _compress(_autocontrast(noise, cutoff=3.0), 150, 255)

    height = Image.new("L", (size, size), 230)
    _tint_cells(size, value, rows=rows, columns=columns, stagger=False, spread=9, rng=rng)
    _grid_lines(
        size,
        height,
        value,
        rows=rows,
        columns=columns,
        stagger=False,
        joint_px=max(size // 150, 3),
        joint_value=92,
        joint_height=95,
        rng=rng,
    )
    height = height.filter(ImageFilter.GaussianBlur(0.7))
    return value, height


def _shingles(spec: MaterialSpec, size: int, rng: random.Random):
    """Overlapping courses, drawn top-down so each one laps the course above.

    The tiles are FILLED, not outlined. An outline one pixel wide (the first
    attempt) disappeared the moment the texture was seen at any real distance, so
    a roof looked like a flat orange sheet.
    """
    rows, columns = 7, 6
    noise = _make_seamless(_noise(size, size, 26.0, rng.randrange(1 << 30)))
    noise = noise.filter(ImageFilter.GaussianBlur(1.0))
    base = _compress(_autocontrast(noise, cutoff=2.0), 120, 255)

    value = Image.new("L", (size, size), 45)
    height = Image.new("L", (size, size), 40)
    value_draw = ImageDraw.Draw(value)
    height_draw = ImageDraw.Draw(height)

    row_height = size / rows
    column_width = size / columns
    overlap = row_height * 0.5

    for row in range(rows + 1):
        y = row * row_height
        offset = (column_width / 2) if row % 2 else 0.0
        for column in range(-1, columns + 1):
            x = column * column_width + offset
            # A rounded lower edge reads as a curved tile without real geometry.
            box = [
                x + column_width * 0.03,
                y - overlap,
                x + column_width * 0.97,
                y + row_height * 0.94,
            ]
            radius = max(int(column_width * 0.3), 2)
            tone = 150 + rng.randint(-25, 45)
            value_draw.rounded_rectangle(box, radius=radius, fill=tone)
            # Each course stands slightly proud of the one it laps.
            height_draw.rounded_rectangle(
                box, radius=radius, fill=170 + rng.randint(-15, 25)
            )

    # Let the noise modulate the drawn tiles so they are not flat fills.
    value = ImageChops.add(value, _compress(base, 0, 60), scale=1.0, offset=-30)
    height = height.filter(ImageFilter.GaussianBlur(1.2))
    return value, height


def _plaster(spec: MaterialSpec, size: int, rng: random.Random):
    coarse = _make_seamless(_noise(size, size, 40.0, rng.randrange(1 << 30)))
    coarse = coarse.filter(ImageFilter.GaussianBlur(3.5))
    fine = _make_seamless(_noise(size, size, 22.0, rng.randrange(1 << 30)))
    fine = fine.filter(ImageFilter.GaussianBlur(0.6))
    value = _autocontrast(_blend(coarse, fine, 0.45), cutoff=2.0)
    return value, value.copy()


def _concrete(spec: MaterialSpec, size: int, rng: random.Random):
    value, height = _plaster(spec, size, rng)
    # Board marks: slow vertical banding, which is what distinguishes cast
    # concrete from plaster.
    boards = _stretched_noise(size, 6, 30.0, rng.randrange(1 << 30), blur=1.5)
    boards = _make_seamless(boards).transpose(Image.ROTATE_90)
    value = _blend(value, boards, 0.3)
    height = _blend(height, boards, 0.25)
    return _autocontrast(value, cutoff=2.0), height


def _gravel(spec: MaterialSpec, size: int, rng: random.Random):
    """Many small stones, drawn as ellipses then relit through the normal map."""
    value = Image.new("L", (size, size), 120)
    height = Image.new("L", (size, size), 90)
    value_draw = ImageDraw.Draw(value)
    height_draw = ImageDraw.Draw(height)

    count = 900
    for _ in range(count):
        x = rng.uniform(0, size)
        y = rng.uniform(0, size)
        radius = rng.uniform(size * 0.008, size * 0.022)
        tone = rng.randint(80, 210)
        # Draw every stone up to four times so ones crossing a border wrap.
        for dx in (0, -size if x + radius > size else 0):
            for dy in (0, -size if y + radius > size else 0):
                box = [x + dx - radius, y + dy - radius, x + dx + radius, y + dy + radius]
                value_draw.ellipse(box, fill=tone)
                height_draw.ellipse(box, fill=min(255, tone + 40))

    value = value.filter(ImageFilter.GaussianBlur(0.5))
    height = height.filter(ImageFilter.GaussianBlur(0.8))
    return _autocontrast(value, cutoff=1.0), height


def _grass(spec: MaterialSpec, size: int, rng: random.Random):
    """Mottled base plus short blades, so it is not a flat green field."""
    base = _make_seamless(_noise(size, size, 34.0, rng.randrange(1 << 30)))
    base = base.filter(ImageFilter.GaussianBlur(2.2))
    value = _autocontrast(base, cutoff=2.0)
    draw = ImageDraw.Draw(value)

    for _ in range(2600):
        x = rng.uniform(0, size)
        y = rng.uniform(0, size)
        length = rng.uniform(size * 0.008, size * 0.02)
        angle = rng.uniform(-math.pi, math.pi)
        x2 = x + math.cos(angle) * length
        y2 = y + math.sin(angle) * length
        draw.line([x, y, x2, y2], fill=rng.randint(60, 220), width=1)

    height = value.filter(ImageFilter.GaussianBlur(0.7))
    return value, height


def _fabric(spec: MaterialSpec, size: int, rng: random.Random):
    """A woven grid: fine threads both ways, which is what makes cloth read as cloth."""
    value = Image.new("L", (size, size), 120)
    draw = ImageDraw.Draw(value)
    # A coarser pitch than thread-level: at 0.6 m per tile this is roughly a 9 mm
    # weave, which is what actually reads as woven cloth instead of as grey noise.
    pitch = max(size // 64, 4)
    thread = max(pitch // 2, 2)
    count = max(size // pitch, 1)

    # Interleaving the threads is the whole point, and getting it wrong is visible
    # from across the room. Drawing each vertical thread and then immediately its
    # matching horizontal one means the later-drawn thread wins at every crossing,
    # so "which thread is on top" depends on which INDEX is larger — and the cloth
    # comes out split corner to corner by a hard diagonal. It looked like a folded
    # sheet rather than a weave.
    #
    # Instead: lay all the warp, then all the weft, then bring the warp back to the
    # front on alternating crossings. That is a checkerboard over/under, which is
    # what weaving actually is.
    for index in range(count):
        position = index * pitch
        draw.rectangle([position, 0, position + thread, size], fill=175)
    for index in range(count):
        position = index * pitch
        draw.rectangle([0, position, size, position + thread], fill=80)
    for row in range(count):
        for column in range(count):
            if (row + column) % 2:
                continue
            x = column * pitch
            y = row * pitch
            draw.rectangle([x, y, x + thread, y + thread], fill=175)

    weave = _make_seamless(_noise(size, size, 18.0, rng.randrange(1 << 30)))
    value = _blend(value, weave, 0.25)
    value = value.filter(ImageFilter.GaussianBlur(0.4))
    value = _autocontrast(value, cutoff=2.0)
    return value, value.copy()


def _metal(spec: MaterialSpec, size: int, rng: random.Random):
    brushed = _make_seamless(_stretched_noise(size, 200, 16.0, rng.randrange(1 << 30)))
    brushed = brushed.filter(ImageFilter.GaussianBlur(0.3))
    value = _autocontrast(brushed, cutoff=4.0)
    # Relief is almost nothing: brushing changes how it reflects, not its shape.
    return value, value.filter(ImageFilter.GaussianBlur(1.4))


def _marble(spec: MaterialSpec, size: int, rng: random.Random):
    """Veins, approximated by thresholding heavily blurred directional noise."""
    body = _make_seamless(_noise(size, size, 30.0, rng.randrange(1 << 30)))
    body = body.filter(ImageFilter.GaussianBlur(6.0))
    body = _autocontrast(body, cutoff=1.0)

    veins = _make_seamless(_stretched_noise(size, 14, 40.0, rng.randrange(1 << 30)))
    veins = veins.rotate(28.0, resample=Image.BICUBIC, expand=False)
    veins = veins.filter(ImageFilter.GaussianBlur(1.2))
    # A narrow band around the midpoint becomes the vein: |v - 128| inverted and
    # steepened, which yields thin dark lines rather than broad smudges.
    veins = ImageChops.difference(veins, Image.new("L", (size, size), 128))
    veins = veins.point(lambda v: max(0, 255 - v * 9))

    value = ImageChops.subtract(body, veins, scale=1.6, offset=40)
    value = _autocontrast(value, cutoff=1.0)
    return value, veins.filter(ImageFilter.GaussianBlur(1.0))


PATTERNS = {
    "planks": _planks,
    "bricks": _bricks,
    "tiles": _tiles,
    "shingles": _shingles,
    "plaster": _plaster,
    "concrete": _concrete,
    "gravel": _gravel,
    "grass": _grass,
    "fabric": _fabric,
    "metal": _metal,
    "marble": _marble,
}


# ---------------------------------------------------------------------------
# map assembly
# ---------------------------------------------------------------------------


def normal_from_height(height: Image.Image, strength: float) -> Image.Image:
    """Tangent-space normal map from a height field.

    The gradient is a central difference taken with ``ImageChops.offset``, which
    wraps — so a normal map built from a seamless height field is itself seamless.
    ``subtract`` with ``offset=128`` keeps the sign, giving the +/- encoding a
    normal map needs rather than a magnitude.
    """
    size = height.size[0]
    scale = max(1.0 / max(strength, 1e-3), 0.05)

    left = ImageChops.offset(height, 1, 0)
    right = ImageChops.offset(height, -1, 0)
    up = ImageChops.offset(height, 0, 1)
    down = ImageChops.offset(height, 0, -1)

    # X points from dark to light across the surface; Y is inverted because image
    # rows increase downward while the tangent frame's +Y points up.
    red = ImageChops.subtract(left, right, scale=scale, offset=128)
    green = ImageChops.subtract(down, up, scale=scale, offset=128)
    blue = Image.new("L", (size, size), 255)

    return Image.merge("RGB", (red, green, blue))


def roughness_map(
    value: Image.Image, spec: MaterialSpec
) -> Image.Image:
    """Per-pixel roughness around the material's midpoint.

    Varying roughness is what stops a surface looking like plastic: real floors
    have worn patches that catch light differently, and a constant value cannot.
    """
    centre = int(round(max(0.0, min(1.0, spec.roughness)) * 255))
    spread = max(0.0, min(1.0, spec.roughness_variation)) * 255.0

    def curve(pixel: int) -> int:
        deviation = (pixel - 128) / 128.0
        return max(0, min(255, int(centre + deviation * spread)))

    return value.point(curve)


def synthesize(spec: MaterialSpec, size: int = TEXTURE_SIZE) -> dict[str, Image.Image]:
    """All three maps for one material.

    Seeded from the material NAME, so every material is different from its
    neighbours and identical to itself across runs.
    """
    pattern = PATTERNS.get(spec.pattern)
    if pattern is None:
        raise KeyError(f"no synthesiser for pattern {spec.pattern!r}")

    rng = random.Random(f"studio-material:{spec.name}")
    value, height = pattern(spec, size, rng)

    mid, light, dark = spec.palette[0], spec.palette[1], spec.palette[2]
    base_color = ImageOps.colorize(value, black=dark, white=light, mid=mid)

    return {
        "base_color": base_color,
        "roughness": roughness_map(value, spec),
        "normal": normal_from_height(height, spec.bump),
    }


__all__ = [
    "PATTERNS",
    "TEXTURE_SIZE",
    "normal_from_height",
    "roughness_map",
    "synthesize",
]
