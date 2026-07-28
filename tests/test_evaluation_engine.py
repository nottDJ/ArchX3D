"""
Integration tests for the evaluation engine.

A complete build — reference photographs, renders, every auxiliary pass, a
manifest tying them together — is constructed on disk by the
``evaluation_project`` fixture, and the engine is run over it exactly as it
would run in production. No Blender: Phase 2's output is a set of PNGs and a
JSON file, and those can be written directly.

Three properties carry the phase and are pinned here:

* the engine **never modifies the scene graph**;
* it is **deterministic** — the same inputs give the same documents;
* an axis it could not measure is **excluded, not zeroed**.
"""

from __future__ import annotations

import json
import os

import pytest

from evaluation import EvaluationConfig, evaluate
from evaluation.engine import DOCUMENTS, Evaluator
from evaluation.schema import AXES, OBJECTS, Subsystem


@pytest.fixture
def config(evaluation_project):
    return EvaluationConfig(
        base_dir=evaluation_project["base_dir"],
        manifest_path=evaluation_project["manifest_path"],
        graph_path=os.path.join(evaluation_project["base_dir"], "graph.json"),
        image_dirs=("reference_images",),
        output_dir=evaluation_project["output_dir"],
        verbose=False,
    )


def run(project, config, **kwargs):
    return evaluate(graph=project["graph"], manifest=project["manifest"],
                    config=config, **kwargs)


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


def test_every_rendered_viewpoint_is_evaluated(evaluation_project, config):
    result = run(evaluation_project, config, write=False)

    assert len(result.viewpoints) == 3
    assert {v.viewpoint_id for v in result.viewpoints} == {"img_a1", "img_a2", "img_b1"}


def test_every_axis_is_measured_when_everything_is_available(evaluation_project, config):
    result = run(evaluation_project, config, write=False)
    viewpoint = result.viewpoint("img_a1")

    for axis in AXES:
        assert viewpoint.axes[axis].measured, f"{axis}: {viewpoint.axes[axis].reason}"
    assert viewpoint.totals.complete


def test_the_reference_photograph_is_found_by_name(evaluation_project, config):
    result = run(evaluation_project, config, write=False)
    viewpoint = result.viewpoint("img_a1")

    assert viewpoint.reference.endswith("img_a1.jpg")
    assert os.path.exists(viewpoint.reference)


def test_every_pass_is_loaded_and_recorded(evaluation_project, config):
    result = run(evaluation_project, config, write=False)
    assert set(result.viewpoint("img_a1").passes_used) == set(evaluation_project["passes"])


def test_rooms_aggregate_their_viewpoints(evaluation_project, config):
    result = run(evaluation_project, config, write=False)
    room = result.room("room_a")

    assert room.viewpoint_ids == ["img_a1", "img_a2"]
    assert room.room_type == "living_room"
    assert 0.0 <= room.totals.score <= 1.0


def test_the_building_scores_and_names_a_subsystem(evaluation_project, config):
    result = run(evaluation_project, config, write=False)

    assert 0.0 <= result.score <= 1.0
    assert result.building.subsystem_pressure
    assert all(name in Subsystem.ALL for name in result.building.subsystem_pressure)


def test_the_render_being_darker_is_actually_found(evaluation_project, config):
    """The fixture's render is darker and flatter than its reference by
    construction, so the engine had better say so."""
    result = run(evaluation_project, config, write=False)
    summaries = [f.summary for f in result.findings]
    assert any("darker" in s for s in summaries), summaries


def test_every_finding_carries_its_explanation(evaluation_project, config):
    result = run(evaluation_project, config, write=False)

    assert result.findings
    for finding in result.findings:
        assert finding.summary and finding.why
        assert finding.subsystem in Subsystem.ALL
        assert 0.0 <= finding.severity <= 1.0
        assert 0.0 <= finding.confidence <= 1.0
        assert finding.axis in AXES


def test_findings_are_ranked_most_severe_first(evaluation_project, config):
    result = run(evaluation_project, config, write=False)
    severities = [f.severity for f in result.findings]
    assert severities == sorted(severities, reverse=True)


# ---------------------------------------------------------------------------
# The engine only measures
# ---------------------------------------------------------------------------


def test_the_scene_graph_is_not_modified(evaluation_project, config):
    """The single hardest constraint in the brief, and the easiest to break."""
    before = json.dumps(evaluation_project["graph"].to_dict(), sort_keys=True)
    run(evaluation_project, config, write=False)
    after = json.dumps(evaluation_project["graph"].to_dict(), sort_keys=True)
    assert before == after


def test_two_runs_over_the_same_inputs_agree(evaluation_project, config):
    """Without this an evaluation cannot be a regression baseline."""
    first = run(evaluation_project, config, write=False).to_dict()
    second = run(evaluation_project, config, write=False).to_dict()

    for document in (first, second):
        document.pop("generated_at")
        document["metadata"].pop("duration_ms")
    assert first == second


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_a_missing_reference_leaves_the_object_axis_working(evaluation_project, config):
    os.unlink(os.path.join(evaluation_project["references"], "img_b1.jpg"))
    result = run(evaluation_project, config, write=False)
    viewpoint = result.viewpoint("img_b1")

    assert not viewpoint.axes["colour"].measured
    assert viewpoint.axes[OBJECTS].measured
    assert viewpoint.totals.weight_used < 1.0
    assert any("not found" in note for note in viewpoint.notes)


def test_a_missing_reference_does_not_score_the_viewpoint_zero(evaluation_project, config):
    complete = run(evaluation_project, config, write=False).viewpoint("img_b1")
    os.unlink(os.path.join(evaluation_project["references"], "img_b1.jpg"))
    partial = run(evaluation_project, config, write=False).viewpoint("img_b1")

    assert partial.totals.score > 0.0
    assert partial.totals.confidence < complete.totals.confidence


def test_missing_passes_weaken_the_axes_rather_than_breaking_them(evaluation_project, config):
    for record in evaluation_project["manifest"].records:
        record.passes = {}
    result = run(evaluation_project, config, write=False)
    viewpoint = result.viewpoint("img_a1")

    assert viewpoint.axes["colour"].measured
    assert viewpoint.axes["material"].measured
    assert viewpoint.axes["material"].detail["source"] == "beauty render"
    # The material axis is making a larger inference without albedo, and the
    # confidence has to admit it.
    assert viewpoint.axes["material"].confidence < 0.7
    assert viewpoint.passes_used == []


def test_a_room_with_nothing_measurable_is_excluded_from_the_building_score(
        evaluation_project, config):
    from vision.schema import Room

    graph = evaluation_project["graph"]
    graph.rooms.append(Room(id="room_empty", area=50.0))

    result = run(evaluation_project, config, write=False)
    assert "room_empty" not in result.building.room_scores
    assert any("excluded from the building score" in note for note in result.notes)
    # A large empty room must not be able to halve a good building's score.
    assert result.score > 0.2


def test_a_failed_render_is_skipped(evaluation_project, config):
    from render.manifest import STATUS_FAILED

    evaluation_project["manifest"].record_for("img_b1").status = STATUS_FAILED
    result = run(evaluation_project, config, write=False)
    assert result.viewpoint("img_b1") is None
    assert len(result.viewpoints) == 2


def test_an_empty_manifest_evaluates_to_nothing_rather_than_crashing(
        evaluation_project, config):
    evaluation_project["manifest"].records = []
    result = run(evaluation_project, config, write=False)

    assert result.viewpoints == []
    assert any("no successful renders" in note for note in result.notes)
    assert result.summary() == "nothing evaluated"


# ---------------------------------------------------------------------------
# The four documents
# ---------------------------------------------------------------------------


def test_all_four_documents_are_written(evaluation_project, config):
    run(evaluation_project, config)

    for name in DOCUMENTS:
        path = os.path.join(config.output_dir, name)
        assert os.path.exists(path), name
        with open(path, encoding="utf-8") as handle:
            assert json.load(handle)


def test_each_document_carries_its_own_slice(evaluation_project, config):
    """Four files because they have four readers; each must stand alone."""
    run(evaluation_project, config)

    def load(name):
        with open(os.path.join(config.output_dir, name), encoding="utf-8") as handle:
            return json.load(handle)

    assert set(load("building_summary.json")["building"]) >= {
        "totals", "axes", "room_scores", "findings", "coverage"}
    assert len(load("per_room.json")["rooms"]) >= 2
    assert len(load("per_viewpoint.json")["viewpoints"]) == 3
    assert set(load("evaluation.json")) >= {
        "building", "rooms", "viewpoints", "findings", "metadata"}


def test_the_viewpoint_document_carries_what_a_refinement_pass_needs(
        evaluation_project, config):
    run(evaluation_project, config)
    with open(os.path.join(config.output_dir, "per_viewpoint.json"),
              encoding="utf-8") as handle:
        payload = json.load(handle)

    viewpoint = payload["viewpoints"][0]
    assert {"viewpoint_id", "room", "reference", "render", "axes", "findings",
            "totals", "passes_used"} <= set(viewpoint)
    finding = next(f for v in payload["viewpoints"] for f in v["findings"])
    assert {"axis", "summary", "why", "evidence", "subsystem", "remedy",
            "severity", "objects", "materials", "room"} <= set(finding)


def test_an_unmeasured_axis_says_why_in_the_document(evaluation_project, config):
    os.unlink(os.path.join(evaluation_project["references"], "img_b1.jpg"))
    run(evaluation_project, config)

    with open(os.path.join(config.output_dir, "per_viewpoint.json"),
              encoding="utf-8") as handle:
        payload = json.load(handle)
    viewpoint = next(v for v in payload["viewpoints"] if v["viewpoint_id"] == "img_b1")

    colour = viewpoint["axes"]["colour"]
    assert colour["measured"] is False
    assert colour["reason"]
    assert "score" not in colour        # an unmeasured axis has none


def test_the_documents_are_stable_across_runs(evaluation_project, config):
    run(evaluation_project, config)
    with open(os.path.join(config.output_dir, "per_room.json"), encoding="utf-8") as h:
        first = h.read()

    run(evaluation_project, config)
    with open(os.path.join(config.output_dir, "per_room.json"), encoding="utf-8") as h:
        second = h.read()

    # Only the timestamp may differ.
    assert json.loads(first)["rooms"] == json.loads(second)["rooms"]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def test_the_html_report_and_overlays_are_written(evaluation_project, config):
    run(evaluation_project, config)

    report = os.path.join(config.output_dir, "report.html")
    assert os.path.exists(report)
    with open(report, encoding="utf-8") as handle:
        html = handle.read()

    assert "<!doctype html>" in html
    assert "reference" in html and "generated" in html and "difference" in html
    assert "img_a1" in html
    # Self-contained: nothing to fetch over a network.
    assert "http://" not in html and "https://" not in html
    assert os.path.exists(os.path.join(config.output_dir, "overlays", "img_a1.png"))


def test_the_report_shows_axis_scores_and_findings(evaluation_project, config):
    result = run(evaluation_project, config)
    with open(os.path.join(config.output_dir, "report.html"), encoding="utf-8") as handle:
        html = handle.read()

    import html as html_module

    for axis in AXES:
        assert axis in html
    # Escaped, because the report escapes everything it prints — see
    # test_report_html_escapes_its_inputs.
    assert html_module.escape(result.findings[0].summary) in html


def test_the_report_can_be_switched_off(evaluation_project, config):
    config.write_html = False
    run(evaluation_project, config)
    assert not os.path.exists(os.path.join(config.output_dir, "report.html"))


def test_report_html_escapes_its_inputs(evaluation_project, config):
    """Findings quote material names from the .blend; those are not trusted."""
    from evaluation import report as report_mod

    result = run(evaluation_project, config, write=False)
    result.notes.append("<script>alert('x')</script>")
    html = report_mod.render_html(result, config, {})

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_resolves_paths_against_the_project_root(tmp_path):
    built = EvaluationConfig.from_config({}, base_dir=str(tmp_path))

    assert built.manifest_path.endswith(os.path.join("preview", "manifest.json"))
    assert built.output_dir.endswith(os.path.join("output", "evaluation"))
    assert "reference_images" in built.image_dirs


def test_config_takes_the_vision_images_directory(tmp_path):
    built = EvaluationConfig.from_config(
        {"vision": {"images_dir": "shots"}}, base_dir=str(tmp_path)
    )
    assert built.image_dirs[0] == "shots"


def test_weights_can_be_overridden_and_bad_ones_ignored(tmp_path):
    built = EvaluationConfig.from_config(
        {"evaluation": {"weights": {"objects": 0.5, "nonsense": 9, "colour": "x"}}},
        base_dir=str(tmp_path),
    )
    assert built.weights["objects"] == 0.5
    assert "nonsense" not in built.weights
    assert built.weights["colour"] == 0.20        # the bad value was ignored


def test_a_reference_is_found_despite_a_different_extension(tmp_path):
    (tmp_path / "reference_images").mkdir()
    (tmp_path / "reference_images" / "img0.png").write_bytes(b"x")

    built = EvaluationConfig(base_dir=str(tmp_path), image_dirs=("reference_images",))
    assert built.resolve_image("img0").endswith("img0.png")
    assert built.resolve_image("img0.jpg").endswith("img0.png")


def test_an_unfindable_reference_resolves_to_nothing(tmp_path):
    built = EvaluationConfig(base_dir=str(tmp_path), image_dirs=("reference_images",))
    assert built.resolve_image("absent.jpg") == ""
    assert built.resolve_image("") == ""


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------


def test_similarity_exposes_evaluate(evaluation_project, config):
    """The name the rest of the pipeline reaches for."""
    from vision import similarity

    result = similarity.evaluate(graph=evaluation_project["graph"],
                                 manifest=evaluation_project["manifest"],
                                 config=config, write=False)
    assert result.viewpoints
    assert 0.0 <= result.score <= 1.0


def test_the_evaluator_can_be_reused_without_carrying_state(evaluation_project, config):
    evaluator = Evaluator(evaluation_project["graph"], evaluation_project["manifest"],
                          config)
    first = evaluator.run()
    second = evaluator.run()
    assert len(first.viewpoints) == len(second.viewpoints)
    assert first.score == pytest.approx(second.score)
