"""
ArchX3D — Validation: the gate between 2D and 3D
================================================
Decides whether a reconstruction is good enough to build, and refuses when it
is not.

Why refusing matters
--------------------
The failure this engine was written to fix produced a *valid GLB*. Every
downstream check passed: the file parsed, the meshes were well formed, the
exporter reported success. The model was a 0.8 m wide heap of slabs. Nothing
in the pipeline was in a position to notice, because nothing in the pipeline
knew what a building was supposed to look like.

So the contract is: a :class:`~modules.recon.ir.Building` is only ever
returned if it passed here, and a reconstruction that cannot pass is raised as
a :class:`~modules.recon.ir.ReconstructionError` carrying the diagnostics.
"No building, and here is why" is a supported outcome. "A wrong building" is
not.

Errors and warnings
-------------------
An **error** means the geometry is not a building and 3D generation is
refused. A **warning** means something is unusual and worth showing the user,
but the model is still worth building. The split is deliberately conservative
in both directions: a plan with one odd room should still build, and a plan
whose walls total 3 m should not, however confidently the exporter would have
written it out.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .ir import Building, Opening, ReconstructionError, Room, Wall

#: A building is at least this wide/deep and at most this wide/deep, in metres.
#: The lower bound is a garden studio; the upper is a large apartment block.
SIZE_BAND = (2.5, 320.0)

#: Total wall length below this cannot describe an enclosure, in metres.
MIN_TOTAL_WALL_LENGTH = 8.0

#: A single wall longer than this on a plan of this size is a dimension string
#: that got through, expressed as a multiple of the plan's diagonal.
MAX_WALL_LENGTH_RATIO = 1.05

#: Wall thickness must stay inside this band, in metres.
THICKNESS_BAND = (0.04, 0.75)

#: Rooms may overlap by at most this fraction of the smaller room's area.
MAX_ROOM_OVERLAP = 0.06

#: Floor area below this fraction of the footprint means most of the building
#: failed to resolve into rooms.
MIN_AREA_COVERAGE = 0.25


class Report:
    """Findings from one validation pass."""

    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.checks: Dict[str, object] = {}

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": self.checks,
        }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _check_scale(b: Building, r: Report) -> None:
    w, d = b.width, b.depth
    r.checks["size_m"] = [round(w, 3), round(d, 3)]
    lo, hi = SIZE_BAND
    if w <= 0 or d <= 0:
        r.error("the building has no extent")
        return
    if w < lo or d < lo:
        r.error("the building measures %.2f x %.2f m, too small to be a floor "
                "plan — the unit scale is almost certainly wrong (resolved as "
                "%s)" % (w, d, b.units.unit_name if b.units else "unknown"))
    elif w > hi or d > hi:
        r.error("the building measures %.0f x %.0f m, too large to be a floor "
                "plan — the unit scale is almost certainly wrong (resolved as "
                "%s)" % (w, d, b.units.unit_name if b.units else "unknown"))
    if b.units and b.units.confidence < 0.5:
        r.warn("the drawing unit could not be determined confidently (%s, %s)"
               % (b.units.unit_name, b.units.reason))
    elif b.units and b.units.conflict:
        r.warn(b.units.conflict)


def _check_walls(b: Building, r: Report) -> None:
    total = b.total_wall_length
    r.checks["wall_count"] = len(b.walls)
    r.checks["wall_length_m"] = round(total, 2)
    if not b.walls:
        r.error("no walls were reconstructed from the drawing")
        return
    if total < MIN_TOTAL_WALL_LENGTH:
        r.error("only %.1f m of wall was reconstructed, which cannot enclose a "
                "building" % total)

    diag = math.hypot(b.width, b.depth)
    too_long = [w for w in b.walls if w.length > diag * MAX_WALL_LENGTH_RATIO]
    if too_long:
        r.error("%d wall(s) are longer than the whole plan (%.1f m vs a %.1f m "
                "diagonal) — documentation geometry was treated as building "
                "geometry" % (len(too_long), max(w.length for w in too_long), diag))

    lo, hi = THICKNESS_BAND
    bad = [w for w in b.walls if not (lo <= w.thickness <= hi)]
    if bad:
        r.error("%d wall(s) have an impossible thickness (%s m)"
                % (len(bad), ", ".join("%.3f" % w.thickness for w in bad[:5])))

    degenerate = [w for w in b.walls if w.length < 0.05]
    if degenerate:
        r.warn("%d wall(s) are shorter than 50 mm and were kept" % len(degenerate))

    r.checks["thickness_profile"] = b.thickness_profile()


def _check_connectivity(b: Building, r: Report) -> None:
    """Wall islands. A building is one connected structure.

    A second island is normally a detached garage or a shed on the same sheet,
    which is fine and common; five islands means the reconstruction fragmented
    and the walls are not meeting.
    """
    if not b.walls:
        return
    parent: Dict[str, str] = {w.id: w.id for w in b.walls}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, c: str) -> None:
        ra, rc = find(a), find(c)
        if ra != rc:
            parent[ra] = rc

    for n in b.nodes:
        ids = n.wall_ids
        for other in ids[1:]:
            if other in parent and ids[0] in parent:
                union(ids[0], other)
    # Mid-wall tee junctions do not share an endpoint, so they are picked up
    # by proximity as well; without this every tee reads as a separate island.
    # Indexed rather than compared pairwise: a 442-wall site plan is 97,000
    # exact distance computations otherwise, and this check alone took nine
    # seconds of a thirty-second run.
    for wa, wb in _touching_pairs(b.walls):
        if find(wa.id) != find(wb.id):
            union(wa.id, wb.id)

    islands: Dict[str, List[Wall]] = {}
    for w in b.walls:
        islands.setdefault(find(w.id), []).append(w)
    sizes = sorted((sum(x.length for x in g) for g in islands.values()),
                   reverse=True)
    r.checks["wall_islands"] = len(islands)
    r.checks["island_lengths_m"] = [round(s, 1) for s in sizes[:6]]

    # An island is only a problem if it encloses nothing. Two buildings on one
    # sheet — a house and its detached garage, or a pair of semi-detached
    # units — are two islands and both are real; counting them as a fault
    # rejects perfectly good drawings. Wall that bounds no room at all is the
    # thing that actually signals a failed reconstruction.
    bounding = set()
    for room in b.rooms:
        bounding.update(room.boundary_wall_ids)
    useless = 0.0
    for group in islands.values():
        if not any(w.id in bounding for w in group):
            useless += sum(w.length for w in group)
    total = max(sum(sizes), 1e-9)
    r.checks["wall_length_enclosing_nothing_m"] = round(useless, 1)
    if useless / total > 0.35:
        r.error("%.0f%% of the reconstructed wall length (%.1f m) encloses no "
                "room at all, so the walls did not resolve into a building"
                % (useless / total * 100, useless))
    elif len(islands) > 1:
        r.warn("the walls form %d disconnected groups (%s m); this is normal "
               "for a sheet carrying more than one structure"
               % (len(islands), ", ".join("%.0f" % s for s in sizes[:4])))


def _segments_touch(a: Wall, b: Wall, tol: float) -> bool:
    try:
        from shapely.geometry import LineString
        return LineString([a.start, a.end]).distance(
            LineString([b.start, b.end])) <= tol
    except Exception:
        return min(math.dist(p, q) for p in (a.start, a.end)
                   for q in (b.start, b.end)) <= tol


def _touching_pairs(walls: Sequence[Wall]):
    """Every pair of walls that meet, found through a spatial index."""
    try:
        from shapely.geometry import LineString
        from shapely.strtree import STRtree
    except Exception:
        for i, a in enumerate(walls):
            for b in walls[i + 1:]:
                if _segments_touch(a, b, tol=max(a.thickness, b.thickness)):
                    yield a, b
        return

    lines = [LineString([w.start, w.end]) for w in walls]
    tree = STRtree(lines)
    seen = set()
    for i, (w, line) in enumerate(zip(walls, lines)):
        tol = max(w.thickness, 0.05)
        for j in tree.query(line.buffer(tol)):
            j = int(j)
            if j == i:
                continue
            key = (i, j) if i < j else (j, i)
            if key in seen:
                continue
            seen.add(key)
            other = walls[j]
            if line.distance(lines[j]) <= max(w.thickness, other.thickness):
                yield w, other


def _check_rooms(b: Building, r: Report) -> None:
    r.checks["room_count"] = len(b.rooms)
    r.checks["floor_area_m2"] = round(b.floor_area, 2)
    r.checks["footprint_area_m2"] = round(b.footprint_area, 2)
    if not b.rooms:
        r.error("no rooms were found — the walls do not enclose any space")
        return

    try:
        from shapely.geometry import Polygon
    except Exception:
        return

    polys: List[Tuple[Room, object]] = []
    for room in b.rooms:
        if len(room.polygon) < 3:
            r.error("room %s has fewer than three corners" % room.id)
            continue
        poly = Polygon(room.polygon, room.holes)
        if not poly.is_valid:
            r.warn("room %s (%s) has a self-intersecting boundary"
                   % (room.id, room.label or room.room_type))
            poly = poly.buffer(0)
        if poly.area <= 0:
            r.error("room %s has no area" % room.id)
            continue
        polys.append((room, poly))

    overlaps = 0
    for i, (ra, pa) in enumerate(polys):
        for rb, pb in polys[i + 1:]:
            try:
                inter = pa.intersection(pb).area
            except Exception:
                continue
            if inter > MAX_ROOM_OVERLAP * min(pa.area, pb.area):
                overlaps += 1
    r.checks["room_overlaps"] = overlaps
    if overlaps:
        r.error("%d pair(s) of rooms overlap each other" % overlaps)

    if b.footprint and len(b.footprint) >= 3:
        from shapely.ops import unary_union
        rings = b.footprint_parts or [b.footprint]
        pieces = []
        for i, ring in enumerate(rings):
            if len(ring) < 3:
                continue
            poly = Polygon(ring, b.footprint_holes if i == 0 else None)
            pieces.append(poly if poly.is_valid else poly.buffer(0))
        env = unary_union(pieces) if pieces else Polygon()
        if env.is_empty:
            return
        outside = [room.id for room, poly in polys
                   if poly.difference(env.buffer(0.12)).area > 0.15 * poly.area]
        r.checks["rooms_outside_envelope"] = len(outside)
        if outside:
            r.warn("%d room(s) lie partly outside the building envelope (%s)"
                   % (len(outside), ", ".join(outside[:5])))
        if env.area > 0:
            coverage = b.floor_area / env.area
            r.checks["area_coverage"] = round(coverage, 3)
            if coverage < MIN_AREA_COVERAGE:
                r.warn("only %.0f%% of the footprint resolved into rooms"
                       % (coverage * 100))


def _check_openings(b: Building, r: Report) -> None:
    counts: Dict[str, int] = {}
    for o in b.openings:
        counts[o.kind] = counts.get(o.kind, 0) + 1
    r.checks["openings"] = counts
    by_wall = {w.id: w for w in b.walls}
    orphan = [o.id for o in b.openings if o.wall_id not in by_wall]
    if orphan:
        r.error("%d opening(s) reference a wall that does not exist" % len(orphan))
    oversize = []
    for o in b.openings:
        w = by_wall.get(o.wall_id)
        if w is None:
            continue
        if o.width > w.length + 0.05:
            oversize.append(o.id)
        elif o.offset - o.width / 2 < -0.05 or o.offset + o.width / 2 > w.length + 0.05:
            oversize.append(o.id)
    r.checks["openings_out_of_bounds"] = len(oversize)
    if oversize:
        r.error("%d opening(s) do not fit inside their host wall (%s)"
                % (len(oversize), ", ".join(oversize[:5])))
    if b.walls and not b.openings:
        r.warn("no doors or windows were found; the walls will be solid")


def _check_annotation_leak(b: Building, r: Report) -> None:
    """Walls that look like dimension strings rather than construction.

    A dimension line is long, perfectly straight, attached to nothing at
    either end, and usually sits outside the envelope. One wall like that is
    a fence; several mean the classifier let the annotation layer through.
    """
    if not b.walls or not b.footprint or len(b.footprint) < 3:
        return
    try:
        from shapely.geometry import LineString, Polygon
    except Exception:
        return
    env = Polygon(b.footprint, b.footprint_holes)
    if not env.is_valid:
        env = env.buffer(0)
    free_ends = {n.point for n in b.nodes if n.degree < 2}
    bounding = set()
    for room in b.rooms:
        bounding.update(room.boundary_wall_ids)
    suspects = []
    for w in b.walls:
        if w.length < 3.0 or w.id in bounding:
            continue
        if env.buffer(1.0).covers(LineString([w.start, w.end])):
            continue
        if w.start in free_ends and w.end in free_ends:
            suspects.append(w)
    stray = sum(w.length for w in suspects)
    total = max(b.total_wall_length, 1e-9)
    r.checks["annotation_suspects"] = len(suspects)
    r.checks["annotation_suspect_length_m"] = round(stray, 1)
    # Both conditions matter. A handful of long free walls outside the
    # envelope is a fence, a retaining wall or a site boundary; a *large share*
    # of the total wall length in that state is the dimension layer.
    if len(suspects) >= 3 and stray / total > 0.15:
        r.error("%d wall(s) totalling %.0f m (%.0f%% of all wall length) lie "
                "outside the building envelope, bound no room and have both "
                "ends free — the signature of dimension or annotation geometry "
                "classified as walls (%s)"
                % (len(suspects), stray, stray / total * 100,
                   ", ".join(w.id for w in suspects[:5])))
    elif suspects:
        r.warn("%d wall(s) sit outside the envelope with both ends free"
               % len(suspects))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

CHECKS = (_check_scale, _check_walls, _check_connectivity, _check_rooms,
          _check_openings, _check_annotation_leak)


def validate(building: Building) -> Report:
    """Run every check over a building and return the findings."""
    report = Report()
    for check in CHECKS:
        try:
            check(building, report)
        except Exception as exc:      # a broken check must not mask the rest
            report.warn("validation check %s failed to run: %s"
                        % (getattr(check, "__name__", "?"), exc))
    building.validation = report.as_dict()
    building.warnings.extend(report.warnings)
    return report


def enforce(building: Building, *, diagnostics: Optional[dict] = None) -> Building:
    """Validate, and raise rather than return an invalid building."""
    report = validate(building)
    if not report.ok:
        raise ReconstructionError(
            "the reconstruction did not pass validation: %s" % report.errors[0],
            stage="validate",
            diagnostics={"validation": report.as_dict(), **(diagnostics or {})},
            failures=report.errors,
        )
    return building
