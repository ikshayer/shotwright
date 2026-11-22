"""AT-18 through the loopback scene and master operations with fake child processes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.domain.anomaly import AnomalyTiming, LightFlicker
from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    GenerationRun,
    Job,
    Location,
    Project,
    Review,
    Shot,
    ShotOutputKind,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobErrorKind
from orchestrator.service.approval import approve_shot
from orchestrator.service.dependencies import revise_camera_profile
from orchestrator.service.runner import AdapterKind, AdapterRegistry, JobRunner, _AdapterShim
from orchestrator.store.json_store import ProjectStore
from tests.adapters.blender.test_supervision_contract import FakePopen, setup

_EPISODE_ID = "ep1"
_ROOM_ID = "shared-room"
_RIG_ID = "shared-rig"
_TOKEN = "at18-test-token"


class _WorkerBoundary(FakePopen):
    """Simulate only the documented shared-base plan at the Blender process seam."""

    def __init__(self, root: ProjectRoot) -> None:
        super().__init__()
        self.root = root
        self.plans: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any):
        plan_path = Path(argv[-1]).resolve().relative_to(self.root.path).as_posix()
        plan = json.loads(self.root.read_text(plan_path))
        self.plans.append(plan)
        child = super().__call__(argv, **kwargs)
        base = plan.get("shared_base")
        if base is None:
            return child

        assert "--disable-autoexec" in argv
        if base["input"] is None:
            assert base["output"]
            blend_path = Path(base["output"]).resolve().relative_to(self.root.path).as_posix()
            self.root.write_bytes(blend_path, f"BLENDER-v300 fixture {base['key']}".encode())
            action = "built"
        else:
            assert base["output"] is None
            blend_path = Path(base["input"]["path"]).resolve().relative_to(self.root.path).as_posix()
            assert _digest(self.root.read_bytes(blend_path)) == base["input"]["sha256"]
            action = "reused"

        metadata_path = Path(plan["outputs"]["metadata"]).resolve().relative_to(self.root.path).as_posix()
        metadata = json.loads(self.root.read_text(metadata_path))
        metadata["shared_base"] = {"key": base["key"], "action": action}
        self.root.write_text(metadata_path, json.dumps(metadata))
        return child


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass
class _Pipeline:
    root: ProjectRoot
    store: ProjectStore
    runner: JobRunner
    worker: _WorkerBoundary
    client: TestClient
    generations: list[GenerationRun] = field(default_factory=list)

    def submit(self, shot_id: str, stage: str, *, episode_id: str = _EPISODE_ID) -> Job:
        response = self.client.post(f"/projects/p1/episodes/{episode_id}/shots/{shot_id}/{stage}", json={})
        assert response.status_code == 202, response.text
        return self.runner.wait("p1", response.json()["job_id"])

    def render(self, shot_id: str, *, episode_id: str = _EPISODE_ID) -> dict[str, Any]:
        built_scene = self.submit(shot_id, "scene", episode_id=episode_id)
        assert built_scene.state == "completed", built_scene.model_dump()
        rendered = self.submit(shot_id, "master", episode_id=episode_id)
        assert rendered.state == "completed", rendered.model_dump()
        plan = self.worker.plans[-1]
        assert plan["shot_id"] == shot_id
        return plan

    def relative_path(self, path: str) -> str:
        return Path(path).resolve().relative_to(self.root.path).as_posix()


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = ProjectRoot(tmp_path)
    project_path = workspace.make_dir("p1")
    worker = _WorkerBoundary(ProjectRoot(project_path))
    root, settings, adapter, queue, _job = setup(project_path, popen=worker)
    settings.project_root = tmp_path
    store = queue.store
    store.save(Project(project_id="p1", title="AT-18 reuse"), expected_version=0)
    store.save(Episode(episode_id=_EPISODE_ID, project_id="p1", number=1, duration_target_sec=60), expected_version=0)
    store.create(Location(location_id=_ROOM_ID, camera_rigs=[_RIG_ID]))
    store.create(CameraProfile(camera_id=_RIG_ID, profile_name="Living room fixed camera"))
    store.create(Shot(
        shot_id="first", episode_id=_EPISODE_ID, location_id=_ROOM_ID,
        camera_id=_RIG_ID, camera_profile=_RIG_ID, duration_sec=0.1, degrade_seed=17,
    ))
    store.create(Shot(
        shot_id="second", episode_id=_EPISODE_ID, location_id=_ROOM_ID,
        camera_id=_RIG_ID, camera_profile=_RIG_ID, duration_sec=0.2, degrade_seed=97,
        event_sec=0.05,
        anomaly=LightFlicker(
            kind="light_flicker", light_name="Panel0", flicker_fraction=0.5,
            frequency_hz=8, timing=AnomalyTiming(duration_sec=0.1),
        ),
    ))
    store.create(Location(location_id="other-room"))
    store.create(Shot(
        shot_id="control", episode_id=_EPISODE_ID, location_id="other-room",
        camera_id=_RIG_ID, camera_profile=_RIG_ID, duration_sec=0.1,
    ))

    registry = AdapterRegistry({
        kind: _AdapterShim(kind, adapter, settings)
        for kind in (AdapterKind.SCENE, AdapterKind.RENDER)
    })
    runner = JobRunner(lambda _project_id: store, settings, queue_for=lambda _project_id: queue, adapters=registry)
    original_render = adapter.render_managed
    generations: list[GenerationRun] = []

    def capture_generation(*args: Any, **kwargs: Any):
        result = original_render(*args, **kwargs)
        generations.append(result.generation_run)
        return result

    monkeypatch.setattr(adapter, "render_managed", capture_generation)
    app = create_app(settings=settings, token=_TOKEN, job_runner=runner)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-Loopback-Token": _TOKEN}) as client:
            yield _Pipeline(root, store, runner, worker, client, generations)
    finally:
        runner.close()


def _approve_fixture_shot(store: ProjectStore, shot_id: str) -> Shot:
    shot, token = store.load_with_version(Shot, shot_id, episode_id=_EPISODE_ID)
    # The test stops after the master; stand in for the degradation/presentation stage.
    fixture_stages = {
        ShotStatus.DRAFT: ShotStatus.PLANNED,
        ShotStatus.PLANNED: ShotStatus.BLOCKING,
        ShotStatus.BLOCKING: ShotStatus.BUILDING,
        ShotStatus.BUILDING: ShotStatus.REVIEW,
    }
    while shot.status is not ShotStatus.REVIEW:
        store.update(shot.model_copy(update={"status": fixture_stages[shot.status]}), expected_version=token)
        shot, token = store.load_with_version(Shot, shot_id, episode_id=_EPISODE_ID)
    review = Review(
        review_id=f"review-{shot_id}", target_id=shot_id, target_version=shot.version,
        target_kind="shot", episode_id=_EPISODE_ID, actor="creator",
        created_at=datetime.now(UTC), approval_state=ShotStatus.APPROVED,
    )
    store.save(review, expected_version=0)
    return approve_shot(store, shot_id, episode_id=_EPISODE_ID, review_id=review.review_id, expected_version=token)


def _assert_reuse_evidence(pipeline: _Pipeline, plan: dict[str, Any], action: str, base_path: str, digest: str) -> None:
    metadata = json.loads(pipeline.root.read_text(pipeline.relative_path(plan["outputs"]["metadata"])))
    assert metadata["shared_base"]["key"] == plan["shared_base"]["key"]
    assert metadata["shared_base"]["action"] == action
    assert {action, base_path, digest}.issubset(pipeline.generations[-1].settings.values())


def test_episode_reuses_the_hashed_room_and_rig_then_invalidates_both_shots(
    pipeline: _Pipeline,
) -> None:
    first = pipeline.render("first")
    assert "shared_base" in first, "first location-bound master never planned a shared blend"
    built = first["shared_base"]
    assert built["input"] is None and built["output"]
    base_path = pipeline.relative_path(built["output"])
    base_bytes = pipeline.root.read_bytes(base_path)
    digest = _digest(base_bytes)
    _assert_reuse_evidence(pipeline, first, "built", base_path, digest)

    camera, camera_token = pipeline.store.load_with_version(CameraProfile, _RIG_ID)
    post_only = camera.model_copy(update={
        **camera.next_version(), "bitrate_kbps": (camera.bitrate_kbps or 800) + 100,
    })
    assert revise_camera_profile(
        pipeline.store, post_only, expected_version=camera_token,
    ) == ()

    second = pipeline.render("second")
    reopened = second["shared_base"]
    assert second["frame_end"] != first["frame_end"] and second["seed"] != first["seed"]
    assert reopened["key"] == built["key"]
    assert reopened["output"] is None
    assert pipeline.relative_path(reopened["input"]["path"]) == base_path
    assert reopened["input"]["sha256"] == digest
    assert reopened["scene"] == built["scene"]
    animated_light = next(light for light in second["scene"]["lights"] if light["name"] == "Panel0")
    base_light = next(light for light in reopened["scene"]["lights"] if light["name"] == "Panel0")
    assert animated_light["energy_keyframes"]
    assert not base_light.get("energy_keyframes")
    assert first["outputs"]["blend"] != second["outputs"]["blend"]
    _assert_reuse_evidence(pipeline, second, "reused", base_path, digest)

    for shot_id in ("first", "second", "control"):
        _approve_fixture_shot(pipeline.store, shot_id)
    old_masters = {
        shot_id: pipeline.store.list_shot_outputs(
            shot_id, episode_id=_EPISODE_ID, record_version=1, kind=ShotOutputKind.MASTER,
        )
        for shot_id in ("first", "second")
    }
    assert all(len(outputs) == 1 for outputs in old_masters.values())

    location, token = pipeline.store.load_with_version(Location, _ROOM_ID)
    revised = location.model_copy(update={**location.next_version(), "scene_look": "low_key"})
    revision = pipeline.client.post(f"/projects/p1/locations/{_ROOM_ID}/revise", json={
        "location": revised.model_dump(mode="json"),
        "expected_version": token,
    })
    assert revision.status_code == 200, revision.text
    assert set(revision.json()["invalidated"]) == {"first", "second"}
    for shot_id, outputs in old_masters.items():
        stale = pipeline.store.load(Shot, shot_id, episode_id=_EPISODE_ID)
        assert stale.status is ShotStatus.STALE
        assert stale.approval_review_id == f"review-{shot_id}"
        assert _digest(pipeline.root.read_bytes(outputs[0].relative_path)) == outputs[0].content_sha256
    assert pipeline.store.load(Shot, "control", episode_id=_EPISODE_ID).status is ShotStatus.APPROVED

    stale, shot_token = pipeline.store.load_with_version(Shot, "first", episode_id=_EPISODE_ID)
    successor = stale.model_copy(update={
        **stale.next_version(), "status": ShotStatus.DRAFT, "approval_review_id": None,
    })
    response = pipeline.client.post("/projects/p1/episodes/ep1/shots/first/revise", json={
        "shot": successor.model_dump(mode="json"), "expected_version": shot_token,
    })
    assert response.status_code == 200, response.text
    next_plan = pipeline.render("first")
    assert next_plan["shot_version"] == 2
    assert next_plan["shared_base"]["key"] != built["key"]
    assert next_plan["shared_base"]["input"] is None
    assert pipeline.root.read_bytes(base_path) == base_bytes
    assert pipeline.store.list_shot_outputs(
        "first", episode_id=_EPISODE_ID, record_version=1, kind=ShotOutputKind.MASTER,
    ) == old_masters["first"]


def test_changed_shared_blend_fails_before_launch_instead_of_rebuilding(pipeline: _Pipeline) -> None:
    first = pipeline.render("first")
    assert "shared_base" in first, "location-bound render did not create a shared blend"
    base_path = pipeline.relative_path(first["shared_base"]["output"])
    pipeline.root.write_bytes(base_path, b"BLENDER-v300 changed fixture")

    built_scene = pipeline.submit("second", "scene")
    assert built_scene.state == "completed", built_scene.model_dump()
    launches = len(pipeline.worker.plans)
    failed = pipeline.submit("second", "master")

    assert failed.state == "failed", failed.model_dump()
    assert failed.error_kind == JobErrorKind.CORRUPT_ARTIFACT
    assert failed.attempts == 2
    assert failed.run_id
    run_path = f"renders/second/v0001/{failed.run_id}"
    attempts = sorted(path.name for path in pipeline.root.resolve(run_path).iterdir() if path.is_dir())
    assert attempts == ["attempt-0001", "attempt-0002"]
    for attempt in attempts:
        failure = json.loads(pipeline.root.read_text(f"{run_path}/{attempt}/failure.json"))
        assert failure["error_kind"] == JobErrorKind.CORRUPT_ARTIFACT.value
        assert failure["retryable"] is True
        assert failure["job_id"] == failed.job_id
        assert failure["run_id"] == failed.run_id
    assert len(pipeline.worker.plans) == launches
    assert pipeline.store.list_shot_outputs("second", episode_id=_EPISODE_ID, kind=ShotOutputKind.MASTER) == ()
    assert pipeline.root.read_bytes(base_path) == b"BLENDER-v300 changed fixture"
