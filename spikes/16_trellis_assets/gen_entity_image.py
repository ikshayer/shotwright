"""Generate one entity reference image with FLUX schnell, measuring VRAM as it runs.

Single image on purpose. TRELLIS reconstructs from one view, and the two-view workflow
would need two *consistent* views of a subject that does not exist yet -- two independent
generations disagree and the mesh melts. One image sidesteps that entirely.

The prompt is written for reconstructability rather than for drama: isolated subject,
plain background, even lighting, whole figure in frame. Those are what an image-to-3D
model needs; a moody dark shot gives it no geometry to read.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8189"
SEED = 70707

PROMPT = (
    "full body photograph of a tall gaunt humanoid figure, pale grey featureless matte "
    "skin, elongated thin limbs, standing upright in a neutral symmetrical A-pose, "
    "no facial features, smooth head, centered in frame, isolated on a plain flat white "
    "background, even diffuse studio lighting, entire figure visible from head to feet, "
    "sharp focus, photographic, neutral perspective"
)

GRAPH = {
    "1": {"class_type": "UNETLoader", "inputs": {
        "unet_name": "flux1-schnell.safetensors", "weight_dtype": "fp8_e4m3fn"}},
    "2": {"class_type": "DualCLIPLoader", "inputs": {
        "clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
        "clip_name2": "clip_l.safetensors", "type": "flux"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": PROMPT, "clip": ["2", 0]}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
    "5": {"class_type": "EmptySD3LatentImage", "inputs": {
        "width": 1024, "height": 1024, "batch_size": 1}},
    "6": {"class_type": "KSampler", "inputs": {
        "seed": SEED, "steps": 4, "cfg": 1.0, "sampler_name": "euler",
        "scheduler": "simple", "denoise": 1.0,
        "model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0],
        "latent_image": ["5", 0]}},
    "7": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["7", 0]}},
    "9": {"class_type": "SaveImage", "inputs": {
        "images": ["8", 0], "filename_prefix": "entity_ref"}},
}


def post(path: str, payload: dict, timeout: float = 120.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "gen/1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def get(path: str, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "gen/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def gpu_used_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def main() -> int:
    baseline = gpu_used_mib()
    print(f"GPU in use before submit: {baseline} MiB")

    try:
        res = post("/prompt", {"prompt": GRAPH, "client_id": "flux-entity"})
    except urllib.error.HTTPError as exc:
        print("REFUSED by /prompt:\n" + exc.read().decode("utf-8", errors="replace")[:2500])
        return 1
    pid = res["prompt_id"]
    print(f"queued prompt {pid}\n")

    peak = baseline or 0
    started = time.monotonic()
    while time.monotonic() - started < 1800:
        used = gpu_used_mib()
        if used is not None:
            peak = max(peak, used)

        hist = get(f"/history/{pid}")
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            elapsed = time.monotonic() - started
            print(f"\nfinished in {elapsed:.0f}s | status={status.get('status_str')}")
            print(f"peak GPU memory in use: {peak} MiB (baseline {baseline} MiB)")
            if baseline is not None:
                print(f"attributable to this run: ~{peak - baseline} MiB")

            if status.get("status_str") != "success":
                for msg in status.get("messages", [])[-6:]:
                    print("  ", str(msg)[:300])
                return 1

            for node_id, out in entry.get("outputs", {}).items():
                for img in out.get("images", []):
                    print(f"  image: {img['filename']}  (subfolder={img.get('subfolder','')!r}, {img.get('type')})")
            return 0
        time.sleep(4)

    print("timed out after 1800s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
