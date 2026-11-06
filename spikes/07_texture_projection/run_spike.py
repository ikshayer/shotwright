"""Measured generated-texture projection experiment. Hardware run by an operator.

Stages are resumable and never remove previous evidence. Examples:
  python spikes/07_texture_projection/run_spike.py generate
  python spikes/07_texture_projection/run_spike.py render
  python spikes/07_texture_projection/run_spike.py degrade
  python spikes/07_texture_projection/run_spike.py analyze

The default run id is stable, so rerunning a completed stage reuses its artifact.
Set --run-id to retain a separate experiment. This harness intentionally does not
start Blender or ComfyUI unless the selected stage needs them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Imported packages must not create bytecode beside the production source tree.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from orchestrator.platform.containment import ProjectRoot  # noqa: E402

SOURCE_BLEND = "_work/phase1/phase1/renders/s1/v0001/db7373cef42a4159ae03e701f359b07d/attempt-0001/scene.blend"
SOURCE_PLAN = "_work/phase1/phase1/renders/s1/v0001/db7373cef42a4159ae03e701f359b07d/attempt-0001/plan.json"
SOURCE_META = "_work/phase1/phase1/renders/s1/v0001/db7373cef42a4159ae03e701f359b07d/attempt-0001/metadata.json"
CAMERA_JSON = "_work/phase1/phase1/camera_profiles/dvr-2005/v0001.json"
PROFILE_JSON = "_work/phase1/phase1/post_profiles/dvr-2005.json"
MODEL_MANIFEST = "_work/fr06/model-hashes.json"
COMFY_HOME = Path(r"D:\ComfyUI_3\ComfyUI_windows_portable")
BLENDER = Path(r"D:\Blender\blender.exe")
WIDTH, HEIGHT = 1280, 960
FPS, CLIP_SECONDS = 60, 15
PROMPT = (
    "Photorealistic empty mundane institutional service room, viewed from a fixed "
    "rectilinear 12 mm wide-angle ceiling security camera in the upper left corner, "
    "looking diagonally across the entire room toward the opposite corner; 4:3 framing. "
    "A small six by four metre room with off-white plaster walls, grey lino floor, "
    "three practical fluorescent ceiling panels with mixed warm and cool white light, "
    "and about twenty ordinary cardboard boxes and plain cylindrical containers "
    "scattered across the floor. Natural perspective, physically plausible architecture, "
    "no fisheye, no barrel distortion, no timestamp, no text, no overlay, no digital "
    "noise, no VHS effects, no person, no dramatic lighting, no reference image."
)
GEN_SEED = 31052026
DEGRADE_SEED = 20051114
SHOT_ID = "spike07_projection"
RUN_ID_DEFAULT = "projection-001"
SPIKE_PREFIX = "spikes/07_texture_projection/"


class SpikeProjectRoot:
    """Read through the repo root; route every write and atomic temp to spike07."""

    def __init__(self) -> None:
        self._repo = ProjectRoot(REPO)
        self._spike = ProjectRoot(REPO / "spikes" / "07_texture_projection")
        self.path = self._spike.path

    def resolve(self, relative: str) -> Path:
        if relative.startswith(SPIKE_PREFIX):
            return self._spike.resolve(relative[len(SPIKE_PREFIX):])
        return self._repo.resolve(relative)

    def read_text(self, relative: str, *, encoding: str = "utf-8") -> str:
        return self._repo.read_text(relative, encoding=encoding)

    def read_bytes(self, relative: str) -> bytes:
        return self._repo.read_bytes(relative)

    def _write_relative(self, relative: str) -> str:
        if not relative.startswith(SPIKE_PREFIX):
            raise ValueError(f"spike harness writes must stay below {SPIKE_PREFIX!r}: {relative!r}")
        return relative[len(SPIKE_PREFIX):]

    def make_dir(self, relative: str) -> Path:
        return self._spike.make_dir(self._write_relative(relative))

    def write_text(self, relative: str, text: str, *, encoding: str = "utf-8") -> Path:
        return self._spike.write_text(self._write_relative(relative), text, encoding=encoding)

    def write_bytes(self, relative: str, data: bytes) -> Path:
        return self._spike.write_bytes(self._write_relative(relative), data)

    def write_with(self, relative: str, writer: Callable[[Path], None]) -> Path:
        return self._spike.write_with(self._write_relative(relative), writer)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_process(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise RuntimeError(f"command exited {result.returncode}: {argv[0]}\n{result.stderr[-5000:]}")
    return result


class DeviceSampler:
    """Sample device-wide NVML usage (not process memory) while a stage runs."""

    def __init__(self, device: int = 0, cadence: float = 0.5) -> None:
        self.device, self.cadence = device, cadence
        self.samples: list[dict[str, float]] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(device)
        except Exception as exc:
            self._nvml = None
            self._handle = None
            self.error = f"NVML unavailable: {type(exc).__name__}: {exc}"

    def sample(self) -> dict[str, float] | None:
        if self._nvml is None:
            return None
        mem = self._nvml.nvmlDeviceGetMemoryInfo(self._handle)
        row = {"t_monotonic": time.monotonic(), "used_mb": mem.used / (1024 * 1024),
               "free_mb": mem.free / (1024 * 1024), "total_mb": mem.total / (1024 * 1024)}
        self.samples.append(row)
        return row

    def start(self) -> dict[str, float] | None:
        before = self.sample()
        if before is not None:
            self._thread = threading.Thread(target=self._poll, daemon=True)
            self._thread.start()
        return before

    def _poll(self) -> None:
        while not self._stop.wait(self.cadence):
            try:
                self.sample()
            except Exception as exc:
                self.error = f"NVML sample failed: {type(exc).__name__}: {exc}"
                return

    def finish(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.cadence + 1)
        self.sample()
        values = [row["used_mb"] for row in self.samples]
        return {"scope": "whole pinned device; not per-process", "device_index": self.device,
                "sample_cadence_seconds": self.cadence, "sample_count": len(values),
                "baseline_used_mb": values[0] if values else None,
                "peak_used_mb": max(values) if values else None,
                "peak_minus_baseline_mb": max(values) - values[0] if values else None,
                "samples": self.samples, "error": self.error}


def dirs(project: ProjectRoot, run_id: str) -> dict[str, str]:
    base = f"spikes/07_texture_projection/_work/{run_id}"
    paths = {name: f"{base}/{name}" for name in
             ("runtime", "tmp", "cache", "generated", "renders", "masters", "degraded", "reports", "logs", "fonts")}
    for rel in paths.values():
        project.make_dir(rel)
    return paths


def env_for(paths: dict[str, str], project: ProjectRoot) -> dict[str, str]:
    env = os.environ.copy()
    temp = str(project.resolve(paths["tmp"]))
    cache = str(project.resolve(paths["cache"]))
    env.update({"TEMP": temp, "TMP": temp, "TMPDIR": temp,
                "HF_HOME": str(project.resolve(paths["runtime"]) / "hf"),
                "TORCH_HOME": str(project.resolve(paths["runtime"]) / "torch"),
                "XDG_CACHE_HOME": cache, "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": cache, "BLENDER_USER_CONFIG": str(project.resolve(paths["runtime"]) / "blender-config"),
                "BLENDER_USER_SCRIPTS": str(project.resolve(paths["runtime"]) / "blender-scripts"),
                "BLENDER_USER_DATAFILES": str(project.resolve(paths["runtime"]) / "blender-datafiles"),
                "BLENDER_USER_EXTENSIONS": str(project.resolve(paths["runtime"]) / "blender-extensions"),
                "CUDA_CACHE_PATH": str(project.resolve(paths["cache"]) / "cuda"),
                "CUDA_CACHE_DISABLE": "0", "OPTIX_CACHE_PATH": str(project.resolve(paths["cache"]) / "optix")})
    for rel in (f"{paths['runtime']}/hf", f"{paths['runtime']}/torch",
                f"{paths['runtime']}/blender-config", f"{paths['runtime']}/blender-scripts",
                f"{paths['runtime']}/blender-datafiles", f"{paths['runtime']}/blender-extensions",
                f"{paths['cache']}/cuda", f"{paths['cache']}/optix"):
        project.make_dir(rel)
    return env


def report_path(run_id: str) -> str:
    return f"spikes/07_texture_projection/_work/{run_id}/reports/metrics.json"


def load_report(project: ProjectRoot, rel: str) -> dict[str, Any]:
    if project.resolve(rel).is_file():
        # Corrupt evidence must fail visibly instead of being replaced by a fresh report.
        return json.loads(project.read_text(rel))
    return {"schema": "spike07/1", "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": Path(rel).parents[1].name, "stages": {}, "artifacts": {}, "provenance": {}}


def save_report(project: ProjectRoot, rel: str, data: dict[str, Any]) -> None:
    project.write_text(rel, json.dumps(data, sort_keys=True, indent=2, default=str) + "\n")


def artifact(project: ProjectRoot, data: dict[str, Any], key: str, rel: str) -> None:
    path = project.resolve(rel)
    if path.is_file():
        data.setdefault("artifacts", {})[key] = {"path": rel, "bytes": path.stat().st_size, "sha256": sha256(path)}


def read_pins(project: ProjectRoot):
    from orchestrator.adapters.comfyui.workflow import ModelPin
    manifest = json.loads(project.read_text(MODEL_MANIFEST))
    names = {"diffusion": "flux-2-klein-4b.safetensors",
             "text_encoder": "qwen_3_4b_fp4_flux2.safetensors", "vae": "flux2-vae.safetensors"}
    sources = {
        "diffusion": "https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/6dfcebf/flux-2-klein-4b.safetensors",
        "text_encoder": "https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/1f52fa6a526ff84b01520be049b63f7835fe059f/split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors",
        "vae": "https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/a9e4ca87c16db4c4e1a16406a9ddb300ab0ae246/split_files/vae/flux2-vae.safetensors"}
    pins = []
    for role, name in names.items():
        row = manifest[role]
        weight = Path(row["path"])
        if not weight.is_file() or weight.name != name or sha256(weight) != row["sha256"]:
            raise RuntimeError(f"local FR06 pinned {role} weight missing or hash mismatch: {weight}")
        pins.append(ModelPin(role=role, path=weight, sha256=row["sha256"], source=sources[role], license_id="Apache-2.0"))
    return manifest, tuple(pins)


def stage_generate(project: ProjectRoot, paths: dict[str, str], data: dict[str, Any], *, port: int) -> None:
    destination = f"{paths['generated']}/generated.png"
    if project.resolve(destination).is_file():
        known = data.get("artifacts", {}).get("generated_still")
        if not known or known.get("sha256") != sha256(project.resolve(destination)):
            raise RuntimeError("existing generated PNG is not bound to a matching retained artifact hash")
        if data.get("provenance", {}).get("prompt") != PROMPT or data.get("provenance", {}).get("generation_seed") != GEN_SEED:
            raise RuntimeError("existing generated PNG provenance does not match the current fixed prompt and seed")
        artifact(project, data, "generated_still", destination)
        data["stages"].setdefault("generate", {}).setdefault("reuse_events", []).append(
            {"at": datetime.now(timezone.utc).isoformat(), "status": "hash_and_provenance_verified_reuse"})
        return
    from orchestrator.adapters.comfyui.runner import ComfyAdapter
    from orchestrator.adapters.comfyui.workflow import ImageRequest
    from orchestrator.domain.entities import Job
    from orchestrator.jobs.lease import GpuLease, PynvmlProbe
    from orchestrator.jobs.queue import JobQueue, ProjectQuarantine, SystemHostResources
    from orchestrator.platform.settings import BinariesSettings, BudgetSettings, ResourceSettings, Settings
    from orchestrator.store.json_store import ProjectStore
    if not COMFY_HOME.is_dir():
        raise FileNotFoundError(f"configured ComfyUI install not found: {COMFY_HOME}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"ComfyUI port {port} is occupied; refusing to adopt another server") from exc
    run_root = ProjectRoot(project.resolve(paths["runtime"]))
    # All adapter-owned temp/cache writes stay in the already-contained run root.
    for rel in ("runtime/tmp", "runtime/cache", "runtime/hf", "runtime/torch"):
        run_root.make_dir(rel)
    env = os.environ.copy()
    temp = str(run_root.resolve("runtime/tmp")); cache = str(run_root.resolve("runtime/cache"))
    env.update({"TEMP": temp, "TMP": temp, "TMPDIR": temp, "HF_HOME": str(run_root.resolve("runtime/hf")),
                "TORCH_HOME": str(run_root.resolve("runtime/torch")), "XDG_CACHE_HOME": cache,
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": cache})
    old_env = {key: os.environ.get(key) for key in env if key in {"TEMP", "TMP", "TMPDIR", "HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "CUDA_CACHE_PATH", "CUDA_CACHE_DISABLE", "OPTIX_CACHE_PATH"}}
    os.environ.update({key: env[key] for key in old_env})
    settings = Settings(project_root=run_root.path,
                        binaries=BinariesSettings(comfyui_root=COMFY_HOME, comfyui_port=port),
                        resources=ResourceSettings(throughput_floor_per_second=0.0),
                        budget=BudgetSettings(min_free_disk_gb=0.1, disk_ceiling_gb=100.0,
                                              retries_per_shot=0, per_shot_wall_clock_seconds=600,
                                              gpu_minutes_per_shot=10, gpu_minutes_per_episode=10))
    manifest, pins = read_pins(project)
    nvml = PynvmlProbe(device_index=0)
    adapter = ComfyAdapter(run_root, settings)
    lease = GpuLease(run_root, settings, nvml=nvml, worker=adapter.worker)
    queue = JobQueue(run_root, settings, lease, ProjectStore(run_root), nvml=nvml,
                     host=SystemHostResources(), quarantine=ProjectQuarantine(run_root))
    job = Job(job_id=f"spike07-{data['run_id']}", type="image_generation_calibration", shot_id=SHOT_ID,
              run_id=f"generation-{data['run_id']}", episode_id=f"generation-{data['run_id']}",
              required_vram_gb=0, required_ram_gb=0, required_disk_gb=0)
    request = ImageRequest(shot_id=SHOT_ID, shot_version=1, run_id=f"generation-{data['run_id']}",
                           prompt=PROMPT, degrade_seed=GEN_SEED, models=pins,
                           width=WIDTH, height=HEIGHT, steps=4, gpu_device=0)
    sampler = DeviceSampler()
    baseline = sampler.start()
    started = time.monotonic()
    sampler_report = None
    try:
        result = adapter.generate(request, job, queue=queue)
        elapsed = time.monotonic() - started
        run_obj = result.generation_run.model_dump(mode="json")
        sampler_report = sampler.finish()
        image_source_rel = result.image_paths[0]
        image_data = run_root.read_bytes(image_source_rel)
        project.write_bytes(destination, image_data)
        data["stages"]["generate"] = {"status": "completed", "wall_seconds": elapsed,
                                      "adapter_generation_run": run_obj,
                                      "device_memory": sampler_report, "memory_before": baseline,
                                      "generation_image_in_adapter_project": image_source_rel,
                                      "model_manifest": manifest}
        data["provenance"].update({"prompt": PROMPT, "generation_seed": GEN_SEED, "shot_id": SHOT_ID,
                                   "width": WIDTH, "height": HEIGHT, "steps": 4,
                                   "models": manifest, "reference_image_input": False})
        artifact(project, data, "generated_still", destination)
    except Exception as exc:
        if sampler_report is None:
            sampler_report = sampler.finish()
        attempt_rel = f"generated/{SHOT_ID}/v0001/generation-{data['run_id']}/attempt-0001"
        attempt_error = None
        attempt_error_rel = f"{attempt_rel}/job.jsonl"
        try:
            attempt_error = json.loads(run_root.read_text(attempt_error_rel).splitlines()[-1])
        except (OSError, ValueError, IndexError):
            attempt_error = None
        server_logs = []
        for path in run_root.resolve("runtime").rglob("server.log"):
            rel = path.relative_to(run_root.path).as_posix()
            server_logs.append({"path": rel, "bytes": path.stat().st_size, "sha256": sha256(path)})
        data.setdefault("stages", {})["generate"] = {
            "status": "error", "wall_seconds": time.monotonic()-started,
            "exception_type": type(exc).__name__, "exception": str(exc),
            "job_error_kind": getattr(getattr(exc, "kind", None), "value", None),
            "job_error_details": getattr(exc, "details", None),
            "inner_attempt_error": attempt_error, "inner_attempt_error_path": attempt_error_rel,
            "device_memory": sampler_report, "memory_before": baseline,
            "comfy_server_logs": server_logs,
            "actual_generation_run_seed_if_known": getattr(getattr(locals().get("result", None), "generation_run", None), "seed", None),
            "models": manifest, "prompt": PROMPT, "request_seed": GEN_SEED,
            "reference_image_input": False}
        raise
    finally:
        try:
            adapter.close()
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def stage_render(project: ProjectRoot, paths: dict[str, str], data: dict[str, Any], *, no_figure: bool) -> None:
    blend = project.resolve(SOURCE_BLEND)
    if not blend.is_file():
        raise FileNotFoundError(f"completed Phase 1 scene blend missing: {SOURCE_BLEND}")
    generated = project.resolve(f"{paths['generated']}/generated.png")
    if not generated.is_file():
        raise FileNotFoundError("generated still missing; run the generate stage first")
    plan = json.loads(project.read_text(SOURCE_PLAN))
    scene_meta = json.loads(project.read_text(SOURCE_META))
    out_rows: dict[str, Any] = {}
    worker = Path(__file__).with_name("worker.py")
    for frame in (1, 450, 900):
        for mode in ("baseline", "projection"):
            key = f"{mode}_frame_{frame:03d}"
            rel = f"{paths['renders']}/{key}.png"
            target = project.resolve(rel)
            if not target.is_file():
                stage_sampler = DeviceSampler()
                mem0 = stage_sampler.start(); start = time.monotonic()
                captured: dict[str, str] = {}
                def render_writer(authorized: Path) -> None:
                    argv = [str(BLENDER), "--background", "--factory-startup", "--disable-autoexec",
                            "--python-exit-code", "1", "--python", str(worker), "--", "render",
                            str(blend), str(generated), str(authorized), str(frame), mode, "main", "0"]
                    proc = subprocess.run(argv, cwd=project.path, capture_output=True, text=True,
                                          errors="replace", env=os.environ.copy())
                    log_rel = f"{paths['logs']}/{key}.log"
                    project.write_text(log_rel, proc.stdout + "\n--- STDERR ---\n" + proc.stderr)
                    captured.update({"stdout": proc.stdout, "stderr": proc.stderr,
                                    "log_path": log_rel, "returncode": str(proc.returncode)})
                    if proc.returncode:
                        raise RuntimeError(f"Blender failed for {key}; see contained log {log_rel}: {proc.stderr[-4000:]}")
                project.write_with(rel, render_writer)
                elapsed = time.monotonic() - start
                out_rows[key] = {"path": rel, "wall_seconds_including_process_startup": elapsed,
                                 "device_memory": stage_sampler.finish(), "memory_before": mem0,
                                 "blender_log": captured.get("log_path"),
                                 "blender_stdout_tail": captured.get("stdout", "")[-2000:]}
            else:
                out_rows[key] = {"path": rel, "status": "reused_existing_output"}
            artifact(project, data, key, rel)
    angle_key = "projection_second_camera"
    angle_rel = f"{paths['renders']}/{angle_key}.png"
    if not project.resolve(angle_rel).is_file():
        sampler = DeviceSampler(); mem0 = sampler.start(); start = time.monotonic()
        captured = {}
        def angle_writer(authorized: Path) -> None:
            argv = [str(BLENDER), "--background", "--factory-startup", "--disable-autoexec",
                    "--python-exit-code", "1", "--python", str(worker), "--", "render",
                    str(blend), str(generated), str(authorized), "450", "projection", "second", "0"]
            proc = subprocess.run(argv, cwd=project.path, capture_output=True, text=True,
                                  errors="replace", env=os.environ.copy())
            log_rel = f"{paths['logs']}/{angle_key}.log"
            project.write_text(log_rel, proc.stdout + "\n--- STDERR ---\n" + proc.stderr)
            captured.update({"stdout": proc.stdout, "log_path": log_rel, "returncode": proc.returncode})
            if proc.returncode:
                raise RuntimeError(f"Blender failed for {angle_key}; see contained log {log_rel}: {proc.stderr[-4000:]}")
        project.write_with(angle_rel, angle_writer)
        out_rows[angle_key] = {"path": angle_rel, "wall_seconds_including_process_startup": time.monotonic()-start,
                               "device_memory": sampler.finish(), "memory_before": mem0,
                               "projector_camera_stays_original": True,
                               "blender_log": captured.get("log_path"),
                               "blender_stdout_tail": captured.get("stdout", "")[-2000:]}
    artifact(project, data, angle_key, angle_rel)
    figure_key = "projection_moving_figure_frame_450"
    figure_rel = f"{paths['renders']}/{figure_key}.png"
    if not no_figure and not project.resolve(figure_rel).is_file():
        sampler = DeviceSampler(); mem0 = sampler.start(); start = time.monotonic()
        captured = {}
        def figure_writer(authorized: Path) -> None:
            argv = [str(BLENDER), "--background", "--factory-startup", "--disable-autoexec",
                    "--python-exit-code", "1", "--python", str(worker), "--", "render",
                    str(blend), str(generated), str(authorized), "450", "projection", "main", "figure"]
            proc = subprocess.run(argv, cwd=project.path, capture_output=True, text=True,
                                  errors="replace", env=os.environ.copy())
            log_rel = f"{paths['logs']}/{figure_key}.log"
            project.write_text(log_rel, proc.stdout + "\n--- STDERR ---\n" + proc.stderr)
            captured.update({"stdout": proc.stdout, "log_path": log_rel})
            if proc.returncode:
                raise RuntimeError(f"Blender failed for {figure_key}; see contained log {log_rel}: {proc.stderr[-4000:]}")
        project.write_with(figure_rel, figure_writer)
        out_rows[figure_key] = {"path": figure_rel, "wall_seconds_including_process_startup": time.monotonic()-start,
                                "device_memory": sampler.finish(), "memory_before": mem0,
                                "blender_log": captured.get("log_path"),
                                "blender_stdout_tail": captured.get("stdout", "")[-2000:]}
        artifact(project, data, figure_key, figure_rel)
    data["stages"]["render"] = {"status": "completed", "frames_rendered": [1, 450, 900],
                                 "second_camera": True, "samples": 32,
                                 "engine": scene_meta.get("applied_settings", {}).get("engine"),
                                 "source_plan_sha256": sha256(project.resolve(SOURCE_PLAN)),
                                 "source_blend_sha256": sha256(blend), "camera_plan": plan.get("camera"),
                                 "camera_identity": "CCTV scene camera from completed Phase1 scene.blend",
                                 "source_historical_render_metrics": {"elapsed_seconds_for_900_frames": scene_meta.get("elapsed_seconds"),
                                                                       "vram_baseline_mb": scene_meta.get("vram_baseline_mb"),
                                                                       "vram_peak_mb": scene_meta.get("vram_peak_mb"),
                                                                       "explicitly_historical": True},
                                 "outputs": out_rows, "figure_proxy_rendered": not no_figure,
                                 "figure_proxy_skipped_by_operator": no_figure}


def make_profile(project: ProjectRoot):
    from orchestrator.adapters.postprofile.profile import EncodeSpec, OverlaySpec, PostProfileSpec
    from orchestrator.domain.entities import CameraProfile, PostProfile
    camera_data = json.loads(project.read_text(CAMERA_JSON))
    camera = CameraProfile.model_validate({key: value for key, value in camera_data.items()
                                           if not key.startswith("_")})
    raw_profile = json.loads(project.read_text(PROFILE_JSON))
    identity = PostProfile.model_validate({key: value for key, value in raw_profile.items() if not key.startswith("_")})
    spec = PostProfileSpec(identity=identity, output_fps=identity.output_fps,
                           storage_resolution=identity.storage_resolution,
                           sample_aspect_ratio=identity.sample_aspect_ratio,
                           artifact_schedule=identity.artifact_schedule,
                           overlay=(OverlaySpec.model_validate({key: value for key, value in identity.overlay.model_dump(mode="python").items()
                                                               if key != "schema_version"}) if identity.overlay else None),
                           encode=EncodeSpec.model_validate({key: value for key, value in identity.encode.model_dump(mode="python").items()
                                                            if key != "schema_version"}))
    return camera, spec


def stage_degrade(project: ProjectRoot, paths: dict[str, str], data: dict[str, Any]) -> None:
    from fractions import Fraction
    from orchestrator.adapters.postprofile.profile import build_filter_graph, validate_profile
    camera, profile = make_profile(project)
    ffmpeg_banner = run_process(["ffmpeg", "-hide_banner", "-version"]).stdout.splitlines()[0]
    pinned_banner = profile.identity.pinned_binaries.get("ffmpeg")
    if pinned_banner != ffmpeg_banner:
        raise RuntimeError(f"installed FFmpeg identity differs from the profile pin: {ffmpeg_banner!r}")
    font_source = Path(r"C:\Windows\Fonts\consola.ttf")
    if not font_source.is_file():
        raise FileNotFoundError(f"pinned PostProfile overlay font missing: {font_source}")
    font_rel = f"{paths['fonts']}/consola.ttf"
    if not project.resolve(font_rel).is_file():
        project.write_bytes(font_rel, font_source.read_bytes())
    output = {}
    for input_key, input_rel in (("baseline", f"{paths['renders']}/baseline_frame_450.png"),
                                 ("generated", f"{paths['generated']}/generated.png"),
                                 ("projection", f"{paths['renders']}/projection_frame_450.png")):
        input_path = project.resolve(input_rel)
        if not input_path.is_file():
            raise FileNotFoundError(f"required path image missing: {input_rel}")
        master_rel = f"{paths['masters']}/{input_key}.mkv"
        master = project.resolve(master_rel)
        if not master.is_file():
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loop", "1", "-framerate", str(FPS),
                       "-i", str(input_path), "-t", str(CLIP_SECONDS), "-an", "-vf", "setsar=1",
                       "-c:v", "ffv1", "-level", "3",
                       "-pix_fmt", "yuv420p", "-threads", "1", str(master)]
            project.write_with(master_rel, lambda authorized, command=command: run_process(command, cwd=REPO))
        validated = validate_profile(profile, camera, master_frames=FPS*CLIP_SECONDS, master_rate=Fraction(FPS, 1))
        graph = build_filter_graph(validated, shot_id=SHOT_ID, degrade_seed=DEGRADE_SEED, font_path=font_rel)
        output_rel = f"{paths['degraded']}/{input_key}.mp4"
        output_path = project.resolve(output_rel)
        if not output_path.is_file():
            from orchestrator.adapters.postprofile.runner import build_command
            command = build_command(Path("ffmpeg"), master=master, output=output_path,
                                    filter_graph=graph, codec=profile.encode.codec, preset=profile.encode.preset,
                                    crf=profile.encode.crf, pixel_format=profile.encode.pix_fmt,
                                    threads=profile.encode.threads)
            started = time.monotonic()
            # The measured filter graph refers to its font by a repo-relative path.
            # Its outputs are absolute paths already authorized by SpikeProjectRoot.
            project.write_with(output_rel, lambda authorized, command=command: run_process(command, cwd=REPO))
            elapsed = time.monotonic() - started
        else:
            elapsed = None
        row = {"master_path": master_rel, "output_path": output_rel, "wall_seconds": elapsed,
               "filter_graph": graph, "filter_graph_sha256": hashlib.sha256(graph.encode()).hexdigest(),
               "profile_name": profile.identity.name, "profile_semver": profile.identity.semver,
               "degrade_seed": DEGRADE_SEED, "artifact_schedule": [event.model_dump(mode="json") for event in profile.artifact_schedule],
               "pinned_ffmpeg": profile.identity.pinned_binaries.get("ffmpeg")}
        artifact(project, data, f"degraded_{input_key}", output_rel)
        view_rel = f"{paths['degraded']}/{input_key}-frame.png"
        if not project.resolve(view_rel).is_file():
            video = project.resolve(output_rel)
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-ss", "0.5", "-i", str(video),
                       "-frames:v", "1", "-update", "1", str(project.resolve(view_rel))]
            project.write_with(view_rel, lambda authorized, command=command: run_process(command, cwd=REPO))
        row["degraded_comparison_frame"] = view_rel
        artifact(project, data, f"degraded_frame_{input_key}", view_rel)
        output[input_key] = row
    data["stages"]["degrade"] = {"status": "completed", "profile": "dvr-2005 0.2.0",
                                  "same_seed_and_schedule_for_all_paths": True, "outputs": output}


def stage_analyze(project: ProjectRoot, paths: dict[str, str], data: dict[str, Any]) -> None:
    from PIL import Image, ImageChops, ImageStat, ImageDraw
    generated_path = project.resolve(data["artifacts"]["generated_still"]["path"])
    rows = {}
    tiles: list[Image.Image] = []
    labels = []
    with Image.open(generated_path) as im:
        generated = im.convert("RGB")
    tiles.append(generated.copy()); labels.append("Generated still")
    projections: dict[int, Image.Image] = {}
    for frame, key in ((1, "projection_frame_001"), (450, "projection_frame_450"),
                       (900, "projection_frame_900")):
        with Image.open(project.resolve(data["artifacts"][key]["path"])) as im:
            projection = im.convert("RGB")
        projections[frame] = projection.copy()
        diff = ImageChops.difference(generated, projection)
        stat = ImageStat.Stat(diff)
        pixels = list(diff.getdata())
        changed = sum(1 for r, g, bl in pixels if r or g or bl)
        rows[f"generated_vs_projected_frame_{frame}"] = {
            "generated_size": generated.size, "projection_size": projection.size,
            "mean_absolute_channel_delta": sum(stat.mean)/3,
            "max_channel_delta": max(stat.extrema[i][1] for i in range(3)),
            "different_pixel_fraction": changed/(generated.width*generated.height)}
        tiles.append(projection.copy()); labels.append(f"Projection f{frame}")
    for earlier, later in ((1, 450), (450, 900)):
        diff = ImageChops.difference(projections[earlier], projections[later])
        stat = ImageStat.Stat(diff)
        rows[f"projection_temporal_stability_{earlier}_to_{later}"] = {
            "mean_absolute_channel_delta": sum(stat.mean)/3,
            "max_channel_delta": max(stat.extrema[i][1] for i in range(3))}
    for base_key, degraded_key in (("baseline_frame_450", "degraded_frame_baseline"),
                                   ("generated_still", "degraded_frame_generated"),
                                   ("projection_frame_450", "degraded_frame_projection")):
        with Image.open(project.resolve(data["artifacts"][base_key]["path"])) as im:
            tiles.append(im.convert("RGB")); labels.append(f"Clean {base_key}")
        with Image.open(project.resolve(data["artifacts"][degraded_key]["path"])) as im:
            tiles.append(im.convert("RGB")); labels.append(f"Degraded {base_key}")
    with Image.open(project.resolve(data["artifacts"]["projection_second_camera"]["path"])) as im:
        rows["second_camera"] = {"size": im.size, "projector_source": "original main camera homogeneous projection shader"}
        tiles.append(im.convert("RGB").copy()); labels.append("Projection second view")
    if "projection_moving_figure_frame_450" in data.get("artifacts", {}):
        with Image.open(project.resolve(data["artifacts"]["projection_moving_figure_frame_450"]["path"])) as im:
            tiles.append(im.convert("RGB").copy()); labels.append("Lit moving proxy / f450")
    thumb_w, thumb_h = 384, 288
    columns = 3
    sheet = Image.new("RGB", (thumb_w*columns, (thumb_h+24)*((len(tiles)+columns-1)//columns)), "#171717")
    draw = ImageDraw.Draw(sheet)
    for idx, image in enumerate(tiles):
        image.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        x, y = (idx%columns)*thumb_w, (idx//columns)*(thumb_h+24)
        sheet.paste(image, (x,y+24)); draw.text((x+8,y+5), labels[idx], fill="white")
    comparison_rel = f"{paths['reports']}/comparison.png"
    project.write_with(comparison_rel, lambda authorized: sheet.save(authorized, format="PNG", optimize=True))
    artifact(project, data, "comparison_sheet", comparison_rel)
    published_rel = "spikes/07_texture_projection/comparison.png"
    project.write_bytes(published_rel, project.read_bytes(comparison_rel))
    artifact(project, data, "published_comparison_sheet", published_rel)
    data["stages"]["analyze"] = {"status": "completed", "pixel_metrics": rows,
                                  "comparison_sheet": comparison_rel,
                                  "limits": ["rendered checks are only frames 1, 450, and 900, not all 900 frames",
                                             "static projection is repeated through time and does not establish generated temporal continuity",
                                             "pixel metrics compare projected output to its generated source, not to the structurally different synthetic baseline"]}


def preflight_nonreusable_stage(stage: str, project: SpikeProjectRoot,
                                paths: dict[str, str], data: dict[str, Any], *,
                                no_figure: bool) -> None:
    """Fail closed before a repeated or partial render/degrade can replace metrics."""
    if stage not in {"render", "degrade"}:
        return
    prior = data.get("stages", {}).get(stage, {})
    if prior.get("status") == "completed":
        raise RuntimeError(f"{stage} already completed for this run ID; use a new --run-id to retain measurements")
    if stage == "render":
        expected = [f"{paths['renders']}/{mode}_frame_{frame:03d}.png"
                    for frame in (1, 450, 900) for mode in ("baseline", "projection")]
        expected.append(f"{paths['renders']}/projection_second_camera.png")
        if not no_figure:
            expected.append(f"{paths['renders']}/projection_moving_figure_frame_450.png")
    else:
        expected = [f"{paths['masters']}/{name}.mkv" for name in ("baseline", "generated", "projection")]
        expected += [f"{paths['degraded']}/{name}{suffix}"
                     for name in ("baseline", "generated", "projection")
                     for suffix in (".mp4", "-frame.png")]
    present = [relative for relative in expected if project.resolve(relative).is_file()]
    if present:
        shown = ", ".join(present[:4])
        more = " …" if len(present) > 4 else ""
        raise RuntimeError(f"partial {stage} outputs already exist ({shown}{more}); use a new --run-id to avoid replacing measurements")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("generate", "render", "degrade", "analyze", "all"))
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("--no-figure", action="store_true", help="record the optional moving proxy as intentionally skipped")
    args = parser.parse_args()
    if not args.run_id.replace("-", "").replace("_", "").isalnum():
        parser.error("--run-id may contain only letters, numbers, underscore and hyphen")
    project = SpikeProjectRoot()
    paths = dirs(project, args.run_id)
    report_rel = report_path(args.run_id)
    data = load_report(project, report_rel)
    data["provenance"].update({"source_blend": SOURCE_BLEND, "source_plan": SOURCE_PLAN,
                               "source_metadata": SOURCE_META, "degrade_seed": DEGRADE_SEED,
                               "comparison_resolution": [WIDTH, HEIGHT],
                               "sample_frames": [1, 450, 900], "reference_image_input": False,
                               "no_model_downloads": True, "run_started_at": datetime.now(timezone.utc).isoformat()})
    data["provenance"].setdefault("prompt", PROMPT)
    data["provenance"].setdefault("generation_seed", GEN_SEED)
    env = env_for(paths, project)
    # Parent process settings are respected except Python cache/bytecode and temp are contained.
    old = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR", "HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "BLENDER_USER_CONFIG", "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES", "BLENDER_USER_EXTENSIONS", "CUDA_CACHE_PATH", "CUDA_CACHE_DISABLE", "OPTIX_CACHE_PATH")}
    os.environ.update({key: env[key] for key in old if key in env})
    tasks: dict[str, Callable[[], None]] = {
        "generate": lambda: stage_generate(project, paths, data, port=args.port),
        "render": lambda: stage_render(project, paths, data, no_figure=args.no_figure),
        "degrade": lambda: stage_degrade(project, paths, data),
        "analyze": lambda: stage_analyze(project, paths, data),
    }
    stages = list(tasks) if args.stage == "all" else [args.stage]
    try:
        # Preflight the whole pipeline before even a safe generation reuse can
        # update its report, so `all` never runs upstream work into old outputs.
        for stage in stages:
            preflight_nonreusable_stage(stage, project, paths, data, no_figure=args.no_figure)
        for stage in stages:
            print(f"=== spike07 {stage}: run {args.run_id}", flush=True)
            stage_start = time.monotonic()
            previous_stage = data.setdefault("stages", {}).get(stage, {})
            previous_total = previous_stage.get("wall_seconds_total")
            previous_reuses = len(previous_stage.get("reuse_events", []))
            try:
                tasks[stage]()
                elapsed = time.monotonic()-stage_start
                stage_record = data.setdefault("stages", {}).setdefault(stage, {})
                reuse_events = stage_record.get("reuse_events", [])
                if stage == "generate" and len(reuse_events) > previous_reuses:
                    reuse_events[-1]["check_wall_seconds"] = elapsed
                    if previous_total is not None:
                        stage_record["wall_seconds_total"] = previous_total
                    else:
                        stage_record["wall_seconds_total"] = elapsed
                else:
                    stage_record["wall_seconds_total"] = elapsed
            except Exception as exc:
                previous = data.setdefault("stages", {}).get(stage, {})
                data["stages"][stage] = {**previous, "status": "error", "error_type": type(exc).__name__,
                                         "error": str(exc), "wall_seconds_total": time.monotonic()-stage_start}
                save_report(project, report_rel, data)
                raise
            save_report(project, report_rel, data)
        print(json.dumps({"status": "completed", "report": report_rel,
                          "artifact_count": len(data.get("artifacts", {}))}, indent=2))
    finally:
        for key, value in old.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
