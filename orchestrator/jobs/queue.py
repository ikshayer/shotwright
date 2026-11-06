"""Resource preflight, persisted job lifecycle, and serialized GPU execution."""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TypeVar

from orchestrator.domain.entities import Job
from orchestrator.jobs.journal import append_job
from orchestrator.jobs.lease import GpuLease, NvmlProbe
from orchestrator.platform.containment import ATOMIC_WRITE_DIRECTORY, ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind, failure_diagnostics
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore

T = TypeVar("T")


class HostResources(Protocol):
    def free_ram_gb(self) -> float: ...
    def free_disk_gb(self, path: Path) -> float: ...


class Quarantine(Protocol):
    def quarantine(self, paths: Sequence[Path]) -> Path: ...


def _raise_walk_error(error: OSError) -> None:
    """A project-size scan must not silently undercount an unreadable subtree."""
    raise error


class SystemHostResources:
    """Host RAM and volume availability from psutil and the OS."""

    def free_ram_gb(self) -> float:
        try:
            import psutil
        except ImportError as exc:
            raise RuntimeError("psutil is required to measure host RAM") from exc
        return psutil.virtual_memory().available / (1024**3)

    def free_disk_gb(self, path: Path) -> float:
        return shutil.disk_usage(path).free / (1024**3)


class ProjectQuarantine:
    """Move partial outputs into a project-local quarantine directory."""

    def __init__(self, root: ProjectRoot) -> None:
        self.root = root

    def quarantine(self, paths: Sequence[Path]) -> Path:
        destination_relative = f"quarantine/{time.strftime('%Y%m%d')}-{uuid.uuid4().hex}"
        destination = self.root.make_dir(destination_relative)
        for index, source in enumerate(paths):
            source_resolved = source.resolve(strict=True)
            try:
                relative = source_resolved.relative_to(self.root.path)
            except ValueError as exc:
                raise JobError(JobErrorKind.INVALID_MANIFEST,
                               f"partial artifact is outside the project root: {source}", cause=exc) from exc
            # Resolve both ends through the single containment authority.
            contained_source = self.root.resolve(relative.as_posix())
            target_relative = f"{destination_relative}/{index:04d}-{contained_source.name}"
            target = self.root.resolve(target_relative)
            shutil.move(str(contained_source), str(target))
        return destination


class JobQueue:
    def __init__(self, root: ProjectRoot, settings: Settings, lease: GpuLease, store: ProjectStore, *,
                 nvml: NvmlProbe, host: HostResources, quarantine: Quarantine,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.root, self.settings, self.lease, self.store = root, settings, lease, store
        self.nvml, self.host, self.quarantine, self.clock = nvml, host, quarantine, clock
        self._budget_lock = threading.Lock()
        self._watch_context = threading.local()

    def preflight(self, job: Job) -> None:
        memory = self.nvml.device_memory()
        allowance = self.settings.resources.foreign_vram_allowance_mb
        baseline = self.lease.baseline_mb if self.lease._owned else memory.used_mb
        foreign_allowance = baseline if allowance is None else float(allowance)
        own_pid = self.lease.worker.pid()
        foreign = [usage for usage in self.nvml.process_usage()
                   if usage.pid != own_pid and usage.used_mb > foreign_allowance]
        if foreign:
            offender = max(foreign, key=lambda item: item.used_mb)
            raise JobError(JobErrorKind.OUT_OF_MEMORY,
                           f"foreign GPU process {offender.name} (PID {offender.pid}) uses "
                           f"{offender.used_mb:.0f} MiB, above the {foreign_allowance:.0f} MiB allowance",
                           job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
        # Device free already excludes every allocation; do not add foreign use back.
        usable_free_mb = memory.free_mb - max(0.0, memory.used_mb - baseline)
        if usable_free_mb < job.required_vram_gb * 1024:
            raise JobError(JobErrorKind.OUT_OF_MEMORY,
                           f"free VRAM precondition failed: {usable_free_mb / 1024:.2f} GB available, "
                           f"{job.required_vram_gb:.2f} GB required",
                           job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
        self._preflight_host(job, cpu_only=False)

    def preflight_cpu(self, job: Job) -> None:
        """Admit a job that the caller has classified as not using a GPU."""
        self._preflight_host(job, cpu_only=True)

    def _preflight_host(self, job: Job, *, cpu_only: bool) -> None:
        free_ram = self.host.free_ram_gb()
        if free_ram < job.required_ram_gb:
            remediation = (
                "Free host RAM or close host-memory consumers, then retry the stage or "
                "reduce its host-memory requirement."
                if cpu_only else None
            )
            raise JobError(JobErrorKind.OUT_OF_MEMORY,
                           f"free host RAM precondition failed: {free_ram:.2f} GB available, "
                           f"{job.required_ram_gb:.2f} GB required",
                           job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id,
                           remediation=remediation)
        free_disk = self.host.free_disk_gb(self.root.path)
        disk_required = job.required_disk_gb + self.settings.budget.min_free_disk_gb
        if free_disk < disk_required:
            raise JobError(JobErrorKind.DISK_EXHAUSTED,
                           f"free disk precondition failed: {free_disk:.2f} GB available, "
                           f"{disk_required:.2f} GB required",
                           job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
        if self.root.path.exists():
            project_size_bytes = 0
            for current, directories, filenames in os.walk(
                self.root.path, onerror=_raise_walk_error
            ):
                if Path(current) == self.root.path:
                    directories[:] = [
                        name
                        for name in directories
                        if os.path.normcase(name) != os.path.normcase(ATOMIC_WRITE_DIRECTORY)
                    ]
                project_size_bytes += sum(
                    (Path(current) / name).stat().st_size for name in filenames
                )
            project_size = project_size_bytes / 1024**3
            if project_size + job.required_disk_gb > self.settings.budget.disk_ceiling_gb:
                raise JobError(JobErrorKind.BUDGET_EXCEEDED,
                               "project disk ceiling would be exceeded by this job",
                               job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)

    def run(self, job: Job, worker_fn: Callable[[Job], T], *,
            partial_artifacts: Sequence[Path] = (),
            startup_allowance_seconds: float | None = None) -> T:
        if startup_allowance_seconds is not None:
            try:
                finite_allowance = math.isfinite(startup_allowance_seconds)
            except (OverflowError, TypeError):
                finite_allowance = False
            if (isinstance(startup_allowance_seconds, bool)
                    or not isinstance(startup_allowance_seconds, (int, float))
                    or not finite_allowance or startup_allowance_seconds < 0):
                raise ValueError("startup_allowance_seconds must be a finite nonnegative number")
        startup_allowance = (
            self.settings.resources.startup_allowance_seconds
            if startup_allowance_seconds is None
            else startup_allowance_seconds
        )
        self._transition(job, "queued")
        try:
            self.preflight(job)
        except JobError as error:
            self._transition(job, "failed", error=error)
            raise
        budget_key = job.shot_id or job.job_id
        deadline = self.clock() + self.settings.budget.per_shot_wall_clock_seconds
        last_error: JobError | None = None
        retry_policy = None
        while True:
            if self.clock() >= deadline:
                last_error = JobError(JobErrorKind.BUDGET_EXCEEDED, "per-shot wall-clock budget exceeded",
                                      job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
                break
            self.lease.acquire(timeout_seconds=max(0.0, deadline - self.clock()))
            acquired_here = True
            release_failed = False
            start = self.clock()
            try:
                if job.attempts >= self.settings.budget.retries_per_shot + 1:
                    raise JobError(JobErrorKind.BUDGET_EXCEEDED, "per-shot attempt ceiling reached",
                                   job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
                job.attempts += 1
                self._persist(job)
                self.preflight(job)
                job.lease_holder_pid = self.lease.worker.pid()
                self._transition(job, "running")
                try:
                    self._watch_context.startup_allowance_seconds = startup_allowance
                    try:
                        result = self._watch(worker_fn, job, deadline)
                    finally:
                        del self._watch_context.startup_allowance_seconds
                except JobError as error:
                    last_error = JobError.from_exception(error, job_id=job.job_id,
                                                         shot_id=job.shot_id, run_id=job.run_id)
                    if (last_error.kind is JobErrorKind.INTERNAL
                            and "watchdog could not confirm worker stopped" in last_error.message):
                        release_failed = True
                        break
                    self._charge_budget(job, budget_key, self.clock() - start)
                    try:
                        self.lease.release(
                            self.settings.resources.vram_floor_mb,
                            self.settings.resources.vram_release_timeout_seconds,
                            after_cuda_error=(last_error.kind is JobErrorKind.OUT_OF_MEMORY
                                              or "cuda error" in last_error.message.lower()),
                        )
                    except Exception:
                        release_failed = True
                        raise
                    acquired_here = False
                    policy = last_error.kind.policy.retry
                    budget_attempt_limit = self.settings.budget.retries_per_shot + 1
                    if policy is not None and job.attempts >= budget_attempt_limit < policy.max_attempts:
                        last_error = JobError(JobErrorKind.BUDGET_EXCEEDED,
                                              "per-shot attempt ceiling reached",
                                              job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
                    effective_limit = min(budget_attempt_limit, policy.max_attempts if policy else 1)
                    if not last_error.retryable or job.attempts >= effective_limit:
                        break
                    retry_policy = last_error.kind.policy.retry
                    if retry_policy is not None:
                        time.sleep(retry_policy.delay_before_retry(job.attempts))
                    self._transition(job, "queued", error=last_error)
                else:
                    self._charge_budget(job, budget_key, self.clock() - start)
                    try:
                        self.lease.release(self.settings.resources.vram_floor_mb,
                                           self.settings.resources.vram_release_timeout_seconds)
                    except Exception:
                        release_failed = True
                        raise
                    acquired_here = False
                    self._transition(job, "completed")
                    return result
            except Exception as exc:  # noqa: BLE001 - worker callables may expose arbitrary failure types.
                if isinstance(exc, JobError):
                    last_error = exc
                else:
                    last_error = JobError.from_exception(exc, job_id=job.job_id,
                                                         shot_id=job.shot_id, run_id=job.run_id)
                if (last_error.kind is JobErrorKind.INTERNAL
                        and "watchdog could not confirm worker stopped" in last_error.message):
                    release_failed = True
                if acquired_here and not release_failed:
                    # Do not discard a lease when measured release fails; it remains held.
                    try:
                        self.lease.release(self.settings.resources.vram_floor_mb,
                                           self.settings.resources.vram_release_timeout_seconds,
                                           after_cuda_error=(last_error.kind is JobErrorKind.OUT_OF_MEMORY
                                                             or "cuda error" in last_error.message.lower()))
                        acquired_here = False
                    except Exception as release_error:  # noqa: BLE001 - keep release failure as the terminal cause.
                        last_error = JobError.from_exception(release_error, job_id=job.job_id,
                                                             shot_id=job.shot_id, run_id=job.run_id)
                        release_failed = True
                if release_failed:
                    break
                retry_policy = last_error.kind.policy.retry
                budget_attempt_limit = self.settings.budget.retries_per_shot + 1
                if retry_policy is not None and job.attempts >= budget_attempt_limit < retry_policy.max_attempts:
                    last_error = JobError(JobErrorKind.BUDGET_EXCEEDED, "per-shot attempt ceiling reached",
                                          job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
                effective_limit = min(budget_attempt_limit, retry_policy.max_attempts if retry_policy else 1)
                if not last_error.retryable or job.attempts >= effective_limit:
                    break
                if retry_policy is not None:
                    time.sleep(retry_policy.delay_before_retry(job.attempts))
                self._transition(job, "queued", error=last_error)
        assert last_error is not None
        job.lease_holder_pid = None
        if last_error.kind is JobErrorKind.CANCELLED:
            try:
                self.quarantine.quarantine(partial_artifacts)
            finally:
                self._transition(job, "cancelled", error=last_error)
        else:
            self._transition(job, "failed", error=last_error)
        raise last_error

    def _watch(self, worker_fn: Callable[[Job], T], job: Job, deadline: float) -> T:
        """Run work in a managed thread; detect stalls and require confirmed worker stop."""
        result: list[T] = []
        errors: list[BaseException] = []
        done = threading.Event()

        def invoke() -> None:
            try:
                result.append(worker_fn(job))
            except Exception as exc:  # noqa: BLE001 - worker callables may expose arbitrary failure types.
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=invoke, name=f"job-{job.job_id}", daemon=True)
        last_progress = job.progress
        startup_started = self.clock()
        last_change = startup_started
        startup_complete = False
        thread.start()
        last_heartbeat = time.monotonic()
        configured_floor = self.settings.resources.throughput_floor_per_second
        startup_allowance_seconds = getattr(
            self._watch_context,
            "startup_allowance_seconds",
            self.settings.resources.startup_allowance_seconds,
        )
        while not done.wait(0.01):
            now = self.clock()
            if now >= deadline:
                raise JobError(JobErrorKind.BUDGET_EXCEEDED, "per-shot wall-clock budget exceeded")
            if configured_floor > 0:
                if not startup_complete:
                    if job.progress > last_progress:
                        last_progress, last_change = job.progress, now
                        startup_complete = True
                    elif now - startup_started >= startup_allowance_seconds:
                        self._fail_throughput_stall()
                elif job.progress > last_progress:
                    progress_rate = (job.progress - last_progress) / max(now - last_change, 1e-9)
                    last_progress, last_change = job.progress, now
                    if progress_rate < configured_floor:
                        self._fail_throughput_stall()
                elif now - last_change >= 1.0 / configured_floor:
                    self._fail_throughput_stall()
            heartbeat_interval = max(1.0, self.lease.ttl_seconds / 3)
            if time.monotonic() - last_heartbeat >= heartbeat_interval:
                try:
                    self.lease.heartbeat()
                except RuntimeError:
                    pass
                last_heartbeat = time.monotonic()
        if errors:
            error = errors[0]
            if isinstance(error, JobError):
                raise error
            raise JobError.from_exception(error, job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
        return result[0] if result else None  # type: ignore[return-value]

    def _fail_throughput_stall(self) -> None:
        if not self.lease.worker.terminate():
            raise JobError(JobErrorKind.INTERNAL,
                           "throughput watchdog could not confirm worker stopped")
        raise JobError(JobErrorKind.BUDGET_EXCEEDED,
                       "VRAM thrash suspected: job progress rate fell below configured floor")

    def _charge_budget(self, job: Job, key: str, elapsed: float) -> None:
        path = "jobs/budgets.json"
        with self._budget_lock:
            try:
                budget = json.loads(self.root.read_text(path, encoding="utf-8"))
            except FileNotFoundError:
                budget = {"shot_seconds": {}, "episode_seconds": {}}
            shot_seconds = float(budget["shot_seconds"].get(key, 0)) + elapsed
            episode_key = job.episode_id or job.run_id or "unassigned"
            episode_seconds = float(budget["episode_seconds"].get(episode_key, 0)) + elapsed
            if shot_seconds > self.settings.budget.gpu_minutes_per_shot * 60:
                raise JobError(JobErrorKind.BUDGET_EXCEEDED, "per-shot GPU-minute ceiling exceeded",
                               job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
            if episode_seconds > self.settings.budget.gpu_minutes_per_episode * 60:
                raise JobError(JobErrorKind.BUDGET_EXCEEDED, "per-episode GPU-minute ceiling exceeded",
                               job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
            budget["shot_seconds"][key] = shot_seconds
            budget["episode_seconds"][episode_key] = episode_seconds
            self.root.write_text(path, json.dumps(budget, sort_keys=True, indent=2))

    def _transition(self, job: Job, state: str, *, error: JobError | None = None) -> None:
        job.state = state
        job.error_kind = error.kind.value if error else None
        if error is None:
            job.error_message = None
            job.error_cause = None
            job.error_traceback = None
            job.error_traceback_truncated = False
            job.error_origin = None
        else:
            origin = error.origin or ("job_queue" if error.kind is JobErrorKind.INTERNAL else None)
            details = failure_diagnostics(
                error, origin=origin, secrets=self.settings.diagnostic_secrets()
            )
            for name, value in details.items():
                setattr(job, name, value)
        self._persist(job)

    def _persist(self, job: Job) -> None:
        append_job(self.root, job)

