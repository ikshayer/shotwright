"""FR-20 composition tests: typed Shot anomalies must reach the Blender plan."""

from __future__ import annotations

import hashlib
from pathlib import Path

from orchestrator.adapters.blender.scene import build_scene_plan
from orchestrator.domain.anomaly import AnomalySpec
from orchestrator.domain.entities import CameraProfile, Episode, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.compose import compose_render_request
from orchestrator.store.json_store import ProjectStore


def _compose_plan(tmp_path: Path, anomaly: AnomalySpec | None, *, event_sec: float) -> dict[str, object]:
    """Compose a v2 Shot and return its CPU-only worker plan."""
    root = tmp_path / f"case-{sum(1 for _ in tmp_path.iterdir())}"
    root.mkdir()
    store = ProjectStore(ProjectRoot(root))
    store.save(Project(project_id="p1", title="FR-20 composition fixture"), expected_version=0)
    store.save(
        Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
        expected_version=0,
    )
    store.create(CameraProfile(camera_id="cam", profile_name="test"))

    duration_sec = max(1.01, event_sec + 0.5)
    if anomaly is not None:
        duration_sec = max(duration_sec, event_sec + anomaly.timing.duration_sec + 0.5)
    shot_values: dict[str, object] = {
        "shot_id": "shot-1",
        "episode_id": "ep1",
        "camera_profile": "cam",
        "duration_sec": duration_sec,
        "event_sec": event_sec,
        "degrade_seed": 9,
    }
    if anomaly is not None:
        shot_values["anomaly"] = anomaly
    first_version = Shot(**shot_values)
    store.create(first_version)
    successor = first_version.model_copy(update=first_version.next_version())
    store.revise(successor, expected_version=1)

    request = compose_render_request(
        store,
        shot_id="shot-1",
        episode_id="ep1",
        run_id="run-1",
        mode="master",
    )
    return build_scene_plan(request)


def test_shot_without_anomaly_composes_to_the_current_static_plan(tmp_path: Path) -> None:
    from tests.adapters.blender.test_anomaly_plan_contract import (
        _STATIC_PLAN_SHA256,
        _canonical_plan_bytes,
    )

    plan = _compose_plan(tmp_path, None, event_sec=0.5)
    observed = hashlib.sha256(_canonical_plan_bytes(plan)).hexdigest()

    assert observed == _STATIC_PLAN_SHA256, (
        "Shot with no anomaly changed the current static worker plan; observed "
        f"canonical SHA-256 {observed}, expected {_STATIC_PLAN_SHA256}"
    )
