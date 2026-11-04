"""Phase 0 spike 03 - does FLUX.2 klein 4B fit, and what does it cost?

Fills three 8.4 rows: `FLUX.2 klein 4B`, `Declared fallback`, and `ComfyUI`.
Also the first real exercise of 8.1's measured-release postcondition and
FR-12's orchestrator-owned ComfyUI lifecycle, rather than a bare generation
test.

Measures, per configuration:
  peak device VRAM over the whole run, sampled by NVML
  the idle/foreign baseline before the run, which drifts with desktop use
  wall clock, split at first sampler progress (encode phase vs diffusion)
  whether POST /free actually returns memory, and how long it takes

Run from the orchestrator venv, never ComfyUI's embedded Python:
  .venv\\Scripts\\python.exe spikes\\03_flux_fit\\run_spike.py [--introspect]
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY_ROOT = r"D:\ComfyUI_3\ComfyUI_windows_portable"
COMFY_PY = os.path.join(COMFY_ROOT, "python_embeded", "python.exe")
COMFY_MAIN = os.path.join(COMFY_ROOT, "ComfyUI", "main.py")
PORT = 8188
BASE = "http://127.0.0.1:%d" % PORT

# Configurations to measure. The distilled model runs 4 steps at cfg 1; the
# base model needs more steps and real guidance (per the bundled template).
CONFIGS = [
    {
        "name": "distilled-4b-fp4enc-1024",
        "unet": "flux-2-klein-4b.safetensors",
        "clip": "qwen_3_4b_fp4_flux2.safetensors",
        "steps": 4,
        "cfg": 1.0,
        "resolution": (1024, 1024),
    },
    {
        "name": "distilled-4b-fp4enc-cctv",
        "unet": "flux-2-klein-4b.safetensors",
        "clip": "qwen_3_4b_fp4_flux2.safetensors",
        "steps": 4,
        "cfg": 1.0,
        # 4.1's delivery geometry at the master spec's 2x, not a square
        "resolution": (1280, 960),
    },
    {
        # the configuration ComfyUI's own template declares; 8.04 GB encoder
        "name": "distilled-4b-fullenc-1024",
        "unet": "flux-2-klein-4b.safetensors",
        "clip": "qwen_3_4b.safetensors",
        "steps": 4,
        "cfg": 1.0,
        "resolution": (1024, 1024),
    },
    {
        # the declared configuration at the geometry this project renders -
        # the measurement 8.4 actually needs
        "name": "distilled-4b-fullenc-cctv",
        "unet": "flux-2-klein-4b.safetensors",
        "clip": "qwen_3_4b.safetensors",
        "steps": 4,
        "cfg": 1.0,
        "resolution": (1280, 960),
    },
]

PROMPT = ("security camera still of an empty apartment hallway at night, "
          "fluorescent ceiling light, closed doors, worn carpet, mundane, "
          "no people")

NODE_TYPES = ["UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode",
              "ConditioningZeroOut", "CFGGuider", "Flux2Scheduler",
              "KSamplerSelect", "RandomNoise", "EmptyFlux2LatentImage",
              "SamplerCustomAdvanced", "VAEDecode", "SaveImage"]


# --------------------------------------------------------------------------
# NVML sampling
# --------------------------------------------------------------------------

class MemorySampler(threading.Thread):
    def __init__(self, interval=0.25):
        super().__init__(daemon=True)
        import pynvml
        self.pynvml = pynvml
        pynvml.nvmlInit()
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()

    def read_gb(self):
        m = self.pynvml.nvmlDeviceGetMemoryInfo(self.handle)
        return m.used / 2 ** 30, m.total / 2 ** 30

    def run(self):
        while not self._stop.is_set():
            used, _ = self.read_gb()
            self.samples.append((time.time(), used))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def peak(self, since=None):
        vals = [u for t, u in self.samples if since is None or t >= since]
        return max(vals) if vals else 0.0


# --------------------------------------------------------------------------
# ComfyUI lifecycle - the orchestrator owns it (FR-12)
# --------------------------------------------------------------------------

def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(path, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
        return json.loads(body) if body.strip() else {}


def already_running():
    try:
        get("/system_stats", timeout=3)
        return True
    except Exception:
        return False


def launch(clean_nodes=False, reserve_gb=None):
    """Named flags per 8.2 - not the shipped run_nvidia_gpu.bat.

    clean_nodes adds --disable-all-custom-nodes. The ambient install carries
    20+ third-party nodes for unrelated projects, and at least one of them
    (smZNodes) monkey-patches comfy.samplers.sampling_function, putting
    itself in the path of every generation.
    """
    args = [COMFY_PY, "-s", COMFY_MAIN,
            "--windows-standalone-build",
            "--port", str(PORT),
            "--disable-auto-launch",
            "--preview-method", "none"]
    if clean_nodes:
        args.append("--disable-all-custom-nodes")
    if reserve_gb:
        # force ComfyUI to leave this much free, so the measurement is a
        # floor rather than 'as much as happened to be available'
        args += ["--reserve-vram", str(reserve_gb)]
    log = open(os.path.join(HERE, "_work", "comfyui.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=os.path.join(COMFY_ROOT, "ComfyUI"),
                            stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < 300:
        if proc.poll() is not None:
            raise SystemExit("ComfyUI exited early; see _work/comfyui.log")
        try:
            get("/system_stats", timeout=3)
            return proc, time.time() - t0
        except Exception:
            time.sleep(1.0)
    proc.terminate()
    raise SystemExit("ComfyUI did not become ready within 300 s")


# --------------------------------------------------------------------------
# workflow
# --------------------------------------------------------------------------

def build_prompt(cfg, seed=20051114):
    w, h = cfg["resolution"]
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": cfg["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": cfg["clip"], "type": "flux2",
                         "device": "default"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": PROMPT, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut",
              "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "CFGGuider",
              "inputs": {"model": ["1", 0], "positive": ["4", 0],
                         "negative": ["5", 0], "cfg": cfg["cfg"]}},
        # Flux2Scheduler takes no model input on this build - checked against
        # /object_info before the first run rather than assumed.
        "7": {"class_type": "Flux2Scheduler",
              "inputs": {"steps": cfg["steps"], "width": w, "height": h}},
        "8": {"class_type": "KSamplerSelect",
              "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "10": {"class_type": "EmptyFlux2LatentImage",
               "inputs": {"width": w, "height": h, "batch_size": 1}},
        "11": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["9", 0], "guider": ["6", 0],
                          "sampler": ["8", 0], "sigmas": ["7", 0],
                          "latent_image": ["10", 0]}},
        "12": {"class_type": "VAEDecode",
               "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "SaveImage",
               "inputs": {"images": ["12", 0],
                          "filename_prefix": "spike03_" + cfg["name"]}},
    }


def introspect():
    """Print each node's required inputs, so a graph mismatch is explicit."""
    info = get("/object_info")
    for t in NODE_TYPES:
        if t not in info:
            print("%-24s MISSING from this ComfyUI build" % t)
            continue
        req = info[t]["input"].get("required", {})
        opt = info[t]["input"].get("optional", {})
        print("%-24s required=%s optional=%s"
              % (t, list(req.keys()), list(opt.keys())))


def run_config(cfg, sampler):
    mark = time.time()
    baseline = sampler.read_gb()[0]
    body = post("/prompt", {"prompt": build_prompt(cfg), "client_id": "spike03"})
    pid = body.get("prompt_id")
    if not pid:
        return {"name": cfg["name"], "error": "no prompt_id", "response": body}

    t0 = time.time()
    result, err = None, None
    while time.time() - t0 < 1800:
        hist = get("/history/%s" % pid)
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error" or status.get("completed") is False:
                err = status
            result = entry
            break
        time.sleep(0.5)
    elapsed = time.time() - t0

    out = {
        "name": cfg["name"],
        "unet": cfg["unet"],
        "clip": cfg["clip"],
        "resolution": list(cfg["resolution"]),
        "steps": cfg["steps"],
        "baseline_gb": round(baseline, 2),
        "peak_gb": round(sampler.peak(since=mark), 2),
        "seconds": round(elapsed, 1),
        "completed": bool(result and not err),
    }
    if err:
        msgs = []
        for m in err.get("messages", []):
            msgs.append(str(m)[:300])
        out["error"] = msgs or err
    return out


def measured_release(sampler, floor_gb, timeout=60):
    """8.1's postcondition: /free then ASSERT, do not assume."""
    t0 = time.time()
    post("/free", {"unload_models": True, "free_memory": True})
    while time.time() - t0 < timeout:
        used, _ = sampler.read_gb()
        if used <= floor_gb:
            return {"released": True, "seconds": round(time.time() - t0, 1),
                    "used_gb": round(used, 2), "floor_gb": round(floor_gb, 2)}
        time.sleep(0.5)
    used, _ = sampler.read_gb()
    return {"released": False, "seconds": timeout,
            "used_gb": round(used, 2), "floor_gb": round(floor_gb, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--introspect", action="store_true")
    ap.add_argument("--keep-running", action="store_true")
    ap.add_argument("--clean-nodes", action="store_true",
                    help="launch with --disable-all-custom-nodes")
    ap.add_argument("--reserve", type=float, default=None,
                    help="GB of VRAM ComfyUI must leave free")
    args = ap.parse_args()

    os.makedirs(os.path.join(HERE, "_work"), exist_ok=True)
    sampler = MemorySampler()
    used, total = sampler.read_gb()
    print("device total %.2f GB | idle/foreign baseline %.2f GB | headroom %.2f GB"
          % (total, used, total - used))
    idle_floor = used + 0.5
    sampler.start()

    proc = None
    if already_running():
        print("ComfyUI already serving on %d - using it, not launching" % PORT)
    else:
        proc, boot = launch(args.clean_nodes, args.reserve)
        print("ComfyUI launched, ready in %.1f s (pid %d)" % (boot, proc.pid))

    stats = get("/system_stats")
    print("ComfyUI %s | torch device: %s"
          % (stats.get("system", {}).get("comfyui_version", "?"),
             (stats.get("devices") or [{}])[0].get("name", "?")))

    try:
        if args.introspect:
            introspect()
            return 0

        results = []
        models = os.path.join(COMFY_ROOT, "ComfyUI", "models")
        for cfg in CONFIGS:
            missing = [f for f, sub in ((cfg["unet"], "diffusion_models"),
                                        (cfg["clip"], "text_encoders"))
                       if not os.path.exists(os.path.join(models, sub, f))]
            if missing:
                print("\n=== %s === SKIPPED, not on disk: %s"
                      % (cfg["name"], ", ".join(missing)))
                results.append({"name": cfg["name"], "skipped": missing})
                continue
            print("\n=== %s ===" % cfg["name"])
            r = run_config(cfg, sampler)
            r["release"] = measured_release(sampler, idle_floor)
            results.append(r)
            print(json.dumps(r, indent=2))

        report = {
            "device_total_gb": round(total, 2),
            "idle_baseline_gb": round(used, 2),
            "comfyui_version": stats.get("system", {}).get("comfyui_version"),
            "custom_nodes_disabled": args.clean_nodes,
            "reserve_vram_gb": args.reserve,
            "configs": results,
        }
        name = "results_reserve%s.json" % (args.reserve or "none")
        with open(os.path.join(HERE, name), "w") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print("\nwrote results.json")
        return 0
    finally:
        sampler.stop()
        if proc and not args.keep_running:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("ComfyUI terminated")


if __name__ == "__main__":
    sys.exit(main())
