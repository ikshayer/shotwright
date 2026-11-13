from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from orchestrator.platform.containment import ProjectRoot

SPIKE = "spikes/23_joint_av_analyzer/_work/media"
AUDIO_ROOT = "references/_work/mandela333_audio/20261111T062929Z_c9ede868"
VIDEO_URL = "https://www.youtube.com/watch?v=1jMFrVk2d_I"
AUDIO_REL = f"{AUDIO_ROOT}/mandela333_23m17s_to_23m52s_native48k_float.wav"
AUDIO_SHA = "07258723a203be7129832b5e237362b9d4dfa1782dfd49416fc166cfbf60a809"
START, END, DURATION = 1397, 1432, 35


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(root: ProjectRoot, command: list[str], *, env: dict[str, str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=root.path, env=env, text=True, capture_output=True, timeout=timeout, check=False)
    root.write_text(f"{SPIKE}/receipts/{datetime.now(UTC).strftime('%Y%m%dT%H%M%S_%fZ')}_{len(command)}.log", result.stdout + "\n--- STDERR ---\n" + result.stderr)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command!r}\n{result.stderr[-4000:]}")
    return result


def probe(root: ProjectRoot, path: Path, env: dict[str, str], *, frames: bool = False) -> dict:
    args = ["ffprobe", "-v", "error", "-show_streams", "-show_format"]
    if frames:
        args += ["-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time"]
    args += ["-of", "json", str(path)]
    return json.loads(run(root, args, env=env).stdout)


def acquire(root: ProjectRoot) -> dict:
    root.make_dir(f"{SPIKE}/receipts")
    root.make_dir(f"{SPIKE}/temp")
    root.make_dir(f"{SPIKE}/cache")
    root.make_dir(f"{SPIKE}/windows")
    if list(root.resolve(SPIKE).glob("source_video.*")) or root.resolve(f"{SPIKE}/aligned_23m17s_to_23m52s.mkv").exists():
        raise RuntimeError("Media outputs already exist; refusing to overwrite retained acquisition artifacts")
    if shutil.disk_usage(root.path).free < 8 * 1024**3:
        raise RuntimeError("At least 8 GiB free disk space is required before video acquisition")
    root.make_dir(f"{SPIKE}/env")
    yt_root = "references/_work/mandela333_audio/20261111T062929Z_c9ede868/tools/site"
    yt_path = root.resolve(yt_root)
    audio_path = root.resolve(AUDIO_REL)
    if not audio_path.is_file() or sha(audio_path) != AUDIO_SHA:
        raise RuntimeError("The contract-pinned audio excerpt is missing or its SHA256 differs")
    # Existing isolated yt-dlp code only; no installation and no inherited proxy/cookies.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(yt_path)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMP"] = str(root.resolve(f"{SPIKE}/temp"))
    env["TEMP"] = env["TMP"]
    env["TMPDIR"] = env["TMP"]
    env["XDG_CACHE_HOME"] = str(root.resolve(f"{SPIKE}/cache"))
    env.pop("HTTP_PROXY", None); env.pop("HTTPS_PROXY", None); env.pop("ALL_PROXY", None)
    env.pop("http_proxy", None); env.pop("https_proxy", None); env.pop("all_proxy", None)
    python = "D:/Work/video_pipeline/.venv/Scripts/python.exe"
    out = root.resolve(f"{SPIKE}/source_video.%(ext)s")
    cmd = [python, "-B", "-m", "yt_dlp", "--no-playlist", "--no-progress", "--proxy", "", "--cache-dir", str(root.resolve(f"{SPIKE}/cache")), "--paths", f"temp:{root.resolve(f'{SPIKE}/temp')}", "--js-runtimes", "node", "-f", "bv*[height<=480]/bv", "--write-info-json", "-o", str(out), VIDEO_URL]
    run(root, cmd, env=env, timeout=1800)
    candidates = sorted(root.resolve(SPIKE).glob("source_video.*"))
    videos = [p for p in candidates if p.suffix.lower() in {".mp4", ".webm", ".mkv"}]
    if len(videos) != 1:
        raise RuntimeError(f"Expected one downloaded video, got {[p.name for p in videos]}")
    source = videos[0]
    info_path = root.resolve(f"{SPIKE}/source_video.info.json")
    if not info_path.is_file():
        raise RuntimeError("yt-dlp did not create source video metadata")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if info.get("id") != "1jMFrVk2d_I" or int(info.get("height", 99999)) > 480:
        raise RuntimeError("Downloaded video identity or resolution violates the acquisition contract")
    src_probe = probe(root, source, env, frames=True)
    vstream = next(s for s in src_probe["streams"] if s["codec_type"] == "video")
    if not src_probe.get("frames"):
        raise RuntimeError("FFprobe returned no source decoded frames")
    src_frames = src_probe["frames"]
    t0 = float(src_frames[0]["best_effort_timestamp_time"])
    # The filter's trim operates on decoded source timestamps, not a keyframe-relative input seek.
    av_rel = f"{SPIKE}/aligned_23m17s_to_23m52s.mkv"
    av_path = root.resolve(av_rel)
    vf = f"trim=start={START}:duration={DURATION},setpts=PTS-STARTPTS,scale='min(640,iw)':-2:flags=lanczos"
    ff_cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(source), "-i", str(audio_path), "-filter:v", vf, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-fps_mode", "passthrough", "-c:a", "copy", "-t", "35", "-metadata", "comment=Reference analysis only; source timeline trim [1397,1432)", str(av_path)]
    run(root, ff_cmd, env=env, timeout=3600)
    aligned_probe = probe(root, av_path, env, frames=True)
    # Exact sample-index trims avoid keyframe seeks and PCM packet-boundary drift.
    window_dir_rel = f"{SPIKE}/windows_verified_20261111T"
    root.make_dir(window_dir_rel)
    windows = []
    for i in range(7):
        offset = i * 5
        first_sample, last_sample = offset * 48000, (offset + 5) * 48000
        rel = f"{window_dir_rel}/window_{i+1:02d}.mkv"
        path = root.resolve(rel)
        video_filter = f"trim=start={offset}:duration=5,setpts=PTS-STARTPTS,scale=min(640\\,iw):-2:flags=lanczos,format=yuv420p"
        audio_filter = f"atrim=start_sample={first_sample}:end_sample={last_sample},asetpts=PTS-STARTPTS"
        command = ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(av_path), "-i", str(audio_path), "-filter_complex", f"[0:v]{video_filter}[v];[1:a]{audio_filter}[a]", "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-fps_mode", "passthrough", "-c:a", "pcm_f32le", str(path)]
        run(root, command, env=env)
        p = probe(root, path, env, frames=True)
        windows.append({"path": rel, "absolute_start_seconds": START + offset, "absolute_end_seconds": START + offset + 5, "relative_start_seconds": offset, "duration_seconds": 5, "sample_interval": [first_sample, last_sample], "probe": p, "sha256": sha(path), "bytes": path.stat().st_size})
    receipt = {
        "status": "acquired",
        "created_utc": datetime.now(UTC).isoformat(),
        "source_url": VIDEO_URL, "video_id": "1jMFrVk2d_I", "title": info.get("title"),
        "yt_dlp_version": info.get("extractor_version", {}).get("current_git_head", "not exposed"),
        "yt_dlp_format_id": info.get("format_id"), "yt_dlp_format": info.get("format"),
        "source_video": {"path": f"{SPIKE}/{source.name}", "bytes": source.stat().st_size, "sha256": sha(source), "info_path": f"{SPIKE}/source_video.info.json", "info_sha256": sha(info_path), "height": info.get("height"), "width": info.get("width"), "duration_seconds": info.get("duration"), "probe": {"video_stream": vstream, "first_decoded_frame": src_frames[0], "last_decoded_frame": src_frames[-1], "decoded_frame_count": len(src_frames)}},
        "requested_source_interval": [START, END], "source_timeline_zero_seconds": t0,
        "alignment_method": "FFmpeg decoded-input trim=start=1397:duration=35; no input -ss/keyframe-relative seek; timestamps reset after trim. Audio is the verified exact 48kHz float32 WAV excerpt, copied as PCM into Matroska.",
        "aligned_media": {"path": av_rel, "bytes": av_path.stat().st_size, "sha256": sha(av_path), "probe": aligned_probe},
        "source_audio_excerpt": {"path": AUDIO_REL, "bytes": audio_path.stat().st_size, "sha256": sha(audio_path), "sample_rate": 48000, "channels": 2, "samples_per_channel": 1680000},
        "windows": windows,
        "commands": {"yt_dlp": cmd, "ffmpeg_alignment": ff_cmd},
        "limitations": ["Video is re-encoded H.264 at CRF 18 and scaled to at most 640px width; audio remains PCM copied from the verified excerpt.", "Video source timestamps are retained in source frame receipt; alignment is timestamp-filter based but decoded frame cadence determines boundary granularity.", "Source is the public YouTube upload; this does not recover pre-upload master timing/audio.", "Analysis reference only; not for production export or generation input."]
    }
    root.write_text(f"{SPIKE}/receipts/media_acquisition.json", json.dumps(receipt, indent=2, ensure_ascii=False))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["acquire", "verify"])
    args = parser.parse_args()
    root = ProjectRoot(REPO)
    if args.command == "acquire":
        receipt = acquire(root)
        print(json.dumps({"status": receipt["status"], "receipt": f"{SPIKE}/receipts/media_acquisition.json", "video_sha256": receipt["source_video"]["sha256"], "aligned_sha256": receipt["aligned_media"]["sha256"], "windows": len(receipt["windows"])}, indent=2))
    else:
        receipt_path = root.resolve(f"{SPIKE}/receipts/media_acquisition.json")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        audio = root.resolve(AUDIO_REL)
        if sha(audio) != AUDIO_SHA:
            raise RuntimeError("verified source audio checksum mismatch")
        files = [
            {"path": f"{SPIKE}/source_video.mp4", "sha256": sha(root.resolve(f"{SPIKE}/source_video.mp4")), "size": root.resolve(f"{SPIKE}/source_video.mp4").stat().st_size},
            {"path": f"{SPIKE}/aligned_23m17s_to_23m52s.mkv", "sha256": sha(root.resolve(f"{SPIKE}/aligned_23m17s_to_23m52s.mkv")), "size": root.resolve(f"{SPIKE}/aligned_23m17s_to_23m52s.mkv").stat().st_size},
        ]
        pairs = [{"media_path": files[1]["path"], "wav_path": AUDIO_REL, "start_seconds": 0, "duration_seconds": 35}]
        for i in range(7):
            rel = f"{SPIKE}/windows_verified_20261111T/window_{i+1:02d}.mkv"
            path = root.resolve(rel)
            pair = {"media_path": rel, "wav_path": AUDIO_REL, "start_seconds": i * 5, "duration_seconds": 5}
            pairs.append(pair)
            files.append({"path": rel, "sha256": sha(path), "size": path.stat().st_size})
        source = root.resolve(f"{SPIKE}/source_video.mp4")
        aligned = root.resolve(f"{SPIKE}/aligned_23m17s_to_23m52s.mkv")
        source_frames = json.loads(run(root, ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(source)], env=os.environ.copy()).stdout)["frames"]
        aligned_frames = json.loads(run(root, ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(aligned)], env=os.environ.copy()).stdout)["frames"]
        source_pts = [float(frame["best_effort_timestamp_time"]) for frame in source_frames]
        selected = [pts for pts in source_pts if START <= pts < END]
        aligned_pts = [float(frame["best_effort_timestamp_time"]) for frame in aligned_frames]
        if not selected or len(selected) != len(aligned_pts) or source_pts[0] != 0.0:
            raise RuntimeError("decoded source/aligned frame count or timeline-zero check failed")
        frame_deltas = [abs((source_t - START) - aligned_t) for source_t, aligned_t in zip(selected, aligned_pts, strict=True)]
        if max(frame_deltas) > 0.002:
            raise RuntimeError(f"source to aligned frame PTS mismatch: {max(frame_deltas)}")
        window_frame_alignment = []
        for i in range(7):
            offset = i * 5
            rel = f"{SPIKE}/windows_verified_20261111T/window_{i+1:02d}.mkv"
            win_frames = json.loads(run(root, ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(root.resolve(rel))], env=os.environ.copy()).stdout)["frames"]
            expected_pts = [pts - offset for pts in aligned_pts if offset <= pts < offset + 5]
            actual_pts = [float(frame["best_effort_timestamp_time"]) for frame in win_frames]
            errors = [abs(expected - actual) for expected, actual in zip(expected_pts, actual_pts, strict=True)] if len(expected_pts) == len(actual_pts) else [float("inf")]
            if len(expected_pts) != len(actual_pts) or not errors or max(errors) > 0.002:
                raise RuntimeError(f"window decoded frame timing differs from aligned source: {rel}")
            window_frame_alignment.append({"path": rel, "expected_frames": len(expected_pts), "decoded_frames": len(actual_pts), "first_pts": actual_pts[0], "last_pts": actual_pts[-1], "max_pts_error_seconds": max(errors)})
        pcm_results = []
        wav_pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-map", "0:a:0", "-f", "f32le", "-acodec", "pcm_f32le", "-"], capture_output=True, check=True).stdout
        for pair in pairs:
            media = root.resolve(pair["media_path"])
            decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(media), "-map", "0:a:0", "-f", "f32le", "-acodec", "pcm_f32le", "-"], capture_output=True, check=True).stdout
            first = pair["start_seconds"] * 48000 * 2 * 4
            last = (pair["start_seconds"] + pair["duration_seconds"]) * 48000 * 2 * 4
            expected = wav_pcm[first:last]
            result = {**pair, "expected_bytes": len(expected), "decoded_bytes": len(decoded), "equal": decoded == expected}
            if not result["equal"]:
                raise RuntimeError(f"decoded PCM differs from verified WAV slice for {pair['media_path']}")
            pcm_results.append(result)
        evidence = {"status": "passed", "pcm_pairs": pcm_results, "files": files, "video_frame_alignment": {"source_path": files[0]["path"], "aligned_path": files[1]["path"], "source_timeline_zero_seconds": source_pts[0], "requested_source_interval_seconds": [START, END], "source_frames_in_interval": len(selected), "aligned_decoded_frames": len(aligned_pts), "first_source_pts": selected[0], "last_source_pts": selected[-1], "first_aligned_pts": aligned_pts[0], "last_aligned_pts": aligned_pts[-1], "maximum_absolute_pts_error_seconds": max(frame_deltas), "windows": window_frame_alignment}}
        root.write_text(f"{SPIKE}/receipts/media_verification_corrected.json", json.dumps(evidence, indent=2))
        print(json.dumps({"status": "verified_pcm_and_video_timing", "receipt": f"{SPIKE}/receipts/media_verification_corrected.json", "pcm_pairs": len(pcm_results), "frames": len(selected), "max_pts_error_seconds": max(frame_deltas)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

