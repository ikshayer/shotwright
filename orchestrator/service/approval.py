"""Serialized approval commands with Review and dependency freshness checks."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Literal

from orchestrator.domain.base import FramingStatus, Invalidation, ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    EpisodeOutput,
    H3Candidate,
    Location,
    Review,
    ReviewTargetKind,
    RubricScores,
    Shot,
    ShotOutputKind,
    StyleBible,
    Timeline,
)
from orchestrator.domain.invalidation import field_invalidation
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


def _review(
    store: ProjectStore,
    review_id: str,
    *,
    target_kind: ReviewTargetKind,
    target_id: str,
    target_version: int,
    episode_id: str | None,
    agent_may_approve: bool,
) -> Review:
    try:
        review = store.load(Review, review_id)
    except RecordNotFound as exc:
        raise JobError(JobErrorKind.INVALID_MANIFEST,
                       f"approval Review {review_id!r} does not exist", cause=exc) from exc
    try:
        check_review_approval(
            review,
            target_kind=target_kind,
            target_id=target_id,
            target_version=target_version,
            episode_id=episode_id,
            agent_may_approve=agent_may_approve,
        )
    except ReviewApprovalError as exc:
        raise JobError(JobErrorKind.INVALID_MANIFEST, str(exc), cause=exc) from exc
    return review


def record_review(
    store: ProjectStore,
    *,
    target_kind: ReviewTargetKind,
    target_id: str,
    target_version: int,
    episode_id: str | None,
    actor: Literal["creator", "agent"],
    approval_state: ShotStatus,
    comment: str = "",
    rubric_scores: Sequence[RubricScores] = (),
    waiver_text: str | None = None,
    review_id: str | None = None,
    now: datetime | None = None,
) -> Review:
    """Record an immutable opinion about one exact, existing target version."""
    with store.command():
        review = Review(
            review_id=review_id if review_id is not None else uuid.uuid4().hex,
            target_kind=target_kind,
            target_id=target_id,
            target_version=target_version,
            episode_id=episode_id,
            actor=actor,
            created_at=now if now is not None else datetime.now(timezone.utc),
            comment=comment,
            approval_state=approval_state,
            rubric_scores=list(rubric_scores),
            waiver_text=waiver_text,
        )

        def missing_target() -> JobError:
            return JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"Review target {target_kind!r} {target_id!r} in Episode "
                f"{episode_id!r} at version {target_version} does not exist",
            )

        try:
            if target_kind in {"shot", "framing"}:
                shot = store.load(Shot, target_id, episode_id=episode_id, version=target_version)
                if shot.shot_id != target_id or shot.episode_id != episode_id:
                    raise missing_target()
            elif target_kind == "episode_cut":
                store.load_timeline_at(target_id, target_version)
            elif target_kind == "style_bible":
                bible = store.load(StyleBible, target_id, version=target_version)
                if bible.project_id != target_id:
                    raise missing_target()
            elif target_kind == "location":
                location = store.load(Location, target_id, version=target_version)
                if location.location_id != target_id:
                    raise missing_target()
            else:
                candidate = store.load(H3Candidate, target_id)
                if (candidate.episode_id != episode_id
                        or candidate.shot_version != target_version):
                    raise missing_target()
        except RecordNotFound as exc:
            error = missing_target()
            error.__cause__ = exc
            raise error from exc
        store.save(review, expected_version=0)
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
                 expected_version: int, agent_may_approve: bool = False) -> Shot:
    """Approve a Shot only after a serialized, commit-adjacent freshness recheck."""
    with store.command():
        _blocked_pending(store)
        shot, token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
        if token != expected_version:
            raise VersionConflict(f"episodes/{episode_id}/shots/{shot_id}", expected_version, token, shot)
        fences = _fences(store)
        _review(
            store, review_id, target_kind="shot", target_id=shot_id,
            target_version=shot.version, episode_id=episode_id,
            agent_may_approve=agent_may_approve,
        )
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
        _review(
            store, review_id, target_kind="shot", target_id=shot_id,
            target_version=current.version, episode_id=episode_id,
            agent_may_approve=agent_may_approve,
        )
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


def approve_framing(
    store: ProjectStore,
    shot_id: str,
    *,
    episode_id: str,
    review_id: str,
    expected_version: int,
    agent_may_approve: bool = False,
) -> Shot:
    """Accept pending FR-29 framing and pin its rebuild-triggering camera values."""
    with store.command():
        shot, token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
        if token != expected_version:
            raise VersionConflict(
                f"episodes/{episode_id}/shots/{shot_id}", expected_version, token, shot,
            )
        if (
            shot.framing_status is FramingStatus.APPROVED
            and shot.camera_profile_version is not None
            and "camera_profile_version" in shot.creator_authored
        ):
            return shot
        _review(
            store, review_id, target_kind="framing", target_id=shot_id,
            target_version=shot.version, episode_id=episode_id,
            agent_may_approve=agent_may_approve,
        )
        if shot.framing_status is not FramingStatus.PENDING or not shot.framing_panel_ref:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"Shot {shot_id!r} has no pending framing panel to approve",
            )
        try:
            camera, _camera_token = store.load_with_version(
                CameraProfile, shot.camera_profile,
            )
        except RecordNotFound as exc:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"Shot {shot_id!r} references missing CameraProfile "
                f"{shot.camera_profile!r}; framing cannot be frozen",
                cause=exc,
            ) from exc
        previews = store.list_shot_outputs(
            shot.shot_id,
            episode_id=shot.episode_id,
            record_version=shot.version,
            kind=ShotOutputKind.PREVIEW,
        )
        if not previews:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"Shot {shot_id!r} has no framing preview for version {shot.version}; "
                "render the preview before accepting framing",
            )
        preview = previews[-1]
        if preview.relative_path != shot.framing_panel_ref:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"Shot {shot_id!r} framing_panel_ref does not name its latest preview; "
                "refresh the panel before accepting framing",
            )
        if (
            preview.camera_profile_id != shot.camera_profile
            or preview.camera_profile_version is None
        ):
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"latest framing preview for Shot {shot_id!r} has no matching "
                "CameraProfile binding; rerender the preview",
            )
        try:
            preview_camera = store.load(
                CameraProfile,
                shot.camera_profile,
                version=preview.camera_profile_version,
            )
        except RecordNotFound as exc:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"latest framing preview for Shot {shot_id!r} refers to missing "
                f"CameraProfile version {preview.camera_profile_version}",
                cause=exc,
            ) from exc
        rebuild_fields = {
            name for name in CameraProfile.model_fields
            if field_invalidation(CameraProfile, name) is Invalidation.REBUILD
        }
        changed = sorted(
            name for name in rebuild_fields
            if getattr(preview_camera, name) != getattr(camera, name)
        )
        if changed:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"latest framing preview used CameraProfile version "
                f"{preview.camera_profile_version}, but current rebuild-triggering values "
                f"changed in {changed}; rerender the preview before accepting framing",
            )

        authored = set(shot.creator_authored)
        authored.update({"camera_profile", "camera_profile_version"})
        authored.update(
            f"camera_profile.{name}"
            for name in CameraProfile.model_fields
            if field_invalidation(CameraProfile, name) is Invalidation.REBUILD
        )
        updated = shot.model_copy(update={
            "status": ShotStatus.BUILDING,
            "framing_status": FramingStatus.APPROVED,
            "camera_profile_version": preview.camera_profile_version,
            "creator_authored": authored,
        })
        try:
            check_transition(
                shot.status,
                ShotStatus.BUILDING,
                TransitionContext(framing_status=FramingStatus.APPROVED),
            )
        except TransitionError as exc:
            raise _domain_error(exc) from exc
        current, current_token = store.load_with_version(Shot, shot_id, episode_id=episode_id)
        if current_token != token or current != shot:
            raise VersionConflict(
                f"episodes/{episode_id}/shots/{shot_id}", expected_version,
                current_token, current,
            )
        _review(
            store, review_id, target_kind="framing", target_id=shot_id,
            target_version=current.version, episode_id=episode_id,
            agent_may_approve=agent_may_approve,
        )
        store.update(updated, expected_version=expected_version)
        return updated


def _current_cut_output(
    store: ProjectStore, episode_id: str, cut_output_id: str,
) -> tuple[EpisodeOutput, int]:
    cut = next(
        (row for row in store.list_episode_outputs(episode_id)
         if row.output_id == cut_output_id),
        None,
    )
    if cut is None:
        raise RecordNotFound(
            f"Episode {episode_id!r} has no cut output {cut_output_id!r}"
        )
    try:
        timeline, timeline_token = store.load_with_version(Timeline, episode_id)
    except RecordNotFound as exc:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"cut {cut_output_id!r} is not of the current Timeline",
            cause=exc,
        ) from exc
    if (cut.timeline_store_version != timeline_token
            or cut.timeline_sha256 != timeline.content_sha256):
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"cut {cut_output_id!r} is not of the current Timeline",
        )
    return cut, timeline_token


def approve_episode(store: ProjectStore, episode_id: str, *, cut_output_id: str,
                    review_id: str, expected_version: int,
                    agent_may_approve: bool = False) -> Episode:
    """Approve one exact current cut when no Shot or pending cascade makes it stale."""
    with store.command():
        _blocked_pending(store)
        episode, token = store.load_with_version(Episode, episode_id)
        if token != expected_version:
            raise VersionConflict(f"episodes/{episode_id}/episode.json", expected_version, token, episode)
        cut, cut_version = _current_cut_output(store, episode_id, cut_output_id)
        shots = _shot_snapshot(store, episode_id)
        _review(
            store, review_id, target_kind="episode_cut", target_id=episode_id,
            target_version=cut_version, episode_id=episode_id,
            agent_may_approve=agent_may_approve,
        )
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
        current_cut, current_cut_version = _current_cut_output(
            store, episode_id, cut_output_id,
        )
        if current_cut != cut or current_cut_version != cut_version:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"cut {cut_output_id!r} is not of the current Timeline",
            )
        _review(
            store, review_id, target_kind="episode_cut", target_id=episode_id,
            target_version=cut.timeline_store_version, episode_id=episode_id,
            agent_may_approve=agent_may_approve,
        )
        try:
            check_cut_approval(pending=False, shots=current_shots)
        except ReviewApprovalError as exc:
            raise _domain_error(exc) from exc
        _blocked_pending(store)
        current_shots = _shot_snapshot(store, episode_id)
        current_cut, current_cut_version = _current_cut_output(
            store, episode_id, cut_output_id,
        )
        if current_cut != cut or current_cut_version != cut_version:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"cut {cut_output_id!r} is not of the current Timeline",
            )
        try:
            check_cut_approval(pending=False, shots=current_shots)
        except ReviewApprovalError as exc:
            raise _domain_error(exc) from exc
        updated = current.model_copy(update={
            "approved_cut_version": cut.timeline_store_version,
            "approved_cut_output_id": cut.output_id,
            "approval_review_id": review_id,
        })
        store.save(updated, expected_version=expected_version)
        return updated


def approve_style_bible(store: ProjectStore, project_id: str, *, review_id: str,
                        expected_version: int,
                        agent_may_approve: bool = False) -> StyleBible:
    """Bind the current StyleBible content version to its approving Review."""
    with store.command():
        _blocked_pending(store)
        bible, token = store.load_with_version(StyleBible, project_id)
        if token != expected_version:
            raise VersionConflict(f"references/style_bible/{project_id}", expected_version, token, bible)
        _review(
            store, review_id, target_kind="style_bible", target_id=project_id,
            target_version=bible.version, episode_id=None,
            agent_may_approve=agent_may_approve,
        )
        current, current_token = store.load_with_version(StyleBible, project_id)
        if current_token != token or current != bible:
            raise VersionConflict(f"references/style_bible/{project_id}", expected_version,
                                  current_token, current)
        _blocked_pending(store)
        _review(
            store, review_id, target_kind="style_bible", target_id=project_id,
            target_version=current.version, episode_id=None,
            agent_may_approve=agent_may_approve,
        )
        _blocked_pending(store)
        updated = current.model_copy(update={"approval_review_id": review_id})
        store.update(updated, expected_version=expected_version)
        return updated
