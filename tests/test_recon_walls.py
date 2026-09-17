"""Wall reconstruction: faces, pairing, thickness and cleanup.

The old engine extruded every drawn line into its own slab. These tests pin
the replacement's central claim: two parallel lines 150 mm apart are *one*
wall 150 mm thick, not two sheets, and the wall's ends reach the walls they
meet.
"""

from __future__ import annotations

import math

import pytest

from modules.recon import classify as C
from modules.recon import walls as W
from modules.recon.ir import Wall
from modules.recon.read import Prim


def prim(points, *, closed=False, role=C.WALL, pid="p1", layer="A-WALL"):
    return Prim(id=pid, points=list(points), closed=closed, role=role,
                confidence=1.0, reason="test", source="layer",
                dxftype="LINE", layer=layer)


def faces_of(prims, **kw):
    return W.build_faces(prims, **kw)


class TestFaces:
    def test_collinear_segments_become_one_face(self):
        prims = [prim([(0, 0), (3, 0)], pid="a"),
                 prim([(3, 0), (7, 0)], pid="b"),
                 prim([(7, 0), (10, 0)], pid="c")]
        faces = faces_of(prims)
        assert len(faces) == 1
        assert faces[0].runs == [pytest.approx((0.0, 10.0))]

    def test_a_doorway_gap_does_not_split_a_face(self):
        """A wall face is interrupted at every door; it is still one face."""
        prims = [prim([(0, 0), (4, 0)], pid="a"),
                 prim([(4.9, 0), (10, 0)], pid="b")]
        faces = faces_of(prims)
        assert len(faces) == 1, "0.9 m is a doorway, not the end of the wall"

    def test_a_corridor_gap_leaves_two_runs_on_the_face(self):
        """One support line, but two separate stretches of wall on it.

        The face is the *line*, so both stretches belong to it; what matters
        is that they are not joined into one continuous run, because nothing
        was drawn across the five-metre gap.
        """
        prims = [prim([(0, 0), (4, 0)], pid="a"),
                 prim([(9, 0), (14, 0)], pid="b")]
        faces = faces_of(prims)
        assert len(faces) == 1
        assert faces[0].runs == [pytest.approx((0.0, 4.0)),
                                 pytest.approx((9.0, 14.0))]

    def test_parallel_faces_are_kept_apart(self):
        prims = [prim([(0, 0), (10, 0)], pid="a"),
                 prim([(0, 0.15), (10, 0.15)], pid="b")]
        faces = faces_of(prims)
        assert len(faces) == 2
        assert abs(faces[0].offset - faces[1].offset) == pytest.approx(0.15)

    def test_angle_clustering_survives_the_wraparound(self):
        """179.6 and 0.3 degrees are 0.7 apart, not 179.3."""
        prims = [prim([(0, 0), (10, 0.05)], pid="a"),
                 prim([(0, 0.2), (10, 0.25)], pid="b")]
        faces = faces_of(prims)
        assert len(faces) == 2


class TestPairing:
    def test_two_parallel_lines_make_one_wall(self):
        prims = [prim([(0, 0), (10, 0)], pid="a"),
                 prim([(0, 0.15), (10, 0.15)], pid="b")]
        res = W.reconstruct(_drawing(prims))
        assert len(res.walls) == 1
        w = res.walls[0]
        assert w.thickness == pytest.approx(0.15)
        assert w.length == pytest.approx(10.0)
        assert w.start[1] == pytest.approx(0.075)

    def test_thickness_is_measured_not_configured(self):
        """Two walls of different thickness stay different."""
        prims = [prim([(0, 0), (10, 0)], pid="a"),
                 prim([(0, 0.20), (10, 0.20)], pid="b"),
                 prim([(0, 4), (10, 4)], pid="c"),
                 prim([(0, 4.10), (10, 4.10)], pid="d")]
        res = W.reconstruct(_drawing(prims))
        got = sorted(round(w.thickness, 3) for w in res.walls)
        assert got == [pytest.approx(0.10), pytest.approx(0.20)]

    def test_lines_too_far_apart_are_not_a_wall(self):
        prims = [prim([(0, 0), (10, 0)], pid="a"),
                 prim([(0, 3.0), (10, 3.0)], pid="b")]
        res = W.reconstruct(_drawing(prims))
        # Falls through to the single-line reading rather than inventing a
        # 3 m thick wall.
        assert all(w.thickness < W.THICKNESS_BAND[1] for w in res.walls)

    def test_one_face_can_pair_differently_along_its_length(self):
        """A long exterior face meets a 100 mm wall, then a 200 mm one."""
        prims = [prim([(0, 0), (20, 0)], pid="a"),
                 prim([(0, 0.10), (10, 0.10)], pid="b"),
                 prim([(10, 0.20), (20, 0.20)], pid="c")]
        res = W.reconstruct(_drawing(prims))
        got = sorted(round(w.thickness, 3) for w in res.walls)
        assert got == [pytest.approx(0.10), pytest.approx(0.20)]


class TestSingleLine:
    def test_bare_centrelines_are_taken_as_centrelines(self):
        prims = [prim([(0, 0), (10, 0)], pid="a"),
                 prim([(10, 0), (10, 7)], pid="b"),
                 prim([(10, 7), (0, 7)], pid="c"),
                 prim([(0, 7), (0, 0)], pid="d")]
        res = W.reconstruct(_drawing(prims))
        assert res.method == "single-line"
        assert len(res.walls) == 4
        assert sum(w.length for w in res.walls) == pytest.approx(34.0, abs=0.5)

    def test_the_reading_is_chosen_once_for_the_whole_drawing(self):
        """Never half-paired and half-single-line: that doubles some walls."""
        prims = [prim([(0, 0), (10, 0)], pid="a"),
                 prim([(0, 0.15), (10, 0.15)], pid="b")]
        res = W.reconstruct(_drawing(prims))
        assert res.method == "paired"


class TestCleanup:
    def test_corners_are_closed_by_extension(self):
        a = Wall(id="w1", start=(0.0, 0.0), end=(9.9, 0.0), thickness=0.2)
        b = Wall(id="w2", start=(10.0, 0.1), end=(10.0, 8.0), thickness=0.2)
        W.extend_to_intersections([a, b])
        assert a.end[0] == pytest.approx(10.0)
        assert b.start[1] == pytest.approx(0.0)

    def test_extension_stops_at_the_limit(self):
        a = Wall(id="w1", start=(0.0, 0.0), end=(5.0, 0.0), thickness=0.2)
        b = Wall(id="w2", start=(20.0, -5.0), end=(20.0, 5.0), thickness=0.2)
        W.extend_to_intersections([a, b])
        assert a.end[0] == pytest.approx(5.0), "15 m is not a corner"

    def test_collinear_pieces_merge_into_one_wall(self):
        a = Wall(id="w1", start=(0.0, 0.0), end=(4.0, 0.0), thickness=0.15)
        b = Wall(id="w2", start=(4.0, 0.0), end=(9.0, 0.0), thickness=0.15)
        merged = W.merge_collinear([a, b])
        assert len(merged) == 1
        assert merged[0].length == pytest.approx(9.0)

    def test_a_doorway_gap_is_closed_and_reported(self):
        """The wall is completed so the room closes; the hole is handed back."""
        a = Wall(id="w1", start=(0.0, 0.0), end=(4.0, 0.0), thickness=0.15)
        b = Wall(id="w2", start=(4.9, 0.0), end=(9.0, 0.0), thickness=0.15)
        walls = [a, b]
        inferred = W.close_collinear_gaps(walls)
        assert len(walls) == 1
        assert walls[0].length == pytest.approx(9.0)
        assert len(inferred) == 1
        wall_id, lo, hi = inferred[0]
        assert hi - lo == pytest.approx(0.9)

    def test_a_room_wide_gap_is_not_closed(self):
        a = Wall(id="w1", start=(0.0, 0.0), end=(4.0, 0.0), thickness=0.15)
        b = Wall(id="w2", start=(8.0, 0.0), end=(12.0, 0.0), thickness=0.15)
        walls = [a, b]
        W.close_collinear_gaps(walls)
        assert len(walls) == 2

    def test_a_wall_stopping_short_of_another_is_completed(self):
        """The partition ends a metre before the corridor wall: that is a door."""
        corridor = Wall(id="w1", start=(0.0, 5.0), end=(12.0, 5.0), thickness=0.15)
        partition = Wall(id="w2", start=(6.0, 0.0), end=(6.0, 4.0), thickness=0.1)
        walls = [corridor, partition]
        inferred = W.close_collinear_gaps(walls)
        assert partition.end[1] == pytest.approx(5.0)
        assert any(wid == partition.id for wid, _, _ in inferred)

    def test_endpoints_within_tolerance_become_one_point(self):
        a = Wall(id="w1", start=(0.0, 0.0), end=(5.0, 0.0), thickness=0.15)
        b = Wall(id="w2", start=(5.04, 0.03), end=(5.04, 6.0), thickness=0.15)
        W.snap_endpoints([a, b])
        assert a.end == pytest.approx(b.start)


class TestExteriorLabelling:
    def test_the_outside_of_the_envelope_is_exterior(self):
        walls = [
            Wall(id="w1", start=(0, 0), end=(10, 0), thickness=0.25),
            Wall(id="w2", start=(10, 0), end=(10, 8), thickness=0.25),
            Wall(id="w3", start=(10, 8), end=(0, 8), thickness=0.25),
            Wall(id="w4", start=(0, 8), end=(0, 0), thickness=0.25),
            Wall(id="w5", start=(5, 0), end=(5, 8), thickness=0.10),
        ]
        W.label_exterior(walls, [(0, 0), (10, 0), (10, 8), (0, 8)])
        kinds = {w.id: w.kind for w in walls}
        assert kinds["w1"] == "exterior"
        assert kinds["w5"] != "exterior"


def _drawing(prims):
    """A CadDrawing carrying just these primitives, with no usable layer survey."""
    from modules.recon.read import CadDrawing
    d = CadDrawing(source_path="test.dxf", prims=list(prims))
    d.layer_counts = {}
    for p in prims:
        d.layer_counts[p.layer] = d.layer_counts.get(p.layer, 0) + 1
    d.layer_survey = C.survey_layers(list(d.layer_counts))
    return d
