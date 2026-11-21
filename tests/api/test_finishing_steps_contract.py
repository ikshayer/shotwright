"""Finishing steps contract, part A: tests from ``orchestrator/api/FINISHING_STEPS_CONTRACT.md``.

Authored from the contract (and the step-1 service contract it builds on) alone, without
reading the implementation. Where these tests and the implementation disagree, the
contract decides and the disagreement is a finding.

Interpretations the contract leaves open are marked ``AMBIGUOUS`` in the docstring of the
test that encodes them.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from orchestrator.api import cli as cli_module
from orchestrator.api import manifest as manifest_module
from orchestrator.api import mcp
from orchestrator.api.app import OperationContext
from orchestrator.domain.base import FramingStatus, ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    EpisodeOutput,
    Job,
    Location,
    Project,
    Review,
    Shot,
    ShotOutput,
    ShotOutputKind,
    StyleBible,
    Timeline,
    TimelineEntry,
)
from orchestrator.jobs.journal import append_job
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import ApprovalSettings, Settings
from orchestrator.service.approval import record_review
from orchestrator.store.json_store import ProjectStore, RecordNotFound
from tests.service.test_h3_promotion import RUN as H3_RUN
from tests.service.test_h3_promotion import _setup as _h3_setup

from .conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER, make_app
from .test_mcp_adapter import AppTransport, _rpc, _tool_result

PID = "p1"
EP = "ep1"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
AUTH = {TOKEN_HEADER: TOKEN}
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not on PATH")

SHOT_BASE = f"/projects/{PID}/episodes/{EP}/shots/s1"
CUT_URL = f"/projects/{PID}/episodes/{EP}/cut/approve"
REVIEWS_URL = f"/projects/{PID}/reviews"
APPROVE_URL = {
    "framing": SHOT_BASE + "/framing/approve",
    "shot": SHOT_BASE + "/approve",
    "cut": CUT_URL,
}
CLI_APPROVE = {"framing": "approve-framing", "shot": "approve-shot", "cut": "approve-cut"}
#: approval kind -> (Review target_kind, target_id, episode_id)
REVIEW_TARGET = {
    "framing": ("framing", "s1", EP),
    "shot": ("shot", "s1", EP),
    "cut": ("episode_cut", EP, EP),
}
APPROVAL_KINDS = ["framing", "shot", "cut"]

# (HTTP method, OpenAPI path) for every operation this contract adds.
OPERATIONS = {
    "record_review": ("post", "/projects/{project_id}/reviews"),
    "list_reviews": ("get", "/projects/{project_id}/reviews"),
    "approve_framing": (
        "post", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/framing/approve"),
    "approve_shot": (
        "post", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/approve"),
    "approve_cut": ("post", "/projects/{project_id}/episodes/{episode_id}/cut/approve"),
    "resume_invalidations": ("post", "/projects/{project_id}/invalidations/resume"),
    "revise_camera_profile": (
        "post", "/projects/{project_id}/camera-profiles/{camera_id}/revise"),
    "cancel_job": ("post", "/projects/{project_id}/jobs/{job_id}/cancel"),
    "list_shot_outputs": (
        "get", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/outputs"),
    "list_cuts": ("get", "/projects/{project_id}/episodes/{episode_id}/cuts"),
    "inspect_output": (
        "get", "/projects/{project_id}/episodes/{episode_id}/outputs/{output_id}/frame"),
}
CREATOR_ONLY = ["approve_framing", "approve_shot", "approve_cut"]


# --------------------------------------------------------------------------
# fixtures and builders
# --------------------------------------------------------------------------


@dataclass
class World:
    settings: Settings
    store: ProjectStore

    @property
    def root(self) -> Path:
        return self.settings.project_root


@pytest.fixture
def world(settings: Settings) -> World:
    base = ProjectRoot(settings.project_root)
    base.make_dir(PID)
    store = ProjectStore(ProjectRoot(base.resolve(PID)))
    store.save(Project(project_id=PID, title="Finishing steps"), expected_version=0)
    return World(settings, store)


@pytest.fixture
def permissive_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"approvals": ApprovalSettings(agent_may_approve=True)})


@pytest.fixture
def permissive_client(permissive_settings: Settings) -> Iterator[TestClient]:
    with TestClient(make_app(permissive_settings), base_url=LOOPBACK_BASE,
                    raise_server_exceptions=False) as c:
        yield c


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def post(client: TestClient, url: str, body: Any = None, headers: dict[str, str] | None = None):
    return client.post(url, json={} if body is None else body, headers={**AUTH, **(headers or {})})


def get(client: TestClient, url: str, **params: Any):
    return client.get(url, headers=AUTH, params=params or None)


def run_cli(settings: Settings, capsys: pytest.CaptureFixture[str], name: str,
            data: dict[str, Any] | None = None, **ids: str) -> tuple[int, str, str]:
    argv = [name]
    for key, value in ids.items():
        argv += [f"--{key.replace('_', '-')}", value]
    argv += ["--data", json.dumps(data or {}), "--json"]
    code = cli_module.main(argv, settings=settings)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def reviews_of(store: ProjectStore) -> list[Review]:
    return store.list_reviews()


def ensure_episode(store: ProjectStore, episode_id: str = EP) -> None:
    if episode_id not in store.episode_ids():
        store.save(Episode(episode_id=episode_id, project_id=PID, number=1,
                           duration_target_sec=30), expected_version=0)


def shot_token(store: ProjectStore, shot_id: str = "s1", episode_id: str = EP) -> int:
    return store.load_with_version(Shot, shot_id, episode_id=episode_id)[1]


def episode_token(store: ProjectStore, episode_id: str = EP) -> int:
    return store.load_with_version(Episode, episode_id)[1]


def add_shot(store: ProjectStore, shot_id: str = "s1", episode_id: str = EP, **fields: Any) -> Shot:
    ensure_episode(store, episode_id)
    shot = Shot(shot_id=shot_id, episode_id=episode_id, duration_sec=4, **fields)
    store.create(shot)
    return shot


def shot_in_review(store: ProjectStore, shot_id: str = "s1", episode_id: str = EP) -> Shot:
    shot = add_shot(store, shot_id, episode_id)
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING, ShotStatus.REVIEW):
        update: dict[str, Any] = {"status": status}
        if status is ShotStatus.BUILDING:
            update["framing_status"] = FramingStatus.APPROVED
        shot = shot.model_copy(update=update)
        store.update(shot, expected_version=shot_token(store, shot_id, episode_id))
    return shot


def framing_fixture(store: ProjectStore) -> None:
    """A BLOCKING shot with pending framing, a matching preview and its CameraProfile."""
    ensure_episode(store)
    if not store.versions(CameraProfile, "cam1"):
        store.create(CameraProfile(camera_id="cam1", profile_name="CCTV"))
    panel = f"previews/{EP}/s1/panel.png"
    data = b"panel bytes"
    store._root.write_bytes(panel, data)
    shot = Shot(shot_id="s1", episode_id=EP, duration_sec=4, camera_profile="cam1",
                framing_panel_ref=panel, framing_status=FramingStatus.PENDING)
    store.create(shot)
    for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING):
        shot = shot.model_copy(update={"status": status})
        store.update(shot, expected_version=shot_token(store))
    store.append_shot_output(ShotOutput(
        output_id="framing-preview", episode_id=EP, shot_id="s1", record_version=1,
        kind=ShotOutputKind.PREVIEW, relative_path=panel,
        content_sha256=hashlib.sha256(data).hexdigest(), camera_profile_id="cam1",
        camera_profile_version=1, engine_id="BLENDER_EEVEE", engine_build="fixture-build",
    ), expected_version=0)


def timeline(duration: float = 3.0) -> Timeline:
    return Timeline(episode_id=EP, entries=[
        TimelineEntry(type="card", start_sec=0, duration_sec=duration)])


def save_review(store: ProjectStore, kind: str, *, actor: str = "creator",
                review_id: str = "pre", version: int = 1, state: ShotStatus = ShotStatus.APPROVED,
                ) -> Review:
    target_kind, target_id, episode_id = REVIEW_TARGET[kind]
    review = Review(review_id=review_id, target_kind=target_kind, target_id=target_id,
                    target_version=version, episode_id=episode_id, actor=actor,
                    created_at=NOW, approval_state=state)
    store.save(review, expected_version=0)
    return review


def prepare(world: World, kind: str, *, review_id: str | None = None,
            comment: str | None = None, draft_blocker: bool = False) -> tuple[str, dict[str, Any]]:
    """Build the project state an approval of ``kind`` needs; return (url, valid body)."""
    store = world.store
    if kind == "framing":
        framing_fixture(store)
        body: dict[str, Any] = {"expected_version": shot_token(store)}
    elif kind == "shot":
        shot_in_review(store)
        body = {"expected_version": shot_token(store)}
    else:
        ensure_episode(store)
        store.save(timeline(), expected_version=0)
        if draft_blocker:
            add_shot(store, "blocker")
        body = {"expected_version": episode_token(store), "cut_version": 1}
    if review_id is not None:
        body["review_id"] = review_id
    if comment is not None:
        body["comment"] = comment
    return APPROVE_URL[kind], body


def cli_ids(kind: str) -> dict[str, str]:
    ids = {"project_id": PID, "episode_id": EP}
    if kind != "cut":
        ids["shot_id"] = "s1"
    return ids


def assert_approved(store: ProjectStore, kind: str, review_id: str, cut_version: int = 1) -> None:
    if kind == "framing":
        assert store.load(Shot, "s1", episode_id=EP).framing_status is FramingStatus.APPROVED
    elif kind == "shot":
        shot = store.load(Shot, "s1", episode_id=EP)
        assert shot.status is ShotStatus.APPROVED
        assert shot.approval_review_id == review_id
    else:
        episode = store.load(Episode, EP)
        assert episode.approved_cut_version == cut_version
        assert episode.approval_review_id == review_id


def assert_not_approved(store: ProjectStore, kind: str) -> None:
    if kind == "framing":
        assert store.load(Shot, "s1", episode_id=EP).framing_status is not FramingStatus.APPROVED
    elif kind == "shot":
        assert store.load(Shot, "s1", episode_id=EP).status is not ShotStatus.APPROVED
    else:
        assert store.load(Episode, EP).approved_cut_version is None


def review_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"target_kind": "shot", "target_id": "s1", "target_version": 1,
                            "episode_id": EP, "approval_state": "approved"}
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not ...}


# --------------------------------------------------------------------------
# R1 - who is calling
# --------------------------------------------------------------------------


def test_r1_operation_context_defaults_to_agent_and_accepts_creator(settings: Settings) -> None:
    assert OperationContext(settings, None).actor == "agent"
    assert OperationContext(settings, None, actor="creator").actor == "creator"


def test_r1_http_record_review_stores_actor_agent(client: TestClient, world: World) -> None:
    add_shot(world.store)
    response = post(client, REVIEWS_URL, review_body(comment="looks fine"))
    assert response.status_code in (200, 201), response.text
    body = response.json()
    assert body["actor"] == "agent"
    assert world.store.load(Review, body["review_id"]).actor == "agent"


def test_r1_cli_record_review_stores_actor_creator(
    world: World, capsys: pytest.CaptureFixture[str],
) -> None:
    add_shot(world.store)
    code, out, err = run_cli(world.settings, capsys, "record-review", review_body(),
                             project_id=PID)
    assert code == 0, err
    created = json.loads(out)
    assert created["actor"] == "creator"
    assert world.store.load(Review, created["review_id"]).actor == "creator"


@pytest.mark.parametrize("headers", [
    {"X-Actor": "creator"}, {"Actor": "creator"}, {"X-Creator": "true"},
    {"X-Pipeline-Actor": "creator"}, {"X-Loopback-Actor": "creator"},
    {"X-Forwarded-For": "127.0.0.1"}, {"User-Agent": "cctv-pipeline-cli"},
    {"Authorization": "Bearer creator"},
])
def test_r1_no_header_changes_the_actor(client: TestClient, world: World,
                                        headers: dict[str, str]) -> None:
    add_shot(world.store)
    response = post(client, REVIEWS_URL, review_body(), headers=headers)
    assert response.status_code in (200, 201), response.text
    assert response.json()["actor"] == "agent"


def test_r1_no_query_parameter_changes_the_actor(client: TestClient, world: World) -> None:
    """AMBIGUOUS: an unknown query parameter may be ignored or rejected; either is safe.
    What is forbidden is a stored actor other than "agent"."""
    add_shot(world.store)
    response = client.post(REVIEWS_URL + "?actor=creator", json=review_body(), headers=AUTH)
    if response.status_code in (200, 201):
        assert response.json()["actor"] == "agent"
    else:
        assert 400 <= response.status_code < 500
        assert reviews_of(world.store) == []


@pytest.mark.parametrize("actor", ["creator", "agent"])
def test_r1_a_body_that_sets_actor_is_rejected(client: TestClient, world: World,
                                               actor: str) -> None:
    add_shot(world.store)
    before = tree_bytes(world.root)
    response = post(client, REVIEWS_URL, review_body(actor=actor))
    assert response.status_code == 422
    assert "actor" in response.text
    assert tree_bytes(world.root) == before


def test_r1_cli_rejects_a_body_that_sets_actor(
    world: World, capsys: pytest.CaptureFixture[str],
) -> None:
    add_shot(world.store)
    code, _out, err = run_cli(world.settings, capsys, "record-review",
                              review_body(actor="agent"), project_id=PID)
    assert code != 0
    assert "actor" in err
    assert reviews_of(world.store) == []


# --------------------------------------------------------------------------
# R2 - creator-only operations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("explicit_review", [False, True])
@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r2_http_approval_is_refused_by_default_and_writes_nothing(
    client: TestClient, world: World, kind: str, explicit_review: bool,
) -> None:
    review_id = None
    url, body = prepare(world, kind)
    if explicit_review:
        save_review(world.store, kind, actor="creator")
        body["review_id"] = "pre"
        review_id = "pre"
    before = tree_bytes(world.root)

    response = post(client, url, body)

    assert response.status_code == 403, response.text
    payload = response.json()
    assert payload["error"] == "creator_only"
    assert "approvals.agent_may_approve" in payload["message"]
    assert "creator" in payload["message"].lower()
    assert "cli" in payload["message"].lower()
    assert tree_bytes(world.root) == before
    assert_not_approved(world.store, kind)
    assert review_id is None or len(reviews_of(world.store)) == 1


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r2_the_refusal_happens_before_anything_is_read(client: TestClient, kind: str) -> None:
    """The project does not exist: a 403 (not 404) proves the check ran first."""
    body: dict[str, Any] = {"expected_version": 1}
    if kind == "cut":
        body["cut_version"] = 1
    response = post(client, APPROVE_URL[kind].replace(f"/projects/{PID}/", "/projects/no-such-project/"), body)
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "creator_only"


@pytest.mark.parametrize("headers", [
    {"X-Actor": "creator"}, {"X-Creator": "true"}, {"User-Agent": "cctv-pipeline-cli"},
])
def test_r2_no_header_unlocks_creator_only_operations(
    client: TestClient, world: World, headers: dict[str, str],
) -> None:
    url, body = prepare(world, "shot")
    response = post(client, url, body, headers=headers)
    assert response.status_code == 403
    assert_not_approved(world.store, "shot")


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r2_the_same_approval_succeeds_through_the_cli(
    world: World, capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    _url, body = prepare(world, kind)
    code, out, err = run_cli(world.settings, capsys, CLI_APPROVE[kind], body, **cli_ids(kind))
    assert code == 0, err
    json.loads(out)
    (review,) = reviews_of(world.store)
    assert review.actor == "creator"
    assert_approved(world.store, kind, review.review_id)


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r2_with_the_setting_on_http_approval_succeeds_with_an_agent_review(
    permissive_client: TestClient, world: World, kind: str,
) -> None:
    url, body = prepare(world, kind)
    response = post(permissive_client, url, body)
    assert response.status_code == 200, response.text
    (review,) = reviews_of(world.store)
    assert review.actor == "agent"
    assert_approved(world.store, kind, review.review_id)


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r2_the_services_review_check_stays_the_authority(
    world: World, capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    """Setting off + CLI (creator) + an explicit *agent* Review: the setting is passed to
    the service, which refuses the agent Review."""
    prepare(world, kind)
    save_review(world.store, kind, actor="agent", review_id="opinion")
    _url, body = prepare_body_only(world, kind, review_id="opinion")
    code, _out, err = run_cli(world.settings, capsys, CLI_APPROVE[kind], body, **cli_ids(kind))
    assert code != 0
    assert "agent_may_approve" in err
    assert_not_approved(world.store, kind)


def prepare_body_only(world: World, kind: str, **extra: Any) -> tuple[str, dict[str, Any]]:
    """The body for an approval whose project state already exists."""
    store = world.store
    if kind == "cut":
        body: dict[str, Any] = {"expected_version": episode_token(store), "cut_version": 1}
    else:
        body = {"expected_version": shot_token(store)}
    body.update(extra)
    return APPROVE_URL[kind], body


def _manifest_names(client: TestClient) -> set[str]:
    response = client.get("/tool-manifest")
    assert response.status_code == 200
    return {tool["name"] for tool in response.json()["tools"]}


def test_r2_manifest_omits_creator_only_tools_when_the_setting_is_off(
    client: TestClient, app: Any,
) -> None:
    names = _manifest_names(client)
    assert not names & set(CREATOR_ONLY)
    direct = {tool["name"] for tool in manifest_module.build_manifest(app)["tools"]}
    assert not direct & set(CREATOR_ONLY)
    # Everything else this contract adds is still discoverable.
    assert (set(OPERATIONS) - set(CREATOR_ONLY)) <= names


def test_r2_manifest_lists_creator_only_tools_when_the_setting_is_on(
    permissive_client: TestClient,
) -> None:
    names = _manifest_names(permissive_client)
    assert set(OPERATIONS) <= names


@pytest.mark.parametrize("on", [False, True])
def test_r2_http_routes_and_openapi_exist_either_way(
    client: TestClient, permissive_client: TestClient, on: bool,
) -> None:
    paths = (permissive_client if on else client).app.openapi()["paths"]
    for name in CREATOR_ONLY:
        method, path = OPERATIONS[name]
        assert method in paths[path], name


def test_r2_cli_commands_always_exist() -> None:
    for name in CREATOR_ONLY:
        assert name in cli_module.app.commands or name.replace("_", "-") in cli_module.app.commands


def test_r2_mcp_tool_list_follows_the_manifest(client: TestClient) -> None:
    server = mcp.McpServer(AppTransport(client))
    response = server.handle(_rpc("tools/list"))
    assert response is not None
    names = {tool["name"] for tool in _tool_result(response)["tools"]}
    assert "pipeline_record_review" in names
    assert not {f"pipeline_{n}" for n in CREATOR_ONLY} & names


# --------------------------------------------------------------------------
# R3 - recording and listing Reviews
# --------------------------------------------------------------------------


@pytest.fixture
def targets(world: World) -> World:
    """A draft shot s1 (v1), a Timeline (token 1), a StyleBible and a Location."""
    add_shot(world.store)
    world.store.save(timeline(), expected_version=0)
    world.store.create(StyleBible(project_id=PID))
    world.store.create(Location(location_id="loc1"))
    return world


VALID_TARGETS = [
    ("shot", "s1", 1, EP),
    ("framing", "s1", 1, EP),
    ("episode_cut", EP, 1, EP),
    ("style_bible", PID, 1, None),
    ("location", "loc1", 1, None),
]


@pytest.mark.parametrize(("kind", "target", "version", "episode"), VALID_TARGETS)
def test_r3_record_review_stores_each_kind(
    client: TestClient, targets: World, kind: str, target: str, version: int,
    episode: str | None,
) -> None:
    response = post(client, REVIEWS_URL, review_body(
        target_kind=kind, target_id=target, target_version=version, episode_id=episode,
        approval_state="rejected", comment="needs work", waiver_text="because"))
    assert response.status_code in (200, 201), response.text
    body = response.json()
    assert (body["target_kind"], body["target_id"], body["target_version"],
            body["episode_id"]) == (kind, target, version, episode)
    assert body["approval_state"] == "rejected"
    assert body["comment"] == "needs work"
    assert body["waiver_text"] == "because"
    stored = targets.store.load(Review, body["review_id"])
    assert stored.actor == "agent"
    assert stored.rubric_scores == []
    # Recording an opinion never changes approval state.
    assert targets.store.load(Shot, "s1", episode_id=EP).status is ShotStatus.DRAFT


def test_r3_record_review_stores_an_h3_candidate_review(
    client: TestClient, settings: Settings,
) -> None:
    project_dir = settings.project_root / PID
    project_dir.mkdir()
    store, _root, candidate = _h3_setup(project_dir)
    response = post(client, REVIEWS_URL, review_body(
        target_kind="h3_candidate", target_id=H3_RUN,
        target_version=candidate.shot_version, episode_id=candidate.episode_id))
    assert response.status_code in (200, 201), response.text
    assert store.load(Review, response.json()["review_id"]).target_kind == "h3_candidate"


@pytest.mark.parametrize(("label", "overrides"), [
    ("extra field", {"surprise": 1}),
    ("bad approval_state", {"approval_state": "pending"}),
    ("draft approval_state", {"approval_state": "draft"}),
    ("zero version", {"target_version": 0}),
    ("negative version", {"target_version": -1}),
    ("unknown kind", {"target_kind": "nonsense"}),
    ("missing kind", {"target_kind": ...}),
    ("missing target_id", {"target_id": ...}),
    ("missing approval_state", {"approval_state": ...}),
    ("shot without episode", {"episode_id": None}),
    ("style_bible with episode", {"target_kind": "style_bible", "target_id": PID,
                                  "episode_id": EP}),
    ("location with episode", {"target_kind": "location", "target_id": "loc1",
                               "episode_id": EP}),
    ("cut episode differs from target", {"target_kind": "episode_cut", "target_id": EP,
                                         "episode_id": "other"}),
    ("non-string comment", {"comment": 5}),
])
def test_r3_invalid_recordings_are_rejected_and_write_nothing(
    client: TestClient, targets: World, label: str, overrides: dict[str, Any],
) -> None:
    before = tree_bytes(targets.root)
    response = post(client, REVIEWS_URL, review_body(**overrides))
    assert response.status_code == 422, f"{label}: {response.text}"
    assert tree_bytes(targets.root) == before


@pytest.mark.parametrize("overrides", [
    {"target_id": "ghost"},
    {"target_version": 2},
    {"episode_id": "other-episode"},
    {"target_kind": "episode_cut", "target_id": EP, "target_version": 2},
    {"target_kind": "location", "target_id": "ghost-loc", "episode_id": None},
])
def test_r3_a_missing_target_is_refused_and_named(
    client: TestClient, targets: World, overrides: dict[str, Any],
) -> None:
    before = tree_bytes(targets.root)
    response = post(client, REVIEWS_URL, review_body(**overrides))
    assert response.status_code == 422, response.text
    assert tree_bytes(targets.root) == before


def _seed_reviews(world: World) -> None:
    """Three scoped Reviews recorded out of order, plus one legacy Review."""
    store = world.store
    add_shot(store, "s1")
    add_shot(store, "s2")
    ensure_episode(store, "ep2")
    add_shot(store, "s1", "ep2")
    base = datetime(2026, 11, 1, tzinfo=UTC)
    record_review(store, target_kind="shot", target_id="s1", target_version=1, episode_id=EP,
                  actor="agent", approval_state=ShotStatus.APPROVED, review_id="b",
                  now=base + timedelta(hours=2))
    record_review(store, target_kind="shot", target_id="s2", target_version=1, episode_id=EP,
                  actor="creator", approval_state=ShotStatus.REJECTED, review_id="a",
                  now=base + timedelta(hours=2))
    record_review(store, target_kind="framing", target_id="s1", target_version=1,
                  episode_id="ep2", actor="agent", approval_state=ShotStatus.APPROVED,
                  review_id="z", now=base + timedelta(hours=1))
    store.save(Review(review_id="legacy", target_id="s1", target_version=1,
                      created_at=datetime(2026, 1, 1, tzinfo=UTC),
                      approval_state=ShotStatus.APPROVED), expected_version=0)


def _listed(client: TestClient, **params: str) -> list[str]:
    response = get(client, REVIEWS_URL, **params)
    assert response.status_code == 200, response.text
    return [item["review_id"] for item in response.json()]


def test_r3_list_reviews_is_empty_for_a_project_without_reviews(
    client: TestClient, world: World,
) -> None:
    response = get(client, REVIEWS_URL)
    assert response.status_code == 200
    assert response.json() == []


def test_r3_list_reviews_orders_by_created_at_then_review_id(
    client: TestClient, world: World,
) -> None:
    _seed_reviews(world)
    assert _listed(client) == ["legacy", "z", "a", "b"]


def test_r3_list_reviews_lists_legacy_reviews_unchanged(
    client: TestClient, world: World,
) -> None:
    _seed_reviews(world)
    legacy = next(r for r in get(client, REVIEWS_URL).json() if r["review_id"] == "legacy")
    assert legacy["target_kind"] is None
    assert legacy["actor"] is None
    assert legacy["episode_id"] is None
    assert legacy["target_id"] == "s1"


@pytest.mark.parametrize(("params", "expected"), [
    ({"target_kind": "shot"}, ["a", "b"]),
    ({"target_kind": "framing"}, ["z"]),
    ({"target_id": "s1"}, ["legacy", "z", "b"]),
    ({"target_id": "s2"}, ["a"]),
    ({"episode_id": EP}, ["a", "b"]),
    ({"episode_id": "ep2"}, ["z"]),
    ({"target_kind": "shot", "target_id": "s1"}, ["b"]),
    ({"target_kind": "shot", "target_id": "s1", "episode_id": EP}, ["b"]),
    ({"target_kind": "shot", "episode_id": "ep2"}, []),
    ({"target_id": "nope"}, []),
])
def test_r3_list_reviews_filters_are_exact_matches(
    client: TestClient, world: World, params: dict[str, str], expected: list[str],
) -> None:
    _seed_reviews(world)
    assert _listed(client, **params) == expected


def test_r3_list_reviews_is_not_creator_only(client: TestClient, world: World) -> None:
    assert get(client, REVIEWS_URL).status_code == 200


# --------------------------------------------------------------------------
# R4 - approvals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r4_an_explicit_review_id_is_used_and_no_review_is_recorded(
    world: World, capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    _url, body = prepare(world, kind)
    save_review(world.store, kind, actor="creator", review_id="gate")
    code, _out, err = run_cli(world.settings, capsys, CLI_APPROVE[kind],
                              {**body, "review_id": "gate"}, **cli_ids(kind))
    assert code == 0, err
    assert [r.review_id for r in reviews_of(world.store)] == ["gate"]
    assert_approved(world.store, kind, "gate")


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r4_an_explicit_agent_review_approves_over_http_with_the_setting(
    permissive_client: TestClient, world: World, kind: str,
) -> None:
    url, body = prepare(world, kind)
    save_review(world.store, kind, actor="agent", review_id="opinion")
    response = post(permissive_client, url, {**body, "review_id": "opinion"})
    assert response.status_code == 200, response.text
    assert_approved(world.store, kind, "opinion")


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r4_an_omitted_review_id_records_an_approving_review_first(
    permissive_client: TestClient, world: World, kind: str,
) -> None:
    url, body = prepare(world, kind, comment="ship it")
    response = post(permissive_client, url, body)
    assert response.status_code == 200, response.text
    (review,) = reviews_of(world.store)
    target_kind, target_id, episode_id = REVIEW_TARGET[kind]
    assert (review.target_kind, review.target_id, review.target_version,
            review.episode_id) == (target_kind, target_id, 1, episode_id)
    assert review.actor == "agent"
    assert review.comment == "ship it"
    assert review.approval_state is ShotStatus.APPROVED
    assert_approved(world.store, kind, review.review_id)


def test_r4_the_returned_entity_is_the_shot_or_the_episode(
    permissive_client: TestClient, world: World,
) -> None:
    url, body = prepare(world, "shot")
    shot = post(permissive_client, url, body).json()
    assert shot["shot_id"] == "s1" and shot["status"] == "approved"
    cut_world = world
    ensure_episode(cut_world.store)
    cut_world.store.save(timeline(), expected_version=0)
    episode = post(permissive_client, CUT_URL, {
        "expected_version": episode_token(cut_world.store), "cut_version": 1}).json()
    assert episode["episode_id"] == EP and episode["approved_cut_version"] == 1


def test_r4_framing_approval_returns_the_shot(permissive_client: TestClient,
                                              world: World) -> None:
    url, body = prepare(world, "framing")
    shot = post(permissive_client, url, body).json()
    assert shot["shot_id"] == "s1" and shot["framing_status"] == "approved"


def test_r4_approve_cut_uses_cut_version(permissive_client: TestClient, world: World) -> None:
    ensure_episode(world.store)
    world.store.save(timeline(3.0), expected_version=0)
    world.store.save(timeline(4.0), expected_version=1)
    body = {"expected_version": episode_token(world.store), "cut_version": 2}
    response = post(permissive_client, CUT_URL, body)
    assert response.status_code == 200, response.text
    (review,) = reviews_of(world.store)
    assert (review.target_kind, review.target_version) == ("episode_cut", 2)
    assert_approved(world.store, "cut", review.review_id, cut_version=2)


def test_r4_approve_cut_for_a_cut_version_that_does_not_exist_fails(
    permissive_client: TestClient, world: World,
) -> None:
    ensure_episode(world.store)
    world.store.save(timeline(), expected_version=0)
    response = post(permissive_client, CUT_URL, {
        "expected_version": episode_token(world.store), "cut_version": 5})
    assert response.status_code in (404, 422), response.text
    assert world.store.load(Episode, EP).approved_cut_version is None


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r4_a_failing_approval_leaves_the_review_and_names_it_over_http(
    permissive_client: TestClient, world: World, kind: str,
) -> None:
    if kind == "cut":
        url, body = prepare(world, "cut", draft_blocker=True)
    else:
        add_shot(world.store)  # draft: not in review, no pending framing panel
        url = APPROVE_URL[kind]
        body = {"expected_version": shot_token(world.store)}
    response = post(permissive_client, url, body)
    assert response.status_code >= 400
    (review,) = reviews_of(world.store)
    assert review.review_id in response.text
    assert review.actor == "agent"
    assert_not_approved(world.store, kind)


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r4_a_failing_approval_leaves_the_review_and_names_it_through_the_cli(
    world: World, capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    if kind == "cut":
        _url, body = prepare(world, "cut", draft_blocker=True)
    else:
        add_shot(world.store)
        body = {"expected_version": shot_token(world.store)}
    code, _out, err = run_cli(world.settings, capsys, CLI_APPROVE[kind], body, **cli_ids(kind))
    assert code != 0
    (review,) = reviews_of(world.store)
    assert review.review_id in err
    assert review.actor == "creator"


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r4_a_stale_expected_version_is_a_409(
    permissive_client: TestClient, world: World, kind: str,
) -> None:
    url, body = prepare(world, kind)
    body["expected_version"] += 7
    response = post(permissive_client, url, body)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "version_conflict"
    assert_not_approved(world.store, kind)


@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r4_an_unknown_explicit_review_is_refused(
    permissive_client: TestClient, world: World, kind: str,
) -> None:
    url, body = prepare(world, kind, review_id="does-not-exist")
    response = post(permissive_client, url, body)
    assert response.status_code == 422, response.text
    assert "does-not-exist" in response.text
    assert_not_approved(world.store, kind)


@pytest.mark.parametrize(("label", "overrides"), [
    ("zero expected_version", {"expected_version": 0}),
    ("missing expected_version", {"expected_version": ...}),
    ("extra field", {"actor": "creator"}),
    ("non-string review_id", {"review_id": 5}),
])
@pytest.mark.parametrize("kind", APPROVAL_KINDS)
def test_r4_request_validation(permissive_client: TestClient, world: World, kind: str,
                               label: str, overrides: dict[str, Any]) -> None:
    url, body = prepare(world, kind)
    body.update(overrides)
    body = {k: v for k, v in body.items() if v is not ...}
    before = tree_bytes(world.root)
    response = post(permissive_client, url, body)
    assert response.status_code == 422, f"{label}: {response.text}"
    assert tree_bytes(world.root) == before


@pytest.mark.parametrize("cut_version", [0, -1, "x", ...])
def test_r4_approve_cut_requires_a_positive_cut_version(
    permissive_client: TestClient, world: World, cut_version: Any,
) -> None:
    _url, body = prepare(world, "cut")
    body["cut_version"] = cut_version
    body = {k: v for k, v in body.items() if v is not ...}
    assert post(permissive_client, CUT_URL, body).status_code == 422


def test_r4_only_approve_cut_takes_a_cut_version(permissive_client: TestClient,
                                                 world: World) -> None:
    url, body = prepare(world, "shot")
    response = post(permissive_client, url, {**body, "cut_version": 1})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# R5 - recovery, profile revision, cancellation
# --------------------------------------------------------------------------


def _pending_plan(store: ProjectStore, location_id: str = "hall") -> str:
    before = Location(location_id=location_id)
    store.create(before)
    after = before.model_copy(update={**before.next_version(), "geometry_provenance": "revised"})
    plan = {
        "cascade_id": uuid.uuid4().hex,
        "entity_type": "Location", "before": before.model_dump(mode="json"),
        "after": after.model_dump(mode="json"), "expected_version": 1,
        "episode_ids": [], "affected": [],
    }
    store.record_cascade(plan)
    return plan["cascade_id"]


def test_r5_resume_invalidations_returns_the_resumed_cascade_ids(
    client: TestClient, world: World,
) -> None:
    first = _pending_plan(world.store, "hall")
    second = _pending_plan(world.store, "yard")
    url = f"/projects/{PID}/invalidations/resume"
    response = post(client, url, {})
    assert response.status_code == 200, response.text
    assert sorted(response.json()["resumed"]) == sorted([first, second])
    assert world.store.cascades(pending_only=True) == ()
    assert world.store.versions(Location, "hall") == [1, 2]
    assert post(client, url, {}).json() == {"resumed": []}


def test_r5_resume_invalidations_rejects_extra_fields(client: TestClient,
                                                      world: World) -> None:
    response = post(client, f"/projects/{PID}/invalidations/resume", {"cascade_id": "x"})
    assert response.status_code == 422


def test_r5_resume_invalidations_through_the_cli(
    world: World, capsys: pytest.CaptureFixture[str],
) -> None:
    cascade = _pending_plan(world.store)
    code, out, err = run_cli(world.settings, capsys, "resume-invalidations", project_id=PID)
    assert code == 0, err
    assert json.loads(out) == {"resumed": [cascade]}


def _camera_world(world: World) -> CameraProfile:
    for episode_id in ("ep1", "ep2"):
        ensure_episode(world.store, episode_id)
    for episode_id, shot_id, camera in (("ep1", "hit1", "cam1"), ("ep2", "hit2", "cam1"),
                                         ("ep2", "miss", "cam2")):
        add_shot(world.store, shot_id, episode_id, camera_id=camera)
        shot = world.store.load(Shot, shot_id, episode_id=episode_id)
        for status in (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING,
                       ShotStatus.REVIEW):
            update: dict[str, Any] = {"status": status}
            if status is ShotStatus.BUILDING:
                update["framing_status"] = FramingStatus.APPROVED
            shot = shot.model_copy(update=update)
            world.store.update(shot, expected_version=shot_token(world.store, shot_id, episode_id))
        world.store.save(Review(review_id=f"r-{shot_id}", target_id=shot_id, target_version=1,
                                created_at=NOW, approval_state=ShotStatus.APPROVED),
                         expected_version=0)
        shot = world.store.load(Shot, shot_id, episode_id=episode_id)
        world.store.update(
            shot.model_copy(update={"status": ShotStatus.APPROVED,
                                    "approval_review_id": f"r-{shot_id}"}),
            expected_version=shot_token(world.store, shot_id, episode_id))
    world.store.create(CameraProfile(camera_id="cam1", profile_name="CCTV"))
    return world.store.load(CameraProfile, "cam1")


def _camera_request(before: CameraProfile, **overrides: Any) -> dict[str, Any]:
    revised = before.model_copy(update={**before.next_version(), "field_of_view_mm": 16})
    body: dict[str, Any] = {"camera_profile": revised.model_dump(mode="json"),
                            "expected_version": 1}
    body.update(overrides)
    return body


def test_r5_revise_camera_profile_invalidates_shots_across_episodes(
    client: TestClient, world: World,
) -> None:
    before = _camera_world(world)
    response = post(client, f"/projects/{PID}/camera-profiles/cam1/revise",
                    _camera_request(before))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["invalidated"]) == {"hit1", "hit2"}
    assert body["camera_profile"]["camera_id"] == "cam1"
    assert body["camera_profile"]["version"] == 2
    assert world.store.load(CameraProfile, "cam1").field_of_view_mm == 16
    assert world.store.load(Shot, "hit2", episode_id="ep2").status is ShotStatus.STALE
    assert world.store.load(Shot, "miss", episode_id="ep2").status is ShotStatus.APPROVED


def test_r5_revise_camera_profile_path_mismatch_is_400(client: TestClient,
                                                        world: World) -> None:
    before = _camera_world(world)
    snapshot = tree_bytes(world.root)
    response = post(client, f"/projects/{PID}/camera-profiles/other/revise",
                    _camera_request(before))
    assert response.status_code == 400, response.text
    assert tree_bytes(world.root) == snapshot


def test_r5_revise_camera_profile_stale_expected_version_is_409(
    client: TestClient, world: World,
) -> None:
    before = _camera_world(world)
    response = post(client, f"/projects/{PID}/camera-profiles/cam1/revise",
                    _camera_request(before, expected_version=9))
    assert response.status_code == 409, response.text


def test_r5_revise_camera_profile_rejects_extra_fields(client: TestClient,
                                                        world: World) -> None:
    before = _camera_world(world)
    response = post(client, f"/projects/{PID}/camera-profiles/cam1/revise",
                    _camera_request(before, episode_ids=["ep1"]))
    assert response.status_code == 422


def _job(world: World, job_id: str, state: str, **fields: Any) -> bytes:
    root = world.store._root
    root.make_dir("state/jobs")
    append_job(root, Job(job_id=job_id, type="generate_media", state=state, **fields))
    return root.resolve(f"state/jobs/{job_id}.jsonl").read_bytes()


def _cancel(client: TestClient, job_id: str):
    return post(client, f"/projects/{PID}/jobs/{job_id}/cancel", {})


def test_r5_cancel_job_cancels_a_queued_job(client: TestClient, world: World) -> None:
    _job(world, "queued1", "queued")
    response = _cancel(client, "queued1")
    assert response.status_code == 200, response.text
    assert response.json() == {"job_id": "queued1", "state": "cancelled",
                               "cancellation_requested": True}
    # A second cancel finds a cancelled job: unchanged, nothing requested.
    again = _cancel(client, "queued1").json()
    assert again == {"job_id": "queued1", "state": "cancelled",
                     "cancellation_requested": False}


def test_r5_cancel_job_unknown_job_is_404(client: TestClient, world: World) -> None:
    assert _cancel(client, "no-such-job").status_code == 404


@pytest.mark.parametrize(("state", "fields"), [
    ("completed", {}), ("failed", {"error_kind": "internal"}), ("cancelled", {}),
])
def test_r5_cancel_job_returns_a_finished_job_unchanged(
    client: TestClient, world: World, state: str, fields: dict[str, Any],
) -> None:
    written = _job(world, "done1", state, **fields)
    response = _cancel(client, "done1")
    assert response.status_code == 200, response.text
    assert response.json() == {"job_id": "done1", "state": state,
                               "cancellation_requested": False}
    assert world.store._root.resolve("state/jobs/done1.jsonl").read_bytes() == written


def test_r5_cancel_job_rejects_extra_fields(client: TestClient, world: World) -> None:
    _job(world, "queued2", "queued")
    response = post(client, f"/projects/{PID}/jobs/queued2/cancel", {"force": True})
    assert response.status_code == 422


def test_r5_cancel_job_is_not_creator_only(client: TestClient, world: World) -> None:
    _job(world, "queued3", "queued")
    assert _cancel(client, "queued3").status_code == 200


# --------------------------------------------------------------------------
# R6 - reading outputs back
# --------------------------------------------------------------------------


def png_bytes(size: tuple[int, int], colour: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (20, 90, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


def make_clip(directory: Path, name: str, size: str, duration: int) -> bytes:
    assert FFMPEG is not None
    target = directory / name
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
         f"testsrc=size={size}:rate=10:duration={duration}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(target)],
        check=True, capture_output=True, timeout=60)
    return target.read_bytes()


@pytest.fixture(scope="module")
def small_clip(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    if FFMPEG is None:
        pytest.skip("ffmpeg is not on PATH")
    return make_clip(tmp_path_factory.mktemp("clips"), "small.mp4", "320x240", 2)


@pytest.fixture(scope="module")
def large_clip(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    if FFMPEG is None:
        pytest.skip("ffmpeg is not on PATH")
    return make_clip(tmp_path_factory.mktemp("clips-large"), "large.mp4", "1920x1080", 1)


def add_output(store: ProjectStore, output_id: str, kind: ShotOutputKind, relative: str,
               data: bytes, *, shot_id: str = "s1", episode_id: str = EP,
               record_version: int = 1) -> ShotOutput:
    store._root.write_bytes(relative, data)
    provenance: dict[str, Any] = {}
    if kind in (ShotOutputKind.MASTER, ShotOutputKind.PREVIEW, ShotOutputKind.GENERATED):
        provenance = {"engine_id": "BLENDER_EEVEE", "engine_build": "fixture-build"}
    elif kind is ShotOutputKind.DEGRADED:
        provenance = {"profile_semver": "1.0.0", "filter_graph_hash": "a" * 64,
                      "pinned_binaries": {"ffmpeg": "fixture"}}
    output = ShotOutput(
        output_id=output_id, episode_id=episode_id, shot_id=shot_id,
        record_version=record_version, kind=kind, relative_path=relative,
        content_sha256=hashlib.sha256(data).hexdigest(), **provenance)
    ledger = len(store.list_shot_outputs(shot_id, episode_id=episode_id))
    store.append_shot_output(output, expected_version=ledger)
    return output


def add_cut(store: ProjectStore, output_id: str, data: bytes) -> EpisodeOutput:
    ensure_episode(store)
    try:
        store.load(Timeline, EP)
    except RecordNotFound:
        store.save(timeline(), expected_version=0)
    head = store.load(Timeline, EP)
    relative = f"exports/{EP}/{output_id}.mp4"
    store._root.write_bytes(relative, data)
    output = EpisodeOutput(
        output_id=output_id, episode_id=EP, timeline_store_version=1,
        timeline_sha256=head.content_sha256, relative_path=relative,
        content_sha256=hashlib.sha256(data).hexdigest(), delivery_profile="dvr-2005",
        storage_resolution=(320, 240), output_fps=10, sample_aspect_ratio="1/1",
        ffmpeg_identity="fixture")
    store.append_episode_output(output, expected_version=len(store.list_episode_outputs(EP)))
    return output


def frame_url(output_id: str, episode_id: str = EP) -> str:
    return f"/projects/{PID}/episodes/{episode_id}/outputs/{output_id}/frame"


def decode(response: Any) -> Image.Image:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == "image/png"
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.content.startswith(PNG_MAGIC)
    return Image.open(io.BytesIO(response.content))


def test_r6_list_shot_outputs_and_its_filters(client: TestClient, world: World) -> None:
    store = world.store
    shot = add_shot(store)
    add_output(store, "p1o", ShotOutputKind.PREVIEW, "previews/a.png", b"a")
    add_output(store, "m1o", ShotOutputKind.MASTER, "renders/a.mkv", b"b")
    store.revise(shot.model_copy(update=shot.next_version()), expected_version=shot_token(store))
    add_output(store, "p2o", ShotOutputKind.PREVIEW, "previews/b.png", b"c", record_version=2)
    add_output(store, "s2o", ShotOutputKind.STILL, "stills/b.png", b"d", record_version=2)
    url = SHOT_BASE + "/outputs"

    def ids(**params: Any) -> list[str]:
        response = get(client, url, **params)
        assert response.status_code == 200, response.text
        return [item["output_id"] for item in response.json()]

    assert ids() == ["p1o", "m1o", "p2o", "s2o"]
    assert ids(record_version=1) == ["p1o", "m1o"]
    assert ids(record_version=2) == ["p2o", "s2o"]
    assert ids(kind="preview") == ["p1o", "p2o"]
    assert ids(kind="still") == ["s2o"]
    assert ids(kind="preview", record_version=2) == ["p2o"]
    assert ids(record_version=3) == []
    first = get(client, url).json()[0]
    assert first["relative_path"] == "previews/a.png"
    assert first["content_sha256"] == hashlib.sha256(b"a").hexdigest()
    assert get(client, url, kind="bogus").status_code == 422


def test_r6_list_cuts_returns_every_cut_in_ledger_order(client: TestClient,
                                                         world: World) -> None:
    url = f"/projects/{PID}/episodes/{EP}/cuts"
    ensure_episode(world.store)
    assert get(client, url).json() == []
    add_cut(world.store, "cut1", b"first cut")
    add_cut(world.store, "cut2", b"second cut")
    response = get(client, url)
    assert response.status_code == 200
    assert [c["output_id"] for c in response.json()] == ["cut1", "cut2"]
    assert response.json()[0]["content_sha256"] == hashlib.sha256(b"first cut").hexdigest()
    assert response.json()[0]["timeline_store_version"] == 1


def test_r6_list_cuts_unknown_episode_is_404(client: TestClient, world: World) -> None:
    """AMBIGUOUS: the contract does not name this case; an unknown episode is a 404."""
    assert get(client, f"/projects/{PID}/episodes/nope/cuts").status_code == 404


@pytest.mark.parametrize("kind", [ShotOutputKind.PREVIEW, ShotOutputKind.STILL,
                                  ShotOutputKind.GENERATED])
def test_r6_inspect_output_returns_an_image_output_as_png(
    client: TestClient, world: World, kind: ShotOutputKind,
) -> None:
    add_shot(world.store)
    add_output(world.store, "img", kind, "previews/img.png", png_bytes((64, 48)))
    before = tree_bytes(world.root)
    image = decode(get(client, frame_url("img")))
    assert image.size == (64, 48)  # never upscaled
    assert tree_bytes(world.root) == before


def test_r6_inspect_output_converts_a_jpeg_source_to_png(client: TestClient,
                                                         world: World) -> None:
    add_shot(world.store)
    add_output(world.store, "jpg", ShotOutputKind.STILL, "stills/frame.jpg", jpeg_bytes((80, 60)))
    assert decode(get(client, frame_url("jpg"))).size == (80, 60)


def test_r6_inspect_output_downscales_a_large_image_within_limits(
    client: TestClient, world: World,
) -> None:
    add_shot(world.store)
    add_output(world.store, "big", ShotOutputKind.PREVIEW, "previews/big.png",
               png_bytes((3000, 1500)))
    response = get(client, frame_url("big"))
    image = decode(response)
    assert max(image.size) <= 1024
    assert abs(image.size[0] / image.size[1] - 2.0) < 0.02
    assert len(response.content) <= 2 * 1024 * 1024


def test_r6_inspect_output_ignores_at_sec_for_images(client: TestClient, world: World) -> None:
    add_shot(world.store)
    add_output(world.store, "img", ShotOutputKind.PREVIEW, "previews/img.png", png_bytes((32, 32)))
    first = get(client, frame_url("img"))
    later = get(client, frame_url("img"), at_sec=5.0)
    assert later.status_code == 200
    assert later.content == first.content


@needs_ffmpeg
@pytest.mark.parametrize("kind", [ShotOutputKind.MASTER, ShotOutputKind.DEGRADED,
                                  ShotOutputKind.GENERATED])
def test_r6_inspect_output_extracts_a_frame_from_a_video_output(
    client: TestClient, world: World, small_clip: bytes, kind: ShotOutputKind,
) -> None:
    add_shot(world.store)
    add_output(world.store, "vid", kind, "renders/vid.mp4", small_clip)
    before = tree_bytes(world.root)
    default = get(client, frame_url("vid"))
    assert decode(default).size == (320, 240)
    early = get(client, frame_url("vid"), at_sec=0.0)
    later = get(client, frame_url("vid"), at_sec=1.5)
    assert decode(later).size == (320, 240)
    assert early.content == default.content
    assert later.content != early.content, "at_sec did not change the frame"
    assert tree_bytes(world.root) == before, "inspect_output wrote to the project"


@needs_ffmpeg
def test_r6_inspect_output_downscales_a_large_video_frame(
    client: TestClient, world: World, large_clip: bytes,
) -> None:
    add_shot(world.store)
    add_output(world.store, "hd", ShotOutputKind.MASTER, "renders/hd.mp4", large_clip)
    image = decode(get(client, frame_url("hd"), at_sec=0.5))
    assert max(image.size) <= 1024
    assert abs(image.size[0] / image.size[1] - 16 / 9) < 0.02


@needs_ffmpeg
def test_r6_inspect_output_extracts_a_frame_from_a_cut(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    add_cut(world.store, "cut1", small_clip)
    before = tree_bytes(world.root)
    assert decode(get(client, frame_url("cut1"), at_sec=1.0)).size == (320, 240)
    assert tree_bytes(world.root) == before


@needs_ffmpeg
def test_r6_inspect_output_past_the_end_is_422_naming_at_sec(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    add_shot(world.store)
    add_output(world.store, "vid", ShotOutputKind.MASTER, "renders/vid.mp4", small_clip)
    before = tree_bytes(world.root)
    response = get(client, frame_url("vid"), at_sec=60.0)
    assert response.status_code == 422, response.text
    assert "at_sec" in response.text
    assert "past the end" in response.text.lower()
    assert tree_bytes(world.root) == before


@needs_ffmpeg
@pytest.mark.parametrize("at_sec", ["-1", "-0.001", "nan", "inf", "-inf"])
def test_r6_inspect_output_rejects_negative_and_non_finite_at_sec(
    client: TestClient, world: World, small_clip: bytes, at_sec: str,
) -> None:
    add_shot(world.store)
    add_output(world.store, "vid", ShotOutputKind.MASTER, "renders/vid.mp4", small_clip)
    assert get(client, frame_url("vid"), at_sec=at_sec).status_code == 422


def test_r6_inspect_output_scene_has_no_frame_and_points_at_render_preview(
    client: TestClient, world: World,
) -> None:
    add_shot(world.store)
    add_output(world.store, "scn", ShotOutputKind.SCENE, "scenes/s1/scene.blend", b"BLENDER-v5")
    before = tree_bytes(world.root)
    response = get(client, frame_url("scn"))
    assert response.status_code == 422, response.text
    assert "render_preview" in response.text
    assert tree_bytes(world.root) == before


def test_r6_inspect_output_unknown_output_is_404(client: TestClient, world: World) -> None:
    add_shot(world.store)
    add_output(world.store, "img", ShotOutputKind.PREVIEW, "previews/img.png", png_bytes((8, 8)))
    assert get(client, frame_url("nope")).status_code == 404


def test_r6_inspect_output_does_not_cross_episodes(client: TestClient, world: World) -> None:
    add_shot(world.store, "s1", "ep2")
    add_output(world.store, "other", ShotOutputKind.PREVIEW, "previews/o.png",
               png_bytes((8, 8)), episode_id="ep2")
    assert get(client, frame_url("other", "ep2")).status_code == 200
    assert get(client, frame_url("other", EP)).status_code == 404


def test_r6_inspect_output_finds_outputs_of_any_shot_and_version(
    client: TestClient, world: World,
) -> None:
    store = world.store
    shot = add_shot(store, "s1")
    add_shot(store, "s2")
    add_output(store, "old", ShotOutputKind.PREVIEW, "previews/old.png", png_bytes((10, 10)))
    store.revise(shot.model_copy(update=shot.next_version()), expected_version=shot_token(store))
    add_output(store, "new", ShotOutputKind.PREVIEW, "previews/new.png", png_bytes((12, 12)),
               record_version=2)
    add_output(store, "second", ShotOutputKind.PREVIEW, "previews/sec.png", png_bytes((14, 14)),
               shot_id="s2")
    assert decode(get(client, frame_url("old"))).size == (10, 10)
    assert decode(get(client, frame_url("new"))).size == (12, 12)
    assert decode(get(client, frame_url("second"))).size == (14, 14)


def test_r6_inspect_output_refuses_tampered_bytes_as_corrupt_artifact(
    client: TestClient, world: World,
) -> None:
    """AMBIGUOUS: the store's ledger reader also verifies artifacts and may itself raise
    ``invalid_manifest`` before the operation can. The contract says CORRUPT_ARTIFACT."""
    add_shot(world.store)
    output = add_output(world.store, "img", ShotOutputKind.PREVIEW, "previews/img.png",
                        png_bytes((16, 16)))
    world.store._root.write_bytes(output.relative_path, png_bytes((16, 16), (1, 2, 3)))
    response = get(client, frame_url("img"))
    assert response.status_code >= 400
    assert response.json()["kind"] == "corrupt_artifact", response.text


@needs_ffmpeg
def test_r6_inspect_output_refuses_a_tampered_video(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    add_shot(world.store)
    output = add_output(world.store, "vid", ShotOutputKind.MASTER, "renders/vid.mp4", small_clip)
    world.store._root.write_bytes(output.relative_path, small_clip + b"\x00")
    response = get(client, frame_url("vid"))
    assert response.status_code >= 400
    assert response.json()["kind"] == "corrupt_artifact", response.text


def test_r6_inspect_output_over_mcp_is_an_image_block(client: TestClient, world: World) -> None:
    add_shot(world.store)
    add_output(world.store, "img", ShotOutputKind.PREVIEW, "previews/img.png", png_bytes((40, 30)))
    server = mcp.McpServer(AppTransport(client))
    response = server.handle(_rpc("tools/call", params={
        "name": "pipeline_inspect_output",
        "arguments": {"project_id": PID, "episode_id": EP, "output_id": "img"}}))
    assert response is not None
    result = _tool_result(response)
    assert result["isError"] is False
    (block,) = result["content"]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]).startswith(PNG_MAGIC)


@needs_ffmpeg
def test_r6_inspect_output_over_mcp_takes_at_sec(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    add_shot(world.store)
    add_output(world.store, "vid", ShotOutputKind.MASTER, "renders/vid.mp4", small_clip)
    server = mcp.McpServer(AppTransport(client))
    response = server.handle(_rpc("tools/call", params={
        "name": "pipeline_inspect_output",
        "arguments": {"project_id": PID, "episode_id": EP, "output_id": "vid", "at_sec": 1.0}}))
    assert response is not None
    (block,) = _tool_result(response)["content"]
    assert block["type"] == "image" and block["mimeType"] == "image/png"


@pytest.mark.parametrize("name", ["list_shot_outputs", "list_cuts", "inspect_output"])
def test_r6_read_operations_are_not_creator_only(client: TestClient, world: World,
                                                  name: str) -> None:
    assert name in _manifest_names(client)


# --------------------------------------------------------------------------
# R7 - descriptions
# --------------------------------------------------------------------------


@pytest.fixture
def openapi(app: Any) -> dict[str, Any]:
    return app.openapi()


def summary_of(openapi: dict[str, Any], name: str) -> str:
    method, path = OPERATIONS[name]
    return openapi["paths"][path][method].get("summary", "")


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_r7_every_new_operation_has_a_substantial_summary(openapi: dict[str, Any],
                                                          name: str) -> None:
    summary = summary_of(openapi, name)
    assert len(summary.split()) >= 15, f"{name}: {summary!r}"
    assert summary.lower().replace("_", " ") != name.replace("_", " ")
    assert "return" in summary.lower(), f"{name} does not say what it returns: {summary!r}"


@pytest.mark.parametrize("name", CREATOR_ONLY)
def test_r7_creator_only_operations_say_so_and_name_the_setting(
    openapi: dict[str, Any], name: str,
) -> None:
    summary = summary_of(openapi, name)
    assert "creator" in summary.lower()
    assert "approvals.agent_may_approve" in summary


def test_r7_inspect_output_says_which_output_kinds_give_a_frame(openapi: dict[str, Any]) -> None:
    summary = summary_of(openapi, "inspect_output").lower()
    assert "at_sec" in summary
    assert "preview" in summary
    assert "scene" in summary
    assert "render_preview" in summary


def test_r7_summaries_reach_the_cli_help() -> None:
    for name in OPERATIONS:
        command = cli_module.app.commands.get(name) or cli_module.app.commands[
            name.replace("_", "-")]
        assert command.help and len(command.help.split()) >= 15


# --------------------------------------------------------------------------
# Review-follow-up hardening
# --------------------------------------------------------------------------


@pytest.fixture
def ffmpeg_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every argv inspect runs through subprocess.run, calling through."""
    from orchestrator.service import inspect as inspect_module

    calls: list[list[str]] = []
    original = subprocess.run

    def recording(argv: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append([str(a) for a in argv])
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(inspect_module.subprocess, "run", recording)
    return calls


def test_hardening_an_hls_playlist_is_refused_and_ffmpeg_never_runs(
    client: TestClient, world: World, ffmpeg_calls: list[list[str]],
) -> None:
    add_shot(world.store)
    playlist = b"#EXTM3U\n#EXTINF:1,\n../../outside-the-project.mp4\n#EXT-X-ENDLIST\n"
    add_output(world.store, "hls", ShotOutputKind.GENERATED, "generated/hls/playlist.m3u8",
               playlist)
    before = tree_bytes(world.root)
    response = get(client, frame_url("hls"))
    assert response.status_code == 422, response.text
    assert ffmpeg_calls == []
    assert tree_bytes(world.root) == before


@needs_ffmpeg
def test_hardening_ffmpeg_is_restricted_to_file_protocol_and_mov_demuxer(
    client: TestClient, world: World, small_clip: bytes, ffmpeg_calls: list[list[str]],
) -> None:
    add_shot(world.store)
    add_output(world.store, "vid", ShotOutputKind.MASTER, "renders/vid.mp4", small_clip)
    assert decode(get(client, frame_url("vid"))).size == (320, 240)
    (argv,) = ffmpeg_calls
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    assert argv[argv.index("-f") + 1] == "mov"


def test_hardening_other_tampered_artifacts_do_not_break_inspection(
    client: TestClient, world: World,
) -> None:
    store = world.store
    add_shot(store, "s1")
    add_shot(store, "s2")
    add_output(store, "good", ShotOutputKind.PREVIEW, "previews/good.png", png_bytes((20, 20)))
    bad = add_output(store, "bad", ShotOutputKind.MASTER, "renders/bad.mkv", b"original",
                     shot_id="s2")
    store._root.write_bytes(bad.relative_path, b"tampered")
    assert decode(get(client, frame_url("good"))).size == (20, 20)
    response = get(client, frame_url("bad"))
    assert response.status_code >= 400
    assert response.json()["kind"] == "corrupt_artifact", response.text
    assert get(client, frame_url("never-recorded")).status_code == 404


@needs_ffmpeg
def test_hardening_tiny_at_sec_returns_a_frame_and_huge_is_past_the_end(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    add_shot(world.store)
    add_output(world.store, "vid", ShotOutputKind.MASTER, "renders/vid.mp4", small_clip)
    assert decode(get(client, frame_url("vid"), at_sec=1e-07)).size == (320, 240)
    response = get(client, frame_url("vid"), at_sec=1e12)
    assert response.status_code == 422, response.text
    assert "at_sec" in response.text


def test_hardening_an_image_with_too_many_pixels_is_corrupt_not_a_500(
    client: TestClient, world: World,
) -> None:
    buffer = io.BytesIO()
    Image.new("1", (10000, 6000)).save(buffer, format="PNG")
    assert len(buffer.getvalue()) < 2 * 1024 * 1024
    add_shot(world.store)
    add_output(world.store, "huge", ShotOutputKind.PREVIEW, "previews/huge.png",
               buffer.getvalue())
    response = get(client, frame_url("huge"))
    assert response.status_code != 500, response.text
    assert response.status_code >= 400
    assert response.json()["kind"] == "corrupt_artifact", response.text


@needs_ffmpeg
def test_hardening_a_1080p_video_frame_keeps_its_aspect_within_a_pixel(
    client: TestClient, world: World, large_clip: bytes,
) -> None:
    add_shot(world.store)
    add_output(world.store, "hd", ShotOutputKind.MASTER, "renders/hd.mp4", large_clip)
    width, height = decode(get(client, frame_url("hd"))).size
    assert max(width, height) <= 1024
    assert abs(height - round(width * 1080 / 1920)) <= 1


def test_hardening_list_reviews_reads_a_naive_created_at_as_utc(
    client: TestClient, world: World,
) -> None:
    store = world.store
    add_shot(store)
    store.save(Review(review_id="naive-early", target_id="s1", target_version=1,
                      created_at=datetime(2026, 11, 21, 11, 0),
                      approval_state=ShotStatus.APPROVED), expected_version=0)
    store.save(Review(review_id="naive-late", target_id="s1", target_version=1,
                      created_at=datetime(2026, 11, 21, 13, 0),
                      approval_state=ShotStatus.APPROVED), expected_version=0)
    record_review(store, target_kind="shot", target_id="s1", target_version=1, episode_id=EP,
                  actor="agent", approval_state=ShotStatus.APPROVED, review_id="aware",
                  now=NOW)
    assert _listed(client) == ["naive-early", "aware", "naive-late"]


def test_hardening_openapi_declares_image_png_for_inspect_output(app: Any) -> None:
    method, path = OPERATIONS["inspect_output"]
    ok = app.openapi()["paths"][path][method]["responses"]["200"]
    assert "image/png" in ok["content"]
