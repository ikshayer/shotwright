"""Real synthetic FFmpeg preparation, independently decoded PCM/frame checks."""
import json
import math
import shutil
import struct
import subprocess
import uuid
from array import array
from pathlib import Path

import pytest

from orchestrator.adapters.qwen.core import QwenSetupError, WindowSpec
from orchestrator.adapters.qwen.runner import NativeBackend
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind

ROOT = Path(__file__).resolve().parents[3]


def command(argv):
    result = subprocess.run(argv, check=False, capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


@pytest.mark.parametrize("silent", [False, True])
def test_native_decoded_trim_preserves_pcm_stereo_rate_frames(silent):
    root = ProjectRoot(ROOT)
    prefix = "_work/qwen_contract_tests/preparation/" + uuid.uuid4().hex
    rel = prefix + "/source.mkv"
    expression = "0|0" if silent else "0.25*sin(2*PI*317*t)|-0.25*sin(2*PI*317*t)"
    root.write_with(rel, lambda path: command([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=320x240:rate=24:duration=3", "-f", "lavfi", "-i",
        f"aevalsrc={expression}:s=48000:d=3", "-c:v", "ffv1", "-c:a", "pcm_f32le",
        "-y", str(path),
    ]))
    backend = NativeBackend(root)
    info = backend.probe(rel, work_rel=prefix + "/probe", modality="joint")
    assert (info.sample_rate, info.channels, info.fps) == (48000, 2, 24)
    window = WindowSpec(1, 0.5, 1.5, 100.5, 101.5)
    prepared = backend.prepare(rel, window, work_rel=prefix + "/window", modality="joint")
    output = root.resolve(prepared.media_path)
    expected = command(["ffmpeg", "-nostdin", "-v", "error", "-i", str(root.resolve(rel)),
                        "-map", "0:a:0", "-af", "atrim=start_sample=24000:end_sample=72000",
                        "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"])
    actual = command(["ffmpeg", "-nostdin", "-v", "error", "-i", str(output), "-map", "0:a:0",
                      "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"])
    assert actual == expected, "native channels/amplitude/sample interval must survive exactly"
    assert len(actual) == 48000 * 2 * 4
    data = json.loads(command(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
                               "-of", "json", str(output)]))
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
    assert video["codec_name"] == "ffv1"
    assert int(video["nb_read_frames"]) == 24
    assert audio["sample_rate"] == "48000" and audio["channels"] == 2
    metrics = prepared.audio_metrics
    assert metrics["sample_rate"] == 48000 and metrics["channels"] == 2
    assert metrics["sample_count"] == 48000
    assert metrics["digital_silence"] is silent
    assert len(prepared.media_sha256) == 64
    json.dumps(metrics, allow_nan=False)
    values = array("f")
    values.frombytes(actual)
    assert all(values[index] == -values[index + 1] for index in range(0, len(values), 2))


def test_nonfinite_native_pcm_is_corrupt_not_inference():
    root = ProjectRoot(ROOT)
    prefix = "_work/qwen_contract_tests/nonfinite/" + uuid.uuid4().hex
    payload = struct.pack("<" + "f" * 48000, *([math.nan] + [0.0] * 47999))
    fmt = struct.pack("<HHIIHH", 3, 1, 48000, 192000, 4, 32)
    wave = b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVEfmt " + struct.pack("<I", 16) + fmt
    wave += b"data" + struct.pack("<I", len(payload)) + payload
    rel = prefix + "/nonfinite.wav"
    root.write_bytes(rel, wave)
    backend = NativeBackend(root)
    with pytest.raises(JobError) as error:
        backend.prepare(rel, WindowSpec(1, 0, 1, 0, 1), work_rel=prefix + "/window", modality="audio")
    assert error.value.kind == JobErrorKind.CORRUPT_ARTIFACT


def test_joint_probe_rejects_material_start_misalignment():
    root = ProjectRoot(ROOT)
    prefix = "_work/qwen_contract_tests/misalignment/" + uuid.uuid4().hex
    rel = prefix + "/source.mkv"
    root.write_with(rel, lambda path: command([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=320x240:rate=24:duration=2", "-f", "lavfi", "-i",
        "anullsrc=r=48000:cl=stereo:d=2", "-af", "asetpts=PTS+0.01/TB",
        "-c:v", "ffv1", "-c:a", "pcm_f32le", "-y", str(path),
    ]))
    with pytest.raises((ValueError, RuntimeError)) as error:
        NativeBackend(root).probe(rel, work_rel=prefix + "/probe", modality="joint")
    assert any(word in str(error.value).lower() for word in ("align", "start", "sync"))


def test_native_prepare_checks_disk_before_ffmpeg(monkeypatch):
    root = ProjectRoot(ROOT)
    prefix = "_work/qwen_contract_tests/lowdisk/" + uuid.uuid4().hex
    rel = prefix + "/source.wav"
    fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
    pcm = b"\0" * 96000
    wav = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<I", 16) + fmt
    root.write_bytes(rel, wav + b"data" + struct.pack("<I", len(pcm)) + pcm)
    usage = shutil.disk_usage(ROOT)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type(usage)(usage.total, usage.total - 1024, 1024))
    with pytest.raises(QwenSetupError, match="(?i)disk"):
        NativeBackend(root).prepare(rel, WindowSpec(1, 0, 1, 0, 1), work_rel=prefix + "/window", modality="audio")


def test_common_nonzero_stream_origin_is_normalized_before_trim():
    root = ProjectRoot(ROOT)
    prefix = "_work/qwen_contract_tests/commonorigin/" + uuid.uuid4().hex
    rel = prefix + "/source.mkv"
    root.write_with(rel, lambda path: command([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=320x240:rate=24:duration=3", "-f", "lavfi", "-i",
        "aevalsrc=0.25*sin(2*PI*317*t):s=48000:d=3", "-vf", "setpts=PTS+5/TB",
        "-af", "asetpts=PTS+5/TB", "-c:v", "ffv1", "-c:a", "pcm_f32le", "-y", str(path),
    ]))
    original = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(root.resolve(rel))]))
    assert all(float(s["start_time"]) == 5 for s in original["streams"])
    backend = NativeBackend(root)
    info = backend.probe(rel, work_rel=prefix + "/probe", modality="joint")
    assert info.duration_seconds == pytest.approx(3, abs=1/48000)
    prepared = backend.prepare(rel, WindowSpec(1, .5, 1.5, .5, 1.5), work_rel=prefix + "/window", modality="joint")
    actual = command(["ffmpeg", "-nostdin", "-v", "error", "-i", str(root.resolve(prepared.media_path)),
                      "-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"])
    expected = command(["ffmpeg", "-nostdin", "-v", "error", "-i", str(root.resolve(rel)),
                        "-map", "0:a:0", "-af", "atrim=start_sample=24000:end_sample=72000",
                        "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"])
    assert actual == expected
