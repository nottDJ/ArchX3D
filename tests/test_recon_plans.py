"""End-to-end reconstruction over every fixture plan, with measured metrics.

"1189 tests pass" is what the engine this replaces could say while producing a
0.8 m building, so the assertions here are geometric rather than structural:
overall size, wall thickness, room count, footprint area, opening count. Each
is a number a wrong reconstruction cannot accidentally hit.

Two kinds of fixture, and both are needed:

* **generated** (``tests/fixtures/plans/t*.dxf``) — drawn by
  ``make_plans.py``, so the truth is known exactly and each one isolates one
  construction: blocks, a lying header, dimension clutter, a rotated sheet.
* **real** — ``residential_us.dxf`` is the drawing that failed the desktop
  acceptance test and is now a permanent regression fixture, and
  ``apartment.dxf`` is the existing semantic fixture.

Nothing here keys off a filename. Every expectation is a property of the
drawing's content that any correct engine would reproduce.
"""

from __future__ import annotations

import math
import os
import time

import pytest

from modules.recon import compat
from modules.recon.ir import ReconstructionError
from modules.recon.pipeline import reconstruct

PLANS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "plans")

#: The three-room 10 x 7 m plan that several fixtures share, so its expected
#: geometry is written once.
RECT = dict(width=10.0, depth=7.0, rooms=3, doors=3, windows=4,
            names={"BEDROOM 1", "LIVING", "KITCHEN"})


def plan(name):
    path = os.path.join(PLANS_DIR, name)
    if not os.path.exists(path):
        pytest.skip("fixture %s not generated; run tests/fixtures/make_plans.py"
                    % name)
    return path


@pytest.fixture(scope="module")
def built():
    """Reconstruct every fixture once and share the results."""
    out = {}
    for name in sorted(os.listdir(PLANS_DIR)) if os.path.isdir(PLANS_DIR) else []:
        if not name.endswith(".dxf"):
            continue
        try:
            out[name] = reconstruct(os.path.join(PLANS_DIR, name))
        except ReconstructionError as exc:
            out[name] = exc
    return out


def single_plan(drawing):
    """The one storey of the one building a single-plan drawing must yield.

    Every fixture in this module is one building of one storey, so this is an
    assertion as much as an accessor: a plan that came back as two buildings,
    or as a building with a phantom second storey, fails here.
    """
    assert len(drawing.buildings) == 1, \
        "expected one building, got %d" % len(drawing.buildings)
    levels = drawing.buildings[0].levels
    assert len(levels) == 1, "expected one storey, got %d" % len(levels)
    assert drawing.level_structure["status"] == "SINGLE_LEVEL"
    return levels[0]


def ok(built, name):
    b = built.get(name)
    if b is None:
        pytest.skip("fixture %s not generated" % name)
    if isinstance(b, ReconstructionError):
        pytest.fail("%s failed to reconstruct: %s" % (name, b.failures or b))
    return single_plan(b)


def assert_rect_plan(b, *, thickness=None, tol=0.35):
    """The shared three-room plan, whatever unit it was drawn in."""
    assert b.width == pytest.approx(RECT["width"], abs=tol), "plan width"
    assert b.depth == pytest.approx(RECT["depth"], abs=tol), "plan depth"
    assert len(b.rooms) == RECT["rooms"]
    assert {r.label for r in b.rooms if r.label} == RECT["names"]
    if thickness is not None:
        got = sorted({round(w.thickness, 3) for w in b.walls})
        assert any(abs(t - thickness) < 0.02 for t in got), \
            "expected a %.3f m wall among %s" % (thickness, got)


# ---------------------------------------------------------------------------
# Test 1-14: the generated set
# ---------------------------------------------------------------------------

class TestGeneratedPlans:
    def test_01_simple_rectangle(self, built):
        b = ok(built, "t01_simple_rect.dxf")
        assert_rect_plan(b, thickness=0.15)
        assert b.floor_area == pytest.approx(66.0, rel=0.06)
        s = b.summary()
        assert s["doors"] == RECT["doors"]
        assert s["windows"] == RECT["windows"]

    def test_02_l_shape(self, built):
        b = ok(built, "t02_l_shape.dxf")
        assert b.width == pytest.approx(12.0, abs=0.35)
        assert b.depth == pytest.approx(9.0, abs=0.35)
        assert len(b.rooms) == 3
        # 12x9 less the 5x4 bite: the footprint must not be the bounding box.
        assert b.footprint_area == pytest.approx(88.0, rel=0.12)
        assert b.footprint_area < b.width * b.depth * 0.95
        assert len(b.footprint) >= 6

    def test_03_many_rooms(self, built):
        b = ok(built, "t03_multi_room.dxf")
        names = {r.label for r in b.rooms if r.label}
        assert {"BEDROOM 1", "BEDROOM 2", "BEDROOM 3"} <= names
        assert len([r for r in b.rooms if r.room_type == "bedroom"]) == 3
        assert len([r for r in b.rooms if r.room_type == "bathroom"]) == 2
        assert any(r.room_type == "circulation" for r in b.rooms)

    def test_04_garage_and_porch(self, built):
        b = ok(built, "t04_garage_porch.dxf")
        garages = [r for r in b.rooms if r.room_type == "garage"]
        assert len(garages) == 1
        assert garages[0].area == pytest.approx(6.0 * 8.0, rel=0.15)
        assert b.width == pytest.approx(18.0, abs=0.6), "the garage wing counts"
        # The 4.8 m garage door is a hole in the garage's front wall, whatever
        # the drawing's own layer chooses to call it.
        wide = [o for o in b.openings if o.width > 3.0]
        assert wide, "the garage door must be an opening"
        assert any(b.wall(o.wall_id).kind == "exterior" for o in wide)

    def test_05_irregular_footprint(self, built):
        b = ok(built, "t05_irregular.dxf")
        assert len(b.footprint) >= 10, "a T-shape with a bay is not a rectangle"
        assert b.footprint_area < b.width * b.depth * 0.85
        assert len(b.rooms) >= 3

    def test_06_dimensions_never_become_walls(self, built):
        """The decisive test. Same plan, buried in dimension strings."""
        b = ok(built, "t06_dimension_heavy.dxf")
        clean = ok(built, "t01_simple_rect.dxf")
        assert_rect_plan(b, thickness=0.15)
        assert len(b.walls) == len(clean.walls)
        assert b.total_wall_length == pytest.approx(clean.total_wall_length,
                                                    rel=0.05)
        assert b.width == pytest.approx(clean.width, abs=0.05), \
            "dimension lines outside the building must not stretch the plan"

    def test_07_wall_geometry_across_several_layers(self, built):
        b = ok(built, "t07_many_layers.dxf")
        assert_rect_plan(b, thickness=0.15)
        assert not any(w.length > 11.0 for w in b.walls), \
            "service runs must not become walls"

    def test_08_walls_only_inside_nested_blocks(self, built):
        b = ok(built, "t08_blocks.dxf")
        assert len(b.walls) >= 4
        assert b.width == pytest.approx(8.0, abs=0.35)
        assert b.depth == pytest.approx(6.0, abs=0.35)
        assert len(b.rooms) == 2
        assert {r.label for r in b.rooms if r.label} == {"BEDROOM", "LIVING"}

    def test_09_inches(self, built):
        b = ok(built, "t09_feet_inches.dxf")
        assert b.units.unit_name == "inches"
        assert_rect_plan(b, thickness=0.1016)

    def test_10_millimetres(self, built):
        b = ok(built, "t10_millimetres.dxf")
        assert b.units.unit_name == "millimetres"
        assert_rect_plan(b, thickness=0.23)

    def test_11_metres(self, built):
        b = ok(built, "t11_metres.dxf")
        assert b.units.unit_name == "metres"
        assert_rect_plan(b, thickness=0.20)

    def test_12_a_header_that_lies(self, built):
        """Drawn in inches, declares millimetres — the failing plan's defect."""
        b = ok(built, "t12_lying_header.dxf")
        assert b.units.unit_name == "inches"
        assert b.units.insunits == 4
        assert b.units.conflict, "the disagreement must be reported"
        assert_rect_plan(b, thickness=0.1016)

    def test_13_single_line_walls(self, built):
        b = ok(built, "t13_single_line.dxf")
        assert b.width == pytest.approx(10.0, abs=0.2)
        assert len(b.rooms) == 3
        assert built["t13_single_line.dxf"].stats["walls"]["method"] == "single-line"

    def test_14_a_rotated_sheet(self, built):
        """23 degrees off axis: the same building, differently oriented."""
        b = ok(built, "t14_rotated.dxf")
        upright = ok(built, "t01_simple_rect.dxf")
        assert len(b.rooms) == len(upright.rooms)
        assert b.floor_area == pytest.approx(upright.floor_area, rel=0.08)
        assert b.total_wall_length == pytest.approx(
            upright.total_wall_length, rel=0.08)


# ---------------------------------------------------------------------------
# Test 15: the drawing that failed, as a permanent regression fixture
# ---------------------------------------------------------------------------

class TestTheFailedPlan:
    """``residential_us.dxf`` — the real drawing the desktop test rejected.

    The numbers below are the drawing's own: a 67 x 47 ft house with 2x4 and
    2x6 framing, whose ``$INSUNITS`` claims millimetres. The old engine made
    it 0.80 x 0.56 m with 87 disconnected slabs and no rooms at all.
    """

    @pytest.fixture(scope="class")
    def b(self):
        return single_plan(reconstruct(plan("residential_us.dxf")))

    def test_the_unit_is_read_from_the_geometry_not_the_header(self, b):
        assert b.units.unit_name == "inches"
        assert b.units.insunits == 4, "the file really does declare millimetres"
        assert b.units.conflict
        assert b.units.confidence > 0.8

    def test_the_building_is_house_sized(self, b):
        assert b.width == pytest.approx(20.2, abs=0.6)     # ~66 ft
        assert b.depth == pytest.approx(14.3, abs=0.6)     # ~47 ft

    def test_framing_thicknesses_are_measured(self, b):
        got = sorted({round(w.thickness, 3) for w in b.walls})
        assert any(abs(t - 0.102) < 0.01 for t in got), "2x4 framing"
        assert any(abs(t - 0.152) < 0.01 for t in got), "2x6 framing"
        assert all(0.05 <= t <= 0.4 for t in got)

    def test_the_rooms_of_the_drawing_are_found(self, b):
        names = {(r.label or "").upper() for r in b.rooms}
        for expected in ("MASTER BEDROOM", "BEDROOM 2", "BEDROOM 3", "KITCHEN",
                         "GREAT ROOM", "DINING ROOM", "2 CAR GARAGE", "ENTRY",
                         "HALL", "UTIL.", "BATH", "MSTR. BATH"):
            assert any(expected in n for n in names), \
                "%s is missing from %s" % (expected, sorted(names))

    def test_the_garage_is_garage_sized(self, b):
        garage = next(r for r in b.rooms if r.room_type == "garage")
        assert garage.area == pytest.approx(41.0, rel=0.2)   # ~440 sq ft

    def test_bedrooms_are_bedroom_sized(self, b):
        beds = [r for r in b.rooms if r.room_type == "bedroom"]
        assert len(beds) >= 3
        assert all(9.0 <= r.area <= 25.0 for r in beds), \
            [round(r.area, 1) for r in beds]

    def test_the_porch_and_deck_survive_as_exterior_space(self, b):
        exterior = [r for r in b.rooms if r.is_exterior]
        types = {r.room_type for r in exterior}
        assert "porch" in types
        assert "deck" in types

    def test_the_footprint_is_not_a_rectangle(self, b):
        assert len(b.footprint) > 4
        assert b.footprint_area < b.width * b.depth * 0.95

    def test_the_floor_area_is_right_for_the_house(self, b):
        # ~1,900 sq ft of conditioned space plus porch and deck.
        assert 150.0 <= b.floor_area <= 200.0

    def test_doors_and_windows_are_found(self, b):
        s = b.summary()
        assert s["doors"] >= 8
        assert s["windows"] >= 5
        assert all(o.width <= b.wall(o.wall_id).length + 1e-6
                   for o in b.openings)

    def test_the_garage_door_is_sixteen_feet(self, b):
        wide = [o for o in b.openings if o.width > 4.0]
        assert wide, "a 2-car garage has a garage door"
        assert max(o.width for o in wide) == pytest.approx(4.88, abs=0.4)

    def test_the_walls_are_one_connected_structure(self, b):
        stray = b.validation["checks"].get("wall_length_enclosing_nothing_m", 0.0)
        assert stray / b.total_wall_length < 0.15

    def test_no_dimension_string_became_a_wall(self, b):
        diag = math.hypot(b.width, b.depth)
        assert all(w.length < diag for w in b.walls)
        assert b.validation["checks"].get("annotation_suspects", 0) <= 2

    def test_it_validates(self, b):
        assert b.validation["ok"], b.validation["errors"]

    def test_it_reconstructs_quickly(self):
        t = time.perf_counter()
        reconstruct(plan("residential_us.dxf"))
        assert time.perf_counter() - t < 15.0


# ---------------------------------------------------------------------------
# Determinism, and the legacy document
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_two_runs_give_the_same_building(self):
        a = single_plan(reconstruct(plan("t01_simple_rect.dxf")))
        b = single_plan(reconstruct(plan("t01_simple_rect.dxf")))
        assert a.as_dict()["walls"] == b.as_dict()["walls"]
        assert a.as_dict()["rooms"] == b.as_dict()["rooms"]

    def test_it_needs_no_api_key(self, monkeypatch):
        """The geometry engine must never reach for a credential."""
        for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        b = single_plan(reconstruct(plan("t01_simple_rect.dxf")))
        assert len(b.rooms) == 3


class TestGeometryJsonCompat:
    def test_the_legacy_document_is_projected_from_the_building(self):
        b = reconstruct(plan("t01_simple_rect.dxf"))
        geo = compat.to_geometry_json(b)
        assert geo["metadata"]["segment_count"] == len(b.walls)
        assert len(geo["walls"]) == len(b.walls)
        assert all({"start", "end"} <= set(w) for w in geo["walls"])
        assert geo["metadata"]["units"] == "meters"
        assert geo["metadata"]["scale_factor"] == b.units.scale_to_m

    def test_room_frames_can_still_be_built_from_it(self):
        from vision.grounding import build_room_frame
        b = reconstruct(plan("t01_simple_rect.dxf"))
        frame = build_room_frame(compat.to_geometry_json(b), 2.7)
        assert frame.walls
        assert frame.bounds_max[0] > frame.bounds_min[0]
