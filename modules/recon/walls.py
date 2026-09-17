"""
ArchX3D — Wall reconstruction from drawn line work
==================================================
Turns the lines a drafter drew into walls that have a thickness, a centreline
and ends that meet.

The mistake this replaces
------------------------
The old engine extruded every line segment on a wall layer into its own box.
That is not a wall system, it is a picture of one. A 150 mm wall is drawn as
*two* lines 150 mm apart, so extruding both gave two paper-thin slabs with a
void between them; a wall drawn as a closed rectangle gave four. Nothing
touched anything, so no room ever closed, and the "building" was a heap of
disconnected sheets — precisely the reported symptom.

The model here
--------------
A wall is a solid with two faces. So the reconstruction runs backwards from
the faces:

1. **Support lines.** Every segment is assigned to a maximal collinear run —
   its *face*. Eleven separate LINE entities along one side of a corridor are
   one face, which is what makes the rest of this tractable.
2. **Pairing.** Two parallel faces, a plausible wall thickness apart, that
   overlap along their shared direction, bound a wall. The overlap span is the
   wall; the midline is its centreline; the separation is its thickness.
   Greedy, best-scoring-first, with each span of each face consumable once —
   which is what lets one long exterior face pair with a 100 mm partition over
   part of its length and a 150 mm wall over the rest.
3. **Single-line fallback.** Some drawings — and most test fixtures — draw
   walls as single centrelines. If pairing explains too little of the wall
   line work, the unpaired runs are taken as centrelines directly, at the
   drawing's modal thickness. This decision is made *globally* and recorded,
   never per-segment, because mixing the two interpretations produces walls
   that are half real and half doubled.
4. **Cleanup.** Centrelines are extended to their true intersections and their
   ends snapped together. This is the step that closes rooms: two walls that
   meet at a corner produce overlap spans that stop half a thickness short of
   each other, and without extension every corner of the building has a gap in
   it and no room polygon can ever be found.

Thickness is measured, never assumed. ``CONVENTIONS`` only breaks ties.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import classify as C
from .ir import Wall
from .read import CadDrawing, Prim

XY = Tuple[float, float]
Segment = Tuple[XY, XY]

#: Two faces are "parallel" within this angle, in degrees. Drafters snap to
#: orthogonal, but xref rotation and float error leave small residuals.
ANGLE_TOL_DEG = 1.5

#: Two segments lie on the same support line if their perpendicular offsets
#: differ by less than this, in metres. Must stay well under the thinnest wall
#: (60 mm) or the two faces of a thin partition collapse into one line.
OFFSET_TOL = 0.018

#: Gap along a support line that still counts as the same face, in metres. A
#: wall face is interrupted by every door and window in it; joining across
#: those gaps is what makes one face out of a wall with three windows.
FACE_JOIN_GAP = 1.25

#: A wall is at least this thick and at most this thick, in metres.
THICKNESS_BAND = (0.055, 0.62)

#: Shorter than this and it is a jamb, a wall end cap or a hatch tick, in
#: metres — not a wall.
MIN_WALL_LENGTH = 0.24

#: How far a centreline may be extended to reach an intersection, in metres.
#: Large enough to close a corner of the thickest wall, small enough that two
#: walls a doorway apart are not welded together.
EXTEND_TOL = 0.75

#: Endpoints closer than this become one node, in metres.
SNAP_TOL = 0.12

#: Below this share of wall line work explained by pairing, the drawing is
#: taken to be drawn in single-line centrelines.
PAIRED_SHARE_FOR_DOUBLE_LINE = 0.30

#: Thickness used when a single-line drawing offers no evidence at all.
DEFAULT_THICKNESS = 0.15

#: Exterior walls are the thick ones. A wall at or above this fraction of the
#: drawing's thickest common wall is treated as exterior when it also lies on
#: the envelope.
EXTERIOR_THICKNESS_RATIO = 0.85


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------

@dataclass
class Face:
    """A maximal collinear run of wall boundary line work.

    ``angle`` is in [0, 180) degrees; ``offset`` is the signed perpendicular
    distance from the origin to the support line. ``runs`` are disjoint
    ``(t0, t1)`` intervals along the line direction, sorted.
    """

    id: str
    angle: float
    offset: float
    runs: List[Tuple[float, float]] = field(default_factory=list)
    source_ids: List[str] = field(default_factory=list)
    layers: List[str] = field(default_factory=list)
    #: ``(t0, t1, prim_id, layer)`` for every segment on the line, so a wall
    #: built from part of the line can cite the entities of *that part*. Two
    #: plans drawn on one sheet share gridlines, and a wall that cited the
    #: whole line claimed the other plan's layer and entities as its own.
    rows: List[Tuple[float, float, str, str]] = field(default_factory=list)

    def provenance(self, a: float, b: float, pad: float = 0.02
                   ) -> Tuple[List[str], List[Tuple[str, float]]]:
        """Entity ids and per-layer overlap length for the span ``[a, b]``."""
        lo, hi = min(a, b), max(a, b)
        ids = set()
        by_layer: Dict[str, float] = {}
        for t0, t1, pid, layer in self.rows:
            overlap = min(t1, hi + pad) - max(t0, lo - pad)
            if overlap <= 0:
                continue
            ids.add(pid)
            by_layer[layer] = by_layer.get(layer, 0.0) + overlap
        if not ids:
            return list(self.source_ids), [(l, 0.0) for l in self.layers]
        return sorted(ids), sorted(by_layer.items(), key=lambda kv: (-kv[1], kv[0]))

    @property
    def direction(self) -> XY:
        r = math.radians(self.angle)
        return (math.cos(r), math.sin(r))

    @property
    def normal(self) -> XY:
        r = math.radians(self.angle)
        return (-math.sin(r), math.cos(r))

    def point(self, t: float) -> XY:
        d, n = self.direction, self.normal
        return (d[0] * t + n[0] * self.offset, d[1] * t + n[1] * self.offset)

    @property
    def length(self) -> float:
        return sum(b - a for a, b in self.runs)

    def covers(self, a: float, b: float) -> bool:
        return any(r0 <= a + 1e-6 and b <= r1 + 1e-6 for r0, r1 in self.runs)


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


def _bridge_cover(bridges: Optional[Sequence]):
    """One unioned cover for every bridge, built once per drawing.

    Unioning inside the per-face loop instead meant 494 unions of 101
    polygons on a large sheet, which was most of the wall stage's runtime.
    """
    if not bridges:
        return None
    try:
        from shapely.ops import unary_union
        return unary_union(list(bridges))
    except Exception:
        return None


def _bridge_runs(runs: Sequence[Tuple[float, float]], cover,
                 angle: float, offset: float) -> List[Tuple[float, float]]:
    """Join runs across a gap that a known opening spans.

    The test is on the gap itself rather than on the runs: the segment from
    one run's end to the next run's start must lie inside an opening
    rectangle. A door in the middle of a wall satisfies it; two walls either
    side of a corridor do not, because nothing was drawn across the corridor.
    """
    if len(runs) < 2 or cover is None:
        return list(runs)
    from shapely.geometry import LineString
    r = math.radians(angle)
    d = (math.cos(r), math.sin(r))
    n = (-math.sin(r), math.cos(r))

    def pt(t: float) -> XY:
        return (d[0] * t + n[0] * offset, d[1] * t + n[1] * offset)

    out = [list(runs[0])]
    for a, b in runs[1:]:
        gap = a - out[-1][1]
        joined = False
        if gap > 0:
            span = LineString([pt(out[-1][1] + 1e-4), pt(a - 1e-4)])
            if cover.covers(span):
                joined = True
            else:
                # An opening symbol is often drawn a little inside the
                # structural gap — a frame set in from the jambs — so its
                # rectangle stops a few centimetres short of the wall line at
                # each end. Most of the gap covered, with only a jamb's worth
                # left over at the ends, is still one wall through one hole.
                covered = span.intersection(cover).length
                if covered >= 0.8 * span.length and \
                        span.length - covered <= BRIDGE_SLACK:
                    joined = True
        if joined:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(x, y) for x, y in out]


#: How much of a gap an opening's evidence may leave uncovered and still join
#: the wall across it, in metres — two frame insets.
BRIDGE_SLACK = 0.3

#: A face continues through an opening that starts within this distance of
#: where the face stops, in metres — a frame set in from its jamb.
THROUGH_START = 0.1

#: The farthest a face is continued through openings, in metres: the widest
#: opening the openings stage accepts.
THROUGH_REACH = 7.0

#: A face continues only through an opening running its way, within this
#: angle, in degrees.
THROUGH_ANGLE_TOL = 5.0


def _extend_through(runs: Sequence[Tuple[float, float]], cover,
                    angle: float, offset: float) -> List[Tuple[float, float]]:
    """Continue each run through an opening that begins where the run ends.

    Unlike :func:`_bridge_runs` this needs no line work on the far side: the
    opening itself is the evidence that the wall goes on. It stops where the
    opening stops, and never past the next run.
    """
    if not runs or cover is None:
        return list(runs)
    from shapely.geometry import LineString, Point
    r = math.radians(angle)
    d = (math.cos(r), math.sin(r))
    n = (-math.sin(r), math.cos(r))

    def pt(t: float) -> XY:
        return (d[0] * t + n[0] * offset, d[1] * t + n[1] * offset)

    def reach(t_from: float, t_to: float) -> float:
        span = abs(t_to - t_from)
        if span <= 0.05:
            return 0.0
        probe = LineString([pt(t_from), pt(t_to)])
        hit = probe.intersection(cover)
        best = 0.0
        for g in getattr(hit, "geoms", [hit]):
            if g.is_empty or g.geom_type != "LineString":
                continue
            ends = sorted(probe.project(Point(c)) for c in (g.coords[0], g.coords[-1]))
            if ends[0] <= THROUGH_START:
                best = max(best, ends[1])
        return min(best, span)

    out = [list(x) for x in sorted(runs)]
    for i, run in enumerate(out):
        ahead = out[i + 1][0] if i + 1 < len(out) else run[1] + THROUGH_REACH
        run[1] += reach(run[1], ahead)
        behind = out[i - 1][1] if i > 0 else run[0] - THROUGH_REACH
        run[0] -= reach(run[0], behind)
    return _merge_intervals([(a, b) for a, b in out], 1e-6)


def _subtract(runs: Sequence[Tuple[float, float]], a: float, b: float
              ) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for r0, r1 in runs:
        if b <= r0 or a >= r1:
            out.append((r0, r1))
            continue
        if r0 < a:
            out.append((r0, a))
        if b < r1:
            out.append((b, r1))
    return [(x, y) for x, y in out if y - x > 1e-9]


def build_faces(prims: Sequence[Prim], *, join_gap: float = FACE_JOIN_GAP,
                bridges: Optional[Sequence] = None,
                through: Optional[Sequence] = None) -> List[Face]:
    """Group segments into maximal collinear runs.

    Clustering is done on angle first and offset second, both greedily over
    sorted values, so a face is never split by an arbitrary bucket boundary
    the way fixed-width binning would split one at 44.99 and 45.01 degrees.

    ``bridges`` are opening rectangles from :mod:`modules.recon.openings`. A
    gap wider than ``join_gap`` is joined anyway when a bridge covers it,
    which is how a wall survives a 4.9 m garage door. Without them the line
    work's gaps and the wall's ends are indistinguishable.

    ``through`` are the openings known to lie along a wall; a face that stops
    at one continues through it (see :func:`_extend_through`).
    """
    cover = _bridge_cover(bridges)
    through_covers: Dict[int, object] = {}

    def through_cover_for(angle: float):
        """The union of the openings that run along ``angle``."""
        key = int(round(angle / ANGLE_TOL_DEG))
        if key not in through_covers:
            through_covers[key] = _bridge_cover([
                poly for poly, a in through or ()
                if abs(((a - angle + 90.0) % 180.0) - 90.0) <= THROUGH_ANGLE_TOL])
        return through_covers[key]
    items: List[Tuple[float, float, float, float, str, str]] = []
    for p in prims:
        for (x0, y0), (x1, y1) in p.segments:
            dx, dy = x1 - x0, y1 - y0
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                continue
            ang = math.degrees(math.atan2(dy, dx)) % 180.0
            items.append((ang, x0, y0, x1, y1, p.id, p.layer))  # type: ignore[arg-type]

    if not items:
        return []

    items.sort(key=lambda it: it[0])
    # Wrap-around: 179.5 and 0.2 degrees are 0.7 degrees apart, so the last
    # cluster may belong with the first. Handled after clustering.
    clusters: List[List[tuple]] = [[items[0]]]
    for it in items[1:]:
        if it[0] - clusters[-1][-1][0] <= ANGLE_TOL_DEG:
            clusters[-1].append(it)
        else:
            clusters.append([it])
    if len(clusters) > 1 and (items[0][0] + 180.0 - items[-1][0]) <= ANGLE_TOL_DEG:
        clusters[0].extend(clusters.pop())

    faces: List[Face] = []
    fid = 0
    for cluster in clusters:
        # A representative angle for the whole cluster, length-weighted so a
        # handful of stray short segments cannot tilt a wall run.
        sx = sy = 0.0
        for ang, x0, y0, x1, y1, _pid, _lay in cluster:
            w = math.dist((x0, y0), (x1, y1))
            sx += w * math.cos(2 * math.radians(ang))
            sy += w * math.sin(2 * math.radians(ang))
        angle = (math.degrees(math.atan2(sy, sx)) / 2.0) % 180.0
        r = math.radians(angle)
        d = (math.cos(r), math.sin(r))
        n = (-math.sin(r), math.cos(r))

        rows: List[Tuple[float, float, float, str, str]] = []
        for _ang, x0, y0, x1, y1, pid, lay in cluster:
            o0 = x0 * n[0] + y0 * n[1]
            o1 = x1 * n[0] + y1 * n[1]
            t0 = x0 * d[0] + y0 * d[1]
            t1 = x1 * d[0] + y1 * d[1]
            rows.append(((o0 + o1) / 2.0, min(t0, t1), max(t0, t1), pid, lay))
        rows.sort()

        groups: List[List[tuple]] = [[rows[0]]]
        for row in rows[1:]:
            if row[0] - groups[-1][-1][0] <= OFFSET_TOL:
                groups[-1].append(row)
            else:
                groups.append([row])

        for group in groups:
            offset = sum(g[0] for g in group) / len(group)
            runs = _merge_intervals([(g[1], g[2]) for g in group], join_gap)
            if cover is not None:
                runs = _bridge_runs(runs, cover, angle, offset)
            if through:
                runs = _extend_through(runs, through_cover_for(angle), angle, offset)
            fid += 1
            faces.append(Face(
                id="f%d" % fid, angle=angle, offset=offset, runs=runs,
                source_ids=sorted({g[3] for g in group}),
                layers=sorted({g[4] for g in group}),
                rows=[(g[1], g[2], g[3], g[4]) for g in group],
            ))
    return faces


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

@dataclass
class _Candidate:
    score: float
    thickness: float
    a: Face
    b: Face
    t0: float
    t1: float


def _thickness_plausibility(t: float, modal: Sequence[float]) -> float:
    """How wall-like a measured separation is, in [0, 1].

    Being near a thickness the *drawing itself* uses repeatedly is the strong
    signal; being near a building convention is a weak tiebreak. Neither is
    allowed to reject a measurement inside the band, because unusual walls
    exist and the drawing is the authority on its own construction.
    """
    lo, hi = THICKNESS_BAND
    if not (lo <= t <= hi):
        return 0.0
    best = 0.55
    for m in modal:
        if m > 0 and abs(t - m) <= max(0.012, 0.06 * m):
            best = max(best, 1.0)
    for c in (0.075, 0.089, 0.100, 0.102, 0.115, 0.125, 0.140, 0.150, 0.152,
              0.178, 0.200, 0.203, 0.229, 0.230, 0.250, 0.254, 0.300, 0.305):
        if abs(t - c) <= 0.012:
            best = max(best, 0.85)
    return best


def pair_faces(faces: Sequence[Face], *, modal: Sequence[float] = (),
               min_length: float = MIN_WALL_LENGTH,
               ) -> Tuple[List[_Candidate], Dict[str, List[Tuple[float, float]]]]:
    """Match opposing faces into wall spans, best first.

    Returns the accepted pairings and the line work each face has left over.
    The leftovers matter as much as the matches: they are what the single-line
    fallback and the diagnostics work from.
    """
    by_angle: Dict[int, List[Face]] = {}
    for f in faces:
        by_angle.setdefault(int(round(f.angle / ANGLE_TOL_DEG)), []).append(f)

    lo, hi = THICKNESS_BAND
    cands: List[_Candidate] = []
    keys = sorted(by_angle)
    for k in keys:
        # Neighbouring buckets too: a pair may straddle a bucket boundary.
        group: List[Face] = []
        for kk in (k - 1, k, k + 1):
            group.extend(by_angle.get(kk, []))
        group = [f for f in group if abs(((f.angle - by_angle[k][0].angle + 90) % 180) - 90)
                 <= ANGLE_TOL_DEG]
        group.sort(key=lambda f: f.offset)
        seen = set()
        for i, fa in enumerate(group):
            for fb in group[i + 1:]:
                t = fb.offset - fa.offset
                if t > hi:
                    break
                if t < lo:
                    continue
                key = (fa.id, fb.id)
                if key in seen:
                    continue
                seen.add(key)
                plaus = _thickness_plausibility(t, modal)
                if plaus <= 0.0:
                    continue
                for a0, a1 in fa.runs:
                    for b0, b1 in fb.runs:
                        s, e = max(a0, b0), min(a1, b1)
                        if e - s < min_length:
                            continue
                        cands.append(_Candidate(
                            score=(e - s) * plaus, thickness=t,
                            a=fa, b=fb, t0=s, t1=e))

    cands.sort(key=lambda c: -c.score)
    remaining: Dict[str, List[Tuple[float, float]]] = {
        f.id: list(f.runs) for f in faces}
    accepted: List[_Candidate] = []
    for c in cands:
        # The span must still be available on *both* faces. Taking the largest
        # still-free sub-span rather than rejecting outright keeps a long wall
        # that a short partition already claimed a metre of.
        free_a = [iv for iv in remaining[c.a.id]
                  if min(iv[1], c.t1) - max(iv[0], c.t0) >= min_length]
        free_b = [iv for iv in remaining[c.b.id]
                  if min(iv[1], c.t1) - max(iv[0], c.t0) >= min_length]
        if not free_a or not free_b:
            continue
        for ia in free_a:
            for ib in free_b:
                s = max(c.t0, ia[0], ib[0])
                e = min(c.t1, ia[1], ib[1])
                if e - s < min_length:
                    continue
                accepted.append(_Candidate(score=(e - s) * (c.score / max(c.t1 - c.t0, 1e-9)),
                                           thickness=c.thickness, a=c.a, b=c.b,
                                           t0=s, t1=e))
                remaining[c.a.id] = _subtract(remaining[c.a.id], s, e)
                remaining[c.b.id] = _subtract(remaining[c.b.id], s, e)
    return accepted, remaining


# ---------------------------------------------------------------------------
# Centreline cleanup
# ---------------------------------------------------------------------------

def _as_line(w: Wall) -> Tuple[XY, XY, float]:
    return w.start, w.end, w.length


def _intersect(p1: XY, p2: XY, p3: XY, p4: XY) -> Optional[Tuple[float, float, XY]]:
    """Parametric intersection of two infinite lines, or None if parallel."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(d) < 1e-12:
        return None
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d
    return t, u, (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def drop_embedded(walls: List[Wall], tol: float = 0.02) -> List[Wall]:
    """Remove "walls" that lie wholly inside a thicker wall.

    The two jamb lines of a narrow pier between a door and a window face each
    other a pier's width apart, so they pair like a thin wall — one that runs
    straight across the thick wall it is part of. No real wall is drawn
    inside another; once extended to its neighbours it would cut the thick
    wall in two and seal a sliver room.
    """
    out = []
    for w in walls:
        inside = False
        for v in walls:
            if v is w or v.thickness <= w.thickness + 1e-6 or v.length < 1e-9:
                continue
            d = v.direction
            ok = True
            for p in (w.start, w.end):
                vx, vy = p[0] - v.start[0], p[1] - v.start[1]
                t = vx * d[0] + vy * d[1]
                if not (-tol <= t <= v.length + tol) or \
                        abs(-vx * d[1] + vy * d[0]) > v.thickness / 2.0 + tol:
                    ok = False
                    break
            if ok:
                inside = True
                break
        if not inside:
            out.append(w)
    return out


def merge_collinear(walls: List[Wall]) -> List[Wall]:
    """Join walls that lie on one line and touch, so a run is one wall.

    Pairing produces a separate span wherever the opposing face changed, which
    splits one continuous wall into three whenever a partition tees into it.
    Those pieces have the same centreline and thickness and should be one wall
    with one set of openings.
    """
    if not walls:
        return walls
    buckets: Dict[Tuple[int, int, int], List[Wall]] = {}
    for w in walls:
        ang = w.angle_deg % 180.0
        r = math.radians(ang)
        n = (-math.sin(r), math.cos(r))
        off = w.start[0] * n[0] + w.start[1] * n[1]
        key = (int(round(ang / ANGLE_TOL_DEG)),
               int(round(off / (OFFSET_TOL * 2))),
               int(round(w.thickness / 0.02)))
        buckets.setdefault(key, []).append(w)

    out: List[Wall] = []
    for group in buckets.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        ref = group[0]
        r = math.radians(ref.angle_deg % 180.0)
        d = (math.cos(r), math.sin(r))
        spans: List[Tuple[float, float, Wall]] = []
        for w in group:
            t0 = w.start[0] * d[0] + w.start[1] * d[1]
            t1 = w.end[0] * d[0] + w.end[1] * d[1]
            spans.append((min(t0, t1), max(t0, t1), w))
        spans.sort()
        cur = [spans[0][0], spans[0][1], [spans[0][2]]]
        merged: List[list] = [cur]
        for s, e, w in spans[1:]:
            if s <= cur[1] + SNAP_TOL:
                cur[1] = max(cur[1], e)
                cur[2].append(w)
            else:
                cur = [s, e, [w]]
                merged.append(cur)
        for s, e, members in merged:
            base = max(members, key=lambda w: w.length)
            n = (-math.sin(r), math.cos(r))
            off = sum(m.start[0] * n[0] + m.start[1] * n[1] for m in members) / len(members)
            base.start = (d[0] * s + n[0] * off, d[1] * s + n[1] * off)
            base.end = (d[0] * e + n[0] * off, d[1] * e + n[1] * off)
            base.thickness = sum(m.thickness * m.length for m in members) / max(
                sum(m.length for m in members), 1e-9)
            for m in members:
                if m is not base:
                    base.source_ids.extend(m.source_ids)
            base.source_ids = sorted(set(base.source_ids))
            out.append(base)
    return out


#: A gap between two collinear wall centrelines up to this wide is a doorway,
#: in metres. Beyond it the two are separate walls with a space between them.
MAX_DOORWAY_GAP = 1.7


def close_collinear_gaps(walls: List[Wall], max_gap: float = MAX_DOORWAY_GAP
                         ) -> List[Tuple[str, float, float]]:
    """Join collinear walls across a doorway-sized gap, and report the gap.

    This runs on centrelines, not on line work, and that is what makes it
    safe. By this point the two pieces are known to be walls — each was built
    from its own matched pair of faces — and they are known to be collinear
    and the same thickness. Two such pieces with a metre between them are one
    wall with a door in it; there is no other construction they could be.

    The gaps are returned rather than swallowed, so each becomes an opening
    and the wall ends up with a hole in it instead of being quietly made
    solid. A drawing whose door layer says nothing still gets its doorways.
    """
    inferred: List[Tuple[str, float, float]] = []
    if len(walls) < 2:
        return inferred

    ends = _free_ends(walls)
    by_angle: Dict[int, List[Wall]] = {}
    for w in walls:
        by_angle.setdefault(int(round((w.angle_deg % 180.0) / ANGLE_TOL_DEG)), []).append(w)

    absorbed: set = set()
    for key in sorted(by_angle):
        group = [w for k in (key - 1, key, key + 1) for w in by_angle.get(k, [])]
        for i, a in enumerate(group):
            if a.id in absorbed:
                continue
            for b in group[i + 1:]:
                if b.id in absorbed or a.id in absorbed:
                    continue
                if abs(((a.angle_deg - b.angle_deg + 90) % 180) - 90) > ANGLE_TOL_DEG:
                    continue
                if abs(a.thickness - b.thickness) > 0.35 * max(a.thickness, b.thickness):
                    continue
                r = math.radians(a.angle_deg % 180.0)
                d = (math.cos(r), math.sin(r))
                n = (-math.sin(r), math.cos(r))
                oa = a.start[0] * n[0] + a.start[1] * n[1]
                ob = b.start[0] * n[0] + b.start[1] * n[1]
                # Same wall line if the two solid bands still overlap across
                # the wall. That is the geometric statement of "one wall",
                # and it holds where a 230 mm wall continues as a 115 mm one
                # flush on a face, which a fraction-of-thickness test does not.
                if abs(oa - ob) > (a.thickness + b.thickness) / 2.0:
                    continue
                ta = sorted((a.start[0] * d[0] + a.start[1] * d[1],
                             a.end[0] * d[0] + a.end[1] * d[1]))
                tb = sorted((b.start[0] * d[0] + b.start[1] * d[1],
                             b.end[0] * d[0] + b.end[1] * d[1]))
                if tb[0] < ta[0]:
                    ta, tb, a_first = tb, ta, False
                else:
                    a_first = True
                gap = tb[0] - ta[1]
                if not (0.0 < gap <= max_gap):
                    continue
                # Both ends facing the gap must be free. A tee joining here
                # means the wall genuinely stops and another begins.
                pa = (d[0] * ta[1] + n[0] * oa, d[1] * ta[1] + n[1] * oa)
                pb = (d[0] * tb[0] + n[0] * ob, d[1] * tb[0] + n[1] * ob)
                if not (_is_free(ends, pa) and _is_free(ends, pb)):
                    continue

                keep, drop = (a, b) if a.length >= b.length else (b, a)
                off = (oa + ob) / 2.0
                lo, hi = ta[0], tb[1]
                keep.start = (d[0] * lo + n[0] * off, d[1] * lo + n[1] * off)
                keep.end = (d[0] * hi + n[0] * off, d[1] * hi + n[1] * off)
                keep.thickness = (a.thickness * (ta[1] - ta[0]) +
                                  b.thickness * (tb[1] - tb[0])) / \
                                 max((ta[1] - ta[0]) + (tb[1] - tb[0]), 1e-9)
                keep.source_ids = sorted(set(keep.source_ids) | set(drop.source_ids))
                absorbed.add(drop.id)
                inferred.append((keep.id, ta[1] - lo, tb[0] - lo))
                del a_first

    if absorbed:
        walls[:] = [w for w in walls if w.id not in absorbed]
    inferred.extend(_close_end_doorways(walls, max_gap))
    return inferred


def _close_end_doorways(walls: List[Wall], max_gap: float
                        ) -> List[Tuple[str, float, float]]:
    """Extend a wall that stops a doorway short of the wall it should meet.

    The commonest way to draw a doorway is not a gap in the middle of a wall
    but a wall that simply stops short of the one it runs into — the partition
    between two bedrooms ends a metre before the corridor wall, and the metre
    is the door. Geometrically the room is then not enclosed, and no amount of
    room-finding can recover it, because the boundary genuinely has a hole.

    The repair is to complete the wall and cut the doorway back out of it, so
    the room closes and the opening is still there. Only free ends qualify,
    and only when the extension actually lands on another wall's body rather
    than on the infinite line through it, which is what stops a wall from
    growing out into open space to meet something it never touched.
    """
    if len(walls) < 2:
        return []
    ends = _free_ends(walls)
    inferred: List[Tuple[str, float, float]] = []
    thickest = max(w.thickness for w in walls)
    # A hit lies on the other wall's body to within 2% of its length, and
    # walls repaired earlier in this pass have grown by up to ``max_gap``.
    grid = _WallGrid(walls, lambda o: 0.02 * o.length + max_gap + 0.1)
    for w in walls:
        if w.length < 1e-9:
            continue
        d = w.direction
        for which in (0, 1):
            p = w.start if which == 0 else w.end
            if not _is_free(ends, p):
                continue
            best: Optional[Tuple[float, XY]] = None
            sign = -1.0 if which == 0 else 1.0
            back = thickest + 0.05
            qa = (p[0] - sign * d[0] * back, p[1] - sign * d[1] * back)
            qb = (p[0] + sign * d[0] * max_gap, p[1] + sign * d[1] * max_gap)
            for other in grid.near(min(qa[0], qb[0]), min(qa[1], qb[1]),
                                   max(qa[0], qb[0]), max(qa[1], qb[1])):
                if other is w or other.length < 1e-9:
                    continue
                hit = _intersect(w.start, w.end, other.start, other.end)
                if hit is None:
                    continue
                t, u, pt = hit
                if not (-0.02 <= u <= 1.02):
                    continue        # misses the other wall's body
                reach = (-t * w.length) if which == 0 else ((t - 1.0) * w.length)
                # Any wall at or just behind this end counts: an end that
                # already tees into a wall is not free, however far the next
                # wall beyond it lies.
                if reach < -max(w.thickness, other.thickness):
                    continue
                if best is None or reach < best[0]:
                    best = (reach, pt)
            # The nearest wall ahead is the only candidate. If it is already
            # within corner-closing reach, this end meets a wall and is no
            # doorway; looking past it would extend the wall *through* that
            # wall into whatever lies beyond — a courtyard, or the next
            # building on the sheet.
            if best is None or best[0] <= EXTEND_TOL or best[0] > max_gap:
                continue
            reach, pt = best
            old_len = w.length
            if which == 0:
                moved = (w.start[0] - d[0] * reach, w.start[1] - d[1] * reach)
                _move_end(ends, w.start, moved)
                w.start = moved
                inferred.append((w.id, 0.0, reach))
            else:
                moved = (w.end[0] + d[0] * reach, w.end[1] + d[1] * reach)
                _move_end(ends, w.end, moved)
                w.end = moved
                inferred.append((w.id, old_len, old_len + reach))
    return inferred


class _WallGrid:
    """Walls by grid cell, for "which walls could be near here" queries.

    Each wall is entered under its box grown by ``pad(wall)`` — as far from
    the wall as any test using the grid can accept a point — so a query
    returns every wall the exhaustive loop would have accepted, plus some it
    rejects. Candidates come back in list order, so ties still resolve as
    they did when every wall was visited.
    """

    CELL = 2.0

    def __init__(self, walls: Sequence[Wall], pad):
        self.walls = list(walls)
        self.cells: Dict[Tuple[int, int], List[int]] = {}
        for i, w in enumerate(self.walls):
            p = pad(w)
            x0 = min(w.start[0], w.end[0]) - p
            x1 = max(w.start[0], w.end[0]) + p
            y0 = min(w.start[1], w.end[1]) - p
            y1 = max(w.start[1], w.end[1]) + p
            for cx in range(int(math.floor(x0 / self.CELL)), int(math.floor(x1 / self.CELL)) + 1):
                for cy in range(int(math.floor(y0 / self.CELL)), int(math.floor(y1 / self.CELL)) + 1):
                    self.cells.setdefault((cx, cy), []).append(i)

    def near(self, x0: float, y0: float, x1: float, y1: float) -> List[Wall]:
        hits = set()
        for cx in range(int(math.floor(x0 / self.CELL)), int(math.floor(x1 / self.CELL)) + 1):
            for cy in range(int(math.floor(y0 / self.CELL)), int(math.floor(y1 / self.CELL)) + 1):
                hits.update(self.cells.get((cx, cy), ()))
        return [self.walls[i] for i in sorted(hits)]


def _move_end(cells: Dict[Tuple[int, int], List[XY]], old: XY, new: XY,
              tol: float = SNAP_TOL) -> None:
    """Keep the wall-end index current after one end moves.

    Rebuilding the whole index after every extension made this quadratic in
    the number of repairs, which on a 442-wall site plan was most of the wall
    stage's runtime.
    """
    key = (int(old[0] // tol), int(old[1] // tol))
    bucket = cells.get(key)
    if bucket:
        for i, p in enumerate(bucket):
            if p == old:
                bucket.pop(i)
                break
    cells.setdefault((int(new[0] // tol), int(new[1] // tol)), []).append(new)


def _free_ends(walls: Sequence[Wall], tol: float = SNAP_TOL
               ) -> Dict[Tuple[int, int], List[XY]]:
    """Index every wall end by grid cell, for a cheap "is anything here" test."""
    cells: Dict[Tuple[int, int], List[XY]] = {}
    for w in walls:
        for p in (w.start, w.end):
            key = (int(p[0] // tol), int(p[1] // tol))
            cells.setdefault(key, []).append(p)
    return cells


def _is_free(cells: Dict[Tuple[int, int], List[XY]], p: XY,
             tol: float = SNAP_TOL) -> bool:
    """Whether this wall end meets no other wall end.

    Tested by real distance rather than by cell occupancy: a tee junction two
    cells away is not this end's business, and counting it made every doorway
    beside a tee look like a deliberate wall end.
    """
    key = (int(p[0] // tol), int(p[1] // tol))
    n = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for q in cells.get((key[0] + dx, key[1] + dy), ()):  # type: ignore[arg-type]
                if math.dist(p, q) <= tol:
                    n += 1
                    if n > 1:
                        return False
    return True


def extend_to_intersections(walls: List[Wall], tol: float = EXTEND_TOL) -> None:
    """Stretch centrelines so walls that should meet actually do.

    Pairing stops each wall where its two faces stop overlapping, which at an
    L corner is half a wall thickness short of the corner point, and at a T
    junction is the full thickness short. Left alone, every room polygon has a
    gap at every corner and none of them close. Extension is capped so that
    two walls either side of a doorway are not joined across it.
    """
    lines = [(w, w.start, w.end, w.length) for w in walls]
    ext: Dict[str, Tuple[float, float]] = {w.id: (0.0, 0.0) for w in walls}
    for i, (wa, a0, a1, la) in enumerate(lines):
        if la < 1e-9:
            continue
        for wb, b0, b1, lb in lines[i + 1:]:
            if lb < 1e-9:
                continue
            hit = _intersect(a0, a1, b0, b1)
            if hit is None:
                continue
            t, u, pt = hit
            # Distance the intersection sits beyond each wall's own extent.
            over_a = (-t * la) if t < 0 else ((t - 1.0) * la if t > 1.0 else 0.0)
            over_b = (-u * lb) if u < 0 else ((u - 1.0) * lb if u > 1.0 else 0.0)
            if over_a > tol or over_b > tol:
                continue
            if over_a <= 0 and over_b <= 0:
                continue    # they already cross
            ea = ext[wa.id]
            eb = ext[wb.id]
            if t < 0:
                ext[wa.id] = (max(ea[0], over_a), ea[1])
            elif t > 1.0:
                ext[wa.id] = (ea[0], max(ea[1], over_a))
            if u < 0:
                ext[wb.id] = (max(eb[0], over_b), eb[1])
            elif u > 1.0:
                ext[wb.id] = (eb[0], max(eb[1], over_b))

    for w in walls:
        s, e = ext[w.id]
        if s <= 0 and e <= 0:
            continue
        d = w.direction
        w.start = (w.start[0] - d[0] * s, w.start[1] - d[1] * s)
        w.end = (w.end[0] + d[0] * e, w.end[1] + d[1] * e)


#: A wall end this close to another wall's body tees into it, in metres (or
#: that wall's thickness, if more).
TEE_TOL = 0.05

#: How far a welded end is carried past the body it meets, in metres, so the
#: two centrelines genuinely cross and the planar graph nodes them. The dangle
#: this leaves is a millimetre and polygonisation discards it.
TEE_OVERSHOOT = 0.002


def weld_tees(walls: List[Wall], tol: float = TEE_TOL) -> int:
    """Put every wall end that stops against another wall *onto* that wall.

    Snapping averages clustered ends, which is exact at an L corner and
    slightly wrong at a T: the end of the tee is pulled towards the ends
    nearby and off the line of the wall it meets. On an axis-aligned plan the
    error is along that wall and harmless. On a plan drawn at 30 degrees it is
    six millimetres *off* the wall, the centrelines no longer touch, and not a
    single room closes. So, last of all, each end near another wall's body is
    moved along its own direction to cross that body.
    """
    moved = 0
    if not walls:
        return 0
    thickest = max(w.thickness for w in walls)
    # An accepted end lies within ``reach`` of the other wall's body, which is
    # itself allowed 2% of its length past each end; ends welded earlier in
    # this pass have moved by up to 2.5 reaches.
    reach_max = max(tol, thickest * 0.6)
    grid = _WallGrid(walls, lambda o: 0.02 * o.length + reach_max * 3.5 + 0.01)
    for w in walls:
        if w.length < 1e-9:
            continue
        d = w.direction
        for which in (0, 1):
            p = w.start if which == 0 else w.end
            best = None
            for o in grid.near(p[0], p[1], p[0], p[1]):
                if o is w or o.length < 1e-9:
                    continue
                if abs(((w.angle_deg - o.angle_deg + 90) % 180) - 90) < 20.0:
                    continue
                reach = max(tol, o.thickness * 0.6)
                ox, oy = o.start
                ex, ey = o.end[0] - ox, o.end[1] - oy
                L2 = ex * ex + ey * ey
                t = ((p[0] - ox) * ex + (p[1] - oy) * ey) / L2
                if t < -0.02 or t > 1.02:
                    continue
                q = (ox + ex * t, oy + ey * t)
                dist = math.dist(p, q)
                # An end that already lies *on* the body is welded too: "on"
                # is only true to fifteen digits, and on a rotated plan the
                # planar graph does not node a point that far off the line.
                if dist > reach:
                    continue
                if t <= 1e-6 or t >= 1.0 - 1e-6:
                    continue        # an end meeting an end is a corner, snapped already
                hit = _intersect(w.start, w.end, o.start, o.end)
                if hit is None:
                    continue
                s, _u, pt = hit
                along = (-s * w.length) if which == 0 else ((s - 1.0) * w.length)
                if abs(along) > reach * 2.5:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, along)
            if best is None:
                continue
            _dist, along = best
            ext = along + TEE_OVERSHOOT
            if which == 0:
                w.start = (w.start[0] - d[0] * ext, w.start[1] - d[1] * ext)
            else:
                w.end = (w.end[0] + d[0] * ext, w.end[1] + d[1] * ext)
            moved += 1
    return moved


def snap_endpoints(walls: List[Wall], tol: float = SNAP_TOL) -> None:
    """Weld wall ends that are within ``tol`` into a single shared point."""
    pts: List[Tuple[Wall, str, XY]] = []
    for w in walls:
        pts.append((w, "start", w.start))
        pts.append((w, "end", w.end))
    clusters: List[List[Tuple[Wall, str, XY]]] = []
    # Grid hashing keeps this linear rather than quadratic on large plans.
    grid: Dict[Tuple[int, int], List[int]] = {}
    cell = max(tol, 1e-6)
    for item in pts:
        x, y = item[2]
        key = (int(x // cell), int(y // cell))
        found = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ci in grid.get((key[0] + dx, key[1] + dy), []):
                    if math.dist(clusters[ci][0][2], item[2]) <= tol:
                        found = ci
                        break
                if found is not None:
                    break
            if found is not None:
                break
        if found is None:
            clusters.append([item])
            grid.setdefault(key, []).append(len(clusters) - 1)
        else:
            clusters[found].append(item)

    for cluster in clusters:
        if len(cluster) < 2:
            continue
        cx = sum(p[2][0] for p in cluster) / len(cluster)
        cy = sum(p[2][1] for p in cluster) / len(cluster)
        for w, which, _ in cluster:
            if which == "start":
                w.start = (cx, cy)
            else:
                w.end = (cx, cy)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@dataclass
class WallResult:
    walls: List[Wall]
    faces: List[Face]
    method: str
    leftovers: Dict[str, List[Tuple[float, float]]]
    stats: Dict[str, object]
    #: ``(wall_id, offset_lo, offset_hi)`` for each doorway-sized gap that was
    #: closed to make a wall continuous. Each one is a hole that has to be cut
    #: back out, or the model gains a door-shaped piece of solid wall.
    inferred_openings: List[Tuple[str, float, float]] = field(default_factory=list)


def _dominant_layer(layers: Sequence[Tuple[str, float]]) -> str:
    """The layer carrying most of a wall's line work."""
    total: Dict[str, float] = {}
    for name, length in layers:
        total[name] = total.get(name, 0.0) + length
    if not total:
        return ""
    return max(sorted(total), key=lambda k: total[k])


def _wall_source_prims(drawing: CadDrawing) -> Tuple[List[Prim], str]:
    """The line work wall detection is allowed to look at.

    Layers first. Only when the drawing's layers cannot identify enough wall
    geometry does this fall back to "anything that is not documentation", and
    that fallback is reported, because it is the case where a cabinet run can
    become a partition.
    """
    walls = drawing.wall_prims()
    if C.has_usable_wall_layers(drawing.layer_survey, drawing.layer_counts):
        return walls, "layer"
    allowed = {C.WALL, C.UNKNOWN, C.FOOTPRINT, C.STRUCTURE_BELOW, C.HATCH}
    geo = [p for p in drawing.prims if p.role in allowed]
    if walls:
        # Unclassified geometry may *add* to walls we already know about; it
        # may not describe a building somewhere else on the sheet. Without
        # this, a title block — a labelled rectangle on layer 0, which is
        # exactly what unclassified geometry looks like — came back as a room
        # called "GROUND FLOOR PLAN" floating beside the flat.
        x0 = min(x for p in walls for x, _ in p.points)
        y0 = min(y for p in walls for _, y in p.points)
        x1 = max(x for p in walls for x, _ in p.points)
        y1 = max(y for p in walls for _, y in p.points)
        mx, my = (x1 - x0) * 0.2 + 0.5, (y1 - y0) * 0.2 + 0.5
        geo = [p for p in geo
               if any(x0 - mx <= x <= x1 + mx and y0 - my <= y <= y1 + my
                      for x, y in p.points)]
    return (geo, "geometry") if len(geo) > len(walls) else (walls, "layer")


def reconstruct(drawing: CadDrawing, *,
                default_thickness: float = DEFAULT_THICKNESS,
                bridges: Optional[Sequence] = None,
                through: Optional[Sequence] = None,
                ) -> WallResult:
    """Reconstruct wall systems from a read drawing."""
    t0 = time.perf_counter()
    prims, selection = _wall_source_prims(drawing)
    faces = build_faces(prims, bridges=bridges, through=through)
    total_face_length = sum(f.length for f in faces)

    from . import units as U
    segs: List[Segment] = []
    for p in prims:
        segs.extend(p.segments)
    modal = U.modal_thickness(U.parallel_pair_distances(segs), 1.0)

    accepted, leftovers = pair_faces(faces, modal=modal)
    paired_length = sum(c.t1 - c.t0 for c in accepted)
    share = paired_length / total_face_length if total_face_length else 0.0

    walls: List[Wall] = []
    n = 0
    for c in accepted:
        mid_off = (c.a.offset + c.b.offset) / 2.0
        d, nvec = c.a.direction, c.a.normal
        s = (d[0] * c.t0 + nvec[0] * mid_off, d[1] * c.t0 + nvec[1] * mid_off)
        e = (d[0] * c.t1 + nvec[0] * mid_off, d[1] * c.t1 + nvec[1] * mid_off)
        ids_a, layers_a = c.a.provenance(c.t0, c.t1)
        ids_b, layers_b = c.b.provenance(c.t0, c.t1)
        n += 1
        walls.append(Wall(
            id="w%d" % n, start=s, end=e, thickness=c.thickness,
            source_ids=sorted(set(ids_a) | set(ids_b)),
            layer=_dominant_layer(layers_a + layers_b), confidence=0.95,
        ))

    method = "paired"
    if share < PAIRED_SHARE_FOR_DOUBLE_LINE:
        # Single-line drawing: the line work *is* the centrelines.
        thickness = modal[0] if modal and THICKNESS_BAND[0] <= modal[0] <= THICKNESS_BAND[1] \
            else default_thickness
        walls = []
        n = 0
        for f in faces:
            for a, b in f.runs:
                if b - a < MIN_WALL_LENGTH:
                    continue
                ids, layers = f.provenance(a, b)
                n += 1
                walls.append(Wall(
                    id="w%d" % n, start=f.point(a), end=f.point(b),
                    thickness=thickness, source_ids=ids,
                    layer=_dominant_layer(layers), confidence=0.7,
                ))
        method = "single-line"

    walls = drop_embedded(merge_collinear(walls))
    for i, w in enumerate(walls, 1):
        w.id = "w%d" % i
    extend_to_intersections(walls)
    snap_endpoints(walls)
    walls = [w for w in walls if w.length >= MIN_WALL_LENGTH]
    for i, w in enumerate(walls, 1):
        w.id = "w%d" % i
    # Last: doorway-sized gaps between collinear centrelines. Deliberately
    # after extension and snapping, so that a gap which was really a corner
    # has already been closed as one and is not mistaken for a doorway.
    inferred = close_collinear_gaps(walls)
    extend_to_intersections(walls)
    snap_endpoints(walls)
    weld_tees(walls)
    id_map = {w.id: "w%d" % i for i, w in enumerate(walls, 1)}
    inferred = [(id_map.get(wid, wid), lo, hi) for wid, lo, hi in inferred]
    for w in walls:
        w.id = id_map[w.id]

    thicknesses = sorted({round(w.thickness, 3) for w in walls})
    stats = {
        "selection": selection,
        "method": method,
        "bridges": len(bridges) if bridges else 0,
        "faces": len(faces),
        "face_length_m": round(total_face_length, 2),
        "paired_length_m": round(paired_length, 2),
        "paired_share": round(share, 3),
        "modal_thickness_m": [round(m, 4) for m in modal[:5]],
        "distinct_thickness_m": thicknesses[:12],
        "walls": len(walls),
        "closed_doorway_gaps": len(inferred),
        "wall_length_m": round(sum(w.length for w in walls), 2),
        "seconds": round(time.perf_counter() - t0, 3),
    }
    return WallResult(walls=walls, faces=faces, method=method,
                      leftovers=leftovers, stats=stats,
                      inferred_openings=inferred)


def _wall_envelope_ring(walls: Sequence[Wall]):
    """Outer boundary of the union of every wall solid, or None."""
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception:
        return None
    polys = []
    for w in walls:
        if w.length < 1e-9:
            continue
        n = w.normal
        h = w.thickness / 2.0
        polys.append(Polygon([
            (w.start[0] + n[0] * h, w.start[1] + n[1] * h),
            (w.end[0] + n[0] * h, w.end[1] + n[1] * h),
            (w.end[0] - n[0] * h, w.end[1] - n[1] * h),
            (w.start[0] - n[0] * h, w.start[1] - n[1] * h),
        ]))
    if not polys:
        return None
    try:
        union = unary_union(polys)
        parts = [union] if union.geom_type == "Polygon" else list(union.geoms)
        best = max((p for p in parts if p.geom_type == "Polygon"),
                   key=lambda p: p.area, default=None)
        return best.exterior if best is not None else None
    except Exception:
        return None


def label_exterior(walls: Sequence[Wall],
                   footprint: Sequence[XY] = ()) -> None:
    """Mark walls on the building envelope as exterior.

    Thickness alone is not enough — a party wall can be as thick as an outside
    one — so the test is thickness *and* proximity to the footprint boundary.
    """
    if not walls:
        return
    thick = sorted((w.thickness for w in walls), reverse=True)
    ref = thick[max(0, len(thick) // 10)] if thick else DEFAULT_THICKNESS
    ring = None
    if footprint and len(footprint) >= 3:
        try:
            from shapely.geometry import Polygon
            ring = Polygon(footprint).exterior
        except Exception:
            ring = None
    if ring is None:
        # No footprint yet — derive one from the walls themselves. This runs
        # before rooms exist, because whether a wall is exterior is part of
        # deciding what the openings in it are, and that has to be settled
        # before rooms can be linked to them.
        ring = _wall_envelope_ring(walls)
    for w in walls:
        on_envelope = True
        if ring is not None:
            try:
                from shapely.geometry import Point
                mid = Point(*w.midpoint)
                on_envelope = ring.distance(mid) <= max(0.35, w.thickness * 1.5)
            except Exception:
                on_envelope = True
        if on_envelope and w.thickness >= ref * EXTERIOR_THICKNESS_RATIO:
            w.kind = "exterior"
        elif w.thickness <= 0.085:
            w.kind = "partition"
        else:
            w.kind = "interior"
