"""CAD ↔ image registration: the transform, the consensus, and the ladder.

The guarantee under test is that the pipeline stops *assuming* how a reference
sheet lines up with the drawing and starts measuring it — and, where it cannot
measure, says so instead of quietly proceeding on the old assumption.

Most of these build their anchors by hand. That is the point of the package
being stdlib-only and type-agnostic: a registration must be checkable without
a DXF, without imagery and without an API key. The last class runs the real
reader over the committed apartment fixture.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pytest

import registration as reg
from registration import (
    Correspondence,
    LabelAnchor,
    Method,
    PlanTransform,
    consensus,
    labels as labels_mod,
    transform as transform_mod,
)

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "apartment.dxf"
)


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------
#
# Deliberately minimal duck types rather than the real vision/CAD records: if
# these stopped being enough, the package would have grown a dependency it is
# not supposed to have, and that should fail loudly here.


@dataclass
class FakeBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)


@dataclass
class FakeLabel:
    local_id: str
    text: str
    bbox: Optional[FakeBox]
    room_type: str = "unknown"
    confidence: float = 0.9
    normalised: str = ""


@dataclass
class FakeObservation:
    image_id: str = "img0"
    labels: List[FakeLabel] = field(default_factory=list)


@dataclass
class FakeText:
    uid: str
    text: str
    normalised: str
    insert: Tuple[float, float]
    role: str = "room_label"
    room_type: str = "unknown"
    confidence: float = 0.9


@dataclass
class FakeDocument:
    texts: List[FakeText] = field(default_factory=list)


@dataclass
class FakeRegion:
    id: str
    area: float
    room_type: str = "unknown"
    room_type_confidence: float = 0.0
    aspect: float = 1.2


def plan_document(**overrides) -> FakeDocument:
    """A four-room plan, in metres, spanning roughly 12 x 9 m."""
    rooms = {
        "LIVING ROOM": (3.0, 6.5),
        "KITCHEN": (9.5, 6.5),
        "MASTER BED": (3.0, 2.0),
        "BATH": (9.5, 2.0),
    }
    rooms.update(overrides)
    return FakeDocument(
        texts=[
            FakeText(uid=f"t{i}", text=name, normalised=name, insert=point)
            for i, (name, point) in enumerate(sorted(rooms.items()))
        ]
    )


BOUNDS_MIN = (0.0, 0.0)
BOUNDS_MAX = (12.0, 9.0)


def observation_from(
    transform: PlanTransform,
    document: FakeDocument,
    *,
    image_id: str = "img0",
    extra: Optional[List[Tuple[str, Tuple[float, float]]]] = None,
    jitter: float = 0.0,
) -> FakeObservation:
    """Print each of the drawing's labels where ``transform`` says they land.

    Inverting a known transform to build the image side is what makes the
    expected answer unambiguous: the test asserts the fit recovers the
    transform it was generated from, rather than asserting on a number nobody
    can derive independently.
    """
    inverse = transform.inverse()
    assert inverse is not None

    labels = []
    for index, text in enumerate(document.texts):
        u, v = inverse.apply(*text.insert)
        labels.append(
            FakeLabel(
                local_id=f"L{index}",
                text=text.text,
                bbox=FakeBox(u - 0.04 + jitter, v - 0.02, u + 0.04 + jitter, v + 0.02),
            )
        )

    for offset, (name, uv) in enumerate(extra or []):
        labels.append(
            FakeLabel(
                local_id=f"X{offset}",
                text=name,
                bbox=FakeBox(uv[0] - 0.04, uv[1] - 0.02, uv[0] + 0.04, uv[1] + 0.02),
            )
        )

    return FakeObservation(image_id=image_id, labels=labels)


def assert_transforms_close(got: PlanTransform, want: PlanTransform, tol: float = 0.05):
    __tracebackhide__ = True
    for name in ("a", "b", "c", "d", "tx", "ty"):
        assert abs(getattr(got, name) - getattr(want, name)) < tol, (
            f"{name}: {getattr(got, name):.4f} != {getattr(want, name):.4f}"
        )


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


class TestSimilarityFit:
    def test_recovers_a_known_transform_exactly(self):
        want = PlanTransform.from_similarity(scale=14.0, rotation_deg=0.0, tx=-1.0, ty=8.5)
        inverse = want.inverse()
        plan = [(2.0, 3.0), (9.0, 7.0), (5.0, 1.5)]
        image = [inverse.apply(*p) for p in plan]

        got = transform_mod.fit_similarity(image, plan)

        assert got is not None
        assert_transforms_close(got, want, tol=1e-6)

    def test_recovers_a_rotated_transform(self):
        want = PlanTransform.from_similarity(scale=20.0, rotation_deg=90.0, tx=3.0, ty=-2.0)
        inverse = want.inverse()
        plan = [(1.0, 1.0), (8.0, 2.0), (4.0, 6.0)]
        image = [inverse.apply(*p) for p in plan]

        got = transform_mod.fit_similarity(image, plan)

        assert got is not None
        assert abs(got.rotation_deg - 90.0) < 1e-4
        assert abs(got.scale - 20.0) < 1e-6

    def test_a_fitted_transform_is_always_a_similarity(self):
        # Noisy inputs must not be absorbed into a shear: the residual is the
        # only warning that a correspondence set is wrong, and six free
        # parameters would hide it.
        got = transform_mod.fit_similarity(
            [(0.1, 0.1), (0.9, 0.15), (0.5, 0.85)],
            [(1.0, 8.0), (11.0, 7.6), (6.0, 1.0)],
        )
        assert got is not None
        assert got.is_similarity

    def test_refuses_a_single_point(self):
        assert transform_mod.fit_similarity([(0.5, 0.5)], [(6.0, 4.5)]) is None

    def test_refuses_coincident_image_points(self):
        # Two labels printed on top of each other give no direction, and a
        # fit that returned identity here would be silently wrong.
        assert transform_mod.fit_similarity(
            [(0.5, 0.5), (0.5, 0.5)], [(1.0, 1.0), (9.0, 8.0)]
        ) is None

    def test_refuses_an_implausible_scale(self):
        # 1e-4 of the image spanning the whole building is not a floor plan.
        assert transform_mod.fit_similarity(
            [(0.5000, 0.5), (0.5001, 0.5)], [(0.0, 0.0), (10.0, 0.0)]
        ) is None

    def test_inverse_round_trips(self):
        forward = PlanTransform.from_similarity(11.0, 37.0, 2.0, -3.0)
        back = forward.inverse()
        assert back is not None
        for u, v in [(0.0, 0.0), (0.3, 0.9), (1.0, 1.0)]:
            x, y = forward.apply(u, v)
            ru, rv = back.apply(x, y)
            assert math.isclose(ru, u, abs_tol=1e-9)
            assert math.isclose(rv, v, abs_tol=1e-9)

    def test_rotation_is_snapped_to_the_sheet(self):
        # A drawing is printed square. A 1.5 degree fit is label noise, and
        # keeping it swings the far end of the building.
        want = PlanTransform.from_similarity(14.0, 1.5, 0.0, 9.0)
        inverse = want.inverse()
        plan = [(1.0, 1.0), (10.0, 7.0), (5.0, 4.0)]
        image = [inverse.apply(*p) for p in plan]

        snapped, did = transform_mod.snap_rotation(want, image, plan)

        assert did
        assert abs(snapped.rotation_deg) < 1e-6

    def test_a_genuinely_angled_drawing_is_not_snapped(self):
        angled = PlanTransform.from_similarity(14.0, 31.0, 0.0, 9.0)
        _, did = transform_mod.snap_rotation(
            angled, [(0.1, 0.1), (0.9, 0.9)], [(1.0, 8.0), (10.0, 1.0)]
        )
        assert not did


class TestLegacyMapping:
    """``stretch_to_bounds`` must reproduce the old inline arithmetic exactly.

    It is the fallback rung, so any drift here would silently move furniture
    in every project that does not register.
    """

    def test_matches_the_previous_linear_map(self):
        bounds_min, bounds_max = (-2.0, 1.0), (10.0, 7.0)
        width = bounds_max[0] - bounds_min[0]
        depth = bounds_max[1] - bounds_min[1]
        stretch = PlanTransform.stretch_to_bounds(bounds_min, bounds_max)

        for u, v in [(0.0, 0.0), (0.25, 0.8), (1.0, 1.0), (0.5, 0.5)]:
            legacy = (bounds_min[0] + u * width, bounds_max[1] - v * depth)
            assert stretch.apply(u, v) == pytest.approx(legacy)

    def test_the_assumed_transform_is_not_a_similarity(self):
        # Which is exactly how a consumer can tell it was assumed: stretching
        # a non-square building to a frame is anisotropic.
        stretch = PlanTransform.stretch_to_bounds((0.0, 0.0), (12.0, 9.0))
        assert not stretch.is_similarity

    def test_a_square_building_stretches_to_a_similarity(self):
        stretch = PlanTransform.stretch_to_bounds((0.0, 0.0), (9.0, 9.0))
        assert stretch.is_similarity


# ---------------------------------------------------------------------------
# Label matching
# ---------------------------------------------------------------------------


class TestNormalisation:
    def test_agrees_with_the_cad_normaliser(self):
        """The two copies must not drift; matching depends on them agreeing."""
        cad_text = pytest.importorskip(
            "cad.text", reason="the CAD package is needed to compare normalisers"
        )
        corpus = [
            "M.BED.RM", "W.C.", "LIVING/DINING", "BEDROOM-2", "Master Bed",
            "KITCHEN", "T O I L E T", "STORE (UNDER STAIR)", "BATH  ",
            "DRAWING\\PUJA", "HALL", "BED RM. 01",
        ]
        for raw in corpus:
            assert labels_mod.normalise(raw) == cad_text.normalise(raw), raw

    def test_uninformative_strings_are_rejected(self):
        for text in ["N", "UP", "1", "3600", "SCALE", "12.5", ""]:
            anchor = LabelAnchor(text=text, normalised=labels_mod.normalise(text))
            assert not anchor.informative, text

    def test_title_block_boilerplate_is_rejected(self):
        # It is printed on the sheet and it does have a position — but the
        # title block is laid out per sheet, not per building, so matching one
        # would drag the fit away from the drawing.
        for text in ["SCALE 1:100", "DRAWN BY: ARCHX", "ALL DIMENSIONS IN MM",
                     "GROUND FLOOR PLAN", "DRAWING NO. A-101"]:
            anchor = LabelAnchor(text=text, normalised=labels_mod.normalise(text))
            assert not anchor.informative, text

    def test_room_names_are_informative(self):
        for text in ["KITCHEN", "MASTER BED", "W C", "BEDROOM 2",
                     "ENTRANCE HALL", "KITCHEN 12.00 SQ.M."]:
            anchor = LabelAnchor(text=text, normalised=labels_mod.normalise(text))
            assert anchor.informative, text


class TestMeasurementStripping:
    """Real drawings write the room name and its area as one string.

    ``KITCHEN\\P12.00 SQ.M.`` in the DXF against ``KITCHEN`` on the sheet has
    to reduce to a common key, or an exact match scores as a fuzzy one.
    """

    @pytest.mark.parametrize("raw,want", [
        ("KITCHEN 12.00 SQ.M.", "KITCHEN"),
        ("LIVING ROOM 28.00 SQ.M.", "LIVING ROOM"),
        ("BATH 8.64 SQ.M.", "BATH"),
        ("MASTER BED 20.16 SQ.M.", "MASTER BED"),
        ("HALL 21.60 SQ.M.", "HALL"),
        ("ENTRANCE HALL", "ENTRANCE HALL"),
        ("STUDY 110 SFT", "STUDY"),
    ])
    def test_the_area_annotation_is_dropped(self, raw, want):
        assert labels_mod.strip_measurements(labels_mod.normalise(raw)) == want

    def test_a_room_number_survives(self):
        # "BEDROOM 2" and "BEDROOM 3" are only told apart by the number; a
        # greedy strip would collapse both to "BEDROOM" and turn an exact
        # match into a tie.
        got = labels_mod.strip_measurements(labels_mod.normalise("BEDROOM 2 16.00 SQ.M."))
        assert got == "BEDROOM 2"

    def test_a_name_with_an_area_matches_the_bare_name(self):
        drawing = LabelAnchor(normalised=labels_mod.normalise("KITCHEN 12.00 SQ.M."))
        sheet = LabelAnchor(normalised="KITCHEN")
        assert labels_mod.text_similarity(drawing.match_key, sheet.match_key) == 1.0


class TestUnknownIsNotARoomType:
    """``"unknown"`` is an absence of information, not a value.

    Both parsers emit it when they cannot resolve a string. Treating it as a
    room type made every unresolved label "agree" with every other one, which
    manufactured a candidate correspondence between every pair of labels on
    the sheet — precisely the ambiguity this engine exists to remove.
    """

    def test_two_unresolved_labels_do_not_pair(self):
        image = [LabelAnchor(text="KITCHEN", normalised="KITCHEN",
                             point=(0.5, 0.5), uid="L0", room_type="unknown")]
        cad = [LabelAnchor(text="BATH", normalised="BATH", point=(9.0, 2.0),
                           uid="c0", room_type="unknown")]
        assert labels_mod.candidates(image, cad) == []

    def test_named_room_types_still_pair(self):
        image = [LabelAnchor(text="TOILET", normalised="TOILET", point=(0.5, 0.5),
                             uid="L0", room_type="bathroom")]
        cad = [LabelAnchor(text="W.C.", normalised="W C", point=(9.0, 2.0),
                           uid="c0", room_type="bathroom")]
        assert len(labels_mod.candidates(image, cad)) == 1


class TestTextSimilarity:
    def test_equality_is_certain(self):
        assert labels_mod.text_similarity("KITCHEN", "KITCHEN") == 1.0

    def test_containment_is_strong(self):
        score = labels_mod.text_similarity("BEDROOM", "MASTER BEDROOM")
        assert score == pytest.approx(labels_mod.CONTAINMENT_WEIGHT)

    def test_a_misread_character_still_matches(self):
        # Sheet-resolution text is a handful of pixels; the model drops or
        # transposes a character routinely.
        assert labels_mod.text_similarity("MASTER BEDROOM", "MASTER BEDROQM") > 0.6

    def test_whitespace_variation_matches(self):
        assert labels_mod.text_similarity("BED ROOM 1", "BEDROOM 1") > 0.6

    def test_different_rooms_do_not_match(self):
        for a, b in [
            ("KITCHEN", "LIVING ROOM"),
            ("BATH", "MASTER BED"),
            ("BEDROOM 2", "MASTER BED"),
            ("HALL", "KITCHEN"),
        ]:
            assert labels_mod.text_similarity(a, b) == 0.0, f"{a} vs {b}"


class TestCandidates:
    def test_a_repeated_name_generates_every_reading(self):
        # Three bedrooms genuinely admit three readings of one printed word.
        # Narrowing here would be guessing with less information than the
        # consensus fit will have.
        image = [LabelAnchor(text="BEDROOM", normalised="BEDROOM", point=(0.5, 0.5), uid="L0")]
        cad = [
            LabelAnchor(text="BEDROOM 1", normalised="BEDROOM 1", point=(2.0, 2.0), uid="c0"),
            LabelAnchor(text="BEDROOM 2", normalised="BEDROOM 2", point=(8.0, 2.0), uid="c1"),
            LabelAnchor(text="BEDROOM 3", normalised="BEDROOM 3", point=(8.0, 7.0), uid="c2"),
        ]
        pairs = labels_mod.candidates(image, cad)
        assert len(pairs) == 3
        assert {p.cad_uid for p in pairs} == {"c0", "c1", "c2"}

    def test_synonyms_match_through_room_type(self):
        image = [LabelAnchor(text="TOILET", normalised="TOILET", point=(0.5, 0.5),
                             uid="L0", room_type="bathroom")]
        cad = [LabelAnchor(text="W.C.", normalised="W C", point=(9.0, 2.0),
                           uid="c0", room_type="bathroom")]
        pairs = labels_mod.candidates(image, cad)
        assert len(pairs) == 1
        assert pairs[0].weight == pytest.approx(labels_mod.ROOM_TYPE_WEIGHT, abs=0.2)


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------


class TestConsensus:
    def _pairs(self):
        """One image label per room, plus two decoys that agree with nothing."""
        want = PlanTransform.from_similarity(14.0, 0.0, -1.0, 8.5)
        inverse = want.inverse()
        plan = {"A": (2.0, 6.0), "B": (9.0, 6.0), "C": (2.0, 2.0), "D": (9.0, 2.0)}

        pairs = []
        for index, (name, point) in enumerate(sorted(plan.items())):
            u, v = inverse.apply(*point)
            pairs.append(Correspondence(
                text=name, image_uv=(u, v), plan_xy=point,
                cad_uid=f"c{index}", image_label_id=f"L{index}", weight=1.0,
            ))
        return want, pairs

    def test_finds_the_transform_the_labels_agree_on(self):
        want, pairs = self._pairs()
        found = consensus.find(pairs, tolerance_m=1.0)

        assert found.ok
        assert found.inlier_count == 4
        assert_transforms_close(found.transform, want, tol=1e-4)

    def test_rejects_a_wrong_pairing(self):
        want, pairs = self._pairs()
        # The same printed word read against the wrong room: a candidate that
        # is textually perfect and geometrically impossible.
        pairs.append(Correspondence(
            text="A", image_uv=pairs[0].image_uv, plan_xy=(9.0, 2.0),
            cad_uid="c3", image_label_id="L0", weight=1.0,
        ))

        found = consensus.find(pairs, tolerance_m=1.0)

        assert found.ok
        assert_transforms_close(found.transform, want, tol=1e-3)
        assert all(c.image_label_id != "L0" or c.plan_xy != (9.0, 2.0)
                   for c in found.correspondences)

    def test_will_not_count_one_label_twice(self):
        # Without the one-to-one rule a collapsed transform scores perfectly
        # by mapping everything onto everything.
        _, pairs = self._pairs()
        duplicated = pairs + [
            Correspondence(text=p.text, image_uv=p.image_uv, plan_xy=p.plan_xy,
                           cad_uid=p.cad_uid, image_label_id=p.image_label_id,
                           weight=1.0)
            for p in pairs
        ]
        found = consensus.find(duplicated, tolerance_m=1.0)
        assert found.inlier_count == 4

    def test_is_deterministic(self):
        _, pairs_a = self._pairs()
        _, pairs_b = self._pairs()
        a = consensus.find(pairs_a, tolerance_m=1.0)
        b = consensus.find(list(reversed(pairs_b)), tolerance_m=1.0)
        assert_transforms_close(a.transform, b.transform, tol=1e-9)

    def test_nothing_agreeing_is_reported_as_failure(self):
        pairs = [
            Correspondence(text="A", image_uv=(0.1, 0.1), plan_xy=(1.0, 1.0),
                           cad_uid="c0", image_label_id="L0", weight=1.0),
            Correspondence(text="B", image_uv=(0.9, 0.9), plan_xy=(2.0, 8.0),
                           cad_uid="c1", image_label_id="L1", weight=1.0),
            Correspondence(text="C", image_uv=(0.5, 0.1), plan_xy=(9.0, 1.0),
                           cad_uid="c2", image_label_id="L2", weight=1.0),
        ]
        # Two points always fit exactly, so a failure has to be a failure to
        # find a *third* agreeing label at a tight tolerance.
        found = consensus.find(pairs, tolerance_m=0.05)
        assert found.inlier_count <= 2

    def test_tolerance_scales_with_the_building(self):
        small = consensus.tolerance_for((0.0, 0.0), (6.0, 4.0))
        large = consensus.tolerance_for((0.0, 0.0), (80.0, 60.0))
        assert small < large
        assert small >= consensus.TOLERANCE_FLOOR_M


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


class TestPlanRegistration:
    def test_a_clean_plan_registers_by_label_consensus(self):
        want = PlanTransform.from_similarity(14.0, 0.0, -1.0, 8.5)
        document = plan_document()
        observation = observation_from(want, document)

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert result.registered
        assert result.method == Method.LABEL_CONSENSUS
        assert len(result.inliers) == 4
        assert result.residual_mean_m < 0.05
        assert result.confidence > 0.7
        assert_transforms_close(result.transform, want, tol=1e-3)

    def test_a_composite_sheet_is_detected_and_located(self):
        # The reported failure case: the plan occupies one corner of a sheet
        # that also carries an exterior render. The old code assumed a full
        # frame and lost every detection.
        want = PlanTransform.from_similarity(48.0, 0.0, -1.0, 9.5)
        document = plan_document()
        observation = observation_from(want, document)

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert result.registered
        assert result.sheet_region is not None
        assert result.sheet_region.looks_composite
        assert result.sheet_region.coverage < 0.2
        assert any("composite sheet" in w for w in result.warnings)

    def test_a_full_frame_plan_is_not_called_composite(self):
        want = PlanTransform.stretch_to_bounds(BOUNDS_MIN, BOUNDS_MAX)
        # Square the scale so the generated image is a true similarity.
        want = PlanTransform.from_similarity(12.0, 0.0, 0.0, 9.0)
        document = plan_document()
        observation = observation_from(want, document)

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert result.registered
        assert not result.sheet_region.looks_composite
        assert not any("composite sheet" in w for w in result.warnings)

    def test_a_second_floor_on_the_sheet_is_reported_not_absorbed(self):
        # A sheet with two plans registers against the one it matches and
        # names the other floor's rooms rather than pulling them in.
        want = PlanTransform.from_similarity(24.0, 0.0, -1.0, 8.5)
        document = plan_document()
        observation = observation_from(
            want, document,
            extra=[("STUDY", (0.80, 0.30)), ("GUEST BED", (0.85, 0.55)),
                   ("TERRACE", (0.90, 0.75))],
        )

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert result.registered
        assert "STUDY" in result.unmatched_image_labels
        assert "GUEST BED" in result.unmatched_image_labels
        assert any("another plan on the same sheet" in w for w in result.warnings)

    def test_a_rotated_sheet_registers(self):
        want = PlanTransform.from_similarity(16.0, 90.0, 1.0, -1.0)
        document = plan_document()
        observation = observation_from(want, document)

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert result.registered
        assert abs(result.transform.rotation_deg - 90.0) < 1.0

    def test_noisy_label_boxes_still_register(self):
        want = PlanTransform.from_similarity(14.0, 0.0, -1.0, 8.5)
        document = plan_document()
        observation = observation_from(want, document, jitter=0.012)

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert result.registered
        assert result.residual_max_m < consensus.tolerance_for(BOUNDS_MIN, BOUNDS_MAX)

    def test_no_labels_in_the_image_falls_back_and_says_so(self):
        document = plan_document()
        observation = FakeObservation(image_id="img0", labels=[])

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert not result.registered
        assert result.method == Method.PLAN_BOUNDS
        assert result.transform is not None
        assert "no labels were read" in result.reason
        assert any("filling the frame" in w for w in result.warnings)

    def test_the_fallback_reproduces_the_legacy_behaviour(self):
        result = reg.register_plan_view(
            plan_document(), FakeObservation(), BOUNDS_MIN, BOUNDS_MAX
        )
        legacy = PlanTransform.stretch_to_bounds(BOUNDS_MIN, BOUNDS_MAX)
        assert_transforms_close(result.transform, legacy, tol=1e-9)

    def test_a_drawing_with_no_text_cannot_register(self):
        result = reg.register_plan_view(
            FakeDocument(texts=[]),
            observation_from(
                PlanTransform.from_similarity(14.0, 0.0, -1.0, 8.5), plan_document()
            ),
            BOUNDS_MIN, BOUNDS_MAX,
        )
        assert not result.registered
        assert "no usable text labels" in result.reason

    def test_labels_that_match_nothing_fall_back(self):
        document = plan_document()
        observation = FakeObservation(labels=[
            FakeLabel("L0", "WORKSHOP", FakeBox(0.1, 0.1, 0.2, 0.15)),
            FakeLabel("L1", "PLANT ROOM", FakeBox(0.7, 0.6, 0.8, 0.65)),
        ])

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert not result.registered
        assert result.method == Method.PLAN_BOUNDS
        assert "do not match" in result.reason or "match" in result.reason

    def test_assumed_transforms_can_be_refused(self):
        # A caller that would rather place nothing than place from a guess.
        result = reg.register_plan_view(
            plan_document(), FakeObservation(), BOUNDS_MIN, BOUNDS_MAX,
            allow_assumed=False,
        )
        assert result.method == Method.NONE
        assert result.transform is None
        assert result.confidence == 0.0

    def test_one_matching_label_anchors_but_admits_it_guessed(self):
        document = plan_document()
        observation = FakeObservation(labels=[
            FakeLabel("L0", "KITCHEN", FakeBox(0.70, 0.28, 0.80, 0.32)),
        ])

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert result.method == Method.SINGLE_ANCHOR
        assert result.registered
        assert result.confidence < 0.4
        assert any("assumed, not measured" in w for w in result.warnings)

    def test_an_ambiguous_single_label_is_not_treated_as_an_anchor(self):
        # "BEDROOM" against three bedrooms is a coin toss with three sides.
        document = FakeDocument(texts=[
            FakeText("c0", "BEDROOM 1", "BEDROOM 1", (2.0, 2.0)),
            FakeText("c1", "BEDROOM 2", "BEDROOM 2", (8.0, 2.0)),
            FakeText("c2", "BEDROOM 3", "BEDROOM 3", (8.0, 7.0)),
        ])
        observation = FakeObservation(labels=[
            FakeLabel("L0", "BEDROOM", FakeBox(0.4, 0.4, 0.5, 0.45)),
        ])

        result = reg.register_plan_view(document, observation, BOUNDS_MIN, BOUNDS_MAX)

        assert result.method == Method.PLAN_BOUNDS
        assert not result.registered

    def test_the_result_serialises(self):
        want = PlanTransform.from_similarity(14.0, 0.0, -1.0, 8.5)
        document = plan_document()
        result = reg.register_plan_view(
            document, observation_from(want, document), BOUNDS_MIN, BOUNDS_MAX
        )
        payload = result.to_dict()

        assert payload["registered"] is True
        assert payload["method"] == Method.LABEL_CONSENSUS
        assert PlanTransform.from_dict(payload["transform"]) is not None
        assert result.explain()

    def test_batch_registration_keys_by_image(self):
        want = PlanTransform.from_similarity(14.0, 0.0, -1.0, 8.5)
        document = plan_document()
        results = reg.register_plan_views(
            document,
            [observation_from(want, document, image_id="a"),
             FakeObservation(image_id="b")],
            BOUNDS_MIN, BOUNDS_MAX,
        )
        assert set(results) == {"a", "b"}
        assert results["a"].registered
        assert not results["b"].registered


# ---------------------------------------------------------------------------
# Interior views
# ---------------------------------------------------------------------------


class TestInteriorRegistration:
    def _area_prior(self):
        from vision.rooms import area_plausibility

        return area_plausibility

    def test_the_drawing_decides_which_room_a_photo_shows(self):
        pytest.importorskip("vision.rooms")
        # Two candidate rooms. Floor area alone favours the larger one for a
        # bedroom, but the drawing names the smaller one BEDROOM.
        regions = [
            FakeRegion("room_big", area=26.0, room_type="living_room",
                       room_type_confidence=0.9),
            FakeRegion("room_small", area=13.0, room_type="bedroom",
                       room_type_confidence=0.9),
        ]

        results = reg.register_interior_views(
            ["bedroom"], regions, area_plausibility=self._area_prior()
        )

        assert results["bedroom"].room_id == "room_small"
        assert results["bedroom"].method == "cad_room_type"
        assert not results["bedroom"].conflicts_with_cad
        assert results["bedroom"].confidence > 0.8

    def test_area_decides_only_when_the_drawing_is_silent(self):
        pytest.importorskip("vision.rooms")
        regions = [
            FakeRegion("room_a", area=26.0),
            FakeRegion("room_b", area=5.0),
        ]

        results = reg.register_interior_views(
            ["living_room"], regions, area_plausibility=self._area_prior()
        )

        assert results["living_room"].room_id == "room_a"
        assert results["living_room"].method == "area_plausibility"

    def test_a_disagreement_is_recorded_not_hidden(self):
        pytest.importorskip("vision.rooms")
        # Only one room exists, and the drawing says it is a kitchen. The
        # bathroom imagery has to go somewhere, but the conflict is the point.
        regions = [FakeRegion("room_a", area=9.0, room_type="kitchen",
                              room_type_confidence=0.92)]

        results = reg.register_interior_views(
            ["bathroom"], regions, area_plausibility=self._area_prior()
        )

        record = results["bathroom"]
        assert record.conflicts_with_cad
        assert record.cad_room_type == "kitchen"
        assert record.confidence < 0.3
        assert any("names this room kitchen" in r for r in record.reasons)

    def test_a_low_confidence_cad_type_is_a_hint_not_a_statement(self):
        pytest.importorskip("vision.rooms")
        regions = [
            FakeRegion("room_a", area=26.0, room_type="bedroom",
                       room_type_confidence=0.2),
            FakeRegion("room_b", area=13.0),
        ]

        results = reg.register_interior_views(
            ["living_room"], regions, area_plausibility=self._area_prior()
        )
        # 26 m2 suits a living room and the weak CAD hint does not veto it.
        assert results["living_room"].room_id == "room_a"

    def test_each_room_type_gets_a_distinct_region(self):
        pytest.importorskip("vision.rooms")
        regions = [
            FakeRegion("r1", area=28.0, room_type="living_room", room_type_confidence=0.9),
            FakeRegion("r2", area=14.0, room_type="bedroom", room_type_confidence=0.9),
            FakeRegion("r3", area=5.0, room_type="bathroom", room_type_confidence=0.9),
        ]

        results = reg.register_interior_views(
            ["bedroom", "living_room", "bathroom"], regions,
            area_plausibility=self._area_prior(),
        )

        assigned = {r.room_id for r in results.values()}
        assert assigned == {"r1", "r2", "r3"}
        assert results["bedroom"].room_id == "r2"
        assert results["living_room"].room_id == "r1"
        assert results["bathroom"].room_id == "r3"

    def test_an_unplaceable_room_type_is_reported(self):
        results = reg.register_interior_views(["bedroom"], [])
        assert results == {}


# ---------------------------------------------------------------------------
# Against the real drawing
# ---------------------------------------------------------------------------


class TestAgainstTheApartmentFixture:
    """The committed apartment DXF, registered against a synthesised sheet."""

    @pytest.fixture(scope="class")
    def document(self):
        if not os.path.exists(FIXTURE):
            pytest.skip(f"fixture not generated: {FIXTURE}")
        pytest.importorskip("ezdxf")
        from cad import read_dxf

        return read_dxf(FIXTURE, log=lambda *a, **k: None)

    def test_the_drawing_yields_usable_anchors(self, document):
        anchors = reg.anchors_from_cad(document)
        names = {a.match_key for a in anchors}
        assert len(anchors) >= 5
        # The fixture writes these as "KITCHEN\P12.00 SQ.M." — the anchor key
        # has to be the name a sheet would print.
        assert "KITCHEN" in names
        assert "MASTER BED" in names
        assert "BEDROOM 2" in names

    def test_the_title_block_is_not_offered_as_an_anchor(self, document):
        # The fixture deliberately places its title block far outside the
        # building. Matching one of its strings would fit the transform to
        # the sheet's layout instead of the drawing's.
        names = {a.match_key for a in reg.anchors_from_cad(document)}
        assert "GROUND FLOOR PLAN" not in names
        assert "DRAWN BY ARCHX" not in names
        assert "ALL DIMENSIONS IN MM" not in names

    def test_dimensions_are_not_offered_as_anchors(self, document):
        # A dimension moves with the drawing but is printed in a dozen
        # near-identical numeric forms; it adds ambiguity and no information.
        anchors = reg.anchors_from_cad(document)
        assert all(a.informative for a in anchors)
        assert not any(a.match_key.replace(" ", "").isdigit() for a in anchors)

    def test_a_sheet_of_this_plan_registers_to_it(self, document):
        want = PlanTransform.from_similarity(18.0, 0.0, -2.0, 12.0)
        inverse = want.inverse()

        labels = []
        for index, text in enumerate(document.room_labels()):
            u, v = inverse.apply(*text.insert)
            labels.append(FakeLabel(
                local_id=f"L{index}", text=text.text,
                bbox=FakeBox(u - 0.03, v - 0.015, u + 0.03, v + 0.015),
            ))
        assert len(labels) >= 5, "fixture should carry room labels"

        result = reg.register_plan_view(
            document, FakeObservation(labels=labels),
            document.bounds_min, document.bounds_max,
        )

        assert result.registered, result.reason
        assert result.method == Method.LABEL_CONSENSUS
        assert result.residual_mean_m < 0.25
        assert_transforms_close(result.transform, want, tol=0.05)

    def test_a_sheet_printing_only_the_room_names_registers(self, document):
        """The drawing writes ``KITCHEN\\P12.00 SQ.M.``; the sheet prints
        ``KITCHEN``. Both must reduce to the same key."""
        want = PlanTransform.from_similarity(18.0, 0.0, -2.0, 12.0)
        inverse = want.inverse()

        labels = []
        for index, text in enumerate(document.room_labels()):
            u, v = inverse.apply(*text.insert)
            bare = labels_mod.strip_measurements(labels_mod.normalise(text.text))
            labels.append(FakeLabel(
                local_id=f"L{index}", text=bare,
                bbox=FakeBox(u - 0.03, v - 0.015, u + 0.03, v + 0.015),
            ))

        result = reg.register_plan_view(
            document, FakeObservation(labels=labels),
            document.bounds_min, document.bounds_max,
        )

        assert result.registered, result.reason
        assert result.residual_mean_m < 0.25
        assert_transforms_close(result.transform, want, tol=0.05)

    def test_registration_survives_a_partly_illegible_sheet(self, document):
        """Half the labels misread or missing, and it still registers."""
        want = PlanTransform.from_similarity(18.0, 0.0, -2.0, 12.0)
        inverse = want.inverse()

        room_labels = list(document.room_labels())
        labels = []
        for index, text in enumerate(room_labels):
            if index % 2:
                continue  # unreadable at sheet resolution
            u, v = inverse.apply(*text.insert)
            garbled = text.text[:-1] + "Q" if len(text.text) > 4 else text.text
            labels.append(FakeLabel(
                local_id=f"L{index}", text=garbled,
                bbox=FakeBox(u - 0.03, v - 0.015, u + 0.03, v + 0.015),
            ))

        result = reg.register_plan_view(
            document, FakeObservation(labels=labels),
            document.bounds_min, document.bounds_max,
        )

        assert result.registered, result.reason
        assert_transforms_close(result.transform, want, tol=0.3)


# ---------------------------------------------------------------------------
# Through the pipeline
# ---------------------------------------------------------------------------


class TestPlanViewsThroughThePipeline:
    """The reported failure, end to end.

    A composite sheet — an exterior render across the top, the plan below —
    used to lose every detection: the whole image was stretched onto the
    plan's bounding box, so everything landed outside every room and was
    dropped one at a time in silence.
    """

    @pytest.fixture
    def setup(self):
        pytest.importorskip("scipy", reason="room segmentation needs scipy")
        from vision import rooms as room_seg
        from vision.pipeline import PipelineConfig, _build_walls

        geometry = {
            "walls": [
                {"start": [0.0, 0.0], "end": [10.0, 0.0]},
                {"start": [10.0, 0.0], "end": [10.0, 6.0]},
                {"start": [10.0, 6.0], "end": [0.0, 6.0]},
                {"start": [0.0, 6.0], "end": [0.0, 0.0]},
                {"start": [5.0, 0.0], "end": [5.0, 2.5]},
                {"start": [5.0, 3.4], "end": [5.0, 6.0]},
            ]
        }
        segmented = room_seg.segment_rooms(geometry["walls"], wall_thickness=0.15)
        assert segmented.ok
        return {
            "regions": segmented.regions,
            "walls": _build_walls(geometry, 3.0, 0.15),
            "config": PipelineConfig(),
            "min": (0.0, 0.0),
            "max": (10.0, 6.0),
        }

    def _document(self):
        """Two labelled rooms, at the centroids of the two segmented halves."""
        return FakeDocument(texts=[
            FakeText("c0", "LIVING ROOM", "LIVING ROOM", (2.5, 3.0),
                     room_type="living_room"),
            FakeText("c1", "KITCHEN", "KITCHEN", (7.5, 3.0), room_type="kitchen"),
        ])

    def _sheet(self, transform, sofa_plan_xy):
        """A plan-view observation with labels and one sofa, built by inverting
        ``transform`` — so the correct answer is known by construction."""
        from vision import observe

        inverse = transform.inverse()
        document = self._document()

        labels = []
        for index, text in enumerate(document.texts):
            u, v = inverse.apply(*text.insert)
            labels.append({
                "id": f"L{index}", "text": text.text,
                "bbox": [u - 0.02, v - 0.01, u + 0.02, v + 0.01],
                "confidence": 0.9,
            })

        su, sv = inverse.apply(*sofa_plan_xy)
        payload = {
            "room": {"room_type": "unknown", "confidence": 0.3},
            "objects": [{
                "id": "s1", "category": "sofa", "label": "grey sofa",
                "bbox": [su - 0.03, sv - 0.02, su + 0.03, sv + 0.02],
                "size_bucket": "medium", "support": "floor",
                "material": "fabric", "color_hex": "#8A8A8A", "confidence": 0.9,
            }],
            "labels": labels,
        }
        return observe.parse_observation(payload, "img0", "sheet.png",
                                         analysis_mode="layout")

    def _solve(self, setup, observation, document):
        from vision.pipeline import _solve_plan_views

        return _solve_plan_views(
            [observation], setup["regions"], setup["walls"],
            setup["min"], setup["max"], setup["config"],
            log=lambda *a, **k: None, document=document,
        )

    def test_a_composite_sheet_now_places_its_furniture(self, setup):
        # The plan occupies the bottom-left quarter of the sheet. Under the
        # old full-frame assumption the sofa mapped to roughly (1.4, 4.4) —
        # the wrong room — or outside the plan entirely.
        transform = PlanTransform.from_similarity(
            scale=40.0, rotation_deg=0.0, tx=-2.0, ty=14.0
        )
        sofa_at = (7.0, 1.5)          # in the kitchen half
        observation = self._sheet(transform, sofa_at)

        objects, diagnostics = self._solve(setup, observation, self._document())

        assert len(objects) == 1, "the sofa must survive a composite sheet"
        sofa = objects[0]
        assert sofa.position.x == pytest.approx(sofa_at[0], abs=0.4)
        assert sofa.position.y == pytest.approx(sofa_at[1], abs=0.4)
        assert "plan_registration_assumed" not in sofa.flags

        assert diagnostics and diagnostics[0]["registered"] is True
        assert diagnostics[0]["sheet_region"]["looks_composite"] is True

    def test_the_object_lands_in_the_room_the_plan_puts_it_in(self, setup):
        transform = PlanTransform.from_similarity(40.0, 0.0, -2.0, 14.0)
        observation = self._sheet(transform, (7.0, 1.5))

        objects, _ = self._solve(setup, observation, self._document())

        kitchen = next(r for r in setup["regions"] if r.centroid[0] > 5.0)
        assert objects[0].room_id == kitchen.id

    def test_without_labels_it_falls_back_and_flags_every_placement(self, setup):
        # Same sheet, but the model reported no labels — a cached response
        # predating the prompt change, or a plan whose text is illegible.
        transform = PlanTransform.from_similarity(40.0, 0.0, -2.0, 14.0)
        observation = self._sheet(transform, (7.0, 1.5))
        observation.labels.clear()

        objects, diagnostics = self._solve(setup, observation, self._document())

        assert diagnostics[0]["registered"] is False
        assert diagnostics[0]["method"] == Method.PLAN_BOUNDS
        for obj in objects:
            assert "plan_registration_assumed" in obj.flags

    def test_a_full_frame_plan_registers_and_is_not_called_composite(self, setup):
        """The common case: one plan, drawn to fill the sheet."""
        transform = PlanTransform.from_similarity(10.0, 0.0, 0.0, 6.0)
        observation = self._sheet(transform, (2.5, 4.0))

        objects, diagnostics = self._solve(setup, observation, self._document())

        assert len(objects) == 1
        assert objects[0].position.x == pytest.approx(2.5, abs=0.4)
        assert objects[0].position.y == pytest.approx(4.0, abs=0.4)
        assert diagnostics[0]["registered"] is True
        assert diagnostics[0]["sheet_region"]["looks_composite"] is False

    def test_the_legacy_stretch_is_corrected_where_it_was_wrong(self, setup):
        """A registered plan does not inherit the fallback's distortion.

        The old map scaled x by the building's width and y by its depth
        independently, so unless the image happened to share the plan's aspect
        ratio it stretched everything along one axis. A drawing is never
        stretched, so the fitted similarity disagrees — and where they
        disagree, the fit is the one that is right.
        """
        stretch = PlanTransform.stretch_to_bounds(setup["min"], setup["max"])
        assert not stretch.is_similarity

        observation = self._sheet(stretch, (2.5, 4.0))
        objects, diagnostics = self._solve(setup, observation, self._document())

        assert diagnostics[0]["registered"] is True
        assert PlanTransform.from_dict(diagnostics[0]["transform"]).is_similarity
        # x is unaffected: the labels pin the horizontal scale directly.
        assert objects[0].position.x == pytest.approx(2.5, abs=0.4)
        # y differs, because a uniform scale is applied where the legacy map
        # applied a 10:6 stretch. That difference *is* the correction.
        assert objects[0].position.y != pytest.approx(4.0, abs=0.4)

    def test_no_cad_document_still_produces_a_model(self, setup):
        # A geometry.json from the legacy extractor carries no CAD model.
        transform = PlanTransform.stretch_to_bounds(setup["min"], setup["max"])
        observation = self._sheet(transform, (2.5, 4.0))

        objects, diagnostics = self._solve(setup, observation, None)

        assert diagnostics[0]["registered"] is False
        assert len(objects) == 1


class TestLabelParsing:
    def test_a_plan_view_keeps_its_labels(self):
        from vision import observe

        obs = observe.parse_observation(
            {"labels": [{"id": "L0", "text": "MASTER BEDROOM",
                         "bbox": [0.2, 0.3, 0.4, 0.35], "confidence": 0.9}]},
            "img0", "plan.png", analysis_mode="layout")

        assert len(obs.labels) == 1
        assert obs.labels[0].text == "MASTER BEDROOM"
        assert obs.labels[0].bbox.center == pytest.approx((0.3, 0.325))

    def test_an_interior_photo_drops_its_labels(self):
        # Text on a book spine locates nothing, and offering it to the
        # registration engine would propose correspondences that cannot be real.
        from vision import observe

        obs = observe.parse_observation(
            {"labels": [{"id": "L0", "text": "PENGUIN CLASSICS",
                         "bbox": [0.2, 0.3, 0.4, 0.35], "confidence": 0.9}]},
            "img0", "room.jpg", analysis_mode="full")

        assert obs.labels == []

    def test_a_label_without_a_box_is_rejected(self):
        from vision import observe

        obs = observe.parse_observation(
            {"labels": [{"id": "L0", "text": "KITCHEN"}]},
            "img0", "plan.png", analysis_mode="layout")

        assert obs.labels == []
        assert obs.rejected.get("label_without_bbox") == 1

    def test_a_response_with_no_labels_is_not_an_error(self):
        from vision import observe

        obs = observe.parse_observation({}, "img0", "plan.png",
                                        analysis_mode="layout")
        assert obs.labels == []


class TestTilingPreservesLabels:
    """Tiling and registration must compose.

    Tiling triggers on large, dense sheets — exactly the composite-sheet case
    registration exists for. A label left in tile-local coordinates would not
    merely weaken the fit, it would corrupt it: a room name at a confidently
    wrong position is worse input than no label at all.
    """

    def _tiles(self):
        from vision.tiling import Tile

        # Two tiles side by side, each covering half the width.
        return [
            Tile(index=0, path="t0.png", rect=(0.0, 0.0, 0.5, 1.0)),
            Tile(index=1, path="t1.png", rect=(0.5, 0.0, 1.0, 1.0)),
        ]

    def test_labels_survive_the_merge(self):
        from vision.tiling import merge_payloads

        left, right = self._tiles()
        merged = merge_payloads([
            (left, {"labels": [{"id": "L0", "text": "KITCHEN",
                                "bbox": [0.2, 0.4, 0.6, 0.5]}]}),
            (right, {"labels": [{"id": "L1", "text": "BATH",
                                 "bbox": [0.2, 0.4, 0.6, 0.5]}]}),
        ])

        assert len(merged["labels"]) == 2
        assert {entry["text"] for entry in merged["labels"]} == {"KITCHEN", "BATH"}

    def test_label_boxes_are_remapped_to_whole_image_space(self):
        from vision.tiling import merge_payloads

        left, right = self._tiles()
        merged = merge_payloads([
            (left, {"labels": [{"id": "L0", "text": "KITCHEN",
                                "bbox": [0.0, 0.4, 1.0, 0.5]}]}),
            (right, {"labels": [{"id": "L1", "text": "BATH",
                                 "bbox": [0.0, 0.4, 1.0, 0.5]}]}),
        ])

        by_text = {entry["text"]: entry["bbox"] for entry in merged["labels"]}
        # A box spanning its own tile spans the corresponding half of the sheet.
        assert by_text["KITCHEN"][0] == pytest.approx(0.0)
        assert by_text["KITCHEN"][2] == pytest.approx(0.5)
        assert by_text["BATH"][0] == pytest.approx(0.5)
        assert by_text["BATH"][2] == pytest.approx(1.0)

    def test_a_label_seen_in_two_tiles_is_merged(self):
        from vision.tiling import merge_payloads

        # Overlapping tiles, both reading the same label at the same place.
        from vision.tiling import Tile

        a = Tile(index=0, path="t0.png", rect=(0.0, 0.0, 0.6, 1.0))
        b = Tile(index=1, path="t1.png", rect=(0.4, 0.0, 1.0, 1.0))

        merged = merge_payloads([
            (a, {"labels": [{"id": "L0", "text": "KITCHEN", "confidence": 0.9,
                             "bbox": [0.75, 0.4, 0.92, 0.46]}]}),
            (b, {"labels": [{"id": "L1", "text": "KITCHEN", "confidence": 0.7,
                             "bbox": [0.083, 0.4, 0.367, 0.46]}]}),
        ])

        assert len(merged["labels"]) == 1
        assert merged["labels"][0]["confidence"] == 0.9

    def test_a_label_without_a_box_is_dropped(self):
        from vision.tiling import merge_payloads

        left, _ = self._tiles()
        merged = merge_payloads([(left, {"labels": [{"id": "L0", "text": "KITCHEN"}]})])
        assert merged["labels"] == []

    def test_a_tiled_sheet_still_registers(self):
        """The end of the chain: tiles in, a fitted transform out."""
        from vision import observe
        from vision.tiling import Tile, merge_payloads

        want = PlanTransform.from_similarity(14.0, 0.0, -1.0, 8.5)
        inverse = want.inverse()
        document = plan_document()

        # Split the sheet down the middle and hand each label to whichever
        # tile contains it, in that tile's local coordinates.
        tiles = [
            Tile(index=0, path="t0.png", rect=(0.0, 0.0, 0.5, 1.0)),
            Tile(index=1, path="t1.png", rect=(0.5, 0.0, 1.0, 1.0)),
        ]
        payloads = {0: [], 1: []}
        for index, text in enumerate(document.texts):
            u, v = inverse.apply(*text.insert)
            which = 0 if u < 0.5 else 1
            tile = tiles[which]
            local_u = (u - tile.rect[0]) / tile.width
            payloads[which].append({
                "id": f"L{index}", "text": text.text, "confidence": 0.9,
                "bbox": [local_u - 0.02, v - 0.01, local_u + 0.02, v + 0.01],
            })

        assert payloads[0] and payloads[1], "labels should land in both tiles"

        merged = merge_payloads([
            (tiles[0], {"labels": payloads[0]}),
            (tiles[1], {"labels": payloads[1]}),
        ])
        observation = observe.parse_observation(
            merged, "img0", "sheet.png", analysis_mode="layout"
        )

        result = reg.register_plan_view(
            document, observation, BOUNDS_MIN, BOUNDS_MAX
        )

        assert result.registered, result.reason
        assert_transforms_close(result.transform, want, tol=0.05)


class TestTheReviewSurfaceReportsRegistration:
    """A placement's trustworthiness is a property of the registration.

    Nothing about an object shows that the sheet it came from was never
    aligned to the drawing, so the reviewer has to be told separately — or
    they cannot tell a placement worth nudging from one worth discarding.
    """

    def _diagnostics(self, **record):
        base = {
            "image_id": "img0", "registered": True, "method": Method.LABEL_CONSENSUS,
            "sheet_region": {"coverage": 0.9, "looks_composite": False},
            "unmatched_image_labels": [],
        }
        base.update(record)
        return {"registration": {"plan_views": [base], "registered": 1, "total": 1}}

    def test_a_clean_registration_warns_about_nothing(self):
        from vision.review import _registration_warnings

        assert _registration_warnings(self._diagnostics()) == []

    def test_an_unregistered_sheet_is_surfaced(self):
        from vision.review import _registration_warnings

        warnings = _registration_warnings(
            self._diagnostics(registered=False, method=Method.PLAN_BOUNDS)
        )
        assert len(warnings) == 1
        assert "could not be aligned" in warnings[0]

    def test_a_composite_sheet_is_surfaced_even_when_it_registered(self):
        from vision.review import _registration_warnings

        warnings = _registration_warnings(self._diagnostics(
            sheet_region={"coverage": 0.18, "looks_composite": True}
        ))
        assert any("occupies only 18%" in w for w in warnings)

    def test_a_multi_floor_sheet_is_surfaced(self):
        from vision.review import _registration_warnings

        warnings = _registration_warnings(self._diagnostics(
            unmatched_image_labels=["GUEST BED", "STUDY", "TERRACE"]
        ))
        assert any("more than one floor" in w for w in warnings)

    def test_a_project_with_no_plan_views_is_silent(self):
        from vision.review import _registration_warnings

        assert _registration_warnings({}) == []
        assert _registration_warnings({"registration": {"plan_views": []}}) == []


class TestTheDrawingOutranksTheImagery:
    """A room type the drawing *states* is not overwritten by a model's guess.

    CAD text is tier 4; a vision impression is tier 6. Assignment used to
    stamp the image's answer over the drawing's unconditionally, so a room
    labelled STUDY became a bedroom because the photograph of it had a bed in
    it — inverting the trust hierarchy at the point it matters most.
    """

    @pytest.fixture
    def regions(self):
        pytest.importorskip("scipy", reason="room segmentation needs scipy")
        from vision import rooms as room_seg

        result = room_seg.segment_rooms([
            {"start": [0.0, 0.0], "end": [10.0, 0.0]},
            {"start": [10.0, 0.0], "end": [10.0, 6.0]},
            {"start": [10.0, 6.0], "end": [0.0, 6.0]},
            {"start": [0.0, 6.0], "end": [0.0, 0.0]},
            {"start": [5.0, 0.0], "end": [5.0, 2.5]},
            {"start": [5.0, 3.4], "end": [5.0, 6.0]},
        ], wall_thickness=0.15)
        assert result.ok
        return result.regions

    def _assign(self, regions, observed_type):
        from vision import assignment, classify, observe

        payload = {
            "room": {"room_type": observed_type, "style": "modern",
                     "confidence": 0.92},
            "objects": [{"id": "b1", "category": "bed", "label": "double bed",
                         "bbox": [0.2, 0.4, 0.7, 0.9], "size_bucket": "large",
                         "support": "floor", "confidence": 0.95}],
        }
        obs = observe.parse_observation(payload, "img0", "room.jpg")
        return assignment.assign([obs], {"img0": classify.ImageProfile("img0", "room.jpg")},
                                 regions)

    def test_a_stated_room_type_survives_a_contradicting_photo(self, regions):
        target = regions[0]
        target.room_type = "office"
        target.room_type_confidence = 0.88

        result = self._assign(regions, "bedroom")

        assert target.room_type == "office", "the drawing's answer must stand"
        group = next(g for g in result.groups if g.region.id == target.id)
        if group.has_imagery:
            assert group.room_type == "office"
            assert group.observed_room_type == "bedroom"

    def test_the_disagreement_is_reported(self, regions):
        for region in regions:
            region.room_type = "office"
            region.room_type_confidence = 0.88

        result = self._assign(regions, "bedroom")

        assert any("drawing names it" in w for w in result.warnings), result.warnings

    def test_agreement_raises_confidence_above_either_alone(self, regions):
        target = regions[0]
        target.room_type = "bedroom"
        target.room_type_confidence = 0.7

        self._assign(regions, "bedroom")

        assert target.room_type == "bedroom"
        assert target.room_type_confidence > 0.7

    def test_a_silent_drawing_lets_the_imagery_answer(self, regions):
        for region in regions:
            region.room_type = "unknown"
            region.room_type_confidence = 0.0

        result = self._assign(regions, "bedroom")

        assert any(g.room_type == "bedroom" for g in result.groups)
