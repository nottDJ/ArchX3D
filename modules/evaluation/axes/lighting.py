"""
ArchX3D — Lighting axis
=======================
Is the room lit like the photograph, and is a difference the *light's* fault?

The separation the albedo pass buys
-----------------------------------
A photograph records albedo times illumination. So does a render. Comparing
the two directly can tell you the render is darker, but not whether that is
because the lamps are underpowered or because the walls are painted charcoal.
Those need opposite fixes, and a finding that cannot tell them apart is not
actionable.

The albedo pass resolves it. Dividing the render's luminance by its own albedo
luminance recovers the *shading* — the light landing on each surface, with the
surface colour divided out. If the render's albedo already matches the
reference's tone and only the lit image is dark, the light is at fault. If the
albedo is dark too, it is a finish problem and this axis defers to colour.

What is measured
----------------
* **Exposure** — mean luminance, the headline "darker / brighter" number.
* **Contrast** — luminance spread, which separates flat ambient light from
  directional light with real shadows.
* **Warmth** — red minus blue, a stand-in for colour temperature. It cannot be
  absolute: a photograph's white balance is unknown. It is only ever compared
  against the same measure taken from the render.
* **Direction** — the dominant brightness gradient, as corroboration for a
  daylight-direction finding rather than as a claim on its own.

Nothing here tries to recover photometric units. A photograph does not carry
them, and inventing them would make every number unfalsifiable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import imaging
from ..schema import LIGHTING, AxisScore, Finding, Subsystem

#: Mean-luminance gap worth a finding. 0.06 in linear light is a visible step
#: in exposure, well beyond render-versus-camera nuance.
EXPOSURE_FINDING = 0.06

#: Where an exposure gap counts as total disagreement, for scoring.
EXPOSURE_SATURATION = 0.35

CONTRAST_FINDING = 0.05
WARMTH_FINDING = 0.05


def evaluate(ctx) -> Tuple[AxisScore, List[Finding]]:
    reason = ctx.missing()
    if reason:
        return AxisScore.unmeasured(LIGHTING, reason), []

    reference, render = ctx.pair.reference, ctx.pair.render
    luma_reference = imaging.luma(reference)
    luma_render = imaging.luma(render)

    exposure_reference = float(luma_reference.mean())
    exposure_render = float(luma_render.mean())
    exposure_gap = exposure_render - exposure_reference

    contrast_reference = float(luma_reference.std())
    contrast_render = float(luma_render.std())
    contrast_gap = contrast_render - contrast_reference

    warmth_reference = imaging.warmth(reference)
    warmth_render = imaging.warmth(render)
    warmth_gap = warmth_render - warmth_reference

    detail: Dict[str, Any] = {
        "exposure": {"reference": exposure_reference, "render": exposure_render,
                     "difference": exposure_gap},
        "contrast": {"reference": contrast_reference, "render": contrast_render,
                     "difference": contrast_gap},
        "warmth": {"reference": warmth_reference, "render": warmth_render,
                   "difference": warmth_gap},
        "direction": {
            "reference": list(imaging.gradient_direction(reference)),
            "render": list(imaging.gradient_direction(render)),
        },
    }

    shading = _shading_ratio(ctx)
    if shading is not None:
        detail["albedo_normalised_shading"] = shading

    score = imaging.clamp01(
        0.5 * (1.0 - imaging.clamp01(abs(exposure_gap) / EXPOSURE_SATURATION))
        + 0.3 * (1.0 - imaging.clamp01(abs(contrast_gap) / 0.2))
        + 0.2 * (1.0 - imaging.clamp01(abs(warmth_gap) / 0.2))
    )

    findings: List[Finding] = []
    findings.extend(_exposure_finding(ctx, exposure_gap, exposure_reference,
                                      exposure_render, shading))
    findings.extend(_contrast_finding(ctx, contrast_gap, contrast_reference,
                                      contrast_render))
    findings.extend(_warmth_finding(ctx, warmth_gap))

    confidence = 0.85 if ctx.has_pass("albedo") else 0.65
    return AxisScore(axis=LIGHTING, score=score, measured=True,
                     confidence=confidence, detail=detail), findings


# ---------------------------------------------------------------------------
# Albedo-normalised shading
# ---------------------------------------------------------------------------


def _shading_ratio(ctx):
    """Mean illumination on the render's surfaces, surface colour divided out.

    ``None`` without an albedo pass. Pixels whose albedo is near black are
    excluded rather than clamped: dividing by an almost-zero albedo produces
    an enormous ratio from a surface that reflects nothing, which would drown
    the statistic in exactly the places it means least.
    """
    albedo = ctx.pair.passes.get("albedo")
    if albedo is None:
        return None

    numpy, _ = imaging.backend()
    albedo_luma = imaging.luma(albedo)
    render_luma = imaging.luma(ctx.pair.render)
    lit = albedo_luma > 0.02
    if not lit.any():
        return None
    return float((render_luma[lit] / albedo_luma[lit]).mean())


def _albedo_matches_reference(ctx) -> "bool | None":
    """Whether the unlit surfaces are already about as bright as the photo.

    The test that decides whether a darkness finding points at the lighting or
    at the finishes. ``None`` when there is no albedo pass to ask.
    """
    albedo = ctx.pair.passes.get("albedo")
    if albedo is None:
        return None
    albedo_mean = float(imaging.luma(albedo).mean())
    reference_mean = float(imaging.luma(ctx.pair.reference).mean())
    return abs(albedo_mean - reference_mean) < EXPOSURE_FINDING


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _exposure_finding(ctx, gap: float, reference: float, render: float,
                      shading) -> List[Finding]:
    if abs(gap) < EXPOSURE_FINDING:
        return []

    darker = gap < 0
    word = "darker" if darker else "brighter"
    subsystem, why, confidence = _attribute_exposure(ctx, gap, darker)

    environment = ctx.lighting()
    evidence: Dict[str, Any] = {
        "reference_luminance": reference,
        "render_luminance": render,
        "difference": abs(gap),
    }
    if shading is not None:
        evidence["albedo_normalised_shading"] = shading
    if environment is not None:
        evidence["recorded_ambient"] = environment.ambient
        evidence["recorded_time_of_day"] = environment.time_of_day

    return [Finding(
        axis=LIGHTING,
        summary=f"Render is {word} than the reference",
        subsystem=subsystem,
        difference=abs(gap),
        severity=imaging.clamp01(abs(gap) / EXPOSURE_SATURATION),
        confidence=confidence,
        why=why,
        evidence=evidence,
        remedy=_exposure_remedy(subsystem, darker, environment),
        room=ctx.room_id,
        viewpoint=ctx.viewpoint_id,
    )]


def _attribute_exposure(ctx, gap: float, darker: bool) -> Tuple[str, str, float]:
    matches = _albedo_matches_reference(ctx)
    if matches is None:
        return (
            Subsystem.LIGHTING_ENVIRONMENT,
            f"mean luminance differs by {abs(gap):.3f}; without an albedo pass "
            f"this cannot be separated from surfaces that are simply darker",
            0.55,
        )
    if matches:
        return (
            Subsystem.LIGHTING_ENVIRONMENT,
            f"the unlit albedo is about as bright as the reference, so the "
            f"surfaces are right and the light falling on them is "
            f"{'insufficient' if darker else 'excessive'}",
            0.85,
        )
    return (
        Subsystem.SURFACE_FINISH,
        f"the unlit albedo is itself {'darker' if darker else 'brighter'} than "
        f"the reference, so the finishes carry the difference rather than the "
        f"lighting",
        0.75,
    )


def _exposure_remedy(subsystem: str, darker: bool, environment) -> str:
    if subsystem == Subsystem.SURFACE_FINISH:
        return ("lighten the surface finishes" if darker
                else "darken the surface finishes")
    direction = "raise" if darker else "lower"
    if environment is not None:
        return (f"{direction} the room's LightingEnvironment ambient "
                f"(currently {environment.ambient:.2f}) or the luminaire power")
    return f"{direction} the room's ambient level or the luminaire power"


def _contrast_finding(ctx, gap: float, reference: float, render: float) -> List[Finding]:
    if abs(gap) < CONTRAST_FINDING:
        return []

    flatter = gap < 0
    environment = ctx.lighting()
    spread = "narrower" if flatter else "wider"
    character = "softer, more uniform" if flatter else "harder, more directional"
    return [Finding(
        axis=LIGHTING,
        summary=f"Render's lighting is {'flatter' if flatter else 'harsher'} "
                f"than the reference",
        subsystem=Subsystem.LIGHTING_ENVIRONMENT,
        difference=abs(gap),
        severity=imaging.clamp01(abs(gap) / 0.2) * 0.7,
        confidence=0.65,
        why=f"luminance spread is {render:.3f} against the reference's "
            f"{reference:.3f}; a {spread} spread means {character} light",
        evidence={"reference_contrast": reference, "render_contrast": render,
                  "shadow_softness": getattr(environment, "shadow_softness", None)},
        remedy=(f"{'lower' if flatter else 'raise'} shadow_softness, or "
                f"{'increase' if flatter else 'reduce'} the share of light "
                f"coming from a single direction"),
        room=ctx.room_id,
        viewpoint=ctx.viewpoint_id,
    )]


def _warmth_finding(ctx, gap: float) -> List[Finding]:
    if abs(gap) < WARMTH_FINDING:
        return []

    warmer = gap > 0
    environment = ctx.lighting()
    recorded = getattr(environment, "color_temperature_k", None)
    return [Finding(
        axis=LIGHTING,
        summary=f"Render's light is {'warmer' if warmer else 'cooler'} than "
                f"the reference",
        subsystem=Subsystem.LIGHTING_ENVIRONMENT,
        difference=abs(gap),
        severity=imaging.clamp01(abs(gap) / 0.2) * 0.8,
        confidence=0.6,
        why=f"the render's red-minus-blue balance differs by {abs(gap):.3f}; "
            f"a photograph's white balance is unknown, so this is a relative "
            f"reading rather than a colour temperature",
        evidence={"warmth_difference": gap,
                  "recorded_color_temperature_k": recorded},
        remedy=(f"{'lower' if warmer else 'raise'} the room's "
                f"color_temperature_k"
                + (f" (currently {recorded:.0f} K)" if recorded else "")),
        room=ctx.room_id,
        viewpoint=ctx.viewpoint_id,
    )]
