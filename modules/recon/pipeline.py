"""
ArchX3D — The reconstruction pipeline
=====================================
One function, :func:`reconstruct`, runs the whole deterministic path:

    DXF -> read+classify -> units -> opening evidence -> walls -> topology
        -> rooms -> openings -> validate -> Building

and one CLI runs it from a shell. No network, no API key, no model. That is
the point of the rewrite: the geometry engine is arithmetic, and arithmetic
does not need a credential.

Stage order, and why it is this order
-------------------------------------
Opening evidence is collected *before* walls, because a wall's line work is
cut at every opening and without knowing where the openings are the engine
cannot tell a doorway from the end of a wall. Rooms come after walls because a
room is a consequence of walls. Validation comes before anything is written,
because the whole failure this replaces was a pipeline that wrote confidently.

Every stage is timed and every stage's output is dumpable; pass
``diagnostics_dir`` and the bundle in :mod:`modules.recon.diagnostics` is
written whether the run succeeded or failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Sequence

if __package__ in (None, ""):    # allow `python modules/recon/pipeline.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    __package__ = "modules.recon"

from . import diagnostics as D
from . import openings as OP
from . import read as RD
from . import topology as TP
from . import validate as VA
from . import walls as WL
from .ir import Building, Level, ReconstructionError

DEFAULT_WALL_HEIGHT = 2.7


def reconstruct(path: str, *,
                wall_height: float = DEFAULT_WALL_HEIGHT,
                user_scale: Optional[float] = None,
                diagnostics_dir: Optional[str] = None,
                strict: bool = True) -> Building:
    """Reconstruct a validated :class:`Building` from a DXF.

    Raises :class:`ReconstructionError` when the drawing cannot be turned into
    a building that passes validation. ``strict=False`` downgrades that to a
    returned building carrying the failures in ``validation`` — for tooling
    that wants to *see* the bad reconstruction, never for the build path.
    """
    timings: Dict[str, float] = {}
    t_all = time.perf_counter()
    drawing = wall_result = building = None

    try:
        t = time.perf_counter()
        drawing = RD.read(path, user_scale=user_scale)
        timings["read"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        evidence = OP.collect_evidence(drawing)
        timings["openings_evidence"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        wall_result = WL.reconstruct(drawing, bridges=OP.bridges(evidence))
        timings["walls"] = round(time.perf_counter() - t, 3)

        # Whether a wall is exterior changes what an opening in it is — a
        # 3 m hole in an outside wall is a garage door, the same hole inside
        # is a cased opening between two rooms. So this is settled from the
        # walls' own envelope before openings are classified, and settled
        # again from the real footprint once rooms exist.
        WL.label_exterior(wall_result.walls)

        t = time.perf_counter()
        openings = OP.assign(evidence, wall_result.walls, drawing)
        # Doorways the wall repair had to close over, so the model does not
        # gain a solid slab where the drawing left a way through.
        openings += OP.from_inferred(wall_result.walls,
                                     wall_result.inferred_openings, openings)
        if not openings:
            # Only when nothing else found anything at all: a drawing with no
            # opening, door, window or header layer still has doorways, and
            # the gaps in its walls are all that is left to read them from.
            openings = OP.from_gaps(wall_result.walls, wall_result.faces, openings)
        timings["openings"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        envelope = TP.envelope_of(drawing)
        rooms = TP.extract(wall_result.walls, drawing, envelope=envelope)
        WL.label_exterior(wall_result.walls, rooms.footprint)
        OP.link_rooms(openings, rooms.rooms, wall_result.walls)
        timings["topology"] = round(time.perf_counter() - t, 3)

        building = _assemble(path, drawing, wall_result, rooms, openings,
                             wall_height=wall_height)
        building.timings = dict(timings)
        building.stats.update({
            "drawing": drawing.summary(),
            "walls": wall_result.stats,
            "rooms": rooms.stats,
        })

        t = time.perf_counter()
        if strict:
            VA.enforce(building, diagnostics=_diag_payload(drawing, wall_result))
        else:
            VA.validate(building)
        timings["validate"] = round(time.perf_counter() - t, 3)
        timings["total"] = round(time.perf_counter() - t_all, 3)
        building.timings = dict(timings)
        if diagnostics_dir:
            D.write_bundle(diagnostics_dir, drawing=drawing,
                           wall_result=wall_result, building=building)
        return building

    except ReconstructionError as exc:
        # Refusal is exactly when the stages are worth seeing, so the bundle
        # is written here too — with whatever got as far as existing.
        exc.diagnostics.setdefault("timings", timings)
        if diagnostics_dir:
            D.write_bundle(diagnostics_dir, drawing=drawing,
                           wall_result=wall_result, building=building,
                           error=exc.as_dict())
        raise


def _diag_payload(drawing, wall_result) -> dict:
    out: dict = {}
    if drawing is not None:
        out["drawing"] = drawing.summary()
    if wall_result is not None:
        out["walls"] = wall_result.stats
    return out


def _assemble(path, drawing, wall_result, rooms, openings, *,
              wall_height: float) -> Building:
    building = Building(
        source_path=os.path.abspath(path),
        units=drawing.units,
        walls=wall_result.walls,
        rooms=rooms.rooms,
        openings=openings,
        nodes=rooms.nodes,
        footprint=rooms.footprint,
        footprint_holes=rooms.footprint_holes,
        footprint_parts=rooms.footprint_parts,
        origin_offset=drawing.origin_offset,
        bounds_min=drawing.bounds_min,
        bounds_max=drawing.bounds_max,
        north_deg=drawing.north_deg,
        default_wall_height=wall_height,
        warnings=list(drawing.warnings),
    )
    building.levels = [Level(
        id="l0", name="Ground floor", elevation=0.0, height=wall_height,
        wall_ids=[w.id for w in building.walls],
        room_ids=[r.id for r in building.rooms],
    )]
    return building


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="modules.recon.pipeline",
        description="Reconstruct a validated 2D building model from a DXF. "
                    "Deterministic and CPU-only: no API key, no network.")
    parser.add_argument("dxf", help="input DXF file")
    parser.add_argument("output", nargs="?", default=None,
                        help="where to write building.json "
                             "(default: alongside the DXF)")
    parser.add_argument("--wall-height", type=float, default=DEFAULT_WALL_HEIGHT,
                        help="storey height in metres (default: %(default)s)")
    parser.add_argument("--scale", type=float, default=None,
                        help="override unit resolution: metres per drawing unit")
    parser.add_argument("--diagnostics", default=None,
                        help="directory to write the diagnostics bundle into")
    parser.add_argument("--no-strict", action="store_true",
                        help="report validation failures instead of refusing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    out = args.output or os.path.splitext(args.dxf)[0] + ".building.json"
    try:
        building = reconstruct(
            args.dxf, wall_height=args.wall_height, user_scale=args.scale,
            diagnostics_dir=args.diagnostics, strict=not args.no_strict)
    except ReconstructionError as exc:
        print("RECONSTRUCTION FAILED (%s): %s" % (exc.stage, exc), file=sys.stderr)
        for f in exc.failures:
            print("  - %s" % f, file=sys.stderr)
        if args.diagnostics:
            print("  diagnostics written to %s" % args.diagnostics, file=sys.stderr)
        return 2

    building.to_json(out)
    if not args.quiet:
        s = building.summary()
        print("ArchX3D reconstruction (CPU / offline)")
        print("  source     %s" % os.path.basename(args.dxf))
        print("  units      %s (%s, confidence %.2f)" % (
            building.units.unit_name, building.units.method,
            building.units.confidence) if building.units else "  units      ?")
        print("  size       %.2f x %.2f m" % (building.width, building.depth))
        print("  walls      %d  (%.1f m)" % (len(building.walls),
                                             building.total_wall_length))
        print("  rooms      %d  (%.1f m2 floor, %.1f m2 footprint)" % (
            len(building.rooms), building.floor_area, building.footprint_area))
        print("  openings   %d doors, %d windows" % (s.get("doors", 0),
                                                     s.get("windows", 0)))
        print("  validation %s" % ("PASS" if building.validation.get("ok")
                                   else "FAIL"))
        for w in building.validation.get("warnings", [])[:6]:
            print("    ! %s" % w)
        print("  written    %s" % out)
        print("  timings    %s" % json.dumps(building.timings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
