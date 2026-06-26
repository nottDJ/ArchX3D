import ezdxf

def create_sample_floorplan():
    # Create a new DXF document
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()

    # Create the layer your pipeline looks for
    doc.layers.add('WALLS', color=7)

    # Define a simple 2-room apartment (Outer dimensions 10m x 6m)
    # Format: (start_x, start_y), (end_x, end_y)
    wall_lines = [
        # Outer Walls
        ((0, 0), (10, 0)),   # Bottom wall
        ((10, 0), (10, 6)),  # Right wall
        ((10, 6), (0, 6)),   # Top wall
        ((0, 6), (0, 0)),    # Left wall
        
        # Interior Dividing Wall (creating a 6x6 Living Room and 4x6 Bedroom)
        ((6, 0), (6, 6))
    ]

    # Draw the lines into the DXF 'WALLS' layer
    for start_point, end_point in wall_lines:
        msp.add_line(start_point, end_point, dxfattribs={'layer': 'WALLS'})

    # Save the file
    filepath = 'test_floorplan.dxf'
    doc.saveas(filepath)
    print(f"[OK] Success! Created sample floorplan: {filepath}")
    print("It contains 1 outer shell and 1 interior dividing wall.")

if __name__ == "__main__":
    create_sample_floorplan()