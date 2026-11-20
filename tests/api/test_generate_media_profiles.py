"""A4.7: `generate_media` composes a pinned-profile ImageRequest when `request.models` is omitted.

Driven through the real HTTP app with a fake job runner that captures the submitted
AdapterRequest. Written from CONTRACT.md Amendment 4 only. The shape of the captured
payload for a composed request is not fixed by the contract, so `image_request()` accepts
an ImageRequest, or the dict form the explicit-models path already submits.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.adapters.comfyui.profiles import DEFAULT_PROFILE, PROFILES
from orchestrator.adapters.comfyui.workflow import ImageRequest
from orchestrator.api import manifest as manifest_module
from orchestrator.api.app import create_app
from orchestrator.domain.entities import Episode, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.service.runner import AdapterKind, AdapterRequest
from orchestrator.store.json_store import ProjectStore
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER

PATH = "/projects/p1/media/generate"
DEGRADE_SEED = 777
CATEGORY = {"diffusion": "diffusion_models", "text_encoder": "text_encoders",
            "vae": "vae", "lora": "loras"}
VALID_PROFILES = ("z_image_realgen", "z_image", "flux2_klein")


class CaptureRunner:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, AdapterRequest]] = []

    def submit(self, project_id: str, request: AdapterRequest) -> SimpleNamespace:
        self.submissions.append((project_id, request))
        return SimpleNamespace(job_id=f"job-{len(self.submissions)}", run_id="r", state="queued")


class Env(SimpleNamespace):
    client: TestClient
    runner: CaptureRunner
    store: ProjectStore
    weights: Path
    settings: Settings

    def post(self, body: dict[str, Any]):
        return self.client.post(PATH, headers={TOKEN_HEADER: TOKEN}, json=body)

    def compose(self, request: dict[str, Any] | None = None, **top: Any):
        body = {"project_id": "p1", "episode_id": "ep1", "shot_id": "s1",
                "request": {"prompt": "A dim stairwell"} if request is None else request, **top}
        return self.post(body)

    def last(self) -> AdapterRequest:
        assert self.runner.submissions, "nothing was submitted"
        return self.runner.submissions[-1][1]


def build_env(tmp_path: Path, **image_models: Any) -> Iterator[Env]:
    parent = tmp_path / "projects"
    (parent / "p1").mkdir(parents=True)
    weights = tmp_path / "pipeline-models"
    settings = Settings.model_validate({
        "project_root": parent, "image_models": {"weights_dir": weights, **image_models}})
    runner = CaptureRunner()
    app = create_app(settings=settings, token=TOKEN, job_runner=runner)
    store = ProjectStore(ProjectRoot(parent / "p1"))
    store.save(Project(project_id="p1", title="A4"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30),
               expected_version=0)
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=10, degrade_seed=DEGRADE_SEED)
    store.create(shot)
    store.revise(shot.model_copy(update={**shot.next_version(), "narrative_beat": "second take"}),
                 expected_version=1)  # Shot is now version 2, so version 1 cannot pass by accident
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield Env(client=client, runner=runner, store=store, weights=weights, settings=settings,
                  app=app)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield from build_env(tmp_path)


def image_request(submitted: AdapterRequest) -> ImageRequest:
    payload: Any = submitted.payload
    if isinstance(payload, Mapping) and "request" in payload and "prompt" not in payload:
        payload = payload["request"]
    if isinstance(payload, ImageRequest):
        return payload
    return ImageRequest.model_validate(payload)


def payload_mapping(submitted: AdapterRequest) -> Mapping[str, Any]:
    payload: Any = submitted.payload
    if isinstance(payload, ImageRequest):
        return payload.model_dump(mode="json")
    assert isinstance(payload, Mapping)
    return payload["request"] if "request" in payload and "prompt" not in payload else payload


def assert_composed(env: Env, profile: str) -> ImageRequest:
    submitted = env.last()
    request = image_request(submitted)
    spec = PROFILES[profile]  # type: ignore[index]
    assert request.family == spec.family
    assert request.steps == spec.steps
    assert request.lora_strength == spec.lora_strength
    by_role = {p.role: p for p in request.models}
    assert set(by_role) == {s.role for s in spec.pins}
    for s in spec.pins:
        got = by_role[s.role]
        assert got.path == env.weights / s.category / s.file_name
        assert (got.sha256, got.source, got.license_id) == (s.sha256, s.source, s.license_id)
    return request


# ------------------------------------------------------------------- default / explicit profile


def test_omitted_models_compose_the_settings_default_profile(env: Env) -> None:
    assert env.settings.image_models.default_profile == DEFAULT_PROFILE == "z_image_realgen"
    response = env.compose()
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "queued"
    request = assert_composed(env, "z_image_realgen")
    assert request.family == "z_image" and request.lora_strength == 0.6
    assert "lora" in {p.role for p in request.models}
    assert request.prompt == "A dim stairwell"
    assert (request.width, request.height) == (1280, 960)
    assert env.runner.submissions[-1][0] == "p1"


@pytest.mark.parametrize("default", VALID_PROFILES)
def test_default_profile_setting_is_honoured_not_hard_coded(tmp_path: Path, default: str) -> None:
    for env in build_env(tmp_path, default_profile=default):
        assert env.compose().status_code == 202
        assert_composed(env, default)


@pytest.mark.parametrize("configured_default", VALID_PROFILES)
@pytest.mark.parametrize("explicit", VALID_PROFILES)
def test_explicit_profile_overrides_the_configured_default(
        tmp_path: Path, configured_default: str, explicit: str) -> None:
    for env in build_env(tmp_path, default_profile=configured_default):
        response = env.compose(profile=explicit)
        assert response.status_code == 202, response.text
        assert_composed(env, explicit)


def test_flux_profile_yields_a_flux_request_without_lora(env: Env) -> None:
    assert env.compose(profile="flux2_klein").status_code == 202
    request = assert_composed(env, "flux2_klein")
    assert request.family == "flux2_klein" and request.steps == 4 and request.lora_strength is None
    assert "lora" not in {p.role for p in request.models}


def test_width_and_height_are_taken_from_the_request(env: Env) -> None:
    assert env.compose({"prompt": "p", "width": 1024, "height": 768}).status_code == 202
    request = image_request(env.last())
    assert (request.width, request.height) == (1024, 768)
    assert env.compose({"prompt": "p", "width": 640}).status_code == 202
    request = image_request(env.last())
    assert (request.width, request.height) == (640, 960)


def test_invalid_geometry_is_a_422_with_no_submission(env: Env) -> None:
    response = env.compose({"prompt": "p", "width": 65})
    assert response.status_code == 422, response.text
    assert env.runner.submissions == []


# ------------------------------------------------------------------- shot, run id, version


def test_shot_fields_come_from_the_loaded_shot_and_record_version_is_its_version(env: Env) -> None:
    shot = env.store.load(Shot, "s1", episode_id="ep1")
    assert shot.version == 2 and shot.degrade_seed == DEGRADE_SEED
    assert env.compose().status_code == 202
    submitted = env.last()
    request = image_request(submitted)
    assert request.shot_id == "s1"
    assert request.shot_version == shot.version == 2
    assert request.degrade_seed == shot.degrade_seed == DEGRADE_SEED
    assert submitted.kind is AdapterKind.GENERATE
    assert submitted.record_id == "s1"
    assert submitted.record_version == shot.version
    assert submitted.episode_id == "ep1"


def test_run_id_is_a_fresh_uuid4_hex_unless_the_request_gives_one(env: Env) -> None:
    for _ in range(2):
        assert env.compose().status_code == 202
    first, second = (image_request(s[1]).run_id for s in env.runner.submissions)
    for run_id in (first, second):
        assert re.fullmatch(r"[0-9a-f]{32}", run_id), run_id
        assert uuid.UUID(hex=run_id).version == 4
    assert first != second

    assert env.compose({"prompt": "p", "run_id": "my-run_01"}).status_code == 202
    assert image_request(env.last()).run_id == "my-run_01"


def test_a_missing_shot_is_404_and_nothing_is_submitted(env: Env) -> None:
    response = env.post({"project_id": "p1", "episode_id": "ep1", "shot_id": "nope",
                         "request": {"prompt": "p"}})
    assert response.status_code == 404, response.text
    assert env.runner.submissions == []


# ------------------------------------------------------------------- refusals (no submission)


def test_unknown_profile_is_refused_naming_the_valid_profiles(env: Env) -> None:
    response = env.compose(profile="z_image_turbo")
    assert response.status_code == 422, response.text
    for name in VALID_PROFILES:
        assert name in response.text
    assert env.runner.submissions == []


@pytest.mark.parametrize("profile", ["Z_IMAGE", "flux1_schnell", "default"])
def test_other_unknown_profile_spellings_are_refused(env: Env, profile: str) -> None:
    assert env.compose(profile=profile).status_code == 422
    assert env.runner.submissions == []


def explicit_models(env: Env) -> list[dict[str, str]]:
    base = env.weights
    return [{"role": "diffusion", "path": str(base / "flux-2-klein-4b.safetensors"),
             "sha256": "a" * 64, "source": "https://models.example.invalid/f", "license_id": "x"},
            {"role": "text_encoder", "path": str(base / "qwen_3_4b.safetensors"),
             "sha256": "b" * 64, "source": "https://models.example.invalid/q", "license_id": "x"},
            {"role": "vae", "path": str(base / "flux2-vae.safetensors"),
             "sha256": "c" * 64, "source": "https://models.example.invalid/v", "license_id": "x"}]


@pytest.mark.parametrize("profile", VALID_PROFILES)
def test_profile_together_with_models_is_refused(env: Env, profile: str) -> None:
    request = {"shot_id": "s1", "shot_version": 2, "run_id": "r1", "prompt": "p",
               "degrade_seed": DEGRADE_SEED, "models": explicit_models(env)}
    response = env.compose(request, profile=profile)
    assert response.status_code == 422, response.text
    assert env.runner.submissions == []


@pytest.mark.parametrize("stray", [
    {"steps": 10}, {"family": "z_image"}, {"lora_strength": 0.6}, {"degrade_seed": 1},
    {"shot_version": 2}, {"precision": "default"}, {"memory_mode": "normal"},
    {"gpu_device": 0}, {"unexpected": "x"}, {"profile": "z_image"},
])
def test_stray_request_keys_are_refused_not_dropped(env: Env, stray: dict[str, Any]) -> None:
    response = env.compose({"prompt": "p", **stray})
    assert response.status_code == 422, response.text
    assert env.runner.submissions == []


@pytest.mark.parametrize("request_body", [{}, {"prompt": ""}, {"prompt": "   "}, {"prompt": None}])
def test_a_composed_request_needs_a_nonblank_prompt(env: Env, request_body: dict[str, Any]) -> None:
    response = env.compose(request_body)
    assert response.status_code == 422, response.text
    assert env.runner.submissions == []


# ------------------------------------------------------------------- explicit models unchanged


def test_explicit_models_are_passed_through_unchanged_with_no_profile_defaults(env: Env) -> None:
    request = {"shot_id": "s1", "shot_version": 2, "run_id": "explicit-run", "prompt": "explicit",
               "degrade_seed": DEGRADE_SEED, "steps": 7, "models": explicit_models(env)}
    response = env.compose(request)
    assert response.status_code == 202, response.text
    submitted = env.last()
    assert image_request(submitted).family == "flux2_klein"
    sent = payload_mapping(submitted)
    for key in ("shot_id", "shot_version", "run_id", "prompt", "degrade_seed", "steps"):
        assert sent[key] == request[key], key
    assert [(m["role"], str(m["path"]), m["sha256"], m["source"], m["license_id"])
            for m in sent["models"]] == [
        (m["role"], str(Path(m["path"])), m["sha256"], m["source"], m["license_id"])
        for m in request["models"]]
    # A caller who names models gets no profile reservation and no profile run_id/seed.
    assert submitted.required_vram_gb == 0.0
    assert submitted.record_version == 2


def test_explicit_models_keep_the_caller_run_id_even_when_it_differs_from_the_shot(env: Env) -> None:
    request = {"shot_id": "s1", "shot_version": 2, "run_id": "r-explicit", "prompt": "p",
               "degrade_seed": 5, "models": explicit_models(env)}
    assert env.compose(request).status_code == 202
    sent = image_request(env.last())
    assert (sent.run_id, sent.degrade_seed) == ("r-explicit", 5)


# ------------------------------------------------------------------- required_vram_gb


@pytest.mark.parametrize("profile,expected", [("z_image_realgen", 7.5), ("z_image", 7.5),
                                              ("flux2_klein", 0.0)])
def test_composed_request_defaults_required_vram_to_the_profile(
        env: Env, profile: str, expected: float) -> None:
    assert env.compose(profile=profile).status_code == 202
    assert env.last().required_vram_gb == expected == PROFILES[profile].required_vram_gb  # type: ignore[index]


def test_default_profile_reserves_7_5_gb_when_nothing_is_stated(env: Env) -> None:
    assert env.compose().status_code == 202
    assert env.last().required_vram_gb == 7.5


@pytest.mark.parametrize("extra,expected", [
    ({"required_vram_gb": 3.5}, 3.5),
    ({"resources": {"required_vram_gb": 2.5}}, 2.5),
    ({"required_vram_gb": 0}, 0.0),
    ({"resources": {"required_vram_gb": 0}}, 0.0),
    ({"required_vram_gb": 11.0}, 11.0),
])
def test_explicit_vram_declarations_win_over_the_profile_default(
        env: Env, extra: dict[str, Any], expected: float) -> None:
    assert env.compose(**extra).status_code == 202
    assert env.last().required_vram_gb == expected


def test_explicit_vram_wins_for_every_profile(env: Env) -> None:
    for profile in VALID_PROFILES:
        assert env.compose(profile=profile, required_vram_gb=1.5).status_code == 202
        assert env.last().required_vram_gb == 1.5


def test_other_resources_do_not_suppress_the_vram_default(env: Env) -> None:
    assert env.compose(resources={"required_ram_gb": 1.0}).status_code == 202
    submitted = env.last()
    assert submitted.required_vram_gb == 7.5
    assert submitted.required_ram_gb == 1.0


@pytest.mark.parametrize("extra", [{"required_vram_gb": -1}, {"required_vram_gb": "lots"},
                                   {"resources": {"required_vram_gb": -0.5}}])
def test_existing_numeric_validation_still_applies_to_composed_requests(
        env: Env, extra: dict[str, Any]) -> None:
    response = env.compose(**extra)
    assert response.status_code == 422, response.text
    assert env.runner.submissions == []


# ------------------------------------------------------------------- route summary / MCP


def generate_media_summary(env: Env) -> str:
    spec = env.client.app.openapi()  # type: ignore[attr-defined]
    return spec["paths"][PATH.replace("p1", "{project_id}")]["post"]["summary"]


def assert_describes_a4(text: str) -> None:
    for name in VALID_PROFILES:
        assert name in text, f"{name} missing from: {text!r}"
    assert "models" in text and "prompt" in text and "profile" in text
    assert "RealGen" in text and "0.6" in text
    assert "Z-Image" in text
    assert "FLUX.2 klein" in text and "4B" in text
    default_pos = text.index("z_image_realgen")
    assert "default" in text[max(0, default_pos - 200): default_pos + 200].lower()


def test_route_summary_names_the_default_and_the_three_profiles(env: Env) -> None:
    assert_describes_a4(generate_media_summary(env))


def test_mcp_tool_description_carries_the_same_text(env: Env) -> None:
    tools = {t["name"]: t for t in manifest_module.build_manifest(env.app)["tools"]}
    assert_describes_a4(tools["generate_media"]["description"])
