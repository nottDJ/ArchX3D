"""The 3D construction rules, tested without Blender.

``modules/blender_build.py`` needs bpy to make meshes, but the decisions that
matter — where the solid parts of a wall with three openings are — are plain
arithmetic and are tested here. The old generator's four failures were all
decisions of this kind: doubled slabs, one thickness for every wall, a
bounding-box floor, and doors painted onto solid walls.
"""

from __future__ import annotations

import json
import os

import pytest

from modules.blender_build import bounds, load_building, wall_pieces


class TestWallPieces:
    def test_a_wall_with_no_openings_is_one_piece(self):
        assert wall_pieces(10.0, 2.7, []) == [(0.0, 10.0, 0.0, 2.7)]

    def test_a_door_leaves_piers_and_a_lintel(self):
        """Not a solid wall with a door drawn on it: an actual hole."""
        pieces = wall_pieces(10.0, 2.7, [(4.0, 4.9, 0.0, 2.03)])
        assert (0.0, 4.0, 0.0, 2.7) in pieces
        assert (4.9, 10.0, 0.0, 2.7) in pieces
        assert (4.0, 4.9, 2.03, 2.7) in pieces      # the lintel over the hole
        assert not any(u0 <= 4.4 <= u1 and z0 <= 1.0 <= z1
                       for u0, u1, z0, z1 in pieces), "the doorway must be open"

    def test_a_window_leaves_a_sill_below_and_a_head_above(self):
        pieces = wall_pieces(10.0, 2.7, [(3.0, 4.5, 0.914, 2.03)])
        assert (3.0, 4.5, 0.0, 0.914) in pieces     # under the sill
        assert (3.0, 4.5, 2.03, 2.7) in pieces      # over the head
        assert not any(u0 <= 3.7 <= u1 and z0 <= 1.5 <= z1
                       for u0, u1, z0, z1 in pieces), "the glass line is open"

    def test_several_openings_in_one_wall(self):
        pieces = wall_pieces(12.0, 2.7, [
            (1.0, 2.5, 0.914, 2.03),
            (5.0, 5.9, 0.0, 2.03),
            (8.0, 9.5, 0.914, 2.03),
        ])
        full = [p for p in pieces if p[2] == 0.0 and p[3] == 2.7]
        assert len(full) == 4, "four piers between and around three openings"

    def test_a_full_height_opening_leaves_only_the_piers(self):
        pieces = wall_pieces(10.0, 2.7, [(4.0, 6.0, 0.0, 2.7)])
        assert pieces == [(0.0, 4.0, 0.0, 2.7), (6.0, 10.0, 0.0, 2.7)]

    def test_an_opening_spanning_the_whole_wall_leaves_nothing(self):
        assert wall_pieces(4.9, 2.7, [(0.0, 4.9, 0.0, 2.7)]) == []

    def test_overlapping_openings_become_one_hole(self):
        """Two overlapping holes are one hole, not a zero-width pier."""
        pieces = wall_pieces(10.0, 2.7, [(4.0, 5.5, 0.0, 2.03),
                                         (5.0, 6.5, 0.0, 2.03)])
        assert (0.0, 4.0, 0.0, 2.7) in pieces
        assert (6.5, 10.0, 0.0, 2.7) in pieces
        assert not any(4.0 < p[0] < 6.5 and p[2] == 0.0 for p in pieces)

    def test_an_opening_is_clamped_to_its_wall(self):
        pieces = wall_pieces(3.0, 2.7, [(-2.0, 8.0, 0.0, 2.03)])
        assert all(0.0 <= u0 <= 3.0 and 0.0 <= u1 <= 3.0
                   for u0, u1, _z0, _z1 in pieces)

    def test_slivers_are_dropped(self):
        pieces = wall_pieces(10.0, 2.7, [(0.001, 5.0, 0.0, 2.7)])
        assert all(u1 - u0 > 0.003 for u0, u1, _z0, _z1 in pieces)

    def test_every_piece_has_positive_extent(self):
        pieces = wall_pieces(10.0, 2.7, [(2.0, 3.0, 0.9, 2.0),
                                         (3.0, 4.0, 0.0, 2.0)])
        assert all(u1 > u0 and z1 > z0 for u0, u1, z0, z1 in pieces)


class TestBounds:
    def test_the_footprint_is_included(self):
        building = {
            "walls": [{"id": "w1", "start": [0, 0], "end": [10, 0],
                       "thickness": 0.2}],
            "footprint_parts": [[[0, 0], [10, 0], [10, 8], [0, 8]],
                                [[14, 0], [20, 0], [20, 6], [14, 6]]],
        }
        x0, y0, x1, y1 = bounds(building)
        assert (x0, y0) == (0.0, 0.0)
        assert x1 == 20.0, "a detached garage is part of the extent"
        assert y1 == 8.0


class TestLoading:
    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert load_building(str(tmp_path / "nope.json")) is None

    def test_a_file_that_is_not_a_building_is_rejected(self, tmp_path):
        path = tmp_path / "geometry.json"
        path.write_text(json.dumps({"metadata": {}, "segments": []}))
        assert load_building(str(path)) is None

    def test_a_real_building_loads(self, tmp_path):
        from modules.recon.pipeline import reconstruct
        plans = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "fixtures", "plans", "t01_simple_rect.dxf")
        if not os.path.exists(plans):
            pytest.skip("fixture not generated")
        b = reconstruct(plans)
        path = str(tmp_path / "building.json")
        b.to_json(path)
        loaded = load_building(path)
        assert loaded is not None
        assert len(loaded["walls"]) == len(b.walls)
        assert len(loaded["rooms"]) == len(b.rooms)


class TestAgainstTheRealPlan:
    """Every opening in the failing plan must survive into solid geometry."""

    @pytest.fixture(scope="class")
    def building(self):
        from modules.recon.pipeline import reconstruct
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "plans", "residential_us.dxf")
        if not os.path.exists(path):
            pytest.skip("regression fixture missing")
        return reconstruct(path).as_dict()

    def test_every_wall_produces_geometry(self, building):
        by_wall = {}
        for o in building["openings"]:
            by_wall.setdefault(o["wall_id"], []).append(o)
        built = 0
        for w in building["walls"]:
            spans = [(o["offset"] - o["width"] / 2, o["offset"] + o["width"] / 2,
                      o["sill_height"], o["sill_height"] + o["height"])
                     for o in by_wall.get(w["id"], [])]
            length = ((w["end"][0] - w["start"][0]) ** 2 +
                      (w["end"][1] - w["start"][1]) ** 2) ** 0.5
            pieces = wall_pieces(length, 2.7, spans)
            # A wall may be entirely opening — the garage front nearly is —
            # but the rest must come out solid.
            if pieces:
                built += 1
            assert all(u1 > u0 for u0, u1, _z0, _z1 in pieces)
        assert built >= len(building["walls"]) - 1

    def test_the_openings_really_are_holes(self, building):
        """Sum of solid area is strictly less than the full elevation."""
        total_full = total_solid = 0.0
        by_wall = {}
        for o in building["openings"]:
            by_wall.setdefault(o["wall_id"], []).append(o)
        for w in building["walls"]:
            length = ((w["end"][0] - w["start"][0]) ** 2 +
                      (w["end"][1] - w["start"][1]) ** 2) ** 0.5
            spans = [(o["offset"] - o["width"] / 2, o["offset"] + o["width"] / 2,
                      o["sill_height"], o["sill_height"] + o["height"])
                     for o in by_wall.get(w["id"], [])]
            total_full += length * 2.7
            total_solid += sum((u1 - u0) * (z1 - z0)
                               for u0, u1, z0, z1 in wall_pieces(length, 2.7, spans))
        assert total_solid < total_full, "the walls must have holes in them"
        assert total_solid > total_full * 0.55, "but they must still be walls"
