"""
ArchX3D — Text Semantics
========================
Parses TEXT / MTEXT / ATTRIB strings into room labels, areas and dimensions.

Why this is harder than it looks
--------------------------------
A room label in a real drawing is rarely the bare word "BEDROOM". It is::

    MASTER BEDROOM
    3.60 x 4.20

    BED ROOM-1
    (12'-6" x 14'-0")

    KITCHEN
    9.45 SQ.M.

    W.C.

So parsing has to survive abbreviation (``BED RM``, ``M.BEDRM``), embedded
dimensions, area annotations, numbering (``BEDROOM 2``), MTEXT formatting
codes, and both metric and imperial notation. Each of those is a separate
failure mode that silently produces ``unknown``, which is the outcome this
whole module exists to prevent.

What is deliberately *not* done
-------------------------------
No fuzzy string distance. Edit-distance matching against a room vocabulary
looks attractive and misfires badly on short strings: ``"DEN"`` is within
edit distance 1 of ``"DEN"``, ``"BED"`` and ``"DECK"``, which are three
different rooms. Instead the vocabulary is explicit and generous, and an
unmatched string honestly returns no room type rather than a plausible wrong
one. Guessing here would violate the project's first design rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Room vocabulary
# ---------------------------------------------------------------------------
#
# Maps a canonical room type to the spellings that name it. Ordered: the first
# entry whose pattern matches wins, so more specific rooms ("master bedroom")
# must precede the general ones ("bedroom") that their text contains.
#
# Canonical types align with ``vision.catalog.ROOM_TYPES`` where one exists;
# the extras (utility, store, garage, ...) are types a plan labels but the
# vision catalog has no style prior for, and are still worth identifying.

_RoomRule = Tuple[str, Tuple[str, ...]]

ROOM_LABEL_RULES: Tuple[_RoomRule, ...] = (
    # --- Sanitary. First, because "BATH" appears inside "BATHROOM" and
    # --- "MASTER BATH", and because these are the least ambiguous labels.
    ("bathroom", (
        r"\bBATH ?ROOMS?\b", r"\bBATHS?\b", r"\bW\.? ?C\.?\b", r"\bWATER CLOSET\b",
        r"\bTOILETS?\b", r"\bREST ?ROOMS?\b", r"\bLAVATORY\b", r"\bPOWDER ?(ROOM|RM)\b",
        r"\bENSUITE\b", r"\bEN ?SUITE\b", r"\bSHOWER ?(ROOM|RM)\b", r"\bWASH ?ROOMS?\b",
        r"\bSANITARY\b", r"\bLOO\b", r"\bCLOAK ?(ROOM|RM)\b", r"\bATT ?BATH\b",
        r"\bCOMMON ?BATH\b", r"\bBATH ?ATT\b",
    )),
    # --- Kitchen and eating.
    ("kitchen", (
        r"\bKITCHENS?\b", r"\bKIT\.?\b", r"\bKITCH\b", r"\bGALLEY\b",
        r"\bPANTRY\b", r"\bSCULLERY\b", r"\bKITCHENETTE\b", r"\bCOOK(ING)? ?(AREA|RM)\b",
    )),
    ("dining_room", (
        r"\bDINING ?(ROOM|RM|AREA|HALL|SPACE)?\b", r"\bDIN\.? ?(RM|ROOM)\b",
        r"\bBREAKFAST\b", r"\bMESS\b", r"\bDINETTE\b",
    )),
    # --- Sleeping. "MASTER" variants first so they are not eaten by "BEDROOM".
    ("master_bedroom", (
        r"\bMASTER ?BED ?(ROOM|RM)?\b", r"\bM\.? ?BED ?(ROOM|RM)?\b",
        r"\bMASTER ?SUITE\b", r"\bPRIMARY ?BED ?(ROOM|RM)?\b", r"\bMBR\b",
    )),
    ("bedroom", (
        r"\bBED ?ROOMS?\b", r"\bBED ?RMS?\b", r"\bBEDS?\b(?! ?SIDE)", r"\bBR ?[0-9]\b",
        r"\bGUEST ?(ROOM|RM|BED)\b", r"\bKIDS? ?(ROOM|RM|BED)\b",
        r"\bCHILD(REN)?S? ?(ROOM|RM)\b", r"\bNURSERY\b", r"\bDORM\b",
    )),
    # --- Living.
    ("living_room", (
        r"\bLIVING ?(ROOM|RM|AREA|SPACE|HALL)?\b", r"\bLIV\.? ?(RM|ROOM)?\b",
        r"\bDRAWING ?(ROOM|RM)\b", r"\bLOUNGE\b", r"\bSITTING ?(ROOM|RM|AREA)\b",
        r"\bFAMILY ?(ROOM|RM)\b", r"\bGREAT ?(ROOM|RM)\b", r"\bPARLOU?R\b",
        r"\bDEN\b", r"\bSALON\b", r"\bRECEPTION\b",
    )),
    ("office", (
        r"\bOFFICES?\b", r"\bSTUDY\b", r"\bWORK ?(ROOM|RM|SPACE|STATION)\b",
        r"\bLIBRARY\b", r"\bCABIN\b", r"\bHOME ?OFFICE\b",
    )),
    ("studio", (r"\bSTUDIO\b", r"\bATELIER\b")),
    # --- Circulation.
    ("hallway", (
        r"\bHALL ?WAYS?\b", r"\bCORRIDORS?\b", r"\bPASSAGES?\b", r"\bFOYER\b",
        r"\bLOBBY\b", r"\bVESTIBULE\b", r"\bENTRY\b", r"\bENTRANCE\b",
        r"\bLANDING\b", r"\bCIRCULATION\b", r"\bHALLS?\b", r"\bPORCH\b",
    )),
    ("staircase", (
        r"\bSTAIR ?(CASE|WELL|S)?\b", r"\bSTEPS\b", r"\bLIFT\b", r"\bELEVATOR\b",
    )),
    # --- Service.
    ("utility", (
        r"\bUTILITY\b", r"\bLAUNDRY\b", r"\bWASH ?(ROOM|AREA)\b", r"\bMACHINE ?(RM|ROOM)\b",
        r"\bSERVICE ?(ROOM|RM|AREA)\b", r"\bBOILER ?(ROOM|RM)\b", r"\bPLANT ?(ROOM|RM)\b",
        r"\bELECT(RICAL)? ?(ROOM|RM)\b", r"\bMECH(ANICAL)? ?(ROOM|RM)\b",
    )),
    ("store", (
        r"\bSTORES?\b", r"\bSTORAGE\b", r"\bSTORE ?(ROOM|RM)\b", r"\bCLOSET\b",
        r"\bWARDROBE ?(ROOM|RM)\b", r"\bWALK ?IN\b", r"\bLARDER\b", r"\bATTIC\b",
        r"\bLOFT\b", r"\bBASEMENT\b", r"\bCELLAR\b",
    )),
    ("garage", (r"\bGARAGE\b", r"\bCAR ?(PARK|PORT)\b", r"\bPARKING\b", r"\bSTILT\b")),
    # --- Outdoor.
    ("balcony", (
        r"\bBALCONY\b", r"\bBALC\.?\b", r"\bTERRACE\b", r"\bDECK\b", r"\bVERANDAH?\b",
        r"\bPATIO\b", r"\bLOGGIA\b", r"\bSIT ?OUT\b", r"\bOPEN ?TO ?SKY\b", r"\bOTS\b",
        r"\bCOURT ?YARD\b", r"\bGARDEN\b", r"\bLAWN\b",
    )),
    ("shaft", (
        r"\bSHAFTS?\b", r"\bDUCT\b", r"\bVOID\b", r"\bCHASE\b", r"\bRISER\b",
        r"\bAIR ?SHAFT\b",
    )),
)

_COMPILED_ROOM_RULES = tuple(
    (room_type, tuple(re.compile(p) for p in patterns))
    for room_type, patterns in ROOM_LABEL_RULES
)

#: Types that are a refinement of a broader type. The semantic layer scores
#: against the broad type but keeps the specific one as the display label.
ROOM_TYPE_PARENTS: Dict[str, str] = {
    "master_bedroom": "bedroom",
    "staircase": "hallway",
    "studio": "living_room",
}


def canonical_room_type(room_type: str) -> str:
    """Collapse a refined type onto the type the priors are defined for."""
    return ROOM_TYPE_PARENTS.get(room_type, room_type)


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

#: MTEXT inline formatting: ``\pxi-3;``, ``{\fArial|b0;...}``, ``\P``, ``%%d``.
_MTEXT_CODES = re.compile(
    r"\\[A-Za-z][^;\\]*;"      # \f..., \H..., \pxi...;
    r"|\\[PXpx~]"              # paragraph / column breaks
    r"|[{}]"                   # grouping braces
    r"|%%[udcpo]",             # %%u underline, %%d degree, %%c diameter
    re.IGNORECASE,
)


def clean_text(raw: str) -> str:
    """Strip MTEXT formatting and collapse whitespace.

    Applied before any matching: an unstripped ``{\\fArial|b1;KITCHEN}`` fails
    every pattern and silently becomes an unlabelled room.
    """
    if not raw:
        return ""
    text = _MTEXT_CODES.sub(" ", raw)
    text = text.replace("\\~", " ").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalise(raw: str) -> str:
    """Uppercase, de-punctuate and collapse — the form rules match against.

    Full stops become spaces so ``M.BED.RM`` and ``W.C.`` normalise to
    ``M BED RM`` and ``W C``, which the vocabulary's patterns anticipate.
    """
    text = clean_text(raw).upper()
    text = re.sub(r"[._/\\|,;:*#()\[\]{}\"']+", " ", text)
    text = re.sub(r"[-–—]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Numeric annotations
# ---------------------------------------------------------------------------

#: ``3.60 X 4.20``, ``3600x4200``, ``12'-6" X 14'-0"``.
_DIMENSION_PAIR = re.compile(
    r"(?P<a>[0-9]+(?:\.[0-9]+)?(?:\s*'\s*(?:[-\s]*[0-9]+(?:\.[0-9]+)?\s*\")?)?)"
    r"\s*[xX×]\s*"
    r"(?P<b>[0-9]+(?:\.[0-9]+)?(?:\s*'\s*(?:[-\s]*[0-9]+(?:\.[0-9]+)?\s*\")?)?)"
)

#: ``9.45 SQ.M``, ``102 SFT``, ``AREA: 14.2 M2``.
#: The optional dots matter: ``SQ.FT`` and ``SQ.M.`` are both ubiquitous, and
#: a pattern that only accepts ``SQ FT`` silently reads neither.
_AREA_ANNOTATION = re.compile(
    r"(?P<value>[0-9]+(?:\.[0-9]+)?)\s*"
    r"(?P<unit>SQ\.?\s?M(?:TR?)?\.?|M2|M\^2|SQ\.?\s?FT?\.?|SFT|FT2)\b",
    re.IGNORECASE,
)

#: A bare imperial measure: ``12'-6"``.
_IMPERIAL = re.compile(r"(?P<feet>[0-9]+)\s*'\s*(?:[-\s]*(?P<inches>[0-9]+(?:\.[0-9]+)?)\s*\")?")


def parse_area(text: str) -> Optional[Tuple[float, str]]:
    """Extract an annotated floor area, returning ``(value_m2, unit)``.

    Converts imperial to m² so callers always receive metric.
    """
    match = _AREA_ANNOTATION.search(clean_text(text))
    if not match:
        return None
    try:
        value = float(match.group("value"))
    except ValueError:
        return None

    unit = match.group("unit").upper().replace(" ", "").replace(".", "")
    if unit in ("SQFT", "SFT", "SQF", "FT2"):
        return value * 0.09290304, "sq_ft"
    return value, "sq_m"


def parse_dimension_pair(text: str) -> Optional[Tuple[float, float]]:
    """Extract a ``W x D`` room dimension annotation, in metres.

    Returns ``None`` rather than guessing when the units are ambiguous — a
    bare ``3.6 x 4.2`` is metres, a bare ``3600 x 4200`` is millimetres, and
    anything between is not safely decidable, so it is refused.
    """
    cleaned = clean_text(text)
    match = _DIMENSION_PAIR.search(cleaned)
    if not match:
        return None

    a = _parse_length(match.group("a"))
    b = _parse_length(match.group("b"))
    if a is None or b is None:
        return None
    return a, b


def _parse_length(token: str) -> Optional[float]:
    """One length in metres, from metric or imperial notation."""
    token = token.strip()

    imperial = _IMPERIAL.search(token)
    if imperial:
        feet = float(imperial.group("feet"))
        inches = float(imperial.group("inches") or 0.0)
        return feet * 0.3048 + inches * 0.0254

    try:
        value = float(token)
    except ValueError:
        return None

    if value <= 0:
        return None
    # A room edge is 0.5-30 m. Values in the hundreds/thousands are mm or cm.
    if value <= 30.0:
        return value
    if 300.0 <= value <= 30000.0:
        return value / 1000.0
    if 30.0 < value < 300.0:
        return value / 100.0
    return None


# ---------------------------------------------------------------------------
# Room label parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextClassification:
    """What one text string was understood to be."""

    #: room_label | area | dimension | note | number
    role: str
    room_type: str = ""
    confidence: float = 0.0
    reason: str = ""
    #: Area in m² when the string annotates one.
    area_m2: Optional[float] = None
    #: ``(width, depth)`` in metres when the string annotates a size.
    dimensions: Optional[Tuple[float, float]] = None
    #: Trailing index in a numbered label: ``BEDROOM 2`` -> 2.
    index: Optional[int] = None


#: Strings that look like labels but name the drawing, not a room. Matched
#: whole-string so a legitimate ``FIRST FLOOR LOBBY`` is unaffected.
_TITLE_NOISE = re.compile(
    r"^(?:"
    r"(?:GROUND|FIRST|SECOND|THIRD|TYPICAL|UPPER|LOWER|BASEMENT|ROOF|[0-9]+(?:ST|ND|RD|TH))?"
    r"\s*FLOOR\s*PLAN"
    r"|(?:SITE|LAYOUT|KEY|LOCATION|ROOF|CEILING|FURNITURE|ELECTRICAL|PLUMBING)\s*PLAN"
    r"|SCALE\s*[:=]?.*|N\.?T\.?S\.?|DO\s*NOT\s*SCALE.*"
    r"|DRAWN\s*BY.*|CHECKED\s*BY.*|APPROVED.*|CLIENT.*|PROJECT.*|DRAWING\s*(NO|TITLE).*"
    r"|SHEET\s*(NO)?.*|REV(ISION)?\s*(NO)?.*|DATE\s*[:=]?.*"
    r"|ALL\s+DIMENSIONS?.*|NOTES?\s*[:=]?|LEGEND|GENERAL\s+NOTES?"
    r"|SECTION\s*[-A-Z0-9 ]*|ELEVATION\s*[-A-Z0-9 ]*|DETAIL\s*[-A-Z0-9 ]*"
    r"|NORTH|TRUE\s*NORTH"
    r")$"
)


def classify_text(raw: str) -> TextClassification:
    """Decide what a single text string is.

    The order of checks encodes precedence: a string containing a room name is
    a room label *even if* it also carries dimensions, because ``KITCHEN
    3.6x4.2`` is primarily a label and secondarily a size.
    """
    cleaned = clean_text(raw)
    if not cleaned:
        return TextClassification(role="note", reason="empty string")

    text = normalise(raw)

    if _TITLE_NOISE.match(text):
        return TextClassification(
            role="note", reason=f"drawing metadata, not a room label: {cleaned!r}"
        )

    area = parse_area(cleaned)
    dimensions = parse_dimension_pair(cleaned)

    room_type, matched = _match_room(text)
    if room_type:
        return TextClassification(
            role="room_label",
            room_type=room_type,
            confidence=0.94,
            reason=f"text {cleaned!r} matched room vocabulary term {matched!r}",
            area_m2=area[0] if area else None,
            dimensions=dimensions,
            index=_trailing_index(text),
        )

    if area:
        return TextClassification(
            role="area", confidence=0.9, area_m2=area[0],
            reason=f"area annotation {cleaned!r} ({area[1]})",
        )

    if dimensions:
        return TextClassification(
            role="dimension", confidence=0.85, dimensions=dimensions,
            reason=f"dimension annotation {cleaned!r}",
        )

    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        return TextClassification(
            role="number", confidence=0.5, reason=f"bare number {cleaned!r}"
        )

    return TextClassification(
        role="note", reason=f"no room vocabulary term in {cleaned!r}"
    )


def _match_room(normalised_text: str) -> Tuple[str, str]:
    """First matching room vocabulary rule, as ``(room_type, matched_term)``."""
    for room_type, patterns in _COMPILED_ROOM_RULES:
        for pattern in patterns:
            match = pattern.search(normalised_text)
            if match:
                return room_type, match.group(0).strip()
    return "", ""


def _trailing_index(text: str) -> Optional[int]:
    """The instance number in ``BEDROOM 2`` / ``BED RM-03``."""
    match = re.search(r"\b([0-9]{1,2})\s*$", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def room_type_from_label(raw: str) -> Tuple[str, float]:
    """Convenience wrapper: ``(room_type, confidence)``, empty when unmatched."""
    result = classify_text(raw)
    if result.role != "room_label":
        return "", 0.0
    return result.room_type, result.confidence


def summarise(classifications: Sequence[TextClassification]) -> Dict[str, int]:
    """Role histogram, for diagnostics."""
    histogram: Dict[str, int] = {}
    for classification in classifications:
        histogram[classification.role] = histogram.get(classification.role, 0) + 1
    return dict(sorted(histogram.items(), key=lambda kv: (-kv[1], kv[0])))
