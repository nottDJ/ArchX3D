"""Doors and windows: found from evidence, hosted by walls, cut as holes."""

from __future__ import annotations

import math

import pytest

from modules.recon import classify as C
from modules.recon import openings as O
from modules.recon.ir import Wall
from modules.recon.read import Arc, BlockRef, CadDrawing, Prim


def prim(points, role, *, closed=False, pid="p1", layer="A-WALL", dxftype="LWPOLYLINE"):
    return Prim(id=pid, points=list(points), closed=closed, role=role,
                confidence=1.0, reason="test", source="layer",
                dxftype=dxftype, layer=layer)


def band_prim(x0, x1, y, thickness, role=C.STRUCTURE_ABOVE, pid="p1",
              layer="A-HEADER"):
    h = thickness / 2.0
    return prim([(x0, y - h), (x1, y - h), (x1, y + h), (x0, y + h)],
                role, closed=True, pid=pid, layer=layer)


class TestEvidence:
    def test_a_header_band_is_an_opening(self):
        d = CadDrawing(prims=[band_prim(3.0, 3.9, 0.0, 0.15)])
        ev = O.collect_evidence(d)
        assert len(ev) == 1
        assert ev[0].width == pytest.approx(0.9)
        assert ev[0].band == pytest.approx(0.15)

    def test_a_room_sized_rectangle_is_not_an_opening(self):
        d = CadDrawing(prims=[band_prim(0.0, 4.0, 0.0, 3.0)])
        assert O.collect_evidence(d) == []

    def test_glazing_lines_cluster_into_one_window(self):
        """Three parallel lines with no rectangle anywhere is still a window."""
        prims = [
            prim([(2.0, y), (3.5, y)], C.WINDOW, pid="g%d" % i,
                 layer="A-GLAZ", dxftype="LINE")
            for i, y in enumerate((-0.05, 0.0, 0.05))
        ]
        ev = O.collect_evidence(CadDrawing(prims=prims))
        assert len(ev) == 1
        assert ev[0].width == pytest.approx(1.5)
        assert ev[0].kind == "window"

    def test_two_windows_a_pier_apart_stay_two(self):
        prims = []
        for k, x0 in enumerate((1.0, 4.0)):
            for i, y in enumerate((-0.05, 0.0, 0.05)):
                prims.append(prim([(x0, y), (x0 + 1.2, y)], C.WINDOW,
                                  pid="g%d%d" % (k, i), layer="A-GLAZ",
                                  dxftype="LINE"))
        assert len(O.collect_evidence(CadDrawing(prims=prims))) == 2

    def test_a_swing_arc_is_a_door(self):
        d = CadDrawing(arcs=[Arc(id="a1", centre=(2.0, 0.0), radius=0.9,
                              start_deg=0.0, end_deg=90.0, role=C.DOOR,
                              layer="A-DOOR")])
        ev = O.collect_evidence(d)
        assert ev and all(e.kind == "door" for e in ev)
        assert all(e.width == pytest.approx(0.9) for e in ev)

    def test_a_swing_offers_both_readings_as_one_group(self):
        """Which chord is the doorway is unknowable without the walls."""
        d = CadDrawing(arcs=[Arc(id="a1", centre=(2.0, 0.0), radius=0.9,
                              start_deg=0.0, end_deg=90.0, role=C.DOOR,
                              layer="A-DOOR")])
        ev = O.collect_evidence(d)
        assert len({e.group for e in ev}) == 1
        assert len(ev) == 2

    def test_evidence_in_different_places_is_not_merged(self):
        """Proximity alone once fused a window with a beam 2.5 m away."""
        d = CadDrawing(prims=[band_prim(0.0, 1.83, 0.0, 0.15, pid="p1"),
                           band_prim(4.0, 8.5, 0.0, 0.10, pid="p2")])
        ev = O.collect_evidence(d)
        assert len(ev) == 2

    def test_a_header_and_a_swing_at_one_doorway_are_one_opening(self):
        d = CadDrawing(
            prims=[band_prim(2.0, 2.9, 0.0, 0.15, pid="p1")],
            arcs=[Arc(id="a1", centre=(2.0, 0.0), radius=0.9, start_deg=0.0,
                      end_deg=90.0, role=C.DOOR, layer="A-DOOR")],
        )
        ev = O.collect_evidence(d)
        doorway = [e for e in ev if abs(e.centre[0] - 2.45) < 0.2
                   and abs(e.centre[1]) < 0.2]
        assert len(doorway) == 1
        assert doorway[0].kind == "door"


class TestHosting:
    def test_an_opening_lands_on_its_wall(self):
        wall = Wall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0), thickness=0.15)
        d = CadDrawing(prims=[band_prim(3.0, 3.9, 0.0, 0.15)])
        ops = O.assign(O.collect_evidence(d), [wall], d)
        assert len(ops) == 1
        assert ops[0].wall_id == "w1"
        assert ops[0].offset == pytest.approx(3.45)
        assert ops[0].width == pytest.approx(0.9)

    def test_an_opening_nowhere_near_a_wall_is_dropped(self):
        wall = Wall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0), thickness=0.15)
        d = CadDrawing(prims=[band_prim(3.0, 3.9, 6.0, 0.15)])
        assert O.assign(O.collect_evidence(d), [wall], d) == []

    def test_a_swing_picks_the_chord_that_lies_in_a_wall(self):
        wall = Wall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0), thickness=0.15)
        d = CadDrawing(arcs=[Arc(id="a1", centre=(2.0, 0.0), radius=0.9,
                              start_deg=0.0, end_deg=90.0, role=C.DOOR,
                              layer="A-DOOR")])
        ops = O.assign(O.collect_evidence(d), [wall], d)
        assert len(ops) == 1, "one swing is one doorway, not two"
        assert ops[0].kind == "door"

    def test_an_opening_never_exceeds_its_wall(self):
        wall = Wall(id="w1", start=(0.0, 0.0), end=(2.0, 0.0), thickness=0.15)
        d = CadDrawing(prims=[band_prim(-1.0, 3.0, 0.0, 0.15)])
        ops = O.assign(O.collect_evidence(d), [wall], d)
        assert ops[0].width <= wall.length + 1e-6

    def test_one_stretch_of_wall_holds_one_opening(self):
        wall = Wall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0), thickness=0.15)
        d = CadDrawing(prims=[band_prim(3.0, 3.9, 0.0, 0.15, pid="p1"),
                           band_prim(3.1, 4.0, 0.0, 0.12, pid="p2",
                                     role=C.OPENING, layer="A-OPENING")])
        ops = O.assign(O.collect_evidence(d), [wall], d)
        assert len(ops) == 1


class TestKinds:
    def test_a_wide_hole_in_an_interior_wall_is_a_cased_opening(self):
        """Width alone once put a roller door between the kitchen and dining."""
        wall = Wall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0),
                    thickness=0.1, kind="interior")
        d = CadDrawing(prims=[band_prim(3.0, 6.5, 0.0, 0.1)])
        ops = O.assign(O.collect_evidence(d), [wall], d)
        assert ops[0].kind == "cased"

    def test_a_wide_hole_in_an_exterior_wall_is_a_garage_door(self):
        wall = Wall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0),
                    thickness=0.15, kind="exterior")
        d = CadDrawing(prims=[band_prim(3.0, 7.9, 0.0, 0.15)])
        ops = O.assign(O.collect_evidence(d), [wall], d)
        assert ops[0].kind == "garage"

    def test_the_opening_is_narrower_than_its_header(self):
        """A header bears on the piers, so it always spans more than the hole."""
        wall = Wall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0),
                    thickness=0.15, kind="exterior")
        d = CadDrawing(prims=[
            band_prim(1.0, 8.0, 0.0, 0.15, pid="p1"),                 # beam
            band_prim(2.0, 6.9, 0.0, 0.15, pid="p2", role=C.OPENING,
                      layer="A-OPENING"),                              # the hole
        ])
        ops = O.assign(O.collect_evidence(d), [wall], d)
        assert len(ops) == 1
        assert ops[0].width == pytest.approx(4.9, abs=0.05)


class TestInferredOpenings:
    def test_a_closed_doorway_gap_becomes_an_opening(self):
        wall = Wall(id="w1", start=(0.0, 0.0), end=(9.0, 0.0), thickness=0.15)
        ops = O.from_inferred([wall], [("w1", 4.0, 4.9)], [])
        assert len(ops) == 1
        assert ops[0].width == pytest.approx(0.9)
        assert ops[0].kind == "cased"
        assert "w1" == ops[0].wall_id
        assert ops[0].id in wall.opening_ids

    def test_it_does_not_double_an_opening_evidence_already_found(self):
        wall = Wall(id="w1", start=(0.0, 0.0), end=(9.0, 0.0), thickness=0.15)
        existing = [O.Opening(id="o1", kind="door", wall_id="w1",
                              position=(4.45, 0.0), offset=4.45, width=0.9,
                              height=2.0, sill_height=0.0, thickness=0.15)]
        assert O.from_inferred([wall], [("w1", 4.0, 4.9)], existing) == []


class TestRoomLinking:
    def test_an_opening_records_the_rooms_it_connects(self):
        from modules.recon.ir import Room
        wall = Wall(id="w1", start=(0.0, 0.0), end=(10.0, 0.0), thickness=0.15)
        op = O.Opening(id="o1", kind="door", wall_id="w1", position=(5.0, 0.0),
                       offset=5.0, width=0.9, height=2.0, sill_height=0.0,
                       thickness=0.15)
        north = Room(id="r1", polygon=[(0, 0.1), (10, 0.1), (10, 6), (0, 6)],
                     area=59.0, centroid=(5.0, 3.0))
        south = Room(id="r2", polygon=[(0, -6), (10, -6), (10, -0.1), (0, -0.1)],
                     area=59.0, centroid=(5.0, -3.0))
        O.link_rooms([op], [north, south], [wall])
        assert set(op.rooms) == {"r1", "r2"}
        assert "o1" in north.opening_ids and "o1" in south.opening_ids
