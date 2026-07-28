"""
Tests for aggregation and the vocabulary it aggregates.

The rule under test throughout is the one the engine exists to keep honest:
**an axis that could not be measured is excluded from normalisation, never
scored zero.** Getting that wrong would make every build with a missing
reference photograph look broken, and would make "improve the score" mean
"supply more photographs".
"""

from __future__ import annotations

import pytest

from evaluation import scoring
from evaluation.schema import (
    AXES,
    COLOUR,
    DEFAULT_WEIGHTS,
    LAYOUT,
    LIGHTING,
    MATERIAL,
    OBJECTS,
    AxisScore,
    Finding,
    Subsystem,
    merge,
    rank,
)


def measured(axis, score, confidence=0.8):
    return AxisScore(axis=axis, score=score, measured=True, confidence=confidence)


def finding(axis=COLOUR, severity=0.5, subsystem=Subsystem.COLOUR_PALETTE,
            confidence=0.8, summary="a finding", **kwargs):
    return Finding(axis=axis, summary=summary, subsystem=subsystem,
                   severity=severity, confidence=confidence, **kwargs)


# ---------------------------------------------------------------------------
# Unmeasured is excluded, not zero
# ---------------------------------------------------------------------------


def test_all_axes_measured_is_a_plain_weighted_mean():
    axes = {axis: measured(axis, 0.8) for axis in AXES}
    totals = scoring.combine(axes)

    assert totals.score == pytest.approx(0.8)
    assert totals.weight_used == pytest.approx(1.0)
    assert totals.complete


def test_an_unmeasured_axis_does_not_drag_the_score_down():
    """The whole point: a missing photograph is not a bad reconstruction."""
    complete = scoring.combine({axis: measured(axis, 0.8) for axis in AXES})

    partial_axes = {axis: measured(axis, 0.8) for axis in AXES}
    partial_axes[MATERIAL] = AxisScore.unmeasured(MATERIAL, "no albedo pass")
    partial = scoring.combine(partial_axes)

    assert partial.score == pytest.approx(complete.score)
    assert partial.unmeasured_axes == [MATERIAL]
    assert not partial.complete


def test_a_partial_evaluation_reports_how_much_it_saw():
    axes = {COLOUR: measured(COLOUR, 0.9), OBJECTS: measured(OBJECTS, 0.9)}
    totals = scoring.combine(axes)

    expected = (DEFAULT_WEIGHTS[COLOUR] + DEFAULT_WEIGHTS[OBJECTS]) / sum(
        DEFAULT_WEIGHTS.values()
    )
    assert totals.weight_used == pytest.approx(expected)
    # A 0.9 over two axes must not be able to pass as a 0.9 over five.
    assert totals.confidence < 0.9


def test_nothing_measurable_scores_zero_with_zero_weight():
    """Distinguishable from a real zero by weight_used, which is why it exists."""
    axes = {axis: AxisScore.unmeasured(axis, "no images") for axis in AXES}
    totals = scoring.combine(axes)

    assert totals.score == 0.0
    assert totals.weight_used == 0.0
    assert totals.measured_axes == []


def test_weights_actually_weight():
    axes = {axis: measured(axis, 1.0) for axis in AXES}
    axes[OBJECTS] = measured(OBJECTS, 0.0)

    balanced = scoring.combine(axes, {axis: 1.0 for axis in AXES})
    object_heavy = scoring.combine(axes, {**DEFAULT_WEIGHTS, OBJECTS: 5.0})
    assert object_heavy.score < balanced.score


def test_confidence_falls_with_coverage_even_when_the_axes_are_certain():
    one = scoring.combine({COLOUR: measured(COLOUR, 0.9, confidence=1.0)})
    all_five = scoring.combine({a: measured(a, 0.9, confidence=1.0) for a in AXES})
    assert one.confidence < all_five.confidence


# ---------------------------------------------------------------------------
# Merging across viewpoints
# ---------------------------------------------------------------------------


def test_an_axis_measured_anywhere_is_measured_for_the_group():
    merged = scoring.merge_axes([
        {COLOUR: measured(COLOUR, 0.4)},
        {COLOUR: AxisScore.unmeasured(COLOUR, "no reference")},
    ])
    assert merged[COLOUR].measured
    assert merged[COLOUR].score == pytest.approx(0.4)


def test_merging_weights_by_confidence():
    """A shaky view should not outvote a clean one."""
    merged = scoring.merge_axes([
        {LIGHTING: measured(LIGHTING, 1.0, confidence=0.9)},
        {LIGHTING: measured(LIGHTING, 0.0, confidence=0.1)},
    ])
    assert merged[LIGHTING].score > 0.8


def test_merging_records_the_spread_between_viewpoints():
    """Two views of one room disagreeing is itself worth knowing."""
    merged = scoring.merge_axes([
        {LAYOUT: measured(LAYOUT, 0.2)},
        {LAYOUT: measured(LAYOUT, 0.9)},
    ])
    assert merged[LAYOUT].detail["spread"] == pytest.approx(0.7)
    assert merged[LAYOUT].detail["sources"] == 2


def test_an_axis_measured_nowhere_keeps_a_reason():
    merged = scoring.merge_axes([
        {COLOUR: AxisScore.unmeasured(COLOUR, "reference image not readable")},
    ])
    assert not merged[COLOUR].measured
    assert "not readable" in merged[COLOUR].reason


def test_merging_nothing_leaves_every_axis_unmeasured():
    merged = scoring.merge_axes([])
    assert all(not merged[axis].measured for axis in AXES)


# ---------------------------------------------------------------------------
# Room weighting
# ---------------------------------------------------------------------------


def test_rooms_weigh_by_floor_area():
    class Room:
        area = 40.0

    assert scoring.room_weight(Room()) == 40.0


def test_a_room_with_no_recorded_area_still_counts():
    """Unmeasurable is not unimportant."""
    class Room:
        area = 0.0

    assert scoring.room_weight(Room()) == 1.0
    assert scoring.room_weight(None) == 1.0


def test_weighted_mean_ignores_zero_weights():
    assert scoring.weighted_mean([(1.0, 0.0), (0.0, 1.0)]) == 0.0
    assert scoring.weighted_mean([]) == 0.0


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def test_findings_rank_by_severity_then_deterministically():
    ordered = rank([
        finding(severity=0.2, summary="b"),
        finding(severity=0.9, summary="a"),
        finding(severity=0.2, summary="a"),
    ])
    assert [f.summary for f in ordered] == ["a", "a", "b"]
    assert ordered[0].severity == 0.9


def test_the_same_finding_from_two_viewpoints_becomes_one():
    merged = merge([
        finding(viewpoint="img_a1", severity=0.4),
        finding(viewpoint="img_a2", severity=0.6),
    ])
    assert len(merged) == 1
    assert merged[0].severity == 0.6
    assert merged[0].evidence["agreeing_viewpoints"] == 2
    assert merged[0].viewpoint == ""       # it is now about the room


def test_corroboration_raises_confidence_but_not_to_certainty():
    merged = merge([finding(viewpoint=f"v{i}", confidence=0.8) for i in range(6)])
    assert 0.8 < merged[0].confidence < 1.0


def test_findings_about_different_objects_stay_separate():
    merged = merge([
        finding(objects=["sofa_1"]),
        finding(objects=["table_1"]),
    ])
    assert len(merged) == 2


def test_subsystem_pressure_sums_severity_times_confidence():
    pressure = scoring.subsystem_pressure([
        finding(subsystem=Subsystem.LIGHTING_ENVIRONMENT, severity=0.5, confidence=0.8),
        finding(subsystem=Subsystem.LIGHTING_ENVIRONMENT, severity=0.5, confidence=0.8),
        finding(subsystem=Subsystem.CAMERA_FIT, severity=1.0, confidence=0.5),
    ])
    assert pressure[Subsystem.LIGHTING_ENVIRONMENT] == pytest.approx(0.8)
    assert pressure[Subsystem.CAMERA_FIT] == pytest.approx(0.5)


def test_many_mild_findings_can_outweigh_one_severe_one():
    """Five rooms slightly too dark is a lighting problem too."""
    mild = [finding(subsystem=Subsystem.LIGHTING_ENVIRONMENT, severity=0.3,
                    confidence=1.0, objects=[f"o{i}"]) for i in range(5)]
    severe = [finding(subsystem=Subsystem.CAMERA_FIT, severity=1.0, confidence=1.0)]
    pressure = scoring.subsystem_pressure(mild + severe)
    assert pressure[Subsystem.LIGHTING_ENVIRONMENT] > pressure[Subsystem.CAMERA_FIT]


def test_coverage_reports_what_was_not_seen():
    reported = scoring.coverage(total_viewpoints=4, evaluated=2, with_reference=1,
                                passes_seen=["albedo", "albedo", "depth"])
    assert reported["reference_coverage"] == 0.25
    assert reported["passes_available"] == ["albedo", "depth"]


def test_top_findings_deduplicates_and_truncates():
    collected = [finding(objects=[f"o{i}"], severity=i / 20.0) for i in range(20)]
    assert len(scoring.top_findings(collected, limit=5)) == 5
