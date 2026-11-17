"""AudioPost JobRunner integration using a fake FFmpeg process boundary."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import subprocess
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.domain.entities import Asset, Episode, GenerationRun, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER
from tests.service.conftest import Environment


CREATE_PATH = "/projects/p1/audio-post-profiles/{name}"
APPLY_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/post"
FFMPEG_IDENTITY = (
    "ffmpeg version audio-test 1.0\n"
    "configuration: --enable-gpl --enable-libfreetype\n"
    "libavutil 60.1.0 / 60.1.0\n"
)
LOUDNORM_JSON = {
    "input_i": "-21.00",
    "input_tp": "-2.00",
    "input_lra": "4.00",
    "input_thresh": "-31.00",
    "output_i": "-18.00",
    "output_tp": "-1.50",
    "output_lra": "4.00",
    "output_thresh": "-28.00",
    "normalization_type": "linear",
    "target_offset": "0.00",
}


def _wav_bytes(*, sample_rate: int = 22050, frames: int = 2205) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * frames)
    return stream.getvalue()


class FakeFFmpeg:
    def __init__(self, *, loudness: float = -18.0) -> None:
        self.identity = FFMPEG_IDENTITY
        self.loudness = loudness
        self.loudnorm_measurements = dict(LOUDNORM_JSON)
        self.ebur128_output: str | None = None
        self.calls: list[dict[str, Any]] = []

    def popen(self, argv, **kwargs):
        process = FakeAudioProcess(argv, fake=self, **kwargs)
        self.calls.append({"argv": process.argv, "kwargs": kwargs, "kind": process.kind})
        return process


class FakeAudioProcess:
    def __init__(self, argv, *, fake: FakeFFmpeg, **kwargs) -> None:
        self.args = list(argv)
        self.argv = [str(value) for value in argv]
        self.kwargs = kwargs
        self.fake = fake
        joined = " ".join(self.argv)
        if "-version" in self.argv:
            self.kind = "version"
        elif "print_format=json" in joined:
            self.kind = "pass1"
        elif "measured_I=" in joined and "linear=true" in joined:
            self.kind = "pass2"
        elif "ebur128" in joined:
            self.kind = "ebur128"
        elif "-show_streams" in self.argv or "-show_format" in self.argv:
            self.kind = "ffprobe"
        else:
            raise AssertionError(f"unexpected FFmpeg invocation: {self.argv!r}")
        self.returncode = 0
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self._stdout_bytes = b""
        self._stderr_bytes = b""
        if self.kind == "version":
            self._stdout_bytes = fake.identity.encode("utf-8")
        elif self.kind == "pass1":
            self._stderr_bytes = json.dumps(fake.loudnorm_measurements).encode("utf-8")
        elif self.kind == "pass2":
            self._write_encoded_output()
            self._stderr_bytes = b"fake encode completed\n"
        elif self.kind == "ebur128":
            default_output = (
                "[Parsed_ebur128] Summary:\n"
                "  Integrated loudness:\n"
                f"    I:         {fake.loudness:.1f} LUFS\n"
                "    Threshold: -28.0 LUFS\n"
            )
            self._stderr_bytes = (fake.ebur128_output or default_output).encode("utf-8")
        elif self.kind == "ffprobe":
            self._stdout_bytes = json.dumps({
                "streams": [{
                    "codec_type": "audio", "codec_name": "pcm_s16le",
                    "sample_rate": "22050", "channels": 1, "duration": "0.100000",
                }],
                "format": {"duration": "0.100000"},
            }).encode("utf-8")

    def _write_encoded_output(self) -> None:
        output_arg = self.argv[-1]
        output = Path(output_arg)
        if not output.is_absolute():
            output = Path(self.kwargs.get("cwd", Path.cwd())) / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_wav_bytes())

    @staticmethod
    def _write_log(target: Any, data: bytes) -> None:
        if not hasattr(target, "write"):
            return
        try:
            target.write(data)
        except TypeError:
            target.write(data.decode("utf-8", errors="replace"))
        if hasattr(target, "flush"):
            target.flush()

    def communicate(self, input=None, timeout=None):
        stdout = self._stdout_bytes
        stderr = self._stderr_bytes
        if hasattr(self.kwargs.get("stdout"), "write"):
            self._write_log(self.kwargs["stdout"], stdout or stderr)
            stdout, stderr = b"", b""
        if hasattr(self.kwargs.get("stderr"), "write"):
            self._write_log(self.kwargs["stderr"], stderr)
            stderr = b""
        return stdout, stderr

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


@pytest.fixture(scope="module")
def audio_post_adapter_type():
    """One isolated module/class assumption: adapter name is not specified in the contract."""
    module = importlib.import_module("orchestrator.adapters.audiopost.runner")
    return module.AudioPostAdapter


@pytest.fixture
def audio_post_service(tmp_path: Path, audio_post_adapter_type):
    projects = tmp_path / "projects"
    project_path = projects / "p1"
    project_path.mkdir(parents=True)
    env = Environment(project_path)
    env.store.save(Project(project_id="p1", title="AudioPost service contract"), expected_version=0)
    env.store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30), expected_version=0)
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=10)
    env.store.create(shot)
    fake = FakeFFmpeg()
    adapter = audio_post_adapter_type(env.root, env.settings, popen=fake.popen)
    lease_states: list[bool] = []

    class ObservedAdapter:
        def invoke(self, request, job, *, queue):
            lease_states.append(bool(getattr(getattr(queue, "lease", None), "_owned", False)))
            return adapter.invoke(request, job, queue=queue)

        def close(self):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    env.registry.adapter = ObservedAdapter()
    settings = Settings.model_validate({"project_root": projects})
    from orchestrator.api.app import create_app

    app = create_app(settings=settings, token=TOKEN, job_runner=env.runner)
    root, store = env.root, env.store
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield client, app, store, root, shot, fake, env, lease_states
    env.runner.close()


def _profile_body(*, name: str = "audio-room", noise: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "audio_post_profile": {
            "name": name,
            "semver": "1.2.3",
            "intent": "A worn analog security intercom.",
            "voice_stages": [
                {"name": "midrange", "filter": "volume", "params": {"gain_db": 3.0}},
            ],
            "noise": {"color": "pink", "amplitude": 0.02} if noise is None else noise,
            "hum": None,
            "encode": {"codec": "pcm_s16le", "sample_rate": 22050},
        },
    }


def _save_profile(store: ProjectStore, name: str = "audio-room") -> None:
    profile_type = getattr(importlib.import_module("orchestrator.domain.entities"), "AudioPostProfile")
    profile = profile_type.model_validate(_profile_body(name=name)["audio_post_profile"] | {
        "pinned_binaries": {"ffmpeg": normalize_ffmpeg_identity(FFMPEG_IDENTITY)},
    })
    store.save(profile, expected_version=0)


def _register_source(store: ProjectStore, root: ProjectRoot) -> Asset:
    data = _wav_bytes()
    relative = "assets/audio/source/take.wav"
    root.write_bytes(relative, data)
    source = Asset.model_validate({
        "asset_id": "source",
        "type": "voice",
        "generated": False,
        "license": "creator supplied voice recording",
        "model_or_voice_license": "voice recording permission",
        "file_path": relative,
        "checksum": hashlib.sha256(data).hexdigest(),
        "voice_consent": "creator_own_voice",
        "voice_consent_basis": "The creator recorded and consented to this take.",
        "reuse_scope": "internal evaluation only",
    })
    store.save(source, expected_version=0)
    return source


def _changed_source_record(source: Asset, change: str) -> Asset:
    if change == "licensed-consent":
        return source.model_copy(update={"voice_consent": "licensed_recording"})
    if change == "removed-consent":
        fields = source.model_dump(mode="python")
        fields.pop("voice_consent", None)
        fields.pop("voice_consent_basis", None)
        return Asset.model_validate(fields)
    if change == "checksum":
        return source.model_copy(update={"checksum": "f" * 64})
    if change == "file-path":
        return source.model_copy(update={"file_path": "assets/audio/source/changed.wav"})
    if change == "type":
        return source.model_copy(update={"type": "image"})
    raise AssertionError(f"unknown source mutation: {change}")


def _submit(client: TestClient, *, profile_name: str = "audio-room", expected_version: int = 1):
    return client.post(
        APPLY_PATH,
        json={
            "expected_version": expected_version,
            "take_asset_id": "source",
            "profile_name": profile_name,
        },
        headers={TOKEN_HEADER: TOKEN},
    )


def _walk_key_values(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value
            yield from _walk_key_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_key_values(value)


def _has_key_value(node: Any, key: str, expected: Any) -> bool:
    return any(candidate.lower() == key.lower() and value == expected for candidate, value in _walk_key_values(node))


def _has_semantic_key_value(node: Any, fragments: tuple[str, ...], expected: Any) -> bool:
    return any(
        all(fragment in key.lower() for fragment in fragments) and value == expected
        for key, value in _walk_key_values(node)
    )


def _values(node: Any):
    if isinstance(node, dict):
        for value in node.values():
            yield from _values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _values(value)
    else:
        yield node


def _pass_calls(fake: FakeFFmpeg, kind: str) -> list[dict[str, Any]]:
    return [call for call in fake.calls if call["kind"] == kind]


def test_fake_ffmpeg_two_pass_success_registers_sidecar_and_rights(audio_post_service) -> None:
    client, app, store, root, original_shot, fake, env, lease_states = audio_post_service
    source = _register_source(store, root)
    _save_profile(store)
    shot_before, shot_token_before = store.load_with_version(Shot, "s1", episode_id="ep1")
    outputs_before = store.list_shot_outputs("s1", episode_id="ep1")

    submitted = _submit(client)
    assert submitted.status_code == 202, submitted.text
    runner = app.state.context.job_runner
    job = runner.wait("p1", submitted.json()["job_id"])

    assert job.state == "completed", f"job={job.model_dump(mode='json')!r}; fake calls={fake.calls!r}"
    adapter_kind = getattr(importlib.import_module("orchestrator.service.runner"), "AdapterKind")
    assert job.type == adapter_kind.AUDIO_POST.value
    assert lease_states == [False]
    pass1_calls = _pass_calls(fake, "pass1")
    pass2_calls = _pass_calls(fake, "pass2")
    assert len(pass1_calls) == len(pass2_calls) == 1
    pass1_argv = pass1_calls[0]["argv"]
    pass2_argv = pass2_calls[0]["argv"]
    pass1_graph = pass1_argv[pass1_argv.index("-filter_complex") + 1]
    pass2_graph = pass2_argv[pass2_argv.index("-filter_complex") + 1]
    assert "loudnorm=I=-18:TP=-1:LRA=11:print_format=json" in pass1_graph
    assert all(token in pass2_graph for token in (
        "measured_I=", "measured_TP=", "measured_LRA=", "measured_thresh=", "offset=", "linear=true",
    ))
    for argv in (pass1_argv, pass2_argv):
        assert argv[argv.index("-threads") + 1] == "1"
        assert "-hide_banner" in argv and "-nostdin" in argv and "-y" in argv
        assert "+bitexact" in argv
        assert "-map_metadata" in argv and argv[argv.index("-map_metadata") + 1] == "-1"
        assert any(call["kwargs"].get("shell", False) is False for call in fake.calls if call["argv"] == argv)

    seeds = importlib.import_module("orchestrator.domain.seeds")
    expected_seed = seeds.derive_audio_seed("audio-room", "1.2.3", "noise", "source")
    assert f"seed={expected_seed}" in pass2_graph

    run_id = job.run_id
    assert run_id
    run_dir = f"generated/audio_post/s1/v0001/{run_id}"
    take_path = f"{run_dir}/take.wav"
    report_path = f"{run_dir}/report.json"
    log_path = f"{run_dir}/logs/ffmpeg.log"
    assert root.resolve(take_path).is_file()
    assert root.resolve(report_path).is_file()
    assert root.resolve(log_path).is_file()
    assert "pass" in root.read_text(log_path, encoding="utf-8").lower()

    report = json.loads(root.read_text(report_path, encoding="utf-8"))
    graph = next(value for value in _values(report) if isinstance(value, str) and "linear=true" in value)
    graph_hash = hashlib.sha256(graph.encode("utf-8")).hexdigest()
    assert report["filter_graph_hash"] == graph_hash
    assert graph == pass2_graph
    assert _has_key_value(report, "semver", "1.2.3")
    assert _has_key_value(report, "name", "audio-room")
    assert _has_semantic_key_value(report, ("noise", "seed"), expected_seed)
    assert _has_semantic_key_value(report, ("ffmpeg", "identity"), normalize_ffmpeg_identity(FFMPEG_IDENTITY))
    assert any(value == source.asset_id for value in _values(report))
    assert any(value == source.checksum for value in _values(report))
    stage_lists = [
        value for _key, value in _walk_key_values(report)
        if isinstance(value, list) and value and all(isinstance(stage, dict) and "filter" in stage for stage in value)
    ]
    assert stage_lists
    assert any(
        [(stage["filter"], stage["params"]) for stage in values]
        == [("volume", {"gain_db": 3.0})]
        for values in stage_lists
    )
    output_bytes = root.read_bytes(take_path)
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    assert any(value == output_sha256 for value in _values(report))
    assert _has_key_value(report, "sample_rate", 22050)
    assert _has_key_value(report, "codec", "pcm_s16le")
    durations = [value for key, value in _walk_key_values(report) if "duration" in key.lower()]
    assert durations and float(durations[0]) > 0, report
    loudness_values = [
        value for key, value in _walk_key_values(report)
        if ("loudness" in key.lower() or "lufs" in key.lower())
        and isinstance(value, (float, int, str))
    ]
    assert loudness_values and any(abs(float(value) - (-18.0)) <= 0.5 for value in loudness_values)
    for measurement in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        values = [value for key, value in _walk_key_values(report) if key == measurement]
        assert values and float(values[0]) == float(LOUDNORM_JSON[measurement])

    output_asset = store.load(Asset, f"audiopost-{run_id}")
    assert output_asset.type == "voice" and output_asset.generated is True
    assert output_asset.provider == "audio_post_profile:audio-room@1.2.3"
    assert output_asset.file_path == take_path
    assert output_asset.checksum == output_sha256
    for field in (
        "license", "model_or_voice_license", "voice_consent", "voice_consent_basis", "reuse_scope",
    ):
        assert getattr(output_asset, field) == getattr(source, field)

    generation = store.load(GenerationRun, run_id)
    assert source.file_path in generation.input_paths
    assert generation.tool == "ffmpeg"
    shot_after, shot_token_after = store.load_with_version(Shot, "s1", episode_id="ep1")
    assert shot_after == shot_before == original_shot
    assert shot_token_after == shot_token_before
    assert store.list_shot_outputs("s1", episode_id="ep1") == outputs_before


def test_audio_post_profiles_cannot_be_overwritten_with_the_current_version_token(
    audio_post_service,
) -> None:
    _client, _app, store, _root, _shot, _fake, _env, _lease_states = audio_post_service
    _save_profile(store)
    profile_type = getattr(importlib.import_module("orchestrator.domain.entities"), "AudioPostProfile")
    original, token = store.load_with_version(profile_type, "audio-room")
    changed = original.model_copy(update={"semver": "1.2.4"})

    with pytest.raises(JobError):
        store.save(changed, expected_version=token)

    assert store.load_with_version(profile_type, "audio-room") == (original, token)


@pytest.mark.parametrize(
    "change",
    ["licensed-consent", "removed-consent", "checksum", "file-path", "type"],
)
def test_source_registry_change_after_submission_fails_before_audio_output(
    audio_post_service, change,
) -> None:
    client, app, store, root, _shot, fake, env, _lease_states = audio_post_service
    _register_source(store, root)
    _save_profile(store)
    original_adapter = env.registry.adapter
    mutated: list[Asset] = []

    class MutatingAdapter:
        def invoke(self, request, job, *, queue):
            source, token = store.load_with_version(Asset, "source")
            changed = _changed_source_record(source, change)
            store.save(changed, expected_version=token)
            mutated.append(changed)
            return original_adapter.invoke(request, job, queue=queue)

        def close(self):
            original_adapter.close()

    env.registry.adapter = MutatingAdapter()
    submitted = _submit(client)
    assert submitted.status_code == 202, submitted.text
    job = app.state.context.job_runner.wait("p1", submitted.json()["job_id"])

    assert mutated, "the registry mutation must happen after submission and before adapter execution"
    assert job.state == "failed", f"job={job.model_dump(mode='json')!r}"
    from orchestrator.platform.errors import JobErrorKind
    assert job.error_kind == JobErrorKind.INVALID_MANIFEST.value
    assert fake.calls == []
    run_dir = root.path / "generated" / "audio_post" / "s1" / "v0001" / str(job.run_id)
    assert not run_dir.exists()
    with pytest.raises(RecordNotFound):
        store.load(Asset, f"audiopost-{job.run_id}")


@pytest.mark.parametrize(
    "summary",
    [
        "    I:         nan LUFS\n    Threshold: -28.0 LUFS\n",
        "    I:         not-a-number LUFS\n    Threshold: -28.0 LUFS\n",
        "    Threshold: -28.0 LUFS\n",
        "    I:         -18.0 LUFS\n    I:         -18.1 LUFS\n    Threshold: -28.0 LUFS\n",
    ],
    ids=["non-finite", "unparseable", "zero-readings", "two-readings"],
)
def test_ebur128_requires_exactly_one_finite_reading_in_the_final_summary(
    audio_post_service, summary,
) -> None:
    client, app, store, root, _shot, fake, _env, _lease_states = audio_post_service
    _register_source(store, root)
    _save_profile(store)
    fake.ebur128_output = (
        "[Parsed_ebur128] I: -18.0 LUFS\n"
        "[Parsed_ebur128] Summary:\n"
        "  Integrated loudness:\n"
        + summary
    )

    submitted = _submit(client)
    assert submitted.status_code == 202, submitted.text
    job = app.state.context.job_runner.wait("p1", submitted.json()["job_id"])

    assert job.state == "failed", f"job={job.model_dump(mode='json')!r}; fake calls={fake.calls!r}"
    assert len(_pass_calls(fake, "pass1")) == 1
    assert len(_pass_calls(fake, "pass2")) == 1
    assert len(_pass_calls(fake, "ebur128")) == 1
    run_dir = root.path / "generated" / "audio_post" / "s1" / "v0001" / str(job.run_id)
    assert not (run_dir / "take.wav").exists()
    with pytest.raises(RecordNotFound):
        store.load(Asset, f"audiopost-{job.run_id}")


@pytest.mark.parametrize(
    "field",
    ["input_i", "input_tp", "input_lra", "input_thresh", "target_offset"],
)
def test_loudnorm_boolean_measurements_are_rejected(audio_post_service, field) -> None:
    client, app, store, root, _shot, fake, _env, _lease_states = audio_post_service
    _register_source(store, root)
    _save_profile(store)
    fake.loudnorm_measurements[field] = True

    submitted = _submit(client)
    assert submitted.status_code == 202, submitted.text
    job = app.state.context.job_runner.wait("p1", submitted.json()["job_id"])

    assert job.state == "failed", f"job={job.model_dump(mode='json')!r}"
    assert len(_pass_calls(fake, "pass1")) == 1
    assert not _pass_calls(fake, "pass2")
    run_dir = root.path / "generated" / "audio_post" / "s1" / "v0001" / str(job.run_id)
    assert not (run_dir / "take.wav").exists()
    with pytest.raises(RecordNotFound):
        store.load(Asset, f"audiopost-{job.run_id}")


def test_cancelled_before_dispatch_has_no_generation_run(
    audio_post_service, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app, store, root, _shot, fake, env, _lease_states = audio_post_service
    _register_source(store, root)
    _save_profile(store)
    original_submit = env.runner.submit
    cancellations = []

    def submit_then_cancel(project_id, request):
        queued = original_submit(project_id, request)
        cancellations.append(env.runner.cancel(project_id, queued.job_id))
        return queued

    monkeypatch.setattr(env.runner, "submit", submit_then_cancel)
    submitted = _submit(client)
    assert submitted.status_code == 202, submitted.text
    env.runner.drain()
    job = app.state.context.job_runner.wait("p1", submitted.json()["job_id"])

    assert cancellations and cancellations[0].state == "cancelled"
    assert job.state == "cancelled"
    assert fake.calls == []
    assert job.run_id
    with pytest.raises(RecordNotFound):
        store.load(GenerationRun, job.run_id)
    with pytest.raises(RecordNotFound):
        store.load(Asset, f"audiopost-{job.run_id}")
    audio_root = root.path / "generated" / "audio_post" / "s1" / "v0001"
    assert not audio_root.exists()


def test_ffmpeg_identity_mismatch_fails_before_creating_output(audio_post_service) -> None:
    client, app, store, root, _shot, fake, _env, _lease_states = audio_post_service
    _register_source(store, root)
    _save_profile(store)
    fake.identity = "ffmpeg version different-build\nconfiguration: --disable-gpl\n"

    submitted = _submit(client)
    assert submitted.status_code == 202, submitted.text
    job = app.state.context.job_runner.wait("p1", submitted.json()["job_id"])

    assert job.state == "failed", f"job={job.model_dump(mode='json')!r}; fake calls={fake.calls!r}"
    from orchestrator.platform.errors import JobErrorKind
    assert job.error_kind == JobErrorKind.INVALID_MANIFEST.value
    assert not _pass_calls(fake, "pass1")
    assert not _pass_calls(fake, "pass2")
    assert not (root.path / "generated" / "audio_post" / "s1" / "v0001" / job.run_id / "take.wav").exists()
    with pytest.raises(RecordNotFound):
        store.load(Asset, f"audiopost-{job.run_id}")


def test_output_loudness_outside_half_lu_fails_and_discards_take_and_asset(audio_post_service) -> None:
    client, app, store, root, _shot, fake, _env, _lease_states = audio_post_service
    _register_source(store, root)
    _save_profile(store)
    fake.loudness = -16.0

    submitted = _submit(client)
    assert submitted.status_code == 202, submitted.text
    job = app.state.context.job_runner.wait("p1", submitted.json()["job_id"])

    assert job.state == "failed", f"job={job.model_dump(mode='json')!r}; fake calls={fake.calls!r}"
    from orchestrator.platform.errors import JobErrorKind
    assert job.error_kind == JobErrorKind.CORRUPT_ARTIFACT.value
    assert len(_pass_calls(fake, "pass1")) == 1
    assert len(_pass_calls(fake, "pass2")) == 1
    run_dir = root.path / "generated" / "audio_post" / "s1" / "v0001" / job.run_id
    assert not (run_dir / "take.wav").exists()
    with pytest.raises(RecordNotFound):
        store.load(Asset, f"audiopost-{job.run_id}")


def test_real_ffmpeg_applies_the_preset_twice_deterministically_at_target_loudness(
    tmp_path: Path, audio_post_adapter_type,
) -> None:
    """Required FR-32/AT-24 integration: real FFmpeg, synthetic WAV, two applications."""
    import math
    import re
    import shutil
    import struct
    import wave

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not on PATH")

    projects = tmp_path / "projects"
    project_path = projects / "p1"
    env = Environment(project_path)
    env.store.save(Project(project_id="p1", title="Real AudioPost determinism"), expected_version=0)
    env.store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30), expected_version=0)
    env.store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))
    env.settings.binaries.ffmpeg_exe = Path(ffmpeg)

    sample_rate = 22050
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        frames = bytearray()
        for index in range(3 * sample_rate):
            tone = 0.045 * math.sin(2 * math.pi * 440 * index / sample_rate)
            noise = 0.003 * (((index * 1103515245 + 12345) & 0xFFFF) / 32767.5 - 1.0)
            frames.extend(struct.pack("<h", round(max(-1.0, min(1.0, tone + noise)) * 32767)))
        writer.writeframes(bytes(frames))
    source_bytes = stream.getvalue()
    source_path = "assets/audio/synthetic/tone-noise.wav"
    env.root.write_bytes(source_path, source_bytes)
    source = Asset.model_validate({
        "asset_id": "synthetic-take", "type": "voice", "generated": True,
        "license": "synthetic fixture", "file_path": source_path,
        "checksum": hashlib.sha256(source_bytes).hexdigest(), "voice_consent": "synthetic",
    })
    env.store.save(source, expected_version=0)

    identity_result = subprocess.run(
        [ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True, check=False,
    )
    assert identity_result.returncode == 0, identity_result.stderr
    identity = normalize_ffmpeg_identity(identity_result.stdout + identity_result.stderr)
    preset_path = Path(__file__).resolve().parents[2] / "orchestrator" / "adapters" / "audiopost" / "presets" / "cctv-overdriven.json"
    preset_body = json.loads(preset_path.read_text(encoding="utf-8"))["audio_post_profile"]
    profile_type = getattr(importlib.import_module("orchestrator.domain.entities"), "AudioPostProfile")
    profile = profile_type.model_validate(preset_body | {"pinned_binaries": {"ffmpeg": identity}})
    env.store.save(profile, expected_version=0)
    env.registry.adapter = audio_post_adapter_type(env.root, env.settings, popen=subprocess.Popen)

    api_settings = Settings.model_validate({
        "project_root": projects, "binaries": {"ffmpeg_exe": ffmpeg},
    })
    from orchestrator.api.app import create_app

    app = create_app(settings=api_settings, token=TOKEN, job_runner=env.runner)
    try:
        with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
            outputs: list[bytes] = []
            output_paths: list[Path] = []
            for _ in range(2):
                submitted = client.post(
                    APPLY_PATH,
                    json={
                        "expected_version": 1,
                        "take_asset_id": source.asset_id,
                        "profile_name": "cctv-overdriven",
                    },
                    headers={TOKEN_HEADER: TOKEN},
                )
                assert submitted.status_code == 202, submitted.text
                job = env.runner.wait("p1", submitted.json()["job_id"])
                assert job.state == "completed", job.model_dump(mode="json")
                take = env.store.load(Asset, f"audiopost-{job.run_id}")
                outputs.append(env.root.read_bytes(take.file_path))
                output_paths.append(project_path / take.file_path)

            assert outputs[0] == outputs[1]
            measured = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-nostdin", "-i", str(output_paths[-1]),
                    "-filter_complex", "ebur128=framelog=verbose", "-f", "null", "-",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert measured.returncode == 0, measured.stderr
            readings = re.findall(r"\bI:\s*(-?\d+(?:\.\d+)?)\s+LUFS", measured.stderr)
            assert readings, measured.stderr
            assert abs(float(readings[-1]) - (-18.0)) <= 0.5
    finally:
        env.runner.close()
