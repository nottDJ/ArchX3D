"""
ArchX3D — Colour maths for the Blender layer
============================================
Conversions and perceptual operations shared by the material, palette and
lighting modules.

Everything here works in one of three spaces, and mixing them up is the usual
source of washed-out or oversaturated renders, so each function says which it
expects:

* **sRGB hex** — what the scene graph stores, and what a person reads.
* **Linear RGB** — what Blender's shader inputs expect. Assigning an sRGB
  value straight into a Principled BSDF is the single most common way to get a
  render that is subtly too bright.
* **HLS** — used for the tinting rules, because "shift the hue a little but
  keep the lightness" is not expressible in RGB.

Stdlib only. No ``bpy``.
"""

from __future__ import annotations

import colorsys
import math
from typing import Iterable, Sequence, Tuple

RGB = Tuple[float, float, float]
RGBA = Tuple[float, float, float, float]

NEUTRAL = "#BFBFBF"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def to_rgb255(color_hex: str) -> Tuple[int, int, int]:
    """Parse ``#RRGGBB`` (or ``#RGB``) into 0–255 channels.

    Never raises: a malformed colour degrades to neutral grey. A bad hex in one
    object's record must not abort a whole scene build.
    """
    text = (color_hex or NEUTRAL).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(channel * 2 for channel in text)
    if len(text) != 6:
        text = "BFBFBF"
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (191, 191, 191)


def to_unit(color_hex: str) -> RGB:
    """Parse into 0–1 sRGB."""
    return tuple(channel / 255.0 for channel in to_rgb255(color_hex))  # type: ignore[return-value]


def to_hex(rgb: Sequence[float]) -> str:
    """Format 0–1 sRGB channels as ``#RRGGBB``."""
    return "#" + "".join(
        f"{int(round(min(1.0, max(0.0, channel)) * 255)):02X}" for channel in rgb[:3]
    )


def to_hex255(rgb: Sequence[float]) -> str:
    """Format 0–255 channels as ``#RRGGBB``."""
    return "#" + "".join(
        f"{int(round(min(255.0, max(0.0, channel)))):02X}" for channel in rgb[:3]
    )


# ---------------------------------------------------------------------------
# Colour space
# ---------------------------------------------------------------------------


def srgb_to_linear(channel: float) -> float:
    """One sRGB channel in 0–1 to linear."""
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def linear_to_srgb(channel: float) -> float:
    if channel <= 0.0031308:
        return channel * 12.92
    return 1.055 * (channel ** (1 / 2.4)) - 0.055


def hex_to_linear(color_hex: str, alpha: float = 1.0) -> RGBA:
    """The form Blender shader inputs want.

    Blender's node inputs are linear. Feeding them sRGB values is why an
    otherwise correct palette renders looking bleached.
    """
    r, g, b = to_unit(color_hex)
    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), alpha)


def linear_to_hex(rgb: Sequence[float]) -> str:
    return to_hex([linear_to_srgb(min(1.0, max(0.0, c))) for c in rgb[:3]])


# ---------------------------------------------------------------------------
# Perceptual
# ---------------------------------------------------------------------------


def luminance(color_hex: str) -> float:
    """Rec. 709 relative luminance, 0–1."""
    r, g, b = to_unit(color_hex)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def hls(color_hex: str) -> Tuple[float, float, float]:
    """Hue, lightness, saturation — each 0–1, hue wrapping at 1."""
    r, g, b = to_unit(color_hex)
    return colorsys.rgb_to_hls(r, g, b)


def from_hls(hue: float, lightness: float, sat: float) -> str:
    r, g, b = colorsys.hls_to_rgb(hue % 1.0, _clamp01(lightness), _clamp01(sat))
    return to_hex((r, g, b))


def hue_distance(a: float, b: float) -> float:
    """Shortest distance between two hues on the 0–1 wheel."""
    raw = abs((a % 1.0) - (b % 1.0))
    return min(raw, 1.0 - raw)


def shift(color_hex: str, delta: int) -> str:
    """Lighten (positive) or darken (negative) by ``delta`` per 0–255 channel.

    Used for the secondary slots of a piece of furniture — legs and frames are
    typically the same material a shade darker, and deriving them keeps a
    two-tone object from needing two observations.
    """
    channels = [max(0, min(255, channel + delta)) for channel in to_rgb255(color_hex)]
    return "#{:02X}{:02X}{:02X}".format(*channels)


def mix(a: str, b: str, factor: float) -> str:
    """Blend ``a`` toward ``b`` by ``factor`` (0–1), in linear space.

    Linear rather than sRGB because mixing two mid-tones in sRGB darkens the
    result — the classic "blend of two greys is not the grey between them"
    artefact.
    """
    factor = _clamp01(factor)
    left = [srgb_to_linear(channel) for channel in to_unit(a)]
    right = [srgb_to_linear(channel) for channel in to_unit(b)]
    blended = [l + (r - l) * factor for l, r in zip(left, right)]
    return linear_to_hex(blended)


def distance(a: str, b: str) -> float:
    """Luma-weighted RGB distance, 0 (identical) to 1 (opposite).

    Matches ``vision.appearance.distance`` so the generator and the similarity
    engine agree on what "a different colour" means.
    """
    ra, ga, ba = to_rgb255(a)
    rb, gb, bb = to_rgb255(b)
    mean_r = (ra + rb) / 2.0
    dr, dg, db = ra - rb, ga - gb, ba - bb
    weight_r = 2 + mean_r / 256.0
    weight_b = 2 + (255 - mean_r) / 256.0
    raw = math.sqrt(weight_r * dr * dr + 4.0 * dg * dg + weight_b * db * db)
    return min(1.0, raw / 764.0)


# ---------------------------------------------------------------------------
# Light
# ---------------------------------------------------------------------------


def kelvin_to_rgb(kelvin: float) -> RGB:
    """Approximate *linear* RGB for a blackbody at ``kelvin``.

    Returned linear because it is fed straight to a light datablock's colour,
    which — unlike a shader base colour — is already linear.

    Normalised so the brightest channel is 1.0: a light's intensity is its
    energy, not its colour, and letting the colour carry brightness makes warm
    lights silently dimmer than cool ones at the same wattage.
    """
    t = min(12000.0, max(1000.0, kelvin)) / 100.0

    if t <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        red = 329.698727446 * ((t - 60) ** -0.1332047592)
        green = 288.1221695283 * ((t - 60) ** -0.0755148492)

    if t >= 66:
        blue = 255.0
    elif t <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(t - 10) - 305.0447927307

    channels = [min(255.0, max(0.0, value)) / 255.0 for value in (red, green, blue)]
    peak = max(channels) or 1.0
    return tuple(srgb_to_linear(channel / peak) for channel in channels)  # type: ignore[return-value]


def kelvin_to_hex(kelvin: float) -> str:
    """sRGB hex of a blackbody, for previews and diagnostics."""
    return linear_to_hex(kelvin_to_rgb(kelvin))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def average(colours: Iterable[str]) -> str:
    """Mean colour in linear space."""
    values = list(colours)
    if not values:
        return NEUTRAL
    totals = [0.0, 0.0, 0.0]
    for colour in values:
        for index, channel in enumerate(to_unit(colour)):
            totals[index] += srgb_to_linear(channel)
    return linear_to_hex([total / len(values) for total in totals])
