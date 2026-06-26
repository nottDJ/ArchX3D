"""
ArchX3D — DXF Geometry Extractor (v2 — Smart Filtering)
=========================================================
Extracts wall geometry from architectural DXF files and exports
to a clean geometry.json for downstream Blender consumption.

v2 improvements:
  - Layer blacklist/whitelist with architectural conventions
  - Origin normalization (centers geometry at 0,0)
  - Unit auto-detection from DXF $INSUNITS header
  - Segment length filtering (removes tiny door/window fragments)
  - Bounding-box outlier detection (removes border lines)
  - Diagnostic layer summary logging

Supported entities: LINE, LWPOLYLINE, POLYLINE, ARC, CIRCLE.
"""

import sys
import json
import math
import os
import ezdxf


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTITY_TYPES = {'LINE', 'LWPOLYLINE', 'POLYLINE', 'ARC', 'CIRCLE'}

# Layers to ALWAYS ignore (case-insensitive substring match)
LAYER_BLACKLIST = [
    'DEFPOINTS', 'TITLE', 'BORDER', 'VIEWPORT',
    'HATCH', 'TEXT', 'DIM', 'ANNO', 'FURNITURE',
    'STAIR', 'ELEVATION', 'SYMBOL', 'NOTE', 'GRID',
    'DOOR', 'WINDOW', 'GLAZ', 'LEGEND', 'PAPER',
]

# Layer name fragments that indicate wall geometry (priority order)
WALL_LAYER_HINTS = [
    'A-WALL', 'WALL', 'A_WALL', 'S-WALL',
    'STR', 'COLS', 'COLUMN', 'STRUCT',
]

# DXF $INSUNITS -> meters conversion factors
INSUNITS_TO_METERS = {
    0: 1.0,       # Unitless
    1: 0.0254,    # Inches
    2: 0.3048,    # Feet
    3: 1.609344e3,# Miles (unlikely but defined)
    4: 0.001,     # Millimeters
    5: 0.01,      # Centimeters
    6: 1.0,       # Meters
    7: 1000.0,    # Kilometers
    8: 2.54e-8,   # Microinches
    9: 0.001,     # Mils (thousandths of inch)
    10: 0.9144,   # Yards
    11: 1.0e-10,  # Angstroms
    12: 1.0e-9,   # Nanometers
    13: 1.0e-6,   # Microns
    14: 0.01,     # Decimeters
}


# ---------------------------------------------------------------------------
# Entity Decomposition Helpers
# ---------------------------------------------------------------------------

def decompose_line(entity, scale):
    """Extract a single segment from a LINE entity."""
    s = entity.dxf.start
    e = entity.dxf.end
    return [{"start": [round(s.x * scale, 6), round(s.y * scale, 6)],
             "end":   [round(e.x * scale, 6), round(e.y * scale, 6)],
             "source_entity": "LINE", "layer": entity.dxf.layer}]


def decompose_lwpolyline(entity, scale):
    """Decompose an LWPOLYLINE into individual line segments."""
    points = list(entity.get_points(format='xy'))
    if len(points) < 2:
        return []
    segments = []
    for i in range(len(points) - 1):
        p1, p2 = points[i], points[i + 1]
        segments.append({"start": [round(p1[0]*scale,6), round(p1[1]*scale,6)],
                         "end":   [round(p2[0]*scale,6), round(p2[1]*scale,6)],
                         "source_entity": "LWPOLYLINE", "layer": entity.dxf.layer})
    if entity.closed and len(points) >= 3:
        segments.append({"start": [round(points[-1][0]*scale,6), round(points[-1][1]*scale,6)],
                         "end":   [round(points[0][0]*scale,6),  round(points[0][1]*scale,6)],
                         "source_entity": "LWPOLYLINE", "layer": entity.dxf.layer})
    return segments


def decompose_polyline(entity, scale):
    """Decompose a legacy POLYLINE into line segments."""
    try:
        points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
    except Exception:
        return []
    if len(points) < 2:
        return []
    segments = []
    for i in range(len(points) - 1):
        p1, p2 = points[i], points[i + 1]
        segments.append({"start": [round(p1[0]*scale,6), round(p1[1]*scale,6)],
                         "end":   [round(p2[0]*scale,6), round(p2[1]*scale,6)],
                         "source_entity": "POLYLINE", "layer": entity.dxf.layer})
    if entity.is_closed and len(points) >= 3:
        segments.append({"start": [round(points[-1][0]*scale,6), round(points[-1][1]*scale,6)],
                         "end":   [round(points[0][0]*scale,6),  round(points[0][1]*scale,6)],
                         "source_entity": "POLYLINE", "layer": entity.dxf.layer})
    return segments


def decompose_arc(entity, scale, num_segments=16):
    """Tessellate an ARC entity into straight line segments."""
    cx, cy = entity.dxf.center.x * scale, entity.dxf.center.y * scale
    r = entity.dxf.radius * scale
    start_deg, end_deg = entity.dxf.start_angle, entity.dxf.end_angle
    if end_deg <= start_deg:
        end_deg += 360.0
    s_rad, e_rad = math.radians(start_deg), math.radians(end_deg)
    delta = (e_rad - s_rad) / num_segments
    segments = []
    for i in range(num_segments):
        a1, a2 = s_rad + delta * i, s_rad + delta * (i + 1)
        segments.append({"start": [round(cx+r*math.cos(a1),6), round(cy+r*math.sin(a1),6)],
                         "end":   [round(cx+r*math.cos(a2),6), round(cy+r*math.sin(a2),6)],
                         "source_entity": "ARC", "layer": entity.dxf.layer})
    return segments


def decompose_circle(entity, scale, num_segments=16):
    """Tessellate a CIRCLE entity into straight line segments."""
    cx, cy = entity.dxf.center.x * scale, entity.dxf.center.y * scale
    r = entity.dxf.radius * scale
    delta = (2 * math.pi) / num_segments
    segments = []
    for i in range(num_segments):
        a1, a2 = delta * i, delta * (i + 1)
        segments.append({"start": [round(cx+r*math.cos(a1),6), round(cy+r*math.sin(a1),6)],
                         "end":   [round(cx+r*math.cos(a2),6), round(cy+r*math.sin(a2),6)],
                         "source_entity": "CIRCLE", "layer": entity.dxf.layer})
    return segments


DECOMPOSERS = {
    'LINE': decompose_line,
    'LWPOLYLINE': decompose_lwpolyline,
    'POLYLINE': decompose_polyline,
    'ARC': decompose_arc,
    'CIRCLE': decompose_circle,
}


# ---------------------------------------------------------------------------
# Layer Intelligence
# ---------------------------------------------------------------------------

def _is_blacklisted(layer_name):
    """Check if a layer name matches any blacklist keyword."""
    upper = layer_name.upper()
    return any(kw in upper for kw in LAYER_BLACKLIST)


def print_layer_summary(msp):
    """Print a diagnostic table of all layers and their entity counts."""
    layer_counts = {}
    for entity in msp:
        if entity.dxftype() in ENTITY_TYPES:
            layer = entity.dxf.layer
            layer_counts[layer] = layer_counts.get(layer, 0) + 1

    if not layer_counts:
        print("[DIAG] No geometry entities found in any layer!")
        return layer_counts

    print("\n[DIAG] === Layer Summary ===================================")
    print(f"  {'Layer':<40s} {'Entities':>8s}  {'Status'}")
    print(f"  {'-'*40} {'-'*8}  {'-'*20}")
    for layer, count in sorted(layer_counts.items(), key=lambda x: -x[1]):
        if _is_blacklisted(layer):
            status = "[SKIP] blacklisted"
        elif any(kw in layer.upper() for kw in WALL_LAYER_HINTS):
            status = "[WALL] candidate"
        else:
            status = "[    ] available"
        print(f"  {layer:<40s} {count:>8d}  {status}")
    print(f"  {'-'*40} {'-'*8}  {'-'*20}")
    print(f"  {'TOTAL':<40s} {sum(layer_counts.values()):>8d}")
    print()
    return layer_counts


def find_best_layers(msp, preferred_layers):
    """Determine which layers to extract from with smart filtering.

    Strategy:
    1. Print diagnostic summary of all layers.
    2. Try each preferred layer name (case-insensitive).
    3. If none found, look for layers matching WALL_LAYER_HINTS.
    4. If still none, pick the non-blacklisted layer with most entities.
    5. Fallback to layer "0".
    """
    layer_counts = print_layer_summary(msp)
    if not layer_counts:
        return ["0"]

    # Filter out blacklisted layers
    eligible = {k: v for k, v in layer_counts.items() if not _is_blacklisted(k)}

    # 1. Try preferred layers (case-insensitive exact match)
    matched = []
    for pref in preferred_layers:
        for actual_layer in eligible:
            if actual_layer.upper() == pref.upper():
                matched.append(actual_layer)
    if matched:
        print(f"[LAYER] Matched preferred layer(s): {matched}")
        return matched

    # 2. Try wall-hint layers (substring match, sorted by entity count)
    hint_matches = []
    for actual_layer, count in sorted(eligible.items(), key=lambda x: -x[1]):
        upper = actual_layer.upper()
        if any(kw in upper for kw in WALL_LAYER_HINTS):
            hint_matches.append(actual_layer)
    if hint_matches:
        print(f"[LAYER] Auto-detected wall layer(s): {hint_matches}")
        return hint_matches

    # 3. Fallback: non-blacklisted layer with most entities
    if eligible:
        best = max(eligible, key=eligible.get)
        print(f"[LAYER] No wall layers found. Using busiest eligible layer: "
              f"'{best}' ({eligible[best]} entities)")
        return [best]

    print("[WARN] All layers are blacklisted! Using layer '0'.")
    return ["0"]


# ---------------------------------------------------------------------------
# Unit Detection
# ---------------------------------------------------------------------------

def detect_unit_scale(doc, user_scale):
    """Auto-detect drawing units from the DXF $INSUNITS header.

    If user provided an explicit scale != 1.0, respect it.
    Otherwise, try to read $INSUNITS and apply conversion to meters.
    """
    if user_scale != 1.0:
        print(f"[UNITS] User-specified scale factor: {user_scale}")
        return user_scale

    try:
        insunits = doc.header.get('$INSUNITS', 0)
    except Exception:
        insunits = 0

    if insunits in INSUNITS_TO_METERS and insunits != 0:
        factor = INSUNITS_TO_METERS[insunits]
        unit_names = {1:'inches',2:'feet',4:'mm',5:'cm',6:'meters',
                      7:'km',10:'yards',14:'decimeters'}
        name = unit_names.get(insunits, f'unit#{insunits}')
        print(f"[UNITS] DXF header $INSUNITS={insunits} ({name}) -> "
              f"scale factor {factor}")
        return factor

    print("[UNITS] No $INSUNITS header found. Using heuristic detection...")
    return None  # Will be resolved after extraction


def heuristic_unit_scale(bbox_width, bbox_height):
    """Guess the unit system from the bounding box dimensions.

    A typical building is 5–100m across. We check if the raw bbox
    dimensions make sense in different unit systems.
    """
    max_dim = max(bbox_width, bbox_height)
    if max_dim <= 0:
        return 1.0

    # If raw dimensions are 5–200, probably already meters
    if 5 <= max_dim <= 200:
        print(f"[UNITS] Bbox {max_dim:.1f} units -- likely meters (no conversion)")
        return 1.0
    # If 500–200000, probably millimeters
    elif 500 <= max_dim <= 200000:
        print(f"[UNITS] Bbox {max_dim:.1f} units -- likely millimeters (/1000)")
        return 0.001
    # If 15–700, probably feet
    elif 200 < max_dim < 500:
        print(f"[UNITS] Bbox {max_dim:.1f} units -- likely feet (x0.3048)")
        return 0.3048
    # If 200–8000, probably inches
    elif max_dim > 200:
        print(f"[UNITS] Bbox {max_dim:.1f} units -- likely inches (x0.0254)")
        return 0.0254
    else:
        print(f"[UNITS] Cannot determine units (bbox={max_dim:.1f}). No conversion.")
        return 1.0


# ---------------------------------------------------------------------------
# Filtering & Post-Processing
# ---------------------------------------------------------------------------

def seg_length(seg):
    """Compute the length of a segment."""
    dx = seg["end"][0] - seg["start"][0]
    dy = seg["end"][1] - seg["start"][1]
    return math.sqrt(dx * dx + dy * dy)


def deduplicate_segments(segments):
    """Remove exact duplicate segments (same start/end or reversed)."""
    seen = set()
    unique = []
    for seg in segments:
        s, e = tuple(seg["start"]), tuple(seg["end"])
        key = (min(s, e), max(s, e))
        if key not in seen:
            seen.add(key)
            unique.append(seg)
    removed = len(segments) - len(unique)
    if removed > 0:
        print(f"[DEDUP] Removed {removed} duplicate segment(s)")
    return unique


def filter_tiny_segments(segments, min_length):
    """Remove segments shorter than min_length."""
    if min_length <= 0:
        return segments
    filtered = [s for s in segments if seg_length(s) >= min_length]
    removed = len(segments) - len(filtered)
    if removed > 0:
        print(f"[FILTER] Removed {removed} tiny segment(s) (< {min_length:.4f}m)")
    return filtered


def filter_border_outliers(segments, threshold=0.80):
    """Remove segments that span > threshold of the total bounding box.

    These are typically page borders or site boundary lines.
    """
    if not segments:
        return segments

    all_x = [v for s in segments for v in (s["start"][0], s["end"][0])]
    all_y = [v for s in segments for v in (s["start"][1], s["end"][1])]
    bbox_w = max(all_x) - min(all_x)
    bbox_h = max(all_y) - min(all_y)

    if bbox_w <= 0 and bbox_h <= 0:
        return segments

    filtered = []
    removed = 0
    for seg in segments:
        dx = abs(seg["end"][0] - seg["start"][0])
        dy = abs(seg["end"][1] - seg["start"][1])
        if (bbox_w > 0 and dx > bbox_w * threshold) or \
           (bbox_h > 0 and dy > bbox_h * threshold):
            removed += 1
        else:
            filtered.append(seg)

    if removed > 0:
        print(f"[FILTER] Removed {removed} border-like segment(s) "
              f"(> {threshold*100:.0f}% of bbox)")
    return filtered


def normalize_origin(segments):
    """Translate all segments so the centroid of the bounding box is at (0,0)."""
    if not segments:
        return segments

    all_x = [v for s in segments for v in (s["start"][0], s["end"][0])]
    all_y = [v for s in segments for v in (s["start"][1], s["end"][1])]

    cx = (min(all_x) + max(all_x)) / 2.0
    cy = (min(all_y) + max(all_y)) / 2.0

    if abs(cx) < 1.0 and abs(cy) < 1.0:
        return segments  # Already near origin

    print(f"[ORIGIN] Centering geometry: offset ({cx:.2f}, {cy:.2f}) -> (0, 0)")
    for seg in segments:
        seg["start"] = [round(seg["start"][0] - cx, 6),
                        round(seg["start"][1] - cy, 6)]
        seg["end"]   = [round(seg["end"][0] - cx, 6),
                        round(seg["end"][1] - cy, 6)]
    return segments


def compute_bounding_box(segments):
    """Compute the axis-aligned bounding box of all segments."""
    if not segments:
        return {"min": [0, 0], "max": [0, 0]}
    all_x = [v for s in segments for v in (s["start"][0], s["end"][0])]
    all_y = [v for s in segments for v in (s["start"][1], s["end"][1])]
    return {"min": [float(min(all_x)), float(min(all_y))], "max": [float(max(all_x)), float(max(all_y))]}


# ---------------------------------------------------------------------------
# Main Extraction
# ---------------------------------------------------------------------------

def extract_walls(dxf_path, output_path, layer_names=None, scale_factor=1.0,
                  arc_segments=16, auto_detect=True, deduplicate=True,
                  min_segment_length=0.05, normalize=True, filter_borders=True):
    """
    Extracts wall geometry from a DXF file and saves to JSON.

    Args:
        dxf_path: Path to the input .dxf file.
        output_path: Path to the output .json file.
        layer_names: Layer names to extract from. Defaults to ["WALLS"].
        scale_factor: Multiplier to convert units to meters (1.0 = auto-detect).
        arc_segments: Number of segments for arc/circle tessellation.
        auto_detect: If True, auto-detect layers when preferred are empty.
        deduplicate: If True, remove duplicate segments.
        min_segment_length: Minimum segment length in meters (after scaling).
        normalize: If True, center geometry at origin.
        filter_borders: If True, remove border-spanning segments.
    """
    if layer_names is None:
        layer_names = ["WALLS"]

    # --- Read the DXF ---
    try:
        doc = ezdxf.readfile(dxf_path)
    except IOError:
        print(f"[ERROR] Not a DXF file or I/O error: {dxf_path}")
        sys.exit(1)
    except ezdxf.DXFStructureError:
        print(f"[ERROR] Invalid or corrupted DXF file: {dxf_path}")
        sys.exit(1)

    msp = doc.modelspace()

    # --- Determine unit scale ---
    detected_scale = detect_unit_scale(doc, scale_factor)

    # --- Determine which layers to use ---
    if auto_detect:
        active_layers = find_best_layers(msp, layer_names)
    else:
        active_layers = [l for l in layer_names]

    print(f"[INFO] Extracting from layer(s): {active_layers}")

    # --- First pass: extract raw (scale=1.0 if we need heuristic) ---
    raw_scale = detected_scale if detected_scale is not None else 1.0

    all_segments = []
    entity_counts = {t: 0 for t in ENTITY_TYPES}

    for entity in msp:
        layer = entity.dxf.layer
        if layer.upper() not in [l.upper() for l in active_layers]:
            continue
        etype = entity.dxftype()
        if etype not in DECOMPOSERS:
            continue

        if etype in ('ARC', 'CIRCLE'):
            segs = DECOMPOSERS[etype](entity, raw_scale, arc_segments)
        else:
            segs = DECOMPOSERS[etype](entity, raw_scale)
        entity_counts[etype] += 1

        for seg in segs:
            if seg["start"] != seg["end"]:
                all_segments.append(seg)

    # --- Heuristic unit detection if needed ---
    if detected_scale is None and all_segments:
        bbox = compute_bounding_box(all_segments)
        bbox_w = bbox["max"][0] - bbox["min"][0]
        bbox_h = bbox["max"][1] - bbox["min"][1]
        h_scale = heuristic_unit_scale(bbox_w, bbox_h)

        if h_scale != 1.0:
            print(f"[UNITS] Re-scaling all {len(all_segments)} segments "
                  f"by {h_scale}")
            for seg in all_segments:
                seg["start"] = [round(seg["start"][0]*h_scale, 6),
                                round(seg["start"][1]*h_scale, 6)]
                seg["end"]   = [round(seg["end"][0]*h_scale, 6),
                                round(seg["end"][1]*h_scale, 6)]
            raw_scale = h_scale

    # --- Post-processing pipeline ---
    if deduplicate:
        all_segments = deduplicate_segments(all_segments)

    if filter_borders:
        all_segments = filter_border_outliers(all_segments)

    if min_segment_length > 0:
        all_segments = filter_tiny_segments(all_segments, min_segment_length)

    if normalize:
        all_segments = normalize_origin(all_segments)

    # --- Compute final bounding box ---
    bbox = compute_bounding_box(all_segments)

    # --- Report ---
    print(f"\n[INFO] Entity breakdown: {entity_counts}")
    print(f"[INFO] Total wall segments extracted: {len(all_segments)}")
    print(f"[INFO] Bounding box: {bbox}")
    bbox_w = bbox["max"][0] - bbox["min"][0]
    bbox_h = bbox["max"][1] - bbox["min"][1]
    print(f"[INFO] Building footprint: {bbox_w:.2f}m x {bbox_h:.2f}m")

    # --- Build output ---
    geometry_data = {
        "metadata": {
            "source": os.path.abspath(dxf_path),
            "layers_used": active_layers,
            "scale_factor": raw_scale,
            "arc_segments": arc_segments,
            "segment_count": len(all_segments),
            "bounding_box": bbox,
            "entity_counts": entity_counts,
            "units": "meters",
            "origin_normalized": normalize,
        },
        "walls": all_segments
    }

    # --- Write JSON ---
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    try:
        with open(output_path, 'w') as f:
            json.dump(geometry_data, f, indent=4)
        print(f"[OK] Geometry saved to {output_path}")
    except IOError as e:
        print(f"[ERROR] Failed to save JSON: {e}")
        sys.exit(1)

    return geometry_data


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python dxf_extractor.py <input_dxf> <output_json> "
              "[layer1,layer2,...] [scale] [arc_segments]")
        print("  Layers: comma-separated list (default: WALLS)")
        print("  Scale:  unit conversion factor (default: 1.0 = auto-detect)")
        print("  Arc:    tessellation segments for arcs/circles (default: 16)")
        sys.exit(1)

    input_dxf = sys.argv[1]
    output_json = sys.argv[2]
    layers = sys.argv[3].split(',') if len(sys.argv) > 3 else ["WALLS"]
    scale = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    arc_segs = int(sys.argv[5]) if len(sys.argv) > 5 else 16

    extract_walls(input_dxf, output_json, layers, scale, arc_segs)
