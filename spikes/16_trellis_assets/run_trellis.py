"""First TRELLIS2 generation: entity reference image -> textured .glb.

Built from the pack's own Simple.json graph, but as an API-format prompt with the
settings this machine needs:

  backend        = flash_attn   (object_info defaults to xformers, which is NOT installed)
  model_format   = GGUF Q8_0    (matches the verified local weights; the user's supplied
                                 workflow asked for Q4_K_M, which would re-download)
  low_vram       = True         (12,227 MiB card with a drifting foreign baseline)
  keep_models_loaded = False    (release after the run)
  remove_background = True      (the FLUX reference has a white background, not alpha)

FLUX is unloaded first: it peaked at 11,354 MiB and nothing else would fit beside it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8189"
IMAGE = "entity_ref_00001_.png"
SEED = 70707

GRAPH = {
    "6": {"class_type": "Trellis2LoadImageWithTransparency_GGUF",
          "inputs": {"image": IMAGE}},

    "44": {"class_type": "Trellis2PreProcessImage_GGUF",
           "inputs": {"image": ["6", 2], "padding": 0, "remove_background": True}},

    "39": {"class_type": "Trellis2LoadModel_GGUF",
           "inputs": {"modelname": "TRELLIS.2-4B", "model_format": "GGUF Q8_0",
                      "backend": "flash_attn", "device": "cuda",
                      "low_vram": True, "keep_models_loaded": False}},

    "41": {"class_type": "Trellis2MeshWithVoxelGenerator_GGUF",
           "inputs": {"pipeline": ["39", 0], "image": ["44", 0], "seed": SEED,
                      "pipeline_type": "1024_cascade",
                      "sparse_structure_steps": 12, "shape_steps": 12,
                      "texture_steps": 12, "max_num_tokens": 49152,
                      "sparse_structure_resolution": 32, "max_views": 4,
                      "generate_texture_slat": True, "use_tiled_decoder": True}},

    "43": {"class_type": "Trellis2PostProcessAndUnWrapAndRasterizer_GGUF",
           "inputs": {"mesh": ["41", 0], "bvh": ["41", 1],
                      "mesh_cluster_threshold_cone_half_angle_rad": 60.0,
                      "mesh_cluster_refine_iterations": 0,
                      "mesh_cluster_global_iterations": 1,
                      "mesh_cluster_smooth_strength": 1,
                      # 2048 rather than 4096, and 500k faces rather than 2M: this is a
                      # first run on a 12 GB card, and the figure is a simple form.
                      "texture_size": 2048,
                      "remesh": True, "remesh_band": 1.0, "remesh_project": 0.0,
                      "target_face_num": 500000, "simplify_method": "Cumesh",
                      "fill_holes": True, "texture_alpha_mode": "OPAQUE",
                      "dual_contouring_resolution": "1024",
                      "double_side_material": False, "remove_floaters": True,
                      "bake_on_vertices": False, "use_custom_normals": False,
                      "uv_unwrap_method": "Xatlas", "remove_inner_faces": True}},

    "19": {"class_type": "Trellis2ExportMesh_GGUF",
           "inputs": {"trimesh": ["43", 0], "filename_prefix": "3D/entity",
                      "file_format": "glb"}},
}


def post(path: str, payload: dict, timeout: float = 180.0) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "trellis/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def get(path: str, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "trellis/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def gpu_used_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def wait_for_server(deadline_s: float = 300.0) -> bool:
    """ComfyUI takes ~30s to start; posting before then just gets WinError 10061."""
    started = time.monotonic()
    while time.monotonic() - started < deadline_s:
        try:
            get("/system_stats", timeout=10)
            print(f"server ready after {time.monotonic() - started:.0f}s")
            return True
        except Exception:
            time.sleep(3)
    return False


def main() -> int:
    if not wait_for_server():
        print("server never came up on :8189")
        return 1

    print("unloading FLUX from VRAM...", flush=True)
    try:
        post("/free", {"unload_models": True, "free_memory": True}, timeout=120)
    except Exception as exc:  # noqa: BLE001 - not fatal, just report
        print(f"  /free complained: {exc}")
    time.sleep(5)

    baseline = gpu_used_mib()
    print(f"GPU in use after unload: {baseline} MiB\n", flush=True)

    try:
        res = post("/prompt", {"prompt": GRAPH, "client_id": "trellis-first"})
    except urllib.error.HTTPError as exc:
        print("REFUSED by /prompt:\n" + exc.read().decode("utf-8", errors="replace")[:3000])
        return 1
    pid = res["prompt_id"]
    print(f"queued prompt {pid}", flush=True)

    peak = baseline or 0
    started = time.monotonic()
    last_report = 0.0
    while time.monotonic() - started < 5400:
        used = gpu_used_mib()
        if used is not None:
            peak = max(peak, used)

        elapsed = time.monotonic() - started
        if elapsed - last_report >= 30:
            last_report = elapsed
            print(f"  t={elapsed:6.0f}s  gpu={used} MiB  peak={peak} MiB", flush=True)

        hist = get(f"/history/{pid}")
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            print(f"\nfinished in {elapsed:.0f}s | status={status.get('status_str')}")
            print(f"peak GPU memory: {peak} MiB (baseline {baseline} MiB, "
                  f"~{peak - (baseline or 0)} MiB attributable)")

            if status.get("status_str") != "success":
                print("\n--- failure detail ---")
                for msg in status.get("messages", [])[-8:]:
                    print("  ", str(msg)[:600])
                return 1

            print("\n--- outputs ---")
            print(json.dumps(entry.get("outputs", {}), indent=2)[:2500])
            return 0
        time.sleep(5)

    print("timed out")
    return 1


if __name__ == "__main__":
    sys.exit(main())
