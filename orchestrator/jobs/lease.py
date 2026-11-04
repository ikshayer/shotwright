"""Persisted exclusive GPU lease with an NVML-verified release postcondition."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pynvml

from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings


class NvmlReader(Protocol):
    def used_memory_mb(self) -> float: ...

    def foreign_processes(self) -> list[tuple[str, float]]: ...


class PynvmlReader:
    """Small pynvml adapter; callers may inject a deterministic fake instead."""

    def __init__(self, device_index: int = 0) -> None:
        pynvml.nvmlInit()
        self._handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)

    def used_memory_mb(self) -> float:
        return pynvml.nvmlDeviceGetMemoryInfo(self._handle).used / (1024 * 1024)

    def foreign_processes(self) -> list[tuple[str, float]]:
        entries = []
        for proc in pynvml.nvmlDeviceGetComputeRunningProcesses(self._handle):
            try:
                name = pynvml.nvmlSystemGetProcessName(proc.pid).decode(errors="replace")
            except pynvml.NVMLError:
                name = f"PID {proc.pid}"
            entries.append((name, (proc.usedGpuMemory or 0) / (1024 * 1024)))
        return entries


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    owner_pid: int
    heartbeat: float
    expires_at: float


class GpuLease:
    """Single holder lease persisted under a project root.

    ``free_worker`` asks the current worker to unload (for example ComfyUI POST /free).
    ``terminate_worker`` and ``respawn_worker`` are injected lifecycle controls.
    """

    def __init__(
        self,
        project_root: ProjectRoot | Path | str,
        settings: Settings,
        *,
        nvml: NvmlReader | None = None,
        free_worker: Callable[[], None] | None = None,
        terminate_worker: Callable[[], None] | None = None,
        respawn_worker: Callable[[], None] | None = None,
        pid_alive: Callable[[int], bool] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        lease_path: str = "state/gpu_lease.json",
        ttl_seconds: float = 30.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self.root = project_root if isinstance(project_root, ProjectRoot) else ProjectRoot(project_root)
        self.settings = settings
        self.nvml = nvml or PynvmlReader()
        self.free_worker = free_worker or (lambda: None)
        self.terminate_worker = terminate_worker or (lambda: None)
        self.respawn_worker = respawn_worker or (lambda: None)
        self.pid_alive = pid_alive or _pid_alive
        self.clock, self.sleep = clock, sleep
        self.path = self.root.resolve(lease_path)
        self.relative_path = lease_path
        parent = lease_path.replace("\\", "/").rsplit("/", 1)[0] if "/" in lease_path.replace("\\", "/") else ""
        if parent:
            self.root.make_dir(parent)
        self.ttl_seconds = ttl_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._owned = False
        self._guard = threading.Lock()

    def acquire(self, *, timeout_seconds: float = 30.0) -> LeaseRecord:
        """Wait until exclusive ownership is persisted or raise WORKER_UNREACHABLE."""
        deadline = self.clock() + timeout_seconds
        while True:
            now = self.clock()
            record = LeaseRecord(os.getpid(), now, now + self.ttl_seconds)
            try:
                self.root.write_with(
                    self.relative_path,
                    lambda path, current=record: _create_record_exclusively(path, current),
                )
            except FileExistsError:
                reclaimed = self._reclaim_if_dead(now)
            else:
                self._owned = True
                return record
            if reclaimed:
                continue
            if now >= deadline:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, "Timed out waiting for GPU lease")
            self.sleep(min(self.poll_interval_seconds, max(0, deadline - now)))

    def heartbeat(self) -> LeaseRecord:
        """Refresh persisted owner heartbeat and TTL; caller must hold this lease."""
        with self._guard:
            current = self._read_record()
            if not self._owned or current is None or current.owner_pid != os.getpid():
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, "Cannot heartbeat a lease not owned by this process")
            now = self.clock()
            updated = LeaseRecord(os.getpid(), now, now + self.ttl_seconds)
            self.root.write_text(self.relative_path, json.dumps(_record_dict(updated)))
            return updated

    def release(self, *, floor_mb: float | None = None, timeout_seconds: float | None = None) -> None:
        """Request unload, poll NVML to the configured floor, then remove persisted lease.

        A timeout always terminates and respawns the worker before surfacing an error.
        """
        if not self._owned:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "Cannot release a lease not owned by this process")
        floor = self.settings.resources.vram_floor_mb if floor_mb is None else floor_mb
        timeout = self.settings.resources.vram_release_timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            self.free_worker()
            deadline = self.clock() + timeout
            while self.nvml.used_memory_mb() > floor:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError(
                        f"GPU release timed out: {self.nvml.used_memory_mb():.0f} MB remains above {floor:.0f} MB floor"
                    )
                self.sleep(min(self.poll_interval_seconds, remaining))
        except Exception as exc:
            try:
                self.terminate_worker()
            except Exception as terminate_error:  # noqa: BLE001 - preserve injected lifecycle failures
                exc = RuntimeError(f"{exc}; worker termination failed: {terminate_error}")
            try:
                self.respawn_worker()
            except Exception as respawn_error:  # noqa: BLE001 - preserve injected lifecycle failures
                exc = RuntimeError(f"{exc}; worker respawn failed: {respawn_error}")
            self._remove_owned_record()
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"GPU lease release failed; worker respawned: {exc}", cause=exc) from exc
        self._remove_owned_record()

    def _remove_owned_record(self) -> None:
        with self._guard:
            record = self._read_record()
            if record is not None and record.owner_pid == os.getpid():
                self.path.unlink(missing_ok=True)
            self._owned = False

    def _read_record(self) -> LeaseRecord | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return LeaseRecord(int(raw["owner_pid"]), float(raw["heartbeat"]), float(raw["expires_at"]))
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise JobError(JobErrorKind.INTERNAL, f"GPU lease record is corrupt: {exc}", cause=exc) from exc

    def _reclaim_if_dead(self, now: float) -> bool:
        record = self._read_record()
        if record is None or now <= record.expires_at:
            return False
        if self.pid_alive(record.owner_pid):
            return False
        # Re-read before unlinking to avoid reclaiming a refreshed/replaced holder.
        latest = self._read_record()
        if latest == record and not self.pid_alive(record.owner_pid):
            self.path.unlink(missing_ok=True)
            return True
        return False


def _record_dict(record: LeaseRecord) -> dict[str, int | float]:
    return {"owner_pid": record.owner_pid, "heartbeat": record.heartbeat, "expires_at": record.expires_at}


def _create_record_exclusively(path: Path, record: LeaseRecord) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(_record_dict(record), stream)


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
