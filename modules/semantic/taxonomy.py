"""
ArchX3D — Room Taxonomy
=======================
The priors every room-classification signal scores against.

Why the numbers are here and not inline
---------------------------------------
Room classification combines a dozen signals. If each signal carries its own
private idea of "how big a bathroom is", the system has a dozen inconsistent
models of a bathroom and no way to correct any of them. Every prior therefore
lives in this one table, and each signal reads from it.

Everything below is a **log-likelihood contribution**, not a probability.
Contributions from independent signals add, which is what lets four weak
agreeing signals outweigh one moderate disagreeing one. The scale is
deliberately interpretable:

    +4.0   decisive          — a toilet means bathroom
    +2.5   strong            — a sofa means living room
    +1.0   supporting        — this area suits a bedroom
    +0.3   weak              — this room has two windows
     0.0   uninformative
    -2.0   contradicting     — a toilet is not in a kitchen

Calibration is on residential floor plans (the project's target), with ranges
taken from Neufert's *Architects' Data* and common residential practice
rather than invented. They are wide on purpose: a prior that excludes an
unusual-but-real room is worse than one that merely prefers the typical.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Room types
# ---------------------------------------------------------------------------

#: Canonical room types. A superset of ``vision.catalog.ROOM_TYPES``: a plan
#: labels utility rooms, stores and shafts, and identifying them correctly
#: matters even though no interior style prior exists for them yet.
ROOM_TYPES: Tuple[str, ...] = (
    "living_room", "bedroom", "kitchen", "dining_room", "bathroom",
    "office", "hallway", "balcony", "utility", "store", "garage",
    "staircase", "shaft", "studio", "unknown",
)

#: Types the vision catalog knows how to style. Others fall back to the
#: dominant scene style when furnishing.
STYLEABLE_ROOM_TYPES = (
    "living_room", "bedroom", "kitchen", "dining_room", "bathroom",
    "office", "hallway", "studio", "balcony",
)


@dataclass(frozen=True)
class RoomPrior:
    """Everything known a priori about one room type."""

    room_type: str

    #: Plausible floor area range in m². Outside this, area evidence turns
    #: negative rather than merely absent.
    area_range: Tuple[float, float]
    #: The most common area, used as the peak of the plausibility curve.
    area_typical: float

    #: Largest length:width ratio that is still normal for this type.
    #: Hallways are exempt in practice — theirs is set high deliberately.
    aspect_max: float = 2.5

    #: Typical count of doors and windows. Used as soft expectations.
    doors_typical: int = 1
    windows_typical: int = 1

    #: How normal it is for this room to have no window at all, 0 to 1.
    #:
    #: Graded rather than a ``requires_window`` / ``forbids_window`` pair,
    #: because that binary could not express the most common case in an
    #: apartment: an internal bathroom is entirely normal, but "forbids" is
    #: wrong (many have windows) and "requires" is wrong too. Scoring it as
    #: neither gave windowless bathrooms *no* credit while handing stores a
    #: bonus, so a windowless room off a bedroom read as a cupboard rather
    #: than as the en-suite it almost always is.
    windowless_normal: float = 0.3

    #: How private the room is, 0 (public, near the entrance) to 1 (most
    #: private, deepest in the plan). Compared against graph depth.
    privacy: float = 0.5

    #: Room types this one is commonly adjacent to, and the strength of that
    #: expectation. An en-suite bathroom beside a bedroom is the canonical case.
    adjacency: Dict[str, float] = field(default_factory=dict)

    #: True when several instances in one plan are normal. Prevents the
    #: classifier from suppressing a second bedroom just because it found one.
    repeatable: bool = True

    def area_score(self, area: float) -> float:
        """Log-likelihood contribution of ``area`` for this room type.

        A plateau inside the plausible range with smooth exponential decay
        outside it. A hard cut-off would make a 31 m² bedroom impossible
        rather than merely unusual, which is not how buildings work.
        """
        low, high = self.area_range
        if low <= area <= high:
            # Peak at the typical area, tapering to +0.4 at the range edges.
            span = max(high - low, 1e-6)
            distance = abs(area - self.area_typical) / span
            return 1.2 * math.exp(-2.0 * distance * distance) + 0.3

        # Outside the range: decay over a characteristic width of 40% of the
        # nearer bound, bottoming out at -2.5 so one odd area cannot alone
        # veto a type that other evidence strongly supports.
        if area < low:
            excess = (low - area) / max(low * 0.4, 1e-6)
        else:
            excess = (area - high) / max(high * 0.4, 1e-6)
        return max(-2.5, -1.2 * excess)

    def aspect_score(self, aspect: float) -> float:
        """Contribution of the room's elongation."""
        if aspect <= self.aspect_max:
            return 0.25
        excess = (aspect - self.aspect_max) / max(self.aspect_max, 1e-6)
        return max(-2.0, -1.0 * excess)


#: The prior table.
#:
#: Areas are habitable floor area in m². Sources: Neufert *Architects' Data*
#: (4th ed.) space standards, UK Nationally Described Space Standard, and
#: Indian NBC residential minimums — the project's plans are predominantly
#: residential, and these three agree closely at the low end.
ROOM_PRIORS: Dict[str, RoomPrior] = {
    "living_room": RoomPrior(
        "living_room", area_range=(12.0, 70.0), area_typical=24.0, aspect_max=2.2,
        doors_typical=2, windows_typical=2, windowless_normal=0.05, privacy=0.2,
        adjacency={"dining_room": 1.0, "hallway": 0.8, "kitchen": 0.6, "balcony": 0.5},
        repeatable=False,
    ),
    "bedroom": RoomPrior(
        "bedroom", area_range=(7.5, 32.0), area_typical=13.0, aspect_max=2.0,
        doors_typical=1, windows_typical=1, windowless_normal=0.05, privacy=0.9,
        adjacency={"bathroom": 1.0, "hallway": 0.8, "balcony": 0.4},
    ),
    "kitchen": RoomPrior(
        "kitchen", area_range=(4.0, 25.0), area_typical=10.0, aspect_max=3.0,
        doors_typical=1, windows_typical=1, windowless_normal=0.3, privacy=0.4,
        adjacency={"dining_room": 1.0, "utility": 0.8, "living_room": 0.6,
                   "balcony": 0.4},
        repeatable=False,
    ),
    "dining_room": RoomPrior(
        "dining_room", area_range=(7.0, 35.0), area_typical=15.0, aspect_max=2.2,
        doors_typical=2, windows_typical=1, windowless_normal=0.15, privacy=0.3,
        adjacency={"kitchen": 1.0, "living_room": 1.0},
        repeatable=False,
    ),
    "bathroom": RoomPrior(
        "bathroom", area_range=(1.8, 12.0), area_typical=4.5, aspect_max=2.6,
        doors_typical=1, windows_typical=0, windowless_normal=0.75, privacy=0.95,
        adjacency={"bedroom": 1.0, "hallway": 0.7},
    ),
    "office": RoomPrior(
        "office", area_range=(5.0, 25.0), area_typical=11.0, aspect_max=2.2,
        doors_typical=1, windows_typical=1, windowless_normal=0.1, privacy=0.7,
        adjacency={"hallway": 0.7, "living_room": 0.4},
    ),
    "hallway": RoomPrior(
        # Elongation is a hallway's defining geometric property, so its
        # aspect ceiling is set far above every other type. This is the single
        # most useful geometric discriminator in the whole table.
        "hallway", area_range=(1.5, 30.0), area_typical=7.0, aspect_max=8.0,
        doors_typical=4, windows_typical=0, windowless_normal=0.75, privacy=0.3,
        adjacency={"bedroom": 1.0, "bathroom": 0.8, "living_room": 0.8},
    ),
    "balcony": RoomPrior(
        "balcony", area_range=(1.5, 25.0), area_typical=6.0, aspect_max=5.0,
        doors_typical=1, windows_typical=0, windowless_normal=0.2, privacy=0.5,
        adjacency={"living_room": 0.8, "bedroom": 0.6, "kitchen": 0.4},
    ),
    "utility": RoomPrior(
        "utility", area_range=(1.5, 14.0), area_typical=5.0, aspect_max=3.0,
        doors_typical=1, windows_typical=0, windowless_normal=0.8, privacy=0.7,
        adjacency={"kitchen": 1.0, "hallway": 0.5},
    ),
    "store": RoomPrior(
        "store", area_range=(0.8, 12.0), area_typical=3.5, aspect_max=3.5,
        doors_typical=1, windows_typical=0, windowless_normal=0.9, privacy=0.8,
        adjacency={"hallway": 0.8, "bedroom": 0.5, "kitchen": 0.4},
    ),
    "garage": RoomPrior(
        "garage", area_range=(12.0, 60.0), area_typical=20.0, aspect_max=2.5,
        doors_typical=2, windows_typical=0, windowless_normal=0.7, privacy=0.2,
        adjacency={"hallway": 0.6, "utility": 0.5},
    ),
    "staircase": RoomPrior(
        "staircase", area_range=(2.0, 20.0), area_typical=7.0, aspect_max=3.5,
        doors_typical=2, windows_typical=0, windowless_normal=0.55, privacy=0.3,
        adjacency={"hallway": 1.0},
    ),
    "shaft": RoomPrior(
        "shaft", area_range=(0.3, 6.0), area_typical=1.2, aspect_max=4.0,
        doors_typical=0, windows_typical=0, windowless_normal=0.95, privacy=0.5,
        adjacency={},
    ),
    "studio": RoomPrior(
        "studio", area_range=(15.0, 60.0), area_typical=30.0, aspect_max=2.5,
        doors_typical=1, windows_typical=2, windowless_normal=0.05, privacy=0.5,
        adjacency={"hallway": 0.6, "balcony": 0.4},
        repeatable=False,
    ),
}


def prior(room_type: str) -> Optional[RoomPrior]:
    return ROOM_PRIORS.get(room_type)


def scoreable_types() -> Tuple[str, ...]:
    """Types the classifier competes between; excludes ``unknown``."""
    return tuple(ROOM_PRIORS.keys())


# ---------------------------------------------------------------------------
# Object evidence
# ---------------------------------------------------------------------------
#
# What the presence of one object category says about a room's type, as a
# log-likelihood contribution per type. Categories are ``cad.blocks``
# vocabulary terms, which the vision layer's categories map onto.
#
# Negative entries matter as much as positive ones. Without them a bathroom
# with a stored washing machine can drift toward "utility" purely because the
# utility hypothesis gained points and nothing pushed back on it.

OBJECT_EVIDENCE: Dict[str, Dict[str, float]] = {
    # ---- Sanitary: the most decisive evidence in a residential plan. -----
    "toilet": {"bathroom": 4.2, "utility": -1.0, "store": -2.5, "kitchen": -3.0,
               "bedroom": -3.0, "living_room": -3.5, "dining_room": -3.5},
    "urinal": {"bathroom": 4.0, "store": -2.5, "kitchen": -3.0, "bedroom": -3.0},
    "bidet": {"bathroom": 3.8, "store": -2.0, "kitchen": -2.5},
    "bathtub": {"bathroom": 4.0, "store": -2.0, "kitchen": -3.0, "bedroom": -2.0},
    "shower": {"bathroom": 3.8, "utility": 0.3, "store": -1.8, "kitchen": -2.5},
    "washbasin": {"bathroom": 2.6, "utility": 0.8, "kitchen": 0.4,
                  "living_room": -1.0, "bedroom": -0.8},
    "floor_drain": {"bathroom": 1.6, "utility": 1.2, "balcony": 0.6,
                    "living_room": -1.2, "bedroom": -1.5},
    "water_heater": {"bathroom": 1.6, "utility": 1.8, "kitchen": 0.8,
                     "living_room": -1.5, "bedroom": -1.5},

    # ---- Kitchen --------------------------------------------------------
    "sink": {"kitchen": 3.2, "utility": 1.4, "bathroom": 0.4,
             "bedroom": -2.0, "living_room": -2.0},
    "cooktop": {"kitchen": 4.2, "utility": 0.4, "bedroom": -3.5,
                "bathroom": -3.5, "living_room": -3.0},
    "oven": {"kitchen": 3.4, "utility": 0.4, "bedroom": -2.5, "bathroom": -2.5},
    "range_hood": {"kitchen": 3.6, "utility": 0.3, "bedroom": -2.5},
    "refrigerator": {"kitchen": 3.2, "utility": 0.8, "dining_room": 0.4,
                     "bathroom": -2.5, "bedroom": -1.5},
    "dishwasher": {"kitchen": 3.0, "utility": 1.0, "bedroom": -2.0},
    "kitchen_counter": {"kitchen": 3.0, "utility": 0.8, "bathroom": -0.5,
                        "bedroom": -1.5},
    "kitchen_island": {"kitchen": 3.4, "dining_room": 0.5, "bedroom": -2.0},
    "washing_machine": {"utility": 3.0, "bathroom": 1.4, "kitchen": 1.0,
                        "living_room": -1.5, "bedroom": -1.5},

    # ---- Sleeping -------------------------------------------------------
    "bed": {"bedroom": 4.2, "studio": 1.2, "living_room": -2.0,
            "kitchen": -3.5, "bathroom": -3.5, "hallway": -2.5},
    "bedside_table": {"bedroom": 3.0, "studio": 0.8, "kitchen": -2.0,
                      "bathroom": -2.0},
    "wardrobe": {"bedroom": 2.2, "store": 1.2, "bathroom": -0.5, "kitchen": -1.5},
    "dressing_table": {"bedroom": 2.0, "bathroom": 0.6, "kitchen": -1.5},

    # ---- Living ---------------------------------------------------------
    "sofa": {"living_room": 3.0, "studio": 1.0, "office": 0.4,
             "bathroom": -3.0, "kitchen": -2.0, "bedroom": -0.5},
    "sectional": {"living_room": 3.2, "studio": 1.0, "bathroom": -3.0,
                  "kitchen": -2.0},
    "armchair": {"living_room": 1.8, "office": 0.6, "bedroom": 0.4,
                 "bathroom": -2.0},
    "coffee_table": {"living_room": 2.6, "studio": 0.8, "bathroom": -2.5,
                     "kitchen": -1.5},
    "tv_unit": {"living_room": 2.4, "bedroom": 0.6, "bathroom": -2.5},
    "tv": {"living_room": 1.6, "bedroom": 0.8, "kitchen": -0.5, "bathroom": -2.0},

    # ---- Dining ---------------------------------------------------------
    "dining_table": {"dining_room": 3.0, "kitchen": 1.0, "living_room": 0.8,
                     "bathroom": -3.0, "bedroom": -1.5},
    "dining_chair": {"dining_room": 2.0, "kitchen": 0.8, "living_room": 0.4,
                     "bathroom": -2.5, "bedroom": -1.0},
    "sideboard": {"dining_room": 1.6, "living_room": 0.8, "kitchen": 0.6},

    # ---- Work -----------------------------------------------------------
    "study_table": {"office": 2.8, "bedroom": 0.8, "living_room": 0.3,
                    "bathroom": -2.5, "kitchen": -1.5},
    "office_chair": {"office": 2.4, "bedroom": 0.6, "bathroom": -2.5},
    "bookshelf": {"office": 1.8, "living_room": 0.8, "bedroom": 0.6,
                  "bathroom": -1.5},

    # ---- Generic: weak on purpose. A "TABLE" block is nearly contentless
    # ---- and must not out-vote a fixture.
    "chair": {"dining_room": 0.5, "office": 0.4, "living_room": 0.3,
              "bathroom": -1.0},
    "table": {"dining_room": 0.6, "living_room": 0.4, "office": 0.3,
              "bathroom": -1.0},
    "stool": {"kitchen": 0.5, "dining_room": 0.4, "bathroom": 0.2},
    "cabinet": {"store": 0.6, "kitchen": 0.5, "utility": 0.4},
    "plant": {"living_room": 0.4, "balcony": 0.8, "bathroom": -0.3},
    "rug": {"living_room": 0.8, "bedroom": 0.6, "bathroom": -0.8, "kitchen": -0.8},

    # ---- Strongly place-bound -------------------------------------------
    "car": {"garage": 4.5, "living_room": -4.0, "bedroom": -4.0,
            "bathroom": -4.0, "kitchen": -4.0},
    "piano": {"living_room": 2.0, "studio": 1.0, "bathroom": -3.0},
}


def object_evidence(category: str) -> Dict[str, float]:
    """Log-likelihood contributions of one object category, by room type."""
    return OBJECT_EVIDENCE.get(category, {})


#: Categories whose presence is on its own near-conclusive. Used to mark
#: evidence as decisive so it is reported as a headline reason rather than
#: buried among a dozen weak signals.
DECISIVE_CATEGORIES = (
    "toilet", "urinal", "bidet", "bathtub", "shower",
    "cooktop", "sink", "range_hood", "refrigerator",
    "bed", "sofa", "sectional", "dining_table", "car",
)


# ---------------------------------------------------------------------------
# Layer evidence
# ---------------------------------------------------------------------------

#: What a *layer role* observed inside a region says about its type. Weaker
#: than a block, because a layer says a kind of thing is present without
#: saying exactly what or precisely where.
LAYER_ROLE_EVIDENCE: Dict[str, Dict[str, float]] = {
    "plumbing_fixture": {"bathroom": 1.8, "kitchen": 0.8, "utility": 0.6,
                         "living_room": -1.0, "bedroom": -1.0},
    "casework": {"kitchen": 0.8, "store": 0.6, "bedroom": 0.4},
    "appliance": {"kitchen": 1.2, "utility": 0.8, "bathroom": -0.5},
    "furniture": {"living_room": 0.4, "bedroom": 0.4, "dining_room": 0.3},
    "stair": {"staircase": 2.5, "hallway": 0.8},
    "plumbing": {"bathroom": 1.0, "kitchen": 0.8, "utility": 0.6},
}


# ---------------------------------------------------------------------------
# Geometric and topological evidence
# ---------------------------------------------------------------------------


def window_evidence(window_count: int, room_type: str) -> float:
    """What a window count says about a room type.

    Scored against ``windowless_normal``, centred on 0.5 so the signal is
    genuinely two-sided: a windowless bedroom is strong evidence *against*
    bedroom (habitable rooms need daylight, and most codes require it), a
    windowless bathroom is mild evidence *for*, and a room type that is
    equally happy either way scores nothing.

    Deliberately smaller in magnitude than a fixture: a window tells you much
    less than a toilet does.
    """
    room_prior = ROOM_PRIORS.get(room_type)
    if room_prior is None:
        return 0.0

    normal = room_prior.windowless_normal

    if window_count == 0:
        # +1.2 when being windowless is entirely normal, -1.2 when it is not.
        return 2.4 * (normal - 0.5)
    # Having a window is mildly confirming for types that expect one, and
    # mildly disconfirming for a service shaft.
    return 1.2 * (0.5 - normal)


def door_evidence(door_count: int, room_type: str) -> float:
    """What a door count says about a room type.

    The informative case is a room with many doors: that is a circulation
    space, not a bedroom. A bedroom with four doors is nearly a contradiction.
    """
    room_prior = ROOM_PRIORS.get(room_type)
    if room_prior is None or door_count <= 0:
        return 0.0

    if door_count >= 3:
        return 1.2 if room_type in ("hallway", "staircase") else -0.9
    if door_count == 1 and room_type in ("bathroom", "bedroom", "store", "utility"):
        # A single door is characteristic of a private terminal room.
        return 0.5
    return 0.0


def adjacency_evidence(
    room_type: str, neighbour_types: Sequence[str]
) -> Tuple[float, List[str]]:
    """Contribution from which rooms this one connects to.

    Returns ``(score, reasons)``. The canonical case is an en-suite: a small
    room opening only onto a bedroom is a bathroom far more often than it is
    anything else.
    """
    room_prior = ROOM_PRIORS.get(room_type)
    if room_prior is None or not neighbour_types:
        return 0.0, []

    total = 0.0
    reasons: List[str] = []
    for neighbour in neighbour_types:
        weight = room_prior.adjacency.get(neighbour, 0.0)
        if weight > 0:
            contribution = 0.7 * weight
            total += contribution
            reasons.append(f"adjacent to {neighbour}")

    # Cap so a hub room touching six spaces cannot accumulate without bound.
    return min(total, 2.0), reasons


def privacy_evidence(room_type: str, normalised_depth: float) -> float:
    """Contribution from how deep the room sits in the circulation graph.

    ``normalised_depth`` is 0 at the entrance and 1 at the deepest room.
    Bathrooms and bedrooms sit deep; entrance halls and living rooms sit
    shallow. Weak evidence, but it is nearly free and it discriminates exactly
    where area and aspect do not.
    """
    room_prior = ROOM_PRIORS.get(room_type)
    if room_prior is None or normalised_depth < 0:
        return 0.0
    # Full agreement scores +0.6, full disagreement -0.6.
    return 0.6 * (1.0 - 2.0 * abs(room_prior.privacy - normalised_depth))


# ---------------------------------------------------------------------------
# Vision alignment
# ---------------------------------------------------------------------------

#: Vision-layer category names that differ from the CAD vocabulary. Keeps the
#: two evidence streams scoring against one table.
VISION_CATEGORY_ALIASES: Dict[str, str] = {
    "kitchen_counter": "kitchen_counter",
    "microwave": "oven",
    "carpet": "rug",
    "console_table": "dressing_table",
    "side_table": "bedside_table",
    "shelves": "bookshelf",
    "monitor": "study_table",
    "laptop": "study_table",
}


def normalise_category(category: str) -> str:
    """Map a vision or CAD category onto the evidence table's vocabulary."""
    if not category:
        return ""
    key = category.strip().lower().replace(" ", "_").replace("-", "_")
    return VISION_CATEGORY_ALIASES.get(key, key)
