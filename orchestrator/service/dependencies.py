"""Append upstream revisions and apply domain-computed shot invalidation."""

from __future__ import annotations

from collections.abc import Iterable

from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import CameraProfile, Episode, Location, Script, Shot
from orchestrator.domain.invalidation import (
    diff_camera_profile,
    revokes_cut_approval,
    shots_invalidated_by,
    shots_invalidated_by_profile,
)
from orchestrator.domain.state import TransitionContext, check_transition
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import ProjectStore, VersionConflict


def _shots(store: ProjectStore, episode_ids: Iterable[str]) -> list[Shot]:
    result: list[Shot] = []
    for episode_id in sorted(set(episode_ids)):
        result.extend(
            store.load(Shot, shot_id, episode_id=episode_id)
            for shot_id in store.ids(Shot, episode_id=episode_id)
        )
    return result


def _invalidate(
    store: ProjectStore,
    shots: Iterable[Shot],
    shot_ids: Iterable[str],
    reason: str,
    *,
    already_changed: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Persist stale transitions, then revoke affected episodes; report partial progress."""
    wanted = set(shot_ids)
    changed: list[str] = []
    revoked: list[str] = []
    episodes: set[str] = set()
    try:
        for shot in shots:
            if shot.shot_id not in wanted:
                continue
            check_transition(shot.status, ShotStatus.STALE, TransitionContext())
            store.update(shot.model_copy(update={"status": ShotStatus.STALE}), expected_version=shot.version)
            changed.append(shot.shot_id)
            episodes.add(shot.episode_id)
        if changed and revokes_cut_approval(ShotStatus.STALE):
            for episode_id in sorted(episodes):
                episode, token = store.load_with_version(Episode, episode_id)
                if episode.approved_cut_version is None and episode.approval_review_id is None:
                    continue
                store.save(
                    episode.model_copy(update={"approved_cut_version": None, "approval_review_id": None}),
                    expected_version=token,
                )
                revoked.append(episode_id)
    except VersionConflict as exc:
        progress = "; ".join(
            (
                f"upstream changed={list(already_changed)}",
                f"shots made stale={changed}",
                f"episode gates revoked={revoked}",
            )
        )
        exc.args = (f"{exc}; partial invalidation progress: {progress}",)
        raise
    except Exception as exc:
        progress = (
            f"upstream records already changed={list(already_changed)}; "
            f"shots made stale={changed}; episode gates revoked={revoked}"
        )
        raise JobError(
            JobErrorKind.INTERNAL,
            f"{reason} invalidation failed after partial progress ({progress}); cause: {exc}",
            cause=exc,
        ) from exc
    return tuple(changed)


def revise_script(
    store: ProjectStore, revised: Script, *, expected_version: int,
) -> tuple[str, ...]:
    """Append a Script version and stale approved Shots depending on changed beats."""
    before = store.load(Script, revised.script_id, episode_id=revised.episode_id)
    if before.version != expected_version:
        raise VersionConflict(f"script/{revised.script_id}", expected_version, before.version, before)
    old = {beat.beat_id: beat for beat in before.beats}
    new = {beat.beat_id: beat for beat in revised.beats}
    changed = {beat_id for beat_id in old.keys() | new.keys() if old.get(beat_id) != new.get(beat_id)}
    shots = _shots(store, [revised.episode_id]) if changed else []
    store.revise(revised, expected_version=expected_version)
    if not changed:
        return ()
    hits = set().union(*(set(shots_invalidated_by(beat_id, shots, "Script beat changed").shot_ids) for beat_id in changed))
    return _invalidate(
        store, shots, hits, "Script beat changed",
        already_changed=(f"Script {revised.script_id}@{revised.version}",),
    )


def revise_location(
    store: ProjectStore, revised: Location, *, expected_version: int, episode_ids: Iterable[str],
) -> tuple[str, ...]:
    """Append a Location version and stale approved Shots depending on it."""
    before = store.load(Location, revised.location_id)
    if before.version != expected_version:
        raise VersionConflict(f"locations/{revised.location_id}", expected_version, before.version, before)
    shots = _shots(store, episode_ids)
    store.revise(revised, expected_version=expected_version)
    decision = shots_invalidated_by(revised.location_id, shots, "Location changed")
    return _invalidate(
        store, shots, decision.shot_ids, decision.reason,
        already_changed=(f"Location {revised.location_id}@{revised.version}",),
    )


def revise_camera_profile(
    store: ProjectStore, revised: CameraProfile, *, expected_version: int, episode_ids: Iterable[str],
) -> tuple[str, ...]:
    """Append a CameraProfile version and apply its field-tagged invalidation."""
    before = store.load(CameraProfile, revised.camera_id)
    if before.version != expected_version:
        raise VersionConflict(f"camera_profiles/{revised.camera_id}", expected_version, before.version, before)
    diff = diff_camera_profile(before, revised)
    shots = _shots(store, episode_ids)
    store.revise(revised, expected_version=expected_version)
    decision = shots_invalidated_by_profile(diff, shots)
    return _invalidate(
        store, shots, decision.shot_ids, decision.reason,
        already_changed=(f"CameraProfile {revised.camera_id}@{revised.version}",),
    )


def revise_shot(store: ProjectStore, revised: Shot, *, expected_version: int) -> Shot:
    """Append a draft Shot successor and revoke its Episode's gate-C approval."""
    store.revise(revised, expected_version=expected_version)
    if revised.status is ShotStatus.DRAFT and revokes_cut_approval(revised.status):
        try:
            episode, token = store.load_with_version(Episode, revised.episode_id)
            if episode.approved_cut_version is not None or episode.approval_review_id is not None:
                store.save(
                    episode.model_copy(update={"approved_cut_version": None, "approval_review_id": None}),
                    expected_version=token,
                )
        except VersionConflict as exc:
            exc.args = (
                "; ".join(
                    (
                        str(exc),
                        f"Shot {revised.shot_id}@{revised.version} was already created as draft",
                        f"Episode {revised.episode_id} gate-C revocation conflicted",
                    )
                ),
            )
            raise
        except Exception as exc:
            raise JobError(
                JobErrorKind.INTERNAL,
                f"Shot {revised.shot_id}@{revised.version} was created as draft, but "
                f"Episode {revised.episode_id} gate-C revocation failed: {exc}",
                cause=exc,
            ) from exc
    return revised
