"""
Tests for the authoring operations added to the review step.

Covers duplication and paste, asset swapping, per-object material and colour,
per-room surface finishes, and lighting edits.

The theme running through these is *separation of concerns*: changing what an
object looks like must never move it, and changing one room's walls must never
repaint another's.
"""

from __future__ import annotations

import pytest

from vision import review
from vision.schema import (
    Dimensions,
    LightSource,
    Room,
    SceneGraph,
    SceneObject,
    Vec3,
    Wall,
)


@pytest.fixture
def graph() -> SceneGraph:
    room = Room(
        id="r1",
        room_type="living_room",
        polygon=[(0.0, 0.0), (6.0, 0.0), (6.0, 5.0), (0.0, 5.0)],
        bounds_min=(0.0, 0.0),
        bounds_max=(6.0, 5.0),
        area=30.0,
        ceiling_height=3.0,
    )
    second = Room(
        id="r2",
        room_type="bedroom",
        polygon=[(7.0, 0.0), (12.0, 0.0), (12.0, 5.0), (7.0, 5.0)],
        bounds_min=(7.0, 0.0),
        bounds_max=(12.0, 5.0),
        area=25.0,
        ceiling_height=3.0,
    )
    sofa = SceneObject(
        id="sofa", category="sofa", room_id="r1",
        position=Vec3(1.5, 1.5, 0.0), dimensions=Dimensions(2.0, 0.9, 0.8),
        confidence=0.9, asset="sofa_low_modern", material="fabric",
    )
    light = LightSource(
        id="l1", kind="pendant_light", room_id="r1",
        position=Vec3(3.0, 3.0, 2.8), power_w=60.0, color_temperature_k=3000.0,
    )
    return SceneGraph(
        rooms=[room, second],
        walls=[Wall(id="w1", start=(0.0, 0.0), end=(6.0, 0.0))],
        objects=[sofa],
        lights=[light],
    )


# ---------------------------------------------------------------------------
# Asset, material and colour
# ---------------------------------------------------------------------------


def test_swapping_an_asset_does_not_move_the_object(graph):
    """The whole point of an asset swap: same place, different thing."""
    before = graph.object_by_id("sofa")
    origin = (before.position.x, before.position.y, before.rotation_z)

    updated, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"asset": "sofa_deep_lounge"}}}
    )

    sofa = updated.object_by_id("sofa")
    assert sofa.asset == "sofa_deep_lounge"
    assert (sofa.position.x, sofa.position.y, sofa.rotation_z) == origin
    assert report.restyled == ["sofa"]


def test_a_user_chosen_asset_clears_the_matcher_score(graph):
    """The score described the matcher's own pick and is not evidence here."""
    graph.object_by_id("sofa").asset_score = 0.87

    updated, _ = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"asset": "sofa_deep_lounge"}}}
    )

    assert updated.object_by_id("sofa").asset_score == 0.0


def test_an_asset_from_another_category_is_refused(graph):
    _, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"asset": "wardrobe_tall_plain"}}}
    )
    assert report.restyled == []
    assert any("is not a variant" in entry for entry in report.rejected)


def test_material_and_colour_can_be_set(graph):
    updated, report = review.apply_edits(
        graph,
        {"object_overrides": {"sofa": {"material": "leather", "color_hex": "#3a2a20"}}},
    )

    sofa = updated.object_by_id("sofa")
    assert sofa.material == "leather"
    assert sofa.color_hex == "#3A2A20"  # normalised to upper case
    assert report.restyled == ["sofa"]


@pytest.mark.parametrize("colour", ["not-a-colour", "#12345", "#GGGGGG", ""])
def test_a_malformed_colour_is_refused(graph, colour):
    _, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"color_hex": colour}}}
    )
    assert any("color_hex" in entry for entry in report.rejected)


def test_an_unrecognised_material_is_refused(graph):
    _, report = review.apply_edits(
        graph, {"object_overrides": {"sofa": {"material": "unobtainium"}}}
    )
    assert any("not recognised" in entry for entry in report.rejected)


# ---------------------------------------------------------------------------
# Duplicate and paste
# ---------------------------------------------------------------------------


def test_duplicating_copies_the_source(graph):
    updated, report = review.apply_edits(
        graph,
        {"add_objects": [{"source_id": "sofa", "position": {"x": 4.0, "y": 3.0}}]},
    )

    assert len(report.added) == 1
    copy = updated.object_by_id(report.added[0])
    assert copy.category == "sofa"
    assert copy.asset == "sofa_low_modern"
    assert (copy.position.x, copy.position.y) == (4.0, 3.0)
    assert copy.id != "sofa"


def test_a_copy_does_not_inherit_observational_evidence(graph):
    """A duplicate is the user's doing, not a second sighting of the original."""
    original = graph.object_by_id("sofa")
    original.observation_count = 3
    original.source_images = ["a.jpg", "b.jpg", "c.jpg"]

    updated, report = review.apply_edits(
        graph, {"add_objects": [{"source_id": "sofa", "position": {"x": 4.0, "y": 3.0}}]}
    )

    copy = updated.object_by_id(report.added[0])
    assert copy.observation_count == 0
    assert copy.source_images == []
    assert any(flag.startswith("copied_from_") for flag in copy.flags)


def test_a_copy_is_never_born_locked(graph):
    locked, _ = review.apply_edits(graph, {"object_overrides": {"sofa": {"locked": True}}})

    updated, report = review.apply_edits(
        locked, {"add_objects": [{"source_id": "sofa", "position": {"x": 4.0, "y": 3.0}}]}
    )

    assert updated.object_by_id(report.added[0]).locked is False


def test_creating_from_a_category_uses_catalog_dimensions(graph):
    updated, report = review.apply_edits(
        graph,
        {"add_objects": [{"category": "armchair", "room_id": "r1",
                          "position": {"x": 3.0, "y": 2.0}}]},
    )

    created = updated.object_by_id(report.added[0])
    assert created.category == "armchair"
    assert not created.dimensions.is_degenerate()
    assert created.confidence == 1.0
    assert "created_by_user" in created.flags


def test_an_unplaceable_copy_is_discarded_rather_than_hidden(graph):
    """A copy that cannot be placed must not linger invisibly on top of its source."""
    before = len(graph.objects)

    updated, report = review.apply_edits(
        graph,
        {"add_objects": [{"source_id": "sofa", "position": {"x": 99.0, "y": 99.0}}]},
    )

    assert report.added == []
    assert len(updated.objects) == before
    assert any("discarded" in entry for entry in report.rejected)


def test_adding_to_a_nonexistent_room_is_refused(graph):
    _, report = review.apply_edits(
        graph, {"add_objects": [{"category": "armchair", "room_id": "nowhere"}]}
    )
    assert report.added == []
    assert any("does not exist" in entry for entry in report.rejected)


def test_repeated_duplication_produces_distinct_ids(graph):
    updated, report = review.apply_edits(
        graph,
        {"add_objects": [
            {"source_id": "sofa", "position": {"x": 4.0, "y": 3.0}},
            {"source_id": "sofa", "position": {"x": 4.5, "y": 3.5}},
        ]},
    )

    assert len(set(report.added)) == 2
    assert len({o.id for o in updated.objects}) == len(updated.objects)


# ---------------------------------------------------------------------------
# Room finishes
# ---------------------------------------------------------------------------


def test_finishes_are_scoped_to_one_room(graph):
    """Repainting the living room must not touch the bedroom."""
    updated, report = review.apply_edits(
        graph,
        {"room_finishes": {"r1": {"wall": {"material": "wallpaper",
                                           "color_hex": "#E8DCC8"}}}},
    )

    assert report.finishes_changed == ["r1"]
    assert updated.room_by_id("r1").wall_finish.material == "wallpaper"
    assert updated.room_by_id("r2").wall_finish is None


def test_a_material_brings_its_own_shading_properties(graph):
    """Roughness belongs to the material, not to the user's taste."""
    updated, _ = review.apply_edits(
        graph, {"room_finishes": {"r1": {"floor": {"material": "marble"}}}}
    )

    finish = updated.room_by_id("r1").floor_finish
    assert finish.material == "marble"
    assert finish.roughness < 0.5  # polished stone, not carpet
    assert finish.confidence == 1.0  # stated by a human


def test_ceiling_type_is_validated(graph):
    updated, report = review.apply_edits(
        graph, {"room_finishes": {"r1": {"ceiling_type": "recessed"}}}
    )
    assert updated.room_by_id("r1").ceiling_type == "recessed"

    _, bad = review.apply_edits(
        graph, {"room_finishes": {"r1": {"ceiling_type": "holographic"}}}
    )
    assert any("ceiling type" in entry for entry in bad.rejected)


def test_an_unknown_room_finish_is_refused(graph):
    _, report = review.apply_edits(
        graph, {"room_finishes": {"nowhere": {"wall": {"material": "wood"}}}}
    )
    assert any("unknown room" in entry for entry in report.rejected)


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------


def test_light_properties_can_be_edited(graph):
    updated, report = review.apply_edits(
        graph,
        {"light_overrides": {"l1": {"power_w": 120.0, "color_temperature_k": 2700.0}}},
    )

    light = updated.lights[0]
    assert report.lights_changed == ["l1"]
    assert light.power_w == 120.0
    assert light.color_temperature_k == 2700.0


def test_changing_a_light_kind_updates_its_mounting(graph):
    updated, _ = review.apply_edits(
        graph, {"light_overrides": {"l1": {"kind": "floor_lamp"}}}
    )

    light = updated.lights[0]
    assert light.kind == "floor_lamp"
    assert light.mounting == "floor"


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("power_w", 999_999.0, review.MAX_POWER_W),
        ("power_w", -50.0, 0.0),
        ("color_temperature_k", 50.0, review.MIN_COLOR_TEMPERATURE_K),
        ("color_temperature_k", 50_000.0, review.MAX_COLOR_TEMPERATURE_K),
    ],
)
def test_lighting_values_are_clamped_to_what_renders(graph, field, value, expected):
    updated, _ = review.apply_edits(graph, {"light_overrides": {"l1": {field: value}}})
    assert getattr(updated.lights[0], field) == expected


def test_a_light_height_is_clamped_to_the_ceiling(graph):
    updated, _ = review.apply_edits(
        graph, {"light_overrides": {"l1": {"position": {"x": 3.0, "y": 3.0, "z": 99.0}}}}
    )
    assert updated.lights[0].position.z == graph.room_by_id("r1").ceiling_height


def test_a_light_can_be_added(graph):
    updated, report = review.apply_edits(
        graph, {"add_lights": [{"kind": "floor_lamp", "room_id": "r1", "power_w": 40.0}]}
    )

    assert len(report.lights_added) == 1
    added = next(x for x in updated.lights if x.id == report.lights_added[0])
    assert added.kind == "floor_lamp"
    assert added.power_w == 40.0
    # Reported as added, not also as changed — one action, one entry.
    assert added.id not in report.lights_changed


def test_an_unrecognised_luminaire_is_refused(graph):
    _, report = review.apply_edits(
        graph, {"add_lights": [{"kind": "plasma_globe", "room_id": "r1"}]}
    )
    assert report.lights_added == []
    assert any("luminaire" in entry for entry in report.rejected)


def test_unrecognised_light_keys_are_reported(graph):
    _, report = review.apply_edits(
        graph, {"light_overrides": {"l1": {"brightnes": 5}}}
    )
    assert any("brightnes" in entry for entry in report.rejected)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_every_new_edit_survives_a_save_and_reload(graph, tmp_path):
    """Generation reads the graph off disk, so all of this has to persist."""
    updated, report = review.apply_edits(
        graph,
        {
            "object_overrides": {
                "sofa": {"asset": "sofa_deep_lounge", "material": "leather",
                         "color_hex": "#3A2A20", "locked": True}
            },
            "add_objects": [{"source_id": "sofa", "position": {"x": 4.0, "y": 3.0}}],
            "room_finishes": {"r1": {"floor": {"material": "marble"},
                                     "ceiling_type": "recessed"}},
            "light_overrides": {"l1": {"power_w": 120.0}},
            "add_lights": [{"kind": "floor_lamp", "room_id": "r1"}],
        },
    )
    assert report.rejected == []

    path = tmp_path / "scene_graph.json"
    updated.save(str(path))
    reloaded = SceneGraph.load(str(path))

    sofa = reloaded.object_by_id("sofa")
    assert sofa.asset == "sofa_deep_lounge"
    assert sofa.material == "leather"
    assert sofa.color_hex == "#3A2A20"
    assert sofa.locked is True
    assert reloaded.object_by_id(report.added[0]) is not None
    assert reloaded.room_by_id("r1").floor_finish.material == "marble"
    assert reloaded.room_by_id("r1").ceiling_type == "recessed"
    assert len(reloaded.lights) == 2


def test_the_review_payload_carries_every_editor_vocabulary(graph):
    payload = review.build_review(graph)
    vocabulary = payload["vocabulary"]

    assert set(vocabulary) >= {
        "room_types", "categories", "materials", "ceiling_types",
        "light_kinds", "assets",
    }
    # The asset browser filters by category, so that field has to be present.
    assert all("category" in entry for entry in vocabulary["assets"])
    # The material picker filters by surface.
    assert all("applies_to" in entry for entry in vocabulary["materials"])
