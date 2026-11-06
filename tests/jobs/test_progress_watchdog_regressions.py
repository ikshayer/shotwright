"""Additional queue assertions for render-progress watchdog integration."""

from __future__ import annotations

import threading

import pytest

from orchestrator.domain.entities import Job
from orchestrator.jobs.journal import read_job
from orchestrator.platform.errors import DIAGNOSTIC_ORIGINS, JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from tests.jobs.test_lease import Clock
from tests.jobs.test_queue import build, make_job


def test_default_startup_allowance_has_margin_over_measured_cold_cache() -> None:
    measured_cold_cache_seconds = 74.0
    derived_startup_bound_seconds = 2 * measured_cold_cache_seconds

    actual = Settings().resources.startup_allowance_seconds

    assert actual >= derived_startup_bound_seconds, (
        f"default startup allowance {actual}s is below the derived bound "
        f"of at least twice the measured ~{measured_cold_cache_seconds}s cold shader-cache startup"
    )


def test_queue_raised_budget_exceeded_persists_job_queue_origin(tmp_path) -> None:
    env = build(
        tmp_path,
        resources={"throughput_floor_per_second": 0.1},
        queue_clock=Clock(step=0.001),
    )
    stopped = threading.Event()
    env.worker.terminate = lambda: (stopped.set(), True)[1]  # type: ignore[method-assign]
    job = make_job(job_id="queue-budget-origin")

    def remain_silent(_job: Job) -> None:
        stopped.wait(timeout=1.0)

    with pytest.raises(JobError) as caught:
        env.queue.run(job, remain_silent, startup_allowance_seconds=0.0)

    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED
    record = read_job(env.root, job.job_id)
    assert record is not None and record.get("state") == "failed", (
        "queue watchdog failure was not persisted as a failed job"
    )
    origin = record.get("error_origin")
    assert origin == "job_queue", f"queue-raised BUDGET_EXCEEDED origin was {origin!r}"
    assert origin in DIAGNOSTIC_ORIGINS, (
        f"queue error origin {origin!r} is outside the pinned diagnostic vocabulary"
    )
