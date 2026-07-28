"""
ArchX3D — Asset matching
========================
Chooses which procedural model to build for each detected object.

The old pipeline emitted furniture *names* ("Scandinavian Sofa 3-Seater") that
nothing consumed. This module replaces that with a real selection step: each
category offers several procedural variants, and the best one is chosen by
comparing the detection's proportions, style, material and colour against each
variant's signature.

Variants are procedural rather than a library of imported meshes because the
project has no asset pack, and generating geometry keeps the GLB small and the
pipeline dependency-free. Every variant is parameterised, so the chosen model is
also *proportioned* to the detection rather than uniformly scaled — a low wide
sectional and a compact two-seater resolve to different geometry, not the same
box at different scales.

Stdlib only — Blender imports this to know what to build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import catalog

#: Relative weights of the matching criteria. Proportion dominates because it
#: is measured from the image, whereas style is inferred prose.
WEIGHT_PROPORTION = 0.45
WEIGHT_STYLE = 0.25
WEIGHT_MATERIAL = 0.20
WEIGHT_TONE = 0.10


@dataclass(frozen=True)
class AssetVariant:
    """One procedural model a category can resolve to."""

    key: str
    category: str
    #: Builder function in ``blender_furniture`` that constructs this variant.
    builder: str
    #: Characteristic (width : depth : height) proportions, normalised on height.
    signature: Tuple[float, float, float]
    #: Style words this variant reads as.
    styles: Tuple[str, ...] = ()
    #: Materials this variant suits.
    materials: Tuple[str, ...] = ()
    #: light | mid | dark — the tonal range the variant is designed around.
    tone: str = "any"
    #: Extra parameters handed to the builder.
    params: Dict[str, float] = field(default_factory=dict)


def _v(key, category, builder, signature, **kwargs) -> AssetVariant:
    return AssetVariant(key=key, category=category, builder=builder, signature=signature, **kwargs)


# ---------------------------------------------------------------------------
# Variant registry
# ---------------------------------------------------------------------------

ASSET_VARIANTS: Tuple[AssetVariant, ...] = (
    # ---- Seating ---------------------------------------------------------
    _v("sofa_low_modern", "sofa", "build_sofa", (2.47, 1.06, 1.0),
       styles=("modern", "minimal", "scandinavian", "contemporary"),
       materials=("fabric",), params={"arm_height": 0.55, "back_height": 0.78, "leg_height": 0.12}),
    _v("sofa_deep_lounge", "sofa", "build_sofa", (2.20, 1.20, 1.0),
       styles=("contemporary", "luxury", "modern"),
       materials=("fabric", "leather"), params={"arm_height": 0.62, "back_height": 0.85, "leg_height": 0.06}),
    _v("sofa_classic_arms", "sofa", "build_sofa", (2.35, 1.00, 1.0),
       styles=("traditional", "classic", "vintage"),
       materials=("fabric", "leather"), params={"arm_height": 0.70, "back_height": 0.95, "leg_height": 0.16}),

    _v("sectional_l_wide", "sectional", "build_sectional", (3.29, 2.12, 1.0),
       styles=("modern", "contemporary", "minimal"),
       materials=("fabric",), params={"chaise_ratio": 0.62, "back_height": 0.80}),
    _v("sectional_compact", "sectional", "build_sectional", (2.70, 1.65, 1.0),
       styles=("scandinavian", "minimal", "modern"),
       materials=("fabric",), params={"chaise_ratio": 0.52, "back_height": 0.74}),

    _v("armchair_tub", "armchair", "build_armchair", (0.94, 0.94, 1.0),
       styles=("modern", "contemporary"), materials=("fabric", "leather")),
    _v("armchair_wing", "armchair", "build_armchair", (0.85, 0.90, 1.0),
       styles=("traditional", "classic"), materials=("fabric",), params={"back_height": 1.15}),

    _v("chair_dining_wood", "dining_chair", "build_chair", (0.52, 0.57, 1.0),
       styles=("scandinavian", "modern", "minimal"), materials=("wood", "wood_light")),
    _v("chair_upholstered", "dining_chair", "build_chair", (0.55, 0.60, 1.0),
       styles=("contemporary", "luxury", "traditional"), materials=("fabric", "leather"),
       params={"padded": 1.0}),
    _v("chair_generic", "chair", "build_chair", (0.55, 0.61, 1.0)),
    _v("office_chair_task", "office_chair", "build_office_chair", (0.56, 0.56, 1.0)),
    _v("stool_round", "stool", "build_stool", (0.56, 0.56, 1.0)),
    _v("bench_plain", "bench", "build_box_furniture", (3.11, 0.89, 1.0)),

    # ---- Tables ----------------------------------------------------------
    _v("table_rect_tapered", "coffee_table", "build_table", (2.86, 1.43, 1.0),
       styles=("scandinavian", "modern", "minimal"), materials=("wood", "wood_light"),
       params={"leg_style": 0.0, "top_thickness": 0.05}),
    _v("table_round_pedestal", "coffee_table", "build_table", (2.20, 2.20, 1.0),
       styles=("contemporary", "modern"), materials=("marble", "glass", "wood"),
       params={"round": 1.0, "top_thickness": 0.04}),
    _v("table_low_slab", "coffee_table", "build_table", (3.10, 1.55, 1.0),
       styles=("industrial", "modern"), materials=("concrete", "wood_dark", "marble"),
       params={"leg_style": 1.0, "top_thickness": 0.09}),

    _v("dining_rect_legs", "dining_table", "build_table", (2.13, 1.20, 1.0),
       styles=("modern", "scandinavian", "minimal"), materials=("wood", "wood_light")),
    _v("dining_round", "dining_table", "build_table", (1.60, 1.60, 1.0),
       styles=("contemporary", "traditional"), materials=("wood", "marble"),
       params={"round": 1.0}),

    _v("side_table_simple", "side_table", "build_table", (0.91, 0.91, 1.0)),
    _v("console_slim", "console_table", "build_table", (1.50, 0.44, 1.0)),
    _v("desk_rect", "study_table", "build_table", (1.87, 0.93, 1.0),
       styles=("modern", "minimal"), materials=("wood", "wood_light", "laminate")),

    # ---- Storage ---------------------------------------------------------
    _v("tv_unit_low_wide", "tv_unit", "build_tv_unit", (3.60, 0.90, 1.0),
       styles=("modern", "minimal", "scandinavian"), materials=("wood", "wood_light"),
       params={"open_shelf": 1.0}),
    _v("tv_unit_cabinet", "tv_unit", "build_tv_unit", (3.00, 0.85, 1.0),
       styles=("contemporary", "traditional"), materials=("wood_dark", "wood"),
       params={"open_shelf": 0.0}),

    _v("cabinet_doors", "cabinet", "build_cabinet", (1.11, 0.50, 1.0)),
    _v("sideboard_long", "sideboard", "build_cabinet", (2.00, 0.56, 1.0)),
    _v("bedside_simple", "bedside_table", "build_cabinet", (0.82, 0.73, 1.0)),
    _v("wardrobe_tall", "wardrobe", "build_cabinet", (0.82, 0.27, 1.0)),
    _v("bookshelf_open", "bookshelf", "build_bookshelf", (0.50, 0.19, 1.0)),
    _v("shelf_floating", "shelves", "build_shelf", (3.33, 0.93, 1.0)),
    _v("kitchen_island_block", "kitchen_island", "build_cabinet", (2.00, 1.00, 1.0)),
    _v("kitchen_counter_run", "kitchen_counter", "build_cabinet", (2.67, 0.67, 1.0)),

    # ---- Bedroom ---------------------------------------------------------
    _v("bed_platform_low", "bed", "build_bed", (2.91, 3.64, 1.0),
       styles=("modern", "minimal", "scandinavian"), params={"headboard": 0.75}),
    _v("bed_upholstered", "bed", "build_bed", (2.70, 3.40, 1.0),
       styles=("contemporary", "luxury"), materials=("fabric",), params={"headboard": 1.15}),

    # ---- Appliances ------------------------------------------------------
    _v("fridge_tall", "refrigerator", "build_box_furniture", (0.39, 0.39, 1.0), materials=("metal",)),
    _v("microwave_box", "microwave", "build_box_furniture", (1.67, 1.33, 1.0), materials=("metal",)),
    _v("washer_box", "washing_machine", "build_box_furniture", (0.71, 0.71, 1.0), materials=("metal",)),
    _v("oven_box", "oven", "build_box_furniture", (1.00, 0.97, 1.0), materials=("metal",)),
    _v("ac_wall_split", "ac_unit", "build_box_furniture", (3.00, 0.73, 1.0), materials=("plastic",)),

    # ---- Screens ---------------------------------------------------------
    _v("tv_panel", "tv", "build_screen", (1.71, 0.10, 1.0)),
    _v("monitor_panel", "monitor", "build_screen", (1.33, 0.44, 1.0)),
    _v("laptop_open", "laptop", "build_box_furniture", (1.59, 1.14, 1.0)),
    _v("speaker_tower", "speaker", "build_box_furniture", (0.25, 0.25, 1.0)),

    # ---- Soft furnishings ------------------------------------------------
    _v("rug_rect", "rug", "build_rug", (120.0, 85.0, 1.0)),
    _v("carpet_rect", "carpet", "build_rug", (150.0, 110.0, 1.0)),
    _v("cushion_square", "cushion", "build_cushion", (3.00, 3.00, 1.0)),
    _v("pillow_long", "pillow", "build_cushion", (3.93, 2.50, 1.0)),
    _v("curtain_pair", "curtains", "build_curtain", (0.67, 0.05, 1.0)),
    _v("blinds_flat", "blinds", "build_blinds", (0.86, 0.04, 1.0)),

    # ---- Decor -----------------------------------------------------------
    _v("painting_frame", "painting", "build_frame", (0.80, 0.05, 1.0)),
    _v("mirror_frame", "mirror", "build_frame", (0.58, 0.04, 1.0), materials=("glass",)),
    _v("photo_frame_small", "photo_frame", "build_frame", (0.80, 0.16, 1.0)),
    _v("clock_round", "clock", "build_clock", (1.00, 0.17, 1.0)),
    _v("plant_tall", "plant", "build_plant", (0.50, 0.50, 1.0), params={"leafy": 1.0}),
    _v("vase_round", "flower_vase", "build_vase", (0.57, 0.57, 1.0), materials=("ceramic", "glass")),
    _v("books_stack", "books", "build_box_furniture", (1.25, 0.80, 1.0)),
    _v("bowl_round", "bowl", "build_vase", (2.50, 2.50, 1.0), materials=("ceramic",)),
    _v("ceiling_fan_blades", "ceiling_fan", "build_ceiling_fan", (3.43, 3.43, 1.0)),
)


#: Indexed once at import; the registry is static.
_BY_CATEGORY: Dict[str, List[AssetVariant]] = {}
for _variant in ASSET_VARIANTS:
    _BY_CATEGORY.setdefault(_variant.category, []).append(_variant)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class AssetMatch:
    variant: Optional[AssetVariant]
    score: float
    reasons: List[str] = field(default_factory=list)


def match_asset(
    category: str,
    dimensions: Tuple[float, float, float],
    style: str,
    material: str,
    color_hex: str,
    label: str = "",
) -> AssetMatch:
    """Pick the best procedural variant for one detection.

    Returns a match with ``variant=None`` when the category has no registered
    variants, which the caller renders as a generic proportioned box rather
    than skipping the object.
    """
    variants = _BY_CATEGORY.get(category)
    if not variants:
        return AssetMatch(variant=None, score=0.0, reasons=["no variant registered"])

    if len(variants) == 1:
        return AssetMatch(variant=variants[0], score=1.0, reasons=["only variant"])

    tone = _tone_of(color_hex)
    text = f"{style} {label}".lower()

    best, best_score, best_reasons = variants[0], -1.0, []

    for variant in variants:
        reasons: List[str] = []

        proportion = _proportion_score(dimensions, variant.signature)
        if proportion > 0.8:
            reasons.append(f"proportions match ({proportion:.2f})")

        style_score = _style_score(text, variant.styles)
        if style_score > 0.6 and variant.styles:
            reasons.append(f"style '{style}' suits {variant.key}")

        material_score = _material_score(material, variant.materials)
        if material_score > 0.8 and variant.materials:
            reasons.append(f"material '{material}' suits {variant.key}")

        tone_score = 1.0 if variant.tone in ("any", tone) else 0.55

        total = (
            WEIGHT_PROPORTION * proportion
            + WEIGHT_STYLE * style_score
            + WEIGHT_MATERIAL * material_score
            + WEIGHT_TONE * tone_score
        )

        if total > best_score:
            best, best_score, best_reasons = variant, total, reasons

    return AssetMatch(variant=best, score=round(best_score, 4), reasons=best_reasons)


def _proportion_score(dimensions: Tuple[float, float, float], signature: Tuple[float, float, float]) -> float:
    """Compare height-normalised proportions; 1.0 is an exact match."""
    width, depth, height = dimensions
    if height <= 1e-6:
        return 0.5

    observed = (width / height, depth / height)
    expected = (signature[0], signature[1])

    error = 0.0
    for obs, exp in zip(observed, expected):
        if exp <= 1e-6:
            continue
        # Symmetric relative error, so 2x and 0.5x score alike.
        error += abs(obs - exp) / max(obs, exp)

    return max(0.0, 1.0 - error / 2.0)


def _style_score(text: str, styles: Sequence[str]) -> float:
    if not styles:
        return 0.6  # Style-agnostic variants are never penalised.
    return 1.0 if any(word in text for word in styles) else 0.35


def _material_score(material: str, materials: Sequence[str]) -> float:
    """How well an observed material suits a variant's declared materials.

    Compared at *family* level. Variants declare broad materials ("wood",
    "fabric") while observations now resolve to species ("walnut", "velvet"),
    and a literal comparison would score every species as a mismatch against
    the very family it belongs to.
    """
    if not materials:
        return 0.6
    if material in materials:
        return 1.0

    family = catalog.material_family(material)
    expected = {catalog.material_family(m) for m in materials}
    if family in expected:
        return 0.95

    # Timber-alikes remain near-interchangeable in tone terms.
    wood = {"wood", "laminate"}
    if family in wood and expected & wood:
        return 0.85
    return 0.35


def _tone_of(color_hex: str) -> str:
    text = (color_hex or "#BFBFBF").lstrip("#")
    if len(text) != 6:
        return "mid"
    try:
        r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "mid"
    # Rec. 709 luma.
    luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    if luma > 0.66:
        return "light"
    if luma < 0.30:
        return "dark"
    return "mid"


def get_variant(key: str) -> Optional[AssetVariant]:
    """Look a variant up by key — used by the Blender generator."""
    for variant in ASSET_VARIANTS:
        if variant.key == key:
            return variant
    return None


def variants_for(category: str) -> List[AssetVariant]:
    """Every variant a category can resolve to.

    Used by the review step's asset browser to offer the alternatives to a
    match, and to validate a user's choice against the category they are
    actually editing.
    """
    return list(_BY_CATEGORY.get(category, ()))


def builder_for(key: str) -> str:
    """Builder function name for an asset key, with a safe generic default."""
    variant = get_variant(key)
    return variant.builder if variant else "build_box_furniture"


#: Below this match score the chosen variant is a compromise rather than a
#: likeness, and the object is flagged so the review step can say so. Silently
#: building an obviously different model is what this threshold exists to stop.
POOR_MATCH_THRESHOLD = 0.62


def assign_assets(objects, style: str) -> Dict[str, int]:
    """Attach the best variant to every object in place.

    Returns a histogram of chosen builders, for the run report.
    """
    histogram: Dict[str, int] = {}

    for obj in objects:
        match = match_asset(
            category=obj.category,
            dimensions=(obj.dimensions.width, obj.dimensions.depth, obj.dimensions.height),
            style=style,
            material=obj.material,
            color_hex=obj.color_hex,
            label=obj.label,
        )
        if match.variant is not None:
            obj.asset = match.variant.key
            obj.asset_score = match.score
            for reason in match.reasons[:2]:
                obj.flags.append(f"asset: {reason}")
            if match.score < POOR_MATCH_THRESHOLD:
                # Recorded as a flag rather than a rejection: a 55% likeness is
                # still far better than a grey box, and the user is the one who
                # should decide whether it is good enough.
                obj.flags.append(
                    f"asset: closest available match, {match.score:.0%} similar"
                )
        else:
            # Fall back to a proportioned box keyed on the catalog family.
            prior = catalog.get_prior(obj.category)
            obj.asset = f"generic_{prior.asset_family}" if prior else "generic_box"
            obj.asset_score = 0.0
            obj.flags.append("asset: generic fallback — no variant for this category")

        builder = builder_for(obj.asset)
        histogram[builder] = histogram.get(builder, 0) + 1

    return histogram


def match_quality(objects) -> Dict[str, Any]:
    """Summarise how well the asset library covered this scene.

    Reported so a systematically poor category is visible as a gap in the
    library rather than as an unexplained wrongness in the render.
    """
    scored = [o for o in objects if o.asset_score > 0]
    poor = [o for o in objects if 0 < o.asset_score < POOR_MATCH_THRESHOLD]
    generic = [o for o in objects if o.asset.startswith("generic_")]

    by_category: Dict[str, List[float]] = {}
    for obj in scored:
        by_category.setdefault(obj.category, []).append(obj.asset_score)

    return {
        "matched": len(scored),
        "mean_score": round(sum(o.asset_score for o in scored) / len(scored), 3)
        if scored else 0.0,
        "poor_matches": [
            {
                "id": o.id,
                "category": o.category,
                "asset": o.asset,
                "score": round(o.asset_score, 3),
            }
            for o in sorted(poor, key=lambda o: o.asset_score)[:20]
        ],
        "no_variant": sorted({o.category for o in generic}),
        "weakest_categories": [
            {"category": name, "mean_score": round(sum(v) / len(v), 3), "count": len(v)}
            for name, v in sorted(
                by_category.items(), key=lambda kv: sum(kv[1]) / len(kv[1])
            )[:5]
        ],
    }
