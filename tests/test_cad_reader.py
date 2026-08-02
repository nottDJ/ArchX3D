"""End-to-end DXF reading, against a realistic generated apartment plan.

The fixture is a 2-bedroom apartment drawn the way a real one is: AIA layer
names, MTEXT room labels, named furniture and sanitary blocks, doors and
windows on their own layers, dimensions, a north arrow, and a title block
placed far outside the building.

Each test names the specific failure mode it guards, because most of these
behaviours look like details and are in fact the difference between a plan
being understood and every room coming back "unknown".
"""

from __future__ import annotations

import os

import pytest

from cad import read_dxf
from cad.schema import CadDocument, Source

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "apartment.dxf")


@pytest.fixture(scope="module")
def document():
    if not os.path.exists(FIXTURE):
        pytest.skip(f"fixture not generated: {FIXTURE}")
    return read_dxf(FIXTURE, log=lambda *a, **k: None)


class TestUnitsAndOrientation:
    def test_millimetre_drawing_is_detected_from_header(self, document):
        assert document.units.scale_to_m == pytest.approx(0.001)
        assert document.units.unit_name == "millimetres"
        assert document.units.method == "header"

    def test_building_measures_correctly_after_conversion(self, document):
        """A unit error is silent and catastrophic; this is the guard."""
        width = document.bounds_max[0] - document.bounds_min[0]
        depth = document.bounds_max[1] - document.bounds_min[1]
        assert width == pytest.approx(14.0, abs=0.3)
        assert depth == pytest.approx(7.6, abs=0.3)

    def test_north_is_read_from_the_header(self, document):
        assert document.north.confidence > 0.9
        assert document.north.source == Source.CAD_METADATA
        # $NORTHDIRECTION = pi/2 is +Y, which is 0 in our +Y-up convention.
        assert document.north.heading_deg == pytest.approx(0.0, abs=0.5)


class TestOriginNormalisation:
    def test_building_is_centred_on_the_origin(self, document):
        """Title blocks sit tens of metres away and must not drag the plan.

        Centring on *all* geometry rather than on structure is what puts a
        model "somewhere off in space" when it is opened.
        """
        centre_x = (document.bounds_min[0] + document.bounds_max[0]) / 2.0
        centre_y = (document.bounds_min[1] + document.bounds_max[1]) / 2.0
        assert centre_x == pytest.approx(0.0, abs=0.3)
        assert centre_y == pytest.approx(0.0, abs=0.3)

    def test_bounds_exclude_drawing_apparatus(self, document):
        """DEFPOINTS construction geometry spans 60 m; the flat does not."""
        width = document.bounds_max[0] - document.bounds_min[0]
        assert width < 20.0


class TestEntityExtraction:
    def test_room_labels_are_recovered(self, document):
        labels = document.room_labels()
        found = {label.room_type for label in labels}
        assert {"living_room", "kitchen", "bedroom", "bathroom", "hallway"} <= found

    def test_master_bedroom_keeps_its_specific_label(self, document):
        assert any(t.room_type == "master_bedroom" for t in document.room_labels())

    def test_blocks_are_classified_into_categories(self, document):
        categories = {b.category for b in document.blocks}
        # The fixtures that decide room types.
        assert {"toilet", "shower", "washbasin", "sink", "cooktop"} <= categories
        # And the furniture.
        assert {"bed", "wardrobe", "sofa", "dining_table"} <= categories

    def test_doors_and_windows_are_kept_not_blacklisted(self, document):
        """v2 discarded these layers outright; they are the opening evidence."""
        assert len(document.blocks_of_kind("door")) == 6
        assert len(document.blocks_of_kind("window")) == 5

    def test_dimensions_are_read(self, document):
        assert len(document.dimensions) == 3
        assert all(d.metres > 0 for d in document.dimensions)

    def test_block_attributes_become_metadata_labels(self, document):
        """A ROOM_NAME attribute is tier-1 evidence and outranks loose text."""
        attributes = [t for t in document.texts if t.dxftype == "ATTRIB"]
        assert any(t.room_type == "hallway" for t in attributes)
        assert all(t.source == Source.CAD_METADATA for t in attributes)

    def test_title_block_text_is_read_but_not_a_room(self, document):
        notes = [t for t in document.texts if t.role == "note"]
        assert any("FLOOR PLAN" in t.text.upper() for t in notes)
        assert not any(t.room_type for t in notes)


class TestLayerRoles:
    def test_layers_receive_semantic_roles(self, document):
        roles = {layer.name: layer.role for layer in document.layers}
        assert roles["A-WALL"] == "wall"
        assert roles["A-DOOR"] == "door"
        assert roles["A-GLAZ"] == "window"
        assert roles["A-FLOR-PFIX"] == "plumbing_fixture"
        assert roles["A-ANNO-TEXT"] == "text"

    def test_only_wall_segments_become_walls(self, document):
        """Furniture geometry on a furniture layer must not extrude as a wall."""
        walls = document.wall_segments()
        assert walls
        assert all(segment.role == "wall" for segment in walls)
        assert all(segment.layer.upper().startswith("A-WALL") for segment in walls)

    def test_block_geometry_is_attributed_to_the_block(self, document):
        """Block internals are drawn on layer 0 and inherit the INSERT's layer.

        Honouring that convention is what stops a wardrobe's outline being
        extruded as a wall.
        """
        furniture = document.segments_with_role("furniture", "casework",
                                                "plumbing_fixture")
        assert furniture
        assert all(s.source == Source.CAD_BLOCK for s in furniture)


class TestSerialisation:
    def test_round_trips_through_dict(self, document):
        restored = CadDocument.from_dict(document.to_dict())
        assert len(restored.segments) == len(document.segments)
        assert len(restored.blocks) == len(document.blocks)
        assert len(restored.room_labels()) == len(document.room_labels())
        assert restored.units.scale_to_m == document.units.scale_to_m

    def test_geometry_json_keeps_the_legacy_shape(self, document):
        """Every existing consumer reads ``metadata`` and ``walls``."""
        geometry = document.to_geometry_json()
        assert set(geometry) >= {"metadata", "walls", "cad"}
        assert geometry["metadata"]["units"] == "meters"
        assert geometry["metadata"]["segment_count"] == len(geometry["walls"])
        for wall in geometry["walls"]:
            assert set(wall) == {"start", "end", "source_entity", "layer"}
            assert len(wall["start"]) == 2

    def test_cad_model_recoverable_from_geometry_json(self, document):
        recovered = CadDocument.from_geometry_json(document.to_geometry_json())
        assert recovered is not None
        assert len(recovered.room_labels()) == len(document.room_labels())

    def test_legacy_geometry_json_yields_no_cad_model(self):
        """An old geometry file is a valid input, not an error."""
        assert CadDocument.from_geometry_json({"metadata": {}, "walls": []}) is None


class TestRobustness:
    def test_uids_are_stable_across_reads(self, document):
        """Content-derived ids let a user's correction survive a re-parse."""
        again = read_dxf(FIXTURE, log=lambda *a, **k: None)
        assert [b.uid for b in again.blocks] == [b.uid for b in document.blocks]

    def test_no_warnings_on_a_well_formed_file(self, document):
        assert document.warnings == []

    def test_missing_file_raises(self):
        with pytest.raises(IOError):
            read_dxf("does_not_exist.dxf", log=lambda *a, **k: None)
