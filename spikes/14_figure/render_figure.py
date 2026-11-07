"""Render matched before/after FR-20 figure stills from the low-key living room.

The before mode restores the prior single-box worker object from the new plan so both
images share the same room, camera, path, frame, and render settings. The degraded still
uses the production Low-light B&W filter graph on a looped copy of the selected master
frame, solely to inspect the 352x288 output surface.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from pydantic import TypeAdapter

from orchestrator.adapters.blender.scene import (
    RenderRequest,
    american_living_room,
    build_scene_plan,
)
from orchestrator.adapters.postprofile.profile import (
    EncodeSpec,
    OverlaySpec,
    PostProfileSpec,
    build_filter_graph,
    validate_profile,
)
from orchestrator.domain.anomaly import AnomalySpec
from orchestrator.domain.entities import CameraProfile
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.service.dependencies import _unmeasured_low_light_bw_defaults

BLENDER = Path(r"D:\Blender\blender.exe")
WORKER = REPO / "orchestrator" / "adapters" / "blender" / "worker.py"
SAMPLE_FRAME = 211  # halfway through the two-second path that starts at 2.5 seconds
SHOT_ID = "fr20-figure"
DEGRADE_SEED = 1977
ANOMALY_DATA = {
    "kind": "linear_walk_path",
    "timing": {"duration_sec": 2.0},
    "subtlety": 2,
    "height_m": 1.8,
    "width_m": 0.5,
    "depth_m": 0.35,
    "minimum_distance_m": 2.0,
    "maximum_on_frame_height_px": 400,
    "path": [[-1.0, 1.5, 0.0], [-1.0, 2.5, 0.0]],
}


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(argv, cwd=cwd, env=env, check=False)
    if completed.returncode:
        raise RuntimeError(f"command exited {completed.returncode}: {argv[0]}")


def _restore_proxy(plan: dict[str, object]) -> None:
    """Replace the new parts with the exact old box shape for the comparison still."""
    scene = plan["scene"]
    anomaly = plan["anomaly"]
    assert isinstance(scene, dict) and isinstance(anomaly, dict)
    objects = scene["objects"]
    assert isinstance(objects, list)
    parts = [
        item for item in objects
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].startswith("FR20AnomalyFigure")
    ]
    torso = next(item for item in parts if item["name"] == "FR20AnomalyFigure")
    height_m = float(anomaly["height_m"])
    width_m = float(anomaly["width_m"])
    depth_m = float(anomaly["depth_m"])
    path = anomaly["path"]
    assert isinstance(path, list) and path
    base = [float(path[0][0]), float(path[0][1]), float(path[0][2]) + height_m / 2]
    raw_keyframes = torso.get("keyframes", [])
    assert isinstance(raw_keyframes, list)
    keyframes = []
    for key in raw_keyframes:
        assert isinstance(key, dict)
        location = list(key["location"])
        location[2] -= 0.15 * height_m
        keyframes.append({**key, "location": location})
    proxy = {
        "name": "FR20AnomalyFigure",
        "kind": "box",
        "location": base,
        "dimensions": [width_m, depth_m, height_m],
        "material": torso["material"],
        "keyframes": keyframes,
        "visibility_keyframes": [dict(key) for key in torso["visibility_keyframes"]],
    }
    scene["objects"] = [item for item in objects if item not in parts] + [proxy]


def _ffmpeg_version() -> str:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        check=True,
    )
    return normalize_ffmpeg_identity(result.stdout)


def render(mode: str, run_id: str) -> dict[str, object]:
    root = ProjectRoot(REPO)
    run_rel = f"spikes/14_figure/_work/{run_id}"
    shared_cache_rel = "spikes/14_figure/_work/cache"
    if root.resolve(run_rel).exists():
        raise FileExistsError(f"run directory already exists; choose another --run-id: {run_rel}")
    root.make_dir(shared_cache_rel)
    for relative in (
        f"{run_rel}/frames",
        f"{run_rel}/temp",
        f"{run_rel}/config/scripts",
        f"{run_rel}/config/datafiles",
        f"{run_rel}/config/extensions",
        f"{run_rel}/assets/fonts",
    ):
        root.make_dir(relative)

    anomaly = TypeAdapter(AnomalySpec).validate_python(ANOMALY_DATA)
    camera_path = HERE.parent / "09_low_key" / "final-profile.json"
    camera = CameraProfile.model_validate_json(camera_path.read_text(encoding="utf-8"))
    request = RenderRequest(
        shot_id=SHOT_ID,
        shot_version=1,
        run_id=f"figure-{mode}",
        degrade_seed=DEGRADE_SEED,
        duration_sec=5.0,
        event_sec=2.5,
        anomaly=anomaly,
        master_resolution=(1280, 960),
        camera_profile=camera,
        scene=american_living_room(look="low_key"),
        engine="eevee",
        samples=32,
    )
    plan = build_scene_plan(request)
    if mode == "before":
        _restore_proxy(plan)
    plan["frame_start"] = SAMPLE_FRAME
    plan["frame_end"] = SAMPLE_FRAME

    exr_rel = f"{run_rel}/frames/frame_{SAMPLE_FRAME:06d}.exr"
    blend_rel = f"{run_rel}/scene.blend"
    metadata_rel = f"{run_rel}/metadata.json"
    log_rel = f"{run_rel}/blender.log"
    plan["outputs"] = {
        "blend": str(root.resolve(blend_rel)),
        "metadata": str(root.resolve(metadata_rel)),
        "frames": [str(root.resolve(exr_rel))],
    }
    plan["runtime"] = {"temp_dir": str(root.resolve(f"{run_rel}/temp"))}
    plan_rel = f"{run_rel}/plan.json"
    root.write_text(plan_rel, json.dumps(plan, indent=2, sort_keys=True) + "\n")

    env = os.environ.copy()
    env.update({
        "TMP": str(root.resolve(f"{run_rel}/temp")),
        "TEMP": str(root.resolve(f"{run_rel}/temp")),
        "TMPDIR": str(root.resolve(f"{run_rel}/temp")),
        "XDG_CACHE_HOME": str(root.resolve(shared_cache_rel)),
        "BLENDER_USER_CONFIG": str(root.resolve(f"{run_rel}/config")),
        "BLENDER_USER_SCRIPTS": str(root.resolve(f"{run_rel}/config/scripts")),
        "BLENDER_USER_DATAFILES": str(root.resolve(f"{run_rel}/config/datafiles")),
        "BLENDER_USER_EXTENSIONS": str(root.resolve(f"{run_rel}/config/extensions")),
        "CUDA_CACHE_PATH": str(root.resolve(shared_cache_rel)),
        "OPTIX_CACHE_PATH": str(root.resolve(shared_cache_rel)),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    command = [
        str(BLENDER), "--background", "--factory-startup", "--disable-autoexec",
        "--python-exit-code", "1", "--threads", "8", "--python", str(WORKER),
        "--", str(root.resolve(plan_rel)),
    ]
    started = time.monotonic()
    completed = subprocess.CompletedProcess(command, 1)

    def run_blender(log_path: Path) -> None:
        nonlocal completed
        with log_path.open("wb") as log:
            completed = subprocess.run(command, cwd=REPO, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, check=False)

    root.write_with(log_rel, run_blender)
    if completed.returncode:
        tail = root.read_text(log_rel, encoding="utf-8")[-3000:]
        raise RuntimeError(f"Blender exited {completed.returncode}:\n{tail}")
    worker_seconds = json.loads(root.read_text(metadata_rel, encoding="utf-8"))["elapsed_seconds"]

    master_png_rel = f"spikes/14_figure/{mode}-master-1280x960.png"
    master_png = root.resolve(master_png_rel)

    def convert_master(output: Path) -> None:
        _run([
            "ffmpeg", "-y", "-v", "error", "-i", str(root.resolve(exr_rel)),
            "-frames:v", "1", "-vf",
            "format=gbrpf32le,zscale=primariesin=bt709:transferin=linear:matrixin=gbr:rangein=full:primaries=bt709:transfer=iec61966-2-1:matrix=gbr:range=full,format=rgb24",
            "-color_primaries", "bt709", "-color_trc", "iec61966-2-1", str(output),
        ], cwd=REPO)

    root.write_with(master_png_rel, convert_master)

    font_source = Path(r"C:\Windows\Fonts\consola.ttf")
    font_rel = f"{run_rel}/assets/fonts/overlay.ttf"

    def copy_font(output: Path) -> None:
        shutil.copyfile(font_source, output)

    root.write_with(font_rel, copy_font)
    ffmpeg_identity = _ffmpeg_version()
    _profile_camera, profile_entity = _unmeasured_low_light_bw_defaults(
        ffmpeg_identity=ffmpeg_identity,
    )
    profile = PostProfileSpec(
        identity=profile_entity,
        output_fps=profile_entity.output_fps,
        storage_resolution=profile_entity.storage_resolution,
        sample_aspect_ratio=profile_entity.sample_aspect_ratio,
        artifact_schedule=tuple(profile_entity.artifact_schedule),
        overlay=(
            OverlaySpec.model_validate(
                profile_entity.overlay.model_dump(
                    mode="python", exclude={"schema_version", "font_relative"}
                )
            )
            if profile_entity.overlay is not None else None
        ),
        encode=EncodeSpec.model_validate(
            profile_entity.encode.model_dump(mode="python", exclude={"schema_version"})
        ),
    )
    validated = validate_profile(
        profile,
        _profile_camera,
        master_frames=6,
        master_rate=Fraction(60, 1),
    )
    graph = build_filter_graph(
        validated,
        shot_id=SHOT_ID,
        degrade_seed=DEGRADE_SEED,
        font_path=font_rel,
    )
    degraded_mp4_rel = f"{run_rel}/degraded.mp4"

    def degrade(output: Path) -> None:
        _run([
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loop", "1", "-framerate", "60",
            "-i", str(master_png), "-t", "0.1", "-map", "0:v:0", "-vf", graph,
            "-frames:v", "1", "-c:v", "libx264", "-preset", "medium", "-crf", "30",
            "-pix_fmt", "yuv420p", "-threads", "1", "-map_metadata", "-1",
            "-flags", "+bitexact", "-fflags", "+bitexact", str(output),
        ], cwd=REPO)

    root.write_with(degraded_mp4_rel, degrade)
    delivered_rel = f"spikes/14_figure/{mode}-delivered-352x288.png"

    def extract_delivered(output: Path) -> None:
        _run([
            "ffmpeg", "-y", "-v", "error", "-i", str(root.resolve(degraded_mp4_rel)),
            "-frames:v", "1", str(output),
        ], cwd=REPO)

    root.write_with(delivered_rel, extract_delivered)
    return {
        "mode": mode,
        "master_image": master_png_rel,
        "delivered_image": delivered_rel,
        "render_wall_seconds": round(time.monotonic() - started, 3),
        "blender_worker_seconds": round(float(worker_seconds), 3),
        "degradation_graph": graph,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("before", "after"))
    parser.add_argument("--run-id", help="unique contained working directory name")
    args = parser.parse_args()
    print(json.dumps(render(args.mode, args.run_id or args.mode), indent=2))


if __name__ == "__main__":
    main()
