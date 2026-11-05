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

from dataclasses import dataclass

from .base import FramingStatus, ShotStatus
from .entities import Review

S = ShotStatus

#: Allowed edges. Anything absent here is not a transition, and the error says so.
_EDGES: dict[ShotStatus, frozenset[ShotStatus]] = {
    S.DRAFT: frozenset({S.PLANNED}),
    S.PLANNED: frozenset({S.BLOCKING}),
    # blocking -> blocking is the FR-29 compare loop iterating
    S.BLOCKING: frozenset({S.BLOCKING, S.BUILDING}),
    S.BUILDING: frozenset({S.REVIEW, S.FAILED}),
    S.FAILED: frozenset({S.BUILDING}),
    S.REVIEW: frozenset({S.APPROVED, S.REJECTED}),
    S.APPROVED: frozenset({S.STALE}),
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

    if (
        current is S.BLOCKING
        and target is S.BUILDING
        and ctx.framing_status is not FramingStatus.APPROVED
    ):
        raise TransitionError(
            current, target,
            "framing_status must be 'approved' first - resolve the FR-29 compare loop",
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
    target_id: str,
    target_version: int,
) -> None:
    """Require a Review to approve exactly ``target_id@target_version``.

    The guard is pure and shared by Shots, StyleBibles, and Episode cut versions.
    """
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
    if review.approval_state is not ShotStatus.APPROVED:
        raise ReviewApprovalError(
            f"Review {review.review_id!r} records {review.approval_state.value}, "
            "not approval"
        )
