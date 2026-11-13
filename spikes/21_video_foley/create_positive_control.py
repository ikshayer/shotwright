"""Create a contained procedural basketball bounce control video."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from orchestrator.platform.containment import ProjectRoot

R=ProjectRoot(ROOT)
BLENDER=Path(r"D:\Blender\blender.exe")
EVENTS=[20,55,90,125,160,195]

WORKER='''import bpy,math,sys
from mathutils import Vector
fd,bf,preview=sys.argv[sys.argv.index("--")+1:]; preview=preview=="1"
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete(use_global=False)
s=bpy.context.scene; s.render.engine="BLENDER_EEVEE"
if s.render.engine!="BLENDER_EEVEE": raise RuntimeError("unsupported engine")
s.render.resolution_x=640;s.render.resolution_y=480;s.render.resolution_percentage=100;s.render.fps=25;s.frame_start=1;s.frame_end=200
s.render.image_settings.file_format="PNG";s.render.image_settings.color_mode="RGB";s.world.color=(.22,.22,.22)
def material(name,color,rough=.55):
 m=bpy.data.materials.new(name);m.diffuse_color=(*color,1);m.use_nodes=True;p=m.node_tree.nodes.get("Principled BSDF");p.inputs["Base Color"].default_value=(*color,1);p.inputs["Roughness"].default_value=rough;return m
orange=material("Procedural bright basketball orange",(.98,.2,.012),.68);black=material("Basketball seams and plank joints",(.012,.008,.005));wood=material("Procedural maple planks",(.52,.29,.105),.7)
p=orange.node_tree.nodes.get("Principled BSDF");p.inputs["Metallic"].default_value=0
p=wood.node_tree.nodes.get("Principled BSDF");noise=wood.node_tree.nodes.new("ShaderNodeTexNoise");noise.inputs["Scale"].default_value=3.2;noise.inputs["Detail"].default_value=2;coord=wood.node_tree.nodes.new("ShaderNodeTexCoord");wood.node_tree.links.new(coord.outputs["Generated"],noise.inputs["Vector"]);ramp=wood.node_tree.nodes.new("ShaderNodeValToRGB");ramp.color_ramp.elements[0].color=(.24,.1,.025,1);ramp.color_ramp.elements[1].color=(.8,.5,.22,1);wood.node_tree.links.new(noise.outputs["Fac"],ramp.inputs["Fac"]);wood.node_tree.links.new(ramp.outputs["Color"],p.inputs["Base Color"])
bpy.ops.mesh.primitive_cube_add(size=1,location=(0,0,-.13));floor=bpy.context.object;floor.name="Visible wooden floor";floor.dimensions=(12,12,.26);bpy.ops.object.transform_apply(location=False,rotation=False,scale=True);floor.data.materials.append(wood)
for y in (-2.4,-1.2,0,1.2,2.4):
 bpy.ops.mesh.primitive_cube_add(size=1,location=(0,y,0));o=bpy.context.object;o.name="Floor plank joint";o.dimensions=(12,.012,.004);o.data.materials.append(black)
bpy.ops.mesh.primitive_uv_sphere_add(segments=48,ring_count=32,radius=.58);ball=bpy.context.object;ball.name="Basketball";ball.data.materials.append(orange)
for poly in ball.data.polygons:poly.use_smooth=True
# Two bold front-facing arcs lie on the visible hemisphere and are locally centered
# on the ball, then parented so they follow its per-frame transform exactly.
for axis in ("vertical","horizontal"):
 cu=bpy.data.curves.new("Front-facing black basketball seam",'CURVE');cu.dimensions='3D';cu.bevel_depth=.014;cu.bevel_resolution=3;sp=cu.splines.new('POLY');sp.points.add(80)
 for i,pt in enumerate(sp.points):
  t=-1.35+2.7*i/80;r=.586
  xyz=(0,-r*math.cos(t),r*math.sin(t)) if axis=="vertical" else (r*math.sin(t),-r*math.cos(t),0)
  pt.co=(*xyz,1)
 ob=bpy.data.objects.new("Front black seam "+axis,cu);bpy.context.collection.objects.link(ob);ob.data.materials.append(black);ob.parent=ball;ob.matrix_parent_inverse.identity()
bpy.ops.object.camera_add(location=(0,-8,5.4));cam=bpy.context.object;cam.rotation_euler=(Vector((0,0,1.4))-cam.location).to_track_quat('-Z','Y').to_euler();cam.data.lens=52;s.camera=cam
for name,loc,power,size,color in [("Warm key",(-3,-3,7),1100,5,(1,.82,.62)),("Soft fill",(3,1,5),800,4,(.75,.86,1))]:
 bpy.ops.object.light_add(type='AREA',location=loc);o=bpy.context.object;o.name=name;o.data.energy=power;o.data.shape='DISK';o.data.size=size;o.data.color=color;o.rotation_euler=(Vector((0,0,0))-o.location).to_track_quat('-Z','Y').to_euler()
contacts=[21,56,91,126,161,196];frames=[1,21,39,56,74,91,109,126,144,161,179,196] if preview else range(1,201)
def center_z(f):
 lo=max([c for c in contacts if c<=f],default=-14);hi=min([c for c in contacts if c>=f],default=231)
 if lo==hi:return .58
 u=(f-lo)/(hi-lo);return .58+4*1.08*u*(1-u)
s.view_settings.view_transform='Standard';s.view_settings.look='Medium High Contrast'
for idx,f in enumerate(frames):
 s.frame_set(f);ball.location=(0,0,center_z(f));ball.rotation_euler[1]=.12*math.sin(f*.12);s.render.filepath=fd+"/frame_%04d.png"%(idx+1 if preview else f)
 if f==frames[0]:bpy.ops.wm.save_as_mainfile(filepath=bf)
 bpy.ops.render.render(write_still=True)
'''

def main()->int:
 ap=argparse.ArgumentParser();ap.add_argument('--preview',action='store_true');ap.add_argument('--run-dir');a=ap.parse_args()
 stamp=datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ');rel=a.run_dir or f"spikes/21_video_foley/_work/positive_control_{stamp}"
 run=R.make_dir(rel);R.make_dir(rel+'/frames');tmp=R.make_dir(rel+'/tmp');worker=rel+'/blender_worker.py';R.write_text(worker,WORKER)
 blend=rel+'/scene.blend';log=rel+'/blender.log'
 cmd=[str(BLENDER),'-b','--python',str(R.resolve(worker)),'--',str(R.resolve(rel+'/frames')),str(R.resolve(blend)),'1' if a.preview else '0']
 env=os.environ.copy();env.update(TMP=str(tmp),TEMP=str(tmp),TMPDIR=str(tmp),PYTHONDONTWRITEBYTECODE='1')
 p=subprocess.run(cmd,check=False,cwd=str(run),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True);R.write_text(log,p.stdout)
 if p.returncode:raise RuntimeError(f"Blender failed ({p.returncode}); log {log}")
 if a.preview:
  sheet=rel+'/contact_sheet.png';q=subprocess.run(['ffmpeg','-y','-framerate','1','-start_number','1','-i',str(R.resolve(rel+'/frames/frame_%04d.png')),'-vf','scale=320:-1,tile=4x4','-frames:v','1',str(R.resolve(sheet))],check=False,capture_output=True,text=True)
  if q.returncode:raise RuntimeError(q.stderr)
 else:
  video=rel+'/positive_control_silent.mp4';q=subprocess.run(['ffmpeg','-y','-framerate','25','-start_number','1','-i',str(R.resolve(rel+'/frames/frame_%04d.png')),'-frames:v','200','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-an',str(R.resolve(video))],check=False,capture_output=True,text=True);R.write_text(rel+'/ffmpeg.log',q.stdout+'\n'+q.stderr)
  if q.returncode:raise RuntimeError(f"FFmpeg failed ({q.returncode})")
  positions=[]
  for frame_index in range(200):
   f=frame_index+1;lo=max([c+1 for c in EVENTS if c+1<=f],default=-14);hi=min([c+1 for c in EVENTS if c+1>=f],default=231);u=0 if lo==hi else (f-lo)/(hi-lo);positions.append({'frame_index':frame_index,'ball_center_z':round(.58+4*1.08*u*(1-u),5),'ball_radius':.58,'floor_z':0.0})
  metadata={'description':'Original procedural bright orange seamed basketball bouncing on warm procedural wood under fixed bright studio lighting.','fps':25,'frame_count':200,'resolution':[640,480],'authored_contact_frame_indices':EVENTS,'frame_positions':positions,'sources':[{'media_path':video,'sha256':hashlib.sha256(R.read_bytes(video)).hexdigest()}],'scene_worker_sha256':hashlib.sha256(WORKER.encode()).hexdigest(),'blender_version':'5.2.0','engine':'BLENDER_EEVEE'}
  R.write_text(rel+'/provenance.json',json.dumps(metadata,indent=2))
 print(f"RUN_DIR={rel}\nBLEND={blend}\nLOG={log}\n"+(f"CONTACT_SHEET={rel}/contact_sheet.png" if a.preview else f"VIDEO={rel}/positive_control_silent.mp4"));return 0
if __name__=='__main__':raise SystemExit(main())
