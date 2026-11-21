"""Approval integrity contract tests (R1-R5, R7-R9 and R6's assemble_episode revocation).

Authored from ``orchestrator/service/APPROVAL_INTEGRITY_CONTRACT.md`` alone. R6's store
behaviour lives in ``tests/store/test_timeline_history_contract.py``.

Fixtures that are not about Review semantics deliberately use *legacy* (unscoped) Review
records: the contract guarantees every existing Review file still loads, and a legacy
record is enough to carry ``approval_review_id`` on a shot that is merely "approved".
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orchestrator.api.app import create_app
from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Location,
    PostProfile,
    Project,
    Review,
    ReviewTargetKind,
    RubricScores,
    Shot,
    ShotOutput,
    ShotOutputKind,
    StyleBible,
    Timeline,
    TimelineEntry,
)
from orchestrator.domain.state import ReviewApprovalError, check_review_approval
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import ApprovalSettings, Settings
from orchestrator.service.approval import (
    approve_episode,
    approve_framing,
    approve_shot,
    approve_style_bible,
    record_review,
)
from orchestrator.service.dependencies import (
    propose_shots,
    revise_camera_profile,
    revise_location,
)
import orchestrator.service.approval as approval_module
from orchestrator.service.minimax_h3 import promote_h3_candidate
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict
from tests.adapters.export.test_portable_package import _offline_media
from tests.adapters.export.test_portable_package import _setup as _export_setup
from tests.service.test_h3_promotion import RUN as H3_RUN
from tests.service.test_h3_promotion import _setup as _h3_setup
from tests.service.test_timeline_assembly_contract import _assemble, _fixture as _assembly_fixture

_NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# fixtures and builders
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(ProjectRoot(tmp_path))


def scoped(
    review_id: str = "r1", *, kind: ReviewTargetKind = "shot", target: str = "s1",
    version: int = 1, episode: str | None = "ep1", actor: str = "creator",
    state: ShotStatus = ShotStatus.APPROVED,
) -> Review:
    return Review(
        review_id=review_id, target_kind=kind, target_id=target, target_version=version,
        episode_id=episode, actor=actor, created_at=_NOW, approval_state=state,
    )


def legacy(review_id: str = "r1", *, target: str = "s1", version: int = 1) -> Review:
    return Review(
        review_id=review_id, target_id=target, target_version=version,
        created_at=_NOW, approval_state=ShotStatus.APPROVED,
    )


def add_episode(store: ProjectStore, episode_id: str = "ep1", *, approved_cut_version=None,
                approval_review_id=None) -> int:
    """Create an Episode; approval fields cannot be set on creation, so they follow it."""
    created = store.save(
        Episode(episode_id=episode_id, project_id="p1", number=1, duration_target_sec=30),
        expected_version=0,
    )
    if approved_cut_version is None and approval_review_id is None:
        return created
    episode = store.load(Episode, episode_id)
    return store.save(
        episode.model_copy(update={"approved_cut_version": approved_cut_version,
                                   "approval_review_id": approval_review_id}),
        expected_version=created,
    )


def token(store: ProjectStore, shot_id: str, episode_id: str = "ep1") -> int:
    return store.load_with_version(Shot, shot_id, episode_id=episode_id)[1]


def shot_in_review(store: ProjectStore, shot_id: str = "s1", episode_id: str = "ep1",
                   **fields) -> Shot:
    shot = Shot(shot_id=shot_id, episode_id=episode_id, duration_sec=4, **fields)
    store.create(shot)
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        update: dict[str, object] = {"status": status}
        if status is ShotStatus.BUILDING:
            update["framing_status"] = FramingStatus.APPROVED
        shot = shot.model_copy(update=update)
        store.update(shot, expected_version=token(store, shot_id, episode_id))
    return shot


def approve(store: ProjectStore, review_id: str = "r1", shot_id: str = "s1",
            episode_id: str = "ep1", **kwargs) -> Shot:
    return approve_shot(store, shot_id, episode_id=episode_id, review_id=review_id,
                        expected_version=token(store, shot_id, episode_id), **kwargs)


def approved_shot(store: ProjectStore, shot_id: str, *, episode_id: str = "ep1",
                  dependencies: tuple[str, ...] = (), camera_id: str = "cam1") -> None:
    """An approved shot, built without exercising Review semantics (legacy Review)."""
    shot_in_review(store, shot_id, episode_id, dependencies=list(dependencies),
                   camera_id=camera_id)
    review_id = f"review-{episode_id}-{shot_id}"
    store.save(legacy(review_id, target=shot_id), expected_version=0)
    shot = store.load(Shot, shot_id, episode_id=episode_id)
    store.update(
        shot.model_copy(update={"status": ShotStatus.APPROVED, "approval_review_id": review_id}),
        expected_version=token(store, shot_id, episode_id),
    )


def approve_cut_directly(store: ProjectStore, episode_id: str = "ep1") -> None:
    episode, tok = store.load_with_version(Episode, episode_id)
    store.save(
        episode.model_copy(update={"approved_cut_version": 1, "approval_review_id": "gate"}),
        expected_version=tok,
    )


def assert_cut_revoked(store: ProjectStore, episode_id: str = "ep1") -> None:
    episode = store.load(Episode, episode_id)
    assert episode.approved_cut_version is None
    assert episode.approval_review_id is None


# --------------------------------------------------------------------------
# R1 - Review model validation
# --------------------------------------------------------------------------


def test_r1_review_target_kind_is_exported_with_the_six_kinds() -> None:
    kinds = set(ReviewTargetKind.__args__)  # type: ignore[attr-defined]
    assert kinds == {"shot", "framing", "episode_cut", "style_bible", "h3_candidate", "location"}


@pytest.mark.parametrize(("kind", "target", "episode"), [
    ("shot", "s1", "ep1"), ("framing", "s1", "ep1"), ("episode_cut", "ep1", "ep1"),
    ("h3_candidate", "cand", "ep1"), ("style_bible", "p1", None), ("location", "loc", None),
])
def test_r1_valid_scoped_records_are_accepted(kind, target, episode) -> None:
    review = scoped(kind=kind, target=target, episode=episode)
    assert (review.target_kind, review.episode_id, review.actor) == (kind, episode, "creator")


def test_r1_legacy_record_has_all_three_new_fields_none() -> None:
    review = legacy()
    assert (review.target_kind, review.episode_id, review.actor) == (None, None, None)


def test_r1_every_existing_review_file_still_loads(store: ProjectStore) -> None:
    store.save(legacy("old"), expected_version=0)
    path = store._root.resolve("reviews/old.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for name in ("target_kind", "episode_id", "actor"):
        payload.pop(name, None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = store.load(Review, "old")
    assert loaded.target_kind is None and loaded.actor is None and loaded.episode_id is None


def _base(**extra) -> dict:
    return {"review_id": "r", "target_id": "s1", "target_version": 1, "created_at": _NOW,
            "approval_state": ShotStatus.APPROVED, **extra}


@pytest.mark.parametrize("extra", [
    {"target_kind": "shot", "episode_id": "ep1"},                   # kind without actor
    {"actor": "creator"},                                            # actor without kind
    {"episode_id": "ep1"},                                           # episode on a legacy record
    {"target_kind": "shot", "actor": "creator"},                     # shot needs episode
    {"target_kind": "framing", "actor": "creator"},
    {"target_kind": "h3_candidate", "actor": "creator"},
    {"target_kind": "episode_cut", "actor": "creator"},
    {"target_kind": "episode_cut", "actor": "creator", "episode_id": "ep2"},  # != target_id
    {"target_kind": "style_bible", "actor": "creator", "episode_id": "ep1"},
    {"target_kind": "location", "actor": "creator", "episode_id": "ep1"},
    {"target_kind": "shot", "actor": "robot", "episode_id": "ep1"},
    {"target_kind": "nonsense", "actor": "creator", "episode_id": "ep1"},
    {"target_kind": "shot", "actor": "creator", "episode_id": ""},   # min_length=1
])
def test_r1_invalid_combinations_are_rejected(extra) -> None:
    with pytest.raises(ValidationError):
        Review(**_base(**extra))


def test_r1_episode_cut_episode_equals_target_id_is_accepted() -> None:
    review = Review(**_base(target_id="ep7", target_kind="episode_cut", actor="agent",
                            episode_id="ep7"))
    assert review.actor == "agent"


def test_r1_approval_state_validator_is_unchanged() -> None:
    with pytest.raises(ValidationError):
        scoped(state=ShotStatus.REVIEW)
    assert scoped(state=ShotStatus.REJECTED).approval_state is ShotStatus.REJECTED


# --------------------------------------------------------------------------
# R2 - the pure Review check
# --------------------------------------------------------------------------


def _check(review: Review, **overrides) -> None:
    args = dict(target_kind="shot", target_id="s1", target_version=1, episode_id="ep1",
                agent_may_approve=False)
    check_review_approval(review, **{**args, **overrides})


def test_r2_matching_creator_review_passes() -> None:
    _check(scoped())


def test_r2_legacy_review_never_approves_even_if_ids_match() -> None:
    with pytest.raises(ReviewApprovalError, match="legacy") as error:
        _check(legacy("old-1"))
    assert "old-1" in str(error.value)
    with pytest.raises(ReviewApprovalError, match="legacy"):
        _check(legacy("old-1"), agent_may_approve=True)


@pytest.mark.parametrize("override", [
    {"target_kind": "framing"}, {"target_id": "s2"}, {"target_version": 2},
    {"episode_id": "ep2"}, {"episode_id": None},
])
def test_r2_each_scope_mismatch_is_refused_and_names_the_review(override) -> None:
    with pytest.raises(ReviewApprovalError) as error:
        _check(scoped("rev-x"), **override)
    assert "rev-x" in str(error.value)


def test_r2_rejected_review_is_refused() -> None:
    with pytest.raises(ReviewApprovalError, match="rev-x"):
        _check(scoped("rev-x", state=ShotStatus.REJECTED))


def test_r2_agent_review_is_an_opinion_unless_the_setting_is_on() -> None:
    with pytest.raises(ReviewApprovalError) as error:
        _check(scoped("rev-a", actor="agent"))
    message = str(error.value)
    assert "approvals.agent_may_approve" in message
    assert "opinion" in message.lower()
    assert "rev-a" in message
    _check(scoped("rev-a", actor="agent"), agent_may_approve=True)


def test_r2_checks_run_in_order_legacy_then_mismatch_then_state_then_agent() -> None:
    # A mismatching agent review reports the mismatch, not the agent rule.
    with pytest.raises(ReviewApprovalError) as mismatch:
        _check(scoped(actor="agent"), target_id="other")
    assert "agent_may_approve" not in str(mismatch.value)
    # A rejected agent review reports the state, not the agent rule.
    with pytest.raises(ReviewApprovalError) as rejected:
        _check(scoped(actor="agent", state=ShotStatus.REJECTED))
    assert "agent_may_approve" not in str(rejected.value)


# --- R2 through the service callers ----------------------------------------


def test_r2_approve_shot_accepts_scoped_creator_review(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    store.save(scoped(), expected_version=0)
    result = approve(store)
    assert result.status is ShotStatus.APPROVED and result.approval_review_id == "r1"


def test_r2_approve_shot_refuses_legacy_review(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    store.save(legacy(), expected_version=0)
    with pytest.raises(JobError) as error:
        approve(store)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW


@pytest.mark.parametrize("make_review", [
    lambda: scoped(kind="framing"),                 # kind mismatch
    lambda: scoped(target="other"),                 # id mismatch
    lambda: scoped(version=2),                      # version mismatch
    lambda: scoped(episode="ep2"),                  # episode mismatch
    lambda: scoped(state=ShotStatus.REJECTED),
])
def test_r2_approve_shot_refuses_each_mismatch(store: ProjectStore, make_review) -> None:
    add_episode(store)
    shot_in_review(store)
    store.save(make_review(), expected_version=0)
    with pytest.raises(JobError) as error:
        approve(store)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW


def test_r2_review_for_one_episodes_shot_does_not_approve_the_same_shot_id_elsewhere(
    store: ProjectStore,
) -> None:
    add_episode(store, "ep1")
    add_episode(store, "ep2")
    shot_in_review(store, "S1", "ep1")
    shot_in_review(store, "S1", "ep2")
    store.save(scoped(target="S1", episode="ep1"), expected_version=0)

    with pytest.raises(JobError) as error:
        approve(store, shot_id="S1", episode_id="ep2")
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Shot, "S1", episode_id="ep2").status is ShotStatus.REVIEW
    assert approve(store, shot_id="S1", episode_id="ep1").status is ShotStatus.APPROVED


def test_r2_agent_review_approves_nothing_by_default_and_names_the_setting(
    store: ProjectStore,
) -> None:
    add_episode(store)
    shot_in_review(store)
    store.save(scoped(actor="agent"), expected_version=0)
    with pytest.raises(JobError) as error:
        approve(store)
    assert "approvals.agent_may_approve" in str(error.value)
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW


def test_r2_agent_review_approves_when_the_caller_passes_the_setting(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    store.save(scoped(actor="agent"), expected_version=0)
    assert approve(store, agent_may_approve=True).status is ShotStatus.APPROVED


def test_r2_approve_episode_binds_episode_cut_kind_id_version_and_episode(
    store: ProjectStore,
) -> None:
    add_episode(store)
    store.save(scoped("gate", kind="episode_cut", target="ep1", version=3, episode="ep1"),
               expected_version=0)
    result = approve_episode(store, "ep1", cut_version=3, review_id="gate", expected_version=1)
    assert (result.approved_cut_version, result.approval_review_id) == (3, "gate")


@pytest.mark.parametrize("make_review", [
    lambda: legacy("gate", target="ep1", version=3),
    lambda: scoped("gate", kind="shot", target="ep1", version=3, episode="ep1"),   # id collision
    lambda: scoped("gate", kind="episode_cut", target="ep1", version=2, episode="ep1"),
    lambda: scoped("gate", kind="episode_cut", target="ep1", version=3, episode="ep1",
           actor="agent"),
])
def test_r2_approve_episode_refuses_wrong_or_legacy_or_agent_reviews(
    store: ProjectStore, make_review,
) -> None:
    add_episode(store)
    store.save(make_review(), expected_version=0)
    with pytest.raises(JobError) as error:
        approve_episode(store, "ep1", cut_version=3, review_id="gate", expected_version=1)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Episode, "ep1").approved_cut_version is None


def test_r2_approve_episode_agent_review_accepted_with_setting(store: ProjectStore) -> None:
    add_episode(store)
    store.save(scoped("gate", kind="episode_cut", target="ep1", version=3, episode="ep1",
                      actor="agent"), expected_version=0)
    result = approve_episode(store, "ep1", cut_version=3, review_id="gate",
                             expected_version=1, agent_may_approve=True)
    assert result.approval_review_id == "gate"


def test_r2_approve_style_bible_binds_kind_id_and_version(store: ProjectStore) -> None:
    store.create(StyleBible(project_id="p1"))
    store.save(scoped("style", kind="style_bible", target="p1", episode=None), expected_version=0)
    assert approve_style_bible(store, "p1", review_id="style",
                               expected_version=1).approval_review_id == "style"


@pytest.mark.parametrize("make_review", [
    lambda: legacy("style", target="p1"),
    lambda: scoped("style", kind="location", target="p1", episode=None),
    lambda: scoped("style", kind="style_bible", target="other", episode=None),
    lambda: scoped("style", kind="style_bible", target="p1", version=2, episode=None),
    lambda: scoped("style", kind="style_bible", target="p1", episode=None, actor="agent"),
])
def test_r2_approve_style_bible_refuses_mismatched_legacy_or_agent_reviews(
    store: ProjectStore, make_review,
) -> None:
    store.create(StyleBible(project_id="p1"))
    store.save(make_review(), expected_version=0)
    with pytest.raises(JobError):
        approve_style_bible(store, "p1", review_id="style", expected_version=1)
    assert store.load(StyleBible, "p1").approval_review_id is None


def test_r2_approve_style_bible_agent_accepted_with_setting(store: ProjectStore) -> None:
    store.create(StyleBible(project_id="p1"))
    store.save(scoped("style", kind="style_bible", target="p1", episode=None, actor="agent"),
               expected_version=0)
    result = approve_style_bible(store, "p1", review_id="style", expected_version=1,
                                 agent_may_approve=True)
    assert result.approval_review_id == "style"


# --- R2: H3 promotion ----------------------------------------------------------


def _h3_review(review_id: str = "h3r", **overrides) -> Review:
    fields = dict(kind="h3_candidate", target=H3_RUN, episode="ep1")
    return scoped(review_id, **{**fields, **overrides})


def test_r2_h3_promotion_accepts_a_scoped_candidate_review(tmp_path: Path) -> None:
    store, _root, _candidate = _h3_setup(tmp_path)
    store.save(_h3_review(), expected_version=0)
    output = promote_h3_candidate(store, candidate_id=H3_RUN, review_id="h3r",
                                  expected_version=0)
    assert output.kind is ShotOutputKind.GENERATED


@pytest.mark.parametrize("make_review", [
    lambda: legacy("h3r", target=H3_RUN),
    lambda: _h3_review(kind="shot"),
    lambda: _h3_review(episode="ep2"),
    lambda: _h3_review(version=2),
    lambda: _h3_review(actor="agent"),
])
def test_r2_h3_promotion_refuses_legacy_mismatched_and_agent_reviews(
    tmp_path: Path, make_review,
) -> None:
    store, _root, _candidate = _h3_setup(tmp_path)
    store.save(make_review(), expected_version=0)
    with pytest.raises(JobError):
        promote_h3_candidate(store, candidate_id=H3_RUN, review_id="h3r", expected_version=0)
    assert not [o for o in store.list_shot_outputs("s1", episode_id="ep1")
                if o.kind is ShotOutputKind.GENERATED]


def test_r2_h3_promotion_agent_review_accepted_with_keyword(tmp_path: Path) -> None:
    store, _root, _candidate = _h3_setup(tmp_path)
    store.save(_h3_review(actor="agent"), expected_version=0)
    output = promote_h3_candidate(store, candidate_id=H3_RUN, review_id="h3r",
                                  expected_version=0, agent_may_approve=True)
    assert output.kind is ShotOutputKind.GENERATED


# --- R2: export re-checks with the current setting --------------------------------


def _reapprove_cut_with(fixture, review: Review) -> None:
    fixture.store.save(review, expected_version=0)
    episode, tok = fixture.store.load_with_version(Episode, "ep1")
    fixture.store.save(episode.model_copy(update={"approval_review_id": review.review_id}),
                       expected_version=tok)
    fixture.episode_token = tok + 1


def _cut_review(review_id: str, fixture, **overrides) -> Review:
    episode = fixture.store.load(Episode, "ep1")
    fields = dict(kind="episode_cut", target="ep1", episode="ep1",
                  version=episode.approved_cut_version)
    return scoped(review_id, **{**fields, **overrides})


def test_r2_export_refuses_a_legacy_cut_review(tmp_path: Path) -> None:
    fixture = _export_setup(tmp_path, b"registered local cut fixture")
    episode = fixture.store.load(Episode, "ep1")
    _reapprove_cut_with(fixture, legacy("old-gate", target="ep1",
                                        version=episode.approved_cut_version))
    response = fixture.export()
    assert response.status_code in {400, 409, 422}, response.text
    assert not list(fixture.root.path.glob("exports/*/export.json"))


def test_r2_export_refuses_an_agent_cut_review_while_the_setting_is_off(tmp_path: Path) -> None:
    fixture = _export_setup(tmp_path, b"registered local cut fixture")
    _reapprove_cut_with(fixture, _cut_review("agent-gate", fixture, actor="agent"))
    response = fixture.export()
    assert response.status_code in {400, 409, 422}, response.text
    assert "agent_may_approve" in response.text
    assert not list(fixture.root.path.glob("exports/*/export.json"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="export encodes with ffmpeg")
def test_r2_export_accepts_an_agent_cut_review_only_while_the_setting_is_on(
    tmp_path: Path,
) -> None:
    cut = _offline_media(ProjectRoot(tmp_path), "mixed-cut.mp4", audio=True)
    fixture = _export_setup(tmp_path, cut)
    _reapprove_cut_with(fixture, _cut_review("agent-gate", fixture, actor="agent"))

    refused = fixture.export()
    assert refused.status_code in {400, 409, 422}, refused.text

    fixture.settings = Settings(project_root=tmp_path,
                                approvals=ApprovalSettings(agent_may_approve=True))
    accepted = fixture.export()
    assert accepted.status_code == 200, accepted.text


# --------------------------------------------------------------------------
# R3 - record_review
# --------------------------------------------------------------------------


def _record(store: ProjectStore, **overrides) -> Review:
    fields = dict(target_kind="shot", target_id="s1", target_version=1, episode_id="ep1",
                  actor="creator", approval_state=ShotStatus.APPROVED)
    return record_review(store, **{**fields, **overrides})


def test_r3_record_review_returns_and_persists_a_scoped_review(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    review = _record(store, comment="looks right", review_id="mine", now=_NOW)
    stored = store.load(Review, "mine")
    assert stored == review
    assert (stored.target_kind, stored.target_id, stored.target_version, stored.episode_id,
            stored.actor, stored.comment) == ("shot", "s1", 1, "ep1", "creator", "looks right")
    assert stored.created_at == _NOW


def test_r3_defaults_are_a_uuid4_hex_id_and_a_utc_timestamp(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    before = datetime.now(UTC)
    review = _record(store)
    after = datetime.now(UTC)
    assert re.fullmatch(r"[0-9a-f]{32}", review.review_id)
    assert review.created_at.tzinfo is not None
    assert before <= review.created_at <= after
    assert _record(store).review_id != review.review_id


def test_r3_passes_rubric_scores_and_waiver_text_through(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    scores = [
        RubricScores(mount_inferable=True, plausible_place=True, no_unmotivated_movement=True,
                     stable_light_and_geometry=False, integrated_anomaly=True),
        RubricScores(mount_inferable=False, plausible_place=True, no_unmotivated_movement=False,
                     stable_light_and_geometry=True, integrated_anomaly=True),
    ]
    review = _record(store, waiver_text="accepted despite flicker", review_id="w",
                     rubric_scores=scores)
    stored = store.load(Review, "w")
    assert stored.waiver_text == "accepted despite flicker"
    assert stored.rubric_scores == scores
    assert review.rubric_scores == scores


def test_r3_agent_actor_is_recorded_even_while_agents_may_not_approve(
    store: ProjectStore,
) -> None:
    add_episode(store)
    shot_in_review(store)
    assert _record(store, actor="agent").actor == "agent"


def test_r3_recording_never_changes_approval_state(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    before_shot, before_token = store.load_with_version(Shot, "s1", episode_id="ep1")
    before_episode = store.load_with_version(Episode, "ep1")
    _record(store)
    assert store.load_with_version(Shot, "s1", episode_id="ep1") == (before_shot, before_token)
    assert store.load_with_version(Episode, "ep1") == before_episode


def test_r3_a_recorded_review_then_drives_approval(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    review = _record(store)
    assert approve(store, review.review_id).approval_review_id == review.review_id


def test_r3_duplicate_review_id_is_a_version_conflict_and_keeps_the_original(
    store: ProjectStore,
) -> None:
    add_episode(store)
    shot_in_review(store)
    _record(store, review_id="dup", comment="first")
    with pytest.raises(VersionConflict):
        _record(store, review_id="dup", comment="second")
    assert store.load(Review, "dup").comment == "first"


@pytest.mark.parametrize("kind", ["shot", "framing"])
@pytest.mark.parametrize("override", [
    {"target_id": "missing"}, {"target_version": 2}, {"episode_id": "ep2"},
])
def test_r3_shot_and_framing_targets_must_exist_at_that_version_and_episode(
    store: ProjectStore, kind: str, override: dict,
) -> None:
    add_episode(store, "ep1")
    add_episode(store, "ep2")
    shot_in_review(store)
    with pytest.raises(JobError) as error:
        _record(store, target_kind=kind, review_id="x", **override)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    with pytest.raises(RecordNotFound):
        store.load(Review, "x")


@pytest.mark.parametrize("kind", ["shot", "framing"])
def test_r3_shot_and_framing_targets_exist_succeeds(store: ProjectStore, kind: str) -> None:
    add_episode(store)
    shot_in_review(store)
    assert _record(store, target_kind=kind).target_kind == kind


def test_r3_missing_target_error_names_the_missing_target(store: ProjectStore) -> None:
    add_episode(store)
    with pytest.raises(JobError) as error:
        _record(store, target_id="ghost-shot")
    assert "ghost-shot" in str(error.value)


def _timeline(duration: float = 3.0) -> Timeline:
    return Timeline(episode_id="ep1", entries=[
        TimelineEntry(type="card", start_sec=0, duration_sec=duration)])


def _cut(store: ProjectStore, **overrides) -> Review:
    return _record(store, target_kind="episode_cut", target_id="ep1", episode_id="ep1",
                   target_version=overrides.pop("target_version", 1), **overrides)


def test_r3_episode_cut_needs_a_timeline_at_that_store_version(store: ProjectStore) -> None:
    add_episode(store)
    with pytest.raises(JobError) as error:
        _cut(store)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    store.save(_timeline(), expected_version=0)
    assert _cut(store).target_version == 1
    with pytest.raises(JobError):
        _cut(store, target_version=2)


def test_r3_episode_cut_review_can_name_a_superseded_timeline_snapshot(
    store: ProjectStore,
) -> None:
    add_episode(store)
    store.save(_timeline(3.0), expected_version=0)
    store.save(_timeline(4.0), expected_version=1)
    assert _cut(store, target_version=1).target_version == 1
    assert _cut(store, target_version=2).target_version == 2


def test_r3_style_bible_target_must_exist_at_that_content_version(store: ProjectStore) -> None:
    kwargs = dict(target_kind="style_bible", target_id="p1", episode_id=None)
    with pytest.raises(JobError):
        _record(store, target_version=1, **kwargs)
    store.create(StyleBible(project_id="p1"))
    assert _record(store, target_version=1, **kwargs).target_kind == "style_bible"
    with pytest.raises(JobError):
        _record(store, target_version=2, **kwargs)
    with pytest.raises(JobError):
        _record(store, target_version=1, **{**kwargs, "target_id": "other"})


def test_r3_location_target_must_exist_at_that_content_version(store: ProjectStore) -> None:
    kwargs = dict(target_kind="location", target_id="loc1", episode_id=None)
    with pytest.raises(JobError):
        _record(store, target_version=1, **kwargs)
    store.create(Location(location_id="loc1"))
    assert _record(store, target_version=1, **kwargs).target_kind == "location"
    with pytest.raises(JobError):
        _record(store, target_version=2, **kwargs)


def test_r3_h3_candidate_target_checks_existence_episode_and_shot_version(
    tmp_path: Path,
) -> None:
    store, _root, candidate = _h3_setup(tmp_path)
    kwargs = dict(target_kind="h3_candidate", target_id=H3_RUN, episode_id="ep1",
                  target_version=candidate.shot_version)
    assert _record(store, **kwargs).target_kind == "h3_candidate"
    with pytest.raises(JobError):
        _record(store, **{**kwargs, "target_id": "no-such-candidate"})
    with pytest.raises(JobError):
        _record(store, **{**kwargs, "episode_id": "ep2"})
    with pytest.raises(JobError):
        _record(store, **{**kwargs, "target_version": candidate.shot_version + 1})


def test_r3_scope_violations_are_refused_by_review_validation(store: ProjectStore) -> None:
    add_episode(store)
    shot_in_review(store)
    with pytest.raises((ValidationError, JobError)):
        _record(store, episode_id=None)          # a shot Review needs its episode
    with pytest.raises((ValidationError, JobError)):
        _record(store, target_kind="style_bible", target_id="p1", episode_id="ep1")


# --------------------------------------------------------------------------
# R4 - Review immutability
# --------------------------------------------------------------------------


def test_r4_resaving_a_review_with_a_matching_token_is_refused(store: ProjectStore) -> None:
    token_after_create = store.save(scoped(), expected_version=0)
    with pytest.raises(JobError) as error:
        store.save(scoped().model_copy(update={"comment": "rewritten"}),
                   expected_version=token_after_create)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "immutable" in str(error.value).lower()
    assert store.load(Review, "r1").comment == ""


@pytest.mark.parametrize("expected", [1, 2, 99])
def test_r4_any_nonzero_expected_version_is_refused_as_immutable(
    store: ProjectStore, expected: int,
) -> None:
    store.save(scoped(), expected_version=0)
    before = store._root.read_bytes("reviews/r1.json")
    with pytest.raises(JobError) as error:
        store.save(scoped().model_copy(update={"comment": "x"}), expected_version=expected)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "immutable" in str(error.value).lower()
    assert store._root.read_bytes("reviews/r1.json") == before


def test_r4_nonzero_expected_version_is_refused_even_for_a_new_review_id(
    store: ProjectStore,
) -> None:
    with pytest.raises(JobError) as error:
        store.save(scoped("fresh"), expected_version=1)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "immutable" in str(error.value).lower()
    with pytest.raises(RecordNotFound):
        store.load(Review, "fresh")


def _post_profile(**fields) -> PostProfile:
    return PostProfile(name="dvr", semver="1.0.0", **fields)


def test_r4_post_profile_resave_with_nonzero_expected_version_is_refused_as_immutable(
    store: ProjectStore,
) -> None:
    token_after_create = store.save(_post_profile(), expected_version=0)
    path = "post_profiles/dvr.json"
    before = store._root.read_bytes(path)
    with pytest.raises(JobError) as error:
        store.save(_post_profile(provenance="rewritten"), expected_version=token_after_create)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "immutable" in str(error.value).lower()
    assert store._root.read_bytes(path) == before


def test_r4_creating_a_second_review_with_the_same_id_is_a_version_conflict(
    store: ProjectStore,
) -> None:
    store.save(scoped(), expected_version=0)
    with pytest.raises(VersionConflict):
        store.save(scoped(), expected_version=0)


# --------------------------------------------------------------------------
# R5 - framing approval needs a Review
# --------------------------------------------------------------------------


def _framing_fixture(store: ProjectStore, shot_id: str = "s1", episode_id: str = "ep1") -> int:
    """A BLOCKING shot with pending framing, a matching preview and its CameraProfile."""
    import hashlib

    add_episode(store, episode_id)
    if not store.versions(CameraProfile, "cam1"):
        store.create(CameraProfile(camera_id="cam1", profile_name="CCTV"))
    panel = f"previews/{episode_id}/{shot_id}/panel.png"
    data = f"panel for {episode_id}/{shot_id}".encode()
    store._root.write_bytes(panel, data)
    shot = Shot(shot_id=shot_id, episode_id=episode_id, duration_sec=4, camera_profile="cam1",
                framing_panel_ref=panel, framing_status=FramingStatus.PENDING)
    store.create(shot)
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING):
        shot = shot.model_copy(update={"status": status})
        store.update(shot, expected_version=token(store, shot_id, episode_id))
    store.append_shot_output(ShotOutput(
        output_id=f"preview-{episode_id}-{shot_id}", episode_id=episode_id, shot_id=shot_id,
        record_version=1, kind=ShotOutputKind.PREVIEW, relative_path=panel,
        content_sha256=hashlib.sha256(data).hexdigest(), camera_profile_id="cam1",
        camera_profile_version=1, engine_id="BLENDER_EEVEE", engine_build="fixture-build",
    ), expected_version=0)
    return token(store, shot_id, episode_id)


def _approve_framing(store: ProjectStore, review_id: str, **kwargs) -> Shot:
    return approve_framing(store, "s1", episode_id="ep1", review_id=review_id,
                           expected_version=token(store, "s1"), **kwargs)


def test_r5_approve_framing_with_a_framing_review_pins_framing(store: ProjectStore) -> None:
    _framing_fixture(store)
    store.save(scoped("f1", kind="framing"), expected_version=0)
    result = _approve_framing(store, "f1")
    assert result.framing_status is FramingStatus.APPROVED
    assert store.load(Shot, "s1", episode_id="ep1").framing_status is FramingStatus.APPROVED


def test_r5_approve_framing_without_a_review_is_refused(store: ProjectStore) -> None:
    _framing_fixture(store)
    with pytest.raises(JobError) as error:
        _approve_framing(store, "no-such-review")
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert store.load(Shot, "s1", episode_id="ep1").framing_status is FramingStatus.PENDING


@pytest.mark.parametrize("make_review", [
    lambda: legacy("f1"),
    lambda: scoped("f1", kind="shot"),                          # a shot Review is not a framing Review
    lambda: scoped("f1", kind="framing", target="other"),
    lambda: scoped("f1", kind="framing", version=2),
    lambda: scoped("f1", kind="framing", episode="ep2"),
    lambda: scoped("f1", kind="framing", state=ShotStatus.REJECTED),
    lambda: scoped("f1", kind="framing", actor="agent"),
])
def test_r5_approve_framing_refuses_every_non_matching_review(
    store: ProjectStore, make_review,
) -> None:
    _framing_fixture(store)
    store.save(make_review(), expected_version=0)
    with pytest.raises(JobError):
        _approve_framing(store, "f1")
    assert store.load(Shot, "s1", episode_id="ep1").framing_status is FramingStatus.PENDING


def test_r5_agent_framing_review_accepted_with_the_setting(store: ProjectStore) -> None:
    _framing_fixture(store)
    store.save(scoped("f1", kind="framing", actor="agent"), expected_version=0)
    result = _approve_framing(store, "f1", agent_may_approve=True)
    assert result.framing_status is FramingStatus.APPROVED


def test_r5_framing_review_for_another_episodes_same_shot_id_is_refused(
    store: ProjectStore,
) -> None:
    _framing_fixture(store, "s1", "ep1")
    _framing_fixture(store, "s1", "ep2")
    store.save(scoped("f1", kind="framing", episode="ep2"), expected_version=0)
    with pytest.raises(JobError):
        _approve_framing(store, "f1")
    assert store.load(Shot, "s1", episode_id="ep1").framing_status is FramingStatus.PENDING


def test_r5_idempotent_return_for_already_approved_framing_needs_no_review(
    store: ProjectStore,
) -> None:
    _framing_fixture(store)
    store.save(scoped("f1", kind="framing"), expected_version=0)
    first = _approve_framing(store, "f1")
    again = approve_framing(store, "s1", episode_id="ep1", review_id="not-a-review",
                            expected_version=token(store, "s1"))
    assert again == first


# --- R5 / R2: the second-phase recheck ----------------------------------------------------


def tree(store: ProjectStore) -> dict[str, bytes]:
    base = store._root.path
    return {path.relative_to(base).as_posix(): path.read_bytes()
            for path in base.rglob("*") if path.is_file()}


def pass_once_then_raise(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The Review check passes on its first call and raises on every later one."""
    calls: list[int] = []
    original = approval_module.check_review_approval

    def flaky(review, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise ReviewApprovalError("Review changed between check and commit")
        return original(review, **kwargs)

    monkeypatch.setattr(approval_module, "check_review_approval", flaky)
    return calls


def test_r5_approve_framing_refuses_when_only_the_second_phase_check_fails(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _framing_fixture(store)
    store.save(scoped("f1", kind="framing"), expected_version=0)
    before = tree(store)
    calls = pass_once_then_raise(monkeypatch)

    with pytest.raises(JobError):
        _approve_framing(store, "f1")

    assert len(calls) >= 2, "the Review must be checked again immediately before commit"
    assert tree(store) == before
    assert store.load(Shot, "s1", episode_id="ep1").framing_status is FramingStatus.PENDING


def test_r2_approve_shot_refuses_when_only_the_second_phase_check_fails(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_episode(store)
    shot_in_review(store)
    store.save(scoped(), expected_version=0)
    before = tree(store)
    calls = pass_once_then_raise(monkeypatch)

    with pytest.raises(JobError):
        approve(store)

    assert len(calls) >= 2
    assert tree(store) == before
    assert store.load(Shot, "s1", episode_id="ep1").status is ShotStatus.REVIEW


# --- R2: H3 promotion through the HTTP route -------------------------------------------------


def _promote_over_http(tmp_path: Path, *, agent_may_approve: bool):
    project = tmp_path / "p1"
    project.mkdir()
    store, _root, _candidate = _h3_setup(project)
    store.save(_h3_review("agent-h3", actor="agent"), expected_version=0)
    settings = Settings(project_root=tmp_path,
                        approvals=ApprovalSettings(agent_may_approve=agent_may_approve))
    app = create_app(settings=settings, token=_TOKEN)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8765",
                        raise_server_exceptions=False) as client:
            response = client.post(
                f"/projects/p1/episodes/ep1/shots/s1/h3-candidates/{H3_RUN}/promote",
                headers=_HEADERS, json={"review_id": "agent-h3", "expected_version": 0},
            )
    finally:
        app.state.context.job_runner.close()
    return store, response


def test_r2_http_h3_promotion_refuses_an_agent_review_while_the_setting_is_off(
    tmp_path: Path,
) -> None:
    store, response = _promote_over_http(tmp_path, agent_may_approve=False)
    assert response.status_code in {400, 409, 422}, response.text
    assert "agent_may_approve" in response.text
    assert not [o for o in store.list_shot_outputs("s1", episode_id="ep1")
                if o.kind is ShotOutputKind.GENERATED]


def test_r2_http_h3_promotion_accepts_an_agent_review_while_the_setting_is_on(
    tmp_path: Path,
) -> None:
    store, response = _promote_over_http(tmp_path, agent_may_approve=True)
    assert response.status_code == 200, response.text
    assert [o for o in store.list_shot_outputs("s1", episode_id="ep1")
            if o.kind is ShotOutputKind.GENERATED]


# --------------------------------------------------------------------------
# R6 - assemble_episode revokes the approved cut when the Timeline changed
# --------------------------------------------------------------------------


def swap_first_two_shots(cut: Timeline) -> Timeline:
    """Same slots and durations, different shot order: a changed cut that stays well-formed."""
    entries = [entry.model_copy() for entry in cut.entries]
    entries[0].ref, entries[1].ref = entries[1].ref, entries[0].ref
    return cut.model_copy(update={"entries": entries})


def test_r6_assembling_a_changed_timeline_clears_the_cut_approval(tmp_path: Path) -> None:
    fixture = _assembly_fixture(tmp_path)
    store = fixture["store"]
    _assemble(fixture)
    first_head, first_token = store.load_with_version(Timeline, "episode-1")
    approve_cut_directly(store, "episode-1")

    changed = swap_first_two_shots(first_head)
    from orchestrator.service.timeline import assemble_episode

    assemble_episode(store, fixture["encoder"], episode_id="episode-1", timeline=changed)

    head, new_token = store.load_with_version(Timeline, "episode-1")
    assert head.content_sha256 != first_head.content_sha256
    assert new_token > first_token
    assert_cut_revoked(store, "episode-1")
    # the earlier cut's source edit is still recoverable
    assert store.load_timeline_at("episode-1", first_token).content_sha256 == (
        first_head.content_sha256)


def test_r6_reassembling_an_unchanged_timeline_keeps_the_cut_approval(tmp_path: Path) -> None:
    fixture = _assembly_fixture(tmp_path)
    store = fixture["store"]
    _assemble(fixture)
    approve_cut_directly(store, "episode-1")
    _assemble(fixture)
    episode = store.load(Episode, "episode-1")
    assert (episode.approved_cut_version, episode.approval_review_id) == (1, "gate")


# --------------------------------------------------------------------------
# R7 - shot-list changes revoke the cut
# --------------------------------------------------------------------------


def test_r7_propose_shots_on_an_approved_episode_clears_both_approval_fields(
    store: ProjectStore,
) -> None:
    add_episode(store, approved_cut_version=2, approval_review_id="gate")
    propose_shots(store, [Shot(shot_id="new", episode_id="ep1", duration_sec=3)])
    episode = store.load(Episode, "ep1")
    assert episode.shot_ids == ["new"]
    assert_cut_revoked(store)


def test_r7_propose_shots_leaves_other_episodes_approval_alone(store: ProjectStore) -> None:
    add_episode(store, "ep1", approved_cut_version=2, approval_review_id="gate")
    add_episode(store, "ep2", approved_cut_version=3, approval_review_id="gate2")
    propose_shots(store, [Shot(shot_id="new", episode_id="ep1", duration_sec=3)])
    assert_cut_revoked(store, "ep1")
    other = store.load(Episode, "ep2")
    assert (other.approved_cut_version, other.approval_review_id) == (3, "gate2")


def test_r7_propose_shots_on_an_unapproved_episode_still_appends(store: ProjectStore) -> None:
    add_episode(store)
    propose_shots(store, [Shot(shot_id="a", episode_id="ep1", duration_sec=3),
                          Shot(shot_id="b", episode_id="ep1", duration_sec=3)])
    assert store.load(Episode, "ep1").shot_ids == ["a", "b"]


# --------------------------------------------------------------------------
# R8 - server-found invalidation scope
# --------------------------------------------------------------------------


def test_r8_episode_ids_lists_every_episode_json_sorted(store: ProjectStore) -> None:
    assert store.episode_ids() == []
    for episode_id in ("ep3", "ep1", "ep2"):
        add_episode(store, episode_id)
    store._root.make_dir("episodes/no-episode-json")       # a folder without episode.json
    assert store.episode_ids() == ["ep1", "ep2", "ep3"]


def test_r8_revise_location_stales_shots_in_every_episode_without_being_told(
    store: ProjectStore,
) -> None:
    add_episode(store, "ep1")
    add_episode(store, "ep2")
    add_episode(store, "ep3")
    approved_shot(store, "a", episode_id="ep1", dependencies=("loc1",))
    approved_shot(store, "a", episode_id="ep2", dependencies=("loc1",))
    approved_shot(store, "other", episode_id="ep3", dependencies=("loc-other",))
    for episode_id in ("ep1", "ep2", "ep3"):
        approve_cut_directly(store, episode_id)
    location = Location(location_id="loc1", geometry_provenance="initial")
    store.create(location)
    revised = location.model_copy(update={**location.next_version(),
                                          "geometry_provenance": "revised"})

    changed = revise_location(store, revised, expected_version=1)

    assert set(changed) == {"a"}
    for episode_id in ("ep1", "ep2"):
        assert store.load(Shot, "a", episode_id=episode_id).status is ShotStatus.STALE
        assert_cut_revoked(store, episode_id)
    assert store.load(Shot, "other", episode_id="ep3").status is ShotStatus.APPROVED
    untouched = store.load(Episode, "ep3")
    assert untouched.approved_cut_version == 1


def test_r8_an_episode_not_named_anywhere_is_still_invalidated(store: ProjectStore) -> None:
    add_episode(store, "named")
    add_episode(store, "unnamed")
    approved_shot(store, "mine", episode_id="named", dependencies=("shared-loc",))
    approved_shot(store, "theirs", episode_id="unnamed", dependencies=("shared-loc",))
    approve_cut_directly(store, "unnamed")
    location = Location(location_id="shared-loc", geometry_provenance="initial")
    store.create(location)
    revised = location.model_copy(update={**location.next_version(),
                                          "geometry_provenance": "revised"})

    changed = revise_location(store, revised, expected_version=1)

    assert set(changed) == {"mine", "theirs"}
    assert store.load(Shot, "theirs", episode_id="unnamed").status is ShotStatus.STALE
    assert_cut_revoked(store, "unnamed")


def test_r8_revise_location_no_longer_accepts_episode_ids(store: ProjectStore) -> None:
    add_episode(store)
    location = Location(location_id="loc1")
    store.create(location)
    with pytest.raises(TypeError):
        revise_location(store, location.model_copy(update=location.next_version()),
                        expected_version=1, episode_ids=["ep1"])  # type: ignore[call-arg]


def test_r8_revise_camera_profile_scans_every_episode_without_being_told(
    store: ProjectStore,
) -> None:
    add_episode(store, "ep1")
    add_episode(store, "ep2")
    approved_shot(store, "hit1", episode_id="ep1", camera_id="cam1")
    approved_shot(store, "hit2", episode_id="ep2", camera_id="cam1")
    approved_shot(store, "miss", episode_id="ep2", camera_id="cam2")
    approve_cut_directly(store, "ep2")
    store.create(CameraProfile(camera_id="cam1", profile_name="CCTV"))
    before = store.load(CameraProfile, "cam1")
    revised = before.model_copy(update={**before.next_version(), "field_of_view_mm": 16})

    changed = revise_camera_profile(store, revised, expected_version=1)

    assert set(changed) == {"hit1", "hit2"}
    assert store.load(Shot, "miss", episode_id="ep2").status is ShotStatus.APPROVED
    assert_cut_revoked(store, "ep2")


def test_r8_revise_camera_profile_no_longer_accepts_episode_ids(store: ProjectStore) -> None:
    add_episode(store)
    store.create(CameraProfile(camera_id="cam1", profile_name="CCTV"))
    before = store.load(CameraProfile, "cam1")
    with pytest.raises(TypeError):
        revise_camera_profile(store, before.model_copy(update=before.next_version()),
                              expected_version=1, episode_ids=["ep1"])  # type: ignore[call-arg]


def _orphan_shot_folder(store: ProjectStore, *, camera_id: str = "cam1") -> None:
    """An approved Shot in an episode folder that has no episode.json."""
    approved_shot(store, "ghost", episode_id="ghost-ep", camera_id=camera_id)
    assert "ghost-ep" not in store.episode_ids()


def test_r8_revise_camera_profile_refuses_before_recording_a_plan_for_a_folder_without_episode_json(
    store: ProjectStore,
) -> None:
    add_episode(store, "ep1")
    _orphan_shot_folder(store)
    store.create(CameraProfile(camera_id="cam1", profile_name="CCTV"))
    before_profile = store.load(CameraProfile, "cam1")
    revised = before_profile.model_copy(update={**before_profile.next_version(),
                                                "field_of_view_mm": 16})
    before = tree(store)

    with pytest.raises((JobError, RecordNotFound)):
        revise_camera_profile(store, revised, expected_version=1)

    assert store.cascades() == ()
    assert tree(store) == before
    assert store.load(Shot, "ghost", episode_id="ghost-ep").status is ShotStatus.APPROVED


def test_r8a_derive_script_scene_plan_refuses_before_any_write_for_a_folder_without_episode_json(
    store: ProjectStore,
) -> None:
    from orchestrator.domain.entities import Beat, BeatSceneDirection, Script
    from orchestrator.service.script_authoring import derive_script_scene_plan

    store.save(Project(project_id="p1", title="fixture"), expected_version=0)
    add_episode(store, "ep1")
    store.create(CameraProfile(camera_id="cam1", profile_name="Fixture CCTV"))
    beat = Beat(beat_id="b1", target_sec=4, subtlety=2, narration_text="A shape.",
                scene_direction=BeatSceneDirection(location_id="room1", camera_profile="cam1",
                                                   framing="wide", event_sec=2))
    store.create(Script(script_id="script1", episode_id="ep1", logline="A quiet room",
                        beats=[beat], beat_to_shot={"b1": "shot1"},
                        scenes=[Location(location_id="room1")]))
    store.create(Shot(shot_id="stray", episode_id="ghost-ep", duration_sec=3))
    assert "ghost-ep" not in store.episode_ids()
    before = tree(store)

    with pytest.raises((JobError, RecordNotFound)):
        derive_script_scene_plan(
            store, episode_id="ep1", script_id="script1", expected_version=1,
            expected_episode_version=1, expected_shot_versions={"shot1": 0},
            expected_location_versions={"room1": 0},
        )

    assert tree(store) == before
    assert store.cascades() == ()


_TOKEN = "approval-integrity-token"
_HEADERS = {"X-Loopback-Token": _TOKEN}


def _location_client(tmp_path: Path) -> tuple[TestClient, ProjectStore]:
    project_dir = tmp_path / "p1"
    project_dir.mkdir()
    store = ProjectStore(ProjectRoot(project_dir))
    store.save(Project(project_id="p1", title="t"), expected_version=0)
    app = create_app(settings=Settings(project_root=tmp_path), token=_TOKEN)
    client = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    return client, store


def _location_revision_body(**extra) -> dict:
    revised = {"location_id": "loc1", "version": 2, "supersedes": 1,
               "geometry_provenance": "revised"}
    return {"location": revised, "expected_version": 1, **extra}


def test_r8_http_revision_with_episode_ids_is_a_422(tmp_path: Path) -> None:
    client, store = _location_client(tmp_path)
    store.create(Location(location_id="loc1"))
    body = _location_revision_body(episode_ids=["ep1"])
    response = client.post("/projects/p1/locations/loc1/revise", headers=_HEADERS, json=body)
    assert response.status_code == 422, response.text
    assert store.versions(Location, "loc1") == [1]


def test_r8_http_revision_without_episode_ids_invalidates_every_episode(
    tmp_path: Path,
) -> None:
    client, store = _location_client(tmp_path)
    add_episode(store, "ep1")
    approved_shot(store, "dependent", episode_id="ep1", dependencies=("loc1",))
    approve_cut_directly(store, "ep1")
    store.create(Location(location_id="loc1"))
    body = _location_revision_body()
    response = client.post("/projects/p1/locations/loc1/revise", headers=_HEADERS, json=body)
    assert response.status_code == 200, response.text
    assert store.load(Shot, "dependent", episode_id="ep1").status is ShotStatus.STALE
    assert_cut_revoked(store, "ep1")


def test_r8_http_revise_location_summary_describes_the_new_scope() -> None:
    from orchestrator.api.routes import OPERATIONS  # noqa: PLC0415

    operation = next(op for op in OPERATIONS if op.name == "revise_location")
    assert "episode_ids" not in operation.summary
    assert "every episode in the project is checked" in operation.summary


# --------------------------------------------------------------------------
# R9 - setting
# --------------------------------------------------------------------------


def test_r9_agent_may_approve_is_off_by_default() -> None:
    assert Settings().approvals.agent_may_approve is False
    assert ApprovalSettings().agent_may_approve is False


def test_r9_setting_can_be_turned_on_and_rejects_unknown_keys() -> None:
    assert Settings(approvals={"agent_may_approve": True}).approvals.agent_may_approve is True
    with pytest.raises(ValidationError):
        ApprovalSettings(agent_may_approve=True, surprise=1)
    with pytest.raises(ValidationError):
        Settings(approvals={"agent_may_approve": True, "surprise": 1})
