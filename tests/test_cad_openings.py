"""Doors and windows recovered from the drawing.

The clustering rules here were each written against a real drawing that broke
the previous rule, so the tests below pin the *reasons*, not just the counts.
"""

import math

import pytest

from cad import openings as cad_openings


# ---------------------------------------------------------------------------
# Stand-ins. The module reads attributes, so it does not need real CAD types —
# which is what lets these run with no DXF, no ezdxf and no Blender.
# ---------------------------------------------------------------------------


class FakeBlock:
    def __init__(self, name="900-Door", category="door", position=(0.0, 0.0),
                 rotation=0.0, bounds_min=None, bounds_max=None, layer="DOOR",
                 uid="blk", confidence=0.9):
        self.name = name
        self.category = category
        self.position = position
        self.rotation = rotation
        self.bounds_min = bounds_min
        self.bounds_max = bounds_max
        self.layer = layer
        self.uid = uid
        self.confidence = confidence


class FakeSegment:
    def __init__(self, start, end, role="window"):
        self.start = start
        self.end = end
        self.role = role


class FakeDocument:
    def __init__(self, blocks=(), segments=()):
        self.blocks = list(blocks)
        self.segments = list(segments)


def window_lines(x0, x1, y, thickness=0.229, lines=3):
    """A window drawn the common way: parallel lines, no jambs closing them."""
    step = thickness / (lines - 1)
    return [
        FakeSegment((x0, y + i * step), (x1, y + i * step))
        for i in range(lines)
    ]


# ---------------------------------------------------------------------------


class TestDoors:
    def test_a_door_block_becomes_an_opening(self):
        doc = FakeDocument(blocks=[FakeBlock(
            position=(3.0, 4.0), rotation=90.0,
            bounds_min=(2.55, 3.55), bounds_max=(3.45, 4.45),
        )])
        found = cad_openings.doors_from_blocks(doc)

        assert len(found) == 1
        door = found[0]
        assert door.kind == "door"
        assert door.x == pytest.approx(3.0)
        assert door.rotation_deg == pytest.approx(90.0)
        assert door.width == pytest.approx(0.9, abs=0.01)

    def test_width_comes_from_the_geometry_not_the_name(self):
        """A block named 900 inserted at reduced scale is not a 900 door."""
        doc = FakeDocument(blocks=[FakeBlock(
            name="900-Door", position=(0.0, 0.0),
            bounds_min=(-0.39, -0.39), bounds_max=(0.39, 0.39),
        )])
        assert cad_openings.doors_from_blocks(doc)[0].width == pytest.approx(0.78, abs=0.01)

    def test_a_door_without_usable_bounds_keeps_a_nominal_leaf(self):
        """The doorway is real even when its extent is not readable."""
        doc = FakeDocument(blocks=[FakeBlock(bounds_min=None, bounds_max=None)])
        found = cad_openings.doors_from_blocks(doc)

        assert len(found) == 1, "a doorway must not be dropped for a missing box"
        assert found[0].width == pytest.approx(0.9)

    def test_non_door_blocks_are_ignored(self):
        doc = FakeDocument(blocks=[
            FakeBlock(name="LA-CAR", category="car"),
            FakeBlock(name="SOFA", category="furniture"),
        ])
        assert cad_openings.doors_from_blocks(doc) == []

    def test_height_is_a_convention_and_position_is_measured(self):
        """A plan is a horizontal cut: it cannot state a head height."""
        doc = FakeDocument(blocks=[FakeBlock(
            bounds_min=(-0.45, -0.45), bounds_max=(0.45, 0.45))])
        door = cad_openings.doors_from_blocks(doc)[0]

        assert door.height == cad_openings.DOOR_HEIGHT_M
        assert "position" in door.measured
        assert "height" not in door.measured


class TestWindows:
    def test_parallel_lines_are_one_window(self):
        """The bug that reported every window three times.

        Three parallel lines across a wall have no shared endpoints, so an
        endpoint-proximity join leaves each line its own figure.
        """
        doc = FakeDocument(segments=window_lines(0.0, 1.5, 5.0))
        found = cad_openings.windows_from_segments(doc)

        assert len(found) == 1
        assert found[0].width == pytest.approx(1.5)
        assert found[0].depth == pytest.approx(0.229, abs=0.01)

    def test_a_glazing_line_meeting_a_jamb_midway_still_joins(self):
        """The bug before that one: a centre line meets jambs at their middle."""
        segs = [
            FakeSegment((0.0, 0.0), (0.0, 0.23)),      # left jamb
            FakeSegment((1.2, 0.0), (1.2, 0.23)),      # right jamb
            FakeSegment((0.0, 0.115), (1.2, 0.115)),   # glazing, mid-jamb
        ]
        found = cad_openings.windows_from_segments(FakeDocument(segments=segs))

        assert len(found) == 1
        assert found[0].width == pytest.approx(1.2)

    def test_two_windows_on_one_wall_stay_separate(self):
        """The join tolerance must not swallow the pier between openings."""
        segs = window_lines(0.0, 1.2, 5.0) + window_lines(3.0, 4.2, 5.0)
        found = cad_openings.windows_from_segments(FakeDocument(segments=segs))

        assert len(found) == 2
        assert sorted(round(w.x, 2) for w in found) == [0.6, 3.6]

    def test_orientation_follows_the_long_axis(self):
        across = window_lines(0.0, 2.0, 0.0)
        found = cad_openings.windows_from_segments(FakeDocument(segments=across))
        assert found[0].rotation_deg == pytest.approx(0.0)

        upright = [FakeSegment((y, 0.0), (y, 2.0)) for y in (0.0, 0.115, 0.229)]
        found = cad_openings.windows_from_segments(FakeDocument(segments=upright))
        assert found[0].rotation_deg == pytest.approx(90.0)

    def test_draughting_marks_are_not_windows(self):
        tiny = [FakeSegment((0.0, 0.0), (0.05, 0.0))]
        assert cad_openings.windows_from_segments(FakeDocument(segments=tiny)) == []

    def test_segments_on_other_layers_are_ignored(self):
        walls = [FakeSegment((0.0, 0.0), (5.0, 0.0), role="wall")]
        assert cad_openings.windows_from_segments(FakeDocument(segments=walls)) == []

    def test_a_window_sits_above_the_floor(self):
        doc = FakeDocument(segments=window_lines(0.0, 1.2, 0.0))
        window = cad_openings.windows_from_segments(doc)[0]

        assert window.sill_height == cad_openings.WINDOW_SILL_M
        assert window.sill_height > 0, "a window is not a doorway"


class TestTheWholeDocument:
    def test_doors_and_windows_are_both_recovered(self):
        doc = FakeDocument(
            blocks=[FakeBlock(bounds_min=(-0.45, -0.45), bounds_max=(0.45, 0.45))],
            segments=window_lines(4.0, 5.2, 2.0),
        )
        found = cad_openings.from_document(doc)

        kinds = sorted(o.kind for o in found)
        assert kinds == ["door", "window"]

    def test_an_empty_drawing_yields_nothing_rather_than_failing(self):
        assert cad_openings.from_document(FakeDocument()) == []
