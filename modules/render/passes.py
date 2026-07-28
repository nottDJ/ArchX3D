"""
ArchX3D — Render pass codec
===========================
What the auxiliary render passes mean and how their pixels decode back into
numbers. Shared by the two sides that must agree: ``_blender_passes`` writes
them, ``evaluation`` reads them.

Why encode passes as PNGs at all
--------------------------------
Blender's native route for auxiliary passes is a multilayer OpenEXR, and in
Blender 5.0 the File Output node emits nothing else. Reading multilayer EXR
from Python needs OpenEXR or OpenImageIO, neither of which this project has
and neither of which is a reasonable dependency for a diagnostic pass.

So each pass is rendered as an ordinary 8-bit PNG through the ordinary render
path, with the quantity encoded into emission colour and the ``Raw`` view
transform switched on so the linear value survives to the file byte-exactly.
Verified: a linear ``7/255`` comes back as the integer ``7``.

That gives passes that any Pillow install can read, that a human can look at,
and that cost one extra render each instead of a new dependency.

The five passes
---------------
``albedo``       Surface colour with lighting removed, sRGB-encoded so it is
                 directly comparable with a photograph. Procedural texture
                 detail is preserved — the material axis measures it.
``depth``        Camera-Z distance in metres, ``byte / 255 * depth_range``.
``normal``       World-space surface normal, ``byte / 255 * 2 - 1`` per axis.
``material_id``  Material index, encoded across R and G.
``object_id``    Object index, encoded across R and G.

What 8 bits costs
-----------------
Depth quantises to ``depth_range / 255`` — 8 cm at the 20 m default. That is
fine for the layout axis, which compares *distributions* of depth, and is not
used for the per-object displacement figure: that comes from projecting the
scene graph analytically, where the precision is the graph's, not the image's.
The ID passes are exact — they are integers, not measurements.

Alpha is not used: the background renders black, and index 0 means "nothing",
which is unambiguous because Blender's object and material indices start at 1
in our assignment.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

#: Surface colour, lighting removed.
ALBEDO = "albedo"
#: Camera-Z distance in metres.
DEPTH = "depth"
#: World-space surface normal.
NORMAL = "normal"
#: Material index per pixel.
MATERIAL_ID = "material_id"
#: Object index per pixel.
OBJECT_ID = "object_id"

#: Every pass this pipeline knows how to render, in render order.
ALL_PASSES: Tuple[str, ...] = (ALBEDO, DEPTH, NORMAL, MATERIAL_ID, OBJECT_ID)

#: Rendered by default. All of them: each costs one extra ~250 ms render, and
#: the evaluation engine degrades an axis rather than guessing when one is
#: absent, so a missing pass is a silently weaker report.
DEFAULT_PASSES: Tuple[str, ...] = ALL_PASSES

#: Metres that map onto the full 0–255 depth range. 20 m comfortably covers a
#: room-scale interior view; anything beyond clamps to white and is reported
#: as "beyond range" rather than as a near-far confusion.
DEFAULT_DEPTH_RANGE = 20.0

#: Which view transform each pass must be rendered under. ``Raw`` writes the
#: linear value straight to the byte, which is what makes decoding exact;
#: ``Standard`` sRGB-encodes, which is what makes albedo comparable with a
#: photograph.
VIEW_TRANSFORM: Dict[str, str] = {
    ALBEDO: "Standard",
    DEPTH: "Raw",
    NORMAL: "Raw",
    MATERIAL_ID: "Raw",
    OBJECT_ID: "Raw",
}

#: Passes whose pixels are indices rather than measurements.
INDEX_PASSES: Tuple[str, ...] = (MATERIAL_ID, OBJECT_ID)

#: Largest index the two-channel encoding can carry.
MAX_INDEX = 255 * 256 + 255


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def pass_filename(beauty_path: str, pass_name: str) -> str:
    """``…/viewpoint_01.png`` + ``albedo`` -> ``…/viewpoint_01_albedo.png``.

    Beside the beauty render rather than in a subdirectory: the room folder
    stays flat, and a pass is obviously an attribute of the preview next to it
    rather than a separate artefact.
    """
    stem = beauty_path[:-4] if beauty_path.lower().endswith(".png") else beauty_path
    return f"{stem}_{pass_name}.png"


def normalise(names: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Validate a configured pass list, preserving the canonical render order.

    Unknown names are dropped rather than raising — ``config.json`` is
    hand-edited, and a typo should cost a pass, not the run.
    """
    if names is None:
        return DEFAULT_PASSES
    if isinstance(names, str):
        names = [part.strip() for part in names.split(",")]
    wanted = {str(name).strip().lower() for name in names if str(name).strip()}
    if "all" in wanted:
        return ALL_PASSES
    if "none" in wanted:
        return ()
    return tuple(name for name in ALL_PASSES if name in wanted)


# ---------------------------------------------------------------------------
# Index encoding
# ---------------------------------------------------------------------------


def encode_index(index: int) -> Tuple[float, float, float]:
    """Index -> linear RGB, low byte in red, high byte in green.

    Two channels rather than one because a large building can exceed 255
    materials, and losing the distinction between material 3 and material 259
    would silently merge two regions of the image.
    """
    value = max(0, min(MAX_INDEX, int(index)))
    return ((value % 256) / 255.0, (value // 256) / 255.0, 0.0)


def decode_index(red, green):
    """The inverse of :func:`encode_index`, from 8-bit channel values.

    Works on scalars and, elementwise, on numpy channel planes — the
    evaluation engine decodes a whole ID pass at once, and doing that a pixel
    at a time in Python would cost more than the render did.
    """
    try:
        return int(red) + int(green) * 256
    except TypeError:  # array-like: let it broadcast
        return red + green * 256


# ---------------------------------------------------------------------------
# Measurement decoding
# ---------------------------------------------------------------------------


def decode_depth(byte, depth_range: float = DEFAULT_DEPTH_RANGE):
    """Depth byte -> metres from the camera plane.

    Scalar or, elementwise, a whole depth plane — the evaluation engine
    decodes the full image at once.
    """
    scale = float(depth_range) / 255.0
    try:
        return float(byte) * scale
    except TypeError:  # array-like: let it broadcast
        return byte * scale


def encode_depth(metres: float, depth_range: float = DEFAULT_DEPTH_RANGE) -> float:
    """Metres -> the linear value the render must emit."""
    if depth_range <= 0:
        return 0.0
    return max(0.0, min(1.0, float(metres) / float(depth_range)))


def decode_normal(red: float, green: float, blue: float) -> Tuple[float, float, float]:
    """Normal bytes -> a world-space unit vector in ``[-1, 1]`` per axis."""
    return (
        float(red) / 255.0 * 2.0 - 1.0,
        float(green) / 255.0 * 2.0 - 1.0,
        float(blue) / 255.0 * 2.0 - 1.0,
    )


# ---------------------------------------------------------------------------
# The index map
# ---------------------------------------------------------------------------


class IndexMap:
    """Which index in an ID pass stands for which object or material.

    Written by the render side into the manifest, because the mapping is a
    property of the build rather than of any one viewpoint, and reading it is
    the whole point of the ID passes: without it an object mask is an
    anonymous blob.

    Index 0 is reserved for "background / unassigned" and never resolves.
    """

    def __init__(self, objects: Optional[Dict[Any, str]] = None,
                 materials: Optional[Dict[Any, str]] = None) -> None:
        self.objects: Dict[int, str] = {int(k): str(v) for k, v in (objects or {}).items()}
        self.materials: Dict[int, str] = {int(k): str(v) for k, v in (materials or {}).items()}

    def object_for(self, index: int) -> str:
        return self.objects.get(int(index), "")

    def material_for(self, index: int) -> str:
        return self.materials.get(int(index), "")

    def to_dict(self) -> Dict[str, Dict[str, str]]:
        return {
            "objects": {str(k): v for k, v in sorted(self.objects.items())},
            "materials": {str(k): v for k, v in sorted(self.materials.items())},
        }

    @staticmethod
    def from_dict(payload: Optional[Dict[str, Any]]) -> "IndexMap":
        payload = payload or {}
        return IndexMap(payload.get("objects"), payload.get("materials"))

    def __bool__(self) -> bool:
        return bool(self.objects or self.materials)
