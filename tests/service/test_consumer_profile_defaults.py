from __future__ import annotations

import hashlib
from fractions import Fraction

from orchestrator.adapters.postprofile.profile import build_filter_graph, validate_profile
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    PostProfile,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.compose import compose_degrade_request
from orchestrator.service.dependencies import create_project
from orchestrator.store.json_store import ProjectStore


def _seed_consumer_shot(tmp_path):
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    project = create_project(store, Project(project_id="p1", title="Consumer profile"))
    store.save(
        Episode(episode_id="ep1", project_id=project.project_id, number=1, duration_target_sec=30),
        expected_version=0,
    )
    shot = Shot(
        shot_id="shot-1",
        episode_id="ep1",
        duration_sec=5,
        camera_profile="consumer-2002",
        post_profile="consumer-2002",
    )
    store.create(shot)
    master_relative = "renders/shot-1/v0001/seed/master.mkv"
    master_bytes = b"fixture current master"
    root.write_bytes(master_relative, master_bytes)
    store.append_shot_output(
        ShotOutput(
            output_id="seed-master",
            episode_id=shot.episode_id,
            shot_id=shot.shot_id,
            record_version=shot.version,
            kind=ShotOutputKind.MASTER,
            relative_path=master_relative,
            content_sha256=hashlib.sha256(master_bytes).hexdigest(),
            engine_id="BLENDER_EEVEE",
            engine_build="fixture-build",
        ),
        expected_version=0,
    )
    return store, shot


def test_project_seeds_consumer_profile_as_unmeasured_sensor_noise_without_overlay(tmp_path):
    store, _shot = _seed_consumer_shot(tmp_path)
    camera = store.load(CameraProfile, "consumer-2002")
    profile = store.load(PostProfile, "consumer-2002")

    assert camera.profile_name == "consumer-2002"
    assert camera.camera_mount == "handheld"
    assert camera.storage_resolution == (640, 480)
    assert camera.display_aspect_ratio == "4:3"
    assert camera.fps == 30.0
    assert camera.analog_artifacts.model_dump(exclude_none=True) == {}
    assert camera.block_artifacts == 0.0
    assert profile.storage_resolution == (640, 480)
    assert profile.sample_aspect_ratio == "1/1"
    assert profile.output_fps == 30
    assert profile.overlay is None
    assert not any(stage.filter == "drawtext" for stage in profile.stages)
    noise, = [stage for stage in profile.stages if stage.filter == "noise"]
    assert noise.params == {"alls": 24, "allf": "t+u"}
    assert noise.seed_source == "shot_degrade_seed"
    assert not any(stage.filter in {"blockdct", "mpdecimate"} for stage in profile.stages)
    assert "UNMEASURED PLACEHOLDER" in profile.provenance
    assert "alls=24" in profile.provenance
    assert "output_fps=30" in profile.provenance


def test_shot_profile_selection_composes_without_clock_or_font(tmp_path):
    store, shot = _seed_consumer_shot(tmp_path)

    request = compose_degrade_request(
        store, shot_id=shot.shot_id, episode_id=shot.episode_id, run_id="run-01",
    )
    validated = validate_profile(
        request.profile,
        request.camera_profile,
        master_frames=300,
        master_rate=Fraction(60, 1),
    )
    graph = build_filter_graph(
        validated,
        shot_id=shot.shot_id,
        degrade_seed=shot.degrade_seed,
        font_path=request.font_relative,
    )

    assert request.camera_profile.camera_id == "consumer-2002"
    assert request.profile.identity.name == "consumer-2002"
    assert request.profile.overlay is None
    assert request.font_relative is None
    assert "drawtext=" not in graph
    assert "noise=alls=24:allf=t+u" in graph
