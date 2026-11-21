"""Independent regressions for the adjudication and hardening addenda.

Metadata names below were observed in persisted JSON, without reading production.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    Beat,
    CameraProfile,
    Episode,
    Location,
    Review,
    Script,
    Shot,
    StyleBible,
)
from orchestrator.domain.invalidation import revision_impact, shots_invalidated_by_script
from orchestrator.domain.state import RevisionError, check_revision
from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import approve_episode, approve_shot, approve_style_bible
from orchestrator.service.dependencies import (
    resume_invalidations,
    revise_camera_profile,
    revise_location,
    revise_script,
    revise_shot,
)
from orchestrator.store.json_store import ProjectStore, VersionConflict
from tests.service.test_concurrency_contract import (
    add_episode,
    approve,
    gate,
    inject_phase,
    make_plan,
    persist_review,
    reviewable,
    token,
    tree_bytes,
)


@pytest.fixture
def project(tmp_path):
    root = ProjectRoot(tmp_path)
    return root, ProjectStore(root)


def mapped_script():
    return Script(
        script_id="sc", episode_id="ep1", logline="night",
        beats=[Beat(beat_id="b1", target_sec=5, subtlety=2),
               Beat(beat_id="b2", target_sec=5, subtlety=2)],
        beat_to_shot={"b1": "mapped", "b2": "review"},
    )


def changed_beats(before):
    return before.model_copy(update={
        **before.next_version(),
        "beats": [beat.model_copy(update={"target_sec": 6}) for beat in before.beats],
    })


@pytest.mark.parametrize("state", list(ShotStatus))
def test_changed_beat_unchanged_mapping_selects_every_state_without_mutation(state):
    before = mapped_script()
    after = changed_beats(before)
    mapped = Shot(shot_id="mapped", episode_id="ep1", duration_sec=5, status=state,
                  approval_review_id="r" if state is ShotStatus.APPROVED else None)
    foreign = mapped.model_copy(update={"episode_id": "ep2"})
    unrelated = mapped.model_copy(update={"shot_id": "unrelated"})
    shots = [foreign, mapped, mapped, unrelated, foreign]
    originals = [entity.model_dump(mode="json") for entity in [before, after, *shots]]
    assert mapped.beat_id is None and mapped.dependencies == []
    assert before.beat_to_shot == after.beat_to_shot
    assert revision_impact(before, after, iter(shots)) == (mapped,)
    normalized = [shot.model_copy(update={"status": ShotStatus.APPROVED,
                                          "approval_review_id": "r"}) for shot in shots]
    existing = shots_invalidated_by_script(before, after, iter(normalized))
    assert existing.shot_ids == ("mapped",)
    assert tuple(shot.shot_id for shot in revision_impact(before, after, normalized)) == existing.shot_ids
    assert shots_invalidated_by_script(before, after, shots).shot_ids == (
        ("mapped",) if state is ShotStatus.APPROVED else ()
    )
    assert [entity.model_dump(mode="json") for entity in [before, after, *shots]] == originals


def test_unchanged_script_mapping_stales_approved_and_fences_review_in_actual_store(project):
    root, store = project
    for episode in ("ep1", "ep2"):
        add_episode(store, episode)
        reviewable(store, "mapped", episode)
        approve(store, "mapped", episode)
    reviewable(store, "review")
    gate(store)
    gate(store, "ep2")
    foreign_shot = store.load_with_version(Shot, "mapped", episode_id="ep2")
    foreign_gate = store.load_with_version(Episode, "ep2")
    before = mapped_script()
    store.create(before)
    review_before = store.load_with_version(Shot, "review", episode_id="ep1")
    assert revise_script(store, changed_beats(before), expected_version=1) == ("mapped",)
    reopened = ProjectStore(root)
    assert reopened.load(Shot, "mapped", episode_id="ep1").status is ShotStatus.STALE
    assert reopened.load_with_version(Shot, "review", episode_id="ep1") == review_before
    assert reopened.load_with_version(Shot, "mapped", episode_id="ep2") == foreign_shot
    assert reopened.load_with_version(Episode, "ep2") == foreign_gate
    assert reopened.load(Episode, "ep1").approved_cut_version is None
    plan, = reopened.cascades()
    affected = [(entry["episode_id"], entry["shot_id"], entry["version"]) for entry in plan["affected"]]
    assert set(affected) == {("ep1", "mapped", 1), ("ep1", "review", 1)}
    assert len(affected) == 2
    snapshot = tree_bytes(root)
    with pytest.raises(JobError) as caught:
        approve(reopened, "review")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert tree_bytes(root) == snapshot


@pytest.mark.parametrize("change", [
    {"location_id": "other"}, {"version": 4}, {"supersedes": None},
    {"approval_review_id": "inherited"},
])
def test_domain_revision_guard_owns_identity_successor_and_approval_reset(change):
    before = Location(location_id="hall")
    valid = before.model_copy(update=before.next_version())
    original = before.model_dump(), valid.model_dump()
    assert check_revision(before, valid) is None
    with pytest.raises(RevisionError):
        check_revision(before, valid.model_copy(update=change))
    with pytest.raises(RevisionError):
        check_revision(before, CameraProfile(camera_id="hall", profile_name="CCTV", version=2, supersedes=1))
    assert (before.model_dump(), valid.model_dump()) == original


@pytest.mark.parametrize("change", [
    {"shot_id": "other"}, {"episode_id": "ep2"},
    {"status": ShotStatus.REVIEW}, {"approval_review_id": "old-review"},
])
def test_domain_shot_revision_requires_same_identity_and_unapproved_draft(change):
    before = Shot(shot_id="s1", episode_id="ep1", duration_sec=5)
    after = before.model_copy(update=before.next_version())
    assert check_revision(before, after) is None
    with pytest.raises(RevisionError):
        check_revision(before, after.model_copy(update=change))


def revision_setup(store, kind):
    add_episode(store)
    reviewable(store, location_id="hall")
    approve(store)
    gate(store)
    if kind == "Location":
        before = Location(location_id="hall")
        store.create(before)
        after = before.model_copy(update=before.next_version())
        call = lambda entity, expected: revise_location(
            store, entity, expected_version=expected)
        expected = token(store, Location, "hall")
    else:
        before, expected = store.load_with_version(Shot, "s1", episode_id="ep1")
        after = before.model_copy(update={**before.next_version(), "status": ShotStatus.DRAFT,
                                          "approval_review_id": None})
        call = lambda entity, expected: revise_shot(store, entity, expected_version=expected)
    return before, after, expected, call


@pytest.mark.parametrize("kind,field", [
    ("Location", "blend_path"), ("Shot", "audio_refs"), ("Shot", "framing_panel_ref"),
])
@pytest.mark.parametrize("unsafe", ["../escape.bin", "C:/escape.bin", "NUL"])
def test_unsafe_successor_paths_fail_before_any_journal_or_gate_write(project, kind, field, unsafe):
    root, store = project
    before, valid, expected, service_call = revision_setup(store, kind)
    snapshot = tree_bytes(root)
    assert store.validate_revision(valid, expected_version=expected) == (before, expected)
    assert tree_bytes(root) == snapshot
    malformed = valid.model_copy(update={field: [unsafe] if field == "audio_refs" else unsafe})
    for operation in (store.validate_revision, store.revise, service_call):
        with pytest.raises(JobError) as caught:
            operation(malformed, expected_version=expected) if operation != service_call else operation(malformed, expected)
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        assert isinstance(caught.value.__cause__, ContainmentError)
        assert tree_bytes(root) == snapshot


@pytest.mark.parametrize("kind,damage", [
    ("Location", {"asset_ids": 42}), ("Shot", {"duration_sec": -1}),
    ("Shot", {"event_sec": 6}), ("Location", {"version": 4}),
    ("Shot", {"status": ShotStatus.REVIEW}),
])
def test_model_copy_payloads_are_revalidated_without_any_write(project, kind, damage):
    root, store = project
    _, valid, expected, service_call = revision_setup(store, kind)
    malformed = valid.model_copy(update=damage)
    snapshot = tree_bytes(root)
    for operation in (store.validate_revision, store.revise, service_call):
        with pytest.raises(JobError) as caught:
            operation(malformed, expected_version=expected) if operation != service_call else operation(malformed, expected)
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        assert caught.value.__cause__ is not None
        assert tree_bytes(root) == snapshot
    with pytest.raises(VersionConflict) as caught:
        store.validate_revision(valid, expected_version=expected + 100)
    assert (caught.value.expected, caught.value.actual) == (expected + 100, expected)
    assert tree_bytes(root) == snapshot


def test_shot_with_both_links_recovers_its_own_successor_and_reapproves(project, monkeypatch):
    root, store = project
    root.write_bytes("audio/master.wav", b"retained audio")
    add_episode(store)
    reviewable(store, location_id="hall", camera_id="cam", audio_refs=["audio/master.wav"])
    approve(store)
    gate(store)
    store.create(Location(location_id="hall"))
    store.create(CameraProfile(camera_id="cam", profile_name="CCTV"))
    before, expected = store.load_with_version(Shot, "s1", episode_id="ep1")
    retained = tree_bytes(root)
    after = before.model_copy(update={**before.next_version(), "status": ShotStatus.DRAFT,
                                      "approval_review_id": None, "audio_refs": []})
    with monkeypatch.context() as patch:
        inject_phase(patch, store, "complete")
        with pytest.raises(JobError) as caught:
            revise_shot(store, after, expected_version=expected)
    assert caught.value.__cause__ is not None
    reopened = ProjectStore(root)
    pending, = reopened.cascades(pending_only=True)
    assert pending["before"]["shot_id"] == pending["after"]["shot_id"] == "s1"
    assert pending["affected"] == [{"episode_id": "ep1", "shot_id": "s1", "version": 1}]
    assert reopened.load(Shot, "s1", episode_id="ep1") == after
    assert reopened.load(Episode, "ep1").approved_cut_version is None
    assert resume_invalidations(reopened) == (pending["cascade_id"],)
    assert resume_invalidations(reopened) == ()
    assert reopened.versions(Shot, "s1", episode_id="ep1") == [1, 2]
    assert reopened.load(Shot, "s1", episode_id="ep1", version=1) == before
    for state in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        after = after.model_copy(update={"status": state, "framing_status": FramingStatus.APPROVED})
        reopened.update(after, expected_version=token(reopened, Shot, "s1", "ep1"))
    snapshot = tree_bytes(root)
    with pytest.raises(JobError) as caught:
        approve(reopened)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert tree_bytes(root) == snapshot
    persist_review(reopened, "fresh", "s1", 2)
    approved = approve_shot(reopened, "s1", episode_id="ep1", review_id="fresh",
                            expected_version=token(reopened, Shot, "s1", "ep1"))
    assert (approved.shot_id, approved.version, approved.supersedes, approved.status) == (
        "s1", 2, 1, ShotStatus.APPROVED)
    assert reopened.load(Review, "fresh").target_version == 2
    assert reopened.load(Location, "hall").version == reopened.load(CameraProfile, "cam").version == 1
    now = tree_bytes(root)
    assert all(now[path] == data for path, data in retained.items()
               if path.startswith("episodes/ep1/shots/s1/") or path == "audio/master.wav")


def metadata_keys(payload):
    """Infer the two observed metadata keys, keeping domain version separate."""
    token_key, = [key for key in payload if key.startswith("_") and "mutation" in key and "token" in key]
    predecessor_key, = [key for key in payload if key.startswith("_") and "predecessor" in key]
    return token_key, predecessor_key


@pytest.mark.parametrize("damage", ["version", "identity", "predecessor", "token"])
def test_lifecycle_corruption_rejects_reopen_and_cannot_revive_stale_token(project, damage):
    root, store = project
    before = Location(location_id="hall")
    store.create(before)
    base = tree_bytes(root)
    store.update(before, expected_version=1)
    added, = set(tree_bytes(root)) - set(base)
    payload = json.loads(root.resolve(added).read_text(encoding="utf-8"))
    token_key, predecessor_key = metadata_keys(payload)
    if damage == "version":
        payload["version"] = 2
    elif damage == "identity":
        payload["location_id"] = "other"
    elif damage == "predecessor":
        payload[predecessor_key] = 0
    else:
        payload[token_key] = 1
    root.write_text(added, json.dumps(payload))
    snapshot = tree_bytes(root)
    reopened = ProjectStore(ProjectRoot(root.path))
    operations = [lambda: reopened.load_with_version(Location, "hall"),
                  lambda: reopened.load(Location, "hall", version=1),
                  lambda: reopened.update(before, expected_version=1)]
    for operation in operations:
        with pytest.raises(JobError) as caught:
            operation()
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        assert tree_bytes(root) == snapshot
    assert all(snapshot[path] == data for path, data in base.items())


@pytest.mark.parametrize("damage", ["regressed-token", "skipped-token", "predecessor", "version"])
def test_cross_content_history_cannot_regress_tokens_or_mismatch_filename(project, damage):
    root, store = project
    old = Location(location_id="hall", geometry_provenance="before")
    store.create(old)
    store.update(old, expected_version=1)
    store.update(old, expected_version=2)
    prior = tree_bytes(root)
    successor = old.model_copy(update={**old.next_version(), "geometry_provenance": "after"})
    store.revise(successor, expected_version=3)
    assert store.load_with_version(Location, "hall") == (successor, 4)
    added, = set(tree_bytes(root)) - set(prior)
    payload = json.loads(root.resolve(added).read_text(encoding="utf-8"))
    token_key, predecessor_key = metadata_keys(payload)
    assert (payload[token_key], payload[predecessor_key], payload["version"], payload["supersedes"]) == (4, 3, 2, 1)
    if damage == "regressed-token":
        payload[token_key], payload[predecessor_key] = 1, 0
    elif damage == "skipped-token":
        payload[token_key] = 5
    elif damage == "predecessor":
        payload[predecessor_key] = 2
    else:
        payload["version"] = 3
    root.write_text(added, json.dumps(payload))
    snapshot = tree_bytes(root)
    reopened = ProjectStore(root)
    for operation in (lambda: reopened.load_with_version(Location, "hall"),
                      lambda: reopened.update(old, expected_version=1)):
        with pytest.raises(JobError) as caught:
            operation()
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        assert tree_bytes(root) == snapshot
    assert all(snapshot[path] == data for path, data in prior.items())


def test_valid_cross_content_and_lifecycle_writes_preserve_all_prior_bytes(project):
    root, store = project
    old = Location(location_id="hall", geometry_provenance="before")
    store.create(old)
    retained = tree_bytes(root)
    store.update(old, expected_version=1)
    assert all(tree_bytes(root)[path] == data for path, data in retained.items())
    retained = tree_bytes(root)
    successor = old.model_copy(update={**old.next_version(), "geometry_provenance": "after"})
    store.revise(successor, expected_version=2)
    assert all(tree_bytes(root)[path] == data for path, data in retained.items())
    retained = tree_bytes(root)
    store.update(successor, expected_version=3)
    now = tree_bytes(root)
    assert all(now[path] == data for path, data in retained.items())
    assert len(now) == 4
    reopened = ProjectStore(root)
    assert reopened.load_with_version(Location, "hall", version=1) == (old, 2)
    assert reopened.load_with_version(Location, "hall") == (successor, 4)
    assert (successor.version, successor.supersedes) == (2, 1)
    records = [json.loads(data) for data in now.values()]
    key, predecessor = metadata_keys(records[0])
    assert sorted((record[key], record[predecessor]) for record in records) == [(1, 0), (2, 1), (3, 2), (4, 3)]


def plan_path(root, cascade_id):
    matches = []
    for path in root.path.rglob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("cascade_id") == cascade_id and "before" in payload:
            matches.append(path.relative_to(root.path).as_posix())
    result, = matches
    return result


@pytest.mark.parametrize("entity_type", [[], {}])
def test_unhashable_journal_type_is_invalid_manifest_with_no_writes(project, entity_type):
    root, store = project
    before = Location(location_id="hall")
    plan = make_plan(before, before.model_copy(update=before.next_version()))
    store.record_cascade(plan)
    relative = plan_path(root, plan["cascade_id"])
    plan["entity_type"] = entity_type
    root.write_text(relative, json.dumps(plan))
    snapshot = tree_bytes(root)
    for pending_only in (False, True):
        with pytest.raises(JobError) as caught:
            ProjectStore(root).cascades(pending_only=pending_only)
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        assert tree_bytes(root) == snapshot


@pytest.mark.parametrize("damage", ["id-mismatch", "truncated-json"])
def test_completion_marker_must_match_filename_and_parse_with_cause(project, damage):
    root, store = project
    before = Location(location_id="hall")
    plan = make_plan(before, before.model_copy(update=before.next_version()))
    store.record_cascade(plan)
    retained = tree_bytes(root)
    store.complete_cascade(plan["cascade_id"])
    marker, = set(tree_bytes(root)) - set(retained)
    root.write_text(marker, "{broken" if damage == "truncated-json" else json.dumps({"cascade_id": uuid4().hex}))
    snapshot = tree_bytes(root)
    for pending_only in (False, True):
        with pytest.raises(JobError) as caught:
            ProjectStore(root).cascades(pending_only=pending_only)
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        if damage == "truncated-json":
            assert isinstance(caught.value.__cause__, json.JSONDecodeError)
        assert tree_bytes(root) == snapshot


@pytest.mark.parametrize("damage", [
    "before-payload", "after-payload", "wrong-identity", "unknown-type",
    "impossible-version", "wrong-supersedes", "unsafe-path",
])
def test_all_approvals_validate_completed_plan_domain_payloads_without_writes(project, damage):
    root, store = project
    add_episode(store)
    reviewable(store)
    persist_review(store, "cut", "ep1", 3)
    store.create(StyleBible(project_id="p1"))
    persist_review(store, "style", "p1")
    before = Location(location_id="hall")
    after = before.model_copy(update=before.next_version())
    store.create(before)
    store.revise(after, expected_version=1)
    plan = make_plan(before, after)
    store.record_cascade(plan)
    store.complete_cascade(plan["cascade_id"])
    relative = plan_path(root, plan["cascade_id"])
    if damage == "before-payload":
        plan["before"] = {"schema_version": 1}
    elif damage == "after-payload":
        plan["after"]["asset_ids"] = 42
    elif damage == "wrong-identity":
        plan["after"]["location_id"] = "other"
    elif damage == "unknown-type":
        plan["entity_type"] = "UnknownEntity"
    elif damage == "impossible-version":
        plan["after"]["version"] = 4
    elif damage == "wrong-supersedes":
        plan["after"]["supersedes"] = None
    else:
        plan["after"]["blend_path"] = "../escape.blend"
    root.write_text(relative, json.dumps(plan))
    reopened = ProjectStore(root)
    # Structurally valid opaque plans remain the store's responsibility boundary.
    assert reopened.cascades() == (plan,)
    assert reopened.cascades(pending_only=True) == ()
    snapshot = tree_bytes(root)
    approvals = [lambda: approve(reopened),
                 lambda: approve_episode(reopened, "ep1", cut_version=3, review_id="cut",
                                         expected_version=token(reopened, Episode, "ep1")),
                 lambda: approve_style_bible(reopened, "p1", review_id="style",
                                             expected_version=token(reopened, StyleBible, "p1"))]
    for operation in approvals:
        with pytest.raises(JobError) as caught:
            operation()
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
        if damage != "unknown-type":
            assert caught.value.__cause__ is not None
        assert tree_bytes(root) == snapshot
    assert reopened.load(Location, "hall") == after
    assert reopened.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW
    assert reopened.load(StyleBible, "p1").approval_review_id is None
    assert reopened.load(Episode, "ep1").approved_cut_version is None


@pytest.mark.parametrize("kind", ["profile-post-only", "profile-no-op", "script-metadata"])
def test_nonimpacting_revisions_preserve_gate_and_allow_review_content_approval(project, kind):
    root, store = project
    add_episode(store)
    reviewable(store, "mapped", camera_id="cam")
    approve(store, "mapped")
    reviewable(store, "review", camera_id="cam")
    gate(store)
    gate_before = store.load_with_version(Episode, "ep1")
    approved_before = store.load_with_version(Shot, "mapped", episode_id="ep1")
    review_before = store.load_with_version(Shot, "review", episode_id="ep1")
    if kind == "script-metadata":
        before = mapped_script()
        store.create(before)
        after = before.model_copy(update={**before.next_version(), "logline": "new synopsis"})
        changed = revise_script(store, after, expected_version=1)
    else:
        before = CameraProfile(camera_id="cam", profile_name="CCTV")
        store.create(before)
        updates = {**before.next_version()}
        if kind == "profile-post-only":
            updates["bitrate_kbps"] = 900
        after = before.model_copy(update=updates)
        changed = revise_camera_profile(store, after, expected_version=1)
    assert changed == ()
    reopened = ProjectStore(root)
    assert reopened.load_with_version(Episode, "ep1") == gate_before
    assert reopened.load_with_version(Shot, "mapped", episode_id="ep1") == approved_before
    assert reopened.load_with_version(Shot, "review", episode_id="ep1") == review_before
    assert all(plan["affected"] == [] for plan in reopened.cascades())
    assert approve(reopened, "review").status is ShotStatus.APPROVED
    record_id = "sc" if kind == "script-metadata" else "cam"
    episode_id = "ep1" if kind == "script-metadata" else None
    assert reopened.load(type(before), record_id, episode_id=episode_id, version=1) == before
    assert reopened.load(type(after), record_id, episode_id=episode_id) == after


@pytest.mark.parametrize("kind", ["Location", "CameraProfile", "Script"])
def test_changed_ids_name_only_actual_approved_to_stale_transitions(project, kind):
    _, store = project
    add_episode(store)
    fields = {"location_id": "hall", "camera_id": "cam", "beat_id": "b"}
    for name in ("approved", "review", "stale"):
        reviewable(store, name, **fields)
        if name != "review":
            approved = approve(store, name)
            if name == "stale":
                store.update(approved.model_copy(update={"status": ShotStatus.STALE}),
                             expected_version=token(store, Shot, name, "ep1"))
    store.create(Shot(shot_id="draft", episode_id="ep1", duration_sec=5, **fields))
    unchanged = {name: store.load_with_version(Shot, name, episode_id="ep1")
                 for name in ("review", "stale", "draft")}
    if kind == "Location":
        before = Location(location_id="hall")
        store.create(before)
        changed = revise_location(store, before.model_copy(update={**before.next_version(), "geometry_provenance": "new"}),
                                  expected_version=1)
    elif kind == "CameraProfile":
        before = CameraProfile(camera_id="cam", profile_name="CCTV")
        store.create(before)
        changed = revise_camera_profile(store, before.model_copy(update={**before.next_version(), "exposure_ev": 2}),
                                        expected_version=1)
    else:
        before = Script(script_id="sc", episode_id="ep1", logline="night",
                        beats=[Beat(beat_id="b", target_sec=5, subtlety=2)])
        store.create(before)
        changed = revise_script(store, changed_beats(before), expected_version=1)
    assert changed == ("approved",)
    assert store.load(Shot, "approved", episode_id="ep1").status is ShotStatus.STALE
    for name, snapshot in unchanged.items():
        assert store.load_with_version(Shot, name, episode_id="ep1") == snapshot
    plan, = store.cascades()
    assert {entry["shot_id"] for entry in plan["affected"]} == {"approved", "review", "stale", "draft"}
    assert len(plan["affected"]) == 4
