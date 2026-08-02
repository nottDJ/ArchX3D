"""
Generate a realistic apartment DXF for semantic-pipeline tests.

Why a generated fixture
-----------------------
The repository's existing test files (``test_floorplan.dxf``,
``test_complex.dxf``) contain wall lines on a single ``WALLS`` layer and
nothing else — no blocks, no text, no layer conventions. They cannot exercise
any part of the CAD semantic layer, so a drawing that does was needed.

This generates a 2-bedroom apartment drawn the way a real one is: AIA/NCS
layer names, room labels as MTEXT, furniture and sanitary ware as named
blocks, doors and windows on their own layers, dimensions, a north arrow, and
a title block placed far outside the building so that origin normalisation is
actually tested.

Run directly to regenerate::

    python tests/fixtures/make_apartment.py

The output is committed, so tests do not depend on ezdxf's writer.
"""

from __future__ import annotations

import os

import ezdxf

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apartment.dxf")

#: Drawn in millimetres, like most real architectural DXFs, so unit detection
#: has something to detect. $INSUNITS is set accordingly.
MM = 1000.0

#: AIA / National CAD Standard layer names, with their ACI colours.
LAYERS = {
    "A-WALL": 7,
    "A-WALL-PATT": 8,
    "A-DOOR": 3,
    "A-GLAZ": 4,
    "A-FLOR-PFIX": 5,       # Plumbing fixtures
    "A-FLOR-CASE": 6,       # Casework
    "A-FURN": 2,
    "A-ANNO-TEXT": 1,
    "A-ANNO-DIMS": 1,
    "A-ANNO-TTLB": 7,
    "A-AREA-IDEN": 1,
    "COLS": 7,
    "DEFPOINTS": 7,
}


# ---------------------------------------------------------------------------
# Plan definition
# ---------------------------------------------------------------------------
#
# Rooms as (name, x0, y0, x1, y1) rectangles in metres, laid out so the
# resulting areas land inside the taxonomy's priors:
#
#   +----------------------------------------------------+
#   |  LIVING ROOM    |  KITCHEN   |      BEDROOM 2       |
#   |    28.0 m2      |  12.0 m2   |       16.0 m2        |
#   |-----------------+------------+----------------------|
#   |    HALL         |   BATH     |    MASTER BEDROOM    |
#   |    21.6 m2      |   8.6 m2   |       20.2 m2        |
#   +----------------------------------------------------+
#
# Overall 14.0 x 7.6 m. Every area sits inside its type's prior in
# ``semantic.taxonomy``, so a correct classification is achievable from
# geometry alone and the label/block signals are testable against it.

ROOMS = [
    # name,            x0,   y0,   x1,   y1
    ("LIVING ROOM",   0.0,  3.6,  7.0,  7.6),
    ("KITCHEN",       7.0,  3.6, 10.0,  7.6),
    ("BEDROOM 2",    10.0,  3.6, 14.0,  7.6),
    ("HALL",          0.0,  0.0,  6.0,  3.6),
    ("BATH",          6.0,  0.0,  8.4,  3.6),
    ("MASTER BED",    8.4,  0.0, 14.0,  3.6),
]

#: Blocks: (block_name, layer, x, y, rotation)
BLOCKS = [
    # --- Sanitary: the decisive bathroom evidence.
    ("WC",              "A-FLOR-PFIX",  6.5,  0.6,   0),
    ("WASH-BASIN",      "A-FLOR-PFIX",  6.5,  2.4,   0),
    ("SHOWER-TRAY",     "A-FLOR-PFIX",  7.9,  2.9,   0),

    # --- Kitchen.
    ("KITCHEN-SINK",    "A-FLOR-PFIX",  7.4,  7.1,   0),
    ("HOB-4BURNER",     "A-FLOR-CASE",  8.6,  7.1,   0),
    ("REFRIGERATOR",    "A-FURN",       9.5,  4.1,   0),
    ("COUNTER-TOP",     "A-FLOR-CASE",  8.5,  6.9,   0),

    # --- Master bedroom.
    ("BED-QUEEN",       "A-FURN",      11.2,  1.4,   0),
    ("WARDROBE",        "A-FLOR-CASE", 13.5,  1.8,  90),
    ("BEDSIDE-TABLE",   "A-FURN",       9.8,  2.4,   0),
    ("BEDSIDE-TABLE",   "A-FURN",      12.6,  2.4,   0),

    # --- Bedroom 2.
    ("BED-SINGLE",      "A-FURN",      11.5,  5.0,   0),
    ("WARDROBE",        "A-FLOR-CASE", 13.5,  6.6,  90),

    # --- Living room.
    ("SOFA-3SEAT",      "A-FURN",       2.0,  4.4,   0),
    ("COFFEE-TABLE",    "A-FURN",       2.0,  5.6,   0),
    ("TV-UNIT",         "A-FURN",       2.0,  7.2,   0),
    ("DINING-TABLE",    "A-FURN",       5.4,  5.6,   0),
    ("DINING-CHAIR",    "A-FURN",       4.8,  5.6,   0),
    ("DINING-CHAIR",    "A-FURN",       6.0,  5.6,   0),

    # --- Drawing furniture, which must NOT be read as room contents.
    ("NORTH-ARROW",     "A-ANNO-TTLB", -4.0,  9.0,   0),
    ("TITLE-BLOCK",     "A-ANNO-TTLB", -8.0, -4.0,   0),
]

#: Doors: (x, y, rotation) — placed in wall openings.
DOORS = [
    (3.0, 3.6,   0),   # hall -> living
    (7.6, 3.6,   0),   # hall -> kitchen (via living)
    (6.9, 3.6,   0),   # hall -> bath
    (8.9, 3.6,   0),   # hall -> master bed
    (12.0, 3.6,  0),   # hall -> bedroom 2
    (0.6, 0.0,   0),   # entrance
]

#: Windows: (x, y, rotation)
WINDOWS = [
    (2.0, 7.6, 0), (4.5, 7.6, 0),   # living room
    (8.5, 7.6, 0),                  # kitchen
    (12.0, 7.6, 0),                 # bedroom 2
    (11.0, 0.0, 0),                 # master bedroom
]


# ---------------------------------------------------------------------------
# Block definitions
# ---------------------------------------------------------------------------
#
# Each block holds a simple rectangle at its real size, so that exploding it
# yields a plausible footprint. Sizes in metres, converted on insert.

BLOCK_SIZES = {
    "WC":             (0.37, 0.65),
    "WASH-BASIN":     (0.55, 0.42),
    "SHOWER-TRAY":    (0.90, 0.90),
    "KITCHEN-SINK":   (0.80, 0.50),
    "HOB-4BURNER":    (0.60, 0.52),
    "REFRIGERATOR":   (0.70, 0.68),
    "COUNTER-TOP":    (2.60, 0.60),
    "BED-QUEEN":      (1.60, 2.00),
    "BED-SINGLE":     (0.90, 1.90),
    "WARDROBE":       (1.80, 0.60),
    "BEDSIDE-TABLE":  (0.45, 0.40),
    "SOFA-3SEAT":     (2.10, 0.90),
    "COFFEE-TABLE":   (1.10, 0.60),
    "TV-UNIT":        (1.60, 0.45),
    "DINING-TABLE":   (1.40, 0.90),
    "DINING-CHAIR":   (0.45, 0.45),
    "DOOR-SINGLE":    (0.90, 0.06),
    "WINDOW-CASEMENT": (1.20, 0.20),
    "NORTH-ARROW":    (0.60, 1.20),
    "TITLE-BLOCK":    (8.00, 3.00),
}


def _define_blocks(doc):
    """Create every block definition as a centred rectangle on layer 0.

    Layer ``0`` on purpose: it is the CAD convention for block geometry that
    should inherit the INSERT's layer, and the reader's handling of that
    convention is one of the behaviours worth having a fixture for.
    """
    for name, (width, depth) in BLOCK_SIZES.items():
        block = doc.blocks.new(name=name)
        hw, hd = width * MM / 2.0, depth * MM / 2.0
        block.add_lwpolyline(
            [(-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)],
            close=True, dxfattribs={"layer": "0"},
        )


def build():
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4          # millimetres
    doc.header["$NORTHDIRECTION"] = 1.5707963267948966  # pi/2 -> north is +Y

    for name, colour in LAYERS.items():
        # `setup=True` already creates some standard layers, DEFPOINTS among
        # them, so existing entries are updated rather than re-created.
        if name in doc.layers:
            doc.layers.get(name).dxf.color = colour
        else:
            doc.layers.new(name=name, dxfattribs={"color": colour})

    _define_blocks(doc)
    msp = doc.modelspace()

    # ---- Walls -----------------------------------------------------------
    # Each room drawn as a closed rectangle. Shared edges are drawn twice,
    # which is exactly what real drawings do and what deduplication handles.
    for _, x0, y0, x1, y1 in ROOMS:
        msp.add_lwpolyline(
            [
                (x0 * MM, y0 * MM), (x1 * MM, y0 * MM),
                (x1 * MM, y1 * MM), (x0 * MM, y1 * MM),
            ],
            close=True,
            dxfattribs={"layer": "A-WALL"},
        )

    # ---- Room labels -----------------------------------------------------
    # MTEXT with an area annotation underneath, the way plans are annotated.
    for name, x0, y0, x1, y1 in ROOMS:
        cx, cy = (x0 + x1) / 2.0 * MM, (y0 + y1) / 2.0 * MM
        area = (x1 - x0) * (y1 - y0)
        msp.add_mtext(
            f"{name}\\P{area:.2f} SQ.M.",
            dxfattribs={"layer": "A-ANNO-TEXT", "char_height": 180},
        ).set_location((cx, cy))

    # ---- Blocks ----------------------------------------------------------
    for name, layer, x, y, rotation in BLOCKS:
        msp.add_blockref(
            name, (x * MM, y * MM),
            dxfattribs={"layer": layer, "rotation": rotation},
        )

    for x, y, rotation in DOORS:
        msp.add_blockref(
            "DOOR-SINGLE", (x * MM, y * MM),
            dxfattribs={"layer": "A-DOOR", "rotation": rotation},
        )

    for x, y, rotation in WINDOWS:
        msp.add_blockref(
            "WINDOW-CASEMENT", (x * MM, y * MM),
            dxfattribs={"layer": "A-GLAZ", "rotation": rotation},
        )

    # ---- A room-tag block carrying structured metadata -------------------
    # Tier 1 evidence: an attribute that names the room outright. Only one
    # room gets one, so tests can tell attribute-driven classification apart
    # from label-driven classification.
    tag = doc.blocks.new(name="ROOM-TAG")
    tag.add_attdef(
        tag="ROOM_NAME", insert=(0, 0),
        dxfattribs={"height": 150, "layer": "A-AREA-IDEN"},
    )
    reference = msp.add_blockref(
        "ROOM-TAG", (2.0 * MM, 1.8 * MM),
        dxfattribs={"layer": "A-AREA-IDEN"},
    )
    reference.add_auto_attribs({"ROOM_NAME": "ENTRANCE HALL"})

    # ---- Dimensions ------------------------------------------------------
    for x0, x1, y in ((0.0, 7.0, 8.4), (7.0, 10.0, 8.4), (10.0, 14.0, 8.4)):
        msp.add_linear_dim(
            base=(0, y * MM),
            p1=(x0 * MM, 7.6 * MM),
            p2=(x1 * MM, 7.6 * MM),
            dxfattribs={"layer": "A-ANNO-DIMS"},
        ).render()

    # ---- Title block text, far from the building -------------------------
    # Deliberately placed well outside the plan so that origin normalisation
    # has to ignore it. Centring on all geometry would push the building
    # several metres off the origin.
    for offset, content in enumerate((
        "GROUND FLOOR PLAN",
        "SCALE 1:100",
        "DRAWN BY: ARCHX",
        "ALL DIMENSIONS IN MM",
    )):
        msp.add_text(
            content,
            dxfattribs={"layer": "A-ANNO-TTLB", "height": 200},
        ).set_placement((-8.0 * MM, (-4.0 - offset * 0.5) * MM))

    # ---- Construction geometry that must be ignored ----------------------
    msp.add_line(
        (-20.0 * MM, -20.0 * MM), (40.0 * MM, 40.0 * MM),
        dxfattribs={"layer": "DEFPOINTS"},
    )

    doc.saveas(OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    path = build()
    print(f"wrote {path}")
