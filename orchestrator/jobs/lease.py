"""Persisted exclusive lease for heavyweight GPU work."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

import psutil

from orchestrator.jobs.gpu_slot import GpuExecutionSlot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings


@dataclass(frozen=True, slots=True)
class MemoryReading:
    used_mb: float
    total_mb: float
    free_mb: float


@dataclass(frozen=True, slots=True)
class ProcessUsage:
    pid: int
    name: str
    used_mb: float


class NvmlProbe(Protocol):
    def device_memory(self) -> MemoryReading: ...
    def process_usage(self) -> list[ProcessUsage]: ...


class WorkerControl(Protocol):
    def unload(self) -> None: ...
    def terminate(self) -> bool: ...
    def respawn(self) -> int: ...
    def pid(self) -> int | None: ...


class PynvmlProbe:
    """NVML-backed probe; total memory is always read from the device handle."""

    def __init__(self, device_index: int = 0, pynvml_module: object | None = None) -> None:
        if pynvml_module is None:
            import pynvml as pynvml_module  # type: ignore[import-not-found]
        self._nvml = pynvml_module
        self._nvml.nvmlInit()
        self._handle = self._nvml.nvmlDeviceGetHandleByIndex(device_index)

    def device_memory(self) -> MemoryReading:
        info = self._nvml.nvmlDeviceGetMemoryInfo(self._handle)
        total = info.total / (1024 * 1024)
        used = info.used / (1024 * 1024)
        free = info.free / (1024 * 1024)
        if total <= 0 or abs((used + free) - total) > 2:
            raise RuntimeError("NVML returned invalid device memory totals")
        return MemoryReading(used_mb=used, total_mb=total, free_mb=free)

    def process_usage(self) -> list[ProcessUsage]:
        rows: dict[int, float] = {}
        for getter in (
            getattr(self._nvml, "nvmlDeviceGetComputeRunningProcesses", None),
            getattr(self._nvml, "nvmlDeviceGetGraphicsRunningProcesses", None),
        ):
            if getter is None:
                continue
            try:
                processes = getter(self._handle)
            except Exception as exc:
                # NVML reports "no permission" for graphics process lists on some drivers.
                code = getattr(exc, "value", None)
                if code in {getattr(self._nvml, "NVML_ERROR_NOT_SUPPORTED", object()),
                            getattr(self._nvml, "NVML_ERROR_NO_PERMISSION", object())}:
                    continue
                raise
            for process in processes:
                used = getattr(process, "usedGpuMemory", None)
                if used is not None and used >= 0:
                    rows[int(process.pid)] = max(rows.get(int(process.pid), 0.0), used / (1024 * 1024))
        result = []
        for pid, used_mb in rows.items():
            try:
                raw_name = self._nvml.nvmlSystemGetProcessName(pid)
                name = raw_name.decode("utf-8", errors="replace") if isinstance(raw_name, bytes) else str(raw_name)
            except Exception:  # noqa: BLE001 - process metadata can disappear between NVML calls.
                name = f"PID {pid}"
            result.append(ProcessUsage(pid, name, used_mb))
        return sorted(result, key=lambda row: row.pid)


class ComfyWorkerControl:
    """Supervise a ComfyUI worker launched by an explicit command."""

    def __init__(self, command: Sequence[str], *, port: int, cwd: Path | None = None) -> None:
        if not command:
            raise ValueError("ComfyUI launch command must not be empty")
        self._command = tuple(command)
        self._port = port
        self._cwd = cwd
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def pid(self) -> int | None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._process.pid
            return None

    def unload(self) -> None:
        request = urllib.request.Request(f"http://127.0.0.1:{self._port}/free", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5):
                return
        except OSError as exc:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"ComfyUI unload request failed: {exc}", cause=exc) from exc

    def terminate(self) -> bool:
        with self._lock:
            process = self._process
        if process is None:
            return True
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    return False
        return process.poll() is not None

    def respawn(self) -> int:
        if not self.terminate():
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "cannot respawn while the old ComfyUI worker is alive")
        process = subprocess.Popen(self._command, cwd=self._cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self._lock:
            self._process = process
        return process.pid


class _ProcessLock:
    """Cross-process lock using the OS byte-range lock APIs."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def __enter__(self) -> Self:
        while True:
            try:
                self._handle = open(self._path, "a+b")
                if os.name == "nt":
                    import msvcrt
                    self._handle.seek(0)
                    if self._handle.read(1) == b"":
                        self._handle.seek(0)
                        self._handle.write(b"0")
                        self._handle.flush()
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
                return self
            except OSError:
                if self._handle is not None:
                    self._handle.close()
                    self._handle = None
                time.sleep(0.005)

    def __exit__(self, *_: object) -> None:
        if self._handle is not None:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()


def pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` is a process that is still running.

    Delegates to psutil, already a dependency, instead of hand-rolling the platform
    check. The Windows path this replaces asked ``OpenProcess`` only whether a handle
    could be opened — and on Windows an exited process keeps an openable handle for as
    long as any parent still holds one, so a dead child reported itself alive. That made
    a GPU lease held by a dead process look live, and on a single-GPU machine an
    unreclaimable lease is a hang rather than a slowdown. Measured on this machine: a
    child observed exiting with returncode 0 was reported alive by the old check and
    dead by psutil.

    A terminated-but-unreaped process is dead for leasing purposes, so a zombie counts
    as dead. When the process exists but cannot be inspected it is reported **alive**:
    refusing to reclaim a lease risks a stall, while reclaiming one whose owner is in
    fact running would put two writers on the same project, which the concurrency
    contract forbids outright.
    """
    if pid <= 0:
        # 0 is not a reclaimable owner on either platform: on POSIX, signalling it
        # addresses the whole process group, and on Windows it is the idle process.
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


class GpuLease:
    def __init__(self, root: ProjectRoot, settings: Settings, *, nvml: NvmlProbe,
                 worker: WorkerControl, pid_alive: Callable[[int], bool] = pid_is_alive,
                 clock: Callable[[], float] = time.monotonic, ttl_seconds: float = 30.0,
                 execution_slot: GpuExecutionSlot | None = None) -> None:
        self.root, self.settings, self.nvml, self.worker = root, settings, nvml, worker
        self._pid_alive, self._clock, self.ttl_seconds = pid_alive, clock, ttl_seconds
        self.execution_slot = execution_slot
        self._path = "jobs/gpu_lease.json"
        root.make_dir("jobs")
        if not root.resolve("jobs/gpu_lease.lock").exists():
            try:
                # The OS lock is attached to this file's identity; replacing it atomically
                # could let another process lock a new file while this process holds the old one.
                root.write_with("jobs/gpu_lease.lock", lambda path: path.open("xb").close())
            except (FileExistsError, PermissionError):
                if not root.resolve("jobs/gpu_lease.lock").exists():
                    raise
        self._lock_path = root.resolve("jobs/gpu_lease.lock")
        self._owned = False
        self._record: dict[str, object] | None = None
        self.baseline_mb = 0.0

    def acquire(self, timeout_seconds: float) -> None:
        slot_acquired = False
        if self.execution_slot is not None:
            self.execution_slot.acquire(timeout_seconds)
            slot_acquired = True
        try:
            deadline = self._clock() + timeout_seconds
            while True:
                with _ProcessLock(self._lock_path):
                    record = self._read()
                    now = self._clock()
                    if record is not None:
                        pid = record.get("pid")
                        expired = now - float(record.get("heartbeat", now)) > float(record.get("ttl", self.ttl_seconds))
                        dead = isinstance(pid, int) and not self._safe_alive(pid)
                        if expired and dead:
                            self._delete_if_same(record)
                            record = None
                    if record is None:
                        pid = self.worker.pid()
                        if pid is None:
                            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "GPU worker has no live PID")
                        self.baseline_mb = self.nvml.device_memory().used_mb
                        new = {"pid": pid, "heartbeat": now, "ttl": self.ttl_seconds, "token": os.urandom(16).hex()}
                        self.root.write_text(self._path, json.dumps(new, sort_keys=True))
                        self._record, self._owned = new, True
                        return
                if self._clock() >= deadline:
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "timed out waiting for the GPU lease")
                time.sleep(min(0.01, max(0.0, deadline - self._clock())))
        except BaseException:
            if slot_acquired and not self._owned:
                self.execution_slot.release()
            raise

    def heartbeat(self) -> None:
        if not self._owned or self._record is None:
            raise RuntimeError("cannot heartbeat a lease this instance does not own")
        with _ProcessLock(self._lock_path):
            current = self._read()
            if current is None or current.get("token") != self._record.get("token"):
                self._owned = False
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, "GPU lease ownership was lost")
            current["heartbeat"] = self._clock()
            self.root.write_text(self._path, json.dumps(current, sort_keys=True))
            self._record = current

    def release(self, floor_mb: float, timeout_seconds: float, *, after_cuda_error: bool = False) -> None:
        if not self._owned:
            return
        worker_stopped = self.worker.pid() is None
        if not worker_stopped:
            self.worker.unload()
        threshold_mb = self.baseline_mb + floor_mb
        deadline = self._clock() + timeout_seconds
        below = worker_stopped
        while True:
            if self.nvml.device_memory().used_mb <= threshold_mb:
                below = True
                break
            if self._clock() >= deadline:
                break
            time.sleep(min(0.01, max(0.0, deadline - self._clock())))
        if worker_stopped or after_cuda_error or not below:
            if not self.worker.terminate():
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "GPU worker termination was not confirmed; lease remains held")
            self.worker.respawn()
        self._drop_owned()
        if self.execution_slot is not None:
            self.execution_slot.release()

    def __enter__(self) -> Self:
        self.acquire(self.settings.resources.vram_release_timeout_seconds)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        after_cuda_error = isinstance(exc, JobError) and (
            exc.kind is JobErrorKind.OUT_OF_MEMORY or "cuda error" in exc.message.lower()
        )
        self.release(
            self.settings.resources.vram_floor_mb,
            self.settings.resources.vram_release_timeout_seconds,
            after_cuda_error=after_cuda_error,
        )

    def _safe_alive(self, pid: int) -> bool:
        try:
            return self._pid_alive(pid)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 87:
                return False
            raise

    def _read(self) -> dict[str, object] | None:
        try:
            data = json.loads(self.root.read_text(self._path, encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
            raise JobError(JobErrorKind.INTERNAL, "GPU lease file is corrupt")
        return data

    def _delete_if_same(self, expected: dict[str, object]) -> None:
        current = self._read()
        if current == expected:
            self.root.resolve(self._path).unlink(missing_ok=True)

    def _drop_owned(self) -> None:
        if self._record is None:
            if self._owned:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, "GPU lease record is unavailable; shared slot remains held")
            return
        with _ProcessLock(self._lock_path):
            current = self._read()
            if current != self._record:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "GPU lease ownership cannot be verified; shared slot remains held")
            self._delete_if_same(self._record)
            if self._read() is not None:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "GPU lease record removal was not verified; shared slot remains held")
        self._record, self._owned = None, False

