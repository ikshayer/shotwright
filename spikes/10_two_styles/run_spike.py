"""One episode, two shots, two camera styles — the creator's core requirement.

The creator's intent: "the pipeline can switch between different camera style depending on
the script or what the user wants... an analog horror production pipeline rather than
strictly a pipeline to create short random CCTV camera clips."

This drives the real CLI to produce two shots in ONE episode that differ in camera profile,
degradation profile and scene lighting, and delivers both. It is a style demonstration, not
the Phase 1 acceptance run: shots are 5 s rather than 15-30 s so two full renders fit in a
reasonable wall clock. Artifacts land in gitignored _work/two_styles.

    PYTHONPATH=. .venv/Scripts/python.exe spikes/10_two_styles/run_spike.py
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

PROJECTS = REPO / "_work" / "two_styles"
PROJECT_ID = "styles"
EPISODE_ID = "ep1"
DURATION_SEC = 5.0

# A figure crosses the room at the event. Same staging for both shots, so any difference in
# the delivered frames is attributable to camera style alone.
ANOMALY = {
    "kind": "linear_walk_path",
    "timing": {"duration_sec": 2.0},
    "subtlety": 2,
    "height_m": 1.8,
    "width_m": 0.5,
    "depth_m": 0.35,
    "minimum_distance_m": 2.0,
    "maximum_on_frame_height_px": 400,
    "path": [[1.5, -1.0, 0.0], [1.5, 1.5, 0.0]],
}

SHOTS = [
    # Today's default: bright room, colour DVR.
    {"shot_id": "a-dvr", "camera_profile": "dvr-2005"},
    # The creator's references: low-key room, monochrome night camera.
    {"shot_id": "b-lowlight", "camera_profile": "low-light-bw",
     "post_profile": "low-light-bw", "scene_look": "low_key"},
]


def settings() -> Settings:
    return parse_settings({
        "project_root": str(PROJECTS),
        "binaries": {"blender_exe": "D:/Blender/blender.exe"},
        "budget": {"per_shot_wall_clock_seconds": 3600.0, "gpu_minutes_per_shot": 60.0,
                   "gpu_minutes_per_episode": 600.0},
    })


def run(label: str, argv: list[str], *, fatal: bool = True) -> int:
    print(f"\n=== {label}", flush=True)
    started = time.monotonic()
    status = main(argv, settings=settings())
    print(f"--- {label}: exit {status} in {time.monotonic() - started:.3f}s", flush=True)
    if status != 0 and fatal:
        raise SystemExit(f"{label} failed with exit status {status}")
    return status


def main_run() -> None:
    PROJECTS.mkdir(parents=True, exist_ok=True)

    run("create-project", ["create-project", "--json", "--data",
                           json.dumps({"project_id": PROJECT_ID, "title": "Two styles"})])
    run("create-episode", ["create-episode", "--json", "--project-id", PROJECT_ID,
                           "--data", json.dumps({"episode": {
                               "episode_id": EPISODE_ID, "project_id": PROJECT_ID,
                               "number": 1, "duration_target_sec": 30}})])
    run("draft-script", ["draft-script", "--json", "--project-id", PROJECT_ID,
                         "--episode-id", EPISODE_ID, "--data", json.dumps({"script": {
                             "script_id": "sc1", "episode_id": EPISODE_ID,
                             "logline": "Two cameras watch the same room on the same night.",
                             "beats": [{"beat_id": "b1", "target_sec": 5, "subtlety": 2},
                                       {"beat_id": "b2", "target_sec": 5, "subtlety": 3}]}})])

    # The overlay font is a creative choice the pipeline deliberately does not ship. Use one
    # installed on this machine; _work is gitignored, so nothing is redistributed.
    font = PROJECTS / PROJECT_ID / "assets" / "fonts" / "overlay.ttf"
    font.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path("C:/Windows/Fonts/consola.ttf"), font)
    print(f"font placed at {font.relative_to(PROJECTS)}", flush=True)

    shots = [{**s, "episode_id": EPISODE_ID, "duration_sec": DURATION_SEC,
              "event_sec": 2.5, "anomaly": ANOMALY} for s in SHOTS]
    run("propose-shots", ["propose-shots", "--json", "--project-id", PROJECT_ID,
                          "--episode-id", EPISODE_ID,
                          "--data", json.dumps({"shots": shots})])

    for shot in SHOTS:
        sid = shot["shot_id"]
        ids = ["--project-id", PROJECT_ID, "--episode-id", EPISODE_ID, "--shot-id", sid]
        run(f"build-scene {sid}", ["build-scene", "--json", *ids])
        run(f"render-master {sid}", ["render-master", "--json", *ids])
        run(f"degrade-media {sid}", ["degrade-media", "--json", *ids], fatal=False)

    run("report-jobs", ["report-jobs", "--json", "--project-id", PROJECT_ID])


if __name__ == "__main__":
    main_run()
    print("\nRUN COMPLETE", flush=True)
