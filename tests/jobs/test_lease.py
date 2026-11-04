from __future__ import annotations

import json
import threading
import time

import pytest

from orchestrator.jobs.lease import GpuLease
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings


class FakeNvml:
    def __init__(self, used: list[float] | None = None) -> None:
        self.readings = list(used or [0])
        self.last = self.readings[-1]

    def used_memory_mb(self) -> float:
        if self.readings:
            self.last = self.readings.pop(0)
        return self.last

    def foreign_processes(self) -> list[tuple[str, float]]:
        return []


def make_lease(tmp_path, **kwargs) -> GpuLease:
    return GpuLease(tmp_path, Settings(), nvml=FakeNvml(), sleep=time.sleep, **kwargs)


def test_lease_acquired_exclusively_and_second_waits(tmp_path) -> None:
    first = make_lease(tmp_path)
    first.acquire()
    acquired = threading.Event()

    def acquire_second() -> None:
        second = make_lease(tmp_path)
        second.acquire(timeout_seconds=0.2)
        acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.02)
    assert not acquired.is_set()
    first.release()
    thread.join(timeout=1)
    assert acquired.is_set()


def test_stale_lease_reclaimed_only_when_holder_is_dead(tmp_path) -> None:
    alive = {1234: True}
    lease = make_lease(tmp_path, pid_alive=lambda pid: alive.get(pid, False), clock=lambda: 100)
    lease.root.write_text("state/gpu_lease.json", json.dumps({"owner_pid": 1234, "heartbeat": 1, "expires_at": 2}))
    with pytest.raises(JobError, match="Timed out"):
        lease.acquire(timeout_seconds=0,)
    alive[1234] = False
    # Reaching a dead holder's expired record reclaims it and acquires ownership.
    lease.acquire(timeout_seconds=0)
    assert lease._owned


def test_release_polls_nvml_until_floor(tmp_path) -> None:
    nvml = FakeNvml([900, 600])
    lease = GpuLease(tmp_path, Settings(), nvml=nvml, sleep=lambda _: None, poll_interval_seconds=0.001)
    lease.acquire()
    lease.release(floor_mb=700, timeout_seconds=1)
    assert not lease.path.exists()


def test_failed_release_terminates_and_respawns(tmp_path) -> None:
    calls: list[str] = []
    lease = GpuLease(tmp_path, Settings(), nvml=FakeNvml([900]), sleep=lambda _: None,
                     poll_interval_seconds=0.001, terminate_worker=lambda: calls.append("terminate"),
                     respawn_worker=lambda: calls.append("respawn"))
    lease.acquire()
    with pytest.raises(JobError) as caught:
        lease.release(floor_mb=10, timeout_seconds=0)
    assert caught.value.kind is JobErrorKind.WORKER_UNREACHABLE
    assert calls == ["terminate", "respawn"]

