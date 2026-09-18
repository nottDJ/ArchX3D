"""
Generate the failure corpus: drawings the engine must refuse or flag.

Every drawing here is one way a file can fail to describe a building — empty,
nothing but annotation, geometry that is not walls, a file that is not a DXF,
units that contradict the geometry, walls that enclose nothing, plans whose
relationship the sheet does not state. None of them may come back as a valid
building. The acceptable outcomes for each are recorded in
``failures/manifest.json``:

* ``refused`` — :class:`~modules.recon.ir.ReconstructionError`, with the
  stage it stopped at and at least one stated failure;
* ``invalid`` / ``ambiguous`` — a drawing returned with that validation
  status, errors or review items saying why, and the Blender gate closed.

Run directly to regenerate::

    python tests/fixtures/make_failures.py
"""

from __future__ import annotations

import json
import os
import random
import sys
from typing import Dict, List

import ezdxf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from make_plans import Plan  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "failures")
MM = dict(unit_code=4, per_metre=1000.0)
M = dict(unit_code=6, per_metre=1.0)

MANIFEST: Dict[str, dict] = {}


def expect(name: str, outcomes: List[str], why: str) -> None:
    MANIFEST[name] = {"outcomes": outcomes, "why": why}


def save(plan: Plan) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, plan.name)
    plan.doc.saveas(path)
    return path


def house(p: Plan, ox: float = 0.0, oy: float = 0.0, walls: str = "NESW") -> None:
    """A 9 x 7 m two-room house; ``walls`` picks which exterior sides exist."""
    def w(a, b, **kw):
        p.wall((a[0] + ox, a[1] + oy), (b[0] + ox, b[1] + oy), **kw)
    if "S" in walls:
        w((0, 0), (9, 0), holes_m=[(2.0, 3.5, "window")])
    if "E" in walls:
        w((9, 0), (9, 7))
    if "N" in walls:
        w((9, 7), (0, 7), holes_m=[(4.0, 4.9, "door")])
    if "W" in walls:
        w((0, 7), (0, 0))
    w((4.5, 0), (4.5, 7), thickness_m=0.1, holes_m=[(3.0, 3.9, "door")])
    p.label((ox + 1.5, oy + 3.5), "LIVING")
    p.label((ox + 6.0, oy + 3.5), "BEDROOM")


def f01_empty() -> None:
    save(Plan("f01_empty.dxf", **MM))
    expect("f01_empty.dxf", ["refused"], "no entities at all")


def f02_dimensions_only() -> None:
    p = Plan("f02_dimensions_only.dxf", **MM)
    for i in range(6):
        p.dimension((0, i * 2.0), (9.0, i * 2.0), offset_m=0.8)
    save(p)
    expect("f02_dimensions_only.dxf", ["refused"],
           "dimension annotation only; a dimension's extension lines are not walls")


def f03_text_only() -> None:
    p = Plan("f03_text_only.dxf", **MM)
    for i, name in enumerate(["LIVING", "KITCHEN", "BEDROOM 1", "BATH", "GROUND FLOOR PLAN"]):
        p.label((i * 4.0, 0.0), name, height_m=0.3)
    save(p)
    expect("f03_text_only.dxf", ["refused"], "room names and a title with no geometry")


def f04_furniture_only() -> None:
    p = Plan("f04_furniture_only.dxf", **MM)
    rng = random.Random(4)
    for i in range(14):
        p.fixture_block("FURN-%d" % (i % 4), (rng.uniform(0, 12), rng.uniform(0, 9)),
                        (rng.uniform(0.5, 2.0), rng.uniform(0.5, 2.0)), layer="A-FURN")
    for i in range(6):
        p.msp.add_circle(p.p(rng.uniform(0, 12), rng.uniform(0, 9)), p.u(0.4),
                         dxfattribs={"layer": "A-FURN"})
    save(p)
    expect("f04_furniture_only.dxf", ["refused", "invalid"], "furniture and fixtures, no walls")


def f05_disconnected_lines() -> None:
    p = Plan("f05_disconnected_lines.dxf", **M)
    rng = random.Random(5)
    for _ in range(120):
        x, y = rng.uniform(0, 30), rng.uniform(0, 20)
        import math
        a = rng.uniform(0, math.pi)
        L = rng.uniform(0.2, 2.5)
        p.msp.add_line((x, y), (x + L * math.cos(a), y + L * math.sin(a)),
                       dxfattribs={"layer": "0"})
    save(p)
    expect("f05_disconnected_lines.dxf", ["refused", "invalid"],
           "scattered unrelated strokes on layer 0")


def f06_corrupt() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = Plan("tmp.dxf", **MM)
    house(p)
    path = os.path.join(OUT_DIR, "f06_corrupt.dxf")
    p.doc.saveas(path)
    with open(path, "rb") as fh:
        data = fh.read()
    # Cut mid-file and splice in binary garbage: a failed download.
    rng = random.Random(6)
    cut = len(data) // 3
    with open(path, "wb") as fh:
        fh.write(data[:cut] + bytes(rng.randrange(256) for _ in range(4096)))
    expect("f06_corrupt.dxf", ["refused"], "truncated file with binary garbage")


def f07_dwg_renamed() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "f07_dwg_renamed.dxf")
    rng = random.Random(7)
    with open(path, "wb") as fh:
        fh.write(b"AC1032" + bytes(rng.randrange(256) for _ in range(8000)))
    expect("f07_dwg_renamed.dxf", ["refused"], "a binary DWG saved with a .dxf name")


def f08_unsupported_entities() -> None:
    p = Plan("f08_unsupported_entities.dxf", **M)
    for i in range(4):
        x = i * 5.0
        p.msp.add_3dface([(x, 0, 0), (x + 4, 0, 0), (x + 4, 0, 3), (x, 0, 3)],
                         dxfattribs={"layer": "A-WALL"})
        mesh = p.msp.add_mesh(dxfattribs={"layer": "A-WALL"})
        with mesh.edit_data() as md:
            md.vertices = [(x, 1, 0), (x + 4, 1, 0), (x + 4, 1.2, 0), (x, 1.2, 0)]
            md.faces = [(0, 1, 2, 3)]
        p.msp.add_point((x, 5, 0), dxfattribs={"layer": "A-WALL"})
    save(p)
    expect("f08_unsupported_entities.dxf", ["refused"],
           "3D faces, meshes and points only; nothing a plan reader can use")


def f09_inconsistent_units() -> None:
    # Declared millimetres, drawn in metres: a 9 m house would be 9 mm.
    p = Plan("f09_inconsistent_units.dxf", unit_code=4, per_metre=1.0)
    house(p)
    save(p)
    expect("f09_inconsistent_units.dxf", ["refused", "invalid", "resolved_metres"],
           "header says millimetres, geometry is metre-sized; must never build a 9 mm house")


def f10_mixed_scales() -> None:
    # One plan in millimetres beside the same plan in metres on one sheet.
    p = Plan("f10_mixed_scales.dxf", **MM)
    house(p)
    # The second copy drawn at 1/1000 of the first's scale, 20 m to the right.
    copy = Plan("second", unit_code=4, per_metre=1.0)
    house(copy)
    for e in copy.msp:
        if e.dxftype() in ("LINE", "LWPOLYLINE", "ARC", "TEXT"):
            ne = e.copy()
            ne.translate(20000.0, 0.0, 0.0)
            p.msp.add_entity(ne)
    save(p)
    expect("f10_mixed_scales.dxf", ["refused", "invalid", "ambiguous", "one_building"],
           "the same plan at two scales; the small copy must not become a building")


def f11_incomplete_walls() -> None:
    p = Plan("f11_incomplete_walls.dxf", **MM)
    house(p, walls="S")      # one exterior wall and a partition: encloses nothing
    p.wall((12, 0), (12, 5))
    save(p)
    expect("f11_incomplete_walls.dxf", ["refused", "invalid"],
           "walls that enclose no space")


def f12_untitled_repeated_plans() -> None:
    p = Plan("f12_untitled_repeated_plans.dxf", **MM)
    house(p)
    house(p, ox=16.0)
    save(p)
    expect("f12_untitled_repeated_plans.dxf", ["ambiguous"],
           "two identical plans, no titles: storeys or neighbours cannot be told apart")


def f13_superimposed_plans() -> None:
    # Two different floor plans drawn on top of each other on different layers,
    # the way an xref'd upper storey is sometimes left overlaid.
    p = Plan("f13_superimposed_plans.dxf", **MM)
    house(p)
    p.wall((0, 3.5), (9, 3.5), thickness_m=0.1, holes_m=[(1.0, 1.9, "door")])
    p.wall((2.0, 0), (2.0, 7), thickness_m=0.1)
    save(p)
    expect("f13_superimposed_plans.dxf", ["valid_one_level", "ambiguous", "invalid"],
           "overlaid partitions from another storey; must stay one level, never two buildings")


def f14_degenerate_geometry() -> None:
    p = Plan("f14_degenerate_geometry.dxf", **M)
    for i in range(50):
        p.msp.add_line((i, 0), (i, 0), dxfattribs={"layer": "A-WALL"})
        p.msp.add_lwpolyline([(i, 1), (i, 1), (i, 1)], dxfattribs={"layer": "A-WALL"})
    p.msp.add_circle((5, 5), 0.0, dxfattribs={"layer": "A-WALL"})
    save(p)
    expect("f14_degenerate_geometry.dxf", ["refused"], "zero-length lines and polylines")


def main() -> int:
    for fn in (f01_empty, f02_dimensions_only, f03_text_only, f04_furniture_only,
               f05_disconnected_lines, f06_corrupt, f07_dwg_renamed,
               f08_unsupported_entities, f09_inconsistent_units, f10_mixed_scales,
               f11_incomplete_walls, f12_untitled_repeated_plans,
               f13_superimposed_plans, f14_degenerate_geometry):
        fn()
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(MANIFEST, fh, indent=1, sort_keys=True)
    print("wrote %d failure drawings to %s" % (len(MANIFEST), OUT_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
