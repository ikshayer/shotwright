"""Prepare an 8-second control take for editing and audition without altering its raw WAV."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

R = ProjectRoot(ROOT)
BASE = "spikes/21_video_foley/_work"
TARGET_RATE = 48_000
TARGET_SAMPLES = 8 * TARGET_RATE


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffmpeg_json(path: Path) -> dict[str, Any]:
    env = os.environ.copy()
    for key, rel in {
        "TMP": BASE + "/tmp",
        "TEMP": BASE + "/tmp",
        "TMPDIR": BASE + "/tmp",
        "XDG_CACHE_HOME": BASE + "/cache/xdg",
    }.items():
        env[key] = str(R.make_dir(rel))
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path),
            "-af", "loudnorm=I=-18:TP=-1:LRA=7:print_format=json", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg loudness measurement failed ({result.returncode}): {result.stderr[-3000:]}")
    start = result.stderr.rfind("{")
    if start < 0:
        raise RuntimeError("FFmpeg loudness measurement returned no JSON")
    values, _ = json.JSONDecoder().raw_decode(result.stderr[start:])
    return values


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def measurement_value(value: Any) -> float | str:
    number = finite_number(value)
    return number if number is not None else str(value)


def run_checked(args: list[str], *, env: dict[str, str], what: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=300, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{what} failed ({result.returncode}): {result.stderr[-4000:]}")
    return result


def audio_metrics(rel: str, transforms: list[dict[str, Any]], loudness: dict[str, Any]) -> dict[str, Any]:
    import numpy as np
    import soundfile as sf

    path = R.resolve(rel)
    samples, rate = sf.read(str(path), dtype="float64", always_2d=True)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
    dc = np.mean(samples, axis=0) if samples.size else np.zeros(samples.shape[1])
    rms_dbfs = 20 * math.log10(rms) if rms > 0 else "-inf"
    return {
        "path": rel,
        "sha256": sha256(path),
        "sample_rate": int(rate),
        "channels": int(samples.shape[1]),
        "sample_count": int(samples.shape[0]),
        "start_sample": 0,
        "transforms": transforms,
        "metrics": {
            "integrated_lufs": measurement_value(loudness.get("input_i", "unknown")),
            "true_peak_dbfs": measurement_value(loudness.get("input_tp", "unknown")),
            "sample_peak_dbfs": 20 * math.log10(peak) if peak > 0 else "-inf",
            "rms_dbfs": rms_dbfs,
            "dc_offset_per_channel": [float(v) for v in dc],
            "clipped_samples": int(np.count_nonzero(np.abs(samples) >= 1.0)),
        },
    }


def probe_video(path: Path, env: dict[str, str]) -> dict[str, Any]:
    result = run_checked(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,time_base,duration,nb_frames,nb_read_frames:format=duration",
            "-of", "json", str(path),
        ],
        env=env,
        what="FFprobe video inspection",
    )
    payload = json.loads(result.stdout)
    if not payload.get("streams"):
        raise ValueError("Input video has no video stream")
    stream = payload["streams"][0]
    stream["container_duration"] = payload.get("format", {}).get("duration")
    return stream


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, help="Contained worktree-relative original control video")
    parser.add_argument("--generation", required=True, help="Contained generation.json path")
    parser.add_argument("--out", required=True, help="Fresh contained output directory")
    parser.add_argument(
        "--normalize", action="store_true",
        help="Explicitly apply FFmpeg two-pass loudnorm when every required measurement is finite",
    )
    args = parser.parse_args()

    output_path = R.resolve(args.out)
    if output_path.exists():
        raise FileExistsError(f"Output directory must be fresh; refusing reuse: {args.out}")
    video_path = R.resolve(args.video)
    generation_path = R.resolve(args.generation)
    generation = json.loads(R.read_text(args.generation))
    native = generation["native_wav"]
    native_rel = native["path"]
    native_path = R.resolve(native_rel)
    if sha256(native_path) != native["sha256"]:
        raise ValueError("Native WAV SHA256 does not match generation metadata")
    if int(native["sample_rate"]) <= 0 or int(native["channels"]) <= 0:
        raise ValueError("Native WAV has invalid declared sample rate or channel count")
    import soundfile as sf

    native_info = sf.info(str(native_path))
    actual_native = (native_info.samplerate, native_info.channels, native_info.frames)
    declared_native = (int(native["sample_rate"]), int(native["channels"]), int(native["sample_count"]))
    if actual_native != declared_native:
        raise ValueError(f"Native WAV metadata mismatch: file={actual_native}, generation={declared_native}")

    env = os.environ.copy()
    for key, rel in {
        "TMP": BASE + "/tmp",
        "TEMP": BASE + "/tmp",
        "TMPDIR": BASE + "/tmp",
        "XDG_CACHE_HOME": BASE + "/cache/xdg",
    }.items():
        env[key] = str(R.make_dir(rel))

    video_info = probe_video(video_path, env)
    frame_rate = Fraction(video_info.get("avg_frame_rate", "0/1"))
    video_frames = int(video_info.get("nb_read_frames") or video_info.get("nb_frames") or 0)
    video_duration = finite_number(video_info.get("duration"))
    if video_duration is None:
        video_duration = finite_number(video_info.get("container_duration"))
    if frame_rate != Fraction(25, 1) or video_frames != 200 or video_duration is None or abs(video_duration - 8.0) > 0.001:
        raise ValueError(
            "Control video must decode as exactly 8 seconds, 25/1 fps, and 200 frames; "
            f"observed duration={video_duration}, fps={frame_rate}, frames={video_frames}"
        )

    R.make_dir(args.out)

    # Record unmodified source levels before deciding whether an explicitly requested
    # normalization is safe to apply. No gain is applied by default.
    source_loudness = ffmpeg_json(native_path)
    needed = ["input_i", "input_tp", "input_lra", "input_thresh", "target_offset"]
    normalization_values = {key: finite_number(source_loudness.get(key)) for key in needed}
    normalization_applied = bool(args.normalize and all(v is not None for v in normalization_values.values()))
    diagnostic_unprocessed = not normalization_applied
    if args.normalize and not normalization_applied:
        diagnostic_reason = "Normalization requested but one or more required loudness measurements were nonfinite; retained unprocessed."
    elif not args.normalize:
        diagnostic_reason = "No normalization requested; native take retained and no gain applied."
    else:
        diagnostic_reason = None

    native_loudness_rel = args.out + "/native_loudness.json"
    R.write_text(native_loudness_rel, json.dumps(source_loudness, indent=2, allow_nan=False) + "\n")

    filters: list[str] = []
    transform_list: list[dict[str, Any]] = []
    if normalization_applied:
        values = normalization_values
        norm = (
            "loudnorm=I=-18:TP=-1:LRA=7:"
            f"measured_I={values['input_i']}:measured_TP={values['input_tp']}:"
            f"measured_LRA={values['input_lra']}:measured_thresh={values['input_thresh']}:"
            f"offset={values['target_offset']}:linear=true:print_format=json"
        )
        filters.append(norm)
        transform_list.append(
            {
                "kind": "loudness_normalization",
                "applied": True,
                "target_integrated_lufs": -18,
                "target_true_peak_dbfs": -1,
                "target_lra": 7,
                "method": "FFmpeg two-pass loudnorm",
                "measurement_path": native_loudness_rel,
            }
        )
    else:
        transform_list.append(
            {"kind": "loudness_normalization", "applied": False, "reason": diagnostic_reason}
        )
    filters.extend(
        [
            f"aresample={TARGET_RATE}",
            f"apad=whole_len={TARGET_SAMPLES}",
            f"atrim=end_sample={TARGET_SAMPLES}",
            "asetpts=PTS-STARTPTS",
        ]
    )
    transform_list.extend(
        [
            {
                "kind": "resample",
                "source_sample_rate": int(native["sample_rate"]),
                "target_sample_rate": TARGET_RATE,
                "method": "FFmpeg aresample",
            },
            {
                "kind": "duration_align",
                "target_duration_seconds": 8,
                "target_sample_count": TARGET_SAMPLES,
                "source_sample_count": int(native["sample_count"]),
                "source_sample_rate": int(native["sample_rate"]),
                "filter": f"apad=whole_len={TARGET_SAMPLES},atrim=end_sample={TARGET_SAMPLES}",
                "padding_or_trimming": (
                    "pad_end" if int(native["sample_count"]) / int(native["sample_rate"]) < 8
                    else "trim_end" if int(native["sample_count"]) / int(native["sample_rate"]) > 8
                    else "none"
                ),
            },
            {"kind": "PCM_encode", "subtype": "signed 24-bit PCM"},
        ]
    )

    edit_rel = args.out + "/edit_48k.wav"
    encode_log_rel = args.out + "/edit_encode.log"
    encode_cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-n", "-i", str(native_path),
        "-af", ",".join(filters), "-ar", str(TARGET_RATE), "-c:a", "pcm_s24le",
    ]

    def encode(target: Path) -> None:
        result = run_checked(encode_cmd + [str(target)], env=env, what="FFmpeg edit WAV encoding")
        R.write_text(encode_log_rel, result.stderr)

    R.write_with(edit_rel, encode)
    edit_loudness = ffmpeg_json(R.resolve(edit_rel))

    audition_rel = args.out + "/audition.mp4"
    audition_log_rel = args.out + "/audition_encode.log"
    audition_cmd = [
        "ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-n", "-i", str(video_path),
        "-i", str(R.resolve(edit_rel)), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-t", "8", "-movflags", "+faststart",
    ]

    def mux(target: Path) -> None:
        result = run_checked(audition_cmd + [str(target)], env=env, what="FFmpeg stream-copy audition")
        R.write_text(audition_log_rel, result.stderr)

    R.write_with(audition_rel, mux)
    native_entry = audio_metrics(native_rel, [], source_loudness)
    edit_entry = audio_metrics(edit_rel, transform_list, edit_loudness)
    video_entry = {
        "path": args.video,
        "sha256": sha256(video_path),
        "codec": video_info.get("codec_name"),
        "width": video_info.get("width"),
        "height": video_info.get("height"),
        "frame_rate": video_info.get("avg_frame_rate"),
        "time_base": video_info.get("time_base"),
        "duration_seconds": video_info.get("duration"),
        "frame_count": video_info.get("nb_frames"),
    }
    if edit_entry["sample_rate"] != TARGET_RATE or edit_entry["sample_count"] != TARGET_SAMPLES:
        raise RuntimeError(
            f"Encoded edit WAV mismatch: got {edit_entry['sample_rate']}Hz/{edit_entry['sample_count']} frames; "
            f"expected {TARGET_RATE}Hz/{TARGET_SAMPLES} frames"
        )

    # Self-contained editor bundle uses only relative links from its project.json.
    bundle_rel = args.out + "/editor_bundle"
    R.make_dir(bundle_rel)
    bundle_files = {}
    for key, source_rel, leaf in [
        ("video", args.video, "control_video.mp4"),
        ("native_wav", native_rel, "native.wav"),
        ("edit_wav", edit_rel, "edit_48k.wav"),
    ]:
        destination_rel = bundle_rel + "/" + leaf
        with R.resolve(source_rel).open("rb") as source_stream:
            R.write_stream(destination_rel, source_stream)
        bundle_files[key] = {"path": leaf, "sha256": sha256(R.resolve(destination_rel))}
    audition_bundle_rel = bundle_rel + "/audition.mp4"
    with R.resolve(audition_rel).open("rb") as source_stream:
        R.write_stream(audition_bundle_rel, source_stream)
    bundle_files["audition"] = {"path": "audition.mp4", "sha256": sha256(R.resolve(audition_bundle_rel))}

    native_entry["portable_path"] = bundle_files["native_wav"]["path"]
    edit_entry["portable_path"] = bundle_files["edit_wav"]["path"]
    manifest = {
        "schema_version": 1,
        "interchange_status": "positive-control diagnostic; not production schema",
        "path_base": {"kind": "worktree-root-relative"},
        "control": {"kind": "synthetic_basketball_bounce", "duration_seconds": 8},
        "sources": {
            "video": video_entry,
            "generation_record": {"path": args.generation, "sha256": sha256(generation_path)},
            "model_assets_manifest": {
                "path": BASE + "/verified-models.json",
                "sha256": sha256(R.resolve(BASE + "/verified-models.json")),
            },
            "proxy": generation.get("proxy"),
        },
        "provenance": {
            "model_sources": [],
            "runtime_files": [],
            "visual_rights": "Original basketball, floor, animation, lighting, camera, and rendered frames were generated procedurally for this diagnostic; no external visual reference or asset was used.",
            "licence_restrictions": [
                "Tencent Hunyuan Community License excludes the EU, UK, and South Korea from its Territory.",
                "Use, reproduction, display, and distribution of Hunyuan outputs outside the Territory are unlicensed.",
                "No worldwide publication clearance is claimed.",
            ],
        },
        "media": {
            "native_wav": native_entry,
            "edit_wav": edit_entry,
            "audition": {
                "path": audition_rel,
                "sha256": sha256(R.resolve(audition_rel)),
                "video_source_path": args.video,
                "video_transform": "video stream copy; AAC audio encode only",
            },
        },
        "timeline": {
            "start_sample": 0,
            "sample_rate": TARGET_RATE,
            "duration_samples": TARGET_SAMPLES,
            "timebase": "1/48000",
            "duration_seconds": 8,
        },
        "generation": generation,
        "diagnostic_unprocessed": diagnostic_unprocessed,
        "normalization_applied": normalization_applied,
        "normalization_decision": diagnostic_reason,
        "native_loudness_measurement": {
            "path": native_loudness_rel,
            "values": {k: measurement_value(v) for k, v in source_loudness.items()},
        },
        "editor_bundle_manifest": bundle_rel + "/project.json",
        "audio_approval": "not assessed; requires creator audition",
    }
    model_manifest_rel = BASE + "/verified-models.json"
    model_records = json.loads(R.read_text(model_manifest_rel))
    grouped_models: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in model_records:
        identity = (item["repo"], item["revision"])
        grouped_models.setdefault(identity, []).append(
            {
                "name": item["name"],
                "sha256": item["sha256"],
                "sha256_expected": item.get("sha256_expected"),
                "size_bytes": item.get("size"),
            }
        )
    model_sources = [
        {
            "repo": repo,
            "revision": revision,
            "license": (
                "Tencent Hunyuan Community License; Territory excludes EU, UK, and South Korea"
                if repo == "tencent/HunyuanVideo-Foley"
                else "See upstream repository/model card; separate terms require review"
            ),
            "files": files,
        }
        for (repo, revision), files in grouped_models.items()
    ]
    runtime_files = []
    for runtime_rel in [
        "spikes/21_video_foley/setup_runtime.py",
        "spikes/21_video_foley/run_foley.py",
        "spikes/21_video_foley/launch_runtime.py",
        "spikes/21_video_foley/prepare_control_audio.py",
    ]:
        runtime_files.append({"path": runtime_rel, "sha256": sha256(R.resolve(runtime_rel))})
    if generation.get("execution_harness"):
        identity = generation["execution_harness"]
        if sha256(R.resolve(identity["path"])) != identity["sha256"]:
            raise ValueError("Generation execution harness snapshot hash mismatch")
        runtime_files.append(identity)
    manifest["provenance"]["model_sources"] = model_sources
    manifest["provenance"]["runtime_files"] = runtime_files

    portable = {
        "schema_version": 1,
        "path_base": "manifest-directory",
        "control": manifest["control"],
        "media": bundle_files,
        "timeline": manifest["timeline"],
        "provenance": {
            "model_manifest": {
                "sha256": sha256(R.resolve(model_manifest_rel)),
                "sources": model_sources,
            },
            "runtime_files": runtime_files,
            "visual_rights": manifest["provenance"]["visual_rights"],
            "licence_restrictions": manifest["provenance"]["licence_restrictions"],
        },
        "generation": {
            key: generation.get(key)
            for key in ["prompt", "negative_prompt", "seed", "steps", "precision", "offload", "device", "versions", "wall_seconds"]
        },
        "diagnostic_unprocessed": diagnostic_unprocessed,
        "normalization_applied": normalization_applied,
        "normalization_decision": diagnostic_reason,
        "audio_approval": manifest["audio_approval"],
        "usage": "Editor-neutral media interchange; WAV and MP4 open without model runtime or weights.",
    }
    R.write_text(bundle_rel + "/project.json", json.dumps(portable, indent=2, allow_nan=False) + "\n")
    R.write_text(args.out + "/editor_manifest.json", json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"manifest": args.out + "/editor_manifest.json", "edit": edit_rel, "audition": audition_rel}, indent=2))


if __name__ == "__main__":
    main()