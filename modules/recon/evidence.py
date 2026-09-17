"""
ArchX3D — The CAD evidence bridge
=================================
Carries what the drawing *says* — entities, layers, blocks, transforms, text —
from the one DXF reader to every stage after reconstruction, so no later stage
ever opens the DXF again.

Why a bridge rather than a second reader
----------------------------------------
The semantic room classifier and the vision stage need evidence the wall
model does not keep: which block a toilet is, what a room label says and
where it sits, what a block's ``ROOM_NAME`` attribute holds, which hatch
pattern fills a space. Before this module that evidence came from
``cad.reader``, an independent DXF parser with its own unit detection and its
own origin. Two readers of one file eventually disagree — about units first,
and then about where everything is — and a room label in one frame names the
wrong room in the other.

So there is one reader (:mod:`modules.recon.read`), and this module *projects*
its records into the two shapes downstream stages consume:

* :func:`to_cad_document` — the :class:`cad.schema.CadDocument` the semantic
  tier was written against, in the reconstruction's own frame and units. Its
  interpretation of strings, block names and layer names still comes from
  ``cad.text``, ``cad.blocks`` and ``cad.layers``; only the geometry and the
  parsing moved.
* :func:`source_evidence` — a structured, id-addressed record of every entity
  the model was built from (``entity_id``, ``entity_type``, ``layer``,
  ``block``, ``transform``, ``geometry``) and, for every wall, room and
  opening, the entity ids that are its evidence. A consumer that wants to know
  *why* there is a door somewhere follows the ids; it does not re-derive them.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import classify as C
from .ir import Drawing
from .read import CadDrawing

EVIDENCE_SCHEMA = "archx3d.evidence/1.0"

XY = Tuple[float, float]


def _cad():
    """The ``cad`` package, whichever way the modules directory is on the path."""
    try:
        from cad import blocks, layers, schema, text  # type: ignore
    except ImportError:  # pragma: no cover - depends on how we were imported
        from modules.cad import blocks, layers, schema, text  # type: ignore
    return schema, text, blocks, layers


#: Segment roles worth carrying into the semantic document. Everything else —
#: hatching, landscaping, construction lines — is evidence of nothing a room
#: classifier can use, and carrying it made a large site plan's document tens
#: of megabytes.
_SEGMENT_ROLES = frozenset({
    "wall", "door", "window", "opening", "plumbing_fixture", "casework",
    "appliance", "furniture", "stair", "plumbing", "column",
})

_BLOCK_KIND_ROLE = {
    "plumbing_fixture": "plumbing_fixture", "kitchen_fixture": "casework",
    "appliance": "appliance", "casework": "casework", "furniture": "furniture",
    "electrical": "electrical", "north_arrow": "annotation",
    "grid_bubble": "grid", "title_block": "title_block", "annotation": "annotation",
}


def _hatch_material(pattern: str) -> str:
    table = {
        "ANSI31": "concrete", "ANSI32": "steel", "ANSI33": "bronze",
        "ANSI37": "insulation", "AR-CONC": "concrete", "AR-B816": "brick",
        "AR-BRSTD": "brick", "AR-HBONE": "wood", "AR-PARQ1": "wood",
        "AR-RROOF": "roofing", "AR-SAND": "sand", "BRICK": "brick",
        "CONCRETE": "concrete", "EARTH": "earth", "GRAVEL": "gravel",
        "HONEY": "tile", "NET": "tile", "STEEL": "steel", "SOLID": "solid",
        "DOTS": "carpet", "GRASS": "landscape", "WOOD": "wood",
    }
    upper = (pattern or "").strip().upper()
    if not upper:
        return ""
    if upper in table:
        return table[upper]
    return next((m for k, m in table.items() if k in upper), "")


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
# The semantic document
# ---------------------------------------------------------------------------

def to_cad_document(cad: CadDrawing):
    """The drawing as a ``cad.schema.CadDocument``, from the one reader."""
    schema, text_sem, block_sem, layer_sem = _cad()
    S = schema

    units = cad.units
    doc = S.CadDocument(
        source_path=cad.source_path,
        dxf_version=cad.dxf_version,
        units=S.DrawingUnits(
            scale_to_m=units.scale_to_m if units else 1.0,
            unit_name=units.unit_name if units else "unknown",
            insunits=cad.insunits,
            method=units.method if units else "heuristic",
            confidence=units.confidence if units else 0.0,
            reason=units.reason if units else "",
        ),
        north=S.NorthArrow(heading_deg=cad.north_deg, source=S.Source.CAD_METADATA,
                           confidence=0.9, reason="$NORTHDIRECTION"),
        origin_offset=cad.origin_offset,
        bounds_min=cad.bounds_min, bounds_max=cad.bounds_max,
        warnings=list(cad.warnings),
    )

    layer_class = {}
    counts: Dict[str, int] = dict(cad.layer_counts)
    for name, n in cad.insert_counts.items():
        counts[name] = counts.get(name, 0) + n
    for name in cad.layer_table:
        counts.setdefault(name, 0)
    for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lc = layer_sem.classify_layer(name)
        layer_class[name] = lc
        state = cad.layer_table.get(name, {})
        doc.layers.append(S.CadLayer(
            name=name, entity_count=count, role=lc.role, confidence=lc.confidence,
            reason=lc.reason, convention=lc.convention,
            off=bool(state.get("off")), frozen=bool(state.get("frozen"))))

    block_class = {}

    def block_of(name: Optional[str], attributes=None):
        if not name:
            return None
        key = (name, tuple(sorted((attributes or {}).items())))
        if key not in block_class:
            block_class[key] = block_sem.classify_block(name, attributes or {})
        return block_class[key]

    for ins in cad.inserts:
        cls = block_of(ins.name, ins.attributes)
        x0, y0, x1, y1 = ins.extents or (ins.point[0], ins.point[1],
                                         ins.point[0], ins.point[1])
        doc.blocks.append(S.CadBlockRef(
            uid=S.make_uid("blk", ins.name, round(ins.point[0], 4),
                           round(ins.point[1], 4), ins.rotation),
            name=ins.name, normalised=block_sem.normalise_block_name(ins.name),
            position=ins.point, rotation=ins.rotation,
            scale=(ins.xscale, ins.yscale), layer=ins.layer,
            attributes={k: text_sem.clean_text(v) for k, v in ins.attributes.items()},
            bounds_min=(x0, y0), bounds_max=(x1, y1),
            category=cls.category, kind=cls.kind, confidence=cls.confidence,
            reason=cls.reason))
        room_name = block_sem.room_name_from_attributes(ins.attributes)
        if room_name:
            parsed = text_sem.classify_text(room_name)
            if parsed.role == "room_label":
                doc.texts.append(S.CadText(
                    uid=S.make_uid("attr", ins.name, room_name,
                                   round(ins.point[0], 4), round(ins.point[1], 4)),
                    text=room_name, normalised=text_sem.normalise(room_name),
                    insert=ins.point, layer=ins.layer, dxftype="ATTRIB",
                    attrib_tag="ROOM_NAME", owner_block=ins.name,
                    role="room_label", room_type=parsed.room_type,
                    value=parsed.area_m2, confidence=0.97,
                    source=S.Source.CAD_METADATA))

    for t in cad.labels:
        cleaned = text_sem.clean_text(t.text)
        if not cleaned:
            continue
        cls = text_sem.classify_text(t.text)
        declared = cls.area_m2
        if declared is None and cls.dimensions:
            declared = cls.dimensions[0] * cls.dimensions[1]
        pos = t.anchor or t.point
        doc.texts.append(S.CadText(
            uid=S.make_uid("txt", t.layer, cleaned, round(pos[0], 4), round(pos[1], 4)),
            text=cleaned, normalised=text_sem.normalise(t.text), insert=pos,
            height=t.height, rotation=t.rotation, layer=t.layer,
            role=cls.role, room_type=cls.room_type, value=declared,
            confidence=cls.confidence))

    scale = units.scale_to_m if units else 1.0
    for d in cad.dimension_refs:
        doc.dimensions.append(S.CadDimension(
            uid=S.make_uid("dim", d.layer, round(d.measurement, 6),
                           round(d.position[0], 4), round(d.position[1], 4)),
            text=text_sem.clean_text(d.text), measurement=d.measurement,
            metres=d.measurement * scale, position=d.position, layer=d.layer,
            kind=d.kind))

    for h in cad.hatches:
        area = _ring_area(h.boundary)
        centroid = ((sum(p[0] for p in h.boundary) / len(h.boundary),
                     sum(p[1] for p in h.boundary) / len(h.boundary))
                    if h.boundary else (0.0, 0.0))
        doc.hatches.append(S.CadHatch(
            uid=S.make_uid("hatch", h.layer, h.pattern, round(centroid[0], 4),
                           round(centroid[1], 4)),
            pattern=h.pattern, layer=h.layer, boundary=list(h.boundary),
            area=area, centroid=centroid, solid=h.solid,
            material=_hatch_material(h.pattern),
            confidence=0.6 if h.pattern else 0.0))

    for p in cad.prims:
        lc = layer_class.get(p.layer)
        role = lc.role if lc is not None else "unknown"
        source = S.Source.CAD_LAYER if role != "unknown" else S.Source.CAD_GEOMETRY
        reason = ""
        bcls = block_of(p.block)
        if bcls is not None and bcls.kind in _BLOCK_KIND_ROLE:
            role = _BLOCK_KIND_ROLE[bcls.kind]
            source = S.Source.CAD_BLOCK
            reason = "geometry belongs to block %r" % p.block
        if role not in _SEGMENT_ROLES:
            continue
        if p.closed and len(p.points) >= 3:
            doc.polylines.append(S.CadPolyline(
                uid=p.id, dxftype=p.dxftype, layer=p.layer, source=source,
                confidence=p.confidence, reason=reason, points=list(p.points),
                closed=True, role=role))
        for a, b in p.segments:
            if math.dist(a, b) < 0.005:
                continue
            doc.segments.append(S.CadSegment(
                uid="%s:%d" % (p.id, len(doc.segments)), dxftype=p.dxftype,
                layer=p.layer, source=source, confidence=p.confidence,
                reason=reason, start=a, end=b, role=role,
                tessellated=p.dxftype in ("ARC", "CIRCLE", "ELLIPSE", "SPLINE")))

    doc.stats = {
        "reader": "recon.read",
        "layers": len(doc.layers),
        "layer_roles": _histogram(l.role for l in doc.layers),
        "segments": len(doc.segments),
        "wall_segments": len(doc.wall_segments()),
        "polylines": len(doc.polylines),
        "closed_polylines": len(doc.polylines),
        "blocks": len(doc.blocks),
        "block_categories": _histogram(b.category for b in doc.blocks if b.category),
        "texts": len(doc.texts),
        "room_labels": len(doc.room_labels()),
        "text_roles": _histogram(t.role for t in doc.texts),
        "dimensions": len(doc.dimensions),
        "hatches": len(doc.hatches),
    }
    return doc


def _histogram(values: Iterable[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


# ---------------------------------------------------------------------------
# Querying the evidence
# ---------------------------------------------------------------------------

class EvidenceIndex:
    """Id- and place-addressed lookups over a ``source_evidence`` document.

    Works on the plain dict as written into ``building.json`` and
    ``geometry.json``, so a stage that only has the JSON — including one
    running in Blender's interpreter — can ask *why* an element exists
    without importing the reader or opening the DXF.
    """

    def __init__(self, evidence: Dict[str, object]):
        self.evidence = evidence or {}
        self.entities: Dict[str, dict] = {
            e["entity_id"]: e for e in self.evidence.get("entities", [])}
        self.links: Dict[str, Dict[str, List[str]]] = dict(self.evidence.get("links", {}))

    def cited_by(self, element_id: str) -> List[dict]:
        """The entities a wall, room or opening was built from."""
        for table in self.links.values():
            if element_id in table:
                return [self.entities[i] for i in table[element_id] if i in self.entities]
        return []

    def citing(self, entity_id: str) -> List[str]:
        """The elements built from an entity."""
        return sorted(eid for table in self.links.values()
                      for eid, ids in table.items() if entity_id in ids)

    def near(self, point: XY, radius: float,
             types: Optional[Sequence[str]] = None) -> List[dict]:
        """Entities whose geometry lies within ``radius`` of ``point``."""
        want = set(types) if types else None
        out = []
        for e in self.entities.values():
            if want and e.get("entity_type") not in want:
                continue
            g = e.get("geometry") or {}
            p = g.get("point") or g.get("centre") or \
                (e.get("transform") or {}).get("insert")
            box = g.get("bbox")
            if p is not None:
                d = math.dist(point, p)
            elif box:
                dx = max(box[0] - point[0], 0.0, point[0] - box[2])
                dy = max(box[1] - point[1], 0.0, point[1] - box[3])
                d = math.hypot(dx, dy)
            else:
                continue
            if d <= radius:
                out.append(e)
        return out


# ---------------------------------------------------------------------------
# Structured source evidence
# ---------------------------------------------------------------------------

def _bbox(points: Sequence[XY]) -> List[float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def source_evidence(cad: CadDrawing, model: Drawing) -> Dict[str, object]:
    """Every entity the model rests on, and what rests on each.

    Entities are recorded once, by the reader's id. Primitives are included
    when some wall or opening cites them — the whole sheet's line work would
    be megabytes of evidence for nothing — while every block reference, text,
    arc, hatch and dimension is included, because those are what semantic
    and vision stages search.
    """
    cited = set()
    links: Dict[str, Dict[str, List[str]]] = {"walls": {}, "rooms": {}, "openings": {}}
    for level in model.levels():
        for w in level.walls:
            links["walls"][w.id] = list(w.source_ids)
            cited.update(w.source_ids)
        for r in level.rooms:
            links["rooms"][r.id] = list(r.source_ids)
            cited.update(r.source_ids)
        for o in level.openings:
            links["openings"][o.id] = list(o.source_ids)
            cited.update(o.source_ids)

    entities: List[Dict[str, object]] = []
    for p in cad.prims:
        if p.id not in cited:
            continue
        entities.append({
            "entity_id": p.id, "entity_type": p.dxftype, "layer": p.layer,
            "block": p.block, "role": p.role, "confidence": round(p.confidence, 3),
            "classification": p.reason,
            "geometry": {"kind": "polyline", "closed": p.closed,
                         "bbox": _bbox(p.points), "points": len(p.points),
                         "length_m": round(p.length, 4)},
        })
    for a in cad.arcs:
        entities.append({
            "entity_id": a.id, "entity_type": "ARC", "layer": a.layer,
            "block": a.block, "role": a.role,
            "geometry": {"kind": "arc",
                         "centre": [round(a.centre[0], 4), round(a.centre[1], 4)],
                         "radius": round(a.radius, 4),
                         "start_deg": round(a.start_deg, 3),
                         "end_deg": round(a.end_deg, 3)},
        })
    for i in cad.inserts:
        entities.append({
            "entity_id": i.id, "entity_type": "INSERT", "layer": i.layer,
            "block": i.name, "parent_block": i.parent, "role": i.role,
            "transform": {"insert": [round(i.point[0], 4), round(i.point[1], 4)],
                          "rotation_deg": round(i.rotation, 3),
                          "scale": [round(i.xscale, 4), round(i.yscale, 4)]},
            "attributes": dict(i.attributes),
            "geometry": {"kind": "extents",
                         "bbox": [round(v, 4) for v in i.extents] if i.extents else None},
        })
    for t in cad.labels:
        entities.append({
            "entity_id": t.id, "entity_type": "TEXT", "layer": t.layer,
            "text": t.text, "height": round(t.height, 4),
            "geometry": {"kind": "text",
                         "point": [round(t.point[0], 4), round(t.point[1], 4)],
                         "bbox": [round(v, 4) for v in t.extent] if t.extent else None},
        })
    for h in cad.hatches:
        entities.append({
            "entity_id": h.id, "entity_type": "HATCH", "layer": h.layer,
            "block": h.block, "pattern": h.pattern, "solid": h.solid,
            "geometry": {"kind": "region",
                         "bbox": _bbox(h.boundary) if h.boundary else None,
                         "area_m2": round(_ring_area(h.boundary), 4)},
        })
    for d in cad.dimension_refs:
        entities.append({
            "entity_id": d.id, "entity_type": "DIMENSION", "layer": d.layer,
            "text": d.text, "measurement": round(d.measurement, 6),
            "geometry": {"kind": "point",
                         "point": [round(d.position[0], 4), round(d.position[1], 4)]},
        })

    return {
        "schema": EVIDENCE_SCHEMA,
        "reader": "modules.recon.read",
        "frame": {"units": "metres", "origin_offset": list(cad.origin_offset),
                  "scale_to_m": cad.units.scale_to_m if cad.units else 1.0},
        "entities": entities,
        "links": links,
        "counts": {
            "entities": len(entities),
            "cited_primitives": sum(1 for e in entities
                                    if e["entity_type"] not in
                                    ("ARC", "INSERT", "TEXT", "HATCH", "DIMENSION")),
            "inserts": len(cad.inserts), "labels": len(cad.labels),
            "arcs": len(cad.arcs), "hatches": len(cad.hatches),
            "dimensions": len(cad.dimension_refs),
        },
    }
