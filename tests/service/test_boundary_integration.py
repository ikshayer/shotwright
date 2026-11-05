"""Service contract exercised against real JSON storage and persisted Reviews."""
from datetime import UTC, datetime

import pytest

from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import Beat, Episode, Review, Script, Shot, StyleBible
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import approve_episode, approve_shot, approve_style_bible
from orchestrator.service.dependencies import revise_script
from orchestrator.store.json_store import ProjectStore, VersionConflict


@pytest.fixture
def store(tmp_path):
    return ProjectStore(ProjectRoot(tmp_path))


def persist_review(store, review_id, target_id, *, version=1, state=ShotStatus.APPROVED):
    record = Review(review_id=review_id, target_id=target_id, target_version=version,
                    created_at=datetime(2026, 11, 4, tzinfo=UTC), approval_state=state)
    store.save(record, expected_version=0)
    return record


def reviewable_shot(store, shot_id="s1", **overrides):
    record = Shot(**({"shot_id": shot_id, "episode_id": "ep1", "duration_sec": 5} | overrides))
    store.create(record)
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        update = {"status": status}
        if status is ShotStatus.BUILDING:
            update["framing_status"] = FramingStatus.APPROVED
        record = record.model_copy(update=update)
        store.update(record, expected_version=record.version)
    return record


def approve_record(store, record):
    review_id = f"r-{record.shot_id}"
    persist_review(store, review_id, record.shot_id, version=record.version)
    return approve_shot(store, record.shot_id, episode_id=record.episode_id,
                        review_id=review_id, expected_version=record.version)


def add_episode(store):
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)


def test_shot_approval_roundtrips_real_review_and_exact_binding(store, tmp_path):
    record = reviewable_shot(store)
    approved = approve_record(store, record)
    reopened = ProjectStore(ProjectRoot(tmp_path))
    loaded = reopened.load(Shot, "s1", episode_id="ep1")
    review = reopened.load(Review, loaded.approval_review_id)
    assert loaded == approved
    assert loaded.status is ShotStatus.APPROVED
    assert (review.target_id, review.target_version) == (loaded.shot_id, loaded.version)


def test_episode_cut_approval_roundtrips_persisted_review(store, tmp_path):
    add_episode(store)
    persist_review(store, "gate", "ep1", version=3)
    approve_episode(store, "ep1", cut_version=3, review_id="gate", expected_version=1)
    reopened = ProjectStore(ProjectRoot(tmp_path))
    loaded = reopened.load(Episode, "ep1")
    assert (loaded.approved_cut_version, loaded.approval_review_id) == (3, "gate")
    assert reopened.load(Review, "gate").target_version == 3


def test_style_approval_roundtrips_persisted_review(store, tmp_path):
    store.create(StyleBible(project_id="p1"))
    persist_review(store, "style", "p1")
    approve_style_bible(store, "p1", review_id="style", expected_version=1)
    reopened = ProjectStore(ProjectRoot(tmp_path))
    assert reopened.load(StyleBible, "p1").approval_review_id == "style"
    assert reopened.load(Review, "style").target_id == "p1"


@pytest.mark.parametrize("case", ["wrong-target", "wrong-version", "rejected", "missing"])
def test_invalid_persisted_review_does_not_mutate_shot(store, case):
    before = reviewable_shot(store)
    if case != "missing":
        persist_review(store, "invalid", "elsewhere" if case == "wrong-target" else "s1",
                       version=2 if case == "wrong-version" else 1,
                       state=ShotStatus.REJECTED if case == "rejected" else ShotStatus.APPROVED)
    with pytest.raises(JobError) as caught:
        approve_shot(store, "s1", episode_id="ep1", review_id="invalid", expected_version=1)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert caught.value.__cause__ is not None
    assert store.load(Shot, "s1", episode_id="ep1") == before


@pytest.mark.parametrize("target", ["shot", "episode", "style"])
def test_approval_expected_version_conflict_preserves_record(store, target):
    if target == "shot":
        before = reviewable_shot(store)
        persist_review(store, "approval", "s1")
        operation = lambda: approve_shot(store, "s1", episode_id="ep1", review_id="approval",
                                        expected_version=99)
        load = lambda: store.load(Shot, "s1", episode_id="ep1")
    elif target == "episode":
        add_episode(store)
        before = store.load(Episode, "ep1")
        persist_review(store, "approval", "ep1", version=3)
        operation = lambda: approve_episode(store, "ep1", cut_version=3, review_id="approval",
                                           expected_version=99)
        load = lambda: store.load(Episode, "ep1")
    else:
        before = StyleBible(project_id="p1")
        store.create(before)
        persist_review(store, "approval", "p1")
        operation = lambda: approve_style_bible(store, "p1", review_id="approval",
                                               expected_version=99)
        load = lambda: store.load(StyleBible, "p1")
    with pytest.raises(VersionConflict):
        operation()
    assert load() == before


def test_mapping_only_revision_retains_artifacts_history_and_revokes_gate_on_reopen(store, tmp_path):
    add_episode(store)
    artifact_root = ProjectRoot(tmp_path)
    artifact_root.write_bytes("audio/old.wav", b"retained master audio")
    old = approve_record(store, reviewable_shot(store, "old", audio_refs=["audio/old.wav"]))
    new = approve_record(store, reviewable_shot(store, "new"))
    unrelated = approve_record(store, reviewable_shot(store, "unrelated", beat_id="b2"))
    pending = reviewable_shot(store, "pending", beat_id="b1")
    persist_review(store, "gate", "ep1", version=4)
    approve_episode(store, "ep1", cut_version=4, review_id="gate", expected_version=1)
    before = Script(script_id="script1", episode_id="ep1", logline="Night shift",
                    beats=[Beat(beat_id="b1", target_sec=5, subtlety=2),
                           Beat(beat_id="b2", target_sec=5, subtlety=2)],
                    beat_to_shot={"b1": "old"})
    store.create(before)
    after = before.model_copy(update={**before.next_version(), "beat_to_shot": {"b1": "new"}})

    changed = revise_script(store, after, expected_version=1)

    assert set(changed) == {old.shot_id, new.shot_id}
    reopened = ProjectStore(ProjectRoot(tmp_path))
    assert reopened.versions(Script, "script1", episode_id="ep1") == [1, 2]
    assert reopened.load(Script, "script1", episode_id="ep1", version=1) == before
    assert reopened.load(Script, "script1", episode_id="ep1") == after
    for record in (old, new):
        stale = reopened.load(Shot, record.shot_id, episode_id="ep1")
        assert stale.status is ShotStatus.STALE
        assert stale.approval_review_id == record.approval_review_id
        assert stale.audio_refs == record.audio_refs
    assert reopened.load(Shot, "unrelated", episode_id="ep1") == unrelated
    assert reopened.load(Shot, "pending", episode_id="ep1") == pending
    assert artifact_root.resolve("audio/old.wav").read_bytes() == b"retained master audio"
    episode = reopened.load(Episode, "ep1")
    assert episode.approved_cut_version is None
    assert episode.approval_review_id is None
    assert reopened.load(Review, "gate").target_version == 4


def test_metadata_only_script_revision_is_persisted_without_staling_or_revoking(store):
    add_episode(store)
    approved = approve_record(store, reviewable_shot(store, dependencies=["b1"]))
    persist_review(store, "gate", "ep1", version=4)
    approve_episode(store, "ep1", cut_version=4, review_id="gate", expected_version=1)
    before = Script(script_id="script1", episode_id="ep1", logline="Night shift",
                    beats=[Beat(beat_id="b1", target_sec=5, subtlety=2)])
    store.create(before)
    after = before.model_copy(update=before.next_version())

    assert revise_script(store, after, expected_version=1) == ()
    assert store.load(Script, "script1", episode_id="ep1").version == 2
    assert store.load(Shot, "s1", episode_id="ep1") == approved
    assert store.load(Episode, "ep1").approval_review_id == "gate"
