"""HTTP contract tests for PerformanceEdit."""

from __future__ import annotations

import hashlib
import io
import math
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.domain.entities import Asset, Episode, Project, Shot
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER


INSPECT_PATH = "/projects/p1/assets/{asset_id}/pauses"
APPLY_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/edit"
RATE = 22050
FRAME_SAMPLES = round(0.010 * RATE)
SPEECH_SAMPLES = 50 * FRAME_SAMPLES


class CaptureRunner:
    """Capture accepted requests without dispatching a job."""

    def __init__(self) -> None:
        self.submissions: list[tuple[str, Any]] = []

    def submit(self, project_id: str, request: Any) -> SimpleNamespace:
        self.submissions.append((project_id, request))
        return SimpleNamespace(job_id="perfedit-job", run_id="perfedit-run", state="queued")


def _wav_bytes(
    *,
    pause_samples: tuple[int, ...] = (30 * FRAME_SAMPLES, 40 * FRAME_SAMPLES),
    leading_samples: int = 10 * FRAME_SAMPLES,
    trailing_samples: int = 10 * FRAME_SAMPLES,
    amplitude: int = 8000,
    sample_rate: int = RATE,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Build frame-aligned tones and exact digital silences using only the stdlib."""
    frame_samples = round(0.010 * sample_rate)
    speech_samples = 50 * frame_samples
    values: list[int] = [0] * leading_samples
    for burst_index in range(len(pause_samples) + 1):
        for i in range(speech_samples):
            values.append(round(amplitude * math.sin(2.0 * math.pi * 440.0 * i / sample_rate)))
        if burst_index < len(pause_samples):
            values.extend([0] * pause_samples[burst_index])

    values.extend([0] * trailing_samples)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        if sample_width == 2:
            if channels == 1:
                frames = struct.pack(f"<{len(values)}h", *values)
            else:
                frames = struct.pack(f"<{len(values) * channels}h", *(values * channels))
        else:
            peak = 1 << (8 * sample_width - 1)
            encoded = bytes(
                max(0, min(255, int(value / peak * 127) + 128))
                for value in values
                for _channel in range(channels)
            )
            frames = encoded
        writer.writeframes(frames)
    return stream.getvalue()


def _wav_from_samples(samples: list[int], *, sample_rate: int = RATE) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return stream.getvalue()


def _mulaw_wav_bytes(*, sample_rate: int = RATE, sample_count: int = 2205) -> bytes:
    """A minimal RIFF/WAVE_FORMAT_MULAW file, which wave.open cannot decode as PCM."""
    channels = 1
    byte_rate = sample_rate * channels
    block_align = channels
    fmt = struct.pack("<HHIIHH", 7, channels, sample_rate, byte_rate, block_align, 8)
    fact = struct.pack("<I", sample_count)
    data = b"\xff" * sample_count

    def chunk(name: bytes, content: bytes) -> bytes:
        pad = b"\x00" if len(content) % 2 else b""
        return name + struct.pack("<I", len(content)) + content + pad

    body = b"WAVE" + chunk(b"fmt ", fmt) + chunk(b"fact", fact) + chunk(b"data", data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.fixture
def perfedit_api(tmp_path: Path):
    projects = tmp_path / "projects"
    project_path = projects / "p1"
    project_path.mkdir(parents=True)
    settings = Settings(project_root=projects)
    runner = CaptureRunner()
    from orchestrator.api.app import create_app

    app = create_app(settings=settings, token=TOKEN, job_runner=runner)
    root = ProjectRoot(project_path)
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="PerformanceEdit API"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30), expected_version=0)
    store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield client, app, store, root, runner


def _register_take(
    store: ProjectStore,
    root: ProjectRoot,
    *,
    asset_id: str = "take",
    data: bytes | None = None,
    filename: str = "take.wav",
    asset_type: str = "voice",
    voice_consent: str | None = "synthetic",
    checksum: str | None = None,
) -> Asset:
    content = _wav_bytes() if data is None else data
    relative = f"assets/audio/{asset_id}/{filename}"
    root.write_bytes(relative, content)
    fields: dict[str, Any] = {
        "asset_id": asset_id,
        "type": asset_type,
        "generated": False,
        "license": "synthetic fixture",
        "model_or_voice_license": "synthetic fixture",
        "file_path": relative,
        "checksum": checksum or hashlib.sha256(content).hexdigest(),
        "reuse_scope": "project fixture",
    }
    if voice_consent is not None:
        fields["voice_consent"] = voice_consent
        if voice_consent != "synthetic":
            fields["voice_consent_basis"] = "Synthetic test fixture."
    asset = Asset.model_validate(fields)
    store.save(asset, expected_version=0)
    return asset


def _snapshot(root: ProjectRoot) -> dict[str, bytes]:
    return {
        path.relative_to(root.path).as_posix(): path.read_bytes()
        for path in root.path.rglob("*")
        if path.is_file()
    }


def _valid_body(store: ProjectStore, *, take_asset_id: str = "take") -> dict[str, Any]:
    _shot, version = store.load_with_version(Shot, "s1", episode_id="ep1")
    return {
        "expected_version": version,
        "take_asset_id": take_asset_id,
        "seed": 7,
        "pauses": [{"index": 0, "target_ms": 500, "breath": None}],
    }


def _apply(client: TestClient, body: dict[str, Any]):
    return client.post(APPLY_PATH, json=body, headers={TOKEN_HEADER: TOKEN})


def test_inspect_take_pauses_reports_sample_derived_shape_and_is_read_only(
    perfedit_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root)
    before = _snapshot(root)

    def unexpected_process(*_args, **_kwargs):
        raise AssertionError("pause inspection must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", unexpected_process)
    monkeypatch.setattr(subprocess, "run", unexpected_process)
    response = client.get(INSPECT_PATH.format(asset_id="take"), headers={TOKEN_HEADER: TOKEN})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"sample_rate", "duration_ms", "speech_onset_ms", "pauses"}
    assert body["sample_rate"] == RATE
    total_samples = 10 * FRAME_SAMPLES + 3 * SPEECH_SAMPLES + 70 * FRAME_SAMPLES + 10 * FRAME_SAMPLES
    assert body["duration_ms"] == round(total_samples * 1000 / RATE, 1)
    assert body["speech_onset_ms"] == round(10 * FRAME_SAMPLES * 1000 / RATE, 1)
    assert [set(pause) for pause in body["pauses"]] == [
        {"index", "start_ms", "end_ms", "duration_ms"},
        {"index", "start_ms", "end_ms", "duration_ms"},
    ]
    expected_starts = [
        10 * FRAME_SAMPLES + SPEECH_SAMPLES,
        10 * FRAME_SAMPLES + 2 * SPEECH_SAMPLES + 30 * FRAME_SAMPLES,
    ]
    expected_lengths = [30 * FRAME_SAMPLES, 40 * FRAME_SAMPLES]
    for index, (pause, start, length) in enumerate(zip(body["pauses"], expected_starts, expected_lengths)):
        assert pause["index"] == index
        assert pause["start_ms"] == round(start * 1000 / RATE, 1)
        assert pause["end_ms"] == round((start + length) * 1000 / RATE, 1)
        assert pause["duration_ms"] == round(length * 1000 / RATE, 1)
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_inspect_take_pauses_honours_noise_and_minimum_pause_queries(perfedit_api) -> None:
    client, _app, store, root, _runner = perfedit_api
    # A low-amplitude tone falls below the default -38 dBFS threshold and is speech at -50.
    data = _wav_bytes(amplitude=400)
    _register_take(store, root, data=data)

    quiet_as_tone = client.get(
        INSPECT_PATH.format(asset_id="take"),
        params={"noise_db": -50, "min_pause_ms": 350},
        headers={TOKEN_HEADER: TOKEN},
    )
    quiet_as_noise = client.get(
        INSPECT_PATH.format(asset_id="take"),
        params={"noise_db": -38, "min_pause_ms": 350},
        headers={TOKEN_HEADER: TOKEN},
    )

    assert quiet_as_tone.status_code == 200, quiet_as_tone.text
    assert len(quiet_as_tone.json()["pauses"]) == 1
    assert quiet_as_tone.json()["pauses"][0]["index"] == 0
    assert quiet_as_noise.status_code == 200, quiet_as_noise.text
    assert quiet_as_noise.json()["pauses"] == []


@pytest.mark.parametrize(
    "query",
    [
        {"noise_db": -81}, {"noise_db": -9},
        {"min_pause_ms": 29}, {"min_pause_ms": 3001},
    ],
)
def test_inspect_take_pauses_rejects_query_values_outside_bounds(perfedit_api, query) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root)
    before = _snapshot(root)

    response = client.get(
        INSPECT_PATH.format(asset_id="take"), params=query,
        headers={TOKEN_HEADER: TOKEN},
    )

    assert response.status_code in {400, 422}, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


@pytest.mark.parametrize(
    "variant",
    ["pcm8", "stereo", "wrong_extension", "checksum"],
)
def test_inspect_take_pauses_refuses_unsupported_or_unbound_takes(perfedit_api, variant) -> None:
    client, _app, store, root, runner = perfedit_api
    content = _wav_bytes(sample_width=1) if variant == "pcm8" else _wav_bytes(channels=2) if variant == "stereo" else _wav_bytes()
    filename = "take.mp3" if variant == "wrong_extension" else "take.wav"
    checksum = "f" * 64 if variant == "checksum" else None
    _register_take(store, root, data=content, filename=filename, checksum=checksum)
    before = _snapshot(root)

    response = client.get(INSPECT_PATH.format(asset_id="take"), headers={TOKEN_HEADER: TOKEN})

    assert response.status_code in {400, 422}, response.text
    assert _snapshot(root) == before
    assert runner.submissions == []


def test_inspection_skips_consent_gate_but_edit_requires_consent(perfedit_api) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root, voice_consent="licensed_recording")

    inspected = client.get(INSPECT_PATH.format(asset_id="take"), headers={TOKEN_HEADER: TOKEN})
    edited = _apply(client, _valid_body(store))

    assert inspected.status_code == 200, inspected.text
    assert edited.status_code in {400, 422}, edited.text
    assert runner.submissions == []


@pytest.mark.parametrize("consent", ["licensed_recording", None])
def test_apply_rejects_licensed_or_missing_voice_consent_without_side_effects(
    perfedit_api, consent,
) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root, voice_consent=consent)
    before = _snapshot(root)

    response = _apply(client, _valid_body(store))

    assert response.status_code in {400, 422}, response.text
    assert runner.submissions == []
    assert _snapshot(root) == before


def test_apply_refuses_mulaw_with_a_post_profile_remediation(perfedit_api) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root, data=_mulaw_wav_bytes())
    before = _snapshot(root)

    response = _apply(client, _valid_body(store))

    assert response.status_code in {400, 422}, response.text
    assert "post" in response.text.lower() and "profile" in response.text.lower(), response.text
    assert runner.submissions == []
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "case",
    [
        "seed_low", "seed_high", "noise_low", "noise_high", "min_pause_low", "min_pause_high",
        "too_many_pauses", "duplicate_index", "descending_indices", "unknown_index",
        "target_low", "target_high", "target_narrower_than_natural",
        "breath_duration_low", "breath_duration_high", "breath_gain_low", "breath_gain_high",
        "gain_list_too_long", "gain_low", "gain_high",
        "false_start_copy_low", "false_start_copy_high", "false_start_gain_low", "false_start_gain_high",
        "false_start_gap_low", "false_start_gap_high",
        "jitter_gap_inverted", "jitter_gap_low", "jitter_gap_high",
        "jitter_gain_inverted", "jitter_gain_low", "jitter_gain_high", "no_op",
    ],
)
def test_invalid_edit_requests_are_synchronous_and_leave_no_job_or_files(perfedit_api, case) -> None:
    client, _app, store, root, runner = perfedit_api
    pause_count = 33 if case == "too_many_pauses" else 2
    _register_take(store, root, data=_wav_bytes(pause_samples=(30 * FRAME_SAMPLES,) * pause_count))
    body = _valid_body(store)
    body["pauses"] = [{"index": 0, "target_ms": 500, "breath": None}]
    if case == "seed_low":
        body["seed"] = -1
    elif case == "seed_high":
        body["seed"] = 2**31
    elif case == "noise_low":
        body["detection"] = {"noise_db": -81, "min_pause_ms": 120}
    elif case == "noise_high":
        body["detection"] = {"noise_db": -9, "min_pause_ms": 120}
    elif case == "min_pause_low":
        body["detection"] = {"noise_db": -38, "min_pause_ms": 29}
    elif case == "min_pause_high":
        body["detection"] = {"noise_db": -38, "min_pause_ms": 3001}
    elif case == "too_many_pauses":
        body["pauses"] = [{"index": i, "target_ms": 500, "breath": None} for i in range(33)]
    elif case == "duplicate_index":
        body["pauses"] = [
            {"index": 0, "target_ms": 500, "breath": None},
            {"index": 0, "target_ms": 600, "breath": None},
        ]
    elif case == "descending_indices":
        body["pauses"] = [
            {"index": 1, "target_ms": 600, "breath": None},
            {"index": 0, "target_ms": 500, "breath": None},
        ]
    elif case == "unknown_index":
        body["pauses"][0]["index"] = 99
    elif case == "target_low":
        body["pauses"][0]["target_ms"] = 29
    elif case == "target_high":
        body["pauses"][0]["target_ms"] = 10001
    elif case == "target_narrower_than_natural":
        body["pauses"][0]["target_ms"] = 299
    elif case in {"breath_duration_low", "breath_duration_high", "breath_gain_low", "breath_gain_high"}:
        duration, gain = 120, -10
        if case == "breath_duration_low": duration = 99
        if case == "breath_duration_high": duration = 2001
        if case == "breath_gain_low": gain = -40.1
        if case == "breath_gain_high": gain = 0.1
        body["pauses"][0]["breath"] = {"duration_ms": duration, "gain_db": gain}
    elif case == "gain_list_too_long":
        body["segment_gain_db"] = [0, 0, 0, 0]
    elif case == "gain_low":
        body["segment_gain_db"] = [-24.1]
    elif case == "gain_high":
        body["segment_gain_db"] = [6.1]
    elif case in {"false_start_copy_low", "false_start_copy_high", "false_start_gain_low", "false_start_gain_high", "false_start_gap_low", "false_start_gap_high"}:
        false_start = {"copy_ms": 100, "gain_db": -6, "gap_after_ms": 10}
        if case == "false_start_copy_low": false_start["copy_ms"] = 49
        if case == "false_start_copy_high": false_start["copy_ms"] = 2001
        if case == "false_start_gain_low": false_start["gain_db"] = -24.1
        if case == "false_start_gain_high": false_start["gain_db"] = 0.1
        if case == "false_start_gap_low": false_start["gap_after_ms"] = -1
        if case == "false_start_gap_high": false_start["gap_after_ms"] = 3001
        body["pauses"] = []
        body["false_start"] = false_start
    elif case.startswith("jitter_"):
        jitter = {"gap_ms_range": [-1, 1], "gain_db_range": [-1, 1]}
        if case == "jitter_gap_inverted": jitter["gap_ms_range"] = [2, 1]
        if case == "jitter_gap_low": jitter["gap_ms_range"] = [-501, 0]
        if case == "jitter_gap_high": jitter["gap_ms_range"] = [0, 501]
        if case == "jitter_gain_inverted": jitter["gain_db_range"] = [2, 1]
        if case == "jitter_gain_low": jitter["gain_db_range"] = [-6.1, 0]
        if case == "jitter_gain_high": jitter["gain_db_range"] = [0, 6.1]
        body["jitter"] = jitter
    elif case == "no_op":
        body["pauses"] = []
    before = _snapshot(root)

    response = _apply(client, body)

    assert response.status_code in {400, 422}, f"{case}: {response.text}"
    assert runner.submissions == [], f"{case}: invalid request reached the job runner"
    assert _snapshot(root) == before, f"{case}: invalid request wrote project files"


@pytest.mark.parametrize("seed", [0, 2**31 - 1])
def test_apply_accepts_seed_bounds(perfedit_api, seed) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root)
    body = _valid_body(store)
    body["seed"] = seed

    response = _apply(client, body)

    assert response.status_code == 202, response.text
    assert len(runner.submissions) == 1


@pytest.mark.parametrize(
    "edge",
    [
        "noise_min", "noise_max", "min_pause_min", "min_pause_max",
        "zero_pauses", "thirty_two_pauses", "target_min", "target_max",
        "breath_duration_min", "breath_duration_max", "breath_gain_min", "breath_gain_max",
        "segment_gain_min", "segment_gain_max",
        "false_copy_min", "false_copy_max", "false_gain_min", "false_gain_max",
        "false_gap_min", "false_gap_max",
        "jitter_gap_min", "jitter_gap_max", "jitter_gain_min", "jitter_gain_max",
    ],
)
def test_apply_accepts_inclusive_edit_bounds(perfedit_api, edge) -> None:
    client, _app, store, root, runner = perfedit_api
    if edge == "thirty_two_pauses":
        pause_count = 32
    elif edge == "target_min":
        pause_count = 2
    else:
        pause_count = 2
    if edge == "target_min":
        # The inclusive lower edge that is actually reachable: a target exactly equal to the
        # pause's natural length ("an edit only widens", contract 3.2). A target of 30 ms
        # itself cannot be exercised at 22050 Hz: a detected pause needs >= 662 samples
        # (> 30 ms), so no detected pause is ever exactly 30 ms long.
        data = _wav_bytes(pause_samples=(30 * FRAME_SAMPLES, 30 * FRAME_SAMPLES))
    else:
        amplitude = 20000 if edge == "noise_max" else 8000
        data = _wav_bytes(pause_samples=(30 * FRAME_SAMPLES,) * pause_count, amplitude=amplitude)
    _register_take(store, root, data=data)
    body = _valid_body(store)

    if edge == "noise_min":
        body["detection"] = {"noise_db": -80, "min_pause_ms": 120}
    elif edge == "noise_max":
        body["detection"] = {"noise_db": -10, "min_pause_ms": 120}
    elif edge == "min_pause_min":
        body["detection"] = {"noise_db": -38, "min_pause_ms": 30}
    elif edge == "min_pause_max":
        body["detection"] = {"noise_db": -38, "min_pause_ms": 3000}
        body["pauses"] = []
        body["segment_gain_db"] = [1]
    elif edge == "zero_pauses":
        body["pauses"] = []
        body["segment_gain_db"] = [1]
    elif edge == "thirty_two_pauses":
        body["pauses"] = [
            {"index": i, "target_ms": 500, "breath": None}
            for i in range(32)
        ]
    elif edge == "target_min":
        natural_ms = 30 * FRAME_SAMPLES * 1000 / RATE
        body["pauses"] = [{"index": 0, "target_ms": natural_ms, "breath": None}]
        # A target equal to the natural length changes nothing by itself; a gain keeps the
        # edit from being refused as a no-op, so the case isolates the target bound.
        body["segment_gain_db"] = [1]
    elif edge == "target_max":
        body["pauses"][0]["target_ms"] = 10000
    elif edge in {"breath_duration_min", "breath_duration_max", "breath_gain_min", "breath_gain_max"}:
        duration, gain = 120, -10
        if edge == "breath_duration_min": duration = 100
        if edge == "breath_duration_max": duration = 2000
        if edge == "breath_gain_min": gain = -40
        if edge == "breath_gain_max": gain = 0
        body["pauses"][0]["target_ms"] = 10000
        body["pauses"][0]["breath"] = {"duration_ms": duration, "gain_db": gain}
    elif edge in {"segment_gain_min", "segment_gain_max"}:
        body["segment_gain_db"] = [-24 if edge == "segment_gain_min" else 6]
    elif edge.startswith("false_"):
        body["pauses"] = []
        body["false_start"] = {"copy_ms": 100, "gain_db": -6, "gap_after_ms": 10}
        field, bound = edge.removeprefix("false_").rsplit("_", 1)
        values = {
            "copy": (50, 2000), "gain": (-24, 0), "gap": (0, 3000),
        }
        key = {"copy": "copy_ms", "gain": "gain_db", "gap": "gap_after_ms"}[field]
        body["false_start"][key] = values[field][0 if bound == "min" else 1]
    elif edge.startswith("jitter_"):
        body["jitter"] = {"gap_ms_range": [-1, 1], "gain_db_range": [-1, 1]}
        field, bound = edge.removeprefix("jitter_").rsplit("_", 1)
        limits = {"gap": (-500, 500), "gain": (-6, 6)}
        key = {"gap": "gap_ms_range", "gain": "gain_db_range"}[field]
        value = limits[field][0 if bound == "min" else 1]
        body["jitter"][key] = [value, value]
        if field == "gap":
            body["pauses"][0]["target_ms"] = 10000

    response = _apply(client, body)

    assert response.status_code == 202, f"{edge}: {response.text}"
    assert len(runner.submissions) == 1


def test_apply_rejects_stale_shot_version_without_enqueuing_or_writing(perfedit_api) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root)
    body = _valid_body(store)
    # A token that is valid in form (>= 1) but not the Shot's current one. Subtracting one
    # would reach 0, which the body schema rejects (422) before the version check (409).
    body["expected_version"] += 1
    before = _snapshot(root)

    response = _apply(client, body)

    assert response.status_code == 409, response.text
    assert runner.submissions == []
    assert _snapshot(root) == before


def test_pause_minimum_uses_sample_duration_at_22050_hz(perfedit_api) -> None:
    client, _app, store, root, _runner = perfedit_api
    below = _wav_bytes(pause_samples=(12 * FRAME_SAMPLES,))
    _register_take(store, root, asset_id="below-threshold", data=below)

    response = client.get(
        INSPECT_PATH.format(asset_id="below-threshold"), headers={TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sample_rate"] == 22050
    assert body["pauses"] == [], "2640 samples are only 119.73 ms, below 120 ms"

    # The zero run is exactly 2646 samples (120 ms). Its last six samples share a
    # 220-sample frame with low-level signal; the frame RMS remains quiet, so it is
    # included as a quiet frame and the detected pause qualifies.
    frame_samples = FRAME_SAMPLES
    first_tone = [
        round(8000 * math.sin(2.0 * math.pi * 440.0 * i / RATE))
        for i in range(50 * frame_samples)
    ]
    second_tone = first_tone.copy()
    exact_threshold = (
        [0] * (10 * frame_samples)
        + first_tone
        + [0] * 2646
        + [1] * (frame_samples - (2646 % frame_samples))
        + second_tone
        + [0] * (10 * frame_samples)
    )
    _register_take(
        store, root, asset_id="at-threshold", data=_wav_from_samples(exact_threshold),
    )

    response = client.get(
        INSPECT_PATH.format(asset_id="at-threshold"), headers={TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200, response.text
    pauses = response.json()["pauses"]
    assert len(pauses) == 1
    assert pauses[0]["duration_ms"] >= 120.0


def test_sub_frame_quiet_run_does_not_form_pause_and_trailing_partial_is_not_pause(
    perfedit_api,
) -> None:
    client, _app, store, root, _runner = perfedit_api
    frame = FRAME_SAMPLES
    tone = [
        round(8000 * math.sin(2.0 * math.pi * 440.0 * i / RATE))
        for i in range(3 * frame)
    ]
    tone[frame + 50 : frame + 150] = [0] * 100
    _register_take(store, root, asset_id="sub-frame", data=_wav_from_samples(tone))
    _register_take(
        store,
        root,
        asset_id="trailing-partial",
        data=_wav_from_samples(tone[: 2 * frame] + [0] * (frame // 2)),
    )

    sub_frame_response = client.get(
        INSPECT_PATH.format(asset_id="sub-frame"), headers={TOKEN_HEADER: TOKEN},
    )
    trailing_response = client.get(
        INSPECT_PATH.format(asset_id="trailing-partial"), headers={TOKEN_HEADER: TOKEN},
    )

    assert sub_frame_response.status_code == 200, sub_frame_response.text
    assert sub_frame_response.json()["pauses"] == []
    assert trailing_response.status_code == 200, trailing_response.text
    trailing_body = trailing_response.json()
    assert trailing_body["speech_onset_ms"] == 0.0
    assert trailing_body["pauses"] == []


@pytest.mark.parametrize("sample_rate", [8000, 48000])
def test_pause_detection_accepts_sample_rate_endpoints(perfedit_api, sample_rate: int) -> None:
    client, _app, store, root, _runner = perfedit_api
    frame = round(0.010 * sample_rate)
    data = _wav_bytes(
        sample_rate=sample_rate,
        pause_samples=(12 * frame,),
        leading_samples=10 * frame,
        trailing_samples=10 * frame,
    )
    _register_take(store, root, asset_id=f"rate-{sample_rate}", data=data)

    response = client.get(
        INSPECT_PATH.format(asset_id=f"rate-{sample_rate}"), headers={TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sample_rate"] == sample_rate
    assert len(body["pauses"]) == 1
    assert body["pauses"][0]["duration_ms"] == pytest.approx(120.0)


def test_all_silent_take_has_no_onset_or_pauses_and_refuses_false_start(perfedit_api) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root, asset_id="silent", data=_wav_bytes(amplitude=0))

    inspected = client.get(
        INSPECT_PATH.format(asset_id="silent"), headers={TOKEN_HEADER: TOKEN},
    )

    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["speech_onset_ms"] is None
    assert inspected.json()["pauses"] == []
    before = _snapshot(root)
    body = _valid_body(store, take_asset_id="silent")
    body["pauses"] = []
    body["false_start"] = {"copy_ms": 100, "gain_db": -6, "gap_after_ms": 0}

    response = _apply(client, body)

    assert response.status_code == 422, response.text
    assert runner.submissions == []
    assert _snapshot(root) == before


@pytest.mark.parametrize(("amplitude", "gain_db"), [(0, 1), (8000, 1e-10)])
def test_apply_refuses_edits_that_do_not_change_any_rounded_sample(
    perfedit_api, amplitude: int, gain_db: float,
) -> None:
    client, _app, store, root, runner = perfedit_api
    _register_take(store, root, data=_wav_bytes(amplitude=amplitude))
    body = _valid_body(store)
    body["pauses"] = []
    body["segment_gain_db"] = [gain_db]
    before = _snapshot(root)

    response = _apply(client, body)

    assert response.status_code == 422, response.text
    assert runner.submissions == []
    assert _snapshot(root) == before


def test_malformed_wav_with_matching_checksum_returns_422_for_inspect_and_apply(
    perfedit_api,
) -> None:
    client, _app, store, root, runner = perfedit_api
    malformed = b"RIFF" + struct.pack("<I", 28) + b"WAVEfmt " + struct.pack("<I", 16)
    matching_checksum = hashlib.sha256(malformed).hexdigest()
    _register_take(
        store,
        root,
        asset_id="truncated-fmt",
        data=malformed,
        checksum=matching_checksum,
    )
    before = _snapshot(root)

    inspected = client.get(
        INSPECT_PATH.format(asset_id="truncated-fmt"), headers={TOKEN_HEADER: TOKEN},
    )
    body = _valid_body(store, take_asset_id="truncated-fmt")
    applied = _apply(client, body)

    assert inspected.status_code == 422, inspected.text
    assert applied.status_code == 422, applied.text
    assert runner.submissions == []
    assert _snapshot(root) == before
