"""CPU-only real FFmpeg acceptance tests; no model/server success claim."""

import hashlib
import json
import os
import socket
import subprocess

import pytest

from orchestrator.adapters.minimax_h3.core import H3Request
from orchestrator.adapters.minimax_h3.native import NativeBackend
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.platform.settings import Settings


@pytest.fixture
def root(tmp_path):
    return ProjectRoot(tmp_path)


def run(root, args):
    temp = str(root.make_dir("_work/temp"))
    env = dict(os.environ, TEMP=temp, TMP=temp, TMPDIR=temp)
    result = subprocess.run(args, check=False, capture_output=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def source(root, *, seconds=2, shifted=False):
    path = root.resolve("source.mp4")
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=160x90:r=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        str(seconds),
    ]
    if shifted:
        args += ["-output_ts_offset", "5"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)]
    run(root, args)
    return path


def request(root, path, **changes):
    data = {
        "shot_id": "s1",
        "shot_version": 1,
        "run_id": "r1",
        "video_path": path.relative_to(root.path).as_posix(),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_output_id": "o1",
        "prompt": "room",
        "duration_seconds": 1,
        "experimental": True,
    }
    data.update(changes)
    return H3Request(**data)


def probe(root, path):
    return json.loads(
        run(
            root,
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
        )
    )


def test_prepare_real_trim_sar_frames_strips_audio_and_contains(root):
    src = source(root)
    backend = NativeBackend(root, Settings())
    req = request(root, src, start_seconds=0.5)
    receipt = backend.prepare_video(req, run_rel="_work/prepared")
    path = root.resolve(receipt["path"])
    assert path == root.resolve("_work/prepared/input/model_proxy.mp4")
    streams = probe(root, path)["streams"]
    assert len(streams) == 1 and streams[0]["codec_type"] == "video"
    video = streams[0]
    assert (video["width"], video["height"]) == (384, 288)
    assert video["sample_aspect_ratio"] == "1:1"
    assert video["r_frame_rate"] == "24/1" and int(video["nb_read_frames"]) == 24
    rgb = run(
        root,
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
    )
    assert max(rgb[: 384 * 3]) < 20, "Letterbox top row must be black, not stretched red"
    center = (144 * 384 + 192) * 3
    assert rgb[center] > 180 and rgb[center + 1] < 30
    assert receipt["source_sha256"] == req.source_sha256
    assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("seconds,start", [(0.5, 0), (2, 1.5)])
def test_prepare_rejects_short_interval_without_padding(root, seconds, start):
    src = source(root, seconds=seconds)
    with pytest.raises((ValueError, JobError)):
        NativeBackend(root, Settings()).prepare_video(
            request(root, src, start_seconds=start), run_rel="_work/short"
        )


def test_prepare_normalizes_nonzero_media_origin(root):
    src = source(root, shifted=True)
    assert float(probe(root, src)["format"]["start_time"]) >= 4.9
    receipt = NativeBackend(root, Settings()).prepare_video(
        request(root, src), run_rel="_work/shift"
    )
    output = probe(root, root.resolve(receipt["path"]))
    assert int(output["streams"][0]["nb_read_frames"]) == 24
    assert abs(float(output["format"]["start_time"])) < 0.1


def test_prepare_rejects_playlist_disguised_as_video_and_hash_change(root):
    src = source(root)
    with socket.socket() as sentry:
        sentry.bind(("127.0.0.1", 0))
        sentry.listen(1)
        sentry.settimeout(0.2)
        port = sentry.getsockname()[1]
        playlist = root.write_text(
            "playlist.mp4", f"#EXTM3U\nhttp://127.0.0.1:{port}/forbidden.ts\n"
        )
        with pytest.raises((ValueError, JobError)):
            NativeBackend(root, Settings()).prepare_video(
                request(root, playlist), run_rel="_work/playlist"
            )
        with pytest.raises(TimeoutError):
            sentry.accept()
    req = request(root, src)
    root.write_bytes("source.mp4", src.read_bytes() + b"mutation")
    with pytest.raises((ValueError, JobError)):
        NativeBackend(root, Settings()).prepare_video(req, run_rel="_work/changed")


def test_export_decoded_aac_pcm_exact_no_gain_and_corrupt_rejection(root):
    path = root.resolve("candidate.mp4")
    run(
        root,
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=384x288:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=337:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(path),
        ],
    )
    req = request(root, path)
    receipt = NativeBackend(root, Settings()).verify_and_export(
        req, run_rel="_work/export", av_path="candidate.mp4"
    )
    original = run(
        root,
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "f32le",
            "-c:a",
            "pcm_f32le",
            "-",
        ],
    )
    wav = root.resolve(receipt["wav_path"])
    extracted = run(
        root, ["ffmpeg", "-v", "error", "-i", str(wav), "-f", "f32le", "-c:a", "pcm_f32le", "-"]
    )
    assert extracted == original
    assert receipt["audio_sample_rate"] == 48000 and receipt["audio_channels"] == 1
    assert receipt["audio_samples_per_channel"] == len(original) // 4
    assert receipt["audio_metrics"]
    root.write_bytes("corrupt.mp4", b"not a video")
    with pytest.raises((ValueError, JobError)):
        NativeBackend(root, Settings()).verify_and_export(
            req, run_rel="_work/bad", av_path="corrupt.mp4"
        )


@pytest.mark.parametrize("run_rel", ["../outside", "C:/outside", "//server/share/run"])
def test_cpu_media_seams_refuse_external_write_roots(root, run_rel):
    src = source(root)
    req = request(root, src)
    backend = NativeBackend(root, Settings())
    with pytest.raises(ValueError):
        backend.prepare_video(req, run_rel=run_rel)
    with pytest.raises(ValueError):
        backend.verify_and_export(req, run_rel=run_rel, av_path="source.mp4")


def test_cpu_media_rejects_wrong_output_frame_count(root):
    src = source(root, seconds=2)
    with pytest.raises((ValueError, JobError)):
        NativeBackend(root, Settings()).verify_and_export(
            request(root, src), run_rel="_work/wrong_frames", av_path="source.mp4"
        )
