"""DXF file in, classified rooms out.

The end-to-end guarantee this project's Stage 10 objective actually asks for:
given a CAD file, ArchX should correctly identify room types. These tests run
the real reader, the real segmentation and the real classifier against the
generated apartment fixture, and assert on the answers a person would check.
"""

from __future__ import annotations

import json
import os

import pytest

from cad import read_dxf
from cad.schema import CadDocument
from semantic import build_inputs, classify_plan, summarise

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "apartment.dxf"
)

#: What the fixture actually is, room by room. Keyed by the label the drawing
#: carries, because region ids depend on segmentation ordering.
EXPECTED = {
    "LIVING ROOM": "living_room",
    "KITCHEN": "kitchen",
    "BEDROOM 2": "bedroom",
    "HALL": "hallway",
    "BATH": "bathroom",
    "MASTER BED": "bedroom",
}


def _segment(geometry):
    from vision import rooms as room_seg

    result = room_seg.segment_rooms(geometry["walls"], wall_thickness=0.15)
    assert result.ok, result.stats
    return result.regions


@pytest.fixture(scope="module")
def classified():
    if not os.path.exists(FIXTURE):
        pytest.skip(f"fixture not generated: {FIXTURE}")
    pytest.importorskip("scipy", reason="room segmentation needs scipy")

    document = read_dxf(FIXTURE, log=lambda *a, **k: None)
    geometry = document.to_geometry_json()
    regions = _segment(geometry)
    results = classify_plan(build_inputs(document, regions))
    return document, regions, {r.room_id: r for r in results}


class TestFullPipeline:
    def test_every_room_is_identified(self, classified):
        """The headline: no room comes back "unknown" on a labelled plan."""
        _, _, results = classified
        unknown = [r.room_id for r in results.values() if r.room_type == "unknown"]
        assert not unknown, f"unidentified rooms: {unknown}"

    def test_the_expected_room_types_are_all_present(self, classified):
        _, _, results = classified
        found = sorted(r.room_type for r in results.values())
        assert found == sorted(EXPECTED.values())

    def test_classification_is_confident(self, classified):
        _, _, results = classified
        stats = summarise(list(results.values()))
        assert stats["mean_confidence"] > 0.85
        assert stats["confident"] == stats["rooms"]

    def test_no_conflicts_on_a_consistent_drawing(self, classified):
        """Labels and blocks agree throughout, so nothing should be flagged."""
        _, _, results = classified
        conflicts = [c.detail for r in results.values() for c in r.conflicts]
        assert conflicts == []

    def test_every_room_explains_itself(self, classified):
        _, _, results = classified
        for result in results.values():
            assert result.reasons, f"{result.room_id} gave no reasons"

    def test_the_bathroom_is_found_by_its_fixtures(self, classified):
        _, _, results = classified
        bathroom = next(r for r in results.values() if r.room_type == "bathroom")
        reasons = " ".join(bathroom.reasons).upper()
        assert "TOILET" in reasons

    def test_the_kitchen_is_found_by_its_fixtures(self, classified):
        _, _, results = classified
        kitchen = next(r for r in results.values() if r.room_type == "kitchen")
        reasons = " ".join(kitchen.reasons).upper()
        assert "COOKTOP" in reasons or "SINK" in reasons

    def test_master_bedroom_keeps_its_specific_label(self, classified):
        _, _, results = classified
        assert any(r.specific_type == "master_bedroom" for r in results.values())

    def test_bed_is_not_placed_in_the_bathroom(self, classified):
        """Exclusive attribution: one label and one block name one room only.

        Proximity-based attribution let the BATH label reach the master
        bedroom next door and classify a room containing a bed as a bathroom.
        """
        _, _, results = classified
        for result in results.values():
            reasons = " ".join(result.reasons).upper()
            if result.room_type == "bathroom":
                assert "BED BLOCK" not in reasons


class TestWithoutLabels:
    """A drawing with no text at all must still be understood from blocks."""

    @pytest.fixture(scope="class")
    def unlabelled(self):
        if not os.path.exists(FIXTURE):
            pytest.skip("fixture not generated")
        pytest.importorskip("scipy")

        document = read_dxf(FIXTURE, log=lambda *a, **k: None)
        geometry = document.to_geometry_json()
        regions = _segment(geometry)
        document.texts = []  # Strip every label and attribute.
        return {r.room_id: r for r in classify_plan(build_inputs(document, regions))}

    def test_furnished_rooms_are_still_identified(self, unlabelled):
        found = {r.room_type for r in unlabelled.values()}
        assert {"bathroom", "kitchen", "bedroom", "living_room"} <= found

    def test_identification_rests_on_blocks(self, unlabelled):
        bathroom = next(r for r in unlabelled.values() if r.room_type == "bathroom")
        assert "TOILET" in " ".join(bathroom.reasons).upper()
        assert bathroom.decided_by == ""  # No authority; fusion decided.

    def test_unfurnished_circulation_is_found_from_topology(self, unlabelled):
        """The hall holds no furniture at all, and is still identified.

        Nothing is invented here: a room with four doors, no window and
        openings onto the kitchen, bathroom and living room is circulation by
        construction. Object evidence is absent and the topological signals
        carry it alone — which is the case adjacency and door-count exist for.

        (The guarantee that a room with *no* discriminating evidence stays
        ``unknown`` is pinned in test_semantic_classifier.py, where the input
        can be made genuinely featureless.)
        """
        hall = next(r for r in unlabelled.values() if r.room_type == "hallway")
        assert not any("block" in reason.lower() for reason in hall.reasons)
        assert any("door" in reason.lower() for reason in hall.reasons)

    def test_confidence_is_lower_without_labels(self, unlabelled, classified):
        _, _, labelled = classified
        assert (
            summarise(list(unlabelled.values()))["mean_confidence"]
            < summarise(list(labelled.values()))["mean_confidence"]
        )


class TestExtractorContract:
    """``dxf_extractor`` is step 1 of the CLI pipeline; its output shape is
    consumed by segmentation, Blender and the web viewer."""

    def test_writes_a_backward_compatible_geometry_json(self, tmp_path):
        if not os.path.exists(FIXTURE):
            pytest.skip("fixture not generated")

        import dxf_extractor

        output = tmp_path / "geometry.json"
        dxf_extractor.extract_walls(
            FIXTURE, str(output), log=lambda *a, **k: None
        )

        document = json.loads(output.read_text(encoding="utf-8"))
        assert set(document) >= {"metadata", "walls", "cad"}
        assert document["walls"]
        assert document["metadata"]["units"] == "meters"
        assert document["metadata"]["segment_count"] == len(document["walls"])

    def test_embedded_cad_model_survives_the_round_trip(self, tmp_path):
        if not os.path.exists(FIXTURE):
            pytest.skip("fixture not generated")

        import dxf_extractor

        output = tmp_path / "geometry.json"
        dxf_extractor.extract_walls(FIXTURE, str(output), log=lambda *a, **k: None)

        geometry = json.loads(output.read_text(encoding="utf-8"))
        recovered = CadDocument.from_geometry_json(geometry)
        assert recovered is not None
        assert len(recovered.room_labels()) >= 6
