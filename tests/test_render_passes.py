"""
Tests for the render pass codec.

The codec is the contract between the two halves of the pipeline: Blender
writes these bytes, the evaluation engine reads them. A silent disagreement
here would not crash anything — it would produce masks of the wrong material
and depths in the wrong units, and every finding downstream would be
confidently wrong. So the round trips are pinned exactly.
"""

from __future__ import annotations

import pytest

from render import passes


# ---------------------------------------------------------------------------
# Index encoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", [1, 2, 7, 100, 255, 256, 257, 1000, 65535])
def test_an_index_survives_the_round_trip(index):
    """Exact, not approximate: these are integers, not measurements."""
    red, green, _blue = passes.encode_index(index)
    assert passes.decode_index(round(red * 255), round(green * 255)) == index


def test_index_zero_is_reserved_for_background():
    assert passes.encode_index(0) == (0.0, 0.0, 0.0)
    assert passes.decode_index(0, 0) == 0


def test_an_index_beyond_the_encoding_is_clamped_not_wrapped():
    """Wrapping would silently alias one material onto another."""
    encoded = passes.encode_index(passes.MAX_INDEX + 5000)
    assert passes.decode_index(round(encoded[0] * 255), round(encoded[1] * 255)) \
        == passes.MAX_INDEX


def test_index_decoding_works_on_whole_planes():
    numpy = pytest.importorskip("numpy")

    red = numpy.array([[1, 2], [3, 4]])
    green = numpy.array([[0, 0], [1, 1]])
    decoded = passes.decode_index(red, green)
    assert decoded.tolist() == [[1, 2], [259, 260]]


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metres", [0.0, 1.0, 3.5, 10.0, 20.0])
def test_depth_round_trips_within_one_quantisation_step(metres):
    """8 bits over 20 m is 8 cm; the codec must not add error beyond that."""
    encoded = passes.encode_depth(metres, 20.0)
    decoded = passes.decode_depth(round(encoded * 255), 20.0)
    assert abs(decoded - metres) <= 20.0 / 255.0


def test_depth_beyond_the_range_clamps_rather_than_wraps():
    """A far surface must read as far, never as near."""
    assert passes.encode_depth(500.0, 20.0) == 1.0


def test_depth_decoding_works_on_whole_planes():
    numpy = pytest.importorskip("numpy")

    plane = numpy.array([[0, 255]])
    assert passes.decode_depth(plane, 20.0).tolist() == [[0.0, 20.0]]


def test_normal_decoding_recovers_a_unit_vector():
    """128 is zero, so a face-on surface decodes to a recognisable normal."""
    decoded = passes.decode_normal(128, 0, 128)
    assert abs(decoded[0]) < 0.01
    assert decoded[1] == pytest.approx(-1.0, abs=0.01)
    assert abs(decoded[2]) < 0.01


# ---------------------------------------------------------------------------
# Naming and selection
# ---------------------------------------------------------------------------


def test_pass_files_sit_beside_the_beauty_render():
    assert passes.pass_filename("/p/room_a/viewpoint_01.png", "albedo") \
        == "/p/room_a/viewpoint_01_albedo.png"


def test_pass_filename_tolerates_a_missing_extension():
    assert passes.pass_filename("/p/viewpoint_01", "depth") == "/p/viewpoint_01_depth.png"


def test_the_pass_list_keeps_canonical_order():
    """Order is render order; a config file must not be able to change it."""
    assert passes.normalise(["object_id", "albedo"]) == ("albedo", "object_id")


def test_the_pass_list_accepts_a_comma_string():
    assert passes.normalise("albedo, depth") == ("albedo", "depth")


def test_unknown_pass_names_are_dropped_not_fatal():
    """config.json is hand-edited; a typo should cost a pass, not the run."""
    assert passes.normalise(["albedo", "sparkle"]) == ("albedo",)


def test_all_and_none_are_understood():
    assert passes.normalise("all") == passes.ALL_PASSES
    assert passes.normalise("none") == ()
    assert passes.normalise(None) == passes.DEFAULT_PASSES


# ---------------------------------------------------------------------------
# The index map
# ---------------------------------------------------------------------------


def test_the_index_map_resolves_names():
    index_map = passes.IndexMap({1: "sofa_1"}, {2: "FloorMaterial"})
    assert index_map.object_for(1) == "sofa_1"
    assert index_map.material_for(2) == "FloorMaterial"


def test_an_unmapped_index_resolves_to_nothing_rather_than_guessing():
    index_map = passes.IndexMap({1: "sofa_1"})
    assert index_map.object_for(99) == ""


def test_the_index_map_round_trips_through_json_keys():
    """It travels in the manifest, where every key becomes a string."""
    original = passes.IndexMap({1: "sofa_1"}, {3: "WallMaterial"})
    restored = passes.IndexMap.from_dict(original.to_dict())
    assert restored.object_for(1) == "sofa_1"
    assert restored.material_for(3) == "WallMaterial"


def test_an_empty_index_map_is_falsey():
    assert not passes.IndexMap()
    assert passes.IndexMap({1: "x"})


def test_data_passes_are_rendered_raw_and_albedo_is_not():
    """Raw is what makes decoding exact; Standard is what makes albedo
    comparable with a photograph."""
    assert passes.VIEW_TRANSFORM[passes.ALBEDO] == "Standard"
    for name in (passes.DEPTH, passes.NORMAL, passes.MATERIAL_ID, passes.OBJECT_ID):
        assert passes.VIEW_TRANSFORM[name] == "Raw"
