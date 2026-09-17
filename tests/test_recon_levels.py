"""Buildings and storeys: how a sheet's plans relate, decided from evidence.

The acceptance criterion is simple to state and easy to fake:

    single building -> 1 Building      one plan per storey -> that many Levels
    two buildings   -> 2 Buildings     ambiguous           -> flagged, not guessed
    three buildings -> 3 Buildings

It is faked by keying off counts or filenames, so nothing here does. Every
expectation is a property of the drawing's content — which walls touch, what
the titles under the plans say, what the layer names say — and the fixtures
isolate one such property each (``tests/fixtures/make_multi.py``). Two real
drawings anchor it: ``final_plan_19th_may.dxf``, a house whose ground and
first floor plans sit side by side under their own titles, and ``sba.dxf``, a
planning sheet whose two congruent floor plates carry no title at all.
"""

from __future__ import annotations

import math
import os

import pytest

from modules.recon import levels as LV
from modules.recon import structure as ST
from modules.recon.ir import (AMBIGUOUS, LEVELS_AMBIGUOUS, LEVELS_RESOLVED,
                              LEVELS_SINGLE, VALID, VALID_WITH_WARNINGS, Wall,
                              drawing_from_dict)
from modules.recon.pipeline import reconstruct

HERE = os.path.dirname(os.path.abspath(__file__))
MULTI = os.path.join(HERE, "fixtures", "multi")
REAL = os.path.join(HERE, "fixtures", "real")
SBA = os.path.join(os.path.dirname(HERE), "uploads", "20260617_102040_sba.dxf")


def fixture(name, where=MULTI):
    path = os.path.join(where, name)
    if not os.path.exists(path):
        pytest.skip("fixture %s not generated; run tests/fixtures/make_multi.py" % name)
    return path


@pytest.fixture(scope="module")
def built():
    out = {}
    for name in sorted(os.listdir(MULTI)) if os.path.isdir(MULTI) else []:
        if name.endswith(".dxf"):
            out[name] = reconstruct(os.path.join(MULTI, name), strict=False)
    return out


def model(built, name):
    if name not in built:
        pytest.skip("fixture %s not generated" % name)
    d = built[name]
    assert d.validation["ok"], d.validation["errors"]
    return d


def storeys(d):
    return [len(b.levels) for b in d.buildings]


# ---------------------------------------------------------------------------
# P0.1 — how many buildings
# ---------------------------------------------------------------------------

class TestBuildingCount:
    def test_a_house_and_a_detached_garage_are_two_buildings(self, built):
        d = model(built, "m01_house_and_garage.dxf")
        assert len(d.buildings) == 2
        assert storeys(d) == [1, 1]
        assert d.level_structure["status"] == LEVELS_SINGLE
        garage = [b for b in d.buildings
                  if any(r.room_type == "garage" for r in b.rooms)]
        assert len(garage) == 1, "the garage is its own building"
        assert len(garage[0].rooms) == 1
        house = next(b for b in d.buildings if b is not garage[0])
        assert {r.label for r in house.rooms} == {"BEDROOM", "LIVING", "KITCHEN"}

    def test_three_structures_are_three_buildings(self, built):
        d = model(built, "m02_three_buildings.dxf")
        assert len(d.buildings) == 3
        assert storeys(d) == [1, 1, 1]
        areas = sorted(round(b.floor_area) for b in d.buildings)
        # house 66 m2, L-studio 48 m2 less walls, garage 36 m2 less walls
        assert areas[0] == pytest.approx(34, abs=3)
        assert areas[1] == pytest.approx(44, abs=4)
        assert areas[2] == pytest.approx(66, abs=3)

    def test_a_party_wall_makes_one_building(self, built):
        """Two dwellings that share a wall are one structure."""
        d = model(built, "m03_semi_detached.dxf")
        assert len(d.buildings) == 1
        assert len(d.buildings[0].levels) == 1
        assert d.buildings[0].levels[0].width == pytest.approx(18.0, abs=0.35)

    def test_a_close_pair_stays_two_and_is_reported(self, built):
        """0.9 m apart: separate construction, but worth a second look."""
        d = model(built, "m04_close_buildings.dxf")
        assert len(d.buildings) == 2
        codes = [r["code"] for r in d.review]
        assert "CLOSE_STRUCTURES" in codes

    def test_each_building_is_labelled_exterior_on_its_own_envelope(self, built):
        """The smaller building's outside walls are exterior walls too."""
        d = model(built, "m01_house_and_garage.dxf")
        for b in d.buildings:
            level = b.levels[0]
            assert sum(1 for w in level.walls if w.kind == "exterior") >= 4, b.name


# ---------------------------------------------------------------------------
# P0.2 — how many storeys, and only from evidence
# ---------------------------------------------------------------------------

class TestStoreys:
    def test_two_titled_plans_are_two_storeys_of_one_building(self, built):
        d = model(built, "m05_two_storey_titled.dxf")
        assert len(d.buildings) == 1
        levels = d.buildings[0].levels
        assert [l.name for l in levels] == ["Ground floor", "First floor"]
        assert [l.title for l in levels] == ["GROUND FLOOR PLAN", "FIRST FLOOR PLAN"]
        assert [l.designation for l in levels] == ["title", "title"]
        assert d.level_structure["status"] == LEVELS_RESOLVED
        # The geometry is sound, but the first floor's height is an assumption
        # (the drawing states no level heights), and P2 requires that be said
        # rather than presented as measured. So the outcome is sound-with-a-
        # caveat, and the caveat has to name what was assumed.
        assert d.validation["status"] == VALID_WITH_WARNINGS
        assert d.validation["ok"], "an estimated height is not a geometry fault"
        assert any("estimated, not measured" in w
                   for w in d.validation["warnings"])

    def test_storeys_are_stacked_by_elevation(self, built):
        d = model(built, "m05_two_storey_titled.dxf")
        g, f = d.buildings[0].levels
        assert g.elevation == 0.0
        assert f.elevation == pytest.approx(g.height)
        assert (g.index, f.index) == (0, 1)

    def test_the_upper_storey_is_registered_over_the_lower(self, built):
        """Drawn 16 m to the right on the sheet; built directly above."""
        d = model(built, "m05_two_storey_titled.dxf")
        f = d.buildings[0].levels[1]
        assert f.placement[0] == pytest.approx(-16.0, abs=0.02)
        assert f.placement[1] == pytest.approx(0.0, abs=0.02)
        assert f.confidence > 0.5

    def test_three_storeys_stacked_up_the_sheet(self, built):
        d = model(built, "m06_three_storey_stacked.dxf")
        assert len(d.buildings) == 1
        levels = d.buildings[0].levels
        assert [l.index for l in levels] == [0, 1, 2]
        assert [l.name for l in levels] == ["Ground floor", "First floor", "Second floor"]
        assert [round(l.elevation, 2) for l in levels] == [0.0, 2.7, 5.4]
        assert levels[1].placement == pytest.approx((0.0, -13.0), abs=0.02)
        assert levels[2].placement == pytest.approx((0.0, -26.0), abs=0.02)
        assert d.units.unit_name == "millimetres"

    def test_layer_names_designate_storeys_without_titles(self, built):
        d = model(built, "m10_layer_levels.dxf")
        assert len(d.buildings) == 1
        levels = d.buildings[0].levels
        assert [l.name for l in levels] == ["Ground floor", "First floor"]
        assert [l.designation for l in levels] == ["layer", "layer"]
        assert levels[1].placement == pytest.approx((0.0, -12.0), abs=0.02)

    def test_a_basement_sits_below_ground(self, built):
        d = model(built, "m11_basement.dxf")
        levels = d.buildings[0].levels
        assert [l.name for l in levels] == ["Basement", "Ground floor"]
        assert levels[0].index == -1
        assert levels[0].elevation == pytest.approx(-levels[0].height)
        assert levels[1].elevation == 0.0
        # The ground floor is the reference; the basement is placed under it.
        assert levels[1].placement == (0.0, 0.0)
        assert levels[0].placement == pytest.approx((16.0, 0.0), abs=0.02)


class TestAmbiguity:
    def test_repeated_untitled_plates_are_ambiguous_not_merged(self, built):
        d = model(built, "m07_repeated_untitled.dxf")
        assert d.level_structure["status"] == LEVELS_AMBIGUOUS
        assert d.validation["status"] == AMBIGUOUS
        # Kept apart exactly as drawn: neither merged nor stacked.
        assert len(d.buildings) == 2
        assert storeys(d) == [1, 1]
        assert all(l.elevation == 0.0 and l.placement == (0.0, 0.0)
                   for l in d.levels())
        item = next(r for r in d.review if r["code"] == LEVELS_AMBIGUOUS)
        assert len(item["structures"]) == 2

    def test_one_title_does_not_settle_its_untitled_twin(self, built):
        d = model(built, "m12_one_title_one_twin.dxf")
        assert d.level_structure["status"] == LEVELS_AMBIGUOUS
        assert len(d.buildings) == 2

    def test_block_designations_resolve_twin_plates_as_two_buildings(self, built):
        d = model(built, "m09_block_designations.dxf")
        assert d.level_structure["status"] == LEVELS_SINGLE
        assert sorted(b.designation for b in d.buildings) == ["BLOCK A", "BLOCK B"]
        assert sorted(b.name for b in d.buildings) == ["Block A", "Block B"]

    def test_a_floor_schedule_is_not_a_set_of_storeys(self, built):
        d = model(built, "m08_floor_schedule.dxf")
        assert len(d.buildings) == 1
        assert len(d.buildings[0].levels) == 1
        rejected = [t for t in d.level_structure["titles"] if t["rejected"]]
        assert len(rejected) >= 3
        assert all("schedule" in t["rejected"] for t in rejected)


# ---------------------------------------------------------------------------
# Real drawings
# ---------------------------------------------------------------------------

class TestRealTwoStoreyHouse:
    """``final_plan_19th_may.dxf``: ground and first floor side by side."""

    @pytest.fixture(scope="class")
    def d(self):
        return reconstruct(fixture("final_plan_19th_may.dxf", REAL))

    def test_one_building_two_storeys_from_the_drawings_titles(self, d):
        assert len(d.buildings) == 1
        levels = d.buildings[0].levels
        assert [l.title for l in levels] == ["GROUND FLOOR PLAN", "FIRST FLOOR PLAN"]
        assert d.level_structure["status"] == LEVELS_RESOLVED

    def test_the_first_floor_stands_on_the_ground_floor(self, d):
        g, f = d.buildings[0].levels
        assert f.elevation > g.elevation
        # Registered by the walls the storeys share; drawn ~20.5 m apart.
        assert f.placement[0] == pytest.approx(-17.04, abs=0.15)
        assert abs(f.placement[1]) < 0.15
        assert f.confidence > 0.5
        stack = [w for w in d.validation["warnings"] if "stands over" in w]
        assert not stack, stack

    def test_the_storeys_have_their_own_rooms(self, d):
        g, f = d.buildings[0].levels
        assert len(g.rooms) >= 10 and len(f.rooms) >= 8
        assert {r.level_id for r in g.rooms} == {g.id}
        assert {r.level_id for r in f.rooms} == {f.id}

    def test_it_is_inches_and_house_sized(self, d):
        assert d.units.unit_name == "inches"
        g = d.buildings[0].levels[0]
        assert 14.0 < g.width < 20.0 and 12.0 < g.depth < 18.0


class TestRealRepeatedFloorPlates:
    """``sba.dxf``: two congruent floor plates and nothing naming either.

    Earlier notes described this sheet as two buildings. The drawing does not
    support that: the plates are the same apartment floor repeated (identical
    unit names, one with an open terrace), each inside its own copy of the
    plot outline, and the sheet's area statement lists a first and a second
    floor. It does not say *which* plate is which, so the honest result is
    ambiguity — the two plates kept apart, flagged, and not stacked.
    """

    @pytest.fixture(scope="class")
    def d(self):
        if not os.path.exists(SBA):
            pytest.skip("sba.dxf not present")
        return reconstruct(SBA)

    def test_the_repeated_plates_are_flagged_not_merged(self, d):
        assert d.level_structure["status"] == LEVELS_AMBIGUOUS
        assert d.validation["status"] == AMBIGUOUS
        item = next(r for r in d.review if r["code"] == LEVELS_AMBIGUOUS)
        plates = [b for b in d.buildings
                  if b.levels[0].stats["structure"] in item["structures"]]
        assert len(plates) == 2
        a, b = (p.levels[0] for p in plates)
        assert a.width == pytest.approx(b.width, abs=1.0)
        assert a.floor_area == pytest.approx(b.floor_area, rel=0.15)
        assert a.width > 70 and a.depth > 20

    def test_the_floor_schedule_names_no_storey(self, d):
        """``FLOOR - BASEMENT`` / ``FIRST`` / ``SECOND`` sit in the area table."""
        schedule = [t for t in d.level_structure["titles"]
                    if t["text"].upper().startswith("FLOOR -")]
        assert all(t["structure"] is None for t in schedule), schedule

    def test_the_stilt_plan_is_named_by_its_own_title(self, d):
        stilt = [l for l in d.levels() if l.title == "SITE / STILT FLOOR PLAN"]
        assert len(stilt) == 1
        assert stilt[0].name == "Stilt floor"
        # The stilt plan is not congruent with the floor plates, so naming it
        # does not settle the plates either way.
        assert stilt[0].width < 40.0

    def test_stray_site_walls_are_reported_not_built(self, d):
        assert d.unassigned
        assert any(r["code"] == "UNASSIGNED_WALLS" for r in d.review)


# ---------------------------------------------------------------------------
# The single-building regression: every existing plan is still one building
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "t01_simple_rect.dxf", "t02_l_shape.dxf", "t03_multi_room.dxf",
    "t04_garage_porch.dxf", "t05_irregular.dxf", "t06_dimension_heavy.dxf",
    "t07_many_layers.dxf", "t08_blocks.dxf", "t09_feet_inches.dxf",
    "t10_millimetres.dxf", "t11_metres.dxf", "t12_lying_header.dxf",
    "t13_single_line.dxf", "t14_rotated.dxf", "residential_us.dxf",
])
def test_single_plans_remain_one_building_one_storey(name):
    path = fixture(name, os.path.join(HERE, "fixtures", "plans"))
    d = reconstruct(path)
    assert len(d.buildings) == 1
    assert len(d.buildings[0].levels) == 1
    assert d.level_structure["status"] == LEVELS_SINGLE


# ---------------------------------------------------------------------------
# Units of the decision
# ---------------------------------------------------------------------------

class TestParseLevel:
    @pytest.mark.parametrize("text,key,name", [
        ("GROUND FLOOR PLAN", 0.0, "Ground floor"),
        ("%%uGROUND FLOOR PLAN", 0.0, "Ground floor"),
        ("FIRST FLOOR PLAN", 1.0, "First floor"),
        ("1ST FLOOR PLAN", 1.0, "First floor"),
        ("Second Floor Plan - Scale 1:100", 2.0, "Second floor"),
        ("3RD FLOOR", 3.0, "Third floor"),
        ("LEVEL 4 PLAN", 4.0, "Fourth floor"),
        ("BASEMENT PLAN", -1.0, "Basement"),
        ("BASEMENT 2 PLAN", -2.0, "Basement 2"),
        ("STILT FLOOR PLAN", 0.0, "Stilt floor"),
        ("G.F. PLAN", 0.0, "Ground floor"),
        ("TERRACE FLOOR PLAN", 99.0, "Roof"),
        ("MEZZANINE PLAN", 0.5, "Mezzanine"),
    ])
    def test_titles(self, text, key, name):
        tag = LV.parse_level(text)
        assert tag is not None, text
        assert tag.key == key
        assert tag.name == name

    @pytest.mark.parametrize("text", [
        "UP TO FIRST FLOOR", "DN TO GROUND FLOOR", "FIRST FLOOR SLAB",
        "FLOOR NAME", "TOTAL BUILT UP AREA", "BEDROOM", "LIVING 20'0\" X 15'9\"",
        "FFL +3.000", "SQ.M FIRST FLOOR AREA",
    ])
    def test_not_titles(self, text):
        assert LV.parse_level(text) is None, text

    def test_views_that_are_not_plans(self):
        assert LV.parse_level("FRONT ELEVATION").kind == "elevation"
        assert LV.parse_level("SECTION A-A").kind == "section"

    @pytest.mark.parametrize("text,key", [
        # Dutch
        ("00 BEGANE GROND PLAN", 0.0), ("Plattegrond eerste verdieping", 1.0),
        ("2e verdieping", 2.0), ("KELDER", -1.0),
        # German
        ("Grundriss Erdgeschoss", 0.0), ("1. Obergeschoss", 1.0), ("Grundriss 2.OG", 2.0),
        ("Grundriss EG", 0.0), ("Grundriss UG", -1.0), ("Dachgeschoss", 98.0),
        # French
        ("Plan du rez-de-chaussée", 0.0), ("1er étage", 1.0), ("Plan R+2", 2.0),
        ("Deuxième étage", 2.0), ("Sous-sol", -1.0),
        # Spanish, Italian, Portuguese
        ("Planta baja", 0.0), ("Planta primera", 1.0), ("Planta 3", 3.0), ("Sótano", -1.0),
        ("Pianta piano terra", 0.0), ("Primo piano", 1.0), ("2º andar", 2.0),
        # Estonian
        ("1. korrus", 1.0), ("3. KORRUSE PLAAN", 3.0),
    ])
    def test_titles_in_other_languages(self, text, key):
        tag = LV.parse_level(text)
        assert tag is not None, text
        assert tag.key == key, (text, tag.key)

    @pytest.mark.parametrize("text", [
        "BERGING", "SLAAPKAMER 1", "Wohnen/Essen", "Salle de bain", "Cocina", "KORIDOR",
        "TRAP NAAR 1E VERDIEPING SCHEDULE",
    ])
    def test_room_names_in_other_languages_are_not_titles(self, text):
        assert LV.parse_level(text) is None, text


class TestParseBuilding:
    @pytest.mark.parametrize("text,expected", [
        ("BLOCK A", "BLOCK A"), ("BLOCK - B", "BLOCK B"), ("TOWER 2", "TOWER 2"),
        ("BLDG. C", "BUILDING C"), ("BUILDING NO. 3", "BUILDING 3"),
    ])
    def test_designations(self, text, expected):
        assert LV.parse_building(text) == expected

    @pytest.mark.parametrize("text", [
        "BLOCK NO. 21, T.S. NO. 57", "BLOCK WORK WALL", "UNIT-1A", "BEDROOM",
    ])
    def test_not_designations(self, text):
        assert LV.parse_building(text) is None

    def test_layer_tokens(self):
        assert LV.layer_level("GF-WALL") == 0.0
        assert LV.layer_level("FF_A-WALL") == 1.0
        assert LV.layer_level("L02-WALLS") == 2.0
        assert LV.layer_level("xref-Tower$0$BSMT-WALL") == -1.0
        assert LV.layer_level("A-WALL") is None
        assert LV.layer_level("A-WALL-FULL") is None


def _box(x0, y0, x1, y1, t=0.2, prefix="w"):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [Wall(id="%s%d" % (prefix, i), start=pts[i], end=pts[(i + 1) % 4],
                 thickness=t) for i in range(4)]


class TestPartition:
    def test_touching_walls_are_one_structure(self):
        walls = _box(0, 0, 10, 8) + _box(10, 0, 16, 8, prefix="g")
        part = ST.partition(walls)
        assert len(part.structures) == 1

    def test_separate_enclosures_are_separate_structures(self):
        walls = _box(0, 0, 10, 8) + _box(14, 0, 20, 6, prefix="g")
        part = ST.partition(walls)
        assert len(part.structures) == 2
        assert sorted(len(s.walls) for s in part.structures) == [4, 4]

    def test_a_structure_inside_another_is_part_of_it(self):
        """A freestanding core within a building's outline is not a building."""
        walls = _box(0, 0, 20, 20) + _box(8, 8, 12, 12, prefix="c")
        part = ST.partition(walls)
        assert len(part.structures) == 1

    def test_a_wall_stub_joins_the_structure_beside_it(self):
        walls = _box(0, 0, 10, 8) + [
            Wall(id="stub", start=(12.0, 0.0), end=(12.0, 3.0), thickness=0.2)]
        part = ST.partition(walls)
        assert len(part.structures) == 1
        assert "stub" in part.structures[0].wall_ids

    def test_a_distant_wall_stub_is_unassigned_not_a_building(self):
        walls = _box(0, 0, 10, 8) + [
            Wall(id="fence", start=(40.0, 0.0), end=(60.0, 0.0), thickness=0.2)]
        part = ST.partition(walls)
        assert len(part.structures) == 1
        assert part.unassigned and part.unassigned[0]["wall_ids"] == ["fence"]

    def test_walls_that_enclose_nothing_are_kept_whole(self):
        walls = [Wall(id="a", start=(0, 0), end=(10, 0), thickness=0.2),
                 Wall(id="b", start=(30, 0), end=(40, 0), thickness=0.2)]
        part = ST.partition(walls)
        assert len(part.structures) == 1
        assert len(part.structures[0].walls) == 2


class TestIrRoundTrip:
    def test_the_hierarchy_survives_serialisation(self, built):
        d = model(built, "m06_three_storey_stacked.dxf")
        again = drawing_from_dict(d.as_dict())
        assert len(again.buildings) == 1
        assert [l.id for l in again.buildings[0].levels] == \
            [l.id for l in d.buildings[0].levels]
        assert [l.placement for l in again.buildings[0].levels] == \
            [l.placement for l in d.buildings[0].levels]
        assert len(again.walls) == len(d.walls)

    def test_a_legacy_flat_document_loads_as_one_building(self):
        flat = {"schema_version": "archx3d.ir/2.0", "walls": [
            {"id": "w1", "start": [0, 0], "end": [5, 0], "thickness": 0.2}],
            "rooms": [], "openings": []}
        d = drawing_from_dict(flat)
        assert len(d.buildings) == 1 and len(d.buildings[0].levels) == 1
        assert d.walls[0].id == "w1"


class TestElevationHonesty:
    """P2: an assumed storey height must never read as a measured one."""

    def test_the_ground_storey_is_the_datum_not_an_estimate(self, built):
        d = model(built, "m05_two_storey_titled.dxf")
        ground = d.buildings[0].levels[0]
        assert ground.elevation == 0.0
        assert ground.elevation_source == "datum"

    def test_an_upper_storey_says_its_elevation_was_estimated(self, built):
        d = model(built, "m06_three_storey_stacked.dxf")
        upper = d.buildings[0].levels[1:]
        assert upper, "fixture should have storeys above the ground floor"
        for level in upper:
            assert level.elevation_source == "estimated"
            assert any("estimated" in e for e in level.evidence), level.evidence

    def test_a_single_storey_claims_nothing_and_warns_about_nothing(self):
        d = reconstruct(os.path.join(HERE, "fixtures", "plans", "t01_simple_rect.dxf"))
        level = next(iter(d.levels()))
        assert level.elevation == 0.0
        assert level.elevation_source == "datum"
        assert not any("estimated" in w for w in d.validation["warnings"])
