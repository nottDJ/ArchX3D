"""
ArchX3D — ``geometry.json`` from the building model
===================================================
Emits the legacy document that the vision, furnishing, evaluation and viewer
stages read, derived from the validated :class:`~modules.recon.ir.Building`
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

What changes for consumers
--------------------------
``walls`` are now wall **centrelines** with a thickness, rather than every line
the drafter drew. That is a strict improvement for every consumer: the viewer's
footprint is the building's, and ``build_room_frame`` gets one segment per wall
instead of two per wall plus the dimension strings.

``rooms`` is new and carries the room polygons, which nothing required before
because nothing had them.
"""

from __future__ import annotations

import os
from typing import Dict, List

from .ir import Building


def to_geometry_json(building: Building) -> dict:
    """The legacy document, projected from the building model."""
    walls: List[dict] = []
    for w in building.walls:
        walls.append({
            "start": [round(w.start[0], 4), round(w.start[1], 4)],
            "end": [round(w.end[0], 4), round(w.end[1], 4)],
            "source_entity": "WALL",
            "layer": w.layer or "WALLS",
            "thickness": round(w.thickness, 4),
            "kind": w.kind,
            "wall_id": w.id,
        })

    units = building.units
    metadata = {
        "source": building.source_path,
        "layers_used": sorted({w.layer for w in building.walls if w.layer}),
        "scale_factor": units.scale_to_m if units else 1.0,
        "segment_count": len(walls),
        "bounding_box": {
            "min": [round(building.bounds_min[0], 4), round(building.bounds_min[1], 4)],
            "max": [round(building.bounds_max[0], 4), round(building.bounds_max[1], 4)],
        },
        "units": "meters",
        "origin_normalized": True,
        "unit_detection": units.as_dict() if units else {},
        "north": {
            "heading_deg": building.north_deg,
            "source": "cad_metadata",
            "confidence": 0.9,
            "reason": "$NORTHDIRECTION",
        },
        "extractor": "recon.pipeline %s" % building.schema_version,
        "wall_height": building.default_wall_height,
        "validation": building.validation,
    }

    return {
        "metadata": metadata,
        "walls": walls,
        # New, and additive: nothing downstream is required to read these, but
        # a consumer that wants the actual rooms no longer has to guess them
        # from a bounding box.
        "rooms": [
            {
                "id": r.id,
                "label": r.label,
                "room_type": r.room_type,
                "area": round(r.area, 3),
                "centroid": [round(r.centroid[0], 4), round(r.centroid[1], 4)],
                "polygon": [[round(x, 4), round(y, 4)] for x, y in r.polygon],
                "is_exterior": r.is_exterior,
            }
            for r in building.rooms
        ],
        "openings": [
            {
                "id": o.id, "kind": o.kind, "wall_id": o.wall_id,
                "position": [round(o.position[0], 4), round(o.position[1], 4)],
                "width": round(o.width, 4), "height": round(o.height, 4),
                "sill_height": round(o.sill_height, 4),
            }
            for o in building.openings
        ],
        "footprint": [[round(x, 4), round(y, 4)] for x, y in building.footprint],
    }


def write_geometry_json(building: Building, path: str) -> str:
    import json
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(to_geometry_json(building), fh, indent=2)
    return path
