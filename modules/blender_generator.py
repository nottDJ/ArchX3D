"""
ArchX3D — Blender 3D Generator
================================
Runs inside Blender's Python environment (bpy).
Reads geometry.json + styling.json + config.json and generates:
  - 3D walls (extruded + solidified)
  - Floor plane
  - Ceiling plane (optional)
  - Materials (from styling or defaults)
  - 3-point lighting
  - Camera orbit animation
  - Exports: .glb (web) + .blend (editing)

Invoked headless: blender --background --python blender_generator.py
"""

import bpy
import bmesh
import json
import os
import sys
import math

# ---------------------------------------------------------------------------
# Path Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
GEOMETRY_PATH = os.path.join(DATA_DIR, 'geometry.json')
STYLING_PATH = os.path.join(DATA_DIR, 'styling.json')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
OUTPUT_GLB_PATH = os.path.join(OUTPUT_DIR, 'model.glb')
OUTPUT_BLEND_PATH = os.path.join(OUTPUT_DIR, 'scene.blend')

# ---------------------------------------------------------------------------
# Default Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "wall_height": 3.0,
    "wall_thickness": 0.15,
    "render": {
        "resolution_x": 960,
        "resolution_y": 540,
        "frames": 120,
        "fps": 24,
        "engine": "EEVEE"
    },
    "export": {
        "glb": True,
        "blend": True
    },
    "generate_ceiling": True,
    "generate_floor": True
}

DEFAULT_WALL_COLOR = "#B0BEC5"   # Blue-grey
DEFAULT_FLOOR_COLOR = "#D7CCC8"  # Warm taupe
DEFAULT_CEILING_COLOR = "#FAFAFA"  # Near-white


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)


def clean_scene():
    """Wipe the entire Blender scene to a blank slate."""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def hex_to_linear_rgb(hex_str):
    """Convert hex color string (#RRGGBB) to Blender Linear RGB (R, G, B, A)."""
    hex_str = hex_str.lstrip('#')
    if len(hex_str) == 3:
        hex_str = ''.join([c * 2 for c in hex_str])

    r = int(hex_str[0:2], 16) / 255.0
    g = int(hex_str[2:4], 16) / 255.0
    b = int(hex_str[4:6], 16) / 255.0

    def srgb_to_linear(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), 1.0)


# ---------------------------------------------------------------------------
# Material Creation
# ---------------------------------------------------------------------------

def create_material(name, hex_color, roughness=0.5, metallic=0.0):
    """Create a Principled BSDF material from a hex color."""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (0, 0)
    bsdf.inputs['Base Color'].default_value = hex_to_linear_rgb(hex_color)
    bsdf.inputs['Roughness'].default_value = roughness
    bsdf.inputs['Metallic'].default_value = metallic

    node_output = nodes.new(type='ShaderNodeOutputMaterial')
    node_output.location = (400, 0)

    mat.node_tree.links.new(bsdf.outputs['BSDF'], node_output.inputs['Surface'])
    return mat


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def load_config():
    """Load config.json or return defaults."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                user_config = json.load(f)
            # Merge with defaults (shallow)
            config = {**DEFAULT_CONFIG, **user_config}
            # Deep merge for nested dicts
            for key in ["render", "export"]:
                if key in DEFAULT_CONFIG and key in user_config:
                    config[key] = {**DEFAULT_CONFIG[key], **user_config[key]}
            print(f"[CONFIG] Loaded from {CONFIG_PATH}")
            return config
        except Exception as e:
            print(f"[WARN] Failed to load config: {e}. Using defaults.")
    return DEFAULT_CONFIG.copy()


def load_geometry():
    """Load geometry.json — required."""
    if not os.path.exists(GEOMETRY_PATH):
        print(f"[ERROR] geometry.json not found at {GEOMETRY_PATH}")
        sys.exit(1)
    with open(GEOMETRY_PATH, 'r') as f:
        return json.load(f)


def load_styling():
    """Load styling.json — optional. Returns None if unavailable."""
    if not os.path.exists(STYLING_PATH):
        print("[INFO] No styling.json found. Using default materials.")
        return None
    try:
        with open(STYLING_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to load styling: {e}. Using defaults.")
        return None


# ---------------------------------------------------------------------------
# Geometry Builders
# ---------------------------------------------------------------------------

def get_bounding_box(geometry):
    """Extract or compute the bounding box from geometry data."""
    meta = geometry.get("metadata", {})
    bbox = meta.get("bounding_box")
    if bbox:
        return bbox["min"], bbox["max"]

    # Compute from wall segments
    walls = geometry.get("walls", [])
    if not walls:
        return [0, 0], [1, 1]

    all_x = []
    all_y = []
    for w in walls:
        all_x.extend([w["start"][0], w["end"][0]])
        all_y.extend([w["start"][1], w["end"][1]])

    return [min(all_x), min(all_y)], [max(all_x), max(all_y)]


def create_walls(geometry, config, wall_material):
    """Build 3D walls from 2D line segments.
    
    Process:
    1. Create vertices at Z=0 for each unique point.
    2. Create edges connecting start→end for each wall segment.
    3. Remove duplicate vertices (within tolerance).
    4. Extrude all edges upward by wall_height.
    5. Apply Solidify modifier for wall thickness.
    """
    wall_height = config.get("wall_height", 3.0)
    wall_thickness = config.get("wall_thickness", 0.15)

    mesh = bpy.data.meshes.new("WallsMesh")
    obj = bpy.data.objects.new("Walls", mesh)
    bpy.context.collection.objects.link(obj)

    bm = bmesh.new()
    vert_map = {}

    def get_vert(xy):
        key = (round(xy[0], 4), round(xy[1], 4))
        if key not in vert_map:
            vert_map[key] = bm.verts.new((xy[0], xy[1], 0))
        return vert_map[key]

    for wall in geometry.get('walls', []):
        v1 = get_vert(wall['start'])
        v2 = get_vert(wall['end'])
        if v1 != v2:
            try:
                bm.edges.new((v1, v2))
            except ValueError:
                pass  # Edge already exists

    # Clean up near-coincident vertices
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.001)

    if not bm.edges:
        print("[WARN] No wall edges created!")
        bm.free()
        return obj

    # Extrude edges upward
    ret = bmesh.ops.extrude_edge_only(bm, edges=bm.edges)
    geom_extrude = ret["geom"]
    verts_extrude = [v for v in geom_extrude if isinstance(v, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, vec=(0, 0, wall_height), verts=verts_extrude)

    bm.to_mesh(mesh)
    bm.free()

    # Apply Solidify modifier for wall thickness
    solidify = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
    solidify.thickness = wall_thickness
    solidify.offset = 0.0  # Center the thickness on the edge

    # Apply the modifier so it bakes into the mesh (important for GLB export)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier="Solidify")

    # Assign material
    if wall_material:
        obj.data.materials.append(wall_material)

    # Smooth shading for better visual quality
    bpy.ops.object.shade_smooth()

    print(f"[OK] Walls created: height={wall_height}m, thickness={wall_thickness}m, "
          f"vertices={len(obj.data.vertices)}, faces={len(obj.data.polygons)}")

    return obj


def create_floor(geometry, floor_material):
    """Create a floor plane matching the bounding box of the walls."""
    bb_min, bb_max = get_bounding_box(geometry)

    center_x = (bb_min[0] + bb_max[0]) / 2
    center_y = (bb_min[1] + bb_max[1]) / 2
    width = bb_max[0] - bb_min[0]
    height = bb_max[1] - bb_min[1]

    # Add a slight margin so floor extends under walls
    margin = 0.01
    bpy.ops.mesh.primitive_plane_add(
        size=1,
        location=(center_x, center_y, -margin)
    )
    floor = bpy.context.active_object
    floor.name = "Floor"
    floor.scale.x = width + margin * 2
    floor.scale.y = height + margin * 2

    if floor_material:
        floor.data.materials.append(floor_material)

    print(f"[OK] Floor created: {width:.1f}m × {height:.1f}m")
    return floor, center_x, center_y, max(width, height)


def create_ceiling(geometry, config, ceiling_material):
    """Create a ceiling plane at wall_height."""
    bb_min, bb_max = get_bounding_box(geometry)
    wall_height = config.get("wall_height", 3.0)

    center_x = (bb_min[0] + bb_max[0]) / 2
    center_y = (bb_min[1] + bb_max[1]) / 2
    width = bb_max[0] - bb_min[0]
    height = bb_max[1] - bb_min[1]

    bpy.ops.mesh.primitive_plane_add(
        size=1,
        location=(center_x, center_y, wall_height)
    )
    ceiling = bpy.context.active_object
    ceiling.name = "Ceiling"
    ceiling.scale.x = width
    ceiling.scale.y = height

    if ceiling_material:
        ceiling.data.materials.append(ceiling_material)

    print(f"[OK] Ceiling created at Z={wall_height}m")
    return ceiling


# ---------------------------------------------------------------------------
# Lighting & Camera
# ---------------------------------------------------------------------------

def setup_lighting(center_x, center_y, max_dim, config):
    """Set up a 3-point lighting system for architectural visualization.
    
    - Key Light: Main sun light (warm, angled)
    - Fill Light: Softer area light (cool, opposite side)
    - Rim Light: Back light for edge definition
    - World: Neutral sky background
    """
    scene = bpy.context.scene
    wall_height = config.get("wall_height", 3.0)

    # --- Key Light (Sun) ---
    bpy.ops.object.light_add(
        type='SUN',
        location=(center_x + max_dim, center_y + max_dim, max_dim * 2)
    )
    key_light = bpy.context.object
    key_light.name = "KeyLight"
    key_light.data.energy = 3.0
    # Warm tint
    key_light.data.color = (1.0, 0.95, 0.9)

    # --- Fill Light (Area) ---
    bpy.ops.object.light_add(
        type='AREA',
        location=(center_x - max_dim * 0.8, center_y - max_dim * 0.5, wall_height * 0.8)
    )
    fill_light = bpy.context.object
    fill_light.name = "FillLight"
    fill_light.data.energy = 50.0  # Area lights need higher energy
    fill_light.data.size = max_dim * 0.5
    # Cool tint
    fill_light.data.color = (0.85, 0.9, 1.0)

    # --- Rim Light (Point) ---
    bpy.ops.object.light_add(
        type='POINT',
        location=(center_x, center_y + max_dim * 1.5, wall_height * 1.5)
    )
    rim_light = bpy.context.object
    rim_light.name = "RimLight"
    rim_light.data.energy = 200.0
    rim_light.data.color = (1.0, 1.0, 1.0)

    # --- World Background ---
    world = bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    bg_node = world.node_tree.nodes["Background"]
    bg_node.inputs[0].default_value = (0.85, 0.9, 0.95, 1.0)  # Soft sky blue
    bg_node.inputs[1].default_value = 0.5  # Subdued so scene lighting dominates

    print("[OK] 3-point lighting configured (Key + Fill + Rim + World)")


def setup_camera_and_animation(center_x, center_y, max_dim, config):
    """Set up an orbiting camera pointing at the building center."""
    scene = bpy.context.scene
    render_cfg = config.get("render", {})
    wall_height = config.get("wall_height", 3.0)

    # Camera at an elevated angle looking down at the model
    cam_distance = max_dim * 1.5
    cam_z = wall_height * 2.5
    bpy.ops.object.camera_add(
        location=(center_x, center_y - cam_distance, cam_z)
    )
    camera = bpy.context.active_object
    camera.name = "MainCamera"
    camera.data.lens = 28  # Wide-angle for architectural views
    camera.data.clip_start = 0.01
    camera.data.clip_end = 500.0

    # Track-to target at building center
    bpy.ops.object.empty_add(
        type='PLAIN_AXES',
        location=(center_x, center_y, wall_height * 0.4)
    )
    target = bpy.context.active_object
    target.name = "CameraTarget"

    track = camera.constraints.new(type='TRACK_TO')
    track.target = target
    track.track_axis = 'TRACK_NEGATIVE_Z'
    track.up_axis = 'UP_Y'

    # Orbit pivot
    bpy.ops.object.empty_add(
        type='PLAIN_AXES',
        location=(center_x, center_y, 0)
    )
    pivot = bpy.context.active_object
    pivot.name = "CameraOrbitPivot"
    camera.parent = pivot
    camera.matrix_parent_inverse = pivot.matrix_world.inverted()

    # Animation: full 360° orbit
    frame_count = render_cfg.get("frames", 120)
    fps = render_cfg.get("fps", 24)

    scene.frame_start = 1
    scene.frame_end = frame_count
    scene.render.fps = fps

    pivot.rotation_euler = (0, 0, 0)
    pivot.keyframe_insert(data_path="rotation_euler", frame=1)
    pivot.rotation_euler = (0, 0, 2 * math.pi)
    pivot.keyframe_insert(data_path="rotation_euler", frame=frame_count + 1)

    # Make the rotation interpolation linear (no ease-in/out)
    if pivot.animation_data and pivot.animation_data.action:
        for fcurve in pivot.animation_data.action.fcurves:
            for kp in fcurve.keyframe_points:
                kp.interpolation = 'LINEAR'

    scene.camera = camera
    print(f"[OK] Camera configured: orbit, {frame_count} frames @ {fps}fps")

    return camera


# ---------------------------------------------------------------------------
# Render Settings
# ---------------------------------------------------------------------------

def setup_render(config):
    """Configure the render engine and settings."""
    scene = bpy.context.scene
    render_cfg = config.get("render", {})

    # Engine selection — handle Blender 4.x/5.x naming
    engine = render_cfg.get("engine", "EEVEE").upper()
    if engine == "EEVEE":
        # Try the newer name first (Blender 4.x+), fall back to legacy
        try:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
            print("[RENDER] Using BLENDER_EEVEE_NEXT engine")
        except Exception:
            try:
                scene.render.engine = "BLENDER_EEVEE"
                print("[RENDER] Using BLENDER_EEVEE engine (legacy)")
            except Exception:
                print("[WARN] Could not set EEVEE engine, using default")
    elif engine == "CYCLES":
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 64
        print("[RENDER] Using Cycles engine (64 samples)")

    scene.render.resolution_x = render_cfg.get("resolution_x", 960)
    scene.render.resolution_y = render_cfg.get("resolution_y", 540)
    scene.render.image_settings.file_format = 'PNG'

    # Enable ambient occlusion if available
    try:
        scene.eevee.use_gtao = True
        scene.eevee.gtao_distance = 1.0
    except AttributeError:
        pass  # Not available in this Blender version

    print(f"[RENDER] Resolution: {scene.render.resolution_x}×{scene.render.resolution_y}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_frames():
    """Render all animation frames as PNG images."""
    scene = bpy.context.scene
    frames_dir = os.path.join(OUTPUT_DIR, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    total = scene.frame_end - scene.frame_start + 1
    print(f"[RENDER] Rendering {total} frames...")

    for f in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(f)
        scene.render.filepath = os.path.join(frames_dir, f'frame_{f:04d}.png')
        bpy.ops.render.render(write_still=True)
        if f % 20 == 0:
            print(f"  Frame {f}/{scene.frame_end}")

    print(f"[OK] All {total} frames rendered to {frames_dir}")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_scene(config):
    """Export the scene in configured formats."""
    export_cfg = config.get("export", {})

    if export_cfg.get("glb", True):
        try:
            bpy.ops.export_scene.gltf(
                filepath=OUTPUT_GLB_PATH,
                export_format='GLB'
            )
            glb_size = os.path.getsize(OUTPUT_GLB_PATH)
            print(f"[EXPORT] GLB saved: {OUTPUT_GLB_PATH} ({glb_size:,} bytes)")
        except Exception as e:
            print(f"[ERROR] GLB export failed: {e}")

    if export_cfg.get("blend", True):
        try:
            bpy.ops.wm.save_as_mainfile(filepath=OUTPUT_BLEND_PATH)
            print(f"[EXPORT] Blend saved: {OUTPUT_BLEND_PATH}")
        except Exception as e:
            print(f"[ERROR] Blend save failed: {e}")


# ---------------------------------------------------------------------------
# Material Resolution
# ---------------------------------------------------------------------------

def resolve_materials(styling):
    """Build materials from styling.json data, or use defaults.
    
    Returns (wall_material, floor_material, ceiling_material)
    """
    if not styling or not styling.get("rooms"):
        print("[MATERIALS] Using default colors (no styling data)")
        wall_mat = create_material("WallMaterial", DEFAULT_WALL_COLOR, roughness=0.8)
        floor_mat = create_material("FloorMaterial", DEFAULT_FLOOR_COLOR, roughness=0.3)
        ceiling_mat = create_material("CeilingMaterial", DEFAULT_CEILING_COLOR, roughness=0.9)
        return wall_mat, floor_mat, ceiling_mat

    # Use the first room's wall color as the primary wall color
    rooms = styling["rooms"]
    wall_hex = rooms[0].get("wall_color_hex", DEFAULT_WALL_COLOR)
    
    # Determine floor color from floor material description
    floor_mat_name = rooms[0].get("floor_material", "").lower()
    if "oak" in floor_mat_name or "wood" in floor_mat_name:
        floor_hex = "#C19A6B"  # Warm wood
    elif "concrete" in floor_mat_name:
        floor_hex = "#808080"
    elif "tile" in floor_mat_name or "marble" in floor_mat_name:
        floor_hex = "#E8E0D8"
    elif "dark" in floor_mat_name:
        floor_hex = "#4A3728"
    else:
        floor_hex = DEFAULT_FLOOR_COLOR

    style_name = styling.get("overall_style", "Default")
    print(f"[MATERIALS] Style: {style_name} | Walls: {wall_hex} | Floor: {floor_hex}")

    wall_mat = create_material("WallMaterial", wall_hex, roughness=0.8)
    floor_mat = create_material("FloorMaterial", floor_hex, roughness=0.3)
    ceiling_mat = create_material("CeilingMaterial", DEFAULT_CEILING_COLOR, roughness=0.9)

    return wall_mat, floor_mat, ceiling_mat


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  ArchX3D — Blender 3D Generator")
    print("=" * 60)

    ensure_output_dir()
    clean_scene()

    # Load all data
    config = load_config()
    geometry = load_geometry()
    styling = load_styling()

    # Build materials
    wall_mat, floor_mat, ceiling_mat = resolve_materials(styling)

    # Build geometry
    walls_obj = create_walls(geometry, config, wall_mat)

    if config.get("generate_floor", True):
        floor_obj, cx, cy, max_dim = create_floor(geometry, floor_mat)
    else:
        bb_min, bb_max = get_bounding_box(geometry)
        cx = (bb_min[0] + bb_max[0]) / 2
        cy = (bb_min[1] + bb_max[1]) / 2
        max_dim = max(bb_max[0] - bb_min[0], bb_max[1] - bb_min[1])

    if config.get("generate_ceiling", True):
        create_ceiling(geometry, config, ceiling_mat)

    # Lighting & Camera
    setup_lighting(cx, cy, max_dim, config)
    setup_render(config)
    setup_camera_and_animation(cx, cy, max_dim, config)

    # Export
    export_scene(config)

    # Render frames for video
    render_frames()

    print("=" * 60)
    print("  ArchX3D — Generation Complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()