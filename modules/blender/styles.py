"""
ArchX3D — Style application
===========================
Turns a recognised interior style into concrete appearance decisions.

What a style is allowed to do
-----------------------------
Exactly one thing: **resolve ambiguity**. Style never overrides an observation.
If a reference photograph showed a walnut table, the table is walnut whatever
the style says. Style acts only where the pipeline genuinely does not know:

* a material recorded as the generic family (``wood``) rather than a species —
  an industrial room's generic metal becomes blackened steel, a scandinavian
  room's generic wood becomes light oak
* a surface with no observed finish at all
* the warmth and softness of lighting that was never measured
* the trim and accent colours of procedural furniture, which are invented by
  the builder rather than observed

Geometry is never touched. A style changes what a scene is *made of* and how it
is *lit*, never where anything sits or how big it is.

Every decision is reported as a :class:`StyleDecision` so the generator can log
which choices came from evidence and which from a style prior.

Stdlib only. No ``bpy``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from vision import catalog


@dataclass(frozen=True)
class StyleProfile:
    """How a style shades and lights a room.

    ``substitutions`` map a *generic family* to the species that style would
    actually use. They apply only when the observation was generic — naming a
    species means the pipeline saw one, and it wins.
    """

    name: str
    substitutions: Dict[str, str] = field(default_factory=dict)
    #: Fallback finishes when a surface was never observed at all.
    wall: str = "paint_matte"
    floor: str = "wood"
    ceiling: str = "gypsum"
    #: Colour temperature this style's interior lighting sits at, kelvin.
    interior_cct: float = 3000.0
    #: Multiplier on fixture power. Industrial and luxury interiors are lit
    #: lower and warmer; offices and scandinavian interiors brighter.
    light_gain: float = 1.0
    #: Added to shadow softness, 0–1. Diffuse styles read softer.
    softness_bias: float = 0.0
    #: Roughness offset applied to the style's substituted materials.
    roughness_bias: float = 0.0
    #: Colour used for legs, frames and trim on procedural furniture.
    trim: str = "#6B6560"
    #: How much small decor a style tolerates, 0–1. Consumed by the generator
    #: to thin out low-confidence clutter in minimal interiors.
    decor_density: float = 1.0


STYLE_PROFILES: Dict[str, StyleProfile] = {
    "industrial": StyleProfile(
        "industrial",
        substitutions={
            "metal": "blackened_steel",
            "concrete": "grey_concrete",
            "wood": "walnut",
            "stone": "slate",
            "paint_matte": "grey_concrete",
        },
        wall="exposed_brick", floor="polished_concrete", ceiling="concrete",
        interior_cct=2600.0,   # warm Edison filament
        light_gain=0.85, softness_bias=-0.10, roughness_bias=0.06,
        trim="#2F3133", decor_density=0.5,
    ),
    "mid_century": StyleProfile(
        "mid_century",
        substitutions={"wood": "teak", "fabric": "wool", "metal": "brass"},
        wall="paint_matte", floor="teak", ceiling="gypsum",
        interior_cct=2750.0, light_gain=0.95, softness_bias=0.05,
        trim="#9C6B3F", decor_density=0.8,
    ),
    "scandinavian": StyleProfile(
        "scandinavian",
        substitutions={"wood": "white_oak", "fabric": "wool", "metal": "brushed_steel"},
        wall="paint_matte", floor="white_oak", ceiling="gypsum",
        interior_cct=3600.0,   # cool, daylight-led
        light_gain=1.15, softness_bias=0.18,
        trim="#E0CBA8", decor_density=0.45,
    ),
    "minimalist": StyleProfile(
        "minimalist",
        substitutions={"wood": "white_oak", "concrete": "polished_concrete",
                       "metal": "brushed_steel"},
        wall="paint_matte", floor="polished_concrete", ceiling="gypsum",
        interior_cct=3800.0, light_gain=1.10, softness_bias=0.15,
        trim="#D8D5CF", decor_density=0.25,
    ),
    "luxury": StyleProfile(
        "luxury",
        substitutions={"marble": "white_marble", "fabric": "velvet",
                       "metal": "brass", "wood": "walnut"},
        wall="paint_satin", floor="white_marble", ceiling="gypsum",
        interior_cct=2700.0, light_gain=0.90, softness_bias=0.08,
        trim="#B08D45", decor_density=0.9,
    ),
    "classic": StyleProfile(
        "classic",
        substitutions={"marble": "white_marble", "wood": "walnut", "metal": "brass"},
        wall="paint_satin", floor="white_marble", ceiling="gypsum",
        interior_cct=2800.0, light_gain=0.95, softness_bias=0.05,
        trim="#B08D45", decor_density=1.0,
    ),
    "traditional": StyleProfile(
        "traditional",
        substitutions={"wood": "mahogany", "fabric": "wool", "metal": "brass"},
        wall="wallpaper", floor="mahogany", ceiling="gypsum",
        interior_cct=2700.0, light_gain=0.90, softness_bias=0.02,
        trim="#7A3B2E", decor_density=1.0,
    ),
    "japanese": StyleProfile(
        "japanese",
        substitutions={"wood": "ash", "fabric": "linen", "paint_matte": "limewash"},
        wall="limewash", floor="ash", ceiling="wood_panel",
        interior_cct=3000.0, light_gain=0.85, softness_bias=0.22,
        trim="#2E2A26", decor_density=0.25,
    ),
    "mediterranean": StyleProfile(
        "mediterranean",
        substitutions={"stone": "travertine", "paint_matte": "limewash",
                       "wood": "teak", "tile": "terrazzo"},
        wall="limewash", floor="travertine", ceiling="limewash",
        interior_cct=3200.0, light_gain=1.10, softness_bias=0.16,
        trim="#C1683F", decor_density=0.7,
    ),
    "bohemian": StyleProfile(
        "bohemian",
        substitutions={"wood": "teak", "fabric": "cotton", "carpet": "jute"},
        wall="paint_matte", floor="teak", ceiling="gypsum",
        interior_cct=2800.0, light_gain=0.90, softness_bias=0.10,
        trim="#8C5A3C", decor_density=1.0,
    ),
    "farmhouse": StyleProfile(
        "farmhouse",
        substitutions={"wood": "birch_ply", "fabric": "linen", "metal": "blackened_steel"},
        wall="paint_matte", floor="birch_ply", ceiling="wood_panel",
        interior_cct=2900.0, light_gain=0.95, softness_bias=0.08,
        trim="#7A6A55", decor_density=0.9,
    ),
    "art_deco": StyleProfile(
        "art_deco",
        substitutions={"metal": "brass", "fabric": "velvet", "marble": "black_marble",
                       "wood": "ebony"},
        wall="paint_gloss", floor="black_marble", ceiling="gypsum",
        interior_cct=2650.0, light_gain=0.85, softness_bias=0.0,
        trim="#B08D45", decor_density=0.95,
    ),
    "contemporary": StyleProfile(
        "contemporary",
        substitutions={"wood": "light_oak", "tile": "porcelain_tile",
                       "fabric": "linen", "metal": "brushed_steel"},
        wall="paint_satin", floor="porcelain_tile", ceiling="gypsum",
        interior_cct=3300.0, light_gain=1.05, softness_bias=0.10,
        trim="#4A5259", decor_density=0.7,
    ),
    "modern": StyleProfile(
        "modern",
        substitutions={"wood": "light_oak", "metal": "brushed_steel", "fabric": "linen"},
        wall="paint_matte", floor="light_oak", ceiling="gypsum",
        interior_cct=3400.0, light_gain=1.05, softness_bias=0.10,
        trim="#3C3F44", decor_density=0.7,
    ),
    "unknown": StyleProfile("unknown"),
}


def profile_for(style: str) -> StyleProfile:
    """The appearance profile for a style name, normalised on the way in."""
    return STYLE_PROFILES.get(catalog.normalise_style(style or ""), STYLE_PROFILES["unknown"])


# ---------------------------------------------------------------------------
# Material resolution
# ---------------------------------------------------------------------------


@dataclass
class StyleDecision:
    """One material choice, and where it came from."""

    material: str
    #: observed | style | default — the provenance of this choice.
    source: str
    reason: str = ""

    @property
    def from_evidence(self) -> bool:
        return self.source == "observed"


def resolve_material(
    observed: Optional[str],
    style: str,
    surface: str = "object",
    confidence: float = 1.0,
) -> StyleDecision:
    """Choose the material to actually build with.

    The precedence that matters:

    1. **An observed species wins outright.** Naming ``walnut`` means the
       pipeline saw walnut; no style may override that.
    2. **An observed family is refined by style**, because "wood" is a
       classification, not a choice — the style says *which* wood a room like
       this uses. Only done when the style itself was recognised confidently.
    3. **Nothing observed** falls back to the style's default for the surface,
       then to the catalog default.
    """
    profile = profile_for(style)
    material = (observed or "").strip()

    if material and material not in ("unknown", ""):
        family = catalog.material_family(material)
        is_species = material != family

        if is_species:
            return StyleDecision(material, "observed", "species observed directly")

        if profile.name != "unknown" and confidence >= 0.5:
            substitute = profile.substitutions.get(family)
            if substitute and substitute in catalog.MATERIALS:
                return StyleDecision(
                    substitute, "style",
                    f"generic '{family}' refined by {profile.name} style",
                )
        return StyleDecision(material, "observed", "family observed, no style refinement")

    if profile.name != "unknown":
        fallback = {"wall": profile.wall, "floor": profile.floor,
                    "ceiling": profile.ceiling}.get(surface)
        if fallback and fallback in catalog.MATERIALS:
            return StyleDecision(fallback, "style", f"{profile.name} default for {surface}")

    default = {"wall": "paint_matte", "floor": "wood", "ceiling": "gypsum"}.get(
        surface, "unknown"
    )
    return StyleDecision(default, "default", "nothing observed, no style")


def trim_colour(style: str, fallback: str = "#6B6560") -> str:
    """Colour for legs, frames and trim on procedural furniture.

    These are invented by the geometry builder rather than observed, which
    makes them exactly the sort of gap a style may legitimately fill.
    """
    profile = profile_for(style)
    return profile.trim if profile.name != "unknown" else fallback


def decor_density(style: str) -> float:
    """How much low-confidence clutter a style tolerates, 0–1.

    A minimalist reconstruction full of uncertain vases reads wrong even when
    every vase was genuinely detected. This does not delete anything the
    pipeline is *confident* about — it only governs marginal detections.
    """
    return profile_for(style).decor_density


def lighting_bias(style: str) -> Tuple[float, float, float]:
    """``(cct_kelvin, gain, softness_bias)`` for a style's interior lighting."""
    profile = profile_for(style)
    return profile.interior_cct, profile.light_gain, profile.softness_bias


def roughness_bias(style: str) -> float:
    return profile_for(style).roughness_bias


def describe(style: str) -> str:
    """One-line summary for the build log."""
    profile = profile_for(style)
    if profile.name == "unknown":
        return "no recognised style; using observed materials as-is"
    # ASCII only: this goes to the build log, which on Windows is a cp1252
    # console that raises on anything outside it.
    substitutions = ", ".join(
        f"{family}->{species}" for family, species in sorted(profile.substitutions.items())
    )
    return (
        f"{profile.name}: {substitutions or 'no substitutions'}; "
        f"{profile.interior_cct:.0f}K interior, gain {profile.light_gain:.2f}"
    )
