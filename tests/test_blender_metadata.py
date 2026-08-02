"""Tests for the semantic metadata stamped onto the exported GLB.

The viewer's roof toggle, view modes and room navigation are all driven by
``archx3d_kind`` and the scene manifest. Getting a classification wrong here
hides the wrong geometry in the browser, and the failure is silent — the model
still loads, it is just missing a wall.

``blender.metadata``'s classification and manifest logic is deliberately
bpy-free so it can be tested here, without Blender, in milliseconds. Only the
tagging pass itself needs a running Blender, and it contains no decisions.
"""

from __future__ import annotations

import json

import pytest

from blender import metadata


class FakeObject:
    """The parts of a ``bpy.types.Object`` the classifier actually reads."""

    def __init__(self, name: str, props: dict | None = None, type: str = "MESH"):
        self.name = name
        self.type = type
        self._props = props or {}

    def get(self, key, default=None):
        return self._props.get(key, default)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Walls", metadata.WALL),
        ("Floor", metadata.FLOOR),
        ("Ceiling", metadata.ROOF),
        ("Roof", metadata.ROOF),
    ],
)
def test_shell_objects_are_classified_by_name(name, expected):
    assert metadata.classify(FakeObject(name)) == expected


def test_blender_deduplication_suffix_is_stripped():
    """A second floor plane is named ``Floor.001`` and is still a floor."""
    assert metadata.classify(FakeObject("Floor.001")) == metadata.FLOOR
    assert metadata.classify(FakeObject("Ceiling.017")) == metadata.ROOF


def test_ceiling_fan_is_not_the_ceiling():
    """The trap a substring match would fall into.

    ``ceiling_fan`` starts with "ceiling"; if the roof toggle swept it up, the
    fan would vanish whenever a user looked inside the building and nothing
    would explain why.
    """
    fan = FakeObject(
        "ceiling_fan_fan_1",
        {"archx3d_category": "ceiling_fan", "archx3d_group": "decor"},
    )
    assert metadata.classify(fan) == metadata.DECOR


@pytest.mark.parametrize(
    "group,expected",
    [
        ("furniture", metadata.FURNITURE),
        ("decor", metadata.DECOR),
        ("appliance", metadata.APPLIANCE),
    ],
)
def test_catalogue_group_decides_the_kind(group, expected):
    obj = FakeObject("thing_1", {"archx3d_group": group})
    assert metadata.classify(obj) == expected


def test_unknown_group_falls_back_to_furniture():
    """The catalogue may gain a group before this module knows about it.

    Furniture is the safe answer: it keeps the object out of the structural
    isolation modes and out of the collision set, which is where a
    misclassified ornament would do the most damage.
    """
    obj = FakeObject("thing_1", {"archx3d_group": "sculpture"})
    assert metadata.classify(obj) == metadata.FURNITURE


def test_category_without_group_is_still_furniture():
    obj = FakeObject("sofa_sofa_1", {"archx3d_category": "sofa"})
    assert metadata.classify(obj) == metadata.FURNITURE


def test_builder_prefixes():
    assert metadata.classify(FakeObject("arch_column_2")) == metadata.STRUCTURE
    assert metadata.classify(FakeObject("Light_l1")) == metadata.LIGHT
    assert metadata.classify(FakeObject("Cutter_op1")) == metadata.OPENING


@pytest.mark.parametrize("name", ["Sun_Daylight", "Key_Sun", "Fill_Area", "KeyLight"])
def test_rig_lights_are_classified(name):
    assert metadata.classify(FakeObject(name, type="LIGHT")) == metadata.LIGHT


def test_any_light_datablock_is_a_light():
    assert metadata.classify(FakeObject("Untitled", type="LIGHT")) == metadata.LIGHT


def test_unrecognised_object_is_unknown_not_guessed():
    """An omission the viewer can recover from beats a confident wrong answer.

    ``unknown`` still collides and still renders; it simply is not isolated by
    the view modes. Guessing "wall" would put an ornament in Structure view.
    """
    assert metadata.classify(FakeObject("Mystery")) == metadata.UNKNOWN


def test_every_kind_is_in_the_published_vocabulary():
    """The viewer switches on these strings; an unlisted one is unhandleable."""
    names = ["Walls", "Floor", "Ceiling", "arch_x", "Light_x", "Cutter_x", "Mystery"]
    for name in names:
        assert metadata.classify(FakeObject(name)) in metadata.KINDS


# ---------------------------------------------------------------------------
# Scene manifest
# ---------------------------------------------------------------------------


def test_manifest_without_a_graph_is_still_valid():
    """A DXF-only build has no scene graph, and must still produce a manifest."""
    manifest = metadata.scene_manifest(None)

    assert manifest["version"] == metadata.METADATA_VERSION
    assert manifest["rooms"] == []
    # The viewer converts plan metres using this; an omission would silently
    # mirror the building.
    assert manifest["up_axis"] == "Y"
    assert manifest["units"] == "metre"


def test_manifest_describes_every_room(preview_graph):
    manifest = metadata.scene_manifest(preview_graph)

    assert len(manifest["rooms"]) == len(preview_graph.rooms)

    for room in manifest["rooms"]:
        assert room["id"]
        assert room["name"]
        assert len(room["bounds_min"]) == 2
        assert len(room["bounds_max"]) == 2
        assert room["ceiling_height"] > 0


def test_manifest_names_are_human_readable(preview_graph):
    """`living_room` becomes `Living Room` — the room list shows this verbatim."""
    manifest = metadata.scene_manifest(preview_graph)
    for room in manifest["rooms"]:
        assert "_" not in room["name"]


def test_manifest_counts_objects_per_room(preview_graph):
    manifest = metadata.scene_manifest(preview_graph)
    total = sum(room["object_count"] for room in manifest["rooms"])
    placed = len([o for o in preview_graph.objects if o.room_id])
    assert total == placed


def test_manifest_is_json_serialisable(preview_graph):
    """It travels as a JSON string in a Blender custom property.

    A value that cannot be encoded would be dropped at export and the viewer
    would silently lose room navigation.
    """
    payload = json.dumps(metadata.scene_manifest(preview_graph))
    assert json.loads(payload)["rooms"]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_summarise_orders_by_count():
    line = metadata.summarise({"wall": 1, "furniture": 12, "roof": 1})
    assert line.startswith("12 furniture")
    # Ties break alphabetically, so the log line is stable between runs.
    assert line.endswith("1 roof, 1 wall")


def test_summarise_says_so_when_nothing_was_tagged():
    assert metadata.summarise({}) == "nothing tagged"
