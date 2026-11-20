"""Loopback ComfyUI transport, launch command, and resident worker supervisor."""
from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from orchestrator.jobs.lease import ComfyWorkerControl
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

COMFYUI_VERSION = "0.44.0"


class HttpTransport:
    def __init__(self, port: int) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("invalid ComfyUI port")
        self.port = port

    def request(self, method: str, path: str, payload: dict | None = None, *, timeout: float = 5.0) -> dict:
        if (not path.startswith("/") or path.startswith("//") or ":" in path or "\\" in path
                or method not in {"GET", "POST"} or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("only bounded relative API paths are allowed")
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                kind = JobErrorKind.PROVIDER_RATE_LIMIT
            elif 400 <= exc.code < 500:
                kind = JobErrorKind.PROVIDER_HTTP_4XX
            else:
                kind = JobErrorKind.WORKER_UNREACHABLE
            raise JobError(kind, f"ComfyUI HTTP {exc.code}", cause=exc) from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"ComfyUI request failed: {exc}", cause=exc) from exc
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI returned invalid JSON", cause=exc) from exc
        if not isinstance(body, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI response is not a JSON object")
        return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def build_command(settings: Settings, *, runtime_dir: Path, model_config: Path,
                  gpu_device: int, memory_mode: str) -> list[str]:
    root = settings.binaries.comfyui_root
    command = [str(root / "python_embeded" / "python.exe"), "-s", "-B",
               str(root / "ComfyUI" / "main.py"), "--listen", "127.0.0.1",
               "--port", str(settings.binaries.comfyui_port), "--disable-auto-launch",
               "--preview-method", "none", "--disable-all-custom-nodes",
               "--disable-api-nodes", "--cuda-device", str(gpu_device), "--base-directory", str(runtime_dir),
               "--input-directory", str(runtime_dir / "input"), "--output-directory", str(runtime_dir / "output"),
               "--temp-directory", str(runtime_dir / "temp"), "--user-directory", str(runtime_dir / "user"),
               "--front-end-root", str(runtime_dir / "frontend"), "--extra-model-paths-config", str(model_config),
               "--database-url", "sqlite:///:memory:", "--cache-none"]
    if memory_mode == "lowvram": command.append("--lowvram")
    elif memory_mode == "novram": command.append("--novram")
    elif memory_mode != "normal": raise ValueError("unsupported memory mode")
    if gpu_device < 0: raise ValueError("gpu_device must be nonnegative")
    return command


class OwnedComfyWorker(ComfyWorkerControl):
    """Lease supervisor PID is this process; an owned child is started lazily."""
    def __init__(self, command_factory: Callable[[], list[str]], transport: HttpTransport, *,
                 popen: Callable[..., subprocess.Popen[bytes]], cwd: Path, runtime_dir: Path, port: int,
                 server_log: Path,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(["python"], port=port, cwd=cwd)
        self.command_factory, self.transport, self._popen, self.clock = command_factory, transport, popen, clock
        self.runtime_dir, self.port, self.server_log = runtime_dir, port, server_log
        self._child: subprocess.Popen[bytes] | None = None
        self._identity: object | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._attempt_thread: threading.Thread | None = None
        self._attempt_stop: threading.Event | None = None
        self._callback_thread: threading.Thread | None = None
        self._callback_epoch: object | None = None
        self._callback_token: str | None = None
        self._epoch: object | None = None
        self._accepting = False
        self._lease_provider: Callable[[], object] | None = None
        self._blocked_tokens: set[str] = set()
        self._interrupt_active: Callable[[], None] | None = None
        self._log_stream = None
        self._unsafe_stop = False
        self._endpoint_verified = False
        self._launch_argv: tuple[str, ...] = ()
        self._gpu_index = 0

    def pid(self) -> int | None:
        return os.getpid()

    @property
    def child_pid(self) -> int | None:
        with self._lock:
            return self._child.pid if self._child is not None and self._child.poll() is None else None

    @property
    def endpoint_verified(self) -> bool:
        return self._endpoint_verified and self.child_pid is not None

    def verify_endpoint(self) -> dict:
        child = self._child
        if child is None or child.poll() is not None:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "owned ComfyUI child is not alive")
        stats = self.transport.request("GET", "/system_stats", timeout=2.0)
        if not isinstance(stats, dict) or not isinstance(stats.get("system"), dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI ownership response is malformed")
        system = stats["system"]
        version = str(system.get("comfyui_version", ""))
        if version != COMFYUI_VERSION or not _endpoint_matches(system.get("argv"), self.runtime_dir, self.port, self._gpu_index):
            if not self.terminate(interrupt=False):
                self._unsafe_stop = True
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "ComfyUI endpoint changed ownership and owned child termination is unconfirmed")
            self._endpoint_verified = False
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI endpoint no longer matches owned launch")
        self._endpoint_verified = True
        return stats

    def ensure_ready(self, identity: object, deadline: float, guard: Callable[[], None] | None = None) -> dict:
        reusable: subprocess.Popen[bytes] | None = None
        if guard is not None: guard()
        with self._lock:
            if self._closed: raise JobError(JobErrorKind.CANCELLED, "worker is closed")
            if self._child is not None and self._child.poll() is None and self._identity == identity:
                reusable = self._child
            if reusable is not None:
                child = reusable
            else:
                self._endpoint_verified = False
                if self._child is not None and not self._stop_child():
                    self._unsafe_stop = True
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "old ComfyUI child could not be stopped")
                if _port_open(self.port):
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI port is occupied by an unowned process")
                if guard is not None: guard()
                try:
                    command = self.command_factory()
                    self._launch_argv = tuple(command)
                    self._gpu_index = _flag_value(command, "--cuda-device", "")
                    self._log_stream = self.server_log.open("ab", buffering=0)
                    self._child = self._popen(command, cwd=str(self._cwd), stdin=subprocess.DEVNULL,
                                              stdout=self._log_stream, stderr=subprocess.STDOUT, shell=False,
                                              env=_offline_env(self.runtime_dir))
                except OSError as exc:
                    if self._log_stream is not None:
                        self._log_stream.close(); self._log_stream = None
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"ComfyUI launch failed: {exc}", cause=exc) from exc
                self._identity = identity
                child = self._child
        if guard is not None:
            try: guard()
            except Exception as exc:
                if not self.terminate(interrupt=False):
                    self._unsafe_stop = True
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                                   "ComfyUI child survived a cancelled readiness check", cause=exc) from exc
                raise
        try:
            result = self._wait_ready(deadline, child, guard)
            self._endpoint_verified = True
            return result
        except Exception:
            self._endpoint_verified = False
            if not self._stop_child():
                self._unsafe_stop = True
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "ComfyUI endpoint failed ownership verification and owned child would not stop")
            raise

    def activate(self, epoch: object, lease_provider: Callable[[], object], interrupt: Callable[[], None]) -> None:
        with self._lock:
            if self._accepting:
                raise RuntimeError("ComfyUI supervisor already has an active epoch")
            self._epoch, self._accepting, self._lease_provider = epoch, True, lease_provider
            self._interrupt_active = interrupt

    def begin_attempt(self, epoch: object, token: str, thread: threading.Thread, stop: threading.Event) -> None:
        with self._lock:
            lease = self._lease_provider() if self._lease_provider else None
            record = getattr(lease, "_record", None)
            if (not self._accepting or epoch is not self._epoch or token in self._blocked_tokens
                    or not getattr(lease, "_owned", False) or not isinstance(record, dict)
                    or record.get("token") != token or stop.is_set()):
                raise JobError(JobErrorKind.CANCELLED, "ComfyUI attempt fenced by epoch or lease")
            if self._attempt_thread is not None and self._attempt_thread.is_alive():
                raise RuntimeError("ComfyUI attempt thread is already active")
            self._attempt_thread, self._attempt_stop = thread, stop
            # Starting while holding the admission lock prevents unload/timeout from
            # observing a registered-but-not-started thread and returning before it runs.
            try:
                thread.start()
            except RuntimeError:
                self._attempt_thread, self._attempt_stop = None, None
                raise

    def begin_callback(self, epoch: object, token: str, thread: threading.Thread) -> None:
        with self._lock:
            lease = self._lease_provider() if self._lease_provider else None
            record = getattr(lease, "_record", None)
            if (not self._accepting or epoch is not self._epoch or token in self._blocked_tokens
                    or not getattr(lease, "_owned", False) or not isinstance(record, dict)
                    or record.get("token") != token):
                raise JobError(JobErrorKind.CANCELLED, "ComfyUI queue callback fenced by epoch or lease")
            if self._callback_thread is not None and self._callback_thread.is_alive() and self._callback_thread is not thread:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, "previous queue callback has not quiesced")
            self._callback_thread, self._callback_epoch, self._callback_token = thread, epoch, token

    def deactivate(self, epoch: object) -> bool:
        with self._lock:
            if self._epoch is not epoch:
                return True
            self._accepting = False
            if self._attempt_stop is not None: self._attempt_stop.set()
            attempt, callback = self._attempt_thread, self._callback_thread
            self._epoch, self._lease_provider = None, None
        current = threading.current_thread()
        for thread in (attempt, callback):
            if thread is not None and thread is not current:
                thread.join(timeout=8)
        return all(thread is None or thread is current or not thread.is_alive() for thread in (attempt, callback))

    def clear_attempt(self, thread: threading.Thread) -> None:
        with self._lock:
            if self._attempt_thread is thread:
                self._attempt_thread, self._attempt_stop = None, None

    def request_stop(self, epoch: object, token: str) -> None:
        with self._lock:
            if epoch is not self._epoch:
                return
            self._blocked_tokens.add(token)
            if self._attempt_stop is not None:
                self._attempt_stop.set()

    def request_queue_stop(self) -> None:
        """Fence the active queue token without blocking on attempt teardown."""
        with self._lock:
            epoch = self._epoch
            lease = self._lease_provider() if self._lease_provider else None
            record = getattr(lease, "_record", None)
            token = record.get("token") if isinstance(record, dict) else None
        if epoch is not None and isinstance(token, str):
            self.request_stop(epoch, token)

    def publish(self, epoch: object, token: str, deadline: float, writer: Callable[[], None],
                *, deadline_clock: Callable[[], float] = time.monotonic,
                deadline_value: float | None = None) -> None:
        """Linearize success artifacts against unload, timeout, and token release."""
        with self._lock:
            lease = self._lease_provider() if self._lease_provider else None
            record = getattr(lease, "_record", None)
            persisted = record.get("token") if isinstance(record, dict) else None
            if (not self._accepting or epoch is not self._epoch or token in self._blocked_tokens
                    or not getattr(lease, "_owned", False) or persisted != token
                    or self._attempt_stop is None or self._attempt_stop.is_set()):
                raise JobError(JobErrorKind.CANCELLED, "success publication fenced by epoch or lease")
            if self.clock() >= deadline or (deadline_value is not None and deadline_clock() >= deadline_value):
                raise JobError(JobErrorKind.BUDGET_EXCEEDED, "ComfyUI generation deadline exceeded before publication")
            writer()

    def _wait_ready(self, deadline: float, child: subprocess.Popen[bytes], guard: Callable[[], None] | None) -> dict:
        boot_deadline = min(deadline, self.clock() + 150.0)
        while self.clock() < boot_deadline:
            if guard is not None:
                guard()
            if child.poll() is not None:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"ComfyUI exited during startup ({child.returncode})")
            try:
                stats = self.transport.request("GET", "/system_stats", timeout=min(2.0, max(.1, boot_deadline-self.clock())))
            except JobError as exc:
                if exc.kind is not JobErrorKind.WORKER_UNREACHABLE: raise
                time.sleep(min(.25, max(0, boot_deadline-self.clock())))
                continue
            system = stats.get("system")
            if not isinstance(system, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI system_stats lacks system metadata")
            version = str(system.get("comfyui_version", stats.get("comfyui_version", "")))
            if version != COMFYUI_VERSION:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"unexpected ComfyUI version {version!r}")
            server_argv = system.get("argv")
            if not _endpoint_matches(server_argv, self.runtime_dir, self.port, self._gpu_index):
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "ComfyUI endpoint argv does not match this owned launch")
            if child.poll() is not None:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI exited after readiness")
            return stats
        raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI startup readiness deadline exceeded")

    def unload(self) -> None:
        if self._unsafe_stop:
            if not self.terminate(interrupt=False):
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "ComfyUI child shutdown is unconfirmed; GPU lease retained")
            self._unsafe_stop = False
        with self._lock:
            lease = self._lease_provider() if self._lease_provider else None
            record = getattr(lease, "_record", None)
            token = record.get("token") if isinstance(record, dict) else None
            if isinstance(token, str): self._blocked_tokens.add(token)
            thread, callback = self._attempt_thread, self._callback_thread
            stop = self._attempt_stop
            if thread is not None and thread.is_alive() and stop is not None:
                stop.set()
                interrupt = self._interrupt_active
            else:
                interrupt = None
        interrupt_error = None
        if interrupt is not None:
            try: interrupt()
            except Exception as exc:  # noqa: BLE001 - owned child death is the bounded fallback.
                interrupt_error = exc
        if interrupt_error is not None and not self.terminate(interrupt=False):
            self._unsafe_stop = True
            raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                           "ComfyUI interrupt failed and child termination was not confirmed",
                           cause=interrupt_error) from interrupt_error
        if interrupt_error is not None:
            return
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=8)
            if thread.is_alive():
                if not self.terminate(interrupt=False):
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI attempt did not quiesce; lease retained")
                thread.join(timeout=5)
                if thread.is_alive():
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI attempt did not quiesce after child termination; lease retained")
        if callback is not None and callback is not threading.current_thread():
            callback.join(timeout=8)
            if callback.is_alive():
                if not self.terminate(interrupt=False):
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI queue callback did not quiesce; lease retained")
                callback.join(timeout=5)
                if callback.is_alive():
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI queue callback remained active; lease retained")
        if self.child_pid is None:
            return
        if not self._endpoint_verified:
            if not self.terminate(interrupt=False):
                self._unsafe_stop = True
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "unverified ComfyUI child shutdown unconfirmed; lease retained")
            return
        self.verify_endpoint()
        self.transport.request("POST", "/free", {"unload_models": True, "free_memory": True}, timeout=5)

    def terminate(self, *, interrupt: bool = True) -> bool:
        with self._lock:
            stop, thread, callback = self._attempt_stop, self._attempt_thread, self._callback_thread
            lease = self._lease_provider() if self._lease_provider else None
            record = getattr(lease, "_record", None)
            token = record.get("token") if isinstance(record, dict) else None
            if isinstance(token, str): self._blocked_tokens.add(token)
            if stop is not None: stop.set()
            callback_thread = callback
            callback_action = self._interrupt_active if interrupt and thread is not None and thread.is_alive() else None
        interrupt_error = None
        if callback_action is not None:
            try: callback_action()
            except Exception as exc:  # noqa: BLE001 - kill the owned child below if remote quiescence failed.
                interrupt_error = exc
        stopped = self._stop_child()
        if not stopped:
            self._unsafe_stop = True
            if interrupt_error is not None:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "interrupt failed and owned child termination was unconfirmed",
                               cause=interrupt_error) from interrupt_error
            return False
        self._unsafe_stop = False
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive(): return False
        current = threading.current_thread()
        # The attempt thread may initiate terminate from its own /prompt failure
        # path while the queue callback is waiting for that attempt to finish.
        # The callback owner joins both after this attempt unwinds.
        if callback_thread is not None and callback_thread is not current and callback_thread is not thread and current is not thread:
            callback_thread.join(timeout=5)
            if callback_thread.is_alive(): return False
        return True

    def mark_unsafe_stop(self) -> None:
        self._unsafe_stop = True

    def _stop_child(self) -> bool:
        with self._lock:
            child = self._child
        if child is not None and child.poll() is None:
            try:
                child.terminate(); child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try: child.kill(); child.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired): return False
        stopped = child is None or child.poll() is not None
        if stopped and self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None
        if stopped:
            self._endpoint_verified = False
        return stopped

    def respawn(self) -> int:
        if not self.terminate(): raise JobError(JobErrorKind.WORKER_UNREACHABLE, "old ComfyUI worker remains alive")
        self._identity = None
        return os.getpid()

    def close(self) -> None:
        self._closed = True
        if not self.terminate(): raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI child shutdown unconfirmed")


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _flag_value(argv: list[str], flag: str, default: str) -> str:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def _endpoint_matches(argv: object, runtime_dir: Path, port: int, gpu_device: str) -> bool:
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        return False
    base = _flag_value(argv, "--base-directory", "")
    actual_port = _flag_value(argv, "--port", "")
    actual_gpu = _flag_value(argv, "--cuda-device", "")
    return (os.path.normcase(os.path.normpath(base)) == os.path.normcase(os.path.normpath(str(runtime_dir)))
            and actual_port == str(port) and actual_gpu == str(gpu_device))


def _offline_env(runtime_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    cache = runtime_dir / "cache"
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "HF_HOME": str(cache), "HF_HUB_CACHE": str(cache / "hub"), "TEMP": str(runtime_dir / "temp"),
                "TMP": str(runtime_dir / "temp"), "TMPDIR": str(runtime_dir / "temp"),
                "XDG_CACHE_HOME": str(cache), "TORCH_HOME": str(cache / "torch"),
                "TRITON_CACHE_DIR": str(cache / "triton"), "CUDA_CACHE_PATH": str(cache / "cuda")})
    return env
