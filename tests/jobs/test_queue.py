from __future__ import annotations

import time

import pytest

from orchestrator.domain.entities import Job
from orchestrator.jobs.lease import GpuLease
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings


class Resources:
    vram = 10_000.0
    ram = 20.0
    disk = 100.0
    foreign = (("other-app.exe", 500.0),)

    def free_vram_mb(self):
        return self.vram

    def free_ram_gb(self):
        return self.ram

    def free_disk_gb(self, path):
        return self.disk

    def foreign_processes(self):
        return self.foreign


class IdleNvml:
    def used_memory_mb(self):
        return 0

    def foreign_processes(self):
        return []


def setup(tmp_path, *, settings=None, resources=None, **kwargs):
    settings = settings or Settings()
    lease = GpuLease(tmp_path, settings, nvml=IdleNvml(), sleep=lambda _: None)
    queue = JobQueue(tmp_path, settings, lease, resources=resources or Resources(),
                     quarantine=kwargs.pop("quarantine", lambda _: None),
                     sleep=kwargs.pop("sleep", time.sleep), **kwargs)
    return queue, lease


def test_vram_precondition_names_foreign_process(tmp_path):
    queue, _ = setup(tmp_path)
    queue.resources.vram = 1500
    job = Job(job_id="j1", type="image", required_vram_gb=2)
    with pytest.raises(JobError, match="other-app.exe") as caught:
        queue.preflight(job)
    assert caught.value.kind is JobErrorKind.OUT_OF_MEMORY


def test_ram_precondition_is_independent(tmp_path):
    queue, _ = setup(tmp_path)
    queue.resources.ram = 5
    job = Job(job_id="j2", type="voice", required_ram_gb=2)
    with pytest.raises(JobError, match="host RAM") as caught:
        queue.preflight(job)
    assert caught.value.kind is JobErrorKind.OUT_OF_MEMORY


def test_disk_precondition(tmp_path):
    queue, _ = setup(tmp_path)
    queue.resources.disk = 1
    with pytest.raises(JobError, match="free disk") as caught:
        queue.preflight(Job(job_id="j3", type="render"))
    assert caught.value.kind is JobErrorKind.DISK_EXHAUSTED


def test_watchdog_detects_stalled_job(tmp_path):
    queue, _ = setup(tmp_path, watchdog_interval_seconds=0.02, progress_rate_floor=0.01)
    job = Job(job_id="j4", type="render")
    with pytest.raises(JobError, match="VRAM thrash suspected") as caught:
        queue.run(job, lambda _: time.sleep(0.08))
    assert caught.value.kind is JobErrorKind.OUT_OF_MEMORY


def test_cancellation_quarantines_partial_artifacts(tmp_path):
    quarantined = []
    queue, _ = setup(tmp_path, quarantine=lambda paths: quarantined.extend(paths))
    job = Job(job_id="j5", type="render")

    def cancel(_):
        raise JobError(JobErrorKind.CANCELLED, "cancel requested")

    with pytest.raises(JobError):
        queue.run(job, cancel, partial_artifacts=["renders/partial/frame.png"])
    assert job.state == "cancelled"
    assert quarantined == ["renders/partial/frame.png"]
