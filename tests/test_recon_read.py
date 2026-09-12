"""DXF reading: flattening, classification, transforms and normalisation."""

from __future__ import annotations

import math
import os

import ezdxf
import pytest

from modules.recon import classify as C
from modules.recon import read as R
from modules.recon.ir import ReconstructionError


@pytest.fixture
def tmp_dxf(tmp_path):
    def make(build, *, insunits=6):
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = insunits
        build(doc, doc.modelspace())
        path = str(tmp_path / "t.dxf")
        doc.saveas(path)
        return path
    return make


def walls_box(msp, x0=0.0, y0=0.0, w=12.0, h=8.0, t=0.2, layer="A-WALL"):
    for off in (0.0, t):
        pts = [(x0 + off, y0 + off), (x0 + w - off, y0 + off),
               (x0 + w - off, y0 + h - off), (x0 + off, y0 + h - off)]
        for i in range(4):
            msp.add_line(pts[i], pts[(i + 1) % 4], dxfattribs={"layer": layer})


class TestClassification:
    def test_a_dimension_on_a_wall_layer_is_still_a_dimension(self, tmp_dxf):
        def build(doc, msp):
            walls_box(msp)
            doc.layers.add("A-WALL") if "A-WALL" not in doc.layers else None
            dim = msp.add_linear_dim(base=(0, -2), p1=(0, 0), p2=(12, 0),
                                     dxfattribs={"layer": "A-WALL"})
            dim.render()
        d = R.read(tmp_dxf(build))
        assert not any(p.role == C.WALL and p.dxftype == "DIMENSION"
                       for p in d.prims)

    def test_text_is_never_geometry(self, tmp_dxf):
        def build(doc, msp):
            walls_box(msp)
            msp.add_text("KITCHEN", height=0.25,
                         dxfattribs={"layer": "A-WALL"}).set_placement((6, 4))
        d = R.read(tmp_dxf(build))
        assert any(t.text == "KITCHEN" for t in d.labels)
        assert all(p.role != C.ROOM_LABEL or p.dxftype not in ("TEXT", "MTEXT")
                   for p in d.prims)

    def test_an_xref_prefixed_layer_is_still_recognised(self, tmp_dxf):
        def build(doc, msp):
            walls_box(msp, layer="xref-Bishop-Overland-08$0$A-WALL")
        d = R.read(tmp_dxf(build))
        assert any(p.role == C.WALL for p in d.prims)

    def test_a_frozen_layer_is_not_part_of_the_drawing(self, tmp_dxf):
        def build(doc, msp):
            walls_box(msp)
            layer = doc.layers.add("A-WALL-OLD")
            layer.freeze()
            walls_box(msp, x0=40.0, layer="A-WALL-OLD")
        d = R.read(tmp_dxf(build))
        assert not any(p.layer == "A-WALL-OLD" and p.role == C.WALL
                       for p in d.prims)


class TestFlattening:
    def test_geometry_inside_a_nested_block_is_found(self, tmp_dxf):
        def build(doc, msp):
            inner = doc.blocks.new("INNER")
            for off in (0.0, 0.2):
                pts = [(off, off), (12 - off, off), (12 - off, 8 - off), (off, 8 - off)]
                for i in range(4):
                    inner.add_line(pts[i], pts[(i + 1) % 4],
                                   dxfattribs={"layer": "A-WALL"})
            outer = doc.blocks.new("OUTER")
            outer.add_blockref("INNER", (0, 0))
            msp.add_blockref("OUTER", (0, 0), dxfattribs={"layer": "A-WALL"})
        d = R.read(tmp_dxf(build))
        assert len([p for p in d.prims if p.role == C.WALL]) >= 8

    def test_a_block_transform_is_applied(self, tmp_dxf):
        def build(doc, msp):
            blk = doc.blocks.new("UNIT")
            blk.add_line((0, 0), (1, 0), dxfattribs={"layer": "A-WALL"})
            msp.add_blockref("UNIT", (5, 5), dxfattribs={
                "layer": "A-WALL", "xscale": 2.0, "yscale": 2.0, "rotation": 90.0})
        d = R.read(tmp_dxf(build))
        pts = [p for prim in d.prims for p in prim.points]
        assert pts, "the block's geometry must survive"
        span = max(p[1] for p in pts) - min(p[1] for p in pts)
        assert span == pytest.approx(2.0, abs=0.01), \
            "rotated 90 degrees and scaled 2x: a 2 m run up the y axis"

    def test_an_arc_is_kept_as_an_arc_and_as_a_polyline(self, tmp_dxf):
        def build(doc, msp):
            walls_box(msp)
            msp.add_arc(center=(3, 0), radius=0.9, start_angle=0, end_angle=90,
                        dxfattribs={"layer": "A-DOOR"})
        d = R.read(tmp_dxf(build))
        assert any(abs(a.radius - 0.9) < 1e-6 for a in d.arcs)
        assert any(p.dxftype == "ARC" for p in d.prims)

    def test_a_bulged_polyline_keeps_its_curve(self, tmp_dxf):
        def build(doc, msp):
            walls_box(msp)
            msp.add_lwpolyline([(20, 0, 0, 0, 1.0), (22, 0, 0, 0, 0)],
                               format="xyseb", dxfattribs={"layer": "A-WALL"})
        d = R.read(tmp_dxf(build))
        curved = [p for p in d.prims if p.dxftype == "LWPOLYLINE"
                  and len(p.points) > 2]
        assert curved, "a bulge must not be flattened to its chord"


class TestNormalisation:
    def test_the_origin_is_the_building_not_the_sheet(self, tmp_dxf):
        def build(doc, msp):
            walls_box(msp, x0=1000.0, y0=500.0)
            msp.add_text("TITLE BLOCK", height=1.0,
                         dxfattribs={"layer": "A-ANNO-TTLB"}
                         ).set_placement((1400, 300))
        d = R.read(tmp_dxf(build))
        assert d.bounds_min == pytest.approx((0.0, 0.0))
        assert d.width == pytest.approx(12.0, abs=0.05)
        assert d.origin_offset[0] == pytest.approx(1000.0, abs=0.5)

    def test_large_world_coordinates_do_not_reach_the_output(self, tmp_dxf):
        def build(doc, msp):
            walls_box(msp, x0=523000.0, y0=4120000.0)
        d = R.read(tmp_dxf(build))
        assert max(abs(x) for p in d.prims for x, _ in p.points) < 1000.0

    def test_one_stray_block_does_not_stretch_the_plan(self, tmp_dxf):
        """A real fixture carries a door block inserted 23 km away."""
        def build(doc, msp):
            walls_box(msp)
            msp.add_line((-23000, 5), (-22999, 5), dxfattribs={"layer": "A-DOOR"})
        d = R.read(tmp_dxf(build))
        assert d.width == pytest.approx(12.0, abs=0.5)


class TestRefusal:
    def test_a_missing_file_is_reported_clearly(self):
        with pytest.raises(ReconstructionError) as excinfo:
            R.read("does-not-exist.dxf")
        assert excinfo.value.stage == "read"

    def test_an_empty_drawing_is_refused(self, tmp_dxf):
        def build(doc, msp):
            msp.add_text("NOTHING HERE", height=1.0).set_placement((0, 0))
        with pytest.raises(ReconstructionError):
            R.read(tmp_dxf(build))


class TestRobustBounds:
    def test_outliers_are_trimmed_but_projections_are_kept(self):
        prims = [
            R.Prim(id="p%d" % i, points=[(x, 0.0), (x, 8.0)], closed=False,
                   role=C.WALL, confidence=1.0, reason="", source="layer",
                   dxftype="LINE", layer="A-WALL")
            for i, x in enumerate(range(0, 13))
        ]
        prims.append(R.Prim(id="porch", points=[(13.0, 0.0), (14.5, 0.0)],
                            closed=False, role=C.WALL, confidence=1.0,
                            reason="", source="layer", dxftype="LINE",
                            layer="A-WALL"))
        prims.append(R.Prim(id="stray", points=[(9000.0, 0.0), (9001.0, 0.0)],
                            closed=False, role=C.WALL, confidence=1.0,
                            reason="", source="layer", dxftype="LINE",
                            layer="A-WALL"))
        x0, y0, x1, y1 = R.robust_bounds(prims)
        assert x1 < 100.0, "the stray must not define the frame"
        assert x1 >= 14.0, "the porch is part of the building"
