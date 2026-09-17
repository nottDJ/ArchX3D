"""
Compose multi-building sheets and site plans from real plans on real sites.

What this is, honestly
----------------------
No legally redistributable CAD site plan with several drawn buildings and
known ground truth was found, so these sheets are *composed*:

* the **buildings** are real house layouts from ResPlan (CC BY 4.0), drawn by
  :func:`resplan_to_dxf.draw` exactly as the single-plan corpus is — walls,
  doors, windows, room names — each a different plan;
* the **arrangement** is real: each plan is placed at the centroid and along
  the orientation of a real house footprint from OpenStreetMap (ODbL,
  (c) OpenStreetMap contributors), so the spacing, orientation and street
  geometry between buildings are what a real street has;
* on **site plans** the real context is drawn the way a site plan draws it:
  road edges, the neighbouring buildings as single outlines, trees, a
  property line, a north arrow and a title.

A slot is used only when its plan fits: a plan that would overlap another
placed plan, or a road, is skipped rather than squeezed. The ground truth is
exact for the sheet: every opening, room and wall of every placed plan, the
footprints, and ``expect`` = one building (one storey) per placed plan.

Usage::

    python tools/corpus/compose_site.py <ResPlan.pkl> <osm.json> <out.dxf>
        --style A --buildings 3 [--site] [--labels] [--skip N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import sys
from typing import Dict, List, Tuple

from shapely import affinity
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import resplan_to_dxf as RP  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROAD_WIDTH = {"motorway": 12.0, "primary": 9.0, "secondary": 8.0, "tertiary": 7.0,
              "residential": 5.5, "unclassified": 5.0, "living_street": 4.5,
              "service": 3.5, "track": 3.0, "cycleway": 2.0, "footway": 1.5,
              "path": 1.2, "pedestrian": 4.0}
CLEARANCE = 2.5


def load_osm(path: str):
    doc = json.load(open(path))
    lat0, lon0 = doc["center"]
    kx = math.cos(math.radians(lat0)) * 111320.0
    ky = 110540.0
    nodes = {e["id"]: ((e["lon"] - lon0) * kx, (e["lat"] - lat0) * ky)
             for e in doc["elements"] if e["type"] == "node" and "lat" in e}
    buildings, roads, trees = [], [], []
    for e in doc["elements"]:
        if e["type"] == "node" and e.get("tags", {}).get("natural") == "tree":
            trees.append(nodes.get(e["id"]))
        if e["type"] != "way":
            continue
        pts = [nodes[n] for n in e.get("nodes", []) if n in nodes]
        tags = e.get("tags", {})
        if "building" in tags and len(pts) >= 4 and pts[0] == pts[-1]:
            p = Polygon(pts).buffer(0)
            if p.area > 8.0:
                buildings.append(p)
        elif "highway" in tags and len(pts) >= 2:
            roads.append((LineString(pts), ROAD_WIDTH.get(tags["highway"], 4.0)))
    return buildings, roads, [t for t in trees if t]


def slots(buildings: List[Polygon]) -> List[Tuple[Tuple[float, float], float]]:
    """House-sized footprints nearest the centre: (centroid, orientation)."""
    out = []
    for b in sorted(buildings, key=lambda p: p.centroid.distance(Point(0, 0))):
        if not (50.0 <= b.area <= 300.0):
            continue
        rect = b.minimum_rotated_rectangle
        c = list(rect.exterior.coords)
        e1, e2 = math.dist(c[0], c[1]), math.dist(c[1], c[2])
        a, bb = (c[0], c[1]) if e1 >= e2 else (c[1], c[2])
        angle = math.degrees(math.atan2(bb[1] - a[1], bb[0] - a[0]))
        out.append(((b.centroid.x, b.centroid.y), angle))
    return out


def house_plans(pickle_path: str, salt: str):
    plans = pickle.load(open(pickle_path, "rb"))
    used = set()
    manifest = os.path.join(ROOT, "tests", "corpus", "resplan_manifest.json")
    if os.path.exists(manifest):
        used = {int(m["plan_id"]) for m in json.load(open(manifest))}
    out = []
    for p in sorted(plans, key=lambda p: hashlib.sha1((str(p["id"]) + salt).encode()).hexdigest()):
        if int(p["id"]) in used or RP.category(p) != "residential":
            continue
        rooms = sum(len(RP.polys(p.get(k))) for k in RP.ROOM_KEYS)
        doors = len(RP.polys(p.get("door"))) + len(RP.polys(p.get("front_door")))
        if rooms < 4 or doors < 2 or not float(p.get("area") or 0):
            continue
        try:
            s = RP.plan_scale(p)
        except Exception:
            continue
        if not (0.14 <= float(p["wall_depth"]) * s <= 0.35):
            continue
        out.append(p)
    return out


def outline(plan) -> Polygon:
    s = RP.plan_scale(plan)
    shell = unary_union([q for key in ("wall", "door", "window", "front_door")
                         for q in RP.polys(affinity.scale(plan.get(key), xfact=s, yfact=s,
                                                          origin=(0, 0))
                                           if plan.get(key) is not None else None)])
    return unary_union([Polygon(q.exterior) for q in RP.polys(shell.buffer(0.05).buffer(-0.05))])


def compose(pickle_path: str, osm_path: str, out_path: str, style: str, count: int,
            site: bool, labels: bool, skip: int) -> dict:
    buildings, roads, trees = load_osm(osm_path)
    road_area = unary_union([ln.buffer(w / 2.0) for ln, w in roads]) if roads else Polygon()
    candidates = house_plans(pickle_path, os.path.basename(out_path))
    name = os.path.basename(out_path)

    base = RP.Writer(style, name)
    doc, msp = base.doc, base.msp
    for layer in ("C-ROAD", "C-BLDG-EXST", "L-PLNT-TREE", "V-PROP-LINE", "A-ANNO-TTLB",
                  "A-ANNO-SYMB"):
        if layer not in doc.layers:
            doc.layers.add(layer)
    offset = RP.STYLES[style]["offset"]

    placed: List[Tuple[Polygon, RP.Writer, dict]] = []
    plan_iter = iter(candidates)
    for centre, angle in slots(buildings)[skip:]:
        if len(placed) >= count:
            break
        plan = next(plan_iter)
        shape = outline(plan)
        if shape.is_empty:
            continue
        rot = affinity.rotate(shape, angle, origin=(0, 0))
        dx, dy = centre[0] - rot.centroid.x, centre[1] - rot.centroid.y
        where = affinity.translate(rot, dx, dy)
        if any(where.distance(p) < CLEARANCE for p, _w, _pl in placed):
            continue
        if not road_area.is_empty and where.intersects(road_area):
            continue
        w = RP.Writer(style, name)
        w.doc, w.msp = doc, msp
        w.rotate = math.radians(angle)
        w.offset = (dx + offset[0], dy + offset[1])
        RP.draw(plan, w)
        placed.append((where, w, plan))
    if len(placed) < 2:
        raise SystemExit("fewer than two plans fit on %s" % osm_path)

    truth: Dict[str, object] = {"openings": [], "rooms": [], "walls": [], "footprint": []}
    for i, (where, w, plan) in enumerate(placed):
        for key in ("openings", "rooms", "walls", "footprint"):
            for item in w.truth.get(key, []):
                if isinstance(item, dict):
                    item = dict(item, building=i)
                truth[key].append(item)
    thick = sorted(float(w.truth["wall_thickness_m"]) for _p, w, _pl in placed)
    truth["wall_thickness_m"] = thick[len(thick) // 2]
    truth["footprint_area_m2"] = round(sum(float(w.truth["footprint_area_m2"])
                                           for _p, w, _pl in placed), 3)
    truth["expect"] = {"buildings": len(placed), "levels": len(placed)}
    truth["units"] = RP.STYLES[style]["units"]
    truth["frame"] = "drawing"

    def u(p):
        """Site metres -> drawing units (the site context is not rotated)."""
        return ((p[0] + offset[0]) * base.per_m, (p[1] + offset[1]) * base.per_m)

    extent = unary_union([p for p, _w, _pl in placed]).buffer(12.0).envelope
    if site:
        window = extent.buffer(25.0).envelope
        if not road_area.is_empty:
            for part in RP.polys(road_area.intersection(window)):
                for ring in [part.exterior] + list(part.interiors):
                    msp.add_lwpolyline([u(q) for q in ring.coords], close=True,
                                       dxfattribs={"layer": "C-ROAD"})
        taken = unary_union([p.buffer(1.0) for p, _w, _pl in placed])
        for b in buildings:
            if b.intersects(window) and not b.intersects(taken):
                msp.add_lwpolyline([u(q) for q in b.exterior.coords[:-1]], close=True,
                                   dxfattribs={"layer": "C-BLDG-EXST"})
        for t in trees:
            if window.contains(Point(t)) and not taken.contains(Point(t)):
                msp.add_circle(u(t), 2.0 * base.per_m, dxfattribs={"layer": "L-PLNT-TREE"})
        msp.add_lwpolyline([u(q) for q in extent.exterior.coords[:-1]], close=True,
                           dxfattribs={"layer": "V-PROP-LINE"})
        x0, y0, x1, _y1 = window.bounds
        msp.add_text("SITE PLAN", height=1.2 * base.per_m,
                     dxfattribs={"layer": "A-ANNO-TTLB"}).set_placement(u(((x0 + x1) / 2, y0 - 4.0)))
        msp.add_text("SCALE 1:500", height=0.6 * base.per_m,
                     dxfattribs={"layer": "A-ANNO-TTLB"}).set_placement(u(((x0 + x1) / 2, y0 - 6.0)))
        nx, ny = x1 - 6.0, y0 - 5.0
        msp.add_line(u((nx, ny)), u((nx, ny + 4.0)), dxfattribs={"layer": "A-ANNO-SYMB"})
        msp.add_line(u((nx - 1.0, ny + 3.0)), u((nx, ny + 4.0)), dxfattribs={"layer": "A-ANNO-SYMB"})
        msp.add_line(u((nx + 1.0, ny + 3.0)), u((nx, ny + 4.0)), dxfattribs={"layer": "A-ANNO-SYMB"})
        msp.add_text("N", height=0.8 * base.per_m,
                     dxfattribs={"layer": "A-ANNO-SYMB"}).set_placement(u((nx - 0.3, ny + 4.6)))
    if labels:
        for i, (where, _w, _pl) in enumerate(placed):
            bx0, by0, bx1, _by1 = where.bounds
            msp.add_text("BLOCK %s" % "ABCDEFGH"[i], height=0.7 * base.per_m,
                         dxfattribs={"layer": "A-ANNO-TTLB"}).set_placement(
                u(((bx0 + bx1) / 2.0 - 1.5, by0 - 2.0)))

    osm_name = os.path.splitext(os.path.basename(osm_path))[0]
    truth["source"] = {
        "dataset": "ResPlan + OpenStreetMap (composed)",
        "plan_ids": [int(pl["id"]) for _p, _w, pl in placed],
        "osm_site": osm_name,
        "license": "plans CC BY 4.0 (ResPlan); site geometry ODbL (c) OpenStreetMap contributors",
        "category": "site_plan" if site else "multi_building",
        "style": style, "native_cad": False,
        "tags": ["multi_building", "composed"] + (["site_plan"] if site else []),
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    doc.saveas(out_path)
    with open(out_path[:-4] + ".truth.json", "w", encoding="utf-8") as fh:
        json.dump(truth, fh, indent=1)
    return truth


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pickle")
    ap.add_argument("osm")
    ap.add_argument("out")
    ap.add_argument("--style", default="A", choices=sorted(RP.STYLES))
    ap.add_argument("--buildings", type=int, default=3)
    ap.add_argument("--site", action="store_true")
    ap.add_argument("--labels", action="store_true")
    ap.add_argument("--skip", type=int, default=0, help="slots to pass over first")
    args = ap.parse_args()
    t = compose(args.pickle, args.osm, args.out, args.style, args.buildings, args.site,
                args.labels, args.skip)
    print("wrote %s: %d buildings, %d openings, %d rooms"
          % (args.out, t["expect"]["buildings"], len(t["openings"]), len(t["rooms"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
