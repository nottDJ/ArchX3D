"""Validation: the gate that refuses to build garbage.

The failure this engine replaces produced a *valid GLB* of a 0.8 m building.
Every test here is a shape of that failure, and every one must be rejected
before Blender is ever invoked.
"""

from __future__ import annotations

import pytest

from modules.recon import validate as V
from modules.recon.ir import (Level, Node, Opening, ReconstructionError,
                              Room, UnitDecision, Wall)


def good_building(**over):
    """A 10 x 8 m single room that passes everything."""
    walls = [
        Wall(id="w1", start=(0, 0), end=(10, 0), thickness=0.2, kind="exterior"),
        Wall(id="w2", start=(10, 0), end=(10, 8), thickness=0.2, kind="exterior"),
        Wall(id="w3", start=(10, 8), end=(0, 8), thickness=0.2, kind="exterior"),
        Wall(id="w4", start=(0, 8), end=(0, 0), thickness=0.2, kind="exterior"),
    ]
    room = Room(id="r1", polygon=[(0.1, 0.1), (9.9, 0.1), (9.9, 7.9), (0.1, 7.9)],
                area=76.4, centroid=(5.0, 4.0), label="LIVING",
                room_type="living", boundary_wall_ids=[w.id for w in walls])
    nodes = [
        Node(id="n1", point=(0, 0), wall_ids=["w1", "w4"]),
        Node(id="n2", point=(10, 0), wall_ids=["w1", "w2"]),
        Node(id="n3", point=(10, 8), wall_ids=["w2", "w3"]),
        Node(id="n4", point=(0, 8), wall_ids=["w3", "w4"]),
    ]
    b = Level(
        source_path="test.dxf", walls=walls, rooms=[room], nodes=nodes,
        footprint=[(0, 0), (10, 0), (10, 8), (0, 8)],
        bounds_min=(0.0, 0.0), bounds_max=(10.0, 8.0),
        units=UnitDecision(scale_to_m=1.0, unit_name="metres", method="evidence",
                           confidence=0.95, reason="test"),
    )
    for key, value in over.items():
        setattr(b, key, value)
    return b


class TestAccepts:
    def test_a_sound_building_passes(self):
        report = V.validate(good_building())
        assert report.ok, report.errors

    def test_warnings_do_not_block_a_build(self):
        b = good_building()
        b.units.conflict = "$INSUNITS disagrees"
        report = V.validate(b)
        assert report.ok
        assert report.warnings


class TestRejectsScale:
    def test_a_building_the_size_of_a_shoebox(self):
        """The exact failure: units resolved 25x too small."""
        b = good_building(bounds_max=(0.8, 0.56))
        for w in b.walls:
            w.start = (w.start[0] * 0.08, w.start[1] * 0.08)
            w.end = (w.end[0] * 0.08, w.end[1] * 0.08)
        report = V.validate(b)
        assert not report.ok
        assert any("too small" in e for e in report.errors)

    def test_a_building_the_size_of_a_town(self):
        b = good_building(bounds_max=(4000.0, 3000.0))
        report = V.validate(b)
        assert not report.ok
        assert any("too large" in e for e in report.errors)


class TestRejectsBadGeometry:
    def test_no_walls(self):
        b = good_building(walls=[], rooms=[])
        assert not V.validate(b).ok

    def test_no_rooms(self):
        b = good_building(rooms=[])
        report = V.validate(b)
        assert not report.ok
        assert any("no rooms" in e for e in report.errors)

    def test_a_wall_longer_than_the_whole_plan(self):
        """A 50-foot dimension string is not a 50-foot wall."""
        b = good_building()
        b.walls.append(Wall(id="w9", start=(-20, -20), end=(60, 60),
                            thickness=0.2))
        report = V.validate(b)
        assert not report.ok
        assert any("longer than the whole plan" in e for e in report.errors)

    def test_an_impossible_wall_thickness(self):
        b = good_building()
        b.walls[0].thickness = 2.5
        report = V.validate(b)
        assert not report.ok
        assert any("impossible thickness" in e for e in report.errors)

    def test_overlapping_rooms(self):
        b = good_building()
        b.rooms.append(Room(id="r2", polygon=[(1, 1), (8, 1), (8, 7), (1, 7)],
                            area=42.0, centroid=(4.5, 4.0),
                            boundary_wall_ids=["w1"]))
        report = V.validate(b)
        assert not report.ok
        assert any("overlap" in e for e in report.errors)

    def test_an_opening_that_does_not_fit_its_wall(self):
        b = good_building()
        b.openings.append(Opening(id="o1", kind="door", wall_id="w1",
                                  position=(5, 0), offset=5.0, width=14.0,
                                  height=2.0, sill_height=0.0, thickness=0.2))
        report = V.validate(b)
        assert not report.ok
        assert any("do not fit" in e for e in report.errors)

    def test_an_opening_on_a_wall_that_does_not_exist(self):
        b = good_building()
        b.openings.append(Opening(id="o1", kind="door", wall_id="nope",
                                  position=(5, 0), offset=5.0, width=0.9,
                                  height=2.0, sill_height=0.0, thickness=0.2))
        assert not V.validate(b).ok


class TestConnectivity:
    def test_wall_that_encloses_nothing_is_rejected(self):
        b = good_building()
        for i in range(4):
            b.walls.append(Wall(id="x%d" % i, start=(30 + i, 30),
                                end=(30 + i, 60), thickness=0.2))
        report = V.validate(b)
        assert not report.ok
        assert any("encloses no room" in e for e in report.errors)

    def test_a_second_building_on_the_sheet_is_only_a_warning(self):
        """A detached garage is two islands and both are real."""
        b = good_building()
        garage = [
            Wall(id="g1", start=(20, 0), end=(26, 0), thickness=0.2),
            Wall(id="g2", start=(26, 0), end=(26, 6), thickness=0.2),
            Wall(id="g3", start=(26, 6), end=(20, 6), thickness=0.2),
            Wall(id="g4", start=(20, 6), end=(20, 0), thickness=0.2),
        ]
        b.walls.extend(garage)
        b.rooms.append(Room(id="r2", polygon=[(20.1, 0.1), (25.9, 0.1),
                                              (25.9, 5.9), (20.1, 5.9)],
                            area=33.6, centroid=(23.0, 3.0), label="GARAGE",
                            room_type="garage",
                            boundary_wall_ids=[w.id for w in garage]))
        b.bounds_max = (26.0, 8.0)
        b.footprint_parts = [b.footprint, [(20, 0), (26, 0), (26, 6), (20, 6)]]
        report = V.validate(b)
        assert report.ok, report.errors
        assert any("disconnected groups" in w for w in report.warnings)


class TestEnforce:
    def test_enforce_raises_with_the_diagnostics_attached(self):
        b = good_building(rooms=[])
        with pytest.raises(ReconstructionError) as excinfo:
            V.enforce(b, diagnostics={"stage": "test"})
        err = excinfo.value
        assert err.stage == "validate"
        assert err.failures
        assert "validation" in err.diagnostics

    def test_enforce_returns_the_building_when_it_passes(self):
        b = good_building()
        assert V.enforce(b) is b
        assert b.validation["ok"] is True
