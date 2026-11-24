"""FFmpeg drawtext measurement and frame-fit checks for recorded and playback overlays."""

from __future__ import annotations

import ast
import calendar
import functools
import math
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from PIL import Image, ImageFont

from orchestrator.adapters.postprofile.profile import (
    PostProfileSpec,
    serialize_drawtext_literal_filter,
)

# At least one pixel of clearance protects the measured glyph bounds at the frame edge.
MEASURE_MARGIN_PX = 1

# Separate, replaceable process seam. Capturing Popen here prevents adapter tests that
# patch subprocess.Popen from intercepting this preflight render.
FFMPEG_POPEN: Callable[..., Any] = subprocess.Popen
FFMPEG_TIMEOUT_SEC = 60
MAX_TEXTS_PER_RENDER = 48
_PILLOW_SCALE = 64
_CLOCK_TOKEN = re.compile(r"%([YymdHMSpIbaj])")
_POSITION_NAMES = {"w", "h", "text_w", "text_h"}


class OverlayFitError(ValueError):
    """An in-recording drawtext box cannot fit or cannot be measured."""


@dataclass(frozen=True)
class _Overlay:
    element: str
    text_candidates: tuple[str, ...]
    font_size: int
    x: int | str
    y: int | str
    camera_id: str | None = None
    clock_format: str | None = None
    clock_epoch: int = 0


@dataclass(frozen=True)
class _RenderText:
    text: str
    fontsize: int
    element: str
    camera_id: str | None = None
    clock_format: str | None = None


@dataclass(frozen=True)
class _PositionedText:
    item: _RenderText
    x: int
    y: int
    row_top: int
    row_bottom: int


@lru_cache(maxsize=128)
def _scaled_font(font_path: str, fontsize: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path, fontsize * _PILLOW_SCALE)


def _font_error(font_path: Path, detail: str, cause: BaseException | None = None) -> OverlayFitError:
    error = OverlayFitError(f"cannot measure drawtext with font {str(font_path)!r}: {detail}")
    if cause is not None:
        error.__cause__ = cause
    return error


def _pillow_width(font_path: Path, fontsize: int, text: str) -> float:
    try:
        font = _scaled_font(str(font_path.resolve()), fontsize)
        return float(font.getlength(text)) / _PILLOW_SCALE
    except (OSError, ValueError) as exc:
        raise _font_error(font_path, f"Pillow could not load it: {exc}", exc) from exc


def measure_text_box(font_path: Path | str, fontsize: int, text: str) -> tuple[int, int]:
    """Measure one literal drawtext string with the FFmpeg renderer used for fit checks."""
    path = Path(font_path)
    if not isinstance(text, str):
        raise TypeError("drawtext text must be a string")
    if type(fontsize) is not int or fontsize < 1:
        raise ValueError("drawtext fontsize must be a positive integer")
    if not path.is_file():
        raise _font_error(path, "font file is missing")
    rendered = _measure_text_batch(
        path,
        [_RenderText(text=text, fontsize=fontsize, element="literal")],
        ffmpeg_exe="ffmpeg",
        font_filter_path=str(path.resolve()),
        working_directory=None,
    )
    return rendered[0]


def has_in_recording_overlays(profile: PostProfileSpec) -> bool:
    """Whether the composed spec burns any drawtext into the recording."""
    overlay = profile.overlay
    for stage in profile.identity.stages:
        if stage.filter != "drawtext" or stage.composite_stage == "on_playback":
            continue
        element = stage.params.get("element", "clock")
        if element == "clock" and overlay is not None and overlay.placement == "on_playback":
            continue
        return True
    return False


def has_on_playback_overlays(profile: PostProfileSpec) -> bool:
    """Whether the composed spec places its clock in the playback/cut layer."""
    return any(
        stage.filter == "drawtext"
        and stage.params.get("element", "clock") == "clock"
        and stage.composite_stage == "on_playback"
        and profile.overlay is not None
        and profile.overlay.placement == "on_playback"
        for stage in profile.identity.stages
    )


def validate_in_recording_overlays(
    profile: PostProfileSpec,
    font_path: Path | str,
    *,
    font_filter_path: str | None = None,
    working_directory: Path | str | None = None,
    ffmpeg_exe: Path | str = "ffmpeg",
    duration_sec: float | None = None,
    clock_offset_seconds: float = 0.0,
    _placement: str = "in_recording",
    _target_resolution: tuple[int, int] | None = None,
) -> None:
    """Refuse an overlay that leaves its storage or explicitly supplied cut frame."""
    has_overlays = (
        has_in_recording_overlays(profile)
        if _placement == "in_recording"
        else has_on_playback_overlays(profile)
    )
    if not has_overlays:
        return
    overlay = profile.overlay
    if overlay is None:
        raise OverlayFitError(f"{_placement} drawtext has no PostProfile overlay settings")
    path = Path(font_path)
    if not path.is_file():
        raise _font_error(path, "font file is missing")

    width, height = _target_resolution or profile.storage_resolution
    overlays: list[_Overlay] = []
    for stage in profile.identity.stages:
        if stage.filter != "drawtext" or (stage.composite_stage or "in_recording") != _placement:
            continue
        element = stage.params.get("element", "clock")
        params = stage.params
        if element == "clock":
            if overlay.placement != _placement:
                continue
            camera_id = overlay.camera_id
            clock_format = overlay.clock_format
            candidates = _clock_candidates(
                font_path=path,
                fontsize=overlay.fontsize,
                camera_id=camera_id,
                clock_format=clock_format,
                clock_epoch=overlay.clock_epoch,
                duration_sec=duration_sec,
                output_fps=profile.output_fps,
                artifact_schedule=profile.artifact_schedule,
                clock_offset_seconds=clock_offset_seconds,
            )
            overlays.append(_Overlay(
                element="clock",
                text_candidates=tuple(candidates),
                font_size=params.get("fontsize", overlay.fontsize),
                x=params.get("x", overlay.x),
                y=params.get("y", overlay.y),
                camera_id=camera_id,
                clock_format=clock_format,
                clock_epoch=overlay.clock_epoch,
            ))
        elif element == "battery_rec":
            indicator = overlay.indicator
            if indicator is None:
                raise OverlayFitError("battery REC overlay has no indicator settings")
            overlays.append(_Overlay(
                element="battery_rec",
                text_candidates=("REC [||||]",),
                font_size=indicator.fontsize,
                x=indicator.x,
                y=indicator.y,
            ))
        else:
            continue  # validate_profile rejects other drawtext elements.

    # Render every distinct displayed string: width-only pruning is unsafe when x/y
    # expressions can depend on text_w or text_h, and glyph height can vary by string.
    requests: list[tuple[int, _RenderText]] = []
    for overlay_index, item in enumerate(overlays):
        selected = _select_ffmpeg_candidates(path, item.font_size, item.text_candidates)
        requests.extend(
            (overlay_index, _RenderText(
                text=text,
                fontsize=item.font_size,
                element=item.element,
                camera_id=item.camera_id,
                clock_format=item.clock_format,
            ))
            for text in selected
        )

    boxes = _measure_text_batch(
        path,
        [request for _index, request in requests],
        ffmpeg_exe=ffmpeg_exe,
        font_filter_path=font_filter_path or str(path.resolve()),
        working_directory=working_directory,
    )

    for overlay_index, item in enumerate(overlays):
        candidates = [
            (request, box)
            for (request_index, request), box in zip(requests, boxes)
            if request_index == overlay_index
        ]
        if not candidates:
            raise _font_error(path, f"FFmpeg returned no measurement for {item.element}")
        failure: tuple[_RenderText, tuple[int, int], float | None, float | None, BaseException | None] | None = None
        for request, (box_width, box_height) in candidates:
            try:
                position_x = _position(
                    item.x, w=width, h=height, text_w=box_width, text_h=box_height,
                )
                position_y = _position(
                    item.y, w=width, h=height, text_w=box_width, text_h=box_height,
                )
                position_error = None
            except (OverlayFitError, ArithmeticError, SyntaxError, ValueError) as exc:
                position_x = position_y = None
                position_error = exc
            fits = (
                position_error is None
                and position_x is not None
                and position_y is not None
                and position_x >= 0
                and position_y >= 0
                and position_x + box_width + MEASURE_MARGIN_PX <= width
                and position_y + box_height + MEASURE_MARGIN_PX <= height
            )
            if not fits:
                if failure is None:
                    failure = (
                        request, (box_width, box_height),
                        position_x, position_y, position_error,
                    )
        if failure is None:
            continue

        largest = _largest_fitting_size(
            path,
            item,
            width,
            height,
            ffmpeg_exe=ffmpeg_exe,
            font_filter_path=font_filter_path or str(path.resolve()),
            working_directory=working_directory,
            duration_sec=duration_sec,
            output_fps=profile.output_fps,
            artifact_schedule=profile.artifact_schedule,
            clock_offset_seconds=clock_offset_seconds,
        )
        request, (box_width, box_height), position_x, position_y, position_error = failure
        position = (
            f"({position_x:g}, {position_y:g})"
            if position_error is None and position_x is not None and position_y is not None
            else f"({item.x!r}, {item.y!r})"
        )
        details = (
            f" at position {position}"
            if position_error is None
            else f" at position {position}; invalid position expression ({position_error})"
        )
        raise OverlayFitError(
            f"{item.element} overlay measured {box_width}x{box_height} px in frame "
            f"{width}x{height}{details} for {request.text!r}; largest fitting integer font size: "
            f"{_largest_text(largest)} (searched 6 through 96). Lower "
            "overlay_model.fontsize, shorten the text or format, or move it."
        )


def validate_on_playback_overlays(
    profile: PostProfileSpec,
    font_path: Path | str,
    *,
    target_resolution: tuple[int, int],
    font_filter_path: str | None = None,
    working_directory: Path | str | None = None,
    ffmpeg_exe: Path | str = "ffmpeg",
    duration_sec: float | None = None,
    clock_offset_seconds: float = 0.0,
) -> None:
    """Measure playback clocks at the cut resolution with FFmpeg before assembly."""
    validate_in_recording_overlays(
        profile,
        font_path,
        font_filter_path=font_filter_path,
        working_directory=working_directory,
        ffmpeg_exe=ffmpeg_exe,
        duration_sec=duration_sec,
        clock_offset_seconds=clock_offset_seconds,
        _placement="on_playback",
        _target_resolution=target_resolution,
    )


def _largest_text(value: int | None) -> str:
    return str(value) if value is not None else "none (no size from 6 through 96 fits)"


def _largest_fitting_size(
    font_path: Path,
    overlay: _Overlay,
    width: int,
    height: int,
    *,
    ffmpeg_exe: Path | str,
    font_filter_path: str,
    working_directory: Path | str | None,
    duration_sec: float | None,
    output_fps: int,
    artifact_schedule: Sequence[Any],
    clock_offset_seconds: float,
) -> int | None:
    """Return the largest size from 6 through 96 at which every candidate fits.

    A position that references ``text_w``/``text_h`` can make fit non-monotonic in size
    (``x=2*text_w-K`` fails both small and large), so every size is measured. Otherwise
    the position does not depend on the text and boxes grow with the font size, so fit is
    monotonic up to hinting jitter: a binary search finds the boundary, and the sweep is
    used instead when the boundary looks unreliable (see below).
    """
    sizes_fit = functools.partial(
        _sizes_fit, font_path, overlay, width, height,
        ffmpeg_exe=ffmpeg_exe, font_filter_path=font_filter_path,
        working_directory=working_directory, duration_sec=duration_sec,
        output_fps=output_fps, artifact_schedule=artifact_schedule,
        clock_offset_seconds=clock_offset_seconds,
    )
    def exhaustive() -> int | None:
        fits_by_size = sizes_fit(range(6, 97))
        return max((size for size, fits in fits_by_size.items() if fits), default=None)

    if any("text_" in str(position) for position in (overlay.x, overlay.y)):
        return exhaustive()
    # Font hinting can still jitter a box by a pixel between neighbouring sizes (seen at
    # 6-7 px), so the search falls back to the exhaustive sweep whenever its answer is
    # "none" or any of the next few sizes also fits. This only affects the advice in the
    # message: the refusal itself is decided by the exact measurement at the requested size.
    low, high = 6, 96
    if not sizes_fit([low])[low]:
        return exhaustive()
    while low < high:  # invariant: low fits
        middle = (low + high + 1) // 2
        if sizes_fit([middle])[middle]:
            low = middle
        else:
            high = middle - 1
    above = [size for size in range(low + 1, min(low + 4, 96) + 1)]
    if above and any(sizes_fit(above).values()):
        return exhaustive()
    return low


def _sizes_fit(
    font_path: Path,
    overlay: _Overlay,
    width: int,
    height: int,
    sizes: Iterable[int],
    *,
    ffmpeg_exe: Path | str,
    font_filter_path: str,
    working_directory: Path | str | None,
    duration_sec: float | None,
    output_fps: int,
    artifact_schedule: Sequence[Any],
    clock_offset_seconds: float,
) -> dict[int, bool]:
    """Measure every candidate at each size (one batched FFmpeg pass) and report fit."""
    sizes = list(sizes)
    requests: list[tuple[int, _RenderText]] = []
    for fontsize in sizes:
        candidates = (
            _clock_candidates(
                font_path=font_path,
                fontsize=fontsize,
                camera_id=overlay.camera_id or "",
                clock_format=overlay.clock_format,
                clock_epoch=overlay.clock_epoch,
                duration_sec=duration_sec,
                output_fps=output_fps,
                artifact_schedule=artifact_schedule,
                clock_offset_seconds=clock_offset_seconds,
            )
            if overlay.element == "clock"
            else list(overlay.text_candidates)
        )
        selected = _select_ffmpeg_candidates(font_path, fontsize, candidates)
        texts = [
            _RenderText(
                text=text,
                fontsize=fontsize,
                element=overlay.element,
                camera_id=overlay.camera_id,
                clock_format=overlay.clock_format,
            )
            for text in selected
        ]
        requests.extend((fontsize, text) for text in texts)
    measured = _measure_text_batch(
        font_path,
        [request for _fontsize, request in requests],
        ffmpeg_exe=ffmpeg_exe,
        font_filter_path=font_filter_path,
        working_directory=working_directory,
    )
    fits_by_size = {fontsize: True for fontsize in sizes}
    for (fontsize, _request), (box_width, box_height) in zip(requests, measured):
        try:
            x = _position(overlay.x, w=width, h=height, text_w=box_width, text_h=box_height)
            y = _position(overlay.y, w=width, h=height, text_w=box_width, text_h=box_height)
        except (OverlayFitError, ArithmeticError, SyntaxError, ValueError):
            fits_by_size[fontsize] = False
            continue
        if (
            x < 0
            or y < 0
            or x + box_width + MEASURE_MARGIN_PX > width
            or y + box_height + MEASURE_MARGIN_PX > height
        ):
            fits_by_size[fontsize] = False
    return fits_by_size


def _select_ffmpeg_candidates(
    font_path: Path,
    fontsize: int,
    candidates: Sequence[str],
) -> tuple[str, ...]:
    """Return every distinct displayable string; all are measured by FFmpeg."""
    unique = tuple(dict.fromkeys(candidates))
    if not unique:
        raise OverlayFitError("drawtext clock produced no displayable strings")
    return unique


def _clock_candidates(
    *,
    font_path: Path,
    fontsize: int,
    camera_id: str,
    clock_format: str | None,
    clock_epoch: int,
    duration_sec: float | None,
    output_fps: int,
    artifact_schedule: Sequence[Any],
    clock_offset_seconds: float,
) -> list[str]:
    fmt = clock_format or "%Y-%m-%d %H:%M:%S"
    derivable = (
        duration_sec is not None
        and math.isfinite(duration_sec)
        and duration_sec >= 0
        and output_fps > 0
    )
    if not math.isfinite(clock_offset_seconds) or clock_offset_seconds < 0:
        raise OverlayFitError("clock_offset_seconds must be finite and nonnegative")
    offsets = {0}
    frame_count = 0
    if derivable:
        cumulative_drops = 0
        for event in sorted(
            artifact_schedule,
            key=lambda item: (getattr(item, "start_frame", 0), getattr(item, "type", "")),
        ):
            if getattr(event, "type", None) == "drop":
                cumulative_drops += int(event.count)
                offsets.add(cumulative_drops)
        frame_count = max(1, math.ceil(duration_sec * output_fps))

    if derivable:
        tokens = {match.group(1) for match in _CLOCK_TOKEN.finditer(fmt)}
        step = (
            1 if "S" in tokens else
            60 if "M" in tokens else
            3600 if tokens & {"H", "I", "p"} else
            86400
        )
        samples: set[int] = set()
        for offset_frames in offsets:
            frame_offset_seconds = offset_frames / output_fps + clock_offset_seconds
            first_second = math.floor(frame_offset_seconds)
            last_second = math.floor(
                (frame_count - 1) / output_fps + frame_offset_seconds
            )
            samples.update(range(first_second, last_second + 1, step))
            samples.add(last_second)
        clock_values = {_format_clock(clock_epoch + second, fmt) for second in samples}
        return [f"{camera_id}  {value}" for value in sorted(clock_values)]

    # Without a derivable display range, form a token-wise upper-bound set. Pillow
    # estimates only select candidates; FFmpeg still measures every selected sample.
    try:
        scaled = _scaled_font(str(font_path.resolve()), fontsize)
    except (OSError, ValueError) as exc:
        raise _font_error(font_path, f"Pillow could not load it: {exc}", exc) from exc
    token_matches = list(_CLOCK_TOKEN.finditer(fmt))
    options = [_clock_token_candidates(match.group(1)) for match in token_matches]
    selected_parts = [
        max(values, key=scaled.getlength) if values else match.group(0)
        for match, values in zip(token_matches, options)
    ]
    literals: list[str] = []
    offset = 0
    for match in token_matches:
        literals.append(fmt[offset:match.start()])
        offset = match.end()
    literals.append(fmt[offset:])
    baseline_parts: list[str] = []
    for index, selected in enumerate(selected_parts):
        baseline_parts.extend((literals[index], selected))
    baseline = "".join((*baseline_parts, literals[-1]))
    candidates = {f"{camera_id}  {baseline}"}
    for index, values in enumerate(options):
        for value in values:
            variant_parts: list[str] = []
            for part_index, selected in enumerate(selected_parts):
                variant_parts.extend((
                    literals[part_index], value if part_index == index else selected,
                ))
            variant_parts.append(literals[-1])
            candidates.add(f"{camera_id}  {''.join(variant_parts)}")
    return list(candidates)


def _clock_token_candidates(token: str) -> tuple[str, ...]:
    if token == "a":
        return tuple(str(day) for day in calendar.day_abbr if day)
    if token == "b":
        return tuple(str(month) for month in calendar.month_abbr[1:] if month)
    if token == "p":
        return ("AM", "PM")
    digits = {"Y": 4, "j": 3}.get(token, 2)
    return tuple(digit * digits for digit in "0123456789")


def _format_clock(timestamp: int, clock_format: str) -> str:
    try:
        value = datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise OverlayFitError(f"clock range cannot be formatted at UTC timestamp {timestamp}: {exc}") from exc

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "Y":
            return f"{value.year:04d}"
        if token == "y":
            return f"{value.year % 100:02d}"
        if token == "m":
            return f"{value.month:02d}"
        if token == "d":
            return f"{value.day:02d}"
        if token == "H":
            return f"{value.hour:02d}"
        if token == "M":
            return f"{value.minute:02d}"
        if token == "S":
            return f"{value.second:02d}"
        if token == "p":
            return "AM" if value.hour < 12 else "PM"
        if token == "I":
            return f"{(value.hour % 12) or 12:02d}"
        if token == "b":
            return calendar.month_abbr[value.month]
        if token == "a":
            return calendar.day_abbr[value.weekday()]
        if token == "j":
            return f"{value.timetuple().tm_yday:03d}"
        raise AssertionError(f"unsupported validated clock token {token!r}")

    return _CLOCK_TOKEN.sub(replace, clock_format)


def _measure_text_batch(
    font_path: Path,
    texts: Sequence[_RenderText],
    *,
    ffmpeg_exe: Path | str,
    font_filter_path: str,
    working_directory: Path | str | None,
) -> list[tuple[int, int]]:
    if not texts:
        return []
    results: list[tuple[int, int]] = []
    for offset in range(0, len(texts), MAX_TEXTS_PER_RENDER):
        batch = texts[offset:offset + MAX_TEXTS_PER_RENDER]
        results.extend(_render_batch(
            font_path,
            batch,
            ffmpeg_exe=ffmpeg_exe,
            font_filter_path=font_filter_path,
            working_directory=working_directory,
        ))
    return results


def _render_batch(
    font_path: Path,
    texts: Sequence[_RenderText],
    *,
    ffmpeg_exe: Path | str,
    font_filter_path: str,
    working_directory: Path | str | None,
) -> list[tuple[int, int]]:
    padding = 16
    positioned: list[_PositionedText] = []
    max_width = 2048
    next_top = padding
    for item in texts:
        # Pillow's same-font advance estimate sizes only the scratch canvas. The
        # generous bearing allowance prevents clipping; FFmpeg still measures every
        # candidate box used by the fit decision.
        estimated_width = _pillow_width(font_path, item.fontsize, item.text)
        max_width = max(
            max_width,
            math.ceil(estimated_width) + item.fontsize * 2 + padding * 2 + 64,
        )
        row_height = item.fontsize * 2 + 48
        draw_y = next_top + padding
        positioned.append(_PositionedText(
            item=item,
            x=padding,
            y=draw_y,
            row_top=next_top,
            row_bottom=next_top + row_height,
        ))
        next_top += row_height
    frame_width = max_width
    frame_height = next_top + padding

    draw_filters = [
        serialize_drawtext_literal_filter(
            font_path=font_filter_path,
            text=placed.item.text,
            x=placed.x,
            y=placed.y,
            fontsize=placed.item.fontsize,
            element=placed.item.element,
            camera_id=placed.item.camera_id,
            clock_format=placed.item.clock_format,
        )
        for placed in positioned
    ]
    filtergraph = ",".join(["negate", *draw_filters, "negate"])
    command = [
        str(ffmpeg_exe),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={frame_width}x{frame_height}:r=1:d=1",
        "-vf",
        filtergraph,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    process: Any | None = None
    try:
        process = FFMPEG_POPEN(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            cwd=working_directory,
        )
        stdout, stderr = process.communicate(timeout=FFMPEG_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired) and process is not None:
            try:
                process.kill()
                process.communicate()
            except OSError:
                pass
        raise _font_error(
            font_path,
            f"FFmpeg could not render the font with {str(ffmpeg_exe)!r}: {exc}",
            exc,
        ) from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise _font_error(
            font_path,
            f"FFmpeg could not render it with {str(ffmpeg_exe)!r}"
            + (f": {detail}" if detail else ""),
        )
    expected_bytes = frame_width * frame_height
    if len(stdout) != expected_bytes:
        raise _font_error(
            font_path,
            f"FFmpeg returned {len(stdout)} raw gray bytes; expected {expected_bytes}",
        )

    frame = Image.frombytes("L", (frame_width, frame_height), stdout)
    measurements: list[tuple[int, int]] = []
    for placed in positioned:
        lit = frame.crop((0, placed.row_top, frame_width, placed.row_bottom))
        bounds = lit.getbbox()
        if bounds is None:
            raise _font_error(
                font_path,
                f"FFmpeg rendered no measurable pixels for {placed.item.element} text "
                f"{placed.item.text!r}",
            )
        left, top, right, bottom = bounds
        measurements.append((right - left, bottom - top))
    return measurements


def _position(
    value: int | str,
    *,
    w: int,
    h: int,
    text_w: int,
    text_h: int,
) -> float:
    if type(value) is int:
        return float(value)
    if not isinstance(value, str):
        raise OverlayFitError(f"position must be an integer or string, got {type(value).__name__}")
    try:
        tree = ast.parse(value, mode="eval")
    except SyntaxError as exc:
        raise OverlayFitError(f"invalid position expression {value!r}") from exc
    result = _eval_position_node(
        tree.body,
        {"w": w, "h": h, "text_w": text_w, "text_h": text_h},
    )
    if not math.isfinite(result):
        raise OverlayFitError(f"position expression {value!r} is not finite")
    return result


def _eval_position_node(node: ast.AST, bindings: dict[str, int]) -> float:
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in _POSITION_NAMES:
            raise OverlayFitError(f"position variable {node.id!r} is not supported")
        return float(bindings[node.id])
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_position_node(node.operand, bindings)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = _eval_position_node(node.left, bindings)
        right = _eval_position_node(node.right, bindings)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left / right
    raise OverlayFitError(
        "position expressions support only numbers, w/h/text_w/text_h, parentheses, "
        "unary signs, and + - * /"
    )
