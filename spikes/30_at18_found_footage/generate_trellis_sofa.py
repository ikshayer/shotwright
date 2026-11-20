"""Generate offline image and TRELLIS candidates using the pinned spike 16 runtime."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import uuid
from copy import deepcopy
from pathlib import Path

import psutil

from orchestrator.adapters.comfyui.runtime import HttpTransport
from orchestrator.adapters.comfyui.workflow import _validate_input
from orchestrator.jobs.gpu_slot import GpuExecutionSlot
from orchestrator.jobs.lease import PynvmlProbe
from orchestrator.jobs.queue import SystemHostResources
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

REPO = Path(__file__).resolve().parents[2]
SLOT_ROOT = Path("D:/Work/shotwright")
RUNTIME = Path("D:/Work/comfyui-trellis")
COMFY = RUNTIME / "ComfyUI"
PINNED_WEIGHTS = Path("D:/Work/trellis2-gguf")
FLUX_ROOT = Path("D:/ComfyUI_3/ComfyUI_windows_portable/ComfyUI/models")
ART_WORKTREE = Path(__file__).resolve().parents[2]  # the checkout this spike runs from
SPIKE_WORK = "spikes/30_at18_found_footage/_work"
Z_IMAGE_WORK = f"{SPIKE_WORK}/z-image-handoff"
CABINET_BASELINE = f"{SPIKE_WORK}/expanded-props-6f23ecedff14453b89896609fb3ba0eb"
Z_IMAGE_REVISION = "eaf9ea44e47ed9778f02d8c92efdc398b6cccea3"
Z_IMAGE_CHECKPOINT = "z_image_int8_convrot.safetensors"
Z_IMAGE_ENCODER = "qwen_3_4b.safetensors"
Z_IMAGE_VAE = "ae.safetensors"
Z_IMAGE_SHA256 = {
    "diffusion_models": "eb2b470694fbd9778f15efd309000ea0091312c018c10a352c120b1da9bf4279",
    "text_encoders": "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a",
    "vae": "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38",
}
Z_IMAGE_BLUEPRINT = (
    "https://github.com/Comfy-Org/workflow_templates/blob/main/packages/blueprints/src/"
    "comfyui_subgraph_blueprints/blueprints/text_to_image_z_image_base.json"
)
Z_IMAGE_ASSET = "cabinet_panel"
CABINET_PNG = "output/cabinet_panel_reference_00001_.png"
CABINET_GLB = "output/cabinet_panel_00001_.glb"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BRIA_RELATIVE = "models/bria-rmbg/bria-rmbg.onnx"
BRIA = Path.home() / ".rembg" / BRIA_RELATIVE
BRIA_SHA = "5b486f08200f513f460da46dd701db5fbb47d79b4be4b708a19444bcd4e79958"
TRELLIS_REVISION = "edf81aef93af0823ac8721da3c9cd3c52d539f87"
PORT = 8189
GPU_NAME = "RTX 5070 Ti"
SEED = 70707
# Measured attributable peaks in spike 16. The remaining 512 MiB is a pilot guard.
FLUX_MIB, TRELLIS_MIB, VRAM_MARGIN_MIB = 9773, 2970, 512
PROMPT = (
    "A single realistic compact late Soviet apartment sofa from the 1980s, full object "
    "catalog photograph, front three-quarter view from ordinary seated eye height. "
    "A worn dark olive-brown woven fabric two-seat couch with a faded small repeating "
    "floral jacquard pattern, lightly sagged separate seat cushions, low squared padded "
    "arms with dark brown varnished wooden arm caps, four short wooden legs and worn "
    "piped seams. Modest mass-produced domestic furniture, natural textile variation, "
    "plain uniform white studio background and floor, very even soft diffuse daylight, "
    "minimal contact shadow. Entire sofa clearly visible including both arms and all "
    "visible feet, centered with generous space on each side, uncluttered, sharp focus, "
    "natural perspective. No other props, room, people, writing, text or watermark."
)
ROOM_PROMPT = (
    "One realistic photograph inside a narrow abandoned late Soviet apartment living "
    "room, viewed from ordinary eye level, full-frame interior with recognizable depth "
    "and natural wide-angle perspective. Warm faded mismatched floral wallpaper is "
    "torn and peeling in large irregular patches across both walls, revealing broken "
    "plaster edges, exposed wood lath and old brick. Scarred worn parquet partly buried "
    "under years of dry scattered junk: torn cardboard, crumpled loose paper, unmarked "
    "bottles, plaster chips, discarded cloth, splintered wood, and empty tins. "
    "A centered white wooden inward-opening "
    "casement window, with an ordinary flat apartment-block facade visible outside. "
    "A tilted veneered cabinet and detached cabinet panels lean against the left wall. "
    "On the right a low dresser holds a simple cloth doll and scattered paper clutter. "
    "Dark warm interior tones with soft overcast daylight at the window. Entire room "
    "fills the image edge to edge, architectural photograph with dusty dry surfaces "
    "and real material wear. No person, readable writing, brand, watermark or overlay."
)
PROP_PROMPTS = {
    "dresser": (
        "One old modest late Soviet four-drawer sideboard, dark worn walnut veneer, "
        "four broad drawer fronts with simple aged metal handles and short wood feet. "
        "Faded edges, small chips and scratches, realistically aged dry wood. "
        "Photographic front three-quarter view, entire sideboard centered with margin "
        "on a plain uniform white backdrop, even soft diffuse daylight, no hard shadow. "
        "No objects on top, no other furniture, writing, text, people or logos."
    ),
    "radiator": (
        "One old Soviet cast-iron under-window radiator with ten evenly spaced tall ribbed "
        "sections, worn off-white enamel and subtle rust flecks on edges, short inlet and "
        "outlet fittings. Photographic frontal three-quarter view, whole radiator centered "
        "with margin on a uniform white backdrop with even soft diffuse daylight, no hard "
        "shadow. No room, other objects, writing, text, people or logos."
    ),
    "cabinet_panel": (
        "One old detached Soviet wardrobe door lying flat, rectangular dark amber "
        "varnished wood veneer with a broken corner, a few separated backing slats "
        "still attached and one aged metal hinge. Dry splintered wood, worn chipped "
        "edges and scratched finish. Photographic three-quarter view from above, "
        "entire single piece centered with margin on a plain uniform white background, "
        "soft even diffuse daylight, minimal shadow. No other furniture, text or logos."
    ),
    "debris": (
        "One small cohesive cluster of dry apartment floor debris: curled torn off-white "
        "paper scraps with blank surfaces and several small irregular chunks of rough "
        "cream plaster, pale dust on their edges. Low uneven pile with loose scraps "
        "immediately around it, entirely visible in a close photographic three-quarter "
        "view from above on a plain uniform white background, soft even diffuse daylight, "
        "minimal shadow. No room, litter elsewhere, readable writing, labels or logos."
    ),
    "rubbish": (
        "One compact asymmetric patch of real discarded floor rubbish from a dry "
        "abandoned apartment. Low and spread unevenly like a photographed surface scan: "
        "overlapping crumpled yellowed newspaper with faint illegible grey print, "
        "flattened torn brown cardboard with exposed corrugated edges, several dark "
        "grimy broken plaster shards and thin loose wood splinters tucked among the "
        "papers. Rough soiled creases, dusty uneven surfaces and distinctly separate "
        "materials. Low three-quarter view angled downward enough to show both the "
        "footprint and height of the scraps. Entire cohesive patch visible with margin "
        "on a plain matte neutral grey backdrop, diffuse even daylight, minimal shadow. "
        "No room, other objects, readable text, labels or logos."
    ),
    "doll": (
        "One small worn late Soviet toy doll seated on the ground, legs out in front "
        "and cloth arms relaxed by its sides. A faded orange cloth hood frames a small "
        "weathered molded vinyl toy face, and a dark red fabric coat has coarse seams "
        "and worn cuffs. Faded textile and small scuffs, believable handmade toy scale. "
        "Front three-quarter product photograph, entire doll visible with margin against "
        "a plain uniform white background in soft even diffuse daylight, minimal shadow. "
        "No chair, furniture, human person, text, labels or logos."
    ),
}
BRICK_PROMPT = (
    "Extreme close-up square albedo scan of one small patch of old Soviet interior "
    "brickwork, camera perfectly perpendicular to the wall. In the middle course, "
    "one whole long brick spans almost half the image width, flanked by two partial "
    "neighboring bricks clipped by the left and right edges. The entire frame holds "
    "only about six or seven horizontal courses with roughly staggered joints. Individual "
    "bricks look large in frame, with visible pores, crumbling chipped edges, and "
    "irregular hand-formed surfaces in muted dusty brownish red clay. Recessed old "
    "pale lime mortar has broken uneven edges and small friable pits. Dry, worn original "
    "materials, natural low-saturation color, even diffuse light. Brickwork fills "
    "the image edge to edge. No wide view or repeating swatch of many rows of little "
    "bricks, paint, plaster, props, perspective, text, or watermark."
)
PROMPTS = {"sofa": PROMPT, "room": ROOM_PROMPT, "brick-texture": BRICK_PROMPT, **PROP_PROMPTS}
PROP_BATCHES = {
    "props": ("dresser", "radiator"),
    "expanded-props": ("cabinet_panel", "debris", "doll"),
}


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def record(root: ProjectRoot, name: str, data: object) -> None:
    root.write_text(name, json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")


def model_receipts(
    *, include_flux: bool = True, include_bria: bool = True, image_only: bool = False
) -> dict:
    flux = []
    if include_flux:
        for relative in (
            "unet/flux1-schnell.safetensors", "clip/t5xxl_fp8_e4m3fn.safetensors",
            "clip/clip_l.safetensors", "vae/ae.safetensors",
        ):
            path = FLUX_ROOT / relative
            flux.append({"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)})
    if image_only:
        return {"trellis": None, "flux_local_files": flux, "background_removal": None}

    installed = COMFY / "models/Trellis2"
    provenance = json.loads((PINNED_WEIGHTS / "PROVENANCE.json").read_text(encoding="utf-8"))
    if provenance["revision"] != TRELLIS_REVISION:
        raise ValueError("TRELLIS source provenance revision differs from spike 16")
    files = []
    for entry in provenance["files"]:
        relative = entry["path"]
        if relative.startswith("Vision/"):
            local = installed / "dinov3/facebook/dinov3-vitl16-pretrain-lvd1689m/model.safetensors"
        else:
            local = installed / relative
            if local.resolve(strict=True) != (PINNED_WEIGHTS / relative).resolve(strict=True):
                raise ValueError(f"TRELLIS checkpoint alias differs from the recorded pin: {relative}")
        if local.stat().st_size != entry["size"] or sha256(local) != entry["sha256"]:
            raise ValueError(f"TRELLIS checkpoint size or SHA-256 mismatch: {relative}")
        files.append({**entry, "installed_path": str(local)})
        if not relative.startswith("Vision/"):
            config = local.with_suffix(".json") if local.suffix != ".gguf" else local.with_name(
                local.name.removesuffix("_Q8_0.gguf") + ".json"
            )
            files.append({"installed_path": str(config), "sha256": sha256(config)})
    for relative in (
        "pipeline.json",
        "dinov3/facebook/dinov3-vitl16-pretrain-lvd1689m/config.json",
        "dinov3/facebook/dinov3-vitl16-pretrain-lvd1689m/preprocessor_config.json",
    ):
        path = installed / relative
        files.append({"installed_path": str(path), "sha256": sha256(path)})
    if include_bria and sha256(BRIA) != BRIA_SHA:
        raise ValueError("local BRIA rembg checkpoint SHA-256 mismatch")
    return {
        "trellis": {**provenance, "files": files}, "flux_local_files": flux,
        "background_removal": {"path": str(BRIA), "sha256": BRIA_SHA} if include_bria else None,
    }


def z_image_model_receipts(repo: ProjectRoot) -> dict:
    files = {
        "diffusion_models": repo.resolve(
            f"{Z_IMAGE_WORK}/models/diffusion_models/{Z_IMAGE_CHECKPOINT}"
        ),
        "text_encoders": FLUX_ROOT / "text_encoders" / Z_IMAGE_ENCODER,
        "vae": FLUX_ROOT / "vae" / Z_IMAGE_VAE,
    }
    names = {
        "diffusion_models": Z_IMAGE_CHECKPOINT, "text_encoders": Z_IMAGE_ENCODER, "vae": Z_IMAGE_VAE,
    }
    models = {}
    for category, path in files.items():
        resolved = path.resolve(strict=True)
        if resolved.name != names[category] or not resolved.is_file():
            raise ValueError(f"Z-Image model does not resolve to the approved filename: {path}")
        digest = sha256(resolved)
        if digest != Z_IMAGE_SHA256[category]:
            raise ValueError(f"Z-Image model SHA-256 mismatch: {resolved}")
        models[category] = {
            "path": str(resolved), "bytes": resolved.stat().st_size, "sha256": digest,
        }
    return {
        "files": models, "blueprint": Z_IMAGE_BLUEPRINT, "checkpoint_revision": Z_IMAGE_REVISION,
        "checkpoint_source": (
            f"https://huggingface.co/Comfy-Org/Z-Image/resolve/{Z_IMAGE_REVISION}/"
            f"split_files/diffusion_models/{Z_IMAGE_CHECKPOINT}"
        ),
    }


def z_image_model_paths(root: ProjectRoot, models: dict) -> Path:
    # Comfy loads this after its installation config and prepends default model folders.
    folders = {
        category: Path(entry["path"]).parent.as_posix()
        for category, entry in models["files"].items()
    }
    # JSON is a valid YAML document and preserves literal Windows paths.
    config = {"z_image_cabinet_trial": {"is_default": True, **folders}}
    return root.write_text("z-image-model-paths.yaml", json.dumps(config, indent=2) + "\n")


def z_image_graph() -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": Z_IMAGE_CHECKPOINT, "weight_dtype": "default",
        }},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": Z_IMAGE_ENCODER, "type": "lumina2",
        }},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": Z_IMAGE_VAE}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": PROP_PROMPTS[Z_IMAGE_ASSET],
        }},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ""}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": 768, "height": 768, "batch_size": 1,
        }},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
            "latent_image": ["7", 0], "seed": SEED, "steps": 25, "cfg": 4,
            "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1,
        }},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {
            "images": ["9", 0], "filename_prefix": f"{Z_IMAGE_ASSET}_reference",
        }},
    }


def workflow(stage: str, schema: dict, asset: str = "sofa", *, image_only: bool = False) -> dict:
    if stage == "flux":
        graph = deepcopy(importlib.import_module("spikes.16_trellis_assets.gen_entity_image").GRAPH)
        graph["3"]["inputs"]["text"] = PROMPTS[asset]
        if image_only or asset in PROP_PROMPTS:
            graph["5"]["inputs"].update(width=768, height=768)
        graph["6"]["inputs"]["seed"] = SEED
        graph["9"]["inputs"]["filename_prefix"] = f"{asset}_reference"
    elif stage == "z-image":
        graph = z_image_graph()
    else:
        graph = deepcopy(importlib.import_module("spikes.16_trellis_assets.run_trellis").GRAPH)
        graph["6"]["inputs"]["image"] = f"{asset}_reference.png"
        if asset == "room":
            # The preload node outputs full RGB. The foreground preprocessor
            # always crops a bounding box, even with remove_background disabled.
            del graph["44"]
            graph["41"]["inputs"]["image"] = ["6", 0]
        graph["41"]["inputs"]["seed"] = SEED
        graph["43"]["inputs"].update(target_face_num=25000, texture_size=1024)
        if asset in PROP_PROMPTS:
            graph["41"]["inputs"]["pipeline_type"] = "512"
            graph["43"]["inputs"].update(
                target_face_num=10000, texture_size=512, dual_contouring_resolution="512"
            )
        graph["19"]["inputs"]["filename_prefix"] = asset
    for node in graph.values():
        name, supplied = node["class_type"], node["inputs"]
        if name not in schema:
            raise ValueError(f"live ComfyUI schema does not expose {name}")
        interface = schema[name]["input"]
        required, optional = interface.get("required", {}), interface.get("optional", {})
        allowed = required | optional
        if unknown := supplied.keys() - allowed.keys():
            raise ValueError(f"unknown node inputs for {name}: {sorted(unknown)}")
        for key in required.keys() - supplied.keys():
            descriptor = required[key]
            if len(descriptor) < 2 or "default" not in descriptor[1]:
                raise ValueError(f"required node input has no observed default: {name}.{key}")
            supplied[key] = descriptor[1]["default"]
        for key, value in supplied.items():
            if isinstance(value, list) and len(value) == 2 and value[0] in graph:
                other = graph[value[0]]["class_type"]
                if type(value[1]) is not int or not 0 <= value[1] < len(schema[other]["output"]):
                    raise ValueError(f"invalid node output link: {name}.{key}")
                if stage == "z-image" and schema[other]["output"][value[1]] != allowed[key][0]:
                    raise ValueError(f"incompatible node output link: {other} -> {name}.{key}")
            else:
                _validate_input(name, key, value, allowed[key], schema)
    return graph


def launch_args(
    root: ProjectRoot, *, image_only: bool = False, extra_model_paths: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    names = ("input", "output", "user", "temp", "previews", "cache")
    paths = {name: root.make_dir(name).as_posix() for name in names}
    cache = paths["cache"]
    env = os.environ.copy()
    env.update({
        **dict.fromkeys(("TEMP", "TMP", "TMPDIR"), paths["temp"]),
        "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "PIP_NO_INDEX": "1",
        "XDG_CACHE_HOME": cache, "HF_HOME": f"{cache}/huggingface", "TORCH_HOME": f"{cache}/torch",
        "CUDA_CACHE_PATH": f"{cache}/cuda", "TRITON_CACHE_DIR": f"{cache}/triton",
        "TORCHINDUCTOR_CACHE_DIR": f"{cache}/inductor", "REMBG_HOME": f"{cache}/rembg",
        "PYTHONNOUSERSITE": "1",
    })
    python = RUNTIME / ".venv/Scripts/python.exe"
    argv = [
        str(python), "-B", str(COMFY / "main.py"),
        "--listen", "127.0.0.1", "--port", str(PORT),
        "--cuda-device", "0", "--disable-auto-launch", "--offline", "--disable-assets",
        "--database-url", "sqlite:///:memory:", "--disable-all-custom-nodes",
    ]
    if not image_only:
        argv.extend(("--whitelist-custom-nodes", "ComfyUI-Trellis2",
                     "ComfyUI-Trellis2-GGUF", "ComfyUI-GGUF"))
    for name in names[:-1]:
        argv.extend((f"--{name}-directory", paths[name]))
    if extra_model_paths is not None:
        argv.extend(("--extra-model-paths-config", str(extra_model_paths)))
    return argv, env


def owned_processes(process: subprocess.Popen) -> list[psutil.Process]:
    if process.poll() is not None:
        return []
    try:
        leader = psutil.Process(process.pid)
        return [*leader.children(recursive=True), leader]
    except psutil.NoSuchProcess:
        return []


def stop(process: subprocess.Popen, owned: list[psutil.Process]) -> None:
    # Venv launchers can spawn the actual Python worker. Track both owned processes.
    processes = {item.pid: item for item in [*owned, *owned_processes(process)]}
    for item in processes.values():
        try:
            item.terminate()
        except psutil.NoSuchProcess:
            continue
    _, alive = psutil.wait_procs(list(processes.values()), timeout=10)
    for item in alive:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            continue
    _, alive = psutil.wait_procs(alive, timeout=10)
    if alive:
        raise RuntimeError(f"owned ComfyUI process tree did not stop: {[item.pid for item in alive]}")
    process.wait(timeout=10)


def wait_for_server(transport: HttpTransport, process: subprocess.Popen) -> tuple[dict, list]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"ComfyUI exited during startup ({process.returncode}); see server.log")
        try:
            stats = transport.request("GET", "/system_stats", timeout=2)
        except JobError:
            time.sleep(1)
            continue
        owned = owned_processes(process)
        if not any(
            entry.status == psutil.CONN_LISTEN and entry.laddr.port == PORT
            for item in owned
            for entry in item.net_connections(kind="tcp")
        ):
            raise RuntimeError("the ComfyUI HTTP listener is not part of the owned process tree")
        if not any(GPU_NAME in item["name"] for item in stats["devices"]):
            raise RuntimeError(f"the isolated runtime did not identify the expected GPU: {stats['devices']}")
        return stats, owned
    raise TimeoutError("isolated ComfyUI did not start within 180 seconds")


def admission(probe: PynvmlProbe, demand_mib: int, *, min_ram_gib: float = 0.0) -> dict:
    reading = probe.device_memory()
    available_ram = SystemHostResources().free_ram_gb()
    ram_floor = max(min_ram_gib, Settings().resources.ram_floor_gb)
    if reading.free_mb < demand_mib + VRAM_MARGIN_MIB or available_ram < ram_floor:
        raise RuntimeError(
            f"pilot admission needs {demand_mib + VRAM_MARGIN_MIB} MiB free VRAM and "
            f"{ram_floor:.2f} GiB available host RAM; observed {reading.free_mb:.0f} MiB "
            f"and {available_ram:.2f} GiB"
        )
    return {"used_mib": reading.used_mb, "free_mib": reading.free_mb,
            "total_mib": reading.total_mb, "host_ram_available_gib": available_ram}


def run_stage(
    root: ProjectRoot, transport: HttpTransport, process: subprocess.Popen,
    probe: PynvmlProbe, stage: str, evidence: dict, budget: int, asset: str,
    *, image_only: bool = False, recheck_mib: int | None = None, min_ram_gib: float = 0.0,
) -> None:
    schemas = transport.request("GET", "/object_info", timeout=20)
    graph = workflow(stage, schemas, asset, image_only=image_only)
    label = f"{asset}-{stage}" if asset in PROP_PROMPTS else stage
    record(root, f"{label}-graph.json", graph)
    record(root, f"{label}-schemas.json", {
        node["class_type"]: schemas[node["class_type"]] for node in graph.values()
    })
    if recheck_mib is not None:
        evidence["baseline"] = admission(probe, recheck_mib, min_ram_gib=min_ram_gib)
    start = time.monotonic()
    prompt_id = transport.request("POST", "/prompt", {"prompt": graph}, timeout=15)["prompt_id"]
    deadline = start + budget
    evidence["prompt_id"] = prompt_id
    evidence["peak_used_mib"] = evidence["baseline"]["used_mib"]
    print(f"{label}: queued {prompt_id}", flush=True)
    while (remaining := deadline - time.monotonic()) > 0:
        if process.poll() is not None:
            raise RuntimeError(f"isolated ComfyUI exited during {label} ({process.returncode})")
        memory = probe.device_memory()
        evidence["peak_used_mib"] = max(evidence["peak_used_mib"], memory.used_mb)
        evidence["elapsed_seconds"] = time.monotonic() - start
        if memory.free_mb < VRAM_MARGIN_MIB:
            raise RuntimeError(f"{label} exhausted the {VRAM_MARGIN_MIB} MiB pilot VRAM margin")
        try:
            result = transport.request("GET", f"/history/{prompt_id}", timeout=min(10, remaining))
        except JobError as exc:
            if (exc.kind is not JobErrorKind.WORKER_UNREACHABLE
                    or isinstance(exc.__cause__, urllib.error.HTTPError)
                    or process.poll() is not None):
                raise
            # Native GPU work can briefly block the history endpoint. Poll the
            # existing prompt until its deadline; never submit it a second time.
            evidence["history_read_failures"] = evidence.get("history_read_failures", 0) + 1
            result = {}
        if time.monotonic() >= deadline:
            break
        if prompt_id in result:
            entry = result[prompt_id]
            record(root, f"{label}-history.json", entry)
            if entry["status"]["status_str"] != "success":
                raise RuntimeError(f"{label} execution failed; see {label}-history.json and server.log")
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"{label} exceeded the {budget}s pilot deadline")


def free_models(transport: HttpTransport, probe: PynvmlProbe, idle_used_mib: float) -> float:
    transport.request("POST", "/free", {"unload_models": True, "free_memory": True}, timeout=10)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        memory = probe.device_memory()
        queue = transport.request("GET", "/queue", timeout=5)
        if (memory.used_mb <= idle_used_mib + VRAM_MARGIN_MIB
                and not queue["queue_running"] and not queue["queue_pending"]):
            return memory.used_mb
        time.sleep(1)
    raise RuntimeError("ComfyUI did not release VRAM to the measured idle range")


def output_receipt(root: ProjectRoot, relative: str, magic: bytes) -> dict:
    path = root.resolve(relative)
    with path.open("rb") as handle:
        if handle.read(len(magic)) != magic:
            raise ValueError(f"invalid generated media signature: {relative}")
    return {"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)}


def retained_reference(repo: ProjectRoot, relative: str, asset: str = "sofa") -> dict:
    reference_path = f"output/{asset}_reference_00001_.png"
    match = re.fullmatch(
        rf"(spikes/30_at18_found_footage/_work/{asset}-[a-f0-9]{{32}})/{re.escape(reference_path)}",
        relative,
    )
    if match is None or repo.resolve(relative) != repo.path.joinpath(*relative.split("/")):
        raise ValueError(f"reference must be the contained FLUX output of an earlier {asset} pilot")
    run = match.group(1)
    receipt_relative = f"{run}/receipt.json"
    receipt = json.loads(repo.read_text(receipt_relative))
    reference = output_receipt(repo, relative, b"\x89PNG\r\n\x1a\n")
    if receipt.get("reference") != {**reference, "path": reference_path}:
        raise ValueError("reference does not match the retained FLUX output receipt")
    flux_prompt_id = receipt["stages"]["flux"]["prompt_id"]
    history = json.loads(repo.read_text(f"{run}/flux-history.json"))
    if history["status"]["status_str"] != "success":
        raise ValueError("reference has no successful internal FLUX history")
    return {
        **reference, "flux_prompt_id": flux_prompt_id, "source_run_receipt": receipt_relative,
        "source_run_receipt_sha256": sha256(repo.resolve(receipt_relative)),
    }


def cabinet_baseline() -> dict:
    repo = ProjectRoot(ART_WORKTREE)
    receipt_relative = f"{CABINET_BASELINE}/receipt.json"
    receipt = json.loads(repo.read_text(receipt_relative))
    cabinet = receipt["props"][Z_IMAGE_ASSET]
    if receipt["seed"] != SEED or cabinet["prompt"] != PROP_PROMPTS[Z_IMAGE_ASSET]:
        raise ValueError("retained FLUX cabinet prompt or seed differs from the trial")
    result = {
        "worktree": str(repo.path), "receipt": receipt_relative,
        "receipt_sha256": sha256(repo.resolve(receipt_relative)),
    }
    for kind, relative, magic in (
        ("reference", CABINET_PNG, PNG_SIGNATURE), ("glb", CABINET_GLB, b"glTF"),
    ):
        observed = output_receipt(repo, f"{CABINET_BASELINE}/{relative}", magic)
        if cabinet[kind] != {**observed, "path": relative}:
            raise ValueError(f"retained FLUX/TRELLIS cabinet {kind} differs from its receipt")
        result[kind] = observed
    flux = json.loads(repo.read_text(f"{CABINET_BASELINE}/{Z_IMAGE_ASSET}-flux-history.json"))
    metadata = flux["outputs"]["9"]["images"][0]["metadata"]
    if (metadata["width"], metadata["height"]) != (768, 768):
        raise ValueError("retained FLUX cabinet resolution differs from the trial")
    return result


def retained_z_image_reference(repo: ProjectRoot, relative: str) -> dict:
    match = re.fullmatch(
        rf"({re.escape(Z_IMAGE_WORK)}/z-image-cabinet-[a-f0-9]{{32}})/{re.escape(CABINET_PNG)}",
        relative,
    )
    if match is None or repo.resolve(relative) != repo.path.joinpath(*relative.split("/")):
        raise ValueError("use the contained PNG from a completed Z-Image cabinet image run")
    run = match.group(1)
    receipt_relative = f"{run}/receipt.json"
    history_relative = f"{run}/{Z_IMAGE_ASSET}-z-image-history.json"
    receipt = json.loads(repo.read_text(receipt_relative))
    history = json.loads(repo.read_text(history_relative))
    image = output_receipt(repo, relative, PNG_SIGNATURE)
    if (
        receipt.get("trial") != "z-image-cabinet"
        or receipt.get("status") != "completed"
        or receipt.get("image_only") is not True
        or receipt.get("reference") != {**image, "path": CABINET_PNG}
    ):
        raise ValueError("candidate PNG does not match its completed Z-Image image receipt")
    prompt_id = receipt["stages"]["z-image"]["prompt_id"]
    output = history["outputs"]["10"]["images"]
    if (
        history["status"]["status_str"] != "success"
        or history["prompt"][1] != prompt_id
        or len(output) != 1
        or output[0]["filename"] != Path(CABINET_PNG).name
        or output[0]["subfolder"] != ""
        or output[0]["type"] != "output"
        or (output[0]["metadata"]["width"], output[0]["metadata"]["height"]) != (768, 768)
    ):
        raise ValueError("candidate PNG is not bound to the successful internal Z-Image prompt")
    return {
        **image, "z_image_prompt_id": prompt_id, "source_run_receipt": receipt_relative,
        "source_run_receipt_sha256": sha256(repo.resolve(receipt_relative)),
    }


def generate(
    root: ProjectRoot, repo: ProjectRoot, reference: dict | None, asset: str, *,
    image_only: bool = False, z_image_mib: int | None = None, comparison: dict | None = None,
) -> None:
    started = time.monotonic()
    props = asset in PROP_BATCHES
    selected = PROP_BATCHES.get(asset, (asset,))
    evidence = {"status": "starting", "asset": asset, "seed": SEED, "image_only": image_only}
    if props:
        evidence["props"] = {
            name: {"prompt": PROP_PROMPTS[name], "stages": {}} for name in selected
        }
    else:
        evidence.update(prompt=PROMPTS[asset], stages={})
    input_relative = f"input/{asset}_reference.png"
    ram_floor = 4.0 if comparison is not None else 0.0
    if comparison is not None:
        evidence.update(
            trial="z-image-cabinet", comparison=comparison,
            host_ram_floor_gib=max(ram_floor, Settings().resources.ram_floor_gb),
            vram_margin_mib=VRAM_MARGIN_MIB,
        )
    try:
        print("Verifying local pinned runtime models.", flush=True)
        extra_model_paths = None
        if z_image_mib is not None:
            evidence["exploratory_vram_reservation_mib"] = z_image_mib
            evidence["models"] = z_image_model_receipts(repo)
            extra_model_paths = z_image_model_paths(root, evidence["models"])
        else:
            evidence["models"] = model_receipts(
                include_flux=reference is None, include_bria=asset != "room" and not image_only,
                image_only=image_only,
            )
        record(root, "model-provenance.json", evidence["models"])
        if asset != "room" and not image_only:
            with BRIA.open("rb") as stream:
                root.write_stream(f"cache/rembg/{BRIA_RELATIVE}", stream)
        argv, env = launch_args(root, image_only=image_only, extra_model_paths=extra_model_paths)
        evidence["argv"] = argv
        stages = (("flux", FLUX_MIB, 240), ("trellis", TRELLIS_MIB, 900))
        if z_image_mib is not None:
            stages = (("z-image", z_image_mib, 900),)
        elif image_only:
            stages = stages[:1]
        elif reference is not None:
            evidence["reference"] = reference
            with repo.resolve(reference["path"]).open("rb") as image:
                copied = root.write_stream(input_relative, image)
            if sha256(copied) != reference["sha256"]:
                raise ValueError("copied reference does not match the retained image hash")
            stages = stages[1:]
        with GpuExecutionSlot(ProjectRoot(SLOT_ROOT)):
            with socket.socket() as check_port:
                check_port.bind(("127.0.0.1", PORT))
            probe = PynvmlProbe(device_index=0)
            evidence["cold_baseline"] = admission(probe, stages[0][1], min_ram_gib=ram_floor)
            transport = HttpTransport(PORT)
            log_path = root.write_with("server.log", lambda _path: None)
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    argv, cwd=COMFY, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, shell=False,
                )
                owned: list[psutil.Process] = []
                try:
                    evidence["runtime"], owned = wait_for_server(transport, process)
                    for name in selected:
                        target = evidence["props"][name] if props else evidence
                        for stage, mib, timeout in stages:
                            stage_evidence = {"baseline": admission(probe, mib, min_ram_gib=ram_floor)}
                            target["stages"][stage] = stage_evidence
                            run_stage(
                                root, transport, process, probe, stage, stage_evidence, timeout,
                                name, image_only=image_only,
                                recheck_mib=mib if comparison is not None else None,
                                min_ram_gib=ram_floor,
                            )
                            if stage in {"flux", "z-image"}:
                                relative = f"output/{name}_reference_00001_.png"
                                target["reference"] = output_receipt(
                                    root, relative, PNG_SIGNATURE
                                )
                                if not image_only:
                                    target["post_flux_unload_used_mib"] = free_models(
                                        transport, probe, stage_evidence["baseline"]["used_mib"]
                                    )
                                    with root.resolve(relative).open("rb") as image:
                                        root.write_stream(f"input/{name}_reference.png", image)
                            else:
                                target["glb"] = output_receipt(
                                    root, f"output/{name}_00001_.glb", b"glTF"
                                )
                                if name in PROP_PROMPTS:
                                    target["post_trellis_unload_used_mib"] = free_models(
                                        transport, probe, stage_evidence["baseline"]["used_mib"]
                                    )
                finally:
                    stop(process, owned)
                    evidence["final_used_mib"] = probe.device_memory().used_mb
        evidence["status"] = "completed"
    except BaseException as exc:
        evidence.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        evidence["elapsed_seconds"] = time.monotonic() - started
        record(root, "receipt.json", evidence)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-models", action="store_true", help="Check local models without launching.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--room", action="store_true", help="Generate the complete room from text.")
    mode.add_argument("--brick-texture", action="store_true",
                      help="Generate one square brick albedo texture with FLUX.")
    mode.add_argument("--props", action="store_true", help="Generate a dresser and cast-iron radiator.")
    mode.add_argument("--expanded-props", action="store_true",
                      help="Generate a broken cabinet panel, debris cluster and doll.")
    mode.add_argument("--prop", choices=(*PROP_BATCHES["expanded-props"], "rubbish"),
                      help="Generate one prop in a fresh local worker.")
    mode.add_argument("--z-image-cabinet", action="store_true",
                      help="Generate a single Z-Image cabinet candidate or convert its reviewed PNG.")
    parser.add_argument("--z-image-vram-mib", type=int, metavar="MIB",
                        help="Positive exploratory VRAM reservation for the Z-Image image phase.")
    parser.add_argument("--z-image-reference", metavar="PNG",
                        help="Convert a reviewed, workspace-relative Z-Image cabinet PNG with TRELLIS.")
    parser.add_argument("--image-only", action="store_true", help="Generate only the FLUX room image.")
    parser.add_argument("--reference", help="Reuse a worktree-relative FLUX PNG from a matching prior pilot.")
    args = parser.parse_args()
    if args.z_image_cabinet:
        if args.reference is not None or args.image_only:
            parser.error("--z-image-cabinet selects its own image or conversion phase")
        if args.z_image_vram_mib is not None and args.z_image_vram_mib <= 0:
            parser.error("--z-image-vram-mib must be positive")
        if args.z_image_reference is not None and args.z_image_vram_mib is not None:
            parser.error("the reviewed-image conversion uses the measured TRELLIS reservation")
        if not args.verify_models and args.z_image_reference is None and args.z_image_vram_mib is None:
            parser.error("Z-Image generation requires --z-image-vram-mib")
        repo = ProjectRoot(SLOT_ROOT)
        if args.verify_models:
            models = (z_image_model_receipts(repo) if args.z_image_reference is None
                      else model_receipts(include_flux=False))
            print(json.dumps(models, indent=2))
            return 0
        comparison = cabinet_baseline()
        reference = None
        if args.z_image_reference is not None:
            reference = retained_z_image_reference(repo, args.z_image_reference)
        phase = "z-image-cabinet" if reference is None else "z-image-cabinet-trellis"
        run_root = repo.make_dir(f"{Z_IMAGE_WORK}/{phase}-{uuid.uuid4().hex}")
        print(f"Pilot output: {run_root}", flush=True)
        generate(
            ProjectRoot(run_root), repo, reference, Z_IMAGE_ASSET, image_only=reference is None,
            z_image_mib=args.z_image_vram_mib, comparison=comparison,
        )
        print(f"Completed: {run_root / (CABINET_PNG if reference is None else CABINET_GLB)}", flush=True)
        return 0
    if args.z_image_vram_mib is not None or args.z_image_reference is not None:
        parser.error("Z-Image options require --z-image-cabinet")
    image_only = args.image_only or args.brick_texture
    if image_only and (not (args.room or args.brick_texture) or args.reference is not None):
        parser.error("image-only generation requires --room or --brick-texture without --reference")
    asset = args.prop or (
        "expanded-props" if args.expanded_props else "props" if args.props else "room" if args.room else "sofa"
    )
    if args.brick_texture:
        asset = "brick-texture"
    if asset in PROP_BATCHES and args.reference is not None:
        parser.error(f"--{asset} generates its own references")
    if args.verify_models:
        print(json.dumps(model_receipts(include_bria=asset != "room", image_only=image_only), indent=2))
        return 0
    repo = ProjectRoot(REPO)
    reference = retained_reference(repo, args.reference, asset) if args.reference is not None else None
    run_root = repo.make_dir(f"spikes/30_at18_found_footage/_work/{asset}-{uuid.uuid4().hex}")
    print(f"Pilot output: {run_root}", flush=True)
    generate(ProjectRoot(run_root), repo, reference, asset, image_only=image_only)
    if asset in PROP_BATCHES:
        for name in PROP_BATCHES[asset]:
            print(f"Completed: {run_root / 'output' / f'{name}_00001_.glb'}", flush=True)
    else:
        filename = f"{asset}_reference_00001_.png" if image_only else f"{asset}_00001_.glb"
        print(f"Completed: {run_root / 'output' / filename}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
