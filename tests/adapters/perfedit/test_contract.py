"""PerformanceEdit output and FFmpeg contract tests using a synthetic FFmpeg boundary."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import math
import re
import struct
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.domain.entities import Asset, Episode, GenerationRun, Project, Shot
from orchestrator.domain.seeds import INT32_MAX, derive_seed, jitter
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.ffmpeg_identity import normalize_ffmpeg_identity
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import RecordNotFound
from tests.api.conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER
from tests.service.conftest import Environment


APPLY_PATH = "/projects/p1/episodes/ep1/shots/s1/speech/edit"
RATE = 22050
FRAME_SAMPLES = round(0.010 * RATE)
SPEECH_SAMPLES = 50 * FRAME_SAMPLES
FFMPEG_IDENTITY = (
    "ffmpeg version perfedit-test 1.0\n"
    "configuration: --disable-gpl --enable-version3\n"
    "libavutil 60.1.0 / 60.1.0\n"
)


@pytest.fixture(scope="module")
def perfedit_adapter_factory():
    """Single assumed symbol: the contract does not name the adapter module or class."""
    module = importlib.import_module("orchestrator.adapters.perfedit.runner")
    adapter_class = module.PerformanceEditAdapter

    def create(root: ProjectRoot, settings, popen):
        return adapter_class(root, settings, popen=popen)

    return create


def take_bytes(
    *,
    pause_samples: tuple[int, ...] = (30 * FRAME_SAMPLES, 40 * FRAME_SAMPLES),
    leading_samples: int = 10 * FRAME_SAMPLES,
    trailing_samples: int = 10 * FRAME_SAMPLES,
    amplitude: int = 8000,
) -> bytes:
    samples = [0] * leading_samples
    for burst_index in range(len(pause_samples) + 1):
        for i in range(SPEECH_SAMPLES):
            samples.append(round(amplitude * math.sin(2 * math.pi * 440 * i / RATE)))
        if burst_index < len(pause_samples):
            samples.extend([0] * pause_samples[burst_index])
    samples.extend([0] * trailing_samples)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


def pcm_samples(wav_data: bytes) -> tuple[int, list[int]]:
    with wave.open(io.BytesIO(wav_data), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    return rate, list(struct.unpack(f"<{len(raw) // 2}h", raw))


def wav_from_samples(samples: list[int], *, rate: int = RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


def register_take(
    store,
    root: ProjectRoot,
    *,
    asset_id: str = "synthetic-take",
    data: bytes | None = None,
    voice_consent: str | None = "synthetic",
) -> Asset:
    content = take_bytes() if data is None else data
    relative = f"assets/audio/{asset_id}/take.wav"
    root.write_bytes(relative, content)
    fields: dict[str, Any] = {
        "asset_id": asset_id,
        "type": "voice",
        "generated": False,
        "license": "synthetic fixture",
        "model_or_voice_license": "synthetic fixture",
        "file_path": relative,
        "checksum": hashlib.sha256(content).hexdigest(),
        "reuse_scope": "project fixture",
    }
    if voice_consent is not None:
        fields["voice_consent"] = voice_consent
        if voice_consent != "synthetic":
            fields["voice_consent_basis"] = "Synthetic test fixture."
    asset = Asset.model_validate(fields)
    store.save(asset, expected_version=0)
    return asset


def gain_samples(source: list[int], gain_db: float) -> tuple[list[int], int]:
    multiplier = 10 ** (gain_db / 20.0)
    result: list[int] = []
    clamped = 0
    for sample in source:
        rounded = round(sample * multiplier)
        if rounded < -32768 or rounded > 32767:
            clamped += 1
        result.append(max(-32768, min(32767, rounded)))
    return result, clamped


def spans_for(report: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    assert isinstance(report.get("segment_map"), list), report
    return [span for span in report["segment_map"] if span.get("kind") == kind]


def assert_map_tiles_output(report: dict[str, Any], output_samples: list[int]) -> None:
    spans = report["segment_map"]
    cursor = 0
    for span in spans:
        assert span["out_start"] == cursor, span
        assert span["length"] > 0, span
        cursor += span["length"]
    assert cursor == len(output_samples)


def assert_source_spans_rebuild(
    report: dict[str, Any], source_samples: list[int], output_samples: list[int],
) -> list[dict[str, Any]]:
    source_spans = spans_for(report, "source")
    assert source_spans, report
    previous_source_end = 0
    for span in source_spans:
        src_start = span["src_start"]
        length = span["length"]
        assert src_start == previous_source_end, span
        source = source_samples[src_start : src_start + length]
        assert len(source) == length
        out = output_samples[span["out_start"] : span["out_start"] + length]
        if span["unchanged"]:
            assert span["gain_db"] == 0
            assert out == source
        else:
            expected, _ = gain_samples(source, span["gain_db"])
            assert out == expected
        previous_source_end = src_start + length
    assert previous_source_end == len(source_samples)
    return source_spans


def _output_arg(argv: list[str]) -> str:
    return argv[-1]


class FakeFFmpeg:
    """A fake FFmpeg that emits a known PCM breath, with optional length skew."""

    def __init__(self, *, breath_delta_samples: int = 0, fail_on_breath: bool = False) -> None:
        self.identity = FFMPEG_IDENTITY
        self.breath_delta_samples = breath_delta_samples
        self.fail_on_breath = fail_on_breath
        self.calls: list[dict[str, Any]] = []
        self.breath_samples: list[list[int]] = []
        self.hang_on_breath = False
        self.breath_started = threading.Event()
        self.hanging_processes: list[NeverFinishingFFmpegProcess] = []

    def popen(self, argv, **kwargs):
        if self.hang_on_breath and "anoisesrc=" in " ".join(str(value) for value in argv):
            process = NeverFinishingFFmpegProcess(argv, fake=self, **kwargs)
            self.hanging_processes.append(process)
            self.calls.append({"argv": process.argv, "kwargs": kwargs, "kind": "breath"})
            self.breath_started.set()
            return process
        process = FakeFFmpegProcess(argv, fake=self, **kwargs)
        self.calls.append({"argv": process.argv, "kwargs": kwargs, "kind": process.kind})
        return process


class FakeFFmpegProcess:
    def __init__(self, argv, *, fake: FakeFFmpeg, **kwargs) -> None:
        self.argv = [str(value) for value in argv]
        self.args = list(argv)
        self.kwargs = kwargs
        self.fake = fake
        joined = " ".join(self.argv)
        self.kind = "version" if "-version" in self.argv else "breath" if "anoisesrc=" in joined else "unexpected"
        if self.kind == "unexpected":
            raise AssertionError(f"unexpected FFmpeg invocation: {self.argv!r}")
        self.returncode = 0
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self._stdout_bytes = b""
        self._stderr_bytes = b""
        if self.kind == "version":
            self._stdout_bytes = fake.identity.encode("utf-8")
            self.stdout = io.BytesIO(self._stdout_bytes)
            return

        rate_match = re.search(r"sample_rate=(\d+)", joined)
        duration_match = re.search(r"duration=([0-9]+(?:\.[0-9]+)?)", joined)
        if rate_match is None or duration_match is None:
            raise AssertionError(f"breath command is missing sample rate or duration: {self.argv!r}")
        self.rate = int(rate_match.group(1))
        requested = round(float(duration_match.group(1)) * self.rate)
        actual = max(0, requested + fake.breath_delta_samples)
        pattern = (1000, -1200, 1400, -1600, 1800, -2000, 2200, -2400)
        self.samples = [pattern[i % len(pattern)] for i in range(actual)]
        fake.breath_samples.append(self.samples)
        self._stderr_bytes = b"fake breath synthesized\n"
        if fake.fail_on_breath:
            self.returncode = 1
            self._stderr_bytes = b"fake breath failure\n"
            self.stderr = io.BytesIO(self._stderr_bytes)
            return

        target = _output_arg(self.argv)
        if target in {"-", "pipe:1"}:
            self._stdout_bytes = struct.pack(f"<{len(self.samples)}h", *self.samples)
        else:
            path = Path(target)
            if not path.is_absolute():
                path = Path(kwargs.get("cwd", Path.cwd())) / path
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = struct.pack(f"<{len(self.samples)}h", *self.samples)
            output_format = None
            if "-f" in self.argv:
                output_format = self.argv[self.argv.index("-f") + 1]
            if output_format in {"s16le", "s16be"} or path.suffix.lower() in {".pcm", ".raw"}:
                path.write_bytes(raw)
            else:
                content = io.BytesIO()
                with wave.open(content, "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(self.rate)
                    writer.writeframes(raw)
                path.write_bytes(content.getvalue())
        self.stdout = io.BytesIO(self._stdout_bytes)
        self.stderr = io.BytesIO(self._stderr_bytes)

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
        stdout, stderr = self._stdout_bytes, self._stderr_bytes
        if hasattr(self.kwargs.get("stdout"), "write"):
            self._write_log(self.kwargs["stdout"], stdout or stderr)
            stdout = b""
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


class NeverFinishingFFmpegProcess:
    """Popen fake for cancellation and timeout coverage of breath synthesis."""

    def __init__(self, argv, *, fake: FakeFFmpeg, **kwargs) -> None:
        self.argv = [str(value) for value in argv]
        self.args = list(argv)
        self.kwargs = kwargs
        self.fake = fake
        self.returncode: int | None = None
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.killed = threading.Event()
        self.communicate_started = threading.Event()

    def communicate(self, input=None, timeout=None):
        self.communicate_started.set()
        wait_seconds = 5.0 if timeout is None else max(0.0, float(timeout))
        if not self.killed.wait(wait_seconds):
            if timeout is None:
                raise AssertionError("hung FFmpeg was not given a timeout or cancelled")
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return b"", b"fake hung FFmpeg killed\n"

    def wait(self, timeout=None):
        wait_seconds = 5.0 if timeout is None else max(0.0, float(timeout))
        if not self.killed.wait(wait_seconds):
            if timeout is None:
                raise AssertionError("hung FFmpeg was not given a timeout or cancelled")
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.killed.set()

    terminate = kill

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def perfedit_service(tmp_path: Path, perfedit_adapter_factory):
    projects = tmp_path / "projects"
    project_path = projects / "p1"
    env = Environment(project_path)
    env.store.save(Project(project_id="p1", title="PerformanceEdit service contract"), expected_version=0)
    env.store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30), expected_version=0)
    shot = Shot(shot_id="s1", episode_id="ep1", duration_sec=10)
    env.store.create(shot)
    fake = FakeFFmpeg()
    adapter = perfedit_adapter_factory(env.root, env.settings, fake.popen)
    lease_states: list[bool] = []
    cancel_check = [lambda: False]

    def set_cancel_check(check) -> None:
        cancel_check[0] = check or (lambda: False)

    class ObservedAdapter:
        def set_cancel_check(self, check) -> None:
            set_cancel_check(check)

        def invoke(self, request, job, *, queue):
            lease_states.append(bool(getattr(getattr(queue, "lease", None), "_owned", False)))
            return adapter.invoke(request, job, queue=queue, cancel=cancel_check[0])

        def close(self):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    env.registry.adapter = ObservedAdapter()
    settings = Settings.model_validate({"project_root": projects})
    from orchestrator.api.app import create_app

    app = create_app(settings=settings, token=TOKEN, job_runner=env.runner)
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield client, app, env, fake, lease_states, adapter
    env.runner.close()


def apply_body(store, source: Asset, **updates: Any) -> dict[str, Any]:
    _shot, version = store.load_with_version(Shot, "s1", episode_id="ep1")
    body: dict[str, Any] = {
        "expected_version": version,
        "take_asset_id": source.asset_id,
        "seed": 123,
        "pauses": [
            {"index": 0, "target_ms": 500, "breath": None},
            {"index": 1, "target_ms": 700, "breath": None},
        ],
    }
    body.update(updates)
    return body


def submit_and_wait(client: TestClient, env: Environment, body: dict[str, Any]):
    response = client.post(APPLY_PATH, json=body, headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 202, response.text
    return env.runner.wait("p1", response.json()["job_id"])


def test_breath_argv_seed_filter_chain_and_bitexact_settings(perfedit_service) -> None:
    client, _app, env, fake, _lease_states, _adapter = perfedit_service
    source = register_take(env.store, env.root)
    body = apply_body(
        env.store,
        source,
        pauses=[{"index": 0, "target_ms": 500, "breath": {"duration_ms": 100, "gain_db": -8}}],
        segment_gain_db=[0],
    )

    job = submit_and_wait(client, env, body)

    assert job.state == "completed", job.model_dump(mode="json")
    assert len(fake.breath_samples) == 1
    expected_count = round(100 * RATE / 1000)
    assert len(fake.breath_samples[0]) == expected_count
    breath_call = next(call for call in fake.calls if call["kind"] == "breath")
    argv = breath_call["argv"]
    joined = " ".join(argv)
    expected_seed = derive_seed(
        f"perfedit|{source.asset_id}", body["seed"], "breath0", INT32_MAX,
    )
    assert isinstance(argv, list)
    assert f"anoisesrc=color=brown:amplitude=0.9:seed={expected_seed}" in joined
    assert "sample_rate=22050" in joined
    # Contract section 5's chain continues ":sample_rate=" after the duration.
    assert re.search(r"duration=0\.1(?:0*)?(?::|\s|,|$)", joined)
    assert "highpass=f=380:p=2" in joined
    assert "lowpass=f=2100:p=2" in joined
    assert re.search(r"afade=t=in:st=0:d=0\.0620*:curve=exp", joined)
    assert re.search(r"afade=t=out:st=0\.0620*:d=0\.0380*:curve=qsin", joined)
    assert re.search(r"volume=-8(?:\.0+)?dB", joined)
    filter_order = [
        joined.index("anoisesrc=color=brown"), joined.index("highpass=f=380:p=2"),
        joined.index("lowpass=f=2100:p=2"), joined.index("afade=t=in:"),
        joined.index("afade=t=out:"), joined.index("volume="),
    ]
    assert filter_order == sorted(filter_order)
    assert "-threads" in argv and argv[argv.index("-threads") + 1] == "1"
    assert "-fflags" in argv and argv[argv.index("-fflags") + 1] == "+bitexact"
    assert "-flags" in argv and argv[argv.index("-flags") + 1] == "+bitexact"
    assert "-map_metadata" in argv and argv[argv.index("-map_metadata") + 1] == "-1"
    assert "pcm_s16le" in argv
    assert "-ac" in argv and argv[argv.index("-ac") + 1] == "1"
    assert "-ar" in argv and argv[argv.index("-ar") + 1] == str(RATE)
    assert breath_call["kwargs"].get("shell") is False
    assert _record_has_value(env, job.run_id, "breath_seeds", expected_seed)
    report = json.loads(env.root.read_text(f"generated/perfedit/s1/v0001/{job.run_id}/report.json", encoding="utf-8"))
    expected_identity = normalize_ffmpeg_identity(FFMPEG_IDENTITY)
    assert any(
        "ffmpeg" in key.lower() and "identity" in key.lower() and value == expected_identity
        for key, value in _walk_key_values(report)
    )


def _walk_key_values(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value
            yield from _walk_key_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_key_values(value)


def _record_has_value(env: Environment, run_id: str, key: str, value: Any) -> bool:
    report = json.loads(env.root.read_text(f"generated/perfedit/s1/v0001/{run_id}/report.json", encoding="utf-8"))
    # The contract fixes no report shape, so accept the value directly under a matching key
    # or anywhere inside it (e.g. {"breath_seeds": {"0": seed}}).
    return any(key.lower() in name.lower() and (item == value or value in _flatten_values(item))
               for name, item in _walk_key_values(report))


def _flatten_values(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [value for item in node.values() for value in _flatten_values(item)]
    if isinstance(node, list):
        return [value for item in node for value in _flatten_values(item)]
    return [node]


def test_edit_composition_gain_false_start_jitter_and_segment_map(perfedit_service) -> None:
    client, _app, env, fake, lease_states, _adapter = perfedit_service
    source_bytes = take_bytes(amplitude=20000)
    source = register_take(env.store, env.root, data=source_bytes)
    rate, source_samples = pcm_samples(source_bytes)
    assert rate == RATE
    gap_range = [-4.0, 4.0]
    gain_range = [-0.5, 0.5]
    seed = 177
    body = apply_body(
        env.store,
        source,
        seed=seed,
        pauses=[
            {"index": 0, "target_ms": 500, "breath": {"duration_ms": 150, "gain_db": -12}},
            {"index": 1, "target_ms": 700, "breath": None},
        ],
        segment_gain_db=[0, 6],
        false_start={"copy_ms": 100, "gain_db": -6, "gap_after_ms": 20},
        jitter={"gap_ms_range": gap_range, "gain_db_range": gain_range},
    )

    job = submit_and_wait(client, env, body)

    assert job.state == "completed", job.model_dump(mode="json")
    assert job.run_id
    assert len(lease_states) == 1 and lease_states[0] is False, "PerformanceEdit is a CPU adapter kind"
    run_dir = f"generated/perfedit/s1/v0001/{job.run_id}"
    take_path = f"{run_dir}/take.wav"
    report_path = f"{run_dir}/report.json"
    log_path = f"{run_dir}/logs/ffmpeg.log"
    assert env.root.resolve(take_path).is_file()
    assert env.root.resolve(report_path).is_file()
    assert env.root.resolve(log_path).is_file()
    output_bytes = env.root.read_bytes(take_path)
    output_rate, output_samples = pcm_samples(output_bytes)
    assert output_rate == RATE
    report = json.loads(env.root.read_text(report_path, encoding="utf-8"))
    assert report["algorithm_version"] == "1.0.0"
    assert_map_tiles_output(report, output_samples)

    source_spans = assert_source_spans_rebuild(report, source_samples, output_samples)
    assert len(source_spans) == 3
    natural_pauses = [30 * FRAME_SAMPLES, 40 * FRAME_SAMPLES]
    pause_starts = [
        10 * FRAME_SAMPLES + SPEECH_SAMPLES,
        10 * FRAME_SAMPLES + 2 * SPEECH_SAMPLES + natural_pauses[0],
    ]
    cuts = [
        (start + (start + length)) // 2
        for start, length in zip(pause_starts, natural_pauses)
    ]
    assert [span["src_start"] + span["length"] for span in source_spans[:-1]] == cuts
    assert [span["src_start"] for span in source_spans[1:]] == cuts
    assert any("detection" in key.lower() for key, _value in _walk_key_values(report))
    assert any("pause" in key.lower() for key, _value in _walk_key_values(report))
    assert any(key.lower() == "noise_db" and value == -38 for key, value in _walk_key_values(report))
    assert any(key.lower() == "min_pause_ms" and value == 120 for key, value in _walk_key_values(report))
    cut_fields = [value for key, value in _walk_key_values(report) if "cut" in key.lower()]
    assert cut_fields, report
    cut_values = _flatten_values(cut_fields)
    assert all(cut in cut_values for cut in cuts)
    # Contract section 4: segment i's gain is its entry plus jitter(f"seg{i}|gain"); the
    # "last entry applies to the rest" rule repeats the entry, not another segment's jitter.
    def seg_jitter(i: int) -> float:
        return jitter(f"perfedit|{source.asset_id}", seed, f"seg{i}|gain", *gain_range)

    assert source_spans[0]["gain_db"] == pytest.approx(0 + seg_jitter(0))
    assert source_spans[0]["unchanged"] is (seg_jitter(0) == 0)
    assert source_spans[1]["gain_db"] == pytest.approx(6 + seg_jitter(1))
    assert source_spans[2]["gain_db"] == pytest.approx(6 + seg_jitter(2))

    false_start = spans_for(report, "false_start")
    assert len(false_start) == 1
    fs = false_start[0]
    assert fs["gain_db"] == -6
    assert fs["unchanged"] is False
    onset = 10 * FRAME_SAMPLES
    copy_count = round(100 * RATE / 1000)
    assert fs["src_start"] == onset
    assert fs["length"] == copy_count
    fs_source = source_samples[onset : onset + copy_count]
    fs_output = output_samples[fs["out_start"] : fs["out_start"] + copy_count]
    false_start_gain = 10 ** (-6 / 20)
    fade_len = min(round(0.050 * RATE), round(copy_count / 2))
    fade_start = copy_count - fade_len
    expected_false_start: list[int] = []
    for index, sample in enumerate(fs_source):
        if index < fade_start:
            factor = 1.0
        else:
            j = index - fade_start
            factor = 1.0 - j / (fade_len - 1) if fade_len > 1 else 1.0
        expected_false_start.append(max(-32768, min(32767, round(sample * false_start_gain * factor))))
    assert fs_output == expected_false_start
    assert abs(fs_source[fade_start]) > 1000, "the fade begins on a meaningful source sample"
    assert fs_output[fade_start] == round(fs_source[fade_start] * false_start_gain)
    if fade_len > 1:
        assert fs_output[-1] == 0, "the final faded sample uses a zero multiplier"

    expected_jitter = {
        name: jitter(f"perfedit|{source.asset_id}", seed, name, *(gap_range if name.endswith("gap") else gain_range))
        for name in ("fs|gap", "pause0|gap", "pause1|gap", "seg0|gain", "seg1|gain", "seg2|gain")
    }
    for name, value in expected_jitter.items():
        assert any(key == name and found == pytest.approx(value) for key, found in _walk_key_values(report)), (name, value, report)

    # Every inserted silence is digital zero; lengths include the widened portion after any breath.
    silence_spans = spans_for(report, "silence")
    for span in silence_spans:
        start = span["out_start"]
        assert output_samples[start : start + span["length"]] == [0] * span["length"]
    first_breath = spans_for(report, "breath")[0]
    assert first_breath["length"] == round(150 * RATE / 1000)
    assert len(fake.breath_samples) == 1
    breath_out = output_samples[first_breath["out_start"] : first_breath["out_start"] + first_breath["length"]]
    assert breath_out == fake.breath_samples[0]

    expected_false_gap = round((20 + expected_jitter["fs|gap"]) * RATE / 1000)
    pause_gap_samples = []
    for index, (target_ms, natural_samples, breath_ms) in enumerate(
        ((500, natural_pauses[0], 150), (700, natural_pauses[1], 0))
    ):
        extra_ms = target_ms - natural_samples * 1000 / RATE + expected_jitter[f"pause{index}|gap"]
        if breath_ms:
            extra_ms -= breath_ms
        pause_gap_samples.append(round(extra_ms * RATE / 1000) if extra_ms > 0 else 0)
    assert [span["length"] for span in silence_spans] == [expected_false_gap, *pause_gap_samples]
    assert len(output_samples) == (
        len(source_samples) + copy_count + expected_false_gap
        + round(150 * RATE / 1000) + sum(pause_gap_samples)
    )

    expected_source_clamps = 0
    for span in source_spans:
        if not span["unchanged"]:
            _, count = gain_samples(source_samples[span["src_start"] : span["src_start"] + span["length"]], span["gain_db"])
            expected_source_clamps += count
    _, false_start_clamps = gain_samples(fs_source, -6)
    reported_clamps = next(
        value for key, value in _walk_key_values(report)
        if "clamp" in key.lower() and isinstance(value, int)
    )
    assert reported_clamps == expected_source_clamps + false_start_clamps
    assert reported_clamps > 0


def test_false_start_fade_length_one_uses_the_defined_unit_fade(perfedit_service) -> None:
    client, _app, env, _fake, _lease_states, _adapter = perfedit_service
    source_bytes = wav_from_samples([12345, -23456])
    source = register_take(env.store, env.root, data=source_bytes)
    _rate, source_samples = pcm_samples(source_bytes)
    body = apply_body(
        env.store,
        source,
        pauses=[],
        false_start={"copy_ms": 50, "gain_db": -6, "gap_after_ms": 0},
    )

    job = submit_and_wait(client, env, body)

    assert job.state == "completed", job.model_dump(mode="json")
    report = json.loads(env.root.read_text(
        f"generated/perfedit/s1/v0001/{job.run_id}/report.json", encoding="utf-8",
    ))
    output_rate, output_samples = pcm_samples(env.root.read_bytes(
        f"generated/perfedit/s1/v0001/{job.run_id}/take.wav",
    ))
    assert output_rate == RATE
    [false_start] = spans_for(report, "false_start")
    assert false_start["src_start"] == 0
    assert false_start["length"] == 2
    assert min(round(0.050 * RATE), round(false_start["length"] / 2)) == 1
    gain = 10 ** (-6 / 20)
    copied = output_samples[
        false_start["out_start"] : false_start["out_start"] + false_start["length"]
    ]
    assert copied == [round(sample * gain) for sample in source_samples]
    # F == 1 uses f_0 == 1, so the single faded sample is not forced to zero.
    assert copied[-1] == round(source_samples[-1] * gain)


@pytest.mark.parametrize("delta", [-7, 0, 7])
def test_breath_sample_count_is_trimmed_or_zero_padded(perfedit_service, delta) -> None:
    client, _app, env, fake, _lease_states, _adapter = perfedit_service
    fake.breath_delta_samples = delta
    source = register_take(env.store, env.root)
    body = apply_body(
        env.store,
        source,
        pauses=[{"index": 0, "target_ms": 500, "breath": {"duration_ms": 101, "gain_db": -10}}],
    )

    job = submit_and_wait(client, env, body)

    assert job.state == "completed", job.model_dump(mode="json")
    output_rate, output = pcm_samples(env.root.read_bytes(f"generated/perfedit/s1/v0001/{job.run_id}/take.wav"))
    assert output_rate == RATE
    report = json.loads(env.root.read_text(f"generated/perfedit/s1/v0001/{job.run_id}/report.json", encoding="utf-8"))
    breath_span = spans_for(report, "breath")[0]
    expected = round(101 * RATE / 1000)
    assert breath_span["length"] == expected
    synthesized = fake.breath_samples[0]
    rebuilt = synthesized[:expected] if len(synthesized) >= expected else synthesized + [0] * (expected - len(synthesized))
    assert output[breath_span["out_start"] : breath_span["out_start"] + expected] == rebuilt


def test_jitter_values_are_omitted_when_jitter_is_not_requested(perfedit_service) -> None:
    client, _app, env, _fake, _lease_states, _adapter = perfedit_service
    source = register_take(env.store, env.root)
    job = submit_and_wait(
        client,
        env,
        apply_body(
            env.store,
            source,
            pauses=[{"index": 0, "target_ms": 500, "breath": None}],
        ),
    )
    report = json.loads(env.root.read_text(f"generated/perfedit/s1/v0001/{job.run_id}/report.json", encoding="utf-8"))
    jitter_fields = [value for key, value in report.items() if "jitter" in key.lower()]
    assert not any(value for value in jitter_fields)

