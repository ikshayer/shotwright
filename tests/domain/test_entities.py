"""10.1 entity invariants."""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from orchestrator.domain.base import (
    InputKind,
    Invalidation,
    ReferenceRole,
    ShotStatus,
)
from orchestrator.domain.entities import (
    Attestation,
    Beat,
    CameraProfile,
    Episode,
    Reference,
    Review,
    RubricScores,
    Script,
    Shot,
    Timeline,
    TimelineEntry,
)
from orchestrator.domain.invalidation import field_invalidation

NOW = datetime(2026, 11, 4, 12, 0, 0)


def make_shot(**kw) -> Shot:
    return Shot(shot_id="S1", episode_id="EP1", duration_sec=10.0, **kw)


def make_ref(**kw) -> Reference:
    return Reference(
        reference_id="R1", input_kind=InputKind.IMAGE_UPLOAD,
        role=ReferenceRole.MOOD, path_or_url="refs/a.png", **kw,
    )


# ---- Shot ---------------------------------------------------------------


def test_approved_shot_without_review_id_is_rejected():
    with pytest.raises(ValidationError, match="approval_review_id"):
        make_shot(status=ShotStatus.APPROVED)


def test_approved_shot_with_empty_review_id_is_rejected():
    with pytest.raises(ValidationError):
        make_shot(status=ShotStatus.APPROVED, approval_review_id="")


def test_approved_shot_with_review_id_is_accepted():
    assert make_shot(status=ShotStatus.APPROVED, approval_review_id="rev-1").status \
        is ShotStatus.APPROVED


def test_assigning_approved_without_review_is_rejected():
    s = make_shot(status=ShotStatus.REVIEW)
    with pytest.raises(ValidationError):
        s.status = ShotStatus.APPROVED


@pytest.mark.parametrize("status", [s for s in ShotStatus if s is not ShotStatus.APPROVED])
def test_non_approved_shot_needs_no_review_id(status):
    assert make_shot(status=status).approval_review_id is None


def test_event_must_fall_within_duration():
    with pytest.raises(ValidationError, match="event_sec"):
        make_shot(event_sec=11.0)
    assert make_shot(event_sec=10.0).event_sec == 10.0


def test_successor_version_links_to_predecessor():
    s = make_shot()
    nxt = Shot(**{**s.model_dump(), **s.next_version()})
    assert (nxt.version, nxt.supersedes) == (2, 1)


def test_creator_authored_is_read_directly():
    s = make_shot(creator_authored={"framing", "duration_sec"})
    assert s.creator_authored == {"framing", "duration_sec"}
    assert make_shot().creator_authored == set()
    assert not hasattr(s, "protected_fields")


def test_unknown_fields_are_refused():
    with pytest.raises(ValidationError):
        make_shot(bogus=1)


# ---- Reference ----------------------------------------------------------


def test_tier_three_without_attestation_is_rejected():
    with pytest.raises(ValidationError, match="attestation"):
        make_ref(inspection_tier=3)


def test_tier_three_with_attestation_is_accepted():
    att = Attestation(authorised_by="creator", method="checkbox", recorded_at=date(2026, 11, 4))
    assert make_ref(inspection_tier=3, attestation=att).inspection_tier == 3


@pytest.mark.parametrize("tier", [1, 2])
def test_lower_tiers_need_no_attestation(tier):
    assert make_ref(inspection_tier=tier).attestation is None


@pytest.mark.parametrize("tier", [0, 4])
def test_tier_outside_1_to_3_is_rejected(tier):
    with pytest.raises(ValidationError):
        make_ref(inspection_tier=tier)


def test_reference_end_must_follow_start():
    with pytest.raises(ValidationError):
        make_ref(start_sec=5.0, end_sec=5.0)
    assert make_ref(start_sec=5.0, end_sec=6.0).end_sec == 6.0


# ---- Review -------------------------------------------------------------


def make_review(state) -> Review:
    return Review(
        review_id="rev-1", target_id="S1", target_version=1,
        created_at=NOW, approval_state=state,
    )


@pytest.mark.parametrize("state", [ShotStatus.APPROVED, ShotStatus.REJECTED])
def test_review_can_record_approval_or_rejection(state):
    assert make_review(state).approval_state is state


@pytest.mark.parametrize(
    "state",
    [s for s in ShotStatus if s not in (ShotStatus.APPROVED, ShotStatus.REJECTED)],
)
def test_review_cannot_record_any_other_state(state):
    with pytest.raises(ValidationError, match="approval or rejection"):
        make_review(state)


def test_review_binds_to_a_version():
    with pytest.raises(ValidationError):
        Review(review_id="r", target_id="S1", target_version=0,
               created_at=NOW, approval_state=ShotStatus.APPROVED)


# ---- Rubric (4.3) -------------------------------------------------------

CRITERIA = (
    "mount_inferable", "plausible_place", "no_unmotivated_movement",
    "stable_light_and_geometry", "integrated_anomaly",
)
MANDATORY = "no_unmotivated_movement"  # criterion 3


def scores(**overrides) -> RubricScores:
    return RubricScores(**{c: overrides.get(c, True) for c in CRITERIA})


def test_all_five_pass():
    assert scores().passed()


@pytest.mark.parametrize("failing", [c for c in CRITERIA if c != MANDATORY])
def test_four_of_five_passes_when_criterion_three_holds(failing):
    assert scores(**{failing: False}).passed()


def test_four_of_five_failing_criterion_three_does_not_pass():
    assert not scores(**{MANDATORY: False}).passed()


@pytest.mark.parametrize("failing", [c for c in CRITERIA if c != MANDATORY])
def test_three_of_five_fails_even_with_criterion_three(failing):
    other = next(c for c in CRITERIA if c not in (failing, MANDATORY))
    assert not scores(**{failing: False, other: False}).passed()


def test_none_pass():
    assert not RubricScores(**{c: False for c in CRITERIA}).passed()


# ---- CameraProfile tags (4.4) ------------------------------------------


@pytest.mark.parametrize(
    "name",
    sorted(set(CameraProfile.model_fields)
           - {"camera_id", "profile_name", "version", "supersedes", "schema_version"}),
)
def test_every_camera_profile_field_carries_a_44_tag(name):
    assert field_invalidation(CameraProfile, name) in (
        Invalidation.POST_ONLY, Invalidation.REBUILD,
    )


# ---- Script -------------------------------------------------------------


def test_script_beat_ids_must_be_unique():
    beat = Beat(beat_id="B1", target_sec=3.0, subtlety=2)
    with pytest.raises(ValidationError, match="beat_id"):
        Script(script_id="sc", episode_id="EP1", logline="x", beats=[beat, beat])


# ---- Episode / Timeline --------------------------------------------------


def test_episode_approval_review_id_round_trips():
    ep = Episode(episode_id="EP1", project_id="P", number=1,
                 duration_target_sec=60.0, approval_review_id="rev-7")
    assert Episode(**ep.model_dump()).approval_review_id == "rev-7"
    assert Episode(episode_id="EP1", project_id="P", number=1,
                   duration_target_sec=60.0).approval_review_id is None


def test_timeline_total_duration_is_derived_from_entry_end_times():
    assert Timeline(episode_id="EP1").total_duration_sec == 0.0
    tl = Timeline(episode_id="EP1", entries=[
        TimelineEntry(type="shot", ref="S1", start_sec=0, duration_sec=10),
        TimelineEntry(type="pause", start_sec=10, duration_sec=2),
    ])
    assert tl.total_duration_sec == 12.0


def test_timeline_total_reflects_a_late_starting_entry():
    tl = Timeline(episode_id="EP1", entries=[
        TimelineEntry(type="shot", ref="S1", start_sec=0, duration_sec=10),
        TimelineEntry(type="card", start_sec=30, duration_sec=3),
    ])
    assert tl.total_duration_sec == 33.0
