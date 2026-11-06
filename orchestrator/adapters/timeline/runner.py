"""Display-aspect-aware, supervised FFmpeg episode assembly."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from orchestrator.adapters.postprofile.profile import PostProfileSpec
from orchestrator.domain.entities import ShotOutput, ShotOutputKind, Timeline
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings


class ProcessSupervisor(Protocol):
    """The child-process surface required from the owning job supervisor."""

    def launch(
        self, command: list[str], *, popen: Callable[..., subprocess.Popen[bytes]] | None = None,
        **kwargs: object,
    ) -> subprocess.Popen[bytes]: ...

    def clear_process(self, process: subprocess.Popen[bytes]) -> None: ...

    def stopped(self) -> bool: ...

    def stop_for(self, reason: JobErrorKind) -> bool: ...


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    sample_aspect_ratio: Fraction
    frame_rate: Fraction
    duration: Fraction

    @property
    def display_aspect_ratio(self) -> Fraction:
        return Fraction(self.width, self.height) * self.sample_aspect_ratio


@dataclass(frozen=True)
class TimelineAssemblyResult:
    relative_path: str
    log_path: str
    content_sha256: str
    ffmpeg_identity: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class _Segment:
    kind: str
    duration: Fraction
    shot_id: str | None = None
    media: MediaInfo | None = None
    card_text: str | None = None


def build_assembly_command(
    ffmpeg_exe: Path,
    *,
    timeline: Timeline,
    shot_outputs: Mapping[str, ShotOutput],
    media_info: Mapping[str, MediaInfo],
    source_paths: Mapping[str, Path],
    profile: PostProfileSpec,
    output: Path,
    font_path: Path | None = None,
) -> list[str]:
    """Build a deterministic CFR concat-filter command for one persisted Timeline."""
    width, height = profile.storage_resolution
    fps = profile.output_fps
    target_sar = _parse_ratio(profile.sample_aspect_ratio, "delivery sample aspect ratio")
    if width <= 0 or height <= 0 or fps <= 0:
        raise _invalid("delivery resolution and frame rate must be positive")
    if timeline.audio_tracks:
        raise _invalid("Timeline.audio_tracks is out of scope; episode audio is planned for Phase 2")
    if not timeline.entries:
        raise _invalid(f"Timeline for episode {timeline.episode_id!r} has no entries")

    segments: list[_Segment] = []
    cursor = Fraction(0)
    for entry in timeline.entries:
        start = Fraction(str(entry.start_sec))
        duration = Fraction(str(entry.duration_sec))
        if start < cursor:
            raise _invalid(
                f"Timeline entry at {entry.start_sec}s overlaps or is out of order; overlaps are unsupported"
            )
        if start > cursor:
            segments.append(_Segment("pause", start - cursor))
        if entry.type == "shot":
            if not entry.ref:
                raise _invalid("Timeline shot entry is missing its shot reference")
            output_receipt = shot_outputs.get(entry.ref)
            media = media_info.get(entry.ref)
            if output_receipt is None:
                raise _invalid(f"Shot {entry.ref!r} has no delivered artifact; run degrade_shot first")
            if output_receipt.kind is not ShotOutputKind.DEGRADED:
                raise _invalid(f"Shot {entry.ref!r} receipt is not a delivered degraded output")
            if media is None:
                raise _invalid(f"Shot {entry.ref!r} has no probed video metadata")
            if duration - media.duration > Fraction(1, 1) / media.frame_rate:
                raise _invalid(
                    f"Timeline duration {entry.duration_sec}s exceeds delivered media duration "
                    f"{float(media.duration):.6f}s for Shot {entry.ref!r}"
                )
            segments.append(_Segment(
                "shot", duration, shot_id=entry.ref, media=media,
            ))
        elif entry.type == "pause":
            if entry.ref is not None:
                raise _invalid("Timeline pause entry must not carry a reference")
            segments.append(_Segment("pause", duration))
        elif entry.type == "card":
            if not entry.ref:
                raise _invalid("Timeline card entry must store its text in ref")
            if font_path is None:
                raise _invalid("Timeline card needs a contained font file from the delivery PostProfile")
            if any(ord(char) < 32 or ord(char) == 127 for char in entry.ref):
                raise _invalid("Timeline card text must be a single printable line")
            segments.append(_Segment("card", duration, card_text=entry.ref))
        else:
            raise _invalid(f"Timeline entry type {entry.type!r} is unsupported; use shot, card, or pause")
        cursor = start + duration

    arguments = [
        str(ffmpeg_exe), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-fflags", "+bitexact",
    ]
    for segment in segments:
        if segment.kind == "shot":
            source = source_paths.get(segment.shot_id or "")
            if source is None:
                raise _invalid(f"Shot {segment.shot_id!r} has no contained source path")
            arguments.extend(("-i", str(source)))
        else:
            color = f"color=c=black:s={width}x{height}:r={fps}:d={_format_seconds(segment.duration)}"
            arguments.extend(("-f", "lavfi", "-i", color))

    filter_parts: list[str] = []
    labels: list[str] = []
    for index, segment in enumerate(segments):
        chain = [
            f"trim=duration={_format_seconds(segment.duration)}",
            "setpts=PTS-STARTPTS",
            f"fps=fps={fps}:round=near",
        ]
        if segment.kind == "shot":
            assert segment.media is not None and segment.shot_id is not None
            chain.extend(_conform_filters(segment.media, width, height, target_sar))
        else:
            chain.append(f"setsar={_ratio_text(target_sar)}")
            if segment.kind == "card":
                assert segment.card_text is not None and font_path is not None
                font = _escape_filter_value(str(font_path.resolve()))
                text = _escape_filter_value(segment.card_text)
                chain.append(
                    f"drawtext=fontfile='{font}':text='{text}':fontcolor=white:fontsize=32:"
                    "x=(w-text_w)/2:y=(h-text_h)/2"
                )
        chain.append(f"format={profile.encode.pix_fmt}")
        label = f"v{index}"
        filter_parts.append(f"[{index}:v:0]{','.join(chain)}[{label}]")
        labels.append(f"[{label}]")

    filter_parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=0[outv]")
    arguments.extend((
        "-filter_complex", ";".join(filter_parts), "-map", "[outv]", "-an", "-sn", "-dn",
        "-fps_mode", "cfr", "-r", str(fps), "-c:v", profile.encode.codec,
        "-preset", profile.encode.preset, "-crf", str(profile.encode.crf),
        "-pix_fmt", profile.encode.pix_fmt, "-threads", str(profile.encode.threads),
        "-movflags", "+faststart", "-map_metadata", "-1", "-map_chapters", "-1",
        "-flags", "+bitexact", str(output),
    ))
    return arguments


def _conform_filters(
    source: MediaInfo,
    width: int,
    height: int,
    target_sar: Fraction,
) -> list[str]:
    """Fit by display AR, preserving the full image and adding bars when needed."""
    source_dar = source.display_aspect_ratio
    target_dar = Fraction(width, height) * target_sar
    if source_dar == target_dar:
        content_width, content_height = width, height
    elif source_dar > target_dar:
        content_width = width
        content_height = _round_positive(Fraction(width) * target_sar / source_dar)
    else:
        content_height = height
        content_width = _round_positive(Fraction(height) * source_dar / target_sar)
    content_width = min(width, max(1, content_width))
    content_height = min(height, max(1, content_height))

    filters: list[str] = []
    if (source.width, source.height) != (content_width, content_height):
        filters.append(f"scale={content_width}:{content_height}:flags=bicubic")
    if (content_width, content_height) != (width, height):
        filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    if source.sample_aspect_ratio != target_sar:
        filters.append(f"setsar={_ratio_text(target_sar)}")
    # If only the storage dimensions differ, the scale is still required. If all
    # storage and aspect fields already match, this function adds no video filter.
    return filters


class TimelineAssembler:
    """Probe, conform, encode, and hash a cut while FFmpeg stays supervised."""

    def __init__(
        self,
        root: ProjectRoot,
        settings: Settings,
        control: ProcessSupervisor,
        *,
        ffprobe_exe: Path | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root
        self.settings = settings
        self.control = control
        self.ffmpeg_exe = settings.binaries.ffmpeg_exe
        self.ffprobe_exe = ffprobe_exe or Path("ffprobe")
        self.popen = popen
        self.clock = clock

    def run(
        self,
        *,
        timeline: Timeline,
        shot_outputs: Mapping[str, ShotOutput],
        profile: PostProfileSpec,
        output_relative: str,
        log_relative: str,
        font_relative: str | None = None,
        budget_seconds: float | None = None,
    ) -> TimelineAssemblyResult:
        validated = Timeline.model_validate(timeline.model_dump(mode="python"))
        profile = PostProfileSpec.model_validate(profile.model_dump(mode="python"))
        budget = (
            self.settings.budget.per_shot_wall_clock_seconds
            if budget_seconds is None else budget_seconds
        )
        if not math.isfinite(budget) or budget <= 0:
            raise _invalid("timeline assembly budget must be a finite positive number")
        deadline = self.clock() + budget
        output_path = self.root.resolve(output_relative)
        log_path = self.root.resolve(log_relative)
        if output_path.exists():
            raise _invalid(f"episode cut output already exists: {output_relative}")
        if log_path.exists():
            raise _invalid(f"episode FFmpeg log already exists: {log_relative}")
        font_path = self._font_path(font_relative, validated, profile)

        source_paths: dict[str, Path] = {}
        media: dict[str, MediaInfo] = {}
        for entry in validated.entries:
            if entry.type != "shot" or not entry.ref or entry.ref in media:
                continue
            receipt = shot_outputs.get(entry.ref)
            if receipt is None:
                raise _invalid(f"Shot {entry.ref!r} has no delivered artifact; run degrade_shot first")
            source_path = self.root.resolve(receipt.relative_path)
            source_paths[entry.ref] = source_path
            media[entry.ref] = self._probe(source_path, deadline, entry.ref)

        identity = self._ffmpeg_identity(deadline)
        pinned = profile.identity.pinned_binaries.get("ffmpeg")
        if pinned and normalize_ffmpeg_identity(pinned) != identity:
            raise _invalid(
                f"PostProfile {profile.identity.name!r} pins FFmpeg {pinned!r}, "
                f"but the supervised executable reports {identity.splitlines()[0]!r}"
            )
        command_box: list[list[str]] = []

        def authorize_output(path: Path) -> None:
            command = build_assembly_command(
                self.ffmpeg_exe, timeline=validated, shot_outputs=shot_outputs,
                media_info=media, source_paths=source_paths, profile=profile,
                output=path, font_path=font_path,
            )
            command_box.append(command)

            def write_log(log_target: Path) -> None:
                self._encode(command, log_target, deadline, output_relative, log_relative)

            self.root.write_with(log_relative, write_log)

        try:
            self.root.write_with(output_relative, authorize_output)
        except OSError as exc:
            kind = JobErrorKind.DISK_EXHAUSTED if getattr(exc, "errno", None) in {28, 112} else JobErrorKind.INTERNAL
            raise JobError(kind, f"episode FFmpeg assembly could not write its output: {exc}", cause=exc) from exc
        if not command_box:
            raise JobError(JobErrorKind.INTERNAL, "episode FFmpeg assembly command was not recorded")
        argv = command_box[0]

        final_path = self.root.resolve(output_relative)
        try:
            if not final_path.is_file() or final_path.stat().st_size <= 0:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                               "FFmpeg episode assembly produced a missing or empty file")
        except OSError as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"cannot inspect the assembled episode cut: {exc}", cause=exc) from exc
        final_info = self._probe(final_path, deadline, "episode cut")
        width, height = profile.storage_resolution
        target_sar = _parse_ratio(profile.sample_aspect_ratio, "delivery sample aspect ratio")
        target_dar = Fraction(width, height) * target_sar
        target_fps = Fraction(profile.output_fps, 1)
        if (final_info.width, final_info.height) != (width, height):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"episode cut resolution is {final_info.width}x{final_info.height}; expected {width}x{height}")
        if final_info.sample_aspect_ratio != target_sar:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"episode cut sample aspect ratio is {_ratio_text(final_info.sample_aspect_ratio)}; "
                           f"expected {_ratio_text(target_sar)}")
        if final_info.display_aspect_ratio != target_dar:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"episode cut display aspect ratio is "
                           f"{_ratio_text(final_info.display_aspect_ratio)}; expected {_ratio_text(target_dar)}")
        if final_info.frame_rate != target_fps:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"episode cut frame rate is {_ratio_text(final_info.frame_rate)}; expected {profile.output_fps}")
        expected_duration = Fraction(str(validated.total_duration_sec))
        previous_end = 0.0
        gap_count = 0
        for entry in validated.entries:
            if entry.start_sec > previous_end:
                gap_count += 1
            previous_end = entry.start_sec + entry.duration_sec
        duration_tolerance = Fraction(len(validated.entries) + gap_count + 1, 2) / target_fps
        if abs(final_info.duration - expected_duration) > duration_tolerance:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"episode cut duration is {float(final_info.duration):.6f}s; "
                           f"Timeline declares {float(expected_duration):.6f}s")
        try:
            digest = _sha256(final_path)
        except OSError as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"cannot hash assembled episode cut: {exc}", cause=exc) from exc
        return TimelineAssemblyResult(
            relative_path=output_relative,
            log_path=log_relative,
            content_sha256=digest,
            ffmpeg_identity=identity,
            argv=tuple(argv),
        )

    def _font_path(
        self, font_relative: str | None, timeline: Timeline, profile: PostProfileSpec,
    ) -> Path | None:
        has_card = any(entry.type == "card" for entry in timeline.entries)
        if not has_card:
            return None
        if not font_relative:
            raise _invalid(
                f"PostProfile {profile.identity.name!r} has no overlay.font_relative for timeline card text"
            )
        font = self.root.resolve(font_relative)
        if not font.is_file():
            raise _invalid(f"PostProfile {profile.identity.name!r} font is missing: {font_relative}")
        return font

    def _ffmpeg_identity(self, deadline: float) -> str:
        raw = self._capture(
            [str(self.ffmpeg_exe), "-hide_banner", "-version"], deadline,
            failure_kind=JobErrorKind.WORKER_UNREACHABLE,
            context="FFmpeg identity probe",
        )
        try:
            identity = normalize_ffmpeg_identity(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           "FFmpeg identity output was not valid UTF-8", cause=exc) from exc
        if not identity:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "FFmpeg returned an empty build identity")
        return identity

    def _probe(self, path: Path, deadline: float, label: str) -> MediaInfo:
        command = [
            str(self.ffprobe_exe), "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,sample_aspect_ratio,avg_frame_rate,r_frame_rate,duration:format=duration",
            "-of", "json", str(path),
        ]
        raw = self._capture(
            command, deadline, failure_kind=JobErrorKind.CORRUPT_ARTIFACT,
            context=f"ffprobe for {label}",
        )
        try:
            data = json.loads(raw.decode("utf-8"))
            stream = data["streams"][0]
            width, height = int(stream["width"]), int(stream["height"])
            sar = _parse_ratio(stream.get("sample_aspect_ratio"), "input sample aspect ratio", default=Fraction(1))
            fps_value = stream.get("avg_frame_rate")
            if fps_value in (None, "N/A", "0/0"):
                fps_value = stream.get("r_frame_rate")
            fps = _parse_ratio(fps_value, "input average frame rate")
            duration_value = stream.get("duration")
            if duration_value in (None, "N/A"):
                duration_value = data.get("format", {}).get("duration")
            duration = Fraction(str(duration_value))
            if width <= 0 or height <= 0 or fps <= 0 or duration <= 0:
                raise ValueError("width, height, frame rate, and duration must be positive")
            return MediaInfo(width, height, sar, fps, duration)
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, json.JSONDecodeError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"ffprobe returned invalid media metadata for {label}: {exc}", cause=exc) from exc

    def _capture(
        self,
        command: list[str],
        deadline: float,
        *,
        failure_kind: JobErrorKind,
        context: str,
    ) -> bytes:
        process = self._launch(command, deadline, context, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while True:
                self._check_running(deadline, context)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.05, deadline - self.clock()))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except OSError as exc:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                           f"could not wait for {context}: {exc}", cause=exc) from exc
        self.control.clear_process(process)
        self._check_running(deadline, context)
        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace")[-3000:]
            raise JobError(failure_kind, f"{context} exited {process.returncode}: {detail}")
        return stdout or b""

    def _encode(
        self,
        command: list[str],
        log_path: Path,
        deadline: float,
        output_relative: str,
        log_relative: str,
    ) -> None:
        with log_path.open("wb") as log_stream:
            process = self._launch(
                command, deadline, "FFmpeg episode assembly",
                stdout=log_stream, stderr=subprocess.STDOUT,
            )
            try:
                while True:
                    self._check_running(deadline, "FFmpeg episode assembly")
                    remaining = deadline - self.clock()
                    try:
                        code = process.wait(timeout=min(0.05, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except OSError as exc:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               f"could not wait for FFmpeg episode assembly: {exc}", cause=exc) from exc
        # Keep a live or indeterminate child registered. The supervisor must stop it
        # before the owning job can report quiescence or release resources.
        self.control.clear_process(process)
        self._check_running(deadline, "FFmpeg episode assembly")
        if code != 0:
            try:
                detail = self.root.read_text(log_relative, encoding="utf-8")[-3000:]
            except (OSError, ValueError):
                detail = "FFmpeg did not leave a readable contained log"
            lowered = detail.lower()
            if any(token in lowered for token in ("no space left on device", "enospc", "error -28", "error 112")):
                kind = JobErrorKind.DISK_EXHAUSTED
            elif any(token in lowered for token in ("unknown encoder", "no such filter", "option not found", "not implemented")):
                kind = JobErrorKind.INVALID_MANIFEST
            else:
                kind = JobErrorKind.CORRUPT_ARTIFACT
            raise JobError(kind, f"FFmpeg episode assembly exited {code}: {detail}")
        if not self.root.resolve(output_relative).is_file():
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           "FFmpeg episode assembly exited successfully without producing the cut")

    def _launch(self, command: list[str], deadline: float, context: str, **streams: object) -> subprocess.Popen[bytes]:
        self._check_running(deadline, context)
        try:
            return self.control.launch(
                command,
                popen=self.popen,
                stdin=subprocess.DEVNULL,
                shell=False,
                cwd=self.root.path,
                **streams,
            )
        except OSError as exc:
            raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                           f"could not launch {context}: {exc}", cause=exc) from exc
        except JobError:
            raise
        except Exception as exc:
            self._raise_if_stopped(context)
            raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                           f"could not launch {context}: {exc}", cause=exc) from exc

    def _check_running(self, deadline: float, context: str) -> None:
        if self.control.stopped():
            self._raise_if_stopped(context)
        remaining = deadline - self.clock()
        if remaining <= 0:
            try:
                stopped_cleanly = self.control.stop_for(JobErrorKind.BUDGET_EXCEEDED)
            except Exception as exc:
                raise JobError(
                    JobErrorKind.WORKER_UNREACHABLE,
                    f"supervisor could not stop {context} after its wall-clock budget: {exc}",
                    cause=exc,
                ) from exc
            if not stopped_cleanly:
                raise JobError(
                    JobErrorKind.WORKER_UNREACHABLE,
                    f"supervisor could not confirm {context} stopped after its wall-clock budget",
                )
            self._raise_if_stopped(context)
            raise JobError(JobErrorKind.BUDGET_EXCEEDED, f"{context} exceeded its wall-clock budget")

    def _raise_if_stopped(self, context: str) -> None:
        if not self.control.stopped():
            return
        reason = getattr(self.control, "_stop_reason", None) or JobErrorKind.CANCELLED
        message = f"{context} exceeded its wall-clock budget" if reason is JobErrorKind.BUDGET_EXCEEDED else f"{context} was cancelled"
        raise JobError(reason, message)


def _parse_ratio(value: object, name: str, *, default: Fraction | None = None) -> Fraction:
    if value in (None, "", "N/A", "0/0") and default is not None:
        return default
    if not isinstance(value, (str, int)):
        raise _invalid(f"{name} must be a positive ratio")
    try:
        parsed = Fraction(str(value).replace(":", "/"))
    except (ValueError, ZeroDivisionError) as exc:
        raise _invalid(f"{name} {value!r} is not a valid positive ratio") from exc
    if parsed <= 0:
        raise _invalid(f"{name} must be positive")
    return parsed


def _ratio_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _round_positive(value: Fraction) -> int:
    return max(1, (2 * value.numerator + value.denominator) // (2 * value.denominator))


def _format_seconds(value: Fraction) -> str:
    return f"{float(value):.9f}".rstrip("0").rstrip(".") or "0"


def _escape_filter_value(value: str) -> str:
    value = value.replace("\\", "\\\\")
    for char in (":", "'", ",", ";", "[", "]"):
        value = value.replace(char, "\\" + char)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _invalid(message: str) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, message)
