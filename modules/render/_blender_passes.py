"""
ArchX3D — Auxiliary pass rendering, inside Blender
==================================================
Renders the albedo, depth, normal, material-ID and object-ID passes that the
evaluation engine reads. Imports ``bpy``; see :mod:`render.passes` for the
codec, which does not.

How a pass is produced
----------------------
Not through Blender's compositor. Blender 5.0's File Output node writes
multilayer OpenEXR and nothing else, and reading that from Python would mean a
new binary dependency for a diagnostic. Instead each quantity is *rendered*:
the scene is temporarily re-shaded so that emission colour carries the value,
and the ordinary render path writes an ordinary PNG.

Two mechanisms, chosen per pass by what varies:

**View-layer override** (``depth``, ``normal``, ``object_id``) — one material
replaces every surface. Correct when the value is a property of the geometry
or the object rather than of the material. Object identity reaches the shader
through ``ShaderNodeObjectInfo``'s colour output, which reads the per-object
``object.color`` we set beforehand.

**Node rewiring** (``albedo``, ``material_id``) — each material's own output
link is redirected through an emission node. Correct when the value *is* a
property of the material, and for albedo it has a second virtue: feeding the
Principled node's existing Base Color input into the emission preserves the
procedural texture, so the material axis can measure grain rather than a flat
average.

Everything is restored afterwards and the ``.blend`` is never saved, so a
pass render leaves no trace in the file it read.

Determinism
-----------
Pass renders inherit every setting from ``_blender_render.apply_settings`` and
change exactly three things: the view transform (``Raw`` for the data passes,
so the linear value reaches the byte unaltered), the world (dropped, so the
background is an unambiguous zero), and the shading. Emission is unlit, so
lights, shadows and sampling cannot influence a data pass at all.
"""

from __future__ import annotations

import os
import time

import bpy

from . import passes as passes_mod

#: Custom property ``blender_furniture`` writes on every built object, holding
#: the scene graph id. It is what makes an object-ID mask nameable.
GRAPH_ID_PROPERTY = "archx3d_id"


def _log(message: str) -> None:
    print(f"[PREVIEW] {message}")


# ---------------------------------------------------------------------------
# Encoding materials
# ---------------------------------------------------------------------------


def _emission_material(name: str):
    """A bare Emission -> Output material, ready for its colour to be wired."""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    return material, tree, emission


def build_depth_material(depth_range: float):
    """Emission = camera-Z distance / range, so the byte decodes to metres."""
    material, tree, emission = _emission_material("ArchX3D_Pass_Depth")
    camera = tree.nodes.new("ShaderNodeCameraData")
    divide = tree.nodes.new("ShaderNodeMath")
    divide.operation = "DIVIDE"
    divide.inputs[1].default_value = max(0.001, float(depth_range))
    tree.links.new(camera.outputs["View Z Depth"], divide.inputs[0])
    tree.links.new(divide.outputs[0], emission.inputs["Color"])
    return material


def build_normal_material():
    """Emission = (world normal + 1) / 2, the usual normal-map convention."""
    material, tree, emission = _emission_material("ArchX3D_Pass_Normal")
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    scale = tree.nodes.new("ShaderNodeVectorMath")
    scale.operation = "MULTIPLY_ADD"
    scale.inputs[1].default_value = (0.5, 0.5, 0.5)
    scale.inputs[2].default_value = (0.5, 0.5, 0.5)
    tree.links.new(geometry.outputs["Normal"], scale.inputs[0])
    tree.links.new(scale.outputs[0], emission.inputs["Color"])
    return material


def build_object_id_material():
    """Emission = the object's own colour, which we load with its index."""
    material, tree, emission = _emission_material("ArchX3D_Pass_ObjectID")
    info = tree.nodes.new("ShaderNodeObjectInfo")
    tree.links.new(info.outputs["Color"], emission.inputs["Color"])
    return material


# ---------------------------------------------------------------------------
# Reversible scene edits
# ---------------------------------------------------------------------------


class _ObjectColours:
    """Loads each renderable object's index into ``object.color``.

    Indices start at 1 so that 0 unambiguously means background. The mapping
    back to scene graph ids comes from the ``archx3d_id`` custom property that
    ``blender_furniture`` writes; objects without one — walls, floor, ceiling,
    the architectural shell — are indexed too and named by their Blender
    object name, because "the render is mostly wall" is a useful thing for a
    finding to be able to say.
    """

    def __init__(self) -> None:
        self._saved = []
        self.index_map = {}

    def __enter__(self) -> "_ObjectColours":
        index = 1
        for obj in bpy.data.objects:
            if obj.type not in ("MESH", "CURVE", "SURFACE", "META", "FONT"):
                continue
            self._saved.append((obj, tuple(obj.color)))
            obj.color = passes_mod.encode_index(index) + (1.0,)
            self.index_map[index] = str(obj.get(GRAPH_ID_PROPERTY) or obj.name)
            index += 1
            if index > passes_mod.MAX_INDEX:
                _log("more objects than the ID pass can encode; the rest are unindexed")
                break
        return self

    def __exit__(self, *exc) -> None:
        for obj, colour in self._saved:
            try:
                obj.color = colour
            except ReferenceError:
                pass  # object removed mid-render; nothing to restore
        return None


class _MaterialOverride:
    """Swaps the view layer's material override, then puts it back."""

    def __init__(self, material) -> None:
        self.material = material
        self._previous = None
        self._layer = None

    def __enter__(self) -> "_MaterialOverride":
        self._layer = bpy.context.view_layer
        self._previous = self._layer.material_override
        self._layer.material_override = self.material
        return self

    def __exit__(self, *exc) -> None:
        if self._layer is not None:
            self._layer.material_override = self._previous
        return None


class _PointSampled:
    """Turns anti-aliasing off, for the passes where averaging is meaningless.

    This is not a quality trade — it is a correctness one. Anti-aliasing
    averages neighbouring samples, and at a silhouette that averages *indices*:
    material 4 beside material 27 yields a pixel claiming to be material 15,
    which the evaluation engine would then mask as a third material that does
    not exist. Depth and normals suffer the same way — the mean of 2 m and 6 m
    is a surface that is not there, and the mean of two unit normals is not a
    unit normal.

    One sample and a degenerate filter width give exact, if jagged, data. The
    albedo pass keeps its anti-aliasing: it is a colour image, compared
    statistically, where averaging is exactly right.
    """

    def __init__(self) -> None:
        self._saved = ()

    def __enter__(self) -> "_PointSampled":
        scene = bpy.context.scene
        eevee = getattr(scene, "eevee", None)
        self._saved = (
            scene.render.filter_size,
            getattr(eevee, "taa_render_samples", None),
        )
        scene.render.filter_size = 0.01
        if eevee is not None and hasattr(eevee, "taa_render_samples"):
            eevee.taa_render_samples = 1
        return self

    def __exit__(self, *exc) -> None:
        scene = bpy.context.scene
        filter_size, samples = self._saved
        scene.render.filter_size = filter_size
        eevee = getattr(scene, "eevee", None)
        if eevee is not None and samples is not None:
            eevee.taa_render_samples = samples
        return None


class _WorldOff:
    """Renders data passes against black, so index 0 means "no surface"."""

    def __init__(self) -> None:
        self._previous = None

    def __enter__(self) -> "_WorldOff":
        scene = bpy.context.scene
        self._previous = scene.world
        scene.world = None
        return self

    def __exit__(self, *exc) -> None:
        bpy.context.scene.world = self._previous
        return None


class _SurfaceRewire:
    """Redirects every material's surface output through an emission node.

    ``colour_for(material, index)`` returns either a constant linear colour or
    ``None`` meaning "use whatever feeds this material's Base Color", which is
    how the albedo pass keeps its procedural texture.
    """

    def __init__(self, colour_for) -> None:
        self.colour_for = colour_for
        self._undo = []
        self.index_map = {}

    def __enter__(self) -> "_SurfaceRewire":
        for index, material in enumerate(bpy.data.materials, start=1):
            if not material.use_nodes or material.node_tree is None:
                continue
            self.index_map[index] = material.name
            try:
                self._rewire(material, index)
            except Exception as exc:  # noqa: BLE001 - one material, not the pass
                _log(f"could not rewire {material.name} for the pass: {exc}")
        return self

    def _rewire(self, material, index: int) -> None:
        tree = material.node_tree
        output = next(
            (n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial"), None
        )
        if output is None:
            return
        surface = output.inputs["Surface"]
        original = surface.links[0].from_socket if surface.is_linked else None

        emission = tree.nodes.new("ShaderNodeEmission")
        emission.inputs["Strength"].default_value = 1.0

        colour = self.colour_for(material, index)
        if colour is None:
            source = _base_colour_source(tree)
            if isinstance(source, tuple):
                emission.inputs["Color"].default_value = source
            elif source is not None:
                tree.links.new(source, emission.inputs["Color"])
        else:
            emission.inputs["Color"].default_value = tuple(colour) + (1.0,)

        tree.links.new(emission.outputs[0], surface)
        self._undo.append((tree, emission, original, surface))

    def __exit__(self, *exc) -> None:
        for tree, emission, original, surface in self._undo:
            try:
                tree.nodes.remove(emission)
                if original is not None:
                    tree.links.new(original, surface)
            except (ReferenceError, RuntimeError):
                pass
        return None


def _base_colour_source(tree):
    """What feeds a material's albedo: a socket to link, or a constant colour.

    Looks for a Principled BSDF because that is what every ArchX3D material is
    built on. Returning the *linked* socket rather than a sampled colour is
    what preserves wood grain, tile joints and weave in the albedo pass.
    """
    principled = next(
        (n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None
    )
    if principled is None:
        return None
    base = principled.inputs.get("Base Color")
    if base is None:
        return None
    if base.is_linked:
        return base.links[0].from_socket
    return tuple(base.default_value)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_passes(beauty_path: str, names, settings: dict) -> "tuple[dict, dict]":
    """Render every requested pass beside ``beauty_path``.

    Returns ``({pass_name: path}, index_maps)``. Never raises: a pass that
    fails is reported and omitted, and the evaluation engine treats an absent
    pass as an unmeasured axis rather than a zero.

    The camera, resolution and every determinism setting are already in place
    — this is called immediately after the beauty render of the same task, and
    deliberately does not touch them.
    """
    names = passes_mod.normalise(names)
    if not names:
        return {}, {}

    scene = bpy.context.scene
    depth_range = float(settings.get("depth_range", passes_mod.DEFAULT_DEPTH_RANGE))
    previous_transform = scene.view_settings.view_transform

    written = {}
    index_maps = {}
    scratch = []

    try:
        for name in names:
            path = passes_mod.pass_filename(beauty_path, name)
            started = time.perf_counter()
            try:
                _set_transform(scene, passes_mod.VIEW_TRANSFORM.get(name, "Raw"))
                indices = _render_one(scene, name, path, depth_range, scratch)
            except Exception as exc:  # noqa: BLE001 - one pass, not the batch
                _log(f"pass {name} failed: {type(exc).__name__}: {exc}")
                continue

            if not os.path.exists(path):
                _log(f"pass {name} reported success but wrote nothing")
                continue

            written[name] = path
            if indices:
                index_maps.update(indices)
            _log(f"  pass {name}: {int((time.perf_counter() - started) * 1000)} ms")
    finally:
        _set_transform(scene, previous_transform)
        for material in scratch:
            try:
                bpy.data.materials.remove(material)
            except (ReferenceError, RuntimeError):
                pass

    return written, index_maps


def _render_one(scene, name: str, path: str, depth_range: float, scratch) -> dict:
    """Set the scene up for one pass, render it, return any index mapping."""
    scene.render.filepath = path[:-4] if path.lower().endswith(".png") else path

    if name == passes_mod.DEPTH:
        material = build_depth_material(depth_range)
        scratch.append(material)
        with _WorldOff(), _PointSampled(), _MaterialOverride(material):
            _render()
        return {}

    if name == passes_mod.NORMAL:
        material = build_normal_material()
        scratch.append(material)
        with _WorldOff(), _PointSampled(), _MaterialOverride(material):
            _render()
        return {}

    if name == passes_mod.OBJECT_ID:
        material = build_object_id_material()
        scratch.append(material)
        with _WorldOff(), _PointSampled(), _ObjectColours() as colours,                 _MaterialOverride(material):
            _render()
        return {"objects": colours.index_map}

    if name == passes_mod.MATERIAL_ID:
        with _WorldOff(), _PointSampled(), _SurfaceRewire(
            lambda material, index: passes_mod.encode_index(index)
        ) as rewire:
            _render()
        return {"materials": rewire.index_map}

    if name == passes_mod.ALBEDO:
        # ``None`` means "keep this material's own base colour input", which is
        # what preserves the procedural detail the material axis measures.
        with _WorldOff(), _SurfaceRewire(lambda material, index: None):
            _render()
        return {}

    raise ValueError(f"unknown pass {name!r}")


def _render() -> None:
    bpy.ops.render.render(write_still=True)


def _set_transform(scene, transform: str) -> None:
    """Colour management for this pass, falling back if a name is unavailable.

    ``Raw`` is what makes the data passes decodable — it writes the linear
    value straight to the byte. If a build does not offer it the pass is still
    written, but the evaluation engine would misread it, so the fallback is
    reported loudly rather than silently.
    """
    for candidate in (transform, "Raw", "Standard"):
        try:
            scene.view_settings.view_transform = candidate
            if candidate != transform:
                _log(f"view transform {transform!r} unavailable; used {candidate!r} "
                     f"— pass values may not decode correctly")
            return
        except (TypeError, ValueError):
            continue
