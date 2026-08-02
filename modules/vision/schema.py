"""
ArchX3D — Scene Graph Schema
============================
The scene graph is the single source of truth handed from the vision pipeline
to the Blender generator. It replaces the old free-text ``styling.json``
(which listed furniture names that nothing ever read).

Design constraints
------------------
* **Stdlib only.** ``blender_generator.py`` runs inside Blender's bundled
  Python, which has no third-party packages. This module and ``catalog`` are
  the only vision modules Blender imports, so neither may import numpy,
  google-generativeai, PIL, etc.
* **Everything carries a confidence.** Nothing is asserted that the vision
  layer was not reasonably sure of; see `ConfidencePolicy`.
* **Metric units throughout.** Positions and dimensions are metres in the same
  coordinate frame as ``geometry.json`` (DXF-derived, +Z up).

Coordinate frame
----------------
``x``/``y`` are floor-plan metres matching the DXF extraction; ``z`` is height
above the floor. ``rotation_z`` is degrees counter-clockwise, where 0 means the
object's "front" faces +Y. An object's ``position`` is the centre of its
footprint at ``z`` (its base height), *not* its volumetric centre.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = "2.0"


# ---------------------------------------------------------------------------
# Confidence policy
# ---------------------------------------------------------------------------


class ConfidencePolicy:
    """Thresholds governing what survives into the generated scene.

    The brief is explicit that a missing object is better than an invented one,
    so the bands are deliberately conservative and the *default* behaviour for
    the uncertain band is to keep the record but not build geometry from it.
    """

    #: At or above this, the detection is built without qualification.
    ACCEPT = 0.65

    #: Between REVIEW and ACCEPT the record is kept but flagged ``uncertain``.
    REVIEW = 0.40

    #: Below REVIEW the detection is discarded entirely.
    #: (Kept in ``diagnostics.discarded`` for traceability.)
    DISCARD = REVIEW

    @classmethod
    def classify(cls, confidence: float) -> str:
        """Return one of ``"accept"``, ``"uncertain"``, ``"discard"``."""
        if confidence >= cls.ACCEPT:
            return "accept"
        if confidence >= cls.REVIEW:
            return "uncertain"
        return "discard"


# ---------------------------------------------------------------------------
# Small value types
# ---------------------------------------------------------------------------


@dataclass
class Vec3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {"x": round(self.x, 4), "y": round(self.y, 4), "z": round(self.z, 4)}

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Vec3":
        d = d or {}
        return Vec3(_f(d.get("x")), _f(d.get("y")), _f(d.get("z")))

    def distance_to(self, other: "Vec3") -> float:
        return math.dist((self.x, self.y, self.z), (other.x, other.y, other.z))

    def planar_distance_to(self, other: "Vec3") -> float:
        return math.dist((self.x, self.y), (other.x, other.y))


@dataclass
class Dimensions:
    """Axis-aligned extents in the object's own frame, in metres.

    ``width`` runs left-to-right across the object's front face, ``depth``
    front-to-back, ``height`` floor-to-top.
    """

    width: float = 0.0
    depth: float = 0.0
    height: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "width": round(self.width, 4),
            "depth": round(self.depth, 4),
            "height": round(self.height, 4),
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Dimensions":
        d = d or {}
        return Dimensions(_f(d.get("width")), _f(d.get("depth")), _f(d.get("height")))

    @property
    def footprint_area(self) -> float:
        return self.width * self.depth

    def is_degenerate(self) -> bool:
        return min(self.width, self.depth, self.height) <= 1e-6


@dataclass
class BBox2D:
    """Normalised image-space box, origin top-left, values in ``[0, 1]``."""

    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "x0": round(self.x0, 4),
            "y0": round(self.y0, 4),
            "x1": round(self.x1, 4),
            "y1": round(self.y1, 4),
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> Optional["BBox2D"]:
        if not d:
            return None
        box = BBox2D(_f(d.get("x0")), _f(d.get("y0")), _f(d.get("x1")), _f(d.get("y1")))
        # Tolerate transposed corners rather than rejecting the detection.
        if box.x1 < box.x0:
            box.x0, box.x1 = box.x1, box.x0
        if box.y1 < box.y0:
            box.y0, box.y1 = box.y1, box.y0
        return box

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    @property
    def aspect(self) -> float:
        """Width / height, guarded against zero-height boxes."""
        return self.width / self.height if self.height > 1e-6 else 0.0

    def iou(self, other: "BBox2D") -> float:
        ix0, iy0 = max(self.x0, other.x0), max(self.y0, other.y0)
        ix1, iy1 = min(self.x1, other.x1), min(self.y1, other.y1)
        inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        union = self.area + other.area - inter
        return inter / union if union > 1e-9 else 0.0


# ---------------------------------------------------------------------------
# Surface finishes
# ---------------------------------------------------------------------------


@dataclass
class Finish:
    """A material treatment applied to an architectural surface.

    ``material`` is a controlled vocabulary term (see ``catalog.MATERIALS``);
    ``description`` keeps the model's free-text observation for traceability.
    """

    material: str = "unknown"
    color_hex: str = "#CCCCCC"
    #: Perceptual roughness in ``[0, 1]``; drives the Blender Principled BSDF.
    roughness: float = 0.7
    metallic: float = 0.0
    #: e.g. "matte", "satin", "gloss" — refines roughness when present.
    finish: str = "matte"
    description: str = ""
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "material": self.material,
            "color_hex": self.color_hex,
            "roughness": round(self.roughness, 3),
            "metallic": round(self.metallic, 3),
            "finish": self.finish,
            "description": self.description,
            "confidence": round(self.confidence, 3),
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Finish":
        d = d or {}
        return Finish(
            material=str(d.get("material", "unknown")),
            color_hex=normalise_hex(d.get("color_hex")),
            roughness=_f(d.get("roughness"), 0.7),
            metallic=_f(d.get("metallic"), 0.0),
            finish=str(d.get("finish", "matte")),
            description=str(d.get("description", "")),
            confidence=_f(d.get("confidence")),
        )


# ---------------------------------------------------------------------------
# Architectural elements
# ---------------------------------------------------------------------------


@dataclass
class Wall:
    """A vertical surface. Geometry is DXF-derived; finish is vision-derived."""

    id: str = ""
    start: Tuple[float, float] = (0.0, 0.0)
    end: Tuple[float, float] = (0.0, 0.0)
    height: float = 3.0
    thickness: float = 0.15
    finish: Finish = field(default_factory=Finish)
    #: True when a reference image was actually matched to this wall.
    observed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "start": [round(self.start[0], 4), round(self.start[1], 4)],
            "end": [round(self.end[0], 4), round(self.end[1], 4)],
            "height": round(self.height, 4),
            "thickness": round(self.thickness, 4),
            "finish": self.finish.to_dict(),
            "observed": self.observed,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Wall":
        start = d.get("start") or [0.0, 0.0]
        end = d.get("end") or [0.0, 0.0]
        return Wall(
            id=str(d.get("id", "")),
            start=(_f(start[0]), _f(start[1])),
            end=(_f(end[0]), _f(end[1])),
            height=_f(d.get("height"), 3.0),
            thickness=_f(d.get("thickness"), 0.15),
            finish=Finish.from_dict(d.get("finish")),
            observed=bool(d.get("observed", False)),
        )

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def angle_deg(self) -> float:
        """Direction of the wall run, degrees CCW from +X."""
        return math.degrees(
            math.atan2(self.end[1] - self.start[1], self.end[0] - self.start[0])
        )

    @property
    def midpoint(self) -> Tuple[float, float]:
        return ((self.start[0] + self.end[0]) / 2.0, (self.start[1] + self.end[1]) / 2.0)

    def inward_normal_deg(self, room_center: Tuple[float, float]) -> float:
        """Heading (degrees CCW from +Y) pointing from the wall into the room.

        Used to orient objects that stand against this wall.
        """
        nx, ny = -math.sin(math.radians(self.angle_deg)), math.cos(
            math.radians(self.angle_deg)
        )
        mx, my = self.midpoint
        # Flip the normal if it points away from the room interior.
        if (room_center[0] - mx) * nx + (room_center[1] - my) * ny < 0:
            nx, ny = -nx, -ny
        return math.degrees(math.atan2(-nx, ny)) % 360.0


@dataclass
class Opening:
    """A door or window cut into a wall."""

    id: str = ""
    kind: str = "window"  # door | window | archway | niche
    wall_id: str = ""
    #: Room this opening serves.
    room_id: str = ""
    #: Centre of the opening in floor-plan metres.
    position: Vec3 = field(default_factory=Vec3)
    width: float = 0.9
    height: float = 2.1
    #: Height of the opening's bottom edge above the floor.
    sill_height: float = 0.0
    confidence: float = 0.0
    uncertain: bool = False
    source_images: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "wall_id": self.wall_id,
            "position": self.position.to_dict(),
            "width": round(self.width, 4),
            "height": round(self.height, 4),
            "sill_height": round(self.sill_height, 4),
            "confidence": round(self.confidence, 3),
            "uncertain": self.uncertain,
            "source_images": list(self.source_images),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Opening":
        return Opening(
            id=str(d.get("id", "")),
            kind=str(d.get("kind", "window")),
            wall_id=str(d.get("wall_id", "")),
            position=Vec3.from_dict(d.get("position")),
            width=_f(d.get("width"), 0.9),
            height=_f(d.get("height"), 2.1),
            sill_height=_f(d.get("sill_height"), 0.0),
            confidence=_f(d.get("confidence")),
            uncertain=bool(d.get("uncertain", False)),
            source_images=list(d.get("source_images") or []),
        )


@dataclass
class ArchElement:
    """Structural features that are neither wall, floor, ceiling nor opening.

    Covers columns, beams, staircases, partitions, false-ceiling volumes.
    """

    id: str = ""
    kind: str = "column"
    position: Vec3 = field(default_factory=Vec3)
    dimensions: Dimensions = field(default_factory=Dimensions)
    rotation_z: float = 0.0
    finish: Finish = field(default_factory=Finish)
    confidence: float = 0.0
    uncertain: bool = False
    source_images: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "position": self.position.to_dict(),
            "dimensions": self.dimensions.to_dict(),
            "rotation_z": round(self.rotation_z, 2),
            "finish": self.finish.to_dict(),
            "confidence": round(self.confidence, 3),
            "uncertain": self.uncertain,
            "source_images": list(self.source_images),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ArchElement":
        return ArchElement(
            id=str(d.get("id", "")),
            kind=str(d.get("kind", "column")),
            position=Vec3.from_dict(d.get("position")),
            dimensions=Dimensions.from_dict(d.get("dimensions")),
            rotation_z=_f(d.get("rotation_z")),
            finish=Finish.from_dict(d.get("finish")),
            confidence=_f(d.get("confidence")),
            uncertain=bool(d.get("uncertain", False)),
            source_images=list(d.get("source_images") or []),
        )


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------


@dataclass
class LightSource:
    """A luminaire recovered from the reference imagery.

    ``kind`` selects both the Blender light type and the procedural fixture
    geometry (see ``catalog.LIGHT_TYPES``).
    """

    id: str = ""
    kind: str = "ceiling_light"
    position: Vec3 = field(default_factory=Vec3)
    #: floor | wall | ceiling | table — determines how the fixture is mounted.
    mounting: str = "ceiling"
    color_temperature_k: float = 3000.0
    #: Blender light power in watts; derived from fixture type and room size.
    power_w: float = 60.0
    #: Radius/edge of the emitting surface, metres.
    size: float = 0.2
    #: For strips/linear fixtures, the run length in metres.
    length: float = 0.0
    #: Wall this fixture is mounted on, when applicable.
    wall_id: str = ""
    #: Which segmented room this fixture lights.
    room_id: str = ""
    confidence: float = 0.0
    uncertain: bool = False
    source_images: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "room_id": self.room_id,
            "position": self.position.to_dict(),
            "mounting": self.mounting,
            "color_temperature_k": round(self.color_temperature_k, 1),
            "power_w": round(self.power_w, 2),
            "size": round(self.size, 4),
            "length": round(self.length, 4),
            "wall_id": self.wall_id,
            "confidence": round(self.confidence, 3),
            "uncertain": self.uncertain,
            "source_images": list(self.source_images),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "LightSource":
        return LightSource(
            id=str(d.get("id", "")),
            kind=str(d.get("kind", "ceiling_light")),
            position=Vec3.from_dict(d.get("position")),
            mounting=str(d.get("mounting", "ceiling")),
            color_temperature_k=_f(d.get("color_temperature_k"), 3000.0),
            power_w=_f(d.get("power_w"), 60.0),
            size=_f(d.get("size"), 0.2),
            length=_f(d.get("length"), 0.0),
            wall_id=str(d.get("wall_id", "")),
            room_id=str(d.get("room_id", "")),
            confidence=_f(d.get("confidence")),
            uncertain=bool(d.get("uncertain", False)),
            source_images=list(d.get("source_images") or []),
        )


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------


@dataclass
class SceneObject:
    """A furniture item or decorative object placed in the room."""

    id: str = ""
    #: Controlled-vocabulary category from ``catalog.OBJECT_CATALOG``.
    category: str = ""
    #: The model's free-text label, e.g. "L-shaped grey fabric sectional".
    label: str = ""
    #: furniture | decor | appliance — drives placement strategy and LOD.
    group: str = "furniture"
    #: Which segmented room this object belongs to.
    room_id: str = ""

    position: Vec3 = field(default_factory=Vec3)
    rotation_z: float = 0.0
    dimensions: Dimensions = field(default_factory=Dimensions)
    #: Uniform scale applied to the matched asset to hit ``dimensions``.
    scale: float = 1.0

    #: floor | wall | ceiling | on_object — what physically carries this object.
    support: str = "floor"
    #: When ``support == "on_object"``, the id of the carrying object.
    support_id: str = ""
    #: When wall-mounted, which wall.
    wall_id: str = ""

    color_hex: str = "#BFBFBF"
    material: str = "unknown"

    #: Chosen procedural asset key (see ``assets.py``).
    asset: str = ""
    asset_score: float = 0.0

    confidence: float = 0.0
    uncertain: bool = False
    #: Human-readable notes: why it was flagged, what was corrected, etc.
    flags: List[str] = field(default_factory=list)

    #: Set by the user in the review step to pin this object's placement.
    #: A locked object rejects further transform edits and is never nudged by
    #: automatic collision resolution — it is the user's stated ground truth.
    locked: bool = False

    #: Image-space evidence, retained for debugging and re-fusion.
    bbox_2d: Optional[BBox2D] = None
    source_images: List[str] = field(default_factory=list)
    #: How many independent images contributed to this record.
    observation_count: int = 1

    #: Distances computed during grounding, exposed for inspection.
    distance_to_nearest_wall: float = 0.0
    distance_to_room_center: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "label": self.label,
            "group": self.group,
            "room_id": self.room_id,
            "position": self.position.to_dict(),
            "rotation_z": round(self.rotation_z, 2),
            "dimensions": self.dimensions.to_dict(),
            "scale": round(self.scale, 4),
            "support": self.support,
            "support_id": self.support_id,
            "wall_id": self.wall_id,
            "color_hex": self.color_hex,
            "material": self.material,
            "asset": self.asset,
            "asset_score": round(self.asset_score, 3),
            "confidence": round(self.confidence, 3),
            "uncertain": self.uncertain,
            "flags": list(self.flags),
            "locked": self.locked,
            "source_images": list(self.source_images),
            "observation_count": self.observation_count,
            "distance_to_nearest_wall": round(self.distance_to_nearest_wall, 3),
            "distance_to_room_center": round(self.distance_to_room_center, 3),
        }
        if self.bbox_2d is not None:
            out["bbox_2d"] = self.bbox_2d.to_dict()
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SceneObject":
        return SceneObject(
            id=str(d.get("id", "")),
            category=str(d.get("category", "")),
            label=str(d.get("label", "")),
            group=str(d.get("group", "furniture")),
            room_id=str(d.get("room_id", "")),
            position=Vec3.from_dict(d.get("position")),
            rotation_z=_f(d.get("rotation_z")),
            dimensions=Dimensions.from_dict(d.get("dimensions")),
            scale=_f(d.get("scale"), 1.0),
            support=str(d.get("support", "floor")),
            support_id=str(d.get("support_id", "")),
            wall_id=str(d.get("wall_id", "")),
            color_hex=normalise_hex(d.get("color_hex")),
            material=str(d.get("material", "unknown")),
            asset=str(d.get("asset", "")),
            asset_score=_f(d.get("asset_score")),
            confidence=_f(d.get("confidence")),
            uncertain=bool(d.get("uncertain", False)),
            flags=list(d.get("flags") or []),
            locked=bool(d.get("locked", False)),
            bbox_2d=BBox2D.from_dict(d.get("bbox_2d")),
            source_images=list(d.get("source_images") or []),
            observation_count=int(d.get("observation_count", 1) or 1),
            distance_to_nearest_wall=_f(d.get("distance_to_nearest_wall")),
            distance_to_room_center=_f(d.get("distance_to_room_center")),
        )

    def footprint_corners(self) -> List[Tuple[float, float]]:
        """The four floor-plane corners of this object's oriented footprint."""
        hw, hd = self.dimensions.width / 2.0, self.dimensions.depth / 2.0
        theta = math.radians(self.rotation_z)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        corners = []
        for dx, dy in ((-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)):
            corners.append(
                (
                    self.position.x + dx * cos_t - dy * sin_t,
                    self.position.y + dx * sin_t + dy * cos_t,
                )
            )
        return corners


@dataclass
class Relationship:
    """A semantic constraint between two entities in the graph.

    Relationships are first-class: the placement solver reads them, so
    "sofa faces tv_unit" actually rotates the sofa rather than being decoration.
    """

    subject: str = ""
    predicate: str = ""
    object: str = ""
    confidence: float = 0.0
    #: True once the solver successfully enforced this constraint.
    satisfied: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "confidence": round(self.confidence, 3),
            "satisfied": self.satisfied,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Relationship":
        return Relationship(
            subject=str(d.get("subject", "")),
            predicate=str(d.get("predicate", "")),
            object=str(d.get("object", "")),
            confidence=_f(d.get("confidence")),
            satisfied=bool(d.get("satisfied", False)),
        )


# ---------------------------------------------------------------------------
# Room + graph root
# ---------------------------------------------------------------------------


@dataclass
class Room:
    id: str = "room_0"
    room_type: str = "unknown"
    style: str = "unknown"
    #: Closed floor polygon in plan metres (DXF-derived where available).
    polygon: List[Tuple[float, float]] = field(default_factory=list)
    bounds_min: Tuple[float, float] = (0.0, 0.0)
    bounds_max: Tuple[float, float] = (0.0, 0.0)
    ceiling_height: float = 3.0
    confidence: float = 0.0
    #: Floor area in m², from segmentation rather than the bounding box.
    area: float = 0.0
    #: Ids of rooms reachable through a doorway.
    connected_to: List[str] = field(default_factory=list)
    #: Wall segments bounding this room.
    wall_ids: List[str] = field(default_factory=list)
    #: Reference images matched to this room.
    source_images: List[str] = field(default_factory=list)
    #: Per-room finishes; fall back to the graph-level ones when unobserved.
    wall_finish: Optional["Finish"] = None
    floor_finish: Optional["Finish"] = None
    ceiling_finish: Optional["Finish"] = None
    ceiling_type: str = "plain"
    #: How sure the style label is. ``style`` was previously free text used as
    #: a label; it now drives asset selection, so its reliability matters.
    style_confidence: float = 0.0
    #: The room's characteristic colours, by role.
    palette: Optional["ColourPalette"] = None
    #: Room-scale lighting conditions, distinct from the fixture list.
    lighting: Optional["LightingEnvironment"] = None

    #: Why this room was classified as it was, most load-bearing reason first.
    #: A confidence with no reasons attached is not reviewable — a user cannot
    #: tell a well-evidenced 0.9 from a lucky one — so the evidence travels
    #: with the answer.
    type_reasons: List[str] = field(default_factory=list)
    #: The signal that settled the room type, e.g. ``"room_label"``. Empty
    #: when no single source was authoritative and fusion decided.
    type_decided_by: str = ""
    #: Evidence that contradicts the chosen type, in human-readable form.
    #: Never silently resolved: a room labelled STORE containing a toilet is
    #: a drafting error a person should see, not one to average away.
    type_conflicts: List[str] = field(default_factory=list)
    #: A more specific label than ``room_type`` where the drawing gave one,
    #: e.g. ``"master_bedroom"`` for a room scored as ``"bedroom"``.
    specific_type: str = ""
    #: Second-most-likely reading and its share, so a near-miss stays visible.
    runner_up_type: str = ""
    runner_up_confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "room_type": self.room_type,
            "style": self.style,
            "polygon": [[round(p[0], 4), round(p[1], 4)] for p in self.polygon],
            "bounds_min": [round(self.bounds_min[0], 4), round(self.bounds_min[1], 4)],
            "bounds_max": [round(self.bounds_max[0], 4), round(self.bounds_max[1], 4)],
            "ceiling_height": round(self.ceiling_height, 4),
            "confidence": round(self.confidence, 3),
            "area": round(self.area, 3),
            "connected_to": list(self.connected_to),
            "wall_ids": list(self.wall_ids),
            "source_images": list(self.source_images),
            "ceiling_type": self.ceiling_type,
            "style_confidence": round(self.style_confidence, 3),
            "type_reasons": list(self.type_reasons),
            "type_decided_by": self.type_decided_by,
            "type_conflicts": list(self.type_conflicts),
            "specific_type": self.specific_type,
            "runner_up_type": self.runner_up_type,
            "runner_up_confidence": round(self.runner_up_confidence, 3),
        }
        for key, value in (
            ("wall_finish", self.wall_finish),
            ("floor_finish", self.floor_finish),
            ("ceiling_finish", self.ceiling_finish),
            ("palette", self.palette),
            ("lighting", self.lighting),
        ):
            if value is not None:
                out[key] = value.to_dict()
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Room":
        bmin = d.get("bounds_min") or [0.0, 0.0]
        bmax = d.get("bounds_max") or [0.0, 0.0]
        return Room(
            id=str(d.get("id", "room_0")),
            room_type=str(d.get("room_type", "unknown")),
            style=str(d.get("style", "unknown")),
            polygon=[(_f(p[0]), _f(p[1])) for p in (d.get("polygon") or [])],
            bounds_min=(_f(bmin[0]), _f(bmin[1])),
            bounds_max=(_f(bmax[0]), _f(bmax[1])),
            ceiling_height=_f(d.get("ceiling_height"), 3.0),
            confidence=_f(d.get("confidence")),
            area=_f(d.get("area")),
            connected_to=list(d.get("connected_to") or []),
            wall_ids=list(d.get("wall_ids") or []),
            source_images=list(d.get("source_images") or []),
            wall_finish=Finish.from_dict(d["wall_finish"]) if d.get("wall_finish") else None,
            floor_finish=Finish.from_dict(d["floor_finish"]) if d.get("floor_finish") else None,
            ceiling_finish=(
                Finish.from_dict(d["ceiling_finish"]) if d.get("ceiling_finish") else None
            ),
            ceiling_type=str(d.get("ceiling_type", "plain")),
            style_confidence=_f(d.get("style_confidence")),
            palette=ColourPalette.from_dict(d.get("palette")),
            lighting=LightingEnvironment.from_dict(d.get("lighting")),
            type_reasons=[str(x) for x in (d.get("type_reasons") or [])],
            type_decided_by=str(d.get("type_decided_by", "")),
            type_conflicts=[str(x) for x in (d.get("type_conflicts") or [])],
            specific_type=str(d.get("specific_type", "")),
            runner_up_type=str(d.get("runner_up_type", "")),
            runner_up_confidence=_f(d.get("runner_up_confidence")),
        )

    @property
    def center(self) -> Tuple[float, float]:
        return (
            (self.bounds_min[0] + self.bounds_max[0]) / 2.0,
            (self.bounds_min[1] + self.bounds_max[1]) / 2.0,
        )

    @property
    def width(self) -> float:
        return self.bounds_max[0] - self.bounds_min[0]

    @property
    def depth(self) -> float:
        return self.bounds_max[1] - self.bounds_min[1]

    @property
    def floor_area(self) -> float:
        return self.width * self.depth


@dataclass
class ViewPoint:
    """The camera fitted to one reference image, kept for comparison.

    ``grounding.estimate_camera`` already fits a plausible pinhole camera to
    every reference shot in order to back-project furniture onto the floor.
    That pose was previously discarded once grounding finished. Persisting it
    is what makes objective comparison possible: the generated scene can be
    rendered from the *same* vantage as the photograph, so the two images are
    of the same view and any difference is a difference in the reconstruction
    rather than in where the camera was standing.

    The pose is a fitting device, not a claim about where the photographer
    stood — ``confidence`` carries how much to trust it.
    """

    image_id: str = ""
    room_id: str = ""
    #: Source filename, so a preview can be matched back to its reference.
    source_image: str = ""
    position: Vec3 = field(default_factory=Vec3)
    #: Heading in degrees, 0 = looking along +Y (matches ``rotation_z``).
    yaw: float = 0.0
    #: Positive tilts the view upward.
    pitch_deg: float = 0.0
    vertical_fov_deg: float = 55.0
    aspect: float = 16 / 9
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "image_id": self.image_id,
            "room_id": self.room_id,
            "source_image": self.source_image,
            "position": self.position.to_dict(),
            "yaw": round(self.yaw, 3),
            "pitch_deg": round(self.pitch_deg, 3),
            "vertical_fov_deg": round(self.vertical_fov_deg, 3),
            "aspect": round(self.aspect, 4),
            "confidence": round(self.confidence, 3),
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "ViewPoint":
        d = d or {}
        return ViewPoint(
            image_id=str(d.get("image_id", "")),
            room_id=str(d.get("room_id", "")),
            source_image=str(d.get("source_image", "")),
            position=Vec3.from_dict(d.get("position")),
            yaw=_f(d.get("yaw")),
            pitch_deg=_f(d.get("pitch_deg")),
            vertical_fov_deg=_f(d.get("vertical_fov_deg"), 55.0),
            aspect=_f(d.get("aspect"), 16 / 9),
            confidence=_f(d.get("confidence")),
        )


@dataclass
class ColourPalette:
    """The colours that characterise one room.

    Six named roles rather than a flat list of dominant colours, because the
    roles are what reconstruction needs: an accent is used sparingly on decor
    and would be wrong as a wall, and lighting colour is not a surface colour
    at all. A flat histogram cannot express that distinction.
    """

    primary: str = "#CCCCCC"
    secondary: str = "#BFBFBF"
    accent: str = "#8899AA"
    lighting: str = "#FFF0DC"
    furniture: str = "#B5AFA5"
    decor: str = "#9AA5AE"
    #: observed | style_prior | mixed — where these colours came from. A
    #: palette derived from a style prior is a plausible guess, not evidence.
    source: str = "observed"
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary": self.primary, "secondary": self.secondary,
            "accent": self.accent, "lighting": self.lighting,
            "furniture": self.furniture, "decor": self.decor,
            "source": self.source, "confidence": round(self.confidence, 3),
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> Optional["ColourPalette"]:
        if not d:
            return None
        return ColourPalette(
            primary=normalise_hex(d.get("primary")),
            secondary=normalise_hex(d.get("secondary")),
            accent=normalise_hex(d.get("accent")),
            lighting=normalise_hex(d.get("lighting")),
            furniture=normalise_hex(d.get("furniture")),
            decor=normalise_hex(d.get("decor")),
            source=str(d.get("source", "observed")),
            confidence=_f(d.get("confidence")),
        )

    def roles(self) -> Dict[str, str]:
        return {
            "primary": self.primary, "secondary": self.secondary,
            "accent": self.accent, "lighting": self.lighting,
            "furniture": self.furniture, "decor": self.decor,
        }


@dataclass
class LightingEnvironment:
    """Room-scale lighting conditions, as distinct from individual fixtures.

    Luminaires say what is emitting; this says what the room *looks* like —
    how bright, how warm, how hard the shadows, and how much of it is daylight
    coming through a window rather than lamps. Two rooms with identical
    fixtures photographed at noon and at midnight need different renders, and
    nothing in the fixture list expresses that.

    Daylight direction is a plan heading, so it can be pointed through the
    room's actual windows rather than invented.
    """

    #: Overall brightness, 0 (dark) to 1 (bright). Not a photometric unit —
    #: a relative level the renderer scales its ambient contribution by.
    ambient: float = 0.45
    #: Plan heading the daylight arrives from, degrees; -1 when unknown.
    daylight_direction: float = -1.0
    #: Sun elevation above the horizon, degrees.
    daylight_elevation: float = 35.0
    #: Share of the room's illumination coming from windows, 0–1.
    window_contribution: float = 0.35
    #: Dominant correlated colour temperature of the scene, kelvin.
    color_temperature_k: float = 3400.0
    #: 0 = hard, crisp shadows (point sun); 1 = fully diffuse (overcast).
    shadow_softness: float = 0.5
    #: day | evening | night | overcast
    time_of_day: str = "day"
    #: observed | inferred | default
    source: str = "default"
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ambient": round(self.ambient, 3),
            "daylight_direction": round(self.daylight_direction, 2),
            "daylight_elevation": round(self.daylight_elevation, 2),
            "window_contribution": round(self.window_contribution, 3),
            "color_temperature_k": round(self.color_temperature_k, 1),
            "shadow_softness": round(self.shadow_softness, 3),
            "time_of_day": self.time_of_day,
            "source": self.source,
            "confidence": round(self.confidence, 3),
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> Optional["LightingEnvironment"]:
        if not d:
            return None
        return LightingEnvironment(
            ambient=_clamp01(_f(d.get("ambient"), 0.45)),
            daylight_direction=_f(d.get("daylight_direction"), -1.0),
            daylight_elevation=_f(d.get("daylight_elevation"), 35.0),
            window_contribution=_clamp01(_f(d.get("window_contribution"), 0.35)),
            color_temperature_k=_f(d.get("color_temperature_k"), 3400.0),
            shadow_softness=_clamp01(_f(d.get("shadow_softness"), 0.5)),
            time_of_day=str(d.get("time_of_day", "day")),
            source=str(d.get("source", "default")),
            confidence=_f(d.get("confidence")),
        )


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


@dataclass
class SceneGraph:
    """Root document. Serialised to ``data/scene_graph.json``."""

    schema_version: str = SCHEMA_VERSION
    #: Every enclosed space found in the plan, largest first. A single-room
    #: plan yields a one-element list, so the whole pipeline has one code path.
    rooms: List[Room] = field(default_factory=lambda: [Room()])
    walls: List[Wall] = field(default_factory=list)
    floor: Finish = field(default_factory=Finish)
    ceiling: Finish = field(default_factory=Finish)
    ceiling_type: str = "plain"
    openings: List[Opening] = field(default_factory=list)
    architecture: List[ArchElement] = field(default_factory=list)
    lights: List[LightSource] = field(default_factory=list)
    objects: List[SceneObject] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)
    #: Cameras fitted to the reference images, one per contributing shot.
    #: Kept so the build can be previewed from the same vantage and compared.
    viewpoints: List[ViewPoint] = field(default_factory=list)
    #: Free-form record of models used, timings, corrections, discards.
    provenance: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    # -- serialisation ------------------------------------------------------

    @property
    def room(self) -> Room:
        """The primary (largest) room.

        Retained so single-room consumers — chiefly the Blender generator's
        camera framing — keep working unchanged against a multi-room graph.
        """
        return self.rooms[0] if self.rooms else Room()

    def room_by_id(self, room_id: str) -> Optional[Room]:
        for room in self.rooms:
            if room.id == room_id:
                return room
        return None

    def objects_in_room(self, room_id: str, include_uncertain: bool = False) -> List["SceneObject"]:
        return [o for o in self.buildable_objects(include_uncertain) if o.room_id == room_id]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rooms": [r.to_dict() for r in self.rooms],
            # Mirrored for consumers that predate multi-room support.
            "room": self.room.to_dict(),
            "walls": [w.to_dict() for w in self.walls],
            "floor": self.floor.to_dict(),
            "ceiling": self.ceiling.to_dict(),
            "ceiling_type": self.ceiling_type,
            "openings": [o.to_dict() for o in self.openings],
            "architecture": [a.to_dict() for a in self.architecture],
            "lights": [lt.to_dict() for lt in self.lights],
            "objects": [o.to_dict() for o in self.objects],
            "relationships": [r.to_dict() for r in self.relationships],
            "viewpoints": [v.to_dict() for v in self.viewpoints],
            "provenance": self.provenance,
            "diagnostics": self.diagnostics,
        }

    def viewpoints_for(self, room_id: str) -> List[ViewPoint]:
        return [v for v in self.viewpoints if v.room_id == room_id]

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SceneGraph":
        # Accept both the multi-room list and the older single `room` object,
        # so a scene graph written by an earlier version still loads.
        raw_rooms = d.get("rooms")
        if raw_rooms:
            rooms = [Room.from_dict(r) for r in raw_rooms]
        elif d.get("room"):
            rooms = [Room.from_dict(d["room"])]
        else:
            rooms = [Room()]

        return SceneGraph(
            schema_version=str(d.get("schema_version", SCHEMA_VERSION)),
            rooms=rooms,
            walls=[Wall.from_dict(w) for w in (d.get("walls") or [])],
            floor=Finish.from_dict(d.get("floor")),
            ceiling=Finish.from_dict(d.get("ceiling")),
            ceiling_type=str(d.get("ceiling_type", "plain")),
            openings=[Opening.from_dict(o) for o in (d.get("openings") or [])],
            architecture=[ArchElement.from_dict(a) for a in (d.get("architecture") or [])],
            lights=[LightSource.from_dict(x) for x in (d.get("lights") or [])],
            objects=[SceneObject.from_dict(o) for o in (d.get("objects") or [])],
            relationships=[Relationship.from_dict(r) for r in (d.get("relationships") or [])],
            viewpoints=[ViewPoint.from_dict(v) for v in (d.get("viewpoints") or [])],
            provenance=dict(d.get("provenance") or {}),
            diagnostics=dict(d.get("diagnostics") or {}),
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)

    @staticmethod
    def load(path: str) -> "SceneGraph":
        with open(path, "r", encoding="utf-8") as fh:
            return SceneGraph.from_dict(json.load(fh))

    # -- queries ------------------------------------------------------------

    def object_by_id(self, obj_id: str) -> Optional[SceneObject]:
        for obj in self.objects:
            if obj.id == obj_id:
                return obj
        return None

    def wall_by_id(self, wall_id: str) -> Optional[Wall]:
        for wall in self.walls:
            if wall.id == wall_id:
                return wall
        return None

    def buildable_objects(self, include_uncertain: bool = False) -> List[SceneObject]:
        """Objects the Blender generator should actually construct.

        Uncertain records are withheld by default — the brief prefers an
        omission over a guess.
        """
        return [
            o
            for o in self.objects
            if (include_uncertain or not o.uncertain) and not o.dimensions.is_degenerate()
        ]

    def buildable_lights(self, include_uncertain: bool = False) -> List[LightSource]:
        return [lt for lt in self.lights if include_uncertain or not lt.uncertain]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce a model-supplied value to float without raising.

    VLM output routinely contains ``null``, ``"1.2m"`` or ``""`` in numeric
    slots; a bad number should degrade to the default, not kill the run.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else default
    if isinstance(value, str):
        cleaned = value.strip().rstrip("m").replace(",", ".").strip()
        try:
            parsed = float(cleaned)
            return parsed if math.isfinite(parsed) else default
        except ValueError:
            return default
    return default


def normalise_hex(value: Any, default: str = "#BFBFBF") -> str:
    """Coerce assorted colour spellings to ``#RRGGBB``."""
    if not isinstance(value, str):
        return default
    text = value.strip().lstrip("#")
    if len(text) == 3 and all(c in "0123456789abcdefABCDEF" for c in text):
        text = "".join(c * 2 for c in text)
    if len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text):
        return "#" + text.upper()
    return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def validate_graph(graph: SceneGraph) -> List[str]:
    """Structural sanity check. Returns a list of human-readable problems.

    This is schema-level validation (ids unique, references resolve, numbers
    finite). Physical plausibility lives in ``validate.py``.
    """
    problems: List[str] = []

    seen_ids = set()
    for obj in graph.objects:
        if not obj.id:
            problems.append(f"object with empty id (category={obj.category!r})")
        elif obj.id in seen_ids:
            problems.append(f"duplicate object id {obj.id!r}")
        seen_ids.add(obj.id)

        if obj.dimensions.is_degenerate():
            problems.append(f"{obj.id}: degenerate dimensions {obj.dimensions.to_dict()}")
        if not 0.0 <= obj.confidence <= 1.0:
            problems.append(f"{obj.id}: confidence {obj.confidence} outside [0,1]")
        if obj.support == "on_object" and not obj.support_id:
            problems.append(f"{obj.id}: support 'on_object' but no support_id")
        if obj.support_id and obj.support_id not in seen_ids | {o.id for o in graph.objects}:
            problems.append(f"{obj.id}: support_id {obj.support_id!r} does not resolve")

    wall_ids = {w.id for w in graph.walls}
    for opening in graph.openings:
        if opening.wall_id and opening.wall_id not in wall_ids:
            problems.append(f"{opening.id}: wall_id {opening.wall_id!r} does not resolve")

    all_ids = seen_ids | wall_ids | {lt.id for lt in graph.lights}
    for rel in graph.relationships:
        if rel.subject not in all_ids:
            problems.append(f"relationship subject {rel.subject!r} does not resolve")
        if rel.object not in all_ids:
            problems.append(f"relationship object {rel.object!r} does not resolve")

    return problems
