"""The approval state machine of 10.2.

Pure: no I/O, only domain record and enum imports, so the guards are
testable without a store, a renderer or a GPU.

Two rules in 10.2 are easy to state and easy to violate in code, so they are
enforced here rather than left to callers:

  - a shot leaves `blocking` only when its framing is approved (FR-29);
  - regeneration never auto-approves - a rebuilt shot enters `review`.

`Review` is the only writer of approval state (10.1), so the transition into
`approved` requires the approving review's id.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .base import FramingStatus, ShotStatus, VersionedEntity
from .entities import (
    CameraProfile,
    Location,
    Review,
    ReviewTargetKind,
    Script,
    Shot,
    StyleBible,
)

S = ShotStatus

#: Allowed edges. Anything absent here is not a transition, and the error says so.
_EDGES: dict[ShotStatus, frozenset[ShotStatus]] = {
    S.DRAFT: frozenset({S.PLANNED}),
    S.PLANNED: frozenset({S.BLOCKING}),
    # blocking -> blocking is the FR-29 compare loop iterating
    S.BLOCKING: frozenset({S.BLOCKING, S.BUILDING}),
    S.BUILDING: frozenset({S.REVIEW, S.POST_STALE, S.FAILED}),
    S.FAILED: frozenset({S.BUILDING}),
    S.REVIEW: frozenset({S.APPROVED, S.REJECTED, S.POST_STALE}),
    S.APPROVED: frozenset({S.STALE, S.POST_STALE}),
    S.POST_STALE: frozenset({S.BUILDING, S.STALE}),
    S.REJECTED: frozenset(),
    S.STALE: frozenset(),
}

#: Terminal for this version - derived, so it cannot drift from the edges.
_REQUIRES_NEW_VERSION: frozenset[ShotStatus] = frozenset(
    status for status, targets in _EDGES.items() if not targets
)


class TransitionError(Exception):
    """A refused transition. Carries enough to tell the operator what to fix."""

    def __init__(self, current: ShotStatus, target: ShotStatus, reason: str) -> None:
        super().__init__(f"cannot move {current.value} -> {target.value}: {reason}")
        self.current = current
        self.target = target
        self.reason = reason


@dataclass(frozen=True)
class TransitionContext:
    """Everything a guard may consult. Explicit, so guards stay pure."""

    framing_status: FramingStatus = FramingStatus.NONE
    approving_review_id: str | None = None
    is_rebuild: bool = False


def allowed_targets(current: ShotStatus) -> frozenset[ShotStatus]:
    return _EDGES[current]


def requires_new_version(status: ShotStatus) -> bool:
    """True where the only way forward is a successor record (10.2)."""
    return status in _REQUIRES_NEW_VERSION


def check_transition(
    current: ShotStatus,
    target: ShotStatus,
    context: TransitionContext | None = None,
) -> None:
    """Raise `TransitionError` unless the move is legal. Returns None on success."""
    ctx = context or TransitionContext()

    if target not in _EDGES[current]:
        if requires_new_version(current):
            raise TransitionError(
                current, target,
                "this version is terminal; create a successor version with `supersedes` set",
            )
        permitted = ", ".join(sorted(s.value for s in _EDGES[current])) or "nothing"
        raise TransitionError(current, target, f"only {permitted} is reachable from here")

    if current is S.BLOCKING and target is S.BUILDING:
        if ctx.framing_status is FramingStatus.PENDING:
            raise TransitionError(
                current, target,
                "framing is pending; approve the FR-29 framing preview before production",
            )
        if ctx.framing_status not in {FramingStatus.APPROVED, FramingStatus.NONE}:
            raise TransitionError(
                current, target,
                "framing_status must be 'approved' or 'none' before production",
            )

    if target is S.APPROVED:
        if not ctx.approving_review_id:
            raise TransitionError(
                current, target,
                "approval requires the id of the Review that granted it; "
                "Review is the only writer of approval state",
            )
        if ctx.is_rebuild:
            raise TransitionError(
                current, target,
                "a rebuilt shot enters 'review', never 'approved' - regeneration "
                "does not carry the previous approval forward",
            )


class ReviewApprovalError(ValueError):
    """A Review does not approve the exact requested target version."""


def check_review_approval(
    review: Review,
    *,
    target_kind: ReviewTargetKind,
    target_id: str,
    target_version: int,
    episode_id: str | None,
    agent_may_approve: bool,
) -> None:
    """Require a Review to approve exactly the scoped requested target version.

    The guard is pure and shared by all approval callers.
    """
    if review.target_kind is None:
        raise ReviewApprovalError(
            f"unscoped legacy Review {review.review_id!r}; record a new Review"
        )
    if review.target_kind != target_kind:
        raise ReviewApprovalError(
            f"Review {review.review_id!r} targets kind {review.target_kind!r}, "
            f"not {target_kind!r}"
        )
    if review.target_id != target_id:
        raise ReviewApprovalError(
            f"Review {review.review_id!r} targets {review.target_id!r}, "
            f"not {target_id!r}"
        )
    if review.target_version != target_version:
        raise ReviewApprovalError(
            f"Review {review.review_id!r} targets version {review.target_version}, "
            f"not version {target_version} of {target_id!r}"
        )
    if review.episode_id != episode_id:
        raise ReviewApprovalError(
            f"Review {review.review_id!r} targets episode {review.episode_id!r}, "
            f"not episode {episode_id!r}"
        )
    if review.approval_state is not ShotStatus.APPROVED:
        raise ReviewApprovalError(
            f"Review {review.review_id!r} records {review.approval_state.value}, "
            "not approval"
        )
    if review.actor == "agent" and not agent_may_approve:
        raise ReviewApprovalError(
            f"Review {review.review_id!r} is an agent-authored opinion; agent Reviews are "
            "opinions unless approvals.agent_may_approve is enabled"
        )


class RevisionError(ValueError):
    """A proposed successor violates identity, version, or approval rules."""


def check_revision(before: VersionedEntity, after: VersionedEntity) -> None:
    """Validate a versioned successor without consulting storage or mutating it."""
    if type(before) is not type(after):
        raise RevisionError("a revision must keep the same concrete entity type")
    identity_fields = {
        Script: ("script_id", "episode_id"),
        Location: ("location_id",),
        CameraProfile: ("camera_id",),
        Shot: ("shot_id", "episode_id"),
        StyleBible: ("project_id",),
    }
    fields = identity_fields.get(type(before))
    if fields is None:
        raise RevisionError(f"{type(before).__name__} is not a revisable entity")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise RevisionError("a revision must keep the same entity identity")
    if after.version != before.version + 1 or after.supersedes != before.version:
        raise RevisionError(
            f"a revision of version {before.version} must be version {before.version + 1} "
            f"with supersedes={before.version}"
        )
    if getattr(after, "approval_review_id", None) is not None:
        raise RevisionError("a revision never inherits approval")
    if isinstance(after, Shot) and after.status is not ShotStatus.DRAFT:
        raise RevisionError("a revised Shot must be draft")
    if (
        isinstance(before, Shot)
        and before.camera_profile != after.camera_profile
        and after.camera_profile_version is not None
    ):
        raise RevisionError(
            "changing Shot.camera_profile must clear camera_profile_version; "
            "accept framing against the new profile to pin it"
        )


def check_dependency_freshness(
    shot: Shot, *, invalidated: Iterable[tuple[str, str, int]],
) -> None:
    """Reject approval when this exact content version has an invalidation fence."""
    try:
        matches = (shot.episode_id, shot.shot_id, shot.version) in invalidated
    except TypeError as exc:
        raise TypeError("invalidated must be an iterable of (episode_id, shot_id, version)") from exc
    if matches:
        raise ReviewApprovalError(
            f"Shot {shot.shot_id}@{shot.version} has an upstream invalidation; "
            "create a fresh successor and obtain its Review"
        )


def check_episode_shots_for_cut(shots: Iterable[Shot]) -> None:
    """Reject cut approval while any current Shot of the Episode, in the cut or not, is
    stale, a draft or post-stale (the rule before cuts were bound to their inputs)."""
    for shot in shots:
        if shot.status in {ShotStatus.STALE, ShotStatus.DRAFT, ShotStatus.POST_STALE}:
            raise ReviewApprovalError(
                f"cut approval is blocked by current Shot {shot.shot_id} in {shot.status.value} state"
            )


def check_cut_approval(*, pending: bool, shots: Iterable[Shot]) -> None:
    """Reject cut approval unless every Shot is eligible for a current degraded output."""
    if pending:
        raise ReviewApprovalError("cut approval is blocked while dependency recovery is pending")
    for shot in shots:
        if shot.status not in {ShotStatus.REVIEW, ShotStatus.APPROVED}:
            raise ReviewApprovalError(
                f"cut approval is blocked by current Shot {shot.shot_id} in {shot.status.value} state; "
                "only review or approved Shots can enter a cut"
            )
