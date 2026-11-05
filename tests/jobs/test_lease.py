"""GpuLease and the production seam implementations, tested from CONTRACT.md only."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.jobs.lease import (
    GpuLease,
    MemoryReading,
    ProcessUsage,
    PynvmlProbe,
)
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings, parse_settings

WORKER_PID = 4242
FLOOR_MB = 1024  # the allowance; the effective floor is baseline + this
TOTAL_MB = 11940


@pytest.fixture(autouse=True)
def _cap_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract: no sleep above ~10 ms. Clamp any polling/backoff sleep."""
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: real_sleep(min(s, 0.001)))


class Clock:
    """Injectable clock. `step` makes every read advance time, so timeouts fire without waiting."""

    def __init__(self, now: float = 1000.0, step: float = 0.01) -> None:
        self.now, self.step = now, step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class FakeNvml:
    def __init__(self, used_mb: float = 1300.0, total_mb: float = TOTAL_MB) -> None:
        self.used_mb, self.total_mb = used_mb, total_mb
        self.processes: list[ProcessUsage] = []

    def device_memory(self) -> MemoryReading:
        return MemoryReading(self.used_mb, self.total_mb, self.total_mb - self.used_mb)

    def process_usage(self) -> list[ProcessUsage]:
        return list(self.processes)


class FakeWorker:
    """`used_after_unload` None means unload frees nothing."""

    def __init__(
        self,
        nvml: FakeNvml,
        *,
        pid: int = WORKER_PID,
        used_after_unload: float | None = None,
        terminate_result: bool = True,
        idle_mb: float = 1300.0,
    ) -> None:
        self.nvml, self._pid = nvml, pid
        self.used_after_unload, self.terminate_result, self.idle_mb = used_after_unload, terminate_result, idle_mb
        self.calls: list[str] = []

    def pid(self) -> int | None:
        return self._pid

    def unload(self) -> None:
        self.calls.append("unload")
        if self.used_after_unload is not None:
            self.nvml.used_mb = self.used_after_unload

    def terminate(self) -> bool:
        self.calls.append("terminate")
        return self.terminate_result

    def respawn(self) -> int:
        self.calls.append("respawn")
        self.nvml.used_mb = self.idle_mb
        self._pid += 1
        return self._pid


def make_settings(**sections: dict[str, object]) -> Settings:
    return parse_settings(dict(sections))


def make_lease(
    root: ProjectRoot,
    nvml: FakeNvml,
    worker: FakeWorker,
    *,
    clock: Clock | None = None,
    pid_alive: Callable[[int], bool] = lambda _pid: True,
    ttl: float = 30.0,
) -> GpuLease:
    return GpuLease(
        root, make_settings(), nvml=nvml, worker=worker, pid_alive=pid_alive,
        clock=clock or Clock(), ttl_seconds=ttl,
    )


@pytest.fixture
def root(tmp_path: Path) -> ProjectRoot:
    return ProjectRoot(tmp_path)


def is_held(root: ProjectRoot, clock: Clock) -> bool:
    """True if a fresh contender facing a live-looking holder cannot take the lease."""
    nvml = FakeNvml()
    contender = make_lease(root, nvml, FakeWorker(nvml, pid=777), clock=clock)
    try:
        contender.acquire(0.2)
    except JobError as error:
        assert error.kind is JobErrorKind.WORKER_UNREACHABLE
        return True
    contender.release(FLOOR_MB, 0.5)
    return False


def json_values(root: ProjectRoot) -> list[object]:
    """Every scalar in every JSON file under the root (format-agnostic view of persisted state)."""
    values: list[object] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        else:
            values.append(node)

    for path in root.path.rglob("*"):
        if path.is_file():
            try:
                walk(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, UnicodeDecodeError):
                continue
    return values


# --- exclusivity ----------------------------------------------------------------------


def test_second_acquire_times_out_while_held(root: ProjectRoot) -> None:
    clock = Clock()
    nvml = FakeNvml()
    holder = make_lease(root, nvml, FakeWorker(nvml), clock=clock)
    holder.acquire(1.0)
    assert is_held(root, clock)
    holder.release(FLOOR_MB, 1.0)
    assert not is_held(root, clock)


def test_persisted_lease_carries_worker_pid_not_orchestrator(root: ProjectRoot) -> None:
    nvml = FakeNvml()
    make_lease(root, nvml, FakeWorker(nvml)).acquire(1.0)
    values = json_values(root)
    assert WORKER_PID in values
    assert os.getpid() not in values


# --- stale reclamation: both conditions are required -----------------------------------


@pytest.mark.parametrize(
    ("heartbeat_expired", "pid_is_alive", "reclaims"),
    [
        (True, False, True),
        (True, True, False),  # old heartbeat alone must never reclaim
        (False, False, False),  # dead pid alone must not reclaim a fresh lease
        (False, True, False),
    ],
)
def test_reclaim_requires_expired_heartbeat_and_dead_pid(
    root: ProjectRoot, heartbeat_expired: bool, pid_is_alive: bool, reclaims: bool
) -> None:
    clock = Clock(step=0.001)
    nvml = FakeNvml()
    make_lease(root, nvml, FakeWorker(nvml, pid=111), clock=clock).acquire(1.0)
    if heartbeat_expired:
        clock.now += 500.0
    nvml2 = FakeNvml()
    challenger = make_lease(
        root, nvml2, FakeWorker(nvml2, pid=222), clock=clock, pid_alive=lambda _pid: pid_is_alive
    )
    if reclaims:
        challenger.acquire(1.0)
        assert 222 in json_values(root)
    else:
        with pytest.raises(JobError) as info:
            challenger.acquire(0.2)
        assert info.value.kind is JobErrorKind.WORKER_UNREACHABLE


def test_heartbeat_keeps_lease_from_expiring(root: ProjectRoot) -> None:
    clock = Clock(step=0.001)
    nvml = FakeNvml()
    holder = make_lease(root, nvml, FakeWorker(nvml, pid=111), clock=clock)
    holder.acquire(1.0)
    clock.now += 25.0
    holder.heartbeat()
    clock.now += 25.0  # 50 s since acquire but only 25 s since heartbeat (ttl 30)
    nvml2 = FakeNvml()
    challenger = make_lease(root, nvml2, FakeWorker(nvml2, pid=222), clock=clock, pid_alive=lambda _p: False)
    with pytest.raises(JobError):
        challenger.acquire(0.2)


def test_default_pid_alive_treats_unknown_pid_as_dead(root: ProjectRoot) -> None:
    """Windows raises OSError (winerror 87) for an unknown pid; reclaim must not propagate it."""
    clock = Clock(step=0.001)
    nvml = FakeNvml()
    nonexistent = 0x7FFFFFF0
    make_lease(root, nvml, FakeWorker(nvml, pid=nonexistent), clock=clock).acquire(1.0)
    clock.now += 500.0
    nvml2 = FakeNvml()
    challenger = GpuLease(
        root, make_settings(), nvml=nvml2, worker=FakeWorker(nvml2, pid=222), clock=clock, ttl_seconds=30.0
    )
    challenger.acquire(1.0)  # default pid_alive; must reclaim, not raise OSError
    assert 222 in json_values(root)


def test_racing_reclaimers_exactly_one_wins_and_winner_lease_survives(root: ProjectRoot) -> None:
    clock = Clock(step=0.0005)
    nvml = FakeNvml()
    make_lease(root, nvml, FakeWorker(nvml, pid=111), clock=clock).acquire(1.0)
    clock.now += 500.0

    both_have_read_stale = threading.Barrier(2)

    def pid_alive(pid: int) -> bool:
        if pid == 111:
            try:  # rendezvous so both contenders have judged the same stale record
                both_have_read_stale.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass
            return False
        return True

    outcomes: dict[int, str] = {}
    leases: dict[int, GpuLease] = {}

    def contend(pid: int) -> None:
        n = FakeNvml()
        leases[pid] = make_lease(root, n, FakeWorker(n, pid=pid), clock=clock, pid_alive=pid_alive)
        try:
            leases[pid].acquire(0.5)
            outcomes[pid] = "won"
        except JobError as error:
            outcomes[pid] = error.kind.value
        except Exception as error:  # noqa: BLE001  # a crash is a distinct, reported outcome
            outcomes[pid] = f"crashed:{type(error).__name__}"

    threads = [threading.Thread(target=contend, args=(pid,)) for pid in (201, 202)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(outcomes.values()) == sorted(["won", JobErrorKind.WORKER_UNREACHABLE.value]), outcomes
    winner = next(pid for pid, outcome in outcomes.items() if outcome == "won")
    assert winner in json_values(root)
    # the loser did not delete the winner's freshly written lease:
    assert is_held(root, clock)
    leases[winner].release(FLOOR_MB, 1.0)
    assert not is_held(root, clock)


# --- release: measured postcondition ---------------------------------------------------


@pytest.mark.parametrize(
    ("case", "baseline", "after_unload", "terminate_ok", "cuda", "expect_calls", "expect_raise"),
    [
        ("healthy", 1300, 1500, True, False, ["unload"], False),
        # idle baseline drifted 1.3 -> 2.7 GB: an absolute 1024 MB floor would time out here
        ("drifted-baseline-healthy", 2700, 3200, True, False, ["unload"], False),
        ("timeout-confirmed-dead", 1300, 9000, True, False, ["unload", "terminate", "respawn"], False),
        ("drifted-still-over", 2700, 3900, True, False, ["unload", "terminate", "respawn"], False),
        ("timeout-not-dead", 1300, 9000, False, False, ["unload", "terminate"], True),
        # CUDA error forces replacement even though memory is already below the floor
        ("cuda-below-floor", 1300, 1300, True, True, ["terminate", "respawn"], False),
        ("cuda-not-dead", 1300, 1300, False, True, ["terminate"], True),
    ],
)
def test_release_postcondition(
    root: ProjectRoot,
    case: str,
    baseline: float,
    after_unload: float,
    terminate_ok: bool,
    cuda: bool,
    expect_calls: list[str],
    expect_raise: bool,
) -> None:
    clock = Clock()
    nvml = FakeNvml(used_mb=baseline)
    worker = FakeWorker(nvml, used_after_unload=after_unload, terminate_result=terminate_ok, idle_mb=baseline)
    lease = make_lease(root, nvml, worker, clock=clock)
    lease.acquire(1.0)

    if expect_raise:
        with pytest.raises(JobError) as info:
            lease.release(FLOOR_MB, 0.5, after_cuda_error=cuda)
        assert info.value.kind is JobErrorKind.WORKER_UNREACHABLE
    else:
        lease.release(FLOOR_MB, 0.5, after_cuda_error=cuda)

    # unload is optional on the CUDA path (the worker is being replaced anyway); the rest is exact
    observed = [c for c in worker.calls if c != "unload"]
    assert observed == [c for c in expect_calls if c != "unload"], (case, worker.calls)
    if "unload" in expect_calls:
        assert worker.calls[0] == "unload", (case, worker.calls)
    # a lease is held exactly when the worker could not be confirmed gone
    assert is_held(root, clock) is expect_raise, case


def test_release_orders_unload_then_terminate_then_respawn(root: ProjectRoot) -> None:
    nvml = FakeNvml()
    worker = FakeWorker(nvml)
    lease = make_lease(root, nvml, worker)
    lease.acquire(1.0)
    nvml.used_mb = 9000
    lease.release(FLOOR_MB, 0.3)
    assert worker.calls == ["unload", "terminate", "respawn"]


# --- seam implementations against stubs of the underlying library ----------------------


def fake_pynvml(total_mib: int, used_mib: int, procs: list[tuple[int, str, int]]) -> SimpleNamespace:
    mib = 1024 * 1024
    process_objs = [SimpleNamespace(pid=p, usedGpuMemory=m * mib) for p, _n, m in procs]
    names = {p: n for p, n, _m in procs}
    return SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetHandleByIndex=lambda _i: object(),
        nvmlDeviceGetMemoryInfo=lambda _h: SimpleNamespace(
            total=total_mib * mib, used=used_mib * mib, free=(total_mib - used_mib) * mib
        ),
        nvmlDeviceGetComputeRunningProcesses=lambda _h: list(process_objs),
        nvmlDeviceGetGraphicsRunningProcesses=lambda _h: list(process_objs),
        nvmlSystemGetProcessName=lambda pid: names[pid],
    )


def test_pynvml_probe_reports_total_and_free_from_the_device() -> None:
    probe = PynvmlProbe(pynvml_module=fake_pynvml(12288, 3072, []))
    reading = probe.device_memory()
    assert reading.total_mb > 0, "a probe without a total accessor makes free VRAM zero"
    assert 12000 < reading.total_mb < 13000
    assert 3000 < reading.used_mb < 3300
    assert reading.free_mb == pytest.approx(reading.total_mb - reading.used_mb, rel=1e-3)
    assert 9000 < reading.free_mb < 9800


def test_pynvml_probe_reports_processes_by_pid_name_and_usage() -> None:
    probe = PynvmlProbe(pynvml_module=fake_pynvml(12288, 3072, [(321, "chrome.exe", 500)]))
    usage = probe.process_usage()
    assert [u.pid for u in usage] == [321]
    assert usage[0].name, "a refusal must be able to name the process"
    assert 480 < usage[0].used_mb < 540


def test_lease_driven_by_the_real_probe_class(root: ProjectRoot) -> None:
    probe = PynvmlProbe(pynvml_module=fake_pynvml(12288, 1300, []))
    worker = FakeWorker(FakeNvml())
    lease = GpuLease(root, make_settings(), nvml=probe, worker=worker, clock=Clock(), pid_alive=lambda _p: True)
    lease.acquire(1.0)
    assert probe.device_memory().free_mb > 0
