"""TTS job lifecycle, output receipts, Asset provenance, and legacy consent fields."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.domain.entities import Asset, Episode, GenerationRun, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobErrorKind
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER
from tests.service.conftest import Environment


PIPER_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/piper"
INDEX_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/indextts2"
PENDING_LICENSE = "Generated speech; rights classification pending creator decision"
INDEX_LICENSE = "bilibili Model Use License (IndexTTS-2; royalty-free below 100M MAU / RMB 1bn)"
INDEX_RESERVATIONS = {
    "required_vram_gb": 1.0,
    "required_ram_gb": 1.0,
    "required_disk_gb": 1.0,
}
EMOTION_KEYS = (
    "happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm",
)


def _wav_bytes(*, frames: int = 32, channels: int = 1, sample_rate: int = 24000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * channels * frames)
    return stream.getvalue()


def _non_pcm_wav_bytes() -> bytes:
    """A RIFF/WAVE container with IEEE float format tag 3, not PCM tag 1."""
    fmt = struct.pack("<HHIIHH", 3, 1, 24000, 96000, 4, 32)
    data = struct.pack("<ff", 0.0, 0.25)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def _all_key_values(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _all_key_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _all_key_values(value)


def _first_value(pairs: list[tuple[str, Any]], key: str) -> Any:
    return next(value for candidate, value in pairs if candidate == key)


def _path_values(node: Any, path: tuple[str, ...] = ()):
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = (*path, str(key))
            yield child_path, value
            yield from _path_values(value, child_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child_path = (*path, str(index))
            yield child_path, value
            yield from _path_values(value, child_path)


def _register_emotion_reference(env: Environment, *, reuse_scope: str = "internal evaluation only") -> Asset:
    data = _wav_bytes()
    path = "assets/audio/emotion/performance.wav"
    env.root.write_bytes(path, data)
    emotion = Asset.model_validate({
        "asset_id": "emotion", "type": "audio", "generated": False,
        "license": "ODbL and DbCL", "source_url": "https://example.invalid/crema-d/sample.wav",
        "file_path": path,
        "checksum": hashlib.sha256(data).hexdigest(),
        "voice_consent": "licensed_recording",
        "voice_consent_basis": "CREMA-D is licensed under ODbL and DbCL for reuse, not cloning.",
        "reuse_scope": reuse_scope,
    })
    env.store.save(emotion, expected_version=0)
    return emotion


def _has_report_value(report: Any, required_path_fragments: tuple[str, ...], expected: Any) -> bool:
    return any(
        all(fragment in "/".join(path).lower() for fragment in required_path_fragments)
        and value == expected
        for path, value in _path_values(report)
    )


def _output_path_from_request(request: Any, project_path: Path) -> Path:
    matches: list[Path] = []
    for key, value in _all_key_values(request):
        if not isinstance(value, str) or not value.lower().endswith(".wav"):
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = project_path / candidate
        if not candidate.exists() and any(token in str(key).lower() for token in ("output", "take", "wav")):
            matches.append(candidate)
    if not matches:
        raise AssertionError(f"worker request has no temporary output WAV path: {request!r}")
    return matches[-1]


class FakeSpeechProcess:
    def __init__(
        self, argv, *, project_path: Path, invalid: str | None, fail: bool,
        fail_after_output: bool = False, pause_after_output: bool = False,
        failure_stderr: bytes = b"controlled subprocess failure\n", **kwargs,
    ) -> None:
        self.argv = list(argv)
        self.kwargs = kwargs
        self.project_path = project_path
        self.invalid = invalid
        self.fail = fail
        self.fail_after_output = fail_after_output
        self.pause_after_output = pause_after_output
        self.failure_stderr = failure_stderr
        self.pid = 5074
        self.returncode: int | None = None
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO(b"controlled speech stderr\n")
        self.request: dict[str, Any] | None = None
        self.request_path: Path | None = None
        self.result_path: Path | None = None
        self.output_written = threading.Event()
        self.release_after_output = threading.Event()
        self.terminated = threading.Event()
        self.cancel_check = lambda: False
        if self.argv and self.argv[-1].lower().endswith(".json"):
            self.request_path = Path(self.argv[-1])
            if not self.request_path.is_absolute():
                cwd = Path(kwargs.get("cwd", Path.cwd()))
                self.request_path = cwd / self.request_path
            self.request = json.loads(self.request_path.read_text(encoding="utf-8"))
            self.output_path = _output_path_from_request(self.request, project_path)
            json_paths = [
                Path(value) if Path(value).is_absolute() else project_path / value
                for _key, value in _all_key_values(self.request)
                if isinstance(value, str) and value.lower().endswith(".json")
            ]
            self.result_path = next((path for path in reversed(json_paths)
                                     if path != self.request_path and not path.exists()), None)
        elif "-f" in self.argv:
            self.output_path = Path(self.argv[self.argv.index("-f") + 1])
        else:
            raise AssertionError(f"unknown speech command shape: {self.argv!r}")

    def _write_outputs(self) -> None:
        if self.invalid == "non_pcm":
            data = _non_pcm_wav_bytes()
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(data)
        else:
            frame_count = 0 if self.invalid == "zero_frames" else 32
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            sample_rate = 22050 if "piper" in self.argv else 24000
            self.output_path.write_bytes(_wav_bytes(frames=frame_count, sample_rate=sample_rate))
        if self.result_path is not None:
            self.result_path.parent.mkdir(parents=True, exist_ok=True)
            self.result_path.write_text(json.dumps({
                "load_seconds": 45.0,
                "generation_seconds": 2.5,
                "max_memory_allocated_bytes": 123456,
                "max_memory_reserved_bytes": 234567,
            }), encoding="utf-8")
        self.output_written.set()

    def _wait_if_gated(self) -> None:
        if self.pause_after_output:
            while not self.release_after_output.wait(0.01):
                if self.cancel_check():
                    self.kill()

    def communicate(self, input: bytes | None = None, timeout: float | None = None):
        if self.fail:
            if self.fail_after_output:
                self._write_outputs()
                self._wait_if_gated()
            elif b"download-forbidden" in self.failure_stderr.lower() and self.result_path is not None:
                self.result_path.parent.mkdir(parents=True, exist_ok=True)
                self.result_path.write_text(json.dumps({
                    "error_code": "download_forbidden",
                    "error_message": self.failure_stderr.decode("utf-8", errors="replace"),
                }), encoding="utf-8")
            self.returncode = -9 if self.terminated.is_set() else 17
            return b"", self.failure_stderr
        self._write_outputs()
        self._wait_if_gated()
        self.returncode = -9 if self.terminated.is_set() else 0
        return b"", b"controlled speech stderr\n"

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 17 if self.fail else 0
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.terminated.set()
        self.release_after_output.set()
        self.returncode = -9

    def terminate(self) -> None:
        self.kill()


class ProcessCallList(list[FakeSpeechProcess]):
    """Fake-Popen capture seam with an event for deterministic lifecycle tests."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def append(self, process: FakeSpeechProcess) -> None:
        super().append(process)
        self.started.set()


@pytest.fixture
def tts_execution(tmp_path: Path, request):
    """The only implementation-name assumption: adapter module/class and popen constructor."""
    params = request.param if hasattr(request, "param") else ("piper", None)
    engine, invalid = params[:2]
    fail = bool(params[2]) if len(params) > 2 else False
    fail_after_output = bool(params[3]) if len(params) > 3 else False
    pause_after_output = bool(params[4]) if len(params) > 4 else False
    failure_stderr = params[5] if len(params) > 5 else b"controlled subprocess failure\n"
    module_name, class_name = {
        "piper": ("orchestrator.adapters.tts.piper", "PiperAdapter"),
        "indextts2": ("orchestrator.adapters.tts.indextts2", "IndexTTS2Adapter"),
    }[engine]
    module = pytest.importorskip(module_name)
    adapter_type = getattr(module, class_name)

    parent = tmp_path / "projects"
    project_path = parent / "p1"
    project_path.mkdir(parents=True)
    env = Environment(project_path)
    if engine == "piper":
        env.settings.piper.enabled = True
    else:
        env.settings.indextts2.enabled = True
        for key, value in INDEX_RESERVATIONS.items():
            setattr(env.settings.indextts2, key, value)
    env.store.save(Project(project_id="p1", title=f"{engine} service contract"), expected_version=0)
    env.store.save(Episode(
        episode_id="ep1", project_id="p1", number=1, duration_target_sec=30,
    ), expected_version=0)
    env.store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))

    if engine == "indextts2":
        speaker_bytes = _wav_bytes()
        speaker_path = "assets/audio/speaker/source.wav"
        env.root.write_bytes(speaker_path, speaker_bytes)
        env.store.save(Asset.model_validate({
            "asset_id": "speaker", "type": "voice", "generated": False,
            "license": "creator supplied reference", "file_path": speaker_path,
            "checksum": hashlib.sha256(speaker_bytes).hexdigest(),
            "voice_consent": "licensed_for_cloning",
            "voice_consent_basis": "The licence permits cloning for this project.",
        }), expected_version=0)

    calls: ProcessCallList = ProcessCallList()
    cancel_check = [lambda: False]

    def set_cancel_check(check) -> None:
        cancel_check[0] = check or (lambda: False)

    env.adapter.set_cancel_check = set_cancel_check

    def popen(argv, **kwargs):
        process = FakeSpeechProcess(
            argv, project_path=project_path, invalid=invalid, fail=fail,
            fail_after_output=fail_after_output, pause_after_output=pause_after_output,
            failure_stderr=failure_stderr, **kwargs,
        )
        process.cancel_check = cancel_check[0]
        calls.append(process)
        return process

    backend = adapter_type(env.root, env.settings, popen=popen)
    lease_states: list[bool] = []

    def invoke(adapter_request, job, *, queue):
        lease_states.append(bool(getattr(getattr(queue, "lease", None), "_owned", False)))
        return backend.invoke(adapter_request, job, queue=queue)

    env.adapter.invoke = invoke
    api_sections = {
        "project_root": parent,
        "piper": {"enabled": engine == "piper"},
        "indextts2": {
            "enabled": engine == "indextts2",
            **(INDEX_RESERVATIONS if engine == "indextts2" else {}),
        },
    }
    api_settings = Settings.model_validate(api_sections)
    app = create_app(settings=api_settings, token=TOKEN, job_runner=env.runner)
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield client, env, calls, lease_states, engine
    env.runner.close()


def _body(engine: str, text: str = "  Quiet corridor.\nDo not normalize.  ") -> dict[str, Any]:
    if engine == "piper":
        return {"expected_version": 1, "text": text, "voice_id": "en_US-ryan-high"}
    return {
        "expected_version": 1,
        "text": text,
        "speaker_asset_id": "speaker",
        "emotion": {"mode": "vector", **dict(zip(EMOTION_KEYS, [0.2] * 8, strict=True))},
    }


def _post(client: TestClient, engine: str, body: dict[str, Any]):
    path = PIPER_PATH if engine == "piper" else INDEX_PATH
    return client.post(path, json=body, headers={TOKEN_HEADER: TOKEN})


def _run_id_from_job(job) -> str:
    assert job.run_id
    return job.run_id


@pytest.mark.parametrize("tts_execution", [("piper", "zero_frames"), ("piper", "non_pcm"),
                                            ("indextts2", "zero_frames"), ("indextts2", "non_pcm")],
                         indirect=True)
def test_invalid_wav_fails_run_without_take_or_asset(tts_execution) -> None:
    client, env, calls, _lease_states, engine = tts_execution
    response = _post(client, engine, _body(engine))
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])
    run_id = _run_id_from_job(job)

    assert job.state == "failed"
    assert len(calls) == 1
    run = env.store.load(GenerationRun, run_id)
    assert run.status == "failed"
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"tts-{run_id}")
    assert not env.root.resolve(f"generated/tts/s1/v0001/{run_id}/take.wav").exists()


@pytest.mark.parametrize("tts_execution", [("piper", None, True, True)], indirect=True)
def test_failed_run_after_output_is_cleaned_up(tts_execution) -> None:
    client, env, calls, _lease_states, engine = tts_execution
    response = _post(client, engine, _body(engine))
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])

    assert job.state == "failed"
    assert calls.started.is_set() and calls[0].output_written.is_set()
    assert job.run_id
    run = env.store.load(GenerationRun, job.run_id)
    assert run.status == "failed"
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"tts-{job.run_id}")
    assert not env.root.resolve(f"generated/tts/s1/v0001/{job.run_id}/take.wav").exists()


@pytest.mark.parametrize("tts_execution", [("piper", None)], indirect=True)
def test_asset_save_failure_repairs_saved_generation_run_and_discards_take(
    tts_execution, monkeypatch,
) -> None:
    client, env, _calls, _lease_states, engine = tts_execution
    assert engine == "piper"
    original_save = env.store.save

    def fail_generated_asset_save(record, *args, **kwargs):
        if isinstance(record, Asset) and record.asset_id.startswith("tts-"):
            raise OSError("controlled Asset registration failure")
        return original_save(record, *args, **kwargs)

    monkeypatch.setattr(env.store, "save", fail_generated_asset_save)
    response = _post(client, engine, _body(engine))
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])

    assert job.state == "failed"
    assert job.run_id
    take_path = f"generated/tts/s1/v0001/{job.run_id}/take.wav"
    report_path = f"generated/tts/s1/v0001/{job.run_id}/report.json"
    assert not env.root.resolve(take_path).exists()
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"tts-{job.run_id}")

    run = env.store.load(GenerationRun, job.run_id)
    assert run.status == "failed"
    assert take_path not in run.output_paths
    report = json.loads(env.root.read_text(report_path, encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["take_discarded"] is True


@pytest.mark.parametrize("tts_execution", [("piper", None, False, False, True)], indirect=True)
def test_cancel_after_fake_worker_wrote_wav_leaves_no_take_or_asset(tts_execution) -> None:
    client, env, calls, _lease_states, engine = tts_execution
    response = _post(client, engine, _body(engine))
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert calls.started.wait(5), "fake Piper process was not started"
    process = calls[0]
    assert process.output_written.wait(5), "fake Piper process did not produce its temporary WAV"

    env.runner.cancel("p1", job_id)
    job = env.runner.wait("p1", job_id)

    assert process.terminated.is_set()
    assert job.run_id
    run = env.store.load(GenerationRun, job.run_id)
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"tts-{job.run_id}")
    assert not env.root.resolve(f"generated/tts/s1/v0001/{job.run_id}/take.wav").exists()
    assert job.state in {"cancelled", "failed"}
    assert run.status == job.state


@pytest.mark.parametrize("tts_execution", [("piper", None, False, False, True)], indirect=True)
def test_completion_racing_cancel_has_one_consistent_take_outcome(tts_execution) -> None:
    client, env, calls, _lease_states, engine = tts_execution
    response = _post(client, engine, _body(engine))
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert calls.started.wait(5), "fake Piper process was not started"
    process = calls[0]
    assert process.output_written.wait(5), "fake Piper process did not produce its temporary WAV"
    race = threading.Barrier(2)
    cancel_failures: list[BaseException] = []

    def finish_worker() -> None:
        race.wait(timeout=5)
        process.release_after_output.set()

    def cancel_job() -> None:
        race.wait(timeout=5)
        try:
            env.runner.cancel("p1", job_id)
        except BaseException as error:
            cancel_failures.append(error)

    finisher = threading.Thread(target=finish_worker, daemon=True)
    canceller = threading.Thread(target=cancel_job, daemon=True)
    finisher.start()
    canceller.start()
    finisher.join(5)
    canceller.join(5)
    assert not finisher.is_alive() and not canceller.is_alive()
    job = env.runner.wait("p1", job_id)

    take = env.root.resolve(f"generated/tts/s1/v0001/{job.run_id}/take.wav")
    asset_id = f"tts-{job.run_id}"
    if job.state == "completed":
        assert take.is_file()
        asset = env.store.load(Asset, asset_id)
        assert asset.file_path == take.relative_to(env.root.path).as_posix()
    elif job.state == "cancelled":
        assert not take.exists()
        with pytest.raises(RecordNotFound):
            env.store.load(Asset, asset_id)
    else:
        pytest.fail(f"completion/cancel race produced unsupported job state {job.state!r}")


@pytest.mark.parametrize(
    "tts_execution",
    [("indextts2", None, True, False, False,
      b"download-forbidden: a model download was attempted and is forbidden\n")],
    indirect=True,
)
def test_worker_download_forbidden_failure_maps_to_missing_model_artifact(tts_execution) -> None:
    from orchestrator.platform.errors import JobErrorKind

    client, env, calls, _lease_states, engine = tts_execution
    response = _post(client, engine, _body(engine))
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])

    assert job.state == "failed"
    assert job.error_kind == JobErrorKind.MISSING_MODEL_ARTIFACT.value
    assert len(calls) == 1


@pytest.mark.parametrize("tts_execution", [("piper", None), ("indextts2", None)], indirect=True)
def test_success_retains_exact_take_report_run_asset_and_unchanged_shot(tts_execution) -> None:
    client, env, calls, lease_states, engine = tts_execution
    original_text = "  Quiet corridor.\r\nNo normalization.\u00a0  "
    shot_before = env.store.load(Shot, "s1", episode_id="ep1").model_dump(mode="json")
    outputs_before = env.store.list_shot_outputs("s1", episode_id="ep1")
    response = _post(client, engine, _body(engine, original_text))
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])
    run_id = _run_id_from_job(job)

    assert job.state == "completed"
    assert len(calls) == 1
    shot_after = env.store.load(Shot, "s1", episode_id="ep1")
    assert shot_after.model_dump(mode="json") == shot_before
    assert env.store.list_shot_outputs("s1", episode_id="ep1") == outputs_before
    assert shot_after.status.value == shot_before["status"]

    run_dir = f"generated/tts/s1/v0001/{run_id}"
    take_path = f"{run_dir}/take.wav"
    text_path = f"{run_dir}/text.txt"
    report_path = f"{run_dir}/report.json"
    stderr_path = f"{run_dir}/logs/stderr.txt"
    assert env.root.read_bytes(text_path) == original_text.encode("utf-8")
    wav_bytes = env.root.read_bytes(take_path)
    assert env.root.resolve(stderr_path).is_file()
    with wave.open(str(env.root.resolve(take_path)), "rb") as generated:
        assert generated.getcomptype() == "NONE"
        assert generated.getnchannels() in {1, 2}
        assert generated.getframerate() > 0
        assert generated.getnframes() > 0
        sample_rate = generated.getframerate()
        channels = generated.getnchannels()
        duration_seconds = generated.getnframes() / sample_rate

    report = json.loads(env.root.read_text(report_path, encoding="utf-8"))
    report_pairs = list(_all_key_values(report))
    report_keys = {key for key, _value in report_pairs}
    report_values = {value for _key, value in report_pairs if isinstance(value, str)}
    assert report["engine"] == engine
    assert report.get("text_sha256") == hashlib.sha256(original_text.encode("utf-8")).hexdigest()
    assert hashlib.sha256(wav_bytes).hexdigest() in report_values
    assert {"sample_rate", "channels", "duration_seconds"} <= report_keys
    assert _first_value(report_pairs, "sample_rate") == sample_rate
    assert _first_value(report_pairs, "channels") == channels
    assert _first_value(report_pairs, "duration_seconds") == pytest.approx(duration_seconds)
    assert any("tim" in str(key).lower() for key, _value in report_pairs)
    assert report_keys & {"engine_version", "piper_version", "runtime_version"}
    assert report_keys & {"request_settings", "settings", "request"}
    assert any(_first_value(report_pairs, key) for key in ("engine_version", "piper_version", "runtime_version")
               if key in report_keys)

    saved_run = env.store.load(GenerationRun, run_id)
    assert saved_run.status == "completed"
    for path in (take_path, text_path, report_path, stderr_path):
        assert path in saved_run.output_paths or path == saved_run.log_path

    asset = env.store.load(Asset, f"tts-{run_id}")
    assert asset.type == "voice"
    assert asset.generated is True
    assert asset.provider == engine
    assert asset.file_path == take_path
    assert asset.checksum == hashlib.sha256(wav_bytes).hexdigest()
    assert asset.license == PENDING_LICENSE
    if engine == "piper":
        # Contract 1.1 amendment: both catalogued voices are non-commercial, so their takes
        # are internal evaluation only (a publication blocker in credits).
        assert asset.reuse_scope == "internal evaluation only"
        assert asset.model_or_voice_license.startswith("CC BY-NC-SA 4.0")
        assert asset.voice_consent == "synthetic"
        assert asset.voice_consent_basis is None
        assert lease_states == [False]
        assert "en_US-ryan-high" in report_values
        assert "b3990d7606e183ec8dbfba70a4607074f162de1a0c412e0180d1ff60bb154eca" in report_values
        assert "c6d3b98f08315cb4bebf0d49d50fc4ff491b503c64b940cd3d5ca28543b48011" in report_values
    else:
        assert asset.model_or_voice_license == INDEX_LICENSE
        # A clone inherits its speaker reference's reuse scope.
        assert asset.reuse_scope == env.store.load(Asset, "speaker").reuse_scope
        assert asset.voice_consent == "licensed_for_cloning"
        assert asset.voice_consent_basis == "The licence permits cloning for this project."
        assert lease_states == [True]
        assert report.get("deterministic") is False
        assert "speaker_asset_id" in report_keys
        assert "voice_consent" in report_keys
        speaker_checksum = env.store.load(Asset, "speaker").checksum
        assert speaker_checksum in report_values
        assert "d9e41aac89fd00b3d71497fddb287b7f24613712" in report_values
        assert "ea9c2815ecc3874577c7ac158b97248c027250e5b05972fbbb1216b6d6539081" in report_values
        allocated_key = next(key for key in ("max_memory_allocated_bytes", "max_memory_allocated")
                             if key in report_keys)
        reserved_key = next(key for key in ("max_memory_reserved_bytes", "max_memory_reserved")
                            if key in report_keys)
        assert _first_value(report_pairs, allocated_key) == 123456
        assert _first_value(report_pairs, reserved_key) == 234567


@pytest.mark.parametrize("tts_execution", [("indextts2", None)], indirect=True)
def test_reference_emotion_report_run_inputs_and_take_restrictions(tts_execution) -> None:
    client, env, calls, _lease_states, engine = tts_execution
    assert engine == "indextts2"

    speaker, speaker_version = env.store.load_with_version(Asset, "speaker")
    speaker_fields = speaker.model_dump(mode="json")
    speaker_fields["reuse_scope"] = "project"
    env.store.save(Asset.model_validate(speaker_fields), expected_version=speaker_version)
    speaker = env.store.load(Asset, "speaker")
    emotion = _register_emotion_reference(env, reuse_scope="internal evaluation only")

    body = _body("indextts2")
    body["emotion"] = {
        "mode": "reference", "emotion_asset_id": emotion.asset_id, "strength": 0.63,
    }
    response = _post(client, engine, body)
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])
    assert job.state == "completed"
    assert len(calls) == 1
    run_id = _run_id_from_job(job)

    report = json.loads(env.root.read_text(
        f"generated/tts/s1/v0001/{run_id}/report.json", encoding="utf-8",
    ))
    emotion_sha256 = hashlib.sha256(env.root.read_bytes(emotion.file_path)).hexdigest()
    assert _has_report_value(report, ("emotion", "asset_id"), emotion.asset_id)
    assert _has_report_value(report, ("emotion", "sha256"), emotion_sha256)
    assert emotion.voice_consent == "licensed_recording"
    assert _has_report_value(report, ("emotion", "consent"), "licensed_recording")
    assert _has_report_value(report, ("emotion", "reuse_scope"), emotion.reuse_scope)
    assert _has_report_value(report, ("strength",), 0.63)

    run = env.store.load(GenerationRun, run_id)
    assert speaker.file_path in run.input_paths
    assert emotion.file_path in run.input_paths

    take = env.store.load(Asset, f"tts-{run_id}")
    assert take.voice_consent == speaker.voice_consent
    assert take.voice_consent != "licensed_recording"
    assert take.voice_consent_basis == speaker.voice_consent_basis
    assert take.reuse_scope == "internal evaluation only"


@pytest.mark.parametrize("tts_execution", [("indextts2", None)], indirect=True)
def test_cli_submits_reference_emotion_through_the_indextts2_command(
    tts_execution, monkeypatch,
) -> None:
    _client, env, calls, _lease_states, engine = tts_execution
    assert engine == "indextts2"
    emotion = _register_emotion_reference(env)

    from orchestrator.api import cli
    from tests.api.test_parity import _cli, _runner_and_command

    original_context = cli.OperationContext

    def operation_context(*args, **kwargs):
        kwargs["job_runner"] = env.runner
        return original_context(*args, **kwargs)

    monkeypatch.setattr(cli, "OperationContext", operation_context)
    settings = Settings.model_validate({
        "project_root": env.root.path.parent,
        "indextts2": {"enabled": True, **INDEX_RESERVATIONS},
    })
    body = {
        "expected_version": 1,
        "text": "The CLI should preserve the recorded emotion reference.",
        "speaker_asset_id": "speaker",
        "emotion": {
            "mode": "reference", "emotion_asset_id": emotion.asset_id, "strength": 0.42,
        },
    }
    runner, command = _runner_and_command(_cli())
    result = runner.invoke(command, [
        "generate-indextts2-speech", "--project-id", "p1", "--episode-id", "ep1",
        "--shot-id", "s1", "--data", json.dumps(body), "--json",
    ], obj={"settings": settings})

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0].returncode == 0


def test_existing_asset_records_without_consent_fields_still_load(tmp_path: Path) -> None:
    legacy = {
        "asset_id": "legacy-voice",
        "type": "voice",
        "generated": False,
        "license": "legacy record",
        "file_path": "assets/audio/legacy/source.wav",
        "checksum": "a" * 64,
    }
    asset = Asset.model_validate(legacy)

    assert asset.voice_consent is None
    assert asset.voice_consent_basis is None


@pytest.mark.parametrize("consent", ["creator_own_voice", "licensed_for_cloning", "licensed_recording"])
@pytest.mark.parametrize("basis", [None, "", "  "])
def test_non_synthetic_voice_consent_requires_nonblank_basis(consent: str, basis: str | None) -> None:
    fields: dict[str, Any] = {
        "asset_id": "consented", "type": "voice", "generated": False,
        "license": "fixture", "voice_consent": consent,
    }
    if basis is not None:
        fields["voice_consent_basis"] = basis

    with pytest.raises(ValueError):
        Asset.model_validate(fields)


def test_synthetic_consent_does_not_require_basis() -> None:
    asset = Asset.model_validate({
        "asset_id": "synthetic", "type": "voice", "generated": True,
        "license": PENDING_LICENSE, "voice_consent": "synthetic",
    })

    assert asset.voice_consent == "synthetic"
    assert asset.voice_consent_basis is None


@pytest.mark.parametrize("tts_execution", [("indextts2", None, True)], indirect=True)
def test_indextts2_failure_is_not_retried(tts_execution) -> None:
    client, env, calls, _lease_states, engine = tts_execution
    response = _post(client, engine, _body(engine))
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])

    assert job.state == "failed"
    assert len(calls) == 1
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"tts-{job.run_id}")


@pytest.fixture
def two_project_tts_jobs(tmp_path: Path):
    """Capture public TTS requests, then fake one shared worker across two project roots."""
    from orchestrator.platform.errors import JobError, JobErrorKind
    from orchestrator.service.runner import JobRunner

    parent = tmp_path / "projects"
    environments = {
        project_id: Environment(parent / project_id)
        for project_id in ("p1", "p2")
    }
    for project_id, episode_id, shot_id in (("p1", "ep1", "s1"), ("p2", "ep2", "s2")):
        store = environments[project_id].store
        store.save(Project(project_id=project_id, title=f"TTS callback {project_id}"), expected_version=0)
        store.save(Episode(
            episode_id=episode_id, project_id=project_id, number=1, duration_target_sec=30,
        ), expected_version=0)
        store.create(Shot(shot_id=shot_id, episode_id=episode_id, duration_sec=10))

    class CaptureRequests:
        def __init__(self) -> None:
            self.submissions: dict[str, Any] = {}

        def submit(self, project_id: str, request: Any):
            self.submissions[project_id] = request
            return SimpleNamespace(job_id=f"captured-{project_id}", run_id=f"run-{project_id}", state="queued")

    capture = CaptureRequests()
    api_settings = Settings.model_validate({"project_root": parent, "piper": {"enabled": True}})
    app = create_app(settings=api_settings, token=TOKEN, job_runner=capture)
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        for project_id, episode_id, shot_id in (("p1", "ep1", "s1"), ("p2", "ep2", "s2")):
            response = client.post(
                f"/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/piper",
                headers={TOKEN_HEADER: TOKEN},
                json={"expected_version": 1, "text": "Two projects can run concurrently.",
                      "voice_id": "en_US-ryan-high"},
            )
            assert response.status_code == 202, response.text
    assert set(capture.submissions) == {"p1", "p2"}

    class SharedFakeAdapter:
        """Single callback slot plus two held workers, matching the regression seam."""

        def __init__(self) -> None:
            self.cancel_check = lambda: False
            self.entered = {"p1": threading.Event(), "p2": threading.Event()}
            self.release_a = threading.Event()
            self.release_b = threading.Event()
            self.cancelled_b = threading.Event()
            self.succeed_a = False

        def set_cancel_check(self, check) -> None:
            self.cancel_check = check or (lambda: False)

        def invoke(self, request, job, *, queue):
            project_id = queue.root.path.name
            if project_id == "p1":
                self.entered["p1"].set()
                if self.succeed_a:
                    from orchestrator.adapters.tts.core import TtsResult
                    from orchestrator.service.runner import AdapterResult

                    speech = request.payload["request"]
                    run_dir = speech.run_directory
                    take_path = f"{run_dir}/take.wav"
                    text_path = f"{run_dir}/text.txt"
                    report_path = f"{run_dir}/report.json"
                    log_path = f"{run_dir}/logs/stderr.txt"
                    take_bytes = _wav_bytes()
                    text_bytes = speech.text.encode("utf-8")
                    report_bytes = json.dumps({"timings": {"wall_seconds": 0.25}}).encode("utf-8")
                    queue.root.write_bytes(take_path, take_bytes)
                    queue.root.write_bytes(text_path, text_bytes)
                    queue.root.write_bytes(report_path, report_bytes)
                    queue.root.write_bytes(log_path, b"fake persistence race worker\n")
                    result = TtsResult(
                        engine="piper", run_id=speech.run_id, tool_version="fake-piper",
                        take_path=take_path, take_sha256=hashlib.sha256(take_bytes).hexdigest(),
                        text_path=text_path, text_sha256=hashlib.sha256(text_bytes).hexdigest(),
                        report_path=report_path, report_sha256=hashlib.sha256(report_bytes).hexdigest(),
                        log_path=log_path, sample_rate=24000, channels=1, duration_seconds=32 / 24000,
                    )
                    return AdapterResult(artifact_path=take_path, value=result)
                assert self.release_a.wait(5), "test did not release job A"
                raise JobError(JobErrorKind.INTERNAL, "job A fake worker finished")
            else:
                self.entered["p2"].set()
                for _ in range(500):
                    if self.cancel_check():
                        self.cancelled_b.set()
                        raise JobError(JobErrorKind.CANCELLED, "job B was cancelled")
                    if self.release_b.wait(0.01):
                        break
                else:
                    raise AssertionError("job B did not receive its cancellation callback")
            raise JobError(JobErrorKind.CANCELLED, "job B was cancelled")

        def close(self) -> None:
            pass

    adapter = SharedFakeAdapter()

    class SharedRegistry:
        def get(self, _kind):
            return adapter

        def close(self) -> None:
            adapter.close()

    registry = SharedRegistry()
    stores = {project_id: environment.store for project_id, environment in environments.items()}
    queues = {project_id: environment._queue for project_id, environment in environments.items()}
    runner = JobRunner(
        lambda project_id: stores[project_id],
        environments["p1"].settings,
        queue_for=lambda project_id: queues[project_id],
        adapters=registry,
        clock=lambda: 1000.0,
    )
    yield environments, runner, adapter, capture
    runner.close()
    for environment in environments.values():
        environment.runner.close()


def test_project_b_cancel_callback_survives_project_a_finishing(two_project_tts_jobs) -> None:
    _environments, runner, adapter, capture = two_project_tts_jobs
    job_a = runner.submit("p1", capture.submissions["p1"])
    job_b = runner.submit("p2", capture.submissions["p2"])
    outcomes: dict[str, Any] = {}
    errors: list[BaseException] = []

    def wait_for(project_id: str, job_id: str) -> None:
        try:
            outcomes[project_id] = runner.wait(project_id, job_id)
        except BaseException as error:  # noqa: BLE001 - preserve worker-thread failures for the assertion
            errors.append(error)

    a_waiter = threading.Thread(target=wait_for, args=("p1", job_a.job_id), daemon=True)
    b_waiter = threading.Thread(target=wait_for, args=("p2", job_b.job_id), daemon=True)
    a_waiter.start()
    b_waiter.start()
    assert adapter.entered["p1"].wait(5), f"job A did not start; outcomes={outcomes!r}, errors={errors!r}"
    assert adapter.entered["p2"].wait(5), f"job B did not start while A was running; outcomes={outcomes!r}, errors={errors!r}"

    adapter.release_a.set()
    a_waiter.join(5)
    assert not a_waiter.is_alive(), "job A did not finish"
    assert outcomes.get("p1") is not None and outcomes["p1"].state == "failed"

    try:
        runner.cancel("p2", job_b.job_id)
        assert adapter.cancelled_b.wait(2), "job B lost its cancellation callback after job A finished"
    finally:
        adapter.release_b.set()
        b_waiter.join(5)
    assert not b_waiter.is_alive()
    assert not errors
    assert outcomes["p2"].state == "cancelled"


def test_cancel_during_tts_persistence_is_nonblocking_and_too_late_to_cancel(
    two_project_tts_jobs, monkeypatch,
) -> None:
    import orchestrator.service.tts as tts_service

    _environments, runner, adapter, capture = two_project_tts_jobs
    adapter.succeed_a = True
    persistence_started = threading.Event()
    release_persistence = threading.Event()
    original_persist = tts_service.persist_tts_success

    def held_persist(store, job, result, request):
        persistence_started.set()
        assert release_persistence.wait(5), "test did not release TTS result persistence"
        return original_persist(store, job, result, request)

    monkeypatch.setattr(tts_service, "persist_tts_success", held_persist)
    job_a = runner.submit("p1", capture.submissions["p1"])
    assert persistence_started.wait(5), "successful TTS job did not enter result persistence"
    job_b = runner.submit("p2", capture.submissions["p2"])
    assert adapter.entered["p2"].wait(5), "second project's job did not run during persistence"

    cancel_results: dict[str, Any] = {}
    cancel_errors: list[BaseException] = []
    cancel_done = {"p1": threading.Event(), "p2": threading.Event()}
    cancel_threads: list[threading.Thread] = []

    def cancel(project_id: str, job_id: str) -> None:
        try:
            cancel_results[project_id] = runner.cancel(project_id, job_id)
        except BaseException as error:  # noqa: BLE001 - preserve thread failures for the assertion
            cancel_errors.append(error)
        finally:
            cancel_done[project_id].set()

    try:
        for project_id, job_id in (("p1", job_a.job_id), ("p2", job_b.job_id)):
            thread = threading.Thread(target=cancel, args=(project_id, job_id), daemon=True)
            cancel_threads.append(thread)
            thread.start()
            assert cancel_done[project_id].wait(1), (
                f"cancel for {project_id} blocked while TTS persistence was held"
            )
            assert not release_persistence.is_set()

        assert not cancel_errors
        assert cancel_results["p1"].state != "cancelled"
        assert cancel_results["p2"].state == "cancelled"
        assert adapter.cancelled_b.wait(2), "project p2 did not observe its cancellation"
    finally:
        release_persistence.set()
        adapter.release_b.set()
        for thread in cancel_threads:
            thread.join(5)

    completed_a = runner.wait("p1", job_a.job_id)
    cancelled_b = runner.wait("p2", job_b.job_id)
    assert not cancel_errors
    assert completed_a.state == "completed"
    registered_asset = _environments["p1"].store.load(Asset, f"tts-{completed_a.run_id}")
    assert registered_asset.file_path.endswith("/take.wav")
    assert cancelled_b.state == "cancelled"



# --- reuse scope of IndexTTS-2 takes (contract 1.1 amendment; review 3) ----------------

def _scope_store(tmp_path: Path, reference_scope: str) -> ProjectStore:
    (tmp_path / "scope_project").mkdir(parents=True, exist_ok=True)
    store = ProjectStore(ProjectRoot(tmp_path / "scope_project"))
    store.save(Asset.model_validate({
        "asset_id": "speaker", "type": "voice", "generated": True, "license": "test",
        "file_path": "assets/audio/speaker/source.wav", "checksum": "0" * 64,
        "voice_consent": "synthetic", "reuse_scope": reference_scope,
    }), expected_version=0)
    return store


@pytest.mark.parametrize("recorded,current,expected", [
    # A restriction added to the reference while synthesising still reaches the take.
    ("project", "internal evaluation only", "internal evaluation only"),
    # A reference loosened after submission does not loosen the take.
    ("internal evaluation only", "project", "internal evaluation only"),
    # A request built before speaker_reuse_scope existed reads the current scope.
    (None, "internal evaluation only", "internal evaluation only"),
    (None, "project", "project"),
    ("project", "project", "project"),
])
def test_indextts2_take_scope_is_never_less_restrictive_than_its_reference(
    tmp_path: Path, recorded: str | None, current: str, expected: str,
) -> None:
    from orchestrator.service.tts import _restricted_scope

    store = _scope_store(tmp_path, current)
    request = SimpleNamespace(speaker_asset_id="speaker", speaker_reuse_scope=recorded)
    assert _restricted_scope(store, request) == expected
