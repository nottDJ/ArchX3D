"""
ArchX3D — The reconstruction pipeline
=====================================
One function, :func:`reconstruct`, runs the whole deterministic path:

    DXF -> read+classify -> units -> opening evidence -> walls
        -> structures -> openings -> rooms -> buildings and storeys
        -> validate -> Drawing

and one CLI runs it from a shell. No network, no API key, no model. That is
the point of the rewrite: the geometry engine is arithmetic, and arithmetic
does not need a credential.

Stage order, and why it is this order
-------------------------------------
Opening evidence is collected *before* walls, because a wall's line work is
cut at every opening and without knowing where the openings are the engine
cannot tell a doorway from the end of a wall. Walls are split into structures
before openings are classified, because whether a wall is on a building's
envelope depends on *which* building, and that decides what a wide hole in it
is. Rooms come after walls because a room is a consequence of walls. Storeys
come after rooms, because a title is only attached to a plan that exists.
Validation comes before anything is written, because the whole failure this
replaces was a pipeline that wrote confidently.

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
from . import evidence as EV
from . import levels as LV
from . import openings as OP
from . import read as RD
from . import structure as ST
from . import topology as TP
from . import validate as VA
from . import walls as WL
from .ir import Building, Drawing, Level, ReconstructionError

DEFAULT_WALL_HEIGHT = 2.7


def reconstruct(path: str, *,
                wall_height: float = DEFAULT_WALL_HEIGHT,
                user_scale: Optional[float] = None,
                diagnostics_dir: Optional[str] = None,
                strict: bool = True) -> Drawing:
    """Reconstruct a validated :class:`~modules.recon.ir.Drawing` from a DXF.

    Raises :class:`ReconstructionError` when the drawing cannot be turned into
    a model that passes validation. ``strict=False`` downgrades that to a
    returned drawing carrying the failures in ``validation`` — for tooling
    that wants to *see* the bad reconstruction, never for the build path.
    """
    timings: Dict[str, float] = {}
    t_all = time.perf_counter()
    cad = wall_result = drawing = None

    try:
        t = time.perf_counter()
        cad = RD.read(path, user_scale=user_scale)
        timings["read"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        evidence = OP.collect_evidence(cad)
        timings["openings_evidence"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        wall_result = WL.reconstruct(cad, bridges=OP.bridges(evidence),
                                     through=OP.through_spans(evidence))
        timings["walls"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        envelope = TP.envelope_of(cad)
        part = ST.partition(wall_result.walls,
                            connectors=ST._polygons(envelope) if envelope is not None else ())
        assigned = {w.id for s in part.structures for w in s.walls}
        walls = [w for w in wall_result.walls if w.id in assigned]
        # Whether a wall is exterior changes what an opening in it is — a
        # 3 m hole in an outside wall is a garage door, the same hole inside
        # is a cased opening between two rooms. So it is settled per
        # structure, from that structure's own walls, before openings are
        # classified, and settled again from its real footprint once rooms
        # exist. Settling it across the whole sheet made every building but
        # the largest one "interior".
        for s in part.structures:
            WL.label_exterior(s.walls)
        timings["structures"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        openings = OP.assign(evidence, walls, cad, faces=wall_result.faces)
        # Doorways the wall repair had to close over, so the model does not
        # gain a solid slab where the drawing left a way through.
        inferred = [span for span in wall_result.inferred_openings
                    if span[0] in assigned]
        openings += OP.from_inferred(walls, inferred, openings)
        # Doorways drawn as nothing but a break in both faces of a wall. Run
        # on every drawing, not only on one with no opening evidence at all:
        # a plan that marks most doors still leaves some as bare gaps, and
        # building solid wall across those seals the rooms behind them.
        openings += OP.from_gaps(walls, wall_result.faces, openings)
        timings["openings"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        rooms = TP.extract(walls, cad, envelope=envelope)
        dropped = ST.distribute(part, rooms=rooms.rooms, openings=openings,
                                nodes=rooms.nodes,
                                footprint_parts=rooms.footprint_parts,
                                footprint_holes=rooms.footprint_holes)
        ST.demote_roomless(part)
        _settle_envelopes(part, envelope)
        for s in part.structures:
            WL.label_exterior(s.walls, s.footprint)
        OP.link_rooms(openings, rooms.rooms, walls)
        timings["topology"] = round(time.perf_counter() - t, 3)

        t = time.perf_counter()
        plan = LV.infer(part.structures, cad.labels, wall_height=wall_height)
        timings["levels"] = round(time.perf_counter() - t, 3)

        drawing = _assemble(path, cad, part, plan, wall_height=wall_height)
        t = time.perf_counter()
        drawing.source_evidence = EV.source_evidence(cad, drawing)
        try:
            drawing.semantic_document = EV.to_cad_document(cad).to_dict()
        except Exception as exc:  # enrichment must never cost the geometry
            drawing.warnings.append("semantic CAD document unavailable: %s" % exc)
        timings["evidence"] = round(time.perf_counter() - t, 3)
        drawing.stats.update({
            "drawing": cad.summary(),
            "walls": wall_result.stats,
            "rooms": rooms.stats,
            "structures": part.stats,
            "dropped_by_structure": {k: (len(v) if isinstance(v, list) else v)
                                     for k, v in dropped.items()},
        })

        t = time.perf_counter()
        if strict:
            VA.enforce(drawing, diagnostics=_diag_payload(cad, wall_result))
        else:
            VA.validate_drawing(drawing)
        timings["validate"] = round(time.perf_counter() - t, 3)
        timings["total"] = round(time.perf_counter() - t_all, 3)
        drawing.timings = dict(timings)
        if diagnostics_dir:
            D.write_bundle(diagnostics_dir, drawing=cad,
                           wall_result=wall_result, model=drawing)
        return drawing

    except ReconstructionError as exc:
        # Refusal is exactly when the stages are worth seeing, so the bundle
        # is written here too — with whatever got as far as existing.
        exc.diagnostics.setdefault("timings", timings)
        if diagnostics_dir:
            D.write_bundle(diagnostics_dir, drawing=cad,
                           wall_result=wall_result, model=drawing,
                           error=exc.as_dict())
        raise


def _settle_envelopes(part: ST.Partition, envelope) -> None:
    """Give each structure the outline that is actually its own.

    With no stated footprint the outline is the structure's rooms plus its
    walls, exactly as for a lone building. A stated outline is only adopted
    when it is plausibly this building's: a site boundary on a layer named
    ``BOUNDARY`` would otherwise become the footprint of whichever building
    it happened to overlap most.
    """
    from shapely.geometry import Polygon
    from .topology import _footprint, wall_solids
    for s in part.structures:
        own = s.region.area if s.region is not None else 0.0
        if envelope is not None and s.footprint_parts:
            kept = [r for r in s.footprint_parts
                    if Polygon(r).area <= max(own * 2.5, 1.0)]
            if kept:
                s.footprint_parts = kept
                s.footprint = list(kept[0])
                continue
        footprint, holes, parts = _footprint(
            s.rooms, wall_solids(s.walls, grow=0.004))
        s.footprint, s.footprint_holes, s.footprint_parts = footprint, holes, parts


def _diag_payload(cad, wall_result) -> dict:
    out: dict = {}
    if cad is not None:
        out["drawing"] = cad.summary()
    if wall_result is not None:
        out["walls"] = wall_result.stats
    return out


def _bounds(walls, rings, extra=()) -> tuple:
    xs = [p[0] for w in walls for p in (w.start, w.end)]
    ys = [p[1] for w in walls for p in (w.start, w.end)]
    for ring in list(rings) + list(extra):
        xs.extend(p[0] for p in ring)
        ys.extend(p[1] for p in ring)
    if not xs:
        return (0.0, 0.0), (0.0, 0.0)
    return (min(xs), min(ys)), (max(xs), max(ys))


def _structure_extras(cad, structures) -> Dict[str, List[list]]:
    """Building geometry each structure owns that did not become a wall.

    Porch posts, piers and columns are drawn on the wall layer and are part of
    the building's extent — a covered porch makes a house wider — but they
    are too small to reconstruct as walls. They belong to the structure they
    stand beside, and only to it.
    """
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree
    prims = [p for p in RD.frame_prims(cad) if len(p.points) >= 1]
    out: Dict[str, List[list]] = {s.id: [] for s in structures}
    if not prims or not structures:
        return out
    geoms = [LineString(p.points) if len(p.points) >= 2 else Point(p.points[0])
             for p in prims]
    tree = STRtree(geoms)
    for s in structures:
        if s.region is None or s.region.is_empty:
            continue
        reach = s.region.buffer(ST.FRAGMENT_REACH)
        for k in tree.query(reach):
            k = int(k)
            g = geoms[k]
            if not reach.covers(g):
                continue
            # Nearest structure only, so a post between two buildings is not
            # counted by both.
            d = s.region.distance(g)
            if any(o is not s and o.region is not None and o.region.distance(g) < d
                   for o in structures):
                continue
            out[s.id].append(list(prims[k].points))
    return out


def _assemble(path, cad, part: ST.Partition, plan: LV.LevelPlan, *,
              wall_height: float) -> Drawing:
    from .ir import LEVELS_AMBIGUOUS  # noqa: F401 - documented outcome
    drawing = Drawing(
        source_path=os.path.abspath(path),
        units=cad.units,
        origin_offset=cad.origin_offset,
        bounds_min=cad.bounds_min,
        bounds_max=cad.bounds_max,
        north_deg=cad.north_deg,
        default_wall_height=wall_height,
        warnings=list(cad.warnings),
        outlying=cad.outlying,
    )
    extras = _structure_extras(cad, [ls.structure for b in plan.buildings
                                     for ls in b.levels])

    for bn, spec in enumerate(plan.buildings, 1):
        bid = "b%d" % bn
        building = Building(
            id=bid,
            name=spec.designation.title() if spec.designation else "Building %d" % bn,
            designation=spec.designation,
            evidence=list(spec.evidence),
            confidence=spec.confidence,
        )
        for ls in spec.levels:
            s = ls.structure
            lid = "%s.l%d" % (bid, ls.index) if ls.index >= 0 else \
                "%s.b%d" % (bid, -ls.index)
            bmin, bmax = _bounds(s.walls, s.footprint_parts, extras.get(s.id, ()))
            level = Level(
                id=lid, name=ls.name, index=ls.index, elevation=ls.elevation,
                elevation_source=ls.elevation_source,
                height=wall_height, placement=ls.placement, title=ls.title,
                designation=ls.designation,
                evidence=list(s.evidence) + list(ls.evidence),
                confidence=ls.confidence, building_id=bid,
                walls=s.walls, rooms=s.rooms, openings=s.openings, nodes=s.nodes,
                footprint=s.footprint, footprint_holes=s.footprint_holes,
                footprint_parts=s.footprint_parts,
                bounds_min=bmin, bounds_max=bmax,
                units=cad.units, source_path=os.path.abspath(path),
            )
            for w in level.walls:
                w.level_id = lid
            for r in level.rooms:
                r.level_id = lid
            for o in level.openings:
                o.level_id = lid
            level.stats["structure"] = s.id
            building.levels.append(level)
        building.levels.sort(key=lambda l: l.index)
        drawing.buildings.append(building)

    drawing.level_structure = {
        "status": plan.status,
        "reason": plan.reason,
        **plan.detail,
    }
    drawing.review = list(plan.review)
    for pair in part.close_pairs:
        drawing.review.append({
            "code": "CLOSE_STRUCTURES",
            "message": "structures %s and %s are only %.2f m apart; they were "
                       "kept as separate structures because their walls do not "
                       "meet" % (pair["structures"][0], pair["structures"][1],
                                 pair["gap_m"]),
            "structures": list(pair["structures"])})
    drawing.unassigned = list(part.unassigned)
    for s, reason in plan.dropped:
        drawing.unassigned.append({
            "wall_ids": [w.id for w in s.walls],
            "length_m": round(sum(w.length for w in s.walls), 2),
            "reason": reason,
        })
    if drawing.unassigned:
        total = sum(float(u["length_m"]) for u in drawing.unassigned)
        drawing.review.append({
            "code": "UNASSIGNED_WALLS",
            "message": "%.1f m of wall in %d group(s) belongs to no building and "
                       "was not built" % (total, len(drawing.unassigned))})
    return drawing


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def describe(drawing: Drawing) -> List[str]:
    """Human-readable account of a reconstruction, one line per fact."""
    s = drawing.summary()
    lines = []
    u = drawing.units
    lines.append("units      %s (%s, confidence %.2f)" % (
        u.unit_name, u.method, u.confidence) if u else "units      ?")
    lines.append("buildings  %d   levels %d   status %s   level structure %s" % (
        s["buildings"], s["levels"], s.get("status"), s.get("level_structure")))
    q = drawing.validation.get("quality") or {}
    if q:
        lines.append("quality    confidence %.2f (unit %.2f, scale %.2f)   openings %s" % (
            q.get("confidence", 0.0), q.get("unit_confidence", 0.0),
            q.get("scale_confidence", 0.0), json.dumps(q.get("openings_by_class", {}))))
    for b in drawing.buildings:
        lines.append("  %s %s" % (b.id, b.name))
        for l in b.levels:
            ls = l.summary()
            lines.append("    %-8s %-18s %6.2f x %-6.2f m  z=%-5.2f walls %-4d rooms %-4d "
                         "doors %-3d windows %-3d%s" % (
                             l.id, l.name, l.width, l.depth, l.elevation,
                             ls["walls"], ls["rooms"], ls["doors"], ls["windows"],
                             ("  title %r" % l.title) if l.title else ""))
    for item in drawing.review[:8]:
        lines.append("  review: [%s] %s" % (item.get("code"), item.get("message")))
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="modules.recon.pipeline",
        description="Reconstruct a validated architectural model from a DXF. "
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
        drawing = reconstruct(
            args.dxf, wall_height=args.wall_height, user_scale=args.scale,
            diagnostics_dir=args.diagnostics, strict=not args.no_strict)
    except ReconstructionError as exc:
        print("RECONSTRUCTION FAILED (%s): %s" % (exc.stage, exc), file=sys.stderr)
        for f in exc.failures:
            print("  - %s" % f, file=sys.stderr)
        if args.diagnostics:
            print("  diagnostics written to %s" % args.diagnostics, file=sys.stderr)
        return 2

    drawing.to_json(out)
    if not args.quiet:
        print("ArchX3D reconstruction (CPU / offline)")
        print("  source     %s" % os.path.basename(args.dxf))
        for line in describe(drawing):
            print("  " + line)
        for w in drawing.validation.get("warnings", [])[:6]:
            print("    ! %s" % w)
        print("  written    %s" % out)
        print("  timings    %s" % json.dumps(drawing.timings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
