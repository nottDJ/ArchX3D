"""The diagnostics bundle: every stage visible, on success and on failure.

The point of these is narrow but important. The failure that motivated the
rewrite was invisible in the final artefact — a valid GLB of the wrong
building — so the bundle has to exist, has to be written even when the
reconstruction is refused, and must never itself be the reason a build fails.
"""

from __future__ import annotations

import json
import os

import pytest

from modules.recon import diagnostics as D
from modules.recon.ir import ReconstructionError
from modules.recon.pipeline import reconstruct

PLAN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fixtures", "plans", "t01_simple_rect.dxf")

EXPECTED = ("entities.json", "units.json", "walls.json", "rooms.json",
            "doors.json", "windows.json", "validation.json", "building.json",
            "debug_raw.svg", "debug_normalized.svg", "debug_walls.svg",
            "debug_rooms.svg", "debug_openings.svg", "debug_topology.svg",
            "reconstruction.svg")


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    if not os.path.exists(PLAN):
        pytest.skip("fixture not generated")
    out = str(tmp_path_factory.mktemp("diag"))
    reconstruct(PLAN, diagnostics_dir=out)
    return out


class TestBundle:
    @pytest.mark.parametrize("name", EXPECTED)
    def test_every_stage_is_written(self, bundle, name):
        path = os.path.join(bundle, name)
        assert os.path.exists(path), "%s is missing from the bundle" % name
        assert os.path.getsize(path) > 0

    def test_the_json_is_readable(self, bundle):
        for name in EXPECTED:
            if not name.endswith(".json"):
                continue
            with open(os.path.join(bundle, name), encoding="utf-8") as fh:
                json.load(fh)

    def test_the_svgs_are_svg(self, bundle):
        for name in EXPECTED:
            if not name.endswith(".svg"):
                continue
            with open(os.path.join(bundle, name), encoding="utf-8") as fh:
                head = fh.read(200)
            assert head.lstrip().startswith("<svg"), name

    def test_the_unit_decision_records_every_candidate(self, bundle):
        with open(os.path.join(bundle, "units.json"), encoding="utf-8") as fh:
            units = json.load(fh)
        assert units["candidates"], "a wrong answer must be explainable"
        assert all({"unit", "score", "reasons"} <= set(c)
                   for c in units["candidates"])

    def test_walls_carry_their_source_entities(self, bundle):
        with open(os.path.join(bundle, "walls.json"), encoding="utf-8") as fh:
            walls = json.load(fh)["walls"]
        assert walls
        assert any(w["source_ids"] for w in walls), \
            "generated geometry must be traceable back to the drawing"


class TestFailurePath:
    def test_the_bundle_is_written_when_the_build_is_refused(self, tmp_path):
        """Refusal is exactly when a user most needs to see the stages."""
        out = str(tmp_path / "diag")
        with pytest.raises(ReconstructionError):
            # A scale override two orders of magnitude out makes the building
            # implausible and validation refuses it.
            reconstruct(PLAN, user_scale=0.01, diagnostics_dir=out)
        assert os.path.exists(os.path.join(out, "error.json"))
        assert os.path.exists(os.path.join(out, "debug_raw.svg"))
        with open(os.path.join(out, "error.json"), encoding="utf-8") as fh:
            err = json.load(fh)
        assert err["stage"] == "validate"
        assert err["failures"]

    def test_a_broken_diagnostic_never_breaks_a_build(self, tmp_path):
        """A drawing tool must not be able to fail the thing it documents."""
        written = D.write_bundle(str(tmp_path / "d"), drawing=None,
                                 wall_result=None, building=None)
        assert isinstance(written, dict)
