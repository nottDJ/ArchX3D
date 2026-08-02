"""
ArchX3D — Block Semantics
=========================
Turns a block *name* (and its attributes) into a semantic category.

Why blocks are the strongest signal
-----------------------------------
When an architect inserts a block named ``WC-01`` they have stated, in the
file, that a toilet is at that coordinate. Nothing inferred from geometry or
guessed from a photograph competes with that. Blocks are trust tier 2 —
above layers and text — because:

* a *text label* can go stale after a plan revision while the block that was
  actually redrawn is current;
* a *layer* says what kind of thing is on it, but a block says which thing and
  exactly where.

Fixtures decide room types
--------------------------
This module's most valuable output is not furniture. It is fixtures. A toilet
means bathroom, essentially without exception. A hob and a sink together mean
kitchen. That single fact resolves most of a residential plan, which is why
``FIXTURE_CATEGORIES`` is more finely specified than the furniture table.

Two vocabularies, deliberately
------------------------------
``category`` is the semantic term used for reasoning and lives in this
module's vocabulary, which includes fixtures (``toilet``, ``bathtub``) that
the vision catalog has no entry for. ``catalog_category`` is the
``vision.catalog`` term used for *building geometry*, and is empty when no
buildable prior exists yet. Keeping them separate means recognising a toilet
does not require being able to model one — classification and generation
advance independently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------

#: Coarse grouping, used to decide how a block is treated by the pipeline.
KINDS = (
    "door", "window", "column", "stair",
    "plumbing_fixture", "kitchen_fixture", "appliance",
    "furniture", "casework", "electrical",
    "annotation", "north_arrow", "grid_bubble", "title_block",
    "unknown",
)


@dataclass(frozen=True)
class BlockClassification:
    """What a block name was understood to mean."""

    category: str
    kind: str
    confidence: float
    reason: str
    #: Equivalent ``vision.catalog`` category, or "" when none exists.
    catalog_category: str = ""


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
#
# Each entry: category -> (kind, catalog_category, patterns...)
#
# Patterns are matched against the *normalised* block name (uppercase, all
# separators collapsed to single spaces). They are regexes, and order within
# the overall table matters — see ``_ORDERED_RULES``.

_Rule = Tuple[str, str, str, Tuple[str, ...]]

#: Plumbing and kitchen fixtures. These carry the most classification weight,
#: so their patterns are written tightly to avoid false positives: a block
#: called ``SINK-UNIT`` is a sink, but ``THINK-BUBBLE`` must not be.
FIXTURE_RULES: Tuple[_Rule, ...] = (
    ("toilet", "plumbing_fixture", "", (
        r"\bWC\b", r"\bW C\b", r"TOILET", r"\bWATER CLOSET\b", r"\bLOO\b",
        r"\bPAN\b(?! ?HEAD)", r"\bCOMMODE\b", r"\bEWC\b",
    )),
    ("urinal", "plumbing_fixture", "", (r"URINAL", r"\bURN\b")),
    ("bidet", "plumbing_fixture", "", (r"BIDET",)),
    ("washbasin", "plumbing_fixture", "", (
        r"WASH ?BASIN", r"\bBASIN\b", r"\bLAVATORY\b", r"\bLAV\b",
        r"HAND ?WASH", r"\bVANITY\b", r"WASH ?HAND",
    )),
    ("bathtub", "plumbing_fixture", "", (
        r"BATH ?TUB", r"\bBATHTUB\b", r"\bTUB\b", r"\bBATH\b(?! ?ROOM)",
        r"\bJACUZZI\b", r"SOAK(ER|ING)? ?TUB",
    )),
    ("shower", "plumbing_fixture", "", (
        r"\bSHOWER\b", r"\bSHR\b", r"SHOWER ?TRAY", r"SHOWER ?CUBICLE",
        r"\bWET ?ROOM\b",
    )),
    ("floor_drain", "plumbing_fixture", "", (r"FLOOR ?DRAIN", r"\bFD\b", r"\bGULLY\b")),
    ("water_heater", "plumbing_fixture", "", (
        r"WATER ?HEATER", r"\bGEYSER\b", r"\bBOILER\b", r"\bCYLINDER\b",
    )),

    # Kitchen. ``sink`` is deliberately separate from ``washbasin``: a sink in
    # a kitchen and a basin in a bathroom point at different rooms, and CAD
    # libraries name them differently, so conflating them destroys evidence.
    ("sink", "kitchen_fixture", "", (
        r"KITCHEN ?SINK", r"\bSINK\b", r"\bSK\b(?! ?LIGHT)", r"DOUBLE ?BOWL",
    )),
    ("cooktop", "kitchen_fixture", "", (
        r"COOK ?TOP", r"\bHOB\b", r"\bSTOVE\b", r"\bRANGE\b(?! ?HOOD)",
        r"\bBURNER", r"\bCHULHA\b", r"GAS ?TOP",
    )),
    ("oven", "appliance", "oven", (r"\bOVEN\b", r"\bMICROWAVE\b(?! ?SHELF)", r"\bMWO\b")),
    ("range_hood", "kitchen_fixture", "", (
        r"RANGE ?HOOD", r"\bCHIMNEY\b", r"EXTRACT(OR)? ?HOOD", r"\bCOOKER ?HOOD\b",
    )),
    ("refrigerator", "appliance", "refrigerator", (
        r"REFRIGERAT", r"\bFRIDGE\b", r"\bFREEZER\b", r"\bREF\b(?! ?LINE)",
    )),
    ("dishwasher", "appliance", "", (r"DISH ?WASHER", r"\bDW\b(?! ?G)")),
    ("washing_machine", "appliance", "washing_machine", (
        r"WASHING ?MACHINE", r"\bWASHER\b", r"\bWM\b", r"\bLAUNDRY\b",
        r"CLOTHES ?DRYER", r"\bTUMBLE ?DRY",
    )),
    ("kitchen_counter", "casework", "kitchen_counter", (
        r"COUNTER ?TOP", r"\bCOUNTER\b", r"\bWORKTOP\b", r"PLATFORM",
        r"KITCHEN ?PLAT", r"\bSLAB ?UNIT\b",
    )),
    ("kitchen_island", "casework", "kitchen_island", (r"\bISLAND\b", r"KITCHEN ?IS")),
)

#: Furniture. Looser matching is acceptable here because a misread sofa costs
#: far less than a misread toilet.
FURNITURE_RULES: Tuple[_Rule, ...] = (
    ("bed", "furniture", "bed", (
        r"\bBED\b(?! ?ROOM)", r"DOUBLE ?BED", r"SINGLE ?BED", r"QUEEN ?BED",
        r"KING ?BED", r"\bBED ?[0-9]", r"\bCOT\b", r"\bBUNK\b", r"\bMATTRESS\b",
    )),
    ("bedside_table", "furniture", "bedside_table", (
        r"BED ?SIDE", r"NIGHT ?STAND", r"NIGHT ?TABLE", r"\bBST\b",
    )),
    ("wardrobe", "casework", "wardrobe", (
        r"WARD ?ROBE", r"\bWDR\b", r"\bALMIRAH\b", r"\bCLOSET\b", r"\bARMOIRE\b",
        r"\bCUPBOARD\b", r"\bCUPB\b", r"\bCLOS\b",
    )),
    ("dressing_table", "furniture", "console_table", (
        r"DRESS(ING)? ?TABLE", r"\bDRESSER\b", r"\bVANITY ?UNIT\b",
    )),
    ("sofa", "furniture", "sofa", (
        r"\bSOFA\b", r"\bCOUCH\b", r"\bSETTEE\b", r"\bLOVE ?SEAT\b",
    )),
    ("sectional", "furniture", "sectional", (r"SECTIONAL", r"\bL ?SHAPE ?SOFA\b")),
    ("armchair", "furniture", "armchair", (
        r"ARM ?CHAIR", r"\bRECLINER\b", r"EASY ?CHAIR", r"\bLOUNGER\b",
    )),
    ("coffee_table", "furniture", "coffee_table", (
        r"COFFEE ?TABLE", r"\bCTR ?TABLE\b", r"CENTRE ?TABLE", r"CENTER ?TABLE",
    )),
    ("dining_table", "furniture", "dining_table", (
        r"DINING ?TABLE", r"\bDIN ?TABLE\b", r"\bDINETTE\b", r"\bDT ?[0-9]",
    )),
    ("dining_chair", "furniture", "dining_chair", (
        r"DINING ?CHAIR", r"\bDIN ?CHAIR\b",
    )),
    ("office_chair", "furniture", "office_chair", (
        r"OFFICE ?CHAIR", r"SWIVEL ?CHAIR", r"TASK ?CHAIR", r"EXEC(UTIVE)? ?CHAIR",
    )),
    ("study_table", "furniture", "study_table", (
        r"STUDY ?TABLE", r"\bDESK\b", r"WORK ?STATION", r"WORK ?TABLE",
        r"COMPUTER ?TABLE", r"WRITING ?TABLE",
    )),
    ("bookshelf", "casework", "bookshelf", (
        r"BOOK ?SHELF", r"BOOK ?CASE", r"\bSHELV(ING|ES)\b", r"\bRACK\b",
    )),
    ("tv_unit", "furniture", "tv_unit", (
        r"\bTV ?UNIT\b", r"MEDIA ?(UNIT|CONSOLE)", r"\bTV ?CAB", r"ENTERTAIN",
    )),
    ("tv", "furniture", "tv", (r"\bTV\b", r"TELEVISION", r"\bLED ?TV\b")),
    ("side_table", "furniture", "side_table", (r"SIDE ?TABLE", r"\bEND ?TABLE\b")),
    ("chair", "furniture", "chair", (r"\bCHAIRS?\b", r"\bCHR\b", r"\bSEAT\b")),
    ("table", "furniture", "dining_table", (r"\bTABLES?\b", r"\bTBL\b")),
    ("stool", "furniture", "stool", (r"\bSTOOL\b", r"\bBAR ?STOOL\b")),
    ("sideboard", "casework", "sideboard", (
        r"SIDE ?BOARD", r"\bCREDENZA\b", r"\bBUFFET\b", r"CROCKERY",
    )),
    ("cabinet", "casework", "cabinet", (
        r"\bCABINET\b", r"\bCAB\b(?! ?LE)", r"\bSTORAGE\b", r"\bLOCKER\b",
    )),
    ("plant", "furniture", "plant", (r"\bPLANT\b", r"\bPLANTER\b", r"\bPOT\b", r"\bSHRUB\b")),
    ("rug", "furniture", "rug", (r"\bRUG\b", r"\bCARPET\b", r"\bMAT\b")),
    ("piano", "furniture", "", (r"\bPIANO\b", r"\bGRAND ?PIANO\b")),
    ("car", "furniture", "", (r"\bCARS?\b(?! ?PET)", r"\bVEHICLE\b", r"\bPARKING\b")),
)

#: Architectural elements and drawing furniture. Matched *before* the
#: furniture table, because a block called ``DOOR-SINGLE`` must resolve to a
#: door and never to a piece of casework.
ARCHITECTURAL_RULES: Tuple[_Rule, ...] = (
    ("north_arrow", "north_arrow", "", (
        r"\bNORTH\b", r"\bN ?ARROW\b", r"NORTH ?(ARROW|POINT|SIGN)",
        r"\bCOMPASS\b", r"\bNORD\b",
    )),
    ("title_block", "title_block", "", (
        r"TITLE ?BLOCK", r"\bTTLB\b", r"DRAWING ?(SHEET|BORDER)", r"\bA[0-4] ?SHEET\b",
    )),
    ("grid_bubble", "grid_bubble", "", (
        r"GRID ?(BUBBLE|HEAD|MARK)", r"\bCOL ?BUBBLE\b", r"\bDATUM\b",
    )),
    ("level_marker", "annotation", "", (
        r"LEVEL ?(MARK|TAG)", r"\bFFL\b", r"SPOT ?LEVEL", r"\bELEV ?MARK\b",
    )),
    ("section_marker", "annotation", "", (
        r"SECTION ?(MARK|ARROW|TAG)", r"\bDETAIL ?(MARK|BUBBLE)\b", r"\bCALL ?OUT\b",
    )),
    ("room_tag", "annotation", "", (
        r"ROOM ?(TAG|NAME|LABEL|STAMP)", r"\bRM ?TAG\b", r"SPACE ?TAG",
        r"\bAREA ?TAG\b",
    )),
    ("door", "door", "", (
        r"\bDOORS?\b", r"\bDR ?[0-9]", r"SINGLE ?DOOR", r"DOUBLE ?DOOR",
        r"SLIDING ?DOOR", r"\bD ?[0-9]{2}\b", r"\bSWING\b", r"FLUSH ?DOOR",
    )),
    ("window", "window", "", (
        r"\bWINDOWS?\b", r"\bWIN ?[0-9]", r"\bW ?[0-9]{2}\b", r"CASEMENT",
        r"\bGLAZING\b", r"\bSLIDING ?WIN", r"\bVENTILATOR\b", r"\bLOUVER",
    )),
    ("column", "column", "", (
        r"\bCOLUMNS?\b", r"\bCOL ?[0-9]", r"\bRCC ?COL", r"\bPILLAR\b",
    )),
    ("stair", "stair", "", (
        r"\bSTAIR", r"\bSTEPS?\b", r"\bFLIGHT\b", r"\bTREAD\b", r"\bLIFT\b",
        r"\bELEVATOR\b",
    )),
    ("light_fixture", "electrical", "", (
        r"\bLIGHT\b", r"\bLUMIN", r"\bLAMP\b", r"\bDOWN ?LIGHT\b", r"\bFAN\b",
        r"\bSWITCH\b", r"\bSOCKET\b", r"\bOUTLET\b", r"\bDB\b",
    )),
)

#: Whole table, in match order. Architecture first (so ``DOOR`` never becomes
#: furniture), then fixtures (whose evidence value is highest and whose
#: patterns are tightest), then general furniture.
_ORDERED_RULES: Tuple[_Rule, ...] = (
    ARCHITECTURAL_RULES + FIXTURE_RULES + FURNITURE_RULES
)

_COMPILED: Tuple[Tuple[str, str, str, Tuple[re.Pattern, ...]], ...] = tuple(
    (category, kind, catalog_category, tuple(re.compile(p) for p in patterns))
    for category, kind, catalog_category, patterns in _ORDERED_RULES
)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

#: Prefixes CAD libraries bolt onto every block name. Stripped so that
#: ``_ARCH_BED_QUEEN`` matches the same rule as ``BED``.
#:
#: AutoCAD's anonymous-block forms (``A$C1A2B3C4``, ``*U12``) are deliberately
#: *not* listed. They carry no name to recover, and a rule stripping them
#: cannot be written safely: ``[0-9A-F]+`` is greedy over hex digits and
#: ``BED``, ``ACE`` and ``FADE`` are all valid hex, so such a rule silently
#: eats the very word it was meant to expose.
_NOISE_PREFIXES = re.compile(
    r"^(?:_+|ACAD_|AEC_|ARCH_|BLK_|BLOCK_|DYN_|LIB_|STD_|M_|IMP_)+",
    re.IGNORECASE,
)

#: Anonymous blocks, which have no meaningful name at all.
_ANONYMOUS = re.compile(r"^(?:\*[A-Z][0-9]*|A\$[A-Z][0-9A-F]{4,})$", re.IGNORECASE)

#: Trailing instance numbering and revision suffixes: ``SOFA-01``, ``WC_A``.
_NOISE_SUFFIXES = re.compile(r"(?:[ _-]+(?:[0-9]{1,3}|[A-Z]|REV[0-9]*|NEW|EXIST(ING)?))+$")


def normalise_block_name(name: str) -> str:
    """Uppercase and strip library noise, keeping words separated by spaces.

    Separators become spaces (rather than being deleted) so ``\\b`` anchors in
    the rule patterns behave: deleting them would turn ``TV_UNIT`` into
    ``TVUNIT`` and stop ``\\bTV\\b`` matching anything sensible.
    """
    if not name:
        return ""
    text = name.strip().upper()
    if _ANONYMOUS.match(text):
        return ""
    text = _NOISE_PREFIXES.sub("", text)
    text = re.sub(r"[-_./|+]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

#: Attribute tags that, when present, name the room a block sits in. Checked
#: before the block name itself, since an explicit attribute is a stronger and
#: more current statement than the name of a reused library block.
ROOM_NAME_ATTRIBUTES = (
    "ROOM", "ROOM_NAME", "ROOMNAME", "RM_NAME", "NAME", "SPACE",
    "SPACE_NAME", "ROOM_TYPE", "USE", "FUNCTION", "LABEL", "DESCRIPTION",
)

#: Attribute tags carrying a room's area.
AREA_ATTRIBUTES = ("AREA", "ROOM_AREA", "SQ_M", "SQM", "SQFT", "SQ_FT", "SIZE")


def classify_block(
    name: str, attributes: Optional[Dict[str, str]] = None
) -> BlockClassification:
    """Resolve a block name to a semantic category.

    ``attributes`` are consulted only for hints the name cannot give (a
    ``TYPE`` or ``CATEGORY`` tag); room naming is handled separately by
    ``room_name_from_attributes`` because it describes the *room*, not the
    block.
    """
    normalised = normalise_block_name(name)
    if not normalised:
        return BlockClassification("", "unknown", 0.0, "empty block name")

    # An explicit type attribute overrides a generic library name.
    for tag in ("TYPE", "CATEGORY", "FAMILY", "SUBTYPE"):
        value = _lookup(attributes, tag)
        if value:
            hit = _match(normalise_block_name(value))
            if hit is not None:
                category, kind, catalog_category, matched = hit
                return BlockClassification(
                    category=category, kind=kind, confidence=0.9,
                    reason=f"block attribute {tag}={value!r} matched {matched!r}",
                    catalog_category=catalog_category,
                )

    hit = _match(normalised)
    if hit is None:
        return BlockClassification(
            "", "unknown", 0.0, f"block name {name!r} matched no known category"
        )

    category, kind, catalog_category, matched = hit

    # Fixtures drive room classification, so their confidence is stated
    # separately and higher: the patterns are tighter and the consequence of a
    # match is larger.
    confidence = 0.92 if kind in ("plumbing_fixture", "kitchen_fixture") else 0.85

    return BlockClassification(
        category=category,
        kind=kind,
        confidence=confidence,
        reason=f"block name {name!r} matched {matched!r}",
        catalog_category=catalog_category,
    )


def _match(normalised: str):
    """First matching rule, or ``None``."""
    for category, kind, catalog_category, patterns in _COMPILED:
        for pattern in patterns:
            match = pattern.search(normalised)
            if match:
                return category, kind, catalog_category, match.group(0).strip()
    return None


def _lookup(attributes: Optional[Dict[str, str]], tag: str) -> str:
    """Case-insensitive attribute lookup."""
    if not attributes:
        return ""
    upper = tag.upper()
    for key, value in attributes.items():
        if key.upper() == upper:
            return str(value).strip()
    return ""


def room_name_from_attributes(attributes: Optional[Dict[str, str]]) -> str:
    """The room name a block's attributes declare, if any.

    A room tag block with ``ROOM_NAME="MASTER BEDROOM"`` is trust tier 1: the
    drawing carries structured metadata naming the space, which beats parsing
    a free-floating text string that merely happens to sit inside the polygon.
    """
    if not attributes:
        return ""
    for tag in ROOM_NAME_ATTRIBUTES:
        value = _lookup(attributes, tag)
        if value and not value.isdigit():
            return value
    return ""


def area_from_attributes(attributes: Optional[Dict[str, str]]) -> Optional[float]:
    """A numeric area declared in block attributes, in the drawing's own unit."""
    if not attributes:
        return None
    for tag in AREA_ATTRIBUTES:
        value = _lookup(attributes, tag)
        if not value:
            continue
        match = re.search(r"[0-9]+(?:\.[0-9]+)?", value.replace(",", ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                continue
    return None


#: Categories whose presence is decisive evidence for a room type. Consumed by
#: ``semantic.taxonomy``; defined here so the vocabulary has one owner.
FIXTURE_CATEGORIES = tuple(rule[0] for rule in FIXTURE_RULES)
FURNITURE_CATEGORIES = tuple(rule[0] for rule in FURNITURE_RULES)
ALL_CATEGORIES = tuple(rule[0] for rule in _ORDERED_RULES)


def summarise(classifications: Sequence[BlockClassification]) -> Dict[str, int]:
    """Category histogram, for diagnostics."""
    histogram: Dict[str, int] = {}
    for classification in classifications:
        if classification.category:
            histogram[classification.category] = (
                histogram.get(classification.category, 0) + 1
            )
    return dict(sorted(histogram.items(), key=lambda kv: (-kv[1], kv[0])))
