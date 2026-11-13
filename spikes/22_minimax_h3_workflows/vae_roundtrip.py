"""Bounded native ComfyUI audio-VAE round-trip diagnostic."""
from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("_h3_roundtrip_helpers", ROOT / "spikes/22_minimax_h3_workflows/local_h3.py")
if _spec is None or _spec.loader is None: raise RuntimeError("Cannot load existing H3 lifecycle helpers")
h3 = importlib.util.module_from_spec(_spec); sys.modules[_spec.name] = h3; _spec.loader.exec_module(h3)
PROJECT = h3.PROJECT
SPIKE = "spikes/22_minimax_h3_workflows"; RUNS = SPIKE + "/_work/local_runs"
RUNTIME, COMFY, MODEL, PORT, GIB = h3.RUNTIME, h3.COMFY, h3.MODEL, h3.PORT, h3.GIB
SAMPLE_RATE, SAMPLE_COUNT, CHANNELS = 32000, 256000, 2

def utc() -> str: return datetime.now(UTC).isoformat()
def write_json(rel: str, value: Any) -> None:
    PROJECT.write_text(rel, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
def record(rel: str) -> dict[str, Any]: return h3.identity(rel)

def make_fixture() -> np.ndarray:
    """The contracted signal, channel-first and little-endian float32."""
    audio = np.zeros((2, SAMPLE_COUNT), dtype=np.float64)
    t = np.arange(64000, dtype=np.float64) / SAMPLE_RATE
    audio[0, :64000] = .1 * (np.sin(2*np.pi*220*t) + .5*np.sin(2*np.pi*440*t))
    audio[1, :64000] = .1 * (np.sin(2*np.pi*330*t) + .5*np.sin(2*np.pi*660*t))
    audio[0, 64000:128000] = .12*np.sin(2*np.pi*(110*t + .5*1945*t*t))
    audio[1, 64000:128000] = .12*np.sin(2*np.pi*(220*t + .5*2890*t*t))
    rng = np.random.Generator(np.random.PCG64(221110))
    audio[:,128000:224000] = .03*rng.standard_normal((2,96000))
    fade = np.linspace(0., 1., 640, dtype=np.float64)
    for start, stop in ((0,64000),(64000,128000),(128000,224000)):
        audio[:,start:start+640] *= fade
        audio[:,stop-640:stop] *= fade[::-1]
    return np.ascontiguousarray(audio.astype("<f4", copy=False))

def _catalog_audio():
    catalog = json.loads(PROJECT.read_text(SPIKE + "/LOCAL_MODEL_CATALOG.json"))
    rows = [x for x in catalog.get("components", []) if x.get("role") == "audio_vae"]
    if len(rows) != 1: raise ValueError("Catalog must contain exactly one audio VAE")
    return catalog, rows[0]

def _required(info, node, port):
    try: return info[node]["input"]["required"][port]
    except (KeyError, TypeError) as exc: raise ValueError(f"Actual schema lacks required port {node}.{port}") from exc

def _port_type(spec):
    if not isinstance(spec, list) or not spec: raise ValueError(f"Malformed Comfy port schema: {spec!r}")
    return spec[0] if isinstance(spec[0], str) else "COMBO"

def _options(spec):
    if not isinstance(spec, list) or not spec: return None
    if spec[0] == "COMBO" and len(spec)>1 and isinstance(spec[1],dict): return spec[1].get("options")
    return spec[0] if isinstance(spec[0],list) else None

def _validate_graph(info, graph):
    classes = {"1":"VAELoader","2":"LoadAudio","3":"VAEEncodeAudio","4":"CCTVH3AudioDiagnostic","5":"SaveAudio"}
    if len(graph)!=5 or {k:v.get("class_type") for k,v in graph.items()} != classes:
        raise ValueError("Graph must contain exactly the contracted five nodes")
    for key,row in graph.items():
        kind = row["class_type"]
        if kind not in info: raise ValueError(f"Actual /object_info lacks {kind}")
        required = info[kind].get("input",{}).get("required",{})
        if set(row["inputs"]) != set(required): raise ValueError(f"Required input set differs from actual {kind} schema")
        for port,value in row["inputs"].items():
            spec = required[port]; options = _options(spec)
            if options is not None and isinstance(value,str) and value not in options:
                raise ValueError(f"Unavailable choice at {kind}.{port}: {value}")
            if isinstance(value,list) and len(value)==2 and isinstance(value[0],str) and value[0] in graph:
                out = info[graph[value[0]]["class_type"]].get("output",[])
                if type(value[1]) is not int or not 0 <= value[1] < len(out): raise ValueError(f"Bad link at {kind}.{port}")
                target = _port_type(spec)
                if target not in ("COMBO",out[value[1]]): raise ValueError(f"Link type mismatch at {kind}.{port}")
    expected = {"1":{"vae_name":graph["1"]["inputs"].get("vae_name")}, "2":{"audio":"known_audio.wav"},
        "3":{"audio":["2",0],"vae":["1",0]}, "4":{"samples":["3",0],"vae":["1",0]},
        "5":{"audio":["4",0],"filename_prefix":"roundtrip_trigger"}}
    if any(graph[k]["inputs"] != v for k,v in expected.items()): raise ValueError("Graph edges/literals differ from Amendment6")
    needed = {("1","vae_name"):"COMBO",("2","audio"):"COMBO",("3","audio"):"AUDIO",("3","vae"):"VAE",
        ("4","samples"):"LATENT",("4","vae"):"VAE",("5","audio"):"AUDIO",("5","filename_prefix"):"STRING"}
    for (key,port),want in needed.items():
        got = _port_type(_required(info,classes[key],port))
        if want=="COMBO":
            if got!="COMBO": raise ValueError(f"Expected combo at {classes[key]}.{port}")
        elif got!=want: raise ValueError(f"Wrong schema type at {classes[key]}.{port}: {got}")
    if info["CCTVH3AudioDiagnostic"].get("output",[None])[0] != "AUDIO": raise ValueError("Diagnostic output is not AUDIO")

def build_graph(info: dict[str, Any]) -> dict[str, Any]:
    _,component = _catalog_audio(); audio_name = Path(component["model_relative_path"]).name
    opts = _options(_required(info,"LoadAudio","audio"))
    if opts is None or "known_audio.wav" not in opts: raise ValueError("Live LoadAudio schema lacks known_audio.wav")
    graph = {"1":{"class_type":"VAELoader","inputs":{"vae_name":audio_name}},
        "2":{"class_type":"LoadAudio","inputs":{"audio":"known_audio.wav"}},
        "3":{"class_type":"VAEEncodeAudio","inputs":{"audio":["2",0],"vae":["1",0]}},
        "4":{"class_type":"CCTVH3AudioDiagnostic","inputs":{"samples":["3",0],"vae":["1",0]}},
        "5":{"class_type":"SaveAudio","inputs":{"audio":["4",0],"filename_prefix":"roundtrip_trigger"}}}
    _validate_graph(info,graph); return graph


def _write_fixture(run_rel: str) -> dict[str, Any]:
    fixture = make_fixture()
    raw = fixture.T.copy(order="C").astype("<f4", copy=False).tobytes()
    raw_rel, wav_rel = run_rel + "/input/known_audio.f32le", run_rel + "/input/known_audio.wav"
    PROJECT.write_bytes(raw_rel, raw)
    def encode(path: Path) -> None:
        args = ["ffmpeg","-hide_banner","-nostdin","-n","-v","error","-f","f32le","-ar",str(SAMPLE_RATE),"-ac",str(CHANNELS),"-i",str(PROJECT.resolve(raw_rel)),"-c:a","pcm_f32le","-f","wav",str(path)]
        h3.command(args, run_rel, "fixture_wav_encode", timeout=180)
    PROJECT.write_with(wav_rel, encode)
    decoded_rel = run_rel + "/temp/fixture_wav_verify.f32le"
    def decode(path: Path) -> None:
        args = ["ffmpeg","-hide_banner","-nostdin","-y","-v","error","-i",str(PROJECT.resolve(wav_rel)),"-map","0:a:0","-f","f32le","-acodec","pcm_f32le","-ar",str(SAMPLE_RATE),"-ac",str(CHANNELS),str(path)]
        h3.command(args, run_rel, "fixture_wav_decode", timeout=180)
    PROJECT.write_with(decoded_rel, decode)
    if PROJECT.read_bytes(decoded_rel) != raw: raise ValueError("PCM float WAV decode differs from exact fixture bytes")
    construction = {"sample_rate":SAMPLE_RATE,"channels":CHANNELS,"shape_channel_first":[2,SAMPLE_COUNT],
        "segments":[{"samples":[0,64000],"left":".1*(sin(2*pi*220*t)+.5*sin(2*pi*440*t))","right":".1*(sin(2*pi*330*t)+.5*sin(2*pi*660*t))"},
        {"samples":[64000,128000],"left":".12*sin(2*pi*(110*t+.5*1945*t*t))","right":".12*sin(2*pi*(220*t+.5*2890*t*t))"},
        {"samples":[128000,224000],"rng":"numpy.random.Generator(numpy.random.PCG64(221110))","scale":.03},
        {"samples":[224000,256000],"signal":"silence"}],"fade_samples_each_nonzero_segment_edge":640,
        "fade":"first linspace(0,1,640), last reversed linspace(0,1,640)","cast_once":"little-endian float32 after float64 synthesis","transforms":[]}
    construction_rel = run_rel + "/input/fixture_construction.json"
    write_json(construction_rel, construction)
    return {"spec":construction,"wav":record(wav_rel),"raw_f32le":record(raw_rel),"construction":record(construction_rel),"decoded_wav_verification":{"exact_equal":True,"bytes":len(raw)}}

def _source_snapshot() -> dict[str, Any]:
    receipt_rel = RUNTIME + "/receipts/source_receipt.json"
    receipt_identity = record(receipt_rel)
    receipt = json.loads(PROJECT.read_text(receipt_rel)); files = receipt.get("files")
    if not isinstance(files,list) or len(files)!=1332: raise ValueError("Runtime receipt does not list exactly1332 files")
    observed=[]
    for row in files:
        actual=record(row["path"])
        if (actual["bytes"],actual["sha256"])!=(row["bytes"],row["sha256"]): raise ValueError("Pinned runtime source hash mismatch: "+row["path"])
        observed.append(actual)
    return {"receipt":receipt_identity,"source_pre":observed}

def _audio_model_snapshot() -> dict[str, Any]:
    _,component=_catalog_audio()
    if component.get("format")!="safetensors": raise ValueError("Audio VAE must be safetensors")
    rel=component.get("actual",{}).get("path") or MODEL+"/"+component["model_relative_path"]
    actual=record(rel)
    if (actual["bytes"],actual["sha256"])!=(component["expected_bytes"],component["expected_sha256"]): raise ValueError("Pinned audio VAE hash/size mismatch")
    acquisition_rel=MODEL+"/acquisition_receipt.json"; acquisition=json.loads(PROJECT.read_text(acquisition_rel))
    rows=[x for x in acquisition.get("components",[]) if x.get("role")=="audio_vae"]
    if len(rows)!=1 or rows[0].get("actual",{}).get("sha256")!=actual["sha256"] or rows[0].get("actual",{}).get("bytes")!=actual["bytes"]:
        raise ValueError("Acquisition receipt audio VAE differs from exact file")
    return {**actual,"role":"audio_vae","expected_bytes":component["expected_bytes"],"expected_sha256":component["expected_sha256"],"revision":component["revision"],"catalog":record(SPIKE+"/LOCAL_MODEL_CATALOG.json"),"acquisition_receipt":record(acquisition_rel)}

def _archive_code(run_rel: str) -> dict[str, Any]:
    sources={"driver":SPIKE+"/vae_roundtrip.py","local_h3_helpers":SPIKE+"/local_h3.py","audio_diagnostic_extension":SPIKE+"/stage_extension/h3_staging/__init__.py"}
    result={}
    for role,rel in sources.items():
        dest=run_rel+"/code/"+Path(rel).name
        with PROJECT.resolve(rel).open("rb") as stream: PROJECT.write_stream(dest,stream)
        result[role]=record(dest)
    return result

def _partial_capture(run_rel: str) -> list[dict[str, Any]]:
    directory=PROJECT.resolve(run_rel+"/audio_diagnostic")
    if not directory.exists(): return []
    return [record(str(path.relative_to(ROOT)).replace("\\","/")) for path in sorted(directory.rglob("*")) if path.is_file()]


def run_roundtrip(*, prepare_only: bool=False) -> dict[str, Any]:
    run_id="vae_roundtrip_"+datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:8]
    run_rel=RUNS+"/"+run_id; PROJECT.make_dir(run_rel)
    for leaf in ("input","output","temp","user","previews","database","cache"): PROJECT.make_dir(run_rel+"/"+leaf)
    policy={"admission_available_ram_bytes":6*GIB,"stop_available_ram_bytes":2*GIB,"stop_consecutive_samples":3,
        "sample_interval_seconds":1,"startup_deadline_seconds":180,"schema_preparation_deadline_seconds":180,
        "generation_deadline_seconds":1800,"limits_are_configured_safety_policy_not_measured_model_demand":True}
    result={"schema_version":1,"strategy":"known_audio_vae_roundtrip","run":run_rel,"status":"preflight",
        "started_utc":utc(),"prepare_only":prepare_only,"policy":policy,"fixture":{},"quality_accepted":False,
        "production_state_mutated":False,"status_history":[],"resource_peaks":{},"runtime":{}}
    process=None; tree=None; log=None; watcher=None; gpu=None; cleanup_error=None
    stop_event=threading.Event(); faults=[]; cleanup_lock=threading.Lock(); deadline={"at":float("inf"),"phase":"not_started"}
    def terminate_owned():
        nonlocal cleanup_error
        with cleanup_lock:
            if tree is None:return None
            try:
                evidence=tree.stop(); result["runtime"]["owned_process_cleanup"]=evidence; return evidence
            except Exception as exc:
                cleanup_error={"error_type":type(exc).__name__,"error":str(exc),"identities":tree.snapshot()}
                result["runtime"]["owned_process_cleanup_error"]=cleanup_error; raise
    def check_watchdog():
        if faults: raise faults[0]
    started=time.monotonic()
    try:
        result["fixture"]=_write_fixture(run_rel)
        result["audio_vae"]=_audio_model_snapshot()
        result["native_sources"]=_source_snapshot()
        result["runtime"].update({"source_receipt":result["native_sources"]["receipt"],"source_pre":result["native_sources"]["source_pre"]})
        result["runtime"]["source"] = record(COMFY+"/main.py")
        result["runtime"]["extension"] = record(SPIKE+"/stage_extension/h3_staging/__init__.py")
        result["runtime"]["driver"] = record(SPIKE+"/vae_roundtrip.py")
        result["runtime"]["code_archive"] = _archive_code(run_rel)
        gpu=h3.PynvmlProbe(device_index=0)
        baseline=h3.resources(gpu); result["resource_baseline"]=baseline
        result["disk_baseline_bytes_by_drive"]=baseline["disk_free_bytes_by_drive"]
        result["resource_peaks"]={"minimum_ram_available_bytes":baseline["ram_available_bytes"],
            "maximum_commit_used_bytes":baseline["windows_commit_used_bytes"],
            "maximum_gpu_whole_device_used_mb":baseline["gpu_whole_device_mb"]["used_mb"],"maximum_process_memory_bytes":{}}
        competitors=[x for x in gpu.process_usage() if any(n in x.name.lower() for n in ("python","comfy","blender"))]
        if competitors:
            result["competing_gpu_processes"]=[x._asdict() if hasattr(x,"_asdict") else str(x) for x in competitors]
            raise RuntimeError("Another possible GPU worker is active; no round-trip started")
        if baseline["ram_available_bytes"] < policy["admission_available_ram_bytes"]:
            result["status"]="held_preflight"; result["status_history"].append({"status":result["status"],"reason":"available RAM below configured6GiB admission floor","utc":utc()}); return result
        device=h3.command(["nvidia-smi","--id=0","--query-gpu=name","--format=csv,noheader"],run_rel,"nvidia_device",timeout=15).stdout.strip()
        if "RTX 5070 Ti Laptop" not in device: raise RuntimeError("GPU0 is not the required RTX 5070 Ti Laptop")
        result["runtime"]["device"]={"cuda_index":0,"name":device}
        env=h3.clean_env(run_rel); env.update({"CCTV_H3_AUDIO_DIAGNOSTIC":"1","CCTV_H3_RUN_REL":run_rel,"CCTV_H3_INVOCATION_ID":run_id})
        config_rel=run_rel+"/extra_model_paths.yaml"
        yaml="h3_staging:\n  base_path: "+str(PROJECT.resolve(SPIKE+"/stage_extension")).replace("\\","/")+"\n  custom_nodes: .\n"
        PROJECT.write_text(config_rel,yaml)
        py=PROJECT.resolve(RUNTIME+"/env/Scripts/python.exe"); main=PROJECT.resolve(COMFY+"/main.py")
        help_proc=subprocess.run([str(py),"-B",str(main),"--help"],cwd=PROJECT.resolve(COMFY),env=env,capture_output=True,text=True,timeout=60,check=True)
        help_text=help_proc.stdout+help_proc.stderr; PROJECT.write_text(run_rel+"/runtime_help.log",help_text)
        args=[str(py),"-B",str(main),"--listen","127.0.0.1","--port",str(PORT),"--offline","--disable-all-custom-nodes","--cuda-device","0",
            "--use-pytorch-cross-attention","--fast-disk","--disable-pinned-memory","--base-directory",str(PROJECT.resolve(COMFY)),"--models-directory",str(PROJECT.resolve(MODEL))]
        for leaf,flag in (("input","--input-directory"),("output","--output-directory"),("temp","--temp-directory"),("user","--user-directory"),("previews","--previews-directory")):
            args.extend([flag,str(PROJECT.resolve(run_rel+"/"+leaf))])
        args.extend(["--database-url","sqlite:///"+str(PROJECT.resolve(run_rel+"/database/comfy.db")).replace("\\","/"),"--extra-model-paths-config",str(PROJECT.resolve(config_rel)),"--whitelist-custom-nodes","h3_staging"])
        if any(flag not in help_text for flag in args if flag.startswith("--")): raise RuntimeError("Pinned Comfy CLI lacks a required isolated-server flag")
        with socket.socket() as sock: sock.bind(("127.0.0.1",PORT))
        log_rel=run_rel+"/comfy.log"; PROJECT.write_bytes(log_rel,b""); log=PROJECT.resolve(log_rel).open("ab")
        process=subprocess.Popen(args,cwd=PROJECT.resolve(COMFY),env=env,stdout=log,stderr=subprocess.STDOUT)
        tree=h3.OwnedProcessTree(process); tree.refresh()
        result["runtime"].update({"command":args,"pid":process.pid,"owned_processes":tree.snapshot(),
            "source_receipt":record(RUNTIME+"/receipts/source_receipt.json"),"extra_paths_config":record(config_rel),
            "isolated_env":RUNTIME+"/env","model_root":MODEL,"network":"loopback; Comfy offline; HF/Transformers offline; remote proxies closed"})
        deadline.update({"at":time.monotonic()+180,"phase":"startup"})
        def watch():
            low=0
            while not stop_event.is_set():
                try:
                    observed=tree.refresh()
                    if process.poll() is not None and not any(x["live_identity_verified"] for x in observed): raise RuntimeError("Owned Comfy tree exited unexpectedly")
                    reading=h3.resources(gpu,tree)
                    PROJECT.append_line(run_rel+"/resources.jsonl",json.dumps(reading,separators=(",",":"),allow_nan=False))
                    peaks=result["resource_peaks"]
                    peaks["minimum_ram_available_bytes"]=min(peaks["minimum_ram_available_bytes"],reading["ram_available_bytes"])
                    peaks["maximum_commit_used_bytes"]=max(peaks["maximum_commit_used_bytes"],reading["windows_commit_used_bytes"])
                    peaks["maximum_gpu_whole_device_used_mb"]=max(peaks["maximum_gpu_whole_device_used_mb"],reading["gpu_whole_device_mb"]["used_mb"])
                    mem=reading["process_memory_bytes"] or {"aggregate_memory_bytes":{}}
                    peaks["maximum_process_memory_bytes"]={k:max(peaks["maximum_process_memory_bytes"].get(k,0),v) for k,v in mem["aggregate_memory_bytes"].items()}
                    peaks["owned_processes"]=mem.get("identity_snapshot",[])
                    low=low+1 if reading["ram_available_bytes"] < policy["stop_available_ram_bytes"] else 0
                    if low>=policy["stop_consecutive_samples"]: raise MemoryError("Available RAM below configured2GiB stop floor for3consecutive samples")
                    if time.monotonic()>=deadline["at"]: raise TimeoutError("Independent watchdog "+deadline["phase"]+" deadline exceeded")
                except Exception as exc:  # noqa: BLE001 - watchdog must retain every observer failure.
                    if stop_event.is_set(): return
                    faults.append(exc); result["watchdog_failure"]={"type":type(exc).__name__,"message":str(exc),"utc":utc()}
                    try: terminate_owned()
                    except Exception as stop_exc:  # noqa: BLE001 - retain cleanup failure during watchdog stop.
                        result["watchdog_cleanup_failure"]={"type":type(stop_exc).__name__,"message":str(stop_exc)}
                    return
                stop_event.wait(1.0)
        watcher=threading.Thread(target=watch,name="H3-roundtrip-watchdog",daemon=False); watcher.start()

        startup_end=time.monotonic()+180
        while time.monotonic()<startup_end:
            check_watchdog()
            if process.poll() is not None and not any(x["live_identity_verified"] for x in tree.refresh()): raise RuntimeError("Owned Comfy process tree exited during startup")
            try: stats=h3.http("/system_stats",timeout=2); break
            except (OSError,TimeoutError,json.JSONDecodeError): time.sleep(1)
        else: raise TimeoutError("Native Comfy startup exceeded180s")
        if not any("RTX 5070 Ti Laptop" in x.get("name","") and x.get("type")=="cuda" for x in stats.get("devices",[])):
            raise RuntimeError("Comfy did not report the required CUDA device")
        result["runtime"]["system_stats"]=stats
        deadline.update({"at":time.monotonic()+180,"phase":"schema_preparation"})
        info=h3.http("/object_info",timeout=30); info_rel=run_rel+"/object_info.json"; write_json(info_rel,info)
        graph=build_graph(info); graph_rel=run_rel+"/graph.json"; write_json(graph_rel,graph)
        result["object_info"]=record(info_rel)
        result["graph"]={**record(graph_rel),"classes":{k:v["class_type"] for k,v in graph.items()},"edges":{k:v["inputs"] for k,v in graph.items()}}
        result["sample_file"]=result["fixture"]["wav"]
        result["prompt"]={"mode":"not_queued" if prepare_only else "pending","text_prompt":None,"graph":result["graph"]}
        result["status_history"].append({"status":"prepared","utc":utc()})
        if prepare_only:
            result["status"]="prepared_not_queued"; return result
        check_watchdog(); deadline.update({"at":time.monotonic()+1800,"phase":"queued_execution_and_capture"})
        submitted=h3.http("/prompt",{"prompt":graph,"client_id":run_id},timeout=30)
        response_rel=run_rel+"/prompt_response.json"; write_json(response_rel,submitted)
        result["prompt_response"]=record(response_rel)
        if submitted.get("node_errors") or not submitted.get("prompt_id"): raise RuntimeError("Comfy rejected round-trip prompt")
        prompt_id=submitted["prompt_id"]; result["prompt_id"]=prompt_id
        result["prompt"]={"mode":"native_comfy_api_graph_submission","text_prompt":None,"prompt_id":prompt_id,"graph":result["graph"],"response":result["prompt_response"]}
        result["status"]="queued"; result["status_history"].append({"status":"queued","prompt_id":prompt_id,"utc":utc()})
        history=None; until=time.monotonic()+1800
        while time.monotonic()<until:
            check_watchdog()
            if process.poll() is not None and not any(x["live_identity_verified"] for x in tree.refresh()): raise RuntimeError("Owned Comfy process tree exited during execution")
            try:
                raw=h3.http("/history/"+prompt_id,timeout=5)
                if prompt_id in raw:
                    history=raw[prompt_id]; history_rel=run_rel+"/history.json"; write_json(history_rel,history)
                    result["history"]={**record(history_rel),"prompt_id":prompt_id,"ui_output_nodes":sorted(history.get("outputs",{}).keys()),"status":history.get("status",{})}
                    messages=history.get("status",{}).get("messages",[])
                    if any(row and row[0] in ("execution_error","execution_interrupted") for row in messages): raise RuntimeError("Comfy history reports execution failure")
                    if history.get("status",{}).get("completed"): break
            except (OSError,TimeoutError,json.JSONDecodeError): pass
            time.sleep(1)
        else: raise TimeoutError("Round-trip queued execution/capture exceeded1800s")
        check_watchdog()
        history_status=history.get("status",{})
        history_messages=history_status.get("messages",[])
        if not history_status.get("completed") or any(row and row[0] in ("execution_error","execution_interrupted") for row in history_messages):
            raise RuntimeError("History does not prove successful completion for the submitted prompt")
        if "5" not in history.get("outputs",{}): raise RuntimeError("History lacks the SaveAudio trigger output")
        receipt_rel=run_rel+"/audio_diagnostic/receipt.json"; receipt=json.loads(PROJECT.read_text(receipt_rel))
        if receipt.get("invocation_id")!=run_id: raise ValueError("Capture receipt invocation mismatch")
        tree.refresh()
        own=next((x for x in tree.snapshot() if x["pid"]==receipt["pid"]),None)
        if own is None or own["create_time"]!=receipt.get("create_time"): raise ValueError("Capture receipt PID/create_time is not in the observed owned tree")
        result["audio_diagnostic"]=record(receipt_rel)
        result["latent_interpretation"]="sampled_audio_latent is the native encoded VAE input for this run, not a diffusion-sampled latent"
        tree.refresh(); result["runtime"]["owned_processes"]=tree.snapshot()
        log_text=PROJECT.read_text(run_rel+"/comfy.log")
        fallback_messages=["Ran out of memory when regular VAE encoding, retrying with tiled VAE encoding.",
                           "Ran out of memory when regular VAE decoding, retrying with tiled VAE decoding."]
        result["native_tiled_oom_fallback_messages"]=[message for message in fallback_messages if message in log_text]
        result["native_tiled_oom_fallback_observed"]=bool(result["native_tiled_oom_fallback_messages"])
        result["status"]="roundtrip_captured"; result["status_history"].append({"status":result["status"],"utc":utc()})
        return result
    except Exception as exc:  # noqa: BLE001 - retain bounded run failure as artifact.
        result["status"]="failed"; result["error"]={"type":type(exc).__name__,"message":str(exc)}
        result["status_history"].append({"status":"failed","utc":utc(),"error_type":type(exc).__name__,"error":str(exc)})
        return result
    finally:
        stop_event.set()
        if tree is not None:
            try: terminate_owned()
            except Exception as exc:  # noqa: BLE001 - cleanup failures become durable run evidence.
                result["status"]="failed"; result["cleanup_failure"]={"type":type(exc).__name__,"message":str(exc),"detail":cleanup_error}
        if watcher is not None:
            watcher.join(timeout=15); result["runtime"]["watchdog_thread_stopped"]=not watcher.is_alive()
            if watcher.is_alive(): result["status"]="failed"; result["cleanup_failure"]={"type":"WatchdogJoinTimeout","message":"Watchdog did not join within15s"}
        if process is not None:
            cleanup=result["runtime"].get("owned_process_cleanup",{})
            final_identities=tree.snapshot() if tree else []
            result["runtime"].update({"owned_server_stopped":cleanup.get("verified_stopped",False),"returncode_after_stop":process.poll(),"owned_processes":final_identities,"owned_processes_final":final_identities})
        if log is not None: log.close()
        if faults:
            result["status"]="failed"
            result["primary_failure"]={"type":type(faults[0]).__name__,"message":str(faults[0]),"source":"independent_watchdog"}
            result["watchdog_primary_failure"]=result["primary_failure"]
        try:
            if "native_sources" in result:
                post=_source_snapshot(); result["native_sources"]["source_post"]=post["source_pre"]
                unchanged=result["native_sources"]["source_pre"]==post["source_pre"]
                result["native_sources"]["source_unchanged"]=unchanged; result["runtime"]["source_post"]=post["source_pre"]
                if not unchanged: result["status"]="failed"; result["source_mutation_detected"]=True
            if "audio_vae" in result:
                after=_audio_model_snapshot(); result["audio_vae_post"]=after
                if (after["sha256"],after["bytes"])!=(result["audio_vae"]["sha256"],result["audio_vae"]["bytes"]): result["status"]="failed"; result["audio_vae_mutation_detected"]=True
            receipt_rel=run_rel+"/audio_diagnostic/receipt.json"
            if PROJECT.resolve(receipt_rel).is_file(): result["audio_diagnostic"]=record(receipt_rel)
            partial=_partial_capture(run_rel)
            if partial: result["audio_diagnostic_partial"]=partial
        except Exception as exc:  # noqa: BLE001 - finalization must report all evidence failures.
            result["finalization_error"]={"type":type(exc).__name__,"message":str(exc)}; result["status"]="failed"
        result["finished_utc"]=utc(); result["wall_seconds"]=time.monotonic()-started
        sample_log=run_rel+"/resources.jsonl"
        if PROJECT.resolve(sample_log).is_file(): result["resource_samples"]=record(sample_log)
        result["runtime"]["driver"]=record(SPIKE+"/vae_roundtrip.py")
        result["resource_observation_scope"]="whole-device GPU, system RAM, Windows commit, C/D disk and owned process tree observations; not demand estimates"
        try: write_json(run_rel+"/roundtrip.json",result)
        except Exception as exc:  # noqa: BLE001 - report if contained final record itself cannot be written.
            result["status"]="failed"
            result["roundtrip_record_write_failure"]={"type":type(exc).__name__,"message":str(exc)}
            print(json.dumps({"status":"roundtrip_record_write_failed","run":run_rel,"error":str(exc)}),flush=True)

def main() -> None:
    parser=argparse.ArgumentParser(description="Bounded native H3 audio VAE round-trip diagnostic")
    parser.add_argument("action",choices=("run","prepare-only")); args=parser.parse_args()
    result=run_roundtrip(prepare_only=args.action=="prepare-only")
    print(json.dumps({"status":result.get("status"),"run":result.get("run"),"roundtrip":result.get("run","")+"/roundtrip.json"}),flush=True)
    if result.get("status") in ("failed","roundtrip_record_write_failed") or result.get("roundtrip_record_write_failure"):
        raise SystemExit(1)
if __name__=="__main__": main()
