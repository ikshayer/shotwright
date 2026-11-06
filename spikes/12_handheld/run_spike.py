"""Does handheld motion actually move the rendered camera?

Deliberately uses a shot with NO anomaly, so any difference between frames is attributable
to camera motion alone. A matching CCTV shot is rendered as the control: it must produce
identical frames, which is what §4.3 criterion 3 requires of a locked-off camera.

The lesson this guards is the 900 identical frames: a correct plan proves nothing until
something renders it and the pixels are compared.

    PYTHONPATH=. .venv/Scripts/python.exe spikes/12_handheld/run_spike.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestrator.api.cli import main  # noqa: E402
from orchestrator.platform.settings import Settings, parse_settings  # noqa: E402

PROJECTS = REPO / "_work" / "handheld"
PROJECT_ID = "hh"
EPISODE_ID = "ep1"
DURATION_SEC = 2.0  # 120 frames at the 60 fps master — enough to see sway, cheap to render

SHOTS = [
    {"shot_id": "handheld", "camera_profile": "consumer-2002", "post_profile": "consumer-2002"},
    {"shot_id": "locked", "camera_profile": "dvr-2005"},
]


def settings() -> Settings:
    return parse_settings({
        "project_root": str(PROJECTS),
        "binaries": {"blender_exe": "D:/Blender/blender.exe"},
        "budget": {"per_shot_wall_clock_seconds": 3600.0, "gpu_minutes_per_shot": 60.0,
                   "gpu_minutes_per_episode": 600.0},
    })


def run(label: str, argv: list[str]) -> None:
    started = time.monotonic()
    status = main(argv, settings=settings())
    print(f"--- {label}: exit {status} in {time.monotonic() - started:.2f}s", flush=True)
    if status != 0:
        raise SystemExit(f"{label} failed")


def main_run() -> None:
    PROJECTS.mkdir(parents=True, exist_ok=True)
    run("create-project", ["create-project", "--json", "--data",
                           json.dumps({"project_id": PROJECT_ID, "title": "Handheld"})])
    run("create-episode", ["create-episode", "--json", "--project-id", PROJECT_ID, "--data",
                           json.dumps({"episode": {"episode_id": EPISODE_ID,
                                                   "project_id": PROJECT_ID, "number": 1,
                                                   "duration_target_sec": 30}})])
    run("draft-script", ["draft-script", "--json", "--project-id", PROJECT_ID,
                         "--episode-id", EPISODE_ID, "--data",
                         json.dumps({"script": {"script_id": "sc1", "episode_id": EPISODE_ID,
                                                "logline": "A held camera and a fixed one.",
                                                "beats": [{"beat_id": "b1", "target_sec": 2,
                                                           "subtlety": 2}]}})])
    shots = [{**s, "episode_id": EPISODE_ID, "duration_sec": DURATION_SEC, "event_sec": 1.0}
             for s in SHOTS]
    run("propose-shots", ["propose-shots", "--json", "--project-id", PROJECT_ID,
                          "--episode-id", EPISODE_ID, "--data", json.dumps({"shots": shots})])

    for shot in SHOTS:
        ids = ["--project-id", PROJECT_ID, "--episode-id", EPISODE_ID,
               "--shot-id", shot["shot_id"]]
        run(f"build-scene {shot['shot_id']}", ["build-scene", "--json", *ids])
        run(f"render-master {shot['shot_id']}", ["render-master", "--json", *ids])


if __name__ == "__main__":
    main_run()
    print("\nRUN COMPLETE", flush=True)
