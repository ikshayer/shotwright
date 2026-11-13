"""Standalone Blender-side worker. Keep imports limited to Blender and stdlib."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time


def asset_fit_factors(source_min, source_max, desired, *, fit_mode="exact") -> tuple[float, float, float]:
    """Return exact per-axis factors or one centered, proportion-preserving contain factor."""
    axes = ("x", "y", "z")
    minimum = tuple(float(value) for value in source_min)
    maximum = tuple(float(value) for value in source_max)
    target = tuple(float(value) for value in desired)
    if len(minimum) != 3 or len(maximum) != 3 or len(target) != 3:
        raise ValueError("source bounds and desired dimensions must each have three axes")

    if fit_mode not in {"exact", "contain"}:
        raise ValueError(f"unsupported fit_mode {fit_mode!r}")
    if fit_mode == "contain":
        extents = tuple(high-low for low, high in zip(minimum, maximum))
        if any(not math.isfinite(e) or e <= 1e-9 for e in extents):
            raise ValueError("source bounds contain a degenerate axis")
        factor = min(dimension/extent for dimension, extent in zip(target, extents))
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("uniform contain factor is invalid")
        return (factor, factor, factor)
    factors = []
    for axis, low, high, dimension in zip(axes, minimum, maximum, target):
        extent = high - low
        if not math.isfinite(extent) or extent <= 1e-9:
            raise ValueError(
                f"source extent on {axis} axis is degenerate ({extent!r}); "
                "declared dimensions cannot be applied"
            )
        factor = dimension / extent
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError(f"scale factor on {axis} axis is invalid ({factor!r})")
        factors.append(factor)
    return tuple(factors)


# Adapted from Blender Codex MCP multi-map material and ARM channel setup (MIT;
# see THIRD_PARTY_NOTICES.md for the complete notice).
def _configure_environment(bpy, scene, spec: dict) -> None:
    """Apply a checksum-verified, packed HDRI to a new scene-owned World."""
    binding = spec.get("binding")
    path = binding.get("asset_path") if isinstance(binding, dict) else None
    expected = binding.get("asset_checksum") if isinstance(binding, dict) else None
    if not isinstance(path, str) or not os.path.isabs(path) or not isinstance(expected, str):
        raise RuntimeError("environment has no authorized absolute path/checksum binding")
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise RuntimeError(f"environment could not be read: {exc}") from exc
    if digest.hexdigest() != expected:
        raise RuntimeError("environment checksum changed after plan authorization")
    extension = os.path.splitext(path)[1].lower()
    if extension not in {".hdr", ".exr"}:
        raise RuntimeError(f"unsupported environment format {extension!r}")
    world = bpy.data.worlds.new(f"CCTV HDRI {spec['asset_id']}")
    world.use_nodes = True
    tree = world.node_tree
    tree.nodes.clear()
    coordinates = tree.nodes.new("ShaderNodeTexCoord")
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.inputs["Rotation"].default_value[2] = float(spec["rotation_rad"])
    environment = tree.nodes.new("ShaderNodeTexEnvironment")
    background = tree.nodes.new("ShaderNodeBackground")
    output = tree.nodes.new("ShaderNodeOutputWorld")
    try:
        image = bpy.data.images.load(path, check_existing=False)
        image.colorspace_settings.name = "Non-Color"
        image.pack()
        image.filepath = "//" + os.path.basename(path)
    except Exception as exc:
        raise RuntimeError(f"environment image could not be decoded, linearized, and packed: {exc}") from exc
    environment.image = image
    background.inputs["Strength"].default_value = float(spec["strength"])
    tree.links.new(coordinates.outputs["Normal"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], environment.inputs["Vector"])
    tree.links.new(environment.outputs["Color"], background.inputs["Color"])
    tree.links.new(background.outputs["Background"], output.inputs["Surface"])
    scene.world = world


def _configure_pbr_material(bpy, material, spec: dict) -> None:
    """Configure one material from serialized PBR maps and verified runtime paths."""
    name = spec.get("name", "<unnamed>")
    textures = spec.get("textures")

    def fail(role: str, message: str) -> None:
        raise RuntimeError(f"material {name!r}, role {role!r}: {message}")

    if not isinstance(textures, dict):
        fail("textures", "expected an object")
    maps, bindings = textures.get("maps"), textures.get("bindings")
    roles = ("base_color", "normal_gl", "roughness", "metallic", "arm")
    if not isinstance(maps, dict) or not isinstance(bindings, dict):
        fail("textures", "maps and bindings are required")
    if not maps or maps.get("base_color") is None or set(maps) - set(roles):
        fail("textures", "invalid map roles")
    if set(bindings) != set(maps):
        fail("textures", "bindings must match map roles")
    uv = textures.get("uv_scale", (1.0, 1.0))
    strength = textures.get("normal_strength", 1.0)
    ao_strength = textures.get("ao_strength", 0.0)
    if not isinstance(uv, (list, tuple)) or len(uv) != 2 or any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in uv
    ):
        fail("uv", "scale must be a finite positive pair")
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not math.isfinite(strength) or strength < 0:
        fail("normal_gl", "strength must be finite and nonnegative")
    if isinstance(ao_strength, bool) or not isinstance(ao_strength, (int, float)) or not math.isfinite(ao_strength) or not 0 <= ao_strength <= 1:
        fail("arm", "AO strength must be in [0, 1]")
    material.use_nodes = True
    if material.node_tree is None:
        fail("surface", "node tree unavailable after enabling nodes")
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()

    def node(kind: str, role: str):
        try:
            return nodes.new(type=kind)
        except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
            fail(role, f"node {kind} unavailable: {exc}")

    def sock(sockets, key: str, role: str):
        result = sockets.get(key)
        if result is None:
            fail(role, f"required socket {key!r} unavailable")
        return result

    def link(output, input_socket, role: str) -> None:
        try:
            links.new(output, input_socket)
        except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
            fail(role, f"cannot connect sockets: {exc}")

    texcoord = node("ShaderNodeTexCoord", "uv")
    mapping = node("ShaderNodeMapping", "uv")
    try:
        mapping.vector_type = "POINT"
        sock(mapping.inputs, "Scale", "uv").default_value = (float(uv[0]), float(uv[1]), 1.0)
    except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
        fail("uv", f"mapping scale unavailable: {exc}")
    link(sock(texcoord.outputs, "UV", "uv"), sock(mapping.inputs, "Vector", "uv"), "uv")

    images = {}
    for role in roles:
        if role not in maps:
            continue
        binding = bindings[role]
        if not isinstance(binding, dict) or binding.get("asset_id") != maps[role]:
            fail(role, "binding ID does not match maps")
        image_path, expected = binding.get("asset_path"), binding.get("asset_checksum")
        if not isinstance(image_path, str) or not os.path.isabs(image_path) or not os.path.isfile(image_path):
            fail(role, "authorized absolute image path missing")
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected):
            fail(role, "registered SHA256 invalid")
        digest = hashlib.sha256()
        try:
            with open(image_path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            fail(role, f"cannot read image: {exc}")
        if digest.hexdigest().lower() != expected.lower():
            fail(role, "image SHA256 differs from registered checksum")
        try:
            image = bpy.data.images.load(image_path, check_existing=False)
        except (RuntimeError, TypeError, ValueError, OSError) as exc:
            fail(role, f"image decode failed: {exc}")
        if len(image.size) < 2 or int(image.size[0]) <= 0 or int(image.size[1]) <= 0:
            fail(role, "decoded image has zero size")
        color_space = "sRGB" if role == "base_color" else "Non-Color"
        try:
            image.colorspace_settings.name = color_space
        except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
            fail(role, f"color space {color_space!r} unavailable: {exc}")
        if image.colorspace_settings.name != color_space:
            fail(role, f"color space read back as {image.colorspace_settings.name!r}")
        try:
            image.pack()
        except (RuntimeError, OSError, ValueError) as exc:
            fail(role, f"image packing failed: {exc}")
        if image.packed_file is None:
            fail(role, "image did not pack")
        tex = node("ShaderNodeTexImage", role)
        tex.image = image
        link(sock(mapping.outputs, "Vector", role), sock(tex.inputs, "Vector", role), role)
        sock(tex.outputs, "Color", role)
        images[role] = tex

    output = node("ShaderNodeOutputMaterial", "surface")
    principled = node("ShaderNodeBsdfPrincipled", "surface")
    link(sock(principled.outputs, "BSDF", "surface"), sock(output.inputs, "Surface", "surface"), "surface")
    if "roughness" not in images and "arm" not in images:
        sock(principled.inputs, "Roughness", "surface").default_value = spec["roughness"]
    if "metallic" not in images and "arm" not in images:
        sock(principled.inputs, "Metallic", "surface").default_value = spec.get("metallic", 0.0)
    emission_strength = spec.get("emission_strength", 0.0)
    if emission_strength:
        emission = principled.inputs.get("Emission Color") or principled.inputs.get("Emission")
        if emission is not None:
            emission.default_value = tuple(spec["color"])
        strength_socket = principled.inputs.get("Emission Strength")
        if strength_socket is not None:
            strength_socket.default_value = emission_strength
    base = sock(images["base_color"].outputs, "Color", "base_color")
    if "arm" in images:
        separate = node("ShaderNodeSeparateColor", "arm")
        try:
            separate.mode = "RGB"
        except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
            fail("arm", f"Separate Color RGB mode unavailable: {exc}")
        if separate.mode != "RGB":
            fail("arm", "Separate Color did not retain RGB mode")
        link(sock(images["arm"].outputs, "Color", "arm"), sock(separate.inputs, "Color", "arm"), "arm")
        if "roughness" not in images:
            link(sock(separate.outputs, "Green", "arm"), sock(principled.inputs, "Roughness", "arm"), "arm")
        if "metallic" not in images:
            link(sock(separate.outputs, "Blue", "arm"), sock(principled.inputs, "Metallic", "arm"), "arm")
        if ao_strength > 0:
            mix = node("ShaderNodeMixRGB", "arm")
            try:
                mix.blend_type = "MULTIPLY"
                mix.inputs[0].default_value = float(ao_strength)
                color_a, color_b, result = sock(mix.inputs, "Color1", "arm"), sock(mix.inputs, "Color2", "arm"), sock(mix.outputs, "Color", "arm")
            except (RuntimeError, TypeError, ValueError, AttributeError, KeyError) as exc:
                fail("arm", f"AO multiply sockets unavailable: {exc}")
            link(base, color_a, "arm")
            link(sock(separate.outputs, "Red", "arm"), color_b, "arm")
            base = result
    link(base, sock(principled.inputs, "Base Color", "base_color"), "base_color")
    if "normal_gl" in images:
        normal = node("ShaderNodeNormalMap", "normal_gl")
        try:
            normal.space = "TANGENT"
            sock(normal.inputs, "Strength", "normal_gl").default_value = float(strength)
            if hasattr(normal, "convention"):
                normal.convention = "OPENGL"
                if normal.convention != "OPENGL":
                    fail("normal_gl", "OpenGL convention was not retained")
            # Blender Normal Map defaults to OpenGL (positive-green) tangent convention.
        except (RuntimeError, TypeError, ValueError, AttributeError, KeyError) as exc:
            fail("normal_gl", f"tangent normal setup unavailable: {exc}")
        link(sock(images["normal_gl"].outputs, "Color", "normal_gl"), sock(normal.inputs, "Color", "normal_gl"), "normal_gl")
        link(sock(normal.outputs, "Normal", "normal_gl"), sock(principled.inputs, "Normal", "normal_gl"), "normal_gl")
    if "roughness" in images:
        link(sock(images["roughness"].outputs, "Color", "roughness"), sock(principled.inputs, "Roughness", "roughness"), "roughness")
    if "metallic" in images:
        link(sock(images["metallic"].outputs, "Color", "metallic"), sock(principled.inputs, "Metallic", "metallic"), "metallic")
    material.use_nodes = True


def _run(plan_path: str) -> None:
    with open(plan_path, encoding="utf-8") as stream:
        plan = json.load(stream)
    # Blender's OS temporary-file consumers inherit these values. The supervisor
    # creates and authorizes this directory before launching us.
    temp_dir = plan.get("runtime", {}).get("temp_dir")
    if not isinstance(temp_dir, str) or not os.path.isabs(temp_dir) or not os.path.isdir(temp_dir):
        raise RuntimeError(
            "plan runtime.temp_dir must be an existing authorized absolute directory"
        )
    for key in ("TMP", "TEMP", "TMPDIR"):
        os.environ[key] = temp_dir
    import bpy
    from mathutils import Euler, Matrix, Vector

    started = time.monotonic()
    scene_data = plan["scene"]
    output = plan["outputs"]
    camera_settings = plan["camera"]
    metadata = {
        "asset_bounds": [],
        "schema_version": 1,
        "shot_id": plan["shot_id"],
        "shot_version": plan["shot_version"],
        "run_id": plan["run_id"],
        "tool_version": bpy.app.version_string,
        "build_hash": getattr(bpy.app, "build_hash", b"").decode("utf-8", "replace")
        if isinstance(getattr(bpy.app, "build_hash", b""), bytes)
        else str(getattr(bpy.app, "build_hash", "")),
        "actual_device": "initializing",
        "applied_settings": {
            **camera_settings,
            "engine": plan["engine"],
            "width": plan["width"],
            "height": plan["height"],
            "fps": plan["fps"],
            "samples": plan["samples"],
        },
        "geometry_fingerprint": None,
        "child_pid": os.getpid(),
        "elapsed_seconds": None,
        "status": "running",
    }
    # Emit identity before any geometry work so failures still leave attributable metadata.
    _write_json(output["metadata"], metadata)
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
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
        # Blender 5.2 can write a scene preview to the per-user thumbnail cache
        # during save. Disable preview generation so the child has no user-profile
        # write path; the retained .blend remains fully editable without a preview.
        filepaths.file_preview_type = "NONE"
        if filepaths.file_preview_type != "NONE":
            raise RuntimeError("Blender scene preview generation could not be disabled")
        filepaths.use_file_compression = False
        if filepaths.use_file_compression:
            raise RuntimeError("Blender .blend compression could not be disabled")
        if (plan["mode"] == "preview") != (plan["engine"] == "workbench"):
            raise RuntimeError(
                "preview must use the explicit Workbench preset; masters use Eevee or Cycles"
            )
        engine_ids = {
            "eevee": ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"),
            "cycles": ("CYCLES",),
            "workbench": ("BLENDER_WORKBENCH",),
        }
        selected = None
        engine_rejections = []
        for candidate in engine_ids[plan["engine"]]:
            try:
                scene.render.engine = candidate
                if scene.render.engine == candidate:
                    selected = candidate
                    break
                engine_rejections.append(
                    f"{candidate}: assignment read back as {scene.render.engine!r}"
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                engine_rejections.append(f"{candidate}: {type(exc).__name__}: {exc}")
        if selected is None:
            raise RuntimeError(
                f"unsupported render engine {plan['engine']!r}; tried {engine_rejections}"
            )
        if plan["engine"] == "cycles":
            _select_cycles_device(bpy, scene, plan["gpu_device"])
            metadata["actual_device"] = plan["gpu_device"]
        else:
            import gpu

            # Background mode does not initialize Blender's drawing GPU context
            # automatically. Initialize it explicitly so renderer_get verifies
            # the real device instead of failing before device validation.
            gpu.init()
            renderer = gpu.platform.renderer_get()
            metadata["actual_device"] = renderer
            if (
                "nvidia" not in renderer.lower()
                and "geforce" not in renderer.lower()
                and "rtx" not in renderer.lower()
            ):
                raise RuntimeError("Eevee renderer is not the requested NVIDIA GPU: " + renderer)
            requested = (
                plan["gpu_device"].lower().replace("nvidia", "").replace("geforce", "").strip()
            )
            actual = renderer.lower().replace("nvidia", "").replace("geforce", "").strip()
            if requested not in actual and actual not in requested:
                raise RuntimeError(
                    f"Eevee renderer mismatch: requested {plan['gpu_device']!r}, actual {renderer!r}"
                )
        metadata["applied_settings"]["engine"] = selected

        materials = {}
        if plan["mode"] == "master":
            for spec in scene_data["materials"]:
                mat = bpy.data.materials.new(spec["name"])
                mat.diffuse_color = tuple(spec["color"])
                mat.use_nodes = True
                if spec.get("textures") is not None:
                    _configure_pbr_material(bpy, mat, spec)
                    shader = None
                else:
                    shader = mat.node_tree.nodes.get("Principled BSDF")
                if shader is not None:
                    shader.inputs["Base Color"].default_value = tuple(spec["color"])
                    shader.inputs["Roughness"].default_value = spec["roughness"]
                    shader.inputs["Metallic"].default_value = spec.get("metallic", 0.0)
                    if spec.get("emission_strength", 0.0):
                        emission = shader.inputs.get("Emission Color") or shader.inputs.get(
                            "Emission"
                        )
                        if emission is not None:
                            emission.default_value = tuple(spec["color"])
                        strength = shader.inputs.get("Emission Strength")
                        if strength is not None:
                            strength.default_value = spec["emission_strength"]
                    if spec.get("surface", "plain") != "plain":
                        _configure_material_surface(mat, spec)
                materials[spec["name"]] = mat
        for spec in scene_data["objects"]:
            kind = spec["kind"]
            if kind not in {"box", "cylinder", "sphere", "cone", "torus"}:
                raise ValueError(f"unsupported object kind {kind!r}")
            imported_objects = []
            if spec.get("asset_id") is not None:
                obj, imported_objects = _import_gltf_asset(
                    bpy, Matrix, Euler, Vector, scene, spec
                )
                meshes = [item for item in imported_objects if item.type == "MESH"]
                points = [item.matrix_world @ Vector(corner) for item in meshes for corner in item.bound_box]
                placed_min = tuple(min(point[a] for point in points) for a in range(3))
                placed_max = tuple(max(point[a] for point in points) for a in range(3))
                metadata["asset_bounds"].append({
                    "object_name": spec["name"], "asset_id": spec["asset_id"],
                    "fit_mode": spec.get("fit_mode", "exact"),
                    "source_min": list(obj.get("cctv_source_min", ())),
                    "source_max": list(obj.get("cctv_source_max", ())),
                    "source_dimensions": list(obj.get("cctv_source_dimensions", ())),
                    "placed_world_min": list(placed_min), "placed_world_max": list(placed_max),
                    "placed_dimensions": [placed_max[a]-placed_min[a] for a in range(3)],
                })
            else:
                if kind == "box":
                    bpy.ops.mesh.primitive_cube_add(size=1, location=spec["location"])
                elif kind == "cylinder":
                    bpy.ops.mesh.primitive_cylinder_add(
                        vertices=32, radius=0.5, depth=1, location=spec["location"]
                    )
                elif kind == "sphere":
                    bpy.ops.mesh.primitive_uv_sphere_add(
                        segments=32, ring_count=16, radius=0.5, location=spec["location"]
                    )
                elif kind == "cone":
                    bpy.ops.mesh.primitive_cone_add(
                        vertices=32, radius1=0.5, radius2=0.12, depth=1, location=spec["location"]
                    )
                else:
                    bpy.ops.mesh.primitive_torus_add(
                        major_segments=48, minor_segments=12, location=spec["location"]
                    )
                obj = bpy.context.object
                obj.dimensions = tuple(spec["dimensions"])
                bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
                obj.rotation_euler = tuple(spec.get("rotation", (0, 0, 0)))
                bevel = float(spec.get("bevel", 0))
                if bevel and kind == "box":
                    mod = obj.modifiers.new(name="Soft edges", type="BEVEL")
                    mod.width = min(bevel, min(spec["dimensions"]) * 0.48)
                    mod.segments = 3
                    obj.modifiers.new(name="Weighted normals", type="WEIGHTED_NORMAL")
                if kind == "sphere":
                    for poly in obj.data.polygons:
                        poly.use_smooth = True
            obj.name = spec["name"]
            if plan["mode"] == "master" and not imported_objects:
                obj.data.materials.append(materials[spec["material"]])
            if spec["name"] == "RoomShell" and not imported_objects:
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.mesh.flip_normals()
                bpy.ops.object.mode_set(mode="OBJECT")
            for key in spec["keyframes"]:
                obj.location = tuple(key["location"])
                obj.keyframe_insert(data_path="location", frame=key["frame"])
            visibility_targets = imported_objects or [obj]
            for key in spec.get("visibility_keyframes", []):
                for target in visibility_targets:
                    target.hide_render = not bool(key["visible"])
                    target.keyframe_insert(data_path="hide_render", frame=key["frame"])
            _linearize_action(obj)
            for target in visibility_targets:
                _constantize_action_path(target, "hide_render")
        for spec in scene_data["lights"]:
            light_kind = spec.get("kind", "area").upper()
            light = bpy.data.lights.new(spec["name"], type=light_kind)
            energy_scale = float(camera_settings["practical_light_intensity"])
            if camera_settings["monochrome_night"]:
                energy_scale *= 0.35
            expected_energy = spec["energy"] * energy_scale
            light.energy = expected_energy
            expected_color = (
                (1.0, 1.0, 1.0)
                if plan["mode"] == "preview"
                else tuple(
                    _temperature_color(spec["color"], camera_settings["practical_light_color_k"])
                )
            )
            if camera_settings["monochrome_night"]:
                value = sum(expected_color) / 3
                expected_color = (value, value, value)
                light.energy = expected_energy
            light.color = expected_color
            if any(
                not math.isclose(float(actual), expected, rel_tol=1e-6, abs_tol=1e-6)
                for actual, expected in zip(light.color, expected_color)
            ):
                raise RuntimeError(f"light {spec['name']!r} color failed readback")
            if light_kind == "AREA":
                light.shape = "RECTANGLE"
                light.size = spec["size"]
                light.size_y = spec["size"] * 0.23
            else:
                light.shadow_soft_size = spec["size"]
            actual_size = light.size if light_kind == "AREA" else light.shadow_soft_size
            if not math.isclose(
                light.energy, expected_energy, rel_tol=1e-6, abs_tol=1e-6
            ) or not math.isclose(actual_size, float(spec["size"]), rel_tol=1e-6, abs_tol=1e-6):
                raise RuntimeError(f"light {spec['name']!r} energy or size was clamped")
            for key in spec.get("energy_keyframes", []):
                light.energy = float(key["energy"]) * energy_scale
                light.keyframe_insert(data_path="energy", frame=key["frame"])
            _linearize_action(light)
            obj = bpy.data.objects.new(spec["name"], light)
            obj.location = spec["location"]
            if spec.get("target") is not None:
                target = Vector(spec["target"])
                if target == Vector(spec["location"]):
                    raise ValueError("light target coincides with location")
                obj.rotation_euler = (
                    (target - Vector(spec["location"]))
                    .normalized()
                    .to_track_quat("-Z", "Y")
                    .to_euler()
                )
            scene.collection.objects.link(obj)
        camera_data = bpy.data.cameras.new("CCTV Camera")
        camera = bpy.data.objects.new("CCTV Camera", camera_data)
        position = camera_settings["camera_position"] or tuple(scene_data["camera_location"])
        camera.location = position
        target = Vector(scene_data["camera_target"])
        if Vector(position) == target:
            raise ValueError("camera position override coincides with target")
        camera.rotation_euler = (
            (target - Vector(position)).normalized().to_track_quat("-Z", "Y").to_euler()
        )
        camera_data.sensor_width = 36.0
        camera_data.sensor_fit = "HORIZONTAL"
        camera_data.lens = camera_settings["field_of_view_mm"]
        if not math.isclose(
            camera_data.lens, float(camera_settings["field_of_view_mm"]), rel_tol=1e-6, abs_tol=1e-6
        ):
            raise RuntimeError("camera lens was clamped by Blender")
        scene.collection.objects.link(camera)
        scene.camera = camera
        for key in scene_data.get("camera_keyframes", []):
            camera.location = tuple(key["location"])
            key_target = Vector(key["target"])
            if Vector(key["location"]) == key_target:
                raise ValueError("camera keyframe location coincides with target")
            camera.rotation_euler = (
                (key_target - Vector(key["location"]))
                .normalized()
                .to_track_quat("-Z", "Y")
                .to_euler()
            )
            camera.keyframe_insert(data_path="location", frame=key["frame"])
            camera.keyframe_insert(data_path="rotation_euler", frame=key["frame"])
        _linearize_action(camera)
        if camera_settings["depth_of_field"] is not None:
            camera_data.dof.use_dof = True
            camera_data.dof.focus_distance = camera_settings["depth_of_field"]
            camera_data.dof.aperture_fstop = 5.6
            if not camera_data.dof.use_dof or not math.isclose(
                camera_data.dof.focus_distance,
                float(camera_settings["depth_of_field"]),
                rel_tol=1e-6,
                abs_tol=1e-6,
            ):
                raise RuntimeError("camera depth-of-field focus distance was clamped")
        monochrome_master = plan["mode"] == "master" and camera_settings["monochrome_night"]
        if (
            camera_settings["barrel_distortion"]
            or camera_settings["exposure_ev"]
            or monochrome_master
        ):
            _configure_compositor(
                bpy,
                scene,
                camera_settings["barrel_distortion"],
                camera_settings["exposure_ev"],
                monochrome=monochrome_master,
            )
        scene.view_settings.view_transform = "Standard"
        # Blender's view exposure is display-referred; apply through compositor so
        # scene-linear EXR masters carry the rebuild-triggering exposure change.
        scene.view_settings.exposure = 0.0
        if plan["mode"] == "preview":
            world = bpy.data.worlds.new("Preview Neutral World")
            world.use_nodes = True
            background = world.node_tree.nodes.get("Background")
            if background:
                background.inputs["Color"].default_value = (0.55, 0.55, 0.55, 1.0)
                background.inputs["Strength"].default_value = 0.5
            scene.world = world
        elif camera_settings["monochrome_night"]:
            for mat in materials.values():
                shader = mat.node_tree.nodes.get("Principled BSDF")
                if shader:
                    color = shader.inputs["Base Color"].default_value
                    luminance = 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]
                    shader.inputs["Base Color"].default_value = (
                        luminance,
                        luminance,
                        luminance,
                        color[3],
                    )
            world = bpy.data.worlds.new("Monochrome Night World")
            world.use_nodes = True
            background = world.node_tree.nodes.get("Background")
            if background:
                background.inputs["Color"].default_value = (0.12, 0.12, 0.12, 1)
                background.inputs["Strength"].default_value = 0.2
            scene.world = world
        else:
            # Spike 02 measured this world illumination for the synthetic
            # interior; retain it as the neutral baseline for generated scenes.
            world = bpy.data.worlds.new("CCTV Neutral World")
            world.use_nodes = True
            background = world.node_tree.nodes.get("Background")
            if background:
                background.inputs["Color"].default_value = (0.08, 0.082, 0.09, 1.0)
                background.inputs["Strength"].default_value = 1.6
            scene.world = world
        if plan["mode"] == "master" and scene_data.get("world_strength") is not None and not scene_data.get("environment"):
            background = scene.world.node_tree.nodes.get("Background") if scene.world and scene.world.use_nodes else None
            if background is None:
                raise RuntimeError("scene world has no Background node for configured world_strength")
            background.inputs["Strength"].default_value = float(scene_data["world_strength"])
        if plan["mode"] == "master" and scene_data.get("environment") is not None:
            _configure_environment(bpy, scene, scene_data["environment"])
            env = scene_data["environment"]
            binding = env["binding"]
            metadata["applied_environment"] = {"asset_id": env["asset_id"], "asset_checksum": binding["asset_checksum"], "strength": env["strength"], "rotation_rad": env["rotation_rad"]}
        if plan["mode"] == "master":
            textured = {
                spec["name"]
                for spec in scene_data["materials"]
                if spec.get("surface", "plain") != "plain"
            }
            for name, mat in materials.items():
                if name not in textured:
                    _add_surface_roughness(mat)
        scene.render.engine = selected
        scene.render.resolution_x, scene.render.resolution_y = plan["width"], plan["height"]
        scene.render.resolution_percentage = 100
        scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1
        scene.render.fps = plan["fps"]
        scene.frame_start, scene.frame_end = plan["frame_start"], plan["frame_end"]
        if plan["engine"] == "eevee":
            scene.eevee.taa_render_samples = plan["samples"]
            if scene.eevee.taa_render_samples != plan["samples"]:
                raise RuntimeError("Eevee render samples were clamped")
        elif plan["engine"] == "cycles":
            cycles_seed = int(plan["seed"])
            if not 0 <= cycles_seed <= 2147483647:
                raise RuntimeError("Cycles seed is outside Blender's signed 32-bit accepted range")
            scene.cycles.samples = plan["samples"]
            scene.cycles.seed = cycles_seed
            if scene.cycles.samples != plan["samples"] or scene.cycles.seed != cycles_seed:
                raise RuntimeError("Cycles samples or deterministic seed were clamped")
        if (
            (scene.render.resolution_x, scene.render.resolution_y)
            != (plan["width"], plan["height"])
            or scene.render.resolution_percentage != 100
            or scene.render.fps != plan["fps"]
        ):
            raise RuntimeError("render dimensions or frame rate were clamped")
        metadata["applied_settings"].update(
            {
                "seed": int(plan["seed"]),
                "color_mode": "RGB",
                "file_format": "PNG" if plan["mode"] == "preview" else "OPEN_EXR",
                "color_depth": "8" if plan["mode"] == "preview" else "16",
                "exr_codec": "ZIP" if plan["mode"] == "master" else None,
                "depth_of_field": camera_settings["depth_of_field"],
                "barrel_distortion": camera_settings["barrel_distortion"],
                "exposure_ev": camera_settings["exposure_ev"],
                "monochrome_night": camera_settings["monochrome_night"],
                "monochrome_conversion": "compositor_rgb_to_bw" if monochrome_master else None,
            }
        )
        scene.render.image_settings.color_mode = "RGB"
        scene.render.image_settings.file_format = "PNG" if plan["mode"] == "preview" else "OPEN_EXR"
        if plan["mode"] == "preview":
            scene.render.image_settings.color_depth = "8"
            shading = scene.display.shading
            shading.light = "STUDIO"
            shading.color_type = "SINGLE"
            shading.single_color = (0.55, 0.55, 0.55)
            shading.show_shadows = True
            shading.show_object_outline = True
            scene.display.render_aa = "FXAA"
            if (
                shading.light != "STUDIO"
                or shading.color_type != "SINGLE"
                or any(
                    not math.isclose(float(actual), 0.55, rel_tol=1e-6, abs_tol=1e-6)
                    for actual in shading.single_color
                )
                or not shading.show_shadows
                or not shading.show_object_outline
                or scene.display.render_aa != "FXAA"
            ):
                raise RuntimeError("Workbench preview shading preset could not be applied")
            metadata["applied_settings"].update(
                {
                    "preview_preset": "workbench_flat_grey_framing",
                    "preview_shading": "STUDIO",
                    "preview_color_mode": "SINGLE",
                    "preview_single_color": [0.55, 0.55, 0.55],
                    "preview_shadows": True,
                    "preview_object_outlines": True,
                    "preview_antialiasing": "FXAA",
                    "preview_samples_semantics": "one Workbench draw pass; not ray samples",
                    "preview_look_limitations": [
                        "physical lighting",
                        "exposure appearance",
                        "DOF blur",
                    ],
                }
            )
        else:
            scene.render.image_settings.color_depth = "16"
            scene.render.image_settings.exr_codec = "ZIP"
        scene.render.use_file_extension = True
        scene.render.filepath = "//frames/frame_000001." + (
            "png" if plan["mode"] == "preview" else "exr"
        )
        scene.render.use_overwrite = False
        scene.render.use_placeholder = False
        scene.render.use_stamp = False
        scene.render.use_compositing = True
        scene.render.use_sequencer = False
        scene.render.use_file_extension = True
        # Use explicit paths rather than Blender's implicit frame naming.
        bpy.context.preferences.filepaths.save_version = 0
        scene.camera.data.lens = camera_settings["field_of_view_mm"]
        scene.frame_set(plan["frame_start"])
        metadata["geometry_fingerprint"] = _fingerprint(scene)
        bpy.ops.wm.save_as_mainfile(filepath=output["blend"], compress=False)
        for index, frame in enumerate(range(plan["frame_start"], plan["frame_end"] + 1), 1):
            if index > len(output["frames"]):
                raise RuntimeError("plan did not enumerate enough frame paths")
            scene.frame_set(frame)
            scene.render.filepath = output["frames"][index - 1]
            bpy.ops.render.render(write_still=True)
            print("CCTV_PROGRESS %.8f" % (index / len(output["frames"])), flush=True)
        metadata["status"] = "completed"
    except BaseException as exc:
        metadata["status"] = "failed"
        metadata["error"] = repr(exc)
        raise
    finally:
        metadata["elapsed_seconds"] = time.monotonic() - started
        _write_json(output["metadata"], metadata)
        print("CCTV_METADATA " + json.dumps(metadata, sort_keys=True), flush=True)


def _write_json(path: str, value: object) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)


def _temperature_color(base: tuple[float, float, float], kelvin: int) -> tuple[float, float, float]:
    # Planckian approximation scaled against the measured fixture color, normalized to [0,1].
    temp = max(1000, min(40000, kelvin)) / 100.0
    if temp <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(temp) - 161.1195681661
        blue = 0.0 if temp <= 19 else 138.5177312231 * math.log(temp - 10) - 305.0447927307
    else:
        red = 329.698727446 * (temp - 60) ** -0.1332047592
        green = 288.1221695283 * (temp - 60) ** -0.0755148492
        blue = 255.0
    kelvin_color = tuple(max(0.0, min(1.0, c / 255)) for c in (red, green, blue))
    return tuple(max(0.0, min(1.0, b * k)) for b, k in zip(base, kelvin_color))


def _linearize_action(obj: object) -> None:
    for curve in _action_fcurves(obj):
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"


def _constantize_action_path(obj: object, data_path: str) -> None:
    for curve in _action_fcurves(obj):
        if getattr(curve, "data_path", None) == data_path:
            for point in curve.keyframe_points:
                point.interpolation = "CONSTANT"


def _action_fcurves(obj: object) -> list[object]:
    action = getattr(getattr(obj, "animation_data", None), "action", None)
    if action is None:
        return []
    curves = list(getattr(action, "fcurves", ()))
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for slot in getattr(action, "slots", ()):
                try:
                    bag = strip.channelbag(slot)
                except (TypeError, RuntimeError):
                    continue
                if bag:
                    curves.extend(getattr(bag, "fcurves", ()))
    return curves


def _configure_material_surface(material: object, spec: dict[str, object]) -> None:
    tree = material.node_tree
    shader = tree.nodes.get("Principled BSDF")
    if shader is None:
        raise RuntimeError("material has no Principled BSDF node")
    coord = tree.nodes.new("ShaderNodeTexCoord")
    mapping = tree.nodes.new("ShaderNodeVectorMath")
    mapping.operation = "MULTIPLY"
    mapping.inputs[1].default_value = tuple(spec.get("texture_scale", (1, 1, 1)))
    tree.links.new(coord.outputs["Object"], mapping.inputs[0])
    surface = str(spec.get("surface", "plain"))
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.noise_dimensions = "3D"
    noise.inputs["Scale"].default_value = {"wood": 1.6, "wood_panel": 1.1, "fabric": 1.5, "rug": 1.8, "plaster": 1.0}.get(surface, 3.0)
    noise.inputs["Detail"].default_value = {"wood": 2.0, "wood_panel": 4.0, "fabric": 2.0, "rug": 3.0, "plaster": 2.0}.get(surface, 3.0)
    noise.inputs["Roughness"].default_value = 0.65
    tree.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    base = tuple(spec["color"])
    var = float(spec.get("variation", 0))
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = tuple(max(0, c * (1 - var)) for c in base[:3]) + (base[3],)
    ramp.color_ramp.elements[1].color = tuple(min(1, c * (1 + var)) for c in base[:3]) + (base[3],)
    tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    if surface == "wood_panel":
        # World-space coordinates keep board width in meters after object transforms.
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        pos = tree.nodes.new("ShaderNodeSeparateXYZ")
        normal = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(geometry.outputs["Position"], pos.inputs[0])
        tree.links.new(geometry.outputs["Normal"], normal.inputs[0])
        abs_nx = tree.nodes.new("ShaderNodeMath")
        abs_nx.operation = "ABSOLUTE"
        tree.links.new(normal.outputs["X"], abs_nx.inputs[0])
        choose_x = tree.nodes.new("ShaderNodeMath")
        choose_x.operation = "LESS_THAN"
        choose_x.inputs[1].default_value = 0.5
        tree.links.new(abs_nx.outputs[0], choose_x.inputs[0])
        x_part = tree.nodes.new("ShaderNodeMath")
        x_part.operation = "MULTIPLY"
        tree.links.new(pos.outputs["X"], x_part.inputs[0])
        tree.links.new(choose_x.outputs[0], x_part.inputs[1])
        y_factor = tree.nodes.new("ShaderNodeMath")
        y_factor.operation = "SUBTRACT"
        y_factor.inputs[0].default_value = 1.0
        tree.links.new(choose_x.outputs[0], y_factor.inputs[1])
        y_part = tree.nodes.new("ShaderNodeMath")
        y_part.operation = "MULTIPLY"
        tree.links.new(pos.outputs["Y"], y_part.inputs[0])
        tree.links.new(y_factor.outputs[0], y_part.inputs[1])
        axis_pos = tree.nodes.new("ShaderNodeMath")
        axis_pos.operation = "ADD"
        tree.links.new(x_part.outputs[0], axis_pos.inputs[0])
        tree.links.new(y_part.outputs[0], axis_pos.inputs[1])
        phase = tree.nodes.new("ShaderNodeMath")
        phase.operation = "PINGPONG"
        phase.inputs[1].default_value = float(spec.get("panel_width", 0.18)) / 2
        tree.links.new(axis_pos.outputs[0], phase.inputs[0])
        seam = tree.nodes.new("ShaderNodeMath")
        seam.operation = "LESS_THAN"
        seam.inputs[1].default_value = float(spec.get("seam_width", 0.003)) / 2
        tree.links.new(phase.outputs[0], seam.inputs[0])
        mix = tree.nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MIX"
        mix.inputs[2].default_value = tuple(max(0.0, c * 0.34) for c in base[:3]) + (base[3],)
        tree.links.new(seam.outputs[0], mix.inputs[0])
        tree.links.new(ramp.outputs["Color"], mix.inputs[1])
        tree.links.new(mix.outputs[0], shader.inputs["Base Color"])
    else:
        tree.links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
    rough = tree.nodes.new("ShaderNodeMapRange")
    roughness_delta = {"wood": 0.08, "wood_panel": 0.06, "fabric": 0.04, "rug": 0.025, "plaster": 0.015}.get(surface, 0.06)
    rough.inputs["To Min"].default_value = max(0, float(spec["roughness"]) - roughness_delta)
    rough.inputs["To Max"].default_value = min(1, float(spec["roughness"]) + roughness_delta)
    tree.links.new(noise.outputs["Fac"], rough.inputs["Value"])
    tree.links.new(rough.outputs["Result"], shader.inputs["Roughness"])
    strength = float(spec.get("bump_strength", 0))
    if strength:
        bump = tree.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = strength
        bump.inputs["Distance"].default_value = 0.025
        tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
        tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])


def _add_surface_roughness(material: object) -> None:
    tree = material.node_tree
    shader = tree.nodes.get("Principled BSDF")
    if shader is None:
        raise RuntimeError("material has no Principled BSDF node")
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 18.0
    noise.inputs["Detail"].default_value = 6.0
    bump = tree.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.045
    tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])


def _configure_compositor(
    bpy: object, scene: object, distortion: float, exposure: float, *, monochrome: bool = False
) -> None:
    # Blender 5 uses a compositor node group whose output connects to a Group Output.
    tree = bpy.data.node_groups.new("CCTV Lens Compositor", "CompositorNodeTree")
    tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    render = tree.nodes.new("CompositorNodeRLayers")
    current = render.outputs["Image"]
    if distortion:
        lens = tree.nodes.new("CompositorNodeLensdist")
        distortion_socket = next(
            (item for item in lens.inputs if item.name in {"Distortion", "Distortion Radial"}), None
        )
        if distortion_socket is None:
            raise RuntimeError("Blender compositor has no supported radial distortion input")
        distortion_socket.default_value = distortion
        if not math.isclose(
            float(distortion_socket.default_value), float(distortion), rel_tol=1e-6, abs_tol=1e-6
        ):
            raise RuntimeError("Blender compositor clamped the requested barrel distortion")
        tree.links.new(current, lens.inputs["Image"])
        current = lens.outputs["Image"]
    if exposure:
        exposure_node = tree.nodes.new("CompositorNodeExposure")
        exposure_node.inputs["Exposure"].default_value = exposure
        if not math.isclose(
            float(exposure_node.inputs["Exposure"].default_value),
            float(exposure),
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            raise RuntimeError("compositor exposure was clamped")
        tree.links.new(current, exposure_node.inputs["Image"])
        current = exposure_node.outputs["Image"]
    if monochrome:
        monochrome_node = tree.nodes.new("CompositorNodeRGBToBW")
        tree.links.new(current, monochrome_node.inputs["Image"])
        current = monochrome_node.outputs["Val"]
    output = tree.nodes.new("NodeGroupOutput")
    output.is_active_output = True
    tree.links.new(current, output.inputs[0])
    if hasattr(scene, "compositing_node_group"):
        scene.compositing_node_group = tree
        scene.use_nodes = True
    elif hasattr(scene, "node_tree"):
        scene.node_tree = tree
        scene.use_nodes = True
    else:
        raise RuntimeError("this Blender version exposes no compositor node group property")


def _select_cycles_device(bpy: object, scene: object, requested: str) -> None:
    scene.cycles.device = "GPU"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "OPTIX"
    prefs.get_devices()
    matches = []
    for device in prefs.devices:
        selected = device.type == "OPTIX" and device.name == requested
        device.use = selected
        if selected:
            matches.append(device)
    if len(matches) != 1:
        raise RuntimeError(
            f"requested OptiX device {requested!r} unavailable; no CPU/iGPU fallback allowed"
        )


def _fingerprint(scene: object) -> str:
    rows = []
    for obj in sorted(scene.objects, key=lambda item: item.name):
        rows.append(
            (
                obj.name,
                obj.type,
                tuple(round(v, 7) for v in obj.location),
                tuple(round(v, 7) for v in obj.dimensions),
                tuple(round(v, 7) for v in obj.rotation_euler),
            )
        )
        if obj.type == "MESH":
            rows[-1] += (
                len(obj.data.vertices),
                len(obj.data.edges),
                len(obj.data.polygons),
                hashlib.sha256(
                    b"".join(
                        bytes(str(tuple(round(c, 7) for c in vertex.co)), "ascii")
                        for vertex in obj.data.vertices
                    )
                ).hexdigest(),
                hashlib.sha256(
                    b"".join(
                        bytes(str(tuple(poly.vertices)), "ascii") for poly in obj.data.polygons
                    )
                ).hexdigest(),
            )
        elif obj.type == "CAMERA":
            rows[-1] += (
                round(obj.data.lens, 7),
                round(obj.data.clip_start, 7),
                round(obj.data.clip_end, 7),
                bool(obj.data.dof.use_dof),
                round(obj.data.dof.focus_distance, 7),
                round(obj.data.dof.aperture_fstop, 7),
            )
        modifiers = tuple(getattr(obj, "modifiers", ()))
        if modifiers:
            modifier_rows = []
            for modifier in modifiers:
                settings = {"type": modifier.type, "show_render": bool(modifier.show_render)}
                if modifier.type == "BEVEL":
                    settings.update({
                        "width": round(float(modifier.width), 7),
                        "segments": int(modifier.segments),
                        "profile": round(float(modifier.profile), 7),
                        "limit_method": modifier.limit_method,
                        "offset_type": modifier.offset_type,
                        "use_clamp_overlap": bool(modifier.use_clamp_overlap),
                    })
                elif modifier.type == "WEIGHTED_NORMAL":
                    settings.update({
                        "keep_sharp": bool(modifier.keep_sharp),
                        "weight": int(modifier.weight),
                        "thresh": round(float(modifier.thresh), 7),
                        "use_face_influence": bool(modifier.use_face_influence),
                    })
                modifier_rows.append(tuple(sorted(settings.items())))
            rows[-1] += (tuple(modifier_rows),)
        action = getattr(getattr(obj, "animation_data", None), "action", None)
        if action is not None:
            curves = list(getattr(action, "fcurves", ()))
            for layer in getattr(action, "layers", ()):
                for strip in getattr(layer, "strips", ()):
                    for slot in getattr(action, "slots", ()):
                        try:
                            bag = strip.channelbag(slot)
                        except (TypeError, RuntimeError):
                            continue
                        if bag:
                            curves.extend(getattr(bag, "fcurves", ()))
            keyframes = sorted(
                (
                    curve.data_path,
                    curve.array_index,
                    tuple(
                        (round(point.co.x, 7), round(point.co.y, 7), point.interpolation)
                        for point in curve.keyframe_points
                    ),
                )
                for curve in curves
            )
            rows[-1] += (tuple(keyframes),)
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def _import_gltf_asset(bpy, Matrix, Euler, Vector, scene, spec):
    """Import a glTF hierarchy and fit its world-space bounds to ObjectSpec.dimensions."""
    path = spec.get("asset_path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise RuntimeError(f"Asset {spec.get('asset_id')!r} has no absolute worker path")
    extension = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as stream:
            header = stream.read(12)
    except OSError as exc:
        raise RuntimeError(f"Asset {spec.get('asset_id')!r} could not be read: {exc}") from exc
    if extension == ".blend" or header.startswith(b"BLENDER"):
        raise RuntimeError(
            f"Asset {spec.get('asset_id')!r} is a Blender .blend file, which can execute "
            "Python on load; only .glb files can be imported as geometry"
        )
    if extension != ".glb":
        raise RuntimeError(
            f"Asset {spec.get('asset_id')!r} has unsupported geometry format {extension!r}; "
            "only .glb files can be imported as geometry"
        )
    if not hasattr(bpy.ops.import_scene, "gltf"):
        raise RuntimeError("Blender's bundled glTF importer is unavailable under this startup mode")

    existing = set(scene.objects)
    result = bpy.ops.import_scene.gltf(filepath=path)
    if "FINISHED" not in result:
        raise RuntimeError(f"glTF importer did not finish for Asset {spec.get('asset_id')!r}")
    imported = [obj for obj in scene.objects if obj not in existing]
    meshes = [obj for obj in imported if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError(f"Asset {spec.get('asset_id')!r} imported no mesh geometry")

    corners = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    minimum = Vector(tuple(min(point[axis] for point in corners) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in corners) for axis in range(3)))
    try:
        factors = asset_fit_factors(minimum, maximum, spec["dimensions"], fit_mode=spec.get("fit_mode", "exact"))
    except ValueError as exc:
        raise RuntimeError(
            f"Asset {spec.get('asset_id')!r} cannot be fitted to declared dimensions: {exc}"
        ) from exc
    center = (minimum + maximum) * 0.5

    root = bpy.data.objects.new(spec["name"], None)
    scene.collection.objects.link(root)
    root.matrix_world = Matrix.Identity(4)
    center_transform = Matrix.Translation(-center)
    original_worlds = {obj: obj.matrix_world.copy() for obj in imported}
    for obj in imported:
        obj.parent = root
        obj.matrix_world = center_transform @ original_worlds[obj]
    root["cctv_source_min"] = list(minimum)
    root["cctv_source_max"] = list(maximum)
    root["cctv_source_dimensions"] = [maximum[i]-minimum[i] for i in range(3)]
    root.location = tuple(spec["location"])
    root.scale = factors
    root.rotation_euler = Euler(tuple(spec.get("rotation", (0, 0, 0))), "XYZ")
    bpy.context.view_layer.update()
    return root, imported


if __name__ == "__main__":
    try:
        separator = sys.argv.index("--")
        _run(sys.argv[separator + 1])
    except Exception as error:
        print("CCTV_FATAL " + repr(error), file=sys.stderr, flush=True)
        raise
