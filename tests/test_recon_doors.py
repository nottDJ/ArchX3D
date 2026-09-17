"""Door recognition: precision first, measured against ground truth.

The rule the whole module is held to: **a false door is worse than an unknown
opening**. So every door here is scored against the truth the fixture
generators wrote, and the invariant behind the ``door`` tier — two independent
statements, or one statement and a wall that is genuinely open — is checked on
every opening the engine reports, not only on the ones a test expects.

Fixtures that exist to trap a recogniser (``tests/fixtures/make_doors.py``):
jambs a short pier apart, door symbols in mirrored anonymous blocks on layer
``0``, chair backs and a symbol in the middle of a room, door tags on the door
layer, doorways drawn as nothing but a break in the wall.
"""

from __future__ import annotations

import math
import os

import pytest

from modules.recon import openings as O
from modules.recon import read as RD
from modules.recon.metrics import load_truth, opening_metrics
from modules.recon.pipeline import reconstruct

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
DOORS = os.path.join(FIX, "doors")
SBA = os.path.join(os.path.dirname(HERE), "uploads", "20260617_102040_sba.dxf")


def fixture(*parts):
    path = os.path.join(FIX, *parts)
    if not os.path.exists(path):
        pytest.skip("fixture missing: %s" % os.path.join(*parts))
    return path


def door_tier_holds(o) -> bool:
    """The definition of the ``door`` tier, restated independently."""
    families = {O._DOOR_CUES[s] for s in o.evidence.split("+") if s in O._DOOR_CUES}
    return len(families) >= 2 or (len(families) >= 1 and o.gap_confirmed is True)


def _truthful_fixtures():
    out = []
    for sub in ("plans", "multi", "doors"):
        folder = os.path.join(FIX, sub)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".dxf") and os.path.exists(
                    os.path.join(folder, name[:-4] + ".truth.json")):
                out.append(os.path.join(sub, name))
    return out


@pytest.fixture(scope="module")
def scored():
    rows = {}
    for rel in _truthful_fixtures():
        path = os.path.join(FIX, rel)
        d = reconstruct(path, strict=False)
        rows[rel] = (d, opening_metrics(d, load_truth(path)))
    return rows


class TestAgainstTruth:
    def test_no_false_door_anywhere(self, scored):
        """Across every generated plan, not one reported door is wrong."""
        assert scored, "no truth fixtures found"
        fp = {rel: m["door"]["false_positive"] for rel, (_d, m) in scored.items()
              if m["door"]["false_positive"]}
        assert not fp, fp

    def test_door_recall(self, scored):
        truth = sum(m["door"]["truth"] for _d, m in scored.values())
        found = sum(m["door"]["true_positive"] for _d, m in scored.values())
        assert truth >= 100
        assert found / truth >= 0.98, "%d of %d doors" % (found, truth)

    def test_windows_are_unaffected(self, scored):
        for rel, (_d, m) in scored.items():
            assert m["window"]["false_positive"] == 0, rel
            assert m["window"]["missed"] == 0, rel

    def test_door_positions_are_accurate(self, scored):
        errors = [m["door"]["mean_position_error_m"] for _d, m in scored.values()
                  if m["door"]["mean_position_error_m"] is not None]
        assert errors and max(errors) < 0.1

    def test_the_door_tier_is_always_earned(self, scored):
        for rel, (d, _m) in scored.items():
            for o in d.openings:
                if o.classification == "door":
                    assert door_tier_holds(o), (rel, o.id, o.evidence, o.gap_confirmed)


class TestTraps:
    def test_jambs_a_pier_apart_are_two_doors_not_one_in_the_pier(self):
        d = reconstruct(fixture("doors", "d01_jamb_doors.dxf"))
        m = opening_metrics(d, load_truth(fixture("doors", "d01_jamb_doors.dxf")))
        assert m["door"]["true_positive"] == 5 and m["door"]["false_positive"] == 0
        ox, oy = d.origin_offset
        # The 0.6 m pier between the two interior doors is centred at x = 3.2.
        pier = [o for o in d.openings
                if abs(o.position[0] + ox - 3.2) < 0.2 and abs(o.position[1] + oy - 4.0) < 0.2]
        assert not pier
        assert all(o.evidence == "jambs" for o in d.openings if o.classification == "door")

    def test_door_symbols_in_mirrored_anonymous_blocks(self):
        path = fixture("doors", "d02_symbol_blocks.dxf")
        d = reconstruct(path)
        m = opening_metrics(d, load_truth(path))
        assert m["door"]["true_positive"] == 5
        assert m["door"]["false_positive"] == 0
        assert m["spurious_openings"] == 0, "chairs and a stray symbol are not openings"
        double = [o for o in d.openings if o.swing == "double"]
        assert len(double) == 1 and double[0].width == pytest.approx(1.6, abs=0.05)

    def test_door_layer_clutter_is_not_doors(self):
        path = fixture("doors", "d03_door_layer_clutter.dxf")
        d = reconstruct(path)
        m = opening_metrics(d, load_truth(path))
        assert m["door"]["false_positive"] == 0
        assert m["spurious_openings"] == 0

    def test_a_bare_gap_is_an_unknown_opening_never_a_door(self):
        path = fixture("doors", "d04_gaps_only.dxf")
        d = reconstruct(path)
        assert {o.classification for o in d.openings} == {"unknown_opening"}
        m = opening_metrics(d, load_truth(path))
        assert m["door"]["reported"] == 0
        assert len(d.openings) == 2
        assert m["spurious_openings"] == 0

    def test_a_swing_cuts_the_wall_it_closes_not_the_one_its_leaf_lies_along(self):
        path = fixture("plans", "t02_l_shape.dxf")
        d = reconstruct(path)
        m = opening_metrics(d, load_truth(path))
        assert m["door"]["true_positive"] == m["door"]["truth"] == 4
        assert m["spurious_openings"] == 0


class TestRealDrawings:
    def test_jamb_doors_on_the_planning_sheet(self):
        """sba draws every door as two jamb marks; the old engine found none."""
        if not os.path.exists(SBA):
            pytest.skip("sba.dxf not present")
        d = reconstruct(SBA)
        by = d.summary()["openings_by_class"]
        assert by.get("door", 0) >= 80
        assert by.get("probable_door", 0) <= 20
        for o in d.openings:
            if o.classification == "door":
                assert door_tier_holds(o), o.id

    def test_no_phantom_door_beside_a_door_symbol(self):
        """Tick marks inside a door block once paired into a door on the next wall."""
        d = reconstruct(fixture("real", "final_plan_19th_may.dxf"))
        phantoms = [(6.39, 13.07), (2.81, 10.17), (11.85, 10.58), (11.85, 8.06),
                    (15.27, 8.36), (28.89, 10.66), (28.89, 8.09), (23.03, 10.66)]
        for o in d.openings:
            for p in phantoms:
                assert math.dist(o.position, p) > 0.3, (o.id, o.classification, p)
        by = d.summary()["openings_by_class"]
        assert by.get("door", 0) >= 11
        assert by.get("door", 0) + by.get("probable_door", 0) <= 14


class TestReaderOcs:
    def test_a_mirrored_block_places_its_swing_where_it_is_drawn(self, tmp_path):
        """Extrusion (0, 0, -1) negates x; the hinge must still be at the hinge."""
        import ezdxf
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 6
        blk = doc.blocks.new("DOORSYM")
        blk.add_arc((0, 0), 0.9, 0, 90)
        blk.add_line((0, 0), (0, 0.9))
        msp = doc.modelspace()
        msp.add_blockref("DOORSYM", (10.0, 5.0), dxfattribs={"xscale": -1.0})
        for a, b in (((0, 0), (20, 0)), ((20, 0), (20, 12)), ((20, 12), (0, 12)),
                     ((0, 12), (0, 0))):
            msp.add_line(a, b, dxfattribs={"layer": "A-WALL"})
        path = str(tmp_path / "mirror.dxf")
        doc.saveas(path)
        cad = RD.read(path)
        ox, oy = cad.origin_offset
        arcs = [a for a in cad.arcs if a.block == "DOORSYM"]
        assert len(arcs) == 1
        a = arcs[0]
        assert (a.centre[0] + ox, a.centre[1] + oy) == pytest.approx((10.0, 5.0), abs=1e-6)
        # Mirrored in x, the quarter runs from straight up round to the left.
        ends = sorted([(round(a.start_point[0] + ox, 3), round(a.start_point[1] + oy, 3)),
                       (round(a.end_point[0] + ox, 3), round(a.end_point[1] + oy, 3))])
        assert ends == [(9.1, 5.0), (10.0, 5.9)]


def test_only_confirmed_doors_get_a_leaf():
    from modules.blender_build import opening_fill
    assert opening_fill({"kind": "door", "classification": "door"}) == "leaf"
    assert opening_fill({"kind": "garage", "classification": "garage_door"}) == "leaf"
    assert opening_fill({"kind": "door", "classification": "probable_door"}) is None
    assert opening_fill({"kind": "cased", "classification": "unknown_opening"}) is None
    assert opening_fill({"kind": "window", "classification": "window"}) == "glass"
    assert opening_fill({"kind": "door"}) == "leaf", "a model without tiers still builds"
