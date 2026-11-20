"""Shared A4 fixtures: Z-Image pins, requests and a combined /object_info.

Written from CONTRACT.md Amendment 4 only. The combined /object_info is the FLUX one from
test_workflow_contract extended with the Z-Image node signatures A4's test rules name
(ModelSamplingAuraFlow, EmptySD3LatentImage, KSampler, LoraLoaderModelOnly) and with
the Z-Image model files / the `lumina2` CLIP type added to the shared loader combos.
"""

from __future__ import annotations

import copy
from pathlib import Path

from orchestrator.adapters.comfyui.workflow import ImageRequest, ModelPin
from tests.adapters.comfyui.test_workflow_contract import object_info as flux_object_info

Z_DIFFUSION = "z_image_int8_convrot.safetensors"
Z_TEXT_ENCODER = "qwen_3_4b.safetensors"
Z_VAE = "ae.safetensors"
Z_LORA = "realgen_v2.safetensors"
FLUX_DIFFUSION = "flux-2-klein-4b.safetensors"
FLUX_TEXT_ENCODER_FP4 = "qwen_3_4b_fp4_flux2.safetensors"
FLUX_VAE = "flux2-vae.safetensors"


def pin(role: str, name: str, *, sha: str = "a" * 64) -> ModelPin:
    return ModelPin(role=role, path=Path("C:/weights") / name, sha256=sha,
                    source="https://models.example.invalid/" + name, license_id="Apache-2.0")


def z_pins(*, lora: bool = False) -> tuple[ModelPin, ...]:
    pins = [pin("diffusion", Z_DIFFUSION), pin("text_encoder", Z_TEXT_ENCODER), pin("vae", Z_VAE)]
    if lora:
        pins.append(pin("lora", Z_LORA))
    return tuple(pins)


def flux_pins() -> tuple[ModelPin, ...]:
    return (pin("diffusion", FLUX_DIFFUSION), pin("text_encoder", FLUX_TEXT_ENCODER_FP4),
            pin("vae", FLUX_VAE))


def z_request(*, lora: bool = False, **changes: object) -> ImageRequest:
    values: dict[str, object] = {
        "shot_id": "shot-1", "shot_version": 2, "run_id": "run-1", "prompt": "A quiet corridor",
        "degrade_seed": 41, "family": "z_image", "models": z_pins(lora=lora),
    }
    if lora:
        values["lora_strength"] = 0.6
    values.update(changes)
    return ImageRequest(**values)


def flux_request(**changes: object) -> ImageRequest:
    values: dict[str, object] = {
        "shot_id": "shot-1", "shot_version": 2, "run_id": "run-1", "prompt": "A quiet corridor",
        "degrade_seed": 41, "models": flux_pins(),
    }
    values.update(changes)
    return ImageRequest(**values)


def combined_object_info() -> dict[str, object]:
    info = copy.deepcopy(flux_object_info())
    required = lambda node: info[node]["input"]["required"]  # noqa: E731
    required("UNETLoader")["unet_name"] = [[FLUX_DIFFUSION, Z_DIFFUSION]]
    required("CLIPLoader")["clip_name"] = [[FLUX_TEXT_ENCODER_FP4, Z_TEXT_ENCODER]]
    required("CLIPLoader")["type"] = [["flux2", "lumina2"]]
    required("VAELoader")["vae_name"] = [[FLUX_VAE, Z_VAE]]
    info["ModelSamplingAuraFlow"] = {"input": {"required": {
        "model": ["MODEL"], "shift": ["FLOAT", {"default": 3.0, "min": 0.0, "max": 100.0, "step": 0.01}]}}}
    info["EmptySD3LatentImage"] = {"input": {"required": {
        "width": ["INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}],
        "height": ["INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}],
        "batch_size": ["INT", {"default": 1, "min": 1, "max": 4096}]}}}
    info["KSampler"] = {"input": {"required": {
        "model": ["MODEL"],
        "seed": ["INT", {"default": 0, "min": 0, "max": 18446744073709551615}],
        "steps": ["INT", {"default": 20, "min": 1, "max": 10000}],
        "cfg": ["FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.1}],
        "sampler_name": [["euler", "res_multistep", "dpmpp_2m"]],
        "scheduler": [["simple", "normal", "karras"]],
        "positive": ["CONDITIONING"], "negative": ["CONDITIONING"], "latent_image": ["LATENT"],
        "denoise": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}]}}}
    info["LoraLoaderModelOnly"] = {"input": {"required": {
        "model": ["MODEL"], "lora_name": [[Z_LORA]],
        "strength_model": ["FLOAT", {"default": 1.0, "min": -100.0, "max": 100.0, "step": 0.01}]}}}
    return info
