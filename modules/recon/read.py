"""
ArchX3D — DXF reading: entities in, classified metric geometry out
==================================================================
The only module that knows what a DXF is. Everything downstream sees
:class:`CadDrawing` — flat lists of classified polylines, labels and dimension
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
    """A TEXT/MTEXT string with the point it is anchored at.

    ``extent`` is the approximate box the string occupies, ``(x0, y0, x1,
    y1)``. It is estimated from the character height, the string length and the
    entity's own alignment, because no font metrics are available here — which
    is accurate enough for its one job: telling whether a plan title sits
    *under* a plan or merely *near* it. The insertion point alone cannot answer
    that for left-aligned titles, whose insertion point is at their far end.
    """

    id: str
    text: str
    point: XY
    height: float
    layer: str
    role: str = C.ROOM_LABEL
    rotation: float = 0.0
    extent: Optional[Tuple[float, float, float, float]] = None
    #: Where a justified string is really placed: its alignment point when it
    #: has one, its insertion point otherwise.
    anchor: Optional[XY] = None


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
    #: ATTRIB tag -> value. ``ROOM_NAME: MASTER BEDROOM`` is structured
    #: metadata, stronger evidence than loose text inside a polygon.
    attributes: Dict[str, str] = field(default_factory=dict)
    #: The block this insert sits inside, when it is nested.
    parent: Optional[str] = None
    depth: int = 0


@dataclass
class HatchRef:
    """A HATCH's own facts: pattern and boundary. Its boundary is also a prim."""

    id: str
    pattern: str
    solid: bool
    layer: str
    boundary: List[XY] = field(default_factory=list)
    block: Optional[str] = None


@dataclass
class DimRef:
    """A DIMENSION as the drawing states it: measurement, printed text, place."""

    id: str
    measurement: float
    text: str
    position: XY
    layer: str
    kind: str = "linear"


@dataclass
class CadDrawing:
    """Everything read from one DXF, classified, in metres, origin-normalised."""

    source_path: str = ""
    prims: List[Prim] = field(default_factory=list)
    arcs: List[Arc] = field(default_factory=list)
    labels: List[Label] = field(default_factory=list)
    inserts: List[BlockRef] = field(default_factory=list)
    dimensions: List[float] = field(default_factory=list)
    dimension_refs: List[DimRef] = field(default_factory=list)
    hatches: List[HatchRef] = field(default_factory=list)
    dxf_version: str = ""
    #: Every layer in the DXF's layer table, ``name -> {"off", "frozen"}``,
    #: including layers that hold only block references and so never appear
    #: in ``layer_counts``.
    layer_table: Dict[str, Dict[str, bool]] = field(default_factory=dict)
    #: INSERTs per layer; kept apart from ``layer_counts``, which counts the
    #: geometry wall detection weighs.
    insert_counts: Dict[str, int] = field(default_factory=dict)
    units: Optional[UnitDecision] = None
    insunits: Optional[int] = None
    origin_offset: XY = (0.0, 0.0)
    bounds_min: XY = (0.0, 0.0)
    bounds_max: XY = (0.0, 0.0)
    north_deg: float = 0.0
    layer_survey: Dict[str, C.Classification] = field(default_factory=dict)
    layer_counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    #: Line work ``robust_bounds`` left outside the plan frame, when there is
    #: enough of it to be worth telling the user about. ``None`` when the
    #: drawing is a single coherent plan, which is the usual case. See
    #: :func:`survey_outlying`.
    outlying: Optional[Dict[str, object]] = None
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


def _ocs(e):
    """The entity's object coordinate system, or ``None`` when it is the WCS.

    ARC, CIRCLE, LWPOLYLINE, 2D POLYLINE, TEXT, INSERT, SOLID and HATCH are
    stored in an OCS defined by their extrusion vector. For a plan drawn the
    normal way that is the world, but a *mirrored* block — the usual way a
    drafter flips a door to swing the other way — writes its contents with
    extrusion ``(0, 0, -1)``, and read raw their x coordinates are negated.
    """
    try:
        ext = e.dxf.extrusion
    except Exception:
        return None
    if ext is None:
        return None
    if abs(ext[0]) < 1e-9 and abs(ext[1]) < 1e-9 and ext[2] > 0:
        return None
    try:
        return e.ocs()
    except Exception:
        return None


def _to_wcs(ocs, pts: Sequence[XY], elevation: float = 0.0) -> List[XY]:
    """OCS points at an elevation, in world plan coordinates."""
    if ocs is None:
        return list(pts)
    out: List[XY] = []
    for x, y in pts:
        v = ocs.to_wcs((x, y, elevation))
        out.append((float(v.x), float(v.y)))
    return out


def _mirrors(ocs) -> bool:
    """Whether the OCS reverses handedness in plan, turning CCW arcs CW."""
    o = ocs.to_wcs((0.0, 0.0, 0.0))
    ux = ocs.to_wcs((1.0, 0.0, 0.0))
    uy = ocs.to_wcs((0.0, 1.0, 0.0))
    return ((ux.x - o.x) * (uy.y - o.y) - (ux.y - o.y) * (uy.x - o.x)) < 0


#: Average advance of one character as a fraction of the text height. Plan
#: lettering is mostly capitals in a simplex or sans face, which runs close to
#: this; the estimate only has to place a title on the right side of a plan.
_CHAR_ADVANCE = 0.8


def _box_about(anchor: XY, dx0: float, dy0: float, dx1: float, dy1: float,
               rotation_deg: float) -> Tuple[float, float, float, float]:
    """Axis-aligned bounds of a box given relative to an anchor, rotated."""
    r = math.radians(rotation_deg or 0.0)
    c, s = math.cos(r), math.sin(r)
    xs, ys = [], []
    for px, py in ((dx0, dy0), (dx1, dy0), (dx1, dy1), (dx0, dy1)):
        xs.append(anchor[0] + px * c - py * s)
        ys.append(anchor[1] + px * s + py * c)
    return (min(xs), min(ys), max(xs), max(ys))


def _text_extent(e) -> Optional[Tuple[float, float, float, float]]:
    """Approximate bounds of a TEXT or ATTRIB, honouring its alignment."""
    try:
        text = str(e.dxf.text or "")
        h = float(e.dxf.height or 0.0)
        if not text.strip() or h <= 0:
            return None
        wf = float(getattr(e.dxf, "width", 1.0) or 1.0)
        halign = int(getattr(e.dxf, "halign", 0) or 0)
        valign = int(getattr(e.dxf, "valign", 0) or 0)
        rot = float(getattr(e.dxf, "rotation", 0.0) or 0.0)
        ins = (e.dxf.insert.x, e.dxf.insert.y)
        width = len(text.strip()) * h * _CHAR_ADVANCE * wf
        anchor = ins
        if halign or valign:
            try:
                ap = e.dxf.align_point
                anchor = (ap.x, ap.y)
            except Exception:
                anchor = ins
        if halign in (3, 5):            # aligned / fit: spans insert -> align
            width = max(math.dist(ins, anchor), 1e-9)
            anchor = ins
            x0 = 0.0
        elif halign in (1, 4):          # centre / middle
            x0 = -width / 2.0
        elif halign == 2:               # right
            x0 = -width
        else:
            x0 = 0.0
        y0 = {0: 0.0, 1: 0.0, 2: -h / 2.0, 3: -h}.get(valign, 0.0)
        if halign == 4:
            y0 = -h / 2.0
        return _box_about(anchor, x0, y0, x0 + width, y0 + h, rot)
    except Exception:
        return None


def _text_anchor(e) -> Optional[XY]:
    """A TEXT's real position: the alignment point when it is justified.

    A justified TEXT carries its placement in ``align_point``; some writers
    leave ``insert`` at the origin for such text, which would detach the
    label from the room it names.
    """
    try:
        halign = int(getattr(e.dxf, "halign", 0) or 0)
        valign = int(getattr(e.dxf, "valign", 0) or 0)
        if halign or valign:
            ap = e.dxf.align_point
            return (float(ap.x), float(ap.y))
        return (float(e.dxf.insert.x), float(e.dxf.insert.y))
    except Exception:
        return None


def _mtext_extent(e, text: str) -> Optional[Tuple[float, float, float, float]]:
    """Approximate bounds of an MTEXT from its attachment point and lines."""
    try:
        h = float(e.dxf.char_height or 0.0)
        lines = [ln for ln in (text or "").splitlines() if ln.strip()] or [text or ""]
        if h <= 0 or not any(ln.strip() for ln in lines):
            return None
        width = max(len(ln.strip()) for ln in lines) * h * _CHAR_ADVANCE
        ref = float(getattr(e.dxf, "width", 0.0) or 0.0)
        if ref > 0:
            width = min(width, ref)
        height = h * (1.0 + 0.66 * (len(lines) - 1))
        ap = int(getattr(e.dxf, "attachment_point", 1) or 1)
        col = (ap - 1) % 3            # 0 left, 1 centre, 2 right
        row = (ap - 1) // 3           # 0 top, 1 middle, 2 bottom
        x0 = (0.0, -width / 2.0, -width)[col]
        y1 = (0.0, height / 2.0, height)[row]
        return _box_about((e.dxf.insert.x, e.dxf.insert.y), x0, y1 - height,
                          x0 + width, y1,
                          float(getattr(e.dxf, "rotation", 0.0) or 0.0))
    except Exception:
        return None


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
        self.dim_refs: List[DimRef] = []
        self.hatches: List[HatchRef] = []
        self.warnings: List[str] = []
        #: Entity types seen and not read — 3D solids, meshes, images.
        self.unsupported: Dict[str, int] = {}
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
                self.unsupported[t] = self.unsupported.get(t, 0) + 1
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
        # Vertices and bulges are in the polyline's own OCS; the arcs are
        # interpolated there and the result carried to world coordinates.
        elevation = float(getattr(e.dxf, "elevation", 0.0) or 0.0)
        self._emit(e, _to_wcs(_ocs(e), pts, elevation), closed, block, depth)

    def _do_polyline(self, e, block, depth) -> None:
        try:
            mode = e.get_mode()
        except Exception:
            mode = "AcDb2dPolyline"
        if mode in ("AcDb3dPolyline", "AcDb2dPolyline"):
            verts = list(e.vertices)
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in verts]
            if mode == "AcDb2dPolyline" and verts:
                pts = _to_wcs(_ocs(e), pts, float(verts[0].dxf.location.z))
            self._emit(e, pts, bool(e.is_closed), block, depth)
        else:
            # Mesh and polyface polylines are 3D shapes, never plan walls.
            self._count(e)

    def _do_arc(self, e, block, depth) -> None:
        c = e.dxf.center
        cls = self._classify(e, block)
        r = float(e.dxf.radius)
        a0, a1 = float(e.dxf.start_angle), float(e.dxf.end_angle)
        pts = _arc_points(c.x, c.y, r, a0, a1)
        ocs = _ocs(e)
        centre = (c.x, c.y)
        if ocs is not None:
            # The arc is defined in its own coordinate system. A mirrored
            # door block gives its swing an extrusion of (0, 0, -1); read
            # raw, the hinge lands on the wrong side of the plan.
            centre = _to_wcs(ocs, [centre], c.z)[0]
            pts = _to_wcs(ocs, pts, c.z)
            sp, ep = pts[0], pts[-1]
            if _mirrors(ocs):
                sp, ep = ep, sp
                pts = list(reversed(pts))
            a0 = math.degrees(math.atan2(sp[1] - centre[1], sp[0] - centre[0]))
            a1 = math.degrees(math.atan2(ep[1] - centre[1], ep[0] - centre[0]))
        self.arcs.append(Arc(
            id=self._next_id("a"), centre=centre, radius=r,
            start_deg=a0, end_deg=a1,
            role=cls.role, layer=getattr(e.dxf, "layer", "0"), block=block,
        ))
        self._emit(e, pts, False, block, depth, cls)

    def _do_circle(self, e, block, depth) -> None:
        c = e.dxf.center
        pts = _arc_points(c.x, c.y, e.dxf.radius, 0.0, 360.0)
        self._emit(e, _to_wcs(_ocs(e), pts, c.z), True, block, depth)

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
        z = 0.0
        for name in ("vtx0", "vtx1", "vtx3", "vtx2"):   # DXF SOLID winding
            try:
                v = getattr(e.dxf, name)
                corners.append((v.x, v.y))
                z = v.z
            except Exception:
                pass
        self._emit(e, _to_wcs(_ocs(e), corners, z), True, block, depth)

    _do_trace = _do_solid

    def _do_hatch(self, e, block, depth) -> None:
        # Hatch *boundaries* can be the only closed outline of a wall poche on
        # drawings that hatch their walls. The fill itself is never geometry.
        boundary: List[XY] = []
        ocs = _ocs(e)
        try:
            elevation = float(e.dxf.elevation.z)
        except Exception:
            elevation = 0.0
        try:
            for path in e.paths:
                pts = [(v[0], v[1]) for v in (getattr(path, "vertices", None) or [])]
                pts = _to_wcs(ocs, pts, elevation)
                if len(pts) >= 3:
                    self._emit(e, pts, True, block, depth)
                    if not boundary:
                        boundary = pts
        except Exception:
            pass
        # The pattern is evidence of its own — ``AR-BRSTD`` is brick, a solid
        # fill in a wall band is poche — and it is recorded once, here, so no
        # later stage has to open the file to ask.
        self.hatches.append(HatchRef(
            id=self._next_id("h"),
            pattern=str(getattr(e.dxf, "pattern_name", "") or ""),
            solid=bool(getattr(e.dxf, "solid_fill", 0)),
            layer=getattr(e.dxf, "layer", "0"), boundary=boundary, block=block,
        ))

    def _do_text(self, e, block, depth) -> None:
        ocs = _ocs(e)
        z = float(e.dxf.insert.z)
        extent = _text_extent(e)
        anchor = _text_anchor(e)
        point = (e.dxf.insert.x, e.dxf.insert.y)
        if ocs is not None:
            point = _to_wcs(ocs, [point], z)[0]
            anchor = _to_wcs(ocs, [anchor], z)[0] if anchor else None
            if extent:
                corners = _to_wcs(ocs, [(extent[0], extent[1]), (extent[2], extent[1]),
                                        (extent[2], extent[3]), (extent[0], extent[3])], z)
                extent = (min(c[0] for c in corners), min(c[1] for c in corners),
                          max(c[0] for c in corners), max(c[1] for c in corners))
        self._label(e, point, e.dxf.text, float(e.dxf.height or 0.0),
                    float(getattr(e.dxf, "rotation", 0.0) or 0.0),
                    extent=extent, anchor=anchor)

    def _do_mtext(self, e, block, depth) -> None:
        try:
            txt = e.plain_text()
        except Exception:
            txt = getattr(e, "text", "")
        self._label(e, (e.dxf.insert.x, e.dxf.insert.y), txt,
                    float(e.dxf.char_height or 0.0),
                    float(getattr(e.dxf, "rotation", 0.0) or 0.0),
                    extent=_mtext_extent(e, txt))

    def _do_attrib(self, e, block, depth) -> None:
        try:
            self._label(e, (e.dxf.insert.x, e.dxf.insert.y), e.dxf.text,
                        float(e.dxf.height or 0.0),
                        float(getattr(e.dxf, "rotation", 0.0) or 0.0),
                        extent=_text_extent(e), anchor=_text_anchor(e))
        except Exception:
            pass

    def _do_attdef(self, e, block, depth) -> None:
        pass   # a definition, not a value

    def _label(self, e, point: XY, text: str, height: float, rot: float,
               extent: Optional[Tuple[float, float, float, float]] = None,
               anchor: Optional[XY] = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        layer = self._count(e)
        self.labels.append(Label(
            id=self._next_id("t"), text=text, point=point, height=height,
            layer=layer, rotation=rot, extent=extent, anchor=anchor,
        ))

    def _do_dimension(self, e, block, depth) -> None:
        self._count(e)
        m = 0.0
        try:
            value = e.get_measurement()
            if not isinstance(value, (tuple, list)):
                m = abs(float(value))
                if m > 0:
                    self.dimensions.append(m)
        except Exception:
            pass
        try:
            dp = e.dxf.defpoint
            position = (float(dp.x), float(dp.y))
        except Exception:
            position = (0.0, 0.0)
        try:
            code = int(e.dimtype) & 7
        except Exception:
            code = 0
        printed = str(getattr(e.dxf, "text", "") or "").strip()
        self.dim_refs.append(DimRef(
            id=self._next_id("d"), measurement=m,
            text="" if printed in ("<>", "") else printed,
            position=position, layer=getattr(e.dxf, "layer", "0"),
            kind={0: "linear", 1: "aligned", 2: "angular", 3: "diameter",
                  4: "radial", 5: "angular", 6: "ordinate"}.get(code, "linear"),
        ))
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
        attributes: Dict[str, str] = {}
        try:
            for attrib in e.attribs:
                tag = str(attrib.dxf.tag).strip()
                if tag:
                    attributes[tag] = str(attrib.dxf.text or "").strip()
        except Exception:
            pass
        point = _to_wcs(_ocs(e), [(ins.x, ins.y)], ins.z)[0]
        ref = BlockRef(
            id=self._next_id("i"), name=name, point=point,
            rotation=float(getattr(e.dxf, "rotation", 0.0) or 0.0),
            xscale=float(getattr(e.dxf, "xscale", 1.0) or 1.0),
            yscale=float(getattr(e.dxf, "yscale", 1.0) or 1.0),
            layer=getattr(e.dxf, "layer", "0"), role=cls.role,
            attributes=attributes, parent=block, depth=depth,
        )
        self.inserts.append(ref)
        if depth >= MAX_BLOCK_DEPTH:
            self.warnings.append("block nesting deeper than %d at %r; not expanded"
                                 % (MAX_BLOCK_DEPTH, name))
            return
        before = len(self.prims)
        insert_layer = getattr(e.dxf, "layer", "0")
        try:
            # virtual_entities applies the insert's full transform (scale,
            # rotation, OCS) to every nested entity, including nested INSERTs.
            for sub in e.virtual_entities():
                # Block content drawn on layer 0 takes the layer of the INSERT
                # that places it — AutoCAD's own rule, and the reason drafters
                # build symbols on 0. Read as "0", a window block inserted on
                # a window layer is geometry of no role at all.
                if getattr(sub.dxf, "layer", "0") == "0" and insert_layer != "0":
                    try:
                        sub.dxf.layer = insert_layer
                    except Exception:
                        pass
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

def read(path: str, *, user_scale: Optional[float] = None) -> CadDrawing:
    """Read a DXF into a classified, metric, origin-normalised :class:`CadDrawing`.

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
        try:
            doc, _auditor = recover.readfile(path)
        except Exception as exc:
            raise ReconstructionError(
                "the file is not a readable DXF: %s" % exc, stage="read",
                failures=["the file is not a readable DXF (%s)" % type(exc).__name__])
        recovered = True

    reader = _Reader(doc)
    if recovered:
        reader.warnings.append("file needed structural recovery before reading")

    for e in doc.modelspace():
        reader.entity(e)
    t_read = time.perf_counter() - t0

    if not reader.prims:
        failures = ["the drawing contains no line work at all (%d text, "
                    "%d dimension entities)" % (len(reader.labels), len(reader.dim_refs))]
        if reader.unsupported:
            failures.append("it holds only entity types a plan is not read from: %s"
                            % ", ".join("%d %s" % (n, t) for t, n in
                                        sorted(reader.unsupported.items())))
        if reader.warnings:
            failures.append("%d entities could not be read" % len(reader.warnings))
        raise ReconstructionError(
            "the drawing contains no usable geometry", stage="read",
            diagnostics={"warnings": reader.warnings,
                         "labels": len(reader.labels),
                         "dimensions": len(reader.dim_refs),
                         "unsupported": dict(reader.unsupported),
                         "layers": reader.layer_counts},
            failures=failures)

    drawing = CadDrawing(
        source_path=os.path.abspath(path),
        prims=reader.prims, arcs=reader.arcs, labels=reader.labels,
        inserts=reader.inserts, dimensions=reader.dimensions,
        dimension_refs=reader.dim_refs, hatches=reader.hatches,
        dxf_version=str(getattr(doc, "dxfversion", "") or ""),
        layer_table={name: {"off": off, "frozen": frozen}
                     for name, (off, frozen) in reader._layer_state.items()},
        insert_counts=_histogram_of(i.layer for i in reader.inserts if i.depth == 0),
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


def _histogram_of(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


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


def frame_prims(drawing: CadDrawing) -> List[Prim]:
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


#: Line work outside the frame is only worth reporting when there is enough of
#: it to be a drawing rather than a stray block. ``robust_bounds`` trims 2% at
#: each end before growing back, so anything that survives that and still lies
#: outside is deliberate geometry, not noise.
_OUTLYING_MIN_FRACTION = 0.02

#: ...and a second trigger by *count*, because length cannot see the case that
#: matters most. A plan copied at 1/1000 carries a thousandth of the drawing's
#: line length however many entities it has, so no length threshold will ever
#: reach it - but it has as many segments as the plan it copies. A genuine
#: stray (one door block inserted 23 km out) is a handful of entities among
#: hundreds and stays well under this.
_OUTLYING_MIN_COUNT_FRACTION = 0.10

#: Below this many outlying segments there is nothing to describe, whatever
#: the fractions say - it guards tiny drawings, where two segments are 10%.
_OUTLYING_MIN_SEGMENTS = 8

#: Below this ratio of diagonals the outlying cluster is not a detail drawn
#: beside the plan, it is the same thing drawn at a different scale.
_OUTLYING_SCALE_RATIO = 0.25


def survey_outlying(prims: Sequence[Prim],
                    frame: Tuple[float, float, float, float]
                    ) -> Optional[Dict[str, object]]:
    """Describe the line work ``robust_bounds`` excluded, if there is much.

    The trimming itself is right - a plan copied at 1/1000 beside the real one
    must not be allowed to decide how big the building is. What was wrong was
    doing it in silence: the user saw a valid single-building reconstruction
    and no hint that half their sheet had been set aside. This reports what was
    left out and how big it was, so the caller can say so.

    Returns ``None`` for the ordinary case of one coherent plan.
    """
    fx0, fy0, fx1, fy1 = frame
    tol_x = max((fx1 - fx0) * 1e-3, 1e-9)
    tol_y = max((fy1 - fy0) * 1e-3, 1e-9)

    def outside(pt) -> bool:
        x, y = pt
        return (x < fx0 - tol_x or x > fx1 + tol_x
                or y < fy0 - tol_y or y > fy1 + tol_y)

    total = 0.0
    out_len = 0.0
    xs: List[float] = []
    ys: List[float] = []
    count = 0
    seen = 0
    for p in prims:
        for a, b in p.segments:
            length = math.dist(a, b)
            total += length
            seen += 1
            # Both ends out, so a wall crossing the frame edge is not counted.
            if outside(a) and outside(b):
                out_len += length
                count += 1
                xs += [a[0], b[0]]
                ys += [a[1], b[1]]

    if total <= 0 or not xs or count < _OUTLYING_MIN_SEGMENTS:
        return None
    by_length = out_len / total >= _OUTLYING_MIN_FRACTION
    by_count = seen > 0 and count / seen >= _OUTLYING_MIN_COUNT_FRACTION
    if not (by_length or by_count):
        return None

    ox0, ox1 = min(xs), max(xs)
    oy0, oy1 = min(ys), max(ys)
    frame_diag = math.hypot(fx1 - fx0, fy1 - fy0)
    out_diag = math.hypot(ox1 - ox0, oy1 - oy0)
    ratio = (out_diag / frame_diag) if frame_diag > 0 else 0.0

    return {
        "segments": count,
        "segment_fraction": round(count / seen, 4) if seen else 0.0,
        "length_fraction": round(out_len / total, 4),
        "bounds": (ox0, oy0, ox1, oy1),
        "size": (ox1 - ox0, oy1 - oy0),
        "frame_size": (fx1 - fx0, fy1 - fy0),
        "diagonal_ratio": round(ratio, 4),
        # A copy of the plan at a fraction of its size is a second scale; a
        # cluster of comparable size beside it is another drawing on the sheet.
        "different_scale": ratio < _OUTLYING_SCALE_RATIO,
    }


def _resolve_units(drawing: CadDrawing, doc, user_scale: Optional[float]) -> None:
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

    # Door swings and room-name lettering carry scale independently of the
    # walls. Only arcs that could be swings count — a quarter circle on a
    # layer that is not furniture, fixtures, landscape or annotation.
    not_swing = {C.FURNITURE, C.FIXTURE, C.CASEWORK, C.LANDSCAPE, C.ELECTRICAL,
                 C.DIMENSION, C.ANNOTATION, C.STAIR, C.TITLE_BLOCK, C.GRID,
                 C.HATCH, C.CONSTRUCTION}
    swings = [a.radius for a in drawing.arcs
              if a.role not in not_swing and 75.0 <= a.sweep_deg <= 105.0]
    heights = [t.height for t in drawing.labels
               if t.height > 0 and 2 <= len(t.text.strip()) <= 24]

    decision = U.resolve(
        segs,
        insunits=drawing.insunits,
        dimensions=drawing.dimensions,
        user_scale=user_scale,
        measurement=doc.header.get("$MEASUREMENT"),
        swing_radii=swings,
        text_heights=heights,
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
        if t.extent:
            t.extent = tuple(v * s for v in t.extent)
        if t.anchor:
            t.anchor = (t.anchor[0] * s, t.anchor[1] * s)
    for h in drawing.hatches:
        h.boundary = [(x * s, y * s) for x, y in h.boundary]
    for dref in drawing.dimension_refs:
        dref.position = (dref.position[0] * s, dref.position[1] * s)
    for i in drawing.inserts:
        i.point = (i.point[0] * s, i.point[1] * s)
        if i.extents:
            i.extents = tuple(v * s for v in i.extents)
    drawing.dimensions = [d * s for d in drawing.dimensions]


def _normalise(drawing: CadDrawing) -> None:
    """Translate so the building's minimum corner is the origin.

    Large CAD world coordinates are a genuine numerical hazard: this plan sits
    near (-725, -522), another test plan near (23000, 2900), and a survey
    drawing can be at (500000, 4000000) where float32 — which is what a GLB
    stores — has spacing of tens of millimetres. Everything downstream works in
    the local frame; ``origin_offset`` converts back.
    """
    frame = robust_bounds(frame_prims(drawing))
    drawing.outlying = survey_outlying(frame_prims(drawing), frame)
    ox, oy, mx, my = frame
    drawing.origin_offset = (ox, oy)

    for p in drawing.prims:
        p.points = [(x - ox, y - oy) for x, y in p.points]
    for a in drawing.arcs:
        a.centre = (a.centre[0] - ox, a.centre[1] - oy)
    for t in drawing.labels:
        t.point = (t.point[0] - ox, t.point[1] - oy)
        if t.extent:
            t.extent = (t.extent[0] - ox, t.extent[1] - oy,
                        t.extent[2] - ox, t.extent[3] - oy)
        if t.anchor:
            t.anchor = (t.anchor[0] - ox, t.anchor[1] - oy)
    for h in drawing.hatches:
        h.boundary = [(x - ox, y - oy) for x, y in h.boundary]
    for dref in drawing.dimension_refs:
        dref.position = (dref.position[0] - ox, dref.position[1] - oy)
    for i in drawing.inserts:
        i.point = (i.point[0] - ox, i.point[1] - oy)
        if i.extents:
            i.extents = (i.extents[0] - ox, i.extents[1] - oy,
                         i.extents[2] - ox, i.extents[3] - oy)

    drawing.bounds_min = (0.0, 0.0)
    drawing.bounds_max = (mx - ox, my - oy)
