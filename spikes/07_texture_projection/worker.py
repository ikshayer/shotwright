"""Single-frame Blender worker for spike 07. Invoked only by run_spike.py."""
import sys
import time
import math
import bpy
from mathutils import Vector


def projected_material(image, name, projector, scene):
    """Homogeneous camera projector using exact camera frame bounds per pixel."""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    links = material.node_tree.links
    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.object = projector
    separate = nodes.new("ShaderNodeSeparateXYZ")
    links.new(texcoord.outputs["Object"], separate.inputs["Vector"])
    frame = projector.data.view_frame(scene=scene)
    x_min, x_max = min(v.x for v in frame), max(v.x for v in frame)
    y_min, y_max = min(v.y for v in frame), max(v.y for v in frame)
    plane_depth = -frame[0].z

    def math_node(operation, left, right):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        if isinstance(left, str):
            links.new(separate.outputs[left], node.inputs[0])
        else:
            node.inputs[0].default_value = left
        if isinstance(right, str):
            links.new(separate.outputs[right], node.inputs[1])
        else:
            node.inputs[1].default_value = right
        return node

    depth = math_node("MULTIPLY", "Z", -1.0)
    # x_screen = (x_camera / depth) * plane_depth; frame.bounds already encode
    # lens, sensor fit, aspect ratio and shift for the original projector.
    x_scaled = math_node("MULTIPLY", "X", plane_depth)
    x_plane = nodes.new("ShaderNodeMath"); x_plane.operation = "DIVIDE"
    links.new(x_scaled.outputs[0], x_plane.inputs[0]); links.new(depth.outputs[0], x_plane.inputs[1])
    y_scaled = math_node("MULTIPLY", "Y", plane_depth)
    y_plane = nodes.new("ShaderNodeMath"); y_plane.operation = "DIVIDE"
    links.new(y_scaled.outputs[0], y_plane.inputs[0]); links.new(depth.outputs[0], y_plane.inputs[1])

    def normalized(plane_node, lower, extent):
        subtract = nodes.new("ShaderNodeMath"); subtract.operation = "SUBTRACT"
        links.new(plane_node.outputs[0], subtract.inputs[0]); subtract.inputs[1].default_value = lower
        divide = nodes.new("ShaderNodeMath"); divide.operation = "DIVIDE"
        links.new(subtract.outputs[0], divide.inputs[0]); divide.inputs[1].default_value = extent
        return divide

    u = normalized(x_plane, x_min, x_max-x_min)
    v = normalized(y_plane, y_min, y_max-y_min)
    combine = nodes.new("ShaderNodeCombineXYZ")
    links.new(u.outputs[0], combine.inputs["X"])
    links.new(v.outputs[0], combine.inputs["Y"])
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.interpolation = "Linear"
    tex.extension = "CLIP"
    links.new(combine.outputs["Vector"], tex.inputs["Vector"])
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(tex.outputs["Color"], emission.inputs["Color"])
    links.new(tex.outputs["Alpha"], emission.inputs["Strength"])
    links.new(emission.outputs["Emission"], output.inputs["Surface"])
    material["spike07_projector_camera"] = projector.name
    material["spike07_camera_frame_bounds"] = [x_min, x_max, y_min, y_max, plane_depth]
    return material


def apply_projected_material(scene, material):
    mesh_objects = [obj for obj in scene.objects if obj.type == "MESH" and not obj.hide_render]
    for obj in mesh_objects:
        obj.data.materials.clear()
        obj.data.materials.append(material)
    return {"projected_mesh_objects": len(mesh_objects),
            "projector": "exact per-shading-point homogeneous camera-local projection",
            "mesh_topology_modified": False}


def add_second_camera(scene):
    camera_data = bpy.data.cameras.new("Spike07_SecondView")
    camera_data.lens = 11.0
    camera = bpy.data.objects.new("Spike07_SecondView", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (2.75, 1.75, 2.52)
    target = Vector((-0.3, -0.2, 1.15))
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
    return camera


def add_moving_proxy(scene):
    """A low-detail lit room-scale body for foreground occlusion evidence."""
    neutral = bpy.data.materials.new("Spike07_NeutralProxy")
    neutral.diffuse_color = (0.24, 0.25, 0.26, 1.0)
    neutral.use_nodes = True
    bsdf = neutral.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = neutral.diffuse_color
    bsdf.inputs["Roughness"].default_value = 0.88
    parts = []
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=0.22, location=(0, 0, 1.25))
    torso = bpy.context.object
    torso.name = "Spike07_Figure_Torso"
    torso.scale = (0.72, 0.42, 1.75)
    parts.append(torso)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=0.17, location=(0, 0, 1.7))
    head = bpy.context.object
    head.name = "Spike07_Figure_Head"
    head.scale = (0.9, 0.9, 1.05)
    parts.append(head)
    for side in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=0.065, depth=0.82,
                                            location=(side * 0.13, 0, 0.52))
        leg = bpy.context.object
        leg.name = "Spike07_Figure_Leg_" + str(side)
        parts.append(leg)
        bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=0.045, depth=0.72,
                                            location=(side * 0.24, 0, 1.2))
        arm = bpy.context.object
        arm.name = "Spike07_Figure_Arm_" + str(side)
        arm.rotation_euler[1] = side * 0.22
        parts.append(arm)
    root = bpy.data.objects.new("Spike07_MovingFigure", None)
    scene.collection.objects.link(root)
    for part in parts:
        part.data.materials.clear()
        part.data.materials.append(neutral)
        part.parent = root
    for frame, x in ((1, -2.0), (450, 0.0), (900, 2.0)):
        root.location = (x, 0.25, 0.0)
        root.keyframe_insert(data_path="location", frame=frame)
    return root


def render_one(blend_path, image_path, output_path, frame, mode, view, proxy):
    started = time.monotonic()
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    scene = bpy.context.scene
    filepaths = bpy.context.preferences.filepaths
    filepaths.use_auto_save_temporary_files = False
    if filepaths.use_auto_save_temporary_files:
        raise RuntimeError("Blender temporary autosaves could not be disabled")
    if hasattr(filepaths, "use_auto_save"):
        filepaths.use_auto_save = False
        if filepaths.use_auto_save:
            raise RuntimeError("Blender autosave could not be disabled")
    filepaths.save_version = 0
    if hasattr(filepaths, "file_preview_type"):
        filepaths.file_preview_type = "NONE"
        if filepaths.file_preview_type != "NONE":
            raise RuntimeError("Blender preview generation could not be disabled")
    scene.frame_set(int(frame))
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 960
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = False
    scene.render.filepath = output_path
    scene.render.use_file_extension = False
    scene.render.use_overwrite = True
    scene.render.use_stamp = False
    engine = "BLENDER_EEVEE"
    scene.render.engine = engine
    if scene.render.engine != engine:
        raise RuntimeError(f"Blender engine assignment mismatch: requested {engine}, read back {scene.render.engine}")
    if not hasattr(scene, "eevee"):
        raise RuntimeError("Blender EEVEE scene settings are unavailable")
    scene.eevee.taa_render_samples = 32
    import gpu
    gpu.init()
    actual_device = gpu.platform.renderer_get()
    expected_device = "NVIDIA GeForce RTX 5070 Ti Laptop GPU"
    if expected_device.lower() not in actual_device.lower():
        raise RuntimeError(f"Eevee GPU mismatch: requested {expected_device!r}, actual {actual_device!r}")
    # Keep the source scene's measured camera and transform for baseline. The
    # source phase-1 scene is Standard; projection explicitly sets Standard too.
    if mode == "projection":
        image = bpy.data.images.load(image_path, check_existing=False)
        if tuple(image.size) != (1280, 960):
            raise RuntimeError("generated still must be exactly 1280x960")
        image.colorspace_settings.name = "sRGB"
        projector = scene.camera
        material = projected_material(image, "Spike07_GeneratedProjection", projector, scene)
        uv_metrics = apply_projected_material(scene, material)
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
        if view == "second":
            scene.camera = add_second_camera(scene)
        if proxy == "figure":
            add_moving_proxy(scene)
            scene.frame_set(int(frame))
        scene["spike07_projector_name"] = projector.name
        scene["spike07_projection_uv_frozen"] = True
        scene["spike07_projected_mesh_objects"] = uv_metrics["projected_mesh_objects"]
    else:
        uv_metrics = {"projected_mesh_objects": 0, "projected_triangles": 0}
        if view != "main":
            raise RuntimeError("baseline view only accepts the source camera")
    render_started = time.monotonic()
    bpy.ops.render.render(write_still=True)
    print("SPIKE07_RENDER=" + str({"frame": frame, "mode": mode, "view": view,
          "wall_seconds_inside_blender": time.monotonic() - started,
          "wall_seconds_render_only": time.monotonic() - render_started,
          "moving_proxy": proxy == "figure", "engine": scene.render.engine,
          "actual_device": actual_device, "samples": scene.eevee.taa_render_samples,
          **uv_metrics}), flush=True)


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:]
    if not args or args[0] != "render" or len(args) != 8:
        raise SystemExit("worker args: render <blend> <generated.png> <output.png> <frame> <baseline|projection> <main|second> <reserved>")
    render_one(*args[1:])
