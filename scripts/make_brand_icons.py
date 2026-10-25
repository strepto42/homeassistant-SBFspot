"""Generate the SBFspot brand icons: a sun with a Bluetooth rune.

Renders a gold sun whose centre is a Bluetooth-blue disc with the white
Bluetooth rune, on a transparent background, into
custom_components/sbfspot/brand/ as ``icon.png`` (256x256) and ``icon@2x.png``
(512x512). Home Assistant serves these directly from a custom integration.

Needs Pillow (the ``dev`` extra).  Run:  python scripts/make_brand_icons.py
"""

from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path

from PIL import Image, ImageDraw

_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = _ROOT / "custom_components" / "sbfspot" / "brand"

# Colours
RAY = (255, 193, 7, 255)  # amber
SUN = (255, 179, 0, 255)  # gold
RING = (255, 214, 79, 255)  # light gold ring around the blue disc
BLUE = (0, 98, 195, 255)  # Bluetooth blue
RUNE = (255, 255, 255, 255)

CANVAS = 1024  # render large, scale down for smooth edges
CENTRE = CANVAS / 2
SUN_RADIUS = 0.30 * CANVAS
RAY_INNER = 0.37 * CANVAS
RAY_OUTER = 0.47 * CANVAS
RAY_WIDTH = 0.060 * CANVAS
RAY_COUNT = 12
RING_RADIUS = 0.235 * CANVAS
DISC_RADIUS = 0.205 * CANVAS

# Bluetooth rune on a unit square (x right, y down), stem from -1 to 1.
RUNE_POINTS = (
    (-0.55, -0.50),
    (0.55, 0.50),
    (0.0, 1.0),
    (0.0, -1.0),
    (0.55, -0.50),
    (-0.55, 0.50),
)
RUNE_HEIGHT = 0.145 * CANVAS  # half the rune's height
RUNE_WIDTH = 0.034 * CANVAS


def _disc(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, fill) -> None:
    draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def _round_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
    fill,
) -> None:
    """A line with round caps (Pillow's own caps are square)."""
    draw.line([start, end], fill=fill, width=round(width))
    for x, y in (start, end):
        _disc(draw, x, y, width / 2, fill)


def render() -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for i in range(RAY_COUNT):
        angle = 2 * math.pi * i / RAY_COUNT
        dx, dy = math.cos(angle), math.sin(angle)
        _round_line(
            draw,
            (CENTRE + RAY_INNER * dx, CENTRE + RAY_INNER * dy),
            (CENTRE + RAY_OUTER * dx, CENTRE + RAY_OUTER * dy),
            RAY_WIDTH,
            RAY,
        )

    _disc(draw, CENTRE, CENTRE, SUN_RADIUS, SUN)
    _disc(draw, CENTRE, CENTRE, RING_RADIUS, RING)
    _disc(draw, CENTRE, CENTRE, DISC_RADIUS, BLUE)

    points = [
        (CENTRE + x * RUNE_HEIGHT, CENTRE + y * RUNE_HEIGHT) for x, y in RUNE_POINTS
    ]
    for start, end in pairwise(points):
        _round_line(draw, start, end, RUNE_WIDTH, RUNE)
    return img


def save(img: Image.Image, name: str, size: int) -> None:
    out = img.resize((size, size), Image.Resampling.LANCZOS)
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    out.save(BRAND_DIR / name, optimize=True)
    print(f"  {name}: {out.size[0]}x{out.size[1]}")


def main() -> None:
    icon = render()
    print("Writing brand assets:")
    save(icon, "icon.png", 256)
    save(icon, "icon@2x.png", 512)


if __name__ == "__main__":
    main()
