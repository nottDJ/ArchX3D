"""
ArchX3D — Reference image classification
========================================
Decides what each uploaded image *is*, and therefore how it should be analysed.

Two signals, combined
---------------------
1. **Local heuristics** (this module, no network). Cheap, deterministic, and
   genuinely reliable for exactly one thing: telling line-art apart from
   photographic content. A CAD export or wireframe has near-zero saturation, a
   dominant flat background and high edge density — a signature no photograph
   or render produces. That single distinction carries most of the routing
   value, because CAD drawings must *not* contribute colours, materials or
   furniture.

2. **The model's own classification**, returned in the same analysis call it
   already makes. No extra request: the prompt asks it to classify first and
   then fill only the sections appropriate to that class. See
   ``prompts.build_observation_prompt``.

An honest note on "AI-generated"
--------------------------------
The brief asks for AI-generated images to be detected. Reliably distinguishing
an AI-generated interior from a conventionally-rendered CG interior is not
something this pipeline can honestly claim — modern renderers and modern
generators produce overlapping artefacts, and detector accuracy on unseen
generators is poor.

What *is* reliable, and what actually matters for the requirement, is
**photograph vs. synthetic render**. Both AI-generated images and CG renders
are synthetic, and both warrant the same treatment: trust them for layout,
palette, lighting design and decoration; never for metric geometry. So this
module classifies `photo | render | drawing` and exposes a `geometry_trust`
weight. Geometry comes from the DXF in every case, so a misclassification here
degrades confidence weighting — it can never corrupt the building's shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: How each image class is analysed.
#:   full      — objects, finishes, lighting, relationships (interior views)
#:   layout    — furniture layout and positions from a top-down plan; no
#:               perspective-derived materials or lighting
#:   geometry  — rooms, walls and openings only; never colours or furniture
#:   skip      — contributes nothing to an interior reconstruction
ANALYSIS_MODES = ("full", "layout", "geometry", "skip")

#: Recognised image classes and the mode each one routes to.
IMAGE_CLASSES: Dict[str, str] = {
    "interior_photograph": "full",
    "interior_render": "full",
    "room_render": "full",
    "furnished_floorplan": "layout",
    "top_down_layout": "layout",
    "cad_drawing": "geometry",
    "wireframe": "geometry",
    "architectural_elevation": "geometry",
    "exterior_render": "skip",
    "site_plan": "skip",
    "unknown": "full",
}

#: How much each class may influence metric geometry, in ``[0, 1]``.
#: Nothing reaches 1.0: the DXF is the only geometric authority.
GEOMETRY_TRUST: Dict[str, float] = {
    "interior_photograph": 0.85,
    "interior_render": 0.55,
    "room_render": 0.55,
    "furnished_floorplan": 0.70,
    "top_down_layout": 0.70,
    "cad_drawing": 0.95,
    "wireframe": 0.95,
    "architectural_elevation": 0.60,
    "exterior_render": 0.0,
    "site_plan": 0.0,
    "unknown": 0.50,
}

#: Classes that must never contribute finishes, furniture or lighting.
GEOMETRY_ONLY_CLASSES = frozenset({"cad_drawing", "wireframe", "architectural_elevation"})

#: Minimum share of high-gradient pixels for an image to contain line work.
#: Below this the image is flat or blurred, not a drawing.
MIN_LINE_ART_EDGE_DENSITY = 0.02

#: Minimum share of pixels in one luminance bucket. A drawing has a dominant
#: paper/screen background; noise and photographs do not.
MIN_LINE_ART_BACKGROUND = 0.35


@dataclass
class ImageProfile:
    """What one uploaded image is, and how to treat it."""

    image_id: str
    path: str
    image_class: str = "unknown"
    analysis_mode: str = "full"
    #: photo | render | drawing
    medium: str = "render"
    #: True for anything synthetic (CG or AI-generated) — see module docstring.
    synthetic: bool = True
    #: Room the model says this image shows, if it is a single-room view.
    room_type_hint: str = "unknown"
    #: 0-1; how far this image may influence metric geometry.
    geometry_trust: float = 0.5
    confidence: float = 0.0
    #: Where the classification came from, for the review UI.
    source: str = "heuristic"
    notes: List[str] = field(default_factory=list)
    #: Raw local measurements, retained for debugging.
    metrics: Dict[str, float] = field(default_factory=dict)

    @property
    def contributes_appearance(self) -> bool:
        """May this image supply colours, materials, furniture and lighting?"""
        return self.image_class not in GEOMETRY_ONLY_CLASSES and self.analysis_mode != "skip"

    def to_dict(self) -> Dict:
        return {
            "image_id": self.image_id,
            "file": self.path.replace("\\", "/").rsplit("/", 1)[-1],
            "image_class": self.image_class,
            "analysis_mode": self.analysis_mode,
            "medium": self.medium,
            "synthetic": self.synthetic,
            "room_type_hint": self.room_type_hint,
            "geometry_trust": round(self.geometry_trust, 3),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "contributes_appearance": self.contributes_appearance,
            "notes": list(self.notes),
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
        }


# ---------------------------------------------------------------------------
# Local heuristics
# ---------------------------------------------------------------------------


def profile_image(image_id: str, path: str) -> ImageProfile:
    """Classify an image from its pixels alone, before any model call.

    The result is a *prior*. The model's own classification refines it in
    `merge_model_classification`, except for the line-art signal, which is
    strong enough locally to be authoritative.
    """
    profile = ImageProfile(image_id=image_id, path=path)
    metrics = _measure(path)

    if not metrics:
        profile.notes.append("could not read image; defaulting to full analysis")
        profile.confidence = 0.0
        profile.geometry_trust = GEOMETRY_TRUST["unknown"]
        return profile

    profile.metrics = metrics

    saturation = metrics["mean_saturation"]
    flat_share = metrics["flat_background_share"]
    edge_density = metrics["edge_density"]
    palette = metrics["palette_diversity"]

    # Line art is *strokes on a background*. Both halves of that are necessary
    # conditions, not merely contributing signals:
    #   - without strokes, a blank or blurred image scores as a drawing on its
    #     low saturation alone;
    #   - without a dominant background, desaturated noise or a monochrome
    #     photograph does the same.
    # Misfiring here is expensive: everything in the image would be discarded
    # as "appearance from a technical drawing".
    has_strokes = edge_density >= MIN_LINE_ART_EDGE_DENSITY
    has_background = flat_share >= MIN_LINE_ART_BACKGROUND

    line_art_score = 0.0
    if has_strokes and has_background:
        if saturation < 0.14:
            line_art_score += 0.35
        if flat_share > 0.45:
            line_art_score += 0.30
        if edge_density > 0.045:
            line_art_score += 0.20
        if palette < 0.22:
            line_art_score += 0.15

    if line_art_score >= 0.70:
        profile.image_class = "cad_drawing"
        profile.medium = "drawing"
        profile.synthetic = True
        profile.confidence = round(min(0.95, 0.55 + line_art_score / 2.5), 3)
        profile.notes.append(
            f"line-art signature (saturation {saturation:.2f}, "
            f"flat background {flat_share:.0%}, edges {edge_density:.3f})"
        )
    else:
        # Everything else is left for the model; a local photo/render call
        # would be guesswork, and the model is markedly better at it.
        profile.image_class = "unknown"
        profile.confidence = 0.25
        profile.notes.append("photographic content; class deferred to the model")

    profile.analysis_mode = IMAGE_CLASSES.get(profile.image_class, "full")
    profile.geometry_trust = GEOMETRY_TRUST.get(profile.image_class, 0.5)
    return profile


def _measure(path: str) -> Dict[str, float]:
    """Compute the cheap statistics the heuristics run on."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return {}

    try:
        with Image.open(path) as handle:
            # A small thumbnail is plenty: every statistic here is global.
            handle = handle.convert("RGB")
            handle.thumbnail((256, 256))
            rgb = np.asarray(handle, dtype=np.float32) / 255.0
    except Exception:
        return {}

    if rgb.size == 0:
        return {}

    maximum = rgb.max(axis=2)
    minimum = rgb.min(axis=2)
    saturation = np.where(maximum > 1e-6, (maximum - minimum) / np.maximum(maximum, 1e-6), 0.0)

    grey = rgb.mean(axis=2)

    # Share of pixels sitting in the single most common luminance bucket —
    # a drawing's paper or screen background dominates; a photo's does not.
    histogram, _ = np.histogram(grey, bins=32, range=(0.0, 1.0))
    flat_background_share = float(histogram.max() / max(1, grey.size))

    # Sobel-free edge estimate: mean absolute gradient, thresholded.
    gy = np.abs(np.diff(grey, axis=0)).mean() if grey.shape[0] > 1 else 0.0
    gx = np.abs(np.diff(grey, axis=1)).mean() if grey.shape[1] > 1 else 0.0
    gradient = np.zeros_like(grey)
    gradient[:-1, :] += np.abs(np.diff(grey, axis=0))
    gradient[:, :-1] += np.abs(np.diff(grey, axis=1))
    edge_density = float((gradient > 0.18).mean())

    # Palette diversity: occupancy of a coarse RGB cube.
    quantised = (rgb * 7).astype(np.int32)
    codes = quantised[..., 0] * 64 + quantised[..., 1] * 8 + quantised[..., 2]
    palette_diversity = float(len(np.unique(codes)) / 512.0)

    return {
        "mean_saturation": float(saturation.mean()),
        "flat_background_share": flat_background_share,
        "edge_density": edge_density,
        "palette_diversity": palette_diversity,
        "mean_gradient": float((gx + gy) / 2.0),
    }


# ---------------------------------------------------------------------------
# Merging the model's classification
# ---------------------------------------------------------------------------


def merge_model_classification(profile: ImageProfile, payload: Dict) -> ImageProfile:
    """Refine a local profile with the classification the model returned.

    The local line-art signal wins where it fired — it is near-deterministic,
    and mistakenly treating a CAD export as a photograph would inject invented
    wall colours into the scene. Everything else defers to the model.
    """
    block = payload.get("image_class") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        profile.notes.append("model returned no classification; keeping local profile")
        if profile.image_class == "unknown":
            profile.analysis_mode = "full"
        return profile

    declared = str(block.get("type", "")).strip().lower().replace(" ", "_").replace("-", "_")
    declared = _canonical_class(declared)
    medium = str(block.get("medium", "")).strip().lower()
    model_confidence = _clamp01(_to_float(block.get("confidence")))

    if profile.image_class in GEOMETRY_ONLY_CLASSES:
        # Local detector fired. Record disagreement but do not act on it.
        if declared and declared not in GEOMETRY_ONLY_CLASSES:
            profile.notes.append(
                f"model called this '{declared}'; local line-art signal kept precedence"
            )
        profile.source = "heuristic+model"
        return profile

    if declared:
        profile.image_class = declared
        profile.confidence = model_confidence
        profile.source = "model"
    else:
        profile.image_class = "unknown"

    if medium in ("photo", "photograph"):
        profile.medium = "photo"
        profile.synthetic = False
    elif medium in ("drawing", "diagram", "line_art"):
        profile.medium = "drawing"
        profile.synthetic = True
    else:
        profile.medium = "render"
        profile.synthetic = True

    hint = str(block.get("room_type", "")).strip().lower().replace(" ", "_")
    if hint and hint != "unknown":
        profile.room_type_hint = hint

    profile.analysis_mode = IMAGE_CLASSES.get(profile.image_class, "full")
    profile.geometry_trust = GEOMETRY_TRUST.get(profile.image_class, 0.5)

    # A photograph is firmer evidence of real geometry than any render.
    if profile.medium == "photo":
        profile.geometry_trust = min(0.9, profile.geometry_trust + 0.2)
    profile.notes.append(f"model classified as {profile.image_class} ({medium or 'medium unknown'})")

    return profile


def _canonical_class(value: str) -> str:
    """Map assorted spellings onto the recognised class vocabulary."""
    if not value:
        return ""
    if value in IMAGE_CLASSES:
        return value

    aliases = {
        "photograph": "interior_photograph",
        "photo": "interior_photograph",
        "interior_photo": "interior_photograph",
        "render": "interior_render",
        "rendering": "interior_render",
        "architectural_render": "interior_render",
        "3d_render": "interior_render",
        "living_room_render": "room_render",
        "bedroom_render": "room_render",
        "kitchen_render": "room_render",
        "bathroom_render": "room_render",
        "floor_plan": "furnished_floorplan",
        "floorplan": "furnished_floorplan",
        "furnished_floor_plan": "furnished_floorplan",
        "top_view": "top_down_layout",
        "top_down": "top_down_layout",
        "plan_view": "top_down_layout",
        "isometric": "top_down_layout",
        "cad": "cad_drawing",
        "cad_screenshot": "cad_drawing",
        "blueprint": "cad_drawing",
        "line_drawing": "wireframe",
        "elevation": "architectural_elevation",
        "section": "architectural_elevation",
        "exterior": "exterior_render",
        "facade": "exterior_render",
    }
    if value in aliases:
        return aliases[value]

    for key, mapped in aliases.items():
        if key in value:
            return mapped
    return ""


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarise(profiles: List[ImageProfile]) -> Dict:
    """Aggregate counts for the run report and the review UI."""
    by_class: Dict[str, int] = {}
    by_mode: Dict[str, int] = {}
    for profile in profiles:
        by_class[profile.image_class] = by_class.get(profile.image_class, 0) + 1
        by_mode[profile.analysis_mode] = by_mode.get(profile.analysis_mode, 0) + 1

    return {
        "total": len(profiles),
        "by_class": by_class,
        "by_mode": by_mode,
        "appearance_sources": sum(1 for p in profiles if p.contributes_appearance),
        "geometry_only": sum(1 for p in profiles if p.image_class in GEOMETRY_ONLY_CLASSES),
        "skipped": sum(1 for p in profiles if p.analysis_mode == "skip"),
        "photographs": sum(1 for p in profiles if p.medium == "photo"),
        "synthetic": sum(1 for p in profiles if p.synthetic),
    }
