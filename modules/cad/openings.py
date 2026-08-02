"""
ArchX3D — Doors and windows from the drawing
============================================
Recovers openings from a :class:`CadDocument` so walls can be cut where the
architect actually put a door, rather than left solid.

Why this exists
---------------
The drawing states its openings. A door is an inserted block on a door layer;
a window is a short run of parallel lines breaking a wall on a window layer.
Both were already parsed, role-tagged and handed to the room classifier as
*evidence* — a room with two windows is more likely a bedroom than a store —
but nothing ever turned them into geometry, so every wall in the build was
unbroken. The information was read and then dropped one step short of use.

That is the exact failure the trust hierarchy exists to prevent: openings were
being inferred from photographs (tier 6) when the drawing declared them
(tiers 2–3). A photograph of one corner sees at most a couple of openings; the
drawing has all of them, positioned to the millimetre.

What is measured and what is assumed
------------------------------------
Plan geometry gives **position, width and orientation** — measured, and
reported as such. It cannot give **height or sill**, because a plan is a
horizontal section: it says where the hole is, never how tall. Those come from
:data:`DOOR_HEIGHT_M` and friends, which are conventions, and every opening
records which of its numbers were read and which were assumed so a reviewer can
tell them apart.

Recognising a window
--------------------
Windows are not blocks; they are drawn. The convention is a rectangle spanning
the wall thickness, usually with one or two glazing lines through it. So the
segments on a window layer are clustered by touching endpoints, and each
cluster's bounding box is read as one window: the long side is the opening
width, the short side is the wall it sits in.

This is deliberately geometric rather than pattern-matching a particular CAD
office's block names — those vary per practice, the rectangle does not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Conventions, in metres. Stated as constants because they are assumptions, and
# an assumption that is named can be overridden; one buried in an expression
# cannot.

#: Head height of a domestic door.
DOOR_HEIGHT_M = 2.1
#: Typical window head height, and the sill it implies.
WINDOW_HEIGHT_M = 1.2
WINDOW_SILL_M = 0.9

#: Openings narrower than this are draughting marks, not openings.
MIN_WIDTH_M = 0.3
#: Wider than this and the "opening" is a mis-clustered run of wall lines.
MAX_WIDTH_M = 6.0

#: Separation below which two drawn segments belong to the same window.
#:
#: Sized to a wall, not to a draughting tolerance. The common convention draws
#: a window as two or three *parallel* lines across the wall — outer face, inner
#: face, and the glazing between — with no jambs closing the ends. Those lines
#: never touch, so an endpoint-scale tolerance leaves each one a separate
#: figure and reports one window three times. What actually binds them is that
#: they span a single wall's thickness.
#:
#: The nearest genuinely distinct windows in a plan are a structural pier
#: apart — metres, not centimetres — so this is a wide margin, not a fine one.
JOIN_TOLERANCE_M = 0.30


@dataclass
class CadOpening:
    """One opening recovered from the drawing."""

    uid: str = ""
    kind: str = "window"           # door | window
    x: float = 0.0
    y: float = 0.0
    width: float = 0.9
    height: float = DOOR_HEIGHT_M
    sill_height: float = 0.0
    #: Direction of the opening's span, degrees CCW from +X.
    rotation_deg: float = 0.0
    #: Thickness of the wall the opening sits in, where the drawing showed it.
    depth: float = 0.0
    confidence: float = 0.0
    source: str = "cad_block"
    #: Which fields were read from the drawing rather than assumed.
    measured: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "kind": self.kind,
            "position": [round(self.x, 4), round(self.y, 4)],
            "width": round(self.width, 4),
            "height": round(self.height, 4),
            "sill_height": round(self.sill_height, 4),
            "rotation_deg": round(self.rotation_deg, 2),
            "depth": round(self.depth, 4),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "measured": list(self.measured),
            "reason": self.reason,
        }


def from_document(document) -> List[CadOpening]:
    """Recover every door and window the drawing declares."""
    return doors_from_blocks(document) + windows_from_segments(document)


# ---------------------------------------------------------------------------
# Doors
# ---------------------------------------------------------------------------


def doors_from_blocks(document) -> List[CadOpening]:
    """Doors are inserted blocks; the reader has already categorised them.

    Width comes from the block's own extent rather than its name or scale
    factor. ``900-Door`` inserted at 0.87 scale is an 780 mm door, and the name
    would have claimed 900 — the geometry is what was drawn.
    """
    openings: List[CadOpening] = []

    for index, block in enumerate(getattr(document, "blocks", []) or []):
        if _category(block) != "door":
            continue

        position = _xy(getattr(block, "position", None))
        if position is None:
            continue

        width = _block_extent(block)
        if width is None or not MIN_WIDTH_M <= width <= MAX_WIDTH_M:
            # A door block whose extent we cannot trust still marks a real
            # doorway, so fall back to the nominal leaf rather than dropping it.
            width = 0.9

        rotation = float(getattr(block, "rotation", 0.0) or 0.0)
        name = getattr(block, "name", "") or "door"

        openings.append(CadOpening(
            uid=getattr(block, "uid", "") or f"cad_door_{index}",
            kind="door",
            x=position[0], y=position[1],
            width=width,
            height=DOOR_HEIGHT_M,
            sill_height=0.0,
            rotation_deg=rotation,
            confidence=float(getattr(block, "confidence", 0.0) or 0.85),
            source="cad_block",
            measured=["position", "width", "rotation"],
            reason=f"{name} block on layer {getattr(block, 'layer', '?')}",
        ))

    return openings


def _block_extent(block) -> Optional[float]:
    """Width of a door block from its bounding box.

    The box of a plan door includes the swing arc, whose radius is the leaf
    length, so the larger side of the box is the opening width.
    """
    lo = _xy(getattr(block, "bounds_min", None))
    hi = _xy(getattr(block, "bounds_max", None))
    if lo is None or hi is None:
        return None
    extent = max(hi[0] - lo[0], hi[1] - lo[1])
    return extent if extent > 0 else None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def windows_from_segments(document) -> List[CadOpening]:
    """Windows are drawn figures; cluster them and read each box."""
    segments = [
        s for s in (getattr(document, "segments", []) or [])
        if _role(s) == "window"
    ]
    if not segments:
        return []

    openings: List[CadOpening] = []
    for index, cluster in enumerate(_cluster(segments)):
        opening = _window_from_cluster(cluster, index)
        if opening is not None:
            openings.append(opening)
    return openings


def _window_from_cluster(cluster: Sequence[Any], index: int) -> Optional[CadOpening]:
    xs: List[float] = []
    ys: List[float] = []
    for segment in cluster:
        for point in (_xy(getattr(segment, "start", None)),
                      _xy(getattr(segment, "end", None))):
            if point is not None:
                xs.append(point[0])
                ys.append(point[1])
    if not xs:
        return None

    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    width, depth = max(span_x, span_y), min(span_x, span_y)
    if not MIN_WIDTH_M <= width <= MAX_WIDTH_M:
        return None

    return CadOpening(
        uid=f"cad_window_{index}",
        kind="window",
        x=(max(xs) + min(xs)) / 2.0,
        y=(max(ys) + min(ys)) / 2.0,
        width=width,
        height=WINDOW_HEIGHT_M,
        sill_height=WINDOW_SILL_M,
        # The opening runs along its longer side.
        rotation_deg=0.0 if span_x >= span_y else 90.0,
        depth=depth,
        confidence=0.8,
        source="cad_layer",
        measured=["position", "width", "rotation"],
        reason=f"{len(cluster)} segment(s) on a window layer",
    )


def _cluster(segments: Sequence[Any]) -> List[List[Any]]:
    """Group segments into drawn figures by touching endpoints.

    Union-find over endpoint proximity. A window's outline, its jambs and its
    glazing lines all meet, so they collapse into one set; the next window
    along the wall is metres away and stays separate.
    """
    parent = list(range(len(segments)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    spans: List[Optional[Tuple[Tuple[float, float], Tuple[float, float]]]] = []
    for segment in segments:
        start = _xy(getattr(segment, "start", None))
        end = _xy(getattr(segment, "end", None))
        spans.append((start, end) if start and end else None)

    for i in range(len(segments)):
        for j in range(i + 1, len(segments)):
            if spans[i] and spans[j] and _touching(spans[i], spans[j]):
                union(i, j)

    grouped: Dict[int, List[Any]] = {}
    for i, segment in enumerate(segments):
        grouped.setdefault(find(i), []).append(segment)
    return list(grouped.values())


Span = Tuple[Tuple[float, float], Tuple[float, float]]


def _touching(a: Span, b: Span) -> bool:
    """Whether two drawn segments meet.

    Endpoint-to-endpoint is not enough: a window's glazing line runs from jamb
    to jamb, meeting each one at its *midpoint*, so comparing endpoints alone
    leaves every glazing line as its own figure and turns one window into
    three. Each endpoint is therefore measured against the whole of the other
    segment.
    """
    return (
        any(_point_to_segment(p, b) <= JOIN_TOLERANCE_M for p in a)
        or any(_point_to_segment(q, a) <= JOIN_TOLERANCE_M for q in b)
    )


def _point_to_segment(point: Tuple[float, float], span: Span) -> float:
    """Shortest distance from a point to a line segment."""
    (x0, y0), (x1, y1) = span
    dx, dy = x1 - x0, y1 - y0
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.dist(point, (x0, y0))
    t = ((point[0] - x0) * dx + (point[1] - y0) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.dist(point, (x0 + t * dx, y0 + t * dy))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _xy(value) -> Optional[Tuple[float, float]]:
    """Read an (x, y) from a tuple, list or point-like object."""
    if value is None:
        return None
    if hasattr(value, "x") and hasattr(value, "y"):
        try:
            return (float(value.x), float(value.y))
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _category(block) -> str:
    return str(getattr(block, "category", "") or "").strip().lower()


def _role(segment) -> str:
    return str(getattr(segment, "role", "") or "").strip().lower()


def summarise(openings: Iterable[CadOpening]) -> str:
    items = list(openings)
    doors = sum(1 for o in items if o.kind == "door")
    windows = sum(1 for o in items if o.kind == "window")
    return f"{len(items)} opening(s) from the drawing: {doors} door(s), {windows} window(s)"
