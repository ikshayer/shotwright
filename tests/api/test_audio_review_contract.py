"""Audio review contract: tests from ``orchestrator/api/AUDIO_REVIEW_CONTRACT.md``.

Authored from the contract alone, without reading the implementation. Where these tests and
the implementation disagree, the contract decides and the disagreement is a finding.

Media is real (FFmpeg generates a clip with a tone, a video-only clip, WAV/FLAC/M4A/OGG
audio, a PNG); inference is not: the helper's injectable ``Backend`` is replaced by
``ManagedFakeBackend`` (tests/adapters/qwen/test_managed_contract.py), installed by wrapping
``QwenAdapter.__init__`` so that whatever ``orchestrator/adapters/qwen/job.py`` constructs
runs against the fake. The production queue composition is kept: ``_DefaultRuntime`` builds
the adapter and a real ``GpuLease`` holding the real ``GpuExecutionSlot``; only NVML, host
resources and the queue clock are substituted.

Production names and seams these tests assume (everything else is existing code):

* ``AdapterKind.AUDIO_REVIEW`` (``"qwen_audio_review"``) in ``service.runner._GPU_KINDS``
* ``Settings.qwen_review`` with the six R4 fields
* the three operations at the R1 paths; ``review_audio`` answers 202 ``{job_id, state}``
* job adapter built by ``_DefaultRuntime.adapter_for`` that constructs ``QwenAdapter(...)``
  (the fake backend is injected into that constructor) and exposes ``.worker``
* project report at ``<project>/audio_reviews/<audio_review_id>.json``; ``audio_review_id``
  equals ``Job.run_id``
* the stored request snapshot is ``AdapterRequest.payload`` as passed to ``JobRunner.submit``
  (read tolerantly: the contract does not name its keys)

Interpretations the contract leaves open are marked ``AMBIGUOUS`` in the docstring of the
test that encodes them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters.export.snapshot import plan_snapshot
import orchestrator.adapters.qwen.job as job_module
from orchestrator.adapters.qwen.core import QwenRequestError, QwenSetupError
from orchestrator.adapters.qwen.runner import QwenAdapter
from orchestrator.api import cli as cli_module
from orchestrator.api.app import create_app
from orchestrator.domain.entities import (
    Asset,
    Episode,
    EpisodeOutput,
    Project,
    Shot,
    ShotOutput,
    ShotOutputKind,
    Timeline,
    TimelineEntry,
)
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.jobs.gpu_slot import GpuExecutionSlot
from orchestrator.jobs.lease import MemoryReading
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings, SettingsError, parse_settings
from orchestrator.service.runner import _CPU_KINDS, _GPU_KINDS, _TAKE_KINDS, AdapterKind, JobRunner
from orchestrator.store.json_store import ProjectStore
from tests.adapters.qwen.test_managed_contract import ManagedFakeBackend, failure

from .conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER, find_key

# orchestrator.api re-exports the CLI as `app`, shadowing the module attribute.
app_module = importlib.import_module("orchestrator.api.app")

REPO = Path(__file__).resolve().parents[2]
_ORIGINAL_QWEN_INIT = QwenAdapter.__init__
_ORIGINAL_SUBMIT = JobRunner.submit
_REAL_JOB_QUEUE = JobQueue
PID = "p1"
EP = "ep1"
AUTH = {TOKEN_HEADER: TOKEN}
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_media = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg and ffprobe are not on PATH")
pytestmark = needs_media

BRIEF = "A dull fluorescent room tone; intentional hiss and narrow bandwidth are wanted."
REVIEWS_URL = f"/projects/{PID}/audio-reviews"
JOBS_URL = f"/projects/{PID}/jobs"
TERMINAL = {"completed", "failed", "cancelled"}
NOTICE_WORDS = ("unverified", "measure_output", "measure_asset", "listening")
SUMMARY_ROUTES = {
    "review_audio": ("post", "/projects/{project_id}/audio-reviews"),
    "get_audio_review": ("get", "/projects/{project_id}/audio-reviews/{audio_review_id}"),
    "list_audio_reviews": ("get", "/projects/{project_id}/audio-reviews"),
}


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------


def _ffmpeg(out: Path, *args: str) -> bytes:
    assert FFMPEG is not None
    subprocess.run([FFMPEG, "-v", "error", "-y", *args, str(out)], check=True,
                   capture_output=True, timeout=180)
    return out.read_bytes()


def _tone(seconds: float, rate: int = 16000) -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}"]


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    d = tmp_path_factory.mktemp("audio-review-media")
    made: dict[str, bytes] = {}
    made["av"] = _ffmpeg(d / "av.mp4", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4",
                         *_tone(4), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", "-shortest")
    made["video_only"] = _ffmpeg(d / "vo.mp4", "-f", "lavfi", "-i",
                                 "testsrc=size=160x120:rate=10:duration=2",
                                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")
    made["wav"] = _ffmpeg(d / "a.wav", *_tone(4), "-c:a", "pcm_s16le")
    made["long_wav"] = _ffmpeg(d / "long.wav", *_tone(100, 8000), "-c:a", "pcm_s16le")
    made["flac"] = _ffmpeg(d / "a.flac", *_tone(4), "-c:a", "flac")
    made["m4a"] = _ffmpeg(d / "a.m4a", *_tone(4), "-c:a", "aac")
    made["ogg"] = _ffmpeg(d / "a.ogg", *_tone(4), "-c:a", "flac", "-f", "ogg")
    made["png"] = _ffmpeg(d / "a.png", "-f", "lavfi", "-i", "color=c=gray:s=32x32:d=1",
                          "-frames:v", "1")
    return made


# --------------------------------------------------------------------------
# the environment
# --------------------------------------------------------------------------


class _Nvml:
    def device_memory(self) -> MemoryReading:
        return MemoryReading(used_mb=1200, total_mb=11940, free_mb=10740)

    def process_usage(self) -> list[Any]:
        return []


class _Host:
    def free_ram_gb(self) -> float:
        return 20.0

    def free_disk_gb(self, _path: Path) -> float:
        return 100.0


class FakeClock:
    """The queue's clock: constant unless a test advances it, so long batches cost no time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def to_jsonable(value: Any) -> Any:
    def default(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            return dataclasses.asdict(item)
        return str(item)

    return json.loads(json.dumps(value, default=default))


def leaf_values(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in leaf_values(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in leaf_values(value)]
    return [node]


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                 qwen: dict[str, Any] | None = None) -> None:
        base = tmp_path / "projects"
        base.mkdir(parents=True)
        values: dict[str, Any] = {"project_root": base}
        if qwen is not None:
            values["qwen_review"] = qwen
        self.settings = Settings(**values)
        base_root = ProjectRoot(base)
        base_root.make_dir(PID)
        self.root = ProjectRoot(base_root.resolve(PID))
        self.store = ProjectStore(self.root)
        self.clock = FakeClock()
        self.fake = ManagedFakeBackend(duration=None)
        self.submitted: list[tuple[str, Any]] = []
        self.gate_events: list[threading.Event] = []
        self._media: dict[str, bytes] = {}

        env = self
        slot_root = ProjectRoot(tmp_path / "slot-root") if (tmp_path / "slot-root").is_dir() else None
        if slot_root is None:
            (tmp_path / "slot-root").mkdir(parents=True)
            slot_root = ProjectRoot(tmp_path / "slot-root")
        self.slot_root = slot_root
        # A per-test repository root: the job stages into it and the helper writes its run
        # directory there, so nothing lands in the real repository's _work/.
        self.repo = tmp_path / "repo"
        self.repo.mkdir(parents=True)
        _CURRENT_REPO.append(self.repo)
        monkeypatch.setattr(job_module, "ROOT", self.repo)
        monkeypatch.setattr(app_module, "GpuExecutionSlot",
                            lambda _repo_root: GpuExecutionSlot(slot_root))
        monkeypatch.setattr(app_module, "PynvmlProbe", _Nvml)
        monkeypatch.setattr(app_module, "SystemHostResources", _Host)

        class ClockedQueue(_REAL_JOB_QUEUE):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs.setdefault("clock", env.clock)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(app_module, "JobQueue", ClockedQueue)

        def injected_init(self: QwenAdapter, root: Any = None, *, backend: Any = None) -> None:
            _ORIGINAL_QWEN_INIT(self, root, backend=env.fake)
            env.fake.root = self.root
            self.native = False

        monkeypatch.setattr(QwenAdapter, "__init__", injected_init)

        def spying_submit(self: JobRunner, project_id: str, request: Any) -> Any:
            env.submitted.append((project_id, request))
            return _ORIGINAL_SUBMIT(self, project_id, request)

        monkeypatch.setattr(JobRunner, "submit", spying_submit)

        self.store.save(Project(project_id=PID, title="Audio review"), expected_version=0)
        self.store.save(Episode(episode_id=EP, project_id=PID, number=1, duration_target_sec=30),
                        expected_version=0)
        self.store.create(Shot(shot_id="s1", episode_id=EP, duration_sec=4))
        self.app = create_app(self.settings, TOKEN)
        self.client = TestClient(self.app, base_url=LOOPBACK_BASE, raise_server_exceptions=False)
        self.client.__enter__()
        self.runner: JobRunner = self.app.state.context.job_runner
        self.runtime = self.runner.queue_for

    @contextlib.contextmanager
    def slot_held(self) -> Iterator[None]:
        """Hold this environment's GPU slot (never the repository's): the job queues behind it."""
        slot = GpuExecutionSlot(self.slot_root)
        slot.acquire(0)
        try:
            yield
        finally:
            slot.release()

    def close(self) -> None:
        for event in self.gate_events:
            event.set()
        def close_runner() -> None:
            with contextlib.suppress(Exception):
                self.runner.close()

        closer = threading.Thread(target=close_runner, daemon=True)
        closer.start()
        closer.join(10)
        self.fake.release_handles()
        self.client.__exit__(None, None, None)

    # fixtures -----------------------------------------------------------------------------

    def add_output(self, output_id: str, kind: ShotOutputKind, relative: str, data: bytes) -> ShotOutput:
        self.store._root.write_bytes(relative, data)
        provenance: dict[str, Any] = {}
        if kind in (ShotOutputKind.MASTER, ShotOutputKind.PREVIEW, ShotOutputKind.GENERATED):
            provenance = {"engine_id": "BLENDER_EEVEE", "engine_build": "fixture-build"}
        output = ShotOutput(
            output_id=output_id, episode_id=EP, shot_id="s1", record_version=1, kind=kind,
            relative_path=relative, content_sha256=hashlib.sha256(data).hexdigest(), **provenance)
        ledger = len(self.store.list_shot_outputs("s1", episode_id=EP))
        self.store.append_shot_output(output, expected_version=ledger)
        return output

    def add_asset(self, asset_id: str, data: bytes, relative: str, *, type_: str = "audio") -> Asset:
        self.store._root.write_bytes(relative, data)
        asset = Asset(asset_id=asset_id, type=type_, generated=True, license="CC0-1.0",
                      file_path=relative, checksum=hashlib.sha256(data).hexdigest())
        self.store.save(asset, expected_version=0)
        return asset

    def add_cut(self, output_id: str, data: bytes) -> EpisodeOutput:
        entries = [TimelineEntry(type="shot", ref="s1", start_sec=0, duration_sec=4)]
        timeline = Timeline(episode_id=EP, entries=entries)
        self.store.save(timeline, expected_version=0)
        token = self.store.load_with_version(Timeline, EP)[1]
        relative = f"episodes/{EP}/outputs/cuts/{output_id}.mp4"
        self.store._root.write_bytes(relative, data)
        receipt = EpisodeOutput(
            output_id=output_id, episode_id=EP, timeline_store_version=token,
            timeline_sha256=timeline.content_sha256, relative_path=relative,
            content_sha256=hashlib.sha256(data).hexdigest(), delivery_profile="test",
            storage_resolution=(160, 120), output_fps=10, sample_aspect_ratio="1/1",
            ffmpeg_identity="fixture")
        self.store.append_episode_output(receipt, expected_version=0)
        return receipt

    def seed(self, media: dict[str, bytes]) -> Env:
        self.add_output("out1", ShotOutputKind.MASTER, "renders/out1.mp4", media["av"])
        self.add_asset("aud1", media["wav"], "assets/audio/aud1.wav")
        return self

    # requests -----------------------------------------------------------------------------

    def post(self, body: Any, *, url: str = REVIEWS_URL) -> Any:
        return self.client.post(url, json=body, headers=AUTH)

    def post_raw(self, text: str) -> Any:
        return self.client.post(REVIEWS_URL, content=text,
                                headers={**AUTH, "content-type": "application/json"})

    def get(self, url: str, **params: Any) -> Any:
        return self.client.get(url, headers=AUTH, params=params or None)

    def jobs(self) -> list[dict[str, Any]]:
        response = self.get(JOBS_URL)
        assert response.status_code == 200, response.text
        return response.json()

    def job(self, job_id: str) -> dict[str, Any]:
        return next(job for job in self.jobs() if job["job_id"] == job_id)

    def wait(self, job_id: str, timeout: float = 90.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = next((job for job in self.jobs() if job["job_id"] == job_id), None)
            if last is not None and last["state"] in TERMINAL:
                return last
            time.sleep(0.1)
        pytest.fail(f"job {job_id} did not finish in {timeout}s; last record: {last}")

    def wait_until(self, predicate: Callable[[], bool], what: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        pytest.fail(f"timed out waiting for {what}")

    def submit(self, body: dict[str, Any]) -> str:
        response = self.post(body)
        assert response.status_code == 202, response.text
        payload = response.json()
        assert set(payload) >= {"job_id", "state"}
        assert payload["state"] == "queued"
        return payload["job_id"]

    def run_review(self, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        job = self.wait(self.submit(body))
        return job, job["run_id"]

    def record_path(self, review_id: str) -> Path:
        return self.root.path / "audio_reviews" / f"{review_id}.json"

    def record(self, review_id: str) -> dict[str, Any]:
        return json.loads(self.record_path(review_id).read_text(encoding="utf-8"))

    def projection(self, review_id: str) -> Any:
        return self.get(f"{REVIEWS_URL}/{review_id}")

    def list_reviews(self, **params: Any) -> Any:
        return self.get(REVIEWS_URL, **params)

    def worker(self) -> Any:
        return self.runtime.for_kind(PID, AdapterKind.AUDIO_REVIEW).lease.worker

    def tree(self) -> dict[str, bytes]:
        return {p.relative_to(self.root.path).as_posix(): p.read_bytes()
                for p in self.root.path.rglob("*") if p.is_file()}


@pytest.fixture
def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media: dict[str, bytes],
             ) -> Iterator[Callable[..., Env]]:
    made: list[Env] = []

    def build(**qwen: Any) -> Env:
        env = Env(tmp_path / f"env{len(made)}", monkeypatch, qwen=qwen or None).seed(media)
        made.append(env)
        return env

    yield build
    for env in reversed(made):
        env.close()


@pytest.fixture
def env(make_env: Callable[..., Env]) -> Env:
    return make_env()


def output_body(output_id: str = "out1", **over: Any) -> dict[str, Any]:
    return {"target": {"kind": "output", "episode_id": EP, "output_id": output_id},
            "brief": BRIEF, **over}


def asset_body(asset_id: str = "aud1", **over: Any) -> dict[str, Any]:
    return {"target": {"kind": "asset", "asset_id": asset_id}, "brief": BRIEF, **over}


_CURRENT_REPO: list[Path] = []


def staged_files(run_id: str) -> list[Path]:
    """The staged copy itself; an emptied directory left behind is not the copy."""
    directory = _CURRENT_REPO[-1] / "_work" / "qwen_review_inputs" / run_id
    return [p for p in directory.rglob("*") if p.is_file()] if directory.is_dir() else []


def assert_refused(env: Env, response: Any, status: int) -> None:
    assert response.status_code == status, response.text
    assert env.submitted == [], "a refused request still reached JobRunner.submit"
    assert env.jobs() == [], "a refused request left a job behind"
    assert not (env.root.path / "audio_reviews").exists()


# --------------------------------------------------------------------------
# R1 - request schema
# --------------------------------------------------------------------------


def _with(body: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**body, **changes}


@pytest.mark.parametrize("label,body", [
    ("unknown top-level key", _with(output_body(), unexpected=1)),
    ("missing target", {"brief": BRIEF}),
    ("missing brief", {"target": output_body()["target"]}),
    ("unknown target kind", _with(output_body(), target={"kind": "reference", "asset_id": "aud1"})),
    ("output target without output_id", _with(output_body(), target={"kind": "output", "episode_id": EP})),
    ("output target without episode_id", _with(output_body(), target={"kind": "output", "output_id": "out1"})),
    ("asset target without asset_id", _with(output_body(), target={"kind": "asset"})),
    ("target without kind", _with(output_body(), target={"asset_id": "aud1"})),
    ("empty brief", output_body(brief="")),
    ("blank brief", output_body(brief="  \n\t ")),
    ("brief over 4000 characters", output_body(brief="x" * 4001)),
    ("brief containing NUL", output_body(brief="hiss\u0000wanted")),
    ("brief not a string", output_body(brief=12)),
    ("unknown mode", output_body(mode="video")),
    ("negative start_sec", output_body(start_sec=-1)),
    ("zero duration_sec", output_body(duration_sec=0)),
    ("negative duration_sec", output_body(duration_sec=-2)),
    ("string start_sec", output_body(start_sec="soon")),
])
def test_r1_malformed_requests_are_422_and_queue_nothing(env: Env, label: str, body: dict[str, Any]) -> None:
    assert_refused(env, env.post(body), 422)


@pytest.mark.parametrize("fragment", [
    '"start_sec": NaN', '"start_sec": Infinity', '"duration_sec": NaN', '"duration_sec": Infinity',
    '"start_sec": -Infinity',
])
def test_r1_non_finite_interval_values_are_422(env: Env, fragment: str) -> None:
    text = ('{"target": {"kind": "output", "episode_id": "%s", "output_id": "out1"}, '
            '"brief": "%s", %s}' % (EP, BRIEF, fragment))
    assert_refused(env, env.post_raw(text), 422)


def test_r1_brief_limits_accept_the_edges(env: Env) -> None:
    for brief in ("x", "x" * 4000, "  padded by whitespace  "):
        response = env.post(output_body(brief=brief))
        assert response.status_code == 202, (len(brief), response.text)
    env.runner.drain()


def test_r1_defaults_are_audio_mode_from_zero_to_the_end(env: Env) -> None:
    job_id = env.submit(output_body())
    snapshot = to_jsonable(env.submitted[0][1].payload)
    assert "audio" in leaf_values(snapshot)
    env.wait(job_id)
    assert env.projection(env.job(job_id)["run_id"]).json()["mode"] == "audio"


@pytest.mark.parametrize("maker", [output_body, asset_body])
def test_r1_a_valid_request_is_202_with_job_id_and_queued_state(env: Env, maker: Callable[..., Any]) -> None:
    response = env.post(maker(start_sec=1.0, duration_sec=2.0))
    assert response.status_code == 202, response.text
    assert set(response.json()) >= {"job_id", "state"}
    assert response.json()["state"] == "queued"
    env.runner.drain()


def test_r1_audio_review_id_is_the_jobs_run_id(env: Env) -> None:
    job, review_id = env.run_review(output_body())
    assert job["run_id"] == review_id
    assert env.record_path(review_id).is_file()
    assert env.projection(review_id).status_code == 200


# --------------------------------------------------------------------------
# R2 - validation before queueing
# --------------------------------------------------------------------------


def test_r2_unknown_targets_are_404_and_queue_nothing(env: Env) -> None:
    assert_refused(env, env.post(output_body("ghost")), 404)
    assert_refused(env, env.post(asset_body("ghost")), 404)
    wrong_episode = _with(output_body(), target={"kind": "output", "episode_id": "ep9", "output_id": "out1"})
    assert_refused(env, env.post(wrong_episode), 404)


def test_r2_unknown_project_is_404(env: Env) -> None:
    response = env.post(output_body(), url="/projects/nope/audio-reviews")
    assert response.status_code == 404, response.text
    assert env.submitted == []


def test_r2_image_outputs_are_422(env: Env, media: dict[str, bytes]) -> None:
    env.add_output("img1", ShotOutputKind.GENERATED, "renders/img1.png", media["png"])
    assert_refused(env, env.post(output_body("img1")), 422)


def test_r2_scene_outputs_are_422(env: Env) -> None:
    env.add_output("scene1", ShotOutputKind.SCENE, "scenes/s1/v0001/scene.json", b'{"plan": {}}')
    assert_refused(env, env.post(output_body("scene1")), 422)


def test_r2_an_output_with_a_disallowed_extension_is_422(env: Env, media: dict[str, bytes]) -> None:
    env.add_output("odd", ShotOutputKind.MASTER, "renders/odd.txt", media["av"])
    assert_refused(env, env.post(output_body("odd")), 422)


def test_r2_an_output_without_an_audio_stream_is_422(env: Env, media: dict[str, bytes]) -> None:
    env.add_output("silent", ShotOutputKind.MASTER, "renders/silent.mp4", media["video_only"])
    assert_refused(env, env.post(output_body("silent")), 422)


def test_r2_joint_without_a_video_stream_is_422(env: Env) -> None:
    assert_refused(env, env.post(asset_body(mode="joint")), 422)


def test_r2_joint_on_a_target_with_video_is_accepted(env: Env) -> None:
    job, review_id = env.run_review(output_body(mode="joint"))
    assert job["state"] == "completed", job
    assert env.projection(review_id).json()["mode"] == "joint"
    assert env.fake.probes[0]["modality"] == "joint"


@pytest.mark.parametrize("label,changes", [
    ("start beyond the media", {"start_sec": 100}),
    ("start at the end of the media", {"start_sec": 4.5}),
    ("end beyond the media", {"start_sec": 3, "duration_sec": 5}),
    ("duration longer than the media", {"duration_sec": 60}),
])
def test_r2_an_interval_outside_the_media_is_422(env: Env, label: str, changes: dict[str, Any]) -> None:
    assert_refused(env, env.post(output_body(**changes)), 422)


def test_r2_an_interval_longer_than_max_interval_seconds_is_422(make_env: Callable[..., Env]) -> None:
    env = make_env(max_interval_seconds=1)
    assert_refused(env, env.post(output_body(start_sec=0, duration_sec=2)), 422)
    # No duration means "to the end": 4 s of media is longer than the 1 s ceiling.
    assert_refused(env, env.post(output_body()), 422)
    assert env.post(output_body(start_sec=1, duration_sec=1)).status_code == 202
    env.runner.drain()


def test_r2_a_tampered_output_is_refused_before_queueing(env: Env, media: dict[str, bytes]) -> None:
    env.store._root.write_bytes("renders/out1.mp4", media["av"] + b"\x00")
    response = env.post(output_body())
    assert 400 <= response.status_code < 500, response.text
    assert response.json()["kind"] in {"corrupt_artifact", "invalid_manifest"}
    assert env.submitted == [] and env.jobs() == []


@pytest.mark.parametrize("label,type_,relative", [
    ("non-audio asset type", "model", "assets/m.wav"),
    ("unsupported extension", "audio", "assets/odd.txt"),
])
def test_r2_assets_that_are_not_playable_audio_are_422(
        env: Env, media: dict[str, bytes], label: str, type_: str, relative: str) -> None:
    env.add_asset("bad", media["wav"], relative, type_=type_)
    assert_refused(env, env.post(asset_body("bad")), 422)


def test_r2_an_audio_asset_without_a_file_is_422(env: Env) -> None:
    env.store.save(Asset(asset_id="nofile", type="audio", generated=True, license="CC0-1.0"),
                   expected_version=0)
    assert_refused(env, env.post(asset_body("nofile")), 422)


def test_r2_an_audio_asset_whose_bytes_changed_is_refused(env: Env, media: dict[str, bytes]) -> None:
    env.store._root.write_bytes("assets/audio/aud1.wav", media["wav"] + b"\x00")
    response = env.post(asset_body())
    assert 400 <= response.status_code < 500, response.text
    assert response.json()["kind"] in {"corrupt_artifact", "invalid_manifest"}
    assert env.submitted == [] and env.jobs() == []


@pytest.mark.parametrize("name,extension,demuxer", [
    ("wav", "wav", "wav"), ("flac", "flac", "flac"), ("m4a", "m4a", "mov"), ("ogg", "ogg", "ogg"),
])
def test_r2_audio_asset_extensions_map_to_named_demuxers(
        env: Env, media: dict[str, bytes], name: str, extension: str, demuxer: str) -> None:
    env.add_asset("snd", media[name], f"assets/audio/snd.{extension}")
    job, _review_id = env.run_review(asset_body("snd"))
    assert job["state"] == "completed", job
    assert demuxer in leaf_values(to_jsonable(env.submitted[0][1].payload))
    assert env.fake.probes[0]["input_format"] == demuxer


def test_r2_a_cut_is_a_valid_target(env: Env, media: dict[str, bytes]) -> None:
    env.add_cut("cut-1", media["av"])
    body = _with(output_body("cut-1"), mode="joint")
    job, review_id = env.run_review(body)
    assert job["state"] == "completed", job
    assert job["episode_id"] == EP and job["shot_id"] is None
    assert env.projection(review_id).json()["target"]["output_id"] == "cut-1"


def test_r2_the_snapshot_carries_identity_verified_sha_demuxer_brief_actor_mode_and_interval(
        env: Env, media: dict[str, bytes]) -> None:
    env.submit(output_body(mode="joint", start_sec=1.0, duration_sec=2.0))
    payload = to_jsonable(env.submitted[0][1].payload)
    leaves = leaf_values(payload)
    assert hashlib.sha256(media["av"]).hexdigest() in leaves
    assert "mov" in leaves
    assert BRIEF in leaves
    assert "out1" in leaves and EP in leaves
    assert "joint" in leaves
    assert 1.0 in leaves and 2.0 in leaves
    assert set(find_key(payload, lambda key: key == "brief_actor")) == {"agent"}
    env.runner.drain()


def test_r2_an_asset_snapshot_carries_the_asset_identity_and_sha(env: Env, media: dict[str, bytes]) -> None:
    env.submit(asset_body())
    leaves = leaf_values(to_jsonable(env.submitted[0][1].payload))
    assert "aud1" in leaves and "wav" in leaves
    assert hashlib.sha256(media["wav"]).hexdigest() in leaves
    env.runner.drain()


# --------------------------------------------------------------------------
# R3 - the job
# --------------------------------------------------------------------------


def test_r3_adapter_kind_is_a_gpu_kind_and_never_a_cpu_or_take_kind() -> None:
    assert AdapterKind.AUDIO_REVIEW.value == "qwen_audio_review"
    assert AdapterKind("qwen_audio_review") is AdapterKind.AUDIO_REVIEW
    assert AdapterKind.AUDIO_REVIEW in _GPU_KINDS
    assert AdapterKind.AUDIO_REVIEW not in _CPU_KINDS
    assert AdapterKind.AUDIO_REVIEW not in _TAKE_KINDS


def test_r3_the_runtime_builds_the_adapter_with_a_worker_control(env: Env) -> None:
    adapter = env.runtime.adapter_for(env.root, AdapterKind.AUDIO_REVIEW)
    worker = adapter.worker
    assert worker.pid() == os.getpid()
    for name in ("request_queue_stop", "terminate", "unload", "respawn"):
        assert callable(getattr(worker, name)), name
    assert env.runtime.for_kind(PID, AdapterKind.AUDIO_REVIEW).lease.worker is worker


def test_r3_the_job_runs_under_the_production_lease_and_holds_the_execution_slot(env: Env) -> None:
    observed: dict[str, Any] = {}
    queue = env.runtime.for_kind(PID, AdapterKind.AUDIO_REVIEW)

    def inspect_lease(number: int, windows: Any) -> None:
        observed.setdefault("owned", queue.lease._owned)
        slot = GpuExecutionSlot(env.slot_root)
        try:
            slot.acquire(0)
        except Exception as exc:  # noqa: BLE001 - the slot being busy is the expected, asserted outcome
            observed.setdefault("slot_busy", type(exc).__name__)
        else:
            slot.release()
            observed.setdefault("slot_busy", None)

    env.fake.on_infer = inspect_lease
    job, _review_id = env.run_review(output_body())
    assert job["state"] == "completed", job
    assert observed["owned"] is True
    assert observed["slot_busy"] == "GpuSlotBusy", "the lease did not hold GpuExecutionSlot during the helper run"
    assert job["type"] == "qwen_audio_review"
    assert job["lease_holder_pid"] is None, "the lease was not released with the job"


def test_r3_the_job_queues_behind_a_held_gpu_slot(env: Env) -> None:
    with env.slot_held():
        job_id = env.submit(output_body())
        time.sleep(0.7)
        assert env.job(job_id)["state"] in {"queued"}, "the job ran while another GPU job held the slot"
        assert env.fake.probes == []
    assert env.wait(job_id)["state"] == "completed"


def test_r3_an_output_job_has_no_shot_but_its_episode_and_changes_no_shot_state(env: Env) -> None:
    store = env.store
    before = {
        "shot": store.load_with_version(Shot, "s1", episode_id=EP),
        "outputs": store.list_shot_outputs("s1", episode_id=EP),
        "episode": store.load_with_version(Episode, EP),
        "assets": sorted(str(p) for p in (env.root.path / "assets").rglob("*")),
        "reviews": store.list_reviews(),
    }
    job, _review_id = env.run_review(output_body())
    assert job["state"] == "completed", job
    assert job["shot_id"] is None
    assert job["episode_id"] == EP
    assert store.load_with_version(Shot, "s1", episode_id=EP) == before["shot"]
    assert store.list_shot_outputs("s1", episode_id=EP) == before["outputs"]
    assert store.load_with_version(Episode, EP) == before["episode"]
    assert sorted(str(p) for p in (env.root.path / "assets").rglob("*")) == before["assets"],         "an Asset was registered"
    assert store.list_reviews() == before["reviews"] == [], "a Review was created"
    budgets = json.loads((env.root.path / "jobs" / "budgets.json").read_text(encoding="utf-8"))
    assert EP in budgets["episode_seconds"], "GPU-seconds were not charged to the episode"
    assert job["job_id"] in budgets["shot_seconds"]


def test_r3_an_asset_job_has_neither_shot_nor_episode_and_is_charged_under_its_job_id(env: Env) -> None:
    job, _review_id = env.run_review(asset_body())
    assert job["state"] == "completed", job
    assert job["shot_id"] is None and job["episode_id"] is None
    budgets = json.loads((env.root.path / "jobs" / "budgets.json").read_text(encoding="utf-8"))
    assert job["job_id"] in budgets["shot_seconds"]
    assert EP not in budgets["episode_seconds"]


def test_r3_the_helper_reads_only_a_hash_checked_staged_copy_and_it_is_removed(
        env: Env, media: dict[str, bytes]) -> None:
    job, review_id = env.run_review(output_body())
    assert job["state"] == "completed", job
    probe = env.fake.probes[0]
    assert probe["media_rel"] == f"_work/qwen_review_inputs/{review_id}/source.mp4"
    assert probe["sha256"] == hashlib.sha256(media["av"]).hexdigest()
    assert probe["input_format"] == "mov"
    assert {p["media_rel"] for p in env.fake.prepares} == {probe["media_rel"]}
    assert {p["input_format"] for p in env.fake.prepares} == {"mov"}
    assert not staged_files(review_id), "the staged copy outlived the job"


def test_r3_a_source_changed_between_enqueue_and_run_is_corrupt_artifact_with_no_helper_read(
        env: Env, media: dict[str, bytes]) -> None:
    with env.slot_held():
        job_id = env.submit(output_body())
        run_id = env.job(job_id)["run_id"]
        time.sleep(0.5)
        env.store._root.write_bytes("renders/out1.mp4", media["av"] + b"tampered")
    job = env.wait(job_id)
    assert job["state"] == "failed"
    assert job["error_kind"] == "corrupt_artifact"
    assert env.fake.probes == [], "the helper read a target that no longer matched its snapshot"
    assert not staged_files(run_id)
    projection = env.projection(run_id)
    assert projection.status_code == 200, projection.text
    assert projection.json()["status"] == "failed"
    assert projection.json()["errors"][0]["kind"] == "corrupt_artifact"


def test_r3_the_staged_copy_is_removed_when_the_run_fails(env: Env) -> None:
    env.fake.raise_in_infer = RuntimeError("synthetic helper crash")
    job, review_id = env.run_review(output_body())
    assert job["state"] == "failed"
    assert job["error_kind"] == "internal"
    assert not staged_files(review_id)
    assert env.record(review_id)["status"] == "failed"


def test_r3_cancelling_a_queued_job_ends_cancelled_and_stages_nothing(env: Env) -> None:
    with env.slot_held():
        job_id = env.submit(output_body())
        run_id = env.job(job_id)["run_id"]
        time.sleep(0.5)
        response = env.post({}, url=f"{JOBS_URL}/{job_id}/cancel")
        assert response.status_code == 200, response.text
    job = env.wait(job_id)
    assert job["state"] == "cancelled"
    assert env.fake.probes == []
    assert not staged_files(run_id)


def test_r3_cancelling_during_inference_ends_cancelled_with_a_persisted_partial_report(
        env: Env, media: dict[str, bytes]) -> None:
    env.add_asset("long", media["long_wav"], "assets/audio/long.wav")
    entered, gate = threading.Event(), threading.Event()
    env.gate_events.append(gate)

    def pause(number: int, windows: Any) -> None:
        if number == 1:
            entered.set()
            assert gate.wait(60), "the test never released the batch"

    env.fake.on_infer = pause
    job_id = env.submit(asset_body("long"))
    assert entered.wait(60)
    run_id = env.job(job_id)["run_id"]
    assert env.post({}, url=f"{JOBS_URL}/{job_id}/cancel").status_code == 200
    gate.set()
    assert env.wait(job_id)["state"] == "cancelled"
    env.wait_until(env.record_path(run_id).is_file, "the partial report to be persisted")
    env.wait_until(lambda: not staged_files(run_id), "the staged copy to be removed")
    record = env.record(run_id)
    assert record["status"] == "cancelled"
    assert [w["status"] for w in record["windows"][:3]] == ["completed"] * 3
    assert env.fake.batches == [[1, 2, 3]], "a batch was launched after cancellation"
    assert env.projection(run_id).json()["status"] == "cancelled"


@pytest.mark.skipif(os.name != "nt", reason="relies on Windows refusing to delete a file held open")
def test_r3_a_failed_staged_copy_removal_is_recorded_in_the_report_errors(env: Env) -> None:
    """AMBIGUOUS: the contract says the copy is removed "after the report is persisted" yet the
    report is immutable and must record a removal failure. Only that the failure is recorded
    in the persisted report's errors, naming the staged copy or its removal, is asserted."""
    env.fake.hold_staged_open = True
    job, review_id = env.run_review(output_body())
    try:
        errors = env.record(review_id)["errors"]
        text = json.dumps(errors).lower()
        assert errors and any(word in text for word in ("staged", "qwen_review_inputs", "remov", "clean")), errors
    finally:
        env.fake.release_handles()
        shutil.rmtree(_CURRENT_REPO[-1] / "_work" / "qwen_review_inputs" / review_id, ignore_errors=True)
    assert job["state"] in TERMINAL


@pytest.mark.parametrize("exception,kind", [
    (QwenSetupError("isolated Qwen spike runtime is unavailable"), "missing_model_artifact"),
    (QwenRequestError("input must contain an audio stream"), "invalid_manifest"),
])
def test_r3_setup_and_request_errors_map_to_job_errors_and_leave_a_minimal_record(
        env: Env, exception: Exception, kind: str) -> None:
    env.fake.raise_in_probe = exception
    job, review_id = env.run_review(output_body())
    assert job["state"] == "failed"
    assert job["error_kind"] == kind
    record = env.record(review_id)
    assert record["status"] == "failed"
    assert record["errors"][0]["kind"] == kind
    projection = env.projection(review_id)
    assert projection.status_code == 200
    assert projection.json()["status"] == "failed"
    assert not staged_files(review_id)


@pytest.mark.parametrize("helper_kind,job_kind", [
    ("out_of_memory", "out_of_memory"), ("disk_exhausted", "disk_exhausted"),
    ("budget_exceeded", "budget_exceeded"), ("not_a_known_kind", "internal"),
])
def test_r3_a_failed_report_maps_its_first_error_kind_and_is_never_retried(
        env: Env, helper_kind: str, job_kind: str) -> None:
    env.fake.fail_batch[1] = (0, failure(helper_kind))
    job, review_id = env.run_review(output_body())
    assert job["state"] == "failed"
    assert job["error_kind"] == job_kind, (job["error_kind"], job.get("error_message"))
    assert job["attempts"] == 1
    assert len(env.fake.batches) == 1, "the failed batch was retried"
    assert env.record(review_id)["status"] == "failed"


def test_r3_truncated_windows_still_complete_the_job_and_are_flagged(env: Env) -> None:
    env.fake.cap = True
    job, review_id = env.run_review(output_body())
    assert job["state"] == "completed", job
    body = env.projection(review_id).json()
    assert body["has_truncated_responses"] is True
    assert {w["status"] for w in body["windows"]} == {"truncated"}
    assert body["status"] == "completed"


def test_r3_unverified_cleanup_fails_the_job_and_no_further_batch_runs(
        env: Env, media: dict[str, bytes]) -> None:
    env.add_asset("long", media["long_wav"], "assets/audio/long.wav")
    env.fake.cleanup_by_batch[1] = False
    job, review_id = env.run_review(asset_body("long"))
    assert job["state"] == "failed"
    assert env.fake.batches == [[1, 2, 3]]
    assert env.record(review_id)["status"] == "failed"
    with pytest.raises(JobError) as raised:
        env.worker().unload()
    assert raised.value.kind is JobErrorKind.WORKER_UNREACHABLE
    assert env.worker().terminate() is False


# --------------------------------------------------------------------------
# R4 - settings and budgets
# --------------------------------------------------------------------------


def test_r4_settings_defaults() -> None:
    section = Settings().qwen_review
    assert section.required_vram_gb == 8.0
    assert section.required_ram_gb == 6.0
    assert section.required_disk_gb == 8.0
    assert section.startup_allowance_seconds == 960
    assert section.wall_clock_seconds == 1800  # contract A1
    assert section.max_interval_seconds == 120  # contract A1


@pytest.mark.parametrize("bad", [
    {"required_vram_gb": 0}, {"required_ram_gb": -1}, {"required_disk_gb": 0},
    {"startup_allowance_seconds": 0}, {"wall_clock_seconds": -5}, {"max_interval_seconds": 0},
    {"wall_clock_seconds": float("nan")}, {"max_interval_seconds": float("inf")},
    {"required_vram_gb": True}, {"wall_clock_seconds": "2700"}, {"unknown_field": 1},
])
def test_r4_invalid_settings_are_rejected_at_load(bad: dict[str, Any]) -> None:
    """AMBIGUOUS: the contract says only "following indextts2's shape"; that section rejects
    non-positive, non-finite, boolean and string numbers and unknown keys, and so is mirrored."""
    with pytest.raises(SettingsError):
        parse_settings({"qwen_review": bad})


def test_r4_valid_overrides_are_accepted() -> None:
    settings = parse_settings({"qwen_review": {"wall_clock_seconds": 120, "max_interval_seconds": 30,
                                               "required_vram_gb": 6.5}})
    assert settings.qwen_review.wall_clock_seconds == 120
    assert settings.qwen_review.max_interval_seconds == 30
    assert settings.qwen_review.required_vram_gb == 6.5


def test_r4_the_job_requests_the_configured_reservations(make_env: Callable[..., Env]) -> None:
    default_job, _ = make_env().run_review(output_body())
    assert (default_job["required_vram_gb"], default_job["required_ram_gb"],
            default_job["required_disk_gb"]) == (8.0, 6.0, 8.0)
    tuned = make_env(required_vram_gb=3.0, required_ram_gb=2.0, required_disk_gb=1.0)
    tuned_job, _ = tuned.run_review(output_body())
    assert (tuned_job["required_vram_gb"], tuned_job["required_ram_gb"],
            tuned_job["required_disk_gb"]) == (3.0, 2.0, 1.0)


def test_r4_a_reservation_the_gpu_cannot_meet_is_refused_at_submission(make_env: Callable[..., Env]) -> None:
    env = make_env(required_vram_gb=64.0)
    response = env.post(output_body())
    assert response.status_code in {409, 422}, response.text
    assert response.json()["kind"] == "out_of_memory"
    assert env.jobs() == []


def test_r4_the_watchdog_does_not_fail_a_long_batch_that_is_within_its_limits(
        env: Env, media: dict[str, bytes]) -> None:
    """Two batches of 550 s with no progress inside either: the first gap is longer than the
    generic 150 s startup allowance, and progress per second after the first batch is under
    the generic 0.001 floor. The native supervisor's own limit is 900 s per batch."""
    env.add_asset("long", media["long_wav"], "assets/audio/long.wav")

    def long_batch(number: int, windows: Any) -> None:
        env.clock.advance(550)
        time.sleep(0.4)  # let the 10 ms watchdog poll observe the jump before progress moves

    env.fake.on_infer = long_batch
    job, review_id = env.run_review(asset_body("long"))
    assert job["state"] == "completed", (job["state"], job["error_kind"], job.get("error_message"))
    assert job["progress"] == pytest.approx(1.0)
    assert env.fake.batches == [[1, 2, 3], [4, 5]]
    assert env.record(review_id)["status"] == "completed"


def test_r4_wall_clock_exhaustion_requests_a_queue_stop_and_fails_budget_exceeded(
        make_env: Callable[..., Env], media: dict[str, bytes]) -> None:
    env = make_env(wall_clock_seconds=100)
    env.add_asset("long", media["long_wav"], "assets/audio/long.wav")
    worker = env.worker()
    stops: list[float] = []
    real_stop = worker.request_queue_stop

    def spying_stop() -> Any:
        stops.append(time.monotonic())
        return real_stop()

    worker.request_queue_stop = spying_stop

    def blow_the_budget(number: int, windows: Any) -> None:
        if number == 1:
            env.clock.advance(150)
            time.sleep(0.6)  # the watchdog polls every 10 ms and sees the deadline here

    env.fake.on_infer = blow_the_budget
    job, _review_id = env.run_review(asset_body("long"))
    assert job["state"] == "failed"
    assert job["error_kind"] == "budget_exceeded"
    assert stops, "the worker was not stopped through request_queue_stop"
    assert env.fake.batches == [[1, 2, 3]], "a batch was launched after the budget was exhausted"


# --------------------------------------------------------------------------
# R5 - the report
# --------------------------------------------------------------------------


def completed_review(env: Env, body: dict[str, Any] | None = None) -> str:
    job, review_id = env.run_review(body or output_body())
    assert job["state"] == "completed", (job["state"], job["error_kind"], job.get("error_message"))
    return review_id


def test_r5_the_project_report_is_written_with_every_listed_field(env: Env) -> None:
    job, review_id = env.run_review(output_body())
    assert job["state"] == "completed", job
    record = env.record(review_id)
    assert record["audio_review_id"] == review_id
    assert record["job_id"] == job["job_id"]
    assert record["advisory"] is True
    assert record["mode"] == "audio"
    assert record["brief_actor"] == "agent"
    assert record["status"] == "completed"
    assert record["target"]["kind"] == "output"
    assert hashlib.sha256((env.root.path / "renders" / "out1.mp4").read_bytes()).hexdigest() in \
        leaf_values(record["target"])
    assert "mov" in leaf_values(record["target"])
    assert record["helper_report_path"].startswith("_work/qwen_helper/")
    helper_path = _CURRENT_REPO[-1] / record["helper_report_path"]
    assert helper_path.exists() or helper_path.parent.exists(), record["helper_report_path"]
    assert record["windows"], "the full helper report is missing"
    created = datetime.datetime.fromisoformat(record["created_at"])
    assert created.tzinfo is not None and created.utcoffset() == timedelta(0), record["created_at"]


def test_r5_the_project_report_is_immutable(env: Env) -> None:
    sentinel = b"PRE-EXISTING RECORD"
    with env.slot_held():
        job_id = env.submit(output_body())
        run_id = env.job(job_id)["run_id"]
        env.root.write_bytes(f"audio_reviews/{run_id}.json", sentinel)
    job = env.wait(job_id)
    assert env.record_path(run_id).read_bytes() == sentinel, "an existing report was overwritten"
    assert job["state"] == "failed", "AMBIGUOUS: a report that cannot be persisted is a failed job"


def test_r5_get_projection_has_the_listed_keys_and_no_native_telemetry(
        env: Env, media: dict[str, bytes]) -> None:
    env.add_asset("long", media["long_wav"], "assets/audio/long.wav")
    env.fake.silent_windows = {1}
    review_id = completed_review(env, asset_body("long", duration_sec=30))
    response = env.projection(review_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"audio_review_id", "status", "target", "brief", "mode", "input_coverage",
                         "has_truncated_responses", "windows", "errors", "limitations", "advisory",
                         "notice"}
    assert body["audio_review_id"] == review_id
    assert body["status"] == "completed"
    assert body["advisory"] is True
    assert body["target"]["kind"] == "asset" and body["target"]["asset_id"] == "long"
    assert body["brief"]["text"] == BRIEF
    assert body["brief"]["sha256"] == hashlib.sha256(BRIEF.encode("utf-8")).hexdigest()
    assert body["brief"]["actor"] == "agent"
    assert body["errors"] == []
    assert body["limitations"]
    assert all(word in body["notice"].lower() for word in NOTICE_WORDS), body["notice"]
    forbidden = find_key(body, lambda key: "telemetry" in key.lower() or key in {
        "batches", "owned_process_tree_cleanup", "ram_available_samples", "artifacts", "request"})
    assert forbidden == [], "native telemetry leaked into the projection"
    first, second = body["windows"][0], body["windows"][1]
    for window in (first, second):
        assert {"index", "status", "raw_response", "termination_reason", "token_cap_reached",
                "audio_metrics"} <= set(window)
        assert any("input" in key for key in window) and any("source" in key for key in window)
    assert first.get("audio_interpretation_note"), "a digital-silence window needs its note"
    assert not second.get("audio_interpretation_note")


def test_r5_raw_responses_are_verbatim_unverified_model_text(env: Env) -> None:
    review_id = completed_review(env)
    windows = env.projection(review_id).json()["windows"]
    assert windows and all(
        w["raw_response"] == f"  raw window {w['index']}\n```unverified```\n" for w in windows)


def test_r5_the_projection_records_the_target_sha_and_demuxer(env: Env, media: dict[str, bytes]) -> None:
    review_id = completed_review(env, asset_body())
    target = env.projection(review_id).json()["target"]
    assert target["kind"] == "asset" and target["asset_id"] == "aud1"
    leaves = leaf_values(target)
    assert hashlib.sha256(media["wav"]).hexdigest() in leaves and "wav" in leaves


def test_r5_an_unknown_review_is_404(env: Env) -> None:
    assert env.projection("does-not-exist").status_code == 404


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b"", b'"a string"'])
def test_r5_an_unparsable_record_is_422_corrupt_artifact_never_a_partial_answer(
        env: Env, content: bytes) -> None:
    env.root.write_bytes("audio_reviews/broken.json", content)
    response = env.projection("broken")
    assert response.status_code == 422, response.text
    assert response.json()["kind"] == "corrupt_artifact"


def test_r5_the_list_is_newest_first_and_each_entry_has_its_four_fields(env: Env) -> None:
    ids = []
    for body in (output_body(), asset_body(), output_body()):
        ids.append(completed_review(env, body))
        time.sleep(0.05)
    response = env.list_reviews()
    assert response.status_code == 200, response.text
    entries = response.json()
    assert [entry["audio_review_id"] for entry in entries] == list(reversed(ids))
    assert all(set(entry) >= {"audio_review_id", "status", "target", "created_at"} for entry in entries)
    stamps = [entry["created_at"] for entry in entries]
    assert stamps == sorted(stamps, reverse=True)
    assert {entry["status"] for entry in entries} == {"completed"}


def test_r5_the_list_filters_by_target(env: Env) -> None:
    out_ids = [completed_review(env, output_body()) for _ in range(2)]
    asset_id = completed_review(env, asset_body())
    by_kind = env.list_reviews(target_kind="output").json()
    assert {e["audio_review_id"] for e in by_kind} == set(out_ids)
    by_target = env.list_reviews(target_kind="asset", target_id="aud1").json()
    assert [e["audio_review_id"] for e in by_target] == [asset_id]
    assert env.list_reviews(target_kind="output", target_id="nothing-here").json() == []


@pytest.mark.parametrize("params", [{"target_id": "out1"}, {"target_kind": "reference"}])
def test_r5_the_list_refuses_an_id_without_a_kind_and_an_unknown_kind(env: Env, params: dict[str, str]) -> None:
    assert env.list_reviews(**params).status_code == 422


def test_r5_an_empty_list_is_an_empty_array(env: Env) -> None:
    response = env.list_reviews()
    assert response.status_code == 200 and response.json() == []


def test_r5_failed_runs_are_listed_too(env: Env) -> None:
    env.fake.raise_in_probe = QwenSetupError("runtime missing")
    review_id = env.run_review(output_body())[1]
    entries = env.list_reviews().json()
    assert [(e["audio_review_id"], e["status"]) for e in entries] == [(review_id, "failed")]


def test_r5_reports_are_not_part_of_export_packages(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    store.save(Project(project_id="px", title="Export"), expected_version=0)
    store.save(Episode(episode_id=EP, project_id="px", number=1, duration_target_sec=30), expected_version=0)
    root.write_text("audio_reviews/r1.json", json.dumps({"audio_review_id": "r1", "advisory": True}))
    planned = [item.relative_path for item in plan_snapshot(store)]
    assert planned, "the snapshot planner returned nothing for a valid project"
    assert not [path for path in planned if path.startswith("audio_reviews")]


def test_r5_nothing_creates_a_review_or_changes_approvals_whatever_the_outcome(env: Env) -> None:
    before = env.store.list_reviews()
    completed_review(env)
    env.fake.fail_batch[len(env.fake.batches) + 1] = (0, failure("out_of_memory"))
    env.run_review(asset_body())
    assert env.store.list_reviews() == before == []


# --------------------------------------------------------------------------
# R6 - descriptions, tool manifest, CLI
# --------------------------------------------------------------------------


def summary_of(env: Env, name: str) -> str:
    method, path = SUMMARY_ROUTES[name]
    return env.app.openapi()["paths"][path][method].get("summary", "")


@pytest.mark.parametrize("name", sorted(SUMMARY_ROUTES))
def test_r6_operations_are_in_the_tool_manifest_and_not_creator_only(env: Env, name: str) -> None:
    response = env.client.get("/tool-manifest")
    assert response.status_code == 200
    assert name in {tool["name"] for tool in response.json()["tools"]}


@pytest.mark.parametrize("name", sorted(SUMMARY_ROUTES))
def test_r6_each_summary_meets_the_description_standard_and_reaches_the_cli_help(env: Env, name: str) -> None:
    assert len(summary_of(env, name).split()) >= 15, summary_of(env, name)
    command = cli_module.app.commands.get(name) or cli_module.app.commands[name.replace("_", "-")]
    assert command.help and len(command.help.split()) >= 15


def test_r6_the_review_summary_places_the_step_and_says_it_is_never_an_approval(env: Env) -> None:
    text = summary_of(env, "review_audio")
    lowered = text.lower()
    for fragment in ("measure_asset", "measure_output", "get_audio_review", "record_review"):
        assert fragment in text, f"review_audio does not mention {fragment}: {text!r}"
    assert "advisory" in lowered
    assert "approv" in lowered and ("never" in lowered or "not" in lowered)
    assert "brief" in lowered
    assert "minute" in lowered, "the duration (minutes) is not stated"
    assert "gpu" in lowered or "queue" in lowered


def test_r6_the_read_summaries_say_what_they_return(env: Env) -> None:
    got = summary_of(env, "get_audio_review").lower()
    assert "advisory" in got or "unverified" in got
    listed = summary_of(env, "list_audio_reviews").lower()
    assert "newest" in listed and "target_kind" in listed


def test_r6_the_cli_waits_and_prints_job_id_state_and_audio_review_id(
        env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_module.main(["review-audio", "--project-id", PID, "--data", json.dumps(output_body()),
                            "--json"], settings=env.settings)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    printed = json.loads(captured.out)
    assert set(printed) == {"job_id", "state", "audio_review_id"}, printed
    assert printed["state"] == "completed"
    job = next(j for j in env.jobs() if j["job_id"] == printed["job_id"])
    assert printed["audio_review_id"] == job["run_id"]
    assert env.record(printed["audio_review_id"])["brief_actor"] == "creator"
    assert env.projection(printed["audio_review_id"]).json()["brief"]["actor"] == "creator"
    assert not staged_files(printed["audio_review_id"])


def test_r6_the_cli_reports_a_failed_job_as_a_failure(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.fake.raise_in_probe = QwenSetupError("runtime missing")
    code = cli_module.main(["review-audio", "--project-id", PID, "--data", json.dumps(output_body()),
                            "--json"], settings=env.settings)
    captured = capsys.readouterr()
    assert code != 0
    assert json.loads(captured.err)["kind"] == "missing_model_artifact"


# --------------------------------------------------------------------------
# review adjudications V1, V2, V3
# --------------------------------------------------------------------------


def test_v1_unverified_cleanup_survives_a_failure_persisting_the_report(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("synthetic disk failure writing the report")

    monkeypatch.setattr(job_module, "persist_audio_review_report", refuse)
    env.fake.cleanup_by_batch[1] = False
    job, _review_id = env.run_review(output_body())
    assert job["state"] == "failed"
    worker = env.worker()
    assert worker.terminate() is False
    with pytest.raises(JobError) as raised:
        worker.unload()
    assert raised.value.kind is JobErrorKind.WORKER_UNREACHABLE
    batches_before = len(env.fake.batches)
    second = env.submit(output_body())
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and env.job(second)["state"] not in TERMINAL:
        time.sleep(0.2)
    assert env.job(second)["state"] != "completed", "a review ran after unverified cleanup"
    assert len(env.fake.batches) == batches_before


def test_v2_bytes_changed_after_staging_fail_corrupt_artifact_before_any_inference(env: Env) -> None:
    def tamper(record: dict[str, Any]) -> None:
        env.fake.root.write_bytes(record["media_rel"], b"changed after staging")

    env.fake.on_probe = tamper
    job, review_id = env.run_review(output_body())
    assert job["state"] == "failed"
    assert job["error_kind"] == "corrupt_artifact", (job["error_kind"], job.get("error_message"))
    assert env.fake.prepares == [] and env.fake.batches == []
    record = env.record(review_id)
    assert not [w for w in record.get("windows", []) if w.get("raw_response")], \
        "a response was bound to bytes that no longer matched the verified hash"


def _rewrite(env: Env, review_id: str, change: Callable[[dict[str, Any]], None]) -> None:
    record = env.record(review_id)
    change(record)
    env.record_path(review_id).write_text(json.dumps(record), encoding="utf-8")


def test_v3_real_helper_shaped_audio_metrics_are_accepted_and_returned_unchanged(env: Env) -> None:
    env.fake.silent_windows = set()
    review_id = completed_review(env)
    window = env.projection(review_id).json()["windows"][0]
    assert window["audio_metrics"] == env.fake.metrics_by_window[1]
    assert window["audio_metrics"]["channel_peak_dbfs"] == [-6.0, None]
    assert window["audio_metrics"]["stereo_correlation"] == 0.5


def test_v3_a_silent_window_with_null_channel_levels_is_accepted(env: Env) -> None:
    env.fake.silent_windows = {1}
    review_id = completed_review(env)
    window = env.projection(review_id).json()["windows"][0]
    assert window["audio_metrics"]["channel_rms_dbfs"] == [None, None]
    assert window["audio_metrics"]["stereo_correlation"] is None


def test_v3_a_dict_in_termination_reason_is_422_corrupt_artifact(env: Env) -> None:
    review_id = completed_review(env)
    _rewrite(env, review_id, lambda r: r["windows"][0].__setitem__("termination_reason", {"x": 1}))
    response = env.projection(review_id)
    assert response.status_code == 422, response.text
    assert response.json()["kind"] == "corrupt_artifact"


def test_v3_extra_keys_under_target_are_never_projected(env: Env) -> None:
    review_id = completed_review(env)
    _rewrite(env, review_id, lambda r: r["target"].__setitem__("injected", "evil"))
    response = env.projection(review_id)
    if response.status_code == 200:
        assert "injected" not in response.json()["target"]
    else:
        assert response.status_code == 422 and response.json()["kind"] == "corrupt_artifact"
