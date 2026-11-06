"""Assemble reproducible visual and render evidence for spike 09."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))

from orchestrator.platform.containment import ProjectRoot

REFERENCE = Path(r"D:\Work\video_pipeline\references\room\Screenshot 2026-11-06 134126.png")
PERCENTILES = (1, 5, 25, 50, 75, 95, 99)


def _load_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _image_metrics(image: Image.Image) -> dict[str, Any]:
    """Measure display-referred Rec. 709 luma on normalized sRGB code values."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    luma = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    values = np.percentile(luma, PERCENTILES)
    return {
        "width": int(image.width),
        "height": int(image.height),
        "pixel_count": int(luma.size),
        "luma_definition": "0.2126 R + 0.7152 G + 0.0722 B on normalized sRGB code values",
        "fraction_luma_le_8_over_255": float(np.mean(luma <= (8.0 / 255.0))),
        "fraction_luma_ge_245_over_255": float(np.mean(luma >= (245.0 / 255.0))),
        "luma_percentiles_0_to_1": {
            str(p): float(v) for p, v in zip(PERCENTILES, values, strict=True)
        },
    }


def _render_cost_record(relative: str, root: ProjectRoot) -> dict[str, Any]:
    record = json.loads(root.read_text(relative))
    metadata = record.get("actual_worker_metadata")
    settings = metadata.get("applied_settings") if isinstance(metadata, dict) else None
    return {
        "run_id": record.get("run_id"),
        "recovery": record.get("recovery", False),
        "started_utc": record.get("started_utc"),
        "configuration": record.get("configuration"),
        "wall_seconds_including_blender_startup": record.get("wall_seconds_including_blender_startup"),
        "worker_elapsed_seconds": (metadata.get("elapsed_seconds") if isinstance(metadata, dict)
                                   else record.get("worker_elapsed_seconds")),
        "per_frame_render_seconds": record.get("per_frame_render_seconds"),
        "first_frame_setup_and_render_seconds": record.get("first_frame_setup_and_render_seconds"),
        "warm_progress_cadence_seconds": record.get("warm_progress_cadence_seconds"),
        "progress_intervals_seconds": record.get("progress_intervals_seconds"),
        "nvml": record.get("nvml"),
        "other_blender_processes_observed": record.get("other_blender_processes_observed"),
        "actual_device": metadata.get("actual_device") if isinstance(metadata, dict) else None,
        "actual_settings": settings,
        "telemetry_file": relative,
        "limitations": {
            "wall_seconds": record.get("wall_seconds_limitation"),
            "nvml": record.get("nvml_limitation"),
        } if record.get("recovery") else None,
    }


def _save_side_by_side(root: ProjectRoot, reference: Path, final: Path) -> None:
    left = _load_rgb(reference)
    right = _load_rgb(final)
    height = max(left.height, right.height)
    if left.height != height:
        width = max(1, round(left.width * height / left.height))
        left = left.resize((width, height), Image.Resampling.LANCZOS)
    if right.height != height:
        width = max(1, round(right.width * height / right.height))
        right = right.resize((width, height), Image.Resampling.LANCZOS)
    gutter = 12
    label_height = 42
    canvas = Image.new("RGB", (left.width + gutter + right.width, label_height + height), "#202020")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    draw.text((8, 11), reference.name, fill="white", font=font)
    draw.text((left.width + gutter + 8, 11), final.name, fill="white", font=font)
    canvas.paste(left, (0, label_height))
    canvas.paste(right, (left.width + gutter, label_height))
    root.write_with("side-by-side.png", lambda target: canvas.save(target, format="PNG"))


def run(args: argparse.Namespace) -> int:
    root = ProjectRoot(HERE)
    final_run = args.final_run
    run_rel = f"_work/{final_run}"
    scene_rel = f"{run_rel}/resolved_scene.json"
    profile_rel = f"{run_rel}/resolved_profile.json"
    png_rel = f"{final_run}.png"
    for relative in (scene_rel, profile_rel, png_rel):
        if not root.resolve(relative).is_file():
            raise FileNotFoundError(f"required final-run artifact is missing: {relative}")
    root.write_bytes("final-scene.json", root.read_bytes(scene_rel))
    root.write_bytes("final-profile.json", root.read_bytes(profile_rel))
    root.write_bytes("final.png", root.read_bytes(png_rel))
    _save_side_by_side(root, REFERENCE, root.resolve("final.png"))

    telemetry_files = sorted(root.resolve("_work").glob("*/telemetry.json"))
    render_costs = [
        _render_cost_record(path.relative_to(root.path).as_posix(), root)
        for path in telemetry_files
    ]
    root.write_text("render-costs.json", json.dumps({
        "schema_version": 1,
        "generated_utc": datetime.now(UTC).isoformat(),
        "final_run": final_run,
        "runs": render_costs,
    }, indent=2, sort_keys=True) + "\n")

    image_paths: list[tuple[str, Path]] = [("reference", REFERENCE), ("current-room", root.resolve("current-room.png"))]
    for path in sorted(root.path.glob("*.png")):
        if path.name in {"current-room.png", "final.png", "side-by-side.png"}:
            continue
        if re.fullmatch(r"[A-Za-z0-9_-]+\.png", path.name):
            image_paths.append((path.stem, path))
    image_paths.append(("final", root.resolve("final.png")))
    metrics: dict[str, Any] = {
        "schema_version": 1,
        "generated_utc": datetime.now(UTC).isoformat(),
        "measurement_note": "Image statistics describe pixel values only and are not a visual quality verdict.",
        "images": {},
    }
    for label, path in image_paths:
        metrics["images"][label] = _image_metrics(_load_rgb(path))
    root.write_text("metrics.json", json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "final_run": final_run,
        "artifacts": ["final-scene.json", "final-profile.json", "final.png", "side-by-side.png", "render-costs.json", "metrics.json"],
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final-run", required=True, help="run ID beneath _work to package as final evidence")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.final_run):
        parser.error("final-run must match [A-Za-z0-9_-]+")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
