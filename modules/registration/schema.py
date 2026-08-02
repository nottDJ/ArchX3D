"""
ArchX3D — Registration Schema
=============================
The typed result of asking "where, on the plan, is this image looking?"

Why this exists
---------------
Two inputs describe the same building in two coordinate systems that nothing
relates. The DXF is metric, origin-normalised, +Y up. A reference image is
normalised pixels, origin top-left, +v down, at an unknown scale, an unknown
rotation, and — for a composite sheet — occupying an unknown *sub-rectangle*
of the frame.

Everything downstream that mixes the two has been papering over that gap.
``grounding.ground_plan_view`` assumes the plan fills the frame exactly;
``assignment`` guesses which room a photo shows from floor area. Both are
guesses where a correspondence exists, which is the one thing this project
says it will not do.

This module is the container for the answer. Nothing here computes anything;
it holds a transform, the evidence that produced it, and an honest statement
of how much to trust it.

Design constraints
------------------
* **Stdlib only.** Same rule as ``cad`` and ``semantic``: no numpy, no ezdxf.
  A registration must be checkable in a Blender process and in a test that has
  no API key.
* **A transform is never anonymous.** Every one carries the ``method`` that
  produced it and the correspondences it was fitted from, so a reviewer can
  see *why* the pipeline believes this image is that floor.
* **Failure is a value, not an exception.** An unregisterable image yields a
  result with ``method="none"``, not a raise — the caller then decides between
  the legacy full-frame assumption and dropping the image's positions, and
  either way it knows which it chose.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1.0"

Point = Tuple[float, float]


class Method:
    """How a transform was arrived at, ordered by how much it is worth.

    The ordering matters: a caller merging registrations from several images
    of the same sheet should prefer the one with the strongest method, and
    "strongest" has to mean something more principled than highest confidence
    — a full-frame assumption can be *consistently* wrong and look confident.
    """

    #: Fitted from two or more label correspondences with a robust consensus.
    LABEL_CONSENSUS = "label_consensus"
    #: One matched label plus an assumed scale. Weak, but anchored to a fact.
    SINGLE_ANCHOR = "single_anchor"
    #: The legacy assumption: the plan fills the frame exactly. A guess.
    PLAN_BOUNDS = "plan_bounds"
    #: Nothing could be established.
    NONE = "none"

    _RANK: Dict[str, int] = {
        LABEL_CONSENSUS: 0,
        SINGLE_ANCHOR: 1,
        PLAN_BOUNDS: 2,
        NONE: 3,
    }

    #: Methods that fitted the transform to observed evidence rather than
    #: assuming it. Only these count as "the image registered".
    EVIDENCE_BASED = (LABEL_CONSENSUS, SINGLE_ANCHOR)

    @classmethod
    def rank(cls, method: str) -> int:
        return cls._RANK.get(method, 99)

    @classmethod
    def better(cls, a: str, b: str) -> bool:
        return cls.rank(a) < cls.rank(b)


# ---------------------------------------------------------------------------
# The transform
# ---------------------------------------------------------------------------


@dataclass
class PlanTransform:
    """Maps normalised image coordinates to plan metres.

    ::

        x = a*u + b*v + tx
        y = c*u + d*v + ty

    where ``(u, v)`` is normalised image space (origin top-left, v downward)
    and ``(x, y)`` is plan metres (+Y up).

    A general affine rather than a similarity, for one reason: the *fallback*
    is anisotropic. Stretching a plan to fill the frame scales x by the
    building's width and y by its depth independently, and those differ unless
    the building is square. Representing only similarities would mean the
    fallback could not be expressed as a transform at all, and the legacy path
    would stay a separate hard-coded branch — which is exactly the ambiguity
    this engine exists to remove.

    Fitted transforms *are* constrained to similarities, because a floor plan
    is a uniformly scaled orthographic view of a floor. A drawing is never
    stretched along one axis, so allowing six free parameters would let the
    fit absorb correspondence error into a nonsensical stretch instead of
    reporting it as residual. ``is_similarity`` says which kind this is.
    """

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = -1.0
    tx: float = 0.0
    ty: float = 0.0

    # -- application --------------------------------------------------------

    def apply(self, u: float, v: float) -> Point:
        """Image ``(u, v)`` → plan metres."""
        return (
            self.a * u + self.b * v + self.tx,
            self.c * u + self.d * v + self.ty,
        )

    def apply_box(self, box: Sequence[float]) -> Tuple[Point, Point]:
        """Image box ``(u0, v0, u1, v1)`` → its axis-aligned plan extent.

        All four corners are mapped rather than just two, because a rotated
        transform sends an axis-aligned image box to a rotated plan box, and
        taking only two corners would silently under-report its extent.
        """
        u0, v0, u1, v1 = box
        corners = [
            self.apply(u0, v0), self.apply(u1, v0),
            self.apply(u1, v1), self.apply(u0, v1),
        ]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        return (min(xs), min(ys)), (max(xs), max(ys))

    def inverse(self) -> Optional["PlanTransform"]:
        """The plan → image transform, or ``None`` if this one is degenerate."""
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-12:
            return None
        return PlanTransform(
            a=self.d / det,
            b=-self.b / det,
            c=-self.c / det,
            d=self.a / det,
            tx=(self.b * self.ty - self.d * self.tx) / det,
            ty=(self.c * self.tx - self.a * self.ty) / det,
        )

    # -- description --------------------------------------------------------

    @property
    def scale_x(self) -> float:
        """Metres spanned by the full image width along the plan's x axis."""
        return math.hypot(self.a, self.c)

    @property
    def scale_y(self) -> float:
        return math.hypot(self.b, self.d)

    @property
    def scale(self) -> float:
        """Mean metres per unit of normalised image extent."""
        return (self.scale_x + self.scale_y) / 2.0

    @property
    def rotation_deg(self) -> float:
        """Plan rotation of the drawing on the sheet, degrees CCW."""
        return math.degrees(math.atan2(self.c, self.a))

    @property
    def is_similarity(self) -> bool:
        """True when the transform is a uniform scale, rotation and flip.

        A vertical flip is baked in — image v runs down and plan y runs up —
        so the similarity condition is ``a == -d`` and ``b == c`` rather than
        the usual ``a == d``, ``b == -c``.
        """
        magnitude = max(abs(self.a), abs(self.b), abs(self.c), abs(self.d), 1e-9)
        return (
            abs(self.a + self.d) / magnitude < 1e-6
            and abs(self.b - self.c) / magnitude < 1e-6
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "a": round(self.a, 8), "b": round(self.b, 8),
            "c": round(self.c, 8), "d": round(self.d, 8),
            "tx": round(self.tx, 8), "ty": round(self.ty, 8),
            "scale_m_per_unit": round(self.scale, 5),
            "rotation_deg": round(self.rotation_deg, 3),
            "similarity": self.is_similarity,
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> Optional["PlanTransform"]:
        if not d:
            return None
        return PlanTransform(
            a=_f(d.get("a"), 1.0), b=_f(d.get("b")),
            c=_f(d.get("c")), d=_f(d.get("d"), -1.0),
            tx=_f(d.get("tx")), ty=_f(d.get("ty")),
        )

    # -- construction -------------------------------------------------------

    @staticmethod
    def from_similarity(scale: float, rotation_deg: float, tx: float, ty: float) -> "PlanTransform":
        """A uniform scale + rotation + vertical flip + translation."""
        theta = math.radians(rotation_deg)
        cos, sin = math.cos(theta), math.sin(theta)
        return PlanTransform(
            a=scale * cos, b=scale * sin,
            c=scale * sin, d=-scale * cos,
            tx=tx, ty=ty,
        )

    @staticmethod
    def stretch_to_bounds(bounds_min: Point, bounds_max: Point) -> "PlanTransform":
        """The legacy assumption: the image *is* the plan, filling the frame.

        Kept as a named, first-class transform rather than left implicit in
        ``ground_plan_view``, so that when it is used the diagnostics can say
        so. Its anisotropy is the tell: a result whose transform is not a
        similarity was assumed, not measured.
        """
        width = bounds_max[0] - bounds_min[0]
        depth = bounds_max[1] - bounds_min[1]
        return PlanTransform(
            a=width, b=0.0, tx=bounds_min[0],
            c=0.0, d=-depth, ty=bounds_max[1],
        )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass
class Correspondence:
    """One image feature paired with one CAD entity that may be the same thing.

    Candidates are generated liberally — the same printed word "BEDROOM" is a
    candidate against every ``BEDROOM`` label in the drawing — and the
    consensus fit decides which pairings are real. ``inlier`` records that
    verdict, and both sides keep their uid so a reviewer can trace a placement
    back to the exact text entity in the DXF.
    """

    #: Normalised text both sides agreed on, e.g. ``"MASTER BEDROOM"``.
    text: str = ""
    #: Centre of the label as printed in the image, normalised, v downward.
    image_uv: Point = (0.0, 0.0)
    #: Anchor of the matching CAD text, in plan metres.
    plan_xy: Point = (0.0, 0.0)
    #: ``CadText.uid`` of the drawing-side entity.
    cad_uid: str = ""
    #: Identifier of the image-side label within its observation.
    image_label_id: str = ""
    #: Match quality before any geometry is considered, in [0, 1].
    weight: float = 1.0
    #: Distance in metres between the mapped image point and ``plan_xy``.
    #: Only meaningful once a transform has been fitted.
    residual_m: float = 0.0
    #: Whether the consensus fit accepted this pairing.
    inlier: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "image_uv": [round(self.image_uv[0], 5), round(self.image_uv[1], 5)],
            "plan_xy": [round(self.plan_xy[0], 4), round(self.plan_xy[1], 4)],
            "cad_uid": self.cad_uid,
            "image_label_id": self.image_label_id,
            "weight": round(self.weight, 3),
            "residual_m": round(self.residual_m, 4),
            "inlier": self.inlier,
        }


@dataclass
class SheetRegion:
    """The rectangle of the image that the CAD plan actually occupies.

    This is the answer to the composite-sheet problem. A sheet holding an
    elevation render above two floor plans registers with the plan filling
    perhaps a fifth of the frame; knowing *which* fifth is what makes it
    possible to read that sheet at all, instead of telling the user to crop it
    by hand.
    """

    u0: float = 0.0
    v0: float = 0.0
    u1: float = 1.0
    v1: float = 1.0

    @property
    def width(self) -> float:
        return max(0.0, self.u1 - self.u0)

    @property
    def height(self) -> float:
        return max(0.0, self.v1 - self.v0)

    @property
    def coverage(self) -> float:
        """Fraction of the frame the plan covers, in [0, 1]."""
        return max(0.0, min(1.0, self.width * self.height))

    @property
    def looks_composite(self) -> bool:
        """True when the plan is clearly not the whole image.

        The threshold is deliberately generous. A plan drawn with normal sheet
        margins still covers well over half the frame; below that, something
        else is on the page.
        """
        return self.coverage < 0.55

    def to_dict(self) -> Dict[str, Any]:
        return {
            "u0": round(self.u0, 4), "v0": round(self.v0, 4),
            "u1": round(self.u1, 4), "v1": round(self.v1, 4),
            "coverage": round(self.coverage, 4),
            "looks_composite": self.looks_composite,
        }


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class RegistrationResult:
    """What registering one plan-view image against one drawing established."""

    image_id: str = ""
    transform: Optional[PlanTransform] = None
    method: str = Method.NONE
    confidence: float = 0.0

    #: Every candidate pairing considered, inliers flagged.
    correspondences: List[Correspondence] = field(default_factory=list)
    #: Where on the sheet the plan sits, once a transform exists.
    sheet_region: Optional[SheetRegion] = None

    #: Labels read in the image that no CAD entity matched. A populated list
    #: on an otherwise good fit is the signature of a multi-floor sheet: those
    #: are the other floor's rooms.
    unmatched_image_labels: List[str] = field(default_factory=list)
    #: Room labels in the drawing that were not found in the image.
    unmatched_cad_labels: List[str] = field(default_factory=list)

    residual_mean_m: float = 0.0
    residual_max_m: float = 0.0

    reason: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def registered(self) -> bool:
        """True only when the transform was fitted to evidence."""
        return self.method in Method.EVIDENCE_BASED and self.transform is not None

    @property
    def inliers(self) -> List[Correspondence]:
        return [c for c in self.correspondences if c.inlier]

    def explain(self) -> str:
        """One line for a log or a review panel."""
        if not self.transform:
            return f"{self.image_id}: not registered — {self.reason or 'no evidence'}"
        head = (
            f"{self.image_id}: {self.method} {self.confidence:.0%}, "
            f"{len(self.inliers)}/{len(self.correspondences)} labels matched"
        )
        if self.registered:
            head += (
                f", residual {self.residual_mean_m:.2f} m mean / "
                f"{self.residual_max_m:.2f} m worst"
            )
        if self.sheet_region and self.sheet_region.looks_composite:
            head += f", plan covers {self.sheet_region.coverage:.0%} of the sheet"
        return head

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "image_id": self.image_id,
            "method": self.method,
            "registered": self.registered,
            "confidence": round(self.confidence, 3),
            "transform": self.transform.to_dict() if self.transform else None,
            "sheet_region": self.sheet_region.to_dict() if self.sheet_region else None,
            "correspondences": [c.to_dict() for c in self.correspondences],
            "unmatched_image_labels": list(self.unmatched_image_labels),
            "unmatched_cad_labels": list(self.unmatched_cad_labels),
            "residual_mean_m": round(self.residual_mean_m, 4),
            "residual_max_m": round(self.residual_max_m, 4),
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


@dataclass
class RoomRegistration:
    """Which room in the plan an interior photograph shows.

    The perspective counterpart to ``RegistrationResult``. A photograph of a
    bedroom cannot be fitted to plan coordinates — there is no shared
    coordinate system to fit to — so registering it means choosing a region,
    with the reasons that choice was made.
    """

    image_ids: List[str] = field(default_factory=list)
    room_id: str = ""
    #: What the image claimed to be.
    observed_room_type: str = "unknown"
    #: What the drawing says the chosen region is. Authoritative when set.
    cad_room_type: str = "unknown"
    score: float = 0.0
    confidence: float = 0.0
    #: cad_room_type | area_plausibility | fallback | none
    method: str = "none"
    reasons: List[str] = field(default_factory=list)
    #: True when the image's claim contradicts the drawing's. The drawing
    #: wins, but the disagreement is recorded rather than hidden.
    conflicts_with_cad: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "image_ids": list(self.image_ids),
            "room_id": self.room_id,
            "observed_room_type": self.observed_room_type,
            "cad_room_type": self.cad_room_type,
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "reasons": list(self.reasons),
            "conflicts_with_cad": self.conflicts_with_cad,
        }


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce to float without raising, mirroring ``cad.schema``."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else default
    if isinstance(value, str):
        try:
            parsed = float(value.strip().replace(",", "."))
            return parsed if math.isfinite(parsed) else default
        except ValueError:
            return default
    return default
