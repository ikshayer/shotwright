"""Amendment 4 (A4.1, A4.4) through ComfyAdapter: model config, hashing, provenance, allowance.

Reuses the fakes of test_runner_contract. Written from CONTRACT.md only.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from orchestrator.adapters.comfyui.runner import ComfyAdapter
from orchestrator.adapters.comfyui.workflow import ImageRequest, ModelPin, graph_seed
from orchestrator.domain.entities import Job
from orchestrator.jobs.lease import GpuLease
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import parse_settings
from orchestrator.store.json_store import ProjectStore
from tests.adapters.comfyui.test_runner_contract import (
    FakePopen,
    FakeTransport,
    Host,
    Nvml,
    _cap_retry_sleep,  # noqa: F401  (autouse fixture, re-exported so the sleep cap applies here)
)
from tests.adapters.comfyui.zimage_fixtures import (
    FLUX_DIFFUSION,
    FLUX_TEXT_ENCODER_FP4,
    FLUX_VAE,
    Z_DIFFUSION,
    Z_LORA,
    Z_TEXT_ENCODER,
    Z_VAE,
    combined_object_info,
)

CATEGORY = {"diffusion": "diffusion_models", "text_encoder": "text_encoders",
            "vae": "vae", "lora": "loras"}
Z_FILES = {"diffusion": Z_DIFFUSION, "text_encoder": Z_TEXT_ENCODER, "vae": Z_VAE, "lora": Z_LORA}
FLUX_FILES = {"diffusion": FLUX_DIFFUSION, "text_encoder": FLUX_TEXT_ENCODER_FP4, "vae": FLUX_VAE}
#: A4.4: FLUX keeps 128.4 s; Z-Image is DERIVED 128.4 x 14.90 / 11.94, rounded up = 161 s.
#: A4.8 will replace the 161 with a measurement; update this constant, not the wiring test.
FLUX_STARTUP_ALLOWANCE = 128.4
Z_IMAGE_STARTUP_ALLOWANCE = 161


class FamilyTransport(FakeTransport):
    """FakeTransport whose /object_info knows both families and whose lora can be mutated."""

    def request(self, method: str, path: str, payload: dict[str, object] | None = None,
                *, timeout: float = 5.0) -> dict[str, object]:
        if method == "GET" and path == "/object_info":
            self.calls.append((method, path, payload, timeout))
            return combined_object_info()  # type: ignore[return-value]
        if method == "POST" and path == "/prompt" and self.mode == "mutate_lora":
            lora = self.root.path / "weights" / "loras" / Z_LORA
            self.root.write_bytes("weights/loras/" + Z_LORA, b"lora changed after pre-submit hashing")
            assert lora.is_file()
        return super().request(method, path, payload, timeout=timeout)


def setup_family(tmp_path: Path, *, family: str, lora: bool = False, mode: str = "success",
                 floor: float = 0.0, steps: int | None = None):
    root = ProjectRoot(tmp_path)
    install = root.make_dir("fake-comfy")
    root.write_bytes("fake-comfy/python_embeded/python.exe", b"python shim")
    root.write_bytes("fake-comfy/ComfyUI/main.py", b"entry point")
    files = Z_FILES if family == "z_image" else FLUX_FILES
    roles = [r for r in files if r != "lora" or lora]
    pins: list[ModelPin] = []
    for role in roles:
        data = (family + role + " fixture weight bytes").encode()
        path = root.write_bytes(f"weights/{CATEGORY[role]}/{files[role]}", data)
        pins.append(ModelPin(role=role, path=path, sha256=hashlib.sha256(data).hexdigest(),
                             source=f"https://models.example.invalid/{files[role]}",
                             license_id="fixture-license"))
    settings = parse_settings({
        "project_root": str(root.path),
        "binaries": {"comfyui_root": str(install), "comfyui_port": 8188},
        "budget": {"per_shot_wall_clock_seconds": 30.0, "retries_per_shot": 1},
        "resources": {"throughput_floor_per_second": floor, "vram_release_timeout_seconds": 0.05},
    })
    popen = FakePopen()
    transport = FamilyTransport(root, popen, mode=mode)
    adapter = ComfyAdapter(root, settings, popen=popen, transport=transport)
    nvml = Nvml()
    lease = GpuLease(root, settings, nvml=nvml, worker=adapter.worker,
                     pid_alive=lambda _pid: True, clock=time.monotonic, ttl_seconds=30.0)
    queue = JobQueue(root, settings, lease, ProjectStore(root), nvml=nvml, host=Host(),
                     quarantine=ProjectQuarantine(root), clock=time.monotonic)
    extra: dict[str, Any] = {"family": family}
    if lora:
        extra["lora_strength"] = 0.6
    if steps is not None:
        extra["steps"] = steps
    req = ImageRequest(shot_id="shot-1", shot_version=2, run_id="run-1", prompt="A quiet corridor",
                       degrade_seed=41, models=tuple(pins), **extra)
    job = Job(job_id="job-1", type="flux_still", shot_id="shot-1", run_id="run-1")
    return root, adapter, queue, job, req, popen, transport


def find_values(node: object, key: str) -> list[object]:
    found: list[object] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found.append(v)
            found.extend(find_values(v, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(find_values(item, key))
    return found


def model_config(popen: FakePopen) -> dict[str, Any]:
    argv = popen.calls[0][0]
    return json.loads(Path(argv[argv.index("--extra-model-paths-config") + 1]).read_text(encoding="utf-8"))


def model_config_dirs(config: dict[str, Any], category: str) -> list[Path]:
    return [Path(str(v)) for v in find_values(config, category)]


# ----------------------------------------------------------------------- success, z_image


def test_z_image_with_lora_succeeds_and_records_family_provenance(tmp_path: Path) -> None:
    root, adapter, queue, job, req, popen, transport = setup_family(
        tmp_path, family="z_image", lora=True)
    result = adapter.generate(req, job, queue=queue)
    run = result.generation_run
    assert run.status == "completed" and run.tool == "comfyui" and run.tool_version == "0.44.0"

    workflow = json.loads(root.resolve(result.workflow_path).read_text(encoding="utf-8"))
    assert workflow["9"]["class_type"] == "KSampler" and workflow["6"]["class_type"] == "LoraLoaderModelOnly"
    assert workflow["6"]["inputs"]["strength_model"] == 0.6
    assert workflow["9"]["inputs"]["steps"] == 25
    assert json.loads(run.invocation) == workflow
    assert run.seed == graph_seed(workflow) == workflow["9"]["inputs"]["seed"]

    settings = run.settings
    assert settings["family"] == "z_image"
    assert settings["cfg"] == 4.0
    assert settings["lora_strength"] == 0.6
    assert settings["sampler"] == "res_multistep"
    assert settings["steps"] == 25
    assert settings["precision"] == "default" and settings["memory_mode"] == "normal"
    assert settings["child_pid"] == 8383 and settings["prompt_id"] == "prompt-1"
    for pin in req.models:
        assert settings[f"{pin.role}_sha256"] == pin.sha256
        assert settings[f"{pin.role}_license"] == pin.license_id
        assert settings[f"{pin.role}_id"], f"{pin.role}_id is blank"
    assert {"lora_id", "lora_sha256", "lora_license"} <= set(settings)

    metadata = json.loads(root.resolve(result.metadata_path).read_text(encoding="utf-8"))
    assert "z_image" in find_values(metadata, "family")
    flat = json.dumps(metadata)
    for pin in req.models:
        assert pin.sha256 in flat and pin.source in flat
    assert transport.prompt_count == 1
    adapter.close()


def test_z_image_without_lora_records_no_lora_provenance(tmp_path: Path) -> None:
    root, adapter, queue, job, req, popen, _transport = setup_family(tmp_path, family="z_image")
    result = adapter.generate(req, job, queue=queue)
    settings = result.generation_run.settings
    workflow = json.loads(root.resolve(result.workflow_path).read_text(encoding="utf-8"))
    assert "6" not in workflow
    assert settings["family"] == "z_image" and settings["cfg"] == 4.0
    assert settings["sampler"] == "res_multistep" and settings["steps"] == 25
    assert "lora_strength" not in settings
    assert not [k for k in settings if k.startswith("lora")]
    assert result.generation_run.seed == graph_seed(workflow)
    adapter.close()


def test_flux_provenance_is_unchanged_plus_family_and_cfg(tmp_path: Path) -> None:
    root, adapter, queue, job, req, _popen, _transport = setup_family(tmp_path, family="flux2_klein")
    result = adapter.generate(req, job, queue=queue)
    settings = result.generation_run.settings
    workflow = json.loads(root.resolve(result.workflow_path).read_text(encoding="utf-8"))
    assert settings["family"] == "flux2_klein"
    assert settings["cfg"] == 1.0
    assert settings["sampler"] == "euler"
    assert settings["steps"] == 4
    assert "lora_strength" not in settings and not [k for k in settings if k.startswith("lora")]
    assert result.generation_run.seed == graph_seed(workflow) == workflow["9"]["inputs"]["noise_seed"]
    for pin in req.models:
        assert settings[f"{pin.role}_sha256"] == pin.sha256
        assert settings[f"{pin.role}_license"] == pin.license_id
    metadata = json.loads(root.resolve(result.metadata_path).read_text(encoding="utf-8"))
    assert "flux2_klein" in find_values(metadata, "family")
    adapter.close()


# ----------------------------------------------------------------------- model config


def test_model_config_has_loras_category_only_when_a_lora_pin_exists(tmp_path: Path) -> None:
    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    root, adapter, queue, job, req, popen, _transport = setup_family(
        tmp_path / "with", family="z_image", lora=True)
    adapter.generate(req, job, queue=queue)
    config = model_config(popen)
    assert model_config_dirs(config, "loras") == [root.resolve("weights/loras")]
    assert model_config_dirs(config, "diffusion_models") == [root.resolve("weights/diffusion_models")]
    assert model_config_dirs(config, "text_encoders") == [root.resolve("weights/text_encoders")]
    assert model_config_dirs(config, "vae") == [root.resolve("weights/vae")]
    assert True in find_values(config, "is_default")
    adapter.close()

    _root, adapter2, queue2, job2, req2, popen2, _transport2 = setup_family(
        tmp_path / "without", family="z_image")
    adapter2.generate(req2, job2, queue=queue2)
    assert find_values(model_config(popen2), "loras") == []
    adapter2.close()


def test_flux_model_config_has_no_loras_category(tmp_path: Path) -> None:
    _root, adapter, queue, job, req, popen, _transport = setup_family(tmp_path, family="flux2_klein")
    adapter.generate(req, job, queue=queue)
    assert find_values(model_config(popen), "loras") == []
    adapter.close()


# ----------------------------------------------------------------------- lora hashing


def test_lora_hash_mismatch_is_rejected_before_submission(tmp_path: Path) -> None:
    root, adapter, queue, job, req, _popen, transport = setup_family(
        tmp_path, family="z_image", lora=True)
    wrong = tuple(ModelPin(role=p.role, path=p.path, sha256="b" * 64, source=p.source,
                           license_id=p.license_id) if p.role == "lora" else p for p in req.models)
    with pytest.raises(JobError) as caught:
        adapter.generate(req.model_copy(update={"models": wrong}), job, queue=queue)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert not any(path == "/prompt" for _m, path, _p, _t in transport.calls)
    assert not any(root.resolve("generated").rglob("generation-run.json"))


def test_missing_lora_file_is_missing_model_artifact(tmp_path: Path) -> None:
    root, adapter, queue, job, req, _popen, transport = setup_family(
        tmp_path, family="z_image", lora=True)
    root.resolve("weights/loras/" + Z_LORA).unlink()
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue)
    assert caught.value.kind is JobErrorKind.MISSING_MODEL_ARTIFACT
    assert not any(path == "/prompt" for _m, path, _p, _t in transport.calls)


def test_lora_identity_is_rechecked_after_generation(tmp_path: Path) -> None:
    root, adapter, queue, job, req, _popen, _transport = setup_family(
        tmp_path, family="z_image", lora=True, mode="mutate_lora")
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert not any(root.resolve("generated").rglob("generation-run.json"))
    assert not queue.lease._owned


@pytest.mark.parametrize("role", ["diffusion", "text_encoder", "vae"])
def test_z_image_hash_mismatch_in_any_base_role_is_rejected(tmp_path: Path, role: str) -> None:
    _root, adapter, queue, job, req, _popen, transport = setup_family(
        tmp_path, family="z_image", lora=True)
    wrong = tuple(ModelPin(role=p.role, path=p.path, sha256="c" * 64, source=p.source,
                           license_id=p.license_id) if p.role == role else p for p in req.models)
    with pytest.raises(JobError) as caught:
        adapter.generate(req.model_copy(update={"models": wrong}), job, queue=queue)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert not any(path == "/prompt" for _m, path, _p, _t in transport.calls)


# ----------------------------------------------------------------------- startup allowance


@pytest.mark.parametrize("family,lora,expected", [
    ("flux2_klein", False, FLUX_STARTUP_ALLOWANCE),
    ("z_image", False, Z_IMAGE_STARTUP_ALLOWANCE),
    ("z_image", True, Z_IMAGE_STARTUP_ALLOWANCE),
])
def test_startup_allowance_is_per_family(
        tmp_path: Path, family: str, lora: bool, expected: float) -> None:
    _root, adapter, queue, job, req, _popen, _transport = setup_family(
        tmp_path, family=family, lora=lora, floor=0.001)
    original_run = queue.run
    forwarded: list[float | None] = []

    def capture(*args: Any, **kwargs: Any) -> Any:
        forwarded.append(kwargs.get("startup_allowance_seconds"))
        return original_run(*args, **kwargs)

    queue.run = capture  # type: ignore[method-assign]
    adapter.generate(req, job, queue=queue)
    assert forwarded == [expected]
    adapter.close()
