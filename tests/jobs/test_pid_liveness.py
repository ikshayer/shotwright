"""Contract tests for process liveness checks used by the GPU lease."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from orchestrator.jobs.lease import pid_is_alive


@pytest.fixture
def exited_child() -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.fixture
def running_child() -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


def test_exited_child_is_dead_while_parent_retains_popen(exited_child: subprocess.Popen[bytes]) -> None:
    """poll observes exit without this test calling wait before the liveness check."""
    deadline = time.monotonic() + 5
    returncode = exited_child.poll()
    while returncode is None and time.monotonic() < deadline:
        time.sleep(0.001)
        returncode = exited_child.poll()

    assert returncode is not None, "child did not exit within the test deadline"
    assert pid_is_alive(exited_child.pid) is False


def test_running_child_is_alive_and_undisturbed(running_child: subprocess.Popen[bytes]) -> None:
    assert pid_is_alive(running_child.pid) is True
    assert running_child.poll() is None


def test_nonexistent_pid_is_dead() -> None:
    assert pid_is_alive(0x7FFFFFF0) is False


@pytest.mark.skipif(sys.platform != "win32", reason="PID 4 is the Windows System process only")
def test_system_pid_inspection_does_not_raise() -> None:
    # PID 4 is a protected system process on Windows; access can be restricted by policy.
    assert isinstance(pid_is_alive(4), bool)
