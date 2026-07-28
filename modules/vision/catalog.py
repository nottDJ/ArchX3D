"""
ArchX3D — Object & Material Catalog
===================================
Controlled vocabulary plus **real-world metric priors** for everything the
vision layer can recognise.

Why priors matter
-----------------
Vision-language models are reliable at *naming* things and describing their
relationships, but poor at absolute metric estimation from a single image —
ask one how wide a sofa is and you get a plausible-sounding number with no
grounding. So the pipeline never trusts model-supplied metres directly.
Instead the model reports a **size bucket** and an image-space box, and this
table supplies the metric anchor that gets modulated by those observations
(see ``grounding.resolve_dimensions``).

Every prior is a typical real-world dimension in metres, with a plausible
range used to clamp anything the model suggests.

Stdlib only — Blender imports this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Object priors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectPrior:
    """Everything the placement layer needs to know about a category."""

    category: str
    #: furniture | decor | appliance | fixture
    group: str
    #: Typical (width, depth, height) in metres.
    typical: Tuple[float, float, float]
    #: Per-axis (min, max) clamps in metres.
    limits: Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]
    #: floor | wall | ceiling | on_object
    support: str = "floor"
    #: 0 = belongs in open space, 1 = almost always pushed against a wall.
    wall_affinity: float = 0.0
    #: Gap left between the object's back face and the wall, metres.
    wall_clearance: float = 0.05
    #: face_room  — back to wall, front into the room
    #: face_target — rotated to face a related object
    #: free        — orientation carries little meaning (rugs, plants)
    orientation: str = "free"
    #: Procedural asset family used to build geometry in Blender.
    asset_family: str = "box"
    #: Height of the surface objects can rest on, metres (0 = not a surface).
    surface_height: float = 0.0
    #: Mounting height above floor for wall-mounted items, metres.
    mount_height: float = 1.4


def _p(
    category: str,
    group: str,
    typical: Tuple[float, float, float],
    limits: Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]],
    **kwargs,
) -> ObjectPrior:
    return ObjectPrior(category=category, group=group, typical=typical, limits=limits, **kwargs)


#: The complete recognised vocabulary. Anything outside this set is either
#: mapped through ``SYNONYMS`` or dropped as unrecognised.
OBJECT_CATALOG: Dict[str, ObjectPrior] = {
    # ---- Seating ---------------------------------------------------------
    "sofa": _p("sofa", "furniture", (2.10, 0.90, 0.85), ((1.4, 3.0), (0.7, 1.1), (0.6, 1.0)),
               wall_affinity=0.75, orientation="face_target", asset_family="sofa", surface_height=0.42),
    "sectional": _p("sectional", "furniture", (2.80, 1.80, 0.85), ((2.0, 4.2), (1.2, 2.6), (0.6, 1.0)),
                    wall_affinity=0.80, orientation="face_target", asset_family="sectional", surface_height=0.42),
    "armchair": _p("armchair", "furniture", (0.85, 0.85, 0.90), ((0.6, 1.2), (0.6, 1.1), (0.6, 1.1)),
                   wall_affinity=0.25, orientation="face_target", asset_family="armchair", surface_height=0.42),
    "chair": _p("chair", "furniture", (0.50, 0.55, 0.90), ((0.35, 0.8), (0.35, 0.8), (0.7, 1.1)),
                orientation="face_target", asset_family="chair", surface_height=0.45),
    "dining_chair": _p("dining_chair", "furniture", (0.48, 0.52, 0.92), ((0.35, 0.7), (0.35, 0.7), (0.75, 1.1)),
                       orientation="face_target", asset_family="chair", surface_height=0.45),
    "office_chair": _p("office_chair", "furniture", (0.62, 0.62, 1.10), ((0.5, 0.8), (0.5, 0.8), (0.85, 1.3)),
                       orientation="face_target", asset_family="office_chair", surface_height=0.48),
    "stool": _p("stool", "furniture", (0.38, 0.38, 0.68), ((0.28, 0.55), (0.28, 0.55), (0.4, 0.85)),
                asset_family="stool", surface_height=0.68),
    "bench": _p("bench", "furniture", (1.40, 0.40, 0.45), ((0.8, 2.2), (0.3, 0.6), (0.35, 0.6)),
                wall_affinity=0.6, orientation="face_room", asset_family="box", surface_height=0.45),

    # ---- Tables ----------------------------------------------------------
    "coffee_table": _p("coffee_table", "furniture", (1.20, 0.60, 0.42), ((0.7, 1.8), (0.4, 0.9), (0.3, 0.55)),
                       orientation="face_target", asset_family="table", surface_height=0.42),
    "dining_table": _p("dining_table", "furniture", (1.60, 0.90, 0.75), ((1.0, 2.8), (0.7, 1.3), (0.68, 0.82)),
                       asset_family="table", surface_height=0.75),
    "side_table": _p("side_table", "furniture", (0.50, 0.50, 0.55), ((0.3, 0.8), (0.3, 0.8), (0.4, 0.75)),
                     wall_affinity=0.3, asset_family="table", surface_height=0.55),
    "bedside_table": _p("bedside_table", "furniture", (0.45, 0.40, 0.55), ((0.3, 0.7), (0.3, 0.6), (0.4, 0.75)),
                        wall_affinity=0.85, orientation="face_room", asset_family="cabinet", surface_height=0.55),
    "study_table": _p("study_table", "furniture", (1.40, 0.70, 0.75), ((0.9, 2.0), (0.5, 0.9), (0.68, 0.82)),
                      wall_affinity=0.75, orientation="face_room", asset_family="table", surface_height=0.75),
    "console_table": _p("console_table", "furniture", (1.20, 0.35, 0.80), ((0.8, 1.8), (0.25, 0.5), (0.7, 0.95)),
                        wall_affinity=0.95, orientation="face_room", asset_family="table", surface_height=0.80),

    # ---- Storage ---------------------------------------------------------
    "tv_unit": _p("tv_unit", "furniture", (1.80, 0.45, 0.50), ((1.0, 3.0), (0.3, 0.7), (0.3, 0.8)),
                  wall_affinity=0.95, orientation="face_room", asset_family="tv_unit", surface_height=0.50),
    "cabinet": _p("cabinet", "furniture", (1.00, 0.45, 0.90), ((0.5, 2.0), (0.3, 0.7), (0.6, 1.3)),
                  wall_affinity=0.9, orientation="face_room", asset_family="cabinet", surface_height=0.90),
    "wardrobe": _p("wardrobe", "furniture", (1.80, 0.60, 2.20), ((0.8, 3.0), (0.5, 0.8), (1.7, 2.6)),
                   wall_affinity=1.0, orientation="face_room", asset_family="cabinet"),
    "bookshelf": _p("bookshelf", "furniture", (0.90, 0.35, 1.80), ((0.5, 2.0), (0.25, 0.5), (0.8, 2.4)),
                    wall_affinity=1.0, orientation="face_room", asset_family="bookshelf"),
    "shelves": _p("shelves", "decor", (1.00, 0.28, 0.30), ((0.4, 2.0), (0.15, 0.45), (0.03, 1.2)),
                  support="wall", orientation="face_room", asset_family="shelf", mount_height=1.5),
    "sideboard": _p("sideboard", "furniture", (1.60, 0.45, 0.80), ((0.9, 2.4), (0.35, 0.6), (0.6, 1.0)),
                    wall_affinity=0.95, orientation="face_room", asset_family="cabinet", surface_height=0.80),

    # ---- Bedroom ---------------------------------------------------------
    "bed": _p("bed", "furniture", (1.60, 2.00, 0.55), ((0.9, 2.2), (1.8, 2.2), (0.35, 1.3)),
              wall_affinity=0.95, orientation="face_room", asset_family="bed", surface_height=0.55),

    # ---- Kitchen ---------------------------------------------------------
    "kitchen_island": _p("kitchen_island", "furniture", (1.80, 0.90, 0.90), ((1.0, 3.0), (0.6, 1.4), (0.85, 1.05)),
                         asset_family="cabinet", surface_height=0.90),
    "kitchen_counter": _p("kitchen_counter", "furniture", (2.40, 0.60, 0.90), ((0.8, 5.0), (0.5, 0.8), (0.85, 1.0)),
                          wall_affinity=1.0, orientation="face_room", asset_family="cabinet", surface_height=0.90),

    # ---- Appliances ------------------------------------------------------
    "refrigerator": _p("refrigerator", "appliance", (0.70, 0.70, 1.80), ((0.5, 1.2), (0.5, 0.9), (1.2, 2.2)),
                       wall_affinity=1.0, orientation="face_room", asset_family="cabinet"),
    "microwave": _p("microwave", "appliance", (0.50, 0.40, 0.30), ((0.35, 0.7), (0.3, 0.55), (0.22, 0.45)),
                    support="on_object", asset_family="box"),
    "washing_machine": _p("washing_machine", "appliance", (0.60, 0.60, 0.85), ((0.5, 0.8), (0.5, 0.8), (0.75, 1.0)),
                          wall_affinity=1.0, orientation="face_room", asset_family="box"),
    "oven": _p("oven", "appliance", (0.60, 0.58, 0.60), ((0.45, 0.8), (0.45, 0.7), (0.45, 1.2)),
               wall_affinity=1.0, orientation="face_room", asset_family="box"),
    "ac_unit": _p("ac_unit", "appliance", (0.90, 0.22, 0.30), ((0.6, 1.3), (0.15, 0.35), (0.2, 0.45)),
                  support="wall", orientation="face_room", asset_family="box", mount_height=2.30),

    # ---- Screens & electronics ------------------------------------------
    "tv": _p("tv", "decor", (1.20, 0.07, 0.70), ((0.6, 2.2), (0.04, 0.15), (0.35, 1.3)),
             support="wall", orientation="face_room", asset_family="screen", mount_height=1.15),
    "monitor": _p("monitor", "decor", (0.60, 0.20, 0.45), ((0.35, 0.9), (0.12, 0.3), (0.3, 0.6)),
                  support="on_object", orientation="face_target", asset_family="screen"),
    "laptop": _p("laptop", "decor", (0.35, 0.25, 0.22), ((0.25, 0.45), (0.18, 0.32), (0.02, 0.3)),
                 support="on_object", asset_family="box"),
    "speaker": _p("speaker", "decor", (0.25, 0.25, 1.00), ((0.12, 0.45), (0.12, 0.45), (0.15, 1.3)),
                  wall_affinity=0.7, asset_family="box"),

    # ---- Soft furnishings ------------------------------------------------
    "rug": _p("rug", "decor", (2.40, 1.70, 0.02), ((0.8, 4.5), (0.6, 3.5), (0.005, 0.06)),
              asset_family="rug"),
    "carpet": _p("carpet", "decor", (3.00, 2.20, 0.02), ((1.0, 6.0), (0.8, 4.5), (0.005, 0.06)),
                 asset_family="rug"),
    "cushion": _p("cushion", "decor", (0.45, 0.45, 0.15), ((0.25, 0.7), (0.15, 0.7), (0.08, 0.25)),
                  support="on_object", asset_family="cushion"),
    "pillow": _p("pillow", "decor", (0.55, 0.35, 0.14), ((0.35, 0.8), (0.25, 0.5), (0.08, 0.22)),
                 support="on_object", asset_family="cushion"),
    "curtains": _p("curtains", "decor", (1.60, 0.12, 2.40), ((0.6, 4.0), (0.05, 0.3), (1.0, 3.2)),
                   support="wall", orientation="face_room", asset_family="curtain", mount_height=2.55),
    "blinds": _p("blinds", "decor", (1.20, 0.06, 1.40), ((0.5, 3.0), (0.03, 0.15), (0.6, 2.6)),
                 support="wall", orientation="face_room", asset_family="blinds", mount_height=2.10),

    # ---- Wall decor ------------------------------------------------------
    "painting": _p("painting", "decor", (0.80, 0.05, 1.00), ((0.2, 2.5), (0.02, 0.12), (0.2, 2.0)),
                   support="wall", orientation="face_room", asset_family="frame", mount_height=1.55),
    "mirror": _p("mirror", "decor", (0.70, 0.05, 1.20), ((0.3, 2.0), (0.02, 0.12), (0.3, 2.2)),
                 support="wall", orientation="face_room", asset_family="mirror", mount_height=1.50),
    "photo_frame": _p("photo_frame", "decor", (0.20, 0.04, 0.25), ((0.08, 0.5), (0.02, 0.1), (0.1, 0.5)),
                      support="on_object", orientation="face_room", asset_family="frame"),
    "clock": _p("clock", "decor", (0.30, 0.05, 0.30), ((0.12, 0.7), (0.03, 0.12), (0.12, 0.7)),
                support="wall", orientation="face_room", asset_family="clock", mount_height=2.00),

    # ---- Greenery & objects ---------------------------------------------
    "plant": _p("plant", "decor", (0.60, 0.60, 1.20), ((0.2, 1.4), (0.2, 1.4), (0.2, 2.4)),
                wall_affinity=0.35, asset_family="plant"),
    "flower_vase": _p("flower_vase", "decor", (0.20, 0.20, 0.35), ((0.08, 0.45), (0.08, 0.45), (0.1, 0.7)),
                      support="on_object", asset_family="vase"),
    "books": _p("books", "decor", (0.25, 0.16, 0.20), ((0.1, 0.5), (0.08, 0.35), (0.03, 0.35)),
                support="on_object", asset_family="box"),
    "bowl": _p("bowl", "decor", (0.25, 0.25, 0.10), ((0.1, 0.5), (0.1, 0.5), (0.04, 0.25)),
               support="on_object", asset_family="vase"),

    # ---- Ceiling fixtures (non-emissive) --------------------------------
    "ceiling_fan": _p("ceiling_fan", "fixture", (1.20, 1.20, 0.35), ((0.7, 1.8), (0.7, 1.8), (0.2, 0.6)),
                      support="ceiling", asset_family="ceiling_fan"),
}


#: Free-text labels the model produces, mapped onto catalog categories.
#: Longest match wins, so multi-word keys are checked before single words.
SYNONYMS: Dict[str, str] = {
    "l-shaped sofa": "sectional", "l shaped sofa": "sectional", "corner sofa": "sectional",
    "couch": "sofa", "settee": "sofa", "loveseat": "sofa", "davenport": "sofa",
    "accent chair": "armchair", "lounge chair": "armchair", "recliner": "armchair",
    "easy chair": "armchair", "club chair": "armchair",
    "desk chair": "office_chair", "swivel chair": "office_chair", "task chair": "office_chair",
    "bar stool": "stool", "counter stool": "stool", "ottoman": "stool", "pouf": "stool",
    "centre table": "coffee_table", "center table": "coffee_table", "cocktail table": "coffee_table",
    "tea table": "coffee_table",
    "dining set": "dining_table", "kitchen table": "dining_table",
    "end table": "side_table", "accent table": "side_table", "lamp table": "side_table",
    "nightstand": "bedside_table", "night stand": "bedside_table", "night table": "bedside_table",
    "desk": "study_table", "writing desk": "study_table", "work desk": "study_table",
    "computer table": "study_table", "workstation": "study_table",
    "tv stand": "tv_unit", "tv console": "tv_unit", "media unit": "tv_unit",
    "media console": "tv_unit", "entertainment unit": "tv_unit", "tv cabinet": "tv_unit",
    "chest of drawers": "cabinet", "dresser": "cabinet", "drawer unit": "cabinet",
    "credenza": "sideboard", "buffet": "sideboard",
    "closet": "wardrobe", "almirah": "wardrobe", "armoire": "wardrobe",
    "book shelf": "bookshelf", "bookcase": "bookshelf", "book case": "bookshelf",
    "shelf": "shelves", "wall shelf": "shelves", "floating shelf": "shelves",
    "double bed": "bed", "queen bed": "bed", "king bed": "bed", "single bed": "bed",
    "mattress": "bed", "platform bed": "bed",
    "island": "kitchen_island", "breakfast bar": "kitchen_island",
    "countertop": "kitchen_counter", "counter": "kitchen_counter", "kitchen cabinets": "kitchen_counter",
    "fridge": "refrigerator", "freezer": "refrigerator",
    "washer": "washing_machine", "washing machine": "washing_machine",
    "air conditioner": "ac_unit", "split ac": "ac_unit", "ac": "ac_unit", "hvac": "ac_unit",
    "television": "tv", "flat screen": "tv", "flatscreen": "tv", "tv screen": "tv",
    "computer monitor": "monitor", "display": "monitor", "screen": "monitor",
    "notebook": "laptop", "macbook": "laptop",
    "loudspeaker": "speaker", "sound system": "speaker", "soundbar": "speaker",
    "area rug": "rug", "floor rug": "rug", "runner": "rug",
    "throw pillow": "cushion", "scatter cushion": "cushion", "bolster": "cushion",
    "drapes": "curtains", "curtain": "curtains", "sheers": "curtains",
    "window blinds": "blinds", "roller blind": "blinds", "venetian blinds": "blinds",
    "artwork": "painting", "wall art": "painting", "canvas": "painting", "picture": "painting",
    "framed art": "painting", "poster": "painting",
    "wall mirror": "mirror", "looking glass": "mirror",
    "picture frame": "photo_frame", "photo": "photo_frame",
    "wall clock": "clock",
    "potted plant": "plant", "houseplant": "plant", "indoor plant": "plant",
    "planter": "plant", "tree": "plant", "greenery": "plant", "fiddle leaf fig": "plant",
    "vase": "flower_vase", "flowers": "flower_vase", "flower pot": "flower_vase",
    "book": "books", "magazines": "books", "stack of books": "books",
    "fruit bowl": "bowl", "decorative bowl": "bowl",
    "fan": "ceiling_fan",
}


def normalise_category(raw: str) -> Optional[str]:
    """Map a free-text model label onto a catalog category.

    Returns ``None`` when the label cannot be confidently recognised — the
    caller should then discard or flag it rather than inventing a category.
    """
    if not raw:
        return None
    text = raw.strip().lower().replace("_", " ")
    text = " ".join(text.split())

    # Exact hits first.
    if text.replace(" ", "_") in OBJECT_CATALOG:
        return text.replace(" ", "_")
    if text in SYNONYMS:
        return SYNONYMS[text]

    # Substring match, longest key first so "l-shaped sofa" beats "sofa".
    for key in sorted(SYNONYMS, key=len, reverse=True):
        if key in text:
            return SYNONYMS[key]
    for key in sorted(OBJECT_CATALOG, key=len, reverse=True):
        if key.replace("_", " ") in text:
            return key

    return None


def get_prior(category: str) -> Optional[ObjectPrior]:
    return OBJECT_CATALOG.get(category)


# ---------------------------------------------------------------------------
# Size buckets
# ---------------------------------------------------------------------------
#
# The model is asked for a coarse bucket rather than metres, because coarse
# relative judgements ("this sofa is large for a sofa") are something a VLM
# does reliably, whereas absolute metric estimates are not.

SIZE_BUCKETS: Dict[str, float] = {
    "very_small": 0.65,
    "small": 0.82,
    "medium": 1.00,
    "large": 1.22,
    "very_large": 1.45,
}
DEFAULT_SIZE_BUCKET = "medium"


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialPrior:
    """Shading defaults for a named material.

    Materials come in two tiers. The *family* ("wood", "marble", "fabric") is
    what the pipeline has always used and what every consumer understands. The
    *species* ("light_oak", "white_marble", "velvet") is a finer named
    variant that carries its own colour and shading response.

    Species declare their family in ``base``. That is what keeps the taxonomy
    extensible without a flag day: anything that only knows families — asset
    matching, the Blender material builder, older scene graphs — can call
    :func:`material_family` and keep working, while anything that wants the
    finer term gets it. A family's ``base`` is itself.
    """

    name: str
    color_hex: str
    roughness: float
    metallic: float = 0.0
    #: Which surfaces this material legitimately applies to.
    applies_to: Tuple[str, ...] = ("wall", "floor", "ceiling", "object")
    #: Generic family this material reduces to. Empty means it is a family.
    base: str = ""
    #: Procedural texture recipe for the Blender builder — see
    #: ``blender_generator``. Species that share a recipe differ only in the
    #: parameters, so one generator covers a whole family.
    texture: str = "flat"
    #: Strength of the procedural pattern, 0–1. Drives bump and colour variance.
    grain: float = 0.0


MATERIALS: Dict[str, MaterialPrior] = {
    # -- Wall finishes -----------------------------------------------------
    "paint_matte": MaterialPrior("paint_matte", "#EFEDE8", 0.92, applies_to=("wall", "ceiling")),
    "paint_satin": MaterialPrior("paint_satin", "#EFEDE8", 0.55, applies_to=("wall", "ceiling")),
    "paint_gloss": MaterialPrior("paint_gloss", "#F2F0EC", 0.22, applies_to=("wall", "ceiling")),
    "wallpaper": MaterialPrior("wallpaper", "#DED5C8", 0.78, applies_to=("wall",)),
    "wood_panel": MaterialPrior("wood_panel", "#B08A5E", 0.55, applies_to=("wall", "ceiling", "object")),
    "exposed_brick": MaterialPrior("exposed_brick", "#9B5D45", 0.90, applies_to=("wall",)),
    "concrete": MaterialPrior("concrete", "#9A9A96", 0.85, applies_to=("wall", "floor", "ceiling")),
    "marble": MaterialPrior("marble", "#E8E4DC", 0.18, applies_to=("wall", "floor", "object")),
    "tile": MaterialPrior("tile", "#DAD8D2", 0.30, applies_to=("wall", "floor")),
    "granite": MaterialPrior("granite", "#6E6B66", 0.28, applies_to=("floor", "object")),
    "stone": MaterialPrior("stone", "#A8A29A", 0.80, applies_to=("wall", "floor")),

    # -- Floor finishes ----------------------------------------------------
    "wood": MaterialPrior("wood", "#C08E5C", 0.45, applies_to=("floor", "object")),
    "wood_dark": MaterialPrior("wood_dark", "#6B4A2F", 0.45, applies_to=("floor", "object")),
    "wood_light": MaterialPrior("wood_light", "#D6B48C", 0.45, applies_to=("floor", "object")),
    "laminate": MaterialPrior("laminate", "#C6A483", 0.38, applies_to=("floor",)),
    "vinyl": MaterialPrior("vinyl", "#BFB4A6", 0.42, applies_to=("floor",)),
    "carpet": MaterialPrior("carpet", "#9A9186", 0.95, applies_to=("floor", "object")),

    # -- Ceiling -----------------------------------------------------------
    "gypsum": MaterialPrior("gypsum", "#F6F5F2", 0.90, applies_to=("ceiling",)),

    # -- Object materials --------------------------------------------------
    "fabric": MaterialPrior("fabric", "#9C978E", 0.95, applies_to=("object",)),
    "leather": MaterialPrior("leather", "#6B4F3A", 0.45, applies_to=("object",)),
    "metal": MaterialPrior("metal", "#8C8F94", 0.32, metallic=0.9, applies_to=("object",)),
    "glass": MaterialPrior("glass", "#D4E2E6", 0.06, applies_to=("object",)),
    "plastic": MaterialPrior("plastic", "#C9C9C9", 0.50, applies_to=("object",)),
    "ceramic": MaterialPrior("ceramic", "#E4E0D8", 0.25, applies_to=("object",)),
    "rattan": MaterialPrior("rattan", "#C4A473", 0.75, applies_to=("object",)),
    "unknown": MaterialPrior("unknown", "#BFBFBF", 0.65),
}

#: Procedural recipe and pattern strength for each family, so a species
#: inherits a sane texture without restating it. Applied below.
_FAMILY_TEXTURE: Dict[str, Tuple[str, float]] = {
    "paint_matte": ("flat", 0.02), "paint_satin": ("flat", 0.02),
    "paint_gloss": ("flat", 0.01), "wallpaper": ("weave", 0.18),
    "wood_panel": ("wood_grain", 0.55), "exposed_brick": ("brick", 0.85),
    "concrete": ("noise", 0.45), "marble": ("veined", 0.60),
    "tile": ("tiled", 0.35), "granite": ("speckle", 0.55),
    "stone": ("noise", 0.70), "wood": ("wood_grain", 0.50),
    "wood_dark": ("wood_grain", 0.50), "wood_light": ("wood_grain", 0.45),
    "laminate": ("wood_grain", 0.25), "vinyl": ("flat", 0.10),
    "carpet": ("weave", 0.80), "gypsum": ("flat", 0.05),
    "fabric": ("weave", 0.60), "leather": ("grain", 0.35),
    "metal": ("brushed", 0.20), "glass": ("flat", 0.0),
    "plastic": ("flat", 0.05), "ceramic": ("flat", 0.08),
    "rattan": ("weave", 0.90), "unknown": ("flat", 0.0),
}

for _name, (_texture, _grain) in _FAMILY_TEXTURE.items():
    MATERIALS[_name] = replace(MATERIALS[_name], texture=_texture, grain=_grain)


def _species(
    name: str,
    base: str,
    color_hex: str,
    roughness: Optional[float] = None,
    **kwargs,
) -> MaterialPrior:
    """A finer-grained material inheriting its family's surfaces and recipe."""
    parent = MATERIALS[base]
    return MaterialPrior(
        name=name,
        color_hex=color_hex,
        roughness=parent.roughness if roughness is None else roughness,
        metallic=kwargs.pop("metallic", parent.metallic),
        applies_to=kwargs.pop("applies_to", parent.applies_to),
        base=base,
        texture=kwargs.pop("texture", parent.texture),
        grain=kwargs.pop("grain", parent.grain),
    )


#: Species-level materials.
#:
#: The pipeline used to answer "wood" where a reference photograph plainly
#: showed walnut. Naming the species is what lets the generator reach a colour
#: and a grain that reads as the same material rather than as generic timber —
#: and because each declares a ``base``, nothing that only understands
#: families has to change.
MATERIALS.update({
    # -- Timber ------------------------------------------------------------
    "light_oak": _species("light_oak", "wood", "#D9BC90", 0.42, grain=0.45),
    "white_oak": _species("white_oak", "wood", "#E0CBA8", 0.40, grain=0.40),
    "walnut": _species("walnut", "wood", "#5C4033", 0.44, grain=0.62),
    "teak": _species("teak", "wood", "#9C6B3F", 0.40, grain=0.55),
    "mahogany": _species("mahogany", "wood", "#7A3B2E", 0.38, grain=0.58),
    "ash": _species("ash", "wood", "#E3D5BC", 0.46, grain=0.38),
    "birch_ply": _species("birch_ply", "wood", "#E5C79A", 0.50, grain=0.30),
    "ebony": _species("ebony", "wood", "#2E2622", 0.35, grain=0.50),

    # -- Stone -------------------------------------------------------------
    "white_marble": _species("white_marble", "marble", "#F1EEE8", 0.14),
    "black_marble": _species("black_marble", "marble", "#22201F", 0.14),
    "green_marble": _species("green_marble", "marble", "#3E5C4B", 0.16),
    "travertine": _species("travertine", "marble", "#D8C7AC", 0.35, grain=0.45),
    "grey_concrete": _species("grey_concrete", "concrete", "#9A9A96", 0.85),
    "polished_concrete": _species("polished_concrete", "concrete", "#A4A29C", 0.30, grain=0.25),
    "terrazzo": _species("terrazzo", "concrete", "#DCD6C8", 0.35, texture="speckle", grain=0.75),
    "slate": _species("slate", "stone", "#4A4E51", 0.70),
    "limestone": _species("limestone", "stone", "#CFC6B4", 0.75),

    # -- Ceramic and tile --------------------------------------------------
    "porcelain_tile": _species("porcelain_tile", "tile", "#E6E3DC", 0.22),
    "ceramic_tile": _species("ceramic_tile", "tile", "#DCD9D1", 0.32),
    "mosaic_tile": _species("mosaic_tile", "tile", "#BFD3D6", 0.30, grain=0.60),
    "subway_tile": _species("subway_tile", "tile", "#F0EEE9", 0.20, grain=0.30),

    # -- Paint -------------------------------------------------------------
    "semi_gloss_paint": _species("semi_gloss_paint", "paint_satin", "#EFEDE8", 0.38),
    "limewash": _species("limewash", "paint_matte", "#E6DFD2", 0.95, grain=0.20),

    # -- Textile -----------------------------------------------------------
    "linen": _species("linen", "fabric", "#D6CDBC", 0.96, grain=0.70),
    "velvet": _species("velvet", "fabric", "#4A3B52", 0.88, grain=0.35),
    "boucle": _species("boucle", "fabric", "#E2DBCE", 0.97, grain=0.90),
    "wool": _species("wool", "fabric", "#9A9384", 0.95, grain=0.65),
    "cotton": _species("cotton", "fabric", "#DEDAD2", 0.94, grain=0.50),
    "jute": _species("jute", "carpet", "#C2A87C", 0.95, grain=0.85),

    # -- Leather -----------------------------------------------------------
    "tan_leather": _species("tan_leather", "leather", "#A9764C", 0.46),
    "black_leather": _species("black_leather", "leather", "#2B2724", 0.42),

    # -- Metal -------------------------------------------------------------
    "brushed_steel": _species("brushed_steel", "metal", "#9EA2A6", 0.36),
    "brass": _species("brass", "metal", "#B08D45", 0.30),
    "blackened_steel": _species("blackened_steel", "metal", "#3A3B3D", 0.42),
    "chrome": _species("chrome", "metal", "#C9CDD1", 0.08),
})

MATERIAL_SYNONYMS: Dict[str, str] = {
    "painted": "paint_matte", "paint": "paint_matte", "matte paint": "paint_matte",
    "emulsion": "paint_matte", "plaster": "paint_matte", "drywall": "paint_matte",
    "satin paint": "paint_satin", "eggshell": "paint_satin",
    "gloss paint": "paint_gloss", "glossy": "paint_gloss",
    "wall paper": "wallpaper", "patterned wallpaper": "wallpaper",
    "wooden panel": "wood_panel", "wood panelling": "wood_panel", "wood paneling": "wood_panel",
    "timber panel": "wood_panel", "slat wall": "wood_panel", "veneer": "wood_panel",
    "brick": "exposed_brick", "brickwork": "exposed_brick",
    "cement": "concrete", "screed": "concrete", "microcement": "concrete",
    "marble tile": "marble", "polished marble": "marble",
    "ceramic tile": "tile", "porcelain": "tile", "tiles": "tile", "tiled": "tile",
    "hardwood": "wood", "wooden": "wood", "parquet": "wood", "timber": "wood",
    "oak": "wood_light", "light oak": "wood_light", "birch": "wood_light", "maple": "wood_light",
    "ash": "wood_light", "pine": "wood_light",
    "walnut": "wood_dark", "mahogany": "wood_dark", "dark wood": "wood_dark", "teak": "wood_dark",
    "rug": "carpet", "carpeted": "carpet", "wool": "carpet",
    "gypsum board": "gypsum", "false ceiling": "gypsum", "pop": "gypsum",
    "plasterboard": "gypsum", "pop ceiling": "gypsum",
    "upholstery": "fabric", "upholstered": "fabric", "linen": "fabric", "cotton": "fabric",
    "velvet": "fabric", "textile": "fabric", "cloth": "fabric",
    "faux leather": "leather", "leatherette": "leather",
    "steel": "metal", "stainless steel": "metal", "chrome": "metal", "brass": "metal",
    "aluminium": "metal", "aluminum": "metal", "iron": "metal",
    "tempered glass": "glass", "mirror": "glass",
    "wicker": "rattan", "cane": "rattan", "bamboo": "rattan",
}


def normalise_material(raw: str) -> str:
    """Map free text to a catalog material, defaulting to ``"unknown"``."""
    if not raw:
        return "unknown"
    text = " ".join(raw.strip().lower().replace("_", " ").split())
    if text.replace(" ", "_") in MATERIALS:
        return text.replace(" ", "_")
    if text in MATERIAL_SYNONYMS:
        return MATERIAL_SYNONYMS[text]
    for key in sorted(MATERIAL_SYNONYMS, key=len, reverse=True):
        if key in text:
            return MATERIAL_SYNONYMS[key]
    for key in sorted(MATERIALS, key=len, reverse=True):
        if key.replace("_", " ") in text:
            return key
    return "unknown"


def get_material(name: str) -> MaterialPrior:
    return MATERIALS.get(name, MATERIALS["unknown"])


def material_family(name: str) -> str:
    """The generic family a material reduces to.

    Consumers that only understand families — asset matching, older scene
    graphs, anything written before the species tier existed — call this and
    keep working unchanged.
    """
    prior = MATERIALS.get(name)
    if prior is None:
        return "unknown"
    return prior.base or prior.name


def species_of(family: str) -> List[str]:
    """Every finer-grained material belonging to ``family``, for the UI."""
    return sorted(name for name, prior in MATERIALS.items() if prior.base == family)


#: Ceiling treatments the model may report.
CEILING_TYPES = ("plain", "gypsum", "wooden", "decorative", "recessed", "coffered", "exposed")


# ---------------------------------------------------------------------------
# Interior styles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StylePrior:
    """What a named interior style implies for reconstruction.

    Style was previously free text on the room, used for nothing but a label.
    Making it a controlled vocabulary with implications is what lets it *do*
    something: bias asset selection, and supply plausible finishes for a room
    the model described stylistically but not materially.
    """

    name: str
    #: Materials characteristic of the style, most typical first.
    materials: Tuple[str, ...] = ()
    #: Representative palette — used only as a fallback when no imagery
    #: established the room's own colours.
    palette: Tuple[str, ...] = ()
    #: Warm | neutral | cool bias of the style's typical lighting.
    lighting: str = "neutral"
    #: Words a model is likely to use for this style, for normalisation.
    synonyms: Tuple[str, ...] = ()


STYLES: Dict[str, StylePrior] = {
    "modern": StylePrior(
        "modern", ("paint_matte", "light_oak", "brushed_steel", "glass"),
        ("#F2F1EE", "#3C3F44", "#9EA2A6"), "neutral",
        ("clean", "sleek", "current"),
    ),
    "minimalist": StylePrior(
        "minimalist", ("paint_matte", "white_oak", "polished_concrete"),
        ("#FAF9F6", "#E3DFD7", "#2E2C2A"), "cool",
        ("minimal", "spare", "pared back", "pared-back"),
    ),
    "industrial": StylePrior(
        "industrial", ("exposed_brick", "grey_concrete", "blackened_steel", "reclaimed"),
        ("#6E6864", "#9B5D45", "#2F3133"), "cool",
        ("loft", "warehouse", "utilitarian"),
    ),
    "luxury": StylePrior(
        "luxury", ("white_marble", "velvet", "brass", "walnut"),
        ("#EFE9DE", "#1F2A33", "#B08D45"), "warm",
        ("luxurious", "opulent", "high end", "high-end", "glamorous"),
    ),
    "scandinavian": StylePrior(
        "scandinavian", ("white_oak", "ash", "wool", "paint_matte"),
        ("#F4F1EA", "#D9C7A9", "#5A6B72"), "neutral",
        ("scandi", "nordic", "hygge"),
    ),
    "contemporary": StylePrior(
        "contemporary", ("paint_satin", "light_oak", "porcelain_tile", "linen"),
        ("#EDEBE6", "#4A5259", "#C0A98B"), "neutral",
        ("present day", "present-day"),
    ),
    "traditional": StylePrior(
        "traditional", ("mahogany", "wallpaper", "wool", "tan_leather"),
        ("#E7DFCD", "#6B4A2F", "#7A3B2E"), "warm",
        ("period", "heritage", "victorian", "georgian"),
    ),
    "bohemian": StylePrior(
        "bohemian", ("rattan", "jute", "cotton", "teak"),
        ("#D8C3A5", "#8C5A3C", "#3F6B5E"), "warm",
        ("boho", "eclectic", "layered"),
    ),
    "japanese": StylePrior(
        "japanese", ("ash", "paper", "birch_ply", "limewash"),
        ("#EDE7DA", "#2E2A26", "#7C8A72"), "warm",
        ("japandi", "zen", "wabi sabi", "wabi-sabi"),
    ),
    "mediterranean": StylePrior(
        "mediterranean", ("limewash", "terracotta", "travertine", "rattan"),
        ("#F0E4D2", "#C1683F", "#4E7B8C"), "warm",
        ("coastal", "spanish", "greek", "santorini"),
    ),
    "classic": StylePrior(
        "classic", ("white_marble", "paint_satin", "walnut", "brass"),
        ("#F2EDE4", "#2C3A4A", "#B08D45"), "warm",
        ("neoclassical", "timeless", "formal"),
    ),
    "farmhouse": StylePrior(
        "farmhouse", ("paint_matte", "reclaimed", "birch_ply", "linen"),
        ("#F5F1E8", "#7A6A55", "#455A4A"), "warm",
        ("rustic", "country", "shaker", "cottage"),
    ),
    "art_deco": StylePrior(
        "art_deco", ("brass", "velvet", "black_marble", "ebony"),
        ("#1C1B1A", "#B08D45", "#2E4A52"), "warm",
        ("deco", "gatsby", "jazz age"),
    ),
    "mid_century": StylePrior(
        "mid_century", ("teak", "wool", "brass", "walnut"),
        ("#E4D9C4", "#9C6B3F", "#3F5D52"), "warm",
        ("mid century", "mid-century", "midcentury", "retro", "50s", "60s"),
    ),
    "unknown": StylePrior("unknown"),
}

STYLE_SYNONYMS: Dict[str, str] = {
    synonym: name
    for name, prior in STYLES.items()
    for synonym in prior.synonyms
}


def normalise_style(raw: str) -> str:
    """Map free text to a catalog style, or ``"unknown"``.

    Deliberately lenient, like the other normalisers: model prose such as
    "warm mid-century modern living space" should resolve rather than be
    thrown away. Longest synonym first so "mid-century modern" does not
    resolve to plain "modern".
    """
    if not raw:
        return "unknown"
    text = " ".join(str(raw).strip().lower().replace("_", " ").split())

    if text.replace(" ", "_") in STYLES:
        return text.replace(" ", "_")
    if text in STYLE_SYNONYMS:
        return STYLE_SYNONYMS[text]

    for key in sorted(STYLE_SYNONYMS, key=len, reverse=True):
        if key in text:
            return STYLE_SYNONYMS[key]
    for key in sorted(STYLES, key=len, reverse=True):
        if key != "unknown" and key.replace("_", " ") in text:
            return key
    return "unknown"


def get_style(name: str) -> StylePrior:
    return STYLES.get(name, STYLES["unknown"])


#: Wall/ceiling-mounted categories that may ALSO legitimately rest on
#: furniture. A television is the obvious case: wall-mounted in one room and
#: standing on a media unit in the next, and the image tells us which. For
#: every other structurally-fixed category the catalog prior wins, because a
#: split AC unit is never actually sitting on the kitchen counter beneath it.
SURFACE_CAPABLE = frozenset({"tv", "monitor", "photo_frame", "clock"})


def support_is_fixed(category: str) -> bool:
    """True when the catalog's mounting prior should override the model."""
    prior = OBJECT_CATALOG.get(category)
    if prior is None:
        return False
    return prior.support in ("wall", "ceiling") and category not in SURFACE_CAPABLE


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LightPrior:
    """Photometric and mounting defaults for a luminaire type."""

    kind: str
    #: Blender light datablock type.
    blender_type: str  # POINT | SPOT | AREA | SUN
    mounting: str      # ceiling | wall | floor | table
    #: Nominal power in watts before room-size scaling.
    power_w: float
    #: Emitter size in metres.
    size: float
    #: Typical correlated colour temperature in kelvin.
    cct_k: float
    #: Height above floor when the model gives no better estimate.
    default_height: float = 2.7
    #: Whether the fixture also needs visible geometry building.
    has_fixture_geometry: bool = True


LIGHT_TYPES: Dict[str, LightPrior] = {
    "recessed_light": LightPrior("recessed_light", "SPOT", "ceiling", 18.0, 0.09, 3200, 2.95, False),
    "ceiling_light": LightPrior("ceiling_light", "AREA", "ceiling", 45.0, 0.35, 3200, 2.90),
    "chandelier": LightPrior("chandelier", "POINT", "ceiling", 90.0, 0.45, 2700, 2.35),
    "pendant_light": LightPrior("pendant_light", "POINT", "ceiling", 30.0, 0.22, 2700, 1.90),
    "led_strip": LightPrior("led_strip", "AREA", "ceiling", 22.0, 0.05, 3000, 2.85, False),
    "cove_light": LightPrior("cove_light", "AREA", "ceiling", 26.0, 0.08, 2900, 2.80, False),
    "wall_light": LightPrior("wall_light", "POINT", "wall", 20.0, 0.14, 2700, 1.95),
    "floor_lamp": LightPrior("floor_lamp", "POINT", "floor", 35.0, 0.28, 2700, 1.55),
    "table_lamp": LightPrior("table_lamp", "POINT", "table", 18.0, 0.20, 2700, 0.45),
    "spotlight": LightPrior("spotlight", "SPOT", "ceiling", 24.0, 0.08, 3400, 2.90),
}

LIGHT_SYNONYMS: Dict[str, str] = {
    "downlight": "recessed_light", "down light": "recessed_light", "can light": "recessed_light",
    "recessed spotlight": "recessed_light", "pot light": "recessed_light",
    "ceiling lamp": "ceiling_light", "flush mount": "ceiling_light", "ceiling fixture": "ceiling_light",
    "surface light": "ceiling_light",
    "hanging light": "pendant_light", "pendant": "pendant_light", "drop light": "pendant_light",
    "hanging lamp": "pendant_light",
    "led strip": "led_strip", "strip light": "led_strip", "linear light": "led_strip",
    "light strip": "led_strip", "under cabinet light": "led_strip",
    "cove lighting": "cove_light", "indirect lighting": "cove_light",
    "sconce": "wall_light", "wall sconce": "wall_light", "wall lamp": "wall_light",
    "picture light": "wall_light",
    "standing lamp": "floor_lamp", "tall lamp": "floor_lamp", "arc lamp": "floor_lamp",
    "desk lamp": "table_lamp", "bedside lamp": "table_lamp", "side lamp": "table_lamp",
    "track light": "spotlight", "accent light": "spotlight",
}


def normalise_light_kind(raw: str) -> Optional[str]:
    """Map a free-text luminaire description to a catalog light type."""
    if not raw:
        return None
    text = " ".join(raw.strip().lower().replace("_", " ").split())
    if text.replace(" ", "_") in LIGHT_TYPES:
        return text.replace(" ", "_")
    if text in LIGHT_SYNONYMS:
        return LIGHT_SYNONYMS[text]
    for key in sorted(LIGHT_SYNONYMS, key=len, reverse=True):
        if key in text:
            return LIGHT_SYNONYMS[key]
    for key in sorted(LIGHT_TYPES, key=len, reverse=True):
        if key.replace("_", " ") in text:
            return key
    return None


def get_light_prior(kind: str) -> LightPrior:
    return LIGHT_TYPES.get(kind, LIGHT_TYPES["ceiling_light"])


def kelvin_to_rgb(kelvin: float) -> Tuple[float, float, float]:
    """Approximate a blackbody colour as linear RGB, normalised to max 1.0.

    Uses Tanner Helland's piecewise approximation, which is accurate enough
    for 1000–12000 K and avoids pulling in a colour-science dependency.
    """
    temp = max(1000.0, min(12000.0, kelvin)) / 100.0

    if temp <= 66:
        red = 255.0
    else:
        red = 329.698727446 * ((temp - 60) ** -0.1332047592)

    if temp <= 66:
        green = 99.4708025861 * _safe_log(temp) - 161.1195681661
    else:
        green = 288.1221695283 * ((temp - 60) ** -0.0755148492)

    if temp >= 66:
        blue = 255.0
    elif temp <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * _safe_log(temp - 10) - 305.0447927307

    channels = [max(0.0, min(255.0, c)) / 255.0 for c in (red, green, blue)]
    peak = max(channels) or 1.0
    return (channels[0] / peak, channels[1] / peak, channels[2] / peak)


def _safe_log(value: float) -> float:
    return math.log(value) if value > 0 else 0.0


# ---------------------------------------------------------------------------
# Room types & relationships
# ---------------------------------------------------------------------------

ROOM_TYPES = (
    "living_room", "bedroom", "kitchen", "dining_room", "bathroom",
    "office", "hallway", "studio", "balcony", "unknown",
)

#: Predicates the placement solver understands. Anything else is stored but
#: not enforced (and reported in diagnostics).
ENFORCED_PREDICATES = (
    "faces",            # subject rotates to look at object
    "on_top_of",        # subject rests on object's surface
    "beside",           # subject sits adjacent to object, along its width
    "surrounds",        # subjects distribute evenly around object
    "centered_under",   # subject centres on object's footprint
    "against_wall",     # subject backs onto the named wall
    "mounted_on",       # subject fixes to the named wall
    "under",            # subject sits below object
    "above",            # subject sits above object
)

#: Category pairs that imply a relationship when the model does not state one.
#: Used only as a fallback, and always recorded with reduced confidence.
IMPLIED_RELATIONSHIPS: Tuple[Tuple[str, str, str], ...] = (
    ("sofa", "faces", "tv_unit"),
    ("sofa", "faces", "tv"),
    ("sectional", "faces", "tv_unit"),
    ("sectional", "faces", "tv"),
    ("armchair", "faces", "coffee_table"),
    ("dining_chair", "surrounds", "dining_table"),
    ("chair", "surrounds", "dining_table"),
    ("bedside_table", "beside", "bed"),
    ("table_lamp", "on_top_of", "bedside_table"),
    ("office_chair", "faces", "study_table"),
    ("rug", "centered_under", "coffee_table"),
    ("carpet", "centered_under", "coffee_table"),
    ("coffee_table", "faces", "sofa"),
    ("tv", "mounted_on", "wall"),
    ("cushion", "on_top_of", "sofa"),
    ("pillow", "on_top_of", "bed"),
    ("monitor", "on_top_of", "study_table"),
    ("laptop", "on_top_of", "study_table"),
    ("flower_vase", "on_top_of", "coffee_table"),
    ("books", "on_top_of", "coffee_table"),
    ("microwave", "on_top_of", "kitchen_counter"),
)

#: Objects that strongly suggest a room type, used to corroborate the model's
#: own room classification.
ROOM_TYPE_EVIDENCE: Dict[str, Tuple[str, ...]] = {
    "living_room": ("sofa", "sectional", "coffee_table", "tv_unit", "tv", "armchair"),
    "bedroom": ("bed", "bedside_table", "wardrobe"),
    "kitchen": ("kitchen_counter", "kitchen_island", "refrigerator", "microwave", "oven"),
    "dining_room": ("dining_table", "dining_chair"),
    "office": ("study_table", "office_chair", "monitor", "bookshelf"),
}


def infer_room_type(categories: Iterable[str]) -> Tuple[str, float]:
    """Score room types from the objects present.

    Counts **distinct** marker categories rather than instances. Counting
    instances lets one dining table with four chairs (5 hits) outvote a
    sectional, coffee table, TV unit and TV (4 hits), and misclassify an
    open-plan living room as a dining room.

    Returns ``(room_type, confidence)``; confidence is the winning share of
    matched evidence, so a tie or an empty room yields low confidence.
    """
    present = set(categories)
    if not present:
        return "unknown", 0.0

    scores: Dict[str, int] = {}
    for room_type, markers in ROOM_TYPE_EVIDENCE.items():
        scores[room_type] = len(present & set(markers))

    total = sum(scores.values())
    if total == 0:
        return "unknown", 0.0

    best = max(sorted(scores), key=lambda k: scores[k])
    return best, round(scores[best] / total, 3)
