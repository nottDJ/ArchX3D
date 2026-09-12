"""
ArchX3D — Entity classification: building geometry vs documentation
===================================================================
Decides what every DXF entity *is* before any of it is treated as a wall.

The problem
-----------
An architectural drawing is mostly not a building. It is a building plus the
apparatus of describing one: dimension strings, extension lines, arrowheads,
leaders, section marks, north arrows, revision clouds, hatch patterns,
furniture symbols, plumbing fixtures, title blocks and notes. On the plan that
motivated this rewrite, only 197 of 1954 drawable segments are wall geometry —
about 10%. Treat the other 90% as walls and you get a building-shaped object
with a 58-foot "wall" running through the middle of it where the overall
dimension string was.

The old engine had a layer *blacklist*, then replaced it with a role lookup
that fell through to "unknown", and then let unknown geometry become walls.
That is the wrong default in both directions.

The rule here
-------------
Classification is a chain of evidence, strongest first, and **UNKNOWN is never
promoted to WALL**. A segment becomes a wall because something positively
identifies it as one, not because nothing ruled it out.

1. Entity type. A DIMENSION is documentation. Always. No layer name, no
   geometry, no context can make it a wall. Same for LEADER, TOLERANCE,
   TEXT/MTEXT, and anything on AutoCAD's non-plotting ``Defpoints`` layer.
2. Layer role. Layer names carry the drafter's own classification, and it is
   usually right. This must survive xref binding, which mangles ``A-WALL``
   into ``xref-Bishop-Overland-08$0$A-WALL``, and must recognise the AIA
   convention (``A-WALL``, ``A-GLAZ``, ``S-STEM-WALL``), the ISO 13567
   convention, and the loose vernacular (``WALLS``, ``MUR``, ``MURO``).
3. Block semantics. Geometry inside a block named ``P-Toilet`` or
   ``FIXT-SNGREF30`` is a fixture wherever it sits.
4. Geometry, only as a fallback for drawings with no usable layers: paired
   parallel lines of a consistent, conventional separation are walls.

Structural layers deserve a note. ``S-FOOTER``, ``S-STEM-WALL`` and
``S-SLAB`` are *foundation* geometry that sits under the building, drawn on
the same sheet. They are real construction but they are not the walls of the
storey being modelled, and extruding them produces a second, slightly larger
ghost building interleaved with the first. They are classified
``STRUCTURE_BELOW`` and excluded from wall reconstruction while remaining
available as envelope evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

WALL = "wall"
DOOR = "door"
WINDOW = "window"
OPENING = "opening"
ROOM_LABEL = "room_label"
DIMENSION = "dimension"
ANNOTATION = "annotation"
FURNITURE = "furniture"
FIXTURE = "fixture"
CASEWORK = "casework"
STAIR = "stair"
STRUCTURE_BELOW = "structure_below"
STRUCTURE_ABOVE = "structure_above"
FOOTPRINT = "footprint"
HATCH = "hatch"
GRID = "grid"
TITLE_BLOCK = "title_block"
CONSTRUCTION = "construction"
LANDSCAPE = "landscape"
ELECTRICAL = "electrical"
UNKNOWN = "unknown"

#: Roles whose geometry may be considered for wall reconstruction. Note the
#: absence of UNKNOWN — that omission is the point of this module.
WALL_ROLES = frozenset({WALL})

#: Roles that describe the building envelope without being walls themselves.
ENVELOPE_ROLES = frozenset({FOOTPRINT, STRUCTURE_BELOW})

#: Roles that can never contribute geometry to the building, whatever else
#: happens. Kept explicit so the exclusion is auditable.
NEVER_BUILDING = frozenset({
    DIMENSION, ANNOTATION, ROOM_LABEL, TITLE_BLOCK, GRID, CONSTRUCTION,
    ELECTRICAL, LANDSCAPE,
})


# ---------------------------------------------------------------------------
# Layer name normalisation
# ---------------------------------------------------------------------------

_XREF_SPLIT = re.compile(r"^.*?\$\d+\$")
_SHEET_PREFIX = re.compile(r"^(xref[-_]?|x[-_])", re.IGNORECASE)


def normalise_layer(name: str) -> str:
    """Reduce a layer name to the part that carries meaning.

    Binding an xref rewrites ``A-WALL`` to ``xref-Bishop-Overland-08$0$A-WALL``.
    Nested binding can do it twice. The old classifier saw those names as
    unknown, which is how a drawing with a perfectly well-named wall layer ended
    up with ten "unknown" layer roles and two recognised ones.

    >>> normalise_layer('xref-Bishop-Overland-08$0$A-WALL')
    'A-WALL'
    >>> normalise_layer('INVA-WALL')
    'INVA-WALL'
    """
    if not name:
        return ""
    cleaned = name.strip()
    # Strip every xref-binding prefix, innermost last.
    while "$" in cleaned:
        stripped = _XREF_SPLIT.sub("", cleaned, count=1)
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = _SHEET_PREFIX.sub("", cleaned)
    return cleaned.strip()


def _tokens(name: str) -> List[str]:
    return [t for t in re.split(r"[-_ .|/]+", name.upper()) if t]


# ---------------------------------------------------------------------------
# Layer role table
# ---------------------------------------------------------------------------
# Ordered most specific first. Each entry is (role, matcher tokens). A rule
# matches when every token in the rule appears in the layer name, so
# ``("S", "STEM")`` matches ``S-STEM-WALL`` but not ``A-WALL``.

_RULES: Sequence[Tuple[str, Tuple[str, ...]]] = (
    # --- explicitly non-building, checked before anything containing WALL ---
    (CONSTRUCTION, ("DEFPOINTS",)),
    (TITLE_BLOCK, ("TITLE",)),
    (TITLE_BLOCK, ("BORDER",)),
    (TITLE_BLOCK, ("SHEET",)),
    (TITLE_BLOCK, ("VIEW", "PORT")),
    (TITLE_BLOCK, ("VIEWPORT",)),
    (DIMENSION, ("DIM",)),
    (DIMENSION, ("DIMS",)),
    (DIMENSION, ("MEASURE",)),
    (ANNOTATION, ("ANNO",)),
    (ANNOTATION, ("ANNOT",)),
    (ANNOTATION, ("ANNTEXT",)),
    (ANNOTATION, ("NOTE",)),
    (ANNOTATION, ("NOTES",)),
    (ANNOTATION, ("LEADER",)),
    (ANNOTATION, ("SYMBOL",)),
    (ANNOTATION, ("SYMBOLS",)),
    (ANNOTATION, ("LEGEND",)),
    (ANNOTATION, ("KEY",)),
    (ANNOTATION, ("REVISION",)),
    (ANNOTATION, ("REV",)),
    (ANNOTATION, ("SECTION", "MARK")),
    (ANNOTATION, ("NORTH",)),
    (GRID, ("GRID",)),
    (GRID, ("AXIS",)),
    (GRID, ("CENTERLINE",)),
    (GRID, ("CENTRELINE",)),
    (CONSTRUCTION, ("CONSTRUCTION",)),
    (CONSTRUCTION, ("TEMP",)),
    (CONSTRUCTION, ("SCRATCH",)),
    (CONSTRUCTION, ("HIDDEN",)),
    (CONSTRUCTION, ("NPLT",)),        # AIA: non-plotting
    (CONSTRUCTION, ("SCRN",)),        # AIA: screened reference

    # --- text ---
    (ROOM_LABEL, ("RM", "NAME")),
    (ROOM_LABEL, ("ROOM", "NAME")),
    (ROOM_LABEL, ("ROOM", "TAG")),
    (ANNOTATION, ("TEXT",)),
    (ANNOTATION, ("TXT",)),

    # --- structure below / above the storey ---
    (STRUCTURE_BELOW, ("FOOTER",)),
    (STRUCTURE_BELOW, ("FOOTING",)),
    (STRUCTURE_BELOW, ("FTG",)),
    (STRUCTURE_BELOW, ("STEM",)),
    (STRUCTURE_BELOW, ("SLAB",)),
    (STRUCTURE_BELOW, ("FOUNDATION",)),
    (STRUCTURE_BELOW, ("PIER",)),
    (STRUCTURE_ABOVE, ("TRUSS",)),
    (STRUCTURE_ABOVE, ("JOIST",)),
    (STRUCTURE_ABOVE, ("JOISTS",)),
    (STRUCTURE_ABOVE, ("RAFTER",)),
    (STRUCTURE_ABOVE, ("OVERHANG",)),
    (STRUCTURE_ABOVE, ("OVERBUILD",)),
    (STRUCTURE_ABOVE, ("ROOF",)),
    (STRUCTURE_ABOVE, ("BEAM",)),
    (STRUCTURE_ABOVE, ("HEADER",)),
    (STRUCTURE_ABOVE, ("HEAD",)),        # A-HEAD, the abbreviated form
    (STRUCTURE_ABOVE, ("HDR",)),
    (STRUCTURE_ABOVE, ("LINTEL",)),
    (STRUCTURE_ABOVE, ("LNTL",)),
    (STRUCTURE_ABOVE, ("CEILING",)),

    # --- openings ---
    (DOOR, ("DOOR",)),
    (DOOR, ("DR",)),
    (DOOR, ("PORTE",)),
    (WINDOW, ("GLAZ",)),
    (WINDOW, ("WINDOW",)),
    (WINDOW, ("WIN",)),
    (WINDOW, ("FENSTER",)),
    (OPENING, ("OPENING",)),
    (OPENING, ("OPNG",)),
    (OPENING, ("OPEN",)),

    # --- contents ---
    (FIXTURE, ("SANITARY",)),
    (FIXTURE, ("PLUMB",)),
    (FIXTURE, ("PLUMBING",)),
    (FIXTURE, ("FIXTURE",)),
    (FIXTURE, ("FIXT",)),
    (FIXTURE, ("SANIT",)),
    (CASEWORK, ("CASE",)),
    (CASEWORK, ("CABINET",)),
    (CASEWORK, ("CASEWORK",)),
    (CASEWORK, ("COUNTER",)),
    (CASEWORK, ("MILLWORK",)),
    (FURNITURE, ("FURN",)),
    (FURNITURE, ("FURNITURE",)),
    (FURNITURE, ("EQPM",)),
    (FURNITURE, ("EQUIP",)),
    (STAIR, ("STAIR",)),
    (STAIR, ("STAIRS",)),
    (STAIR, ("STR", "TREAD")),
    (ELECTRICAL, ("ELEC",)),
    (ELECTRICAL, ("POWR",)),
    (ELECTRICAL, ("LITE",)),
    (ELECTRICAL, ("LIGHT",)),
    (LANDSCAPE, ("GREEN",)),
    (LANDSCAPE, ("GREENS",)),
    (LANDSCAPE, ("PLANT",)),
    (LANDSCAPE, ("TREE",)),
    (LANDSCAPE, ("LANDSCAPE",)),
    (HATCH, ("HATCH",)),
    (HATCH, ("PATTERN",)),
    (HATCH, ("POCHE",)),

    # --- envelope ---
    (FOOTPRINT, ("FOOTPRINT",)),
    (FOOTPRINT, ("OUTLINE",)),
    (FOOTPRINT, ("ENVELOPE",)),
    (FOOTPRINT, ("BOUNDARY",)),
    (FOOTPRINT, ("PERIMETER",)),

    # --- walls, last, so the exclusions above win ties ---
    (WALL, ("WALL",)),
    (WALL, ("WALLS",)),
    (WALL, ("WAL",)),
    (WALL, ("MUR",)),
    (WALL, ("MURO",)),
    (WALL, ("PARED",)),
    (WALL, ("PARTITION",)),
    (WALL, ("PARTN",)),
    (WALL, ("MASONRY",)),
    (WALL, ("BRICK",)),
    (WALL, ("BLOCK", "WORK")),
)

#: Layer names that mean "the drafter put it on the default layer", which tells
#: us nothing. Kept separate so they are reported as genuinely unclassified
#: rather than silently lumped in with annotation.
_NEUTRAL = frozenset({"0", "", "DEFAULT", "MISC", "GENERAL"})


def layer_role(layer_name: str) -> Tuple[str, float, str]:
    """Classify a layer by name. Returns ``(role, confidence, reason)``."""
    normalised = normalise_layer(layer_name)
    tokens = set(_tokens(normalised))
    if not tokens or normalised.upper() in _NEUTRAL:
        return UNKNOWN, 0.0, "layer %r carries no classification" % layer_name

    for role, needed in _RULES:
        if all(t in tokens for t in needed):
            return role, 0.9, "layer %r matches %s by %s" % (
                layer_name, role, "+".join(needed))

    # Substring fallback for run-together names like "INTERIORWALLS".
    flat = normalised.upper().replace("-", "").replace("_", "")
    for role, needed in _RULES:
        if len(needed) == 1 and len(needed[0]) >= 4 and needed[0] in flat:
            return role, 0.65, "layer %r contains %r" % (layer_name, needed[0])

    return UNKNOWN, 0.0, "layer %r matches no known convention" % layer_name


# ---------------------------------------------------------------------------
# Block name semantics
# ---------------------------------------------------------------------------

_BLOCK_RULES: Sequence[Tuple[str, Tuple[str, ...]]] = (
    (FIXTURE, ("TOILET",)), (FIXTURE, ("WC",)), (FIXTURE, ("LAV",)),
    (FIXTURE, ("BASIN",)), (FIXTURE, ("SINK",)), (FIXTURE, ("BATH",)),
    (FIXTURE, ("TUB",)), (FIXTURE, ("SHOWER",)), (FIXTURE, ("URINAL",)),
    (FIXTURE, ("BIDET",)),
    (CASEWORK, ("KIT",)), (CASEWORK, ("CKTOP",)), (CASEWORK, ("REF",)),
    (CASEWORK, ("WASHER",)), (CASEWORK, ("DRYER",)), (CASEWORK, ("RANGE",)),
    (CASEWORK, ("OVEN",)), (CASEWORK, ("DISHWASH",)), (CASEWORK, ("CAB",)),
    (DOOR, ("DOOR",)), (DOOR, ("OPEN30",)), (DOOR, ("OPEN36",)),
    (WINDOW, ("WINDOW",)), (WINDOW, ("GLAZ",)),
    (STAIR, ("STAIR",)), (STAIR, ("HANGER",)),
    (ANNOTATION, ("ARROW",)), (ANNOTATION, ("ARCHTICK",)),
    (ANNOTATION, ("TICK",)), (ANNOTATION, ("MARKER",)),
    (ANNOTATION, ("SECTION",)), (ANNOTATION, ("DETAIL",)),
    (ANNOTATION, ("NORTH",)), (ANNOTATION, ("BUBBLE",)),
    (ANNOTATION, ("GENAXEH",)),
    (FURNITURE, ("SOFA",)), (FURNITURE, ("BED",)), (FURNITURE, ("TABLE",)),
    (FURNITURE, ("CHAIR",)), (FURNITURE, ("DESK",)), (FURNITURE, ("WARDROBE",)),
    (FURNITURE, ("CAR",)), (FURNITURE, ("VEHICLE",)),
    (ELECTRICAL, ("SWITCH",)), (ELECTRICAL, ("SOCKET",)), (ELECTRICAL, ("OUTLET",)),
)


def block_role(block_name: str) -> Tuple[str, float, str]:
    """Classify a block by name — a fixture is a fixture wherever it is drawn."""
    normalised = normalise_layer(block_name)
    flat = normalised.upper().replace("-", "").replace("_", "").replace("$", "")
    if not flat:
        return UNKNOWN, 0.0, "unnamed block"
    if flat.startswith("A") and re.match(r"^A\$C[0-9A-F]+$", normalised.upper()):
        # AutoCAD's anonymous bound-xref block naming.
        return UNKNOWN, 0.0, "anonymous block %r" % block_name
    for role, needed in _BLOCK_RULES:
        if all(t in flat for t in needed):
            return role, 0.85, "block %r matches %s by %s" % (
                block_name, role, "+".join(needed))
    return UNKNOWN, 0.0, "block %r matches no known convention" % block_name


# ---------------------------------------------------------------------------
# Entity-type rules
# ---------------------------------------------------------------------------

#: Entity types that are documentation no matter what layer they sit on.
#: This is the hard guarantee behind "a dimension line is never a wall".
ALWAYS_ANNOTATION = frozenset({
    "DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION",
    "LEADER", "MULTILEADER", "MLEADER", "TOLERANCE",
    "TEXT", "MTEXT", "ATTDEF", "ATTRIB",
    "WIPEOUT", "IMAGE", "OLE2FRAME", "SHAPE", "RAY", "XLINE",
    "TABLE", "ACAD_TABLE", "MESH", "HELIX",
})

#: Entity types that can carry building geometry.
DRAWABLE = frozenset({
    "LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE",
    "SOLID", "TRACE", "3DFACE", "HATCH", "INSERT",
})


@dataclass
class Classification:
    """The decision for one entity, with the evidence that produced it."""

    role: str
    confidence: float
    reason: str
    source: str = "layer"          # type | layer | block | geometry

    @property
    def is_wall(self) -> bool:
        return self.role in WALL_ROLES

    @property
    def is_building(self) -> bool:
        return self.role not in NEVER_BUILDING and self.role != UNKNOWN

    def as_dict(self) -> dict:
        return {"role": self.role, "confidence": self.confidence,
                "reason": self.reason, "source": self.source}


def classify_entity(
    dxftype: str,
    layer: str,
    *,
    block_name: Optional[str] = None,
    is_frozen: bool = False,
    is_off: bool = False,
) -> Classification:
    """Classify one entity by type, then layer, then block name.

    The order is not cosmetic. Type beats layer because a DIMENSION placed on a
    layer called ``A-WALL`` is still a dimension — drafters do this, and the
    only safe reading is the entity's own type. Layer beats block because a
    drafter who named the layer has classified the geometry deliberately.
    """
    upper = (dxftype or "").upper()

    if upper in ALWAYS_ANNOTATION:
        role = ROOM_LABEL if upper in ("TEXT", "MTEXT") else (
            DIMENSION if "DIMENSION" in upper else ANNOTATION)
        return Classification(
            role=role, confidence=1.0, source="type",
            reason="%s entities are documentation regardless of layer" % upper,
        )

    if is_off or is_frozen:
        return Classification(
            role=CONSTRUCTION, confidence=0.9, source="layer",
            reason="layer %r is switched off or frozen, so it is not part of the "
                   "drawing as issued" % layer,
        )

    role, confidence, reason = layer_role(layer)
    if role != UNKNOWN:
        return Classification(role=role, confidence=confidence,
                              reason=reason, source="layer")

    if block_name:
        role, confidence, reason = block_role(block_name)
        if role != UNKNOWN:
            return Classification(role=role, confidence=confidence,
                                  reason=reason, source="block")

    return Classification(
        role=UNKNOWN, confidence=0.0, source="layer",
        reason="neither layer %r nor entity type %s identifies this geometry"
               % (layer, upper),
    )


# ---------------------------------------------------------------------------
# Drawing-level layer survey
# ---------------------------------------------------------------------------

def survey_layers(
    layer_entity_counts: Dict[str, int],
    frozen: Sequence[str] = (),
    off: Sequence[str] = (),
) -> Dict[str, Classification]:
    """Classify every layer in the drawing once, up front.

    Returned as a lookup so per-entity classification is a dictionary hit
    rather than a regex sweep, which matters on a 12 MB drawing.
    """
    frozen_set = {normalise_layer(n).upper() for n in frozen}
    off_set = {normalise_layer(n).upper() for n in off}
    out: Dict[str, Classification] = {}
    for name in layer_entity_counts:
        key = normalise_layer(name).upper()
        if key in off_set or key in frozen_set:
            out[name] = Classification(
                role=CONSTRUCTION, confidence=0.9, source="layer",
                reason="layer %r is frozen or off" % name)
            continue
        role, confidence, reason = layer_role(name)
        out[name] = Classification(role=role, confidence=confidence,
                                   reason=reason, source="layer")
    return out


def wall_layers(survey: Dict[str, Classification]) -> List[str]:
    """The layers whose geometry is eligible to become walls."""
    return sorted(name for name, c in survey.items() if c.role in WALL_ROLES)


def has_usable_wall_layers(survey: Dict[str, Classification],
                           layer_entity_counts: Dict[str, int],
                           minimum: int = 8) -> bool:
    """Whether layer names alone identify enough wall geometry to work from.

    When this is false the pipeline falls back to geometric wall detection over
    all non-annotation geometry. That fallback is deliberately *not* the default
    — on a well-drawn sheet the layer names are better evidence than any
    geometric heuristic, and using the heuristic anyway is how ``A-CASE-1``
    kitchen cabinets become interior walls.
    """
    return sum(layer_entity_counts.get(n, 0) for n in wall_layers(survey)) >= minimum
