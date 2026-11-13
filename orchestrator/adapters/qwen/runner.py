"""Contained local Qwen helper adapter; never imported by the production job registry."""
from __future__ import annotations

import array
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from orchestrator.adapters.qwen.core import (
    AnalysisRequest,
    BatchOutcome,
    MediaInfo,
    PreparedWindow,
    QwenRequestError,
    QwenSetupError,
    WindowSpec,
    build_prompt,
    coverage_gaps,
    plan_windows,
    render_markdown,
    validate_request,
)
from orchestrator.jobs.gpu_slot import GpuExecutionSlot, GpuSlotBusy
from orchestrator.jobs.lease import pid_is_alive
from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind

ROOT = Path(__file__).resolve().parents[3]
SPIKE = "spikes/23_joint_av_analyzer"
BASE = f"{SPIKE}/_work"
LIMITATIONS = [
    "Advisory text only; every Qwen response is unverified and requires creator review.",
    "The helper lock coordinates helper invocations only; other GPU applications are not serialized.",
    "The production lease precheck has a race with a production job starting immediately afterward.",
    "Source audio remains native-rate PCM; joint video is decoded and scaled to at most 640 pixels wide.",
]


class Backend(Protocol):
    def probe(self, media_rel: str, *, work_rel: str, modality: str) -> MediaInfo: ...
    def prepare(self, media_rel: str, window: WindowSpec, *, work_rel: str, modality: str) -> PreparedWindow: ...
    def infer_batch(self, prepared: Sequence[PreparedWindow], windows: Sequence[WindowSpec], *, prompt: str,
                    max_new_tokens: int, modality: str, work_rel: str) -> BatchOutcome: ...


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _contained_env(root: ProjectRoot, work_rel: str) -> dict[str, str]:
    temp = root.make_dir(f"{work_rel}/temp")
    cache = root.make_dir(f"{work_rel}/cache")
    env = os.environ.copy()
    env.update(TMP=str(temp), TEMP=str(temp), TMPDIR=str(temp), XDG_CACHE_HOME=str(cache),
               PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    return env


def _run(command: list[str], *, root: ProjectRoot, work_rel: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    command = _local_protocol_command(command)
    process = subprocess.Popen(command, cwd=root.path, env=_contained_env(root, work_rel), text=True,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        process.kill()
        try:
            process.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"contained subprocess did not exit after kill: {command[0]}") from exc
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command!r}: {result.stderr[-3000:]}")
    return result


def _require_disk_space(root: ProjectRoot) -> int:
    free = shutil.disk_usage(root.path).free
    if free < 8 * 1024**3:
        raise QwenSetupError(f"Qwen helper requires at least 8 GiB free disk; observed {free} bytes")
    return free


def _run_bytes(command: list[str], *, root: ProjectRoot, work_rel: str, timeout: int = 900) -> bytes:
    command = _local_protocol_command(command)
    process = subprocess.Popen(command, cwd=root.path, env=_contained_env(root, work_rel),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        process.kill()
        try:
            process.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"contained subprocess did not exit after kill: {command[0]}") from exc
        raise
    if process.returncode:
        raise RuntimeError(f"command failed ({process.returncode}): {command!r}: {stderr[-3000:].decode('utf-8', 'replace')}")
    return stdout


def _local_protocol_command(command: list[str]) -> list[str]:
    """Block URL/network protocols even when a local playlist references one."""
    result = list(command)
    if "-i" in result:
        position = result.index("-i")
        result[position:position] = ["-protocol_whitelist", "file,pipe"]
    elif Path(result[0]).name.lower().startswith("ffprobe"):
        result[1:1] = ["-protocol_whitelist", "file,pipe"]
    return result


def _metrics(pcm: bytes, sample_rate: int, channels: int) -> dict:
    samples = array.array("f")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) % channels:
        raise ValueError("decoded PCM byte count does not contain complete interleaved frames")
    count = len(samples) // channels
    if not count:
        raise ValueError("decoded PCM contains no samples")
    sums = [0.0] * channels
    squares = [0.0] * channels
    peaks = [0.0] * channels
    quiet = 0
    full = 0
    for frame in range(count):
        for channel in range(channels):
            value = float(samples[frame * channels + channel])
            if not math.isfinite(value):
                raise ValueError("decoded PCM contains a non-finite sample")
            sums[channel] += value
            squares[channel] += value * value
            peaks[channel] = max(peaks[channel], abs(value))
            if abs(value) < 0.001:
                quiet += 1
            if abs(value) >= 1.0:
                full += 1
    means = [value / count for value in sums]
    rms = [math.sqrt(value / count) for value in squares]
    to_db = lambda value: None if value == 0 else 20 * math.log10(value)
    correlation = None
    if channels == 2:
        left = [float(samples[i * 2]) for i in range(count)]
        right = [float(samples[i * 2 + 1]) for i in range(count)]
        ml, mr = means
        cov = sum((a - ml) * (b - mr) for a, b in zip(left, right, strict=True))
        vl = sum((a - ml) ** 2 for a in left)
        vr = sum((b - mr) ** 2 for b in right)
        if vl > 0 and vr > 0:
            correlation = cov / math.sqrt(vl * vr)
    return {"sample_rate": sample_rate, "channels": channels, "sample_count": count,
            "sample_count_per_channel": count,
            "channel_rms_dbfs": [to_db(value) for value in rms], "channel_peak_dbfs": [to_db(value) for value in peaks],
            "channel_mean_dc": means, "digital_silence": all(value == 0 for value in peaks),
            "samples_at_or_above_full_scale": full, "quiet_sample_fraction_below_0_001": quiet / (count * channels),
            "stereo_correlation": correlation}


def _validate_native_result(item: dict, *, prompt: str, cap: int, modality: str, duration: float) -> None:
    if not isinstance(item, dict):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native worker result must be an object")
    required = {"text", "prompt", "output_token_ids", "output_token_count", "termination_reason",
                "token_cap_reached", "effective_generation_kwargs", "decoded_media", "tensor_shapes",
                "loading_info", "quantized_linear4bit_count", "quantized_linear4bit_sha256",
                "unquantized_linear_count", "unquantized_linear_sha256", "model_provenance", "elapsed_seconds"}
    if not required.issubset(item):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"native worker result is missing fields: {sorted(required - item.keys())}")
    if not isinstance(item["text"], str) or not item["text"].strip() or item["prompt"] != prompt:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native worker response or effective prompt is invalid")
    ids, count = item["output_token_ids"], item["output_token_count"]
    if not isinstance(ids, list) or type(count) is not int or count != len(ids) or not 1 <= count <= cap:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native output token protocol is invalid")
    if any(type(token) is not int or token < 0 for token in ids):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native output token IDs are invalid")
    if type(item["token_cap_reached"]) is not bool:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native token cap flag is invalid")
    termination = "eos" if 151645 in ids else ("token_cap" if count == cap else "other")
    if termination == "other" or (151645 in ids and ids[-1] != 151645) or item["termination_reason"] != termination:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native EOS/termination protocol is invalid")
    if item["token_cap_reached"] is not (termination == "token_cap"):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native token cap flag disagrees with termination")
    kwargs = item["effective_generation_kwargs"]
    if (not isinstance(kwargs, dict) or kwargs.get("do_sample") is not False
            or kwargs.get("max_new_tokens") != cap or kwargs.get("eos_token_id") != 151645
            or kwargs.get("pad_token_id") != 151643 or kwargs.get("use_audio_in_video") is not (modality == "joint")):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native effective generation settings do not match request")
    loading = item["loading_info"]
    if not isinstance(loading, dict) or any(loading.get(key) for key in ("missing_keys", "unexpected_keys", "mismatched_keys")):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native model loading reported missing or incompatible weights")
    if item["quantized_linear4bit_count"] != 196 or item["unquantized_linear_count"] != 420:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native quantized module coverage differs from verified runtime")
    for key in ("quantized_linear4bit_sha256", "unquantized_linear_sha256"):
        if not isinstance(item[key], str) or len(item[key]) != 64:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"native {key} is invalid")
    if not isinstance(item["model_provenance"], dict) or not item["model_provenance"]:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native model provenance is absent")
    if not isinstance(item["decoded_media"], dict) or not isinstance(item["tensor_shapes"], dict):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native decoded media or tensor metadata is absent")
    decoded, shapes = item["decoded_media"], item["tensor_shapes"]
    try:
        audio_duration = decoded["audio_duration_seconds"]
        audio_samples = decoded["audio_samples"]
        if (not math.isfinite(audio_duration) or not 0 < audio_duration <= duration + 1 / 16000
                or type(audio_samples) is not int or audio_samples <= 0
                or abs(audio_duration - audio_samples / decoded["audio_sample_rate_hz"]) > 1 / 16000):
            raise ValueError("audio sample interval differs from prepared window")
        if modality == "joint" and abs(decoded["audio_first_pts_seconds"] - decoded["video_first_pts_seconds"]) > 0.001:
            raise ValueError("audio/video decoded starts are misaligned")
    except (KeyError, TypeError, ValueError) as exc:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"native decoded audio metadata is invalid: {exc}", cause=exc) from exc
    required_shapes = {"input_ids", "attention_mask", "input_features", "feature_attention_mask"}
    if modality == "joint":
        required_shapes.update({"pixel_values_videos", "video_grid_thw", "video_second_per_grid"})
        if type(decoded.get("video_frames_sampled")) is not int or decoded["video_frames_sampled"] <= 0:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "joint result has no sampled video frames")
    elif decoded.get("video_frames_sampled", 0) != 0 or any(key.startswith("pixel_values_videos") for key in shapes):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "audio-only result unexpectedly contains video tensors")
    if not required_shapes.issubset(shapes) or any(not isinstance(shapes[key], list) or not shapes[key]
                                                   or any(type(dim) is not int or dim <= 0 for dim in shapes[key])
                                                   for key in required_shapes):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native tensor shapes do not match the requested modality")
    if type(item["elapsed_seconds"]) not in (int, float) or not math.isfinite(item["elapsed_seconds"]) or item["elapsed_seconds"] < 0:
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "native elapsed time is invalid")


class NativeBackend:
    """FFmpeg decoded preparation plus the spike's isolated supervisor and original worker."""
    def __init__(self, root: ProjectRoot):
        self.root = root
        self.spike_path = root.resolve(f"{SPIKE}/analyze.py")
        self._analyzer = None

    def _module(self):
        if self._analyzer is None:
            spec = importlib.util.spec_from_file_location("qwen_spike_analyze", self.spike_path)
            if spec is None or spec.loader is None:
                raise QwenSetupError("spike analyzer cannot be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            self._analyzer = module
        return self._analyzer

    def probe(self, media_rel: str, *, work_rel: str, modality: str) -> MediaInfo:
        source = self.root.resolve(media_rel)
        if not source.is_file():
            raise QwenRequestError(f"media file does not exist: {media_rel}")
        result = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
                      root=self.root, work_rel=work_rel)
        data = json.loads(result.stdout)
        probe_command = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)]
        self.last_probe = {"command": _local_protocol_command(probe_command), "exit_code": result.returncode,
                           "stderr": result.stderr,
                           "version": _run(["ffprobe", "-version"], root=self.root, work_rel=work_rel).stdout.splitlines()[0],
                           "streams_format": data}
        streams = data.get("streams", [])
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if audio is None:
            raise QwenRequestError("input must contain an audio stream")
        if modality == "joint" and video is None:
            raise QwenRequestError("joint modality requires an audio and video stream")
        fmt = data.get("format", {})
        fmt_start = float(fmt.get("start_time") or 0)
        duration_candidates = [float(stream["duration"]) for stream in streams
                               if stream.get("duration") not in (None, "N/A")]
        duration = max(duration_candidates, default=float(fmt.get("duration") or 0) - fmt_start)
        rate, channels = int(audio.get("sample_rate") or 0), int(audio.get("channels") or 0)
        if not math.isfinite(duration) or duration <= 0 or not 1 <= rate <= 192000 or not 1 <= channels <= 8:
            raise QwenRequestError("media duration/sample rate/channel count is outside supported limits")
        astart = float(audio.get("start_time") or fmt_start)
        vstart = float(video.get("start_time") or fmt_start) if video else None
        if vstart is not None and abs(astart - vstart) > 0.001:
            raise QwenRequestError("audio/video stream starts differ by more than 1 ms; synchronize the source first")
        fps = None
        width = height = None
        if video:
            width, height = int(video.get("width") or 0), int(video.get("height") or 0)
            rawfps = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
            n, d = rawfps.split("/", 1)
            fps = float(n) / float(d)
            if width <= 0 or height <= 0 or not math.isfinite(fps) or fps <= 0:
                raise QwenRequestError("video dimensions and frame rate must be positive")
        return MediaInfo(duration, rate, channels, width, height, fps, astart, vstart)

    def prepare(self, media_rel: str, window: WindowSpec, *, work_rel: str, modality: str) -> PreparedWindow:
        src = self.root.resolve(media_rel)
        _require_disk_space(self.root)
        out_rel = f"{work_rel}/prepared/window_{window.index:03d}.mkv"
        out = self.root.resolve(out_rel)
        info = self.probe(media_rel, work_rel=work_rel, modality=modality)
        first = max(0, math.floor(window.start_seconds * info.sample_rate))
        last = first + max(1, math.floor(window.duration_seconds * info.sample_rate))
        audio_filter = f"atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS"
        if modality == "audio":
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(src), "-map", "0:a:0",
                       "-af", audio_filter, "-c:a", "pcm_f32le", "-avoid_negative_ts", "make_zero", str(out)]
        else:
            # FFmpeg's default input timestamp normalization makes the common
            # container origin zero before the trim filter.
            video_start = window.start_seconds
            vf = (f"trim=start={video_start:.9f}:duration={window.duration_seconds:.9f},"
                  "setpts=PTS-STARTPTS,scale='min(640,iw)':-2:flags=lanczos,format=yuv420p")
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(src), "-filter_complex",
                       f"[0:v:0]{vf}[v];[0:a:0]{audio_filter}[a]", "-map", "[v]", "-map", "[a]",
                       "-c:v", "ffv1", "-fps_mode", "passthrough", "-c:a", "pcm_f32le", str(out)]
        prepared_process: list[subprocess.CompletedProcess[str]] = []
        def run_prepare(_: Path) -> None:
            prepared_process.append(_run(command, root=self.root, work_rel=work_rel))
        self.root.write_with(out_rel, run_prepare)
        proc = prepared_process[0]
        # Capture native float PCM as bytes for objective metrics.
        pcm = _run_bytes(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(out), "-map", "0:a:0",
                          "-f", "f32le", "-acodec", "pcm_f32le", "-"], root=self.root, work_rel=work_rel)
        try:
            metrics = _metrics(pcm, info.sample_rate, info.channels)
        except (ValueError, OverflowError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"prepared native PCM is corrupt: {exc}", cause=exc) from exc
        prepared_probe = _run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(out)],
                              root=self.root, work_rel=work_rel)
        probe_data = json.loads(prepared_probe.stdout)
        out_audio = next((stream for stream in probe_data.get("streams", []) if stream.get("codec_type") == "audio"), None)
        out_video = next((stream for stream in probe_data.get("streams", []) if stream.get("codec_type") == "video"), None)
        if (out_audio is None or int(out_audio.get("sample_rate", 0)) != info.sample_rate
                or int(out_audio.get("channels", 0)) != info.channels
                or metrics["sample_count"] != last - first
                or metrics["sample_count"] / info.sample_rate > window.duration_seconds + 1 / info.sample_rate):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "prepared PCM changed rate/channels or exceeded the requested sample interval")
        required_frames = max(4, 2 * math.ceil(window.duration_seconds * 0.4))
        if modality == "joint" and (out_video is None or int(out_video.get("nb_read_frames") or 0) < required_frames):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"prepared joint window has fewer than {required_frames} decoder-budget frames")
        prep = {"sample_interval": [first, last], "requested_interval_seconds": [window.start_seconds, window.end_seconds],
                "ffmpeg_command": command, "ffmpeg_version": _run(["ffmpeg", "-version"], root=self.root, work_rel=work_rel).stdout.splitlines()[0],
                "ffprobe_command": ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(out)],
                "ffmpeg_exit_code": proc.returncode, "ffmpeg_stdout": proc.stdout,
                "ffmpeg_stderr": proc.stderr, "ffprobe": probe_data,
                "source_sha256": _sha(src), "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                "native_sample_rate": info.sample_rate, "native_channels": info.channels,
                "pcm_decode_command": ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(out), "-map", "0:a:0", "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                "pcm_decode_stderr": "",
                "native_pcm": True, "video_codec": "ffv1" if modality == "joint" else None}
        return PreparedWindow(out_rel, _sha(out), metrics, prep)

    def infer_batch(self, prepared: Sequence[PreparedWindow], windows: Sequence[WindowSpec], *, prompt: str,
                    max_new_tokens: int, modality: str, work_rel: str) -> BatchOutcome:
        analyze = self._module()
        run_id = f"batch_{windows[0].index:03d}_{uuid.uuid4().hex[:8]}"
        result_rel = f"{work_rel}/batches/{run_id}_worker_result.json"
        stdout_rel, stderr_rel = f"{work_rel}/batches/{run_id}_stdout.log", f"{work_rel}/batches/{run_id}_stderr.log"
        resources_rel = f"{work_rel}/batches/{run_id}_resources.jsonl"
        module = analyze
        module.BASE = BASE
        items = [{"media": str(self.root.resolve(item.media_path)), "start": spec.source_start_seconds,
                  "media_sha256": item.media_sha256} for item, spec in zip(prepared, windows, strict=True)]
        try:
            output, telemetry = analyze.run_supervised(items, modality, max_new_tokens,
                        max(spec.duration_seconds for spec in windows), result_rel, stdout_rel, stderr_rel,
                        resources_rel, prompt)
        except BaseException as interruption:  # noqa: BLE001 - supervisor has completed its cleanup path
            output = {}
            result_path = self.root.resolve(result_rel)
            if result_path.is_file():
                output = json.loads(self.root.read_text(result_rel, encoding="utf-8"))
            resource_path = self.root.resolve(resources_rel)
            records = []
            if resource_path.is_file():
                records = [json.loads(line) for line in self.root.read_text(resources_rel, encoding="utf-8").splitlines() if line.strip()]
            failure_record = next((record for record in reversed(records) if record.get("event") == "supervisor_failure"), {})
            identities = failure_record.get("owned_process_identities", [])
            native_cleanup = failure_record.get("owned_process_tree_cleanup")
            if isinstance(native_cleanup, dict):
                cleanup = {**native_cleanup,
                           "verified_stopped": native_cleanup.get("verified_stopped") is True
                               and not native_cleanup.get("survivors")
                               and not native_cleanup.get("unverifiable_identities")
                               and not native_cleanup.get("failures")
                               and not failure_record.get("cleanup_error")}
            else:
                cleanup = {"verified_stopped": False, "survivors": [],
                           "unverifiable_identities": identities, "failures": ["native cleanup proof missing"],
                           "native_cleanup": native_cleanup}
            telemetry = {"owned_process_tree_cleanup": cleanup, "supervisor_failure": failure_record,
                         "native_artifacts": []}
            for rel in (result_rel, stdout_rel, stderr_rel, resources_rel):
                path = self.root.resolve(rel)
                if path.is_file():
                    telemetry["native_artifacts"].append({"path": rel, "sha256": _sha(path)})
            cancelled = isinstance(interruption, KeyboardInterrupt)
            kind = JobErrorKind.CANCELLED if cancelled else JobError.from_exception(interruption, origin="adapter").kind
            error = {"family": "job", "kind": kind.value,
                     "message": "native inference interrupted after supervisor cleanup" if cancelled else str(interruption),
                     "retryable": kind.retryable, "remediation": kind.remediation,
                     "traceback": __import__("traceback").format_exc()}
            return BatchOutcome(tuple(output.get("results", [])), telemetry,
                                "cancelled" if cancelled else "failure", error)
        cleanup = telemetry.get("owned_process_tree_cleanup")
        cleanup_ok = (isinstance(cleanup, dict) and cleanup.get("verified_stopped") is True
                      and not cleanup.get("survivors") and not cleanup.get("unverifiable_identities")
                      and not cleanup.get("failures"))
        results = tuple(output.get("results", []))
        status = output.get("status", "failure")
        if not cleanup_ok:
            status = "failure"
        for rel in (result_rel, stdout_rel, stderr_rel, resources_rel):
            path = self.root.resolve(rel)
            if path.is_file():
                telemetry.setdefault("native_artifacts", []).append({"path": rel, "sha256": _sha(path)})
        failure_text = str(output.get("error", ""))
        failure_type = str(output.get("error_type", ""))
        error_kind = JobErrorKind.INTERNAL
        if "out of memory" in failure_text.lower() or failure_type in {"OutOfMemoryError", "MemoryError"}:
            error_kind = JobErrorKind.OUT_OF_MEMORY
        elif "missing model" in failure_text.lower() or failure_type == "FileNotFoundError":
            error_kind = JobErrorKind.MISSING_MODEL_ARTIFACT
        elif "disk" in failure_text.lower() and "space" in failure_text.lower():
            error_kind = JobErrorKind.DISK_EXHAUSTED
        elif "900 second" in failure_text.lower() or "available ram below" in failure_text.lower():
            error_kind = JobErrorKind.BUDGET_EXCEEDED
        if not cleanup_ok:
            error_kind = JobErrorKind.WORKER_UNREACHABLE
        error = None if status == "success" else {"family": "job", "kind": error_kind.value,
                "message": output.get("error", "native worker failure or cleanup is unverified"),
                "retryable": error_kind.retryable, "remediation": error_kind.remediation,
                "traceback": output.get("traceback"), "cleanup": cleanup}
        return BatchOutcome(results, {"native": telemetry, "owned_process_tree_cleanup": cleanup, "native_status": status,
                    "worker_result_path": result_rel, "stdout_path": stdout_rel,
                    "stderr_path": stderr_rel, "resources_path": resources_rel}, status, error)


class _Lock:
    def __init__(self, root: ProjectRoot, execution_slot: GpuExecutionSlot | None = None):
        self.root = root
        self.execution_slot = execution_slot
        self.file = None
    def __enter__(self):
        path = self.root.write_with(f"{BASE}/helper.lock", lambda p: p.touch(exist_ok=True))
        self.file = path.open("r+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close(); self.file = None
            raise QwenSetupError("another Qwen helper invocation holds the helper lock") from exc
        if self.execution_slot is not None:
            try:
                self.execution_slot.acquire(0)
            except GpuSlotBusy as exc:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
                self.file.close(); self.file = None
                raise QwenSetupError("shared GPU execution slot is busy") from exc
            except BaseException:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
                self.file.close(); self.file = None
                raise
        return self
    def __exit__(self, *_):
        if self.execution_slot is not None:
            self.execution_slot.release()
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class QwenAdapter:
    def __init__(self, root: ProjectRoot | None = None, *, backend: Backend | None = None):
        self.root = root or ProjectRoot(ROOT)
        self.backend = backend or NativeBackend(self.root)
        self.native = isinstance(self.backend, NativeBackend)
        if self.native and self.root.path != ROOT.resolve():
            raise QwenSetupError(f"native Qwen backend is restricted to repository root {ROOT}")

    def run(self, request: AnalysisRequest) -> dict:
        validate_request(request)
        media_path = self.root.resolve(request.media)
        if not media_path.is_file():
            raise QwenRequestError(f"media does not exist: {request.media}")
        if self.native:
            analyzer = self.root.resolve(f"{SPIKE}/analyze.py")
            env_python = self.root.resolve(f"{BASE}/env/Scripts/python.exe" if os.name == "nt" else f"{BASE}/env/bin/python")
            model = self.root.resolve(f"{BASE}/model")
            if not analyzer.is_file() or not env_python.is_file() or not model.is_dir():
                raise QwenSetupError("isolated Qwen spike runtime is unavailable; install is not performed by this helper")
        rid = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(4)
        run_rel = f"_work/qwen_helper/{rid}"
        self.root.write_with(run_rel, lambda path: path.mkdir(parents=True, exist_ok=False))
        report = {"schema_version": 1, "report_id": rid, "version": 1, "supersedes": None,
                  "status": "planned", "request": request.__dict__, "media": {"path": request.media},
                  "artifacts": {"adapter_sha256": _sha(Path(__file__).resolve()),
                                "helper_sha256": _sha(Path(__file__).with_name("__main__.py")),
                                "analyzer_sha256": _sha(self.root.resolve(f"{SPIKE}/analyze.py")) if self.native else None,
                                "core_sha256": _sha(Path(__file__).with_name("core.py"))},
                  "brief": {"text": request.brief, "sha256": hashlib.sha256(request.brief.encode()).hexdigest() if request.brief is not None else None},
                  "windows": [], "batches": [], "input_coverage": {}, "has_truncated_responses": False,
                  "errors": [], "limitations": LIMITATIONS.copy()}
        request_rel = f"{run_rel}/request.json"
        self.root.write_text(request_rel, json.dumps(report["request"], ensure_ascii=False, allow_nan=False, indent=2))
        self.root.append_line(f"{run_rel}/events.jsonl", json.dumps({"event": "request_created", "report_id": rid}, separators=(",", ":")))
        try:
            shared_slot = GpuExecutionSlot(self.root) if not request.dry_run else None
            with _Lock(self.root, execution_slot=shared_slot):
                if not request.dry_run:
                    lease_rel = "jobs/gpu_lease.json"
                    if self.root.resolve(lease_rel).is_file():
                        lease = json.loads(self.root.read_text(lease_rel))
                        pid = lease.get("pid")
                        alive = type(pid) is int and pid > 0 and pid_is_alive(pid)
                        if alive:
                            raise QwenSetupError("a live production GPU lease exists; Qwen helper will not launch")
                info = self.backend.probe(request.media, work_rel=run_rel, modality=request.modality)
                end = info.duration_seconds if request.duration_seconds is None else request.start_seconds + request.duration_seconds
                windows = plan_windows(info.duration_seconds, start_seconds=request.start_seconds, end_seconds=end,
                    window_seconds=request.window_seconds, overlap_seconds=request.overlap_seconds,
                    source_offset_seconds=request.source_offset_seconds)
                report["media"].update(sha256=_sha(media_path), info=info.__dict__)
                if isinstance(self.backend, NativeBackend) and hasattr(self.backend, "last_probe"):
                    report["media"]["probe"] = self.backend.last_probe
                report["input_coverage"] = {"requested_interval": [request.start_seconds, end],
                    "completed_intervals": [], "uncovered_intervals": [[request.start_seconds, end]]}
                report["windows"] = [{"index": w.index, "input_start_seconds": w.start_seconds,
                    "input_end_seconds": w.end_seconds, "source_start_seconds": w.source_start_seconds,
                    "source_end_seconds": w.source_end_seconds, "overlap_previous_seconds": w.overlap_previous_seconds,
                    "status": "planned", "prepared_media_path": None, "prepared_media_hash": None,
                    "audio_metrics": None, "raw_response": None, "output_token_count": None,
                    "termination_reason": None, "token_cap_reached": False, "receipt_path": None} for w in windows]
                if request.dry_run:
                    report["status"] = "planned"
                else:
                    completed: list[WindowSpec] = []
                    for offset in range(0, len(windows), 3):
                        _require_disk_space(self.root)
                        batch_windows = windows[offset:offset + 3]
                        prepared = []
                        for window in batch_windows:
                            _require_disk_space(self.root)
                            if _sha(media_path) != report["media"]["sha256"]:
                                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "source media changed during helper execution")
                            item = self.backend.prepare(request.media, window, work_rel=run_rel, modality=request.modality)
                            if item.preparation.get("source_sha256", report["media"]["sha256"]) != report["media"]["sha256"]:
                                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "source hash changed during window preparation")
                            try:
                                json.dumps(item.audio_metrics, allow_nan=False)
                            except (TypeError, ValueError) as exc:
                                raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                                               "prepared audio metrics contain invalid or non-finite values", cause=exc) from exc
                            prepared.append(item)
                        if _sha(media_path) != report["media"]["sha256"]:
                            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "source media changed before inference")
                        _require_disk_space(self.root)
                        outcome = self.backend.infer_batch(prepared, batch_windows,
                            prompt=build_prompt(request.purpose, request.brief, request.modality),
                            max_new_tokens=request.max_new_tokens, modality=request.modality, work_rel=run_rel)
                        report["batches"].append({"windows": [w.index for w in batch_windows], **outcome.telemetry,
                                                   "status": outcome.status, "error": outcome.error})
                        cleanup = outcome.telemetry.get("owned_process_tree_cleanup")
                        cleanup_ok = (isinstance(cleanup, dict) and cleanup.get("verified_stopped") is True
                                      and not cleanup.get("survivors") and not cleanup.get("unverifiable_identities")
                                      and not cleanup.get("failures"))
                        batch_failed = outcome.status != "success" or not cleanup_ok or len(outcome.results) != len(batch_windows)
                        for i, window in enumerate(batch_windows):
                            target = report["windows"][window.index - 1]
                            if i >= len(outcome.results):
                                target["status"] = "failed" if not any(
                                    value["status"] == "failed" for value in report["windows"]
                                ) else "not_run"
                                continue
                            native = outcome.results[i]
                            _validate_native_result(native, prompt=build_prompt(request.purpose, request.brief, request.modality),
                                                    cap=request.max_new_tokens, modality=request.modality,
                                                    duration=window.duration_seconds)
                            raw = native.get("text", native.get("output_text"))
                            raw = raw if isinstance(raw, str) else ""
                            tokens = native.get("output_token_count", native.get("generated_tokens"))
                            term = native.get("termination_reason", native.get("termination", {}))
                            cap = native.get("token_cap_reached") is True
                            target.update(status="truncated" if cap else "completed", prepared_media_path=prepared[i].media_path,
                                prepared_media_hash=prepared[i].media_sha256, audio_metrics=prepared[i].audio_metrics,
                                raw_response=raw, output_token_count=tokens, termination_reason=term,
                                token_cap_reached=cap, preparation=prepared[i].preparation)
                            if prepared[i].audio_metrics.get("digital_silence") is True:
                                target["audio_interpretation_note"] = (
                                    "Measured PCM is digital silence; any semantic audio description should be treated as unreliable."
                                )
                            receipt_rel = f"{run_rel}/receipts/window_{window.index:03d}.json"
                            self.root.write_text(receipt_rel, json.dumps(native, ensure_ascii=False, allow_nan=False, indent=2))
                            target["receipt_path"] = receipt_rel
                            completed.append(window)
                            self.root.append_line(f"{run_rel}/events.jsonl", json.dumps(
                                {"event": "window_completed", "index": window.index, "status": target["status"]},
                                ensure_ascii=False, allow_nan=False, separators=(",", ":")))
                        if batch_failed:
                            report["errors"].append(outcome.error or {"family": "job", "kind": JobErrorKind.WORKER_UNREACHABLE.value,
                                "message": "native cleanup was unverified; no later batch will launch",
                                "retryable": JobErrorKind.WORKER_UNREACHABLE.retryable,
                                "remediation": JobErrorKind.WORKER_UNREACHABLE.remediation})
                            if len(outcome.results) < len(batch_windows):
                                report["windows"][batch_windows[len(outcome.results)].index - 1]["status"] = "failed"
                            for window in windows[offset + max(1, len(outcome.results) + 1):]:
                                report["windows"][window.index - 1]["status"] = "not_run"
                            break
                    done = [w for w in windows if report["windows"][w.index - 1]["status"] in {"completed", "truncated"}]
                    report["input_coverage"] = {"requested_interval": [request.start_seconds, end],
                        "completed_intervals": [[w.start_seconds, w.end_seconds] for w in done],
                        "uncovered_intervals": coverage_gaps(request.start_seconds, end, done)}
                    report["has_truncated_responses"] = any(w["status"] == "truncated" for w in report["windows"])
                    cancelled = any(error.get("kind") == JobErrorKind.CANCELLED.value for error in report["errors"])
                    report["status"] = ("cancelled" if cancelled else
                        "completed" if not report["errors"] and all(w["status"] in {"completed", "truncated"} for w in report["windows"])
                        else "failed")
        except KeyboardInterrupt:
            report["status"] = "cancelled"
            kind = JobErrorKind.CANCELLED
            report["errors"].append({"family": "job", "kind": kind.value,
                                     "message": "interrupted after native supervisor cleanup",
                                     "retryable": kind.retryable, "remediation": kind.remediation})
        except (QwenRequestError, QwenSetupError, ContainmentError):
            raise
        except Exception as exc:  # noqa: BLE001 - unknown execution failures map to INTERNAL
            err = JobError.from_exception(exc, origin="adapter")
            report["status"] = "failed"
            report["errors"].append({"family": "job", "kind": err.kind.value, "message": err.message, "retryable": err.retryable,
                                     "remediation": err.remediation, "traceback": __import__("traceback").format_exc()})
        if report["status"] in {"failed", "cancelled"}:
            pending = [row for row in report["windows"] if row["status"] == "planned"]
            if pending:
                pending[0]["status"] = "failed"
                for row in pending[1:]:
                    row["status"] = "not_run"
            completed_specs = [spec for spec in locals().get("windows", [])
                               if report["windows"][spec.index - 1]["status"] in {"completed", "truncated"}]
            coverage = report.get("input_coverage", {})
            if coverage.get("requested_interval"):
                left, right = coverage["requested_interval"]
                report["input_coverage"] = {"requested_interval": [left, right],
                    "completed_intervals": [[spec.start_seconds, spec.end_seconds] for spec in completed_specs],
                    "uncovered_intervals": coverage_gaps(left, right, completed_specs)}
            self.root.append_line(f"{run_rel}/events.jsonl", json.dumps(
                {"event": report["status"], "completed_windows": [w.index for w in completed_specs]}, separators=(",", ":")))
        report["report_path"] = f"{run_rel}/report.json"
        report["markdown_path"] = f"{run_rel}/report.md"
        self.root.write_text(report["report_path"], json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
        self.root.write_text(report["markdown_path"], render_markdown(report))
        return report




