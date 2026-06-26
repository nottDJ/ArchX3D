import os
import json
import google.generativeai as genai
import sys

def generate_styling(geometry_path, output_path):
    """
    Generates interior design styling using Gemini API based on geometry data.
    """
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable not found.")
        sys.exit(1)

    try:
        with open(geometry_path, 'r') as f:
            geometry_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Geometry file not found at {geometry_path}")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in {geometry_path}")
        sys.exit(1)

    genai.configure(api_key=api_key)

    # Calculate basic dimensions/area to give context to the model
    # This is a simple heuristic or we just pass the raw walls
    # Passing raw walls might be too much token usage if complex, but for this MVP it's fine.
    # Let's verify the "walls" structure in geometry.json from previous turn.
    # It has "start" and "end" points.
    
    prompt = f"""
    You are an expert interior designer. Analyze the following 2D floor plan geometry data (walls defined by start/end coordinates in meters) and generate a cohesive interior design style.

    Geometry Data:
    {json.dumps(geometry_data, indent=2)}

    Based on the dimensions and layout, infer probable room types (e.g., Living Room, Bedroom, Kitchen). 
    The layout seems to be a 10x6m area divided into two 5x6m rooms by a wall at x=5.

    Output a STRICT JSON object (no markdown formatting, no code blocks) with the following schema:
    {{
      "rooms": [
        {{
          "room_type": "guessed_type",
          "description": "brief rationale",
          "wall_color_hex": "#RRGGBB",
          "floor_material": "material_name",
          "furniture_assets": [
            "low_poly_asset_1",
            "low_poly_asset_2",
            "low_poly_asset_3"
          ]
        }}
      ],
      "overall_style": "style_name (e.g., Modern, Scandinavian)"
    }}

    Ensure there are 3-5 low-poly furniture assets per room.
    """

    model = genai.GenerativeModel("gemini-2.5-flash")
    
    print("Querying Gemini API for styling...")
    try:
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        
        styling_data = json.loads(response.text)
        
        with open(output_path, 'w') as f:
            json.dump(styling_data, f, indent=4)
            
        print(f"Styling data saved to {output_path}")
        
    except Exception as e:
        print(f"Error calling Gemini API or saving output: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python style_generator.py <geometry_json> <output_json>")
        # Default for local testing if arguments not provided
        geometry = "data/geometry.json"
        output = "data/styling.json"
        print(f"Using default paths: {geometry} -> {output}")
        generate_styling(geometry, output)
    else:
        generate_styling(sys.argv[1], sys.argv[2])
