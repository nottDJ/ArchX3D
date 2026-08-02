"""
ArchX3D — DXF Reader
====================
Reads *everything* a DXF file contains into a ``CadDocument``.

Contrast with the old extractor
-------------------------------
``dxf_extractor`` visited five entity types and discarded the rest, then
blacklisted the layers carrying the most semantic value. This reader inverts
both decisions: every modelspace entity is visited, and no layer is dropped.
Filtering happens later, by *role*, once there is enough information to filter
correctly.

Nothing is thrown away, so the cost of a mistake here is a low-confidence
record rather than an unrecoverable loss. That matters because a discarded
entity cannot be reconsidered when a later stage discovers it needed it.

Block references
----------------
An INSERT is recorded as a ``CadBlockRef`` — the semantic claim — *and*, when
its definition holds real geometry, optionally exploded so its lines
contribute to walls. Exploding is bounded: deeply nested or enormous blocks
are recorded but not exploded, because a title block containing a company
logo can hold thousands of segments that are geometry in name only.

Coordinate handling
-------------------
Every coordinate is scaled to metres and then translated by a single origin
offset computed from the *structural* geometry only. Annotation sitting far
off to the side of a drawing must not drag the building off-centre, which is
what would happen if the offset were computed over all entities.
"""

from __future__ import annotations

import math
import os
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import ezdxf
from ezdxf.entities import DXFEntity
from ezdxf.math import OCS, Vec3

from . import blocks as block_semantics
from . import layers as layer_semantics
from . import text as text_semantics
from . import units as unit_detection
from .schema import (
    CadBlockRef,
    CadDimension,
    CadDocument,
    CadHatch,
    CadLayer,
    CadPolyline,
    CadSegment,
    CadText,
    Source,
    make_uid,
)

Point = Tuple[float, float]

#: Curve tessellation resolution.
DEFAULT_ARC_SEGMENTS = 16

#: An INSERT whose definition explodes to more than this many entities is
#: recorded but not exploded. Title blocks and logos hit this; real
#: architectural blocks do not.
MAX_EXPLODE_ENTITIES = 400

#: How deep nested INSERTs are followed.
MAX_EXPLODE_DEPTH = 3

#: Segments shorter than this (metres) after scaling are drafting artefacts.
MIN_SEGMENT_LENGTH = 0.02


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def read(
    dxf_path: str,
    *,
    user_scale: Optional[float] = None,
    arc_segments: int = DEFAULT_ARC_SEGMENTS,
    normalise_origin: bool = True,
    explode_blocks: bool = True,
    log=print,
) -> CadDocument:
    """Parse a DXF file into a complete ``CadDocument``.

    Raises ``IOError`` / ``ezdxf.DXFStructureError`` for an unreadable file;
    everything short of that degrades to a warning on the document so a single
    malformed entity cannot abort a project.
    """
    started = time.time()
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    document = CadDocument(
        source_path=os.path.abspath(dxf_path),
        dxf_version=str(doc.dxfversion),
    )

    # ---- 1. Layer table --------------------------------------------------
    document.layers = _read_layers(doc, msp)
    roles = {layer.name.upper(): layer.role for layer in document.layers}

    # ---- 2. First pass in raw units, to learn the scale ------------------
    raw = _RawHarvest()
    _harvest(msp, raw, roles, arc_segments, explode_blocks, document.warnings, depth=0)

    extent = raw.extent()
    detected = unit_detection.resolve(
        insunits=_header(doc, "$INSUNITS"),
        extent=extent,
        dimension_measurements=raw.dimension_measurements,
        dimension_texts=raw.dimension_texts,
        user_scale=user_scale,
    )
    document.units = detected
    scale = detected.scale_to_m

    # ASCII only: this runs as a subprocess whose stdout is often cp1252 on
    # Windows, where a non-ASCII log line raises and takes the run with it.
    log(f"[CAD] Units: {detected.unit_name} (x{scale:g}) via {detected.method} "
        f"- {detected.reason}")

    # ---- 3. North --------------------------------------------------------
    document.north = unit_detection.resolve_north(
        north_direction=_header(doc, "$NORTHDIRECTION"),
        arrow_rotation=raw.north_arrow_rotation,
    )

    # ---- 4. Scale, then centre on the structural geometry ----------------
    offset = (0.0, 0.0)
    if normalise_origin:
        offset = _structural_offset(raw, scale)

    document.segments = [_place_segment(s, scale, offset) for s in raw.segments]
    document.polylines = [_place_polyline(p, scale, offset) for p in raw.polylines]
    document.blocks = [_place_block(b, scale, offset) for b in raw.blocks]
    document.texts = [_place_text(t, scale, offset) for t in raw.texts]
    document.dimensions = [_place_dimension(d, scale, offset) for d in raw.dimensions]
    document.hatches = [_place_hatch(h, scale, offset) for h in raw.hatches]
    document.origin_offset = offset

    # ---- 5. Drop drafting dust ------------------------------------------
    before = len(document.segments)
    document.segments = [
        s for s in document.segments if s.length >= MIN_SEGMENT_LENGTH
    ]
    dropped_tiny = before - len(document.segments)

    document.bounds_min, document.bounds_max = _bounds(document.segments)

    # ---- 6. Diagnostics --------------------------------------------------
    document.stats = {
        "elapsed_s": round(time.time() - started, 3),
        "layers": len(document.layers),
        "layer_roles": layer_semantics.summarise(
            {layer.name: layer_semantics.classify_layer(layer.name)
             for layer in document.layers}
        ),
        "segments": len(document.segments),
        "wall_segments": len(document.wall_segments()),
        "segments_dropped_tiny": dropped_tiny,
        "polylines": len(document.polylines),
        "closed_polylines": sum(1 for p in document.polylines if p.closed),
        "blocks": len(document.blocks),
        "block_categories": block_semantics.summarise(
            [
                block_semantics.BlockClassification(
                    b.category, b.kind, b.confidence, b.reason
                )
                for b in document.blocks
            ]
        ),
        "texts": len(document.texts),
        "room_labels": len(document.room_labels()),
        "text_roles": _histogram(t.role for t in document.texts),
        "dimensions": len(document.dimensions),
        "hatches": len(document.hatches),
        "entity_types": dict(raw.entity_counts),
        "skipped_types": dict(raw.skipped),
    }

    log(f"[CAD] {len(document.segments)} segments "
        f"({len(document.wall_segments())} walls), "
        f"{len(document.blocks)} blocks, {len(document.texts)} texts "
        f"({len(document.room_labels())} room labels), "
        f"{len(document.dimensions)} dimensions, {len(document.hatches)} hatches")

    return document


# ---------------------------------------------------------------------------
# Raw harvest (drawing units, pre-scale)
# ---------------------------------------------------------------------------


class _RawHarvest:
    """Accumulator for one traversal, holding coordinates in drawing units."""

    def __init__(self) -> None:
        self.segments: List[CadSegment] = []
        self.polylines: List[CadPolyline] = []
        self.blocks: List[CadBlockRef] = []
        self.texts: List[CadText] = []
        self.dimensions: List[CadDimension] = []
        self.hatches: List[CadHatch] = []
        self.entity_counts: Dict[str, int] = {}
        self.skipped: Dict[str, int] = {}
        self.dimension_measurements: List[float] = []
        self.dimension_texts: List[str] = []
        self.north_arrow_rotation: Optional[float] = None

    def count(self, dxftype: str) -> None:
        self.entity_counts[dxftype] = self.entity_counts.get(dxftype, 0) + 1

    def skip(self, dxftype: str) -> None:
        self.skipped[dxftype] = self.skipped.get(dxftype, 0) + 1

    def extent(self) -> Tuple[float, float]:
        """Raw bounding-box size of structural geometry, in drawing units."""
        xs: List[float] = []
        ys: List[float] = []
        for segment in self.segments:
            xs.extend((segment.start[0], segment.end[0]))
            ys.extend((segment.start[1], segment.end[1]))
        if not xs:
            for polyline in self.polylines:
                xs.extend(p[0] for p in polyline.points)
                ys.extend(p[1] for p in polyline.points)
        if not xs:
            return (0.0, 0.0)
        return (max(xs) - min(xs), max(ys) - min(ys))


def _harvest(
    entities: Iterable[DXFEntity],
    raw: _RawHarvest,
    roles: Dict[str, str],
    arc_segments: int,
    explode_blocks: bool,
    warnings: List[str],
    depth: int,
    inherited_layer: str = "",
) -> None:
    """Visit every entity, dispatching on type. Never raises."""
    for entity in entities:
        dxftype = entity.dxftype()
        raw.count(dxftype)
        try:
            layer = _layer_of(entity, inherited_layer)
            role = roles.get(layer.upper(), "unknown")

            if dxftype == "LINE":
                raw.segments.append(_read_line(entity, layer, role))
            elif dxftype == "LWPOLYLINE":
                _read_lwpolyline(entity, layer, role, raw)
            elif dxftype == "POLYLINE":
                _read_polyline(entity, layer, role, raw)
            elif dxftype == "ARC":
                raw.segments.extend(_read_arc(entity, layer, role, arc_segments))
            elif dxftype == "CIRCLE":
                raw.segments.extend(_read_circle(entity, layer, role, arc_segments))
            elif dxftype == "ELLIPSE":
                raw.segments.extend(_read_ellipse(entity, layer, role, arc_segments))
            elif dxftype in ("SPLINE",):
                raw.segments.extend(_read_spline(entity, layer, role, arc_segments))
            elif dxftype in ("TEXT", "MTEXT"):
                record = _read_text(entity, layer)
                if record is not None:
                    raw.texts.append(record)
            elif dxftype == "INSERT":
                _read_insert(
                    entity, layer, raw, roles, arc_segments, explode_blocks,
                    warnings, depth,
                )
            elif dxftype.startswith("DIMENSION") or dxftype == "DIMENSION":
                record = _read_dimension(entity, layer)
                if record is not None:
                    raw.dimensions.append(record)
                    raw.dimension_measurements.append(record.measurement)
                    raw.dimension_texts.append(record.text)
            elif dxftype == "HATCH":
                record = _read_hatch(entity, layer)
                if record is not None:
                    raw.hatches.append(record)
            elif dxftype == "SOLID":
                raw.segments.extend(_read_solid(entity, layer, role))
            elif dxftype in ("POINT", "ATTDEF", "VIEWPORT", "LEADER", "MLEADER",
                             "IMAGE", "WIPEOUT", "3DFACE", "MESH", "BODY",
                             "REGION", "RAY", "XLINE", "TOLERANCE", "SHAPE"):
                # Recognised but not semantically useful for floor plans.
                raw.skip(dxftype)
            else:
                raw.skip(dxftype)
        except Exception as exc:  # pragma: no cover - defensive
            # One malformed entity must never abort a project. Recording the
            # failure keeps it visible instead of silently losing geometry.
            warnings.append(f"{dxftype}: {exc}")
            raw.skip(dxftype)


# ---------------------------------------------------------------------------
# Object Coordinate System
# ---------------------------------------------------------------------------
#
# Most planar DXF entities (LWPOLYLINE, POLYLINE, ARC, CIRCLE, SOLID, TEXT)
# store their points in an *Object* Coordinate System defined by the entity's
# extrusion vector, not in world coordinates. When the extrusion is the
# default (0, 0, 1) the two coincide and the distinction is invisible, which
# is why it is so easy to miss.
#
# It stops being invisible the moment a block is mirrored. A mirrored INSERT
# is routinely written with extrusion (0, 0, -1) rather than a negative scale,
# which flips the OCS x-axis. Reading those vertices as world coordinates
# reflects the geometry through x = 0 — so a door at x = +23.8 m lands at
# x = -23.8 m, and the plan's bounding box silently doubles.
#
# Mirrored doors are near-universal in architectural drawings (one block,
# both hands), so this is not an edge case.


def _needs_ocs(entity) -> bool:
    """True when this entity's points must be transformed out of its OCS."""
    try:
        extrusion = entity.dxf.extrusion
    except (AttributeError, ValueError):
        return False
    if extrusion is None:
        return False
    # Only a non-default extrusion changes anything.
    return abs(extrusion[0]) > 1e-9 or abs(extrusion[1]) > 1e-9 or extrusion[2] < 0


def _ocs_of(entity) -> Optional[OCS]:
    try:
        return OCS(entity.dxf.extrusion)
    except (AttributeError, ValueError):
        return None


def _wcs(entity, points: Sequence[Point], elevation: float = 0.0) -> List[Point]:
    """Convert entity-local points to world coordinates when required."""
    if not _needs_ocs(entity):
        return [(float(x), float(y)) for x, y in points]

    ocs = _ocs_of(entity)
    if ocs is None:
        return [(float(x), float(y)) for x, y in points]

    converted = []
    for x, y in points:
        world = ocs.to_wcs(Vec3(float(x), float(y), elevation))
        converted.append((float(world.x), float(world.y)))
    return converted


def _wcs_point(entity, point) -> Point:
    """Convert one entity-local point to world coordinates."""
    return _wcs(entity, [(point[0], point[1])], _z_of(point))[0]


def _z_of(point) -> float:
    try:
        return float(point[2])
    except (IndexError, TypeError, KeyError):
        return 0.0


def _elevation_of(entity) -> float:
    """An OCS entity's elevation, which its 2D vertices are implicitly at."""
    try:
        return float(entity.dxf.elevation or 0.0)
    except (AttributeError, ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Entity readers
# ---------------------------------------------------------------------------


def _read_line(entity, layer: str, role: str) -> CadSegment:
    start = entity.dxf.start
    end = entity.dxf.end
    return CadSegment(
        uid=make_uid("seg", layer, start.x, start.y, end.x, end.y),
        dxftype="LINE", layer=layer, role=role,
        source=Source.CAD_LAYER if role != "unknown" else Source.CAD_GEOMETRY,
        start=(float(start.x), float(start.y)),
        end=(float(end.x), float(end.y)),
    )


def _read_lwpolyline(entity, layer: str, role: str, raw: _RawHarvest) -> None:
    points = _wcs(
        entity,
        [(x, y) for x, y in entity.get_points(format="xy")],
        _elevation_of(entity),
    )
    if len(points) < 2:
        return
    closed = bool(entity.closed)
    _emit_polyline(points, closed, "LWPOLYLINE", layer, role, raw)


def _read_polyline(entity, layer: str, role: str, raw: _RawHarvest) -> None:
    try:
        locations = [v.dxf.location for v in entity.vertices]
    except Exception:
        return
    points = _wcs(entity, [(p.x, p.y) for p in locations])
    if len(points) < 2:
        return
    _emit_polyline(points, bool(entity.is_closed), "POLYLINE", layer, role, raw)


def _emit_polyline(
    points: List[Point], closed: bool, dxftype: str, layer: str,
    role: str, raw: _RawHarvest,
) -> None:
    """Record the polyline whole *and* as segments.

    Both, deliberately. Closure is semantic — a closed polyline on a
    room-boundary layer states a room's extent exactly — and is destroyed by
    decomposition. Segments are what the wall extruder consumes. Keeping both
    costs a little memory and preserves information that cannot be recovered.
    """
    raw.polylines.append(
        CadPolyline(
            uid=make_uid("poly", layer, dxftype, len(points), points[0][0], points[0][1]),
            dxftype=dxftype, layer=layer, role=role,
            source=Source.CAD_LAYER if role != "unknown" else Source.CAD_GEOMETRY,
            points=points, closed=closed,
        )
    )

    ordered = points + [points[0]] if closed and len(points) >= 3 else points
    for a, b in zip(ordered, ordered[1:]):
        if a == b:
            continue
        raw.segments.append(
            CadSegment(
                uid=make_uid("seg", layer, a[0], a[1], b[0], b[1]),
                dxftype=dxftype, layer=layer, role=role,
                source=Source.CAD_LAYER if role != "unknown" else Source.CAD_GEOMETRY,
                start=a, end=b,
            )
        )


def _read_arc(entity, layer: str, role: str, count: int) -> List[CadSegment]:
    centre = entity.dxf.center
    radius = float(entity.dxf.radius)
    start_deg = float(entity.dxf.start_angle)
    end_deg = float(entity.dxf.end_angle)
    if end_deg <= start_deg:
        end_deg += 360.0
    # Tessellate in the entity's own frame, then transform: the sweep angles
    # are defined in the OCS, so converting the centre alone would rotate the
    # arc's endpoints into the wrong places on a mirrored entity.
    return _tessellate(
        entity, float(centre.x), float(centre.y), radius,
        math.radians(start_deg), math.radians(end_deg),
        count, "ARC", layer, role, _z_of(centre),
    )


def _read_circle(entity, layer: str, role: str, count: int) -> List[CadSegment]:
    centre = entity.dxf.center
    radius = float(entity.dxf.radius)
    return _tessellate(
        entity, float(centre.x), float(centre.y), radius, 0.0, 2 * math.pi,
        count, "CIRCLE", layer, role, _z_of(centre),
    )


def _tessellate(
    entity, cx: float, cy: float, radius: float, start: float, end: float,
    count: int, dxftype: str, layer: str, role: str, elevation: float = 0.0,
) -> List[CadSegment]:
    count = max(2, count)
    delta = (end - start) / count

    local = [
        (cx + radius * math.cos(start + delta * i),
         cy + radius * math.sin(start + delta * i))
        for i in range(count + 1)
    ]
    points = _wcs(entity, local, elevation)

    segments = []
    for p1, p2 in zip(points, points[1:]):
        segments.append(
            CadSegment(
                uid=make_uid("seg", layer, dxftype, p1[0], p1[1], p2[0], p2[1]),
                dxftype=dxftype, layer=layer, role=role, tessellated=True,
                source=Source.CAD_LAYER if role != "unknown" else Source.CAD_GEOMETRY,
                start=p1, end=p2,
            )
        )
    return segments


def _read_ellipse(entity, layer: str, role: str, count: int) -> List[CadSegment]:
    """Flatten an ELLIPSE via ezdxf's own vertex sampler."""
    try:
        points = [(float(p.x), float(p.y)) for p in entity.flattening(distance=0.01)]
    except Exception:
        return []
    return _chain(points, "ELLIPSE", layer, role)


def _read_spline(entity, layer: str, role: str, count: int) -> List[CadSegment]:
    """Flatten a SPLINE. Curved walls and furniture outlines are both common."""
    try:
        points = [(float(p.x), float(p.y)) for p in entity.flattening(distance=0.01)]
    except Exception:
        try:
            points = [(float(p[0]), float(p[1])) for p in entity.control_points]
        except Exception:
            return []
    return _chain(points, "SPLINE", layer, role)


def _read_solid(entity, layer: str, role: str) -> List[CadSegment]:
    """A filled triangle/quad — used for wall poché and small fixtures."""
    corners = []
    elevation = 0.0
    for attr in ("vtx0", "vtx1", "vtx3", "vtx2"):  # DXF winding is Z-shaped.
        try:
            vertex = getattr(entity.dxf, attr)
            corners.append((float(vertex.x), float(vertex.y)))
            elevation = _z_of(vertex)
        except Exception:
            continue
    if len(corners) < 3:
        return []
    corners = _wcs(entity, corners, elevation)
    return _chain(corners + [corners[0]], "SOLID", layer, role)


def _chain(points: Sequence[Point], dxftype: str, layer: str, role: str) -> List[CadSegment]:
    """Consecutive points into segments, skipping zero-length steps."""
    segments = []
    for a, b in zip(points, points[1:]):
        if a == b:
            continue
        segments.append(
            CadSegment(
                uid=make_uid("seg", layer, dxftype, a[0], a[1], b[0], b[1]),
                dxftype=dxftype, layer=layer, role=role, tessellated=True,
                source=Source.CAD_LAYER if role != "unknown" else Source.CAD_GEOMETRY,
                start=(a[0], a[1]), end=(b[0], b[1]),
            )
        )
    return segments


def _read_text(entity, layer: str) -> Optional[CadText]:
    """A TEXT or MTEXT, classified into a role by ``cad.text``."""
    dxftype = entity.dxftype()
    if dxftype == "MTEXT":
        raw_text = entity.text
        insert = entity.dxf.insert
        height = float(getattr(entity.dxf, "char_height", 0.0) or 0.0)
        rotation = float(getattr(entity.dxf, "rotation", 0.0) or 0.0)
    else:
        raw_text = entity.dxf.text
        # A justified TEXT carries its real position in `align_point`;
        # `insert` is (0,0) for centred text, which would otherwise drop the
        # label at the origin and detach it from the room it names.
        insert = entity.dxf.insert
        align = getattr(entity.dxf, "align_point", None)
        halign = int(getattr(entity.dxf, "halign", 0) or 0)
        valign = int(getattr(entity.dxf, "valign", 0) or 0)
        if align is not None and (halign or valign):
            insert = align
        height = float(getattr(entity.dxf, "height", 0.0) or 0.0)
        rotation = float(getattr(entity.dxf, "rotation", 0.0) or 0.0)

    cleaned = text_semantics.clean_text(raw_text)
    if not cleaned:
        return None

    classification = text_semantics.classify_text(raw_text)

    # A label often states the room's size, either as an area ("14.2 SQ.M.")
    # or as a dimension pair ("16'0\" X 15'9\""). Either way it is the
    # drawing's own claim about how big the room is, which is the only
    # independent check available on whether segmentation recovered that room
    # correctly. Derived here so the claim travels with the label.
    declared = classification.area_m2
    if declared is None and classification.dimensions:
        width, depth = classification.dimensions
        declared = width * depth

    # TEXT is an OCS entity (MTEXT is not); a mirrored one would otherwise
    # place its label on the wrong side of the plan, and a room label in the
    # wrong room is worse than no label at all.
    if dxftype == "TEXT":
        position = _wcs(entity, [(insert.x, insert.y)], _z_of(insert))[0]
    else:
        position = (float(insert.x), float(insert.y))

    return CadText(
        uid=make_uid("txt", layer, cleaned, position[0], position[1]),
        text=cleaned,
        normalised=text_semantics.normalise(raw_text),
        insert=position,
        height=height,
        rotation=rotation,
        layer=layer,
        dxftype=dxftype,
        role=classification.role,
        room_type=classification.room_type,
        value=declared,
        confidence=classification.confidence,
    )


def _read_insert(
    entity, layer: str, raw: _RawHarvest, roles: Dict[str, str],
    arc_segments: int, explode_blocks: bool, warnings: List[str], depth: int,
) -> None:
    """Record a block reference and, when useful, explode its geometry."""
    name = str(entity.dxf.name)
    # INSERT is an OCS entity: a mirrored block reference carries extrusion
    # (0, 0, -1), and its insert point must be transformed to match the
    # geometry that `virtual_entities` produces.
    insert_point = _wcs_point(entity, entity.dxf.insert)
    rotation = float(getattr(entity.dxf, "rotation", 0.0) or 0.0)
    xscale = float(getattr(entity.dxf, "xscale", 1.0) or 1.0)
    yscale = float(getattr(entity.dxf, "yscale", 1.0) or 1.0)

    attributes: Dict[str, str] = {}
    try:
        for attrib in entity.attribs:
            tag = str(attrib.dxf.tag).strip()
            value = text_semantics.clean_text(str(attrib.dxf.text))
            if tag:
                attributes[tag] = value
    except Exception:
        pass

    classification = block_semantics.classify_block(name, attributes)

    reference = CadBlockRef(
        uid=make_uid("blk", name, insert_point[0], insert_point[1], rotation),
        name=name,
        normalised=block_semantics.normalise_block_name(name),
        position=insert_point,
        rotation=rotation,
        scale=(xscale, yscale),
        layer=layer,
        attributes=attributes,
        category=classification.category,
        kind=classification.kind,
        confidence=classification.confidence,
        reason=classification.reason,
    )

    # A north arrow declares the drawing's orientation.
    if classification.kind == "north_arrow" and raw.north_arrow_rotation is None:
        raw.north_arrow_rotation = rotation

    # Attributes naming a room become text records in their own right: an
    # explicit ROOM_NAME attribute is structured metadata (tier 1), stronger
    # than a free-floating string that merely sits inside the polygon.
    room_name = block_semantics.room_name_from_attributes(attributes)
    if room_name:
        parsed = text_semantics.classify_text(room_name)
        if parsed.role == "room_label":
            raw.texts.append(
                CadText(
                    uid=make_uid("attr", name, room_name, insert_point[0], insert_point[1]),
                    text=room_name,
                    normalised=text_semantics.normalise(room_name),
                    insert=insert_point,
                    layer=layer,
                    dxftype="ATTRIB",
                    attrib_tag="ROOM_NAME",
                    owner_block=name,
                    role="room_label",
                    room_type=parsed.room_type,
                    value=parsed.area_m2,
                    # Higher than a loose TEXT: this is structured metadata.
                    confidence=0.97,
                    source=Source.CAD_METADATA,
                )
            )

    # Explode to recover geometry and the block's real footprint.
    child_start = len(raw.segments)
    if explode_blocks and depth < MAX_EXPLODE_DEPTH:
        try:
            virtual = list(entity.virtual_entities())
        except Exception as exc:
            virtual = []
            warnings.append(f"INSERT {name}: cannot explode ({exc})")

        if len(virtual) > MAX_EXPLODE_ENTITIES:
            raw.skip(f"INSERT:{name}(too-large)")
        elif virtual:
            _harvest(
                virtual, raw, roles, arc_segments, explode_blocks, warnings,
                depth + 1, inherited_layer=layer,
            )

    # The exploded children give the block its true extent, which the semantic
    # layer needs to tell a wardrobe from a wall-mounted switch.
    children = raw.segments[child_start:]
    if children:
        xs = [v for s in children for v in (s.start[0], s.end[0])]
        ys = [v for s in children for v in (s.start[1], s.end[1])]
        reference.bounds_min = (min(xs), min(ys))
        reference.bounds_max = (max(xs), max(ys))
    else:
        reference.bounds_min = reference.position
        reference.bounds_max = reference.position

    # Geometry belonging to furniture and annotation blocks must not be
    # mistaken for walls, whatever layer it happens to sit on. The block's own
    # classification is the more specific statement, so it wins.
    if classification.kind in (
        "furniture", "casework", "appliance", "plumbing_fixture",
        "kitchen_fixture", "annotation", "north_arrow", "grid_bubble",
        "title_block", "electrical",
    ):
        for segment in children:
            segment.role = _role_for_block_kind(classification.kind)
            segment.source = Source.CAD_BLOCK
            segment.reason = f"geometry belongs to block {name!r}"

    raw.blocks.append(reference)


def _role_for_block_kind(kind: str) -> str:
    """Segment role implied by the kind of block the geometry came from."""
    return {
        "plumbing_fixture": "plumbing_fixture",
        "kitchen_fixture": "casework",
        "appliance": "appliance",
        "casework": "casework",
        "furniture": "furniture",
        "electrical": "electrical",
        "north_arrow": "annotation",
        "grid_bubble": "grid",
        "title_block": "title_block",
        "annotation": "annotation",
    }.get(kind, "furniture")


def _read_dimension(entity, layer: str) -> Optional[CadDimension]:
    """A DIMENSION, keeping both its measurement and its printed text."""
    try:
        measurement = float(entity.get_measurement())
    except Exception:
        measurement = 0.0
    if isinstance(measurement, (tuple, list)):  # angular dims return vectors
        return None

    printed = text_semantics.clean_text(str(getattr(entity.dxf, "text", "") or ""))
    # "<>" and "" both mean "print the measured value".
    if printed in ("<>", ""):
        printed = ""

    try:
        midpoint = entity.dxf.defpoint
        position = (float(midpoint.x), float(midpoint.y))
    except Exception:
        position = (0.0, 0.0)

    return CadDimension(
        uid=make_uid("dim", layer, measurement, position[0], position[1]),
        text=printed,
        measurement=abs(measurement),
        position=position,
        layer=layer,
        dxftype=entity.dxftype(),
        kind=_dimension_kind(entity),
    )


def _dimension_kind(entity) -> str:
    try:
        code = int(entity.dimtype) & 7
    except Exception:
        return "linear"
    return {
        0: "linear", 1: "aligned", 2: "angular", 3: "diameter",
        4: "radial", 5: "angular", 6: "ordinate",
    }.get(code, "linear")


def _read_hatch(entity, layer: str) -> Optional[CadHatch]:
    """A HATCH, with its outer boundary recovered where possible."""
    pattern = str(getattr(entity.dxf, "pattern_name", "") or "")
    solid = bool(getattr(entity.dxf, "solid_fill", 0))

    boundary: List[Point] = []
    try:
        for path in entity.paths:
            vertices = getattr(path, "vertices", None)
            if vertices:
                boundary = _wcs(
                    entity, [(v[0], v[1]) for v in vertices], _elevation_of(entity)
                )
                break
    except Exception:
        boundary = []

    area = 0.0
    centroid = (0.0, 0.0)
    if len(boundary) >= 3:
        total = 0.0
        for i, (x0, y0) in enumerate(boundary):
            x1, y1 = boundary[(i + 1) % len(boundary)]
            total += x0 * y1 - x1 * y0
        area = abs(total) / 2.0
        centroid = (
            sum(p[0] for p in boundary) / len(boundary),
            sum(p[1] for p in boundary) / len(boundary),
        )

    return CadHatch(
        uid=make_uid("hatch", layer, pattern, centroid[0], centroid[1]),
        pattern=pattern,
        layer=layer,
        boundary=boundary,
        area=area,
        centroid=centroid,
        solid=solid,
        material=_material_from_pattern(pattern),
        confidence=0.6 if pattern else 0.0,
    )


#: Standard AutoCAD hatch patterns and the material they conventionally denote.
_HATCH_MATERIALS = {
    "ANSI31": "concrete", "ANSI32": "steel", "ANSI33": "bronze",
    "ANSI37": "insulation", "AR-CONC": "concrete", "AR-B816": "brick",
    "AR-BRSTD": "brick", "AR-HBONE": "wood", "AR-PARQ1": "wood",
    "AR-RROOF": "roofing", "AR-SAND": "sand", "BRICK": "brick",
    "CONCRETE": "concrete", "EARTH": "earth", "GRAVEL": "gravel",
    "HONEY": "tile", "NET": "tile", "STEEL": "steel", "SOLID": "solid",
    "DOTS": "carpet", "GRASS": "landscape", "WOOD": "wood",
}


def _material_from_pattern(pattern: str) -> str:
    if not pattern:
        return ""
    upper = pattern.strip().upper()
    if upper in _HATCH_MATERIALS:
        return _HATCH_MATERIALS[upper]
    for key, material in _HATCH_MATERIALS.items():
        if key in upper:
            return material
    return ""


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------


def _read_layers(doc, msp) -> List[CadLayer]:
    """Layer table entries, classified, with modelspace entity counts."""
    counts: Dict[str, int] = {}
    for entity in msp:
        try:
            name = str(entity.dxf.layer)
        except Exception:
            continue
        counts[name] = counts.get(name, 0) + 1

    records: List[CadLayer] = []
    seen = set()

    for layer in doc.layers:
        name = str(layer.dxf.name)
        seen.add(name.upper())
        classification = layer_semantics.classify_layer(name)
        records.append(
            CadLayer(
                name=name,
                color=int(getattr(layer.dxf, "color", 7) or 7),
                linetype=str(getattr(layer.dxf, "linetype", "CONTINUOUS")),
                frozen=bool(layer.is_frozen()),
                off=bool(layer.is_off()),
                locked=bool(layer.is_locked()),
                entity_count=counts.get(name, 0),
                role=classification.role,
                confidence=classification.confidence,
                reason=classification.reason,
                convention=classification.convention,
            )
        )

    # Entities can reference layers with no table entry in malformed files.
    for name, count in counts.items():
        if name.upper() in seen:
            continue
        classification = layer_semantics.classify_layer(name)
        records.append(
            CadLayer(
                name=name, entity_count=count, role=classification.role,
                confidence=classification.confidence, reason=classification.reason,
                convention=classification.convention,
            )
        )

    return sorted(records, key=lambda x: (-x.entity_count, x.name))


# ---------------------------------------------------------------------------
# Placement into metric, origin-centred space
# ---------------------------------------------------------------------------


def _structural_offset(raw: _RawHarvest, scale: float) -> Point:
    """Centre on structural geometry only.

    Annotation, title blocks and legends routinely sit far outside the
    building. Including them in the centring calculation pushes the building
    off the origin by an arbitrary amount, which is precisely the bug that
    makes a model appear "somewhere off in space" when opened.
    """
    structural = [s for s in raw.segments if s.role in _STRUCTURAL_ROLES]
    if not structural:
        structural = [s for s in raw.segments if s.role not in _NON_BUILDING_ROLES]
    if not structural:
        structural = raw.segments
    if not structural:
        return (0.0, 0.0)

    xs = [v for s in structural for v in (s.start[0], s.end[0])]
    ys = [v for s in structural for v in (s.start[1], s.end[1])]
    cx = (min(xs) + max(xs)) / 2.0 * scale
    cy = (min(ys) + max(ys)) / 2.0 * scale
    return (cx, cy)


def _t(point: Point, scale: float, offset: Point) -> Point:
    return (
        round(point[0] * scale - offset[0], 6),
        round(point[1] * scale - offset[1], 6),
    )


def _place_segment(segment: CadSegment, scale: float, offset: Point) -> CadSegment:
    segment.start = _t(segment.start, scale, offset)
    segment.end = _t(segment.end, scale, offset)
    return segment


def _place_polyline(polyline: CadPolyline, scale: float, offset: Point) -> CadPolyline:
    polyline.points = [_t(p, scale, offset) for p in polyline.points]
    return polyline


def _place_block(block: CadBlockRef, scale: float, offset: Point) -> CadBlockRef:
    block.position = _t(block.position, scale, offset)
    block.bounds_min = _t(block.bounds_min, scale, offset)
    block.bounds_max = _t(block.bounds_max, scale, offset)
    return block


def _place_text(text: CadText, scale: float, offset: Point) -> CadText:
    text.insert = _t(text.insert, scale, offset)
    text.height = text.height * scale
    return text


def _place_dimension(dim: CadDimension, scale: float, offset: Point) -> CadDimension:
    dim.position = _t(dim.position, scale, offset)
    dim.metres = dim.measurement * scale
    return dim


def _place_hatch(hatch: CadHatch, scale: float, offset: Point) -> CadHatch:
    hatch.boundary = [_t(p, scale, offset) for p in hatch.boundary]
    hatch.centroid = _t(hatch.centroid, scale, offset)
    hatch.area = hatch.area * scale * scale
    return hatch


def _bounds(segments: Sequence[CadSegment]) -> Tuple[Point, Point]:
    """The *building's* extent, not the drawing's.

    Restricted to structural geometry for the same reason ``_structural_offset``
    is: a title block, a legend or a stray construction line on DEFPOINTS sits
    tens of metres from the building, and including it reports an apartment as
    a 60-metre structure. That figure propagates into ``geometry.json``
    metadata and into every consumer that sizes a camera or a ground plane
    from it.
    """
    structural = [s for s in segments if s.role in _STRUCTURAL_ROLES]
    if not structural:
        structural = [s for s in segments if s.role not in _NON_BUILDING_ROLES]
    if not structural:
        structural = list(segments)
    if not structural:
        return (0.0, 0.0), (0.0, 0.0)

    xs = [v for s in structural for v in (s.start[0], s.end[0])]
    ys = [v for s in structural for v in (s.start[1], s.end[1])]
    return (min(xs), min(ys)), (max(xs), max(ys))


#: Roles that make up the physical building.
_STRUCTURAL_ROLES = (
    "wall", "column", "door", "window", "beam", "room_boundary", "stair",
)

#: Roles that are drawing apparatus rather than building.
_NON_BUILDING_ROLES = (
    "title_block", "annotation", "text", "dimension", "grid", "construction",
    "site",
)


def _header(doc, name: str):
    try:
        value = doc.header.get(name)
    except Exception:
        return None
    return value


def _layer_of(entity, inherited: str) -> str:
    """An entity's layer, resolving the ``BYBLOCK`` layer ``0`` convention.

    Geometry inside a block definition is conventionally drawn on layer ``0``
    so it inherits the INSERT's layer. Honouring that is what lets a door
    block placed on ``A-DOOR`` be recognised as a door.
    """
    try:
        layer = str(entity.dxf.layer)
    except Exception:
        return inherited or "0"
    if layer in ("0", "") and inherited:
        return inherited
    return layer


def _histogram(values: Iterable[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))
