"""Legacy schema-1 histories retain lifecycle state and acquire monotonic CAS tokens."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.domain.base import Entity, EvidenceTier, ShotStatus
from orchestrator.domain.entities import Episode, Location, Review, Shot, StyleBible, StyleClaim
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import approve_shot, approve_style_bible
from orchestrator.store.json_store import ProjectStore, VersionConflict

LegacyEntity = Location | Shot | StyleBible
KINDS = ("location", "shot", "style_bible")


def _review_id(kind: str, version: int) -> str:
    return f"{kind}-review-v{version}"


def _entity(kind: str, version: int, *, approved: bool) -> LegacyEntity:
    fields = {
        "version": version,
        "supersedes": version - 1 if version > 1 else None,
        "approval_review_id": _review_id(kind, version) if approved else None,
    }
    if kind == "location":
        return Location(location_id="hall", geometry_provenance=f"survey {version}", **fields)
    if kind == "shot":
        return Shot(
            shot_id="S01", episode_id="ep01", duration_sec=12,
            narrative_beat=f"beat {version}",
            status=ShotStatus.APPROVED if approved else ShotStatus.REVIEW,
            **fields,
        )
    assert kind == "style_bible"
    return StyleBible(
        project_id="project01",
        desired=[StyleClaim(claim=f"lighting {version}", evidence_tier=EvidenceTier.CREATOR)],
        **fields,
    )


def _address(entity: LegacyEntity) -> tuple[str, dict[str, str]]:
    if isinstance(entity, Location):
        return entity.location_id, {}
    if isinstance(entity, Shot):
        return entity.shot_id, {"episode_id": entity.episode_id}
    return entity.project_id, {}


def _base_path(entity: LegacyEntity) -> str:
    if isinstance(entity, Location):
        directory = f"locations/{entity.location_id}"
    elif isinstance(entity, Shot):
        directory = f"episodes/{entity.episode_id}/shots/{entity.shot_id}"
    else:
        directory = "references/style_bible"
    return f"{directory}/v{entity.version:04d}.json"


def _write_legacy(root: ProjectRoot, relative: str, entity: Entity) -> None:
    # Deliberately bypass store serialization: baseline files had no mutation metadata.
    root.write_text(relative, entity.model_dump_json(indent=2))


def _persist_review(root: ProjectRoot, kind: str, entity: LegacyEntity) -> Review:
    target_id, _ = _address(entity)
    review = Review(
        review_id=_review_id(kind, entity.version),
        target_id=target_id, target_version=entity.version,
        target_kind="shot" if isinstance(entity, Shot) else "style_bible",
        episode_id=entity.episode_id if isinstance(entity, Shot) else None,
        actor="creator",
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
        approval_state=ShotStatus.APPROVED,
    )
    _write_legacy(root, f"reviews/{review.review_id}.json", review)
    return review


def _files(root: ProjectRoot) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.path.rglob("*.json")}


def _assert_retained(before: dict[Path, bytes]) -> None:
    for path, content in before.items():
        assert path.read_bytes() == content, f"rewrote history: {path.name}"


def _load(
    store: ProjectStore, entity: LegacyEntity, *, version: int | None = None,
) -> tuple[LegacyEntity, int]:
    record_id, scope = _address(entity)
    return store.load_with_version(type(entity), record_id, version=version, **scope)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("v2_approved", [False, True], ids=["unapproved-v2", "approved-v2"])
def test_legacy_multiversion_lifecycle_and_successor_are_append_only(
    tmp_path: Path, kind: str, v2_approved: bool,
) -> None:
    root = ProjectRoot(tmp_path)
    first = _entity(kind, 1, approved=True)
    second = _entity(kind, 2, approved=v2_approved)
    reviews = [_persist_review(root, kind, entity) for entity in (first, second)]
    _write_legacy(root, _base_path(first), first)
    _write_legacy(root, _base_path(second), second)
    _write_legacy(
        root, "episodes/ep01/episode.json",
        Episode(episode_id="ep01", project_id="project01", number=1, duration_target_sec=12),
    )
    original = _files(root)

    store = ProjectStore(root)
    assert _load(store, first, version=1) == (first, 1)
    assert _load(store, second) == (second, 2)
    assert _load(store, second, version=2) == (second, 2)
    assert _files(root) == original  # Reads must not silently migrate legacy files.

    # A no-op is a successful lifecycle write, separating token from content version.
    store.update(second, expected_version=2)
    assert _load(ProjectStore(root), second) == (second, 3)
    assert _load(store, second, version=2) == (second, 3)
    _assert_retained(original)
    after_noop = _files(root)
    assert len(after_noop) > len(original)
    with pytest.raises(VersionConflict) as conflict:
        store.update(second, expected_version=2)
    assert (conflict.value.expected, conflict.value.actual) == (2, 3)
    assert _files(root) == after_noop

    # Approve using a persisted Review for CONTENT v2 after the token has reached 3.
    if not v2_approved and isinstance(second, Shot):
        lifecycle = approve_shot(
            store, second.shot_id, episode_id=second.episode_id,
            review_id=reviews[1].review_id, expected_version=3,
        )
    elif not v2_approved and isinstance(second, StyleBible):
        lifecycle = approve_style_bible(
            store, second.project_id, review_id=reviews[1].review_id, expected_version=3,
        )
    elif isinstance(second, Shot):
        lifecycle = second.model_copy(update={"status": ShotStatus.STALE})
        store.update(lifecycle, expected_version=3)
    else:
        lifecycle = second.model_copy(update={"approval_review_id": reviews[1].review_id})
        store.update(lifecycle, expected_version=3)
    if isinstance(lifecycle, Shot):
        assert lifecycle.status is (ShotStatus.STALE if v2_approved else ShotStatus.APPROVED)
    assert lifecycle.version == 2
    assert lifecycle.approval_review_id == reviews[1].review_id
    assert _load(ProjectStore(root), lifecycle) == (lifecycle, 4)
    _assert_retained(after_noop)
    before_successor = _files(root)
    assert len(before_successor) > len(after_noop)

    successor = lifecycle.model_copy(update={
        **lifecycle.next_version(), "approval_review_id": None,
        **({"status": ShotStatus.DRAFT} if isinstance(lifecycle, Shot) else {}),
    })
    store.revise(successor, expected_version=4)
    reopened = ProjectStore(root)
    assert _load(reopened, successor) == (successor, 5)
    assert _load(reopened, first, version=1) == (first, 1)
    assert _load(reopened, lifecycle, version=2) == (lifecycle, 4)
    record_id, scope = _address(successor)
    assert reopened.versions(type(successor), record_id, **scope) == [1, 2, 3]
    _assert_retained(before_successor)
    after_successor = _files(root)
    assert len(after_successor) > len(before_successor)
    for review in reviews:
        assert reopened.load(Review, review.review_id) == review
        assert review.target_version in (1, 2)
    # Historical content and the new head's content version are not valid CAS tokens.
    with pytest.raises(VersionConflict) as conflict:
        reopened.update(lifecycle, expected_version=4)
    assert conflict.value.actual == 5
    with pytest.raises(VersionConflict) as conflict:
        reopened.update(successor, expected_version=3)
    assert conflict.value.actual == 5
    assert _files(root) == after_successor
    assert not list(root.path.rglob("*.tmp"))


@pytest.mark.parametrize("kind", KINDS)
def test_legacy_successor_cannot_regress_a_persisted_lifecycle_token(
    tmp_path: Path, kind: str,
) -> None:
    root = ProjectRoot(tmp_path)
    first = _entity(kind, 1, approved=False)
    _write_legacy(root, _base_path(first), first)
    store = ProjectStore(root)
    store.update(first, expected_version=1)
    store.update(first, expected_version=2)
    assert _load(ProjectStore(root), first) == (first, 3)
    before_successor = _files(root)

    # Synthetic v2 predecessor 1 does not agree with v1's actual head token 3.
    second = _entity(kind, 2, approved=False)
    _write_legacy(root, _base_path(second), second)
    corrupted_history = _files(root)
    with pytest.raises(JobError) as caught:
        _load(ProjectStore(root), second)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    with pytest.raises(JobError) as caught:
        ProjectStore(root).update(second, expected_version=2)
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert _files(root) == corrupted_history
    _assert_retained(before_successor)


@pytest.mark.parametrize("kind", KINDS)
def test_isolated_legacy_v3_keeps_content_version_token(tmp_path: Path, kind: str) -> None:
    root = ProjectRoot(tmp_path)
    third = _entity(kind, 3, approved=True)
    review = _persist_review(root, kind, third)
    _write_legacy(root, _base_path(third), third)
    original = _files(root)
    store = ProjectStore(root)
    assert _load(store, third) == (third, 3)
    assert _load(store, third, version=3) == (third, 3)
    assert _files(root) == original
    store.update(third, expected_version=3)
    reopened = ProjectStore(root)
    assert _load(reopened, third, version=3) == (third, 4)
    assert reopened.load(Review, review.review_id).target_version == 3
    _assert_retained(original)
