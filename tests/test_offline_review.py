"""A drawing on its own is a supported mode, not an error (P3).

Offline analysis used to put ``error: no reference images supplied`` and
"N room(s) ... will be built empty" in the review panel of every user who
deliberately ran without photographs — while the same panel counted the
furniture those rooms would get from their drawn types.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "modules"))

from vision import pipeline, review  # noqa: E402

PLAN = os.path.join(ROOT, "tests", "fixtures", "plans", "t03_multi_room.dxf")


@pytest.fixture(scope="module")
def offline_review(tmp_path_factory):
    from modules.recon import compat
    from modules.recon.pipeline import reconstruct

    out = tmp_path_factory.mktemp("offline")
    geometry_path = out / "geometry.json"
    compat.write_geometry_json(reconstruct(PLAN), str(geometry_path))
    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    result = pipeline.analyse([], geometry, pipeline.PipelineConfig(), log=lambda *a: None)
    return result, review.build_review(result.graph)


def test_no_images_is_not_an_error(offline_review):
    result, payload = offline_review
    assert result.errors == []
    assert not any(w.lower().startswith("error") for w in payload["warnings"]), payload["warnings"]


def test_furnished_rooms_are_not_called_empty(offline_review):
    _result, payload = offline_review
    assert payload["totals"]["objects"] > 0, "fixture should be furnished from room types"
    assert not any("built empty" in w for w in payload["warnings"]), payload["warnings"]


def test_the_review_states_how_it_ran(offline_review):
    _result, payload = offline_review
    assert payload["analysis"] == {
        "engine": "deterministic CPU reconstruction",
        "ai": "disabled",
        "network": "not required",
        "reference_images": 0,
    }


def test_a_genuinely_empty_room_is_still_reported():
    from vision.schema import Room, SceneGraph

    graph = SceneGraph(rooms=[Room(id="r1"), Room(id="r2")])
    warnings = review._warnings(graph, {})
    assert any("r1" in w and "built empty" in w for w in warnings)
