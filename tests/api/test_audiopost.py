"""HTTP validation and profile creation tests for AudioPostProfile."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.domain.entities import Asset, Episode, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER, operations


CREATE_PATH = "/projects/p1/audio-post-profiles/{name}"
APPLY_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/post"
FFMPEG_IDENTITY = (
    "ffmpeg version audio-test 1.0\n"
    "configuration: --enable-gpl --enable-libfreetype\n"
    "libavutil 60.1.0 / 60.1.0\n"
)
FFMPEG_VERSION_CALLS: list[list[str]] = []


class CaptureRunner:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, Any]] = []

    def submit(self, project_id: str, request: Any) -> SimpleNamespace:
        self.submissions.append((project_id, request))
        return SimpleNamespace(job_id="audio-post-job", run_id="audio-post-run", state="queued")


class VersionProcess:
    def __init__(self, argv, *, calls: list[list[str]], identity: str = FFMPEG_IDENTITY, **kwargs) -> None:
        self.args = list(argv)
        self.argv = [str(value) for value in argv]
        self.calls = calls
        self.calls.append(self.argv)
        self.identity = identity
        self.kwargs = kwargs
        self.returncode = 0
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def communicate(self, input=None, timeout=None):
        return self.identity.encode("utf-8"), b""

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    terminate = kill

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def audiopost_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects = tmp_path / "projects"
    project_path = projects / "p1"
    project_path.mkdir(parents=True)
    calls = FFMPEG_VERSION_CALLS
    calls.clear()
    original_run = subprocess.run
    original_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        normalized_argv = [str(value) for value in argv]
        if normalized_argv and Path(normalized_argv[0]).name.lower().startswith("ffmpeg") and "-version" in normalized_argv:
            return VersionProcess(argv, calls=calls, **kwargs)
        return original_popen(argv, **kwargs)

    def fake_run(argv, **kwargs):
        normalized_argv = [str(value) for value in argv]
        if normalized_argv and Path(normalized_argv[0]).name.lower().startswith("ffmpeg") and "-version" in normalized_argv:
            calls.append(normalized_argv)
            return SimpleNamespace(stdout=FFMPEG_IDENTITY, stderr="", returncode=0, args=normalized_argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)
    from orchestrator.api.app import create_app

    # A service may import a subprocess callable before create_app is imported. Replace
    # only aliases equal to the original stdlib callables, preserving unrelated processes.
    for loaded in tuple(__import__("sys").modules.values()):
        if loaded is None:
            continue
        try:
            attributes = vars(loaded)
        except TypeError:
            continue
        for attribute, value in tuple(attributes.items()):
            if value is original_run:
                monkeypatch.setattr(loaded, attribute, fake_run, raising=False)
            elif value is original_popen:
                monkeypatch.setattr(loaded, attribute, fake_popen, raising=False)

    settings = Settings.model_validate({"project_root": projects})
    runner = CaptureRunner()
    app = create_app(settings=settings, token=TOKEN, job_runner=runner)
    root = ProjectRoot(project_path)
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="AudioPost contract"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30), expected_version=0)
    store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield client, app, settings, store, root, runner, calls


def _profile_body(name: str = "unit-profile", **updates: Any) -> dict[str, Any]:
    profile = {
        "name": name,
        "semver": "1.0.0",
        "intent": "A narrow-band security intercom.",
        "voice_stages": [
            {"name": "voice", "filter": "volume", "params": {"gain_db": 0.0}},
        ],
        "noise": None,
        "hum": None,
        "encode": {"codec": "pcm_s16le", "sample_rate": 22050},
    }
    profile.update(updates)
    return {"audio_post_profile": profile}


def _create(client: TestClient, name: str, body: dict[str, Any]):
    return client.post(
        CREATE_PATH.format(name=name), json=body, headers={TOKEN_HEADER: TOKEN},
    )


def _apply(client: TestClient, body: dict[str, Any]):
    return client.post(APPLY_PATH, json=body, headers={TOKEN_HEADER: TOKEN})


def _wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(22050)
        writer.writeframes(b"\x00\x00" * 64)
    return stream.getvalue()


def _register_take(
    store: ProjectStore,
    root: ProjectRoot,
    *,
    asset_id: str = "take",
    asset_type: str = "voice",
    filename: str = "take.wav",
    file_bytes: bytes | None = None,
    checksum: str | None = None,
    voice_consent: str | None = "synthetic",
) -> Asset:
    data = _wav_bytes() if file_bytes is None else file_bytes
    relative = f"assets/audio/{asset_id}/{filename}"
    root.write_bytes(relative, data)
    values: dict[str, Any] = {
        "asset_id": asset_id,
        "type": asset_type,
        "generated": False,
        "license": "fixture license",
        "model_or_voice_license": "fixture model license",
        "file_path": relative,
        "checksum": checksum or hashlib.sha256(data).hexdigest(),
        "reuse_scope": "project",
    }
    if voice_consent is not None:
        values["voice_consent"] = voice_consent
        if voice_consent != "synthetic":
            values["voice_consent_basis"] = "The creator consented to this project use."
    asset = Asset.model_validate(values)
    store.save(asset, expected_version=0)
    return asset


def _snapshot(root: ProjectRoot) -> dict[str, bytes]:
    return {
        path.relative_to(root.path).as_posix(): path.read_bytes()
        for path in root.path.rglob("*")
        if path.is_file()
    }


def _create_profile(audiopost_api, name: str = "unit-profile") -> None:
    client, _app, _settings, _store, _root, _runner, _calls = audiopost_api
    response = _create(client, name, _profile_body(name))
    assert response.status_code in {200, 201}, response.text


def test_audio_post_routes_are_registered_at_the_contract_paths(audiopost_api) -> None:
    client, app, *_ = audiopost_api
    found = {op.name: op for op in operations(app)}

    assert found["create_audio_post_profile"].method == "POST"
    assert found["create_audio_post_profile"].path == "/projects/{project_id}/audio-post-profiles/{profile_name}"
    assert found["apply_audio_post_profile"].method == "POST"
    assert found["apply_audio_post_profile"].path == (
        "/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/post"
    )
    assert client.get("/openapi.json").status_code == 200


def test_create_profile_pins_the_normalized_identity_from_one_ffmpeg_version_call(audiopost_api) -> None:
    client, _app, _settings, store, _root, _runner, calls = audiopost_api
    response = _create(client, "unit-profile", _profile_body())

    assert response.status_code in {200, 201}, response.text
    assert len(calls) == 1
    assert calls[0][-2:] == ["-hide_banner", "-version"]
    profile_type = getattr(importlib.import_module("orchestrator.domain.entities"), "AudioPostProfile")
    stored = store.load(profile_type, "unit-profile")
    assert stored.pinned_binaries["ffmpeg"] == normalize_ffmpeg_identity(FFMPEG_IDENTITY)


def test_preset_is_accepted_as_a_create_body(audiopost_api) -> None:
    client, _app, _settings, _store, _root, _runner, _calls = audiopost_api
    preset = Path(__file__).resolve().parents[2] / "orchestrator" / "adapters" / "audiopost" / "presets" / "cctv-overdriven.json"

    response = _create(client, "cctv-overdriven", json.loads(preset.read_text(encoding="utf-8")))

    assert response.status_code in {200, 201}, response.text


def test_profile_path_name_must_equal_body_name_without_writing(audiopost_api) -> None:
    client, _app, _settings, _store, root, _runner, calls = audiopost_api
    before = _snapshot(root)

    response = _create(client, "path-name", _profile_body("body-name"))

    assert response.status_code in {400, 422}, response.text
    assert _snapshot(root) == before
    assert calls == []


def test_second_profile_create_conflicts_and_preserves_the_original_record(audiopost_api) -> None:
    client, _app, _settings, _store, root, _runner, calls = audiopost_api
    first = _create(client, "unit-profile", _profile_body())
    assert first.status_code in {200, 201}, first.text
    stored_path = root.path / "audio_post_profiles" / "unit-profile.json"
    original = stored_path.read_bytes()

    second = _create(client, "unit-profile", _profile_body(semver="9.9.9"))

    assert second.status_code == 409, second.text
    assert stored_path.read_bytes() == original


@pytest.mark.parametrize("pinned_binaries", [{}, {"ffmpeg": "creator-asserted"}])
def test_create_refuses_asserted_pinned_binaries_before_invoking_ffmpeg(
    audiopost_api, pinned_binaries,
) -> None:
    client, _app, _settings, _store, root, _runner, calls = audiopost_api
    body = _profile_body()
    body["audio_post_profile"]["pinned_binaries"] = pinned_binaries
    before = _snapshot(root)

    response = _create(client, "unit-profile", body)

    assert response.status_code == 422, response.text
    assert calls == []
    assert _snapshot(root) == before


def test_create_accepts_inclusive_acompressor_threshold_minimum(audiopost_api) -> None:
    client, _app, _settings, _store, _root, _runner, calls = audiopost_api
    body = _profile_body()
    body["audio_post_profile"]["voice_stages"] = [{
        "name": "compressor", "filter": "acompressor", "params": {
            "threshold": 0.000976563, "ratio": 1, "attack": 0.01,
            "release": 0.01, "makeup": 1,
        },
    }]

    response = _create(client, "unit-profile", body)

    assert response.status_code in {200, 201}, response.text
    assert len(calls) == 1


@pytest.mark.parametrize(
    "stage",
    [
        {"name": "voice", "filter": "mystery", "params": {}},
        {"name": "voice", "filter": "volume", "params": {"gain_db": 0, "extra": 1}},
        {"name": "voice", "filter": "highpass", "params": {"f": 20001}},
        {"name": "voice", "filter": "volume", "params": {"gain_db": True}},
        {"name": "voice", "filter": "volume", "params": {"gain_db": float("nan")}},
        # Just below FFmpeg's declared minimum; the bound itself (0.000976563) is accepted
        # (contract section 2, corrected 2026-11-17 to FFmpeg's inclusive range).
        {"name": "voice", "filter": "acompressor", "params": {
            "threshold": 0.00097, "ratio": 1, "attack": 0.01,
            "release": 0.01, "makeup": 1,
        }},
    ],
)
def test_create_revalidates_closed_filter_allowlist_and_typed_values(audiopost_api, stage) -> None:
    client, _app, _settings, _store, root, _runner, calls = audiopost_api
    body = _profile_body()
    body["audio_post_profile"]["voice_stages"] = [stage]
    before = _snapshot(root)
    response = client.post(
        CREATE_PATH.format(name="unit-profile"),
        content=json.dumps(body, allow_nan=True),
        headers={TOKEN_HEADER: TOKEN, "content-type": "application/json"},
    )

    assert response.status_code == 422, response.text
    assert calls == []
    assert _snapshot(root) == before


def test_create_rejects_duplicate_voice_stage_names_before_writing(audiopost_api) -> None:
    client, _app, _settings, _store, root, _runner, calls = audiopost_api
    body = _profile_body()
    body["audio_post_profile"]["voice_stages"] = [
        {"name": "same", "filter": "volume", "params": {"gain_db": 0}},
        {"name": "same", "filter": "highpass", "params": {"f": 350}},
    ]
    before = _snapshot(root)

    response = _create(client, "unit-profile", body)

    assert response.status_code == 422, response.text
    assert calls == []
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "registered,body_updates",
    [
        (False, {"take_asset_id": "missing"}),
        (True, {"take_asset_id": "wrong-type", "asset_type": "image"}),
        (True, {"take_asset_id": "not-wav", "filename": "take.mp3"}),
        (True, {"take_asset_id": "bad-checksum", "checksum": "0" * 64}),
        (True, {"take_asset_id": "licensed", "voice_consent": "licensed_recording"}),
        (True, {"take_asset_id": "no-consent", "voice_consent": None}),
    ],
)
def test_apply_rejects_invalid_source_takes_before_job_or_files(audiopost_api, registered, body_updates) -> None:
    client, _app, _settings, store, root, runner, _calls = audiopost_api
    _create_profile(audiopost_api)
    updates = dict(body_updates)
    take_id = updates.pop("take_asset_id")
    if registered:
        _register_take(store, root, asset_id=take_id, **updates)
    before = _snapshot(root)

    response = _apply(client, {
        "expected_version": 1,
        "take_asset_id": take_id,
        "profile_name": "unit-profile",
    })

    assert response.status_code == 422, response.text
    assert runner.submissions == []
    assert _snapshot(root) == before


def test_apply_requires_a_take_asset_id_before_job_creation(audiopost_api) -> None:
    client, _app, _settings, _store, root, runner, _calls = audiopost_api
    _create_profile(audiopost_api)
    before = _snapshot(root)

    response = _apply(client, {"expected_version": 1, "profile_name": "unit-profile"})

    assert response.status_code == 422, response.text
    assert runner.submissions == []
    assert _snapshot(root) == before


def test_apply_rejects_stale_expected_version_without_job_or_files(audiopost_api) -> None:
    client, _app, _settings, store, root, runner, _calls = audiopost_api
    _create_profile(audiopost_api)
    _register_take(store, root)
    before = _snapshot(root)

    response = _apply(client, {
        "expected_version": 99, "take_asset_id": "take", "profile_name": "unit-profile",
    })

    assert response.status_code == 409, response.text
    assert runner.submissions == []
    assert _snapshot(root) == before


def test_apply_rejects_an_unknown_profile_without_job_or_files(audiopost_api) -> None:
    client, _app, _settings, store, root, runner, _calls = audiopost_api
    _register_take(store, root)
    before = _snapshot(root)

    response = _apply(client, {
        "expected_version": 1, "take_asset_id": "take", "profile_name": "missing-profile",
    })

    assert response.status_code == 422, response.text
    assert runner.submissions == []
    assert _snapshot(root) == before
