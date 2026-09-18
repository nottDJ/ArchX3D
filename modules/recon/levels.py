"""
ArchX3D — Buildings and storeys from the drawing's own evidence
===============================================================
Decides, for the structures :mod:`modules.recon.structure` separated, which
are separate buildings and which are storeys of one building — and says so
when the drawing does not settle it.

The problem
-----------
A sheet with two disconnected floor plans on it is one of three things:

* two buildings on one site — a house and its detached garage;
* two storeys of one building, drawn side by side because paper is flat;
* genuinely unclear — two congruent floor plates with nothing saying which.

Geometry alone cannot tell the first two apart. A house beside its garage and
a ground floor beside its first floor are both "two closed wall networks a few
metres apart". What tells them apart is what the drafter wrote: a title under
each plan (``GROUND FLOOR PLAN``), level tokens in the layer names
(``GF-WALL``), a building designation (``BLOCK A``).

The rule
--------
Storeys are only ever inferred from **positive evidence**. Disconnected
geometry is never stacked because it happens to be disconnected.

* Structures carrying distinct level designations become storeys of one
  building, ordered by the designations and registered over each other by
  their walls.
* Structures with no level evidence are separate buildings.
* Structures with no level evidence whose footprints are *congruent* — the
  same floor plate repeated — could be storeys or twin buildings. That is
  :data:`~modules.recon.ir.LEVELS_AMBIGUOUS`: they are kept apart exactly as
  drawn and flagged for a person to decide, because stacking them is a guess
  and merging them is wrong.

Text is only evidence when it is placed like a title. A floor-area schedule
lists ``FLOOR - FIRST`` and ``FLOOR - SECOND`` one above the other in a table,
and a stair is labelled ``UP TO FIRST FLOOR``; neither names a plan, and both
are rejected with the reason recorded.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .structure import Structure

XY = Tuple[float, float]

#: A title may sit this far from the plan it names, in metres, or this
#: fraction of the plan's own size if that is more. Titles clear the plan's
#: dimension strings, which on a large plan run several metres out.
TITLE_REACH = 6.0
TITLE_REACH_RATIO = 0.75

#: A title is ambiguous if a second plan is nearly as close as the first.
TITLE_AMBIGUITY_RATIO = 1.3

#: Footprints whose best-aligned overlap reaches this IoU are the same floor
#: plate repeated.
CONGRUENT_IOU = 0.8

#: Below this wall-registration score the storeys' relative placement is
#: reported as unreliable.
MIN_REGISTRATION = 0.25

#: Largest raster used for footprint correlation, in cells per side.
MAX_CELLS = 640


# ---------------------------------------------------------------------------
# Designations in text
# ---------------------------------------------------------------------------

_ORDINALS = {
    "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6,
    "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10, "ELEVENTH": 11,
    "TWELFTH": 12,
}
_STOREY = r"(?:FLOOR|FLR|LEVEL|LVL|STOREY|STORY)"

#: Strings that mention a floor without naming a plan.
_NOT_A_TITLE = re.compile(
    r"\b(UP|DN|DOWN|TO|FROM|SLAB|FFL|SFL|SILL|LINTEL|BEAM|COLUMN|AREA|SQ|SQM|"
    r"SQFT|M2|NET|FSI|FAR|BUILT|BUILTUP|DEDUCTION|HEIGHT|HT|THK|THICK|FINISH|"
    r"NAME|TOTAL|NO|TILE|TILES|SKIRTING|DETAIL|SCHEDULE)\b")

_SURVEY = re.compile(r"\b(T\s*S|S\s*F|SURVEY|PLOT|WARD|VILLAGE|PATTA|KHASRA)\b")

# Storey vocabulary of the other languages drawings are commonly titled in.
# Language, not drafting convention: "eerste verdieping" names the first floor
# on every Dutch sheet. Text is accent-folded before matching (see _norm).
_PLAN_WORDS = r"\b(?:PLANS?|PLATTEGROND|GRUNDRISS|PLANTA|PIANTA|PLAAN|PLANTE)\b"
_FOREIGN_STOREY = (r"(?:VERDIEPING|ETAGE|OBERGESCHOSS|STOCKWERK|STOCK|PLANTA|PISO|"
                   r"PIANO|KORRUSE|KORRUS|ANDAR|PAVIMENTO)")
_FOREIGN_ORDINALS = {
    "EERSTE": 1, "TWEEDE": 2, "DERDE": 3, "VIERDE": 4, "VIJFDE": 5, "ZESDE": 6,
    "ERSTES": 1, "ZWEITES": 2, "DRITTES": 3, "VIERTES": 4, "FUNFTES": 5,
    "PREMIER": 1, "PREMIERE": 1, "DEUXIEME": 2, "TROISIEME": 3, "QUATRIEME": 4,
    "CINQUIEME": 5,
    "PRIMERA": 1, "PRIMER": 1, "PRIMEIRO": 1, "SEGUNDA": 2, "SEGUNDO": 2,
    "TERCERA": 3, "TERCER": 3, "TERCEIRO": 3, "CUARTA": 4, "QUINTA": 5,
    "PRIMO": 1, "SECONDO": 2, "TERZO": 3, "QUARTO": 4, "QUINTO": 5,
}
_FOREIGN_GROUND = re.compile(
    r"\b(BEGANE GROND|GELIJKVLOERS|ERDGESCHOSS|REZ DE CHAUSSEE|PLANTA BAJA|"
    r"PIANO TERRA|PIANTERRENO|RES DO CHAO|PAVIMENTO TERREO|PARTERRE)\b")
_FOREIGN_BASEMENT = re.compile(
    r"\b(KELDER|KELLER|KELLERGESCHOSS|UNTERGESCHOSS|SOUS SOL|SOTANO|"
    r"SEMINTERRATO|INTERRATO|CAVE|KELDRIKORRUS|KELDER VERDIEPING)\b")
_FOREIGN_ROOF = re.compile(
    r"\b(DAK|DAKAANZICHT|DAKVLOER|DACHAUFSICHT|TOITURE|TOITURE TERRASSE|CUBIERTA|"
    r"AZOTEA|COPERTURA|KATUS)\b")
_FOREIGN_ATTIC = re.compile(r"\b(ZOLDER|DACHGESCHOSS|COMBLES|BUHARDILLA|SOTTOTETTO)\b")


def _parse_foreign(s: str, has_plan: bool) -> Optional[Tuple[float, Optional[str], str]]:
    """``(key, name, kind)`` for a non-English storey title, or ``None``."""
    if _FOREIGN_BASEMENT.search(s):
        return -1.0, None, "level"
    if _FOREIGN_GROUND.search(s) or (has_plan and re.search(r"\b(EG|RDC|PB)\b", s)):
        return 0.0, None, "level"
    m = re.search(r"\b(\d{1,2}) ?(?:E|ER|RE|EME|ME|O|A|ST|TE)? ?(?:%s|OG)\b" % _FOREIGN_STOREY, s)
    if m:
        return float(int(m.group(1))), None, "level"
    m = re.search(r"\b(%s) %s\b" % ("|".join(_FOREIGN_ORDINALS), _FOREIGN_STOREY), s) or \
        re.search(r"\b%s (%s)\b" % (_FOREIGN_STOREY, "|".join(_FOREIGN_ORDINALS)), s)
    if m:
        return float(_FOREIGN_ORDINALS[m.group(1)]), None, "level"
    m = re.search(r"\b%s (\d{1,2})\b" % _FOREIGN_STOREY, s)
    if m and (has_plan or len(s.split()) <= 3):
        return float(int(m.group(1))), None, "level"
    if has_plan:
        m = re.search(r"\bR ?\+ ?(\d{1,2})\b", s)       # French "R+2"
        if m:
            return float(int(m.group(1))), None, "level"
        if re.search(r"\b(UG|KG)\b", s):
            return -1.0, None, "level"
        if re.search(r"\bOG\b", s):
            return 1.0, None, "level"
    if _FOREIGN_ATTIC.search(s) or (has_plan and re.search(r"\bDG\b", s)):
        return 98.0, "Attic", "level"
    if _FOREIGN_ROOF.search(s) and (has_plan or len(s.split()) <= 3):
        return 99.0, "Roof", "roof"
    return None


@dataclass
class LevelTag:
    """What a piece of text or a layer name says about a storey."""

    key: float                 # ordering: basement < ground < first < ... < roof
    name: str                  # canonical, e.g. "First floor"
    kind: str = "level"        # level | roof | typical | site | elevation | section
    text: str = ""
    source: str = "title"      # title | layer

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "kind": self.kind,
                "text": self.text, "source": self.source}


def _norm(text: str) -> str:
    import unicodedata
    from .topology import clean_label
    s = clean_label(text or "")
    # Accents folded, so "Étage" and "Sótano" match like their plain spellings.
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").upper()
    s = re.sub(r"(?<=\b[A-Z])\.(?=[A-Z]\b)", "", s)     # G.F. -> GF
    s = s.replace(".", " ")
    s = re.sub(r"[^A-Z0-9+]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _canonical(key: float) -> str:
    if key >= 99:
        return "Roof"
    if key >= 98:
        return "Attic"
    if key < 0:
        n = int(round(-key))
        return "Basement" if n <= 1 else "Basement %d" % n
    if key == 0:
        return "Ground floor"
    if key != int(key):
        return "Mezzanine"
    names = {v: k.title() for k, v in _ORDINALS.items()}
    return "%s floor" % names.get(int(key), "Level %d" % int(key))


def parse_level(text: str) -> Optional[LevelTag]:
    """Read a storey designation out of a plan title, or ``None``.

    Only title-shaped text qualifies: short, not a construction note, and
    either naming a plan or naming a storey outright.
    """
    s = _norm(text)
    if not s:
        return None
    s = re.sub(r"\bSCALE\b.*$", "", s).strip()
    words = s.split()
    if not words or len(words) > 8:
        return None
    if _NOT_A_TITLE.search(s):
        return None
    has_plan = bool(re.search(_PLAN_WORDS, s))

    def tag(key: float, name: Optional[str] = None, kind: str = "level") -> LevelTag:
        return LevelTag(key=key, name=name or _canonical(key), kind=kind,
                        text=text.strip())

    if re.search(r"\bELEVATIONS?\b", s) and not re.search(_STOREY, s):
        return tag(0, "Elevation", "elevation")
    if re.search(r"\bSECTION(AL)?\b", s):
        return tag(0, "Section", "section")

    m = re.search(r"\bBASEMENT(?:\s+(\d+))?\b", s)
    if m:
        return tag(-float(m.group(1) or 1))
    if re.search(r"\bCELLAR\b", s):
        return tag(-1.0)
    if re.search(r"\bLOWER GROUND\b", s):
        return tag(-0.5, "Lower ground floor")
    if re.search(r"\bUPPER GROUND\b", s):
        return tag(0.5, "Upper ground floor")
    if re.search(r"\bSTILT\b|\bPARKING %s\b|\bPODIUM\b" % _STOREY, s):
        return tag(0.0, "Stilt floor")
    if re.search(r"\bGROUND %s\b" % _STOREY, s) or \
            (has_plan and re.search(r"\bGF\b|\bGROUND\b", s)):
        return tag(0.0)
    if re.search(r"\bMEZZ(ANINE)?\b", s):
        return tag(0.5)
    if re.search(r"\bTYPICAL %s\b" % _STOREY, s):
        return tag(1.5, "Typical floor", "typical")
    if re.search(r"\b(ROOF|TERRACE|HEADROOM)\b", s) and \
            (has_plan or re.search(_STOREY, s)):
        return tag(99.0, "Roof", "roof")
    m = re.search(r"\b(%s) %s\b" % ("|".join(_ORDINALS), _STOREY), s)
    if m:
        return tag(float(_ORDINALS[m.group(1)]))
    m = re.search(r"\b(\d{1,2}) ?(?:ST|ND|RD|TH) %s\b" % _STOREY, s)
    if m:
        return tag(float(int(m.group(1))))
    m = re.search(r"\b%s (\d{1,2})\b" % _STOREY, s)
    if m and (has_plan or len(words) <= 3):
        return tag(float(int(m.group(1))))
    if has_plan:
        m = re.search(r"\bL ?(\d{1,2})\b|\b(\d{1,2})F\b", s)
        if m:
            return tag(float(int(m.group(1) or m.group(2))))
        m = re.search(r"\bB(\d)F?\b", s)
        if m:
            return tag(-float(int(m.group(1))))
        for token, key in (("FF", 1.0), ("SF", 2.0), ("TF", 3.0)):
            if re.search(r"\b%s\b" % token, s):
                return tag(key)
        if re.search(r"\bUPPER %s\b" % _STOREY, s):
            return tag(1.0, "Upper floor")
        if re.search(r"\bLOWER %s\b" % _STOREY, s):
            return tag(0.0, "Lower floor")
        if re.search(r"\b(SITE|LOCATION|KEY|LAYOUT)\b", s):
            return tag(0.0, "Site plan", "site")
    foreign = _parse_foreign(s, has_plan)
    if foreign is not None:
        key, name, kind = foreign
        return tag(key, name, kind)
    return None


def parse_building(text: str) -> Optional[str]:
    """A building designation such as ``BLOCK A`` or ``TOWER 2``, or ``None``.

    Land-record references (``BLOCK NO. 10, T.S. NO. 78``) share the vocabulary
    and are excluded: they name a parcel, not a structure.
    """
    s = _norm(text)
    if not s or _SURVEY.search(s) or len(s.split()) > 8:
        return None
    m = re.search(r"\b(BLOCK|BLDG|BUILDING|TOWER|WING)\s+(?:NO\s+)?([A-Z]|\d{1,3})\b", s)
    if not m:
        return None
    word = {"BLDG": "BUILDING"}.get(m.group(1), m.group(1))
    return "%s %s" % (word, m.group(2))


#: Level tokens in layer names: ``GF-WALL``, ``L02_A-WALL``, ``1F-WALLS``.
_LAYER_TOKENS = (
    (re.compile(r"^(GF|GRD|GROUND)$"), 0.0),
    (re.compile(r"^(FF|FIRST|1ST)$"), 1.0),
    (re.compile(r"^(SF|SECOND|2ND)$"), 2.0),
    (re.compile(r"^(TF|THIRD|3RD)$"), 3.0),
    (re.compile(r"^(BSMT|BASEMENT|B1|B1F)$"), -1.0),
)


def layer_level(layer: str) -> Optional[float]:
    from .classify import normalise_layer
    tokens = [t for t in re.split(r"[-_ .|/$]+", normalise_layer(layer).upper()) if t]
    for tok in tokens:
        for pattern, key in _LAYER_TOKENS:
            if pattern.match(tok):
                return key
        m = re.match(r"^(?:L|LVL|LEVEL|FL|FLR)(\d{1,2})$", tok) or \
            re.match(r"^(\d{1,2})F$", tok)
        if m:
            return float(int(m.group(1)))
    return None


# ---------------------------------------------------------------------------
# Titles on the sheet
# ---------------------------------------------------------------------------

@dataclass
class Title:
    label_id: str
    text: str
    extent: Tuple[float, float, float, float]
    height: float
    point: XY
    level: Optional[LevelTag] = None
    building: Optional[str] = None
    structure_id: Optional[str] = None
    rejected: str = ""

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "point": [round(self.point[0], 3), round(self.point[1], 3)],
            "level": self.level.as_dict() if self.level else None,
            "building": self.building,
            "structure": self.structure_id,
            "rejected": self.rejected or None,
        }


def collect_titles(labels: Sequence) -> List[Title]:
    """Every label that reads as a plan or building title, schedules removed."""
    out: List[Title] = []
    for t in labels:
        level = parse_level(t.text)
        building = parse_building(t.text)
        if level is None and building is None:
            continue
        extent = t.extent or (t.point[0], t.point[1],
                              t.point[0] + max(t.height, 0.1),
                              t.point[1] + max(t.height, 0.1))
        out.append(Title(label_id=t.id, text=t.text.strip(), extent=extent,
                         height=max(t.height, 1e-6), point=t.point,
                         level=level, building=building))
    _reject_schedules(out)
    return out


def _reject_schedules(titles: List[Title]) -> None:
    """Floor names stacked in a column are a table, not plan titles.

    An area statement lists every storey one under another at one insertion
    x; a plan title stands alone under its plan. Two or more level
    designations in such a column are all rejected.
    """
    cands = [t for t in titles if t.level is not None]
    cands.sort(key=lambda t: (round(t.point[0], 1), -t.point[1]))
    used = set()
    for i, a in enumerate(cands):
        column = [a]
        for b in cands:
            if b is a:
                continue
            h = max(a.height, b.height)
            if abs(b.point[0] - a.point[0]) <= 2.0 * h and \
                    abs(b.point[1] - a.point[1]) <= 3.0 * h * max(len(column), 1):
                column.append(b)
        if len(column) >= 2:
            for t in column:
                if id(t) not in used:
                    used.add(id(t))
                    t.rejected = "part of a floor schedule, not a plan title"


def _inside_room(point: XY, structures: Sequence[Structure]) -> bool:
    from shapely.geometry import Point, Polygon
    p = Point(*point)
    for s in structures:
        for r in s.rooms:
            try:
                if Polygon(r.polygon).contains(p):
                    return True
            except Exception:
                continue
    return False


def associate(titles: Sequence[Title], structures: Sequence[Structure]) -> None:
    """Attach each title to the one plan it is placed under, above or beside.

    Alignment is required, not mere proximity: a title shares the plan's
    column (placed above or below it) or its row (placed beside it). Two plans
    side by side are each nearest to the other's title often enough that
    distance alone mislabels one of them.
    """
    for t in titles:
        if t.rejected:
            continue
        tx0, ty0, tx1, ty1 = t.extent
        cands: List[Tuple[float, Structure]] = []
        for s in structures:
            sx0, sy0, sx1, sy1 = s.bounds
            ox = min(tx1, sx1) - max(tx0, sx0)
            oy = min(ty1, sy1) - max(ty0, sy0)
            if ox > 0 and oy > 0:
                gap = 0.0
            elif ox > 0:
                gap = max(sy0 - ty1, ty0 - sy1)
            elif oy > 0:
                gap = max(sx0 - tx1, tx0 - sx1)
            else:
                continue
            if gap <= max(TITLE_REACH, TITLE_REACH_RATIO * s.size):
                cands.append((gap, s))
        if not cands:
            t.rejected = "not aligned with any reconstructed plan"
            continue
        cands.sort(key=lambda c: (c[0], c[1].id))
        best_gap, best = cands[0]
        if best_gap == 0.0 and _inside_room(t.point, [best]):
            t.rejected = "inside a room, so it names the room rather than the plan"
            continue
        if len(cands) > 1 and cands[1][0] <= best_gap * TITLE_AMBIGUITY_RATIO + 0.5:
            t.rejected = ("equally close to %s and %s"
                          % (best.id, cands[1][1].id))
            continue
        t.structure_id = best.id


# ---------------------------------------------------------------------------
# Footprint congruence and registration
# ---------------------------------------------------------------------------

def _raster(geom, origin: XY, res: float, shape: Tuple[int, int]):
    import numpy as np
    import shapely
    ny, nx = shape
    xs = origin[0] + (np.arange(nx) + 0.5) * res
    ys = origin[1] + (np.arange(ny) + 0.5) * res
    X, Y = np.meshgrid(xs, ys)
    shapely.prepare(geom)
    return shapely.contains_xy(geom, X, Y).astype(np.float64)


def _grid(geoms, res: float, pad: float) -> Tuple[XY, Tuple[int, int]]:
    x0 = min(g.bounds[0] for g in geoms) - pad
    y0 = min(g.bounds[1] for g in geoms) - pad
    x1 = max(g.bounds[2] for g in geoms) + pad
    y1 = max(g.bounds[3] for g in geoms) + pad
    return (x0, y0), (int(math.ceil((y1 - y0) / res)) + 1,
                      int(math.ceil((x1 - x0) / res)) + 1)


def _fast_len(n: int) -> int:
    """The smallest 5-smooth number at least ``n``.

    An FFT of a length with a large prime factor is many times slower than
    one a few cells longer; padding further changes no overlap.
    """
    best = 1 << max(0, (n - 1).bit_length())
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            m = p35
            while m < n:
                m *= 2
            best = min(best, m)
            p35 *= 3
        p5 *= 5
    return best


def _correlate(a, b):
    """Overlap of ``a`` with ``b`` shifted by every integer cell offset."""
    import numpy as np
    H = _fast_len(a.shape[0] + b.shape[0])
    W = _fast_len(a.shape[1] + b.shape[1])
    fa = np.fft.fft2(a, s=(H, W))
    fb = np.fft.fft2(b, s=(H, W))
    return np.real(np.fft.ifft2(fa * np.conj(fb)))


def _peak_shift(corr, a_shape, b_shape) -> Tuple[int, int, float]:
    import numpy as np
    H, W = corr.shape
    iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
    dy = iy if iy < a_shape[0] else iy - H
    dx = ix if ix < a_shape[1] else ix - W
    return int(dx), int(dy), float(corr[iy, ix])


def _resolution(*geoms) -> float:
    size = max(max(g.bounds[2] - g.bounds[0], g.bounds[3] - g.bounds[1])
               for g in geoms)
    return max(0.1, size / MAX_CELLS)


def congruence(a: Structure, b: Structure) -> Tuple[float, XY]:
    """Best-aligned footprint IoU of two structures, and the aligning shift.

    The shift is the translation that carries ``b``'s drawn position onto
    ``a``'s.
    """
    fa, fb = a.region, b.region
    if fa is None or fb is None or fa.is_empty or fb.is_empty:
        return 0.0, (0.0, 0.0)
    res = _resolution(fa, fb)
    oa, sa = _grid([fa], res, res)
    ob, sb = _grid([fb], res, res)
    A = _raster(fa, oa, res, sa)
    B = _raster(fb, ob, res, sb)
    dx, dy, peak = _peak_shift(_correlate(A, B), A.shape, B.shape)
    union = A.sum() + B.sum() - peak
    iou = peak / union if union > 0 else 0.0
    shift = (oa[0] - ob[0] + dx * res, oa[1] - ob[1] + dy * res)
    return float(iou), shift


def register(base: Structure, other: Structure) -> Tuple[XY, float]:
    """The translation that puts ``other`` over ``base``, and its score.

    Storeys share their structure — exterior walls, the stair and lift core,
    the party walls — so the walls are correlated, with the filled footprint
    added so a plan made of many identical units locks onto the whole plate
    rather than onto one unit's repeat. The integer-cell answer is then
    refined against the wall centrelines themselves, so the stacking error is
    millimetres rather than a raster cell.
    """
    import numpy as np
    from .topology import wall_solids
    wa = wall_solids(base.walls)
    wb = wall_solids(other.walls)
    if wa is None or wb is None:
        return (0.0, 0.0), 0.0
    res = _resolution(base.region, other.region)
    grow = res * 0.75
    wa_g, wb_g = wa.buffer(grow), wb.buffer(grow)
    oa, sa = _grid([base.region, wa_g], res, res)
    ob, sb = _grid([other.region, wb_g], res, res)
    WA, WB = _raster(wa_g, oa, res, sa), _raster(wb_g, ob, res, sb)
    FA, FB = _raster(base.region, oa, res, sa), _raster(other.region, ob, res, sb)
    walls_corr = _correlate(WA, WB)
    corr = (walls_corr / max(min(WA.sum(), WB.sum()), 1.0) +
            _correlate(FA, FB) / max(min(FA.sum(), FB.sum()), 1.0))
    dx, dy, _ = _peak_shift(corr, WA.shape, WB.shape)
    shift = (oa[0] - ob[0] + dx * res, oa[1] - ob[1] + dy * res)
    H, W = corr.shape
    wall_overlap = walls_corr[dy % H, dx % W]
    score = float(wall_overlap / max(min(WA.sum(), WB.sum()), 1.0))
    shift = _refine(base.walls, other.walls, shift, tol=max(res * 2.0, 0.2))
    return shift, score


def _refine(base_walls, other_walls, shift: XY, tol: float) -> XY:
    """Least-squares correction aligning parallel wall centrelines."""
    rows: List[Tuple[float, float, float]] = []
    for w in other_walls:
        if w.length < 1.0:
            continue
        s = (w.start[0] + shift[0], w.start[1] + shift[1])
        e = (w.end[0] + shift[0], w.end[1] + shift[1])
        d = w.direction
        n = (-d[1], d[0])
        mid = ((s[0] + e[0]) / 2.0, (s[1] + e[1]) / 2.0)
        best = None
        for b in base_walls:
            if b.length < 1.0:
                continue
            if abs(((w.angle_deg - b.angle_deg + 90) % 180) - 90) > 2.0:
                continue
            bd = b.direction
            t = (mid[0] - b.start[0]) * bd[0] + (mid[1] - b.start[1]) * bd[1]
            if t < -0.2 or t > b.length + 0.2:
                continue
            delta = (b.start[0] - mid[0]) * n[0] + (b.start[1] - mid[1]) * n[1]
            if abs(delta) <= tol and (best is None or abs(delta) < abs(best)):
                best = delta
        if best is not None:
            weight = min(w.length, 10.0)
            rows.append((n[0] * weight, n[1] * weight, best * weight))
    if len(rows) < 2:
        return shift
    sxx = sum(r[0] * r[0] for r in rows)
    sxy = sum(r[0] * r[1] for r in rows)
    syy = sum(r[1] * r[1] for r in rows)
    bx = sum(r[0] * r[2] for r in rows)
    by = sum(r[1] * r[2] for r in rows)
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-9:
        return shift
    cx = (bx * syy - by * sxy) / det
    cy = (by * sxx - bx * sxy) / det
    if math.hypot(cx, cy) > tol * 1.5:
        return shift
    return (shift[0] + cx, shift[1] + cy)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

@dataclass
class LevelSpec:
    structure: Structure
    tag: Optional[LevelTag]
    designation: str = "assumed"      # title | layer | assumed
    title: Optional[str] = None
    placement: XY = (0.0, 0.0)
    elevation: float = 0.0
    #: How ``elevation`` was arrived at: ``"datum"`` for the ground storey,
    #: which defines zero, or ``"estimated"`` when it was derived from an
    #: assumed storey height because the drawing states no level heights.
    #: There is deliberately no ``"measured"`` yet - nothing reads elevation
    #: annotations, and claiming otherwise would be the silent-estimate bug.
    elevation_source: str = "datum"
    index: int = 0
    name: str = "Level 0"
    confidence: float = 1.0
    evidence: List[str] = field(default_factory=list)


@dataclass
class BuildingSpec:
    levels: List[LevelSpec]
    designation: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    confidence: float = 1.0
    ambiguous_with: List[str] = field(default_factory=list)


@dataclass
class LevelPlan:
    buildings: List[BuildingSpec]
    status: str
    reason: str
    review: List[Dict[str, object]] = field(default_factory=list)
    dropped: List[Tuple[Structure, str]] = field(default_factory=list)
    detail: Dict[str, object] = field(default_factory=dict)


def _layer_tag(s: Structure) -> Optional[LevelTag]:
    total = sum(w.length for w in s.walls)
    if total <= 0:
        return None
    by_key: Dict[float, float] = {}
    for w in s.walls:
        k = layer_level(w.layer)
        if k is not None:
            by_key[k] = by_key.get(k, 0.0) + w.length
    if not by_key:
        return None
    key = max(sorted(by_key), key=lambda k: by_key[k])
    if by_key[key] / total < 0.8 or len(by_key) > 1 and \
            sorted(by_key.values())[-2] / total > 0.1:
        return None
    return LevelTag(key=key, name=_canonical(key), source="layer",
                    text="wall layers")


def infer(structures: Sequence[Structure], labels: Sequence, *,
          wall_height: float) -> LevelPlan:
    """Group structures into buildings and storeys, or say it cannot."""
    from .ir import LEVELS_AMBIGUOUS, LEVELS_RESOLVED, LEVELS_SINGLE

    titles = collect_titles(labels)
    associate(titles, structures)
    review: List[Dict[str, object]] = []
    dropped: List[Tuple[Structure, str]] = []

    level_of: Dict[str, LevelSpec] = {}
    building_of: Dict[str, Optional[str]] = {}
    for s in structures:
        mine = [t for t in titles if t.structure_id == s.id]
        levels = [t for t in mine if t.level is not None]
        blds = sorted({t.building for t in mine if t.building})
        building_of[s.id] = blds[0] if len(blds) == 1 else None
        if len(blds) > 1:
            review.append({"code": "CONFLICTING_BUILDING_TITLES",
                           "message": "%s is titled as more than one building (%s)"
                                      % (s.id, ", ".join(blds)),
                           "structures": [s.id]})
        keys = sorted({(t.level.kind, t.level.key) for t in levels})
        spec = LevelSpec(structure=s, tag=None)
        if len(keys) == 1:
            t = levels[0]
            spec.tag, spec.designation, spec.title = t.level, "title", t.text
            spec.evidence.append("titled %r" % t.text)
        elif len(keys) > 1:
            review.append({"code": "CONFLICTING_LEVEL_TITLES",
                           "message": "%s carries titles naming different storeys (%s)"
                                      % (s.id, "; ".join(t.text for t in levels)),
                           "structures": [s.id]})
            spec.evidence.append("conflicting level titles ignored")
        if spec.tag is None:
            lt = _layer_tag(s)
            if lt is not None:
                spec.tag, spec.designation = lt, "layer"
                spec.evidence.append("wall layers carry the %s designation" % lt.name)
        level_of[s.id] = spec

    # Views that are not plans contribute no building.
    plans: List[Structure] = []
    for s in structures:
        tag = level_of[s.id].tag
        if tag is not None and tag.kind in ("elevation", "section"):
            dropped.append((s, "titled as an %s, not a plan" % tag.kind))
            continue
        if tag is not None and tag.kind == "site":
            level_of[s.id].tag = None
            level_of[s.id].evidence.append("drawn on the site plan")
        plans.append(s)

    by_id = {s.id: s for s in plans}
    congruent: List[Dict[str, object]] = []

    def congruent_pairs(ids: Sequence[str]) -> Dict[Tuple[str, str], float]:
        out: Dict[Tuple[str, str], float] = {}
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                sa, sb = by_id[a], by_id[b]
                small, big = sorted((sa.region.area, sb.region.area))
                if big <= 0 or small / big < CONGRUENT_IOU:
                    continue        # areas alone rule congruence out
                iou, _ = congruence(sa, sb)
                if iou >= CONGRUENT_IOU:
                    out[(a, b)] = iou
        return out

    # -- group into buildings ------------------------------------------------
    groups: Dict[Optional[str], List[str]] = {}
    for s in plans:
        groups.setdefault(building_of[s.id], []).append(s.id)

    buildings: List[BuildingSpec] = []
    ambiguous = False
    for designation in sorted(groups, key=lambda d: (d is None, d or "")):
        ids = groups[designation]
        titled = [i for i in ids if level_of[i].tag is not None]
        untitled = [i for i in ids if level_of[i].tag is None]

        stack: List[str] = []
        keys = [level_of[i].tag.key for i in titled]
        if len(titled) >= 2 and len(set(keys)) == len(keys):
            stack = sorted(titled, key=lambda i: level_of[i].tag.key)
        elif len(titled) >= 2:
            review.append({
                "code": LEVELS_AMBIGUOUS,
                "message": "plans %s are titled with the same storey more than "
                           "once; they are kept as separate buildings"
                           % ", ".join(titled),
                "structures": list(titled)})
            ambiguous = True
            untitled = untitled + titled
            titled = []
        elif len(titled) == 1:
            stack = titled

        singles = list(untitled)
        if designation is not None and not stack and len(singles) > 1:
            review.append({
                "code": LEVELS_AMBIGUOUS,
                "message": "%s has %d plans and nothing saying which storey "
                           "each is" % (designation, len(singles)),
                "structures": list(singles)})
            ambiguous = True

        if stack:
            b = BuildingSpec(levels=[level_of[i] for i in stack],
                             designation=designation)
            if len(stack) > 1:
                b.evidence.append("%d plans titled as distinct storeys: %s" % (
                    len(stack), ", ".join(repr(level_of[i].title or level_of[i].tag.name)
                                          for i in stack)))
            buildings.append(b)

        # Untitled plans congruent with anything else in the group cannot be
        # placed: they are either storeys of it or its twin.
        pool = stack + singles
        pairs = congruent_pairs(pool) if len(pool) > 1 else {}
        flagged = set()
        for (a, b_id), iou in sorted(pairs.items()):
            if a in singles or b_id in singles:
                flagged.update(x for x in (a, b_id) if x in singles)
                congruent.append({"structures": [a, b_id], "iou": round(iou, 3)})
        if flagged:
            ambiguous = True
            members = sorted({x for pair in congruent for x in pair["structures"]
                              if x in pool})
            review.append({
                "code": LEVELS_AMBIGUOUS,
                "message": "plans %s repeat the same floor plate (footprint IoU "
                           "%s) with no title or layer saying whether they are "
                           "storeys of one building or separate buildings; they "
                           "are kept apart as drawn"
                           % (", ".join(members),
                              ", ".join("%.2f" % c["iou"] for c in congruent
                                        if set(c["structures"]) <= set(members))),
                "structures": members})

        for i in singles:
            spec = level_of[i]
            b = BuildingSpec(levels=[spec], designation=designation)
            if i in flagged:
                b.ambiguous_with = sorted(
                    {x for c in congruent for x in c["structures"]
                     if i in c["structures"] and x != i})
                b.confidence = 0.5
                b.evidence.append("repeated floor plate; storey relationship "
                                  "undetermined")
            buildings.append(b)

    # -- storeys: order, elevation, registration ------------------------------
    for b in buildings:
        _place_levels(b, wall_height, review)
        b.evidence.insert(0, "wall network %s" % ", ".join(
            l.structure.id for l in b.levels))
        if b.designation:
            b.evidence.append("designated %r in the drawing" % b.designation)

    storeyed = [l for b in buildings for l in b.levels if l.tag is not None]
    bare = [l for b in buildings for l in b.levels
            if l.tag is None and not b.ambiguous_with]
    if storeyed and bare:
        review.append({
            "code": "UNDESIGNATED_PLANS",
            "message": "%s carry no storey designation while other plans do; "
                       "they are treated as separate single-storey buildings"
                       % ", ".join(l.structure.id for l in bare),
            "structures": [l.structure.id for l in bare]})

    # Order: largest building first, stable by structure id.
    buildings.sort(key=lambda b: (-sum(l.structure.floor_area for l in b.levels),
                                  b.levels[0].structure.id))

    named_levels = [t for t in titles if t.level is not None and not t.rejected
                    and t.level.kind in ("level", "roof", "typical")]
    if len(plans) == 1 and len({t.level.key for t in named_levels}) > 1:
        review.append({
            "code": "LEVEL_TITLES_WITHOUT_PLANS",
            "message": "the drawing titles %d storeys but only one plan was "
                       "reconstructed" % len({t.level.key for t in named_levels})})

    multi = any(len(b.levels) > 1 for b in buildings)
    if ambiguous:
        status = LEVELS_AMBIGUOUS
        reason = "the drawing does not establish how its plans relate as storeys"
    elif multi:
        status = LEVELS_RESOLVED
        reason = "storeys identified from %s" % ", ".join(sorted({
            l.designation for b in buildings for l in b.levels if len(b.levels) > 1}))
    else:
        status = LEVELS_SINGLE
        reason = ("one plan" if len(plans) == 1 else
                  "%d separate single-storey structures" % len(plans))

    return LevelPlan(
        buildings=buildings, status=status, reason=reason, review=review,
        dropped=dropped,
        detail={
            "titles": [t.as_dict() for t in titles],
            "congruent": congruent,
            "structures": [{
                "id": s.id,
                "bounds": [round(v, 3) for v in s.bounds],
                "enclosed_m2": round(s.enclosed_area, 2),
                "walls": len(s.walls),
                "rooms": len(s.rooms),
                "level": level_of[s.id].tag.as_dict() if level_of[s.id].tag else None,
                "building": building_of[s.id],
            } for s in structures],
        })


def _place_levels(b: BuildingSpec, wall_height: float,
                  review: List[Dict[str, object]]) -> None:
    """Order a building's storeys, give each an elevation and a placement."""
    levels = b.levels
    if len(levels) == 1:
        spec = levels[0]
        spec.index, spec.elevation, spec.placement = 0, 0.0, (0.0, 0.0)
        spec.name = spec.tag.name if spec.tag else "Level 0"
        if spec.tag is not None and spec.tag.key < 0:
            spec.index = int(round(spec.tag.key))
        spec.evidence.append("single storey of this building")
        return

    levels.sort(key=lambda l: l.tag.key)
    ground = next((n for n, l in enumerate(levels) if l.tag.key >= 0), 0)
    base = levels[ground]
    for n, spec in enumerate(levels):
        spec.index = n - ground
        spec.elevation = round((n - ground) * wall_height, 4)
        spec.name = spec.tag.name
        if spec.index != 0:
            # No drawing in the corpus states its level heights, so this is a
            # stack of equal storeys, not a measurement. Say which it is: a
            # user told "First floor at 3.00 m" is entitled to assume the
            # drawing said so.
            spec.elevation_source = "estimated"
            spec.evidence.append(
                "elevation %.3f m estimated as %d x the %.3f m storey height; "
                "the drawing states no level heights"
                % (spec.elevation, spec.index, wall_height))
        if spec is base:
            spec.placement = (0.0, 0.0)
            spec.evidence.append("reference storey for placement")
            continue
        shift, score = register(base.structure, spec.structure)
        spec.placement = (round(shift[0], 4), round(shift[1], 4))
        spec.confidence = round(max(0.0, min(1.0, score)), 3)
        spec.evidence.append("registered over %s by wall correlation "
                             "(score %.2f)" % (base.name, score))
        if score < MIN_REGISTRATION:
            review.append({
                "code": "LOW_LEVEL_REGISTRATION",
                "message": "%s could not be reliably placed over %s (score %.2f); "
                           "its position in 3D may be wrong" % (spec.name, base.name, score),
                "structures": [spec.structure.id, base.structure.id]})
