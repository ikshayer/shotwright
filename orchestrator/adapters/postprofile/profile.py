"""Pure validation and FFmpeg graph construction for FR-21 post profiles."""

from __future__ import annotations

import math
import re
import struct
import unicodedata
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
    model_serializer,
)

from orchestrator.domain.entities import (
    AnalogArtifacts,
    ArtifactScheduleEntry,
    CAMERA_PROFILE_LEGACY_ANALOG_SCHEMA_VERSION,
    CameraProfile,
    PostProfile,
    PostStage,
    Shot,
)
from orchestrator.domain.invalidation import revision_impact
from orchestrator.domain.seeds import INT32_MAX, derive_seed


class ProfileValidationError(ValueError):
    """A malformed profile or unsupported FFmpeg feature."""


class OverlayIndicatorSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The persisted nested Entity carries this field through compose's model_dump().
    schema_version: Literal[1] = 1
    kind: Literal["battery_rec"] = "battery_rec"
    fontsize: int = Field(gt=0, strict=True)
    x: StrictInt | StrictStr
    y: StrictInt | StrictStr
    placement: Literal["in_recording"] = "in_recording"

    @field_validator("x", "y")
    @classmethod
    def _safe_position(cls, value: StrictInt | StrictStr) -> StrictInt | StrictStr:
        return _validate_overlay_position(value)


class OverlaySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    camera_id: str = Field(min_length=1)
    clock_epoch: int = Field(ge=0, strict=True)
    clock_format: str | None = Field(None, min_length=1)
    fontsize: int = Field(gt=0, strict=True)
    x: StrictInt | StrictStr = 8
    y: StrictInt | StrictStr = "h-24"
    placement: Literal["in_recording", "on_playback"] = "in_recording"
    indicator: OverlayIndicatorSpec | None = None

    @field_validator("camera_id")
    @classmethod
    def _printable_camera_id(cls, value: str) -> str:
        if not value or any(
            unicodedata.category(char) == "Cc" or char in "\u2028\u2029" for char in value
        ):
            raise ValueError("camera_id must be nonempty and must not contain control characters or newlines")
        return value

    @field_validator("x", "y")
    @classmethod
    def _safe_position(cls, value: StrictInt | StrictStr) -> StrictInt | StrictStr:
        return _validate_overlay_position(value)

    @field_validator("clock_format")
    @classmethod
    def _safe_clock_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = set("YymdHMSpIbaj")
        index = 0
        while index < len(value):
            if value[index] == "%":
                if index + 1 >= len(value) or value[index + 1] not in allowed:
                    raise ValueError(
                        "clock_format may use only %Y %y %m %d %H %M %S %p %I %b %a %j"
                    )
                index += 2
                continue
            char = value[index]
            if not (char.isascii() and (char.isalnum() or char in " /-:.,[]()")):
                raise ValueError(
                    "clock_format may contain only strftime tokens, letters, digits, "
                    "spaces and / - : . , [ ] ( )"
                )
            index += 1
        return value


def _validate_overlay_position(value: StrictInt | StrictStr) -> StrictInt | StrictStr:
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
    bitrate_kbps: int | None = Field(None, ge=16, le=20000, strict=True)
    block_artifacts: float = Field(0.0, ge=0, le=1, allow_inf_nan=False)

    @field_validator("pix_fmt")
    @classmethod
    def _safe_pixel_format(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValueError("pix_fmt must be a safe FFmpeg pixel-format name")
        return value


class PostProfileSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: PostProfile
    output_fps: int = Field(gt=0, strict=True)
    storage_resolution: tuple[int, int]
    sample_aspect_ratio: str = "1/1"
    artifact_schedule: tuple[ArtifactScheduleEntry, ...] = ()
    overlay: OverlaySpec | None = None
    encode: EncodeSpec
    analog_artifacts: AnalogArtifacts = Field(default_factory=AnalogArtifacts)
    supersedes: str | None = None
    provenance: dict[str, list[dict[str, str | None]]] = Field(
        default_factory=lambda: {"camera_overrides": []}
    )

    @field_validator("storage_resolution", mode="before")
    @classmethod
    def _positive_resolution(cls, value: Any) -> tuple[int, int]:
        if not isinstance(value, (tuple, list)) or len(value) != 2 or any(type(item) is not int or item < 1 for item in value):
            raise ValueError("storage_resolution dimensions must be positive integers")
        return tuple(value)

    @model_serializer(mode="wrap")
    def _omit_neutral_analog_artifacts(self, handler: Any) -> dict[str, Any]:
        """Keep legacy neutral spec JSON (and its receipt hash) byte-for-byte stable."""
        data = handler(self)
        if not self.analog_artifacts.active:
            data.pop("analog_artifacts", None)
        return data


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
    "select": frozenset({"from_schedule", "pattern"}),
    "loop": frozenset({"from_schedule"}),
    "setpts": frozenset({"from_schedule", "timebase", "pts"}),
    "drawtext": frozenset({"camera_id", "clock_epoch", "clock_format", "fontsize", "x", "y", "element"}),
    "colortemperature": frozenset({"temperature"}),
    "colorchannelmixer": frozenset({"rr", "gg", "bb"}),
    "eq": frozenset({"brightness", "contrast", "saturation"}),
    "gblur": frozenset({"sigma", "sigmaV", "planes"}),
    "noise": frozenset({
        "alls", "allf", "all_seed_max", "c0s", "c1s", "c2s", "c0f", "c1f", "c2f",
    }),
    "unsharp": frozenset({"luma_msize_x", "luma_msize_y", "luma_amount"}),
    # A closed, typed external stage marker. It is never emitted as an FFmpeg filter.
    "camera_analog_artifacts": frozenset(),
    "setsar": frozenset({"sar"}),
}
_EXPECTED_FILTERS = frozenset(_FILTER_PARAMS)
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


# ntsc-rs v0.9.7 `SettingsList::from_json` initializes from `legacy_value()`, not
# `NtscEffect::default()`. Pin every non-seed setting from the latter here so omitted
# creator groups cannot inherit version-compatibility values. Enum integers and ratio
# values follow the pinned crate's public SettingDescriptor JSON format.
_NTSC_RS_097_DEFAULT_SETTINGS: dict[str, object] = {
    "version": 1,
    "use_field": 4,  # InterleavedUpper
    "filter_type": 1,  # Butterworth
    "input_luma_filter": 2,  # Notch
    "chroma_lowpass_in": 2,  # Full
    "chroma_demodulation": 1,  # Notch
    "chroma_lowpass_out": 2,  # Full
    "luma_smear": 0.5,
    "composite_preemphasis": 1.0,
    "head_switching": True,
    "head_switching_height": 8,
    "head_switching_offset": 3,
    "head_switching_horizontal_shift": 72.0,
    "head_switching_start_mid_line": True,
    "head_switching_mid_line_position": 0.95,
    "head_switching_mid_line_jitter": 0.03,
    "tracking_noise": True,
    "tracking_noise_height": 12,
    "tracking_noise_wave_intensity": 15.0,
    "tracking_noise_snow_intensity": 0.025,
    "tracking_noise_snow_anisotropy": 0.25,
    "tracking_noise_noise_intensity": 0.25,
    "composite_noise": True,
    "composite_noise_intensity": 0.05,
    "composite_noise_frequency": 0.5,
    "composite_noise_detail": 1,
    "luma_noise": True,
    "luma_noise_intensity": 0.01,
    "luma_noise_frequency": 0.5,
    "luma_noise_detail": 1,
    "chroma_noise": True,
    "chroma_noise_intensity": 0.1,
    "chroma_noise_frequency": 0.05,
    "chroma_noise_detail": 2,
    "snow_intensity": 0.00025,
    "snow_anisotropy": 0.5,
    "chroma_phase_noise_intensity": 0.001,
    "chroma_phase_error": 0.0,
    "chroma_delay_horizontal": 0.0,
    "chroma_delay_vertical": 0,
    "ringing": True,
    "ringing_frequency": 0.45,
    "ringing_power": 4.0,
    "ringing_scale": 4.0,
    "vhs_settings": True,
    "vhs_tape_speed": 2,  # LP
    "vhs_chroma_loss": 0.000025,
    "vhs_sharpen_enabled": True,
    "vhs_sharpen": 0.25,
    "vhs_sharpen_frequency": 1.0,
    "vhs_edge_wave_enabled": True,
    "vhs_edge_wave": 0.5,
    "vhs_edge_wave_speed": 4.0,
    "vhs_edge_wave_frequency": 0.05,
    "vhs_edge_wave_detail": 2,
    "vhs_chroma_vert_blend": False,
    "scale_settings": True,
    "bandwidth_scale": 1.0,
    "vertical_scale": 1.0,
    "scale_with_video_size": False,
}


def ntsc_settings_from_analog_artifacts(artifacts: AnalogArtifacts) -> dict[str, object]:
    """Translate validated creator controls to the pinned ntsc-rs v0.9.7 JSON keys.

    The crate parser starts from ``NtscEffect::legacy_value()``. Start with the complete
    source-pinned ``NtscEffect::default()`` value, then override supplied creator groups.
    Percentage controls are normalized to the crate's 0-1 JSON units.
    """
    if not isinstance(artifacts, AnalogArtifacts):
        raise ProfileValidationError("analog_artifacts settings must be an AnalogArtifacts model")
    result: dict[str, object] = dict(_NTSC_RS_097_DEFAULT_SETTINGS)

    head = artifacts.head_switching
    if head is not None:
        result.update({
            "head_switching": True,
            "head_switching_height": head.height,
            "head_switching_offset": head.offset,
            "head_switching_horizontal_shift": head.horizontal_shift,
        })
        if head.position is not None or head.jitter is not None:
            result["head_switching_start_mid_line"] = True
            if head.position is not None:
                result["head_switching_mid_line_position"] = head.position / 100
            if head.jitter is not None:
                result["head_switching_mid_line_jitter"] = head.jitter / 100

    tracking = artifacts.tracking_noise
    if tracking is not None:
        result.update({
            "tracking_noise": True,
            "tracking_noise_height": tracking.height,
            "tracking_noise_wave_intensity": tracking.wave_intensity,
            "tracking_noise_snow_intensity": tracking.snow_intensity,
            "tracking_noise_snow_anisotropy": tracking.snow_anisotropy / 100,
            "tracking_noise_noise_intensity": tracking.noise_intensity / 100,
        })

    vhs = artifacts.vhs
    if vhs is not None:
        tape_speed = {"none": 0, "SP": 1, "LP": 2, "EP": 3}[vhs.tape_speed]
        result.update({
            "vhs_settings": True,
            "vhs_tape_speed": tape_speed,
            "vhs_chroma_loss": vhs.chroma_loss / 100,
            "vhs_edge_wave_enabled": vhs.edge_wave.intensity > 0,
            "vhs_edge_wave": vhs.edge_wave.intensity,
            "vhs_edge_wave_speed": vhs.edge_wave.speed,
            "vhs_edge_wave_frequency": vhs.edge_wave.frequency,
            "vhs_edge_wave_detail": vhs.edge_wave.detail,
        })
        if vhs.sharpen is not None:
            result.update({
                "vhs_sharpen_enabled": True,
                "vhs_sharpen": vhs.sharpen.intensity,
                "vhs_sharpen_frequency": vhs.sharpen.frequency,
            })

    signal = artifacts.signal
    if signal is not None:
        result.update({
            "composite_preemphasis": signal.composite_sharpening,
            "luma_smear": signal.luma_smear,
            "chroma_phase_error": signal.phase_error / 100,
            "chroma_phase_noise_intensity": signal.phase_noise / 100,
            "chroma_delay_horizontal": signal.horizontal_delay,
            "chroma_delay_vertical": signal.vertical_delay,
            "chroma_lowpass_in": {"None": 0, "Light": 1, "Full": 2}[signal.chroma_lowpass_in],
            "chroma_lowpass_out": {"None": 0, "Light": 1, "Full": 2}[signal.chroma_lowpass_out],
            "chroma_demodulation": {
                "Box": 0, "Notch": 1, "OneLineComb": 2, "TwoLineComb": 3,
                "TwoD": 4, "TwoDAdaptive": 5,
            }[signal.chroma_demodulation],
            "input_luma_filter": {"None": 0, "Box": 1, "Notch": 2}[signal.input_luma_filter],
            "use_field": {
                "Alternating": 0, "Upper": 1, "Lower": 2, "Both": 3,
                "InterleavedUpper": 4, "InterleavedLower": 5,
            }[signal.field_mode],
            "filter_type": {"ConstantK": 0, "Butterworth": 1}[signal.filter_type],
            "vhs_chroma_vert_blend": signal.vertical_chroma_blend,
        })

    noise = artifacts.noise
    if noise is not None:
        result["snow_intensity"] = noise.snow_intensity
        result["snow_anisotropy"] = noise.snow_anisotropy / 100
        for key, block in (("composite", noise.composite), ("luma", noise.luma),
                           ("chroma", noise.chroma)):
            toggle = f"{key}_noise"
            result[toggle] = block is not None
            if block is not None:
                result[f"{key}_noise_intensity"] = block.intensity / 100
                result[f"{key}_noise_frequency"] = block.frequency
                result[f"{key}_noise_detail"] = block.detail

    ringing = artifacts.ringing
    if ringing is not None:
        result.update({
            "ringing": True,
            "ringing_frequency": ringing.frequency / 100,
            "ringing_power": ringing.power,
            "ringing_scale": ringing.intensity,
        })
    return result


def validate_ntsc_geometry(width: int, height: int, artifacts: AnalogArtifacts) -> None:
    """Reject dimensions that cannot satisfy the pinned processor's filter neighborhoods."""
    if width < 4:
        raise ProfileValidationError(
            "camera_analog_artifacts requires a frame width of at least 4 pixels"
        )
    field_mode = artifacts.signal.field_mode if artifacts.signal is not None else "InterleavedUpper"
    if field_mode == "Both":
        field_rows = height
    elif field_mode == "Upper":
        field_rows = (height + 1) // 2
    else:
        # Lower, Alternating, and both interleaved modes must be safe for the smaller
        # of the two alternating/parity fields.
        field_rows = height // 2
    if field_rows < 2:
        raise ProfileValidationError(
            "camera_analog_artifacts requires at least 2 rows in each processed field; "
            f"{width}x{height} is too small for field_mode={field_mode}"
        )


def validate_ntsc_frame_indices(
    start_index: int, frame_count: int, artifacts: AnalogArtifacts,
) -> None:
    """Keep every stage frame index representable by the pinned Rust field processor."""
    if (not isinstance(start_index, int) or isinstance(start_index, bool) or start_index < 0
            or not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count < 1):
        raise ProfileValidationError("ntsc-pipe frame range must use a nonnegative start and positive count")
    rust_usize_max = (1 << (struct.calcsize("P") * 8)) - 1
    last_index = start_index + frame_count - 1
    if last_index > rust_usize_max:
        raise ProfileValidationError("ntsc-pipe frame index overflows usize")
    signal = artifacts.signal
    field_mode = signal.field_mode if signal is not None else "InterleavedUpper"
    if field_mode in {"InterleavedUpper", "InterleavedLower"} and last_index > (rust_usize_max - 1) // 2:
        raise ProfileValidationError("interleaved field frame index overflows usize")


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProfileValidationError(f"{name} must be a finite number")
    return float(value)


def _parse_decimation_pattern(value: object) -> tuple[int, ...]:
    if not isinstance(value, str):
        raise ProfileValidationError("decimation pattern must be a colon-separated string")
    terms = value.split(":")
    if not 1 <= len(terms) <= 16 or any(
        not term.isascii() or not term.isdigit() or int(term) <= 0 for term in terms
    ):
        raise ProfileValidationError(
            "decimation pattern must contain 1-16 colon-separated positive integers"
        )
    return tuple(int(term) for term in terms)


def _validate_stages(stages: tuple[PostStage, ...]) -> None:
    names: set[str] = set()
    by_filter: dict[str, list[PostStage]] = {}
    for stage in stages:
        if stage.name in names:
            raise ProfileValidationError(f"duplicate stage name: {stage.name}")
        names.add(stage.name)
        if stage.filter not in _FILTER_PARAMS:
            raise ProfileValidationError(f"unsupported FFmpeg filter {stage.filter!r}")
        if stage.filter == "camera_analog_artifacts" and (
            stage.name != "camera_analog_artifacts" or stage.params
            or stage.seed_source is not None or stage.composite_stage is not None
        ):
            raise ProfileValidationError(
                "camera_analog_artifacts must be the parameter-free external stage marker"
            )
        by_filter.setdefault(stage.filter, []).append(stage)
        unknown = set(stage.params) - _FILTER_PARAMS[stage.filter]
        if unknown:
            raise ProfileValidationError(f"unknown {stage.filter} parameter(s): {', '.join(sorted(unknown))}")
        if stage.filter != "drawtext" and stage.composite_stage is not None:
            raise ProfileValidationError(
                f"{stage.name}.composite_stage is only valid on drawtext stages"
            )
        for key, value in stage.params.items():
            if stage.filter == "drawtext" and key in {"camera_id", "clock_format"}:
                continue  # These fields have drawtext-specific grammar and escaping below.
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
        elif stage.filter == "select" and "pattern" in stage.params:
            if "from_schedule" in stage.params:
                raise ProfileValidationError("decimation select may not also declare from_schedule")
            _parse_decimation_pattern(stage.params["pattern"])
        elif stage.filter in {"select", "loop"}:
            if stage.params.get("from_schedule") not in {"drop+freeze", "freeze"}:
                raise ProfileValidationError(f"unsupported {stage.filter} schedule mode")
        elif "from_schedule" in stage.params:
            raise ProfileValidationError(f"unexpected from_schedule on {stage.name}")
        if stage.filter == "noise" and stage.seed_source not in (None, "shot", "shot_degrade_seed"):
            raise ProfileValidationError("noise seed_source must be shot_degrade_seed")
        if stage.filter == "noise":
            all_family = {"alls", "allf"} & set(stage.params)
            component_family = {
                "c0s", "c1s", "c2s", "c0f", "c1f", "c2f",
            } & set(stage.params)
            if all_family and component_family:
                raise ProfileValidationError(
                    "noise stage must not mix all* and c0*/c1*/c2* parameter families"
                )
            for key in ("alls", "c0s", "c1s", "c2s"):
                if key not in stage.params:
                    continue
                strength = stage.params[key]
                if not isinstance(strength, int) or isinstance(strength, bool) or not 0 <= strength <= 100:
                    raise ProfileValidationError(f"noise {key} must be an integer in [0, 100]")
            maximum = stage.params.get("all_seed_max", INT32_MAX)
            if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= INT32_MAX:
                raise ProfileValidationError("noise all_seed_max must be in [1, INT32_MAX]")
            flags = stage.params.get("allf", "t+u")
            if not isinstance(flags, str) or flags not in {"a", "t", "u", "t+u"}:
                raise ProfileValidationError("noise allf must be one of a, t, u or t+u")
            component_flags = {"a", "p", "t", "u", "a+p", "a+t", "a+u", "p+t", "p+u", "t+u", "a+p+t", "a+p+u", "a+t+u", "p+t+u", "a+p+t+u"}
            for key in ("c0f", "c1f", "c2f"):
                if key in stage.params and stage.params[key] not in component_flags:
                    raise ProfileValidationError(f"noise {key} has unsupported flags")
            if any(key in stage.params for key in ("c0s", "c1s", "c2s", "c0f", "c1f", "c2f")):
                if stage.seed_source not in (None, "shot", "shot_degrade_seed"):
                    raise ProfileValidationError("component noise seed_source must be shot_degrade_seed")
                if stage.name == "camera_noise_model" and stage.seed_source != "shot_degrade_seed":
                    raise ProfileValidationError("camera_noise_model seed_source must be shot_degrade_seed")
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
        if stage.filter == "colortemperature" and "temperature" in stage.params:
            temperature = stage.params["temperature"]
            if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(temperature) or not 1000 <= temperature <= 40000:
                raise ProfileValidationError("colortemperature.temperature must be in [1000, 40000]")
        if stage.filter == "colorchannelmixer":
            for key, value in stage.params.items():
                number = _finite(value, f"{stage.name}.{key}")
                if not -2 <= number <= 2:
                    raise ProfileValidationError(f"colorchannelmixer.{key} must be in [-2, 2] for installed FFmpeg")
        if stage.filter == "gblur":
            if "sigma" in stage.params:
                sigma = _finite(stage.params["sigma"], f"{stage.name}.sigma")
                if not 0 <= sigma <= 1024:
                    raise ProfileValidationError("gblur.sigma must be in [0, 1024]")
            if "sigmaV" in stage.params:
                sigma_v = _finite(stage.params["sigmaV"], f"{stage.name}.sigmaV")
                if not -1 <= sigma_v <= 1024:
                    raise ProfileValidationError("gblur.sigmaV must be in [-1, 1024]")
            if "planes" in stage.params and (
                not isinstance(stage.params["planes"], int)
                or isinstance(stage.params["planes"], bool)
                or not 0 <= stage.params["planes"] <= 15
            ):
                raise ProfileValidationError("gblur.planes must be an integer bitmask in [0, 15]")
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
        if stage.filter == "drawtext" and stage.composite_stage not in (
            None, "in_recording", "on_playback"
        ):
            raise ProfileValidationError(
                "drawtext composite_stage must be 'in_recording' or 'on_playback'"
            )
        if stage.filter == "drawtext" and stage.params.get("element", "clock") not in {"clock", "battery_rec"}:
            raise ProfileValidationError("drawtext element must be 'clock' or 'battery_rec'")
        if stage.filter == "drawtext" and "camera_id" in stage.params:
            if not isinstance(stage.params["camera_id"], str):
                raise ProfileValidationError("drawtext camera_id must be printable text")
            try:
                OverlaySpec(camera_id=stage.params["camera_id"], clock_epoch=0, fontsize=12)
            except ValidationError as exc:
                raise ProfileValidationError(f"invalid drawtext camera_id: {exc}") from exc
        if stage.filter == "drawtext" and stage.params.get("clock_format") is not None:
            try:
                OverlaySpec(camera_id="CAMERA", clock_epoch=0, fontsize=12,
                            clock_format=stage.params["clock_format"])
            except ValidationError as exc:
                raise ProfileValidationError(f"invalid drawtext clock_format: {exc}") from exc

    for filter_name, instances in by_filter.items():
        if len(instances) == 1 or filter_name in {"setpts", "drawtext"}:
            continue
        if filter_name == "select":
            pattern_stages = [stage for stage in instances if "pattern" in stage.params]
            schedule_stages = [stage for stage in instances if "from_schedule" in stage.params]
            if len(instances) == 2 and len(pattern_stages) == 1 and len(schedule_stages) == 1:
                continue
        elif filter_name == "colorchannelmixer":
            if len(instances) == 2 and all(
                stage.name.startswith("camera_grade_white_balance") for stage in instances
            ):
                continue
        raise ProfileValidationError(f"duplicate {filter_name!r} stage is unsupported")


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
        camera_context = None
        if camera.analog_artifacts.legacy_flat_values is not None:
            camera_context = {
                "legacy_camera_profile_schema": CAMERA_PROFILE_LEGACY_ANALOG_SCHEMA_VERSION,
            }
        camera = CameraProfile.model_validate(
            camera.model_dump(mode="python"), context=camera_context,
        )
    except (ValidationError, AttributeError, TypeError) as exc:
        raise ProfileValidationError(f"invalid profile or camera profile: {exc}") from exc
    if not _SEMVER.fullmatch(profile.identity.semver):
        raise ProfileValidationError("profile semver must be MAJOR.MINOR.PATCH")
    if not isinstance(master_frames, int) or isinstance(master_frames, bool) or master_frames < 1:
        raise ProfileValidationError("master_frames must be a positive integer")
    if camera.analog_artifacts.legacy_flat_values is not None:
        raise ProfileValidationError(
            "legacy schema-3 analog_artifacts cannot be processed; replace them with typed settings"
        )
    camera_analog = camera.analog_artifacts
    camera_effects = camera_analog.model_copy(update={"ntsc_pipe_sha256": None})
    if profile.analog_artifacts != camera_effects:
        raise ProfileValidationError(
            "derived PostProfileSpec.analog_artifacts must match CameraProfile.analog_artifacts effect controls"
        )
    _validate_stages(tuple(profile.identity.stages))
    analog_stages = [
        stage for stage in profile.identity.stages
        if stage.filter == "camera_analog_artifacts"
    ]
    if profile.analog_artifacts.active:
        camera_pin = camera_analog.ntsc_pipe_sha256
        if camera_pin is not None and profile.identity.pinned_binaries.get("ntsc-pipe") != camera_pin:
            raise ProfileValidationError(
                "derived PostProfile pinned_binaries['ntsc-pipe'] must match "
                "CameraProfile.analog_artifacts.ntsc_pipe_sha256"
            )
        if len(analog_stages) != 1 or analog_stages[0].name != "camera_analog_artifacts":
            raise ProfileValidationError(
                "active CameraProfile.analog_artifacts requires exactly one camera_analog_artifacts stage marker"
            )
        if sum(stage.filter == "setsar" for stage in profile.identity.stages) > 1:
            raise ProfileValidationError("the analog consumer supports at most one explicit setsar stage")
    elif analog_stages:
        raise ProfileValidationError(
            "camera_analog_artifacts stage marker requires active CameraProfile.analog_artifacts settings"
        )

    def require_camera_stage_in_recording(stage: PostStage, field_name: str) -> None:
        if stage.composite_stage not in (None, "in_recording"):
            raise ProfileValidationError(
                f"active CameraProfile.{field_name} stage must be composited in_recording"
            )

    def require_camera_overlay_stage_side(stage: PostStage) -> None:
        # Overlay placement is an explicit CameraProfile choice: in-recording text is
        # burned into this degradation; on-playback text belongs to the delivery layer.
        requested_side = camera.overlay_model.placement or "in_recording"
        actual_side = stage.composite_stage or "in_recording"
        if actual_side != requested_side:
            raise ProfileValidationError(
                "active CameraProfile.overlay_model placement must match its drawtext stage"
            )

    scale = next((s for s in profile.identity.stages if s.filter == "scale"), None)
    fps_stage = next((s for s in profile.identity.stages if s.filter == "fps"), None)
    pattern_stages = [
        stage for stage in profile.identity.stages
        if stage.filter == "select" and "pattern" in stage.params
    ]
    schedule_selects = [
        stage for stage in profile.identity.stages
        if stage.filter == "select" and "from_schedule" in stage.params
    ]
    if scale is None or len(schedule_selects) != 1:
        raise ProfileValidationError("profile requires scale and exactly one select schedule stage")
    if camera.decimation_pattern is None:
        if fps_stage is None or pattern_stages:
            raise ProfileValidationError("profile requires uniform fps decimation when CameraProfile.decimation_pattern is None")
    elif (fps_stage is not None or len(pattern_stages) != 1
          or pattern_stages[0].params.get("pattern") != camera.decimation_pattern):
        raise ProfileValidationError(
            "profile cadence stage must match CameraProfile.decimation_pattern and replace fps"
        )
    if camera.decimation_pattern is not None:
        require_camera_stage_in_recording(pattern_stages[0], "decimation_pattern")
    filter_names = [stage.filter for stage in profile.identity.stages]
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
    if profile.encode.bitrate_kbps != camera.bitrate_kbps:
        raise ProfileValidationError("derived encode bitrate_kbps must match CameraProfile.bitrate_kbps")
    if not math.isclose(profile.encode.block_artifacts, camera.block_artifacts):
        raise ProfileValidationError("derived encode block_artifacts must match CameraProfile.block_artifacts")
    if camera.block_artifacts > 0 and camera.codec != "libx264":
        raise ProfileValidationError(
            f"block_artifacts is supported only by libx264; codec {camera.codec!r} has no quantizer-floor mapping"
        )
    if (
        camera.noise_model.active or camera.chroma_smearing > 0 or camera.sharpening > 0
    ) and not profile.encode.pix_fmt.startswith("yuv"):
        raise ProfileValidationError(
            "active luma/chroma CameraProfile effects require a YUV pix_fmt"
        )
    if not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in (width, height)):
        raise ProfileValidationError("scale dimensions must be positive integers")
    for stage in profile.identity.stages:
        if stage.filter == "scale" and (("w" in stage.params and stage.params["w"] != width)
                                         or ("h" in stage.params and stage.params["h"] != height)):
            raise ProfileValidationError("scale stage dimensions must match PostProfileSpec.storage_resolution")
    output_fps = _finite(profile.output_fps, "output fps")
    if output_fps <= 0 or not math.isclose(output_fps, camera.fps):
        raise ProfileValidationError("profile output_fps must match CameraProfile.fps")
    if fps_stage is not None and "fps" in fps_stage.params:
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
    if camera.decimation_pattern is not None:
        terms = _parse_decimation_pattern(camera.decimation_pattern)
        pattern_mean = Fraction(sum(terms), len(terms))
        expected_span = source_rate / output_rate
        if pattern_mean != expected_span:
            raise ProfileValidationError(
                f"decimation_pattern mean span {pattern_mean} does not equal source rate "
                f"{source_rate} fps divided by output rate {output_rate} fps"
            )
        selected_count = 0
        source_index = 0
        term_index = 0
        while source_index < master_frames:
            selected_count += 1
            source_index += terms[term_index]
            term_index = (term_index + 1) % len(terms)
        if selected_count != decimated:
            raise ProfileValidationError(
                f"decimation_pattern selects {selected_count} frames from {master_frames} source frames, "
                f"but uniform {source_rate} fps to {output_rate} fps decimation predicts {decimated}; "
                "the finite master length does not match this cadence phase"
            )

    if camera.noise_model.active:
        noise_stage = next((stage for stage in profile.identity.stages
                            if stage.name == "camera_noise_model" and stage.filter == "noise"), None)
        if noise_stage is None:
            raise ProfileValidationError("derived profile is missing active CameraProfile.noise_model")
        require_camera_stage_in_recording(noise_stage, "noise_model")
        expected_noise = {
            "c0s": camera.noise_model.luma,
            "c1s": camera.noise_model.chroma,
            "c2s": camera.noise_model.chroma,
        }
        flags = "+".join(flag for enabled, flag in (
            (camera.noise_model.temporal, "t"), (camera.noise_model.uniform, "u"),
        ) if enabled)
        for key in ("c0f", "c1f", "c2f"):
            expected_noise[key] = flags or None
        if any(noise_stage.params.get(key) != value for key, value in expected_noise.items()):
            raise ProfileValidationError("derived noise stage disagrees with CameraProfile.noise_model")

    grade = camera.grade_white_balance
    if grade.temperature_active:
        temperature_stage = next((stage for stage in profile.identity.stages
                                  if stage.name == "camera_grade_white_balance_temperature"), None)
        if (temperature_stage is None or temperature_stage.filter != "colortemperature"
                or temperature_stage.params.get("temperature") != grade.temperature_k):
            raise ProfileValidationError("derived colortemperature stage disagrees with CameraProfile.grade_white_balance")
        require_camera_stage_in_recording(temperature_stage, "grade_white_balance")
    if grade.gains_active:
        mixer_stages = [stage for stage in profile.identity.stages
                        if stage.name.startswith("camera_grade_white_balance")
                        and stage.filter == "colorchannelmixer"]
        if not mixer_stages:
            raise ProfileValidationError("derived colorchannelmixer stage disagrees with CameraProfile.grade_white_balance")
        for mixer_stage in mixer_stages:
            require_camera_stage_in_recording(mixer_stage, "grade_white_balance")
        for key, expected in (("rr", grade.red_gain), ("gg", grade.green_gain), ("bb", grade.blue_gain)):
            actual = math.prod(_finite(stage.params.get(key, 1), f"{stage.name}.{key}")
                               for stage in mixer_stages)
            if not math.isclose(actual, 1 if expected is None else expected, rel_tol=1e-9, abs_tol=1e-9):
                raise ProfileValidationError("derived colorchannelmixer gains disagree with CameraProfile.grade_white_balance")
    if grade.eq_active:
        eq_stage = next((stage for stage in profile.identity.stages
                         if stage.name == "camera_grade_white_balance_eq" and stage.filter == "eq"), None)
        expected_eq = {
            "brightness": 0 if grade.brightness is None else grade.brightness,
            "contrast": 1 if grade.contrast is None else grade.contrast,
            "saturation": 1 if grade.saturation is None else grade.saturation,
        }
        if eq_stage is None or any(
            not math.isclose(_finite(eq_stage.params.get(key, default), f"eq.{key}"), value)
            for key, value in expected_eq.items()
            for default in (0 if key == "brightness" else 1,)
        ):
            raise ProfileValidationError("derived eq stage disagrees with CameraProfile.grade_white_balance")
        require_camera_stage_in_recording(eq_stage, "grade_white_balance")

    if camera.chroma_smearing > 0:
        smear_stage = next((stage for stage in profile.identity.stages
                            if stage.name == "camera_chroma_smearing"), None)
        if (smear_stage is None or smear_stage.filter != "gblur"
                or not math.isclose(_finite(smear_stage.params.get("sigma"), "gblur.sigma"), camera.chroma_smearing / 2)
                or smear_stage.params.get("sigmaV") != 0
                or smear_stage.params.get("planes") != 6):
            raise ProfileValidationError("derived gblur stage disagrees with CameraProfile.chroma_smearing")
        require_camera_stage_in_recording(smear_stage, "chroma_smearing")
    if camera.sharpening > 0:
        sharpen_stage = next((stage for stage in profile.identity.stages
                              if stage.name == "camera_sharpening"), None)
        if (sharpen_stage is None or sharpen_stage.filter != "unsharp"
                or not math.isclose(_finite(sharpen_stage.params.get("luma_amount"), "unsharp.luma_amount"), camera.sharpening)
                or sharpen_stage.params.get("luma_msize_x") != 5
                or sharpen_stage.params.get("luma_msize_y") != 5):
            raise ProfileValidationError("derived unsharp stage disagrees with CameraProfile.sharpening")
        require_camera_stage_in_recording(sharpen_stage, "sharpening")

    overlay_model = camera.overlay_model
    if any(getattr(overlay_model, key) is not None for key in (
        "camera_id_text", "clock_epoch", "clock_format", "fontsize", "x", "y", "placement",
    )):
        if profile.overlay is None:
            raise ProfileValidationError("active CameraProfile.overlay_model requires PostProfileSpec.overlay")
        expected_overlay = {
            "camera_id": overlay_model.camera_id_text,
            "clock_epoch": overlay_model.clock_epoch,
            "clock_format": overlay_model.clock_format,
            "fontsize": overlay_model.fontsize,
            "x": overlay_model.x,
            "y": overlay_model.y,
            "placement": overlay_model.placement,
        }
        for key, expected in expected_overlay.items():
            if expected is not None and getattr(profile.overlay, key) != expected:
                raise ProfileValidationError(f"derived overlay {key} disagrees with CameraProfile.overlay_model")
        overlay_stage = next((stage for stage in profile.identity.stages
                              if stage.name == "camera_overlay_model" and stage.filter == "drawtext"), None)
        if overlay_stage is None:
            raise ProfileValidationError("derived profile is missing active CameraProfile.overlay_model")
        require_camera_overlay_stage_side(overlay_stage)
        for key, expected in (("camera_id", profile.overlay.camera_id),
                              ("clock_epoch", profile.overlay.clock_epoch),
                              ("clock_format", profile.overlay.clock_format),
                              ("fontsize", profile.overlay.fontsize),
                              ("x", profile.overlay.x), ("y", profile.overlay.y)):
            if expected is not None and overlay_stage.params.get(key) != expected:
                raise ProfileValidationError(f"derived drawtext {key} disagrees with CameraProfile.overlay_model")
        if overlay_stage.composite_stage != profile.overlay.placement:
            raise ProfileValidationError("derived drawtext placement disagrees with CameraProfile.overlay_model")

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
        clock_stages = [
            stage for stage in profile.identity.stages
            if stage.filter == "drawtext" and stage.params.get("element", "clock") == "clock"
        ]
        indicator_stages = [
            stage for stage in profile.identity.stages
            if stage.filter == "drawtext" and stage.params.get("element", "clock") == "battery_rec"
        ]
        if len(clock_stages) != 1:
            raise ProfileValidationError("profile requires exactly one clock overlay element")
        if len(indicator_stages) > 1:
            raise ProfileValidationError("profile supports at most one battery_rec overlay element")
        if bool(indicator_stages) != (profile.overlay.indicator is not None):
            raise ProfileValidationError(
                "PostProfileSpec.overlay.indicator and battery_rec drawtext stage must be present together"
            )
        # `overlay.placement` states the intent; `composite_stage` marks the stage that
        # realises it. They are two spellings of one decision, so they must agree, or the
        # clock is silently burned in after the profile said it would be composited --
        # which is the failure adapters/export/CONTRACT.md exists to prevent.
        clock_side = clock_stages[0].composite_stage or "in_recording"
        if clock_side != profile.overlay.placement:
            raise ProfileValidationError(
                f"clock drawtext composite_stage {clock_side!r} disagrees with "
                f"PostProfileSpec.overlay.placement {profile.overlay.placement!r}"
            )
        # PostProfileOverlayIndicator.placement is Literal["in_recording"]: a real DVR
        # burns its REC graphic into the recording. Widening that is export CONTRACT D2
        # and is the creator's decision, so it is enforced rather than assumed here.
        for stage in indicator_stages:
            if (stage.composite_stage or "in_recording") != "in_recording":
                raise ProfileValidationError(
                    "battery_rec overlay must be composited in_recording; see "
                    "adapters/export/CONTRACT.md D2"
                )
        for stage in profile.identity.stages:
            if stage.filter != "drawtext":
                continue
            element = stage.params.get("element", "clock")
            if element == "battery_rec":
                continue
            fields = {"camera_id": profile.overlay.camera_id, "clock_epoch": profile.overlay.clock_epoch,
                      "clock_format": profile.overlay.clock_format,
                      "fontsize": profile.overlay.fontsize, "x": profile.overlay.x, "y": profile.overlay.y}
            for key, expected in fields.items():
                if key in stage.params and stage.params[key] != expected:
                    raise ProfileValidationError(f"drawtext {key} disagrees with PostProfileSpec.overlay")
    order = {
        "scale": 0, "fps": 1, "select": 2, "loop": 3, "drawtext": 5,
        "colortemperature": 7, "colorchannelmixer": 8, "eq": 9,
        "gblur": 10, "noise": 11, "unsharp": 12,
        "camera_analog_artifacts": 13, "setsar": 14,
    }
    ranks = [
        4 if stage.filter == "setpts" and stage.params.get("from_schedule") == "clock"
        else 6 if stage.filter == "setpts"
        else 1 if stage.filter == "select" and "pattern" in stage.params
        else order[stage.filter]
        for stage in profile.identity.stages
    ]
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
    clock_steps = list(schedule_clock_steps(events, decimated_frames=decimated))

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


# Camera ids main's quoted drawtext serialization handled correctly: none of these needs
# escaping at either FFmpeg parsing layer. Anything else uses the two-layer escaping.
_LEGACY_DRAWTEXT_TEXT = re.compile(r"[A-Za-z0-9 _.#()/-]+")


def _escape_filter_value(value: str) -> str:
    """Escape a path for FFmpeg's nested filter option grammar."""
    value = value.replace("\\", "/")
    for char in ("\\", ":", "'", ",", ";", "[", "]"):
        value = value.replace(char, "\\" + char)
    return value


def _escape_drawtext_layers(value: str) -> str:
    """Escape drawtext's option value, then the containing filtergraph value.

    ``ffmpeg -vf`` receives argv directly, so there is no shell-escaping layer.
    Drawtext expansion escaping is applied by the caller before these two layers.
    """
    option_value = value.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return "".join("\\" + char if char in "\\,;[]'" else char for char in option_value)


def _escape_drawtext_literal(value: str) -> str:
    """Preserve literal input text with drawtext's normal expansion enabled."""
    # This FFmpeg build's normal expansion treats ``%%`` as a stray percent;
    # drawtext documents backslash-next-character escaping, so use ``\%``.
    expansion_safe = value.replace("\\", "\\\\").replace("%", r"\%")
    return _escape_drawtext_layers(expansion_safe)


def serialize_drawtext_filter(
    *,
    font_path: str,
    content: str,
    x: int | str,
    y: int | str,
    fontsize: int,
    quote_text: bool,
) -> str:
    """Serialize shared drawtext options for the degrade graph and fit measurement."""
    font = _escape_filter_value(font_path)
    text_argument = f"text='{content}'" if quote_text else f"text={content}"
    return (
        f"drawtext=fontfile='{font}':{text_argument}:x={x}:y={y}:fontsize={fontsize}:"
        "fontcolor=white:box=1:boxcolor=black@0.4"
    )


def serialize_clock_drawtext_filter(
    *,
    font_path: str,
    overlay: OverlaySpec,
    x: int | str | None = None,
    y: int | str | None = None,
    fontsize: int | None = None,
    clock_epoch: int | None = None,
) -> str:
    """Serialize a clock with the same legacy/two-layer escaping used by degradation."""
    epoch = overlay.clock_epoch if clock_epoch is None else clock_epoch
    position_x = overlay.x if x is None else x
    position_y = overlay.y if y is None else y
    size = overlay.fontsize if fontsize is None else fontsize
    if overlay.clock_format is None and _LEGACY_DRAWTEXT_TEXT.fullmatch(overlay.camera_id):
        # Preserve the quoted legacy graph bytes for values that need no escaping.
        text = _escape_filter_value(overlay.camera_id)
        content = f"{text}  %{{pts\\:gmtime\\:{epoch}}}"
        quote_text = True
    else:
        text = _escape_drawtext_literal(overlay.camera_id)
        if overlay.clock_format is None:
            clock_expansion = f"%{{pts:gmtime:{epoch}}}"
        else:
            format_argument = overlay.clock_format.replace(":", r"\:")
            clock_expansion = f"%{{pts:gmtime:{epoch}:{format_argument}}}"
        content = f"{text}  {_escape_drawtext_layers(clock_expansion)}"
        quote_text = False
    return serialize_drawtext_filter(
        font_path=font_path,
        content=content,
        x=position_x,
        y=position_y,
        fontsize=size,
        quote_text=quote_text,
    )


def serialize_drawtext_literal_filter(
    *,
    font_path: str,
    text: str,
    x: int | str,
    y: int | str,
    fontsize: int,
    element: str = "clock",
    camera_id: str | None = None,
    clock_format: str | None = None,
) -> str:
    """Serialize a static sample using the same escaping policy as its graph element."""
    if element == "battery_rec":
        content = _escape_filter_value(text)
        quote_text = True
    elif (
        element == "clock"
        and clock_format is None
        and camera_id is not None
        and _LEGACY_DRAWTEXT_TEXT.fullmatch(camera_id)
        and text.startswith(f"{camera_id}  ")
    ):
        # The production graph quotes the legacy camera id and leaves the gmtime
        # expansion to FFmpeg. Here the already-expanded sample is quoted the same
        # way, with its literal clock value following that identical camera prefix.
        clock_text = text[len(camera_id) + 2:].replace(":", r"\:")
        content = f"{_escape_filter_value(camera_id)}  {clock_text}"
        quote_text = True
    else:
        content = _escape_drawtext_literal(text)
        quote_text = False
    return serialize_drawtext_filter(
        font_path=font_path,
        content=content,
        x=x,
        y=y,
        fontsize=fontsize,
        quote_text=quote_text,
    )


def schedule_clock_steps(
    schedule: Iterable[ArtifactScheduleEntry], *, decimated_frames: int | None = None,
) -> tuple[tuple[int, int], ...]:
    """Derive schedule-clock jumps on the post-loop output frame index.

    Schedule positions are post-decimation frame indices. ``select`` removes both
    drop and freeze windows, then ``loop`` inserts held freeze frames before
    ``clock_timebase`` evaluates N. Thus freezes move a later drop boundary, while
    only drop windows add missing frame counts to the clock.
    """
    events = tuple(sorted(schedule, key=lambda event: (event.start_frame, event.type)))
    frame_count = decimated_frames
    if frame_count is None:
        frame_count = max((event.start_frame + event.count for event in events), default=0) + 1
    occupied: set[int] = set()
    drops: list[ArtifactScheduleEntry] = []
    freezes: list[ArtifactScheduleEntry] = []
    for event in events:
        indices = set(range(event.start_frame, event.start_frame + event.count))
        if event.start_frame + event.count > frame_count:
            raise ProfileValidationError(
                f"{event.type} window {event.start_frame}-{event.start_frame + event.count - 1} "
                f"falls outside decimated range 0-{frame_count - 1}"
            )
        if occupied & indices:
            raise ProfileValidationError("drop/freeze schedule windows may not overlap")
        occupied.update(indices)
        (drops if event.type == "drop" else freezes).append(event)

    output_to_source = [index for index in range(frame_count) if index not in occupied]
    for freeze in freezes:
        held = freeze.start_frame - 1
        try:
            position = output_to_source.index(held)
        except ValueError as exc:
            raise ProfileValidationError(
                f"freeze at {freeze.start_frame} has no surviving previous frame"
            ) from exc
        output_to_source[position + 1:position + 1] = [held] * freeze.count

    steps: list[tuple[int, int]] = []
    cumulative = 0
    for drop in drops:
        first_after = drop.start_frame + drop.count
        boundary = next(
            (index for index, source_index in enumerate(output_to_source)
             if source_index >= first_after),
            None,
        )
        if boundary is None:
            raise ProfileValidationError("drop schedule has no clock boundary after the dropped interval")
        cumulative += drop.count
        steps.append((boundary, cumulative))
    return tuple(steps)


def _clock_expr(steps: tuple[tuple[int, int], ...], frame_index: str = "N") -> str:
    expr = "0"
    for boundary, cumulative in steps:
        expr = f"if(gte({frame_index},{boundary}),{cumulative},{expr})"
    return expr


def clock_pts_expression(
    schedule: Iterable[ArtifactScheduleEntry], *, frame_index: str = "N",
) -> str:
    """Return the same clock_timebase PTS expression for a caller-supplied frame index."""
    return f"{frame_index}+{_clock_expr(schedule_clock_steps(schedule), frame_index)}"


def _ffmpeg_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    return format(value, ".12g")


def _build_filter_graph_slice(
    profile: ValidatedPostProfile,
    *,
    shot_id: str,
    degrade_seed: int,
    font_path: str | None = None,
    stage_start: int,
    stage_stop: int,
    final_fps: bool,
    final_sar: bool,
) -> str:
    if degrade_seed < 0:
        raise ProfileValidationError("degrade_seed must be nonnegative")
    if font_path is not None and (
        PurePosixPath(font_path).is_absolute()
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", font_path)
        or any(part in {"", ".."} for part in font_path.split("/"))
    ):
        raise ProfileValidationError(
            "font_path must use [A-Za-z0-9_./-] path segments without a leading / or .. segment"
        )
    stages = profile.profile.identity.stages
    parts: list[str] = []
    rgb_stage_indexes = [
        index for index, stage in enumerate(stages)
        if stage.filter in {"colortemperature", "colorchannelmixer"}
    ]
    last_rgb_stage = max(rgb_stage_indexes, default=-1)
    yuv_stage_indexes = [
        index for index, stage in enumerate(stages)
        if index > last_rgb_stage and stage.filter in {"eq", "gblur", "noise", "unsharp"}
    ]
    pixel_format = profile.profile.encode.pix_fmt
    # The RGB24 tape input can pull upstream filters into planar RGB. Component
    # noise and chroma blur need the same YUV planes they use in the camera chain.
    force_yuv_before_tape = profile.profile.analog_artifacts.active and pixel_format.startswith("yuv")
    format_index = (
        yuv_stage_indexes[0]
        if yuv_stage_indexes and (rgb_stage_indexes or force_yuv_before_tape)
        else None
    )
    for index, stage in enumerate(stages):
        if index < stage_start or index >= stage_stop:
            continue
        if format_index is not None and index == format_index:
            parts.append(f"format=pix_fmts={pixel_format}")
        name, f, p = stage.name, stage.filter, stage.params
        if f == "camera_analog_artifacts":
            raise ProfileValidationError(
                "camera_analog_artifacts is an external stage marker and cannot be emitted as an FFmpeg filter"
            )
        # FR-21 declares which side of degradation an overlay sits on. A stage marked
        # `on_playback` is composited by the delivery render (adapters/export/CONTRACT.md)
        # and must not be burned into the recording, because anything burned in before
        # MiniMax H3 is regenerated by it: measured 2026-11-15, the clock was erased
        # outright at 352x288 and 640x480 and hallucinated non-monotonically at 512x384.
        if stage.composite_stage == "on_playback":
            continue
        if f == "scale":
            parts.append(f"scale={profile.profile.storage_resolution[0]}:"
                         f"{profile.profile.storage_resolution[1]}:flags={p.get('flags', 'bicubic')}")
        elif f == "fps":
            parts.append(f"fps={profile.profile.output_fps}")
        elif f == "select":
            if "pattern" in p:
                spans = _parse_decimation_pattern(p["pattern"])
                cycle = sum(spans)
                offsets: list[int] = []
                offset = 0
                for span in spans:
                    offsets.append(offset)
                    offset += span
                terms = [f"eq(mod(n,{cycle}),{item})" for item in offsets]
                parts.append("select='" + "+".join(terms) + "'")
            else:
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
            if font_path is None:
                raise ProfileValidationError("drawtext stage requires a contained overlay font path")
            element = p.get("element", "clock")
            if element == "clock":
                if overlay.placement == "on_playback":
                    # Unreachable for a well-formed profile: the loop skips on_playback
                    # stages above. Reaching here means the stage was not marked, which
                    # validation should already have refused.
                    raise ProfileValidationError(
                        "clock overlay is placed on_playback but its stage is not marked "
                        "composite_stage='on_playback'"
                    )
                parts.append(serialize_clock_drawtext_filter(
                    font_path=font_path,
                    overlay=overlay,
                    x=overlay.x,
                    y=overlay.y,
                    fontsize=overlay.fontsize,
                ))
                continue
            elif element == "battery_rec":
                indicator = overlay.indicator
                if indicator is None:
                    raise ProfileValidationError("battery_rec drawtext stage requires an indicator specification")
                content = _escape_filter_value("REC [||||]")
                quote_text = True
                x, y, fontsize = indicator.x, indicator.y, indicator.fontsize
            else:  # checked when the persisted profile is validated
                raise ProfileValidationError(f"unsupported drawtext element {element!r}")
            parts.append(serialize_drawtext_filter(
                font_path=font_path,
                content=content,
                x=x,
                y=y,
                fontsize=fontsize,
                quote_text=quote_text,
            ))
        elif f == "eq":
            parts.append(f"eq=brightness={p.get('brightness', 0)}:contrast={p.get('contrast', 1)}:saturation={p.get('saturation', 1)}")
        elif f == "colortemperature":
            parts.append(f"colortemperature=temperature={_ffmpeg_number(p.get('temperature', 6500))}")
        elif f == "colorchannelmixer":
            options = [f"{key}={_ffmpeg_number(value)}" for key, value in p.items()]
            parts.append("colorchannelmixer=" + ":".join(options))
        elif f == "gblur":
            parts.append(
                f"gblur=sigma={_ffmpeg_number(p.get('sigma', 0.5))}:"
                f"sigmaV={_ffmpeg_number(p.get('sigmaV', -1))}:planes={p.get('planes', 15)}"
            )
        elif f == "noise":
            maximum = p.get("all_seed_max", INT32_MAX)
            if not isinstance(maximum, int) or maximum < 1:
                raise ProfileValidationError("noise all_seed_max must be a positive integer")
            seed = derive_seed(shot_id, degrade_seed, name, maximum)
            if any(key in p for key in ("c0s", "c1s", "c2s", "c0f", "c1f", "c2f")):
                # In FFmpeg 8.1.1, noise's init path overwrites each cN_seed
                # with all_seed (or a hard-coded 123457 when all_seed is unset).
                # Set the supported all_seed option so changes to the derived seed
                # reach all active component streams.
                options = [f"all_seed={seed}"]
                options.extend(f"c{component}s={p.get(f'c{component}s', 0)}"
                               for component in range(3))
                for component in range(3):
                    flag = p.get(f"c{component}f")
                    if flag is not None:
                        options.append(f"c{component}f={flag}")
                parts.append("noise=" + ":".join(options))
            else:
                parts.append(f"noise=alls={p.get('alls', 12)}:allf={p.get('allf', 't+u')}:all_seed={seed}")
        elif f == "unsharp":
            parts.append(f"unsharp={p.get('luma_msize_x', 5)}:{p.get('luma_msize_y', 5)}:{p.get('luma_amount', 0.8)}")
        elif f == "setsar":
            parts.append(f"setsar={str(p.get('sar', profile.profile.sample_aspect_ratio)).replace(':', '/')}")
        else:  # validated against the closed map above
            raise ProfileValidationError(f"unsupported FFmpeg filter {f!r}")
    selected_stages = stages[stage_start:stage_stop]
    if final_fps and any(stage.filter == "select" and "pattern" in stage.params for stage in selected_stages):
        # select preserves source-frame durations. The final selected frame can
        # remain 1/source_fps long even after setpts gives it the correct output
        # PTS; a final fps pass restores the uniform duration without changing count.
        parts.append(f"fps={profile.profile.output_fps}")
    if (final_sar and not any(stage.filter == "setsar" for stage in stages)
            and profile.profile.sample_aspect_ratio not in ("1/1", "1:1")):
        parts.append(f"setsar={profile.profile.sample_aspect_ratio.replace(':', '/')}")
    return ",".join(parts)


def build_filter_graph(
    profile: ValidatedPostProfile,
    *,
    shot_id: str,
    degrade_seed: int,
    font_path: str | None = None,
) -> str:
    """Build the deterministic single-process graph for profiles without NTSC tape."""
    stages = profile.profile.identity.stages
    if profile.profile.analog_artifacts.active:
        raise ProfileValidationError(
            "active analog_artifacts requires build_filter_graph_parts and the ntsc-pipe process"
        )
    return _build_filter_graph_slice(
        profile,
        shot_id=shot_id,
        degrade_seed=degrade_seed,
        font_path=font_path,
        stage_start=0,
        stage_stop=len(stages),
        final_fps=True,
        final_sar=True,
    )


def build_filter_graph_parts(
    profile: ValidatedPostProfile,
    *,
    shot_id: str,
    degrade_seed: int,
    font_path: str | None = None,
) -> tuple[str, str]:
    """Split FFmpeg work around the typed, dimension-preserving external NTSC stage."""
    stages = profile.profile.identity.stages
    marker_indexes = [
        index for index, stage in enumerate(stages)
        if stage.filter == "camera_analog_artifacts"
    ]
    if not profile.profile.analog_artifacts.active or len(marker_indexes) != 1:
        raise ProfileValidationError(
            "build_filter_graph_parts requires one active camera_analog_artifacts stage marker"
        )
    marker_index = marker_indexes[0]
    prefix = _build_filter_graph_slice(
        profile,
        shot_id=shot_id,
        degrade_seed=degrade_seed,
        font_path=font_path,
        stage_start=0,
        stage_stop=marker_index,
        final_fps=True,
        final_sar=False,
    )
    suffix = _build_filter_graph_slice(
        profile,
        shot_id=shot_id,
        degrade_seed=degrade_seed,
        font_path=font_path,
        stage_start=marker_index + 1,
        stage_stop=len(stages),
        final_fps=False,
        final_sar=False,
    )
    if not any(stage.filter == "setsar" for stage in stages):
        sample_aspect_ratio = profile.profile.sample_aspect_ratio.replace(":", "/")
        suffix = f"{suffix}," if suffix else ""
        suffix += f"setsar={sample_aspect_ratio}"
    return prefix, suffix


def camera_profile_impact(before: CameraProfile, after: CameraProfile,
                           shots: Iterable[Shot]) -> tuple[Shot, ...]:
    """Delegate profile invalidation classification to the domain dependency graph."""
    return revision_impact(before, after, shots)
