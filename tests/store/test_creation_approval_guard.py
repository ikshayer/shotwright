from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    Episode,
    Location,
    Project,
    Review,
    Script,
    Shot,
    StyleBible,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.approval import approve_shot
from orchestrator.service.dependencies import create_episode
from orchestrator.store.json_store import ProjectStore
from tests.service.test_compose import _tree_snapshot


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(ProjectRoot(tmp_path))


@pytest.mark.parametrize(
    ("entity", "field", "writer"),
    [
        pytest.param(
            Location(location_id="hall", approval_review_id="forged-review"),
            "approval_review_id",
            "create",
            id="location-review-pointer",
        ),
        pytest.param(
            Shot(
                shot_id="s1",
                episode_id="ep1",
                duration_sec=5,
                status=ShotStatus.DRAFT,
                approval_review_id="forged-review",
            ),
            "approval_review_id",
            "create",
            id="draft-shot-review-pointer",
        ),
        pytest.param(
            Script(
                script_id="script-1",
                episode_id="ep1",
                logline="A test script",
                approval_review_id="forged-review",
            ),
            "approval_review_id",
            "create",
            id="script-review-pointer",
        ),
        pytest.param(
            StyleBible(project_id="p1", approval_review_id="forged-review"),
            "approval_review_id",
            "create",
            id="style-bible-review-pointer",
        ),
        pytest.param(
            Episode(
                episode_id="ep1",
                project_id="p1",
                number=1,
                duration_target_sec=30,
                approval_review_id="forged-review",
            ),
            "approval_review_id",
            "create_episode",
            id="episode-review-pointer",
        ),
        pytest.param(
            Episode(
                episode_id="ep1",
                project_id="p1",
                number=1,
                duration_target_sec=30,
                approved_cut_version=1,
            ),
            "approved_cut_version",
            "create_episode",
            id="episode-approved-cut-version",
        ),
        pytest.param(
            Episode(
                episode_id="ep1",
                project_id="p1",
                number=1,
                duration_target_sec=30,
                approved_cut_output_id="cut-1",
            ),
            "approved_cut_output_id",
            "create_episode",
            id="episode-approved-cut-output-id",
        ),
    ],
)
def test_creation_rejects_approval_claim_without_writing(
    store: ProjectStore,
    tmp_path: Path,
    entity: Location | Shot | Script | StyleBible | Episode,
    field: str,
    writer: str,
) -> None:
    if writer == "create_episode":
        store.save(Project(project_id="p1", title="Approval guard fixture"), expected_version=0)

    before = _tree_snapshot(tmp_path)
    with pytest.raises(JobError) as caught:
        if writer == "create_episode":
            assert isinstance(entity, Episode)
            create_episode(store, entity)
        else:
            assert writer == "create"
            assert not isinstance(entity, Episode)
            store.create(entity)

    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    assert field in str(caught.value)
    assert "approval is set only by the approval service" in str(caught.value)
    assert _tree_snapshot(tmp_path) == before


def test_approval_service_can_approve_a_cleanly_created_shot(store: ProjectStore) -> None:
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=5)
    store.create(shot)

    for status in (
        ShotStatus.PLANNED,
        ShotStatus.BLOCKING,
        ShotStatus.BUILDING,
        ShotStatus.REVIEW,
    ):
        current, token = store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id)
        changes = {"status": status}
        if status is ShotStatus.BUILDING:
            changes["framing_status"] = FramingStatus.APPROVED
        store.update(current.model_copy(update=changes), expected_version=token)

    store.save(
        Review(
            review_id="r1",
            target_id=shot.shot_id,
            target_version=shot.version,
            target_kind="shot",
            episode_id=shot.episode_id,
            actor="creator",
            created_at=datetime.now(UTC),
            approval_state=ShotStatus.APPROVED,
        ),
        expected_version=0,
    )

    current, token = store.load_with_version(Shot, shot.shot_id, episode_id=shot.episode_id)
    approved = approve_shot(
        store,
        shot.shot_id,
        episode_id=shot.episode_id,
        review_id="r1",
        expected_version=token,
    )

    assert approved.status is ShotStatus.APPROVED
    assert approved.approval_review_id == "r1"
    assert store.load(Shot, shot.shot_id, episode_id=shot.episode_id) == approved
