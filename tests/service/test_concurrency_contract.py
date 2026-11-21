"""Independent SVC-01..03 tests from CONCURRENCY_CONTRACT.md."""
from __future__ import annotations

import json
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    Beat,
    CameraProfile,
    Episode,
    EpisodeOutput,
    Location,
    Review,
    Script,
    Shot,
    StyleBible,
    Timeline,
)
from orchestrator.domain.invalidation import revision_impact
from orchestrator.domain.state import (
    ReviewApprovalError,
    check_cut_approval,
    check_dependency_freshness,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import approve_episode, approve_shot, approve_style_bible
from orchestrator.service.dependencies import (
    resume_invalidations,
    revise_camera_profile,
    revise_location,
    revise_script,
    revise_shot,
)
from orchestrator.store.json_store import CommandInProgress, ProjectStore, VersionConflict


@pytest.fixture
def project(tmp_path):
    root = ProjectRoot(tmp_path)
    return root, ProjectStore(root)


def token(store, cls, name, episode_id=None):
    return store.load_with_version(cls, name, episode_id=episode_id)[1]


def persist_review(store, name, target, version=1, *, target_kind="shot", episode_id="ep1"):
    store.save(Review(review_id=name, target_id=target, target_version=version,
                      target_kind=target_kind, episode_id=episode_id, actor="creator",
                      created_at=datetime(2026, 11, 4, tzinfo=UTC),
                      approval_state=ShotStatus.APPROVED), expected_version=0)


def add_episode(store, name="ep1"):
    store.save(Episode(episode_id=name, project_id="p1", number=1,
                       duration_target_sec=30), expected_version=0)


def add_cut_output(store, episode_id="ep1", *, timeline_version=3):
    timeline = Timeline(episode_id=episode_id, entries=[])
    for expected_token in range(timeline_version):
        store.save(timeline, expected_version=expected_token)
    timeline = store.load(Timeline, episode_id)
    output_id = "cut-" + episode_id
    artifact = f"test cut {output_id}".encode()
    relative_path = f"episodes/{episode_id}/outputs/cuts/{output_id}.mp4"
    store._root.write_bytes(relative_path, artifact)
    receipt = EpisodeOutput(
        output_id=output_id, episode_id=episode_id,
        timeline_store_version=timeline_version, timeline_sha256=timeline.content_sha256,
        relative_path=relative_path, content_sha256=hashlib.sha256(artifact).hexdigest(),
        delivery_profile="test", storage_resolution=(64, 48), output_fps=12,
        sample_aspect_ratio="1/1", ffmpeg_identity="test",
    )
    store.append_episode_output(receipt, expected_version=0)
    return receipt


def reviewable(store, name="s1", episode_id="ep1", **fields):
    shot = Shot(shot_id=name, episode_id=episode_id, duration_sec=5, **fields)
    store.create(shot)
    for state in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        shot = shot.model_copy(update={"status": state, "framing_status": FramingStatus.APPROVED})
        store.update(shot, expected_version=token(store, Shot, name, episode_id))
    persist_review(store, "r-" + episode_id + "-" + name, name, episode_id=episode_id)
    return shot


def approve(store, name="s1", episode_id="ep1"):
    return approve_shot(store, name, episode_id=episode_id,
                        review_id="r-" + episode_id + "-" + name,
                        expected_version=token(store, Shot, name, episode_id))


def gate(store, episode_id="ep1"):
    cut = add_cut_output(store, episode_id)
    persist_review(store, "gate-" + episode_id, episode_id, 3,
                   target_kind="episode_cut", episode_id=episode_id)
    approve_episode(store, episode_id, cut_output_id=cut.output_id,
                    review_id="gate-" + episode_id,
                    expected_version=token(store, Episode, episode_id))


def location_successor(store):
    before = store.load(Location, "hall")
    return before.model_copy(update={**before.next_version(), "geometry_provenance": "after"})


def revise_hall(store, episodes=("ep1",)):
    return revise_location(store, location_successor(store),
                           expected_version=token(store, Location, "hall"))


def setup_cascade(root, store, *, two_episodes=False):
    episodes = ("ep1", "ep2") if two_episodes else ("ep1",)
    root.write_bytes("audio/master.wav", b"retain this artifact")
    for ep in episodes:
        add_episode(store, ep)
        for name in ("s1", "s2"):
            reviewable(store, name, ep, dependencies=["hall"], audio_refs=["audio/master.wav"])
            approve(store, name, ep)
        gate(store, ep)
    store.create(Location(location_id="hall", geometry_provenance="before"))
    return episodes


def assert_safe(store, episodes):
    for ep in episodes:
        shots = [store.load(Shot, name, episode_id=ep) for name in store.ids(Shot, episode_id=ep)]
        if any(s.status in {ShotStatus.STALE, ShotStatus.DRAFT} for s in shots):
            episode = store.load(Episode, ep)
            assert (episode.approved_cut_version, episode.approved_cut_output_id,
                    episode.approval_review_id) == (None, None, None)


def tree_bytes(root):
    return {p.relative_to(root.path).as_posix(): p.read_bytes()
            for p in root.path.rglob("*") if p.is_file()}


def test_lifecycle_tokens_preserve_notes_and_reject_stale_approval(project):
    root, store = project
    add_episode(store)
    reviewable(store)
    first, original_token = store.load_with_version(Shot, "s1", episode_id="ep1")
    second, second_token = store.load_with_version(Shot, "s1", episode_id="ep1")
    assert original_token == second_token == 5
    store.update(first.model_copy(update={"approval_notes": "creator feedback"}), expected_version=original_token)
    with pytest.raises(VersionConflict) as caught:
        approve_shot(store, "s1", episode_id="ep1", review_id="r-ep1-s1", expected_version=second_token)
    assert (caught.value.expected, caught.value.actual) == (5, 6)
    assert caught.value.current.approval_notes == "creator feedback"
    with pytest.raises(VersionConflict):
        store.update(second, expected_version=second_token)
    reopened = ProjectStore(ProjectRoot(root.path))
    current, current_token = reopened.load_with_version(Shot, "s1", episode_id="ep1")
    assert (current.version, current_token, current.approval_notes) == (1, 6, "creator feedback")
    approved = approve(reopened)
    assert approved.version == 1
    assert reopened.load(Review, approved.approval_review_id).target_version == 1
    assert token(reopened, Shot, "s1", "ep1") == 7


def test_every_write_advances_token_and_all_snapshots_are_immutable(project):
    root, store = project
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=5)
    store.create(shot)
    retained = tree_bytes(root)
    for expected in (1, 2, 3):
        store.update(shot, expected_version=expected)
        assert token(store, Shot, "s1", "ep1") == expected + 1
        now = tree_bytes(root)
        assert len(now) == len(retained) + 1
        assert all(now[path] == data for path, data in retained.items())
        retained = now
    successor = shot.model_copy(update=shot.next_version())
    store.revise(successor, expected_version=4)
    assert store.load_with_version(Shot, "s1", episode_id="ep1") == (successor, 5)
    assert store.load_with_version(Shot, "s1", episode_id="ep1", version=1) == (shot, 4)
    with pytest.raises(JobError):
        store.update(shot, expected_version=5)
    with pytest.raises(VersionConflict) as caught:
        store.create(shot)
    assert (caught.value.expected, caught.value.actual) == (0, 5)
    assert all(tree_bytes(root)[path] == data for path, data in retained.items())
    assert not list(root.path.rglob("*.tmp"))


def test_failed_or_rejected_write_does_not_consume_token(project, monkeypatch):
    root, store = project
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=5)
    store.create(shot)
    with pytest.raises(JobError):
        store.update(shot.model_copy(update={"narrative_beat": "illegal content"}), expected_version=1)
    assert token(store, Shot, "s1", "ep1") == 1
    before = tree_bytes(root)
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("injected durable rename failure")
        patch.setattr(store, "_replace", fail)
        with pytest.raises(JobError):
            store.update(shot, expected_version=1)
    assert tree_bytes(root) == before
    assert token(ProjectStore(root), Shot, "s1", "ep1") == 1
    store.update(shot, expected_version=1)
    assert token(store, Shot, "s1", "ep1") == 2


def test_legacy_versioned_and_unversioned_tokens(project):
    root, store = project
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=5, version=3, supersedes=2)
    root.write_text("episodes/ep1/shots/s1/v0003.json", shot.model_dump_json())
    episode = Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30)
    root.write_text("episodes/ep1/episode.json", episode.model_dump_json())
    assert store.load_with_version(Shot, "s1", episode_id="ep1") == (shot, 3)
    assert store.load_with_version(Episode, "ep1") == (episode, 1)
    store.update(shot, expected_version=3)
    assert token(ProjectStore(root), Shot, "s1", "ep1") == 4
    assert store.save(episode, expected_version=1) == 2
    assert store.save(episode, expected_version=2) == 3
    with pytest.raises(VersionConflict):
        store.save(episode, expected_version=2)


@pytest.mark.parametrize("bad", [0, -1, True, "1", None])
def test_malformed_mutation_metadata_fails_closed(project, bad):
    root, store = project
    payload = Location(location_id="hall").model_dump(mode="json")
    payload["_store_version"] = bad
    root.write_text("locations/hall/v0001.json", json.dumps(payload))
    with pytest.raises(JobError) as caught:
        store.load_with_version(Location, "hall")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


@pytest.mark.parametrize("state", list(ShotStatus))
def test_domain_impact_includes_all_states_and_deduplicates_by_episode_and_id(state):
    before = Location(location_id="hall")
    after = before.model_copy(update={**before.next_version(), "geometry_provenance": "changed"})
    fields = {"status": state, "approval_review_id": "r" if state is ShotStatus.APPROVED else None}
    direct = Shot(shot_id="same", episode_id="ep1", duration_sec=5, location_id="hall", **fields)
    dependency = Shot(shot_id="same", episode_id="ep2", duration_sec=5, dependencies=["hall"], **fields)
    miss = Shot(shot_id="miss", episode_id="ep1", duration_sec=5)
    snapshot = direct.model_dump()
    assert revision_impact(before, after, [dependency, direct, dependency, miss]) == (dependency, direct)
    assert direct.model_dump() == snapshot


def test_domain_impact_script_mapping_profile_split_and_shot_revision():
    old = Shot(shot_id="old", episode_id="ep1", duration_sec=5)
    new = Shot(shot_id="new", episode_id="ep1", duration_sec=5)
    foreign = old.model_copy(update={"episode_id": "ep2"})
    before = Script(script_id="sc", episode_id="ep1", logline="night", beats=[Beat(beat_id="b", target_sec=5, subtlety=2)], beat_to_shot={"b": "old"})
    after = before.model_copy(update={**before.next_version(), "beat_to_shot": {"b": "new"}})
    assert revision_impact(before, after, [old, foreign, new, old]) == (old, new)
    assert revision_impact(before, before.model_copy(update=before.next_version()), [old]) == ()
    camera = CameraProfile(camera_id="cam", profile_name="CCTV")
    linked = old.model_copy(update={"camera_id": "cam"})
    assert revision_impact(camera, camera.model_copy(update={**camera.next_version(), "bitrate_kbps": 900}), [linked]) == ()
    assert revision_impact(camera, camera.model_copy(update={**camera.next_version(), "exposure_ev": 2}), [linked]) == (linked,)
    assert revision_impact(old, old.model_copy(update=old.next_version()), [new, old, foreign, old]) == (old,)
    for wrong in (Location(location_id="elsewhere"), camera):
        with pytest.raises(ValueError):
            revision_impact(Location(location_id="hall"), wrong, [])


def test_pure_dependency_fence_matches_exact_episode_shot_and_version():
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=5)
    for evidence in ([("ep2", "s1", 1)], [("ep1", "s2", 1)], [("ep1", "s1", 2)]):
        check_dependency_freshness(shot, invalidated=iter(evidence))
    with pytest.raises(ReviewApprovalError):
        check_dependency_freshness(shot, invalidated=iter([("ep1", "s1", 1)]))
    check_dependency_freshness(shot.model_copy(update=shot.next_version()), invalidated=[("ep1", "s1", 1)])


@pytest.mark.parametrize("state", [ShotStatus.DRAFT, ShotStatus.STALE])
def test_pure_cut_guard_rejects_current_draft_or_stale(state):
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=5, status=state)
    with pytest.raises(ReviewApprovalError):
        check_cut_approval(pending=False, shots=iter([shot]))
    with pytest.raises(ReviewApprovalError):
        check_cut_approval(pending=True, shots=[])
    check_cut_approval(pending=False, shots=[])


def inject_phase(patch, store, phase):
    method = {"plan": "record_cascade", "gate1": "save", "gate2": "save", "upstream": "revise", "stale1": "update", "stale2": "update", "complete": "complete_cascade"}[phase]
    original = getattr(store, method)
    counter = 0

    def injected(*args, **kwargs):
        nonlocal counter
        entity = args[0]
        applies = phase in {"plan", "upstream", "complete"}
        if phase.startswith("gate"):
            applies = isinstance(entity, Episode) and entity.approved_cut_version is None
        if phase.startswith("stale"):
            applies = isinstance(entity, Shot) and entity.status is ShotStatus.STALE
        if applies:
            counter += 1
            fail_at = 2 if phase in {"gate2", "stale2"} else 1
            if counter == fail_at:
                raise OSError("injected " + phase + " failure")
        return original(*args, **kwargs)
    patch.setattr(store, method, injected)


@pytest.mark.parametrize("phase", ["plan", "gate1", "gate2", "upstream", "stale1", "stale2", "complete"])
def test_fault_each_cascade_phase_reopens_and_recovers_once(project, monkeypatch, phase):
    root, store = project
    episodes = setup_cascade(root, store, two_episodes=True)
    original = tree_bytes(root)
    with monkeypatch.context() as patch:
        inject_phase(patch, store, phase)
        with pytest.raises(JobError) as caught:
            revise_hall(store, episodes)
    assert caught.value.__cause__ is not None
    reopened = ProjectStore(ProjectRoot(root.path))
    assert_safe(reopened, episodes)
    if phase == "plan":
        assert reopened.cascades(pending_only=True) == ()
        assert tree_bytes(root) == original
        changed = revise_hall(reopened, episodes)
        # Existing return shapes name IDs; journal evidence disambiguates episodes.
        assert set(changed) == {"s1", "s2"}
    else:
        pending, = reopened.cascades(pending_only=True)
        assert pending["cascade_id"] in str(caught.value)
        assert "resume_invalidations" in str(caught.value)
        assert pending["episode_ids"] == ["ep1", "ep2"]
        assert {(s["episode_id"], s["shot_id"], s["version"]) for s in pending["affected"]} == {(ep, name, 1) for ep in episodes for name in ("s1", "s2")}
        if phase.startswith("gate"):
            assert reopened.load(Location, "hall").version == 1
            assert all(reopened.load(Shot, name, episode_id=ep).status is ShotStatus.APPROVED for ep in episodes for name in ("s1", "s2"))
        with monkeypatch.context() as patch:
            inject_phase(patch, reopened, "complete")
            with pytest.raises(JobError):
                resume_invalidations(reopened)
        assert_safe(reopened, episodes)
        assert reopened.cascades(pending_only=True)[0]["cascade_id"] == pending["cascade_id"]
        fresh = ProjectStore(ProjectRoot(root.path))
        assert resume_invalidations(fresh) == (pending["cascade_id"],)
        assert resume_invalidations(fresh) == ()
    final = ProjectStore(ProjectRoot(root.path))
    assert final.cascades(pending_only=True) == ()
    assert len(final.cascades()) == 1
    assert final.versions(Location, "hall") == [1, 2]
    assert final.load(Location, "hall", version=1).geometry_provenance == "before"
    assert final.load(Location, "hall").geometry_provenance == "after"
    for ep in episodes:
        for name in ("s1", "s2"):
            shot = final.load(Shot, name, episode_id=ep)
            assert (shot.version, shot.status, shot.audio_refs) == (1, ShotStatus.STALE, ["audio/master.wav"])
            assert shot.approval_review_id == "r-" + ep + "-" + name
    assert root.resolve("audio/master.wav").read_bytes() == b"retain this artifact"
    assert all(tree_bytes(root)[path] == data for path, data in original.items() if path.endswith("v0001.json"))
    assert not list(root.path.rglob("*.tmp"))


def test_pending_plan_blocks_every_approval_and_revision_without_writing(project, monkeypatch):
    root, store = project
    setup_cascade(root, store)
    reviewable(store, "unrelated")
    store.create(StyleBible(project_id="p1"))
    persist_review(store, "style", "p1", target_kind="style_bible", episode_id=None)
    script = Script(script_id="sc", episode_id="ep1", logline="night")
    camera = CameraProfile(camera_id="cam", profile_name="CCTV")
    store.create(script)
    store.create(camera)
    with monkeypatch.context() as patch:
        inject_phase(patch, store, "stale2")
        with pytest.raises(JobError):
            revise_hall(store)
    store = ProjectStore(root)
    shot = store.load(Shot, "unrelated", episode_id="ep1")
    calls = [
        lambda: approve(store, "unrelated"),
        lambda: approve_episode(store, "ep1", cut_output_id="cut-ep1",
                                review_id="gate-ep1",
                                expected_version=token(store, Episode, "ep1")),
        lambda: approve_style_bible(store, "p1", review_id="style", expected_version=token(store, StyleBible, "p1")),
        lambda: revise_hall(store),
        lambda: revise_script(store, script.model_copy(update=script.next_version()), expected_version=1),
        lambda: revise_camera_profile(store, camera.model_copy(update=camera.next_version()), expected_version=1),
        lambda: revise_shot(store, shot.model_copy(update={**shot.next_version(), "status": ShotStatus.DRAFT}), expected_version=token(store, Shot, "unrelated", "ep1")),
    ]
    before = tree_bytes(root)
    for call in calls:
        with pytest.raises(JobError) as caught:
            call()
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        assert "resume_invalidations" in str(caught.value)
        assert tree_bytes(root) == before


def test_completed_plan_fences_review_and_draft_until_successor_rebuild(project):
    root, store = project
    add_episode(store)
    reviewable(store, location_id="hall")
    draft = Shot(shot_id="draft", episode_id="ep1", duration_sec=5, location_id="hall")
    store.create(draft)
    store.create(Location(location_id="hall"))
    assert revise_hall(store) == ()
    reopened = ProjectStore(root)
    plan, = reopened.cascades()
    assert {(s["shot_id"], s["version"]) for s in plan["affected"]} == {("s1", 1), ("draft", 1)}
    assert reopened.cascades(pending_only=True) == ()
    with pytest.raises(JobError) as caught:
        approve(reopened)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert reopened.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW
    for state in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        draft = draft.model_copy(update={"status": state, "framing_status": FramingStatus.APPROVED})
        reopened.update(draft, expected_version=token(reopened, Shot, "draft", "ep1"))
    persist_review(reopened, "r-ep1-draft", "draft", episode_id="ep1")
    with pytest.raises(JobError):
        approve(reopened, "draft")
    old = reopened.load(Shot, "s1", episode_id="ep1")
    successor = old.model_copy(update={**old.next_version(), "status": ShotStatus.DRAFT, "approval_review_id": None, "audio_refs": []})
    revise_shot(reopened, successor, expected_version=token(reopened, Shot, "s1", "ep1"))
    for state in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        successor = successor.model_copy(update={"status": state, "framing_status": FramingStatus.APPROVED})
        reopened.update(successor, expected_version=token(reopened, Shot, "s1", "ep1"))
    persist_review(reopened, "fresh-review", "s1", 2, episode_id="ep1")
    approved = approve_shot(reopened, "s1", episode_id="ep1", review_id="fresh-review", expected_version=token(reopened, Shot, "s1", "ep1"))
    assert (approved.version, approved.status) == (2, ShotStatus.APPROVED)


@pytest.mark.parametrize("state", [ShotStatus.DRAFT, ShotStatus.STALE])
def test_cut_approval_uses_all_current_episode_shots(project, state):
    _, store = project
    add_episode(store)
    if state is ShotStatus.DRAFT:
        store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=5))
    else:
        reviewable(store)
        approved = approve(store)
        store.update(approved.model_copy(update={"status": ShotStatus.STALE}), expected_version=token(store, Shot, "s1", "ep1"))
    cut = add_cut_output(store, timeline_version=3)
    persist_review(store, "gate", "ep1", 3, target_kind="episode_cut", episode_id="ep1")
    before = store.load_with_version(Episode, "ep1")
    with pytest.raises(JobError) as caught:
        approve_episode(store, "ep1", cut_output_id=cut.output_id,
                        review_id="gate", expected_version=before[1])
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load_with_version(Episode, "ep1") == before


def test_shot_successor_revokes_gate_before_append_and_recovers(project, monkeypatch):
    root, store = project
    add_episode(store)
    reviewable(store)
    old = store.load(Shot, "s1", episode_id="ep1")
    store.update(old.model_copy(update={"status": ShotStatus.REJECTED}), expected_version=token(store, Shot, "s1", "ep1"))
    gate(store)
    before = store.load(Shot, "s1", episode_id="ep1")
    after = before.model_copy(update={**before.next_version(), "status": ShotStatus.DRAFT})
    with monkeypatch.context() as patch:
        inject_phase(patch, store, "gate1")
        with pytest.raises(JobError):
            revise_shot(store, after, expected_version=token(store, Shot, "s1", "ep1"))
    reopened = ProjectStore(root)
    assert reopened.versions(Shot, "s1", episode_id="ep1") == [1]
    assert reopened.load(Episode, "ep1").approved_cut_version == 3
    pending, = reopened.cascades(pending_only=True)
    assert pending["entity_type"] == "Shot"
    assert resume_invalidations(reopened) == (pending["cascade_id"],)
    assert reopened.load(Shot, "s1", episode_id="ep1") == after
    assert reopened.load(Shot, "s1", episode_id="ep1", version=1) == before
    assert_safe(reopened, ["ep1"])
    assert resume_invalidations(reopened) == ()


def test_revision_validation_happens_before_plan_or_gate_write(project):
    root, store = project
    setup_cascade(root, store)
    before = tree_bytes(root)
    with pytest.raises(VersionConflict):
        revise_location(store, location_successor(store), expected_version=999)
    assert tree_bytes(root) == before
    malformed = location_successor(store).model_copy(update={"version": 4})
    with pytest.raises(JobError):
        revise_location(store, malformed, expected_version=1)
    assert tree_bytes(root) == before


def test_missing_affected_episode_rejects_before_recording_plan(project):
    root, store = project
    reviewable(store, dependencies=["hall"])
    store.create(Location(location_id="hall"))
    before = tree_bytes(root)
    with pytest.raises((JobError, LookupError)):
        revise_hall(store)
    assert store.cascades() == ()
    assert tree_bytes(root) == before


def test_post_only_profile_has_no_fence_or_gate_revocation(project):
    _, store = project
    add_episode(store)
    reviewable(store, camera_id="cam")
    gate(store)
    camera = CameraProfile(camera_id="cam", profile_name="CCTV")
    store.create(camera)
    after = camera.model_copy(update={**camera.next_version(), "bitrate_kbps": 900})
    assert revise_camera_profile(store, after, expected_version=1) == ()
    assert all(not p["affected"] for p in store.cascades())
    assert store.load(Episode, "ep1").approved_cut_version == 3
    assert approve(store).status is ShotStatus.APPROVED


def test_script_mapping_plan_fences_review_and_draft_and_revokes_gate(project):
    _, store = project
    add_episode(store)
    reviewable(store, "old")
    store.create(Shot(shot_id="new", episode_id="ep1", duration_sec=5))
    episode, ep_token = store.load_with_version(Episode, "ep1")
    store.save(episode.model_copy(update={"approved_cut_version": 3,
                                          "approved_cut_output_id": "approved-cut",
                                          "approval_review_id": "gate"}), expected_version=ep_token)
    script = Script(script_id="sc", episode_id="ep1", logline="night", beat_to_shot={"b": "old"})
    store.create(script)
    after = script.model_copy(update={**script.next_version(), "beat_to_shot": {"b": "new"}})
    assert revise_script(store, after, expected_version=1) == ()
    plan, = store.cascades()
    assert {s["shot_id"] for s in plan["affected"]} == {"old", "new"}
    assert store.load(Episode, "ep1").approval_review_id is None
    with pytest.raises(JobError):
        approve(store, "old")


def make_plan(before, after, *, affected=()):
    return {"cascade_id": uuid4().hex, "entity_type": type(before).__name__,
            "before": before.model_dump(mode="json"), "after": after.model_dump(mode="json"),
            "expected_version": 1, "episode_ids": sorted({s["episode_id"] for s in affected}),
            "affected": list(affected)}


def test_journal_detached_reads_and_append_only_completion(project):
    root, store = project
    before = Location(location_id="hall")
    plan = make_plan(before, before.model_copy(update=before.next_version()))
    store.record_cascade(plan)
    recorded = tree_bytes(root)
    loaded, = store.cascades()
    loaded["after"]["geometry_provenance"] = "caller mutation"
    assert store.cascades()[0] == plan
    store.complete_cascade(plan["cascade_id"])
    assert all(tree_bytes(root)[path] == data for path, data in recorded.items())
    assert len(tree_bytes(root)) > len(recorded)
    assert ProjectStore(root).cascades() == (plan,)
    assert ProjectStore(root).cascades(pending_only=True) == ()


@pytest.mark.parametrize("damage", ["truncated", "bad-id", "bad-affected", "bad-completion"])
def test_corrupt_journal_fails_closed_on_reopen(project, damage):
    root, store = project
    before = Location(location_id="hall")
    plan = make_plan(before, before.model_copy(update=before.next_version()))
    store.record_cascade(plan)
    candidates = [p for p in root.path.rglob("*.json") if json.loads(p.read_text())["cascade_id"] == plan["cascade_id"]]
    plan_file, = candidates
    if damage == "bad-completion":
        store.complete_cascade(plan["cascade_id"])
        marker, = [p for p in root.path.rglob("*.json") if p != plan_file]
        root.write_text(marker.relative_to(root.path).as_posix(), '{"cascade_id": 42}')
    else:
        payload = dict(plan)
        if damage == "bad-id":
            payload["cascade_id"] = "not-a-uuid"
        if damage == "bad-affected":
            payload["affected"] = [{"episode_id": "ep1"}]
        root.write_text(plan_file.relative_to(root.path).as_posix(), "{truncated" if damage == "truncated" else json.dumps(payload))
    with pytest.raises(JobError) as caught:
        ProjectStore(root).cascades(pending_only=True)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


@pytest.mark.parametrize("damage", ["unknown-type", "bad-payload"])
def test_recovery_validates_opaque_plan_payload_without_skipping(project, damage):
    root, store = project
    location = Location(location_id="hall")
    store.create(location)
    plan = make_plan(location, location.model_copy(update=location.next_version()))
    if damage == "unknown-type":
        plan["entity_type"] = "UnknownEntity"
    else:
        plan["after"] = {"schema_version": 1}
    store.record_cascade(plan)
    with pytest.raises(JobError):
        resume_invalidations(ProjectStore(root))
    assert len(store.cascades(pending_only=True)) == 1
    assert store.load(Location, "hall") == location


@pytest.mark.parametrize("divergent", ["upstream", "shot"])
def test_recovery_refuses_divergent_content_and_keeps_plan(project, monkeypatch, divergent):
    root, store = project
    setup_cascade(root, store)
    with monkeypatch.context() as patch:
        inject_phase(patch, store, "upstream")
        with pytest.raises(JobError):
            revise_hall(store)
    pending = store.cascades(pending_only=True)
    if divergent == "upstream":
        before = store.load(Location, "hall")
        store.revise(before.model_copy(update={**before.next_version(), "geometry_provenance": "unexpected divergent value"}), expected_version=token(store, Location, "hall"))
    else:
        before = store.load(Shot, "s1", episode_id="ep1")
        store.revise(before.model_copy(update={**before.next_version(), "status": ShotStatus.DRAFT, "approval_review_id": None}), expected_version=token(store, Shot, "s1", "ep1"))
    with pytest.raises(JobError):
        resume_invalidations(ProjectStore(root))
    assert store.cascades(pending_only=True) == pending
    if divergent == "upstream":
        assert store.load(Location, "hall").geometry_provenance == "unexpected divergent value"
    else:
        assert store.load(Shot, "s1", episode_id="ep1").version == 2


def test_approval_before_revision_is_selected_and_staled(project):
    _, store = project
    add_episode(store)
    reviewable(store, location_id="hall")
    store.create(Location(location_id="hall"))
    approve(store)
    assert revise_hall(store) == ("s1",)
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.STALE


def test_nested_approval_at_upstream_commit_is_rejected(project, monkeypatch):
    root, store = project
    add_episode(store)
    reviewable(store, dependencies=["hall"])
    store.create(Location(location_id="hall"))
    original = store.revise
    rejected = []

    def interleave(entity, *, expected_version):
        other = ProjectStore(ProjectRoot(root.path / "."))
        with pytest.raises(CommandInProgress):
            approve(other)
        rejected.append(True)
        return original(entity, expected_version=expected_version)
    monkeypatch.setattr(store, "revise", interleave)
    assert revise_hall(store) == ()
    assert rejected == [True]
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW
    with pytest.raises(JobError):
        approve(store)


def test_nested_revision_during_approval_validation_is_rejected(project, monkeypatch):
    _, store = project
    add_episode(store)
    reviewable(store, dependencies=["hall"])
    store.create(Location(location_id="hall"))
    original = store.load
    rejected = []

    def interleave(cls, record_id=None, **kwargs):
        if cls is Review:
            with pytest.raises(CommandInProgress):
                revise_hall(store)
            rejected.append(True)
        return original(cls, record_id, **kwargs)
    monkeypatch.setattr(store, "load", interleave)
    assert approve(store).status is ShotStatus.APPROVED
    assert rejected
    assert store.load(Location, "hall").version == 1
    assert store.cascades() == ()


def test_approval_rechecks_dependency_evidence_before_commit(project, monkeypatch):
    _, store = project
    add_episode(store)
    reviewable(store, dependencies=["hall"])
    before = Location(location_id="hall")
    store.create(before)
    original = store.cascades
    calls = 0

    def change_evidence(*, pending_only=False):
        nonlocal calls
        result = original(pending_only=pending_only)
        if not pending_only:
            calls += 1
            if calls == 1:
                plan = make_plan(before, before.model_copy(update=before.next_version()),
                                 affected=[{"episode_id": "ep1", "shot_id": "s1", "version": 1}])
                store.record_cascade(plan)
                store.complete_cascade(plan["cascade_id"])
        return result
    monkeypatch.setattr(store, "cascades", change_evidence)
    shot_before = store.load_with_version(Shot, "s1", episode_id="ep1")
    with pytest.raises(JobError) as caught:
        approve(store)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert calls >= 2
    assert store.load_with_version(Shot, "s1", episode_id="ep1") == shot_before


def test_command_rejects_same_thread_nested_service_but_allows_store_calls(project):
    root, store = project
    add_episode(store)
    reviewable(store)
    with store.command():
        entity, current_token = store.load_with_version(Shot, "s1", episode_id="ep1")
        store.update(entity, expected_version=current_token)
        with pytest.raises(CommandInProgress), ProjectStore(ProjectRoot(root.path / ".")).command():
            pytest.fail("nested command must never enter")
        with pytest.raises(CommandInProgress):
            approve(store)
    assert approve(store).status is ShotStatus.APPROVED


def test_two_root_alias_stores_serialize_revision_before_approval(project, monkeypatch):
    root, store = project
    add_episode(store)
    reviewable(store, dependencies=["hall"])
    store.create(Location(location_id="hall"))
    other = ProjectStore(ProjectRoot(root.path / "."))
    at_commit, attempted, release, finished = (threading.Event() for _ in range(4))
    original = store.revise

    def hold_commit(entity, *, expected_version):
        at_commit.set()
        assert release.wait(5), "coordinator did not release upstream commit"
        return original(entity, expected_version=expected_version)
    monkeypatch.setattr(store, "revise", hold_commit)
    approval_token = token(other, Shot, "s1", "ep1")

    def concurrent_approval():
        assert at_commit.wait(5)
        attempted.set()
        try:
            return approve_shot(other, "s1", episode_id="ep1", review_id="r-ep1-s1", expected_version=approval_token)
        finally:
            finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        revision = pool.submit(revise_hall, store)
        approval = pool.submit(concurrent_approval)
        try:
            assert attempted.wait(5)
            assert not finished.wait(0.1), "approval crossed active revision command boundary"
        finally:
            release.set()
        assert revision.result(timeout=5) == ()
        with pytest.raises(JobError):
            approval.result(timeout=5)
    reopened = ProjectStore(root)
    assert reopened.load(Location, "hall").version == 2
    assert reopened.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW


def test_two_same_token_approvals_have_one_winner(project):
    root, store = project
    add_episode(store)
    reviewable(store)
    original_token = token(store, Shot, "s1", "ep1")
    barrier = threading.Barrier(2)

    def attempt(writer):
        barrier.wait(timeout=5)
        try:
            return approve_shot(writer, "s1", episode_id="ep1", review_id="r-ep1-s1", expected_version=original_token)
        except VersionConflict as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, writer) for writer in (store, ProjectStore(root))]
        results = [f.result(timeout=5) for f in futures]
    assert sum(isinstance(r, Shot) for r in results) == 1
    assert sum(isinstance(r, VersionConflict) for r in results) == 1
    assert token(store, Shot, "s1", "ep1") == original_token + 1


@pytest.mark.parametrize("kind", ["Script", "Location", "CameraProfile", "Shot"])
def test_revision_commands_use_mutation_token_separately_from_content_version(project, kind):
    root, store = project
    add_episode(store)
    entities = {
        "Script": Script(script_id="sc", episode_id="ep1", logline="night"),
        "Location": Location(location_id="hall"),
        "CameraProfile": CameraProfile(camera_id="cam", profile_name="CCTV"),
        "Shot": Shot(shot_id="s1", episode_id="ep1", duration_sec=5),
    }
    record = entities[kind]
    store.create(record)
    store.update(record, expected_version=1)
    revised = record.model_copy(update=record.next_version())
    calls = {
        "Script": lambda expected: revise_script(store, revised, expected_version=expected),
        "Location": lambda expected: revise_location(store, revised, expected_version=expected),
        "CameraProfile": lambda expected: revise_camera_profile(store, revised, expected_version=expected),
        "Shot": lambda expected: revise_shot(store, revised, expected_version=expected),
    }
    before = tree_bytes(root)
    with pytest.raises(VersionConflict) as caught:
        calls[kind](1)
    assert (caught.value.expected, caught.value.actual) == (1, 2)
    assert tree_bytes(root) == before
    calls[kind](2)
    record_id = {"Script": "sc", "Location": "hall", "CameraProfile": "cam", "Shot": "s1"}[kind]
    episode_id = "ep1" if kind in {"Script", "Shot"} else None
    loaded, current_token = store.load_with_version(type(record), record_id, episode_id=episode_id)
    assert (loaded.version, current_token) == (2, 3)


def test_style_approval_binds_content_version_after_noop_token_advances(project):
    _, store = project
    bible = StyleBible(project_id="p1")
    store.create(bible)
    store.update(bible, expected_version=1)
    persist_review(store, "style", "p1", 1, target_kind="style_bible", episode_id=None)
    with pytest.raises(VersionConflict):
        approve_style_bible(store, "p1", review_id="style", expected_version=1)
    result = approve_style_bible(store, "p1", review_id="style", expected_version=2)
    assert (result.version, result.approval_review_id) == (1, "style")
    assert token(store, StyleBible, "p1") == 3


def test_pending_json_project_copy_recovers_without_original_process_state(project, monkeypatch):
    root, store = project
    setup_cascade(root, store)
    with monkeypatch.context() as patch:
        inject_phase(patch, store, "stale2")
        with pytest.raises(JobError):
            revise_hall(store)
    pending, = store.cascades(pending_only=True)
    original = tree_bytes(root)
    copied = ProjectRoot(root.make_dir("export-copy"))
    for relative, data in original.items():
        copied.write_bytes(relative, data)
    reopened = ProjectStore(copied)
    assert resume_invalidations(reopened) == (pending["cascade_id"],)
    assert resume_invalidations(reopened) == ()
    assert_safe(reopened, ["ep1"])
    assert reopened.versions(Location, "hall") == [1, 2]
    assert reopened.cascades(pending_only=True) == ()
    assert store.cascades(pending_only=True) == (pending,)
    assert copied.resolve("audio/master.wav").read_bytes() == b"retain this artifact"


def test_multiple_pending_plans_roll_forward_in_reproducible_order(project):
    root, store = project
    plans = []
    for name in ("west", "east"):
        before = Location(location_id=name)
        store.create(before)
        plan = make_plan(before, before.model_copy(update={**before.next_version(), "geometry_provenance": name}))
        store.record_cascade(plan)
        plans.append(plan)
    copied = ProjectRoot(root.make_dir("copy"))
    for path, data in tree_bytes(root).items():
        copied.write_bytes(path, data)
    first_order = resume_invalidations(store)
    second_order = resume_invalidations(ProjectStore(copied))
    assert first_order == second_order
    assert set(first_order) == {plan["cascade_id"] for plan in plans}
    assert len(first_order) == 2
    assert resume_invalidations(store) == ()
    assert resume_invalidations(ProjectStore(copied)) == ()


def test_other_thread_store_write_waits_for_entire_command(project):
    root, store = project
    add_episode(store)
    other = ProjectStore(root)
    attempted, finished = threading.Event(), threading.Event()

    def write():
        attempted.set()
        entity, expected = other.load_with_version(Episode, "ep1")
        result = other.save(entity, expected_version=expected)
        finished.set()
        return result
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.command():
            entity, initial = store.load_with_version(Episode, "ep1")
            future = pool.submit(write)
            assert attempted.wait(5)
            assert not finished.wait(0.1)
            assert store.save(entity, expected_version=initial) == 2
        assert future.result(timeout=5) == 3


def test_two_stores_serialize_approval_before_revision(project, monkeypatch):
    root, store = project
    add_episode(store)
    reviewable(store, dependencies=["hall"])
    store.create(Location(location_id="hall"))
    other = ProjectStore(root)
    entered, attempted, release, finished = (threading.Event() for _ in range(4))
    original = store.load

    def hold_review(cls, record_id=None, **kwargs):
        if cls is Review:
            entered.set()
            assert release.wait(5)
        return original(cls, record_id, **kwargs)
    monkeypatch.setattr(store, "load", hold_review)

    def revise_after_approval_starts():
        assert entered.wait(5)
        attempted.set()
        try:
            return revise_hall(other)
        finally:
            finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(approve, store)
        revision = pool.submit(revise_after_approval_starts)
        try:
            assert attempted.wait(5)
            assert not finished.wait(0.1)
        finally:
            release.set()
        assert approval.result(timeout=5).status is ShotStatus.APPROVED
        assert revision.result(timeout=5) == ("s1",)
    assert ProjectStore(root).load(Shot, "s1", episode_id="ep1").status is ShotStatus.STALE


def test_approval_rechecks_target_token_without_erasing_concurrent_notes(project, monkeypatch):
    _, store = project
    add_episode(store)
    reviewable(store)
    original = store.load
    changed = False

    def interleave(cls, record_id=None, **kwargs):
        nonlocal changed
        if cls is Review and not changed:
            changed = True
            entity, current_token = store.load_with_version(Shot, "s1", episode_id="ep1")
            store.update(entity.model_copy(update={"approval_notes": "new feedback"}), expected_version=current_token)
        return original(cls, record_id, **kwargs)
    monkeypatch.setattr(store, "load", interleave)
    with pytest.raises(VersionConflict) as caught:
        approve(store)
    assert (caught.value.expected, caught.value.actual) == (5, 6)
    result, current_token = store.load_with_version(Shot, "s1", episode_id="ep1")
    assert (result.status, result.approval_notes, current_token) == (ShotStatus.REVIEW, "new feedback", 6)
