"""Serialized, journaled dependency revisions and roll-forward recovery."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from orchestrator.domain.base import ShotStatus, VersionedEntity
from orchestrator.domain.entities import CameraProfile, Episode, Location, Script, Shot
from orchestrator.domain.invalidation import revision_impact
from orchestrator.domain.state import TransitionContext, check_revision, check_transition
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import ProjectStore, VersionConflict

_ENTITY_TYPES = {"Script": Script, "Location": Location, "CameraProfile": CameraProfile, "Shot": Shot}


def validated_cascades(store: ProjectStore) -> tuple[dict[str, Any], ...]:
    """Validate every journal payload before history is used as evidence."""
    plans = store.cascades()
    for plan in plans:
        entity_type = plan["entity_type"]
        cls = _ENTITY_TYPES.get(entity_type)
        if cls is None:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"cascade {plan['cascade_id']} has unknown entity type {entity_type!r}",
            )
        try:
            before = store.validate_entity_payload(cls.model_validate(plan["before"]))
            after = store.validate_entity_payload(cls.model_validate(plan["after"]))
            check_revision(before, after)
            record_id = _record_id(before)
            episode_id = getattr(before, "episode_id", None)
            stored_before, stored_token = store.load_with_version(
                cls, record_id, episode_id=episode_id, version=before.version,
            )
            if (stored_before.model_dump(mode="json") != plan["before"]
                    or stored_token != plan["expected_version"]):
                raise ValueError("planned before payload or mutation token does not match retained history")
            # Exercise domain impact validation as well as successor validation;
            # this keeps journal payloads subject to the same accepted entity set.
            revision_impact(before, after, ())
        except Exception as exc:
            cause = exc.__cause__ if isinstance(exc, JobError) and exc.__cause__ is not None else exc
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"cascade {plan['cascade_id']} has invalid entity payload; cause: {cause}",
                cause=cause,
            ) from exc
    return plans


def _record_id(entity: VersionedEntity) -> str:
    """Select the storage key from the entity's concrete layout."""
    if type(entity) is Script:
        return entity.script_id
    if type(entity) is Location:
        return entity.location_id
    if type(entity) is CameraProfile:
        return entity.camera_id
    if type(entity) is Shot:
        return entity.shot_id
    raise JobError(JobErrorKind.INVALID_MANIFEST,
                   f"unsupported revision entity type {type(entity).__name__}")


def _pending_error(store: ProjectStore) -> None:
    plans = validated_cascades(store)
    pending_ids = {plan["cascade_id"] for plan in store.cascades(pending_only=True)}
    pending = tuple(plan for plan in plans if plan["cascade_id"] in pending_ids)
    if pending:
        ids = [plan["cascade_id"] for plan in pending]
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            f"pending cascade plan(s) {ids}; call resume_invalidations(store) before another approval or revision",
        )


def _shots(store: ProjectStore, episode_ids: Iterable[str]) -> list[Shot]:
    result: list[Shot] = []
    for episode_id in sorted(set(episode_ids)):
        result.extend(store.load(Shot, shot_id, episode_id=episode_id)
                      for shot_id in store.ids(Shot, episode_id=episode_id))
    return result


def _plan(store: ProjectStore, before: VersionedEntity, after: VersionedEntity,
          expected_version: int, shots: Iterable[Shot], episode_ids: Iterable[str]) -> dict[str, Any]:
    affected = sorted(
        ({"episode_id": shot.episode_id, "shot_id": shot.shot_id, "version": shot.version}
         for shot in revision_impact(before, after, shots)),
        key=lambda row: (row["episode_id"], row["shot_id"], row["version"]),
    )
    ids = sorted({row["episode_id"] for row in affected})
    if isinstance(after, Shot):
        ids = sorted(set(ids) | {after.episode_id})
    # Validate every gate record before the durable plan or any mutation.
    for episode_id in ids:
        store.load(Episode, episode_id)
    return {
        "cascade_id": uuid.uuid4().hex,
        "entity_type": type(before).__name__,
        "before": before.model_dump(mode="json"),
        "after": after.model_dump(mode="json"),
        "expected_version": expected_version,
        "episode_ids": ids,
        "affected": affected,
    }


def _same(a: VersionedEntity, payload: dict[str, Any]) -> bool:
    return a.model_dump(mode="json") == payload


def _revoke_gates(store: ProjectStore, plan: dict[str, Any], revoked: list[str]) -> None:
    for episode_id in plan["episode_ids"]:
        episode, token = store.load_with_version(Episode, episode_id)
        if episode.approved_cut_version is None and episode.approval_review_id is None:
            continue
        store.save(episode.model_copy(update={"approved_cut_version": None, "approval_review_id": None}),
                   expected_version=token)
        revoked.append(episode_id)


def _commit_upstream(store: ProjectStore, plan: dict[str, Any]) -> bool:
    cls = _ENTITY_TYPES[plan["entity_type"]]
    after = cls.model_validate(plan["after"])
    before = cls.model_validate(plan["before"])
    record_id = _record_id(after)
    episode_id = getattr(after, "episode_id", None)
    current, token = store.load_with_version(cls, record_id, episode_id=episode_id)
    if _same(current, plan["after"]):
        return False
    if current.version != before.version or not _same(current, plan["before"]):
        raise RuntimeError(f"upstream {record_id} diverged from both planned before and after payloads")
    # A changed lifecycle token on the same content is safe only when it is still the planned CAS token.
    if token != plan["expected_version"]:
        raise VersionConflict(f"cascade/{plan['cascade_id']}/{record_id}", plan["expected_version"], token, current)
    store.revise(after, expected_version=token)
    return True


def _stale_affected(store: ProjectStore, plan: dict[str, Any], changed: list[str]) -> None:
    if plan["entity_type"] == "Shot":
        return  # the planned successor is itself the fresh draft for this Shot
    for row in plan["affected"]:
        current, token = store.load_with_version(Shot, row["shot_id"], episode_id=row["episode_id"])
        if current.version != row["version"]:
            raise RuntimeError(
                f"affected Shot {row['episode_id']}/{row['shot_id']} diverged from planned version {row['version']}"
            )
        if current.status is ShotStatus.STALE:
            continue
        if current.status is ShotStatus.APPROVED:
            check_transition(current.status, ShotStatus.STALE, TransitionContext())
            store.update(current.model_copy(update={"status": ShotStatus.STALE}), expected_version=token)
            changed.append(current.shot_id)
        # Review and draft records retain their lifecycle state; the journal fences them.


def _roll_forward(store: ProjectStore, plan: dict[str, Any]) -> tuple[str, ...]:
    phase = "gate revocation"
    revoked: list[str] = []
    upstream_committed = False
    stale_ids: list[str] = []
    try:
        _revoke_gates(store, plan, revoked)
        phase = "upstream commit"
        upstream_committed = _commit_upstream(store, plan)
        phase = "Shot staleness"
        _stale_affected(store, plan, stale_ids)
        phase = "completion marker"
        store.complete_cascade(plan["cascade_id"])
        return tuple(stale_ids)
    except VersionConflict as exc:
        progress = (
            f"phase={phase}, gates revoked={list(revoked)}, "
            f"upstream newly committed={upstream_committed}, shots newly stale={list(stale_ids)}"
        )
        conflict_message = (
            f"cascade {plan['cascade_id']} remains pending ({progress}); "
            f"resolve conflict and resume_invalidations(store); cause: {exc}"
        )
        exc.args = (conflict_message,)
        raise
    except Exception as exc:
        progress = (
            f"phase={phase}, gates revoked={list(revoked)}, "
            f"upstream newly committed={upstream_committed}, shots newly stale={list(stale_ids)}"
        )
        raise JobError(
            JobErrorKind.INTERNAL,
            f"cascade {plan['cascade_id']} remains pending ({progress}); "
            f"retry resume_invalidations(store); cause: {exc}", cause=exc,
        ) from exc


def resume_invalidations(store: ProjectStore) -> tuple[str, ...]:
    """Roll all pending cascade plans forward under the shared service command lock."""
    with store.command():
        validated_cascades(store)
        plans = store.cascades(pending_only=True)
        for plan in plans:
            for episode_id in plan["episode_ids"]:
                try:
                    store.load(Episode, episode_id)
                except Exception as exc:
                    raise JobError(JobErrorKind.INVALID_MANIFEST,
                                   f"cascade {plan['cascade_id']} references invalid Episode {episode_id}; cause: {exc}",
                                   cause=exc) from exc
            _roll_forward(store, plan)
        return tuple(plan["cascade_id"] for plan in plans)


def _revise(store: ProjectStore, revised: VersionedEntity, expected_version: int,
            episode_ids: Iterable[str] = ()) -> tuple[str, ...]:
    _pending_error(store)
    before, token = store.validate_revision(revised, expected_version=expected_version)
    shots = _shots(store, set(episode_ids) | ({revised.episode_id} if isinstance(revised, Script) else set()))
    ids = [revised.episode_id] if isinstance(revised, Shot) else []
    plan = _plan(store, before, revised, token, shots, set(episode_ids) | set(ids))
    try:
        store.record_cascade(plan)
    except Exception as exc:
        cause = exc.__cause__ or exc
        mapped = JobError.from_exception(cause)
        mapped.message = (
            f"cascade {plan['cascade_id']} plan persistence failed; if the plan exists, "
            f"call resume_invalidations(store); cause: {cause}"
        )
        mapped.args = (mapped.message,)
        mapped.__cause__ = cause
        raise mapped from exc
    return _roll_forward(store, plan)


def revise_script(store: ProjectStore, revised: Script, *, expected_version: int) -> tuple[str, ...]:
    with store.command():
        return _revise(store, revised, expected_version)


def revise_location(store: ProjectStore, revised: Location, *, expected_version: int,
                    episode_ids: Iterable[str]) -> tuple[str, ...]:
    with store.command():
        return _revise(store, revised, expected_version, episode_ids)


def revise_camera_profile(store: ProjectStore, revised: CameraProfile, *, expected_version: int,
                           episode_ids: Iterable[str]) -> tuple[str, ...]:
    with store.command():
        return _revise(store, revised, expected_version, episode_ids)


def revise_shot(store: ProjectStore, revised: Shot, *, expected_version: int) -> Shot:
    with store.command():
        _revise(store, revised, expected_version, [revised.episode_id])
        return revised
