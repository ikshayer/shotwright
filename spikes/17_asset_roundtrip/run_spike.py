"""Small real-queue round trip for the generated TRELLIS figure asset.

Run with the repository's Python environment. The default is a six-frame integration
probe; ``--duration 15`` enables the full living-room fixture after the probe works.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import time
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestrator.api.cli import main as cli_main
from orchestrator.domain.entities import Asset, Shot, ShotOutput
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import parse_settings
from orchestrator.store.json_store import ProjectStore

SOURCE_ASSET = Path(r"D:\Work\comfyui-trellis\ComfyUI\output\3D\entity_00001_.glb")
SOURCE_PROVENANCE = Path(r"D:\Work\trellis2-gguf\PROVENANCE.json")
PROJECT_ID, EPISODE_ID, SHOT_ID, ASSET_ID, LOCATION_ID = "asset_roundtrip", "ep1", "s1", "entity", "living_room"
ANOMALY_BASE: dict[str, Any] = {
    "kind": "linear_walk_path", "subtlety": 2, "height_m": 1.8, "width_m": 0.5,
    "depth_m": 0.35, "minimum_distance_m": 2.0, "maximum_on_frame_height_px": 400,
    "path": [[1.5, -1.0, 0.0], [1.5, 1.5, 0.0]], "figure_asset_id": ASSET_ID,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Harness:
    def __init__(self, root: ProjectRoot, run_rel: str, project_rel: str, duration: float,
                 provenance_text: str, asset_hash: str,
                 throughput_floor: float | None = None) -> None:
        self.root, self.run_rel, self.project_rel = root, run_rel, project_rel
        self.duration = duration
        self.event = 8.0 if duration == 15.0 else 1 / 60
        self.anomaly_duration = 4.0 if duration == 15.0 else 3 / 60
        # Settings points at the projects container; the actual project root is its
        # child created by create-project.
        self.projects_root = root.resolve(project_rel)
        self.project_relative = f"{project_rel}/{PROJECT_ID}"
        self.project_root = root.resolve(self.project_relative)
        self.throughput_floor = 0.001 if throughput_floor is None else throughput_floor
        self.settings = parse_settings({
            "project_root": str(self.projects_root),
            "binaries": {"blender_exe": "D:/Blender/blender.exe"},
            "budget": {"per_shot_wall_clock_seconds": 3600.0,
                       "gpu_minutes_per_shot": 60.0, "gpu_minutes_per_episode": 600.0},
            "resources": {"throughput_floor_per_second": self.throughput_floor},
        })
        self.provenance_text, self.asset_hash = provenance_text, asset_hash
        self.steps: list[dict[str, Any]] = []
        self.write_json(f"{self.run_rel}/runtime_settings.json", {
            "resources": {"throughput_floor_per_second": self.throughput_floor}})

    def write_json(self, relative: str, value: Any) -> None:
        self.root.write_text(relative, json.dumps(value, indent=2, ensure_ascii=False) + "\n")

    def cli(self, label: str, argv: list[str]) -> Any:
        print(f"=== {label}", flush=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        started = time.monotonic()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli_main(argv, settings=self.settings)
        elapsed = time.monotonic() - started
        # Keep the real operation output even on failure; the run remains diagnosable.
        record = {"step": label, "status": "completed" if status == 0 else "failed",
                  "exit_code": status, "elapsed_sec": round(elapsed, 3),
                  "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}
        self.steps.append(record)
        self.write_json(f"{self.run_rel}/steps/{len(self.steps):02d}_{label}.json", record)
        print(f"--- {label}: {record['status']} in {elapsed:.2f}s", flush=True)
        if status:
            raise RuntimeError(f"CLI operation {label} failed ({status}); see retained step output")
        try:
            return json.loads(stdout.getvalue())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"CLI operation {label} returned non-JSON output") from exc

    def run(self) -> None:
        p, e, s = PROJECT_ID, EPISODE_ID, SHOT_ID
        self.cli("create-project", ["create-project", "--json", "--data", json.dumps({
            "project_id": p, "title": "Generated asset round trip", "output_profile": "low-light-bw"})])
        self.cli("create-episode", ["create-episode", "--json", "--project-id", p,
            "--data", json.dumps({"episode": {"episode_id": e, "project_id": p,
                "number": 1, "duration_target_sec": self.duration}})])
        self.cli("draft-script", ["draft-script", "--json", "--project-id", p,
            "--episode-id", e, "--data", json.dumps({"script": {"script_id": "sc1",
                "episode_id": e, "logline": "A low-lit living room; a figure crosses the room.",
                "beats": [{"beat_id": "b1", "target_sec": self.event, "subtlety": 2}]}})])
        self.root.write_bytes(f"{self.project_relative}/assets/fonts/overlay.ttf",
                              Path(r"C:\Windows\Fonts\consola.ttf").read_bytes())
        self.write_json(f"{self.run_rel}/font_usage.json", {
            "source": "C:/Windows/Fonts/consola.ttf", "license_status": "internal-only local evaluation",
            "redistribution": "not cleared"})
        self.cli("create-asset", ["create-asset", "--json", "--project-id", p,
            "--file", str(SOURCE_ASSET), "--data", json.dumps({
                "asset_id": ASSET_ID, "type": "model", "generated": True,
                "provider": "TRELLIS2", "license": "Restricted to internal evaluation; not cleared for redistribution.",
                "model_or_voice_license": self.provenance_text,
                "reuse_scope": "internal evaluation only"})])
        self.cli("create-location", ["create-location", "--json", "--project-id", p,
            "--location-id", LOCATION_ID, "--data", json.dumps({"location": {
                "location_id": LOCATION_ID, "base_shell": "american_living_room",
                "scene_look": "low_key"}})])
        shot = {"shot_id": s, "episode_id": e, "location_id": LOCATION_ID,
                "duration_sec": self.duration, "event_sec": self.event,
                "camera_profile": "low-light-bw", "post_profile": "low-light-bw",
                "anomaly": {**ANOMALY_BASE,
                            "timing": {"duration_sec": self.anomaly_duration}}}
        self.cli("propose-shots", ["propose-shots", "--json", "--project-id", p,
            "--episode-id", e, "--data", json.dumps({"shots": [shot]})])
        ids = ["--project-id", p, "--episode-id", e, "--shot-id", s]
        self.cli("build-scene", ["build-scene", "--json", *ids])
        self.cli("render-master", ["render-master", "--json", *ids])
        self.cli("degrade-media", ["degrade-media", "--json", *ids])
        self.cli("report-jobs", ["report-jobs", "--json", "--project-id", p])
        self.record_evidence()

    def record_evidence(self) -> None:
        store = ProjectStore(ProjectRoot(self.project_root))
        asset = store.load(Asset, ASSET_ID)
        shot_outputs = store.list_shot_outputs(SHOT_ID, episode_id=EPISODE_ID)
        by_kind = {item.kind.value: item for item in shot_outputs}
        required = ("scene", "master", "degraded")
        missing = [kind for kind in required if kind not in by_kind]
        if missing:
            raise RuntimeError(f"missing output receipts: {missing}")
        master = by_kind["master"]
        expected_frames = round(self.duration * 60)
        master_path = ProjectRoot(self.project_root).resolve(master.relative_path)
        probe_result = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams",
            "v:0", "-show_entries", "stream=nb_read_frames", "-of", "json", str(master_path)],
            check=True, capture_output=True, text=True)
        actual_frames = int(json.loads(probe_result.stdout)["streams"][0]["nb_read_frames"])
        if actual_frames != expected_frames:
            raise RuntimeError(f"master has {actual_frames} frames; expected {expected_frames}")
        evidence = {
            "schema_version": 1, "run_id": self.run_rel.rsplit("/", 1)[-1],
            "run_relative": self.run_rel, "project_relative": self.project_relative,
            "duration_sec": self.duration, "expected_master_frames": expected_frames,
            "actual_master_frames": actual_frames,
            "runtime_settings": {
                "resources": {"throughput_floor_per_second": self.throughput_floor}},
            "source_asset_path": str(SOURCE_ASSET), "source_asset_sha256": self.asset_hash,
            "asset_id": asset.asset_id, "source_generator_caveat": self.provenance_text,
            "steps": [{"step": x["step"], "status": x["status"],
                       "elapsed_sec": x["elapsed_sec"]} for x in self.steps],
            "receipts": {kind: {"relative_path": rec.relative_path,
                                "content_sha256": rec.content_sha256,
                                "record_version": rec.record_version,
                                "output_id": rec.output_id}
                         for kind, rec in by_kind.items()},
        }
        self.write_json(f"{self.run_rel}/evidence.json", evidence)
        self.samples(master, by_kind["degraded"])

    def samples(self, master: ShotOutput, degraded: ShotOutput) -> None:
        extract_samples(self.root, self.run_rel, self.project_root, self.duration, master, degraded)


def extract_samples(root: ProjectRoot, run_rel: str, project_path: Path, duration: float,
                    master: ShotOutput, degraded: ShotOutput, *, include_master: bool = True) -> None:
    project = ProjectRoot(project_path)
    root.make_dir(f"{run_rel}/samples")
    samples: list[dict[str, Any]] = []
    sources = (("master", master), ("delivered", degraded)) if include_master else (("delivered", degraded),)
    for label, receipt in sources:
        src = project.resolve(receipt.relative_path)
        info = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames,sample_aspect_ratio",
            "-of", "json", str(src)],
            check=True, capture_output=True, text=True)
        probe = json.loads(info.stdout)["streams"][0]
        count = int(probe.get("nb_read_frames") or 0)
        if count <= 0:
            raise RuntimeError(f"{label} output has no decoded video frames")
        fps = float(Fraction(probe["avg_frame_rate"]))
        if duration == 15.0:
            target_times: list[float] = [0.0, 3.0, 4.0, 6.0, 8.4, 9.0, 10.0, 12.0]
            target_times.append((count - 1) / fps)
        else:
            target_times = [0.0, 3 / 60, 5 / 60]
        selected: dict[int, list[float]] = {}
        for target_time in target_times:
            index = min(count - 1, max(0, round(target_time * fps)))
            selected.setdefault(index, []).append(target_time)
        for index, requested_times in sorted(selected.items()):
            filename = f"{run_rel}/samples/{label}_{index:06d}.png"
            output = root.resolve(filename)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vf", f"select=eq(n\\,{index})",
                "-vsync", "0", "-frames:v", "1", "-y", str(output)], check=True,
                capture_output=True)
            if not output.is_file():
                raise RuntimeError(f"FFmpeg did not extract {filename}")
            decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output),
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                check=True, capture_output=True).stdout
            samples.append({"filename": filename, "source": label,
                "source_frame_index": index, "source_frame_count": count,
                "requested_time_sec": requested_times,
                "width": probe.get("width"), "height": probe.get("height"),
                "sample_aspect_ratio": probe.get("sample_aspect_ratio"),
                "decoded_sha256": hashlib.sha256(decoded).hexdigest()})
    root.write_text(f"{run_rel}/samples.json",
                    json.dumps({"samples": samples}, indent=2, ensure_ascii=False) + "\n")


def resolve_work_run(root: ProjectRoot, run_relative: str) -> tuple[Path, str]:
    run_path = root.resolve(run_relative.replace("\\", "/"))
    work_root = (REPO / "spikes" / "17_asset_roundtrip" / "_work").resolve()
    try:
        run_path.relative_to(work_root)
    except ValueError as exc:
        raise ValueError("run path must be beneath spikes/17_asset_roundtrip/_work") from exc
    return run_path, run_path.relative_to(REPO).as_posix()


def sample_existing_run(run_relative: str, *, delivery_only: bool = False) -> None:
    root = ProjectRoot(REPO)
    _, run_rel = resolve_work_run(root, run_relative)
    project_rel = f"{run_rel}/projects/{PROJECT_ID}"
    project_path = root.resolve(project_rel)
    store = ProjectStore(ProjectRoot(project_path))
    receipts = store.list_shot_outputs(SHOT_ID, episode_id=EPISODE_ID)
    by_kind = {item.kind.value: item for item in receipts}
    missing = [kind for kind in ("master", "degraded") if kind not in by_kind]
    if missing:
        raise RuntimeError(f"run is missing required receipts: {missing}")
    shot = store.load(Shot, SHOT_ID, episode_id=EPISODE_ID)
    extract_samples(root, run_rel, project_path, shot.duration_sec,
                    by_kind["master"], by_kind["degraded"],
                    include_master=not delivery_only)
    print(f"Samples extracted: {run_rel}", flush=True)


def make_contact_sheet(run_relative: str) -> None:
    """Tile existing delivered samples with ffprobe's display aspect ratio."""
    from PIL import Image, ImageDraw

    root = ProjectRoot(REPO)
    _, run_rel = resolve_work_run(root, run_relative)
    sample_data = json.loads(root.read_text(f"{run_rel}/samples.json"))
    delivered = [item for item in sample_data["samples"] if item["source"] == "delivered"]
    if not delivered:
        raise RuntimeError("samples.json has no delivered frames")
    store = ProjectStore(ProjectRoot(root.resolve(f"{run_rel}/projects/{PROJECT_ID}")))
    receipts = store.list_shot_outputs(SHOT_ID, episode_id=EPISODE_ID)
    output = next((item for item in receipts if item.kind.value == "degraded"), None)
    if output is None:
        raise RuntimeError("run has no degraded receipt")
    media = ProjectRoot(root.resolve(f"{run_rel}/projects/{PROJECT_ID}")).resolve(output.relative_path)
    probe_result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=sample_aspect_ratio", "-of", "json", str(media)],
        check=True, capture_output=True, text=True)
    sar_text = json.loads(probe_result.stdout)["streams"][0].get("sample_aspect_ratio")
    if not sar_text or sar_text in {"N/A", "0:1", "0/1"}:
        raise RuntimeError(f"delivery has no usable sample aspect ratio: {sar_text!r}")
    sar = float(Fraction(sar_text.replace(":", "/")))
    margin, caption_height, columns = 16, 28, 3
    tiles: list[tuple[Any, str]] = []
    display_width = 0
    for item in sorted(delivered, key=lambda sample: sample["source_frame_index"]):
        sample_path = root.resolve(item["filename"])
        with Image.open(sample_path) as opened:
            image = opened.convert("RGB")
        corrected_width = round(image.width * sar)
        display_width = max(display_width, corrected_width)
        image = image.resize((corrected_width, image.height), Image.Resampling.NEAREST)
        times = ", ".join(f"{value:g}s" for value in item["requested_time_sec"])
        caption = f"{times} | frame {item['source_frame_index']}"
        tiles.append((image, caption))
    rows = (len(tiles) + columns - 1) // columns
    tile_height = max(image.height for image, _ in tiles)
    sheet = Image.new("RGB", (columns * display_width + (columns + 1) * margin,
        rows * (tile_height + caption_height) + (rows + 1) * margin), "white")
    draw = ImageDraw.Draw(sheet)
    for position, (image, caption) in enumerate(tiles):
        col, row = position % columns, position // columns
        x = margin + col * (display_width + margin)
        y = margin + row * (tile_height + caption_height + margin)
        sheet.paste(image, (x, y))
        draw.text((x, y + tile_height + 4), caption, fill="black")
    root.write_with(f"{run_rel}/review-sheet.png",
                    lambda target: sheet.save(target, format="PNG"))
    print(f"Review sheet: {run_rel}/review-sheet.png (display SAR {sar_text})", flush=True)


def positive_finite(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, choices=(0.1, 15.0), default=0.1,
                        help="0.1 seconds (six frames) or full 15-second fixture")
    run_actions = parser.add_mutually_exclusive_group()
    run_actions.add_argument("--samples-only", metavar="RUN_RELATIVE",
        help="extract samples from a completed run without running pipeline steps")
    run_actions.add_argument("--review-sheet", metavar="RUN_RELATIVE",
        help="tile existing delivered samples into a contained review sheet")
    parser.add_argument("--delivery-only", action="store_true",
                        help="with --samples-only, skip master probing and extraction")
    parser.add_argument("--throughput-floor", type=positive_finite, default=None,
                        help="optional positive finite GPU progress floor per second (default: 0.001)")
    args = parser.parse_args()
    if args.samples_only:
        sample_existing_run(args.samples_only, delivery_only=args.delivery_only)
        return
    if args.delivery_only:
        parser.error("--delivery-only requires --samples-only")
    if args.review_sheet:
        make_contact_sheet(args.review_sheet)
        return
    run_id = time.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    run_rel = f"spikes/17_asset_roundtrip/_work/{run_id}"
    root = ProjectRoot(REPO)
    # Refuse reuse, and route initial directory creation through ProjectRoot.
    if root.resolve(run_rel).exists():
        raise FileExistsError(run_rel)
    root.make_dir(run_rel)
    root.make_dir(f"{run_rel}/runtime/tmp")
    root.make_dir(f"{run_rel}/runtime/blender-config")
    root.make_dir(f"{run_rel}/runtime/blender-cache")
    print(f"Run directory: {run_rel}", flush=True)
    projects_rel = f"{run_rel}/projects"
    root.make_dir(projects_rel)
    os.environ["TMP"] = str(root.resolve(f"{run_rel}/runtime/tmp"))
    os.environ["TEMP"] = os.environ["TMP"]
    os.environ["TMPDIR"] = os.environ["TMP"]
    os.environ["BLENDER_USER_CONFIG"] = str(root.resolve(f"{run_rel}/runtime/blender-config"))
    os.environ["BLENDER_USER_SCRIPTS"] = os.environ["BLENDER_USER_CONFIG"]
    os.environ["BLENDER_USER_DATAFILES"] = str(root.resolve(f"{run_rel}/runtime/blender-cache"))
    if not SOURCE_ASSET.is_file() or not SOURCE_PROVENANCE.is_file():
        raise FileNotFoundError("generated GLB or source PROVENANCE.json is unavailable")
    provenance_bytes = SOURCE_PROVENANCE.read_bytes()
    provenance = json.loads(provenance_bytes)["licence_note"]
    # Retain the exact source generator record in the run, through the containment helper.
    root.write_bytes(f"{run_rel}/source_PROVENANCE.json", provenance_bytes)
    harness = Harness(root, run_rel, projects_rel, args.duration, provenance,
                      sha256(SOURCE_ASSET), args.throughput_floor)
    try:
        harness.run()
    except BaseException as exc:
        root.write_text(f"{run_rel}/failure.txt", f"{type(exc).__name__}: {exc}\n")
        raise
    print(f"Run complete: {run_rel}")


if __name__ == "__main__":
    main()
