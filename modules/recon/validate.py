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

So the contract is: a :class:`~modules.recon.ir.Drawing` is only ever
returned if it passed here, and a reconstruction that cannot pass is raised as
a :class:`~modules.recon.ir.ReconstructionError` carrying the diagnostics.
"No building, and here is why" is a supported outcome. "A wrong building" is
not.

Errors, warnings and ambiguity
------------------------------
An **error** means the geometry is not a building and 3D generation is
refused. A **warning** means something is unusual and worth showing the user,
but the model is still worth building. **Ambiguity** means the geometry is
sound but the drawing does not settle how its plans relate — which are storeys
of which building — and a person has to decide; the model is built exactly as
drawn and says so. The outcome is one of
:data:`~modules.recon.ir.VALID`, :data:`~modules.recon.ir.VALID_WITH_WARNINGS`,
:data:`~modules.recon.ir.AMBIGUOUS`, :data:`~modules.recon.ir.INVALID`.

Checks run per storey, because a storey is the unit that has walls, rooms and
openings; then per building and per drawing, for what only exists at those
levels — how storeys stack, and how much of the sheet belongs to no building.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .ir import (AMBIGUOUS, INVALID, LEVELS_AMBIGUOUS, VALID,
                 VALID_WITH_WARNINGS, Drawing, Level, Opening,
                 ReconstructionError, Room, Wall)
from .structure import touching_pairs

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

#: More than this share of a drawing's wall length belonging to no building
#: means the walls did not resolve into buildings at all.
MAX_UNASSIGNED_SHARE = 0.35


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

    @property
    def status(self) -> str:
        if self.errors:
            return INVALID
        return VALID_WITH_WARNINGS if self.warnings else VALID

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": self.checks,
        }


# ---------------------------------------------------------------------------
# Storey checks
# ---------------------------------------------------------------------------

def _check_scale(b: Level, r: Report, *, primary: bool = True) -> None:
    w, d = b.width, b.depth
    r.checks["size_m"] = [round(w, 3), round(d, 3)]
    lo, hi = SIZE_BAND
    if w <= 0 or d <= 0:
        r.error("the building has no extent")
        return
    unit = b.units.unit_name if b.units else "unknown"
    if w < lo or d < lo:
        msg = ("the building measures %.2f x %.2f m, too small to be a floor "
               "plan — the unit scale is almost certainly wrong (resolved as "
               "%s)" % (w, d, unit))
        # The unit decision is the drawing's, so it is judged on the drawing's
        # principal plan. A 2 x 2 m store beside a house is small, not wrong.
        if primary:
            r.error(msg)
        else:
            r.warn("a secondary structure measures only %.2f x %.2f m" % (w, d))
    elif w > hi or d > hi:
        r.error("the building measures %.0f x %.0f m, too large to be a floor "
                "plan — the unit scale is almost certainly wrong (resolved as "
                "%s)" % (w, d, unit))
    if primary and b.units and b.units.confidence < 0.5:
        r.warn("the drawing unit could not be determined confidently (%s, %s)"
               % (b.units.unit_name, b.units.reason))
    elif primary and b.units and b.units.conflict:
        r.warn(b.units.conflict)


def _check_walls(b: Level, r: Report) -> None:
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


def _check_connectivity(b: Level, r: Report) -> None:
    """Wall islands. A storey is one connected structure plus what it absorbed.

    A second island here is a fragment the structure stage attached — a deck
    post, a garden wall — which is fine; wall that bounds no room at all is
    the thing that actually signals a failed reconstruction.
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
    for wa, wb in touching_pairs(b.walls):
        if find(wa.id) != find(wb.id):
            union(wa.id, wb.id)

    islands: Dict[str, List[Wall]] = {}
    for w in b.walls:
        islands.setdefault(find(w.id), []).append(w)
    sizes = sorted((sum(x.length for x in g) for g in islands.values()),
                   reverse=True)
    r.checks["wall_islands"] = len(islands)
    r.checks["island_lengths_m"] = [round(s, 1) for s in sizes[:6]]

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


def _check_rooms(b: Level, r: Report) -> None:
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
    try:
        from shapely.strtree import STRtree
        tree = STRtree([p for _, p in polys])
        for i, (ra, pa) in enumerate(polys):
            for j in tree.query(pa):
                j = int(j)
                if j <= i:
                    continue
                pb = polys[j][1]
                try:
                    inter = pa.intersection(pb).area
                except Exception:
                    continue
                if inter > MAX_ROOM_OVERLAP * min(pa.area, pb.area):
                    overlaps += 1
    except Exception:
        pass
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


def _check_openings(b: Level, r: Report) -> None:
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


def _check_annotation_leak(b: Level, r: Report) -> None:
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
# Entry points
# ---------------------------------------------------------------------------

LEVEL_CHECKS = (_check_walls, _check_connectivity, _check_rooms,
                _check_openings, _check_annotation_leak)
#: Kept under its historical name for callers that enumerate the checks.
CHECKS = (_check_scale,) + LEVEL_CHECKS


def validate(level: Level, *, primary: bool = True) -> Report:
    """Run every storey check over one level and return the findings."""
    report = Report()
    try:
        _check_scale(level, report, primary=primary)
    except Exception as exc:
        report.warn("validation check _check_scale failed to run: %s" % exc)
    for check in LEVEL_CHECKS:
        try:
            check(level, report)
        except Exception as exc:      # a broken check must not mask the rest
            report.warn("validation check %s failed to run: %s"
                        % (getattr(check, "__name__", "?"), exc))
    level.validation = report.as_dict()
    level.warnings.extend(w for w in report.warnings if w not in level.warnings)
    return report


def validate_drawing(drawing: Drawing) -> Report:
    """Validate every storey, every building and the drawing as a whole.

    The drawing's report carries every storey's errors and warnings prefixed
    with where they came from, so one list says everything; each storey keeps
    its own report as well.
    """
    report = Report()
    levels = list(drawing.levels())
    multi = len(levels) > 1
    report.checks["building_count"] = len(drawing.buildings)
    report.checks["level_count"] = len(levels)
    if not levels:
        report.error("no building could be reconstructed from the drawing")

    primary = max(levels, key=lambda l: (l.floor_area, l.total_wall_length),
                  default=None)
    per_level: Dict[str, dict] = {}
    for level in levels:
        sub = validate(level, primary=level is primary)
        per_level[level.id] = sub.as_dict()
        where = ("%s: " % level.id) if multi else ""
        report.errors.extend(where + e for e in sub.errors)
        report.warnings.extend(where + w for w in sub.warnings)
    report.checks["levels"] = per_level

    for b in drawing.buildings:
        b_warn: List[str] = []
        for l in b.levels:
            if len(b.levels) > 1 and l.confidence < 0.25:
                b_warn.append("%s is placed over the storey below with low "
                              "confidence (%.2f)" % (l.id, l.confidence))
        stack = _check_stacking(b)
        b_warn.extend(stack)
        estimated = [l for l in b.levels if l.elevation_source == "estimated"]
        if estimated:
            # P2: an estimated storey height must not read as a measured one.
            b_warn.append(
                "storey elevations are estimated, not measured: %s were placed "
                "at multiples of the %.2f m storey height because the drawing "
                "states no level heights"
                % (", ".join("%s (%.2f m)" % (l.name, l.elevation)
                             for l in estimated),
                   drawing.default_wall_height))
        b.validation = {"warnings": b_warn, "levels": len(b.levels)}
        report.warnings.extend("%s: %s" % (b.id, w) for w in b_warn)

    assigned = sum(l.total_wall_length for l in levels)
    stray = sum(float(u.get("length_m", 0.0)) for u in drawing.unassigned)
    share = stray / max(assigned + stray, 1e-9)
    report.checks["unassigned_wall_length_m"] = round(stray, 2)
    report.checks["unassigned_wall_share"] = round(share, 3)
    if levels and share > MAX_UNASSIGNED_SHARE:
        report.error("%.0f%% of the reconstructed wall length (%.1f m) belongs to "
                     "no building, so the walls did not resolve into buildings"
                     % (share * 100, stray))
    elif stray > 0:
        report.warn("%.1f m of wall belongs to no building and was not built"
                    % stray)

    _check_outlying(drawing, report)

    quality = quality_metrics(drawing)
    _judge_quality(quality, report, drawing)

    status_levels = drawing.level_structure.get("status")
    report.checks["level_structure"] = status_levels
    drawing.validation = report.as_dict()
    if report.ok and status_levels == LEVELS_AMBIGUOUS:
        drawing.validation["status"] = AMBIGUOUS
    quality["status"] = drawing.validation["status"]
    drawing.validation["quality"] = quality
    drawing.validation["review"] = len(drawing.review)
    drawing.warnings.extend(w for w in report.warnings if w not in drawing.warnings)
    return report


# ---------------------------------------------------------------------------
# Quality metrics
# ---------------------------------------------------------------------------

#: A drawing with at least this many walls, most of which touch nothing, did
#: not reconstruct into construction: its "walls" are loose line work.
FRAGMENTED_MIN_WALLS = 30
FRAGMENTED_ERROR_RATIO = 0.5
FRAGMENTED_WARN_RATIO = 0.25

#: Below this scale confidence the model's size is not trusted; below the
#: error value, with no confident unit decision either, it is refused.
SCALE_WARN = 0.5
SCALE_ERROR = 0.25


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    k = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[k]


def scale_evidence(drawing: Drawing) -> Dict[str, object]:
    """Independent measurements that each say whether the scale is right.

    A unit decision is one number; a wrong one makes *everything* the wrong
    size together. So it is checked against things architecture fixes
    independently of any drawing: how thick walls are, how wide doors are,
    how big rooms are, how big a building is. Each check that the drawing can
    answer votes; the scale confidence is the share that agree.
    """
    levels = list(drawing.levels())
    checks: Dict[str, Dict[str, object]] = {}

    thick = [w.thickness for l in levels for w in l.walls]
    if thick:
        t = _percentile(thick, 0.5)
        checks["median_wall_thickness_m"] = {"value": round(t, 4),
                                             "plausible": 0.05 <= t <= 0.6}
    doors = [o.width for l in levels for o in l.openings
             if o.classification in ("door", "probable_door")]
    if len(doors) >= 2:
        dw = _percentile(doors, 0.5)
        checks["median_door_width_m"] = {"value": round(dw, 3),
                                         "plausible": 0.55 <= dw <= 1.8}
    rooms = [r.area for l in levels for r in l.rooms if not r.is_exterior]
    if rooms:
        ra = _percentile(rooms, 0.5)
        checks["median_room_area_m2"] = {"value": round(ra, 2),
                                         "plausible": 1.5 <= ra <= 120.0}
    if levels:
        big = max(levels, key=lambda l: l.width * l.depth)
        extent = max(big.width, big.depth)
        checks["largest_extent_m"] = {"value": round(extent, 2),
                                      "plausible": SIZE_BAND[0] <= extent <= SIZE_BAND[1]}
    votes = [c["plausible"] for c in checks.values()]
    confidence = (sum(1 for v in votes if v) / len(votes)) if votes else 0.0
    return {"confidence": round(confidence, 3), "checks": checks}


def quality_metrics(drawing: Drawing) -> Dict[str, object]:
    """The numbers that describe how good a reconstruction is.

    Every figure is computed from the model itself, so it can be tracked
    across versions of the engine and compared between drawings.
    """
    levels = list(drawing.levels())
    walls = [w for l in levels for w in l.walls]
    rooms = [r for l in levels for r in l.rooms]
    openings = [o for l in levels for o in l.openings]
    thick = [w.thickness for w in walls]

    bins: Dict[str, int] = {}
    for t in thick:
        key = "%.3f" % (round(t / 0.025) * 0.025)
        bins[key] = bins.get(key, 0) + 1

    overlaps = 0
    worst_overlap = 0.0
    self_intersecting = 0
    try:
        from shapely.geometry import Polygon
        from shapely.strtree import STRtree
        for l in levels:
            polys = []
            for r in l.rooms:
                if len(r.polygon) < 3:
                    continue
                p = Polygon(r.polygon, r.holes)
                if not p.is_valid:
                    self_intersecting += 1
                    p = p.buffer(0)
                polys.append(p)
            for ring in (l.footprint_parts or ([l.footprint] if l.footprint else [])):
                if len(ring) >= 3 and not Polygon(ring).is_valid:
                    self_intersecting += 1
            if len(polys) > 1:
                tree = STRtree(polys)
                for i, a in enumerate(polys):
                    for j in tree.query(a):
                        j = int(j)
                        if j <= i:
                            continue
                        inter = a.intersection(polys[j]).area
                        if inter <= 0:
                            continue
                        ratio = inter / max(min(a.area, polys[j].area), 1e-9)
                        worst_overlap = max(worst_overlap, ratio)
                        if ratio > MAX_ROOM_OVERLAP:
                            overlaps += 1
    except Exception:
        pass

    islands = [int((l.validation.get("checks") or {}).get("wall_islands", 0) or 0)
               for l in levels]
    support = drawn_support(drawing, walls)
    part = drawing.stats.get("structures") or {}
    room_stats = drawing.stats.get("rooms") or {}
    scale = scale_evidence(drawing)
    units = drawing.units
    ambiguous = drawing.level_structure.get("status") == LEVELS_AMBIGUOUS
    unit_conf = float(units.confidence) if units else 0.0
    return {
        "building_count": len(drawing.buildings),
        "level_count": len(levels),
        "wall_count": len(walls),
        "room_count": len(rooms),
        "opening_count": len(openings),
        "openings_by_class": _count(o.classification for o in openings),
        "footprint_area_m2": round(sum(b.footprint_area for b in drawing.buildings), 2),
        "floor_area_m2": round(sum(l.floor_area for l in levels), 2),
        "wall_length_m": round(sum(w.length for w in walls), 2),
        "wall_thickness_distribution": {
            "p10_m": _round(_percentile(thick, 0.1)),
            "median_m": _round(_percentile(thick, 0.5)),
            "p90_m": _round(_percentile(thick, 0.9)),
            "histogram_25mm": dict(sorted(bins.items())),
        },
        "connected_components": {
            "wall_groups_in_drawing": int(part.get("components", 0) or 0),
            "structures": int(part.get("structures", 0) or 0),
            "unassigned_groups": len(drawing.unassigned),
            "wall_islands_per_level": dict(zip([l.id for l in levels], islands)),
        },
        "room_overlap": {"pairs": overlaps, "worst_ratio": round(worst_overlap, 4)},
        "self_intersections": self_intersecting,
        "unbounded_faces": int(room_stats.get("dropped_large", 0) or 0),
        "unenclosed_envelope_m2": float(room_stats.get("unenclosed_envelope_m2", 0.0) or 0.0),
        "unit_confidence": round(unit_conf, 3),
        "scale_confidence": scale["confidence"],
        "scale_evidence": scale["checks"],
        "level_structure": drawing.level_structure.get("status"),
        "drawn_support": support,
        "confidence": round(min(unit_conf, scale["confidence"]) * (0.6 if ambiguous else 1.0), 3),
    }


#: Walls longer than this multiple of the wall line work actually drawn were
#: mostly made by repair — extension, snapping, gap closing — not drawn.
INVENTED_WARN = 1.25
INVENTED_ERROR = 1.6

#: Share of wall length along a plan's dominant pair of directions. Drawn
#: construction follows a grid; below the error value, with walls read from
#: single lines, the "walls" are unrelated strokes.
COHERENCE_WARN = 0.6
COHERENCE_ERROR = 0.35


def drawn_support(drawing: Drawing, walls: Sequence) -> Dict[str, object]:
    """How much of the wall model the drawing's own line work accounts for.

    Two numbers, both measured on the drawing rather than the model. The
    *repair ratio* compares the reconstructed wall length with the line work
    that paired into walls (half of it: two faces per wall) or, read as
    centrelines, all of it. The *orientation coherence* is the share of wall
    length running along the plan's dominant directions, modulo 90 degrees.
    Real plans measure 0.8-1.0 and 0.97-1.0; 120 random strokes measured 3.6
    and 0.15, and without these checks came back as a valid building.
    """
    stats = drawing.stats.get("walls") or {}
    face = float(stats.get("face_length_m", 0.0) or 0.0)
    built = float(stats.get("wall_length_m", 0.0) or 0.0)
    single = stats.get("method") == "single-line"
    drawn = face if single else face / 2.0
    hist: Dict[int, float] = {}
    for w in walls:
        key = int(round(w.angle_deg % 90.0)) % 90
        hist[key] = hist.get(key, 0.0) + w.length
    total = sum(hist.values())
    best = max((sum(hist.get((k + j) % 90, 0.0) for j in range(-3, 4)) for k in range(90)),
               default=0.0)
    return {
        "method": stats.get("method"),
        "drawn_wall_linework_m": round(drawn, 2),
        "reconstructed_wall_m": round(built, 2),
        "repair_ratio": round(built / drawn, 3) if drawn > 0 else None,
        "orientation_coherence": round(best / total, 3) if total > 0 else None,
    }


def _round(v: Optional[float], nd: int = 4) -> Optional[float]:
    return None if v is None else round(v, nd)


def _count(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def _judge_quality(q: Dict[str, object], report: Report, drawing: Drawing) -> None:
    """Turn the metrics that signal a broken reconstruction into findings."""
    walls = int(q["wall_count"])
    groups = int(q["connected_components"]["wall_groups_in_drawing"])
    if walls >= FRAGMENTED_MIN_WALLS and groups:
        ratio = groups / walls
        if ratio > FRAGMENTED_ERROR_RATIO:
            report.error("the %d walls fall into %d disconnected groups — the line "
                         "work never joined into construction" % (walls, groups))
        elif ratio > FRAGMENTED_WARN_RATIO:
            report.warn("the %d walls fall into %d disconnected groups; much of the "
                        "drawing did not join up" % (walls, groups))

    scale = float(q["scale_confidence"])
    unit = float(q["unit_confidence"])
    failing = [name for name, c in q["scale_evidence"].items() if not c["plausible"]]
    if q["scale_evidence"] and scale < SCALE_ERROR and unit < 0.5:
        report.error("the model's proportions are not a building's (%s) and the "
                     "drawing unit is uncertain — the scale is almost certainly "
                     "wrong" % ", ".join(failing))
    elif q["scale_evidence"] and scale < SCALE_WARN:
        report.warn("the model's proportions look wrong for a building (%s)"
                    % ", ".join(failing))

    if int(q["self_intersections"]):
        report.warn("%d room or envelope outline(s) cross themselves"
                    % q["self_intersections"])

    support = q.get("drawn_support") or {}
    ratio = support.get("repair_ratio")
    if ratio is not None and walls:
        if ratio > INVENTED_ERROR:
            report.error("the reconstructed walls (%.0f m) are %.1f times the wall line "
                         "work actually drawn (%.0f m) — most of the wall was made up by "
                         "gap repair, not drawn" % (support["reconstructed_wall_m"], ratio,
                                                    support["drawn_wall_linework_m"]))
        elif ratio > INVENTED_WARN:
            report.warn("the reconstructed walls are %.2f times the wall line work "
                        "drawn; a good deal was added by gap repair" % ratio)
    coherence = support.get("orientation_coherence")
    if coherence is not None and walls >= 10:
        if coherence < COHERENCE_ERROR and support.get("method") == "single-line":
            report.error("only %.0f%% of the wall length runs along the plan's main "
                         "directions — the lines read as walls are unrelated strokes, "
                         "not construction" % (coherence * 100))
        elif coherence < COHERENCE_WARN:
            report.warn("only %.0f%% of the wall length runs along the plan's main "
                        "directions" % (coherence * 100))
    if int(q["unbounded_faces"]):
        report.warn("%d enclosed area(s) were too large to be rooms and were "
                    "discarded — the envelope probably leaks" % q["unbounded_faces"])


def _check_outlying(drawing, report: Report) -> None:
    """Say so when part of the sheet took no part in the reconstruction.

    The frame trimming in ``read.robust_bounds`` is right to exclude a plan
    copied at another scale - letting it decide the drawing's extent wrecks
    unit resolution. Doing it silently was not: the user got a clean
    single-building result with no hint that half the sheet had been set
    aside, and no way to tell that from a drawing that only ever held one
    plan. This does not change what is built; it explains what was not.
    """
    info = getattr(drawing, "outlying", None)
    if not info:
        return

    report.checks["outlying_geometry"] = info
    ow, od = info["size"]
    fw, fd = info["frame_size"]
    share = float(info["segment_fraction"]) * 100

    if info.get("different_scale"):
        report.warn(
            "multiple drawing scales detected: %d%% of the line work forms a "
            "separate cluster measuring %.3f x %.3f m beside a plan of "
            "%.2f x %.2f m. The larger plan was taken as the building and the "
            "smaller geometry was ignored — at that size it cannot be a floor "
            "plan, and letting it set the drawing's extent would break unit "
            "resolution" % (round(share), ow, od, fw, fd))
    else:
        report.warn(
            "%d%% of the line work lies outside the plan and took no part in "
            "the reconstruction: a cluster of %.2f x %.2f m beside a plan of "
            "%.2f x %.2f m. If that is a second plan rather than a detail or a "
            "legend, it was not built" % (round(share), ow, od, fw, fd))


def _check_stacking(b) -> List[str]:
    """An upper storey should mostly stand on the one below it."""
    out: List[str] = []
    if len(b.levels) < 2:
        return out
    try:
        from shapely.affinity import translate
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception:
        return out
    ordered = sorted(b.levels, key=lambda l: l.index)

    def plate(l):
        rings = [r for r in (l.footprint_parts or [l.footprint]) if len(r) >= 3]
        if not rings:
            return None
        poly = unary_union([Polygon(r).buffer(0) for r in rings])
        return translate(poly, l.placement[0], l.placement[1])

    for lower, upper in zip(ordered, ordered[1:]):
        a, c = plate(lower), plate(upper)
        if a is None or c is None or c.area <= 0:
            continue
        supported = a.buffer(0.5).intersection(c).area / c.area
        if supported < 0.5:
            out.append("only %.0f%% of %s stands over %s once registered"
                       % (supported * 100, upper.id, lower.id))
    return out


def enforce(model, *, diagnostics: Optional[dict] = None):
    """Validate, and raise rather than return an invalid model.

    Accepts a :class:`~modules.recon.ir.Drawing` or a single
    :class:`~modules.recon.ir.Level`.
    """
    if isinstance(model, Drawing):
        report = validate_drawing(model)
        # The drawing's own record carries the quality metrics and review
        # items; a refusal is exactly when those numbers are wanted.
        payload = dict(model.validation)
        extra = {"review": list(model.review),
                 "level_structure": {"status": model.level_structure.get("status"),
                                     "reason": model.level_structure.get("reason")}}
    else:
        report = validate(model)
        payload = report.as_dict()
        extra = {}
    if not report.ok:
        raise ReconstructionError(
            "the reconstruction did not pass validation: %s" % report.errors[0],
            stage="validate",
            diagnostics={"validation": payload, **extra, **(diagnostics or {})},
            failures=report.errors,
        )
    return model
