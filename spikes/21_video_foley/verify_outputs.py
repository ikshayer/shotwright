"""Independent CPU verifier for spike 21. No model imports or media writes.

Paths are relative to worktree root, not manifest directory. Required schema:
sources.{master,delivery}, media.{native_wav,edit_wav,audition}, generation,
provenance. Media records have path/sha256. WAV records additionally contain
sample_rate/channels/sample_count/start_sample/transforms and metrics with
integrated_lufs/true_peak_dbfs/clipped_samples. Provenance has sources: a list
of {repo,revision,files:[{path,sha256}]}. Local provenance files are hashed.
This verifies evidence, not authenticity of remote origins or human approval.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

MASTER = "9dbb789b260bfde899a549eaf18cfc20c4d8feed050f59edfeb2f25848f61389"
DELIVERY = "9860e9ab850b52fb19d03aa9b73aa5b4330f7bc998ec06318ebacae6ea9a8317"


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, check=True, timeout=360)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> dict:
    return json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-threads",
                "2",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        ).stdout
    )


def video_hash(path: Path) -> str:
    # Hash compressed packet payloads, independent of container metadata/timestamps.
    return (
        run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                "streamhash",
                "-hash",
                "sha256",
                "-",
            ]
        )
        .stdout.decode()
        .strip()
    )


def pcm_metrics(path: Path) -> dict:
    raw = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "f64le",
            "-acodec",
            "pcm_f64le",
            "-",
        ]
    ).stdout
    values = array.array("d")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    if not all(math.isfinite(v) for v in values):
        raise ValueError("nonfinite decoded audio")
    result = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            "loudnorm=I=-23:TP=-1:LRA=7:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    measured = json.loads(re.findall(r'\{[^{}]*"input_i"[^{}]*\}', result.stderr.decode())[-1])
    return {
        "decoded_values": len(values),
        "clipped_samples": sum(abs(v) >= 1 for v in values),
        "sample_peak": max(map(abs, values), default=0),
        "integrated_lufs": float(measured["input_i"]),
        "true_peak_dbfs": float(measured["input_tp"]),
    }


def verify(manifest: dict, root: ProjectRoot) -> dict:
    failures: list[str] = []
    quality_failures: list[str] = []
    evidence: dict = {}

    def check(ok: bool, message: str) -> None:
        if not ok:
            failures.append(message)

    def record(label: str, item: dict) -> Path:
        path = root.resolve(item["path"])
        check(
            "references" not in [p.lower() for p in path.relative_to(root.path).parts],
            label + ": reference input prohibited",
        )
        digest = sha(path)
        check(digest == item["sha256"], label + ": SHA256 mismatch")
        evidence[label] = {"path": item["path"], "sha256": digest}
        return path

    check(bool(manifest.get("schema_version")), "missing schema_version")
    check(manifest["shot_id"] == "s1" and manifest["shot_version"] == 2, "shot identity mismatch")

    def reject_reference_paths(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("path", "input_path", "video_path") and isinstance(child, str):
                    check(
                        "references" not in child.replace("\\", "/").lower().split("/"),
                        "reference media path in manifest",
                    )
                reject_reference_paths(child)
        elif isinstance(value, list):
            for child in value:
                reject_reference_paths(child)

    reject_reference_paths(manifest)
    sources = manifest["sources"]
    media = manifest["media"]
    if "proxy" in media:
        proxy = record("proxy", media["proxy"])
        pp = probe(proxy)
        check(abs(float(pp["format"]["duration"]) - 15) < 0.001, "proxy must be 15 seconds")
        ps = next(s for s in pp["streams"] if s["codec_type"] == "video")
        check(
            Fraction(ps["avg_frame_rate"]) > 0 and Fraction(ps["time_base"]) > 0,
            "proxy requires explicit rate/timebase",
        )
    master = record("master", sources["master"])
    delivery = record("delivery", sources["delivery"])
    check(evidence["master"]["sha256"] == MASTER, "master differs from accepted immutable source")
    check(
        evidence["delivery"]["sha256"] == DELIVERY,
        "delivery differs from accepted immutable source",
    )
    for label, path, codec, frames, rate in [
        ("master", master, "ffv1", 900, 60),
        ("delivery", delivery, "h264", 150, 10),
    ]:
        data = probe(path)
        stream = next(s for s in data["streams"] if s["codec_type"] == "video")
        check(stream["codec_name"] == codec, label + ": codec mismatch")
        check(int(stream["nb_read_frames"]) == frames, label + ": frame count mismatch")
        check(Fraction(stream["avg_frame_rate"]) == rate, label + ": frame rate mismatch")
        check(abs(float(data["format"]["duration"]) - 15) < 0.001, label + ": duration mismatch")
        if label == "delivery":
            check(
                (stream["width"], stream["height"], stream["sample_aspect_ratio"])
                == (352, 288, "12:11"),
                "delivery geometry mismatch",
            )
    for name in ("native_wav", "edit_wav"):
        item = media[name]
        path = record(name, item)
        data = probe(path)
        streams = data["streams"]
        audio = next(s for s in streams if s["codec_type"] == "audio")
        check(
            len(streams) == 1 and audio["codec_name"].startswith("pcm_"),
            name + ": expected PCM-only WAV",
        )
        rate = int(audio["sample_rate"])
        channels = int(audio["channels"])
        metrics = pcm_metrics(path)
        count = metrics["decoded_values"] // channels
        if name == "edit_wav":
            check(count == rate * 15, name + ": expected exactly 15 seconds of samples")
        evidence[name]["native_duration_seconds"] = count / rate
        for key, actual in [
            ("sample_rate", rate),
            ("channels", channels),
            ("sample_count", count),
            ("start_sample", 0),
        ]:
            check(item[key] == actual, name + ": " + key + " mismatch")
        if name == "edit_wav":
            check(rate == 48000, "edit_wav must be 48kHz")
        check(
            isinstance(item["transforms"], list),
            name + ": transforms must be explicit list (empty for raw)",
        )
        if name == "native_wav":
            check(item["transforms"] == [], "native WAV must remain untouched")
        for key in ("clipped_samples", "integrated_lufs", "true_peak_dbfs"):
            claimed = float(item["metrics"][key])
            actual = metrics[key]
            check(
                claimed == actual or (key != "clipped_samples" and math.isfinite(claimed) and math.isfinite(actual) and abs(claimed - actual) <= 0.15),
                name + ": recorded " + key + " differs from measurement",
            )
        if name == "edit_wav":
            if not math.isfinite(metrics["integrated_lufs"]) or abs(metrics["integrated_lufs"] + 18) > 0.5:
                quality_failures.append("edit_wav: integrated loudness must be -18 +/- 0.5 LUFS")
            if not metrics["true_peak_dbfs"] <= -0.9:
                quality_failures.append("edit_wav: true peak must be <= -0.9 dBFS")
            if metrics["clipped_samples"] != 0:
                quality_failures.append("edit_wav: clipping detected")
        evidence[name]["metrics"] = metrics
        evidence[name]["sample_count"] = count
    check(
        media["native_wav"]["path"] != media["edit_wav"]["path"],
        "native and processed files must be separate",
    )
    audition = record("audition", media["audition"])
    ap = probe(audition)
    check(
        sum(s["codec_type"] == "video" for s in ap["streams"]) == 1
        and sum(s["codec_type"] == "audio" for s in ap["streams"]) == 1,
        "audition needs one video and one audio stream",
    )
    check(
        abs(float(ap["format"]["duration"]) - 15) <= 0.1,
        "audition duration differs from 15 seconds",
    )
    evidence["video_packet_hash"] = {
        "delivery": video_hash(delivery),
        "audition": video_hash(audition),
    }
    check(
        evidence["video_packet_hash"]["delivery"] == evidence["video_packet_hash"]["audition"],
        "audition video packet payload changed",
    )
    generation = manifest["generation"]
    for key in (
        "prompt",
        "negative_prompt",
        "seed",
        "steps",
        "precision",
        "offload",
        "device",
        "versions",
        "wall_seconds",
    ):
        check(key in generation, "generation missing " + key)
    provenance = manifest["provenance"]["sources"]
    check(bool(provenance), "missing pinned source provenance")
    for index, source in enumerate(provenance):
        check(
            bool(source["repo"]) and bool(re.fullmatch(r"[0-9a-f]{40,64}", source["revision"])),
            "source revision must be pinned hexadecimal commit",
        )
        check(bool(source.get("licence")), "source licence absent")
        check(bool(source["files"]), "source hashed files absent")
        for entry in source["files"]:
            record("provenance_" + str(index) + "_" + entry["path"], entry)
    # Guard immutability across verification, too.
    check(sha(master) == MASTER and sha(delivery) == DELIVERY, "source changed during verification")
    return {
        "passed": not failures and not quality_failures,
        "overall_passed": not failures and not quality_failures,
        "technical_passed": not failures,
        "audio_quality_passed": not quality_failures,
        "failures": failures,
        "audio_quality_failures": quality_failures,
        "evidence": evidence,
        "limitations": [
            "No human audio approval or semantic timing judgement.",
            "Remote provenance authenticity, runtime resource samples, and execution safety require separate review.",
            "Declared transformation history cannot be reconstructed solely from final media.",
        ],
    }


def json_safe(value):
    """Represent nonfinite measurements explicitly without invalid JSON numbers."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("-inf" if value < 0 else "+inf")
    if isinstance(value, dict):
        return {key: json_safe(child) for key, child in value.items()}
    if isinstance(value, list):
        return [json_safe(child) for child in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="worktree-relative JSON path")
    parser.add_argument("--report", help="optional worktree-relative report path")
    args = parser.parse_args()
    root = ProjectRoot(ROOT)
    try:
        result = verify(json.loads(root.read_text(args.manifest)), root)
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        result = {"passed": False, "overall_passed": False, "technical_passed": False, "audio_quality_passed": False, "failures": [type(exc).__name__ + ": " + str(exc)], "audio_quality_failures": ["Not assessed: technical verification aborted"]}
    output = json.dumps(json_safe(result), indent=2, allow_nan=False)
    if args.report:
        root.write_text(args.report, output + "\n")
    print(output)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
