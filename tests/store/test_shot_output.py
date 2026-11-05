from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from orchestrator.domain.entities import Episode, Project, Shot, ShotOutput, ShotOutputKind
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


@pytest.fixture
def project(tmp_path):
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    store.save(Project(project_id="project-1", title="Test"), expected_version=0)
    store.save(
        Episode(episode_id="episode-1", project_id="project-1", number=1,
                duration_target_sec=10),
        expected_version=0,
    )
    store.create(Shot(shot_id="shot-1", episode_id="episode-1", duration_sec=2))
    return root, store


def artifact(root: ProjectRoot, name: str, payload: bytes = b"render") -> tuple[str, str]:
    relative = f"runs/{name}.mp4"
    root.write_bytes(relative, payload)
    return relative, hashlib.sha256(payload).hexdigest()


def output(root: ProjectRoot, *, output_id: str | None = None, record_version: int = 1,
           kind: ShotOutputKind = ShotOutputKind.MASTER,
           name: str | None = None) -> ShotOutput:
    relative, digest = artifact(root, name or output_id or uuid.uuid4().hex)
    return ShotOutput(
        output_id=output_id or uuid.uuid4().hex,
        episode_id="episode-1",
        shot_id="shot-1",
        record_version=record_version,
        kind=kind,
        relative_path=relative,
        content_sha256=digest,
        engine_id=None if kind is ShotOutputKind.STILL else "BLENDER_EEVEE",
        engine_build=None if kind is ShotOutputKind.STILL else "build-hash",
        profile_semver=None,
        filter_graph_hash=None,
        pinned_binaries=None,
    )


def test_append_token_is_separate_from_content_version_and_historical_outputs_are_retained(project):
    root, store = project
    shot = store.load(Shot, "shot-1", episode_id="episode-1")
    successor = shot.model_copy(update={"version": 2, "supersedes": 1})
    store.revise(successor, expected_version=1)

    first = output(root, output_id="attempt-a", record_version=1, name="attempt-a")
    second = output(root, output_id="attempt-b", record_version=1, name="attempt-b")
    assert store.append_shot_output(first, expected_version=0) == 1
    assert store.append_shot_output(second, expected_version=1) == 2

    records = store.list_shot_outputs("shot-1", episode_id="episode-1")
    assert [item.output_id for item in records] == ["attempt-a", "attempt-b"]
    assert all(item.record_version == 1 for item in records)
    assert store.load(Shot, "shot-1", episode_id="episode-1").version == 2


def test_filtering_by_version_and_kind(project):
    root, store = project
    store.append_shot_output(output(root, output_id="master", name="master"), expected_version=0)
    store.append_shot_output(
        output(root, output_id="still", kind=ShotOutputKind.STILL, name="still"),
        expected_version=1,
    )
    assert [x.output_id for x in store.list_shot_outputs(
        "shot-1", episode_id="episode-1", record_version=1,
        kind=ShotOutputKind.MASTER,
    )] == ["master"]


def test_stale_ledger_token_conflicts_without_consuming_next_token(project):
    root, store = project
    first = output(root, output_id="first", name="first")
    store.append_shot_output(first, expected_version=0)
    with pytest.raises(VersionConflict) as caught:
        store.append_shot_output(output(root, output_id="second", name="second"),
                                 expected_version=0)
    assert caught.value.expected == 0
    assert caught.value.actual == 1
    assert store.append_shot_output(output(root, output_id="third", name="third"),
                                    expected_version=1) == 2


def test_missing_episode_shot_and_content_version_fail(project):
    root, store = project
    record = output(root, output_id="missing", name="missing")
    with pytest.raises(RecordNotFound):
        store.append_shot_output(record.model_copy(update={"episode_id": "absent"}),
                                 expected_version=0)
    with pytest.raises(RecordNotFound):
        store.append_shot_output(record.model_copy(update={"shot_id": "absent"}),
                                 expected_version=0)
    with pytest.raises(RecordNotFound):
        store.append_shot_output(record.model_copy(update={"record_version": 2}),
                                 expected_version=0)


def test_duplicate_output_id_does_not_replace_prior_record(project):
    root, store = project
    first = output(root, output_id="duplicate", name="first")
    store.append_shot_output(first, expected_version=0)
    second = output(root, output_id="duplicate", name="second")
    with pytest.raises(JobError):
        store.append_shot_output(second, expected_version=1)
    records = store.list_shot_outputs("shot-1", episode_id="episode-1")
    assert len(records) == 1
    assert records[0].content_sha256 == first.content_sha256


def test_append_rejects_missing_artifact_and_hash_mismatch(project):
    root, store = project
    missing_path = "runs/never-created.mp4"
    missing = ShotOutput(
        output_id="missing-artifact", episode_id="episode-1", shot_id="shot-1",
        record_version=1, kind=ShotOutputKind.MASTER, relative_path=missing_path,
        content_sha256=hashlib.sha256(b"expected bytes").hexdigest(),
        engine_id="BLENDER_EEVEE", engine_build="build-hash",
        profile_semver=None, filter_graph_hash=None, pinned_binaries=None,
    )
    with pytest.raises(JobError):
        store.append_shot_output(missing, expected_version=0)

    altered = output(root, output_id="altered", name="altered")
    altered = altered.model_copy(update={"content_sha256": "0" * 64})
    with pytest.raises(JobError):
        store.append_shot_output(altered, expected_version=0)


def test_listing_fails_on_missing_or_changed_artifact(project):
    root, store = project
    record = output(root, output_id="tamper", name="tamper")
    store.append_shot_output(record, expected_version=0)
    root.write_bytes(record.relative_path, b"changed")
    with pytest.raises(JobError):
        store.list_shot_outputs("shot-1", episode_id="episode-1")


def test_listing_fails_loudly_on_malformed_record(project):
    root, store = project
    record = output(root, output_id="malformed", name="malformed")
    store.append_shot_output(record, expected_version=0)
    record_file = root.resolve(
        "episodes/episode-1/shots/shot-1/outputs/o00000001-malformed.json"
    )
    root.write_text(
        "episodes/episode-1/shots/shot-1/outputs/o00000001-malformed.json", "{not json"
    )
    assert record_file.exists()
    with pytest.raises(JobError):
        store.list_shot_outputs("shot-1", episode_id="episode-1")


def test_listing_fails_on_malformed_unexpected_ledger_filename(project):
    root, store = project
    root.write_text(
        "episodes/episode-1/shots/shot-1/outputs/unexpected.json", "{not json"
    )
    with pytest.raises(JobError):
        store.list_shot_outputs("shot-1", episode_id="episode-1")


def test_listing_fails_on_filename_token_mismatch(project):
    root, store = project
    store.append_shot_output(output(root, output_id="token", name="token"), expected_version=0)
    relative = "episodes/episode-1/shots/shot-1/outputs/o00000001-token.json"
    payload = json.loads(root.resolve(relative).read_text(encoding="utf-8"))
    payload["_mutation_token"] = 7
    root.write_text(relative, json.dumps(payload))
    with pytest.raises(JobError):
        store.list_shot_outputs("shot-1", episode_id="episode-1")


def test_listing_rejects_false_predecessor_metadata(project):
    root, store = project
    store.append_shot_output(output(root, output_id="predecessor", name="predecessor"),
                             expected_version=0)
    relative = "episodes/episode-1/shots/shot-1/outputs/o00000001-predecessor.json"
    payload = json.loads(root.resolve(relative).read_text(encoding="utf-8"))
    payload["_predecessor_token"] = False
    root.write_text(relative, json.dumps(payload))

    with pytest.raises(JobError):
        store.list_shot_outputs("shot-1", episode_id="episode-1")


def test_append_rejects_prior_receipt_with_tampered_episode_identity(project):
    root, store = project
    store.append_shot_output(output(root, output_id="identity", name="identity"),
                             expected_version=0)
    relative = "episodes/episode-1/shots/shot-1/outputs/o00000001-identity.json"
    payload = json.loads(root.resolve(relative).read_text(encoding="utf-8"))
    payload["episode_id"] = "other-episode"
    root.write_text(relative, json.dumps(payload))

    with pytest.raises(JobError):
        store.append_shot_output(output(root, output_id="next", name="next"),
                                 expected_version=1)


def test_listing_rejects_nonexistent_positive_content_version(project):
    root, store = project
    store.append_shot_output(output(root, output_id="unknown-version", name="unknown-version"),
                             expected_version=0)
    relative = "episodes/episode-1/shots/shot-1/outputs/o00000001-unknown-version.json"
    payload = json.loads(root.resolve(relative).read_text(encoding="utf-8"))
    payload["record_version"] = 999
    root.write_text(relative, json.dumps(payload))

    with pytest.raises(RecordNotFound):
        store.list_shot_outputs("shot-1", episode_id="episode-1")


def test_listing_rejects_regular_file_at_outputs_ledger_path(project):
    root, store = project
    root.write_text("episodes/episode-1/shots/shot-1/outputs", "not a directory")

    with pytest.raises(JobError) as caught:
        store.list_shot_outputs("shot-1", episode_id="episode-1")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def test_listing_fails_on_token_gap(project):
    root, store = project
    first = output(root, output_id="gap-1", name="gap-1")
    store.append_shot_output(first, expected_version=0)
    gap_record = output(root, output_id="gap-3", name="gap-3")
    gap_relative = "episodes/episode-1/shots/shot-1/outputs/o00000003-gap-3.json"
    root.write_text(gap_relative, json.dumps({
        **gap_record.model_dump(mode="json"),
        "_mutation_token": 3,
        "_predecessor_token": 2,
    }))
    with pytest.raises(JobError):
        store.list_shot_outputs("shot-1", episode_id="episode-1")


def test_legacy_schema_one_shot_without_metadata_reads_without_rewrite(project):
    root, store = project
    relative = "episodes/episode-1/shots/shot-1/v0001.json"
    data = json.loads(root.resolve(relative).read_text(encoding="utf-8"))
    data.pop("_mutation_token", None)
    data.pop("_predecessor_token", None)
    legacy_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    root.write_bytes(relative, legacy_bytes)

    assert store.load(Shot, "shot-1", episode_id="episode-1").version == 1
    assert root.resolve(relative).read_bytes() == legacy_bytes
