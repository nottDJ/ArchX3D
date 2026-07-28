"""
ArchX3D — Image loading and comparison primitives
=================================================
The numeric floor the axes stand on: loading a reference photograph and a
render into comparable arrays, decoding the auxiliary passes, and the colour
maths that turns pixels into quantities a person can argue with.

Optional dependencies, honestly handled
---------------------------------------
Pillow and numpy are optional throughout this project. Without them the pixel
axes report ``measured=False`` and say why — they never fabricate a score.
:func:`available` is the single place that decides, and every axis asks it
before doing anything.

Making two images comparable
----------------------------
The render and the photograph are of the same view — that is the whole point
of storing the fitted ``ViewPoint`` — but they are not the same size, and the
photograph may have a slightly different aspect ratio from the frame the
preview pipeline chose. Both are resampled to a common small working size
(:data:`WORK_WIDTH`), which does three things: it makes the arrays alignable,
it costs almost nothing, and it low-passes away the sensor noise and JPEG
artefacts that would otherwise dominate a high-frequency comparison.

What it deliberately does *not* do is warp, align or register the images. If
the reconstruction put the sofa somewhere else, that is the finding, not an
error to be corrected away before measuring.

Colour space
------------
Comparisons happen in CIELAB, reached through sRGB -> linear -> XYZ (D65).
Euclidean distance in Lab is roughly perceptual, which means a ``dE`` figure
in a finding corresponds to how different the two colours *look* — the thing
being reported on. Distances in raw sRGB do not have that property and would
make "how far off is this wall" unanswerable.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Working width every image is resampled to. 256 keeps a 16:9 frame at
#: 256x144 — enough to carry layout, colour and coarse texture, small enough
#: that a whole building's evaluation is milliseconds.
WORK_WIDTH = 256

#: Below this share of the frame a masked region is too small to say anything
#: reliable about, and a finding drawn from it would be noise with a name.
MIN_REGION_FRACTION = 0.02


# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------


_BACKEND: Optional[Tuple[Any, Any]] = None
_BACKEND_ERROR = ""


def backend() -> Optional[Tuple[Any, Any]]:
    """``(numpy, PIL.Image)`` or ``None``. Imported once, cached."""
    global _BACKEND, _BACKEND_ERROR
    if _BACKEND is not None:
        return _BACKEND
    if _BACKEND_ERROR:
        return None
    try:
        import numpy
        from PIL import Image
    except ImportError as exc:
        _BACKEND_ERROR = f"pixel analysis needs numpy and Pillow ({exc})"
        return None
    _BACKEND = (numpy, Image)
    return _BACKEND


def available() -> bool:
    return backend() is not None


def unavailable_reason() -> str:
    backend()
    return _BACKEND_ERROR or ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_rgb(path: str, size: Optional[Tuple[int, int]] = None):
    """An image as float RGB in ``[0, 1]``, resampled to the working size.

    Returns ``None`` for anything unreadable — a missing render and a corrupt
    JPEG are both "cannot measure this", which the caller reports rather than
    treats as black pixels.
    """
    parts = backend()
    if parts is None or not path or not os.path.exists(path):
        return None
    numpy, Image = parts
    try:
        with Image.open(path) as handle:
            image = handle.convert("RGB")
            target = size or work_size(image.width, image.height)
            # Bilinear, not nearest: this is a resample of continuous-tone
            # imagery, and nearest would alias the fine texture the material
            # axis measures into noise.
            image = image.resize(target, Image.BILINEAR)
            return numpy.asarray(image).astype(numpy.float32) / 255.0
    except Exception:  # noqa: BLE001 - unreadable is a measurement outcome
        return None


def load_raw(path: str, size: Optional[Tuple[int, int]] = None):
    """An image as 8-bit integers, *without* interpolation.

    For the ID passes: their pixels are indices, and any resampling that
    averages neighbours invents indices that were never rendered. Nearest
    neighbour is the only correct filter here, and it is why this is a
    separate function rather than a flag.
    """
    parts = backend()
    if parts is None or not path or not os.path.exists(path):
        return None
    numpy, Image = parts
    try:
        with Image.open(path) as handle:
            image = handle.convert("RGB")
            target = size or work_size(image.width, image.height)
            image = image.resize(target, Image.NEAREST)
            return numpy.asarray(image).astype(numpy.int32)
    except Exception:  # noqa: BLE001
        return None


def work_size(width: int, height: int) -> Tuple[int, int]:
    """The common size an image of this shape is compared at."""
    if width <= 0 or height <= 0:
        return (WORK_WIDTH, WORK_WIDTH)
    scale = WORK_WIDTH / float(width)
    return (WORK_WIDTH, max(2, int(round(height * scale))))


@dataclass
class ImagePair:
    """A reference photograph and the render taken from its camera.

    Both are resampled to the *render's* shape. The render's frame is the one
    the preview pipeline chose to match the viewpoint's stored aspect ratio,
    so it is the frame the comparison is defined in; stretching the render to
    the photograph instead would distort the thing being judged.
    """

    reference: Any = None
    render: Any = None
    reference_path: str = ""
    render_path: str = ""
    #: Decoded auxiliary passes, by name.
    passes: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reference is not None and self.render is not None

    @property
    def shape(self) -> Tuple[int, int]:
        if self.render is None:
            return (0, 0)
        return (int(self.render.shape[1]), int(self.render.shape[0]))

    def has(self, *names: str) -> bool:
        return all(self.passes.get(name) is not None for name in names)


def load_pair(reference_path: str, render_path: str) -> ImagePair:
    """Load a reference and a render into one comparable pair."""
    pair = ImagePair(reference_path=reference_path, render_path=render_path)
    render = load_rgb(render_path)
    if render is None:
        pair.notes.append(f"render not readable: {render_path or '(none)'}")
        return pair
    pair.render = render
    size = (int(render.shape[1]), int(render.shape[0]))

    reference = load_rgb(reference_path, size=size)
    if reference is None:
        pair.notes.append(
            f"reference image not readable: {reference_path or '(none)'}"
        )
        return pair
    pair.reference = reference
    return pair


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def srgb_to_linear(image):
    """Undo the sRGB transfer function. Required before any averaging.

    Averaging sRGB values is a common and wrong shortcut: the encoding is
    non-linear, so the mean of two sRGB values is not the value of their mean
    light. Every statistic here is taken in linear light and converted back
    only for display.
    """
    numpy, _ = backend()
    return numpy.where(
        image <= 0.04045, image / 12.92, ((image + 0.055) / 1.055) ** 2.4
    )


def linear_to_srgb(image):
    numpy, _ = backend()
    return numpy.where(
        image <= 0.0031308, image * 12.92, 1.055 * numpy.clip(image, 0, None) ** (1 / 2.4) - 0.055
    )


#: sRGB primaries, D65 white. The standard matrix; spelled out so the colour
#: pipeline is auditable rather than borrowed from a library that may not be
#: installed.
_XYZ_FROM_LINEAR = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
_WHITE = (0.95047, 1.00000, 1.08883)


def to_lab(image):
    """sRGB in ``[0,1]`` -> CIELAB. ``L`` in 0–100, ``a``/``b`` roughly ±128."""
    numpy, _ = backend()
    linear = srgb_to_linear(image)
    flat = linear.reshape(-1, 3)
    matrix = numpy.array(_XYZ_FROM_LINEAR, dtype=numpy.float32).T
    xyz = flat @ matrix
    xyz = xyz / numpy.array(_WHITE, dtype=numpy.float32)

    epsilon = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    f = numpy.where(xyz > epsilon, numpy.cbrt(numpy.clip(xyz, 0, None)),
                    (kappa * xyz + 16.0) / 116.0)
    lab = numpy.stack(
        [116.0 * f[:, 1] - 16.0,
         500.0 * (f[:, 0] - f[:, 1]),
         200.0 * (f[:, 1] - f[:, 2])],
        axis=-1,
    )
    return lab.reshape(image.shape)


def delta_e(lab_a, lab_b) -> float:
    """Mean CIE76 distance between two Lab images or two Lab colours.

    CIE76 rather than CIEDE2000: it is a plain Euclidean distance, so a reader
    can verify it, and the extra accuracy of the 2000 formula is irrelevant
    when the input is a render compared with a photograph taken on unknown
    equipment under unknown white balance. Claiming that precision would be
    false precision.
    """
    numpy, _ = backend()
    difference = numpy.asarray(lab_a, dtype=numpy.float64) - numpy.asarray(
        lab_b, dtype=numpy.float64
    )
    return float(numpy.sqrt((difference ** 2).sum(axis=-1)).mean())


def mean_colour(image, mask=None) -> Tuple[float, float, float]:
    """Mean sRGB of an image or a masked region, averaged in linear light."""
    numpy, _ = backend()
    linear = srgb_to_linear(image)
    if mask is not None:
        if not mask.any():
            return (0.0, 0.0, 0.0)
        linear = linear[mask]
    mean = linear.reshape(-1, 3).mean(axis=0)
    srgb = linear_to_srgb(mean)
    return tuple(float(numpy.clip(c, 0.0, 1.0)) for c in srgb)


def to_hex(colour: Tuple[float, float, float]) -> str:
    return "#" + "".join(f"{int(round(max(0.0, min(1.0, c)) * 255)):02X}" for c in colour)


def luma(image):
    """Rec. 709 luminance from linear light, in ``[0, 1]``."""
    numpy, _ = backend()
    linear = srgb_to_linear(image)
    return (0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2])


def saturation(image, mask=None) -> float:
    """Mean HSV-style saturation: ``(max - min) / max`` per pixel.

    Chosen over Lab chroma because it is scale-free — halving the light
    halves L but leaves saturation alone — which is what makes it comparable
    between an unlit albedo pass and a lit photograph.
    """
    numpy, _ = backend()
    pixels = image[mask] if mask is not None else image.reshape(-1, 3)
    if pixels.size == 0:
        return 0.0
    high = pixels.max(axis=-1)
    low = pixels.min(axis=-1)
    return float(numpy.where(high > 1e-6, (high - low) / numpy.maximum(high, 1e-6), 0.0).mean())


def warmth(image, mask=None) -> float:
    """Red minus blue in linear light. Positive is warm.

    A blunt instrument on purpose: colour temperature cannot be recovered from
    a photograph without knowing the camera's white balance, so the honest
    measure is the relative one, and it is only ever compared against the same
    measure taken from the other image.
    """
    numpy, _ = backend()
    linear = srgb_to_linear(image)
    pixels = linear[mask] if mask is not None else linear.reshape(-1, 3)
    if pixels.size == 0:
        return 0.0
    return float((pixels[..., 0] - pixels[..., 2]).mean())


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


#: Width of the smoothing kernel applied to every histogram. Measured, not
#: guessed: a reference JPEG and a render PNG of *identical* content intersect
#: at only 0.71 unsmoothed, because JPEG's quantisation nudges values across
#: bin boundaries. At width 3 the same pair reaches 0.90 while a genuinely
#: warm-shifted render still scores 0.24, so the discrimination that matters
#: survives. Width 5 flattens the difference between a mild and a real shift
#: and was rejected for that reason.
HISTOGRAM_SMOOTHING = 3


def histogram(image, bins: int = 24):
    """Per-channel normalised histogram, smoothed, as one concatenated vector.

    Smoothing is not cosmetic. Without it the measure answers "were these
    encoded the same way" as loudly as "are these the same colours", and the
    first question is not one this engine is asking.
    """
    numpy, _ = backend()
    kernel = numpy.ones(HISTOGRAM_SMOOTHING) / HISTOGRAM_SMOOTHING
    channels = []
    for index in range(3):
        counts, _edges = numpy.histogram(image[..., index], bins=bins, range=(0.0, 1.0))
        total = counts.sum()
        normalised = counts / total if total else counts.astype(numpy.float64)
        if HISTOGRAM_SMOOTHING > 1:
            normalised = numpy.convolve(normalised, kernel, mode="same")
        channels.append(normalised)
    return numpy.concatenate(channels)


def intersection(hist_a, hist_b) -> float:
    """Histogram intersection in ``[0, 1]``; 1 means identical distributions."""
    numpy, _ = backend()
    return float(numpy.minimum(hist_a, hist_b).sum() / 3.0)


def mass_grid(image, rows: int = 6, columns: int = 8):
    """Where the visual mass sits, as a coarse normalised grid.

    Uses local contrast rather than brightness: a bright empty wall is not
    "mass", and a dark sofa against a light wall very much is. Contrast is
    also far less sensitive to the exposure difference between a render and a
    photograph, which the lighting axis is responsible for and this one should
    not be double-counting.
    """
    numpy, _ = backend()
    values = luma(image)
    height, width = values.shape
    cell_h = max(1, height // rows)
    cell_w = max(1, width // columns)

    grid = numpy.zeros((rows, columns), dtype=numpy.float64)
    for row in range(rows):
        for column in range(columns):
            block = values[row * cell_h:(row + 1) * cell_h,
                           column * cell_w:(column + 1) * cell_w]
            if block.size:
                grid[row, column] = float(block.std())
    total = grid.sum()
    return grid / total if total > 1e-9 else grid


def detail_energy(image, scale: int) -> float:
    """How much detail survives at a given downsample factor.

    A box-filtered copy is subtracted from the original, leaving the content
    finer than ``scale``. Wood grain, tile joints and weave live here; flat
    paint does not. Comparing this between a render and a photograph asks
    "does this surface have about the right amount of visible texture", which
    is answerable, unlike "is this the same wood", which is not.
    """
    numpy, _ = backend()
    values = luma(image)
    height, width = values.shape
    if height < scale * 2 or width < scale * 2:
        return 0.0
    trimmed = values[:height - height % scale, :width - width % scale]
    blocks = trimmed.reshape(trimmed.shape[0] // scale, scale,
                             trimmed.shape[1] // scale, scale)
    coarse = blocks.mean(axis=(1, 3))
    upsampled = numpy.repeat(numpy.repeat(coarse, scale, axis=0), scale, axis=1)
    return float(numpy.abs(trimmed - upsampled).mean())


def gradient_direction(image) -> Tuple[float, float]:
    """Where the image gets brighter, as a unit vector in image space.

    The dominant brightness gradient is a crude proxy for where the light is
    coming from. It cannot separate a window from a lamp, so the lighting axis
    uses it as corroboration for a direction finding rather than as the
    finding itself.
    """
    numpy, _ = backend()
    values = luma(image)
    if values.shape[0] < 4 or values.shape[1] < 4:
        return (0.0, 0.0)
    dy, dx = numpy.gradient(values)
    vector = numpy.array([dx.mean(), dy.mean()], dtype=numpy.float64)
    magnitude = float(numpy.linalg.norm(vector))
    if magnitude < 1e-9:
        return (0.0, 0.0)
    return (float(vector[0] / magnitude), float(vector[1] / magnitude))


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------


def index_plane(raw):
    """An ID pass's integer index per pixel."""
    from render import passes as passes_mod

    return passes_mod.decode_index(raw[..., 0], raw[..., 1])


def region_masks(indices, minimum_fraction: float = MIN_REGION_FRACTION):
    """Boolean masks per index, dropping background and slivers.

    Small regions are excluded because a comparison over 40 pixels is a
    coin-toss dressed as a measurement, and a finding built on one would be
    noise with a material's name attached to it.
    """
    numpy, _ = backend()
    total = float(indices.size)
    masks = {}
    for value in numpy.unique(indices):
        index = int(value)
        if index <= 0:
            continue  # 0 is background by construction
        mask = indices == index
        if mask.sum() / total >= minimum_fraction:
            masks[index] = mask
    return masks


def fraction(mask) -> float:
    return float(mask.sum()) / float(mask.size) if mask.size else 0.0


def depth_metres(raw, depth_range: float):
    """A depth pass's metres per pixel, with background as NaN.

    NaN rather than zero: a pixel with no surface has no depth, and letting it
    read as "zero metres away" would drag every statistic towards the camera.
    """
    numpy, _ = backend()
    from render import passes as passes_mod

    values = passes_mod.decode_depth(raw[..., 0].astype(numpy.float64), depth_range)
    return numpy.where(raw[..., 0] > 0, values, numpy.nan)


def normal_vectors(raw):
    """A normal pass's unit vectors per pixel, world space."""
    numpy, _ = backend()
    return raw.astype(numpy.float64) / 255.0 * 2.0 - 1.0


def clamp01(value: float) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))
