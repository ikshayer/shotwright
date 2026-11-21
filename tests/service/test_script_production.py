"""Blind authoring contract tests for structured Script-to-scene derivation."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.domain.entities import CameraProfile, Episode, Project, Script, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.store.json_store import ProjectStore, VersionConflict


def _store(*, duration_target_sec=4):
    base = Path.cwd() / "_work" / "production-feature-tests" / ("script-" + uuid.uuid4().hex)
    root = ProjectRoot(Path.cwd())
    root.make_dir(base.relative_to(Path.cwd()).as_posix())
    store = ProjectStore(ProjectRoot(base))
    store.save(Project(project_id="p1", title="authoring fixture"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=duration_target_sec), expected_version=0)
    store.create(CameraProfile(camera_id="cam1", profile_name="Fixture CCTV"))
    return store


def _script(store, *, direction=None):
    from orchestrator.domain.entities import Beat, BeatSceneDirection, Location
    room = Location(location_id="room1")
    beat = Beat(beat_id="b1", target_sec=4, subtlety=2, scene_direction=direction or BeatSceneDirection(
        location_id="room1", camera_profile="cam1", framing="wide", event_sec=2,
    ))
    record = Script(script_id="script1", episode_id="ep1", logline="A quiet room",
                    beats=[beat.model_copy(update={"narration_text": "The night clerk notices a shape."})],
                    beat_to_shot={"b1": "shot1"}, scenes=[room])
    store.create(record)
    return record


def _derive(store, *, script_version=1, episode_version=1, shot_versions=None, location_versions=None):
    from orchestrator.service.script_authoring import derive_script_scene_plan
    return derive_script_scene_plan(
        store, episode_id="ep1", script_id="script1", expected_version=script_version,
        expected_episode_version=episode_version,
        expected_shot_versions={"shot1": 0} if shot_versions is None else shot_versions,
        expected_location_versions={"room1": 0} if location_versions is None else location_versions,
    )


def _files(store):
    return {path.relative_to(store._root.path).as_posix(): path.read_bytes()
            for path in store._root.path.rglob("*") if path.is_file()}


def test_structured_derivation_creates_real_location_and_shot_from_direction():
    store = _store()
    _script(store)
    result = _derive(store)
    shot = store.load(Shot, "shot1", episode_id="ep1")
    from orchestrator.domain.entities import Location
    room = store.load(Location, "room1")
    assert shot.duration_sec == 4 and shot.camera_profile == "cam1"
    assert shot.framing == "wide" and shot.event_sec == 2 and shot.location_id == "room1"
    assert shot.beat_id == "b1" and shot.narrative_beat
    assert room.version == 1
    assert result["conflicts"] == [] and result["changed"]
    assert shot.approval_review_id is None


def test_exact_derivation_rerun_is_noop_without_new_versions_or_journal():
    store = _store()
    _script(store)
    first = _derive(store)
    from orchestrator.domain.entities import Location
    shot = store.load(Shot, "shot1", episode_id="ep1")
    room = store.load(Location, "room1")
    before = _files(store)
    second = _derive(store, episode_version=store.load_with_version(Episode, "ep1")[1],
                     shot_versions={"shot1": shot.version}, location_versions={"room1": room.version})
    assert second["changed"] == []
    assert second["journal_path"] is None
    assert _files(store) == before
    assert first["changed"]


@pytest.mark.parametrize(("script_version", "episode_version"), [(99, 1), (1, 99)])
def test_stale_script_or_episode_tokens_refuse_before_any_write(script_version, episode_version):
    store = _store()
    _script(store)
    before = _files(store)
    with pytest.raises(VersionConflict):
        _derive(store, script_version=script_version, episode_version=episode_version)
    assert _files(store) == before


@pytest.mark.parametrize("shot_versions,location_versions", [
    ({}, {"room1": 0}), ({"unknown": 0}, {"room1": 0}),
    ({"shot1": 0}, {}), ({"shot1": 0}, {"unknown": 0}),
])
def test_inexact_derived_version_keysets_refuse_as_invalid_manifest(shot_versions, location_versions):
    from orchestrator.platform.errors import JobError, JobErrorKind
    store = _store()
    _script(store)
    before = _files(store)
    with pytest.raises(JobError) as caught:
        _derive(store, shot_versions=shot_versions, location_versions=location_versions)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert _files(store) == before


def test_creator_authored_equal_direction_is_preserved_but_conflicting_direction_refused():
    store = _store()
    _script(store)
    _derive(store)
    original = store.load(Shot, "shot1", episode_id="ep1")
    from orchestrator.domain.entities import Location
    location = store.load(Location, "room1")
    edited = original.model_copy(update={**original.next_version(), "creator_authored": {"framing"}})
    store.revise(edited, expected_version=original.version)
    script = store.load(Script, "script1", episode_id="ep1")
    token = store.load_with_version(Script, "script1", episode_id="ep1")[1]
    changed_script = script.model_copy(update={**script.next_version(), "beats": [
        script.beats[0].model_copy(update={"scene_direction": script.beats[0].scene_direction.model_copy(update={"framing": "tight"})})
    ]})
    from orchestrator.service.dependencies import revise_script
    revise_script(store, changed_script, expected_version=token)
    token = store.load_with_version(Script, "script1", episode_id="ep1")[1]
    before = _files(store)
    from orchestrator.platform.errors import JobError, JobErrorKind
    with pytest.raises(JobError) as caught:
        _derive(store, script_version=token,
                episode_version=store.load_with_version(Episode, "ep1")[1],
                shot_versions={"shot1": store.load_with_version(Shot, "shot1", episode_id="ep1")[1]},
                location_versions={"room1": location.version})
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert _files(store) == before
    assert store.load(Shot, "shot1", episode_id="ep1").framing == "wide"


def test_scene_direction_event_is_relative_to_beat_target_and_anomaly_requires_event():
    from orchestrator.domain.entities import Beat, BeatSceneDirection, Location
    store = _store(duration_target_sec=1)
    beat = Beat(beat_id="b1", target_sec=1, subtlety=2,
                scene_direction=BeatSceneDirection(location_id="room1", event_sec=2))
    before = _files(store)
    with pytest.raises(ValidationError):
        Script(script_id="script1", episode_id="ep1", logline="bad event",
               beats=[beat], beat_to_shot={"b1": "shot1"},
               scenes=[Location(location_id="room1")])
    assert _files(store) == before
    with pytest.raises(ValidationError):
        BeatSceneDirection(location_id="room1", anomaly={"kind": "shadow"})


def test_incomplete_mapping_and_missing_direction_refuse_without_writes():
    store = _store()
    from orchestrator.domain.entities import Beat, Location
    record = Script(script_id="script1", episode_id="ep1", logline="incomplete",
                    beats=[Beat(beat_id="b1", target_sec=4, subtlety=2)], scenes=[Location(location_id="room1")])
    store.create(record)
    before = _files(store)
    from orchestrator.platform.errors import JobError, JobErrorKind
    with pytest.raises(JobError) as caught:
        _derive(store)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert _files(store) == before


def test_stale_script_mutation_token_is_a_version_conflict_before_writes():
    store = _store()
    _script(store)
    before = _files(store)
    with pytest.raises(VersionConflict):
        _derive(store, script_version=2)
    assert _files(store) == before


def _revise_embedded_scene(store, *, change_direction=False):
    script, token = store.load_with_version(Script, "script1", episode_id="ep1")
    room = script.scenes[0].model_copy(update={"scene_look": "low_key"})
    beats = list(script.beats)
    if change_direction:
        direction = beats[0].scene_direction.model_copy(update={"framing": "tight"})
        beats[0] = beats[0].model_copy(update={"scene_direction": direction})
    successor = script.model_copy(update={
        **script.next_version(), "scenes": [room], "beats": beats,
    })
    from orchestrator.service.dependencies import revise_script
    revise_script(store, successor, expected_version=token)
    return store.load_with_version(Script, "script1", episode_id="ep1")


def _approve_derived_shot(store, shot, *, review_id="review-shot1"):
    from datetime import UTC, datetime

    from orchestrator.domain.base import FramingStatus, ShotStatus
    from orchestrator.domain.entities import Review
    from orchestrator.service.approval import approve_shot
    token = store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id)[1]
    current = shot
    for state in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        values = {"status": state}
        if state is ShotStatus.BUILDING:
            values["framing_status"] = FramingStatus.APPROVED
        store.update(current.model_copy(update=values), expected_version=token)
        current, token = store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id)
    store.save(Review(review_id=review_id, target_id=shot.shot_id,
                      target_version=current.version, created_at=datetime.now(UTC),
                      target_kind="shot", episode_id=shot.episode_id, actor="creator",
                      approval_state=ShotStatus.APPROVED), expected_version=0)
    return approve_shot(store, shot.shot_id, episode_id=shot.episode_id,
                        review_id=review_id, expected_version=token)


def test_scene_only_script_revision_stales_approved_shared_location_and_derivation_appends_successor():
    from orchestrator.domain.base import ShotStatus
    from orchestrator.domain.entities import Episode, Location, ShotOutput, ShotOutputKind
    store = _store()
    _script(store)
    _derive(store)
    shot = store.load(Shot, "shot1", episode_id="ep1")
    approved = _approve_derived_shot(store, shot)
    root = store._root
    output_data = b"retained approved master"
    output_path = "renders/approved-master.mkv"
    root.write_bytes(output_path, output_data)
    old_output = ShotOutput(
        output_id="approved-master", episode_id="ep1", shot_id="shot1",
        record_version=approved.version, kind=ShotOutputKind.MASTER, relative_path=output_path,
        content_sha256=hashlib.sha256(output_data).hexdigest(), engine_id="BLENDER_EEVEE",
        engine_build="fixture-build",
    )
    store.append_shot_output(old_output, expected_version=0)
    store.save(Episode(episode_id="ep2", project_id="p1", number=2, duration_target_sec=3), expected_version=0)
    from orchestrator.service.dependencies import propose_shots
    shared = Shot(shot_id="other-episode-shot", episode_id="ep2", location_id="room1", duration_sec=3)
    propose_shots(store, [shared])
    shared = store.load(Shot, shared.shot_id, episode_id="ep2")
    approved_shared = _approve_derived_shot(store, shared, review_id="review-shared")

    _revised_script, script_token = _revise_embedded_scene(store)
    stale = store.load(Shot, "shot1", episode_id="ep1")
    still_approved_shared = store.load(Shot, "other-episode-shot", episode_id="ep2")
    assert stale.status is ShotStatus.STALE and stale.approval_review_id == "review-shot1"
    assert still_approved_shared.status is ShotStatus.APPROVED
    assert still_approved_shared.approval_review_id == "review-shared"
    assert store.list_shot_outputs("shot1", episode_id="ep1") == (old_output,)

    result = _derive(store, script_version=script_token,
                     episode_version=store.load_with_version(Episode, "ep1")[1],
                     shot_versions={"shot1": store.load_with_version(Shot, "shot1", episode_id="ep1")[1]},
                     location_versions={"room1": store.load_with_version(Location, "room1")[1]})
    successor = store.load(Shot, "shot1", episode_id="ep1")
    assert successor.version == approved.version + 1 and successor.supersedes == approved.version
    assert successor.status is ShotStatus.DRAFT and successor.approval_review_id is None
    assert store.list_shot_outputs("shot1", episode_id="ep1") == (old_output,)
    stale_shared = store.load(Shot, "other-episode-shot", episode_id="ep2")
    assert stale_shared.status is ShotStatus.STALE
    assert stale_shared.approval_review_id == approved_shared.approval_review_id
    assert result["changed"]
    assert root.read_bytes(output_path) == output_data


def test_combined_location_and_direction_revision_uses_fresh_tokens_and_initial_stale_tokens_conflict():
    from orchestrator.domain.entities import Episode, Location
    from orchestrator.store.json_store import VersionConflict
    store = _store()
    _script(store)
    _derive(store)
    shot = store.load(Shot, "shot1", episode_id="ep1")
    _approve_derived_shot(store, shot)
    old_shot_token = store.load_with_version(Shot, "shot1", episode_id="ep1")[1]
    _updated_script, script_token = _revise_embedded_scene(store, change_direction=True)
    episode_token = store.load_with_version(Episode, "ep1")[1]
    before = _files(store)
    with pytest.raises(VersionConflict):
        _derive(store, script_version=script_token, episode_version=episode_token,
                shot_versions={"shot1": old_shot_token},
                location_versions={"room1": store.load_with_version(Location, "room1")[1]})
    assert _files(store) == before

    result = _derive(store, script_version=script_token, episode_version=episode_token,
                     shot_versions={"shot1": store.load_with_version(Shot, "shot1", episode_id="ep1")[1]},
                     location_versions={"room1": store.load_with_version(Location, "room1")[1]})
    current = store.load(Shot, "shot1", episode_id="ep1")
    room = store.load(Location, "room1")
    assert current.framing == "tight" and room.scene_look == "low_key"
    assert result["changed"]


def test_beat_timing_edit_updates_successor_duration_without_changing_direction_fields():
    from orchestrator.domain.entities import Location
    from orchestrator.service.dependencies import revise_script
    store = _store()
    _script(store)
    _derive(store)
    before = store.load(Shot, "shot1", episode_id="ep1")
    script, script_token = store.load_with_version(Script, "script1", episode_id="ep1")
    changed_beat = script.beats[0].model_copy(update={"target_sec": 6.5})
    successor = script.model_copy(update={**script.next_version(), "beats": [changed_beat]})
    revise_script(store, successor, expected_version=script_token)
    current_script, current_script_token = store.load_with_version(Script, "script1", episode_id="ep1")
    _derive(store, script_version=current_script_token,
            episode_version=store.load_with_version(Episode, "ep1")[1],
            shot_versions={"shot1": store.load_with_version(Shot, "shot1", episode_id="ep1")[1]},
            location_versions={"room1": store.load_with_version(Location, "room1")[1]})
    after = store.load(Shot, "shot1", episode_id="ep1")
    assert after.version == before.version + 1 and after.supersedes == before.version
    assert after.duration_sec == 6.5
    assert (after.camera_profile, after.framing, after.event_sec, after.location_id, after.beat_id) == (
        before.camera_profile, before.framing, before.event_sec, before.location_id, before.beat_id,
    )
    assert current_script.beats[0].scene_direction == script.beats[0].scene_direction


def test_anomaly_full_path_geometry_is_validated_before_any_derivation_writes():
    from orchestrator.domain.entities import Beat, BeatSceneDirection, Location
    from orchestrator.platform.errors import JobError, JobErrorKind
    from tests.adapters.blender.test_anomaly_plan_contract import _valid_anomaly

    store = _store(duration_target_sec=5)
    anomaly = _valid_anomaly(
        "linear_walk_path", height_m=1.7, width_m=0.3, depth_m=0.2,
        minimum_distance_m=2.0, maximum_on_frame_height_px=5000,
        path=((0.0, 0.0, 0.0), (-2.48, -1.85, 0.0)),
    )
    beat = Beat(beat_id="b1", target_sec=5, subtlety=2,
                scene_direction=BeatSceneDirection(
                    location_id="room1", camera_profile="cam1", event_sec=1, anomaly=anomaly,
                ))
    store.create(Script(script_id="script1", episode_id="ep1", logline="walk",
                        beats=[beat], beat_to_shot={"b1": "shot1"},
                        scenes=[Location(location_id="room1")]))
    before = _files(store)
    with pytest.raises(JobError) as caught:
        _derive(store)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert _files(store) == before
