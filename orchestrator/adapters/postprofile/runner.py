"""Contained FFmpeg execution boundary for FR-21 PostProfile degradation."""

from __future__ import annotations

import errno
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from queue import Empty, Queue
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from orchestrator.domain.entities import CameraProfile
from orchestrator.domain.seeds import INT32_MAX, derive_seed
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings

from .profile import (
    PostProfileSpec,
    ProfileValidationError,
    build_filter_graph,
    build_filter_graph_parts,
    ntsc_settings_from_analog_artifacts,
    validate_profile,
    validate_ntsc_frame_indices,
    validate_ntsc_geometry,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
_NTSC_RS_COMMIT = "8cfc51014092a6dc1da5eb0217bb9ae6516b124e"
_NTSC_RS_RELEASE_VERSION = "0.9.7"
_NTSCRS_CRATE_VERSION = "0.1.2"
_NTSC_PIPE_PROTOCOL_ERROR_EXIT_CODE = 2
_ANALOG_X264_CODECS = frozenset({"libx264", "libx264rgb"})


@contextmanager
def _open_executable_read_locked(path: Path) -> Iterator[Any]:
    """Open a binary while denying Windows write and delete sharing until context exit."""
    if os.name != "nt":
        with path.open("rb") as stream:
            yield stream
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    # FILE_SHARE_READ permits CreateProcess to read the image while excluding opens
    # that would modify or remove the verified path before process creation completes.
    handle = create_file(
        str(path), 0x80000000, 0x00000001, None, 3, 0x00000080, None,
    )
    handle_value = handle if isinstance(handle, int) else handle.value
    if handle_value in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle_value, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    try:
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield stream


class DegradeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str
    shot_version: int = Field(ge=1)
    run_id: str
    degrade_seed: int = Field(ge=0)
    master_relative: str
    font_relative: str | None = None
    camera_profile: CameraProfile
    profile: PostProfileSpec

    @field_validator("shot_id", "run_id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("identifier must match [A-Za-z0-9_-]+")
        return value

    @field_validator("master_relative")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        if not value or "\x00" in value or Path(value).is_absolute() or re.match(r"^[A-Za-z]:", value):
            raise ValueError("path must be project-relative")
        return value

    @field_validator("font_relative")
    @classmethod
    def _optional_relative_font_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
            or value.startswith("/")
            or Path(value).is_absolute()
            or re.match(r"^[A-Za-z]:", value)
            or any(part in {"", ".."} for part in value.split("/"))
        ):
            raise ValueError(
                "font_relative must use [A-Za-z0-9_./-] path segments without a leading / or .. segment"
            )
        return value

    @model_validator(mode="after")
    def _font_matches_overlay_stages(self) -> DegradeRequest:
        has_drawtext = any(stage.filter == "drawtext" for stage in self.profile.identity.stages)
        if has_drawtext and self.font_relative is None:
            raise ValueError("font_relative is required when the PostProfile has a drawtext stage")
        return self


@dataclass(frozen=True)
class DegradeResult:
    output_path: str
    log_path: str
    metadata_path: str
    artifact_schedule: tuple[Any, ...]
    filter_graph: str
    filter_graph_hash: str
    postprofile_spec_sha256: str
    output_sha256: str
    frame_count: int
    output_fps: int
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
    bitrate_kbps: int | None = None,
    block_artifacts: float = 0.0,
) -> list[str]:
    """Return explicit FFmpeg argv, never a shell command string."""
    command = [str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y", "-fflags", "+bitexact",
               "-i", str(master), "-map", "0:v:0", "-vf", filter_graph,
               "-c:v", codec, "-preset", preset]
    if bitrate_kbps is None:
        command.extend(["-crf", str(crf)])
    else:
        command.extend([
            "-b:v", f"{bitrate_kbps}k",
            "-maxrate", f"{bitrate_kbps}k",
            "-bufsize", f"{bitrate_kbps * 2}k",
        ])
    if block_artifacts > 0:
        if codec != "libx264":
            raise ProfileValidationError(
                f"block_artifacts is supported only by libx264; codec {codec!r} has no quantizer-floor mapping"
            )
        qpmin = round(18 + 33 * block_artifacts)
        # qpmin is an x264 private parameter, not an FFmpeg libx264 AVOption on
        # this build; -x264-params is the accepted route. The weakest loop filter
        # strength is applied explicitly alongside it.
        command.extend(["-x264-params", f"qpmin={qpmin}", "-deblock", "-6:-6"])
    command.extend(["-pix_fmt", pixel_format, "-threads", str(threads),
                    "-map_metadata", "-1", "-flags", "+bitexact",
                    "-fflags", "+bitexact", str(output)])
    return command


def build_analog_producer_command(
    ffmpeg_exe: Path,
    *,
    master: Path,
    filter_graph: str,
    frames: int,
) -> list[str]:
    """Decode and run the validated pre-tape graph to packed RGB24 stdout."""
    return [
        str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y", "-filter_threads", "1",
        "-threads", "1", "-fflags", "+bitexact", "-i", str(master),
        "-map", "0:v:0", "-an", "-vf", filter_graph,
        "-frames:v", str(frames), "-fps_mode", "passthrough", "-pix_fmt", "rgb24",
        "-flags", "+bitexact", "-fflags", "+bitexact",
        "-f", "rawvideo", "pipe:1",
    ]


def build_analog_encoder_command(
    ffmpeg_exe: Path,
    *,
    output: Path,
    width: int,
    height: int,
    frame_rate: int,
    filter_graph: str,
    codec: str,
    preset: str,
    crf: int,
    pixel_format: str,
    threads: int,
    bitrate_kbps: int | None = None,
    block_artifacts: float = 0.0,
) -> list[str]:
    """Encode packed RGB24 stdin, applying only the post-tape metadata/filter suffix."""
    command = [
        str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y", "-filter_threads", "1",
        "-fflags", "+bitexact",
        "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{width}x{height}",
        "-framerate", str(frame_rate), "-i", "pipe:0", "-map", "0:v:0",
    ]
    if filter_graph:
        command.extend(["-vf", filter_graph])
    command.extend(["-fps_mode", "passthrough", "-c:v", codec, "-preset", preset])
    if bitrate_kbps is None:
        command.extend(["-crf", str(crf)])
    else:
        command.extend([
            "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
            "-bufsize", f"{bitrate_kbps * 2}k",
        ])
    x264_params: list[str] = []
    if codec in _ANALOG_X264_CODECS:
        # Macroblock-tree lookahead produced different first packets across
        # identical one-thread encodes of the same RGB input on the measured host.
        x264_params.append("mbtree=0")
    if block_artifacts > 0:
        if codec != "libx264":
            raise ProfileValidationError(
                f"block_artifacts is supported only by libx264; codec {codec!r} has no quantizer-floor mapping"
            )
        qpmin = round(18 + 33 * block_artifacts)
        x264_params.append(f"qpmin={qpmin}")
        command.extend(["-deblock", "-6:-6"])
    if x264_params:
        command.extend(["-x264-params", ":".join(x264_params)])
    command.extend([
        "-pix_fmt", pixel_format, "-threads", str(threads), "-map_metadata", "-1",
        "-flags", "+bitexact", "-fflags", "+bitexact", str(output),
    ])
    return command


class PostProfileAdapter:
    def __init__(self, root: ProjectRoot, settings: Settings, *,
                 popen: Callable[..., Any] = subprocess.Popen,
                 ffprobe_exe: Path | None = None) -> None:
        self.root = root
        self.settings = settings
        self.popen = popen
        self.ffmpeg_exe = settings.binaries.ffmpeg_exe
        self.ffprobe_exe = ffprobe_exe or Path("ffprobe")
        self.ntsc_pipe_exe = Path(settings.binaries.ntsc_pipe_exe).expanduser().resolve()
        self._cancel_check: Callable[[], bool] | None = None
        self._deadline: float | None = None

    def set_cancel_check(self, check: Callable[[], bool] | None) -> None:
        """Install a parent job cancellation callback for direct adapter callers."""
        self._cancel_check = check

    def set_deadline(self, deadline: float | None) -> None:
        """Install a monotonic deadline supplied by the job scheduler."""
        self._deadline = deadline

    def degrade(
        self,
        request: DegradeRequest,
        *,
        cancel: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> DegradeResult:
        cancel = cancel or self._cancel_check
        if deadline is None:
            deadline = self._deadline
        if deadline is None:
            deadline = time.monotonic() + self.settings.budget.per_shot_wall_clock_seconds
        try:
            request = DegradeRequest.model_validate(request.model_dump(mode="python"))
        except (ValidationError, AttributeError, TypeError) as exc:
            raise ValueError(f"invalid degradation request: {exc}") from exc
        master = self.root.resolve(request.master_relative)
        has_drawtext = any(stage.filter == "drawtext" for stage in request.profile.identity.stages)
        font = self.root.resolve(request.font_relative) if request.font_relative is not None else None
        if not master.is_file():
            raise JobError(JobErrorKind.MISSING_MODEL_ARTIFACT, f"master input is missing: {request.master_relative}",
                           shot_id=request.shot_id, run_id=request.run_id)
        if has_drawtext and (font is None or not font.is_file()):
            raise JobError(
                           JobErrorKind.MISSING_MODEL_ARTIFACT,
                           f"overlay font is missing: {request.font_relative}; run "
                           "install_default_overlay_font or set the PostProfile to a project-local font",
                           shot_id=request.shot_id, run_id=request.run_id)
        analog_active = request.camera_profile.analog_artifacts.active
        pipe_sha256 = self._verify_ntsc_pipe(request) if analog_active else None
        identity = normalize_ffmpeg_identity(
            self._capture([str(self.ffmpeg_exe), "-hide_banner", "-version"], request,
                          cancel=cancel, deadline=deadline)
        )
        if not identity.startswith("ffmpeg version "):
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, "FFmpeg did not return a version banner",
                           shot_id=request.shot_id, run_id=request.run_id)
        pinned_identity = request.profile.identity.pinned_binaries.get("ffmpeg")
        if pinned_identity is None or normalize_ffmpeg_identity(pinned_identity) != identity:
            raise JobError(JobErrorKind.INVALID_MANIFEST,
                           "PostProfile FFmpeg pin does not exactly match installed `ffmpeg -version` output",
                           shot_id=request.shot_id, run_id=request.run_id)
        self._check_capabilities(request, cancel=cancel, deadline=deadline)
        probe = self._probe(master, request, cancel=cancel, deadline=deadline)
        try:
            validated = validate_profile(request.profile, request.camera_profile,
                                         master_frames=probe["frame_count"],
                                         master_rate=Fraction(probe["avg_frame_rate"]))
            if analog_active:
                validate_ntsc_geometry(
                    *validated.profile.storage_resolution,
                    validated.profile.analog_artifacts,
                )
                validate_ntsc_frame_indices(
                    0, validated.predicted_frames, validated.profile.analog_artifacts,
                )
                producer_graph, consumer_graph = build_filter_graph_parts(
                    validated,
                    shot_id=request.shot_id,
                    degrade_seed=request.degrade_seed,
                    font_path=request.font_relative,
                )
                graph = producer_graph
                ntsc_settings = ntsc_settings_from_analog_artifacts(
                    validated.profile.analog_artifacts
                )
                settings_text = json.dumps(
                    ntsc_settings, ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                )
                ntsc_seed = derive_seed(
                    request.shot_id, request.degrade_seed,
                    "camera_analog_artifacts", INT32_MAX,
                )
            else:
                graph = build_filter_graph(validated, shot_id=request.shot_id,
                                           degrade_seed=request.degrade_seed,
                                           font_path=request.font_relative)
                producer_graph, consumer_graph = graph, ""
                ntsc_settings = None
                ntsc_seed = None
                settings_text = None
        except (ProfileValidationError, ValidationError, ValueError) as exc:
            raise JobError(JobErrorKind.INVALID_MANIFEST, str(exc), shot_id=request.shot_id,
                           run_id=request.run_id, cause=exc) from exc
        if analog_active and request.profile.encode.threads != 1:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                "the approved analog_artifacts pipeline requires an explicit single-threaded encoder; set encode.threads=1",
                shot_id=request.shot_id, run_id=request.run_id,
            )

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
        partial_output_rel = f"{run_relative}/degraded.partial.mp4"
        log_rel = f"{run_relative}/producer.log" if analog_active else f"{run_relative}/ffmpeg.log"
        metadata_rel = f"{run_relative}/metadata.json"
        settings_rel = f"{run_relative}/ntsc-settings.json"
        producer_log_rel = f"{run_relative}/producer.log"
        processor_log_rel = f"{run_relative}/ntsc-pipe.log"
        encoder_log_rel = f"{run_relative}/encoder.log"
        run_log = self.root.resolve(log_rel)
        derived_spec = request.profile.model_dump(mode="json")
        derived_spec_canonical_json = json.dumps(
            derived_spec, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        derived_spec_sha256 = hashlib.sha256(derived_spec_canonical_json.encode("utf-8")).hexdigest()
        if analog_active:
            graph_hash_source = json.dumps(
                {"producer": producer_graph, "consumer": consumer_graph},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
        else:
            graph_hash_source = graph
        filter_graph_hash = hashlib.sha256(graph_hash_source.encode("utf-8")).hexdigest()
        pinned_binaries = {"ffmpeg": identity}
        if pipe_sha256 is not None:
            pinned_binaries["ntsc-pipe"] = pipe_sha256
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "status": "running",
            "shot_id": request.shot_id,
            "shot_version": request.shot_version,
            "run_id": request.run_id,
            "profile_name": request.profile.identity.name,
            "profile_semver": request.profile.identity.semver,
            "supersedes": request.profile.supersedes,
            "derived_spec": derived_spec,
            "derived_spec_canonical_json": derived_spec_canonical_json,
            "postprofile_spec_sha256": derived_spec_sha256,
            "degrade_seed": request.degrade_seed,
            "master_path": request.master_relative,
            "output_path": output_rel,
            "ffmpeg_identity": identity,
            "pinned_binaries": pinned_binaries,
            "filter_graph": graph,
            "filter_graph_hash": filter_graph_hash,
            "artifact_schedule": [event.model_dump(mode="json") for event in validated.schedule],
            "predicted_frames": validated.predicted_frames,
            "source_probe": probe,
        }
        if analog_active:
            assert settings_text is not None
            stage_order = [
                stage.filter for stage in request.profile.identity.stages
                if stage.composite_stage != "on_playback"
            ]
            if not any(stage.filter == "setsar" for stage in request.profile.identity.stages):
                stage_order.append("setsar")
            metadata.update({
                "filter_graph_scope": "producer and consumer FFmpeg graphs",
                "producer_filter_graph": producer_graph,
                "consumer_filter_graph": consumer_graph,
                "stage_order": stage_order,
                "pipeline_logs": {
                    "producer": producer_log_rel,
                    "ntsc_pipe": processor_log_rel,
                    "encoder": encoder_log_rel,
                },
                "ntsc_pipe": {
                    "source_commit": _NTSC_RS_COMMIT,
                    "source_version": _NTSC_RS_RELEASE_VERSION,
                    "crate_version": _NTSCRS_CRATE_VERSION,
                    "seed": ntsc_seed,
                    "sha256": pipe_sha256,
                    "settings_path": settings_rel,
                    "settings_sha256": hashlib.sha256(settings_text.encode("utf-8")).hexdigest(),
                    "settings": ntsc_settings,
                },
            })
        self._write_json(metadata_rel, metadata, request)
        artifact_rel = output_rel
        if analog_active:
            artifact_rel = partial_output_rel
            try:
                assert settings_text is not None and ntsc_seed is not None
                self._write_text(settings_rel, settings_text, request)
                settings_path = self.root.resolve(settings_rel)
                partial_path = self.root.resolve(partial_output_rel)
                width, height = request.profile.storage_resolution
                producer_command = build_analog_producer_command(
                    self.ffmpeg_exe, master=master, filter_graph=producer_graph,
                    frames=validated.predicted_frames,
                )
                processor_command = [
                    str(self.ntsc_pipe_exe), "--width", str(width), "--height", str(height),
                    "--frames", str(validated.predicted_frames), "--seed", str(ntsc_seed),
                    "--settings", str(settings_path), "--frame-index", "0",
                ]
                consumer_command = build_analog_encoder_command(
                    self.ffmpeg_exe, output=partial_path, width=width, height=height,
                    frame_rate=request.profile.output_fps, filter_graph=consumer_graph,
                    codec=request.profile.encode.codec, preset=request.profile.encode.preset,
                    crf=request.profile.encode.crf, pixel_format=request.profile.encode.pix_fmt,
                    threads=request.profile.encode.threads,
                    bitrate_kbps=request.profile.encode.bitrate_kbps,
                    block_artifacts=request.profile.encode.block_artifacts,
                )
                metadata["pipeline_plan"] = {
                    "producer_ffmpeg": {
                        "argv": producer_command,
                        "filter_graph": producer_graph,
                    },
                    "ntsc_pipe": {"argv": processor_command},
                    "consumer_ffmpeg": {
                        "argv": consumer_command,
                        "filter_graph": consumer_graph,
                    },
                }
                self._write_json(metadata_rel, metadata, request)
                self.root.write_with(
                    partial_output_rel,
                    lambda _output_path: self._run_analog_pipeline(
                        producer_command, processor_command, consumer_command,
                        log_relatives={
                            "producer": producer_log_rel,
                            "ntsc-pipe": processor_log_rel,
                            "encoder": encoder_log_rel,
                        },
                        request=request, cancel=cancel, deadline=deadline,
                    ),
                )
            except JobError as exc:
                self._remove_analog_outputs(partial_output_rel, output_rel, request)
                metadata.update(
                    status="cancelled" if exc.kind is JobErrorKind.CANCELLED else "failed",
                    error=exc.message,
                )
                self._write_json(metadata_rel, metadata, request)
                raise
            except OSError as exc:
                self._remove_analog_outputs(partial_output_rel, output_rel, request)
                metadata.update(status="failed", error=str(exc))
                self._write_json(metadata_rel, metadata, request)
                self._raise_os_error(exc, request)
        else:
            try:
                def authorize_output(output_path: Path) -> None:
                    command = build_command(self.ffmpeg_exe, master=master, output=output_path,
                                            filter_graph=graph, codec=request.profile.encode.codec,
                                            preset=request.profile.encode.preset, crf=request.profile.encode.crf,
                                            pixel_format=request.profile.encode.pix_fmt,
                                            threads=request.profile.encode.threads,
                                            bitrate_kbps=request.profile.encode.bitrate_kbps,
                                            block_artifacts=request.profile.encode.block_artifacts)

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
            output = self.root.resolve(artifact_rel)
            output_is_valid = output.is_file() and output.stat().st_size > 0
        except OSError as exc:
            if analog_active:
                self._remove_analog_outputs(partial_output_rel, output_rel, request)
            self._raise_os_error(exc, request)
        if not output_is_valid:
            metadata.update(status="failed", error="FFmpeg output is missing or empty")
            try:
                self._write_json(metadata_rel, metadata, request)
            finally:
                if analog_active:
                    self._remove_analog_outputs(partial_output_rel, output_rel, request)
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, metadata["error"],
                           shot_id=request.shot_id, run_id=request.run_id)
        try:
            output_probe = self._probe(output, request, cancel=cancel, deadline=deadline)
        except JobError as exc:
            metadata.update(
                status="cancelled" if exc.kind is JobErrorKind.CANCELLED else "failed",
                error=exc.message,
            )
            try:
                self._write_json(metadata_rel, metadata, request)
            finally:
                if analog_active:
                    self._remove_analog_outputs(partial_output_rel, output_rel, request)
            raise
        if output_probe["frame_count"] != validated.predicted_frames:
            metadata.update(status="failed", error="output frame count does not match predicted schedule",
                            output_probe=output_probe)
            try:
                self._write_json(metadata_rel, metadata, request)
            finally:
                if analog_active:
                    self._remove_analog_outputs(partial_output_rel, output_rel, request)
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, metadata["error"],
                           shot_id=request.shot_id, run_id=request.run_id)
        try:
            digest = self._sha256(output)
        except OSError as exc:
            if analog_active:
                self._remove_analog_outputs(partial_output_rel, output_rel, request)
            self._raise_os_error(exc, request)
        if analog_active:
            try:
                final_path = self.root.resolve(output_rel)
                if final_path.exists():
                    raise FileExistsError(f"PostProfile output already exists: {output_rel}")
                control = self._control_error(request, cancel, deadline)
                if control is not None:
                    raise control
                self.root.write_with(
                    output_rel, lambda target: os.replace(self.root.resolve(partial_output_rel), target)
                )
                output = final_path
                artifact_rel = output_rel
            except JobError as exc:
                metadata.update(
                    status="cancelled" if exc.kind is JobErrorKind.CANCELLED else "failed",
                    error=exc.message,
                )
                try:
                    self._write_json(metadata_rel, metadata, request)
                finally:
                    self._remove_analog_outputs(partial_output_rel, output_rel, request)
                raise
            except OSError as exc:
                self._remove_analog_outputs(partial_output_rel, output_rel, request)
                self._raise_os_error(exc, request)
        metadata.update(status="completed", output_probe=output_probe, output_sha256=digest)
        try:
            self._write_json(metadata_rel, metadata, request)
        except JobError:
            if analog_active:
                self._remove_analog_outputs(partial_output_rel, output_rel, request)
            raise
        return DegradeResult(
            output_path=output_rel,
            log_path=log_rel,
            metadata_path=metadata_rel,
            artifact_schedule=validated.schedule,
            filter_graph=graph,
            filter_graph_hash=metadata["filter_graph_hash"],
            postprofile_spec_sha256=derived_spec_sha256,
            output_sha256=digest,
            frame_count=output_probe["frame_count"],
            output_fps=request.profile.output_fps,
            pts_delta_distribution=output_probe["pts_delta_distribution"],
            ffmpeg_identity=identity,
            profile_semver=request.profile.identity.semver,
            pinned_binaries=pinned_binaries,
        )

    def _verify_ntsc_pipe(self, request: DegradeRequest) -> str:
        expected = request.profile.identity.pinned_binaries.get("ntsc-pipe")
        if expected is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                "active analog_artifacts requires a sha256:<64 lowercase hex> pin in "
                "CameraProfile.analog_artifacts.ntsc_pipe_sha256 or PostProfile.pinned_binaries['ntsc-pipe']. "
                "Get the digest from tools/build_ntsc_pipe.ps1.",
                shot_id=request.shot_id, run_id=request.run_id,
            )
        executable = Path(self.ntsc_pipe_exe)
        if not executable.is_file():
            raise JobError(
                JobErrorKind.WORKER_UNREACHABLE,
                f"ntsc-pipe binary is missing at {executable}; build it with tools/build_ntsc_pipe.ps1 "
                "(cargo build --release --offline)",
                shot_id=request.shot_id, run_id=request.run_id,
            )
        try:
            with _open_executable_read_locked(executable) as stream:
                observed = "sha256:" + self._sha256_stream(stream)
        except OSError as exc:
            raise JobError(
                JobErrorKind.WORKER_UNREACHABLE,
                f"could not read ntsc-pipe binary {executable}; rebuild with tools/build_ntsc_pipe.ps1: {exc}",
                shot_id=request.shot_id, run_id=request.run_id, cause=exc,
            ) from exc
        if observed != expected:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"ntsc-pipe SHA-256 pin mismatch: PostProfile has {expected}, installed binary is {observed}; "
                "restore the pinned binary or build and verify the intended binary with tools/build_ntsc_pipe.ps1",
                shot_id=request.shot_id, run_id=request.run_id,
            )
        return observed

    @contextmanager
    def _verified_ntsc_pipe_for_launch(self, request: DegradeRequest) -> Iterator[None]:
        """Recheck the pin while holding a no-write/delete share through CreateProcess."""
        expected = request.profile.identity.pinned_binaries.get("ntsc-pipe")
        if expected is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                "active analog_artifacts requires a valid ntsc-pipe SHA-256 pin; "
                "tools/build_ntsc_pipe.ps1 writes the build's digest sidecar",
                shot_id=request.shot_id, run_id=request.run_id,
            )
        with _open_executable_read_locked(self.ntsc_pipe_exe) as stream:
            observed = "sha256:" + self._sha256_stream(stream)
            if observed != expected:
                raise JobError(
                    JobErrorKind.INVALID_MANIFEST,
                    f"ntsc-pipe SHA-256 pin mismatch at launch: PostProfile has {expected}, "
                    f"installed binary is {observed}; restore the pinned binary or build and verify "
                    "the intended binary with tools/build_ntsc_pipe.ps1",
                    shot_id=request.shot_id, run_id=request.run_id,
                )
            yield

    def _launch_pipeline_child(
        self, stage: str, command: list[str], streams: dict[str, Any], request: DegradeRequest,
    ) -> Any:
        if stage == "ntsc-pipe":
            with self._verified_ntsc_pipe_for_launch(request):
                return self.popen(command, shell=False, cwd=self.root.path, **streams)
        return self.popen(command, shell=False, cwd=self.root.path, **streams)

    def _run_analog_pipeline(
        self,
        producer_command: list[str],
        processor_command: list[str],
        encoder_command: list[str],
        *,
        log_relatives: dict[str, str],
        request: DegradeRequest,
        cancel: Callable[[], bool] | None,
        deadline: float | None,
    ) -> None:
        """Run the RGB pipe with owned children, drained stage logs, and bounded teardown."""
        processes: list[tuple[str, Any]] = []
        threads: list[threading.Thread] = []
        stream_errors: Queue[tuple[str, BaseException]] = Queue()
        stopping = threading.Event()
        log_streams: dict[str, Any] = {}
        handles: list[Any] = []
        failure: BaseException | None = None

        try:
            for stage, relative in log_relatives.items():
                def open_log(path: Path, *, key: str = stage) -> None:
                    stream = path.open("wb")
                    log_streams[key] = stream
                    handles.append(stream)

                self.root.write_with(relative, open_log)

            # Start consumers first so neither upstream process can fill an unobserved pipe.
            launch_order = (
                ("encoder", encoder_command, {
                    "stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE,
                }),
                ("ntsc-pipe", processor_command, {
                    "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                }),
                ("producer", producer_command, {
                    "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                }),
            )
            for stage, command, streams in launch_order:
                control = self._control_error(request, cancel, deadline)
                if control is not None:
                    raise control
                try:
                    process = self._launch_pipeline_child(stage, command, streams, request)
                except OSError as exc:
                    raise _PipelineLaunchFailure(stage, command[0], exc) from exc
                processes.append((stage, process))

            by_stage = dict(processes)

            def drain_stderr(stage: str, source: Any, target: Any) -> None:
                try:
                    while block := source.read(64 * 1024):
                        target.write(block)
                    target.flush()
                except BaseException as exc:  # noqa: BLE001 - cross-thread pipe errors are reported below.
                    if not stopping.is_set():
                        stream_errors.put((stage, exc))

            def pump(source: Any, target: Any, stage: str) -> None:
                try:
                    while block := source.read(1024 * 1024):
                        try:
                            written = target.write(block)
                            if written != len(block):
                                raise OSError(f"short raw-frame pipe write: {written} of {len(block)} bytes")
                        except BrokenPipeError:
                            # The owning child failure is reported by its return code and stage log.
                            return
                except BaseException as exc:  # noqa: BLE001 - cross-thread pipe errors are reported below.
                    if not stopping.is_set():
                        stream_errors.put((stage, exc))
                finally:
                    for stream in (source, target):
                        try:
                            stream.close()
                        except OSError:
                            pass

            for stage, process in processes:
                thread = threading.Thread(
                    target=drain_stderr,
                    args=(stage, process.stderr, log_streams[stage]),
                    name=f"postprofile-{stage}-stderr", daemon=True,
                )
                thread.start()
                threads.append(thread)

            pipe_pairs = (
                (by_stage["producer"].stdout, by_stage["ntsc-pipe"].stdin,
                 "producer-to-ntsc-pipe"),
                (by_stage["ntsc-pipe"].stdout, by_stage["encoder"].stdin,
                 "ntsc-pipe-to-encoder"),
            )
            for source, target, stage in pipe_pairs:
                thread = threading.Thread(
                    target=pump, args=(source, target, stage),
                    name=f"postprofile-{stage}", daemon=True,
                )
                thread.start()
                threads.append(thread)

            while True:
                control = self._control_error(request, cancel, deadline)
                if control is not None:
                    raise control
                try:
                    stage, cause = stream_errors.get_nowait()
                except Empty:
                    pass
                else:
                    raise _PipelineIOFailure(stage, cause) from cause
                statuses = [(stage, process.poll()) for stage, process in processes]
                failed = next(((stage, code) for stage, code in statuses if code not in (None, 0)), None)
                if failed is not None:
                    raise _PipelineChildFailure(failed[0], int(failed[1]))
                if all(code is not None for _stage, code in statuses) and all(
                    not thread.is_alive() for thread in threads
                ):
                    break
                time.sleep(0.025)
        except BaseException as exc:
            failure = exc
        finally:
            if failure is not None:
                stopping.set()
                try:
                    self._stop_processes(processes, request)
                except BaseException as stop_error:  # noqa: BLE001 - still close pipes and join drainers.
                    failure = stop_error
                for _stage, process in processes:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
            for thread in threads:
                thread.join(timeout=3.0)
            alive_threads = [thread.name for thread in threads if thread.is_alive()]
            for handle in handles:
                try:
                    handle.close()
                except OSError:
                    pass
            if alive_threads and failure is None:
                failure = RuntimeError(f"PostProfile pipeline threads did not drain: {alive_threads}")

        if failure is None:
            return
        if isinstance(failure, _PipelineChildFailure):
            tails: dict[str, str] = {}
            child_statuses: list[str] = []
            for stage, relative in log_relatives.items():
                try:
                    tails[stage] = self._tail_log(self.root.resolve(relative))
                except OSError:
                    tails[stage] = ""
            for stage, process in processes:
                if process.returncode is not None:
                    child_statuses.append(f"{stage}={process.returncode}")
            diagnostic = tails.get(failure.stage, "")
            all_logs = "\n".join(tails.values()).lower()
            disk_full = any(value in all_logs for value in (
                "no space left on device", "enospc", "error -28", "error 112",
            ))
            if disk_full:
                kind = JobErrorKind.DISK_EXHAUSTED
            elif (failure.stage == "ntsc-pipe"
                  and failure.code == _NTSC_PIPE_PROTOCOL_ERROR_EXIT_CODE):
                kind = JobErrorKind.CORRUPT_ARTIFACT
            else:
                kind = JobErrorKind.INTERNAL
            raise JobError(
                kind,
                f"{failure.stage} exited with status {failure.code} "
                f"(child statuses: {', '.join(child_statuses)}): {diagnostic[-3000:]}",
                shot_id=request.shot_id, run_id=request.run_id, cause=failure,
            ) from failure
        if isinstance(failure, _PipelineLaunchFailure):
            install_hint = (
                "; build it with tools/build_ntsc_pipe.ps1 (cargo build --release --offline)"
                if failure.stage == "ntsc-pipe" else ""
            )
            raise JobError(
                JobErrorKind.WORKER_UNREACHABLE,
                f"could not launch PostProfile {failure.stage} process {failure.executable}: "
                f"{failure.cause}{install_hint}",
                shot_id=request.shot_id, run_id=request.run_id, cause=failure.cause,
            ) from failure
        if isinstance(failure, _PipelineIOFailure):
            kind = (
                JobErrorKind.DISK_EXHAUSTED
                if isinstance(failure.cause, OSError) and failure.cause.errno in {errno.ENOSPC, 112}
                else JobErrorKind.CORRUPT_ARTIFACT
            )
            raise JobError(
                kind,
                f"PostProfile raw-frame pipe {failure.stage} failed: {failure.cause}",
                shot_id=request.shot_id, run_id=request.run_id, cause=failure.cause,
            ) from failure
        if isinstance(failure, JobError):
            raise failure
        if isinstance(failure, OSError):
            self._raise_os_error(failure, request)
        if isinstance(failure, RuntimeError):
            raise JobError(
                JobErrorKind.INTERNAL, str(failure),
                shot_id=request.shot_id, run_id=request.run_id, cause=failure,
            ) from failure
        raise failure

    def _stop_processes(self, processes: list[tuple[str, Any]], request: DegradeRequest) -> None:
        for _stage, process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        survivors: list[tuple[str, Any]] = []
        for stage, process in processes:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                survivors.append((stage, process))
            except (ChildProcessError, ProcessLookupError):
                continue
        for _stage, process in survivors:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        unconfirmed: list[str] = []
        for stage, process in survivors:
            try:
                process.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, ChildProcessError, ProcessLookupError):
                unconfirmed.append(stage)
        unconfirmed.extend(stage for stage, process in processes if process.poll() is None
                           and stage not in unconfirmed)
        if unconfirmed:
            raise JobError(
                JobErrorKind.INTERNAL,
                f"could not stop PostProfile child process(es): {', '.join(unconfirmed)}",
                shot_id=request.shot_id, run_id=request.run_id,
            )

    def _remove_analog_outputs(
        self, partial_relative: str, output_relative: str, request: DegradeRequest,
    ) -> None:
        for relative in (partial_relative, output_relative):
            try:
                self.root.write_with(relative, lambda path: path.unlink(missing_ok=True))
            except OSError as exc:
                self._raise_os_error(exc, request)

    def _write_text(self, relative: str, text: str, request: DegradeRequest) -> None:
        try:
            self.root.write_with(
                relative,
                lambda path: path.write_text(text, encoding="utf-8", newline="\n"),
            )
        except OSError as exc:
            self._raise_os_error(exc, request)

    @staticmethod
    def _control_error(
        request: DegradeRequest,
        cancel: Callable[[], bool] | None,
        deadline: float | None,
    ) -> JobError | None:
        if cancel is not None and cancel():
            return JobError(
                JobErrorKind.CANCELLED, "PostProfile pipeline cancelled by its parent job",
                shot_id=request.shot_id, run_id=request.run_id,
            )
        if deadline is not None and time.monotonic() >= deadline:
            return JobError(
                JobErrorKind.BUDGET_EXCEEDED, "PostProfile per-shot wall-clock deadline expired",
                shot_id=request.shot_id, run_id=request.run_id,
            )
        return None

    def _capture(
        self,
        argv: list[str],
        request: DegradeRequest,
        *,
        nonzero_kind: JobErrorKind = JobErrorKind.WORKER_UNREACHABLE,
        cancel: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> str:
        control = self._control_error(request, cancel, deadline)
        if control is not None:
            raise control
        proc = None
        try:
            proc = self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, shell=False)
            while True:
                control = self._control_error(request, cancel, deadline)
                if control is not None:
                    self._stop_capture_process(proc, request)
                    raise control
                try:
                    stdout, stderr = proc.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except FileNotFoundError as exc:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE, f"could not launch {argv[0]}: {exc}",
                           shot_id=request.shot_id, run_id=request.run_id, cause=exc) from exc
        except JobError:
            raise
        except OSError as exc:
            if proc is not None and proc.poll() is None:
                self._stop_capture_process(proc, request)
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

    def _check_capabilities(
        self,
        request: DegradeRequest,
        *,
        cancel: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> None:
        capture = lambda argv, **kwargs: self._capture(  # noqa: E731 - binds job controls for preflight probes.
            argv, request, cancel=cancel, deadline=deadline, **kwargs
        )
        filters = capture([str(self.ffmpeg_exe), "-hide_banner", "-filters"])
        required = {
            stage.filter for stage in request.profile.identity.stages
            if stage.filter != "camera_analog_artifacts"
        }
        analog_active = request.profile.analog_artifacts.active
        if analog_active:
            required.add("setsar")
            if required & {"eq", "gblur", "noise", "unsharp"} and (
                request.profile.encode.pix_fmt.startswith("yuv")
                or required & {"colortemperature", "colorchannelmixer"}
            ):
                required.add("format")
        if "setpts" in required:
            required.add("settb")
        for name in sorted(required):
            if not self._listed_entry(filters, name):
                raise JobError(JobErrorKind.INVALID_MANIFEST,
                               f"installed FFmpeg lacks required filter {name!r}",
                               shot_id=request.shot_id, run_id=request.run_id)
        encoders = capture([str(self.ffmpeg_exe), "-hide_banner", "-encoders"])
        if not self._listed_entry(encoders, request.profile.encode.codec):
            raise JobError(JobErrorKind.INVALID_MANIFEST,
                           f"installed FFmpeg lacks required encoder {request.profile.encode.codec!r}",
                           shot_id=request.shot_id, run_id=request.run_id)
        global_help = capture([str(self.ffmpeg_exe), "-hide_banner", "-h", "full"])
        if not re.search(r"^\s*-threads\s", global_help, flags=re.MULTILINE):
            raise JobError(JobErrorKind.INVALID_MANIFEST, "installed FFmpeg lacks required global -threads option",
                           shot_id=request.shot_id, run_id=request.run_id)
        if "noise" in required:
            help_text = capture([str(self.ffmpeg_exe), "-hide_banner", "-h", "filter=noise"])
            match = re.search(r"^\s*all_seed\s+<int>[^\n]*\(from\s+(-?\d+)\s+to\s+(INT_MAX|\d+)\)",
                              help_text, flags=re.MULTILINE)
            upper = 2**31 - 1 if match and match.group(2) == "INT_MAX" else int(match.group(2)) if match else None
            if not match or upper != 2**31 - 1:
                raise JobError(JobErrorKind.INVALID_MANIFEST,
                               "installed FFmpeg noise all_seed range differs from the measured INT32_MAX contract",
                               shot_id=request.shot_id, run_id=request.run_id)
        option_requirements = {
            "colortemperature": {"temperature"},
            "colorchannelmixer": {"rr", "gg", "bb"},
            "gblur": {"sigma", "sigmaV", "planes"},
            "unsharp": {"luma_msize_x", "luma_msize_y", "luma_amount"},
        }
        for stage in request.profile.identity.stages:
            if stage.filter == "noise":
                names = {key for key in stage.params if key in {
                    "c0s", "c1s", "c2s", "c0f", "c1f", "c2f",
                }}
                if names:
                    names.add("all_seed")
                    option_requirements.setdefault("noise", set()).update(names)
        for filter_name, options in option_requirements.items():
            if filter_name not in required:
                continue
            help_text = capture([str(self.ffmpeg_exe), "-hide_banner", "-h", f"filter={filter_name}"])
            missing = sorted(option for option in options
                             if not re.search(rf"^\s*{re.escape(option)}\s+", help_text, flags=re.MULTILINE))
            if missing:
                raise JobError(
                    JobErrorKind.INVALID_MANIFEST,
                    f"installed FFmpeg {filter_name} filter lacks required option(s): {', '.join(missing)}",
                    shot_id=request.shot_id, run_id=request.run_id,
                )
        if "loop" in required:
            help_text = capture([str(self.ffmpeg_exe), "-hide_banner", "-h", "filter=loop"])
            if not re.search(r"\bstart\b", help_text):
                raise JobError(JobErrorKind.INVALID_MANIFEST, "installed FFmpeg loop filter has no start option",
                               shot_id=request.shot_id, run_id=request.run_id)
        encoder_help = capture([str(self.ffmpeg_exe), "-hide_banner", "-h",
                                f"encoder={request.profile.encode.codec}"])
        required_encoder_options = ["preset"]
        if request.profile.encode.bitrate_kbps is None:
            required_encoder_options.append("crf")
        block_artifacts = request.profile.encode.block_artifacts > 0
        if block_artifacts or (analog_active and request.profile.encode.codec in _ANALOG_X264_CODECS):
            required_encoder_options.append("x264-params")
        if block_artifacts:
            required_encoder_options.append("deblock")
        for option in required_encoder_options:
            if not re.search(rf"^\s*-{option}\s", encoder_help, flags=re.MULTILINE):
                raise JobError(JobErrorKind.INVALID_MANIFEST,
                               f"installed encoder lacks required -{option} option",
                               shot_id=request.shot_id, run_id=request.run_id)
        if request.profile.encode.bitrate_kbps is not None:
            required_rate_options = {"b", "maxrate", "bufsize"}
            missing = sorted(option for option in required_rate_options
                             if not re.search(rf"^\s*-{option}\s", global_help, flags=re.MULTILINE))
            if missing:
                raise JobError(
                    JobErrorKind.INVALID_MANIFEST,
                    f"installed FFmpeg lacks required rate-control option(s): {', '.join(missing)}",
                    shot_id=request.shot_id, run_id=request.run_id,
                )

    @staticmethod
    def _listed_entry(output: str, name: str) -> bool:
        """FFmpeg flags columns vary between filters and builds; parse whitespace columns."""
        return any(len(columns := line.split()) >= 2 and columns[1] == name
                   for line in output.splitlines())

    def _stop_capture_process(self, process: Any, request: DegradeRequest) -> None:
        self._stop_processes([("preflight", process)], request)
        try:
            process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
            process.communicate()

    def _probe(
        self,
        path: Path,
        request: DegradeRequest,
        *,
        cancel: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        raw = self._capture([str(self.ffprobe_exe), "-v", "error", "-select_streams", "v:0",
                             "-count_frames", "-show_entries",
                             "stream=nb_read_frames,avg_frame_rate,time_base,duration,width,height,start_time:frame=pts_time:format=duration",
                             "-show_frames",
                             "-of", "json", str(path)], request,
                            nonzero_kind=JobErrorKind.CORRUPT_ARTIFACT,
                            cancel=cancel, deadline=deadline)
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
            self._write_text(
                relative,
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
                request,
            )
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
    def _sha256_stream(source: Any) -> str:
        digest = hashlib.sha256()
        source.seek(0)
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        source.seek(0)
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


class _PipelineChildFailure(Exception):
    def __init__(self, stage: str, code: int) -> None:
        self.stage, self.code = stage, code
        super().__init__(f"{stage} exited with status {code}")


class _PipelineLaunchFailure(Exception):
    def __init__(self, stage: str, executable: str, cause: OSError) -> None:
        self.stage, self.executable, self.cause = stage, executable, cause
        super().__init__(f"could not launch {stage}: {cause}")


class _PipelineIOFailure(Exception):
    def __init__(self, stage: str, cause: BaseException) -> None:
        self.stage, self.cause = stage, cause
        super().__init__(f"{stage}: {cause}")
