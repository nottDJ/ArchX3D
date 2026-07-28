"""
ArchX3D — Style, palette and lighting environment
=================================================
The layer between "what objects are in this room" and "what does this room
look like".

Why derive rather than ask
--------------------------
The obvious approach is to add three more questions to the observation prompt.
That has two problems: it invalidates every cached response, and it asks the
model for judgements it is worse at than arithmetic on what it already told
us. A model asked "what is the dominant colour of this room?" answers from
impression; the pipeline already holds the wall finish, the floor finish and
the colour of every detected object, which is *evidence*.

So each function here takes an optional observed value and falls back to
derivation from the graph. Where the model did answer, its answer wins and is
marked ``observed``; where it did not, the derived value is marked
``inferred`` and carries a lower confidence. Nothing is invented: a room with
no imagery gets no palette at all rather than a plausible-looking guess.

Stdlib only — no numpy, so Blender can import this.
"""

from __future__ import annotations

import colorsys
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import catalog
from .schema import (
    ColourPalette,
    LightingEnvironment,
    LightSource,
    Opening,
    Room,
    SceneObject,
)

#: Below this, a colour is treated as neutral and cannot serve as an accent.
ACCENT_MIN_SATURATION = 0.22

#: Fixture power per square metre that reads as a normally lit room.
NOMINAL_W_PER_M2 = 4.0

#: Glazed area as a share of floor area that reads as a well-daylit room.
NOMINAL_GLAZING_RATIO = 0.18


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------


def to_rgb(color_hex: str) -> Tuple[int, int, int]:
    text = (color_hex or "#BFBFBF").lstrip("#")
    if len(text) != 6:
        return (191, 191, 191)
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (191, 191, 191)


def to_hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(
        f"{min(255, max(0, int(round(channel)))):02X}" for channel in rgb[:3]
    )


def saturation(color_hex: str) -> float:
    r, g, b = (channel / 255.0 for channel in to_rgb(color_hex))
    return colorsys.rgb_to_hls(r, g, b)[2]


def luminance(color_hex: str) -> float:
    """Rec. 709 relative luminance in [0, 1]."""
    r, g, b = to_rgb(color_hex)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def mix(colours: Sequence[str], weights: Optional[Sequence[float]] = None) -> str:
    """Weighted mean colour.

    Averaged in linear-ish RGB rather than in hue, because mixing a red and a
    cyan by hue gives green, which is in neither input. A dull average is the
    honest answer for "the overall colour of this furniture".
    """
    if not colours:
        return "#BFBFBF"
    weights = list(weights or [1.0] * len(colours))
    total = sum(weights) or 1.0

    channels = [0.0, 0.0, 0.0]
    for colour, weight in zip(colours, weights):
        rgb = to_rgb(colour)
        for index in range(3):
            # Approximate sRGB → linear with a gamma of 2, cheap and adequate.
            channels[index] += (rgb[index] ** 2) * weight
    return to_hex([math.sqrt(channel / total) for channel in channels])


def distance(a: str, b: str) -> float:
    """Perceptual-ish distance between two colours, 0 (same) to 1 (opposite).

    Uses a luma-weighted RGB metric rather than plain Euclidean: the eye is far
    more sensitive to green than to blue, and an unweighted metric calls two
    obviously different greys "closer" than two indistinguishable blues.
    """
    ra, ga, ba = to_rgb(a)
    rb, gb, bb = to_rgb(b)
    mean_r = (ra + rb) / 2.0
    dr, dg, db = ra - rb, ga - gb, ba - bb
    # Low-cost approximation of CIE94 weighting.
    weight_r = 2 + mean_r / 256.0
    weight_g = 4.0
    weight_b = 2 + (255 - mean_r) / 256.0
    raw = math.sqrt(weight_r * dr * dr + weight_g * dg * dg + weight_b * db * db)
    return min(1.0, raw / 764.0)


def kelvin_to_hex(kelvin: float) -> str:
    """Approximate sRGB of a blackbody at ``kelvin``.

    A piecewise fit of the Planckian locus. Used to give the palette's
    ``lighting`` role a colour and to compare a render's warmth against a
    photograph's; the renderer itself works from the Kelvin value.
    """
    t = min(10000.0, max(1500.0, kelvin)) / 100.0
    red = 255.0 if t <= 66 else _clamp_channel(329.7 * ((t - 60) ** -0.1332))
    if t <= 66:
        green = _clamp_channel(99.47 * math.log(t) - 161.12)
    else:
        green = _clamp_channel(288.12 * ((t - 60) ** -0.0755))
    if t >= 66:
        blue = 255.0
    elif t <= 19:
        blue = 0.0
    else:
        blue = _clamp_channel(138.52 * math.log(t - 10) - 305.04)
    return to_hex((red, green, blue))


def _clamp_channel(value: float) -> float:
    return min(255.0, max(0.0, value))


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------


def resolve_style(
    raw: str,
    objects: Sequence[SceneObject] = (),
    materials: Sequence[str] = (),
) -> Tuple[str, float]:
    """Normalise a style label and say how much to trust it.

    Confidence combines two independent signals: whether the model's words
    resolved to a known style at all, and whether the room's *materials* agree
    with what that style implies. A room the model called "industrial" whose
    surfaces are all exposed brick and concrete is a much safer claim than one
    whose surfaces are marble and velvet, and asset selection should weigh them
    differently.
    """
    style = catalog.normalise_style(raw)
    if style == "unknown":
        return "unknown", 0.0

    # A resolved label is worth something on its own.
    score = 0.55

    prior = catalog.get_style(style)
    observed = {catalog.material_family(m) for m in materials if m}
    observed |= {catalog.material_family(o.material) for o in objects if o.material}
    observed.discard("unknown")

    if prior.materials and observed:
        expected = {catalog.material_family(m) for m in prior.materials}
        agreement = len(observed & expected) / len(expected)
        score += 0.45 * min(1.0, agreement * 1.5)

    return style, round(min(1.0, score), 3)


def dominant_style(rooms: Sequence[Room]) -> Tuple[str, float]:
    """The style of the scene as a whole, weighted by floor area.

    Area-weighted because a large open living space characterises a home more
    than a small bathroom does, and the fallback style is applied to rooms with
    no imagery of their own.
    """
    scores: Dict[str, float] = {}
    for room in rooms:
        if room.style and room.style != "unknown":
            weight = max(room.area, 1.0) * max(room.style_confidence, 0.2)
            scores[room.style] = scores.get(room.style, 0.0) + weight

    if not scores:
        return "unknown", 0.0

    best = max(scores, key=lambda key: scores[key])
    share = scores[best] / sum(scores.values())
    return best, round(share, 3)


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------


def derive_palette(
    room: Room,
    objects: Sequence[SceneObject],
    lights: Sequence[LightSource] = (),
    fallback_wall: Optional[str] = None,
    fallback_floor: Optional[str] = None,
) -> Optional[ColourPalette]:
    """The room's characteristic colours, by role.

    Returns ``None`` for a room with nothing observed — a palette invented from
    a style prior would be presented with the same authority as one measured
    from a photograph, which is exactly the guessing this pipeline avoids.

    Roles are assigned by what actually determines each: walls dominate the
    visual field so they set ``primary``; the floor is next by area; the accent
    is the most saturated colour among decor, because that is what an accent
    *is* — used sparingly and deliberately.
    """
    wall = (room.wall_finish.color_hex if room.wall_finish else None) or fallback_wall
    floor = (room.floor_finish.color_hex if room.floor_finish else None) or fallback_floor

    furniture = [o for o in objects if o.group == "furniture" and o.color_hex]
    decor = [o for o in objects if o.group in ("decor", "appliance") and o.color_hex]

    if not wall and not floor and not furniture and not decor:
        return None

    # Furniture is area-weighted: a sectional characterises a room more than a
    # side table, and an unweighted mean lets clutter outvote the sofa.
    furniture_colour = mix(
        [o.color_hex for o in furniture],
        [max(o.dimensions.footprint_area, 0.01) for o in furniture],
    ) if furniture else "#B5AFA5"

    decor_colour = mix([o.color_hex for o in decor]) if decor else furniture_colour

    accent = _pick_accent(decor + furniture, exclude=(wall, floor))

    lighting_colour = kelvin_to_hex(
        _weighted_mean(
            [x.color_temperature_k for x in lights],
            [max(x.power_w, 1.0) for x in lights],
        ) if lights else 3400.0
    )

    observed = sum(
        1 for value in (wall, floor, furniture or None, decor or None) if value
    )
    return ColourPalette(
        primary=wall or floor or furniture_colour,
        secondary=floor or wall or furniture_colour,
        accent=accent,
        lighting=lighting_colour,
        furniture=furniture_colour,
        decor=decor_colour,
        source="observed",
        confidence=round(min(1.0, 0.25 * observed), 3),
    )


def palette_from_style(style: str) -> Optional[ColourPalette]:
    """A palette implied by a style, for rooms with no imagery of their own.

    Marked ``style_prior`` so no consumer mistakes it for measurement. Used to
    keep an unphotographed room from being built in default grey when the rest
    of the home has an established character.
    """
    prior = catalog.get_style(style)
    if not prior.palette:
        return None

    colours = list(prior.palette)
    while len(colours) < 3:
        colours.append(colours[-1])

    warmth = {"warm": 2700.0, "neutral": 3400.0, "cool": 4200.0}
    return ColourPalette(
        primary=colours[0],
        secondary=colours[1],
        accent=colours[2],
        lighting=kelvin_to_hex(warmth.get(prior.lighting, 3400.0)),
        furniture=mix(colours[:2]),
        decor=colours[2],
        source="style_prior",
        confidence=0.25,
    )


def _pick_accent(objects: Sequence[SceneObject], exclude: Sequence[Optional[str]]) -> str:
    """The most saturated object colour that is not already a surface colour."""
    best, best_score = None, 0.0
    for obj in objects:
        colour = obj.color_hex
        if not colour:
            continue
        if any(other and distance(colour, other) < 0.08 for other in exclude):
            continue
        score = saturation(colour)
        if score > best_score:
            best, best_score = colour, score

    if best is None or best_score < ACCENT_MIN_SATURATION:
        # A genuinely neutral room has no accent; saying so beats inventing one.
        return best or "#9AA5AE"
    return best


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    total = sum(weights) or 1.0
    return sum(v * w for v, w in zip(values, weights)) / total


# ---------------------------------------------------------------------------
# Lighting environment
# ---------------------------------------------------------------------------


def derive_lighting(
    room: Room,
    lights: Sequence[LightSource],
    openings: Sequence[Opening],
    observed: Optional[Dict[str, Any]] = None,
) -> LightingEnvironment:
    """Room-scale lighting conditions.

    Fixtures say what is emitting. This says what the room *looks* like, which
    is what a render has to match: two rooms with identical lamps photographed
    at noon and at midnight need different environments, and nothing in the
    fixture list expresses that.

    Everything here is computed from the room's own geometry and contents.
    ``observed`` lets a model-supplied value win where one exists — the model
    can see whether it is night outside, and no amount of arithmetic on the
    fixture list can.
    """
    observed = observed or {}

    windows = [o for o in openings if o.kind == "window"]
    glazed = sum(o.width * o.height for o in windows)
    floor_area = max(room.area, 1.0)
    glazing_ratio = glazed / floor_area

    # Window contribution saturates: past a point more glass stops making the
    # room proportionally brighter, it just moves the light around.
    window_contribution = min(0.85, glazing_ratio / NOMINAL_GLAZING_RATIO * 0.45)

    fixture_power = sum(x.power_w for x in lights if not x.uncertain)
    fixture_level = min(1.0, (fixture_power / floor_area) / NOMINAL_W_PER_M2)

    time_of_day = str(observed.get("time_of_day") or "").strip().lower()
    if time_of_day not in ("day", "evening", "night", "overcast"):
        time_of_day = _infer_time_of_day(window_contribution, fixture_level)

    if time_of_day in ("night", "evening"):
        # After dark the windows contribute nothing; only the fixtures do.
        window_contribution = 0.0 if time_of_day == "night" else window_contribution * 0.25

    ambient = _clamp01(
        0.15 + 0.55 * window_contribution + 0.45 * fixture_level
    )
    if time_of_day == "night":
        ambient = min(ambient, 0.45)
    elif time_of_day == "overcast":
        ambient = min(ambient, 0.7)

    colour_temperature = (
        _weighted_mean(
            [x.color_temperature_k for x in lights],
            [max(x.power_w, 1.0) for x in lights],
        )
        if lights
        else 3400.0
    )
    if time_of_day == "day" and window_contribution > 0.3:
        # Daylight is much cooler than tungsten; blend it in by its share.
        colour_temperature = (
            colour_temperature * (1 - window_contribution) + 6200.0 * window_contribution
        )

    direction = _daylight_direction(room, windows)

    # Big windows and overcast skies give soft shadows; a single point lamp in a
    # dark room gives hard ones.
    softness = _clamp01(
        0.3 + 0.5 * window_contribution + (0.2 if time_of_day == "overcast" else 0.0)
    )

    confidence = 0.3
    if windows:
        confidence += 0.25
    if lights:
        confidence += 0.25
    if observed:
        confidence += 0.2

    return LightingEnvironment(
        ambient=round(ambient, 3),
        daylight_direction=round(direction, 2),
        daylight_elevation=_elevation_for(time_of_day),
        window_contribution=round(window_contribution, 3),
        color_temperature_k=round(colour_temperature, 1),
        shadow_softness=round(softness, 3),
        time_of_day=time_of_day,
        source="observed" if observed else "inferred",
        confidence=round(min(1.0, confidence), 3),
    )


def _infer_time_of_day(window_contribution: float, fixture_level: float) -> str:
    """Guess day or night from how the room is being lit.

    A room whose illumination is overwhelmingly artificial despite having
    glazing reads as evening; one with substantial daylight reads as day. This
    is a weak inference and is marked as such — the model's own answer, when it
    gives one, always wins.
    """
    if window_contribution >= 0.35:
        return "day"
    if window_contribution <= 0.05 and fixture_level > 0.4:
        return "night"
    if fixture_level > window_contribution * 1.5:
        return "evening"
    return "day"


def _elevation_for(time_of_day: str) -> float:
    return {"day": 45.0, "overcast": 30.0, "evening": 8.0, "night": -10.0}.get(
        time_of_day, 35.0
    )


def _daylight_direction(room: Room, windows: Sequence[Opening]) -> float:
    """Plan heading the daylight arrives from.

    Pointed through the room's actual glazing rather than picked: the sun is
    placed on the far side of the largest window, looking in. With no windows
    recorded there is no defensible answer, so -1 signals "unknown" and the
    renderer falls back to uniform ambient rather than inventing a sun.
    """
    if not windows:
        return -1.0

    largest = max(windows, key=lambda o: o.width * o.height)
    centre = (
        (room.bounds_min[0] + room.bounds_max[0]) / 2.0,
        (room.bounds_min[1] + room.bounds_max[1]) / 2.0,
    )
    dx = largest.position.x - centre[0]
    dy = largest.position.y - centre[1]
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return -1.0
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))
