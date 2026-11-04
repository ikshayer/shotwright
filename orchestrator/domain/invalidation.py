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
from .entities import CameraProfile, Shot


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


#: 10.2: "any shot moving to `stale` or `draft` revokes that cut approval".
#: The `draft` case is a rejected shot's successor re-entering the pipeline,
#: which is a different route in but has the same effect on gate C.
_REVOKES_CUT_APPROVAL: frozenset[ShotStatus] = frozenset(
    {ShotStatus.STALE, ShotStatus.DRAFT}
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
        if s.status is ShotStatus.APPROVED and s.camera_id == diff.camera_id
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
