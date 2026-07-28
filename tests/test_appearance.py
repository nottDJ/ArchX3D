"""
Tests for style recognition, material species, palette and lighting reconstruction.

The recurring principle: everything here is *derived from evidence or declared
absent*. A room with no imagery gets no palette rather than a plausible-looking
one, and a value that came from a style prior is labelled differently from one
measured off a photograph.
"""

from __future__ import annotations

import pytest

from vision import appearance, assets, catalog
from vision.schema import (
    ColourPalette,
    Dimensions,
    Finish,
    LightSource,
    Opening,
    Room,
    SceneObject,
    Vec3,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def room() -> Room:
    return Room(
        id="r1",
        room_type="living_room",
        area=30.0,
        bounds_min=(0.0, 0.0),
        bounds_max=(6.0, 5.0),
        wall_finish=Finish(material="paint_matte", color_hex="#EDE7DD", confidence=0.9),
        floor_finish=Finish(material="light_oak", color_hex="#D8B98C", confidence=0.85),
    )


@pytest.fixture
def objects():
    return [
        SceneObject(id="sofa", category="sofa", group="furniture",
                    color_hex="#4A5259", material="linen",
                    dimensions=Dimensions(2.4, 0.95, 0.8)),
        SceneObject(id="table", category="coffee_table", group="furniture",
                    color_hex="#5C4033", material="walnut",
                    dimensions=Dimensions(1.1, 0.6, 0.4)),
        SceneObject(id="vase", category="flower_vase", group="decor",
                    color_hex="#C1683F", material="ceramic",
                    dimensions=Dimensions(0.2, 0.2, 0.3)),
    ]


@pytest.fixture
def lights():
    return [
        LightSource(id="l1", kind="pendant_light", power_w=30.0, color_temperature_k=2700.0),
        LightSource(id="l2", kind="recessed_light", power_w=18.0, color_temperature_k=3200.0),
    ]


# ---------------------------------------------------------------------------
# Material taxonomy
# ---------------------------------------------------------------------------


class TestMaterialSpecies:
    def test_species_declare_their_family(self):
        assert catalog.material_family("walnut") == "wood"
        assert catalog.material_family("white_marble") == "marble"
        assert catalog.material_family("velvet") == "fabric"

    def test_a_family_is_its_own_family(self):
        """Consumers can call this on anything without branching."""
        assert catalog.material_family("wood") == "wood"
        assert catalog.material_family("fabric") == "fabric"

    def test_an_unknown_material_degrades_rather_than_raising(self):
        assert catalog.material_family("unobtainium") == "unknown"

    def test_species_inherit_the_surfaces_their_family_applies_to(self):
        """A walnut floor is legal because a wood floor is."""
        assert set(catalog.get_material("walnut").applies_to) == set(
            catalog.get_material("wood").applies_to
        )
        assert "wall" not in catalog.get_material("velvet").applies_to

    def test_species_carry_distinct_colours(self):
        """The whole point: walnut must not render as generic timber."""
        assert catalog.get_material("walnut").color_hex != catalog.get_material("wood").color_hex
        assert catalog.get_material("white_marble").color_hex != (
            catalog.get_material("black_marble").color_hex
        )

    def test_free_text_resolves_to_a_species(self):
        assert catalog.normalise_material("light oak") == "light_oak"
        assert catalog.normalise_material("white marble") == "white_marble"

    def test_species_are_discoverable_by_family(self):
        wood = catalog.species_of("wood")
        assert "walnut" in wood and "teak" in wood
        assert "wood" not in wood  # a family is not its own species


class TestAssetMaterialScoring:
    def test_a_species_suits_a_variant_declaring_its_family(self):
        """Without family-aware scoring every species would look like a mismatch."""
        assert assets._material_score("walnut", ("wood",)) > 0.9
        assert assets._material_score("velvet", ("fabric",)) > 0.9

    def test_an_unrelated_species_still_scores_low(self):
        assert assets._material_score("walnut", ("fabric",)) < 0.5

    def test_an_exact_name_still_wins_outright(self):
        assert assets._material_score("wood", ("wood",)) == 1.0


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------


class TestStyle:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Modern", "modern"),
            ("mid-century modern", "mid_century"),
            ("warm scandi living space", "scandinavian"),
            ("loft / warehouse", "industrial"),
            ("japandi", "japanese"),
            ("wabi-sabi", "japanese"),
            ("", "unknown"),
            ("nonsense words here", "unknown"),
        ],
    )
    def test_free_text_resolves(self, raw, expected):
        assert catalog.normalise_style(raw) == expected

    def test_a_compound_style_is_not_reduced_to_its_last_word(self):
        """"mid-century modern" is its own style, not "modern"."""
        assert catalog.normalise_style("mid-century modern") == "mid_century"

    def test_confidence_rises_when_materials_agree(self, objects):
        """A style claim corroborated by the surfaces is worth more."""
        agreeing = appearance.resolve_style("mid-century", objects, ["light_oak", "walnut"])
        disagreeing = appearance.resolve_style("industrial", objects, ["white_marble", "velvet"])
        assert agreeing[1] > disagreeing[1]

    def test_an_unresolved_style_has_no_confidence(self):
        assert appearance.resolve_style("nonsense") == ("unknown", 0.0)

    def test_dominant_style_weighs_area_and_confidence(self):
        big = Room(id="a", style="modern", area=40.0, style_confidence=0.9)
        small = Room(id="b", style="bohemian", area=6.0, style_confidence=0.9)
        style, share = appearance.dominant_style([big, small])
        assert style == "modern"
        assert 0.0 < share <= 1.0

    def test_a_confident_small_room_can_still_lose_to_a_large_one(self):
        big = Room(id="a", style="modern", area=60.0, style_confidence=0.5)
        small = Room(id="b", style="luxury", area=8.0, style_confidence=1.0)
        assert appearance.dominant_style([big, small])[0] == "modern"

    def test_no_styles_yields_unknown(self):
        assert appearance.dominant_style([Room(id="a")]) == ("unknown", 0.0)


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


class TestColour:
    def test_hex_round_trips(self):
        assert appearance.to_hex(appearance.to_rgb("#4A5259")) == "#4A5259"

    def test_a_malformed_colour_degrades_to_neutral(self):
        assert appearance.to_rgb("not a colour") == (191, 191, 191)

    def test_distance_is_zero_for_identical_colours(self):
        assert appearance.distance("#EDE7DD", "#EDE7DD") == 0.0

    def test_distance_grows_with_difference(self):
        near = appearance.distance("#EDE7DD", "#EAE4DA")
        far = appearance.distance("#EDE7DD", "#1A1A1A")
        assert near < 0.1 < far

    def test_mixing_averages_rather_than_rotating_hue(self):
        """Red and cyan must not average to green."""
        mixed = appearance.mix(["#FF0000", "#00FFFF"])
        r, g, b = appearance.to_rgb(mixed)
        assert g < 200 and r > 100

    def test_mixing_respects_weights(self):
        mostly_dark = appearance.mix(["#000000", "#FFFFFF"], [9.0, 1.0])
        assert appearance.luminance(mostly_dark) < 0.5

    def test_warm_and_cool_kelvin_differ_in_the_expected_direction(self):
        warm = appearance.to_rgb(appearance.kelvin_to_hex(2200))
        cool = appearance.to_rgb(appearance.kelvin_to_hex(7000))
        assert warm[0] - warm[2] > cool[0] - cool[2]


class TestPalette:
    def test_walls_set_the_primary_and_floor_the_secondary(self, room, objects, lights):
        palette = appearance.derive_palette(room, objects, lights)
        assert palette.primary == "#EDE7DD"
        assert palette.secondary == "#D8B98C"

    def test_the_accent_is_the_most_saturated_non_surface_colour(self, room, objects, lights):
        palette = appearance.derive_palette(room, objects, lights)
        assert palette.accent == "#C1683F"  # the terracotta vase

    def test_furniture_colour_is_weighted_by_footprint(self, room, lights):
        """A sectional characterises a room more than a side table does."""
        big = SceneObject(id="a", category="sectional", group="furniture",
                          color_hex="#102030", dimensions=Dimensions(3.0, 2.0, 0.8))
        small = SceneObject(id="b", category="side_table", group="furniture",
                            color_hex="#F0F0F0", dimensions=Dimensions(0.4, 0.4, 0.5))
        palette = appearance.derive_palette(room, [big, small], lights)
        assert appearance.luminance(palette.furniture) < 0.5

    def test_a_room_with_nothing_observed_gets_no_palette(self):
        """Inventing one would present a guess with the authority of evidence."""
        assert appearance.derive_palette(Room(id="empty"), [], []) is None

    def test_a_style_palette_is_labelled_as_a_guess(self):
        palette = appearance.palette_from_style("industrial")
        assert palette.source == "style_prior"
        assert palette.confidence < 0.5

    def test_an_observed_palette_outranks_a_style_one(self, room, objects, lights):
        observed = appearance.derive_palette(room, objects, lights)
        guessed = appearance.palette_from_style("industrial")
        assert observed.source == "observed"
        assert observed.confidence > guessed.confidence

    def test_lighting_colour_follows_the_fixtures(self, room, objects):
        warm = appearance.derive_palette(
            room, objects, [LightSource(id="l", power_w=40, color_temperature_k=2200)]
        )
        cool = appearance.derive_palette(
            room, objects, [LightSource(id="l", power_w=40, color_temperature_k=6500)]
        )
        wr, _, wb = appearance.to_rgb(warm.lighting)
        cr, _, cb = appearance.to_rgb(cool.lighting)
        assert wr - wb > cr - cb


# ---------------------------------------------------------------------------
# Lighting environment
# ---------------------------------------------------------------------------


class TestLightingEnvironment:
    def test_large_glazing_raises_the_window_contribution(self, room, lights):
        small = appearance.derive_lighting(
            room, lights, [Opening(id="w", kind="window", position=Vec3(0, 2.5, 1.2),
                                   width=0.6, height=0.8)]
        )
        large = appearance.derive_lighting(
            room, lights, [Opening(id="w", kind="window", position=Vec3(0, 2.5, 1.2),
                                   width=3.0, height=2.2)]
        )
        assert large.window_contribution > small.window_contribution

    def test_daylight_points_through_the_largest_window(self, room, lights):
        """The sun is placed on the far side of the glazing, not picked."""
        east = appearance.derive_lighting(
            room, lights,
            [Opening(id="w", kind="window", position=Vec3(6.0, 2.5, 1.2), width=2.0, height=1.5)],
        )
        west = appearance.derive_lighting(
            room, lights,
            [Opening(id="w", kind="window", position=Vec3(0.0, 2.5, 1.2), width=2.0, height=1.5)],
        )
        assert abs(east.daylight_direction - west.daylight_direction) > 90

    def test_no_windows_means_no_daylight_direction(self, room, lights):
        """-1 signals unknown; the renderer must not invent a sun."""
        assert appearance.derive_lighting(room, lights, []).daylight_direction == -1.0

    def test_night_removes_the_window_contribution(self, room, lights):
        windows = [Opening(id="w", kind="window", position=Vec3(0, 2.5, 1.2),
                           width=3.0, height=2.2)]
        night = appearance.derive_lighting(room, lights, windows, {"time_of_day": "night"})
        assert night.window_contribution == 0.0
        assert night.time_of_day == "night"

    def test_an_observed_time_of_day_beats_the_inference(self, room, lights):
        """Only the model can see that it is dark outside."""
        windows = [Opening(id="w", kind="window", position=Vec3(0, 2.5, 1.2),
                           width=3.0, height=2.2)]
        inferred = appearance.derive_lighting(room, lights, windows)
        observed = appearance.derive_lighting(room, lights, windows, {"time_of_day": "night"})
        assert inferred.time_of_day == "day"
        assert observed.time_of_day == "night"
        assert observed.source == "observed"
        assert inferred.source == "inferred"

    def test_a_nonsense_time_of_day_falls_back_to_inference(self, room, lights):
        result = appearance.derive_lighting(room, lights, [], {"time_of_day": "brunch"})
        assert result.time_of_day in ("day", "evening", "night", "overcast")

    def test_daylight_cools_the_scene_temperature(self, room):
        """A sunlit room is cooler than the same room lit only by tungsten."""
        lamps = [LightSource(id="l", power_w=40, color_temperature_k=2700)]
        windows = [Opening(id="w", kind="window", position=Vec3(0, 2.5, 1.2),
                           width=3.0, height=2.4)]
        dark = appearance.derive_lighting(room, lamps, [], {"time_of_day": "night"})
        sunlit = appearance.derive_lighting(room, lamps, windows, {"time_of_day": "day"})
        assert sunlit.color_temperature_k > dark.color_temperature_k

    def test_big_windows_soften_shadows(self, room, lights):
        bare = appearance.derive_lighting(room, lights, [], {"time_of_day": "day"})
        glazed = appearance.derive_lighting(
            room, lights,
            [Opening(id="w", kind="window", position=Vec3(0, 2.5, 1.2), width=3.0, height=2.4)],
            {"time_of_day": "day"},
        )
        assert glazed.shadow_softness > bare.shadow_softness

    def test_values_stay_inside_their_ranges(self, room, lights):
        windows = [Opening(id="w", kind="window", position=Vec3(0, 2.5, 1.2),
                           width=50.0, height=50.0)]
        env = appearance.derive_lighting(room, lights, windows)
        assert 0.0 <= env.ambient <= 1.0
        assert 0.0 <= env.window_contribution <= 1.0
        assert 0.0 <= env.shadow_softness <= 1.0
