"""The CAD evidence bridge: one reader, and every stage can see what it read.

Downstream stages need more than walls — which block a toilet is, what a
label says and where, what a block's ``ROOM_NAME`` attribute holds — and they
used to get it from a second DXF parser. These tests hold the replacement to
two promises: the evidence that reaches them is at least what the old parser
provided, and every element of the model can be traced to the entities it
was built from without reading the DXF again.
"""

from __future__ import annotations

import json
import os

import pytest

from modules.recon import compat
from modules.recon import evidence as EV
from modules.recon import read as RD
from modules.recon.pipeline import reconstruct

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
APARTMENT = os.path.join(FIX, "apartment.dxf")


def need(*parts):
    path = os.path.join(FIX, *parts)
    if not os.path.exists(path):
        pytest.skip("fixture missing: %s" % os.path.join(*parts))
    return path


class TestParityWithTheRetiredParser:
    """What the semantic tier received before, it still receives."""

    @pytest.fixture(scope="class")
    def both(self):
        from cad import read_dxf
        path = need("apartment.dxf")
        legacy = read_dxf(path, log=lambda *a, **k: None)
        bridged = EV.to_cad_document(RD.read(path))
        return legacy, bridged

    def test_the_same_room_labels(self, both):
        legacy, bridged = both
        assert sorted((t.text, t.room_type) for t in bridged.room_labels()) == \
            sorted((t.text, t.room_type) for t in legacy.room_labels())

    def test_the_same_blocks_and_categories(self, both):
        legacy, bridged = both
        assert sorted((b.name, b.category, b.kind) for b in bridged.blocks) == \
            sorted((b.name, b.category, b.kind) for b in legacy.blocks)

    def test_block_attributes_survive(self, both):
        legacy, bridged = both
        assert sorted(t.text for t in bridged.texts if t.dxftype == "ATTRIB") == \
            sorted(t.text for t in legacy.texts if t.dxftype == "ATTRIB")

    def test_the_same_dimensions(self, both):
        legacy, bridged = both
        assert sorted(round(d.metres, 3) for d in bridged.dimensions) == \
            sorted(round(d.metres, 3) for d in legacy.dimensions)

    def test_layer_roles_use_the_semantic_vocabulary(self, both):
        legacy, bridged = both
        roles_legacy = {l.name: l.role for l in legacy.layers if l.entity_count}
        roles_bridged = {l.name: l.role for l in bridged.layers}
        for name, role in roles_legacy.items():
            assert roles_bridged.get(name) == role, name

    def test_but_in_the_reconstructions_own_frame(self, both):
        """The retired parser centred the plan; the bridge shares the model's frame."""
        _legacy, bridged = both
        d = reconstruct(need("apartment.dxf"))
        assert bridged.origin_offset == pytest.approx(d.origin_offset)
        assert bridged.units.scale_to_m == d.units.scale_to_m


class TestTraceability:
    @pytest.fixture(scope="class")
    def d(self):
        return reconstruct(need("doors", "d02_symbol_blocks.dxf"))

    def test_a_symbol_door_cites_its_swing_and_leaf(self, d):
        index = EV.EvidenceIndex(d.source_evidence)
        door = next(o for o in d.openings if o.classification == "door" and o.swing != "double")
        cited = index.cited_by(door.id)
        types = sorted(e["entity_type"] for e in cited)
        assert "ARC" in types and ("LINE" in types or "LWPOLYLINE" in types)
        assert all(e["block"] and e["block"].startswith("A$C") for e in cited)

    def test_block_references_carry_transform_and_attributes(self, d):
        index = EV.EvidenceIndex(d.source_evidence)
        inserts = [e for e in index.entities.values() if e["entity_type"] == "INSERT"]
        assert inserts
        mirrored = [e for e in inserts if e["transform"]["scale"][1] < 0]
        assert mirrored, "the fixture mirrors half its doors"

    def test_a_jamb_door_cites_its_jambs(self):
        d = reconstruct(need("doors", "d01_jamb_doors.dxf"))
        index = EV.EvidenceIndex(d.source_evidence)
        for o in d.openings:
            if o.evidence != "jambs":
                continue
            cited = index.cited_by(o.id)
            assert len(cited) == 2
            assert {e["layer"] for e in cited} == {"A-DOOR"}
            assert all(index.citing(e["entity_id"]) == [o.id] for e in cited)

    def test_a_room_cites_the_label_that_named_it(self):
        d = reconstruct(need("plans", "t03_multi_room.dxf"))
        index = EV.EvidenceIndex(d.source_evidence)
        named = [r for r in d.rooms if r.label]
        assert named
        for r in named:
            texts = [e["text"] for e in index.cited_by(r.id) if e["entity_type"] == "TEXT"]
            assert texts and r.label.split()[0] in " ".join(texts).upper(), r.label

    def test_spatial_lookup(self, d):
        index = EV.EvidenceIndex(d.source_evidence)
        o = d.openings[0]
        near = index.near(o.position, 1.5)
        assert near

    def test_the_evidence_survives_json(self, d):
        again = EV.EvidenceIndex(json.loads(json.dumps(d.source_evidence)))
        assert len(again.entities) == len(EV.EvidenceIndex(d.source_evidence).entities)


class TestReachesTheSemanticTier:
    def test_vision_receives_cad_evidence_from_a_reconstruction(self):
        from vision.pipeline import _classify_from_cad
        from vision.rooms import regions_from_reconstruction
        geo = compat.to_geometry_json(reconstruct(need("apartment.dxf")))
        lines = []
        document, results = _classify_from_cad(
            geo, regions_from_reconstruction(geo), lines.append)
        assert document is not None
        assert not any("legacy extractor" in l for l in lines)
        typed = [r for r in results.values() if r.room_type != "unknown"]
        assert len(typed) >= 5

    def test_geometry_json_carries_both_views(self):
        geo = compat.to_geometry_json(reconstruct(need("plans", "t01_simple_rect.dxf")))
        assert geo["source_evidence"]["schema"] == EV.EVIDENCE_SCHEMA
        assert geo["cad"]["stats"]["reader"] == "recon.read"
