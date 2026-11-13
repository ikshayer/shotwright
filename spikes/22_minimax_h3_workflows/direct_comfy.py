"""Prepare a direct Easy browser workflow and supervise its isolated Comfy server.

Never submits a prompt. The creator queues work from the actual Comfy frontend.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.domain.seeds import derive_seed
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
SPIKE = "spikes/22_minimax_h3_workflows"
BASE = SPIKE + "/_work/direct_comfy"
RUNTIME = SPIKE + "/_work/local_runtime"
COMFY = RUNTIME + "/source/ComfyUI"
PLUGIN = COMFY + "/custom_nodes/ComfyUI-MiniMaxH3-Easy"
PORT = 8298
URL = f"http://127.0.0.1:{PORT}"
GIB = 2**30
PROMPT = ("Use <Video 1> as the visual reference. A realistic fixed security-camera view of an empty room. "
          "Preserve the room layout, furniture, lighting, camera position and quiet atmosphere. "
          "Audio: clearly audible continuous low electrical room hum and soft ventilation airflow throughout "
          "the entire eight seconds, naturally matched to this indoor room. No speech, voices or music. "
          "No cuts, camera movement, new people or dramatic sound events.")


def h3_module() -> Any:
    spec = importlib.util.spec_from_file_location("_direct_comfy_h3", PROJECT.resolve(SPIKE + "/local_h3.py"))
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load existing local H3 process helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(rel: str, obj: Any) -> None:
    PROJECT.write_text(rel, json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def read_json(rel: str) -> Any:
    return json.loads(PROJECT.read_text(rel))


def optimizer_disabled() -> None:
    # The backend global config, not the UI checkbox, controls optimize-on-run.
    path = PROJECT.resolve(PLUGIN + "/prompt_optimizer.json")
    if path.exists() and bool(json.loads(path.read_text(encoding="utf-8")).get("optimize_on_run", False)):
        raise ValueError("Easy backend prompt optimization is enabled; no source/config mutation performed")


def session_path(rel: str) -> str:
    path = PROJECT.resolve(rel)
    base = PROJECT.resolve(BASE)
    if path.parent != base or not path.is_dir():
        raise ValueError("Expected an existing direct_comfy session immediately below its contained root")
    return str(path.relative_to(ROOT)).replace("\\", "/")


def prepare() -> str:
    h3 = h3_module()
    optimizer_disabled()
    run = BASE + "/" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "_" + uuid.uuid4().hex[:8]
    PROJECT.make_dir(run)
    components = []
    for row in h3.catalog()["components"]:
        actual = h3.identity(h3.MODEL + "/" + row["model_relative_path"])
        if (actual["bytes"], actual["sha256"]) != (row["expected_bytes"], row["expected_sha256"]):
            raise ValueError("Model identity differs: " + row["role"])
        components.append({"role": row["role"], **actual})
    prior = SPIKE + "/_work/local_runs/20261110T060837Z_aadcfd60"
    generation = read_json(prior + "/generation.json")
    proxy = generation["sources"]["proxy"]
    h3.checked(proxy)
    h3.check_silent(h3.probe(proxy["path"], run, "source_proxy_probe"), 384, 288)
    PROJECT.write_bytes(run + "/input/model_proxy.mp4", PROJECT.resolve(proxy["path"]).read_bytes())
    manifest = read_json(h3.INPUT)
    write_json(run + "/input/source_manifest.json", manifest)
    write_json(run + "/input/source_provenance.json", generation["sources"])
    write_json(run + "/input/rights_notices.json", h3.source_notices(manifest))
    info = read_json(RUNTIME + "/receipts/object_info_easy.json")
    maximum = min(info["RandomNoise"]["input"]["required"]["noise_seed"][1]["max"], 2**53 - 1)
    seed = derive_seed("s1", 0, "minimax_h3_ambience", max_value=maximum)
    names = {row["role"]: Path(row["path"]).name for row in components}
    graph = {
        "1": {"class_type": "MiniMaxH3EasyLoader", "inputs": {
            "fl2va_model": "none", "ref2va_model": names["diffusion"], "text_encoder": names["text_encoder"],
            "video_vae": names["video_vae"], "audio_vae": names["audio_vae"]}},
        "2": {"class_type": "MiniMaxH3Easy", "inputs": {
            "h3_bundle": ["1", 0], "media": ["41", 0], "mode": "reference", "prompt": PROMPT,
            "resolution": "custom", "aspect_ratio": "4:3", "width": 384, "height": 288, "seconds": 8.0,
            "advanced": True, "fps": 24.0, "keyframe_role": "first", "ref_image_size": "match",
            "reference_mention_mode": "index", "prompt_optimizer_settings": False,
            "prompt_optimizer_scene_guide": "none"}},
        "13": {"class_type": "MiniMaxH3EasyOutput", "inputs": {"h3_context": ["2", 1]}},
        "4": {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["13", 0]}},
        "5": {"class_type": "BasicScheduler", "inputs": {"model": ["2", 0], "scheduler": "simple", "steps": 20, "denoise": 1.0}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "8": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["6", 0], "guider": ["4", 0], "sampler": ["7", 0], "sigmas": ["5", 0], "latent_image": ["13", 1]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["13", 2]}},
        "10": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["13", 3]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "audio": ["10", 0], "fps": ["13", 4]}},
        "12": {"class_type": "SaveVideo", "inputs": {"video": ["11", 0], "filename_prefix": "direct_easy", "format": "mp4", "format.codec": "h264"}},
        "40": {"class_type": "LoadVideo", "inputs": {"file": "model_proxy.mp4"}},
        "41": {"class_type": "MiniMaxH3EasyMediaBridge", "inputs": {"image_count": 0, "video_count": 1, "audio_count": 0, "video_1": ["40", 0]}},
    }
    template_rel = PLUGIN + "/workflow/1.MiniMax_H3_Easy.json"
    workflow = copy.deepcopy(read_json(template_rel))
    workflow["id"] = str(uuid.uuid4())
    workflow["revision"] = 0
    workflow["nodes"] = [n for n in workflow["nodes"] if n["id"] not in (15, 24, 25, 34)]
    workflow["links"] = [l for l in workflow["links"] if l[0] not in (27, 28, 36)]
    for link in workflow["links"]:
        if link[0] == 12:
            link[2] = 0
        if link[0] in (37, 38):
            link[1:3] = [2, 0]
    by_id = {n["id"]: n for n in workflow["nodes"]}
    by_id[1]["widgets_values"] = list(graph["1"]["inputs"].values())
    named = {k: v for k, v in graph["2"]["inputs"].items() if k not in ("h3_bundle", "media")}
    by_id[2]["widgets_values_named"] = named
    by_id[2]["widgets_values"] = ["reference", PROMPT, None, "custom", "4:3", 384, 288, 8.0, True, 24.0, "first", "match", "index", False, "none"]
    by_id[2]["properties"] = {"Node name for S&R": "MiniMaxH3Easy",
        "minimax_h3_prompt_reference_doc": {"version": 1, "text": PROMPT, "parts": [{"type": "text", "text": PROMPT}]},
        "minimax_h3_prompt_optimizer": {"enabled": False, "read_media": False, "scene_guide": "none"}}
    by_id[2]["inputs"][1]["link"] = 41
    by_id[2]["outputs"][0]["links"] = [37, 38]
    by_id[6]["widgets_values"] = [seed, "fixed"]
    by_id[5]["widgets_values"] = ["simple", 20, 1.0]
    by_id[7]["widgets_values"] = ["res_multistep"]
    by_id[8]["outputs"][0]["links"] = [11, 12]
    by_id[8]["outputs"][1]["links"] = []
    by_id[11]["outputs"][0]["links"] = [17]
    by_id[11]["widgets_values"] = [24.0]
    by_id[12]["widgets_values"] = ["direct_easy", "mp4", "h264"]
    by_id[2]["size"] = [460, 650]
    bridge_inputs = [{"name": f"{kind}_{i}", "type": "*", "link": 40 if (kind, i) == ("video", 1) else None}
                     for kind, count in (("image", 9), ("video", 3), ("audio", 3)) for i in range(1, count + 1)]
    workflow["nodes"].extend([
        {"id": 40, "type": "LoadVideo", "pos": [-1000, 0], "size": [320, 300], "flags": {}, "order": 0, "mode": 0,
         "inputs": [], "outputs": [{"name": "VIDEO", "type": "VIDEO", "links": [40]}], "properties": {"Node name for S&R": "LoadVideo"}, "widgets_values": ["model_proxy.mp4"]},
        {"id": 41, "type": "MiniMaxH3EasyMediaBridge", "pos": [-600, 0], "size": [300, 360], "flags": {}, "order": 1, "mode": 0,
         "inputs": bridge_inputs, "outputs": [{"name": "media_bundle", "type": "MINIMAX_H3_MEDIA_BUNDLE", "links": [41]}],
         "properties": {"Node name for S&R": "MiniMaxH3EasyMediaBridge"}, "widgets_values": [0, 1, 0]},
    ])
    workflow["links"].extend([[40, 40, 0, 41, 9, "VIDEO"], [41, 41, 0, 2, 1, "MINIMAX_H3_MEDIA_BUNDLE"]])
    workflow["last_node_id"] = 41
    workflow["last_link_id"] = 41
    workflow["groups"] = []
    workflow["extra"] = {"ds": {"scale": 0.65, "offset": [1050, 650]}}
    PROJECT.write_text(run + "/prompt.txt", PROMPT + "\n")
    write_json(run + "/workflow.json", workflow)
    write_json(run + "/api_graph.json", graph)
    for leaf in ("output", "temp", "user/default/workflows", "previews", "database"):
        PROJECT.make_dir(run + "/" + leaf)
    write_json(run + "/user/default/workflows/MiniMaxH3_Easy_Direct.json", workflow)
    receipt = {"status": "prepared_not_queued", "session": run, "created_utc": h3.utc(), "url": URL,
               "source_template": h3.identity(template_rel), "workflow": h3.identity(run + "/workflow.json"),
               "api_graph": h3.identity(run + "/api_graph.json"), "proxy": h3.identity(run + "/input/model_proxy.mp4"),
               "models": components, "seed": {"shot_id": "s1", "degrade_seed": 0, "stage": "minimax_h3_ambience", "max_value": maximum, "value": seed},
               "comparison": "New direct Easy browser test, not a same-seed controlled A/B; browser seed clamped to JS safe integer range.",
               "memory": "Easy retains clip and VAEs and loads diffusion before conditioning; this direct route has no pipeline stage-release nodes and memory fit is unverified.",
               "optimizer": "Backend config absent or optimize_on_run false; workflow optimizer disabled; offline launch.",
               "notes": ["Removed unused image, RAMCleanup, turbo LoRA and attention patch", "Only downloaded Ref2VA NVFP4 weights selected; FL2VA none", "Explicit silent video MediaBridge, no audio input", "Custom384x288x192 at24fps; res_multistep20/simple/denoise1", "Both decoders use sampler output0; joint audio/video MP4 h264", "No generation queued"],
               "code": h3.identity(SPIKE + "/direct_comfy.py"), "helper": h3.identity(SPIKE + "/local_h3.py")}
    write_json(run + "/prepared.json", receipt)
    PROJECT.write_text(run + "/README.txt", f"Open {URL}; load workflow.json (also saved in workflow browser). Press Run once to test.\nOutput is under this session/output. No inference has been queued by preparation.\nServer stops on RAM safety floor, per-job30min deadline or2h idle.\nStop via: python spikes/22_minimax_h3_workflows/direct_comfy.py stop --session {run}\n")
    print(json.dumps({"status": receipt["status"], "session": run, "url": URL}), flush=True)
    return run


def http(endpoint: str, timeout: float = 3.0) -> Any:
    request = urllib.request.Request(URL + endpoint)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.loads(response.read())


def serve(rel: str) -> None:
    run = session_path(rel)
    h3 = h3_module()
    optimizer_disabled()
    prep = read_json(run + "/prepared.json")
    for item in [prep["code"], prep["helper"], prep["workflow"], prep["api_graph"], prep["proxy"]]:
        h3.checked(item)
    if PROJECT.resolve(run + "/started.json").exists():
        raise ValueError("Session already served; prepare a fresh session instead of replacing lifecycle evidence")
    with socket.socket() as check:
        check.bind(("127.0.0.1", PORT))
    gpu = h3.PynvmlProbe()
    baseline = h3.resources(gpu)
    if baseline["ram_available_bytes"] < 6 * GIB:
        raise MemoryError("Available RAM below6GiB admission floor; no server started")
    env = h3.clean_env(run)
    args = [str(PROJECT.resolve(RUNTIME + "/env/Scripts/python.exe")), "-B", str(PROJECT.resolve(COMFY + "/main.py")),
            "--listen", "127.0.0.1", "--port", str(PORT), "--offline", "--disable-all-custom-nodes",
            "--whitelist-custom-nodes", "ComfyUI-MiniMaxH3-Easy", "--cuda-device", "0", "--use-pytorch-cross-attention",
            "--fast-disk", "--disable-pinned-memory", "--cache-none", "--base-directory", str(PROJECT.resolve(COMFY)),
            "--models-directory", str(PROJECT.resolve(h3.MODEL))]
    for leaf in ("input", "output", "temp", "user", "previews"):
        args.extend([f"--{leaf}-directory", str(PROJECT.resolve(run + "/" + leaf))])
    args.extend(["--database-url", "sqlite:///" + str(PROJECT.resolve(run + "/database/comfy.db")).replace("\\", "/")])
    stop = threading.Event()
    # Direct Easy startup reached frontend import at180s without model work; allow a bounded600s import window.
    state: dict[str, Any] = {"phase": "startup", "deadline": time.monotonic() + 600, "failure": None}
    tree = None
    process = None
    watchdog = None
    result: dict[str, Any] = {"status": "starting", "started_utc": h3.utc(), "session": run, "url": URL,
                              "command": args, "baseline": baseline, "queued_by_launcher": False}
    def watch() -> None:
        low = 0
        while not stop.is_set():
            try:
                snapshot = tree.refresh()
                if not any(p["live_identity_verified"] for p in snapshot):
                    raise RuntimeError("Owned Comfy process tree exited")
                reading = h3.resources(gpu, tree)
                PROJECT.append_line(run + "/resources.jsonl", json.dumps(reading))
                low = low + 1 if reading["ram_available_bytes"] < 2 * GIB else 0
                if low >= 3:
                    raise MemoryError("Available RAM below2GiB for3samples")
                if PROJECT.resolve(run + "/STOP").exists():
                    raise InterruptedError("Creator requested session stop")
                if time.monotonic() > state["deadline"]:
                    raise TimeoutError("Session deadline exceeded during " + state["phase"])
            except Exception as exc:  # noqa: BLE001 - watchdog retains and stops on every observation failure.
                state["failure"] = {"type": type(exc).__name__, "error": str(exc), "utc": h3.utc()}
                stop.set()
                try:
                    tree.stop()
                except Exception as cleanup_error:  # noqa: BLE001 - cleanup errors retained separately.
                    state["watchdog_cleanup_error"] = str(cleanup_error)
                return
            stop.wait(1.0)
    try:
        PROJECT.write_bytes(run + "/server.log", b"")
        with PROJECT.resolve(run + "/server.log").open("ab") as log:
            process = subprocess.Popen(args, cwd=PROJECT.resolve(COMFY), env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            tree = h3.OwnedProcessTree(process)
            tree.refresh()
            result["owned_process_identities"] = tree.snapshot()
            write_json(run + "/started.json", result)
            watchdog = threading.Thread(target=watch, daemon=True)
            watchdog.start()
            while not stop.is_set():
                try:
                    stats = http("/system_stats")
                    break
                except (OSError, ValueError):
                    stop.wait(0.5)
            if stop.is_set():
                raise RuntimeError("Server startup failed: " + json.dumps(state["failure"]))
            if not any("RTX 5070 Ti Laptop" in d.get("name", "") and d.get("type") == "cuda" for d in stats.get("devices", [])):
                raise RuntimeError("Comfy did not report the expected CUDA GPU; no CPU fallback accepted")
            info = http("/object_info", timeout=30)
            graph = read_json(run + "/api_graph.json")
            missing = sorted({v["class_type"] for v in graph.values()} - set(info))
            if missing:
                raise ValueError("Runtime lacks required workflow nodes: " + ",".join(missing))
            write_json(run + "/object_info.json", info)
            result.update({"status": "ready_not_queued", "ready_utc": h3.utc(), "system_stats": stats,
                           "owned_process_identities": tree.snapshot(), "queue_at_ready": http("/queue")})
            state.update({"phase": "browser_idle", "deadline": time.monotonic() + 7200})
            write_json(run + "/ready.json", result)
            print(json.dumps({"status": "ready_not_queued", "session": run, "url": URL}), flush=True)
            active = False
            while not stop.wait(1.0):
                queue = http("/queue")
                busy = bool(queue.get("queue_running") or queue.get("queue_pending"))
                if busy and not active:
                    state.update({"phase": "browser_execution", "deadline": time.monotonic() + 1800})
                    PROJECT.append_line(run + "/queue_events.jsonl", json.dumps({"utc": h3.utc(), "event": "creator_queue_active", "queue": queue}))
                elif active and not busy:
                    state.update({"phase": "browser_idle", "deadline": time.monotonic() + 7200})
                    PROJECT.append_line(run + "/queue_events.jsonl", json.dumps({"utc": h3.utc(), "event": "creator_queue_finished"}))
                active = busy
    except BaseException as exc:
        result["error"] = {"type": type(exc).__name__, "error": str(exc)}
        raise
    finally:
        stop.set()
        if watchdog is not None:
            watchdog.join(timeout=25)
        result["watchdog_failure"] = state["failure"]
        result["watchdog_cleanup_error"] = state.get("watchdog_cleanup_error")
        try:
            if tree is not None:
                result["cleanup"] = tree.stop()
        finally:
            result.update({"status": "stopped", "finished_utc": h3.utc(), "watchdog_joined": watchdog is None or not watchdog.is_alive()})
            write_json(run + "/stopped.json", result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "serve", "stop"))
    parser.add_argument("--session")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    else:
        if not args.session:
            parser.error("--session required for serve/stop")
        run = session_path(args.session)
        if args.action == "serve":
            serve(run)
        else:
            PROJECT.write_text(run + "/STOP", "Creator requested stop\n")
            print(json.dumps({"status": "stop_requested", "session": run}), flush=True)


if __name__ == "__main__":
    main()
