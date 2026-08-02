"""
ArchX3D — CAD Document Schema
=============================
The typed representation of *everything* a DXF file contains, produced by
``cad.reader`` and consumed by ``semantic`` and the vision pipeline.

Why this exists
---------------
``dxf_extractor`` reduces a drawing to a list of wall segments. That discards
the highest-confidence information in the file: the room labels the architect
typed, the furniture blocks they placed, and the layer names they filed
everything under. Room classification was therefore left with nothing but
image understanding — priority 6 in the design philosophy — while priorities
1 through 5 sat unread in the file.

This module is the container for those priorities. Nothing here interprets
anything; it is a faithful, typed record of what the file says. Interpretation
happens in ``cad.layers`` / ``cad.blocks`` / ``cad.text`` and, at the building
level, in ``modules/semantic``.

Design constraints
------------------
* **Stdlib only.** No numpy, no ezdxf. ``reader`` owns the ezdxf dependency so
  that a scene graph can be round-tripped without CAD libraries installed.
* **Everything is identified.** Every record carries a stable ``uid``, so a
  finding, a correction or a user override can name an entity unambiguously.
* **Everything carries provenance.** ``source`` records *which* tier of the
  design philosophy produced a claim, so a downstream consumer can prefer a
  block name over a geometric guess without re-deriving why.
* **Metric.** All coordinates are metres, in the same frame ``geometry.json``
  uses (origin-normalised, +Z up), so CAD entities and scene-graph objects can
  be compared without a transform.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1.0"

Point = Tuple[float, float]


# ---------------------------------------------------------------------------
# Provenance tiers
# ---------------------------------------------------------------------------


class Source:
    """Where a claim came from, ordered by the project's trust hierarchy.

    The ordering is the design philosophy made executable: "never guess if
    reliable information exists" is enforced by comparing ``Source.tier`` of
    competing claims rather than by each consumer re-inventing a preference.

    Lower tier number == more trustworthy.
    """

    CAD_METADATA = "cad_metadata"
    CAD_BLOCK = "cad_block"
    CAD_LAYER = "cad_layer"
    CAD_TEXT = "cad_text"
    CAD_GEOMETRY = "cad_geometry"
    IMAGE = "image"
    REASONING = "reasoning"
    USER = "user"

    _TIERS: Dict[str, int] = {
        CAD_METADATA: 1,
        CAD_BLOCK: 2,
        CAD_LAYER: 3,
        CAD_TEXT: 4,
        CAD_GEOMETRY: 5,
        IMAGE: 6,
        REASONING: 7,
        USER: 0,  # An explicit human decision outranks everything.
    }

    @classmethod
    def tier(cls, source: str) -> int:
        """Trust rank of ``source``; unknown sources sort last."""
        return cls._TIERS.get(source, 99)

    @classmethod
    def more_trusted(cls, a: str, b: str) -> bool:
        return cls.tier(a) < cls.tier(b)

    @classmethod
    def all(cls) -> Tuple[str, ...]:
        return tuple(sorted(cls._TIERS, key=cls._TIERS.get))


def make_uid(kind: str, *parts: Any) -> str:
    """A stable id for an entity, derived from its identifying content.

    Content-derived rather than sequential so that re-running extraction on an
    unchanged file yields unchanged ids. That is what lets a user's correction
    ("room_3 is a utility room, not a bathroom") survive a re-parse.
    """
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"{kind}_{digest[:10]}"


# ---------------------------------------------------------------------------
# Base record
# ---------------------------------------------------------------------------


@dataclass
class CadRecord:
    """Common fields every extracted entity carries."""

    uid: str = ""
    #: The DXF entity type this came from, e.g. ``"LWPOLYLINE"``.
    dxftype: str = ""
    #: Layer the entity was drawn on, verbatim.
    layer: str = ""
    #: Which trust tier produced this record's *interpretation*.
    source: str = Source.CAD_GEOMETRY
    #: How sure we are of the interpretation, not of the geometry.
    confidence: float = 1.0
    #: Human-readable justification, surfaced in diagnostics and review UIs.
    reason: str = ""

    def _base_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "dxftype": self.dxftype,
            "layer": self.layer,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass
class CadSegment(CadRecord):
    """A straight line segment, the atom all curve geometry decomposes to."""

    start: Point = (0.0, 0.0)
    end: Point = (0.0, 0.0)
    #: Semantic role assigned by ``cad.layers``: wall | door | window | ...
    role: str = "unknown"
    #: True when the segment came from tessellating an ARC or CIRCLE.
    tessellated: bool = False

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def midpoint(self) -> Point:
        return ((self.start[0] + self.end[0]) / 2.0, (self.start[1] + self.end[1]) / 2.0)

    @property
    def angle_deg(self) -> float:
        return math.degrees(
            math.atan2(self.end[1] - self.start[1], self.end[0] - self.start[0])
        )

    def to_dict(self) -> Dict[str, Any]:
        out = self._base_dict()
        out.update({
            "start": [round(self.start[0], 6), round(self.start[1], 6)],
            "end": [round(self.end[0], 6), round(self.end[1], 6)],
            "role": self.role,
            "tessellated": self.tessellated,
        })
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CadSegment":
        start = d.get("start") or [0.0, 0.0]
        end = d.get("end") or [0.0, 0.0]
        return CadSegment(
            uid=str(d.get("uid", "")),
            dxftype=str(d.get("dxftype", "")),
            layer=str(d.get("layer", "")),
            source=str(d.get("source", Source.CAD_GEOMETRY)),
            confidence=_f(d.get("confidence"), 1.0),
            reason=str(d.get("reason", "")),
            start=(_f(start[0]), _f(start[1])),
            end=(_f(end[0]), _f(end[1])),
            role=str(d.get("role", "unknown")),
            tessellated=bool(d.get("tessellated", False)),
        )

    def as_wall_dict(self) -> Dict[str, Any]:
        """The shape ``dxf_extractor`` has always emitted.

        Kept so ``geometry.json`` stays byte-compatible for every existing
        consumer (room segmentation, the Blender generator, the web viewer)
        while the richer record travels alongside it.
        """
        return {
            "start": [round(self.start[0], 6), round(self.start[1], 6)],
            "end": [round(self.end[0], 6), round(self.end[1], 6)],
            "source_entity": self.dxftype,
            "layer": self.layer,
        }


@dataclass
class CadPolyline(CadRecord):
    """A retained polyline, kept whole because its closure is meaningful.

    Segment decomposition loses the fact that a polyline was *closed*, and a
    closed polyline on a room-boundary layer is one of the strongest room
    delineations a drawing can carry — far better than flood-filling walls.
    """

    points: List[Point] = field(default_factory=list)
    closed: bool = False
    role: str = "unknown"

    @property
    def area(self) -> float:
        """Absolute shoelace area; 0 for open or degenerate polylines."""
        if not self.closed or len(self.points) < 3:
            return 0.0
        total = 0.0
        for i, (x0, y0) in enumerate(self.points):
            x1, y1 = self.points[(i + 1) % len(self.points)]
            total += x0 * y1 - x1 * y0
        return abs(total) / 2.0

    @property
    def centroid(self) -> Point:
        if not self.points:
            return (0.0, 0.0)
        return (
            sum(p[0] for p in self.points) / len(self.points),
            sum(p[1] for p in self.points) / len(self.points),
        )

    def to_dict(self) -> Dict[str, Any]:
        out = self._base_dict()
        out.update({
            "points": [[round(p[0], 6), round(p[1], 6)] for p in self.points],
            "closed": self.closed,
            "role": self.role,
            "area": round(self.area, 4),
        })
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CadPolyline":
        return CadPolyline(
            uid=str(d.get("uid", "")),
            dxftype=str(d.get("dxftype", "")),
            layer=str(d.get("layer", "")),
            source=str(d.get("source", Source.CAD_GEOMETRY)),
            confidence=_f(d.get("confidence"), 1.0),
            reason=str(d.get("reason", "")),
            points=[(_f(p[0]), _f(p[1])) for p in (d.get("points") or [])],
            closed=bool(d.get("closed", False)),
            role=str(d.get("role", "unknown")),
        )


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------


@dataclass
class CadText:
    """A TEXT / MTEXT / ATTRIB string with its plan position.

    Position matters as much as content: "KITCHEN" is only evidence about the
    room whose polygon contains it. ``insert`` is the text's anchor point in
    plan metres.
    """

    uid: str = ""
    text: str = ""
    #: Uppercased, punctuation-normalised form used for matching.
    normalised: str = ""
    insert: Point = (0.0, 0.0)
    height: float = 0.0
    rotation: float = 0.0
    layer: str = ""
    dxftype: str = "TEXT"
    #: Set when the string came from a block attribute; names the tag.
    attrib_tag: str = ""
    #: Block reference this text belongs to, when it is an ATTRIB.
    owner_block: str = ""
    #: What ``cad.text`` made of it: room_label | dimension | area | note | ...
    role: str = "note"
    #: Room type parsed out of the string, when ``role == "room_label"``.
    room_type: str = ""
    #: Numeric value parsed out, e.g. an area in m² or a dimension in metres.
    value: Optional[float] = None
    confidence: float = 0.0
    source: str = Source.CAD_TEXT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "text": self.text,
            "normalised": self.normalised,
            "insert": [round(self.insert[0], 6), round(self.insert[1], 6)],
            "height": round(self.height, 4),
            "rotation": round(self.rotation, 2),
            "layer": self.layer,
            "dxftype": self.dxftype,
            "attrib_tag": self.attrib_tag,
            "owner_block": self.owner_block,
            "role": self.role,
            "room_type": self.room_type,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "source": self.source,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CadText":
        insert = d.get("insert") or [0.0, 0.0]
        return CadText(
            uid=str(d.get("uid", "")),
            text=str(d.get("text", "")),
            normalised=str(d.get("normalised", "")),
            insert=(_f(insert[0]), _f(insert[1])),
            height=_f(d.get("height")),
            rotation=_f(d.get("rotation")),
            layer=str(d.get("layer", "")),
            dxftype=str(d.get("dxftype", "TEXT")),
            attrib_tag=str(d.get("attrib_tag", "")),
            owner_block=str(d.get("owner_block", "")),
            role=str(d.get("role", "note")),
            room_type=str(d.get("room_type", "")),
            value=d.get("value") if d.get("value") is None else _f(d.get("value")),
            confidence=_f(d.get("confidence")),
            source=str(d.get("source", Source.CAD_TEXT)),
        )


@dataclass
class CadDimension:
    """A dimension annotation — the drawing stating its own measurements.

    Dimensions are the only place a DXF *asserts* a length rather than
    implying one through coordinates, which makes them the arbiter when the
    drawing's units are ambiguous: if a wall measures 4200 units and its
    dimension text reads "4200", the file is in millimetres.
    """

    uid: str = ""
    #: The dimension's own text, verbatim ("" means "use the measurement").
    text: str = ""
    #: Measured value in drawing units, before unit conversion.
    measurement: float = 0.0
    #: Value in metres after unit conversion.
    metres: float = 0.0
    #: Midpoint of the dimension line, in plan metres.
    position: Point = (0.0, 0.0)
    layer: str = ""
    dxftype: str = "DIMENSION"
    #: linear | aligned | angular | radial | diameter | ordinate
    kind: str = "linear"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "text": self.text,
            "measurement": round(self.measurement, 4),
            "metres": round(self.metres, 4),
            "position": [round(self.position[0], 6), round(self.position[1], 6)],
            "layer": self.layer,
            "dxftype": self.dxftype,
            "kind": self.kind,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CadDimension":
        pos = d.get("position") or [0.0, 0.0]
        return CadDimension(
            uid=str(d.get("uid", "")),
            text=str(d.get("text", "")),
            measurement=_f(d.get("measurement")),
            metres=_f(d.get("metres")),
            position=(_f(pos[0]), _f(pos[1])),
            layer=str(d.get("layer", "")),
            dxftype=str(d.get("dxftype", "DIMENSION")),
            kind=str(d.get("kind", "linear")),
        )


@dataclass
class CadHatch:
    """A filled region. Pattern name often encodes material or room use."""

    uid: str = ""
    pattern: str = ""
    layer: str = ""
    #: Outer boundary in plan metres, when one could be recovered.
    boundary: List[Point] = field(default_factory=list)
    area: float = 0.0
    centroid: Point = (0.0, 0.0)
    solid: bool = False
    #: Material guessed from the pattern name, e.g. "concrete", "tile".
    material: str = ""
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "pattern": self.pattern,
            "layer": self.layer,
            "boundary": [[round(p[0], 6), round(p[1], 6)] for p in self.boundary],
            "area": round(self.area, 4),
            "centroid": [round(self.centroid[0], 6), round(self.centroid[1], 6)],
            "solid": self.solid,
            "material": self.material,
            "confidence": round(self.confidence, 3),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CadHatch":
        centroid = d.get("centroid") or [0.0, 0.0]
        return CadHatch(
            uid=str(d.get("uid", "")),
            pattern=str(d.get("pattern", "")),
            layer=str(d.get("layer", "")),
            boundary=[(_f(p[0]), _f(p[1])) for p in (d.get("boundary") or [])],
            area=_f(d.get("area")),
            centroid=(_f(centroid[0]), _f(centroid[1])),
            solid=bool(d.get("solid", False)),
            material=str(d.get("material", "")),
            confidence=_f(d.get("confidence")),
        )


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


@dataclass
class CadBlockRef:
    """An INSERT — a placed instance of a block definition.

    Blocks are trust tier 2 because an architect who placed a block named
    ``BED-QUEEN`` has *stated* that a queen bed is there. That is a far
    stronger claim than any inference from geometry, and stronger than a text
    label, which may be stale after a plan revision while the block is what
    actually got drawn.
    """

    uid: str = ""
    #: Block definition name, verbatim.
    name: str = ""
    #: Uppercased, separator-normalised form used for matching.
    normalised: str = ""
    position: Point = (0.0, 0.0)
    rotation: float = 0.0
    scale: Tuple[float, float] = (1.0, 1.0)
    layer: str = ""
    #: ATTRIB tag -> value, e.g. ``{"ROOM_NAME": "MASTER BEDROOM"}``.
    attributes: Dict[str, str] = field(default_factory=dict)
    #: Axis-aligned footprint of the block's own geometry, in plan metres.
    bounds_min: Point = (0.0, 0.0)
    bounds_max: Point = (0.0, 0.0)

    #: Category resolved by ``cad.blocks``, in ``catalog.OBJECT_CATALOG`` terms.
    category: str = ""
    #: furniture | fixture | door | window | column | appliance | annotation
    kind: str = "unknown"
    confidence: float = 0.0
    reason: str = ""
    source: str = Source.CAD_BLOCK

    @property
    def width(self) -> float:
        return self.bounds_max[0] - self.bounds_min[0]

    @property
    def depth(self) -> float:
        return self.bounds_max[1] - self.bounds_min[1]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "normalised": self.normalised,
            "position": [round(self.position[0], 6), round(self.position[1], 6)],
            "rotation": round(self.rotation, 2),
            "scale": [round(self.scale[0], 4), round(self.scale[1], 4)],
            "layer": self.layer,
            "attributes": dict(self.attributes),
            "bounds_min": [round(self.bounds_min[0], 6), round(self.bounds_min[1], 6)],
            "bounds_max": [round(self.bounds_max[0], 6), round(self.bounds_max[1], 6)],
            "category": self.category,
            "kind": self.kind,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "source": self.source,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CadBlockRef":
        pos = d.get("position") or [0.0, 0.0]
        scale = d.get("scale") or [1.0, 1.0]
        bmin = d.get("bounds_min") or [0.0, 0.0]
        bmax = d.get("bounds_max") or [0.0, 0.0]
        return CadBlockRef(
            uid=str(d.get("uid", "")),
            name=str(d.get("name", "")),
            normalised=str(d.get("normalised", "")),
            position=(_f(pos[0]), _f(pos[1])),
            rotation=_f(d.get("rotation")),
            scale=(_f(scale[0], 1.0), _f(scale[1], 1.0)),
            layer=str(d.get("layer", "")),
            attributes={str(k): str(v) for k, v in (d.get("attributes") or {}).items()},
            bounds_min=(_f(bmin[0]), _f(bmin[1])),
            bounds_max=(_f(bmax[0]), _f(bmax[1])),
            category=str(d.get("category", "")),
            kind=str(d.get("kind", "unknown")),
            confidence=_f(d.get("confidence")),
            reason=str(d.get("reason", "")),
            source=str(d.get("source", Source.CAD_BLOCK)),
        )


@dataclass
class CadLayer:
    """A layer table entry plus the semantic role inferred from its name."""

    name: str = ""
    #: ACI colour index; 256 = ByLayer, 7 = white/black default.
    color: int = 7
    linetype: str = "CONTINUOUS"
    frozen: bool = False
    off: bool = False
    locked: bool = False
    #: How many modelspace entities were drawn on it.
    entity_count: int = 0
    #: wall | door | window | column | furniture | text | dimension | ...
    role: str = "unknown"
    confidence: float = 0.0
    reason: str = ""
    #: Naming standard recognised: aia | iso13567 | generic | none
    convention: str = "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "color": self.color,
            "linetype": self.linetype,
            "frozen": self.frozen,
            "off": self.off,
            "locked": self.locked,
            "entity_count": self.entity_count,
            "role": self.role,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "convention": self.convention,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CadLayer":
        return CadLayer(
            name=str(d.get("name", "")),
            color=int(d.get("color", 7) or 7),
            linetype=str(d.get("linetype", "CONTINUOUS")),
            frozen=bool(d.get("frozen", False)),
            off=bool(d.get("off", False)),
            locked=bool(d.get("locked", False)),
            entity_count=int(d.get("entity_count", 0) or 0),
            role=str(d.get("role", "unknown")),
            confidence=_f(d.get("confidence")),
            reason=str(d.get("reason", "")),
            convention=str(d.get("convention", "none")),
        )


# ---------------------------------------------------------------------------
# Document header
# ---------------------------------------------------------------------------


@dataclass
class DrawingUnits:
    """Everything known about the drawing's measurement system."""

    #: Multiplier taking raw DXF coordinates to metres.
    scale_to_m: float = 1.0
    #: Human name of the detected unit: millimetres, feet, ...
    unit_name: str = "unknown"
    #: Raw ``$INSUNITS`` header value, or ``None`` when absent.
    insunits: Optional[int] = None
    #: header | dimension | heuristic | user — how ``scale_to_m`` was decided.
    method: str = "heuristic"
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scale_to_m": self.scale_to_m,
            "unit_name": self.unit_name,
            "insunits": self.insunits,
            "method": self.method,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "DrawingUnits":
        d = d or {}
        insunits = d.get("insunits")
        return DrawingUnits(
            scale_to_m=_f(d.get("scale_to_m"), 1.0),
            unit_name=str(d.get("unit_name", "unknown")),
            insunits=int(insunits) if insunits is not None else None,
            method=str(d.get("method", "heuristic")),
            confidence=_f(d.get("confidence")),
            reason=str(d.get("reason", "")),
        )


@dataclass
class NorthArrow:
    """Plan heading of true north, in degrees CCW from +Y.

    Recovered from ``$NORTHDIRECTION`` or from a north-arrow block. Drives
    daylight direction, so a wrong value tilts every sun study in the project.
    """

    #: Degrees; 0 means north points along +Y (up the page).
    heading_deg: float = 0.0
    source: str = Source.CAD_METADATA
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "heading_deg": round(self.heading_deg, 3),
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "NorthArrow":
        d = d or {}
        return NorthArrow(
            heading_deg=_f(d.get("heading_deg")),
            source=str(d.get("source", Source.CAD_METADATA)),
            confidence=_f(d.get("confidence")),
            reason=str(d.get("reason", "")),
        )


# ---------------------------------------------------------------------------
# Root document
# ---------------------------------------------------------------------------


@dataclass
class CadDocument:
    """Everything extracted from one DXF file."""

    schema_version: str = SCHEMA_VERSION
    source_path: str = ""
    dxf_version: str = ""

    units: DrawingUnits = field(default_factory=DrawingUnits)
    north: NorthArrow = field(default_factory=NorthArrow)

    layers: List[CadLayer] = field(default_factory=list)
    segments: List[CadSegment] = field(default_factory=list)
    polylines: List[CadPolyline] = field(default_factory=list)
    blocks: List[CadBlockRef] = field(default_factory=list)
    texts: List[CadText] = field(default_factory=list)
    dimensions: List[CadDimension] = field(default_factory=list)
    hatches: List[CadHatch] = field(default_factory=list)

    #: Translation applied to centre the drawing on the origin, in metres.
    #: Recorded so a coordinate can be mapped back to the original file.
    origin_offset: Point = (0.0, 0.0)
    bounds_min: Point = (0.0, 0.0)
    bounds_max: Point = (0.0, 0.0)

    #: Counts, timings, layer histograms, what was skipped and why.
    stats: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # -- queries ------------------------------------------------------------

    def layer_by_name(self, name: str) -> Optional[CadLayer]:
        upper = name.upper()
        for layer in self.layers:
            if layer.name.upper() == upper:
                return layer
        return None

    def layer_role(self, name: str) -> str:
        layer = self.layer_by_name(name)
        return layer.role if layer else "unknown"

    def segments_with_role(self, *roles: str) -> List[CadSegment]:
        wanted = set(roles)
        return [s for s in self.segments if s.role in wanted]

    def wall_segments(self) -> List[CadSegment]:
        """Segments that should be extruded into 3D wall geometry."""
        return self.segments_with_role("wall")

    def blocks_of_kind(self, *kinds: str) -> List[CadBlockRef]:
        wanted = set(kinds)
        return [b for b in self.blocks if b.kind in wanted]

    def room_labels(self) -> List[CadText]:
        """Text records parsed as naming a room."""
        return [t for t in self.texts if t.role == "room_label"]

    def entities_within(
        self, polygon: Sequence[Point]
    ) -> Tuple[List[CadText], List[CadBlockRef]]:
        """Texts and blocks whose anchor point lies inside ``polygon``.

        The primary spatial query the semantic layer runs: it is how a label
        or a block becomes evidence about one specific room.
        """
        texts = [t for t in self.texts if point_in_polygon(t.insert, polygon)]
        blocks = [b for b in self.blocks if point_in_polygon(b.position, polygon)]
        return texts, blocks

    def to_geometry_json(self) -> Dict[str, Any]:
        """The legacy ``geometry.json`` document, with the CAD model attached.

        ``walls`` keeps exactly the shape every existing consumer expects, so
        room segmentation, the Blender generator and the web viewer are
        unaffected. The ``cad`` key is additive.
        """
        walls = [s.as_wall_dict() for s in self.wall_segments()]
        return {
            "metadata": {
                "source": self.source_path,
                "layers_used": sorted({s.layer for s in self.wall_segments()}),
                "scale_factor": self.units.scale_to_m,
                "segment_count": len(walls),
                "bounding_box": {
                    "min": [self.bounds_min[0], self.bounds_min[1]],
                    "max": [self.bounds_max[0], self.bounds_max[1]],
                },
                "units": "meters",
                "origin_normalized": self.origin_offset != (0.0, 0.0),
                "unit_detection": self.units.to_dict(),
                "north": self.north.to_dict(),
                "extractor": f"cad.reader v{SCHEMA_VERSION}",
            },
            "walls": walls,
            "cad": self.to_dict(),
        }

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_path": self.source_path,
            "dxf_version": self.dxf_version,
            "units": self.units.to_dict(),
            "north": self.north.to_dict(),
            "layers": [x.to_dict() for x in self.layers],
            "segments": [x.to_dict() for x in self.segments],
            "polylines": [x.to_dict() for x in self.polylines],
            "blocks": [x.to_dict() for x in self.blocks],
            "texts": [x.to_dict() for x in self.texts],
            "dimensions": [x.to_dict() for x in self.dimensions],
            "hatches": [x.to_dict() for x in self.hatches],
            "origin_offset": [round(self.origin_offset[0], 6), round(self.origin_offset[1], 6)],
            "bounds_min": [round(self.bounds_min[0], 6), round(self.bounds_min[1], 6)],
            "bounds_max": [round(self.bounds_max[0], 6), round(self.bounds_max[1], 6)],
            "stats": self.stats,
            "warnings": list(self.warnings),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CadDocument":
        offset = d.get("origin_offset") or [0.0, 0.0]
        bmin = d.get("bounds_min") or [0.0, 0.0]
        bmax = d.get("bounds_max") or [0.0, 0.0]
        return CadDocument(
            schema_version=str(d.get("schema_version", SCHEMA_VERSION)),
            source_path=str(d.get("source_path", "")),
            dxf_version=str(d.get("dxf_version", "")),
            units=DrawingUnits.from_dict(d.get("units")),
            north=NorthArrow.from_dict(d.get("north")),
            layers=[CadLayer.from_dict(x) for x in (d.get("layers") or [])],
            segments=[CadSegment.from_dict(x) for x in (d.get("segments") or [])],
            polylines=[CadPolyline.from_dict(x) for x in (d.get("polylines") or [])],
            blocks=[CadBlockRef.from_dict(x) for x in (d.get("blocks") or [])],
            texts=[CadText.from_dict(x) for x in (d.get("texts") or [])],
            dimensions=[CadDimension.from_dict(x) for x in (d.get("dimensions") or [])],
            hatches=[CadHatch.from_dict(x) for x in (d.get("hatches") or [])],
            origin_offset=(_f(offset[0]), _f(offset[1])),
            bounds_min=(_f(bmin[0]), _f(bmin[1])),
            bounds_max=(_f(bmax[0]), _f(bmax[1])),
            stats=dict(d.get("stats") or {}),
            warnings=list(d.get("warnings") or []),
        )

    @staticmethod
    def from_geometry_json(d: Dict[str, Any]) -> Optional["CadDocument"]:
        """Recover the CAD model from a ``geometry.json``, if one is embedded.

        Returns ``None`` for a file written by the legacy extractor, which
        callers must treat as "no CAD evidence available" rather than as an
        error — an old geometry file is still a valid input.
        """
        cad = d.get("cad")
        return CadDocument.from_dict(cad) if isinstance(cad, dict) else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray-casting containment test.

    Duplicated from ``vision.geometry2d`` rather than imported: that module
    lives under the vision package, and ``cad`` must stay independent of it so
    either can be used without the other.
    """
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            denominator = yj - yi
            if abs(denominator) > 1e-12 and x < (xj - xi) * (y - yi) / denominator + xi:
                inside = not inside
        j = i
    return inside


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce a value to float without raising, mirroring ``vision.schema``."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else default
    if isinstance(value, str):
        try:
            parsed = float(value.strip().replace(",", "."))
            return parsed if math.isfinite(parsed) else default
        except ValueError:
            return default
    return default
