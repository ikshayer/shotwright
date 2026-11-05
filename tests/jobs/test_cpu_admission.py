"""CPU-only JobQueue admission tests based on ADMISSION_CONTRACT.md."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.domain.entities import Job
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobErrorKind


class NeverTouchNVML:
    def device_memory(self, *args, **kwargs):
        raise AssertionError("CPU admission queried device memory")

    def process_usage(self, *args, **kwargs):
        raise AssertionError("CPU admission queried process usage")


class Host:
    def __init__(self, ram=32.0, disk=100.0, ram_error=None, disk_error=None):
        self.ram = ram
        self.disk = disk
        self.ram_error = ram_error
        self.disk_error = disk_error
        self.calls = []

    def free_ram_gb(self):
        self.calls.append("ram")
        if self.ram_error:
            raise self.ram_error
        return self.ram

    def free_disk_gb(self, path: Path):
        self.calls.append(("disk", path))
        if self.disk_error:
            raise self.disk_error
        return self.disk


def make_job(**changes):
    fields = {
        "job_id": "job-1",
        "type": "unrestricted-test-job-type",
        "required_vram_gb": 0.0,
        "required_ram_gb": 4.0,
        "required_disk_gb": 3.0,
        "shot_id": "shot-2",
        "run_id": "run-3",
    }
    fields.update(changes)
    return Job(**fields)


def make_queue(tmp_path, *, ram=32.0, disk=100.0, min_free=5.0,
               ceiling=200.0, nvml=None, host_error=None,
               disk_error=None):
    q = object.__new__(JobQueue)
    q.root = ProjectRoot(tmp_path)
    q.settings = SimpleNamespace(
        budget=SimpleNamespace(min_free_disk_gb=min_free,
                               disk_ceiling_gb=ceiling)
    )
    q.host = Host(ram=ram, disk=disk, ram_error=host_error,
                  disk_error=disk_error)
    q.nvml = nvml or NeverTouchNVML()
    return q, q.host


def caught(call):
    with pytest.raises(Exception) as exc:
        call()
    return exc.value


def assert_correlated(error, job):
    assert error.job_id == job.job_id
    assert error.shot_id == job.shot_id
    assert error.run_id == job.run_id


def test_cpu_admission_skips_nvml_including_zero_vram(tmp_path):
    q, host = make_queue(tmp_path)
    q.preflight_cpu(make_job(required_vram_gb=0))
    assert host.calls == ["ram", ("disk", q.root.path)]


def test_cpu_admission_ignores_gpu_pressure_and_lease_state(tmp_path):
    class GPUIsExhausted(NeverTouchNVML):
        def device_memory(self, *args, **kwargs):
            return SimpleNamespace(free=0, total=1)

        def process_usage(self, *args, **kwargs):
            return [(999, 999)]

    q, _ = make_queue(tmp_path, nvml=GPUIsExhausted())
    q.lease = SimpleNamespace(worker=object())
    q.preflight_cpu(make_job(required_vram_gb=0))


def test_cpu_ram_refusal_is_correlated_and_host_specific(tmp_path):
    q, _ = make_queue(tmp_path, ram=1.0)
    job = make_job(required_ram_gb=4.0)
    error = caught(lambda: q.preflight_cpu(job))
    assert error.kind is JobErrorKind.OUT_OF_MEMORY
    assert_correlated(error, job)
    assert error.retryable is JobErrorKind.OUT_OF_MEMORY.retryable
    hint = error.remediation.lower()
    assert "ram" in hint or "host" in hint
    assert "gpu" not in hint and "vram" not in hint
    assert error.to_log_record()["remediation"] == error.remediation


@pytest.mark.parametrize(
    "kwargs, expected_kind",
    [
        ({"disk": 7.0, "min_free": 5.0}, JobErrorKind.DISK_EXHAUSTED),
        # Empty project size plus required output exceeds this positive ceiling.
        ({"ceiling": 1.0, "min_free": 0.0}, JobErrorKind.BUDGET_EXCEEDED),
    ],
)
def test_cpu_disk_refusals_keep_taxonomy_and_correlation(
    tmp_path, kwargs, expected_kind
):
    q, _ = make_queue(tmp_path, **kwargs)
    job = make_job(required_disk_gb=3.0)
    error = caught(lambda: q.preflight_cpu(job))
    assert error.kind is expected_kind
    assert_correlated(error, job)
    assert error.retryable is expected_kind.retryable
    assert error.remediation == expected_kind.remediation


@pytest.mark.parametrize("measurement", ["ram", "disk"])
def test_measurement_errors_propagate(tmp_path, measurement):
    original = RuntimeError(f"{measurement} measurement unavailable")
    q, _ = make_queue(
        tmp_path,
        host_error=original if measurement == "ram" else None,
        disk_error=original if measurement == "disk" else None,
    )
    with pytest.raises(RuntimeError) as exc:
        q.preflight_cpu(make_job())
    assert exc.value is original


def test_cpu_classification_does_not_depend_on_type_or_vram(tmp_path):
    q, _ = make_queue(tmp_path)
    q.preflight_cpu(
        make_job(type="unrecognized-type", required_vram_gb=1234.0)
    )
