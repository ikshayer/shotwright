"""Pinned still-image workflows and ComfyUI manifest validation."""
from __future__ import annotations

import math
import ntpath
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.domain.seeds import derive_seed
from orchestrator.platform.errors import JobError, JobErrorKind


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ModelPin(_Frozen):
    role: Literal["diffusion", "text_encoder", "vae", "lora"]
    path: Path
    sha256: str
    source: str
    license_id: str

    @field_validator("path")
    @classmethod
    def _absolute_safe(cls, value: Path) -> Path:
        if not value.is_absolute() or value.suffix.lower() != ".safetensors":
            raise ValueError("model path must be absolute and point to a .safetensors file")
        return value

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("source")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://") or not value[8:].strip():
            raise ValueError("source must be a nonempty HTTPS provenance URL")
        return value

    @field_validator("license_id")
    @classmethod
    def _license(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("license_id must be nonblank")
        return value


class ImageRequest(_Frozen):
    shot_id: str
    shot_version: int = Field(ge=1)
    run_id: str
    prompt: str
    degrade_seed: int = Field(ge=0)
    models: tuple[ModelPin, ...]
    family: Literal["flux2_klein", "z_image"] = "flux2_klein"
    width: int = 1280
    height: int = 960
    steps: int | None = Field(None, ge=1, le=100)
    lora_strength: float | None = None
    precision: Literal["default", "fp8_e4m3fn", "fp8_e5m2"] = "default"
    memory_mode: Literal["normal", "lowvram", "novram"] = "normal"
    gpu_device: int = Field(0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _resolve_steps(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        resolved = dict(value)
        if resolved.get("steps") is None:
            resolved["steps"] = 25 if resolved.get("family", "flux2_klein") == "z_image" else 4
        return resolved

    @field_validator("shot_id", "run_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("IDs may contain only letters, digits, underscore, and hyphen")
        return value

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must be nonblank")
        return value

    @field_validator("lora_strength", mode="before")
    @classmethod
    def _finite_lora_strength(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("lora_strength must be a finite number")
        if not math.isfinite(value):
            raise ValueError("lora_strength must be a finite number")
        return value

    @field_validator("width", "height")
    @classmethod
    def _dimension(cls, value: int) -> int:
        if value < 64 or value > 2048 or value % 16:
            raise ValueError("dimensions must be multiples of 16 in [64, 2048]")
        return value

    @model_validator(mode="after")
    def _pins(self) -> ImageRequest:
        roles = [pin.role for pin in self.models]
        expected = {"diffusion", "text_encoder", "vae"}
        if self.family == "z_image" and "lora" in roles:
            expected.add("lora")
        if set(roles) != expected or len(roles) != len(expected):
            raise ValueError(f"exactly one model pin for each {self.family} role is required")
        names = {pin.role: pin.path.name for pin in self.models}
        if self.family == "flux2_klein":
            allowed = {"diffusion": {"flux-2-klein-4b.safetensors"},
                       "text_encoder": {"qwen_3_4b_fp4_flux2.safetensors", "qwen_3_4b.safetensors"},
                       "vae": {"flux2-vae.safetensors"}}
        else:
            allowed = {"diffusion": {"z_image_int8_convrot.safetensors"},
                       "text_encoder": {"qwen_3_4b.safetensors"},
                       "vae": {"ae.safetensors"},
                       "lora": {"realgen_v2.safetensors"}}
        for role, name in names.items():
            if name not in allowed[role]:
                raise ValueError(f"unsupported measured {role} model name: {name}")
        has_lora = "lora" in roles
        if self.family == "z_image" and self.precision != "default":
            raise ValueError("z_image requires default precision")
        if has_lora:
            if self.lora_strength is None or not 0 < self.lora_strength <= 2:
                raise ValueError("a lora pin requires lora_strength in (0, 2]")
        elif self.lora_strength is not None:
            raise ValueError("lora_strength requires a lora pin")
        return self


NODE_TYPES = ("UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode", "ConditioningZeroOut",
              "CFGGuider", "Flux2Scheduler", "KSamplerSelect", "RandomNoise",
              "EmptyFlux2LatentImage", "SamplerCustomAdvanced", "VAEDecodeTiled", "SaveImage",
              "ModelSamplingAuraFlow", "EmptySD3LatentImage", "KSampler", "LoraLoaderModelOnly")


def build_workflow(request: ImageRequest, object_info: dict, *, output_prefix: str) -> dict[str, dict]:
    request = ImageRequest.model_validate(request.model_dump(mode="python"))
    if not isinstance(object_info, dict):
        raise _invalid("/object_info must be an object")
    parts = output_prefix.split("/") if isinstance(output_prefix, str) else []
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if (not isinstance(output_prefix, str) or not output_prefix or output_prefix.startswith(("/", "\\")) or "\\" in output_prefix
            or ":" in output_prefix or "%" in output_prefix or any(part in {"", ".", ".."} for part in parts)
            or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) or ntpath.basename(part).upper() in reserved for part in parts)):
        raise _invalid("output_prefix must be a safe relative POSIX path")
    pins = {pin.role: pin for pin in request.models}
    if request.family == "flux2_klein":
        graph: dict[str, dict] = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": pins["diffusion"].path.name, "weight_dtype": request.precision}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": pins["text_encoder"].path.name, "type": "flux2", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": pins["vae"].path.name}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": request.prompt, "clip": ["2", 0]}},
            "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
            "6": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "cfg": 1.0}},
            "7": {"class_type": "Flux2Scheduler", "inputs": {"steps": request.steps, "width": request.width, "height": request.height}},
            "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": derive_seed(request.shot_id, request.degrade_seed, "comfyui", 2**64-1)}},
            "10": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": request.width, "height": request.height, "batch_size": 1}},
            "11": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["6", 0], "sampler": ["8", 0], "sigmas": ["7", 0], "latent_image": ["10", 0]}},
            "12": {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["11", 0], "vae": ["3", 0], "tile_size": 512, "overlap": 64}},
            "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": output_prefix}},
        }
    else:
        graph = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": pins["diffusion"].path.name, "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": pins["text_encoder"].path.name, "type": "lumina2", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": pins["vae"].path.name}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": request.prompt, "clip": ["2", 0]}},
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
            "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {
                "model": ["6", 0] if "lora" in pins else ["1", 0], "shift": 3.0}},
            "8": {"class_type": "EmptySD3LatentImage", "inputs": {
                "width": request.width, "height": request.height, "batch_size": 1}},
            "9": {"class_type": "KSampler", "inputs": {
                "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0],
                "latent_image": ["8", 0],
                "seed": derive_seed(request.shot_id, request.degrade_seed, "comfyui", 2**64-1),
                "steps": request.steps, "cfg": 4.0, "sampler_name": "res_multistep",
                "scheduler": "simple", "denoise": 1.0}},
            "12": {"class_type": "VAEDecodeTiled", "inputs": {
                "samples": ["9", 0], "vae": ["3", 0], "tile_size": 512, "overlap": 64}},
            "13": {"class_type": "SaveImage", "inputs": {
                "images": ["12", 0], "filename_prefix": output_prefix}},
        }
        if "lora" in pins:
            graph["6"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": ["1", 0], "lora_name": pins["lora"].path.name,
                "strength_model": _required_lora_strength(request)}}
    for node in graph.values():
        cls = node["class_type"]
        schema = object_info.get(cls)
        if not isinstance(schema, dict):
            raise _invalid(f"missing node signature {cls}")
        inp = schema.get("input")
        if not isinstance(inp, dict):
            raise _invalid(f"malformed signature for {cls}")
        required = inp.get("required", {})
        optional = inp.get("optional", {})
        if not isinstance(required, dict) or not isinstance(optional, dict):
            raise _invalid(f"malformed input signature for {cls}")
        supplied = node["inputs"]
        unknown = set(supplied) - (set(required) | set(optional))
        if unknown:
            raise _invalid(f"unknown inputs for {cls}: {sorted(unknown)}")
        for name, descriptor in required.items():
            if name in supplied:
                _validate_input(cls, name, supplied[name], descriptor, object_info)
                continue
            if not isinstance(descriptor, list) or len(descriptor) < 2 or not isinstance(descriptor[1], dict) or "default" not in descriptor[1]:
                raise _invalid(f"required input {cls}.{name} has no explicit default")
            supplied[name] = descriptor[1]["default"]
            _validate_input(cls, name, supplied[name], descriptor, object_info)
        for name, value in list(supplied.items()):
            if name in optional:
                _validate_input(cls, name, value, optional[name], object_info)
    return graph


_MODEL_INPUTS = frozenset({"unet_name", "clip_name", "vae_name", "lora_name"})


def _validate_input(cls: str, name: str, value: object, descriptor: object, info: dict) -> None:
    if not isinstance(descriptor, list) or not descriptor:
        raise _invalid(f"malformed input descriptor {cls}.{name}")
    kind = descriptor[0]
    opts = descriptor[1] if len(descriptor) > 1 and isinstance(descriptor[1], dict) else {}
    if isinstance(kind, list) or kind == "COMBO":
        # Legacy nodes list combo options inline; ComfyUI 0.44.0's schema-defined nodes
        # declare ["COMBO", {"options": [...]}] instead (e.g. KSamplerSelect.sampler_name).
        # Both forms must be checked: an unchecked combo is an unvalidated graph.
        _validate_choice(cls, name, value, kind if isinstance(kind, list) else opts.get("options"))
        return
    if isinstance(kind, str) and isinstance(value, (int, float)):
        if isinstance(opts.get("min"), (int, float)) and value < opts["min"] or isinstance(opts.get("max"), (int, float)) and value > opts["max"]:
            raise _invalid(f"out-of-range input {cls}.{name}")


def _validate_choice(cls: str, name: str, value: object, choices: object) -> None:
    if (not isinstance(choices, list)
            or not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in choices)):
        raise _invalid(f"malformed combo options for {cls}.{name}")
    if not choices:
        if name in _MODEL_INPUTS:
            raise JobError(JobErrorKind.MISSING_MODEL_ARTIFACT, f"ComfyUI has no model choices for {cls}.{name}")
        raise _invalid(f"combo {cls}.{name} declares no options")
    if value not in choices:
        if name in _MODEL_INPUTS:
            raise JobError(JobErrorKind.MISSING_MODEL_ARTIFACT,
                           f"ComfyUI does not expose pinned model {value}")
        raise _invalid(f"invalid choice for {cls}.{name}: {value!r}")


def _invalid(message: str) -> JobError:
    return JobError(JobErrorKind.INVALID_MANIFEST, message)


def graph_seed(graph: dict[str, dict]) -> int:
    """Return the family-specific graph seed from the shared sampler node."""
    node = graph.get("9")
    inputs = node.get("inputs") if isinstance(node, dict) else None
    if not isinstance(inputs, dict):
        raise _invalid("workflow graph lacks sampler seed node 9")
    seed = inputs.get("noise_seed", inputs.get("seed"))
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise _invalid("workflow graph has no integer sampler seed")
    return seed


def family_sampler(family: Literal["flux2_klein", "z_image"]) -> str:
    if family == "flux2_klein":
        return "euler"
    if family == "z_image":
        return "res_multistep"
    raise ValueError(f"unknown image family: {family}")


def _required_lora_strength(request: ImageRequest) -> float:
    # The model validator guarantees this whenever node 6 is present.
    if request.lora_strength is None:
        raise _invalid("LoRA graph node requires a lora_strength")
    return request.lora_strength
