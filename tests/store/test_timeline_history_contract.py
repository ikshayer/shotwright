"""Timeline history contract tests (R6 store behaviour).

Authored from ``orchestrator/service/APPROVAL_INTEGRITY_CONTRACT.md`` R6 alone.

Not testable through the public API: "a snapshot at or below the head token is immutable and
a write that would change its bytes raises INVALID_MANIFEST". Every save produces token
head + 1, which is always above the head, so the guard is unreachable without reaching into
the store. What is tested instead is its observable consequence: snapshots at or below the
head are never altered by later saves, and one above the head is replaceable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.adapters.export.snapshot import plan_snapshot
from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import Episode, Project, Review, Timeline, TimelineEntry
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import record_review
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict

EPISODE = "ep1"
HEAD = f"episodes/{EPISODE}/edits/timeline.json"


def snapshot_path(token: int, episode_id: str = EPISODE) -> str:
    return f"episodes/{episode_id}/edits/timelines/{token:06d}.json"


def timeline(duration: float, episode_id: str = EPISODE) -> Timeline:
    return Timeline(episode_id=episode_id, entries=[
        TimelineEntry(type="card", start_sec=0, duration_sec=duration)])


def payload(root: ProjectRoot, relative: str) -> dict:
    """Entity JSON with store metadata removed, so head and snapshot compare on content."""
    data = json.loads(root.read_text(relative))
    data.pop("_store_version", None)
    return data


@pytest.fixture
def root(tmp_path: Path) -> ProjectRoot:
    return ProjectRoot(tmp_path)


@pytest.fixture
def store(root: ProjectRoot) -> ProjectStore:
    return ProjectStore(root)


def write_legacy_head(root: ProjectRoot, cut: Timeline, token: int | None) -> None:
    data = {**cut.model_dump(mode="json"), "schema_version": 1}
    if token is not None:
        data["_store_version"] = token
    root.write_text(HEAD, json.dumps(data, indent=4) + "\n")


# --- a snapshot is written for every save -------------------------------------------------


def test_every_timeline_save_writes_a_snapshot_named_by_its_token(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    first = store.save(timeline(1.0), expected_version=0)
    second = store.save(timeline(2.0), expected_version=first)
    third = store.save(timeline(3.0), expected_version=second)

    assert (first, second, third) == (1, 2, 3)
    for token, duration in ((1, 1.0), (2, 2.0), (3, 3.0)):
        written = payload(root, snapshot_path(token))
        assert written["entries"][0]["duration_sec"] == duration


def test_snapshot_holds_the_same_entity_json_as_the_head_it_accompanies(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    token = store.save(timeline(1.5), expected_version=0)
    assert payload(root, snapshot_path(token)) == payload(root, HEAD)
    token = store.save(timeline(2.5), expected_version=token)
    assert payload(root, snapshot_path(token)) == payload(root, HEAD)


def test_head_file_is_unchanged_in_place_and_loads_the_latest_save(
    store: ProjectStore,
) -> None:
    store.save(timeline(1.0), expected_version=0)
    store.save(timeline(2.0), expected_version=1)
    head, token = store.load_with_version(Timeline, EPISODE)
    assert token == 2
    assert head.entries[0].duration_sec == 2.0


def test_snapshots_are_per_episode(store: ProjectStore, root: ProjectRoot) -> None:
    store.save(timeline(1.0, "ep1"), expected_version=0)
    store.save(timeline(9.0, "ep2"), expected_version=0)
    assert payload(root, snapshot_path(1, "ep1"))["entries"][0]["duration_sec"] == 1.0
    assert payload(root, snapshot_path(1, "ep2"))["entries"][0]["duration_sec"] == 9.0


def test_a_rejected_save_writes_no_snapshot(store: ProjectStore, root: ProjectRoot) -> None:
    store.save(timeline(1.0), expected_version=0)
    with pytest.raises(VersionConflict):
        store.save(timeline(2.0), expected_version=7)
    assert not root.resolve(snapshot_path(2)).exists()
    assert not root.resolve(snapshot_path(8)).exists()


# --- immutability at or below the head, orphan replacement above it ----------------------


def test_snapshots_at_or_below_the_head_are_never_altered_by_later_saves(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    token = store.save(timeline(1.0), expected_version=0)
    before = root.read_bytes(snapshot_path(1))
    for duration in (2.0, 3.0, 4.0):
        token = store.save(timeline(duration), expected_version=token)
    assert root.read_bytes(snapshot_path(1)) == before


def test_a_snapshot_above_the_head_is_an_orphan_and_is_replaced(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    store.save(timeline(1.0), expected_version=0)
    # a previous save died after its snapshot write and before its head write
    root.write_text(snapshot_path(2), '{"orphan": true}\n')

    assert store.save(timeline(2.0), expected_version=1) == 2

    replaced = payload(root, snapshot_path(2))
    assert "orphan" not in replaced
    assert replaced["entries"][0]["duration_sec"] == 2.0
    assert store.load_timeline_at(EPISODE, 2).entries[0].duration_sec == 2.0


def test_an_orphan_snapshot_does_not_hide_the_real_head_before_the_next_save(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    store.save(timeline(1.0), expected_version=0)
    root.write_text(snapshot_path(2), '{"orphan": true}\n')
    head, token = store.load_with_version(Timeline, EPISODE)
    assert token == 1 and head.entries[0].duration_sec == 1.0


# --- load_timeline_at -----------------------------------------------------------------------


def test_load_timeline_at_returns_the_snapshot_for_that_token(store: ProjectStore) -> None:
    store.save(timeline(1.0), expected_version=0)
    store.save(timeline(2.0), expected_version=1)
    store.save(timeline(3.0), expected_version=2)
    for token, duration in ((1, 1.0), (2, 2.0), (3, 3.0)):
        found = store.load_timeline_at(EPISODE, token)
        assert isinstance(found, Timeline)
        assert found.entries[0].duration_sec == duration


def test_load_timeline_at_serves_a_legacy_head_that_has_no_snapshot(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    write_legacy_head(root, timeline(5.0), token=3)
    assert not root.resolve(snapshot_path(3)).exists()
    assert store.load_timeline_at(EPISODE, 3).entries[0].duration_sec == 5.0


def test_load_timeline_at_serves_a_legacy_head_without_store_metadata_as_token_one(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    write_legacy_head(root, timeline(5.0), token=None)
    assert store.load_with_version(Timeline, EPISODE)[1] == 1
    assert store.load_timeline_at(EPISODE, 1).entries[0].duration_sec == 5.0


def test_load_timeline_at_refuses_tokens_that_are_neither_snapshot_nor_head(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    write_legacy_head(root, timeline(5.0), token=3)
    for missing in (1, 2, 4):
        with pytest.raises(RecordNotFound):
            store.load_timeline_at(EPISODE, missing)


def test_load_timeline_at_refuses_an_unknown_token_after_saves(store: ProjectStore) -> None:
    store.save(timeline(1.0), expected_version=0)
    with pytest.raises(RecordNotFound):
        store.load_timeline_at(EPISODE, 2)
    with pytest.raises(RecordNotFound):
        store.load_timeline_at(EPISODE, 0)


def test_load_timeline_at_refuses_an_episode_without_a_timeline(store: ProjectStore) -> None:
    with pytest.raises(RecordNotFound):
        store.load_timeline_at("no-such-episode", 1)


def test_r6b_a_superseded_legacy_head_is_preserved_byte_for_byte_at_its_token(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    write_legacy_head(root, timeline(5.0), token=3)
    old_head = root.read_bytes(HEAD)
    assert not root.resolve(snapshot_path(3)).exists()

    assert store.save(timeline(6.0), expected_version=3) == 4

    assert root.read_bytes(snapshot_path(3)) == old_head
    assert store.load_timeline_at(EPISODE, 3).entries[0].duration_sec == 5.0
    assert store.load_timeline_at(EPISODE, 4).entries[0].duration_sec == 6.0
    assert payload(root, snapshot_path(4))["entries"][0]["duration_sec"] == 6.0


def test_r6b_a_legacy_head_without_store_metadata_is_preserved_at_token_one(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    write_legacy_head(root, timeline(5.0), token=None)
    old_head = root.read_bytes(HEAD)
    assert store.save(timeline(6.0), expected_version=1) == 2
    assert root.read_bytes(snapshot_path(1)) == old_head
    assert store.load_timeline_at(EPISODE, 1).entries[0].duration_sec == 5.0


def test_r6b_preserved_legacy_bytes_are_written_before_the_new_snapshot_and_head(
    store: ProjectStore, root: ProjectRoot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_legacy_head(root, timeline(5.0), token=3)
    writes: list[str] = []
    original = store._atomic_write

    def recording(relative: str, text: str) -> None:
        writes.append(relative)
        original(relative, text)

    monkeypatch.setattr(store, "_atomic_write", recording)
    store.save(timeline(6.0), expected_version=3)
    assert writes == [snapshot_path(3), snapshot_path(4), HEAD]


# --- R6a: orphans are never returned ------------------------------------------------------


class HeadWriteFailed(RuntimeError):
    pass


def fail_head_write(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make the write to the head file raise; return the ordered list of attempted writes."""
    attempts: list[str] = []
    original = store._atomic_write

    def flaky(relative: str, text: str) -> None:
        attempts.append(relative)
        if relative == HEAD:
            raise HeadWriteFailed(relative)
        original(relative, text)

    monkeypatch.setattr(store, "_atomic_write", flaky)
    return attempts


def test_r6a_a_manufactured_orphan_is_not_returned_but_the_head_is(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    store.save(timeline(1.0), expected_version=0)
    root.write_text(snapshot_path(2), json.dumps({**timeline(99.0).model_dump(mode="json"),
                                                  "_store_version": 2}) + "\n")
    with pytest.raises(RecordNotFound):
        store.load_timeline_at(EPISODE, 2)
    assert store.load_timeline_at(EPISODE, 1).entries[0].duration_sec == 1.0


def test_r6a_a_real_failed_head_write_leaves_the_head_and_an_unreturned_orphan(
    store: ProjectStore, root: ProjectRoot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.save(timeline(1.0), expected_version=0)
    head_before = root.read_bytes(HEAD)
    attempts = fail_head_write(store, monkeypatch)

    with pytest.raises(Exception) as raised:  # noqa: PT011 - may be wrapped by the store
        store.save(timeline(2.0), expected_version=1)

    assert "HeadWriteFailed" in repr(raised.value) or isinstance(raised.value, HeadWriteFailed) \
        or HEAD in str(raised.value)
    # Write order: the snapshot was attempted (and landed) strictly before the head.
    assert attempts == [snapshot_path(2), HEAD]
    assert root.resolve(snapshot_path(2)).exists()
    assert root.read_bytes(HEAD) == head_before
    head, token = store.load_with_version(Timeline, EPISODE)
    assert token == 1 and head.entries[0].duration_sec == 1.0
    with pytest.raises(RecordNotFound):
        store.load_timeline_at(EPISODE, 2)


def test_r6a_write_order_is_snapshot_then_head_on_success(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []
    original = store._atomic_write

    def recording(relative: str, text: str) -> None:
        writes.append(relative)
        original(relative, text)

    monkeypatch.setattr(store, "_atomic_write", recording)
    store.save(timeline(1.0), expected_version=0)
    assert writes == [snapshot_path(1), HEAD]


def test_r6a_a_review_cannot_bind_an_orphan_and_a_later_save_cannot_change_what_it_approved(
    store: ProjectStore, root: ProjectRoot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.save(Episode(episode_id=EPISODE, project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)
    store.save(timeline(1.0), expected_version=0)
    with monkeypatch.context() as patch:
        fail_head_write(store, patch)
        with pytest.raises(Exception):  # noqa: B017,PT011
            store.save(timeline(2.0), expected_version=1)
    assert root.resolve(snapshot_path(2)).exists()

    def attempt(review_id: str) -> Review:
        return record_review(store, target_kind="episode_cut", target_id=EPISODE, target_version=2,
                      episode_id=EPISODE, actor="creator", approval_state=ShotStatus.APPROVED,
                      review_id=review_id)

    with pytest.raises(JobError) as refused:
        attempt("orphan-review")
    assert refused.value.kind is JobErrorKind.INVALID_MANIFEST
    with pytest.raises(RecordNotFound):
        store.load(Review, "orphan-review")

    assert store.save(timeline(3.0), expected_version=1) == 2   # replaces the orphan

    # the refused attempt left nothing behind, and the new head is what token 2 now means
    with pytest.raises(RecordNotFound):
        store.load(Review, "orphan-review")
    assert store.load_timeline_at(EPISODE, 2).entries[0].duration_sec == 3.0
    assert attempt("fresh-review").target_version == 2


# --- export snapshot ---------------------------------------------------------------------------


def test_export_snapshot_captures_every_timeline_snapshot_alongside_the_head(
    store: ProjectStore,
) -> None:
    store.save(Project(project_id="p1", title="t"), expected_version=0)
    store.save(Episode(episode_id=EPISODE, project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)
    token = store.save(timeline(1.0), expected_version=0)
    token = store.save(timeline(2.0), expected_version=token)
    store.save(timeline(3.0), expected_version=token)

    planned = {item.relative_path for item in plan_snapshot(store)}

    assert HEAD in planned
    assert {snapshot_path(1), snapshot_path(2), snapshot_path(3)} <= planned
    assert not [path for path in planned if "/edits/timelines/" in path
                and path not in {snapshot_path(n) for n in (1, 2, 3)}]


# --- R6c: export packages the committed history ------------------------------------------------


def audio_timeline(audio: str, duration: float = 1.0) -> Timeline:
    cut = timeline(duration)
    return cut.model_copy(update={"audio_tracks": [audio]})


def export_ready(store: ProjectStore, root: ProjectRoot) -> None:
    store.save(Project(project_id="p1", title="t"), expected_version=0)
    store.save(Episode(episode_id=EPISODE, project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)


def test_r6c_history_audio_is_packaged_even_when_the_head_no_longer_references_it(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    export_ready(store, root)
    root.write_bytes("assets/audio/old.wav", b"old stem")
    root.write_bytes("assets/audio/new.wav", b"new stem")
    token = store.save(audio_timeline("assets/audio/old.wav"), expected_version=0)
    store.save(audio_timeline("assets/audio/new.wav", 2.0), expected_version=token)

    planned = {item.relative_path for item in plan_snapshot(store)}

    assert "assets/audio/old.wav" in planned
    assert "assets/audio/new.wav" in planned
    assert {snapshot_path(1), snapshot_path(2), HEAD} <= planned


def test_r6c_an_orphan_above_the_head_is_skipped_with_its_audio(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    export_ready(store, root)
    root.write_bytes("assets/audio/head.wav", b"head stem")
    root.write_bytes("assets/audio/orphan.wav", b"orphan stem")
    store.save(audio_timeline("assets/audio/head.wav"), expected_version=0)
    orphan = {**audio_timeline("assets/audio/orphan.wav", 9.0).model_dump(mode="json"),
              "_store_version": 2}
    root.write_text(snapshot_path(2), json.dumps(orphan) + "\n")

    planned = {item.relative_path for item in plan_snapshot(store)}

    assert snapshot_path(1) in planned
    assert snapshot_path(2) not in planned
    assert "assets/audio/orphan.wav" not in planned


@pytest.mark.parametrize("name", ["latest.json", "1.json", "0000001.json"])
def test_r6c_a_history_file_with_a_bad_name_is_refused(
    store: ProjectStore, root: ProjectRoot, name: str,
) -> None:
    export_ready(store, root)
    store.save(timeline(1.0), expected_version=0)
    root.write_text(f"episodes/{EPISODE}/edits/timelines/{name}",
                    root.read_text(snapshot_path(1)))
    with pytest.raises(JobError) as refused:
        plan_snapshot(store)
    assert refused.value.kind is JobErrorKind.INVALID_MANIFEST


def test_r6c_history_without_a_timeline_head_is_refused(
    store: ProjectStore, root: ProjectRoot,
) -> None:
    export_ready(store, root)
    root.write_text(snapshot_path(1), json.dumps(
        {**timeline(1.0).model_dump(mode="json"), "_store_version": 1}) + "\n")
    with pytest.raises(JobError) as refused:
        plan_snapshot(store)
    assert refused.value.kind is JobErrorKind.INVALID_MANIFEST
