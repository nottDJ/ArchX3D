"""
ArchX3D — Doors, windows and the holes they make in walls
=========================================================
Finds openings from CAD evidence, and — just as importantly — tells the wall
builder where a wall continues *through* a gap in its line work.

The circular problem, and how it is broken
------------------------------------------
A wall's line work stops at every door and window: that is what an opening is
on a plan. So the wall faces arrive already cut into pieces, and a naive
reconstruction produces a dozen short walls per elevation with holes between
them where the doors were. No room then closes, because the enclosing loop is
broken everywhere there is a door. But you cannot find openings from the
walls, because the walls are what you are trying to build.

The way out is that openings are drawn *positively*, not as absence:

* a **header** or lintel — a rectangle exactly as thick as its wall and as
  long as the opening it spans (``A-HEADER``, ``R-BEAM``, ``S-LINTEL``)
* an **opening band** on an openings layer, the same rectangle by another name
* a **door swing arc**, whose radius is the door width and whose centre is the
  hinge
* **glazing lines** — the two or three parallel lines drawn across a window
* a **door or window block**, placed at the opening

Every one of these is evidence that exists independently of the walls. So this
module runs *first*, on the drawing alone, and hands the wall builder a set of
bridges: places where a gap in the line work is a hole in a wall rather than
the end of one. The walls then come back complete, and the same evidence is
matched onto them to become :class:`~modules.recon.ir.Opening` records.

Anything left over — a gap with no positive evidence at all — is only taken as
an opening when both sides of it are unambiguously the same wall and the gap
is a plausible opening width. That is the weakest rule here and it is applied
last, with a confidence to match.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import classify as C
from .ir import Opening, Wall
from .read import Arc, CadDrawing, Prim

XY = Tuple[float, float]

#: An opening is at least this wide and at most this wide, in metres. The top
#: end has to admit a 16 ft garage door and a wide sliding wall.
WIDTH_BAND = (0.35, 7.0)

#: A band of geometry this thin, in metres, is a wall-thickness band — a
#: header, a lintel or an opening marker — rather than a room-sized shape.
BAND_THICKNESS_BAND = (0.04, 0.70)

#: How far an opening's centre may sit from a wall centreline and still be
#: hosted by it, as a multiple of the wall's thickness.
HOST_DISTANCE_RATIO = 1.35

#: An opening must be parallel to its host wall within this angle, in degrees.
HOST_ANGLE_TOL = 8.0

#: A door symbol's bridge reaches this far into its wall from the chord — the
#: thickest wall the wall stage accepts, with a little over — and this far
#: behind it, in metres, for symbols hinged on the centreline.
BRIDGE_REACH = 0.66
BRIDGE_BACK = 0.3

#: Conventional heights, in metres. Used only as defaults: an opening's
#: elevation is not present in a plan view, and inventing a per-opening value
#: would be a fabrication, whereas the conventions are what the building code
#: assumes when the drawing is silent.
DOOR_HEIGHT = 2.032           # 6'-8"
GARAGE_HEIGHT = 2.134         # 7'-0"
WINDOW_HEAD = 2.032
WINDOW_SILL = 0.914           # 3'-0"
CASED_HEIGHT = 2.134

#: A door this wide is a garage door rather than a patio slider, in metres.
GARAGE_MIN_WIDTH = 2.6


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """One piece of positive evidence that an opening exists somewhere.

    ``centre``/``angle``/``width``/``band`` describe the rectangle it occupies:
    ``width`` along the wall, ``band`` across it. Two pieces of evidence in the
    same place (a header *and* a swing arc) are merged, and their kinds vote.
    """

    id: str
    centre: XY
    angle: float                  # degrees, along the opening
    width: float
    band: float
    kind: str                     # door | window | garage | cased | unknown
    confidence: float
    source: str
    layer: str = ""
    source_ids: List[str] = field(default_factory=list)
    swing: Optional[str] = None
    #: Evidence from one symbol that offers two mutually exclusive readings
    #: shares a group; at most one member of a group becomes an opening.
    group: str = ""
    #: Unit vector across the opening towards the wall body, when the symbol
    #: says which side that is: a door leaf swings out of its wall, so the
    #: wall lies on the side away from the leaf.
    wall_side: Optional[XY] = None

    @property
    def direction(self) -> XY:
        r = math.radians(self.angle)
        return (math.cos(r), math.sin(r))

    def span(self) -> Tuple[XY, XY]:
        d = self.direction
        h = self.width / 2.0
        return ((self.centre[0] - d[0] * h, self.centre[1] - d[1] * h),
                (self.centre[0] + d[0] * h, self.centre[1] + d[1] * h))

    @property
    def bridge_band(self) -> float:
        """Perpendicular reach when used to join a wall's line work.

        A swing arc's chord lies along one face of its wall, not down the
        middle, and its own band is nominal. Bridging with that nominal value
        reaches one face and not the other, so the wall is joined on one side
        and split on the other. Widening to a generous wall thickness costs
        nothing — a bridge only permits joining runs that are already
        collinear and already have a gap — and is what lets a door in a
        230 mm wall bridge both of its faces.
        """
        return max(self.band, 0.55)

    def bridge_box(self, pad: float = 0.0):
        """The region this evidence lets the wall builder join across.

        A door symbol is hinged on one face of its wall, so a band centred on
        its chord reaches the far face only of a thin wall; a 417 mm cavity
        wall stayed split on one side and the house never closed. When the
        symbol says where the wall is, the region runs from a little behind
        the chord to the far face of the thickest wall, on that side only.
        """
        if self.wall_side is None:
            return self.box(pad, band=self.bridge_band)
        from shapely.geometry import Polygon
        d = self.direction
        s = self.wall_side
        hw = self.width / 2.0 + pad
        back, reach = BRIDGE_BACK + pad, BRIDGE_REACH + pad
        c = self.centre
        corners = []
        for u, v in ((hw, -back), (-hw, -back), (-hw, reach), (hw, reach)):
            corners.append((c[0] + d[0] * u + s[0] * v, c[1] + d[1] * u + s[1] * v))
        return Polygon(corners)

    def box(self, pad: float = 0.0, band: Optional[float] = None):
        from shapely.geometry import Polygon
        d = self.direction
        n = (-d[1], d[0])
        hw = self.width / 2.0 + pad
        hb = (self.band if band is None else band) / 2.0 + pad
        return Polygon([
            (self.centre[0] + d[0] * hw + n[0] * hb, self.centre[1] + d[1] * hw + n[1] * hb),
            (self.centre[0] - d[0] * hw + n[0] * hb, self.centre[1] - d[1] * hw + n[1] * hb),
            (self.centre[0] - d[0] * hw - n[0] * hb, self.centre[1] - d[1] * hw - n[1] * hb),
            (self.centre[0] + d[0] * hw - n[0] * hb, self.centre[1] + d[1] * hw - n[1] * hb),
        ])


def _oriented_box(points: Sequence[XY]) -> Optional[Tuple[XY, float, float, float]]:
    """Minimum-area rectangle of a point set: (centre, angle, long, short).

    Used rather than an axis-aligned box so an opening in a wall running at
    23 degrees is measured along the wall instead of across the page.
    """
    from shapely.geometry import MultiPoint
    try:
        rect = MultiPoint([(float(x), float(y)) for x, y in points]) \
            .minimum_rotated_rectangle
    except Exception:
        return None
    coords = list(getattr(getattr(rect, "exterior", None), "coords", []) or [])
    if len(coords) < 5:
        return None
    p0, p1, p2 = coords[0], coords[1], coords[2]
    e1 = math.dist(p0, p1)
    e2 = math.dist(p1, p2)
    if e1 >= e2:
        long_, short, a, b = e1, e2, p0, p1
    else:
        long_, short, a, b = e2, e1, p1, p2
    angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    c = rect.centroid
    return ((c.x, c.y), angle, long_, short)


#: Opening geometry closer together than this is one symbol, in metres. A
#: window's two or three glazing lines are a few tens of millimetres apart; the
#: next window along is at least a pier away.
CLUSTER_GAP = 0.32


def _prim_axis(p: Prim) -> Optional[float]:
    """The direction a primitive runs in, degrees in [0, 180), if it has one."""
    if len(p.points) == 2 and not p.closed:
        (x0, y0), (x1, y1) = p.points
        if math.dist((x0, y0), (x1, y1)) < 1e-9:
            return None
        return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
    box = _oriented_box(p.points) if len(p.points) >= 3 else None
    if box is None or box[2] < 1.5 * max(box[3], 1e-9):
        return None
    return box[1]


def _cluster_by_proximity(prims: Sequence[Prim], gap: float) -> List[List[Prim]]:
    """Group primitives that are genuinely close and run the same way.

    Distance is measured between the geometries themselves. Bounding boxes
    were used before, and on a plan drawn at 30 degrees a thin diagonal
    window's box is big enough to reach the windows on the next wall and
    round the corner — seven windows fused into one square and none of them
    was read. Two marks that clearly run in different directions are never
    one symbol either: a corner has a window on each wall, not one window.
    """
    if not prims:
        return []
    from shapely.geometry import LineString, Point, Polygon
    from shapely.strtree import STRtree

    geoms = []
    for p in prims:
        if len(p.points) == 1:
            geoms.append(Point(p.points[0]))
        elif p.closed and len(p.points) >= 3:
            poly = Polygon(p.points)
            geoms.append(poly if poly.is_valid else LineString(p.points + [p.points[0]]))
        else:
            geoms.append(LineString(p.points))
    axes = [_prim_axis(p) for p in prims]
    parent = list(range(len(prims)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tree = STRtree(geoms)
    for i, g in enumerate(geoms):
        for j in tree.query(g.buffer(gap)):
            j = int(j)
            if j <= i:
                continue
            a, b = axes[i], axes[j]
            if a is not None and b is not None and \
                    20.0 < abs(((a - b) % 180)) < 160.0:
                # Allow the jamb line across the end of a glazing band: it is
                # no longer than the wall is thick and meets the band, rather
                # than running beside it. Bounding it by the cluster gap left
                # the jambs of a 417 mm wall to pair with each other across
                # every pier, as a window the width of the wall.
                short = min(prims[i].length, prims[j].length)
                if short > max(gap, BAND_THICKNESS_BAND[1]):
                    continue
            if g.distance(geoms[j]) <= gap:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups: Dict[int, List[Prim]] = {}
    for i, p in enumerate(prims):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


def _band_evidence(drawing: CadDrawing, skip: Optional[set] = None) -> List[Evidence]:
    """Openings as thin rectangles that span a wall.

    Clusters before measuring, because the same opening is drawn three
    different ways depending on the office. A header is one closed rectangle.
    A window is commonly two or three parallel glazing lines with a jamb line
    at each end — no rectangle anywhere, but the *group* of them is exactly
    the rectangle. Measuring per entity finds the first and misses the second
    entirely, which is how a plan with 24 windows yielded no window evidence
    and then failed to close a single room.
    """
    out: List[Evidence] = []
    roles = (C.STRUCTURE_ABOVE, C.OPENING, C.DOOR, C.WINDOW)
    lo_w, hi_w = WIDTH_BAND
    lo_b, hi_b = BAND_THICKNESS_BAND

    by_role: Dict[str, List[Prim]] = {}
    for p in drawing.prims:
        # A door symbol's leaf lies right beside the header band of the same
        # door. Clustered together they make a square, which reads as a
        # symbol rather than a span, and the band is lost. The leaf has
        # already said what it has to say as part of its symbol.
        if skip and p.id in skip:
            continue
        if p.role in roles and len(p.points) >= 2 and p.dxftype != "ARC":
            by_role.setdefault(p.role, []).append(p)

    for role, prims in by_role.items():
        groups = [sub for group in _cluster_by_proximity(prims, CLUSTER_GAP)
                  for sub in _split_along(group)]
        for group in groups:
            pts = [q for p in group for q in p.points]
            box = _oriented_box(pts)
            if box is None:
                continue
            centre, angle, long_, short = box
            if not (lo_w <= long_ <= hi_w) or not (lo_b <= short <= hi_b):
                continue
            if short > long_ * 0.85:
                continue        # a square is a symbol, not a span
            if role == C.DOOR and short < MIN_DOOR_BAND:
                continue        # a drawn door leaf, which says nothing about where the hole is
            first = group[0]
            kind, conf = _kind_from_names(first.layer, first.block, role)
            out.append(Evidence(
                id="e_%s" % first.id, centre=centre, angle=angle, width=long_,
                band=short, kind=kind, confidence=conf,
                source="band:%s" % role, layer=first.layer,
                source_ids=sorted(p.id for p in group),
            ))
    return out


#: A swing is hinged at an opening's end when its centre lies this close to
#: it, in metres — a hinge drawn on the wall face rather than the centreline.
SWING_HINGE_REACH = 0.25

#: A door-layer band thinner than this is a door leaf, not a header, in
#: metres: a header spans its wall, and no wall is this thin.
MIN_DOOR_BAND = 0.055

#: Along a wall, line work of one cluster separated by more than this is two
#: openings, in metres. A window's glazing lines and jambs overlap along the
#: wall; two windows either side of a mullion pier do not.
SPLIT_GAP = 0.05


def _split_along(group: List[Prim]) -> List[List[Prim]]:
    """Split a cluster where its line work leaves a gap along the wall.

    Proximity clusters a window's glazing lines with its jambs, and it also
    clusters two windows a 100 mm pier apart — which then read as one window
    of their combined width, centred on neither.
    """
    if len(group) < 2:
        return [group]
    pts = [q for p in group for q in p.points]
    box = _oriented_box(pts)
    if box is None:
        return [group]
    r = math.radians(box[1])
    d = (math.cos(r), math.sin(r))
    spans = []
    for p in group:
        ts = [q[0] * d[0] + q[1] * d[1] for q in p.points]
        spans.append((min(ts), max(ts), p))
    spans.sort(key=lambda s: s[0])
    out: List[List[Prim]] = [[spans[0][2]]]
    reach = spans[0][1]
    for lo, hi, p in spans[1:]:
        if lo > reach + SPLIT_GAP:
            out.append([p])
        else:
            out[-1].append(p)
        reach = max(reach, hi)
    return [unit for sub in out for unit in _split_frames(sub, d)]


def _split_frames(group: List[Prim], d: XY) -> List[List[Prim]]:
    """Split a run of window units drawn frame against frame.

    Two units mulled together leave no gap between them, but each is drawn
    as its own closed frame, and the frames meet end to end rather than
    overlapping. A header over a window is also a closed rectangle, but it
    overlaps the window's span, so it never splits one.
    """
    frames = []
    for p in group:
        if p.closed and len(p.points) >= 4:
            ts = [q[0] * d[0] + q[1] * d[1] for q in p.points]
            if max(ts) - min(ts) >= 0.3:
                frames.append((min(ts), max(ts)))
    if len(frames) < 2:
        return [group]
    frames.sort()
    cuts = []
    for (a0, a1), (b0, b1) in zip(frames, frames[1:]):
        if b0 < a1 - SPLIT_OVERLAP:
            return [group]          # overlapping frames: one opening, drawn twice
        cuts.append((a1 + b0) / 2.0)
    units: List[List[Prim]] = [[] for _ in range(len(cuts) + 1)]
    for p in group:
        ts = [q[0] * d[0] + q[1] * d[1] for q in p.points]
        lo, hi = min(ts), max(ts)
        k = sum(1 for c in cuts if (lo + hi) / 2.0 > c)
        if any(lo < c - SPLIT_OVERLAP and hi > c + SPLIT_OVERLAP for c in cuts):
            return [group]          # a line runs across the joint: one opening
        units[k].append(p)
    return [u for u in units if u]


#: Frames overlapping by no more than this along the wall meet end to end, in
#: metres: a shared jamb line drawn twice.
SPLIT_OVERLAP = 0.02


def _kind_from_names(layer: str, block: Optional[str], role: str
                     ) -> Tuple[str, float]:
    """Read door/window/garage out of a layer or block name."""
    text = "%s %s" % (C.normalise_layer(layer or ""), (block or "").upper())
    if "GARAGE" in text or "OHD" in text or "OVERHEAD" in text:
        return "garage", 0.9
    if any(k in text for k in ("GLAZ", "WIND", "WDW", "GLASS", "FENSTER",
                               "VENTANA", "SASH")):
        return "window", 0.9
    if "DOOR" in text or "DR-" in text or text.endswith(" DR"):
        return "door", 0.9
    if role == C.WINDOW:
        return "window", 0.85
    if role == C.DOOR:
        return "door", 0.85
    return "unknown", 0.35


def _arc_evidence(drawing: CadDrawing, skip: Optional[set] = None) -> List[Evidence]:
    """Door swings. The radius is the leaf width, the centre is the hinge.

    A quarter-circle drawn on an openings or door layer is the single most
    reliable door marker in architectural drafting, and it also gives the
    swing direction for free. Arcs already read as a complete door symbol
    (``skip``) have a known doorway chord and are not offered twice.
    """
    out: List[Evidence] = []
    lo_w, hi_w = WIDTH_BAND
    for a in drawing.arcs:
        if skip and a.id in skip:
            continue
        if a.role not in (C.OPENING, C.DOOR, C.WALL):
            continue
        if not (lo_w <= a.radius <= hi_w):
            continue
        sweep = a.sweep_deg
        if not (55.0 <= sweep <= 190.0):
            continue
        # The opening runs from the hinge towards the arc's start point: that
        # chord is the closed-door position.
        s = a.start_point
        e = a.end_point
        # A swing has two chords from the hinge — one lies in the wall (the
        # closed door) and one is where the leaf ends up (open). Which is
        # which cannot be known without the walls, so both are emitted as one
        # group and the wall matching later picks whichever actually lies in a
        # wall. Emitting only one would be a coin flip on every door.
        for tip, which in ((s, "s"), (e, "e")):
            angle = math.degrees(math.atan2(tip[1] - a.centre[1],
                                            tip[0] - a.centre[0])) % 180.0
            mid = ((a.centre[0] + tip[0]) / 2.0, (a.centre[1] + tip[1]) / 2.0)
            out.append(Evidence(
                id="e_%s_%s" % (a.id, which), centre=mid,
                angle=angle, width=a.radius, band=0.12, kind="door",
                confidence=0.75, source="swing", layer=a.layer,
                source_ids=[a.id], swing="left", group="g_%s" % a.id,
            ))
    return out


def _block_evidence(drawing: CadDrawing) -> List[Evidence]:
    """Door and window symbols placed as blocks."""
    out: List[Evidence] = []
    lo_w, hi_w = WIDTH_BAND
    for ins in drawing.inserts:
        kind, conf = _kind_from_names(ins.layer, ins.name, ins.role)
        if kind == "unknown":
            continue
        if not ins.extents:
            continue
        x0, y0, x1, y1 = ins.extents
        w, h = x1 - x0, y1 - y0
        long_, short = max(w, h), min(w, h)
        if not (lo_w <= long_ <= hi_w):
            continue
        if short > long_ * 0.62:
            # A door block's extents enclose its swing, so the box is square
            # and says nothing about which way the opening runs. The arc
            # inside the block is read separately and carries that answer;
            # guessing an orientation here would place half the doors across
            # the wrong wall.
            continue
        angle = 0.0 if w >= h else 90.0
        out.append(Evidence(
            id="e_%s" % ins.id, centre=((x0 + x1) / 2.0, (y0 + y1) / 2.0),
            angle=angle, width=long_, band=max(short, 0.1), kind=kind,
            confidence=conf * 0.9, source="block", layer=ins.layer,
            source_ids=[ins.id],
        ))
    return out


#: A door leaf is this wide, in metres: a narrow cupboard door at the low end,
#: a wide single leaf at the top. Double doors are two symbols.
LEAF_BAND = (0.5, 1.35)

#: Roles whose arcs are never door swings, whatever their shape: a chair back,
#: a basin, a round table, a planting bed, a dimension arc.
_NOT_SWING_ROLES = frozenset({
    C.FURNITURE, C.FIXTURE, C.CASEWORK, C.LANDSCAPE, C.ELECTRICAL,
    C.DIMENSION, C.ANNOTATION, C.STAIR, C.TITLE_BLOCK, C.GRID, C.HATCH,
    C.CONSTRUCTION, C.ROOM_LABEL,
})


def _grid_index(items: Sequence, key, cell: float) -> Dict[Tuple[int, int], List[int]]:
    grid: Dict[Tuple[int, int], List[int]] = {}
    for i, it in enumerate(items):
        x, y = key(it)
        grid.setdefault((int(math.floor(x / cell)), int(math.floor(y / cell))), []).append(i)
    return grid


def _near(grid, p: XY, cell: float, reach: int = 1) -> List[int]:
    cx, cy = int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell))
    out: List[int] = []
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 1):
            out.extend(grid.get((cx + dx, cy + dy), ()))
    return out


def _leaf_axes(p: Prim) -> List[Tuple[XY, XY]]:
    """The straight leaf a primitive could be: its segment, or a thin rectangle's axis."""
    if not p.closed and len(p.points) == 2:
        return [(p.points[0], p.points[1])]
    if p.closed and len(p.points) == 4:
        a, b, c, d = p.points
        e1, e2 = math.dist(a, b), math.dist(b, c)
        if min(e1, e2) <= 0.08 and max(e1, e2) >= 0.3:
            if e1 < e2:     # a-b is a short side
                return [(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2),
                         ((c[0] + d[0]) / 2, (c[1] + d[1]) / 2))]
            return [(((b[0] + c[0]) / 2, (b[1] + c[1]) / 2),
                     ((d[0] + a[0]) / 2, (d[1] + a[1]) / 2))]
    return []


def _symbol_evidence(drawing: CadDrawing) -> Tuple[List[Evidence], set]:
    """Door symbols recognised by their shape: a quarter swing and its leaf.

    The drafted door is an arc of about ninety degrees whose radius is the leaf
    width, and a straight leaf of the same length from the arc's centre to one
    of its ends. That pairing is specific — a chair back is a half circle, a
    basin a closed curve, a quarter-round counter has no leaf — so it names a
    door even when the symbol sits in an anonymous block on layer ``0``, which
    is where real drawings put it and where layer and block names say nothing.

    The leaf also settles which chord is the doorway: the leaf shows the door
    open, so the doorway runs from the hinge to the *other* end of the swing.
    """
    lo, hi = LEAF_BAND
    arcs = [a for a in drawing.arcs
            if a.role not in _NOT_SWING_ROLES and lo <= a.radius <= hi
            and 75.0 <= a.sweep_deg <= 105.0]
    if not arcs:
        return [], set()
    cell = 0.5
    leaves = []
    for p in drawing.prims:
        if p.role in _NOT_SWING_ROLES or p.dxftype == "ARC":
            continue
        for axis in _leaf_axes(p):
            if lo * 0.9 <= math.dist(*axis) <= hi * 1.1:
                leaves.append((p, axis))
    grid: Dict[Tuple[int, int], List[int]] = {}
    for i, (_p, (s, e)) in enumerate(leaves):
        for q in (s, e):
            grid.setdefault((int(math.floor(q[0] / cell)), int(math.floor(q[1] / cell))), []).append(i)

    out: List[Evidence] = []
    used: set = set()
    for a in arcs:
        tol = max(0.03, 0.06 * a.radius)
        ends = (a.start_point, a.end_point)
        best = None
        for i in set(_near(grid, a.centre, cell)):
            p, (s, e) = leaves[i]
            for hinge, tip in ((s, e), (e, s)):
                if math.dist(hinge, a.centre) > tol:
                    continue
                if abs(math.dist(hinge, tip) - a.radius) > max(0.05, 0.1 * a.radius):
                    continue
                k = min(range(2), key=lambda j: math.dist(tip, ends[j]))
                if math.dist(tip, ends[k]) > max(0.06, 0.12 * a.radius):
                    continue
                score = math.dist(hinge, a.centre) + math.dist(tip, ends[k])
                if best is None or score < best[0]:
                    best = (score, p, 1 - k)
        if best is None:
            continue
        _score, leaf, closed_end = best
        tip = ends[closed_end]
        angle = math.degrees(math.atan2(tip[1] - a.centre[1], tip[0] - a.centre[0])) % 180.0
        opened = ends[1 - closed_end]
        ol = math.dist(opened, a.centre) or 1.0
        used.add(a.id)
        out.append(Evidence(
            id="e_sym_%s" % a.id,
            centre=((a.centre[0] + tip[0]) / 2.0, (a.centre[1] + tip[1]) / 2.0),
            angle=angle, width=a.radius, band=0.12, kind="door", confidence=0.85,
            source="symbol", layer=a.layer, source_ids=[a.id, leaf.id],
            swing="left" if closed_end == 0 else "right",
            wall_side=((a.centre[0] - opened[0]) / ol, (a.centre[1] - opened[1]) / ol)))
    return _pair_double_doors(out), used


def _pair_double_doors(leaves: List[Evidence]) -> List[Evidence]:
    """Two leaves on one line whose closed positions meet are one double door.

    A pair of doors is drafted as two swings hinged at the outer jambs; their
    closed chords lie end to end on the wall line. Left as two openings, each
    half-width opening sits a quarter of the doorway away from its centre and
    neither is where the door is.
    """
    done = set()
    out: List[Evidence] = []
    for i, a in enumerate(leaves):
        if i in done:
            continue
        partner = None
        for j in range(i + 1, len(leaves)):
            b = leaves[j]
            if j in done or abs(((a.angle - b.angle + 90) % 180) - 90) > 5.0:
                continue
            d = a.direction
            n = (-d[1], d[0])
            dx, dy = b.centre[0] - a.centre[0], b.centre[1] - a.centre[1]
            if abs(dx * n[0] + dy * n[1]) > 0.1:
                continue
            gap = abs(dx * d[0] + dy * d[1]) - (a.width + b.width) / 2.0
            if -0.1 <= gap <= 0.12:
                partner = j
                break
        if partner is None:
            out.append(a)
            continue
        b = leaves[partner]
        done.update((i, partner))
        pa, pb = a.span(), b.span()
        ends = sorted([pa[0], pa[1], pb[0], pb[1]],
                      key=lambda p: p[0] * a.direction[0] + p[1] * a.direction[1])
        s, e = ends[0], ends[-1]
        out.append(Evidence(
            id="%s_%s" % (a.id, b.id.split("_")[-1]),
            centre=((s[0] + e[0]) / 2.0, (s[1] + e[1]) / 2.0),
            angle=a.angle, width=math.dist(s, e), band=max(a.band, b.band),
            kind="door", confidence=max(a.confidence, b.confidence),
            source="symbol", layer=a.layer, source_ids=a.source_ids + b.source_ids,
            swing="double", wall_side=_common_side(a.wall_side, b.wall_side)))
    return out


def _common_side(a: Optional[XY], b: Optional[XY]) -> Optional[XY]:
    """The wall side two leaves agree on; none when they swing opposite ways."""
    if a is None or b is None or a[0] * b[0] + a[1] * b[1] < 0.9:
        return None
    return a


#: A jamb marker is a small mark at the end of an opening: no longer than this
#: across the wall and no longer than ``JAMB_ALONG`` along it, in metres.
JAMB_ACROSS = 0.40
JAMB_ALONG = 0.20

#: Two jambs this far apart, centre to centre, may frame one opening.
JAMB_SPAN = (0.5, 2.2)


def _jamb_evidence(drawing: CadDrawing) -> List[Evidence]:
    """Openings framed by a pair of jamb marks, one at each side of the gap.

    Many offices draw a door not as a swing but as two small frame marks on
    the door layer, facing each other across the hole in the wall. Neither
    mark is an opening; the *pair* is, and its span is the opening width.

    Pairing is deliberately strict, because a wrong pair is a false door:

    * the two marks are mutually nearest among compatible marks;
    * each mark's long axis runs across the pair's axis — they face each
      other rather than sitting side by side;
    * the space between them is clear of wall line work. A pier between two
      doors has wall lines along it, so the jamb of one door and the jamb of
      the next can never be read as a door in the pier.
    """
    roles = (C.DOOR, C.WINDOW, C.OPENING)
    marks = []
    for p in drawing.prims:
        if p.role not in roles or len(p.points) < 2:
            continue
        box = _oriented_box(p.points) if len(p.points) >= 3 else None
        if box is None:
            if len(p.points) != 2:
                continue
            a, b = p.points
            length = math.dist(a, b)
            if not (0.05 <= length <= JAMB_ACROSS):
                continue
            centre = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
            marks.append((p, centre, angle, length, 0.0))
            continue
        centre, angle, long_, short = box
        if long_ > JAMB_ACROSS or short > JAMB_ALONG or long_ < 0.03:
            continue
        if long_ < short * 1.4:
            angle = None            # a square mark has no facing of its own
        marks.append((p, centre, angle, long_, short))
    if len(marks) < 2:
        return []

    walls = [s for p in drawing.wall_prims() for s in p.segments]
    wall_tree = None
    if walls:
        from shapely.geometry import LineString
        from shapely.strtree import STRtree
        wall_lines = [LineString(s) for s in walls]
        wall_tree = STRtree(wall_lines)

    def clear_between(m1, m2, axis: float) -> bool:
        if wall_tree is None:
            return True
        from shapely.geometry import LineString
        (x1, y1), (x2, y2) = m1[1], m2[1]
        d = (math.cos(math.radians(axis)), math.sin(math.radians(axis)))
        inset1 = m1[4] / 2.0 + 0.02
        inset2 = m2[4] / 2.0 + 0.02
        s = (x1 + d[0] * inset1, y1 + d[1] * inset1)
        e = (x2 - d[0] * inset2, y2 - d[1] * inset2)
        gap = math.dist(s, e)
        if gap <= 0.1:
            return False
        across = max(m1[3], m2[3]) / 2.0 + 0.02
        corridor = LineString([s, e]).buffer(across, cap_style=2)
        covered = 0.0
        for k in wall_tree.query(corridor):
            seg = wall_lines[int(k)]
            sa = math.degrees(math.atan2(seg.coords[1][1] - seg.coords[0][1],
                                         seg.coords[1][0] - seg.coords[0][0])) % 180.0
            if abs(((sa - axis + 90) % 180) - 90) > 10.0:
                continue
            covered = max(covered, seg.intersection(corridor).length)
        return covered < 0.35 * gap

    def face_ends_at(m, axis: float, away: float) -> bool:
        """The wall stops at this mark: a face ends on each side of it.

        A jamb is where a wall stops. Both faces of the wall end at it and
        carry on away from the gap, one either side of the jamb, because the
        jamb stands inside the wall's thickness. Two tick marks inside a door
        symbol, or a pair of tags in a room, have at most a wall on one side,
        however well they face each other.
        """
        if wall_tree is None:
            return False
        from shapely.geometry import Point
        d = (math.cos(math.radians(axis)), math.sin(math.radians(axis)))
        # A frame mark need not sit flush with the end of the wall line: a
        # frame is often drawn a little inside the structural opening.
        along_tol = m[4] / 2.0 + 0.2
        across_tol = 0.35               # half the thickest wall a jamb frames
        probe = Point(m[1]).buffer(along_tol + across_tol)
        sides = set()
        for k in wall_tree.query(probe):
            (x0, y0), (x1, y1) = wall_lines[int(k)].coords
            sa = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
            if abs(((sa - axis + 90) % 180) - 90) > 10.0:
                continue
            for (ex, ey), (fx, fy) in (((x0, y0), (x1, y1)), ((x1, y1), (x0, y0))):
                vx, vy = ex - m[1][0], ey - m[1][1]
                t = vx * d[0] + vy * d[1]
                perp = -vx * d[1] + vy * d[0]
                if abs(t) > along_tol or abs(perp) > across_tol or abs(perp) < 0.01:
                    continue
                # The rest of the face must lie away from the gap.
                if ((fx - ex) * d[0] + (fy - ey) * d[1]) * away > 0.05:
                    sides.add(perp > 0)
        return len(sides) == 2

    def abuts_wall(m, axis: float, away: float) -> bool:
        """The gap ends against a wall crossing its line, just past this mark.

        A door beside a corner has no wall *continuing* beyond one jamb: the
        opening runs up to the face of the wall it meets. That face crosses the
        opening's line within the mark's reach and spans the mark.
        """
        if wall_tree is None:
            return False
        from shapely.geometry import LineString, Point
        d = (math.cos(math.radians(axis)), math.sin(math.radians(axis)))
        n = (-d[1], d[0])
        reach = m[4] / 2.0 + 0.2
        half = max(m[3] / 2.0, 0.05)
        probe = Point(m[1]).buffer(reach + half)
        for k in wall_tree.query(probe):
            seg = wall_lines[int(k)]
            (x0, y0), (x1, y1) = seg.coords
            sa = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
            if abs(((sa - axis) % 180) - 90) > 10.0:
                continue
            mid = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
            t = (mid[0] - m[1][0]) * d[0] + (mid[1] - m[1][1]) * d[1]
            if not (-0.01 <= t * away <= reach):
                continue
            across = LineString([(m[1][0] + d[0] * t - n[0] * half, m[1][1] + d[1] * t - n[1] * half),
                                 (m[1][0] + d[0] * t + n[0] * half, m[1][1] + d[1] * t + n[1] * half)])
            if seg.buffer(0.01).intersection(across).length >= 1.6 * half:
                return True
        return False

    cell = JAMB_SPAN[1]
    grid = _grid_index(marks, lambda m: m[1], cell)
    lo, hi = JAMB_SPAN

    def compatible(i: int, j: int) -> Optional[float]:
        a, b = marks[i], marks[j]
        if a[0].layer != b[0].layer:
            return None
        dist = math.dist(a[1], b[1])
        if not (lo <= dist <= hi):
            return None
        axis = math.degrees(math.atan2(b[1][1] - a[1][1], b[1][0] - a[1][0])) % 180.0
        for m in (a, b):
            if m[2] is not None and abs(((m[2] - axis) % 180) - 90) > 15.0:
                return None
        if a[2] is None and b[2] is None:
            return None             # two squares say nothing about facing
        return dist

    # Clearance is tested before nearness. The inner jambs of two doors a pier
    # apart are each other's nearest mark; rejecting that pair only after
    # choosing it would leave both real doors unpaired.
    clear: Dict[Tuple[int, int], bool] = {}

    def is_clear(i: int, j: int) -> bool:
        key = (i, j) if i < j else (j, i)
        if key not in clear:
            a, b = marks[key[0]], marks[key[1]]
            raw = math.degrees(math.atan2(b[1][1] - a[1][1], b[1][0] - a[1][0]))
            axis = raw % 180.0
            # ``away`` for a is back along the axis from b; for b, onward.
            sign = 1.0 if abs(((raw - axis) + 180) % 360 - 180) < 90 else -1.0
            ends_a = face_ends_at(a, axis, -sign)
            ends_b = face_ends_at(b, axis, sign)
            # At least one jamb is a true wall end; the other may be where the
            # opening meets a crossing wall — a door beside a corner.
            bounded = (ends_a and ends_b) or \
                (ends_a and abuts_wall(b, axis, sign)) or \
                (ends_b and abuts_wall(a, axis, -sign))
            clear[key] = bounded and clear_between(a, b, axis)
        return clear[key]

    nearest: Dict[int, Tuple[float, int]] = {}
    for i, m in enumerate(marks):
        for j in _near(grid, m[1], cell):
            if j == i:
                continue
            dist = compatible(i, j)
            if dist is None:
                continue
            if i in nearest and dist >= nearest[i][0]:
                continue
            if not is_clear(i, j):
                continue
            nearest[i] = (dist, j)

    out: List[Evidence] = []
    for i, (dist, j) in nearest.items():
        if j <= i or nearest.get(j, (None, None))[1] != i:
            continue
        a, b = marks[i], marks[j]
        axis = math.degrees(math.atan2(b[1][1] - a[1][1], b[1][0] - a[1][0])) % 180.0
        width = dist - (a[4] + b[4]) / 2.0
        if width < WIDTH_BAND[0]:
            continue
        kind, conf = _kind_from_names(a[0].layer, a[0].block, a[0].role)
        out.append(Evidence(
            id="e_jmb_%s_%s" % (a[0].id, b[0].id),
            centre=((a[1][0] + b[1][0]) / 2.0, (a[1][1] + b[1][1]) / 2.0),
            angle=axis, width=width, band=max(a[3], b[3]), kind=kind,
            confidence=min(conf, 0.8), source="jambs", layer=a[0].layer,
            source_ids=[a[0].id, b[0].id]))
    return out


def collect_evidence(drawing: CadDrawing) -> List[Evidence]:
    """All positive opening evidence in the drawing, merged where it coincides.

    Runs before wall reconstruction, because its output is what lets the wall
    builder bridge the gaps that openings cut in the line work.
    """
    symbols, used_arcs = _symbol_evidence(drawing)
    leaves = {sid for e in symbols for sid in e.source_ids} - used_arcs
    ev = (_band_evidence(drawing, skip=leaves) + symbols
          + _arc_evidence(drawing, skip=used_arcs)
          + _block_evidence(drawing) + _jamb_evidence(drawing))
    return _merge_evidence(ev)


def _same_opening(a: Evidence, b: Evidence) -> bool:
    """Whether two pieces of evidence describe one opening.

    The test is overlap, not proximity. An earlier version compared centre
    distances against a radius scaled by width, which merged a 1.83 m window
    with a 4.5 m porch beam two and a half metres away on a different wall —
    and the window's evidence, once absorbed, stopped bridging its own wall.
    Two rectangles are the same opening only if they lie on the same line and
    their spans genuinely overlap.
    """
    if abs(((a.angle - b.angle + 90) % 180) - 90) > 15.0:
        return False
    d = a.direction
    n = (-d[1], d[0])
    dx, dy = b.centre[0] - a.centre[0], b.centre[1] - a.centre[1]
    perp = abs(dx * n[0] + dy * n[1])
    if perp > max(a.band, b.band) / 2.0 + 0.18:
        return False
    along = abs(dx * d[0] + dy * d[1])
    overlap = (a.width + b.width) / 2.0 - along
    return overlap >= 0.4 * min(a.width, b.width)


def _merge_evidence(ev: Sequence[Evidence]) -> List[Evidence]:
    """Fuse evidence describing the same opening.

    A header and a swing arc at the same doorway must not become two doors.
    The merged record keeps the band evidence's geometry — it is measured from
    the wall itself — and the arc's kind, which is the better classifier.
    """
    order = sorted(ev, key=lambda e: (-e.confidence, -e.width))
    kept: List[Evidence] = []
    for e in order:
        host = None
        for k in kept:
            if _same_opening(e, k):
                host = k
                break
        if host is None:
            kept.append(e)
            continue
        if e.group != host.group:
            host.group = host.group or e.group
        host.source_ids.extend(e.source_ids)
        host.source = "%s+%s" % (host.source, e.source)
        # A header bears on the piers either side of the hole it spans, so it
        # is never narrower than the opening and is often wider — the garage
        # front's beam runs the whole elevation. When a more specific piece of
        # evidence disagrees about the width, the narrower one is the opening.
        if e.width < host.width and not e.source.startswith("band:structure"):
            host.centre, host.angle, host.width = e.centre, e.angle, e.width
            host.band = max(host.band, e.band)
        if host.kind in ("unknown", "cased") and e.kind not in ("unknown",):
            host.kind = e.kind
            host.confidence = max(host.confidence, e.confidence)
        elif host.kind == e.kind:
            host.confidence = min(0.99, host.confidence + 0.1)
        if e.swing and not host.swing:
            host.swing = e.swing
        if e.wall_side and not host.wall_side:
            host.wall_side = e.wall_side
    return kept


def bridges(evidence: Sequence[Evidence], pad: float = 0.06) -> List:
    """Evidence rectangles as polygons, for the wall builder to join across."""
    out = []
    for e in evidence:
        try:
            out.append(e.bridge_box(pad))
        except Exception:
            continue
    return out


def through_spans(evidence: Sequence[Evidence], pad: float = 0.06) -> List:
    """Openings that certainly lie along a wall, for faces to continue through.

    A window drawn right up to an inside corner leaves the wall's inner face
    with no line work beyond it, so there is no gap to bridge — the face just
    stops. The window says the wall carries on to the corner. Only evidence
    that is itself a span of wall counts: a band, a jamb pair, a symbol whose
    doorway chord is known. A bare swing arc offers two chords, one of them
    pointing into the room, and a face must not follow that one.

    Each span is ``(polygon, angle)``: only faces running along the opening
    continue through it. The jamb lines at a doorway's ends lie inside its
    region too, across the wall, and continued they pair into a wall as wide
    as the door.
    """
    out = []
    for e in evidence:
        if e.group:
            continue
        try:
            out.append((e.bridge_box(pad) if e.wall_side else e.box(pad), e.angle))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Matching evidence onto walls
# ---------------------------------------------------------------------------

def _host_for(e: Evidence, walls: Sequence[Wall]
              ) -> Optional[Tuple[Wall, float, float]]:
    """The wall this opening sits in, the offset along it, and how well it fits.

    The fit score is how far the evidence sits off the centreline, less a
    small bonus for length. It is what decides, for a door swing, which of the
    two chords is the doorway: the one lying in a wall scores, the one
    pointing into the room finds no host at all.
    """
    best: Optional[Tuple[float, Wall, float]] = None
    for w in walls:
        if w.length < 1e-9:
            continue
        if abs(((e.angle - w.angle_deg + 90) % 180) - 90) > HOST_ANGLE_TOL:
            continue
        d = w.direction
        vx, vy = e.centre[0] - w.start[0], e.centre[1] - w.start[1]
        t = vx * d[0] + vy * d[1]
        if t < -0.15 or t > w.length + 0.15:
            continue
        perp = abs(-vx * d[1] + vy * d[0])
        if perp > max(w.thickness * HOST_DISTANCE_RATIO, 0.12):
            continue
        score = perp - 0.01 * min(e.width, w.length)
        if best is None or score < best[0]:
            best = (score, w, max(0.0, min(w.length, t)))
    if best is None:
        return None
    return best[1], best[2], best[0]


class _KindIndex:
    """Opening-layer line work and swings, indexed once per drawing.

    :func:`_refine_kind` asks, for every opening, which glazing lines cross
    it and which swings sit beside it. Scanning the whole drawing each time
    was most of the openings stage on a 250-door clinic.
    """

    def __init__(self, drawing: CadDrawing):
        from shapely.geometry import LineString
        from shapely.strtree import STRtree
        self.segments: List[Tuple[float, float, object]] = []
        # Glazing is drawn on an opening or window layer. Counting wall and
        # header line work made every header read as glazed, because a header
        # rectangle's own two long edges are, unavoidably, a pair of lines
        # running the length of the opening.
        for p in drawing.prims:
            if p.role not in (C.OPENING, C.WINDOW, C.DOOR):
                continue
            for (x0, y0), (x1, y1) in p.segments:
                seg_len = math.dist((x0, y0), (x1, y1))
                if seg_len < 1e-9:
                    continue
                ang = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
                self.segments.append((seg_len, ang, LineString([(x0, y0), (x1, y1)])))
        self.tree = STRtree([s[2] for s in self.segments]) if self.segments else None
        self.arcs = [a for a in drawing.arcs if a.role in (C.OPENING, C.DOOR)]

    def parallel_lines(self, area, e: Evidence) -> int:
        if self.tree is None:
            return 0
        count = 0
        for i in sorted(int(k) for k in self.tree.query(area)):
            seg_len, ang, line = self.segments[i]
            if seg_len < e.width * 0.55:
                continue
            if abs(((ang - e.angle + 90) % 180) - 90) > 10.0:
                continue
            if area.intersects(line):
                count += 1
        return count

    def has_swing(self, area, e: Evidence) -> bool:
        """Whether a door swing belongs to this opening: hinged at one of its ends.

        A swing merely *near* an opening is not its swing. The entrance door
        beside a window swings past the window's end, and counting it turned
        the window into a door.
        """
        ends = e.span()
        reach = max(SWING_HINGE_REACH, e.band * 0.75 + 0.1)
        for a in self.arcs:
            if abs(a.radius - e.width) > max(0.12, e.width * 0.25):
                continue
            if min(math.dist(a.centre, p) for p in ends) <= reach:
                return True
        return False


def _refine_kind(e: Evidence, wall: Wall, drawing: CadDrawing,
                 index: Optional[_KindIndex] = None) -> Tuple[str, float]:
    """Settle door vs window vs cased opening from local drawing evidence.

    Glazing is drawn as two or three lines running the length of the opening;
    a door has a swing. When the drawing says neither, a cased opening is the
    honest answer rather than a guessed door.
    """
    if e.kind in ("garage",):
        return e.kind, e.confidence

    try:
        area = e.box(0.10)
    except Exception:
        return e.kind, e.confidence

    index = index or _KindIndex(drawing)
    parallel = index.parallel_lines(area, e)
    swing = index.has_swing(area, e)

    if swing:
        return "door", max(e.confidence, 0.85)
    if parallel >= 3:
        return "window", max(e.confidence, 0.8)
    if e.kind != "unknown":
        return e.kind, e.confidence
    if e.width >= GARAGE_MIN_WIDTH and wall.kind == "exterior":
        return "garage", 0.6
    # A wide hole in an interior wall is a cased opening between two rooms —
    # the way a kitchen opens onto a dining room. Calling it a garage door
    # because of its width alone put roller doors inside houses.
    return "cased", 0.5


def _heights(kind: str) -> Tuple[float, float]:
    if kind == "window":
        return WINDOW_HEAD - WINDOW_SILL, WINDOW_SILL
    if kind == "garage":
        return GARAGE_HEIGHT, 0.0
    if kind == "cased":
        return CASED_HEIGHT, 0.0
    return DOOR_HEIGHT, 0.0


#: Evidence families that say "door". Cues from the same family are one cue:
#: a header band and a door-layer band at one doorway are one statement, a
#: swing and a jamb pair are two.
_DOOR_CUES = {
    "swing": "swing", "symbol": "swing", "block": "block", "jambs": "jambs",
    "band:door": "door-layer", "band:opening": "opening-layer",
}


def _cue_families(e: Evidence) -> set:
    return {_DOOR_CUES[s] for s in e.source.split("+") if s in _DOOR_CUES}


def wall_gap(wall: Wall, lo: float, hi: float, faces) -> Optional[bool]:
    """Whether the drawing's own wall lines stop where this opening is.

    A doorway or a window is drawn as a break in both faces of its wall. The
    raw segments on the faces beside the wall — not the joined runs, which
    deliberately bridge openings — are measured across ``[lo, hi]``. ``None``
    means no face could be found to ask, which is the answer for a wall drawn
    as a single centreline.
    """
    if not faces or hi - lo <= 0.05:
        return None
    d = wall.direction
    n = wall.normal
    w_angle = math.degrees(math.atan2(d[1], d[0])) % 180.0
    # How far each face lies from the opening, measured at the opening along
    # the face's own normal. Comparing offsets from the drawing's origin
    # instead amplifies any angle between wall and face by the distance to
    # the origin: a wall snapped 0.8 degrees off its faces, 20 m out, read
    # its own faces as 0.28 m away and its doorway as unconfirmed.
    here = wall.point_at((lo + hi) / 2.0)
    coverages = []
    for f in faces:
        if abs(((f.angle - w_angle + 90) % 180) - 90) > 2.0:
            continue
        fn = f.normal
        rel = f.offset - (here[0] * fn[0] + here[1] * fn[1])
        if abs(abs(rel) - wall.thickness / 2.0) > 0.03:
            continue
        fd = f.direction
        pa, pb = wall.point_at(lo), wall.point_at(hi)
        ta = pa[0] * fd[0] + pa[1] * fd[1]
        tb = pb[0] * fd[0] + pb[1] * fd[1]
        t0, t1 = min(ta, tb), max(ta, tb)
        spans = sorted((max(r0, t0), min(r1, t1)) for r0, r1, _pid, _l in f.rows
                       if min(r1, t1) > max(r0, t0))
        covered = 0.0
        cursor = t0
        for a, b in spans:
            a = max(a, cursor)
            if b > a:
                covered += b - a
                cursor = b
        coverages.append(covered / (t1 - t0))
    if not coverages:
        return None
    return max(coverages) < 0.35


def classify_opening(kind: str, e: Optional[Evidence], gap: Optional[bool]) -> str:
    """The certainty tier of one opening; see :attr:`Opening.classification`.

    Precision first. A door needs two independent statements — door evidence
    and a second family of door evidence, or door evidence and a wall that is
    genuinely open there. One unconfirmed statement is a probable door. A hole
    that no evidence explains is an unknown opening, never a door.
    """
    if e is None:
        return "unknown_opening"
    if kind == "window":
        return "window"
    families = _cue_families(e)
    if kind == "garage":
        named = e.kind == "garage" or bool(families)
        return "garage_door" if named else "unknown_opening"
    if kind == "door":
        if len(families) >= 2 or (families and gap is True):
            return "door"
        if families or e.kind == "door":
            return "probable_door"
        return "unknown_opening"
    return "unknown_opening"


def assign(evidence: Sequence[Evidence], walls: Sequence[Wall],
           drawing: CadDrawing, faces=None) -> List[Opening]:
    """Turn evidence into openings hosted by actual walls.

    ``faces`` are the wall stage's faces; given them, each opening is checked
    against the drawing's own wall lines and classified accordingly.
    """
    out: List[Opening] = []
    n = 0
    claimed: Dict[str, List[Tuple[float, float]]] = {w.id: [] for w in walls}
    index: Optional[_KindIndex] = None

    # Resolve each evidence group down to its single best-hosted member first,
    # so a door swing contributes one doorway and not two at right angles.
    hosted: List[Tuple[Evidence, Wall, float]] = []
    best_of_group: Dict[str, Tuple[tuple, int]] = {}
    for e in evidence:
        host = _host_for(e, walls)
        if host is None:
            continue
        wall, offset, fit = host
        if e.group:
            # Which chord of a swing is the doorway: the one the drawing
            # corroborates — a wall that is really open there, more than one
            # kind of door evidence — before the one that merely sits closest
            # to a centreline. The open leaf lies along the neighbouring wall
            # and fits it well; choosing on fit alone cut the door there.
            gap = wall_gap(wall, offset - e.width / 2.0, offset + e.width / 2.0, faces)
            score = ({True: 0, None: 1, False: 2}[gap], -len(_cue_families(e)), fit)
            prev = best_of_group.get(e.group)
            if prev is not None and prev[0] <= score:
                continue
            if prev is not None:
                hosted[prev[1]] = (e, wall, offset)
                best_of_group[e.group] = (score, prev[1])
                continue
            best_of_group[e.group] = (score, len(hosted))
        hosted.append((e, wall, offset))

    for e, wall, offset in sorted(hosted, key=lambda h: -h[0].confidence):
        half = e.width / 2.0
        lo, hi = offset - half, offset + half
        if any(not (hi <= a + 0.05 or lo >= b - 0.05) for a, b in claimed[wall.id]):
            continue        # this stretch of wall already has an opening
        if index is None:
            index = _KindIndex(drawing)
        kind, conf = _refine_kind(e, wall, drawing, index)
        # Clamp inside the wall: an opening cannot be longer than its wall.
        lo = max(0.0, lo)
        hi = min(wall.length, hi)
        width = hi - lo
        if width < WIDTH_BAND[0]:
            continue
        claimed[wall.id].append((lo, hi))
        centre_t = (lo + hi) / 2.0
        height, sill = _heights(kind)
        gap = wall_gap(wall, lo, hi, faces)
        classification = classify_opening(kind, e, gap)
        n += 1
        op = Opening(
            id="o%d" % n, kind=kind, wall_id=wall.id,
            position=wall.point_at(centre_t), offset=round(centre_t, 4),
            width=round(width, 4), height=height, sill_height=sill,
            thickness=wall.thickness, swing=e.swing, source_ids=e.source_ids,
            evidence=e.source, confidence=round(min(0.99, conf), 3),
            classification=classification, gap_confirmed=gap,
        )
        out.append(op)
        wall.opening_ids.append(op.id)
    return out


def from_inferred(walls: Sequence[Wall],
                  spans: Sequence[Tuple[str, float, float]],
                  existing: Sequence[Opening]) -> List[Opening]:
    """Cut back out the doorways that wall repair closed over.

    :func:`modules.recon.walls.close_collinear_gaps` completes walls across
    doorway-sized gaps so rooms can close, and reports each gap. If those
    reports were dropped the model would gain a door-shaped slab of solid wall
    at every doorway the drawing left open — the room would be enclosed and
    unreachable. So each reported span becomes an opening here, unless
    positive evidence already put one there.
    """
    out: List[Opening] = []
    by_id = {w.id: w for w in walls}
    taken: Dict[str, List[Tuple[float, float]]] = {}
    for o in existing:
        taken.setdefault(o.wall_id, []).append(
            (o.offset - o.width / 2, o.offset + o.width / 2))

    n = len(existing)
    for wall_id, lo, hi in spans:
        w = by_id.get(wall_id)
        if w is None:
            continue
        lo, hi = max(0.0, min(lo, hi)), min(w.length, max(lo, hi))
        width = hi - lo
        if width < WIDTH_BAND[0] or width > WIDTH_BAND[1]:
            continue
        if any(not (hi <= a + 0.05 or lo >= b - 0.05)
               for a, b in taken.get(wall_id, ())):
            continue
        taken.setdefault(wall_id, []).append((lo, hi))
        n += 1
        centre_t = (lo + hi) / 2.0
        op = Opening(
            id="o%d" % n, kind="cased", wall_id=wall_id,
            position=w.point_at(centre_t), offset=round(centre_t, 4),
            width=round(width, 4), height=CASED_HEIGHT, sill_height=0.0,
            thickness=w.thickness, evidence="wall-gap", confidence=0.45,
            classification="unknown_opening", gap_confirmed=True,
        )
        out.append(op)
        w.opening_ids.append(op.id)
    return out


def from_gaps(walls: Sequence[Wall], faces, existing: Sequence[Opening],
              min_width: float = 0.6, max_width: float = 3.2) -> List[Opening]:
    """Openings nothing explains but the wall's own line work.

    A doorway drawn as nothing but a break in the wall — no swing, no header,
    no jambs — is still a doorway, and building solid wall across it seals the
    room. So every wall is scanned for a span where **both** of its drawn
    faces stop and start again, at a width a person passes through, with
    wall drawn on either side. Both faces, because one broken face is a
    partition teeing in or a drafting break; bounded on both sides, because a
    face that simply ends is the end of the wall.

    Faces are read from their raw segments, not from the joined runs: runs
    are joined across exactly these gaps so that walls come out whole.

    The result is an ``unknown_opening`` — a hole — and never a door: nothing
    says what, if anything, fills it.
    """
    from collections import defaultdict
    out: List[Opening] = []
    taken: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for o in existing:
        taken[o.wall_id].append((o.offset - o.width / 2, o.offset + o.width / 2))

    n = len(existing)
    for w in walls:
        if w.length < min_width + 0.4:
            continue
        d, nrm = w.direction, w.normal
        w_angle = math.degrees(math.atan2(d[1], d[0])) % 180.0
        here = w.point_at(w.length / 2.0)
        u0 = w.start[0] * d[0] + w.start[1] * d[1]
        sides: Dict[bool, List[Tuple[float, float]]] = {True: [], False: []}
        for f in faces or ():
            if abs(((f.angle - w_angle + 90) % 180) - 90) > 2.0:
                continue
            fn = f.normal
            sign = 1.0 if fn[0] * nrm[0] + fn[1] * nrm[1] >= 0 else -1.0
            # Distance from the wall's middle to the face, along the face's
            # normal and signed along the wall's (see wall_gap).
            rel = (f.offset - (here[0] * fn[0] + here[1] * fn[1])) * sign
            if abs(abs(rel) - w.thickness / 2.0) > 0.03:
                continue
            for r0, r1, _pid, _layer in f.rows:
                pa, pb = f.point(r0), f.point(r1)
                a = pa[0] * d[0] + pa[1] * d[1] - u0
                b = pb[0] * d[0] + pb[1] * d[1] - u0
                sides[rel > 0].append((min(a, b), max(a, b)))
        if not sides[True] or not sides[False]:
            continue

        def gaps_of(spans):
            merged = _merge_intervals(spans, 0.01)
            return [(a1, b0) for (_a0, a1), (b0, _b1) in zip(merged, merged[1:])
                    if b0 - a1 >= min_width * 0.9]

        for lo_p, hi_p in gaps_of(sides[True]):
            for lo_n, hi_n in gaps_of(sides[False]):
                lo, hi = max(lo_p, lo_n), min(hi_p, hi_n)
                if not (min_width <= hi - lo <= max_width):
                    continue
                if lo < 0.05 or hi > w.length - 0.05:
                    continue
                if any(not (hi <= a + 0.05 or lo >= b - 0.05) for a, b in taken[w.id]):
                    continue
                taken[w.id].append((lo, hi))
                n += 1
                centre_t = (lo + hi) / 2.0
                op = Opening(
                    id="o%d" % n, kind="cased", wall_id=w.id,
                    position=w.point_at(centre_t), offset=round(centre_t, 4),
                    width=round(hi - lo, 4), height=CASED_HEIGHT, sill_height=0.0,
                    thickness=w.thickness, evidence="gap", confidence=0.4,
                    classification="unknown_opening", gap_confirmed=True,
                )
                out.append(op)
                w.opening_ids.append(op.id)
    return out


def _merge_intervals(iv: Sequence[Tuple[float, float]], gap: float
                     ) -> List[Tuple[float, float]]:
    if not iv:
        return []
    iv = sorted(iv)
    out = [list(iv[0])]
    for a, b in iv[1:]:
        if a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _dedupe_spans(spans: Sequence[Tuple[float, float]]
                  ) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1] + 0.05:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def link_rooms(openings: Sequence[Opening], rooms, walls: Sequence[Wall]) -> None:
    """Record which rooms each opening connects.

    An opening's two sides are the rooms either side of its host wall, which
    is what makes the model navigable: it is the difference between "a hole"
    and "the way from the hall to bedroom 2".
    """
    try:
        from shapely.geometry import Point, Polygon
    except Exception:
        return
    polys = []
    for r in rooms:
        try:
            polys.append((r, Polygon(r.polygon, r.holes)))
        except Exception:
            continue
    by_id = {w.id: w for w in walls}
    for o in openings:
        w = by_id.get(o.wall_id)
        if w is None:
            continue
        n = w.normal
        reach = w.thickness / 2.0 + 0.30
        for sign in (1, -1):
            probe = Point(o.position[0] + n[0] * reach * sign,
                          o.position[1] + n[1] * reach * sign)
            for r, poly in polys:
                if poly.covers(probe) and r.id not in o.rooms:
                    o.rooms.append(r.id)
                    if o.id not in r.opening_ids:
                        r.opening_ids.append(o.id)
                    break
