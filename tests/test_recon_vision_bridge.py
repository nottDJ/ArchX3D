"""The reconstruction's room typing reaching the vision stage.

``geometry.json`` written by the reconstruction carries no ``cad`` document,
so the semantic tier's CAD evidence is unavailable and it falls back to
typing rooms from floor area. But the reconstruction has *already* typed every
room from the drawing's own labels, and that answer is in the file. This is
the bridge, and these tests are what keep it wired.
"""

from __future__ import annotations

import pytest

from vision.pipeline import _stamp_recon_room_types


class FakeRegion:
    def __init__(self, rid, lo, hi, room_type="unknown"):
        self.id = rid
        self.bounds_min = lo
        self.bounds_max = hi
        self.room_type = room_type
        self.room_type_confidence = 0.0


def geometry(rooms):
    return {"metadata": {}, "walls": [], "rooms": rooms}


def test_an_untyped_region_takes_the_reconstructions_answer():
    regions = [FakeRegion("a", (0, 0), (5, 4))]
    n = _stamp_recon_room_types(geometry([
        {"id": "r1", "room_type": "kitchen", "label": "KITCHEN",
         "centroid": [2.5, 2.0], "label_confidence": 0.95},
    ]), regions, lambda *_: None)
    assert n == 1
    assert regions[0].room_type == "kitchen"
    assert regions[0].room_type_confidence == pytest.approx(0.95)


def test_a_region_the_semantic_tier_already_typed_is_left_alone():
    regions = [FakeRegion("a", (0, 0), (5, 4), room_type="bedroom")]
    _stamp_recon_room_types(geometry([
        {"id": "r1", "room_type": "kitchen", "centroid": [2.5, 2.0]},
    ]), regions, lambda *_: None)
    assert regions[0].room_type == "bedroom"


def test_a_room_elsewhere_does_not_type_this_region():
    regions = [FakeRegion("a", (0, 0), (5, 4))]
    _stamp_recon_room_types(geometry([
        {"id": "r1", "room_type": "garage", "centroid": [40.0, 40.0]},
    ]), regions, lambda *_: None)
    assert regions[0].room_type == "unknown"


def test_unknown_room_types_are_not_propagated():
    regions = [FakeRegion("a", (0, 0), (5, 4))]
    assert _stamp_recon_room_types(geometry([
        {"id": "r1", "room_type": "unknown", "centroid": [2.5, 2.0]},
    ]), regions, lambda *_: None) == 0


def test_a_legacy_geometry_file_is_handled():
    """No rooms key at all — the legacy extractor's output."""
    regions = [FakeRegion("a", (0, 0), (5, 4))]
    assert _stamp_recon_room_types({"metadata": {}, "walls": []}, regions,
                                   lambda *_: None) == 0


def test_it_works_on_a_real_reconstruction():
    import os

    from modules.recon import compat
    from modules.recon.pipeline import reconstruct

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "plans", "t03_multi_room.dxf")
    if not os.path.exists(path):
        pytest.skip("fixture not generated")
    geo = compat.to_geometry_json(reconstruct(path))
    regions = [FakeRegion(r["id"],
                          (min(p[0] for p in r["polygon"]),
                           min(p[1] for p in r["polygon"])),
                          (max(p[0] for p in r["polygon"]),
                           max(p[1] for p in r["polygon"])))
               for r in geo["rooms"]]
    stamped = _stamp_recon_room_types(geo, regions, lambda *_: None)
    assert stamped >= 5
    types = {r.room_type for r in regions}
    assert "bedroom" in types and "bathroom" in types
