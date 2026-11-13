"""Pure, standard-library helpers for the local Qwen advisory adapter."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Real


@dataclass(frozen=True)
class AnalysisRequest:
    media: str
    purpose: str = "reference"
    brief: str | None = None
    modality: str = "joint"
    start_seconds: float = 0
    duration_seconds: float | None = None
    source_offset_seconds: float = 0
    window_seconds: float = 25
    overlap_seconds: float = 2
    max_new_tokens: int = 500
    dry_run: bool = False
    expected_version: int = 0


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float
    sample_rate: int
    channels: int
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_start_seconds: float = 0
    video_start_seconds: float | None = None


@dataclass(frozen=True)
class WindowSpec:
    index: int
    start_seconds: float
    end_seconds: float
    source_start_seconds: float
    source_end_seconds: float
    overlap_previous_seconds: float = 0

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class PreparedWindow:
    media_path: str
    media_sha256: str
    audio_metrics: dict
    preparation: dict


@dataclass(frozen=True)
class BatchOutcome:
    results: tuple[dict, ...]
    telemetry: dict
    status: str = "success"
    error: dict | None = None


class QwenRequestError(ValueError):
    """Invalid helper request; this is not a retryable job failure."""


class QwenSetupError(RuntimeError):
    """Unavailable or inadmissible helper runtime; not a job failure."""


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise QwenRequestError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise QwenRequestError(f"{name} must be a finite number")
    return number


def validate_request(request: AnalysisRequest) -> None:
    """Validate request values without coercion or implicit clamping."""
    if not isinstance(request, AnalysisRequest):
        raise QwenRequestError("request must be an AnalysisRequest")
    for name in ("media", "purpose", "modality"):
        value = getattr(request, name)
        if not isinstance(value, str) or "\x00" in value:
            raise QwenRequestError(f"{name} must be a string without NUL")
    if not request.media.strip():
        raise QwenRequestError("media must not be empty")
    if request.purpose not in {"reference", "audio_review"}:
        raise QwenRequestError("purpose must be 'reference' or 'audio_review'")
    if request.modality not in {"joint", "audio"}:
        raise QwenRequestError("modality must be 'joint' or 'audio'")
    if request.brief is not None and (
        not isinstance(request.brief, str) or "\x00" in request.brief
    ):
        raise QwenRequestError("brief must be a string without NUL")
    if request.purpose == "audio_review" and (
        request.brief is None or not request.brief.strip()
    ):
        raise QwenRequestError("audio_review requires a nonempty intent brief")
    for name in ("start_seconds", "source_offset_seconds"):
        if _finite_number(name, getattr(request, name)) < 0:
            raise QwenRequestError(f"{name} must be nonnegative")
    if request.duration_seconds is not None and _finite_number(
        "duration_seconds", request.duration_seconds
    ) <= 0:
        raise QwenRequestError("duration_seconds must be positive when supplied")
    window = _finite_number("window_seconds", request.window_seconds)
    overlap = _finite_number("overlap_seconds", request.overlap_seconds)
    if not 1 <= window <= 25:
        raise QwenRequestError("window_seconds must be between 1 and 25")
    if not 0 <= overlap < window:
        raise QwenRequestError("overlap_seconds must be nonnegative and less than window_seconds")
    if isinstance(request.max_new_tokens, bool) or not isinstance(request.max_new_tokens, int):
        raise QwenRequestError("max_new_tokens must be an integer")
    if not 1 <= request.max_new_tokens <= 500:
        raise QwenRequestError("max_new_tokens must be between 1 and 500")
    if type(request.dry_run) is not bool:
        raise QwenRequestError("dry_run must be a boolean")
    if isinstance(request.expected_version, bool) or not isinstance(request.expected_version, int):
        raise QwenRequestError("expected_version must be an integer")
    if request.expected_version != 0:
        raise QwenRequestError("expected_version must be 0 for a new helper report")


def plan_windows(
    duration_seconds: float,
    *,
    start_seconds: float = 0,
    end_seconds: float | None = None,
    window_seconds: float = 25,
    overlap_seconds: float = 2,
    source_offset_seconds: float = 0,
) -> list[WindowSpec]:
    """Partition an input interval, anchoring the last full window at its end."""
    total = _finite_number("duration_seconds", duration_seconds)
    start = _finite_number("start_seconds", start_seconds)
    window = _finite_number("window_seconds", window_seconds)
    overlap = _finite_number("overlap_seconds", overlap_seconds)
    offset = _finite_number("source_offset_seconds", source_offset_seconds)
    end = total if end_seconds is None else _finite_number("end_seconds", end_seconds)
    if total <= 0:
        raise QwenRequestError("media duration_seconds must be positive")
    if start < 0 or end <= start or end > total:
        raise QwenRequestError("requested interval must be positive and within media duration")
    if not 1 <= window <= 25:
        raise QwenRequestError("window_seconds must be between 1 and 25")
    if overlap < 0 or overlap >= window:
        raise QwenRequestError("overlap_seconds must be nonnegative and less than window_seconds")
    if offset < 0:
        raise QwenRequestError("source_offset_seconds must be nonnegative")

    interval = end - start
    if interval <= window:
        starts = [start]
        ends = [end]
    else:
        step = window - overlap
        regular_ratio = (interval - window) / step
        if not math.isfinite(regular_ratio) or regular_ratio > 255:
            raise QwenRequestError(
                "interval requires more than 256 windows; choose a shorter explicit interval"
            )
        regular_count = math.ceil(regular_ratio)
        if regular_count + 1 > 256:
            raise QwenRequestError(
                "interval requires more than 256 windows; choose a shorter explicit interval"
            )
        starts = []
        cursor = start
        while cursor + window < end:
            starts.append(cursor)
            next_cursor = cursor + step
            if next_cursor <= cursor:
                raise QwenRequestError(
                    "window step is too small to advance at this interval; choose a shorter interval"
                )
            cursor = next_cursor
        tail_start = end - window
        if not starts or starts[-1] < tail_start:
            starts.append(tail_start)
        ends = [min(position + window, end) for position in starts]

    if len(starts) > 256:
        raise QwenRequestError(
            "interval requires more than 256 windows; choose a shorter explicit interval"
        )
    specs: list[WindowSpec] = []
    previous_end: float | None = None
    for index, (left, right) in enumerate(zip(starts, ends), start=1):
        shared = 0.0 if previous_end is None else max(0.0, previous_end - left)
        specs.append(
            WindowSpec(
                index=index,
                start_seconds=left,
                end_seconds=right,
                source_start_seconds=left + offset,
                source_end_seconds=right + offset,
                overlap_previous_seconds=shared,
            )
        )
        previous_end = right
    return specs


def build_prompt(purpose: str, brief: str | None, modality: str) -> str:
    """Build the advisory prompt while preserving supplied intent verbatim."""
    if purpose not in {"reference", "audio_review"}:
        raise QwenRequestError("purpose must be 'reference' or 'audio_review'")
    if modality not in {"joint", "audio"}:
        raise QwenRequestError("modality must be 'joint' or 'audio'")
    if brief is not None and (not isinstance(brief, str) or "\x00" in brief):
        raise QwenRequestError("brief must be a string without NUL")
    if purpose == "audio_review" and (brief is None or not brief.strip()):
        raise QwenRequestError("audio_review requires a nonempty intent brief")

    if purpose == "audio_review":
        media_instruction = "Use audible evidence only; do not make visual claims."
        intent = f"\nCreator's intent brief (preserve as supplied):\n{brief}\n"
        headings = "Audible observations\nFit to brief\nSuggested changes\nUncertainties"
        guidance = (
            "Assess fit against the creator's stated intent. Hiss, distortion, noise, or "
            "limited fidelity may be intentional stylization; do not assume studio-clean "
            "audio is the goal. Tie suggestions to audible observations and the intended "
            "sound. Do not invent numerical edits, dialogue, identities, or narrative."
        )
    else:
        media_instruction = (
            "Describe audio and video evidence." if modality == "joint" else
            "Describe audible evidence only; do not request or infer visual details."
        )
        intent = f"\nOptional creator context (quote faithfully):\n{brief}\n" if brief else ""
        headings = "Audio observations\nVisual observations\nUncertainties"
        if modality == "audio":
            headings = "Audio observations\nSequence and voice qualities\nUncertainties"
        guidance = (
            "Be evidence-focused and concrete. Describe useful sequence, sounds, and voice "
            "qualities when audible. Separate observations from uncertainty; do not invent "
            "dialogue, identities, or narrative."
        )
    return (
        "You are an advisory media analyst. Report only what the supplied clip supports. "
        f"{media_instruction}\n{intent}\n{guidance}\n"
        f"Organize a detailed response under these headings:\n{headings}\n"
        "Keep the response useful and specific without padding. Recommendations are advisory; "
        "the creator makes the final decision."
    )


def coverage_gaps(
    start_seconds: float,
    end_seconds: float,
    completed: Sequence[WindowSpec],
) -> list[list[float]]:
    """Return uncovered portions of an interval after merging covered windows."""
    start = _finite_number("start_seconds", start_seconds)
    end = _finite_number("end_seconds", end_seconds)
    if start < 0 or end <= start:
        raise QwenRequestError("coverage interval must be positive and nonnegative")
    covered: list[tuple[float, float]] = []
    for item in completed:
        left = max(start, _finite_number("window start", item.start_seconds))
        right = min(end, _finite_number("window end", item.end_seconds))
        if right > left:
            covered.append((left, right))
    covered.sort()
    merged: list[list[float]] = []
    for left, right in covered:
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    gaps: list[list[float]] = []
    cursor = start
    for left, right in merged:
        if left > cursor:
            gaps.append([cursor, left])
        cursor = max(cursor, right)
    if cursor < end:
        gaps.append([cursor, end])
    return gaps


def render_markdown(report: dict) -> str:
    """Render an advisory report, preserving every supplied raw response verbatim."""
    request = report.get("request") or {}
    media = report.get("media") or {}
    brief_data = report.get("brief")
    if isinstance(brief_data, dict):
        brief = brief_data.get("text")
    else:
        brief = brief_data
    coverage = report.get("input_coverage") or {}
    requested = coverage.get("requested_interval")
    if isinstance(requested, dict):
        interval_start = requested.get("start_seconds", requested.get("start", 0))
        interval_end = requested.get("end_seconds", requested.get("end"))
    elif isinstance(requested, (list, tuple)) and len(requested) == 2:
        interval_start, interval_end = requested
    else:
        interval_start = request.get("start_seconds", 0)
        interval_end = (
            interval_start + request["duration_seconds"]
            if request.get("duration_seconds") is not None
            else (media.get("info") or {}).get("duration_seconds")
        )
    lines = [
        "# Qwen media analysis",
        "",
        f"- Status: {report.get('status', 'unknown')}",
        f"- Purpose: {request.get('purpose', 'unknown')}",
        f"- Modality: {request.get('modality', 'unknown')}",
        f"- Media: {media.get('path', request.get('media', 'unknown'))}",
        f"- Requested interval: {interval_start} s to {interval_end} s",
        (
            f"- Window / overlap: {request.get('window_seconds', 'unknown')} s / "
            f"{request.get('overlap_seconds', 'unknown')} s"
        ),
        "",
        "## Creator intent brief",
        "",
        "(none supplied)" if brief is None else str(brief),
        "",
        "## Signal metrics",
        "",
    ]
    windows = report.get("windows", [])
    metric_windows = [w for w in windows if w.get("audio_metrics") is not None]
    if not metric_windows:
        lines.append("No prepared signal metrics.")
    for item in metric_windows:
        lines.extend(
            [f"### Window {item.get('index', '?')}", "", "```json", _json(item["audio_metrics"]), "```", ""]
        )
    lines.extend(["## Window responses", ""])
    for item in windows:
        input_start = item.get("input_start_seconds", item.get("start_seconds"))
        input_end = item.get("input_end_seconds", item.get("end_seconds"))
        source_start = item.get("source_start_seconds")
        source_end = item.get("source_end_seconds")
        raw_response = item.get("raw_response")
        if isinstance(raw_response, str):
            longest_tick_run = max(
                (len(run) for run in re.findall(r"`+", raw_response)), default=0
            )
            fence = "`" * max(3, longest_tick_run + 1)
        else:
            fence = "```"
        lines.extend(
            [
                f"### Window {item.get('index', '?')} ({item.get('status', 'unknown')})",
                "",
                f"- Input interval: {input_start} s to {input_end} s",
                f"- Source interval: {source_start} s to {source_end} s",
                f"- Overlap with previous window: {item.get('overlap_previous_seconds', 'unknown')} s",
                f"- Termination: {item.get('termination_reason', 'unknown')}",
                f"- Token cap reached: {item.get('token_cap_reached', 'unknown')}",
                "",
                f"{fence}text",
                raw_response if isinstance(raw_response, str) else "",
                fence,
                "",
            ]
        )
    lines.extend(["## Coverage and limits", "", "Uncovered intervals:"])
    lines.append(_json(coverage.get("uncovered_intervals", [])))
    lines.extend(["", "## Errors", "", _json(report.get("errors", [])), ""])
    lines.extend(["## Limitations", ""])
    limitations = report.get("limitations", [])
    if isinstance(limitations, list):
        lines.extend(f"- {value}" for value in limitations)
    else:
        lines.append(str(limitations))
    return "\n".join(lines)


def _json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
