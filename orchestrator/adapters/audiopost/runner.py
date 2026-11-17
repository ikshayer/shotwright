"""Contained FFmpeg execution boundary for AudioPostProfile (FR-32, contract 4 and 5).

Two loudnorm passes and one ebur128 measurement, all through an injectable `popen` with
`shell=False`; every write goes through the ProjectRoot. The take is published only after
the encoded file's loudness has been verified.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import struct
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orchestrator.adapters.tts.core import sha256_file
from orchestrator.domain.entities import AudioPostProfile
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings

from .profile import (
    LOUDNORM_MEASURE,
    LOUDNESS_TOLERANCE_LU,
    TARGET_LUFS,
    LoudnessMeasurementError,
    LoudnormMeasurement,
    build_filter_graph,
    noise_seed,
    parse_integrated_loudness,
    parse_loudnorm_report,
    stage_records,
)

__all__ = [
    "AudioPostAdapter", "AudioPostRequest", "AudioPostRequestError", "AudioPostResult",
    "build_command", "capture_ffmpeg_identity", "read_wav_facts",
]

_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


class AudioPostRequestError(ValueError):
    """A request to apply an AudioPostProfile is invalid (the API layer maps this to 422)."""


class AudioPostRequest(BaseModel):
    """One take bound to one Shot version and one profile. Carries no runner state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str = Field(pattern=_ID_PATTERN)
    shot_version: int = Field(ge=1)
    run_id: str = Field(pattern=_ID_PATTERN)
    take_asset_id: str = Field(min_length=1)
    take_path: str = Field(min_length=1)
    take_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: AudioPostProfile

    @field_validator("take_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if "\x00" in value or Path(value).is_absolute() or re.match(r"^[A-Za-z]:", value):
            raise ValueError("path must be project-relative")
        return value

    @property
    def run_directory(self) -> str:
        return f"generated/audio_post/{self.shot_id}/v{self.shot_version:04d}/{self.run_id}"

    @property
    def noise_seed(self) -> int | None:
        return noise_seed(self.profile, self.take_asset_id)

    def graph(self, loudnorm: str) -> str:
        return build_filter_graph(self.profile, seed=self.noise_seed, loudnorm=loudnorm)


@dataclass(frozen=True)
class AudioPostResult:
    take_path: str
    take_sha256: str
    report_path: str
    report_sha256: str
    log_path: str
    filter_graph_hash: str
    noise_seed: int | None
    measured_lufs: float
    tool_version: str
    ffmpeg_identity: str
    sample_rate: int
    codec: str
    duration_seconds: float
    wall_seconds: float


@dataclass(frozen=True)
class WavFacts:
    format_tag: int
    channels: int
    sample_rate: int
    bits_per_sample: int
    frames: int

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate


#: WAVE format tags for the two codecs a profile may encode with.
_WAVE_FORMAT_TAGS = {"pcm_s16le": 1, "pcm_mulaw": 7}


def read_wav_facts(data: bytes) -> WavFacts:
    """Parse a RIFF/WAVE header. The stdlib `wave` module refuses mu-law, so this does it."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt: tuple[int, int, int, int] | None = None
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        (size,) = struct.unpack_from("<I", data, offset + 4)
        body = offset + 8
        if chunk_id == b"fmt " and size >= 16:
            tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, body)
            fmt = (tag, channels, rate, bits)
        elif chunk_id == b"data":
            if fmt is None:
                raise ValueError("data chunk precedes the fmt chunk")
            tag, channels, rate, bits = fmt
            available = min(size, len(data) - body)
            width = channels * bits // 8
            if width <= 0 or rate <= 0:
                raise ValueError("fmt chunk describes no samples")
            return WavFacts(tag, channels, rate, bits, available // width)
        offset = body + size + (size & 1)
    raise ValueError("no data chunk")


def build_command(ffmpeg_exe: Path, *, source: Path, filter_graph: str, output: Path | None,
                  codec: str, sample_rate: int) -> list[str]:
    """Explicit FFmpeg argv, never a shell string. `output=None` measures to the null muxer."""
    command = [str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y", "-threads", "1",
               "-fflags", "+bitexact", "-i", str(source),
               "-filter_complex", filter_graph, "-map", "[out]"]
    if output is not None:
        command += ["-c:a", codec, "-ar", str(sample_rate), "-ac", "1"]
    command += ["-flags", "+bitexact", "-fflags", "+bitexact", "-map_metadata", "-1"]
    command += ["-f", "wav", str(output)] if output is not None else ["-f", "null", "-"]
    return command


def _measure_command(ffmpeg_exe: Path, encoded: Path) -> list[str]:
    return [str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y", "-threads", "1",
            "-i", str(encoded), "-af", "ebur128=framelog=quiet", "-f", "null", "-"]


def capture_ffmpeg_identity(ffmpeg_exe: Path, popen: Callable[..., Any] | None = None) -> str:
    """Run `ffmpeg -hide_banner -version` and return its normalised banner.

    `popen` defaults to `subprocess.Popen` looked up at call time, not bound at import, so
    a substitute installed later (tests, sandboxes) is honoured.
    """
    popen = popen or subprocess.Popen
    argv = [str(ffmpeg_exe), "-hide_banner", "-version"]
    try:
        proc = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, shell=False)
        stdout, stderr = proc.communicate()
    except OSError as exc:
        raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"could not launch {argv[0]}: {exc}",
                       cause=exc) from exc
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace")[-1000:]
        raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                       f"{Path(argv[0]).name} -version exited {proc.returncode}: {detail}")
    identity = normalize_ffmpeg_identity((stdout or b"").decode("utf-8", errors="replace"))
    if not identity.startswith("ffmpeg version "):
        raise JobError(JobErrorKind.WORKER_UNREACHABLE, "FFmpeg did not return a version banner")
    return identity


class _Run:
    """Per-apply state: the combined FFmpeg log of every pass."""

    def __init__(self, request: AudioPostRequest, job: Any) -> None:
        self.request, self.job = request, job
        self.log: list[str] = []

    def error(self, kind: JobErrorKind, message: str, cause: BaseException | None = None) -> JobError:
        return JobError(kind, message, job_id=getattr(self.job, "job_id", None),
                        shot_id=self.request.shot_id, run_id=self.request.run_id, cause=cause)


class AudioPostAdapter:
    #: Runs on the CPU: the runner never gives it a GPU lease.
    def __init__(self, root: ProjectRoot, settings: Settings, *,
                 popen: Callable[..., Any] | None = None) -> None:
        self.root = root
        self.settings = settings
        # Looked up when the adapter is built, not bound at import (see capture_ffmpeg_identity).
        self.popen = popen or subprocess.Popen
        self.ffmpeg_exe = settings.binaries.ffmpeg_exe

    def close(self) -> None:
        return None

    def invoke(self, request: Any, job: Any, *, queue: Any = None,
               cancel: Callable[[], bool] | None = None) -> Any:
        """The job runner's `Adapter` protocol: an `AdapterRequest` carrying an AudioPostRequest."""
        from orchestrator.service.runner import AdapterResult

        result = self.apply(request.payload["request"], job, cancel=cancel)
        return AdapterResult(artifact_path=result.take_path, value=result)

    def apply(self, request: AudioPostRequest, job: Any, *,
              cancel: Callable[[], bool] | None = None) -> AudioPostResult:
        run = _Run(request, job)
        try:
            # Re-validated, so the allowlist holds for a request that was built unchecked.
            request = AudioPostRequest.model_validate(request.model_dump(mode="python"))
        except (ValidationError, AttributeError, TypeError) as exc:
            raise run.error(JobErrorKind.INVALID_MANIFEST, f"invalid audio post request: {exc}", exc) from exc
        run.request = request
        profile = request.profile
        self._recheck_source_record(request, run)
        source = self.root.resolve(request.take_path)
        if not source.is_file():
            raise run.error(JobErrorKind.MISSING_MODEL_ARTIFACT, f"source take is missing: {request.take_path}")
        if sha256_file(source) != request.take_sha256:
            raise run.error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"source take {request.take_asset_id!r} no longer matches its registered checksum")
        identity = self._identity(run)
        pinned = (profile.pinned_binaries or {}).get("ffmpeg")
        if pinned is None or normalize_ffmpeg_identity(pinned) != identity:
            raise run.error(JobErrorKind.INVALID_MANIFEST,
                            f"AudioPostProfile {profile.name!r} FFmpeg pin does not exactly match "
                            "the installed `ffmpeg -version` output")

        directory = request.run_directory
        if self.root.resolve(directory).exists():
            raise FileExistsError(f"audio post run already exists: {directory}")
        take, partial = f"{directory}/take.wav", f"{directory}/take.partial.wav"
        report_path, log_path = f"{directory}/report.json", f"{directory}/logs/ffmpeg.log"
        try:
            self.root.make_dir(f"{directory}/logs")
        except OSError as exc:
            raise self._os_error(run, exc) from exc
        started = time.monotonic()
        seed = request.noise_seed
        report: dict[str, Any] = {
            "schema_version": 1, "status": "running", "run_id": request.run_id,
            "shot_id": request.shot_id, "shot_version": request.shot_version,
            "profile": {"name": profile.name, "semver": profile.semver,
                        "voice_stages": stage_records(profile)},
            "noise_seed": seed, "ffmpeg_identity": identity,
            "source": {"asset_id": request.take_asset_id, "path": request.take_path,
                       "sha256": request.take_sha256},
        }
        try:
            self._raise_if_cancelled(run, cancel)
            pass1 = self._run(run, build_command(
                self.ffmpeg_exe, source=source, filter_graph=request.graph(LOUDNORM_MEASURE),
                output=None, codec=profile.encode.codec, sample_rate=profile.encode.sample_rate))
            try:
                measurement = parse_loudnorm_report(pass1)
            except LoudnessMeasurementError as exc:
                raise run.error(JobErrorKind.CORRUPT_ARTIFACT, str(exc), exc) from exc
            graph = request.graph(measurement.filter_text)
            graph_hash = hashlib.sha256(graph.encode("utf-8")).hexdigest()
            report.update(filter_graph=graph, filter_graph_hash=graph_hash,
                          pass1_measurement=measurement.as_record())
            self._raise_if_cancelled(run, cancel)
            self._encode(run, source, partial, graph, profile)
            measured = self._measure(run, self.root.resolve(partial))
            if abs(measured - TARGET_LUFS) > LOUDNESS_TOLERANCE_LU:
                raise run.error(JobErrorKind.CORRUPT_ARTIFACT,
                                f"encoded take measures {measured:.1f} LUFS, more than "
                                f"{LOUDNESS_TOLERANCE_LU} LU from the {TARGET_LUFS:g} LUFS target; "
                                "the take is discarded")
            facts = self._verify_encoded(run, partial, profile)
            os.replace(self.root.resolve(partial), self.root.resolve(take))
            take_sha256 = sha256_file(self.root.resolve(take))
            wall = time.monotonic() - started
            report.update(
                status="completed", measured_lufs=measured, sha256=take_sha256,
                sample_rate=facts.sample_rate, codec=profile.encode.codec,
                duration_seconds=facts.duration_seconds, wall_seconds=wall,
            )
            report_sha256 = self._write_report(run, report_path, report)
            self._write_log(run, log_path)
        except BaseException as exc:
            self._record_failure(run, directory, report_path, log_path, report, exc)
            raise
        version = identity.splitlines()[0]
        return AudioPostResult(
            take_path=take, take_sha256=take_sha256, report_path=report_path,
            report_sha256=report_sha256, log_path=log_path, filter_graph_hash=graph_hash,
            noise_seed=seed, measured_lufs=measured, tool_version=version,
            ffmpeg_identity=identity, sample_rate=facts.sample_rate, codec=profile.encode.codec,
            duration_seconds=facts.duration_seconds, wall_seconds=wall,
        )

    # -- passes ------------------------------------------------------------------------

    def _identity(self, run: _Run) -> str:
        try:
            return capture_ffmpeg_identity(self.ffmpeg_exe, self.popen)
        except JobError as exc:
            raise run.error(exc.kind, exc.message, exc) from exc

    def _encode(self, run: _Run, source: Path, partial: str, graph: str,
                profile: AudioPostProfile) -> None:
        def write(target: Path) -> None:
            self._run(run, build_command(
                self.ffmpeg_exe, source=source, filter_graph=graph, output=target,
                codec=profile.encode.codec, sample_rate=profile.encode.sample_rate))

        try:
            self.root.write_with(partial, write)
        except OSError as exc:
            raise self._os_error(run, exc) from exc

    def _measure(self, run: _Run, encoded: Path) -> float:
        stderr = self._run(run, _measure_command(self.ffmpeg_exe, encoded))
        try:
            return parse_integrated_loudness(stderr)
        except LoudnessMeasurementError as exc:
            raise run.error(JobErrorKind.CORRUPT_ARTIFACT, str(exc), exc) from exc

    def _recheck_source_record(self, request: AudioPostRequest, run: _Run) -> None:
        """Re-read the source take's registry record at execution (contract 3.2).

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
                            f"{asset.voice_consent!r}; a processed voice needs consent that permits "
                            "it to be heard as a voice (FR-16)")

    def _verify_encoded(self, run: _Run, partial: str, profile: AudioPostProfile) -> WavFacts:
        try:
            facts = read_wav_facts(self.root.read_bytes(partial))
        except (ValueError, OSError) as exc:
            raise run.error(JobErrorKind.CORRUPT_ARTIFACT, f"encoded take is not a readable WAV file: {exc}", exc) from exc
        expected = (_WAVE_FORMAT_TAGS[profile.encode.codec], 1, profile.encode.sample_rate)
        if (facts.format_tag, facts.channels, facts.sample_rate) != expected or facts.frames < 1:
            raise run.error(JobErrorKind.CORRUPT_ARTIFACT,
                            f"encoded take is {facts}, expected format/channels/rate {expected}")
        return facts

    def _run(self, run: _Run, argv: list[str]) -> str:
        """Run one FFmpeg process, append its output to the log, and return its stderr."""
        try:
            proc = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, shell=False, cwd=self.root.path)
            stdout, stderr = proc.communicate()
        except OSError as exc:
            raise run.error(JobErrorKind.WORKER_UNREACHABLE, f"FFmpeg launch failed: {exc}", exc) from exc
        text = (stderr or b"").decode("utf-8", errors="replace")
        run.log.append(f"$ {subprocess.list2cmdline(argv)}\n{text}\n"
                       f"{(stdout or b'').decode('utf-8', errors='replace')}")
        if proc.returncode != 0:
            tail = text[-3000:]
            disk_full = any(marker in tail.lower() for marker in
                            ("no space left on device", "enospc", "error -28", "error 112"))
            raise run.error(JobErrorKind.DISK_EXHAUSTED if disk_full else JobErrorKind.INTERNAL,
                            f"FFmpeg exited with status {proc.returncode}: {tail}")
        return text

    # -- files -------------------------------------------------------------------------

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
        for name in ("take.partial.wav", "take.wav"):
            try:
                self.root.resolve(f"{directory}/{name}").unlink(missing_ok=True)
            except OSError as exc:
                failure.add_note(f"could not remove {name}: {exc}")
        report.update(status="cancelled" if _is_cancel(failure) else "failed", error=str(failure))
        try:
            self._write_report(run, report_path, report)
            self._write_log(run, log_path)
        except (OSError, JobError, ValueError) as exc:
            failure.add_note(f"could not record the failed audio post run: {exc}")

    @staticmethod
    def _raise_if_cancelled(run: _Run, cancel: Callable[[], bool] | None) -> None:
        if cancel is not None and cancel():
            raise run.error(JobErrorKind.CANCELLED, "job cancelled")

    @staticmethod
    def _os_error(run: _Run, exc: OSError) -> JobError:
        kind = JobErrorKind.DISK_EXHAUSTED if exc.errno in {errno.ENOSPC, 112} else JobErrorKind.INTERNAL
        return run.error(kind, f"audio post filesystem error: {exc}", exc)


def _is_cancel(exc: BaseException) -> bool:
    return isinstance(exc, JobError) and exc.kind is JobErrorKind.CANCELLED

