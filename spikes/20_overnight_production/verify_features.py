#!/usr/bin/env python3
"""Independent CPU Blender HDRI and contain-fit runtime probe. All outputs are ProjectRoot-contained."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(REPO))
from PIL import Image, ImageFont, ImageStat

from orchestrator.adapters.blender.runner import BlenderAdapter
from orchestrator.adapters.blender.scene import build_scene_plan
from orchestrator.adapters.timeline.runner import build_assembly_command
from orchestrator.api.cli import main as cli_main
from orchestrator.domain.entities import (
 Asset,
 Job,
 Shot,
 ShotOutput,
 ShotOutputKind,
 Timeline,
 TimelineEntry,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.service.compose import compose_post_profile_spec, compose_render_request
from orchestrator.service.credits import collect_credits_for_shots
from orchestrator.service.timeline import _credit_cards
from orchestrator.store.json_store import ProjectStore

RUN="spikes/20_overnight_production/_work/feature_probe"
SOURCE="spikes/18_asset_sourcing/_work/20261107T224036_9c46656d/projects/asset_sourcing"
BLENDER=Path(r"D:\Blender\blender.exe")
SOURCES=("orchestrator/domain/entities.py","orchestrator/adapters/blender/scene.py","orchestrator/adapters/blender/runner.py","orchestrator/adapters/blender/worker.py","orchestrator/service/compose.py","orchestrator/store/json_store.py","orchestrator/platform/settings.py","orchestrator/service/credits.py","orchestrator/service/timeline.py","orchestrator/adapters/timeline/runner.py")
def sha(path):
 h=hashlib.sha256()
 with open(path,"rb") as f:
  for b in iter(lambda:f.read(1<<20),b""): h.update(b)
 return h.hexdigest()
def main():
 root=ProjectRoot(REPO); hashes={p:sha(root.resolve(p)) for p in SOURCES}
 root.make_dir(RUN+"/projects")
 for d in ("tmp","config","cache","scripts","datafiles","extensions"): root.make_dir(RUN+"/runtime/"+d)
 tmp=str(root.resolve(RUN+"/runtime/tmp")); conf=str(root.resolve(RUN+"/runtime/config")); cache=str(root.resolve(RUN+"/runtime/cache"))
 env=os.environ.copy(); env.update({"TMP":tmp,"TEMP":tmp,"TMPDIR":tmp,"XDG_CACHE_HOME":cache,"BLENDER_USER_CONFIG":conf,"BLENDER_USER_SCRIPTS":str(root.resolve(RUN+"/runtime/scripts")),"BLENDER_USER_DATAFILES":str(root.resolve(RUN+"/runtime/datafiles")),"BLENDER_USER_EXTENSIONS":str(root.resolve(RUN+"/runtime/extensions")),"PYTHONDONTWRITEBYTECODE":"1"})
 settings=Settings(project_root=root.resolve(RUN+"/projects"),binaries={"blender_exe":str(BLENDER)})
 def cli(label,args):
  out,err=io.StringIO(),io.StringIO()
  with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err): code=cli_main(args,settings=settings)
  root.write_text(RUN+"/steps_"+label+".json",json.dumps({"code":code,"stdout":out.getvalue(),"stderr":err.getvalue()},indent=2)+"\n")
  if code: raise RuntimeError(label+" CLI failed: "+err.getvalue())
  return json.loads(out.getvalue())
 pid="feature_probe_"+time.strftime("%H%M%S")
 cli("project",["create-project","--json","--data",json.dumps({"project_id":pid,"title":"Feature runtime verification","output_profile":"low-light-bw"})])
 src=ProjectStore(ProjectRoot(root.resolve(SOURCE))); project_root=ProjectRoot(root.resolve(RUN+"/projects/"+pid)); store=ProjectStore(project_root); assets={}
 for aid in ("ph_studio_small_09","ph_wooden_table_02","sf_selected_model"):
  a=src.load(Asset,aid); ap=src._root.resolve(a.file_path)
  if sha(ap)!=a.checksum: raise RuntimeError("cached asset checksum mismatch: "+aid)
  data={k:v for k,v in a.model_dump(mode="json").items() if k in {"asset_id","type","generated","source_url","provider","license","model_or_voice_license","attribution","attribution_surface_required","reuse_scope"} and v is not None}
  cli("asset_"+aid,["create-asset","--json","--project-id",pid,"--file",str(ap),"--data",json.dumps(data)])
  b=store.load(Asset,aid); bp=project_root.resolve(b.file_path)
  if b.checksum!=sha(bp): raise RuntimeError("copied asset checksum mismatch: "+aid)
  assets[aid]={"sha256":b.checksum,"type":b.type,"path":str(bp),"source_url":a.source_url}
 location={"location_id":"hdri_fit_location","base_shell":"american_living_room","scene_look":"low_key","environment":{"asset_id":"ph_studio_small_09","strength":1.75,"rotation_rad":0.731},"placements":[{"asset_id":"ph_wooden_table_02","position":[0,0,0],"dimensions":[1.0,0.7,0.3],"fit_mode":"contain"}]}
 cli("location",["create-location","--json","--project-id",pid,"--location-id",location["location_id"],"--data",json.dumps({"location":location})])
 cli("episode",["create-episode","--json","--project-id",pid,"--data",json.dumps({"episode":{"episode_id":"ep1","project_id":pid,"number":1,"duration_target_sec":1.0}})])
 cli("script",["draft-script","--json","--project-id",pid,"--episode-id","ep1","--data",json.dumps({"script":{"script_id":"sc1","episode_id":"ep1","logline":"Feature runtime verification.","beats":[{"beat_id":"b1","target_sec":1.0,"subtlety":1}]}})])
 cli("shot",["propose-shots","--json","--project-id",pid,"--episode-id","ep1","--data",json.dumps({"shots":[{"shot_id":"s1","episode_id":"ep1","location_id":location["location_id"],"duration_sec":1.0,"event_sec":0.5,"camera_profile":"low-light-bw","post_profile":"low-light-bw","asset_ids":["sf_selected_model"]}]})])
 request=compose_render_request(store,shot_id="s1",episode_id="ep1",run_id="feature-probe",mode="master")
 # Persist a real adapter-built scene receipt for exact-version credit collection.
 assembled=BlenderAdapter(project_root,settings).assemble_scene(request,Job(job_id="feature-scene",type="build_scene",shot_id="s1",episode_id="ep1",run_id="feature-probe"))
 scene_path=project_root.resolve(assembled.scene_path); scene_hash=sha(scene_path)
 store.append_shot_output(ShotOutput(output_id="feature_scene_receipt",episode_id="ep1",shot_id="s1",record_version=request.shot_version,kind=ShotOutputKind.SCENE,relative_path=assembled.scene_path,content_sha256=scene_hash),expected_version=0)
 shot=store.load(Shot,"s1",episode_id="ep1")
 # Collect the exact version's real persisted Scene and current Shot asset references.
 credit_data=collect_credits_for_shots(store,[shot])
 required=[row for row in credit_data["entries"] if row["publication_required"]]
 if not required: raise RuntimeError("diagnostic fixture lacks an actual registered CC-BY asset")
 font_rel="diagnostics/overlay.ttf"; project_root.make_dir("diagnostics")
 font_source=Path(r"C:\Windows\Fonts\consola.ttf")
 if not font_source.is_file(): raise FileNotFoundError(str(font_source))
 project_root.write_bytes(font_rel,font_source.read_bytes())
 profile=compose_post_profile_spec(store,shot=shot)
 cards=_credit_cards(required,project_root.resolve(font_rel),profile.storage_resolution)
 entries=[]; card_files={}; cursor=0.0
 for idx,(card_text,asset_ids,fontsize) in enumerate(cards):
  relative=f"diagnostics/credit_card_{idx+1:03d}.txt"; project_root.write_text(relative,card_text)
  entries.append(TimelineEntry(type="card",ref=card_text,start_sec=cursor,duration_sec=5.0)); card_files[idx]=(project_root.read_text(relative),fontsize); cursor+=5.0
 timeline=Timeline(episode_id="ep1",entries=entries)
 # Exercise the exact formatter and drawtext textfile expansion=none path.
 resolved={i:(project_root.resolve(f"diagnostics/credit_card_{i+1:03d}.txt"),fs) for i,(_txt,_ids,fs) in enumerate(cards)}
 argv=build_assembly_command(settings.binaries.ffmpeg_exe,timeline=timeline,shot_outputs={},media_info={},source_paths={},profile=profile,output=project_root.resolve("diagnostics/credits_preview.mp4"),font_path=project_root.resolve(font_rel),card_text_files=resolved)
 def encode(target):
  argv[-1]=str(target)
  def write_log(log_target):
   with log_target.open("wb") as log: subprocess.run(argv,cwd=REPO,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
  project_root.write_with("diagnostics/ffmpeg.log",write_log)
 project_root.write_with("diagnostics/credits_preview.mp4",encode)
 ffprobe=subprocess.run(["ffprobe","-v","error","-count_frames","-select_streams","v:0","-show_entries","stream=width,height,avg_frame_rate,nb_read_frames","-of","json",str(project_root.resolve("diagnostics/credits_preview.mp4"))],capture_output=True,text=True,check=True,timeout=30)
 probe=json.loads(ffprobe.stdout)["streams"][0]
 def decode_still(target):
  subprocess.run(["ffmpeg","-v","error","-i",str(project_root.resolve("diagnostics/credits_preview.mp4")),"-frames:v","1","-y",str(target)],check=True,timeout=30)
 project_root.write_with("diagnostics/credits_preview.png",decode_still)
 still=project_root.resolve("diagnostics/credits_preview.png")
 with Image.open(still) as decoded:
  decoded.load(); pixels=ImageStat.Stat(decoded.convert("RGB")).stddev
  if max(pixels)<2: raise RuntimeError("decoded credit preview appears blank")
 for text,(path,fontsize) in zip((c[0] for c in cards),resolved.values()):
  measure=ImageFont.truetype(str(project_root.resolve(font_rel)),fontsize)
  if any(measure.getlength(line)>profile.storage_resolution[0]-32 for line in text.splitlines()): raise RuntimeError("credit card line exceeds measured frame layout")
 project_root.write_text("diagnostics/credits_manifest.json",json.dumps(credit_data,indent=2,ensure_ascii=False)+"\n")
 project_root.write_text("diagnostics/credits_text.txt",credit_data["plain_text"])
 project_root.write_text("diagnostics/credit_preview_evidence.json",json.dumps({"asset_ids":sorted(x["asset_id"] for x in required),"source_and_license_fields_checked":True,"cards":[{"relative_path":f"diagnostics/credit_card_{i+1:03d}.txt","text":card[0],"fontsize":card[2],"asset_ids":card[1]} for i,card in enumerate(cards)],"duration_sec":timeline.total_duration_sec,"profile_resolution":profile.storage_resolution,"ffmpeg_argv":argv,"ffprobe":probe,"decoded_preview":"diagnostics/credits_preview.png","decoded_pixel_stddev":pixels},indent=2,ensure_ascii=False)+"\n")
 adapter=BlenderAdapter(project_root,settings); plan=build_scene_plan(request)
 job=Job(job_id="verify",type="build_scene",shot_id="s1",episode_id="ep1",run_id="feature-probe")
 adapter._verify_plan_assets(plan,job=job,bind_paths=True)
 reopened=compose_render_request(ProjectStore(ProjectRoot(root.resolve(RUN+"/projects/"+pid))),shot_id="s1",episode_id="ep1",run_id="feature-reopen",mode="master")
 plan2=build_scene_plan(reopened); adapter._verify_plan_assets(plan2,job=job,bind_paths=True); env2=plan2["scene"]["environment"]
 assert env2["binding"]["asset_checksum"]==assets["ph_studio_small_09"]["sha256"]
 obj=next(x for x in plan2["scene"]["objects"] if x.get("asset_id")=="ph_wooden_table_02")
 obj["asset_path"]=assets["ph_wooden_table_02"]["path"]
 config={"environment":env2,"object":obj,"blend":str(root.resolve(RUN+"/probe.blend")),"report":str(root.resolve(RUN+"/blender_report.json")),"repo":str(REPO)}
 root.write_text(RUN+"/config.json",json.dumps(config,indent=2)+"\n")
 root.write_with(RUN+"/synthetic_multi_root.glb",lambda path: None)
 child=r'''import bpy,json,sys
from pathlib import Path
from mathutils import Matrix,Euler,Vector
cfg=json.loads(Path(sys.argv[sys.argv.index("--")+1]).read_text()); sys.path.insert(0,cfg["repo"])
from orchestrator.adapters.blender.worker import _configure_environment,_import_gltf_asset
for k,v in (("use_auto_save_temporary_files",False),("use_auto_save",False),("save_version",0),("file_preview_type","NONE")):
 if hasattr(bpy.context.preferences.filepaths,k): setattr(bpy.context.preferences.filepaths,k,v)
scene=bpy.context.scene; _configure_environment(bpy,scene,cfg["environment"]); world=scene.world
nodes={n.bl_idname:n for n in world.node_tree.nodes}; links=[(l.from_node.bl_idname,l.from_socket.name,l.to_node.bl_idname,l.to_socket.name) for l in world.node_tree.links]
assert abs(nodes["ShaderNodeMapping"].inputs["Rotation"].default_value[2]-.731)<1e-6
assert abs(nodes["ShaderNodeBackground"].inputs["Strength"].default_value-1.75)<1e-6
image=nodes["ShaderNodeTexEnvironment"].image; assert image.packed_file and image.colorspace_settings.name=="Non-Color"
assert ("ShaderNodeTexCoord","Normal","ShaderNodeMapping","Vector") in links and ("ShaderNodeMapping","Vector","ShaderNodeTexEnvironment","Vector") in links
# Generate an internal two-root GLB fixture, then import it through the production helper.
from mathutils import Vector as V
bpy.ops.mesh.primitive_cube_add(size=1,location=(-1,0,0)); bpy.context.object.name="FixtureLeft"
bpy.ops.mesh.primitive_cube_add(size=1,location=(1,0,0)); bpy.context.object.name="FixtureRight"
bpy.ops.object.select_all(action="SELECT")
bpy.ops.export_scene.gltf(filepath=str(Path(cfg["report"]).with_name("synthetic_multi_root.glb")),export_format="GLB",use_selection=True)
fixture_path=str(Path(cfg["report"]).with_name("synthetic_multi_root.glb"))
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete(use_global=False)
synth={"asset_id":"synthetic_internal_fixture","asset_path":fixture_path,"name":"SyntheticMultiRoot","kind":"box","location":[0,0,0],"dimensions":[3.0,1.5,1.0],"fit_mode":"contain","rotation":[0,0,0]}
sroot,sobjects=_import_gltf_asset(bpy,Matrix,Euler,Vector,scene,synth); smeshes=[o for o in sobjects if o.type=="MESH"]
assert len(smeshes)>=2 and all(o.parent==sroot for o in sobjects),"synthetic imported roots do not share common parent"
spts=[o.matrix_world@Vector(c) for o in smeshes for c in o.bound_box]; slo=[min(float(p[i]) for p in spts) for i in range(3)]; shi=[max(float(p[i]) for p in spts) for i in range(3)]; sdims=[shi[i]-slo[i] for i in range(3)]
assert all(sdims[i]<=synth["dimensions"][i]+1e-4 for i in range(3)),(sdims,synth["dimensions"])
# Also exercise a real registered Poly Haven GLB through the same helper.
spec=cfg["object"]; root,imported=_import_gltf_asset(bpy,Matrix,Euler,Vector,scene,spec); meshes=[o for o in imported if o.type=="MESH"]
assert all(o.parent==root for o in imported),"GLB roots do not share transform parent"
pts=[o.matrix_world@Vector(c) for o in meshes for c in o.bound_box]; lo=[min(float(p[i]) for p in pts) for i in range(3)]; hi=[max(float(p[i]) for p in pts) for i in range(3)]; dims=[hi[i]-lo[i] for i in range(3)]
assert min(dims)>0 and all(dims[i]<=spec["dimensions"][i]+1e-4 for i in range(3)),(dims,spec["dimensions"])
report={"blender_version":bpy.app.version_string,"world_nodes":[n.bl_idname for n in world.node_tree.nodes],"links":links,"environment":{"asset_id":cfg["environment"]["asset_id"],"checksum":cfg["environment"]["binding"]["asset_checksum"],"strength":nodes["ShaderNodeBackground"].inputs["Strength"].default_value,"rotation_rad":nodes["ShaderNodeMapping"].inputs["Rotation"].default_value[2],"packed":bool(image.packed_file),"colorspace":image.colorspace_settings.name,"path":image.filepath},"import":{"mesh_count":len(meshes),"imported_count":len(imported),"all_share_root":all(o.parent==root for o in imported),"source_min":list(root["cctv_source_min"]),"source_max":list(root["cctv_source_max"]),"source_dimensions":list(root["cctv_source_dimensions"]),"placed_world_min":lo,"placed_world_max":hi,"placed_dimensions":dims,"fit_mode":spec.get("fit_mode","exact"),"target_dimensions":spec["dimensions"]},"synthetic_multiroot":{"provenance":"Generated internally in Blender for runtime validation; not third-party source.","mesh_count":len(smeshes),"imported_count":len(sobjects),"all_share_root":all(o.parent==sroot for o in sobjects),"source_min":list(sroot["cctv_source_min"]),"source_max":list(sroot["cctv_source_max"]),"placed_world_min":slo,"placed_world_max":shi,"placed_dimensions":sdims,"fit_mode":"contain","target_dimensions":synth["dimensions"]}}
for im in bpy.data.images:
 if im.source=="FILE": im.filepath="//detached/"+im.name
bpy.ops.wm.save_as_mainfile(filepath=cfg["blend"]); Path(cfg["report"]).write_text(json.dumps(report,indent=2)+"\n")
'''
 root.write_text(RUN+"/inspect.py",child)
 command=[str(BLENDER),"--background","--factory-startup","--disable-autoexec","--python-exit-code","1","--python",str(root.resolve(RUN+"/inspect.py")),"--",str(root.resolve(RUN+"/config.json"))]
 proc=subprocess.run(command,check=False,cwd=REPO,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300); root.write_bytes(RUN+"/blender.log",proc.stdout)
 if proc.returncode: raise RuntimeError("Blender inspect failed; see contained blender.log")
 reopen=r'''import bpy,json,sys
from pathlib import Path
scene=bpy.context.scene; world=scene.world; assert world and world.use_nodes
image=next(n.image for n in world.node_tree.nodes if n.bl_idname=="ShaderNodeTexEnvironment"); assert image.packed_file and image.size[0]>0 and image.size[1]>0 and "detached" in image.filepath.lower()
r=json.loads(Path(sys.argv[sys.argv.index("--")+1]).read_text()); r["fresh_reopen"]={"packed":True,"dimensions":list(image.size),"path":image.filepath}; Path(sys.argv[sys.argv.index("--")+2]).write_text(json.dumps(r,indent=2)+"\n")
'''
 root.write_text(RUN+"/reopen.py",reopen)
 proc=subprocess.run([str(BLENDER),"--background","--factory-startup","--disable-autoexec",str(root.resolve(RUN+"/probe.blend")),"--python-exit-code","1","--python",str(root.resolve(RUN+"/reopen.py")),"--",str(root.resolve(RUN+"/blender_report.json")),str(root.resolve(RUN+"/reopen_report.json"))],check=False,cwd=REPO,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180); root.write_bytes(RUN+"/reopen.log",proc.stdout)
 if proc.returncode: raise RuntimeError("fresh Blender reopen failed; see contained reopen.log")
 after={p:sha(root.resolve(p)) for p in SOURCES}; root.write_text(RUN+"/source_hashes_after.json",json.dumps(after,indent=2)+"\n")
 report=json.loads(root.read_text(RUN+"/reopen_report.json")); report.update({"project_id":pid,"registered_assets":assets,"persisted_location":location,"source_hashes_unchanged":hashes==after,"source_hashes_before":hashes,"source_hashes_after":after})
 root.write_text(RUN+"/evidence.json",json.dumps(report,indent=2)+"\n")
 print(json.dumps({"run":RUN,"source_hashes_unchanged":hashes==after,"blender_version":report["blender_version"],"environment":report["environment"],"import":report["import"],"reopen":report["fresh_reopen"]},indent=2))
if __name__=="__main__": main()
