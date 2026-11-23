"""What a change invalidates (PRD 4.4, FR-11).

FR-07 may only promise "no re-render" for fields post-processing can actually
change, so the promise is decided here from the field tags rather than from a
caller's judgement.

Pure: takes records, returns decisions. No store, no renderer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .base import Invalidation, ShotStatus
from .entities import CameraProfile, Location, Script, Shot


def field_invalidation(model: type, name: str) -> Invalidation | None:
    """The 4.4 class of a field, or None if the field carries no tag."""
    info = model.model_fields.get(name)
    if info is None:
        return None
    extra = info.json_schema_extra or {}
    if not isinstance(extra, dict):
        return None
    raw = extra.get("invalidation")
    return Invalidation(raw) if raw else None


@dataclass(frozen=True)
class ProfileDiff:
    """The outcome of comparing two CameraProfile versions."""

    camera_id: str = ""
    post_only: tuple[str, ...] = ()
    rebuild: tuple[str, ...] = ()
    untagged: tuple[str, ...] = ()

    @property
    def requires_rebuild(self) -> bool:
        """Untagged counts as rebuild: unclassified must not silently mean cheap."""
        return bool(self.rebuild or self.untagged)

    @property
    def changed(self) -> tuple[str, ...]:
        return self.post_only + self.rebuild + self.untagged


def diff_camera_profile(before: CameraProfile, after: CameraProfile) -> ProfileDiff:
    """Classify every changed field by its 4.4 tag."""
    if before.camera_id != after.camera_id:
        raise ValueError(
            f"cannot diff different cameras: {before.camera_id!r} vs {after.camera_id!r}"
        )
    post, rebuild, untagged = [], [], []
    ignore = {"version", "supersedes", "schema_version"}
    for name in CameraProfile.model_fields:
        if name in ignore:
            continue
        if getattr(before, name) == getattr(after, name):
            continue
        match field_invalidation(CameraProfile, name):
            case Invalidation.POST_ONLY:
                post.append(name)
            case Invalidation.REBUILD:
                rebuild.append(name)
            case _:
                untagged.append(name)
    return ProfileDiff(after.camera_id, tuple(post), tuple(rebuild), tuple(untagged))


@dataclass(frozen=True)
class Invalidated:
    """Shots moved to `stale`, and why. Artifacts are retained (10.2)."""

    shot_ids: tuple[str, ...] = ()
    reason: str = ""

    @property
    def revokes_cut_approval(self) -> bool:
        """Derived, so it cannot contradict `shot_ids` (10.2, gate scope)."""
        return bool(self.shot_ids)


#: 10.2: a Shot moving to `stale`, `post_stale` or `draft` revokes its cut approval.
#: The `draft` case is a rejected shot's successor re-entering the pipeline,
#: which is a different route in but has the same effect on gate C.
_REVOKES_CUT_APPROVAL: frozenset[ShotStatus] = frozenset(
    {ShotStatus.STALE, ShotStatus.POST_STALE, ShotStatus.DRAFT}
)


def revokes_cut_approval(target: ShotStatus) -> bool:
    """Whether a shot entering `target` invalidates the episode's gate-C cut."""
    return target in _REVOKES_CUT_APPROVAL


def shots_invalidated_by(
    changed_id: str,
    shots: Iterable[Shot],
    reason: str,
) -> Invalidated:
    """Approved shots depending on `changed_id` become stale.

    10.2: invalidation retains artifacts and surfaces the shot; it never
    deletes and never silently re-approves. Any shot leaving `approved` also
    revokes the episode's gate-C cut approval.
    """
    hits = tuple(
        s.shot_id
        for s in shots
        if changed_id in s.dependencies and s.status is ShotStatus.APPROVED
    )
    return Invalidated(hits, reason)


def shots_invalidated_by_profile(
    diff: ProfileDiff,
    shots: Iterable[Shot],
) -> Invalidated:
    """Apply a CameraProfile diff. Post-only changes invalidate nothing.

    Only shots using the changed camera are affected. The previous version
    invalidated every approved shot passed in, so handing it an episode
    staled footage from unrelated cameras.
    """
    if not diff.changed:
        return Invalidated((), "no change")
    if not diff.requires_rebuild:
        return Invalidated(
            (),
            f"post-only change ({', '.join(diff.post_only)}); re-derived from the master",
        )
    detail = ", ".join(diff.rebuild + diff.untagged)
    hits = tuple(
        s.shot_id
        for s in shots
        if s.status is ShotStatus.APPROVED
        and s.camera_profile_version is None
        and (s.camera_id == diff.camera_id or s.camera_profile == diff.camera_id)
    )
    return Invalidated(hits, f"rebuild-triggering change ({detail})")


def agent_overwrites_creator_fields(
    shot: Shot,
    proposed: Mapping[str, object],
) -> tuple[str, ...]:
    """Creator-authored fields an agent pass would overwrite (FR-29).

    Returned rather than raised: FR-29 requires the conflict be *reported*,
    not silently fixed, and the caller decides how to surface it.
    """
    protected = shot.creator_authored
    return tuple(
        sorted(
            name
            for name, value in proposed.items()
            if name in protected and getattr(shot, name, None) != value
        )
    )


def shots_invalidated_by_script(
    before: Script,
    after: Script,
    shots: Iterable[Shot],
) -> Invalidated:
    """Select approved episode shots affected by Script beat or mapping edits.

    Logline, ordering, and version metadata do not define invalidation scope.
    The function is pure and preserves the supplied shot order and records.
    """
    if before.script_id != after.script_id:
        raise ValueError(
            f"cannot diff different scripts: {before.script_id!r} vs {after.script_id!r}"
        )
    if before.episode_id != after.episode_id:
        raise ValueError(
            f"cannot diff scripts from different episodes: "
            f"{before.episode_id!r} vs {after.episode_id!r}"
        )

    old_beats = {beat.beat_id: beat for beat in before.beats}
    new_beats = {beat.beat_id: beat for beat in after.beats}
    affected_beats = {
        beat_id
        for beat_id in old_beats.keys() | new_beats.keys()
        if old_beats.get(beat_id) != new_beats.get(beat_id)
    }

    old_mapping = before.beat_to_shot
    new_mapping = after.beat_to_shot
    mapping_changes = {
        beat_id
        for beat_id in old_mapping.keys() | new_mapping.keys()
        if old_mapping.get(beat_id) != new_mapping.get(beat_id)
    }
    affected_beats.update(mapping_changes)
    affected_shots = {
        shot_id
        for beat_id in affected_beats
        for shot_id in (old_mapping.get(beat_id), new_mapping.get(beat_id))
        if shot_id is not None
    }

    def scene_blueprints(script: Script) -> dict[str, object]:
        result: dict[str, object] = {}
        for scene in getattr(script, "scenes", []):
            payload = scene.model_dump(mode="python")
            for field in ("version", "supersedes", "schema_version", "approval_review_id"):
                payload.pop(field, None)
            result[scene.location_id] = payload
        return result

    old_scenes, new_scenes = scene_blueprints(before), scene_blueprints(after)
    changed_locations = {
        location_id for location_id in old_scenes.keys() | new_scenes.keys()
        if old_scenes.get(location_id) != new_scenes.get(location_id)
    }
    if changed_locations:
        affected_shots.update(
            shot.shot_id for shot in shots if shot.location_id in changed_locations
        )
        for script, mapping in ((before, old_mapping), (after, new_mapping)):
            for beat in script.beats:
                direction = getattr(beat, "scene_direction", None)
                if direction is not None and direction.location_id in changed_locations:
                    shot_id = mapping.get(beat.beat_id)
                    if shot_id is not None:
                        affected_shots.add(shot_id)

    if not affected_beats and not affected_shots:
        return Invalidated((), "no beat content or beat-to-shot mapping change")

    hits = tuple(dict.fromkeys(
        shot.shot_id
        for shot in shots
        if shot.episode_id == before.episode_id
        and shot.status is ShotStatus.APPROVED
        and (
            shot.beat_id in affected_beats
            or any(beat_id in shot.dependencies for beat_id in affected_beats)
            or shot.shot_id in affected_shots
            or shot.location_id in changed_locations
        )
    ))
    return Invalidated(hits, "Script beat content or beat-to-shot mapping changed")


def revision_impact(
    before: Script | Location | CameraProfile | Shot,
    after: Script | Location | CameraProfile | Shot,
    shots: Iterable[Shot],
) -> tuple[Shot, ...]:
    """Return all existing Shot heads impacted by one content revision.

    Unlike the historical approved-only selectors, this includes every Shot state so
    the durable dependency fence can prevent an old Review from being reused later.
    """
    if type(before) is not type(after):
        raise ValueError("revision impact requires matching entity types")
    cls = type(before)
    if isinstance(before, Script):
        if before.script_id != after.script_id or before.episode_id != after.episode_id:
            raise ValueError("cannot compare different Scripts")
        episode_shots = tuple(shot for shot in shots if shot.episode_id == before.episode_id)
        # Keep Script's existing selector authoritative. It is approved-only by
        # design, so normalize temporary copies to approved and map its selected
        # identities back to the original all-state Shot records.
        normalized = tuple(shot.model_copy(update={"status": ShotStatus.APPROVED})
                           for shot in episode_shots)
        selected = shots_invalidated_by_script(before, after, normalized)
        selected_ids = set(selected.shot_ids)
        candidates = (shot for shot in episode_shots if shot.shot_id in selected_ids)
    elif isinstance(before, Location):
        if before.location_id != after.location_id:
            raise ValueError("cannot compare different Locations")
        changed_fields = tuple(
            name
            for name in Location.model_fields
            if name not in {"schema_version", "version", "supersedes"}
            and getattr(before, name) != getattr(after, name)
        )
        # Untagged Location content remains conservatively rebuild-triggering. Explicit
        # tags let the dependency graph honor a future post-only field without weakening
        # the existing behavior for geometry provenance or camera rigs.
        if not any(
            field_invalidation(Location, name) is not Invalidation.POST_ONLY
            for name in changed_fields
        ):
            return ()
        candidates = (shot for shot in shots if before.location_id in shot.dependencies or shot.location_id == before.location_id)
    elif isinstance(before, CameraProfile):
        diff = diff_camera_profile(before, after)
        def uses_camera(shot: Shot) -> bool:
            return (
                shot.camera_profile_version is None
                and (shot.camera_id == before.camera_id or shot.camera_profile == before.camera_id)
            )

        if diff.requires_rebuild:
            candidates = (shot for shot in shots if uses_camera(shot))
        elif diff.post_only:
            # This is a candidate selector only. The service additionally checks the
            # latest degraded receipt's derived-spec hash before planning a post cascade.
            candidates = (
                shot for shot in shots
                if uses_camera(shot)
                and shot.status in {ShotStatus.REVIEW, ShotStatus.APPROVED}
            )
        else:
            return ()
    elif isinstance(before, Shot):
        if before.shot_id != after.shot_id or before.episode_id != after.episode_id:
            raise ValueError("cannot compare different Shots")
        if before == after:
            return ()
        candidates = (shot for shot in shots if shot.episode_id == before.episode_id and shot.shot_id == before.shot_id)
    else:
        raise TypeError(f"unsupported revision impact type: {cls.__name__}")
    unique: dict[tuple[str, str], Shot] = {}
    for shot in candidates:
        unique.setdefault((shot.episode_id, shot.shot_id), shot)
    return tuple(unique.values())
