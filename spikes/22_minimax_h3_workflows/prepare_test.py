from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
SPIKE = "spikes/22_minimax_h3_workflows"
SOURCE = (
    "spikes/20_overnight_production/_work/20261108T001855_03bdf358/projects/"
    "scripted_03bdf358/renders/s1/v0002/3859f220afd54b6f9bcee4a0743336c9/"
    "attempt-0001/master.mkv"
)
EXPECTED_SOURCE_SHA256 = "9dbb789b260bfde899a549eaf18cfc20c4d8feed050f59edfeb2f25848f61389"
PROMPT = (
    "The target video is an edited version of <Video 1>. Preserve the fixed security-camera "
    "viewpoint, room layout, furnishings, lighting, and lack of camera motion. Keep the room "
    "empty. Generate a continuous, natural indoor ambience soundtrack: gentle ventilation "
    "airflow, a low steady fan hum, and faint distant building noise. No speech, voices, music, "
    "footsteps, dramatic impacts, or jump-scare sounds. Keep the sound present and restrained, "
    "with no abrupt start or cut."
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_bounded(args: list[str], *, env: dict[str, str], label: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout, check=False)
    log_rel = CURRENT_RUN + "/" + label + ".log"
    PROJECT.write_text(log_rel, "COMMAND: " + json.dumps(args) + "\nRETURN_CODE: " + str(result.returncode) + "\nSTDOUT:\n" + result.stdout + "\nSTDERR:\n" + result.stderr)
    if result.returncode:
        raise RuntimeError(f"{label} failed ({result.returncode}); see {log_rel}")
    return result


def ffprobe(path: Path, env: dict[str, str]) -> dict[str, Any]:
    probe_args = ["ffprobe", "-v", "error"]
    if path.name != "master.mkv":
        probe_args.append("-count_frames")
    probe_args.extend(["-show_streams", "-show_format", "-of", "json", str(path)])
    result = run_bounded(probe_args, env=env, label="source_probe" if path.name == "master.mkv" else "input_probe")
    return json.loads(result.stdout)


def prepare() -> str:
    global CURRENT_RUN
    source_path = PROJECT.resolve(SOURCE)
    if sha256(source_path) != EXPECTED_SOURCE_SHA256:
        raise ValueError("Source master SHA256 differs from the contract; refusing preparation")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    CURRENT_RUN = f"{SPIKE}/_work/test_inputs/{run_id}"
    run_dir = PROJECT.resolve(CURRENT_RUN)
    if run_dir.exists():
        raise FileExistsError(f"Refusing to reuse completed or existing run: {CURRENT_RUN}")
    PROJECT.make_dir(CURRENT_RUN)
    temp_rel = CURRENT_RUN + "/tmp"
    cache_rel = CURRENT_RUN + "/cache"
    env = os.environ.copy()
    for key, rel in (("TMP", temp_rel), ("TEMP", temp_rel), ("TMPDIR", temp_rel), ("XDG_CACHE_HOME", cache_rel)):
        env[key] = str(PROJECT.make_dir(rel))
    source_info = ffprobe(source_path, env)
    vstreams = [s for s in source_info.get("streams", []) if s.get("codec_type") == "video"]
    if len(vstreams) != 1:
        raise ValueError(f"Expected one video stream in source; found {len(vstreams)}")
    source_video = vstreams[0]
    if (int(source_video.get("width", 0)), int(source_video.get("height", 0))) != (1280, 960):
        raise ValueError("Source dimensions differ from contract 1280x960")
    input_rel = CURRENT_RUN + "/input_silent.mp4"
    input_path = PROJECT.resolve(input_rel)
    video_cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-n", "-ss", "0", "-i", str(source_path),
        "-t", "8", "-map", "0:v:0", "-an", "-vf", "fps=24,scale=1024:768:flags=lanczos,setsar=1",
        "-frames:v", "192", "-r", "24", "-vsync", "cfr", "-c:v", "libx264", "-preset", "medium",
        "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", str(input_path),
    ]
    PROJECT.write_with(input_rel, lambda target: run_bounded(video_cmd[:-1] + [str(target)], env=env, label="video_encode"))
    sheet_rel = CURRENT_RUN + "/contact_sheet.png"
    sheet_cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-n", "-i", str(input_path), "-vf",
        "select=eq(n\\,0)+eq(n\\,48)+eq(n\\,96)+eq(n\\,144)+eq(n\\,191),scale=320:240,tile=5x1",
        "-frames:v", "1", "-f", "image2", str(PROJECT.resolve(sheet_rel)),
    ]
    PROJECT.write_with(sheet_rel, lambda target: run_bounded(sheet_cmd[:-1] + [str(target)], env=env, label="contact_sheet"))
    request = {
        "model": "MiniMax-H3", "resolution": "768P", "duration": 8, "ratio": "4:3",
        "content": [
            {"type": "text", "text": PROMPT},
            {"type": "video_url", "role": "reference_video", "video_url": {"url": "REPLACE_WITH_PROVIDER_VIDEO_URL_OR_DATA_URI"}},
        ],
    }
    prompt_rel = CURRENT_RUN + "/prompt.txt"
    request_rel = CURRENT_RUN + "/request_template.json"
    PROJECT.write_text(prompt_rel, PROMPT + "\n")
    PROJECT.write_text(request_rel, json.dumps(request, indent=2, ensure_ascii=False) + "\n")
    probe = ffprobe(input_path, env)
    video_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    audio_count = sum(s.get("codec_type") == "audio" for s in probe.get("streams", []))
    if len(video_streams) != 1 or audio_count != 0:
        raise RuntimeError("Prepared input stream layout did not meet silent single-video contract")
    stream = video_streams[0]
    if (stream.get("width"), stream.get("height"), stream.get("avg_frame_rate"), stream.get("sample_aspect_ratio")) != (1024, 768, "24/1", "1:1"):
        raise RuntimeError(f"Prepared stream metadata mismatch: {stream}")
    if int(stream.get("nb_read_frames", 0)) != 192:
        raise RuntimeError(f"Prepared frame count mismatch: {stream.get('nb_read_frames')}")
    if abs(float(stream.get("duration", 0)) - 8.0) > 0.001:
        raise RuntimeError(f"Prepared duration mismatch: {stream.get('duration')}")
    version_result = run_bounded(["ffmpeg", "-version"], env=env, label="ffmpeg_version")
    manifest_rel = CURRENT_RUN + "/manifest.json"
    code_path = PROJECT.resolve("spikes/22_minimax_h3_workflows/prepare_test.py")
    manifest = {
        "schema_version": 1,
        "status": "prepared_not_submitted",
        "media_path_base": "worktree_root",
        "shot_id": "s1",
        "shot_version": 2,
        "source": {
            "path": SOURCE, "sha256": sha256(source_path), "start_seconds": 0, "end_seconds": 8,
            "upstream_rights_status": "pending review; internal-evaluation-only source figure not cleared for hosted use",
        },
        "input_video": {
            "path": input_rel, "sha256": sha256(input_path), "width": 1024, "height": 768,
            "fps": "24/1", "frame_count": 192, "duration_seconds": 8, "audio_stream_count": 0,
        },
        "prompt": {"path": prompt_rel, "sha256": sha256(PROJECT.resolve(prompt_rel))},
        "request_template": {"path": request_rel, "sha256": sha256(PROJECT.resolve(request_rel))},
        "contact_sheet": {"path": sheet_rel, "sha256": sha256(PROJECT.resolve(sheet_rel)), "frame_indices": [0, 48, 96, 144, 191]},
        "preparation": {
            "ffmpeg_version": version_result.stdout.splitlines()[0] if version_result.stdout else "unknown",
            "ffmpeg_command": video_cmd,
            "exact_transformations": ["select source interval [0,8)", "fps=24 frame selection, no interpolation", "scale 1280x960 to 1024x768 preserving 4:3", "set SAR 1:1", "H.264 libx264 yuv420p", "remove all audio"],
            "code": {"path": "spikes/22_minimax_h3_workflows/prepare_test.py", "sha256": sha256(code_path)},
            "contact_sheet_sample_times_seconds": [0, 2, 4, 6, 191 / 24],
        },
        "provenance": {
            "proposed_model": "MiniMax-H3", "proposed_route": "documented direct API; route/access pending",
            "generation_submitted": False, "no_input_audio": True,
            "estimated_direct_api_cost_usd": 1.28, "cost_as_of": "2026-11-09",
            "cost_basis": "input 8 seconds * USD 0.08 + output 8 seconds * USD 0.08; excludes other charges; not Cloud pricing",
            "approval_status": "unapproved; template is not permission to upload",
            "rights_review": "pending",
        },
    }
    PROJECT.write_text(manifest_rel, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest_rel


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare an offline silent-video + text H3 test input")
    parser.add_argument("--out", help="Optional worktree-relative run directory (must not exist)")
    args = parser.parse_args()
    if args.out:
        raise ValueError("Output run IDs are generated uniquely; --out is intentionally unsupported")
    print(json.dumps({"manifest": prepare()}, indent=2))


CURRENT_RUN = ""
if __name__ == "__main__":
    main()
