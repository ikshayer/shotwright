"""A4.10 review regressions: weights_dir validation, combo validation, FLUX golden graphs.

Written from CONTRACT.md A4.10 only. The combo tests run against the retained REAL ComfyUI
0.44.0 /object_info subset; the FLUX golden graphs were produced by `main`'s builder.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.adapters.comfyui.workflow import ImageRequest, ModelPin, build_workflow
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings, load_settings
from tests.adapters.comfyui.zimage_fixtures import flux_request, z_request

DATA = Path(__file__).parent / "data"
PREFIX = "generated/shot-1/v0002/run-1"
INVALID = JobErrorKind.INVALID_MANIFEST
MISSING = JobErrorKind.MISSING_MODEL_ARTIFACT


@pytest.fixture
def info() -> dict[str, dict]:
    with (DATA / "object_info_0_44_0_subset.json").open(encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------- relative weights_dir


def test_settings_reject_a_relative_weights_dir() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"image_models": {"weights_dir": "models/weights"}})
    with pytest.raises(ValidationError):
        Settings(image_models={"weights_dir": Path("relative")})


def test_settings_accept_an_absolute_weights_dir(tmp_path: Path) -> None:
    settings = Settings.model_validate({"image_models": {"weights_dir": str(tmp_path / "w")}})
    assert settings.image_models.weights_dir == tmp_path / "w"


def test_load_settings_rejects_a_relative_weights_dir(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('[image_models]\nweights_dir = "models"\n', encoding="utf-8")
    with pytest.raises(Exception) as caught:
        load_settings(config, verify_paths=False)
    assert "weights_dir" in str(caught.value)


def test_load_settings_accepts_an_absolute_weights_dir(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(f'[image_models]\nweights_dir = "{(tmp_path / "w").as_posix()}"\n',
                      encoding="utf-8")
    assert load_settings(config, verify_paths=False).image_models.weights_dir == tmp_path / "w"


# ------------------------------------------------------------------ combo validation


def build(request: ImageRequest, info: dict) -> dict[str, dict]:
    return build_workflow(request, info, output_prefix=PREFIX)


@pytest.mark.parametrize("make", [
    pytest.param(lambda: flux_request(), id="flux2_klein"),
    pytest.param(lambda: z_request(), id="z_image"),
    pytest.param(lambda: z_request(lora=True), id="z_image_lora"),
], )
def test_real_0_44_0_schema_builds_every_family_graph(make, info) -> None:
    before = copy.deepcopy(info)
    graph = build(make(), info)
    assert graph and all("class_type" in node for node in graph.values())
    assert info == before


def graph_value(request: ImageRequest, info: dict, node_type: str, name: str) -> object:
    node = next(n for n in build(request, info).values() if n["class_type"] == node_type)
    return node["inputs"][name]


def required(info: dict, node: str) -> dict:
    return info[node]["input"]["required"]


def test_v3_combo_missing_the_graph_value_is_invalid_manifest(info) -> None:
    wanted = graph_value(flux_request(), info, "KSamplerSelect", "sampler_name")
    options = required(info, "KSamplerSelect")["sampler_name"][1]["options"]
    assert wanted in options
    options.remove(wanted)
    with pytest.raises(JobError) as caught:
        build(flux_request(), info)
    assert caught.value.kind is INVALID


def test_v3_combo_value_present_among_options_is_accepted(info) -> None:
    descriptor = required(info, "KSamplerSelect")["sampler_name"]
    assert descriptor[0] == "COMBO" and isinstance(descriptor[1]["options"], list)
    build(flux_request(), info)


def _break_options(descriptor: list, how: str) -> None:
    config = descriptor[1]
    if how == "missing":
        del config["options"]
    elif how == "not_a_list":
        config["options"] = "euler"
    elif how == "dict_element":
        config["options"] = [*config["options"], {"name": "euler"}]
    elif how == "bool_element":
        config["options"] = [*config["options"], True]


@pytest.mark.parametrize("how", ["missing", "not_a_list", "dict_element", "bool_element"])
def test_v3_combo_with_malformed_options_is_invalid_manifest(how: str, info) -> None:
    _break_options(required(info, "KSamplerSelect")["sampler_name"], how)
    with pytest.raises(JobError) as caught:
        build(flux_request(), info)
    assert caught.value.kind is INVALID


def test_v3_combo_with_empty_options_is_invalid_manifest_for_a_non_model_input(info) -> None:
    required(info, "KSamplerSelect")["sampler_name"][1]["options"] = []
    with pytest.raises(JobError) as caught:
        build(flux_request(), info)
    assert caught.value.kind is INVALID


def test_legacy_empty_non_model_combo_is_invalid_manifest(info) -> None:
    required(info, "CLIPLoader")["type"] = [[]]
    with pytest.raises(JobError) as caught:
        build(flux_request(), info)
    assert caught.value.kind is INVALID


def test_legacy_empty_unet_name_is_missing_model_artifact(info) -> None:
    required(info, "UNETLoader")["unet_name"] = [[]]
    with pytest.raises(JobError) as caught:
        build(flux_request(), info)
    assert caught.value.kind is MISSING


def test_legacy_empty_lora_name_is_missing_model_artifact(info) -> None:
    required(info, "LoraLoaderModelOnly")["lora_name"] = [[]]
    with pytest.raises(JobError) as caught:
        build(z_request(lora=True), info)
    assert caught.value.kind is MISSING


@pytest.mark.parametrize(("node", "name", "request_factory"), [
    ("UNETLoader", "unet_name", flux_request),
    ("CLIPLoader", "clip_name", flux_request),
    ("VAELoader", "vae_name", flux_request),
    ("LoraLoaderModelOnly", "lora_name", lambda: z_request(lora=True)),
])
def test_legacy_combo_missing_a_model_name_is_missing_model_artifact(
        node: str, name: str, request_factory, info) -> None:
    wanted = graph_value(request_factory(), info, node, name)
    options = required(info, node)[name][0]
    options.remove(wanted)
    with pytest.raises(JobError) as caught:
        build(request_factory(), info)
    assert caught.value.kind is MISSING


@pytest.mark.parametrize(("node", "name", "request_factory"), [
    ("CLIPLoader", "type", flux_request),
    ("CLIPLoader", "type", z_request),
    ("UNETLoader", "weight_dtype", flux_request),
])
def test_legacy_combo_missing_a_non_model_value_is_invalid_manifest(
        node: str, name: str, request_factory, info) -> None:
    wanted = graph_value(request_factory(), info, node, name)
    required(info, node)[name][0].remove(wanted)
    with pytest.raises(JobError) as caught:
        build(request_factory(), info)
    assert caught.value.kind is INVALID


@pytest.mark.parametrize("bad", [{"name": "x"}, None, 1.5, ["nested"]])
@pytest.mark.parametrize(("node", "name"), [("CLIPLoader", "type"), ("UNETLoader", "unet_name")])
def test_legacy_combo_with_a_non_str_int_element_is_invalid_manifest(
        node: str, name: str, bad: object, info) -> None:
    required(info, node)[name][0].append(bad)
    with pytest.raises(JobError) as caught:
        build(flux_request(), info)
    assert caught.value.kind is INVALID


# -------------------------------------------------------------------- FLUX golden graphs


def golden_cases() -> list[dict]:
    with (DATA / "flux2_klein_golden_graphs.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def _pin(role: str, name: str) -> ModelPin:
    return ModelPin(role=role, path=Path("C:/weights") / name, sha256="a" * 64,
                    source="https://models.example.invalid/" + name, license_id="research-only")


def test_golden_file_covers_the_three_representative_requests() -> None:
    assert [c["name"] for c in golden_cases()] == [
        "default", "fp8_small_more_steps", "qwen_3_4b_encoder"]


@pytest.mark.parametrize("case", golden_cases(), ids=lambda c: c["name"])
def test_current_builder_reproduces_main_flux_golden_graph_exactly(case: dict, info) -> None:
    request = ImageRequest(
        models=(_pin("diffusion", "flux-2-klein-4b.safetensors"),
                _pin("text_encoder", case["text_encoder"]),
                _pin("vae", "flux2-vae.safetensors")),
        **case["request"])
    graph = build_workflow(request, info, output_prefix=case["output_prefix"])
    assert json.dumps(graph, sort_keys=True) == json.dumps(case["graph"], sort_keys=True)
