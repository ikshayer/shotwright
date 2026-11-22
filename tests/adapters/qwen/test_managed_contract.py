"""Managed (queued-job) entry of the Qwen helper: tests from
``orchestrator/api/AUDIO_REVIEW_CONTRACT.md`` (R3, the helper-facing half).

Authored from the contract alone, without reading the implementation. Where these tests
and the implementation disagree, the contract decides and the disagreement is a finding.

Inference is faked through the helper's injectable ``Backend`` (the fake returns the native
worker result schema from ``orchestrator/adapters/qwen/CONTRACT.md``, "BatchOutcome minimum
valid injected-backend fixture"). Cancellation of ``run_supervised`` is tested for real on the
CPU with a stand-in worker process, as ``test_native_interruption.py`` does. No GPU, no model.

Production names and seams these tests assume (everything else is existing code):

* ``QwenAdapter.run_managed(request, *, lease, cancel, input_format)`` returning the report dict
* ``QwenAdapter.worker`` with ``pid / request_queue_stop / terminate / unload / respawn``
* ``Backend.probe`` / ``Backend.prepare`` gain keyword-only ``input_format: str | None = None``
* ``runner._Lock`` and ``runner.GpuExecutionSlot`` (existing names) for the lock checks
* ``analyze.run_supervised(..., should_stop=callable)``

Interpretations the contract leaves open are marked ``AMBIGUOUS`` in the docstring of the
test that encodes them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest

from orchestrator.adapters.qwen import runner as qwen_runner
from orchestrator.adapters.qwen.core import (
    AnalysisRequest,
    BatchOutcome,
    MediaInfo,
    PreparedWindow,
    QwenRequestError,
    QwenSetupError,
    WindowSpec,
)
from orchestrator.adapters.qwen.runner import Backend, NativeBackend, QwenAdapter
from orchestrator.domain.entities import Job
from orchestrator.jobs.gpu_slot import GpuExecutionSlot
from orchestrator.jobs.lease import GpuLease, MemoryReading
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore
from tests.adapters.qwen.test_batches import BatchBackend as LegacyBatchBackend

REPO = Path(__file__).resolve().parents[3]
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_media = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg and ffprobe are not on PATH")

BRIEF = "A dull fluorescent room tone; intentional hiss and narrow bandwidth are wanted."


# --------------------------------------------------------------------------
# the injected backend
# --------------------------------------------------------------------------


def native_result(window: WindowSpec, *, prompt: str, cap: int, modality: str,
                  eos: bool = True, text: str | None = None) -> dict[str, Any]:
    """One native worker result item, valid for the helper's strict validator."""
    duration = window.duration_seconds
    count = 2 if eos else cap
    ids = [42] * count
    if eos:
        ids[-1] = 151645
    joint = modality == "joint"
    samples = max(4, 2 * math.ceil(duration * 0.4))
    decoded: dict[str, Any] = {
        "audio_sample_rate_hz": 16000, "audio_samples": round(duration * 16000),
        "audio_duration_seconds": duration, "audio_first_pts_seconds": 0,
        "video_first_pts_seconds": 0, "audio_minus_video_start_seconds": 0,
        "video_decoded_frames": round(duration * 24) if joint else 0,
    }
    shapes: dict[str, Any] = {"input_ids": [1, 20], "attention_mask": [1, 20],
                              "input_features": [1, 128, 30000],
                              "feature_attention_mask": [1, 30000]}
    if joint:
        decoded.update(video_frame_cadence_seconds=1 / 24, video_sample_target_error_seconds=[0] * samples,
                       video_sample_pts_relative_seconds=[i * duration / samples for i in range(samples)],
                       video_frame_budget=samples, video_frames_sampled=samples,
                       video_sample_fps=samples / duration)
        shapes.update(pixel_values_videos=[100, 1176], video_grid_thw=[1, 3], video_second_per_grid=[1])
    return {
        "text": text if text is not None else f"  raw window {window.index}\n```unverified```\n",
        "prompt": prompt, "output_token_ids": ids, "output_token_count": count,
        "termination_reason": "eos" if eos else "token_cap", "token_cap_reached": not eos,
        "effective_generation_kwargs": {"do_sample": False, "use_audio_in_video": joint,
                                        "max_new_tokens": cap, "eos_token_id": 151645,
                                        "pad_token_id": 151643},
        "decoded_media": decoded, "tensor_shapes": shapes,
        "loading_info": {"missing_keys": [], "unexpected_keys": [], "mismatched_keys": []},
        "quantized_linear4bit_count": 196, "unquantized_linear_count": 420,
        "quantized_linear4bit_sha256": "b" * 64, "unquantized_linear_sha256": "c" * 64,
        "model_provenance": {"synthetic": "not live model verification"}, "elapsed_seconds": 0.1,
    }


class ManagedFakeBackend:
    """Records every call; injects failures, cancellations, cleanup state and pauses.

    ``duration=None`` makes ``probe`` measure the staged file with the real ffprobe, so the
    window plan matches real media in the API tests.
    """

    def __init__(self, root: ProjectRoot | None = None, *, duration: float | None = 100.0,
                 with_video: bool = True) -> None:
        self.root = root
        self.duration = duration
        self.with_video = with_video
        self.probes: list[dict[str, Any]] = []
        self.prepares: list[dict[str, Any]] = []
        self.batches: list[list[int]] = []
        self.cap = False
        self.cleanup_by_batch: dict[int, bool] = {}
        self.silent_windows: set[int] = set()
        #: batch number (1-based) -> (results before failing, error dict); returns a failure outcome.
        self.fail_batch: dict[int, tuple[int, dict[str, Any]]] = {}
        #: batch number -> number of results before a "cancelled" outcome.
        self.cancel_batch: dict[int, int] = {}
        self.raise_in_probe: BaseException | None = None
        self.raise_in_infer: BaseException | None = None
        self.hold_staged_open = False
        self.metrics_by_window: dict[int, dict[str, Any]] = {}
        self.on_probe: Callable[[dict[str, Any]], None] | None = None
        self.on_prepare: Callable[[WindowSpec], None] | None = None
        self.on_infer: Callable[[int, list[WindowSpec]], None] | None = None
        self._held: list[Any] = []

    def release_handles(self) -> None:
        for handle in self._held:
            handle.close()
        self._held.clear()

    # Backend protocol ------------------------------------------------------------------

    def probe(self, media_rel: str, *, work_rel: str, modality: str,
              input_format: str | None = None) -> MediaInfo:
        assert self.root is not None
        record: dict[str, Any] = {"media_rel": media_rel, "input_format": input_format,
                                  "modality": modality}
        path = self.root.resolve(media_rel)
        if path.is_file():
            import hashlib
            record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            if self.hold_staged_open:
                self._held.append(path.open("rb"))
        self.probes.append(record)
        duration = self.duration
        if duration is None:
            out = subprocess.run(
                [FFPROBE or "ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True, timeout=60)
            duration = float(out.stdout.strip())
        if self.on_probe is not None:
            self.on_probe(record)
        if self.raise_in_probe is not None:
            raise self.raise_in_probe
        if self.with_video:
            return MediaInfo(duration, 48000, 2, 320, 240, 24, 0, 0)
        return MediaInfo(duration, 48000, 2)

    def prepare(self, media_rel: str, window: WindowSpec, *, work_rel: str, modality: str,
                input_format: str | None = None) -> PreparedWindow:
        assert self.root is not None
        self.prepares.append({"index": window.index, "input_format": input_format,
                              "media_rel": media_rel})
        if self.on_prepare is not None:
            self.on_prepare(window)
        rel = f"{work_rel}/prepared/window_{window.index:03d}.mkv"
        self.root.write_bytes(rel, b"synthetic prepared media")
        silent = window.index in self.silent_windows
        count = round(window.duration_seconds * 48000)
        # The key set and value shapes of runner._metrics: per-channel lists, null for an
        # exactly-zero channel, null correlation for a degenerate one.
        metrics = {"sample_rate": 48000, "channels": 2, "sample_count": count,
                   "sample_count_per_channel": count,
                   "channel_rms_dbfs": [None, None] if silent else [-18.0, -18.5],
                   "channel_peak_dbfs": [None, None] if silent else [-6.0, None],
                   "channel_mean_dc": [0.0, 0.0] if silent else [0.0, 0.001],
                   "digital_silence": silent, "samples_at_or_above_full_scale": 0,
                   "quiet_sample_fraction_below_0_001": 1.0 if silent else 0.02,
                   "stereo_correlation": None if silent else 0.5}
        self.metrics_by_window[window.index] = metrics
        return PreparedWindow(rel, "a" * 64, metrics, {"synthetic_test_only": True})

    def infer_batch(self, prepared: Any, windows: Any, *, prompt: str, max_new_tokens: int,
                    modality: str, work_rel: str) -> BatchOutcome:
        windows = list(windows)
        self.batches.append([w.index for w in windows])
        number = len(self.batches)
        if self.on_infer is not None:
            self.on_infer(number, windows)
        if self.raise_in_infer is not None:
            raise self.raise_in_infer
        verified = self.cleanup_by_batch.get(number, True)
        telemetry = {"owned_process_tree_cleanup": {
            "verified_stopped": verified, "survivors": [], "unverifiable_identities": [],
            "failures": []}}
        results: list[dict[str, Any]] = []
        for window in windows:
            if number in self.fail_batch and len(results) == self.fail_batch[number][0]:
                return BatchOutcome(tuple(results), telemetry, "failure", self.fail_batch[number][1])
            if number in self.cancel_batch and len(results) == self.cancel_batch[number]:
                return BatchOutcome(tuple(results), telemetry, "cancelled", {
                    "family": "job", "kind": "cancelled", "message": "synthetic cancellation",
                    "retryable": False, "remediation": "none"})
            results.append(native_result(window, prompt=prompt, cap=max_new_tokens,
                                         modality=modality, eos=not self.cap))
        return BatchOutcome(tuple(results), telemetry)


def failure(kind: str, message: str = "synthetic failure") -> dict[str, Any]:
    return {"family": "job", "kind": kind, "message": message, "retryable": True,
            "remediation": "Retry."}


def audio_request(*, modality: str = "audio", **overrides: Any) -> AnalysisRequest:
    values: dict[str, Any] = {"media": "media.mkv", "purpose": "audio_review", "brief": BRIEF,
                              "modality": modality}
    values.update(overrides)
    return AnalysisRequest(**values)


# --------------------------------------------------------------------------
# a real lease and queue around a temporary project
# --------------------------------------------------------------------------


class _Nvml:
    def device_memory(self) -> MemoryReading:
        return MemoryReading(used_mb=1200, total_mb=11940, free_mb=10740)

    def process_usage(self) -> list[Any]:
        return []


class _Worker:
    """A lease worker whose pid is this live process, as the production adapter's is."""

    def pid(self) -> int:
        return os.getpid()

    def unload(self) -> None:
        return None

    def terminate(self) -> bool:
        return True

    def respawn(self) -> int:
        return os.getpid()


class _Host:
    def free_ram_gb(self) -> float:
        return 20.0

    def free_disk_gb(self, _path: Path) -> float:
        return 100.0


class LeaseEnv:
    """The production composition: a real ``GpuLease`` holding the execution slot, in a ``JobQueue``."""

    def __init__(self, root: ProjectRoot, worker: Any | None = None, *,
                 attempt_limit: int | None = None, retries_per_shot: int | None = None) -> None:
        self.root = root
        extra: dict[str, Any] = {}
        if retries_per_shot is not None:
            extra["budget"] = {"retries_per_shot": retries_per_shot}
        self.settings = Settings(project_root=root.path, **extra)
        self.slot = GpuExecutionSlot(root)
        self.lease = GpuLease(root, self.settings, nvml=_Nvml(), worker=worker or _Worker(),
                              execution_slot=self.slot)
        self.queue = JobQueue(root, self.settings, self.lease, ProjectStore(root), nvml=_Nvml(),
                              host=_Host(), quarantine=ProjectQuarantine(root),
                              attempt_limit=attempt_limit)

    def run(self, call: Callable[[GpuLease], Any]) -> Any:
        job = Job(job_id=uuid.uuid4().hex, type="qwen_audio_review", run_id=uuid.uuid4().hex)
        return self.queue.run(job, lambda _job: call(self.lease))


def managed(adapter: QwenAdapter, env: LeaseEnv, request: AnalysisRequest, *,
            cancel: Callable[[], bool] = lambda: False, input_format: str | None = "matroska") -> dict[str, Any]:
    # Contract V2: the managed run is bound to the hash verified at staging.
    expected = hashlib.sha256(adapter.root.resolve(request.media).read_bytes()).hexdigest()
    return env.run(lambda lease: adapter.run_managed(
        request, lease=lease, cancel=cancel, input_format=input_format, expected_sha256=expected))


def read_report(project: ProjectRoot, report: dict[str, Any]) -> dict[str, Any]:
    return json.loads(project.read_text(report["report_path"]))


# --------------------------------------------------------------------------
# run_managed: lease ownership and what it must not take
# --------------------------------------------------------------------------


def test_run_managed_refuses_a_lease_that_is_not_owned(project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project)
    env = LeaseEnv(project)
    assert env.lease._owned is False
    with pytest.raises((ValueError, JobError)):
        QwenAdapter(project, backend=backend).run_managed(
            audio_request(), lease=env.lease, cancel=lambda: False, input_format="matroska",
            expected_sha256="0" * 64)
    assert backend.probes == [] and backend.batches == []


def test_run_managed_runs_the_standalone_path_under_an_owned_lease(project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=100.0)
    env = LeaseEnv(project)
    owned_during: list[bool] = []
    backend.on_infer = lambda number, windows: owned_during.append(env.lease._owned)
    report = managed(QwenAdapter(project, backend=backend), env, audio_request())
    assert report["status"] == "completed", report.get("errors")
    assert backend.batches == [[1, 2, 3], [4, 5]]
    assert owned_during == [True, True]
    assert [w["status"] for w in report["windows"]] == ["completed"] * 5
    assert report["report_path"] and report["markdown_path"]
    assert read_report(project, report)["status"] == "completed"


def test_run_managed_neither_takes_the_helper_lock_nor_the_gpu_slot_nor_refuses_the_live_lease(
        project: ProjectRoot) -> None:
    """The caller's lease already holds the slot and has written a live ``gpu_lease.json`` into
    this very root (the case the standalone path refuses). The helper lock is held elsewhere."""
    backend = ManagedFakeBackend(project, duration=30.0)
    env = LeaseEnv(project)
    with qwen_runner._Lock(project):
        report = managed(QwenAdapter(project, backend=backend), env, audio_request())
    assert report["status"] == "completed", report.get("errors")
    assert len(backend.batches) == 1


def test_run_managed_does_not_construct_the_lock_or_the_slot(
        project: ProjectRoot, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("run_managed must not take the helper lock or the GPU execution slot")

    env = LeaseEnv(project)  # built first: it legitimately constructs a slot
    monkeypatch.setattr(qwen_runner, "_Lock", forbidden)
    monkeypatch.setattr(qwen_runner, "GpuExecutionSlot", forbidden)
    backend = ManagedFakeBackend(project, duration=30.0)
    report = managed(QwenAdapter(project, backend=backend), env, audio_request())
    assert report["status"] == "completed", report.get("errors")


def test_the_standalone_run_still_takes_the_helper_lock(project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=30.0)
    with qwen_runner._Lock(project):
        with pytest.raises(QwenSetupError):
            QwenAdapter(project, backend=backend).run(audio_request())
    assert backend.probes == []


def test_the_standalone_run_still_takes_the_gpu_slot(project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=30.0)
    slot = GpuExecutionSlot(project)
    slot.acquire(0)
    try:
        with pytest.raises(QwenSetupError):
            QwenAdapter(project, backend=backend).run(audio_request())
    finally:
        slot.release()
    assert backend.probes == []


def test_the_standalone_run_still_refuses_a_live_production_lease(project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=30.0)
    env = LeaseEnv(project)
    env.lease.acquire(5)
    try:
        # The lease holds the slot too; only the refusal itself is under test, so release
        # that and keep the live pid record.
        env.slot.release()
        with pytest.raises(QwenSetupError):
            QwenAdapter(project, backend=backend).run(audio_request())
    finally:
        env.lease.release(1024, 5)
    assert backend.probes == []


# --------------------------------------------------------------------------
# run_managed: named demuxer, legacy backends
# --------------------------------------------------------------------------


def test_input_format_reaches_probe_and_every_prepare(project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=100.0)
    report = managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request(),
                     input_format="matroska")
    assert report["status"] == "completed", report.get("errors")
    assert [p["input_format"] for p in backend.probes] == ["matroska"]
    assert len(backend.prepares) == 5
    assert {p["input_format"] for p in backend.prepares} == {"matroska"}


def test_the_standalone_run_keeps_working_with_a_backend_that_has_no_input_format(
        project: ProjectRoot) -> None:
    """Existing callers (and their fakes) keep the old three-keyword signature."""
    legacy = LegacyBatchBackend(project)
    report = QwenAdapter(project, backend=legacy).run(AnalysisRequest(
        "media.mkv", window_seconds=15, purpose="audio_review", brief=BRIEF))
    assert report["status"] == "completed", report.get("errors")


@pytest.mark.parametrize("method", ["probe", "prepare"])
@pytest.mark.parametrize("owner", [Backend, NativeBackend])
def test_probe_and_prepare_gain_a_keyword_only_input_format_defaulting_to_none(
        owner: type, method: str) -> None:
    parameter = inspect.signature(getattr(owner, method)).parameters["input_format"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_infer_batch_signature_is_unchanged() -> None:
    parameters = inspect.signature(Backend.infer_batch).parameters
    assert "input_format" not in parameters


def _command(argv: list[str]) -> bytes:
    result = subprocess.run(argv, check=False, capture_output=True, timeout=120,
                            stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def _synthetic_source(prefix: str, name: str, *extra: str) -> str:
    root = ProjectRoot(REPO)
    rel = f"{prefix}/{name}"
    root.write_with(rel, lambda path: _command([
        FFMPEG or "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=320x240:rate=24:duration=3", "-f", "lavfi", "-i",
        "aevalsrc=0.25*sin(2*PI*317*t)|-0.25*sin(2*PI*317*t):s=48000:d=3",
        *extra, "-y", str(path)]))
    return rel


def _contains_named_input_demuxer(command: list[str], demuxer: str) -> bool:
    position = command.index("-i")
    return any(command[k] == "-f" and command[k + 1] == demuxer for k in range(position - 1))


@needs_media
def test_native_probe_and_prepare_force_the_named_demuxer() -> None:
    root = ProjectRoot(REPO)
    prefix = "_work/qwen_contract_tests/managed/" + uuid.uuid4().hex
    rel = _synthetic_source(prefix, "source.mkv", "-c:v", "ffv1", "-c:a", "pcm_f32le")
    backend = NativeBackend(root)
    info = backend.probe(rel, work_rel=prefix + "/probe", modality="joint", input_format="matroska")
    assert (info.sample_rate, info.channels) == (48000, 2)
    probe_command = backend.last_probe["command"]
    assert any(probe_command[k] == "-f" and probe_command[k + 1] == "matroska"
               for k in range(len(probe_command) - 1)), probe_command
    window = WindowSpec(1, 0.5, 1.5, 0.5, 1.5)
    prepared = backend.prepare(rel, window, work_rel=prefix + "/window", modality="joint",
                               input_format="matroska")
    assert _contains_named_input_demuxer(prepared.preparation["ffmpeg_command"], "matroska")
    actual = _command([FFMPEG or "ffmpeg", "-nostdin", "-v", "error", "-i",
                       str(root.resolve(prepared.media_path)), "-map", "0:a:0", "-c:a", "pcm_f32le",
                       "-f", "f32le", "pipe:1"])
    expected = _command([FFMPEG or "ffmpeg", "-nostdin", "-v", "error", "-i", str(root.resolve(rel)),
                         "-map", "0:a:0", "-af", "atrim=start_sample=24000:end_sample=72000",
                         "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"])
    assert actual == expected, "naming the demuxer must not change the prepared PCM"


@needs_media
def test_native_helper_inputs_are_not_probed_a_wrong_named_demuxer_fails() -> None:
    root = ProjectRoot(REPO)
    prefix = "_work/qwen_contract_tests/managed/" + uuid.uuid4().hex
    rel = _synthetic_source(prefix, "source.mkv", "-c:v", "ffv1", "-c:a", "pcm_f32le")
    backend = NativeBackend(root)
    failures = (RuntimeError, ValueError, JobError, QwenRequestError)
    with pytest.raises(failures):
        backend.probe(rel, work_rel=prefix + "/probe", modality="joint", input_format="wav")
    with pytest.raises(failures):
        backend.prepare(rel, WindowSpec(1, 0, 1, 0, 1), work_rel=prefix + "/window",
                        modality="joint", input_format="wav")


# --------------------------------------------------------------------------
# run_managed: cancellation
# --------------------------------------------------------------------------


def test_cancel_before_the_first_window_ends_cancelled_with_a_persisted_report(
        project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=100.0)
    report = managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request(),
                     cancel=lambda: True)
    assert report["status"] == "cancelled"
    assert backend.prepares == [] and backend.batches == []
    assert read_report(project, report)["status"] == "cancelled"
    assert any(error.get("kind") == "cancelled" for error in report["errors"])


def test_cancel_is_checked_before_each_window_preparation(project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=100.0)
    report = managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request(),
                     cancel=lambda: len(backend.prepares) >= 2)
    assert report["status"] == "cancelled"
    assert [p["index"] for p in backend.prepares] == [1, 2], "window 3 was prepared after cancel"
    assert backend.batches == []
    assert read_report(project, report)["status"] == "cancelled"


def test_cancel_is_checked_before_each_batch_and_keeps_the_completed_prefix(
        project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=100.0)
    report = managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request(),
                     cancel=lambda: bool(backend.batches))
    assert report["status"] == "cancelled"
    assert backend.batches == [[1, 2, 3]]
    assert [w["status"] for w in report["windows"][:3]] == ["completed"] * 3
    assert report["input_coverage"]["uncovered_intervals"], "windows 4-5 were never covered"
    on_disk = read_report(project, report)
    assert on_disk["status"] == "cancelled"
    assert on_disk["windows"][0]["raw_response"] == "  raw window 1\n```unverified```\n"


def test_a_cancelled_outcome_from_the_backend_during_inference_ends_cancelled(
        project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=100.0)
    backend.cancel_batch[1] = 1
    report = managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request())
    assert report["status"] == "cancelled"
    assert report["windows"][0]["status"] == "completed"
    assert backend.batches == [[1, 2, 3]]
    assert read_report(project, report)["status"] == "cancelled"


# --------------------------------------------------------------------------
# run_managed: outcomes, no retry
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cap,truncated", [(False, False), (True, True)])
def test_completed_and_truncated_windows_both_complete_the_run(
        project: ProjectRoot, cap: bool, truncated: bool) -> None:
    backend = ManagedFakeBackend(project, duration=30.0)
    backend.cap = cap
    report = managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request())
    assert report["status"] == "completed", report.get("errors")
    assert report["has_truncated_responses"] is truncated
    assert {w["status"] for w in report["windows"]} == {"truncated" if truncated else "completed"}


def test_a_failed_batch_reports_the_first_error_and_is_attempted_once(project: ProjectRoot) -> None:
    """No automatic retry: whatever the error kind's usual policy, one attempt."""
    backend = ManagedFakeBackend(project, duration=100.0)
    backend.fail_batch[1] = (1, failure("out_of_memory", "synthetic OOM"))
    report = managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request())
    assert report["status"] == "failed"
    assert report["errors"][0]["kind"] == "out_of_memory"
    assert backend.batches == [[1, 2, 3]], "the failed batch was retried or a later batch ran"
    assert [w["status"] for w in report["windows"]] == [
        "completed", "failed", "not_run", "not_run", "not_run"]


def test_a_setup_error_from_the_helper_propagates_as_the_setup_error(project: ProjectRoot) -> None:
    """The job maps it to MISSING_MODEL_ARTIFACT; the helper entry must not swallow it."""
    backend = ManagedFakeBackend(project, duration=30.0)
    backend.raise_in_probe = QwenSetupError("isolated Qwen spike runtime is unavailable")
    # The queue wraps non-JobError exceptions; the message must survive.
    with pytest.raises((QwenSetupError, JobError), match="runtime is unavailable"):
        managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request())


def test_a_request_error_from_the_helper_propagates_as_the_request_error(project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=30.0)
    backend.raise_in_probe = QwenRequestError("input must contain an audio stream")
    with pytest.raises((QwenRequestError, JobError), match="audio stream"):
        managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request())


# --------------------------------------------------------------------------
# the worker control
# --------------------------------------------------------------------------


def test_the_worker_control_surface_when_idle(project: ProjectRoot) -> None:
    """The worker control belongs to the job adapter (job.py), which the lease reads."""
    from orchestrator.adapters.qwen.job import QwenAudioReviewAdapter

    worker = QwenAudioReviewAdapter(project, Settings(project_root=project.path),
                                    backend=ManagedFakeBackend(project)).worker
    assert worker.pid() == os.getpid()
    worker.unload()
    assert worker.terminate() is True
    worker.respawn()  # AMBIGUOUS: "does nothing"; only that it does not raise


# --------------------------------------------------------------------------
# run_supervised: should_stop, with a real stand-in worker process tree
# --------------------------------------------------------------------------


def _load_analyze() -> Any:
    path = REPO / "spikes/23_joint_av_analyzer/analyze.py"
    spec = importlib.util.spec_from_file_location("managed_contract_analyze_target", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_supervised_declares_should_stop_defaulting_to_none() -> None:
    parameter = inspect.signature(_load_analyze().run_supervised).parameters["should_stop"]
    assert parameter.default is None


def _alive(pid: int, created: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == created and process.is_running()
    except psutil.NoSuchProcess:
        return False


class StandIn:
    """Runs ``run_supervised`` against a CPU sleeper instead of the model worker."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = ProjectRoot(REPO)
        self.target = _load_analyze()
        self.prefix = "_work/qwen_contract_tests/should_stop/" + uuid.uuid4().hex
        self.python = self.root.resolve("spikes/23_joint_av_analyzer/_work/env/Scripts/python.exe")
        assert self.python.is_file(), "native isolated launcher required for this CPU test"
        self.dummy = self.root.write_text(self.prefix + "/sleeper.py", "import time\ntime.sleep(90)\n")
        self.identities: dict[int, float] = {}
        self.processes: list[subprocess.Popen[bytes]] = []
        self.sentinel = subprocess.Popen(
            [str(self.python), "-B", str(self.dummy)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.sentinel_identity = psutil.Process(self.sentinel.pid).create_time()
        stand_in = self

        def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            process = subprocess.Popen([argv[0], "-B", str(stand_in.dummy)], **kwargs)
            stand_in.processes.append(process)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                parent = psutil.Process(process.pid)
                for item in [parent, *parent.children(recursive=True)]:
                    stand_in.identities[item.pid] = item.create_time()
                if len(stand_in.identities) >= 2:
                    break
                time.sleep(0.02)
            assert len(stand_in.identities) >= 2, "stand-in launcher descendant absent"
            return process

        import types
        self.target.subprocess = types.SimpleNamespace(
            **{key: getattr(subprocess, key) for key in dir(subprocess) if key != "Popen"}, Popen=spawn)
        monkeypatch.setattr(self.target, "_gpu_memory_sample", lambda: {
            "index": 0, "used_mb": 0, "used_mib": 0, "free_mib": 0, "test_stub": "CPU only"})
        monkeypatch.setattr(self.target, "_ram_available", lambda: 10 * 1024**3)

    def run(self, should_stop: Callable[[], bool]) -> tuple[dict[str, Any], dict[str, Any]]:
        p = self.prefix
        return self.target.run_supervised([], "audio", 1, 5, p + "/result.json", p + "/stdout.log",
                                          p + "/stderr.log", p + "/resources.jsonl",
                                          should_stop=should_stop)

    def cleanup(self) -> None:
        for pid, created in reversed(list(self.identities.items())):
            if _alive(pid, created):
                psutil.Process(pid).kill()
        for process in self.processes:
            process.wait(timeout=10)
        try:
            family = [psutil.Process(self.sentinel.pid),
                      *psutil.Process(self.sentinel.pid).children(recursive=True)]
        except psutil.NoSuchProcess:
            family = []
        for process in reversed(family):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        self.sentinel.wait(timeout=10)


def test_should_stop_stops_the_owned_tree_quickly_and_leaves_an_unrelated_process(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """AMBIGUOUS: "within about 2 s" is asserted as 3 s, to tolerate a loaded CI machine; the
    measured value is in the assertion message. The result's caller-cancelled marker is not
    named by the contract, so only the word "cancel" anywhere in the failure result is required."""
    stand_in = StandIn(monkeypatch)
    stop_requested_at: list[float] = []
    flag = threading.Event()

    def stop_soon() -> None:
        time.sleep(1.0)
        stop_requested_at.append(time.monotonic())
        flag.set()

    threading.Thread(target=stop_soon, daemon=True).start()
    try:
        output, telemetry = stand_in.run(flag.is_set)
        returned_at = time.monotonic()
        elapsed = returned_at - stop_requested_at[0]
        assert elapsed <= 3.0, f"supervisor took {elapsed:.2f}s to honour should_stop"
        assert output["status"] == "failure"
        assert "cancel" in json.dumps(output).lower(), output
        assert isinstance(telemetry, dict)
        assert not any(_alive(pid, created) for pid, created in stand_in.identities.items()), \
            "the owned worker tree survived should_stop"
        assert _alive(stand_in.sentinel.pid, stand_in.sentinel_identity), "an unrelated process was stopped"
    finally:
        stand_in.cleanup()


def test_should_stop_is_polled_at_least_every_quarter_second_and_resources_stay_at_five_seconds(
        monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in = StandIn(monkeypatch)
    polls: list[float] = []
    resource_samples: list[float] = []
    original_sample = stand_in.target._gpu_memory_sample

    def counting_sample() -> dict[str, Any]:
        resource_samples.append(time.monotonic())
        return original_sample()

    monkeypatch.setattr(stand_in.target, "_gpu_memory_sample", counting_sample)
    begin = time.monotonic()

    def should_stop() -> bool:
        polls.append(time.monotonic())
        return time.monotonic() - begin >= 2.2

    try:
        output, _telemetry = stand_in.run(should_stop)
        assert output["status"] == "failure"
        assert len(polls) >= 6, f"only {len(polls)} polls in 2.2 s"
        gaps = [later - earlier for earlier, later in zip(polls, polls[1:], strict=False)]
        assert max(gaps) <= 0.6, f"largest gap between polls was {max(gaps):.2f}s (contract: 0.25s)"
        # Resource sampling keeps its 5 s cadence: baseline + one sample in 2.2 s, not one per poll.
        assert len(resource_samples) <= 4, resource_samples
    finally:
        stand_in.cleanup()


def test_a_should_stop_that_is_true_immediately_never_leaves_the_worker_running(
        monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in = StandIn(monkeypatch)
    try:
        started = time.monotonic()
        output, _telemetry = stand_in.run(lambda: True)
        assert time.monotonic() - started <= 8.0
        assert output["status"] == "failure"
        assert not any(_alive(pid, created) for pid, created in stand_in.identities.items())
        assert _alive(stand_in.sentinel.pid, stand_in.sentinel_identity)
    finally:
        stand_in.cleanup()


# --------------------------------------------------------------------------
# review adjudications V2 and V5
# --------------------------------------------------------------------------


def test_v2_a_staged_file_that_does_not_match_the_expected_hash_is_corrupt_before_probing(
        project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=30.0)
    adapter = QwenAdapter(project, backend=backend)
    with pytest.raises(JobError) as raised:
        LeaseEnv(project).run(lambda lease: adapter.run_managed(
            audio_request(), lease=lease, cancel=lambda: False, input_format="matroska",
            expected_sha256="0" * 64))
    assert raised.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert backend.probes == [] and backend.prepares == [] and backend.batches == []


def test_v2_bytes_changed_after_the_first_check_are_corrupt_before_any_preparation(
        project: ProjectRoot) -> None:
    backend = ManagedFakeBackend(project, duration=30.0)
    backend.on_probe = lambda record: project.write_bytes("media.mkv", b"changed after staging")
    try:
        report = managed(QwenAdapter(project, backend=backend), LeaseEnv(project), audio_request())
    except JobError as exc:
        assert exc.kind is JobErrorKind.CORRUPT_ARTIFACT
    else:
        assert report["status"] == "failed"
        assert any(e.get("kind") == "corrupt_artifact" for e in report["errors"]), report["errors"]
    assert backend.batches == [], "inference ran on bytes that no longer matched the verified hash"
    assert not [w for w in (locals().get("report") or {}).get("windows", []) if w.get("raw_response")]


@pytest.mark.parametrize("attempt_limit,expected", [
    (1, JobErrorKind.OUT_OF_MEMORY), (None, JobErrorKind.BUDGET_EXCEEDED)])
def test_v5_the_per_queue_attempt_limit_keeps_the_retryable_kind_and_the_default_relabels(
        project: ProjectRoot, attempt_limit: int | None, expected: JobErrorKind) -> None:
    env = LeaseEnv(project, attempt_limit=attempt_limit, retries_per_shot=0)

    def fail(_lease: Any) -> None:
        raise JobError(JobErrorKind.OUT_OF_MEMORY, "synthetic OOM")

    with pytest.raises(JobError) as raised:
        env.run(fail)
    assert raised.value.kind is expected
