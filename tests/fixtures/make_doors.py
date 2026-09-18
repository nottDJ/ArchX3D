"""
Generate door-recognition fixtures, each with its ground truth.

Doors are drawn the ways real offices draw them — and the ways that trap a
recogniser into a false door:

* ``d01`` — jamb marks only: two small frame marks per door on the door
  layer, no swing, no header. Includes two doors separated by a short pier,
  whose inner jambs must not pair into a door in the pier, and a double door.
* ``d02`` — the door symbol (quarter swing + leaf) inside anonymous blocks on
  layer ``0``, inserted rotated, so no layer or block name says "door". Chairs
  with half-circle backs, a quarter-round counter with no leaf, and a swing
  symbol parked in the middle of a room share the drawing.
* ``d03`` — door-layer clutter that is not a door: door tags and schedule
  boxes inside rooms, tiny marks nowhere near a wall.
* ``d04`` — gaps in the walls and no door evidence at all.

Every fixture writes ``<name>.truth.json`` listing every real opening; an
opening the engine reports anywhere else is a false positive.

Run directly to regenerate::

    python tests/fixtures/make_doors.py
"""

from __future__ import annotations

import math
import os
import sys
from typing import List, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from make_plans import Plan  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doors")


class JambPlan(Plan):
    """Openings drawn as jamb marks rather than headers and swings."""

    def _header(self, ax, ay, d, n, s, e, t, kind: str) -> None:
        layer = {"door": "A-DOOR", "window": "A-GLAZ"}.get(kind, "A-DOOR")
        if kind == "gap":
            return
        mark = self.u(0.05)
        for at, inward in ((s, 1.0), (e, -1.0)):
            x0 = at - (mark if inward < 0 else 0.0)
            x1 = x0 + mark
            pts = [(ax + d[0] * x0 + n[0] * t * 0.55, ay + d[1] * x0 + n[1] * t * 0.55),
                   (ax + d[0] * x1 + n[0] * t * 0.55, ay + d[1] * x1 + n[1] * t * 0.55),
                   (ax + d[0] * x1 - n[0] * t * 0.55, ay + d[1] * x1 - n[1] * t * 0.55),
                   (ax + d[0] * x0 - n[0] * t * 0.55, ay + d[1] * x0 - n[1] * t * 0.55)]
            self.msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
        if kind == "window":
            for frac in (-0.2, 0.2):
                off = (n[0] * t * frac, n[1] * t * frac)
                self._line((ax + d[0] * s + off[0], ay + d[1] * s + off[1]),
                           (ax + d[0] * e + off[0], ay + d[1] * e + off[1]), "A-GLAZ")

    def save(self) -> str:
        return Plan.save(self, OUT_DIR)


class SymbolPlan(JambPlan):
    """Doors as anonymous block inserts of a swing + leaf symbol on layer 0."""

    _n = 0

    def _symbol_block(self, width: float) -> str:
        name = "A$C%08X" % (0x1F2E3D00 + int(round(width * 100)))
        if name not in self.doc.blocks:
            blk = self.doc.blocks.new(name=name)
            r = self.u(width)
            blk.add_arc(center=(0, 0), radius=r, start_angle=0, end_angle=90,
                        dxfattribs={"layer": "0"})
            blk.add_line((0, 0), (0, r), dxfattribs={"layer": "0"})
        return name

    def _header(self, ax, ay, d, n, s, e, t, kind: str) -> None:
        if kind != "door":
            return JambPlan._header(self, ax, ay, d, n, s, e, t, kind)
        width = (e - s) / self.per_metre
        rot = math.degrees(math.atan2(d[1], d[0]))
        if width > 1.2:
            # A double door is drafted as two leaves hinged at the outer
            # jambs, meeting in the middle — never as one wide leaf.
            half = self._symbol_block(width / 2.0)
            self.msp.add_blockref(half, (ax + d[0] * s, ay + d[1] * s),
                                  dxfattribs={"layer": "0", "rotation": rot})
            self.msp.add_blockref(half, (ax + d[0] * e, ay + d[1] * e),
                                  dxfattribs={"layer": "0", "rotation": rot + 180.0,
                                              "yscale": -1.0})
            return
        name = self._symbol_block(width)
        SymbolPlan._n += 1
        flip = SymbolPlan._n % 2 == 0
        hinge = (ax + d[0] * (e if flip else s), ay + d[1] * (e if flip else s))
        self.msp.add_blockref(name, hinge, dxfattribs={
            "layer": "0", "rotation": rot + (180.0 if flip else 0.0),
            "yscale": -1.0 if flip else 1.0})


def rooms(p: Plan, holes_door, holes_window) -> None:
    """A 12 x 8 m plan with a corridor, parametrised by how openings are drawn."""
    p.wall((0, 0), (12, 0), holes_m=[(1.5, 3.0, "window"), (8.0, 9.5, "window")])
    p.wall((12, 0), (12, 8), holes_m=[(3.0, 4.2, "window")])
    p.wall((12, 8), (0, 8), holes_m=[(5.0, 6.6, "door")])          # double door
    p.wall((0, 8), (0, 0), holes_m=[(3.0, 3.9, "door")])
    # Interior wall with two doors 0.6 m apart: the pier trap.
    p.wall((0, 4), (12, 4), thickness_m=0.12,
           holes_m=[(2.0, 2.9, "door"), (3.5, 4.4, "door"), (8.0, 8.9, "door")])
    p.wall((6, 0), (6, 4), thickness_m=0.12)
    p.label((2.5, 2.0), "BEDROOM")
    p.label((9.0, 2.0), "KITCHEN")
    p.label((6.0, 6.0), "LIVING")


def d01_jambs() -> str:
    p = JambPlan("d01_jamb_doors.dxf", unit_code=6, per_metre=1.0, thickness_m=0.2)
    rooms(p, None, None)
    return p.save()


def d02_symbols() -> str:
    p = SymbolPlan("d02_symbol_blocks.dxf", unit_code=4, per_metre=1000.0,
                   thickness_m=0.23)
    rooms(p, None, None)
    # Decoys, all on layer 0 in anonymous blocks.
    chair = p.doc.blocks.new(name="A$C9ABCDEF0")
    chair.add_lwpolyline([(0, 0), (450, 0), (450, 450), (0, 450)], close=True)
    chair.add_arc(center=(225, 450), radius=450, start_angle=0, end_angle=180)
    counter = p.doc.blocks.new(name="A$C9ABCDEF1")
    counter.add_arc(center=(0, 0), radius=900, start_angle=0, end_angle=90)
    counter.add_line((0, 0), (900, 0))                   # a side, not a leaf
    stray = p.doc.blocks.new(name="A$C9ABCDEF2")
    stray.add_arc(center=(0, 0), radius=800, start_angle=0, end_angle=90)
    stray.add_line((0, 0), (0, 800))
    for x, y, blk in ((2000, 1500, "A$C9ABCDEF0"), (3000, 1500, "A$C9ABCDEF0"),
                      (9500, 1000, "A$C9ABCDEF1"), (9000, 6000, "A$C9ABCDEF2")):
        p.msp.add_blockref(blk, (x, y), dxfattribs={"layer": "0"})
    return p.save()


def d03_door_layer_clutter() -> str:
    p = JambPlan("d03_door_layer_clutter.dxf", unit_code=6, per_metre=1.0,
                 thickness_m=0.2)
    rooms(p, None, None)
    # Door tags: a circle and a code, inside rooms, on the door layer.
    for x, y in ((2.2, 1.0), (9.0, 6.5), (4.0, 6.0)):
        p.msp.add_circle(p.p(x, y), p.u(0.25), dxfattribs={"layer": "A-DOOR"})
        p.msp.add_text("D1", height=p.u(0.15), dxfattribs={"layer": "A-DOOR"}
                       ).set_placement(p.p(x - 0.1, y - 0.07))
    # Tiny marks a door-width apart, but in the middle of a room.
    for x in (8.0, 9.0):
        p.msp.add_lwpolyline([p.p(x, 6.0), p.p(x + 0.05, 6.0), p.p(x + 0.05, 6.2),
                              p.p(x, 6.2)], close=True, dxfattribs={"layer": "A-DOOR"})
    return p.save()


def d04_gaps_only() -> str:
    p = JambPlan("d04_gaps_only.dxf", unit_code=6, per_metre=1.0, thickness_m=0.2)
    p.wall((0, 0), (10, 0))
    p.wall((10, 0), (10, 7))
    p.wall((10, 7), (0, 7), holes_m=[(4.0, 5.0, "gap")])
    p.wall((0, 7), (0, 0))
    p.wall((4, 0), (4, 7), thickness_m=0.12, holes_m=[(1.0, 1.9, "gap")])
    p.label((2.0, 3.0), "BEDROOM")
    p.label((7.0, 3.0), "LIVING")
    for o in p.truth["openings"]:
        o["kind"] = "unknown_opening"
    return p.save()


BUILDERS = [d01_jambs, d02_symbols, d03_door_layer_clutter, d04_gaps_only]


def build_all() -> List[str]:
    return [fn() for fn in BUILDERS]


if __name__ == "__main__":
    for path in build_all():
        print("wrote", path)
