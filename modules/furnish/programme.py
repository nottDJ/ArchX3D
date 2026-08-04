"""
ArchX3D — Furniture Programme
=============================
What furniture a room of a given type and size should contain.

The distinction this module rests on
------------------------------------
Everything the vision layer produces is a *claim about the building*: a sofa
is recorded because one was seen. Everything this module produces is a
*design decision*: a bedroom gets a bed because bedrooms have beds, not
because anybody observed one.

Those are different kinds of statement and must never be confused, so
procedural items are tagged and carry a confidence that reflects convention
rather than evidence. The project's rule — "never guess if reliable
information exists" — is honoured by only furnishing rooms where no
observation exists, and by labelling what is generated so a user can always
tell the two apart.

Sizing by area, not by count
----------------------------
A 9 m² bedroom and a 30 m² bedroom both need a bed; only one of them has room
for an armchair and a desk. So a programme is an *ordered* list of candidate
items with a minimum area each, and the placement solver stops adding when it
runs out of floor or clearance. Ordering is by importance, so a room that can
only fit two things gets the right two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Programme entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgrammeItem:
    """One candidate piece of furniture for a room type."""

    #: ``vision.catalog`` category, so the metric priors and asset builders
    #: already defined for it apply unchanged.
    category: str

    #: Floor area, m², below which this item is not attempted at all. Keeps a
    #: 4 m² box room from being handed a three-seat sofa.
    min_area: float = 0.0

    #: How many to place. ``per_area`` adds one more per this many m² above
    #: ``min_area``, capped by ``max_count`` — dining chairs and downlights
    #: scale with the room, a bed does not.
    count: int = 1
    per_area: float = 0.0
    max_count: int = 1

    #: Ordering weight. Higher is placed first and is more likely to survive a
    #: cramped room. The bed in a bedroom is 1.0; a decorative plant is 0.1.
    importance: float = 0.5

    #: When set, this item is only placed if the named category was placed.
    #: A bedside table without a bed is furniture in a void.
    requires: str = ""

    #: Placement relationship applied after positioning, using the existing
    #: ``vision.relations`` predicates.
    relation: str = ""
    relation_target: str = ""

    #: True when the item rests on another object's surface rather than the
    #: floor; ``requires`` names the carrier.
    on_surface: bool = False


def _i(category: str, min_area: float, importance: float, **kwargs) -> ProgrammeItem:
    return ProgrammeItem(
        category=category, min_area=min_area, importance=importance, **kwargs
    )


# ---------------------------------------------------------------------------
# Programmes
# ---------------------------------------------------------------------------
#
# Ordered most-important first within each room type. Minimum areas are the
# whole-room area at which the item becomes reasonable, taken from the same
# residential space standards as ``semantic.taxonomy`` (Neufert, NDSS, NBC).

PROGRAMMES: Dict[str, Tuple[ProgrammeItem, ...]] = {
    "bedroom": (
        _i("bed", 7.0, 1.0),
        _i("bedside_table", 8.5, 0.8, count=1, per_area=6.0, max_count=2,
           requires="bed", relation="beside", relation_target="bed"),
        _i("wardrobe", 9.0, 0.75),
        _i("study_table", 15.0, 0.4),
        _i("office_chair", 16.0, 0.35, requires="study_table",
           relation="faces", relation_target="study_table"),
        _i("armchair", 20.0, 0.3),
        _i("rug", 12.0, 0.25),
        _i("plant", 14.0, 0.15),
    ),
    "living_room": (
        _i("sofa", 12.0, 1.0),
        _i("coffee_table", 14.0, 0.8, relation="faces", relation_target="sofa"),
        _i("tv_unit", 13.0, 0.75),
        _i("tv", 13.0, 0.7, requires="tv_unit"),
        _i("armchair", 18.0, 0.5, count=1, per_area=12.0, max_count=2),
        _i("rug", 15.0, 0.4, requires="coffee_table",
           relation="centered_under", relation_target="coffee_table"),
        _i("side_table", 20.0, 0.3),
        _i("bookshelf", 22.0, 0.3),
        _i("plant", 16.0, 0.25, count=1, per_area=18.0, max_count=3),
        _i("painting", 14.0, 0.2),
    ),
    "dining_room": (
        _i("dining_table", 7.0, 1.0),
        # Four chairs at a minimum, six in a generous room. Placed by the
        # `surrounds` predicate, which distributes them around the table.
        _i("dining_chair", 7.0, 0.9, count=4, per_area=6.0, max_count=8,
           requires="dining_table", relation="surrounds",
           relation_target="dining_table"),
        _i("sideboard", 14.0, 0.4),
        _i("rug", 14.0, 0.25, requires="dining_table",
           relation="centered_under", relation_target="dining_table"),
        _i("plant", 12.0, 0.2),
    ),
    "kitchen": (
        _i("kitchen_counter", 4.0, 1.0, count=1, per_area=8.0, max_count=3),
        _i("refrigerator", 6.0, 0.85),
        _i("oven", 7.0, 0.6),
        _i("kitchen_island", 16.0, 0.5),
        _i("stool", 18.0, 0.3, count=2, per_area=8.0, max_count=4,
           requires="kitchen_island", relation="surrounds",
           relation_target="kitchen_island"),
        _i("microwave", 8.0, 0.25, requires="kitchen_counter", on_surface=True,
           relation="on_top_of", relation_target="kitchen_counter"),
    ),
    "office": (
        _i("study_table", 5.0, 1.0),
        _i("office_chair", 5.0, 0.9, requires="study_table",
           relation="faces", relation_target="study_table"),
        _i("bookshelf", 8.0, 0.6, count=1, per_area=8.0, max_count=2),
        _i("cabinet", 12.0, 0.35),
        _i("monitor", 6.0, 0.3, requires="study_table", on_surface=True,
           relation="on_top_of", relation_target="study_table"),
        _i("plant", 10.0, 0.2),
        _i("rug", 12.0, 0.2),
    ),
    "bathroom": (
        # No catalog priors or asset builders exist for sanitary ware yet, so
        # a bathroom is deliberately furnished only with what can actually be
        # built. Generating a `toilet` category with no builder would emit an
        # object the Blender stage silently skips — worse than an empty room,
        # because the scene graph would claim something the model lacks.
        _i("cabinet", 3.5, 0.5),
        _i("mirror", 2.5, 0.4),
    ),
    "hallway": (
        _i("console_table", 6.0, 0.5),
        _i("mirror", 5.0, 0.35),
        _i("rug", 6.0, 0.3),
        _i("plant", 7.0, 0.25),
        _i("painting", 5.0, 0.2),
    ),
    "studio": (
        _i("bed", 15.0, 1.0),
        _i("sofa", 20.0, 0.8),
        _i("dining_table", 18.0, 0.6),
        _i("dining_chair", 18.0, 0.5, count=2, per_area=8.0, max_count=4,
           requires="dining_table", relation="surrounds",
           relation_target="dining_table"),
        _i("wardrobe", 18.0, 0.6),
        _i("kitchen_counter", 16.0, 0.5),
        _i("coffee_table", 22.0, 0.4, relation="faces", relation_target="sofa"),
        _i("rug", 20.0, 0.25),
        _i("plant", 18.0, 0.2),
    ),
    "balcony": (
        _i("chair", 3.0, 0.6, count=2, per_area=6.0, max_count=4),
        _i("side_table", 4.0, 0.5),
        _i("plant", 2.0, 0.45, count=2, per_area=4.0, max_count=6),
    ),
    "utility": (
        _i("washing_machine", 2.5, 0.9),
        _i("cabinet", 4.0, 0.5, count=1, per_area=5.0, max_count=2),
        _i("shelves", 5.0, 0.35),
    ),
    "store": (
        _i("shelves", 1.5, 0.8, count=1, per_area=4.0, max_count=3),
        _i("cabinet", 4.0, 0.4),
    ),
    "garage": (
        _i("cabinet", 15.0, 0.4, count=1, per_area=15.0, max_count=2),
        _i("shelves", 14.0, 0.3),
    ),
}

#: Room types that are deliberately left empty. Circulation cores and service
#: voids are not furnished, and putting a plant in a duct is not a feature.
UNFURNISHED_TYPES = frozenset({"staircase", "shaft", "unknown"})


# ---------------------------------------------------------------------------
# Lighting programme
# ---------------------------------------------------------------------------
#
# Separate from the furniture programme because lights are `LightSource`
# records, not `SceneObject`s — `table_lamp` and `floor_lamp` are entries in
# `catalog.LIGHT_TYPES`, and emitting them as furniture would produce objects
# with no asset builder.
#
# Not optional, either. A furnished room with no luminaire renders black, so
# every furnishable room gets at least a ceiling fitting; a room lit only by
# whatever the vision layer happened to notice is a room that is usually dark.


@dataclass(frozen=True)
class LightingItem:
    """One luminaire type in a room's lighting scheme."""

    kind: str
    #: Base count, plus one per ``per_area`` m² above ``min_area``.
    count: int = 1
    per_area: float = 0.0
    max_count: int = 1
    min_area: float = 0.0
    #: When set, the fitting stands on / beside this furniture category and is
    #: only placed if that category was.
    requires: str = ""


LIGHTING_PROGRAMMES: Dict[str, Tuple[LightingItem, ...]] = {
    "bedroom": (
        LightingItem("ceiling_light", count=1, per_area=18.0, max_count=2),
        LightingItem("table_lamp", count=1, per_area=6.0, max_count=2,
                     min_area=8.5, requires="bedside_table"),
    ),
    "living_room": (
        LightingItem("ceiling_light", count=1, per_area=16.0, max_count=3),
        LightingItem("floor_lamp", count=1, per_area=20.0, max_count=2,
                     min_area=18.0),
    ),
    "dining_room": (
        LightingItem("pendant_light", count=1, per_area=14.0, max_count=2),
    ),
    "kitchen": (
        LightingItem("ceiling_light", count=1, per_area=10.0, max_count=2),
        LightingItem("recessed_light", count=2, per_area=5.0, max_count=6,
                     min_area=8.0),
    ),
    "office": (
        LightingItem("ceiling_light"),
        LightingItem("table_lamp", min_area=6.0, requires="study_table"),
    ),
    "bathroom": (
        LightingItem("ceiling_light"),
        LightingItem("wall_light", count=1, per_area=6.0, max_count=2,
                     min_area=3.0),
    ),
    "hallway": (
        LightingItem("ceiling_light", count=1, per_area=8.0, max_count=3),
    ),
    "studio": (
        LightingItem("ceiling_light", count=1, per_area=16.0, max_count=3),
        LightingItem("floor_lamp", min_area=20.0),
    ),
    "balcony": (
        LightingItem("wall_light", count=1, per_area=6.0, max_count=2),
    ),
    "utility": (LightingItem("ceiling_light"),),
    "store": (LightingItem("ceiling_light"),),
    "garage": (
        LightingItem("ceiling_light", count=1, per_area=18.0, max_count=3),
    ),
    "staircase": (LightingItem("ceiling_light"),),
}


@dataclass
class PlannedLight:
    """A lighting item resolved against one specific room."""

    kind: str
    index: int = 0
    requires: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}_{self.index}"


def plan_lighting(room_type: str, area: float) -> List[PlannedLight]:
    """The luminaires a room of this type and size should have.

    Circulation cores still get a light even though they get no furniture —
    an unlit stairwell is a safety problem in the model as much as in life.
    """
    if area <= 0:
        return []

    programme = LIGHTING_PROGRAMMES.get(room_type)
    if not programme:
        # An unrecognised room is still an enclosed space someone walks into,
        # and the honest reading of "we do not know what this room is" is not
        # "therefore it has no ceiling". Every interior room in a dwelling has
        # a general light; withholding one leaves a black void that is
        # certainly wrong, in preference to a plain pendant that is almost
        # certainly right.
        #
        # This is convention, not invention — the distinction the project draws
        # elsewhere. Nothing about the room's *contents* is guessed here, and a
        # room whose type is later identified gets that type's programme
        # instead.
        return [PlannedLight(kind="ceiling_light", index=0, requires="")]

    planned: List[PlannedLight] = []
    for item in programme:
        if area < item.min_area:
            continue
        count = item.count
        if item.per_area > 0:
            extra = int(max(0.0, area - item.min_area) / item.per_area)
            count = min(item.max_count, item.count + extra)
        count = max(1, min(count, max(item.max_count, item.count)))

        for index in range(count):
            planned.append(PlannedLight(
                kind=item.kind, index=index, requires=item.requires
            ))
    return planned


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass
class PlannedItem:
    """A programme item resolved against one specific room."""

    category: str
    importance: float
    requires: str = ""
    relation: str = ""
    relation_target: str = ""
    on_surface: bool = False
    #: Index within its category, so ``bedside_table`` 0 and 1 are distinct.
    index: int = 0

    @property
    def key(self) -> str:
        return f"{self.category}_{self.index}"


def plan_room(room_type: str, area: float) -> List[PlannedItem]:
    """The ordered list of items to attempt in a room of this type and size.

    Returns candidates, not commitments: the placement solver decides what
    actually fits. Ordering is by importance so that when it runs out of
    space, what survives is what matters.
    """
    if room_type in UNFURNISHED_TYPES:
        return []

    programme = PROGRAMMES.get(room_type)
    if not programme or area <= 0:
        return []

    planned: List[PlannedItem] = []

    for item in programme:
        if area < item.min_area:
            continue

        count = item.count
        if item.per_area > 0:
            extra = int(max(0.0, area - item.min_area) / item.per_area)
            count = min(item.max_count, item.count + extra)
        count = max(1, min(count, max(item.max_count, item.count)))

        for index in range(count):
            planned.append(PlannedItem(
                category=item.category,
                importance=item.importance,
                requires=item.requires,
                relation=item.relation,
                relation_target=item.relation_target,
                on_surface=item.on_surface,
                index=index,
            ))

    # Stable ordering: importance first, then the programme's own order, so a
    # room's furniture is reproducible run to run.
    planned.sort(key=lambda p: (-p.importance, p.category, p.index))
    return planned


def programme_for(room_type: str) -> Tuple[ProgrammeItem, ...]:
    """The raw programme for a room type, for inspection and tests."""
    return PROGRAMMES.get(room_type, ())


def furnishable_types() -> Tuple[str, ...]:
    return tuple(sorted(PROGRAMMES))
