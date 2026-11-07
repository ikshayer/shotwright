"""Render the generated .glb from two angles so the result can actually be judged.

Also a second, independent confirmation that glTF import works under
`blender --background --factory-startup`, which FR-04 requires of the production
renderer.

Engine id is assigned and read back rather than assumed: AGENTS.md records
BLENDER_EEVEE for this machine, but Blender has renamed that id before.
"""

import sys
import bpy
from mathutils import Vector

GLB = "D:/Work/comfyui-trellis/ComfyUI/output/3D/entity_00001_.glb"
OUT = "C:/Users/spark/.claude/jobs/ef13790d/tmp/entity_render"
RES = 900


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def pick_engine():
    scene = bpy.context.scene
    for candidate in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            scene.render.engine = candidate
        except TypeError:
            continue
        if scene.render.engine == candidate:
            return candidate
    return scene.render.engine


def main():
    clear_scene()
    engine = pick_engine()
    print(f"[render] engine={engine}", flush=True)

    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=GLB)
    imported = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    print(f"[render] imported {len(imported)} mesh object(s)", flush=True)
    if not imported:
        print("[render] FAILED: no mesh imported")
        return 1

    # World-space bounds of everything imported.
    lo = Vector((1e9, 1e9, 1e9))
    hi = Vector((-1e9, -1e9, -1e9))
    total_tris = 0
    for obj in imported:
        total_tris += len(obj.data.polygons)
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            for i in range(3):
                lo[i] = min(lo[i], world[i])
                hi[i] = max(hi[i], world[i])

    size = hi - lo
    centre = (hi + lo) / 2.0
    print(f"[render] polygons={total_tris}")
    print(f"[render] world bounds {size.x:.3f} x {size.y:.3f} x {size.z:.3f}")
    print(f"[render] up-axis height (z) = {size.z:.3f}")

    # Scale to a 1.8 m figure, the FR-20 declared envelope, so the result is judged
    # at the size it would actually be used at.
    tallest = max(size)
    scale = 1.8 / tallest if tallest > 1e-6 else 1.0
    for obj in imported:
        obj.scale = (scale, scale, scale)
        obj.location = obj.location - centre * scale
    bpy.context.view_layer.update()
    print(f"[render] scaled by {scale:.3f} to a 1.8 m envelope")

    # Neutral studio lighting: the point is to read the silhouette and surface, not to
    # make it atmospheric.
    world = bpy.data.worlds.new("W")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.05, 0.05, 0.06, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 1.0
    bpy.context.scene.world = world

    for name, loc, energy in (
        ("Key", (2.4, -3.0, 2.6), 900),
        ("Fill", (-2.8, -2.0, 1.2), 300),
        ("Rim", (0.0, 3.2, 2.4), 600),
    ):
        light_data = bpy.data.lights.new(name, type="AREA")
        light_data.energy = energy
        light_data.size = 2.0
        light = bpy.data.objects.new(name, light_data)
        light.location = loc
        direction = Vector((0, 0, 0.2)) - Vector(loc)
        light.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        bpy.context.collection.objects.link(light)

    cam_data = bpy.data.cameras.new("Cam")
    cam_data.lens = 50
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam

    scene = bpy.context.scene
    scene.render.resolution_x = RES
    scene.render.resolution_y = RES
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False

    target = Vector((0, 0, 0))
    for label, pos in (
        ("front", Vector((0.0, -4.2, 0.35))),
        ("three_quarter", Vector((3.0, -3.0, 0.9))),
    ):
        cam.location = pos
        cam.rotation_euler = (target - pos).to_track_quat("-Z", "Y").to_euler()
        scene.render.filepath = f"{OUT}_{label}.png"
        bpy.ops.render.render(write_still=True)
        print(f"[render] wrote {scene.render.filepath}", flush=True)

    print("[render] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
