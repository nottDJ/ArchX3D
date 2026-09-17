"""Validation outcomes and quality metrics: what a reconstruction is worth.

Four outcomes, and each has to mean something:

    VALID               sound geometry, nothing to flag
    VALID_WITH_WARNINGS sound geometry, something worth showing the user
    AMBIGUOUS           sound geometry, but the drawing leaves a structural
                        question open — built as drawn, flagged for review
    INVALID             not a building; refused before Blender

The metrics are the numbers those outcomes rest on. They are tested as
measurements of known drawings, and the refusals are tested with the shapes of
failure that motivated them: a house scaled to a shoebox, walls that never
join, a model that reached ``building.json`` without passing.
"""

from __future__ import annotations

import json
import os

import pytest

from modules.recon import validate as V
from modules.recon.ir import (AMBIGUOUS, INVALID, VALID, VALID_WITH_WARNINGS,
                              ReconstructionError)
from modules.recon.pipeline import reconstruct

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


def need(*parts):
    path = os.path.join(FIX, *parts)
    if not os.path.exists(path):
        pytest.skip("fixture missing")
    return path


class TestOutcomes:
    def test_a_clean_plan_is_valid(self):
        d = reconstruct(need("plans", "t01_simple_rect.dxf"))
        assert d.validation["status"] == VALID

    def test_a_lying_header_is_valid_with_warnings(self):
        d = reconstruct(need("plans", "t12_lying_header.dxf"))
        assert d.validation["status"] == VALID_WITH_WARNINGS
        assert any("INSUNITS" in w for w in d.validation["warnings"])

    def test_repeated_untitled_plates_are_ambiguous(self):
        d = reconstruct(need("multi", "m07_repeated_untitled.dxf"))
        assert d.validation["status"] == AMBIGUOUS
        assert d.validation["ok"], "ambiguity is not a geometry failure"
        assert d.validation["quality"]["confidence"] < 0.7

    def test_a_house_scaled_to_a_shoebox_is_invalid(self, tmp_path):
        """The founding failure: every exporter check passed on a 0.8 m house."""
        diag = str(tmp_path / "diag")
        with pytest.raises(ReconstructionError) as err:
            reconstruct(need("plans", "residential_us.dxf"), user_scale=0.001,
                        diagnostics_dir=diag)
        assert any("too small" in f for f in err.value.failures)
        with open(os.path.join(diag, "error.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        validation = payload["diagnostics"]["validation"]
        assert validation["status"] == INVALID
        assert validation["errors"]

    def test_non_strict_reports_invalid_rather_than_raising(self):
        d = reconstruct(need("plans", "residential_us.dxf"), user_scale=0.001,
                        strict=False)
        assert d.validation["status"] == INVALID
        assert not d.validation["ok"]


class TestMetrics:
    @pytest.fixture(scope="class")
    def q(self):
        return reconstruct(need("plans", "t01_simple_rect.dxf")).validation["quality"]

    def test_counts(self, q):
        assert (q["building_count"], q["level_count"]) == (1, 1)
        assert q["wall_count"] == 6
        assert q["room_count"] == 3
        assert q["opening_count"] == 7
        assert q["openings_by_class"] == {"door": 3, "window": 4}

    def test_areas_and_lengths(self, q):
        assert q["floor_area_m2"] == pytest.approx(66.0, rel=0.06)
        assert q["footprint_area_m2"] == pytest.approx(72.7, rel=0.05)
        assert q["wall_length_m"] == pytest.approx(47.0, rel=0.05)

    def test_thickness_distribution(self, q):
        dist = q["wall_thickness_distribution"]
        assert dist["p10_m"] == pytest.approx(0.1, abs=0.01)
        assert dist["p90_m"] == pytest.approx(0.15, abs=0.01)
        assert sum(dist["histogram_25mm"].values()) == q["wall_count"]

    def test_topology_health(self, q):
        assert q["connected_components"]["wall_groups_in_drawing"] == 1
        assert q["room_overlap"]["pairs"] == 0
        assert q["self_intersections"] == 0
        assert q["unbounded_faces"] == 0

    def test_confidence(self, q):
        assert q["unit_confidence"] > 0.9
        assert q["scale_confidence"] == 1.0
        assert set(q["scale_evidence"]) == {"median_wall_thickness_m", "median_door_width_m",
                                           "median_room_area_m2", "largest_extent_m"}

    def test_multi_building_counts(self):
        q = reconstruct(need("multi", "m06_three_storey_stacked.dxf")).validation["quality"]
        assert (q["building_count"], q["level_count"]) == (1, 3)
        assert q["room_count"] == 9


class TestScaleEvidence:
    def test_a_model_the_wrong_size_votes_against_itself(self):
        """A correct model shrunk 25x — the founding failure's proportions."""
        from modules.recon.ir import drawing_from_dict
        d = reconstruct(need("plans", "t01_simple_rect.dxf"))
        assert V.scale_evidence(d)["confidence"] == 1.0
        small = drawing_from_dict(json.loads(json.dumps(d.as_dict())))
        k = 1 / 25.0
        for level in small.levels():
            for w in level.walls:
                w.start = (w.start[0] * k, w.start[1] * k)
                w.end = (w.end[0] * k, w.end[1] * k)
                w.thickness *= k
            for r in level.rooms:
                r.area *= k * k
            for o in level.openings:
                o.width *= k
            level.bounds_max = (level.bounds_max[0] * k, level.bounds_max[1] * k)
        ev = V.scale_evidence(small)
        assert ev["confidence"] == 0.0
        assert all(not c["plausible"] for c in ev["checks"].values())


class TestJudgement:
    def _q(self, **over):
        q = {"wall_count": 120, "connected_components": {"wall_groups_in_drawing": 10},
             "scale_confidence": 1.0, "unit_confidence": 0.95,
             "scale_evidence": {"largest_extent_m": {"value": 20, "plausible": True}},
             "self_intersections": 0, "unbounded_faces": 0}
        q.update(over)
        return q

    def test_hundreds_of_disconnected_walls_fail(self):
        r = V.Report()
        V._judge_quality(self._q(wall_count=300,
                                 connected_components={"wall_groups_in_drawing": 240}),
                         r, None)
        assert not r.ok
        assert any("disconnected groups" in e for e in r.errors)

    def test_a_partly_fragmented_drawing_warns(self):
        r = V.Report()
        V._judge_quality(self._q(wall_count=100,
                                 connected_components={"wall_groups_in_drawing": 35}),
                         r, None)
        assert r.ok and r.warnings

    def test_implausible_proportions_with_an_uncertain_unit_fail(self):
        r = V.Report()
        V._judge_quality(self._q(scale_confidence=0.0, unit_confidence=0.3, scale_evidence={
            "median_wall_thickness_m": {"value": 0.002, "plausible": False},
            "largest_extent_m": {"value": 0.8, "plausible": False}}), r, None)
        assert not r.ok

    def test_implausible_proportions_with_a_confident_unit_only_warn(self):
        r = V.Report()
        V._judge_quality(self._q(scale_confidence=0.0, unit_confidence=0.95, scale_evidence={
            "median_room_area_m2": {"value": 400, "plausible": False}}), r, None)
        assert r.ok and r.warnings

    def test_a_drawing_of_loose_lines_is_refused(self, tmp_path):
        """Forty wall-layer stubs that never meet are not a building."""
        import ezdxf
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 6
        msp = doc.modelspace()
        doc.layers.add("A-WALL")
        for i in range(40):
            x, y = (i % 8) * 5.0, (i // 8) * 5.0
            msp.add_line((x, y), (x + 2.0, y), dxfattribs={"layer": "A-WALL"})
            msp.add_line((x, y + 0.2), (x + 2.0, y + 0.2), dxfattribs={"layer": "A-WALL"})
        path = str(tmp_path / "loose.dxf")
        doc.saveas(path)
        with pytest.raises(ReconstructionError) as err:
            reconstruct(path)
        assert err.value.failures


class TestTheBlenderGate:
    def test_only_passing_models_are_buildable(self):
        from modules.blender_build import check_buildable
        good = reconstruct(need("plans", "t01_simple_rect.dxf")).as_dict()
        assert check_buildable(good)[0]
        ambiguous = reconstruct(need("multi", "m07_repeated_untitled.dxf")).as_dict()
        assert check_buildable(ambiguous) == (True, AMBIGUOUS)
        bad = reconstruct(need("plans", "t01_simple_rect.dxf"), user_scale=0.01,
                          strict=False).as_dict()
        ok, reason = check_buildable(bad)
        assert not ok and "validation failed" in reason
        assert not check_buildable(None)[0]
        assert not check_buildable({"buildings": []})[0]
        forged = dict(good, validation={"ok": True, "status": INVALID})
        assert not check_buildable(forged)[0]
