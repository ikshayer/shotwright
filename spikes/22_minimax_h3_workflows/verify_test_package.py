"""Independent, CPU-only verification of the H3 offline input package."""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT_PATH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_PATH))
from orchestrator.platform.containment import ProjectRoot

SOURCE = "spikes/20_overnight_production/_work/20261108T001855_03bdf358/projects/scripted_03bdf358/renders/s1/v0002/3859f220afd54b6f9bcee4a0743336c9/attempt-0001/master.mkv"
SOURCE_SHA = "9dbb789b260bfde899a549eaf18cfc20c4d8feed050f59edfeb2f25848f61389"
PROMPT = "The target video is an edited version of <Video 1>. Preserve the fixed security-camera viewpoint, room layout, furnishings, lighting, and lack of camera motion. Keep the room empty. Generate a continuous, natural indoor ambience soundtrack: gentle ventilation airflow, a low steady fan hum, and faint distant building noise. No speech, voices, music, footsteps, dramatic impacts, or jump-scare sounds. Keep the sound present and restrained, with no abrupt start or cut."


def run(command: list[str]) -> str:
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=180, cwd=ROOT_PATH)
    if result.returncode:
        raise ValueError(f"command failed ({result.returncode}): {result.stderr[-4000:]}")
    return result.stdout


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def png(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG signature"
    offset = 8
    dimensions = None
    image_data = bytearray()
    ended = False
    while offset < len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        assert len(payload) == size, "truncated PNG"
        crc = struct.unpack(">I", data[offset + 8 + size:offset + 12 + size])[0]
        assert zlib.crc32(kind + payload) & 0xffffffff == crc, "PNG CRC"
        if kind == b"IHDR":
            dimensions = struct.unpack(">II", payload[:8])
        if kind == b"IDAT":
            image_data.extend(payload)
        offset += size + 12
        if kind == b"IEND":
            ended = True
            break
    assert ended and dimensions and min(dimensions) > 0 and offset == len(data)
    assert zlib.decompress(image_data), "empty PNG raster"
    return dimensions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Worktree-root-relative manifest")
    parser.add_argument("--report", required=True, help="Worktree-root-relative JSON report")
    args = parser.parse_args()
    root = ProjectRoot(ROOT_PATH)
    root.resolve(args.report)
    checks: list[dict] = []

    def check(name: str, operation) -> None:
        try:
            detail = operation()
            checks.append({"name": name, "pass": True, "detail": detail})
        except (ValueError, KeyError, TypeError, OSError, AssertionError, subprocess.SubprocessError, struct.error, zlib.error) as exc:
            checks.append({"name": name, "pass": False, "error": f"{type(exc).__name__}: {exc}"})

    def require(value: bool, message: str) -> None:
        if not value:
            raise ValueError(message)

    try:
        manifest = json.loads(root.read_text(args.manifest))
    except (ValueError, KeyError, TypeError, OSError, AssertionError, subprocess.SubprocessError, struct.error, zlib.error) as exc:
        manifest = {}
        checks.append({"name": "manifest_read", "pass": False, "error": str(exc)})

    def identity() -> None:
        for key, value in {"schema_version": 1, "status": "prepared_not_submitted", "media_path_base": "worktree_root", "shot_id": "s1", "shot_version": 2}.items():
            require(manifest.get(key) == value, f"{key} must equal {value!r}")
        require(manifest.get("no_input_audio", manifest.get("provenance", {}).get("no_input_audio")) is True, "no_input_audio must be true")
        require(manifest.get("provenance", {}).get("generation_submitted") is False, "generation_submitted must be false")
    check("manifest_identity", identity)
    for key in ("source", "input_video", "prompt", "request_template", "contact_sheet"):
        def artifact(key=key):
            entry = manifest[key]
            path = root.resolve(entry["path"])
            require(sha(path) == entry["sha256"], f"{key} SHA mismatch")
            return {"path": entry["path"], "sha256": entry["sha256"]}
        check(f"{key}_actual_hash_and_containment", artifact)

    def source() -> None:
        entry = manifest["source"]
        require(entry["path"] == SOURCE, "unexpected source identity")
        require(sha(root.resolve(SOURCE)) == SOURCE_SHA == entry["sha256"], "original source hash changed")
        require(entry["start_seconds"] == 0 and entry["end_seconds"] == 8, "source interval mismatch")
        require("pending" in json.dumps(entry).lower(), "source rights review must remain pending")
    check("source_identity_interval_rights", source)

    def source_metadata() -> dict:
        probe = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(root.resolve(SOURCE))]))
        stream = next(item for item in probe["streams"] if item["codec_type"] == "video")
        for key, value in {"codec_name": "ffv1", "width": 1280, "height": 960, "r_frame_rate": "60/1"}.items():
            require(stream.get(key) == value, f"source {key} mismatch")
        require(float(probe["format"]["duration"]) == 15, "source duration mismatch")
        return {"actual_header": stream, "frame_count": 900, "frame_count_basis": "frozen contract receipt; not independently recounted per Amendment 1"}
    check("actual_source_metadata", source_metadata)

    def video() -> dict:
        entry = manifest["input_video"]
        path = str(root.resolve(entry["path"]))
        probe = json.loads(run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", path]))
        streams = probe["streams"]
        require(len(streams) == 1 and streams[0]["codec_type"] == "video", "expected one video stream and no audio/other streams")
        stream = streams[0]
        for key, value in {"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1024, "height": 768, "sample_aspect_ratio": "1:1", "r_frame_rate": "24/1", "avg_frame_rate": "24/1", "nb_read_frames": "192"}.items():
            require(stream.get(key) == value, f"actual {key}: {stream.get(key)!r} != {value!r}")
        require(float(stream["duration"]) == 8 and float(probe["format"]["duration"]) == 8, "actual duration not exactly 8s")
        for key, value in {"width": 1024, "height": 768, "fps": "24/1", "frame_count": 192, "duration_seconds": 8, "audio_stream_count": 0}.items():
            require(entry.get(key) == value, f"manifest input {key} mismatch")
        run(["ffmpeg", "-v", "error", "-xerror", "-i", path, "-map", "0:v:0", "-f", "null", "-"])
        return stream
    check("actual_video_and_full_decode", video)
    check("contact_sheet_png", lambda: {"dimensions": png(root.read_bytes(manifest["contact_sheet"]["path"]))})
    check("contact_sheet_indices", lambda: require(manifest["contact_sheet"]["frame_indices"] == [0, 48, 96, 144, 191], "sample indices mismatch"))
    check("exact_prompt", lambda: require(root.read_text(manifest["prompt"]["path"]).rstrip("\n") == PROMPT, "prompt mismatch"))

    def request() -> None:
        actual = json.loads(root.read_text(manifest["request_template"]["path"]))
        expected = {"model": "MiniMax-H3", "resolution": "768P", "duration": 8, "ratio": "4:3", "content": [{"type": "text", "text": PROMPT}, {"type": "video_url", "role": "reference_video", "video_url": {"url": "REPLACE_WITH_PROVIDER_VIDEO_URL_OR_DATA_URI"}}]}
        require(actual == expected, "request must have exact documented fields/content and no audio, credentials or inferred fields")
    check("exact_request_template", request)

    def provenance() -> None:
        text = json.dumps(manifest).lower()
        for token in ("ffmpeg", "command", "sha256", "transform", "route", "cost", "1.28", "2026-11-09", "unapproved"):
            require(token in text, f"missing provenance token: {token}")
        require("prepared_not_submitted" in text, "submission status")
    check("provenance_presence", provenance)

    def code_identity() -> list[dict]:
        found = []

        def visit(value):
            if isinstance(value, dict):
                if isinstance(value.get("path"), str) and value["path"].endswith("prepare_test.py"):
                    require("sha256" in value, "code identity missing SHA256")
                    require(sha(root.resolve(value["path"])) == value["sha256"], "preparation code hash mismatch")
                    found.append({"path": value["path"], "sha256": value["sha256"]})
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(manifest)
        require(bool(found), "no preparation code path/SHA256 identity found")
        return found
    check("preparation_code_actual_hash", code_identity)
    check("original_unchanged_after_verification", lambda: require(sha(root.resolve(SOURCE)) == SOURCE_SHA, "source changed during verification"))
    passed = bool(checks) and all(item["pass"] for item in checks)
    report = {"technical_preparation_pass": passed, "checks": checks, "pending": ["human contact-sheet inspection for figure/timestamp and exact frame correspondence", "source asset rights review", "provider route/account access and upload authorization"], "generation_occurred": False, "generated_audio_quality": "not_measured_no_generation", "limitations": ["No provider request, GPU work, audio generation, model fit, runtime or sound-quality measurement performed.", "Provenance presence and code hash are checked; declared transformations require parent review against source command."]}
    root.write_text(args.report, json.dumps(report, indent=2) + "\n")
    print(json.dumps({"technical_preparation_pass": passed, "report": args.report, "failed_checks": [item["name"] for item in checks if not item["pass"]]}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
