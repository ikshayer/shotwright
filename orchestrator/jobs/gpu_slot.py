"""Stable cross-process mutex for heavyweight GPU execution."""

from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path
from typing import Self

from orchestrator.platform.containment import ProjectRoot


class GpuSlotBusy(TimeoutError):
    """The shared heavyweight GPU slot was not available before the deadline."""


class GpuExecutionSlot:
    """An OS-backed lock on a stable, project-contained file inode."""

    def __init__(self, root: ProjectRoot, relative: str = "_work/gpu_execution.lock") -> None:
        self.root = root
        self.relative = relative
        root.make_dir(str(Path(relative).parent).replace("\\", "/"))
        path = root.resolve(relative)
        if not path.exists():
            try:
                root.write_with(relative, lambda target: target.open("xb").write(b"0"))
            except (FileExistsError, PermissionError):
                if not root.resolve(relative).exists():
                    raise
        self._path = root.resolve(relative)
        self._handle = None
        self._state_lock = threading.Lock()

    def acquire(self, timeout_seconds: float = 0) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("GPU slot timeout must be a finite nonnegative number")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("GPU slot timeout must be a finite nonnegative number")
        with self._state_lock:
            if self._handle is not None:
                raise RuntimeError("this GpuExecutionSlot instance already holds the GPU slot")
        deadline = time.monotonic() + timeout
        while True:
            handle = self._path.open("r+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if time.monotonic() >= deadline:
                    raise GpuSlotBusy("shared GPU execution slot is busy") from exc
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                continue
            with self._state_lock:
                if self._handle is not None:
                    # Defensive against concurrent acquire calls on one instance.
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                    raise RuntimeError("this GpuExecutionSlot instance already holds the GPU slot")
                self._handle = handle
            return

    def release(self) -> None:
        with self._state_lock:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> Self:
        self.acquire(0)
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
