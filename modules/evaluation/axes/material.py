"""
ArchX3D — Material axis
=======================
Do the surfaces read as the right *materials* — not the right colours, which
the colour axis owns, but the right substances?

The hard part
-------------
A photograph has no albedo channel, so a like-for-like comparison is
impossible in principle. Two measures survive that, and this axis is built
from exactly those two rather than pretending to more:

* **Saturation.** Illumination mostly scales luminance; it moves saturation
  far less. So comparing the saturation of the render's *unlit* albedo against
  the photograph's is defensible in a way that comparing lightness is not.
  This is what catches the spec's case — a walnut floor rendered too grey.
* **Texture energy.** How much fine detail a surface carries, measured across
  two frequency bands. Wood grain, tile joints and fabric weave live there;
  flat paint does not. Asking "does this surface have about the right amount
  of visible structure" is answerable. Asking "is this the same oak" is not,
  and this axis does not ask it.

Both are taken per material region where a material-ID pass exists, which is
what lets a finding name the material instead of gesturing at the frame.

Why it carries the lowest weight
--------------------------------
It makes the largest inference of the five axes. The scoring weights say so,
and every finding here is issued with a lower confidence than the colour or
lighting equivalents.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import imaging
from ..schema import MATERIAL, AxisScore, Finding, Subsystem

#: Frequency bands for the texture comparison. 2 px catches grain and weave,
#: 8 px catches planks, tiles and panels.
BANDS = (2, 8)

#: Saturation ratio outside this band is worth reporting. A render at 0.7x or
#: 1.4x the reference's saturation reads as a different substance.
SATURATION_LOW = 0.7
SATURATION_HIGH = 1.4

#: Texture ratio outside this band is worth reporting: an order of magnitude
#: less detail is a flat colour standing in for a textured surface.
TEXTURE_LOW = 0.45
TEXTURE_HIGH = 2.2


def evaluate(ctx) -> Tuple[AxisScore, List[Finding]]:
    reason = ctx.missing()
    if reason:
        return AxisScore.unmeasured(MATERIAL, reason), []

    reference = ctx.pair.reference
    # Albedo where available, the beauty render otherwise. The fallback is
    # weaker — lighting contaminates both measures — so it costs confidence.
    surface = ctx.pair.passes.get("albedo")
    using_albedo = surface is not None
    if surface is None:
        surface = ctx.pair.render

    texture = _texture_agreement(reference, surface)
    saturation_reference = imaging.saturation(reference)
    saturation_render = imaging.saturation(surface)
    ratio = _ratio(saturation_render, saturation_reference)

    detail: Dict[str, Any] = {
        "source": "albedo" if using_albedo else "beauty render",
        "texture": texture,
        "saturation": {"reference": saturation_reference,
                       "render": saturation_render, "ratio": ratio},
    }

    score = imaging.clamp01(
        0.5 * texture["agreement"] + 0.5 * _ratio_score(ratio)
    )

    findings: List[Finding] = []
    findings.extend(_global_texture_finding(ctx, texture))
    findings.extend(_region_findings(ctx, reference, surface, detail))

    confidence = 0.7 if using_albedo else 0.45
    return AxisScore(axis=MATERIAL, score=score, measured=True,
                     confidence=confidence, detail=detail), findings


# ---------------------------------------------------------------------------
# Texture
# ---------------------------------------------------------------------------


def _texture_agreement(reference, render) -> Dict[str, Any]:
    """Detail energy per band, and how well the two agree overall."""
    bands: Dict[str, Any] = {}
    scores = []
    for scale in BANDS:
        left = imaging.detail_energy(reference, scale)
        right = imaging.detail_energy(render, scale)
        ratio = _ratio(right, left)
        bands[f"scale_{scale}"] = {"reference": left, "render": right, "ratio": ratio}
        scores.append(_ratio_score(ratio))
    agreement = sum(scores) / len(scores) if scores else 0.0
    return {"bands": bands, "agreement": agreement}


def _global_texture_finding(ctx, texture: Dict[str, Any]) -> List[Finding]:
    fine = texture["bands"][f"scale_{BANDS[0]}"]
    ratio = fine["ratio"]
    if TEXTURE_LOW <= ratio <= TEXTURE_HIGH:
        return []

    flatter = ratio < 1.0
    return [Finding(
        axis=MATERIAL,
        summary=f"Surfaces carry {'less' if flatter else 'more'} fine texture "
                f"than the reference",
        subsystem=Subsystem.MATERIAL_SPECIES,
        difference=abs(1.0 - ratio),
        severity=imaging.clamp01(abs(1.0 - ratio) / 1.5) * 0.8,
        confidence=0.55,
        why=f"fine detail energy is {fine['render']:.4f} against the "
            f"reference's {fine['reference']:.4f}; "
            + ("procedural materials that resolve to a flat colour produce "
               "this" if flatter else
               "an over-scaled procedural texture produces this"),
        evidence=texture["bands"],
        remedy=("choose material species with visible grain or pattern, or "
                "raise the procedural detail" if flatter else
                "reduce the procedural texture scale or strength"),
        room=ctx.room_id,
        viewpoint=ctx.viewpoint_id,
    )]


# ---------------------------------------------------------------------------
# Per material
# ---------------------------------------------------------------------------


def _region_findings(ctx, reference, surface, detail: Dict[str, Any]) -> List[Finding]:
    """Saturation and texture per named material region."""
    regions = ctx.material_regions()
    if not regions:
        return []

    measured: Dict[str, Any] = {}
    findings: List[Finding] = []

    for name, mask in list(regions.items())[:8]:
        coverage = imaging.fraction(mask)
        reference_saturation = imaging.saturation(reference, mask)
        render_saturation = imaging.saturation(surface, mask)
        ratio = _ratio(render_saturation, reference_saturation)
        measured[name] = {
            "coverage": coverage,
            "reference_saturation": reference_saturation,
            "render_saturation": render_saturation,
            "ratio": ratio,
        }

        if SATURATION_LOW <= ratio <= SATURATION_HIGH:
            continue

        washed = ratio < 1.0
        species = _species(name)
        findings.append(Finding(
            axis=MATERIAL,
            summary=f"{_readable(name)} appears too "
                    f"{'desaturated' if washed else 'saturated'}",
            subsystem=Subsystem.MATERIAL_SPECIES,
            difference=abs(1.0 - ratio),
            severity=imaging.clamp01(
                imaging.clamp01(abs(1.0 - ratio)) * (0.5 + coverage)
            ),
            confidence=0.6 if ctx.has_pass("albedo") else 0.4,
            why=f"over the {coverage * 100:.0f}% of the frame the material-ID "
                f"pass assigns to {name}, the reference averages "
                f"{reference_saturation:.3f} saturation and the render "
                f"{render_saturation:.3f}",
            evidence=measured[name],
            remedy=(f"the {species} species is rendering too flat; check its "
                    f"base colour saturation and procedural tint"
                    if washed else
                    f"the {species} species is rendering too vividly; reduce "
                    f"its base colour saturation"),
            room=ctx.room_id,
            viewpoint=ctx.viewpoint_id,
            materials=[name],
            objects=_objects_using(ctx, species),
        ))

    if measured:
        detail["regions"] = measured
    return findings


def _objects_using(ctx, species: str) -> List[str]:
    """Which graph objects claim this material species.

    Turns a material finding into a list of things to look at. Best-effort:
    the material name encodes the species, and objects record theirs, so the
    join is on that.
    """
    if ctx.graph is None or not species:
        return []
    return sorted(
        obj.id for obj in ctx.graph.objects
        if obj.material and species.replace(" ", "_") in obj.material
    )[:8]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ratio(value: float, against: float) -> float:
    """``value / against``, guarded. 1.0 when the reference is featureless.

    Returning 1.0 rather than 0 or infinity for a featureless reference is the
    honest choice: it says "no disagreement detected", which is true, where
    the alternatives would manufacture a finding out of a blank wall.
    """
    if against <= 1e-6:
        return 1.0
    return float(value) / float(against)


def _ratio_score(ratio: float) -> float:
    """Turn a ratio into ``[0, 1]``, symmetric about 1.0.

    Symmetric in log space, because half as much texture and twice as much are
    equally wrong, while a linear treatment would rate the first as far worse.
    """
    import math

    if ratio <= 0:
        return 0.0
    return imaging.clamp01(1.0 - abs(math.log(ratio)) / math.log(4.0))


def _species(material_name: str) -> str:
    """``M_wood_dark_5A3B22`` -> ``wood dark``."""
    return _readable(material_name)


def _readable(material_name: str) -> str:
    if not material_name.startswith("M_"):
        return material_name
    parts = material_name[2:].split("_")
    if len(parts) > 1 and all(c in "0123456789ABCDEFabcdef" for c in parts[-1]):
        parts = parts[:-1]
    return " ".join(parts) or material_name
