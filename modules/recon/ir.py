"""
ArchX3D — Architectural Intermediate Representation
===================================================
The validated 2D building model that sits between the DXF and Blender.

Why an IR at all
----------------
The engine this replaces had no building model. ``geometry.json`` carried a
flat list of ``{start, end, layer}`` line segments, and every consumer
re-derived meaning from it: Blender guessed walls by extruding each segment,
the furnisher guessed rooms, the viewer guessed a footprint. Nothing could be
validated because there was nothing to validate — a list of segments is always
"valid", including when it is a picture of a dimension string.

Everything in this module is expressed in **metres**, in a **normalised local
frame** whose origin is the minimum corner of the building envelope. The
transform back to DXF world coordinates is retained on :class:`Building` so a
diagnostic can always be traced to the source drawing, and so nothing depends
on CAD world coordinates that may be in the hundreds of thousands.

The invariant every stage downstream may rely on: if you were handed a
:class:`Building`, it passed :mod:`modules.recon.validate`. A reconstruction
that cannot satisfy the validator is never returned as a Building — it is
raised as a :class:`ReconstructionError` carrying the diagnostics that explain
why. "No building" is a supported outcome; "a wrong building" is not.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "archx3d.ir/2.0"

XY = Tuple[float, float]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ReconstructionError(RuntimeError):
    """Raised when the drawing cannot be reconstructed into a valid building.

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


@dataclass
class Level:
    """One storey. Multi-storey sheets are split into levels rather than merged.

    A drawing that contains a ground-floor plan and a first-floor plan side by
    side is the classic way to produce a building with two overlapping halves.
    Keeping levels explicit means the engine can either build one of them or
    stack them, but never accidentally union them.
    """

    id: str
    name: str
    elevation: float = 0.0
    height: float = 2.7
    wall_ids: List[str] = field(default_factory=list)
    room_ids: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# The building
# ---------------------------------------------------------------------------

@dataclass
class Building:
    """The validated 2D architectural model.

    This is the contract between reconstruction and 3D generation. The 3D stage
    extrudes exactly this and guesses nothing.
    """

    source_path: str = ""
    schema_version: str = SCHEMA_VERSION
    units: Optional[UnitDecision] = None
    walls: List[Wall] = field(default_factory=list)
    rooms: List[Room] = field(default_factory=list)
    openings: List[Opening] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)
    levels: List[Level] = field(default_factory=list)
    footprint: List[XY] = field(default_factory=list)
    footprint_holes: List[List[XY]] = field(default_factory=list)
    #: Every outer ring of the envelope, largest first. A sheet may carry a
    #: house *and* its detached garage, or a pair of semi-detached units;
    #: keeping only the largest ring in ``footprint`` made the second one an
    #: area of rooms that lay outside the building.
    footprint_parts: List[List[XY]] = field(default_factory=list)
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

    @staticmethod
    def _ring_area(pts: Sequence[XY]) -> float:
        if len(pts) < 3:
            return 0.0
        a = 0.0
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            a += x1 * y2 - x2 * y1
        return abs(a) / 2.0

    @property
    def footprint_area(self) -> float:
        if len(self.footprint) < 3:
            return 0.0
        parts = self.footprint_parts or [self.footprint]
        gross = sum(self._ring_area(r) for r in parts)
        for hole in self.footprint_holes:
            h = 0.0
            for i in range(len(hole)):
                x1, y1 = hole[i]
                x2, y2 = hole[(i + 1) % len(hole)]
                h += x1 * y2 - x2 * y1
            gross -= abs(h) / 2.0
        return gross

    def thickness_profile(self) -> Dict[str, float]:
        """Modal wall thickness per wall kind, for reporting and validation."""
        out: Dict[str, List[float]] = {}
        for w in self.walls:
            out.setdefault(w.kind, []).append(w.thickness)
        return {k: round(sorted(v)[len(v) // 2], 4) for k, v in out.items() if v}

    # -- serialisation ------------------------------------------------------

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
            "footprint": [list(p) for p in self.footprint],
            "footprint_holes": [[list(p) for p in h] for h in self.footprint_holes],
            "footprint_parts": [[list(p) for p in r] for r in self.footprint_parts],
            "walls": [w.as_dict() for w in self.walls],
            "rooms": [r.as_dict() for r in self.rooms],
            "openings": [o.as_dict() for o in self.openings],
            "nodes": [n.as_dict() for n in self.nodes],
            "levels": [l.as_dict() for l in self.levels],
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "validation": dict(self.validation),
            "timings": {k: round(v, 4) for k, v in self.timings.items()},
            "summary": self.summary(),
        }

    def summary(self) -> dict:
        return {
            "walls": len(self.walls),
            "rooms": len(self.rooms),
            "openings": len(self.openings),
            "doors": sum(1 for o in self.openings if o.kind in ("door", "garage")),
            "windows": sum(1 for o in self.openings if o.kind == "window"),
            "nodes": len(self.nodes),
            "footprint_m2": round(self.footprint_area, 2),
            "floor_area_m2": round(self.floor_area, 2),
            "total_wall_length_m": round(self.total_wall_length, 2),
            "extent_m": [round(self.width, 3), round(self.depth, 3)],
            "thickness_profile": self.thickness_profile(),
            "units": self.units.unit_name if self.units else None,
            "scale_to_m": self.units.scale_to_m if self.units else None,
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
# Deserialisation — used by the Blender child process, which cannot import
# shapely and must not re-run reconstruction.
# ---------------------------------------------------------------------------

def building_from_dict(data: dict) -> Building:
    """Rebuild a :class:`Building` from ``as_dict`` output.

    Kept deliberately tolerant of missing optional keys so an IR written by an
    older build still loads; anything structural that is missing raises, because
    silently defaulting a wall's thickness is how the old engine produced
    plausible-looking wrong buildings.
    """
    def _xy(v) -> XY:
        return (float(v[0]), float(v[1]))

    units = None
    if data.get("units"):
        u = dict(data["units"])
        units = UnitDecision(
            scale_to_m=float(u["scale_to_m"]),
            unit_name=u.get("unit_name", "unknown"),
            method=u.get("method", "unknown"),
            confidence=float(u.get("confidence", 0.0)),
            reason=u.get("reason", ""),
            insunits=u.get("insunits"),
            conflict=u.get("conflict"),
            candidates=u.get("candidates", []),
        )

    walls = [
        Wall(
            id=w["id"], start=_xy(w["start"]), end=_xy(w["end"]),
            thickness=float(w["thickness"]), kind=w.get("kind", "interior"),
            height=w.get("height"),
            node_ids=tuple(w.get("node_ids", [None, None])),
            opening_ids=list(w.get("opening_ids", [])),
            source_ids=list(w.get("source_ids", [])),
            layer=w.get("layer", ""), confidence=float(w.get("confidence", 1.0)),
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
        )
        for o in data.get("openings", [])
    ]
    nodes = [
        Node(id=n["id"], point=_xy(n["point"]), wall_ids=list(n.get("wall_ids", [])))
        for n in data.get("nodes", [])
    ]
    levels = [
        Level(id=l["id"], name=l.get("name", l["id"]),
              elevation=float(l.get("elevation", 0.0)),
              height=float(l.get("height", 2.7)),
              wall_ids=list(l.get("wall_ids", [])),
              room_ids=list(l.get("room_ids", [])))
        for l in data.get("levels", [])
    ]

    return Building(
        source_path=data.get("source_path", ""),
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        units=units, walls=walls, rooms=rooms, openings=openings, nodes=nodes,
        levels=levels,
        footprint=[_xy(p) for p in data.get("footprint", [])],
        footprint_parts=[[_xy(p) for p in r] for r in data.get("footprint_parts", [])],
        footprint_holes=[[_xy(p) for p in h] for h in data.get("footprint_holes", [])],
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


def load_building(path: str) -> Building:
    with open(path, "r", encoding="utf-8") as fh:
        return building_from_dict(json.load(fh))
