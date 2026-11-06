"""Contained FFmpeg execution boundary for FR-21 PostProfile degradation."""

from __future__ import annotations

import errno
import hashlib
import itertools
import json
import math
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orchestrator.domain.entities import CameraProfile
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings

from .profile import (
    PostProfileSpec,
    ProfileValidationError,
    build_filter_graph,
    validate_profile,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


class DegradeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str
    shot_version: int = Field(ge=1)
    run_id: str
    degrade_seed: int = Field(ge=0)
    master_relative: str
    font_relative: str
    camera_profile: CameraProfile
    profile: PostProfileSpec

    @field_validator("shot_id", "run_id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("identifier must match [A-Za-z0-9_-]+")
        return value

    @field_validator("master_relative", "font_relative")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if not value or "\x00" in value or Path(value).is_absolute() or re.match(r"^[A-Za-z]:", value):
            raise ValueError("path must be project-relative")
        return value


@dataclass(frozen=True)
class DegradeResult:
    output_path: str
    log_path: str
    metadata_path: str
    artifact_schedule: tuple[Any, ...]
    filter_graph: str
    filter_graph_hash: str
    output_sha256: str
    frame_count: int
    pts_delta_distribution: dict[str, int]
    ffmpeg_identity: str
    profile_semver: str
    pinned_binaries: dict[str, str]


def build_command(
    ffmpeg_exe: Path,
    *,
    master: Path,
    output: Path,
    filter_graph: str,
    codec: str,
    preset: str,
    crf: int,
    pixel_format: str,
    threads: int,
) -> list[str]:
    """Return explicit FFmpeg argv, never a shell command string."""
    return [str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y", "-fflags", "+bitexact",
            "-i", str(master), "-map", "0:v:0", "-vf", filter_graph,
            "-c:v", codec, "-preset", preset, "-crf", str(crf), "-pix_fmt", pixel_format,
            "-threads", str(threads), "-map_metadata", "-1", "-flags", "+bitexact",
            "-fflags", "+bitexact", str(output)]


class PostProfileAdapter:
    def __init__(self, root: ProjectRoot, settings: Settings, *,
                 popen: Callable[..., Any] = subprocess.Popen,
                 ffprobe_exe: Path | None = None) -> None:
        self.root = root
        self.settings = settings
        self.popen = popen
        self.ffmpeg_exe = settings.binaries.ffmpeg_exe
        self.ffprobe_exe = ffprobe_exe or Path("ffprobe")

    def degrade(self, request: DegradeRequest) -> DegradeResult:
        try:
            request = DegradeRequest.model_validate(request.model_dump(mode="python"))
        except (ValidationError, AttributeError, TypeError) as exc:
            raise ValueError(f"invalid degradation request: {exc}") from exc
        master = self.root.resolve(request.master_relative)
        font = self.root.resolve(request.font_relative)
        if not master.is_file():
            raise JobError(JobErrorKind.MISSING_MODEL_ARTIFACT, f"master input is missing: {request.master_relative}",
                           shot_id=request.shot_id, run_id=request.run_id)
        if not font.is_file():
            raise JobError(JobErrorKind.MISSING_MODEL_ARTIFACT, f"overlay font is missing: {request.font_relative}",
                           shot_id=request.shot_id, run_id=request.run_id)
        identity = normalize_ffmpeg_identity(
            self._capture([str(self.ffmpeg_exe), "-hide_banner", "-version"], request)
        )
        if not identity.startswith("ffmpeg version "):
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "FFmpeg did not return a version banner",
                           shot_id=request.shot_id, run_id=request.run_id)
        pinned_identity = request.profile.identity.pinned_binaries.get("ffmpeg")
        if pinned_identity is None or normalize_ffmpeg_identity(pinned_identity) != identity:
            raise JobError(JobErrorKind.INVALID_MANIFEST,
                           "PostProfile FFmpeg pin does not exactly match installed `ffmpeg -version` output",
                           shot_id=request.shot_id, run_id=request.run_id)
        self._check_capabilities(request)
        probe = self._probe(master, request)
        try:
            validated = validate_profile(request.profile, request.camera_profile,
                                         master_frames=probe["frame_count"],
                                         master_rate=Fraction(probe["avg_frame_rate"]))
            graph = build_filter_graph(validated, shot_id=request.shot_id,
                                       degrade_seed=request.degrade_seed,
                                       font_path=request.font_relative)
        except (ProfileValidationError, ValidationError, ValueError) as exc:
            raise JobError(JobErrorKind.INVALID_MANIFEST, str(exc), shot_id=request.shot_id,
                           run_id=request.run_id, cause=exc) from exc

        run_relative = f"post/{request.shot_id}/v{request.shot_version:04d}/{request.run_id}"
        existing = self.root.resolve(run_relative)
        if existing.exists():
            raise FileExistsError(f"post-profile run already exists: {run_relative}")
        marker = f"{run_relative}/.reserved"
        try:
            self.root.make_dir(f"post/{request.shot_id}/v{request.shot_version:04d}")
            self.root.make_dir(run_relative)
            self.root.write_with(marker, lambda path: path.open("xb").close())
        except FileExistsError as exc:
            raise FileExistsError(f"post-profile run already exists: {run_relative}") from exc
        except OSError as exc:
            self._raise_os_error(exc, request)

        output_rel = f"{run_relative}/degraded.mp4"
        log_rel = f"{run_relative}/ffmpeg.log"
        metadata_rel = f"{run_relative}/metadata.json"
        run_log = self.root.resolve(log_rel)
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "status": "running",
            "shot_id": request.shot_id,
            "shot_version": request.shot_version,
            "run_id": request.run_id,
            "profile_name": request.profile.identity.name,
            "profile_semver": request.profile.identity.semver,
            "supersedes": request.profile.supersedes,
            "degrade_seed": request.degrade_seed,
            "master_path": request.master_relative,
            "output_path": output_rel,
            "ffmpeg_identity": identity,
            "filter_graph": graph,
            "filter_graph_hash": hashlib.sha256(graph.encode("utf-8")).hexdigest(),
            "artifact_schedule": [event.model_dump(mode="json") for event in validated.schedule],
            "predicted_frames": validated.predicted_frames,
            "source_probe": probe,
        }
        self._write_json(metadata_rel, metadata, request)
        try:
            def authorize_output(output_path: Path) -> None:
                command = build_command(self.ffmpeg_exe, master=master, output=output_path,
                                        filter_graph=graph, codec=request.profile.encode.codec,
                                        preset=request.profile.encode.preset, crf=request.profile.encode.crf,
                                        pixel_format=request.profile.encode.pix_fmt,
                                        threads=request.profile.encode.threads)

                def write_log(log_path: Path) -> None:
                    with log_path.open("wb") as log_file:
                        try:
                            proc = self.popen(command, stdin=subprocess.DEVNULL, stdout=log_file,
                                              stderr=subprocess.STDOUT, shell=False, cwd=self.root.path)
                        except OSError as exc:
                            raise _LaunchFailure(exc) from exc
                        code = proc.wait()
                        if code != 0:
                            raise _FfmpegExit(code)

                self.root.write_with(log_rel, write_log)

            self.root.write_with(output_rel, authorize_output)
        except _FfmpegExit as exc:
            metadata.update(status="failed", error=f"FFmpeg exited with status {exc.code}")
            self._write_json(metadata_rel, metadata, request)
            try:
                tail = self._tail_log(run_log)
            except OSError as log_exc:
                self._raise_os_error(log_exc, request)
            disk_full = any(value in tail.lower() for value in ("no space left on device", "enospc", "error -28", "error 112"))
            kind = JobErrorKind.DISK_EXHAUSTED if disk_full else JobErrorKind.INTERNAL
            raise JobError(kind,
                           f"FFmpeg exited with status {exc.code}: {tail}",
                           shot_id=request.shot_id, run_id=request.run_id, cause=exc) from exc
        except _LaunchFailure as exc:
            metadata.update(status="failed", error=str(exc.cause))
            self._write_json(metadata_rel, metadata, request)
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"FFmpeg launch failed: {exc.cause}",
                           shot_id=request.shot_id, run_id=request.run_id, cause=exc.cause) from exc
        except FileNotFoundError as exc:
            metadata.update(status="failed", error=str(exc))
            self._write_json(metadata_rel, metadata, request)
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"FFmpeg launch failed: {exc}",
                           shot_id=request.shot_id, run_id=request.run_id, cause=exc) from exc
        except OSError as exc:
            metadata.update(status="failed", error=str(exc))
            self._write_json(metadata_rel, metadata, request)
            self._raise_os_error(exc, request)

        try:
            output = self.root.resolve(output_rel)
            output_is_valid = output.is_file() and output.stat().st_size > 0
        except OSError as exc:
            self._raise_os_error(exc, request)
        if not output_is_valid:
            metadata.update(status="failed", error="FFmpeg output is missing or empty")
            self._write_json(metadata_rel, metadata, request)
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, metadata["error"],
                           shot_id=request.shot_id, run_id=request.run_id)
        try:
            output_probe = self._probe(output, request)
        except JobError as exc:
            metadata.update(status="failed", error=exc.message)
            self._write_json(metadata_rel, metadata, request)
            raise
        if output_probe["frame_count"] != validated.predicted_frames:
            metadata.update(status="failed", error="output frame count does not match predicted schedule",
                            output_probe=output_probe)
            self._write_json(metadata_rel, metadata, request)
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, metadata["error"],
                           shot_id=request.shot_id, run_id=request.run_id)
        try:
            digest = self._sha256(output)
        except OSError as exc:
            self._raise_os_error(exc, request)
        metadata.update(status="completed", output_probe=output_probe, output_sha256=digest)
        self._write_json(metadata_rel, metadata, request)
        return DegradeResult(
            output_path=output_rel,
            log_path=log_rel,
            metadata_path=metadata_rel,
            artifact_schedule=validated.schedule,
            filter_graph=graph,
            filter_graph_hash=metadata["filter_graph_hash"],
            output_sha256=digest,
            frame_count=output_probe["frame_count"],
            pts_delta_distribution=output_probe["pts_delta_distribution"],
            ffmpeg_identity=identity,
            profile_semver=request.profile.identity.semver,
            pinned_binaries={"ffmpeg": identity},
        )

    def _capture(self, argv: list[str], request: DegradeRequest,
                 *, nonzero_kind: JobErrorKind = JobErrorKind.WORKER_UNREACHABLE) -> str:
        try:
            proc = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, shell=False)
            stdout, stderr = proc.communicate()
        except FileNotFoundError as exc:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"could not launch {argv[0]}: {exc}",
                           shot_id=request.shot_id, run_id=request.run_id, cause=exc) from exc
        except OSError as exc:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                           f"could not launch or communicate with {argv[0]}: {exc}",
                           shot_id=request.shot_id, run_id=request.run_id, cause=exc) from exc
        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace")[-3000:]
            cause = subprocess.CalledProcessError(proc.returncode, argv, output=stdout, stderr=stderr)
            raise JobError(nonzero_kind,
                           f"{Path(argv[0]).name} exited {proc.returncode}: {detail}",
                           shot_id=request.shot_id, run_id=request.run_id, cause=cause)
        try:
            return (stdout or b"").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"{Path(argv[0]).name} output was not valid UTF-8",
                           shot_id=request.shot_id, run_id=request.run_id, cause=exc) from exc

    def _check_capabilities(self, request: DegradeRequest) -> None:
        filters = self._capture([str(self.ffmpeg_exe), "-hide_banner", "-filters"], request)
        required = {stage.filter for stage in request.profile.identity.stages}
        if "setpts" in required:
            required.add("settb")
        for name in required:
            if not self._listed_entry(filters, name):
                raise JobError(JobErrorKind.INVALID_MANIFEST,
                               f"installed FFmpeg lacks required filter {name!r}",
                               shot_id=request.shot_id, run_id=request.run_id)
        encoders = self._capture([str(self.ffmpeg_exe), "-hide_banner", "-encoders"], request)
        if not self._listed_entry(encoders, request.profile.encode.codec):
            raise JobError(JobErrorKind.INVALID_MANIFEST,
                           f"installed FFmpeg lacks required encoder {request.profile.encode.codec!r}",
                           shot_id=request.shot_id, run_id=request.run_id)
        global_help = self._capture([str(self.ffmpeg_exe), "-hide_banner", "-h", "full"], request)
        if not re.search(r"^\s*-threads\s", global_help, flags=re.MULTILINE):
            raise JobError(JobErrorKind.INVALID_MANIFEST, "installed FFmpeg lacks required global -threads option",
                           shot_id=request.shot_id, run_id=request.run_id)
        if "noise" in required:
            help_text = self._capture([str(self.ffmpeg_exe), "-hide_banner", "-h", "filter=noise"], request)
            match = re.search(r"^\s*all_seed\s+<int>[^\n]*\(from\s+(-?\d+)\s+to\s+(INT_MAX|\d+)\)",
                              help_text, flags=re.MULTILINE)
            upper = 2**31 - 1 if match and match.group(2) == "INT_MAX" else int(match.group(2)) if match else None
            if not match or upper != 2**31 - 1:
                raise JobError(JobErrorKind.INVALID_MANIFEST,
                               "installed FFmpeg noise all_seed range differs from the measured INT32_MAX contract",
                               shot_id=request.shot_id, run_id=request.run_id)
        if "loop" in required:
            help_text = self._capture([str(self.ffmpeg_exe), "-hide_banner", "-h", "filter=loop"], request)
            if not re.search(r"\bstart\b", help_text):
                raise JobError(JobErrorKind.INVALID_MANIFEST, "installed FFmpeg loop filter has no start option",
                               shot_id=request.shot_id, run_id=request.run_id)
        encoder_help = self._capture([str(self.ffmpeg_exe), "-hide_banner", "-h",
                                      f"encoder={request.profile.encode.codec}"], request)
        for option in ("preset", "crf"):
            if not re.search(rf"^\s*-{option}\s", encoder_help, flags=re.MULTILINE):
                raise JobError(JobErrorKind.INVALID_MANIFEST,
                               f"installed encoder lacks required -{option} option",
                               shot_id=request.shot_id, run_id=request.run_id)

    @staticmethod
    def _listed_entry(output: str, name: str) -> bool:
        """FFmpeg flags columns vary between filters and builds; parse whitespace columns."""
        return any(len(columns := line.split()) >= 2 and columns[1] == name
                   for line in output.splitlines())

    def _probe(self, path: Path, request: DegradeRequest) -> dict[str, Any]:
        raw = self._capture([str(self.ffprobe_exe), "-v", "error", "-select_streams", "v:0",
                             "-count_frames", "-show_entries",
                             "stream=nb_read_frames,avg_frame_rate,time_base,duration,width,height,start_time:frame=pts_time:format=duration",
                             "-show_frames",
                             "-of", "json", str(path)], request,
                            nonzero_kind=JobErrorKind.CORRUPT_ARTIFACT)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("ffprobe JSON root must be an object")
            streams = data.get("streams", [])
            if not streams or not isinstance(streams[0], dict):
                raise ValueError("ffprobe found no video stream")
            stream = streams[0]
            count = int(stream["nb_read_frames"])
            if count < 1:
                raise ValueError("ffprobe returned no video frames")
            frames = data.get("frames")
            if not isinstance(frames, list) or len(frames) != count:
                raise ValueError("ffprobe timestamp count does not match decoded frame count")
            pts = [float(frame["pts_time"]) for frame in frames]
            if any(not math.isfinite(value) for value in pts):
                raise ValueError("ffprobe returned a non-finite frame timestamp")
            if any(right <= left for left, right in itertools.pairwise(pts)):
                raise ValueError("ffprobe frame timestamps are not strictly increasing")
            delta_counts: Counter[str] = Counter()
            for left, right in itertools.pairwise(pts):
                delta_counts[f"{right - left:.6f}"] += 1
            frame_rate = stream.get("avg_frame_rate")
            if not frame_rate or Fraction(frame_rate) <= 0:
                raise ValueError("ffprobe returned no positive avg_frame_rate")
            duration_value = stream.get("duration")
            if duration_value in (None, "N/A") or not math.isfinite(float(duration_value)) or float(duration_value) <= 0:
                format_info = data.get("format")
                duration_value = format_info.get("duration") if isinstance(format_info, dict) else None
            if duration_value in (None, "N/A") or not math.isfinite(float(duration_value)) or float(duration_value) <= 0:
                duration_value = str(Fraction(count, 1) / Fraction(frame_rate))
            duration = Fraction(str(duration_value))
            if duration <= 0:
                raise ValueError("ffprobe duration must be positive")
            if stream.get("start_time") in (None, "N/A"):
                raise ValueError("ffprobe returned no video start_time")
            start_time = Fraction(stream["start_time"])
            if stream.get("time_base") in (None, "N/A"):
                raise ValueError("ffprobe returned no video time_base")
            time_base = Fraction(stream["time_base"])
            if time_base <= 0:
                raise ValueError("ffprobe returned no positive video time_base")
            if abs(start_time) > time_base:
                raise ValueError("video start_time is nonzero beyond one stream time-base tick")
            return {"frame_count": count, "avg_frame_rate": frame_rate,
                    "time_base": stream.get("time_base"), "duration": str(duration),
                    "width": stream.get("width"), "height": stream.get("height"),
                    "pts_delta_distribution": dict(sorted(delta_counts.items()))}
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError, json.JSONDecodeError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"invalid ffprobe result for {path.name}: {exc}",
                           shot_id=request.shot_id, run_id=request.run_id, cause=exc) from exc

    def _write_json(self, relative: str, value: dict[str, Any], request: DegradeRequest) -> None:
        try:
            self.root.write_text(relative, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        except OSError as exc:
            self._raise_os_error(exc, request)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _tail_log(path: Path) -> str:
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - 3000))
            return source.read().decode("utf-8", errors="replace")

    @staticmethod
    def _raise_os_error(exc: OSError, request: DegradeRequest) -> None:
        kind = JobErrorKind.DISK_EXHAUSTED if exc.errno in {errno.ENOSPC, 112} else JobErrorKind.INTERNAL
        raise JobError(kind, f"post-profile filesystem/process error: {exc}",
                       shot_id=request.shot_id, run_id=request.run_id, cause=exc) from exc


class _FfmpegExit(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"FFmpeg exited with status {code}")


class _LaunchFailure(Exception):
    def __init__(self, cause: OSError) -> None:
        self.cause = cause
        super().__init__(str(cause))
