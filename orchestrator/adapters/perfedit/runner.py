"""Contained execution of a PerformanceEdit (FR-32; contract sections 5 and 7).

The edit is sample arithmetic in pure Python (`edit.py`); FFmpeg is used only to synthesise
the breaths, through an injectable `popen` with `shell=False`. Every write goes through the
ProjectRoot, and a failed or cancelled run leaves no take.
"""

from __future__ import annotations

import errno
import hashlib
import json
import re
import subprocess
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orchestrator.adapters.audiopost.runner import capture_ffmpeg_identity, read_wav_facts
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

from .detect import PerformanceEditRequestError, pcm16_wav_bytes, read_take
from .edit import ALGORITHM_VERSION, EditPlan, PerformanceEditSpec, assemble, plan_edit

__all__ = [
    "PerformanceEditAdapter", "PerformanceEditRequest", "PerformanceEditResult",
    "breath_command", "breath_graph",
]

_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
#: A breath is at most 2 s of filtered noise; anything near this deadline is hung.
_FFMPEG_TIMEOUT_SECONDS = 120.0
_POLL_SECONDS = 0.25


class PerformanceEditRequest(BaseModel):
    """One take bound to one Shot version and one edit. Carries no runner state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str = Field(pattern=_ID_PATTERN)
    shot_version: int = Field(ge=1)
    run_id: str = Field(pattern=_ID_PATTERN)
    take_asset_id: str = Field(min_length=1)
    take_path: str = Field(min_length=1)
    take_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    edit: PerformanceEditSpec

    @field_validator("take_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if "\x00" in value or Path(value).is_absolute() or re.match(r"^[A-Za-z]:", value):
            raise ValueError("path must be project-relative")
        return value

    @property
    def run_directory(self) -> str:
        return f"generated/perfedit/{self.shot_id}/v{self.shot_version:04d}/{self.run_id}"


@dataclass(frozen=True)
class PerformanceEditResult:
    take_path: str
    take_sha256: str
    report_path: str
    report_sha256: str
    log_path: str
    tool_version: str
    ffmpeg_identity: str
    sample_rate: int
    duration_seconds: float
    clamped_samples: int
    wall_seconds: float


def breath_graph(*, duration_ms: float, gain_db: float, sample_rate: int, seed: int) -> str:
    """The approved spike 05 breath: band-limited brown noise, an exponential in, a quarter-sine out."""
    duration = duration_ms / 1000
    fade_in = 0.62 * duration
    fade_out = 0.38 * duration
    return (
        f"anoisesrc=color=brown:amplitude=0.9:seed={seed}:duration={duration:.6f}"
        f":sample_rate={sample_rate},"
        "highpass=f=380:p=2,lowpass=f=2100:p=2,"
        f"afade=t=in:st=0:d={fade_in:.6f}:curve=exp,"
        f"afade=t=out:st={fade_in:.6f}:d={fade_out:.6f}:curve=qsin,"
        f"volume={gain_db:g}dB"
    )


def breath_command(ffmpeg_exe: Path, *, graph: str, output: Path, sample_rate: int) -> list[str]:
    """Explicit FFmpeg argv, never a shell string."""
    return [str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y", "-threads", "1",
            "-fflags", "+bitexact", "-f", "lavfi", "-i", graph,
            "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "1",
            "-flags", "+bitexact", "-fflags", "+bitexact", "-map_metadata", "-1",
            "-f", "wav", str(output)]


class _Run:
    """Per-apply state: the combined FFmpeg log."""

    def __init__(self, request: PerformanceEditRequest, job: Any) -> None:
        self.request, self.job = request, job
        self.log: list[str] = []

    def error(self, kind: JobErrorKind, message: str, cause: BaseException | None = None) -> JobError:
        return JobError(kind, message, job_id=getattr(self.job, "job_id", None),
                        shot_id=self.request.shot_id, run_id=self.request.run_id, cause=cause)


class PerformanceEditAdapter:
    #: Runs on the CPU: the runner never gives it a GPU lease.
    def __init__(self, root: ProjectRoot, settings: Settings, *,
                 popen: Callable[..., Any] | None = None) -> None:
        self.root = root
        self.settings = settings
        # Looked up when the adapter is built, not bound at import, so a substitute
        # installed later (tests, sandboxes) is honoured.
        self.popen = popen or subprocess.Popen
        self.ffmpeg_exe = settings.binaries.ffmpeg_exe

    def close(self) -> None:
        return None

    def invoke(self, request: Any, job: Any, *, queue: Any = None,
               cancel: Callable[[], bool] | None = None) -> Any:
        """The job runner's `Adapter` protocol: an `AdapterRequest` carrying a PerformanceEditRequest."""
        from orchestrator.service.runner import AdapterResult

        result = self.apply(request.payload["request"], job, cancel=cancel)
        return AdapterResult(artifact_path=result.take_path, value=result)

    def apply(self, request: PerformanceEditRequest, job: Any, *,
              cancel: Callable[[], bool] | None = None) -> PerformanceEditResult:
        run = _Run(request, job)
        try:
            # Re-validated, so the bounds hold for a request that was built unchecked.
            request = PerformanceEditRequest.model_validate(request.model_dump(mode="python"))
        except (ValidationError, AttributeError, TypeError) as exc:
            raise run.error(JobErrorKind.INVALID_MANIFEST, f"invalid performance edit request: {exc}", exc) from exc
        run.request = request
        self._recheck_source_record(request, run)
        source = self.root.resolve(request.take_path)
        if not source.is_file():
            raise run.error(JobErrorKind.MISSING_MODEL_ARTIFACT, f"source take is missing: {request.take_path}")
        # One read: the bytes that are hashed are the bytes that are decoded, so a file
        # replaced between a check and a second read cannot pass as the registered take.
        try:
            source_bytes = self.root.read_bytes(request.take_path)
        except OSError as exc:
            raise self._os_error(run, exc) from exc
        if hashlib.sha256(source_bytes).hexdigest() != request.take_sha256:
            raise run.error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"source take {request.take_asset_id!r} no longer matches its registered checksum")
        try:
            take = read_take(source_bytes)
            plan = plan_edit(take, request.take_asset_id, request.edit)
        except PerformanceEditRequestError as exc:
            raise run.error(JobErrorKind.INVALID_MANIFEST, str(exc), exc) from exc
        identity = self._identity(run)

        directory = request.run_directory
        if self.root.resolve(directory).exists():
            raise FileExistsError(f"performance edit run already exists: {directory}")
        take_rel = f"{directory}/take.wav"
        report_path, log_path = f"{directory}/report.json", f"{directory}/logs/ffmpeg.log"
        try:
            self.root.make_dir(f"{directory}/logs")
        except OSError as exc:
            raise self._os_error(run, exc) from exc
        started = time.monotonic()
        report: dict[str, Any] = {
            "schema_version": 1, "status": "running", "run_id": request.run_id,
            "shot_id": request.shot_id, "shot_version": request.shot_version,
            "algorithm_version": ALGORITHM_VERSION, "ffmpeg_identity": identity,
            "source": {"asset_id": request.take_asset_id, "path": request.take_path},
            "source_sha256": request.take_sha256,
            "edit": request.edit.model_dump(mode="json"),
        }
        try:
            report.update(self._plan_record(plan))
            self._raise_if_cancelled(run, cancel)
            breaths = self._synthesise_breaths(run, request, plan, directory, cancel)
            samples, clamped = assemble(take, plan, breaths)
            self._raise_if_cancelled(run, cancel)
            encoded = pcm16_wav_bytes(samples, take.sample_rate)
            try:
                self.root.write_bytes(take_rel, encoded)
            except OSError as exc:
                raise self._os_error(run, exc) from exc
            take_sha256 = hashlib.sha256(encoded).hexdigest()
            wall = time.monotonic() - started
            report.update(status="completed", output_sha256=take_sha256, clamped_samples=clamped,
                          sample_rate=take.sample_rate, source_samples=len(take.samples),
                          output_samples=len(samples), wall_seconds=wall)
            report_sha256 = self._write_report(run, report_path, report)
            self._write_log(run, log_path)
        except BaseException as exc:
            self._record_failure(run, directory, report_path, log_path, report, exc)
            raise
        return PerformanceEditResult(
            take_path=take_rel, take_sha256=take_sha256, report_path=report_path,
            report_sha256=report_sha256, log_path=log_path, tool_version=ALGORITHM_VERSION,
            ffmpeg_identity=identity, sample_rate=take.sample_rate,
            duration_seconds=len(samples) / take.sample_rate, clamped_samples=clamped,
            wall_seconds=wall,
        )

    # -- the plan on the record -------------------------------------------------------

    @staticmethod
    def _plan_record(plan: EditPlan) -> dict[str, Any]:
        rate = plan.sample_rate
        return {
            "detection": plan.detection.model_dump(mode="json"),
            "speech_onset": plan.scan.speech_onset,
            "pauses": [{"index": p.index, "start": p.start, "end": p.end,
                        "natural_ms": p.natural_ms(rate)} for p in plan.scan.pauses],
            "cuts": list(plan.cuts),
            "segment_map": plan.segment_map(),
            "breath_seeds": {str(index): seed for index, seed in plan.breath_seeds.items()},
            "jitter": dict(plan.jitter_values),
        }

    # -- breaths -----------------------------------------------------------------------

    def _synthesise_breaths(self, run: _Run, request: PerformanceEditRequest, plan: EditPlan,
                            directory: str, cancel: Callable[[], bool] | None) -> dict[int, array]:
        specs = {item.index: item.breath for item in request.edit.pauses if item.breath is not None}
        lengths = {p.breath_index: p.length for p in plan.pieces if p.kind == "breath"}
        breaths: dict[int, array] = {}
        for index, breath in specs.items():
            self._raise_if_cancelled(run, cancel)
            relative = f"{directory}/work/breath{index}.wav"
            graph = breath_graph(duration_ms=breath.duration_ms, gain_db=breath.gain_db,
                                 sample_rate=plan.sample_rate, seed=plan.breath_seeds[index])
            try:
                self.root.write_with(relative, lambda target, graph=graph: self._run(run, breath_command(
                    self.ffmpeg_exe, graph=graph, output=target, sample_rate=plan.sample_rate), cancel))
                breaths[index] = self._read_breath(run, relative, plan.sample_rate, lengths[index])
            except OSError as exc:
                raise self._os_error(run, exc) from exc
            finally:
                self.root.resolve(relative).unlink(missing_ok=True)
        if specs:
            self.root.resolve(f"{directory}/work").rmdir()
        return breaths

    def _read_breath(self, run: _Run, relative: str, sample_rate: int, length: int) -> array:
        """Decode the FFmpeg breath; trim or zero-pad it to exactly `length` samples."""
        try:
            data = self.root.read_bytes(relative)
            facts = read_wav_facts(data)
            if (facts.format_tag, facts.channels, facts.bits_per_sample, facts.sample_rate) != (1, 1, 16, sample_rate):
                raise ValueError(f"unexpected breath format {facts}")
            samples = read_take(data).samples[:length]
        except (ValueError, PerformanceEditRequestError) as exc:
            raise run.error(JobErrorKind.CORRUPT_ARTIFACT, f"synthesised breath is unreadable: {exc}", exc) from exc
        if len(samples) < length:
            samples.extend(array("h", bytes(2 * (length - len(samples)))))
        return samples

    # -- checks, processes and files ----------------------------------------------------

    def _identity(self, run: _Run) -> str:
        try:
            return capture_ffmpeg_identity(self.ffmpeg_exe, self.popen)
        except JobError as exc:
            raise run.error(exc.kind, exc.message, exc) from exc

    def _recheck_source_record(self, request: PerformanceEditRequest, run: _Run) -> None:
        """Re-read the source take's registry record at execution (contract section 1).

        Submission checked it; a job may wait in the queue, so a consent withdrawn or
        changed to `licensed_recording`, or a record repointed at other bytes, is refused
        before any file is written.
        """
        from orchestrator.domain.entities import CLONABLE_VOICE_CONSENT, Asset
        from orchestrator.store.json_store import ProjectStore, RecordNotFound

        try:
            asset = ProjectStore(self.root).load(Asset, request.take_asset_id)
        except RecordNotFound as exc:
            raise run.error(JobErrorKind.INVALID_MANIFEST,
                            f"source take {request.take_asset_id!r} no longer exists", exc) from exc
        if (asset.type not in {"voice", "audio"} or asset.file_path != request.take_path
                or asset.checksum != request.take_sha256):
            raise run.error(JobErrorKind.INVALID_MANIFEST,
                            f"source take {request.take_asset_id!r} changed (type, file or checksum) "
                            "since the request was submitted; refusing")
        if asset.voice_consent not in CLONABLE_VOICE_CONSENT:
            raise run.error(JobErrorKind.INVALID_MANIFEST,
                            f"source take {request.take_asset_id!r} now has voice_consent "
                            f"{asset.voice_consent!r}; an edited voice needs consent that permits "
                            "it to be heard as a voice (FR-16)")

    def _run(self, run: _Run, argv: list[str], cancel: Callable[[], bool] | None = None) -> None:
        """Run one FFmpeg process and append its output to the log.

        The wait polls for cancellation and has a hard deadline (FR-27): a cancelled or hung
        process is killed and the job fails, instead of blocking the runner indefinitely.
        """
        try:
            proc = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, shell=False, cwd=self.root.path)
        except OSError as exc:
            raise run.error(JobErrorKind.WORKER_UNREACHABLE, f"FFmpeg launch failed: {exc}", exc) from exc
        deadline = time.monotonic() + _FFMPEG_TIMEOUT_SECONDS
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel():
                    _kill(proc)
                    raise run.error(JobErrorKind.CANCELLED, "performance edit cancelled while FFmpeg ran")
                if time.monotonic() >= deadline:
                    _kill(proc)
                    raise run.error(JobErrorKind.BUDGET_EXCEEDED,
                                    f"FFmpeg did not finish within {_FFMPEG_TIMEOUT_SECONDS:g} s; killed")
        text = (stderr or b"").decode("utf-8", errors="replace")
        run.log.append(f"$ {subprocess.list2cmdline(argv)}\n{text}\n"
                       f"{(stdout or b'').decode('utf-8', errors='replace')}")
        if proc.returncode != 0:
            tail = text[-3000:]
            disk_full = any(marker in tail.lower() for marker in
                            ("no space left on device", "enospc", "error -28", "error 112"))
            raise run.error(JobErrorKind.DISK_EXHAUSTED if disk_full else JobErrorKind.INTERNAL,
                            f"FFmpeg exited with status {proc.returncode}: {tail}")

    def _write_report(self, run: _Run, relative: str, report: dict[str, Any]) -> str:
        encoded = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
                   + "\n").encode("utf-8")
        try:
            self.root.write_bytes(relative, encoded)
        except OSError as exc:
            raise self._os_error(run, exc) from exc
        return hashlib.sha256(encoded).hexdigest()

    def _write_log(self, run: _Run, relative: str) -> None:
        try:
            self.root.write_text(relative, "\n".join(run.log))
        except OSError as exc:
            raise self._os_error(run, exc) from exc

    def _record_failure(self, run: _Run, directory: str, report_path: str, log_path: str,
                        report: dict[str, Any], failure: BaseException) -> None:
        """Leave no take behind and record why. A failure to record annotates the original."""
        leftovers = [self.root.resolve(f"{directory}/take.wav"),
                     *self.root.resolve(directory).glob("work/breath*.wav")]
        for path in leftovers:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                failure.add_note(f"could not remove {path.name}: {exc}")
        try:
            self.root.resolve(f"{directory}/work").rmdir()
        except OSError:
            pass  # Absent, or not empty because a removal above failed and was noted.
        report.update(status="cancelled" if _is_cancel(failure) else "failed", error=str(failure))
        report.pop("output_sha256", None)
        try:
            self._write_report(run, report_path, report)
            self._write_log(run, log_path)
        except (OSError, JobError, ValueError) as exc:
            failure.add_note(f"could not record the failed performance edit run: {exc}")

    @staticmethod
    def _raise_if_cancelled(run: _Run, cancel: Callable[[], bool] | None) -> None:
        if cancel is not None and cancel():
            raise run.error(JobErrorKind.CANCELLED, "job cancelled")

    @staticmethod
    def _os_error(run: _Run, exc: OSError) -> JobError:
        kind = JobErrorKind.DISK_EXHAUSTED if exc.errno in {errno.ENOSPC, 112} else JobErrorKind.INTERNAL
        return run.error(kind, f"performance edit filesystem error: {exc}", exc)


def _is_cancel(exc: BaseException) -> bool:
    return isinstance(exc, JobError) and exc.kind is JobErrorKind.CANCELLED


def _kill(proc: Any) -> None:
    """Kill a process and reap it; a process that cannot be reaped is left to the OS."""
    try:
        proc.kill()
        proc.communicate(timeout=5)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass  # already gone, or unkillable; the job still fails and nothing is published
