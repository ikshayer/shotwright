"""Reproducible, contained renderer and telemetry harness for spike 08.

This script builds the production Blender plan, launches the production worker, and
stores every artifact beneath this spike directory. It deliberately does not decide
which render is visually acceptable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))
BLENDER_DEFAULT = r"D:\Blender\blender.exe"
CAMERA_LOCATION = (-2.48, -1.85, 2.25)
CAMERA_TARGET = (0.2, 0.5, 0.2)

from orchestrator.adapters.blender.scene import (
    RenderRequest,
    build_scene_plan,
    synthetic_interior,
)
from orchestrator.domain.entities import CameraProfile
from orchestrator.platform.containment import ProjectRoot


class DeviceSampler:
    """Poll device-wide NVML memory without claiming it is process-attributed."""

    def __init__(self, interval: float = 0.1, device_index: int = 0) -> None:
        self.interval = interval
        self.device_index = device_index
        self.samples: list[dict[str, float]] = []
        self.error: str | None = None
        self.identity: dict[str, Any] = {"device_index": device_index}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._nvml: Any = None
        self._handle: Any = None

    def _read(self) -> int:
        return int(self._nvml.nvmlDeviceGetMemoryInfo(self._handle).used)

    def start(self) -> None:
        try:
            import pynvml  # type: ignore[import-not-found]
        except ImportError as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return
        self._nvml = pynvml
        try:
            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(self.device_index)
            name = pynvml.nvmlDeviceGetName(self._handle)
            uuid = pynvml.nvmlDeviceGetUUID(self._handle)
            self.identity.update({
                "name": name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name),
                "uuid": uuid.decode("utf-8", "replace") if isinstance(uuid, bytes) else str(uuid),
            })
            try:
                bus_id = pynvml.nvmlDeviceGetPciInfo(self._handle).busId
                self.identity["pci_bus_id"] = bus_id.decode("utf-8", "replace") if isinstance(bus_id, bytes) else str(bus_id)
            except (pynvml.NVMLError, OSError) as exc:
                self.identity["pci_bus_id_error"] = f"{type(exc).__name__}: {exc}"
            # Capture several immediate idle readings before Blender starts.
            for _ in range(5):
                self.samples.append({"t": time.monotonic(), "used_bytes": self._read()})
                time.sleep(0.05)
        except (pynvml.NVMLError, OSError) as exc:  # telemetry limitation is recorded
            self.error = f"{type(exc).__name__}: {exc}"
            return

        def poll() -> None:
            while not self._stop.is_set():
                try:
                    self.samples.append({"t": time.monotonic(), "used_bytes": self._read()})
                except (pynvml.NVMLError, OSError) as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
                    return
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=poll, name="nvml-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._nvml:
            try:
                self._nvml.nvmlShutdown()
            except (self._nvml.NVMLError, OSError) as exc:
                self.error = self.error or f"{type(exc).__name__}: {exc}"
        values = [row["used_bytes"] for row in self.samples]
        idle = min(values[:5]) if values else None
        peak = max(values) if values else None
        return {
            "source": "device-wide NVML; includes other processes",
            "device": self.identity,
            "sampling_interval_seconds": self.interval,
            "idle_baseline_bytes": idle,
            "peak_bytes": peak,
            "delta_bytes": peak - idle if peak is not None and idle is not None else None,
            "sample_count": len(values),
            "error": self.error,
        }


class BlenderProcessSampler:
    """Record other Blender process identities observed during this run.

    Only process name, PID, and create time are read. This is a contention indicator,
    not proof that another process used the GPU.
    """

    def __init__(self, interval: float = 0.75) -> None:
        self.interval = interval
        self.own_pid: int | None = None
        self.launch_monotonic: float | None = None
        self.observed: dict[int, dict[str, float | int | str | None]] = {}
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, *, own_pid: int, launch_monotonic: float) -> None:
        self.own_pid = own_pid
        self.launch_monotonic = launch_monotonic

        def poll() -> None:
            try:
                import psutil
            except ImportError as exc:
                self.errors.append(f"psutil import: {type(exc).__name__}: {exc}")
                return
            while not self._stop.is_set():
                now = time.monotonic()
                try:
                    for process in psutil.process_iter(("pid", "name", "create_time")):
                        try:
                            info = process.info
                            pid = int(info["pid"])
                            name = str(info.get("name") or "")
                            if pid == self.own_pid or name.casefold() != "blender.exe":
                                continue
                            created = float(info["create_time"])
                            record = self.observed.get(pid)
                            if record is None:
                                self.observed[pid] = {
                                    "pid": pid, "create_time_epoch_seconds": created,
                                    "first_seen_monotonic_seconds": now,
                                    "first_seen_seconds_since_launch": now - self.launch_monotonic,
                                    "last_seen_monotonic_seconds": now,
                                    "last_seen_seconds_since_launch": now - self.launch_monotonic,
                                }
                            else:
                                record["last_seen_monotonic_seconds"] = now
                                record["last_seen_seconds_since_launch"] = now - self.launch_monotonic
                        except (psutil.Error, OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
                            if len(self.errors) < 20:
                                self.errors.append(f"process sample: {type(exc).__name__}: {exc}")
                except (psutil.Error, OSError) as exc:
                    if len(self.errors) < 20:
                        self.errors.append(f"process iteration: {type(exc).__name__}: {exc}")
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=poll, name="blender-process-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return {
            "interval_seconds": self.interval,
            "own_blender_pid_excluded": self.own_pid,
            "other_blender_processes_observed": sorted(
                self.observed.values(), key=lambda row: int(row["pid"])),
            "sampling_errors": self.errors,
            "interpretation": "no competing Blender observed is not proof of GPU isolation",
        }


def _living_room():
    # The composition implementation is being added separately for this spike.
    from orchestrator.adapters.blender.scene import american_living_room

    return american_living_room()


def _profile(args: argparse.Namespace) -> CameraProfile:
    return CameraProfile(
        camera_id="living-room-cctv",
        profile_name="Spike 08 shared CCTV camera",
        field_of_view_mm=args.lens,
        barrel_distortion=0.0,
        exposure_ev=args.exposure,
        practical_light_color_k=args.kelvin,
        practical_light_intensity=1.0,
        camera_position=CAMERA_LOCATION,
        camera_mount="fixed corner mount below ceiling",
    )


def _frame_times(stdout: str) -> list[float]:
    """Best-effort parse of Blender's per-frame 'Rendered ... in N.NN sec' lines."""
    found = []
    for line in stdout.splitlines():
        match = re.search(r"Rendered .*? in\s+([0-9]+(?:\.[0-9]+)?)\s+sec", line, re.IGNORECASE)
        if match:
            found.append(float(match.group(1)))
    return found


def _progress_cadence(stdout: str) -> list[float]:
    """Per-frame receipt intervals from production worker progress markers.

    The first marker includes startup and scene setup, so it is returned separately
    from the subsequent warmed intervals in the telemetry record.
    """
    stamps = _progress_timestamps(stdout)
    return [right - left for left, right in pairwise(stamps)]


def _progress_timestamps(stdout: str) -> list[float]:
    stamps = []
    # The live reader adds monotonic seconds as a prefix to each captured line.
    for line in stdout.splitlines():
        if "CCTV_PROGRESS " not in line:
            continue
        match = re.match(r"([0-9]+(?:\.[0-9]+)?) ", line)
        if match:
            stamps.append(float(match.group(1)))
    return stamps


def _convert_first_frame(root: ProjectRoot, exr: Path, png_rel: str, ffmpeg: str) -> str | None:
    # Blender's master EXR is scene-linear. Convert transfer explicitly to display sRGB.
    output = root.resolve(png_rel)
    command = [ffmpeg, "-y", "-v", "error", "-i", str(exr), "-frames:v", "1",
               "-vf", "format=gbrpf32le,zscale=primariesin=bt709:transferin=linear:matrixin=gbr:rangein=full:primaries=bt709:transfer=iec61966-2-1:matrix=gbr:range=full,format=rgb24",
               "-color_primaries", "bt709", "-color_trc", "iec61966-2-1", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        return f"ffmpeg PNG conversion failed ({completed.returncode}): {completed.stderr[-2000:]}"
    if not output.is_file() or output.stat().st_size == 0:
        return "ffmpeg reported success but did not produce a nonempty PNG"
    return None


def run(args: argparse.Namespace) -> int:
    root = ProjectRoot(HERE)
    run_rel = f"_work/{args.run_id}"
    if root.resolve(run_rel).exists():
        raise FileExistsError(f"run directory already exists; choose a new --run-id: {run_rel}")
    root.make_dir(run_rel)
    temp_dir = root.make_dir(f"{run_rel}/temp")
    cache_dir = root.make_dir(f"{run_rel}/cache")
    config_dir = root.make_dir(f"{run_rel}/config")
    for child in ("scripts", "datafiles", "extensions"):
        root.make_dir(f"{run_rel}/config/{child}")
    root.make_dir(f"{run_rel}/frames")
    scene = synthetic_interior() if args.scene == "baseline" else _living_room()
    request = RenderRequest(
        shot_id=f"spike08-{args.scene}", shot_version=1, run_id=args.run_id,
        degrade_seed=1977, duration_sec=args.frames / 60,
        master_resolution=(args.width, args.height), camera_profile=_profile(args),
        scene=scene.model_copy(update={"camera_location": CAMERA_LOCATION,
                                       "camera_target": CAMERA_TARGET}),
        mode="master", engine="eevee", samples=args.samples,
    )
    plan = build_scene_plan(request)
    # Apply the same fixed corner camera to both room builders. Preserve all other
    # production-derived plan fields and the shared target in the SceneSpec.
    plan["frame_end"] = args.frames
    frame_paths = [root.resolve(f"{run_rel}/frames/frame_{index:06d}.exr")
                   for index in range(1, args.frames + 1)]
    blend = root.resolve(f"{run_rel}/scene.blend")
    metadata = root.resolve(f"{run_rel}/metadata.json")
    plan["outputs"] = {"blend": str(blend), "metadata": str(metadata),
                       "frames": [str(path) for path in frame_paths]}
    plan["runtime"] = {"temp_dir": str(temp_dir)}
    plan_rel = f"{run_rel}/plan.json"
    root.write_text(plan_rel, json.dumps(plan, indent=2, sort_keys=True) + "\n")

    blender = args.blender
    if not Path(blender).is_file():
        raise FileNotFoundError(f"Blender executable does not exist: {blender}")
    worker = REPO / "orchestrator" / "adapters" / "blender" / "worker.py"
    command = [blender, "--background", "--factory-startup", "--disable-autoexec",
               "--python-exit-code", "1", "--threads", str(args.threads),
               "--python", str(worker), "--", str(root.resolve(plan_rel))]
    env = os.environ.copy()
    env.update({
        "TMP": str(temp_dir), "TEMP": str(temp_dir), "TMPDIR": str(temp_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "BLENDER_USER_CONFIG": str(config_dir),
        "BLENDER_USER_SCRIPTS": str(root.resolve(f"{run_rel}/config/scripts")),
        "BLENDER_USER_DATAFILES": str(root.resolve(f"{run_rel}/config/datafiles")),
        "BLENDER_USER_EXTENSIONS": str(root.resolve(f"{run_rel}/config/extensions")),
        "CUDA_CACHE_PATH": str(cache_dir), "CUDA_CACHE_DISABLE": "0",
        "OPTIX_CACHE_PATH": str(cache_dir), "PYTHONDONTWRITEBYTECODE": "1",
    })
    sampler = DeviceSampler(device_index=args.gpu_index)
    sampler.start()
    start = time.monotonic()
    stdout_path = root.resolve(f"{run_rel}/blender.log")
    progress_output: list[str] = []
    started_utc = datetime.now(UTC).isoformat()
    with stdout_path.open("w", encoding="utf-8", errors="replace") as log:
        launch_monotonic = time.monotonic()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env=env, cwd=str(root.resolve(run_rel)), text=True,
                                   errors="replace", bufsize=1)
        process_sampler = BlenderProcessSampler()
        process_sampler.start(own_pid=process.pid, launch_monotonic=launch_monotonic)
        assert process.stdout is not None
        for line in process.stdout:
            captured = f"{time.monotonic():.8f} {line}"
            log.write(captured)
            if "CCTV_PROGRESS " in line:
                progress_output.append(captured.rstrip("\r\n"))
        return_code = process.wait()
    wall_seconds = time.monotonic() - start
    telemetry = sampler.stop()
    process_telemetry = process_sampler.stop()
    output_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    validation_errors: list[str] = []
    actual_metadata = None
    if metadata.exists():
        try:
            actual_metadata = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            validation_errors.append(f"worker metadata could not be read: {type(exc).__name__}: {exc}")
    if not isinstance(actual_metadata, dict):
        validation_errors.append("worker metadata is missing or is not a JSON object")
    elif actual_metadata.get("status") != "completed":
        validation_errors.append(f"worker status is {actual_metadata.get('status')!r}, expected 'completed'")
    missing_frames = [str(path) for path in frame_paths if not path.is_file() or path.stat().st_size == 0]
    if missing_frames:
        validation_errors.append(f"missing or empty EXR output(s): {missing_frames}")
    telemetry_record = {
        "schema_version": 1,
        "started_utc": started_utc,
        "scene": args.scene, "run_id": args.run_id,
        "command": command, "configuration": {"frames": args.frames, "width": args.width,
                         "height": args.height, "samples": args.samples, "threads": args.threads,
                         "engine": "eevee", "camera_location": CAMERA_LOCATION,
                         "camera_target": CAMERA_TARGET, "lens_mm": args.lens,
                         "practical_light_color_k": args.kelvin, "exposure_ev": args.exposure},
        "return_code": return_code,
        "environment_paths": {key: env[key] for key in (
            "TMP", "TEMP", "TMPDIR", "XDG_CACHE_HOME", "BLENDER_USER_CONFIG",
            "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES", "BLENDER_USER_EXTENSIONS",
            "CUDA_CACHE_PATH", "OPTIX_CACHE_PATH")},
        "validation_errors": validation_errors,
        "wall_seconds_including_blender_startup": wall_seconds,
        "progress_receipt_times_seconds_since_launch": [
            stamp - launch_monotonic for stamp in _progress_timestamps("\n".join(progress_output))],
        "per_frame_render_seconds": _frame_times(output_text),
        "progress_intervals_seconds": _progress_cadence("\n".join(progress_output)),
        "first_frame_setup_and_render_seconds": None,
        "warm_progress_cadence_seconds": None,
        "cadence_seconds_per_frame_excluding_startup": (
            sum(_frame_times(output_text)) / len(_frame_times(output_text)) if _frame_times(output_text) else None
        ),
        "nvml": telemetry,
        **process_telemetry,
        "actual_worker_metadata": actual_metadata,
        "png_conversion_error": None,
    }
    intervals = telemetry_record["progress_intervals_seconds"]
    if intervals:
        telemetry_record["warm_progress_cadence_seconds"] = sum(intervals) / len(intervals)
    markers = [float(re.match(r"([0-9]+(?:\.[0-9]+)?) ", line).group(1))
               for line in progress_output]
    if markers:
        telemetry_record["first_frame_setup_and_render_seconds"] = markers[0] - launch_monotonic
    if return_code == 0 and frame_paths:
        telemetry_record["png_conversion_error"] = _convert_first_frame(
            root, frame_paths[0], f"{args.run_id}.png", args.ffmpeg)
        if telemetry_record["png_conversion_error"]:
            validation_errors.append(telemetry_record["png_conversion_error"])
    final_return_code = return_code if return_code else (1 if validation_errors else 0)
    telemetry_record["final_return_code"] = final_return_code
    root.write_text(f"{run_rel}/telemetry.json",
                    json.dumps(telemetry_record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"telemetry": str(root.resolve(f'{run_rel}/telemetry.json')),
                      "log": str(stdout_path), "return_code": final_return_code}, indent=2))
    return final_return_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", choices=("baseline", "living"), required=True)
    parser.add_argument("--run-id", required=True, help="stable artifact directory name")
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--samples", type=int, default=32)
    parser.add_argument("--lens", type=float, default=12.0)
    parser.add_argument("--kelvin", type=int, default=3200)
    parser.add_argument("--exposure", type=float, default=0.0)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--blender", default=BLENDER_DEFAULT)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    if args.frames < 1 or args.threads < 1 or args.samples < 1 or args.gpu_index < 0:
        parser.error("frames, threads, and samples must be positive; gpu-index must be nonnegative")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id must match [A-Za-z0-9_-]+")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
