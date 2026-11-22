"""Read-only perception helpers for verified project media."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Literal

from PIL import Image, ImageChops, ImageFilter, ImageOps, UnidentifiedImageError

from orchestrator.domain.entities import (
    Asset,
    EpisodeOutput,
    RubricScores,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.platform.containment import ContainmentError
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.inspect import (
    _IMAGE_SUFFIXES,
    _VIDEO_DEMUXERS,
)
from orchestrator.store.json_store import ProjectStore

_AUDIO_DEMUXERS = {
    ".wav": "wav",
    ".flac": "flac",
    ".mp3": "mp3",
    ".m4a": "mov",
    ".ogg": "ogg",
}
_AUDIO_ASSET_TYPES = frozenset({"audio", "voice"})
_MAX_FRAME_SECONDS = 86_400.0
_MAX_SOURCE_PIXELS = 50_000_000
_CONTACT_SHEET_LONG_EDGE = 1536
_CONTACT_SHEET_MAX_BYTES = 4 * 1024 * 1024
_MEDIA_BUDGET_SECONDS = 120.0
_SUBPROCESS_STDOUT_MAX_BYTES = 16 * 1024 * 1024
_SUBPROCESS_STDERR_TAIL_BYTES = 64 * 1024
_SILENCE_THRESHOLD_DB = -50
_SILENCE_MIN_SECONDS = 0.5
_SCENE_THRESHOLD = 0.3
_CREDIT_MANIFEST_SCHEMA = 1
_METRIC_UNITS = {
    "duration_sec": "seconds",
    "overall_bitrate_bps": "bits per second",
    "width_px": "pixels",
    "height_px": "pixels",
    "fps_frames_per_sec": "frames per second",
    "time_sec": "seconds",
    "frame_time_sec": "seconds",
    "luma_mean_0_255": "8-bit luma level",
    "luma_min_0_255": "8-bit luma level",
    "luma_max_0_255": "8-bit luma level",
    "temporal_luma_difference_0_255": "8-bit luma level difference",
    "saturation_mean_0_255": "8-bit saturation level",
    "edge_energy_mean_0_255": "8-bit edge luma level",
    "active_width_px": "pixels",
    "active_height_px": "pixels",
    "active_x_px": "pixels",
    "active_y_px": "pixels",
    "left_bar_estimate_px": "pixels",
    "right_bar_estimate_px": "pixels",
    "top_bar_estimate_px": "pixels",
    "bottom_bar_estimate_px": "pixels",
    "active_area_fraction": "fraction of full frame area",
    "scene_change_times_sec_threshold_0_3": "seconds",
    "sample_rate_hz": "samples per second",
    "channels": "audio channels",
    "integrated_loudness_lufs": "LUFS",
    "loudness_range_lu": "LU",
    "true_peak_dbtp": "dBTP",
    "start_sec": "seconds",
    "end_sec": "seconds",
}


class PerceptionInvalid(ValueError):
    """A requested perception operation does not apply to this media or input."""


@dataclass(frozen=True)
class _MediaProbe:
    format_name: str
    duration_sec: float
    bitrate_bps: int | None
    streams: tuple[dict[str, Any], ...]
    external_track_skipped: bool = False

    @property
    def video(self) -> dict[str, Any] | None:
        return next(
            (row for row in self.streams
             if row.get("codec_type") == "video"
             and not row.get("disposition", {}).get("attached_pic")),
            None,
        )

    @property
    def audio(self) -> dict[str, Any] | None:
        return next((row for row in self.streams if row.get("codec_type") == "audio"), None)


class _Deadline:
    """One wall-clock budget shared by every FFmpeg/ffprobe child in an operation."""

    def __init__(self, seconds: float = _MEDIA_BUDGET_SECONDS) -> None:
        self.seconds = seconds
        self.end = time.monotonic() + self.seconds

    def check(self, operation: str = "perception operation") -> None:
        if time.monotonic() >= self.end:
            raise JobError(
                JobErrorKind.BUDGET_EXCEEDED,
                f"{operation} exceeded the perception operation's {self.seconds:g}-second budget",
            )

    def run(
        self,
        command: list[str],
        *,
        operation: str,
        stderr_line_parser: Callable[[str], None] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise JobError(
                JobErrorKind.BUDGET_EXCEEDED,
                f"perception operation exceeded its {self.seconds:g}-second budget",
            )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise JobError(
                JobErrorKind.WORKER_UNREACHABLE,
                f"could not run {operation}: {exc}",
                cause=exc,
            ) from exc

        stdout = bytearray()
        stderr_tail = bytearray()
        stdout_exceeded = threading.Event()
        reader_errors: list[BaseException] = []

        def stop_child() -> None:
            try:
                process.kill()
            except OSError:
                pass

        def drain_stdout() -> None:
            assert process.stdout is not None
            try:
                while True:
                    # Read at most one byte beyond the cap to detect overflow.
                    chunk = process.stdout.read(min(64 * 1024, _SUBPROCESS_STDOUT_MAX_BYTES + 1 - len(stdout)))
                    if not chunk:
                        return
                    if len(stdout) + len(chunk) > _SUBPROCESS_STDOUT_MAX_BYTES:
                        stdout.extend(chunk[:_SUBPROCESS_STDOUT_MAX_BYTES - len(stdout)])
                        stdout_exceeded.set()
                        stop_child()
                        return
                    stdout.extend(chunk)
            except BaseException as exc:
                reader_errors.append(exc)
                stop_child()

        def drain_stderr() -> None:
            assert process.stderr is not None
            pending = bytearray()
            try:
                while True:
                    chunk = process.stderr.read(64 * 1024)
                    if not chunk:
                        break
                    stderr_tail.extend(chunk)
                    if len(stderr_tail) > _SUBPROCESS_STDERR_TAIL_BYTES:
                        del stderr_tail[:-_SUBPROCESS_STDERR_TAIL_BYTES]
                    pending.extend(chunk)
                    while True:
                        newline = pending.find(b"\n")
                        carriage_return = pending.find(b"\r")
                        points = [point for point in (newline, carriage_return) if point >= 0]
                        if not points:
                            break
                        point = min(points)
                        line = bytes(pending[:point])
                        del pending[:point + 1]
                        if stderr_line_parser is not None and line:
                            stderr_line_parser(line.decode("utf-8", errors="replace"))
                    if len(pending) > _SUBPROCESS_STDERR_TAIL_BYTES:
                        # FFmpeg diagnostics are line-oriented. Bound malformed or
                        # progress output that does not contain a line separator.
                        del pending[:-_SUBPROCESS_STDERR_TAIL_BYTES]
                if stderr_line_parser is not None and pending:
                    stderr_line_parser(bytes(pending).decode("utf-8", errors="replace"))
            except BaseException as exc:
                reader_errors.append(exc)
                stop_child()

        stdout_thread = threading.Thread(target=drain_stdout, name="perception-stdout", daemon=True)
        stderr_thread = threading.Thread(target=drain_stderr, name="perception-stderr", daemon=True)
        started: list[threading.Thread] = []
        timed_out = False
        try:
            # Started inside the try: if a reader cannot start, the finally below still
            # kills FFmpeg and joins whichever reader did start.
            for thread in (stdout_thread, stderr_thread):
                try:
                    thread.start()
                except BaseException as exc:  # noqa: BLE001 - reported below as a JobError.
                    # Treated like a reader failure: the loop below stops FFmpeg, and
                    # the error surfaces as INTERNAL instead of a bare 500.
                    reader_errors.append(exc)
                    break
                started.append(thread)
            while True:
                if stdout_exceeded.is_set() or reader_errors:
                    stop_child()
                    break
                remaining = self.end - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    stop_child()
                    break
                try:
                    process.wait(timeout=min(remaining, 0.1))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                stop_child()
            process.wait()
            for thread in started:
                thread.join()

        if timed_out:
            raise JobError(
                JobErrorKind.BUDGET_EXCEEDED,
                f"{operation} exceeded the perception operation's {self.seconds:g}-second budget; process was killed",
            )
        if reader_errors:
            error = reader_errors[0]
            if isinstance(error, JobError):
                raise error
            raise JobError(JobErrorKind.INTERNAL, f"could not read {operation} output: {error}", cause=error) from error
        if stdout_exceeded.is_set():
            raise JobError(
                JobErrorKind.CORRUPT_ARTIFACT,
                f"{operation} produced more than {_SUBPROCESS_STDOUT_MAX_BYTES} bytes of stdout",
            )
        self.check(operation)
        result = subprocess.CompletedProcess(command, process.returncode, bytes(stdout), bytes(stderr_tail))
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise JobError(
                JobErrorKind.CORRUPT_ARTIFACT,
                f"{operation} failed: {detail or f'exit status {result.returncode}'}",
            )
        return result


def contact_sheet(
    store: ProjectStore,
    *,
    episode_id: str,
    output_id: str,
    ffmpeg_exe: str | Path,
    ffprobe_exe: str | Path | None = None,
    frames: int = 9,
    times: str | None = None,
    mode: Literal["frames", "difference"] = "frames",
) -> bytes:
    """Return a bounded PNG sheet from a verified video output."""
    deadline = _Deadline()
    output = _find_output(store, episode_id, output_id)
    deadline.check("contact_sheet")
    relative = output.relative_path
    suffix = Path(relative).suffix.lower()
    if isinstance(output, ShotOutput) and output.kind is ShotOutputKind.SCENE:
        raise PerceptionInvalid("scene outputs have no frames; use inspect_output on a rendered preview")
    if isinstance(output, ShotOutput) and output.kind in {ShotOutputKind.PREVIEW, ShotOutputKind.STILL}:
        raise PerceptionInvalid("contact_sheet accepts video outputs only; use inspect_output for image outputs")
    if (isinstance(output, ShotOutput) and output.kind is ShotOutputKind.GENERATED
            and suffix in _IMAGE_SUFFIXES):
        raise PerceptionInvalid("contact_sheet accepts video outputs only; use inspect_output for image outputs")
    demuxer = _VIDEO_DEMUXERS.get(suffix)
    if demuxer is None:
        raise PerceptionInvalid(
            f"output {output_id!r} is not a supported video; use inspect_output for a single frame"
        )
    if type(frames) is not int or not 2 <= frames <= 16:
        raise PerceptionInvalid("frames must be an integer from 2 through 16")
    if mode not in {"frames", "difference"}:
        raise PerceptionInvalid("mode must be 'frames' or 'difference'")

    source = _contained_media_path(store, relative)
    if isinstance(output, ShotOutput) and output.frame_count and output.output_fps:
        duration = output.frame_count / output.output_fps
    else:
        probe = _probe_media(
            source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe,
            ffprobe_exe=ffprobe_exe, deadline=deadline, require_video=True,
        )
        duration = probe.duration_sec
        video = probe.video
        if video is None:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"verified output {output_id!r} has no video stream")
    sample_times = _parse_sample_times(times, frames, duration)
    tile_count = len(sample_times) if mode == "frames" else len(sample_times) - 1
    columns = math.ceil(math.sqrt(tile_count))
    rows = math.ceil(tile_count / columns)
    label_height = 20
    max_tile_edge = min(
        512,
        _CONTACT_SHEET_LONG_EDGE // max(columns, rows) - label_height,
    )
    frames_png = [
        _extract_scaled_frame(
            source, output_id=output_id, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe,
            ffprobe_exe=ffprobe_exe, at_sec=at_sec, max_edge=max_tile_edge,
            duration=duration, deadline=deadline,
        )
        for at_sec in sample_times
    ]
    with_images = [_decode_small_frame(raw, output_id) for raw in frames_png]
    deadline.check("contact_sheet")
    if mode == "difference":
        tiles = [
            ImageOps.autocontrast(ImageChops.difference(left.convert("RGB"), right.convert("RGB")))
            for left, right in zip(with_images, with_images[1:])
        ]
        labels = [f"{_timestamp(left)}→{_timestamp(right)}"
                  for left, right in zip(sample_times, sample_times[1:])]
    else:
        tiles = [image.convert("RGB") for image in with_images]
        labels = [_timestamp(value) for value in sample_times]
    result = _compose_contact_sheet(tiles, labels, output_id, deadline=deadline)
    deadline.check("contact_sheet")
    if len(result) > _CONTACT_SHEET_MAX_BYTES:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"contact sheet for output {output_id!r} is {len(result)} bytes; maximum is {_CONTACT_SHEET_MAX_BYTES}",
        )
    return result


def rubric_sample(
    store: ProjectStore,
    *,
    episode_id: str,
    output_id: str,
    _deadline: _Deadline | None = None,
) -> dict[str, Any]:
    """Build the deterministic FR-22 sample plan for one immutable cut."""
    deadline = _deadline or _Deadline()
    output = _find_output(store, episode_id, output_id)
    deadline.check("rubric_sample")
    if not isinstance(output, EpisodeOutput):
        raise PerceptionInvalid("rubric_sample requires a cut from list_cuts")
    timeline = store.load_timeline_at(episode_id, output.timeline_store_version)
    deadline.check("rubric_sample")
    narrative_duration = timeline.total_duration_sec
    if not math.isfinite(narrative_duration) or narrative_duration <= 0:
        raise PerceptionInvalid("the cut Timeline must have a positive finite narrative duration")

    pinned_versions, source = _cut_shot_versions(store, output, deadline=deadline)
    shot_entries = sorted(
        (entry for entry in timeline.entries if entry.type == "shot" and entry.ref),
        key=lambda entry: (entry.start_sec, entry.ref or ""),
    )
    shot_records: dict[str, Shot] = {}
    for entry in shot_entries:
        shot_id = entry.ref
        if shot_id in shot_records:
            continue
        if source == "credit_manifest" and shot_id not in pinned_versions:
            raise JobError(
                JobErrorKind.INVALID_MANIFEST,
                f"cut credit manifest has no pinned Shot version for {shot_id!r}",
            )
        version = pinned_versions.get(shot_id)
        shot_records[shot_id] = store.load(
            Shot, shot_id, episode_id=episode_id, version=version,
        )
        deadline.check("rubric_sample")

    anomalies = sorted(
        (
            (entry.start_sec + shot_records[entry.ref].event_sec, entry.ref)
            for entry in shot_entries
            if entry.ref in shot_records
            and shot_records[entry.ref].anomaly is not None
            and shot_records[entry.ref].event_sec is not None
        ),
        key=lambda item: (item[0], item[1]),
    )
    samples: list[dict[str, Any]] = []

    def add(time_sec: float, reason: str, shot_id: str | None) -> None:
        samples.append({
            "index": len(samples),
            "time_sec": round(time_sec, 6),
            "reason": reason,
            "shot_id": shot_id,
        })

    for step in range(math.ceil(narrative_duration / 3.0)):
        time_sec = step * 3.0
        if time_sec < narrative_duration:
            add(time_sec, "every_3s", _shot_at(shot_entries, time_sec))

    if anomalies:
        anomaly_time, _anomaly_shot_id = anomalies[0]
        for time_sec in _sequence_times(anomaly_time, narrative_duration):
            add(time_sec, "first_anomaly", _shot_at(shot_entries, time_sec))

    bounds = [0.0, *(time for time, _ in anomalies), narrative_duration]
    spans = [(right - left, left, right) for left, right in zip(bounds, bounds[1:])]
    _span, neutral_start, neutral_end = max(spans, key=lambda item: (item[0], -item[1]))
    neutral_midpoint = (neutral_start + neutral_end) / 2
    for time_sec in _sequence_times(neutral_midpoint, narrative_duration):
        add(time_sec, "longest_neutral", _shot_at(shot_entries, time_sec))
    deadline.check("rubric_sample")

    interpretation = (
        "The plan includes one sample every 3 seconds before the narrative end, a five-frame "
        "sequence at 0.5-second spacing around the first anomaly when one exists, and a "
        "five-frame sequence at 0.5-second spacing around the longest neutral interval. "
        "Sequences are shifted/clamped to remain within the narrative. "
        + ("No anomaly exists, so the first-anomaly sequence is omitted." if not anomalies
           else "The anomaly sequence uses the Shot version pinned by this cut's credit manifest when available.")
        + " To review: use contact_sheet with the sequence times (or inspect_output for each sample), "
        "submit them to evaluate_rubric, then record the outcome with record_review."
    )
    return {
        "output_id": output_id,
        "narrative_duration_sec": narrative_duration,
        "shot_versions_source": source,
        "samples": samples,
        "interpretation": interpretation,
    }


def evaluate_rubric(
    store: ProjectStore,
    *,
    episode_id: str,
    output_id: str,
    scores: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate submitted binary scores against the immutable cut's sample indices."""
    deadline = _Deadline()
    plan = rubric_sample(store, episode_id=episode_id, output_id=output_id, _deadline=deadline)
    expected = {row["index"] for row in plan["samples"]}
    actual_indices = [row.get("index") for row in scores]
    index_counts = Counter(actual_indices)
    duplicates = {index for index, count in index_counts.items() if count > 1}
    provided = set(actual_indices)
    missing = sorted(expected - provided)
    extra = sorted((provided - expected) | duplicates, key=lambda item: (item is None, str(item)))
    if missing or extra:
        bits = []
        if missing:
            bits.append(f"missing indices {missing}")
        if extra:
            bits.append(f"extra or duplicate indices {extra}")
        raise PerceptionInvalid("scores must contain exactly one entry for every rubric sample index: " + "; ".join(bits))

    validated: dict[int, RubricScores] = {}
    for row in scores:
        index = row["index"]
        try:
            validated[index] = RubricScores.model_validate(row["scores"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PerceptionInvalid(f"scores[{index}] must contain a valid RubricScores object: {exc}") from exc
    per_sample = [
        {"index": index, "passed": validated[index].passed()}
        for index in sorted(expected)
    ]
    passing_samples = sum(row["passed"] for row in per_sample)
    sample_count = len(per_sample)
    pass_ratio = passing_samples / sample_count if sample_count else 0.0
    criterion_3_failures = sorted(
        index for index, score in validated.items() if not score.no_unmotivated_movement
    )
    passed = (
        sample_count > 0
        and passing_samples / sample_count >= 0.8
        and not criterion_3_failures
    )
    deadline.check("evaluate_rubric")
    return {
        "passed": passed,
        "samples": sample_count,
        "passing_samples": passing_samples,
        "pass_ratio": pass_ratio,
        "criterion_3_failures": criterion_3_failures,
        "per_sample": per_sample,
        "next_step": (
            "Record this outcome with record_review (target_kind='episode_cut', include rubric_scores, "
            "and include the creator's waiver_text if the cut failed)."
        ),
    }


def measure_output(
    store: ProjectStore,
    *,
    episode_id: str,
    output_id: str,
    ffmpeg_exe: str | Path,
    ffprobe_exe: str | Path | None = None,
    samples: int = 12,
) -> dict[str, Any]:
    """Return measured media properties for a verified image or video output."""
    deadline = _Deadline()
    if type(samples) is not int or not 1 <= samples <= 60:
        raise PerceptionInvalid("samples must be an integer from 1 through 60")
    output = _find_output(store, episode_id, output_id)
    deadline.check("measure_output")
    suffix = Path(output.relative_path).suffix.lower()
    if isinstance(output, ShotOutput) and output.kind is ShotOutputKind.SCENE:
        raise PerceptionInvalid("scene outputs have no media metrics; render a preview and use measure_output")
    if isinstance(output, ShotOutput) and output.kind in {ShotOutputKind.PREVIEW, ShotOutputKind.STILL}:
        image_bytes = _read_verified_output_bytes(
            store, output.relative_path, output.content_sha256, output_id=output_id, deadline=deadline,
        )
        image = _image_metrics(image_bytes, output_id)
        deadline.check("measure_output image metrics")
        return {"output_id": output_id, "image": image, "units": dict(_METRIC_UNITS)}
    if (isinstance(output, ShotOutput) and output.kind is ShotOutputKind.GENERATED
            and suffix in _IMAGE_SUFFIXES):
        image_bytes = _read_verified_output_bytes(
            store, output.relative_path, output.content_sha256, output_id=output_id, deadline=deadline,
        )
        image = _image_metrics(image_bytes, output_id)
        deadline.check("measure_output image metrics")
        return {"output_id": output_id, "image": image, "units": dict(_METRIC_UNITS)}
    demuxer = _VIDEO_DEMUXERS.get(suffix)
    if demuxer is None:
        raise PerceptionInvalid(f"output {output_id!r} has no supported video or image metrics")
    source = _contained_media_path(store, output.relative_path)
    probe = _probe_media(
        source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe, ffprobe_exe=ffprobe_exe,
        deadline=deadline, require_video=True,
    )
    video = probe.video
    if video is None:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"verified output {output_id!r} has no video stream")
    width, height = _positive_int(video.get("width"), "video width"), _positive_int(video.get("height"), "video height")
    fps = _parse_rate(video.get("avg_frame_rate"), video.get("r_frame_rate"))
    sample_times = [probe.duration_sec * (index + 0.5) / samples for index in range(samples)]
    video_metrics = _measure_video(
        source, output_id=output_id, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe,
        ffprobe_exe=ffprobe_exe, deadline=deadline, video=video,
        width=width, height=height, fps=fps, duration=probe.duration_sec,
        sample_times=sample_times,
    )
    result: dict[str, Any] = {
        "output_id": output_id,
        "container": _container_metrics(probe),
        "video": video_metrics,
        "units": dict(_METRIC_UNITS),
    }
    if probe.audio is not None:
        result["audio"] = _measure_audio(
            source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe,
            deadline=deadline, audio=probe.audio,
            stream_duration=_audio_stream_duration(probe.audio, probe.duration_sec),
        )
    deadline.check("measure_output")
    return result


def measure_asset(
    store: ProjectStore,
    *,
    asset_id: str,
    ffmpeg_exe: str | Path,
    ffprobe_exe: str | Path | None = None,
) -> dict[str, Any]:
    """Measure an audio Asset after checking its project-local checksum."""
    deadline = _Deadline()
    asset = store.load(Asset, asset_id)
    deadline.check("measure_asset")
    if asset.type not in _AUDIO_ASSET_TYPES or not asset.file_path:
        raise PerceptionInvalid("measure_asset accepts only audio Assets with a project-local file_path")
    suffix = Path(asset.file_path).suffix.lower()
    demuxer = _AUDIO_DEMUXERS.get(suffix)
    if demuxer is None:
        raise PerceptionInvalid(
            f"audio Asset {asset_id!r} has unsupported extension {suffix or '(none)'!r}; "
            f"accepted extensions are {', '.join(sorted(_AUDIO_DEMUXERS))}"
        )
    source = _verify_asset_file(store, asset, deadline=deadline)
    probe = _probe_media(
        source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe, ffprobe_exe=ffprobe_exe,
        deadline=deadline, require_video=False,
    )
    audio = probe.audio
    if audio is None:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"audio Asset {asset_id!r} has no audio stream")
    result = {
        "asset_id": asset_id,
        "container": _container_metrics(probe),
        "audio": _measure_audio(
            source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe,
            deadline=deadline, audio=audio,
            stream_duration=_audio_stream_duration(audio, probe.duration_sec),
        ),
        "units": dict(_METRIC_UNITS),
    }
    deadline.check("measure_asset")
    return result


def audio_review_demuxer(relative_path: str, *, output: bool) -> str | None:
    """Return the already-allowlisted demuxer used by audio review validation."""
    suffix = Path(relative_path).suffix.lower()
    return (_VIDEO_DEMUXERS if output else _AUDIO_DEMUXERS).get(suffix)


def probe_audio_review_media(
    store: ProjectStore,
    relative_path: str,
    *,
    demuxer: str,
    ffmpeg_exe: str | Path,
) -> dict[str, Any]:
    """Probe verified local review media with perception's named-demuxer deadline path."""
    source = _contained_media_path(store, relative_path)
    probe = _probe_media(
        source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe, ffprobe_exe=None,
        deadline=_Deadline(), require_video=False,
    )
    return {
        "duration_sec": probe.duration_sec,
        "audio": probe.audio,
        "video": probe.video,
        "format": probe.format_name,
    }


def verify_audio_review_asset(store: ProjectStore, asset: Asset) -> Path:
    """Expose measure_asset's existing path, existence and checksum verification."""
    return _verify_asset_file(store, asset, deadline=_Deadline())


def _find_output(store: ProjectStore, episode_id: str, output_id: str) -> ShotOutput | EpisodeOutput:
    try:
        return store.find_output(episode_id, output_id)
    except JobError as exc:
        if exc.kind is JobErrorKind.INVALID_MANIFEST and (
            "artifact hash mismatch" in exc.message
            or "artifact is missing or not a file" in exc.message
        ):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, exc.message, cause=exc) from exc
        raise


def _contained_media_path(store: ProjectStore, relative_path: str) -> Path:
    try:
        return store._root.resolve(relative_path)
    except ContainmentError as exc:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"media artifact path {relative_path!r} is not contained in the project: {exc}",
            cause=exc,
        ) from exc


def _read_verified_output_bytes(
    store: ProjectStore,
    relative_path: str,
    content_sha256: str,
    *,
    output_id: str,
    deadline: _Deadline,
) -> bytes:
    try:
        path = store._root.resolve(relative_path)
    except ContainmentError as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"output artifact path {relative_path!r} is not contained in the project: {exc}",
                       cause=exc) from exc
    if not path.is_file():
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"output artifact is missing or not a file: {relative_path}")
    digest = hashlib.sha256()
    data = bytearray()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                deadline.check("measure_output image verification")
                digest.update(block)
                data.extend(block)
    except OSError as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"cannot read image output {output_id!r}: {exc}", cause=exc) from exc
    if digest.hexdigest() != content_sha256:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"output artifact hash mismatch: {relative_path}")
    deadline.check("measure_output image verification")
    return bytes(data)


def _verify_asset_file(store: ProjectStore, asset: Asset, *, deadline: _Deadline) -> Path:
    if not asset.file_path or not asset.checksum:
        raise PerceptionInvalid(f"audio Asset {asset.asset_id!r} needs file_path and checksum")
    try:
        path = store._root.resolve(asset.file_path)
    except ContainmentError as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"audio Asset path escapes the project: {exc}", cause=exc) from exc
    if not path.is_file():
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"audio Asset file is missing or not a file: {asset.file_path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                deadline.check("measure_asset verification")
                digest.update(block)
    except OSError as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"cannot verify audio Asset {asset.asset_id!r}: {exc}", cause=exc) from exc
    if digest.hexdigest() != asset.checksum:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"audio Asset {asset.asset_id!r} checksum mismatch")
    deadline.check("measure_asset verification")
    return path


def _parse_sample_times(times: str | None, frames: int, duration: float) -> list[float]:
    if not math.isfinite(duration) or duration <= 0:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "video duration must be positive and finite")
    if times is None:
        return [duration * (index + 0.5) / frames for index in range(frames)]
    tokens = times.split(",")
    if not 2 <= len(tokens) <= 16:
        raise PerceptionInvalid("times must contain 2 through 16 comma-separated seconds values")
    parsed: list[float] = []
    for token in tokens:
        try:
            value = float(token.strip())
        except ValueError as exc:
            raise PerceptionInvalid(f"times contains an invalid seconds value {token!r}") from exc
        if not math.isfinite(value) or value < 0:
            raise PerceptionInvalid("times values must be finite and greater than or equal to zero")
        parsed.append(value)
    if any(right <= left for left, right in zip(parsed, parsed[1:])):
        raise PerceptionInvalid("times values must be in strictly ascending order")
    if any(value >= duration for value in parsed):
        raise PerceptionInvalid(f"times values must be before the output end ({duration:.6f} seconds)")
    if any(value > _MAX_FRAME_SECONDS for value in parsed):
        raise PerceptionInvalid(f"times values may not exceed {_MAX_FRAME_SECONDS:g} seconds")
    return parsed


def _extract_scaled_frame(
    source: Path,
    *,
    output_id: str,
    demuxer: str,
    ffmpeg_exe: str | Path,
    ffprobe_exe: str | Path | None,
    at_sec: float,
    max_edge: int,
    duration: float,
    deadline: _Deadline,
) -> bytes:
    if at_sec > _MAX_FRAME_SECONDS:
        raise PerceptionInvalid(f"sample at {at_sec:g} seconds is past the end of the output")
    packet_time, _previous_time = _packet_presentation_time_at_or_before(
        source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe, ffprobe_exe=ffprobe_exe,
        stream_specifier="v:0", at_sec=at_sec, deadline=deadline,
    )
    command = [
        str(ffmpeg_exe), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-protocol_whitelist", "file", "-f", demuxer,
        "-ss", packet_time,
        "-i", str(source), "-map", "0:v:0",
        "-frames:v", "1",
        "-vf", f"scale=w='min(iw,{max_edge})':h='min(ih,{max_edge})':force_original_aspect_ratio=decrease",
        "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    result = deadline.run(command, operation=f"FFmpeg frame extraction for output {output_id!r}")
    if not result.stdout:
        raise PerceptionInvalid(f"sample time {at_sec:.6f} seconds produced no frame before output end {duration:.6f}")
    return result.stdout


def _decode_small_frame(data: bytes, output_id: str) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data), formats=("PNG",)) as image:
            if image.width * image.height > _MAX_SOURCE_PIXELS:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                               f"decoded frame for output {output_id!r} exceeds the pixel limit")
            image.load()
            return image.copy()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"FFmpeg returned an invalid PNG frame for output {output_id!r}: {exc}", cause=exc) from exc


def _compose_contact_sheet(
    tiles: list[Image.Image],
    labels: list[str],
    output_id: str,
    *,
    deadline: _Deadline,
) -> bytes:
    if not tiles or len(tiles) != len(labels):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"no contact-sheet frames were decoded for output {output_id!r}")
    count = len(tiles)
    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    label_height = 20
    from PIL import ImageDraw
    from PIL import ImageFont

    font = ImageFont.load_default()
    label_width = max(ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), label, font=font)[2]
                      for label in labels) + 6
    content_width = max(tile.width for tile in tiles)
    content_height = max(tile.height for tile in tiles)
    cell_width = max(content_width, label_width)
    cell_height = content_height + label_height

    while True:
        deadline.check("contact_sheet")
        sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), color=(0, 0, 0))
        draw = ImageDraw.Draw(sheet)
        for index, (tile, label) in enumerate(zip(tiles, labels)):
            tile = tile.convert("RGB")
            column, row = index % columns, index // columns
            cell_x, cell_y = column * cell_width, row * cell_height
            image_x = cell_x + (cell_width - tile.width) // 2
            image_y = cell_y + (content_height - tile.height) // 2
            sheet.paste(tile, (image_x, image_y))
            bounds = draw.textbbox((0, 0), label, font=font)
            text_width = bounds[2] - bounds[0]
            draw.text((cell_x + (cell_width - text_width) // 2, cell_y + content_height + 2),
                      label, fill=(255, 255, 255), font=font)
        buffer = io.BytesIO()
        try:
            sheet.save(buffer, format="PNG")
        except (OSError, ValueError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"could not encode contact sheet for output {output_id!r}: {exc}", cause=exc) from exc
        result = buffer.getvalue()
        deadline.check("contact_sheet")
        if max(sheet.size) > _CONTACT_SHEET_LONG_EDGE:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"contact sheet for output {output_id!r} exceeds {_CONTACT_SHEET_LONG_EDGE} pixels")
        if len(result) <= _CONTACT_SHEET_MAX_BYTES:
            return result
        resized = False
        for tile in tiles:
            if max(tile.size) > 1:
                new_size = (max(1, math.floor(tile.width * 0.8)), max(1, math.floor(tile.height * 0.8)))
                if new_size != tile.size:
                    tile.thumbnail(new_size, Image.Resampling.LANCZOS)
                    resized = True
        if not resized:
            raise JobError(
                JobErrorKind.CORRUPT_ARTIFACT,
                f"contact sheet for output {output_id!r} cannot be encoded within {_CONTACT_SHEET_MAX_BYTES} bytes",
            )
        content_width = max(tile.width for tile in tiles)
        content_height = max(tile.height for tile in tiles)
        cell_width = max(content_width, label_width)
        cell_height = content_height + label_height


def _timestamp(value: float) -> str:
    minutes = int(value // 60)
    seconds = value - minutes * 60
    return f"{minutes:02d}:{seconds:04.1f}"


def _ffprobe_executable(ffmpeg_exe: str | Path, ffprobe_exe: str | Path | None) -> str:
    if ffprobe_exe is not None:
        return str(ffprobe_exe)
    ffmpeg = Path(ffmpeg_exe)
    resolved = shutil.which(str(ffmpeg)) if not ffmpeg.is_file() else str(ffmpeg.resolve())
    if resolved:
        ffmpeg_path = Path(resolved)
        sibling = ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix)
        if sibling.is_file():
            return str(sibling)
        if ffmpeg.is_absolute() or ffmpeg.parent != Path("."):
            raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                           f"ffprobe beside configured FFmpeg is unavailable: {sibling}")
    found = shutil.which("ffprobe")
    if found is None:
        raise JobError(JobErrorKind.WORKER_UNREACHABLE, "ffprobe was not found beside FFmpeg or on PATH")
    return found


def _probe_media(
    source: Path,
    *,
    demuxer: str,
    ffmpeg_exe: str | Path,
    ffprobe_exe: str | Path | None,
    deadline: _Deadline,
    require_video: bool,
) -> _MediaProbe:
    executable = _ffprobe_executable(ffmpeg_exe, ffprobe_exe)
    command = [
        executable, "-hide_banner", "-v", "error", "-protocol_whitelist", "file",
        "-f", demuxer,
        "-show_entries",
        ("format=format_name,duration,bit_rate:stream=index,codec_type,codec_name,width,height,pix_fmt,"
         "avg_frame_rate,r_frame_rate,duration,duration_ts,time_base,start_time,sample_rate,channels,nb_frames:"
         "stream_disposition=attached_pic"),
        "-of", "json", str(source),
    ]
    external_track_skipped = False

    def detect_skipped_track(line: str) -> None:
        nonlocal external_track_skipped
        if re.search(r"\bskip(?:ped|ping)?\s+(?:the\s+)?external\s+track\b", line, re.IGNORECASE):
            external_track_skipped = True

    result = deadline.run(
        command,
        operation=f"ffprobe for {source.name}",
        stderr_line_parser=detect_skipped_track,
    )
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
        format_data = payload["format"]
        streams = tuple(payload["streams"])
        duration_value = format_data.get("duration")
        if duration_value in (None, "N/A"):
            candidates = [row.get("duration") for row in streams if row.get("codec_type") in {"video", "audio"}]
            duration_value = next((value for value in candidates if value not in (None, "N/A")), None)
        duration = float(duration_value)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be positive and finite")
        bitrate_raw = format_data.get("bit_rate")
        bitrate = int(bitrate_raw) if bitrate_raw not in (None, "N/A") else None
        probe = _MediaProbe(
            format_name=str(format_data.get("format_name", "unknown")),
            duration_sec=duration,
            bitrate_bps=bitrate,
            streams=streams,
            external_track_skipped=external_track_skipped,
        )
        if probe.external_track_skipped:
            raise ValueError("ffprobe reported a skipped external track")
        if require_video and probe.video is None:
            raise ValueError("no video stream was found")
        return probe
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"ffprobe returned invalid media metadata for {source.name}: {exc}", cause=exc) from exc


def _container_metrics(probe: _MediaProbe) -> dict[str, Any]:
    return {
        "format": probe.format_name,
        "duration_sec": probe.duration_sec,
        "overall_bitrate_bps": probe.bitrate_bps,
    }


def _measure_video(
    source: Path,
    *,
    output_id: str,
    demuxer: str,
    ffmpeg_exe: str | Path,
    ffprobe_exe: str | Path | None,
    deadline: _Deadline,
    video: dict[str, Any],
    width: int,
    height: int,
    fps: float,
    duration: float,
    sample_times: list[float],
) -> dict[str, Any]:
    stream_index = _positive_int(video.get("index"), "video stream index", allow_zero=True)
    sample_rows = []
    for at_sec in sample_times:
        packet_time, previous_time = _packet_presentation_time_at_or_before(
            source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe, ffprobe_exe=ffprobe_exe,
            stream_specifier=str(stream_index), at_sec=at_sec, deadline=deadline,
        )
        stats = _sample_signalstats(
            source, output_id=output_id, stream_index=stream_index, demuxer=demuxer,
            ffmpeg_exe=ffmpeg_exe, packet_time=packet_time, previous_time=previous_time,
            deadline=deadline,
        )
        edge = _sample_edge_energy(
            source, output_id=output_id, stream_index=stream_index, demuxer=demuxer,
            ffmpeg_exe=ffmpeg_exe, packet_time=packet_time, deadline=deadline,
        )
        sample_rows.append({
            "time_sec": at_sec,
            "luma_mean_0_255": stats["YAVG"],
            "luma_min_0_255": stats["YMIN"],
            "luma_max_0_255": stats["YMAX"],
            "temporal_luma_difference_0_255": stats["YDIF"],
            "saturation_mean_0_255": stats["SATAVG"],
            "edge_energy_mean_0_255": edge,
        })
    crop = _crop_estimate(
        source, output_id=output_id, stream_index=stream_index, demuxer=demuxer,
        ffmpeg_exe=ffmpeg_exe, width=width, height=height, deadline=deadline,
    )
    scene_times = _scene_changes(
        source, stream_index=stream_index, demuxer=demuxer,
        ffmpeg_exe=ffmpeg_exe, deadline=deadline,
    )
    return {
        "codec": str(video.get("codec_name", "unknown")),
        "width_px": width,
        "height_px": height,
        "fps_frames_per_sec": fps,
        "source_pixel_format": str(video.get("pix_fmt", "unknown")),
        "samples": sample_rows,
        "letterbox_determined": crop is not None,
        "letterbox_pillarbox_estimate": crop,
        "scene_change_times_sec_threshold_0_3": scene_times,
    }


def _ffmpeg_input(
    source: Path,
    *,
    demuxer: str,
    ffmpeg_exe: str | Path,
    stream_index: int,
    at_sec: float | str | None = None,
    duration_sec: float | None = None,
) -> list[str]:
    command = [
        str(ffmpeg_exe), "-hide_banner", "-nostdin", "-loglevel", "info",
        "-protocol_whitelist", "file", "-f", demuxer,
    ]
    if at_sec is not None:
        command.extend(("-ss", at_sec if isinstance(at_sec, str) else f"{at_sec:.6f}"))
    if duration_sec is not None:
        command.extend(("-t", f"{duration_sec:.6f}"))
    command.extend(("-i", str(source), "-map", f"0:{stream_index}"))
    return command


def _sample_signalstats(
    source: Path,
    *,
    output_id: str,
    stream_index: int,
    demuxer: str,
    ffmpeg_exe: str | Path,
    packet_time: str,
    previous_time: str | None,
    deadline: _Deadline,
) -> dict[str, float | None]:
    # YDIF compares a frame with the one before it, so decode the previous frame as
    # well and report the second (the sampled one). The first frame has no previous.
    frames = 2 if previous_time is not None else 1
    command = _ffmpeg_input(
        source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe,
        stream_index=stream_index, at_sec=previous_time or packet_time,
    )
    command.extend((
        "-vf", f"select='lt(n\\,{frames})',format=yuv420p,signalstats,metadata=print:file=-",
        "-frames:v", str(frames),
        "-f", "null", "-",
    ))
    result = deadline.run(command, operation=f"FFmpeg signalstats for output {output_id!r}")
    stats = _parse_signalstats(result.stdout + result.stderr, output_id)
    if previous_time is None:
        stats["YDIF"] = None
    return stats


def _sample_edge_energy(
    source: Path,
    *,
    output_id: str,
    stream_index: int,
    demuxer: str,
    ffmpeg_exe: str | Path,
    packet_time: str,
    deadline: _Deadline,
) -> float:
    command = _ffmpeg_input(
        source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe,
        stream_index=stream_index, at_sec=packet_time,
    )
    command.extend((
        "-vf", "select='eq(n\\,0)',format=yuv420p,edgedetect,signalstats,metadata=print:file=-",
        "-frames:v", "1",
        "-f", "null", "-",
    ))
    result = deadline.run(command, operation=f"FFmpeg edgedetect for output {output_id!r}")
    stats = _parse_signalstats(result.stdout + result.stderr, output_id)
    value = stats.get("YAVG")
    if value is None:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"FFmpeg edgedetect returned no edge mean for output {output_id!r}")
    return value


def _packet_presentation_time_at_or_before(
    source: Path,
    *,
    demuxer: str,
    ffmpeg_exe: str | Path,
    ffprobe_exe: str | Path | None,
    stream_specifier: str,
    at_sec: float,
    deadline: _Deadline,
) -> tuple[str, str | None]:
    """Find the latest packet PTS at or before a sample, and the one before it.

    The earlier one lets signalstats measure YDIF (change from the previous frame) at
    the sampled frame. Neither lookup decodes any frames.
    """
    command = [
        _ffprobe_executable(ffmpeg_exe, ffprobe_exe),
        "-hide_banner", "-v", "error", "-protocol_whitelist", "file",
        "-probesize", "32", "-analyzeduration", "0", "-f", demuxer,
        "-select_streams", stream_specifier,
        "-show_packets", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
        str(source),
    ]
    result = deadline.run(command, operation=f"ffprobe packet timestamps for {source.name}")
    best_time: str | None = None
    best_sec: float | None = None
    previous_time: str | None = None
    previous_sec: float | None = None
    stdout = result.stdout
    start = 0
    # Packet PTS can be out of order when the codec uses B-frames. Scan the
    # packet timestamps and compare all of them instead of stopping at the first
    # timestamp after the sample.
    while start < len(stdout):
        end = stdout.find(b"\n", start)
        if end < 0:
            end = len(stdout)
        raw_time = stdout[start:end].strip()
        start = end + 1
        if not raw_time or raw_time == b"N/A":
            continue
        try:
            packet_sec = float(raw_time)
        except ValueError as exc:
            raise JobError(
                JobErrorKind.CORRUPT_ARTIFACT,
                f"ffprobe returned invalid packet presentation time for {source.name}: {raw_time!r}",
                cause=exc,
            ) from exc
        if not math.isfinite(packet_sec):
            raise JobError(
                JobErrorKind.CORRUPT_ARTIFACT,
                f"ffprobe returned non-finite packet presentation time for {source.name}",
            )
        if packet_sec > at_sec:
            continue
        if best_sec is None or packet_sec > best_sec:
            previous_sec, previous_time = best_sec, best_time
            best_sec, best_time = packet_sec, raw_time.decode("ascii")
        elif packet_sec < best_sec and (previous_sec is None or packet_sec > previous_sec):
            previous_sec, previous_time = packet_sec, raw_time.decode("ascii")
    if best_time is None:
        raise PerceptionInvalid(f"sample time {at_sec:.6f} seconds has no frame at or before it")
    return best_time, previous_time


def _parse_signalstats(stderr: bytes, output_id: str) -> dict[str, float | None]:
    raw = stderr.decode("utf-8", errors="replace")
    found: dict[str, float | None] = {}
    for key in ("YAVG", "YMIN", "YMAX", "YDIF", "SATAVG"):
        matches = re.findall(rf"lavfi\.signalstats\.{key}=([^\s]+)", raw)
        if not matches:
            if key == "YDIF":
                found[key] = None
                continue
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"FFmpeg signalstats omitted {key} for output {output_id!r}")
        try:
            value = float(matches[-1])
        except ValueError as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"FFmpeg signalstats returned invalid {key} for output {output_id!r}: {matches[-1]!r}",
                           cause=exc) from exc
        found[key] = value if math.isfinite(value) else None
    return found


def _crop_estimate(
    source: Path,
    *,
    output_id: str,
    stream_index: int,
    demuxer: str,
    ffmpeg_exe: str | Path,
    width: int,
    height: int,
    deadline: _Deadline,
) -> dict[str, Any] | None:
    command = _ffmpeg_input(source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe, stream_index=stream_index)
    command.extend(("-vf", "cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"))
    parsed: Counter[tuple[int, int, int, int]] = Counter()

    def collect_crop(line: str) -> None:
        for values in re.findall(r"crop=(\d+):(\d+):(-?\d+):(-?\d+)", line):
            parsed[tuple(map(int, values))] += 1

    deadline.run(
        command,
        operation=f"FFmpeg cropdetect for output {output_id!r}",
        stderr_line_parser=collect_crop,
    )
    if not parsed:
        return None
    crop_width, crop_height, x, y = parsed.most_common(1)[0][0]
    right = max(0, width - x - crop_width)
    bottom = max(0, height - y - crop_height)
    fraction = crop_width * crop_height / (width * height)
    return {
        "active_width_px": crop_width,
        "active_height_px": crop_height,
        "active_x_px": x,
        "active_y_px": y,
        "left_bar_estimate_px": max(0, x),
        "right_bar_estimate_px": right,
        "top_bar_estimate_px": max(0, y),
        "bottom_bar_estimate_px": bottom,
        "active_area_fraction": fraction,
    }


def _scene_changes(
    source: Path,
    *,
    stream_index: int,
    demuxer: str,
    ffmpeg_exe: str | Path,
    deadline: _Deadline,
) -> list[float]:
    command = _ffmpeg_input(source, demuxer=demuxer, ffmpeg_exe=ffmpeg_exe, stream_index=stream_index)
    command.extend((
        "-vf", f"select='gt(scene\\,{_SCENE_THRESHOLD:.1f})',showinfo", "-an", "-f", "null", "-",
    ))
    scene_times: list[float] = []

    def collect_scene(line: str) -> None:
        match = re.search(r"\bpts_time:([0-9]+(?:\.[0-9]+)?)", line)
        if match:
            scene_times.append(float(match.group(1)))

    deadline.run(command, operation="FFmpeg scene-change detection", stderr_line_parser=collect_scene)
    return scene_times


def _measure_audio(
    source: Path,
    *,
    demuxer: str,
    ffmpeg_exe: str | Path,
    deadline: _Deadline,
    audio: dict[str, Any],
    stream_duration: float,
) -> dict[str, Any]:
    loudness_tokens: dict[str, str] = {}
    silence_starts: list[float] = []
    silence_ends: list[float] = []
    coverage_total = 0.0
    coverage_start: float | None = None
    coverage_end: float | None = None
    pending_pts: float | None = None
    pending_rate: int | None = None
    decoded_frames = 0
    skipped_external_track = False

    def collect_audio(line: str) -> None:
        nonlocal coverage_total, coverage_start, coverage_end
        nonlocal pending_pts, pending_rate, decoded_frames, skipped_external_track
        if re.search(r"\bskip(?:ped|ping)?\s+(?:the\s+)?external\s+track\b", line, re.IGNORECASE):
            skipped_external_track = True

        patterns = {
            "integrated": r"\bI:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)|[-+]?inf(?:inity)?)\s*LUFS",
            "loudness_range": r"\bLRA:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)|[-+]?inf(?:inity)?)\s*LU",
            "true_peak": r"\bPeak:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)|[-+]?inf(?:inity)?)\s*dB(?:TP|FS)",
        }
        for name, pattern in patterns.items():
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                loudness_tokens[name] = match.group(1)

        start = re.search(r"silence_start:\s*([+-]?[0-9.]+)", line)
        end = re.search(r"silence_end:\s*([+-]?[0-9.]+)", line)
        if start:
            silence_starts.append(float(start.group(1)))
        if end:
            silence_ends.append(float(end.group(1)))

        pts = re.search(r"\bpts_time:([+-]?(?:\d+(?:\.\d*)?|\.\d+))", line)
        if pts:
            pending_pts = float(pts.group(1))
        rate = re.search(r"\brate:(\d+)", line)
        if rate:
            pending_rate = int(rate.group(1))
        samples = re.search(r"\bnb_samples:(\d+)", line)
        if samples and pending_pts is not None and pending_rate is not None:
            sample_count = int(samples.group(1))
            if sample_count > 0:
                frame_start = pending_pts
                frame_end = frame_start + sample_count / pending_rate
                decoded_frames += 1
                if coverage_start is None:
                    coverage_start, coverage_end = frame_start, frame_end
                elif frame_start <= (coverage_end or frame_start) + 1e-6:
                    coverage_end = max(coverage_end or frame_end, frame_end)
                else:
                    coverage_total += max(0.0, (coverage_end or frame_start) - coverage_start)
                    coverage_start, coverage_end = frame_start, frame_end
            pending_pts = None
            pending_rate = None

    command = [
        str(ffmpeg_exe), "-hide_banner", "-nostdin", "-loglevel", "info",
        "-protocol_whitelist", "file", "-f", demuxer, "-i", str(source),
        "-map", f"0:{audio['index']}", "-vn", "-af",
        ("ashowinfo,ebur128=peak=true,"
         f"silencedetect=noise={_SILENCE_THRESHOLD_DB}dB:duration={_SILENCE_MIN_SECONDS:.6f}"),
        "-f", "null", "-",
    ]
    deadline.run(
        command,
        operation=f"FFmpeg loudness analysis for {source.name}",
        stderr_line_parser=collect_audio,
    )
    if skipped_external_track:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"FFmpeg reported a skipped external track while decoding {source.name}")
    if coverage_start is not None and coverage_end is not None:
        coverage_total += max(0.0, coverage_end - coverage_start)
    if decoded_frames == 0:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"FFmpeg decoded no audio samples from {source.name}")
    if stream_duration <= 0 or coverage_total / stream_duration < 0.9:
        raise JobError(
            JobErrorKind.CORRUPT_ARTIFACT,
            f"FFmpeg decoded only {coverage_total:.6f} seconds of the {stream_duration:.6f}-second audio stream in {source.name}",
        )
    integrated = _parse_metric_token(loudness_tokens.get("integrated"), "integrated loudness")
    loudness_range = _parse_metric_token(loudness_tokens.get("loudness_range"), "loudness range")
    true_peak = _parse_metric_token(loudness_tokens.get("true_peak"), "true peak")
    intervals = _parse_silence_values(silence_starts, silence_ends)
    return {
        "codec": str(audio.get("codec_name", "unknown")),
        "sample_rate_hz": _optional_positive_int(audio.get("sample_rate")),
        "channels": _optional_positive_int(audio.get("channels")),
        "integrated_loudness_lufs": integrated,
        "loudness_range_lu": loudness_range,
        "true_peak_dbtp": true_peak,
        "silent": integrated is None or loudness_range is None or true_peak is None,
        "silence_intervals": intervals,
    }


def _parse_metric_token(token: str | None, label: str) -> float | None:
    if token is None:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"FFmpeg ebur128 output omitted {label}")
    if token.lower() in {"-inf", "-infinity"}:
        return None
    try:
        value = float(token)
    except ValueError as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"FFmpeg ebur128 returned invalid {label}: {token!r}", cause=exc) from exc
    if not math.isfinite(value):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"FFmpeg ebur128 returned non-finite {label}: {token!r}")
    return value


def _parse_silence_values(starts: list[float], ends: list[float]) -> list[dict[str, float | None]]:
    intervals: list[dict[str, float | None]] = []
    end_index = 0
    for start in starts:
        while end_index < len(ends) and ends[end_index] < start:
            end_index += 1
        end = ends[end_index] if end_index < len(ends) else None
        if end is not None:
            end_index += 1
        intervals.append({
            "start_sec": start,
            "end_sec": end,
            "duration_sec": None if end is None else max(0.0, end - start),
        })
    return intervals


def _audio_stream_duration(audio: dict[str, Any], container_duration: float) -> float:
    raw_duration = audio.get("duration")
    if raw_duration not in (None, "N/A"):
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"ffprobe returned invalid audio stream duration {raw_duration!r}", cause=exc) from exc
        if not math.isfinite(duration) or duration <= 0:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"ffprobe returned non-positive audio stream duration {raw_duration!r}")
        return duration
    duration_ts, time_base = audio.get("duration_ts"), audio.get("time_base")
    has_duration_ts = duration_ts not in (None, "N/A")
    has_time_base = time_base not in (None, "N/A")
    if has_duration_ts != has_time_base:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "ffprobe returned incomplete audio stream duration metadata")
    if has_duration_ts and has_time_base:
        try:
            duration = int(duration_ts) * float(Fraction(str(time_base)))
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           "ffprobe returned invalid audio stream duration metadata", cause=exc) from exc
        if not math.isfinite(duration) or duration <= 0:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           "ffprobe returned non-positive audio stream duration metadata")
        return duration
    if not math.isfinite(container_duration) or container_duration <= 0:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       "ffprobe returned no positive finite audio stream or container duration")
    return container_duration


def _image_metrics(data: bytes, output_id: str) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "WEBP", "BMP", "TIFF")) as source:
            if source.width * source.height > _MAX_SOURCE_PIXELS:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                               f"image output {output_id!r} exceeds the {_MAX_SOURCE_PIXELS}-pixel metrics limit")
            source.load()
            mode = source.mode
            image = source.convert("RGB")
    except JobError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"image output {output_id!r} cannot be decoded for metrics: {exc}", cause=exc) from exc
    luma = image.convert("L")
    histogram = luma.histogram()
    pixels = image.width * image.height
    luma_mean = sum(index * count for index, count in enumerate(histogram)) / pixels
    saturation = image.convert("HSV").getchannel("S")
    saturation_mean = sum(index * count for index, count in enumerate(saturation.histogram())) / pixels
    edges = luma.filter(ImageFilter.FIND_EDGES)
    edge_mean = sum(index * count for index, count in enumerate(edges.histogram())) / pixels
    return {
        "width_px": image.width,
        "height_px": image.height,
        "mode": mode,
        "luma_mean_0_255": luma_mean,
        "luma_min_0_255": luma.getextrema()[0],
        "luma_max_0_255": luma.getextrema()[1],
        "saturation_mean_0_255": saturation_mean,
        "edge_energy_mean_0_255": edge_mean,
    }


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"ffprobe returned invalid {label}: {value!r}", cause=exc) from exc
    if parsed < (0 if allow_zero else 1):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"ffprobe returned non-positive {label}: {parsed}")
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, "N/A"):
        return None
    return _positive_int(value, "audio stream value")


def _parse_rate(primary: Any, fallback: Any) -> float:
    value = primary if primary not in (None, "N/A", "0/0", "0:0") else fallback
    try:
        rate = float(Fraction(str(value).replace(":", "/")))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"ffprobe returned invalid frame rate: {value!r}", cause=exc) from exc
    if not math.isfinite(rate) or rate <= 0:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"ffprobe returned non-positive frame rate: {value!r}")
    return rate


def _cut_shot_versions(
    store: ProjectStore,
    output: EpisodeOutput,
    *,
    deadline: _Deadline,
) -> tuple[dict[str, int], str]:
    if output.credit_manifest_path is None:
        return {}, "current"
    try:
        manifest_bytes = store._root.read_bytes(output.credit_manifest_path)
        deadline.check("rubric_sample manifest verification")
    except (ContainmentError, OSError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"cannot read cut credit manifest {output.credit_manifest_path!r}: {exc}", cause=exc) from exc
    if hashlib.sha256(manifest_bytes).hexdigest() != output.credit_manifest_sha256:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"cut credit manifest checksum mismatch: {output.credit_manifest_path}")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != _CREDIT_MANIFEST_SCHEMA:
            raise ValueError("manifest must be a schema-1 JSON object")
        rows = manifest["shot_versions"]
        if not isinstance(rows, list):
            raise ValueError("shot_versions must be an array")
        versions: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("shot_versions contains a non-object row")
            shot_id, version = row.get("shot_id"), row.get("shot_version")
            if not isinstance(shot_id, str) or not shot_id or type(version) is not int or version < 1:
                raise ValueError("shot_versions rows need shot_id and positive shot_version")
            if shot_id in versions:
                raise ValueError(f"shot_versions repeats {shot_id!r}")
            versions[shot_id] = version
        return versions, "credit_manifest"
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                       f"cut credit manifest has invalid shot_versions: {exc}", cause=exc) from exc


def _sequence_times(moment: float, duration: float) -> list[float]:
    if duration >= 2.0:
        center = min(max(moment, 1.0), duration - 1.0)
        first = center - 1.0
        candidates = [first + index * 0.5 for index in range(5)]
    else:
        candidates = [moment + (index - 2) * 0.5 for index in range(5)]
    last_in_narrative = max(0.0, duration - 0.000001)
    return [round(min(max(value, 0.0), last_in_narrative), 6) for value in candidates]


def _shot_at(entries: list[Any], time_sec: float) -> str | None:
    for entry in entries:
        if entry.start_sec <= time_sec < entry.start_sec + entry.duration_sec:
            return entry.ref
    return None
