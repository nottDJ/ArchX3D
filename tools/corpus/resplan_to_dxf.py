"""
Convert ResPlan floor plans into DXF drawings with ground truth.

Source
------
ResPlan (https://github.com/m-agour/ResPlan) — 17,000 residential floor plans
from public real-estate listings, as polygons: walls, doors, windows and typed
rooms. Data licence CC BY 4.0; code MIT. Cite: Abouagour & Garyfallidis,
"ResPlan: A Large-Scale Vector-Graph Dataset of 17,000 Residential Floor
Plans", arXiv:2508.14006.

What the conversion is, honestly
--------------------------------
The *layouts* are real: real rooms, walls, doors and windows from real homes.
The *drawings* are made here — ResPlan ships polygons, not CAD — so the
drafting conventions are chosen by this script, and several are deliberately
used so the engine is not tested on one office's habits:

    A  AIA layers, walls as closed outlines, door = header band + swing arc,
       window = glazing band, millimetres, TEXT labels
    B  vernacular layers (WALLS/DOORS/WINDOWS), walls as loose LINEs, doors
       as jamb marks, centimetres, MTEXT labels
    C  door symbols as mirrored anonymous blocks on layer 0, windows as named
       blocks, metres, rotated 30 degrees, survey-scale coordinates
    D  inches with no unit declared, dimension strings and furniture clutter

ResPlan's pickle stores geometry on a 256-unit canvas. The metric scale is
recovered as ``sqrt(area / plan_area)``, which puts walls at 0.16-0.30 m on
every plan checked — consistent with real construction, but a derivation, not
a measurement. The ground truth written beside each DXF is exact *for the DXF*.

Usage::

    python tools/corpus/resplan_to_dxf.py <ResPlan.pkl> <out_dir> [--houses N] [--apartments N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
from typing import Dict, List, Sequence, Tuple

import ezdxf
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

ROOM_KEYS = {
    "bedroom": "BEDROOM", "bathroom": "BATH", "kitchen": "KITCHEN",
    "living": "LIVING", "balcony": "BALCONY", "inner": "HALL",
}
UNIT_CODES = {"mm": (4, 1000.0), "cm": (5, 100.0), "m": (6, 1.0), "in": (1, 1 / 0.0254)}
STYLES = {
    "A": {"units": "mm", "rotate": 0.0, "offset": (0.0, 0.0)},
    "B": {"units": "cm", "rotate": 0.0, "offset": (1200.0, -300.0)},
    "C": {"units": "m", "rotate": 30.0, "offset": (512340.0, 4012870.0)},
    "D": {"units": "in", "rotate": 0.0, "offset": (0.0, 0.0), "declare": 0},
}


def polys(geom) -> List[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def plan_scale(plan) -> float:
    parts = []
    for key in list(ROOM_KEYS) + ["wall", "door", "window", "front_door"]:
        parts.extend(polys(plan.get(key)))
    union = unary_union(parts)
    return math.sqrt(float(plan["area"]) / union.area)


def category(plan) -> str:
    outdoor = any(not (plan.get(k) is None or plan[k].is_empty)
                  for k in ("garden", "parking", "pool"))
    return "residential" if outdoor else "apartment"


class Writer:
    def __init__(self, style: str, name: str):
        self.style = style
        cfg = STYLES[style]
        self.name = name
        self.code, self.per_m = UNIT_CODES[cfg["units"]]
        self.rotate = math.radians(cfg["rotate"])
        self.offset = cfg["offset"]
        self.doc = ezdxf.new("R2010", setup=True)
        self.doc.header["$INSUNITS"] = cfg.get("declare", self.code)
        self.msp = self.doc.modelspace()
        layers = {"A": ("A-WALL", "A-DOOR", "A-GLAZ", "A-ANNO-TEXT"),
                  "B": ("WALLS", "DOORS", "WINDOWS", "TEXT"),
                  "C": ("A-WALL-EXT", "0", "A-WIND", "ROOM-NAMES"),
                  "D": ("WALL", "DOOR", "WINDOW", "ANNO")}[style]
        self.L_WALL, self.L_DOOR, self.L_WIN, self.L_TEXT = layers
        for layer in set(layers) | {"A-ANNO-DIMS", "A-FURN"}:
            if layer not in self.doc.layers:
                self.doc.layers.add(layer)
        self.truth: Dict[str, object] = {"openings": [], "rooms": [], "walls": []}

    # metres (plan frame) -> metres (drawing frame, rotated/offset)
    def m(self, x: float, y: float) -> Tuple[float, float]:
        c, s = math.cos(self.rotate), math.sin(self.rotate)
        return (x * c - y * s + self.offset[0], x * s + y * c + self.offset[1])

    def u(self, x: float, y: float) -> Tuple[float, float]:
        mx, my = self.m(x, y)
        return (mx * self.per_m, my * self.per_m)

    def ring(self, coords, layer: str, loose: bool) -> None:
        pts = [self.u(x, y) for x, y in list(coords)[:-1]]
        if len(pts) < 3:
            return
        if loose:
            for i in range(len(pts)):
                self.msp.add_line(pts[i], pts[(i + 1) % len(pts)], dxfattribs={"layer": layer})
        else:
            self.msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})

    def text(self, x: float, y: float, s: str, h: float = 0.22) -> None:
        if self.style == "B":
            t = self.msp.add_mtext(s, dxfattribs={"layer": self.L_TEXT,
                                                  "char_height": h * self.per_m})
            t.set_location(self.u(x, y), attachment_point=5)
        else:
            self.msp.add_text(s, height=h * self.per_m,
                              dxfattribs={"layer": self.L_TEXT}).set_placement(self.u(x, y))

    def save(self, out_dir: str) -> str:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, self.name)
        self.doc.saveas(path)
        with open(path[:-4] + ".truth.json", "w", encoding="utf-8") as fh:
            json.dump(self.truth, fh, indent=1)
        return path


def _long_axis(p: Polygon) -> Tuple[Tuple[float, float], float, float, float]:
    rect = p.minimum_rotated_rectangle
    c = list(rect.exterior.coords)
    e1, e2 = math.dist(c[0], c[1]), math.dist(c[1], c[2])
    if e1 >= e2:
        a, b, w, t = c[0], c[1], e1, e2
    else:
        a, b, w, t = c[1], c[2], e2, e1
    ang = math.atan2(b[1] - a[1], b[0] - a[0])
    return (rect.centroid.x, rect.centroid.y), ang, w, t


def convert(plan, style: str, name: str, out_dir: str) -> str:
    w = Writer(style, name)
    draw(plan, w)
    return w.save(out_dir)


def draw(plan, w: Writer) -> None:
    """Draw one plan into ``w``'s document and fill ``w.truth``.

    ``w.rotate`` and ``w.offset`` place it; several writers sharing one
    document put several plans on one sheet (see ``compose_site.py``).
    """
    style = w.style
    s = plan_scale(plan)
    def scale(g):
        if g is None or g.is_empty:
            return Polygon()
        return affinity.scale(g, xfact=s, yfact=s, origin=(0, 0))
    cat = category(plan)
    walls = scale(plan["wall"])
    for p in polys(walls):
        w.ring(p.exterior.coords, w.L_WALL, loose=(style == "B"))
        for hole in p.interiors:
            w.ring(hole.coords, w.L_WALL, loose=(style == "B"))
    w.truth["walls"] = [{"polygon": [list(w.m(x, y)) for x, y in p.exterior.coords[:-1]],
                         "area_m2": round(p.area, 4)} for p in polys(walls)]
    w.truth["wall_thickness_m"] = round(float(plan["wall_depth"]) * s, 4)

    anon = 0
    for key, kind in (("door", "door"), ("front_door", "door"), ("window", "window")):
        for p in polys(scale(plan.get(key))):
            (cx, cy), ang, width, thick = _long_axis(p)
            if width < 0.3:
                continue
            w.truth["openings"].append({"kind": kind, "centre": list(w.m(cx, cy)),
                                        "width": round(width, 4)})
            d = (math.cos(ang), math.sin(ang))
            n = (-d[1], d[0])
            hs, ht = width / 2.0, thick / 2.0
            corners = [(cx - d[0] * hs - n[0] * ht, cy - d[1] * hs - n[1] * ht),
                       (cx + d[0] * hs - n[0] * ht, cy + d[1] * hs - n[1] * ht),
                       (cx + d[0] * hs + n[0] * ht, cy + d[1] * hs + n[1] * ht),
                       (cx - d[0] * hs + n[0] * ht, cy - d[1] * hs + n[1] * ht)]
            hinge = (cx - d[0] * hs, cy - d[1] * hs)
            deg = math.degrees(ang) + math.degrees(w.rotate)
            if kind == "window":
                if style == "C":
                    wname = "WIN-%03d" % int(round(width * 100))
                    if wname not in w.doc.blocks:
                        blk = w.doc.blocks.new(wname)
                        L, T = width * w.per_m, max(thick, 0.1) * w.per_m
                        blk.add_lwpolyline([(0, -T / 2), (L, -T / 2), (L, T / 2), (0, T / 2)],
                                           close=True, dxfattribs={"layer": "0"})
                        blk.add_line((0, 0), (L, 0), dxfattribs={"layer": "0"})
                    w.msp.add_blockref(wname, w.u(*hinge), dxfattribs={"layer": w.L_WIN,
                                                                        "rotation": deg})
                else:
                    w.msp.add_lwpolyline([w.u(*q) for q in corners], close=True,
                                         dxfattribs={"layer": w.L_WIN})
                    w.msp.add_line(w.u(cx - d[0] * hs, cy - d[1] * hs),
                                   w.u(cx + d[0] * hs, cy + d[1] * hs),
                                   dxfattribs={"layer": w.L_WIN})
                continue
            if style in ("A", "D"):
                w.msp.add_lwpolyline([w.u(*q) for q in corners], close=True,
                                     dxfattribs={"layer": w.L_DOOR})
                r = width * w.per_m
                w.msp.add_arc(w.u(*hinge), r, deg, deg + 90.0, dxfattribs={"layer": w.L_DOOR})
                tip = (hinge[0] + n[0] * width, hinge[1] + n[1] * width)
                w.msp.add_line(w.u(*hinge), w.u(*tip), dxfattribs={"layer": w.L_DOOR})
            elif style == "B":
                mark = 0.05
                for end, inward in ((-1, 1), (1, -1)):
                    ex = cx + d[0] * hs * end
                    ey = cy + d[1] * hs * end
                    a0 = (ex, ey)
                    a1 = (ex + d[0] * mark * inward, ey + d[1] * mark * inward)
                    pts = [(a0[0] - n[0] * ht, a0[1] - n[1] * ht), (a1[0] - n[0] * ht, a1[1] - n[1] * ht),
                           (a1[0] + n[0] * ht, a1[1] + n[1] * ht), (a0[0] + n[0] * ht, a0[1] + n[1] * ht)]
                    w.msp.add_lwpolyline([w.u(*q) for q in pts], close=True,
                                         dxfattribs={"layer": w.L_DOOR})
            else:
                anon += 1
                bname = "A$C%08X" % (0x5EED0000 + int(round(width * 100)))
                if bname not in w.doc.blocks:
                    blk = w.doc.blocks.new(bname)
                    r = width * w.per_m
                    blk.add_arc((0, 0), r, 0, 90, dxfattribs={"layer": "0"})
                    blk.add_line((0, 0), (0, r), dxfattribs={"layer": "0"})
                if anon % 2:
                    w.msp.add_blockref(bname, w.u(*hinge), dxfattribs={"layer": "0", "rotation": deg})
                else:
                    far = (cx + d[0] * hs, cy + d[1] * hs)
                    w.msp.add_blockref(bname, w.u(*far), dxfattribs={
                        "layer": "0", "rotation": deg + 180.0, "yscale": -1.0})

    # The building is what its walls enclose. ResPlan's ``inner`` and balcony
    # polygons can reach well outside the walls (a shared landing, a terrace),
    # and counting those as footprint made the truth larger than the building.
    shell = unary_union([p for key in ("wall", "door", "window", "front_door")
                         for p in polys(scale(plan.get(key)))])
    shell = shell.buffer(0.05).buffer(-0.05)
    fp = unary_union([Polygon(p.exterior) for p in polys(shell)])

    for key, label in ROOM_KEYS.items():
        for p in polys(scale(plan.get(key))):
            if p.area < 1.0:
                continue
            pt = p.representative_point()
            inside = fp.buffer(0.1).contains(pt)
            w.truth["rooms"].append({"type": key, "label": label, "area_m2": round(p.area, 3),
                                     "point": list(w.m(pt.x, pt.y)),
                                     "enclosed": bool(inside),
                                     "exterior": key == "balcony" or not inside})
            w.text(pt.x, pt.y, label)
    w.truth["footprint"] = [[list(w.m(x, y)) for x, y in p.exterior.coords[:-1]]
                            for p in polys(fp)]
    w.truth["footprint_area_m2"] = round(fp.area, 3)
    b = fp.bounds
    w.truth["extent_m"] = [round(b[2] - b[0], 3), round(b[3] - b[1], 3)]
    w.truth["expect"] = {"buildings": 1, "levels": 1}
    w.truth["units"] = STYLES[style]["units"]
    w.truth["frame"] = "drawing"

    if style == "D":
        ox, oy, _, _ = fp.bounds
        _x0, _y0, x1, y1 = fp.bounds
        dim = w.msp.add_linear_dim(base=w.u(ox, oy - 1.0), p1=w.u(ox, oy), p2=w.u(x1, oy),
                                   dxfattribs={"layer": "A-ANNO-DIMS"})
        dim.render()
        dim = w.msp.add_linear_dim(base=w.u(ox - 1.0, oy), p1=w.u(ox, oy), p2=w.u(ox, y1),
                                   angle=90, dxfattribs={"layer": "A-ANNO-DIMS"})
        dim.render()
        for p in polys(scale(plan.get("bedroom")))[:2]:
            c = p.centroid
            w.msp.add_lwpolyline([w.u(c.x - 0.8, c.y - 1.0), w.u(c.x + 0.8, c.y - 1.0),
                                  w.u(c.x + 0.8, c.y + 1.0), w.u(c.x - 0.8, c.y + 1.0)],
                                 close=True, dxfattribs={"layer": "A-FURN"})
    w.truth["source"] = {"dataset": "ResPlan", "plan_id": int(plan["id"]),
                         "license": "CC BY 4.0", "category": cat, "style": style,
                         "scale_m_per_canvas_unit": round(s, 6)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pickle")
    ap.add_argument("out_dir")
    ap.add_argument("--houses", type=int, default=22)
    ap.add_argument("--apartments", type=int, default=12)
    args = ap.parse_args()
    plans = pickle.load(open(args.pickle, "rb"))
    split_path = os.path.join(os.path.dirname(args.pickle), "split.json")
    order = list(range(len(plans)))
    if os.path.exists(split_path):
        split = json.load(open(split_path))
        test_ids = set(int(i) for i in split.get("test", []))
        order = [i for i, p in enumerate(plans) if int(p["id"]) in test_ids]
    chosen = {"residential": [], "apartment": []}
    want = {"residential": args.houses, "apartment": args.apartments}
    for i in sorted(order, key=lambda k: hashlib.sha1(str(plans[k]["id"]).encode()).hexdigest()):
        plan = plans[i]
        cat = category(plan)
        if len(chosen[cat]) >= want[cat]:
            continue
        rooms = sum(len(polys(plan.get(k))) for k in ROOM_KEYS)
        doors = len(polys(plan.get("door"))) + len(polys(plan.get("front_door")))
        if rooms < 4 or doors < 2 or not float(plan.get("area") or 0):
            continue
        try:
            s = plan_scale(plan)
        except Exception:
            continue
        if not (0.14 <= float(plan["wall_depth"]) * s <= 0.35):
            continue          # an augmented or implausibly scaled plan
        chosen[cat].append(plan)
        if all(len(chosen[c]) >= want[c] for c in want):
            break
    manifest = []
    styles = "ABCD"
    for cat, items in chosen.items():
        for k, plan in enumerate(items):
            style = styles[k % 4]
            name = "resplan_%s_%05d_%s.dxf" % (cat[:3], int(plan["id"]), style)
            path = convert(plan, style, name, os.path.join(args.out_dir, cat))
            manifest.append({"file": os.path.relpath(path, args.out_dir).replace("\\", "/"),
                             "category": cat, "source": "ResPlan", "license": "CC BY 4.0",
                             "plan_id": int(plan["id"]), "style": style,
                             "units": STYLES[style]["units"], "native_cad": False})
            print("wrote", path)
    with open(os.path.join(args.out_dir, "resplan_manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
