"""Reproducible, contained renderer and telemetry harness for spike 09.

This script builds the production Blender plan, launches the production worker, and
stores every artifact beneath this spike directory. It deliberately does not decide
which render is visually acceptable.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))
BLENDER_DEFAULT = r"D:\Blender\blender.exe"
from orchestrator.adapters.blender.scene import RenderRequest, SceneSpec, build_scene_plan
from orchestrator.domain.entities import CameraProfile
from orchestrator.platform.containment import ProjectRoot

# Reuse the established NVML/process telemetry and progress parsers from spike 08.
_spike08 = importlib.import_module("spikes.08_living_room.render_room")
DeviceSampler = _spike08.DeviceSampler
BlenderProcessSampler = _spike08.BlenderProcessSampler
_frame_times = _spike08._frame_times
_progress_cadence = _spike08._progress_cadence
_progress_timestamps = _spike08._progress_timestamps


def _living_room(look: str):
    from orchestrator.adapters.blender.scene import american_living_room
    if look == "low_key":
        return american_living_room(look="low_key")
    return american_living_room()


def _profile(args: argparse.Namespace) -> CameraProfile:
    if args.profile_json:
        return CameraProfile.model_validate_json(args.profile_json_text)
    return CameraProfile(
        camera_id="living-room-cctv",
        profile_name="Spike 09 shared CCTV camera",
        field_of_view_mm=args.lens,
        barrel_distortion=0.0,
        exposure_ev=args.exposure,
        practical_light_color_k=args.kelvin,
        practical_light_intensity=1.0,
        monochrome_night=args.mono if args.mono is not None else args.look == "low_key",
        camera_position=args.camera_location,
        camera_mount="fixed corner mount below ceiling",
    )


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
    cache_dir = root.make_dir("_work/cache")
    config_dir = root.make_dir(f"{run_rel}/config")
    for child in ("scripts", "datafiles", "extensions"):
        root.make_dir(f"{run_rel}/config/{child}")
    root.make_dir(f"{run_rel}/frames")
    if args.scene_json_text:
        scene = SceneSpec.model_validate_json(args.scene_json_text)
    else:
        scene = _living_room(args.look)
    camera_location = args.camera_location or tuple(scene.camera_location)
    camera_target = args.camera_target or tuple(scene.camera_target)
    scene = scene.model_copy(update={"camera_location": camera_location, "camera_target": camera_target})
    profile = _profile(args)
    root.write_text(f"{run_rel}/resolved_scene.json", scene.model_dump_json(indent=2) + "\n")
    root.write_text(f"{run_rel}/resolved_profile.json", profile.model_dump_json(indent=2) + "\n")
    request = RenderRequest(
        shot_id=f"spike09-{args.look}", shot_version=1, run_id=args.run_id,
        degrade_seed=1977, duration_sec=args.frames / 60,
        master_resolution=(args.width, args.height), camera_profile=profile,
        scene=scene,
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
    process_sampler: BlenderProcessSampler | None = None
    return_code = 1
    launch_monotonic: float | None = None
    def capture(log_path: Path) -> None:
        nonlocal process_sampler, return_code, launch_monotonic
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
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
    root.write_with(f"{run_rel}/blender.log", capture)
    wall_seconds = time.monotonic() - start
    telemetry = sampler.stop()
    process_telemetry = process_sampler.stop() if process_sampler else {"other_blender_processes_observed": [], "sampling_errors": ["sampler did not start"]}
    output_text = root.read_text(f"{run_rel}/blender.log", encoding="utf-8")
    validation_errors: list[str] = []
    actual_metadata = None
    if metadata.exists():
        try:
            actual_metadata = json.loads(root.read_text(f"{run_rel}/metadata.json", encoding="utf-8"))
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
        "look": args.look, "run_id": args.run_id,
        "command": command, "configuration": {"frames": args.frames, "width": args.width,
                         "height": args.height, "samples": args.samples, "threads": args.threads,
                         "engine": "eevee", "camera_location": camera_location,
                         "camera_target": camera_target, "monochrome_night": profile.monochrome_night, "lens_mm": args.lens,
                         "practical_light_color_k": args.kelvin, "exposure_ev": args.exposure},
        "return_code": return_code,
        "environment_paths": {key: env[key] for key in (
            "TMP", "TEMP", "TMPDIR", "XDG_CACHE_HOME", "BLENDER_USER_CONFIG",
            "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES", "BLENDER_USER_EXTENSIONS",
            "CUDA_CACHE_PATH", "OPTIX_CACHE_PATH")},
        "validation_errors": validation_errors,
        "wall_seconds_including_blender_startup": wall_seconds,
        "progress_receipt_times_seconds_since_launch": [
            stamp - launch_monotonic for stamp in _progress_timestamps("\n".join(progress_output))] if launch_monotonic is not None else [],
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
        "named_iteration_png": f"{args.run_id}.png",
        "resolved_scene_json": f"{run_rel}/resolved_scene.json",
        "resolved_profile_json": f"{run_rel}/resolved_profile.json",
    }
    intervals = telemetry_record["progress_intervals_seconds"]
    if intervals:
        telemetry_record["warm_progress_cadence_seconds"] = sum(intervals) / len(intervals)
    markers = [float(re.match(r"([0-9]+(?:\.[0-9]+)?) ", line).group(1))
               for line in progress_output]
    if markers and launch_monotonic is not None:
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
    parser.add_argument("--look", choices=("bright", "low_key"), default="bright")
    parser.add_argument("--scene-json", help="contained path to a full SceneSpec JSON file")
    parser.add_argument("--profile-json", help="contained path to a full CameraProfile JSON file")
    parser.add_argument("--camera-location", nargs=3, type=float)
    parser.add_argument("--camera-target", nargs=3, type=float)
    parser.add_argument("--mono", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--run-id", required=True, help="stable artifact directory name")
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--samples", type=int, default=32)
    parser.add_argument("--lens", type=float, default=12.0)
    parser.add_argument("--kelvin", type=int, default=6500)
    parser.add_argument("--exposure", type=float, default=0.0)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--blender", default=BLENDER_DEFAULT)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    source_root = ProjectRoot(REPO)
    if args.scene_json:
        args.scene_json_text = source_root.read_text(args.scene_json)
    else:
        args.scene_json_text = None
    if args.profile_json:
        args.profile_json_text = source_root.read_text(args.profile_json)
    else:
        args.profile_json_text = None
    if args.frames < 1 or args.threads < 1 or args.samples < 1 or args.gpu_index < 0:
        parser.error("frames, threads, and samples must be positive; gpu-index must be nonnegative")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id must match [A-Za-z0-9_-]+")
    # Copy the measured spike-08 reference using contained input/output paths.
    spike_root = ProjectRoot(HERE)
    reference = ProjectRoot(REPO / "spikes" / "08_living_room")
    ref = "after-living-room.png"
    if reference.resolve(ref).is_file() and not spike_root.resolve("current-room.png").exists():
        spike_root.write_bytes("current-room.png", reference.read_bytes(ref))
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
