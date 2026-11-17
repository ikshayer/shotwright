"""Blender-side dressing and CPU render for the found-footage diagnostic."""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
import time
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector


def _room_worker(path: str):
    spec = importlib.util.spec_from_file_location("shotwright_room_worker", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Blender room worker at {path}")
    worker = importlib.util.module_from_spec(spec)
    # Blender's embedded interpreter would otherwise cache this import next to the worker.
    sys.dont_write_bytecode = True
    spec.loader.exec_module(worker)
    return worker


def _cpu_only(_bpy, scene, requested):
    if requested != "CPU":
        raise RuntimeError(f"Expected CPU diagnostic, got {requested!r}")
    scene.cycles.device = "CPU"
    if scene.cycles.device != "CPU":
        raise RuntimeError("Blender did not select CPU Cycles")


def _material(name, tone, *, roughness=0.7, metal=0.0, sheen=0.0):
    surface = bpy.data.materials.new(name)
    surface.use_nodes = True
    shader = surface.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (*tone, 1.0)
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Metallic"].default_value = metal
    shader.inputs["Sheen Weight"].default_value = sheen
    return surface


def _box(name, center, dimensions, surface, *, bevel=0.0, angle=0.0):
    bpy.ops.mesh.primitive_cube_add(size=1, location=center)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.rotation_euler.z = angle
    obj.data.materials.append(surface)
    if bevel:
        edge = obj.modifiers.new("Worn edge", "BEVEL")
        edge.segments, edge.width = 4, bevel
        obj.modifiers.new("Weighted corner normals", "WEIGHTED_NORMAL")
    return obj


def _mesh(name, vertices, faces, surface):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(surface)
    for polygon in mesh.polygons:
        polygon.use_smooth = True
    return obj


def _line(name, points, surface, radius, *, closed=False):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 8
    curve.bevel_depth = radius
    curve.bevel_resolution = 2
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for coordinate, point in zip(points, spline.points, strict=True):
        point.co = (*coordinate, 1.0)
    spline.use_cyclic_u = closed
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(surface)
    return obj


def _seat_seams(surface):
    for column, x in enumerate((-1.77, -1.19, -0.61)):
        suffix = f" {column + 1:02d}" if column else ""
        cushion = bpy.data.objects[f"Sofa seat cushion{suffix}"]
        cushion.rotation_euler.z = (0.015, -0.021, -0.009)[column]
        for index, z in enumerate((0.388, 0.466)):
            points = []
            for item in range(64):
                angle = math.tau * item / 64
                c, s = math.cos(angle), math.sin(angle)
                px = 0.279 * math.copysign(abs(c) ** 0.27, c)
                py = 0.453 * math.copysign(abs(s) ** 0.27, s)
                local = Vector((px, py, z - 0.43))
                points.append(tuple(cushion.rotation_euler.to_matrix() @ local + Vector((x, 1.06, 0.43))))
            _line(f"Cushion {column + 1} welt {index + 1}", points, surface, 0.0027, closed=True)


def _throw(surface):
    # A thin blanket with unequal folds travels over the arm and sags toward the rug.
    rows, columns = 26, 21
    vertices = []
    for row in range(rows):
        v = row / (rows - 1)
        for column in range(columns):
            u = column / (columns - 1)
            x = -2.12 + v * 0.68
            y = 1.14 + (u - 0.5) * 0.50 + 0.08 * v
            arch = math.exp(-((v - 0.25) / 0.29) ** 4)
            z = 0.51 + 0.31 * arch + 0.03 * math.sin(u * 26 + v * 8) * (0.55 + v)
            vertices.append((x, y, z))
    faces = []
    for row in range(rows - 1):
        for column in range(columns - 1):
            a = row * columns + column
            faces.append((a, a + columns, a + columns + 1, a + 1))
    blanket = _mesh("Knitted throw on sofa arm", vertices, faces, surface)
    subdivision = blanket.modifiers.new("Soft cloth folds", "SUBSURF")
    subdivision.levels = subdivision.render_levels = 1
    thickness = blanket.modifiers.new("Cloth hem thickness", "SOLIDIFY")
    thickness.thickness = 0.004
    for side in (0, columns - 1):
        _line("Throw edge binding", [vertices[row * columns + side] for row in range(rows)], surface, 0.0035)


def _stoneware(surface):
    old = bpy.data.objects.get("Coffee mug")
    bpy.data.objects.remove(old, do_unlink=True)
    bpy.data.objects.remove(bpy.data.objects["Coffee mug handle"], do_unlink=True)
    # A continuous turned cross-section has an actual lip and inside wall.
    profile = ((0.002, 0.0), (0.039, 0.0), (0.048, 0.008), (0.052, 0.028),
               (0.054, 0.101), (0.052, 0.106), (0.047, 0.105), (0.045, 0.095),
               (0.043, 0.021), (0.036, 0.013), (0.002, 0.013))
    sides = 40
    x, y, z = 0.36, -0.11, 0.611
    vertices = []
    for radius, height in profile:
        for index in range(sides):
            angle = math.tau * index / sides
            vertices.append((x + radius * math.cos(angle), y + radius * math.sin(angle), z + height))
    faces = []
    for ring in range(len(profile) - 1):
        for index in range(sides):
            nxt = (index + 1) % sides
            faces.append((ring * sides + index, ring * sides + nxt, (ring + 1) * sides + nxt, (ring + 1) * sides + index))
    _mesh("Turned stoneware mug", vertices, faces, surface)
    points = []
    for index in range(25):
        angle = -math.pi / 2 + math.pi * index / 24
        points.append((x + 0.048 + 0.032 * math.cos(angle), y, z + 0.053 + 0.032 * math.sin(angle)))
    _line("Stoneware handle", points, surface, 0.0065)


def _surface_detail(material, stretches, *, strength, distance, dark_level):
    tree = material.node_tree
    shader = tree.nodes.get("Principled BSDF")
    coords = tree.nodes.new("ShaderNodeTexCoord")
    scale = tree.nodes.new("ShaderNodeVectorMath")
    scale.operation = "MULTIPLY"
    scale.inputs[1].default_value = stretches
    tree.links.new(coords.outputs["Generated"], scale.inputs[0])
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 1.0
    noise.inputs["Detail"].default_value = 2.5
    tree.links.new(scale.outputs["Vector"], noise.inputs["Vector"])
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.21
    ramp.color_ramp.elements[0].color = (dark_level, dark_level, dark_level, 1)
    ramp.color_ramp.elements[1].position = 0.81
    tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    base = shader.inputs["Base Color"]
    tone = tree.nodes.new("ShaderNodeMixRGB")
    tone.blend_type = "MULTIPLY"
    tone.inputs[0].default_value = 1.0
    if base.links:
        tree.links.new(base.links[0].from_socket, tone.inputs[1])
    else:
        tone.inputs[1].default_value = base.default_value
    tree.links.new(ramp.outputs["Color"], tone.inputs[2])
    tree.links.new(tone.outputs["Color"], base)
    normal = shader.inputs["Normal"]
    bump = tree.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = strength
    bump.inputs["Distance"].default_value = distance
    if normal.links:
        tree.links.new(normal.links[0].from_socket, bump.inputs["Normal"])
    tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], normal)


def _wall_print():
    width, height = 160, 104
    canvas = bpy.data.images.new("Original dusk landscape print", width=width, height=height, alpha=False)
    rng = random.Random(30)
    trees = sorted((rng.random(), rng.uniform(0.11, 0.57), rng.uniform(0.013, 0.054)) for _ in range(26))
    px = []
    for row in range(height):
        y = row / height
        for column in range(width):
            x = column / width
            ridge = 0.44 + 0.07 * math.sin(9.3 * x) + 0.063 * math.sin(24.5 * x + 1.7)
            grain = rng.uniform(-0.021, 0.021)
            tone = (0.48 + 0.22 * (1 - y)) if y > ridge else (0.18 + 0.22 * y)
            if any(0.16 < y < top and abs(x - center) < radius * (top - y) / (top - 0.16)
                   for center, top, radius in trees if top > 0.16):
                tone *= 0.43
            if 0.32 < x < 0.43 and 0.13 < y < 0.45 and y < 0.47 - abs(x - 0.375) * 1.7:
                tone *= 0.22
            tone += grain
            px.extend((tone, tone, tone, 1.0))
    canvas.pixels.foreach_set(px)
    canvas.pack()
    surface = _material("Matte landscape print", (0.25, 0.25, 0.25), roughness=0.91)
    tree = surface.node_tree
    coordinates = tree.nodes.new("ShaderNodeTexCoord")
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    tree.links.new(coordinates.outputs["Generated"], separate.inputs["Vector"])
    tree.links.new(separate.outputs["Y"], combine.inputs["X"])
    tree.links.new(separate.outputs["Z"], combine.inputs["Y"])
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = canvas
    tree.links.new(combine.outputs["Vector"], texture.inputs["Vector"])
    tree.links.new(texture.outputs["Color"], tree.nodes.get("Principled BSDF").inputs["Base Color"])
    bpy.data.objects["Framed picture"].data.materials[0] = surface


def _dress_room():
    thread = _material("Age-darkened upholstery binding", (0.095, 0.102, 0.090), sheen=0.4)
    fabric = bpy.data.materials["Pillow fabric"].copy()
    fabric.name = "Knitted blanket wool"
    ceramic = _material("Satin glazed stoneware", (0.37, 0.46, 0.44), roughness=0.27)
    paper = _material("Yellowed paper edges", (0.68, 0.60, 0.45), roughness=0.97)
    dark = _material("Faded ink and aged plastic", (0.045, 0.042, 0.039), roughness=0.56)
    satin = _material("Rubbed brass", (0.29, 0.22, 0.12), roughness=0.38, metal=0.55)
    _seat_seams(thread)
    _throw(fabric)
    _stoneware(ceramic)
    _wall_print()
    _surface_detail(bpy.data.materials["Honey oak"], (2, 47, 19), strength=0.28, distance=0.016, dark_level=0.53)
    _surface_detail(bpy.data.materials["Sofa fabric"], (27, 31, 29), strength=0.34, distance=0.013, dark_level=0.60)
    _surface_detail(fabric, (23, 30, 24), strength=0.42, distance=0.016, dark_level=0.56)
    for index, (x, y, size, angle) in enumerate(((0.08, -0.34, 0.28, -0.11), (0.04, -0.34, 0.23, 0.18))):
        z = 0.622 + index * 0.025
        cover = bpy.data.materials["Pillow fabric"] if index else dark
        _box(f"Paperback {index + 1} pages", (x, y, z), (size, 0.16, 0.016), paper, bevel=0.001, angle=angle)
        for j, face in enumerate((-1, 1)):
            _box(f"Paperback {index + 1} cover {j + 1}", (x, y, z + face * 0.0095), (size + 0.006, 0.166, 0.003), cover, bevel=0.0008, angle=angle)
    # Shelf wear and real scale cues break up the original line of identical books.
    for index, (x, height) in enumerate(((1.50, 0.24), (1.59, 0.20), (1.66, 0.25), (1.74, 0.18), (2.04, 0.28), (2.13, 0.23), (2.23, 0.29))):
        z = 0.94 + height / 2
        cover = (dark, thread, bpy.data.materials["Pillow fabric"])[index % 3]
        _box(f"Upper shelf book {index + 1}", (x, 1.69, z), (0.066, 0.17, height), cover, bevel=0.006, angle=0.02 * (index % 3 - 1))
        _box(f"Spine rule {index + 1}", (x, 1.602, z + height * 0.29), (0.043, 0.0018, 0.008), paper, bevel=0.0006)
    _box("Remote battery panel", (-0.42, -0.15, 0.649), (0.178, 0.061, 0.005), dark, bevel=0.002)
    _box("Hall switch plate", (0.146, 2.48, 1.11), (0.010, 0.073, 0.120), bpy.data.materials["White trim"], bevel=0.003)
    _box("Hall switch lever", (0.153, 2.48, 1.115), (0.016, 0.016, 0.026), satin, bevel=0.002)
    bpy.data.lights["Panel0"].energy = 58
    bpy.data.lights["Panel1"].energy = 13
    bpy.data.lights["Window daylight"].energy = 25
    ceiling_surface = bpy.data.materials["Lamp shade"].copy()
    ceiling_surface.name = "Aged overhead diffuser"
    ceiling_shader = ceiling_surface.node_tree.nodes.get("Principled BSDF")
    ceiling_shader.inputs["Emission Strength"].default_value = 0.13
    bpy.data.objects["Ceiling light diffuser"].data.materials[0] = ceiling_surface
    world = bpy.context.scene.world
    world.node_tree.nodes.get("Background").inputs["Strength"].default_value = 0.38
    hall = bpy.data.lights.new("Hall practical", "POINT")
    hall.shadow_soft_size = 0.115
    hall.color = (1.0, 0.79, 0.63)
    lamp = bpy.data.objects.new("Hall practical", hall)
    lamp.location = (0.67, 2.83, 2.16)
    bpy.context.collection.objects.link(lamp)
    for frame, energy in ((1, 19), (71, 19), (72, 0), (73, 13), (74, 0), (90, 0)):
        hall.energy = energy
        hall.keyframe_insert(data_path="energy", frame=frame)
    return hall


def _key_camera(camera, poses, worker):
    camera.rotation_mode = "QUATERNION"
    previous = None
    for pose in poses:
        camera.location = Vector(pose["location"])
        direction = Vector(pose["target"]) - camera.location
        attitude = direction.to_track_quat("-Z", "Y") @ Quaternion((0, 0, 1), pose["roll"])
        if previous is not None and previous.dot(attitude) < 0:
            attitude.negate()
        camera.rotation_quaternion = attitude
        previous = attitude.copy()
        camera.keyframe_insert(data_path="location", frame=pose["frame"])
        camera.keyframe_insert(data_path="rotation_quaternion", frame=pose["frame"])
    worker._linearize_action(camera)


def _establishing_camera(scene):
    lens = bpy.data.cameras.new("Fixed room camera")
    lens.lens = 15
    camera = bpy.data.objects.new("Fixed room camera", lens)
    camera.location = (-1.99, -1.73, 2.06)
    camera.rotation_euler = (Vector((0.25, 1.18, 1.03)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(camera)
    return camera


def main(plan_path: str) -> None:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    shot = plan["walkthrough"]
    worker = _room_worker(shot["worker"])
    # Reuse the production worker as a scenery builder. Zero source frames and an
    # explicitly CPU-only selector apply exclusively to this Blender process.
    worker._select_cycles_device = _cpu_only
    worker._run(plan_path)
    scene = bpy.context.scene
    hall = _dress_room()
    worker._constantize_action_path(hall, "energy")
    camera = scene.camera
    camera.name = "Operator handheld camera"
    camera.data.lens = 17.0
    camera.data.clip_start = 0.055
    _key_camera(camera, shot["camera"], worker)
    worker._configure_compositor(bpy, scene, 0.012, 0.0, monochrome=True)
    scene.view_settings.view_transform = "AgX"
    scene.cycles.device = "CPU"
    scene.cycles.use_denoising = True
    if hasattr(scene.cycles, "denoising_use_gpu"):
        scene.cycles.denoising_use_gpu = False
    scene.cycles.max_bounces = 6
    scene.cycles.diffuse_bounces = 3
    scene.cycles.glossy_bounces = 2
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 8
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.filepath = "//frames/frame_"
    scene.frame_end = len(shot["camera"])
    scene.frame_set(1)
    establishing = _establishing_camera(scene)
    bpy.ops.wm.save_as_mainfile(filepath=shot["blend"], compress=True)
    scene.camera = establishing
    scene.render.filepath = shot["establishing"]
    bpy.ops.render.render(write_still=True)
    scene.camera = camera
    timings = []
    for frame, destination in shot["frames"]:
        if scene.cycles.device != "CPU":
            raise RuntimeError("Cycles changed device before rendering")
        started = time.monotonic()
        scene.frame_set(frame)
        scene.render.filepath = destination
        bpy.ops.render.render(write_still=True)
        timings.append({"frame": frame, "seconds": round(time.monotonic() - started, 3)})
        print(f"WALKTHROUGH_FRAME {frame}: {timings[-1]['seconds']}s", flush=True)
    report = {
        "blender": bpy.app.version_string,
        "engine": scene.render.engine,
        "device": scene.cycles.device,
        "samples": scene.cycles.samples,
        "operator_first_pose": shot["camera"][0],
        "operator_last_pose": shot["camera"][-1],
        "path_metrics": shot["path_metrics"],
        "rendered_frames": timings,
        "source": "Repository procedural room with original locally authored props",
    }
    worker._write_json(shot["report"], report)


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1])
