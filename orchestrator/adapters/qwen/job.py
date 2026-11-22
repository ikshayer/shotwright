"""Queued Qwen audio review adapter and lease-facing worker control."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.adapters.qwen.core import AnalysisRequest, QwenRequestError, QwenSetupError
from orchestrator.adapters.qwen.runner import ROOT, QwenAdapter
from orchestrator.domain.entities import Job
from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.service.audio_review import (
    persist_audio_review_report,
    persist_minimal_audio_review,
    remove_staged_audio_review_input,
    stage_audio_review_input,
    settings_for_audio_review,
)
from orchestrator.store.json_store import ProjectStore


class _Attempt:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.cleanup_verified = False


def _cleanup_is_verified(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False
    batches = report.get("batches")
    if not isinstance(batches, list):
        return False
    for batch in batches:
        if not isinstance(batch, dict):
            return False
        cleanup = batch.get("owned_process_tree_cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("verified_stopped") is not True:
            return False
        if cleanup.get("survivors") or cleanup.get("unverifiable_identities") or cleanup.get("failures"):
            return False
    return True


def _exception_cleanup_is_verified(exc: BaseException) -> bool:
    """Use cleanup evidence attached by a helper exception; unknown means unverified."""
    for name in ("helper_report", "report"):
        report = getattr(exc, name, None)
        if isinstance(report, dict):
            return _cleanup_is_verified(report)
    batches = getattr(exc, "batches", None)
    if isinstance(batches, list):
        return _cleanup_is_verified({"batches": batches})
    cleanup = getattr(exc, "owned_process_tree_cleanup", None)
    if isinstance(cleanup, dict):
        return _cleanup_is_verified({"batches": [{"owned_process_tree_cleanup": cleanup}]})
    if isinstance(exc, (ContainmentError, JobError, QwenRequestError, QwenSetupError)):
        return True  # These validation/setup failures occur before native inference starts.
    return False


class QwenAudioReviewWorkerControl:
    """Expose the active supervisor to the production GPU lease and watchdog."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempt: _Attempt | None = None
        self._pending_stop = False
        self._last_cleanup_verified = True

    def pid(self) -> int:
        return os.getpid()

    def begin(self) -> _Attempt:
        with self._lock:
            if not self._last_cleanup_verified:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "the previous Qwen supervisor cleanup was not verified")
            if self._attempt is not None and not self._attempt.finished.is_set():
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               "a previous Qwen review is still cleaning up")
            attempt = _Attempt()
            if self._pending_stop:
                attempt.stop.set()
                self._pending_stop = False
            self._attempt = attempt
            self._last_cleanup_verified = True
            return attempt

    def request_queue_stop(self) -> None:
        with self._lock:
            if self._attempt is not None and not self._attempt.finished.is_set():
                self._attempt.stop.set()
            else:
                self._pending_stop = True

    def record_cleanup(self, attempt: _Attempt, verified: bool) -> None:
        with self._lock:
            attempt.cleanup_verified = verified is True

    def finish(self, attempt: _Attempt) -> None:
        with self._lock:
            self._last_cleanup_verified = attempt.cleanup_verified
            attempt.finished.set()

    def terminate(self) -> bool:
        with self._lock:
            attempt = self._attempt
            if attempt is None or attempt.finished.is_set():
                return self._last_cleanup_verified
            attempt.stop.set()
        attempt.finished.wait()
        with self._lock:
            return attempt.cleanup_verified

    def unload(self) -> None:
        if not self.terminate():
            raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                           "the last Qwen supervisor cleanup was not verified; GPU lease retained")

    def respawn(self) -> int:
        return os.getpid()


class QwenAudioReviewAdapter:
    """Run a frozen review input through the existing helper under JobQueue's lease."""

    def __init__(
        self,
        root: ProjectRoot,
        settings: Settings,
        *,
        backend: Any | None = None,
        repository_root: ProjectRoot | None = None,
    ) -> None:
        self.root = root
        self.settings = settings_for_audio_review(settings)
        self.repository_root = repository_root or ProjectRoot(ROOT)
        self.qwen = QwenAdapter(self.repository_root, backend=backend)
        self._worker = QwenAudioReviewWorkerControl()

    @property
    def worker(self) -> QwenAudioReviewWorkerControl:
        return self._worker

    def invoke(
        self,
        request: Any,
        job: Job,
        *,
        queue: Any,
        cancel: Callable[[], bool] | None = None,
    ) -> Any:
        from orchestrator.service.runner import AdapterResult

        payload = request.payload
        if (not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict)
                or payload.get("audio_review_id") != job.run_id or job.shot_id is not None):
            raise JobError(JobErrorKind.INVALID_MANIFEST,
                           "audio review job identity or request snapshot is invalid")
        snapshot = payload["snapshot"]
        if not queue.lease._owned:
            raise JobError(JobErrorKind.CANCELLED,
                           "audio review requires the currently owned production GPU lease")
        attempt = self._worker.begin()
        started = time.monotonic()
        staged_rel: str | None = None
        helper_report: dict[str, Any] | None = None
        caught: BaseException | None = None
        helper_invoked = False
        try:
            staged_rel = (
                f"_work/qwen_review_inputs/{payload['audio_review_id']}/source"
                f"{Path(snapshot['source_path']).suffix.lower()}"
            )
            staged_rel, input_format = stage_audio_review_input(
                ProjectStore(self.root), self.repository_root, snapshot,
                audio_review_id=payload["audio_review_id"],
            )
            model = AnalysisRequest(
                media=staged_rel,
                purpose="audio_review",
                brief=snapshot["brief"],
                modality="audio" if snapshot["mode"] == "audio" else "joint",
                start_seconds=snapshot["start_sec"],
                duration_seconds=snapshot["duration_sec"],
            )

            def should_stop() -> bool:
                return attempt.stop.is_set() or (cancel is not None and cancel())

            def update_progress(value: float) -> None:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    job.progress = max(job.progress, min(1.0, max(0.0, float(value))))

            helper_invoked = True
            helper_report = self.qwen.run_managed(
                model,
                lease=queue.lease,
                cancel=should_stop,
                input_format=input_format,
                expected_sha256=snapshot["target"]["sha256"],
                progress=update_progress,
            )
            self._worker.record_cleanup(attempt, _cleanup_is_verified(helper_report))
        except BaseException as exc:  # noqa: BLE001 - cleanup/report before classifying the failed attempt.
            caught = exc
            self._worker.record_cleanup(
                attempt, _exception_cleanup_is_verified(exc) if helper_invoked else True,
            )

        cleanup_message = (
            remove_staged_audio_review_input(self.repository_root, staged_rel)
            if staged_rel is not None else None
        )
        store = ProjectStore(self.root)
        if helper_report is None:
            mapped = self._map_exception(caught or RuntimeError("Qwen helper returned no report"))
            status = "cancelled" if mapped.kind is JobErrorKind.CANCELLED else "failed"
            family = "startup" if isinstance(caught, QwenSetupError) else "job"
            errors = [{
                "family": family, "kind": mapped.kind.value, "message": mapped.message,
                "retryable": mapped.retryable, "remediation": mapped.remediation,
            }]
            if cleanup_message:
                errors.append(self._cleanup_error(cleanup_message))
            try:
                persist_minimal_audio_review(
                    store, audio_review_id=payload["audio_review_id"], job_id=job.job_id,
                    snapshot=snapshot, status=status, errors=errors,
                )
            finally:
                self._worker.finish(attempt)
            job.resource_metrics["qwen_audio_review_wall_seconds"] = time.monotonic() - started
            raise mapped
        elif cleanup_message:
            helper_report = dict(helper_report)
            helper_report["errors"] = [*helper_report.get("errors", []), self._cleanup_error(cleanup_message)]
            if helper_report.get("status") == "completed":
                helper_report["status"] = "failed"

        helper_report = dict(helper_report)
        coverage = helper_report.get("input_coverage")
        if not isinstance(coverage, dict) or not isinstance(coverage.get("requested_interval"), list):
            interval = [snapshot["start_sec"], snapshot["start_sec"] + snapshot["duration_sec"]]
            helper_report["input_coverage"] = {
                "requested_interval": interval,
                "completed_intervals": [],
                "uncovered_intervals": [interval],
            }
        helper_report.setdefault("errors", [])
        helper_report.setdefault("windows", [])
        helper_report.setdefault("batches", [])
        helper_report.setdefault("input_coverage", {})
        helper_report.setdefault("has_truncated_responses", False)
        helper_report.setdefault("limitations", [])
        helper_report.setdefault("media", {"path": snapshot["source_path"]})
        helper_report.setdefault("brief", {"text": snapshot["brief"], "sha256": snapshot["brief_sha256"]})
        try:
            report_rel = persist_audio_review_report(
                store,
                audio_review_id=payload["audio_review_id"],
                job_id=job.job_id,
                snapshot=snapshot,
                helper_report=helper_report,
            )
        finally:
            self._worker.finish(attempt)
        job.resource_metrics["qwen_audio_review_wall_seconds"] = time.monotonic() - started
        status = helper_report.get("status")
        if status == "cancelled":
            raise self._report_error(helper_report, JobErrorKind.CANCELLED)
        if status == "failed":
            raise self._report_error(helper_report, JobErrorKind.INTERNAL)
        if status != "completed":
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"Qwen helper returned unexpected terminal status {status!r}")
        job.progress = 1.0
        return AdapterResult(artifact_path=report_rel, value=helper_report)

    @staticmethod
    def _cleanup_error(message: str) -> dict[str, Any]:
        kind = JobErrorKind.INTERNAL
        return {
            "family": "job", "kind": kind.value, "message": message,
            "retryable": kind.retryable, "remediation": kind.remediation,
        }

    @staticmethod
    def _map_exception(exc: BaseException) -> JobError:
        if isinstance(exc, JobError):
            return exc
        if isinstance(exc, QwenSetupError):
            return JobError(JobErrorKind.MISSING_MODEL_ARTIFACT, str(exc), cause=exc)
        if isinstance(exc, QwenRequestError):
            return JobError(JobErrorKind.INVALID_MANIFEST, str(exc), cause=exc)
        if isinstance(exc, KeyboardInterrupt):
            return JobError(JobErrorKind.CANCELLED,
                            "Qwen audio review interrupted after helper cleanup", cause=exc)
        return JobError.from_exception(exc, origin="adapter")

    @staticmethod
    def _report_error(report: dict[str, Any], default_kind: JobErrorKind) -> JobError:
        errors = report.get("errors")
        error = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        try:
            kind = JobErrorKind(error.get("kind", default_kind.value))
        except (TypeError, ValueError):
            kind = JobErrorKind.INTERNAL
        message = error.get("message")
        if not isinstance(message, str) or not message:
            message = "Qwen audio review failed" if default_kind is not JobErrorKind.CANCELLED else "Qwen audio review cancelled"
        return JobError(kind, message)

    def close(self) -> None:
        self._worker.unload()
