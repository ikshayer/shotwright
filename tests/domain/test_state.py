"""10.2 approval state machine."""

import itertools

import pytest

from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.state import (
    TransitionContext,
    TransitionError,
    allowed_targets,
    check_transition,
    requires_new_version,
)

S = ShotStatus

#: Edges drawn in the 10.2 diagram (plus the blocking self-loop of FR-29).
LEGAL = {
    (S.DRAFT, S.PLANNED),
    (S.PLANNED, S.BLOCKING),
    (S.BLOCKING, S.BLOCKING),
    (S.BLOCKING, S.BUILDING),
    (S.BUILDING, S.REVIEW),
    (S.BUILDING, S.POST_STALE),
    (S.BUILDING, S.FAILED),
    (S.FAILED, S.BUILDING),
    (S.REVIEW, S.APPROVED),
    (S.REVIEW, S.REJECTED),
    (S.REVIEW, S.POST_STALE),
    (S.APPROVED, S.STALE),
    (S.APPROVED, S.POST_STALE),
    (S.POST_STALE, S.BUILDING),
    (S.POST_STALE, S.STALE),
}
ILLEGAL = sorted(set(itertools.product(S, S)) - LEGAL)

#: Context that satisfies every guard, so edge tests isolate topology.
OK_CTX = TransitionContext(
    framing_status=FramingStatus.APPROVED, approving_review_id="rev-1"
)


@pytest.mark.parametrize("current,target", sorted(LEGAL))
def test_legal_edge_is_permitted(current, target):
    check_transition(current, target, OK_CTX)
    assert target in allowed_targets(current)


@pytest.mark.parametrize("current,target", ILLEGAL)
def test_illegal_edge_is_refused_even_with_every_guard_satisfied(current, target):
    with pytest.raises(TransitionError) as exc:
        check_transition(current, target, OK_CTX)
    assert exc.value.current is current
    assert exc.value.target is target


def test_allowed_targets_matches_legal_table_exactly():
    for status in S:
        assert allowed_targets(status) == {t for c, t in LEGAL if c is status}


@pytest.mark.parametrize("framing", [FramingStatus.NONE, FramingStatus.PENDING])
def test_blocking_gate_allows_optional_none_and_refuses_pending(framing):
    context = TransitionContext(framing_status=framing)
    if framing is FramingStatus.PENDING:
        with pytest.raises(TransitionError, match="framing is pending") as exc:
            check_transition(S.BLOCKING, S.BUILDING, context)
        assert "approve the FR-29 framing preview" in str(exc.value), (
            f"pending framing refusal did not say how to resolve the gate: {exc.value}"
        )
    else:
        check_transition(S.BLOCKING, S.BUILDING, context)


def test_blocking_default_context_allows_optional_framing_to_pass():
    check_transition(S.BLOCKING, S.BUILDING)
    assert S.BUILDING in allowed_targets(S.BLOCKING), (
        "default framing context represents no panel, which the creator decided passes"
    )


def test_blocking_leaves_once_framing_approved():
    ctx = TransitionContext(framing_status=FramingStatus.APPROVED)
    check_transition(S.BLOCKING, S.BUILDING, ctx)


@pytest.mark.parametrize("framing", list(FramingStatus))
def test_blocking_self_loop_needs_no_framing_approval(framing):
    # FR-29 compare loop iterates while framing is unresolved.
    check_transition(S.BLOCKING, S.BLOCKING, TransitionContext(framing_status=framing))


@pytest.mark.parametrize("review_id", [None, ""])
def test_approval_requires_review_id(review_id):
    with pytest.raises(TransitionError, match="Review"):
        check_transition(S.REVIEW, S.APPROVED, TransitionContext(approving_review_id=review_id))


def test_approval_with_review_id_succeeds():
    ctx = TransitionContext(approving_review_id="rev-9")
    check_transition(S.REVIEW, S.APPROVED, ctx)


def test_rebuilt_shot_cannot_go_straight_to_approved():
    ctx = TransitionContext(approving_review_id="rev-9", is_rebuild=True)
    with pytest.raises(TransitionError, match="review"):
        check_transition(S.REVIEW, S.APPROVED, ctx)


def test_rebuilt_shot_may_enter_review():
    ctx = TransitionContext(is_rebuild=True)
    check_transition(S.BUILDING, S.REVIEW, ctx)


def test_rebuild_cannot_reach_approved_from_any_state():
    ctx = TransitionContext(approving_review_id="rev-9", is_rebuild=True)
    for current in S:
        with pytest.raises(TransitionError):
            check_transition(current, S.APPROVED, ctx)


@pytest.mark.parametrize("terminal", [S.REJECTED, S.STALE])
@pytest.mark.parametrize("target", list(S))
def test_terminal_versions_have_no_exit_and_say_successor_needed(terminal, target):
    assert allowed_targets(terminal) == frozenset()
    assert requires_new_version(terminal)
    with pytest.raises(TransitionError, match="successor version") as exc:
        check_transition(terminal, target, OK_CTX)
    assert "supersedes" in exc.value.reason


@pytest.mark.parametrize("status", [s for s in S if s not in (S.REJECTED, S.STALE)])
def test_non_terminal_states_do_not_demand_a_successor(status):
    assert not requires_new_version(status)
    for target in S:
        if (status, target) not in LEGAL:
            with pytest.raises(TransitionError) as exc:
                check_transition(status, target, OK_CTX)
            assert "successor" not in str(exc.value)


def test_failed_retry_goes_back_to_building_not_review():
    check_transition(S.FAILED, S.BUILDING)
    with pytest.raises(TransitionError):
        check_transition(S.FAILED, S.REVIEW, OK_CTX)


def test_approved_can_become_stale_or_post_stale():
    assert allowed_targets(S.APPROVED) == {S.STALE, S.POST_STALE}
