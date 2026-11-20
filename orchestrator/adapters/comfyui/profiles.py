"""Pinned local model profiles for ComfyUI still-image generation."""
from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict

from orchestrator.platform.settings import ProfileName

from .workflow import ImageRequest, ModelPin

DEFAULT_PROFILE: ProfileName = "z_image_realgen"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class PinSpec(_Frozen):
    role: Literal["diffusion", "text_encoder", "vae", "lora"]
    category: Literal["diffusion_models", "text_encoders", "vae", "loras"]
    file_name: str
    sha256: str
    source: str
    license_id: str


class ModelProfile(_Frozen):
    name: ProfileName
    family: Literal["flux2_klein", "z_image"]
    pins: tuple[PinSpec, ...]
    steps: int
    lora_strength: float | None
    required_vram_gb: float
    summary: str


_Z_IMAGE_PINS = (
    PinSpec(
        role="diffusion", category="diffusion_models",
        file_name="z_image_int8_convrot.safetensors",
        sha256="eb2b470694fbd9778f15efd309000ea0091312c018c10a352c120b1da9bf4279",
        source="https://huggingface.co/Comfy-Org/Z-Image/blob/eaf9ea44e47ed9778f02d8c92efdc398b6cccea3/split_files/diffusion_models/z_image_int8_convrot.safetensors",
        license_id="Apache-2.0",
    ),
    PinSpec(
        role="text_encoder", category="text_encoders", file_name="qwen_3_4b.safetensors",
        sha256="6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a",
        source="https://huggingface.co/Comfy-Org/Z-Image/blob/eaf9ea44e47ed9778f02d8c92efdc398b6cccea3/split_files/text_encoders/qwen_3_4b.safetensors",
        license_id="Apache-2.0",
    ),
    PinSpec(
        role="vae", category="vae", file_name="ae.safetensors",
        sha256="afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38",
        source="https://huggingface.co/Comfy-Org/Z-Image/blob/eaf9ea44e47ed9778f02d8c92efdc398b6cccea3/split_files/vae/ae.safetensors",
        license_id="Apache-2.0",
    ),
)

PROFILES: Mapping[ProfileName, ModelProfile] = MappingProxyType({
    "flux2_klein": ModelProfile(
        name="flux2_klein",
        family="flux2_klein",
        steps=4,
        lora_strength=None,
        required_vram_gb=0.0,
        summary="FLUX.2 klein 4B, 4 steps.",
        pins=(
            PinSpec(
                role="diffusion", category="diffusion_models",
                file_name="flux-2-klein-4b.safetensors",
                sha256="ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343",
                source="https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/main/flux-2-klein-4b.safetensors",
                license_id="Apache-2.0",
            ),
            PinSpec(
                role="text_encoder", category="text_encoders",
                file_name="qwen_3_4b_fp4_flux2.safetensors",
                sha256="3eab03a77adb0ee5304a4e677d5c10ac22f9049c1d7c894adca4f8bb39206ca8",
                source="https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/1f52fa6a526ff84b01520be049b63f7835fe059f/split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors",
                license_id="Apache-2.0",
            ),
            PinSpec(
                role="vae", category="vae", file_name="flux2-vae.safetensors",
                sha256="868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3",
                source="https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/a9e4ca87c16db4c4e1a16406a9ddb300ab0ae246/split_files/vae/flux2-vae.safetensors",
                license_id="Apache-2.0",
            ),
        ),
    ),
    "z_image": ModelProfile(
        name="z_image",
        family="z_image",
        steps=25,
        lora_strength=None,
        required_vram_gb=7.5,
        summary="Z-Image Base, 25 steps.",
        pins=_Z_IMAGE_PINS,
    ),
    "z_image_realgen": ModelProfile(
        name="z_image_realgen",
        family="z_image",
        steps=25,
        lora_strength=0.6,
        required_vram_gb=7.5,
        summary="Z-Image Base + RealGen V2 at 0.6, 25 steps.",
        pins=_Z_IMAGE_PINS + (
            PinSpec(
                role="lora", category="loras", file_name="realgen_v2.safetensors",
                sha256="c50a3437f85592512f92a2f956d699c6fc524e2b05cc77a05dc47a3057133973",
                source="https://huggingface.co/nynxz/RealGen-V2/blob/ebf1c59d130a438153a675fb4a7125b2b06e7dcf/realgen_v2.safetensors",
                license_id="Apache-2.0",
            ),
        ),
    ),
})


def compose_image_request(
    *,
    profile: ProfileName,
    weights_dir: Path,
    shot_id: str,
    shot_version: int,
    degrade_seed: int,
    run_id: str,
    prompt: str,
    width: int | None = None,
    height: int | None = None,
) -> ImageRequest:
    """Compose a request from pinned profile metadata without reading any files."""
    try:
        selected = PROFILES[profile]
    except (KeyError, TypeError) as exc:
        valid = ", ".join(PROFILES)
        raise ValueError(f"unknown profile {profile!r}; valid profiles: {valid}") from exc

    pins = tuple(
        ModelPin(
            role=pin.role,
            path=weights_dir / pin.category / pin.file_name,
            sha256=pin.sha256,
            source=pin.source,
            license_id=pin.license_id,
        )
        for pin in selected.pins
    )
    values: dict[str, object] = {
        "shot_id": shot_id,
        "shot_version": shot_version,
        "degrade_seed": degrade_seed,
        "run_id": run_id,
        "prompt": prompt,
        "models": pins,
        "family": selected.family,
        "steps": selected.steps,
        "lora_strength": selected.lora_strength,
    }
    if width is not None:
        values["width"] = width
    if height is not None:
        values["height"] = height
    return ImageRequest.model_validate(values)
