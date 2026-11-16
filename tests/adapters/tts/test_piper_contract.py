"""Piper subprocess contract tests, driven through the public HTTP operation."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.domain.entities import Episode, GenerationRun, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER
from tests.service.conftest import Environment


VOICE_ID = "en_US-ryan-high"
PIPER_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/piper"
VOICE_RELATIVE = f"spikes/05_voice/_work/{VOICE_ID}.onnx"
CONFIG_RELATIVE = f"spikes/05_voice/_work/{VOICE_ID}.onnx.json"
PIPER_STDERR = b"fake Piper startup diagnostic\npiper final diagnostic line\n"
PIPER_LAST_STDERR_LINE = "piper final diagnostic line"


def _write_wav(path: Path, *, channels: int = 1, sample_rate: int = 22050, frames: int = 32) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * channels * frames)


class FakePopen:
    """Piper process double that writes only its requested temporary WAV."""

    def __init__(self, argv: Any, **kwargs: Any) -> None:
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = 2417
        self.returncode: int | None = None
        self.killed = False
        self.input_bytes: bytes | None = None
        self._timed_out = False
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO(PIPER_STDERR)
        self.timeout_once = False
        self.block_until_killed = False
        self.cancel_check = lambda: False
        self.entered_communicate = threading.Event()
        self.release_communicate = threading.Event()

    def communicate(self, input: bytes | None = None, timeout: float | None = None):
        self.input_bytes = input
        self.entered_communicate.set()
        if self.block_until_killed:
            while not self.release_communicate.wait(0.01):
                if self.cancel_check():
                    self.kill()
            assert self.killed, "fake Piper process was released without cancellation"
        if self.timeout_once and not self._timed_out:
            self._timed_out = True
            raise subprocess.TimeoutExpired(self.argv, timeout, stderr=PIPER_STDERR)
        self.returncode = -9 if self.killed else 0
        if "-f" in self.argv:
            _write_wav(Path(self.argv[self.argv.index("-f") + 1]))
        return b"", PIPER_STDERR

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.release_communicate.set()

    def terminate(self) -> None:
        self.kill()


class ProcessCallList(list[FakePopen]):
    """Fake-Popen capture seam with an event for deterministic cancellation tests."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def append(self, process: FakePopen) -> None:
        super().append(process)
        self.started.set()


@pytest.fixture
def piper_harness(tmp_path: Path):
    """Assumed public adapter name/constructor: PiperAdapter(root, settings, popen=...)."""
    module = pytest.importorskip("orchestrator.adapters.tts.piper")
    adapter_type = module.PiperAdapter
    repo = Path(__file__).resolve().parents[3]
    if not (repo / VOICE_RELATIVE).is_file() or not (repo / CONFIG_RELATIVE).is_file():
        pytest.skip("the pinned Piper voice files are not installed")

    parent = tmp_path / "projects"
    project_path = parent / "p1"
    project_path.mkdir(parents=True)
    env = Environment(project_path)
    env.settings.piper.enabled = True
    env.store.save(Project(project_id="p1", title="Piper adapter contract"), expected_version=0)
    env.store.save(Episode(
        episode_id="ep1", project_id="p1", number=1, duration_target_sec=30,
    ), expected_version=0)
    env.store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))

    calls: ProcessCallList = ProcessCallList()
    lease_states: list[bool] = []
    cancel_check = [lambda: False]

    def set_cancel_check(check) -> None:
        cancel_check[0] = check or (lambda: False)

    env.adapter.set_cancel_check = set_cancel_check

    def popen(argv, **kwargs):
        process = FakePopen(argv, **kwargs)
        process.timeout_once = bool(getattr(popen, "timeout_next", False))
        popen.timeout_next = False
        process.block_until_killed = bool(getattr(popen, "block_next", False))
        popen.block_next = False
        process.cancel_check = cancel_check[0]
        calls.append(process)
        return process

    popen.timeout_next = False
    popen.block_next = False

    backend = adapter_type(env.root, env.settings, popen=popen)
    original_invoke = env.adapter.invoke

    def invoke(request, job, *, queue):
        lease_states.append(bool(getattr(getattr(queue, "lease", None), "_owned", False)))
        # Production passes the runner's cancel check to the adapter (the app's runtime
        # adapter installs it); the harness must too, or cancel can never reach it.
        return backend.invoke(request, job, queue=queue, cancel=cancel_check[0])

    env.adapter.invoke = invoke
    api_settings = Settings.model_validate({"project_root": parent, "piper": {"enabled": True}})
    app = create_app(settings=api_settings, token=TOKEN, job_runner=env.runner)
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield (
            client, env, calls,
            lambda value: _set_timeout(value, popen, calls),
            lease_states,
            lambda value: _set_blocking(value, popen),
        )
    env.runner.close()
    # Keep a reference so an accidental unused callback does not hide a fixture error.
    assert original_invoke is not None


def _set_timeout(value: bool, _popen, calls: list[FakePopen]) -> None:
    # The flag is stored on a factory attribute because the adapter may invoke it later
    # on the JobRunner's worker thread.
    _popen.timeout_next = value


def _set_blocking(value: bool, popen) -> None:
    popen.block_next = value


def _piper_body(text: str, **options: Any) -> dict[str, Any]:
    return {
        "expected_version": 1,
        "text": text,
        "voice_id": VOICE_ID,
        **options,
    }


def _submit_and_wait(client, env: Environment, body: dict[str, Any]):
    response = client.post(PIPER_PATH, json=body, headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 202, response.text
    job = env.runner.wait("p1", response.json()["job_id"])
    return response, job


def test_piper_uses_list_argv_and_utf8_stdin_with_optional_flags(piper_harness) -> None:
    client, env, calls, _timeout, lease_states, _set_blocking = piper_harness
    text = "  keep this line.\nquiet\u00a0door  "
    _response, job = _submit_and_wait(
        client, env, _piper_body(text, length_scale=1.25, sentence_silence_seconds=0.4),
    )

    assert job.state == "completed"
    assert len(calls) == 1
    process = calls[0]
    argv = process.argv
    assert isinstance(argv, list)
    assert all(isinstance(item, str) for item in argv)
    assert argv[1:4] == ["-I", "-m", "piper"]
    assert argv[4:10:2] == ["-m", "-c", "-f"]
    assert Path(argv[5]).as_posix().endswith(VOICE_RELATIVE)
    assert Path(argv[7]).as_posix().endswith(CONFIG_RELATIVE)
    assert "--length-scale" in argv and argv[argv.index("--length-scale") + 1] == "1.25"
    assert "--sentence-silence" in argv and argv[argv.index("--sentence-silence") + 1] == "0.4"
    assert text not in argv
    assert process.input_bytes == text.encode("utf-8")
    assert lease_states == [False], "Piper is CPU-only and must not hold the GPU lease"


def test_piper_omits_optional_flags_when_unspecified(piper_harness) -> None:
    client, env, calls, _timeout, _lease_states, _set_blocking = piper_harness
    _response, job = _submit_and_wait(client, env, _piper_body("one plain sentence"))

    assert job.state == "completed"
    assert len(calls) == 1
    assert "--length-scale" not in calls[0].argv
    assert "--sentence-silence" not in calls[0].argv


@pytest.mark.parametrize("mismatch", [VOICE_RELATIVE, CONFIG_RELATIVE])
def test_catalog_hash_mismatch_is_refused_before_process_start(
    piper_harness, monkeypatch, mismatch: str,
) -> None:
    client, env, calls, _timeout, _lease_states, _set_blocking = piper_harness
    target = (Path(__file__).resolve().parents[3] / mismatch).resolve()
    original_open = Path.open

    def mismatched_open(path: Path, mode: str = "r", *args, **kwargs):
        if path.resolve() == target and "b" in mode and "r" in mode:
            return io.BytesIO(b"changed after catalogue receipt")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", mismatched_open)
    response = client.post(PIPER_PATH, json=_piper_body("quiet"), headers={TOKEN_HEADER: TOKEN})
    if response.status_code == 202:
        job = env.runner.wait("p1", response.json()["job_id"])
        assert job.state == "failed"
    else:
        assert response.status_code in {400, 409, 422, 503}, response.text
    assert calls == []


def test_piper_timeout_kills_process_and_fails_job(piper_harness) -> None:
    client, env, calls, set_timeout, _lease_states, _set_blocking = piper_harness
    set_timeout(True)
    _response, job = _submit_and_wait(client, env, _piper_body("wait forever"))

    assert job.state == "failed"
    assert len(calls) == 1
    assert calls[0].killed is True
    assert job.run_id
    stderr_path = f"generated/tts/s1/v0001/{job.run_id}/logs/stderr.txt"
    assert env.root.read_bytes(stderr_path) == PIPER_STDERR
    run = env.store.load(GenerationRun, job.run_id)
    diagnostics = json.dumps(
        [job.model_dump(mode="json"), run.model_dump(mode="json")], ensure_ascii=False,
    )
    assert PIPER_LAST_STDERR_LINE in diagnostics


def test_piper_cancel_writes_captured_stderr_and_last_line_to_error(piper_harness) -> None:
    client, env, calls, _set_timeout, _lease_states, set_blocking = piper_harness
    set_blocking(True)
    response = client.post(PIPER_PATH, json=_piper_body("stop after stderr"),
                           headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert calls.started.wait(5), "fake Piper process was not started"
    process = calls[0]
    assert process.entered_communicate.wait(5), "Piper did not begin consuming process output"

    env.runner.cancel("p1", job_id)
    job = env.runner.wait("p1", job_id)

    assert process.killed is True
    assert job.run_id
    stderr_path = f"generated/tts/s1/v0001/{job.run_id}/logs/stderr.txt"
    assert env.root.read_bytes(stderr_path) == PIPER_STDERR
    # A user cancel is recorded on the job first, by the runner; the adapter's diagnostic
    # (with the stderr tail) is kept in the run's report and log rather than replacing it.
    env.store.load(GenerationRun, job.run_id)
    report_text = env.root.read_text(f"generated/tts/s1/v0001/{job.run_id}/report.json", encoding="utf-8")
    assert PIPER_LAST_STDERR_LINE in report_text
    assert json.loads(report_text)["status"] == "cancelled"
    assert job.state == "cancelled"


def _catalog_voice_ids(path: Path) -> set[str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            value = node.get("voice_id")
            if isinstance(value, str):
                found.add(value)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(document)
    return found


def test_real_piper_synthesizes_when_pinned_runtime_and_voice_exist(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    catalog = repo / "orchestrator" / "adapters" / "tts" / "piper_voices.json"
    runtime = repo / "spikes" / "05_voice" / "_work" / "piper_env" / "Scripts" / "python.exe"
    onnx = repo / VOICE_RELATIVE
    config = repo / CONFIG_RELATIVE
    if not runtime.is_file() or not onnx.is_file() or not config.is_file():
        pytest.skip("the Piper runtime or catalog voice is not installed")
    if not catalog.is_file() or VOICE_ID not in _catalog_voice_ids(catalog):
        pytest.skip("the pinned catalog voice is absent")

    parent = tmp_path / "projects"
    project_path = parent / "p1"
    project_path.mkdir(parents=True)
    root = ProjectRoot(project_path)
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="Real Piper contract"), expected_version=0)
    store.save(Episode(
        episode_id="ep1", project_id="p1", number=1, duration_target_sec=30,
    ), expected_version=0)
    store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))
    settings = Settings.model_validate({"project_root": parent, "piper": {"enabled": True}})
    app = create_app(settings=settings, token=TOKEN)
    try:
        with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
            response = client.post(
                PIPER_PATH,
                json=_piper_body("The hall is empty."),
                headers={TOKEN_HEADER: TOKEN},
            )
            assert response.status_code == 202, response.text
            job = app.state.context.job_runner.wait("p1", response.json()["job_id"])
            assert job.state == "completed"
        run_dir = next((project_path / "generated" / "tts" / "s1" / "v0001").iterdir())
        with wave.open(str(run_dir / "take.wav"), "rb") as generated:
            assert generated.getnframes() > 0
            assert generated.getframerate() > 0
    finally:
        app.state.context.job_runner.close()

