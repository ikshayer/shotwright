from __future__ import annotations

from datetime import UTC, datetime

import pytest

from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import Episode, Review, Shot, StyleBible
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import approve_episode, approve_shot, approve_style_bible
from orchestrator.store.json_store import ProjectStore


@pytest.fixture
def store(tmp_path):
    return ProjectStore(ProjectRoot(tmp_path))


def review(review_id="r1", *, target="s1", version=1, state=ShotStatus.APPROVED):
    return Review(
        review_id=review_id, target_id=target, target_version=version,
        created_at=datetime.now(UTC), approval_state=state,
    )


def move_to_review(store, shot):
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        update = {"status": status}
        if status is ShotStatus.BUILDING:
            update["framing_status"] = FramingStatus.APPROVED
        shot = shot.model_copy(update=update)
        store.update(shot, expected_version=store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id)[1])
    return shot


def test_approve_shot_binds_exact_approving_review(store):
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=4)
    store.create(shot)
    move_to_review(store, shot)
    store.save(review(), expected_version=0)

    result = approve_shot(store, "s1", episode_id="ep1", review_id="r1", expected_version=store.load_with_version(Shot, "s1", episode_id="ep1")[1])

    assert result.status is ShotStatus.APPROVED
    assert result.approval_review_id == "r1"
    assert store.load(Shot, "s1", episode_id="ep1").approval_review_id == "r1"


@pytest.mark.parametrize("record", ["wrong_version", "rejected", "dangling"])
def test_approval_rejects_invalid_review(store, record):
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=4)
    store.create(shot)
    move_to_review(store, shot)
    if record == "wrong_version":
        store.save(review(version=2), expected_version=0)
    elif record == "rejected":
        store.save(review(state=ShotStatus.REJECTED), expected_version=0)
    review_id = "missing" if record == "dangling" else "r1"

    with pytest.raises(JobError) as error:
        approve_shot(store, "s1", episode_id="ep1", review_id=review_id, expected_version=store.load_with_version(Shot, "s1", episode_id="ep1")[1])

    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW


def test_approve_episode_review_targets_cut_version(store):
    episode = Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30)
    store.save(episode, expected_version=0)
    store.save(review("gate", target="ep1", version=3), expected_version=0)

    result = approve_episode(store, "ep1", cut_version=3, review_id="gate", expected_version=1)

    assert (result.approved_cut_version, result.approval_review_id) == (3, "gate")


def test_approve_style_bible_review_targets_its_version(store):
    bible = StyleBible(project_id="p1")
    store.create(bible)
    store.save(review("style", target="p1"), expected_version=0)

    result = approve_style_bible(store, "p1", review_id="style", expected_version=1)

    assert result.approval_review_id == "style"
