"""
ArchX3D — ``geometry.json`` from the architectural model
========================================================
Emits the legacy document that the vision, furnishing, evaluation and viewer
stages read, derived from the validated :class:`~modules.recon.ir.Drawing`
rather than from a second pass over the DXF.

Why derive it rather than parse twice
-------------------------------------
Two independent readers of one drawing will eventually disagree, and the
disagreement that matters most is scale. The old extractor resolved the failing
plan as millimetres and the new engine resolves it as inches; running both
would leave ``building.json`` and ``geometry.json`` describing buildings 25
times different in size, in the same project directory, for different stages to
pick up. So there is one reader, one unit decision, one frame — and this file
is a projection of it.

What the projection keeps
-------------------------
``walls`` are wall **centrelines** with a thickness. ``rooms`` and
``openings`` are the reconstruction's own, and a 2D consumer uses them as they
are; it never segments rooms or looks for openings again.

Every storey of every building is projected **as drawn on the sheet**, so 2D
stages see plans that do not overlap. Each wall, room and opening carries its
``building_id`` and ``level_id``, and ``metadata.levels`` carries each storey's
``placement`` and ``elevation`` — the only two numbers the 3D stage needs to
stack them, which it applies to anything a 2D stage placed in a room.
"""

from __future__ import annotations

import os
from typing import Dict, List

from .ir import Drawing


def to_geometry_json(drawing: Drawing) -> dict:
    """The legacy document, projected from the architectural model."""
    walls: List[dict] = []
    rooms: List[dict] = []
    openings: List[dict] = []
    footprint_parts: List[list] = []
    levels_meta: List[dict] = []

    for b in drawing.buildings:
        for l in b.levels:
            levels_meta.append({
                "id": l.id, "building_id": b.id, "building": b.name,
                "name": l.name, "index": l.index,
                "elevation": round(l.elevation, 4), "height": l.height,
                "placement": [round(l.placement[0], 4), round(l.placement[1], 4)],
                "title": l.title, "designation": l.designation,
                "bounds_min": [round(v, 4) for v in l.bounds_min],
                "bounds_max": [round(v, 4) for v in l.bounds_max],
            })
            for w in l.walls:
                walls.append({
                    "start": [round(w.start[0], 4), round(w.start[1], 4)],
                    "end": [round(w.end[0], 4), round(w.end[1], 4)],
                    "source_entity": "WALL",
                    "layer": w.layer or "WALLS",
                    "thickness": round(w.thickness, 4),
                    "kind": w.kind,
                    "wall_id": w.id,
                    "building_id": b.id,
                    "level_id": l.id,
                })
            for r in l.rooms:
                rooms.append({
                    "id": r.id,
                    "label": r.label,
                    "room_type": r.room_type,
                    "area": round(r.area, 3),
                    "centroid": [round(r.centroid[0], 4), round(r.centroid[1], 4)],
                    "polygon": [[round(x, 4), round(y, 4)] for x, y in r.polygon],
                    "holes": [[[round(x, 4), round(y, 4)] for x, y in h] for h in r.holes],
                    "is_exterior": r.is_exterior,
                    "label_confidence": round(r.label_confidence, 3),
                    "boundary_wall_ids": list(r.boundary_wall_ids),
                    "neighbour_ids": list(r.neighbour_ids),
                    "opening_ids": list(r.opening_ids),
                    "building_id": b.id,
                    "level_id": l.id,
                })
            for o in l.openings:
                openings.append({
                    "id": o.id, "kind": o.kind, "wall_id": o.wall_id,
                    "position": [round(o.position[0], 4), round(o.position[1], 4)],
                    "offset": round(o.offset, 4),
                    "width": round(o.width, 4), "height": round(o.height, 4),
                    "sill_height": round(o.sill_height, 4),
                    "confidence": round(o.confidence, 3),
                    "classification": o.classification,
                    "evidence": o.evidence,
                    "rooms": list(o.rooms),
                    "building_id": b.id,
                    "level_id": l.id,
                })
            footprint_parts.extend(
                [[round(x, 4), round(y, 4)] for x, y in ring]
                for ring in (l.footprint_parts or ([l.footprint] if l.footprint else [])))

    units = drawing.units
    metadata = {
        "source": drawing.source_path,
        "layers_used": sorted({w["layer"] for w in walls if w["layer"]}),
        "scale_factor": units.scale_to_m if units else 1.0,
        "segment_count": len(walls),
        "bounding_box": {
            "min": [round(drawing.bounds_min[0], 4), round(drawing.bounds_min[1], 4)],
            "max": [round(drawing.bounds_max[0], 4), round(drawing.bounds_max[1], 4)],
        },
        "units": "meters",
        "origin_normalized": True,
        "unit_detection": units.as_dict() if units else {},
        "north": {
            "heading_deg": drawing.north_deg,
            "source": "cad_metadata",
            "confidence": 0.9,
            "reason": "$NORTHDIRECTION",
        },
        "extractor": "recon.pipeline %s" % drawing.schema_version,
        "wall_height": drawing.default_wall_height,
        "validation": drawing.validation,
        "level_structure": {"status": drawing.level_structure.get("status"),
                            "reason": drawing.level_structure.get("reason")},
        "buildings": [{"id": b.id, "name": b.name, "levels": [l.id for l in b.levels]}
                      for b in drawing.buildings],
        "levels": levels_meta,
        "review": list(drawing.review),
    }

    largest = max(footprint_parts, key=lambda r: _ring_area(r), default=[])
    return {
        "metadata": metadata,
        "walls": walls,
        "rooms": rooms,
        "openings": openings,
        "footprint": largest,
        "footprint_parts": footprint_parts,
        "source_evidence": dict(drawing.source_evidence),
        # The semantic tier's CAD document, from the same reader and in the
        # same frame. Absent only for a model loaded back from building.json.
        **({"cad": drawing.semantic_document} if drawing.semantic_document else {}),
    }


def _ring_area(ring) -> float:
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def write_geometry_json(drawing: Drawing, path: str) -> str:
    import json
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(to_geometry_json(drawing), fh, indent=2)
    return path
