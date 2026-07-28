"""
ArchX3D — Blender 3D Generator
================================
Runs inside Blender's Python environment (bpy).
Reads geometry.json + scene_graph.json + config.json and generates:
  - 3D walls (extruded + solidified)
  - Floor plane
  - Ceiling plane (optional)
  - Materials from the observed room finishes
  - Procedural furniture and decor from the vision scene graph
  - Luminaires recovered from the reference imagery
  - Camera orbit animation
  - Exports: .glb (web) + .blend (editing)

Data sources, in order of preference:
  1. ``data/scene_graph.json`` — the vision pipeline's output (furnished)
  2. ``data/styling.json``     — the legacy text-only styling (colours only)
  3. built-in defaults

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
MODULES_DIR = os.path.join(BASE_DIR, 'modules')
DATA_DIR = os.path.join(BASE_DIR, 'data')
OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
GEOMETRY_PATH = os.path.join(DATA_DIR, 'geometry.json')
STYLING_PATH = os.path.join(DATA_DIR, 'styling.json')
SCENE_GRAPH_PATH = os.path.join(DATA_DIR, 'scene_graph.json')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
OUTPUT_GLB_PATH = os.path.join(OUTPUT_DIR, 'model.glb')
OUTPUT_BLEND_PATH = os.path.join(OUTPUT_DIR, 'scene.blend')

# Blender's bundled Python does not see the project on sys.path.
if MODULES_DIR not in sys.path:
    sys.path.insert(0, MODULES_DIR)

# The vision modules imported here are deliberately stdlib-only so they load
# inside Blender. If they are unavailable the generator still runs, producing
# the unfurnished architectural shell.
try:
    import blender_furniture
    from vision import assets as vision_assets
    from vision import catalog as vision_catalog
    from vision.schema import SceneGraph
    # The appearance layer: species materials, palette tinting, style policy,
    # lighting reconstruction and viewpoint cameras. Split out of this file so
    # each concern is separately readable and the bpy-free parts are testable.
    from blender import camera as bl_camera
    from blender import lighting as bl_lighting
    from blender import materials as bl_materials
    from blender import styles as bl_styles
    VISION_AVAILABLE = True
except ImportError as _exc:  # pragma: no cover - depends on install layout
    print(f"[WARN] Vision modules unavailable ({_exc}); furniture will be skipped.")
    VISION_AVAILABLE = False

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


def load_scene_graph():
    """Load the vision scene graph — optional. Returns None if unavailable."""
    if not VISION_AVAILABLE or not os.path.exists(SCENE_GRAPH_PATH):
        return None
    try:
        graph = SceneGraph.load(SCENE_GRAPH_PATH)
    except Exception as e:
        print(f"[WARN] Failed to load scene_graph.json: {e}")
        return None

    print(f"[SCENE] Loaded scene graph v{graph.schema_version}: "
          f"{graph.room.room_type} ({graph.room.style}), "
          f"{len(graph.objects)} objects, {len(graph.lights)} lights")
    return graph


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


def iter_action_fcurves(action):
    """Yield an Action's F-Curves across Blender API generations.

    Blender 4.4 moved F-Curves under Action layers → strips → channelbags, and
    5.0 removed the flat ``action.fcurves`` accessor entirely. Handle both so
    the orbit animation works on either.
    """
    if action is None:
        return

    flat = getattr(action, "fcurves", None)
    if flat is not None:
        for fcurve in flat:
            yield fcurve
        return

    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            for bag in getattr(strip, "channelbags", []) or []:
                for fcurve in getattr(bag, "fcurves", []) or []:
                    yield fcurve


def set_linear_interpolation(obj):
    """Force LINEAR interpolation on every keyframe of an object."""
    anim = getattr(obj, "animation_data", None)
    if not anim or not anim.action:
        return

    count = 0
    for fcurve in iter_action_fcurves(anim.action):
        for keyframe in fcurve.keyframe_points:
            keyframe.interpolation = 'LINEAR'
            count += 1

    if count == 0:
        print("[WARN] Could not reach keyframes to set linear interpolation; "
              "the orbit may ease in and out.")


def setup_camera_and_animation(center_x, center_y, max_dim, config, graph=None):
    """Build the walkthrough orbit plus a camera per stored reference viewpoint.

    The viewpoint cameras are not made active. They exist so a preview can be
    rendered from each and compared against the photograph it was fitted to —
    see ``vision.similarity``.
    """
    if not VISION_AVAILABLE:
        return None

    cameras = bl_camera.build(graph, center_x, center_y, max_dim, config)

    for note in cameras.notes:
        print(f"[CAMERA] {note}")
    print(f"[CAMERA] {cameras.summary()}")

    for image_id, camera in sorted(cameras.by_image.items()):
        print(f"[CAMERA]   {camera.name}: reproduces {camera['archx3d_source_image']}")

    return cameras.walkthrough


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
# Scene Graph → Materials
# ---------------------------------------------------------------------------

def library_for(graph):
    """Build the material library, configured by the scene's dominant style."""
    style, confidence = _dominant_style(graph)
    library = bl_materials.MaterialLibrary(style=style, style_confidence=confidence)
    print(f"[STYLE] {bl_styles.describe(style)} (confidence {confidence:.2f})")
    return library


def _dominant_style(graph):
    """Area-weighted style across the rooms that have one.

    Mirrors ``vision.appearance.dominant_style`` but reads the persisted graph
    rather than the in-flight room records, so it works on a reloaded scene.
    """
    scores = {}
    for room in graph.rooms:
        if room.style and room.style != "unknown":
            weight = max(room.area, 1.0) * max(room.style_confidence, 0.2)
            scores[room.style] = scores.get(room.style, 0.0) + weight
    if not scores:
        return "unknown", 0.0
    best = max(sorted(scores), key=lambda key: scores[key])
    return best, min(1.0, scores[best] / sum(scores.values()))


def palette_for(graph, room_id):
    """The colour palette of a room, or the largest room's as a fallback.

    Objects that never got a room assignment still deserve a coherent scheme,
    and the primary room's palette is the best available answer.
    """
    room = graph.room_by_id(room_id) if room_id else None
    if room is not None and room.palette is not None:
        return room.palette
    for candidate in sorted(graph.rooms, key=lambda r: -r.area):
        if candidate.palette is not None:
            return candidate.palette
    return None


def resolve_materials_from_graph(graph, library):
    """Wall / floor / ceiling materials from the observed finishes.

    Per-room finishes take precedence; the graph-level ones are the fallback
    for consumers that predate per-room appearance.
    """
    primary = max(graph.rooms, key=lambda r: r.area) if graph.rooms else None
    room_palette = primary.palette if primary is not None else None

    wall_finish = (primary.wall_finish if primary else None) or (
        graph.walls[0].finish if graph.walls else None
    )
    floor_finish = (primary.floor_finish if primary else None) or graph.floor
    ceiling_finish = (primary.ceiling_finish if primary else None) or graph.ceiling

    return (
        library.surface(wall_finish, "wall", room_palette, "WallMaterial"),
        library.surface(floor_finish, "floor", room_palette, "FloorMaterial"),
        library.surface(ceiling_finish, "ceiling", room_palette, "CeilingMaterial"),
    )


# ---------------------------------------------------------------------------
# Scene Graph → Furniture
# ---------------------------------------------------------------------------

def build_furniture(graph, library, include_uncertain=False):
    """Instantiate every buildable object from the scene graph.

    Objects flagged uncertain are skipped by default: the brief is explicit
    that an omission is preferable to a guess.
    """
    if not VISION_AVAILABLE:
        return []

    buildable = graph.buildable_objects(include_uncertain=include_uncertain)
    skipped = len(graph.objects) - len(buildable)

    built = []
    for scene_object in buildable:
        variant = vision_assets.get_variant(scene_object.asset)
        builder_name = variant.builder if variant else "build_box_furniture"
        params = dict(variant.params) if variant else {}

        # Materials are resolved per object against its own room's palette, so
        # a bedroom's scheme does not leak into the living room.
        slots = library.for_object(scene_object, palette_for(graph, scene_object.room_id))
        obj = blender_furniture.build_object(scene_object, slots, builder_name, params)
        if obj is not None:
            built.append(obj)

    print(f"[FURNITURE] Built {len(built)} objects"
          + (f", skipped {skipped} uncertain" if skipped else ""))

    # Report what was withheld so the omission is visible, not silent.
    if skipped:
        withheld = sorted({o.category for o in graph.objects if o not in buildable})
        print(f"[FURNITURE] Withheld (low confidence): {', '.join(withheld)}")

    return built


def build_openings(graph, library):
    """Cut doors and windows out of the wall mesh with boolean modifiers."""
    if not graph.openings:
        return 0

    walls = bpy.data.objects.get("Walls")
    if walls is None:
        return 0

    cut = 0
    for opening in graph.openings:
        if opening.uncertain:
            continue

        wall = graph.wall_by_id(opening.wall_id)
        rotation = wall.angle_deg if wall else 0.0

        bpy.ops.mesh.primitive_cube_add(size=1, location=(
            opening.position.x,
            opening.position.y,
            opening.sill_height + opening.height / 2.0,
        ))
        cutter = bpy.context.active_object
        cutter.name = f"Cutter_{opening.id}"
        # Over-deep on the wall normal so the boolean fully penetrates.
        cutter.scale = (opening.width, 1.5, opening.height)
        cutter.rotation_euler = (0.0, 0.0, math.radians(rotation))

        modifier = walls.modifiers.new(name=f"Opening_{opening.id}", type='BOOLEAN')
        modifier.operation = 'DIFFERENCE'
        modifier.object = cutter

        bpy.context.view_layer.objects.active = walls
        try:
            bpy.ops.object.modifier_apply(modifier=modifier.name)
            cut += 1
        except RuntimeError as exc:
            print(f"[OPENINGS] ! {opening.id} boolean failed: {exc}")
            walls.modifiers.remove(modifier)

        bpy.data.objects.remove(cutter, do_unlink=True)

    print(f"[OPENINGS] Cut {cut}/{len(graph.openings)} openings into the walls")
    return cut


def build_architecture(graph, library):
    """Build columns, beams, partitions and similar structural elements."""
    if not VISION_AVAILABLE or not graph.architecture:
        return 0

    built = 0
    for element in graph.architecture:
        if element.uncertain:
            continue
        part = blender_furniture.Part()
        part.box_base(
            (0, 0),
            (element.dimensions.width, element.dimensions.depth, element.dimensions.height),
            0.0,
        )
        decision = library.resolve(element.finish.material, "object")
        material = library.get(element.finish.color_hex, decision.material)
        obj = part.to_object(f"arch_{element.id}", [material])
        obj.location = (element.position.x, element.position.y, element.position.z)
        obj.rotation_euler = (0.0, 0.0, math.radians(element.rotation_z))
        built += 1

    if built:
        print(f"[ARCHITECTURE] Built {built} structural elements")
    return built


# ---------------------------------------------------------------------------
# Scene Graph → Lighting
# ---------------------------------------------------------------------------

def setup_lighting_from_graph(graph, center_x, center_y, max_dim, config):
    """Build the lighting rig from the graph's environment and luminaires.

    Returns True when the graph supplied something usable; the caller falls
    back to the generic rig when it did not.
    """
    style, _ = _dominant_style(graph)
    report = bl_lighting.build(graph, style=style, config=config)

    for note in report.notes:
        print(f"[LIGHTING] {note}")
    print(f"[LIGHTING] {report.summary()}")

    # A rig with no fixtures *and* no sun lights nothing; anything else is a
    # legitimate scene (a daylit room needs no lamps).
    return report.fixtures > 0 or report.sun


# ---------------------------------------------------------------------------
# Evaluation Previews
# ---------------------------------------------------------------------------

def render_previews(graph, config):
    """Render one deterministic preview per stored viewpoint.

    The whole pipeline lives in ``modules/render``; this is only the hook that
    starts it. It runs in-process because the scene is already built here —
    reloading the .blend we just exported would double the cost for nothing.

    Skipped when ARCHX3D_SKIP_PREVIEW=1, and a no-op when the graph carries no
    viewpoints (no reference photographs were supplied).
    """
    if os.environ.get("ARCHX3D_SKIP_PREVIEW") == "1":
        print("[PREVIEW] Skipped (ARCHX3D_SKIP_PREVIEW=1)")
        return None
    if graph is None or not getattr(graph, "viewpoints", None):
        return None

    try:
        from render import preview as render_preview
    except ImportError as exc:
        print(f"[PREVIEW] Render pipeline unavailable ({exc})")
        return None

    try:
        report = render_preview.render_after_generation(graph, config, base_dir=BASE_DIR)
    except Exception as exc:  # A diagnostic pass must never fail the build.
        print(f"[PREVIEW] Preview pass failed: {exc}")
        return None

    for note in report.notes:
        print(f"[PREVIEW] {note}")
    print(f"[PREVIEW] {report.summary()}")
    print(f"[PREVIEW] Manifest: {report.manifest_path}")
    return report


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
    graph = load_scene_graph()
    styling = load_styling() if graph is None else None

    include_uncertain = config.get("include_uncertain_objects", False)
    library = library_for(graph) if (graph is not None and VISION_AVAILABLE) else None

    # Build materials — the scene graph's observed finishes take precedence
    # over the legacy text styling, which takes precedence over defaults.
    if graph is not None and library is not None:
        wall_mat, floor_mat, ceiling_mat = resolve_materials_from_graph(graph, library)
    else:
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

    # Furniture, openings and structure from the vision scene graph
    if graph is not None and library is not None:
        build_openings(graph, library)
        build_architecture(graph, library)
        build_furniture(graph, library, include_uncertain=include_uncertain)
        for line in library.log:
            print(f"[MATERIALS] {line}")
        print(f"[MATERIALS] {library.summary()}")

    # Lighting — prefer the luminaires actually observed in the reference images
    used_graph_lighting = False
    if graph is not None:
        used_graph_lighting = setup_lighting_from_graph(graph, cx, cy, max_dim, config)
    if not used_graph_lighting:
        print("[LIGHTING] Falling back to the generic 3-point rig")
        setup_lighting(cx, cy, max_dim, config)

    setup_render(config)
    setup_camera_and_animation(cx, cy, max_dim, config, graph)

    # Export
    export_scene(config)

    # Evaluation previews — one deterministic low-resolution image per stored
    # viewpoint, for `vision.similarity` to score. Rendered here, in the
    # process that just built the scene, rather than by reloading the .blend.
    render_previews(graph, config)

    # Render frames for video. `main.py --skip-render` sets this so the
    # expensive frame loop is skipped when only the GLB is wanted.
    if os.environ.get("ARCHX3D_SKIP_RENDER") == "1":
        print("[RENDER] Frame rendering skipped (ARCHX3D_SKIP_RENDER=1)")
    else:
        render_frames()

    print("=" * 60)
    print("  ArchX3D — Generation Complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()