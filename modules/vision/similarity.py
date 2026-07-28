"""
ArchX3D — Reference-versus-generated similarity
===============================================
An objective measure of how close the reconstruction came to the photographs
it was built from.

Why this module exists
----------------------
"Improve the visual fidelity" is unfalsifiable without a number. Every change
to materials, lighting or asset selection is otherwise argued from screenshots
and impressions, and regressions are invisible. This produces a score per axis
plus *specific* findings — "wall colour too warm", "dining table missing" —
which is what makes a refinement pass possible at all: you cannot fix what you
cannot name.

What is compared, and how
-------------------------
Four axes are measured from pixels, comparing a render taken from the *same
fitted camera* as the photograph (see ``schema.ViewPoint``):

* **colour**    — palette and histogram agreement
* **lighting**  — brightness, contrast and warmth
* **layout**    — where the visual mass sits, on a coarse grid
* **material**  — texture busyness across frequency bands

The fifth axis, **objects**, is *not* measured from pixels. Detecting furniture
in the render would need another model call, and would compare one model's
opinion against another's rather than against the truth. Instead it compares
what the reference images were observed to contain against what the graph
actually built. That difference is exactly recoverable, and it is where the
useful findings live: an object detected but dropped below the confidence floor
is a "missing plant" the user can act on.

Honest degradation
------------------
Pillow and numpy are optional, as elsewhere in this pipeline. Without them the
pixel axes report ``available=False`` rather than a fabricated score, and the
object axis — which needs no image at all — still works.

Two entry points
----------------
``compare(pairs, graph)``
    The original: score a set of (viewpoint, reference, render) triples the
    caller has already paired up. Small, dependency-light, and still what the
    review UI uses to put a number beside a build.

``evaluate(...)``
    The full reconstruction evaluation engine — five axes measured against the
    auxiliary render passes, findings that name the subsystem to change, and
    the four JSON documents. It lives in :mod:`evaluation`; this module is
    where the pipeline expects to find the entry point, so it is re-exported
    here rather than duplicated.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import appearance
from .schema import SceneGraph, SceneObject, ViewPoint

#: Weight of each axis in the overall score. Colour and layout dominate
#: because they are what a person notices first when the two are side by side;
#: material is weighted least because procedural texture can only ever
#: approximate a photograph.
AXIS_WEIGHTS: Dict[str, float] = {
    "colour": 0.26,
    "layout": 0.24,
    "lighting": 0.22,
    "objects": 0.20,
    "material": 0.08,
}

#: Working resolution for pixel comparison. Small on purpose: the question is
#: whether the room reads the same, not whether the pixels match. Comparing at
#: full resolution would score a correct reconstruction badly for having
#: different wood grain.
ANALYSIS_WIDTH = 128
ANALYSIS_HEIGHT = 96

#: Grid used for the layout axis.
LAYOUT_COLUMNS = 8
LAYOUT_ROWS = 6

#: Score below which an axis is called out as a problem.
WEAK_AXIS = 0.70


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One concrete, actionable difference."""

    axis: str
    severity: str  # error | warning | note
    detail: str
    #: What a refinement pass would change to address this: materials |
    #: lighting | assets | decor. Empty when nothing automatic can help.
    remedy: str = ""
    subject: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "severity": self.severity,
            "detail": self.detail,
            "remedy": self.remedy,
            "subject": self.subject,
        }


@dataclass
class AxisScore:
    name: str
    score: float = 0.0
    available: bool = True
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "available": self.available,
            "detail": self.detail,
        }


@dataclass
class ViewComparison:
    """One reference image against its matching render."""

    image_id: str
    reference: str
    rendered: str
    room_id: str = ""
    axes: Dict[str, AxisScore] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Weighted mean over the axes that could actually be measured.

        Renormalised over available axes so a missing render does not silently
        drag the score down as if the reconstruction were bad.
        """
        total = sum(
            AXIS_WEIGHTS.get(name, 0.0)
            for name, axis in self.axes.items()
            if axis.available
        )
        if total <= 0:
            return 0.0
        return sum(
            axis.score * AXIS_WEIGHTS.get(name, 0.0)
            for name, axis in self.axes.items()
            if axis.available
        ) / total

    def to_dict(self) -> Dict[str, Any]:
        return {
            "image_id": self.image_id,
            "reference": self.reference,
            "rendered": self.rendered,
            "room_id": self.room_id,
            "score": round(self.score, 4),
            "axes": {name: axis.to_dict() for name, axis in self.axes.items()},
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class SimilarityReport:
    views: List[ViewComparison] = field(default_factory=list)
    #: Findings that concern the scene as a whole rather than one view.
    findings: List[Finding] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.views:
            return 0.0
        return sum(view.score for view in self.views) / len(self.views)

    def axis_means(self) -> Dict[str, float]:
        totals: Dict[str, List[float]] = {}
        for view in self.views:
            for name, axis in view.axes.items():
                if axis.available:
                    totals.setdefault(name, []).append(axis.score)
        return {
            name: round(sum(values) / len(values), 4)
            for name, values in totals.items()
        }

    def all_findings(self) -> List[Finding]:
        out = list(self.findings)
        for view in self.views:
            out.extend(view.findings)
        return out

    def remedies(self) -> List[str]:
        """Which refinement levers the findings actually call for."""
        order = ["materials", "lighting", "assets", "decor"]
        wanted = {f.remedy for f in self.all_findings() if f.remedy}
        return [remedy for remedy in order if remedy in wanted]

    def to_dict(self) -> Dict[str, Any]:
        findings = self.all_findings()
        return {
            "score": round(self.score, 4),
            "axis_means": self.axis_means(),
            "views": [view.to_dict() for view in self.views],
            "findings": [f.to_dict() for f in findings],
            "remedies": self.remedies(),
            "notes": list(self.notes),
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if not self.views:
            return "no comparable views"
        weak = [name for name, value in self.axis_means().items() if value < WEAK_AXIS]
        text = f"{self.score:.0%} similar across {len(self.views)} view(s)"
        if weak:
            text += f"; weakest: {', '.join(sorted(weak))}"
        return text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate(*args, **kwargs):
    """Run the reconstruction evaluation engine. See :func:`evaluation.evaluate`.

    A thin re-export, deliberately: the engine is large enough to deserve its
    own package, and this module is small enough that importing that package
    eagerly would make every consumer of ``compare`` pay for it. The import is
    therefore deferred to the call.
    """
    import os
    import sys

    modules = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if modules not in sys.path:
        sys.path.insert(0, modules)
    from evaluation import evaluate as _evaluate

    return _evaluate(*args, **kwargs)


def compare(
    graph: SceneGraph,
    pairs: Sequence[Tuple[ViewPoint, str, str]],
) -> SimilarityReport:
    """Score a set of (viewpoint, reference path, rendered path) triples.

    The caller supplies the pairing because it owns the render step; this
    module stays a pure function of images and the graph, which is what makes
    it testable without Blender.
    """
    report = SimilarityReport()

    backend = _load_backend()
    if backend is None:
        report.notes.append(
            "Pillow/numpy unavailable — pixel comparison skipped, "
            "object comparison still applied"
        )

    for viewpoint, reference, rendered in pairs:
        view = ViewComparison(
            image_id=viewpoint.image_id,
            reference=os.path.basename(reference or ""),
            rendered=os.path.basename(rendered or ""),
            room_id=viewpoint.room_id,
        )

        if backend is not None and _readable(reference) and _readable(rendered):
            _score_pixels(view, backend, reference, rendered)
        else:
            for name in ("colour", "lighting", "layout", "material"):
                view.axes[name] = AxisScore(
                    name, 0.0, available=False,
                    detail="no render available for this viewpoint",
                )

        _score_objects(view, graph, viewpoint)
        report.views.append(view)

    _scene_findings(report, graph)
    return report


def _readable(path: str) -> bool:
    return bool(path) and os.path.isfile(path)


def _load_backend():
    """Return the imaging helpers, or ``None`` when they are unavailable."""
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None
    return (np, Image)


# ---------------------------------------------------------------------------
# Pixel axes
# ---------------------------------------------------------------------------


def _score_pixels(view: ViewComparison, backend, reference: str, rendered: str) -> None:
    np, Image = backend

    try:
        left = _load(np, Image, reference)
        right = _load(np, Image, rendered)
    except Exception as error:  # a corrupt file must not abort the report
        for name in ("colour", "lighting", "layout", "material"):
            view.axes[name] = AxisScore(name, 0.0, False, f"could not read images: {error}")
        return

    view.axes["colour"] = _colour_axis(np, left, right, view)
    view.axes["lighting"] = _lighting_axis(np, left, right, view)
    view.axes["layout"] = _layout_axis(np, left, right, view)
    view.axes["material"] = _material_axis(np, left, right, view)


def _load(np, Image, path: str):
    """Load an image as a float array at the working resolution.

    Both images are forced to the same small size, so a render at a different
    output resolution than the photograph still compares.
    """
    with Image.open(path) as handle:
        rgb = handle.convert("RGB").resize(
            (ANALYSIS_WIDTH, ANALYSIS_HEIGHT), Image.BILINEAR
        )
        return np.asarray(rgb, dtype="float64") / 255.0


def _colour_axis(np, left, right, view: ViewComparison) -> AxisScore:
    """Palette and histogram agreement."""
    # Coarse 4x4x4 RGB histogram, intersected. Coarse on purpose: the question
    # is whether the room is beige-and-walnut, not whether two beiges match to
    # the last bit.
    hist_left = _histogram(np, left)
    hist_right = _histogram(np, right)
    intersection = float(np.minimum(hist_left, hist_right).sum())

    mean_left = _mean_hex(np, left)
    mean_right = _mean_hex(np, right)
    mean_distance = appearance.distance(mean_left, mean_right)

    score = max(0.0, 0.6 * intersection + 0.4 * (1.0 - mean_distance))

    if mean_distance > 0.18:
        view.findings.append(Finding(
            axis="colour", severity="warning", remedy="materials",
            detail=(
                f"overall colour differs — reference reads {mean_left}, "
                f"render reads {mean_right}"
            ),
        ))

    return AxisScore(
        "colour", score,
        detail=f"histogram {intersection:.2f}, mean colour Δ{mean_distance:.2f}",
    )


def _histogram(np, image):
    quantised = np.clip((image * 4).astype("int32"), 0, 3)
    flat = quantised[..., 0] * 16 + quantised[..., 1] * 4 + quantised[..., 2]
    counts = np.bincount(flat.ravel(), minlength=64).astype("float64")
    return counts / max(1.0, counts.sum())


def _mean_hex(np, image) -> str:
    mean = image.reshape(-1, 3).mean(axis=0) * 255.0
    return appearance.to_hex(tuple(mean))


def _lighting_axis(np, left, right, view: ViewComparison) -> AxisScore:
    """Brightness, contrast and warmth."""
    luma_left = _luma(np, left)
    luma_right = _luma(np, right)

    brightness_left, brightness_right = float(luma_left.mean()), float(luma_right.mean())
    contrast_left, contrast_right = float(luma_left.std()), float(luma_right.std())
    warmth_left, warmth_right = _warmth(np, left), _warmth(np, right)

    brightness_score = 1.0 - min(1.0, abs(brightness_left - brightness_right) / 0.5)
    contrast_score = 1.0 - min(1.0, abs(contrast_left - contrast_right) / 0.35)
    warmth_score = 1.0 - min(1.0, abs(warmth_left - warmth_right) / 0.35)

    score = 0.4 * brightness_score + 0.25 * contrast_score + 0.35 * warmth_score

    if brightness_right < brightness_left - 0.10:
        view.findings.append(Finding(
            axis="lighting", severity="warning", remedy="lighting",
            detail=f"render is darker than the reference "
                   f"({brightness_right:.2f} vs {brightness_left:.2f})",
        ))
    elif brightness_right > brightness_left + 0.10:
        view.findings.append(Finding(
            axis="lighting", severity="warning", remedy="lighting",
            detail=f"render is brighter than the reference "
                   f"({brightness_right:.2f} vs {brightness_left:.2f})",
        ))

    if warmth_right > warmth_left + 0.08:
        view.findings.append(Finding(
            axis="lighting", severity="warning", remedy="lighting",
            detail="lighting too warm compared with the reference",
        ))
    elif warmth_right < warmth_left - 0.08:
        view.findings.append(Finding(
            axis="lighting", severity="warning", remedy="lighting",
            detail="lighting too cool compared with the reference",
        ))

    return AxisScore(
        "lighting", score,
        detail=(
            f"brightness {brightness_right:.2f} vs {brightness_left:.2f}, "
            f"warmth {warmth_right:+.2f} vs {warmth_left:+.2f}"
        ),
    )


def _luma(np, image):
    return (
        0.2126 * image[..., 0] + 0.7152 * image[..., 1] + 0.0722 * image[..., 2]
    )


def _warmth(np, image) -> float:
    """Red-minus-blue balance; positive is warm."""
    return float(image[..., 0].mean() - image[..., 2].mean())


def _layout_axis(np, left, right, view: ViewComparison) -> AxisScore:
    """Where the visual mass sits.

    Compared as a coarse grid of local contrast rather than raw brightness:
    contrast marks where *things* are — edges of furniture, window frames —
    while brightness would mostly measure how the room is lit, which the
    lighting axis already covers.
    """
    grid_left = _mass_grid(np, left)
    grid_right = _mass_grid(np, right)

    # Correlate the two grids. Correlation rather than absolute difference
    # because the question is whether the mass is in the same *places*, not
    # whether the render is uniformly busier.
    a = grid_left - grid_left.mean()
    b = grid_right - grid_right.mean()
    denominator = float(np.sqrt((a * a).sum() * (b * b).sum()))
    correlation = float((a * b).sum() / denominator) if denominator > 1e-9 else 0.0
    score = max(0.0, (correlation + 1.0) / 2.0)

    if score < WEAK_AXIS:
        heavier = _describe_shift(np, grid_left, grid_right)
        view.findings.append(Finding(
            axis="layout", severity="warning", remedy="assets",
            detail=f"furniture layout differs from the reference{heavier}",
        ))

    return AxisScore("layout", score, detail=f"grid correlation {correlation:+.2f}")


def _mass_grid(np, image):
    """Mean local contrast per cell of a coarse grid."""
    luma = _luma(np, image)
    gradient_y, gradient_x = np.gradient(luma)
    energy = np.sqrt(gradient_x**2 + gradient_y**2)

    rows = np.array_split(energy, LAYOUT_ROWS, axis=0)
    grid = []
    for band in rows:
        grid.append([float(cell.mean()) for cell in np.array_split(band, LAYOUT_COLUMNS, axis=1)])
    return np.array(grid, dtype="float64")


def _describe_shift(np, left, right) -> str:
    """Name the side of the frame where the biggest disagreement sits."""
    difference = right - left
    row, column = np.unravel_index(int(np.argmax(np.abs(difference))), difference.shape)
    vertical = ("upper", "middle", "lower")[min(2, int(row / max(1, LAYOUT_ROWS / 3)))]
    horizontal = ("left", "centre", "right")[min(2, int(column / max(1, LAYOUT_COLUMNS / 3)))]
    more = "more" if difference[row][column] > 0 else "less"
    return f" — {more} detail in the {vertical} {horizontal} of the frame"


def _material_axis(np, left, right, view: ViewComparison) -> AxisScore:
    """Texture busyness across scales.

    Procedural texture will never match a photograph pixel for pixel, so this
    asks a weaker and fairer question: does the render have roughly the same
    amount of fine detail as the reference? A scene rendered in flat colours
    against a photograph full of grain and weave scores low here, which is the
    signal worth having.
    """
    scores = []
    detail = []
    for scale in (1, 2, 4):
        energy_left = _detail_energy(np, left, scale)
        energy_right = _detail_energy(np, right, scale)
        largest = max(energy_left, energy_right, 1e-6)
        scores.append(1.0 - min(1.0, abs(energy_left - energy_right) / largest))
        detail.append(f"{energy_right:.3f}/{energy_left:.3f}")

    score = sum(scores) / len(scores)

    if score < WEAK_AXIS:
        view.findings.append(Finding(
            axis="material", severity="note", remedy="materials",
            detail="surfaces read flatter than the reference; "
                   "textures may be missing grain",
        ))

    return AxisScore("material", score, detail="detail energy " + ", ".join(detail))


def _detail_energy(np, image, scale: int) -> float:
    luma = _luma(np, image)
    if scale > 1:
        rows = luma.shape[0] // scale * scale
        columns = luma.shape[1] // scale * scale
        luma = luma[:rows, :columns].reshape(
            rows // scale, scale, columns // scale, scale
        ).mean(axis=(1, 3))
    gradient_y, gradient_x = np.gradient(luma)
    return float(np.sqrt(gradient_x**2 + gradient_y**2).mean())


# ---------------------------------------------------------------------------
# Object axis
# ---------------------------------------------------------------------------


def _score_objects(view: ViewComparison, graph: SceneGraph, viewpoint: ViewPoint) -> None:
    """What the photograph was observed to contain, versus what was built.

    Measured from the graph rather than from the render. Re-detecting furniture
    in the render would compare one model's opinion with another's; the graph
    already records what each image contributed and what survived to the build,
    and the difference between those is exact.
    """
    observed = [
        obj for obj in graph.objects
        if viewpoint.image_id in obj.source_images
        or (obj.room_id == viewpoint.room_id and not obj.source_images)
    ]
    if not observed:
        view.axes["objects"] = AxisScore(
            "objects", 0.0, available=False,
            detail="no objects traced to this image",
        )
        return

    built = [obj for obj in observed if _will_build(obj)]
    missing = [obj for obj in observed if not _will_build(obj)]

    score = len(built) / len(observed)

    for obj in sorted(missing, key=lambda o: -o.dimensions.footprint_area)[:8]:
        view.findings.append(Finding(
            axis="objects",
            severity="warning" if obj.dimensions.footprint_area > 0.25 else "note",
            remedy="decor",
            subject=obj.id,
            detail=_why_missing(obj),
        ))

    return_detail = f"{len(built)}/{len(observed)} observed objects built"
    view.axes["objects"] = AxisScore("objects", score, detail=return_detail)


def _will_build(obj: SceneObject) -> bool:
    return not obj.uncertain and not obj.dimensions.is_degenerate()


def _why_missing(obj: SceneObject) -> str:
    """Explain an absence in terms the user can act on."""
    label = obj.category.replace("_", " ")
    if obj.dimensions.is_degenerate():
        return f"{label} was detected but has no usable size, so it was not built"
    for flag in obj.flags:
        if flag.startswith("withheld_failed_validation"):
            return f"{label} could not be placed plausibly and was withheld"
    return (
        f"{label} was detected at {obj.confidence:.0%} confidence, "
        "below the floor — keep it in the review step to build it"
    )


# ---------------------------------------------------------------------------
# Scene-level findings
# ---------------------------------------------------------------------------


def _scene_findings(report: SimilarityReport, graph: SceneGraph) -> None:
    """Problems that belong to the reconstruction rather than to one view."""
    poor = [
        obj for obj in graph.objects
        if 0 < obj.asset_score < 0.62 and _will_build(obj)
    ]
    for obj in sorted(poor, key=lambda o: o.asset_score)[:6]:
        report.findings.append(Finding(
            axis="objects", severity="note", remedy="assets", subject=obj.id,
            detail=(
                f"{obj.category.replace('_', ' ')} uses the closest available "
                f"model ({obj.asset}), {obj.asset_score:.0%} similar"
            ),
        ))

    generic = sorted({
        obj.category for obj in graph.objects if obj.asset.startswith("generic_")
    })
    if generic:
        report.findings.append(Finding(
            axis="objects", severity="note", remedy="",
            detail=(
                "no procedural model exists for: " + ", ".join(
                    name.replace("_", " ") for name in generic[:8]
                ) + " — built as proportioned blocks"
            ),
        ))

    for room in graph.rooms:
        if room.source_images and room.style == "unknown":
            report.findings.append(Finding(
                axis="colour", severity="note", remedy="materials",
                subject=room.id,
                detail=f"{room.id} has imagery but no recognised style, so asset "
                       "selection could not be style-guided",
            ))
