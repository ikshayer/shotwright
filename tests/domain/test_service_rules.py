"""Independent contract tests for approval binding and Script impact policy."""
from datetime import UTC, datetime

import pytest

from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import Beat, Review, Script, Shot
from orchestrator.domain.invalidation import shots_invalidated_by_script
from orchestrator.domain.state import ReviewApprovalError, check_review_approval


def opinion(**overrides):
    values = {"review_id": "r1", "target_id": "s1", "target_version": 2,
              "target_kind": "shot", "episode_id": "ep1", "actor": "creator",
              "created_at": datetime(2026, 11, 4, tzinfo=UTC),
              "approval_state": ShotStatus.APPROVED}
    return Review(**(values | overrides))


def beat(beat_id="b1", **overrides):
    return Beat(**({"beat_id": beat_id, "target_sec": 3, "subtlety": 2} | overrides))


def script(**overrides):
    values = {"script_id": "script1", "episode_id": "ep1", "logline": "Night shift",
              "beats": [beat("b1"), beat("b2")]}
    return Script(**(values | overrides))


def approved(shot_id, **overrides):
    values = {"shot_id": shot_id, "episode_id": "ep1", "duration_sec": 5,
              "status": ShotStatus.APPROVED, "approval_review_id": f"r-{shot_id}"}
    return Shot(**(values | overrides))


def test_domain_approval_accepts_exact_target_and_version_without_mutation():
    record = opinion()
    snapshot = record.model_dump()
    assert check_review_approval(
        record, target_kind="shot", target_id="s1", target_version=2,
        episode_id="ep1", agent_may_approve=False,
    ) is None
    assert record.model_dump() == snapshot


@pytest.mark.parametrize("overrides", [
    {"target_id": "other"}, {"target_version": 1},
    {"approval_state": ShotStatus.REJECTED},
])
def test_domain_approval_refuses_wrong_binding_or_opinion(overrides):
    record = opinion(**overrides)
    snapshot = record.model_dump()
    with pytest.raises(ReviewApprovalError) as caught:
        check_review_approval(
            record, target_kind="shot", target_id="s1", target_version=2,
            episode_id="ep1", agent_may_approve=False,
        )
    assert str(caught.value)
    assert record.model_dump() == snapshot


@pytest.mark.parametrize("operation", ["edit", "add", "remove"])
def test_script_beat_content_addition_and_removal_select_dependent_approved_shots(operation):
    before = script()
    if operation == "edit":
        after = script(beats=[beat("b1", target_sec=4), beat("b2")])
        affected = "b1"
    elif operation == "add":
        after = script(beats=[beat("b1"), beat("b2"), beat("b3")])
        affected = "b3"
    else:
        after = script(beats=[beat("b1")])
        affected = "b2"
    shots = [approved("hit", dependencies=[affected]),
             approved("unrelated", dependencies=["unrelated-beat"])]
    decision = shots_invalidated_by_script(before, after, shots)
    assert decision.shot_ids == ("hit",)
    assert decision.revokes_cut_approval
    assert decision.reason


def test_script_change_selects_dependency_beat_id_and_mapping_links_in_input_order():
    before = script(beat_to_shot={"b1": "mapped"})
    after = script(beats=[beat("b1", narration_text="hello"), beat("b2")],
                   beat_to_shot={"b1": "mapped"})
    shots = [approved("mapped"), approved("beat-field", beat_id="b1"),
             approved("dependency", dependencies=["b1"]), approved("unrelated", beat_id="b2")]
    snapshots = [record.model_dump() for record in [before, after, *shots]]
    decision = shots_invalidated_by_script(before, after, iter(shots))
    assert decision.shot_ids == ("mapped", "beat-field", "dependency")
    assert [record.model_dump() for record in [before, after, *shots]] == snapshots


@pytest.mark.parametrize("before_mapping,after_mapping,expected", [
    ({"b1": "old"}, {"b1": "new"}, ("new", "old", "dependent")),
    ({}, {"b1": "new"}, ("new", "dependent")),
    ({"b1": "old"}, {}, ("old", "dependent")),
])
def test_mapping_only_revision_selects_old_new_and_beat_dependents(
    before_mapping, after_mapping, expected,
):
    before, after = script(beat_to_shot=before_mapping), script(beat_to_shot=after_mapping)
    shots = [approved("new"), approved("old"),
             approved("dependent", dependencies=["b1"]), approved("unrelated")]
    assert shots_invalidated_by_script(before, after, shots).shot_ids == expected


def test_script_change_excludes_other_episode_and_nonapproved_shots():
    before = script()
    after = script(beats=[beat("b1", subtlety=4), beat("b2")])
    shots = [approved("hit", beat_id="b1"),
             approved("foreign", beat_id="b1", episode_id="ep2")]
    shots.extend(approved(state.value, beat_id="b1", status=state)
                 for state in ShotStatus if state is not ShotStatus.APPROVED)
    assert shots_invalidated_by_script(before, after, shots).shot_ids == ("hit",)


def test_script_change_selects_a_shot_once_even_when_multiple_links_match():
    before = script(beat_to_shot={"b1": "hit", "b2": "hit"})
    after = script(beats=[beat("b1", target_sec=4), beat("b2", target_sec=4)],
                   beat_to_shot={"b1": "hit", "b2": "hit"})
    record = approved("hit", beat_id="b1", dependencies=["b1", "b2"])
    assert shots_invalidated_by_script(before, after, [record]).shot_ids == ("hit",)


@pytest.mark.parametrize("changes", [
    {}, {"version": 2, "supersedes": 1}, {"approval_review_id": "new-review"},
    {"logline": "A different synopsis"}, {"beats": [beat("b2"), beat("b1")]},
])
def test_nonimpacting_script_revision_selects_no_shots(changes):
    before = script(beat_to_shot={"b1": "hit"})
    after = before.model_copy(update=changes)
    decision = shots_invalidated_by_script(before, after, [approved("hit", dependencies=["b1"])])
    assert decision.shot_ids == ()
    assert not decision.revokes_cut_approval


@pytest.mark.parametrize("changes", [{"script_id": "other"}, {"episode_id": "ep2"}])
def test_script_diff_rejects_different_identity(changes):
    with pytest.raises(ValueError):
        shots_invalidated_by_script(script(), script(**changes), [])


def test_script_change_deduplicates_repeated_shots_in_first_seen_order():
    before = script()
    after = script(beats=[beat("b1", target_sec=4), beat("b2")])
    first = approved("first", dependencies=["b1"])
    second = approved("second", beat_id="b1")
    shots = [first, first, second, first, second]
    assert shots_invalidated_by_script(before, after, iter(shots)).shot_ids == ("first", "second")
