"""
ArchX3D — Palette application
=============================
Decides what colour a surface actually gets, given the room's palette and the
material it is made of.

The problem this solves
-----------------------
A room palette is a statement about the *scheme* — this is a warm beige room
with a terracotta accent. Applying it naively repaints everything, and you get
blue walnut and green marble. Applying it not at all wastes the information and
leaves every room looking like the catalog defaults.

So tinting is **bounded by material realism**. Every material family declares
how far its colour may legitimately move:

* Timber, stone and metal are what they are. Walnut is brown because walnut is
  brown; a palette may nudge its warmth, not its hue. These get a tiny hue
  budget and a small lightness budget.
* Paint, wallpaper and plaster are *chosen* colours. A palette may set them
  outright — that is the entire point of a paint colour.
* Fabric sits between: upholstery genuinely comes in any colour, but a linen
  sofa observed as pale grey should not become scarlet because the room has a
  red accent, so the budget is generous but not unlimited.

Every decision returns a :class:`Tint` recording what was applied and why, so
the generator can log it and a reviewer can see whether the palette drove a
surface or merely brushed it.

Stdlib only. No ``bpy``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from vision import catalog

from . import colour

#: Per-family limits on how far a palette may move a material's own colour.
#:
#: ``hue`` is the maximum shift on the 0–1 hue wheel, ``lightness`` and
#: ``saturation`` the maximum absolute change, and ``pull`` how strongly the
#: colour is drawn toward the palette target before those limits are applied.
@dataclass(frozen=True)
class TintBudget:
    hue: float
    lightness: float
    saturation: float
    pull: float
    #: True when the palette may replace the colour outright rather than
    #: nudging it. Only ever true for materials whose colour is a choice.
    replaceable: bool = False


#: Keyed by material *family*, so every species inherits its family's physics.
TINT_BUDGETS: Dict[str, TintBudget] = {
    # -- Chosen colours: the palette is the point ---------------------------
    "paint_matte": TintBudget(1.0, 1.0, 1.0, 1.0, replaceable=True),
    "paint_satin": TintBudget(1.0, 1.0, 1.0, 1.0, replaceable=True),
    "paint_gloss": TintBudget(1.0, 1.0, 1.0, 1.0, replaceable=True),
    "wallpaper": TintBudget(0.35, 0.30, 0.35, 0.75),
    "gypsum": TintBudget(0.20, 0.18, 0.20, 0.55),

    # -- Textiles: wide but not unlimited -----------------------------------
    "fabric": TintBudget(0.30, 0.25, 0.30, 0.70),
    "carpet": TintBudget(0.22, 0.20, 0.22, 0.55),
    "leather": TintBudget(0.06, 0.16, 0.14, 0.35),
    "rattan": TintBudget(0.04, 0.12, 0.10, 0.25),

    # -- Timber: warmth only ------------------------------------------------
    "wood": TintBudget(0.025, 0.10, 0.12, 0.30),
    "wood_panel": TintBudget(0.025, 0.10, 0.12, 0.30),
    "laminate": TintBudget(0.05, 0.14, 0.16, 0.40),
    "vinyl": TintBudget(0.10, 0.16, 0.18, 0.45),

    # -- Stone and mineral: essentially fixed -------------------------------
    "marble": TintBudget(0.02, 0.08, 0.08, 0.22),
    "granite": TintBudget(0.02, 0.08, 0.08, 0.22),
    "stone": TintBudget(0.03, 0.10, 0.10, 0.25),
    "concrete": TintBudget(0.03, 0.12, 0.10, 0.30),
    "exposed_brick": TintBudget(0.02, 0.10, 0.10, 0.22),
    "tile": TintBudget(0.30, 0.25, 0.30, 0.65),
    "ceramic": TintBudget(0.30, 0.25, 0.30, 0.60),

    # -- Metal and glass: colour is the alloy -------------------------------
    "metal": TintBudget(0.015, 0.06, 0.06, 0.15),
    "glass": TintBudget(0.05, 0.05, 0.08, 0.15),
    "plastic": TintBudget(0.40, 0.30, 0.35, 0.75),

    "unknown": TintBudget(0.10, 0.15, 0.15, 0.40),
}

DEFAULT_BUDGET = TintBudget(0.08, 0.14, 0.14, 0.35)

#: Saturation below which a colour is treated as neutral and its *hue* is not
#: constrained.
#:
#: Hue is numerically degenerate near grey: for ``#22201F`` a one-bit change in
#: any channel swings the reported hue by a large fraction of the wheel while
#: the colour stays visually identical. Enforcing a hue budget there chases
#: quantisation noise and never converges. What actually has to be protected
#: for a near-neutral material is that it *stays* near-neutral — black marble
#: must not become blue marble — and that is the saturation budget's job.
NEUTRAL_SATURATION = 0.12

#: Which palette role drives which surface.
SURFACE_ROLE: Dict[str, str] = {
    "wall": "primary",
    "floor": "secondary",
    "ceiling": "primary",
    "curtains": "accent",
    "rug": "accent",
}

#: How strongly each surface follows its role. Ceilings are pulled only
#: slightly toward the wall colour — a ceiling painted the full wall colour
#: reads as a cave, and in real interiors it is almost always lighter.
SURFACE_STRENGTH: Dict[str, float] = {
    "wall": 1.0,
    "floor": 0.55,
    "ceiling": 0.30,
    "curtains": 0.85,
    "rug": 0.70,
    "furniture": 0.35,
    "decor": 0.60,
}


@dataclass
class Tint:
    """The outcome of applying a palette to one surface."""

    color_hex: str
    #: How far the colour actually moved, 0–1.
    moved: float
    #: Which palette role drove it, if any.
    role: str = ""
    #: True when the budget stopped the palette getting what it wanted.
    clamped: bool = False

    def __str__(self) -> str:
        # ASCII only: this reaches the build log, which on Windows is a cp1252
        # console that raises on anything outside it.
        if not self.role:
            return f"{self.color_hex} (untinted)"
        suffix = ", clamped" if self.clamped else ""
        return f"{self.color_hex} (-> {self.role}, moved {self.moved:.2f}{suffix})"


def budget_for(material: str) -> TintBudget:
    """The tint budget for a material, resolved through its family.

    Species inherit their family's physics: ``walnut`` gets the timber budget
    because walnut is timber, without the table needing an entry per species.
    """
    return TINT_BUDGETS.get(catalog.material_family(material), DEFAULT_BUDGET)


def apply(
    base_hex: str,
    material: str,
    target_hex: Optional[str],
    strength: float = 1.0,
    role: str = "",
) -> Tint:
    """Move ``base_hex`` toward ``target_hex``, within what the material allows.

    ``strength`` scales the pull for the specific surface — a floor follows the
    palette less closely than a wall does.

    Returns the original colour untouched when there is no target, when the
    material has no budget to move, or when the two are already the same. That
    "no change" path matters: it is what keeps an observed colour authoritative
    over a derived palette.
    """
    if not target_hex or not base_hex:
        return Tint(base_hex or colour.NEUTRAL, 0.0)

    limits = budget_for(material)

    # A chosen colour is simply set. Repainting a wall the palette's primary
    # colour is not a distortion of the material — it *is* the material.
    if limits.replaceable and strength >= 0.99:
        moved = colour.distance(base_hex, target_hex)
        return Tint(target_hex, moved, role=role)

    pull = limits.pull * max(0.0, min(1.0, strength))
    if pull <= 0.0:
        return Tint(base_hex, 0.0, role=role)

    wanted = colour.mix(base_hex, target_hex, pull)
    bounded, clamped = _clamp_to_budget(base_hex, wanted, limits)

    return Tint(
        color_hex=bounded,
        moved=colour.distance(base_hex, bounded),
        role=role,
        clamped=clamped,
    )


def _clamp_to_budget(base_hex: str, wanted_hex: str, limits: TintBudget) -> Tuple[str, bool]:
    """Hold a proposed colour inside the material's believable range.

    Clamped in HLS rather than RGB because the constraint is perceptual: what
    must be preserved is *which material this looks like*, and that lives
    almost entirely in hue. Clamping RGB channels independently would let a
    colour drift across the hue wheel while every channel stayed within its
    numeric limit.
    """
    base_h, base_l, base_s = colour.hls(base_hex)
    want_h, want_l, want_s = colour.hls(wanted_hex)

    clamped = False

    # Hue, on the shortest way round the wheel.
    delta_h = want_h - base_h
    if delta_h > 0.5:
        delta_h -= 1.0
    elif delta_h < -0.5:
        delta_h += 1.0
    if abs(delta_h) > limits.hue:
        delta_h = limits.hue if delta_h > 0 else -limits.hue
        clamped = True

    delta_l = want_l - base_l
    if abs(delta_l) > limits.lightness:
        delta_l = limits.lightness if delta_l > 0 else -limits.lightness
        clamped = True

    delta_s = want_s - base_s
    if abs(delta_s) > limits.saturation:
        delta_s = limits.saturation if delta_s > 0 else -limits.saturation
        clamped = True

    result = colour.from_hls(base_h + delta_h, base_l + delta_l, base_s + delta_s)

    # The clamp above is exact in continuous HLS, but the result is emitted as
    # 8-bit hex, and that quantisation can nudge a component back outside its
    # budget by a thousandth or so. The guarantee has to hold on what is
    # actually emitted, not on an intermediate, so any overshoot is walked
    # back and re-emitted until the *quantised* colour is inside budget.
    for _ in range(6):
        overshoot = _overshoot(result, base_h, base_l, base_s, limits)
        if overshoot <= 1.0:
            break
        # Back off by slightly more than the measured overshoot, so a single
        # pass normally suffices instead of creeping toward the limit.
        scale = 0.95 / overshoot
        delta_h, delta_l, delta_s = delta_h * scale, delta_l * scale, delta_s * scale
        result = colour.from_hls(base_h + delta_h, base_l + delta_l, base_s + delta_s)
        clamped = True

    return result, clamped


def _overshoot(result_hex: str, base_h: float, base_l: float, base_s: float,
               limits: TintBudget) -> float:
    """How far outside budget the emitted colour is, as a ratio (1.0 = at it).

    Hue is exempt for near-neutral colours, where it is degenerate: for a
    near-grey a one-bit change swings the reported hue across a large fraction
    of the wheel while the colour stays visually identical. What protects a
    neutral material is the saturation budget — black marble must not become
    blue marble — and that is checked here regardless.
    """
    result_h, result_l, result_s = colour.hls(result_hex)

    ratios = [
        abs(result_l - base_l) / limits.lightness if limits.lightness > 0 else 0.0,
        abs(result_s - base_s) / limits.saturation if limits.saturation > 0 else 0.0,
    ]
    if base_s >= NEUTRAL_SATURATION and limits.hue > 0:
        ratios.append(colour.hue_distance(result_h, base_h) / limits.hue)

    return max(ratios) if ratios else 0.0


# ---------------------------------------------------------------------------
# Surface and object entry points
# ---------------------------------------------------------------------------


def for_surface(
    base_hex: str, material: str, room_palette, surface: str
) -> Tint:
    """Tint an architectural surface — wall, floor, ceiling, curtains, rug."""
    if room_palette is None:
        return Tint(base_hex, 0.0)

    role = SURFACE_ROLE.get(surface, "primary")
    strength = SURFACE_STRENGTH.get(surface, 0.5)
    target = getattr(room_palette, role, None)

    tint = apply(base_hex, material, target, strength=strength, role=role)

    # A ceiling that ends up darker than its walls reads as a mistake even when
    # the arithmetic is right, so it is lifted back above them.
    if surface == "ceiling" and colour.luminance(tint.color_hex) < colour.luminance(base_hex):
        return Tint(base_hex, 0.0, role=role)

    return tint


def for_object(base_hex: str, material: str, group: str, room_palette) -> Tint:
    """Tint a furniture or decor colour.

    Furniture follows the palette weakly: its colour was usually *observed*,
    and an observation outranks a scheme derived from the same room. Decor
    follows more closely, because small accent objects are exactly what a
    palette's accent role describes.
    """
    if room_palette is None:
        return Tint(base_hex, 0.0)

    if group in ("decor", "appliance"):
        role, strength = "accent", SURFACE_STRENGTH["decor"]
    else:
        role, strength = "furniture", SURFACE_STRENGTH["furniture"]

    target = getattr(room_palette, role, None)
    return apply(base_hex, material, target, strength=strength, role=role)


def accent_for(room_palette, fallback: str = "#9AA5AE") -> str:
    """The palette's accent colour, for cushions, throws and small decor."""
    if room_palette is None:
        return fallback
    return getattr(room_palette, "accent", fallback) or fallback
