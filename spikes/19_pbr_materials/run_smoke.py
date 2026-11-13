"""Local Blender/PBR runtime probe using only the retained spike18 assets.

Run from the repository Python environment. No provider, credential, render queue,
or network is used. Child scripts, config, logs, and reports are reserved through
ProjectRoot before Blender is launched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))

from orchestrator.adapters.blender.runner import BlenderAdapter
from orchestrator.adapters.blender.scene import build_scene_plan
from orchestrator.api.cli import main as cli_main
from orchestrator.domain.entities import Asset, Job, ShotOutput, ShotOutputKind
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.service.compose import compose_render_request
from orchestrator.store.json_store import ProjectStore

SOURCE_PROJECT = "spikes/18_asset_sourcing/_work/20261107T224036_9c46656d/projects/asset_sourcing"
ASSET_IDS = ("ph_wooden_table_02", "sf_selected_model")
BLENDER = Path(r"D:\Blender\blender.exe")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", type=Path, default=BLENDER)
    args = parser.parse_args()
    root = ProjectRoot(REPO)
    run_rel = f"spikes/19_pbr_materials/_work/{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    root.make_dir(run_rel)
    project_root = root.resolve(SOURCE_PROJECT)
    source_store = ProjectStore(ProjectRoot(project_root))
    run_id = run_rel.rsplit("/", 1)[-1]
    project_id = "pbr_" + run_id.replace("_", "")[-12:]
    projects_rel = f"{run_rel}/projects"
    root.make_dir(projects_rel)
    settings = Settings(project_root=root.resolve(projects_rel))
    import contextlib
    import io

    def cli(label: str, arguments: list[str]) -> Any:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(arguments, settings=settings)
        root.write_text(f"{run_rel}/steps/{label}.json", json.dumps({"exit_code": code,
            "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}, indent=2) + "\n")
        if code:
            raise RuntimeError(f"CLI {label} failed with exit {code}; see {run_rel}/steps/{label}.json")
        try:
            return json.loads(stdout.getvalue())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"CLI {label} returned malformed JSON") from exc

    cli("create-project", ["create-project", "--json", "--data", json.dumps({
        "project_id": project_id, "title": "PBR runtime smoke", "output_profile": "low-light-bw"})])
    project_rel = f"{projects_rel}/{project_id}"
    store = ProjectStore(ProjectRoot(root.resolve(project_rel)))
    assets: dict[str, dict[str, Any]] = {}
    for asset_id in ASSET_IDS:
        asset = source_store.load(Asset, asset_id)
        if not asset.file_path or not asset.checksum:
            raise RuntimeError(f"retained asset {asset_id} lacks a registered file/checksum")
        asset_path = source_store._root.resolve(asset.file_path)
        if digest(asset_path) != asset.checksum:
            raise RuntimeError(f"retained asset {asset_id} failed its registered SHA256")
        payload = {key: value for key, value in asset.model_dump(mode="json").items()
                   if key in {"asset_id", "type", "generated", "source_url", "provider", "license",
                              "model_or_voice_license", "attribution", "attribution_surface_required",
                              "reuse_scope"} and value is not None}
        cli("register-retained-model", ["create-asset", "--json", "--project-id", project_id,
            "--file", str(asset_path), "--data", json.dumps(payload)])
        copied = store.load(Asset, asset_id)
        copied_path = store._root.resolve(copied.file_path)
        if copied.checksum != digest(copied_path):
            raise RuntimeError(f"copied retained GLB {asset_id} failed checksum verification")
        assets[asset_id] = {"type": copied.type, "path": str(copied_path), "sha256": copied.checksum,
                            "license": copied.license, "source_url": copied.source_url}
    # The PH model acquisition cache retained the original provider JPGs and a
    # hash-bearing source manifest. Register those real map files through the existing
    # CLI operation, carrying the model's CC0 attribution into each registered record.
    manifest_rel = "assets/provider_cache/manifests/ph_wooden_table_02-e468addbd0234ffaabf83dd09cd2b5bd.json"
    manifest = json.loads(source_store._root.read_text(manifest_rel))
    source_by_name = {entry["name"]: entry for entry in manifest["source_files"]}
    map_specs = {"base_color": "textures/wooden_table_02_diff_1k.jpg",
                 "normal_gl": "textures/wooden_table_02_nor_gl_1k.jpg",
                 "arm": "textures/wooden_table_02_arm_1k.jpg"}
    map_assets: dict[str, str] = {}
    root.make_dir(f"{run_rel}/incoming")
    for role, source_name in map_specs.items():
        entry = source_by_name[source_name]
        source_rel = entry["relative_path"]
        source_bytes = source_store._root.read_bytes(source_rel)
        if len(source_bytes) != entry["size"] or hashlib.sha256(source_bytes).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"cached Poly Haven map failed source manifest verification: {source_name}")
        incoming_rel = f"{run_rel}/incoming/{Path(source_name).name}"
        root.write_bytes(incoming_rel, source_bytes)
        source_path = root.resolve(incoming_rel)
        asset_id = "ph_table_" + role
        payload = {"asset_id": asset_id, "type": "texture", "generated": False,
                   "source_url": manifest["source_page"], "provider": "polyhaven",
                   "license": manifest["license"], "attribution": manifest["attribution"],
                   "attribution_surface_required": True, "reuse_scope": "project",
                   "expected_version": 0}
        cli("register-cached-map", ["create-asset", "--json", "--project-id", project_id,
            "--file", str(source_path), "--data", json.dumps(payload)])
        registered = store.load(Asset, asset_id)
        registered_path = store._root.resolve(registered.file_path)
        if registered.checksum != digest(registered_path):
            raise RuntimeError(f"registered copied map {asset_id} failed its asset checksum")
        map_assets[role] = asset_id
        assets[asset_id] = {"type": registered.type, "path": str(registered_path),
                            "sha256": registered.checksum, "license": registered.license,
                            "source_url": registered.source_url, "source_map_name": source_name,
                            "source_map_sha256": entry["sha256"]}

    # Exercise the existing typed CLI/API Location route with real, separately sourced
    # Diffuse, OpenGL normal, and ARM maps from the retained PH acquisition manifest.
    material_maps = map_assets
    location_id = f"pbr_smoke_{uuid.uuid4().hex[:8]}"
    location = {"location_id": location_id,
                "base_shell": "american_living_room", "scene_look": "low_key",
                "material_overrides": {"Oak planks": {"maps": material_maps,
                    "uv_scale": [2.0, 3.0], "normal_strength": 0.65, "ao_strength": 0.25}}}
    # CLI route may point at the existing project container, but its write target is the
    # project-local JSON store. Preserve only safe status and parsed response metadata.
    request = {"location": location}
    cli("create-location", ["create-location", "--json", "--project-id", project_id,
        "--location-id", location_id, "--data", json.dumps(request)])

    episode_id, shot_id = "ep1", "pbr_shot"
    cli("create-episode", ["create-episode", "--json", "--project-id", project_id,
        "--data", json.dumps({"episode": {"episode_id": episode_id, "project_id": project_id,
            "number": 1, "duration_target_sec": 1.0}})])
    cli("draft-script", ["draft-script", "--json", "--project-id", project_id,
        "--episode-id", episode_id, "--data", json.dumps({"script": {"script_id": "sc1",
            "episode_id": episode_id, "logline": "A textured floor in a quiet room.",
            "beats": [{"beat_id": "b1", "target_sec": 0.5, "subtlety": 1}]}})])
    cli("propose-shots", ["propose-shots", "--json", "--project-id", project_id,
        "--episode-id", episode_id, "--data", json.dumps({"shots": [{"shot_id": shot_id,
            "episode_id": episode_id, "location_id": location_id, "duration_sec": 1.0,
            "event_sec": 0.5, "camera_profile": "low-light-bw", "post_profile": "low-light-bw"}]})])
    request_spec = compose_render_request(store, shot_id=shot_id, episode_id=episode_id,
        run_id="pbr-scene-build", mode="master")
    adapter = BlenderAdapter(ProjectRoot(root.resolve(project_rel)), settings)
    build_job = Job(job_id="pbr-scene-job", type="build_scene", shot_id=shot_id,
        episode_id=episode_id, run_id=request_spec.run_id)
    assembled = adapter.assemble_scene(request_spec, build_job)
    scene_bytes = root.resolve(f"{project_rel}/{assembled.scene_path}").read_bytes()
    store.append_shot_output(ShotOutput(output_id="pbr-scene-receipt", episode_id=episode_id,
        shot_id=shot_id, record_version=request_spec.shot_version, kind=ShotOutputKind.SCENE,
        relative_path=assembled.scene_path, content_sha256=hashlib.sha256(scene_bytes).hexdigest()),
        expected_version=0)
    reopened = compose_render_request(ProjectStore(ProjectRoot(root.resolve(project_rel))),
        shot_id=shot_id, episode_id=episode_id, run_id="pbr-scene-reopen", mode="master",
        require_built_scene=True)
    reopen_job = Job(job_id="pbr-scene-reopen-job", type="build_scene", shot_id=shot_id,
        episode_id=episode_id, run_id=reopened.run_id)
    bound_plan = build_scene_plan(reopened)
    adapter._verify_plan_assets(bound_plan, job=reopen_job, bind_paths=True)
    pbr_material = next((m for m in bound_plan["scene"]["materials"]
                         if m.get("name") == "Oak planks" and m.get("textures")), None)
    if not pbr_material:
        raise RuntimeError("persisted/reopened scene lost the Oak planks PBR material")
    for role, binding in pbr_material["textures"]["bindings"].items():
        if not Path(binding.get("asset_path", "")).is_file():
            raise RuntimeError(f"adapter did not bind an existing authorized path for {role}")
    dedicated_material = json.loads(json.dumps(pbr_material))
    texture_plan = dedicated_material["textures"]
    arm_binding = texture_plan["bindings"]["arm"]
    arm_asset_id = texture_plan["maps"]["arm"]
    for role in ("roughness", "metallic"):
        texture_plan["maps"][role] = arm_asset_id
        texture_plan["bindings"][role] = dict(arm_binding)

    # Blender child explicitly invokes the implemented worker seam on real files and
    # imports the provider GLBs. Its outputs are preauthorized and kept in run_rel.
    child_rel, config_rel, report_rel = (f"{run_rel}/{name}" for name in
                                         ("inspect_blender.py", "config.json", "blender_report.json"))
    log_rel = f"{run_rel}/blender.log"
    blend_rel = f"{run_rel}/pbr_smoke.blend"
    for rel in (child_rel, config_rel, report_rel, blend_rel):
        root.write_with(rel, lambda path: None)
    for rel in (f"{run_rel}/temp", f"{run_rel}/cache", f"{run_rel}/config",
                f"{run_rel}/config/scripts", f"{run_rel}/config/datafiles", f"{run_rel}/config/extensions"):
        root.make_dir(rel)
    config = {"assets": assets, "material_spec": pbr_material,
              "dedicated_material_spec": dedicated_material, "output": str(root.resolve(report_rel)),
              "blend": str(root.resolve(blend_rel)),
              "uv_scale": [2.0, 3.0], "normal_strength": 0.65, "ao_strength": 0.25,
              "maps": material_maps,
              "glbs": [assets["ph_wooden_table_02"]["path"], assets["sf_selected_model"]["path"]]}
    root.write_text(config_rel, json.dumps(config, indent=2) + "\n")
    child = r'''import bpy, hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
from orchestrator.adapters.blender.worker import _configure_pbr_material
cfg=json.loads(Path(bpy.data.filepath).read_text()) if False else json.loads(Path(sys.argv[-2]).read_text())
def sha(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(1048576),b''): h.update(b)
 return h.hexdigest()
scene=bpy.context.scene
mat=bpy.data.materials.new('PBR Smoke')
spec=cfg['material_spec']
spec['name']='PBR Smoke'
_configure_pbr_material(bpy,mat,spec)
obj=bpy.data.objects.new('PBR Probe',bpy.data.meshes.new('PBR Probe Mesh'))
scene.collection.objects.link(obj)
obj.data.from_pydata([(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0)],[],[(0,1,2,3)])
obj.data.materials.append(mat)
images=[]
for image in bpy.data.images:
 if image.source=='FILE': images.append({'name':image.name,'colorspace':image.colorspace_settings.name,
  'packed':bool(image.packed_file),'path':image.filepath})
nodes={n.bl_idname:n for n in mat.node_tree.nodes}
mapping=nodes.get('ShaderNodeMapping')
normal=nodes.get('ShaderNodeNormalMap')
links=[(l.from_node.bl_idname,l.from_socket.name,l.to_node.bl_idname,l.to_socket.name)
 for l in mat.node_tree.links]
if not mapping or tuple(round(float(x),5) for x in mapping.inputs['Scale'].default_value[:2]) != tuple(cfg['uv_scale']):
 raise RuntimeError('mapping scale did not match request')
if not normal: raise RuntimeError('normal map node missing')
if len(images)<3 or not all(i['packed'] for i in images): raise RuntimeError('map images were not independently loaded and packed')
if not any(x[0]=='ShaderNodeTexImage' and x[2]=='ShaderNodeNormalMap' for x in links):
 raise RuntimeError('normal texture is not connected to Normal Map node')
separate=nodes.get('ShaderNodeSeparateColor')
if not separate: raise RuntimeError('ARM Separate Color node missing')
if not any(x[0]=='ShaderNodeSeparateColor' and x[1]=='Green' and x[3]=='Roughness' for x in links):
 raise RuntimeError('ARM green channel did not drive roughness fallback')
if not any(x[0]=='ShaderNodeSeparateColor' and x[1]=='Blue' and x[3]=='Metallic' for x in links):
 raise RuntimeError('ARM blue channel did not drive metallic fallback')
if not any(x[0]=='ShaderNodeSeparateColor' and x[1]=='Red' for x in links):
 raise RuntimeError('ARM red channel did not participate in requested AO mix')
for role,asset_id in cfg['maps'].items():
 expected=cfg['assets'][asset_id]['path']
 norm=lambda p:os.path.normcase(os.path.abspath(p))
 image=next((i for i in bpy.data.images if norm(bpy.path.abspath(i.filepath))==norm(expected)),None)
 if image is None: raise RuntimeError('requested image was not independently loaded: '+role+
  '; observed '+repr([(i.filepath,bpy.path.abspath(i.filepath)) for i in bpy.data.images]))
 expected_space='sRGB' if role=='base_color' else 'Non-Color'
 if image.colorspace_settings.name!=expected_space:
  raise RuntimeError('wrong image color space for '+role+': '+image.colorspace_settings.name)
if cfg['ao_strength']>0:
 if not any(x[0] in ('ShaderNodeMixRGB','ShaderNodeMix') and x[2]=='ShaderNodeBsdfPrincipled' and x[3]=='Base Color' for x in links):
  raise RuntimeError('AO blend output did not reach Principled Base Color')
else:
 if not any(x[0]=='ShaderNodeTexImage' and x[2]=='ShaderNodeBsdfPrincipled' and x[3]=='Base Color' for x in links):
  raise RuntimeError('base color image is not linked to Principled Base Color')
if not any(x[0]=='ShaderNodeTexCoord' and x[2]=='ShaderNodeMapping' and x[3]=='Vector' for x in links):
 raise RuntimeError('UV output is not linked to the Mapping node')
if sum(1 for x in links if x[0]=='ShaderNodeMapping' and x[2]=='ShaderNodeTexImage' and x[3]=='Vector')<3:
 raise RuntimeError('not all image maps share the configured UV mapping')
graph={'images':images,'links':links,'normal_strength':float(normal.inputs['Strength'].default_value),
 'uv_scale':list(mapping.inputs['Scale'].default_value[:2])}
dedicated=bpy.data.materials.new('PBR Dedicated Precedence Probe')
_configure_pbr_material(bpy,dedicated,cfg['dedicated_material_spec'])
dedicated_links=[(l.from_node.bl_idname,l.from_socket.name,l.to_node.bl_idname,l.to_socket.name)
 for l in dedicated.node_tree.links]
if not any(x[0]=='ShaderNodeTexImage' and x[2]=='ShaderNodeBsdfPrincipled' and x[3]=='Roughness' for x in dedicated_links):
 raise RuntimeError('dedicated roughness map did not override ARM green fallback')
if not any(x[0]=='ShaderNodeTexImage' and x[2]=='ShaderNodeBsdfPrincipled' and x[3]=='Metallic' for x in dedicated_links):
 raise RuntimeError('dedicated metallic map did not override ARM blue fallback')
if any(x[0]=='ShaderNodeSeparateColor' and x[1] in ('Green','Blue') and
       x[2]=='ShaderNodeBsdfPrincipled' and x[3] in ('Roughness','Metallic') for x in dedicated_links):
 raise RuntimeError('ARM channel overrode a dedicated scalar map')
# Prevent background user-preference side effects and thumbnail writes before saving.
filepaths=bpy.context.preferences.filepaths
for key,value in (('use_auto_save_temporary_files',False),('use_auto_save',False),
                  ('save_version',0),('file_preview_type','NONE')):
 if hasattr(filepaths,key): setattr(filepaths,key,value)
 if hasattr(filepaths,key) and getattr(filepaths,key)!=value:
  raise RuntimeError('could not disable Blender preference '+key)
# Preserve the actual PBR scene before resetting Blender for independent GLB imports.
for image in bpy.data.images:
 if image.source=='FILE': image.filepath='//detached/'+image.name
bpy.ops.wm.save_as_mainfile(filepath=cfg['blend'])
glb_reports=[]
for path in cfg['glbs']:
 bpy.ops.wm.read_factory_settings(use_empty=True)
 before=set(bpy.data.objects)
 bpy.ops.import_scene.gltf(filepath=path)
 meshes=[o for o in bpy.data.objects if o.type=='MESH']
 if not meshes: raise RuntimeError('GLB import produced no meshes: '+path)
 bounds=[]
 for o in meshes:
  for v in o.bound_box: bounds.append(tuple(float(x) for x in o.matrix_world @ __import__('mathutils').Vector(v)))
 glb_reports.append({'path':path,'mesh_count':len(meshes),'bounds_min':[min(v[i] for v in bounds) for i in range(3)],
  'bounds_max':[max(v[i] for v in bounds) for i in range(3)],
  'materials':[{'name':m.name,'image_links':sum(1 for l in m.node_tree.links if l.from_node.bl_idname=='ShaderNodeTexImage')
   if m.use_nodes and m.node_tree else 0} for m in bpy.data.materials]})
 # next import must be independent, avoid counting retained objects
 bpy.ops.wm.read_factory_settings(use_empty=True)
graph['dedicated_links']=dedicated_links
report={'blender_version':bpy.app.version_string,'graph':graph,'glbs':glb_reports}
Path(cfg['output']).write_text(json.dumps(report,indent=2)+'\n')
'''
    root.write_text(child_rel, child)
    command = [str(args.blender), "--background", "--factory-startup", "--disable-autoexec",
               "--python-exit-code", "1",
               "--python", str(root.resolve(child_rel)), "--", str(root.resolve(config_rel)), str(REPO)]
    env = os.environ.copy()
    temp_abs, config_abs, cache_abs = (str(root.resolve(f"{run_rel}/{part}"))
                                       for part in ("temp", "config", "cache"))
    env.update({"TEMP": temp_abs, "TMP": temp_abs, "TMPDIR": temp_abs,
                "XDG_CACHE_HOME": cache_abs, "BLENDER_USER_CONFIG": config_abs,
                "BLENDER_USER_SCRIPTS": str(root.resolve(f"{run_rel}/config/scripts")),
                "BLENDER_USER_DATAFILES": str(root.resolve(f"{run_rel}/config/datafiles")),
                "BLENDER_USER_EXTENSIONS": str(root.resolve(f"{run_rel}/config/extensions")),
                "PYTHONDONTWRITEBYTECODE": "1"})
    def run_blender_log(path: Path) -> None:
        with path.open("wb") as log:
            subprocess.run(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=300, env=env)
    root.write_with(log_rel, run_blender_log)
    report = json.loads(root.read_text(report_rel))
    # Reopen the saved blend in a second fresh Blender process to test that packed images
    # remain available independently of source paths.
    reopen_rel = f"{run_rel}/reopen.py"
    reopen_log_rel = f"{run_rel}/reopen.log"
    root.write_with(reopen_rel, lambda path: None)
    root.write_text(reopen_rel, "import bpy\nimages=[i for i in bpy.data.images if i.source == 'FILE']\nassert bpy.data.filepath\nassert len(images) >= 3\nassert all(i.packed_file for i in images)\nassert all(i.size[0] > 0 and i.size[1] > 0 for i in images)\nassert all('detached' in i.filepath.lower() for i in images)\n")
    def run_reopen_log(path: Path) -> None:
        with path.open("wb") as log:
            subprocess.run([str(args.blender), "--background", "--factory-startup", "--disable-autoexec",
                            "--python-exit-code", "1", str(root.resolve(blend_rel)), "--python",
                            str(root.resolve(reopen_rel))], cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=180, env=env)
    root.write_with(reopen_log_rel, run_reopen_log)
    evidence = {"status": "completed", "run_relative": run_rel, "project_relative": project_rel,
                "location_id": location_id, "persisted_scene": assembled.scene_path,
                "reopened_and_adapter_bound_map_count": len(pbr_material["textures"]["bindings"]),
                "location_cli": "create-location completed", "assets": assets, "blender": report,
                "blend_reopened_with_packed_images": True,
                "limits": ["Dedicated scalar routing was tested by reusing the registered ARM image for explicit roughness/metallic roles; no separate sourced scalar maps were available.",
                           "No render queue was used and no rendered still was produced."]}
    root.write_text(f"{run_rel}/evidence.json", json.dumps(evidence, indent=2) + "\n")
    print(f"PBR runtime probe completed: {run_rel}")


if __name__ == "__main__":
    main()
