"""
ArchX3D — Layer Semantics
=========================
Turns a layer *name* into a semantic role.

Why layer names are trustworthy
-------------------------------
Layer naming in architectural CAD is not arbitrary. Most professional
drawings follow the AIA CAD Layer Guidelines (now the US National CAD
Standard) or ISO 13567, both of which encode discipline and content into the
name itself. ``A-WALL-FULL`` is not a hopeful guess about walls — it is a
declaration, in a published standard, that this layer holds full-height
architectural walls. That makes layer names trust tier 3.

The previous extractor treated layers as a blacklist: anything matching
``DOOR``, ``TEXT`` or ``FURNITURE`` was *discarded*. That is exactly backwards.
A layer called ``A-DOOR`` is the single most reliable statement in the file
about where the doors are, and doors are what tell rooms apart. This module
keeps everything and labels it instead.

Matching strategy
-----------------
Three passes, most specific first, so a structured name never falls through to
a loose substring match:

1. **AIA / NCS** — parse ``<discipline>-<major>-<minor>`` and look the major
   group up in the published table. Highest confidence, because the position
   of the field carries meaning that a substring match cannot see.
2. **ISO 13567** — agent/element fields, recognised by their fixed widths.
3. **Keyword** — ordered substring rules for the large body of drawings that
   follow no standard at all (``Walls``, ``furniture-new``, ``05 DOORS``).

Ordering within the keyword pass matters: ``WINDOW`` must be tested before
``WIND`` (wind loading), and ``DOOR`` before ``FLOOR``, or a floor layer
becomes a door layer. The table is ordered deliberately and the tests pin it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

#: Every role a layer can carry. ``wall`` is the only one that becomes
#: extruded geometry by default; the rest inform semantics.
ROLES = (
    "wall", "door", "window", "column", "beam", "stair", "railing",
    "room_boundary", "area",
    "furniture", "casework", "appliance", "plumbing_fixture", "equipment",
    "text", "dimension", "annotation", "grid", "hatch",
    "ceiling", "floor", "roof", "electrical", "plumbing", "hvac",
    "site", "title_block", "construction", "unknown",
)

#: Roles whose geometry should be extruded as walls.
WALL_ROLES = ("wall",)

#: Roles that carry semantic evidence but must never become wall geometry.
#: The old extractor's blacklist, inverted: these are *kept*, just not built.
NON_STRUCTURAL_ROLES = (
    "text", "dimension", "annotation", "grid", "title_block",
    "furniture", "casework", "appliance", "plumbing_fixture", "equipment",
    "area", "construction",
)


@dataclass(frozen=True)
class LayerClassification:
    role: str
    confidence: float
    reason: str
    convention: str  # aia | iso13567 | keyword | none


# ---------------------------------------------------------------------------
# AIA / National CAD Standard
# ---------------------------------------------------------------------------

#: Discipline designators. Only the ones that alter interpretation are listed;
#: an unrecognised discipline still parses, it just carries less weight.
AIA_DISCIPLINES = {
    "A": "architectural", "I": "interiors", "S": "structural",
    "M": "mechanical", "E": "electrical", "P": "plumbing",
    "F": "fire_protection", "C": "civil", "L": "landscape",
    "Q": "equipment", "G": "general", "T": "telecom", "X": "other",
}

#: AIA major group -> our role. These are the published four-character codes.
AIA_MAJOR_GROUPS: Dict[str, str] = {
    "WALL": "wall",
    "DOOR": "door",
    "GLAZ": "window",      # Glazing: windows, curtain wall, storefront.
    "COLS": "column",
    "BEAM": "beam",
    "STRS": "stair",
    "HRAL": "railing",
    "FLOR": "floor",
    "CLNG": "ceiling",
    "ROOF": "roof",
    "FURN": "furniture",
    "CASE": "casework",     # Built-in cabinetry, counters, shelving.
    "EQPM": "equipment",
    "APPL": "appliance",
    "PFIX": "plumbing_fixture",
    "TPTN": "casework",     # Toilet partitions — bathroom evidence.
    "AREA": "area",
    "ANNO": "annotation",
    "IDEN": "text",         # Identification: room names and numbers.
    "DIMS": "dimension",
    "PATT": "hatch",
    "GRID": "grid",
    "LITE": "electrical",
    "POWR": "electrical",
    "HVAC": "hvac",
    "PIPE": "plumbing",
    "SITE": "site",
    "TTLB": "title_block",
    "NPLT": "construction",  # Non-plotting construction geometry.
}

#: ``A-ANNO-TEXT`` is text, not generic annotation. Minor groups that override
#: the major group's role, keyed by ``(major, minor)``.
AIA_MINOR_OVERRIDES: Dict[Tuple[str, str], str] = {
    ("ANNO", "TEXT"): "text",
    ("ANNO", "NOTE"): "text",
    ("ANNO", "IDEN"): "text",
    ("ANNO", "DIMS"): "dimension",
    ("ANNO", "TTLB"): "title_block",
    ("ANNO", "PATT"): "hatch",
    ("AREA", "IDEN"): "text",
    # ``A-FLOR-*`` is the most heavily sub-classified major group in the
    # standard: almost everything sitting on a floor is filed under it, so
    # without these overrides the layers carrying the plan's fixture evidence
    # all collapse to the uninformative role "floor".
    ("FLOR", "PATT"): "hatch",
    ("FLOR", "TPTN"): "casework",
    ("FLOR", "STRS"): "stair",
    ("FLOR", "HRAL"): "railing",
    ("FLOR", "FIXT"): "plumbing_fixture",
    ("FLOR", "PFIX"): "plumbing_fixture",
    ("FLOR", "CASE"): "casework",
    ("FLOR", "APPL"): "appliance",
    ("FLOR", "EQPM"): "equipment",
    ("FLOR", "FURN"): "furniture",
    ("FLOR", "WDWK"): "casework",
    ("FLOR", "IDEN"): "text",
    ("FLOR", "AREA"): "area",
    ("FLOR", "EVTR"): "stair",   # Elevators and vertical transport.
    ("WALL", "PATT"): "hatch",
}

_AIA_PATTERN = re.compile(
    r"^(?P<discipline>[A-Z])(?P<sub>[A-Z0-9])?[-_]"
    r"(?P<major>[A-Z]{4})"
    r"(?:[-_](?P<minor>[A-Z]{4}))?"
    r"(?:[-_](?P<minor2>[A-Z]{4}))?"
    r"(?:[-_](?P<status>[A-Z0-9]{1,4}))?$"
)


def _classify_aia(name: str) -> Optional[LayerClassification]:
    """Parse an AIA / NCS layer name, or return ``None`` if it isn't one."""
    match = _AIA_PATTERN.match(name.strip().upper())
    if not match:
        return None

    discipline = match.group("discipline")
    major = match.group("major")
    minor = match.group("minor") or ""
    minor2 = match.group("minor2") or ""

    if major not in AIA_MAJOR_GROUPS:
        return None

    role = AIA_MAJOR_GROUPS[major]
    detail = f"major group {major}"

    # A minor group can refine the major group's meaning; check both slots so
    # ``A-FLOR-OTLN-PATT`` still resolves to a hatch.
    for candidate in (minor, minor2):
        override = AIA_MINOR_OVERRIDES.get((major, candidate))
        if override:
            role = override
            detail = f"major group {major}, minor group {candidate}"
            break

    # A plumbing-discipline layer carrying fixtures is stronger evidence about
    # bathrooms than the same major group under architectural.
    known_discipline = discipline in AIA_DISCIPLINES
    confidence = 0.95 if known_discipline else 0.80

    return LayerClassification(
        role=role,
        confidence=confidence,
        reason=(
            f"AIA/NCS layer name: discipline "
            f"{AIA_DISCIPLINES.get(discipline, discipline)}, {detail}"
        ),
        convention="aia",
    )


# ---------------------------------------------------------------------------
# ISO 13567
# ---------------------------------------------------------------------------

#: ISO 13567 element codes for building parts (table 2 of the standard),
#: restricted to the ones that matter for interior reconstruction.
ISO_ELEMENTS: Dict[str, str] = {
    "21": "wall",       # Walls
    "22": "wall",       # Internal walls
    "23": "floor",      # Floors
    "24": "stair",      # Stairs and ramps
    "27": "roof",       # Roofs
    "28": "ceiling",    # Ceilings / building frame
    "31": "window",     # Openings in external walls
    "32": "door",       # Openings in internal walls
    "34": "railing",    # Balustrades
    "37": "furniture",  # Loose furniture
    "41": "casework",   # Fittings
    "52": "plumbing",   # Water/drainage
    "61": "electrical",
    "71": "furniture",  # Fixed furniture / fittings
}

_ISO_PATTERN = re.compile(r"^[A-Z]{1,2}(?P<element>\d{2})\d{0,2}")


def _classify_iso(name: str) -> Optional[LayerClassification]:
    """Parse an ISO 13567 layer name.

    Deliberately conservative: the standard's fields are positional and many
    non-conforming names coincidentally start with letters then digits, so a
    match only counts when the element code is one we recognise.
    """
    match = _ISO_PATTERN.match(name.strip().upper())
    if not match:
        return None

    element = match.group("element")
    role = ISO_ELEMENTS.get(element)
    if role is None:
        return None

    return LayerClassification(
        role=role,
        confidence=0.75,
        reason=f"ISO 13567 element code {element}",
        convention="iso13567",
    )


# ---------------------------------------------------------------------------
# Keyword fallback
# ---------------------------------------------------------------------------

#: Ordered ``(pattern, role, confidence)`` rules for non-standard names.
#:
#: Order is load-bearing. Each rule is tried in turn and the first hit wins,
#: so more specific and more easily-confused terms must come first:
#:   * ``WINDOW`` before any ``WIND`` rule
#:   * ``DOOR`` before ``FLOOR``  (``FLOOR`` contains no ``DOOR``... but
#:     ``FLOOR`` does contain ``LOOR``; the real trap is the reverse, and
#:     word-boundary anchoring handles it)
#:   * ``ROOM`` boundary before generic text
_KEYWORD_RULES: Tuple[Tuple[str, str, float], ...] = (
    # --- Annotation: matched early because these names often also contain
    # --- an element word ("WALL-DIMENSIONS", "DOOR TAGS").
    (r"TITLE\s*BLOCK|TTLB|TITLEBLK|\bTBLK\b", "title_block", 0.9),
    (r"DIMENSION|\bDIMS?\b|\bDIM[-_]", "dimension", 0.9),
    (r"\bTEXT\b|\bTXT\b|\bLABEL|\bNOTES?\b|\bIDEN\b|ROOM[-_ ]?NAME|ROOM[-_ ]?TAG",
     "text", 0.88),
    (r"\bANNO|ANNOTATION|\bTAGS?\b|\bLEGEND\b|\bKEYNOTE", "annotation", 0.85),
    (r"\bGRID\b|GRIDLINE|COLUMN[-_ ]?GRID|\bAXIS\b", "grid", 0.85),
    (r"\bHATCH|\bPATT\b|PATTERN|\bFILL\b|POCHE", "hatch", 0.8),
    (r"DEFPOINTS|\bNPLT\b|NO[-_ ]?PLOT|CONSTRUCTION|\bTEMP\b|\bSCRATCH\b",
     "construction", 0.85),
    (r"VIEWPORT|\bVPORT\b|PAPER[-_ ]?SPACE|\bBORDER\b|SHEET", "title_block", 0.8),

    # --- Openings: before walls, because "WALL-OPENINGS" is about openings.
    (r"WINDOW|\bGLAZ|GLAZING|CURTAIN[-_ ]?WALL|STOREFRONT|\bWIN\b|\bWDW\b",
     "window", 0.9),
    (r"\bDOORS?\b|\bDR\b|DOORWAY|\bOPENING", "door", 0.9),

    # --- Structure.
    (r"COLUMN|\bCOLS?\b|\bPILLAR|\bPIER\b", "column", 0.88),
    (r"\bBEAMS?\b|\bJOIST|\bLINTEL|\bGIRDER", "beam", 0.85),
    (r"STAIR|\bSTRS\b|\bSTEPS?\b|ESCALATOR|\bRAMP\b", "stair", 0.88),
    (r"RAILING|HANDRAIL|BALUSTRADE|\bHRAL\b|\bGUARD\b", "railing", 0.85),
    (r"PARTITION|\bWALLS?\b|\bWALL[-_]|[-_]WALL\b|\bPARTN\b|MASONRY|BRICKWORK",
     "wall", 0.85),

    # --- Room delineation: distinct from walls; these are the polygons a
    # --- drawing uses to state where one room ends, which beats flood-fill.
    (r"ROOM[-_ ]?(BOUND|OUTLINE|POLY|AREA|EXTENT)|\bAREA[-_ ]?(POLY|BOUND)"
     r"|SPACE[-_ ]?(BOUND|POLY)|\bRMBD\b", "room_boundary", 0.9),
    (r"\bAREAS?\b|\bZONE\b|\bSPACES?\b|\bRMS?\b|\bROOMS?\b", "area", 0.7),

    # --- Contents.
    (r"PLUMB|\bPFIX\b|SANITARY|\bWC\b|TOILET|LAVATORY|\bBATH\b|SHOWER|\bSINK\b|"
     r"BASIN|URINAL|BIDET", "plumbing_fixture", 0.88),
    (r"CASEWORK|\bCASE\b|CABINET|MILLWORK|JOINERY|COUNTERTOP|\bCOUNTER\b|"
     r"\bSHELV|WARDROBE|CLOSET|\bTPTN\b", "casework", 0.85),
    (r"APPLIANCE|\bAPPL\b|REFRIGERAT|\bFRIDGE\b|\bOVEN\b|\bSTOVE\b|COOKTOP|"
     r"DISHWASH|\bHOB\b", "appliance", 0.85),
    (r"FURNITURE|\bFURN\b|\bFF&E\b|\bFFE\b|\bMOBILIER\b|\bSEATING\b|\bDESKS?\b",
     "furniture", 0.88),
    (r"EQUIPMENT|\bEQPM\b|\bEQUIP\b|MACHINERY", "equipment", 0.8),

    # --- Services and envelope.
    (r"\bHVAC\b|DUCT|MECHANICAL|VENTILAT|\bAHU\b", "hvac", 0.85),
    (r"ELECTRIC|\bELEC\b|\bPOWR\b|\bPOWER\b|LIGHTING|\bLITE\b|SWITCH|SOCKET|"
     r"\bOUTLET", "electrical", 0.85),
    (r"\bPIPE|DRAINAGE|\bWASTE\b|\bSOIL\b", "plumbing", 0.8),
    (r"CEILING|\bCLNG\b|\bRCP\b|SOFFIT", "ceiling", 0.85),
    (r"\bROOF\b|\bRF\b", "roof", 0.8),
    (r"\bFLOOR\b|\bFLOR\b|SLAB|SCREED|\bFFL\b", "floor", 0.8),
    (r"\bSITE\b|\bLANDSCAPE\b|\bPARKING\b|\bTOPO\b|CONTOUR|\bTREE\b",
     "site", 0.8),
)

_COMPILED_RULES = tuple(
    (re.compile(pattern), role, confidence)
    for pattern, role, confidence in _KEYWORD_RULES
)


def _classify_keyword(name: str) -> Optional[LayerClassification]:
    """First matching keyword rule wins; ``None`` when nothing matches."""
    # Separators become spaces so ``\b`` anchors work on ``A_WALL_NEW`` and
    # ``05-DOORS`` alike, while the original name stays intact for reporting.
    haystack = re.sub(r"[-_./|]+", " ", name.upper())

    for pattern, role, confidence in _COMPILED_RULES:
        match = pattern.search(haystack)
        if match:
            return LayerClassification(
                role=role,
                confidence=confidence,
                reason=f"layer name contains {match.group(0).strip()!r}",
                convention="keyword",
            )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_layer(name: str) -> LayerClassification:
    """Resolve a layer name to a semantic role.

    Never raises and never returns ``None``: an unrecognised layer is reported
    as ``unknown`` with zero confidence, which downstream code treats as "no
    evidence" rather than as an error.
    """
    if not name or not name.strip():
        return LayerClassification("unknown", 0.0, "empty layer name", "none")

    cleaned = name.strip()

    # Layer "0" is AutoCAD's default. Geometry left on it says nothing about
    # intent, so it is deliberately not guessed at.
    if cleaned in ("0", "0.0"):
        return LayerClassification(
            "unknown", 0.0, "default layer '0' carries no semantic intent", "none"
        )

    for classifier in (_classify_aia, _classify_iso, _classify_keyword):
        result = classifier(cleaned)
        if result is not None:
            return result

    return LayerClassification(
        "unknown", 0.0, f"no naming convention recognised in {cleaned!r}", "none"
    )


def classify_all(names: Sequence[str]) -> Dict[str, LayerClassification]:
    """Classify a collection of layer names."""
    return {name: classify_layer(name) for name in names}


def wall_layer_names(names: Sequence[str]) -> List[str]:
    """Layers whose geometry should be extruded into walls.

    Used when no explicit layer selection is supplied. Returns names in the
    input order so extraction is reproducible.
    """
    return [n for n in names if classify_layer(n).role in WALL_ROLES]


def summarise(classifications: Dict[str, LayerClassification]) -> Dict[str, int]:
    """Role histogram, for diagnostics."""
    histogram: Dict[str, int] = {}
    for classification in classifications.values():
        histogram[classification.role] = histogram.get(classification.role, 0) + 1
    return dict(sorted(histogram.items(), key=lambda kv: (-kv[1], kv[0])))
