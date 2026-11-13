"""Independent tests derived from CONTRACT.md, without implementation inspection."""
import math
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from orchestrator.adapters.qwen.core import (
    AnalysisRequest,
    QwenRequestError,
    WindowSpec,
    build_prompt,
    coverage_gaps,
    plan_windows,
    validate_request,
)


@pytest.mark.parametrize("field,value", [
    ("purpose", "other"), ("modality", "video"), ("media", "x\0.wav"),
    ("brief", "x\0y"), ("start_seconds", True), ("start_seconds", -1),
    ("start_seconds", float("nan")), ("source_offset_seconds", -1),
    ("duration_seconds", 0), ("duration_seconds", float("inf")),
    ("window_seconds", 0.99), ("window_seconds", 25.01),
    ("overlap_seconds", -1), ("overlap_seconds", 25),
    ("max_new_tokens", True), ("max_new_tokens", 501),
    ("max_new_tokens", 1.0), ("dry_run", 1), ("expected_version", True),
    ("expected_version", 1),
])
def test_request_rejects_without_coercion(field, value):
    with pytest.raises(QwenRequestError):
        validate_request(replace(AnalysisRequest("sample.mkv"), **{field: value}))


@pytest.mark.parametrize("brief", [None, "", " \r\n\t"])
def test_review_requires_independent_intent(brief):
    with pytest.raises(QwenRequestError):
        validate_request(AnalysisRequest("a.wav", purpose="audio_review", brief=brief))


def test_request_and_windows_are_frozen():
    request = AnalysisRequest("a.wav")
    validate_request(request)
    with pytest.raises(FrozenInstanceError):
        request.media = "b.wav"
    with pytest.raises(FrozenInstanceError):
        plan_windows(1)[0].index = 5


@pytest.mark.parametrize("duration,window,overlap,expected", [
    (35, 15, 2, [(0, 15), (13, 28), (20, 35)]),
    (26, 25, 2, [(0, 25), (1, 26)]),
    (2, 15, 2, [(0, 2)]), (30, 15, 0, [(0, 15), (15, 30)]),
])
def test_end_anchored_windows(duration, window, overlap, expected):
    got = plan_windows(duration, window_seconds=window, overlap_seconds=overlap,
                       source_offset_seconds=1397)
    assert [(w.start_seconds, w.end_seconds) for w in got] == expected
    assert [w.index for w in got] == list(range(1, len(got) + 1))
    for index, item in enumerate(got):
        assert item.source_start_seconds == item.start_seconds + 1397
        assert item.source_end_seconds == item.end_seconds + 1397
        assert item.duration_seconds <= window
        assert item.overlap_previous_seconds == (0 if index == 0 else got[index-1].end_seconds-item.start_seconds)
    assert coverage_gaps(0, duration, got) == []


@pytest.mark.parametrize("kwargs", [
    {"duration_seconds": 0}, {"duration_seconds": True},
    {"duration_seconds": math.nan}, {"duration_seconds": math.inf},
    {"duration_seconds": 10, "end_seconds": 11},
    {"duration_seconds": 10, "start_seconds": -1},
    {"duration_seconds": 10, "start_seconds": 5, "end_seconds": 5},
    {"duration_seconds": 10, "overlap_seconds": 25},
    {"duration_seconds": 10, "window_seconds": False},
])
def test_invalid_plans(kwargs):
    with pytest.raises(QwenRequestError):
        plan_windows(**kwargs)


def test_window_limit_is_explicit():
    with pytest.raises(QwenRequestError, match="(?i)interval"):
        plan_windows(1000, window_seconds=1, overlap_seconds=0)


def test_subinterval_and_union_complement():
    got = plan_windows(100, start_seconds=10, end_seconds=45, window_seconds=15)
    assert [(w.start_seconds, w.end_seconds) for w in got] == [(10, 25), (23, 38), (30, 45)]
    completed = [WindowSpec(1, 12, 22, 12, 22), WindowSpec(2, 18, 28, 18, 28),
                 WindowSpec(3, 30, 35, 30, 35)]
    assert coverage_gaps(10, 40, completed) == [[10, 12], [28, 30], [35, 40]]
    assert coverage_gaps(10, 40, list(reversed(completed))) == [[10, 12], [28, 30], [35, 40]]


def test_review_prompt_preserves_brief_and_links_stylization():
    brief = "  Wanted: harsh hiss.\nKeep the deliberate distant voice.  "
    prompt = build_prompt("audio_review", brief, "audio")
    assert brief in prompt
    for heading in ("Audible observations", "Fit to brief", "Suggested changes", "Uncertainties"):
        assert heading.lower() in prompt.lower()
    assert "intent" in prompt.lower()
    assert any(word in prompt.lower() for word in ("styliz", "deliberate", "intended"))
    assert any(word in prompt.lower() for word in ("invent", "unsupported"))


def test_reference_audio_prompt_does_not_request_visuals():
    prompt = build_prompt("reference", None, "audio").lower()
    assert "uncertainties" in prompt and "audio" in prompt
    assert not any(phrase in prompt for phrase in ("describe the scene", "visual observations", "what you see"))
    joint = build_prompt("reference", None, "joint").lower()
    assert "visual" in joint and "audio" in joint


def test_near_window_overlap_guard_finishes_in_bounded_process():
    result = subprocess.run([sys.executable, "-B", "-c", '''
from orchestrator.adapters.qwen.core import plan_windows,QwenRequestError
try:
    plan_windows(100,window_seconds=25,overlap_seconds=24.999999999999996)
except QwenRequestError:
    print('bounded rejection')
else:
    raise AssertionError('expected max256 guard')
'''], cwd=Path(__file__).resolve().parents[3], check=False, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == b"bounded rejection"
