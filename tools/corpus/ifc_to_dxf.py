"""
Convert a real BIM model (IFC) into a multi-storey DXF drawing sheet with truth.

What this produces
------------------
One DXF sheet holding a floor plan of every storey of the building, laid out
the way a drafter lays out a drawing set on one sheet — side by side or one
above the other, each with its title — and a ``.truth.json`` beside it.

A floor plan is a horizontal cut through the building. Every wall, column and
curtain-wall part of the *whole* model is sectioned at 1.2 m above each
storey's floor, exactly as a plan view cuts it: a wall that runs through two
storeys appears on both, a door or window the cut passes through leaves a gap
in the wall, and a window whose sill is above the cut is not in the plan. The
cut outlines are drawn as closed wall outlines; doors are drawn as a leaf and a
swing arc, windows as glazing lines across the gap, and every space is labelled
with its name at a point inside it.

Which storeys are floor plans is decided by content, not by name: a storey is
drawn when doors and spaces sit on it (a foundation or a roof storey has
neither).

Truth
-----
In metres in the sheet's own frame (see :mod:`modules.recon.metrics`):
openings (every door and window the cut passes through, with the IFC nominal
width), rooms (every space cut at plan height, with its area and whether walls
and openings actually enclose it), wall solids, the footprint of each storey,
and ``expect`` = 1 building with N levels.

Requires ``ifcopenshell`` (not a project dependency; run it from a separate
environment)::

    python tools/corpus/ifc_to_dxf.py MODEL.ifc OUT.dxf --units mm --layout row
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
from typing import Dict, List, Optional, Sequence, Tuple

import ezdxf
import numpy as np
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import linemerge, unary_union

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element as UE
import ifcopenshell.util.placement as UP
import ifcopenshell.util.unit as UU

CUT_HEIGHT = 1.2
#: Gaps between bounding elements narrower than this do not open a room, in
#: metres (unjoined wall seams in the model).
SEAM = 0.16
UNIT_CODES = {"mm": (4, 1000.0), "cm": (5, 100.0), "m": (6, 1.0),
              "in": (1, 1.0 / 0.0254), "ft": (2, 1.0 / 0.3048)}
WALL_TYPES = ("IfcWall", "IfcWallStandardCase")
GLAZED_PARTS = ("IfcPlate", "IfcMember")
XY = Tuple[float, float]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

class Mesh:
    __slots__ = ("element", "verts", "faces", "zmin", "zmax")

    def __init__(self, element, verts, faces):
        self.element = element
        self.verts = np.asarray(verts, dtype=float).reshape(-1, 3)
        self.faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
        self.zmin = float(self.verts[:, 2].min()) if len(self.verts) else 0.0
        self.zmax = float(self.verts[:, 2].max()) if len(self.verts) else 0.0


def load_meshes(model, elements) -> Dict[int, Mesh]:
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    out: Dict[int, Mesh] = {}
    if not elements:
        return out
    it = ifcopenshell.geom.iterator(settings, model, multiprocessing.cpu_count(),
                                    include=list(elements))
    if not it.initialize():
        return out
    while True:
        shape = it.get()
        el = model.by_id(shape.id)
        g = shape.geometry
        if len(g.faces):
            out[shape.id] = Mesh(el, g.verts, g.faces)
        if not it.next():
            break
    return out


class Outline:
    """A space known only by its 2D footprint: its plan is the same at any height."""
    __slots__ = ("element", "polygon", "zmin", "zmax")

    def __init__(self, element, polygon: Polygon, zmin: float, height: float):
        self.element = element
        self.polygon = polygon
        self.zmin = zmin
        self.zmax = zmin + height


def footprint_outline(element, unit_scale: float) -> Optional[Outline]:
    """A space's ``FootPrint`` curve set, placed in the world, in metres.

    Many exporters give spaces a 2D footprint and a bounding box and no body
    at all; the geometry iterator meshes bodies only.
    """
    rep = getattr(element, "Representation", None)
    if rep is None:
        return None
    try:
        mtx = np.array(UP.get_local_placement(element.ObjectPlacement), dtype=float)
    except Exception:
        return None
    rings = []
    for r in rep.Representations:
        if r.RepresentationIdentifier != "FootPrint":
            continue
        for item in r.Items:
            curves = list(getattr(item, "Elements", None) or [item])
            for c in curves:
                pts = _curve_points(c)
                if len(pts) >= 3:
                    rings.append(pts)
    polys = []
    for pts in rings:
        world = []
        for x, y in pts:
            v = mtx @ np.array([x, y, 0.0, 1.0])
            world.append((v[0] * unit_scale, v[1] * unit_scale))
        p = Polygon(world).buffer(0)
        if p.area > 0.5:
            polys.append(p)
    if not polys:
        return None
    z = float(mtx[2][3]) * unit_scale
    return Outline(element, unary_union(polys), z, 2.6)


def _curve_points(curve) -> List[XY]:
    kind = curve.is_a()
    if kind == "IfcPolyline":
        return [tuple(p.Coordinates[:2]) for p in curve.Points]
    if kind == "IfcIndexedPolyCurve":
        return [tuple(p[:2]) for p in curve.Points.CoordList]
    if kind == "IfcCompositeCurve":
        out: List[XY] = []
        for seg in curve.Segments:
            out.extend(_curve_points(seg.ParentCurve))
        return out
    if kind == "IfcTrimmedCurve":
        return _curve_points(curve.BasisCurve)
    return []


def section(mesh, z: float):
    """The solid a horizontal plane at ``z`` cuts from a closed mesh."""
    if isinstance(mesh, Outline):
        return mesh.polygon if mesh.zmin - 0.01 <= z <= mesh.zmax else Polygon()
    if not (mesh.zmin < z < mesh.zmax):
        return Polygon()
    V, F = mesh.verts, mesh.faces
    dz = V[:, 2] - z
    dz = np.where(np.abs(dz) < 1e-9, 1e-9, dz)
    above = dz > 0
    pts = np.zeros((len(F), 3, 2))
    hit = np.zeros((len(F), 3), dtype=bool)
    for k, (i, j) in enumerate(((0, 1), (1, 2), (2, 0))):
        a = np.minimum(F[:, i], F[:, j])
        b = np.maximum(F[:, i], F[:, j])
        m = above[a] != above[b]
        t = np.where(m, dz[a] / np.where(m, dz[a] - dz[b], 1.0), 0.0)
        pts[:, k] = V[a, :2] + t[:, None] * (V[b, :2] - V[a, :2])
        hit[:, k] = m
    rows = hit.sum(axis=1) == 2
    if not rows.any():
        return Polygon()
    order = np.argsort(~hit[rows], axis=1, kind="stable")[:, :2]
    seg = np.take_along_axis(pts[rows], order[:, :, None], axis=1)
    seg = np.round(seg, 5)
    lines = [((s[0, 0], s[0, 1]), (s[1, 0], s[1, 1])) for s in seg
             if (s[0] != s[1]).any()]
    if not lines:
        return Polygon()
    merged = linemerge(MultiLineString(lines))
    rings = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    polys = []
    for r in rings:
        coords = list(r.coords)
        if len(coords) >= 4 and math.dist(coords[0], coords[-1]) < 1e-4:
            p = Polygon(coords).buffer(0)
            if p.area > 1e-5:
                polys.append(p)
    solid = Polygon()
    for p in sorted(polys, key=lambda q: -q.area):
        solid = solid.symmetric_difference(p)
    return solid.buffer(0)


def parts(geom) -> List[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]


def plan_rect(mesh: Mesh):
    """Centre, unit axis, long side and short side of the element in plan."""
    hull = unary_union([Point(p) for p in mesh.verts[:, :2]]).convex_hull
    rect = hull.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return None
    c = list(rect.exterior.coords)
    e1, e2 = math.dist(c[0], c[1]), math.dist(c[1], c[2])
    a, b = (c[0], c[1]) if e1 >= e2 else (c[1], c[2])
    long_, short = max(e1, e2), min(e1, e2)
    d = ((b[0] - a[0]) / long_, (b[1] - a[1]) / long_) if long_ else (1.0, 0.0)
    return (rect.centroid.x, rect.centroid.y), d, long_, short


def across_wall(solid, c: XY, d: XY, half: float) -> Optional[Tuple[XY, float]]:
    """The wall's centreline point and thickness either side of an opening.

    Probes short lines across the wall beyond each jamb; a probe's
    intersection with the wall solid is the wall's thickness there. Beside a
    corner or a tee a probe runs *along* the other wall and reads long, so
    only the probes agreeing with the thinnest reading are believed.
    """
    n = (-d[1], d[0])
    hits = []
    for side in (-1.0, 1.0):
        for extra in (0.06, 0.15, 0.25, 0.4, 0.6):
            px = c[0] + d[0] * side * (half + extra)
            py = c[1] + d[1] * side * (half + extra)
            probe = LineString([(px - n[0] * 0.7, py - n[1] * 0.7),
                                (px + n[0] * 0.7, py + n[1] * 0.7)])
            cut = probe.intersection(solid)
            segs = [g for g in getattr(cut, "geoms", [cut]) if g.geom_type == "LineString"
                    and 0.03 < g.length < 1.35]
            if not segs:
                continue
            s = min(segs, key=lambda g: g.distance(Point(px, py)))
            if s.distance(Point(px, py)) > 0.3:
                continue
            (x0, y0), (x1, y1) = s.coords[0], s.coords[-1]
            mid = ((x0 + x1) / 2 - d[0] * side * (half + extra),
                   (y0 + y1) / 2 - d[1] * side * (half + extra))
            hits.append((mid, s.length))
    if not hits:
        return None
    thin = min(h[1] for h in hits)
    good = [h for h in hits if h[1] <= thin + 0.01]
    mx = sum(h[0][0] for h in good) / len(good)
    my = sum(h[0][1] for h in good) / len(good)
    # Only the offset across the wall moves the opening; along it, keep ``c``.
    off = (mx - c[0]) * n[0] + (my - c[1]) * n[1]
    return (c[0] + n[0] * off, c[1] + n[1] * off), thin


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

def storey_heights(model) -> List[Tuple[object, float]]:
    scale = UU.calculate_unit_scale(model)
    out = []
    for st in model.by_type("IfcBuildingStorey"):
        try:
            z = float(UP.get_local_placement(st.ObjectPlacement)[2][3]) * scale
        except Exception:
            z = float(st.Elevation or 0.0) * scale
        out.append((st, z))
    return sorted(out, key=lambda t: t[1])


def band_of(z: float, bands: Sequence[Tuple[object, float]]) -> Optional[int]:
    idx = None
    for i, (_st, zb) in enumerate(bands):
        if z + 0.3 >= zb:
            idx = i
    return idx


def operation(element) -> str:
    t = UE.get_type(element)
    for obj in (t, element):
        op = getattr(obj, "OperationType", None) if obj is not None else None
        if op:
            return str(op)
    return ""


class Sheet:
    def __init__(self, units: str, declare: Optional[int], loose: bool):
        self.code, self.per_m = UNIT_CODES[units]
        self.units = units
        self.loose = loose
        self.doc = ezdxf.new("R2013", setup=True)
        self.doc.header["$INSUNITS"] = self.code if declare is None else declare
        self.msp = self.doc.modelspace()
        for layer in ("A-WALL", "A-COLS", "A-DOOR", "A-GLAZ", "A-GLAZ-CURT",
                      "A-AREA-IDEN", "A-ANNO-TTLB"):
            self.doc.layers.add(layer)
        self.offset = (0.0, 0.0)

    def u(self, p: XY) -> XY:
        return ((p[0] + self.offset[0]) * self.per_m, (p[1] + self.offset[1]) * self.per_m)

    def m(self, p: XY) -> List[float]:
        return [round(p[0] + self.offset[0], 5), round(p[1] + self.offset[1], 5)]

    def ring(self, coords, layer: str) -> None:
        pts = [self.u(p) for p in list(coords)[:-1]]
        if len(pts) < 3:
            return
        if self.loose:
            for i in range(len(pts)):
                self.msp.add_line(pts[i], pts[(i + 1) % len(pts)], dxfattribs={"layer": layer})
        else:
            self.msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})

    def line(self, a: XY, b: XY, layer: str) -> None:
        self.msp.add_line(self.u(a), self.u(b), dxfattribs={"layer": layer})

    def text(self, p: XY, s: str, h: float, layer: str, align: str = "MIDDLE_CENTER") -> None:
        from ezdxf.enums import TextEntityAlignment
        t = self.msp.add_text(s, height=h * self.per_m, dxfattribs={"layer": layer})
        t.set_placement(self.u(p), align=getattr(TextEntityAlignment, align))


def draw_door(sheet: Sheet, c: XY, d: XY, width: float, thick: float, sign: float,
              hinge_first: bool, double: bool) -> None:
    n = (-d[1] * sign, d[0] * sign)
    face = (c[0] + n[0] * thick / 2, c[1] + n[1] * thick / 2)
    ends = [(face[0] - d[0] * width / 2, face[1] - d[1] * width / 2),
            (face[0] + d[0] * width / 2, face[1] + d[1] * width / 2)]
    leaves = ([(ends[0], d, width / 2), (ends[1], (-d[0], -d[1]), width / 2)] if double
              else [(ends[0], d, width) if hinge_first else (ends[1], (-d[0], -d[1]), width)])
    for hinge, toward, r in leaves:
        tip = (hinge[0] + n[0] * r, hinge[1] + n[1] * r)
        t = 0.04
        sheet.ring([hinge, tip, (tip[0] + toward[0] * t, tip[1] + toward[1] * t),
                    (hinge[0] + toward[0] * t, hinge[1] + toward[1] * t), hinge], "A-DOOR")
        a_leaf = math.degrees(math.atan2(n[1], n[0]))
        a_shut = math.degrees(math.atan2(toward[1], toward[0]))
        ccw = n[0] * toward[1] - n[1] * toward[0] > 0
        start, end = (a_leaf, a_shut) if ccw else (a_shut, a_leaf)
        sheet.msp.add_arc(sheet.u(hinge), r * sheet.per_m, start, end,
                          dxfattribs={"layer": "A-DOOR"})


def draw_window(sheet: Sheet, c: XY, d: XY, width: float, thick: float) -> None:
    n = (-d[1], d[0])
    hw, ht = width / 2, thick / 2
    corners = [(c[0] - d[0] * hw - n[0] * ht, c[1] - d[1] * hw - n[1] * ht),
               (c[0] + d[0] * hw - n[0] * ht, c[1] + d[1] * hw - n[1] * ht),
               (c[0] + d[0] * hw + n[0] * ht, c[1] + d[1] * hw + n[1] * ht),
               (c[0] - d[0] * hw + n[0] * ht, c[1] - d[1] * hw + n[1] * ht)]
    sheet.ring(corners + [corners[0]], "A-GLAZ")
    for off in (-0.02, 0.02):
        sheet.line((c[0] - d[0] * hw + n[0] * off, c[1] - d[1] * hw + n[1] * off),
                   (c[0] + d[0] * hw + n[0] * off, c[1] + d[1] * hw + n[1] * off), "A-GLAZ")


def convert(ifc_path: str, out_path: str, units: str, layout: str, titles: str,
            loose: bool, declare: Optional[int], category: str, style: str) -> dict:
    model = ifcopenshell.open(ifc_path)
    unit_scale = UU.calculate_unit_scale(model)
    bands = storey_heights(model)

    walls = [e for t in WALL_TYPES for e in model.by_type(t, include_subtypes=False)]
    columns = model.by_type("IfcColumn")
    glazed = [e for e in model.by_type("IfcCurtainWall")]
    for cw in list(glazed):
        glazed += [p for p in UE.get_decomposition(cw) if p.is_a() in GLAZED_PARTS]
    doors = model.by_type("IfcDoor")
    windows = model.by_type("IfcWindow")
    spaces = model.by_type("IfcSpace")
    meshes = load_meshes(model, walls + columns + glazed + doors + windows + spaces)

    def of(elements):
        return [meshes[e.id()] for e in elements if e.id() in meshes]

    wall_m, col_m, glz_m = of(walls), of(columns), of(glazed)
    door_m, win_m, space_m = of(doors), of(windows), of(spaces)
    for sp in spaces:
        if sp.id() not in meshes:
            outline = footprint_outline(sp, unit_scale)
            if outline is not None:
                space_m.append(outline)

    # A storey is a floor plan when rooms and doors stand on it: a footing
    # or roof storey may hold a stray door or a single roof space, not three.
    plans = []
    for i, (st, z) in enumerate(bands):
        on = lambda ms: [m for m in ms if band_of(m.zmin, bands) == i]
        if len(on(door_m)) >= 3 and len(on(space_m)) >= 3:
            plans.append((st, z, on(door_m), on(win_m), on(space_m)))
    if len(plans) < 2:
        raise SystemExit("fewer than two floor-plan storeys in %s" % ifc_path)

    storeys = []
    for st, z, ds, ws, ss in plans:
        cut = z + CUT_HEIGHT
        # Mitred wall ends meet along a shared edge that rounding leaves a
        # hair apart; closing by a millimetre dissolves the seam.
        solid = unary_union([section(m, cut) for m in wall_m]).buffer(0.001, join_style=2)
        solid = solid.buffer(-0.001, join_style=2).simplify(0.0005)
        cols = unary_union([section(m, cut) for m in col_m]).buffer(0)
        glass = unary_union([section(m, cut) for m in glz_m]).buffer(0)
        storeys.append({"storey": st, "z": z, "cut": cut, "walls": solid, "cols": cols,
                        "glass": glass, "doors": ds, "windows": ws, "spaces": ss})

    boxes = [unary_union([s["walls"], s["cols"], s["glass"]]).bounds for s in storeys]
    width = max(b[2] - b[0] for b in boxes)
    depth = max(b[3] - b[1] for b in boxes)
    gx0 = min(b[0] for b in boxes)
    gy0 = min(b[1] for b in boxes)
    gap = max(8.0, 0.35 * max(width, depth))

    sheet = Sheet(units, declare, loose)
    truth = {"openings": [], "rooms": [], "spaces": [], "walls": [], "footprint": [],
             "glazing": [], "levels": []}
    thicknesses: List[Tuple[float, float]] = []
    ground = min(range(len(storeys)), key=lambda k: abs(storeys[k]["z"]))
    for k, s in enumerate(storeys):
        if layout == "row":
            sheet.offset = (-gx0 + k * (width + gap), -gy0)
        else:
            sheet.offset = (-gx0, -gy0 - k * (depth + gap))
        solid = s["walls"]
        for p in parts(solid):
            sheet.ring(p.exterior.coords, "A-WALL")
            for hole in p.interiors:
                sheet.ring(hole.coords, "A-WALL")
            truth["walls"].append({"polygon": [sheet.m(q) for q in p.exterior.coords[:-1]],
                                   "holes": [[sheet.m(q) for q in h.coords[:-1]] for h in p.interiors],
                                   "area_m2": round(p.area, 4), "level": k})
        for p in parts(s["cols"]):
            sheet.ring(p.exterior.coords, "A-COLS")
        for p in parts(s["glass"]):
            sheet.ring(p.exterior.coords, "A-GLAZ-CURT")
            truth["glazing"].append({"polygon": [sheet.m(q) for q in p.exterior.coords[:-1]],
                                     "level": k})

        closers = []
        for kind, pool in (("door", s["doors"]), ("window", s["windows"])):
            for mesh in pool:
                if not (mesh.zmin - 0.01 <= s["cut"] <= mesh.zmax + 0.01):
                    continue            # above or below the cut: not in this plan
                r = plan_rect(mesh)
                if r is None:
                    continue
                c, d, long_, short = r
                w = long_
                if getattr(mesh.element, "OverallWidth", None):
                    w = float(mesh.element.OverallWidth) * unit_scale
                if not (0.4 <= w <= 4.0) or w > long_ + 0.25:
                    w = long_
                if w < 0.4:
                    continue
                probe = across_wall(solid, c, d, w / 2)
                if probe is None:
                    continue            # not in a wall this plan cuts
                centre, thick = probe
                thick = min(max(thick, 0.08), 0.8)
                if kind == "door":
                    try:
                        mtx = UP.get_local_placement(mesh.element.ObjectPlacement)
                        ax, ay = float(mtx[0][0]), float(mtx[1][0])
                        yx, yy = float(mtx[0][1]), float(mtx[1][1])
                    except Exception:
                        ax, ay, yx, yy = d[0], d[1], -d[1], d[0]
                    sign = 1.0 if (-d[1]) * yx + d[0] * yy >= 0 else -1.0
                    hinge_first = d[0] * ax + d[1] * ay >= 0
                    op = operation(mesh.element).upper()
                    double = "DOUBLE" in op
                    draw_door(sheet, centre, d, w, thick, sign, hinge_first, double)
                    truth_kind = "double_door" if double else "door"
                else:
                    draw_window(sheet, centre, d, w, thick)
                    truth_kind = "window"
                truth["openings"].append({"kind": truth_kind, "centre": sheet.m(centre),
                                          "width": round(w, 4), "level": k,
                                          "ifc": mesh.element.GlobalId})
                n = (-d[1], d[0])
                hw, ht = w / 2 + 0.02, thick / 2
                closers.append(Polygon([
                    (centre[0] - d[0] * hw - n[0] * ht, centre[1] - d[1] * hw - n[1] * ht),
                    (centre[0] + d[0] * hw - n[0] * ht, centre[1] + d[1] * hw - n[1] * ht),
                    (centre[0] + d[0] * hw + n[0] * ht, centre[1] + d[1] * hw + n[1] * ht),
                    (centre[0] - d[0] * hw + n[0] * ht, centre[1] - d[1] * hw + n[1] * ht)]))

        # What bounds a room in plan: walls, columns, glazing and openings.
        # The rooms a wall model can produce are exactly the regions these
        # close off; an open-plan ground floor holding four named spaces is
        # one such region, and counting it as four would score a correct
        # reconstruction as wrong.
        bounds = unary_union([solid, s["cols"], s["glass"]] + closers).buffer(0)
        shell = bounds.buffer(0.15).buffer(-0.15)
        fp = unary_union([Polygon(p.exterior) for p in parts(shell) if p.area > 2.0])
        for p in parts(fp):
            truth["footprint"].append([sheet.m(q) for q in p.exterior.coords[:-1]])
        # Models leave seams of a few centimetres where walls were never
        # joined; a seam narrower than SEAM is not a way out of a room.
        sealed = bounds.buffer(SEAM / 2, join_style=2).buffer(-SEAM / 2, join_style=2)
        filled = unary_union([Polygon(p.exterior) for p in parts(sealed)])
        regions = [r for r in parts(filled.difference(sealed).buffer(-0.005).buffer(0.005))
                   if r.area >= 1.0]

        named: Dict[int, List[str]] = {}
        for mesh in s["spaces"]:
            cut = s["cut"] if mesh.zmin < s["cut"] < mesh.zmax else mesh.zmin + 0.1
            poly = section(mesh, cut)
            for p in parts(poly):
                if p.area < 1.0:
                    continue
                el = mesh.element
                name = (el.LongName or el.Name or "").strip()
                number = (el.Name or "").strip() if el.LongName else ""
                pt = p.representative_point()
                home = next((i for i, r in enumerate(regions) if r.contains(pt)), None)
                if home is not None:
                    named.setdefault(home, []).append(name)
                truth["spaces"].append({"label": name, "number": number,
                                        "area_m2": round(p.area, 3), "point": sheet.m((pt.x, pt.y)),
                                        "level": k, "region": home})
                if name:
                    sheet.text((pt.x, pt.y + 0.18), name.upper(), 0.2, "A-AREA-IDEN")
                if number:
                    sheet.text((pt.x, pt.y - 0.18), number, 0.15, "A-AREA-IDEN")
        for i, r in enumerate(regions):
            pt = r.representative_point()
            truth["rooms"].append({"label": " / ".join(named.get(i, [])),
                                   "area_m2": round(r.area, 3), "point": sheet.m((pt.x, pt.y)),
                                   "enclosed": True, "exterior": False, "level": k,
                                   "spaces": len(named.get(i, [])),
                                   "polygon": [sheet.m(q) for q in r.exterior.coords[:-1]]})

        for m in wall_m:
            for p in parts(section(m, s["cut"])):
                rect = p.minimum_rotated_rectangle
                if rect.geom_type != "Polygon":
                    continue
                c = list(rect.exterior.coords)
                e1, e2 = math.dist(c[0], c[1]), math.dist(c[1], c[2])
                # A straight wall's section is its own rectangle; an L-shaped
                # or curved wall's is not, and says nothing about thickness.
                if min(e1, e2) > 0.03 and max(e1, e2) / min(e1, e2) > 3 \
                        and p.area >= 0.9 * rect.area:
                    thicknesses.append((min(e1, e2), max(e1, e2)))

        b = unary_union([solid, s["cols"], s["glass"]]).bounds
        title = storey_title(s["storey"], k - ground, titles)
        if layout == "row":
            sheet.text(((b[0] + b[2]) / 2, b[1] - 2.5), title, 0.6, "A-ANNO-TTLB")
            sheet.text(((b[0] + b[2]) / 2, b[1] - 3.4), "SCALE 1:100", 0.3, "A-ANNO-TTLB")
        else:
            sheet.text((b[0], b[3] + 2.0), title, 0.6, "A-ANNO-TTLB", align="MIDDLE_LEFT")
        truth["levels"].append({"index": k - ground, "title": title,
                                "ifc_storey": s["storey"].Name, "elevation_m": round(s["z"], 3),
                                "sheet_offset_m": [round(sheet.offset[0], 4), round(sheet.offset[1], 4)]})

    if thicknesses:
        thicknesses.sort()
        total = sum(L for _t, L in thicknesses)
        acc, med = 0.0, thicknesses[0][0]
        for t, L in thicknesses:
            acc += L
            if acc >= total / 2:
                med = t
                break
        truth["wall_thickness_m"] = round(med, 4)
    fp_all = unary_union([Polygon(r) for r in truth["footprint"]])
    truth["footprint_area_m2"] = round(fp_all.area, 3)
    truth["expect"] = {"buildings": 1, "levels": len(storeys)}
    truth["units"] = units
    truth["frame"] = "drawing"
    truth["source"] = {"dataset": "buildingSMART Community Sample Test Files",
                       "file": os.path.basename(ifc_path), "license": "CC BY 4.0",
                       "category": category, "style": style, "native_cad": False,
                       "tags": ["multi_storey", "ifc_section"],
                       "conversion": "tools/corpus/ifc_to_dxf.py, plan cut %.1f m" % CUT_HEIGHT}
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sheet.doc.saveas(out_path)
    with open(out_path[:-4] + ".truth.json", "w", encoding="utf-8") as fh:
        json.dump(truth, fh, indent=1)
    return truth


_ENGLISH = ["GROUND FLOOR", "FIRST FLOOR", "SECOND FLOOR", "THIRD FLOOR", "FOURTH FLOOR",
            "FIFTH FLOOR", "SIXTH FLOOR"]


def storey_title(storey, index: int, titles: str) -> str:
    """The plan's title: the storey's own name, or an English floor name."""
    if titles == "native":
        return "%s PLAN" % (storey.Name or "").strip().upper()
    if index < 0:
        return "BASEMENT PLAN" if index == -1 else "BASEMENT %d PLAN" % -index
    return "%s PLAN" % _ENGLISH[index] if index < len(_ENGLISH) else "LEVEL %d PLAN" % index


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ifc")
    ap.add_argument("out")
    ap.add_argument("--units", default="mm", choices=sorted(UNIT_CODES))
    ap.add_argument("--declare", type=int, default=None,
                    help="$INSUNITS to write (default: the true unit; 0 = undeclared)")
    ap.add_argument("--layout", default="row", choices=("row", "column"))
    ap.add_argument("--titles", default="native", choices=("native", "english"))
    ap.add_argument("--loose", action="store_true", help="walls as loose LINEs")
    ap.add_argument("--category", default="residential")
    ap.add_argument("--style", default="IFC")
    args = ap.parse_args()
    truth = convert(args.ifc, args.out, args.units, args.layout, args.titles, args.loose,
                    args.declare, args.category, args.style)
    print("wrote %s: %d levels, %d openings, %d rooms" % (
        args.out, len(truth["levels"]), len(truth["openings"]), len(truth["rooms"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
