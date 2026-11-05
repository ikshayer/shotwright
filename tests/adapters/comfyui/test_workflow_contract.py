from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.adapters.comfyui.workflow import ImageRequest, ModelPin, build_workflow
from orchestrator.domain.seeds import derive_seed
from orchestrator.platform.errors import JobError, JobErrorKind


def pin(role: str, name: str) -> ModelPin:
    return ModelPin(role=role, path=Path("C:/weights") / name, sha256="a" * 64,
                    source="https://models.example.invalid/" + name, license_id="research-only")


def request(**changes: object) -> ImageRequest:
    values: dict[str, object] = {
        "shot_id": "shot-1", "shot_version": 2, "run_id": "run-1", "prompt": "A quiet corridor",
        "degrade_seed": 41,
        "models": (pin("diffusion", "flux-2-klein-4b.safetensors"),
                   pin("text_encoder", "qwen_3_4b.safetensors"),
                   pin("vae", "flux2-vae.safetensors")),
    }
    values.update(changes)
    return ImageRequest(**values)


def object_info() -> dict[str, object]:
    """Measured node names with Comfy's real /object_info descriptor shapes/defaults."""
    return {
        "UNETLoader": {"input": {"required": {"unet_name": [["flux-2-klein-4b.safetensors"]], "weight_dtype": [["default", "fp8_e4m3fn", "fp8_e5m2"], {"default": "default"}]}}},
        "CLIPLoader": {"input": {"required": {"clip_name": [["qwen_3_4b_fp4_flux2.safetensors", "qwen_3_4b.safetensors"]], "type": [["flux2"]]}, "optional": {"device": [["default", "cpu"], {"default": "default"}]}}},
        "VAELoader": {"input": {"required": {"vae_name": [["flux2-vae.safetensors"]]}}},
        "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {"default": ""}], "clip": ["CLIP"]}}},
        "ConditioningZeroOut": {"input": {"required": {"conditioning": ["CONDITIONING"]}}},
        "CFGGuider": {"input": {"required": {"model": ["MODEL"], "positive": ["CONDITIONING"], "negative": ["CONDITIONING"], "cfg": ["FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}]}}},
        "Flux2Scheduler": {"input": {"required": {"steps": ["INT", {"default": 20, "min": 1}], "width": ["INT", {"default": 1024, "min": 16}], "height": ["INT", {"default": 1024, "min": 16}]}}},
        "KSamplerSelect": {"input": {"required": {"sampler_name": [["euler"]]}}},
        "RandomNoise": {"input": {"required": {"noise_seed": ["INT", {"default": 0, "min": 0, "max": 18446744073709551615}]}}},
        "EmptyFlux2LatentImage": {"input": {"required": {"width": ["INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}], "height": ["INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}], "batch_size": ["INT", {"default": 1, "min": 1}]}}},
        "SamplerCustomAdvanced": {"input": {"required": {"noise": ["NOISE"], "guider": ["GUIDER"], "sampler": ["SAMPLER"], "sigmas": ["SIGMAS"], "latent_image": ["LATENT"]}}},
        "VAEDecodeTiled": {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"], "tile_size": ["INT", {"default": 512, "min": 64, "max": 4096, "step": 32}], "overlap": ["INT", {"default": 64, "min": 0, "max": 4096, "step": 32}], "temporal_size": ["INT", {"default": 64, "min": 8}], "temporal_overlap": ["INT", {"default": 8, "min": 4}]}}},
        "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "ComfyUI"}]}, "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"}}},
    }


@pytest.mark.parametrize("change", [
    {"shot_id": "../x"}, {"run_id": "a/b"}, {"shot_version": 0},
    {"degrade_seed": -1}, {"prompt": "  "}, {"width": 65}, {"height": 2050},
    {"steps": 0}, {"gpu_device": -1}, {"precision": "fp16"},
])
def test_request_rejects_invalid_identity_geometry_and_options(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**change)


def test_defaults_and_immutable_strict_schema() -> None:
    req = request()
    assert (req.width, req.height, req.steps, req.precision, req.memory_mode, req.gpu_device) == (
        1280, 960, 4, "default", "normal", 0)
    with pytest.raises(ValidationError):
        request(unexpected="caller graph")
    with pytest.raises((ValidationError, TypeError)):
        req.width = 640  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ModelPin(role="vae", path=Path("weights/vae.safetensors"), sha256="a" * 64,
                 source="https://example.invalid/vae", license_id="ok")


@pytest.mark.parametrize("bad", [
    {"role": "vae", "path": Path("C:/x/vae.bin"), "sha256": "a" * 64,
     "source": "https://a.invalid/x", "license_id": "ok"},
    {"role": "vae", "path": Path("C:/x/vae.safetensors"), "sha256": "A" * 64,
     "source": "https://a.invalid/x", "license_id": "ok"},
    {"role": "vae", "path": Path("C:/x/vae.safetensors"), "sha256": "a" * 64,
     "source": "file:///x", "license_id": "ok"},
    {"role": "vae", "path": Path("C:/x/vae.safetensors"), "sha256": "a" * 64,
     "source": "https://a.invalid/x", "license_id": " "},
])
def test_model_pin_requires_safetensors_absolute_hash_and_provenance(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelPin(**bad)


def test_exactly_one_model_pin_per_role() -> None:
    pins = request().models
    with pytest.raises(ValidationError):
        request(models=pins[:2])
    with pytest.raises(ValidationError):
        request(models=(*pins, pins[0]))


def test_graph_is_deterministic_tiled_seeded_and_nonmutating() -> None:
    req, info = request(), object_info()
    untouched = copy.deepcopy(info)
    graph = build_workflow(req, info, output_prefix="attempt-0001/image")
    assert graph == build_workflow(req, info, output_prefix="attempt-0001/image")
    assert info == untouched
    assert graph["10"]["class_type"] == "EmptyFlux2LatentImage"
    assert graph["12"]["class_type"] == "VAEDecodeTiled"
    assert graph["12"]["inputs"]["tile_size"] == 512
    assert graph["12"]["inputs"]["overlap"] == 64
    assert graph["12"]["inputs"]["temporal_size"] == 64
    assert graph["12"]["inputs"]["temporal_overlap"] == 8
    assert graph["7"]["class_type"] == "Flux2Scheduler"
    assert "model" not in graph["7"]["inputs"] and "seed" not in graph["7"]["inputs"]
    assert graph["6"]["inputs"]["cfg"] == 1.0
    assert graph["8"]["inputs"]["sampler_name"] == "euler"
    assert graph["9"]["inputs"]["noise_seed"] == derive_seed("shot-1", 41, "comfyui", 2**64 - 1)
    assert graph["13"]["inputs"]["filename_prefix"] == "attempt-0001/image"


@pytest.mark.parametrize("prefix", ["../escape", "C:/absolute", "/rooted", "a\\..\\b", "",
                                    "a/%width%", "CON/image", "a:stream/image", "a./image"])
def test_graph_output_prefix_must_be_safe_relative_subpath(prefix: str) -> None:
    with pytest.raises((JobError, ValueError)) as caught:
        build_workflow(request(), object_info(), output_prefix=prefix)
    if isinstance(caught.value, JobError):
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def test_graph_rejects_missing_node_unknown_input_and_unsupported_named_model() -> None:
    info = object_info()
    del info["VAEDecodeTiled"]
    with pytest.raises(JobError) as missing:
        build_workflow(request(), info, output_prefix="safe")
    assert missing.value.kind is JobErrorKind.INVALID_MANIFEST

    info = object_info()
    info["UNETLoader"]["input"]["required"]["unexpected"] = ["STRING"]  # type: ignore[index]
    with pytest.raises(JobError) as extra:
        build_workflow(request(), info, output_prefix="safe")
    assert extra.value.kind is JobErrorKind.INVALID_MANIFEST

    info = object_info()
    info["UNETLoader"]["input"]["required"]["unet_name"][0].remove("flux-2-klein-4b.safetensors")  # type: ignore[index]
    with pytest.raises(JobError) as model:
        build_workflow(request(), info, output_prefix="safe")
    assert model.value.kind is JobErrorKind.MISSING_MODEL_ARTIFACT


def test_graph_rejects_missing_declared_default_and_out_of_range_default() -> None:
    info = object_info()
    del info["VAEDecodeTiled"]["input"]["required"]["temporal_size"][1]["default"]  # type: ignore[index]
    with pytest.raises(JobError) as missing:
        build_workflow(request(), info, output_prefix="safe")
    assert missing.value.kind is JobErrorKind.INVALID_MANIFEST

    info = object_info()
    info["VAEDecodeTiled"]["input"]["required"]["temporal_size"][1]["default"] = 7  # type: ignore[index]
    with pytest.raises(JobError) as invalid:
        build_workflow(request(), info, output_prefix="safe")
    assert invalid.value.kind is JobErrorKind.INVALID_MANIFEST
