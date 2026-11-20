"""Original editable shell for the small apartment around the existing room."""

from __future__ import annotations

import math

FLOOR, CEILING = 0.0, 2.49
PARTITION, DOOR_HEIGHT = 0.10, 2.03
BOUNDS = {
    "abandoned_room": (-1.325, 1.325, 0.0, 3.93),
    "hallway": (-0.54, 0.54, -5.05, 0.0),
    "living_room": (-3.65, -0.54, -3.42, -0.24),
    "kitchen": (0.54, 2.70, -2.66, -0.24),
    "bathroom": (0.54, 2.30, -4.82, -2.80),
}
# Run axis, fixed plane, center along that axis, clear width.
DOORS = {
    "abandoned_room": ("x", -0.05, 0.0, 0.80),
    "living_room": ("y", -0.54, -1.12, 0.78),
    "kitchen": ("y", 0.54, -0.98, 0.74),
    "bathroom": ("y", 0.54, -3.70, 0.68),
}
# Center along Y, aperture width, sill, head.
WINDOWS = {
    "living_room": (-1.91, 1.19, 0.78, 2.08),
    "kitchen": (-1.42, 0.92, 1.04, 2.08),
}
VIEWS = {
    "hallway": ((0.02, -4.71, 1.53), (-0.055, -0.40, 1.35), 18.5),
    "living_room": ((-0.79, -0.55, 1.55), (-2.53, -2.30, 1.08), 17.5),
    "kitchen": ((0.77, -2.39, 1.54), (2.17, -1.20, 1.14), 17.0),
    "bathroom": ((0.70, -3.72, 1.58), (1.85, -3.93, 1.06), 15.0),
}
# Position along Y, bulb wattage, Kelvin, enamel color.
PRACTICALS = {
    "hallway": (-2.73, 17.0, 2700, (0.28, 0.245, 0.18)),
    "living_room": (-1.82, 39.0, 2760, (0.42, 0.39, 0.31)),
    "kitchen": (-1.37, 28.0, 2920, (0.52, 0.50, 0.44)),
    "bathroom": (-3.77, 22.0, 2990, (0.49, 0.48, 0.43)),
}


def _band(prim, name, axis, fixed, start, end, bottom, top, thickness, material):
    center, height = (start + end) / 2, top - bottom
    z = (bottom + top) / 2
    if axis == "x":
        return prim._box(name, (center, fixed, z), (end - start, thickness, height), material)
    return prim._box(name, (fixed, center, z), (thickness, end - start, height), material)


def _strip(prim, name, axis, fixed, span, material, *, openings=(), z=(FLOOR, CEILING), thickness=PARTITION):
    """Make a solid band cut by actual doors or windows rather than visible decals."""
    cursor, end = span
    bottom, top = z
    for left, right, sill, head in sorted(openings):
        if right <= cursor or left >= end or head <= bottom or sill >= top:
            continue
        left, right = max(left, cursor), min(right, end)
        if left > cursor:
            _band(prim, name, axis, fixed, cursor, left, bottom, top, thickness, material)
        if sill > bottom:
            _band(prim, name, axis, fixed, left, right, bottom, min(sill, top), thickness, material)
        if head < top:
            _band(prim, name, axis, fixed, left, right, max(head, bottom), top, thickness, material)
        cursor = right
    if cursor < end:
        _band(prim, name, axis, fixed, cursor, end, bottom, top, thickness, material)


def _aperture(center, width, sill, head):
    return center - width / 2, center + width / 2, sill, head


def _door_hole(key):
    _, _, center, width = DOORS[key]
    return _aperture(center, width + 0.048, FLOOR, DOOR_HEIGHT + 0.034)


def _finish(prim, name, tone, *, stretches=(13, 16, 16), roughness=0.9, detail=0.33):
    material = prim._material(name, tone, roughness=roughness)
    prim._surface_detail(material, stretches, strength=detail, distance=0.004, dark_level=0.54)
    return material


def _grid(prim, name, tones, size, axes, *, grout, joint, roughness, offset=0.0):
    material = prim._material(name, tones[0], roughness=roughness)
    tree = material.node_tree
    shader = tree.nodes.get("Principled BSDF")
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    split = tree.nodes.new("ShaderNodeSeparateXYZ")
    vector = tree.nodes.new("ShaderNodeCombineXYZ")
    tree.links.new(geometry.outputs["Position"], split.inputs["Vector"])
    for source, target in zip(axes, ("X", "Y"), strict=True):
        tree.links.new(split.outputs[source], vector.inputs[target])
    pattern = tree.nodes.new("ShaderNodeTexBrick")
    pattern.offset = offset
    for socket, value in (("Scale", 1), ("Brick Width", size[0]), ("Row Height", size[1]), ("Mortar Size", joint)):
        pattern.inputs[socket].default_value = value
    for socket, color in zip(("Color1", "Color2", "Mortar"), (*tones, grout), strict=True):
        pattern.inputs[socket].default_value = (*color, 1)
    tree.links.new(vector.outputs["Vector"], pattern.inputs["Vector"])
    tree.links.new(pattern.outputs["Color"], shader.inputs["Base Color"])
    bump = tree.nodes.new("ShaderNodeBump")
    bump.invert = True
    bump.inputs["Strength"].default_value = 0.55
    bump.inputs["Distance"].default_value = 0.003
    tree.links.new(pattern.outputs["Fac"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], shader.inputs["Normal"])
    prim._surface_detail(material, (49, 7, 20) if offset else (24, 25, 15),
                         strength=0.16, distance=0.0012, dark_level=0.66)
    return material


def _room_materials(prim):
    return {
        "hallway": _finish(prim, "Hallway mottled tan lime coat", (0.356, 0.281, 0.199)),
        "hall_low": _finish(prim, "Scuffed olive oil paint below hall dado", (0.119, 0.144, 0.105), roughness=0.64),
        "living_room": _finish(prim, "Faded tobacco-sage parlor wallpaper", (0.301, 0.277, 0.199),
                               stretches=(61, 42, 6)),
        "kitchen": _finish(prim, "Warm gray-green kitchen wash", (0.327, 0.347, 0.259)),
        "bathroom": _finish(prim, "Sweated chalk plaster above bathroom tiles", (0.397, 0.399, 0.341)),
        "ceiling": _finish(prim, "Nicotine and smoke marks in old limewashed ceiling", (0.409, 0.365, 0.306)),
        "trim": _finish(prim, "Dark battered brown door frames and skirting", (0.149, 0.111, 0.072), roughness=0.64),
        "sash": _finish(prim, "Scuffed ivory overpaint on side-window sash", (0.463, 0.423, 0.338),
                        stretches=(35, 11, 70), roughness=0.67),
    }


def _floor_and_ceiling(prim, surfaces):
    floor = {
        "hallway": _grid(prim, "Long worn corridor floorboards",
                         ((0.098, 0.065, 0.038), (0.164, 0.102, 0.054)), (1.17, 0.109), ("Y", "X"),
                         grout=(0.040, 0.025, 0.014), joint=0.0016, roughness=0.52, offset=0.5),
        "living_room": _grid(prim, "Dulled small parlor parquet",
                             ((0.124, 0.070, 0.036), (0.227, 0.139, 0.067)), (0.63, 0.083), ("Y", "X"),
                             grout=(0.046, 0.027, 0.016), joint=0.0014, roughness=0.48, offset=0.5),
        "kitchen": _grid(prim, "Faded ochre and olive vinyl floor squares",
                          ((0.215, 0.161, 0.092), (0.136, 0.149, 0.099)), (0.23, 0.23), ("X", "Y"),
                          grout=(0.111, 0.095, 0.069), joint=0.0015, roughness=0.74),
        "bathroom": _grid(prim, "Old gray green unglazed bathroom floor tile",
                           ((0.186, 0.205, 0.169), (0.259, 0.268, 0.211)), (0.145, 0.145), ("X", "Y"),
                           grout=(0.077, 0.081, 0.064), joint=0.0035, roughness=0.80),
    }
    for room, material in floor.items():
        xmin, xmax, ymin, ymax = BOUNDS[room]
        # Shared floor edges meet exactly on the doorway center plane.
        center = ((xmin + xmax) / 2, (ymin + ymax) / 2)
        span = (xmax - xmin, ymax - ymin)
        prim._box(f"{room} level floor slab", (*center, -0.042), (*span, 0.084), material)
        prim._box(f"{room} plaster ceiling", (*center, CEILING + 0.052), (*span, 0.104), surfaces["ceiling"])


def _door_frame(prim, room, wood):
    axis, fixed, center, width = DOORS[room]
    left, right = center - width / 2, center + width / 2
    for edge, sign in ((left, -1), (right, 1)):
        inner, outer = sorted((edge, edge + sign * 0.031))
        _band(prim, f"{room} deep wood jamb", axis, fixed, inner, outer, FLOOR, DOOR_HEIGHT, 0.15, wood)
        inner, outer = sorted((edge + 0.008 * sign, edge + 0.052 * sign))
        for face in (-1, 1):
            _band(prim, f"{room} flat architrave", axis, fixed + PARTITION * 0.53 * face,
                  inner, outer, FLOOR, DOOR_HEIGHT + 0.044, 0.027, wood)
    _band(prim, f"{room} timber lintel", axis, fixed, left - 0.031, right + 0.031,
          DOOR_HEIGHT, DOOR_HEIGHT + 0.043, 0.15, wood)


def _hall(prim, materials):
    _, _, ymin, _ = BOUNDS["hallway"]
    xmin, xmax, room_entry_y, _ = BOUNDS["abandoned_room"]
    _strip(prim, "Abandoned-room front wall", "x", DOORS["abandoned_room"][1],
           (xmin - 0.105, xmax + 0.105), materials["hallway"], openings=(_door_hole("abandoned_room"),))
    for x, keys in ((-0.54, ("living_room",)), (0.54, ("bathroom", "kitchen"))):
        holes = tuple(_door_hole(key) for key in keys)
        side = math.copysign(1, x)
        wall_span = (ymin, room_entry_y - PARTITION)
        _strip(prim, "Shared hallway partition", "y", x, wall_span, materials["hallway"], openings=holes)
        _strip(prim, "Hall olive dado band", "y", x - side * 0.052, wall_span, materials["hall_low"],
               openings=holes, z=(0.065, 1.03), thickness=0.006)
        _strip(prim, "Hall skirting", "y", x - side * 0.061, wall_span, materials["trim"],
               openings=holes, z=(0, 0.075), thickness=0.023)
        _strip(prim, "Hall dado cap", "y", x - side * 0.058, wall_span, materials["trim"],
               openings=holes, z=(1.012, 1.038), thickness=0.017)
    _strip(prim, "Hall door wall", "x", ymin, (-0.59, 0.59), materials["hallway"],
           openings=(_aperture(0, 0.865, FLOOR, 2.04),))
    leaf = _finish(prim, "Nicked enamel painted on locked apartment entrance", (0.176, 0.137, 0.090),
                   stretches=(29, 5, 23), roughness=0.61)
    prim._box("Shut apartment entry door", (0, ymin + 0.008, 1.02), (0.864, 0.069, 2.04), leaf, bevel=0.008)
    prim._box("Old apartment entrance lock plate", (0.319, ymin + 0.049, 0.98), (0.041, 0.020, 0.11),
              materials["trim"], bevel=0.005)
    prim._line("Oval interior door handle", [(0.319, ymin + 0.06, 1.015), (0.319, ymin + 0.097, 1.015),
               (0.252, ymin + 0.097, 1.002)], materials["sash"], 0.011)
    for room in DOORS:
        _door_frame(prim, room, materials["trim"])


def _room_walls(prim, materials, room):
    xmin, xmax, ymin, ymax = BOUNDS[room]
    side = -1 if room == "living_room" else 1
    outside, near = (xmin, xmax) if side < 0 else (xmax, xmin)
    window = WINDOWS.get(room)
    hole = (_aperture(*window),) if window else ()
    walls = [("y", outside, (ymin, ymax), -side, hole),
             ("y", near + side * 0.054, (ymin, ymax), side, (_door_hole(room),))]
    # This 140 mm service wall is the only solid between the kitchen and bathroom.
    if room == "kitchen":
        _strip(prim, "Kitchen/bathroom service partition", "x", -2.73, (xmin, xmax + 0.048),
               materials[room], thickness=0.14)
    else:
        walls.append(("x", ymin, (xmin, xmax), 1, ()))
    if room != "bathroom":
        walls.append(("x", ymax, (xmin, xmax), -1, ()))

    for axis, fixed, span, face, holes in walls:
        shared = axis == "y" and face == side
        thickness = 0.011 if shared else PARTITION
        _strip(prim, f"{room} wall", axis, fixed, span, materials[room], openings=holes, thickness=thickness)
        _strip(prim, f"{room} skirting", axis, fixed + face * (thickness / 2 + 0.007), span,
               materials["trim"], openings=holes, z=(0, 0.071), thickness=0.018)
    if room == "bathroom":
        _strip(prim, "Bathroom lime coat on shared service wall", "x", ymax - 0.003, (xmin, xmax),
               materials[room], thickness=0.009)
        tiled = walls + [("x", ymax - 0.004, (xmin, xmax), -1, ())]
        for axis, fixed, span, face, holes in tiled:
            tile = _grid(prim, f"Crackled bone bathroom tile facing {axis}",
                         ((0.299, 0.321, 0.262), (0.439, 0.434, 0.350)), (0.146, 0.146),
                         ("Y" if axis == "y" else "X", "Z"), grout=(0.132, 0.135, 0.109),
                         joint=0.0027, roughness=0.59)
            on_partition = axis == "y" and face == side
            at_service_wall = axis == "x" and face == -1
            skin = 0.012 if on_partition else 0.004 if at_service_wall else 0.058
            _strip(prim, "Bathroom lower ceramic tile", axis, fixed + face * skin, span, tile,
                   openings=holes, z=(0.077, 1.47), thickness=0.012)
            _strip(prim, "Bathroom glazed bullnose cap", axis, fixed + face * (skin + 0.004),
                   span, materials["sash"], openings=holes, z=(1.462, 1.482), thickness=0.018)


def _glass(prim):
    surface = prim._material("Clear side-window glass with light dust", (0.88, 0.88, 0.85), roughness=0.18, metal=1)
    tree = surface.node_tree
    clear = tree.nodes.new("ShaderNodeBsdfTransparent")
    fresnel = tree.nodes.new("ShaderNodeFresnel")
    fresnel.inputs["IOR"].default_value = 1.51
    blend = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(fresnel.outputs["Fac"], blend.inputs[0])
    tree.links.new(clear.outputs["BSDF"], blend.inputs[1])
    tree.links.new(tree.nodes.get("Principled BSDF").outputs["BSDF"], blend.inputs[2])
    tree.links.new(blend.outputs[0], tree.nodes.get("Material Output").inputs["Surface"])
    return surface


def _outside(prim, key, side, plane, *, night):
    building = prim._material("Distant shadowed neighboring brickwork", (0.16, 0.19, 0.22), roughness=0.96)
    masonry = prim._material("Distant concrete lintels and slab edges", (0.12, 0.14, 0.16), roughness=0.96)
    dark = prim._material("Unlit neighboring apartment glass", (0.035, 0.046, 0.056), roughness=0.75)
    for material, value in ((building, 0.105), (masonry, 0.13), (dark, 0.081)):
        tree = material.node_tree
        shader = tree.nodes.get("Principled BSDF")
        glow = tree.nodes.new("ShaderNodeEmission")
        glow.inputs["Color"].default_value = shader.inputs["Base Color"].default_value
        glow.inputs["Strength"].default_value = value if night else 1.1
        ray = tree.nodes.new("ShaderNodeLightPath")
        mix = tree.nodes.new("ShaderNodeMixShader")
        tree.links.new(ray.outputs["Is Camera Ray"], mix.inputs[0])
        tree.links.new(shader.outputs["BSDF"], mix.inputs[1])
        tree.links.new(glow.outputs["Emission"], mix.inputs[2])
        tree.links.new(mix.outputs[0], tree.nodes.get("Material Output").inputs["Surface"])
    behind = plane + side * 2.62
    prim._box(f"{key} neighbor facade across dark court", (behind, -1.80, 1.8), (0.13, 6.4, 4.8), building)
    front = behind - side * 0.080
    for y in (-3.43, -2.06, -0.69, 0.68):
        for z in (0.54, 1.74, 2.94):
            prim._box("Distant apartment window stone surround", (front, y, z), (0.052, 0.68, 0.65), masonry)
            prim._box("Dark neighboring apartment pane", (front - side * 0.033, y, z),
                      (0.023, 0.59, 0.53), dark)
            prim._box("Distant chipped concrete windowsill", (front - side * 0.054, y, z - 0.32),
                      (0.10, 0.75, 0.046), masonry)


def _window(prim, room, wood, *, night):
    center, width, low, high = WINDOWS[room]
    xmin, xmax, _, _ = BOUNDS[room]
    side = -1 if room == "living_room" else 1
    plane = xmin if side < 0 else xmax
    y0, y1 = center - width / 2, center + width / 2
    for stretch in (y0, center, y1):
        _band(prim, "Narrow sprung vertical sash", "y", plane, stretch - 0.024, stretch + 0.024,
              low, high, 0.102, wood)
    for z in (low, high):
        _band(prim, "Weathered narrow horizontal sash", "y", plane, y0, y1, z - 0.026, z + 0.026, 0.105, wood)
    _band(prim, "Thin side-room sill", "y", plane - side * 0.040, y0 - 0.026, y1 + 0.026,
          low - 0.030, low + 0.021, 0.170, wood)
    glass_x = plane + side * 0.013
    prim._mesh("Closed clear side-room window pane", [(glass_x, y0, low), (glass_x, y1, low),
               (glass_x, y1, high), (glass_x, y0, high)], [(0, 1, 2, 3)], _glass(prim))
    if room == "living_room":
        cloth = _finish(prim, "Dull green cloth on parlor windows", (0.133, 0.158, 0.116),
                        stretches=(34, 34, 19), roughness=0.94)
        curtain_x = plane - side * 0.176
        rod_z = high + 0.13
        prim._line("Aged parlor curtain rod", [(curtain_x, y0 - 0.155, rod_z), (curtain_x, y1 + 0.155, rod_z)],
                   wood, 0.009)
        for edge, direction in ((y0 - 0.11, 1), (y1 + 0.11, -1)):
            columns, rows = 51, 20
            verts = []
            for row in range(rows):
                v = row / (rows - 1)
                for column in range(columns):
                    u = column / (columns - 1)
                    fold = math.sin(math.tau * (4.20 * u + 0.09 * math.sin(8 * u) + 0.08 * v * u))
                    x = curtain_x + (0.024 + 0.010 * v) * fold
                    y = edge + direction * (u * (0.315 + 0.032 * v) + 0.009 * v * math.sin(7 * u))
                    z = rod_z - 0.041 - (rod_z - low + 0.047) * v + 0.011 * v * v * fold
                    verts.append((x, y, z))
            faces = [(i, i + 1, i + 1 + columns, i + columns)
                     for i in range(columns * (rows - 1)) if (i + 1) % columns]
            panel = prim._mesh("Parted parlor curtain", verts, faces, cloth)
            hem = panel.modifiers.new("Folded cloth thickness", "SOLIDIFY")
            hem.thickness = 0.0012
            for column in (4, 14, 24, 34, 46):
                x, y, z = verts[column]
                prim._line("Small curtain hanger", [(curtain_x, y, rod_z), (x, y, z)], cloth, 0.003)
    _outside(prim, room, side, plane, night=night)


def _practical(prim, scene, room, *, night):
    y, watts, temperature, tone = PRACTICALS[room]
    xmin, xmax, _, _ = BOUNDS[room]
    x = (xmin + xmax) / 2
    is_living = room == "living_room"
    enamel = prim._material(f"{room} aged enamel fixture", tone, roughness=0.55, metal=0.16)
    prim._box(f"{room} ceiling canopy", (x, y, CEILING - 0.041), (0.17, 0.17, 0.079), enamel, bevel=0.035)
    if is_living:
        prim._line("Short enamel pendant stem", [(x, y, CEILING - 0.065), (x, y, CEILING - 0.19)], enamel, 0.013)
    hanging = 0.170 if is_living else 0.048
    sections = ((0.062, CEILING - hanging), (0.108, CEILING - hanging - 0.047),
                (0.149 if is_living else 0.108, CEILING - hanging - 0.104))
    count = 32
    points = [(x + radius * math.cos(math.tau*i/count), y + radius * math.sin(math.tau*i/count), z)
              for radius, z in sections for i in range(count)]
    faces = [(ring*count+i, ring*count+(i+1)%count, (ring+1)*count+(i+1)%count, (ring+1)*count+i)
             for ring in range(len(sections)-1) for i in range(count)]
    luminaire = prim._mesh(f"{room} aging flared enamel shade", points, faces, enamel)
    thickness = luminaire.modifiers.new("Rolled enamel thickness", "SOLIDIFY")
    thickness.thickness = 0.004
    opal = prim._material(f"{room} warm frosted glass diffuser", (0.64, 0.55, 0.41), roughness=0.27)
    tree = opal.node_tree
    shader = tree.nodes.get("Principled BSDF")
    tungsten = tree.nodes.new("ShaderNodeBlackbody")
    tungsten.inputs["Temperature"].default_value = temperature
    tree.links.new(tungsten.outputs["Color"], shader.inputs["Emission Color"])
    shader.inputs["Emission Strength"].default_value = 2.6 if night else 0.8
    diffuser_z = sections[-1][1] + 0.006
    radius = sections[-1][0] * .89
    disc = [(x + radius * math.cos(math.tau*i/count), y + radius * math.sin(math.tau*i/count), diffuser_z)
            for i in range(count)]
    prim._mesh(f"{room} warm frosted round glass diffuser", disc, [tuple(range(count - 1, -1, -1))], opal)
    bulb = prim.bpy.data.lights.new(f"{room} tungsten practical bulb", type="POINT")
    bulb.use_temperature, bulb.temperature = True, temperature
    bulb.energy = watts if night else watts * 0.62
    bulb.shadow_soft_size = 0.098 if is_living else 0.065
    light = prim.bpy.data.objects.new(f"{room} enclosed practical light", bulb)
    light.location = (x, y, diffuser_z - 0.029)
    scene.collection.objects.link(light)
    return {"position_m": list(light.location), "watts": bulb.energy, "kelvin": temperature}


def _cameras(prim, scene):
    cameras = {}
    for key, (position, target, lens) in VIEWS.items():
        data = prim.bpy.data.cameras.new(f"Apartment inspection | {key}")
        data.lens, data.sensor_width, data.sensor_fit = lens, 36, "HORIZONTAL"
        data.clip_start = 0.045
        obj = prim.bpy.data.objects.new(key, data)
        obj.location = position
        obj.rotation_euler = (prim.Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
        scene.collection.objects.link(obj)
        cameras[key] = obj
    return cameras


def build_layout(prim, scene, *, night: bool) -> tuple[dict, dict]:
    """Add the original adjoining apartment around the existing abandoned room."""
    materials = _room_materials(prim)
    _floor_and_ceiling(prim, materials)
    _hall(prim, materials)
    for room in ("living_room", "kitchen", "bathroom"):
        _room_walls(prim, materials, room)
    for room in WINDOWS:
        _window(prim, room, materials["sash"], night=night)
    lights = {room: _practical(prim, scene, room, night=night) for room in PRACTICALS}
    cameras = _cameras(prim, scene)
    return cameras, {
        "source": "Original procedurally built apartment shell, finishes, facade and practical fixtures",
        "floor_z_m": FLOOR,
        "ceiling_z_m": CEILING,
        "room_bounds_xy_m": {room: [list(bounds[:2]), list(bounds[2:])] for room, bounds in BOUNDS.items()},
        "bounds_convention": "Nominal layout centerlines except the existing room. Shared hallway partitions are 0.10 m.",
        "doorways": {
            room: {"center_m": ([center, fixed, DOOR_HEIGHT / 2] if axis == "x"
                                 else [fixed, center, DOOR_HEIGHT / 2]),
                   "floor_center_m": ([center, fixed, FLOOR] if axis == "x" else [fixed, center, FLOOR]),
                   "clear_width_m": width, "clear_height_m": DOOR_HEIGHT}
            for room, (axis, fixed, center, width) in DOORS.items()
        },
        "inspection_cameras": {
            key: {"object_name": cameras[key].name, "position_m": list(position),
                  "aim_m": list(target), "lens_mm": lens}
            for key, (position, target, lens) in VIEWS.items()
        },
        "practicals": lights,
    }
