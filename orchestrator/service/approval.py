"""Approval orchestration: bind Review records to the exact approved version."""

from __future__ import annotations

from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import Episode, Review, Shot, StyleBible
from orchestrator.domain.state import (
    ReviewApprovalError,
    TransitionContext,
    TransitionError,
    check_review_approval,
    check_transition,
)
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


def _review(store: ProjectStore, review_id: str, target_id: str, target_version: int) -> Review:
    try:
        review = store.load(Review, review_id)
    except RecordNotFound as exc:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"approval Review {review_id!r} does not exist",
            cause=exc,
        ) from exc
    try:
        check_review_approval(
            review, target_id=target_id, target_version=target_version,
        )
    except ReviewApprovalError as exc:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            str(exc),
            cause=exc,
        ) from exc
    return review


def approve_shot(
    store: ProjectStore, shot_id: str, *, episode_id: str, review_id: str,
    expected_version: int,
) -> Shot:
    """Verify an approving Review, transition the current Shot, and persist it."""
    shot = store.load(Shot, shot_id, episode_id=episode_id)
    if shot.version != expected_version:
        # update() owns the canonical CAS error and current-record diff.
        raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}", expected_version, shot.version, shot)
    _review(store, review_id, shot_id, shot.version)
    try:
        check_transition(
            shot.status, ShotStatus.APPROVED,
            TransitionContext(framing_status=shot.framing_status, approving_review_id=review_id),
        )
    except TransitionError as exc:
        raise JobError(JobErrorKind.INVALID_MANIFEST, str(exc), cause=exc) from exc
    updated = shot.model_copy(update={"status": ShotStatus.APPROVED, "approval_review_id": review_id})
    store.update(updated, expected_version=expected_version)
    return updated


def approve_episode(
    store: ProjectStore, episode_id: str, *, cut_version: int, review_id: str,
    expected_version: int,
) -> Episode:
    """Record gate C approval against the cut version named by its Review."""
    episode = store.load(Episode, episode_id)
    _review(store, review_id, episode_id, cut_version)
    updated = episode.model_copy(update={"approved_cut_version": cut_version, "approval_review_id": review_id})
    store.save(updated, expected_version=expected_version)
    return updated


def approve_style_bible(
    store: ProjectStore, project_id: str, *, review_id: str, expected_version: int,
) -> StyleBible:
    """Bind a StyleBible version to its approving Review and persist the pointer."""
    bible = store.load(StyleBible, project_id)
    if bible.version != expected_version:
        raise VersionConflict(f"references/style_bible/{project_id}", expected_version, bible.version, bible)
    _review(store, review_id, project_id, bible.version)
    updated = bible.model_copy(update={"approval_review_id": review_id})
    store.update(updated, expected_version=expected_version)
    return updated
