from __future__ import annotations

from datetime import UTC, datetime

from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    Beat,
    CameraProfile,
    Episode,
    Location,
    Review,
    Script,
    Shot,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.dependencies import (
    revise_camera_profile,
    revise_location,
    revise_script,
    revise_shot,
)
from orchestrator.store.json_store import ProjectStore


def add_episode(store, episode_id="ep1"):
    store.save(Episode(episode_id=episode_id, project_id="p1", number=1, duration_target_sec=30), expected_version=0)


def approved_shot(store, shot_id, *, episode_id="ep1", camera_id="cam1", dependencies=()):
    shot = Shot(
        shot_id=shot_id, episode_id=episode_id, duration_sec=5, camera_id=camera_id,
        dependencies=list(dependencies),
    )
    store.create(shot)
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        update = {"status": status}
        if status is ShotStatus.BUILDING:
            update["framing_status"] = FramingStatus.APPROVED
        shot = shot.model_copy(update=update)
        store.update(shot, expected_version=store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id)[1])
    review = Review(
        review_id=f"review-{shot_id}", target_id=shot_id, target_version=1,
        target_kind="shot", episode_id=episode_id, actor="creator",
        created_at=datetime.now(UTC), approval_state=ShotStatus.APPROVED,
    )
    store.save(review, expected_version=0)
    store.update(shot.model_copy(update={"status": ShotStatus.APPROVED, "approval_review_id": review.review_id}), expected_version=store.load_with_version(Shot, shot_id, episode_id=episode_id)[1])


def test_post_only_profile_change_invalidates_nothing(tmp_path):
    store = ProjectStore(ProjectRoot(tmp_path))
    add_episode(store)
    approved_shot(store, "s1", camera_id="cam1")
    store.create(CameraProfile(camera_id="cam1", profile_name="CCTV"))
    before = store.load(CameraProfile, "cam1")
    revised = before.model_copy(update={**before.next_version(), "bitrate_kbps": 900})

    changed = revise_camera_profile(store, revised, expected_version=1)

    assert changed == ()
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.APPROVED


def test_rebuild_profile_stales_only_approved_shots_on_that_camera(tmp_path):
    store = ProjectStore(ProjectRoot(tmp_path))
    add_episode(store)
    approved_shot(store, "cam-hit", camera_id="cam1")
    approved_shot(store, "cam-other", camera_id="cam2")
    store.create(CameraProfile(camera_id="cam1", profile_name="CCTV"))
    before = store.load(CameraProfile, "cam1")
    revised = before.model_copy(update={**before.next_version(), "field_of_view_mm": 16})

    changed = revise_camera_profile(store, revised, expected_version=1)

    assert changed == ("cam-hit",)
    assert store.load(Shot, "cam-hit", episode_id="ep1").status is ShotStatus.STALE
    assert store.load(Shot, "cam-other", episode_id="ep1").status is ShotStatus.APPROVED


def test_rejected_shot_successor_draft_revokes_gate_c(tmp_path):
    store = ProjectStore(ProjectRoot(tmp_path))
    add_episode(store)
    episode, token = store.load_with_version(Episode, "ep1")
    gate = episode.model_copy(update={"approved_cut_version": 4,
                                      "approved_cut_output_id": "approved-cut",
                                      "approval_review_id": "gate-c"})
    store.save(gate, expected_version=token)
    rejected = Shot(shot_id="s1", episode_id="ep1", duration_sec=5)
    store.create(rejected)
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW, ShotStatus.REJECTED):
        update = {"status": status}
        if status is ShotStatus.BUILDING:
            update["framing_status"] = FramingStatus.APPROVED
        rejected = rejected.model_copy(update=update)
        store.update(rejected, expected_version=store.load_with_version(Shot, rejected.shot_id, episode_id=rejected.episode_id)[1])
    successor = rejected.model_copy(update={**rejected.next_version(), "status": ShotStatus.DRAFT})

    revise_shot(store, successor, expected_version=store.load_with_version(Shot, "s1", episode_id="ep1")[1])

    after = store.load(Episode, "ep1")
    assert after.approved_cut_version is None
    assert after.approved_cut_output_id is None
    assert after.approval_review_id is None
    assert store.load(Shot, "s1", episode_id="ep1", version=1).status is ShotStatus.REJECTED
    assert store.load(Shot, "s1", episode_id="ep1", version=2).status is ShotStatus.DRAFT


def test_changed_script_beat_stales_only_dependent_approved_shot(tmp_path):
    store = ProjectStore(ProjectRoot(tmp_path))
    add_episode(store)
    approved_shot(store, "dependent", dependencies=["beat-a"])
    approved_shot(store, "unrelated", dependencies=["beat-b"])
    script = Script(
        script_id="script1", episode_id="ep1", logline="night shift",
        beats=[Beat(beat_id="beat-a", target_sec=2, subtlety=2)],
    )
    store.create(script)
    revised = script.model_copy(
        update={**script.next_version(), "beats": [Beat(beat_id="beat-a", target_sec=3, subtlety=2)]}
    )

    changed = revise_script(store, revised, expected_version=1)

    assert changed == ("dependent",)
    assert store.load(Shot, "dependent", episode_id="ep1").status is ShotStatus.STALE
    assert store.load(Shot, "unrelated", episode_id="ep1").status is ShotStatus.APPROVED


def test_location_change_stales_only_dependent_approved_shot(tmp_path):
    store = ProjectStore(ProjectRoot(tmp_path))
    add_episode(store)
    approved_shot(store, "dependent", dependencies=["location-a"])
    approved_shot(store, "unrelated", dependencies=["location-b"])
    location = Location(location_id="location-a", geometry_provenance="initial")
    store.create(location)
    revised = location.model_copy(update={**location.next_version(), "geometry_provenance": "revised"})

    changed = revise_location(store, revised, expected_version=1)

    assert changed == ("dependent",)
    assert store.load(Shot, "dependent", episode_id="ep1").status is ShotStatus.STALE
    assert store.load(Shot, "unrelated", episode_id="ep1").status is ShotStatus.APPROVED
