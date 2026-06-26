"""
ArchX3D — Complex Test DXF Generator
======================================
Creates a multi-room floor plan DXF using various entity types:
  - LINE (standard walls)
  - LWPOLYLINE (connected wall segments)
  - ARC (curved wall / bay window)
  - CIRCLE (structural column)

This is used to test the full entity support of dxf_extractor.py.
"""

import ezdxf


def create_complex_floorplan():
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()

    # Create the WALLS layer
    doc.layers.add('WALLS', color=7)

    # =========================================================================
    # ROOM 1: Living Room (0,0) to (8,6) — using LINE entities
    # =========================================================================
    line_walls = [
        # Outer walls (bottom, right partial, top, left)
        ((0, 0), (8, 0)),      # Bottom wall
        ((8, 0), (8, 6)),      # Right wall
        ((8, 6), (0, 6)),      # Top wall
        ((0, 6), (0, 0)),      # Left wall
    ]

    for start, end in line_walls:
        msp.add_line(start, end, dxfattribs={'layer': 'WALLS'})

    # =========================================================================
    # ROOM 2: Kitchen (8,0) to (14,6) — using LWPOLYLINE entity
    # A single polyline draws the kitchen as one connected shape
    # =========================================================================
    kitchen_points = [
        (8, 0),   # Shared wall with living room (bottom-left of kitchen)
        (14, 0),  # Bottom-right
        (14, 6),  # Top-right
        (8, 6),   # Top-left (shared wall)
    ]
    # Note: We don't close back to (8,0) because that wall is already
    # drawn by the living room. But we set closed=False to avoid duplication.
    msp.add_lwpolyline(
        kitchen_points,
        dxfattribs={'layer': 'WALLS'},
        close=False
    )

    # =========================================================================
    # Interior dividing wall between Living Room and Kitchen
    # =========================================================================
    msp.add_line((8, 0), (8, 2.5), dxfattribs={'layer': 'WALLS'})   # Door gap bottom
    msp.add_line((8, 3.5), (8, 6), dxfattribs={'layer': 'WALLS'})   # Door gap top
    # Gap from y=2.5 to y=3.5 is the doorway (1m wide)

    # =========================================================================
    # ROOM 3: Bathroom (0,6) to (4,9) — using LINE entities
    # =========================================================================
    bathroom_walls = [
        ((0, 6), (4, 6)),    # Bottom wall (shared, but we'll deduplicate)
        ((4, 6), (4, 9)),    # Right wall
        ((4, 9), (0, 9)),    # Top wall
        ((0, 9), (0, 6)),    # Left wall
    ]
    for start, end in bathroom_walls:
        msp.add_line(start, end, dxfattribs={'layer': 'WALLS'})

    # =========================================================================
    # ROOM 4: Bedroom (4,6) to (14,9) — using LINE entities
    # =========================================================================
    bedroom_walls = [
        ((4, 6), (14, 6)),   # Bottom wall (shared with kitchen top)
        ((14, 6), (14, 9)),  # Right wall
        ((14, 9), (4, 9)),   # Top wall
        ((4, 9), (4, 6)),    # Left wall (shared with bathroom)
    ]
    for start, end in bedroom_walls:
        msp.add_line(start, end, dxfattribs={'layer': 'WALLS'})

    # =========================================================================
    # ARC: Bay window in Living Room (curved wall on left side)
    # A semicircular protrusion at the bottom-left
    # =========================================================================
    msp.add_arc(
        center=(2, 0),       # Center on the bottom wall
        radius=1.5,          # 1.5m radius
        start_angle=180,     # Start pointing left
        end_angle=360,       # End pointing right (semicircle outward)
        dxfattribs={'layer': 'WALLS'}
    )

    # =========================================================================
    # CIRCLE: Structural column in the kitchen
    # =========================================================================
    msp.add_circle(
        center=(11, 3),      # Center of kitchen
        radius=0.3,          # 30cm radius column
        dxfattribs={'layer': 'WALLS'}
    )

    # Save
    filepath = 'test_complex.dxf'
    doc.saveas(filepath)
    print(f"[OK] Created complex test floorplan: {filepath}")
    print("   Contents:")
    print("   - Living Room (8x6m) with LINE entities")
    print("   - Kitchen (6x6m) with LWPOLYLINE entity")
    print("   - Bathroom (4x3m) with LINE entities")
    print("   - Bedroom (10x3m) with LINE entities")
    print("   - Interior wall with 1m doorway gap")
    print("   - Bay window ARC (r=1.5m)")
    print("   - Structural column CIRCLE (r=0.3m)")


if __name__ == "__main__":
    create_complex_floorplan()
