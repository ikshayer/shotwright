"""Standalone Blender-side worker. Keep imports limited to Blender and stdlib."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time


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
    from mathutils import Vector

    started = time.monotonic()
    scene_data = plan["scene"]
    output = plan["outputs"]
    camera_settings = plan["camera"]
    metadata = {
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
            elif kind == "torus":
                bpy.ops.mesh.primitive_torus_add(
                    major_segments=48, minor_segments=12, location=spec["location"]
                )
            else:
                raise ValueError(f"unsupported object kind {kind!r}")
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
            if plan["mode"] == "master":
                obj.data.materials.append(materials[spec["material"]])
            if spec["name"] == "RoomShell":
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.mesh.flip_normals()
                bpy.ops.object.mode_set(mode="OBJECT")
            for key in spec["keyframes"]:
                obj.location = tuple(key["location"])
                obj.keyframe_insert(data_path="location", frame=key["frame"])
            for key in spec.get("visibility_keyframes", []):
                obj.hide_render = not bool(key["visible"])
                obj.keyframe_insert(data_path="hide_render", frame=key["frame"])
            _linearize_action(obj)
            _constantize_action_path(obj, "hide_render")
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
    noise.inputs["Scale"].default_value = {"wood": 1.6, "fabric": 1.5, "rug": 1.8, "plaster": 1.0}.get(surface, 3.0)
    noise.inputs["Detail"].default_value = {"wood": 2.0, "fabric": 2.0, "rug": 3.0, "plaster": 2.0}.get(surface, 3.0)
    noise.inputs["Roughness"].default_value = 0.65
    tree.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    base = tuple(spec["color"])
    var = float(spec.get("variation", 0))
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = tuple(max(0, c * (1 - var)) for c in base[:3]) + (base[3],)
    ramp.color_ramp.elements[1].color = tuple(min(1, c * (1 + var)) for c in base[:3]) + (base[3],)
    tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
    rough = tree.nodes.new("ShaderNodeMapRange")
    roughness_delta = {"wood": 0.08, "fabric": 0.04, "rug": 0.025, "plaster": 0.015}.get(surface, 0.06)
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


if __name__ == "__main__":
    try:
        separator = sys.argv.index("--")
        _run(sys.argv[separator + 1])
    except Exception as error:
        print("CCTV_FATAL " + repr(error), file=sys.stderr, flush=True)
        raise
