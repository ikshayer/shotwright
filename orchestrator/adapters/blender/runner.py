"""Contained Blender invocation and queue-integrated process supervision."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.domain.entities import GenerationRun, Job
from orchestrator.jobs.lease import WorkerControl
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

from .scene import RenderRequest, build_scene_plan


@dataclass(frozen=True)
class RenderResult:
    blend_path: str
    frame_paths: tuple[str, ...]
    metadata_path: str
    log_path: str
    generation_run: GenerationRun


def build_command(blender_exe: Path, script_path: Path, plan_path: Path) -> list[str]:
    return [str(blender_exe), "--background", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", str(script_path), "--", str(plan_path)]


class _Supervisor(WorkerControl):
    """Persistent lease identity with a joined per-attempt child supervisor."""
    def __init__(self, popen: Callable[..., subprocess.Popen[bytes]]) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._attempt: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._popen = popen
        self._stop_reason: JobErrorKind | None = None
        self._epoch: object | None = None
        self._accepting = False
        self._lease_provider: Callable[[], object] | None = None
        self._blocked_lease_tokens: set[str] = set()

    def pid(self) -> int | None:
        return os.getpid()

    def activate(self) -> object:
        with self._lock:
            if self._accepting:
                raise RuntimeError("supervisor already owns an active render epoch")
            self._epoch = object()
            self._accepting = True
            self._stop.clear()
            self._stop_reason = None
            self._lease_provider = None
            self._blocked_lease_tokens.clear()
            return self._epoch

    def bind_lease(self, epoch: object, lease_provider: Callable[[], object]) -> None:
        with self._lock:
            if self._epoch is not epoch or not self._accepting:
                raise JobError(JobErrorKind.CANCELLED, "render invocation is no longer active")
            self._lease_provider = lease_provider

    def deactivate(self, epoch: object) -> bool:
        with self._lock:
            if self._epoch is not epoch:
                return True
            self._accepting = False
        stopped = self._stop_and_join(None)
        with self._lock:
            if self._epoch is epoch:
                self._epoch = None
        return stopped

    def begin(self, epoch: object, target: Callable[[], None], lease: object) -> threading.Thread:
        with self._lock:
            if not self._accepting or self._epoch is not epoch:
                raise JobError(JobErrorKind.CANCELLED, "render invocation is no longer active")
            record = getattr(lease, "_record", None)
            token = record.get("token") if isinstance(record, dict) else None
            if not getattr(lease, "_owned", False) or not isinstance(token, str):
                raise JobError(JobErrorKind.CANCELLED, "GPU lease expired before Blender launch")
            if token in self._blocked_lease_tokens:
                raise JobError(JobErrorKind.CANCELLED, "GPU lease was stopped before Blender launch")
            if self._stop.is_set() and self._stop_reason is not None:
                raise JobError(self._stop_reason, "render attempt was stopped before Blender launch")
            if self._attempt is not None and self._attempt.is_alive():
                raise RuntimeError("adapter already has an active render")
            # Re-arm only within the same still-active render epoch after a completed
            # queue attempt. Deactivation closes the epoch before late callbacks run.
            self._stop.clear()
            self._attempt = threading.Thread(target=target, name="blender-attempt", daemon=True)
            self._attempt.start()
            return self._attempt

    def launch(self, command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        with self._lock:
            if self._stop.is_set():
                raise _CancelledBeforeLaunch("render stopped before Blender launch")
            process = self._popen(command, **kwargs)  # type: ignore[arg-type]
            self._process = process
            return process

    def clear_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def unload(self) -> None:
        reason = JobErrorKind.CANCELLED if self._attempt is None else None
        if not self._stop_and_join(reason):
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "Blender attempt supervisor did not stop; lease retained")

    def terminate(self) -> bool:
        reason = JobErrorKind.BUDGET_EXCEEDED if not self._stop.is_set() else self._stop_reason
        return self._stop_and_join(reason)

    def respawn(self) -> int:
        with self._lock:
            self._stop.clear()
        return os.getpid()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop_for(self, reason: JobErrorKind) -> bool:
        return self._stop_and_join(reason)

    def _stop_and_join(self, reason: JobErrorKind | None) -> bool:
        with self._lock:
            if self._lease_provider is not None:
                lease = self._lease_provider()
                record = getattr(lease, "_record", None)
                token = record.get("token") if isinstance(record, dict) else None
                if isinstance(token, str):
                    self._blocked_lease_tokens.add(token)
            if reason is not None and (not self._stop.is_set() or self._stop_reason is None):
                self._stop_reason = reason
        self._stop.set()
        with self._lock:
            process, attempt = self._process, self._attempt
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if attempt is not None and attempt is not threading.current_thread():
            attempt.join(timeout=8)
            if attempt.is_alive():
                process = self._process
                if process is not None and process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                attempt.join(timeout=5)
        if attempt is not None and attempt.is_alive():
            return False
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    return False
        return True


class _CancelledBeforeLaunch(RuntimeError):
    pass


class BlenderAdapter:
    def __init__(self, root: ProjectRoot, settings: Settings, *, popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen) -> None:
        self.root = root
        self.settings = settings
        self._popen = popen
        self._control = _Supervisor(popen)
        self._render_lock = threading.Lock()

    @property
    def worker(self) -> WorkerControl:
        return self._control

    def render(self, request: RenderRequest, job: Job, *, queue: JobQueue,
               cancel: Callable[[], bool] | None = None) -> RenderResult:
        if not self._render_lock.acquire(blocking=False):
            raise RuntimeError("this BlenderAdapter already has an active render")
        epoch = None
        try:
            epoch = self._control.activate()
            return self._render_locked(request, job, queue=queue, cancel=cancel, epoch=epoch)
        finally:
            if epoch is None or self._control.deactivate(epoch):
                self._render_lock.release()
            else:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "Blender attempt supervisor did not quiesce; lease must remain held")

    def _render_locked(self, request: RenderRequest, job: Job, *, queue: JobQueue,
                       cancel: Callable[[], bool] | None = None, epoch: object) -> RenderResult:
        # Revalidate snapshots to catch model_copy(update=...) and nested mutable edits.
        request = RenderRequest.model_validate(request.model_dump(mode="python"))
        Job.model_validate(job.model_dump(mode="python"))
        if queue.root.path != self.root.path or queue.lease.worker is not self.worker or queue.settings is not self.settings:
            raise ValueError("queue must share adapter ProjectRoot, settings, and worker lease")
        if job.shot_id != request.shot_id or job.run_id != request.run_id:
            raise ValueError("job shot_id/run_id must match the render request")
        if self.settings.resources.throughput_floor_per_second != 0:
            raise ValueError("BlenderAdapter requires throughput_floor_per_second=0 until JobQueue startup grace exists")
        plan_base = build_scene_plan(request)
        base_rel = f"renders/{request.shot_id}/v{request.shot_version:04d}/{request.run_id}"
        base = self.root.resolve(base_rel)
        try:
            base = self.root.write_with(base_rel, lambda path: path.mkdir(exist_ok=False))
        except FileExistsError as exc:
            raise FileExistsError(f"render run already exists: {base_rel}") from exc
        result_box: list[RenderResult] = []
        error_box: list[BaseException] = []
        done = threading.Event()
        start_time = time.monotonic()
        max_elapsed = 10.0 if request.mode == "preview" else self.settings.budget.per_shot_wall_clock_seconds

        def attempt_supervisor() -> None:
            try:
                if self._control.stopped():
                    raise _CancelledBeforeLaunch("render stopped before attempt setup")
                attempt_index = max(1, job.attempts)
                attempt_rel = f"{base_rel}/attempt-{attempt_index:04d}"
                self.root.make_dir(attempt_rel)
                source = Path(__file__).with_name("worker.py").read_bytes()
                script_rel = f"{attempt_rel}/worker.py"
                self.root.write_bytes(script_rel, source)
                script_sha = hashlib.sha256(source).hexdigest()
                count = plan_base["frame_end"]
                ext = "png" if request.mode == "preview" else "exr"
                frame_rels = tuple(f"{attempt_rel}/frames/frame_{i:06d}.{ext}" for i in range(1, int(count) + 1))
                blend_rel = f"{attempt_rel}/scene.blend"
                metadata_rel = f"{attempt_rel}/metadata.json"
                log_rel = f"{attempt_rel}/blender.log"
                job.log_path = log_rel
                worker_identity = hashlib.sha256(source).hexdigest()
                try:
                    executable_identity = _file_sha256(self.settings.binaries.blender_exe)
                except OSError as exc:
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"Blender executable unavailable: {exc}",
                                   job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id) from exc
                cache_identity = hashlib.sha256(
                    f"{executable_identity}|{worker_identity}|{request.gpu_device}".encode()).hexdigest()
                cache_rel = f"runtime/blender/{cache_identity}"
                temp_rel = f"{attempt_rel}/temp"
                config_rel = f"{attempt_rel}/config"
                for directory in (f"{attempt_rel}/frames", cache_rel, temp_rel, config_rel,
                                  f"{config_rel}/scripts", f"{config_rel}/datafiles", f"{config_rel}/extensions"):
                    self.root.make_dir(directory)
                # Output authorization goes through write_with; the callbacks only reserve destinations.
                for rel in (*frame_rels, blend_rel, metadata_rel, log_rel):
                    self.root.write_with(rel, lambda path: None)
                plan = dict(plan_base)
                plan["outputs"] = {
                    "blend": _forward(self.root.resolve(blend_rel)),
                    "frames": [_forward(self.root.resolve(rel)) for rel in frame_rels],
                    "metadata": _forward(self.root.resolve(metadata_rel)),
                }
                plan["runtime"] = {"attempt_dir": _forward(self.root.resolve(attempt_rel)),
                                    "temp_dir": _forward(self.root.resolve(temp_rel)),
                                    "cache_dir": _forward(self.root.resolve(cache_rel))}
                plan_path_rel = f"{attempt_rel}/plan.json"
                plan_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                plan_digest = hashlib.sha256(plan_bytes).hexdigest()
                self.root.write_bytes(plan_path_rel, plan_bytes)
                exe = self.settings.binaries.blender_exe
                argv = build_command(exe, self.root.resolve(script_rel), self.root.resolve(plan_path_rel))
                env = os.environ.copy()
                temp_abs = _forward(self.root.resolve(temp_rel))
                config_abs = _forward(self.root.resolve(config_rel))
                cache_abs = _forward(self.root.resolve(cache_rel))
                env.update({"TEMP": temp_abs, "TMP": temp_abs, "TMPDIR": temp_abs,
                            "XDG_CACHE_HOME": cache_abs,
                            "BLENDER_USER_CONFIG": config_abs, "BLENDER_USER_SCRIPTS": _forward(self.root.resolve(f"{config_rel}/scripts")),
                            "BLENDER_USER_DATAFILES": _forward(self.root.resolve(f"{config_rel}/datafiles")),
                            "BLENDER_USER_EXTENSIONS": _forward(self.root.resolve(f"{config_rel}/extensions")),
                            "CUDA_CACHE_PATH": cache_abs, "CUDA_CACHE_DISABLE": "0", "OPTIX_CACHE_PATH": cache_abs,
                            "PYTHONDONTWRITEBYTECODE": "1"})
                if self._control.stopped():
                    raise _CancelledBeforeLaunch("render stopped before Blender launch")
                started = time.monotonic()
                baseline = queue.lease.baseline_mb
                with open(self.root.resolve(log_rel), "ab", buffering=0) as log_stream, \
                        open(self.root.resolve(log_rel), "rb") as log_reader:
                    try:
                        process = self._control.launch(argv, cwd=str(self.root.path), stdin=subprocess.DEVNULL,
                                                       stdout=log_stream, stderr=subprocess.STDOUT, env=env,
                                                       shell=False, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                    except OSError as exc:
                        raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"could not launch Blender: {exc}",
                                       job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id, cause=exc) from exc
                    child_pid = process.pid
                    log_offset = 0
                    last_progress = 0.0
                    peak = None
                    hard_deadline = started + max_elapsed
                    while process.poll() is None:
                        now = time.monotonic()
                        if cancel is not None and cancel():
                            self._control.stop_for(JobErrorKind.CANCELLED)
                            raise JobError(JobErrorKind.CANCELLED, "render cancelled by caller", job_id=job.job_id,
                                           shot_id=job.shot_id, run_id=job.run_id)
                        if self._control.stopped():
                            kind = self._control._stop_reason or JobErrorKind.CANCELLED
                            raise JobError(kind, "render supervisor stopped", job_id=job.job_id,
                                           shot_id=job.shot_id, run_id=job.run_id)
                        if now >= hard_deadline:
                            raise JobError(JobErrorKind.BUDGET_EXCEEDED, "Blender render deadline exceeded",
                                           job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
                        log_stream.flush()
                        log_reader.seek(log_offset)
                        chunk = log_reader.read(256 * 1024)
                        log_offset += len(chunk)
                        addition = chunk.decode("utf-8", errors="replace")
                        for match in re.finditer(r"CCTV_PROGRESS\s+([0-9.]+)", addition):
                            last_progress = max(last_progress, min(0.99, float(match.group(1))))
                        if not last_progress:
                            produced = sum(self.root.resolve(rel).exists() for rel in frame_rels)
                            last_progress = min(0.95, produced / max(1, len(frame_rels)))
                        job.progress = max(job.progress, last_progress)
                        memory = queue.nvml.device_memory()
                        peak = max(peak or memory.used_mb, memory.used_mb)
                        time.sleep(0.05)
                    code = process.wait()
                    self._control.clear_process(process)
                if code != 0:
                    if self._control._stop_reason is not None:
                        raise _stopped_error(self._control, job)
                    raise _classify_process_failure(_tail_text(self.root.resolve(log_rel)), code, job)
                if self._control.stopped():
                    raise _stopped_error(self._control, job)
                try:
                    metadata = json.loads(self.root.resolve(metadata_rel).read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"worker metadata missing or malformed: {exc}",
                                   job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id) from exc
                _verify_outputs(self.root, metadata, request, frame_rels, blend_rel, metadata_rel, child_pid)
                if self._control.stopped():
                    raise _stopped_error(self._control, job)
                elapsed = time.monotonic() - start_time
                metadata.update({"script_sha256": script_sha, "plan_sha256": plan_digest,
                                 "argv": argv, "elapsed_seconds": elapsed, "child_pid": child_pid,
                                 "vram_baseline_mb": baseline, "vram_peak_mb": peak,
                                 "cache_identity": cache_identity,
                                 "status": "completed"})
                self.root.write_text(metadata_rel, json.dumps(metadata, sort_keys=True, indent=2))
                version = metadata.get("tool_version", "unknown")
                build_hash = metadata.get("build_hash", "unknown")
                job.resource_metrics["vram_baseline_mb"] = baseline
                job.resource_metrics["elapsed_seconds"] = elapsed
                if peak is not None:
                    job.resource_metrics["vram_peak_mb"] = peak
                job.progress = 1.0
                run_settings: dict[str, str | int | float] = {
                    "engine": plan["engine"], "mode": plan["mode"], "plan_sha256": plan_digest,
                    "script_sha256": script_sha, "child_pid": child_pid,
                    "elapsed_seconds": elapsed, "vram_baseline_mb": baseline,
                    "cache_identity": cache_identity,
                }
                if peak is not None:
                    run_settings["vram_peak_mb"] = peak
                run_metrics: dict[str, float] = {"elapsed_seconds": elapsed, "vram_baseline_mb": baseline}
                if peak is not None:
                    run_metrics["vram_peak_mb"] = peak
                generation = GenerationRun(
                    run_id=request.run_id, tool="blender", tool_version=f"{version}+{build_hash}",
                    invocation=json.dumps(argv), seed=plan["seed"],
                    settings=run_settings,
                    input_paths=[script_rel, plan_path_rel], output_paths=[blend_rel, *frame_rels, metadata_rel],
                    log_path=log_rel,
                    status="completed", resource_metrics=run_metrics)
                if self._control.stopped():
                    raise _stopped_error(self._control, job)
                result_box.append(RenderResult(blend_rel, frame_rels, metadata_rel, log_rel, generation))
            except Exception as exc:  # noqa: BLE001 - preserve the cause across the supervisor-thread boundary.
                error_box.append(exc)
                try:
                    record = _failure_record(exc, job)
                    failure_rel = f"{attempt_rel if 'attempt_rel' in locals() else base_rel}/failure.json"
                    self.root.write_text(failure_rel, json.dumps(record, sort_keys=True, indent=2))
                    self.root.write_text(failure_rel + "l", json.dumps(record, sort_keys=True) + "\n")
                except Exception as log_error:  # noqa: BLE001 - a secondary write failure must replace the surfaced job error.
                    log_error.__cause__ = exc
                    classified = JobError.from_exception(log_error, job_id=job.job_id,
                                                         shot_id=job.shot_id, run_id=job.run_id)
                    surfaced = JobError(
                        classified.kind,
                        f"{classified.message}; recording original failure {type(exc).__name__}: {exc}",
                        job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id, cause=log_error,
                    )
                    error_box[-1] = surfaced
            finally:
                done.set()

        def queued_worker(_: Job) -> RenderResult:
            done.clear()
            result_box.clear()
            error_box.clear()
            try:
                thread = self._control.begin(epoch, attempt_supervisor, queue.lease)
            except JobError as error:
                raise JobError.from_exception(error, job_id=job.job_id,
                                              shot_id=job.shot_id, run_id=job.run_id) from error
            while not done.wait(0.05):
                if cancel is not None and cancel():
                    self._control.stop_for(JobErrorKind.CANCELLED)
                if time.monotonic() - start_time >= max_elapsed and not self._control.stopped():
                    self._control.stop_for(JobErrorKind.BUDGET_EXCEEDED)
            thread.join()
            if error_box:
                error = error_box[0]
                if isinstance(error, _CancelledBeforeLaunch):
                    stopped = _stopped_error(self._control, job)
                    stopped.__cause__ = error
                    raise stopped from error
                if isinstance(error, JobError):
                    raise error
                raise JobError.from_exception(error, job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
            if not result_box:
                raise JobError(JobErrorKind.INTERNAL, "Blender supervisor ended without result",
                               job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
            return result_box[0]

        self._control.bind_lease(epoch, lambda: queue.lease)
        return queue.run(job, queued_worker, partial_artifacts=(base,))


def _forward(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def _tail_text(path: Path, limit: int = 16_384) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read().decode("utf-8", errors="replace")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _verify_outputs(root: ProjectRoot, metadata: dict[str, object], request: RenderRequest,
                    frames: tuple[str, ...], blend: str, metadata_rel: str, child_pid: int) -> None:
    if not isinstance(metadata, dict):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker metadata is not a JSON object")
    if metadata.get("schema_version") != 1 or metadata.get("status") != "completed" \
            or metadata.get("shot_id") != request.shot_id \
            or metadata.get("run_id") != request.run_id or metadata.get("shot_version") != request.shot_version \
            or metadata.get("child_pid") != child_pid:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker metadata identity/status mismatch")
    applied = metadata.get("applied_settings")
    required = {"width": 640 if request.mode == "preview" else request.master_resolution[0],
                "height": 480 if request.mode == "preview" else request.master_resolution[1],
                "fps": 60, "samples": 1 if request.mode == "preview" else request.samples}
    if request.mode == "preview":
        engine_expected = {"BLENDER_WORKBENCH"}
    elif request.engine == "eevee":
        engine_expected = {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}
    else:
        engine_expected = {"CYCLES"}
    if not isinstance(applied, dict) or any(applied.get(key) != value for key, value in required.items()) \
            or applied.get("engine") not in engine_expected:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker applied settings do not match the request")
    if (not isinstance(metadata.get("tool_version"), str) or not metadata["tool_version"].strip()
            or not isinstance(metadata.get("build_hash"), str) or not metadata["build_hash"].strip()
            or not isinstance(metadata.get("actual_device"), str) or not metadata["actual_device"].strip()):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker version, build, or device metadata is missing")
    if (not isinstance(metadata.get("child_pid"), int)
            or not isinstance(metadata.get("elapsed_seconds"), (int, float))
            or metadata.get("child_pid") <= 0 or not math.isfinite(metadata["elapsed_seconds"])
            or metadata.get("elapsed_seconds") < 0):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker child PID or elapsed time is invalid")
    if not _device_matches(str(metadata.get("actual_device")), request.gpu_device):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker device metadata does not match the requested GPU")
    fingerprint = metadata.get("geometry_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker geometry fingerprint is invalid")
    try:
        blend_path = root.resolve(blend)
        with blend_path.open("rb") as stream:
            blend_magic = stream.read(7)
        blend_size = blend_path.stat().st_size
    except OSError as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"saved .blend is missing: {exc}") from exc
    if blend_size <= 7 or not blend_magic.startswith(b"BLENDER"):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "saved .blend magic is missing")
    for frame in frames:
        try:
            frame_path = root.resolve(frame)
            with frame_path.open("rb") as stream:
                data = stream.read(8)
            frame_size = frame_path.stat().st_size
        except OSError as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"frame output is missing: {frame}") from exc
        magic = b"\x89PNG\r\n\x1a\n" if request.mode == "preview" else b"v/1\x01"
        if frame_size <= 8 or len(data) < 8 or not data.startswith(magic):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"frame output is empty or malformed: {frame}")
    try:
        if not root.resolve(metadata_rel).stat().st_size:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "worker metadata is empty")
    except OSError as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"worker metadata missing: {exc}") from exc


def _device_matches(actual: str, requested: str) -> bool:
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower().replace("nvidia", "").replace("geforce", ""))
    return normalize(requested) in normalize(actual) or normalize(actual) in normalize(requested)


def _classify_process_failure(log: str, code: int, job: Job) -> JobError:
    lower = log.lower()
    if "out of memory" in lower or "cuda_error_out_of_memory" in lower or "failed to allocate" in lower:
        kind = JobErrorKind.OUT_OF_MEMORY
    elif "no space left" in lower or "disk full" in lower or "enospc" in lower:
        kind = JobErrorKind.DISK_EXHAUSTED
    elif "unsupported render engine" in lower or "device" in lower and "unavailable" in lower \
            or "renderer mismatch" in lower or "invalid scene" in lower or "camera position override" in lower \
            or "unsupported radial distortion" in lower or "clamped" in lower \
            or "not the requested nvidia gpu" in lower:
        kind = JobErrorKind.INVALID_MANIFEST
    else:
        kind = JobErrorKind.INTERNAL
    return JobError(kind, f"Blender exited with code {code}: {log[-4000:]}",
                    job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)


def _failure_record(error: BaseException, job: Job) -> dict[str, object]:
    if isinstance(error, JobError):
        correlated = JobError.from_exception(error, job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
        return {**correlated.to_log_record(),
                "diagnostic": repr(correlated.__cause__) if correlated.__cause__ else correlated.message}
    return {"job_id": job.job_id, "shot_id": job.shot_id, "run_id": job.run_id,
            "error": type(error).__name__, "message": str(error)}


def _stopped_error(control: _Supervisor, job: Job) -> JobError:
    kind = control._stop_reason or JobErrorKind.CANCELLED
    message = "Blender render deadline exceeded" if kind is JobErrorKind.BUDGET_EXCEEDED else "Blender render cancelled"
    return JobError(kind, message, job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
