"""
ArchX3D — Species-aware procedural materials
============================================
Builds Blender shader node graphs from the catalog's material taxonomy.

Why procedural
--------------
The project ships no texture library and should not start downloading one: a
scanned texture pack is hundreds of megabytes, licence-encumbered, and still
wrong for the species you happen to need. Procedural node graphs are a few
kilobytes, resolution-independent, and — crucially — *parameterisable*, so one
wood recipe covers oak, walnut, teak, mahogany and ebony by moving colours and
grain contrast rather than by shipping five images.

How a material is decided
-------------------------
Four inputs, in order of authority:

1. **The observed species** from the scene graph — ``walnut``, not ``wood``.
2. **The style**, which refines a generic family into a species when the
   pipeline only managed the family (see :mod:`blender.styles`).
3. **The room palette**, which tints within what the material can believably
   do (see :mod:`blender.palette`).
4. **The catalog prior**, supplying colour, roughness, metallic, texture
   recipe and grain strength.

Recipes
-------
``catalog.MaterialPrior.texture`` names the recipe; ``grain`` scales its
strength. Nine recipes cover all 61 materials:

======================  =================================================
``wood_grain``          Stretched noise bands, two-tone ramp, grain bump
``veined``              Sparse high-contrast noise veins over a stone base
``speckle``             Voronoi chips — terrazzo, granite
``weave``               Crossed waves plus fine noise — textiles, carpet
``brick``               Brick node with mortar
``tiled``               Brick node squared up, wide grout
``brushed``             Directional noise driving roughness, not colour
``noise``               Broad blotches plus fine tooth — concrete, stone
``grain``               Fine bump only — leather, ceramic
``flat``                Plain Principled — paint, glass
======================  =================================================

Version robustness
------------------
Blender renames Principled BSDF sockets between releases ("Sheen" became
"Sheen Weight" in 4.0, "Specular" became "Specular IOR Level") and removed the
Musgrave node in 4.1. Everything here goes through name-list helpers and avoids
removed nodes, so the same code builds on 3.x through 5.x. A socket that does
not exist in the running version is skipped rather than raising.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional, Sequence, Tuple

import bpy

from vision import catalog

from . import colour, palette as palette_mod, styles

#: Roughness is clamped away from 0 — a perfectly smooth surface produces a
#: mirror highlight that reads as a rendering error on furniture.
MIN_ROUGHNESS = 0.04
MAX_ROUGHNESS = 1.0


# ---------------------------------------------------------------------------
# Node helpers — version-tolerant
# ---------------------------------------------------------------------------


def _set_input(node, names: Sequence[str], value) -> bool:
    """Set the first socket that exists from ``names``.

    Blender renames Principled sockets between releases. Trying a list and
    skipping silently is what lets one code path target 3.x through 5.x; the
    alternative is a version matrix in every material function.
    """
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            try:
                socket.default_value = value
                return True
            except (TypeError, ValueError):
                continue
    return False


def _new_mix(nodes, links, factor):
    """A colour-mix node, whichever the running Blender provides.

    Returns ``(node, input_a, input_b, output)`` so callers link sockets
    without caring which node class they got.

    ``ShaderNodeMixRGB`` was superseded by the generic ``ShaderNodeMix`` in
    4.0. The legacy node still registers, so it is preferred for its stable
    named sockets; the generic node is the fallback and needs index access
    because it exposes several identically-named sockets per data type.
    """
    try:
        node = nodes.new("ShaderNodeMixRGB")
        node.blend_type = "MIX"
        node.inputs["Fac"].default_value = factor
        return node, node.inputs["Color1"], node.inputs["Color2"], node.outputs["Color"]
    except RuntimeError:
        pass

    node = nodes.new("ShaderNodeMix")
    node.data_type = "RGBA"
    # Sockets 6 and 7 are the RGBA A/B pair; 0 is Factor, output 2 is Result.
    node.inputs[0].default_value = factor
    return node, node.inputs[6], node.inputs[7], node.outputs[2]


def _ramp(nodes, stops: Sequence[Tuple[float, Sequence[float]]]):
    """A colour ramp with the given ``(position, rgba)`` stops."""
    node = nodes.new("ShaderNodeValToRGB")
    elements = node.color_ramp.elements

    while len(elements) > len(stops):
        elements.remove(elements[-1])
    while len(elements) < len(stops):
        elements.new(1.0)

    for element, (position, rgba) in zip(elements, stops):
        element.position = position
        element.color = tuple(rgba)
    return node


def _coords(nodes, links, scale: Sequence[float]):
    """Object-space texture coordinates with an anisotropic scale.

    Object coordinates rather than UV or Generated: procedural furniture is
    built without UVs, and Generated coordinates normalise to each object's
    bounding box, which would make a wood grain finer on a stool than on a
    table. Object space keeps the grain at a consistent real-world size across
    every piece in the room.
    """
    coordinate = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = tuple(scale)
    links.new(coordinate.outputs["Object"], mapping.inputs["Vector"])
    return mapping.outputs["Vector"]


def _bump(nodes, links, height_socket, strength: float, target_socket) -> None:
    """Drive a normal from a height signal."""
    if strength <= 0.001:
        return
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = min(1.0, max(0.0, strength))
    bump.inputs["Distance"].default_value = 0.02
    links.new(height_socket, bump.inputs["Height"])
    links.new(bump.outputs["Normal"], target_socket)


def _noise(nodes, vector_socket, links, scale: float, detail: float = 6.0,
           roughness: float = 0.5, distortion: float = 0.0):
    node = nodes.new("ShaderNodeTexNoise")
    node.inputs["Scale"].default_value = scale
    node.inputs["Detail"].default_value = detail
    _set_input(node, ["Roughness"], roughness)
    _set_input(node, ["Distortion"], distortion)
    links.new(vector_socket, node.inputs["Vector"])
    return node


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


def _recipe_wood_grain(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Long stretched bands with fibre noise — oak through ebony.

    The grain runs along object X, which matches how the furniture builder
    orients planks and table tops. Two tones are mixed rather than one colour
    varied, because real timber reads as light fibre against darker latewood.
    """
    vector = _coords(nodes, links, (0.35, 5.0, 5.0))

    # Distorted wave gives the band structure; the distortion is what stops it
    # looking like corrugated iron.
    wave = nodes.new("ShaderNodeTexWave")
    wave.wave_type = "BANDS"
    wave.bands_direction = "X"
    wave.wave_profile = "SIN"
    wave.inputs["Scale"].default_value = 2.4
    _set_input(wave, ["Distortion"], 12.0 * (0.4 + grain))
    _set_input(wave, ["Detail"], 3.0)
    _set_input(wave, ["Detail Scale"], 1.4)
    links.new(vector, wave.inputs["Vector"])

    fibre = _noise(nodes, vector, links, scale=90.0, detail=4.0, roughness=0.7)

    dark = colour.hex_to_linear(colour.shift(base, int(-46 * (0.5 + grain))))
    light = colour.hex_to_linear(colour.shift(base, int(20 * (0.5 + grain))))
    ramp = _ramp(nodes, [(0.30, dark), (0.72, light)])
    links.new(wave.outputs["Color"], ramp.inputs["Fac"])

    mix, slot_a, slot_b, out = _new_mix(nodes, links, 0.18 * grain)
    links.new(ramp.outputs["Color"], slot_a)
    links.new(fibre.outputs["Color"], slot_b)
    links.new(out, bsdf.inputs["Base Color"])

    # Latewood is slightly glossier than earlywood.
    rough_ramp = _ramp(nodes, [
        (0.25, (prior.roughness + 0.10,) * 3 + (1.0,)),
        (0.80, (max(MIN_ROUGHNESS, prior.roughness - 0.10),) * 3 + (1.0,)),
    ])
    links.new(wave.outputs["Color"], rough_ramp.inputs["Fac"])
    links.new(rough_ramp.outputs["Color"], bsdf.inputs["Roughness"])

    _bump(nodes, links, wave.outputs["Color"], 0.30 * grain, bsdf.inputs["Normal"])


def _recipe_veined(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Sparse high-contrast veins over a stone field — marble, travertine."""
    vector = _coords(nodes, links, (1.0, 1.0, 1.0))

    veins = _noise(nodes, vector, links, scale=3.2, detail=8.0,
                   roughness=0.62, distortion=2.4)

    # A narrow ramp window is what turns smooth noise into discrete veins;
    # a wide one gives clouds, which reads as concrete rather than marble.
    edge = 0.5 - 0.10 * grain
    vein_colour = colour.hex_to_linear(colour.shift(base, -95 if colour.luminance(base) > 0.4 else 70))
    field = colour.hex_to_linear(base)
    ramp = _ramp(nodes, [(edge, field), (edge + 0.06, vein_colour), (edge + 0.13, field)])
    links.new(veins.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

    # Faint large-scale mottling so the slab is not uniform between veins.
    mottle = _noise(nodes, vector, links, scale=1.1, detail=3.0)
    rough_ramp = _ramp(nodes, [
        (0.35, (prior.roughness,) * 3 + (1.0,)),
        (0.75, (min(MAX_ROUGHNESS, prior.roughness + 0.08),) * 3 + (1.0,)),
    ])
    links.new(mottle.outputs["Fac"], rough_ramp.inputs["Fac"])
    links.new(rough_ramp.outputs["Color"], bsdf.inputs["Roughness"])

    _bump(nodes, links, veins.outputs["Fac"], 0.10 * grain, bsdf.inputs["Normal"])


def _recipe_speckle(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Voronoi chips in a matrix — terrazzo, granite."""
    vector = _coords(nodes, links, (1.0, 1.0, 1.0))

    voronoi = nodes.new("ShaderNodeTexVoronoi")
    voronoi.feature = "F1"
    voronoi.inputs["Scale"].default_value = 26.0 + 30.0 * grain
    _set_input(voronoi, ["Randomness"], 1.0)
    links.new(vector, voronoi.inputs["Vector"])

    chip_light = colour.hex_to_linear(colour.shift(base, 52))
    chip_dark = colour.hex_to_linear(colour.shift(base, -58))
    ramp = _ramp(nodes, [(0.0, chip_dark), (0.45, colour.hex_to_linear(base)),
                         (0.85, chip_light)])
    links.new(voronoi.outputs["Color"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

    _set_input(bsdf, ["Roughness"], _clamp_roughness(prior.roughness))
    _bump(nodes, links, voronoi.outputs["Distance"], 0.12 * grain, bsdf.inputs["Normal"])


def _recipe_weave(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Crossed threads plus fine fuzz — linen, wool, bouclé, carpet, rattan."""
    vector = _coords(nodes, links, (1.0, 1.0, 1.0))

    warp = nodes.new("ShaderNodeTexWave")
    warp.wave_type = "BANDS"
    warp.bands_direction = "X"
    warp.inputs["Scale"].default_value = 120.0 * (0.5 + grain)
    links.new(vector, warp.inputs["Vector"])

    weft = nodes.new("ShaderNodeTexWave")
    weft.wave_type = "BANDS"
    weft.bands_direction = "Y"
    weft.inputs["Scale"].default_value = 120.0 * (0.5 + grain)
    links.new(vector, weft.inputs["Vector"])

    cross, cross_a, cross_b, cross_out = _new_mix(nodes, links, 0.5)
    links.new(warp.outputs["Color"], cross_a)
    links.new(weft.outputs["Color"], cross_b)

    fuzz = _noise(nodes, vector, links, scale=260.0, detail=2.0, roughness=0.8)

    ramp = _ramp(nodes, [
        (0.30, colour.hex_to_linear(colour.shift(base, -20))),
        (0.70, colour.hex_to_linear(colour.shift(base, 14))),
    ])
    links.new(cross_out, ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

    _set_input(bsdf, ["Roughness"], _clamp_roughness(prior.roughness))

    # Textiles scatter at grazing angles. Sheen is what separates velvet from
    # painted cardboard, and it is the one Principled input that matters most
    # for fabric looking like fabric.
    _set_input(bsdf, ["Sheen Weight", "Sheen"], min(1.0, 0.25 + grain * 0.55))
    _set_input(bsdf, ["Sheen Roughness"], 0.35)
    _set_input(bsdf, ["Sheen Tint"], colour.hex_to_linear(colour.shift(base, 60)))

    combined, combined_a, combined_b, combined_out = _new_mix(nodes, links, 0.45)
    links.new(cross_out, combined_a)
    links.new(fuzz.outputs["Color"], combined_b)
    _bump(nodes, links, combined_out, 0.45 * grain, bsdf.inputs["Normal"])


def _recipe_brick(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Course-laid brick with mortar."""
    vector = _coords(nodes, links, (1.0, 1.0, 1.0))

    brick = nodes.new("ShaderNodeTexBrick")
    brick.inputs["Scale"].default_value = 6.0
    _set_input(brick, ["Mortar Size"], 0.018)
    _set_input(brick, ["Mortar Smooth"], 0.10)
    _set_input(brick, ["Bias"], 0.0)
    _set_input(brick, ["Brick Width"], 0.5)
    _set_input(brick, ["Row Height"], 0.22)
    _set_input(brick, ["Color1"], colour.hex_to_linear(colour.shift(base, -18)))
    _set_input(brick, ["Color2"], colour.hex_to_linear(colour.shift(base, 26)))
    _set_input(brick, ["Mortar"], colour.hex_to_linear("#BFB8AC"))
    links.new(vector, brick.inputs["Vector"])
    links.new(brick.outputs["Color"], bsdf.inputs["Base Color"])

    _set_input(bsdf, ["Roughness"], _clamp_roughness(prior.roughness))
    _bump(nodes, links, brick.outputs["Fac"], 0.70 * grain, bsdf.inputs["Normal"])


def _recipe_tiled(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Square tiles with grout — porcelain, ceramic, subway, mosaic."""
    vector = _coords(nodes, links, (1.0, 1.0, 1.0))

    tile = nodes.new("ShaderNodeTexBrick")
    tile.inputs["Scale"].default_value = 4.0
    _set_input(tile, ["Mortar Size"], 0.012)
    _set_input(tile, ["Bias"], 0.0)
    _set_input(tile, ["Brick Width"], 0.5)
    _set_input(tile, ["Row Height"], 0.5)   # square, not running bond
    _set_input(tile, ["Offset"], 0.0)
    _set_input(tile, ["Color1"], colour.hex_to_linear(base))
    _set_input(tile, ["Color2"], colour.hex_to_linear(colour.shift(base, -10)))
    _set_input(tile, ["Mortar"], colour.hex_to_linear(colour.shift(base, -46)))
    links.new(vector, tile.inputs["Vector"])
    links.new(tile.outputs["Color"], bsdf.inputs["Base Color"])

    # Grout is matte where the tile face is not; driving roughness from the
    # same signal is what makes the joint read as a joint.
    rough_ramp = _ramp(nodes, [
        (0.0, (0.85,) * 3 + (1.0,)),
        (0.5, (_clamp_roughness(prior.roughness),) * 3 + (1.0,)),
    ])
    links.new(tile.outputs["Fac"], rough_ramp.inputs["Fac"])
    links.new(rough_ramp.outputs["Color"], bsdf.inputs["Roughness"])

    _bump(nodes, links, tile.outputs["Fac"], 0.35 * grain, bsdf.inputs["Normal"])


def _recipe_brushed(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Directional micro-scratches — brushed steel, brass, chrome.

    The brushing varies *roughness*, not colour. A metal's colour is its alloy;
    what the finish changes is how the highlight stretches, so the noise is
    scaled hard along one axis and drives roughness alone.
    """
    vector = _coords(nodes, links, (1.0, 220.0, 220.0))
    scratches = _noise(nodes, vector, links, scale=8.0, detail=2.0, roughness=0.8)

    _set_input(bsdf, ["Base Color"], colour.hex_to_linear(base))
    _set_input(bsdf, ["Metallic"], prior.metallic)

    spread = 0.16 * grain + 0.03
    rough_ramp = _ramp(nodes, [
        (0.30, (max(MIN_ROUGHNESS, prior.roughness - spread),) * 3 + (1.0,)),
        (0.70, (min(MAX_ROUGHNESS, prior.roughness + spread),) * 3 + (1.0,)),
    ])
    links.new(scratches.outputs["Fac"], rough_ramp.inputs["Fac"])
    links.new(rough_ramp.outputs["Color"], bsdf.inputs["Roughness"])

    _set_input(bsdf, ["Anisotropic"], min(0.8, 0.35 + grain))
    _bump(nodes, links, scratches.outputs["Fac"], 0.06 * grain, bsdf.inputs["Normal"])


def _recipe_noise(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Blotching at two scales — concrete, stone, plaster."""
    vector = _coords(nodes, links, (1.0, 1.0, 1.0))

    broad = _noise(nodes, vector, links, scale=2.6, detail=5.0, roughness=0.55)
    fine = _noise(nodes, vector, links, scale=48.0, detail=3.0, roughness=0.7)

    ramp = _ramp(nodes, [
        (0.34, colour.hex_to_linear(colour.shift(base, int(-30 * grain)))),
        (0.68, colour.hex_to_linear(colour.shift(base, int(22 * grain)))),
    ])
    links.new(broad.outputs["Fac"], ramp.inputs["Fac"])

    mix, slot_a, slot_b, out = _new_mix(nodes, links, 0.12 * grain)
    links.new(ramp.outputs["Color"], slot_a)
    links.new(fine.outputs["Color"], slot_b)
    links.new(out, bsdf.inputs["Base Color"])

    _set_input(bsdf, ["Roughness"], _clamp_roughness(prior.roughness))
    _bump(nodes, links, fine.outputs["Fac"], 0.22 * grain, bsdf.inputs["Normal"])


def _recipe_grain(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Fine pebbled surface — leather, ceramic glaze."""
    vector = _coords(nodes, links, (1.0, 1.0, 1.0))
    pores = _noise(nodes, vector, links, scale=180.0, detail=2.0, roughness=0.75)

    _set_input(bsdf, ["Base Color"], colour.hex_to_linear(base))
    _set_input(bsdf, ["Roughness"], _clamp_roughness(prior.roughness))
    _set_input(bsdf, ["Sheen Weight", "Sheen"], 0.10)
    _bump(nodes, links, pores.outputs["Fac"], 0.35 * grain, bsdf.inputs["Normal"])


def _recipe_flat(nodes, links, bsdf, base, prior, grain: float) -> None:
    """Plain shading — paint, gypsum, plastic, glass.

    Not literally flat: even matte emulsion has a faint roller texture, and a
    perfectly uniform wall is one of the strongest cues that a render is CG.
    """
    _set_input(bsdf, ["Base Color"], colour.hex_to_linear(base))
    _set_input(bsdf, ["Roughness"], _clamp_roughness(prior.roughness))

    if grain > 0.01:
        vector = _coords(nodes, links, (1.0, 1.0, 1.0))
        tooth = _noise(nodes, vector, links, scale=320.0, detail=2.0)
        _bump(nodes, links, tooth.outputs["Fac"], 0.06 * grain, bsdf.inputs["Normal"])


RECIPES = {
    "wood_grain": _recipe_wood_grain,
    "veined": _recipe_veined,
    "speckle": _recipe_speckle,
    "weave": _recipe_weave,
    "brick": _recipe_brick,
    "tiled": _recipe_tiled,
    "brushed": _recipe_brushed,
    "noise": _recipe_noise,
    "grain": _recipe_grain,
    "flat": _recipe_flat,
}


def _clamp_roughness(value: float) -> float:
    return min(MAX_ROUGHNESS, max(MIN_ROUGHNESS, value))


# ---------------------------------------------------------------------------
# Material construction
# ---------------------------------------------------------------------------


def build_material(name: str, color_hex: str, material: str,
                   roughness_bias: float = 0.0) -> "bpy.types.Material":
    """Create one procedural material for a species or family."""
    prior = catalog.get_material(material)
    recipe = RECIPES.get(prior.texture, _recipe_flat)

    datablock = bpy.data.materials.new(name=name)
    datablock.use_nodes = True
    tree = datablock.node_tree
    nodes, links = tree.nodes, tree.links
    nodes.clear()

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (0, 0)
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (320, 0)
    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    # Defaults first; the recipe overrides what it cares about.
    _set_input(bsdf, ["Base Color"], colour.hex_to_linear(color_hex))
    _set_input(bsdf, ["Metallic"], prior.metallic)
    _set_input(bsdf, ["Roughness"], _clamp_roughness(prior.roughness + roughness_bias))

    # The style's roughness bias is folded in before the recipe runs, so a
    # recipe that derives a roughness *range* from the prior widens around the
    # biased value rather than around the catalog one.
    biased = dataclasses.replace(
        prior, roughness=_clamp_roughness(prior.roughness + roughness_bias)
    )
    recipe(nodes, links, bsdf, color_hex, biased, max(0.0, min(1.0, prior.grain)))

    _apply_family_traits(bsdf, prior)

    datablock["archx3d_material"] = material
    datablock["archx3d_family"] = catalog.material_family(material)
    datablock["archx3d_texture"] = prior.texture
    return datablock


def _apply_family_traits(bsdf, prior) -> None:
    """Physical traits that belong to a family regardless of recipe."""
    family = prior.base or prior.name

    if family == "glass":
        _set_input(bsdf, ["Transmission Weight", "Transmission"], 0.92)
        _set_input(bsdf, ["IOR"], 1.45)
        _set_input(bsdf, ["Roughness"], 0.03)
    elif family == "metal":
        _set_input(bsdf, ["Metallic"], max(0.85, prior.metallic))
    elif family in ("marble", "granite", "ceramic"):
        # Polished stone has a shallow subsurface glow; without it white
        # marble renders like painted plaster.
        _set_input(bsdf, ["Specular IOR Level", "Specular"], 0.6)
        _set_input(bsdf, ["Coat Weight", "Clearcoat"], 0.25)
        _set_input(bsdf, ["Coat Roughness", "Clearcoat Roughness"], 0.10)
    elif family == "paint_gloss":
        _set_input(bsdf, ["Coat Weight", "Clearcoat"], 0.35)


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


class MaterialLibrary:
    """Creates and caches materials for one scene.

    Dozens of objects typically resolve to a handful of distinct
    (species, colour) pairs. Caching keeps the GLB small and the shader compile
    count down, which matters: each unique node graph is a separate compile.

    The library also owns the *decisions* — style refinement and palette
    tinting — so every consumer gets the same answer for the same object and
    the reasoning is logged once.
    """

    def __init__(self, style: str = "unknown", style_confidence: float = 0.0):
        self.style = style
        self.style_confidence = style_confidence
        self._cache: Dict[Tuple[str, str], "bpy.types.Material"] = {}
        self.created = 0
        self.log: List[str] = []
        self._roughness_bias = styles.roughness_bias(style)

    # -- core ---------------------------------------------------------------

    def get(self, color_hex: str, material: str, name_hint: Optional[str] = None):
        """A cached procedural material for an exact (colour, species) pair."""
        key = (colour.to_hex(colour.to_unit(color_hex)), material)
        if key in self._cache:
            return self._cache[key]

        name = name_hint or f"M_{material}_{key[0].lstrip('#')}"
        datablock = build_material(name, key[0], material, self._roughness_bias)
        self._cache[key] = datablock
        self.created += 1
        return datablock

    def resolve(self, observed_material: Optional[str], surface: str = "object"):
        """Style-refined species for an observed material."""
        return styles.resolve_material(
            observed_material, self.style, surface, self.style_confidence
        )

    # -- surfaces -----------------------------------------------------------

    def surface(self, finish, surface: str, room_palette=None, name: Optional[str] = None):
        """Build a wall, floor or ceiling material.

        ``finish`` is a :class:`vision.schema.Finish` or ``None``.
        """
        observed = getattr(finish, "material", None)
        base_hex = getattr(finish, "color_hex", None)

        decision = self.resolve(observed, surface)
        tint = palette_mod.for_surface(
            base_hex or catalog.get_material(decision.material).color_hex,
            decision.material, room_palette, surface,
        )

        self.log.append(
            f"{surface:<8} {decision.material:<18} [{decision.source}] {tint}"
        )
        return self.get(tint.color_hex, decision.material, name)

    # -- objects ------------------------------------------------------------

    def for_object(self, scene_object, room_palette=None):
        """Three slots per object: primary surface, frame/legs, accent.

        The furniture builders expect exactly three, and having the library
        derive the secondary tones means a two-tone piece needs only one
        observed colour.
        """
        decision = self.resolve(scene_object.material, "object")
        tint = palette_mod.for_object(
            scene_object.color_hex, decision.material,
            getattr(scene_object, "group", "furniture"), room_palette,
        )
        primary = self.get(tint.color_hex, decision.material)

        # Legs and frames are structural: timber or metal, never upholstery.
        frame_material = decision.material
        if catalog.material_family(decision.material) in ("fabric", "carpet", "leather"):
            frame_material = self.resolve("wood", "object").material
        frame = self.get(
            styles.trim_colour(self.style, colour.shift(tint.color_hex, -34)),
            frame_material,
        )

        accent = self.get(
            palette_mod.accent_for(room_palette, colour.shift(tint.color_hex, 20)),
            decision.material,
        )
        return [primary, frame, accent]

    # -- reporting ----------------------------------------------------------

    def summary(self) -> str:
        families: Dict[str, int] = {}
        for (_, material) in self._cache:
            family = catalog.material_family(material)
            families[family] = families.get(family, 0) + 1
        breakdown = ", ".join(f"{name} x{count}" for name, count in sorted(families.items()))
        return f"{self.created} procedural materials ({breakdown})"
