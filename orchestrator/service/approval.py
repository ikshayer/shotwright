"""Serialized approval commands with Review and dependency freshness checks."""

from __future__ import annotations

from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import Episode, Review, Shot, StyleBible
from orchestrator.domain.state import (
    ReviewApprovalError,
    TransitionContext,
    TransitionError,
    check_cut_approval,
    check_dependency_freshness,
    check_review_approval,
    check_transition,
)
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.dependencies import validated_cascades
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


def _blocked_pending(store: ProjectStore) -> tuple[dict, ...]:
    plans = validated_cascades(store)
    pending_ids = {p["cascade_id"] for p in store.cascades(pending_only=True)}
    pending = tuple(plan for plan in plans if plan["cascade_id"] in pending_ids)
    if pending:
        raise JobError(JobErrorKind.INVALID_MANIFEST,
                       f"pending cascade plan(s) {[p['cascade_id'] for p in pending]}; "
                       "call resume_invalidations(store) before approval")
    return pending


def _review(store: ProjectStore, review_id: str, target_id: str, target_version: int) -> Review:
    try:
        review = store.load(Review, review_id)
    except RecordNotFound as exc:
        raise JobError(JobErrorKind.INVALID_MANIFEST,
                       f"approval Review {review_id!r} does not exist", cause=exc) from exc
    try:
        check_review_approval(review, target_id=target_id, target_version=target_version)
    except ReviewApprovalError as exc:
        raise JobError(JobErrorKind.INVALID_MANIFEST, str(exc), cause=exc) from exc
    return review


def _fences(store: ProjectStore) -> tuple[tuple[str, str, int], ...]:
    return tuple(sorted({(row["episode_id"], row["shot_id"], row["version"])
                         for plan in validated_cascades(store) for row in plan["affected"]}))


def _shot_snapshot(store: ProjectStore, episode_id: str) -> tuple[Shot, ...]:
    return tuple(store.load(Shot, shot_id, episode_id=episode_id)
                 for shot_id in store.ids(Shot, episode_id=episode_id))


def _domain_error(exc: ReviewApprovalError | TransitionError) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, str(exc), cause=exc)


def approve_shot(store: ProjectStore, shot_id: str, *, episode_id: str, review_id: str,
                 expected_version: int) -> Shot:
    """Approve a Shot only after a serialized, commit-adjacent freshness recheck."""
    with store.command():
        _blocked_pending(store)
        shot, token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
        if token != expected_version:
            raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}", expected_version, token, shot)
        fences = _fences(store)
        _review(store, review_id, shot_id, shot.version)
        try:
            check_dependency_freshness(shot, invalidated=fences)
            check_transition(shot.status, ShotStatus.APPROVED,
                             TransitionContext(framing_status=shot.framing_status,
                                               approving_review_id=review_id))
        except (ReviewApprovalError, TransitionError) as exc:
            raise _domain_error(exc) from exc
        current, current_token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
        if current_token != token or current != shot:
            raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}", expected_version,
                                  current_token, current)
        _blocked_pending(store)
        _review(store, review_id, shot_id, current.version)
        updated = current.model_copy(update={"status": ShotStatus.APPROVED,
                                             "approval_review_id": review_id})
        final_fences = _fences(store)
        _blocked_pending(store)
        try:
            check_dependency_freshness(current, invalidated=final_fences)
        except ReviewApprovalError as exc:
            raise _domain_error(exc) from exc
        store.update(updated, expected_version=expected_version)
        return updated


def approve_episode(store: ProjectStore, episode_id: str, *, cut_version: int,
                    review_id: str, expected_version: int) -> Episode:
    """Approve gate C only when no current Shot or pending cascade makes it stale."""
    with store.command():
        _blocked_pending(store)
        episode, token = store.load_with_version(Episode, episode_id)
        if token != expected_version:
            raise VersionConflict(f"episodes/{episode_id}/episode.json", expected_version, token, episode)
        shots = _shot_snapshot(store, episode_id)
        _review(store, review_id, episode_id, cut_version)
        try:
            check_cut_approval(pending=False, shots=shots)
        except ReviewApprovalError as exc:
            raise _domain_error(exc) from exc
        current, current_token = store.load_with_version(Episode, episode_id)
        current_shots = _shot_snapshot(store, episode_id)
        if current_token != token or current != episode:
            raise VersionConflict(f"episodes/{episode_id}/episode.json", expected_version,
                                  current_token, current)
        _blocked_pending(store)
        _review(store, review_id, episode_id, cut_version)
        try:
            check_cut_approval(pending=False, shots=current_shots)
        except ReviewApprovalError as exc:
            raise _domain_error(exc) from exc
        _blocked_pending(store)
        current_shots = _shot_snapshot(store, episode_id)
        try:
            check_cut_approval(pending=False, shots=current_shots)
        except ReviewApprovalError as exc:
            raise _domain_error(exc) from exc
        updated = current.model_copy(update={"approved_cut_version": cut_version,
                                             "approval_review_id": review_id})
        store.save(updated, expected_version=expected_version)
        return updated


def approve_style_bible(store: ProjectStore, project_id: str, *, review_id: str,
                        expected_version: int) -> StyleBible:
    """Bind the current StyleBible content version to its approving Review."""
    with store.command():
        _blocked_pending(store)
        bible, token = store.load_with_version(StyleBible, project_id)
        if token != expected_version:
            raise VersionConflict(f"references/style_bible/{project_id}", expected_version, token, bible)
        _review(store, review_id, project_id, bible.version)
        current, current_token = store.load_with_version(StyleBible, project_id)
        if current_token != token or current != bible:
            raise VersionConflict(f"references/style_bible/{project_id}", expected_version,
                                  current_token, current)
        _blocked_pending(store)
        _review(store, review_id, project_id, current.version)
        _blocked_pending(store)
        updated = current.model_copy(update={"approval_review_id": review_id})
        store.update(updated, expected_version=expected_version)
        return updated
