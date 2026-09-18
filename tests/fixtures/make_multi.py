"""
Generate the multi-building and multi-storey DXF fixtures.

Each drawing isolates one decision the engine has to make about how a sheet's
plans relate, with the true answer known exactly:

* how many **buildings** the wall topology describes — detached structures,
  a party wall that joins two dwellings into one structure, a pair so close
  the gap has to be reported;
* how many **storeys**, and only from evidence — plan titles placed under
  their plans, level tokens in layer names — and never from disconnection
  alone;
* when the drawing **does not say** — repeated floor plates with no title —
  which must come back ambiguous rather than guessed;
* text that mentions a floor without naming a plan — a floor-area schedule, a
  stair annotation — which must not become a storey.

Run directly to regenerate::

    python tests/fixtures/make_multi.py
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from make_plans import Plan  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "multi")


class Sheet(Plan):
    def save(self) -> str:
        return Plan.save(self, OUT_DIR)

    def title(self, at_m: Tuple[float, float], text: str, height_m: float = 0.45) -> None:
        self.msp.add_text(text, height=self.u(height_m),
                          dxfattribs={"layer": "A-ANNO-TEXT"}).set_placement(self.p(*at_m))


def house(p: Plan, ox: float, oy: float, *, names=("BEDROOM", "LIVING", "KITCHEN"),
          layer: Optional[str] = None, variant: int = 0) -> None:
    """A 10 x 7 m three-room plan at an offset. ``variant`` moves the partitions,
    so storeys share an exterior and differ inside, as real storeys do."""
    def w(a, b, **kw):
        p.wall((a[0] + ox, a[1] + oy), (b[0] + ox, b[1] + oy), **kw)

    w((0, 0), (10, 0), holes_m=[(3.0, 4.5, "window"), (6.5, 8.0, "window")])
    w((10, 0), (10, 7), holes_m=[(2.5, 4.0, "window")])
    w((10, 7), (0, 7), holes_m=[(4.0, 5.0, "door" if variant == 0 else "window")])
    w((0, 7), (0, 0), holes_m=[(3.0, 4.5, "window")])
    xa = 4.0 if variant == 0 else 5.5
    w((xa, 0), (xa, 7), thickness_m=0.1, holes_m=[(1.0, 1.9, "door")])
    w((xa, 4), (10, 4), thickness_m=0.1, holes_m=[(1.5, 2.4, "door")])
    p.label((ox + 1.4, oy + 3.2), names[0])
    p.label((ox + xa + 2.0, oy + 1.6), names[1])
    p.label((ox + xa + 2.0, oy + 5.4), names[2])
    if layer:
        for e in p.msp:
            if e.dxftype() == "LINE" and e.dxf.layer == "A-WALL":
                e.dxf.layer = layer


def garage(p: Plan, ox: float, oy: float, size=(6.0, 6.0)) -> None:
    gw, gd = size
    p.wall((ox, oy), (ox + gw, oy), holes_m=[(0.6, gw - 0.6, "door")])
    p.wall((ox + gw, oy), (ox + gw, oy + gd))
    p.wall((ox + gw, oy + gd), (ox, oy + gd), holes_m=[(2.0, 2.9, "door")])
    p.wall((ox, oy + gd), (ox, oy))
    p.label((ox + gw / 2 - 0.6, oy + gd / 2), "GARAGE")


def l_studio(p: Plan, ox: float, oy: float) -> None:
    ring = [(0, 0), (8, 0), (8, 4), (4, 4), (4, 8), (0, 8)]
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        holes = [(1.0, 1.9, "door")] if i == 0 else []
        p.wall((a[0] + ox, a[1] + oy), (b[0] + ox, b[1] + oy), holes_m=holes)
    p.wall((ox, oy + 4), (ox + 4, oy + 4), thickness_m=0.1, holes_m=[(1.5, 2.4, "door")])
    p.label((ox + 1.5, oy + 2.0), "STUDIO")
    p.label((ox + 1.5, oy + 6.0), "BATH")


def retag(p: Plan, layer: str, before: int) -> None:
    """Move wall lines added since ``before`` entities onto ``layer``."""
    ents = list(p.msp)
    for e in ents[before:]:
        if e.dxftype() == "LINE" and e.dxf.layer == "A-WALL":
            e.dxf.layer = layer


# ---------------------------------------------------------------------------

def m01_house_and_garage() -> str:
    p = Sheet("m01_house_and_garage.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0)
    garage(p, 14.0, 0.5)
    return p.save()


def m02_three_buildings() -> str:
    p = Sheet("m02_three_buildings.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0)
    garage(p, 16.0, 0.0)
    l_studio(p, 0.0, 13.0)
    return p.save()


def m03_semi_detached() -> str:
    """Two dwellings sharing one party wall: one structure."""
    p = Sheet("m03_semi_detached.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0, names=("BEDROOM", "LIVING", "KITCHEN"))
    p.wall((10, 0), (18, 0), holes_m=[(2.0, 3.5, "window")])
    p.wall((18, 0), (18, 7), holes_m=[(2.5, 4.0, "window")])
    p.wall((18, 7), (10, 7), holes_m=[(3.0, 3.9, "door")])
    p.wall((14, 0), (14, 7), thickness_m=0.1, holes_m=[(3.0, 3.9, "door")])
    p.label((11.2, 3.2), "BEDROOM 2")
    p.label((15.2, 3.2), "LIVING 2")
    return p.save()


def m04_close_buildings() -> str:
    p = Sheet("m04_close_buildings.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0)
    garage(p, 11.05, 0.5)          # 0.9 m face to face
    return p.save()


def m05_two_storey_titled() -> str:
    """Ground and first floor side by side, each titled under its plan."""
    p = Sheet("m05_two_storey_titled.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0, names=("BEDROOM", "LIVING", "KITCHEN"))
    house(p, 16.0, 0, names=("BEDROOM 2", "BEDROOM 3", "BATH"), variant=1)
    p.title((0.5, -2.5), "GROUND FLOOR PLAN")
    p.title((16.5, -2.5), "FIRST FLOOR PLAN")
    return p.save()


def m06_three_storey_stacked() -> str:
    """Three storeys stacked up the sheet, titles under each, one column."""
    p = Sheet("m06_three_storey_stacked.dxf", unit_code=4, per_metre=1000.0)
    for n, (y, text, names) in enumerate((
            (0.0, "GROUND FLOOR PLAN", ("BEDROOM", "LIVING", "KITCHEN")),
            (13.0, "FIRST FLOOR PLAN", ("BEDROOM 2", "BEDROOM 3", "BATH")),
            (26.0, "SECOND FLOOR PLAN", ("STUDY", "BEDROOM 4", "BATH 2")))):
        house(p, 0, y, names=names, variant=n % 2)
        p.title((1.0, y - 2.5), text)
    return p.save()


def m07_repeated_untitled() -> str:
    """The same floor plate twice and nothing saying what either is."""
    p = Sheet("m07_repeated_untitled.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0)
    house(p, 0, 12.0)
    return p.save()


def m08_floor_schedule() -> str:
    """One plan, and an area schedule listing storeys that are not drawn."""
    p = Sheet("m08_floor_schedule.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0)
    for i, (floor, area) in enumerate((("GROUND FLOOR", "66.0"),
                                       ("FIRST FLOOR", "64.2"),
                                       ("SECOND FLOOR", "40.0"))):
        p.title((13.0, 5.0 - i * 0.6), floor, height_m=0.25)
        p.title((16.0, 5.0 - i * 0.6), area, height_m=0.25)
    p.label((6.0, 1.0), "UP TO FIRST FLOOR", height_m=0.12)
    return p.save()


def m09_block_designations() -> str:
    """Two identical plates, titled as two blocks: two buildings, no doubt."""
    p = Sheet("m09_block_designations.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0)
    house(p, 16.0, 0)
    p.title((2.0, -2.5), "BLOCK A")
    p.title((18.0, -2.5), "BLOCK B")
    return p.save()


def m10_layer_levels() -> str:
    """No titles; the wall layers say which storey each plan is."""
    p = Sheet("m10_layer_levels.dxf", unit_code=6, per_metre=1.0)
    for name in ("GF-WALL", "FF-WALL"):
        p.doc.layers.add(name, color=7)
    before = len(list(p.msp))
    house(p, 0, 0)
    retag(p, "GF-WALL", before)
    before = len(list(p.msp))
    house(p, 0, 12.0, names=("BEDROOM 2", "BEDROOM 3", "BATH"), variant=1)
    retag(p, "FF-WALL", before)
    return p.save()


def m11_basement() -> str:
    p = Sheet("m11_basement.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0, names=("STORE", "PLANT", "GYM"))
    house(p, 16.0, 0, names=("BEDROOM", "LIVING", "KITCHEN"), variant=1)
    p.title((0.5, -2.5), "BASEMENT PLAN")
    p.title((16.5, -2.5), "GROUND FLOOR PLAN")
    return p.save()


def m12_one_title_one_twin() -> str:
    """One plan titled, a congruent twin beside it untitled: undetermined."""
    p = Sheet("m12_one_title_one_twin.dxf", unit_code=6, per_metre=1.0)
    house(p, 0, 0)
    house(p, 16.0, 0)
    p.title((0.5, -2.5), "GROUND FLOOR PLAN")
    return p.save()


BUILDERS = [
    m01_house_and_garage, m02_three_buildings, m03_semi_detached,
    m04_close_buildings, m05_two_storey_titled, m06_three_storey_stacked,
    m07_repeated_untitled, m08_floor_schedule, m09_block_designations,
    m10_layer_levels, m11_basement, m12_one_title_one_twin,
]


def build_all() -> List[str]:
    return [fn() for fn in BUILDERS]


if __name__ == "__main__":
    for path in build_all():
        print("wrote", path)
