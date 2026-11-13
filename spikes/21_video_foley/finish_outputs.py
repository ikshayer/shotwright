"""Prepare explicit editor WAV and video-stream-copy audition from a retained take."""

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

R = ProjectRoot(ROOT)
B = "spikes/21_video_foley/_work"


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for c in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(c)
    return h.hexdigest()


def command(args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=180)


def loudness(path):
    cp = command(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-18:TP=-1:LRA=7:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    start = cp.stderr.rfind("{")
    return json.JSONDecoder().raw_decode(cp.stderr[start:])[0]


def audio_entry(rel, transforms):
    import numpy as np
    import soundfile as sf

    data, rate = sf.read(str(R.resolve(rel)), dtype="float64", always_2d=True)
    levels = loudness(R.resolve(rel))
    return {
        "path": rel,
        "sha256": sha(R.resolve(rel)),
        "sample_rate": rate,
        "channels": data.shape[1],
        "sample_count": data.shape[0],
        "start_sample": 0,
        "transforms": transforms,
        "metrics": {
            "integrated_lufs": float(levels["input_i"])
            if math.isfinite(float(levels["input_i"]))
            else levels["input_i"],
            "true_peak_dbfs": float(levels["input_tp"]),
            "clipped_samples": int(np.count_nonzero(np.abs(data) >= 1)),
        },
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("generation")
    p.add_argument("--out", required=True)
    p.add_argument("--diagnostic-unprocessed", action="store_true")
    p.add_argument("--source-module-snapshot")
    a = p.parse_args()
    R.make_dir(a.out)
    g = json.loads(R.read_text(a.generation))
    native = g["native_wav"]
    assert sha(R.resolve(native["path"])) == native["sha256"]
    master = "spikes/20_overnight_production/_work/20261108T001855_03bdf358/projects/scripted_03bdf358/renders/s1/v0002/3859f220afd54b6f9bcee4a0743336c9/attempt-0001/master.mkv"
    delivery = "spikes/20_overnight_production/_work/20261108T001855_03bdf358/projects/scripted_03bdf358/post/s1/v0002/4cacb702506344519e0728ef067f41ff/degraded.mp4"
    assert (
        sha(R.resolve(master)) == "9dbb789b260bfde899a549eaf18cfc20c4d8feed050f59edfeb2f25848f61389"
    )
    assert (
        sha(R.resolve(delivery))
        == "9860e9ab850b52fb19d03aa9b73aa5b4330f7bc998ec06318ebacae6ea9a8317"
    )
    measured = loudness(R.resolve(native["path"]))
    R.write_text(a.out + "/native_loudness.json", json.dumps(measured, indent=2) + "\n")
    if not a.diagnostic_unprocessed and (
        not math.isfinite(float(measured["input_i"]))
        or not math.isfinite(float(measured["target_offset"]))
    ):
        raise ValueError(
            "Native take is below gated loudness measurement; retained unchanged. Refusing infinite normalization gain."
        )
    norm = f"loudnorm=I=-18:TP=-1:LRA=7:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}:linear=true:print_format=json"
    filters = (
        norm + ",aresample=48000,apad=whole_len=720000,atrim=end_sample=720000,asetpts=PTS-STARTPTS"
    )
    if a.diagnostic_unprocessed:
        filters = (
            "aresample=48000,apad=whole_len=720000,atrim=end_sample=720000,asetpts=PTS-STARTPTS"
        )
    edit = a.out + "/edit_48k.wav"

    def encode(path):
        cp = command(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-nostdin",
                "-n",
                "-i",
                str(R.resolve(native["path"])),
                "-af",
                filters,
                "-ar",
                "48000",
                "-c:a",
                "pcm_s24le",
                str(path),
            ]
        )
        R.write_text(a.out + "/edit_encode.log", cp.stderr)

    R.write_with(edit, encode)
    audition = a.out + "/audition.mp4"
    R.write_with(
        audition,
        lambda path: command(
            [
                "ffmpeg",
                "-hide_banner",
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-i",
                str(R.resolve(delivery)),
                "-i",
                str(R.resolve(edit)),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(path),
            ]
        ),
    )
    transforms = [
        {
            "kind": "loudness",
            "target_integrated_lufs": -18,
            "target_true_peak_dbfs": -1,
            "target_lra": 7,
            "method": "none: explicit unprocessed diagnostic"
            if a.diagnostic_unprocessed
            else "FFmpeg two-pass loudnorm",
            "applied": not a.diagnostic_unprocessed,
            "measurement": measured,
        },
        {"kind": "resample", "sample_rate": 48000},
        {
            "kind": "duration_align",
            "target_sample_count": 720000,
            "original_sample_count": native["sample_count"],
            "original_sample_rate": native["sample_rate"],
            "padding_or_trimming_needed": native["sample_count"] / native["sample_rate"] != 15,
            "filter": filters,
        },
        {"kind": "PCM_encode", "subtype": "signed24bit"},
    ]
    files = json.loads(R.read_text(B + "/verified-models.json"))
    grouped = {}
    for f in files:
        grouped.setdefault((f["repo"], f["revision"]), []).append(
            {
                "path": f["path"],
                "sha256": f["sha256"],
                "upstream_name": f["name"],
                "sha256_expected": f["sha256_expected"],
                "git_blob_sha1": f["git_blob_sha1"],
            }
        )
    sources = [
        {
            "repo": repo,
            "revision": rev,
            "licence": "Tencent Hunyuan Community (territorial restrictions including outputs)"
            if repo.startswith("tencent/")
            else "Apache-2.0 declared by pinned model card; separate licence text not supplied",
            "files": items,
        }
        for (repo, rev), items in grouped.items()
    ]
    sourcefiles = [
        "LICENSE",
        "NOTICE",
        "configs/hunyuanvideo-foley-xl.yaml",
        "hunyuanvideo_foley/utils/model_utils.py",
    ]
    sources.append(
        {
            "repo": "https://github.com/Tencent-Hunyuan/HunyuanVideo-Foley",
            "revision": "df7b005b5023df2a9b73e1d66dd51d452799884e",
            "licence": "Tencent Hunyuan Community",
            "locally_modified": ["hunyuanvideo_foley/utils/model_utils.py"],
            "files": [
                {"path": B + "/source/" + f, "sha256": sha(R.resolve(B + "/source/" + f))}
                for f in sourcefiles
            ],
        }
    )
    harness_files = [
        {
            "path": "spikes/21_video_foley/" + name,
            "sha256": sha(R.resolve("spikes/21_video_foley/" + name)),
        }
        for name in ["setup_runtime.py", "run_foley.py", "launch_runtime.py", "finish_outputs.py"]
    ]
    if g.get("execution_harness"):
        harness_files.append(g["execution_harness"])
    if a.source_module_snapshot:
        harness_files.append(
            {"path": a.source_module_snapshot, "sha256": sha(R.resolve(a.source_module_snapshot))}
        )
    sources.append(
        {
            "repo": "local:spike21",
            "revision": hashlib.sha256(
                json.dumps(harness_files, sort_keys=True).encode()
            ).hexdigest(),
            "licence": "Project-authored harness; upstream components retain separate terms",
            "files": harness_files,
        }
    )
    manifest = {
        "schema_version": 1,
        "interchange_status": "spike proposal, not production schema",
        "path_base": {
            "kind": "worktree-root-relative",
            "note": "All path fields resolve against pipeline worktree root. Use editor_bundle/project.json for portable manifest-relative media.",
        },
        "project_id": "scripted_03bdf358",
        "shot_id": "s1",
        "shot_version": 2,
        "audio_approval": "not approved; diagnostic quality targets not met"
        if a.diagnostic_unprocessed
        else "pending creator audition",
        "diagnostic_unprocessed": a.diagnostic_unprocessed,
        "quality_notes": "Near-silent native take retained, no gain or normalization applied"
        if a.diagnostic_unprocessed
        else "Quality targets require independent verification",
        "sources": {
            "master": {"path": master, "sha256": sha(R.resolve(master)), "frame_rate": "60/1"},
            "delivery": {
                "path": delivery,
                "sha256": sha(R.resolve(delivery)),
                "frame_rate": "10/1",
            },
        },
        "media": {
            "proxy": g["proxy"],
            "native_wav": audio_entry(native["path"], []),
            "edit_wav": audio_entry(edit, transforms),
            "audition": {
                "path": audition,
                "sha256": sha(R.resolve(audition)),
                "video_transform": "stream copy; AAC audio encode only",
            },
        },
        "timeline": {
            "start_sample": 0,
            "sample_rate": 48000,
            "duration_samples": 720000,
            "timebase": "1/48000",
            "source_start_sec": 0,
            "delivery_start_frame": 0,
        },
        "generation": {k: v for k, v in g.items() if k not in ["native_wav", "samples"]},
        "provenance": {
            "sources": sources,
            "runtime_metrics_path": a.generation,
            "licence_blockers": [
                "Hunyuan restrictions include output outside Territory (EU/UK/South Korea excluded)",
                "Original figure and copied font internal-evaluation limits remain",
                "No worldwide publication clearance",
            ],
        },
    }
    bundle = a.out + "/editor_bundle"
    copy_map = {"video": "source_video.mp4", "native_wav": "native.wav", "edit_wav": "edit_48k.wav"}
    bundle_sources = {"video": delivery, "native_wav": native["path"], "edit_wav": edit}
    bundle_media = {}
    for key, name in copy_map.items():
        with R.resolve(bundle_sources[key]).open("rb") as stream:
            R.write_stream(bundle + "/" + name, stream)
        bundle_media[key] = {"path": name, "sha256": sha(R.resolve(bundle + "/" + name))}
    portable = {
        "schema_version": 1,
        "path_base": "manifest-directory",
        "shot_id": "s1",
        "shot_version": 2,
        "media": bundle_media,
        "timeline": manifest["timeline"],
        "generation": {
            k: g[k] for k in ["prompt", "negative_prompt", "seed", "steps", "wall_seconds"]
        },
        "model_sources": [
            {"repo": x["repo"], "revision": x["revision"], "licence": x["licence"]} for x in sources
        ],
        "licence_blockers": manifest["provenance"]["licence_blockers"],
        "audio_approval": "pending creator audition",
        "usage": "Internal editing evaluation; no model runtime or weight files required to open media",
    }
    portable["diagnostic_unprocessed"] = a.diagnostic_unprocessed
    portable["audio_approval"] = manifest["audio_approval"]
    portable["quality_notes"] = manifest["quality_notes"]
    R.write_text(bundle + "/project.json", json.dumps(portable, indent=2) + "\n")
    manifest["editor_bundle_manifest"] = bundle + "/project.json"
    R.write_text(a.out + "/editor_manifest.json", json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "manifest": a.out + "/editor_manifest.json",
                "audition": audition,
                "native": manifest["media"]["native_wav"],
                "edit": manifest["media"]["edit_wav"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
