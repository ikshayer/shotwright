"""Phase 0 spike 02b - render budget on a scene that matches 4.1.

The available .blend/.glTF fixtures are game assets: low object counts, toon
or simple materials, no volumetrics, no raytraced reflections. They measured
0.59-0.71 s/frame and showed that cost is dominated by resolution x samples
rather than scene complexity (94x the objects cost +20% time).

So this builds the thing the PRD actually describes - an ordinary interior
with practical lighting, mundane clutter, mismatched colour temperature,
rough surfaces and haze - and itemises what each realism feature costs, so
the budget in 8.3 is built from the features we intend to use.

Run:
  blender --background --factory-startup --python synthetic_interior.py -- <out_dir> [master_frames]
"""

import os
import sys
import time

import bpy
import json
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
OUT_DIR = os.path.abspath(argv[0]).replace("\\", "/")
MASTER_FRAMES = int(argv[1]) if len(argv) > 1 else 0
# greybox: same geometry and camera, no materials, flat shading - the FR-29
# blocking preset, and the conditioning input for 9's geometry-conditioned path
GREYBOX = "greybox" in argv

MASTER_W, MASTER_H = 1280, 960
MASTER_FPS = 60
SAMPLES = 32
TIMING_FRAMES = 8
SHOT_SECONDS = 15

os.makedirs(OUT_DIR, exist_ok=True)
report = {}


def log(key, value):
    report[key] = value
    print("PROBE %s=%s" % (key, value))


def rough_material(name, base, roughness, scale, bump):
    """Muted PBR-ish surface with procedural roughness and bump variation."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = base
    bsdf.inputs["Roughness"].default_value = roughness
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = scale
    noise.inputs["Detail"].default_value = 6.0
    bump_n = nt.nodes.new("ShaderNodeBump")
    bump_n.inputs["Strength"].default_value = bump
    nt.links.new(noise.outputs["Fac"], bump_n.inputs["Height"])
    nt.links.new(bump_n.outputs["Normal"], bsdf.inputs["Normal"])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.35
    ramp.color_ramp.elements[1].position = 0.75
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "FLOAT"
    mix.inputs[0].default_value = 0.6
    mix.inputs[2].default_value = roughness
    mix.inputs[3].default_value = min(roughness + 0.3, 1.0)
    nt.links.new(ramp.outputs["Color"], mix.inputs[0])
    nt.links.new(mix.outputs[0], bsdf.inputs["Roughness"])
    return mat


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# --- room shell: 6 x 4 x 2.7 m corridor-ish room, normals inward ---
W, D, H = 6.0, 4.0, 2.7
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, H / 2))
room = bpy.context.active_object
room.scale = (W / 2, D / 2, H / 2)
bpy.ops.object.transform_apply(scale=True)
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.mesh.flip_normals()
bpy.ops.object.mode_set(mode="OBJECT")
room.data.materials.append(rough_material("Plaster", (0.52, 0.5, 0.47, 1), 0.78, 14.0, 0.09))

bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0.002))
floor = bpy.context.active_object
floor.scale = (W / 2, D / 2, 1)
bpy.ops.object.transform_apply(scale=True)
floor.data.materials.append(rough_material("Lino", (0.3, 0.29, 0.28, 1), 0.42, 28.0, 0.04))

# --- mundane clutter ---
# The camera position is fixed first so clutter can keep clear of the lens.
# The first version placed props at random and buried the camera inside one,
# producing a black frame - a small live demonstration of why FR-29 resolves
# framing through a compare loop instead of trusting a computed placement.
CAM_LOC = Vector((-W / 2 + 0.25, -D / 2 + 0.25, H - 0.18))
CAM_TARGET = Vector((0.3, 0.2, 1.15))
CLEAR_RADIUS = 1.6

clutter = rough_material("Clutter", (0.36, 0.33, 0.3, 1), 0.65, 22.0, 0.06)
import random
random.seed(1977)
placed = 0
attempts = 0
while placed < 22 and attempts < 400:
    attempts += 1
    i = placed
    kind = i % 3
    x = random.uniform(-W / 2 + 0.5, W / 2 - 0.5)
    y = random.uniform(-D / 2 + 0.5, D / 2 - 0.5)
    if (Vector((x, y, 0.5)) - CAM_LOC).length < CLEAR_RADIUS:
        continue
    placed += 1
    if kind == 0:
        bpy.ops.mesh.primitive_cube_add(size=random.uniform(0.25, 0.7), location=(x, y, 0.3))
    elif kind == 1:
        bpy.ops.mesh.primitive_cylinder_add(radius=random.uniform(0.1, 0.25),
                                            depth=random.uniform(0.4, 1.1),
                                            vertices=24, location=(x, y, 0.45))
    else:
        bpy.ops.mesh.primitive_cube_add(size=random.uniform(0.4, 1.0), location=(x, y, 0.5))
        bpy.context.active_object.rotation_euler = (0, 0, random.uniform(0, 3.14))
    bpy.context.active_object.data.materials.append(clutter)
log("clutter_objects", 22)

# --- practical lights with mismatched colour temperature (4.1) ---
for idx, (lx, warm) in enumerate(((-1.6, True), (1.7, False))):
    ld = bpy.data.lights.new("Panel%d" % idx, type="AREA")
    ld.shape = "RECTANGLE"
    ld.size, ld.size_y = 1.1, 0.25
    ld.energy = 120.0
    ld.color = (1.0, 0.86, 0.68) if warm else (0.80, 0.88, 1.0)
    obj = bpy.data.objects.new("Panel%d" % idx, ld)
    obj.location = (lx, 0.1, H - 0.08)
    scene.collection.objects.link(obj)

ld = bpy.data.lights.new("PanelFar", type="AREA")
ld.shape = "RECTANGLE"
ld.size, ld.size_y = 1.1, 0.25
ld.energy = 110.0
ld.color = (1.0, 0.9, 0.75)
obj = bpy.data.objects.new("PanelFar", ld)
obj.location = (0.2, D / 2 - 0.9, H - 0.08)
scene.collection.objects.link(obj)

scene.world = bpy.data.worlds.new("W")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.08, 0.082, 0.09, 1)
scene.world.node_tree.nodes["Background"].inputs[1].default_value = 1.6

# --- camera: high corner, wide, angled down - a cheap fixed camera ---
cam_data = bpy.data.cameras.new("CCTV")
cam_data.lens = 11.0
cam = bpy.data.objects.new("CCTV", cam_data)
cam.location = CAM_LOC
cam.rotation_euler = (CAM_TARGET - CAM_LOC).normalized().to_track_quat("-Z", "Y").to_euler()
scene.collection.objects.link(cam)
scene.camera = cam

mesh_objs = [o for o in scene.objects if o.type == "MESH"]
log("mesh_objects", len(mesh_objs))
log("triangles", sum(sum(max(len(p.vertices) - 2, 0) for p in o.data.polygons) for o in mesh_objs))
log("materials", len(bpy.data.materials))

if GREYBOX:
    # One flat, unshaded-ish grey on everything. Geometry, camera and lights
    # are untouched, so the result differs from the dressed render only in
    # surfacing - which is what makes it a fair conditioning input.
    flat = bpy.data.materials.new("GreyBox")
    flat.use_nodes = True
    bsdf = flat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.55, 0.55, 0.55, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.9
    bsdf.inputs["Specular IOR Level"].default_value = 0.1
    for o in [x for x in scene.objects if x.type == "MESH"]:
        o.data.materials.clear()
        o.data.materials.append(flat)
    log("greybox", True)

# --- engine ---
engine = None
for candidate in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
    try:
        scene.render.engine = candidate
        if scene.render.engine == candidate:
            engine = candidate
            break
    except Exception:
        continue
if engine is None:
    raise SystemExit("PROBE FATAL no Eevee engine")
log("engine", engine)
log("blender_version", bpy.app.version_string)

scene.render.fps = MASTER_FPS
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGB"
scene.view_settings.view_transform = "Standard"
scene.render.resolution_x, scene.render.resolution_y = MASTER_W, MASTER_H
scene.eevee.taa_render_samples = SAMPLES


def set_feature(path, value):
    """Set an eevee property if this Blender exposes it; report either way."""
    obj = scene.eevee
    try:
        setattr(obj, path, value)
        return getattr(obj, path) == value
    except Exception:
        return None


def timed_sequence(label, frames=TIMING_FRAMES):
    d = OUT_DIR + "/seq_" + label
    os.makedirs(d, exist_ok=True)
    scene.render.filepath = d + "/f_"
    scene.frame_start, scene.frame_end = 1, frames
    t = time.time()
    bpy.ops.render.render(animation=True)
    dt = (time.time() - t) / frames
    log("%s_seconds_per_frame" % label, round(dt, 3))
    log("%s_projected_15s_minutes" % label, round(dt * SHOT_SECONDS * MASTER_FPS / 60.0, 1))
    pngs = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".png")]
    if pngs:
        log("%s_png_bytes_per_frame" % label, int(sum(os.path.getsize(p) for p in pngs) / len(pngs)))
    return dt


# warm the cache before any measurement
scene.render.filepath = OUT_DIR + "/warmup.png"
t = time.time()
bpy.ops.render.render(write_still=True)
log("warmup_seconds", round(time.time() - t, 2))
try:
    import gpu
    log("gpu_renderer", gpu.platform.renderer_get())
except Exception as exc:
    log("gpu_renderer", "unavailable (%s)" % exc)

if GREYBOX:
    scene.render.resolution_x, scene.render.resolution_y = MASTER_W, MASTER_H
    scene.eevee.taa_render_samples = 16
    set_feature("use_raytracing", False)
    scene.render.filepath = OUT_DIR + "/greybox_1280x960.png"
    t = time.time()
    bpy.ops.render.render(write_still=True)
    log("greybox_seconds", round(time.time() - t, 2))
    with open(OUT_DIR + "/synthetic_report.json", "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print("PROBE DONE")
    raise SystemExit(0)

# A: flat - no raytracing, no volumetrics
log("applied_raytracing_off", set_feature("use_raytracing", False))
base = timed_sequence("a_flat")

# B: + raytraced reflections / shadows
log("applied_raytracing_on", set_feature("use_raytracing", True))
rt = timed_sequence("b_raytracing")

# C: + volumetric haze, the single most CCTV-looking realism feature
vol_mat = bpy.data.materials.new("Haze")
vol_mat.use_nodes = True
nt = vol_mat.node_tree
for n in list(nt.nodes):
    if n.type != "OUTPUT_MATERIAL":
        nt.nodes.remove(n)
scatter = nt.nodes.new("ShaderNodeVolumeScatter")
scatter.inputs["Density"].default_value = 0.035
nt.links.new(scatter.outputs[0], nt.nodes["Material Output"].inputs["Volume"])
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, H / 2))
haze = bpy.context.active_object
haze.scale = (W / 2 - 0.02, D / 2 - 0.02, H / 2 - 0.02)
bpy.ops.object.transform_apply(scale=True)
haze.data.materials.append(vol_mat)
vol = timed_sequence("c_volumetrics")

log("cost_of_raytracing_pct", round((rt / base - 1) * 100, 1))
log("cost_of_volumetrics_pct", round((vol / rt - 1) * 100, 1))
log("full_vs_flat_pct", round((vol / base - 1) * 100, 1))

# projections at the three plausible master frame rates
for fps in (60, 30, 24):
    log("full_15s_shot_minutes_at_%dfps_master" % fps,
        round(vol * SHOT_SECONDS * fps / 60.0, 1))

if MASTER_FRAMES:
    d = OUT_DIR + "/master"
    os.makedirs(d, exist_ok=True)
    scene.render.filepath = d + "/f_"
    scene.frame_start, scene.frame_end = 1, MASTER_FRAMES
    t = time.time()
    bpy.ops.render.render(animation=True)
    log("master_frames", MASTER_FRAMES)
    log("master_render_seconds", round(time.time() - t, 1))

with open(OUT_DIR + "/synthetic_report.json", "w") as fh:
    json.dump(report, fh, indent=2, sort_keys=True)
print("PROBE DONE")
