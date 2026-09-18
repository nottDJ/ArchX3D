"""
ArchX3D — Planar topology and room extraction
=============================================
Turns a set of wall centrelines into a planar graph, and the graph's bounded
faces into rooms.

Why the graph comes first
-------------------------
Rooms are not found by looking for rooms. They are what is left over when the
walls are drawn: every bounded face of the planar subdivision induced by the
wall centrelines is a candidate room, and no candidate can exist that the
walls do not enclose. That ordering is the whole point — it is impossible for
this stage to invent a room in a place the drawing has no walls, and equally
impossible for it to miss one that the walls do enclose.

The old engine did the opposite. It had no topology at all: rooms came from
clustering text labels, so a plan with no labels had no rooms, a label in the
wrong place moved a room, and the room's extent was a bounding box around
whatever the clustering swept up. Labels are used here too — but only to *name*
a face that geometry has already found.

Centrelines, then interiors
---------------------------
Polygonising the centrelines gives faces that run down the middle of the
walls, so each is half a wall thickness too big on every side. The interior is
recovered by subtracting the wall solids from the face. Doing it this way
rather than by insetting a fixed amount is what keeps a room bounded by a
100 mm partition on one side and a 200 mm exterior wall on the other correct
on both sides.

Openings do not divide rooms
----------------------------
A doorway is a hole in a wall, not a wall, so it never appears in the graph
and a room is never split by one. A cased opening between a dining room and a
great room likewise leaves one face — which is correct: architecturally that
*is* one space. Where such a face carries more than one room label the face is
partitioned between the labels and every part is marked ``open_plan``, so the
distinction between "a wall divides these" and "a drafter named two parts of
one space" survives into the model instead of being flattened away.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import classify as C
from .ir import Node, Room, Wall
from .read import CadDrawing, Label

XY = Tuple[float, float]

#: Faces smaller than this are wall slivers and junction scraps, in m^2. A
#: genuine room — even a broom cupboard — clears it comfortably.
MIN_ROOM_AREA = 0.85

#: A face bigger than this is the outside world leaking in through a gap in
#: the envelope, not a room, in m^2.
MAX_ROOM_AREA = 400.0

#: A label must be at least this far inside a face to name it, in metres.
#: Keeps a label sitting on a wall from naming the room on the wrong side.
LABEL_INSET = 0.02

#: Labels this close together, in metres, are lines of one name
#: ("MASTER" / "BEDROOM"). Scaled by text height, since a drawing's line
#: spacing is proportional to its text size.
LABEL_LINE_SPACING = 2.2

#: Text taller than this is a sheet title, not a room name, in metres.
MAX_LABEL_HEIGHT = 0.75


# ---------------------------------------------------------------------------
# Label cleaning and room typing
# ---------------------------------------------------------------------------

_FORMAT_CODES = re.compile(r"%%[uUoOdDpPcC]|\\[A-Za-z][^;]*;|[{}]")
_WS = re.compile(r"\s+")

#: Keyword -> room type. Ordered longest-first at match time so "MASTER BATH"
#: is a bathroom rather than a bedroom.
ROOM_TYPES: Dict[str, str] = {
    "master bath": "bathroom", "mstr. bath": "bathroom", "mstr bath": "bathroom",
    "powder": "bathroom", "bathroom": "bathroom", "bath": "bathroom",
    "toilet": "bathroom", "toil": "bathroom", "w.c": "bathroom", "wc": "bathroom",
    "ensuite": "bathroom", "en-suite": "bathroom", "shower": "bathroom",
    "master bedroom": "bedroom", "master": "bedroom", "bedroom": "bedroom",
    "bed room": "bedroom", "bdrm": "bedroom", "guest": "bedroom",
    "nursery": "bedroom",
    "kitchen": "kitchen", "kitchenette": "kitchen", "pantry": "pantry",
    "dining": "dining", "breakfast": "dining", "nook": "dining",
    "great room": "living", "family": "living", "living": "living",
    "lounge": "living", "den": "living", "study": "office", "office": "office",
    "library": "office",
    "garage": "garage", "carport": "garage",
    "porch": "porch", "deck": "deck", "patio": "patio", "balcony": "balcony",
    "terrace": "terrace", "veranda": "porch", "lanai": "porch",
    "hall": "circulation", "hallway": "circulation", "corridor": "circulation",
    "entry": "circulation", "foyer": "circulation", "vestibule": "circulation",
    "landing": "circulation", "stair": "stair", "stairs": "stair",
    "w.i.c": "closet", "wic": "closet", "closet": "closet", "clo.": "closet",
    "clo": "closet", "wardrobe": "closet", "storage": "storage",
    "store": "storage", "util": "utility", "utility": "utility",
    "laundry": "laundry", "mud": "utility", "mech": "utility",
    "mechanical": "utility", "furnace": "utility",
}

#: Rooms that are outside the weather envelope. They get a floor but no
#: ceiling, and are excluded from the conditioned floor area.
EXTERIOR_TYPES = frozenset({"porch", "deck", "patio", "balcony", "terrace"})


def clean_label(text: str) -> str:
    """Strip CAD formatting codes and collapse whitespace.

    ``%%u`` is AutoCAD's underline toggle and appears in front of most room
    names on this plan; left in, every room would be called ``%%uKITCHEN``.
    """
    text = _FORMAT_CODES.sub("", text or "")
    text = text.replace("\\P", " ").replace("\\n", " ").replace("\n", " ")
    return _WS.sub(" ", text).strip()


def room_type_of(label: str) -> Tuple[str, float]:
    """Map a cleaned room name to a room type, with confidence."""
    s = clean_label(label).lower().strip(" .:-")
    if not s:
        return "unknown", 0.0
    for key in sorted(ROOM_TYPES, key=len, reverse=True):
        if key in s:
            exact = s == key
            return ROOM_TYPES[key], 0.95 if exact else 0.8
    return "unknown", 0.0


#: Words that mark a construction note however short the string is.
_NOTE_WORDS = re.compile(
    r"\b(NOTE|NOTES|MIN|MAX|TYP|SIM|EQ|O\.?C|A\.?F\.?F|GYP|OSB|BD|PLYWD|CONT"
    r"|SEE|REF|DETAIL|SECT|SHT|SHEET|SCALE|REV|SPEC|CODE|GA|CLG|FIN|VERIFY"
    r"|EXIST|EXISTING|PROVIDE|INSTALL|ALL|EACH)\b", re.I)


def _is_room_name(text: str) -> bool:
    """Whether a string reads as a room name rather than a construction note.

    Notes are sentences; room names are one to three words, mostly letters,
    and rarely carry units or punctuation. Getting this wrong is cheap in one
    direction (an unnamed room keeps its geometry) and expensive in the other
    (a room called ``5/8" TYPE "X" GYP. BD. ON CEILING``).
    """
    s = clean_label(text)
    if not s or len(s) > 28:
        return False
    words = s.split()
    if len(words) > 3:
        return False
    # Abbreviations are the norm on plans — W.I.C., CLO., TOIL., MSTR. — so
    # full stops are not counted against the string, only digits and symbols.
    body = [c for c in s if not c.isspace() and c != "."]
    letters = sum(c.isalpha() for c in body)
    if letters < 2 or letters / max(len(body), 1) < 0.6:
        return False
    if any(ch in s for ch in '"#@/\\*+=%:;'):
        return False
    if _NOTE_WORDS.search(s):
        return False
    if re.search(r"\d\s*['\"]|R-\d|\d\s*(MM|CM|FT|IN)\b", s, re.I):
        return False
    return True


def _name_height_band(cands: Sequence[Label]) -> Tuple[float, float]:
    """The text height room names are drawn at on this sheet.

    A drafter letters every room name at one height, and it is the largest
    short text inside the plan — notes and callouts are set smaller so they do
    not compete with it. Selecting that band is what stops ``PLATFORM`` (the
    tail of ``18" MIN. RAISED PLATFORM``, set one size down) from naming the
    garage it happens to sit in. A vocabulary match overrides the band, so a
    room name at a size this rule did not anticipate is still honoured.
    """
    heights = sorted({round(t.height, 4) for t in cands if t.height > 0})
    if not heights:
        return (0.0, MAX_LABEL_HEIGHT)
    top = heights[-1]
    return (top * 0.88, top * 1.14)


def gather_room_labels(drawing: CadDrawing) -> List[Label]:
    """Candidate room names: short strings, merged across their own lines."""
    cands = [t for t in drawing.labels
             if 0.0 < t.height <= MAX_LABEL_HEIGHT and _is_room_name(t.text)]
    if not cands:
        return []
    lo, hi = _name_height_band(cands)
    cands = [t for t in cands
             if lo <= t.height <= hi or room_type_of(t.text)[0] != "unknown"]
    if not cands:
        return []
    cands.sort(key=lambda t: (-t.point[1], t.point[0]))
    used = [False] * len(cands)
    merged: List[Label] = []
    for i, a in enumerate(cands):
        if used[i]:
            continue
        group = [a]
        used[i] = True
        for j in range(i + 1, len(cands)):
            if used[j]:
                continue
            b = cands[j]
            span = max(a.height, b.height) * LABEL_LINE_SPACING
            if abs(b.point[0] - a.point[0]) < span and \
                    0 <= a.point[1] - b.point[1] < span:
                group.append(b)
                used[j] = True
                a = b
        text = " ".join(clean_label(g.text) for g in group)
        cx = sum(g.point[0] for g in group) / len(group)
        cy = sum(g.point[1] for g in group) / len(group)
        merged.append(Label(id=group[0].id, text=text, point=(cx, cy),
                            height=group[0].height, layer=group[0].layer))
    return merged


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

def build_nodes(walls: Sequence[Wall], tol: float = 0.12) -> List[Node]:
    """Cluster wall endpoints into shared nodes and record incidence."""
    clusters: List[List[XY]] = []
    members: List[List[Tuple[Wall, int]]] = []
    grid: Dict[Tuple[int, int], List[int]] = {}
    cell = max(tol, 1e-6)

    def add(p: XY, w: Wall, which: int) -> None:
        key = (int(p[0] // cell), int(p[1] // cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ci in grid.get((key[0] + dx, key[1] + dy), []):
                    if math.dist(clusters[ci][0], p) <= tol:
                        clusters[ci].append(p)
                        members[ci].append((w, which))
                        return
        clusters.append([p])
        members.append([(w, which)])
        grid.setdefault(key, []).append(len(clusters) - 1)

    for w in walls:
        add(w.start, w, 0)
        add(w.end, w, 1)

    nodes: List[Node] = []
    for i, (pts, mem) in enumerate(zip(clusters, members), 1):
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        node = Node(id="n%d" % i, point=(cx, cy),
                    wall_ids=sorted({w.id for w, _ in mem}))
        nodes.append(node)
        for w, which in mem:
            ends = list(w.node_ids)
            ends[which] = node.id
            w.node_ids = (ends[0], ends[1])
    return nodes


def wall_solids(walls: Sequence[Wall], grow: float = 0.0):
    """Each wall as a rectangle of its own thickness, unioned.

    ``grow`` widens every wall slightly before the union. A tiny amount closes
    hairline cracks at junctions that would otherwise leak one room into the
    next when the interiors are cut.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    polys = []
    for w in walls:
        if w.length < 1e-9:
            continue
        n = w.normal
        h = w.thickness / 2.0 + grow
        d = w.direction
        # Extend by half a thickness at each end so corners are mitred solid.
        e = w.thickness / 2.0
        s = (w.start[0] - d[0] * e, w.start[1] - d[1] * e)
        t = (w.end[0] + d[0] * e, w.end[1] + d[1] * e)
        polys.append(Polygon([
            (s[0] + n[0] * h, s[1] + n[1] * h),
            (t[0] + n[0] * h, t[1] + n[1] * h),
            (t[0] - n[0] * h, t[1] - n[1] * h),
            (s[0] - n[0] * h, s[1] - n[1] * h),
        ]))
    if not polys:
        return None
    return unary_union(polys)


# ---------------------------------------------------------------------------
# Faces -> rooms
# ---------------------------------------------------------------------------

@dataclass
class RoomResult:
    rooms: List[Room]
    nodes: List[Node]
    footprint: List[XY]
    footprint_holes: List[List[XY]]
    #: Every outer ring of the envelope, largest first. ``footprint`` is the
    #: first of these; the rest are the detached structures on the same sheet.
    footprint_parts: List[List[XY]] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)


def _poly_xy(poly) -> List[XY]:
    return [(round(x, 4), round(y, 4)) for x, y in poly.exterior.coords[:-1]]


def envelope_of(drawing: CadDrawing):
    """The building outline the drawing states, if it states one.

    A footprint layer is the drafter's own answer to "where does the building
    stop", and it includes the parts that walls do not enclose — the covered
    porch, the deck, the carport. Deriving the envelope from the rooms instead
    silently drops all of them, because a porch has posts rather than walls.
    Returns ``None`` when the drawing has no footprint layer, in which case
    the envelope is derived from the rooms and walls as before.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    polys = []
    for p in drawing.by_role(C.FOOTPRINT):
        if len(p.points) < 3:
            continue
        try:
            poly = Polygon(p.points)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.area >= 1.0:
                polys.append(poly)
        except Exception:
            continue
    if not polys:
        return None
    return unary_union(polys)


def _grown(bounds: Tuple[float, float, float, float], pad: float
           ) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = bounds
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def extract(walls: Sequence[Wall], drawing: Optional[CadDrawing] = None, *,
            min_area: float = MIN_ROOM_AREA,
            max_area: float = MAX_ROOM_AREA,
            envelope=None) -> RoomResult:
    """Find rooms as the bounded faces of the wall-centreline graph."""
    from shapely.geometry import LineString, MultiPolygon, Point, Polygon
    from shapely.ops import polygonize, unary_union

    t0 = time.perf_counter()
    nodes = build_nodes(walls)

    lines = [LineString([w.start, w.end]) for w in walls if w.length > 1e-9]
    if not lines:
        return RoomResult(rooms=[], nodes=nodes, footprint=[], footprint_holes=[],
                          stats={"faces": 0, "seconds": 0.0})

    # Noded on a 10 micron grid. Exact floating-point noding misses a wall end
    # that lies on another wall to fifteen digits but not sixteen, which is
    # the ordinary case on any plan drawn off the axes; the grid is far below
    # anything a drawing can mean.
    # ``shapely.ops.unary_union`` takes no grid size in shapely 2.x — the call
    # raised TypeError and this silently fell back to exact noding — so the
    # grid is applied through ``shapely.union_all``.
    try:
        from shapely import union_all
        noded = union_all(lines, grid_size=1e-5)
    except (ImportError, TypeError):  # shapely < 2.0
        noded = unary_union(lines)
    faces = [f for f in polygonize(noded) if f.is_valid and not f.is_empty]
    solids = wall_solids(walls, grow=0.004)

    labels = gather_room_labels(drawing) if drawing is not None else []

    rooms: List[Room] = []
    dropped_small = dropped_large = 0
    n = 0
    try:
        from shapely import clip_by_rect
    except ImportError:          # shapely < 2.0
        clip_by_rect = None
    for face in faces:
        if face.area < min_area * 0.5:
            dropped_small += 1
            continue
        if solids is None:
            interior = face
        else:
            # Only the walls around this face can cut it; subtracting the
            # whole building's wall solid from every face was most of this
            # stage on a large plan.
            try:
                local = solids if clip_by_rect is None else clip_by_rect(
                    solids, *_grown(face.bounds, 0.01))
                interior = face.difference(local)
            except Exception:    # a clipped solid can be invalid; use it whole
                interior = face.difference(solids)
        parts = _as_polygons(interior)
        if not parts:
            dropped_small += 1
            continue
        for part in parts:
            if part.area < min_area:
                dropped_small += 1
                continue
            if part.area > max_area:
                dropped_large += 1
                continue
            x0, y0, x1, y1 = part.bounds
            nearby = [t for t in labels
                      if x0 <= t.point[0] <= x1 and y0 <= t.point[1] <= y1]
            inset = part.buffer(-LABEL_INSET) if nearby else None
            inside = [t for t in nearby if inset.covers(Point(*t.point))]
            pieces = _split_open_plan(part, inside)
            for piece, text in pieces:
                n += 1
                rooms.append(_make_room("r%d" % n, piece, text,
                                        open_plan=len(pieces) > 1))

    outside, unenclosed = _envelope_spaces(envelope, rooms, solids, labels,
                                           start_index=n)
    rooms.extend(outside)

    if envelope is not None:
        footprint, holes, parts = _rings_of(envelope)
    else:
        footprint, holes, parts = _footprint(rooms, solids)
    _attach_walls(rooms, walls)

    return RoomResult(
        rooms=rooms, nodes=nodes, footprint=footprint, footprint_holes=holes,
        footprint_parts=parts,
        stats={
            "faces": len(faces),
            "rooms": len(rooms),
            "dropped_small": dropped_small,
            "dropped_large": dropped_large,
            "labels": len(labels),
            "named": sum(1 for r in rooms if r.label),
            "exterior_spaces": len(outside),
            "unenclosed_envelope_m2": round(unenclosed, 2),
            "seconds": round(time.perf_counter() - t0, 3),
        })


def _envelope_spaces(envelope, rooms: Sequence[Room], solids,
                     labels: Sequence[Label], *, start_index: int
                     ) -> Tuple[List[Room], float]:
    """Parts of the stated envelope that no wall encloses.

    Two quite different things land here and they must not be confused. A
    covered porch or a deck genuinely has no walls — posts and a roof are the
    whole construction — and the drawing says so by naming it. Anything else
    is a hole in the reconstruction: an envelope area the walls failed to
    close. The first becomes an exterior room; the second is measured and
    returned so validation can report it rather than quietly absorb it.
    """
    if envelope is None:
        return [], 0.0
    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union

    taken = []
    for r in rooms:
        try:
            taken.append(Polygon(r.polygon, r.holes))
        except Exception:
            continue
    if solids is not None:
        taken.append(solids)
    try:
        leftover = envelope.difference(unary_union(taken)) if taken else envelope
    except Exception:
        return [], 0.0

    out: List[Room] = []
    unenclosed = 0.0
    n = start_index
    for part in _as_polygons(leftover):
        # A long thin remainder is the gap between a wall solid and the
        # footprint line, not a space.
        if part.area < MIN_ROOM_AREA * 2 or \
                part.area / max(part.length, 1e-9) < 0.18:
            continue
        inside = [t for t in labels if part.covers(Point(*t.point))]
        exterior = [t for t in inside
                    if room_type_of(t.text)[0] in EXTERIOR_TYPES]
        if not exterior:
            unenclosed += part.area
            continue
        n += 1
        room = _make_room("r%d" % n, part, exterior[0], open_plan=False)
        room.is_exterior = True
        out.append(room)
    return out, unenclosed


def _rings_of(geom) -> Tuple[List[XY], List[List[XY]], List[List[XY]]]:
    """Exterior ring, holes, and every part's ring, largest part first."""
    from shapely.geometry import Polygon
    polys = sorted(_as_polygons(geom), key=lambda p: -p.area)
    if not polys:
        return [], [], []
    parts = [p.simplify(0.012, preserve_topology=True) for p in polys
             if p.area > MIN_ROOM_AREA]
    if not parts:
        parts = [polys[0].simplify(0.012, preserve_topology=True)]
    best = parts[0]
    holes = [[(round(x, 4), round(y, 4)) for x, y in ring.coords[:-1]]
             for ring in best.interiors if Polygon(ring).area > 0.5]
    return _poly_xy(best), holes, [_poly_xy(p) for p in parts]


def _as_polygons(geom) -> List:
    from shapely.geometry import MultiPolygon, Polygon
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def _split_open_plan(poly, labels: Sequence[Label]) -> List[Tuple[object, Optional[Label]]]:
    """Partition one face between the several rooms named inside it.

    Only reached for open-plan space — a face with two or more room names and
    no wall between them. The partition is the Voronoi diagram of the label
    points clipped to the face, which is a deterministic reading of "this half
    is the dining room", and every part is flagged ``open_plan`` so no
    consumer mistakes the boundary for a wall.
    """
    named = [t for t in labels if room_type_of(t.text)[0] != "unknown"]
    if len(named) >= 2:
        labels = named
    elif len(named) == 1:
        # One recognised room name and some unrecognised strings: the
        # recognised one names the whole space. Splitting on a string the
        # vocabulary does not know would invent a room out of a stray word.
        return [(poly, named[0])]
    if len(labels) <= 1:
        return [(poly, labels[0] if labels else None)]
    from shapely.geometry import Point
    from shapely.ops import voronoi_diagram
    from shapely.geometry import MultiPoint
    try:
        cells = voronoi_diagram(MultiPoint([Point(*t.point) for t in labels]),
                                envelope=poly, tolerance=0.0)
    except Exception:
        return [(poly, labels[0])]
    out: List[Tuple[object, Optional[Label]]] = []
    for cell in cells.geoms:
        piece = cell.intersection(poly)
        for part in _as_polygons(piece):
            if part.area < MIN_ROOM_AREA:
                continue
            owner = min(labels, key=lambda t: part.centroid.distance(Point(*t.point)))
            out.append((part, owner))
    return out or [(poly, labels[0])]


def _make_room(rid: str, poly, label: Optional[Label], *, open_plan: bool) -> Room:
    text = clean_label(label.text) if label is not None else None
    rtype, conf = room_type_of(text or "")
    holes = [[(round(x, 4), round(y, 4)) for x, y in ring.coords[:-1]]
             for ring in poly.interiors]
    return Room(
        id=rid, polygon=_poly_xy(poly), area=round(poly.area, 4),
        centroid=(round(poly.centroid.x, 4), round(poly.centroid.y, 4)),
        holes=holes, label=text, label_confidence=conf if text else 0.0,
        room_type=rtype, is_exterior=rtype in EXTERIOR_TYPES,
        open_plan=open_plan,
        source_ids=[label.id] if label is not None else [],
    )


def _footprint(rooms: Sequence[Room], solids
               ) -> Tuple[List[XY], List[List[XY]], List[List[XY]]]:
    """The building outline: the rooms plus the walls around them.

    Derived rather than taken from a bounding box, so an L-shaped plan with a
    garage wing and a porch comes out L-shaped with a garage wing and a porch.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    parts = []
    for r in rooms:
        try:
            parts.append(Polygon(r.polygon, r.holes))
        except Exception:
            continue
    if solids is not None:
        parts.append(solids)
    if not parts:
        return [], [], []
    union = unary_union(parts).buffer(0.01).buffer(-0.01)
    return _rings_of(union)


def _attach_walls(rooms: Sequence[Room], walls: Sequence[Wall]) -> None:
    """Record which walls bound each room, and which rooms neighbour each other."""
    from shapely.geometry import LineString, Polygon
    polys = {}
    for r in rooms:
        try:
            polys[r.id] = Polygon(r.polygon, r.holes)
        except Exception:
            continue
    # Indexed: every wall against every room is 89,000 distance computations
    # on a large site plan, and the answer only ever involves the handful of
    # rooms the wall actually runs past.
    ids = [r.id for r in rooms if r.id in polys]
    shapes = [polys[i] for i in ids]
    tree = None
    if shapes:
        try:
            from shapely.strtree import STRtree
            tree = STRtree(shapes)
        except Exception:
            tree = None
    by_id = {r.id: r for r in rooms}

    for w in walls:
        if w.length < 1e-9:
            continue
        line = LineString([w.start, w.end])
        reach = w.thickness / 2.0 + 0.06
        if tree is not None:
            near = [by_id[ids[int(k)]] for k in tree.query(line.buffer(reach))]
        else:
            near = [r for r in rooms if r.id in polys]
        touching = [r for r in near if polys[r.id].distance(line) <= reach]
        for r in touching:
            r.boundary_wall_ids.append(w.id)
        for i, a in enumerate(touching):
            for b in touching[i + 1:]:
                if b.id not in a.neighbour_ids:
                    a.neighbour_ids.append(b.id)
                if a.id not in b.neighbour_ids:
                    b.neighbour_ids.append(a.id)
    for r in rooms:
        r.boundary_wall_ids = sorted(set(r.boundary_wall_ids))
