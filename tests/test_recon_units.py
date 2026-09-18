"""Unit resolution: the decision that broke the failing plan.

The plan that motivated the rewrite declares millimetres and is drawn in
inches. The old resolver believed the header, produced a building 0.8 m
across, and every stage after it worked on a shoebox. These tests pin the
behaviour that replaced it: the header is evidence, the geometry is better
evidence, and the disagreement is reported rather than hidden.
"""

from __future__ import annotations

import math

import pytest

from modules.recon import units as U


def rect_walls(width, height, thickness, *, scale=1.0):
    """A closed rectangle drawn the way a wall is: two parallel faces."""
    w, h, t = width * scale, height * scale, thickness * scale
    segs = []
    for off in (0.0, t):
        segs += [
            ((off, off), (w - off, off)),
            ((w - off, off), (w - off, h - off)),
            ((w - off, h - off), (off, h - off)),
            ((off, h - off), (off, off)),
        ]
    return segs


class TestEvidenceBeatsHeader:
    def test_inches_drawing_declaring_millimetres(self):
        """A 40 x 28 ft house in inches, with $INSUNITS lying about mm."""
        segs = rect_walls(480, 336, 4.0)      # inches: 40 ft x 28 ft, 4" walls
        d = U.resolve(segs, insunits=4, dimensions=[480.0, 336.0, 96.0])
        assert d.unit_name == "inches"
        assert d.scale_to_m == pytest.approx(0.0254)
        assert d.conflict, "a disagreement with $INSUNITS must be reported"
        assert "millimetres" in d.conflict and "inches" in d.conflict

    def test_the_header_is_kept_when_geometry_agrees(self):
        segs = rect_walls(12000, 8000, 150.0)     # millimetres
        d = U.resolve(segs, insunits=4, dimensions=[12000.0, 8000.0])
        assert d.unit_name == "millimetres"
        assert d.conflict is None
        assert d.method == "evidence+header"

    def test_a_thin_drawing_does_not_overturn_the_header(self):
        """Weak geometry must not outvote an explicit declaration.

        Six incidental parallel distances are not a wall thickness. Letting
        them decide is how a 14 m flat came out 140 m across.
        """
        segs = [((0, 0), (14000, 0)), ((0, 300), (14000, 300)),
                ((0, 0), (0, 7600)), ((300, 0), (300, 7600))]
        d = U.resolve(segs, insunits=4)
        assert d.unit_name == "millimetres"


class TestEveryUnit:
    @pytest.mark.parametrize("name,scale,code", [
        ("millimetres", 0.001, 4),
        ("centimetres", 0.01, 5),
        ("metres", 1.0, 6),
        ("inches", 0.0254, 1),
        ("feet", 0.3048, 2),
    ])
    def test_round_trip(self, name, scale, code):
        """A 12 x 8 m building with 200 mm walls, drawn in each unit."""
        per_unit = 1.0 / scale
        segs = rect_walls(12 * per_unit, 8 * per_unit, 0.2 * per_unit)
        d = U.resolve(segs, insunits=code)
        assert d.unit_name == name
        assert d.scale_to_m == pytest.approx(scale)

    @pytest.mark.parametrize("scale,code", [
        (0.001, 4), (0.01, 5), (1.0, 6), (0.0254, 1), (0.3048, 2)])
    def test_resolved_without_any_header(self, scale, code):
        per_unit = 1.0 / scale
        segs = rect_walls(12 * per_unit, 8 * per_unit, 0.2 * per_unit)
        d = U.resolve(segs, insunits=None)
        assert d.scale_to_m == pytest.approx(scale), d.reason


class TestRefusal:
    def test_no_plausible_unit_is_reported_not_guessed(self):
        """A drawing 40 km across is not a floor plan in any unit."""
        segs = [((0, 0), (40000000, 0)), ((0, 500), (40000000, 500))]
        d = U.resolve(segs, insunits=None)
        assert d.confidence <= 0.3
        assert d.conflict


class TestUserOverride:
    def test_explicit_scale_wins_outright(self):
        segs = rect_walls(480, 336, 4.0)
        d = U.resolve(segs, insunits=4, user_scale=0.5)
        assert d.scale_to_m == 0.5
        assert d.method == "user"


class TestPairSearch:
    def test_finds_the_thickness_of_overlapping_parallel_lines(self):
        segs = [((0, 0), (10, 0)), ((0, 0.15), (10, 0.15))]
        assert U.parallel_pair_distances(segs) == pytest.approx([0.15])

    def test_ignores_lines_that_barely_overlap(self):
        """A dimension line beside its extension line is not a wall."""
        segs = [((0, 0), (10, 0)), ((9.8, 0.15), (12, 0.15))]
        assert U.parallel_pair_distances(segs) == []

    def test_bounded_scan_finds_the_same_pairs_as_an_exhaustive_one(self):
        """The offset-sorted scan is an optimisation, not a change of answer."""
        segs = []
        for i in range(60):
            y = i * 0.37
            segs.append(((0.0, y), (8.0, y)))
            segs.append(((0.0, y + 0.15), (8.0, y + 0.15)))
        fast = sorted(U.parallel_pair_distances(segs))
        slow = sorted(_exhaustive_pairs(segs))
        assert fast == pytest.approx(slow)


def _exhaustive_pairs(segments, angle_tol_deg=2.0):
    """The O(n^2) reference implementation, kept only to check the fast one."""
    tol = math.radians(angle_tol_deg)
    out = []
    for i in range(len(segments)):
        (x1, y1), (x2, y2) = segments[i]
        a1 = math.atan2(y2 - y1, x2 - x1) % math.pi
        u = (math.cos(a1), math.sin(a1))
        n = (-math.sin(a1), math.cos(a1))
        t1 = sorted(((x1 * u[0] + y1 * u[1]), (x2 * u[0] + y2 * u[1])))
        o1 = x1 * n[0] + y1 * n[1]
        for j in range(i + 1, len(segments)):
            (x3, y3), (x4, y4) = segments[j]
            a2 = math.atan2(y4 - y3, x4 - x3) % math.pi
            delta = abs(a1 - a2)
            if min(delta, math.pi - delta) > tol:
                continue
            o2 = (x3 * n[0] + y3 * n[1] + x4 * n[0] + y4 * n[1]) / 2.0
            dist = abs(o2 - o1)
            if dist <= 1e-9:
                continue
            t2 = sorted(((x3 * u[0] + y3 * u[1]), (x4 * u[0] + y4 * u[1])))
            overlap = min(t1[1], t2[1]) - max(t1[0], t2[0])
            if overlap <= 0 or overlap < dist * 1.5:
                continue
            out.append(dist)
    return out


class TestModalThickness:
    def test_reports_the_thicknesses_actually_present(self):
        pairs = [0.102] * 40 + [0.152] * 25 + [0.9, 1.4]
        modes = U.modal_thickness(pairs, 1.0)
        assert modes[0] == pytest.approx(0.102, abs=0.002)
        assert modes[1] == pytest.approx(0.152, abs=0.002)
        assert all(m <= 0.5 for m in modes), "a 1.4 m gap is not a wall"
