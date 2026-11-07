"""How many faces does this entity actually need at CCTV delivery resolution?

Decimates the generated mesh to a ladder of face counts, exports each as .glb to get a
real file size, and renders each at 640x480 with the figure at a plausible on-frame
height. The question is not "what looks good in a viewport" but "what survives a 640x480
frame that is then degraded", so the comparison is made at the delivery condition.

Texture is kept at the original 2048 throughout, so the only variable is geometry.
"""

import os
import sys
import bpy

GLB = "D:/Work/comfyui-trellis/ComfyUI/output/3D/entity_00001_.glb"
OUT = "C:/Users/spark/.claude/jobs/ef13790d/tmp/lod"
LADDER = [500000, 100000, 25000, 8000, 3000, 1000]
RES_X, RES_Y = 640, 480


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def setup_world_and_lights():
    world = bpy.data.worlds.new("W")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.03, 0.03, 0.035, 1)
    bpy.context.scene.world = world
    for name, loc, energy in (
        ("Key", (2.0, -2.6, 2.4), 700),
        ("Fill", (-2.4, -1.8, 1.0), 220),
        ("Rim", (0.0, 2.8, 2.2), 500),
    ):
        d = bpy.data.lights.new(name, type="AREA")
        d.energy, d.size = energy, 2.0
        o = bpy.data.objects.new(name, d)
        o.location = loc
        from mathutils import Vector
        o.rotation_euler = (Vector((0, 0, 0.2)) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
        bpy.context.collection.objects.link(o)


def main():
    from mathutils import Vector

    print("[lod] faces      glb_MB   render")
    for target in LADDER:
        reset()
        bpy.ops.import_scene.gltf(filepath=GLB)
        meshes = [o for o in bpy.data.objects if o.type == "MESH"]
        if not meshes:
            print("[lod] FAILED: nothing imported")
            return 1
        obj = meshes[0]
        original = len(obj.data.polygons)

        if target < original:
            mod = obj.modifiers.new("dec", "DECIMATE")
            mod.decimate_type = "COLLAPSE"
            mod.ratio = target / original
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.modifier_apply(modifier="dec")
        faces = len(obj.data.polygons)

        # Normalise to a 1.8 m figure standing on the origin.
        lo = Vector((1e9,) * 3)
        hi = Vector((-1e9,) * 3)
        for c in obj.bound_box:
            w = obj.matrix_world @ Vector(c)
            for i in range(3):
                lo[i], hi[i] = min(lo[i], w[i]), max(hi[i], w[i])
        size, centre = hi - lo, (hi + lo) / 2
        scale = 1.8 / max(size)
        obj.scale = (scale,) * 3
        obj.location = obj.location - centre * scale
        bpy.context.view_layer.update()

        glb_path = f"{OUT}_{faces}.glb"
        bpy.ops.export_scene.gltf(filepath=glb_path, export_format="GLB",
                                  use_selection=False)
        mb = os.path.getsize(glb_path) / 1e6

        setup_world_and_lights()
        cam_data = bpy.data.cameras.new("Cam")
        cam_data.lens = 35
        cam = bpy.data.objects.new("Cam", cam_data)
        bpy.context.collection.objects.link(cam)
        bpy.context.scene.camera = cam
        pos = Vector((1.6, -3.4, 1.1))
        cam.location = pos
        cam.rotation_euler = (Vector((0, 0, 0.1)) - pos).to_track_quat("-Z", "Y").to_euler()

        sc = bpy.context.scene
        sc.render.engine = "BLENDER_EEVEE"
        sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
        sc.render.image_settings.file_format = "PNG"
        sc.render.filepath = f"{OUT}_{faces}.png"
        bpy.ops.render.render(write_still=True)

        print(f"[lod] {faces:>8}  {mb:6.2f}   {sc.render.filepath}", flush=True)

    print("[lod] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
