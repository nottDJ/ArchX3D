"""
ArchX3D — Structures: which walls belong to the same physical building
=======================================================================
Splits one drawing's reconstructed walls into separate structures, from the
wall topology alone.

What a structure is
-------------------
A structure is a set of walls that physically hang together. The test is
geometric, never a filename, a coordinate or a count: two walls are in the
same structure if they touch, and two groups of touching walls are the same
structure if the gap between them is smaller than any real separation between
buildings (:data:`STRUCTURE_GAP`). Everything else is a *different* structure.

A structure is not yet a building. Two structures may be a house and its
detached garage (two buildings), or a ground-floor plan and a first-floor plan
drawn side by side (one building, two storeys). That question needs text and
layer evidence and is answered by :mod:`modules.recon.levels`. This module
only guarantees that it never welds two separate pieces of construction into
one, and never splits one piece into two.

Substance and fragments
-----------------------
Real sheets carry wall-layer geometry that encloses nothing: a boundary wall
stub, a freestanding screen, a planter, a length of fence, the post of a deck.
Treating each of those as a building would report sixty buildings on a site
plan. So a group of walls only founds a structure when it *encloses space*
(:data:`MIN_ENCLOSED_AREA`). A group that encloses nothing is a fragment: it
joins the structure it sits against when one is within reach, and otherwise
it is reported as unassigned wall — neither silently extruded as a building
nor silently deleted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .ir import Node, Opening, Room, Wall

XY = Tuple[float, float]

#: Two groups of walls closer than this, in metres, are one structure. Large
#: enough to absorb a joint the wall repair could not close; smaller than the
#: narrowest real gap between two separate buildings (fire separation, access
#: paths and setbacks are all wider).
STRUCTURE_GAP = 0.6

#: A group of walls must enclose at least this much space, in m^2, to be a
#: structure in its own right rather than a fragment. A guard booth or a
#: garden store clears it; a wall stub, a fence line or a column does not.
MIN_ENCLOSED_AREA = 4.0

#: A fragment within this distance of a structure, in metres, belongs to it —
#: a deck post, a garden wall returning from the house, a porch screen.
FRAGMENT_REACH = 3.0

#: Two separate structures closer than this, in metres, are flagged for review:
#: they are probably separate buildings, but a missing wall could also have
#: split one building in two, and the user should get to see which.
CLOSE_STRUCTURES = 2.0


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Structure:
    """One physically connected piece of construction and what it owns."""

    id: str
    walls: List[Wall]
    enclosed_area: float
    #: The ground the structure covers: its wall solids with every enclosed
    #: space filled in. Shapely geometry.
    region: object = None
    fragments: List[str] = field(default_factory=list)
    rooms: List[Room] = field(default_factory=list)
    openings: List[Opening] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)
    footprint: List[XY] = field(default_factory=list)
    footprint_holes: List[List[XY]] = field(default_factory=list)
    footprint_parts: List[List[XY]] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

    @property
    def wall_ids(self) -> List[str]:
        return [w.id for w in self.walls]

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        xs = [p[0] for w in self.walls for p in (w.start, w.end)]
        ys = [p[1] for w in self.walls for p in (w.start, w.end)]
        if self.region is not None and not self.region.is_empty:
            x0, y0, x1, y1 = self.region.bounds
            xs += [x0, x1]
            ys += [y0, y1]
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def size(self) -> float:
        x0, y0, x1, y1 = self.bounds
        return max(x1 - x0, y1 - y0)

    @property
    def floor_area(self) -> float:
        return sum(r.area for r in self.rooms if not r.is_exterior)


@dataclass
class Partition:
    structures: List[Structure]
    #: ``{"wall_ids", "length_m", "reason"}`` per group of walls that belongs
    #: to no structure.
    unassigned: List[Dict[str, object]] = field(default_factory=list)
    #: Pairs of structures close enough to deserve a second look.
    close_pairs: List[Dict[str, object]] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)

    def structure_of_wall(self) -> Dict[str, Structure]:
        return {w.id: s for s in self.structures for w in s.walls}


# ---------------------------------------------------------------------------
# Wall connectivity
# ---------------------------------------------------------------------------

def touching_pairs(walls: Sequence[Wall]):
    """Every pair of walls that meet, found through a spatial index.

    "Meet" is centreline distance within the thicker wall's thickness, which
    covers L and T junctions whose centrelines stop half a thickness short of
    each other as well as ends that were welded together.
    """
    try:
        from shapely.geometry import LineString
        from shapely.strtree import STRtree
    except Exception:  # pragma: no cover - shapely is a hard dependency
        for i, a in enumerate(walls):
            for b in walls[i + 1:]:
                if min(math.dist(p, q) for p in (a.start, a.end)
                       for q in (b.start, b.end)) <= max(a.thickness, b.thickness):
                    yield a, b
        return

    lines = [LineString([w.start, w.end]) for w in walls]
    if not lines:
        return
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


class _UnionFind:
    def __init__(self, keys: Iterable) -> None:
        self.parent = {k: k for k in keys}

    def find(self, x):
        p = self.parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic root choice keeps ids stable run to run.
            if str(ra) < str(rb):
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb

    def groups(self) -> Dict[object, List]:
        out: Dict[object, List] = {}
        for k in self.parent:
            out.setdefault(self.find(k), []).append(k)
        return out


def wall_components(walls: Sequence[Wall]) -> List[List[Wall]]:
    """Walls grouped by physical contact, largest total length first."""
    uf = _UnionFind(w.id for w in walls)
    for a, b in touching_pairs(walls):
        uf.union(a.id, b.id)
    by_id = {w.id: w for w in walls}
    groups = [[by_id[i] for i in ids] for ids in uf.groups().values()]
    order = {w.id: n for n, w in enumerate(walls)}
    for g in groups:
        g.sort(key=lambda w: order[w.id])
    groups.sort(key=lambda g: (-sum(w.length for w in g), order[g[0].id]))
    return groups


def _solids(walls: Sequence[Wall]):
    from .topology import wall_solids
    return wall_solids(walls, grow=0.0)


def _filled(geom):
    """The geometry with every interior ring filled — the ground it covers."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    polys = []
    for g in _polygons(geom):
        polys.append(Polygon(g.exterior))
    return unary_union(polys) if polys else geom


def _polygons(geom) -> List:
    from shapely.geometry import MultiPolygon, Polygon
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


# ---------------------------------------------------------------------------
# Partition
# ---------------------------------------------------------------------------

@dataclass
class _Component:
    walls: List[Wall]
    solid: object
    filled: object
    enclosed: float

    @property
    def length(self) -> float:
        return sum(w.length for w in self.walls)


def _component(walls: List[Wall]) -> _Component:
    solid = _solids(walls)
    if solid is None:
        from shapely.geometry import Polygon
        solid = Polygon()
    # A hair of closing heals the sub-millimetre cracks junction mitres leave,
    # which would otherwise leak an enclosed room out into the open.
    closed = solid.buffer(0.02, join_style=2).buffer(-0.02, join_style=2)
    filled = _filled(closed)
    enclosed = max(0.0, filled.area - closed.area)
    return _Component(walls=walls, solid=closed, filled=filled, enclosed=enclosed)


def partition(walls: Sequence[Wall], *,
              connectors: Sequence = ()) -> Partition:
    """Split walls into structures, fragments attached, strays reported.

    ``connectors`` are polygons that assert "this is one building" — the
    drawing's own footprint outline. They may pull a fragment into the
    structure they outline; they never merge two structures that each enclose
    space, because a site boundary drawn on a footprint-like layer would then
    weld every building on the site into one.
    """
    walls = list(walls)
    if not walls:
        return Partition(structures=[], stats={"components": 0})

    comps = [_component(g) for g in wall_components(walls)]
    substantial = [c for c in comps if c.enclosed >= MIN_ENCLOSED_AREA]
    fragments = [c for c in comps if c.enclosed < MIN_ENCLOSED_AREA]

    if not substantial:
        # Nothing encloses space anywhere. There is no structure to divide,
        # and inventing one per wall group would hide the real finding —
        # which validation reports — that the walls do not close.
        s = Structure(id="s1", walls=walls, enclosed_area=0.0,
                      region=_filled(_solids(walls)),
                      evidence=["no wall group encloses space; kept whole "
                                "so validation can report why"])
        return Partition(structures=[s], stats={
            "components": len(comps), "substantial": 0,
            "fragments": len(fragments), "structures": 1})

    # -- merge substantial components that are really one structure ---------
    from shapely.strtree import STRtree
    uf = _UnionFind(range(len(substantial)))
    tree = STRtree([c.filled for c in substantial])
    for i, c in enumerate(substantial):
        for j in tree.query(c.filled.buffer(STRUCTURE_GAP)):
            j = int(j)
            if j <= i:
                continue
            if c.filled.distance(substantial[j].filled) <= STRUCTURE_GAP:
                uf.union(i, j)

    groups = sorted(uf.groups().values(),
                    key=lambda idx: -sum(substantial[k].length for k in idx))
    structures: List[Structure] = []
    for n, idx in enumerate(groups, 1):
        members = [substantial[k] for k in sorted(idx)]
        from shapely.ops import unary_union
        region = unary_union([m.filled for m in members])
        s_walls = [w for m in members for w in m.walls]
        s = Structure(id="s%d" % n, walls=s_walls,
                      enclosed_area=sum(m.enclosed for m in members),
                      region=region)
        s.evidence.append("%d connected wall group(s) enclosing %.1f m2"
                          % (len(members), s.enclosed_area))
        structures.append(s)

    # -- fragments ------------------------------------------------------------
    connector_polys = [p for p in connectors if p is not None and not p.is_empty]
    unassigned: List[Dict[str, object]] = []
    regions = [s.region for s in structures]
    for c in fragments:
        best: Optional[Tuple[float, int]] = None
        for k, region in enumerate(regions):
            d = region.distance(c.solid)
            if best is None or d < best[0]:
                best = (d, k)
        host = None
        reason = ""
        if best is not None and best[0] <= FRAGMENT_REACH:
            host = best[1]
            reason = "within %.2f m" % best[0]
        else:
            # A stated footprint outline that contains both the fragment and
            # a structure says they are one building, however far apart.
            for poly in connector_polys:
                if not poly.intersects(c.solid):
                    continue
                hits = [k for k, region in enumerate(regions)
                        if poly.intersects(region)]
                if len(hits) == 1:
                    host = hits[0]
                    reason = "inside the drawing's footprint outline"
                    break
        if host is None:
            unassigned.append({
                "wall_ids": [w.id for w in c.walls],
                "length_m": round(c.length, 2),
                "reason": ("encloses no space and is %.1f m from the nearest "
                           "structure" % best[0]) if best else "encloses no space",
            })
            continue
        s = structures[host]
        s.walls.extend(c.walls)
        s.fragments.append(",".join(w.id for w in c.walls))
        s.evidence.append("absorbed %.1f m of unenclosing wall %s"
                          % (c.length, reason))

    # Walls in their original order within each structure, so ids and
    # downstream iteration are stable.
    order = {w.id: n for n, w in enumerate(walls)}
    for s in structures:
        s.walls.sort(key=lambda w: order[w.id])

    close_pairs: List[Dict[str, object]] = []
    for i, a in enumerate(structures):
        for b in structures[i + 1:]:
            gap = a.region.distance(b.region)
            if gap <= CLOSE_STRUCTURES:
                close_pairs.append({"structures": [a.id, b.id],
                                    "gap_m": round(gap, 2)})

    return Partition(
        structures=structures, unassigned=unassigned, close_pairs=close_pairs,
        stats={
            "components": len(comps),
            "substantial": len(substantial),
            "fragments": len(fragments),
            "structures": len(structures),
            "unassigned_wall_groups": len(unassigned),
            "unassigned_wall_length_m": round(
                sum(float(u["length_m"]) for u in unassigned), 2),
        })


def demote_roomless(part: Partition) -> List[str]:
    """Structures in which no room formed are not buildings.

    Enclosure is judged on wall solids before rooms exist, and a loop of wall
    can enclose space that is no room — the upstand round a balcony, a planter,
    a light well's parapet. Once rooms are known, such a structure joins the
    structure it stands against, or becomes unassigned wall; it never stands
    as a building of its own, and it never fails the drawing by having no
    rooms. The largest structure is never demoted: a drawing whose only
    structure has no rooms is exactly the failure validation must report.
    """
    if len(part.structures) < 2:
        return []
    keep = [s for s in part.structures if s.floor_area > 0]
    if not keep:
        return []
    demoted: List[str] = []
    for s in list(part.structures):
        if s in keep:
            continue
        best = min(keep, key=lambda k: k.region.distance(s.region))
        gap = best.region.distance(s.region)
        if gap <= FRAGMENT_REACH:
            best.walls.extend(s.walls)
            best.rooms.extend(s.rooms)
            best.openings.extend(s.openings)
            best.nodes.extend(s.nodes)
            from shapely.ops import unary_union
            best.region = unary_union([best.region, s.region])
            best.evidence.append("absorbed %s, which enclosed no room, %.2f m away"
                                 % (s.id, gap))
        else:
            part.unassigned.append({
                "wall_ids": [w.id for w in s.walls],
                "length_m": round(sum(w.length for w in s.walls), 2),
                "reason": "encloses space but no room, %.1f m from the nearest "
                          "building" % gap,
            })
        part.structures.remove(s)
        demoted.append(s.id)
    part.close_pairs = [p for p in part.close_pairs
                        if not set(p["structures"]) & set(demoted)]
    part.stats["demoted_roomless"] = len(demoted)
    part.stats["structures"] = len(part.structures)
    return demoted


# ---------------------------------------------------------------------------
# Distribution of rooms, openings, nodes and envelope
# ---------------------------------------------------------------------------

def distribute(part: Partition, *, rooms: Sequence[Room],
               openings: Sequence[Opening], nodes: Sequence[Node],
               footprint_parts: Sequence[Sequence[XY]],
               footprint_holes: Sequence[Sequence[XY]] = ()) -> Dict[str, object]:
    """Hand every room, opening, node and envelope ring to its structure.

    Ownership follows construction: a room belongs to the structure whose
    walls bound it, an opening to the structure its host wall is in. Only a
    room bounded by no wall at all (a porch taken from the footprint outline)
    falls back to where it lies.
    """
    from shapely.geometry import Polygon

    owner = part.structure_of_wall()
    dropped = {"rooms": [], "openings": [], "nodes": 0}

    for r in rooms:
        votes: Dict[str, float] = {}
        for wid in r.boundary_wall_ids:
            s = owner.get(wid)
            if s is not None:
                votes[s.id] = votes.get(s.id, 0.0) + 1.0
        target = None
        if votes:
            best = max(sorted(votes), key=lambda k: votes[k])
            target = next(s for s in part.structures if s.id == best)
        else:
            try:
                poly = Polygon(r.polygon)
                cands = [(s.region.distance(poly), s) for s in part.structures]
                d, s = min(cands, key=lambda t: t[0])
                if d <= FRAGMENT_REACH:
                    target = s
            except Exception:
                target = None
        if target is None:
            dropped["rooms"].append(r.id)
            continue
        target.rooms.append(r)

    for o in openings:
        s = owner.get(o.wall_id)
        if s is None:
            dropped["openings"].append(o.id)
            continue
        s.openings.append(o)

    for n in nodes:
        s = next((owner[w] for w in n.wall_ids if w in owner), None)
        if s is None:
            dropped["nodes"] += 1
            continue
        s.nodes.append(n)

    # Envelope rings go to the structure they cover most of.
    for ring in footprint_parts:
        if len(ring) < 3:
            continue
        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
        except Exception:
            continue
        best = max(part.structures,
                   key=lambda s: s.region.buffer(0.3).intersection(poly).area)
        if best.region.buffer(0.3).intersection(poly).area <= 0:
            continue
        best.footprint_parts.append([tuple(p) for p in ring])

    holes = [list(h) for h in footprint_holes]
    for s in part.structures:
        s.footprint_parts.sort(key=lambda r: -Polygon(r).area)
        s.footprint = list(s.footprint_parts[0]) if s.footprint_parts else []
        if s.footprint:
            fp = Polygon(s.footprint)
            s.footprint_holes = [h for h in holes
                                 if len(h) >= 3 and fp.contains(Polygon(h).centroid)]
    return dropped
