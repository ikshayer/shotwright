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
def store(tmp_path, monkeypatch):
    result = ProjectStore(ProjectRoot(tmp_path))
    reviews: dict[str, Review] = {}
    load = result.load

    def load_with_reviews(cls, record_id=None, **kwargs):
        if cls is Review:
            try:
                return reviews[record_id]
            except KeyError as exc:
                from orchestrator.store.json_store import RecordNotFound
                raise RecordNotFound(f"Review {record_id!r} does not exist") from exc
        return load(cls, record_id, **kwargs)

    monkeypatch.setattr(result, "load", load_with_reviews)
    result.test_reviews = reviews
    return result


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
        store.update(shot, expected_version=shot.version)
    return shot


def test_approve_shot_binds_exact_approving_review(store):
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=4)
    store.create(shot)
    move_to_review(store, shot)
    store.test_reviews["r1"] = review()

    result = approve_shot(store, "s1", episode_id="ep1", review_id="r1", expected_version=1)

    assert result.status is ShotStatus.APPROVED
    assert result.approval_review_id == "r1"
    assert store.load(Shot, "s1", episode_id="ep1").approval_review_id == "r1"


@pytest.mark.parametrize("record", ["wrong_version", "rejected", "dangling"])
def test_approval_rejects_invalid_review(store, record):
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=4)
    store.create(shot)
    move_to_review(store, shot)
    if record == "wrong_version":
        store.test_reviews["r1"] = review(version=2)
    elif record == "rejected":
        store.test_reviews["r1"] = review(state=ShotStatus.REJECTED)
    review_id = "missing" if record == "dangling" else "r1"

    with pytest.raises(JobError) as error:
        approve_shot(store, "s1", episode_id="ep1", review_id=review_id, expected_version=1)

    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW


def test_approve_episode_review_targets_cut_version(store):
    episode = Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30)
    store.save(episode, expected_version=0)
    store.test_reviews["gate"] = review("gate", target="ep1", version=3)

    result = approve_episode(store, "ep1", cut_version=3, review_id="gate", expected_version=1)

    assert (result.approved_cut_version, result.approval_review_id) == (3, "gate")


def test_approve_style_bible_review_targets_its_version(store):
    bible = StyleBible(project_id="p1")
    store.create(bible)
    store.test_reviews["style"] = review("style", target="p1")

    result = approve_style_bible(store, "p1", review_id="style", expected_version=1)

    assert result.approval_review_id == "style"
