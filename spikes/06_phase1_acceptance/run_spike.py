"""Phase 1 acceptance run: project -> episode -> script -> shot -> scene -> master.

Driven through the real CLI (`orchestrator.api.cli.main`), not the service layer, so the
entry path is what gets exercised. Disposable; evidence lands under _work/phase1.

Two settings choices matter and are deliberate:

* `throughput_floor_per_second` keeps its NONZERO default. Switching it off is the
  workaround three passes used instead of reporting progress, so a run with the floor off
  would prove nothing about the fix.
* The budget ceilings are raised. The defaults (900 s wall clock, 20 GPU-minutes) are
  UNMEASURED PLACEHOLDERs, and a 15 s shot at the 60 fps master is 900 frames -- at the
  measured 0.56-0.63 s/frame that is 8-9.5 minutes, close enough to the 900 s default to
  fail on variance. This run is the first real AT-15 measurement; it should not be shaped
  by a placeholder it is meant to replace.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from orchestrator.api.cli import main
from orchestrator.platform.settings import Settings, parse_settings

REPO = Path(__file__).resolve().parents[2]
# Artifacts go to the gitignored, disposable tree, never beside this script: one 15 s
# shot is 1.76 GB of EXR.
PROJECTS = REPO / "_work" / "phase1"
PROJECT_ID = "phase1"
EPISODE_ID = "ep1"
SHOT_ID = "s1"
CAMERA_ID = "dvr-2005"  # Shot.camera_profile's default; the composer resolves it by name.
DURATION_SEC = 15.0     # Phase 1's floor: 15-30 s. 15 s = 900 frames at the 60 fps master.


def settings() -> Settings:
    return parse_settings({
        "project_root": str(PROJECTS),
        "binaries": {"blender_exe": "D:/Blender/blender.exe"},
        "budget": {
            "per_shot_wall_clock_seconds": 3600.0,
            "gpu_minutes_per_shot": 60.0,
            "gpu_minutes_per_episode": 240.0,
        },
    })


def run(label: str, argv: list[str]) -> int:
    print(f"\n=== {label}\n$ python -m orchestrator.api {' '.join(argv)}", flush=True)
    started = time.monotonic()
    status = main(argv, settings=settings())
    elapsed = time.monotonic() - started
    print(f"--- {label}: exit {status} in {elapsed:.3f}s", flush=True)
    if status != 0:
        raise SystemExit(f"{label} failed with exit status {status}")
    return status


def main_run() -> None:
    PROJECTS.mkdir(parents=True, exist_ok=True)

    run("create-project", [
        "create-project", "--json",
        "--data", json.dumps({"project_id": PROJECT_ID, "title": "Phase 1 acceptance"}),
    ])

    run("create-episode", [
        "create-episode", "--json", "--project-id", PROJECT_ID,
        "--data", json.dumps({"episode": {
            "episode_id": EPISODE_ID, "project_id": PROJECT_ID,
            "number": 1, "duration_target_sec": 30,
        }}),
    ])

    run("draft-script", [
        "draft-script", "--json", "--project-id", PROJECT_ID, "--episode-id", EPISODE_ID,
        "--data", json.dumps({"script": {
            "script_id": "sc1", "episode_id": EPISODE_ID,
            "logline": "A corridor camera holds on an empty doorway until it does not.",
            "beats": [
                {"beat_id": "b1", "target_sec": 6, "subtlety": 2},
                {"beat_id": "b2", "target_sec": 9, "subtlety": 3},
            ],
        }}),
    ])

    # No create-camera-profile: `create-project` already seeds the profile named by
    # Project.output_profile ("dvr-2005"), so creating it again is a version_conflict
    # against the stored version 1. The composer resolves the seeded one by name.

    # A figure walks across the room at event_sec. The spike-02 room is 6x4x2.7 m with the
    # camera at (-2.75, -1.75, 2.52), so a path along x=1.5 keeps the figure about 4.3 m
    # away -- comfortably past the 2.0 m minimum this shot declares. At 12 mm on a 36 mm
    # sensor, focal_px = 12*1280/36 = 426.7, so a 1.8 m figure at 4.3 m projects to roughly
    # 180 px; the 400 px cap is the shot's declared ceiling, not a fitted number.
    anomaly = {
        "kind": "linear_walk_path",
        "timing": {"duration_sec": 3.0},
        "subtlety": 2,
        "height_m": 1.8,
        "width_m": 0.5,
        "depth_m": 0.35,
        "minimum_distance_m": 2.0,
        "maximum_on_frame_height_px": 400,
        "path": [[1.5, -1.0, 0.0], [1.5, 1.5, 0.0]],
    }

    run("propose-shots", [
        "propose-shots", "--json", "--project-id", PROJECT_ID, "--episode-id", EPISODE_ID,
        "--data", json.dumps({"shots": [{
            "shot_id": SHOT_ID, "episode_id": EPISODE_ID,
            "duration_sec": DURATION_SEC, "event_sec": 8.0,
            "camera_profile": CAMERA_ID,
            "anomaly": anomaly,
        }]}),
    ])

    run("build-scene", [
        "build-scene", "--json", "--project-id", PROJECT_ID,
        "--episode-id", EPISODE_ID, "--shot-id", SHOT_ID,
    ])

    run("render-master", [
        "render-master", "--json", "--project-id", PROJECT_ID,
        "--episode-id", EPISODE_ID, "--shot-id", SHOT_ID,
    ])

    # Degradation is attempted but NOT allowed to fail the run: Amendment 3 made
    # PostProfileOverlay.font_relative required at composition, and the profile seeded by
    # create-project predates it. Whether this step works is itself the finding.
    try:
        run("degrade-media", [
            "degrade-media", "--json", "--project-id", PROJECT_ID,
            "--episode-id", EPISODE_ID, "--shot-id", SHOT_ID,
        ])
    except SystemExit as exc:
        print(f"--- degrade-media FAILED (recorded, not fatal): {exc}", flush=True)

    run("report-jobs", ["report-jobs", "--json", "--project-id", PROJECT_ID])


if __name__ == "__main__":
    try:
        main_run()
    except SystemExit as exc:
        print(f"\nRUN FAILED: {exc}", file=sys.stderr, flush=True)
        raise
    print("\nRUN COMPLETE", flush=True)
