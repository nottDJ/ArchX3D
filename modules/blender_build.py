"""
ArchX3D — 3D construction from the validated 2D building model
==============================================================
Runs inside Blender. Takes ``building.json`` — the output of
:mod:`modules.recon.pipeline` — and extrudes it. Nothing here decides what a
wall is, where a room is, or how thick anything should be: every one of those
questions was answered and validated before this module was called, and its
only job is to turn the answers into meshes.

Why this replaces the old geometry step
---------------------------------------
The generator this supersedes read a flat list of line segments and:

* extruded **every segment** into its own slab, so a wall drawn as two
  parallel lines became two thin sheets with a gap between them;
* gave every wall the **one thickness** in ``config.json``, so the drawing's
  own 100 mm and 150 mm walls both came out at 150 mm;
* made the floor a **rectangle around the bounding box**, so an L-shaped house
  with a garage and a porch got a floor over the garden as well;
* left walls **solid** and placed door meshes in front of them.

All four are fixed by construction here, not by patching: walls come from wall
records that already have a centreline and a measured thickness, floors come
from room polygons, and openings are holes.

Openings are holes, and no booleans
-----------------------------------
A wall with openings is built as the solid parts that remain: the piers
between openings, the lintel over each one, and the sill block under each
window. That is exact, it is manifold, and it is fast — a boolean modifier per
opening on a hundred-wall building is neither reliable nor quick, and it is
the usual reason a "3D floor plan" ships with doors painted on.

Buildings and storeys
---------------------
``building.json`` is a drawing of one or more buildings, each of one or more
storeys. Every storey is built from its own walls, rooms and openings exactly
as for a single plan, and its objects are then moved by the two numbers the
reconstruction recorded for it: its ``placement`` over the storey below and
its ``elevation``. Nothing here decides how storeys stack, which plans are
storeys, or where a building stands — those were settled, with evidence,
before this ran.

Standalone by necessity
-----------------------
Blender's interpreter has bpy but not shapely, ezdxf or numpy-in-our-venv, so
this module imports nothing from :mod:`modules.recon` and reads plain JSON.
The seam is the file.
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

XY = Tuple[float, float]

#: Floors and ceilings are modelled as slabs of this thickness, in metres.
SLAB_THICKNESS = 0.12

#: Slabs sit this far below z=0 so the floor surface is exactly z=0.
FLOOR_TOP_Z = 0.0

#: A pier, lintel or sill narrower than this is dropped, in metres. Below it
#: the piece is a sliver that only adds z-fighting.
MIN_PIECE = 0.004


# ---------------------------------------------------------------------------
# Geometry helpers (no bpy)
# ---------------------------------------------------------------------------

def wall_pieces(length: float, height: float,
                openings: Sequence[Tuple[float, float, float, float]],
                ) -> List[Tuple[float, float, float, float]]:
    """Solid parts of one wall elevation, as ``(u0, u1, z0, z1)`` rectangles.

    ``openings`` are ``(u0, u1, sill, head)``. Overlapping openings are merged
    first, because two overlapping holes are one hole and treating them
    separately leaves a zero-width pier between them that renders as a crack.
    """
    spans: List[List[float]] = []
    for u0, u1, sill, head in sorted(openings):
        u0 = max(0.0, min(length, u0))
        u1 = max(0.0, min(length, u1))
        if u1 - u0 < MIN_PIECE:
            continue
        sill = max(0.0, sill)
        head = min(height, head if head > sill else height)
        if head - sill < MIN_PIECE:
            continue
        if spans and u0 <= spans[-1][1] + MIN_PIECE:
            spans[-1][1] = max(spans[-1][1], u1)
            spans[-1][2] = min(spans[-1][2], sill)
            spans[-1][3] = max(spans[-1][3], head)
        else:
            spans.append([u0, u1, sill, head])

    pieces: List[Tuple[float, float, float, float]] = []
    cursor = 0.0
    for u0, u1, sill, head in spans:
        if u0 - cursor >= MIN_PIECE:
            pieces.append((cursor, u0, 0.0, height))
        if sill >= MIN_PIECE:
            pieces.append((u0, u1, 0.0, sill))          # under a window
        if height - head >= MIN_PIECE:
            pieces.append((u0, u1, head, height))       # lintel over the hole
        cursor = u1
    if length - cursor >= MIN_PIECE:
        pieces.append((cursor, length, 0.0, height))
    return pieces


def _ring_area(pts: Sequence[XY]) -> float:
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def _ensure_ccw(pts: List[XY]) -> List[XY]:
    return pts if _ring_area(pts) >= 0 else list(reversed(pts))


# ---------------------------------------------------------------------------
# Mesh construction (needs bpy)
# ---------------------------------------------------------------------------

def _new_bmesh():
    import bmesh
    return bmesh.new()


def _add_box(bm, corners: Sequence[Tuple[float, float, float]]) -> None:
    """Add an axis-consistent box from its eight corners (bottom then top)."""
    verts = [bm.verts.new(c) for c in corners]
    faces = ((0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1),
             (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0))
    for f in faces:
        try:
            bm.faces.new([verts[i] for i in f])
        except ValueError:
            pass    # duplicate face where two pieces share a plane


def _finish(bm, name: str, material=None, collection=None):
    import bpy
    mesh = bpy.data.meshes.new(name + "Mesh")
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    (collection or bpy.context.collection).objects.link(obj)
    if material is not None:
        obj.data.materials.append(material)
    return obj


def build_walls(building: dict, material=None, *,
                height: Optional[float] = None, name: str = "Walls"):
    """Build every wall as a solid with its openings cut out.

    Walls are welded into one mesh rather than one object each: a residential
    plan has thirty to sixty of them, and thirty objects with thirty material
    slots is a heavier glTF and a slower viewer for no benefit. Provenance is
    kept in the IR, which is where a tool should look for it.
    """
    walls = building.get("walls", [])
    if not walls:
        return None
    default_h = float(height or building.get("height")
                      or building.get("default_wall_height", 2.7))
    openings = building.get("openings", [])
    by_wall: Dict[str, List[dict]] = {}
    for o in openings:
        by_wall.setdefault(o.get("wall_id"), []).append(o)

    # Degree of each wall end, so a wall that meets another can be mitred out
    # to the corner instead of stopping half a thickness short of it.
    ends: Dict[Tuple[int, int], int] = {}

    def key(p: Sequence[float]) -> Tuple[int, int]:
        return (int(round(p[0] / 0.02)), int(round(p[1] / 0.02)))

    for w in walls:
        ends[key(w["start"])] = ends.get(key(w["start"]), 0) + 1
        ends[key(w["end"])] = ends.get(key(w["end"]), 0) + 1

    bm = _new_bmesh()
    built = 0
    holes = 0
    for w in walls:
        sx, sy = float(w["start"][0]), float(w["start"][1])
        ex, ey = float(w["end"][0]), float(w["end"][1])
        dx, dy = ex - sx, ey - sy
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        d = (dx / length, dy / length)
        n = (-d[1], d[0])
        t = float(w.get("thickness", 0.15))
        h = float(w.get("height") or default_h)

        spans = []
        for o in by_wall.get(w["id"], []):
            half = float(o.get("width", 0.0)) / 2.0
            off = float(o.get("offset", 0.0))
            sill = float(o.get("sill_height", 0.0))
            head = sill + float(o.get("height", 2.0))
            spans.append((off - half, off + half, sill, min(head, h)))
        holes += len(spans)

        # Mitre: grow into the junction only where the wall actually meets
        # something and no opening reaches that end, or the growth would seal
        # a doorway that sits right at the corner.
        grow0 = grow1 = 0.0
        touches_start = any(s[0] <= 0.02 for s in spans)
        touches_end = any(s[1] >= length - 0.02 for s in spans)
        if ends.get(key(w["start"]), 0) > 1 and not touches_start:
            grow0 = t / 2.0
        if ends.get(key(w["end"]), 0) > 1 and not touches_end:
            grow1 = t / 2.0

        pieces = wall_pieces(length, h, spans)
        if not pieces:
            continue
        for u0, u1, z0, z1 in pieces:
            if u0 <= MIN_PIECE:
                u0 -= grow0
            if u1 >= length - MIN_PIECE:
                u1 += grow1
            corners = []
            for u, v in ((u0, -t / 2), (u1, -t / 2), (u1, t / 2), (u0, t / 2)):
                corners.append((sx + d[0] * u + n[0] * v,
                                sy + d[1] * u + n[1] * v, z0))
            for u, v in ((u0, -t / 2), (u1, -t / 2), (u1, t / 2), (u0, t / 2)):
                corners.append((sx + d[0] * u + n[0] * v,
                                sy + d[1] * u + n[1] * v, z1))
            _add_box(bm, corners)
        built += 1

    obj = _finish(bm, name, material)
    print("[OK] Walls: %d built, %d openings cut, %d faces"
          % (built, holes, len(obj.data.polygons)))
    return obj


def _slab(bm, outer: Sequence[XY], holes: Sequence[Sequence[XY]],
          z_bottom: float, z_top: float) -> bool:
    """Add a prism between two z planes over a polygon with holes."""
    from mathutils import Vector
    from mathutils.geometry import tessellate_polygon

    outer = _ensure_ccw([(float(x), float(y)) for x, y in outer])
    if len(outer) < 3:
        return False
    contours = [[Vector((x, y, 0.0)) for x, y in outer]]
    for hole in holes or ():
        ring = [(float(x), float(y)) for x, y in hole]
        if len(ring) >= 3:
            contours.append([Vector((x, y, 0.0)) for x, y in ring])
    try:
        tris = tessellate_polygon(contours)
    except Exception:
        return False
    if not tris:
        return False

    flat: List[XY] = []
    for ring in contours:
        flat.extend((v.x, v.y) for v in ring)

    bottom = [bm.verts.new((x, y, z_bottom)) for x, y in flat]
    top = [bm.verts.new((x, y, z_top)) for x, y in flat]
    for a, b, c in tris:
        try:
            bm.faces.new((bottom[a], bottom[c], bottom[b]))
        except (ValueError, IndexError):
            pass
        try:
            bm.faces.new((top[a], top[b], top[c]))
        except (ValueError, IndexError):
            pass
    # Sides, ring by ring, so the slab is closed rather than two loose sheets.
    base = 0
    for ring in contours:
        n = len(ring)
        for i in range(n):
            j = (i + 1) % n
            try:
                bm.faces.new((bottom[base + i], bottom[base + j],
                              top[base + j], top[base + i]))
            except (ValueError, IndexError):
                pass
        base += n
    return True


def build_floors(building: dict, material=None, *, name: str = "Floor"):
    """One slab per room, plus the envelope, following the actual outlines.

    The footprint is used for the slab rather than a bounding box, so an
    L-shaped plan gets an L-shaped floor and the garden does not get paved.
    ``footprint_parts`` is honoured, so a detached garage on the same sheet
    gets its own slab instead of one rectangle swallowing the space between.
    """
    parts = building.get("footprint_parts") or (
        [building.get("footprint")] if building.get("footprint") else [])
    if not parts:
        parts = [[(r["polygon"]) for r in building.get("rooms", [])]]
    bm = _new_bmesh()
    made = 0
    for i, ring in enumerate(parts):
        if not ring or len(ring) < 3:
            continue
        holes = building.get("footprint_holes", []) if i == 0 else []
        if _slab(bm, ring, holes, FLOOR_TOP_Z - SLAB_THICKNESS, FLOOR_TOP_Z):
            made += 1
    if not made:
        bm.free()
        return None
    obj = _finish(bm, name, material)
    print("[OK] Floor: %d slab(s) from the building envelope" % made)
    return obj


def room_floor_material(room: dict, by_type: Optional[Dict[str, object]] = None,
                        fallback=None):
    """The material a room's floor surface is built with.

    A material for the room's type when the caller has one, else the storey's
    own floor. Never nothing: a room floor with no material exports as glTF's
    default — white, fully metallic, fully rough — and the viewer drew every
    room as grey metal laid a millimetre over the finished slab.
    """
    if by_type:
        chosen = by_type.get(room.get("room_type")) or by_type.get("default")
        if chosen is not None:
            return chosen
    return fallback


def build_room_floors(building: dict, materials: Optional[Dict[str, object]] = None,
                      *, prefix: str = "RoomFloor", fallback=None):
    """A thin per-room surface, so rooms can be coloured and picked separately.

    Sits a millimetre above the envelope slab. Kept separate from it because a
    viewer wants to highlight "the kitchen", and a single welded floor cannot
    answer which part that is. ``fallback`` is the storey's floor material; see
    :func:`room_floor_material`.
    """
    made = []
    for room in building.get("rooms", []):
        poly = room.get("polygon") or []
        if len(poly) < 3:
            continue
        bm = _new_bmesh()
        if not _slab(bm, poly, room.get("holes", []), FLOOR_TOP_Z + 0.001,
                     FLOOR_TOP_Z + 0.004):
            bm.free()
            continue
        mat = room_floor_material(room, materials, fallback)
        obj = _finish(bm, "%s_%s" % (prefix, room.get("id", "r")), mat)
        obj["archx3d_room_id"] = room.get("id", "")
        obj["archx3d_room_label"] = room.get("label") or ""
        obj["archx3d_room_type"] = room.get("room_type", "unknown")
        obj["archx3d_room_area"] = float(room.get("area", 0.0))
        made.append(obj)
    print("[OK] Room floors: %d" % len(made))
    return made


def build_ceilings(building: dict, material=None, *,
                   height: Optional[float] = None, name: str = "Ceiling"):
    """A ceiling over every interior room. Porches and decks get none.

    Roofing a covered deck at storey height would seal the model's daylight
    out and misrepresent the building; an exterior room is exterior.
    """
    z = float(height or building.get("height")
              or building.get("default_wall_height", 2.7))
    bm = _new_bmesh()
    made = 0
    for room in building.get("rooms", []):
        if room.get("is_exterior"):
            continue
        poly = room.get("polygon") or []
        if len(poly) < 3:
            continue
        if _slab(bm, poly, room.get("holes", []), z, z + SLAB_THICKNESS / 2.0):
            made += 1
    if not made:
        bm.free()
        return None
    obj = _finish(bm, name, material)
    print("[OK] Ceilings: %d room(s)" % made)
    return obj


def opening_fill(opening: dict) -> Optional[str]:
    """What goes in a hole: ``"glass"``, ``"leaf"`` or ``None`` (nothing).

    The certainty tier decides, when the model carries one: a leaf only in a
    door the drawing confirms. A probable door or an unknown opening stays an
    open hole — a false leaf would wall off a passage the drawing may never
    have closed. A model written before tiers existed falls back on the kind.
    """
    kind = opening.get("kind")
    tier = opening.get("classification")
    if kind == "window" and tier in (None, "window"):
        return "glass"
    if kind in ("door", "garage") and tier in (None, "door", "garage_door"):
        return "leaf"
    return None


def build_opening_frames(building: dict, materials: Optional[Dict[str, object]] = None,
                         suffix: str = ""):
    """Glass in the windows and a slab in each door reveal.

    These sit *inside* the holes the walls already have, which is the whole
    difference from the old behaviour: the wall was solid and a door mesh was
    parked in front of it, so every room was sealed.
    """
    glass = (materials or {}).get("glass")
    leaf = (materials or {}).get("door")
    by_id = {w["id"]: w for w in building.get("walls", [])}
    bm_glass = _new_bmesh()
    bm_leaf = _new_bmesh()
    n_glass = n_leaf = 0
    for o in building.get("openings", []):
        w = by_id.get(o.get("wall_id"))
        if w is None:
            continue
        sx, sy = float(w["start"][0]), float(w["start"][1])
        ex, ey = float(w["end"][0]), float(w["end"][1])
        length = math.hypot(ex - sx, ey - sy)
        if length < 1e-6:
            continue
        d = ((ex - sx) / length, (ey - sy) / length)
        n = (-d[1], d[0])
        off = float(o.get("offset", 0.0))
        half = float(o.get("width", 0.0)) / 2.0
        sill = float(o.get("sill_height", 0.0))
        head = sill + float(o.get("height", 2.0))
        fill = opening_fill(o)
        if fill == "glass":
            depth = 0.01
            bm, count = bm_glass, "glass"
        elif fill == "leaf":
            depth = float(o.get("thickness", 0.1)) * 0.35
            bm, count = bm_leaf, "leaf"
        else:
            continue        # a hole and nothing else
        corners = []
        for u, v in ((off - half, -depth), (off + half, -depth),
                     (off + half, depth), (off - half, depth)):
            corners.append((sx + d[0] * u + n[0] * v, sy + d[1] * u + n[1] * v, sill))
        for u, v in ((off - half, -depth), (off + half, -depth),
                     (off + half, depth), (off - half, depth)):
            corners.append((sx + d[0] * u + n[0] * v, sy + d[1] * u + n[1] * v, head))
        _add_box(bm, corners)
        if count == "glass":
            n_glass += 1
        else:
            n_leaf += 1

    made = []
    if n_glass:
        made.append(_finish(bm_glass, "Glazing" + suffix, glass))
    else:
        bm_glass.free()
    if n_leaf:
        made.append(_finish(bm_leaf, "DoorLeaves" + suffix, leaf))
    else:
        bm_leaf.free()
    print("[OK] Openings: %d glazed, %d leaves" % (n_glass, n_leaf))
    return made


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_building(path: str) -> Optional[dict]:
    """The architectural model from ``building.json``, or ``None``.

    Accepts the current drawing document (``buildings`` → ``levels``) and
    the older single-plan document (``walls`` at the top level), which is one
    building of one storey.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print("[WARN] could not read %s: %s" % (path, exc))
        return None
    if not isinstance(data, dict):
        return None
    if "buildings" in data and isinstance(data["buildings"], list):
        return data
    if "walls" in data:
        return data
    return None


#: Validation outcomes a model may be built from. ``AMBIGUOUS`` is buildable:
#: the geometry is sound and the model is built exactly as drawn, with the
#: open question carried in its review list. ``INVALID`` never is.
BUILDABLE = ("VALID", "VALID_WITH_WARNINGS", "AMBIGUOUS")


def check_buildable(model: Optional[dict]) -> Tuple[bool, str]:
    """Whether Blender may build this model, and if not, why.

    The last gate before geometry is made. The pipeline refuses to write an
    invalid model at all; this refuses again, from the file, so a model that
    reached ``building.json`` by any other route still cannot become a
    beautiful GLB of broken geometry.
    """
    if not model:
        return False, "no architectural model"
    validation = model.get("validation") or {}
    if not validation:
        return False, "the model carries no validation result"
    if not validation.get("ok", False):
        errors = validation.get("errors") or ["validation failed"]
        return False, "validation failed: %s" % errors[0]
    status = validation.get("status")
    if status is not None and status not in BUILDABLE:
        return False, "validation status %s is not buildable" % status
    if not any(l.get("walls") for _b, l in iter_levels(model)):
        return False, "the model has no walls"
    return True, str(status or "VALID")


def iter_levels(model: dict) -> Iterable[Tuple[dict, dict]]:
    """``(building, level)`` for every storey in the model, in build order."""
    if "buildings" in model:
        for b in model.get("buildings") or []:
            for l in sorted(b.get("levels") or [], key=lambda l: l.get("index", 0)):
                yield b, l
    elif "walls" in model:
        yield {"id": "b1", "name": "Building 1"}, model


def level_transform(level: dict) -> Tuple[float, float, float]:
    """Where a storey's drawn geometry goes in the model: ``(dx, dy, z)``."""
    px, py = (level.get("placement") or (0.0, 0.0))[:2]
    return float(px), float(py), float(level.get("elevation", 0.0) or 0.0)


def room_transforms(model: dict) -> Dict[str, Tuple[float, float, float]]:
    """Room id → its storey's transform, for anything placed in a room in 2D."""
    out: Dict[str, Tuple[float, float, float]] = {}
    for _b, l in iter_levels(model):
        t = level_transform(l)
        for r in l.get("rooms", []):
            out[r.get("id")] = t
    return out


def build(model: dict, materials: Optional[Dict[str, object]] = None, *,
          wall_height: Optional[float] = None,
          generate_floor: bool = True,
          generate_ceiling: bool = True) -> dict:
    """Build every storey of every building and report what was made."""
    materials = materials or {}
    storeys = list(iter_levels(model))
    single = len(storeys) == 1
    objects: Dict[str, object] = {}
    counts = {"walls": 0, "rooms": 0, "openings": 0}
    for b, level in storeys:
        # One storey keeps the historical object names, so a single-plan model
        # exports exactly as it always did.
        tag = "" if single else ".%s" % level.get("id", "l")
        made: Dict[str, object] = {}
        made["walls"] = build_walls(level, materials.get("wall"),
                                    height=wall_height, name="Walls" + tag)
        if generate_floor:
            made["floor"] = build_floors(level, materials.get("floor"),
                                         name="Floor" + tag)
            made["room_floors"] = build_room_floors(
                level, materials.get("rooms") if isinstance(
                    materials.get("rooms"), dict) else None,
                fallback=materials.get("floor"))
        if generate_ceiling:
            made["ceiling"] = build_ceilings(level, materials.get("ceiling"),
                                             height=wall_height, name="Ceiling" + tag)
        made["openings"] = build_opening_frames(level, materials, suffix=tag)

        dx, dy, z = level_transform(level)
        kinds = {"walls": "wall", "floor": "floor", "room_floors": "floor",
                 "ceiling": "roof", "openings": "opening"}
        for key, obj in made.items():
            for o in (obj if isinstance(obj, list) else [obj]):
                if o is None:
                    continue
                o.location = (o.location[0] + dx, o.location[1] + dy,
                              o.location[2] + z)
                o["archx3d_kind"] = kinds[key]
                o["archx3d_building"] = b.get("id", "")
                o["archx3d_level"] = level.get("id", "")
                o["archx3d_elevation"] = z
            if key in objects:
                prev = objects[key]
                objects[key] = (prev if isinstance(prev, list) else [prev]) + \
                    (obj if isinstance(obj, list) else [obj])
            else:
                objects[key] = obj
        for k in counts:
            counts[k] += len(level.get(k, []))
        print("[OK] %s %s: z=%.2f placement=(%.2f, %.2f)"
              % (b.get("id", ""), level.get("id", ""), z, dx, dy))

    return {
        **counts,
        "buildings": len({b.get("id") for b, _ in storeys}),
        "levels": len(storeys),
        "objects": {k: (len([o for o in v if o is not None]) if isinstance(v, list)
                        else int(v is not None))
                    for k, v in objects.items()},
        "_objects": objects,
    }


def bounds(model: dict) -> Tuple[float, float, float, float]:
    """Plan extents of the built model, for camera and lighting placement.

    Each storey is measured where it is built — its drawn geometry moved by
    its placement — so a first floor drawn beside the ground floor on the
    sheet does not double the extent of a building it actually sits on.
    """
    xs: List[float] = []
    ys: List[float] = []
    for _b, level in iter_levels(model):
        dx, dy, _z = level_transform(level)
        for w in level.get("walls", []):
            for p in (w["start"], w["end"]):
                xs.append(float(p[0]) + dx)
                ys.append(float(p[1]) + dy)
        for ring in (level.get("footprint_parts") or
                     [level.get("footprint") or []]):
            for p in ring:
                xs.append(float(p[0]) + dx)
                ys.append(float(p[1]) + dy)
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))
