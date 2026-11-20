"""Build an editable abandoned-room set and render CPU stills."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import sys
import time
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WIDTH, DEPTH, HEIGHT = 2.65, 3.93, 2.49
HALF = WIDTH / 2
WINDOW_WIDTH, SILL, HEAD = 1.19, 0.69, 2.12
CAMERA_LOCATION = (0.052, 0.20, 1.53)
SEED = 61918
NIGHT_KELVIN = 2830
NIGHT_BULB_WATTS = 19
APARTMENT_VIEWS = ("hallway", "living_room", "kitchen", "bathroom")
LIVING_ROOM_BOUNDS = ((-3.65, -0.54), (-3.42, -0.24))
LIVING_SOFA_SHA256 = "8e4a929eb3200ce18423229b447e476157aa5b2a4adebc06c43f4bdc0eb3bc34"
CABINET_WALL_CLEARANCE_M = 0.002
PROP_SURFACES = {
    "dresser": (0.19, 0.33, 0.64, 0.39, "varnished wood, excluding strongly metallic areas"),
    "radiator": (0.24, 0.42, 0.88, 0.43, "painted cast iron"),
    "doll": (0.17, 0.64, 0.93, 0.39, "warm coat below the face"),
    "rubbish": (0.13, 0.59, 0.95, 0.37, "paper and cardboard, attenuated on dark pieces"),
    "debris": (0.25, 0.67, 0.97, 0.43, "matte broken plaster"),
}
MATFUSE_STUDIO = "spikes/30_at18_found_footage/_work/matfuse-cabinet-room-assets/review_meshes.py"
MATFUSE_STUDIO_SHA256 = "075268e8d16c91286ccc6cf217540ff41ae16cce2e56b6f4ef98d92fc02a7237"


def _matfuse_helpers(path: Path) -> dict:
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != MATFUSE_STUDIO_SHA256:
        raise RuntimeError("The staged MatFuse studio helper does not match the verified bench script")
    namespace = {"__file__": str(path), "__name__": "_verified_matfuse_studio"}
    # Execute exactly the bytes verified against the compiled-in bench digest.
    exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    return namespace


def _coordinator() -> None:
    sys.path.insert(0, str(REPO))
    from run import _checked_program

    from orchestrator.platform.containment import ProjectRoot
    from orchestrator.service.assets import _validate_format

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dresser", required=True)
    parser.add_argument("--radiator", required=True)
    parser.add_argument("--dresser-yaw", type=float, default=-90.0)
    parser.add_argument("--radiator-yaw", type=float, default=0.0)
    parser.add_argument("--cabinet-panel")
    parser.add_argument("--cabinet-panel-yaw", type=float)
    parser.add_argument("--cabinet-matfuse", nargs=3, metavar=("NORMAL_PNG", "ROUGHNESS_PNG", "MASK_PNG"),
                        help="Use the matched studio MatFuse finish on the RealGen cabinet")
    parser.add_argument("--debris")
    parser.add_argument("--debris-yaw", type=float, default=0.0)
    parser.add_argument("--rubbish", help="Use a project-local GLB for low paper and cardboard litter")
    parser.add_argument("--rubbish-yaw", type=float, default=0.0)
    parser.add_argument("--doll")
    parser.add_argument("--doll-yaw", type=float, default=-22.0)
    parser.add_argument("--bottle", help="Use a project-local upright GLB in the dresser bottle position")
    parser.add_argument("--bottle-yaw", type=float, default=0.0)
    parser.add_argument("--wallpaper", help="Use a project-local PNG albedo for the room walls")
    parser.add_argument("--facade", help="Use a project-local PNG for the neighboring building seen through the window")
    parser.add_argument("--brick", help="Use a project-local brick albedo on the exposed masonry")
    parser.add_argument("--prop-surface", nargs=3, action="append", default=[],
                        metavar=("PROP", "NORMAL_PNG", "ROUGHNESS_PNG"),
                        help="Apply packed MatFuse maps to dresser, radiator, doll, rubbish or debris")
    parser.add_argument("--night", action="store_true", help="Darken the window and light the room from the warm chandelier")
    parser.add_argument("--expanded-apartment", action="store_true",
                        help="Add connected rooms and render their separate views.")
    parser.add_argument("--living-sofa", help="Use the pinned generated sofa in the expanded living room.")
    parser.add_argument("--width", type=int, choices=(640, 960), default=640)
    parser.add_argument("--blender", default=r"D:\Blender\blender.exe")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if args.cabinet_matfuse is not None and args.cabinet_panel is None:
        parser.error("--cabinet-matfuse requires the matching --cabinet-panel GLB")
    if args.living_sofa is not None and not args.expanded_apartment:
        parser.error("--living-sofa requires --expanded-apartment")
    if args.cabinet_panel_yaw is None:
        args.cabinet_panel_yaw = 255.0 if args.cabinet_matfuse is not None else 60.0
    repo = ProjectRoot(REPO)

    def pin(relative, extension):
        source = repo.resolve(relative)
        if source.suffix.lower() != extension:
            raise ValueError(f"This asset requires a contained {extension} path")
        with source.open("rb") as stream:
            _validate_format(stream, filename=source.name, size=os.fstat(stream.fileno()).st_size)
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"path": str(source), "sha256": digest}

    props = {name: {**pin(getattr(args, name), ".glb"), "yaw_deg": getattr(args, f"{name}_yaw")}
             for name in ("dresser", "radiator", "cabinet_panel", "debris", "rubbish", "doll", "bottle")
             if getattr(args, name) is not None}
    if args.living_sofa is not None:
        sofa = pin(args.living_sofa, ".glb")
        if sofa["sha256"] != LIVING_SOFA_SHA256:
            raise ValueError("The living-room sofa does not match the expected pinned GLB")
        props["living_sofa"] = {**sofa, "yaw_deg": 90.0}
    if any(not math.isfinite(binding["yaw_deg"]) for binding in props.values()):
        raise ValueError("Each prop yaw must be finite")
    if len({binding["sha256"] for binding in props.values()}) != len(props):
        raise ValueError("Each prop must bind a distinct GLB asset")
    wallpaper = pin(args.wallpaper, ".png") if args.wallpaper is not None else None
    facade = pin(args.facade, ".png") if args.facade is not None else None
    brick = pin(args.brick, ".png") if args.brick is not None else None
    matfuse = None
    helper_path = repo.resolve(MATFUSE_STUDIO) if args.cabinet_matfuse or args.prop_surface else None
    helpers = _matfuse_helpers(helper_path) if helper_path is not None else {}
    if args.cabinet_matfuse is not None:
        maps = {role: helpers["pin_png"](repo, path, role)
                for role, path in zip(("normal", "roughness", "mask"), args.cabinet_matfuse, strict=True)}
        if maps["normal"]["size_px"] != maps["roughness"]["size_px"] or len({
                binding["sha256"] for binding in maps.values()}) != len(maps):
            raise ValueError("MatFuse expects separate normal, roughness and mask PNGs with matching map dimensions")
        matfuse = {"mode": "matfuse", "helper": str(helper_path), **maps}
    prop_surfaces = {}
    for role, normal_path, roughness_path in args.prop_surface:
        if role not in PROP_SURFACES or role not in props or role in prop_surfaces:
            parser.error("Each --prop-surface must name one distinct, present dresser, radiator, doll, rubbish or debris.")
        normal_map = helpers["pin_png"](repo, normal_path, "normal")
        roughness_map = helpers["pin_png"](repo, roughness_path, "roughness")
        if normal_map["size_px"] != roughness_map["size_px"] or normal_map["sha256"] == roughness_map["sha256"]:
            raise ValueError(f"{role} expects distinct MatFuse normal and roughness PNGs at the same dimensions")
        prop_surfaces[role] = {"normal": normal_map, "roughness": roughness_map}
    root = ProjectRoot(repo.make_dir(f"spikes/30_at18_found_footage/_work/abandoned-room-{uuid4().hex}"))
    runtime = str(root.make_dir("tmp"))
    stills = ("room", *APARTMENT_VIEWS) if args.expanded_apartment else ("room",)
    outputs = ("room.blend", "room.json", *(f"{view}.png" for view in stills))
    plan = {
        "props": props,
        "wallpaper": wallpaper,
        "facade": facade,
        "brick": brick,
        "cabinet_matfuse": matfuse,
        "prop_surfaces": prop_surfaces,
        "prop_surface_helper": str(helper_path) if prop_surfaces else None,
        "night": args.night,
        "expanded_apartment": args.expanded_apartment,
        "render_size": [args.width, args.width * 3 // 4],
        "worker": str(REPO / "orchestrator/adapters/blender/worker.py"),
        "outputs": {name: str(root.write_with(name, lambda path: None))
                    for name in outputs},
    }
    plan_path = root.write_text("plan.json", json.dumps(plan, indent=2))
    print(f"Plan: {plan_path}", flush=True)
    if args.plan_only:
        return
    blender = shutil.which(args.blender)
    if not blender:
        raise RuntimeError("Set --blender to the existing Blender executable")
    env = dict(os.environ, TMP=runtime, TEMP=runtime, TMPDIR=runtime, BLENDER_USER_CONFIG=runtime,
               PYTHONDONTWRITEBYTECODE="1")
    argv = [blender, "--background", "--factory-startup", "--disable-autoexec", "--threads", "8",
            "--python-exit-code", "1", "--python", str(Path(__file__).resolve()), "--", str(plan_path)]
    _checked_program(root, "blender", argv, timeout=300 * len(stills), env=env)
    if not all(root.resolve(name).is_file() for name in plan["outputs"]):
        raise RuntimeError("The Blender process omitted a room deliverable")
    for view in stills:
        print(f"Render: {root.resolve(f'{view}.png')}", flush=True)


def _stains(prim, material, *, scale, low):
    tree = material.node_tree
    base = tree.nodes.get("Principled BSDF").inputs["Base Color"]
    position = tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = scale
    noise.inputs["Detail"].default_value = 7
    noise.inputs["Roughness"].default_value = 0.72
    tree.links.new(position, noise.inputs["Vector"])
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.27
    ramp.color_ramp.elements[0].color = (*low, 1)
    ramp.color_ramp.elements[1].position = 0.69
    tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    stain = tree.nodes.new("ShaderNodeMixRGB")
    stain.blend_type = "MULTIPLY"
    stain.inputs[0].default_value = 1
    if base.links:
        tree.links.new(base.links[0].from_socket, stain.inputs[1])
    else:
        stain.inputs[1].default_value = base.default_value
    tree.links.new(ramp.outputs["Color"], stain.inputs[2])
    tree.links.new(stain.outputs["Color"], base)
    return material


def _aged_floral_paper(prim):
    surface = prim._wallpaper()
    rng = random.Random(SEED + 3)

    def pixel(u, v):
        x = (u + (0.46 if v >= 0.5 else 0)) % 1 - 0.5
        y = (v * 2) % 1 - 0.5
        r = math.hypot(x * 0.83, y)
        angle = math.atan2(y, x * 0.83)
        petals = math.exp(-(((r - 0.129 - 0.050 * math.cos(5 * angle)) / 0.043) ** 2))
        center = math.exp(-((r / 0.081) ** 2))
        vine = math.exp(-(((x - 0.23 * math.sin(y * 6.5)) / 0.018) ** 2))
        leaf = math.exp(-(((x - 0.17 + y * 0.37) / 0.095) ** 2) - (((y + 0.25) / 0.061) ** 2))
        wear = max(0.38, 0.83 + 0.27 * math.sin(u * 19 + v * 37) + rng.uniform(-0.19, 0.20))
        umber = wear * (0.20 * petals + 0.27 * center + 0.13 * leaf + 0.05 * vine)
        paper = 0.62 + rng.uniform(-0.035, 0.035) - umber
        return paper, paper * 0.91 - 0.024 * petals, paper * 0.74

    texture = next(node for node in surface.node_tree.nodes if node.type == "TEX_IMAGE")
    texture.image = prim._painted_texture("Original worn floral repeat", 176, 176, pixel)
    texture.inputs["Vector"].links[0].from_node.inputs[1].default_value = (2.25, 1.55, 1.0)
    return _stains(prim, surface, scale=5.3, low=(0.083, 0.069, 0.056))


def _packed_png(prim, binding, *, name, saved_path):
    path = Path(binding["path"])
    if not path.is_absolute() or path.suffix.lower() != ".png":
        raise RuntimeError(f"{name} requires an authorized absolute PNG path")

    def verify():
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != binding["sha256"]:
                raise RuntimeError(f"{name} PNG changed after the room plan was authorized")

    verify()
    image = prim.bpy.data.images.load(str(path), check_existing=False)
    image.name = name
    image.colorspace_settings.name = "sRGB"
    width, height = image.size
    if min(width, height) <= 0:
        raise RuntimeError(f"{name} PNG did not decode")
    image.pack()
    verify()
    if image.packed_file is None:
        raise RuntimeError(f"{name} did not pack into the scene")
    image.filepath = saved_path
    return image, {"sha256": binding["sha256"], "size_px": [width, height], "packed": True}


def _wallpaper_surfaces(prim, binding):
    if binding is None:
        paper = _aged_floral_paper(prim)
        pale_paper = paper.copy()
        pale_paper.name = "Lighter faded plaster paper"
        _stains(prim, pale_paper, scale=1.6, low=(0.43, 0.34, 0.25))
        return (pale_paper, paper, paper), None

    image, report = _packed_png(prim, binding, name="Original distressed wallpaper albedo", saved_path="//wallpaper.png")

    # Cover each wall once without stretching the square motifs or wrapping a
    # non-seamless texture. World coordinates keep the rear window pieces aligned.
    aspect = image.size[0] / image.size[1]
    sheet_width = max(DEPTH, HEIGHT * aspect) * 1.13
    sheet_height = sheet_width / aspect
    vertical_margin = (1 - HEIGHT / sheet_height) * 0.5
    base = prim._wallpaper()
    surfaces = (base, base.copy(), base.copy())
    mappings = (("Left wall", -1, 0.96, -0.37), ("Right wall", 1, 0.026, 0.42), ("Rear wall", 1, 0.48, 0.04))
    for material, (name, direction, horizontal_offset, vertical_offset) in zip(surfaces, mappings, strict=True):
        material.name = f"{name}: worn floral wallpaper"
        tree = material.node_tree
        texture = next(node for node in tree.nodes if node.type == "TEX_IMAGE")
        texture.image = image
        texture.extension = "EXTEND"
        scale = texture.inputs["Vector"].links[0].from_node
        scale.inputs[1].default_value = (direction / sheet_width, 1 / sheet_height, 1.0)
        offset = tree.nodes.new("ShaderNodeVectorMath")
        offset.operation = "ADD"
        offset.inputs[1].default_value = (horizontal_offset, vertical_margin * (1 + vertical_offset), 0)
        tree.links.new(scale.outputs["Vector"], offset.inputs[0])
        tree.links.new(offset.outputs["Vector"], texture.inputs["Vector"])
    return surfaces, {**report, "sheet_m": [sheet_width, sheet_height]}


def _floor_sheen(material):
    tree = material.node_tree
    position = tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 4.8
    noise.inputs["Detail"].default_value = 3.8
    tree.links.new(position, noise.inputs["Vector"])
    grain = tree.nodes.new("ShaderNodeMapRange")
    grain.inputs["From Min"].default_value = 0.31
    grain.inputs["From Max"].default_value = 0.68
    grain.inputs["To Min"].default_value = 0.37
    grain.inputs["To Max"].default_value = 0.82
    tree.links.new(noise.outputs["Fac"], grain.inputs["Value"])
    shader = tree.nodes.get("Principled BSDF")
    tree.links.new(grain.outputs["Result"], shader.inputs["Roughness"])
    polish = tree.nodes.new("ShaderNodeMapRange")
    polish.label = "Broken patches of reflective varnish"
    polish.inputs["From Min"].default_value = 0.31
    polish.inputs["From Max"].default_value = 0.68
    polish.inputs["To Min"].default_value = 0.17
    polish.inputs["To Max"].default_value = 0.018
    tree.links.new(noise.outputs["Fac"], polish.inputs["Value"])
    tree.links.new(polish.outputs["Result"], shader.inputs["Specular IOR Level"])

    stretch = tree.nodes.new("ShaderNodeVectorMath")
    stretch.operation = "MULTIPLY"
    stretch.inputs[1].default_value = (83, 0.82, 6)
    tree.links.new(position, stretch.inputs[0])
    fibers = tree.nodes.new("ShaderNodeTexNoise")
    fibers.inputs["Scale"].default_value = 1.2
    fibers.inputs["Detail"].default_value = 4.4
    tree.links.new(stretch.outputs["Vector"], fibers.inputs["Vector"])
    color = tree.nodes.new("ShaderNodeValToRGB")
    color.label = "Long exposed wood fibers and scratches"
    color.color_ramp.elements[0].position = 0.27
    color.color_ramp.elements[0].color = (0.19, 0.16, 0.12, 1)
    color.color_ramp.elements[1].position = 0.77
    color.color_ramp.elements[1].color = (0.94, 0.84, 0.73, 1)
    tree.links.new(fibers.outputs["Fac"], color.inputs["Fac"])
    base = shader.inputs["Base Color"]
    weathered = tree.nodes.new("ShaderNodeMixRGB")
    weathered.blend_type = "MULTIPLY"
    weathered.inputs[0].default_value = 0.73
    tree.links.new(base.links[0].from_socket, weathered.inputs[1])
    tree.links.new(color.outputs["Color"], weathered.inputs[2])

    axes = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(position, axes.inputs["Vector"])
    outward = tree.nodes.new("ShaderNodeMath")
    outward.operation = "ABSOLUTE"
    tree.links.new(axes.outputs["X"], outward.inputs[0])
    shift = tree.nodes.new("ShaderNodeMath")
    shift.operation = "MULTIPLY_ADD"
    shift.inputs[1].default_value = 0.39
    tree.links.new(noise.outputs["Fac"], shift.inputs[0])
    tree.links.new(outward.outputs[0], shift.inputs[2])
    edge = tree.nodes.new("ShaderNodeMapRange")
    edge.label = "Irregular dampness near the baseboards"
    edge.inputs["From Min"].default_value = HALF - 0.04
    edge.inputs["From Max"].default_value = HALF + 0.30
    edge.inputs["To Max"].default_value = 0.80
    tree.links.new(shift.outputs[0], edge.inputs["Value"])
    damp = tree.nodes.new("ShaderNodeMixRGB")
    damp.blend_type = "MULTIPLY"
    damp.inputs[2].default_value = (0.14, 0.16, 0.15, 1)
    tree.links.new(edge.outputs["Result"], damp.inputs[0])
    tree.links.new(weathered.outputs["Color"], damp.inputs[1])
    tree.links.new(damp.outputs["Color"], base)


def _recess_masonry(prim, binding):
    material = prim._material("Exposed brick and mortar behind torn wallpaper", (0.25, 0.135, 0.097), roughness=0.89)
    tree = material.node_tree
    shader = tree.nodes.get("Principled BSDF")
    axes = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"], axes.inputs["Vector"])
    surface = tree.nodes.new("ShaderNodeCombineXYZ")
    tree.links.new(axes.outputs["Y"], surface.inputs["X"])
    tree.links.new(axes.outputs["Z"], surface.inputs["Y"])
    report = None
    if binding is None:
        texture = tree.nodes.new("ShaderNodeTexBrick")
        texture.inputs["Scale"].default_value = 1
        texture.inputs["Brick Width"].default_value = 0.245
        texture.inputs["Row Height"].default_value = 0.072
        texture.inputs["Mortar Size"].default_value = 0.008
        texture.inputs["Color1"].default_value = (0.24, 0.094, 0.062, 1)
        texture.inputs["Color2"].default_value = (0.33, 0.16, 0.108, 1)
        texture.inputs["Mortar"].default_value = (0.15, 0.123, 0.10, 1)
        tree.links.new(surface.outputs["Vector"], texture.inputs["Vector"])
    else:
        image, report = _packed_png(prim, binding, name="Original worn masonry albedo", saved_path="//brick.png")
        sheet_width = 0.55
        sheet_height = sheet_width * image.size[1] / image.size[0]
        scale = tree.nodes.new("ShaderNodeVectorMath")
        scale.operation = "MULTIPLY"
        scale.inputs[1].default_value = (1 / sheet_width, 1 / sheet_height, 1)
        tree.links.new(surface.outputs["Vector"], scale.inputs[0])
        offset = tree.nodes.new("ShaderNodeVectorMath")
        offset.operation = "ADD"
        offset.inputs[1].default_value = (0.52 - 2.17 / sheet_width, 0.47 - 1.44 / sheet_height, 0)
        tree.links.new(scale.outputs["Vector"], offset.inputs[0])
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.extension = "EXTEND"
        texture.image = image
        tree.links.new(offset.outputs["Vector"], texture.inputs["Vector"])
        report["sheet_m"] = [sheet_width, sheet_height]
    tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])
    bump = tree.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.38
    bump.inputs["Distance"].default_value = 0.007
    tree.links.new(texture.outputs["Color"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])
    return material, report


def _chipped_opening(profile, rng):
    sampled = []
    for index, (lower, upper) in enumerate(pairwise(profile)):
        before = profile[max(0, index - 1)]
        after = profile[min(len(profile) - 1, index + 2)]
        height = upper[0] - lower[0]
        samples = max(2, round(height / 0.0054))
        sampled.append(lower)
        for number in range(1, samples):
            t = (number + rng.uniform(-0.27, 0.27)) / samples
            eased = t * t * (3 - 2 * t)
            slope_start = t * (1 - t) ** 2
            slope_end = -t * t * (1 - t)
            left, right = [lower[k] * (1 - eased) + upper[k] * eased
                           + (upper[k] - before[k]) * 0.37 * slope_start
                           + (after[k] - lower[k]) * 0.37 * slope_end for k in (1, 2)]
            if right <= left:
                left, right = [lower[k] + (upper[k] - lower[k]) * t for k in (1, 2)]
            chip = min((right - left) * 0.19, rng.uniform(0.0007, 0.0041))
            sampled.append((lower[0] + height * t, left + rng.uniform(-chip, chip), right + rng.uniform(-chip, chip)))
    return (*sampled, profile[-1])


def _tapered_fracture(prim, material, name, profile, width, rng):
    trace = []
    for (y0, z0), (y1, z1) in pairwise(profile):
        length = math.hypot(y1 - y0, z1 - z0)
        samples = max(2, round(length / 0.006))
        for index in range(samples):
            t = index / samples
            chip = rng.uniform(-0.0015, 0.0015) if index else 0
            trace.append((HALF - 0.00015, y0 + (y1 - y0) * t + chip, z0 + (z1 - z0) * t))
    trace.append((HALF - 0.00015, *profile[-1]))
    crack = prim._line(name, trace, material, width)
    length = len(trace) - 1
    for index, node in enumerate(crack.data.splines[0].points):
        node.radius = max(0.055, (1 - index / length) ** 1.38 * rng.uniform(0.67, 1.0))


def _opening_topology(opening):
    outline = [(y, z) for z, y, _ in opening]
    outline.extend((y, z) for z, _, y in reversed(opening[1:-1]))
    count = len(outline)
    strips = []
    last = len(opening) - 1
    for row in range(last):
        bottom_right = count - row if row else 0
        top_right = count - row - 1 if row + 1 < last else last
        strips.append(tuple(dict.fromkeys((row, row + 1, top_right, bottom_right))))
    bevel = [(i, (i + 1) % count, (i + 1) % count + count, i + count) for i in range(count)]
    return outline, strips, bevel


def _upper_wall_tear(prim, paper, adhesive, opening, rng):
    outline, strips, bevel = _opening_topology(opening)
    filler = prim._material("Bare weathered lime skim beneath wallpaper", (0.291, 0.257, 0.207), roughness=0.97)
    prim._surface_detail(filler, (48, 68, 75), strength=0.42, distance=0.003, dark_level=0.32)
    _stains(prim, filler, scale=19, low=(0.62, 0.53, 0.43))
    surface = [(HALF, y, z) for y, z in outline]
    skim, substrate = [], []
    depths = []
    for y, z in outline:
        recess = 0.005 + 0.0011 * math.sin(41 * y + 29 * z) + rng.uniform(-0.0007, 0.0007)
        dy = (3.346 - y) * rng.uniform(0.010, 0.047)
        dz = (2.205 - z) * rng.uniform(0.010, 0.047)
        skim.append((HALF + rng.uniform(0.0008, 0.0015), y + dy * 0.20, z + dz * 0.20))
        substrate.append((HALF + recess, y + dy, z + dz))
        depths.append(recess)
    prim._mesh("Frayed wallpaper and exposed adhesive", surface + skim, bevel, adhesive)
    prim._mesh("Shallow fractured lime skim", skim + substrate, bevel, filler)
    prim._mesh("Recessed worn lime skim", substrate, strips, filler)

    # Three small edge runs have lifted and bent inward from the adhesive.
    split = len(opening)
    for start, stop in ((6, 12), (split - 12, split - 5), (split + 15, split + 21)):
        edge = surface[start:stop]
        lip = []
        for index, (_, y, z) in enumerate(edge):
            curl = math.sin(math.pi * index / (len(edge) - 1))
            lip.append((HALF - 0.0039 * curl, y + (3.346 - y) * curl * 0.053,
                        z + (2.205 - z) * curl * 0.053))
        length = len(edge)
        ribbon = [(i, i + 1, length + i + 1, length + i) for i in range(length - 1)]
        prim._mesh("Curl of torn floral paper", edge + lip, ribbon, paper)
    _tapered_fracture(prim, adhesive, "Hairline below peeled skim",
                     ((3.303, 2.024), (3.293, 2.011), (3.302, 1.989), (3.289, 1.969)), 0.00054, rng)
    return {"depth_range_m": [round(min(depths), 5), round(max(depths), 5)], "center_yz_m": [3.346, 2.205]}


def _right_wall(prim, paper, brick):
    # Rows trace both lips of one elongated tear. The papered wall contains a
    # genuine open region here, with plaster bevels and brick farther into the wall.
    profile = ((1.195, 2.214, 2.214), (1.207, 2.199, 2.238), (1.225, 2.180, 2.257),
               (1.244, 2.183, 2.253), (1.279, 2.140, 2.284), (1.302, 2.122, 2.279),
               (1.333, 2.101, 2.321), (1.359, 2.063, 2.324), (1.387, 2.084, 2.335),
               (1.419, 2.069, 2.312), (1.450, 2.042, 2.351), (1.478, 2.049, 2.345),
               (1.508, 2.087, 2.325), (1.542, 2.093, 2.307), (1.561, 2.081, 2.285),
               (1.599, 2.111, 2.291), (1.635, 2.127, 2.257), (1.672, 2.164, 2.259),
               (1.700, 2.159, 2.211), (1.719, 2.190, 2.219), (1.738, 2.201, 2.201))
    rng = random.Random(SEED + 149)
    opening = _chipped_opening(profile, rng)
    upper_rng = random.Random(SEED + 150)
    upper_profile = ((2.024, 3.303, 3.303), (2.046, 3.268, 3.315), (2.065, 3.263, 3.333),
                     (2.091, 3.227, 3.369), (2.116, 3.246, 3.411), (2.154, 3.229, 3.441),
                     (2.179, 3.248, 3.429), (2.204, 3.244, 3.457), (2.238, 3.278, 3.446),
                     (2.268, 3.289, 3.421), (2.292, 3.307, 3.429), (2.318, 3.310, 3.402),
                     (2.338, 3.342, 3.382), (2.361, 3.348, 3.365), (2.373, 3.355, 3.355))
    upper = _chipped_opening(upper_profile, upper_rng)
    rows = ((0, opening[0][1], opening[0][2]), *opening, *upper, (HEIGHT, upper[-1][1], upper[-1][2]))
    vertex_ids = {}

    def vertex(y, z):
        return vertex_ids.setdefault((HALF, y, z), len(vertex_ids))

    faces = []
    for (z0, a0, b0), (z1, a1, b1) in pairwise(rows):
        faces.append((vertex(0, z0), vertex(0, z1), vertex(a1, z1), vertex(a0, z0)))
        faces.append((vertex(b0, z0), vertex(b1, z1), vertex(DEPTH, z1), vertex(DEPTH, z0)))
    wall = prim._mesh("Right wall with torn wallpaper opening", list(vertex_ids), faces, paper)
    for polygon in wall.data.polygons:
        polygon.use_smooth = False

    outline, masonry_faces, bevel_faces = _opening_topology(opening)
    center_y, center_z = 2.194, 1.456
    paper_glue = prim._material("Grey browned glue and paper backing", (0.235, 0.177, 0.126), roughness=0.96)
    broken = prim._material("Broken lime and dust in cut plaster edge", (0.312, 0.270, 0.216), roughness=0.96)
    for material in (paper_glue, broken):
        prim._surface_detail(material, (48, 54, 63), strength=0.50, distance=0.003, dark_level=0.35)
    _stains(prim, paper_glue, scale=170, low=(0.36, 0.30, 0.24))
    _stains(prim, broken, scale=230, low=(0.15, 0.14, 0.12))
    outer = [(HALF, y, z) for y, z in outline]
    backing, exposed, depths = [], [], []
    for y, z in outline:
        depth = 0.019 + 0.0026 * math.sin(z * 38 + y * 7.2) + rng.uniform(-0.00135, 0.00135)
        width = (0.84 if y < center_y else 0.945) + 0.022 * math.sin(z * 71 + y * 11) + rng.uniform(-0.028, 0.023)
        height = 0.970 + 0.002 * math.cos(z * 49 + y * 3.9)
        lip = rng.uniform(0.03, 0.15)
        rim_y = (y - center_y) * (width - 1)
        rim_z = (z - center_z) * (height - 1)
        backing.append((HALF + depth * lip, y + rim_y * lip, z + rim_z * lip))
        exposed.append((HALF + depth, y + rim_y, z + rim_z))
        depths.append(depth)
    prim._mesh("Frayed paper edge, set into the wall", outer + backing, bevel_faces, paper_glue)
    prim._mesh("Recessed plaster chip bevel", backing + exposed, bevel_faces, broken)
    masonry, report = _recess_masonry(prim, brick)
    prim._mesh("Recessed masonry face", exposed, masonry_faces, masonry)
    prim._box("Solid masonry backing of right wall", (HALF + 0.079, DEPTH / 2, HEIGHT / 2),
              (0.082, DEPTH, HEIGHT), broken)
    cracks = (("Tapered crack above the opening", ((2.201, 1.738), (2.216, 1.770), (2.208, 1.803),
                (2.230, 1.846), (2.226, 1.876), (2.239, 1.917)), 0.00115),
              ("Fine branch in the wallpaper", ((2.209, 1.800), (2.189, 1.815), (2.184, 1.827),
                (2.167, 1.844)), 0.00061),
              ("Tapered crack below the opening", ((2.214, 1.195), (2.224, 1.179), (2.218, 1.165),
                (2.227, 1.151), (2.227, 1.134), (2.235, 1.123)), 0.00096))
    for name, points, width in cracks:
        _tapered_fracture(prim, paper_glue, name, points, width, rng)
    tear = _upper_wall_tear(prim, paper, paper_glue, upper, upper_rng)
    return {"depth_range_m": [round(min(depths), 5), round(max(depths), 5)], "texture": report,
            "upper_wallpaper_tear": tear}


def _shell(prim, rng, wallpaper, brick):
    (left_paper, right_paper, rear_paper), report = _wallpaper_surfaces(prim, wallpaper)
    wash = prim._material("Dirty limewashed ceiling", (0.236, 0.208, 0.180), roughness=0.97)
    _stains(prim, wash, scale=2.6, low=(0.56, 0.49, 0.41))
    trim = prim._material("Old brown paint on wood trim", (0.145, 0.109, 0.074), roughness=0.77)
    floor_dark = prim._material("Exposed blackened subfloor", (0.058, 0.034, 0.018), roughness=0.96)
    planks = [prim._material(f"Mottled floorboard {i + 1}", (v, v * 0.51, v * 0.29), roughness=0.32)
              for i, v in enumerate((0.086, 0.105, 0.139, 0.166, 0.187, 0.221))]
    for plank in planks:
        prim._surface_detail(plank, (62, 2.4, 14), strength=0.22, distance=0.005, dark_level=0.24)
        _stains(prim, plank, scale=2.2, low=(0.21, 0.18, 0.15))
        _floor_sheen(plank)

    box = prim._box
    box("Subfloor", (0, DEPTH / 2, -0.055), (WIDTH, DEPTH, 0.06), floor_dark)
    for index in range(23):
        cursor = 0.0
        x = -HALF + (index + 0.5) * WIDTH / 23
        while cursor < DEPTH - 0.005:
            length = min(rng.uniform(0.29, 1.18), DEPTH - cursor)
            if length > 0.015:
                box("Worn tongue-and-groove boards", (x, cursor + length / 2, -0.017),
                    (WIDTH / 23 - 0.002, length - 0.002, 0.034), rng.choice(planks), bevel=0.001)
            cursor += length
    box("Left wall", (-HALF - 0.06, DEPTH / 2, HEIGHT / 2), (0.12, DEPTH, HEIGHT), left_paper)
    masonry = _right_wall(prim, right_paper, brick)
    box("Ceiling", (0, DEPTH / 2, HEIGHT + 0.06), (WIDTH, DEPTH, 0.12), wash)
    side = (WIDTH - WINDOW_WIDTH) / 2
    for sign in (-1, 1):
        box("Rear window side wall", (sign * (WINDOW_WIDTH + side) / 2, DEPTH + 0.06, HEIGHT / 2),
            (side, 0.12, HEIGHT), rear_paper)
        box("Low wood baseboard", (sign * (HALF - 0.018), DEPTH / 2, 0.048), (0.036, DEPTH, 0.096), trim, bevel=0.004)
        box("Side wall narrow dado rail", (sign * (HALF - 0.014), DEPTH / 2, 0.98), (0.029, DEPTH, 0.038), trim, bevel=0.006)
        box("Side ceiling cornice", (sign * (HALF - 0.027), DEPTH / 2, HEIGHT - 0.049), (0.055, DEPTH, 0.098), wash, bevel=0.018)
    box("Rear lower wall", (0, DEPTH + 0.06, SILL / 2), (WINDOW_WIDTH, 0.12, SILL), rear_paper)
    box("Window masonry head", (0, DEPTH + 0.06, (HEAD + HEIGHT) / 2), (WINDOW_WIDTH, 0.12, HEIGHT - HEAD), rear_paper)
    box("Rear ceiling cornice", (0, DEPTH - 0.027, HEIGHT - 0.049), (WIDTH, 0.055, 0.098), wash, bevel=0.018)
    box("Rear kickboard", (0, DEPTH - 0.014, 0.048), (WIDTH, 0.029, 0.096), trim, bevel=0.004)
    return report, masonry


def _damage(prim, rng):
    mortar = prim._material("Dark wall cavities and crumbling mortar", (0.12, 0.090, 0.064), roughness=0.99)
    plaster = prim._material("Pale broken lime plaster", (0.40, 0.35, 0.28), roughness=0.97)
    torn = prim._material("Curls of paper and dry plaster", (0.31, 0.24, 0.16), roughness=0.96)
    brick = prim._material("Reddish exposed kiln brick", (0.23, 0.108, 0.066), roughness=0.97)
    lath = prim._material("Old splintered lath", (0.24, 0.13, 0.05), roughness=0.94)
    for material in (mortar, plaster, torn, brick, lath):
        prim._surface_detail(material, (15, 19, 13), strength=0.58, distance=0.014, dark_level=0.48)

    def patch(sign, y, z, width, height, core, *, visible=True):
        def point(u, v, depth):
            return (sign * (HALF - depth), y + u, z + v)

        outline, lining = [], []
        phase = rng.uniform(0, math.tau)
        for item in range(40):
            angle = math.tau * item / 40
            edge = (0.89 + 0.15 * math.sin(3 * angle + phase) + 0.09 * math.sin(5 * angle - phase)
                    + 0.065 * math.sin(9 * angle + phase) + rng.uniform(-0.038, 0.038))
            u, v = width * math.cos(angle) * edge, height * math.sin(angle) * edge
            outline.append((u, v))
            lining.append(0.70 + 0.11 * math.sin(4 * angle - phase))
        lifts = [rng.uniform(0.010, 0.022) for _ in outline[::3]]
        if not visible:
            return
        boundary = [point(u, v, 0.003) for u, v in outline]
        faces = [(0, i + 1, (i + 1) % len(outline) + 1) for i in range(len(outline))]
        prim._mesh("Flaked wallpaper plaster break", [point(0, 0, 0.004), *boundary], faces, plaster)
        inner = [point(u * depth, v * depth, 0.008) for (u, v), depth in zip(outline, lining, strict=True)]
        prim._mesh("Exposed damaged wall pocket", [point(0, 0, 0.009), *inner], faces, mortar)
        for index, ((u1, v1), (u2, v2)) in enumerate(zip(outline, outline[1:] + outline[:1], strict=True)):
            if index % 3 == 0:
                prim._mesh("Raised peeling wallpaper edge", [point(u1, v1, 0.007), point(u2, v2, 0.006),
                           point((u1 + u2) * 0.43, (v1 + v2) * 0.40, lifts[index // 3])],
                           [(0, 1, 2)], torn)
        if core == "brick":
            for row in range(3):
                count = 2 if row == 1 else 1
                for column in range(count):
                    u = (column - (count - 1) / 2) * width * 0.56
                    v = height * (row - 1) * 0.33
                    corners = ((-0.5, -0.26), (-0.31, -0.5), (0.36, -0.43), (0.49, -0.13),
                               (0.43, 0.25), (0.23, 0.47), (-0.37, 0.43), (-0.52, 0.12))
                    chipped = [point(u + (x + rng.uniform(-0.09, 0.07)) * width * 0.56,
                                     v + (z + rng.uniform(-0.08, 0.08)) * height * 0.25,
                                     rng.uniform(0.011, 0.015)) for x, z in corners]
                    prim._mesh("Eroded brick face behind plaster", chipped, [tuple(range(len(corners)))], brick)
        elif core == "lath":
            for row in range(-2, 3):
                prim._box("Split wood strip behind plaster", point(0, height * row * 0.24, 0.013),
                          (0.014, width * (1.25 - 0.12 * abs(row)), height * 0.13), lath, bevel=0.002)

    for args in ((-1, 1.22, 1.64, 0.30, 0.39, "lath"), (-1, 2.87, 0.29, 0.31, 0.19, "brick"),
                 (1, 3.33, 2.19, 0.18, 0.20, "paper"),
                 (-1, 0.92, 0.28, 0.15, 0.14, "paper")):
        # Keep the original random draws for this old patch so the room clutter is reproducible.
        patch(*args, visible=args[0] < 0)
    points = [(-HALF + 0.014, 1.07 + rng.uniform(-0.05, 0.05) + 0.21 * i / 18, 2.47 - i * 0.078)
              for i in range(19)]
    prim._line("Tall branching structural crack", points, mortar, 0.007)
    prim._line("Tapered crack branch", [points[7], (-HALF + 0.008, 1.40, 1.88), (-HALF + 0.009, 1.49, 1.83)], mortar, 0.003)
    return mortar, plaster, torn, brick, lath


def _facade_plate(prim, binding, night):
    image, receipt = _packed_png(prim, binding, name="Original overcast neighboring facade", saved_path="//facade.png")
    distance = 4.62
    pane_y = DEPTH + distance
    cam_x, cam_y, cam_z = CAMERA_LOCATION
    view_scale = (pane_y - cam_y) / (DEPTH - cam_y)
    center_x = cam_x * (1 - view_scale) + 0.71
    center_z = cam_z + ((SILL + HEAD) * 0.5 - cam_z) * view_scale + 0.10
    aspect = image.size[0] / image.size[1]
    canvas_width = max(WINDOW_WIDTH * view_scale + 2.0, ((HEAD - SILL) * view_scale + 0.62) * aspect)
    canvas_height = canvas_width / aspect
    left, right = center_x - canvas_width / 2, center_x + canvas_width / 2
    low, high = center_z - canvas_height / 2, center_z + canvas_height / 2

    material = prim._material("Neighbor facade photographic plate", (0.34, 0.32, 0.29), roughness=1)
    tree = material.node_tree
    output = tree.nodes.get("Material Output")
    rock = tree.nodes.get("Principled BSDF")
    coordinates = tree.nodes.new("ShaderNodeTexCoord")
    axes = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(coordinates.outputs["Generated"], axes.inputs["Vector"])
    uv = tree.nodes.new("ShaderNodeCombineXYZ")
    tree.links.new(axes.outputs["X"], uv.inputs["X"])
    tree.links.new(axes.outputs["Z"], uv.inputs["Y"])
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.extension = "EXTEND"
    tree.links.new(uv.outputs["Vector"], texture.inputs["Vector"])
    color = tree.nodes.new("ShaderNodeEmission")
    color.inputs["Strength"].default_value = 0.077 if night else 1.14
    image_color = texture.outputs["Color"]
    if night:
        moonlit = tree.nodes.new("ShaderNodeMixRGB")
        moonlit.label = "Cool shade on the unlit courtyard facade"
        moonlit.blend_type = "MULTIPLY"
        moonlit.inputs[0].default_value = 1
        moonlit.inputs[2].default_value = (0.46, 0.65, 1.0, 1)
        tree.links.new(image_color, moonlit.inputs[1])
        image_color = moonlit.outputs["Color"]
    tree.links.new(image_color, color.inputs["Color"])
    light_path = tree.nodes.new("ShaderNodeLightPath")
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.label = "Show photo through glass while lighting stays in the scene"
    tree.links.new(light_path.outputs["Is Camera Ray"], mix.inputs[0])
    tree.links.new(rock.outputs["BSDF"], mix.inputs[1])
    tree.links.new(color.outputs["Emission"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], output.inputs["Surface"])

    prim._mesh("Neighbor facade at courtyard depth", [(left, pane_y, low), (right, pane_y, low),
               (right, pane_y, high), (left, pane_y, high)], [(0, 1, 2, 3)], material)
    return {**receipt, "depth_beyond_window_m": distance, "dimensions_m": [canvas_width, canvas_height],
            "night": night}


def _window_noise(tree, position, scale, detail):
    stretch = tree.nodes.new("ShaderNodeVectorMath")
    stretch.operation = "MULTIPLY"
    stretch.inputs[1].default_value = scale
    texture = tree.nodes.new("ShaderNodeTexNoise")
    texture.inputs["Scale"].default_value = 1
    texture.inputs["Detail"].default_value = detail
    texture.inputs["Roughness"].default_value = 0.66
    tree.links.new(position, stretch.inputs[0])
    tree.links.new(stretch.outputs["Vector"], texture.inputs["Vector"])
    return texture.outputs["Fac"]


def _window_paint(prim, name, grain_scale, *, worn=False):
    material = prim._material(name, (0.51, 0.49, 0.443), roughness=0.65)
    tree = material.node_tree
    shader = tree.nodes.get("Principled BSDF")
    position = tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    chips = _window_noise(tree, position, (27, 35, 42), 4)
    grain = _window_noise(tree, position, grain_scale, 3)
    color = tree.nodes.new("ShaderNodeValToRGB")
    color.label = "Satin paint, yellowed undercoat and isolated bare wood"
    ramp = color.color_ramp
    ramp.elements[0].position = 0.25
    ramp.elements[0].color = (0.332, 0.322, 0.288, 1)
    ramp.elements[1].position = 0.58
    ramp.elements[1].color = (0.535, 0.518, 0.461, 1)
    fail = 0.676 if worn else 0.728
    ramp.elements.new(fail).color = (0.472, 0.441, 0.354, 1)
    ramp.elements.new(fail + 0.037).color = (0.286, 0.234, 0.159, 1)
    ramp.elements.new(fail + 0.080).color = (0.158, 0.103, 0.057, 1)
    tree.links.new(chips, color.inputs["Fac"])
    tree.links.new(color.outputs["Color"], shader.inputs["Base Color"])

    grain_bump = tree.nodes.new("ShaderNodeBump")
    grain_bump.inputs["Strength"].default_value = 0.25 if worn else 0.16
    grain_bump.inputs["Distance"].default_value = 0.0018
    tree.links.new(grain, grain_bump.inputs["Height"])
    paint_bump = tree.nodes.new("ShaderNodeBump")
    paint_bump.inputs["Strength"].default_value = 0.27
    paint_bump.inputs["Distance"].default_value = 0.0034
    tree.links.new(grain_bump.outputs["Normal"], paint_bump.inputs["Normal"])
    tree.links.new(color.outputs["Color"], paint_bump.inputs["Height"])
    tree.links.new(paint_bump.outputs["Normal"], shader.inputs["Normal"])
    roughness = tree.nodes.new("ShaderNodeMapRange")
    roughness.clamp = True
    for label, value in (("From Min", 0.32), ("From Max", 0.76), ("To Min", 0.48), ("To Max", 0.83)):
        roughness.inputs[label].default_value = value
    tree.links.new(chips, roughness.inputs["Value"])
    tree.links.new(roughness.outputs["Result"], shader.inputs["Roughness"])
    return material


def _window_glass(prim):
    material = prim._material("Old clear glass with faint edge residue", (0.91, 0.90, 0.86), roughness=0.11, metal=1)
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    reflected = nodes.get("Principled BSDF")
    transmitted = nodes.new("ShaderNodeBsdfTransparent")
    fresnel = nodes.new("ShaderNodeFresnel")
    fresnel.inputs["IOR"].default_value = 1.51
    pane = nodes.new("ShaderNodeMixShader")
    pane.label = "Thin clear pane with weak physical Fresnel reflection"
    links.new(fresnel.outputs["Fac"], pane.inputs[0])
    links.new(transmitted.outputs["BSDF"], pane.inputs[1])
    links.new(reflected.outputs["BSDF"], pane.inputs[2])
    links.new(pane.outputs["Shader"], nodes.get("Material Output").inputs["Surface"])

    generated = nodes.new("ShaderNodeTexCoord").outputs["Generated"]
    centered = nodes.new("ShaderNodeVectorMath")
    centered.operation = "SUBTRACT"
    centered.inputs[1].default_value = (0.5, 0.5, 0.5)
    links.new(generated, centered.inputs[0])
    absolute = nodes.new("ShaderNodeVectorMath")
    absolute.operation = "ABSOLUTE"
    links.new(centered.outputs["Vector"], absolute.inputs[0])
    axes = nodes.new("ShaderNodeSeparateXYZ")
    links.new(absolute.outputs["Vector"], axes.inputs["Vector"])
    edge = nodes.new("ShaderNodeMath")
    edge.operation = "MAXIMUM"
    links.new(axes.outputs["X"], edge.inputs[0])
    links.new(axes.outputs["Z"], edge.inputs[1])
    edge_amount = nodes.new("ShaderNodeMapRange")
    edge_amount.clamp = True
    edge_amount.inputs["From Min"].default_value = 0.34
    edge_amount.inputs["From Max"].default_value = 0.49
    links.new(edge.outputs[0], edge_amount.inputs["Value"])
    position = nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    streaks = _window_noise(tree, position, (38, 4, 6), 3)
    grime = nodes.new("ShaderNodeMath")
    grime.operation = "MULTIPLY"
    links.new(streaks, grime.inputs[0])
    links.new(edge_amount.outputs["Result"], grime.inputs[1])
    blur = nodes.new("ShaderNodeMapRange")
    blur.inputs["To Min"].default_value = 0.10
    blur.inputs["To Max"].default_value = 0.43
    links.new(grime.outputs[0], blur.inputs["Value"])
    links.new(blur.outputs["Result"], reflected.inputs["Roughness"])
    return material


def _window(prim, facade, night):
    box = prim._box
    stiles = _window_paint(prim, "Dull layered off-white paint on upright sash wood", (127, 81, 5))
    rails = _window_paint(prim, "Dull layered off-white paint on sash rails", (5, 81, 127))
    wet_paint = _window_paint(prim, "Flaking lower rail and stool, long wood grain", (4, 94, 147), worn=True)
    bead = _window_paint(prim, "Window stops and worn glazing bead", (45, 45, 46))
    putty = _stains(prim, prim._material("Dirty yellowed glazing putty", (0.27, 0.247, 0.191), roughness=0.91),
                   scale=72, low=(0.44, 0.40, 0.34))
    seam = prim._material("Dirt trapped in window rebates", (0.108, 0.083, 0.058), roughness=0.93)
    latch = _stains(prim, prim._material("Patinated brass casement hardware", (0.273, 0.184, 0.089),
                                        roughness=0.47, metal=0.73), scale=106, low=(0.45, 0.43, 0.38))
    hinge = prim._material("Oxidized steel hinge pin and screws", (0.119, 0.096, 0.068), roughness=0.53, metal=0.61)
    glass = _window_glass(prim)
    sash_y = DEPTH - 0.01
    middle_z = (HEAD + SILL) / 2
    jamb = WINDOW_WIDTH / 2 - 0.026
    outer_stile = jamb - 0.048
    meeting_stile = 0.027
    rail_width = 0.056
    lower_rail, cross_rail, upper_rail = SILL + 0.048, 1.62, HEAD - 0.044
    for sign in (-1, 1):
        box("Repainted wood lining", (sign * jamb, sash_y + 0.012, middle_z),
            (0.052, 0.205, HEAD - SILL), stiles, bevel=0.004)
        box("Narrow shadow seam at casement", (sign * (jamb - 0.025), sash_y - 0.045, middle_z),
            (0.005, 0.010, HEAD - SILL - 0.08), seam, bevel=0.001)
        box("Painted movable casement stile", (sign * outer_stile, sash_y + 0.016, middle_z),
            (0.052, 0.117, HEAD - SILL - 0.048), stiles, bevel=0.0035)
        box("Raised meeting stile", (sign * meeting_stile, sash_y - 0.014, middle_z),
            (0.023, 0.098, HEAD - SILL - 0.047), stiles, bevel=0.0028)
    box("Slim fixed top lining", (0, sash_y + 0.012, HEAD - 0.014),
        (2 * jamb, 0.205, 0.028), rails, bevel=0.003)
    for z, material in ((lower_rail, wet_paint), (upper_rail, rails), (cross_rail, rails)):
        box("Painted horizontal sash rail", (0, sash_y + 0.025, z),
            (2 * outer_stile, 0.115, rail_width), material, bevel=0.0038)
    box("Recessed mullion behind meeting stiles", (0, sash_y + 0.029, middle_z),
        (0.032, 0.103, HEAD - SILL - 0.047), stiles, bevel=0.0025)
    box("Thin wooden window stool with worn bullnose", (0, sash_y - 0.093, SILL),
        (WINDOW_WIDTH + 0.052, 0.23, 0.046), wet_paint, bevel=0.006)
    box("Dark joint at back of weathered stool", (0, sash_y + 0.018, SILL + 0.023),
        (WINDOW_WIDTH - 0.065, 0.008, 0.003), seam, bevel=0.0008)

    half_rail = rail_width / 2
    pane_openings = ((lower_rail + half_rail, cross_rail - half_rail),
                     (cross_rail + half_rail, upper_rail - half_rail))
    for sign in (-1, 1):
        left, right = sorted((sign * (meeting_stile + 0.013), sign * (outer_stile - 0.026)))
        pane_left, pane_right = left - 0.006, right + 0.006
        for low, high in pane_openings:
            mid = (low + high) / 2
            span = right - left
            for x in (left, right):
                box("Setback vertical glazing bead", (x, sash_y + 0.044, mid),
                    (0.012, 0.034, high - low), bead, bevel=0.0022)
            for z in (low, high):
                box("Setback horizontal glazing bead", ((left + right) / 2, sash_y + 0.044, z),
                    (span, 0.034, 0.012), bead, bevel=0.0022)
            stopper_y, pane_y = sash_y + 0.068, sash_y + 0.073
            for x in (left + 0.006, right - 0.006):
                box("Recessed vertical putty seam", (x, stopper_y, mid), (0.007, 0.013, high - low), putty, bevel=0.002)
            for z in (low + 0.006, high - 0.006):
                box("Recessed horizontal putty seam", ((left + right) / 2, stopper_y, z),
                    (span - 0.014, 0.013, 0.007), putty, bevel=0.002)
            prim._mesh("Thin single pane in sash rabbet", [(pane_left, pane_y, low - 0.008),
                       (pane_right, pane_y, low - 0.008), (pane_right, pane_y, high + 0.008),
                       (pane_left, pane_y, high + 0.008)], [(0, 1, 2, 3)], glass)

        handle_x, handle_y, handle_z = sign * meeting_stile, sash_y - 0.066, 1.365 + sign * 0.009
        box("Old brass handle escutcheon", (handle_x, handle_y, handle_z),
            (0.019, 0.010, 0.063), latch, bevel=0.0026)
        prim._line("Bent brass casement handle", [(handle_x, handle_y - 0.004, handle_z + 0.006),
                   (handle_x, handle_y - 0.028, handle_z + 0.006),
                   (handle_x + sign * 0.011, handle_y - 0.033, handle_z - 0.011),
                   (handle_x + sign * 0.030, handle_y - 0.027, handle_z - 0.020)], latch, 0.0063)
        for z in (handle_z - 0.024, handle_z + 0.025):
            box("Dark single-slot escutcheon screw", (handle_x, handle_y - 0.0055, z),
                (0.006, 0.002, 0.005), hinge, bevel=0.001)
        for z in (0.93, 1.47):
            x = sign * (outer_stile + 0.018)
            box("Dark hinge leaf across casement stile", (x, sash_y - 0.049, z),
                (0.030, 0.008, 0.036), hinge, bevel=0.002)
            prim._line("Rounded hinge pin barrel", [(x + sign * 0.012, sash_y - 0.057, z - 0.027),
                       (x + sign * 0.012, sash_y - 0.057, z + 0.027)], hinge, 0.0055)

    if facade is not None:
        return _facade_plate(prim, facade, night)

    wall = prim._material("Exterior weathered apartment sandstone", (0.71, 0.60, 0.45), roughness=0.92)
    masonry = prim._material("Exterior stone surrounds", (0.77, 0.71, 0.58), roughness=0.89)
    window_tone = prim._material("Opposite apartment windows", (0.30, 0.34, 0.31), roughness=0.76)
    facade_y = DEPTH + 1.27
    box("Exterior apartment facade seen through closed window", (0.10, facade_y, 0.90),
        (3.8, 0.12, 3.48), _stains(prim, wall, scale=6, low=(0.52, 0.45, 0.37)))
    for row in range(4):
        z = -0.27 + row * 0.72
        for x in (-0.82, -0.04, 0.73):
            box("Apartment window painted surround", (x, facade_y - 0.071, z), (0.45, 0.046, 0.46), masonry, bevel=0.007)
            box("Apartment window dark panes", (x, facade_y - 0.099, z), (0.365, 0.026, 0.36), window_tone)
            box("Apartment window vertical bar", (x, facade_y - 0.122, z), (0.026, 0.018, 0.365), masonry)
        x = 0.70
        box("Neighbor balcony concrete slab", (x, facade_y - 0.16, z - 0.22), (0.69, 0.31, 0.063), masonry)
        for index in range(6):
            box("Weathered exterior balcony rail", (x - 0.30 + index * 0.12, facade_y - 0.287, z - 0.095),
                (0.012, 0.016, 0.24), window_tone)
        box("Exterior balcony cap rail", (x, facade_y - 0.287, z + 0.025), (0.64, 0.024, 0.025), window_tone)
    return None


def _curtain_panel(prim, side, rod_y, rod_z, cloth, hem, iron):
    # Measured cloth coordinates run from the wall-side selvage to the parted edge.
    # Closely gathered pleats fan out and carry their weight to a softly uneven hem.
    def point(u, v):
        outer = 0.762 + 0.019 * v + 0.007 * math.sin(6.2 * v + side)
        inner = 0.455 - 0.041 * v + 0.011 * math.sin(4.3 * v + 0.8 * side)
        x = outer + (inner - outer) * u + 0.008 * v * math.sin(9.7 * u + 5.3 * v + side)
        phase = math.tau * (4.48 * u + 0.11 * math.sin(9.2 * u + side) + 0.12 * v * math.sin(5.8 * u))
        fold = (0.027 + 0.013 * v) * (0.75 + 0.23 * math.sin(9.8 * u + side))
        y = rod_y + 0.004 + fold * math.sin(phase + 0.24 * side)
        y += 0.006 * v * math.sin(13 * u - 4.2 * v + side)
        z = rod_z - 0.049 - (rod_z - SILL + 0.099 + 0.018 * side) * v
        z -= 0.012 * math.sin(math.pi * v) * math.sin(11.5 * u + side)
        z += 0.015 * v * v * math.sin(phase + 0.67 * side)
        z -= 0.010 * (1 - v) ** 10 * math.sin(phase) ** 2
        return (side * x, y, z)

    columns, rows = 75, 44
    vertices = [point(i / (columns - 1), j / (rows - 1)) for j in range(rows) for i in range(columns)]
    faces = []
    for j in range(rows - 1):
        for i in range(columns - 1):
            a = j * columns + i
            face = (a, a + 1, a + 1 + columns, a + columns)
            faces.append(face if side > 0 else face[::-1])
    panel = prim._mesh("Parted gray-olive window curtain", vertices, faces, cloth)
    thickness = panel.modifiers.new("Woven cloth thickness", "SOLIDIFY")
    thickness.thickness = 0.00135
    for u in (0.011, 0.989):
        prim._line("Turned curtain selvage", [point(u, j / (rows - 1)) for j in range(rows)], hem, 0.00155)
    for v in (0.025, 0.982):
        prim._line("Folded curtain heading and hem", [point(i / (columns - 1), v) for i in range(columns)],
                   hem, 0.0019)
    for u in (0.038, 0.215, 0.407, 0.586, 0.769, 0.953):
        x, y, z = point(u, 0)
        ring_center_z = rod_z - 0.004
        ring = [(x, rod_y + 0.018 * math.cos(n * math.tau / 20),
                 ring_center_z + 0.019 * math.sin(n * math.tau / 20)) for n in range(20)]
        prim._line("Dark wire curtain ring", ring, iron, 0.00185, closed=True)
        prim._line("Short cloth loop through curtain ring", [(x, rod_y, ring_center_z - 0.018),
                   (x, (y + rod_y) / 2, z + 0.013), (x, y, z)], hem, 0.00225)


def _window_curtains(prim):
    cloth = prim._material("Dusty gray-olive curtain linen", (0.147, 0.158, 0.125), roughness=0.93, sheen=0.33)
    _stains(prim, cloth, scale=4.5, low=(0.53, 0.58, 0.51))
    tree = cloth.node_tree
    position = tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    warp = _window_noise(tree, position, (470, 470, 42), 2)
    weft = _window_noise(tree, position, (38, 38, 520), 2)
    cross = tree.nodes.new("ShaderNodeMath")
    cross.operation = "MULTIPLY"
    tree.links.new(warp, cross.inputs[0])
    tree.links.new(weft, cross.inputs[1])
    bump = tree.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.34
    bump.inputs["Distance"].default_value = 0.00075
    tree.links.new(cross.outputs[0], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], tree.nodes.get("Principled BSDF").inputs["Normal"])
    hem = prim._material("Dark folded and stitched linen edging", (0.095, 0.103, 0.082), roughness=0.93, sheen=0.29)
    iron = _stains(prim, prim._material("Oxidized dark curtain pole and fittings", (0.090, 0.075, 0.056),
                                       roughness=0.58, metal=0.72), scale=56, low=(0.40, 0.39, 0.35))
    rod_y, rod_z = DEPTH - 0.310, HEAD + 0.149
    end_x = 0.845
    prim._line("Slim dark curtain pole", [(-end_x, rod_y, rod_z), (end_x, rod_y, rod_z)], iron, 0.0095)
    for side in (-1, 1):
        x = side * 0.803
        prim._box("Rear-wall curtain-pole mounting plate", (x, DEPTH - 0.007, rod_z - 0.029),
                  (0.025, 0.017, 0.070), iron, bevel=0.003)
        prim._line("Curved curtain-pole support", [(x, DEPTH - 0.018, rod_z - 0.040),
                   (x, DEPTH - 0.050, rod_z - 0.023), (x, rod_y + 0.050, rod_z - 0.023),
                   (x, rod_y + 0.010, rod_z - 0.014), (x, rod_y, rod_z)], iron, 0.0065)
        prim._line("Rounded curtain-pole end", [(side * (end_x - 0.019), rod_y, rod_z),
                   (side * end_x, rod_y, rod_z)], iron, 0.014)
        _curtain_panel(prim, side, rod_y, rod_z, cloth, hem, iron)


def _weathered_wood(prim, material):
    tree = material.node_tree
    shader = next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if shader is None or not shader.inputs["Base Color"].links:
        return
    base = shader.inputs["Base Color"]
    muted = tree.nodes.new("ShaderNodeHueSaturation")
    muted.label = "Faded varnish over original wood and metal texture"
    muted.inputs["Saturation"].default_value = 0.54
    muted.inputs["Value"].default_value = 0.68
    tree.links.new(base.links[0].from_socket, muted.inputs["Color"])
    tree.links.new(muted.outputs["Color"], base)
    _stains(prim, material, scale=7.5, low=(0.31, 0.36, 0.38))

    position = tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"]
    abrasion = tree.nodes.new("ShaderNodeTexNoise")
    abrasion.label = "Fine grain, dust and worn finish"
    abrasion.inputs["Scale"].default_value = 76
    abrasion.inputs["Detail"].default_value = 4
    tree.links.new(position, abrasion.inputs["Vector"])
    weather = tree.nodes.new("ShaderNodeMapRange")
    weather.inputs["From Min"].default_value = 0.25
    weather.inputs["From Max"].default_value = 0.75
    weather.inputs["To Min"].default_value = 0.38
    weather.inputs["To Max"].default_value = 0.83
    tree.links.new(abrasion.outputs["Fac"], weather.inputs["Value"])
    roughness = shader.inputs["Roughness"]
    blend = tree.nodes.new("ShaderNodeMixRGB")
    blend.inputs[0].default_value = 0.79
    if roughness.links:
        tree.links.new(roughness.links[0].from_socket, blend.inputs[1])
    else:
        blend.inputs[1].default_value = (roughness.default_value,) * 3 + (1,)
    tree.links.new(weather.outputs["Result"], blend.inputs[2])
    tree.links.new(blend.outputs["Color"], roughness)



def _cabinet_matfuse_finish(prim, root, meshes, binding) -> dict:
    helpers = _matfuse_helpers(Path(binding["helper"]))
    context = prim.bpy.context
    actual_rotation = root.rotation_euler.copy()
    actual_front = actual_rotation.to_matrix() @ prim.Vector((0, 1, 0))
    original_bounds = prim._world_bounds(meshes)

    # Reuse the bench projection without a second shader implementation. The
    # RealGen face is +Y at import and therefore -Y in the canonical 180? pose.
    root.rotation_euler = (0, 0, math.pi)
    try:
        context.view_layer.update()
        width, height = helpers["project_front_uv"](meshes, binding["normal"]["size_px"])
    finally:
        root.rotation_euler = actual_rotation
        context.view_layer.update()
    if any((first - second).length > 1e-5 for first, second in
           zip(original_bounds, prim._world_bounds(meshes), strict=True)):
        raise RuntimeError("MatFuse UV projection moved the fitted cabinet")

    materials = {mat for mesh in meshes for mat in mesh.data.materials if mat and mat.node_tree}
    if not materials:
        raise RuntimeError("The MatFuse cabinet has no imported material to preserve")
    mask = Path(binding["mask"]["path"])
    for material in materials:
        nodes = material.node_tree.nodes
        shader = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
        original = {name: tuple(link.from_socket for link in shader.inputs[name].links)
                    for name in ("Base Color", "Metallic")}
        helpers["add_wood_detail"](material, mask, binding)
        (front,) = (node for node in nodes if node.label == "Compare true normal to cabinet front")
        front.inputs[1].default_value = actual_front
        if any(tuple(link.from_socket for link in shader.inputs[name].links) != links
               for name, links in original.items()):
            raise RuntimeError("MatFuse changed the original cabinet base color or metal inputs")
    return {
        "bench_helper_sha256": MATFUSE_STUDIO_SHA256,
        "normal_convention": "OpenGL tangent (+V green) assumed",
        "preview_aligned_uv_extent_m": [width, height],
        "front_gate_world_normal": list(actual_front),
        "maps": {role: {key: binding[role][key] for key in ("sha256", "size_px")}
                 for role in ("normal", "roughness", "mask")},
    }


def _prop_matfuse_finish(meshes, role, binding, load, bounds) -> dict:
    normal_strength, rough_min, rough_max, rough_weight, target = PROP_SURFACES[role]
    uv_names = {next((layer.name for layer in obj.data.uv_layers if layer.active_render), None) for obj in meshes}
    if None in uv_names or len(uv_names) != 1:
        raise RuntimeError(f"MatFuse {role} material needs a shared active GLB UV layer")
    (uv_name,) = uv_names
    materials = {mat for obj in meshes for mat in obj.data.materials if mat and mat.node_tree}
    if not materials:
        raise RuntimeError(f"The MatFuse {role} has no imported node material")
    images = {key: load(binding[key], f"{role}-matfuse-{key}") for key in ("normal", "roughness")}

    for material in materials:
        tree = material.node_tree
        nodes, links = tree.nodes, tree.links
        shader = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
        base, metal = shader.inputs["Base Color"], shader.inputs["Metallic"]
        original = [tuple(link.from_socket for link in socket.links) for socket in (base, metal)]

        def connect(value, socket, links=links):
            if isinstance(value, (int, float)):
                socket.default_value = value
            else:
                links.new(value, socket)

        def remap(label, value, limits, output, nodes=nodes):
            node = nodes.new("ShaderNodeMapRange")
            node.label = label
            node.clamp = True
            connect(value, node.inputs["Value"])
            for key, number in zip(("From Min", "From Max", "To Min", "To Max"), (*limits, *output), strict=True):
                node.inputs[key].default_value = number
            return node.outputs["Result"]

        def multiply(label, first, second, nodes=nodes):
            node = nodes.new("ShaderNodeMath")
            node.label = label
            node.operation = "MULTIPLY"
            connect(first, node.inputs[0])
            connect(second, node.inputs[1])
            return node.outputs[0]

        mask = 1.0
        if role == "dresser":
            metal_value = metal.links[0].from_socket if metal.is_linked else metal.default_value
            mask = remap("Protect exposed metal fittings", metal_value, (0.17, 0.71), (1, 0))
        elif role in {"doll", "rubbish"}:
            hsv = nodes.new("ShaderNodeSeparateColor")
            hsv.mode = "HSV"
            if not base.is_linked:
                raise RuntimeError(f"MatFuse {role} mask needs the imported color texture")
            links.new(base.links[0].from_socket, hsv.inputs[0])
            if role == "doll":
                hue = remap("Warm red-orange cloth", hsv.outputs[0], (0.099, 0.187), (1, 0))
                saturation = remap("Protect neutral cloth and dark soles", hsv.outputs[1], (0.35, 0.57), (0, 1))
                position = nodes.new("ShaderNodeNewGeometry")
                axis = nodes.new("ShaderNodeSeparateXYZ")
                links.new(position.outputs["Position"], axis.inputs["Vector"])
                low, high = bounds[0].z, bounds[1].z
                height = remap("Stop the cloth finish below the face", axis.outputs["Z"],
                               (low + 0.57 * (high - low), low + 0.71 * (high - low)), (1, 0))
                mask = multiply("Cloth hue and saturation", hue, saturation)
                mask = multiply("Warm coat only", mask, height)
            else:
                mask = remap("Leave dark items on the floor alone", hsv.outputs[2], (0.093, 0.27), (0, 1))

        uv = nodes.new("ShaderNodeUVMap")
        uv.label = "Original GLB tangent UV layer"
        uv.uv_map = uv_name
        maps = {}
        for key, image in images.items():
            texture = nodes.new("ShaderNodeTexImage")
            texture.label = f"Packed MatFuse {key}"
            texture.image = image
            texture.extension = "EXTEND"
            links.new(uv.outputs["UV"], texture.inputs["Vector"])
            maps[key] = texture.outputs["Color"]

        normal = shader.inputs["Normal"]
        tangent = nodes.new("ShaderNodeNormalMap")
        tangent.label = f"Subtle MatFuse {target}"
        tangent.space = "TANGENT"
        tangent.uv_map = uv_name
        links.new(maps["normal"], tangent.inputs["Color"])
        adjusted_normal = tangent.outputs["Normal"]
        if normal.is_linked:
            tangent.inputs["Strength"].default_value = normal_strength
            blend = nodes.new("ShaderNodeMixRGB")
            blend.label = "Keep imported normals outside the selected surface"
            connect(mask, blend.inputs[0])
            links.new(normal.links[0].from_socket, blend.inputs[1])
            links.new(adjusted_normal, blend.inputs[2])
            unit = nodes.new("ShaderNodeVectorMath")
            unit.operation = "NORMALIZE"
            links.new(blend.outputs["Color"], unit.inputs[0])
            adjusted_normal = unit.outputs["Vector"]
        else:
            connect(multiply("Masked relief strength", mask, normal_strength), tangent.inputs["Strength"])
        links.new(adjusted_normal, normal)

        roughness = shader.inputs["Roughness"]
        material_finish = remap("Restrained MatFuse roughness", maps["roughness"], (0, 1), (rough_min, rough_max))
        rough_blend = nodes.new("ShaderNodeMixRGB")
        rough_blend.label = "Keep original roughness under protected areas"
        connect(multiply("Masked roughness weight", mask, rough_weight), rough_blend.inputs[0])
        if roughness.is_linked:
            links.new(roughness.links[0].from_socket, rough_blend.inputs[1])
        else:
            rough_blend.inputs[1].default_value = (roughness.default_value,) * 3 + (1,)
        links.new(material_finish, rough_blend.inputs[2])
        links.new(rough_blend.outputs["Color"], roughness)
        if [tuple(link.from_socket for link in socket.links) for socket in (base, metal)] != original:
            raise RuntimeError(f"MatFuse unexpectedly changed {role} base-color or metallic inputs")
    return {"maps": {key: {field: binding[key][field] for field in ("sha256", "size_px")}
                     for key in ("normal", "roughness")},
            "uv": uv_name, "normal_convention": "OpenGL tangent (+V green) assumed",
            "normal_strength": normal_strength, "roughness_interval": [rough_min, rough_max],
            "roughness_weight": rough_weight, "surface": target}


def _clear_cabinet_dado(prim, meshes):
    rail = prim.bpy.data.objects["Side wall narrow dado rail"]
    if rail.location.x >= 0 or len(rail.data.materials) != 1:
        raise RuntimeError("Unable to locate the left dado rail behind the leaning cabinet")
    material = rail.data.materials[0]
    rail_inner_x = rail.location.x + rail.dimensions.x / 2
    rail_low_z = rail.location.z - rail.dimensions.z / 2
    rail_high_z = rail.location.z + rail.dimensions.z / 2
    intersecting_y = []
    for obj in meshes:
        points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
        for face in obj.data.polygons:
            corners = [points[index] for index in face.vertices]
            # Face bounds catch edges that cross the trim plane between vertices.
            if (min(point.x for point in corners) <= rail_inner_x
                    and min(point.z for point in corners) <= rail_high_z
                    and max(point.z for point in corners) >= rail_low_z):
                intersecting_y.extend(point.y for point in corners)
    if not intersecting_y:
        return None
    gap = (max(0.0, min(intersecting_y) - 0.006), min(DEPTH, max(intersecting_y) + 0.006))
    for label, start, end in (("entry", 0.0, gap[0]), ("window", gap[1], DEPTH)):
        if end - start > 0.012:
            prim._box(f"Left dado rail broken at cabinet, {label} section",
                      (rail.location.x, (start + end) / 2, rail.location.z),
                      (rail.dimensions.x, end - start, rail.dimensions.z), material, bevel=0.006)
    prim.bpy.data.objects.remove(rail, do_unlink=True)
    return gap


def _props(prim, worker, scene, plan):
    from mathutils import Euler

    surface_inputs = plan.get("prop_surfaces", {})
    load_map = _matfuse_helpers(Path(plan["prop_surface_helper"]))["load_pinned_map"] if surface_inputs else None
    applied_surfaces = {}

    def place(name, binding, center, dimensions, support=0.0, *, turn=0.0, lean=0.0,
              aisle=None, tone=None, wood=False, paper_smoothing=False, matfuse=None, surface_role=None,
              room_bounds=((-HALF, HALF), (0.0, DEPTH))):
        path = Path(binding["path"])
        prim._verify_sofa_file(path, binding["sha256"])
        yaw_deg = binding["yaw_deg"] + turn
        yaw = math.radians(yaw_deg)
        root, imported = worker._import_gltf_asset(prim.bpy, prim.Matrix, Euler, prim.Vector, scene, {
            "asset_id": name, "asset_path": str(path), "name": f"Generated {name}", "location": (0, 0, 0),
            "dimensions": (1, 1, 1), "rotation": (0, 0, yaw), "fit_mode": "contain",
        })
        prim._verify_sofa_file(path, binding["sha256"])
        if any(obj.type not in {"MESH", "EMPTY"} or obj.animation_data is not None for obj in imported):
            raise RuntimeError(f"The {name} GLB contains unexpected motion or non-geometry")
        mesh = [obj for obj in imported if obj.type == "MESH"]
        def measure():
            if not lean:
                return (*prim._world_bounds(mesh), [])
            points = [obj.matrix_world @ vertex.co for obj in mesh for vertex in obj.data.vertices]
            low, high = (prim.Vector(tuple(reduce(point[axis] for point in points) for axis in range(3)))
                         for reduce in (min, max))
            return low, high, points

        if lean:
            root.rotation_euler = (prim.Matrix.Rotation(lean, 3, "Y") @ root.rotation_euler.to_matrix()).to_euler()
            prim.bpy.context.view_layer.update()
        minimum, maximum, _ = measure()
        root.scale *= worker.asset_fit_factors(minimum, maximum, dimensions, fit_mode="contain")[0]
        prim.bpy.context.view_layer.update()
        minimum, maximum, _ = measure()
        (x_min, x_max), (y_min, y_max) = room_bounds
        left_contact_x = x_min + CABINET_WALL_CLEARANCE_M
        x_shift = left_contact_x - minimum.x if lean else center[0] - (minimum.x + maximum.x) / 2
        root.location += prim.Vector((x_shift, center[1] - (minimum.y + maximum.y) / 2, support - minimum.z))
        prim.bpy.context.view_layer.update()
        low, high, points = measure()
        if abs(low.z - support) > 0.001 or low.x <= x_min or high.x >= x_max or low.y < y_min or high.y >= y_max:
            raise RuntimeError(f"The fitted {name} intersects the wall or lost floor support")
        if aisle is not None and low.x < aisle and high.x > -aisle:
            raise RuntimeError(f"The fitted {name} intersects the center walking lane")
        lean_receipt = None
        if lean:
            near_wall = [point.z for point in points if point.x - low.x <= 0.002]
            contact_floor_gap = low.z - support
            if (abs(low.x - left_contact_x) > 0.0001
                    or min(near_wall) < support + (high.z - low.z) * 0.80):
                raise RuntimeError(f"The fitted {name} needs an upper edge against the left wall; adjust its yaw")
            if any(point.z < support + 0.096 and point.x < x_min + 0.036 for point in points):
                raise RuntimeError(f"The fitted {name} intersects the left baseboard; adjust its yaw")
            lean_receipt = {
                "lean_about_world_y_deg": math.degrees(lean),
                "wall_x_m": x_min, "wall_clearance_m": low.x - x_min,
                "wall_contact_height_m": [min(near_wall), max(near_wall)],
                "floor_clearance_m": contact_floor_gap,
                "center_aisle_clearance_m": -aisle - high.x if aisle is not None else None,
                "omitted_left_dado_y_m": _clear_cabinet_dado(prim, mesh),
            }
        smoothed = 0
        if paper_smoothing:
            for obj in mesh:
                data = obj.data
                if not data.has_custom_normals:
                    data.polygons.foreach_set("use_smooth", [True] * len(data.polygons))
                    data.set_sharp_from_angle(angle=math.radians(48))
                    smoothed += 1
        if wood:
            for material in {mat for obj in mesh for mat in obj.data.materials if mat and mat.node_tree}:
                _weathered_wood(prim, material)
        if tone is not None:
            for material in {mat for obj in mesh for mat in obj.data.materials if mat and mat.node_tree}:
                tree = material.node_tree
                shader = next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
                base = shader.inputs["Base Color"] if shader is not None else None
                if base is not None and base.links:
                    faded = tree.nodes.new("ShaderNodeMixRGB")
                    faded.label = "Dust-darkened plaster and paper"
                    faded.blend_type = "MULTIPLY"
                    faded.inputs[0].default_value = 1
                    faded.inputs[2].default_value = (*tone, 1)
                    tree.links.new(base.links[0].from_socket, faded.inputs[1])
                    tree.links.new(faded.outputs["Color"], base)
        finish = _cabinet_matfuse_finish(prim, root, mesh, matfuse) if matfuse is not None else None
        if surface_role in surface_inputs:
            surface = _prop_matfuse_finish(mesh, surface_role, surface_inputs[surface_role], load_map, (low, high))
            if surface_role not in applied_surfaces:
                applied_surfaces[surface_role] = {**surface, "placements": 0}
            applied_surfaces[surface_role]["placements"] += 1
        materials, textures = prim._sofa_textures(mesh)
        return {"sha256": binding["sha256"], "yaw_deg": yaw_deg, "bounds_m": [list(low), list(high)],
                "extent_m": list(high - low), "faces": sum(len(item.data.polygons) for item in mesh),
                "materials": materials, "packed_textures": textures, "meshes_smoothed_by_angle": smoothed,
                **({"matfuse_veneer": finish} if finish is not None else {}),
                **({"wall_lean": lean_receipt} if lean_receipt is not None else {})}

    props = plan["props"]
    receipt = {
        "dresser": place("dresser", props["dresser"], (0.995, 1.99), (0.59, 1.50, 0.94), wood=True, surface_role="dresser"),
        "radiator": place("radiator", props["radiator"], (0, DEPTH - 0.149), (1.03, 0.22, 0.51), 0.060,
                          surface_role="radiator"),
    }
    if "cabinet_panel" in props:
        matfuse = plan.get("cabinet_matfuse")
        receipt["cabinet_panel"] = place("leaning hinged cabinet panel", props["cabinet_panel"], (-0.89, 2.58),
                                         (0.84, 1.17, 1.16), lean=-0.145, aisle=0.43,
                                         wood=matfuse is None, matfuse=matfuse)
    if "debris" in props:
        spots = (((-1.05, 2.16), (0.44, 0.43, 0.095), 12.0, (0.58, 0.53, 0.47)),
                 ((1.01, 2.91), (0.47, 0.63, 0.13), 94.0, (0.67, 0.64, 0.54)),
                 ((-0.94, 3.44), (0.38, 0.41, 0.086), -37.0, (0.47, 0.46, 0.42)))
        if "rubbish" in props:
            spots = (((-1.09, 3.16), (0.28, 0.39, 0.080), 72.0, (0.48, 0.43, 0.38)),
                     ((1.117, 2.63), (0.28, 0.33, 0.067), -33.0, (0.57, 0.52, 0.46)))
        receipt["debris"] = {"placements": [place(f"debris cluster {index}", props["debris"], center, dimensions,
                                                 turn=turn, aisle=0.43, tone=tone, surface_role="debris")
                                             for index, (center, dimensions, turn, tone) in enumerate(spots, 1)]}
    if "rubbish" in props:
        spots = (((-0.89, 2.20), (0.66, 0.77, 0.117), 10, (0.82, 0.80, 0.75)),
                 ((0.98, 2.93), (0.59, 0.68, 0.095), -63, (0.71, 0.68, 0.60)),
                 ((-0.79, 3.62), (0.48, 0.44, 0.066), 159, (0.63, 0.59, 0.52)))
        receipt["rubbish"] = {"placements": [place(f"Discarded paper and cardboard {index}", props["rubbish"],
                                                   center, dimensions, turn=turn, aisle=0.43, tone=tone,
                                                   paper_smoothing=True, surface_role="rubbish")
                                               for index, (center, dimensions, turn, tone) in enumerate(spots, 1)]}
    low, high = receipt["dresser"]["bounds_m"]
    center_x = (low[0] + high[0]) / 2

    def dresser_support(x, y, prop):
        origin = prim.Vector((x, y, high[2] + 0.07))
        hit, point, _, _, obj, _ = scene.ray_cast(prim.bpy.context.evaluated_depsgraph_get(), origin,
                                                prim.Vector((0, 0, -1)), distance=0.14)
        if not hit or obj.parent is None or obj.parent.name != "Generated dresser":
            raise RuntimeError(f"No dresser surface was found under the {prop}")
        return point.z

    if "doll" in props:
        x, y = center_x - 0.05, low[1] + 0.21 * (high[1] - low[1])
        receipt["doll"] = place("seated doll", props["doll"], (x, y), (0.29, 0.26, 0.38),
                                dresser_support(x, y, "seated doll"), surface_role="doll")
    if "bottle" in props:
        x, y = center_x + 0.10, high[1] - 0.18
        bottle = place("green bottle", props["bottle"], (x, y), (0.078, 0.078, 0.24),
                       dresser_support(x, y, "green bottle"))
        bottle_low, bottle_high = bottle["bounds_m"]
        if any(bottle_low[i] <= low[i] or bottle_high[i] >= high[i] for i in range(2)):
            raise RuntimeError("The fitted green bottle overhangs the dresser")
        if "doll" in receipt:
            doll_low, doll_high = receipt["doll"]["bounds_m"]
            if all(bottle_low[i] < doll_high[i] and bottle_high[i] > doll_low[i] for i in range(2)):
                raise RuntimeError("The fitted green bottle overlaps the seated doll")
        receipt["bottle"] = bottle
    if "living_sofa" in props:
        receipt["living_sofa"] = place(
            "living-room sofa", props["living_sofa"], (-3.11, -1.88), (0.94, 1.86, 0.90),
            room_bounds=LIVING_ROOM_BOUNDS,
        )
    return receipt, applied_surfaces


def _bent_scrap(prim, name, center, dimensions, angle, curl, material):
    cos, sin = math.cos(angle), math.sin(angle)
    width, length = dimensions
    x, y, z = center
    vertices = []
    for column, u in enumerate((-0.5, -0.20, 0.21, 0.5)):
        for row, v in enumerate((-0.5, -0.11, 0.5)):
            du = width * (u + (0.08 if row == 0 and column != 0 else 0))
            dv = length * (v - (0.10 if column == 3 and row != 1 else 0))
            rise = ((u + 0.32) ** 2 + (v - 0.27) ** 2) * curl
            vertices.append((x + du * cos - dv * sin, y + du * sin + dv * cos, z + rise))
    faces = [(i, i + 3, i + 4, i + 1) for i in (0, 1, 3, 4, 6, 7)]
    scrap = prim._mesh(name, vertices, faces, material)
    return scrap


def _clutter(prim, rng, debris, props):
    mortar, plaster, paper, brick, lath = debris
    board = prim._material("Warped dusty wood panels", (0.47, 0.33, 0.19), roughness=0.85)
    prim._surface_detail(board, (12, 16, 3), strength=0.43, distance=0.014, dark_level=0.40)
    boards = [((-1.03, 2.26, 0.57), (0.042, 0.41, 1.19), -0.29),
              ((-1.01, 1.91, 0.28), (0.047, 0.21, 0.64), -0.54)]
    if "cabinet_panel" in props:
        boards = [((-1.121, 1.945, 0.18), (0.033, 0.15, 0.40), -0.57)]
        board = lath
    else:
        boards.append(((-1.11, 2.69, 0.67), (0.054, 0.57, 1.39), -0.23))
    for center, dimensions, lean in boards:
        panel = prim._box("Leaning discarded timber board", center, dimensions, board, bevel=0.006)
        panel.rotation_euler.y = lean
    piles = ((-1.02, 2.20, 42), (-1.03, 2.87, 29), (-0.94, 3.54, 24), (0.90, 2.29, 39), (1.02, 3.14, 33))
    sparse_grit = "rubbish" in props
    largest_grit = 0.055 if sparse_grit else 0.136
    rubble_height = 0.023 if sparse_grit else 0.105
    palette = (mortar, mortar, brick, lath) if sparse_grit else (mortar, mortar, plaster, plaster, brick)
    for x_center, y_center, count in piles:
        count = count // 3 if sparse_grit else count
        for index in range(count):
            size = rng.uniform(0.018, largest_grit) * (1.37 if index % 9 == 0 else 1)
            x = math.copysign(max(0.54 + size, min(HALF - size, abs(rng.gauss(x_center, 0.19)))), x_center)
            y = max(0.13 + size, min(DEPTH - size, rng.gauss(y_center, 0.25)))
            height = rng.uniform(0.006, rubble_height) if sparse_grit else (
                rng.uniform(0.010, 0.031) if index % 3 == 0 else rng.uniform(0.023, rubble_height))
            turn = rng.uniform(0, math.tau)
            material = rng.choice(palette)
            corners = rng.choice((5, 6, 7))
            chunk = [(x + size * rng.uniform(0.38, 1) * math.cos(turn + i * math.tau / corners),
                      y + size * rng.uniform(0.37, 1) * math.sin(turn + i * math.tau / corners),
                      rng.uniform(0.001, height * 0.29)) for i in range(corners)]
            tip = (x + rng.uniform(-size / 3, size / 3), y + rng.uniform(-size / 3, size / 3), height)
            faces = [(i, (i + 1) % corners, corners) for i in range(corners)]
            fragment = prim._mesh("Scattered paper, plaster, wood and grit", [*chunk, tip], faces, material)
            for polygon in fragment.data.polygons:
                polygon.use_smooth = False

    newsprint = _stains(prim, prim._material("Grey pulpy paper and old newsprint", (0.27, 0.247, 0.204),
                                            roughness=0.99), scale=36, low=(0.48, 0.43, 0.36))
    for x, y, length, angle in ((-0.73, 2.11, 0.24, -0.28), (-0.89, 2.35, 0.30, 0.63), (-1.10, 2.68, 0.27, -1.12),
                                (0.61, 2.41, 0.24, 0.53), (0.78, 2.74, 0.29, -0.88), (1.02, 3.29, 0.23, 1.07),
                                (-0.66, 3.55, 0.21, 0.02)):
        dimensions = (0.099, length * 0.76) if sparse_grit else (0.16, length)
        curl = 0.027 if sparse_grit else 0.045
        _bent_scrap(prim, "Thin creased grey paper on the floor", (x, y, 0.003), dimensions, angle, curl, newsprint)
    for x, y, angle in ((-0.97, 2.19, -0.37), (-0.73, 2.53, 0.78), (0.94, 2.63, 0.95), (0.68, 3.14, -0.22)):
        _bent_scrap(prim, "Split dark timber lath", (x, y, 0.009), (0.058, 0.29), angle, 0.023, lath)

    low, high = props["dresser"]["bounds_m"]
    center_x = (low[0] + high[0]) / 2
    doll = props.get("doll")
    bottle = props.get("bottle")
    # The bent paper can extend past its nominal edge before rotation.
    paper_radius = math.hypot(0.14 * 0.58, 0.16 * 0.60)
    for index in range(7):
        y = low[1] + (index + 1) * (high[1] - low[1]) / 8
        if doll is not None and doll["bounds_m"][0][1] - 0.05 < y < doll["bounds_m"][1][1] + 0.05:
            continue
        x = center_x + rng.uniform(-0.08, 0.06)
        if bottle is not None:
            bottle_low, bottle_high = bottle["bounds_m"]
            if bottle_low[1] - paper_radius < y < bottle_high[1] + paper_radius:
                x = min(x, bottle_low[0] - paper_radius - 0.006)
        _bent_scrap(prim, "Water-damaged papers on dresser", (x, y, high[2] + 0.004),
                    (0.14, 0.16), rng.uniform(-0.6, 0.6), 0.026, rng.choice((newsprint, paper, mortar)))
    if bottle is not None:
        return
    bottle = prim._material("Dusty green glass bottle", (0.10, 0.18, 0.088), roughness=0.33)
    for name, z, radius, height in (("Bottle body", 0.079, 0.038, 0.154), ("Bottle mouth", 0.170, 0.014, 0.062)):
        prim.bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=radius, depth=height,
            location=(center_x + 0.10, high[1] - 0.18, high[2] + z))
        obj = prim.bpy.context.object
        obj.name = name
        obj.data.materials.append(bottle)



def _night_chandelier(prim, scene):
    bpy = prim.bpy
    center_x, center_y = 0.04, 3.07
    brass = prim._material("Aged satin brass ceiling fitting", (0.29, 0.172, 0.058), roughness=0.46, metal=0.71)
    _stains(prim, brass, scale=17, low=(0.29, 0.22, 0.16))
    glass = prim._material("Amber-white opal chandelier shades", (0.75, 0.55, 0.27), roughness=0.25)
    bulb_glass = prim._material("Warm frosted practical bulbs", (0.83, 0.62, 0.32), roughness=0.37)
    for material, strength in ((glass, 0.42), (bulb_glass, 3.9)):
        tree = material.node_tree
        shader = tree.nodes.get("Principled BSDF")
        filament = tree.nodes.new("ShaderNodeBlackbody")
        filament.label = "Warm incandescent filament"
        filament.inputs["Temperature"].default_value = NIGHT_KELVIN
        tree.links.new(filament.outputs["Color"], shader.inputs["Emission Color"])
        shader.inputs["Emission Strength"].default_value = strength

    def cylinder(name, x, y, z, radius, height):
        bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=radius, depth=height, location=(x, y, z))
        item = bpy.context.object
        item.name = name
        item.data.materials.append(brass)
        edge = item.modifiers.new("Worn brass roundover", "BEVEL")
        edge.width, edge.segments = 0.003, 3

    cylinder("Chandelier canopy against the ceiling", center_x, center_y, HEIGHT - 0.017, 0.102, 0.034)
    cylinder("Brass chandelier stem", center_x, center_y, 2.353, 0.015, 0.229)
    cylinder("Rounded chandelier arm hub", center_x, center_y, 2.228, 0.059, 0.061)
    for index, angle in enumerate((0.12, 2.22, 4.31), 1):
        ux, uy = math.cos(angle), math.sin(angle)
        x, y = center_x + 0.257 * ux, center_y + 0.257 * uy
        arm = [(center_x + radius * ux, center_y + radius * uy, z)
               for radius, z in ((0.045, 2.226), (0.093, 2.224), (0.145, 2.203), (0.195, 2.188),
                                 (0.242, 2.195), (0.257, 2.218), (0.257, 2.294))]
        prim._line(f"Curved brass chandelier arm {index}", arm, brass, 0.013)
        cylinder(f"Brass cap for opal shade {index}", x, y, 2.319, 0.057, 0.025)
        rings = ((2.183, 0.109), (2.192, 0.111), (2.225, 0.103),
                 (2.272, 0.084), (2.306, 0.057), (2.318, 0.049))
        sides = 40
        vertices = [(x + radius * math.cos(math.tau * point / sides),
                     y + radius * math.sin(math.tau * point / sides), z)
                    for z, radius in rings for point in range(sides)]
        faces = [(band * sides + point, band * sides + (point + 1) % sides,
                  (band + 1) * sides + (point + 1) % sides, (band + 1) * sides + point)
                 for band in range(len(rings) - 1) for point in range(sides)]
        shade = prim._mesh(f"Open opal bell shade {index}", vertices, faces, glass)
        thickness = shade.modifiers.new("Molded glass wall", "SOLIDIFY")
        thickness.thickness = 0.0035
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=0.029,
                                            location=(x, y, 2.195))
        globe = bpy.context.object
        globe.name = f"Lit frosted bulb {index}"
        globe.scale.z = 1.19
        globe.data.materials.append(bulb_glass)
        data = bpy.data.lights.new(f"Chandelier bulb practical {index}", "POINT")
        data.energy = NIGHT_BULB_WATTS
        data.shadow_soft_size = 0.034
        data.use_temperature = True
        data.temperature = NIGHT_KELVIN
        if not data.use_temperature or abs(data.temperature - NIGHT_KELVIN) > 1:
            raise RuntimeError("Blender did not retain the chandelier's incandescent temperature")
        light = bpy.data.objects.new(data.name, data)
        light.location = (x, y, 2.153)
        scene.collection.objects.link(light)


def _camera_and_light(prim, scene, night):
    bpy = prim.bpy

    def area(name, position, target, energy, color, size, spread=180):
        data = bpy.data.lights.new(name, "AREA")
        data.energy, data.color, data.shape, data.size, data.size_y = energy, color, "RECTANGLE", *size
        data.spread = math.radians(spread)
        obj = bpy.data.objects.new(name, data)
        obj.location = position
        obj.rotation_euler = (prim.Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
        scene.collection.objects.link(obj)

    if night:
        _night_chandelier(prim, scene)
    else:
        area("Directional window spill on worn floor", (-0.115, DEPTH - 0.15, 1.69), (0.10, 1.48, 0.44), 162,
             (1.0, 0.960, 0.899), (0.83, 1.17), spread=104)
        area("Broad daylight on the exterior building", (-0.85, DEPTH + 0.48, 3.38), (0.14, DEPTH + 1.26, 0.85), 460,
             (1.0, 0.967, 0.922), (2.65, 2.25))
        area("Very weak entryway bounce", (-0.36, 0.08, 2.25), (0.20, 2.6, 1.12), 2.6,
             (0.86, 0.93, 1.0), (0.70, 0.92))
    world_name = "Residual cool night outside closed window" if night else "Muted daylight outside closed window"
    world = bpy.data.worlds.new(world_name)
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.052, 0.081, 0.145, 1) if night else (0.52, 0.64, 0.76, 1)
    background.inputs["Strength"].default_value = 0.030 if night else 0.4
    scene.world = world
    lens = bpy.data.cameras.new("Eye-level reference framing")
    lens.lens, lens.sensor_width, lens.sensor_fit = 23.5, 36, "HORIZONTAL"
    camera = bpy.data.objects.new("Eye-level room camera", lens)
    camera.location = CAMERA_LOCATION
    camera.rotation_euler = (prim.Vector((0.14, DEPTH, 0.80)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(camera)
    scene.camera = camera
    return camera


def _blender(plan_path: str) -> None:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(HERE))
    import render as prim

    bpy = prim.bpy
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    worker = prim._room_worker(plan["worker"])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    preferences = bpy.context.preferences.filepaths
    preferences.use_auto_save_temporary_files = False
    preferences.file_preview_type = "NONE"
    preferences.save_version = 0
    scene = bpy.context.scene
    rng = random.Random(SEED)
    wallpaper, masonry = _shell(prim, rng, plan.get("wallpaper"), plan.get("brick"))
    debris = _damage(prim, rng)
    night = plan.get("night", False)
    facade = _window(prim, plan.get("facade"), night)
    _window_curtains(prim)
    placements, prop_surfaces = _props(prim, worker, scene, plan)
    _clutter(prim, rng, debris, placements)
    camera = _camera_and_light(prim, scene, night)
    views = {"room": camera}
    apartment = None
    if plan.get("expanded_apartment"):
        from apartment_fixtures import dress_rooms
        from apartment_layout import build_layout

        extra_views, layout_report = build_layout(prim, scene, night=night)
        if set(extra_views) != set(APARTMENT_VIEWS):
            raise RuntimeError(f"Apartment layout returned unexpected cameras: {sorted(extra_views)}")
        furnishings = dress_rooms(prim, scene, has_living_sofa="living_sofa" in placements)
        views.update((name, extra_views[name]) for name in APARTMENT_VIEWS)
        apartment = {"layout": layout_report, "furnishings": furnishings, "views": {}}
    scene.camera = camera
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    if scene.render.engine != "CYCLES" or scene.cycles.device != "CPU":
        raise RuntimeError("The requested CPU Cycles renderer is unavailable")
    scene.cycles.samples = 24
    scene.cycles.use_denoising = True
    if hasattr(scene.cycles, "denoising_use_gpu"):
        scene.cycles.denoising_use_gpu = False
    scene.cycles.max_bounces = 8
    scene.cycles.diffuse_bounces = 4
    scene.render.threads_mode, scene.render.threads = "FIXED", 8
    scene.render.resolution_x, scene.render.resolution_y = plan["render_size"]
    scene.render.resolution_percentage = 100
    scene.view_settings.view_transform = "AgX"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = "//room.png"
    bpy.ops.wm.save_as_mainfile(filepath=plan["outputs"]["room.blend"], compress=True)
    elapsed = {}
    for name, view in views.items():
        scene.camera = view
        scene.render.filepath = plan["outputs"][f"{name}.png"]
        start = time.monotonic()
        if "FINISHED" not in bpy.ops.render.render(write_still=True):
            raise RuntimeError(f"Blender could not render the {name} camera")
        elapsed[name] = round(time.monotonic() - start, 3)
        if apartment is not None:
            apartment["views"][name] = {
                "object": view.name, "camera_m": list(view.location), "lens_mm": view.data.lens,
                "rotation_rad": list(view.rotation_euler), "path": f"{name}.png",
                "render_seconds": elapsed[name],
            }
    scene.camera = camera
    report = {"renderer": bpy.app.version_string, "device": scene.cycles.device, "samples": scene.cycles.samples,
              "render_size": plan["render_size"], "render_seconds": elapsed["room"],
              "interior_m": [WIDTH, DEPTH, HEIGHT], "camera_m": list(camera.location), "lens_mm": camera.data.lens,
              "geometry_assets": placements, "prop_surfaces": prop_surfaces,
              "wallpaper": wallpaper, "facade": facade, "recessed_masonry": masonry,
              "lighting": ({"mode": "night", "chandelier_kelvin": NIGHT_KELVIN,
                            "point_bulbs": 3, "watts_per_point": NIGHT_BULB_WATTS} if night else {"mode": "day"}),
              "source": "Original Blender set reconstructed from the supplied room composition"}
    if apartment is not None:
        report["apartment"] = apartment
    worker._write_json(plan["outputs"]["room.json"], report)


if __name__ == "__main__":
    if "--" in sys.argv:
        _blender(sys.argv[sys.argv.index("--") + 1])
    else:
        _coordinator()
