"""What the pipeline recognises, and what it refuses to invent.

Both halves of this file exist because a plausible-sounding fix was the wrong
one, and the tests pin the reasoning so it is not undone later.
"""

import pytest

from vision import catalog, rooms


class TestSanitaryFixtures:
    """Bathrooms were furnishing empty because the vocabulary had no fixtures.

    ``cad.blocks`` has recognised ``toilet`` and ``bathtub`` from the start, so
    the drawing's own fixtures were being parsed and then dropped one step
    later by a vocabulary that disagreed with it.
    """

    @pytest.mark.parametrize("label,expected", [
        ("toilet", "toilet"),
        ("WC", "toilet"),
        ("water closet", "toilet"),
        ("commode", "toilet"),
        ("sink", "sink"),
        ("washbasin", "sink"),
        ("wash basin", "sink"),
        ("basin", "sink"),
        ("vanity", "sink"),
        ("bathtub", "bathtub"),
        ("bath", "bathtub"),
        ("shower", "shower"),
        ("shower cubicle", "shower"),
    ])
    def test_fixtures_are_recognised(self, label, expected):
        assert catalog.normalise_category(label) == expected

    def test_both_spellings_of_a_basin_agree(self):
        """A drawing set mixes British and American naming freely."""
        assert catalog.normalise_category("wash hand basin") == \
            catalog.normalise_category("sink")

    def test_fixtures_stand_against_a_wall(self):
        """No sanitary fixture is ever placed centrally in a room."""
        for name in ("toilet", "sink", "bathtub", "shower"):
            assert catalog.get_prior(name).wall_affinity == 1.0


class TestWhatMustStillBeDropped:
    """The vocabulary gap was real; the proposed cure was worse than it.

    A blanket fuzzy fallback onto generic proxies would have mapped these too,
    and every one of them would put furniture in the building that nobody
    observed.
    """

    def test_unknown_is_not_a_category(self):
        """It is the model saying it could not tell — absence, not a value.

        This was the single largest group of dropped detections. Mapping it to
        a proxy mesh would manufacture furniture out of the model's own
        admission that it did not recognise anything.
        """
        assert catalog.normalise_category("unknown") is None
        assert catalog.normalise_category("unidentified") is None

    def test_site_plan_content_is_not_furniture(self):
        """Cars appear on plans. They are not in the building."""
        assert catalog.normalise_category("car") is None

    def test_an_empty_label_is_not_a_category(self):
        assert catalog.normalise_category("") is None
        assert catalog.normalise_category("   ") is None


class TestDoorwaySealing:
    """The gap-closing dilation seals doorways before the flood fill.

    The direction is counter-intuitive and worth pinning: *raising* this is
    the safe move. An unsealed doorway does not simply merge two rooms — the
    merged region usually reaches an external opening, touches the grid
    border, and is discarded as exterior. Under-sealing deletes floor area.
    """

    def _plan(self, gap: float):
        """Two 4x4 m rooms sharing a wall with a 1.2 m doorway in it."""
        walls = []

        def run(x0, y0, x1, y1):
            walls.append({"start": [x0, y0], "end": [x1, y1]})

        # Outer shell of an 8 x 4 m building.
        run(0, 0, 8, 0)
        run(8, 0, 8, 4)
        run(8, 4, 0, 4)
        run(0, 4, 0, 0)
        # Party wall at x = 4, with a 1.2 m doorway between y = 1.4 and 2.6.
        run(4, 0, 4, 1.4)
        run(4, 2.6, 4, 4)
        return rooms.segment_rooms(walls, gap_closing_m=gap)

    def test_a_wide_doorway_needs_a_wide_seal(self):
        """Below the doorway width the two rooms flood into one another."""
        narrow = self._plan(0.8).regions
        wide = self._plan(1.4).regions
        assert len(wide) > len(narrow), (
            "raising the seal past the doorway width should separate the rooms"
        )

    def test_the_default_clears_a_standard_double_door(self):
        """The reference drawing carries a 1.25 m double door.

        A default at or below that leaves it unsealed, which is exactly the
        merge that produced a 47.7 m2 'bedroom' from two 23 m2 ones.
        """
        assert rooms.DEFAULT_GAP_CLOSING > 1.25

    def test_the_default_stays_below_an_open_plan_threshold(self):
        """It must not seal an opening meant to read as one space."""
        assert rooms.DEFAULT_GAP_CLOSING < 2.0
