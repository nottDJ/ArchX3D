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
from .read import Arc, Drawing, Prim

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


def _cluster_by_proximity(prims: Sequence[Prim], gap: float) -> List[List[Prim]]:
    """Group primitives whose bounding boxes lie within ``gap`` of each other."""
    boxes = []
    for p in prims:
        xs = [q[0] for q in p.points]
        ys = [q[1] for q in p.points]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    parent = list(range(len(prims)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(prims)):
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, len(prims)):
            bx0, by0, bx1, by1 = boxes[j]
            if ax0 - gap <= bx1 and bx0 - gap <= ax1 and \
                    ay0 - gap <= by1 and by0 - gap <= ay1:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups: Dict[int, List[Prim]] = {}
    for i, p in enumerate(prims):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


def _band_evidence(drawing: Drawing) -> List[Evidence]:
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
        if p.role in roles and len(p.points) >= 2 and p.dxftype != "ARC":
            by_role.setdefault(p.role, []).append(p)

    for role, prims in by_role.items():
        for group in _cluster_by_proximity(prims, CLUSTER_GAP):
            pts = [q for p in group for q in p.points]
            box = _oriented_box(pts)
            if box is None:
                continue
            centre, angle, long_, short = box
            if not (lo_w <= long_ <= hi_w) or not (lo_b <= short <= hi_b):
                continue
            if short > long_ * 0.85:
                continue        # a square is a symbol, not a span
            first = group[0]
            kind, conf = _kind_from_names(first.layer, first.block, role)
            out.append(Evidence(
                id="e_%s" % first.id, centre=centre, angle=angle, width=long_,
                band=short, kind=kind, confidence=conf,
                source="band:%s" % role, layer=first.layer,
                source_ids=sorted(p.id for p in group),
            ))
    return out


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


def _arc_evidence(drawing: Drawing) -> List[Evidence]:
    """Door swings. The radius is the leaf width, the centre is the hinge.

    A quarter-circle drawn on an openings or door layer is the single most
    reliable door marker in architectural drafting, and it also gives the
    swing direction for free.
    """
    out: List[Evidence] = []
    lo_w, hi_w = WIDTH_BAND
    for a in drawing.arcs:
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


def _block_evidence(drawing: Drawing) -> List[Evidence]:
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


def collect_evidence(drawing: Drawing) -> List[Evidence]:
    """All positive opening evidence in the drawing, merged where it coincides.

    Runs before wall reconstruction, because its output is what lets the wall
    builder bridge the gaps that openings cut in the line work.
    """
    ev = _band_evidence(drawing) + _arc_evidence(drawing) + _block_evidence(drawing)
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
    return kept


def bridges(evidence: Sequence[Evidence], pad: float = 0.06) -> List:
    """Evidence rectangles as polygons, for the wall builder to join across."""
    out = []
    for e in evidence:
        try:
            out.append(e.box(pad, band=e.bridge_band))
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


def _refine_kind(e: Evidence, wall: Wall, drawing: Drawing) -> Tuple[str, float]:
    """Settle door vs window vs cased opening from local drawing evidence.

    Glazing is drawn as two or three lines running the length of the opening;
    a door has a swing. When the drawing says neither, a cased opening is the
    honest answer rather than a guessed door.
    """
    if e.kind in ("garage",):
        return e.kind, e.confidence
    if e.width >= GARAGE_MIN_WIDTH and e.kind == "unknown":
        # A very wide opening in an exterior wall with no glazing and no swing
        # is a garage or a slider; "garage" only when the wall is a garage's.
        pass

    try:
        area = e.box(0.10)
    except Exception:
        return e.kind, e.confidence

    from shapely.geometry import LineString
    parallel = 0
    swing = False
    for p in drawing.prims:
        # Glazing is drawn on an opening or window layer. Counting wall and
        # header line work here made every header read as glazed, because a
        # header rectangle's own two long edges are, unavoidably, a pair of
        # lines running the length of the opening.
        if p.role not in (C.OPENING, C.WINDOW, C.DOOR):
            continue
        for (x0, y0), (x1, y1) in p.segments:
            seg_len = math.dist((x0, y0), (x1, y1))
            if seg_len < e.width * 0.55:
                continue
            ang = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
            if abs(((ang - e.angle + 90) % 180) - 90) > 10.0:
                continue
            try:
                if area.intersects(LineString([(x0, y0), (x1, y1)])):
                    parallel += 1
            except Exception:
                continue
    for a in drawing.arcs:
        if a.role not in (C.OPENING, C.DOOR):
            continue
        if abs(a.radius - e.width) <= max(0.12, e.width * 0.25) and \
                area.buffer(e.width * 0.6).contains(
                    __import__("shapely.geometry", fromlist=["Point"]).Point(*a.centre)):
            swing = True
            break

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


def assign(evidence: Sequence[Evidence], walls: Sequence[Wall],
           drawing: Drawing) -> List[Opening]:
    """Turn evidence into openings hosted by actual walls."""
    out: List[Opening] = []
    n = 0
    claimed: Dict[str, List[Tuple[float, float]]] = {w.id: [] for w in walls}

    # Resolve each evidence group down to its single best-hosted member first,
    # so a door swing contributes one doorway and not two at right angles.
    hosted: List[Tuple[Evidence, Wall, float]] = []
    best_of_group: Dict[str, Tuple[float, int]] = {}
    for e in evidence:
        host = _host_for(e, walls)
        if host is None:
            continue
        wall, offset, score = host
        if e.group:
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
        kind, conf = _refine_kind(e, wall, drawing)
        # Clamp inside the wall: an opening cannot be longer than its wall.
        lo = max(0.0, lo)
        hi = min(wall.length, hi)
        width = hi - lo
        if width < WIDTH_BAND[0]:
            continue
        claimed[wall.id].append((lo, hi))
        centre_t = (lo + hi) / 2.0
        height, sill = _heights(kind)
        n += 1
        op = Opening(
            id="o%d" % n, kind=kind, wall_id=wall.id,
            position=wall.point_at(centre_t), offset=round(centre_t, 4),
            width=round(width, 4), height=height, sill_height=sill,
            thickness=wall.thickness, swing=e.swing, source_ids=e.source_ids,
            evidence=e.source, confidence=round(min(0.99, conf), 3),
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
        )
        out.append(op)
        w.opening_ids.append(op.id)
    return out


def from_gaps(walls: Sequence[Wall], faces, existing: Sequence[Opening],
              min_width: float = 0.6, max_width: float = 3.2) -> List[Opening]:
    """Openings inferred from unexplained gaps in a wall's own line work.

    The fallback for drawings with no header, opening or door layer at all.
    Deliberately narrow: the gap must lie inside a wall that the pairing
    already built, so its two sides are known to be one wall rather than two.
    """
    from collections import defaultdict
    out: List[Opening] = []
    taken: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for o in existing:
        taken[o.wall_id].append((o.offset - o.width / 2, o.offset + o.width / 2))

    n = len(existing)
    for w in walls:
        d = w.direction
        gaps: List[Tuple[float, float]] = []
        for f in faces:
            if abs(((f.angle - w.angle_deg + 90) % 180) - 90) > 2.0:
                continue
            perp = abs((w.start[0] - f.point(0)[0]) * -d[1] +
                       (w.start[1] - f.point(0)[1]) * d[0])
            if perp > w.thickness * 0.85:
                continue
            runs = sorted(f.runs)
            t0 = w.start[0] * d[0] + w.start[1] * d[1]
            for (a0, a1), (b0, _b1) in zip(runs, runs[1:]):
                gap = b0 - a1
                if min_width <= gap <= max_width:
                    gaps.append((a1 - t0, b0 - t0))
        for lo, hi in _dedupe_spans(gaps):
            if lo < 0 or hi > w.length:
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
            )
            out.append(op)
            w.opening_ids.append(op.id)
    return out


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
