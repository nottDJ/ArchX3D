"""
ArchX3D — Architectural Intermediate Representation
===================================================
The validated architectural model that sits between the DXF and Blender::

    Drawing                      one DXF, one unit decision, one local frame
     ├── Building                a physical structure
     │    ├── Level              one storey of it
     │    │    ├── envelope      footprint rings
     │    │    ├── walls
     │    │    ├── rooms
     │    │    └── openings
     │    └── Level ...
     └── Building ...

Why an IR at all
----------------
The engine this replaces had no building model. ``geometry.json`` carried a
flat list of ``{start, end, layer}`` line segments, and every consumer
re-derived meaning from it: Blender guessed walls by extruding each segment,
the furnisher guessed rooms, the viewer guessed a footprint. Nothing could be
validated because there was nothing to validate — a list of segments is always
"valid", including when it is a picture of a dimension string.

Why buildings and levels are explicit
-------------------------------------
The first version of this IR had exactly one building with exactly one level,
and a drawing that did not fit was flattened into that shape. Real sheets do
not fit: one carries a ground-floor plan and a first-floor plan side by side,
another a house and its detached garage, another two congruent floor plates
with no title saying which is which. Flattening the first gives a building with
two overlapping halves; flattening the second hides that there are two
structures; flattening the third silently picks an answer the drawing does not
support. So the hierarchy is explicit, and the drawing records *how sure* the
engine is about it (:attr:`Drawing.level_structure`).

Frames
------
Everything is in **metres**, in the drawing's **normalised local frame** (the
origin is the minimum corner of the drawing's building geometry). Each level's
walls, rooms and openings stay where the drafter drew them on the sheet, so a
2D consumer sees non-overlapping plans. A level additionally carries a
``placement`` — the translation that registers it over the lowest level of its
building — and an ``elevation``. The 3D stage applies those two numbers and
nothing else; it does not decide how floors stack.

The invariant every stage downstream may rely on: a :class:`Drawing` handed
out by :func:`modules.recon.pipeline.reconstruct` passed
:mod:`modules.recon.validate`. A reconstruction that cannot satisfy the
validator is raised as a :class:`ReconstructionError` carrying the diagnostics
that explain why. "No building" is a supported outcome; "a wrong building" is
not.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "archx3d.ir/3.0"

XY = Tuple[float, float]

#: Validation outcomes, most to least buildable. ``AMBIGUOUS`` is not a
#: failure: the geometry is sound but the drawing does not settle something
#: structural (typically which plans are storeys of which building), and a
#: person has to say.
VALID = "VALID"
VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
AMBIGUOUS = "AMBIGUOUS"
INVALID = "INVALID"
STATUSES = (VALID, VALID_WITH_WARNINGS, AMBIGUOUS, INVALID)

#: Level-structure outcomes recorded on :attr:`Drawing.level_structure`.
LEVELS_SINGLE = "SINGLE_LEVEL"
LEVELS_RESOLVED = "RESOLVED"
LEVELS_AMBIGUOUS = "AMBIGUOUS_LEVEL_STRUCTURE"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ReconstructionError(RuntimeError):
    """Raised when the drawing cannot be reconstructed into a valid model.

    Carries the partial diagnostics so the caller can show the user *where*
    the reconstruction broke down rather than a bare failure. This is the
    mechanism by which the engine refuses to emit garbage 3D.
    """

    def __init__(self, message: str, *, stage: str = "unknown",
                 diagnostics: Optional[dict] = None,
                 failures: Optional[Sequence[str]] = None):
        super().__init__(message)
        self.stage = stage
        self.diagnostics = diagnostics or {}
        self.failures = list(failures or [])

    def as_dict(self) -> dict:
        return {
            "error": str(self),
            "stage": self.stage,
            "failures": self.failures,
            "diagnostics": self.diagnostics,
        }


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

@dataclass
class UnitDecision:
    """How one drawing unit was decided to map to metres, and how sure we are.

    ``candidates`` keeps every unit that was considered with its score, so a
    wrong answer is debuggable rather than mysterious. ``conflict`` is set when
    the evidence contradicts the file's own ``$INSUNITS`` declaration — the
    single most valuable line in the whole diagnostics bundle, because a
    drawing whose header lies is otherwise silently scaled wrong.
    """

    scale_to_m: float
    unit_name: str
    method: str
    confidence: float
    reason: str
    insunits: Optional[int] = None
    conflict: Optional[str] = None
    candidates: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """A junction in the wall network — where two or more centrelines meet."""

    id: str
    point: XY
    wall_ids: List[str] = field(default_factory=list)

    @property
    def degree(self) -> int:
        return len(self.wall_ids)

    def as_dict(self) -> dict:
        return {"id": self.id, "point": list(self.point), "wall_ids": list(self.wall_ids),
                "degree": self.degree}


@dataclass
class Opening:
    """A door, window or cased opening that interrupts a wall.

    ``position`` is the centre of the opening **on the host wall's centreline**,
    so the 3D stage can cut it without re-deriving anything. ``offset`` is the
    same point expressed as a distance from the wall's start, which is what a
    boolean cutter actually needs.
    """

    id: str
    kind: str                      # door | window | cased | garage
    wall_id: str
    position: XY
    offset: float                  # metres from wall.start along the centreline
    width: float
    height: float
    sill_height: float
    thickness: float               # host wall thickness, for the cutter depth
    swing: Optional[str] = None    # left | right | double | slide | None
    rooms: List[str] = field(default_factory=list)
    source_ids: List[str] = field(default_factory=list)
    evidence: str = ""
    confidence: float = 0.5
    level_id: str = ""
    #: How sure the engine is of what this opening *is*, independent of how it
    #: is built: ``door`` (door evidence confirmed by a second, independent
    #: cue), ``probable_door`` (one unconfirmed door cue), ``window``,
    #: ``garage_door``, or ``unknown_opening`` (a hole with no evidence of what
    #: fills it). A false door is worse than an unknown opening, so the tier
    #: only rises on evidence.
    classification: str = "unknown_opening"
    #: Whether the drawing's own wall line work is interrupted where this
    #: opening sits: ``True``, ``False``, or ``None`` when it cannot be told.
    gap_confirmed: Optional[bool] = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["position"] = list(self.position)
        return d


@dataclass
class Wall:
    """One architectural wall: a centreline with a real thickness.

    A wall is *not* a drawn line. It is the thing two drawn lines describe.
    ``left``/``right`` are the reconstructed boundary faces, kept so the
    diagnostics can show the reconstruction against the original CAD lines.
    """

    id: str
    start: XY
    end: XY
    thickness: float
    kind: str = "interior"         # exterior | interior | partition | railing
    height: Optional[float] = None
    node_ids: Tuple[Optional[str], Optional[str]] = (None, None)
    opening_ids: List[str] = field(default_factory=list)
    source_ids: List[str] = field(default_factory=list)
    layer: str = ""
    confidence: float = 1.0
    level_id: str = ""

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def direction(self) -> XY:
        dx, dy = self.end[0] - self.start[0], self.end[1] - self.start[1]
        n = math.hypot(dx, dy) or 1.0
        return (dx / n, dy / n)

    @property
    def normal(self) -> XY:
        ux, uy = self.direction
        return (-uy, ux)

    @property
    def angle_deg(self) -> float:
        ux, uy = self.direction
        return math.degrees(math.atan2(uy, ux))

    @property
    def midpoint(self) -> XY:
        return ((self.start[0] + self.end[0]) / 2.0, (self.start[1] + self.end[1]) / 2.0)

    def left(self) -> Tuple[XY, XY]:
        nx, ny = self.normal
        h = self.thickness / 2.0
        return ((self.start[0] + nx * h, self.start[1] + ny * h),
                (self.end[0] + nx * h, self.end[1] + ny * h))

    def right(self) -> Tuple[XY, XY]:
        nx, ny = self.normal
        h = self.thickness / 2.0
        return ((self.start[0] - nx * h, self.start[1] - ny * h),
                (self.end[0] - nx * h, self.end[1] - ny * h))

    def point_at(self, offset: float) -> XY:
        ux, uy = self.direction
        return (self.start[0] + ux * offset, self.start[1] + uy * offset)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["start"] = list(self.start)
        d["end"] = list(self.end)
        d["node_ids"] = list(self.node_ids)
        d["length"] = round(self.length, 4)
        d["angle_deg"] = round(self.angle_deg, 3)
        return d


@dataclass
class Room:
    """An enclosed region of the plan, derived from wall topology.

    ``polygon`` is the room's own boundary in metres, counter-clockwise, with
    the first point *not* repeated at the end. ``holes`` covers the rare plan
    with a genuine void (a lightwell, a stair core). ``label`` comes from CAD
    text that falls inside the polygon; it annotates the room, it never defines
    it — geometry decides where a room is, text only decides what it is called.
    """

    id: str
    polygon: List[XY]
    area: float
    centroid: XY
    holes: List[List[XY]] = field(default_factory=list)
    label: Optional[str] = None
    label_confidence: float = 0.0
    room_type: str = "unknown"
    boundary_wall_ids: List[str] = field(default_factory=list)
    opening_ids: List[str] = field(default_factory=list)
    neighbour_ids: List[str] = field(default_factory=list)
    is_exterior: bool = False      # porch / deck / balcony — unroofed or open
    open_plan: bool = False        # bounded by a named neighbour, not a wall
    source_ids: List[str] = field(default_factory=list)
    level_id: str = ""

    @property
    def perimeter(self) -> float:
        pts = self.polygon
        return sum(math.dist(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))

    def as_dict(self) -> dict:
        d = asdict(self)
        d["polygon"] = [list(p) for p in self.polygon]
        d["holes"] = [[list(p) for p in h] for h in self.holes]
        d["centroid"] = list(self.centroid)
        d["area"] = round(self.area, 4)
        d["perimeter"] = round(self.perimeter, 4)
        return d


def _by_class(openings: Sequence["Opening"]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for o in openings:
        out[o.classification] = out.get(o.classification, 0) + 1
    return dict(sorted(out.items()))


def _ring_area(pts: Sequence[XY]) -> float:
    if len(pts) < 3:
        return 0.0
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


# ---------------------------------------------------------------------------
# Level — one storey's plan
# ---------------------------------------------------------------------------

@dataclass
class Level:
    """One storey: its envelope, walls, rooms and openings.

    The geometry stays where it was drawn on the sheet. ``placement`` is the
    translation that puts this storey over the lowest storey of its building
    (``(0, 0)`` for that storey itself and for every single-storey building),
    and ``elevation`` is its floor height above that storey's floor. Both are
    decided by :mod:`modules.recon.levels` from the drawing's own evidence and
    recorded in ``evidence``; the 3D stage applies them and decides nothing.
    """

    id: str = "l0"
    name: str = "Level 0"
    index: int = 0
    elevation: float = 0.0
    #: ``"datum"`` or ``"estimated"`` - see ``levels.LevelSpec``. An estimated
    #: elevation must never be presented as a measured one.
    elevation_source: str = "datum"
    height: float = 2.7
    placement: XY = (0.0, 0.0)
    #: The drawing text that named this storey, when one did.
    title: Optional[str] = None
    #: How the storey was identified: ``title`` | ``layer`` | ``assumed``.
    designation: str = "assumed"
    evidence: List[str] = field(default_factory=list)
    confidence: float = 1.0
    building_id: str = ""
    walls: List[Wall] = field(default_factory=list)
    rooms: List[Room] = field(default_factory=list)
    openings: List[Opening] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)
    footprint: List[XY] = field(default_factory=list)
    footprint_holes: List[List[XY]] = field(default_factory=list)
    #: Every outer ring of this storey's envelope, largest first.
    footprint_parts: List[List[XY]] = field(default_factory=list)
    bounds_min: XY = (0.0, 0.0)
    bounds_max: XY = (0.0, 0.0)
    #: The drawing's unit decision, carried so a level can be validated on its
    #: own. Serialised once, on the drawing.
    units: Optional[UnitDecision] = None
    source_path: str = ""
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)
    validation: Dict[str, object] = field(default_factory=dict)

    # -- lookups ------------------------------------------------------------

    def wall(self, wall_id: str) -> Optional[Wall]:
        return next((w for w in self.walls if w.id == wall_id), None)

    def room(self, room_id: str) -> Optional[Room]:
        return next((r for r in self.rooms if r.id == room_id), None)

    def opening(self, opening_id: str) -> Optional[Opening]:
        return next((o for o in self.openings if o.id == opening_id), None)

    def openings_on(self, wall_id: str) -> List[Opening]:
        return [o for o in self.openings if o.wall_id == wall_id]

    # -- derived measures ---------------------------------------------------

    @property
    def default_wall_height(self) -> float:
        return self.height

    @property
    def width(self) -> float:
        return self.bounds_max[0] - self.bounds_min[0]

    @property
    def depth(self) -> float:
        return self.bounds_max[1] - self.bounds_min[1]

    @property
    def total_wall_length(self) -> float:
        return sum(w.length for w in self.walls)

    @property
    def floor_area(self) -> float:
        return sum(r.area for r in self.rooms if not r.is_exterior)

    @property
    def footprint_area(self) -> float:
        if len(self.footprint) < 3:
            return 0.0
        parts = self.footprint_parts or [self.footprint]
        gross = sum(_ring_area(r) for r in parts)
        for hole in self.footprint_holes:
            gross -= _ring_area(hole)
        return gross

    def thickness_profile(self) -> Dict[str, float]:
        """Modal wall thickness per wall kind, for reporting and validation."""
        out: Dict[str, List[float]] = {}
        for w in self.walls:
            out.setdefault(w.kind, []).append(w.thickness)
        return {k: round(sorted(v)[len(v) // 2], 4) for k, v in out.items() if v}

    def summary(self) -> dict:
        return {
            "walls": len(self.walls),
            "rooms": len(self.rooms),
            "openings": len(self.openings),
            "doors": sum(1 for o in self.openings if o.kind in ("door", "garage")),
            "windows": sum(1 for o in self.openings if o.kind == "window"),
            "openings_by_class": _by_class(self.openings),
            "nodes": len(self.nodes),
            "footprint_m2": round(self.footprint_area, 2),
            "floor_area_m2": round(self.floor_area, 2),
            "total_wall_length_m": round(self.total_wall_length, 2),
            "extent_m": [round(self.width, 3), round(self.depth, 3)],
            "thickness_profile": self.thickness_profile(),
        }

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "index": self.index,
            "elevation": round(self.elevation, 4),
            "height": self.height,
            "placement": [round(self.placement[0], 4), round(self.placement[1], 4)],
            "title": self.title,
            "designation": self.designation,
            "evidence": list(self.evidence),
            "confidence": round(self.confidence, 3),
            "building_id": self.building_id,
            "bounds_min": list(self.bounds_min),
            "bounds_max": list(self.bounds_max),
            "footprint": [list(p) for p in self.footprint],
            "footprint_holes": [[list(p) for p in h] for h in self.footprint_holes],
            "footprint_parts": [[list(p) for p in r] for r in self.footprint_parts],
            "walls": [w.as_dict() for w in self.walls],
            "rooms": [r.as_dict() for r in self.rooms],
            "openings": [o.as_dict() for o in self.openings],
            "nodes": [n.as_dict() for n in self.nodes],
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "validation": dict(self.validation),
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# Building — one physical structure, one or more storeys
# ---------------------------------------------------------------------------

@dataclass
class Building:
    """A physical structure: its storeys, and why the engine grouped them.

    ``evidence`` is the chain of reasons this set of plans is one building
    (a shared wall network, plan titles naming its storeys, a designation
    such as ``BLOCK A``). A building is never produced by proximity alone.
    """

    id: str = "b1"
    name: str = "Building 1"
    levels: List[Level] = field(default_factory=list)
    #: The drawing's own name for the building (``BLOCK A``), when it has one.
    designation: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    confidence: float = 1.0
    warnings: List[str] = field(default_factory=list)
    validation: Dict[str, object] = field(default_factory=dict)

    def level(self, level_id: str) -> Optional[Level]:
        return next((l for l in self.levels if l.id == level_id), None)

    @property
    def walls(self) -> List[Wall]:
        return [w for l in self.levels for w in l.walls]

    @property
    def rooms(self) -> List[Room]:
        return [r for l in self.levels for r in l.rooms]

    @property
    def openings(self) -> List[Opening]:
        return [o for l in self.levels for o in l.openings]

    @property
    def base(self) -> Optional[Level]:
        return min(self.levels, key=lambda l: l.index) if self.levels else None

    @property
    def floor_area(self) -> float:
        return sum(l.floor_area for l in self.levels)

    @property
    def footprint_area(self) -> float:
        """Plan area of the largest storey — the ground the building covers."""
        return max((l.footprint_area for l in self.levels), default=0.0)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "designation": self.designation,
            "levels": len(self.levels),
            "walls": len(self.walls),
            "rooms": len(self.rooms),
            "openings": len(self.openings),
            "footprint_m2": round(self.footprint_area, 2),
            "floor_area_m2": round(self.floor_area, 2),
        }

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "designation": self.designation,
            "evidence": list(self.evidence),
            "confidence": round(self.confidence, 3),
            "warnings": list(self.warnings),
            "validation": dict(self.validation),
            "levels": [l.as_dict() for l in self.levels],
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# Drawing — the root
# ---------------------------------------------------------------------------

@dataclass
class Drawing:
    """The validated architectural model of one DXF.

    This is the contract between reconstruction and 3D generation. The 3D stage
    extrudes exactly this and guesses nothing.
    """

    source_path: str = ""
    schema_version: str = SCHEMA_VERSION
    units: Optional[UnitDecision] = None
    buildings: List[Building] = field(default_factory=list)
    #: How the drawing's plans were organised into buildings and storeys, and
    #: how sure the engine is: ``{"status": SINGLE_LEVEL | RESOLVED |
    #: AMBIGUOUS_LEVEL_STRUCTURE, "reason": ..., ...}``.
    level_structure: Dict[str, object] = field(default_factory=dict)
    #: Things a person should look at before trusting the model, each
    #: ``{"code", "message", ...}``. Never used for geometry errors — those
    #: refuse the build outright.
    review: List[Dict[str, object]] = field(default_factory=list)
    #: Wall geometry that belongs to no building (site walls, fences, debris),
    #: reported rather than silently extruded or silently dropped.
    unassigned: List[Dict[str, object]] = field(default_factory=list)
    #: Line work that lay outside the plan frame and so took no part in the
    #: reconstruction - most often the same plan copied at another scale.
    #: ``None`` when the sheet holds a single coherent drawing. See
    #: ``read.survey_outlying``.
    outlying: Optional[Dict[str, object]] = None
    # world = local + origin_offset, then / scale_to_m back to drawing units
    origin_offset: XY = (0.0, 0.0)
    bounds_min: XY = (0.0, 0.0)
    bounds_max: XY = (0.0, 0.0)
    north_deg: float = 0.0
    default_wall_height: float = 2.7
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)
    validation: Dict[str, object] = field(default_factory=dict)
    timings: Dict[str, float] = field(default_factory=dict)
    #: The CAD evidence bridge — see :mod:`modules.recon.evidence`. Written
    #: alongside the model so downstream stages never re-read the DXF.
    source_evidence: Dict[str, object] = field(default_factory=dict)
    #: The semantic tier's view of the drawing (a serialised
    #: ``cad.schema.CadDocument``), projected from the same reader. Carried in
    #: ``geometry.json`` for the 2D stages; not written into ``building.json``,
    #: which the 3D stage reads and which has no use for it.
    semantic_document: Dict[str, object] = field(default_factory=dict, repr=False)

    # -- traversal ----------------------------------------------------------

    def levels(self) -> Iterator[Level]:
        for b in self.buildings:
            for l in b.levels:
                yield l

    def building(self, building_id: str) -> Optional[Building]:
        return next((b for b in self.buildings if b.id == building_id), None)

    def level(self, level_id: str) -> Optional[Level]:
        return next((l for l in self.levels() if l.id == level_id), None)

    def level_of(self, element_id: str) -> Optional[Level]:
        """The level that owns a wall, room, opening or node id."""
        for l in self.levels():
            if any(w.id == element_id for w in l.walls) or \
                    any(r.id == element_id for r in l.rooms) or \
                    any(o.id == element_id for o in l.openings) or \
                    any(n.id == element_id for n in l.nodes):
                return l
        return None

    @property
    def walls(self) -> List[Wall]:
        return [w for l in self.levels() for w in l.walls]

    @property
    def rooms(self) -> List[Room]:
        return [r for l in self.levels() for r in l.rooms]

    @property
    def openings(self) -> List[Opening]:
        return [o for l in self.levels() for o in l.openings]

    def wall(self, wall_id: str) -> Optional[Wall]:
        return next((w for w in self.walls if w.id == wall_id), None)

    def room(self, room_id: str) -> Optional[Room]:
        return next((r for r in self.rooms if r.id == room_id), None)

    def opening(self, opening_id: str) -> Optional[Opening]:
        return next((o for o in self.openings if o.id == opening_id), None)

    @property
    def status(self) -> str:
        return str(self.validation.get("status", ""))

    # -- serialisation ------------------------------------------------------

    def summary(self) -> dict:
        levels = list(self.levels())
        openings = self.openings
        return {
            "buildings": len(self.buildings),
            "levels": len(levels),
            "walls": len(self.walls),
            "rooms": len(self.rooms),
            "openings": len(openings),
            "doors": sum(1 for o in openings if o.kind in ("door", "garage")),
            "windows": sum(1 for o in openings if o.kind == "window"),
            "openings_by_class": _by_class(openings),
            "footprint_m2": round(sum(b.footprint_area for b in self.buildings), 2),
            "floor_area_m2": round(sum(l.floor_area for l in levels), 2),
            "total_wall_length_m": round(sum(l.total_wall_length for l in levels), 2),
            "level_structure": self.level_structure.get("status"),
            "status": self.validation.get("status"),
            "units": self.units.unit_name if self.units else None,
            "scale_to_m": self.units.scale_to_m if self.units else None,
            "per_building": [b.summary() for b in self.buildings],
        }

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source_path": self.source_path,
            "units": self.units.as_dict() if self.units else None,
            "origin_offset": list(self.origin_offset),
            "bounds_min": list(self.bounds_min),
            "bounds_max": list(self.bounds_max),
            "north_deg": self.north_deg,
            "default_wall_height": self.default_wall_height,
            "level_structure": dict(self.level_structure),
            "review": list(self.review),
            "unassigned": list(self.unassigned),
            "buildings": [b.as_dict() for b in self.buildings],
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "validation": dict(self.validation),
            "timings": {k: round(v, 4) for k, v in self.timings.items()},
            "source_evidence": dict(self.source_evidence),
            "summary": self.summary(),
        }

    def to_json(self, path: str, indent: int = 2) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.as_dict(), fh, indent=indent)

    # -- coordinate transforms ---------------------------------------------

    def to_world(self, point: XY) -> XY:
        """Local metres -> original DXF drawing units, for tracing a diagnostic."""
        scale = self.units.scale_to_m if self.units else 1.0
        return ((point[0] + self.origin_offset[0]) / scale,
                (point[1] + self.origin_offset[1]) / scale)


# ---------------------------------------------------------------------------
# Deserialisation — used by tools that must not re-run reconstruction.
# ---------------------------------------------------------------------------

def _xy(v) -> XY:
    return (float(v[0]), float(v[1]))


def _units_from(u: Optional[dict]) -> Optional[UnitDecision]:
    if not u:
        return None
    u = dict(u)
    return UnitDecision(
        scale_to_m=float(u["scale_to_m"]),
        unit_name=u.get("unit_name", "unknown"),
        method=u.get("method", "unknown"),
        confidence=float(u.get("confidence", 0.0)),
        reason=u.get("reason", ""),
        insunits=u.get("insunits"),
        conflict=u.get("conflict"),
        candidates=u.get("candidates", []),
    )


def level_from_dict(data: dict, units: Optional[UnitDecision] = None) -> Level:
    """Rebuild a :class:`Level` from ``as_dict`` output.

    Tolerant of missing optional keys so an IR written by an older build still
    loads; anything structural that is missing raises, because silently
    defaulting a wall's thickness is how the old engine produced
    plausible-looking wrong buildings.
    """
    walls = [
        Wall(
            id=w["id"], start=_xy(w["start"]), end=_xy(w["end"]),
            thickness=float(w["thickness"]), kind=w.get("kind", "interior"),
            height=w.get("height"),
            node_ids=tuple(w.get("node_ids", [None, None])),
            opening_ids=list(w.get("opening_ids", [])),
            source_ids=list(w.get("source_ids", [])),
            layer=w.get("layer", ""), confidence=float(w.get("confidence", 1.0)),
            level_id=w.get("level_id", ""),
        )
        for w in data.get("walls", [])
    ]
    rooms = [
        Room(
            id=r["id"], polygon=[_xy(p) for p in r["polygon"]],
            area=float(r.get("area", 0.0)), centroid=_xy(r.get("centroid", (0, 0))),
            holes=[[_xy(p) for p in h] for h in r.get("holes", [])],
            label=r.get("label"), label_confidence=float(r.get("label_confidence", 0.0)),
            room_type=r.get("room_type", "unknown"),
            boundary_wall_ids=list(r.get("boundary_wall_ids", [])),
            opening_ids=list(r.get("opening_ids", [])),
            neighbour_ids=list(r.get("neighbour_ids", [])),
            is_exterior=bool(r.get("is_exterior", False)),
            open_plan=bool(r.get("open_plan", False)),
            source_ids=list(r.get("source_ids", [])),
            level_id=r.get("level_id", ""),
        )
        for r in data.get("rooms", [])
    ]
    openings = [
        Opening(
            id=o["id"], kind=o.get("kind", "door"), wall_id=o["wall_id"],
            position=_xy(o["position"]), offset=float(o.get("offset", 0.0)),
            width=float(o["width"]), height=float(o.get("height", 2.1)),
            sill_height=float(o.get("sill_height", 0.0)),
            thickness=float(o.get("thickness", 0.1)),
            swing=o.get("swing"), rooms=list(o.get("rooms", [])),
            source_ids=list(o.get("source_ids", [])),
            evidence=o.get("evidence", ""), confidence=float(o.get("confidence", 0.5)),
            level_id=o.get("level_id", ""),
            classification=o.get("classification", "unknown_opening"),
            gap_confirmed=o.get("gap_confirmed"),
        )
        for o in data.get("openings", [])
    ]
    nodes = [
        Node(id=n["id"], point=_xy(n["point"]), wall_ids=list(n.get("wall_ids", [])))
        for n in data.get("nodes", [])
    ]
    return Level(
        id=data.get("id", "l0"), name=data.get("name", "Level 0"),
        index=int(data.get("index", 0)),
        elevation=float(data.get("elevation", 0.0)),
        height=float(data.get("height", data.get("default_wall_height", 2.7))),
        placement=_xy(data.get("placement", (0.0, 0.0))),
        title=data.get("title"), designation=data.get("designation", "assumed"),
        evidence=list(data.get("evidence", [])),
        confidence=float(data.get("confidence", 1.0)),
        building_id=data.get("building_id", ""),
        walls=walls, rooms=rooms, openings=openings, nodes=nodes,
        footprint=[_xy(p) for p in data.get("footprint", [])],
        footprint_parts=[[_xy(p) for p in r] for r in data.get("footprint_parts", [])],
        footprint_holes=[[_xy(p) for p in h] for h in data.get("footprint_holes", [])],
        bounds_min=_xy(data.get("bounds_min", (0.0, 0.0))),
        bounds_max=_xy(data.get("bounds_max", (0.0, 0.0))),
        units=units,
        source_path=data.get("source_path", ""),
        warnings=list(data.get("warnings", [])),
        stats=dict(data.get("stats", {})),
        validation=dict(data.get("validation", {})),
    )


def drawing_from_dict(data: dict) -> Drawing:
    """Rebuild a :class:`Drawing` from ``as_dict`` output.

    A 2.0 document — one flat building — loads as a drawing with one building
    of one level, which is exactly what that format could express.
    """
    units = _units_from(data.get("units"))
    if "buildings" not in data and "walls" in data:
        level = level_from_dict(data, units)
        level.id, level.building_id = "b1.l0", "b1"
        level.designation = "assumed"
        return Drawing(
            source_path=data.get("source_path", ""),
            schema_version=data.get("schema_version", "archx3d.ir/2.0"),
            units=units,
            buildings=[Building(id="b1", name="Building 1", levels=[level],
                                evidence=["legacy single-building document"])],
            level_structure={"status": LEVELS_SINGLE,
                             "reason": "legacy single-building document"},
            origin_offset=_xy(data.get("origin_offset", (0.0, 0.0))),
            bounds_min=_xy(data.get("bounds_min", (0.0, 0.0))),
            bounds_max=_xy(data.get("bounds_max", (0.0, 0.0))),
            north_deg=float(data.get("north_deg", 0.0)),
            default_wall_height=float(data.get("default_wall_height", 2.7)),
            warnings=list(data.get("warnings", [])),
            stats=dict(data.get("stats", {})),
            validation=dict(data.get("validation", {})),
            timings=dict(data.get("timings", {})),
        )

    buildings = []
    for b in data.get("buildings", []):
        buildings.append(Building(
            id=b.get("id", "b1"), name=b.get("name", "Building"),
            levels=[level_from_dict(l, units) for l in b.get("levels", [])],
            designation=b.get("designation"),
            evidence=list(b.get("evidence", [])),
            confidence=float(b.get("confidence", 1.0)),
            warnings=list(b.get("warnings", [])),
            validation=dict(b.get("validation", {})),
        ))
    return Drawing(
        source_path=data.get("source_path", ""),
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        units=units, buildings=buildings,
        level_structure=dict(data.get("level_structure", {})),
        review=list(data.get("review", [])),
        unassigned=list(data.get("unassigned", [])),
        origin_offset=_xy(data.get("origin_offset", (0.0, 0.0))),
        bounds_min=_xy(data.get("bounds_min", (0.0, 0.0))),
        bounds_max=_xy(data.get("bounds_max", (0.0, 0.0))),
        north_deg=float(data.get("north_deg", 0.0)),
        default_wall_height=float(data.get("default_wall_height", 2.7)),
        warnings=list(data.get("warnings", [])),
        stats=dict(data.get("stats", {})),
        validation=dict(data.get("validation", {})),
        timings=dict(data.get("timings", {})),
        source_evidence=dict(data.get("source_evidence", {})),
    )


def load_drawing(path: str) -> Drawing:
    with open(path, "r", encoding="utf-8") as fh:
        return drawing_from_dict(json.load(fh))
