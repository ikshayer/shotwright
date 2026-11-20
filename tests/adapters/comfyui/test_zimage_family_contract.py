"""Amendment 4 (A4.2, A4.3): families, pin/name/precision/lora validation, the Z-Image graph.

Written from CONTRACT.md only. Every expected value is copied from the A4.3 table.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.adapters.comfyui.workflow import (
    ImageRequest,
    ModelPin,
    build_workflow,
    family_sampler,
    graph_seed,
)
from orchestrator.domain.seeds import derive_seed
from orchestrator.platform.errors import JobError, JobErrorKind
from tests.adapters.comfyui.test_workflow_contract import object_info as flux_only_object_info
from tests.adapters.comfyui.zimage_fixtures import (
    FLUX_DIFFUSION,
    FLUX_TEXT_ENCODER_FP4,
    FLUX_VAE,
    Z_DIFFUSION,
    Z_LORA,
    Z_TEXT_ENCODER,
    Z_VAE,
    combined_object_info,
    flux_pins,
    flux_request,
    pin,
    z_pins,
    z_request,
)

SEED = derive_seed("shot-1", 41, "comfyui", 2**64 - 1)


def jsonable(graph: dict[str, object]) -> dict[str, object]:
    """JSON round trip, so a tuple-vs-list wiring choice cannot matter (the graph is JSON)."""
    return json.loads(json.dumps(graph))


# --------------------------------------------------------------------------- A4.2 validation


def test_family_defaults_to_flux2_klein_and_flux_requests_stay_valid() -> None:
    req = flux_request()
    assert req.family == "flux2_klein"
    assert (req.width, req.height, req.precision, req.memory_mode, req.gpu_device) == (
        1280, 960, "default", "normal", 0)
    assert req.lora_strength is None


def test_unknown_family_is_rejected() -> None:
    with pytest.raises(ValidationError):
        flux_request(family="flux1_schnell")
    with pytest.raises(ValidationError):
        z_request(family="sdxl")


def test_z_image_request_accepts_the_measured_pins_with_and_without_lora() -> None:
    plain = z_request()
    assert plain.family == "z_image" and len(plain.models) == 3
    with_lora = z_request(lora=True)
    assert {p.role for p in with_lora.models} == {"diffusion", "text_encoder", "vae", "lora"}
    assert with_lora.lora_strength == 0.6


def test_model_pin_role_lora_is_valid_with_the_same_pin_rules() -> None:
    assert pin("lora", Z_LORA).role == "lora"
    with pytest.raises(ValidationError):
        ModelPin(role="lora", path=Path("C:/w/realgen_v2.bin"), sha256="a" * 64,
                 source="https://a.invalid/x", license_id="ok")
    with pytest.raises(ValidationError):
        ModelPin(role="lora", path=Path("C:/w/realgen_v2.safetensors"), sha256="a" * 64,
                 source="https://a.invalid/x", license_id=" ")
    with pytest.raises(ValidationError):
        ModelPin(role="controlnet", path=Path("C:/w/x.safetensors"), sha256="a" * 64,
                 source="https://a.invalid/x", license_id="ok")


def test_default_family_with_z_image_files_is_a_cross_family_mix() -> None:
    # No family given -> flux2_klein, whose measured names Z-Image files do not satisfy.
    with pytest.raises(ValidationError):
        ImageRequest(shot_id="s", shot_version=1, run_id="r", prompt="p", degrade_seed=1,
                     models=z_pins())


@pytest.mark.parametrize("swap", [
    ("diffusion", FLUX_DIFFUSION), ("text_encoder", FLUX_TEXT_ENCODER_FP4), ("vae", FLUX_VAE),
])
def test_z_image_rejects_a_flux_file_name_in_any_role(swap: tuple[str, str]) -> None:
    role, name = swap
    pins = tuple(pin(role, name) if p.role == role else p for p in z_pins())
    with pytest.raises(ValidationError):
        z_request(models=pins)


@pytest.mark.parametrize("swap", [
    ("diffusion", Z_DIFFUSION), ("vae", Z_VAE),
])
def test_flux_rejects_a_z_image_file_name(swap: tuple[str, str]) -> None:
    role, name = swap
    pins = tuple(pin(role, name) if p.role == role else p for p in flux_pins())
    with pytest.raises(ValidationError):
        flux_request(models=pins)


def test_flux_accepts_both_measured_text_encoders_and_z_image_only_the_plain_one() -> None:
    flux_request(models=(pin("diffusion", FLUX_DIFFUSION), pin("text_encoder", Z_TEXT_ENCODER),
                         pin("vae", FLUX_VAE)))
    flux_request()  # fp4 flux2 encoder
    with pytest.raises(ValidationError):
        z_request(models=(pin("diffusion", Z_DIFFUSION),
                          pin("text_encoder", FLUX_TEXT_ENCODER_FP4), pin("vae", Z_VAE)))


@pytest.mark.parametrize("role,name", [
    ("diffusion", "z_image_turbo.safetensors"), ("text_encoder", "qwen_3_8b.safetensors"),
    ("vae", "ae2.safetensors"), ("lora", "other_lora.safetensors"),
])
def test_z_image_rejects_unmeasured_file_names(role: str, name: str) -> None:
    pins = list(z_pins(lora=True))
    pins = [pin(role, name) if p.role == role else p for p in pins]
    with pytest.raises(ValidationError):
        z_request(models=tuple(pins), lora_strength=0.6)


def test_flux_with_a_lora_pin_is_invalid() -> None:
    with pytest.raises(ValidationError):
        flux_request(models=(*flux_pins(), pin("lora", Z_LORA)), lora_strength=0.6)
    with pytest.raises(ValidationError):
        flux_request(models=(*flux_pins(), pin("lora", Z_LORA)))


def test_z_image_requires_exactly_one_pin_per_required_role_and_at_most_one_lora() -> None:
    base = z_pins()
    for missing in ("diffusion", "text_encoder", "vae"):
        with pytest.raises(ValidationError):
            z_request(models=tuple(p for p in base if p.role != missing))
    with pytest.raises(ValidationError):
        z_request(models=(*base, base[0]))
    with pytest.raises(ValidationError):
        z_request(models=(*base, pin("lora", Z_LORA), pin("lora", Z_LORA)), lora_strength=0.6)
    with pytest.raises(ValidationError):
        z_request(models=())


@pytest.mark.parametrize("precision", ["fp8_e4m3fn", "fp8_e5m2"])
def test_z_image_requires_default_precision_but_flux_keeps_fp8(precision: str) -> None:
    with pytest.raises(ValidationError):
        z_request(precision=precision)
    with pytest.raises(ValidationError):
        z_request(lora=True, precision=precision)
    assert z_request(precision="default").precision == "default"
    assert flux_request(precision=precision).precision == precision


@pytest.mark.parametrize("strength", [0.0, -0.1, 2.0001, 3, math.nan, math.inf, -math.inf])
def test_lora_strength_must_be_finite_in_open_zero_closed_two(strength: float) -> None:
    with pytest.raises(ValidationError):
        z_request(lora=True, lora_strength=strength)


@pytest.mark.parametrize("strength", [0.0001, 0.6, 1, 2, 2.0])
def test_lora_strength_boundaries_that_are_valid(strength: float) -> None:
    assert z_request(lora=True, lora_strength=strength).lora_strength == strength


def test_lora_strength_is_required_with_a_lora_pin_and_forbidden_without() -> None:
    with pytest.raises(ValidationError):
        z_request(models=z_pins(lora=True))  # lora pin, no strength
    with pytest.raises(ValidationError):
        z_request(models=z_pins(lora=True), lora_strength=None)
    with pytest.raises(ValidationError):
        z_request(lora_strength=0.6)  # strength, no lora pin
    with pytest.raises(ValidationError):
        flux_request(lora_strength=0.6)
    assert z_request().lora_strength is None
    assert flux_request().lora_strength is None


@pytest.mark.parametrize("family,expected", [("flux2_klein", 4), ("z_image", 25)])
def test_steps_none_resolves_to_the_family_default_and_is_always_an_int(
        family: str, expected: int) -> None:
    req = flux_request() if family == "flux2_klein" else z_request()
    assert req.steps == expected and type(req.steps) is int
    explicit_none = (flux_request(steps=None) if family == "flux2_klein"
                     else z_request(steps=None))
    assert explicit_none.steps == expected and type(explicit_none.steps) is int
    assert req.model_dump()["steps"] == expected
    assert (family == "z_image") == (req.steps == 25)


def test_explicit_steps_are_kept_and_range_is_one_to_one_hundred() -> None:
    assert flux_request(steps=10).steps == 10
    assert z_request(steps=10).steps == 10
    assert z_request(lora=True, steps=100).steps == 100
    assert z_request(steps=1).steps == 1
    for bad in (0, 101, -3):
        with pytest.raises(ValidationError):
            z_request(steps=bad)
        with pytest.raises(ValidationError):
            flux_request(steps=bad)


def test_the_resolved_request_stays_frozen_and_strict() -> None:
    req = z_request(lora=True)
    with pytest.raises((ValidationError, TypeError)):
        req.steps = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        z_request(unexpected="caller graph")


# --------------------------------------------------------------------------- A4.3 graph


def expected_z_graph(*, lora: bool, prefix: str = "attempt-0001/image", steps: int = 25,
                     strength: float = 0.6, width: int = 1280, height: int = 960) -> dict[str, object]:
    graph: dict[str, object] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": Z_DIFFUSION, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": Z_TEXT_ENCODER, "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": Z_VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "A quiet corridor", "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "7": {"class_type": "ModelSamplingAuraFlow",
              "inputs": {"model": ["6", 0] if lora else ["1", 0], "shift": 3.0}},
        "8": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "9": {"class_type": "KSampler", "inputs": {
            "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["8", 0],
            "seed": SEED, "steps": steps, "cfg": 4.0, "sampler_name": "res_multistep",
            "scheduler": "simple", "denoise": 1.0}},
        "12": {"class_type": "VAEDecodeTiled", "inputs": {
            "samples": ["9", 0], "vae": ["3", 0], "tile_size": 512, "overlap": 64,
            "temporal_size": 64, "temporal_overlap": 8}},
        "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": prefix}},
    }
    if lora:
        graph["6"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1", 0], "lora_name": Z_LORA, "strength_model": strength}}
    return graph


def test_z_image_graph_without_lora_is_exactly_the_a43_table() -> None:
    req, info = z_request(), combined_object_info()
    untouched = copy.deepcopy(info)
    graph = build_workflow(req, info, output_prefix="attempt-0001/image")
    assert info == untouched, "build_workflow mutated object_info"
    assert set(graph) == {"1", "2", "3", "4", "5", "7", "8", "9", "12", "13"}
    assert jsonable(graph) == expected_z_graph(lora=False)
    assert graph == build_workflow(req, info, output_prefix="attempt-0001/image")


def test_z_image_graph_with_lora_inserts_node_6_and_rewires_model_sampling() -> None:
    req, info = z_request(lora=True), combined_object_info()
    graph = build_workflow(req, info, output_prefix="attempt-0001/image")
    assert set(graph) == {"1", "2", "3", "4", "5", "6", "7", "8", "9", "12", "13"}
    assert jsonable(graph) == expected_z_graph(lora=True)
    assert graph["6"]["inputs"]["strength_model"] == 0.6  # type: ignore[index]
    assert graph["7"]["inputs"]["model"] in (["6", 0], ("6", 0))  # type: ignore[index]


def test_z_image_graph_follows_request_steps_size_strength_and_prompt() -> None:
    req = z_request(lora=True, steps=12, width=1024, height=768, lora_strength=1.25,
                    prompt="Another prompt")
    graph = jsonable(build_workflow(req, combined_object_info(), output_prefix="a/b"))
    expected = expected_z_graph(lora=True, prefix="a/b", steps=12, strength=1.25, width=1024,
                                height=768)
    expected["4"]["inputs"]["text"] = "Another prompt"  # type: ignore[index]
    assert graph == expected


def test_z_image_graph_default_steps_come_from_the_family_not_flux() -> None:
    graph = build_workflow(z_request(), combined_object_info(), output_prefix="safe")
    assert graph["9"]["inputs"]["steps"] == 25
    assert graph["9"]["inputs"]["steps"] != flux_request().steps


def test_z_image_graph_seed_derivation_is_the_family_independent_one() -> None:
    graph = build_workflow(z_request(degrade_seed=7, shot_id="other-shot"),
                           combined_object_info(), output_prefix="safe")
    assert graph["9"]["inputs"]["seed"] == derive_seed("other-shot", 7, "comfyui", 2**64 - 1)
    assert "noise_seed" not in graph["9"]["inputs"]


def test_z_image_graph_has_no_flux_nodes_and_flux_graph_has_no_z_image_nodes() -> None:
    z_classes = {n["class_type"] for n in build_workflow(
        z_request(lora=True), combined_object_info(), output_prefix="safe").values()}
    flux_classes = {n["class_type"] for n in build_workflow(
        flux_request(), combined_object_info(), output_prefix="safe").values()}
    flux_only = {"CFGGuider", "Flux2Scheduler", "KSamplerSelect", "RandomNoise",
                 "EmptyFlux2LatentImage", "SamplerCustomAdvanced"}
    z_only = {"KSampler", "ModelSamplingAuraFlow", "EmptySD3LatentImage", "LoraLoaderModelOnly"}
    assert not (z_classes & (flux_only | {"ConditioningZeroOut"}))
    assert z_only <= z_classes
    assert not (flux_classes & z_only)
    assert flux_only <= flux_classes  # FLUX keeps its sampler stack


def test_flux_graph_is_unchanged_and_independent_of_the_extra_object_info_nodes() -> None:
    req = flux_request(models=(pin("diffusion", FLUX_DIFFUSION),
                               pin("text_encoder", Z_TEXT_ENCODER), pin("vae", FLUX_VAE)))
    legacy = build_workflow(req, flux_only_object_info(), output_prefix="attempt-0001/image")
    extended = build_workflow(req, combined_object_info(), output_prefix="attempt-0001/image")
    assert legacy == extended
    assert set(legacy) == {str(i) for i in range(1, 14)}
    assert legacy["10"]["class_type"] == "EmptyFlux2LatentImage"
    assert legacy["12"]["class_type"] == "VAEDecodeTiled"
    assert (legacy["12"]["inputs"]["tile_size"], legacy["12"]["inputs"]["overlap"]) == (512, 64)
    assert legacy["7"]["class_type"] == "Flux2Scheduler" and legacy["7"]["inputs"]["steps"] == 4
    assert legacy["6"]["inputs"]["cfg"] == 1.0
    assert legacy["8"]["inputs"]["sampler_name"] == "euler"
    assert legacy["9"]["inputs"]["noise_seed"] == SEED
    assert legacy["13"]["inputs"]["filename_prefix"] == "attempt-0001/image"
    clip = next(n for n in legacy.values() if n["class_type"] == "CLIPLoader")
    assert clip["inputs"]["type"] == "flux2"
    unet = next(n for n in legacy.values() if n["class_type"] == "UNETLoader")
    assert unet["inputs"]["unet_name"] == FLUX_DIFFUSION


def test_flux_graph_with_default_pins_uses_the_fp4_encoder_and_flux_vae() -> None:
    graph = build_workflow(flux_request(), combined_object_info(), output_prefix="safe")
    clip = next(n for n in graph.values() if n["class_type"] == "CLIPLoader")
    vae = next(n for n in graph.values() if n["class_type"] == "VAELoader")
    assert clip["inputs"]["clip_name"] == FLUX_TEXT_ENCODER_FP4
    assert vae["inputs"]["vae_name"] == FLUX_VAE
    assert graph["7"]["inputs"]["steps"] == 4


def test_flux_explicit_steps_reach_the_flux_scheduler() -> None:
    graph = build_workflow(flux_request(steps=9), combined_object_info(), output_prefix="safe")
    assert graph["7"]["inputs"]["steps"] == 9


# ------------------------------------------------------------- graph_seed / family_sampler


def test_graph_seed_reads_the_family_specific_input() -> None:
    info = combined_object_info()
    flux = build_workflow(flux_request(), info, output_prefix="safe")
    z = build_workflow(z_request(), info, output_prefix="safe")
    z_lora = build_workflow(z_request(lora=True), info, output_prefix="safe")
    assert graph_seed(flux) == SEED == flux["9"]["inputs"]["noise_seed"]
    assert graph_seed(z) == SEED == z["9"]["inputs"]["seed"]
    assert graph_seed(z_lora) == SEED
    other = build_workflow(z_request(degrade_seed=42), info, output_prefix="safe")
    assert graph_seed(other) == derive_seed("shot-1", 42, "comfyui", 2**64 - 1) != SEED
    assert type(graph_seed(z)) is int


def test_graph_seed_follows_the_graph_not_a_family_guess() -> None:
    z = build_workflow(z_request(), combined_object_info(), output_prefix="safe")
    z["9"]["inputs"]["seed"] = 123456789
    assert graph_seed(z) == 123456789
    flux = build_workflow(flux_request(), combined_object_info(), output_prefix="safe")
    flux["9"]["inputs"]["noise_seed"] = 987654321
    assert graph_seed(flux) == 987654321


def test_family_sampler_names() -> None:
    assert family_sampler("flux2_klein") == "euler"
    assert family_sampler("z_image") == "res_multistep"
    info = combined_object_info()
    assert build_workflow(flux_request(), info, output_prefix="s")["8"]["inputs"][
        "sampler_name"] == family_sampler("flux2_klein")
    assert build_workflow(z_request(), info, output_prefix="s")["9"]["inputs"][
        "sampler_name"] == family_sampler("z_image")


# ------------------------------------------------------ /object_info validation, Z-Image


def test_z_image_lora_name_is_a_model_combo_missing_is_missing_model_artifact() -> None:
    info = combined_object_info()
    info["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0].remove(Z_LORA)  # type: ignore[index]
    with pytest.raises(JobError) as caught:
        build_workflow(z_request(lora=True), info, output_prefix="safe")
    assert caught.value.kind is JobErrorKind.MISSING_MODEL_ARTIFACT


@pytest.mark.parametrize("node,field,value", [
    ("UNETLoader", "unet_name", Z_DIFFUSION), ("CLIPLoader", "clip_name", Z_TEXT_ENCODER),
    ("VAELoader", "vae_name", Z_VAE),
])
def test_z_image_missing_named_model_is_missing_model_artifact(
        node: str, field: str, value: str) -> None:
    info = combined_object_info()
    info[node]["input"]["required"][field][0].remove(value)  # type: ignore[index]
    with pytest.raises(JobError) as caught:
        build_workflow(z_request(), info, output_prefix="safe")
    assert caught.value.kind is JobErrorKind.MISSING_MODEL_ARTIFACT


@pytest.mark.parametrize("node", [
    "ModelSamplingAuraFlow", "EmptySD3LatentImage", "KSampler", "VAEDecodeTiled", "SaveImage",
    "CLIPTextEncode",
])
def test_z_image_missing_node_is_invalid_manifest(node: str) -> None:
    info = combined_object_info()
    del info[node]
    with pytest.raises(JobError) as caught:
        build_workflow(z_request(), info, output_prefix="safe")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def test_z_image_missing_lora_node_is_invalid_manifest_only_when_the_lora_is_used() -> None:
    info = combined_object_info()
    del info["LoraLoaderModelOnly"]
    with pytest.raises(JobError) as caught:
        build_workflow(z_request(lora=True), info, output_prefix="safe")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
    # Inference from "the same /object_info validation applies to every node [of the graph]":
    # a graph without node 6 does not need that node's signature.
    assert "6" not in build_workflow(z_request(), info, output_prefix="safe")


def test_z_image_rejects_unsupported_combo_values_and_unknown_inputs() -> None:
    info = combined_object_info()
    info["CLIPLoader"]["input"]["required"]["type"] = [["flux2"]]  # no lumina2
    with pytest.raises(JobError) as no_lumina:
        build_workflow(z_request(), info, output_prefix="safe")
    assert no_lumina.value.kind is JobErrorKind.INVALID_MANIFEST

    info = combined_object_info()
    info["KSampler"]["input"]["required"]["sampler_name"] = [["euler"]]  # no res_multistep
    with pytest.raises(JobError) as no_sampler:
        build_workflow(z_request(), info, output_prefix="safe")
    assert no_sampler.value.kind is JobErrorKind.INVALID_MANIFEST

    info = combined_object_info()
    info["KSampler"]["input"]["required"]["scheduler"] = [["normal"]]  # no simple
    with pytest.raises(JobError) as no_scheduler:
        build_workflow(z_request(), info, output_prefix="safe")
    assert no_scheduler.value.kind is JobErrorKind.INVALID_MANIFEST

    info = combined_object_info()
    info["KSampler"]["input"]["required"]["unexpected"] = ["STRING"]
    with pytest.raises(JobError) as extra:
        build_workflow(z_request(), info, output_prefix="safe")
    assert extra.value.kind is JobErrorKind.INVALID_MANIFEST

    info = combined_object_info()
    info["ModelSamplingAuraFlow"]["input"]["required"]["shift"][1]["max"] = 2.0
    with pytest.raises(JobError) as out_of_range:
        build_workflow(z_request(), info, output_prefix="safe")
    assert out_of_range.value.kind is JobErrorKind.INVALID_MANIFEST


def test_z_image_cfg_out_of_declared_range_is_invalid_manifest() -> None:
    info = combined_object_info()
    info["KSampler"]["input"]["required"]["cfg"][1]["max"] = 3.0  # graph needs cfg 4.0
    with pytest.raises(JobError) as caught:
        build_workflow(z_request(), info, output_prefix="safe")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


def test_z_image_vae_decode_defaults_are_read_from_object_info() -> None:
    info = combined_object_info()
    info["VAEDecodeTiled"]["input"]["required"]["temporal_size"][1]["default"] = 96
    graph = build_workflow(z_request(), info, output_prefix="safe")
    assert graph["12"]["inputs"]["temporal_size"] == 96
    del info["VAEDecodeTiled"]["input"]["required"]["temporal_size"][1]["default"]
    with pytest.raises(JobError) as caught:
        build_workflow(z_request(), info, output_prefix="safe")
    assert caught.value.kind is JobErrorKind.INVALID_MANIFEST


@pytest.mark.parametrize("prefix", ["../escape", "C:/absolute", "/rooted", "a\\..\\b", "",
                                    "a/%width%", "CON/image"])
def test_z_image_output_prefix_must_be_a_safe_relative_subpath(prefix: str) -> None:
    with pytest.raises((JobError, ValueError)) as caught:
        build_workflow(z_request(), combined_object_info(), output_prefix=prefix)
    if isinstance(caught.value, JobError):
        assert caught.value.kind is JobErrorKind.INVALID_MANIFEST
