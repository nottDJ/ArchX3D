"""Layer, block and text name classification.

These three tables are the project's interface to real-world CAD conventions,
and almost every classification failure traces back to one of them. The tests
therefore pin the *ordering* traps specifically — the cases where a loose
substring rule would shadow a more specific one — rather than only checking
that the happy path works.
"""

from __future__ import annotations

import pytest

from cad import blocks, layers, text


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------


class TestLayerClassification:
    @pytest.mark.parametrize(
        "name,role",
        [
            ("A-WALL", "wall"),
            ("A-WALL-FULL", "wall"),
            ("A-DOOR", "door"),
            ("A-GLAZ", "window"),
            ("A-FLOR-PFIX", "plumbing_fixture"),
            ("A-FLOR-CASE", "casework"),
            ("A-FLOR-STRS", "stair"),
            ("A-ANNO-TEXT", "text"),
            ("A-ANNO-DIMS", "dimension"),
            ("A-AREA-IDEN", "text"),
            ("I-FURN", "furniture"),
            ("S-COLS", "column"),
            ("A-FLOR-PATT", "hatch"),
        ],
    )
    def test_aia_names(self, name, role):
        result = layers.classify_layer(name)
        assert result.role == role
        assert result.convention == "aia"
        assert result.confidence >= 0.8

    def test_aia_minor_group_refines_major(self):
        """``A-FLOR-PFIX`` is fixtures, not the generic 'floor' its major says.

        Without the minor-group override every fixture layer in a standards-
        compliant drawing collapses to "floor" and the bathroom evidence is
        lost — which is exactly what happened before this override existed.
        """
        assert layers.classify_layer("A-FLOR").role == "floor"
        assert layers.classify_layer("A-FLOR-PFIX").role == "plumbing_fixture"

    @pytest.mark.parametrize(
        "name,role",
        [
            ("WALLS", "wall"),
            ("Walls", "wall"),
            ("05-DOORS", "door"),
            ("windows_new", "window"),
            ("furniture-existing", "furniture"),
            ("Room Names", "text"),
            ("DIMENSIONS", "dimension"),
            ("SANITARY", "plumbing_fixture"),
            ("KITCHEN CABINETS", "casework"),
            ("column grid", "grid"),
        ],
    )
    def test_keyword_fallback(self, name, role):
        assert layers.classify_layer(name).role == role

    def test_window_is_tested_before_wall(self):
        """A layer naming both must resolve to the more specific opening."""
        assert layers.classify_layer("WALL-WINDOWS").role == "window"
        assert layers.classify_layer("WALL-OPENINGS").role == "door"

    def test_default_layer_zero_carries_no_intent(self):
        """Layer '0' is AutoCAD's default; guessing from it invents evidence."""
        result = layers.classify_layer("0")
        assert result.role == "unknown"
        assert result.confidence == 0.0

    def test_unknown_name_is_reported_not_guessed(self):
        result = layers.classify_layer("ZQ_47_XX")
        assert result.role == "unknown"
        assert result.confidence == 0.0

    def test_annotation_layers_are_kept_not_discarded(self):
        """The old extractor blacklisted these; they are the label evidence."""
        for name in ("A-ANNO-TEXT", "ROOM NAMES", "A-DOOR", "A-FURN"):
            assert layers.classify_layer(name).role != "unknown"

    def test_wall_layer_names_selects_only_walls(self):
        names = ["A-WALL", "A-DOOR", "WALLS", "A-ANNO-TEXT", "0"]
        assert layers.wall_layer_names(names) == ["A-WALL", "WALLS"]


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


class TestBlockClassification:
    @pytest.mark.parametrize(
        "name,category,kind",
        [
            ("WC", "toilet", "plumbing_fixture"),
            ("WC-01", "toilet", "plumbing_fixture"),
            ("TOILET_2", "toilet", "plumbing_fixture"),
            ("WASH-BASIN", "washbasin", "plumbing_fixture"),
            ("SHOWER-TRAY", "shower", "plumbing_fixture"),
            ("BATHTUB", "bathtub", "plumbing_fixture"),
            ("KITCHEN-SINK", "sink", "kitchen_fixture"),
            ("HOB-4BURNER", "cooktop", "kitchen_fixture"),
            ("REFRIGERATOR", "refrigerator", "appliance"),
            ("BED-QUEEN", "bed", "furniture"),
            ("BED-SINGLE", "bed", "furniture"),
            ("WARDROBE", "wardrobe", "casework"),
            ("SOFA-3SEAT", "sofa", "furniture"),
            ("DINING-TABLE", "dining_table", "furniture"),
            ("DOOR-SINGLE", "door", "door"),
            ("WINDOW-CASEMENT", "window", "window"),
            ("NORTH-ARROW", "north_arrow", "north_arrow"),
        ],
    )
    def test_common_library_names(self, name, category, kind):
        result = blocks.classify_block(name)
        assert result.category == category
        assert result.kind == kind

    def test_library_prefixes_are_stripped(self):
        """CAD libraries bolt prefixes onto everything; they must not blind us."""
        for name in ("_ARCH_BED_QUEEN", "AEC_BED-QUEEN", "ACAD_BED", "LIB_BED_1"):
            assert blocks.classify_block(name).category == "bed"

    def test_anonymous_blocks_yield_nothing(self):
        """``A$C...`` and ``*U12`` have no name to read, so nothing is claimed.

        Worth pinning because the obvious prefix-stripping rule for these is
        unsafe: hex digits cover ``BED``, ``ACE`` and ``FADE``, so a greedy
        ``[0-9A-F]+`` consumes the word it was meant to reveal.
        """
        for name in ("A$C1A2B3C4", "*U12", "*X3"):
            assert blocks.normalise_block_name(name) == ""
            assert blocks.classify_block(name).category == ""

    def test_architecture_beats_furniture(self):
        """``DOOR-*`` must never be read as a piece of casework."""
        assert blocks.classify_block("DOOR-CABINET").kind == "door"

    def test_fixtures_score_higher_than_furniture(self):
        """Fixture patterns are tighter, so their confidence is stated higher."""
        fixture = blocks.classify_block("WC")
        furniture = blocks.classify_block("SOFA")
        assert fixture.confidence > furniture.confidence

    def test_sink_and_washbasin_stay_distinct(self):
        """They point at different rooms; conflating them destroys evidence."""
        assert blocks.classify_block("KITCHEN-SINK").category == "sink"
        assert blocks.classify_block("WASH-BASIN").category == "washbasin"

    def test_unknown_block_is_not_guessed(self):
        result = blocks.classify_block("XYZZY_42")
        assert result.category == ""
        assert result.confidence == 0.0

    def test_type_attribute_overrides_a_generic_name(self):
        result = blocks.classify_block("BLOCK1", {"TYPE": "WC"})
        assert result.category == "toilet"

    def test_room_name_attribute_is_extracted(self):
        assert blocks.room_name_from_attributes(
            {"ROOM_NAME": "MASTER BEDROOM"}
        ) == "MASTER BEDROOM"
        assert blocks.room_name_from_attributes({"OTHER": "x"}) == ""

    def test_room_name_attribute_ignores_bare_numbers(self):
        """A room *number* is not a room name."""
        assert blocks.room_name_from_attributes({"NAME": "204"}) == ""

    def test_area_attribute_parsing(self):
        assert blocks.area_from_attributes({"AREA": "14.25"}) == pytest.approx(14.25)
        assert blocks.area_from_attributes({"SQ_M": "9.4 m2"}) == pytest.approx(9.4)
        assert blocks.area_from_attributes({}) is None

    def test_normalisation_keeps_word_boundaries(self):
        """Separators become spaces, not nothing, so ``\\b`` anchors survive."""
        assert blocks.normalise_block_name("TV_UNIT") == "TV UNIT"
        assert blocks.normalise_block_name("bed--queen") == "BED QUEEN"


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


class TestTextClassification:
    @pytest.mark.parametrize(
        "raw,room_type",
        [
            ("BEDROOM", "bedroom"),
            ("BED ROOM", "bedroom"),
            ("BEDROOM 2", "bedroom"),
            ("MASTER BEDROOM", "master_bedroom"),
            ("M.BED", "master_bedroom"),
            ("KITCHEN", "kitchen"),
            ("KIT.", "kitchen"),
            ("LIVING ROOM", "living_room"),
            ("DRAWING ROOM", "living_room"),
            ("W.C.", "bathroom"),
            ("BATH", "bathroom"),
            ("TOILET", "bathroom"),
            ("DINING", "dining_room"),
            ("STUDY", "office"),
            ("BALCONY", "balcony"),
            ("UTILITY", "utility"),
            ("STORE", "store"),
            ("PASSAGE", "hallway"),
        ],
    )
    def test_room_vocabulary(self, raw, room_type):
        assert text.room_type_from_label(raw)[0] == room_type

    def test_specific_beats_general(self):
        """``MASTER BEDROOM`` must not be eaten by the ``BEDROOM`` rule."""
        assert text.room_type_from_label("MASTER BEDROOM")[0] == "master_bedroom"
        assert text.canonical_room_type("master_bedroom") == "bedroom"

    def test_label_with_embedded_dimensions(self):
        result = text.classify_text("KITCHEN\\P3.60 X 4.20")
        assert result.role == "room_label"
        assert result.room_type == "kitchen"
        assert result.dimensions == pytest.approx((3.6, 4.2))

    def test_label_with_area_annotation(self):
        result = text.classify_text("BEDROOM 2\\P16.00 SQ.M.")
        assert result.role == "room_label"
        assert result.room_type == "bedroom"
        assert result.area_m2 == pytest.approx(16.0)

    def test_mtext_formatting_codes_are_stripped(self):
        """An unstripped format code fails every pattern, silently."""
        assert text.clean_text(r"{\fArial|b1;KITCHEN}") == "KITCHEN"
        assert text.room_type_from_label(r"{\fArial|b1;KITCHEN}")[0] == "kitchen"

    def test_imperial_dimensions(self):
        result = text.parse_dimension_pair("12'-6\" X 14'-0\"")
        assert result[0] == pytest.approx(3.8100, abs=1e-3)
        assert result[1] == pytest.approx(4.2672, abs=1e-3)

    def test_millimetre_dimensions_are_converted(self):
        assert text.parse_dimension_pair("3600 x 4200") == pytest.approx((3.6, 4.2))

    def test_square_feet_converted_to_metric(self):
        area, unit = text.parse_area("102 SQ.FT")
        assert unit == "sq_ft"
        assert area == pytest.approx(9.476, abs=1e-2)

    @pytest.mark.parametrize(
        "raw",
        [
            "GROUND FLOOR PLAN",
            "SCALE 1:100",
            "DRAWN BY: ARCHX",
            "ALL DIMENSIONS IN MM",
            "REVISION NO 3",
        ],
    )
    def test_drawing_metadata_is_not_a_room_label(self, raw):
        """Title-block text must not name a room; ``FLOOR PLAN`` is the trap."""
        assert text.classify_text(raw).role == "note"

    def test_unmatched_string_returns_no_room_type(self):
        """No fuzzy matching: an unknown string yields nothing, not a guess."""
        result = text.classify_text("ZONE 4C")
        assert result.role == "note"
        assert result.room_type == ""

    def test_normalisation_handles_punctuated_abbreviations(self):
        assert text.normalise("M.BED.RM") == "M BED RM"
        assert text.normalise("W.C.") == "W C"
