"""Managed backend protocol and persistent worker control for MiniMax H3."""
from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from typing import Protocol

from orchestrator.domain.entities import Job
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

from .core import H3Request, H3Result, H3SetupError


class Backend(Protocol):
    """Execution boundary. Implementations must retain cleanup evidence on failure."""
    def inspect(self) -> dict: ...
    def execute(self, request: H3Request, job: Job, *, root: ProjectRoot, queue: JobQueue,
                cancel: Callable[[], bool] | None, progress: Callable[[float], None]) -> H3Result: ...
    def stop(self) -> bool: ...
    def close(self) -> None: ...


class H3WorkerControl:
    """Stable host identity with lease-token and epoch scoped stop gates.

    Like the Blender supervisor, PID is the API process which owns the coordinator.
    A stop marks the current persisted lease token terminal before signaling backend
    children, so no stale attempt may start work after a release/timeout callback.
    """

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self._lock = threading.RLock()
        self._closed = False
        self._epoch: object | None = None
        self._token: str | None = None
        self._blocked_tokens: set[str] = set()
        self._lease_provider: Callable[[], object] | None = None
        self._attempt: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._unsafe = False

    def pid(self) -> int | None:
        return os.getpid()

    def activate(self, lease: object) -> object:
        record = getattr(lease, "_record", None)
        token = record.get("token") if isinstance(record, dict) else None
        with self._lock:
            if self._closed or not getattr(lease, "_owned", False) or not isinstance(token, str) or not token:
                raise JobError(JobErrorKind.CANCELLED, "H3 coordinator requires an owned persisted GPU lease")
            if self._attempt is not None and self._attempt.is_alive():
                raise RuntimeError("H3 coordinator already has an active attempt")
            if token in self._blocked_tokens:
                raise JobError(JobErrorKind.CANCELLED, "H3 lease token was already stopped")
            self._token = token
            self._lease_provider = lambda: lease
            self._epoch = object()
            self._stop_event.clear()
            self._unsafe = False
            return self._epoch

    def allowed(self, epoch: object, token: str) -> bool:
        with self._lock:
            return (not self._closed and self._epoch is epoch and self._token == token
                    and token not in self._blocked_tokens and not self._stop_event.is_set())

    def begin(self, epoch: object, token: str, target: Callable[[], None]) -> threading.Thread:
        with self._lock:
            if not self.allowed(epoch, token):
                raise JobError(JobErrorKind.CANCELLED, "H3 attempt stopped before native launch")
            if self._attempt is not None and self._attempt.is_alive():
                raise RuntimeError("H3 adapter already has an active attempt")
            self._attempt = threading.Thread(target=target, name="minimax-h3-attempt", daemon=True)
            self._attempt.start()
            return self._attempt

    def finish(self, epoch: object) -> None:
        with self._lock:
            if self._epoch is epoch:
                self._epoch = None
                self._token = None
                self._lease_provider = None
                self._attempt = None

    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def request_queue_stop(self) -> None:
        """Fence future H3 work; lease cleanup performs and verifies backend stop."""
        with self._lock:
            if isinstance(self._token, str):
                self._blocked_tokens.add(self._token)
            lease = self._lease_provider() if self._lease_provider else None
            record = getattr(lease, "_record", None)
            if isinstance(record, dict) and isinstance(record.get("token"), str):
                self._blocked_tokens.add(record["token"])
            self._stop_event.set()

    def unload(self) -> None:
        if not self.terminate():
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "H3 children or attempt did not stop; GPU lease retained")

    def terminate(self) -> bool:
        with self._lock:
            if self._closed and self._attempt is None:
                return not self._unsafe
            if isinstance(self._token, str):
                self._blocked_tokens.add(self._token)
            lease = self._lease_provider() if self._lease_provider else None
            record = getattr(lease, "_record", None)
            if isinstance(record, dict) and isinstance(record.get("token"), str):
                self._blocked_tokens.add(record["token"])
            self._stop_event.set()
            attempt = self._attempt
        safe = False
        try:
            safe = self._backend.stop()
        except BaseException:  # noqa: BLE001
            safe = False
        if attempt is not None and attempt is not threading.current_thread():
            attempt.join(timeout=25)
        if attempt is not None and attempt.is_alive():
            safe = False
        with self._lock:
            self._unsafe = not safe
        return safe

    def respawn(self) -> int:
        """Re-arm future work under a fresh epoch; persistent coordinator is this process."""
        with self._lock:
            if self._closed:
                raise RuntimeError("H3 worker is closed")
            if self._attempt is not None and self._attempt.is_alive():
                raise RuntimeError("cannot re-arm H3 while prior attempt remains active")
            if self._unsafe:
                raise RuntimeError("cannot re-arm H3 after unresolved process cleanup")
            rearm = getattr(self._backend, "respawn", None)
            if callable(rearm):
                rearm()
            self._stop_event.clear()
            self._unsafe = False
            self._epoch = None
            self._token = None
            self._lease_provider = None
            self._blocked_tokens.clear()
        return os.getpid()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        if not self.terminate():
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "H3 worker cleanup is unresolved")
        self._backend.close()


class MinimaxH3Adapter:
    def __init__(self, root: ProjectRoot, settings: Settings, *, backend: Backend | None = None) -> None:
        self.root, self.settings = root, settings
        if backend is None:
            from .native import NativeBackend

            backend = NativeBackend(root, settings)
        self._backend = backend
        self._worker = H3WorkerControl(self._backend)
        self._closed = False

    @property
    def worker(self) -> H3WorkerControl:
        return self._worker

    def inspect(self) -> dict:
        """Model-free runtime presence/configuration inspection."""
        return self._backend.inspect()

    def generate_managed(self, request: H3Request, job: Job, *, queue: JobQueue,
                         cancel: Callable[[], bool] | None = None) -> H3Result:
        request = H3Request.model_validate(request.model_dump(mode="python"))
        Job.model_validate(job.model_dump(mode="python"))
        if self._closed:
            raise H3SetupError("MiniMax H3 adapter is closed")
        if queue.root is not self.root or queue.settings is not self.settings or queue.lease.worker is not self.worker:
            raise ValueError("managed H3 queue must share this exact root, settings and worker")
        record = getattr(queue.lease, "_record", None)
        if not queue.lease._owned or not isinstance(record, dict) or not isinstance(record.get("token"), str):
            raise ValueError("generate_managed requires an owned GPU lease and persisted token")
        if job.shot_id != request.shot_id or job.run_id != request.run_id:
            raise ValueError("job shot_id/run_id must match H3 request")
        cfg = getattr(self.settings, "minimax_h3", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            raise H3SetupError("MiniMax H3 is disabled; enable it and configure explicit positive reservations")
        epoch = self.worker.activate(queue.lease)
        result: list[H3Result] = []
        errors: list[BaseException] = []
        token = str(record["token"])
        def stopped() -> bool:
            return self.worker.stopped() or bool(cancel and cancel()) or not self.worker.allowed(epoch, token)
        def report(value: float) -> None:
            if not math.isfinite(value) or value < 0 or value > 1:
                raise ValueError("backend progress must be finite in [0,1]")
            job.progress = value
        def invoke() -> None:
            try:
                if stopped():
                    raise JobError(JobErrorKind.CANCELLED, "H3 cancelled before backend launch", job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
                result.append(self._backend.execute(request, job, root=self.root, queue=queue, cancel=stopped, progress=report))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        attempt = self.worker.begin(epoch, token, invoke)
        deadline = time.monotonic() + float(getattr(cfg, "wall_clock_seconds", 2400))
        try:
            while attempt.is_alive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.worker.terminate()
                    if attempt.is_alive():
                        raise JobError(JobErrorKind.INTERNAL, "H3 attempt cleanup unresolved; lease retained")
                    raise JobError(JobErrorKind.BUDGET_EXCEEDED, "H3 attempt exceeded its managed deadline")
                attempt.join(timeout=min(0.2, remaining))
            if errors:
                error = errors[0]
                if isinstance(error, BaseException):
                    raise error
            if not result:
                raise JobError(JobErrorKind.INTERNAL, "H3 backend returned no result")
            if stopped():
                raise JobError(JobErrorKind.CANCELLED, "H3 attempt cancelled before result acceptance")
            job.progress = 1.0
            return result[0]
        except BaseException:
            if attempt.is_alive():
                self.worker.terminate()
            if attempt.is_alive():
                # Keep the callback and epoch attached: lease release remains unsafe.
                raise JobError(JobErrorKind.INTERNAL, "H3 callback remains active after stop; lease retained")
            raise
        finally:
            if not attempt.is_alive():
                self.worker.finish(epoch)

    def generate(self, request: H3Request, job: Job, *, queue: JobQueue,
                 cancel: Callable[[], bool] | None = None) -> H3Result:
        if queue.root is not self.root or queue.settings is not self.settings or queue.lease.worker is not self.worker:
            raise ValueError("standalone H3 queue must share this exact root, settings and worker")
        return queue.run(job, lambda managed_job: self.generate_managed(request, managed_job, queue=queue, cancel=cancel))

    def close(self) -> None:
        self._closed = True
        self._worker.close()



