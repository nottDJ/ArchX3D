import bpy
import math
from mathutils import Vector
import subprocess

# ==================================================
# RESET SCENE
# ==================================================
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ==================================================
# IMPORT MODEL
# ==================================================
model_path = "D:/program/KADAI/model.glb"
bpy.ops.import_scene.gltf(filepath=model_path)

# ==================================================
# AUTO SCALE MODEL (SAFE)
# ==================================================
mesh_objs = [o for o in scene.objects if o.type == "MESH"]

if mesh_objs:
    min_v = Vector((1e9, 1e9, 1e9))
    max_v = Vector((-1e9, -1e9, -1e9))

    for o in mesh_objs:
        for c in o.bound_box:
            wc = o.matrix_world @ Vector(c)
            min_v = Vector((min(min_v.x, wc.x), min(min_v.y, wc.y), min(min_v.z, wc.z)))
            max_v = Vector((max(max_v.x, wc.x), max(max_v.y, wc.y), max(max_v.z, wc.z)))

    size = (max_v - min_v).length
    TARGET = 20.0

    if size > 0:
        s = TARGET / size
        for o in mesh_objs:
            o.scale *= s
            o.location = (0, 0, 0)

# ==================================================
# LIGHTING (FAST)
# ==================================================
bpy.ops.object.light_add(type='SUN', location=(20, 20, 40))
sun = bpy.context.object
sun.data.energy = 3
sun.data.use_shadow = False  # HUGE speed win

# World
world = bpy.data.worlds.new("World")
scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[1].default_value = 1.2

# ==================================================
# CAMERA
# ==================================================
bpy.ops.object.camera_add()
camera = bpy.context.object
camera.data.lens = 20
scene.camera = camera

# ==================================================
# SIMPLE ROOM CENTERS (FAST + GENERIC)
# ==================================================
HUMAN_HEIGHT = 1.7
centers = []

for o in mesh_objs:
    bb = [o.matrix_world @ Vector(c) for c in o.bound_box]
    center = sum(bb, Vector()) / 8
    centers.append(center)

if len(centers) < 2:
    raise Exception("Not enough geometry for walkthrough")

# Order by nearest neighbor
path_points = [centers.pop(0)]
while centers:
    last = path_points[-1]
    nxt = min(centers, key=lambda p: (p - last).length)
    path_points.append(nxt)
    centers.remove(nxt)

path_points = [Vector((p.x, p.y, p.z + HUMAN_HEIGHT)) for p in path_points]

# ==================================================
# WALKTHROUGH PATH
# ==================================================
bpy.ops.curve.primitive_bezier_curve_add(location=path_points[0])
path = bpy.context.object
path.data.dimensions = '3D'
spline = path.data.splines[0]

spline.bezier_points[0].co = path_points[0]
for p in path_points[1:]:
    spline.bezier_points.add(1)
    spline.bezier_points[-1].co = p

for bp in spline.bezier_points:
    bp.handle_left_type = 'AUTO'
    bp.handle_right_type = 'AUTO'

# ==================================================
# CAMERA CONSTRAINTS
# ==================================================
follow = camera.constraints.new(type='FOLLOW_PATH')
follow.target = path
follow.use_curve_follow = True

track = camera.constraints.new(type='TRACK_TO')
track.target = path
track.track_axis = 'TRACK_NEGATIVE_Z'
track.up_axis = 'UP_Y'

# ==================================================
# ANIMATION
# ==================================================
scene.frame_start = 1
scene.frame_end = len(path_points) * 40

path.data.use_path = True
path.data.path_duration = scene.frame_end

path.data.eval_time = 0
path.data.keyframe_insert("eval_time", frame=1)
path.data.eval_time = path.data.path_duration
path.data.keyframe_insert("eval_time", frame=scene.frame_end)

# ==================================================
# ⚡ FAST RENDER SETTINGS (BLENDER 5.0 SAFE)
# ==================================================
scene.render.engine = "BLENDER_EEVEE"
scene.render.resolution_x = 960
scene.render.resolution_y = 540
scene.render.resolution_percentage = 100
scene.render.fps = 24
scene.render.use_persistent_data = True

scene.render.filepath = "D:/program/KADAI/output/walkthrough_"
scene.render.image_settings.file_format = 'PNG'

print("RENDER ENGINE:", scene.render.engine)

# ==================================================
# RENDER FRAMES
# ==================================================
bpy.ops.render.render(animation=True)

# ==================================================
# MP4 EXPORT (SEQUENCE MODE – WORKS)
# ==================================================
FFMPEG = r"C:\ffmpeg\ffmpeg-8.0.1-essentials_build\bin\ffmpeg.exe"

subprocess.run([
    FFMPEG,
    "-y",
    "-framerate", "24",
    "-start_number", "1",
    "-i", "D:/program/KADAI/output/walkthrough_%04d.png",
    "-c:v", "libx264",
    "-pix_fmt", "yuv420p",
    "D:/program/KADAI/output/walkthrough.mp4"
], check=True)

print("✅ WALKTHROUGH VIDEO GENERATED")
