"""
ArchX3D — DXF Extractor (v3 — semantic)
=======================================
Pipeline step 1. Reads an architectural DXF and writes ``geometry.json``.

What changed in v3
------------------
v2 extracted wall centrelines and discarded everything else. It kept five
entity types and ran a layer *blacklist* that deleted the layers carrying the
most meaning — ``TEXT``, ``DOOR``, ``WINDOW``, ``FURNITURE``, ``ANNO``. The
consequence was that room classification had nothing from the drawing to work
with and depended entirely on reference imagery, so a project without usable
photographs produced a building in which every room was "unknown".

v3 delegates to :mod:`cad.reader`, which reads *everything* — blocks, room
labels, dimensions, hatches, attributes, layer conventions, units and north —
and labels each entity with a semantic role instead of discarding it. Walls
are then the entities whose role is ``wall``, rather than the entities that
survived a blacklist.

Output compatibility
--------------------
``geometry.json`` keeps its exact previous shape. ``metadata`` and ``walls``
are unchanged in structure and meaning, so room segmentation, the Blender
generator and the web viewer need no changes. The full CAD model is *added*
under a new ``cad`` key, which consumers may ignore.

Usage::

    python modules/dxf_extractor.py <input.dxf> <output.json> [layers] [scale] [arcs]

``layers`` is a comma-separated list restricting which layers become walls.
Pass ``AUTO`` (the default) to use the semantic role instead, which is
correct far more often than a hand-maintained list.
"""

from __future__ import annotations

import json
import os
import sys

# Allow both `python modules/dxf_extractor.py` and `python -m modules.dxf_extractor`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cad.reader import read  # noqa: E402


def extract_walls(
    dxf_path,
    output_path,
    layer_names=None,
    scale_factor=1.0,
    arc_segments=16,
    auto_detect=True,
    deduplicate=True,
    min_segment_length=0.05,
    normalize=True,
    filter_borders=True,
    log=print,
):
    """Extract geometry and semantics from a DXF, and save ``geometry.json``.

    The signature is retained from v2 so existing callers keep working.
    Several parameters are now handled more precisely inside ``cad.reader``
    and are accepted but no longer act as they did:

    ``deduplicate``
        Always on. Duplicate segments are keyed by their endpoints.
    ``filter_borders``
        No longer needed. v2 deleted any segment spanning >80% of the bounding
        box to remove sheet borders — which also deleted the long external
        walls of any narrow building. Borders are now identified by their
        layer role (``title_block``) and simply not classified as walls, which
        is both safer and correct.
    ``auto_detect``
        Always on, unless ``layer_names`` restricts the result.

    Args:
        dxf_path: Path to the input .dxf file.
        output_path: Path to the output .json file.
        layer_names: Restrict walls to these layers. ``None`` or ``["AUTO"]``
            uses the semantic role instead.
        scale_factor: Unit conversion to metres; 1.0 means auto-detect.
        arc_segments: Tessellation resolution for arcs and circles.
        min_segment_length: Drop wall segments shorter than this, in metres.
        normalize: Centre the plan on the origin.
        log: Callable used for progress output.

    Returns:
        The ``geometry.json`` document, as a dict.
    """
    try:
        document = read(
            dxf_path,
            user_scale=scale_factor if scale_factor and scale_factor != 1.0 else None,
            arc_segments=arc_segments,
            normalise_origin=normalize,
            log=log,
        )
    except IOError:
        log(f"[ERROR] Not a DXF file or I/O error: {dxf_path}")
        sys.exit(1)
    except Exception as exc:  # ezdxf.DXFStructureError and friends
        log(f"[ERROR] Could not read DXF: {exc}")
        sys.exit(1)

    geometry = document.to_geometry_json()

    # An explicit layer list overrides the semantic role. Kept because a user
    # who names layers has told us something we should obey — the same
    # principle the whole trust hierarchy rests on.
    requested = _requested_layers(layer_names)
    if requested:
        wanted = {name.upper() for name in requested}
        walls = [
            segment.as_wall_dict()
            for segment in document.segments
            if segment.layer.upper() in wanted
        ]
        if walls:
            log(f"[LAYER] Restricted to caller-specified layer(s): {sorted(wanted)}")
            geometry["walls"] = walls
            geometry["metadata"]["layers_used"] = sorted(wanted)
            geometry["metadata"]["segment_count"] = len(walls)
        else:
            log(f"[LAYER] No geometry on requested layer(s) {sorted(wanted)}; "
                f"falling back to semantic wall detection "
                f"({len(geometry['walls'])} segments)")

    if min_segment_length and min_segment_length > 0:
        before = len(geometry["walls"])
        geometry["walls"] = [
            wall for wall in geometry["walls"]
            if _length(wall) >= min_segment_length
        ]
        removed = before - len(geometry["walls"])
        if removed:
            log(f"[FILTER] Removed {removed} segment(s) under {min_segment_length}m")
        geometry["metadata"]["segment_count"] = len(geometry["walls"])

    _report(document, geometry, log)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(geometry, fh, indent=2)
        log(f"[OK] Geometry saved to {output_path}")
    except IOError as exc:
        log(f"[ERROR] Failed to save JSON: {exc}")
        sys.exit(1)

    return geometry


def _requested_layers(layer_names):
    """Normalise the caller's layer argument, treating AUTO as 'no restriction'.

    ``["WALLS"]`` was the old default and is *not* treated as a deliberate
    restriction, because it was almost never an informed choice — it was
    simply the value nobody changed, and honouring it would keep the v2
    behaviour of ignoring correctly-named ``A-WALL`` layers.
    """
    if not layer_names:
        return []
    names = [n.strip() for n in layer_names if n and n.strip()]
    if not names:
        return []
    upper = {n.upper() for n in names}
    if upper in ({"AUTO"}, {"WALLS"}):
        return []
    return names


def _length(wall):
    dx = wall["end"][0] - wall["start"][0]
    dy = wall["end"][1] - wall["start"][1]
    return (dx * dx + dy * dy) ** 0.5


def _report(document, geometry, log) -> None:
    """Print what the drawing turned out to contain."""
    bounds = geometry["metadata"]["bounding_box"]
    width = bounds["max"][0] - bounds["min"][0]
    depth = bounds["max"][1] - bounds["min"][1]

    log("")
    log("[INFO] === Drawing contents ==================================")
    log(f"  Footprint     : {width:.2f} m x {depth:.2f} m")
    log(f"  Wall segments : {len(geometry['walls'])}")
    log(f"  Blocks        : {len(document.blocks)}")
    log(f"  Text entities : {len(document.texts)}")
    log(f"  Room labels   : {len(document.room_labels())}")
    log(f"  Dimensions    : {len(document.dimensions)}")
    log(f"  Hatches       : {len(document.hatches)}")
    log(f"  North         : {document.north.heading_deg:.1f} deg "
        f"({document.north.reason})")

    labels = document.room_labels()
    if labels:
        log("  Rooms named in the drawing:")
        for label in labels[:12]:
            log(f"    - {label.text!r} -> {label.room_type}")
        if len(labels) > 12:
            log(f"    ... and {len(labels) - 12} more")
    else:
        log("  No room labels found - room typing will rely on blocks, "
            "layers, geometry and imagery")

    categories = document.stats.get("block_categories") or {}
    if categories:
        top = ", ".join(f"{k} x{v}" for k, v in list(categories.items())[:10])
        log(f"  Block categories: {top}")

    roles = document.stats.get("layer_roles") or {}
    if roles:
        log("  Layer roles   : "
            + ", ".join(f"{k} x{v}" for k, v in list(roles.items())[:10]))
    log("[INFO] =======================================================")
    log("")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python dxf_extractor.py <input_dxf> <output_json> "
              "[layers] [scale] [arc_segments]")
        print("  layers: comma-separated list, or AUTO to detect by layer role")
        print("  scale:  unit conversion factor (1.0 = auto-detect)")
        print("  arcs:   tessellation segments for arcs/circles (default 16)")
        sys.exit(1)

    input_dxf = sys.argv[1]
    output_json = sys.argv[2]
    layers = sys.argv[3].split(",") if len(sys.argv) > 3 else None
    scale = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    arcs = int(sys.argv[5]) if len(sys.argv) > 5 else 16

    extract_walls(input_dxf, output_json, layers, scale, arcs)
