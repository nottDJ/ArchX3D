"""Rooms from topology, and the labels that name them.

The claim under test: a room is a bounded face of the wall graph. Geometry
finds it; text only names it. A plan with no labels still has rooms, and a
label in the wrong place cannot move a wall.
"""

from __future__ import annotations

import pytest

from modules.recon import topology as T
from modules.recon.ir import Wall
from modules.recon.read import Drawing, Label


def wall(i, a, b, t=0.15):
    return Wall(id="w%d" % i, start=a, end=b, thickness=t)


def box_walls(w=10.0, h=8.0, t=0.2):
    return [wall(1, (0, 0), (w, 0), t), wall(2, (w, 0), (w, h), t),
            wall(3, (w, h), (0, h), t), wall(4, (0, h), (0, 0), t)]


def drawing_with(labels):
    d = Drawing(source_path="test.dxf")
    d.labels = [Label(id="t%d" % i, text=text, point=pt, height=0.22,
                      layer="A-ANNO-TEXT")
                for i, (text, pt) in enumerate(labels)]
    return d


class TestRoomsComeFromWalls:
    def test_a_closed_ring_is_one_room(self):
        res = T.extract(box_walls())
        assert len(res.rooms) == 1
        # Centreline face less the wall solids: 9.8 x 7.8 for 200 mm walls.
        assert res.rooms[0].area == pytest.approx(9.8 * 7.8, rel=0.02)

    def test_a_partition_makes_two_rooms(self):
        walls = box_walls() + [wall(5, (4, 0), (4, 8), 0.1)]
        res = T.extract(walls)
        assert len(res.rooms) == 2
        assert sum(r.area for r in res.rooms) == pytest.approx(9.8 * 7.8, rel=0.05)

    def test_an_open_ring_makes_no_room(self):
        """A wall missing from the ring means the space is not enclosed."""
        res = T.extract(box_walls()[:3])
        assert res.rooms == []

    def test_rooms_exist_without_any_labels(self):
        res = T.extract(box_walls() + [wall(5, (4, 0), (4, 8), 0.1)])
        assert len(res.rooms) == 2
        assert all(r.label is None for r in res.rooms)

    def test_an_l_shape_stays_l_shaped(self):
        walls = [
            wall(1, (0, 0), (12, 0)), wall(2, (12, 0), (12, 5)),
            wall(3, (12, 5), (7, 5)), wall(4, (7, 5), (7, 9)),
            wall(5, (7, 9), (0, 9)), wall(6, (0, 9), (0, 0)),
        ]
        res = T.extract(walls)
        assert len(res.rooms) == 1
        # 12x9 minus the 5x4 bite = 88 m^2 gross, less the wall solids.
        assert res.rooms[0].area == pytest.approx(88.0, rel=0.06)
        assert len(res.footprint) >= 6, "the footprint must not be a rectangle"


class TestLabels:
    def test_a_label_inside_a_room_names_it(self):
        walls = box_walls() + [wall(5, (4, 0), (4, 8), 0.1)]
        d = drawing_with([("KITCHEN", (2.0, 4.0)), ("LIVING", (7.0, 4.0))])
        res = T.extract(walls, d)
        names = {r.label for r in res.rooms}
        assert names == {"KITCHEN", "LIVING"}

    def test_room_type_is_derived_from_the_name(self):
        walls = box_walls()
        d = drawing_with([("MSTR. BATH", (5.0, 4.0))])
        res = T.extract(walls, d)
        assert res.rooms[0].room_type == "bathroom"

    def test_a_construction_note_does_not_name_a_room(self):
        walls = box_walls()
        d = drawing_with([('5/8" TYPE X GYP. BD.', (5.0, 4.0))])
        res = T.extract(walls, d)
        assert res.rooms[0].label is None

    def test_abbreviations_survive(self):
        assert T.room_type_of("W.I.C.")[0] == "closet"
        assert T.room_type_of("TOIL.")[0] == "bathroom"
        assert T.room_type_of("UTIL.")[0] == "utility"
        assert T.room_type_of("2 CAR GARAGE")[0] == "garage"

    def test_autocad_formatting_codes_are_stripped(self):
        assert T.clean_label("%%uKITCHEN") == "KITCHEN"
        assert T.clean_label("MASTER\\PBEDROOM") == "MASTER BEDROOM"

    def test_two_lines_of_one_name_are_joined(self):
        walls = box_walls()
        d = drawing_with([("MASTER", (5.0, 4.2)), ("BEDROOM", (5.0, 3.9))])
        res = T.extract(walls, d)
        assert res.rooms[0].label == "MASTER BEDROOM"
        assert res.rooms[0].room_type == "bedroom"

    def test_a_label_cannot_create_a_room(self):
        """Three walls and a label is still not a room."""
        d = drawing_with([("KITCHEN", (5.0, 4.0))])
        res = T.extract(box_walls()[:3], d)
        assert res.rooms == []


class TestOpenPlan:
    def test_two_named_areas_in_one_space_are_split_and_flagged(self):
        walls = box_walls(16.0, 8.0)
        d = drawing_with([("KITCHEN", (3.0, 4.0)), ("DINING ROOM", (13.0, 4.0))])
        res = T.extract(walls, d)
        assert len(res.rooms) == 2
        assert all(r.open_plan for r in res.rooms)
        assert {r.label for r in res.rooms} == {"KITCHEN", "DINING ROOM"}

    def test_one_name_and_one_stray_word_do_not_split_it(self):
        walls = box_walls(16.0, 8.0)
        d = drawing_with([("KITCHEN", (3.0, 4.0)), ("BOLLARD", (13.0, 4.0))])
        res = T.extract(walls, d)
        assert len(res.rooms) == 1
        assert res.rooms[0].label == "KITCHEN"
        assert not res.rooms[0].open_plan


class TestValidatedShapes:
    def test_room_polygons_are_closed_and_simple(self):
        from shapely.geometry import Polygon
        walls = box_walls() + [wall(5, (4, 0), (4, 8), 0.1)]
        res = T.extract(walls)
        for room in res.rooms:
            poly = Polygon(room.polygon)
            assert poly.is_valid
            assert poly.area > 0

    def test_rooms_do_not_overlap(self):
        from shapely.geometry import Polygon
        walls = box_walls() + [wall(5, (4, 0), (4, 8), 0.1)]
        res = T.extract(walls)
        a, b = (Polygon(r.polygon) for r in res.rooms)
        assert a.intersection(b).area == pytest.approx(0.0, abs=1e-6)

    def test_boundary_walls_are_recorded(self):
        res = T.extract(box_walls())
        assert len(res.rooms[0].boundary_wall_ids) == 4
