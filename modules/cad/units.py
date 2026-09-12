"""
ArchX3D — Unit, Scale and North Detection
=========================================
Decides what one drawing unit means in metres, and which way is north.

Why unit detection deserves its own module
------------------------------------------
Getting this wrong is catastrophic and silent. A plan read as metres when it
is millimetres produces a 12-kilometre-wide apartment; every room passes
straight through the area priors as "too big to classify", every furniture
placement is nonsense, and nothing visibly errors. The old heuristic guessed
from bounding-box size alone, with overlapping and unreachable branches (the
``200 < max_dim < 500`` feet case sat *after* a ``500 <= max_dim`` millimetre
case that already claimed part of its range).

The fix is to rank evidence rather than fall through a chain of ``elif``:

1. **User override** — an explicit scale factor is obeyed, always.
2. **``$INSUNITS`` header** — the file stating its own units. Authoritative
   when present and non-zero.
3. **``$MEASUREMENT`` + dimension text** — a dimension annotation reading
   "3600" against a segment measuring 3600 units confirms millimetres
   directly. This is real corroboration, not a guess.
4. **Bounding-box heuristic** — last resort, with non-overlapping bands and an
   explicitly stated confidence so downstream code knows it was a guess.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

from .schema import DrawingUnits, NorthArrow, Source

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

#: Plausible extent of a building floor plan, in metres. A plan outside this
#: range after conversion indicates the conversion was wrong.
PLAUSIBLE_PLAN_M = (3.0, 500.0)


def from_insunits(code: Optional[int]) -> Optional[DrawingUnits]:
    """Units declared by the ``$INSUNITS`` header, if it declares any.

    Code 0 means "unitless" and is *not* a declaration — treating it as metres
    is the single most common way a millimetre drawing gets misread.
    """
    if not code:
        return None

    entry = INSUNITS.get(int(code))
    if entry is None:
        return None

    scale, name = entry
    return DrawingUnits(
        scale_to_m=scale,
        unit_name=name,
        insunits=int(code),
        method="header",
        confidence=0.97,
        reason=f"$INSUNITS={code} declares {name}",
    )


def from_dimensions(
    raw_measurements: Sequence[float], texts: Sequence[str]
) -> Optional[DrawingUnits]:
    """Corroborate units from dimension annotations.

    A DIMENSION entity carries both a measured value in drawing units and the
    text that gets printed. When an architect overrides the text, the printed
    string is usually in the project's working unit. If a dimension measuring
    3600 units prints "3600", the drawing unit *is* the printed unit; if it
    prints "3.60", the drawing is in millimetres printed as metres.

    Only unambiguous agreement counts — anything else returns ``None`` rather
    than a weak guess.
    """
    ratios = []
    for measurement, text in zip(raw_measurements, texts):
        if measurement <= 0 or not text:
            continue
        printed = _first_number(text)
        if printed is None or printed <= 0:
            continue
        ratios.append(measurement / printed)

    if len(ratios) < 2:
        return None

    ratios.sort()
    median = ratios[len(ratios) // 2]

    # Every ratio must agree with the median, or the sample is inconsistent
    # and says nothing reliable.
    if any(abs(r / median - 1.0) > 0.02 for r in ratios):
        return None

    for scale, name, expected in (
        (0.001, "millimetres", 1.0),
        (1.0, "metres", 1.0),
        (0.001, "millimetres", 1000.0),
        (0.01, "centimetres", 1.0),
        (0.3048, "feet", 1.0),
    ):
        if abs(median - expected) < 0.02 * expected:
            return DrawingUnits(
                scale_to_m=scale,
                unit_name=name,
                method="dimension",
                confidence=0.9,
                reason=(
                    f"{len(ratios)} dimension annotations agree that one drawing "
                    f"unit prints as {median:.3g} of the printed unit"
                ),
            )
    return None


def from_extent(width: float, height: float) -> DrawingUnits:
    """Guess units from the plan's raw extent.

    The bands below are **disjoint and exhaustive**, tested against the
    largest dimension, and each is justified by what a building actually is:
    a floor plan's longest side is 4-150 m. Any raw extent is therefore
    consistent with exactly one unit system under that assumption.
    """
    extent = max(width, height)
    if extent <= 0:
        return DrawingUnits(
            scale_to_m=1.0, unit_name="unknown", method="heuristic",
            confidence=0.0, reason="empty drawing extent; assuming metres",
        )

    # Ordered widest-first; the first band containing `extent` wins, and the
    # bands do not overlap.
    bands = (
        (150.0, 1e9, 0.001, "millimetres",
         "extent of {e:.0f} units is only a building if the unit is a millimetre"),
        (60.0, 150.0, 0.3048, "feet",
         "extent of {e:.0f} units suits feet for a large plan"),
        (4.0, 60.0, 1.0, "metres",
         "extent of {e:.1f} units is a plausible building size in metres"),
        (0.0, 4.0, 1.0, "metres",
         "extent of {e:.2f} units is small; assuming metres"),
    )

    for low, high, scale, name, template in bands:
        if low <= extent < high:
            converted = extent * scale
            plausible = PLAUSIBLE_PLAN_M[0] <= converted <= PLAUSIBLE_PLAN_M[1]
            return DrawingUnits(
                scale_to_m=scale,
                unit_name=name,
                method="heuristic",
                # A heuristic is never as trustworthy as a declaration, and it
                # is worth less again when the result is implausible.
                confidence=0.55 if plausible else 0.25,
                reason=template.format(e=extent)
                + (f" -> {converted:.1f} m" if scale != 1.0 else ""),
            )

    return DrawingUnits(
        scale_to_m=1.0, unit_name="metres", method="heuristic",
        confidence=0.2, reason=f"extent {extent:.1f} matched no band; assuming metres",
    )


def resolve(
    insunits: Optional[int],
    extent: Tuple[float, float],
    dimension_measurements: Sequence[float] = (),
    dimension_texts: Sequence[str] = (),
    user_scale: Optional[float] = None,
) -> DrawingUnits:
    """Pick the best-supported unit interpretation available.

    Ranked, not chained: each source is asked in trust order and the first
    that produces an answer wins, with its own confidence attached.
    """
    if user_scale is not None and user_scale > 0 and abs(user_scale - 1.0) > 1e-9:
        return DrawingUnits(
            scale_to_m=user_scale, unit_name="user-specified", method="user",
            confidence=1.0, reason=f"caller supplied an explicit scale of {user_scale}",
        )

    header = from_insunits(insunits)
    if header is not None:
        # A header claim that yields an absurd building is more likely to be a
        # stale template value than the truth, so it is checked, not trusted
        # blindly. The heuristic takes over only when it is *itself* plausible —
        # overriding a declared, high-confidence unit with a guess that lands
        # on an equally or more absurd size (e.g. a 0.6 m building) throws away
        # real evidence for no gain, and silently zeroes every wall downstream.
        converted = max(extent) * header.scale_to_m
        if converted and not (PLAUSIBLE_PLAN_M[0] <= converted <= PLAUSIBLE_PLAN_M[1]):
            fallback = from_extent(*extent)
            fallback_converted = max(extent) * fallback.scale_to_m
            if PLAUSIBLE_PLAN_M[0] <= fallback_converted <= PLAUSIBLE_PLAN_M[1]:
                fallback.insunits = header.insunits
                fallback.confidence = min(fallback.confidence, 0.5)
                fallback.reason = (
                    f"$INSUNITS={insunits} ({header.unit_name}) would make the plan "
                    f"{converted:.0f} m across, which is not a building; "
                    f"overridden — {fallback.reason}"
                )
                return fallback
            # Neither the header nor the heuristic lands in a plausible range
            # (large multi-floor or multi-building sheets legitimately do
            # this). The header is still the file's own declaration and the
            # heuristic is no better, so keep it — flagged with reduced
            # confidence so the uncertainty is visible downstream.
            header.confidence = min(header.confidence, 0.6)
            header.reason = (
                f"{header.reason}; plan is {converted:.0f} m across, unusually "
                f"large for a single floor, but the heuristic fallback "
                f"({fallback.unit_name}, {fallback_converted:.0f} m) was no "
                f"more plausible, so the header is kept"
            )
            return header
        return header

    corroborated = from_dimensions(dimension_measurements, dimension_texts)
    if corroborated is not None:
        return corroborated

    return from_extent(*extent)


# ---------------------------------------------------------------------------
# North
# ---------------------------------------------------------------------------


def resolve_north(
    north_direction: Optional[float] = None,
    arrow_rotation: Optional[float] = None,
) -> NorthArrow:
    """Determine the plan heading of true north.

    ``$NORTHDIRECTION`` is an angle in radians CCW from the +X axis in the
    WCS. Our convention states headings in degrees CCW from +Y (up the page),
    matching ``rotation_z`` elsewhere in the project, so the value is
    converted rather than passed through — a 90-degree error here rotates
    every sun study in the project.
    """
    if north_direction is not None:
        heading = (math.degrees(float(north_direction)) - 90.0) % 360.0
        return NorthArrow(
            heading_deg=heading,
            source=Source.CAD_METADATA,
            confidence=0.95,
            reason=f"$NORTHDIRECTION={north_direction:.4f} rad",
        )

    if arrow_rotation is not None:
        return NorthArrow(
            heading_deg=float(arrow_rotation) % 360.0,
            source=Source.CAD_BLOCK,
            confidence=0.7,
            reason=f"north-arrow block rotated {arrow_rotation:.1f}°",
        )

    return NorthArrow(
        heading_deg=0.0,
        source=Source.REASONING,
        confidence=0.15,
        reason="no north declared; assuming plan north is up the page",
    )


def _first_number(text: str) -> Optional[float]:
    """The first numeric literal in a string, ignoring formatting."""
    import re

    match = re.search(r"[0-9]+(?:[.,][0-9]+)?", str(text).replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None
