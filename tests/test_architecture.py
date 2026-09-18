"""The architectural IR is the single source of truth after reconstruction.

    DXF -> modules.recon (one reader) -> Drawing / Building / Level
        -> validation -> building.json -> Blender -> GLB
                      -> geometry.json (a projection) -> 2D stages

These tests hold that shape in place. They fail when a production path starts
reading the DXF a second time, when a 2D stage starts segmenting rooms or
hunting openings of its own on a reconstruction, or when the 3D stage builds
anything that did not come from the validated model.
"""

from __future__ import annotations

import json
import os
import struct

import pytest

from modules.recon import compat
from modules.recon import evidence as EV
from modules.recon.pipeline import reconstruct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANS = os.path.join(ROOT, "tests", "fixtures", "plans")
MULTI = os.path.join(ROOT, "tests", "fixtures", "multi")
APARTMENT = os.path.join(ROOT, "tests", "fixtures", "apartment.dxf")


def need(path):
    if not os.path.exists(path):
        pytest.skip("fixture missing: %s" % os.path.basename(path))
    return path


# ---------------------------------------------------------------------------
# One DXF reader
# ---------------------------------------------------------------------------

#: The only production module allowed to open a DXF.
READER = os.path.join("modules", "recon", "read.py")

#: ``cad.reader`` survives as a library with its own tests; nothing on a
#: pipeline, API, desktop or Blender path may call it.
LIBRARY_ONLY = {os.path.join("modules", "cad", "reader.py"),
                os.path.join("modules", "cad", "__init__.py")}

def _parses_dxf(source: str) -> list:
    """Imports and calls in real code (not prose) that open a DXF."""
    import ast
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = {a.name for a in node.names}
            if mod.endswith("cad.reader") or (mod.endswith("cad") and "read_dxf" in names):
                found.append("import %s" % mod)
            if mod == "ezdxf" and names & {"readfile", "recover"}:
                found.append("from ezdxf import %s" % sorted(names))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.endswith("cad.reader"):
                    found.append("import %s" % a.name)
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in ("readfile", "read_dxf"):
                found.append("call .%s" % f.attr)
            elif isinstance(f, ast.Name) and f.id == "read_dxf":
                found.append("call read_dxf")
    return found


def _production_sources():
    roots = [os.path.join(ROOT, "modules")]
    files = [os.path.join(ROOT, name) for name in
             ("main.py", "server.py", "walkthrough.py")]
    files.append(os.path.join(ROOT, "desktop", "backend_main.py"))
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            files.extend(os.path.join(dirpath, f) for f in filenames if f.endswith(".py"))
    return [f for f in files if os.path.exists(f)]


def test_only_the_reconstruction_reader_parses_dxf():
    offenders = []
    for path in _production_sources():
        rel = os.path.relpath(path, ROOT)
        if rel == READER or rel in LIBRARY_ONLY:
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        hits = _parses_dxf(source)
        if hits:
            offenders.append((rel, hits))
    assert not offenders, "a second DXF parser is in use: %s" % offenders


def test_the_guard_recognises_a_second_parser():
    """The guard above is only worth something if it can fail."""
    assert _parses_dxf("from cad import read_dxf\nread_dxf('x.dxf')")
    assert _parses_dxf("import ezdxf\nezdxf.readfile('x.dxf')")
    assert _parses_dxf("from cad.reader import read")
    assert not _parses_dxf('"""from cad import read_dxf"""\nx = 1')


def test_the_extractor_entry_point_runs_the_reconstruction(tmp_path):
    import dxf_extractor
    out = tmp_path / "geometry.json"
    dxf_extractor.extract_walls(need(os.path.join(PLANS, "t01_simple_rect.dxf")),
                                str(out), log=lambda *a, **k: None)
    geometry = json.loads(out.read_text(encoding="utf-8"))
    assert geometry["metadata"]["extractor"].startswith("recon.pipeline")
    assert geometry["rooms"] and geometry["openings"]


# ---------------------------------------------------------------------------
# 2D stages consume the model; they do not re-derive it
# ---------------------------------------------------------------------------

class TestVisionUsesTheModel:
    @pytest.fixture(scope="class")
    def geometry(self):
        return compat.to_geometry_json(
            reconstruct(need(os.path.join(PLANS, "t03_multi_room.dxf"))))

    def test_rooms_are_the_reconstructions_not_a_raster_segmentation(self, geometry):
        from vision import rooms as room_seg
        from vision.pipeline import PipelineConfig, _segment

        called = []
        original = room_seg.segment_rooms
        room_seg.segment_rooms = lambda *a, **k: called.append(1) or original(*a, **k)
        try:
            regions, stats = _segment(geometry, PipelineConfig(), lambda *a: None)
        finally:
            room_seg.segment_rooms = original
        assert not called, "a reconstruction must never be re-segmented"
        assert stats["mode"] == "reconstruction"
        assert sorted(r.id for r in regions) == sorted(r["id"] for r in geometry["rooms"])
        by_id = {r["id"]: r for r in geometry["rooms"]}
        for region in regions:
            assert region.area == pytest.approx(by_id[region.id]["area"], rel=1e-6)

    def test_rooms_connect_through_the_models_openings(self, geometry):
        from vision.rooms import regions_from_reconstruction
        regions = regions_from_reconstruction(geometry)
        assert any(r.connected_to for r in regions)
        for o in geometry["openings"]:
            if len(o["rooms"]) == 2:
                a, b = o["rooms"]
                region = next(r for r in regions if r.id == a)
                assert b in region.connected_to

    def test_openings_are_the_models(self, geometry):
        from vision.pipeline import _merge_reconstruction_openings
        merged = _merge_reconstruction_openings(geometry, [], lambda *a: None)
        assert sorted(o.id for o in merged) == sorted(o["id"] for o in geometry["openings"])
        assert all(o.wall_id.startswith("wall_") for o in merged)

    def test_legacy_geometry_is_still_segmented(self, rect_geometry):
        from vision.pipeline import PipelineConfig, _segment
        regions, stats = _segment(rect_geometry, PipelineConfig(), lambda *a: None)
        assert stats.get("mode") != "reconstruction"
        assert regions


class TestTheProjection:
    def test_every_element_names_its_storey(self):
        d = reconstruct(need(os.path.join(MULTI, "m05_two_storey_titled.dxf")))
        geo = compat.to_geometry_json(d)
        levels = {l["id"]: l for l in geo["metadata"]["levels"]}
        assert set(levels) == {"b1.l0", "b1.l1"}
        assert levels["b1.l1"]["elevation"] > levels["b1.l0"]["elevation"]
        for key in ("walls", "rooms", "openings"):
            assert geo[key]
            assert all(item["level_id"] in levels for item in geo[key]), key
        assert geo["metadata"]["level_structure"]["status"] == "RESOLVED"

    def test_the_semantic_document_comes_from_the_same_reader(self):
        from cad.schema import CadDocument
        d = reconstruct(need(APARTMENT))
        geo = compat.to_geometry_json(d)
        doc = CadDocument.from_geometry_json(geo)
        assert doc is not None
        assert doc.stats["reader"] == "recon.read"
        assert doc.units.scale_to_m == d.units.scale_to_m
        assert len(doc.room_labels()) >= 6
        assert any(t.dxftype == "ATTRIB" for t in doc.texts), \
            "block attributes must survive the single reader"
        assert {"toilet", "bed"} <= {b.category for b in doc.blocks}


class TestSourceEvidence:
    @pytest.fixture(scope="class")
    def d(self):
        return reconstruct(need(os.path.join(PLANS, "residential_us.dxf")))

    def test_every_wall_and_opening_cites_entities_that_exist(self, d):
        ev = d.source_evidence
        ids = {e["entity_id"] for e in ev["entities"]}
        assert ev["schema"] == EV.EVIDENCE_SCHEMA
        for wall in d.walls:
            cited = ev["links"]["walls"][wall.id]
            assert cited, wall.id
            assert set(cited) <= ids, wall.id
        for o in d.openings:
            assert set(ev["links"]["openings"][o.id]) <= ids, o.id

    def test_block_references_carry_their_transform(self, d):
        inserts = [e for e in d.source_evidence["entities"]
                   if e["entity_type"] == "INSERT"]
        assert inserts
        for e in inserts:
            assert set(e["transform"]) == {"insert", "rotation_deg", "scale"}
            assert e["block"]

    def test_a_wall_cites_only_its_own_plans_entities(self):
        """Two plans on shared gridlines must not borrow each other's lines."""
        d = reconstruct(need(os.path.join(MULTI, "m10_layer_levels.dxf")))
        for level in d.levels():
            layers = {w.layer for w in level.walls}
            assert len(layers) == 1, (level.id, layers)


# ---------------------------------------------------------------------------
# The 3D stage builds only the model
# ---------------------------------------------------------------------------

def test_the_generator_has_no_second_geometry_path():
    with open(os.path.join(ROOT, "modules", "blender_generator.py"), encoding="utf-8") as fh:
        source = fh.read()
    for forbidden in ("def create_walls", "def create_floor", "def create_ceiling",
                      "extrude_edge_only", "def load_geometry", "def build_openings",
                      "GEOMETRY_PATH"):
        assert forbidden not in source, forbidden
    assert "blender_build.build(" in source


class TestBlenderBuildFrames:
    def test_storeys_are_measured_where_they_are_built(self):
        from modules.blender_build import bounds, level_transform, room_transforms
        d = reconstruct(need(os.path.join(MULTI, "m05_two_storey_titled.dxf")))
        model = d.as_dict()
        x0, y0, x1, y1 = bounds(model)
        # Drawn 26 m wide across the sheet; built as one 10 m house.
        assert x1 - x0 == pytest.approx(10.15, abs=0.3)
        g, f = model["buildings"][0]["levels"]
        assert level_transform(f)[2] == pytest.approx(f["elevation"])
        rooms = room_transforms(model)
        assert all(rooms[r["id"]] == level_transform(f) for r in f["rooms"])

    def test_separate_buildings_are_not_moved(self):
        from modules.blender_build import bounds, room_transforms
        d = reconstruct(need(os.path.join(MULTI, "m01_house_and_garage.dxf")))
        model = d.as_dict()
        assert set(room_transforms(model).values()) == {(0.0, 0.0, 0.0)}
        x0, _, x1, _ = bounds(model)
        assert x1 - x0 > 19.0


# ---------------------------------------------------------------------------
# GLB validation
# ---------------------------------------------------------------------------

def _glb(nodes, accessors, meshes, extras=None):
    doc = {"asset": {"version": "2.0"}, "nodes": nodes, "meshes": meshes,
           "accessors": accessors, "bufferViews": [], "buffers": []}
    body = json.dumps(doc).encode("utf-8")
    body += b" " * ((4 - len(body) % 4) % 4)
    total = 12 + 8 + len(body)
    return struct.pack("<III", 0x46546C67, 2, total) + \
        struct.pack("<II", len(body), 0x4E4F534A) + body


class TestGlbValidate:
    def _write(self, tmp_path, z0=0.0, height=2.7, extent=(10.0, 7.0), name="Walls"):
        acc = [{"min": [0.0, 0.0, -extent[1]], "max": [extent[0], height, 0.0]}]
        meshes = [{"primitives": [{"attributes": {"POSITION": 0}}]}]
        nodes = [{"name": name, "mesh": 0, "translation": [0.0, z0, 0.0],
                  "extras": {"archx3d_kind": "wall", "archx3d_level": "b1.l0"}}]
        p = tmp_path / "m.glb"
        p.write_bytes(_glb(nodes, acc, meshes))
        return str(p)

    def _model(self, elevation=0.0):
        walls = [{"id": "w1", "start": [0, 0], "end": [10, 0], "thickness": 0.2},
                 {"id": "w2", "start": [10, 0], "end": [10, 7], "thickness": 0.2},
                 {"id": "w3", "start": [10, 7], "end": [0, 7], "thickness": 0.2}]
        return {"buildings": [{"id": "b1", "levels": [
            {"id": "b1.l0", "elevation": elevation, "placement": [0, 0], "walls": walls}]}]}

    def test_a_correct_model_passes(self, tmp_path):
        r = __import__("glb_validate").validate(self._write(tmp_path), self._model())
        assert r["ok"], r["errors"]

    def test_a_shoebox_sized_model_fails(self, tmp_path):
        """The original failure: a valid GLB 25 times too small."""
        path = self._write(tmp_path, extent=(0.4, 0.28))
        r = __import__("glb_validate").validate(path, self._model())
        assert not r["ok"]

    def test_a_storey_at_the_wrong_height_fails(self, tmp_path):
        path = self._write(tmp_path, z0=0.0)
        r = __import__("glb_validate").validate(path, self._model(elevation=3.0))
        assert not r["ok"]
        assert any("elevation" in e for e in r["errors"])

    def test_garbage_is_not_a_glb(self, tmp_path):
        p = tmp_path / "x.glb"
        p.write_bytes(b"not a glb at all")
        assert not __import__("glb_validate").validate(str(p))["ok"]
