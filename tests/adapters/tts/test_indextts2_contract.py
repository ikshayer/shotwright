"""IndexTTS-2 process contract with a fake subprocess; never runs the worker."""

from __future__ import annotations

import io
import json
import ast
import importlib.util
import sys
import threading
import types
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.domain.entities import Asset, Episode, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER
from tests.service.conftest import Environment


INDEX_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/indextts2"
INDEX_RESERVATIONS = {
    "required_vram_gb": 1.0,
    "required_ram_gb": 1.0,
    "required_disk_gb": 1.0,
}
EMOTION_KEYS = (
    "happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm",
)


def _write_wav(path: Path, *, channels: int = 1, sample_rate: int = 24000, frames: int = 48) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * channels * frames)


def _wav_bytes() -> bytes:
    from io import BytesIO

    stream = BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x00\x00" * 24)
    return stream.getvalue()


def _keyed_strings(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value
            yield from _keyed_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _keyed_strings(value)


def _change_registered_speaker(env: Environment, change: str) -> None:
    """Mutate the speaker after submission, from the fake adapter invocation seam."""
    asset, expected_version = env.store.load_with_version(Asset, "speaker")
    if change == "bytes":
        env.root.write_bytes(asset.file_path, _wav_bytes() + b"changed after submission")
        return

    fields = asset.model_dump(mode="json")
    if change == "checksum":
        fields["checksum"] = "0" * 64
    elif change == "type":
        fields["type"] = "image"
    elif change == "consent":
        fields["voice_consent"] = "creator_own_voice"
        fields["voice_consent_basis"] = "Changed after the speech request was submitted."
    else:  # pragma: no cover - only the parameterized cases above are supported
        raise AssertionError(f"unknown speaker mutation {change!r}")
    env.store.save(Asset.model_validate(fields), expected_version=expected_version)


class FakeIndexProcess:
    """Writes the worker's declared temporary WAV and measurement result."""

    def __init__(self, argv, *, project_path: Path, **kwargs) -> None:
        self.argv = list(argv)
        self.kwargs = kwargs
        self.project_path = project_path
        self.pid = 3719
        self.returncode: int | None = None
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO(b"fake IndexTTS-2 stderr\n")
        self.input_bytes: bytes | None = None
        self.request_path = Path(self.argv[-1])
        if not self.request_path.is_absolute():
            self.request_path = Path(kwargs["cwd"]) / self.request_path
        self.request = json.loads(self.request_path.read_text(encoding="utf-8"))
        values = list(_keyed_strings(self.request))
        self.reference_snapshots: dict[str, tuple[Path, bytes | None]] = {}
        for key, value in values:
            if (isinstance(value, str) and value.lower().endswith(".wav")
                    and any(word in key.lower() for word in ("speaker", "prompt", "reference"))):
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = self.project_path / candidate
                self.reference_snapshots[value] = (
                    candidate, candidate.read_bytes() if candidate.is_file() else None,
                )
        wav_values = [
            value for _key, value in values
            if isinstance(value, str) and value.lower().endswith(".wav")
        ]
        outputs = []
        for value in wav_values:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = self.project_path / candidate
            if not candidate.exists():
                outputs.append(candidate)
        assert outputs, f"worker request declared no not-yet-created WAV output: {self.request!r}"
        self.output_path = outputs[-1]

        json_values = [
            value for _key, value in values
            if isinstance(value, str) and value.lower().endswith(".json")
        ]
        result_candidates = []
        for value in json_values:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = self.project_path / candidate
            if candidate != self.request_path and not candidate.exists():
                result_candidates.append(candidate)
        self.result_path = result_candidates[-1] if result_candidates else None

    def _complete(self) -> None:
        _write_wav(self.output_path)
        if self.result_path is not None:
            self.result_path.parent.mkdir(parents=True, exist_ok=True)
            self.result_path.write_text(json.dumps({
                "load_seconds": 45.0,
                "generation_seconds": 2.5,
                "max_memory_allocated_bytes": 123456,
                "max_memory_reserved_bytes": 234567,
            }), encoding="utf-8")

    def communicate(self, input: bytes | None = None, timeout: float | None = None):
        self.input_bytes = input
        self._complete()
        self.returncode = 0
        return b"", b"fake IndexTTS-2 stderr\n"

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.kill()


@pytest.fixture
def indextts2_harness(tmp_path: Path, request):
    """Assumed public adapter name/constructor: IndexTTS2Adapter(root, settings, popen=...)."""
    mutation = getattr(request, "param", None)
    module = pytest.importorskip("orchestrator.adapters.tts.indextts2")
    adapter_type = module.IndexTTS2Adapter
    repo = Path(__file__).resolve().parents[3]
    index_repo = repo / "spikes" / "05_voice" / "_work" / "index-tts"
    if not (index_repo / ".venv" / "Scripts" / "python.exe").is_file() or not (
        index_repo / "checkpoints" / "config.yaml"
    ).is_file():
        pytest.skip("the pinned IndexTTS-2 interpreter is not installed; no model will be started")

    parent = tmp_path / "projects"
    project_path = parent / "p1"
    project_path.mkdir(parents=True)
    env = Environment(project_path)
    env.settings.indextts2.enabled = True
    env.settings.indextts2.required_vram_gb = INDEX_RESERVATIONS["required_vram_gb"]
    env.settings.indextts2.required_ram_gb = INDEX_RESERVATIONS["required_ram_gb"]
    env.settings.indextts2.required_disk_gb = INDEX_RESERVATIONS["required_disk_gb"]
    env.store.save(Project(project_id="p1", title="IndexTTS-2 adapter contract"), expected_version=0)
    env.store.save(Episode(
        episode_id="ep1", project_id="p1", number=1, duration_target_sec=30,
    ), expected_version=0)
    env.store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))
    speaker_path = "assets/audio/speaker/source.wav"
    speaker_data = _wav_bytes()
    env.root.write_bytes(speaker_path, speaker_data)
    env.store.save(Asset.model_validate({
        "asset_id": "speaker", "type": "voice", "generated": False,
        "license": "creator supplied reference", "file_path": speaker_path,
        "checksum": __import__("hashlib").sha256(speaker_data).hexdigest(),
        "voice_consent": "creator_own_voice",
        "voice_consent_basis": "Creator recorded and approved this reference.",
    }), expected_version=0)

    calls: list[FakeIndexProcess] = []
    lease_states: list[bool] = []

    def popen(argv, **kwargs):
        process = FakeIndexProcess(argv, project_path=project_path, **kwargs)
        calls.append(process)
        return process

    backend = adapter_type(env.root, env.settings, popen=popen)
    # Expose the fake-backed adapter to lifecycle tests that need to deliver worker
    # control signals at a precise point in synthesize_managed.
    env.indextts2_backend = backend
    # Production passes the runner's cancel check to the adapter (the app's runtime adapter
    # installs it per job); the harness captures it the same way and passes it on.
    cancel_check = [None]

    def set_cancel_check(check) -> None:
        cancel_check[0] = check

    def invoke(request, job, *, queue):
        if mutation is not None:
            _change_registered_speaker(env, mutation)
        lease_states.append(bool(getattr(getattr(queue, "lease", None), "_owned", False)))
        return backend.invoke(request, job, queue=queue, cancel=cancel_check[0])

    env.adapter.set_cancel_check = set_cancel_check
    env.adapter.invoke = invoke
    api_settings = Settings.model_validate({
        "project_root": parent,
        "indextts2": {"enabled": True, **INDEX_RESERVATIONS},
    })
    app = create_app(settings=api_settings, token=TOKEN, job_runner=env.runner)
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield client, env, calls, lease_states, project_path, index_repo
    env.runner.close()


def _submit_and_wait(client, env: Environment, body: dict[str, Any]):
    response = client.post(INDEX_PATH, json=body, headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])
    return response, job


def test_indextts2_is_one_offline_subprocess_with_ordered_emotion_vector(indextts2_harness) -> None:
    client, env, calls, lease_states, _project, index_repo = indextts2_harness
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    vector = dict(zip(EMOTION_KEYS, values, strict=True))
    response, job = _submit_and_wait(client, env, {
        "expected_version": 1,
        "text": "Someone is still here.",
        "speaker_asset_id": "speaker",
        "emotion": {"mode": "vector", **vector},
    })

    assert job.state == "completed"
    assert len(calls) == 1
    process = calls[0]
    argv = process.argv
    assert isinstance(argv, list)
    assert all(isinstance(item, str) for item in argv)
    assert argv[1] == "-B" and argv[2] == "-I"
    assert Path(argv[0]).as_posix().endswith("index-tts/.venv/Scripts/python.exe")
    assert Path(argv[3]).name == "indextts2_worker.py"
    assert Path(argv[4]).name.endswith(".json")
    assert Path(process.kwargs["cwd"]).resolve() == index_repo.resolve()
    assert process.kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert process.kwargs["env"]["TRANSFORMERS_OFFLINE"] == "1"
    emotion_vectors = [
        value for key, value in _keyed_strings(process.request)
        if key == "emo_vector" and isinstance(value, list)
    ]
    assert emotion_vectors == [values]
    assert process.input_bytes in (None, b"")
    assert lease_states == [True], "IndexTTS-2 subprocess must run while the GPU lease is held"
    assert response.json()["job_id"]


def test_cancel_during_run_file_preparation_prevents_index_worker_launch(
    indextts2_harness, monkeypatch,
) -> None:
    client, env, calls, _lease_states, _project, _index_repo = indextts2_harness
    preparing_run_dir = threading.Event()
    release_preparation = threading.Event()
    original_make_dir = env.root.make_dir

    def delayed_make_dir(relative_path, *args, **kwargs):
        if str(relative_path).replace("\\", "/").startswith("generated/tts/"):
            preparing_run_dir.set()
            assert release_preparation.wait(5), "test did not release run-file preparation"
        return original_make_dir(relative_path, *args, **kwargs)

    monkeypatch.setattr(env.root, "make_dir", delayed_make_dir)
    response = client.post(INDEX_PATH, json={
        "expected_version": 1,
        "text": "The worker must never launch after cancellation.",
        "speaker_asset_id": "speaker",
        "emotion": {"mode": "none"},
    }, headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert preparing_run_dir.wait(5), "the job did not enter run-directory preparation"
    try:
        env.runner.cancel("p1", job_id)
    finally:
        release_preparation.set()
    job = env.runner.wait("p1", job_id)

    assert job.state in {"cancelled", "failed"}
    assert job.error_kind in {JobErrorKind.CANCELLED.value, JobErrorKind.BUDGET_EXCEEDED.value}
    assert calls == [], "IndexTTS-2 subprocess launched after cancellation during preparation"
    if job.run_id:
        assert not env.root.resolve(f"generated/tts/s1/v0001/{job.run_id}/take.wav").exists()


@pytest.mark.parametrize("stop_method", ["terminate", "unload"])
def test_index_worker_stop_during_readiness_blocks_launch_but_not_next_job(
    indextts2_harness, monkeypatch, stop_method: str,
) -> None:
    client, env, calls, _lease_states, _project, _index_repo = indextts2_harness
    backend = env.indextts2_backend
    readiness_entered = threading.Event()
    release_readiness = threading.Event()
    original_require_ready = backend.require_ready
    readiness_calls = 0

    def gated_require_ready() -> None:
        nonlocal readiness_calls
        readiness_calls += 1
        if readiness_calls == 1:
            readiness_entered.set()
            assert release_readiness.wait(5), "test did not release IndexTTS-2 readiness check"
        original_require_ready()

    monkeypatch.setattr(backend, "require_ready", gated_require_ready)
    response = client.post(INDEX_PATH, json={
        "expected_version": 1,
        "text": "A stop during readiness must prevent the worker launch.",
        "speaker_asset_id": "speaker",
        "emotion": {"mode": "none"},
    }, headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 202, response.text
    first_job_id = response.json()["job_id"]
    assert readiness_entered.wait(5), "IndexTTS-2 job did not enter synthesize_managed readiness"

    try:
        getattr(backend.worker, stop_method)()
    finally:
        release_readiness.set()

    stopped_job = env.runner.wait("p1", first_job_id)
    assert stopped_job.state == "cancelled"
    assert stopped_job.error_kind == JobErrorKind.CANCELLED.value
    assert calls == [], "IndexTTS-2 subprocess launched after a pre-attempt worker stop"
    assert not env.root.resolve(
        f"generated/tts/s1/v0001/{stopped_job.run_id}/take.wav"
    ).exists()

    _shot, current_version = env.store.load_with_version(Shot, "s1", episode_id="ep1")
    _next_response, next_job = _submit_and_wait(client, env, {
        "expected_version": current_version,
        "text": "The next job should start on the same adapter.",
        "speaker_asset_id": "speaker",
        "emotion": {"mode": "none"},
    })
    assert readiness_calls == 2
    assert next_job.state == "completed"
    assert len(calls) == 1


def test_indextts2_worker_gets_private_speaker_copy_and_reports_its_hash(indextts2_harness) -> None:
    client, env, calls, _lease_states, project_path, _index_repo = indextts2_harness
    speaker = env.store.load(Asset, "speaker")
    original_bytes = env.root.read_bytes(speaker.file_path)
    response, job = _submit_and_wait(client, env, {
        "expected_version": 1,
        "text": "Keep the same voice identity.",
        "speaker_asset_id": "speaker",
        "emotion": {"mode": "none"},
    })

    assert response.status_code == 202
    assert job.state == "completed"
    assert len(calls) == 1
    references = [
        value for key, value in _keyed_strings(calls[0].request)
        if isinstance(value, str)
        and value.lower().endswith(".wav")
        and any(word in key.lower() for word in ("speaker", "prompt", "reference"))
    ]
    assert len(references) == 1, calls[0].request
    speaker_copy, copy_bytes = calls[0].reference_snapshots[references[0]]
    run_dir = env.root.resolve(f"generated/tts/s1/v0001/{job.run_id}")
    assert speaker_copy.resolve().is_relative_to(run_dir.resolve())
    assert speaker_copy.resolve() != env.root.resolve(speaker.file_path).resolve()
    assert copy_bytes == original_bytes

    report = json.loads(env.root.read_text(
        f"generated/tts/s1/v0001/{job.run_id}/report.json", encoding="utf-8",
    ))
    reported_strings = {value for _key, value in _keyed_strings(report) if isinstance(value, str)}
    assert __import__("hashlib").sha256(copy_bytes).hexdigest() in reported_strings


@pytest.mark.parametrize("indextts2_harness", ["bytes", "checksum", "type", "consent"], indirect=True)
def test_changed_speaker_binding_is_refused_before_worker_launch(indextts2_harness) -> None:
    client, env, calls, _lease_states, _project, _index_repo = indextts2_harness
    response = client.post(INDEX_PATH, json={
        "expected_version": 1,
        "text": "Use the bound speaker reference.",
        "speaker_asset_id": "speaker",
        "emotion": {"mode": "none"},
    }, headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])

    assert job.state == "failed"
    assert calls == []
    assert job.run_id
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"tts-{job.run_id}")
    assert not env.root.resolve(f"generated/tts/s1/v0001/{job.run_id}/take.wav").exists()


def _worker_module():
    worker_path = Path(__file__).resolve().parents[3] / "orchestrator/adapters/tts/indextts2_worker.py"
    spec = importlib.util.spec_from_file_location("tts_worker_contract_test", worker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, worker_path


def test_worker_blocks_all_download_entry_points_before_importing_indextts(monkeypatch) -> None:
    worker, worker_path = _worker_module()

    def package(name: str) -> types.ModuleType:
        module = types.ModuleType(name)
        module.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, module)
        return module

    def child(name: str, parent: types.ModuleType, attribute: str) -> types.ModuleType:
        module = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        setattr(parent, attribute, module)
        return module

    hf = package("huggingface_hub")
    hf_file = child("huggingface_hub.file_download", hf, "file_download")
    modelscope = package("modelscope")
    modelscope_hub = package("modelscope.hub")
    modelscope.hub = modelscope_hub
    modelscope_snapshot = child("modelscope.hub.snapshot_download", modelscope_hub, "snapshot_download")
    modelscope_file = child("modelscope.hub.file_download", modelscope_hub, "file_download")

    worker._forbid_downloads()
    endpoints = (
        (hf, "snapshot_download"), (hf, "hf_hub_download"),
        (hf_file, "hf_hub_download"), (modelscope, "snapshot_download"),
        (modelscope, "model_file_download"), (modelscope_snapshot, "snapshot_download"),
        (modelscope_file, "model_file_download"),
    )
    for module, name in endpoints:
        with pytest.raises(worker.WorkerFailure) as caught:
            getattr(module, name)("would-download-this")
        assert caught.value.code == "download_forbidden"

    tree = ast.parse(worker_path.read_text(encoding="utf-8"))
    synthesise = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "synthesise")
    block_idx = next(index for index, node in enumerate(synthesise.body)
                     if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                     and isinstance(node.value.func, ast.Name) and node.value.func.id == "_forbid_downloads")
    import_idx = next(index for index, node in enumerate(synthesise.body)
                      if isinstance(node, ast.ImportFrom) and node.module == "indextts.infer_v2")
    assert block_idx < import_idx


def test_indextts2_worker_disables_cuda_kernel_without_importing_torch() -> None:
    _worker, worker_path = _worker_module()
    tree = ast.parse(worker_path.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "IndexTTS2"]
    assert len(calls) == 1
    kernel = next((keyword for keyword in calls[0].keywords if keyword.arg == "use_cuda_kernel"), None)
    assert kernel is not None and isinstance(kernel.value, ast.Constant) and kernel.value.value is False



