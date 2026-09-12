"""
ArchX3D — Diagnostics: make every stage of the reconstruction visible
=====================================================================
Writes the JSON and SVG bundle that turns "the model looks wrong" into "stage
four dropped the garage".

Why SVG and not a render
------------------------
The failure that motivated the rewrite was invisible in the GLB: the model was
*there*, correctly exported, valid glTF — and 25 times too small with its walls
unpaired. Nothing about the final artefact says which stage went wrong. A
per-stage drawing does, and it costs milliseconds.

Each SVG is deliberately one idea:

    debug_raw.svg          everything the DXF contains, coloured by role
    debug_normalized.svg   what survived classification as building geometry
    debug_walls.svg        reconstructed centrelines with their thickness bands
    debug_rooms.svg        room polygons with labels and areas
    debug_openings.svg     doors and windows on their host walls
    debug_topology.svg     the planar graph: nodes by degree, edges
    reconstruction.svg     everything together, the one to look at first

The y axis is flipped on the way out, because SVG counts downwards and a plan
drawn upside down is unreadable by the person trying to debug it.
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import classify as C
from .ir import Building

XY = Tuple[float, float]

MARGIN = 24.0
MAX_PX = 1400.0

#: Role -> stroke colour. Chosen so building geometry is dark and saturated
#: and documentation is pale, which makes "a dimension became a wall" obvious
#: at a glance rather than something you have to measure.
ROLE_COLOUR = {
    C.WALL: "#111827",
    C.FOOTPRINT: "#7c3aed",
    C.STRUCTURE_BELOW: "#9ca3af",
    C.STRUCTURE_ABOVE: "#c4b5fd",
    C.DOOR: "#dc2626",
    C.WINDOW: "#0284c7",
    C.OPENING: "#f59e0b",
    C.STAIR: "#059669",
    C.CASEWORK: "#a3a3a3",
    C.FURNITURE: "#d4d4d8",
    C.FIXTURE: "#93c5fd",
    C.HATCH: "#e5e7eb",
    C.LANDSCAPE: "#bbf7d0",
    C.ELECTRICAL: "#fde68a",
    C.DIMENSION: "#fca5a5",
    C.ANNOTATION: "#fecaca",
    C.ROOM_LABEL: "#fbbf24",
    C.GRID: "#eab308",
    C.TITLE_BLOCK: "#e4e4e7",
    C.CONSTRUCTION: "#f3f4f6",
    C.UNKNOWN: "#fb923c",
}


class _Canvas:
    """A minimal SVG writer with a plan-to-pixel transform.

    Hand-rolled rather than pulled from a library because the whole job is
    "draw some line segments in a box", and a diagnostic that cannot be
    produced when a dependency is missing is not much of a diagnostic.
    """

    def __init__(self, bounds: Tuple[float, float, float, float], title: str):
        x0, y0, x1, y1 = bounds
        w = max(x1 - x0, 1e-6)
        h = max(y1 - y0, 1e-6)
        self.scale = min(MAX_PX / w, MAX_PX / h)
        self.ox, self.oy = x0, y0
        self.w = w * self.scale + 2 * MARGIN
        self.h = h * self.scale + 2 * MARGIN
        self.title = title
        self.parts: List[str] = []

    def pt(self, p: XY) -> Tuple[float, float]:
        return (MARGIN + (p[0] - self.ox) * self.scale,
                self.h - MARGIN - (p[1] - self.oy) * self.scale)

    def polyline(self, pts: Sequence[XY], colour: str, width: float = 1.0,
                 closed: bool = False, fill: str = "none", opacity: float = 1.0) -> None:
        if len(pts) < 2:
            return
        d = " ".join("%s%.2f,%.2f" % ("M" if i == 0 else "L", *self.pt(p))
                     for i, p in enumerate(pts))
        if closed:
            d += " Z"
        self.parts.append(
            '<path d="%s" fill="%s" stroke="%s" stroke-width="%.2f" '
            'stroke-opacity="%.2f" fill-opacity="%.2f" stroke-linecap="round"/>'
            % (d, fill, colour, width, opacity, opacity * 0.35 if fill != "none" else 0))

    def line(self, a: XY, b: XY, colour: str, width: float = 1.0,
             opacity: float = 1.0, dash: str = "") -> None:
        ax, ay = self.pt(a)
        bx, by = self.pt(b)
        self.parts.append(
            '<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" '
            'stroke-width="%.2f" stroke-opacity="%.2f"%s stroke-linecap="round"/>'
            % (ax, ay, bx, by, colour, width, opacity,
               ' stroke-dasharray="%s"' % dash if dash else ""))

    def circle(self, p: XY, r: float, colour: str, fill: str = "none",
               width: float = 1.0) -> None:
        x, y = self.pt(p)
        self.parts.append(
            '<circle cx="%.2f" cy="%.2f" r="%.2f" fill="%s" stroke="%s" '
            'stroke-width="%.2f"/>' % (x, y, r, fill, colour, width))

    def text(self, p: XY, s: str, size: float = 11.0, colour: str = "#111827",
             anchor: str = "middle", weight: str = "normal") -> None:
        x, y = self.pt(p)
        self.parts.append(
            '<text x="%.2f" y="%.2f" font-family="ui-sans-serif,system-ui,sans-serif" '
            'font-size="%.1f" font-weight="%s" fill="%s" text-anchor="%s">%s</text>'
            % (x, y, size, weight, colour, anchor, _esc(s)))

    def legend(self, rows: Sequence[Tuple[str, str]]) -> None:
        y = 18.0
        self.parts.append(
            '<text x="10" y="%.1f" font-family="ui-sans-serif,system-ui,sans-serif" '
            'font-size="13" font-weight="600" fill="#111827">%s</text>'
            % (y, _esc(self.title)))
        for colour, text in rows:
            y += 15.0
            self.parts.append(
                '<rect x="10" y="%.1f" width="10" height="10" fill="%s"/>'
                '<text x="26" y="%.1f" font-family="ui-sans-serif,system-ui,sans-serif" '
                'font-size="11" fill="#374151">%s</text>'
                % (y - 9, colour, y, _esc(text)))

    def render(self) -> str:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
            'viewBox="0 0 %.0f %.0f">\n<rect width="100%%" height="100%%" fill="#ffffff"/>\n%s\n</svg>\n'
            % (self.w, self.h, self.w, self.h, "\n".join(self.parts)))

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.render())
        return path


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _bounds_of(points: Iterable[XY], pad: float = 0.5
               ) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


# ---------------------------------------------------------------------------
# Per-stage drawings
# ---------------------------------------------------------------------------

def draw_raw(drawing, path: str, *, building_only: bool = False) -> str:
    """Every primitive, coloured by the role it was classified as."""
    pts = [p for prim in drawing.prims for p in prim.points]
    canvas = _Canvas(_bounds_of(pts), os.path.basename(path))
    counts: Dict[str, int] = {}
    for prim in sorted(drawing.prims, key=lambda p: p.role == C.WALL):
        if building_only and not _is_building(prim.role):
            continue
        counts[prim.role] = counts.get(prim.role, 0) + 1
        colour = ROLE_COLOUR.get(prim.role, "#fb923c")
        width = 1.6 if prim.role == C.WALL else 0.7
        canvas.polyline(prim.points, colour, width, closed=prim.closed)
    canvas.legend([(ROLE_COLOUR.get(r, "#fb923c"), "%s  x%d" % (r, n))
                   for r, n in sorted(counts.items(), key=lambda kv: -kv[1])])
    return canvas.save(path)


def _is_building(role: str) -> bool:
    return role in (C.WALL, C.FOOTPRINT, C.STRUCTURE_BELOW, C.STRUCTURE_ABOVE,
                    C.DOOR, C.WINDOW, C.OPENING, C.STAIR)


def draw_walls(drawing, walls, faces, path: str) -> str:
    """Reconstructed centrelines over the line work they came from."""
    pts = [p for w in walls for p in (w.start, w.end)] or \
          [p for prim in drawing.wall_prims() for p in prim.points]
    canvas = _Canvas(_bounds_of(pts), os.path.basename(path))
    for prim in drawing.wall_prims():
        canvas.polyline(prim.points, "#d1d5db", 0.8, closed=prim.closed)
    for f in faces:
        for a, b in f.runs:
            canvas.line(f.point(a), f.point(b), "#93c5fd", 1.0, 0.8)
    for w in walls:
        half = w.thickness / 2.0
        n = w.normal
        band = [(w.start[0] + n[0] * half, w.start[1] + n[1] * half),
                (w.end[0] + n[0] * half, w.end[1] + n[1] * half),
                (w.end[0] - n[0] * half, w.end[1] - n[1] * half),
                (w.start[0] - n[0] * half, w.start[1] - n[1] * half)]
        colour = {"exterior": "#111827", "partition": "#6b7280"}.get(w.kind, "#374151")
        canvas.polyline(band, colour, 0.8, closed=True, fill=colour, opacity=0.9)
        canvas.line(w.start, w.end, "#ef4444", 0.9, 0.9)
    canvas.legend([
        ("#d1d5db", "source line work"),
        ("#93c5fd", "detected faces (%d)" % len(faces)),
        ("#374151", "wall bands (%d)" % len(walls)),
        ("#ef4444", "centrelines"),
    ])
    return canvas.save(path)


def draw_rooms(building: Building, path: str) -> str:
    """Room polygons, labelled, with areas."""
    pts = [p for r in building.rooms for p in r.polygon] or building.footprint
    canvas = _Canvas(_bounds_of(pts or [(0, 0), (1, 1)]), os.path.basename(path))
    if building.footprint:
        canvas.polyline(building.footprint, "#7c3aed", 1.6, closed=True)
    palette = ["#fef3c7", "#dbeafe", "#dcfce7", "#fae8ff", "#ffe4e6",
               "#e0f2fe", "#fef9c3", "#ede9fe"]
    for i, r in enumerate(building.rooms):
        fill = palette[i % len(palette)]
        canvas.polyline(r.polygon, "#6b7280", 0.8, closed=True, fill=fill, opacity=1.0)
        for hole in r.holes:
            canvas.polyline(hole, "#9ca3af", 0.6, closed=True, fill="#ffffff")
    for w in building.walls:
        canvas.line(w.start, w.end, "#111827", max(0.8, w.thickness * 14), 0.85)
    for r in building.rooms:
        name = r.label or r.room_type
        canvas.text(r.centroid, name, 11.0, "#111827", weight="600")
        canvas.text((r.centroid[0], r.centroid[1] - 0.35),
                    "%.1f m2" % r.area, 9.5, "#4b5563")
    canvas.legend([("#7c3aed", "footprint %.1f m2" % building.footprint_area),
                   ("#dbeafe", "rooms (%d)" % len(building.rooms)),
                   ("#111827", "walls (%d)" % len(building.walls))])
    return canvas.save(path)


def draw_openings(building: Building, path: str) -> str:
    """Doors and windows in the walls that host them."""
    pts = [p for w in building.walls for p in (w.start, w.end)]
    canvas = _Canvas(_bounds_of(pts or [(0, 0), (1, 1)]), os.path.basename(path))
    for w in building.walls:
        canvas.line(w.start, w.end, "#d1d5db", max(1.0, w.thickness * 14), 1.0)
    kinds = {"door": "#dc2626", "window": "#0284c7",
             "garage": "#7c3aed", "cased": "#f59e0b"}
    counts: Dict[str, int] = {}
    for o in building.openings:
        counts[o.kind] = counts.get(o.kind, 0) + 1
        w = building.wall(o.wall_id)
        colour = kinds.get(o.kind, "#f59e0b")
        if w is None:
            canvas.circle(o.position, 3.0, colour)
            continue
        d = w.direction
        half = o.width / 2.0
        a = (o.position[0] - d[0] * half, o.position[1] - d[1] * half)
        b = (o.position[0] + d[0] * half, o.position[1] + d[1] * half)
        canvas.line(a, b, colour, max(2.0, o.thickness * 14), 1.0)
    canvas.legend([(c, "%s (%d)" % (k, counts.get(k, 0))) for k, c in kinds.items()])
    return canvas.save(path)


def draw_topology(building: Building, path: str) -> str:
    """The planar graph: wall edges and nodes coloured by degree.

    Degree-1 nodes are the diagnostic that matters — a wall end that joins
    nothing is either a genuine free end (a stub at an opening) or the reason
    a room failed to close.
    """
    pts = [n.point for n in building.nodes] or \
          [p for w in building.walls for p in (w.start, w.end)]
    canvas = _Canvas(_bounds_of(pts or [(0, 0), (1, 1)]), os.path.basename(path))
    for w in building.walls:
        canvas.line(w.start, w.end, "#374151", 1.2, 0.9)
    degree_colour = {1: "#dc2626", 2: "#f59e0b", 3: "#16a34a", 4: "#2563eb"}
    counts: Dict[int, int] = {}
    for n in building.nodes:
        deg = n.degree
        counts[deg] = counts.get(deg, 0) + 1
        canvas.circle(n.point, 3.2, degree_colour.get(deg, "#7c3aed"),
                      fill=degree_colour.get(deg, "#7c3aed"), width=0.5)
    canvas.legend([(degree_colour.get(d, "#7c3aed"),
                    "degree %d  x%d%s" % (d, c, "  <- free ends" if d == 1 else ""))
                   for d, c in sorted(counts.items())])
    return canvas.save(path)


def draw_reconstruction(building: Building, path: str) -> str:
    """Everything at once — the single picture to look at first."""
    pts = ([p for r in building.rooms for p in r.polygon] +
           [p for w in building.walls for p in (w.start, w.end)])
    canvas = _Canvas(_bounds_of(pts or [(0, 0), (1, 1)]), os.path.basename(path))
    palette = ["#f8fafc", "#f1f5f9", "#f8fafc", "#f1f5f9"]
    for i, r in enumerate(building.rooms):
        canvas.polyline(r.polygon, "#e2e8f0", 0.5, closed=True,
                        fill=palette[i % len(palette)], opacity=1.0)
    for w in building.walls:
        half = w.thickness / 2.0
        n = w.normal
        band = [(w.start[0] + n[0] * half, w.start[1] + n[1] * half),
                (w.end[0] + n[0] * half, w.end[1] + n[1] * half),
                (w.end[0] - n[0] * half, w.end[1] - n[1] * half),
                (w.start[0] - n[0] * half, w.start[1] - n[1] * half)]
        canvas.polyline(band, "#1f2937", 0.4, closed=True, fill="#1f2937", opacity=1.0)
    for o in building.openings:
        w = building.wall(o.wall_id)
        colour = {"door": "#dc2626", "window": "#0284c7",
                  "garage": "#7c3aed"}.get(o.kind, "#f59e0b")
        if w is None:
            continue
        d = w.direction
        half = o.width / 2.0
        canvas.line((o.position[0] - d[0] * half, o.position[1] - d[1] * half),
                    (o.position[0] + d[0] * half, o.position[1] + d[1] * half),
                    colour, max(2.5, o.thickness * 15), 1.0)
    for r in building.rooms:
        canvas.text(r.centroid, (r.label or r.room_type).upper(), 10.0,
                    "#0f172a", weight="600")
        canvas.text((r.centroid[0], r.centroid[1] - 0.32), "%.1f m2" % r.area,
                    9.0, "#64748b")
    s = building.summary()
    canvas.legend([
        ("#1f2937", "%d walls, %.0f m" % (len(building.walls), building.total_wall_length)),
        ("#f1f5f9", "%d rooms, %.0f m2" % (len(building.rooms), building.floor_area)),
        ("#dc2626", "%d doors" % s.get("doors", 0)),
        ("#0284c7", "%d windows" % s.get("windows", 0)),
        ("#7c3aed", "%.1f x %.1f m  (%s)" % (
            building.width, building.depth,
            building.units.unit_name if building.units else "?")),
    ])
    return canvas.save(path)


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

def write_bundle(directory: str, *, drawing=None, wall_result=None,
                 building: Optional[Building] = None,
                 error: Optional[dict] = None) -> Dict[str, str]:
    """Write everything available about this run into ``directory``.

    Deliberately tolerant: it is called on the failure path too, where there
    may be a drawing but no building, and the whole point is to emit whatever
    got as far as existing.
    """
    os.makedirs(directory, exist_ok=True)
    written: Dict[str, str] = {}

    def _json(name: str, payload) -> None:
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        written[name] = path

    if drawing is not None:
        _json("entities.json", {
            "summary": drawing.summary(),
            "layers": {k: {"count": drawing.layer_counts.get(k, 0),
                           **(v.as_dict() if hasattr(v, "as_dict") else {})}
                       for k, v in sorted(drawing.layer_survey.items())},
            "warnings": drawing.warnings,
            "labels": [{"text": t.text, "point": [round(v, 3) for v in t.point],
                        "layer": t.layer} for t in drawing.labels],
        })
        _json("units.json", drawing.units.as_dict() if drawing.units else {})
        try:
            written["debug_raw.svg"] = draw_raw(
                drawing, os.path.join(directory, "debug_raw.svg"))
            written["debug_normalized.svg"] = draw_raw(
                drawing, os.path.join(directory, "debug_normalized.svg"),
                building_only=True)
        except Exception as exc:      # a diagnostic must never break a build
            written["svg_error"] = str(exc)

    if wall_result is not None:
        _json("walls.json", {
            "stats": wall_result.stats,
            "walls": [w.as_dict() for w in wall_result.walls],
        })
        if drawing is not None:
            try:
                written["debug_walls.svg"] = draw_walls(
                    drawing, wall_result.walls, wall_result.faces,
                    os.path.join(directory, "debug_walls.svg"))
            except Exception as exc:
                written["svg_error_walls"] = str(exc)

    if building is not None:
        _json("rooms.json", [r.as_dict() for r in building.rooms])
        _json("doors.json", [o.as_dict() for o in building.openings
                             if o.kind in ("door", "garage", "cased")])
        _json("windows.json", [o.as_dict() for o in building.openings
                               if o.kind == "window"])
        _json("validation.json", building.validation)
        _json("building.json", building.as_dict())
        for fn, name in ((draw_rooms, "debug_rooms.svg"),
                         (draw_openings, "debug_openings.svg"),
                         (draw_topology, "debug_topology.svg"),
                         (draw_reconstruction, "reconstruction.svg")):
            try:
                written[name] = fn(building, os.path.join(directory, name))
            except Exception as exc:
                written["svg_error_" + name] = str(exc)

    if error is not None:
        _json("error.json", error)

    return written
