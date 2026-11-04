"""Resource-aware serial job queue over the persisted ``Job`` entity."""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from orchestrator.domain.entities import Job
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

from .lease import GpuLease


class ResourceProbe(Protocol):
    def free_ram_gb(self) -> float: ...

    def free_disk_gb(self, path: Path) -> float: ...

    def free_vram_mb(self) -> float: ...

    def foreign_processes(self) -> list[tuple[str, float]]: ...


class SystemResourceProbe:
    """Host resources plus device headroom through the same NVML adapter as the lease."""

    def __init__(self, nvml: object) -> None:
        self.nvml = nvml

    def free_ram_gb(self) -> float:
        if sys.platform == "win32":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise OSError("GlobalMemoryStatusEx failed")
            return status.ullAvailPhys / (1024**3)
        if hasattr(os, "sysconf"):
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024**3)
        raise OSError("Host available-memory probe is unsupported on this platform")

    def free_disk_gb(self, path: Path) -> float:
        return shutil.disk_usage(path).free / (1024**3)

    def free_vram_mb(self) -> float:
        return max(0.0, _device_total_mb(self.nvml) - self.nvml.used_memory_mb())

    def foreign_processes(self) -> list[tuple[str, float]]:
        return self.nvml.foreign_processes()


class JobQueue:
    """In-process queue. GPU work is serialized by ``GpuLease``."""

    def __init__(
        self,
        project_root: ProjectRoot | Path | str,
        settings: Settings,
        lease: GpuLease,
        *,
        resources: ResourceProbe,
        quarantine: Callable[[Iterable[str]], None],
        stop_worker: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        watchdog_interval_seconds: float = 10.0,
        progress_rate_floor: float = 0.001,
    ) -> None:
        self.root = project_root if isinstance(project_root, ProjectRoot) else ProjectRoot(project_root)
        self.settings = settings
        self.lease = lease
        self.resources = resources
        self.quarantine = quarantine
        self.stop_worker = stop_worker or (lambda: None)
        self.clock, self.sleep = clock, sleep
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.progress_rate_floor = progress_rate_floor

    def preflight(self, job: Job) -> None:
        """Check declared VRAM/RAM/disk needs before allowing a job to start."""
        free_vram = self.resources.free_vram_mb()
        allowance = self.settings.resources.foreign_vram_allowance_mb
        if allowance is None:
            # Re-measured on every preflight, never cached. Baseline is NVML usage
            # attributable to other processes, where process accounting is available.
            allowance = sum(memory for _, memory in self.resources.foreign_processes())
        needed_mb = job.required_vram_gb * 1024
        reserve_mb = needed_mb + allowance + self.settings.resources.vram_floor_mb
        if free_vram < reserve_mb:
            processes = self.resources.foreign_processes()
            names = ", ".join(name for name, memory in processes if memory > 0) or "unknown process"
            raise JobError(
                JobErrorKind.OUT_OF_MEMORY,
                f"Insufficient free VRAM: need {needed_mb:.0f} MB plus {allowance:.0f} MB foreign allowance "
                f"and {self.settings.resources.vram_floor_mb:.0f} MB floor; available {free_vram:.0f} MB. "
                f"Foreign process: {names}",
                job_id=job.job_id,
            )
        free_ram = self.resources.free_ram_gb()
        ram_floor = self.settings.resources.ram_floor_gb
        if free_ram - ram_floor < job.required_ram_gb:
            raise JobError(
                JobErrorKind.OUT_OF_MEMORY,
                f"Insufficient free host RAM: need {job.required_ram_gb:.2f} GB plus {ram_floor:.2f} GB floor; "
                f"available {free_ram:.2f} GB",
                job_id=job.job_id,
            )
        free_disk = self.resources.free_disk_gb(self.root.path)
        required_disk = max(self.settings.budget.min_free_disk_gb, job.required_disk_gb)
        if free_disk < required_disk:
            raise JobError(
                JobErrorKind.DISK_EXHAUSTED,
                f"Insufficient free disk: need {required_disk:.2f} GB; available {free_disk:.2f} GB",
                job_id=job.job_id,
            )

    def run(self, job: Job, worker: Callable[[Job], object], *, partial_artifacts: Iterable[str] = ()) -> object:
        """Preflight and execute one job with the taxonomy's retry policy and cancellation handling."""
        policy = JobErrorKind.INTERNAL.policy.retry
        try:
            self.preflight(job)
            self.lease.acquire()
            job.lease_holder_pid = __import__("os").getpid()
            while True:
                job.attempts += 1
                try:
                    return self._run_watched(job, worker)
                except JobError as error:
                    error.job_id = error.job_id or job.job_id
                    policy = error.kind.policy.retry
                    if policy is None or not policy.allows_attempt(job.attempts + 1):
                        job.state = "failed"
                        job.error_kind = error.kind.value
                        raise
                    self.lease.release()
                    self.sleep(policy.delay_before_retry(job.attempts))
                    self.preflight(job)
                    self.lease.acquire()
        except JobError as error:
            job.state = "cancelled" if error.kind is JobErrorKind.CANCELLED else "failed"
            job.error_kind = error.kind.value
            if error.kind is JobErrorKind.CANCELLED:
                self.quarantine(partial_artifacts)
            raise
        except Exception as exc:
            error = JobError.from_exception(exc, job_id=job.job_id)
            job.state = "failed"
            job.error_kind = error.kind.value
            raise error from exc
        finally:
            if getattr(self.lease, "_owned", False):
                self.lease.release()
            job.lease_holder_pid = None

    def _run_watched(self, job: Job, worker: Callable[[Job], object]) -> object:
        start = self.clock()
        baseline = job.progress
        result: list[object] = []
        failure: list[Exception] = []

        def execute() -> None:
            try:
                result.append(worker(job))
            except Exception as exc:  # noqa: BLE001 - classify injected worker failures
                failure.append(exc)

        import threading

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        last_sample = start
        while thread.is_alive():
            self.sleep(min(0.05, self.watchdog_interval_seconds))
            now = self.clock()
            if now - last_sample < self.watchdog_interval_seconds:
                continue
            elapsed = now - last_sample
            rate = max(0.0, job.progress - baseline) / elapsed
            if rate < self.progress_rate_floor:
                self.stop_worker()
                thread.join(timeout=max(1.0, self.watchdog_interval_seconds))
                raise JobError(JobErrorKind.OUT_OF_MEMORY, "VRAM thrash suspected: job progress rate below floor", job_id=job.job_id)
            baseline, last_sample = job.progress, now
        thread.join()
        if failure:
            raise JobError.from_exception(failure[0], job_id=job.job_id)
        job.state = "completed"
        job.progress = 1.0
        return result[0] if result else None


def _device_total_mb(nvml: object) -> float:
    if hasattr(nvml, "total_memory_mb"):
        return float(nvml.total_memory_mb())
    return float(getattr(nvml, "total_mb", 0.0))
