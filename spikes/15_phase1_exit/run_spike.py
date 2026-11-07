"""Phase 1 acceptance run — the shot the Exit clause asks for.

    Exit: one finished 15-30 second CCTV shot with a traceable source package, no
    user-supplied assets, and a passing 4.3 rubric score - reached through the API or CLI.

Creator decisions of 2026-11-07 shaping this run:
  * camera style `low-light-bw`, the monochrome night CCTV profile matching the creator's
    reference images;
  * the humanoid silhouette figure, built before the run rather than after;
  * the orchestrator samples and pre-scores 4.3, the creator ratifies.

Everything goes through the real CLI. 15 s is the floor of the Exit clause's range and is
deliberate: at the measured ~0.37 s/frame a 60 fps master is 900 frames, and a 30 s shot
would double an already long run without changing what the gate tests.

    PYTHONPATH=. .venv/Scripts/python.exe spikes/15_phase1_exit/run_spike.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestrator.api.cli import main  # noqa: E402
from orchestrator.platform.settings import Settings, parse_settings  # noqa: E402

PROJECTS = REPO / "_work" / "phase1_exit"
PROJECT_ID = "exit"
EPISODE_ID = "ep1"
SHOT_ID = "s1"
DURATION_SEC = 15.0
EVENT_SEC = 8.0

# The figure walks across the room at the event. Distance and on-frame height are the
# shot's declared limits, enforced geometrically against the camera's lens and the master
# resolution before any render is spent.
ANOMALY = {
    "kind": "linear_walk_path",
    "timing": {"duration_sec": 4.0},
    "subtlety": 2,
    "height_m": 1.8,
    "width_m": 0.5,
    "depth_m": 0.35,
    "minimum_distance_m": 2.0,
    "maximum_on_frame_height_px": 400,
    "path": [[1.5, -1.0, 0.0], [1.5, 1.5, 0.0]],
}


def settings() -> Settings:
    return parse_settings({
        "project_root": str(PROJECTS),
        "binaries": {"blender_exe": "D:/Blender/blender.exe"},
        "budget": {"per_shot_wall_clock_seconds": 3600.0, "gpu_minutes_per_shot": 60.0,
                   "gpu_minutes_per_episode": 600.0},
    })


def run(label: str, argv: list[str]) -> None:
    print(f"\n=== {label}", flush=True)
    started = time.monotonic()
    status = main(argv, settings=settings())
    print(f"--- {label}: exit {status} in {time.monotonic() - started:.2f}s", flush=True)
    if status != 0:
        raise SystemExit(f"{label} failed with exit status {status}")


def main_run() -> None:
    PROJECTS.mkdir(parents=True, exist_ok=True)
    ids = ["--project-id", PROJECT_ID, "--episode-id", EPISODE_ID, "--shot-id", SHOT_ID]

    run("create-project", ["create-project", "--json", "--data", json.dumps(
        {"project_id": PROJECT_ID, "title": "Phase 1 exit",
         "output_profile": "low-light-bw"})])
    run("create-episode", ["create-episode", "--json", "--project-id", PROJECT_ID,
                           "--data", json.dumps({"episode": {
                               "episode_id": EPISODE_ID, "project_id": PROJECT_ID,
                               "number": 1, "duration_target_sec": 30}})])
    run("draft-script", ["draft-script", "--json", "--project-id", PROJECT_ID,
                         "--episode-id", EPISODE_ID, "--data", json.dumps({"script": {
                             "script_id": "sc1", "episode_id": EPISODE_ID,
                             "logline": "A living room at night; something crosses it.",
                             "beats": [{"beat_id": "b1", "target_sec": 8, "subtlety": 2},
                                       {"beat_id": "b2", "target_sec": 7, "subtlety": 3}]}})])

    # The pipeline ships no font: a typeface is a creative choice and redistributing one
    # carries a licence obligation. Using a font installed on this machine is not a
    # user-supplied *asset* in the Exit clause's sense -- no footage, image or model is
    # creator-supplied. _work is gitignored, so nothing is redistributed.
    font = PROJECTS / PROJECT_ID / "assets" / "fonts" / "overlay.ttf"
    font.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path("C:/Windows/Fonts/consola.ttf"), font)

    run("propose-shots", ["propose-shots", "--json", "--project-id", PROJECT_ID,
                          "--episode-id", EPISODE_ID, "--data", json.dumps({"shots": [{
                              "shot_id": SHOT_ID, "episode_id": EPISODE_ID,
                              "duration_sec": DURATION_SEC, "event_sec": EVENT_SEC,
                              "camera_profile": "low-light-bw",
                              "post_profile": "low-light-bw",
                              "scene_look": "low_key",
                              "anomaly": ANOMALY}]})])

    run("build-scene", ["build-scene", "--json", *ids])
    run("render-master", ["render-master", "--json", *ids])
    run("degrade-media", ["degrade-media", "--json", *ids])
    run("report-jobs", ["report-jobs", "--json", "--project-id", PROJECT_ID])


if __name__ == "__main__":
    main_run()
    print("\nRUN COMPLETE", flush=True)
