"""
ArchX3D — DXF reading: entities in, classified metric geometry out
==================================================================
The only module that knows what a DXF is. Everything downstream sees
:class:`Drawing` — flat lists of classified polylines, labels and dimension
measurements, in metres, in a local frame.

What this stage is responsible for
----------------------------------
* **Flattening.** Architectural DXFs put the building inside blocks, blocks
  inside blocks, and blocks inside xrefs. A reader that only walks modelspace
  sees the INSERT markers and none of the geometry. Every INSERT is expanded
  recursively through its own transform, with a depth cap so a self-referential
  block cannot hang the process.
* **Curve flattening.** ARC, CIRCLE, ELLIPSE and SPLINE become polylines, since
  every stage after this one reasons about straight segments. Door swing arcs
  are kept as arcs *as well*, because their radius is the door width and their
  chord tells you the leaf direction — evidence that flattening destroys.
* **Coordinate correctness.** Entities carry an extrusion vector; an entity
  drawn on a flipped OCS has coordinates that mean nothing until the OCS->WCS
  transform is applied. Skipping this is how a plan comes out mirrored.
* **Classification.** Every entity is labelled by :mod:`modules.recon.classify`
  before anything looks at its geometry, and the label travels with it.
* **Units and normalisation.** One scale factor, resolved from evidence, is
  applied once. The local frame's origin is the minimum corner of the
  *building* geometry — not of the sheet, which would put the origin somewhere
  in the title block half a kilometre away.

What this stage is explicitly *not* responsible for: deciding what is a wall.
It reports what the drawing contains and what each thing was classified as.

Sheet vs building
-----------------
The origin is derived from wall-ish geometry only. The failing plan's extents
run from (-725, -522) to (239, 416) because of notes and dimension strings,
while the building occupies (-649, -217) to (147, 347). Normalising to the
sheet would leave the building floating off-origin with a phantom margin, and
every "is this inside the envelope" test downstream would be answered against
the wrong box.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import classify as C
from . import units as U
from .ir import ReconstructionError, UnitDecision

XY = Tuple[float, float]
Segment = Tuple[XY, XY]

#: How deep block nesting may go before we assume a cycle. Real drawings rarely
#: exceed three or four; xref-of-xref-of-block reaches six.
MAX_BLOCK_DEPTH = 12

#: Chord tolerance for flattening curves, as a fraction of the curve radius.
#: Applied before the unit scale is known, so an absolute distance would mean
#: different things in a millimetre drawing and a metre one.
CURVE_SAGITTA_RATIO = 0.02
MIN_CURVE_SEGMENTS = 8
MAX_CURVE_SEGMENTS = 64


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Prim:
    """One flattened primitive: a polyline with its provenance and role.

    ``points`` is in the drawing's own units until the unit scale has been
    applied, and in metres in the local frame afterwards. Keeping one type for
    lines, polylines and flattened curves is what lets the wall detector treat
    "two parallel LINEs" and "one closed LWPOLYLINE" as the same problem.
    """

    id: str
    points: List[XY]
    closed: bool
    role: str
    confidence: float
    reason: str
    source: str
    dxftype: str
    layer: str
    block: Optional[str] = None
    depth: int = 0

    @property
    def segments(self) -> List[Segment]:
        pts = self.points + ([self.points[0]] if self.closed and len(self.points) > 2 else [])
        return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]

    @property
    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in self.segments)

    @property
    def is_wall(self) -> bool:
        return self.role in C.WALL_ROLES


@dataclass
class Arc:
    """A preserved arc. Door swings are arcs and lose their meaning as chords."""

    id: str
    centre: XY
    radius: float
    start_deg: float
    end_deg: float
    role: str
    layer: str
    block: Optional[str] = None

    def point_at(self, deg: float) -> XY:
        r = math.radians(deg)
        return (self.centre[0] + self.radius * math.cos(r),
                self.centre[1] + self.radius * math.sin(r))

    @property
    def start_point(self) -> XY:
        return self.point_at(self.start_deg)

    @property
    def end_point(self) -> XY:
        return self.point_at(self.end_deg)

    @property
    def sweep_deg(self) -> float:
        s = (self.end_deg - self.start_deg) % 360.0
        return s if s > 1e-9 else 360.0


@dataclass
class Label:
    """A TEXT/MTEXT string with the point it is anchored at."""

    id: str
    text: str
    point: XY
    height: float
    layer: str
    role: str = C.ROOM_LABEL
    rotation: float = 0.0


@dataclass
class BlockRef:
    """An INSERT, kept as a record even though its geometry was flattened.

    Door and window symbols are usually blocks, and the insertion point plus
    the block's name and scale is far better opening evidence than the loose
    lines that fell out of it.
    """

    id: str
    name: str
    point: XY
    rotation: float
    xscale: float
    yscale: float
    layer: str
    role: str
    extents: Optional[Tuple[float, float, float, float]] = None


@dataclass
class Drawing:
    """Everything read from one DXF, classified, in metres, origin-normalised."""

    source_path: str = ""
    prims: List[Prim] = field(default_factory=list)
    arcs: List[Arc] = field(default_factory=list)
    labels: List[Label] = field(default_factory=list)
    inserts: List[BlockRef] = field(default_factory=list)
    dimensions: List[float] = field(default_factory=list)
    units: Optional[UnitDecision] = None
    insunits: Optional[int] = None
    origin_offset: XY = (0.0, 0.0)
    bounds_min: XY = (0.0, 0.0)
    bounds_max: XY = (0.0, 0.0)
    north_deg: float = 0.0
    layer_survey: Dict[str, C.Classification] = field(default_factory=dict)
    layer_counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    timings: Dict[str, float] = field(default_factory=dict)

    # -- selection helpers --------------------------------------------------

    def by_role(self, *roles: str) -> List[Prim]:
        want = set(roles)
        return [p for p in self.prims if p.role in want]

    def wall_prims(self) -> List[Prim]:
        return [p for p in self.prims if p.is_wall]

    def segments_of(self, prims: Sequence[Prim]) -> List[Segment]:
        out: List[Segment] = []
        for p in prims:
            out.extend(p.segments)
        return out

    @property
    def width(self) -> float:
        return self.bounds_max[0] - self.bounds_min[0]

    @property
    def depth(self) -> float:
        return self.bounds_max[1] - self.bounds_min[1]

    def summary(self) -> dict:
        roles: Dict[str, int] = {}
        for p in self.prims:
            roles[p.role] = roles.get(p.role, 0) + 1
        return {
            "source": os.path.basename(self.source_path),
            "primitives": len(self.prims),
            "arcs": len(self.arcs),
            "labels": len(self.labels),
            "inserts": len(self.inserts),
            "dimensions": len(self.dimensions),
            "roles": dict(sorted(roles.items(), key=lambda kv: -kv[1])),
            "size_m": [round(self.width, 3), round(self.depth, 3)],
            "units": self.units.as_dict() if self.units else None,
        }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _arc_points(cx: float, cy: float, r: float, a0: float, a1: float) -> List[XY]:
    """Flatten an arc from a0 to a1 (degrees, counter-clockwise)."""
    sweep = (a1 - a0) % 360.0
    if sweep < 1e-9:
        sweep = 360.0
    if r <= 0:
        return [(cx, cy)]
    # sagitta = r(1-cos(theta/2)); solve for the step that keeps it in budget
    budget = max(CURVE_SAGITTA_RATIO, 1e-6)
    step = 2.0 * math.degrees(math.acos(max(-1.0, min(1.0, 1.0 - budget))))
    n = int(math.ceil(sweep / max(step, 1e-6)))
    n = max(MIN_CURVE_SEGMENTS, min(MAX_CURVE_SEGMENTS, n))
    return [
        (cx + r * math.cos(math.radians(a0 + sweep * i / n)),
         cy + r * math.sin(math.radians(a0 + sweep * i / n)))
        for i in range(n + 1)
    ]


def _bulge_points(p1: XY, p2: XY, bulge: float) -> List[XY]:
    """Intermediate points for one bulged LWPOLYLINE span.

    A bulge is the tangent of a quarter of the included angle. Dropping bulges
    turns a rounded bay window into a straight chord, which then reads as a
    wall of the wrong length.
    """
    if abs(bulge) < 1e-9:
        return []
    try:
        from ezdxf.math import bulge_to_arc
        centre, start_a, end_a, radius = bulge_to_arc(p1, p2, bulge)
        pts = _arc_points(centre[0], centre[1], radius,
                          math.degrees(start_a), math.degrees(end_a))
        return [(p[0], p[1]) for p in pts[1:-1]]
    except Exception:
        return []


def _dedupe_points(pts: Sequence[XY], tol: float) -> List[XY]:
    out: List[XY] = []
    for p in pts:
        q = (float(p[0]), float(p[1]))
        if not out or math.dist(out[-1], q) > tol:
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# Entity expansion
# ---------------------------------------------------------------------------

class _Reader:
    """Walks one DXF document, producing classified primitives.

    Held as a class purely so the id counter, the warning list and the layer
    state table do not have to be threaded through every function.
    """

    def __init__(self, doc):
        self.doc = doc
        self.prims: List[Prim] = []
        self.arcs: List[Arc] = []
        self.labels: List[Label] = []
        self.inserts: List[BlockRef] = []
        self.dimensions: List[float] = []
        self.warnings: List[str] = []
        self.layer_counts: Dict[str, int] = {}
        self._n = 0
        self._layer_state: Dict[str, Tuple[bool, bool]] = {}
        for layer in doc.layers:
            try:
                self._layer_state[layer.dxf.name] = (bool(layer.is_off()),
                                                     bool(layer.is_frozen()))
            except Exception:
                self._layer_state[layer.dxf.name] = (False, False)

    def _next_id(self, prefix: str) -> str:
        self._n += 1
        return "%s%d" % (prefix, self._n)

    def _state(self, layer: str) -> Tuple[bool, bool]:
        return self._layer_state.get(layer, (False, False))

    # -- classification ----------------------------------------------------

    def _classify(self, e, block: Optional[str]) -> C.Classification:
        layer = getattr(e.dxf, "layer", "0")
        off, frozen = self._state(layer)
        cls = C.classify_entity(e.dxftype(), layer, block_name=block,
                                is_off=off, is_frozen=frozen)
        # Geometry inside a block whose *own* name is a fixture or furniture
        # symbol is that symbol even when the block sits on a wall layer —
        # drafters routinely leave a toilet block on A-WALL. The block name is
        # the more specific statement, so it overrides a generic layer
        # classification, but it never overrides an explicit annotation type.
        if block and cls.role in (C.WALL, C.UNKNOWN):
            brole, bconf, breason = C.block_role(block)
            if brole not in (C.UNKNOWN, C.WALL) and bconf >= 0.7:
                return C.Classification(role=brole, confidence=bconf,
                                        reason=breason, source="block")
        return cls

    def _count(self, e) -> str:
        layer = getattr(e.dxf, "layer", "0")
        self.layer_counts[layer] = self.layer_counts.get(layer, 0) + 1
        return layer

    def _emit(self, e, pts: Sequence[XY], closed: bool, block: Optional[str],
              depth: int, cls: Optional[C.Classification] = None) -> None:
        pts = _dedupe_points(pts, 1e-9)
        if len(pts) < 2:
            return
        cls = cls or self._classify(e, block)
        layer = self._count(e)
        self.prims.append(Prim(
            id=self._next_id("p"), points=list(pts), closed=closed,
            role=cls.role, confidence=cls.confidence, reason=cls.reason,
            source=cls.source, dxftype=e.dxftype(), layer=layer,
            block=block, depth=depth,
        ))

    # -- dispatch -----------------------------------------------------------

    def entity(self, e, block: Optional[str] = None, depth: int = 0) -> None:
        t = e.dxftype()
        try:
            handler = getattr(self, "_do_" + t.lower(), None)
            if handler is not None:
                handler(e, block, depth)
            else:
                # Unhandled types are still counted, so diagnostics can show
                # that something was seen and ignored rather than silently
                # vanishing.
                self._count(e)
        except Exception as exc:  # one bad entity must not lose the drawing
            self.warnings.append("skipped %s on %r: %s" % (
                t, getattr(e.dxf, "layer", "?"), exc))

    # -- per-type handlers --------------------------------------------------

    def _do_line(self, e, block, depth) -> None:
        s, en = e.dxf.start, e.dxf.end
        self._emit(e, [(s.x, s.y), (en.x, en.y)], False, block, depth)

    def _do_lwpolyline(self, e, block, depth) -> None:
        pts: List[XY] = []
        raw = list(e.get_points("xyb"))
        n = len(raw)
        closed = bool(e.closed)
        for i, (x, y, b) in enumerate(raw):
            pts.append((x, y))
            if i + 1 < n:
                nxt = raw[i + 1]
            elif closed and n > 2:
                nxt = raw[0]
            else:
                nxt = None
            if nxt is not None and abs(b) > 1e-9:
                pts.extend(_bulge_points((x, y), (nxt[0], nxt[1]), b))
        self._emit(e, pts, closed, block, depth)

    def _do_polyline(self, e, block, depth) -> None:
        try:
            mode = e.get_mode()
        except Exception:
            mode = "AcDb2dPolyline"
        if mode in ("AcDb3dPolyline", "AcDb2dPolyline"):
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            self._emit(e, pts, bool(e.is_closed), block, depth)
        else:
            # Mesh and polyface polylines are 3D shapes, never plan walls.
            self._count(e)

    def _do_arc(self, e, block, depth) -> None:
        c = e.dxf.center
        cls = self._classify(e, block)
        self.arcs.append(Arc(
            id=self._next_id("a"), centre=(c.x, c.y), radius=float(e.dxf.radius),
            start_deg=float(e.dxf.start_angle), end_deg=float(e.dxf.end_angle),
            role=cls.role, layer=getattr(e.dxf, "layer", "0"), block=block,
        ))
        self._emit(e, _arc_points(c.x, c.y, e.dxf.radius,
                                  e.dxf.start_angle, e.dxf.end_angle),
                   False, block, depth, cls)

    def _do_circle(self, e, block, depth) -> None:
        c = e.dxf.center
        self._emit(e, _arc_points(c.x, c.y, e.dxf.radius, 0.0, 360.0),
                   True, block, depth)

    def _do_ellipse(self, e, block, depth) -> None:
        try:
            tol = float(e.minor_axis.magnitude) * CURVE_SAGITTA_RATIO + 1e-9
            pts = [(p.x, p.y) for p in e.flattening(distance=tol)]
        except Exception:
            return
        self._emit(e, pts, bool(e.is_closed), block, depth)

    def _do_spline(self, e, block, depth) -> None:
        try:
            pts = [(p.x, p.y) for p in e.flattening(distance=0.5, segments=4)]
        except Exception:
            return
        self._emit(e, pts, bool(e.closed), block, depth)

    def _do_solid(self, e, block, depth) -> None:
        corners = []
        for name in ("vtx0", "vtx1", "vtx3", "vtx2"):   # DXF SOLID winding
            try:
                v = getattr(e.dxf, name)
                corners.append((v.x, v.y))
            except Exception:
                pass
        self._emit(e, corners, True, block, depth)

    _do_trace = _do_solid

    def _do_hatch(self, e, block, depth) -> None:
        # Hatch *boundaries* can be the only closed outline of a wall poche on
        # drawings that hatch their walls. The fill itself is never geometry.
        try:
            for path in e.paths:
                pts = [(v[0], v[1]) for v in (getattr(path, "vertices", None) or [])]
                if len(pts) >= 3:
                    self._emit(e, pts, True, block, depth)
        except Exception:
            pass

    def _do_text(self, e, block, depth) -> None:
        self._label(e, (e.dxf.insert.x, e.dxf.insert.y), e.dxf.text,
                    float(e.dxf.height or 0.0),
                    float(getattr(e.dxf, "rotation", 0.0) or 0.0))

    def _do_mtext(self, e, block, depth) -> None:
        try:
            txt = e.plain_text()
        except Exception:
            txt = getattr(e, "text", "")
        self._label(e, (e.dxf.insert.x, e.dxf.insert.y), txt,
                    float(e.dxf.char_height or 0.0),
                    float(getattr(e.dxf, "rotation", 0.0) or 0.0))

    def _do_attrib(self, e, block, depth) -> None:
        try:
            self._label(e, (e.dxf.insert.x, e.dxf.insert.y), e.dxf.text,
                        float(e.dxf.height or 0.0),
                        float(getattr(e.dxf, "rotation", 0.0) or 0.0))
        except Exception:
            pass

    def _do_attdef(self, e, block, depth) -> None:
        pass   # a definition, not a value

    def _label(self, e, point: XY, text: str, height: float, rot: float) -> None:
        text = (text or "").strip()
        if not text:
            return
        layer = self._count(e)
        self.labels.append(Label(
            id=self._next_id("t"), text=text, point=point, height=height,
            layer=layer, rotation=rot,
        ))

    def _do_dimension(self, e, block, depth) -> None:
        self._count(e)
        try:
            m = abs(float(e.get_measurement()))
            if m > 0:
                self.dimensions.append(m)
        except Exception:
            pass
        # The dimension's geometry lives in an anonymous block (*D12). It is
        # never expanded: that block is exactly the 58-foot-wall trap.

    def _do_leader(self, e, block, depth) -> None:
        self._count(e)

    _do_mleader = _do_leader
    _do_multileader = _do_leader
    _do_tolerance = _do_leader

    def _do_insert(self, e, block, depth) -> None:
        name = str(e.dxf.name)
        cls = self._classify(e, name)
        ins = e.dxf.insert
        ref = BlockRef(
            id=self._next_id("i"), name=name, point=(ins.x, ins.y),
            rotation=float(getattr(e.dxf, "rotation", 0.0) or 0.0),
            xscale=float(getattr(e.dxf, "xscale", 1.0) or 1.0),
            yscale=float(getattr(e.dxf, "yscale", 1.0) or 1.0),
            layer=getattr(e.dxf, "layer", "0"), role=cls.role,
        )
        self.inserts.append(ref)
        if depth >= MAX_BLOCK_DEPTH:
            self.warnings.append("block nesting deeper than %d at %r; not expanded"
                                 % (MAX_BLOCK_DEPTH, name))
            return
        before = len(self.prims)
        try:
            # virtual_entities applies the insert's full transform (scale,
            # rotation, OCS) to every nested entity, including nested INSERTs.
            for sub in e.virtual_entities():
                self.entity(sub, block=name, depth=depth + 1)
        except Exception as exc:
            self.warnings.append("could not expand block %r: %s" % (name, exc))
        xs: List[float] = []
        ys: List[float] = []
        for p in self.prims[before:]:
            for x, y in p.points:
                xs.append(x)
                ys.append(y)
        if xs:
            ref.extents = (min(xs), min(ys), max(xs), max(ys))

    def _do_acad_proxy_entity(self, e, block, depth) -> None:
        # Geometry from an application we do not have. Frequently AEC walls,
        # but unreadable, so it is recorded as a warning rather than guessed at.
        self._count(e)
        self.warnings.append("ACAD_PROXY_ENTITY on %r could not be read"
                             % getattr(e.dxf, "layer", "?"))

    def _do_viewport(self, e, block, depth) -> None:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def read(path: str, *, user_scale: Optional[float] = None) -> Drawing:
    """Read a DXF into a classified, metric, origin-normalised :class:`Drawing`.

    ``user_scale`` overrides unit resolution entirely — the escape hatch for a
    drawing whose geometry is too unusual for the evidence to settle.
    """
    import ezdxf
    from ezdxf import recover

    t0 = time.perf_counter()
    if not os.path.exists(path):
        raise ReconstructionError("DXF not found: %s" % path, stage="read")
    try:
        doc = ezdxf.readfile(path)
        recovered = False
    except Exception:
        # Real-world DXFs are frequently slightly malformed. recover.readfile
        # repairs what it can; refusing them outright would fail on files every
        # CAD program opens without complaint.
        doc, _auditor = recover.readfile(path)
        recovered = True

    reader = _Reader(doc)
    if recovered:
        reader.warnings.append("file needed structural recovery before reading")

    for e in doc.modelspace():
        reader.entity(e)
    t_read = time.perf_counter() - t0

    if not reader.prims:
        raise ReconstructionError(
            "the drawing contains no usable geometry", stage="read",
            diagnostics={"warnings": reader.warnings})

    drawing = Drawing(
        source_path=os.path.abspath(path),
        prims=reader.prims, arcs=reader.arcs, labels=reader.labels,
        inserts=reader.inserts, dimensions=reader.dimensions,
        insunits=doc.header.get("$INSUNITS"),
        warnings=reader.warnings, layer_counts=reader.layer_counts,
    )
    drawing.layer_survey = C.survey_layers(list(reader.layer_counts.keys()))
    drawing.north_deg = _north(doc)

    t1 = time.perf_counter()
    _resolve_units(drawing, doc, user_scale)
    _normalise(drawing)
    drawing.timings = {
        "read": round(t_read, 4),
        "units": round(time.perf_counter() - t1, 4),
    }
    return drawing


def _north(doc) -> float:
    """Plan-north heading in degrees, 0 = up the page.

    ``$NORTHDIRECTION`` is measured counter-clockwise from the x axis, so the
    angle measured from *up* is 90 degrees less.
    """
    try:
        rad = float(doc.header.get("$NORTHDIRECTION", 0.0) or 0.0)
    except Exception:
        return 0.0
    return (math.degrees(rad) - 90.0) % 360.0


#: Roles whose geometry may define the building's frame, in preference order.
_FRAME_ROLES = (C.WALL, C.FOOTPRINT, C.STRUCTURE_BELOW, C.OPENING,
                C.WINDOW, C.DOOR, C.STRUCTURE_ABOVE)

#: A frame is grown from its robust core by at most this fraction of the
#: core's own size. Enough to take in a porch or a projecting bay; not enough
#: to take in a block inserted twenty kilometres away.
_FRAME_GROWTH = 0.35


def frame_prims(drawing: Drawing) -> List[Prim]:
    """The geometry that gets to say where the building is.

    Walls when there are enough of them, because walls are the building. A
    drawing with no wall layer falls back to the wider building roles.
    """
    walls = [p for p in drawing.prims if p.role == C.WALL]
    if len(walls) >= 8:
        return walls
    building = [p for p in drawing.prims if p.role in _FRAME_ROLES]
    return building or list(drawing.prims)


def robust_bounds(prims: Sequence[Prim]
                  ) -> Tuple[float, float, float, float]:
    """Bounds of the geometry's dense core, then grown to include its outskirts.

    A plain min/max is at the mercy of one stray entity, and real drawings
    have them: one test plan carries a door block inserted 23 km from the
    building, another a furniture block 2 km out. Either one, taken at face
    value, makes the plan appear 40 km wide — which then makes every candidate
    unit look absurd, so unit resolution collapses and the building is scaled
    by whatever survived. Trimming to a length-weighted percentile core and
    then re-including everything near it keeps genuine projections while
    discarding the strays.
    """
    xs: List[Tuple[float, float]] = []
    ys: List[Tuple[float, float]] = []
    for p in prims:
        for (x0, y0), (x1, y1) in p.segments:
            w = max(math.dist((x0, y0), (x1, y1)), 1e-6)
            xs.append((x0, w))
            xs.append((x1, w))
            ys.append((y0, w))
            ys.append((y1, w))
    if not xs:
        pts = [q for p in prims for q in p.points]
        if not pts:
            return (0.0, 0.0, 1.0, 1.0)
        return (min(q[0] for q in pts), min(q[1] for q in pts),
                max(q[0] for q in pts), max(q[1] for q in pts))

    def quantile(vals: List[Tuple[float, float]], q: float) -> float:
        total = sum(w for _, w in vals)
        target = total * q
        acc = 0.0
        for v, w in vals:
            acc += w
            if acc >= target:
                return v
        return vals[-1][0]

    def core(vals: List[Tuple[float, float]]) -> Tuple[float, float]:
        """Where the drawing's line work actually is, by weighted percentile.

        Two percent at each end, not a fraction of one: a single block
        inserted nine kilometres away is about one percent of the total wall
        length, and a half-percent cut leaves it in — which is the whole
        problem. An interquartile rule fails the other way, because a plan
        whose walls sit on three distinct gridlines has almost no quartile
        spread and gets trimmed to nothing. Two percent plus the growth pass
        below handles both, and a real projection is restored by the growth.
        """
        vals = sorted(vals)
        return quantile(vals, 0.02), quantile(vals, 0.98)

    x0, x1 = core(xs)
    y0, y1 = core(ys)
    gx = max((x1 - x0) * _FRAME_GROWTH, 1e-9)
    gy = max((y1 - y0) * _FRAME_GROWTH, 1e-9)
    keep_x = [v for v, _ in xs if x0 - gx <= v <= x1 + gx]
    keep_y = [v for v, _ in ys if y0 - gy <= v <= y1 + gy]
    if not keep_x or not keep_y:
        return (x0, y0, x1, y1)
    return (min(keep_x), min(keep_y), max(keep_x), max(keep_y))


def _resolve_units(drawing: Drawing, doc, user_scale: Optional[float]) -> None:
    """Decide metres-per-unit and apply it to every coordinate.

    The evidence handed to the resolver is deliberately *only* geometry that
    could be a wall. Feeding it furniture and hatching floods the parallel-pair
    distribution with 20 mm cabinet lines and the modal thickness stops meaning
    anything.
    """
    candidates = [p for p in drawing.prims
                  if p.role in (C.WALL, C.FOOTPRINT, C.STRUCTURE_BELOW,
                                C.OPENING, C.WINDOW, C.DOOR, C.UNKNOWN)]
    if not candidates:
        candidates = list(drawing.prims)
    # Clip to the frame so a block inserted far outside the building cannot
    # decide how big the plan is; the wall thickness evidence is unaffected
    # either way, but the extent evidence is entirely at its mercy.
    fx0, fy0, fx1, fy1 = robust_bounds(frame_prims(drawing))
    mx, my = (fx1 - fx0) * 0.25, (fy1 - fy0) * 0.25
    segs: List[Segment] = []
    for p in candidates:
        for a, b in p.segments:
            if fx0 - mx <= a[0] <= fx1 + mx and fy0 - my <= a[1] <= fy1 + my:
                segs.append((a, b))
    if len(segs) < 4:
        segs = [s for p in candidates for s in p.segments]

    decision = U.resolve(
        segs,
        insunits=drawing.insunits,
        dimensions=drawing.dimensions,
        user_scale=user_scale,
        measurement=doc.header.get("$MEASUREMENT"),
    )
    drawing.units = decision
    if decision.conflict:
        drawing.warnings.append(decision.conflict)

    s = decision.scale_to_m
    if s == 1.0:
        return
    for p in drawing.prims:
        p.points = [(x * s, y * s) for x, y in p.points]
    for a in drawing.arcs:
        a.centre = (a.centre[0] * s, a.centre[1] * s)
        a.radius *= s
    for t in drawing.labels:
        t.point = (t.point[0] * s, t.point[1] * s)
        t.height *= s
    for i in drawing.inserts:
        i.point = (i.point[0] * s, i.point[1] * s)
        if i.extents:
            i.extents = tuple(v * s for v in i.extents)
    drawing.dimensions = [d * s for d in drawing.dimensions]


def _normalise(drawing: Drawing) -> None:
    """Translate so the building's minimum corner is the origin.

    Large CAD world coordinates are a genuine numerical hazard: this plan sits
    near (-725, -522), another test plan near (23000, 2900), and a survey
    drawing can be at (500000, 4000000) where float32 — which is what a GLB
    stores — has spacing of tens of millimetres. Everything downstream works in
    the local frame; ``origin_offset`` converts back.
    """
    ox, oy, mx, my = robust_bounds(frame_prims(drawing))
    drawing.origin_offset = (ox, oy)

    for p in drawing.prims:
        p.points = [(x - ox, y - oy) for x, y in p.points]
    for a in drawing.arcs:
        a.centre = (a.centre[0] - ox, a.centre[1] - oy)
    for t in drawing.labels:
        t.point = (t.point[0] - ox, t.point[1] - oy)
    for i in drawing.inserts:
        i.point = (i.point[0] - ox, i.point[1] - oy)
        if i.extents:
            i.extents = (i.extents[0] - ox, i.extents[1] - oy,
                         i.extents[2] - ox, i.extents[3] - oy)

    drawing.bounds_min = (0.0, 0.0)
    drawing.bounds_max = (mx - ox, my - oy)
