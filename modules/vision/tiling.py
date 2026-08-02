"""
ArchX3D — Tiled image analysis
==============================
Analyses a large or dense image in overlapping tiles instead of one pass.

Why
---
A multimodal model sees an image at a fixed internal resolution. A 1500 x 1050
sheet carrying two full floor plans is downscaled until individual furniture is
a few pixels across, and the model reports what it can still resolve — which
in a real run was 11 objects for an entire house, all of them from one corner
where the items happened to be largest.

Cutting the image into tiles and analysing each restores the effective
resolution, at the cost of one model call per tile.

What this delivers, and what it does not
----------------------------------------
Tiling recovers **appearance evidence**: far more of the palette, the
materials and the object mix become visible, which is what style resolution
and the room classifier's tier-6 signals consume.

Tiling and registration are complementary, and both are needed
--------------------------------------------------------------
Tiling alone cannot attribute a detection to a room: knowing *what* is in the
sheet says nothing about where the plan sits inside it. That is
``modules/registration``'s job, and it used to be unsolved, which is why this
module originally disclaimed positional knowledge entirely.

It is solved now, and the two compose — but only because the room labels this
module recovers are remapped into whole-image coordinates along with
everything else. That matters more here than anywhere: tiling triggers on
large, dense sheets, which is exactly the composite-sheet case registration
exists for, so a label left in tile-local coordinates would corrupt the fit in
precisely the situation both features were built to handle. Tiling a sheet
therefore *improves* registration, by resolving labels too small to read in
one pass.

A tiled sheet that does not register still contributes appearance only, and
layout still comes from ``furnish``. Being explicit about that boundary
matters: many more observations are not, by themselves, positional knowledge.

Seam handling
-------------
Tiles overlap, so an object straddling a seam is seen twice. Detections are
remapped to whole-image coordinates and merged by IoU, keeping the most
confident copy — the same rule ``fusion`` uses across images.

A room label cut by a seam is the one case overlap does not fully solve: the
model may read ``MASTER`` in one tile and ``BED`` in the next. Neither
fragment matches, so the label is simply lost rather than mismatched — the
fit sees one fewer correspondence, which is a degradation and not an error.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Images smaller than this in either axis are analysed whole; tiling them
#: would cost calls without revealing anything new.
MIN_TILE_DIMENSION_PX = 1100

#: Fraction of a tile shared with its neighbour. Enough that an object the
#: size of a sofa cannot fall entirely into a seam.
TILE_OVERLAP = 0.18

#: Detections overlapping by more than this in whole-image space are treated
#: as the same object seen in two tiles.
MERGE_IOU = 0.45

#: Hard cap on tiles per image, so a very large sheet cannot fan out into an
#: unbounded number of model calls.
MAX_TILES = 9

#: How much an extra tile is worth against tile squareness when choosing a
#: grid. Small on purpose — see ``_grid_for``.
TILE_COUNT_BONUS = 0.06


@dataclass
class Tile:
    """One crop of a source image, with its place in the whole."""

    index: int
    path: str
    #: Normalised rect within the source image, ``(x0, y0, x1, y1)``.
    rect: Tuple[float, float, float, float]

    @property
    def width(self) -> float:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> float:
        return self.rect[3] - self.rect[1]

    def to_global(self, bbox: Sequence[float]) -> List[float]:
        """Map a tile-local normalised bbox into whole-image coordinates."""
        x0, y0, x1, y1 = bbox
        return [
            self.rect[0] + x0 * self.width,
            self.rect[1] + y0 * self.height,
            self.rect[0] + x1 * self.width,
            self.rect[1] + y1 * self.height,
        ]


def should_tile(path: str, analysis_mode: str = "full") -> bool:
    """Whether this image is worth splitting.

    Tiling is for images that are both large and information-dense. A
    photograph of one room gains little — the subject already fills the frame —
    whereas a plan sheet gains a great deal.
    """
    if analysis_mode in ("skip", "geometry"):
        return False

    size = image_size(path)
    if size is None:
        return False

    width, height = size
    if max(width, height) < MIN_TILE_DIMENSION_PX:
        return False

    # Plan and layout views are the dense case tiling exists for.
    return analysis_mode == "layout" or max(width, height) >= MIN_TILE_DIMENSION_PX * 1.6


def plan_tiles(path: str, output_dir: str, max_tiles: int = MAX_TILES) -> List[Tile]:
    """Cut ``path`` into overlapping tiles written under ``output_dir``.

    Returns an empty list when the image cannot be read or is not worth
    tiling, which callers treat as "analyse it whole".
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return []

    size = image_size(path)
    if size is None:
        return []

    width, height = size
    cols, rows = _grid_for(width, height, max_tiles)
    if cols * rows <= 1:
        return []

    os.makedirs(output_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]

    tiles: List[Tile] = []
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            for row in range(rows):
                for col in range(cols):
                    rect = _tile_rect(col, row, cols, rows)
                    box = (
                        int(rect[0] * width), int(rect[1] * height),
                        int(rect[2] * width), int(rect[3] * height),
                    )
                    if box[2] - box[0] < 16 or box[3] - box[1] < 16:
                        continue

                    index = len(tiles)
                    tile_path = os.path.join(output_dir, f"{stem}_tile{index}.jpg")
                    image.crop(box).save(tile_path, "JPEG", quality=92)
                    tiles.append(Tile(index=index, path=tile_path, rect=rect))
    except Exception:
        return []

    return tiles


def _grid_for(width: int, height: int, max_tiles: int) -> Tuple[int, int]:
    """Choose a tile grid matching the image's aspect ratio.

    Tiles should be near-square, because a very elongated crop wastes the
    model's fixed input aspect on empty space. So a wide sheet is cut into
    more columns than rows: a 1493 x 1050 sheet becomes 3 x 2, giving roughly
    497 x 525 tiles.

    Among grids that tile equally squarely, more tiles is better — that is the
    whole point — so tile count breaks the tie, bounded by ``max_tiles``. The
    tie-break is deliberately weak: each extra tile is another model call, and
    on a 1493 x 1050 sheet a strong bonus chose 9 badly-proportioned tiles over
    6 well-proportioned ones for no gain in what the model could resolve.
    """
    if width <= 0 or height <= 0:
        return (1, 1)

    best = (1, 1)
    best_score = float("-inf")

    for cols in range(1, 5):
        for rows in range(1, 5):
            count = cols * rows
            if count <= 1 or count > max_tiles:
                continue

            tile_aspect = (width / cols) / (height / rows)
            squareness = -abs(math.log(tile_aspect))
            score = squareness + TILE_COUNT_BONUS * count

            if score > best_score:
                best_score = score
                best = (cols, rows)

    return best


def _tile_rect(col: int, row: int, cols: int, rows: int) -> Tuple[float, float, float, float]:
    """Normalised rect for one tile, expanded by the overlap fraction."""
    base_w = 1.0 / cols
    base_h = 1.0 / rows
    pad_w = base_w * TILE_OVERLAP
    pad_h = base_h * TILE_OVERLAP

    x0 = max(0.0, col * base_w - pad_w)
    y0 = max(0.0, row * base_h - pad_h)
    x1 = min(1.0, (col + 1) * base_w + pad_w)
    y1 = min(1.0, (row + 1) * base_h + pad_h)
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def merge_payloads(
    tiled: Sequence[Tuple[Tile, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Combine per-tile model payloads into one whole-image payload.

    Every boxed reading — objects, lights, openings and labels — is remapped to
    whole-image coordinates and de-duplicated across seams. Scalar readings —
    room type, finishes, camera — are taken from the tile that expressed most
    confidence, because averaging a material name is meaningless.

    ``labels`` must be remapped along with the rest, and the reason is worth
    stating: a label's *only* value is positional. One left in tile-local
    coordinates does not degrade the registration, it corrupts it — the fit
    would be handed a room name at a confidently wrong position. Dropping them
    silently is barely better, because tiling triggers on exactly the large,
    dense sheets that registration exists to handle, so the two features would
    fail to compose in precisely the case both were built for.
    """
    if not tiled:
        return {}

    merged: Dict[str, Any] = {}
    objects: List[Dict[str, Any]] = []
    lights: List[Dict[str, Any]] = []
    openings: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []

    best_scalar: Optional[Dict[str, Any]] = None
    best_confidence = -1.0

    for tile, payload in tiled:
        if not isinstance(payload, dict):
            continue

        for key, sink in (("objects", objects), ("lights", lights),
                          ("openings", openings), ("labels", labels)):
            for entry in payload.get(key) or []:
                if not isinstance(entry, dict):
                    continue
                remapped = dict(entry)
                bbox = entry.get("bbox")
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    try:
                        remapped["bbox"] = tile.to_global([float(v) for v in bbox])
                    except (TypeError, ValueError):
                        continue
                else:
                    # Without a box a detection cannot be placed or merged.
                    continue
                remapped["_tile"] = tile.index
                sink.append(remapped)

        room = payload.get("room") or {}
        confidence = _f(room.get("confidence"))
        if confidence > best_confidence:
            best_confidence = confidence
            best_scalar = payload

    if best_scalar:
        for key in ("image_class", "room", "camera", "finishes", "architecture"):
            if key in best_scalar:
                merged[key] = best_scalar[key]

    merged["objects"] = _dedupe(objects)
    merged["lights"] = _dedupe(lights)
    merged["openings"] = _dedupe(openings)
    merged["labels"] = _dedupe(labels)

    # Relationships reference ids that are only meaningful inside one tile, so
    # they cannot survive the merge. `relations.infer_relationships` recovers
    # the implied ones from the merged object set instead.
    merged["relationships"] = []
    return merged


def _dedupe(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop duplicates of the same physical item seen in adjoining tiles."""
    ordered = sorted(entries, key=lambda e: -_f(e.get("confidence")))
    kept: List[Dict[str, Any]] = []

    for entry in ordered:
        bbox = entry.get("bbox")
        category = str(entry.get("category") or entry.get("kind") or "")
        duplicate = False

        for existing in kept:
            existing_category = str(
                existing.get("category") or existing.get("kind") or ""
            )
            if category != existing_category:
                continue
            if _iou(bbox, existing.get("bbox")) >= MERGE_IOU:
                duplicate = True
                break

        if not duplicate:
            kept.append(entry)

    for entry in kept:
        entry.pop("_tile", None)
    return kept


def _iou(a: Any, b: Any) -> float:
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
        return 0.0
    if len(a) != 4 or len(b) != 4:
        return 0.0

    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)

    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)

    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 1e-9 else 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def image_size(path: str) -> Optional[Tuple[int, int]]:
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as image:
            return image.width, image.height
    except Exception:
        return None


def _f(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default
