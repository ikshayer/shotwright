"""Independent contract cases for lifecycle payload integrity; real JSON and Reviews."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import Review, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import approve_shot
from orchestrator.store.json_store import ProjectStore


@pytest.fixture
def root() -> ProjectRoot:
    # Contained temp workaround: avoid pytest's Windows temp-directory ACL handling.
    repository = ProjectRoot(Path(__file__).resolve().parents[2])
    return ProjectRoot(repository.make_dir(f"_work/lifecycle-integrity/{uuid4().hex}"))


def tree(root: ProjectRoot) -> dict[str, bytes | None]:
    return {
        path.relative_to(root.path).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.path.rglob("*")
    }


def load(store: ProjectStore, *, version: int | None = None) -> tuple[Shot, int]:
    return store.load_with_version(Shot, "s1", episode_id="ep1", version=version)


def setup_shot(root: ProjectRoot) -> tuple[ProjectStore, Shot, int]:
    store = ProjectStore(root)
    root.write_bytes("audio/take.wav", b"retained audio take")
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=4, audio_refs=["audio/take.wav"])
    store.create(shot)
    store.save(
        Review(
            review_id="r1", target_id=shot.shot_id, target_version=shot.version,
            created_at=datetime(2026, 11, 4, tzinfo=UTC), approval_state=ShotStatus.APPROVED,
        ),
        expected_version=0,
    )
    assert store.load(Review, "r1").target_version == shot.version
    return store, *load(store)


def advance_to_review(store: ProjectStore) -> tuple[Shot, int]:
    for status in (
        ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW,
    ):
        shot, token = load(store)
        store.update(
            shot.model_copy(update={"status": status, "framing_status": FramingStatus.APPROVED}),
            expected_version=token,
        )
        changed, next_token = load(store)
        assert changed.status is status
        assert next_token == token + 1
    return load(store)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"status": "approved", "approval_review_id": None}, id="missing-review"),
        pytest.param({"approval_notes": {"invalid": "object"}}, id="notes-type"),
        pytest.param({"framing_status": "invalid-state"}, id="framing-enum"),
        pytest.param({"approval_review_id": ["r1"]}, id="review-pointer-type"),
    ],
)
def test_model_copy_lifecycle_payload_is_validated_before_append(root, changes):
    store, _, _ = setup_shot(root)
    shot, token = advance_to_review(store)
    before = tree(root)

    try:
        with pytest.raises(JobError) as error:
            store.update(shot.model_copy(update=changes), expected_version=token)
        assert error.value.kind is JobErrorKind.INVALID_MANIFEST
        assert error.value.__cause__ is not None
    finally:
        assert tree(root) == before
        assert load(ProjectStore(ProjectRoot(root.path))) == (shot, token)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"duration_sec": 8.0}, id="content-duration"),
        pytest.param({"audio_refs": ["audio/another-take.wav"]}, id="artifact-audio"),
        pytest.param({"status": "review"}, id="draft-to-review"),
        pytest.param(
            {"status": "approved", "approval_review_id": "r1"}, id="draft-to-approved",
        ),
    ],
)
@pytest.mark.parametrize("operation", ["load", "reopen", "update", "approve"])
def test_imported_lifecycle_snapshot_must_preserve_content_and_legal_edge(
    root, changes, operation,
):
    store, draft, token = setup_shot(root)
    before_update = tree(root)
    planned = draft.model_copy(update={"status": ShotStatus.PLANNED})
    store.update(planned, expected_version=token)
    planned, token = load(store)
    after_update = tree(root)
    assert all(after_update[name] == value for name, value in before_update.items())

    # Discover the immutable snapshot from the update's added files, never its layout.
    snapshot, = [
        name for name in after_update.keys() - before_update.keys()
        if name.endswith(".json")
    ]
    payload = json.loads(root.resolve(snapshot).read_text(encoding="utf-8"))
    identity = {name: payload[name] for name in ("shot_id", "episode_id", "version", "supersedes")}
    payload.update(changes)
    assert {name: payload[name] for name in identity} == identity
    # Isolate lifecycle-history validation: every tampered entity is valid Pydantic data.
    Shot.model_validate({name: value for name, value in payload.items() if name in Shot.model_fields})
    root.write_text(snapshot, json.dumps(payload))
    tampered_tree = tree(root)

    try:
        with pytest.raises(JobError) as error:
            if operation == "load":
                store.load(Shot, "s1", episode_id="ep1")
            elif operation == "reopen":
                load(ProjectStore(ProjectRoot(root.path)))
            elif operation == "update":
                ProjectStore(ProjectRoot(root.path)).update(
                    planned.model_copy(update={"approval_notes": "must not append"}),
                    expected_version=token,
                )
            else:
                approve_shot(
                    ProjectStore(ProjectRoot(root.path)), "s1", episode_id="ep1",
                    review_id="r1", expected_version=token,
                )
        assert error.value.kind is JobErrorKind.INVALID_MANIFEST
        assert error.value.__cause__ is not None
    finally:
        # Includes all persisted token metadata, retained artifacts, and empty directories.
        assert tree(root) == tampered_tree


def test_legal_approval_then_stale_retains_artifacts_and_immutable_history(root):
    store, _, _ = setup_shot(root)
    shot, token = advance_to_review(store)
    review_history = tree(root)
    approved = approve_shot(
        store, "s1", episode_id="ep1", review_id="r1", expected_version=token,
    )
    approved, approved_token = load(store)
    assert approved.status is ShotStatus.APPROVED
    assert approved.approval_review_id == "r1"
    assert approved.version == shot.version == 1
    assert approved_token == token + 1
    approved_history = tree(root)
    assert all(approved_history[name] == value for name, value in review_history.items())

    stale = approved.model_copy(update={"status": ShotStatus.STALE})
    store.update(stale, expected_version=approved_token)
    reopened = ProjectStore(ProjectRoot(root.path))
    assert load(reopened) == (stale, approved_token + 1)
    assert load(reopened, version=1) == (stale, approved_token + 1)
    assert reopened.versions(Shot, "s1", episode_id="ep1") == [1]
    assert stale.audio_refs == approved.audio_refs == ["audio/take.wav"]
    assert stale.approval_review_id == "r1"
    retained = tree(root)
    assert all(retained[name] == value for name, value in approved_history.items())
    assert retained["audio/take.wav"] == b"retained audio take"
    assert not any(name.endswith(".tmp") for name in retained)
