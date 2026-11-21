"""Cut assembly contract, part B: tests from ``orchestrator/api/CUT_ASSEMBLY_CONTRACT.md``.

Authored from the contract (plus the timeline and finishing-steps contracts it builds on)
alone, without reading the implementation. Where these tests and the implementation
disagree, the contract decides and the disagreement is a finding.

Interpretations the contract leaves open are marked ``AMBIGUOUS`` in the docstring of the
test that encodes them. Tests that run the real job path use real FFmpeg and ffprobe over
tiny ``testsrc`` clips and need no Blender, GPU or font.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from orchestrator.api import cli as cli_module
from orchestrator.adapters.blender.scene import RenderRequest, build_scene_plan, synthetic_interior
from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    PostProfile,
    PostProfileEncode,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore
from tests.service.test_timeline_assembly_contract import _approve_fixture_shot

from .conftest import TOKEN, TOKEN_HEADER, make_app

PID = "p1"
EP = "ep1"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
AUTH = {TOKEN_HEADER: TOKEN}
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_media = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg and ffprobe are not on PATH")

ASSEMBLE_URL = f"/projects/{PID}/episodes/{EP}/cut/assemble"
CUTS_URL = f"/projects/{PID}/episodes/{EP}/cuts"
JOBS_URL = f"/projects/{PID}/jobs"
WAIT_SECONDS = 120.0
TERMINAL = {"completed", "failed", "cancelled"}

# Delivery format of the small profile used by the real-ffmpeg tests.
SMALL = {"size": (320, 240), "fps": 10, "seconds": 1, "preset": "fast"}
# A deliberately slow delivery: 3 x 60 s of 640x480 through a single-threaded veryslow encode.
LONG = {"size": (640, 480), "fps": 30, "seconds": 60, "preset": "veryslow"}


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

    @property
    def jobs_dir(self) -> Path:
        return self.settings.project_root / PID / "state" / "jobs"


@pytest.fixture
def world(settings: Settings) -> World:
    base = ProjectRoot(settings.project_root)
    base.make_dir(PID)
    return World(settings, ProjectStore(ProjectRoot(base.resolve(PID))))


def make_clip(directory: Path, name: str, fmt: dict[str, Any], source: str = "testsrc") -> bytes:
    """Generate a clip inside a ProjectRoot, resolving its path through containment."""
    assert FFMPEG is not None
    width, height = fmt["size"]
    clips = ProjectRoot(directory)
    target = clips.resolve(name)
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
         f"{source}=size={width}x{height}:rate={fmt['fps']}:duration={fmt['seconds']}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(target)],
        check=True, capture_output=True, timeout=300)
    return clips.read_bytes(name)


@pytest.fixture(scope="module")
def small_clip(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    if FFMPEG is None or FFPROBE is None:
        pytest.skip("ffmpeg and ffprobe are not on PATH")
    return make_clip(tmp_path_factory.mktemp("cut-small"), "small.mp4", SMALL)


@pytest.fixture(scope="module")
def other_clip(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Same duration, fps and resolution as ``small_clip`` but a different picture."""
    if FFMPEG is None or FFPROBE is None:
        pytest.skip("ffmpeg and ffprobe are not on PATH")
    return make_clip(tmp_path_factory.mktemp("cut-other"), "other.mp4", SMALL, "testsrc2")


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    if FFMPEG is None or FFPROBE is None:
        pytest.skip("ffmpeg and ffprobe are not on PATH")
    return make_clip(tmp_path_factory.mktemp("cut-long"), "long.mp4", LONG)


def delivery_profile(fmt: dict[str, Any]) -> PostProfile:
    return PostProfile(
        name="delivery-test", semver="1.0.0", output_fps=fmt["fps"],
        storage_resolution=fmt["size"], sample_aspect_ratio="1/1",
        encode=PostProfileEncode(codec="libx264", preset=fmt["preset"], crf=24,
                                 pix_fmt="yuv420p", threads=1))


def shot_token(store: ProjectStore, shot_id: str) -> int:
    return store.load_with_version(Shot, shot_id, episode_id=EP)[1]


_PATH_TO = {
    ShotStatus.DRAFT: (),
    ShotStatus.PLANNED: (ShotStatus.PLANNED,),
    ShotStatus.BLOCKING: (ShotStatus.PLANNED, ShotStatus.BLOCKING),
    ShotStatus.BUILDING: (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING),
    ShotStatus.REVIEW: (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING,
                        ShotStatus.REVIEW),
    ShotStatus.REJECTED: (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING,
                          ShotStatus.REVIEW, ShotStatus.REJECTED),
    ShotStatus.FAILED: (ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING,
                        ShotStatus.FAILED),
}


def put_shot_in(store: ProjectStore, shot: Shot, status: ShotStatus) -> Shot:
    """Walk a freshly created draft Shot to ``status`` through legitimate store updates."""
    from orchestrator.domain.base import FramingStatus

    if status is ShotStatus.APPROVED or status is ShotStatus.STALE:
        shot = _approve_fixture_shot(store, shot)
        if status is ShotStatus.STALE:
            shot = shot.model_copy(update={"status": ShotStatus.STALE})
            store.update(shot, expected_version=shot_token(store, shot.shot_id))
        return store.load(Shot, shot.shot_id, episode_id=EP)
    for step in _PATH_TO[status]:
        changes: dict[str, Any] = {"status": step}
        if step is ShotStatus.BUILDING:
            changes["framing_status"] = FramingStatus.APPROVED
        shot = shot.model_copy(update=changes)
        store.update(shot, expected_version=shot_token(store, shot.shot_id))
    return store.load(Shot, shot.shot_id, episode_id=EP)


def add_scene_receipt(store: ProjectStore, shot: Shot) -> ShotOutput:
    """A real persisted scene plan (no licensed assets), as assembly's credit collection reads."""
    request = RenderRequest(
        shot_id=shot.shot_id, shot_version=shot.version, run_id=f"scene-{shot.shot_id}",
        degrade_seed=shot.degrade_seed, duration_sec=shot.duration_sec,
        camera_profile=CameraProfile(camera_id=f"camera-{shot.shot_id}", profile_name="fixture"),
        scene=synthetic_interior())
    relative = f"scenes/{shot.shot_id}/v{shot.version:04d}/cut-test/scene.json"
    data = json.dumps({"schema_version": 1, "plan": build_scene_plan(request)},
                      sort_keys=True).encode("utf-8")
    store._root.write_bytes(relative, data)
    output = ShotOutput(
        output_id=f"scene-{shot.shot_id}", episode_id=EP, shot_id=shot.shot_id,
        record_version=shot.version, kind=ShotOutputKind.SCENE, relative_path=relative,
        content_sha256=hashlib.sha256(data).hexdigest())
    ledger = len(store.list_shot_outputs(shot.shot_id, episode_id=EP))
    store.append_shot_output(output, expected_version=ledger)
    return output


def add_degraded_receipt(store: ProjectStore, shot: Shot, clip: bytes, fmt: dict[str, Any],
                         *, record_version: int | None = None, tag: str = "") -> ShotOutput:
    relative = f"delivered/{shot.shot_id}{tag}.mp4"
    store._root.write_bytes(relative, clip)
    output = ShotOutput(
        output_id=f"degraded-{shot.shot_id}{tag}", episode_id=EP, shot_id=shot.shot_id,
        record_version=record_version or shot.version, kind=ShotOutputKind.DEGRADED,
        relative_path=relative, content_sha256=hashlib.sha256(clip).hexdigest(),
        profile_semver="1.0.0", filter_graph_hash="a" * 64,
        pinned_binaries={"ffmpeg": "fixture-build"},
        frame_count=fmt["fps"] * fmt["seconds"], output_fps=fmt["fps"])
    ledger = len(store.list_shot_outputs(shot.shot_id, episode_id=EP))
    store.append_shot_output(output, expected_version=ledger)
    return output


def build_project(world: World, shots: dict[str, ShotStatus], clip: bytes | None,
                  fmt: dict[str, Any] = SMALL, *, without_output: tuple[str, ...] = (),
                  without_scene: tuple[str, ...] = ()) -> ProjectStore:
    """A project whose Episode lists ``shots`` in order, each in its status, each with a
    real degraded clip receipt (except ``without_output``). No Script, no CC-BY assets."""
    store = world.store
    profile = delivery_profile(fmt)
    store.save(Project(project_id=PID, title="Cut assembly", output_profile=profile.name),
               expected_version=0)
    store.save(profile, expected_version=0)
    store.save(Episode(episode_id=EP, project_id=PID, number=1, duration_target_sec=30,
                       shot_ids=tuple(shots)), expected_version=0)
    for shot_id, status in shots.items():
        created = Shot(shot_id=shot_id, episode_id=EP, post_profile=profile.name, duration_sec=1)
        store.create(created)
        shot = put_shot_in(store, created, status)
        if shot_id not in without_scene:
            add_scene_receipt(store, shot)
        if shot_id not in without_output:
            assert clip is not None
            add_degraded_receipt(store, shot, clip, fmt)
    return store


def post(client: TestClient, url: str, body: Any = None) -> Any:
    return client.post(url, json={} if body is None else body, headers=AUTH)


def get(client: TestClient, url: str) -> Any:
    return client.get(url, headers=AUTH)


def jobs(client: TestClient) -> list[dict[str, Any]]:
    response = get(client, JOBS_URL)
    assert response.status_code == 200, response.text
    return response.json()


def job_files(world: World) -> list[Path]:
    return sorted(world.jobs_dir.glob("*")) if world.jobs_dir.is_dir() else []


def wait_for_job(client: TestClient, job_id: str, timeout: float = WAIT_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = next((j for j in jobs(client) if j["job_id"] == job_id), None)
        if last is not None and last["state"] in TERMINAL:
            return last
        time.sleep(0.4)
    pytest.fail(f"job {job_id} did not finish within {timeout}s; last record: {last}")


def submit(client: TestClient, body: dict[str, Any] | None = None) -> str:
    response = post(client, ASSEMBLE_URL, body)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["job_id"]
    assert payload["state"] == "queued"
    return payload["job_id"]


def run_cli(settings: Settings, capsys: pytest.CaptureFixture[str], *extra: str,
            data: dict[str, Any] | None = None) -> tuple[int, str, str]:
    argv = ["assemble-cut", "--project-id", PID, "--episode-id", EP,
            "--data", json.dumps(data or {}), "--json", *extra]
    code = cli_module.main(argv, settings=settings)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def decode_png(response: Any) -> Image.Image:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == "image/png"
    assert response.content.startswith(PNG_MAGIC)
    return Image.open(io.BytesIO(response.content))


def cuts_dir(world: World) -> Path:
    return world.root / PID / "episodes" / EP / "outputs" / "cuts"


def files_named_after(world: World, run_id: str) -> list[Path]:
    directory = cuts_dir(world)
    return [p for p in directory.iterdir() if run_id in p.name] if directory.is_dir() else []


def quarantined(world: World) -> list[Path]:
    directory = world.root / PID / "quarantine"
    return [p for p in directory.rglob("*") if p.is_file()] if directory.is_dir() else []


def episode_token(store: ProjectStore) -> int:
    return store.load_with_version(Episode, EP)[1]


class LongEncode:
    """Record every child launched through the CPU supervisor, for long-encode tests."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.launched: list[tuple[list[str], subprocess.Popen[bytes]]] = []
        supervisor = supervisor_class()
        original = supervisor.launch
        launched = self.launched

        def recording_launch(this: Any, command: list[str], *args: Any, **kwargs: Any) -> Any:
            process = original(this, command, *args, **kwargs)
            launched.append((list(command), process))
            return process

        monkeypatch.setattr(supervisor, "launch", recording_launch)

    def encoders(self) -> list[subprocess.Popen[bytes]]:
        return [p for command, p in self.launched
                if Path(str(command[0])).name.lower().startswith("ffmpeg")]

    def wait_running(self) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not any(p.poll() is None for p in self.encoders()):
            time.sleep(0.2)
        assert any(p.poll() is None for p in self.encoders()), "no FFmpeg encode was launched"
        time.sleep(1.5)
        assert any(p.poll() is None for p in self.encoders()), "the encode was meant to run long"

    def assert_all_exited(self) -> None:
        assert self.encoders()
        for process in self.encoders():
            assert process.poll() is not None, "an FFmpeg child outlived its job"

    def kill_leftovers(self) -> None:
        for process in self.encoders():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def supervisor_class() -> Any:
    from orchestrator.jobs.process import CpuProcessSupervisor

    return CpuProcessSupervisor


# --------------------------------------------------------------------------
# R3/R4 - real end to end: HTTP -> job -> real TimelineAssembler -> list_cuts -> inspect
# --------------------------------------------------------------------------


@needs_media
def test_real_assembly_end_to_end_over_http(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    """Shots in review and approved states assemble through the real job path."""
    build_project(
        world, {"s1": ShotStatus.REVIEW, "s2": ShotStatus.APPROVED, "s3": ShotStatus.REVIEW},
        small_clip)

    job_id = submit(client)
    record = wait_for_job(client, job_id)
    assert record["state"] == "completed", record

    # The job record is episode-scoped, with no shot.
    assert record.get("shot_id") is None
    assert record["episode_id"] == EP
    assert record.get("run_id")
    assert job_id in [j["job_id"] for j in jobs(client)]

    # Exactly one cut, receipted against the file on disk.
    listed = get(client, CUTS_URL)
    assert listed.status_code == 200, listed.text
    cuts = listed.json()
    assert len(cuts) == 1
    cut = cuts[0]
    cut_bytes = world.store._root.resolve(cut["relative_path"]).read_bytes()
    assert cut_bytes
    assert hashlib.sha256(cut_bytes).hexdigest() == cut["content_sha256"]
    assert (cut["storage_resolution"], cut["output_fps"]) == (list(SMALL["size"]), SMALL["fps"])
    assert cut["delivery_profile"] == "delivery-test"

    # The same receipt is the ledger's, and the Timeline it is bound to was persisted.
    (stored,) = world.store.list_episode_outputs(EP)
    assert stored.output_id == cut["output_id"]
    assert stored.content_sha256 == cut["content_sha256"]

    # inspect_output reads a frame of the real cut back as a PNG.
    frame = get(client, f"/projects/{PID}/episodes/{EP}/outputs/{cut['output_id']}/frame")
    image = decode_png(frame)
    assert image.size[0] > 0 and image.size[1] > 0

    # A2: the cut is named after the job's run id and nothing was quarantined.
    assert cut["output_id"] == record["run_id"]
    assert quarantined(world) == []

    # Assembly is not approval (D5): no shot changed status, the cut is unapproved.
    assert world.store.load(Shot, "s1", episode_id=EP).status is ShotStatus.REVIEW
    assert world.store.load(Shot, "s2", episode_id=EP).status is ShotStatus.APPROVED
    assert world.store.load(Episode, EP).approved_cut_version is None


@needs_media
def test_real_assembly_records_no_shot_output(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    """The runner records no ShotOutput for an episode job."""
    store = build_project(world, {"s1": ShotStatus.APPROVED}, small_clip)
    before = {s: len(store.list_shot_outputs(s, episode_id=EP)) for s in ("s1",)}
    record = wait_for_job(client, submit(client))
    assert record["state"] == "completed", record
    assert {s: len(store.list_shot_outputs(s, episode_id=EP)) for s in before} == before


@needs_media
def test_a_second_assembly_appends_a_second_cut(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    """AMBIGUOUS: the contract says the cut is the newest entry of list_cuts, so repeated
    assembly appends (the ledger is append-only) rather than replacing."""
    build_project(world, {"s1": ShotStatus.REVIEW}, small_clip)
    assert wait_for_job(client, submit(client))["state"] == "completed"
    assert wait_for_job(client, submit(client))["state"] == "completed"
    ids = [c["output_id"] for c in get(client, CUTS_URL).json()]
    assert len(ids) == 2 and len(set(ids)) == 2


# --------------------------------------------------------------------------
# R2/R4 - cancellation stops a real FFmpeg
# --------------------------------------------------------------------------


@needs_media
def test_cancel_job_stops_a_real_ffmpeg_and_appends_no_cut(
    client: TestClient, world: World, long_clip: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = build_project(
        world, {"s1": ShotStatus.REVIEW, "s2": ShotStatus.REVIEW, "s3": ShotStatus.APPROVED},
        long_clip, LONG)
    encode = LongEncode(monkeypatch)
    job_id = submit(client)
    try:
        encode.wait_running()
        response = post(client, f"{JOBS_URL}/{job_id}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["cancellation_requested"] is True

        record = wait_for_job(client, job_id, timeout=60)
        assert record["state"] == "cancelled", record
        encode.assert_all_exited()
        assert len(store.list_episode_outputs(EP)) == 0
        assert get(client, CUTS_URL).json() == []
        # A2: the partial output is quarantined, not left among the cuts.
        run_id = record["run_id"]
        assert files_named_after(world, run_id) == []
        assert any(run_id in p.name for p in quarantined(world))
    finally:
        encode.kill_leftovers()


@needs_media
def test_a1_a_failure_mid_encode_leaves_no_ffmpeg_and_quarantines_partials(
    client: TestClient, world: World, long_clip: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The encoder raises while FFmpeg is alive: the job fails, no child survives."""
    from orchestrator.adapters.timeline.runner import TimelineAssembler

    store = build_project(
        world, {"s1": ShotStatus.REVIEW, "s2": ShotStatus.APPROVED}, long_clip, LONG)
    encode = LongEncode(monkeypatch)
    original_check = TimelineAssembler._check_running
    seen_alive_at: list[float] = []

    def failing_check(self: Any, deadline: float, context: str) -> None:
        if any(p.poll() is None for p in encode.encoders()):
            seen_alive_at.append(time.monotonic())
            if time.monotonic() - seen_alive_at[0] > 1.5:
                raise JobError(JobErrorKind.INTERNAL, "injected failure mid-encode")
        original_check(self, deadline, context)

    monkeypatch.setattr(TimelineAssembler, "_check_running", failing_check)
    job_id = submit(client)
    try:
        record = wait_for_job(client, job_id, timeout=90)
        assert record["state"] == "failed", record
        encode.assert_all_exited()
        assert len(store.list_episode_outputs(EP)) == 0
        run_id = record["run_id"]
        assert files_named_after(world, run_id) == []
        assert any(run_id in p.name for p in quarantined(world))
    finally:
        encode.kill_leftovers()


# --------------------------------------------------------------------------
# A3/A4 - approval revocation and the CLI's cut
# --------------------------------------------------------------------------


def approve_current_cut_through_cli(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    from orchestrator.domain.entities import Timeline

    store = world.store
    body = {"expected_version": episode_token(store),
            "cut_version": store.load_with_version(Timeline, EP)[1]}
    argv = ["approve-cut", "--project-id", PID, "--episode-id", EP,
            "--data", json.dumps(body), "--json"]
    code = cli_module.main(argv, settings=world.settings)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    episode = store.load(Episode, EP)
    assert episode.approved_cut_version == body["cut_version"]
    assert episode.approval_review_id


@needs_media
def test_a3_a_different_cut_of_the_same_timeline_revokes_the_cut_approval(
    client: TestClient, world: World, small_clip: bytes, other_clip: bytes,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A newer DEGRADED receipt for s1 at the same version changes the video, not the
    Timeline (same duration), so the second cut differs byte-wise."""
    store = build_project(world, {"s1": ShotStatus.APPROVED, "s2": ShotStatus.APPROVED},
                          small_clip)
    assert wait_for_job(client, submit(client))["state"] == "completed"
    approve_current_cut_through_cli(world, capsys)
    add_degraded_receipt(store, store.load(Shot, "s1", episode_id=EP), other_clip, SMALL,
                         tag="-v2")

    assert wait_for_job(client, submit(client))["state"] == "completed"
    first, second = store.list_episode_outputs(EP)
    assert first.timeline_sha256 == second.timeline_sha256
    assert first.content_sha256 != second.content_sha256
    episode = store.load(Episode, EP)
    assert episode.approved_cut_version is None
    assert episode.approval_review_id is None


@needs_media
def test_a3_reassembling_with_nothing_changed_keeps_the_approval(
    client: TestClient, world: World, small_clip: bytes, capsys: pytest.CaptureFixture[str],
) -> None:
    """A byte-identical cut keeps the approval. If two real encodes of the same inputs are
    not byte-identical here, that is a determinism finding, reported by this failure."""
    store = build_project(world, {"s1": ShotStatus.APPROVED, "s2": ShotStatus.APPROVED},
                          small_clip)
    assert wait_for_job(client, submit(client))["state"] == "completed"
    approve_current_cut_through_cli(world, capsys)
    approved = store.load(Episode, EP)

    assert wait_for_job(client, submit(client))["state"] == "completed"
    first, second = store.list_episode_outputs(EP)
    assert first.content_sha256 == second.content_sha256, (
        f"real FFmpeg encodes of identical inputs differ: {first.content_sha256} "
        f"vs {second.content_sha256}")
    episode = store.load(Episode, EP)
    assert episode.approved_cut_version == approved.approved_cut_version
    assert episode.approval_review_id == approved.approval_review_id


@needs_media
def test_a3_a_failed_reassembly_keeps_an_existing_approval(
    client: TestClient, world: World, small_clip: bytes, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.adapters.timeline.runner import TimelineAssembler

    store = build_project(world, {"s1": ShotStatus.APPROVED}, small_clip)
    assert wait_for_job(client, submit(client))["state"] == "completed"
    approve_current_cut_through_cli(world, capsys)
    approved = store.load(Episode, EP)

    def failing_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise JobError(JobErrorKind.INTERNAL, "injected encoder failure")

    monkeypatch.setattr(TimelineAssembler, "run", failing_run)
    record = wait_for_job(client, submit(client, {"credit_card_duration_sec": 7.0}))
    assert record["state"] == "failed", record
    episode = store.load(Episode, EP)
    assert episode.approved_cut_version == approved.approved_cut_version
    assert episode.approval_review_id == approved.approval_review_id
    assert len(store.list_episode_outputs(EP)) == 1
    assert files_named_after(world, record["run_id"]) == []


@needs_media
def test_a4_the_cli_reports_this_jobs_cut_not_the_newest(
    world: World, small_clip: bytes, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another cut lands in the ledger after the job's own; the CLI must not report it."""
    from orchestrator.adapters.timeline.job import CutAssemblyAdapter

    store = build_project(world, {"s1": ShotStatus.REVIEW}, small_clip)
    own: list[str] = []
    original = CutAssemblyAdapter.invoke

    def invoke_then_append_another(self: Any, request: Any, job: Any, **kwargs: Any) -> Any:
        receipt = original(self, request, job, **kwargs)
        own.append(receipt.output_id)
        later = receipt.model_copy(update={"output_id": "later-cut"})
        store.append_episode_output(
            later, expected_version=len(store.list_episode_outputs(EP)))
        return receipt

    monkeypatch.setattr(CutAssemblyAdapter, "invoke", invoke_then_append_another)
    code, out, err = run_cli(world.settings, capsys)
    assert code == 0, err
    assert [c.output_id for c in store.list_episode_outputs(EP)][-1] == "later-cut"
    assert own and own[0] != "later-cut"
    assert json.loads(out)["output_id"] == own[0]


# --------------------------------------------------------------------------
# R1 - which shots a cut may contain
# --------------------------------------------------------------------------


@needs_media
@pytest.mark.parametrize("status", [ShotStatus.REVIEW, ShotStatus.APPROVED])
def test_r1_review_and_approved_shots_are_accepted(
    client: TestClient, world: World, small_clip: bytes, status: ShotStatus,
) -> None:
    build_project(world, {"s1": status}, small_clip)
    record = wait_for_job(client, submit(client))
    assert record["state"] == "completed", record
    assert len(get(client, CUTS_URL).json()) == 1


@pytest.mark.parametrize("status", [
    ShotStatus.DRAFT, ShotStatus.PLANNED, ShotStatus.BLOCKING, ShotStatus.BUILDING,
    ShotStatus.STALE, ShotStatus.REJECTED, ShotStatus.FAILED,
])
def test_r1_other_statuses_are_refused_at_submission_naming_shot_and_status(
    client: TestClient, world: World, status: ShotStatus,
) -> None:
    """Each ineligible shot has a DEGRADED receipt, so only its status can be the reason.
    The clip bytes are never decoded because submission refuses first."""
    build_project(world, {"ok": ShotStatus.REVIEW, "blocked": status}, b"not decoded")
    response = post(client, ASSEMBLE_URL)
    assert response.status_code == 422, response.text
    assert "blocked" in response.text
    assert status.value in response.text.lower()
    assert jobs(client) == []
    assert job_files(world) == []


# --------------------------------------------------------------------------
# R3 - fail-fast 422s and request validation
# --------------------------------------------------------------------------


def test_missing_episode_is_404_and_creates_no_job(client: TestClient, world: World) -> None:
    world.store.save(Project(project_id=PID, title="Cut assembly"), expected_version=0)
    response = post(client, f"/projects/{PID}/episodes/ghost/cut/assemble")
    assert response.status_code == 404, response.text
    assert job_files(world) == []


def test_a_shot_without_a_degraded_output_is_422_and_creates_no_job(
    client: TestClient, world: World,
) -> None:
    build_project(world, {"s1": ShotStatus.REVIEW, "s2": ShotStatus.APPROVED}, b"x",
                  without_output=("s2",))
    response = post(client, ASSEMBLE_URL)
    assert response.status_code == 422, response.text
    assert "s2" in response.text
    assert jobs(client) == []
    assert job_files(world) == []


def test_a_degraded_output_for_an_older_version_does_not_count(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    """The receipt must be for the shot's *current* version: s1 has one for v1 only, then
    is revised to v2."""
    store = build_project(world, {"s1": ShotStatus.REVIEW}, small_clip)
    shot, token = store.load_with_version(Shot, "s1", episode_id=EP)
    revision = shot.model_copy(update={**shot.next_version(), "status": ShotStatus.DRAFT,
                                       "approval_review_id": None})
    store.revise(revision, expected_version=token)
    revised = store.load(Shot, "s1", episode_id=EP)
    assert revised.version == 2
    put_shot_in(store, revised, ShotStatus.REVIEW)
    response = post(client, ASSEMBLE_URL)
    assert response.status_code == 422, response.text
    assert "s1" in response.text
    assert "degraded" in response.text.lower()
    assert job_files(world) == []


def test_a_shot_without_a_scene_output_is_422_naming_the_shot_and_scene(
    client: TestClient, world: World,
) -> None:
    """Credit collection needs each shot's SCENE output; submission must refuse, not queue."""
    build_project(world, {"s1": ShotStatus.REVIEW, "s2": ShotStatus.APPROVED}, b"x",
                  without_scene=("s2",))
    response = post(client, ASSEMBLE_URL)
    assert response.status_code == 422, response.text
    assert "s2" in response.text
    assert "scene" in response.text.lower()
    assert jobs(client) == []
    assert job_files(world) == []


@pytest.mark.parametrize("body", [
    {"credit_card_duration_sec": 0},
    {"credit_card_duration_sec": -1.5},
    {"required_disk_gb": -1},
    {"surprise": 1},
    {"episode_id": EP},
    {"shot_id": "s1"},
])
def test_invalid_bodies_are_422_and_create_no_job(
    client: TestClient, world: World, body: dict[str, Any],
) -> None:
    build_project(world, {"s1": ShotStatus.REVIEW}, b"x")
    response = post(client, ASSEMBLE_URL, body)
    assert response.status_code == 422, response.text
    assert job_files(world) == []


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["credit_card_duration_sec", "required_disk_gb"])
def test_non_finite_numbers_are_422(
    client: TestClient, world: World, field: str, literal: str,
) -> None:
    build_project(world, {"s1": ShotStatus.REVIEW}, b"x")
    response = client.post(ASSEMBLE_URL, content=f'{{"{field}": {literal}}}',
                           headers={**AUTH, "Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert job_files(world) == []


@needs_media
def test_defaults_and_explicit_values_are_accepted(
    client: TestClient, world: World, small_clip: bytes,
) -> None:
    build_project(world, {"s1": ShotStatus.REVIEW}, small_clip)
    job_id = submit(client, {"credit_card_duration_sec": 2.5, "required_disk_gb": 0.0})
    assert wait_for_job(client, job_id)["state"] == "completed"


# --------------------------------------------------------------------------
# R3 - CLI
# --------------------------------------------------------------------------


@needs_media
def test_cli_waits_and_reports_the_new_cuts_output_id(
    world: World, small_clip: bytes, capsys: pytest.CaptureFixture[str],
) -> None:
    store = build_project(world, {"s1": ShotStatus.REVIEW, "s2": ShotStatus.APPROVED},
                          small_clip)
    code, out, err = run_cli(world.settings, capsys)
    assert code == 0, err
    result = json.loads(out)
    assert result["state"] == "completed"
    assert result["job_id"]
    (cut,) = store.list_episode_outputs(EP)
    assert result["output_id"] == cut.output_id
    assert hashlib.sha256(store._root.resolve(cut.relative_path).read_bytes()
                          ).hexdigest() == cut.content_sha256


def test_cli_refuses_no_wait_and_creates_no_job(
    world: World, capsys: pytest.CaptureFixture[str],
) -> None:
    build_project(world, {"s1": ShotStatus.REVIEW}, b"x")
    code, _out, err = run_cli(world.settings, capsys, "--no-wait")
    assert code != 0
    assert "no-wait" in err
    assert job_files(world) == []


def test_cli_reports_an_ineligible_shot_without_a_job(
    world: World, capsys: pytest.CaptureFixture[str],
) -> None:
    build_project(world, {"s1": ShotStatus.DRAFT}, b"x")
    code, _out, err = run_cli(world.settings, capsys)
    assert code != 0
    assert "s1" in err
    assert job_files(world) == []


# --------------------------------------------------------------------------
# R3 - operation registration and summary
# --------------------------------------------------------------------------


def test_assemble_cut_is_an_async_adapter_operation() -> None:
    from orchestrator.api.routes import ASYNC_ADAPTER_OPERATIONS

    assert "assemble_cut" in ASYNC_ADAPTER_OPERATIONS


def test_assemble_cut_is_in_the_manifest_without_the_creator_setting(
    client: TestClient,
) -> None:
    """Not creator-only: it is discoverable with ``agent_may_approve`` off."""
    response = client.get("/tool-manifest")
    assert response.status_code == 200
    tools = {tool["name"]: tool for tool in response.json()["tools"]}
    assert "assemble_cut" in tools
    description = tools["assemble_cut"]["description"]
    for name in ("list_cuts", "inspect_output", "approve_cut"):
        assert name in description, f"summary does not mention {name}: {description}"
    lowered = description.lower()
    assert "review" in lowered and "approved" in lowered and "degraded" in lowered


def test_assemble_cut_openapi_route() -> None:
    app = make_app(Settings(project_root=Path.cwd()))
    path = "/projects/{project_id}/episodes/{episode_id}/cut/assemble"
    assert "post" in app.openapi()["paths"][path]
    assert "202" in app.openapi()["paths"][path]["post"]["responses"]


# --------------------------------------------------------------------------
# R4 - settings
# --------------------------------------------------------------------------


def test_r4_cut_assembly_budget_defaults_to_900_seconds() -> None:
    assert Settings().budget.cut_assembly_wall_clock_seconds == 900.0


def test_r4_cut_assembly_budget_must_be_positive() -> None:
    from pydantic import ValidationError

    from orchestrator.platform.settings import BudgetSettings

    with pytest.raises(ValidationError):
        BudgetSettings(cut_assembly_wall_clock_seconds=0)


# --------------------------------------------------------------------------
# R2 - CpuProcessSupervisor
# --------------------------------------------------------------------------


def sleeper() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def supervisor() -> Iterator[Any]:
    instance = supervisor_class()()
    yield instance
    instance.stop_for(JobErrorKind.CANCELLED)


def test_r2_a_fresh_supervisor_is_not_stopped(supervisor: Any) -> None:
    assert supervisor.stopped() is False
    assert supervisor.stop_reason is None


def test_r2_launch_starts_the_process_with_the_arguments_given(supervisor: Any) -> None:
    process = supervisor.launch(
        [sys.executable, "-c", "import sys; sys.stdout.write('hello')"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        stdout, _ = process.communicate(timeout=30)
        assert stdout == b"hello"
    finally:
        supervisor.clear_process(process)


def test_r2_stop_for_terminates_a_real_sleeping_child(supervisor: Any) -> None:
    process = supervisor.launch(sleeper(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        assert process.poll() is None
        started = time.monotonic()
        assert supervisor.stop_for(JobErrorKind.CANCELLED) is True
        assert process.poll() is not None
        assert time.monotonic() - started < 15
        assert supervisor.stopped() is True
        assert supervisor.stop_reason is JobErrorKind.CANCELLED
    finally:
        if process.poll() is None:
            process.kill()


def test_r2_stop_for_is_idempotent(supervisor: Any) -> None:
    """AMBIGUOUS: the first recorded reason is kept on a repeated stop."""
    process = supervisor.launch(sleeper(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        assert supervisor.stop_for(JobErrorKind.BUDGET_EXCEEDED) is True
        assert supervisor.stop_for(JobErrorKind.CANCELLED) is True
        assert process.poll() is not None
        assert supervisor.stop_reason is JobErrorKind.BUDGET_EXCEEDED
    finally:
        if process.poll() is None:
            process.kill()


def test_r2_launch_after_stop_is_refused_with_the_stop_reason(supervisor: Any) -> None:
    """AMBIGUOUS: stopping with nothing running returns True (nothing is left to exit)."""
    assert supervisor.stop_for(JobErrorKind.CANCELLED) is True
    with pytest.raises(JobError) as caught:
        supervisor.launch(sleeper())
    assert caught.value.kind is JobErrorKind.CANCELLED


def test_r2_launch_after_a_budget_stop_raises_budget_exceeded(supervisor: Any) -> None:
    supervisor.stop_for(JobErrorKind.BUDGET_EXCEEDED)
    with pytest.raises(JobError) as caught:
        supervisor.launch(sleeper())
    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED


def test_r2_a_cleared_finished_process_needs_no_stop(supervisor: Any) -> None:
    process = supervisor.launch([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    process.wait(timeout=30)
    supervisor.clear_process(process)
    assert supervisor.stop_for(JobErrorKind.CANCELLED) is True
