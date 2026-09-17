"""
ArchX3D — Unit resolution by evidence, not by declaration
=========================================================
Decides what one drawing unit means in metres.

Why this module exists in the form it does
------------------------------------------
The plan that motivated this rewrite declares ``$INSUNITS = 4`` — millimetres.
It is drawn in inches. The old resolver treated the header as authoritative at
confidence 0.97, noticed afterwards that the resulting building was 0.8 m
across, wrote a log line saying so, and kept the answer anyway. Every stage
downstream then operated on a house the size of a shoebox, and the 3 m wall
height from the config turned it into the "giant distorted slabs" the user saw.

So the rule here is: **the header is a claim, and claims are falsifiable.**

The decisive evidence is wall thickness. Buildings are not self-similar — a
wall is between roughly 60 mm and 500 mm thick no matter how large the
building is, and the common thicknesses are a short list of conventions
(100/150/200/230/300 mm; 4/6/8 in). Measuring the perpendicular distance
between overlapping parallel line pairs gives a distribution whose mode is the
wall thickness *in drawing units*. Testing that mode against the conventions
under each candidate unit picks the unit out unambiguously:

    floorplan.dxf modal pair distances: 4 and 6 drawing units
        as millimetres -> 4 mm and 6 mm walls   absurd
        as feet        -> 1.2 m and 1.8 m walls absurd
        as inches      -> 102 mm and 152 mm     exactly 2x4 and 2x6 framing

No other unit is close, and the header's claim loses to the drawing's own
geometry. That is the whole idea.

Evidence is ranked, scored and *all of it is recorded* — :class:`UnitDecision`
keeps every candidate's score so a wrong answer can be explained. When the
winner disagrees with ``$INSUNITS`` the disagreement is reported as a conflict
rather than resolved silently.
"""

from __future__ import annotations

import collections
import math
from typing import Dict, List, Optional, Sequence, Tuple

from .ir import UnitDecision

XY = Tuple[float, float]
Segment = Tuple[XY, XY]

#: ``$INSUNITS`` code -> (metres per unit, human name).
INSUNITS: Dict[int, Tuple[float, str]] = {
    1: (0.0254, "inches"),
    2: (0.3048, "feet"),
    3: (1609.344, "miles"),
    4: (0.001, "millimetres"),
    5: (0.01, "centimetres"),
    6: (1.0, "metres"),
    7: (1000.0, "kilometres"),
    8: (2.54e-8, "microinches"),
    9: (2.54e-5, "mils"),
    10: (0.9144, "yards"),
    11: (1e-10, "angstroms"),
    12: (1e-9, "nanometres"),
    13: (1e-6, "microns"),
    14: (0.1, "decimetres"),
    15: (10.0, "decametres"),
    16: (100.0, "hectometres"),
    17: (1e9, "gigametres"),
    20: (1.495978707e11, "astronomical units"),
}

#: The units an architectural drawing is realistically authored in. Exotic
#: ``$INSUNITS`` codes are still honoured if declared, but they are never
#: *guessed* — nobody draws a house in angstroms, and including them as
#: candidates only creates ways to be wrong.
CANDIDATES: List[Tuple[float, str]] = [
    (0.001, "millimetres"),
    (0.01, "centimetres"),
    (1.0, "metres"),
    (0.0254, "inches"),
    (0.3048, "feet"),
]

#: A wall is this thick, in metres. Outside this band a measurement is not a
#: wall thickness, whatever else it might be.
WALL_BAND = (0.06, 0.50)

#: Wall thicknesses that recur across building traditions, in metres:
#: timber framing (89/140 mm studs + linings), brick (115/230 mm), block
#: (100/150/200 mm), and the imperial nominal equivalents.
CONVENTIONS_M = (
    0.075, 0.089, 0.100, 0.102, 0.115, 0.125, 0.140, 0.150, 0.152,
    0.178, 0.200, 0.203, 0.229, 0.230, 0.250, 0.254, 0.300, 0.305,
    0.343, 0.350, 0.400,
)
CONVENTION_TOL = 0.013   # metres — half an inch of slack

#: Wall-thickness evidence counts in full once this many parallel pairs
#: support it; fewer pairs count proportionally less.
THICKNESS_SUPPORT = 6

#: A floor plan spans at least a small room and at most a large campus block.
#: Anything outside this after conversion means the conversion is wrong.
PLAN_BAND_M = (3.0, 400.0)

#: The size most floor plates actually are, across, in metres. Preference
#: tapers smoothly outside this rather than stopping at an edge: a flat
#: in-band bonus made a 10 m plan and a 121 m plan score identically on size,
#: and a 4-inch wall read as feet is a 12-inch wall, which is also a
#: convention — so the two readings tied and the wrong one won by 0.015.
PLAN_TYPICAL_M = (7.0, 45.0)
PLAN_SWEET_M = PLAN_TYPICAL_M          # retained name for existing callers


def _extent_score(extent_m: float) -> float:
    """How typical a plan of this size is, in [0, 1.5].

    Flat across the usual range and decaying logarithmically outside it, so
    that being an order of magnitude too large costs real score while a large
    but genuine building is not rejected.
    """
    lo, hi = PLAN_TYPICAL_M
    if lo <= extent_m <= hi:
        return 1.5
    if extent_m < lo:
        floor_m = PLAN_BAND_M[0]
        if lo <= floor_m:
            return 1.5
        return 1.5 * max(0.0, (extent_m - floor_m) / (lo - floor_m))
    ceil_m = PLAN_BAND_M[1]
    if ceil_m <= hi:
        return 1.5
    return 1.5 * max(0.0, 1.0 - math.log(extent_m / hi) / math.log(ceil_m / hi))


# ---------------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------------

def parallel_pair_distances(
    segments: Sequence[Segment],
    angle_tol_deg: float = 2.0,
    max_pairs: int = 200000,
    max_span: Optional[float] = None,
) -> List[float]:
    """Perpendicular distances between overlapping near-parallel segment pairs.

    These are wall-thickness candidates *in drawing units*. The overlap test
    (``overlap > 1.5 x separation``) is what keeps this honest: two parallel
    lines that barely overlap are far more likely to be a dimension line beside
    an extension line than the two faces of a wall.

    Segments are bucketed by direction so this is close to linear in practice
    on the axis-aligned drawings that dominate architecture, rather than the
    O(n^2) the nested loop suggests.
    """
    tol = math.radians(angle_tol_deg)
    buckets: Dict[int, List[Tuple[float, Segment]]] = collections.defaultdict(list)
    for seg in segments:
        (x1, y1), (x2, y2) = seg
        if math.hypot(x2 - x1, y2 - y1) < 1e-9:
            continue
        angle = math.atan2(y2 - y1, x2 - x1) % math.pi
        buckets[int(angle / tol)].append((angle, seg))

    # The nested loop this replaces was O(n^2) *within* an angle bucket, and an
    # architectural drawing puts nearly every segment into one of two buckets.
    # On a 7,000-segment sheet that was 24 million comparisons and 95 seconds
    # of a 100-second run. Sorting each bucket by perpendicular offset makes
    # the search a bounded forward scan instead: the pair test requires
    # ``overlap >= 1.5 x distance`` and overlap can never exceed the shorter
    # segment, so no partner can be further away than ``len / 1.5`` and the
    # scan may stop there. Nothing is lost — the same pairs are found — and
    # the same sheet now takes under a second.
    out: List[float] = []
    for key in list(buckets):
        # Include the neighbouring bucket so a pair straddling a bucket edge is
        # not missed — the classic off-by-one that makes this kind of binning
        # quietly lossy.
        group = buckets[key] + buckets.get(key + 1, [])
        if len(group) < 2:
            continue
        rows: List[Tuple[float, float, float, float, float]] = []
        for a, (p, q) in group:
            u = (math.cos(a), math.sin(a))
            n = (-math.sin(a), math.cos(a))
            t_a = p[0] * u[0] + p[1] * u[1]
            t_b = q[0] * u[0] + q[1] * u[1]
            off = (p[0] * n[0] + p[1] * n[1] + q[0] * n[0] + q[1] * n[1]) / 2.0
            rows.append((off, min(t_a, t_b), max(t_a, t_b), a,
                         abs(t_b - t_a)))
        rows.sort()
        n_rows = len(rows)
        for i in range(n_rows):
            off1, lo1, hi1, a1, len1 = rows[i]
            window = len1 / 1.5
            if max_span is not None:
                window = min(window, max_span)
            for j in range(i + 1, n_rows):
                off2, lo2, hi2, a2, _len2 = rows[j]
                dist = off2 - off1
                if dist > window:
                    break
                if dist <= 1e-9:
                    continue
                delta = abs(a1 - a2)
                if min(delta, math.pi - delta) > tol:
                    continue
                overlap = min(hi1, hi2) - max(lo1, lo2)
                if overlap <= 0 or overlap < dist * 1.5:
                    continue
                out.append(dist)
                if len(out) >= max_pairs:
                    return out
    return out


def _modal_clusters(values: Sequence[float], rel_tol: float = 0.02,
                    top: int = 6) -> List[Tuple[float, int]]:
    """Cluster values that agree to within ``rel_tol`` and return the biggest.

    A plain histogram with fixed bins splits a mode that straddles a bin edge,
    which on this data is the difference between finding "152 mm x 65" and
    finding two unremarkable bins of 33 each.
    """
    if not values:
        return []
    ordered = sorted(values)
    clusters: List[List[float]] = [[ordered[0]]]
    for v in ordered[1:]:
        ref = clusters[-1][0]
        if abs(v - ref) <= max(rel_tol * ref, 1e-9):
            clusters[-1].append(v)
        else:
            clusters.append([v])
    scored = [(sum(c) / len(c), len(c)) for c in clusters]
    scored.sort(key=lambda t: -t[1])
    return scored[:top]


#: Pairs in one thickness cluster beyond which the drawing is taken to state
#: its own wall thickness emphatically enough to overrule a header on its own.
EVIDENCE_SATURATION = 90


def _evidence_strength(pair_distances: Sequence[float]) -> float:
    """How emphatically the geometry states a wall thickness, in [0, 1].

    Measured by the size of the largest agreeing cluster rather than the raw
    number of parallel pairs. Sixty distances that agree on nothing are not
    evidence of anything; sixty that agree to within 2% are a wall.
    """
    clusters = _modal_clusters(pair_distances, top=1)
    if not clusters:
        return 0.0
    return min(1.0, clusters[0][1] / float(EVIDENCE_SATURATION))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_candidate(
    scale: float,
    name: str,
    pair_distances: Sequence[float],
    extent: float,
    dimensions: Sequence[float],
    swing_radii: Sequence[float] = (),
    text_heights: Sequence[float] = (),
) -> Tuple[float, List[str]]:
    """Score one candidate unit. Returns ``(score, reasons)``; score < 0 rejects."""
    reasons: List[str] = []
    extent_m = extent * scale
    if not (PLAN_BAND_M[0] <= extent_m <= PLAN_BAND_M[1]):
        return -1.0, [
            "rejected: plan would be %.2f m across, outside the plausible %g-%g m"
            % (extent_m, *PLAN_BAND_M)
        ]

    score = 0.0

    # --- wall thickness evidence (the decisive one) ------------------------
    if pair_distances:
        in_band = [d * scale for d in pair_distances
                   if WALL_BAND[0] <= d * scale <= WALL_BAND[1]]
        fraction = len(in_band) / len(pair_distances)
        # A share of one pair is not evidence: two of a hundred random strokes
        # happening to lie 230 mm apart made "100% of pair distances" say feet,
        # and outvoted a declared unit. The score grows with the pairs behind it.
        support = min(1.0, len(pair_distances) / THICKNESS_SUPPORT)
        score += 4.0 * fraction * support
        if in_band:
            modes = _modal_clusters(in_band)
            total = sum(c for _, c in modes) or 1
            conventional = sum(
                c for v, c in modes
                if any(abs(v - k) <= CONVENTION_TOL for k in CONVENTIONS_M)
            )
            score += 4.0 * (conventional / total) * support
            reasons.append(
                "%.0f%% of %d parallel-pair distances are plausible wall thicknesses; "
                "modal %s" % (100 * fraction, len(pair_distances),
                              ", ".join("%.3f m x%d" % (v, c) for v, c in modes[:3]))
            )
        else:
            reasons.append("no parallel-pair distance falls in the wall-thickness band")
    else:
        reasons.append("no parallel line pairs found — wall-thickness evidence unavailable")

    # --- dimension modality ------------------------------------------------
    # Architects dimension in whole working units. A drawing whose DIMENSION
    # measurements are all integers is authored in an integer unit (mm or in),
    # which rules metres in or out on its own.
    if dimensions:
        integral = sum(1 for d in dimensions if abs(d - round(d)) < 0.02) / len(dimensions)
        fractional_unit = scale in (1.0, 0.3048)
        if integral > 0.85 and not fractional_unit:
            score += 1.0
            reasons.append("%.0f%% of dimension measurements are whole numbers, "
                           "consistent with an integer drawing unit" % (100 * integral))
        elif integral < 0.4 and fractional_unit:
            score += 0.5
            reasons.append("dimension measurements are fractional, consistent with %s" % name)

    # --- door leaves -------------------------------------------------------
    # A door swing's radius is the leaf width, and leaves are 0.6-1.2 m wide in
    # every building tradition. Where walls cannot separate two units — an
    # 8-inch wall read as centimetres is an 81 mm partition, also a convention,
    # and a 20 m house read that way is a plausible 8 m one — the doors can:
    # a 900 mm leaf read as centimetres is a 35 cm cat flap.
    if swing_radii and len(swing_radii) >= 2:
        leaf = sorted(swing_radii)[len(swing_radii) // 2] * scale
        if LEAF_BAND_M[0] <= leaf <= LEAF_BAND_M[1]:
            score += 2.5
            reasons.append("median door swing %.2f m is a door leaf" % leaf)
        else:
            score -= 1.5
            reasons.append("median door swing would be %.2f m, not a door leaf" % leaf)

    # --- lettering ------------------------------------------------------------
    # Room names are plotted at a few millimetres on paper, which is 0.1-0.6 m
    # in the model at any architectural scale. Weak, but independent.
    if text_heights and len(text_heights) >= 3:
        h = sorted(text_heights)[len(text_heights) // 2] * scale
        if TEXT_BAND_M[0] <= h <= TEXT_BAND_M[1]:
            score += 0.5
            reasons.append("median text height %.2f m is plan lettering" % h)

    # --- how typical a plan of that size would be --------------------------
    score += _extent_score(extent_m)
    reasons.append("plan would be %.1f m across" % extent_m)
    return score, reasons


#: A door leaf is this wide, in metres.
LEAF_BAND_M = (0.55, 1.3)

#: Plan lettering is this tall in the model, in metres.
TEXT_BAND_M = (0.07, 0.6)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve(
    segments: Sequence[Segment],
    *,
    insunits: Optional[int] = None,
    dimensions: Sequence[float] = (),
    user_scale: Optional[float] = None,
    measurement: Optional[int] = None,
    swing_radii: Sequence[float] = (),
    text_heights: Sequence[float] = (),
) -> UnitDecision:
    """Decide the drawing's unit from all available evidence.

    Args:
        segments: Building-classified segments in raw drawing units. Passing
            *classified* geometry matters — running this over dimension lines
            and text boxes pollutes the thickness distribution.
        insunits: The ``$INSUNITS`` header value, if any.
        dimensions: ``DIMENSION.get_measurement()`` values, in drawing units.
        user_scale: An explicit override. Always wins; the user has told us
            something we cannot derive.
        measurement: The ``$MEASUREMENT`` header (0 imperial, 1 metric). Weak
            evidence, used only to break a tie between two units of the same
            system.
        swing_radii: Radii of quarter arcs that could be door swings, in
            drawing units.
        text_heights: Heights of short text that could be room names, in
            drawing units.

    Returns:
        A :class:`UnitDecision` recording the choice, the confidence, every
        candidate considered, and any conflict with the declared header.
    """
    if user_scale:
        return UnitDecision(
            scale_to_m=float(user_scale),
            unit_name="user-specified",
            method="user",
            confidence=1.0,
            reason="caller supplied an explicit scale of %g m per drawing unit" % user_scale,
            insunits=insunits,
        )

    xs = [v for (p, q) in segments for v in (p[0], q[0])]
    ys = [v for (p, q) in segments for v in (p[1], q[1])]
    if not xs:
        declared = INSUNITS.get(int(insunits)) if insunits else None
        if declared:
            return UnitDecision(
                scale_to_m=declared[0], unit_name=declared[1], method="header-only",
                confidence=0.5, insunits=insunits,
                reason="no geometry to corroborate; $INSUNITS=%s declares %s"
                       % (insunits, declared[1]),
            )
        return UnitDecision(
            scale_to_m=1.0, unit_name="assumed metres", method="default",
            confidence=0.1, insunits=insunits,
            reason="no geometry and no $INSUNITS; assuming metres",
        )

    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    # No wall is a tenth of the building across, whatever the unit turns out to
    # be, so the pair search need never look further than that.
    pair_distances = parallel_pair_distances(segments, max_span=extent * 0.12)

    scored: List[dict] = []
    for scale, name in CANDIDATES:
        score, reasons = _score_candidate(scale, name, pair_distances, extent, dimensions,
                                         swing_radii, text_heights)
        scored.append({
            "unit": name, "scale_to_m": scale,
            "score": round(score, 3), "reasons": reasons,
        })

    declared = INSUNITS.get(int(insunits)) if insunits else None
    if declared:
        # A declared unit outside the candidate list (yards, decimetres) is still
        # scored, so an honest exotic header is not thrown away.
        if not any(abs(c["scale_to_m"] - declared[0]) < 1e-12 for c in scored):
            score, reasons = _score_candidate(
                declared[0], declared[1], pair_distances, extent, dimensions,
                swing_radii, text_heights)
            scored.append({"unit": declared[1], "scale_to_m": declared[0],
                           "score": round(score, 3), "reasons": reasons})
        # The header is real evidence, just not conclusive evidence — and how
        # much it takes to overturn it depends on how much geometry there is
        # to overturn it *with*. A plan whose walls are drawn as hundreds of
        # matched line pairs states its own thickness emphatically and the
        # header loses to it. A drawing with a dozen incidental parallel
        # distances states nothing, and letting those dozen outvote an
        # explicit declaration is how a 14 m flat becomes a 140 m one.
        bonus = round(1.5 + 3.0 * (1.0 - _evidence_strength(pair_distances)), 3)
        for c in scored:
            if abs(c["scale_to_m"] - declared[0]) < 1e-12:
                c["declared"] = True
                c["score"] = round(c["score"] + bonus, 3) if c["score"] >= 0 else c["score"]
                c["reasons"] = c["reasons"] + ["$INSUNITS=%s declares %s (+%.2f)"
                                               % (insunits, declared[1], bonus)]

    if measurement is not None:
        imperial = measurement == 0
        for c in scored:
            if c["score"] < 0:
                continue
            is_imperial = c["scale_to_m"] in (0.0254, 0.3048)
            if is_imperial == imperial:
                c["score"] = round(c["score"] + 0.25, 3)

    scored.sort(key=lambda c: -c["score"])
    best = scored[0]

    if best["score"] < 0:
        # Everything was rejected by the plausibility filter. Fall back to the
        # declaration if there is one and say plainly that this is a guess.
        if declared:
            return UnitDecision(
                scale_to_m=declared[0], unit_name=declared[1], method="header-fallback",
                confidence=0.2, insunits=insunits, candidates=scored,
                conflict="no candidate unit produces a plausible building size",
                reason="every candidate unit gives an implausible plan extent; "
                       "falling back to the declared $INSUNITS=%s (%s)"
                       % (insunits, declared[1]),
            )
        return UnitDecision(
            scale_to_m=1.0, unit_name="assumed metres", method="default",
            confidence=0.1, insunits=insunits, candidates=scored,
            conflict="no candidate unit produces a plausible building size",
            reason="no candidate unit gives a plausible plan extent and no "
                   "$INSUNITS is declared; assuming metres",
        )

    runner_up = scored[1]["score"] if len(scored) > 1 else -1.0
    margin = best["score"] - max(runner_up, 0.0)
    confidence = max(0.25, min(0.98, 0.5 + 0.10 * best["score"] + 0.08 * margin))

    conflict = None
    method = "evidence"
    if declared:
        if abs(best["scale_to_m"] - declared[0]) < 1e-12:
            method = "evidence+header"
        else:
            conflict = (
                "$INSUNITS=%s declares %s, but the drawing's own geometry says %s "
                "(scores: %s %.2f vs %s %.2f)"
                % (insunits, declared[1], best["unit"], best["unit"], best["score"],
                   declared[1],
                   next((c["score"] for c in scored if c.get("declared")), float("nan")))
            )

    reason = "; ".join(best["reasons"])
    if conflict:
        reason = conflict + " — " + reason

    return UnitDecision(
        scale_to_m=best["scale_to_m"],
        unit_name=best["unit"],
        method=method,
        confidence=round(confidence, 3),
        reason=reason,
        insunits=insunits,
        conflict=conflict,
        candidates=scored,
    )


def modal_thickness(pair_distances: Sequence[float], scale_to_m: float) -> List[float]:
    """The wall thicknesses actually present, in metres, most common first.

    Used by the wall reconstructor to decide which parallel-pair separations
    are real walls, and by the validator to reject a plan whose "walls" have no
    coherent thickness at all.
    """
    in_band = [d * scale_to_m for d in pair_distances
               if WALL_BAND[0] <= d * scale_to_m <= WALL_BAND[1]]
    return [round(v, 4) for v, _ in _modal_clusters(in_band, top=8)]
