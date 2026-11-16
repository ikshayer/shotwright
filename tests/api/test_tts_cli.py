"""CLI wait semantics for the TTS generation operations."""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path
from typing import Any

import pytest

from orchestrator.domain.entities import Asset, Episode, Project, Shot
from orchestrator.platform.settings import Settings
from tests.api.test_parity import _cli, _runner_and_command
from tests.service.conftest import Environment


PIPER_VOICE = "en_US-ryan-high"
PIPER_VOICE_RELATIVE = f"spikes/05_voice/_work/{PIPER_VOICE}.onnx"
PIPER_CONFIG_RELATIVE = f"spikes/05_voice/_work/{PIPER_VOICE}.onnx.json"


@pytest.mark.parametrize(
    "command,body",
    [
        (
            "generate-piper-speech",
            {"expected_version": 1, "text": "A voice from the speaker.", "voice_id": PIPER_VOICE},
        ),
        (
            "generate-indextts2-speech",
            {"expected_version": 1, "text": "A voice from the room.", "speaker_asset_id": "speaker",
             "emotion": {"mode": "none"}},
        ),
    ],
)
def test_tts_cli_rejects_no_wait_before_dispatch(command: str, body: dict[str, Any]) -> None:
    runner, cli = _runner_and_command(_cli())

    result = runner.invoke(cli, [
        command,
        "--project-id", "missing",
        "--episode-id", "missing",
        "--shot-id", "missing",
        "--no-wait",
        "--data", json.dumps(body),
    ])

    assert result.exit_code != 0
    assert "wait" in result.output.lower()
    assert "not found" not in result.output.lower()


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(22050)
        output.writeframes(b"\x00\x00" * 48)


@pytest.fixture
def successful_piper_cli(tmp_path: Path, monkeypatch):
    """The single CLI fake-Popen seam used to exercise a real wait and Asset registration."""
    from orchestrator.adapters.tts.piper import PiperAdapter
    from orchestrator.api import cli as cli_module

    repo = Path(__file__).resolve().parents[2]
    runtime = repo / "spikes/05_voice/_work/piper_env/Scripts/python.exe"
    if not runtime.is_file() or not (repo / PIPER_VOICE_RELATIVE).is_file() or not (
        repo / PIPER_CONFIG_RELATIVE
    ).is_file():
        pytest.skip("the pinned Piper runtime and catalog voice are not installed")

    parent = tmp_path / "projects"
    project_path = parent / "p1"
    env = Environment(project_path)
    env.settings.piper.enabled = True
    env.store.save(Project(project_id="p1", title="TTS CLI wait"), expected_version=0)
    env.store.save(Episode(
        episode_id="ep1", project_id="p1", number=1, duration_target_sec=30,
    ), expected_version=0)
    env.store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))

    class FakePopen:
        def __init__(self, argv, **kwargs):
            self.argv = list(argv)
            self.kwargs = kwargs
            self.returncode = None
            self.stderr = io.BytesIO(b"CLI fake Piper stderr\n")
            self.stdout = io.BytesIO()

        def communicate(self, input=None, timeout=None):
            _write_wav(Path(self.argv[self.argv.index("-f") + 1]))
            self.returncode = 0
            return b"", b"CLI fake Piper stderr\n"

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def terminate(self):
            self.kill()

    backend = PiperAdapter(env.root, env.settings, popen=FakePopen)
    env.adapter.invoke = lambda request, job, *, queue: backend.invoke(request, job, queue=queue)

    original_context = cli_module.OperationContext

    def context(*args, **kwargs):
        kwargs["job_runner"] = env.runner
        return original_context(*args, **kwargs)

    monkeypatch.setattr(cli_module, "OperationContext", context)
    settings = Settings.model_validate({
        "project_root": parent,
        "piper": {"enabled": True},
    })
    yield env, settings
    env.runner.close()


def test_tts_cli_wait_result_includes_run_and_asset_ids(successful_piper_cli) -> None:
    env, settings = successful_piper_cli
    runner, cli = _runner_and_command(_cli())
    result = runner.invoke(cli, [
        "generate-piper-speech",
        "--project-id", "p1",
        "--episode-id", "ep1",
        "--shot-id", "s1",
        "--data", json.dumps({
            "expected_version": 1,
            "text": "A processed voice for an intercom.",
            "voice_id": PIPER_VOICE,
        }),
        "--json",
    ], obj={"settings": settings})

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    values = list(_mappings(payload))
    run_ids = [value["run_id"] for value in values if isinstance(value.get("run_id"), str)]
    asset_ids = [value["asset_id"] for value in values if isinstance(value.get("asset_id"), str)]
    assert run_ids and asset_ids, payload
    assert run_ids[0] in result.output
    assert asset_ids[0] == f"tts-{run_ids[0]}"
    registered = env.store.load(Asset, asset_ids[0])
    assert registered.asset_id == asset_ids[0]


def _mappings(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _mappings(child)
