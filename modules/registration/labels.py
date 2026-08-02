"""
ArchX3D — Label anchors and candidate correspondences
=====================================================
The evidence layer. Turns "words printed in the drawing" and "words read off
the image" into pairs a transform can be fitted to.

Why room labels are the right anchor
------------------------------------
Registering two views of a building means finding the same *thing* in both.
The obvious candidates are corners, wall junctions and door openings — and
every one of them is repeated dozens of times in a floor plan and is
essentially featureless, so matching them is a combinatorial mess with no
distinguishing information to break ties.

Room labels are the opposite. They are sparse, they are textual, and they are
*already extracted on both sides*: ``cad.text`` parses them out of the DXF with
plan coordinates, and a vision model reads them off the image with pixel
coordinates. The architect printed the correspondence into the drawing; the
job is only to notice it.

Matching is deliberately generous, because deciding which pairing is real is
the consensus fit's job, not this module's. A drawing with three bedrooms
generates a candidate against all three; exactly one survives geometry.

Design constraints
------------------
* **Stdlib only**, and the CAD adapter is the one function that imports CAD
  types — the same split ``semantic`` uses, so the fitting machinery stays
  testable from hand-built anchors.
* **Normalisation is duplicated from ``cad.text``, not imported.** The rules
  must stay identical for matching to work, so they are re-stated here with a
  test asserting the two agree, rather than importing across a package
  boundary that is meant to stay severable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Sequence, Tuple

from .schema import Correspondence

Point = Tuple[float, float]

#: Below this text similarity, two labels are not the same room.
MIN_TEXT_SIMILARITY = 0.62

#: A label matched by containment or fuzz, not equality, is worth less.
CONTAINMENT_WEIGHT = 0.75
ROOM_TYPE_WEIGHT = 0.5

#: Most candidates a single image label may generate. A plan with eight
#: identical "BEDROOM" labels is fine; one with eighty is a schedule table,
#: and enumerating pairs over it would swamp the consensus fit.
MAX_CANDIDATES_PER_LABEL = 8

#: Strings that are printed on drawings but say nothing about position.
_UNINFORMATIVE = {
    "", "N", "S", "E", "W", "UP", "DN", "DOWN", "REF", "TYP", "NTS", "SCALE",
    "NORTH", "PLAN", "FLOOR PLAN", "GROUND FLOOR", "FIRST FLOOR", "LEVEL",
    "A", "B", "C", "D", "1", "2", "3", "4",
}

#: Title-block and general-note boilerplate. These *are* printed on the sheet
#: and they *do* have positions, so they look like usable anchors — but the
#: title block is laid out per sheet, not per building. In the test fixture it
#: sits far outside the plan extent entirely. Matching one would drag the fit
#: away from the drawing it is supposed to be registering.
_BOILERPLATE = (
    "SCALE", "DRAWN BY", "CHECKED", "DIMENSIONS IN", "FLOOR PLAN", "SITE PLAN",
    "DRAWING NO", "DWG NO", "SHEET", "REVISION", "CLIENT", "PROJECT", "TITLE",
    "DO NOT SCALE", "ALL LEVELS", "NOTES",
)

#: Mirrors ``cad.text._MTEXT_CODES``. The general form requires a terminating
#: semicolon; paragraph and column breaks do not. Getting this wrong makes
#: ``DRAWING\PPUJA`` swallow the second room name instead of splitting on it.
_MTEXT_CODES = re.compile(
    r"\\[A-Za-z][^;\\]*;"      # \f..., \H..., \pxi...;
    r"|\\[PXpx~]"              # paragraph / column breaks
    r"|[{}]"                   # grouping braces
    r"|%%[udcpo]",             # %%u underline, %%d degree, %%c diameter
    re.IGNORECASE,
)

#: A trailing area or dimension annotation, on an already-normalised string.
#: Real drawings write the room name and its area as one piece of text —
#: ``KITCHEN\P12.00 SQ.M.`` — while the sheet may print only the name, so the
#: two forms have to be reduced to a common key before they can be compared.
#:
#: The numeric run is capped at two tokens because normalisation splits
#: ``16.00`` into ``16 00``. Allowing more would let ``BEDROOM 2 16 00 SQ M``
#: match greedily from the ``2`` and strip the room's own number.
_MEASUREMENT_TAIL = re.compile(
    r"\s+\d+(?:\s+\d+)?\s+(?:SQ\s+(?:M|FT)|SQM|SQFT|SFT|M2|MT|MM)\s*$"
)


def normalise(raw: str) -> str:
    """Uppercase, de-punctuate and collapse.

    Mirrors ``cad.text.normalise`` exactly — full stops become spaces so
    ``M.BED.RM`` and ``W.C.`` normalise to ``M BED RM`` and ``W C``. The two
    implementations must not drift; ``tests/test_registration.py`` asserts
    they agree on a corpus of real label spellings.
    """
    if not raw:
        return ""
    text = _MTEXT_CODES.sub(" ", raw)
    text = text.replace("\\~", " ").replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip().upper()
    text = re.sub(r"[._/\\|,;:*#()\[\]{}\"']+", " ", text)
    text = re.sub(r"[-–—]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_measurements(normalised: str) -> str:
    """Drop a trailing area annotation, leaving the room's name.

    ``BATH 8 64 SQ M`` → ``BATH``; ``BEDROOM 2 16 00 SQ M`` → ``BEDROOM 2``.
    The room number survives because it is what distinguishes one bedroom from
    the next, and losing it would turn an exact match into a three-way tie.
    """
    stripped = _MEASUREMENT_TAIL.sub("", normalised).strip()
    return stripped or normalised


def _known(room_type: str) -> str:
    """A room type, or ``""`` for the absent/unresolved sentinels.

    ``"unknown"`` is what both the CAD parser and the vision parser emit when
    they could not resolve a string. Treating it as a value makes every
    unresolved label "agree" with every other unresolved label, which
    manufactures a candidate correspondence between every pair of them.
    """
    value = (room_type or "").strip().lower()
    return "" if value in ("", "unknown", "none") else value


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


@dataclass
class LabelAnchor:
    """A piece of text with a position, from either side of the match.

    ``point`` is plan metres for a CAD anchor and normalised image ``(u, v)``
    for an image anchor. The type deliberately does not distinguish them: the
    whole purpose of the fit is to discover the map between those two spaces,
    so a shared container keeps the matching code from needing to care.
    """

    text: str = ""
    normalised: str = ""
    point: Point = (0.0, 0.0)
    uid: str = ""
    #: Room type parsed from the string, when either side resolved one.
    room_type: str = ""
    #: How sure the producing side is that it read this correctly.
    confidence: float = 1.0
    #: room_label | note | attribute — room labels are trusted further.
    kind: str = "room_label"

    @property
    def match_key(self) -> str:
        """The form two anchors are actually compared on.

        A drawing writes ``KITCHEN 12.00 SQ.M.`` where the sheet may print
        only ``KITCHEN``. Comparing the raw normalised strings would score
        that pair at a fuzzy 0.6 instead of an exact 1.0, and on a plan with
        several similar names the difference decides the fit.
        """
        return strip_measurements(self.normalised)

    @property
    def informative(self) -> bool:
        """False for strings that appear on every drawing and locate nothing."""
        key = self.match_key
        if key in _UNINFORMATIVE or len(key) < 2:
            return False
        if any(phrase in key for phrase in _BOILERPLATE):
            return False
        # A bare number is a dimension, a door tag or a grid bubble.
        return not re.fullmatch(r"[0-9 .,'\"-]+", key)


def anchors_from_cad(document: Any) -> List[LabelAnchor]:
    """Label anchors from a ``cad.CadDocument``, in plan metres.

    Room labels first, then any other text that carries a usable string. A
    note reading ``ENTRY`` that the vocabulary never resolved to a room type
    is still a perfectly good positional anchor — it is printed in the image
    too — so it is kept at a lower weight rather than discarded.
    """
    if document is None:
        return []

    anchors: List[LabelAnchor] = []
    seen: set = set()

    for text in getattr(document, "texts", []) or []:
        # Re-derived rather than read off the record, for the same reason as
        # on the image side: matching only works if both sides used identical
        # rules, and this module owns that guarantee.
        raw = getattr(text, "text", "")
        normalised = normalise(raw) or getattr(text, "normalised", "")
        role = getattr(text, "role", "note")

        # Dimensions and area callouts move with the drawing but are printed
        # in a dozen near-identical forms; they generate candidates that are
        # numerically ambiguous and add nothing a room label does not.
        if role in ("dimension", "area"):
            continue

        anchor = LabelAnchor(
            text=raw,
            normalised=normalised,
            point=tuple(getattr(text, "insert", (0.0, 0.0))),
            uid=getattr(text, "uid", ""),
            room_type=_known(getattr(text, "room_type", "")),
            confidence=float(getattr(text, "confidence", 0.0) or 0.0),
            kind="room_label" if role == "room_label" else "note",
        )
        if not anchor.informative:
            continue

        # Two texts at the same spot with the same string are one anchor.
        key = (anchor.match_key, round(anchor.point[0], 3), round(anchor.point[1], 3))
        if key in seen:
            continue
        seen.add(key)
        anchors.append(anchor)

    return anchors


def anchors_from_observation(observation: Any) -> List[LabelAnchor]:
    """Label anchors read off one image, in normalised image coordinates.

    Reads ``observation.labels`` — the ``TextObservation`` records the plan
    prompt asks for. An observation from a model that ignored the request, or
    from a cached response predating it, simply yields nothing, and the
    engine falls back accordingly.
    """
    anchors: List[LabelAnchor] = []

    for index, label in enumerate(getattr(observation, "labels", []) or []):
        text = getattr(label, "text", "") or ""
        # Re-normalised here rather than trusted from the producer. Matching
        # only works if both sides used identical rules, and this module is
        # where that guarantee has to live — a caller that normalised its own
        # way must not be able to break the match by doing so.
        normalised = normalise(text) or getattr(label, "normalised", "")
        bbox = getattr(label, "bbox", None)
        if bbox is None:
            continue

        anchor = LabelAnchor(
            text=text,
            normalised=normalised,
            point=tuple(bbox.center),
            uid=getattr(label, "local_id", "") or f"label_{index}",
            room_type=_known(getattr(label, "room_type", "")),
            confidence=float(getattr(label, "confidence", 0.0) or 0.0),
            kind="room_label",
        )
        if anchor.informative:
            anchors.append(anchor)

    return anchors


# ---------------------------------------------------------------------------
# Text similarity
# ---------------------------------------------------------------------------


def text_similarity(a: str, b: str) -> float:
    """How likely two normalised strings name the same room, in [0, 1].

    Three rules, strongest first. Equality is certain. Containment catches the
    common case where the drawing says ``MASTER BEDROOM`` and the sheet prints
    ``BEDROOM``, or vice versa. Bigram overlap catches the model misreading a
    character or two at sheet resolution, which is routine — a label six
    millimetres tall on an A1 sheet is a handful of pixels.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    tokens_a, tokens_b = set(a.split()), set(b.split())
    if tokens_a and tokens_b and (tokens_a <= tokens_b or tokens_b <= tokens_a):
        return CONTAINMENT_WEIGHT

    dice = _bigram_dice(a, b)
    return dice if dice >= MIN_TEXT_SIMILARITY else 0.0


def _bigram_dice(a: str, b: str) -> float:
    """Sørensen–Dice coefficient over character bigrams.

    Chosen over edit distance because it is order-tolerant and costs a set
    intersection rather than a matrix: ``BED ROOM 1`` and ``BEDROOM 1`` score
    highly, which is exactly the whitespace variation drawings are full of.
    """
    grams_a = _bigrams(a)
    grams_b = _bigrams(b)
    if not grams_a or not grams_b:
        return 0.0
    overlap = len(grams_a & grams_b)
    return 2.0 * overlap / (len(grams_a) + len(grams_b))


def _bigrams(text: str) -> set:
    squashed = text.replace(" ", "")
    return {squashed[i:i + 2] for i in range(len(squashed) - 1)}


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------


def candidates(
    image_anchors: Sequence[LabelAnchor],
    cad_anchors: Sequence[LabelAnchor],
) -> List[Correspondence]:
    """Every plausible pairing of an image label with a CAD label.

    Generated liberally on purpose. The geometry has not been consulted yet,
    so a plan with three bedrooms genuinely does admit three readings of one
    ``BEDROOM`` on the sheet; narrowing that here would mean guessing with
    less information than the consensus fit will have.
    """
    pairs: List[Correspondence] = []

    for image_anchor in image_anchors:
        scored: List[Tuple[float, LabelAnchor]] = []
        image_key = image_anchor.match_key
        image_room_type = _known(image_anchor.room_type)

        for cad_anchor in cad_anchors:
            weight = text_similarity(image_key, cad_anchor.match_key)

            if not weight and image_room_type and _known(cad_anchor.room_type):
                # Different words, same meaning: the drawing says "W C" and
                # the sheet prints "TOILET". Only trusted when both sides
                # independently resolved a *named* room type — the "unknown"
                # sentinel is an absence of information, and letting it match
                # itself would pair every unresolved label with every other.
                if image_room_type == _known(cad_anchor.room_type):
                    weight = ROOM_TYPE_WEIGHT

            if weight <= 0.0:
                continue

            # A note is a weaker anchor than a parsed room label, and a label
            # either side read with low confidence is weaker still.
            if cad_anchor.kind != "room_label":
                weight *= 0.8
            weight *= 0.5 + 0.5 * _clamp01(
                min(image_anchor.confidence or 1.0, cad_anchor.confidence or 1.0)
            )

            scored.append((weight, cad_anchor))

        scored.sort(key=lambda pair: -pair[0])
        for weight, cad_anchor in scored[:MAX_CANDIDATES_PER_LABEL]:
            pairs.append(
                Correspondence(
                    text=image_key,
                    image_uv=image_anchor.point,
                    plan_xy=cad_anchor.point,
                    cad_uid=cad_anchor.uid,
                    image_label_id=image_anchor.uid,
                    weight=round(weight, 4),
                )
            )

    return pairs


def unmatched(
    anchors: Sequence[LabelAnchor], correspondences: Sequence[Correspondence], side: str
) -> List[str]:
    """Labels on one side that no accepted correspondence used.

    On the image side this is the multi-floor tell: a sheet showing two floors
    registers cleanly against one of them and leaves the other floor's room
    names sitting here, unexplained.
    """
    used = {
        c.image_label_id if side == "image" else c.cad_uid
        for c in correspondences
        if c.inlier
    }
    return sorted({a.match_key for a in anchors if a.uid not in used})


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))
