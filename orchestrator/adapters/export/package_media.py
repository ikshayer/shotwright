"""Bounded local media validation and delivery encoding for portable packages."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity

_INPUT_OPTIONS = ("-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm")
_SILENCE = "anullsrc=r=48000:cl=mono"


@dataclass(frozen=True)
class AudioStream:
    index: int
    codec: str
    duration: Fraction


@dataclass(frozen=True)
class CutMedia:
    video_index: int
    video_codec: str
    resolution: tuple[int, int]
    sample_aspect_ratio: Fraction
    frame_rate: Fraction
    video_duration: Fraction
    audio: tuple[AudioStream, ...]

    def evidence(self) -> dict[str, object]:
        return {
            "video_codec": self.video_codec,
            "audio_codecs": [stream.codec for stream in self.audio],
            "resolution": self.resolution,
            "sample_aspect_ratio": str(self.sample_aspect_ratio),
            "frame_rate": str(self.frame_rate),
            "duration_seconds": float(self.video_duration),
            "audio_duration_seconds": [float(stream.duration) for stream in self.audio],
        }


@dataclass(frozen=True)
class DeliveryEvidence:
    ffmpeg_identity: str
    media: CutMedia
    silent_audio_added: bool
    filter_graph_hash: str


def encode_delivery(
    ffmpeg: Path, *, source: Path, output: Path, temporary_directory: Path,
    expected_resolution: tuple[int, int], expected_frame_rate: int, expected_sar: str,
    timeline_has_audio: bool, budget_seconds: float,
) -> DeliveryEvidence:
    """Verify the bound cut and make a playable MP4 in the private package stage."""
    if not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise JobError(JobErrorKind.INVALID_MANIFEST, "export wall-clock budget must be finite and positive")
    deadline = time.monotonic() + budget_seconds
    ffmpeg = _resolve_executable(ffmpeg)
    sibling = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    ffprobe = sibling if ffmpeg.is_absolute() and sibling.is_file() else Path("ffprobe")
    env = os.environ.copy()
    env.update({key: str(temporary_directory) for key in ("TMP", "TEMP", "TMPDIR")})

    raw_identity = _run(
        [str(ffmpeg), "-hide_banner", "-version"], deadline, env, source.parent,
        "FFmpeg identity probe", JobErrorKind.WORKER_UNREACHABLE,
    )
    try:
        identity = normalize_ffmpeg_identity(raw_identity.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "FFmpeg identity bytes are not valid UTF-8", cause=exc) from exc
    if not identity.strip():
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "FFmpeg returned no build identity")

    before = _probe(ffprobe, source, deadline, env)
    ratio = Fraction(expected_sar.replace(":", "/"))
    if (before.resolution, before.frame_rate, before.sample_aspect_ratio) != (
        expected_resolution, expected_frame_rate, ratio,
    ):
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            "assembled cut resolution, frame rate, or sample aspect ratio differs from its receipt",
        )
    silent_audio = not before.audio
    if silent_audio and timeline_has_audio:
        raise JobError(
            JobErrorKind.INVALID_MANIFEST,
            "the Timeline names audio tracks but the selected cut has no audio stream. "
            "Mix those tracks into a new cut and approve it before exporting",
        )
    _run(
        _command(ffmpeg, source, output, before), deadline, env, output.parent,
        "FFmpeg package delivery", JobErrorKind.CORRUPT_ARTIFACT,
    )
    after = _probe(ffprobe, output, deadline, env)
    _verify_delivery(before, after, silent_audio)
    return DeliveryEvidence(
        identity, after, silent_audio,
        hashlib.sha256((_SILENCE if silent_audio else "").encode("ascii")).hexdigest(),
    )


def _resolve_executable(exe: Path) -> Path:
    return exe.resolve() if exe.is_absolute() or exe.parent != Path(".") else exe


def _command(ffmpeg: Path, source: Path, output: Path, media: CutMedia) -> list[str]:
    argv = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-n", "-loglevel", "error",
        *_INPUT_OPTIONS, "-i", str(source),
    ]
    if not media.audio:
        # Trim the silence input itself so audio packet rounding cannot end video early.
        microseconds = round(media.video_duration * 1_000_000)
        argv.extend(("-t", f"{microseconds}us", "-f", "lavfi", "-i", _SILENCE))
    argv.extend(("-map", f"0:{media.video_index}"))
    if media.video_codec == "h264":
        argv.extend(("-c:v", "copy"))
    else:
        pix_fmt = "yuv420p" if all(dimension % 2 == 0 for dimension in media.resolution) else "yuv444p"
        argv.extend(("-c:v", "libx264", "-preset", "medium", "-crf", "18",
                     "-pix_fmt", pix_fmt, "-fps_mode", "passthrough"))
    if media.audio:
        for index, stream in enumerate(media.audio):
            argv.extend(("-map", f"0:{stream.index}"))
            if stream.codec == "aac":
                argv.extend((f"-c:a:{index}", "copy"))
            else:
                argv.extend((f"-c:a:{index}", "aac", f"-b:a:{index}", "160k"))
    else:
        argv.extend(("-map", "1:a:0", "-c:a", "aac", "-b:a", "160k"))
    return argv + [
        "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-movflags", "+faststart", "-f", "mp4", str(output),
    ]


def _probe(ffprobe: Path, path: Path, deadline: float, env: dict[str, str]) -> CutMedia:
    raw = _run(
        [str(ffprobe), "-v", "error", *_INPUT_OPTIONS,
         "-show_entries",
         ("stream=index,codec_name,codec_type,width,height,sample_aspect_ratio,"
         "avg_frame_rate,r_frame_rate,duration:stream_disposition=attached_pic:format=duration"),
         "-of", "json", str(path)],
        deadline, env, path.parent, f"FFprobe for {path.name}", JobErrorKind.CORRUPT_ARTIFACT,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
        streams = payload["streams"]
        video, = [
            row for row in streams
            if row["codec_type"] == "video" and not row.get("disposition", {}).get("attached_pic")
        ]
        container_duration = payload.get("format", {}).get("duration")
        width, height = int(video["width"]), int(video["height"])
        if width <= 0 or height <= 0:
            raise ValueError("video resolution must be positive")
        audio = tuple(
            AudioStream(int(row["index"]), row["codec_name"],
                        _positive_fraction(row.get("duration"), container_duration))
            for row in streams if row["codec_type"] == "audio"
        )
        return CutMedia(
            int(video["index"]), video["codec_name"], (width, height),
            _positive_fraction(video.get("sample_aspect_ratio"), "1/1"),
            _positive_fraction(video.get("avg_frame_rate"), video.get("r_frame_rate")),
            _positive_fraction(video.get("duration"), container_duration), audio,
        )
    except (KeyError, TypeError, AttributeError, ValueError, ZeroDivisionError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"FFprobe returned invalid media metadata for {path.name}: {exc}", cause=exc) from exc


def _positive_fraction(value: object, fallback: object) -> Fraction:
    if value in (None, "N/A", "0:0", "0/0"):
        value = fallback
    result = Fraction(str(value).replace(":", "/"))
    if result <= 0:
        raise ValueError(f"media ratio or duration must be positive, got {value!r}")
    return result


def _verify_delivery(source: CutMedia, delivered: CutMedia, silent_audio: bool) -> None:
    if (delivered.video_codec != "h264" or len(delivered.audio) != (len(source.audio) or 1)
            or any(track.codec != "aac" for track in delivered.audio)):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "delivery validation requires H.264 video and AAC audio")
    if (delivered.resolution, delivered.frame_rate, delivered.sample_aspect_ratio) != (
        source.resolution, source.frame_rate, source.sample_aspect_ratio,
    ):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "delivery changed the cut resolution, frame rate, or aspect ratio")
    if abs(delivered.video_duration - source.video_duration) > 1 / source.frame_rate:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "delivery changed the cut duration")
    if silent_audio and abs(delivered.audio[0].duration - delivered.video_duration) > Fraction(1024, 48000):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "generated AAC silence does not span the delivered video")


def _run(
    argv: list[str], deadline: float, env: dict[str, str], cwd: Path,
    label: str, failure_kind: JobErrorKind,
) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise JobError(JobErrorKind.BUDGET_EXCEEDED, f"{label} exceeded the export wall-clock budget")
    try:
        result = subprocess.run(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
            shell=False, timeout=remaining, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise JobError(JobErrorKind.BUDGET_EXCEEDED,
                       f"{label} exceeded the export wall-clock budget", cause=exc) from exc
    except OSError as exc:
        kind = JobErrorKind.DISK_EXHAUSTED if exc.errno == errno.ENOSPC else JobErrorKind.WORKER_UNREACHABLE
        raise JobError(kind, f"could not launch {label}: {exc}", cause=exc) from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[-2500:].strip()
        if "no space left" in detail.lower() or "disk full" in detail.lower():
            failure_kind = JobErrorKind.DISK_EXHAUSTED
        raise JobError(failure_kind, f"{label} exited {result.returncode}: {detail}")
    return result.stdout

