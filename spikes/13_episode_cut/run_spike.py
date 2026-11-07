"""Assemble the two-style episode into one playable cut (FR-09).

Uses the shots spike 10 already delivered: `a-dvr` at 640x480 SAR 1:1 12 fps and
`b-lowlight` at 352x288 SAR 12:11 10 fps. The point is that a mixed-spec episode becomes
one file without the CIF shot being stretched.

Drives the service directly, because FR-09 has no CLI or API operation yet. That gap is
itself a finding: the Phase 1 exit clause requires the pipeline be "reached through the
API or CLI".

    PYTHONPATH=. .venv/Scripts/python.exe spikes/13_episode_cut/run_spike.py
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestrator.adapters.timeline.runner import TimelineAssembler  # noqa: E402
from orchestrator.platform.containment import ProjectRoot  # noqa: E402
from orchestrator.platform.settings import parse_settings  # noqa: E402
from orchestrator.service.timeline import assemble_episode, compose_timeline  # noqa: E402
from orchestrator.store.json_store import ProjectStore  # noqa: E402

PROJECT_DIR = REPO / "_work" / "two_styles" / "styles"


class Supervisor:
    """Minimal stand-in for the adapter supervisor: never stops, tracks the child."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.process: subprocess.Popen[bytes] | None = None

    def stopped(self) -> bool:
        return self._stop.is_set()

    def launch(self, argv, **kwargs):
        self.process = subprocess.Popen(argv, **kwargs)
        return self.process

    def clear_process(self, process) -> None:
        if self.process is process:
            self.process = None



def _approve_all(store: ProjectStore) -> None:
    """Create a passing Review per shot and approve it, as a reviewer would."""
    from datetime import datetime, timezone

    from orchestrator.domain.entities import Episode, Review, Shot
    from orchestrator.service.approval import approve_shot

    episode = store.load(Episode, "ep1")
    for shot_id in episode.shot_ids:
        shot, token = store.load_with_version(Shot, shot_id, episode_id="ep1")
        review_id = f"rv-{shot_id}"
        store.save(
            Review(
                review_id=review_id,
                target_id=shot_id,
                target_version=shot.version,
                created_at=datetime.now(timezone.utc),
                approval_state="approved",
            ),
            expected_version=0,
        )
        approve_shot(store, shot_id, episode_id="ep1", review_id=review_id,
                     expected_version=token)
        print(f"approved {shot_id} (content v{shot.version}) via {review_id}")


def main() -> None:
    root = ProjectRoot(PROJECT_DIR)
    settings = parse_settings({
        "project_root": str(PROJECT_DIR.parent),
        "binaries": {"blender_exe": "D:/Blender/blender.exe"},
        "budget": {"per_shot_wall_clock_seconds": 3600.0},
    })
    store = ProjectStore(root)

    # FR-09 assembles APPROVED shots only, and the shots arrive as drafts. There is no
    # API or CLI operation for approval, so the spike drives the service directly; that
    # missing operation is itself a finding, recorded in FINDINGS.md.
    _approve_all(store)

    timeline = compose_timeline(store, episode_id="ep1")
    print(f"timeline entries: {len(timeline.entries)}")
    for entry in timeline.entries:
        print(f"  {entry.model_dump(mode='json')}")

    started = time.monotonic()
    receipt = assemble_episode(
        store,
        TimelineAssembler(root, settings, Supervisor()),
        episode_id="ep1",
        timeline=timeline,
    )
    print(f"\nassembled in {time.monotonic() - started:.2f}s")
    print(f"receipt -> {receipt.relative_path}")
    print(f"sha256    {receipt.content_sha256}")


if __name__ == "__main__":
    main()
