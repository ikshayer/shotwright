"""Confirm the glTF importer wires the base colour texture into the material.

Distinguishes two very different situations that look identical in a render:
  (a) the texture is applied but is flat grey, and
  (b) the texture exists in the file but never reached the shader.
"""

import sys
import bpy

GLB = "D:/Work/comfyui-trellis/ComfyUI/output/3D/entity_00001_.glb"


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=GLB)

    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    print(f"[mat] mesh objects: {len(meshes)}")

    for obj in meshes:
        print(f"[mat] {obj.name}: {len(obj.data.materials)} material slot(s)")
        print(f"[mat]   uv layers: {[uv.name for uv in obj.data.uv_layers]}")
        for mat in obj.data.materials:
            if mat is None:
                print("[mat]   EMPTY SLOT")
                continue
            print(f"[mat]   material {mat.name!r} use_nodes={mat.use_nodes}")
            if not mat.use_nodes:
                continue

            images = [n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE"]
            print(f"[mat]   image texture nodes: {len(images)}")
            for node in images:
                img = node.image
                if img is None:
                    print("[mat]     node with no image")
                    continue
                linked = [
                    f"{link.to_node.name}.{link.to_socket.name}"
                    for out in node.outputs for link in out.links
                ]
                print(f"[mat]     {img.name!r} {tuple(img.size)} -> {linked or 'NOT CONNECTED'}")

            bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if bsdf is not None:
                base = bsdf.inputs.get("Base Color")
                if base is not None:
                    if base.is_linked:
                        src = base.links[0].from_node
                        print(f"[mat]   Base Color <- {src.type} "
                              f"({getattr(getattr(src,'image',None),'name','-')})")
                    else:
                        print(f"[mat]   Base Color NOT linked, constant "
                              f"{tuple(round(v,3) for v in base.default_value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
