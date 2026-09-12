"""
Generate the synthetic DXF plans the reconstruction regression suite runs on.

Why generated and not collected
-------------------------------
The engine has to be right about things that are hard to find a real drawing
for *and* know the truth of: a plan in feet, the same plan in millimetres, a
plan whose walls only exist inside blocks, a plan buried in dimension strings.
Generating them means the expected wall count, room count and overall size are
known exactly, so a regression test can assert a number instead of "it did not
crash".

The real drawings are the other half of the suite and neither half substitutes
for the other. These fixtures say "the engine handles this construction"; the
real ones say "the engine handles what people actually send".

Every plan here is drawn the way a drafter draws: walls as **two parallel
lines** at a stated thickness, doors and windows as gaps with a header band
over them, room names as text, and — where the test calls for it — a thick
layer of dimensions and annotation that must not become walls.

Run directly to regenerate::

    python tests/fixtures/make_plans.py
"""

from __future__ import annotations

import math
import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import ezdxf

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plans")

XY = Tuple[float, float]

#: Layer set used by every generated plan, in the AIA convention so the
#: classifier is exercised on the names it will actually meet.
LAYERS = {
    "A-WALL": 7,
    "A-DOOR": 3,
    "A-GLAZ": 4,
    "A-HEAD": 6,
    "A-ANNO-DIMS": 1,
    "A-ANNO-TEXT": 2,
    "A-FLOR-PFIX": 5,
    "A-FURN": 8,
}


class Plan:
    """A drawing under construction, in one unit, with a wall thickness."""

    def __init__(self, name: str, *, unit_code: int, per_metre: float,
                 thickness_m: float = 0.15):
        self.name = name
        self.per_metre = per_metre          # drawing units in one metre
        self.t = thickness_m * per_metre
        self.doc = ezdxf.new("R2010", setup=True)
        self.doc.header["$INSUNITS"] = unit_code
        self.msp = self.doc.modelspace()
        for layer, colour in LAYERS.items():
            if layer not in self.doc.layers:
                self.doc.layers.add(layer, color=colour)
        self.openings: List[Tuple[XY, XY, float, str]] = []

    # -- units ----------------------------------------------------------

    def u(self, metres: float) -> float:
        return metres * self.per_metre

    def p(self, x_m: float, y_m: float) -> XY:
        return (self.u(x_m), self.u(y_m))

    # -- walls ----------------------------------------------------------

    def wall(self, a_m: XY, b_m: XY, *, thickness_m: Optional[float] = None,
             holes_m: Sequence[Tuple[float, float, str]] = ()) -> None:
        """A wall drawn as two parallel faces, broken at each opening.

        ``holes_m`` are ``(start, end, kind)`` measured along the wall from
        ``a_m``. Each gets a header band across the wall, which is how a real
        drawing says "the wall continues here, through a hole".
        """
        t = (thickness_m * self.per_metre) if thickness_m else self.t
        ax, ay = self.p(*a_m)
        bx, by = self.p(*b_m)
        length = math.hypot(bx - ax, by - ay)
        if length < 1e-9:
            return
        d = ((bx - ax) / length, (by - ay) / length)
        n = (-d[1], d[0])

        spans = sorted((self.u(s), self.u(e), k) for s, e, k in holes_m)
        for side in (+1, -1):
            off = (n[0] * t / 2 * side, n[1] * t / 2 * side)
            cursor = 0.0
            for s, e, _k in spans:
                if s - cursor > 1e-6:
                    self._line((ax + d[0] * cursor + off[0], ay + d[1] * cursor + off[1]),
                               (ax + d[0] * s + off[0], ay + d[1] * s + off[1]), "A-WALL")
                cursor = e
            if length - cursor > 1e-6:
                self._line((ax + d[0] * cursor + off[0], ay + d[1] * cursor + off[1]),
                           (ax + d[0] * length + off[0], ay + d[1] * length + off[1]), "A-WALL")

        # End caps, so a wall end is closed the way a drawn one is.
        for at in (0.0, length):
            self._line((ax + d[0] * at + n[0] * t / 2, ay + d[1] * at + n[1] * t / 2),
                       (ax + d[0] * at - n[0] * t / 2, ay + d[1] * at - n[1] * t / 2),
                       "A-WALL")

        for s, e, kind in spans:
            self._header(ax, ay, d, n, s, e, t, kind)

    def _header(self, ax, ay, d, n, s, e, t, kind: str) -> None:
        layer = {"door": "A-DOOR", "window": "A-GLAZ"}.get(kind, "A-HEAD")
        pts = [
            (ax + d[0] * s + n[0] * t / 2, ay + d[1] * s + n[1] * t / 2),
            (ax + d[0] * e + n[0] * t / 2, ay + d[1] * e + n[1] * t / 2),
            (ax + d[0] * e - n[0] * t / 2, ay + d[1] * e - n[1] * t / 2),
            (ax + d[0] * s - n[0] * t / 2, ay + d[1] * s - n[1] * t / 2),
        ]
        self.msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
        if kind == "door":
            # A swing arc: radius = leaf width, hinged at the near jamb.
            r = e - s
            self.msp.add_arc(
                center=(ax + d[0] * s, ay + d[1] * s), radius=r,
                start_angle=math.degrees(math.atan2(d[1], d[0])),
                end_angle=math.degrees(math.atan2(d[1], d[0])) + 90.0,
                dxfattribs={"layer": "A-DOOR"})
        elif kind == "window":
            # Three glazing lines across the opening.
            for frac in (-0.25, 0.0, 0.25):
                off = (n[0] * t * frac, n[1] * t * frac)
                self._line((ax + d[0] * s + off[0], ay + d[1] * s + off[1]),
                           (ax + d[0] * e + off[0], ay + d[1] * e + off[1]), "A-GLAZ")

    def _line(self, a: XY, b: XY, layer: str) -> None:
        self.msp.add_line(a, b, dxfattribs={"layer": layer})

    # -- annotation -----------------------------------------------------

    def label(self, at_m: XY, text: str, height_m: float = 0.22) -> None:
        self.msp.add_text(text, height=self.u(height_m),
                          dxfattribs={"layer": "A-ANNO-TEXT"}
                          ).set_placement(self.p(*at_m))

    def dimension(self, a_m: XY, b_m: XY, offset_m: float = 1.0) -> None:
        """A real DIMENSION plus the extension lines that come with it."""
        a, b = self.p(*a_m), self.p(*b_m)
        off = self.u(offset_m)
        dim = self.msp.add_linear_dim(
            base=(a[0], a[1] - off) if abs(a[1] - b[1]) < 1e-9 else (a[0] - off, a[1]),
            p1=a, p2=b, dxfattribs={"layer": "A-ANNO-DIMS"})
        dim.render()

    def note_block(self, at_m: XY, lines: Sequence[str]) -> None:
        for i, text in enumerate(lines):
            self.msp.add_text(
                text, height=self.u(0.14),
                dxfattribs={"layer": "A-ANNO-TEXT"}
            ).set_placement(self.p(at_m[0], at_m[1] - i * 0.22))

    def fixture_block(self, name: str, at_m: XY, size_m: Tuple[float, float],
                      layer: str = "A-FLOR-PFIX") -> None:
        if name not in self.doc.blocks:
            blk = self.doc.blocks.new(name=name)
            w, h = self.u(size_m[0]), self.u(size_m[1])
            blk.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True,
                               dxfattribs={"layer": layer})
            blk.add_circle((w / 2, h / 2), min(w, h) / 3,
                           dxfattribs={"layer": layer})
        self.msp.add_blockref(name, self.p(*at_m), dxfattribs={"layer": layer})

    def save(self) -> str:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, self.name)
        self.doc.saveas(path)
        return path


# ---------------------------------------------------------------------------
# The plans
# ---------------------------------------------------------------------------

def rect_rooms(p: Plan) -> None:
    """A 10 x 7 m rectangle split into three rooms, with doors and windows."""
    p.wall((0, 0), (10, 0), holes_m=[(3.0, 4.5, "window"), (6.5, 8.0, "window")])
    p.wall((10, 0), (10, 7), holes_m=[(2.5, 4.0, "window")])
    p.wall((10, 7), (0, 7), holes_m=[(4.0, 5.0, "door")])
    p.wall((0, 7), (0, 0), holes_m=[(3.0, 4.5, "window")])
    p.wall((4, 0), (4, 7), thickness_m=0.1, holes_m=[(1.0, 1.9, "door")])
    p.wall((4, 4), (10, 4), thickness_m=0.1, holes_m=[(2.0, 2.9, "door")])
    p.label((1.4, 3.2), "BEDROOM 1")
    p.label((6.4, 1.6), "LIVING")
    p.label((6.4, 5.4), "KITCHEN")


def build_simple_rect() -> str:
    p = Plan("t01_simple_rect.dxf", unit_code=6, per_metre=1.0)
    rect_rooms(p)
    return p.save()


def build_l_shape() -> str:
    p = Plan("t02_l_shape.dxf", unit_code=6, per_metre=1.0)
    # Outline: (0,0) -> (12,0) -> (12,5) -> (7,5) -> (7,9) -> (0,9) -> close
    p.wall((0, 0), (12, 0), holes_m=[(4.0, 5.5, "window")])
    p.wall((12, 0), (12, 5), holes_m=[(1.8, 3.3, "window")])
    p.wall((12, 5), (7, 5))
    p.wall((7, 5), (7, 9), holes_m=[(1.2, 2.1, "door")])
    p.wall((7, 9), (0, 9), holes_m=[(2.5, 4.0, "window")])
    p.wall((0, 9), (0, 0), holes_m=[(4.0, 4.9, "door")])
    p.wall((0, 5), (7, 5), thickness_m=0.1, holes_m=[(3.0, 3.9, "door")])
    p.wall((4, 0), (4, 5), thickness_m=0.1, holes_m=[(1.5, 2.4, "door")])
    p.label((1.6, 7.0), "LIVING")
    p.label((1.6, 2.2), "BEDROOM")
    p.label((8.0, 2.2), "KITCHEN")


    return p.save()


def build_multi_room() -> str:
    """Three bedrooms and two bathrooms off a corridor."""
    p = Plan("t03_multi_room.dxf", unit_code=6, per_metre=1.0)
    W, H = 14.0, 9.0
    p.wall((0, 0), (W, 0), holes_m=[(2.0, 3.5, "window"), (7.0, 8.5, "window"),
                                    (11.0, 12.5, "window")])
    p.wall((W, 0), (W, H), holes_m=[(3.5, 5.0, "window")])
    p.wall((W, H), (0, H), holes_m=[(2.0, 3.5, "window"), (9.0, 10.5, "window")])
    p.wall((0, H), (0, 0), holes_m=[(4.0, 4.9, "door")])
    # corridor between y=4.0 and y=5.2
    p.wall((0, 4.0), (W, 4.0), thickness_m=0.1,
           holes_m=[(2.0, 2.9, "door"), (6.0, 6.9, "door"), (10.5, 11.4, "door")])
    p.wall((0, 5.2), (W, 5.2), thickness_m=0.1,
           holes_m=[(3.0, 3.9, "door"), (8.0, 8.9, "door")])
    p.wall((4.5, 0), (4.5, 4.0), thickness_m=0.1)
    p.wall((9.0, 0), (9.0, 4.0), thickness_m=0.1)
    p.wall((5.5, 5.2), (5.5, H), thickness_m=0.1)
    p.wall((10.0, 5.2), (10.0, H), thickness_m=0.1)
    p.label((1.5, 1.8), "BEDROOM 1")
    p.label((6.0, 1.8), "BEDROOM 2")
    p.label((10.5, 1.8), "BATH")
    p.label((1.8, 7.0), "BEDROOM 3")
    p.label((7.0, 7.0), "LIVING")
    p.label((11.2, 7.0), "BATH 2")
    p.label((6.5, 4.4), "HALL")
    p.fixture_block("P-WC", (10.6, 0.6), (0.4, 0.7))
    p.fixture_block("P-BATH", (11.4, 2.2), (0.8, 1.7))
    return p.save()


def build_garage_porch() -> str:
    """House with an attached garage and a covered porch — no walls on the porch."""
    p = Plan("t04_garage_porch.dxf", unit_code=6, per_metre=1.0)
    p.wall((0, 0), (9, 0), holes_m=[(3.0, 4.5, "window")])
    p.wall((9, 0), (9, 8), holes_m=[(2.0, 4.4, "door")])       # into the garage
    p.wall((9, 8), (0, 8), holes_m=[(3.0, 4.5, "window")])
    p.wall((0, 8), (0, 0), holes_m=[(3.5, 4.4, "door")])
    p.wall((0, 4), (9, 4), thickness_m=0.1, holes_m=[(4.0, 4.9, "door")])
    # Garage: 6 x 8, sharing the 9.0 wall, with a 4.8 m garage door.
    p.wall((9, 0), (15, 0), holes_m=[(0.6, 5.4, "door")])
    p.wall((15, 0), (15, 8))
    p.wall((15, 8), (9, 8))
    p.label((2.0, 1.8), "LIVING")
    p.label((2.0, 6.0), "BEDROOM")
    p.label((11.0, 4.0), "GARAGE")
    # Covered porch: posts and a beam, no walls at all.
    for x in (-3.0, -0.2):
        p.msp.add_lwpolyline(
            [p.p(x, 1.0), p.p(x + 0.2, 1.0), p.p(x + 0.2, 1.2), p.p(x, 1.2)],
            close=True, dxfattribs={"layer": "A-WALL"})
    p.msp.add_lwpolyline(
        [p.p(-3.0, 1.0), p.p(0.0, 1.0), p.p(0.0, 6.0), p.p(-3.0, 6.0)],
        close=True, dxfattribs={"layer": "A-FLOR-PFIX"})
    p.label((-2.4, 3.4), "PORCH")
    return p.save()


def build_irregular() -> str:
    """A T-shaped plan with a setback and a projecting bay."""
    p = Plan("t05_irregular.dxf", unit_code=6, per_metre=1.0)
    ring = [(0, 0), (6, 0), (6, -3), (11, -3), (11, 0), (16, 0),
            (16, 6), (11, 6), (11, 9), (5, 9), (5, 6), (0, 6)]
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        holes = [(1.2, 2.6, "window")] if math.dist(a, b) > 4.0 else []
        p.wall(a, b, holes_m=holes)
    p.wall((6, 0), (6, 6), thickness_m=0.1, holes_m=[(2.0, 2.9, "door")])
    p.wall((11, 0), (11, 6), thickness_m=0.1, holes_m=[(2.0, 2.9, "door")])
    p.label((2.4, 3.0), "BEDROOM")
    p.label((8.0, 2.0), "LIVING")
    p.label((13.0, 3.0), "KITCHEN")
    return p.save()


def build_dimension_heavy() -> str:
    """The same rectangle, buried in dimension strings and notes.

    The dimensions run the full width of the plan and then some. If any of
    them reaches the wall reconstruction, the result has a wall right through
    the middle of the building and the test says so.
    """
    p = Plan("t06_dimension_heavy.dxf", unit_code=6, per_metre=1.0)
    rect_rooms(p)
    for y, off in ((0.0, 1.2), (0.0, 2.4), (7.0, -1.2)):
        p.dimension((0, y), (10, y), off)
        p.dimension((0, y), (4, y), off + 0.8)
        p.dimension((4, y), (10, y), off + 0.8)
    for x, off in ((0.0, 1.2), (10.0, -1.2)):
        p.dimension((x, 0), (x, 7), off)
    p.note_block((12.0, 6.0), [
        "GENERAL NOTES",
        "1. ALL DIMENSIONS ARE TO FACE OF STUD.",
        "2. 5/8\" TYPE X GYP. BD. ON ALL BEARING WALLS.",
        "3. VERIFY ALL CONDITIONS IN FIELD.",
        "4. R-21 MIN. INSULATION AT EXTERIOR WALLS.",
    ])
    return p.save()


def build_many_layers() -> str:
    """Wall geometry scattered across several differently-named wall layers."""
    p = Plan("t07_many_layers.dxf", unit_code=6, per_metre=1.0)
    for name in ("S-STEM-WALL", "A-WALL-EXTR", "A-WALL-INTR", "M-DUCT",
                 "E-LITE", "A-FLOR-STRS", "G-ANNO-NPLT"):
        if name not in p.doc.layers:
            p.doc.layers.add(name, color=6)
    rect_rooms(p)
    # Re-home half the wall lines onto a differently named wall layer, and
    # scatter services and lighting that must not become walls.
    walls = [e for e in p.msp if e.dxftype() == "LINE" and e.dxf.layer == "A-WALL"]
    for i, e in enumerate(walls):
        e.dxf.layer = "A-WALL-EXTR" if i % 2 else "A-WALL-INTR"
    for i in range(12):
        y = 0.5 + i * 0.5
        p.msp.add_line(p.p(0.4, y), p.p(9.6, y), dxfattribs={"layer": "M-DUCT"})
        p.msp.add_line(p.p(0.4, y + 0.1), p.p(9.6, y + 0.1),
                       dxfattribs={"layer": "E-LITE"})
    return p.save()


def build_blocks() -> str:
    """Every wall inside a nested block, inserted rotated and scaled.

    A reader that walks modelspace and stops sees six INSERT markers and no
    geometry at all.
    """
    p = Plan("t08_blocks.dxf", unit_code=6, per_metre=1.0)
    inner = p.doc.blocks.new(name="WALL-UNIT")
    t = p.t

    def band(a: XY, b: XY, holes: Sequence[Tuple[float, float, str]] = ()) -> None:
        ax, ay = p.p(*a)
        bx, by = p.p(*b)
        length = math.hypot(bx - ax, by - ay)
        d = ((bx - ax) / length, (by - ay) / length)
        n = (-d[1], d[0])
        spans = sorted((p.u(s), p.u(e), k) for s, e, k in holes)
        for side in (+1, -1):
            off = (n[0] * t / 2 * side, n[1] * t / 2 * side)
            cursor = 0.0
            for s, e, _k in spans:
                if s - cursor > 1e-6:
                    inner.add_line(
                        (ax + d[0] * cursor + off[0], ay + d[1] * cursor + off[1]),
                        (ax + d[0] * s + off[0], ay + d[1] * s + off[1]),
                        dxfattribs={"layer": "A-WALL"})
                cursor = e
            inner.add_line(
                (ax + d[0] * cursor + off[0], ay + d[1] * cursor + off[1]),
                (ax + d[0] * length + off[0], ay + d[1] * length + off[1]),
                dxfattribs={"layer": "A-WALL"})
        for s, e, _k in spans:
            inner.add_lwpolyline([
                (ax + d[0] * s + n[0] * t / 2, ay + d[1] * s + n[1] * t / 2),
                (ax + d[0] * e + n[0] * t / 2, ay + d[1] * e + n[1] * t / 2),
                (ax + d[0] * e - n[0] * t / 2, ay + d[1] * e - n[1] * t / 2),
                (ax + d[0] * s - n[0] * t / 2, ay + d[1] * s - n[1] * t / 2),
            ], close=True, dxfattribs={"layer": "A-HEAD"})

    band((0, 0), (8, 0), [(3.0, 4.5, "window")])
    band((8, 0), (8, 6), [(2.0, 3.5, "window")])
    band((8, 6), (0, 6), [(3.5, 4.4, "door")])
    band((0, 6), (0, 0), [(2.0, 3.5, "window")])
    band((3, 0), (3, 6), [(2.0, 2.9, "door")])

    outer = p.doc.blocks.new(name="FLOOR-PLATE")
    outer.add_blockref("WALL-UNIT", (0, 0))
    p.msp.add_blockref("FLOOR-PLATE", (0, 0), dxfattribs={"layer": "A-WALL"})
    p.label((1.4, 3.0), "BEDROOM")
    p.label((5.4, 3.0), "LIVING")
    return p.save()


def build_feet_inches() -> str:
    """The three-room plan drawn in inches, declaring inches."""
    p = Plan("t09_feet_inches.dxf", unit_code=1, per_metre=1.0 / 0.0254,
             thickness_m=0.1016)
    rect_rooms(p)
    for y, off in ((0.0, 1.2), (7.0, -1.2)):
        p.dimension((0, y), (10, y), off)
    return p.save()


def build_millimetres() -> str:
    p = Plan("t10_millimetres.dxf", unit_code=4, per_metre=1000.0,
             thickness_m=0.23)
    rect_rooms(p)
    return p.save()


def build_metres() -> str:
    p = Plan("t11_metres.dxf", unit_code=6, per_metre=1.0, thickness_m=0.2)
    rect_rooms(p)
    return p.save()


def build_lying_header() -> str:
    """Drawn in inches; the header says millimetres.

    This is the failing plan's defect in isolation: a drawing whose
    ``$INSUNITS`` is simply wrong. It belongs in the generated set as well as
    the real one, because the real one can only be tested as a whole and this
    isolates the single decision.
    """
    p = Plan("t12_lying_header.dxf", unit_code=4, per_metre=1.0 / 0.0254,
             thickness_m=0.1016)
    rect_rooms(p)
    for y, off in ((0.0, 1.2), (7.0, -1.2)):
        p.dimension((0, y), (10, y), off)
    return p.save()


def build_single_line() -> str:
    """Walls drawn as bare centrelines, the way a sketch or an old plan is."""
    p = Plan("t13_single_line.dxf", unit_code=6, per_metre=1.0)
    for a, b in (((0, 0), (10, 0)), ((10, 0), (10, 7)), ((10, 7), (0, 7)),
                 ((0, 7), (0, 0)), ((4, 0), (4, 7)), ((4, 4), (10, 4))):
        p.msp.add_line(p.p(*a), p.p(*b), dxfattribs={"layer": "A-WALL"})
    p.label((1.4, 3.2), "BEDROOM")
    p.label((6.4, 1.6), "LIVING")
    p.label((6.4, 5.4), "KITCHEN")
    return p.save()


def build_rotated() -> str:
    """The rectangle plan rotated 23 degrees, to catch anything axis-aligned."""
    p = Plan("t14_rotated.dxf", unit_code=6, per_metre=1.0)
    rect_rooms(p)
    angle = math.radians(23.0)
    import ezdxf.math as em
    m = em.Matrix44.z_rotate(angle)
    for e in list(p.msp):
        try:
            e.transform(m)
        except Exception:
            pass
    return p.save()


BUILDERS = {
    "t01_simple_rect.dxf": build_simple_rect,
    "t02_l_shape.dxf": build_l_shape,
    "t03_multi_room.dxf": build_multi_room,
    "t04_garage_porch.dxf": build_garage_porch,
    "t05_irregular.dxf": build_irregular,
    "t06_dimension_heavy.dxf": build_dimension_heavy,
    "t07_many_layers.dxf": build_many_layers,
    "t08_blocks.dxf": build_blocks,
    "t09_feet_inches.dxf": build_feet_inches,
    "t10_millimetres.dxf": build_millimetres,
    "t11_metres.dxf": build_metres,
    "t12_lying_header.dxf": build_lying_header,
    "t13_single_line.dxf": build_single_line,
    "t14_rotated.dxf": build_rotated,
}


def build_all() -> List[str]:
    return [fn() for fn in BUILDERS.values()]


if __name__ == "__main__":
    for path in build_all():
        print("wrote", path)
