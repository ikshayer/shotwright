"""Build and render the furnished found-footage walkthrough with a contained run directory."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestrator.adapters.blender.scene import (
    RenderRequest,
    american_living_room,
    build_scene_plan,
)
from orchestrator.domain.entities import CameraProfile
from orchestrator.platform.containment import ProjectRoot

FPS = 15
DURATION = 6
WIDTH, HEIGHT = 384, 288
SEED = 30518
START_XY = (1.48, -1.72)
STOP_XY = (0.94, 0.84)
PACE_START = 0.45
PACE_END = 4.95
HALL_AIM_XY = (0.66, 1.98)


def _ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3.0 - 2.0 * t)


def _camera_pose(frame: int) -> dict:
    t = (frame - 1) / FPS
    pace = min(1.0, max(0.0, (t - PACE_START) / (PACE_END - PACE_START)))
    travel = _ease(pace)
    gait = 4 * pace * (1 - pace)
    foot = math.tau * 1.65 * (t - 0.91)
    dx, dy = (finish - begin for begin, finish in zip(START_XY, STOP_XY, strict=True))
    length = math.hypot(dx, dy)
    sway = 0.012 * gait * math.sin(foot / 2)
    x = START_XY[0] + dx * travel + dy / length * sway
    y = START_XY[1] + dy * travel - dx / length * sway
    z = 1.59 + 0.024 * gait * math.sin(foot) + 0.006 * math.sin(2.0 * t)
    heading = math.atan2(dx, dy)
    opening_glance = math.radians(-10) * (1 - _ease(t / PACE_START))
    hall_heading = math.atan2(HALL_AIM_XY[0] - STOP_XY[0], HALL_AIM_XY[1] - STOP_XY[1])
    settle = _ease((t - 4.70) / 0.65)
    yaw = heading + opening_glance + settle * (hall_heading - heading)
    yaw += math.radians(0.75 * gait * math.sin(foot / 2 + 0.35))
    pitch = math.radians(-7 + 5 * settle + 0.8 * gait * math.sin(foot + 0.8))
    target = [x + math.sin(yaw), y + math.cos(yaw), z + math.tan(pitch)]
    if not 0.90 <= x <= 1.57 or not -1.78 <= y <= 0.93 or not 1.52 <= z <= 1.66:
        raise ValueError(f"Camera frame {frame} leaves the open aisle")
    return {
        "frame": frame, "location": [x, y, z], "target": target,
        "roll": math.radians(1.1 * gait * math.sin(foot / 2 + 0.3) + 0.32 * math.sin(2.7 * t)),
    }


def _path_metrics(poses: list[dict]) -> dict:
    start, finish = poses[0]["location"], poses[-1]["location"]
    dx, dy = finish[0] - start[0], finish[1] - start[1]
    travel = math.hypot(dx, dy)
    lateral = []
    clearance = []
    angles = []
    walking_speed = 0.2
    for prior, pose, following in zip(poses, poses[1:], poses[2:], strict=False):
        x, y, _ = pose["location"]
        lateral.append(abs(dy * (x - start[0]) - dx * (y - start[1])) / travel)
        clearance.append(math.hypot(max(abs(x) - 0.74, 0), max(abs(y + 0.15) - 0.46, 0)))
        vx = following["location"][0] - prior["location"][0]
        vy = following["location"][1] - prior["location"][1]
        if math.hypot(vx, vy) * FPS / 2 < walking_speed:
            continue
        look_x, look_y = pose["target"][0] - x, pose["target"][1] - y
        delta = math.atan2(abs(vx * look_y - vy * look_x), vx * look_x + vy * look_y)
        angles.append(math.degrees(delta))
    return {
        "horizontal_travel_m": round(travel, 3),
        "max_lateral_offset_m": round(max(lateral), 3),
        "min_coffee_table_clearance_m": round(min(clearance), 3),
        "active_walk_min_speed_m_s": walking_speed,
        "active_walk_frames": len(angles),
        "active_walk_mean_gaze_delta_deg": round(sum(angles) / len(angles), 2),
        "active_walk_max_gaze_delta_deg": round(max(angles), 2),
    }


def _room_tone() -> bytes:
    sample_rate = 48_000
    rng = random.Random(SEED)
    hiss, creak = 0.0, 0.0
    samples = array("h")
    steps = (0.91, 1.54, 2.19, 2.82, 3.50, 4.19, 4.77)
    for index in range(sample_rate * DURATION):
        t = index / sample_rate
        white = rng.uniform(-1.0, 1.0)
        hiss = 0.94 * hiss + 0.06 * white
        sample = 0.003 * white + 0.004 * hiss + 0.0012 * math.sin(math.tau * 60 * t)
        age = next((t - step for step in steps if 0.0 <= t - step < 0.26), None)
        if age is not None:
            creak = 0.83 * creak + 0.17 * white
            contact = math.sin(math.pi * min(age / 0.023, 0.5)) * math.exp(-12 * age)
            floor = math.sin(math.tau * (83 * age - 21 * age * age))
            friction = math.sin(math.tau * (318 * age - 175 * age * age))
            sample += contact * (0.027 * floor + 0.010 * friction + 0.016 * creak)
        samples.append(round(sample * 32767))
    if sys.byteorder != "little":
        samples.byteswap()
    content = io.BytesIO()
    with wave.open(content, "wb") as stream:
        stream.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    return content.getvalue()


def _write_plan(root: ProjectRoot, *, samples: int, preview: bool, run_id: str) -> Path:
    request = RenderRequest(
        shot_id="handheld-walkthrough", shot_version=1, run_id=run_id, degrade_seed=SEED,
        duration_sec=DURATION, camera_profile=CameraProfile(
            camera_id="walkthrough", profile_name="Walkthrough diagnostic", field_of_view_mm=17,
        ), scene=american_living_room(), engine="cycles", samples=samples, gpu_device="CPU",
    )
    plan = build_scene_plan(request)
    sequence = list(range(1, FPS * DURATION + 1))
    poses = [_camera_pose(frame) for frame in sequence]
    selected = [1, 24, 47, 71, 83] if preview else sequence
    plan.update(width=WIDTH, height=HEIGHT, fps=FPS, frame_end=0)
    plan["runtime"] = {"temp_dir": str(root.make_dir("tmp"))}
    root.make_dir("frames")
    plan["outputs"] = {
        "blend": str(root.write_with("base-room.blend", lambda path: None)),
        "metadata": str(root.write_with("base-room-worker.json", lambda path: None)),
        "frames": [],
    }
    plan["walkthrough"] = {
        "camera": poses,
        "path_metrics": _path_metrics(poses),
        "blend": str(root.write_with("walkthrough.blend", lambda path: None)),
        "frames": [
            [frame, str(root.write_with(f"frames/frame_{frame:03d}.png", lambda path: None))]
            for frame in selected
        ],
        "establishing": str(root.write_with("locked-establishing.png", lambda path: None)),
        "report": str(root.write_with("walkthrough.json", lambda path: None)),
        "worker": str(REPO / "orchestrator/adapters/blender/worker.py"),
    }
    return root.write_text("plan.json", json.dumps(plan, indent=2))


def _checked_program(root: ProjectRoot, name: str, argv: list[str], *, timeout: int, env=None) -> None:
    print(f"{name}: {root.path}", flush=True)

    def run(log_path: Path) -> None:
        with log_path.open("xb") as log:
            completed = subprocess.run(
                argv, cwd=root.path, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, shell=False, timeout=timeout, check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError(f"{name} exited {completed.returncode}. See {log_path}")

    root.write_with(f"{name}.log", run)


def _probe(root: ProjectRoot, ffprobe: str, name: str, movie: Path, *, frames: int, env) -> None:
    argv = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(movie)]
    _checked_program(root, name, argv, timeout=30, env=env)
    data = json.loads(root.read_text(f"{name}.log"))
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
    expected = {"width": WIDTH, "height": HEIGHT, "r_frame_rate": f"{FPS}/1", "nb_frames": str(frames)}
    if any(video.get(key) != value for key, value in expected.items()) or audio["sample_rate"] != "48000":
        raise RuntimeError(f"Unexpected delivered stream shape. See {name}.log")
    root.write_text(f"{name}.json", json.dumps(data, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", default=r"D:\Blender\blender.exe")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--samples", type=int, default=8, choices=range(4, 33), metavar="4..32")
    parser.add_argument("--preview", action="store_true", help="Render three review frames only")
    parser.add_argument("--plan-only", action="store_true", help="Write the plan without starting Blender")
    options = parser.parse_args()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
    repo = ProjectRoot(REPO)
    root = ProjectRoot(repo.make_dir(f"spikes/30_at18_found_footage/_work/{run_id}"))
    plan_path = _write_plan(root, samples=options.samples, preview=options.preview, run_id=run_id)
    print(f"Plan: {plan_path}", flush=True)
    if options.plan_only:
        return

    blender = shutil.which(options.blender)
    ffmpeg = shutil.which(options.ffmpeg)
    ffprobe = shutil.which(options.ffprobe)
    if not blender or not ffmpeg or not ffprobe:
        raise SystemExit("Supply existing Blender, FFmpeg, and FFprobe executables")
    runtime = str(root.resolve("tmp"))
    env = dict(os.environ, TMP=runtime, TEMP=runtime, TMPDIR=runtime, BLENDER_USER_CONFIG=runtime)
    argv = [
        blender, "--background", "--factory-startup", "--disable-autoexec", "--threads", "8",
        "--python-exit-code", "1", "--python", str(Path(__file__).with_name("render.py")), "--", str(plan_path),
    ]
    _checked_program(root, "blender", argv, timeout=1_500, env=env)
    if not root.resolve("walkthrough.blend").is_file():
        raise RuntimeError("Blender did not retain the editable scene")
    if options.preview:
        return

    audio = root.write_bytes("room-tone.wav", _room_tone())
    frames = root.resolve("frames/frame_%03d.png")
    movie = root.write_with("walkthrough.mp4", lambda path: None)
    ffmpeg_argv = [
        ffmpeg, "-hide_banner", "-nostdin", "-n", "-framerate", str(FPS), "-start_number", "1",
        "-i", str(frames), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-vf",
        f"format=yuv420p,noise=alls=9:allf=t+u:all_seed={SEED},hue=s=0,setsar=1",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-threads", "1",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(movie),
    ]
    _checked_program(root, "ffmpeg", ffmpeg_argv, timeout=90, env=env)
    _probe(root, ffprobe, "ffprobe", movie, frames=FPS * DURATION, env=env)
    print(f"Movie: {movie}", flush=True)

    rough_cut = root.write_with("rough-cut.mp4", lambda path: None)
    insert = root.resolve("locked-establishing.png")
    graph = (
        f"[0:v]format=yuv420p,noise=alls=9:allf=t+u:all_seed={SEED},hue=s=0,setsar=1,"
        "trim=end_frame=21,setpts=PTS-STARTPTS[est];"
        "[2:a]atrim=duration=1.4,asetpts=PTS-STARTPTS[silence];"
        "[1:v]setpts=PTS-STARTPTS[walk];[1:a]asetpts=PTS-STARTPTS[walk_audio];"
        "[est][silence][walk][walk_audio]concat=n=2:v=1:a=1[v][a]"
    )
    rough_argv = [
        ffmpeg, "-hide_banner", "-nostdin", "-n", "-loop", "1", "-framerate", str(FPS),
        "-i", str(insert), "-i", str(movie), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-r", str(FPS), "-threads", "1",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(rough_cut),
    ]
    _checked_program(root, "rough-cut-ffmpeg", rough_argv, timeout=90, env=env)
    _probe(root, ffprobe, "rough-cut-ffprobe", rough_cut, frames=21 + FPS * DURATION, env=env)
    print(f"Rough cut: {rough_cut}", flush=True)


if __name__ == "__main__":
    main()
