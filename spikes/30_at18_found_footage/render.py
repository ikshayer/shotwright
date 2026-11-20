"""Blender-side dressing and CPU render for the found-footage diagnostic."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import random
import sys
import time
from pathlib import Path

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector


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


def _painted_texture(name, width, height, pixel):
    canvas = bpy.data.images.new(name, width=width, height=height, alpha=False)
    colors = []
    for row in range(height):
        for column in range(width):
            colors.extend((*pixel(column / width, row / height), 1.0))
    canvas.pixels.foreach_set(colors)
    canvas.pack()
    return canvas


def _apply_image(surface, canvas, vector):
    tree = surface.node_tree
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = canvas
    tree.links.new(vector, texture.inputs["Vector"])
    shader = tree.nodes.get("Principled BSDF")
    tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])


def _wallpaper():
    source = random.Random(681)

    def pixel(x, y):
        row = (y * 2) % 1
        column = (x + (0.5 if y > 0.5 else 0)) % 1
        dx, dy = column - 0.5, row - 0.48
        dist = math.hypot(dx, dy * 0.72)
        petals = 0.11 + 0.035 * math.cos(5 * math.atan2(dy, dx))
        motif = math.exp(-((dist - petals) / 0.033) ** 2) + 0.35 * math.exp(-(dist / 0.045) ** 2)
        stripe = 0.04 * math.exp(-((column - 0.07) / 0.023) ** 2)
        paper = 0.65 - 0.12 * motif - stripe + source.uniform(-0.025, 0.025)
        return paper, paper * 0.90, paper * 0.73

    surface = _material("Faded floral wallpaper", (0.53, 0.48, 0.32), roughness=0.93)
    canvas = _painted_texture("Original floral block print tile", 96, 128, pixel)
    tree = surface.node_tree
    geo = tree.nodes.new("ShaderNodeNewGeometry")
    position = tree.nodes.new("ShaderNodeSeparateXYZ")
    normal = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(geo.outputs["Position"], position.inputs["Vector"])
    tree.links.new(geo.outputs["Normal"], normal.inputs["Vector"])
    selector = tree.nodes.new("ShaderNodeMath")
    selector.operation = "ABSOLUTE"
    tree.links.new(normal.outputs["X"], selector.inputs[0])
    along_wall = tree.nodes.new("ShaderNodeMix")
    along_wall.data_type = "FLOAT"
    tree.links.new(selector.outputs[0], along_wall.inputs["Factor"])
    tree.links.new(position.outputs["X"], along_wall.inputs["A"])
    tree.links.new(position.outputs["Y"], along_wall.inputs["B"])
    uv = tree.nodes.new("ShaderNodeCombineXYZ")
    tree.links.new(along_wall.outputs["Result"], uv.inputs["X"])
    tree.links.new(position.outputs["Z"], uv.inputs["Y"])
    repeat = tree.nodes.new("ShaderNodeVectorMath")
    repeat.operation = "MULTIPLY"
    repeat.inputs[1].default_value = (3.6, 2.3, 1.0)
    tree.links.new(uv.outputs["Vector"], repeat.inputs[0])
    _apply_image(surface, canvas, repeat.outputs["Vector"])
    _surface_detail(surface, (70, 70, 84), strength=0.24, distance=0.004, dark_level=0.80)
    return surface


def _wall_carpet():
    source = random.Random(52)

    def pixel(x, y):
        border = min(x, y, 1 - x, 1 - y)
        band = 0.04 < border < 0.12
        medallion = abs(x - 0.5) * 1.6 + abs(y - 0.5)
        diamond = abs((x * 8) % 1 - 0.5) + abs((y * 8) % 1 - 0.5)
        if border < 0.018 or 0.13 < border < 0.15:
            palette = (0.20, 0.12, 0.10)
        elif band and diamond < 0.42 or 0.36 < medallion < 0.42:
            palette = (0.65, 0.53, 0.38)
        elif medallion < 0.34 and diamond < 0.25:
            palette = (0.59, 0.47, 0.35)
        else:
            palette = (0.41, 0.23, 0.22)
        jitter = source.uniform(-0.038, 0.038)
        return tuple(channel + jitter for channel in palette)

    surface = _material("Faded wool wall carpet", (0.41, 0.23, 0.22), roughness=0.96, sheen=0.3)
    canvas = _painted_texture("Original woven diamond carpet pattern", 172, 128, pixel)
    tree = surface.node_tree
    coordinates = tree.nodes.new("ShaderNodeTexCoord")
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    uv = tree.nodes.new("ShaderNodeCombineXYZ")
    tree.links.new(coordinates.outputs["Generated"], separate.inputs["Vector"])
    tree.links.new(separate.outputs["Y"], uv.inputs["X"])
    tree.links.new(separate.outputs["Z"], uv.inputs["Y"])
    _apply_image(surface, canvas, uv.outputs["Vector"])
    _surface_detail(surface, (45, 103, 95), strength=0.48, distance=0.008, dark_level=0.48)
    _box("Wall-hung woven carpet", (-2.55, 0.66, 1.45), (0.031, 1.68, 1.18), surface, bevel=0.008)
    for index in range(3):
        y = -0.08 + index * 0.74
        _line(f"Carpet suspension loop {index + 1}", [(-2.554, y, 2.02), (-2.551, y, 2.073)], surface, 0.006)


def _crt(veneer):
    for name in ("Television foot", "Television screen", "Television glass", "Remote battery panel"):
        bpy.data.objects.remove(bpy.data.objects[name], do_unlink=True)
    for obj in tuple(bpy.context.scene.objects):
        if obj.name.startswith(("Remote button", "Remote control")):
            bpy.data.objects.remove(obj, do_unlink=True)

    molded = _material("Nicked brown phenolic CRT bezel", (0.14, 0.076, 0.042), roughness=0.57)
    vents = _material("Black receiver vents", (0.027, 0.023, 0.021), roughness=0.69)
    glass = _material("Unlit convex phosphor glass", (0.037, 0.041, 0.037), roughness=0.24)
    boot = _material("Scuffed tuning dial and trim", (0.39, 0.35, 0.28), roughness=0.5, metal=0.38)
    _surface_detail(molded, (11, 37, 26), strength=0.34, distance=0.009, dark_level=0.73)
    _box("Box television deep cabinet", (2.329, 0.46, 0.87), (0.52, 0.94, 0.64), veneer, bevel=0.060)
    for index, (x, y) in enumerate(((2.17, 0.19), (2.17, 0.73), (2.49, 0.19), (2.49, 0.73))):
        _box(f"Resting CRT rubber foot {index + 1}", (x, y, 0.539),
             (0.060, 0.055, 0.025), vents, bevel=0.005)
    _box("Box television thick molded fascia", (2.049, 0.46, 0.87), (0.064, 0.92, 0.61), molded, bevel=0.028)
    _box("CRT deep screen surround", (2.008, 0.37, 0.877), (0.025, 0.688, 0.499), vents, bevel=0.011)

    # A convex superellipse models the rounded rectangular tube front.
    vertices = [(1.974, 0.37, 0.877)]
    faces = []
    slices, rings = 48, 9
    for ring in range(1, rings + 1):
        radius = ring / rings
        for segment in range(slices):
            turn = math.tau * segment / slices
            cos = math.cos(turn)
            sin = math.sin(turn)
            vertices.append((
                1.974 + 0.034 * radius ** 2,
                0.37 + 0.302 * radius * math.copysign(abs(cos) ** 0.55, cos),
                0.877 + 0.205 * radius * math.copysign(abs(sin) ** 0.55, sin),
            ))
    for segment in range(slices):
        faces.append((0, 1 + segment, 1 + (segment + 1) % slices))
    for ring in range(rings - 1):
        index = 1 + ring * slices
        for segment in range(slices):
            nxt = (segment + 1) % slices
            faces.append((index + segment, index + slices + segment, index + slices + nxt, index + nxt))
    _mesh("CRT curved glass tube", vertices, faces, glass)

    for index in range(7):
        _box(f"Television speaker louver {index + 1}", (2.015, 0.813, 0.738 + 0.020 * index),
             (0.013, 0.108, 0.007), vents, bevel=0.002)
    for index, z in enumerate((0.927, 1.039)):
        bpy.ops.mesh.primitive_cylinder_add(vertices=20, radius=0.033, depth=0.027,
                                           location=(2.006, 0.815, z), rotation=(0, math.pi / 2, 0))
        dial = bpy.context.object
        dial.name = f"Manual television tuning knob {index + 1}"
        dial.data.materials.append(boot)
        _line(f"Mechanical dial index {index + 1}", [(1.99, 0.815, z + 0.008), (1.99, 0.815, z + 0.025)], vents, 0.003)
    _line("Television power lead", [(2.53, 0.89, 0.71), (2.57, 1.04, 0.34), (2.57, 1.0, 0.09),
                                     (2.57, 0.58, 0.07), (2.57, 0.49, 0.16)], vents, 0.004)


def _apartment_dressing():
    paper = _wallpaper()
    walls = ("Rear wall", "Wall below window", "Wall above window", "Left wall", "Right wall", "Hallway header")
    for obj in bpy.context.scene.objects:
        if obj.name.startswith(walls):
            obj.data.materials[0] = paper
    _box("Rear return behind operator", (0, -2.076, 1.22), (5.4, 0.12, 2.44), paper)
    _box("Closed room entry door", (-1.42, -1.991, 0.97), (0.78, 0.053, 1.94),
         bpy.data.materials["Walnut"], bevel=0.015)
    trim = bpy.data.materials["White trim"]
    trim_shader = trim.node_tree.nodes.get("Principled BSDF")
    trim_shader.inputs["Base Color"].default_value = (0.58, 0.50, 0.36, 1)
    _surface_detail(trim, (16, 34, 21), strength=0.28, distance=0.009, dark_level=0.68)
    _wall_carpet()
    for name in ("Picture frame", "Framed picture"):
        print_obj = bpy.data.objects[name]
        print_obj.location = (1.92, 1.965 if name == "Picture frame" else 1.929, 2.20)
        print_obj.rotation_euler.z = -math.pi / 2
        print_obj.scale = tuple(v * 0.50 for v in print_obj.scale)

    veneer = bpy.data.materials["Walnut"].copy()
    veneer.name = "Worn dark varnished veneer"
    shader = veneer.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Roughness"].default_value = 0.38
    _surface_detail(veneer, (36, 4, 24), strength=0.27, distance=0.013, dark_level=0.53)
    handle = _material("Tarnished cabinet brass", (0.43, 0.32, 0.19), roughness=0.47, metal=0.45)
    for obj in bpy.context.scene.objects:
        if obj.name.startswith(("Bookcase", "TV stand", "TV console")):
            obj.data.materials[0] = veneer
    for index, x in enumerate((1.63, 2.17)):
        _box(f"Wall unit cabinet door {index + 1}", (x, 1.555, 0.24), (0.49, 0.035, 0.37), veneer, bevel=0.012)
        pull = x + (0.15 if index == 0 else -0.15)
        _line(f"Wall unit drop handle {index + 1}", [(pull, 1.533, 0.245), (pull, 1.504, 0.245),
                                                      (pull, 1.504, 0.318), (pull, 1.533, 0.318)], handle, 0.007)
    _crt(veneer)


def _tighten_room(scene, scale):
    transform = Matrix.Diagonal((*scale, 1.0))
    bpy.context.view_layer.update()
    for obj in scene.objects:
        if obj.type != "CAMERA":
            obj.matrix_world = transform @ obj.matrix_world
    bpy.context.view_layer.update()


def _world_bounds(objects):
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    if not corners:
        raise RuntimeError("No geometry from which to measure the sofa bounds")
    return tuple(Vector(tuple(reduce(point[axis] for point in corners) for axis in range(3)))
                 for reduce in (min, max))


def _upstream_images(socket):
    pending = [link.from_node for link in socket.links]
    seen, images = set(), set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        if node.type == "TEX_IMAGE" and node.image is not None:
            images.add(node.image)
        pending.extend(link.from_node for item in node.inputs for link in item.links)
    return images


def _sofa_textures(meshes):
    materials = {mat for obj in meshes for mat in obj.data.materials if mat is not None}
    images, base_colors = set(), set()
    for material in materials:
        if material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                images.add(node.image)
            if node.type == "BSDF_PRINCIPLED":
                base_colors.update(_upstream_images(node.inputs["Base Color"]))
    if not base_colors:
        raise RuntimeError("The sofa GLB has no image texture connected to a Principled Base Color input")
    for image in images:
        if not all(size > 0 for size in image.size):
            raise RuntimeError(f"Imported sofa texture could not be decoded: {image.name}")
        if image.packed_file is None:
            image.pack()
        if image.packed_file is None:
            raise RuntimeError(f"Imported sofa texture could not be packed: {image.name}")
    return sorted(material.name for material in materials), [
        {"name": image.name, "dimensions": list(image.size), "base_color": image in base_colors, "packed": True}
        for image in sorted(images, key=lambda item: item.name)
    ]


def _verify_sofa_file(path, expected_sha256):
    if not path.is_absolute() or path.suffix.lower() != ".glb":
        raise RuntimeError("The generated sofa requires an authorized absolute GLB path")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
            raise RuntimeError("Generated sofa GLB hash differs from the authorized plan")


def _replace_sofa(scene, worker, sofa, poses):
    prefixes = ("Sofa ", "Cushion ", "Knitted throw on sofa arm", "Throw edge binding")
    originals = [obj for obj in scene.objects if obj.name.startswith(prefixes)]
    baseline_min, baseline_max = _world_bounds(originals)
    floor = _world_bounds([obj for obj in scene.objects if obj.name.startswith("Floor plank")])[1].z
    path = Path(sofa["path"])
    _verify_sofa_file(path, sofa["sha256"])
    yaw = math.radians(sofa["yaw_deg"])
    if not math.isfinite(yaw):
        raise RuntimeError("Generated sofa yaw must be finite")

    root, imported = worker._import_gltf_asset(bpy, Matrix, Euler, Vector, scene, {
        "asset_id": "generated-sofa", "asset_path": str(path), "name": "Generated sofa",
        "location": (0, 0, 0), "dimensions": (1, 1, 1), "rotation": (0, 0, yaw), "fit_mode": "contain",
    })
    _verify_sofa_file(path, sofa["sha256"])
    if any(obj.type not in {"MESH", "EMPTY"} or obj.animation_data is not None for obj in imported):
        raise RuntimeError("The sofa GLB must contain static geometry and transforms only")
    meshes = [obj for obj in imported if obj.type == "MESH"]
    oriented_min, oriented_max = _world_bounds(meshes)
    target_dimensions = (baseline_max.x - baseline_min.x, baseline_max.y - baseline_min.y, baseline_max.z - floor)
    scale, *_ = worker.asset_fit_factors(oriented_min, oriented_max, target_dimensions, fit_mode="contain")
    root.scale *= scale
    bpy.context.view_layer.update()
    fitted_min, fitted_max = _world_bounds(meshes)
    center_offset = (baseline_min + baseline_max - fitted_min - fitted_max) * 0.5
    root.location += Vector((center_offset.x, center_offset.y, floor - fitted_min.z))
    bpy.context.view_layer.update()
    placed_min, placed_max = _world_bounds(meshes)
    tolerance = 0.0001
    if (abs(placed_min.z - floor) > tolerance or placed_max.z > baseline_max.z + tolerance
            or any(placed_min[axis] < baseline_min[axis] - tolerance
                   or placed_max[axis] > baseline_max[axis] + tolerance for axis in (0, 1))):
        raise RuntimeError("The fitted sofa exceeds the previous envelope or does not touch the floor")

    materials, textures = _sofa_textures(meshes)
    footprint_clearance = min(math.hypot(
        max(placed_min.x - pose["location"][0], pose["location"][0] - placed_max.x, 0),
        max(placed_min.y - pose["location"][1], pose["location"][1] - placed_max.y, 0),
    ) for pose in poses)
    if footprint_clearance <= tolerance:
        raise RuntimeError("Generated sofa intersects the handheld camera route")
    for obj in originals:
        bpy.data.objects.remove(obj, do_unlink=True)

    return {
        "sha256": sofa["sha256"], "yaw_deg": sofa["yaw_deg"], "floor_z_m": floor,
        "source_dimensions_blender_units": list(root["cctv_source_dimensions"]),
        "procedural_bounds_m": [list(baseline_min), list(baseline_max)],
        "placed_bounds_m": [list(placed_min), list(placed_max)],
        "placed_dimensions_m": list(placed_max - placed_min),
        "camera_clearance_m": round(footprint_clearance, 4),
        "removed_procedural_objects": len(originals), "meshes": len(meshes),
        "vertices": sum(len(obj.data.vertices) for obj in meshes),
        "faces": sum(len(obj.data.polygons) for obj in meshes),
        "materials": materials, "textures": textures,
    }


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
    bpy.data.lights["Panel1"].energy = 37
    bpy.data.lights["Window daylight"].energy = 17
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
    _apartment_dressing()
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
    lens.lens = 14.5
    camera = bpy.data.objects.new("Fixed room camera", lens)
    camera.location = (0.16, -1.56, 1.94)
    camera.rotation_euler = (Vector((0.02, 0.95, 1.03)) - camera.location).to_track_quat("-Z", "Y").to_euler()
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
    _tighten_room(scene, shot["room_scale"])
    sofa_report = _replace_sofa(scene, worker, shot["sofa_asset"], shot["camera"]) if "sofa_asset" in shot else None
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
        "room_scale": shot["room_scale"],
        "sofa_variant": "generated_glb" if sofa_report is not None else "procedural",
        "sofa_asset": sofa_report,
        "rendered_frames": timings,
        "source": "Compressed procedural set with original late Soviet apartment dressing",
    }
    worker._write_json(shot["report"], report)


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1])
