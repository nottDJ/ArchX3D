"""Evidence fusion and room classification.

The behaviours under test are the ones the design philosophy demands:

* corroborating evidence compounds, so a well-evidenced room scores higher
  than a thinly-evidenced one with the same label;
* a stated fact (a room label, a ``ROOM_NAME`` attribute) is never outvoted by
  an accumulation of heuristics;
* contradictions are surfaced rather than averaged away;
* a room with no usable evidence comes back ``unknown`` rather than guessed.

Inputs are hand-built ``RoomEvidenceInput`` records rather than parsed from a
DXF, so each test varies exactly one signal.
"""

from __future__ import annotations

import pytest

from semantic import (
    Evidence,
    RoomEvidenceInput,
    classify_plan,
    classify_room,
    fuse,
    summarise,
    taxonomy,
)

TYPES = taxonomy.scoreable_types()


def room(**overrides):
    """A featureless 12 m² room, with signals added per test."""
    settings = dict(room_id="r", area=12.0, width=3.0, depth=4.0)
    settings.update(overrides)
    return RoomEvidenceInput(**settings)


# ---------------------------------------------------------------------------
# Fusion mechanics
# ---------------------------------------------------------------------------


class TestFusion:
    def test_no_evidence_yields_unknown(self):
        result = fuse([], TYPES)
        assert result.label == "unknown"
        assert result.confidence == 0.0

    def test_corroboration_compounds(self):
        """Two agreeing signals must beat one, or evidence does not accumulate."""
        one = fuse(
            [Evidence("a", 2, {"bedroom": 3.0}, "a bed")], TYPES
        )
        two = fuse(
            [
                Evidence("a", 2, {"bedroom": 3.0}, "a bed"),
                Evidence("b", 2, {"bedroom": 3.0}, "a wardrobe"),
            ],
            TYPES,
        )
        assert two.confidence > one.confidence

    def test_one_strong_signal_outweighs_several_weak_ones(self):
        """A toilet means bathroom, whatever a pile of +0.3s prefers."""
        result = fuse(
            [
                Evidence("fixture", 2, {"bathroom": 4.2}, "toilet"),
                Evidence("w1", 5, {"office": 0.3}, "weak"),
                Evidence("w2", 5, {"office": 0.3}, "weak"),
                Evidence("w3", 5, {"office": 0.3}, "weak"),
                Evidence("w4", 5, {"office": 0.3}, "weak"),
            ],
            TYPES,
        )
        assert result.label == "bathroom"

    def test_authoritative_evidence_is_not_outvoted(self):
        """The core of 'never guess if reliable information exists'."""
        result = fuse(
            [
                Evidence("room_label", 4, {"store": 4.5}, "labelled STORE",
                         authoritative=True),
                Evidence("f1", 2, {"bathroom": 4.2}, "toilet"),
                Evidence("f2", 2, {"bathroom": 3.8}, "shower"),
            ],
            TYPES,
        )
        assert result.label == "store"
        assert result.decided_by == "room_label"

    def test_contradicted_authority_reports_a_conflict(self):
        result = fuse(
            [
                Evidence("room_label", 4, {"store": 4.5}, "labelled STORE",
                         authoritative=True),
                Evidence("fixture", 2, {"bathroom": 4.2, "store": -2.5}, "toilet"),
            ],
            TYPES,
        )
        assert result.conflicts
        assert any(c.contradicted_by == "bathroom" for c in result.conflicts)

    def test_contradiction_lowers_confidence(self):
        agreed = fuse(
            [
                Evidence("room_label", 4, {"bathroom": 4.5}, "labelled BATH",
                         authoritative=True),
                Evidence("fixture", 2, {"bathroom": 4.2}, "toilet"),
            ],
            TYPES,
        )
        contradicted = fuse(
            [
                Evidence("room_label", 4, {"store": 4.5}, "labelled STORE",
                         authoritative=True),
                Evidence("fixture", 2, {"bathroom": 4.2, "store": -2.5}, "toilet"),
            ],
            TYPES,
        )
        assert agreed.confidence > contradicted.confidence

    def test_authority_does_not_corroborate_itself(self):
        """Regression: including an authority in its own corroboration check
        made every authoritative answer self-confirming, so no conflict could
        ever be detected."""
        lonely = fuse(
            [Evidence("room_label", 4, {"store": 4.5}, "labelled STORE",
                      authoritative=True)],
            TYPES,
        )
        corroborated = fuse(
            [
                Evidence("room_label", 4, {"store": 4.5}, "labelled STORE",
                         authoritative=True),
                Evidence("g", 5, {"store": 1.5}, "small and windowless"),
            ],
            TYPES,
        )
        assert corroborated.confidence > lonely.confidence

    def test_two_disagreeing_authorities_conflict(self):
        result = fuse(
            [
                Evidence("room_name_attribute", 1, {"kitchen": 5.0}, "attr",
                         authoritative=True),
                Evidence("room_label", 4, {"bedroom": 4.5}, "label",
                         authoritative=True),
            ],
            TYPES,
        )
        # The more trusted tier wins, and the disagreement is reported.
        assert result.label == "kitchen"
        assert result.conflicts

    def test_near_tie_is_reported_not_decided(self):
        result = fuse(
            [
                Evidence("a", 5, {"bedroom": 1.0}, "x"),
                Evidence("b", 5, {"office": 1.0}, "y"),
            ],
            TYPES,
        )
        assert result.conflicts or result.label == "unknown"

    def test_confidence_never_saturates(self):
        """No single observation should express certainty."""
        result = fuse(
            [Evidence("f", 2, {"bathroom": 4.2}, "toilet")], TYPES
        )
        assert result.confidence < 1.0

    def test_explanation_includes_contradicting_evidence(self):
        result = fuse(
            [
                Evidence("room_label", 4, {"store": 4.5}, "labelled STORE",
                         authoritative=True),
                Evidence("fixture", 2, {"bathroom": 4.2, "store": -2.5},
                         "TOILET block"),
            ],
            TYPES,
        )
        assert any("against:" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# Individual signals
# ---------------------------------------------------------------------------


class TestSignals:
    def test_toilet_alone_identifies_a_bathroom(self):
        result = classify_room(room(
            area=4.2, width=1.8, depth=2.3,
            blocks=[("toilet", "plumbing_fixture", 0.92, 0.5)],
        ))
        assert result.room_type == "bathroom"
        assert result.confidence > 0.6

    def test_bed_alone_identifies_a_bedroom(self):
        result = classify_room(room(
            blocks=[("bed", "furniture", 0.85, 1.0)],
        ))
        assert result.room_type == "bedroom"

    def test_kitchen_fixtures_identify_a_kitchen(self):
        result = classify_room(room(
            area=9.0,
            blocks=[
                ("cooktop", "kitchen_fixture", 0.92, 1.0),
                ("sink", "kitchen_fixture", 0.92, 1.5),
                ("refrigerator", "appliance", 0.85, 2.0),
            ],
        ))
        assert result.room_type == "kitchen"
        assert result.confidence > 0.8

    def test_elongation_identifies_a_corridor(self):
        """Aspect ratio is the one geometric signal that truly discriminates."""
        result = classify_room(room(
            area=6.5, width=8.0, depth=1.1, door_count=4, openings_known=True,
        ))
        assert result.room_type == "hallway"

    def test_repeated_instances_have_diminishing_returns(self):
        """N copies of a category must weigh more than one, but far less than N.

        Tested on the evidence weight directly rather than through a
        classification outcome: linear accumulation is the specific failure
        mode — six dining chairs summing to +12 would bury any single fixture —
        and the invariant is about the weighting, not about any one room.
        """
        from semantic.classifier import _block_evidence

        chair = ("dining_chair", "furniture", 0.9, 1.0)
        one = _block_evidence(room(blocks=[chair]))[0]
        six = _block_evidence(room(blocks=[chair] * 6))[0]

        assert six.weight > one.weight
        assert six.weight < 6 * one.weight
        # Sub-linear enough that the sixth chair adds far less than the first.
        assert six.weight < 2.0 * one.weight

    def test_a_decisive_fixture_is_not_buried_by_weak_furniture(self):
        """Three chairs alongside a toilet must not read as a dining room."""
        result = classify_room(room(
            area=4.5, width=1.9, depth=2.4,
            blocks=[("dining_chair", "furniture", 0.85, 1.0)] * 3
            + [("toilet", "plumbing_fixture", 0.92, 0.5)],
        ))
        assert result.room_type != "dining_room"

    def test_label_survives_contradicting_geometry(self):
        """A 40 m² room labelled BATH stays a bathroom, with a lower score."""
        result = classify_room(room(
            area=40.0, width=6.0, depth=6.7,
            labels=[("BATH", "bathroom", 0.94)],
        ))
        assert result.room_type == "bathroom"

    def test_attribute_outranks_a_disagreeing_text_label(self):
        result = classify_room(room(
            name_attributes=[("ENTRANCE HALL", "hallway")],
            labels=[("STORE", "store", 0.94)],
        ))
        assert result.room_type == "hallway"
        assert result.decided_by == "room_name_attribute"

    def test_vision_cannot_overturn_a_cad_label(self):
        """Imagery is tier 6 and must not overrule what the drawing states."""
        result = classify_room(room(
            labels=[("KITCHEN", "kitchen", 0.94)],
            vision_room_type="bedroom", vision_confidence=0.95,
            vision_categories=["bed", "wardrobe"],
        ))
        assert result.room_type == "kitchen"
        assert result.conflicts

    def test_vision_identifies_a_room_cad_left_anonymous(self):
        result = classify_room(room(
            vision_room_type="living_room", vision_confidence=0.9,
            vision_categories=["sofa", "coffee_table", "tv_unit"],
        ))
        assert result.room_type == "living_room"

    def test_absent_opening_data_is_not_evidence_of_no_openings(self):
        """A legacy geometry file has no opening data; that is not a claim.

        Regression: ``window_count == 0`` was scored as "this room has no
        window" even when the drawing simply had no glazing layer to read.
        Every room then looked windowless, and the types that expect to be —
        garage, store, shaft — won rooms they had no business winning.
        """
        blind = classify_room(room(area=47.0, width=7.0, depth=6.7))
        assert blind.room_type not in ("garage", "store", "shaft")

    def test_opening_counts_are_used_when_they_are_known(self):
        """The same signal must still work once the data really is present."""
        known = classify_room(room(
            area=6.5, width=8.0, depth=1.1, door_count=4, window_count=0,
            openings_known=True,
        ))
        assert known.room_type == "hallway"

    def test_featureless_room_stays_unknown(self):
        """The brief's first rule: an omission beats an invention."""
        result = classify_room(room(area=0.0, width=0.0, depth=0.0))
        assert result.room_type == "unknown"
        assert result.confidence == 0.0

    def test_specific_label_is_preserved(self):
        result = classify_room(room(
            labels=[("MASTER BED", "master_bedroom", 0.94)],
        ))
        assert result.room_type == "bedroom"
        assert result.specific_type == "master_bedroom"

    def test_area_mismatch_is_reported(self):
        """Segmentation losing most of a room must not pass silently.

        The drawing states the size; segmentation measures it. A 5x gap means
        the flood fill leaked or was cut off, and that is invisible without
        this check because the resulting geometry still looks plausible.
        """
        result = classify_room(room(
            area=4.6, declared_area=23.4,
            labels=[("BED ROOM", "bedroom", 0.94)],
        ))
        mismatch = [c for c in result.conflicts if c.signal == "area_mismatch"]
        assert mismatch
        assert "not closed" in mismatch[0].detail

    def test_merged_rooms_are_diagnosed_differently_from_lost_ones(self):
        result = classify_room(room(
            area=47.7, declared_area=23.4,
            labels=[("BED ROOM", "bedroom", 0.94)],
        ))
        mismatch = next(c for c in result.conflicts if c.signal == "area_mismatch")
        assert "merged" in mismatch.detail

    def test_rounding_and_wall_thickness_do_not_trigger_a_mismatch(self):
        """Stated sizes are internal-clear and rounded; small gaps are normal."""
        result = classify_room(room(
            area=13.0, declared_area=14.2,
            labels=[("BEDROOM", "bedroom", 0.94)],
        ))
        assert not [c for c in result.conflicts if c.signal == "area_mismatch"]

    def test_no_stated_area_means_no_cross_check(self):
        result = classify_room(room(area=4.6, labels=[("BEDROOM", "bedroom", 0.94)]))
        assert not [c for c in result.conflicts if c.signal == "area_mismatch"]

    def test_area_mismatch_does_not_change_the_room_type(self):
        """It is a data-quality report, not evidence about what the room is."""
        result = classify_room(room(
            area=4.6, declared_area=23.4,
            labels=[("BED ROOM", "bedroom", 0.94)],
        ))
        assert result.room_type == "bedroom"

    def test_reasons_are_always_produced(self):
        result = classify_room(room(
            blocks=[("toilet", "plumbing_fixture", 0.92, 0.5)],
        ))
        assert result.reasons
        assert any("TOILET" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# Plan-level behaviour
# ---------------------------------------------------------------------------


class TestPlanClassification:
    def test_adjacency_propagates_between_passes(self):
        """Being next to a bedroom raises a small room's bathroom score.

        Propagation only happens because classification runs twice: the first
        pass types the bedroom, the second uses that label as evidence for its
        neighbour. The assertion is that the neighbour *moves* — a bare
        3.6 m² windowless room is genuinely ambiguous between an en-suite and
        a walk-in store, and claiming otherwise would be the confident guess
        this system exists to avoid.
        """

        def ensuite(neighbours):
            return RoomEvidenceInput(
                room_id="ensuite", area=3.6, width=1.6, depth=2.2,
                neighbours=neighbours, window_count=0, door_count=1, openings_known=True,
            )

        bedroom = RoomEvidenceInput(
            room_id="bed", area=14.0, width=3.5, depth=4.0,
            blocks=[("bed", "furniture", 0.85, 1.0)],
            neighbours=["ensuite"], window_count=1, openings_known=True,
        )

        attached = {r.room_id: r for r in classify_plan([bedroom, ensuite(["bed"])])}
        isolated = {r.room_id: r for r in classify_plan([ensuite([])])}

        assert attached["bed"].room_type == "bedroom"
        assert (
            attached["ensuite"].posterior["bathroom"]
            > isolated["ensuite"].posterior["bathroom"]
        )

    def test_ensuite_with_a_fixture_resolves_confidently(self):
        """Adjacency plus one fixture is enough to settle an en-suite."""
        rooms = [
            RoomEvidenceInput(
                room_id="bed", area=14.0, width=3.5, depth=4.0,
                blocks=[("bed", "furniture", 0.85, 1.0)],
                neighbours=["ensuite"], window_count=1, openings_known=True,
            ),
            RoomEvidenceInput(
                room_id="ensuite", area=3.6, width=1.6, depth=2.2,
                blocks=[("toilet", "plumbing_fixture", 0.92, 0.4)],
                neighbours=["bed"], window_count=0, door_count=1, openings_known=True,
            ),
        ]
        results = {r.room_id: r for r in classify_plan(rooms)}
        assert results["ensuite"].room_type == "bathroom"
        assert results["ensuite"].confidence > 0.7

    def test_duplicate_unique_rooms_are_flagged_not_reassigned(self):
        """Two kitchens is a fact worth reporting, not one to silently fix."""
        rooms = [
            RoomEvidenceInput(
                room_id=f"k{i}", area=10.0, width=3.0, depth=3.3,
                labels=[("KITCHEN", "kitchen", 0.94)],
            )
            for i in range(2)
        ]
        results = classify_plan(rooms)
        assert all(r.room_type == "kitchen" for r in results)
        assert any(
            any(c.signal == "uniqueness" for c in r.conflicts) for r in results
        )

    def test_bedrooms_may_repeat_without_complaint(self):
        rooms = [
            RoomEvidenceInput(
                room_id=f"b{i}", area=13.0, width=3.2, depth=4.0,
                labels=[("BEDROOM", "bedroom", 0.94)],
            )
            for i in range(3)
        ]
        results = classify_plan(rooms)
        assert all(r.room_type == "bedroom" for r in results)
        assert not any(
            any(c.signal == "uniqueness" for c in r.conflicts) for r in results
        )

    def test_empty_plan_returns_nothing(self):
        assert classify_plan([]) == []

    def test_summary_counts_identified_rooms(self):
        rooms = [
            RoomEvidenceInput(room_id="a", area=4.0, width=1.8, depth=2.2,
                              blocks=[("toilet", "plumbing_fixture", 0.92, 0.4)]),
            RoomEvidenceInput(room_id="b", area=0.0),
        ]
        stats = summarise(classify_plan(rooms))
        assert stats["rooms"] == 2
        assert stats["identified"] == 1
        assert stats["unidentified"] == 1


# ---------------------------------------------------------------------------
# Priors
# ---------------------------------------------------------------------------


class TestTaxonomy:
    def test_area_score_peaks_at_the_typical_area(self):
        prior = taxonomy.ROOM_PRIORS["bedroom"]
        assert prior.area_score(prior.area_typical) > prior.area_score(30.0)
        assert prior.area_score(30.0) > prior.area_score(200.0)

    def test_area_score_decays_rather_than_vetoing(self):
        """An unusual-but-real room must stay possible."""
        prior = taxonomy.ROOM_PRIORS["bedroom"]
        assert prior.area_score(34.0) > -2.6

    def test_hallway_tolerates_extreme_elongation(self):
        hallway = taxonomy.ROOM_PRIORS["hallway"]
        bedroom = taxonomy.ROOM_PRIORS["bedroom"]
        assert hallway.aspect_score(6.0) > bedroom.aspect_score(6.0)

    def test_habitable_rooms_expect_a_window(self):
        """A windowless bedroom is implausible; a windowless bathroom is not."""
        assert taxonomy.window_evidence(0, "bedroom") < 0
        assert taxonomy.window_evidence(0, "bathroom") > 0
        assert taxonomy.window_evidence(1, "bedroom") > 0

    def test_windowless_scoring_is_two_sided(self):
        """A shaft with a window is as odd as a bedroom without one."""
        assert taxonomy.window_evidence(1, "shaft") < 0
        assert taxonomy.window_evidence(0, "shaft") > 0

    def test_many_doors_suggest_circulation(self):
        assert taxonomy.door_evidence(4, "hallway") > 0
        assert taxonomy.door_evidence(4, "bedroom") < 0

    def test_every_prior_has_a_sane_range(self):
        for name, prior in taxonomy.ROOM_PRIORS.items():
            low, high = prior.area_range
            assert 0 < low < high, name
            assert low <= prior.area_typical <= high, name
