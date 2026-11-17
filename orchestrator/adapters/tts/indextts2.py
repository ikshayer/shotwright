"""IndexTTS-2 (GPU) diegetic speech synthesis, supervised per job.

IndexTTS-2 is not a resident server: each job spawns one `indextts2_worker.py` process,
which loads the models, makes one take and exits. The GPU lease, though, wants a stable
worker identity that exists before the job starts and can be terminated by the watchdog.
`IndexTTS2WorkerControl` bridges the two: its pid is this process (as for H3), and its
`terminate()` kills whichever worker process the current attempt has launched.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from orchestrator.domain.entities import Asset, Job
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound

from .core import (
    INDEXTTS2_AUXILIARY_FILES,
    INDEXTTS2_CHECKPOINTS,
    INDEXTTS2_COMMIT,
    INDEXTTS2_CONFIG,
    INDEXTTS2_CONFIG_SHA256,
    INDEXTTS2_PYTHON,
    INDEXTTS2_REPOSITORY,
    EmotionReference,
    EmotionText,
    EmotionVector,
    IndexTts2Request,
    RunFiles,
    TtsResult,
    TtsSetupError,
    inspect_wav,
    job_error,
    last_lines,
    repository_root,
    sha256_file,
)

WORKER_SCRIPT = Path(__file__).with_name("indextts2_worker.py")
_POLL_SECONDS = 0.2
#: What the worker's result file holds once a take is complete.
_RESULT_KEYS = frozenset({
    "load_seconds", "generation_seconds", "max_memory_allocated_bytes", "max_memory_reserved_bytes",
})
_TERMINATE_GRACE_SECONDS = 5.0
_ATTEMPT_DRAIN_SECONDS = 10.0
_SETTINGS_HINT = "set enabled = true and explicit reservations under [indextts2] in settings.toml"

#: Worker error codes (see indextts2_worker.py) mapped onto the closed FR-27 taxonomy.
_WORKER_ERROR_KINDS = {
    "missing_model_file": JobErrorKind.MISSING_MODEL_ARTIFACT,
    "cuda_unavailable": JobErrorKind.WORKER_UNREACHABLE,
    "out_of_memory": JobErrorKind.OUT_OF_MEMORY,
    "no_audio": JobErrorKind.CORRUPT_ARTIFACT,
    "download_forbidden": JobErrorKind.MISSING_MODEL_ARTIFACT,
}


class _Attempt:
    """One job's claim on the worker identity, and the process it launched."""

    def __init__(self) -> None:
        self.process: Any = None
        self.stopped = False
        self.stops_seen = 0
        self.thread = threading.current_thread()
        self.finished = threading.Event()


def _kill_process_tree(process: Any) -> bool:
    """Terminate, then kill, `process` and its descendants. True once it has exited."""
    if process.poll() is not None:
        return True
    descendants: list[psutil.Process] = []
    if isinstance(process, subprocess.Popen):
        # Only a real child is swept: an injected test double's pid names nothing of ours.
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return False
    for child in descendants:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
    return process.poll() is not None


class IndexTTS2WorkerControl:
    """Lease-facing worker control (`jobs.lease.WorkerControl`) for per-job subprocesses.

    `pid()` is the orchestrator process, a stable identity the lease can record before any
    worker exists. `terminate()` and `unload()` end the current attempt's process, which
    is what releases the GPU; `respawn()` has nothing to restart because the next job
    launches its own process.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempt: _Attempt | None = None
        self._unsafe = False
        self._closed = False
        #: Incremented by every terminate()/unload(), with or without a live attempt. A job
        #: reads it before checking its lease, so a stop delivered at any later moment --
        #: including before begin(), when there is no attempt to mark -- refuses the launch.
        self._stops = 0

    def pid(self) -> int | None:
        return os.getpid()

    def stop_count(self) -> int:
        with self._lock:
            return self._stops

    def begin(self, stops_seen: int | None = None) -> _Attempt:
        with self._lock:
            if self._closed:
                raise JobError(JobErrorKind.CANCELLED, "IndexTTS-2 worker control is closed")
            if stops_seen is not None and stops_seen != self._stops:
                raise JobError(JobErrorKind.CANCELLED,
                               "IndexTTS-2 worker was stopped before this job began")
            previous = self._attempt
            if previous is not None and previous.process is not None and previous.process.poll() is None:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "a previous IndexTTS-2 process is still running; GPU is not free")
            self._unsafe = False
            self._attempt = _Attempt()
            self._attempt.stops_seen = self._stops
            return self._attempt

    def launch(self, attempt: _Attempt, spawn: Callable[[], Any],
               lease_owned: Callable[[], bool],
               cancelled: Callable[[], bool] | None = None) -> Any:
        """Start the process unless the attempt was stopped first (no launch after a kill).

        The attempt is begun before any preparation, so a `terminate()` that lands while the
        job is still preparing marks this attempt and the launch is refused here. A job
        cancellation requested during preparation is refused here too.
        """
        with self._lock:
            if (attempt.stopped or self._attempt is not attempt
                    or attempt.stops_seen != self._stops):
                raise JobError(JobErrorKind.CANCELLED, "IndexTTS-2 attempt stopped before launch")
            if cancelled is not None and cancelled():
                raise JobError(JobErrorKind.CANCELLED, "IndexTTS-2 job cancelled before launch")
            if not lease_owned():
                raise JobError(JobErrorKind.CANCELLED, "GPU lease was lost before IndexTTS-2 launch")
            attempt.process = spawn()
            return attempt.process

    def finish(self, attempt: _Attempt) -> None:
        attempt.finished.set()

    def terminate(self) -> bool:
        with self._lock:
            self._stops += 1
            attempt = self._attempt
            if attempt is None:
                return not self._unsafe
            attempt.stopped = True
            process = attempt.process
        stopped = True if process is None else _kill_process_tree(process)
        if (stopped and attempt.thread is not threading.current_thread()
                and not attempt.finished.wait(_ATTEMPT_DRAIN_SECONDS)):
            # The process is gone but its supervising thread has not finished recording
            # the failure. Not unsafe for the GPU, merely unfinished bookkeeping.
            pass
        with self._lock:
            self._unsafe = not stopped
        return stopped

    def unload(self) -> None:
        if not self.terminate():
            raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                           "IndexTTS-2 process did not stop; GPU lease retained")

    def respawn(self) -> int:
        with self._lock:
            if self._unsafe:
                raise RuntimeError("cannot re-arm IndexTTS-2 after unresolved process cleanup")
        return os.getpid()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        if not self.terminate():
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "IndexTTS-2 worker cleanup is unresolved")


class IndexTTS2Adapter:
    def __init__(self, root: ProjectRoot, settings: Settings, *,
                 popen: Callable[..., Any] = subprocess.Popen,
                 repository: ProjectRoot | None = None) -> None:
        self.root, self.settings, self.popen = root, settings, popen
        self.repository = repository or repository_root()
        self._worker = IndexTTS2WorkerControl()

    @property
    def worker(self) -> IndexTTS2WorkerControl:
        return self._worker

    # -- readiness ---------------------------------------------------------------------

    def require_ready(self) -> None:
        """Cheap pre-submission checks: enabled and the runtime files present."""
        if not self.settings.indextts2.enabled:
            raise TtsSetupError(f"IndexTTS-2 is disabled; {_SETTINGS_HINT}")
        for relative in (INDEXTTS2_PYTHON, INDEXTTS2_CONFIG):
            if not self.repository.resolve(relative).is_file():
                raise TtsSetupError(f"IndexTTS-2 runtime file is missing: {relative}")
        if not WORKER_SCRIPT.is_file():
            raise TtsSetupError(f"IndexTTS-2 worker script is missing: {WORKER_SCRIPT}")

    # -- synthesis ---------------------------------------------------------------------

    def invoke(self, request: Any, job: Job, *, queue: Any,
               cancel: Callable[[], bool] | None = None) -> Any:
        """The job runner's `Adapter` protocol: an `AdapterRequest` carrying an IndexTts2Request."""
        from orchestrator.service.runner import AdapterResult

        result = self.synthesize_managed(request.payload["request"], job, queue=queue,
                                         cancel=cancel or request.payload.get("cancel_check"))
        return AdapterResult(artifact_path=result.take_path, value=result)

    def synthesize_managed(self, request: IndexTts2Request, job: Job, *, queue: Any,
                           cancel: Callable[[], bool] | None = None) -> TtsResult:
        request = IndexTts2Request.model_validate(request.model_dump(mode="python"))
        # Read before the lease check: any stop from here on (a watchdog unload while this
        # job is still checking readiness or preparing) refuses the launch.
        stops_seen = self._worker.stop_count()
        if not queue.lease._owned:
            raise ValueError("synthesize_managed requires an owned GPU lease")
        if job.shot_id != request.shot_id or job.run_id != request.run_id:
            raise ValueError("job shot_id/run_id must match the IndexTTS-2 request")
        self.require_ready()
        files = RunFiles(self.root, request)
        started = time.monotonic()
        report: dict[str, Any] = {
            "engine": "indextts2", "status": "running", "run_id": request.run_id,
            "shot_id": request.shot_id, "shot_version": request.shot_version,
            "settings": request.settings_record, "text_sha256": request.text_sha256,
            "text_bytes": len(request.text.encode("utf-8")), "deterministic": False,
            "use_fp16": self.settings.indextts2.use_fp16,
        }
        # Begun first: from here on a terminate()/unload() for this job is sticky, however
        # long the preparation below takes.
        attempt = self._worker.begin(stops_seen)
        try:
            files.prepare(request.text)
            commit, config_sha256 = self._verify_runtime(request, job)
            identity = f"indextts2 {commit} config.yaml sha256 {config_sha256}"
            report["engine_version"] = identity
            report["runtime"] = {"git_commit": commit, "config_sha256": config_sha256}
            speaker_copy = self._stage_speaker(request, files, job)
            report["speaker"] = {
                "speaker_asset_id": request.speaker_asset_id, "path": request.speaker_path,
                "sha256": request.speaker_sha256, "voice_consent": request.speaker_consent,
                "voice_consent_basis": request.speaker_consent_basis,
                "copy_path": speaker_copy, "copy_sha256": request.speaker_sha256,
            }
            emotion_copy = self._stage_emotion(request, files, job)
            if emotion_copy is not None:
                binding = request.emotion_reference
                report["emotion_reference"] = {
                    "emotion_asset_id": binding.asset_id, "path": binding.path,
                    "sha256": binding.sha256, "voice_consent": binding.consent,
                    "voice_consent_basis": binding.consent_basis,
                    "reuse_scope": binding.reuse_scope, "strength": request.emotion.strength,
                    "seconds_used_at_most": 15.0,
                    "copy_path": emotion_copy, "copy_sha256": binding.sha256,
                }
            worker_result = self._run_worker(request, files, job, attempt, cancel, started,
                                             speaker_copy, queue, emotion_copy)
            facts = inspect_wav(self.root.resolve(files.temporary_take), job=job)
            files.publish_take()
            take_sha256 = sha256_file(self.root.resolve(files.take))
            report.update(
                status="completed", sha256=take_sha256, sample_rate=facts.sample_rate,
                channels=facts.channels, frames=facts.frames,
                duration_seconds=facts.duration_seconds,
                timings={"wall_seconds": time.monotonic() - started,
                         "load_seconds": worker_result["load_seconds"],
                         "generation_seconds": worker_result["generation_seconds"]},
                vram={"max_memory_allocated_bytes": worker_result["max_memory_allocated_bytes"],
                      "max_memory_reserved_bytes": worker_result["max_memory_reserved_bytes"]},
                device={"name": worker_result.get("cuda_device_name"), "torch": worker_result.get("torch")},
            )
            report_sha256 = files.write_report(report)
        except BaseException as exc:
            self._record_failure(files, report, exc, started)
            raise
        finally:
            self._cleanup_scratch(files)
            self._worker.finish(attempt)
        job.resource_metrics = {
            "indextts2_load_seconds": float(worker_result["load_seconds"]),
            "indextts2_generation_seconds": float(worker_result["generation_seconds"]),
            "cuda_max_memory_allocated_bytes": float(worker_result["max_memory_allocated_bytes"]),
            "cuda_max_memory_reserved_bytes": float(worker_result["max_memory_reserved_bytes"]),
        }
        job.progress = 1.0
        return TtsResult(
            engine="indextts2", run_id=request.run_id, tool_version=identity,
            take_path=files.take, take_sha256=take_sha256, text_path=files.text,
            text_sha256=request.text_sha256, report_path=files.report,
            report_sha256=report_sha256, log_path=files.stderr, sample_rate=facts.sample_rate,
            channels=facts.channels, duration_seconds=facts.duration_seconds,
        )

    def _verify_runtime(self, request: IndexTts2Request, job: Job) -> tuple[str, str]:
        """Refuse an unpinned runtime or a changed speaker reference.

        Returns the verified (git commit, config.yaml SHA-256).
        """
        for relative in (INDEXTTS2_PYTHON, INDEXTTS2_CONFIG, *(
                f"{INDEXTTS2_CHECKPOINTS}/{name}" for name in INDEXTTS2_AUXILIARY_FILES)):
            if not self.repository.resolve(relative).is_file():
                raise job_error(JobErrorKind.MISSING_MODEL_ARTIFACT,
                                f"IndexTTS-2 file is missing and will not be downloaded: {relative}", job)
        config_sha256 = sha256_file(self.repository.resolve(INDEXTTS2_CONFIG))
        if config_sha256 != INDEXTTS2_CONFIG_SHA256:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"{INDEXTTS2_CONFIG} does not match the pinned SHA-256", job)
        commit = _git_head(self.repository.resolve(INDEXTTS2_REPOSITORY))
        if commit != INDEXTTS2_COMMIT:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"{INDEXTTS2_REPOSITORY} is at {commit or 'an unreadable commit'}, "
                            f"not the pinned {INDEXTTS2_COMMIT}", job)
        return commit, config_sha256

    def _stage_speaker(self, request: IndexTts2Request, files: RunFiles, job: Job) -> str:
        """Re-verify the speaker Asset now, then hand the worker a private, hashed copy.

        The Asset record is reloaded, so consent withdrawn or a clip swapped since
        submission is refused. The bytes are read once, hashed, and written into the run
        directory; the worker never sees the registered file, so it cannot be substituted
        between this check and the model reading it.
        """
        try:
            asset = ProjectStore(self.root).load(Asset, request.speaker_asset_id)
        except RecordNotFound as exc:
            raise job_error(JobErrorKind.INVALID_MANIFEST,
                            f"speaker asset {request.speaker_asset_id!r} no longer exists", job, exc) from exc
        if (asset.type not in {"voice", "audio"} or asset.file_path != request.speaker_path
                or asset.checksum != request.speaker_sha256
                or asset.voice_consent != request.speaker_consent
                or asset.voice_consent_basis != request.speaker_consent_basis
                or (request.speaker_reuse_scope is not None
                    and asset.reuse_scope != request.speaker_reuse_scope)):
            raise job_error(JobErrorKind.INVALID_MANIFEST,
                            f"speaker asset {request.speaker_asset_id!r} changed (type, file, checksum "
                            "or voice consent) since the request was submitted; refusing", job)
        data = self.root.read_bytes(request.speaker_path)
        if hashlib.sha256(data).hexdigest() != request.speaker_sha256:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"speaker reference {request.speaker_path} no longer matches its "
                            "registered checksum", job)
        copy_rel = f"{request.run_directory}/input/speaker.wav"
        self.root.write_bytes(copy_rel, data)
        if sha256_file(self.root.resolve(copy_rel)) != request.speaker_sha256:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            "the staged speaker copy does not match the registered checksum", job)
        return copy_rel

    def _stage_emotion(self, request: IndexTts2Request, files: RunFiles, job: Job) -> str | None:
        """The emotion clip gets the speaker's treatment: re-verified now, used as a copy."""
        binding = request.emotion_reference
        if binding is None:
            return None
        try:
            asset = ProjectStore(self.root).load(Asset, binding.asset_id)
        except RecordNotFound as exc:
            raise job_error(JobErrorKind.INVALID_MANIFEST,
                            f"emotion asset {binding.asset_id!r} no longer exists", job, exc) from exc
        if (asset.type not in {"voice", "audio"} or asset.file_path != binding.path
                or asset.checksum != binding.sha256 or asset.voice_consent != binding.consent
                or asset.voice_consent_basis != binding.consent_basis
                or asset.reuse_scope != binding.reuse_scope):
            raise job_error(JobErrorKind.INVALID_MANIFEST,
                            f"emotion asset {binding.asset_id!r} changed (type, file, checksum, "
                            "voice consent or reuse scope) since the request was submitted; refusing", job)
        data = self.root.read_bytes(binding.path)
        if hashlib.sha256(data).hexdigest() != binding.sha256:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"emotion reference {binding.path} no longer matches its registered checksum", job)
        copy_rel = f"{request.run_directory}/input/emotion.wav"
        self.root.write_bytes(copy_rel, data)
        if sha256_file(self.root.resolve(copy_rel)) != binding.sha256:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            "the staged emotion copy does not match the registered checksum", job)
        return copy_rel

    def _run_worker(self, request: IndexTts2Request, files: RunFiles, job: Job, attempt: _Attempt,
                    cancel: Callable[[], bool] | None, started: float, speaker_rel: str,
                    queue: Any, emotion_rel: str | None = None) -> dict[str, Any]:
        cfg = self.settings.indextts2
        run = request.run_directory
        request_rel, result_rel = f"{run}/worker_request.json", f"{run}/worker_result.json"
        emotion = request.emotion
        emotion_record: dict[str, Any] = {"mode": emotion.mode}
        if isinstance(emotion, EmotionVector):
            emotion_record["emo_vector"] = emotion.as_list()
        elif isinstance(emotion, EmotionText):
            emotion_record["description"] = emotion.description
        elif isinstance(emotion, EmotionReference):
            if emotion_rel is None:
                raise job_error(JobErrorKind.INVALID_MANIFEST,
                                "emotion mode 'reference' reached the worker without a staged clip", job)
            emotion_record.update(audio_path=str(self.root.resolve(emotion_rel)),
                                  alpha=emotion.strength)
        repo_dir = self.repository.resolve(INDEXTTS2_REPOSITORY)
        checkpoints = self.repository.resolve(INDEXTTS2_CHECKPOINTS)
        self.root.write_bytes(request_rel, json.dumps({
            "repo_dir": str(repo_dir), "model_dir": str(checkpoints),
            "cfg_path": str(self.repository.resolve(INDEXTTS2_CONFIG)),
            "use_fp16": cfg.use_fp16, "speaker_path": str(self.root.resolve(speaker_rel)),
            "text": request.text, "emotion": emotion_record,
            "output_path": str(self.root.resolve(files.temporary_take)),
            "result_path": str(self.root.resolve(result_rel)),
        }, ensure_ascii=False).encode("utf-8"))
        argv = [str(self.repository.resolve(INDEXTTS2_PYTHON)), "-B", "-I", str(WORKER_SCRIPT),
                str(self.root.resolve(request_rel))]
        environment = os.environ | {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        stdout_path, stderr_path = self.root.resolve(files.stdout), self.root.resolve(files.stderr)
        loaded_at: float | None = None
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = self._worker.launch(attempt, lambda: self.popen(
                    argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
                    cwd=repo_dir, env=environment,
                ), lambda: bool(queue.lease._owned), cancel)
            except OSError as exc:
                raise job_error(JobErrorKind.WORKER_UNREACHABLE,
                                f"IndexTTS-2 launch failed: {exc}", job, exc) from exc
            while True:
                try:
                    # No pipes are attached, so this is a bounded wait for exit.
                    process.communicate(timeout=_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if attempt.stopped or (cancel is not None and cancel()):
                    _kill_process_tree(process)
                    raise job_error(JobErrorKind.CANCELLED, "IndexTTS-2 synthesis cancelled", job)
                if loaded_at is None:
                    if self._model_loaded(result_rel):
                        loaded_at = now
                        job.progress = 0.5
                    elif now - started > cfg.startup_allowance_seconds:
                        _kill_process_tree(process)
                        raise job_error(JobErrorKind.BUDGET_EXCEEDED,
                                        f"IndexTTS-2 had not loaded its models within "
                                        f"{cfg.startup_allowance_seconds:g} s "
                                        "([indextts2].startup_allowance_seconds)", job)
                elif now - loaded_at > cfg.execution_timeout_seconds:
                    _kill_process_tree(process)
                    raise job_error(JobErrorKind.BUDGET_EXCEEDED,
                                    f"IndexTTS-2 generation exceeded {cfg.execution_timeout_seconds:g} s "
                                    "([indextts2].execution_timeout_seconds)", job)
        if attempt.stopped:
            raise job_error(JobErrorKind.CANCELLED, "IndexTTS-2 process was stopped by the GPU lease", job)
        return self._read_outcome(process.returncode, result_rel, files, job)

    def _model_loaded(self, result_rel: str) -> bool:
        document = self._read_result(result_rel)
        return document is not None and "load_seconds" in document

    def _read_result(self, result_rel: str) -> dict[str, Any] | None:
        path = self.root.resolve(result_rel)
        if not path.is_file():
            return None
        try:
            document = json.loads(self.root.read_text(result_rel, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None  # Replaced atomically by the worker; an unreadable moment is retried.
        return document if isinstance(document, dict) else None

    def _read_outcome(self, returncode: int, result_rel: str, files: RunFiles, job: Job) -> dict[str, Any]:
        document = self._read_result(result_rel)
        if returncode != 0:
            tail = last_lines(self.root.resolve(files.stderr).read_bytes())
            code = document.get("error_code") if document else None
            kind = _WORKER_ERROR_KINDS.get(code, JobErrorKind.INTERNAL)
            raise job_error(kind, f"IndexTTS-2 worker exited with status {returncode}: {tail}", job)
        if document is None or not _RESULT_KEYS <= document.keys():
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            "IndexTTS-2 worker exited cleanly but wrote no completed result", job)
        return document

    def _cleanup_scratch(self, files: RunFiles) -> None:
        run = files.directory
        for name in ("worker_request.json", "worker_result.json", "worker_result.json.tmp",
                     "input/speaker.wav", "input/emotion.wav"):
            self.root.resolve(f"{run}/{name}").unlink(missing_ok=True)
        try:
            self.root.resolve(f"{run}/input").rmdir()
        except OSError:
            pass  # absent or not empty; the files above are what matter

    def _record_failure(self, files: RunFiles, report: dict[str, Any], error: BaseException,
                        started: float) -> None:
        status = ("cancelled" if isinstance(error, JobError) and error.kind is JobErrorKind.CANCELLED
                  else "failed")
        report.update(status=status, error={
            "type": type(error).__name__, "message": str(error),
            "kind": error.kind.value if isinstance(error, JobError) else None,
        }, timings={"wall_seconds": time.monotonic() - started})
        try:
            files.discard_partial_take()
            self.root.resolve(files.take).unlink(missing_ok=True)
            files.write_report(report)
        except Exception as secondary:  # noqa: BLE001 - never mask the run's own failure.
            error.add_note(f"IndexTTS-2 failure report could not be written: "
                           f"{type(secondary).__name__}: {secondary}")

    def close(self) -> None:
        self._worker.close()


def _git_head(repository: Path) -> str | None:
    """The checked-out commit, read from `.git` without running git. None if unreadable."""
    git = repository / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        reference = head[5:]
        ref_file = git / reference
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip()
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + reference):
                return line.split(" ", 1)[0]
    except OSError:
        return None
    return None
