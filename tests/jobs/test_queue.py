"""JobQueue, tested from CONTRACT.md only."""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.domain.entities import Job
from orchestrator.jobs.lease import GpuLease, ProcessUsage
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine, SystemHostResources
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.store.json_store import ProjectStore
from tests.jobs.test_lease import (
    WORKER_PID,
    Clock,
    FakeNvml,
    FakeWorker,
    _cap_sleep,  # noqa: F401  (autouse fixture: no sleeps above ~10 ms)
    make_settings,
)


class FakeHost:
    def __init__(self, ram_gb: float = 20.0, disk_gb: float = 100.0) -> None:
        self.ram_gb, self.disk_gb = ram_gb, disk_gb

    def free_ram_gb(self) -> float:
        return self.ram_gb

    def free_disk_gb(self, path: Path) -> float:
        return self.disk_gb


class FakeQuarantine:
    """Moves, never deletes, like the contract's seam."""

    def __init__(self, destination: Path) -> None:
        self.destination, self.received = destination, []

    def quarantine(self, paths) -> Path:
        self.received.append(list(paths))
        self.destination.mkdir(parents=True, exist_ok=True)
        for path in paths:
            shutil.move(str(path), str(self.destination / path.name))
        return self.destination


class HookedLease(GpuLease):
    """Real lease whose acquire can run a hook afterwards or fail as a timed-out waiter."""

    after_acquire: Callable[[], None] | None = None
    fail_acquire = False

    def acquire(self, timeout_seconds: float) -> None:
        if self.fail_acquire:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "timed out waiting for the GPU lease")
        super().acquire(timeout_seconds)
        if self.after_acquire:
            self.after_acquire()


def build(tmp_path: Path, *, queue_clock: Clock | None = None, **sections: dict[str, object]) -> SimpleNamespace:
    root = ProjectRoot(tmp_path)
    sections.setdefault("resources", {})
    sections["resources"] = {"vram_release_timeout_seconds": 0.2, **sections["resources"]}  # type: ignore[dict-item]
    settings = make_settings(**sections)
    nvml = FakeNvml()
    worker = FakeWorker(nvml, used_after_unload=1400.0)
    lease_clock = Clock()
    lease = HookedLease(
        root, settings, nvml=nvml, worker=worker, pid_alive=lambda _p: True, clock=lease_clock, ttl_seconds=30.0
    )
    host = FakeHost()
    quarantine = FakeQuarantine(tmp_path / "quarantine_out")
    clock = queue_clock or Clock(step=0.0)
    queue = JobQueue(
        root, settings, lease, ProjectStore(root), nvml=nvml, host=host, quarantine=quarantine, clock=clock
    )
    return SimpleNamespace(
        root=root, settings=settings, nvml=nvml, worker=worker, lease=lease, host=host,
        quarantine=quarantine, clock=clock, queue=queue, lease_clock=lease_clock,
    )


def make_job(**fields: object) -> Job:
    fields.setdefault("job_id", "job-1")
    fields.setdefault("type", "flux_still")
    return Job(**fields)  # type: ignore[arg-type]


def persisted(root: ProjectRoot, job_id: str) -> dict[str, object]:
    """The last persisted record for `job_id`, found anywhere under the project root."""
    found: list[dict[str, object]] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if node.get("job_id") == job_id and "state" in node:
                found.append(node)
            for item in node.values():
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    for path in sorted(root.path.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for chunk in (text, *text.splitlines()):
            try:
                visit(json.loads(chunk))
            except ValueError:
                continue
    assert found, f"job {job_id} was never persisted under the project root"
    return found[-1]


def lease_is_free(env: SimpleNamespace) -> bool:
    other_nvml = FakeNvml()
    other = GpuLease(
        env.root, env.settings, nvml=other_nvml, worker=FakeWorker(other_nvml, pid=999),
        pid_alive=lambda _p: True, clock=env.lease_clock, ttl_seconds=30.0,
    )
    try:
        other.acquire(0.2)
    except JobError:
        return False
    other.release(env.settings.resources.vram_floor_mb, 0.5)
    return True


# --- happy path and persistence --------------------------------------------------------


def test_run_returns_worker_result_and_persists_each_transition(tmp_path: Path) -> None:
    env = build(tmp_path)
    job = make_job()
    seen: dict[str, object] = {}

    def work(j: Job) -> str:
        seen.update(persisted(env.root, j.job_id))
        return "artifact"

    assert env.queue.run(job, work) == "artifact"
    assert seen["state"] == "running"
    assert seen["attempts"] == 1
    assert seen["lease_holder_pid"] == WORKER_PID  # the worker's pid, not the orchestrator's
    assert persisted(env.root, job.job_id)["state"] == "completed"
    assert lease_is_free(env)


def test_refused_job_is_persisted_as_failed_with_its_error_kind(tmp_path: Path) -> None:
    env = build(tmp_path)
    job = make_job(required_ram_gb=8.0)
    env.host.ram_gb = 0.5  # refused on precondition; the restart-visible record must say why
    with pytest.raises(JobError):
        env.queue.run(job, lambda _j: None)
    assert persisted(env.root, job.job_id)["state"] == "failed"
    assert persisted(env.root, job.job_id)["error_kind"] == "out_of_memory"


def test_release_failure_does_not_record_completed(tmp_path: Path) -> None:
    env = build(tmp_path)
    env.lease_clock.step = 5.0  # a retry waiting on the still-held lease times out in a few polls
    env.worker.used_after_unload = None  # memory never drops
    env.nvml.used_mb = 9000
    env.worker.terminate_result = False  # and the worker cannot be confirmed dead
    env.nvml.used_mb = 1300

    def work(_j: Job) -> str:
        env.nvml.used_mb = 9000  # the job leaves memory behind
        return "done"

    job = make_job()
    with pytest.raises(JobError) as info:
        env.queue.run(job, work)
    assert info.value.kind is JobErrorKind.WORKER_UNREACHABLE
    assert persisted(env.root, job.job_id)["state"] == "failed"
    assert not lease_is_free(env), "an unconfirmed worker must keep the lease held"


# --- preconditions, each in isolation --------------------------------------------------


@pytest.mark.parametrize(
    ("case", "job_fields", "setup", "kind", "message_hint"),
    [
        ("vram", {"required_vram_gb": 8.0}, lambda e: setattr(e.nvml, "used_mb", 9500), JobErrorKind.OUT_OF_MEMORY, "vram"),
        ("host-ram-with-idle-gpu", {"required_ram_gb": 8.0}, lambda e: setattr(e.host, "ram_gb", 6.0),
         JobErrorKind.OUT_OF_MEMORY, "ram"),
        ("disk", {}, lambda e: setattr(e.host, "disk_gb", 5.0), JobErrorKind.DISK_EXHAUSTED, "disk"),
    ],
)
def test_preflight_refuses_each_precondition_in_isolation(
    tmp_path: Path, case: str, job_fields: dict[str, object], setup: Callable[[SimpleNamespace], None],
    kind: JobErrorKind, message_hint: str,
) -> None:
    env = build(tmp_path)
    env.nvml.used_mb = 1300  # idle GPU unless the case says otherwise
    setup(env)
    with pytest.raises(JobError) as info:
        env.queue.preflight(make_job(**job_fields))
    assert info.value.kind is kind, case
    assert message_hint in info.value.message.lower(), (case, info.value.message)


def test_preflight_passes_when_everything_fits(tmp_path: Path) -> None:
    env = build(tmp_path)
    env.queue.preflight(make_job(required_vram_gb=6.0, required_ram_gb=8.0, required_disk_gb=1.0))


def test_foreign_process_over_allowance_is_refused_and_named(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"foreign_vram_allowance_mb": 2048})
    env.nvml.processes = [ProcessUsage(pid=999, name="chrome.exe", used_mb=5000), ProcessUsage(1000, "dwm.exe", 100)]
    env.nvml.used_mb = 5100
    with pytest.raises(JobError) as info:
        env.queue.preflight(make_job(required_vram_gb=1.0))
    assert "chrome.exe" in info.value.message
    assert "dwm.exe" not in info.value.message


def test_foreign_process_within_allowance_is_accepted(tmp_path: Path) -> None:
    env = build(tmp_path, resources={"foreign_vram_allowance_mb": 2048})
    env.nvml.processes = [ProcessUsage(pid=999, name="chrome.exe", used_mb=1500)]
    env.nvml.used_mb = 1500
    env.queue.preflight(make_job(required_vram_gb=6.0))


def test_preconditions_are_rechecked_after_the_lease_is_acquired(tmp_path: Path) -> None:
    env = build(tmp_path)
    calls: list[str] = []
    env.lease.after_acquire = lambda: setattr(env.host, "ram_gb", 2.0)  # headroom vanished while queued
    job = make_job(required_ram_gb=8.0)
    with pytest.raises(JobError) as info:
        env.queue.run(job, lambda _j: calls.append("ran"))
    assert info.value.kind is JobErrorKind.OUT_OF_MEMORY
    assert calls == [], "the worker must not start when headroom is gone"
    assert persisted(env.root, job.job_id)["state"] == "failed"
    assert lease_is_free(env)


# --- retries follow platform/errors.py -------------------------------------------------


def test_retryable_error_is_retried_then_succeeds(tmp_path: Path) -> None:
    env = build(tmp_path)
    attempts: list[int] = []

    def flaky(j: Job) -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise JobError(JobErrorKind.PROVIDER_RATE_LIMIT, "429")
        return "ok"

    job = make_job()
    assert env.queue.run(job, flaky) == "ok"
    assert len(attempts) == 3
    assert persisted(env.root, job.job_id)["attempts"] == 3
    assert persisted(env.root, job.job_id)["state"] == "completed"


@pytest.mark.parametrize(
    "kind", [JobErrorKind.INVALID_MANIFEST, JobErrorKind.MISSING_MODEL_ARTIFACT, JobErrorKind.INTERNAL]
)
def test_terminal_error_is_not_retried(tmp_path: Path, kind: JobErrorKind) -> None:
    env = build(tmp_path)
    calls: list[int] = []

    def fail(_j: Job) -> None:
        calls.append(1)
        raise JobError(kind, "no")

    job = make_job()
    with pytest.raises(JobError) as info:
        env.queue.run(job, fail)
    assert info.value.kind is kind
    assert len(calls) == 1
    persisted_job = persisted(env.root, job.job_id)
    assert persisted_job["state"] == "failed"
    assert persisted_job["error_kind"] == kind.value
    assert lease_is_free(env)


def test_retryable_error_stops_at_the_policy_cap(tmp_path: Path) -> None:
    env = build(tmp_path)
    calls: list[int] = []

    def oom(_j: Job) -> None:
        calls.append(1)
        raise JobError(JobErrorKind.OUT_OF_MEMORY, "oom")

    with pytest.raises(JobError) as info:
        env.queue.run(make_job(), oom)
    assert len(calls) == JobErrorKind.OUT_OF_MEMORY.policy.retry.max_attempts  # type: ignore[union-attr]
    assert info.value.kind in (JobErrorKind.OUT_OF_MEMORY, JobErrorKind.BUDGET_EXCEEDED)


# --- FR-26 ceilings are terminal -------------------------------------------------------


def test_attempts_ceiling_is_terminal_budget_exceeded(tmp_path: Path) -> None:
    env = build(tmp_path, budget={"retries_per_shot": 1})
    calls: list[int] = []

    def limited(_j: Job) -> None:
        calls.append(1)
        raise JobError(JobErrorKind.PROVIDER_RATE_LIMIT, "429")  # policy alone would allow 5

    job = make_job(shot_id="shot-1")
    with pytest.raises(JobError) as info:
        env.queue.run(job, limited)
    assert info.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert not info.value.retryable
    assert len(calls) <= 2
    assert persisted(env.root, job.job_id)["error_kind"] == "budget_exceeded"


@pytest.mark.parametrize(
    "budget",
    [{"gpu_minutes_per_shot": 1.0}, {"per_shot_wall_clock_seconds": 60.0}],
    ids=["gpu-minutes", "wall-clock"],
)
def test_time_ceilings_stop_further_retries(tmp_path: Path, budget: dict[str, object]) -> None:
    env = build(tmp_path, budget=budget)
    calls: list[int] = []

    def slow_then_fail(_j: Job) -> None:
        calls.append(1)
        env.clock.now += 120.0  # two minutes of GPU time and wall clock
        raise JobError(JobErrorKind.PROVIDER_RATE_LIMIT, "429")  # retryable by policy

    job = make_job(shot_id="shot-1")
    with pytest.raises(JobError) as info:
        env.queue.run(job, slow_then_fail)
    assert info.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert len(calls) == 1, "a ceiling is terminal; the job must not be retried past it"
    assert persisted(env.root, job.job_id)["state"] == "failed"
    assert lease_is_free(env)


# --- watchdog --------------------------------------------------------------------------


def stalled_env(tmp_path: Path, *, terminate_ok: bool) -> SimpleNamespace:
    huge = {"gpu_minutes_per_shot": 1e9, "per_shot_wall_clock_seconds": 1e9, "gpu_minutes_per_episode": 1e9}
    env = build(tmp_path, queue_clock=Clock(step=1000.0), budget=huge)
    env.worker.terminate_result = terminate_ok
    return env


def test_watchdog_fails_a_stalled_job_after_confirming_the_worker_stopped(tmp_path: Path) -> None:
    env = stalled_env(tmp_path, terminate_ok=True)
    stop = threading.Event()
    calls: list[int] = []

    def stalled(j: Job) -> None:
        calls.append(1)
        j.progress = 0.001  # progress rate far below any sane floor over 1000 s ticks
        stop.wait(timeout=0.3)  # the "worker" keeps running until the queue stops it

    env.worker.terminate = lambda: (stop.set(), True)[1]  # type: ignore[method-assign]
    job = make_job(shot_id="s")
    with pytest.raises(JobError) as info:
        env.queue.run(job, stalled)
    assert "VRAM thrash suspected" in info.value.message
    assert stop.is_set(), "the attempt must be stopped before it is retired"
    assert persisted(env.root, job.job_id)["state"] == "failed"


def test_watchdog_does_not_retire_an_attempt_that_may_still_be_writing(tmp_path: Path) -> None:
    env = stalled_env(tmp_path, terminate_ok=False)
    release_worker = threading.Event()
    calls: list[int] = []

    def stalled(j: Job) -> None:
        calls.append(1)
        j.progress = 0.001
        release_worker.wait(timeout=0.3)  # never stops by itself within the watchdog window

    job = make_job(shot_id="s")
    try:
        with pytest.raises(JobError):
            env.queue.run(job, stalled)
        assert len(calls) == 1, "no second attempt may start beside a worker not confirmed dead"
        assert not lease_is_free(env), "the lease must stay held while the worker may still write"
        assert persisted(env.root, job.job_id)["state"] != "completed"
    finally:
        release_worker.set()


# --- cancellation, cleanup -------------------------------------------------------------


def test_cancellation_quarantines_partials_and_releases_the_lease(tmp_path: Path) -> None:
    env = build(tmp_path)
    partial = tmp_path / "partial.png"
    partial.write_bytes(b"half a frame")
    calls: list[int] = []

    def cancelled(_j: Job) -> None:
        calls.append(1)
        raise JobError(JobErrorKind.CANCELLED, "user cancelled")

    job = make_job()
    with pytest.raises(JobError) as info:
        env.queue.run(job, cancelled, partial_artifacts=[partial])
    assert info.value.kind is JobErrorKind.CANCELLED
    assert len(calls) == 1
    assert env.quarantine.received == [[partial]]
    assert (env.quarantine.destination / "partial.png").read_bytes() == b"half a frame"
    assert persisted(env.root, job.job_id)["state"] == "cancelled"
    assert lease_is_free(env)


def test_waiter_timing_out_never_releases_the_running_jobs_lease(tmp_path: Path) -> None:
    env = build(tmp_path)
    running, may_finish = threading.Event(), threading.Event()
    outcome: list[object] = []

    def long_job(_j: Job) -> str:
        running.set()
        may_finish.wait(timeout=5.0)
        return "first"

    def run_first() -> None:
        try:
            outcome.append(env.queue.run(make_job(job_id="a"), long_job))
        except Exception as error:  # noqa: BLE001  # surfaced by the assertion below
            outcome.append(error)

    first = threading.Thread(target=run_first)
    first.start()
    try:
        assert running.wait(timeout=5.0)
        env.lease.fail_acquire = True  # the second invocation times out waiting for the lease
        with pytest.raises(JobError):
            env.queue.run(make_job(job_id="b"), lambda _j: "second")
        env.lease.fail_acquire = False
        assert not lease_is_free(env), "the waiter's cleanup released a lease it never acquired"
    finally:
        may_finish.set()
        first.join(timeout=10)
    assert outcome == ["first"]
    assert lease_is_free(env)


def test_worker_exception_still_releases_the_lease(tmp_path: Path) -> None:
    env = build(tmp_path)

    def boom(_j: Job) -> None:
        raise RuntimeError("boom")

    with pytest.raises(JobError):
        env.queue.run(make_job(), boom)
    assert lease_is_free(env)


# --- production seams against the real library -----------------------------------------


def test_project_quarantine_moves_and_never_deletes(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    partial = tmp_path / "frame_0001.png"
    partial.write_bytes(b"bytes")
    destination = ProjectQuarantine(root).quarantine([partial])
    assert not partial.exists()
    moved = [p for p in destination.rglob("*") if p.is_file()]
    assert [p.read_bytes() for p in moved] == [b"bytes"]
    assert destination.resolve().is_relative_to(root.path)


def test_system_host_resources_reports_real_positive_figures(tmp_path: Path) -> None:
    host = SystemHostResources()
    assert host.free_ram_gb() > 0
    assert host.free_disk_gb(tmp_path) > 0
