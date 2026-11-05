"""Pure validation and FFmpeg graph construction for FR-21 post profiles."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from orchestrator.domain.entities import (
    ArtifactScheduleEntry,
    CameraProfile,
    PostProfile,
    PostStage,
    Shot,
)
from orchestrator.domain.invalidation import revision_impact
from orchestrator.domain.seeds import INT32_MAX, derive_seed


class ProfileValidationError(ValueError):
    """A malformed profile or unsupported FFmpeg feature."""


class OverlaySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    camera_id: str = Field(min_length=1)
    clock_epoch: int = Field(strict=True)
    fontsize: int = Field(gt=0, strict=True)
    x: StrictInt | StrictStr = 8
    y: StrictInt | StrictStr = "h-24"
    placement: Literal["in_recording", "on_playback"] = "in_recording"

    @field_validator("camera_id")
    @classmethod
    def _printable_camera_id(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("camera_id must not contain control characters")
        return value

    @field_validator("x", "y")
    @classmethod
    def _safe_position(cls, value: StrictInt | StrictStr) -> StrictInt | StrictStr:
        if isinstance(value, str) and not re.fullmatch(r"[A-Za-z0-9_()+*/. -]+", value):
            raise ValueError("overlay position contains unsupported filter syntax")
        return value


class EncodeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    codec: str = Field(min_length=1)
    preset: str = "medium"
    crf: int = Field(ge=0, le=51, strict=True)
    pix_fmt: str = Field(min_length=1)
    threads: int = Field(ge=1, strict=True)


class PostProfileSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: PostProfile
    output_fps: int = Field(gt=0, strict=True)
    storage_resolution: tuple[int, int]
    sample_aspect_ratio: str = "1/1"
    artifact_schedule: tuple[ArtifactScheduleEntry, ...] = ()
    overlay: OverlaySpec | None = None
    encode: EncodeSpec
    supersedes: str | None = None

    @field_validator("storage_resolution", mode="before")
    @classmethod
    def _positive_resolution(cls, value: Any) -> tuple[int, int]:
        if not isinstance(value, (tuple, list)) or len(value) != 2 or any(type(item) is not int or item < 1 for item in value):
            raise ValueError("storage_resolution dimensions must be positive integers")
        return tuple(value)


@dataclass(frozen=True)
class ValidatedPostProfile:
    profile: PostProfileSpec
    camera: CameraProfile
    master_frames: int
    source_rate: Fraction
    output_rate: Fraction
    decimated_frames: int
    predicted_frames: int
    clock_steps: tuple[tuple[int, int], ...]
    schedule: tuple[ArtifactScheduleEntry, ...]


_FILTER_PARAMS: dict[str, frozenset[str]] = {
    "scale": frozenset({"w", "h", "flags"}),
    "fps": frozenset({"fps"}),
    "select": frozenset({"from_schedule"}),
    "loop": frozenset({"from_schedule"}),
    "setpts": frozenset({"from_schedule", "timebase", "pts"}),
    "drawtext": frozenset({"camera_id", "clock_epoch", "fontsize", "x", "y"}),
    "eq": frozenset({"brightness", "contrast", "saturation"}),
    "noise": frozenset({"alls", "allf", "all_seed_max"}),
    "unsharp": frozenset({"luma_msize_x", "luma_msize_y", "luma_amount"}),
    "setsar": frozenset({"sar"}),
}
_EXPECTED_FILTERS = frozenset(_FILTER_PARAMS)
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProfileValidationError(f"{name} must be a finite number")
    return float(value)


def _validate_stages(stages: tuple[PostStage, ...]) -> None:
    names: set[str] = set()
    seen_filters: set[str] = set()
    for stage in stages:
        if stage.name in names:
            raise ProfileValidationError(f"duplicate stage name: {stage.name}")
        names.add(stage.name)
        if stage.filter not in _FILTER_PARAMS:
            raise ProfileValidationError(f"unsupported FFmpeg filter {stage.filter!r}")
        if stage.filter in seen_filters and stage.filter not in {"setpts", "drawtext"}:
            raise ProfileValidationError(f"duplicate {stage.filter!r} stage is unsupported")
        seen_filters.add(stage.filter)
        unknown = set(stage.params) - _FILTER_PARAMS[stage.filter]
        if unknown:
            raise ProfileValidationError(f"unknown {stage.filter} parameter(s): {', '.join(sorted(unknown))}")
        for key, value in stage.params.items():
            if isinstance(value, str) and any(token in value for token in (";", "[", "]", "\n", "\r")):
                raise ProfileValidationError(f"unsafe filter parameter {stage.name}.{key}")
        if stage.filter == "setpts":
            marker = stage.params.get("from_schedule")
            if marker not in (None, "clock"):
                raise ProfileValidationError(f"unsupported setpts schedule mode: {marker!r}")
            expected_timebase = "1/output_fps"
            if "timebase" in stage.params and stage.params["timebase"] != expected_timebase:
                raise ProfileValidationError("setpts timebase must be explicit 1/output_fps")
            expected_pts = "N+schedule_clock" if marker == "clock" else "N"
            if "pts" in stage.params and stage.params["pts"] != expected_pts:
                raise ProfileValidationError(f"setpts expression must be {expected_pts}")
        elif stage.filter in {"select", "loop"}:
            if stage.params.get("from_schedule") not in {"drop+freeze", "freeze"}:
                raise ProfileValidationError(f"unsupported {stage.filter} schedule mode")
        elif "from_schedule" in stage.params:
            raise ProfileValidationError(f"unexpected from_schedule on {stage.name}")
        if stage.filter == "noise" and stage.seed_source not in (None, "shot", "shot_degrade_seed"):
            raise ProfileValidationError("noise seed_source must be shot_degrade_seed")
        if stage.filter == "noise":
            strength = stage.params.get("alls", 12)
            if not isinstance(strength, int) or isinstance(strength, bool) or not 0 <= strength <= 100:
                raise ProfileValidationError("noise alls must be an integer in [0, 100]")
            maximum = stage.params.get("all_seed_max", INT32_MAX)
            if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= INT32_MAX:
                raise ProfileValidationError("noise all_seed_max must be in [1, INT32_MAX]")
            flags = stage.params.get("allf", "t+u")
            if not isinstance(flags, str) or flags not in {"a", "t", "u", "t+u"}:
                raise ProfileValidationError("noise allf must be one of a, t, u or t+u")
        if stage.filter == "scale":
            for key in ("w", "h"):
                if key in stage.params and (not isinstance(stage.params[key], int)
                                            or isinstance(stage.params[key], bool) or stage.params[key] <= 0):
                    raise ProfileValidationError(f"scale {key} must be a positive integer")
            if stage.params.get("flags", "bicubic") not in {
                "fast_bilinear", "bilinear", "bicubic", "experimental", "neighbor", "area",
                "bicublin", "gauss", "sinc", "lanczos", "spline",
            }:
                raise ProfileValidationError("unsupported scale flags value")
        if stage.filter == "fps" and "fps" in stage.params:
            fps_value = stage.params["fps"]
            if not isinstance(fps_value, int) or isinstance(fps_value, bool) or fps_value <= 0:
                raise ProfileValidationError("fps stage fps must be a positive integer")
        if stage.filter == "eq":
            for key in ("brightness", "contrast", "saturation"):
                if key in stage.params:
                    _finite(stage.params[key], f"{stage.name}.{key}")
        if stage.filter == "unsharp":
            for key in ("luma_msize_x", "luma_msize_y"):
                if key in stage.params and (not isinstance(stage.params[key], int) or not 3 <= stage.params[key] <= 23 or stage.params[key] % 2 == 0):
                    raise ProfileValidationError(f"{stage.name}.{key} must be an odd integer from 3 to 23")
            if "luma_amount" in stage.params:
                amount = _finite(stage.params["luma_amount"], f"{stage.name}.luma_amount")
                if not -2 <= amount <= 5:
                    raise ProfileValidationError("unsharp luma_amount must be in [-2, 5]")
        if stage.filter == "drawtext" and stage.composite_stage not in (None, "in_recording"):
            raise ProfileValidationError("on_playback overlays are not implemented in this pass")


def validate_profile(
    profile: PostProfileSpec,
    camera: CameraProfile,
    *,
    master_frames: int,
    master_rate: Fraction,
) -> ValidatedPostProfile:
    """Validate profile fields against a probed 60 fps master and its schedule."""
    try:
        profile = PostProfileSpec.model_validate(profile.model_dump(mode="python"))
        camera = CameraProfile.model_validate(camera.model_dump(mode="python"))
    except (ValidationError, AttributeError, TypeError) as exc:
        raise ProfileValidationError(f"invalid profile or camera profile: {exc}") from exc
    if not _SEMVER.fullmatch(profile.identity.semver):
        raise ProfileValidationError("profile semver must be MAJOR.MINOR.PATCH")
    if not isinstance(master_frames, int) or isinstance(master_frames, bool) or master_frames < 1:
        raise ProfileValidationError("master_frames must be a positive integer")
    _validate_stages(tuple(profile.identity.stages))

    scale = next((s for s in profile.identity.stages if s.filter == "scale"), None)
    fps_stage = next((s for s in profile.identity.stages if s.filter == "fps"), None)
    if scale is None or fps_stage is None or "select" not in {stage.filter for stage in profile.identity.stages}:
        raise ProfileValidationError("profile requires scale, fps and select stages")
    filter_names = [stage.filter for stage in profile.identity.stages]
    if "select" not in filter_names:
        raise ProfileValidationError("profile requires a select schedule stage")
    setpts_stages = [stage for stage in profile.identity.stages if stage.filter == "setpts"]
    if len(setpts_stages) != 2 or sum(stage.params.get("from_schedule") == "clock" for stage in setpts_stages) != 1:
        raise ProfileValidationError("profile requires clock and output setpts stages")
    if any(stage.params.get("from_schedule") not in {None, "clock"} for stage in setpts_stages):
        raise ProfileValidationError("setpts stages must be one clock stage and one output stage")
    if setpts_stages[1].params.get("from_schedule") == "clock":
        raise ProfileValidationError("clock setpts must precede output setpts")
    width, height = profile.storage_resolution
    if (width, height) != tuple(camera.storage_resolution):
        raise ProfileValidationError("profile dimensions must match CameraProfile.storage_resolution")
    if profile.encode.codec != camera.codec:
        raise ProfileValidationError("profile codec must match CameraProfile.codec")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in (width, height)):
        raise ProfileValidationError("scale dimensions must be positive integers")
    for stage in profile.identity.stages:
        if stage.filter == "scale" and (("w" in stage.params and stage.params["w"] != width)
                                         or ("h" in stage.params and stage.params["h"] != height)):
            raise ProfileValidationError("scale stage dimensions must match PostProfileSpec.storage_resolution")
    output_fps = _finite(profile.output_fps, "output fps")
    if output_fps <= 0 or not math.isclose(output_fps, camera.fps):
        raise ProfileValidationError("profile output_fps must match CameraProfile.fps")
    if "fps" in fps_stage.params:
        stage_fps = _finite(fps_stage.params["fps"], "fps stage value")
        if not math.isclose(stage_fps, output_fps):
            raise ProfileValidationError("fps stage value must match PostProfileSpec.output_fps")
    if master_rate <= 0:
        raise ProfileValidationError("master_rate must be positive")
    source_rate = master_rate
    output_rate = Fraction(str(output_fps))
    exact_frames = Fraction(master_frames) * output_rate / source_rate
    # FFmpeg's fps filter rounds to nearest, with ties away from zero. Values are
    # positive here, so floor(x + 1/2) is exact and avoids Python's ties-to-even.
    decimated = (2 * exact_frames.numerator + exact_frames.denominator) // (2 * exact_frames.denominator)
    if decimated < 1:
        raise ProfileValidationError("profile produces no decimated frames")

    events = tuple(profile.artifact_schedule)
    camera_events = tuple(sorted(camera.frame_drop_schedule, key=lambda event: (event.start_frame, event.type)))
    if tuple(sorted(events, key=lambda event: (event.start_frame, event.type))) != camera_events:
        raise ProfileValidationError("profile artifact_schedule must match CameraProfile.frame_drop_schedule")
    if any(event.type not in {"drop", "freeze"} for event in events):
        raise ProfileValidationError("only drop and freeze schedule events are supported")
    events = tuple(sorted(events, key=lambda event: (event.start_frame, event.type)))
    has_drawtext = any(stage.filter == "drawtext" for stage in profile.identity.stages)
    if has_drawtext != (profile.overlay is not None):
        raise ProfileValidationError("PostProfileSpec.overlay and drawtext stage must be present together")
    if any(event.type == "drop" for event in events) and not any(
        stage.filter == "setpts" and stage.params.get("from_schedule") == "clock"
        for stage in profile.identity.stages
    ):
        raise ProfileValidationError("drop schedules require a schedule-clock setpts stage")
    if any(event.type == "freeze" for event in events) and "loop" not in filter_names:
        raise ProfileValidationError("freeze schedules require a loop stage")
    if profile.identity.stages and any(stage.filter == "drawtext" for stage in profile.identity.stages):
        if profile.overlay is None:
            raise ProfileValidationError("drawtext stage requires PostProfileSpec.overlay")
        if profile.overlay.placement == "on_playback":
            raise ProfileValidationError("on_playback overlay positioning is unsupported in this pass")
        for stage in profile.identity.stages:
            if stage.filter != "drawtext":
                continue
            fields = {"camera_id": profile.overlay.camera_id, "clock_epoch": profile.overlay.clock_epoch,
                      "fontsize": profile.overlay.fontsize, "x": profile.overlay.x, "y": profile.overlay.y}
            for key, expected in fields.items():
                if key in stage.params and stage.params[key] != expected:
                    raise ProfileValidationError(f"drawtext {key} disagrees with PostProfileSpec.overlay")
    order = {"scale": 0, "fps": 1, "select": 2, "loop": 3, "drawtext": 5,
             "eq": 7, "noise": 8, "unsharp": 9, "setsar": 10}
    ranks = [4 if stage.filter == "setpts" and stage.params.get("from_schedule") == "clock"
             else 6 if stage.filter == "setpts" else order[stage.filter]
             for stage in profile.identity.stages]
    if ranks != sorted(ranks):
        raise ProfileValidationError("stages violate the measured degradation order")
    occupied: set[int] = set()
    drops: list[ArtifactScheduleEntry] = []
    freezes: list[ArtifactScheduleEntry] = []
    for event in events:
        indices = set(range(event.start_frame, event.start_frame + event.count))
        if event.start_frame + event.count > decimated:
            raise ProfileValidationError(
                f"{event.type} window {event.start_frame}-{event.start_frame + event.count - 1} "
                f"falls outside decimated range 0-{decimated - 1}"
            )
        if occupied & indices:
            raise ProfileValidationError("drop/freeze schedule windows may not overlap")
        occupied.update(indices)
        (drops if event.type == "drop" else freezes).append(event)
    if any(event.start_frame == 0 for event in freezes):
        raise ProfileValidationError("freeze cannot start at frame 0 because it has no previous image")
    survivors = [index for index in range(decimated) if index not in occupied]
    for drop in drops:
        end = drop.start_frame + drop.count
        if not any(index >= end for index in survivors):
            raise ProfileValidationError("drop window leaves no surviving frames after it")
    output_to_source = list(survivors)
    for freeze in freezes:
        held = freeze.start_frame - 1
        try:
            position = output_to_source.index(held)
        except ValueError as exc:
            raise ProfileValidationError(f"freeze at {freeze.start_frame} has no surviving previous frame") from exc
        output_to_source[position + 1:position + 1] = [held] * freeze.count
    predicted = len(output_to_source)
    clock_steps: list[tuple[int, int]] = []
    cumulative = 0
    for drop in drops:
        first_after = drop.start_frame + drop.count
        boundary = next((out_idx for out_idx, src_idx in enumerate(output_to_source) if src_idx >= first_after), None)
        if boundary is None:
            raise ProfileValidationError("drop schedule has no clock boundary after the dropped interval")
        cumulative += drop.count
        clock_steps.append((boundary, cumulative))

    sar = profile.sample_aspect_ratio
    if sar not in ("1/1", "1:1", "12/11"):
        try:
            sample_aspect = Fraction(sar.replace(":", "/"))
        except (ValueError, ZeroDivisionError) as exc:
            raise ProfileValidationError(f"invalid sample aspect ratio: {sar!r}") from exc
    else:
        sample_aspect = Fraction(sar.replace(":", "/"))
    try:
        display_aspect = Fraction(camera.display_aspect_ratio.replace(":", "/"))
    except (ValueError, ZeroDivisionError) as exc:
        raise ProfileValidationError(f"invalid display aspect ratio: {camera.display_aspect_ratio!r}") from exc
    if Fraction(width, height) * sample_aspect != display_aspect:
        raise ProfileValidationError("storage resolution and sample aspect ratio do not match display aspect ratio")
    for stage in profile.identity.stages:
        if stage.filter == "setsar":
            explicit_sar = stage.params.get("sar", sar)
            try:
                explicit_sar_value = Fraction(str(explicit_sar).replace(":", "/"))
            except (ValueError, ZeroDivisionError) as exc:
                raise ProfileValidationError("setsar.sar must be a positive rational") from exc
            if explicit_sar_value <= 0 or explicit_sar_value != sample_aspect:
                raise ProfileValidationError("explicit setsar.sar must equal PostProfileSpec.sample_aspect_ratio")
    return ValidatedPostProfile(profile, camera, master_frames, source_rate, output_rate,
                                decimated, predicted, tuple(clock_steps), events)


def _escape_filter_value(value: str) -> str:
    """Escape a path for FFmpeg's nested filter option grammar."""
    value = value.replace("\\", "/")
    for char in ("\\", ":", "'", ",", ";", "[", "]"):
        value = value.replace(char, "\\" + char)
    return value


def _clock_expr(steps: tuple[tuple[int, int], ...]) -> str:
    expr = "0"
    for boundary, cumulative in steps:
        expr = f"if(gte(N,{boundary}),{cumulative},{expr})"
    return expr


def build_filter_graph(
    profile: ValidatedPostProfile,
    *,
    shot_id: str,
    degrade_seed: int,
    font_path: str,
) -> str:
    """Build the deterministic filter graph, preserving spike 01 stage semantics."""
    if degrade_seed < 0:
        raise ProfileValidationError("degrade_seed must be nonnegative")
    if PurePosixPath(font_path).is_absolute() or ".." in PurePosixPath(font_path).parts:
        raise ProfileValidationError("font_path must be a contained project-relative path")
    stages = profile.profile.identity.stages
    by_filter: dict[str, list[PostStage]] = {}
    for stage in stages:
        by_filter.setdefault(stage.filter, []).append(stage)
    parts: list[str] = []
    for stage in stages:
        name, f, p = stage.name, stage.filter, stage.params
        if f == "scale":
            parts.append(f"scale={profile.profile.storage_resolution[0]}:"
                         f"{profile.profile.storage_resolution[1]}:flags={p.get('flags', 'bicubic')}")
        elif f == "fps":
            parts.append(f"fps={profile.profile.output_fps}")
        elif f == "select":
            windows = [e for e in profile.schedule if e.type in {"drop", "freeze"}]
            terms = [f"between(n,{e.start_frame},{e.start_frame + e.count - 1})" for e in windows]
            parts.append("select='not(" + "+".join(terms) + ")'" if terms else "select='1'")
        elif f == "loop":
            dropped_before = 0
            for event in profile.schedule:
                if event.type == "freeze":
                    held = event.start_frame - 1
                    dropped_before = sum(d.count for d in profile.schedule
                                         if d.type == "drop" and d.start_frame < held)
                    parts.append(f"loop=loop={event.count}:size=1:start={held - dropped_before + 1}")
        elif f == "setpts":
            if p.get("from_schedule") == "clock":
                parts.append(f"settb=1/{profile.output_rate.numerator},setpts='N+{_clock_expr(profile.clock_steps)}'")
            else:
                parts.append(f"settb=1/{profile.output_rate.numerator},setpts=N")
        elif f == "drawtext":
            overlay = profile.profile.overlay
            if overlay is None:
                raise ProfileValidationError("drawtext stage requires an overlay specification")
            if overlay.placement == "on_playback":
                raise ProfileValidationError("on_playback overlay positioning is unsupported in this pass")
            font = _escape_filter_value(font_path)
            text = _escape_filter_value(overlay.camera_id)
            parts.append(f"drawtext=fontfile='{font}':text='{text}  %{{pts\\:gmtime\\:{overlay.clock_epoch}}}':"
                         f"x={overlay.x}:y={overlay.y}:fontsize={overlay.fontsize}:fontcolor=white:box=1:boxcolor=black@0.4")
        elif f == "eq":
            parts.append(f"eq=brightness={p.get('brightness', 0)}:contrast={p.get('contrast', 1)}:saturation={p.get('saturation', 1)}")
        elif f == "noise":
            maximum = p.get("all_seed_max", INT32_MAX)
            if not isinstance(maximum, int) or maximum < 1:
                raise ProfileValidationError("noise all_seed_max must be a positive integer")
            seed = derive_seed(shot_id, degrade_seed, name, maximum)
            parts.append(f"noise=alls={p.get('alls', 12)}:allf={p.get('allf', 't+u')}:all_seed={seed}")
        elif f == "unsharp":
            parts.append(f"unsharp={p.get('luma_msize_x', 5)}:{p.get('luma_msize_y', 5)}:{p.get('luma_amount', 0.8)}")
        elif f == "setsar":
            parts.append(f"setsar={str(p.get('sar', profile.profile.sample_aspect_ratio)).replace(':', '/')}")
        else:  # validated against the closed map above
            raise ProfileValidationError(f"unsupported FFmpeg filter {f!r}")
    if not any(stage.filter == "setsar" for stage in stages) and profile.profile.sample_aspect_ratio not in ("1/1", "1:1"):
        parts.append(f"setsar={profile.profile.sample_aspect_ratio.replace(':', '/')}")
    return ",".join(parts)


def camera_profile_impact(before: CameraProfile, after: CameraProfile,
                           shots: Iterable[Shot]) -> tuple[Shot, ...]:
    """Delegate profile invalidation classification to the domain dependency graph."""
    return revision_impact(before, after, shots)
