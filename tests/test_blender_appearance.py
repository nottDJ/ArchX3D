"""
Tests for the Blender appearance decision layer.

These cover ``blender.colour``, ``blender.palette`` and ``blender.styles`` —
the three modules that decide *what* a surface should look like. They import
no ``bpy``, which is the whole reason the package is split that way: the
interesting judgements are testable without a running Blender.

The node-graph construction in ``blender.materials`` and the light and camera
building in ``blender.lighting`` / ``blender.camera`` need real ``bpy`` and are
verified by running Blender headless (see docs/APPEARANCE.md); what is pinned
here is the logic that decides what those modules are asked to build.

The property under test throughout is **bounded influence**: a palette must
change a scene without making it physically absurd, and a style must fill gaps
without overriding evidence.
"""

from __future__ import annotations

import pytest

from blender import colour, palette, styles
from vision import catalog
from vision.schema import ColourPalette


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


class TestColourParsing:
    def test_hex_round_trips(self):
        assert colour.to_hex(colour.to_unit("#4A5259")) == "#4A5259"

    @pytest.mark.parametrize("bad", ["", "not a colour", "#12345", "#GGGGGG", None])
    def test_malformed_colours_degrade_to_neutral(self, bad):
        """A bad hex in one record must not abort a whole scene build."""
        assert colour.to_rgb255(bad) == (191, 191, 191)

    def test_short_form_expands(self):
        assert colour.to_rgb255("#F0A") == (255, 0, 170)


class TestColourSpace:
    def test_srgb_and_linear_are_inverses(self):
        for value in (0.0, 0.02, 0.25, 0.5, 0.9, 1.0):
            assert colour.linear_to_srgb(colour.srgb_to_linear(value)) == pytest.approx(
                value, abs=1e-9
            )

    def test_linear_is_darker_than_srgb_in_the_midtones(self):
        """The conversion that, omitted, makes every render look bleached."""
        assert colour.srgb_to_linear(0.5) < 0.5

    def test_hex_to_linear_returns_rgba(self):
        value = colour.hex_to_linear("#808080")
        assert len(value) == 4
        assert value[3] == 1.0
        assert value[0] < 0.5  # linearised

    def test_pure_black_and_white_survive(self):
        assert colour.hex_to_linear("#000000")[:3] == (0.0, 0.0, 0.0)
        assert colour.hex_to_linear("#FFFFFF")[:3] == pytest.approx((1.0, 1.0, 1.0))


class TestColourOperations:
    def test_shift_lightens_and_darkens(self):
        assert colour.luminance(colour.shift("#808080", 40)) > colour.luminance("#808080")
        assert colour.luminance(colour.shift("#808080", -40)) < colour.luminance("#808080")

    def test_shift_saturates_rather_than_wrapping(self):
        """Overflow must clamp; wrapping would turn a highlight black."""
        assert colour.to_rgb255(colour.shift("#FFFFFF", 60)) == (255, 255, 255)
        assert colour.to_rgb255(colour.shift("#000000", -60)) == (0, 0, 0)

    def test_mix_endpoints_are_exact(self):
        assert colour.mix("#204060", "#A0C0E0", 0.0) == "#204060"
        assert colour.mix("#204060", "#A0C0E0", 1.0) == "#A0C0E0"

    def test_mixing_two_greys_does_not_darken(self):
        """The classic sRGB-space blending artefact."""
        blended = colour.luminance(colour.mix("#000000", "#FFFFFF", 0.5))
        assert blended > 0.4, "a linear blend of black and white sits near mid-grey"

    def test_distance_is_zero_for_identity(self):
        assert colour.distance("#123456", "#123456") == 0.0

    def test_distance_grows_with_difference(self):
        near = colour.distance("#EDE7DD", "#EAE4DA")
        far = colour.distance("#EDE7DD", "#1A1A1A")
        assert near < 0.1 < far

    def test_hue_distance_wraps(self):
        assert colour.hue_distance(0.99, 0.01) == pytest.approx(0.02, abs=1e-9)


class TestKelvin:
    def test_warm_is_redder_than_cool(self):
        warm = colour.kelvin_to_rgb(2200)
        cool = colour.kelvin_to_rgb(7000)
        assert warm[0] - warm[2] > cool[0] - cool[2]

    def test_colour_is_normalised_so_warmth_does_not_dim(self):
        """A light's brightness is its energy; the colour must not carry it."""
        for kelvin in (2000, 3000, 4500, 6500, 9000):
            assert max(colour.kelvin_to_rgb(kelvin)) == pytest.approx(1.0, abs=1e-6)

    def test_extremes_are_clamped_not_extrapolated(self):
        assert colour.kelvin_to_rgb(-500) == colour.kelvin_to_rgb(1000)
        assert colour.kelvin_to_rgb(99999) == colour.kelvin_to_rgb(12000)


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------


@pytest.fixture
def cold_palette():
    """A deliberately hostile palette: cold blue against warm materials."""
    return ColourPalette(
        primary="#2E4A7A", secondary="#2E4A7A", accent="#1F6FE0",
        lighting="#FFF0DC", furniture="#2E4A7A", decor="#1F6FE0",
        source="observed", confidence=0.9,
    )


class TestTintBudgets:
    def test_species_inherit_their_family_budget(self):
        """Walnut is timber, so it gets timber's physics without its own entry."""
        assert palette.budget_for("walnut") is palette.budget_for("wood")
        assert palette.budget_for("white_marble") is palette.budget_for("marble")
        assert palette.budget_for("velvet") is palette.budget_for("fabric")

    def test_an_unknown_material_gets_a_conservative_budget(self):
        budget = palette.budget_for("unobtainium")
        assert budget.hue < 0.2
        assert not budget.replaceable

    def test_only_chosen_colours_are_replaceable(self):
        assert palette.budget_for("paint_matte").replaceable
        assert not palette.budget_for("walnut").replaceable
        assert not palette.budget_for("white_marble").replaceable
        assert not palette.budget_for("brass").replaceable


class TestRealismBounds:
    def test_walnut_never_becomes_blue(self, cold_palette):
        """The headline constraint."""
        result = palette.for_object("#5C4033", "walnut", "furniture", cold_palette)
        original_hue = colour.hls("#5C4033")[0]
        tinted_hue = colour.hls(result.color_hex)[0]

        assert colour.hue_distance(original_hue, tinted_hue) <= 0.03
        assert result.clamped, "the budget must have bitten"
        # Still recognisably brown, not blue.
        r, g, b = colour.to_rgb255(result.color_hex)
        assert r > b, f"{result.color_hex} lost its warmth"

    def test_marble_barely_moves(self, cold_palette):
        result = palette.for_surface("#E8E4DC", "white_marble", cold_palette, "floor")
        assert result.moved < 0.08

    def test_metal_is_almost_immovable(self, cold_palette):
        """A metal's colour is its alloy."""
        result = palette.for_object("#8C8F94", "brass", "furniture", cold_palette)
        assert result.moved < 0.05

    def test_paint_is_replaced_outright(self, cold_palette):
        """A wall colour is a choice, so the palette simply sets it."""
        result = palette.for_surface("#EFEDE8", "paint_matte", cold_palette, "wall")
        assert result.color_hex == cold_palette.primary
        assert not result.clamped

    def test_fabric_moves_more_than_timber(self, cold_palette):
        fabric = palette.for_object("#9C978E", "linen", "furniture", cold_palette)
        timber = palette.for_object("#9C8B7E", "walnut", "furniture", cold_palette)
        assert fabric.moved > timber.moved

    # Four palettes chosen to fight the materials as hard as possible.
    HOSTILE = [
        ColourPalette(primary="#2E4A7A", secondary="#2E4A7A", accent="#1F6FE0",
                      furniture="#2E4A7A", decor="#1F6FE0"),
        ColourPalette(primary="#D01818", secondary="#D01818", accent="#FF0000",
                      furniture="#D01818", decor="#FF0000"),
        ColourPalette(primary="#00FF66", secondary="#00FF66", accent="#00FF00",
                      furniture="#00FF66", decor="#00FF00"),
        ColourPalette(primary="#000000", secondary="#FFFFFF", accent="#FF00FF",
                      furniture="#000000", decor="#FF00FF"),
    ]

    @pytest.mark.parametrize("material", sorted(catalog.MATERIALS))
    def test_no_material_is_ever_pushed_outside_its_budget(self, material):
        """The core guarantee, over every material and a hostile palette set.

        Stated in two halves because hue is only meaningful on a saturated
        colour. For anything with real chroma the hue must stay inside budget —
        that is what stops walnut going blue. For a near-neutral material the
        hue is degenerate, so what is protected instead is that it *stays*
        neutral: black marble must not become blue marble.
        """
        base = catalog.get_material(material).color_hex
        budget = palette.budget_for(material)
        if budget.replaceable:
            pytest.skip("paint is a chosen colour and is set outright")

        base_saturation = colour.hls(base)[2]

        for scheme in self.HOSTILE:
            results = [palette.for_object(base, material, group, scheme)
                       for group in ("furniture", "decor")]
            results += [palette.for_surface(base, material, scheme, surface)
                        for surface in ("wall", "floor", "ceiling")]

            for result in results:
                hue_moved = colour.hue_distance(
                    colour.hls(base)[0], colour.hls(result.color_hex)[0]
                )
                saturation_moved = abs(colour.hls(result.color_hex)[2] - base_saturation)

                assert saturation_moved <= budget.saturation + 1e-6, (
                    f"{material}: saturation moved {saturation_moved:.3f} "
                    f"(budget {budget.saturation})"
                )
                if base_saturation >= palette.NEUTRAL_SATURATION:
                    assert hue_moved <= budget.hue + 1e-6, (
                        f"{material}: {base} -> {result.color_hex} moved hue "
                        f"{hue_moved:.4f} (budget {budget.hue})"
                    )

    def test_a_neutral_material_cannot_be_given_a_colour(self):
        """Black marble must not become blue marble."""
        scheme = ColourPalette(primary="#1F6FE0", secondary="#1F6FE0",
                               accent="#1F6FE0", furniture="#1F6FE0")
        result = palette.for_object("#22201F", "black_marble", "furniture", scheme)
        assert colour.hls(result.color_hex)[2] < 0.15, (
            f"{result.color_hex} picked up chroma a stone should not have"
        )


class TestSurfaceRules:
    def test_a_ceiling_never_ends_up_darker_than_it_started(self, cold_palette):
        """A ceiling painted the full wall colour reads as a cave."""
        result = palette.for_surface("#F6F5F2", "gypsum", cold_palette, "ceiling")
        assert colour.luminance(result.color_hex) >= colour.luminance("#F6F5F2")

    def test_a_wall_follows_the_palette_more_than_a_floor(self):
        pale = ColourPalette(primary="#3A5F8A", secondary="#3A5F8A")
        wall = palette.for_surface("#CFCFCF", "wallpaper", pale, "wall")
        floor = palette.for_surface("#CFCFCF", "wallpaper", pale, "floor")
        assert wall.moved > floor.moved

    def test_decor_follows_the_accent_more_than_furniture_does(self, cold_palette):
        decor = palette.for_object("#BFBFBF", "ceramic", "decor", cold_palette)
        furniture = palette.for_object("#BFBFBF", "ceramic", "furniture", cold_palette)
        assert decor.moved > furniture.moved

    def test_no_palette_means_no_change(self):
        """An observed colour outranks a palette that does not exist."""
        result = palette.for_surface("#123456", "paint_matte", None, "wall")
        assert result.color_hex == "#123456"
        assert result.moved == 0.0

    def test_a_missing_target_leaves_the_colour_alone(self):
        assert palette.apply("#123456", "paint_matte", None).color_hex == "#123456"


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


class TestStyleProfiles:
    def test_every_catalog_style_has_a_profile(self):
        """A recognised style with no profile would silently do nothing."""
        for name in catalog.STYLES:
            assert name in styles.STYLE_PROFILES, f"{name} has no appearance profile"

    def test_every_substitution_target_exists(self):
        """A typo here would resolve to 'unknown' and grey out a whole scene."""
        for profile in styles.STYLE_PROFILES.values():
            for family, species in profile.substitutions.items():
                assert species in catalog.MATERIALS, f"{profile.name}: {species} unknown"

    def test_every_surface_default_exists(self):
        for profile in styles.STYLE_PROFILES.values():
            for surface in (profile.wall, profile.floor, profile.ceiling):
                assert surface in catalog.MATERIALS, f"{profile.name}: {surface} unknown"

    def test_profiles_resolve_through_synonyms(self):
        assert styles.profile_for("mid-century modern").name == "mid_century"
        assert styles.profile_for("loft").name == "industrial"

    def test_an_unknown_style_is_inert(self):
        profile = styles.profile_for("nonsense")
        assert profile.name == "unknown"
        assert profile.substitutions == {}


class TestMaterialResolution:
    def test_an_observed_species_always_wins(self):
        """Naming walnut means the pipeline saw walnut. No style overrides that."""
        for style in ("scandinavian", "industrial", "luxury", "unknown"):
            decision = styles.resolve_material("walnut", style)
            assert decision.material == "walnut"
            assert decision.from_evidence

    def test_a_generic_family_is_refined_by_style(self):
        assert styles.resolve_material("wood", "scandinavian").material == "white_oak"
        assert styles.resolve_material("wood", "industrial").material == "walnut"
        assert styles.resolve_material("metal", "industrial").material == "blackened_steel"

    def test_refinement_is_marked_as_a_style_choice_not_evidence(self):
        decision = styles.resolve_material("wood", "scandinavian")
        assert decision.source == "style"
        assert not decision.from_evidence

    def test_a_weak_style_does_not_refine(self):
        """A stray adjective should not restyle a room."""
        decision = styles.resolve_material("wood", "scandinavian", confidence=0.2)
        assert decision.material == "wood"
        assert decision.source == "observed"

    def test_an_unobserved_surface_falls_back_to_the_style(self):
        decision = styles.resolve_material(None, "industrial", "wall")
        assert decision.material == "exposed_brick"
        assert decision.source == "style"

    def test_an_unobserved_surface_with_no_style_uses_a_neutral_default(self):
        decision = styles.resolve_material(None, "unknown", "wall")
        assert decision.material == "paint_matte"
        assert decision.source == "default"

    @pytest.mark.parametrize("empty", [None, "", "unknown"])
    def test_all_the_empty_forms_are_treated_alike(self, empty):
        assert styles.resolve_material(empty, "unknown", "floor").source == "default"

    def test_every_resolution_yields_a_real_material(self):
        """Whatever the inputs, the generator must get something buildable."""
        for style in list(catalog.STYLES) + ["nonsense"]:
            for observed in (None, "wood", "walnut", "unknown", "unobtainium"):
                for surface in ("wall", "floor", "ceiling", "object"):
                    material = styles.resolve_material(observed, style, surface).material
                    assert material in catalog.MATERIALS or material == "unobtainium"


class TestStyleLighting:
    def test_industrial_is_warmer_and_dimmer_than_scandinavian(self):
        industrial_cct, industrial_gain, _ = styles.lighting_bias("industrial")
        scandi_cct, scandi_gain, _ = styles.lighting_bias("scandinavian")
        assert industrial_cct < scandi_cct     # Edison filament vs daylight
        assert industrial_gain < scandi_gain

    def test_diffuse_styles_bias_toward_softer_shadows(self):
        assert styles.lighting_bias("japanese")[2] > styles.lighting_bias("art_deco")[2]

    def test_minimal_styles_tolerate_less_clutter(self):
        assert styles.decor_density("minimalist") < styles.decor_density("bohemian")

    def test_an_unknown_style_is_neutral(self):
        cct, gain, softness = styles.lighting_bias("unknown")
        assert gain == 1.0
        assert softness == 0.0
        assert styles.decor_density("unknown") == 1.0

    def test_describe_is_ascii_for_the_windows_console(self):
        """The build log is a cp1252 console; non-ASCII raises there."""
        for style in catalog.STYLES:
            styles.describe(style).encode("cp1252")

    def test_tint_str_is_ascii(self, cold_palette):
        str(palette.for_object("#5C4033", "walnut", "furniture", cold_palette)).encode("cp1252")
