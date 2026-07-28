"""
ArchX3D — Lighting reconstruction
=================================
Turns the scene graph's :class:`~vision.schema.LightingEnvironment` and its
recovered luminaires into a physically plausible Blender lighting rig.

Three contributions, built in order
-----------------------------------
1. **Sun** — a directional light aimed through the room's actual glazing,
   using the plan heading and elevation the vision layer recorded. Skipped
   entirely when ``daylight_direction`` is ``-1``: the pipeline is saying it
   does not know where the windows face, and inventing a sun in an arbitrary
   direction would put hard shadows across the room in a direction nothing
   supports.
2. **Sky** — a world background whose colour and strength come from the
   environment's ambient level and colour temperature. This is what fills the
   shadows; without it interiors render with black corners regardless of how
   many lamps are placed.
3. **Fixtures** — the observed luminaires, each with its catalog photometry,
   scaled by the style's gain.

Why not just place lamps
------------------------
Interior lighting is dominated by what bounces, not by what emits. A room lit
only by the lamps that happen to be visible in a photograph renders far darker
and harder than the photograph, because the photograph also contains daylight
through a window, light from the next room, and inter-reflection the fixture
list never mentions. The environment terms are what stand in for all of that.

Units
-----
Blender's point and area lights are in watts, which is what the catalog's
photometry already uses, so fixture power passes through directly. Sun
strength is in W/m² — a different quantity entirely — and is derived from the
environment rather than from any fixture.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import bpy

from vision import catalog

from . import colour, styles

#: Sun irradiance at full daylight, W/m². Blender's sun strength is not a
#: photometric watt; ~4 reads as bright daylight in EEVEE and Cycles alike.
FULL_DAYLIGHT = 4.2

#: Ambient world strength at each time of day, before the environment's own
#: ambient level scales it.
SKY_STRENGTH = {
    "day": 0.85,
    "overcast": 1.05,   # the sky *is* the light source when overcast
    "evening": 0.35,
    "night": 0.12,
}

#: Sky colour temperature. Overcast sky is markedly cooler than direct sun;
#: night is dominated by whatever spills from interior fixtures.
SKY_CCT = {
    "day": 6800.0,
    "overcast": 7600.0,
    "evening": 4200.0,
    "night": 5200.0,
}

#: Sun colour temperature. Low sun is heavily reddened by atmosphere.
SUN_CCT = {
    "day": 5600.0,
    "overcast": 6400.0,
    "evening": 3100.0,
    "night": 4000.0,
}

#: Angular diameter of the sun disc in degrees, before softening. The real sun
#: is 0.53°; larger values are how a soft-shadow overcast look is produced.
SUN_ANGLE_SHARP = 0.53
SUN_ANGLE_DIFFUSE = 11.0


@dataclass
class LightingReport:
    """What was actually built, for the run log."""

    fixtures: int = 0
    sun: bool = False
    sky_strength: float = 0.0
    sun_strength: float = 0.0
    time_of_day: str = "day"
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.fixtures} fixture(s)"]
        if self.sun:
            parts.append(f"sun {self.sun_strength:.2f} W/m2")
        else:
            parts.append("no sun (direction unknown)")
        parts.append(f"sky {self.sky_strength:.2f}")
        return f"{self.time_of_day}: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build(graph, style: str = "unknown", config: Optional[Dict] = None) -> LightingReport:
    """Build the full rig from the graph's lighting environments.

    Returns a report; the caller decides whether to fall back to a generic rig
    when nothing usable was found.
    """
    config = config or {}
    environment = _dominant_environment(graph)
    report = LightingReport(time_of_day=environment.time_of_day if environment else "day")

    _, gain, softness_bias = styles.lighting_bias(style)

    if environment is not None:
        report.sky_strength = _build_world(environment, softness_bias)
        sun_strength = _build_sun(environment, graph, softness_bias)
        report.sun = sun_strength > 0.0
        report.sun_strength = sun_strength
        if not report.sun:
            report.notes.append(
                "no daylight direction recorded; sky-only ambient"
            )
    else:
        report.sky_strength = _build_default_world()
        report.notes.append("no lighting environment in the graph; neutral sky")

    report.fixtures = _build_fixtures(graph, style, gain, environment)
    return report


def _dominant_environment(graph):
    """The lighting environment of the largest room that has one.

    One environment for the scene rather than one per room: sun direction and
    time of day are properties of the *building*, and a walkthrough that
    changed the time of day between rooms would look broken. Per-room variation
    lives in the fixtures, which are placed per room.
    """
    candidates = [
        room for room in getattr(graph, "rooms", [])
        if getattr(room, "lighting", None) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda room: room.area).lighting


# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------


def _build_world(environment, softness_bias: float) -> float:
    """Sky dome: fills shadow, sets overall exposure and colour cast."""
    world = bpy.data.worlds.new("ArchX3D_Sky")
    bpy.context.scene.world = world
    world.use_nodes = True

    background = world.node_tree.nodes["Background"]
    base = SKY_STRENGTH.get(environment.time_of_day, 0.7)

    # The environment's ambient level is a relative brightness, so it scales
    # the time-of-day baseline rather than replacing it.
    strength = base * (0.45 + environment.ambient)

    # A room with lots of glazing gets more of its light from the sky.
    strength *= 1.0 + 0.5 * environment.window_contribution

    kelvin = SKY_CCT.get(environment.time_of_day, 6500.0)
    red, green, blue = colour.kelvin_to_rgb(kelvin)

    background.inputs[0].default_value = (red, green, blue, 1.0)
    background.inputs[1].default_value = round(strength, 4)
    return strength


def _build_default_world() -> float:
    """Neutral sky for a graph with no environment recorded."""
    world = bpy.data.worlds.new("ArchX3D_Sky")
    bpy.context.scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes["Background"]
    background.inputs[0].default_value = (0.62, 0.66, 0.72, 1.0)
    background.inputs[1].default_value = 0.45
    return 0.45


# ---------------------------------------------------------------------------
# Sun
# ---------------------------------------------------------------------------


def _build_sun(environment, graph, softness_bias: float) -> float:
    """Directional daylight, aimed through the recorded glazing.

    Returns the strength used, or 0.0 when no sun was built.
    """
    if environment.daylight_direction < 0:
        return 0.0
    if environment.time_of_day == "night":
        return 0.0

    strength = (
        FULL_DAYLIGHT
        * max(0.05, environment.window_contribution)
        * (0.4 + 0.6 * environment.ambient)
    )
    if environment.time_of_day == "overcast":
        strength *= 0.35   # the disc is gone; the sky carries the light
    elif environment.time_of_day == "evening":
        strength *= 0.45

    bpy.ops.object.light_add(type="SUN", location=(0.0, 0.0, 10.0))
    sun = bpy.context.object
    sun.name = "Sun_Daylight"
    data = sun.data

    data.energy = round(strength, 4)
    data.color = colour.kelvin_to_rgb(SUN_CCT.get(environment.time_of_day, 5600.0))

    # Softness maps to the sun's angular diameter, which is the physically
    # correct control: a bigger apparent disc gives a wider penumbra.
    softness = min(1.0, max(0.0, environment.shadow_softness + softness_bias))
    data.angle = math.radians(
        SUN_ANGLE_SHARP + (SUN_ANGLE_DIFFUSE - SUN_ANGLE_SHARP) * softness
    )

    sun.rotation_euler = _sun_rotation(
        environment.daylight_direction, environment.daylight_elevation
    )
    sun["archx3d_direction"] = environment.daylight_direction
    sun["archx3d_elevation"] = environment.daylight_elevation
    return strength


def _sun_rotation(direction_deg: float, elevation_deg: float):
    """Euler angles aiming a sun *from* a plan heading at a given elevation.

    ``daylight_direction`` is where the light comes *from* — the heading of the
    window in plan, measured from the room centre. A Blender sun points along
    its local -Z, so it is tilted down from vertical by the sun's zenith angle
    and then spun to face the right way round.
    """
    elevation = max(1.0, min(89.0, elevation_deg))
    zenith = math.radians(90.0 - elevation)

    # Rotating about X tips the -Z axis toward +Y; the Z rotation then swings
    # that tilt to the requested plan heading. Adding 180 makes the light
    # travel *from* the heading rather than toward it.
    return (zenith, 0.0, math.radians(direction_deg + 180.0))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_fixtures(graph, style: str, gain: float, environment) -> int:
    """Recreate the observed luminaires as Blender lights."""
    lights = graph.buildable_lights() if hasattr(graph, "buildable_lights") else []
    if not lights:
        return 0

    # Lamps carry the room after dark and are largely invisible at noon, so
    # their contribution is scaled by how much daylight the room is getting.
    daylight_damping = 1.0
    if environment is not None and environment.time_of_day == "day":
        daylight_damping = 1.0 - 0.45 * environment.window_contribution

    created = 0
    for source in lights:
        prior = catalog.get_light_prior(source.kind)

        bpy.ops.object.light_add(
            type=prior.blender_type,
            location=(source.position.x, source.position.y, source.position.z),
        )
        light_object = bpy.context.object
        light_object.name = f"Light_{source.id}"
        data = light_object.data

        data.energy = max(0.0, source.power_w) * gain * daylight_damping
        data.color = colour.kelvin_to_rgb(source.color_temperature_k)

        if prior.blender_type == "AREA":
            data.size = max(0.02, source.size)
            if source.length > 0:
                data.shape = "RECTANGLE"
                data.size_y = max(0.02, source.length)
        elif prior.blender_type == "SPOT":
            data.spot_size = math.radians(70.0)
            data.spot_blend = 0.35
            data.shadow_soft_size = max(0.01, source.size / 2.0)
            light_object.rotation_euler = (math.pi, 0.0, 0.0)  # aimed at the floor
        else:
            data.shadow_soft_size = max(0.01, source.size / 2.0)

        light_object["archx3d_id"] = source.id
        light_object["archx3d_kind"] = source.kind
        light_object["archx3d_confidence"] = round(source.confidence, 3)
        created += 1

    return created


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


def build_fallback_rig(center_x: float, center_y: float, max_dim: float,
                       wall_height: float = 3.0) -> LightingReport:
    """Generic three-point rig, for a graph with no lighting at all.

    Retained because an unfurnished architectural shell built straight from a
    DXF has no luminaires and no environment, and must still be visible.
    """
    report = LightingReport(time_of_day="day", notes=["generic three-point rig"])

    bpy.ops.object.light_add(type="SUN", location=(center_x, center_y, wall_height * 4))
    key = bpy.context.object
    key.name = "Key_Sun"
    key.data.energy = 3.0
    key.data.angle = math.radians(4.0)
    key.rotation_euler = (math.radians(50.0), 0.0, math.radians(35.0))

    bpy.ops.object.light_add(
        type="AREA", location=(center_x - max_dim, center_y - max_dim, wall_height * 2)
    )
    fill = bpy.context.object
    fill.name = "Fill_Area"
    fill.data.energy = max(60.0, max_dim * 40.0)
    fill.data.size = max(2.0, max_dim * 0.5)

    report.sky_strength = _build_default_world()
    report.sun = True
    report.sun_strength = 3.0
    report.fixtures = 2
    return report
