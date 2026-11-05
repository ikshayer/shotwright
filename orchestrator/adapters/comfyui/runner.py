"""Queue-integrated FLUX.2 still generation with retained provenance."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from orchestrator.domain.entities import GenerationRun, Job
from orchestrator.jobs.lease import WorkerControl
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

from .runtime import HttpTransport, OwnedComfyWorker, build_command
from .workflow import ImageRequest, build_workflow

COMFYUI_STARTUP_ALLOWANCE_SECONDS = 33.5  # PRD §8.4 / spike 03: clean boot, custom nodes disabled.


@dataclass(frozen=True)
class ImageResult:
    image_paths: tuple[str, ...]
    workflow_path: str
    metadata_path: str
    log_path: str
    generation_run: GenerationRun


class ComfyAdapter:
    def __init__(self, root: ProjectRoot, settings: Settings, *, popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen, transport=None) -> None:
        self.root, self.settings = root, settings
        self.transport = transport if transport is not None else HttpTransport(settings.binaries.comfyui_port)
        self._lock = threading.Lock()
        self._epoch = 0
        self._deadline = 0.0
        self._queue_deadline = 0.0
        self._queue_clock: Callable[[], float] = time.monotonic
        self._cancel: Callable[[], bool] | None = None
        self._active_prompt: str | None = None
        self._submitting = False
        self._blocked_tokens: set[str] = set()
        self._attempt_stop: threading.Event | None = None
        self._output_dirs: set[Path] = set()
        self._runtime_rel = f"runtime/comfyui-{uuid.uuid4().hex}"
        # Supervisor creates no files and launches only after a persisted queue lease exists.
        self._control = OwnedComfyWorker(lambda: self._command(), self.transport,
                                          popen=popen, cwd=settings.binaries.comfyui_root / "ComfyUI",
                                          runtime_dir=root.resolve(self._runtime_rel), port=settings.binaries.comfyui_port,
                                          server_log=root.resolve(f"{self._runtime_rel}/server.log"))

    @property
    def worker(self) -> WorkerControl:
        return self._control

    def _command(self) -> list[str]:
        return build_command(self.settings, runtime_dir=self.root.resolve(self._runtime_rel),
                             model_config=self.root.resolve(f"{self._runtime_rel}/models.yaml"),
                             gpu_device=self._gpu, memory_mode=self._memory)

    def generate(self, request: ImageRequest, job: Job, *, queue: JobQueue,
                 cancel: Callable[[], bool] | None = None) -> ImageResult:
        if not self._lock.acquire(blocking=False): raise RuntimeError("ComfyAdapter already has an active generation")
        self._epoch += 1; epoch = self._epoch
        try:
            request = ImageRequest.model_validate(request.model_dump(mode="python"))
            Job.model_validate(job.model_dump(mode="python"))
            if queue.root.path != self.root.path or queue.lease.worker is not self.worker or queue.settings is not self.settings:
                raise ValueError("queue must share adapter ProjectRoot, settings, and worker lease")
            if (job.shot_id, job.run_id) != (request.shot_id, request.run_id): raise ValueError("job IDs must match request")
            self._control.activate(epoch, lambda: queue.lease, self._interrupt)
            self._deadline = time.monotonic() + self.settings.budget.per_shot_wall_clock_seconds
            self._queue_clock = queue.clock
            self._queue_deadline = queue.clock() + self.settings.budget.per_shot_wall_clock_seconds
            run_rel = f"generated/{request.shot_id}/v{request.shot_version:04d}/{request.run_id}"
            try: self.root.write_with(run_rel, lambda p: p.mkdir(parents=True, exist_ok=False))
            except FileExistsError as exc: raise FileExistsError(f"generation run already exists: {run_rel}") from exc
            self._cancel = cancel
            partial_paths: list[Path] = [self.root.resolve(run_rel)]
            def invoke(_: Job) -> ImageResult:
                record = queue.lease._record
                token = record.get("token") if isinstance(record, dict) else None
                if not isinstance(token, str): raise JobError(JobErrorKind.CANCELLED, "no persisted lease token")
                self._control.begin_callback(epoch, token, threading.current_thread())
                box: list[ImageResult] = []
                errors: list[BaseException] = []
                done = threading.Event()
                stop = threading.Event()
                self._attempt_stop = stop
                telemetry = [queue.lease.baseline_mb, queue.lease.baseline_mb]
                def work() -> None:
                    try:
                        box.append(self._run_attempt(request, job, queue, run_rel, epoch, token, stop,
                                                     partial_paths, telemetry))
                    except Exception as exc:  # noqa: BLE001 - child errors must return to queue thread.
                        errors.append(exc)
                        error = exc if isinstance(exc, JobError) else JobError(
                            JobErrorKind.DISK_EXHAUSTED if getattr(exc, "errno", None) == 28 else JobErrorKind.INTERNAL,
                            f"ComfyUI attempt failed before completion: {exc}", cause=exc)
                        attempt_rel = f"{run_rel}/attempt-{max(1, job.attempts):04d}"
                        record = error.to_log_record()
                        record.update({"job_id": job.job_id, "shot_id": job.shot_id, "run_id": job.run_id})
                        try:
                            self.root.write_text(f"{attempt_rel}/job.jsonl", json.dumps(record, sort_keys=True)+"\n")
                            self.root.write_text(f"{attempt_rel}/metadata.json", json.dumps({"status":"failed", "error":record}, sort_keys=True, indent=2))
                        except OSError as log_exc:
                            errors[-1] = JobError(JobErrorKind.DISK_EXHAUSTED,
                                f"original {error.kind.value} failure: {error.message}; failure log write failed: {log_exc}", cause=log_exc)
                    finally: done.set()
                thread = threading.Thread(target=work, name="comfyui-attempt", daemon=True)
                self._control.begin_attempt(epoch, token, thread, stop)
                peak = telemetry[1]
                telemetry_error = None
                loop_error = None
                try:
                    while not done.wait(.02):
                        try:
                            peak = max(peak, queue.nvml.device_memory().used_mb)
                            telemetry[1] = peak
                        except Exception as exc:  # noqa: BLE001 - telemetry failure must stop and join generation.
                            telemetry_error = exc
                            self._control.request_stop(epoch, token)
                            self._interrupt()
                            break
                        if cancel is not None and cancel():
                            self._control.request_stop(epoch, token); self._interrupt()
                        if time.monotonic() >= self._deadline:
                            self._control.request_stop(epoch, token); self._interrupt()
                except Exception as exc:  # noqa: BLE001 - supervisor callbacks cannot escape without cleanup.
                    loop_error = exc
                    stop.set()
                    self._control.terminate(interrupt=False)
                finally:
                    if thread.is_alive() and not self._control.terminate(interrupt=False):
                        raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                                       "attempt thread could not be joined after stop")
                thread.join()
                self._control.clear_attempt(thread)
                self._blocked_tokens.add(token)
                if loop_error is not None:
                    if isinstance(loop_error, JobError): raise loop_error
                    raise JobError(JobErrorKind.INTERNAL, f"ComfyUI supervisor monitor failed: {loop_error}", cause=loop_error)
                if telemetry_error is not None:
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                                   f"NVML telemetry failed: {telemetry_error}", cause=telemetry_error)
                if errors: raise errors[0]
                if not box: raise JobError(JobErrorKind.INTERNAL, "ComfyUI attempt ended without a result")
                return box[0]
            result = queue.run(job, invoke, partial_artifacts=partial_paths,
                               startup_allowance_seconds=COMFYUI_STARTUP_ALLOWANCE_SECONDS)
            return result
        finally:
            self._epoch += 1
            quiescent = self._control.deactivate(epoch)
            self._cancel = None
            if self._control.child_pid is not None:
                # A successful lease release deliberately leaves the unloaded worker resident.
                pass
            if quiescent:
                self._lock.release()
            else:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "ComfyUI attempt supervisor did not quiesce; lease must remain held")

    def _run_attempt(self, request: ImageRequest, job: Job, queue: JobQueue, run_rel: str, epoch: int,
                     token: str, stop: threading.Event, partial_paths: list[Path],
                     telemetry: list[float]) -> ImageResult:
        self._fence(epoch, queue, token, stop)
        attempt = max(1, job.attempts)
        attempt_rel = f"{run_rel}/attempt-{attempt:04d}"
        self.root.make_dir(attempt_rel)
        runtime_rel = self._runtime_rel
        for rel in (runtime_rel, f"{runtime_rel}/input", f"{runtime_rel}/output", f"{runtime_rel}/temp",
                    f"{runtime_rel}/user", f"{runtime_rel}/frontend", f"{runtime_rel}/user/custom_nodes",
                    f"{runtime_rel}/cache", f"{runtime_rel}/cache/hub", f"{runtime_rel}/cache/torch",
                    f"{runtime_rel}/cache/triton", f"{runtime_rel}/cache/cuda"):
            self.root.make_dir(rel)
        output_name = f"{request.shot_id}-v{request.shot_version:04d}-{request.run_id}-a{attempt:04d}"
        self._output_dirs = [self.root.resolve(f"{runtime_rel}/output/{output_name}")]
        out_rel = f"{runtime_rel}/output/{output_name}"
        self.root.make_dir(out_rel)
        partial_paths.append(self.root.resolve(out_rel))
        self.root.write_with(f"{runtime_rel}/server.log", lambda p: None)
        self._gpu, self._memory = request.gpu_device, request.memory_mode
        self._active_prompt = None
        self._submitting = False
        pins = {pin.role: pin for pin in request.models}
        for pin in pins.values():
            self._fence(epoch, queue, token, stop)
            if not pin.path.is_file(): raise JobError(JobErrorKind.MISSING_MODEL_ARTIFACT, f"model file missing: {pin.path}")
            if pin.path.is_symlink(): raise JobError(JobErrorKind.INVALID_MANIFEST, f"model pin may not be a symlink: {pin.path}")
            if _sha(pin.path, checkpoint=lambda: self._fence(epoch, queue, token, stop)) != pin.sha256:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"model hash mismatch: {pin.path}")
            self._fence(epoch, queue, token, stop)
        model_manifest_rel = f"{attempt_rel}/model-manifest.json"
        self._write_model_config(pins, model_manifest_rel)
        identity = tuple((k, str(p.path.resolve(strict=True)), p.sha256) for k, p in sorted(pins.items())) + (request.gpu_device, request.memory_mode)
        stats = self._control.ensure_ready(identity, self._deadline,
                                           guard=lambda: self._fence(epoch, queue, token, stop))
        self._fence(epoch, queue, token, stop)
        object_info = self.transport.request("GET", "/object_info", timeout=self._remaining())
        self._fence(epoch, queue, token, stop)
        prefix = f"{output_name}/image"
        graph = build_workflow(request, object_info, output_prefix=prefix)
        self.root.write_text(f"{attempt_rel}/object-info.json", json.dumps(object_info, sort_keys=True, indent=2))
        graph_bytes = json.dumps(graph, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.root.write_bytes(f"{attempt_rel}/workflow.json", graph_bytes)
        prompt_id = None
        try:
            self._fence(epoch, queue, token, stop)
            self._control.verify_endpoint()
            self._submitting = True
            try:
                submitted = self.transport.request("POST", "/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex}, timeout=self._remaining())
            except Exception as submit_error:
                if not self._control.terminate(interrupt=False):
                    self._control.mark_unsafe_stop()
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                                   f"prompt response was lost and owned ComfyUI stop is unconfirmed: {submit_error}",
                                   cause=submit_error) from submit_error
                raise
            finally:
                self._submitting = False
            if not isinstance(submitted, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI submission response is malformed")
            prompt_id = submitted.get("prompt_id")
            if not isinstance(prompt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", prompt_id): raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI submission lacks a safe prompt_id")
            self._active_prompt = prompt_id
            job.progress = max(job.progress, .1)
            history = None
            while time.monotonic() < self._deadline:
                self._fence(epoch, queue, token, stop)
                data = self.transport.request("GET", f"/history/{prompt_id}", timeout=self._remaining())
                if not isinstance(data, dict):
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI history response is malformed")
                history = data.get(prompt_id)
                if history is not None: break
                time.sleep(min(.25, self._remaining()))
            if history is None: raise JobError(JobErrorKind.BUDGET_EXCEEDED, "ComfyUI generation deadline exceeded")
            if not isinstance(history, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI history entry is malformed")
            self._fence(epoch, queue, token, stop)
            status = history.get("status", {})
            if not isinstance(status, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI history status is malformed")
            if not isinstance(status.get("completed"), bool) or not isinstance(status.get("status_str"), str):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI history status lacks completion fields")
            if status.get("completed") is not True or status.get("status_str") != "success":
                detail = json.dumps(status, sort_keys=True)
                if "out of memory" in detail.lower() or "bad allocation" in detail.lower():
                    raise JobError(JobErrorKind.OUT_OF_MEMORY, f"ComfyUI CUDA allocation failed: {detail}")
                if "cuda error" in detail.lower(): raise JobError(JobErrorKind.INTERNAL, f"ComfyUI CUDA error: {detail}")
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"ComfyUI history reports failure: {detail}")
            output_map = history.get("outputs")
            if not isinstance(output_map, dict) or not isinstance(output_map.get("13"), dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI history lacks SaveImage output node 13")
            outputs = output_map["13"].get("images")
            if not isinstance(outputs, list) or len(outputs) != 1: raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "expected exactly one SaveImage output")
            item = outputs[0]
            if not isinstance(item, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI SaveImage output descriptor is malformed")
            filename, subfolder, typ = item.get("filename"), item.get("subfolder", ""), item.get("type")
            if typ != "output" or not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.png", filename, re.IGNORECASE) or subfolder != prefix.rsplit("/", 1)[0]:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI output path is outside the authorized attempt")
            try:
                source = self.root.resolve(f"{runtime_rel}/output/{subfolder}/{filename}")
            except Exception as exc:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"ComfyUI output escaped its authorized root: {exc}", cause=exc) from exc
            if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI output is missing or unsafe")
            try:
                with Image.open(source) as image:
                    image.load()
                    if image.format != "PNG" or image.size != (request.width, request.height): raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "generated PNG dimensions or format mismatch")
            except (OSError, ValueError) as exc:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"generated image cannot be decoded: {exc}", cause=exc) from exc
            self._fence(epoch, queue, token, stop)
            retained_rel = f"{attempt_rel}/image.png"
            self.root.write_bytes(retained_rel, source.read_bytes())
            for pin in pins.values():
                if _sha(pin.path, checkpoint=lambda: self._fence(epoch, queue, token, stop)) != pin.sha256:
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"model changed during generation: {pin.path}")
            stats = self.transport.request("GET", "/system_stats", timeout=self._remaining())
            version = str(stats.get("system", {}).get("comfyui_version", stats.get("comfyui_version", "")))
            if version != "0.21.1": raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"unexpected ComfyUI version {version!r}")
            now = time.time()
            metrics = {"vram_baseline_mb": telemetry[0],
                       "vram_peak_mb": max(telemetry[1], queue.nvml.device_memory().used_mb)}
            schema_defaults = _schema_defaults(object_info)
            metadata = {"status": "completed", "prompt_id": prompt_id, "child_pid": self._control.child_pid,
                        "server_version": version, "system_stats": stats, "workflow_sha256": hashlib.sha256(graph_bytes).hexdigest(),
                        "image_sha256": _sha(self.root.resolve(retained_rel)), "models": [_pin_data(p) for p in pins.values()],
                        "object_info": "object-info.json", "model_manifest": "model-manifest.json",
                        "schema_defaults": schema_defaults, "schema_sha256": _sha(self.root.resolve(f"{attempt_rel}/object-info.json")),
                        "server_log": f"{runtime_rel}/server.log", "created_unix": now,
                        "duration_seconds": self.settings.budget.per_shot_wall_clock_seconds-(self._deadline-time.monotonic()),
                        "history": history, **metrics}
            gen = GenerationRun(run_id=request.run_id, tool="comfyui", tool_version=version,
                invocation=graph_bytes.decode(), seed=graph["9"]["inputs"]["noise_seed"],
                settings={"steps": request.steps, "precision": request.precision, "sampler": "euler", "memory_mode": request.memory_mode,
                          "workflow_sha256": metadata["workflow_sha256"], "child_pid": int(self._control.child_pid or 0), "prompt_id": prompt_id,
                          **{f"{p.role}_id": p.path.name for p in pins.values()},
                          **{f"{p.role}_sha256": p.sha256 for p in pins.values()},
                          **{f"{p.role}_license": p.license_id for p in pins.values()}},
                input_paths=[f"{attempt_rel}/workflow.json", f"{attempt_rel}/object-info.json", model_manifest_rel],
                output_paths=[retained_rel, f"{attempt_rel}/metadata.json"], log_path=f"{attempt_rel}/job.jsonl", status="completed",
                resource_metrics=metrics)
            self._fence(epoch, queue, token, stop)
            success_record = {"event":"job_completed", "job_id":job.job_id, "shot_id":job.shot_id,
                              "run_id":job.run_id, "prompt_id":prompt_id, **metrics}
            def commit_success() -> None:
                self._publication_check(epoch, queue, token, stop)
                self.root.write_text(f"{attempt_rel}/metadata.json", json.dumps(metadata, sort_keys=True, indent=2))
                self._publication_check(epoch, queue, token, stop)
                self.root.write_text(f"{attempt_rel}/generation-run.json", gen.model_dump_json(indent=2))
                self._publication_check(epoch, queue, token, stop)
                self.root.write_text(f"{attempt_rel}/job.jsonl", json.dumps(success_record, sort_keys=True)+"\n")
                self._publication_check(epoch, queue, token, stop)
                job.resource_metrics.update(metrics)
                job.progress = max(job.progress, .95)
            self._control.publish(epoch, token, self._deadline, commit_success,
                                  deadline_clock=queue.clock, deadline_value=self._queue_deadline)
            self._active_prompt = None
            return ImageResult((retained_rel,), f"{attempt_rel}/workflow.json", f"{attempt_rel}/metadata.json", f"{attempt_rel}/job.jsonl", gen)
        except Exception as exc:  # noqa: BLE001 - classify every worker failure for durable diagnostics.
            if isinstance(exc, JobError): error = exc
            else:
                kind = JobErrorKind.DISK_EXHAUSTED if getattr(exc, "errno", None) == 28 else JobErrorKind.INTERNAL
                error = JobError(kind, f"ComfyUI attempt failed: {exc}", cause=exc)
            try:
                record = error.to_log_record()
                self.root.write_text(f"{attempt_rel}/job.jsonl", json.dumps(record, sort_keys=True)+"\n")
                self.root.write_text(f"{attempt_rel}/metadata.json", json.dumps({"status":"failed","error":record,"prompt_id":prompt_id},sort_keys=True,indent=2))
                gen_path = self.root.resolve(f"{attempt_rel}/generation-run.json")
                if gen_path.is_file():
                    previous_gen = GenerationRun.model_validate_json(gen_path.read_text(encoding="utf-8"))
                    self.root.write_text(f"{attempt_rel}/generation-run.json",
                                         previous_gen.model_copy(update={"status":"failed"}).model_dump_json(indent=2))
            except OSError as log_exc:
                raise JobError(JobErrorKind.DISK_EXHAUSTED, f"failed to persist original error {error.kind.value}: {error.message}; log failure: {log_exc}", cause=log_exc) from error
            raise error

    def _write_model_config(self, pins: dict, manifest_rel: str) -> None:
        install_configs = (self.settings.binaries.comfyui_root / "extra_model_paths.yaml",
                           self.settings.binaries.comfyui_root / "ComfyUI" / "extra_model_paths.yaml")
        if any(path.exists() for path in install_configs) or self.settings.models.extra_model_paths_file is not None or self.settings.models.model_roots:
            raise JobError(JobErrorKind.INVALID_MANIFEST, "ambient extra model roots are forbidden")
        manifest = []
        for pin in pins.values():
            resolved = pin.path.resolve(strict=True)
            manifest.append({**_pin_data(pin), "resolved_path": str(resolved), "resolved_parent": str(resolved.parent)})
        config = {"cctv_pinned": {
            "base_path": str(self.root.resolve(self._runtime_rel)),
            "is_default": True,
            **{category: str(pin.path.resolve(strict=True).parent) for category, pin in
               (("diffusion_models", pins["diffusion"]),
                ("text_encoders", pins["text_encoder"]), ("vae", pins["vae"]))},
        }}
        self.root.write_text(f"{self._runtime_rel}/models.yaml", json.dumps(config, sort_keys=True, indent=2))
        self.root.write_text(manifest_rel, json.dumps(manifest, sort_keys=True, indent=2))

    def _remaining(self) -> float:
        remaining = min(self._deadline - time.monotonic(), self._queue_deadline - self._queue_clock())
        if remaining <= 0: raise JobError(JobErrorKind.BUDGET_EXCEEDED, "ComfyUI generation deadline exceeded")
        return min(5.0, remaining)

    def _publication_check(self, epoch: int, queue: JobQueue, token: str, stop: threading.Event) -> None:
        if time.monotonic() >= self._deadline or queue.clock() >= self._queue_deadline:
            raise JobError(JobErrorKind.BUDGET_EXCEEDED, "ComfyUI generation deadline exceeded during publication")
        if epoch != self._epoch or stop.is_set() or token in self._blocked_tokens:
            raise JobError(JobErrorKind.CANCELLED, "ComfyUI success publication was stopped")

    def _fence(self, epoch: int, queue: JobQueue, token: str, stop: threading.Event) -> None:
        if epoch != self._epoch: raise JobError(JobErrorKind.CANCELLED, "generation epoch is no longer active")
        if time.monotonic() >= self._deadline or queue.clock() >= self._queue_deadline:
            self._interrupt()
            raise JobError(JobErrorKind.BUDGET_EXCEEDED, "ComfyUI generation deadline exceeded")
        if stop.is_set(): raise JobError(JobErrorKind.CANCELLED, "generation attempt was stopped")
        lease = queue.lease
        record = lease._record
        if not lease._owned or token in self._blocked_tokens or not isinstance(record, dict) or record.get("token") != token or not self.root.resolve("jobs/gpu_lease.json").exists():
            raise JobError(JobErrorKind.CANCELLED, "GPU lease ownership was lost")
        try:
            persisted = json.loads(self.root.resolve("jobs/gpu_lease.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc: raise JobError(JobErrorKind.CANCELLED, "GPU lease record is unavailable", cause=exc) from exc
        if persisted.get("token") != token: raise JobError(JobErrorKind.CANCELLED, "GPU lease token was replaced")
        if self._cancel is not None and self._cancel():
            self._interrupt()
            raise JobError(JobErrorKind.CANCELLED, "generation cancelled by caller")
        if time.monotonic() >= self._deadline or queue.clock() >= self._queue_deadline:
            self._interrupt()
            raise JobError(JobErrorKind.BUDGET_EXCEEDED, "ComfyUI generation deadline exceeded")

    def _interrupt(self) -> None:
        if self._control.child_pid is None:
            return
        if not self._control.endpoint_verified:
            if self._submitting and not self._control.terminate(interrupt=False):
                self._control.mark_unsafe_stop()
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "unverified ComfyUI endpoint could not be safely stopped")
            return
        try:
            self._control.verify_endpoint()
        except JobError as exc:
            if self._control.child_pid is None:
                return
            if not self._control.terminate(interrupt=False):
                self._control.mark_unsafe_stop()
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "ComfyUI endpoint verification failed and child stop is unconfirmed", cause=exc) from exc
            raise
        prompt = self._active_prompt
        if prompt is None:
            if self._submitting and not self._control.terminate(interrupt=False):
                self._control.mark_unsafe_stop()
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "ComfyUI submission response is unknown and child termination is unconfirmed")
            return
        failed = False
        try: self.transport.request("POST", "/interrupt", {}, timeout=1)
        except JobError: failed = True
        try:
            self._control.verify_endpoint()
            self.transport.request("POST", "/queue", {"delete": [prompt]}, timeout=1)
        except JobError: failed = True
        deadline = time.monotonic() + 2.0
        while not failed and time.monotonic() < deadline:
            try:
                queue_state = self.transport.request("GET", "/queue", timeout=.5)
                if not isinstance(queue_state, dict):
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI queue response is malformed")
                running = queue_state.get("queue_running", [])
                pending = queue_state.get("queue_pending", [])
                if not isinstance(running, list) or not isinstance(pending, list):
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI queue response is malformed")
                rows = [*running, *pending]
                if any(not isinstance(row, (list, tuple)) or len(row) < 2 or not isinstance(row[1], str)
                       for row in rows):
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI queue entries are malformed")
                ids = {row[1] for row in rows}
                if prompt not in ids:
                    self._active_prompt = None
                    return
            except JobError:
                failed = True
            time.sleep(.05)
        # A cancellation cannot release the lease while this owned server may still run it.
        if not self._control.terminate(interrupt=False):
            self._control.mark_unsafe_stop()
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ComfyUI did not quiesce and owned child termination was unconfirmed")

    def close(self) -> None:
        self._control.close()


def _sha(path: Path, *, checkpoint: Callable[[], None] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for index, chunk in enumerate(iter(lambda: stream.read(1024 * 1024), b"")):
            digest.update(chunk)
            if checkpoint is not None and index % 64 == 0:
                checkpoint()
    return digest.hexdigest()


def _schema_defaults(object_info: dict) -> dict[str, dict[str, object]]:
    evidence: dict[str, dict[str, object]] = {}
    for node_name, signature in object_info.items():
        if not isinstance(signature, dict):
            continue
        inputs = signature.get("input", {})
        required = inputs.get("required", {}) if isinstance(inputs, dict) else {}
        if not isinstance(required, dict):
            continue
        defaults = {}
        for name, descriptor in required.items():
            if isinstance(descriptor, list) and len(descriptor) > 1 and isinstance(descriptor[1], dict) and "default" in descriptor[1]:
                defaults[name] = descriptor[1]["default"]
        evidence[node_name] = defaults
    return evidence


def _schema_defaults(object_info: dict) -> dict[str, dict[str, object]]:
    evidence: dict[str, dict[str, object]] = {}
    for node_name, signature in object_info.items():
        if not isinstance(signature, dict):
            continue
        inputs = signature.get("input", {})
        required = inputs.get("required", {}) if isinstance(inputs, dict) else {}
        if not isinstance(required, dict):
            continue
        defaults = {}
        for name, descriptor in required.items():
            if isinstance(descriptor, list) and len(descriptor) > 1 and isinstance(descriptor[1], dict) and "default" in descriptor[1]:
                defaults[name] = descriptor[1]["default"]
        evidence[node_name] = defaults
    return evidence


def _pin_data(pin) -> dict[str, str]:
    return {"role":pin.role,"path":str(pin.path),"sha256":pin.sha256,"source":pin.source,"license_id":pin.license_id}
