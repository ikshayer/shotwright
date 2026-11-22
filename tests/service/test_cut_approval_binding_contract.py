"""Cut approval binding contract tests (R1-R4 and the attack scenario).

Authored from ``orchestrator/api/CUT_APPROVAL_BINDING_CONTRACT.md`` alone, with fixtures taken
from pre-existing tests. The defect: gate C bound to a Timeline *version*, so two different
cut files of one version shared an approval and export could ship a cut the creator never
watched. Now an approval names one exact cut (``approved_cut_output_id``) and export ships only
that cut or a byte-identical one.

Distinct cuts of one Timeline version are produced by a fake encoder that writes chosen bytes.
Export tests that must reach a successful export need real FFmpeg and skip without it; export
refusal tests do not need media.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orchestrator.api import cli as cli_module
from orchestrator.api.app import create_app
from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import (
    Episode,
    EpisodeOutput,
    Location,
    Review,
    Script,
    Shot,
    Timeline,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import ApprovalSettings, Settings
from orchestrator.service.approval import approve_episode
from orchestrator.service.dependencies import (
    propose_shots,
    revise_location,
    revise_script,
    revise_shot,
)
from orchestrator.service.timeline import assemble_episode, compose_timeline
from orchestrator.store.json_store import ProjectStore, RecordNotFound
from tests.adapters.export.test_portable_package import (
    _EPISODE as EXPORT_EPISODE,
    _digest,
    _offline_media,
    _setup as _export_setup,
)
from tests.service.test_approval_integrity_contract import approved_shot
from tests.service.test_approval_integrity_contract import swap_first_two_shots
from tests.service.test_timeline_assembly_contract import _FakeEncoder
from tests.service.test_timeline_assembly_contract import _fixture as _assembly_fixture

EP = "episode-1"
PROJECT = "project-1"
_TOKEN = "cut-binding-token"
_NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
_REFUSED = {400, 404, 409, 422}


@pytest.fixture
def tmp_path() -> Path:
    """Contained, task-local scratch roots instead of the inaccessible system temp."""
    base = Path.cwd() / "_work" / "cut-binding-tests" / uuid.uuid4().hex
    ProjectRoot(Path.cwd()).make_dir(base.relative_to(Path.cwd()).as_posix())
    return base


# --------------------------------------------------------------------------
# service-level world: real assemble_episode, a fake encoder writing chosen bytes
# --------------------------------------------------------------------------


class _BytesEncoder(_FakeEncoder):
    """The fixture encoder, but the cut's bytes are whatever ``payload`` says."""

    payload: bytes = b"cut-a"

    def run(self, **kwargs: Any) -> Any:
        result = super().run(**kwargs)
        self.root.write_bytes(kwargs["output_relative"], self.payload)
        return dataclasses.replace(
            result, content_sha256=hashlib.sha256(self.payload).hexdigest())


@dataclass
class World:
    base: Path
    store: ProjectStore
    encoder: _BytesEncoder

    @property
    def settings(self) -> Settings:
        return Settings(project_root=self.base)

    def assemble(self, output_id: str, payload: bytes, *, timeline: Timeline | None = None,
                 ) -> EpisodeOutput:
        self.encoder.payload = payload
        cut = timeline or compose_timeline(self.store, episode_id=EP)
        return assemble_episode(self.store, self.encoder, episode_id=EP, timeline=cut,
                                output_id=output_id)

    def review(self, review_id: str, version: int) -> None:
        self.store.save(Review(
            review_id=review_id, target_id=EP, target_version=version,
            target_kind="episode_cut", episode_id=EP, actor="creator", created_at=_NOW,
            approval_state=ShotStatus.APPROVED,
        ), expected_version=0)

    def approve(self, cut_id: str, review_id: str, *, version: int | None = None) -> Episode:
        cut = next(c for c in self.store.list_episode_outputs(EP) if c.output_id == cut_id)
        self.review(review_id, cut.timeline_store_version if version is None else version)
        return approve_episode(self.store, EP, cut_output_id=cut_id, review_id=review_id,
                               expected_version=self.episode_token)

    @property
    def episode_token(self) -> int:
        return self.store.load_with_version(Episode, EP)[1]

    @property
    def timeline_token(self) -> int:
        return self.store.load_with_version(Timeline, EP)[1]

    @property
    def episode(self) -> Episode:
        return self.store.load(Episode, EP)

    def approve_directly(self, cut_id: str = "cut-a") -> None:
        """Set all three approval fields without the service (for the clearing-site tests)."""
        episode, token = self.store.load_with_version(Episode, EP)
        self.store.save(episode.model_copy(update={
            "approved_cut_version": 1, "approved_cut_output_id": cut_id,
            "approval_review_id": "gate"}), expected_version=token)


@pytest.fixture
def world(tmp_path: Path) -> World:
    project = ProjectRoot(tmp_path).make_dir(PROJECT)
    fixture = _assembly_fixture(Path(project))
    encoder = fixture["encoder"]
    encoder.__class__ = _BytesEncoder
    return World(tmp_path, fixture["store"], encoder)


def _approval(episode: Episode) -> tuple[Any, Any, Any]:
    return (episode.approved_cut_version, episode.approved_cut_output_id,
            episode.approval_review_id)


def assert_approval_cleared(episode: Episode) -> None:
    assert _approval(episode) == (None, None, None)


# --------------------------------------------------------------------------
# the attack scenario
# --------------------------------------------------------------------------


def test_attack_service_state_approval_follows_the_cut_creator_watched(world: World) -> None:
    """Approve A; assemble a different B of the same version; re-approve choosing B;
    re-assemble A byte-identically. The approval must still name B, and A2 is not B."""
    a = world.assemble("cut-a", b"cut-a")
    world.approve("cut-a", "gate-a")
    b = world.assemble("cut-b", b"cut-b")
    assert (a.timeline_store_version, a.timeline_sha256) == (
        b.timeline_store_version, b.timeline_sha256)
    assert a.content_sha256 != b.content_sha256

    world.approve("cut-b", "gate-b")
    a2 = world.assemble("cut-a2", b"cut-a")

    assert a2.content_sha256 == a.content_sha256 != b.content_sha256
    episode = world.episode
    assert episode.approved_cut_output_id == "cut-b"
    assert episode.approved_cut_output_id != a2.output_id
    assert episode.approval_review_id == "gate-b"
    assert episode.approved_cut_version == b.timeline_store_version


def _append_cut(fixture: Any, output_id: str, data: bytes, *, like: EpisodeOutput | None = None,
                ) -> EpisodeOutput:
    """A further cut of the export fixture's Timeline version, receipted against its file."""
    like = like or fixture.cut
    relative = f"episodes/{EXPORT_EPISODE}/outputs/cuts/{output_id}.mp4"
    fixture.root.write_bytes(relative, data)
    cut = like.model_copy(update={"output_id": output_id, "relative_path": relative,
                                  "content_sha256": _digest(data)})
    fixture.store.append_episode_output(
        cut, expected_version=len(fixture.store.list_episode_outputs(EXPORT_EPISODE)))
    return cut


def _approve_export_cut(fixture: Any, cut_id: str, review_id: str) -> None:
    fixture.store.save(Review(
        review_id=review_id, target_id=EXPORT_EPISODE,
        target_version=fixture.cut.timeline_store_version, target_kind="episode_cut",
        episode_id=EXPORT_EPISODE, actor="creator", created_at=_NOW,
        approval_state=ShotStatus.APPROVED,
    ), expected_version=0)
    approve_episode(fixture.store, EXPORT_EPISODE, cut_output_id=cut_id, review_id=review_id,
                    expected_version=fixture.store.load_with_version(Episode, EXPORT_EPISODE)[1])


def _export(fixture: Any, cut_id: str):
    token = fixture.store.load_with_version(Episode, EXPORT_EPISODE)[1]
    return fixture.export(cut_output_id=cut_id, expected_version=token)


def _assert_no_export(fixture: Any) -> None:
    assert not list(fixture.root.path.glob("exports/*/export.json"))


def test_attack_export_refuses_the_byte_identical_cut_the_approval_does_not_name(
    tmp_path: Path,
) -> None:
    fixture = _export_setup(tmp_path, b"registered local cut fixture")   # A approved
    _append_cut(fixture, "cut-b", b"a different cut of the same Timeline version")
    _approve_export_cut(fixture, "cut-b", "gate-b")                       # creator watched B
    _append_cut(fixture, "cut-a2", b"registered local cut fixture")       # A re-assembled

    response = _export(fixture, "cut-a2")

    assert response.status_code in _REFUSED, response.text
    assert "cut-a2" in response.text and "cut-b" in response.text
    _assert_no_export(fixture)


def test_attack_export_ships_only_the_approved_cut(tmp_path: Path) -> None:
    """The full scenario with real media: A2 is refused, B (the approved cut) exports."""
    cut_a = _offline_media(ProjectRoot(tmp_path), "mixed-cut.mp4", audio=True)
    fixture = _setup_with_real_variant(tmp_path, cut_a)
    _approve_export_cut(fixture, "cut-b", "gate-b")
    _append_cut(fixture, "cut-a2", cut_a)

    refused = _export(fixture, "cut-a2")
    assert refused.status_code in _REFUSED, refused.text
    _assert_no_export(fixture)

    accepted = _export(fixture, "cut-b")
    assert accepted.status_code == 200, accepted.text


def _setup_with_real_variant(tmp_path: Path, cut_a: bytes) -> Any:
    """Export fixture approved on A, plus a real, byte-different cut B of the same version."""
    fixture = _export_setup(tmp_path, cut_a)
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    variant = ProjectRoot(tmp_path)

    def encode(path: Path) -> None:
        subprocess.run([
            ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=0x9a5686:size=64x48:rate=12:duration=1", "-f", "lavfi", "-i",
            "sine=frequency=900:sample_rate=48000:duration=1", "-c:v", "libx264",
            "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ], check=True, capture_output=True, timeout=30)

    variant.write_with("variant-cut.mp4", encode)
    data = variant.read_bytes("variant-cut.mp4")
    assert data != cut_a
    _append_cut(fixture, "cut-b", data)
    return fixture


# --------------------------------------------------------------------------
# R1 - the Episode records the approved cut
# --------------------------------------------------------------------------


def test_r1_field_defaults_to_none() -> None:
    episode = Episode(episode_id="e", project_id="p", number=1, duration_target_sec=30)
    assert episode.approved_cut_output_id is None


def test_r1_old_episode_json_without_the_field_loads(world: World) -> None:
    path = world.store._root.resolve(f"episodes/{EP}/episode.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("approved_cut_output_id", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert world.store.load(Episode, EP).approved_cut_output_id is None


def test_r1_a_new_episode_cannot_be_created_with_it_set(tmp_path: Path) -> None:
    store = ProjectStore(ProjectRoot(tmp_path))
    with pytest.raises(JobError, match="approved_cut_output_id") as error:
        store.save(Episode(episode_id="e", project_id="p", number=1, duration_target_sec=30,
                           approved_cut_output_id="some-cut"), expected_version=0)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST


def test_r1_the_field_uses_the_output_id_pattern() -> None:
    with pytest.raises(ValidationError):
        Episode(episode_id="e", project_id="p", number=1, duration_target_sec=30,
                approved_cut_output_id="not a valid/id")


def _approved_world_episode(world: World) -> World:
    world.assemble("cut-a", b"cut-a")
    world.approve_directly()
    assert world.episode.approved_cut_output_id == "cut-a"
    return world


def test_r1_revise_shot_cascade_clears_all_three_fields(world: World) -> None:
    _approved_world_episode(world)
    shot, token = world.store.load_with_version(Shot, "shot-dvr", episode_id=EP)
    revise_shot(world.store, shot.model_copy(update={
        **shot.next_version(), "status": ShotStatus.DRAFT, "approval_review_id": None}),
        expected_version=token)
    assert_approval_cleared(world.episode)


def test_r1_revise_script_cascade_clears_all_three_fields(world: World) -> None:
    _approved_world_episode(world)
    script = world.store.load(Script, "script-1", episode_id=EP)
    swapped = {**script.beat_to_shot, "beat-lowlight": script.beat_to_shot["beat-dvr"],
               "beat-dvr": script.beat_to_shot["beat-lowlight"]}
    revise_script(world.store, script.model_copy(update={
        **script.next_version(), "beat_to_shot": swapped}), expected_version=1)
    assert_approval_cleared(world.episode)


def test_r1_revise_location_cascade_clears_all_three_fields(tmp_path: Path) -> None:
    store = ProjectStore(ProjectRoot(tmp_path))
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)
    approved_shot(store, "s1", dependencies=("loc1",))
    episode, token = store.load_with_version(Episode, "ep1")
    store.save(episode.model_copy(update={
        "approved_cut_version": 1, "approved_cut_output_id": "cut-a",
        "approval_review_id": "gate"}), expected_version=token)
    location = Location(location_id="loc1", geometry_provenance="initial")
    store.create(location)
    revise_location(store, location.model_copy(update={
        **location.next_version(), "geometry_provenance": "revised"}), expected_version=1)
    assert_approval_cleared(store.load(Episode, "ep1"))


def test_r1_propose_shots_on_an_approved_episode_clears_all_three_fields(world: World) -> None:
    _approved_world_episode(world)
    propose_shots(world.store, [Shot(shot_id="extra", episode_id=EP, duration_sec=3)])
    assert_approval_cleared(world.episode)


def test_r1_a_changed_timeline_in_assemble_episode_clears_all_three_fields(
    world: World,
) -> None:
    world.assemble("cut-a", b"cut-a")
    world.approve("cut-a", "gate-a")
    assert world.episode.approved_cut_output_id == "cut-a"
    head = world.store.load(Timeline, EP)
    world.assemble("cut-c", b"cut-c", timeline=swap_first_two_shots(head))
    assert_approval_cleared(world.episode)


# --------------------------------------------------------------------------
# R2 - approving a cut names the cut
# --------------------------------------------------------------------------


def test_r2_approve_sets_version_output_id_and_review(world: World) -> None:
    cut = world.assemble("cut-a", b"cut-a")
    result = world.approve("cut-a", "gate-a")
    assert _approval(result) == (cut.timeline_store_version, "cut-a", "gate-a")
    assert _approval(world.episode) == (world.timeline_token, "cut-a", "gate-a")


def test_r2_unknown_cut_id_is_record_not_found(world: World) -> None:
    world.assemble("cut-a", b"cut-a")
    world.review("gate", world.timeline_token)
    with pytest.raises(RecordNotFound):
        approve_episode(world.store, EP, cut_output_id="no-such-cut", review_id="gate",
                        expected_version=world.episode_token)
    assert_approval_cleared(world.episode)


def test_r2_a_cut_of_an_older_timeline_version_is_refused(world: World) -> None:
    old = world.assemble("cut-old", b"cut-old")
    head = world.store.load(Timeline, EP)
    world.assemble("cut-new", b"cut-new", timeline=swap_first_two_shots(head))
    assert world.timeline_token > old.timeline_store_version

    world.review("gate-old", old.timeline_store_version)   # even a review of the old version
    with pytest.raises(JobError) as error:
        approve_episode(world.store, EP, cut_output_id="cut-old", review_id="gate-old",
                        expected_version=world.episode_token)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert "timeline" in str(error.value).lower()
    assert_approval_cleared(world.episode)


def test_r2_a_cut_whose_timeline_hash_mismatches_is_refused(world: World) -> None:
    world.assemble("cut-a", b"cut-a")
    receipt = next(world.store._root.path.glob("episodes/*/outputs/cut_receipts/*cut-a.json"))
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["timeline_sha256"] = "a" * 64
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    world.review("gate", world.timeline_token)
    with pytest.raises(JobError) as error:
        approve_episode(world.store, EP, cut_output_id="cut-a", review_id="gate",
                        expected_version=world.episode_token)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert_approval_cleared(world.episode)


def test_r2_the_review_must_target_the_cuts_timeline_version(world: World) -> None:
    cut = world.assemble("cut-a", b"cut-a")
    world.review("wrong-version", cut.timeline_store_version + 1)
    with pytest.raises(JobError) as error:
        approve_episode(world.store, EP, cut_output_id="cut-a", review_id="wrong-version",
                        expected_version=world.episode_token)
    assert error.value.kind is JobErrorKind.INVALID_MANIFEST
    assert_approval_cleared(world.episode)

    world.review("right-version", cut.timeline_store_version)
    approve_episode(world.store, EP, cut_output_id="cut-a", review_id="right-version",
                    expected_version=world.episode_token)
    assert world.episode.approval_review_id == "right-version"


def test_r2_the_cut_version_parameter_is_gone(world: World) -> None:
    world.assemble("cut-a", b"cut-a")
    world.review("gate", world.timeline_token)
    with pytest.raises(TypeError):
        approve_episode(world.store, EP, cut_version=1, review_id="gate",  # type: ignore[call-arg]
                        expected_version=world.episode_token)


def test_r2_cli_approve_cut_with_a_cut_id_auto_records_a_review_of_the_cuts_version(
    world: World, capsys: pytest.CaptureFixture[str],
) -> None:
    cut = world.assemble("cut-a", b"cut-a")
    body = {"expected_version": world.episode_token, "cut_output_id": "cut-a"}
    before = {r.review_id for r in world.store.list_reviews()}

    code = cli_module.main(
        ["approve-cut", "--project-id", PROJECT, "--episode-id", EP,
         "--data", json.dumps(body), "--json"], settings=world.settings)

    captured = capsys.readouterr()
    assert code == 0, captured.err
    (review,) = [r for r in world.store.list_reviews() if r.review_id not in before]
    assert (review.target_kind, review.target_id, review.target_version) == (
        "episode_cut", EP, cut.timeline_store_version)
    assert review.actor == "creator"
    assert _approval(world.episode) == (cut.timeline_store_version, "cut-a", review.review_id)


@pytest.mark.parametrize("body", [
    {"cut_version": 1},                                    # the removed field alone
    {"cut_output_id": "cut-a", "cut_version": 1},          # the removed field alongside
    {},                                                    # no cut named at all
])
def test_r2_http_approve_cut_rejects_cut_version_and_requires_cut_output_id(
    world: World, body: dict[str, Any],
) -> None:
    world.assemble("cut-a", b"cut-a")
    settings = Settings(project_root=world.base,
                        approvals=ApprovalSettings(agent_may_approve=True))
    app = create_app(settings=settings, token=_TOKEN)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8765",
                        raise_server_exceptions=False) as client:
            response = client.post(
                f"/projects/{PROJECT}/episodes/{EP}/cut/approve",
                headers={"X-Loopback-Token": _TOKEN},
                json={"expected_version": world.episode_token, **body})
    finally:
        app.state.context.job_runner.close()
    assert response.status_code == 422, response.text
    assert_approval_cleared(world.episode)


def test_r2_http_approve_cut_with_a_cut_id_approves_that_cut(world: World) -> None:
    cut = world.assemble("cut-a", b"cut-a")
    before = {r.review_id for r in world.store.list_reviews()}
    settings = Settings(project_root=world.base,
                        approvals=ApprovalSettings(agent_may_approve=True))
    app = create_app(settings=settings, token=_TOKEN)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8765",
                        raise_server_exceptions=False) as client:
            response = client.post(
                f"/projects/{PROJECT}/episodes/{EP}/cut/approve",
                headers={"X-Loopback-Token": _TOKEN},
                json={"expected_version": world.episode_token, "cut_output_id": "cut-a"})
    finally:
        app.state.context.job_runner.close()
    assert response.status_code == 200, response.text
    (review,) = [r for r in world.store.list_reviews() if r.review_id not in before]
    assert review.target_version == cut.timeline_store_version
    assert _approval(world.episode) == (cut.timeline_store_version, "cut-a", review.review_id)


# --------------------------------------------------------------------------
# R3 - export ships only the approved cut
# --------------------------------------------------------------------------


def test_r3_export_refuses_a_legacy_approval_without_a_cut_id(tmp_path: Path) -> None:
    fixture = _export_setup(tmp_path, b"registered local cut fixture")
    episode, token = fixture.store.load_with_version(Episode, EXPORT_EPISODE)
    assert episode.approved_cut_output_id == fixture.cut.output_id   # the fixture approved it
    fixture.store.save(episode.model_copy(update={"approved_cut_output_id": None}),
                       expected_version=token)
    assert fixture.store.load(Episode, EXPORT_EPISODE).approved_cut_version is not None

    response = _export(fixture, fixture.cut.output_id)

    assert response.status_code in _REFUSED, response.text
    assert "approve_cut" in response.text
    _assert_no_export(fixture)


def test_r3_export_refuses_a_different_cut_of_the_approved_version_naming_both_ids(
    tmp_path: Path,
) -> None:
    fixture = _export_setup(tmp_path, b"registered local cut fixture")
    other = _append_cut(fixture, "other-cut", b"a different cut of the same version")
    assert other.timeline_store_version == fixture.cut.timeline_store_version

    response = _export(fixture, "other-cut")

    assert response.status_code in _REFUSED, response.text
    assert "other-cut" in response.text and fixture.cut.output_id in response.text
    _assert_no_export(fixture)


def test_r3_export_accepts_a_byte_identical_reassembly_of_the_approved_cut(
    tmp_path: Path,
) -> None:
    cut = _offline_media(ProjectRoot(tmp_path), "mixed-cut.mp4", audio=True)
    fixture = _export_setup(tmp_path, cut)
    _append_cut(fixture, "reassembled", cut)

    response = _export(fixture, "reassembled")

    assert response.status_code == 200, response.text


def test_r3_export_still_accepts_the_approved_cut_itself(tmp_path: Path) -> None:
    cut = _offline_media(ProjectRoot(tmp_path), "mixed-cut.mp4", audio=True)
    fixture = _export_setup(tmp_path, cut)
    assert _export(fixture, fixture.cut.output_id).status_code == 200


# --------------------------------------------------------------------------
# R4 - new cuts no longer touch the approval
# --------------------------------------------------------------------------


def test_r4_a_new_different_cut_leaves_the_approval_pointing_at_its_own_cut(
    world: World,
) -> None:
    world.assemble("cut-a", b"cut-a")
    approved = world.approve("cut-a", "gate-a")

    new = world.assemble("cut-b", b"cut-b")

    assert new.content_sha256 != world.store.list_episode_outputs(EP)[0].content_sha256
    assert _approval(world.episode) == _approval(approved)
    assert world.episode.approved_cut_output_id == "cut-a"


def test_r4_a_byte_identical_reassembly_leaves_the_approval_untouched(world: World) -> None:
    world.assemble("cut-a", b"cut-a")
    approved = world.approve("cut-a", "gate-a")
    world.assemble("cut-a2", b"cut-a")
    assert _approval(world.episode) == _approval(approved)


def test_r4_a_changed_timeline_still_revokes_all_three_fields(world: World) -> None:
    world.assemble("cut-a", b"cut-a")
    world.approve("cut-a", "gate-a")
    head = world.store.load(Timeline, EP)
    world.assemble("cut-c", b"cut-c", timeline=swap_first_two_shots(head))
    assert_approval_cleared(world.episode)
