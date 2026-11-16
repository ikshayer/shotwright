"""Piper (CPU) speech synthesis for processed-source voices.

One subprocess per take. The text goes in on stdin, the interpreter is the Piper venv
under `spikes/05_voice/_work`, and nothing here installs, downloads or modifies that tree.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from orchestrator.domain.entities import Job
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

from .core import (
    PIPER_PYTHON,
    PIPER_SITE_PACKAGES,
    PiperRequest,
    PiperVoice,
    RunFiles,
    TtsRequestError,
    TtsResult,
    TtsSetupError,
    inspect_wav,
    job_error,
    last_lines,
    load_piper_catalog,
    repository_root,
    sha256_file,
)

_POLL_SECONDS = 0.2
_SETTINGS_HINT = "set enabled = true under [piper] in settings.toml"


class PiperAdapter:
    def __init__(self, root: ProjectRoot, settings: Settings, *,
                 popen: Callable[..., Any] = subprocess.Popen,
                 repository: ProjectRoot | None = None) -> None:
        self.root, self.settings, self.popen = root, settings, popen
        self.repository = repository or repository_root()
        self._catalog = load_piper_catalog()

    def voice(self, voice_id: str) -> PiperVoice:
        try:
            return self._catalog[voice_id]
        except KeyError:
            raise TtsRequestError(
                f"voice_id {voice_id!r} is not in the Piper voice catalog; "
                f"allowed: {sorted(self._catalog)}"
            ) from None

    def require_ready(self, voice_id: str) -> PiperVoice:
        """Cheap pre-submission checks: enabled, a catalog voice, files present."""
        if not self.settings.piper.enabled:
            raise TtsSetupError(f"Piper is disabled; {_SETTINGS_HINT}")
        voice = self.voice(voice_id)
        for relative in (PIPER_PYTHON, voice.onnx_relative, voice.config_relative):
            if not self.repository.resolve(relative).is_file():
                raise TtsSetupError(f"Piper runtime file is missing: {relative}")
        return voice

    # -- synthesis ---------------------------------------------------------------------

    def invoke(self, request: Any, job: Job, *, queue: Any = None,
               cancel: Callable[[], bool] | None = None) -> Any:
        """The job runner's `Adapter` protocol: an `AdapterRequest` carrying a PiperRequest."""
        from orchestrator.service.runner import AdapterResult

        result = self.synthesize(request.payload["request"], job, queue=queue,
                                 cancel=cancel or request.payload.get("cancel_check"))
        return AdapterResult(artifact_path=result.take_path, value=result)

    def synthesize(self, request: PiperRequest, job: Job, *, queue: Any = None,
                   cancel: Callable[[], bool] | None = None) -> TtsResult:
        request = PiperRequest.model_validate(request.model_dump(mode="python"))
        if not self.settings.piper.enabled:
            raise TtsSetupError(f"Piper is disabled; {_SETTINGS_HINT}")
        files = RunFiles(self.root, request)
        files.prepare(request.text)
        started = time.monotonic()
        report: dict[str, Any] = {
            "engine": "piper", "status": "running", "run_id": request.run_id,
            "shot_id": request.shot_id, "shot_version": request.shot_version,
            "settings": request.settings_record, "text_sha256": request.text_sha256,
            "text_bytes": len(request.text.encode("utf-8")),
        }
        try:
            voice = self._verified_voice(request.voice_id, job)
            report["engine_version"] = self._piper_version()
            report["voice"] = voice.model_dump(mode="json") | {"verified": True}
            stderr = self._run_piper(request, voice, files, job, cancel)
            self.root.write_bytes(files.stderr, stderr)
            facts = inspect_wav(self.root.resolve(files.temporary_take), job=job)
            files.publish_take()
            take_sha256 = sha256_file(self.root.resolve(files.take))
            report.update(
                status="completed", sha256=take_sha256, sample_rate=facts.sample_rate,
                channels=facts.channels, frames=facts.frames,
                duration_seconds=facts.duration_seconds,
                timings={"wall_seconds": time.monotonic() - started},
            )
            report_sha256 = files.write_report(report)
        except BaseException as exc:
            self._record_failure(files, report, exc, started)
            raise
        return TtsResult(
            engine="piper", run_id=request.run_id, tool_version=report["engine_version"],
            take_path=files.take, take_sha256=take_sha256, text_path=files.text,
            text_sha256=request.text_sha256, report_path=files.report,
            report_sha256=report_sha256, log_path=files.stderr, sample_rate=facts.sample_rate,
            channels=facts.channels, duration_seconds=facts.duration_seconds,
        )

    def _verified_voice(self, voice_id: str, job: Job) -> PiperVoice:
        """Refuse any voice whose bytes differ from the catalog (model weights are code)."""
        voice = self.voice(voice_id)
        onnx = self.repository.resolve(voice.onnx_relative)
        config = self.repository.resolve(voice.config_relative)
        python = self.repository.resolve(PIPER_PYTHON)
        for path in (python, onnx, config):
            if not path.is_file():
                raise job_error(JobErrorKind.MISSING_MODEL_ARTIFACT,
                                f"Piper runtime file is missing: {path}", job)
        if onnx.stat().st_size != voice.onnx_bytes:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"{onnx} is {onnx.stat().st_size} bytes; the catalog says {voice.onnx_bytes}", job)
        if sha256_file(onnx) != voice.onnx_sha256:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"{onnx} does not match the catalog SHA-256 for {voice.voice_id}", job)
        if sha256_file(config) != voice.config_sha256:
            raise job_error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"{config} does not match the catalog SHA-256 for {voice.voice_id}", job)
        return voice

    def _piper_version(self) -> str:
        """The `piper-tts` distribution version, read from its metadata (no process)."""
        site = self.repository.resolve(PIPER_SITE_PACKAGES)
        for metadata in sorted(site.glob("piper_tts-*.dist-info/METADATA")):
            match = re.search(r"^Version:\s*(\S+)", metadata.read_text(encoding="utf-8"), re.MULTILINE)
            if match:
                return match.group(1)
        return "unknown"

    def _run_piper(self, request: PiperRequest, voice: PiperVoice, files: RunFiles, job: Job,
                   cancel: Callable[[], bool] | None) -> bytes:
        argv = [str(self.repository.resolve(PIPER_PYTHON)), "-I", "-m", "piper",
                "-m", str(self.repository.resolve(voice.onnx_relative)),
                "-c", str(self.repository.resolve(voice.config_relative)),
                "-f", str(self.root.resolve(files.temporary_take))]
        if request.length_scale is not None:
            argv += ["--length-scale", repr(request.length_scale)]
        if request.sentence_silence_seconds is not None:
            argv += ["--sentence-silence", repr(request.sentence_silence_seconds)]
        timeout = self.settings.piper.timeout_seconds
        try:
            process = self.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, shell=False, cwd=self.root.path)
        except OSError as exc:
            raise job_error(JobErrorKind.WORKER_UNREACHABLE, f"Piper launch failed: {exc}", job, exc) from exc
        cancelled = threading.Event()
        finished = threading.Event()

        def watch_for_cancellation() -> None:
            # `communicate` blocks for the whole timeout, so a cancel is delivered by a
            # watcher that kills the process out from under it.
            while not finished.wait(_POLL_SECONDS):
                if cancel():
                    cancelled.set()
                    process.kill()
                    return

        watcher = None
        if cancel is not None:
            watcher = threading.Thread(target=watch_for_cancellation, daemon=True,
                                       name=f"piper-cancel-{request.run_id}")
            watcher.start()
        try:
            _, stderr = process.communicate(input=request.text.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            captured = self._kill(process)
            raise self._stopped_error(JobErrorKind.BUDGET_EXCEEDED, files, captured, job,
                                      f"Piper exceeded its {timeout:g} s timeout ([piper].timeout_seconds)") from None
        finally:
            finished.set()
            if watcher is not None:
                watcher.join()
        # The watcher may not have polled yet when something else (the runner, a lease
        # forced unload) ended the process; a cancel is a cancel whoever delivered it.
        if cancelled.is_set() or (cancel is not None and cancel()):
            raise self._stopped_error(JobErrorKind.CANCELLED, files, stderr or b"", job,
                                      "Piper synthesis cancelled")
        stderr = stderr or b""
        if process.returncode != 0:
            self.root.write_bytes(files.stderr, stderr)
            tail = last_lines(stderr)
            kind = (JobErrorKind.DISK_EXHAUSTED if "no space left" in tail.lower()
                    else JobErrorKind.INTERNAL)
            raise job_error(kind, f"Piper exited with status {process.returncode}: {tail}", job)
        return stderr

    @staticmethod
    def _kill(process: Any) -> bytes:
        """Kill the process and return whatever stderr it had produced."""
        process.kill()
        try:
            _, stderr = process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return b""
        return stderr or b""

    def _stopped_error(self, kind: JobErrorKind, files: RunFiles, stderr: bytes, job: Job,
                       message: str) -> JobError:
        """Persist the captured stderr and carry its last lines in the error."""
        self.root.write_bytes(files.stderr, stderr)
        tail = last_lines(stderr)
        return job_error(kind, f"{message}: {tail}" if tail else message, job)

    def _record_failure(self, files: RunFiles, report: dict[str, Any], error: BaseException,
                        started: float) -> None:
        """Leave a failure report and no take. A secondary failure annotates the primary."""
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
            error.add_note(f"Piper failure report could not be written: {type(secondary).__name__}: {secondary}")

    def close(self) -> None:
        return None
