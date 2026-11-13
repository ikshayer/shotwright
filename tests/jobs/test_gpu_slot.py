"""OS-lock tests: separate native Python processes, no fake lock implementation."""

import os
import subprocess
import sys
import time

import pytest

from orchestrator.jobs.gpu_slot import GpuExecutionSlot, GpuSlotBusy
from orchestrator.platform.containment import ProjectRoot


@pytest.fixture
def root(tmp_path):
    return ProjectRoot(tmp_path)


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), -float("inf")])
def test_invalid_timeout(root, timeout):
    slot = GpuExecutionSlot(root)
    with pytest.raises((ValueError, TypeError)):
        slot.acquire(timeout_seconds=timeout)


def test_distinct_handles_and_stable_inode(root):
    first, second = GpuExecutionSlot(root), GpuExecutionSlot(root)
    first.acquire()
    path = root.resolve("_work/gpu_execution.lock")
    before = path.stat()
    try:
        with pytest.raises((RuntimeError, ValueError, GpuSlotBusy)):
            first.acquire()
        with pytest.raises(GpuSlotBusy):
            second.acquire()
        assert path.stat().st_ino == before.st_ino
    finally:
        first.release()
    assert path.exists(), "Do not unlink the OS mutex inode on release"
    with second:
        assert path.stat().st_ino == before.st_ino


CHILD = r"""
import sys
from orchestrator.jobs.gpu_slot import GpuExecutionSlot, GpuSlotBusy
from orchestrator.platform.containment import ProjectRoot
slot = GpuExecutionSlot(ProjectRoot(sys.argv[1]))
try:
    slot.acquire(timeout_seconds=float(sys.argv[2]))
except GpuSlotBusy:
    print('BUSY', flush=True)
    raise SystemExit(23)
print('HELD', flush=True)
sys.stdin.readline()
slot.release()
"""


def child(root, timeout=0):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.Popen(
        [sys.executable, "-B", "-c", CHILD, str(root.path), str(timeout)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_real_process_contends_and_exit_releases(root):
    holder = child(root)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        contender = child(root)
        out, err = contender.communicate(timeout=8)
        assert contender.returncode == 23, (out, err)
        assert out.strip() == "BUSY"
        slot = GpuExecutionSlot(root)
        started = time.monotonic()
        with pytest.raises(GpuSlotBusy):
            slot.acquire(timeout_seconds=0.15)
        assert 0.10 <= time.monotonic() - started < 3
        holder.terminate()
        holder.communicate(timeout=8)
        slot.acquire(timeout_seconds=1)
        slot.release()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=8)


def test_context_releases_on_baseexception(root):
    for exc in (KeyboardInterrupt, SystemExit):
        with pytest.raises(exc), GpuExecutionSlot(root):
            raise exc()
        with GpuExecutionSlot(root):
            pass


def test_lease_failed_cleanup_holds_real_slot_until_verified_release(root):
    from orchestrator.jobs.lease import GpuLease, MemoryReading
    from orchestrator.platform.errors import JobError
    from orchestrator.platform.settings import Settings

    class Probe:
        used = 1300

        def device_memory(self):
            return MemoryReading(self.used, 12000, 12000 - self.used)

        def process_usage(self):
            return []

    class Worker:
        safe = False

        def pid(self):
            return os.getpid()

        def unload(self):
            pass

        def terminate(self):
            return self.safe

        def respawn(self):
            probe.used = 1300
            return os.getpid()

    probe, worker = Probe(), Worker()
    slot = GpuExecutionSlot(root)
    lease = GpuLease(root, Settings(), nvml=probe, worker=worker, execution_slot=slot)
    lease.acquire(0.2)
    contender = GpuExecutionSlot(root)
    try:
        with pytest.raises(GpuSlotBusy):
            contender.acquire()
        probe.used = 9000
        with pytest.raises(JobError):
            lease.release(1024, 0.05)
        with pytest.raises(GpuSlotBusy):
            contender.acquire()
    finally:
        worker.safe = True
        probe.used = 1300
        lease.release(1024, 0.2)
    contender.acquire()
    contender.release()
