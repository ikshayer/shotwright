"""Black-box adapter and real CLI tests; no model execution."""
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from orchestrator.adapters.qwen.core import AnalysisRequest, MediaInfo
from orchestrator.adapters.qwen.runner import QwenAdapter
from orchestrator.platform.containment import ContainmentError, ProjectRoot

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def project():
    parent = ProjectRoot(REPO)
    directory = parent.make_dir("_work/qwen_contract_tests/" + uuid.uuid4().hex)
    root = ProjectRoot(directory)
    root.write_bytes("media.mkv", b"fake media; backend probes independently")
    return root


class PlanningBackend:
    def __init__(self):
        self.calls = []

    def probe(self, media_rel, *, work_rel, modality):
        self.calls.append(("probe", media_rel, modality))
        return MediaInfo(70, 48000, 2, 320, 240, 24, 0, 0)

    def prepare(self, *args, **kwargs):
        raise AssertionError("dry run must not prepare")

    def infer_batch(self, *args, **kwargs):
        raise AssertionError("dry run must not load a model")


def test_dry_run_writes_strict_immutable_planning_report(project):
    backend = PlanningBackend()
    adapter = QwenAdapter(project, backend=backend)
    request = AnalysisRequest("media.mkv", purpose="audio_review", brief="deliberate hiss",
                              start_seconds=5, duration_seconds=60, source_offset_seconds=100,
                              dry_run=True)
    first = adapter.run(request)
    assert first["status"] == "planned"
    assert first["version"] == 1 and first["supersedes"] is None
    assert first["brief"]["text"] == "deliberate hiss"
    assert first["input_coverage"]["requested_interval"] == [5, 65]
    assert first["input_coverage"]["uncovered_intervals"] == [[5, 65]]
    assert all(w["status"] == "planned" for w in first["windows"])
    assert first["windows"][0]["source_start_seconds"] == 105
    assert len(first["windows"]) == 3
    original = project.read_bytes(first["report_path"])
    disk = json.loads(original)
    assert disk["status"] == "planned"
    json.dumps(first, allow_nan=False)
    assert project.resolve(first["markdown_path"]).is_file()
    second = adapter.run(request)
    assert second["report_path"] != first["report_path"]
    assert project.read_bytes(first["report_path"]) == original
    assert backend.calls == [("probe", "media.mkv", "joint")] * 2


@pytest.mark.parametrize("media", ["../outside.wav", "C:/outside.wav", "https://example.com/a.wav"])
def test_reject_external_before_backend(project, media):
    backend = PlanningBackend()
    with pytest.raises((ContainmentError, ValueError)):
        QwenAdapter(project, backend=backend).run(AnalysisRequest(media, dry_run=True))
    assert backend.calls == []


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "orchestrator.adapters.qwen", *args],
                          check=False, cwd=REPO, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("args", [[], ["unknown"], ["analyze"],
    ["review", "--media", "missing.wav"],
    ["analyze", "--media", "missing.wav", "--max-new-tokens", "501"],
    ["review", "--media", "missing.wav", "--brief", "x", "--brief-file", "brief.txt"],
    ["review", "--media", "missing.wav", "--brief-file", "../outside.txt"],
])
def test_real_cli_errors_are_single_json(args):
    result = cli(*args)
    assert result.returncode != 0
    assert result.stdout.strip() == ""
    error = json.loads(result.stderr)
    assert isinstance(error, dict)
    assert "retryable" not in error
    assert "remediation" not in error


def test_inspect_is_json_without_model_load():
    result = cli("inspect")
    assert result.returncode == 0, result.stderr
    assert isinstance(json.loads(result.stdout), dict)
