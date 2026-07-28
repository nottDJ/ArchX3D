"""
ArchX3D — Colour axis
=====================
Does the reconstruction have the right colours, and if not, whose fault is it?

What is measured
----------------
Two things, in CIELAB so that the distances mean what a person would mean:

* **Cast** — the mean colour of the whole frame against the mean colour of the
  photograph. A single number that catches "everything is too warm".
* **Distribution** — histogram intersection, which catches the case the mean
  misses: a render whose average matches because it is half too blue and half
  too orange.

Then, where a material-ID pass exists, the same comparison **per region**.
That is the difference between "colour: 0.71" and "the floor is 14 dE from the
reference", and it is the whole reason the ID passes are rendered.

Attribution
-----------
A colour difference has at least three possible owners: the surface finish,
the room's colour palette, and the lighting. This axis distinguishes the first
two from the third by consulting the albedo pass — if the render's *unlit*
colour already differs from the photograph, no amount of relighting will fix
it, and the finding points at the finish. If albedo agrees and only the lit
render differs, the finding is the lighting axis's to make, and this one says
so rather than double-reporting it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import imaging
from ..schema import COLOUR, AxisScore, Finding, Subsystem

#: Above this mean CIE76 distance the frames read as different colours rather
#: than as the same colour photographed differently. 40 is roughly the gap
#: between "warm white" and "beige" — well past a rendering nuance.
DE_SATURATION = 40.0

#: Region difference worth reporting. ~6 dE is around the point a side-by-side
#: comparison stops being arguable.
DE_REGION_FINDING = 6.0

#: Whole-frame cast worth reporting.
DE_GLOBAL_FINDING = 8.0


def evaluate(ctx) -> Tuple[AxisScore, List[Finding]]:
    reason = ctx.missing()
    if reason:
        return AxisScore.unmeasured(COLOUR, reason), []

    reference, render = ctx.pair.reference, ctx.pair.render
    lab_reference = imaging.to_lab(reference)
    lab_render = imaging.to_lab(render)

    cast = imaging.delta_e(
        _mean_lab(lab_reference), _mean_lab(lab_render)
    )
    per_pixel = imaging.delta_e(lab_reference, lab_render)
    agreement = imaging.intersection(
        imaging.histogram(reference), imaging.histogram(render)
    )

    # The mean and the distribution answer different questions and neither
    # subsumes the other, so both carry weight. Per-pixel dE is reported as
    # evidence but not scored: it punishes a correct render of a misplaced
    # sofa twice, once here and once in the layout axis.
    cast_score = 1.0 - imaging.clamp01(cast / DE_SATURATION)
    score = imaging.clamp01(0.6 * cast_score + 0.4 * agreement)

    detail: Dict[str, Any] = {
        "cast_delta_e": cast,
        "per_pixel_delta_e": per_pixel,
        "histogram_agreement": agreement,
        "reference_mean": imaging.to_hex(imaging.mean_colour(reference)),
        "render_mean": imaging.to_hex(imaging.mean_colour(render)),
    }

    findings: List[Finding] = []
    findings.extend(_global_finding(ctx, cast, lab_reference, lab_render, detail))
    findings.extend(_region_findings(ctx, detail))

    confidence = 0.85 if ctx.has_pass("albedo") else 0.7
    return AxisScore(axis=COLOUR, score=score, measured=True,
                     confidence=confidence, detail=detail), findings


# ---------------------------------------------------------------------------
# Whole frame
# ---------------------------------------------------------------------------


def _global_finding(ctx, cast: float, lab_reference, lab_render,
                    detail: Dict[str, Any]) -> List[Finding]:
    if cast < DE_GLOBAL_FINDING:
        return []

    mean_reference = _mean_lab(lab_reference)
    mean_render = _mean_lab(lab_render)
    delta_b = float(mean_render[2] - mean_reference[2])   # +b is yellow
    delta_a = float(mean_render[1] - mean_reference[1])   # +a is red
    delta_l = float(mean_render[0] - mean_reference[0])

    direction = _describe(delta_a, delta_b)
    detail["delta_lab"] = [delta_l, delta_a, delta_b]

    # Which subsystem owns a whole-frame cast depends on whether the unlit
    # surfaces are already wrong. Without the albedo pass this cannot be
    # decided, and the finding says so rather than guessing.
    subsystem, why, confidence = _attribute(ctx, cast)

    return [Finding(
        axis=COLOUR,
        summary=f"Render reads {direction} than the reference",
        subsystem=subsystem,
        difference=cast,
        unit="dE",
        severity=imaging.clamp01(cast / DE_SATURATION),
        confidence=confidence,
        why=why,
        evidence={
            "cast_delta_e": cast,
            "delta_L": delta_l, "delta_a": delta_a, "delta_b": delta_b,
            "reference_mean": detail["reference_mean"],
            "render_mean": detail["render_mean"],
        },
        remedy=_remedy(subsystem, direction),
        room=ctx.room_id,
        viewpoint=ctx.viewpoint_id,
    )]


def _attribute(ctx, cast: float) -> Tuple[str, str, float]:
    """Decide whether a cast belongs to the surfaces or to the light."""
    albedo = ctx.pair.passes.get("albedo")
    if albedo is None:
        return (
            Subsystem.COLOUR_PALETTE,
            "the whole frame differs in mean colour; without an albedo pass "
            "this cannot be separated from a lighting difference",
            0.5,
        )

    albedo_cast = imaging.delta_e(
        _mean_lab(imaging.to_lab(ctx.pair.reference)),
        _mean_lab(imaging.to_lab(albedo)),
    )
    if albedo_cast >= cast * 0.6:
        return (
            Subsystem.SURFACE_FINISH,
            f"the unlit albedo is already {albedo_cast:.1f} dE from the "
            f"reference, so the surfaces themselves carry the difference "
            f"rather than the light on them",
            0.8,
        )
    return (
        Subsystem.LIGHTING_ENVIRONMENT,
        f"the unlit albedo is only {albedo_cast:.1f} dE from the reference "
        f"while the lit render is {cast:.1f} dE away, so the light is tinting "
        f"surfaces that are themselves close to correct",
        0.75,
    )


# ---------------------------------------------------------------------------
# Per region
# ---------------------------------------------------------------------------


def _region_findings(ctx, detail: Dict[str, Any]) -> List[Finding]:
    """Localised colour differences, named by the material that owns them."""
    regions = ctx.material_regions()
    if not regions:
        return []

    lab_reference = imaging.to_lab(ctx.pair.reference)
    lab_render = imaging.to_lab(ctx.pair.render)

    measured: Dict[str, float] = {}
    findings: List[Finding] = []

    for name, mask in list(regions.items())[:8]:
        reference_mean = _mean_lab(lab_reference[mask])
        render_mean = _mean_lab(lab_render[mask])
        difference = imaging.delta_e(reference_mean, render_mean)
        measured[name] = difference
        if difference < DE_REGION_FINDING:
            continue

        coverage = imaging.fraction(mask)
        findings.append(Finding(
            axis=COLOUR,
            summary=f"{_readable(name)} is {difference:.0f} dE from the reference "
                    f"in the region it covers",
            subsystem=Subsystem.SURFACE_FINISH,
            difference=difference,
            unit="dE",
            # A large region being wrong matters more than a small one, so
            # severity is scaled by how much of the frame it decides. Clamped:
            # severity is a 0-1 quantity by contract, and it is what ranking
            # compares across axes whose own units share nothing.
            severity=imaging.clamp01(
                imaging.clamp01(difference / DE_SATURATION) * (0.5 + coverage)
            ),
            confidence=0.7,
            why=f"the material-ID pass puts {name} across {coverage * 100:.0f}% of "
                f"the frame; over exactly those pixels the reference averages "
                f"{imaging.to_hex(imaging.mean_colour(ctx.pair.reference, mask))} "
                f"and the render "
                f"{imaging.to_hex(imaging.mean_colour(ctx.pair.render, mask))}",
            evidence={
                "delta_e": difference,
                "coverage": coverage,
                "reference_mean": imaging.to_hex(
                    imaging.mean_colour(ctx.pair.reference, mask)),
                "render_mean": imaging.to_hex(
                    imaging.mean_colour(ctx.pair.render, mask)),
            },
            remedy=f"adjust the colour of the {_readable(name)} finish, or the "
                   f"palette role it is drawn from",
            room=ctx.room_id,
            viewpoint=ctx.viewpoint_id,
            materials=[name],
        ))

    if measured:
        detail["regions"] = measured
    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mean_lab(lab):
    numpy, _ = imaging.backend()
    return numpy.asarray(lab, dtype=numpy.float64).reshape(-1, 3).mean(axis=0)


def _describe(delta_a: float, delta_b: float) -> str:
    """Name a colour shift the way a person would."""
    if abs(delta_b) >= abs(delta_a):
        return "warmer" if delta_b > 0 else "cooler"
    return "redder" if delta_a > 0 else "greener"


def _readable(material_name: str) -> str:
    """``M_wood_dark_5A3B22`` -> ``wood dark``; leave plain names alone."""
    if not material_name.startswith("M_"):
        return material_name
    parts = material_name[2:].split("_")
    if len(parts) > 1 and all(c in "0123456789ABCDEFabcdef" for c in parts[-1]):
        parts = parts[:-1]
    return " ".join(parts) or material_name


def _remedy(subsystem: str, direction: str) -> str:
    if subsystem == Subsystem.LIGHTING_ENVIRONMENT:
        return (f"the surfaces are close; shift the room's lighting colour "
                f"temperature to make the render less {direction}")
    if subsystem == Subsystem.SURFACE_FINISH:
        return f"correct the surface finishes; they are themselves {direction}"
    return f"review the room's colour palette; the render is {direction}"
