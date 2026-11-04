"""Phase 0 spike 02 - Eevee Next render budget on a dressed scene.

Replaces spike 01's three-primitive floor values with numbers from real
geometry, and measures the master-intermediate byte cost that FR-26's disk
ceiling depends on.

Run under Blender:
  blender --background --factory-startup --python render_probe.py -- <scene> <out_dir>

Reports, as PROBE lines parsed by the driver:
  which GPU Eevee actually bound to (hybrid-graphics check, 8.3)
  scene complexity, and whether the asset brought its own lights
  seconds per frame at grey-box and master spec
  a projection for a 15 s shot at 60 fps
"""

import os
import sys
import time

import bpy
import json
import math
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
# Absolute, forward-slash paths: Blender did not resolve a mixed-separator
# relative render.filepath, and silently wrote nothing while still timing
# the render. FR-25 wants explicit contained paths anyway.
SCENE_PATH = os.path.abspath(argv[0]).replace("\\", "/")
OUT_DIR = os.path.abspath(argv[1]).replace("\\", "/")

MASTER_W, MASTER_H, MASTER_FPS = 1280, 960, 60
GREYBOX_W, GREYBOX_H = 640, 480
TIMING_FRAMES = int(argv[2]) if len(argv) > 2 else 12
SHOT_SECONDS = 15
PROJECTION_BUDGET_S = 600

os.makedirs(OUT_DIR, exist_ok=True)
report = {"scene": os.path.basename(SCENE_PATH)}


def log(key, value):
    report[key] = value
    print("PROBE %s=%s" % (key, value))


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
ext = os.path.splitext(SCENE_PATH)[1].lower()
t0 = time.time()
if ext == ".blend":
    bpy.ops.wm.open_mainfile(filepath=SCENE_PATH)
elif ext in (".gltf", ".glb"):
    bpy.ops.import_scene.gltf(filepath=SCENE_PATH)
else:
    raise SystemExit("unsupported scene format: %s" % ext)
log("load_seconds", round(time.time() - t0, 2))

scene = bpy.context.scene
meshes = [o for o in scene.objects if o.type == "MESH"]
lights = [o for o in scene.objects if o.type == "LIGHT"]
tris = 0
for o in meshes:
    try:
        tris += sum(max(len(p.vertices) - 2, 0) for p in o.data.polygons)
    except Exception:
        pass
log("mesh_objects", len(meshes))
log("triangles", tris)
log("materials", len(bpy.data.materials))
log("images", len(bpy.data.images))
log("asset_lights", len(lights))

if not meshes:
    raise SystemExit("PROBE FATAL no mesh objects imported")

# --------------------------------------------------------------------------
# camera: high corner of the scene bounding box, looking at the centre.
# This is the same input FR-29 gives the agent - location geometry plus a
# written framing intent - so the placement logic is deliberately crude.
# --------------------------------------------------------------------------
lo = Vector((1e18, 1e18, 1e18))
hi = Vector((-1e18, -1e18, -1e18))
for o in meshes:
    for corner in o.bound_box:
        w = o.matrix_world @ Vector(corner)
        lo = Vector((min(lo[i], w[i]) for i in range(3)))
        hi = Vector((max(hi[i], w[i]) for i in range(3)))
size = hi - lo
centre = (hi + lo) / 2.0
log("bbox_size", [round(v, 2) for v in size])

cam_data = bpy.data.cameras.new("CCTV")
cam_data.lens = 12.0  # wide, as a cheap fixed camera would be
cam = bpy.data.objects.new("CCTV", cam_data)
# high corner, just inside the volume, angled down at the centre
cam.location = Vector((lo.x + size.x * 0.06,
                       lo.y + size.y * 0.06,
                       lo.z + size.z * 0.92))
direction = (centre - cam.location).normalized()
cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
scene.collection.objects.link(cam)
scene.camera = cam

# practical light only if the asset brought none, so we measure the asset's
# own lighting cost where it has it
if not lights:
    ld = bpy.data.lights.new("Practical", type="AREA")
    ld.energy = max(size.x, size.y) * 120.0
    ld.size = max(size.x, size.y) * 0.25
    lo_obj = bpy.data.objects.new("Practical", ld)
    lo_obj.location = Vector((centre.x, centre.y, hi.z * 0.95))
    scene.collection.objects.link(lo_obj)
    log("added_practical_light", True)
else:
    log("added_practical_light", False)

world = bpy.data.worlds.new("W") if not scene.world else scene.world
scene.world = world
world.use_nodes = True
try:
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.02, 0.02, 0.025, 1.0)
    bg.inputs[1].default_value = 0.35
except Exception:
    pass

# --------------------------------------------------------------------------
# render settings: 8.3's master intermediate spec
# --------------------------------------------------------------------------
# Engine id differs across Blender majors, and the `engine` enum omits
# externally registered engines, so assign and read back (8.3).
engine = None
for candidate in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
    try:
        scene.render.engine = candidate
        if scene.render.engine == candidate:
            engine = candidate
            break
    except Exception:
        continue
if engine is None:
    raise SystemExit("PROBE FATAL no Eevee engine could be selected")
log("engine", engine)
log("blender_version", bpy.app.version_string)
log("blender_build_hash", bpy.app.build_hash.decode() if isinstance(bpy.app.build_hash, bytes) else bpy.app.build_hash)
scene.render.fps = MASTER_FPS
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGB"
scene.render.film_transparent = False
scene.view_settings.view_transform = "Standard"  # neutral, no baked grade


def render_still(w, h, samples, path):
    scene.render.resolution_x, scene.render.resolution_y = w, h
    scene.eevee.taa_render_samples = samples
    scene.render.filepath = path
    t = time.time()
    bpy.ops.render.render(write_still=True)
    return time.time() - t


# warm the shader/kernel cache first; spike 01 measured ~74 s cold
warm = render_still(GREYBOX_W, GREYBOX_H, 4, OUT_DIR + "/warmup.png")
log("warmup_seconds", round(warm, 2))

try:
    import gpu
    log("gpu_renderer", gpu.platform.renderer_get())
    log("gpu_backend", gpu.platform.backend_type_get())
except Exception as exc:
    log("gpu_renderer", "unavailable (%s)" % exc)

greybox = render_still(GREYBOX_W, GREYBOX_H, 8, OUT_DIR + "/greybox.png")
log("greybox_seconds", round(greybox, 3))

master_one = render_still(MASTER_W, MASTER_H, 32, OUT_DIR + "/master_still.png")
log("master_still_seconds", round(master_one, 3))

# --------------------------------------------------------------------------
# animation timing: a short run, then project
# --------------------------------------------------------------------------
scene.render.resolution_x, scene.render.resolution_y = MASTER_W, MASTER_H
scene.eevee.taa_render_samples = 32
scene.render.image_settings.file_format = "PNG"
frames_dir = OUT_DIR + "/frames"
os.makedirs(frames_dir, exist_ok=True)
scene.render.filepath = frames_dir + "/f_"
scene.frame_start = 1
scene.frame_end = TIMING_FRAMES
t = time.time()
bpy.ops.render.render(animation=True)
seq = time.time() - t
per_frame = seq / TIMING_FRAMES
log("timing_frames", TIMING_FRAMES)
log("sequence_seconds", round(seq, 2))
log("seconds_per_frame", round(per_frame, 3))

shot_frames = SHOT_SECONDS * MASTER_FPS
projection = per_frame * shot_frames
log("projected_15s_shot_seconds", round(projection, 1))
log("projected_15s_shot_minutes", round(projection / 60.0, 1))
log("within_budget", projection <= PROJECTION_BUDGET_S)

png_bytes = sum(os.path.getsize(os.path.join(frames_dir, f))
                for f in os.listdir(frames_dir) if f.endswith(".png"))
log("png_bytes_per_frame", int(png_bytes / TIMING_FRAMES))

with open(os.path.join(OUT_DIR, "render_report.json"), "w") as fh:
    json.dump(report, fh, indent=2, sort_keys=True)
print("PROBE DONE")
