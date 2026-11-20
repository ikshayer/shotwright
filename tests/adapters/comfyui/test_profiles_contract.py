"""Amendment 4 (A4.5, A4.6): the pinned profile manifest, compose_image_request, settings.

Every constant below is copied from A4.5 / A4.6, not from the implementation.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.adapters.comfyui import profiles
from orchestrator.adapters.comfyui.profiles import (
    DEFAULT_PROFILE,
    PROFILES,
    ModelProfile,
    PinSpec,
    ProfileName,
    compose_image_request,
)
from orchestrator.adapters.comfyui.workflow import build_workflow, graph_seed
from orchestrator.domain.seeds import derive_seed
from orchestrator.platform.settings import Settings, SettingsError, load_settings, parse_settings
from tests.adapters.comfyui.zimage_fixtures import combined_object_info

Z_REV = "eaf9ea44e47ed9778f02d8c92efdc398b6cccea3"
Z_BASE = f"https://huggingface.co/Comfy-Org/Z-Image/blob/{Z_REV}/split_files"

FLUX_PINS = {
    "diffusion": PinSpec(
        role="diffusion", category="diffusion_models", file_name="flux-2-klein-4b.safetensors",
        sha256="ec3d4e733a771f61c052fb4856c48b336c55eaf2c65487c2a1faeb9bbda7a343",
        source="https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/main/flux-2-klein-4b.safetensors",
        license_id="Apache-2.0"),
    "text_encoder": PinSpec(
        role="text_encoder", category="text_encoders", file_name="qwen_3_4b_fp4_flux2.safetensors",
        sha256="3eab03a77adb0ee5304a4e677d5c10ac22f9049c1d7c894adca4f8bb39206ca8",
        source="https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/1f52fa6a526ff84b01520be049b63f7835fe059f/split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors",
        license_id="Apache-2.0"),
    "vae": PinSpec(
        role="vae", category="vae", file_name="flux2-vae.safetensors",
        sha256="868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3",
        source="https://huggingface.co/Comfy-Org/Flux-2-Klein-4B/blob/a9e4ca87c16db4c4e1a16406a9ddb300ab0ae246/split_files/vae/flux2-vae.safetensors",
        license_id="Apache-2.0"),
}
Z_PINS = {
    "diffusion": PinSpec(
        role="diffusion", category="diffusion_models", file_name="z_image_int8_convrot.safetensors",
        sha256="eb2b470694fbd9778f15efd309000ea0091312c018c10a352c120b1da9bf4279",
        source=f"{Z_BASE}/diffusion_models/z_image_int8_convrot.safetensors", license_id="Apache-2.0"),
    "text_encoder": PinSpec(
        role="text_encoder", category="text_encoders", file_name="qwen_3_4b.safetensors",
        sha256="6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a",
        source=f"{Z_BASE}/text_encoders/qwen_3_4b.safetensors", license_id="Apache-2.0"),
    "vae": PinSpec(
        role="vae", category="vae", file_name="ae.safetensors",
        sha256="afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38",
        source=f"{Z_BASE}/vae/ae.safetensors", license_id="Apache-2.0"),
}
LORA_PIN = PinSpec(
    role="lora", category="loras", file_name="realgen_v2.safetensors",
    sha256="c50a3437f85592512f92a2f956d699c6fc524e2b05cc77a05dc47a3057133973",
    source="https://huggingface.co/nynxz/RealGen-V2/blob/ebf1c59d130a438153a675fb4a7125b2b06e7dcf/realgen_v2.safetensors",
    license_id="Apache-2.0")

EXPECTED = {
    "flux2_klein": dict(family="flux2_klein", steps=4, lora_strength=None, required_vram_gb=0.0,
                        pins=FLUX_PINS),
    "z_image": dict(family="z_image", steps=25, lora_strength=None, required_vram_gb=7.5,
                    pins=Z_PINS),
    "z_image_realgen": dict(family="z_image", steps=25, lora_strength=0.6, required_vram_gb=7.5,
                            pins={**Z_PINS, "lora": LORA_PIN}),
}


def pins_by_role(profile: ModelProfile) -> dict[str, PinSpec]:
    roles = [p.role for p in profile.pins]
    assert len(roles) == len(set(roles)), "one pin per role"
    return {p.role: p for p in profile.pins}


# ------------------------------------------------------------------------------- manifest


def test_profile_names_and_default() -> None:
    assert set(typing.get_args(ProfileName)) == {"z_image_realgen", "z_image", "flux2_klein"}
    assert DEFAULT_PROFILE == "z_image_realgen"
    assert set(PROFILES) == set(EXPECTED)
    assert DEFAULT_PROFILE in PROFILES


@pytest.mark.parametrize("name", list(EXPECTED))
def test_profile_contents_are_exactly_a45(name: str) -> None:
    profile = PROFILES[name]  # type: ignore[index]
    expected = EXPECTED[name]
    assert profile.name == name
    assert profile.family == expected["family"]
    assert profile.steps == expected["steps"]
    assert profile.lora_strength == expected["lora_strength"]
    assert profile.required_vram_gb == expected["required_vram_gb"]
    assert isinstance(profile.summary, str) and profile.summary.strip()
    actual = pins_by_role(profile)
    assert set(actual) == set(expected["pins"])
    for role, want in expected["pins"].items():  # type: ignore[union-attr]
        got = actual[role]
        assert (got.role, got.category, got.file_name, got.sha256, got.source, got.license_id) == (
            want.role, want.category, want.file_name, want.sha256, want.source, want.license_id), role


def test_z_image_realgen_is_the_z_image_pins_plus_the_lora_at_point_six() -> None:
    base, realgen = pins_by_role(PROFILES["z_image"]), pins_by_role(PROFILES["z_image_realgen"])
    for role, spec in base.items():
        assert realgen[role] == spec
    assert set(realgen) - set(base) == {"lora"}
    assert PROFILES["z_image_realgen"].lora_strength == 0.6
    assert PROFILES["z_image"].lora_strength is None
    assert PROFILES["flux2_klein"].lora_strength is None


def test_every_hash_is_64_lowercase_hex_and_every_source_is_https() -> None:
    for profile in PROFILES.values():
        for spec in profile.pins:
            assert len(spec.sha256) == 64 and spec.sha256 == spec.sha256.lower()
            assert set(spec.sha256) <= set("0123456789abcdef")
            assert spec.source.startswith("https://") and spec.license_id.strip()
            assert spec.file_name.endswith(".safetensors")


def test_profiles_mapping_and_entries_are_read_only() -> None:
    with pytest.raises(TypeError):
        PROFILES["evil"] = PROFILES["z_image"]  # type: ignore[index]
    with pytest.raises(TypeError):
        del PROFILES["z_image"]  # type: ignore[attr-defined,misc]
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        PROFILES["z_image"].steps = 1  # type: ignore[misc]
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        PROFILES["z_image"].pins[0].sha256 = "0" * 64  # type: ignore[misc]
    assert isinstance(PROFILES["z_image"].pins, tuple)


def test_module_exposes_the_public_names_of_the_contract() -> None:
    for name in ("ProfileName", "DEFAULT_PROFILE", "ModelProfile", "PinSpec", "PROFILES",
                 "compose_image_request"):
        assert hasattr(profiles, name)


# ------------------------------------------------------------------- compose_image_request


def compose(profile: str, weights: Path, **changes: object):
    values: dict[str, object] = dict(
        profile=profile, weights_dir=weights, shot_id="shot-9", shot_version=3,
        degrade_seed=123, run_id="run-xyz", prompt="A dim stairwell")
    values.update(changes)
    return compose_image_request(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", list(EXPECTED))
def test_compose_builds_pins_from_the_profile_under_weights_dir(name: str, tmp_path: Path) -> None:
    weights = tmp_path / "does-not-exist-yet" / "pipeline-models"
    req = compose(name, weights)
    expected = EXPECTED[name]
    assert req.family == expected["family"]
    assert req.steps == expected["steps"] and type(req.steps) is int
    assert req.lora_strength == expected["lora_strength"]
    assert (req.shot_id, req.shot_version, req.degrade_seed, req.run_id, req.prompt) == (
        "shot-9", 3, 123, "run-xyz", "A dim stairwell")
    assert (req.width, req.height) == (1280, 960)
    assert (req.precision, req.memory_mode, req.gpu_device) == ("default", "normal", 0)
    by_role = {p.role: p for p in req.models}
    assert len(by_role) == len(req.models) == len(expected["pins"])  # type: ignore[arg-type]
    for role, want in expected["pins"].items():  # type: ignore[union-attr]
        got = by_role[role]
        assert got.path == weights / want.category / want.file_name
        assert (got.sha256, got.source, got.license_id) == (want.sha256, want.source, want.license_id)
    assert not weights.exists(), "compose_image_request must do no I/O"


def test_compose_width_and_height_default_when_none_and_override_when_given(tmp_path: Path) -> None:
    default = compose("z_image", tmp_path, width=None, height=None)
    assert (default.width, default.height) == (1280, 960)
    custom = compose("z_image_realgen", tmp_path, width=1024, height=768)
    assert (custom.width, custom.height) == (1024, 768)
    only_width = compose("flux2_klein", tmp_path, width=640)
    assert (only_width.width, only_width.height) == (640, 960)


def test_compose_realgen_differs_from_plain_z_image(tmp_path: Path) -> None:
    realgen, plain = compose("z_image_realgen", tmp_path), compose("z_image", tmp_path)
    assert {p.role for p in realgen.models} - {p.role for p in plain.models} == {"lora"}
    assert realgen.lora_strength == 0.6 and plain.lora_strength is None


@pytest.mark.parametrize("bad", ["", "z_image_turbo", "flux1_schnell", "Z_IMAGE", "default"])
def test_compose_unknown_profile_is_value_error(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        compose(bad, tmp_path)


def test_composed_requests_build_the_matching_graph(tmp_path: Path) -> None:
    info = combined_object_info()
    realgen = build_workflow(compose("z_image_realgen", tmp_path), info, output_prefix="p/q")
    assert realgen["6"]["class_type"] == "LoraLoaderModelOnly"
    assert realgen["6"]["inputs"]["lora_name"] == "realgen_v2.safetensors"
    assert realgen["6"]["inputs"]["strength_model"] == 0.6
    assert realgen["9"]["inputs"]["steps"] == 25 and realgen["9"]["inputs"]["cfg"] == 4.0
    assert graph_seed(realgen) == derive_seed("shot-9", 123, "comfyui", 2**64 - 1)

    plain = build_workflow(compose("z_image", tmp_path), info, output_prefix="p/q")
    assert "6" not in plain and plain["7"]["inputs"]["model"] == ["1", 0]
    assert plain["1"]["inputs"]["unet_name"] == "z_image_int8_convrot.safetensors"

    flux = build_workflow(compose("flux2_klein", tmp_path), info, output_prefix="p/q")
    assert flux["7"]["class_type"] == "Flux2Scheduler" and flux["7"]["inputs"]["steps"] == 4
    assert flux["9"]["class_type"] == "RandomNoise"
    clip = next(n for n in flux.values() if n["class_type"] == "CLIPLoader")
    assert clip["inputs"]["clip_name"] == "qwen_3_4b_fp4_flux2.safetensors"
    assert clip["inputs"]["type"] == "flux2"


# ------------------------------------------------------------------------------- settings


def test_settings_defaults_for_image_models_and_comfyui_root() -> None:
    settings = Settings()
    assert settings.image_models.weights_dir == Path(r"D:\Work\pipeline-models")
    assert settings.image_models.weights_dir.is_absolute()
    assert settings.image_models.default_profile == "z_image_realgen"
    assert settings.image_models.default_profile == DEFAULT_PROFILE
    assert settings.binaries.comfyui_root == Path(r"D:\ComfyUI_0.44.0\ComfyUI_windows_portable")


def test_settings_image_models_overrides_and_validation(tmp_path: Path) -> None:
    weights = tmp_path / "models"
    settings = parse_settings({"image_models": {"weights_dir": str(weights),
                                                "default_profile": "flux2_klein"}})
    assert settings.image_models.weights_dir == weights
    assert settings.image_models.default_profile == "flux2_klein"
    with pytest.raises(ValidationError):
        Settings.model_validate({"image_models": {"default_profile": "z_image_turbo"}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"image_models": {"unexpected": 1}})


def test_settings_toml_section_image_models(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    weights = tmp_path / "w"
    config.write_text(f'[image_models]\nweights_dir = "{weights.as_posix()}"\n'
                      'default_profile = "z_image"\n', encoding="utf-8")
    settings = load_settings(config, verify_paths=False)
    assert settings.image_models.weights_dir == weights
    assert settings.image_models.default_profile == "z_image"


def test_verify_paths_reports_a_missing_weights_dir_like_the_other_path_checks(
        tmp_path: Path) -> None:
    missing = tmp_path / "absent-models"
    config = tmp_path / "settings.toml"
    config.write_text(f'project_root = "{tmp_path.as_posix()}"\n'
                      f'[image_models]\nweights_dir = "{missing.as_posix()}"\n', encoding="utf-8")
    with pytest.raises(SettingsError) as caught:
        load_settings(config, verify_paths=True)
    assert "image_models.weights_dir" in {issue.key for issue in caught.value.issues}

    missing.mkdir()
    try:
        load_settings(config, verify_paths=True)
    except SettingsError as other:
        # Other prerequisites (Blender, FFmpeg, ComfyUI) may be absent on this machine, but the
        # now-existing weights_dir must no longer be among the reported issues.
        assert "image_models.weights_dir" not in {issue.key for issue in other.issues}
