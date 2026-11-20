"""ProjectStore: optimistic concurrency, append-only versions, atomic writes, schema gate."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.domain.base import SCHEMA_VERSION, FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    Beat,
    CameraProfile,
    Episode,
    Location,
    Project,
    Review,
    Script,
    Shot,
    StyleBible,
    StyleClaim,
    Timeline,
)
from orchestrator.domain.state import TransitionError
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import approve_shot, approve_style_bible
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


@pytest.fixture
def root(tmp_path: Path) -> ProjectRoot:
    return ProjectRoot(tmp_path)


@pytest.fixture
def store(root: ProjectRoot) -> ProjectStore:
    return ProjectStore(root)


def make_shot(**overrides) -> Shot:
    fields = {"shot_id": "EP01-S03", "episode_id": "ep01", "duration_sec": 18.0}
    fields.update(overrides)
    return Shot(**fields)


def current_token(store, entity=None):
    entity = make_shot() if entity is None else entity
    record_id = next(getattr(entity, name) for name in (
        "shot_id", "script_id", "location_id", "camera_id", "project_id"
    ) if hasattr(entity, name))
    return store.load_with_version(type(entity), record_id,
                                   episode_id=getattr(entity, "episode_id", None))[1]


def shot_dir(root: ProjectRoot) -> Path:
    return root.path / "episodes" / "ep01" / "shots" / "EP01-S03"


def test_create_and_load_roundtrip(store, root):
    shot = make_shot(creator_authored={"framing"})
    store.create(shot)
    assert (shot_dir(root) / "v0001.json").is_file()
    assert store.load(Shot, "EP01-S03", episode_id="ep01") == shot


def test_unversioned_record_roundtrip(store, root):
    project = Project(project_id="p1", title="Night Shift")
    store.save(project, expected_version=0)
    assert (root.path / "project.json").is_file()
    assert store.load(Project) == project


def test_create_twice_conflicts_with_expected_zero(store):
    store.create(make_shot())
    with pytest.raises(VersionConflict) as err:
        store.create(make_shot())
    assert (err.value.expected, err.value.actual) == (0, 1)


def test_stale_expected_version_reports_both_versions(store):
    store.create(make_shot())
    store.revise(make_shot(**make_shot().next_version()), expected_version=1)
    stale_attempt = make_shot(**make_shot().next_version())
    with pytest.raises(VersionConflict) as err:
        store.revise(stale_attempt, expected_version=1)
    assert err.value.expected == 1
    assert err.value.actual == 2
    assert err.value.current.version == 2
    assert "expected version 1" in str(err.value) and "stored version is 2" in str(err.value)
    assert store.versions(Shot, "EP01-S03", episode_id="ep01") == [1, 2]


def test_update_with_stale_expected_version_conflicts(store):
    store.create(make_shot())
    store.revise(make_shot(**make_shot().next_version()), expected_version=1)
    with pytest.raises(VersionConflict) as err:
        store.update(make_shot(narrative_beat="late"), expected_version=1)
    assert (err.value.expected, err.value.actual) == (1, 2)


def test_revision_appends_and_never_touches_superseded_record(store, root):
    store.create(make_shot(narrative_beat="original"))
    path_v1 = shot_dir(root) / "v0001.json"
    before = path_v1.read_bytes()

    first = store.load(Shot, "EP01-S03", episode_id="ep01")
    store.revise(first.model_copy(update={**first.next_version(), "narrative_beat": "second"}),
                 expected_version=1)

    assert path_v1.read_bytes() == before
    head = store.load(Shot, "EP01-S03", episode_id="ep01")
    assert (head.version, head.supersedes, head.narrative_beat) == (2, 1, "second")
    assert store.load(Shot, "EP01-S03", episode_id="ep01", version=1).narrative_beat == "original"


def test_revision_must_be_next_version_with_supersedes(store):
    store.create(make_shot())
    with pytest.raises(JobError) as err:
        store.revise(make_shot(version=3, supersedes=1), expected_version=1)
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST


def test_revision_cannot_be_born_approved(store):
    store.create(make_shot())
    approved = make_shot(**make_shot().next_version(), status=ShotStatus.APPROVED,
                         approval_review_id="r1")
    with pytest.raises(JobError) as err:
        store.revise(approved, expected_version=1)
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST


def test_in_place_status_change_goes_through_state_machine(store):
    store.create(make_shot())
    with pytest.raises(TransitionError):
        store.update(make_shot(status=ShotStatus.APPROVED, approval_review_id="r1"),
                     expected_version=1)
    store.update(make_shot(status=ShotStatus.PLANNED), expected_version=1)
    assert store.load(Shot, "EP01-S03", episode_id="ep01").status is ShotStatus.PLANNED


def approve(store: ProjectStore) -> None:
    store.create(make_shot())
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING,
                   ShotStatus.REVIEW):
        store.update(make_shot(status=status, framing_status=FramingStatus.APPROVED),
                     expected_version=current_token(store))
    store.update(make_shot(status=ShotStatus.APPROVED, approval_review_id="r1"),
                 expected_version=current_token(store))


def test_approved_version_is_immutable_in_place(store):
    approve(store)
    with pytest.raises(JobError) as err:
        store.update(make_shot(status=ShotStatus.APPROVED, approval_review_id="r1",
                               narrative_beat="sneaky"), expected_version=current_token(store))
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Shot, "EP01-S03", episode_id="ep01").narrative_beat == ""


def test_approved_shot_can_go_stale_and_artifacts_stay(store, root):
    approve(store)
    artifact = shot_dir(root) / "v0001.json"
    store.update(make_shot(status=ShotStatus.STALE, approval_review_id="r1"), expected_version=current_token(store))
    assert artifact.is_file()
    assert store.load(Shot, "EP01-S03", episode_id="ep01").status is ShotStatus.STALE


def test_approved_shot_stale_move_cannot_smuggle_other_changes(store):
    approve(store)
    with pytest.raises(JobError):
        store.update(make_shot(status=ShotStatus.STALE, approval_review_id="r1",
                               narrative_beat="sneaky"), expected_version=current_token(store))


def test_approval_pointer_on_unstated_entity_makes_it_immutable(store):
    store.create(Location(location_id="hall"))
    store.update(Location(location_id="hall", approval_review_id="r1"), expected_version=current_token(store, Location(location_id="hall")))
    with pytest.raises(JobError):
        store.update(Location(location_id="hall", approval_review_id="r1",
                              geometry_provenance="changed"), expected_version=current_token(store, Location(location_id="hall")))


def test_missing_record_raises_not_found(store):
    with pytest.raises(RecordNotFound):
        store.load(Shot, "nope", episode_id="ep01")
    with pytest.raises(RecordNotFound):
        store.update(make_shot(), expected_version=1)


# -- atomicity ---------------------------------------------------------------------------


def test_crash_before_rename_leaves_old_record_intact(store, root, monkeypatch):
    store.create(make_shot(narrative_beat="good"))
    path = shot_dir(root) / "v0001.json"
    before = path.read_bytes()

    def crash(source, target):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(JobError):
        store.update(make_shot(narrative_beat="good", status=ShotStatus.PLANNED),
                     expected_version=1)
    monkeypatch.undo()

    assert path.read_bytes() == before
    assert store.load(Shot, "EP01-S03", episode_id="ep01").narrative_beat == "good"
    assert not list(root.path.rglob("*.tmp"))


def test_stray_temp_files_are_invisible_to_readers(store, root):
    store.create(make_shot())
    (shot_dir(root) / "v0002.json.deadbeef.tmp").write_text('{"half": ', encoding="utf-8")
    assert store.versions(Shot, "EP01-S03", episode_id="ep01") == [1]
    assert store.load(Shot, "EP01-S03", episode_id="ep01").version == 1


def test_successful_write_leaves_no_temp_file(store, root):
    store.create(make_shot())
    store.update(make_shot(status=ShotStatus.PLANNED), expected_version=1)
    assert (shot_dir(root) / "v0001.json").is_file()
    snapshots = list(shot_dir(root).rglob("*.json"))
    assert len(snapshots) >= 2  # retained base and immutable lifecycle snapshot
    assert not list(root.path.rglob("*.tmp"))


def test_disk_full_maps_to_disk_exhausted(store, root, monkeypatch):
    import errno

    def full(source, target):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(JobError) as err:
        store.create(make_shot())
    assert err.value.kind is JobErrorKind.DISK_EXHAUSTED
    assert not list(root.path.rglob("*.tmp"))


# -- schema and portability ----------------------------------------------------------------


def test_newer_schema_is_refused_with_clear_error(store, root):
    store.create(make_shot())
    path = shot_dir(root) / "v0001.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(JobError) as err:
        store.load(Shot, "EP01-S03", episode_id="ep01")
    assert err.value.kind is JobErrorKind.SCHEMA_VERSION_MISMATCH
    assert "99" in str(err.value)


def test_missing_schema_version_is_invalid_manifest(store, root):
    store.create(make_shot())
    path = shot_dir(root) / "v0001.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["schema_version"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(JobError) as err:
        store.load(Shot, "EP01-S03", episode_id="ep01")
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST


def test_corrupt_json_is_invalid_manifest_with_cause(store, root):
    store.create(make_shot())
    (shot_dir(root) / "v0001.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(JobError) as err:
        store.load(Shot, "EP01-S03", episode_id="ep01")
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST
    assert err.value.__cause__ is not None


def test_schema_version_is_written_on_every_record(store, root):
    store.create(make_shot())
    store.save(Project(project_id="p1", title="t"), expected_version=0)
    for path in (shot_dir(root) / "v0001.json", root.path / "project.json"):
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == SCHEMA_VERSION


def test_absolute_paths_are_refused(store):
    for bad in (r"C:\Users\x\scene.blend", "/home/x/scene.blend", r"\\server\share\a.blend"):
        with pytest.raises(JobError) as err:
            store.create(Location(location_id="hall", blend_path=bad))
        assert err.value.kind is JobErrorKind.INVALID_MANIFEST
    store.create(Location(location_id="hall", blend_path="locations/hall/scene.blend"))


def test_copied_project_reopens_standalone(store, root, tmp_path_factory):
    import shutil

    store.create(make_shot())
    store.revise(make_shot(**make_shot().next_version()), expected_version=1)
    store.save(Project(project_id="p1", title="t"), expected_version=0)
    elsewhere = tmp_path_factory.mktemp("export") / "copy"
    shutil.copytree(root.path, elsewhere)

    reopened = ProjectStore(ProjectRoot(elsewhere))
    assert reopened.load(Shot, "EP01-S03", episode_id="ep01").version == 2
    assert reopened.load(Project).title == "t"
    assert str(root.path) not in (elsewhere / "project.json").read_text(encoding="utf-8")


# -- containment and enumeration -------------------------------------------------------------


def test_traversal_in_record_id_is_rejected(store):
    with pytest.raises(JobError) as err:
        store.create(make_shot(shot_id="../../escape"))
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST


def test_ids_lists_records_in_episode(store):
    store.create(make_shot(shot_id="S2"))
    store.create(make_shot(shot_id="S1"))
    assert store.ids(Shot, episode_id="ep01") == ["S1", "S2"]
    assert store.ids(Shot, episode_id="ep02") == []


def test_episode_scoped_type_requires_episode_id(store):
    with pytest.raises(JobError):
        store.load(Shot, "EP01-S03")


# -- lifecycle-only updates and approval inheritance --------------------------------------


@pytest.mark.parametrize(("entity", "changes"), [
    (make_shot(), {"narrative_beat": "changed"}),
    (make_shot(), {"figure_params": {"pose": "standing"}}),
    (make_shot(), {"creator_authored": {"framing"}}),
    (Script(script_id="s1", episode_id="ep01", logline="original"), {"logline": "changed"}),
    (Location(location_id="hall"), {"geometry_provenance": "changed"}),
    (CameraProfile(camera_id="c1", profile_name="dvr"), {"exposure_ev": 2.0}),
    (StyleBible(project_id="p1"), {
        "desired": [StyleClaim(claim="dark", evidence_tier="creator")],
    }),
])
def test_update_rejects_content_on_unapproved_versions(store, entity, changes):
    store.create(entity)
    with pytest.raises(JobError, match="revise") as err:
        store.update(entity.model_copy(update=changes), expected_version=1)
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST
    layout_id = next(getattr(entity, name) for name in (
        "shot_id", "script_id", "location_id", "camera_id", "project_id"
    ) if hasattr(entity, name))
    assert store.load(type(entity), layout_id,
                      episode_id=getattr(entity, "episode_id", None)) == entity


def test_update_allows_unapproved_lifecycle_fields(store):
    shot = make_shot()
    store.create(shot)
    changed = shot.model_copy(update={
        "status": ShotStatus.PLANNED,
        "framing_status": FramingStatus.PENDING,
        "approval_notes": "Please compare the framing",
    })
    store.update(changed, expected_version=1)
    assert store.load(Shot, shot.shot_id, episode_id=shot.episode_id) == changed


@pytest.mark.parametrize("changes", [
    {"narrative_beat": "changed"},
    {"approval_review_id": None},
    {"approval_notes": "changed"},
    {"framing_status": FramingStatus.PENDING},
])
def test_stale_approved_version_remains_immutable(store, changes):
    approve(store)
    stale = make_shot(status=ShotStatus.STALE, approval_review_id="r1")
    store.update(stale, expected_version=current_token(store))
    with pytest.raises(JobError):
        store.update(stale.model_copy(update=changes), expected_version=current_token(store))
    assert store.load(Shot, stale.shot_id, episode_id=stale.episode_id) == stale


@pytest.mark.parametrize("entity", [
    make_shot(),
    Script(script_id="s1", episode_id="ep01", logline="logline"),
    Location(location_id="hall"),
    StyleBible(project_id="p1"),
])
def test_revision_rejects_approval_pointer_on_every_type_that_has_one(store, entity):
    if isinstance(entity, Shot):
        store.create(entity)
        for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING,
                       ShotStatus.REVIEW):
            changes = {"status": status}
            if status is ShotStatus.BUILDING:
                changes["framing_status"] = FramingStatus.APPROVED
            entity = entity.model_copy(update=changes)
            store.update(entity, expected_version=current_token(store, entity))
        review_id = "r1"
        store.save(Review(
            review_id=review_id,
            target_id=entity.shot_id,
            target_version=entity.version,
            created_at=datetime.now(UTC),
            approval_state=ShotStatus.APPROVED,
        ), expected_version=0)
        entity = approve_shot(
            store,
            entity.shot_id,
            episode_id=entity.episode_id,
            review_id=review_id,
            expected_version=current_token(store, entity),
        )
    elif isinstance(entity, StyleBible):
        store.create(entity)
        review_id = "r1"
        store.save(Review(
            review_id=review_id,
            target_id=entity.project_id,
            target_version=entity.version,
            created_at=datetime.now(UTC),
            approval_state=ShotStatus.APPROVED,
        ), expected_version=0)
        entity = approve_style_bible(
            store,
            entity.project_id,
            review_id=review_id,
            expected_version=current_token(store, entity),
        )
    else:
        store.create(entity)
    changes = {**entity.next_version(), "approval_review_id": "r1"}
    if isinstance(entity, Shot):
        changes["status"] = ShotStatus.DRAFT
    with pytest.raises(JobError, match="never inherits approval"):
        store.revise(entity.model_copy(update=changes), expected_version=current_token(store, entity))
    # A fresh pointer also cannot pre-approve a successor.
    with pytest.raises(JobError, match="never inherits approval"):
        store.revise(entity.model_copy(update={**changes, "approval_review_id": "r2"}),
                     expected_version=current_token(store, entity))
    changes["approval_review_id"] = None
    store.revise(entity.model_copy(update=changes), expected_version=current_token(store, entity))


@pytest.mark.parametrize(("field", "value"), [
    ("framing", "overwrite"),
    ("approval_notes", "overwrite"),
    ("framing_status", FramingStatus.PENDING),
    ("status", ShotStatus.PLANNED),
    ("approval_review_id", "r1"),
])
def test_update_reports_creator_authored_conflicts(store, field, value):
    shot = make_shot(creator_authored={field})
    store.create(shot)
    # Removing the protection in the same request must not bypass the stored set.
    changed = shot.model_copy(update={field: value, "creator_authored": set()})
    with pytest.raises(JobError, match=f"FR-29 creator_authored conflict: {field}") as err:
        store.update(changed, expected_version=1)
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Shot, shot.shot_id, episode_id=shot.episode_id) == shot


@pytest.mark.parametrize("status", [s for s in ShotStatus if s is not ShotStatus.DRAFT])
def test_shot_creation_requires_draft(store, root, status):
    # Keep this test on the status guard: an otherwise invalid APPROVED payload
    # without a review pointer must still fail because new Shots start as drafts.
    shot = make_shot().model_copy(update={"status": status, "approval_review_id": None})
    with pytest.raises(JobError, match="draft"):
        store.create(shot)
    assert not list(root.path.rglob("*.json"))


@pytest.mark.parametrize("status", [s for s in ShotStatus if s is not ShotStatus.DRAFT])
def test_shot_revision_requires_draft(store, status):
    shot = make_shot()
    store.create(shot)
    successor = make_shot(**shot.next_version(), status=status,
                          approval_review_id="r1" if status is ShotStatus.APPROVED else None)
    with pytest.raises(JobError):
        store.revise(successor, expected_version=1)
    assert store.versions(Shot, shot.shot_id, episode_id=shot.episode_id) == [1]


# -- save concurrency tokens --------------------------------------------------------------


@pytest.mark.parametrize("entity", [
    Project(project_id="p1", title="original"),
    Episode(episode_id="ep01", project_id="p1", number=1, duration_target_sec=180),
    Timeline(episode_id="ep01"),
])
def test_save_checks_and_persists_expected_version(store, root, entity):
    token = store.save(entity, expected_version=0)
    assert token == 1
    with pytest.raises(VersionConflict) as err:
        store.save(entity, expected_version=0)
    assert (err.value.expected, err.value.actual, err.value.current) == (0, 1, entity)
    reopened = ProjectStore(ProjectRoot(root.path))
    token = reopened.save(entity, expected_version=token)
    assert token == 2
    with pytest.raises(VersionConflict) as err:
        store.save(entity, expected_version=1)
    assert (err.value.expected, err.value.actual, err.value.current) == (1, 2, entity)
    record_id = getattr(entity, "episode_id", None)
    assert store.load(type(entity), record_id) == entity
    assert store.load_with_version(type(entity), record_id) == (entity, 2)


def test_save_requires_expected_version(store):
    with pytest.raises(TypeError, match="expected_version"):
        store.save(Project(project_id="p1", title="t"))


def test_save_missing_record_with_nonzero_expectation_is_not_found(store):
    with pytest.raises(RecordNotFound):
        store.save(Project(project_id="p1", title="t"), expected_version=1)


def test_save_legacy_record_starts_at_version_one(store, root):
    project = Project(project_id="p1", title="legacy")
    (root.path / "project.json").write_text(project.model_dump_json(), encoding="utf-8")
    assert store.save(project, expected_version=1) == 2
    assert store.load(Project) == project


def test_saved_snapshot_token_prevents_overwriting_an_intervening_save(store, root):
    store.save(Project(project_id="p1", title="initial"), expected_version=0)
    project, token = store.load_with_version(Project)
    other = ProjectStore(ProjectRoot(root.path))
    other.save(project.model_copy(update={"title": "other writer"}), expected_version=token)
    with pytest.raises(VersionConflict) as err:
        store.save(project.model_copy(update={"title": "late writer"}), expected_version=token)
    assert err.value.current.title == "other writer"
    assert store.load_with_version(Project) == (err.value.current, 2)


def test_versioned_snapshot_exposes_domain_version(store):
    shot = make_shot()
    store.create(shot)
    successor = shot.model_copy(update=shot.next_version())
    store.revise(successor, expected_version=1)
    assert store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id) == (successor, 2)
    assert store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id,
                                   version=1) == (shot, 1)


# -- temporary file cleanup ---------------------------------------------------------------


@pytest.mark.parametrize("stage", ["write", "flush", "fsync", "replace", "containment"])
def test_every_io_failure_cleans_temp_file(store, root, monkeypatch, stage):
    import builtins

    from orchestrator.platform.containment import ContainmentError
    from orchestrator.store import json_store

    store.create(make_shot())
    original = (shot_dir(root) / "v0001.json").read_bytes()

    def fail(*args, **kwargs):
        raise OSError(f"simulated {stage} failure")

    if stage in {"write", "flush"}:
        class FailingFile:
            def __init__(self, *args, **kwargs):
                # deliberate: this double owns the handle and implements
                # __enter__/__exit__ itself
                self.handle = builtins.open(*args, **kwargs)  # noqa: SIM115

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, text):
                self.handle.write(text[:5])
                if stage == "write":
                    fail()

            def flush(self):
                fail()

        monkeypatch.setattr(json_store, "open", FailingFile, raising=False)
    elif stage == "fsync":
        monkeypatch.setattr(os, "fsync", fail)
    elif stage == "replace":
        monkeypatch.setattr(os, "replace", fail)
    else:
        resolve = store._resolve

        def reject_target(relative):
            if not relative.endswith(".tmp"):
                raise ContainmentError(relative, "simulated target rejection")
            return resolve(relative)

        monkeypatch.setattr(store, "_resolve", reject_target)

    with pytest.raises(JobError):
        # Use the low-level write so the containment failure happens after staging.
        store._write("episodes/ep01/shots/EP01-S03/v0001.json", make_shot())
    assert (shot_dir(root) / "v0001.json").read_bytes() == original
    assert not list(root.path.rglob("*.tmp"))


def test_exhausted_permission_retries_clean_temp_file(store, root, monkeypatch):
    from orchestrator.store import json_store

    calls = []

    def denied(*args):
        calls.append(args)
        raise PermissionError("target locked")

    monkeypatch.setattr(os, "replace", denied)
    monkeypatch.setattr(json_store.time, "sleep", lambda _: None)
    with pytest.raises(JobError):
        store.create(make_shot())
    assert len(calls) == json_store._REPLACE_ATTEMPTS
    assert not list(root.path.rglob("*.tmp"))


def test_unexpected_failure_also_cleans_temp_file(store, root, monkeypatch):
    def fail(*args):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(RuntimeError):
        store.create(make_shot())
    assert not list(root.path.rglob("*.tmp"))


# -- declared path fields and cross-instance locking ---------------------------------------


def test_prose_starting_with_slash_is_storable(store):
    project = Project(project_id="p1", title="/ Night Shift", story_brief="C:/ narrative")
    store.save(project, expected_version=0)
    shot = make_shot(narrative_beat="/ Something moves", framing="/ wide")
    store.create(shot)
    script = Script(script_id="s1", episode_id="ep01", logline="/ logline", beats=[
        Beat(beat_id="b1", target_sec=10, subtlety=1, narration_text="/ narration"),
    ])
    store.create(script)
    assert store.load(Project) == project
    assert store.load(Shot, shot.shot_id, episode_id=shot.episode_id) == shot
    assert store.load(Script, script.script_id, episode_id=script.episode_id) == script


@pytest.mark.parametrize(("entity", "field"), [
    (Location(location_id="hall", blend_path="/absolute.blend"), "blend_path"),
    (make_shot(audio_refs=["audio/relative.wav", "C:/absolute.wav"]), "audio_refs[1]"),
    (make_shot(framing_panel_ref="/absolute.png"), "framing_panel_ref"),
    (Timeline(episode_id="ep01", audio_tracks=[r"\\server\share\audio.wav"]), "audio_tracks[0]"),
])
def test_declared_paths_are_checked_including_lists(store, root, entity, field):
    with pytest.raises(JobError) as err:
        if isinstance(entity, Timeline):
            store.save(entity, expected_version=0)
        else:
            store.create(entity)
    assert err.value.kind is JobErrorKind.INVALID_MANIFEST
    assert field in str(err.value)
    assert not list(root.path.rglob("*.json"))


@pytest.mark.parametrize("operation", ["create", "revise", "save"])
def test_stores_for_same_resolved_root_serialize_writes(store, root, operation):
    second = ProjectStore(ProjectRoot(root.path / "."))
    assert store._lock is second._lock
    if operation == "revise":
        store.create(make_shot())
    barrier = threading.Barrier(2)

    def mutate(writer, label):
        barrier.wait(timeout=5)
        try:
            if operation == "save":
                writer.save(Project(project_id="p1", title=label), expected_version=0)
            elif operation == "create":
                writer.create(make_shot(narrative_beat=label))
            else:
                writer.revise(make_shot(version=2, supersedes=1, narrative_beat=label),
                              expected_version=1)
        except VersionConflict as exc:
            return exc
        return label

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(mutate, writer, label)
                   for writer, label in ((store, "first"), (second, "second"))]
        results = [future.result(timeout=5) for future in futures]
    winner, = [result for result in results if isinstance(result, str)]
    conflict, = [result for result in results if isinstance(result, VersionConflict)]
    if operation == "save":
        assert store.load(Project).title == winner
        assert conflict.actual == 1
    else:
        assert store.load(Shot, "EP01-S03", episode_id="ep01").narrative_beat == winner
        assert store.versions(Shot, "EP01-S03", episode_id="ep01") == (
            [1, 2] if operation == "revise" else [1]
        )
        assert conflict.actual == (2 if operation == "revise" else 1)
    assert not list(root.path.rglob("*.tmp"))


class TestPathFieldContainment:
    r"""Declared path fields go through containment, not a local regex.

    The store previously used its own `^(?:[A-Za-z]:[\/]|\\|/)` check, which
    caught absolute paths but passed traversal, drive-relative and reserved
    device names. Those values are opened later, so the weaker check was a
    validator differential (FR-25, CLAUDE.md invariant 4).
    """

    @pytest.mark.parametrize(
        "bad_path",
        [
            "../../../outside.blend",
            r"..\..\outside.blend",
            "C:evil.blend",
            r"C:\absolute.blend",
            r"\\server\share\x.blend",
            "/etc/passwd",
            "CON",
            "locations/../../escape.blend",
        ],
    )
    def test_unsafe_path_field_is_rejected_on_write(self, store, bad_path):
        with pytest.raises(JobError) as caught:
            store.create(Location(location_id="L-bad", blend_path=bad_path))
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST

    def test_safe_relative_path_is_accepted(self, store):
        store.create(Location(location_id="L-ok", blend_path="locations/hall.blend"))
        assert store.load(Location, "L-ok").blend_path == "locations/hall.blend"

    def test_unsafe_path_is_rejected_on_load(self, store, tmp_path):
        """A hand-edited or imported project folder is untrusted input."""
        store.create(Location(location_id="L-edit", blend_path="locations/hall.blend"))
        target = next(tmp_path.rglob("locations/L-edit/v0001.json"))
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["blend_path"] = "../../../outside.blend"
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(JobError) as caught:
            store.load(Location, "L-edit")
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
