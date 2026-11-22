"""Queued CPU adapter for episode cut assembly."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.adapters.timeline.runner import TimelineAssembler
from orchestrator.domain.entities import EpisodeOutput
from orchestrator.jobs.process import CpuProcessSupervisor
from orchestrator.jobs.queue import ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.service.timeline import assemble_episode
from orchestrator.store.json_store import ProjectStore


class CutAssemblyAdapter:
    """Build each cut with its own supervisor and a cancellation watcher."""

    def __init__(self, root: ProjectRoot, settings: Settings) -> None:
        self.root, self.settings = root, settings

    def _quarantine_partials(self, episode_id: str, run_id: str | None) -> JobError | None:
        """Move this job's leftover cut files out of the normal cuts folder (FR-27).

        A failed or cancelled encode can leave a partial MP4, credit-card text and a
        log behind with no receipt. They are named after the job's output id (its run
        id), so only this job's files are moved. Returns the error instead of raising,
        so the caller can report it without hiding the assembly's own failure.
        """
        if not run_id:
            return None
        relative = f"episodes/{episode_id}/outputs/cuts"
        try:
            directory = self.root.resolve(relative)
            if not directory.is_dir():
                return None
            leftovers = sorted(
                entry for entry in directory.iterdir()
                if entry.is_file() and (entry.name.startswith(f"{run_id}.") or entry.name.startswith(f"{run_id}-"))
            )
            if leftovers:
                ProjectQuarantine(self.root).quarantine(leftovers)
        except Exception as exc:  # noqa: BLE001 - returned to the caller, which reports it.
            return exc if isinstance(exc, JobError) else JobError(
                JobErrorKind.INTERNAL, f"could not quarantine partial cut outputs: {exc}", cause=exc,
            )
        return None

    def invoke(
        self,
        request: Any,
        job: Any,
        *,
        queue: Any,
        cancel: Callable[[], bool] | None = None,
    ) -> EpisodeOutput:
        del queue
        episode_id = request.episode_id or request.record_id
        credit_card_duration_sec = request.payload.get("credit_card_duration_sec", 5.0)
        control = CpuProcessSupervisor()
        finished = threading.Event()
        watcher_errors: list[JobError] = []

        def stop_until_exited(reason: JobErrorKind) -> None:
            while True:
                try:
                    if control.stop_for(reason):
                        return
                except JobError as exc:
                    if not watcher_errors:
                        watcher_errors.append(exc)
                except Exception as exc:  # noqa: BLE001 - keep supervising until the child is gone.
                    if not watcher_errors:
                        watcher_errors.append(JobError(
                            JobErrorKind.WORKER_UNREACHABLE,
                            f"cut assembly supervisor could not stop FFmpeg: {exc}",
                            cause=exc,
                        ))
                time.sleep(0.25)

        def watch_cancellation() -> None:
            while not finished.wait(0.25):
                try:
                    should_cancel = cancel is not None and cancel()
                except Exception as exc:  # noqa: BLE001 - propagate watcher failures to the owning job.
                    if not watcher_errors:
                        watcher_errors.append(JobError(
                            JobErrorKind.INTERNAL,
                            f"cut assembly cancellation check failed: {exc}",
                            cause=exc,
                        ))
                    stop_until_exited(JobErrorKind.INTERNAL)
                    return
                if should_cancel:
                    stop_until_exited(JobErrorKind.CANCELLED)
                    return

        watcher = threading.Thread(
            target=watch_cancellation,
            name=f"cut-assembly-cancel-{job.job_id}",
            daemon=True,
        )
        ffmpeg = Path(self.settings.binaries.ffmpeg_exe)
        sibling = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
        ffprobe = sibling if ffmpeg.is_absolute() and sibling.is_file() else Path("ffprobe")
        encoder = TimelineAssembler(
            self.root,
            self.settings,
            control,
            ffprobe_exe=ffprobe,
        )
        store = ProjectStore(self.root)
        watcher.start()
        receipt: EpisodeOutput | None = None
        primary_error: Exception | None = None
        try:
            receipt = assemble_episode(
                store,
                encoder,
                episode_id=episode_id,
                run_id=job.run_id,
                budget_seconds=self.settings.budget.cut_assembly_wall_clock_seconds,
                credit_card_duration_sec=credit_card_duration_sec,
                # The cut is named after the job, so the CLI and callers can find
                # exactly this job's cut rather than the newest one.
                output_id=job.run_id,
            )
        except Exception as exc:
            primary_error = exc
        finally:
            finished.set()
            watcher.join()
            if receipt is None:
                # Any failure, cancellation or worker exit: no FFmpeg may outlive the
                # job. stop_for is idempotent and keeps a cancellation's reason.
                stop_until_exited(control.stop_reason or JobErrorKind.INTERNAL)
                quarantine_error = self._quarantine_partials(episode_id, job.run_id)
                if quarantine_error is not None:
                    if primary_error is not None:
                        primary_error.add_note(f"partial cut outputs were not quarantined: {quarantine_error}")
                    else:
                        watcher_errors.append(quarantine_error)

        if watcher_errors:
            watcher_error = watcher_errors[0]
            if primary_error is not None:
                watcher_error.add_note(
                    f"cut assembly also failed: {type(primary_error).__name__}: {primary_error}"
                )
            raise watcher_error
        if primary_error is not None:
            raise primary_error
        if receipt is None:
            raise JobError(JobErrorKind.INTERNAL, "cut assembly returned no EpisodeOutput receipt")
        return receipt
