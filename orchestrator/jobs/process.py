"""Lease-free process supervision for CPU jobs."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable

from orchestrator.platform.errors import JobError, JobErrorKind


class CpuProcessSupervisor:
    """Own at most one child process and make stop requests permanent."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self.stop_reason: JobErrorKind | None = None

    def launch(
        self,
        command: list[str],
        *,
        popen: Callable[..., subprocess.Popen[bytes]] | None = None,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        with self._lock:
            if self.stop_reason is not None:
                raise JobError(self.stop_reason, "CPU process launch refused after the job was stopped")
            factory = subprocess.Popen if popen is None else popen
            process = factory(command, **kwargs)  # type: ignore[arg-type]
            self._process = process
            return process

    def clear_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def stopped(self) -> bool:
        with self._lock:
            return self.stop_reason is not None

    def stop_for(self, reason: JobErrorKind) -> bool:
        """Fence future launches, terminate the child, then kill after five seconds."""
        with self._stop_lock:
            with self._lock:
                if self.stop_reason is None:
                    self.stop_reason = reason
                process = self._process
            if process is None or process.poll() is not None:
                return True

            try:
                process.terminate()
            except OSError as exc:
                if process.poll() is None:
                    raise JobError(
                        JobErrorKind.WORKER_UNREACHABLE,
                        f"could not terminate CPU child process: {exc}",
                        cause=exc,
                    ) from exc
                return True

            try:
                process.wait(timeout=5)
                return True
            except subprocess.TimeoutExpired:
                if process.poll() is not None:
                    return True

            try:
                process.kill()
            except OSError as exc:
                if process.poll() is None:
                    raise JobError(
                        JobErrorKind.WORKER_UNREACHABLE,
                        f"could not kill CPU child process after its termination grace period: {exc}",
                        cause=exc,
                    ) from exc
                return True

            # Do not let the owning job report quiescence while the child survives.
            try:
                process.wait()
            except OSError as exc:
                if process.poll() is None:
                    raise JobError(
                        JobErrorKind.WORKER_UNREACHABLE,
                        f"could not confirm CPU child process exit after kill: {exc}",
                        cause=exc,
                    ) from exc
            return process.poll() is not None
