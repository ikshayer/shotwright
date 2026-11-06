"""FR-12 startup-aware throughput watchdog contract tests."""

from __future__ import annotations

import math
import threading
from pathlib import Path

import pytest

from orchestrator.domain.entities import Job
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings, SettingsError
from tests.jobs.test_lease import Clock, make_settings
from tests.jobs.test_queue import build, make_job, persisted


@pytest.mark.parametrize("allowance", [-1.0, math.nan, math.inf, -math.inf])
def test_run_rejects_invalid_startup_allowance_before_any_side_effect(
    tmp_path: Path, allowance: float
) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.1})
    calls: list[str] = []
    job = make_job()

    with pytest.raises(ValueError):
        env.queue.run(job, lambda _j: calls.append("worker"), startup_allowance_seconds=allowance)

    assert calls == []
    assert not list(tmp_path.rglob("*.json")), "validation must precede job persistence and lease writes"


@pytest.mark.parametrize("allowance", [-1.0, math.nan, math.inf, -math.inf])
def test_settings_reject_invalid_startup_allowance(allowance: float) -> None:
    with pytest.raises(SettingsError):
        make_settings(resources={"startup_allowance_seconds": allowance})


@pytest.mark.parametrize("floor", [math.nan, math.inf, -math.inf])
def test_settings_reject_non_finite_throughput_floor(floor: float) -> None:
    with pytest.raises(SettingsError):
        make_settings(resources={"throughput_floor_per_second": floor})


def test_floor_zero_disables_startup_and_throughput_checks_but_not_deadline(tmp_path: Path) -> None:
    env = build(
        tmp_path,
        resources={"throughput_floor_per_second": 0.0},
        budget={"gpu_minutes_per_shot": 1e9, "gpu_minutes_per_episode": 1e9,
                "per_shot_wall_clock_seconds": 1e9},
        queue_clock=Clock(step=1000.0),
    )
    # A zero throughput floor must not fail merely because startup allowance is zero
    # and the worker never reports progress. The ordinary wall-clock ceiling remains active.
    env.settings.budget.per_shot_wall_clock_seconds = 1e9
    job = make_job()
    assert env.queue.run(job, lambda _j: "finished", startup_allowance_seconds=0.0) == "finished"
    assert persisted(env.root, job.job_id)["state"] == "completed"


def test_worker_can_report_progress_after_silent_startup_and_complete(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.1}, queue_clock=Clock(step=0.001))

    def work(job: Job) -> str:
        job.progress = 0.5
        return "done"

    assert env.queue.run(make_job(), work, startup_allowance_seconds=10.0) == "done"


def test_silent_startup_strictly_before_allowance_then_progress_completes(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.5}, queue_clock=Clock(step=0.001))

    def delayed_progress(job: Job) -> str:
        # Remain silent for several watcher polls while comfortably inside the allowance.
        threading.Event().wait(timeout=0.02)
        job.progress = 0.5
        return "done"

    assert env.queue.run(make_job(), delayed_progress, startup_allowance_seconds=10.0) == "done"


def test_immediate_first_progress_is_accepted_with_zero_startup_allowance(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.5}, queue_clock=Clock(step=0.1))

    def immediate_progress(job: Job) -> str:
        job.progress = 0.5
        # Keep the worker active long enough for the watcher to observe its first sample.
        threading.Event().wait(timeout=0.05)
        return "done"

    assert env.queue.run(make_job(), immediate_progress, startup_allowance_seconds=0.0) == "done"


def test_no_first_progress_at_zero_allowance_fails_only_after_confirmed_stop(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.1}, queue_clock=Clock(step=0.001))
    stopped = threading.Event()
    env.worker.terminate = lambda: (stopped.set(), True)[1]  # type: ignore[method-assign]

    def silent(_job: Job) -> None:
        stopped.wait(timeout=1.0)

    with pytest.raises(JobError) as info:
        env.queue.run(make_job(), silent, startup_allowance_seconds=0.0)
    assert info.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert stopped.is_set()


def test_no_first_progress_and_unconfirmed_stop_keeps_lease(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.1}, queue_clock=Clock(step=0.001))
    release = threading.Event()
    env.worker.terminate = lambda: False  # type: ignore[method-assign]

    def silent(_job: Job) -> None:
        release.wait(timeout=1.0)

    with pytest.raises(JobError) as info:
        env.queue.run(make_job(), silent, startup_allowance_seconds=0.0)
    assert info.value.kind is JobErrorKind.INTERNAL
    from tests.jobs.test_queue import lease_is_free

    assert not lease_is_free(env)
    release.set()


def test_silence_after_first_progress_times_out_from_configured_floor(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.5}, queue_clock=Clock(step=0.1))
    stopped = threading.Event()
    env.worker.terminate = lambda: (stopped.set(), True)[1]  # type: ignore[method-assign]

    def silent_after_startup(job: Job) -> None:
        job.progress = 0.25
        # Let the watcher observe the first sample before remaining silent.
        threading.Event().wait(timeout=0.02)
        stopped.wait(timeout=1.0)

    with pytest.raises(JobError) as info:
        env.queue.run(make_job(), silent_after_startup, startup_allowance_seconds=10.0)
    assert info.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert stopped.is_set()


def test_below_floor_subsequent_progress_rate_fails(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.5}, queue_clock=Clock(step=0.1))
    stopped = threading.Event()
    env.worker.terminate = lambda: (stopped.set(), True)[1]  # type: ignore[method-assign]

    def slow_progress(job: Job) -> None:
        job.progress = 0.25
        # Give the watcher time to observe its first post-startup sample.
        threading.Event().wait(timeout=0.02)
        # A second positive delta follows after a measurable interval and is below floor.
        job.progress = 0.2501
        stopped.wait(timeout=1.0)

    with pytest.raises(JobError) as info:
        env.queue.run(make_job(), slow_progress, startup_allowance_seconds=10.0)
    assert info.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert stopped.is_set()


def test_worker_job_error_before_startup_expiry_keeps_correlation_fields(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"throughput_floor_per_second": 0.5}, queue_clock=Clock(step=0.001))
    job = make_job(job_id="job-correlated", shot_id="shot-correlated", run_id="run-correlated")

    def fail(_job: Job) -> None:
        raise JobError(JobErrorKind.INVALID_MANIFEST, "worker failed during startup")

    with pytest.raises(JobError) as info:
        env.queue.run(job, fail, startup_allowance_seconds=10.0)
    assert info.value.kind is JobErrorKind.INVALID_MANIFEST
    assert info.value.job_id == "job-correlated"
    assert info.value.shot_id == "shot-correlated"
    assert info.value.run_id == "run-correlated"


def test_hard_deadline_takes_precedence_during_startup(tmp_path: Path) -> None:
    env = build(
        tmp_path,
        resources={"throughput_floor_per_second": 0.5},
        budget={"per_shot_wall_clock_seconds": 0.1, "gpu_minutes_per_shot": 1e9,
                "gpu_minutes_per_episode": 1e9},
        queue_clock=Clock(step=1000.0),
    )
    release = threading.Event()
    terminate_calls: list[bool] = []
    env.worker.terminate = lambda: (terminate_calls.append(True), False)[1]  # type: ignore[method-assign]

    def startup_stall(_job: Job) -> None:
        release.wait(timeout=1.0)

    try:
        with pytest.raises(JobError) as info:
            env.queue.run(make_job(), startup_stall, startup_allowance_seconds=100.0)
        assert info.value.kind is JobErrorKind.BUDGET_EXCEEDED
        assert terminate_calls == [], "hard deadline must retain its existing path, not startup-stop handling"
    finally:
        release.set()


def test_settings_default_is_measured_startup_allowance() -> None:
    # A bound must EXCEED its measurement, not equal it: 8.4 measures ~74 s for a cold
    # Blender shader cache, so a default equal to that fails about half the time.
    assert Settings().resources.startup_allowance_seconds > 74.0
