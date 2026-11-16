"""HTTP contract tests for TTS routing, validation, consent, and uploads."""

from __future__ import annotations

import hashlib
import builtins
import json
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.domain.entities import Asset, Episode, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER, operations
from tests.api.test_parity import _manifest


PIPER_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/piper"
INDEX_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/indextts2"
VOICE_WAV = "assets/audio/speaker/source.wav"
INDEX_RESERVATIONS = {
    "required_vram_gb": 1.0,
    "required_ram_gb": 1.0,
    "required_disk_gb": 1.0,
}


class CaptureRunner:
    """A submission seam that proves validation precedes job creation."""

    def __init__(self) -> None:
        self.submissions: list[tuple[str, Any]] = []

    def submit(self, project_id: str, request: Any) -> SimpleNamespace:
        self.submissions.append((project_id, request))
        return SimpleNamespace(job_id="tts-job", run_id="tts-run", state="queued")


@pytest.fixture
def tts_api(tmp_path: Path):
    parent = tmp_path / "projects"
    parent.mkdir()
    project_path = parent / "p1"
    project_path.mkdir()
    settings = Settings.model_validate({
        "project_root": parent,
        "piper": {"enabled": True},
        "indextts2": {"enabled": True, **INDEX_RESERVATIONS},
    })
    runner = CaptureRunner()
    app = create_app(settings=settings, token=TOKEN, job_runner=runner)
    root = ProjectRoot(project_path)
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="TTS contract"), expected_version=0)
    store.save(Episode(
        episode_id="ep1", project_id="p1", number=1, duration_target_sec=30,
    ), expected_version=0)
    store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield client, app, settings, store, root, runner


def _wav_bytes(*, sample_rate: int = 22050, frames: int = 16) -> bytes:
    from io import BytesIO

    stream = BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * frames)
    return stream.getvalue()


def _register_speaker(
    store: ProjectStore,
    root: ProjectRoot,
    *,
    asset_id: str = "speaker",
    asset_type: str = "voice",
    filename: str = "source.wav",
    file_bytes: bytes | None = None,
    checksum: str | None = None,
    voice_consent: str | None = "synthetic",
    voice_consent_basis: str | None = None,
) -> None:
    data = _wav_bytes() if file_bytes is None else file_bytes
    relative = f"assets/audio/{asset_id}/{filename}"
    root.write_bytes(relative, data)
    fields: dict[str, Any] = {
        "asset_id": asset_id,
        "type": asset_type,
        "generated": False,
        "license": "creator supplied reference",
        "file_path": relative,
        "checksum": checksum or hashlib.sha256(data).hexdigest(),
    }
    if voice_consent is not None:
        fields["voice_consent"] = voice_consent
    if voice_consent_basis is not None:
        fields["voice_consent_basis"] = voice_consent_basis
    store.save(Asset.model_validate(fields), expected_version=0)


def _snapshot(root: ProjectRoot) -> dict[str, bytes]:
    return {
        path.relative_to(root.path).as_posix(): path.read_bytes()
        for path in root.path.rglob("*")
        if path.is_file()
    }


def _piper_body(**updates: Any) -> dict[str, Any]:
    return {
        "expected_version": 1,
        "text": "Quiet corridor.",
        "voice_id": "en_US-ryan-high",
        **updates,
    }


def _index_body(**updates: Any) -> dict[str, Any]:
    return {
        "expected_version": 1,
        "text": "Someone is still in the room.",
        "speaker_asset_id": "speaker",
        "emotion": {"mode": "none"},
        **updates,
    }


def _post(client: TestClient, path: str, body: dict[str, Any]):
    return client.post(path, json=body, headers={TOKEN_HEADER: TOKEN})


def test_tts_operations_have_exact_registry_methods_paths_and_manifest(tts_api) -> None:
    client, app, *_ = tts_api
    expected = {
        "inspect_tts": ("GET", "/models/tts"),
        "generate_piper_speech": (
            "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/piper",
        ),
        "generate_indextts2_speech": (
            "POST", "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/indextts2",
        ),
    }
    found = {op.name: op for op in operations(app)}

    assert set(expected) <= set(found)
    for name, (method, path) in expected.items():
        assert (found[name].method, found[name].path) == (method, path)
        assert found[name].mutating is (method != "GET")

    names = {
        str(item.get("name", "")).lower().replace("-", "_")
        for item in _manifest(app)
    }
    assert {"generate_piper_speech", "generate_indextts2_speech"} <= names
    manifest_response = client.get("/tool-manifest")
    assert manifest_response.status_code == 200, manifest_response.text


def test_generation_operations_are_http_202_and_inspection_is_get(tts_api) -> None:
    client, _app, _settings, store, root, runner = tts_api
    _register_speaker(store, root)

    piper = _post(client, PIPER_PATH, _piper_body())
    index = _post(client, INDEX_PATH, _index_body())

    assert piper.status_code == 202, piper.text
    assert index.status_code == 202, index.text
    assert len(runner.submissions) == 2


def _walk_mappings(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _engine_info(payload: dict[str, Any], name: str) -> dict[str, Any]:
    for item in _walk_mappings(payload):
        if isinstance(item.get(name), dict):
            return item[name]
        if item.get("engine") == name or item.get("name") == name:
            return item
    raise AssertionError(f"inspect_tts response has no {name!r} status: {payload!r}")


def test_inspect_tts_is_model_free_and_reports_runtime_status(tts_api, monkeypatch) -> None:
    client, app, *_ = tts_api

    def forbidden(*_args, **_kwargs):
        pytest.fail("inspect_tts attempted to start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    original_import = builtins.__import__

    def no_model_import(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch.") or name == "onnxruntime" or name.startswith("onnxruntime."):
            pytest.fail(f"inspect_tts imported model runtime {name!r}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_model_import)
    route = next(route for route in app.routes if getattr(route, "name", None) == "inspect_tts")
    assert "GET" in route.methods
    response = client.get("/models/tts")
    assert response.status_code == 200, response.text
    payload = response.json()
    piper = _engine_info(payload, "piper")
    index = _engine_info(payload, "indextts2")
    assert isinstance(piper.get("enabled"), bool)
    assert isinstance(piper.get("runtime_present"), bool)
    assert isinstance(piper.get("voices"), (list, dict))
    assert isinstance(index.get("enabled"), bool)
    assert isinstance(index.get("runtime_present"), bool)
    assert isinstance(index.get("checkpoints_present"), bool)
    assert isinstance(payload.get("limitations"), list)
    assert any(
        voice.get("voice_id") == "en_US-ryan-high" and isinstance(voice.get("present"), bool)
        for voice in _walk_mappings(piper.get("voices"))
    )


@pytest.mark.parametrize(
    "body",
    [
        {"expected_version": 1, "voice_id": "en_US-ryan-high"},
        {"expected_version": 1, "text": "x", "text_file": "script.txt",
         "voice_id": "en_US-ryan-high"},
    ],
)
def test_piper_requires_exactly_one_text_source_without_side_effects(tts_api, body) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    before = _snapshot(root)
    response = _post(client, PIPER_PATH, body)

    assert response.status_code == 422, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_piper_rejects_blank_text_without_side_effects(tts_api) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    before = _snapshot(root)
    response = _post(client, PIPER_PATH, _piper_body(text=" \t\n"))

    assert response.status_code == 422, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_utf8_text_limit_accepts_exact_multibyte_boundary_and_rejects_overage(tts_api) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    boundary = "🙂" * 2048
    accepted = _post(client, PIPER_PATH, _piper_body(text=boundary))
    assert len(boundary.encode("utf-8")) == 8192
    assert accepted.status_code == 202, accepted.text
    assert len(runner.submissions) == 1

    before = _snapshot(root)
    rejected = _post(client, PIPER_PATH, _piper_body(text="🙂" * 2049))
    assert rejected.status_code == 422, rejected.text
    assert _snapshot(root) == before
    assert len(runner.submissions) == 1


def test_text_file_path_cannot_escape_project_root(tts_api) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    before = _snapshot(root)
    response = _post(client, PIPER_PATH, _piper_body(text=None, text_file="../outside.txt"))

    assert response.status_code in {400, 422}, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_stale_expected_version_is_409_without_job_or_files(tts_api) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    before = _snapshot(root)
    response = _post(client, PIPER_PATH, _piper_body(expected_version=99))

    assert response.status_code == 409, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_generation_request_rejects_unknown_fields_before_submission(tts_api) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    before = _snapshot(root)
    response = _post(client, PIPER_PATH, _piper_body(unrecognized="forbidden"))

    assert response.status_code == 422, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


@pytest.mark.parametrize(
    "section,path,body",
    [
        ("piper", PIPER_PATH, _piper_body()),
        ("indextts2", INDEX_PATH, _index_body()),
    ],
)
def test_disabled_engine_refusal_names_its_settings_section(tts_api, section, path, body) -> None:
    client, _app, settings, _store, root, runner = tts_api
    getattr(settings, section).enabled = False
    before = _snapshot(root)
    response = _post(client, path, body)

    assert response.status_code == 503, response.text
    assert response.json().get("error") == "tts_setup_unavailable", response.text
    assert section in response.text.lower(), response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_invalid_speech_request_uses_tts_validation_error_envelope(tts_api) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    before = _snapshot(root)

    # A semantic refusal (the request parses, but names a text_file that does not exist).
    # Schema failures, such as blank text rejected by the body model, use the shared
    # request-validation envelope like every other operation (contract 5.4 amendment).
    body = _piper_body(text_file="prompts/missing.txt")
    del body["text"]
    response = _post(client, PIPER_PATH, body)

    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload.get("error") == "validation_error", payload
    assert payload.get("details", {}).get("field") == "speech_request", payload
    assert _snapshot(root) == before
    assert runner.submissions == []


@pytest.mark.parametrize("voice_id", ["unlisted-voice", "en_US-lessac-medium"])
def test_piper_refuses_unlisted_voices_before_job_creation(tts_api, voice_id: str) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    before = _snapshot(root)
    response = _post(client, PIPER_PATH, _piper_body(voice_id=voice_id))

    assert response.status_code == 422, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


@pytest.mark.parametrize("field,value", [
    ("length_scale", 0.5), ("length_scale", 2.0),
    ("sentence_silence_seconds", 0.0), ("sentence_silence_seconds", 2.0),
])
def test_piper_numeric_bounds_are_inclusive(tts_api, field: str, value: float) -> None:
    client, _app, _settings, _store, _root, runner = tts_api
    response = _post(client, PIPER_PATH, _piper_body(**{field: value}))

    assert response.status_code == 202, response.text
    assert len(runner.submissions) == 1


@pytest.mark.parametrize("field,value", [
    ("length_scale", 0.49), ("length_scale", 2.01),
    ("sentence_silence_seconds", -0.01), ("sentence_silence_seconds", 2.01),
])
def test_piper_rejects_values_outside_numeric_bounds_without_side_effects(
    tts_api, field: str, value: float,
) -> None:
    client, _app, _settings, _store, root, runner = tts_api
    before = _snapshot(root)
    response = _post(client, PIPER_PATH, _piper_body(**{field: value}))

    assert response.status_code == 422, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


@pytest.mark.parametrize(
    "emotion",
    [
        {"mode": "vector", "happy": 0.0, "angry": 0.0, "sad": 0.0,
         "afraid": 0.0, "disgusted": 0.0, "melancholic": 0.0, "calm": 0.0},
        {"mode": "vector", "happy": -0.01, "angry": 0.0, "sad": 0.0,
         "afraid": 0.0, "disgusted": 0.0, "melancholic": 0.0,
         "surprised": 1.0, "calm": 0.0},
        {"mode": "vector", "happy": 1.01, "angry": 0.0, "sad": 0.0,
         "afraid": 0.0, "disgusted": 0.0, "melancholic": 0.0,
         "surprised": 0.0, "calm": 0.0},
        {"mode": "text", "description": "  \n"},
        {"mode": "text", "description": "x" * 501},
    ],
)
def test_indextts2_rejects_invalid_emotions_without_job_or_files(tts_api, emotion) -> None:
    client, _app, _settings, store, root, runner = tts_api
    _register_speaker(store, root)
    before = _snapshot(root)
    response = _post(client, INDEX_PATH, _index_body(emotion=emotion))

    assert response.status_code == 422, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_indextts2_vector_requires_all_eight_keys(tts_api) -> None:
    client, _app, _settings, store, root, runner = tts_api
    _register_speaker(store, root)
    before = _snapshot(root)
    vector = {
        "mode": "vector", "happy": 1.0, "angry": 0.0, "sad": 0.0,
        "afraid": 0.0, "disgusted": 0.0, "melancholic": 0.0, "calm": 0.0,
    }
    response = _post(client, INDEX_PATH, _index_body(emotion=vector))

    assert response.status_code == 422, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_indextts2_rejects_nonfinite_emotion_values(tts_api) -> None:
    client, _app, _settings, store, root, runner = tts_api
    _register_speaker(store, root)
    before = _snapshot(root)
    vector = {
        "mode": "vector", "happy": float("nan"), "angry": 0.0, "sad": 0.0,
        "afraid": 0.0, "disgusted": 0.0, "melancholic": 0.0,
        "surprised": 0.0, "calm": 0.0,
    }
    response = client.post(
        INDEX_PATH,
        content=json.dumps(_index_body(emotion=vector), allow_nan=True),
        headers={TOKEN_HEADER: TOKEN, "content-type": "application/json"},
    )

    assert response.status_code == 422, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


@pytest.mark.parametrize(
    "emotion",
    [
        {"mode": "vector", "happy": 1.0, "angry": 0.0, "sad": 0.0,
         "afraid": 0.0, "disgusted": 0.0, "melancholic": 0.0,
         "surprised": 0.0, "calm": 0.0},
        {"mode": "text", "description": " restrained, deliberate delivery "},
        {"mode": "text", "description": "x" * 500},
    ],
)
def test_indextts2_accepts_valid_emotion_discriminators(tts_api, emotion) -> None:
    client, _app, _settings, store, root, runner = tts_api
    _register_speaker(store, root)
    response = _post(client, INDEX_PATH, _index_body(emotion=emotion))

    assert response.status_code == 202, response.text
    assert len(runner.submissions) == 1


@pytest.mark.parametrize(
    "asset_case",
    ["missing", "wrong_type", "not_wav", "checksum_mismatch", "missing_consent"],
)
def test_index_speaker_reference_requires_registered_wav_and_consent(tts_api, asset_case: str) -> None:
    client, _app, _settings, store, root, runner = tts_api
    asset_id = "speaker"
    if asset_case == "wrong_type":
        _register_speaker(store, root, asset_type="image")
    elif asset_case == "not_wav":
        _register_speaker(store, root, filename="source.mp3")
    elif asset_case == "checksum_mismatch":
        _register_speaker(store, root, checksum="0" * 64)
    elif asset_case == "missing_consent":
        _register_speaker(store, root, voice_consent=None)
    before = _snapshot(root)
    response = _post(client, INDEX_PATH, _index_body(speaker_asset_id=asset_id))

    assert response.status_code in {400, 404, 422}, response.text
    if asset_case == "missing_consent":
        assert "consent" in response.text.lower(), response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


@pytest.mark.parametrize(
    "consent,basis",
    [
        ("synthetic", None),
        ("creator_own_voice", "Creator recorded and approved this reference."),
        ("licensed_for_cloning", "License permits voice cloning; agreement on file."),
    ],
)
def test_index_accepts_each_supported_voice_consent(tts_api, consent: str, basis: str | None) -> None:
    client, _app, _settings, store, root, runner = tts_api
    _register_speaker(
        store, root, voice_consent=consent, voice_consent_basis=basis,
    )
    response = _post(client, INDEX_PATH, _index_body())

    assert response.status_code == 202, response.text
    assert len(runner.submissions) == 1


def test_tts_upload_accepts_riff_wave_voice_with_consent(tts_api) -> None:
    client, *_ = tts_api
    response = client.post(
        "/projects/p1/assets",
        headers={TOKEN_HEADER: TOKEN},
        files={"files": ("voice.wav", _wav_bytes(), "audio/wav")},
        data={
            "asset_id": "uploaded-voice",
            "type": "voice",
            "generated": "false",
            "source_url": "https://assets.example.invalid/creator-voice.wav",
            "license": "creator supplied reference",
            "voice_consent": "synthetic",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["type"] == "voice"
    assert response.json()["voice_consent"] == "synthetic"


def test_tts_upload_accepts_creator_own_voice_without_source_url(tts_api) -> None:
    client, _app, _settings, store, root, _runner = tts_api
    data = {
        "asset_id": "creator-voice-no-url",
        "type": "voice",
        "generated": "false",
        "license": "creator recording",
        "voice_consent": "creator_own_voice",
        "voice_consent_basis": "Recorded by the creator for this project on 2026-11-16.",
    }

    response = client.post(
        "/projects/p1/assets",
        headers={TOKEN_HEADER: TOKEN},
        files={"files": ("voice.wav", _wav_bytes(), "audio/wav")},
        data=data,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["type"] == "voice"
    assert payload["voice_consent"] == "creator_own_voice"
    assert payload["voice_consent_basis"] == data["voice_consent_basis"]
    assert payload.get("source_url") is None
    registered = store.load(Asset, "creator-voice-no-url")
    assert registered.voice_consent == "creator_own_voice"
    assert registered.source_url is None
    assert root.resolve(registered.file_path).is_file()


def test_tts_upload_requires_source_url_for_licensed_cloning_consent(tts_api) -> None:
    client, _app, _settings, _store, root, _runner = tts_api
    before = _snapshot(root)
    response = client.post(
        "/projects/p1/assets",
        headers={TOKEN_HEADER: TOKEN},
        files={"files": ("licensed-voice.wav", _wav_bytes(), "audio/wav")},
        data={
            "asset_id": "licensed-voice-no-url",
            "type": "voice",
            "generated": "false",
            "license": "voice cloning permission",
            "voice_consent": "licensed_for_cloning",
            "voice_consent_basis": "The licence permits cloning for this project.",
        },
    )

    assert response.status_code in {400, 422}, response.text
    assert _snapshot(root) == before


def test_tts_upload_refuses_voice_without_consent_and_writes_nothing(tts_api) -> None:
    client, _app, _settings, _store, root, _runner = tts_api
    before = _snapshot(root)
    response = client.post(
        "/projects/p1/assets",
        headers={TOKEN_HEADER: TOKEN},
        files={"files": ("voice.wav", _wav_bytes(), "audio/wav")},
        data={"asset_id": "no-consent", "type": "voice",
              "source_url": "https://assets.example.invalid/creator-voice.wav",
              "license": "creator supplied reference"},
    )

    assert response.status_code in {400, 422}, response.text
    assert "consent" in response.text.lower(), response.text
    assert _snapshot(root) == before


def test_tts_upload_rejects_wav_extension_without_riff_wave_magic(tts_api) -> None:
    client, _app, _settings, _store, root, _runner = tts_api
    before = _snapshot(root)
    bad_header = b"NOPE\x00\x00\x00\x00WAVE" + b"not a wave file"
    response = client.post(
        "/projects/p1/assets",
        headers={TOKEN_HEADER: TOKEN},
        files={"files": ("not-really.wav", bad_header, "audio/wav")},
        data={"asset_id": "bad-wave", "type": "voice",
              "source_url": "https://assets.example.invalid/bad-wave.wav", "license": "fixture",
              "voice_consent": "synthetic"},
    )

    assert response.status_code in {400, 422}, response.text
    assert _snapshot(root) == before

