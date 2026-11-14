from __future__ import annotations

import json
import math
import ntpath
import re
from decimal import Decimal
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from orchestrator.domain.seeds import derive_seed

_SEED_MAX = min(2**53 - 1, 2**64 - 1)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BASELINE = (8.0, 384, 288, 24, 20)


class H3RequestError(ValueError):
    """Invalid H3 request or graph input; not a retryable job error."""


class H3SetupError(RuntimeError):
    """H3 runtime is missing or not ready; not a retryable job error."""


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{field} must contain only letters, digits, underscore, and hyphen")
    return value


def _safe_relative_path(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} must be a nonempty project-relative path")
    if "\\" in value or value.startswith("/") or ntpath.splitdrive(value)[0] or ":" in value:
        raise ValueError(f"{field} must be a project-relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} cannot contain empty, dot, or traversal path components")
    if any(part.upper() in _RESERVED for part in parts):
        raise ValueError(f"{field} contains a reserved path component")
    return value


def _safe_graph_name(value: str, field: str) -> str:
    _safe_relative_path(value, field)
    if any(not _ID.fullmatch(part) for part in value.split("/")):
        raise H3RequestError(f"{field} components may contain only letters, digits, underscore, and hyphen")
    return value


def _safe_input_filename(value: str) -> str:
    if (not isinstance(value, str) or "\x00" in value or "/" in value or "\\" in value
            or ntpath.splitdrive(value)[0] or ":" in value
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.mp4", value)):
        raise H3RequestError("input_filename must be a contained single basename with a safe .mp4 extension")
    if value.split(".", 1)[0].upper() in _RESERVED:
        raise H3RequestError("input_filename contains a reserved Windows path component")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class H3Request(_StrictModel):
    shot_id: str
    shot_version: StrictInt = Field(ge=1)
    run_id: str
    video_path: str
    source_sha256: str
    source_output_id: str
    prompt: str
    degrade_seed: StrictInt = Field(default=0, ge=0)
    start_seconds: float = Field(default=0.0, ge=0.0)
    duration_seconds: float = 8.0
    width: StrictInt = 384
    height: StrictInt = 288
    fps: StrictInt = 24
    steps: StrictInt = 20
    experimental: StrictBool = False

    @field_validator("shot_id", "run_id", "source_output_id")
    @classmethod
    def _identifier(cls, value: str, info: Any) -> str:
        return _safe_id(value, info.field_name)

    @field_validator("video_path")
    @classmethod
    def _video_path(cls, value: str) -> str:
        return _safe_relative_path(value, "video_path")

    @field_validator("source_sha256")
    @classmethod
    def _source_hash(cls, value: str) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("prompt must be nonblank and contain no NUL")
        if len(value.encode("utf-8")) > 65536:
            raise ValueError("UTF-8 prompt exceeds the 65536-byte adapter input limit")
        return value

    @field_validator("start_seconds", "duration_seconds", mode="before")
    @classmethod
    def _numeric_time(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("time values must be finite numbers")  # noqa: TRY004
        if not math.isfinite(value):
            raise ValueError("time values must be finite")
        return value

    @model_validator(mode="after")
    def _request_bounds(self) -> H3Request:
        duration = Decimal(str(self.duration_seconds))
        if not Decimal("0.2") <= duration <= Decimal(30) or duration * 10 != (duration * 10).to_integral_value():
            raise ValueError("duration_seconds must be in [0.2, 30.0] in 0.1 second increments")
        for name, value in (("width", self.width), ("height", self.height)):
            if not 32 <= value <= 16384 or value % 32:
                raise ValueError(f"{name} must be a multiple of 32 in [32, 16384]")
        if not 1 <= self.fps <= 120:
            raise ValueError("fps must be an integer in [1, 120]")
        if not 1 <= self.steps <= 10000:
            raise ValueError("steps must be an integer in [1, 10000]")
        frames = duration * self.fps
        if frames != frames.to_integral_value() or frames < 5:
            raise ValueError("duration_seconds * fps must be an exact integer of at least 5 frames")
        is_baseline = (self.duration_seconds, self.width, self.height, self.fps, self.steps) == _BASELINE
        if not self.experimental and not is_baseline:
            raise ValueError("nonbaseline H3 settings require experimental=True")
        return self

    @property
    def seed(self) -> int:
        """Deterministic noise seed clamped to the live RandomNoise integer range."""
        return derive_seed(self.shot_id, self.degrade_seed, "minimax_h3_ambience", max_value=_SEED_MAX)


class H3Result(_StrictModel):
    run_id: str
    av_path: str
    av_sha256: str
    wav_path: str
    wav_sha256: str
    report_path: str
    report_sha256: str
    frame_count: StrictInt = Field(ge=1)
    fps: StrictInt = Field(ge=1)
    audio_sample_rate: StrictInt = Field(ge=1)
    audio_channels: StrictInt = Field(ge=1)
    audio_samples_per_channel: StrictInt = Field(ge=1)
    seed: StrictInt = Field(ge=0, le=_SEED_MAX)
    experimental: StrictBool

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        return _safe_id(value, "run_id")

    @field_validator("av_path", "wav_path", "report_path")
    @classmethod
    def _artifact_path(cls, value: str, info: Any) -> str:
        return _safe_relative_path(value, info.field_name)

    @field_validator("av_sha256", "wav_sha256", "report_sha256")
    @classmethod
    def _artifact_hash(cls, value: str, info: Any) -> str:
        if not _HASH.fullmatch(value):
            raise ValueError(f"{info.field_name} must be 64 lowercase hexadecimal characters")
        return value


#: The `aspect_ratio` options the pinned MiniMaxH3Easy node publishes. The graph builder is
#: pure -- it has no live schema -- so this mirrors the installed list. If the two ever
#: drift, `_validate_live_schema` refuses the run early and names the rejected value, which
#: is exactly how the 352x288 case surfaced rather than failing after a 28 GB model load.
_ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9")


def _nearest_aspect_ratio(width: int, height: int) -> str:
    """Select the offered aspect ratio closest to the frame's shape.

    Storage dimensions are not the picture's shape. CIF 352x288 is 11:9 in square pixels
    but must *display* 4:3 (the PRD's aspect-ratio rule), which is how the production
    clips are already encoded: SAR 12:11, DAR 4:3. The previous derivation emitted a
    literal `WIDTH:HEIGHT` for anything that was not exactly 4:3, and such a string can
    never appear in a fixed option list -- not even for a square frame, where it produced
    `512:512` instead of `1:1`. Only exactly-4:3 input ever worked.

    Distance is measured on the log of the ratio so a given proportional error counts the
    same either side of square. Comparing raw ratios would make landscape errors look
    larger than the identical portrait error and could pick `16:9` for a 9:16 frame.
    """
    target = math.log(width / height)

    def distance(option: str) -> float:
        offered_w, offered_h = option.split(":")
        return abs(math.log(int(offered_w) / int(offered_h)) - target)

    return min(_ASPECT_RATIOS, key=distance)


def build_api_graph(
    request: H3Request,
    *,
    models: dict[str, str],
    seed: int,
    input_filename: str = "model_proxy.mp4",
    output_prefix: str = "h3_candidate",
) -> dict[str, dict[str, Any]]:
    """Build a fresh, pure API graph for the pinned Easy video+prompt workflow."""
    request = H3Request.model_validate(request.model_dump(mode="python"))
    if not isinstance(models, dict) or set(models) != {"diffusion", "text_encoder", "video_vae", "audio_vae"}:
        raise H3RequestError("models must provide exactly diffusion, text_encoder, video_vae, and audio_vae")
    checked_models: dict[str, str] = {}
    for role, name in models.items():
        if not isinstance(name, str) or not name or ntpath.basename(name) != name or "/" in name or "\\" in name or "\x00" in name:
            raise H3RequestError(f"models[{role!r}] must be a catalog file basename")
        checked_models[role] = name
    _safe_input_filename(input_filename)
    _safe_graph_name(output_prefix, "output_prefix")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= _SEED_MAX:
        raise H3RequestError(f"seed must be an integer in [0, {_SEED_MAX}]")
    resource = {"type": "video", "tag": "<Video 1>", "name": input_filename,
                "asset": {"filename": input_filename, "subfolder": "", "storage": "input"}}
    prompt_resources = json.dumps([resource], ensure_ascii=False, separators=(",", ":"))
    media_state = json.dumps({"images": [], "audios": [], "videos": [{"filename": input_filename}]},
                             ensure_ascii=False, separators=(",", ":"))
    aspect_ratio = _nearest_aspect_ratio(request.width, request.height)
    return {
        "1": {"class_type": "MiniMaxH3EasyLoader", "inputs": {"fl2va_model": "None", "ref2va_model": checked_models["diffusion"], "text_encoder": checked_models["text_encoder"], "video_vae": checked_models["video_vae"], "audio_vae": checked_models["audio_vae"]}},
        "2": {"class_type": "MiniMaxH3Easy", "inputs": {"mode": "reference", "prompt": request.prompt, "resolution": "custom", "aspect_ratio": aspect_ratio, "width": request.width, "height": request.height, "seconds": request.duration_seconds, "advanced": True, "fps": request.fps, "keyframe_role": "first", "ref_image_size": "match", "reference_mention_mode": "index", "prompt_optimizer_settings": False, "prompt_optimizer_scene_guide": "none", "h3_bundle": ["1", 0], "media": ["42", 0], "prompt_optimizer_resources": prompt_resources, "prompt_optimizer_marker": "{}", "prompt_optimizer_prompt_connected": False}},
        "4": {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["13", 0]}},
        "5": {"class_type": "BasicScheduler", "inputs": {"scheduler": "simple", "steps": request.steps, "denoise": 1, "model": ["2", 0]}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "8": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["6", 0], "guider": ["4", 0], "sampler": ["7", 0], "sigmas": ["5", 0], "latent_image": ["13", 1]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["13", 2]}},
        "10": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["13", 3]}},
        "11": {"class_type": "CreateVideo", "inputs": {"fps": ["13", 4], "bit_depth": "auto", "color_space": "sRGB", "chroma_subsampling": "auto", "codec": "none", "images": ["9", 0], "audio": ["10", 0]}},
        "12": {"class_type": "SaveVideo", "inputs": {"filename_prefix": output_prefix, "format": "mp4", "format.codec": "h264", "format.codec.encoding": "auto", "codec": "auto", "video": ["11", 0]}},
        "13": {"class_type": "MiniMaxH3EasyOutput", "inputs": {"h3_context": ["2", 1]}},
        "42": {"class_type": "MiniMaxH3EasyMediaLoader", "inputs": {"media_state": media_state}},
    }



