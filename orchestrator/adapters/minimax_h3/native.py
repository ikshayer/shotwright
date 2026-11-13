"""MiniMax H3 adapter boundary and native runtime integration."""
from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from array import array
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from orchestrator.domain.entities import Job
from orchestrator.domain.seeds import derive_seed
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings

from .core import H3Request, H3Result, H3SetupError, build_api_graph
from .schema import DYNAMIC_COMBO, dynamic_combo_option_keys, resolve_node_inputs

_CATALOG_SHA256 = "d44bbcebfdf2635f9a9ca9c1e37e7788fac57bb9fde021a9d1132ab08649b6e5"
_DOC_CATALOG_SHA256 = "9712c30a721268abc77ad1eac92e266a23737f5555c3ebdc04e0775282ebb5a1"
_SOURCE_RECEIPT_SHA256 = "220c4f772a09332bbec353f6a0a89c47ad9d52453991fb42079965ed00532575"
_EASY_STAGE_SHA256 = "c3c3984ad115e623fb395852e66c80ff8bdaa312f83738d583353a952b4c1655"
_GIB = 1024**3
_INITIAL_AVAILABLE_RAM_BYTES = 6 * _GIB
_LOW_AVAILABLE_RAM_BYTES = 2 * _GIB
_LOW_RAM_SAMPLES_TO_STOP = 3


class _ListenerOwnershipError(H3SetupError):
    """The loopback listener could not be positively tied to the owned process tree."""


class NativeBackend:
    """Native resource verifier and execution backend.

    Repository-owned runtime assets are resolved against ``repository_root``; durable
    candidate data is always written through the injected project ``root``.
    """

    def __init__(self, root: ProjectRoot, settings: Settings, *, popen: Callable[..., Any] | None = None,
                 http: Any = None, resources: Any = None,
                 repository_root: ProjectRoot | None = None) -> None:
        self.root, self.settings = root, settings
        self.repository_root = repository_root or ProjectRoot(Path(__file__).resolve().parents[3])
        self._popen = popen or subprocess.Popen
        self._http = http
        self._resources = resources
        self._lock = threading.RLock()
        self._closed = False
        self._active: Any = None
        self._session_rel: str | None = None
        self._tree: Any = None
        self._cleanup_lock = threading.Lock()
        self._stop_requested = False
        self._resource_stop = threading.Event()
        self._launch_lock = threading.RLock()
        self._children: dict[int, tuple[Any, Any]] = {}
        self._unsafe_cleanup = False
        self._resource_samples: list[dict[str, Any]] = []
        self._last_resource_check = 0.0
        self._admission_checked = False
        self._admission_sample: dict[str, Any] | None = None
        self._runtime_initial_checked = False
        self._runtime_low_ram_samples = 0
        self._last_listener_identity: dict[str, Any] | None = None
        self._last_submission_listener: dict[str, Any] | None = None
        self._attempt_cfg: Any = None
        self._attempt_root: ProjectRoot | None = None
        self._attempt_run_rel: str | None = None
        self._verify_cancel: Callable[[], bool] | None = None
        self._attempt_provenance: dict[str, Any] = {}
        self._attempt_seed: int | None = None

    def inspect(self) -> dict[str, Any]:
        cfg = getattr(self.settings, "minimax_h3", None)
        enabled = bool(getattr(cfg, "enabled", False)) if cfg is not None else False
        spike = self.repository_root.resolve("spikes/22_minimax_h3_workflows")
        runtime = self.repository_root.resolve("spikes/22_minimax_h3_workflows/_work/local_runtime")
        catalog = self.repository_root.resolve("spikes/22_minimax_h3_workflows/LOCAL_MODEL_CATALOG.json")
        return {"enabled": enabled, "runtime_present": spike.is_dir() and runtime.is_dir() and catalog.is_file(),
                "port": 8298, "tested_defaults": {"duration_seconds": 8, "width": 384,
                "height": 288, "fps": 24, "steps": 20},
                "limitations": ["video plus prompt only", "explicitly experimental settings are opt-in",
                                "capacity remains operator-configured and unmeasured"]}

    def verify_runtime(self, cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Check pinned runtime, helper, plugin and all four model files without loading them."""
        if cancel is None:
            cancel = self._verify_cancel
        h3 = _load_helper(self.repository_root)
        try:
            receipt = _verify_models_pinned(self.repository_root, h3, cancel)
            source_receipt = _verify_receipt_files(
                self.repository_root,
                "spikes/22_minimax_h3_workflows/_work/local_runtime/receipts/source_receipt.json",
                cancel,
            )
            stage_receipt = _verify_easy_stage(self.repository_root, cancel)
        except Exception as exc:
            if isinstance(exc, (JobError, H3SetupError)):
                raise
            raise H3SetupError(f"MiniMax H3 pinned runtime verification failed: {exc}") from exc
        catalog = json.loads(self.repository_root.read_text("spikes/22_minimax_h3_workflows/LOCAL_MODEL_CATALOG.json"))
        runtime_rel = "spikes/22_minimax_h3_workflows/_work/local_runtime"
        runtime = self.repository_root.resolve(runtime_rel)
        source = runtime / "source" / "ComfyUI"
        plugin = source / "custom_nodes" / "ComfyUI-MiniMaxH3-Easy"
        python = runtime / "env" / "Scripts" / "python.exe"
        for label, path in (("ComfyUI source", source / "main.py"), ("Easy plugin", plugin),
                            ("pinned runtime Python", python)):
            if not path.exists():
                raise H3SetupError(f"MiniMax H3 {label} is missing: {path}")
        names = {row["role"]: Path(row["model_relative_path"]).name for row in catalog["components"]}
        controller = self.repository_root.resolve("spikes/22_minimax_h3_workflows/direct_comfy.py")
        helper = self.repository_root.resolve("spikes/22_minimax_h3_workflows/local_h3.py")
        provenance = {"model_receipt": receipt,
                      "catalog": _identity(self.repository_root, "spikes/22_minimax_h3_workflows/LOCAL_MODEL_CATALOG.json"),
                      "controller": _identity(self.repository_root, "spikes/22_minimax_h3_workflows/direct_comfy.py"),
                      "helper": _identity(self.repository_root, "spikes/22_minimax_h3_workflows/local_h3.py"),
                      "source_receipt": source_receipt, "easy_stage_receipt": stage_receipt,
                      "plugin": _identity_path(plugin / "__init__.py")}
        return {"models": names, "provenance": provenance, "runtime_python": str(python),
                "comfy_root": str(source), "controller_path": str(controller), "helper_path": str(helper)}

    def prepare_video(self, request: H3Request, *, run_rel: str,
                      cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Prepare a contained, silent, exact-frame proxy from the bound project input."""
        request = H3Request.model_validate(request.model_dump(mode="python"))
        self.root.resolve(run_rel)
        source = self.root.resolve(request.video_path)
        if not source.is_file():
            raise JobError(JobErrorKind.INVALID_MANIFEST, "bound H3 video source is missing")
        if "references" in {part.lower() for part in Path(request.video_path).parts}:
            raise JobError(JobErrorKind.INVALID_MANIFEST, "H3 source must be a bound master or degraded output, not reference media")
        if _hash_file(source, cancel) != request.source_sha256:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "bound H3 source hash changed before use")
        if source.suffix.lower() not in {".mp4", ".mkv", ".webm", ".avi"}:
            raise JobError(JobErrorKind.INVALID_MANIFEST, "H3 accepts only local MP4, Matroska, WebM or AVI sources")
        if cancel and cancel():
            raise JobError(JobErrorKind.CANCELLED, "H3 video preparation cancelled")
        self.root.make_dir(run_rel + "/input")
        output_rel = run_rel + "/input/model_proxy.mp4"
        output = self.root.resolve(output_rel)
        ffmpeg, ffprobe = _resolve_ffmpeg(self.settings)
        probe = self._run_capture([ffprobe, "-protocol_whitelist", "file,pipe", "-v", "error", "-show_entries", "format=format_name,duration,start_time:stream=codec_type,width,height,r_frame_rate,avg_frame_rate,start_time,duration,duration_ts,time_base,nb_frames,sample_rate,channels", "-of", "json", str(source)],
                             cwd=self.root.path, cancel=cancel, timeout=30,
                             env=self._contained_env(run_rel + "/temp"))
        if probe.returncode != 0:
            raise JobError(JobErrorKind.INVALID_MANIFEST, "ffprobe rejected the bound H3 video source")
        try:
            meta = json.loads(probe.stdout.decode("utf-8"))
            container_duration = float(meta["format"]["duration"])
            container_start = float(meta["format"].get("start_time") or 0)
            demuxers = set(str(meta["format"]["format_name"]).split(","))
            streams = meta.get("streams", [])
            video = next(row for row in streams if row.get("codec_type") == "video")
            raw_video_start = video.get("start_time")
            video_start = (None if raw_video_start in (None, "N/A")
                           else float(raw_video_start))
            stream_duration = video.get("duration")
            if stream_duration not in (None, "N/A"):
                video_duration = float(stream_duration)
            elif video.get("nb_frames") not in (None, "N/A"):
                rate_num, rate_den = map(int, video.get("avg_frame_rate", video["r_frame_rate"]).split("/"))
                if rate_num <= 0 or rate_den <= 0:
                    raise ValueError("video stream frame rate is invalid")
                video_duration = int(video["nb_frames"]) * rate_den / rate_num
            elif video_start is not None:
                # Bound a stream with no explicit duration at the container end,
                # accounting for an earlier audio-origin timestamp.
                video_duration = container_start + container_duration - video_start
            else:
                raise ValueError("video stream has no origin or finite duration")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 source timing is unavailable", cause=exc) from exc
        except StopIteration as exc:
            raise JobError(JobErrorKind.INVALID_MANIFEST, "H3 source contains no video stream", cause=exc) from exc
        if (not all(math.isfinite(value) for value in (container_duration, container_start, video_duration))
                or (video_start is not None and not math.isfinite(video_start)) or video_duration <= 0):
            raise JobError(JobErrorKind.INVALID_MANIFEST, "requested H3 interval is outside the source video")
        try:
            interval_end = Decimal(str(request.start_seconds)) + Decimal(str(request.duration_seconds))
            source_end = Decimal(str(video_duration))
        except InvalidOperation as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 source timing is not finite decimal time", cause=exc) from exc
        if interval_end > source_end:
            raise JobError(JobErrorKind.INVALID_MANIFEST, "requested H3 interval is outside the source video")
        allowed_demuxers = {"mp4", "mov", "matroska", "webm", "avi"}
        if not demuxers.intersection(allowed_demuxers):
            raise JobError(JobErrorKind.INVALID_MANIFEST, "H3 source is not a supported self-contained video container")
        if output.exists():
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "refusing to overwrite an existing H3 proxy")
        filters = (f"setpts=PTS-STARTPTS,trim=start={request.start_seconds:.9f}:duration={request.duration_seconds:.9f},"
                   f"setpts=PTS-STARTPTS,scale={request.width}:{request.height}:force_original_aspect_ratio=decrease,"
                   f"pad={request.width}:{request.height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={request.fps}")
        # Preserve source PTS at demux, then normalize the first decoded video
        # frame to zero before trimming.  This makes start_seconds relative to
        # the video stream even when the container or an audio stream starts
        # earlier.  There is deliberately no input-side -ss seek to double-apply
        # the media origin.
        command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-protocol_whitelist", "file,pipe",
                   "-copyts", "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
                   "-vf", filters, "-frames:v", str(round(request.duration_seconds * request.fps)),
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
        run_receipt = self._run_owned(command, cwd=self.root.path, cancel=cancel,
                                      timeout=120, env=self._contained_env(run_rel + "/temp"))
        if not output.is_file() or output.stat().st_size <= 0:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 proxy creation produced no video")
        proxy_probe = self._run_capture([ffprobe, "-protocol_whitelist", "file,pipe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(output)], cwd=self.root.path,
                                   cancel=cancel, timeout=30,
                                   env=self._contained_env(run_rel + "/temp"))
        try:
            proxy_info = json.loads(proxy_probe.stdout.decode("utf-8"))
            video_streams = [stream for stream in proxy_info["streams"] if stream["codec_type"] == "video"]
            if proxy_probe.returncode or len(video_streams) != 1 or any(stream["codec_type"] == "audio" for stream in proxy_info["streams"]):
                raise ValueError("proxy streams are invalid")
            row = video_streams[0]
            if (int(row["width"]), int(row["height"]), row["avg_frame_rate"],
                    row.get("sample_aspect_ratio"), int(row["nb_read_frames"])) != (
                    request.width, request.height, f"{request.fps}/1", "1:1",
                    int(request.duration_seconds * request.fps)):
                raise ValueError("proxy does not match the requested exact frame grid")
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, f"H3 silent proxy failed full validation: {exc}", cause=exc) from exc
        return {"path": output_rel, "sha256": _hash_file(output, cancel),
                "frame_count": round(request.duration_seconds * request.fps), "fps": request.fps,
                "width": request.width, "height": request.height,
                "duration_seconds": request.duration_seconds, "source_sha256": request.source_sha256,
                "source_path": request.video_path, "source_duration_seconds": video_duration,
                "source_container_duration_seconds": container_duration,
                "source_container_start_seconds": container_start,
                "source_video_start_seconds": video_start,
                "timestamp_normalization": {
                    "basis": "first decoded video-frame PTS (video stream origin)",
                    "decode_side": True,
                    "input_timestamps_preserved": True,
                    "filter": "setpts=PTS-STARTPTS before trim; reset after trim",
                    "requested_interval_relative_to_video_origin": {
                        "start_seconds": request.start_seconds,
                        "duration_seconds": request.duration_seconds,
                    },
                },
                "command": command,
                "process": run_receipt}

    def _contained_env(self, temp_rel: str) -> dict[str, str]:
        self.root.make_dir(temp_rel)
        temp = str(self.root.resolve(temp_rel))
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
        env.update({"TEMP": temp, "TMP": temp, "TMPDIR": temp,
                    "HOME": temp, "USERPROFILE": temp, "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1", "COMFYUI_DISABLE_TELEMETRY": "1"})
        return env

    def _owned_tree(self, process: Any) -> Any:
        existing = self._children.get(id(process))
        if existing is not None and existing[1] is not None:
            return existing[1]
        helper = _load_helper(self.repository_root)
        tree = helper.OwnedProcessTree(process)
        tree.refresh()
        self._children[id(process)] = (process, tree)
        return tree

    def _forget_process(self, process: Any) -> None:
        self._children.pop(id(process), None)

    def respawn(self) -> None:
        with self._launch_lock:
            if self._unsafe_cleanup:
                raise RuntimeError("cannot re-arm H3 after uncertain native process cleanup")
            if self._children or self._active is not None:
                raise RuntimeError("cannot re-arm H3 while owned processes remain")
            self._resource_stop.clear()
            self._attempt_cfg = None
            self._attempt_root = None
            self._attempt_run_rel = None
            self._attempt_provenance = {}
            self._attempt_seed = None

    def _launch_popen(self, command: list[str], **kwargs: Any) -> Any:
        """Atomically gate child creation against stop/release and capture its identity tree."""
        with self._launch_lock:
            if self._resource_stop.is_set() or self._closed:
                raise JobError(JobErrorKind.CANCELLED, "H3 native launch blocked by stop or close")
            process = self._popen(command, **kwargs)
            try:
                tree = self._owned_tree(process)
            except BaseException:  # noqa: BLE001
                try:
                    process.kill()
                    process.wait(timeout=5)
                finally:
                    raise
            self._children[id(process)] = (process, tree)
            return process

    def _run_capture(self, command: list[str], *, cwd: Path,
                     cancel: Callable[[], bool] | None, timeout: float,
                     env: dict[str, str]) -> Any:
        return _run_capture(command, cwd=cwd, popen=self._launch_popen, cancel=self._guard_cancel(cancel),
                             timeout=timeout, env=env, tree_factory=self._owned_tree)

    def _run_owned(self, command: list[str], *, cwd: Path, cancel: Callable[[], bool] | None,
                   timeout: float, env: dict[str, str]) -> dict[str, Any]:
        return _run_owned(command, cwd=cwd, popen=self._launch_popen, cancel=self._guard_cancel(cancel), timeout=timeout,
                          env=env, tree_factory=self._owned_tree)

    def _guard_cancel(self, cancel: Callable[[], bool] | None) -> Callable[[], bool]:
        def check() -> bool:
            if self._resource_stop.is_set() or (cancel is not None and cancel()):
                return True
            if self._attempt_cfg is not None:
                self._check_resources(force=False)
            return False
        return check

    def _read_resource_sample(self) -> tuple[dict[str, Any], int, int, float]:
        if self._resources is not None:
            sample = self._resources()
        else:
            helper = _load_helper(self.repository_root)
            sample = helper.resources(helper.PynvmlProbe(device_index=0), self._tree)
        try:
            if not isinstance(sample, dict):
                raise TypeError("resource probe did not return an object")
            ram = int(sample["ram_available_bytes"])
            disk = int(sample["disk_free_bytes"])
            gpu = sample["gpu_whole_device_mb"]
            vram = float(gpu["free_mb"])
        except (TypeError, KeyError, ValueError) as exc:
            raise JobError(JobErrorKind.INTERNAL, "H3 resource telemetry is malformed", cause=exc) from exc
        if ram < 0 or disk < 0 or not math.isfinite(vram) or vram < 0:
            raise JobError(JobErrorKind.INTERNAL, "H3 resource telemetry contains invalid negative or nonfinite values")
        return sample, ram, disk, vram

    def _record_resource_sample(self, sample: dict[str, Any], *, phase: str,
                                details: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {**sample, "utc_recorded": time.time(), "phase": phase}
        if details:
            record.update(details)
        self._resource_samples.append(record)
        if self._attempt_root is not None and self._attempt_run_rel is not None:
            self._attempt_root.append_line(self._attempt_run_rel + "/events/resources.jsonl",
                                           json.dumps(record, ensure_ascii=False, allow_nan=False))
        return record

    def _check_admission_resources(self) -> dict[str, Any]:
        """Apply operator reservations once, before any H3 model process is launched."""
        if self._admission_checked and self._admission_sample is not None:
            return self._admission_sample
        cfg = self._attempt_cfg
        try:
            required_ram = float(cfg.required_ram_gb)
            required_disk = float(cfg.required_disk_gb)
            required_vram = float(cfg.required_vram_gb)
        except (AttributeError, TypeError, ValueError) as exc:
            raise H3SetupError("MiniMax H3 requires explicit finite positive RAM, disk, and VRAM reservations") from exc
        if any(not math.isfinite(x) or x <= 0 for x in (required_ram, required_disk, required_vram)):
            raise H3SetupError("MiniMax H3 requires explicit finite positive RAM, disk, and VRAM reservations")
        sample, ram, disk, vram = self._read_resource_sample()
        if (ram < required_ram * _GIB or disk < required_disk * _GIB
                or vram < required_vram * 1024):
            record = self._record_resource_sample(sample, phase="admission", details={
                "admission_reservations_gb": {"ram": required_ram, "disk": required_disk, "vram": required_vram},
                "admission_passed": False,
            })
            self._admission_sample = record
            self._admission_checked = True
            raise JobError(JobErrorKind.BUDGET_EXCEEDED,
                           "Current H3 RAM, disk, or whole-device VRAM is below configured operator admission reservations")
        record = self._record_resource_sample(sample, phase="admission", details={
            "admission_reservations_gb": {"ram": required_ram, "disk": required_disk, "vram": required_vram},
            "admission_passed": True,
        })
        self._admission_sample = record
        self._admission_checked = True
        return record

    def _check_resources(self, *, force: bool) -> dict[str, Any]:
        """Apply the fixed runtime RAM floors; operator reservations are admission-only."""
        now = time.monotonic()
        if not force and now - self._last_resource_check < 1.0 and self._resource_samples:
            last = self._resource_samples[-1]
            if last.get("phase") == "runtime":
                return last
        if not self._runtime_initial_checked:
            raise H3SetupError("MiniMax H3 runtime monitoring started without the initial 6 GiB RAM check")
        sample, ram, _disk, _vram = self._read_resource_sample()
        self._runtime_low_ram_samples = (
            self._runtime_low_ram_samples + 1 if ram < _LOW_AVAILABLE_RAM_BYTES else 0
        )
        record = self._record_resource_sample(sample, phase="runtime", details={
            "runtime_ram_floor_policy": {
                "initial_available_ram_bytes": _INITIAL_AVAILABLE_RAM_BYTES,
                "stop_below_available_ram_bytes": _LOW_AVAILABLE_RAM_BYTES,
                "consecutive_samples_to_stop": _LOW_RAM_SAMPLES_TO_STOP,
            },
            "low_available_ram_consecutive_samples": self._runtime_low_ram_samples,
        })
        self._last_resource_check = now
        if self._runtime_low_ram_samples >= _LOW_RAM_SAMPLES_TO_STOP:
            raise JobError(JobErrorKind.BUDGET_EXCEEDED,
                           "H3 available RAM remained below 2 GiB for three runtime samples")
        return record

    def _check_initial_runtime_resources(self) -> dict[str, Any]:
        """Enforce the existing 6 GiB prelaunch runtime floor, independent of reservations."""
        sample, ram, _disk, _vram = self._read_resource_sample()
        record = self._record_resource_sample(sample, phase="runtime_initial", details={
            "runtime_ram_floor_policy": {
                "initial_available_ram_bytes": _INITIAL_AVAILABLE_RAM_BYTES,
                "stop_below_available_ram_bytes": _LOW_AVAILABLE_RAM_BYTES,
                "consecutive_samples_to_stop": _LOW_RAM_SAMPLES_TO_STOP,
            },
            "initial_available_ram_bytes": ram,
        })
        self._last_resource_check = time.monotonic()
        if ram < _INITIAL_AVAILABLE_RAM_BYTES:
            raise JobError(JobErrorKind.BUDGET_EXCEEDED,
                           "H3 available RAM is below the existing 6 GiB initial runtime floor")
        self._runtime_low_ram_samples = 0
        self._runtime_initial_checked = True
        return record

    def verify_and_export(self, request: H3Request, *, run_rel: str, av_path: str,
                          cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Validate AV container and extract decoded encoded audio without level changes."""
        request = H3Request.model_validate(request.model_dump(mode="python"))
        self.root.resolve(run_rel)
        self.root.resolve(av_path)
        path = self.root.resolve(av_path)
        ffmpeg, ffprobe = _resolve_ffmpeg(self.settings)
        probe = self._run_capture([ffprobe, "-protocol_whitelist", "file,pipe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                             cwd=self.root.path, cancel=cancel, timeout=30,
                             env=self._contained_env(run_rel + "/temp"))
        if probe.returncode:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 output failed ffprobe validation")
        try:
            data = json.loads(probe.stdout.decode("utf-8"))
            videos = [row for row in data["streams"] if row.get("codec_type") == "video"]
            audios = [row for row in data["streams"] if row.get("codec_type") == "audio"]
            if len(videos) != 1 or len(audios) != 1:
                raise ValueError("expected exactly one video and one audio stream")
            video, audio = videos[0], audios[0]
            frames = int(video.get("nb_frames") or video.get("nb_read_frames"))
            rate_num, rate_den = map(int, video["avg_frame_rate"].split("/"))
            if rate_den <= 0 or rate_num % rate_den:
                raise ValueError("video frame rate is not an exact integer")
            fps = rate_num // rate_den
            channels, sample_rate = int(audio["channels"]), int(audio["sample_rate"])
            av_duration = float(data["format"]["duration"])
            audio_duration = float(audio["duration"])
            if not math.isfinite(av_duration) or not math.isfinite(audio_duration):
                raise ValueError("AV duration is not finite")
        except (ValueError, TypeError, KeyError, StopIteration, ZeroDivisionError) as exc:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 AV streams are missing or malformed", cause=exc) from exc
        expected_frames = round(request.duration_seconds * request.fps)
        if ((int(video.get("width", 0)), int(video.get("height", 0)), fps, frames)
                != (request.width, request.height, request.fps, expected_frames)
                or abs(av_duration - request.duration_seconds) > 1 / request.fps
                or abs(audio_duration - request.duration_seconds) > 1 / request.fps):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 output dimensions, frame rate, or frame count differ from request")
        wav_rel = run_rel + "/audio.wav"
        wav = self.root.resolve(wav_rel)
        if wav.exists():
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "refusing to overwrite an existing decoded H3 WAV")
        wav_receipt = self._run_owned([ffmpeg, "-hide_banner", "-nostdin", "-y", "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:a:0", "-vn", "-c:a", "pcm_f32le", str(wav)],
                                      cwd=self.root.path, cancel=cancel, timeout=120,
                                      env=self._contained_env(run_rel + "/temp"))
        # Decode both streams fully; stream metadata alone does not prove readability.
        decode_receipt = self._run_owned([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(path), "-f", "null", "-"],
                                         cwd=self.root.path, cancel=cancel, timeout=120,
                                         env=self._contained_env(run_rel + "/temp"))
        wav_probe = self._run_capture([ffprobe, "-protocol_whitelist", "file,pipe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels,duration_ts,time_base", "-of", "json", str(wav)], cwd=self.root.path,
                                 cancel=cancel, timeout=30,
                                 env=self._contained_env(run_rel + "/temp"))
        if wav_probe.returncode or not wav.is_file() or wav.stat().st_size < 48:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 decoded WAV is missing or invalid")
        wav_stream = json.loads(wav_probe.stdout.decode("utf-8"))["streams"][0]
        samples = round(float(wav_stream["duration_ts"]) * _time_base(wav_stream["time_base"]) * int(wav_stream["sample_rate"]))
        if (samples <= 0 or int(wav_stream["sample_rate"]) != sample_rate
                or int(wav_stream["channels"]) != channels
                or abs(samples / sample_rate - request.duration_seconds) > 1 / request.fps):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 decoded WAV contains no samples")
        metrics = _wav_metrics(wav, cancel)
        return {"av_path": av_path, "av_sha256": _hash_file(path, cancel), "wav_path": wav_rel,
                "wav_sha256": _hash_file(wav, cancel), "frame_count": frames, "fps": fps,
                "audio_sample_rate": sample_rate, "audio_channels": channels,
                "audio_samples_per_channel": samples, "audio_metrics": metrics,
                "wav_process": wav_receipt, "decode_process": decode_receipt,
                "duration_seconds": av_duration}

    def execute(self, request: H3Request, job: Job, *, root: ProjectRoot, queue: JobQueue,
                cancel: Callable[[], bool] | None, progress: Callable[[float], None]) -> H3Result:
        request = H3Request.model_validate(request.model_dump(mode="python"))
        run_rel = f"generated/h3/{request.shot_id}/v{request.shot_version:04d}/{request.run_id}"
        try:
            return self._execute_impl(request, job, root=root, queue=queue,
                                      cancel=cancel, progress=progress)
        except BaseException as exc:
            try:
                root.resolve(run_rel)
                root.make_dir(run_rel)
                report_rel = run_rel + "/report.json"
                if not root.resolve(report_rel).exists():
                    cleanup_error = getattr(self, "_last_cleanup_error", None)
                    stopped_path = root.resolve(run_rel + "/events/server-stopped.json")
                    try:
                        cleanup_record = json.loads(stopped_path.read_text(encoding="utf-8-sig")) if stopped_path.is_file() else cleanup_error
                    except (OSError, ValueError, json.JSONDecodeError):
                        cleanup_record = cleanup_error
                    report = {"status": "cancelled" if (isinstance(exc, JobError) and exc.kind == JobErrorKind.CANCELLED) or isinstance(exc, KeyboardInterrupt) else "failed",
                              "run_id": request.run_id, "shot_id": request.shot_id,
                              "shot_version": request.shot_version,
                              "source_output_id": request.source_output_id,
                              "source_path": request.video_path,
                              "source_sha256": request.source_sha256,
                              "prompt": request.prompt,
                              "prompt_sha256": hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
                              "settings": request.model_dump(mode="json"),
                              "seed": self._attempt_seed,
                              "runtime": self._attempt_provenance,
                              "resource_samples": self._resource_samples,
                              "listener_before_prompt": self._last_submission_listener,
                              "log_path": run_rel + "/events/runtime-evidence.json",
                              "experimental": request.experimental,
                              "primary_error": {"type": type(exc).__name__, "message": str(exc)},
                              "cleanup": cleanup_record,
                              "cleanup_error": cleanup_error,
                              "submission_ambiguous": (
                                  root.resolve(run_rel + "/events/submission-marker.json").exists()
                                  and not root.resolve(run_rel + "/events/submission-response.json").exists()
                                  and not root.resolve(run_rel + "/events/submission-refused.json").exists()
                              )}
                    _write_json(root, report_rel, report)
            except BaseException as report_error:  # noqa: BLE001
                try:
                    exc.add_note(f"H3 failure report persistence also failed: {type(report_error).__name__}: {report_error}")
                except AttributeError:
                    pass
            raise

    def _execute_impl(self, request: H3Request, job: Job, *, root: ProjectRoot, queue: JobQueue,
                      cancel: Callable[[], bool] | None, progress: Callable[[float], None]) -> H3Result:
        if self._closed:
            raise H3SetupError("MiniMax H3 adapter is closed")
        cfg = getattr(self.settings, "minimax_h3", None)
        if not getattr(cfg, "enabled", False):
            raise H3SetupError("MiniMax H3 is disabled; enable [minimax_h3] and configure explicit admission reservations")
        request = H3Request.model_validate(request.model_dump(mode="python"))
        run_rel = f"generated/h3/{request.shot_id}/v{request.shot_version:04d}/{request.run_id}"
        if root.resolve(run_rel).exists():
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "refusing to overwrite an existing H3 run directory")
        if self._active is not None or self._children:
            raise JobError(JobErrorKind.INTERNAL, "MiniMax H3 cannot start while prior owned processes remain tracked")
        self._tree = None
        self._session_rel = None
        self._attempt_cfg = cfg
        self._attempt_root = root
        self._attempt_run_rel = run_rel
        self._resource_samples = []
        self._last_resource_check = 0.0
        self._admission_checked = False
        self._admission_sample = None
        self._runtime_initial_checked = False
        self._runtime_low_ram_samples = 0
        self._last_listener_identity = None
        self._last_submission_listener = None
        self._last_cleanup_error = None
        self._attempt_provenance = {}
        self._attempt_seed = None
        root.make_dir(run_rel)
        root.make_dir(run_rel + "/events")
        root.make_dir(run_rel + "/logs")
        root.make_dir(run_rel + "/bundle")
        self._check_initial_runtime_resources()
        if cancel and cancel():
            raise JobError(JobErrorKind.CANCELLED, "H3 generation cancelled before runtime verification")
        self._verify_cancel = cancel
        try:
            runtime = self.verify_runtime()
        finally:
            self._verify_cancel = None
        self._attempt_provenance = runtime.get("provenance", {})
        root.write_text(run_rel + "/prompt.txt", request.prompt)
        progress(0.05)
        source_before = _hash_file(root.resolve(request.video_path), cancel)
        if source_before != request.source_sha256:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 source changed before execution")
        proxy = self.prepare_video(request, run_rel=run_rel, cancel=cancel)
        progress(0.1)

        repo = self.repository_root
        base = "spikes/22_minimax_h3_workflows/_work/direct_comfy"
        session_rel = base + "/managed_" + uuid.uuid4().hex[:12]
        self._session_rel = session_rel
        for leaf in ("input", "output", "temp", "user/default/workflows", "previews", "database"):
            repo.make_dir(session_rel + "/" + leaf)
        with root.resolve(proxy["path"]).open("rb") as stream:
            repo.write_stream(session_rel + "/input/model_proxy.mp4", stream)
        runtime_proxy_rel = session_rel + "/input/model_proxy.mp4"
        # A provisional graph lets the isolated supervisor become ready. It is never
        # queued: the RandomNoise seed is rebound from the live /object_info schema.
        seed = 0
        graph = build_api_graph(request, models=runtime["models"], seed=seed)
        graph_rel = session_rel + "/api_graph.json"
        workflow_rel = session_rel + "/workflow.json"
        _write_json(repo, graph_rel, graph)
        _write_json(repo, workflow_rel, graph)
        code_rel = "spikes/22_minimax_h3_workflows/direct_comfy.py"
        helper_rel = "spikes/22_minimax_h3_workflows/local_h3.py"
        controller_path = Path(runtime.get("controller_path", str(repo.resolve(code_rel))))
        helper_path = Path(runtime.get("helper_path", str(repo.resolve(helper_rel))))
        prepared = {"session": session_rel, "url": "http://127.0.0.1:8298",
                    "code": _identity_from_root(repo, controller_path), "helper": _identity_from_root(repo, helper_path),
                    "workflow": _identity(repo, workflow_rel), "api_graph": _identity(repo, graph_rel),
                    "proxy": _identity(repo, runtime_proxy_rel), "optimizer": "disabled by request graph"}
        _write_json(repo, session_rel + "/prepared.json", prepared)
        log_rel = session_rel + "/supervisor.log"
        log_path = repo.resolve(log_rel)
        self._check_admission_resources()
        with log_path.open("xb") as log:
            controller = controller_path
            command = [sys.executable, "-B", str(controller), "serve", "--session", session_rel]
            self._check_resources(force=True)
            with self._launch_lock:
                if self._resource_stop.is_set() or self._closed:
                    raise JobError(JobErrorKind.CANCELLED, "H3 supervisor launch blocked by stop or close")
                process = self._launch_popen(command, cwd=repo.path, stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=subprocess.STDOUT, shell=False,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self._active = process
                self._tree = self._owned_tree(process)
        _write_json(root, run_rel + "/events/server-launch.json", {
            "argv": command, "session": session_rel, "identities": self._tree.snapshot(),
            "runtime_provenance": runtime["provenance"]})
        settings = self.settings.minimax_h3
        startup_deadline = time.monotonic() + min(float(settings.startup_allowance_seconds), 600.0)
        try:
            while time.monotonic() < startup_deadline:
                if cancel and cancel():
                    raise JobError(JobErrorKind.CANCELLED, "H3 cancelled during server startup")
                if process.poll() is not None:
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "owned H3 server supervisor exited during startup")
                if repo.resolve(session_rel + "/ready.json").is_file():
                    break
                time.sleep(0.25)
            else:
                raise JobError(JobErrorKind.BUDGET_EXCEEDED, "H3 server startup deadline exceeded")
            ready = json.loads(repo.read_text(session_rel + "/ready.json"))
            schema = self._request("GET", "/object_info", timeout=30)
            maximum = _validate_live_schema(graph, schema)
            seed = derive_seed(request.shot_id, request.degrade_seed,
                               "minimax_h3_ambience", max_value=min(maximum, 2**53 - 1))
            self._attempt_seed = seed
            graph = build_api_graph(request, models=runtime["models"], seed=seed)
            _validate_live_schema(graph, schema)
            _write_json(repo, graph_rel, graph)
            _write_json(repo, workflow_rel, graph)
            root.write_text(run_rel + "/api_graph.json", json.dumps(graph, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            prepared["api_graph"] = _identity(repo, graph_rel)
            prepared["workflow"] = _identity(repo, workflow_rel)
            _write_json(repo, session_rel + "/prepared.json", prepared)
            queue_state = self._request("GET", "/queue", timeout=10)
            if queue_state.get("queue_running") or queue_state.get("queue_pending"):
                raise JobError(JobErrorKind.WORKER_UNREACHABLE, "owned H3 server queue was not empty before submission")
            live_stats = self._request("GET", "/system_stats", timeout=10)
            devices = live_stats.get("devices") if isinstance(live_stats, dict) else None
            if not isinstance(devices, list) or not any(
                    row.get("type") == "cuda" and "RTX 5070 Ti Laptop" in str(row.get("name", ""))
                    for row in devices if isinstance(row, dict)):
                raise H3SetupError("Owned MiniMax H3 server is not using the measured CUDA GPU 0")
            self._check_resources(force=True)
            graph_rel_project = run_rel + "/api_graph.json"
            prompt_hash = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
            root.write_text(graph_rel_project, json.dumps(graph, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            root.write_text(run_rel + "/events/submission-request.json", json.dumps({
                "graph": graph, "prompt_sha256": prompt_hash, "seed": seed,
                "source_sha256": source_before, "proxy_sha256": proxy["sha256"]}, ensure_ascii=False, allow_nan=False))
            root.write_text(run_rel + "/events/submission-marker.json",
                            "POST /prompt will be attempted at most once after listener ownership verification\n")
            # A transport failure here is ambiguous. The marker is retained and no retry is made.
            try:
                submitted = self._request("POST", "/prompt", {"prompt": graph, "client_id": str(uuid.uuid4())}, timeout=30)
            except _ListenerOwnershipError as exc:
                # Ownership failures are definitive refusals, not ambiguous HTTP outcomes.
                try:
                    root.write_text(run_rel + "/events/submission-refused.json", json.dumps({
                        "post_attempted": False, "reason": str(exc),
                    }, ensure_ascii=False) + "\n")
                except OSError as record_error:
                    try:
                        exc.add_note(f"Ownership refusal evidence could not be persisted: {record_error}")
                    except AttributeError:
                        pass
                raise
            except Exception as exc:
                raise JobError(JobErrorKind.WORKER_UNREACHABLE,
                               f"H3 /prompt submission outcome is ambiguous; no retry was attempted: {exc}",
                               cause=exc) from exc
            prompt_id = submitted.get("prompt_id")
            if not isinstance(prompt_id, str) or not prompt_id:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 server returned no prompt_id")
            if submitted.get("node_errors"):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 rejected graph nodes: " + json.dumps(submitted["node_errors"], ensure_ascii=False))
            root.write_text(run_rel + "/events/submission-response.json", json.dumps(submitted, allow_nan=False))
            progress(0.12)
            execution_deadline = time.monotonic() + min(float(settings.execution_timeout_seconds), 1800.0)
            history: dict[str, Any] | None = None
            last_log_bytes = 0
            while time.monotonic() < execution_deadline:
                if cancel and cancel():
                    raise JobError(JobErrorKind.CANCELLED, "H3 generation cancelled")
                if process.poll() is not None:
                    raise JobError(JobErrorKind.WORKER_UNREACHABLE, "H3 server supervisor exited during generation")
                self._check_resources(force=False)
                from urllib.parse import quote

                history_all = self._request("GET", "/history/" + quote(prompt_id, safe=""), timeout=10)
                candidate = history_all.get(prompt_id) if isinstance(history_all, dict) else None
                if isinstance(candidate, dict):
                    status = candidate.get("status", {})
                    if status.get("completed") is True and status.get("status_str") == "success":
                        history = candidate
                        break
                    if status.get("status_str") in {"error", "failed"}:
                        raise JobError(JobErrorKind.INTERNAL, "MiniMax H3 generation failed: " + json.dumps(status, ensure_ascii=False))
                server_log = repo.resolve(session_rel + "/server.log")
                if server_log.exists():
                    try:
                        size = server_log.stat().st_size
                        if size > last_log_bytes:
                            with server_log.open("rb") as stream:
                                stream.seek(last_log_bytes)
                                text = stream.read().decode("utf-8", "replace")
                            last_log_bytes = size
                            matches = re.findall(r"(\d+(?:\.\d+)?)%\s*\|", text)
                            if not matches:
                                matches = re.findall(r"Progress:\s*(\d+(?:\.\d+)?)%", text)
                            if matches:
                                progress(min(0.94, 0.12 + float(matches[-1]) * 0.0082))
                    except OSError:
                        pass
                time.sleep(0.5)
            if history is None:
                raise JobError(JobErrorKind.BUDGET_EXCEEDED, "MiniMax H3 execution deadline exceeded")
            recorded_prompt = history.get("prompt")
            if not isinstance(recorded_prompt, list) or len(recorded_prompt) < 5 or not _same_api_graph(recorded_prompt[2], graph):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 completed history graph differs from the exact submitted graph")
            if recorded_prompt[4] != ["12"]:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 history executed unexpected output nodes")
            outputs = history.get("outputs")
            if not isinstance(outputs, dict) or set(outputs) != {"12"}:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 history contains unexpected output nodes")
            node_output = outputs["12"]
            videos = node_output.get("images") if isinstance(node_output, dict) else None
            if videos is None and isinstance(node_output, dict):
                videos = node_output.get("videos")
            if not isinstance(videos, list) or len(videos) != 1:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 history must contain exactly one node-12 video")
            if isinstance(node_output, dict) and node_output.get("animated") not in (None, [True]):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 node-12 output was not a video")
            item = videos[0]
            if not isinstance(item, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 node-12 output entry is malformed")
            metadata = item.get("metadata", {})
            if isinstance(metadata, dict) and metadata:
                expected_meta = {"kind": "video", "width": request.width, "height": request.height,
                                 "fps": request.fps, "frame_count": round(request.duration_seconds * request.fps)}
                if any(metadata.get(key) != value for key, value in expected_meta.items()):
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 node-12 metadata differs from the request")
            filename, subfolder, output_type = item.get("filename"), item.get("subfolder", ""), item.get("type", "output")
            if (not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.mp4", filename, re.IGNORECASE)
                    or filename in {".", ".."} or ".." in filename
                    or ntpath.basename(filename) != filename or ":" in filename or "\\" in filename
                    or subfolder not in {"", None} or output_type != "output"):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 output path is unsafe or outside the expected output root")
            output_rel = session_rel + "/output/" + filename
            runtime_av = repo.resolve(output_rel)
            if not runtime_av.is_file():
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 history video file is missing")
            av_rel = run_rel + "/candidate.mp4"
            with runtime_av.open("rb") as stream:
                root.write_stream(av_rel, stream)
            progress(0.96)
            source_after = _hash_file(root.resolve(request.video_path), cancel)
            proxy_after = _hash_file(repo.resolve(runtime_proxy_rel), cancel)
            if source_after != source_before or proxy_after != proxy["sha256"]:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 source or proxy changed during generation")
        finally:
            primary_error = sys.exc_info()[1]
            cleanup: dict[str, Any] = {}
            try:
                cleanup = self._stop_server(repo, session_rel, process, self._tree)
            except BaseException as cleanup_exception:
                self._last_cleanup_error = {"type": type(cleanup_exception).__name__,
                                            "message": str(cleanup_exception)}
                self._unsafe_cleanup = True
                if primary_error is None:
                    raise JobError(JobErrorKind.INTERNAL,
                                   "H3 owned server cleanup could not be verified; GPU lease must remain held",
                                   cause=cleanup_exception) from cleanup_exception
                try:
                    primary_error.add_note(
                        f"H3 cleanup exception retained separately: {type(cleanup_exception).__name__}: {cleanup_exception}"
                    )
                except AttributeError:
                    pass
            self._active = None
            try:
                root.write_text(run_rel + "/events/server-stopped.json",
                                json.dumps(cleanup, ensure_ascii=False, allow_nan=False))
                self._copy_runtime_evidence(repo, root, session_rel, run_rel)
            except BaseException as evidence_error:  # noqa: BLE001
                self._last_cleanup_error = {
                    **(self._last_cleanup_error or {}),
                    "evidence_error": f"{type(evidence_error).__name__}: {evidence_error}",
                }
            if cleanup and not _process_cleanup_verified(cleanup):
                physical_error = _cleanup_failure_record(cleanup, physical=True)
                self._last_cleanup_error = {**(self._last_cleanup_error or {}), **physical_error}
                self._stop_requested = False
                if primary_error is None:
                    raise JobError(JobErrorKind.INTERNAL,
                                   "H3 owned server cleanup is unresolved; GPU lease must remain held")
                try:
                    primary_error.add_note(
                        "H3 physical cleanup was unresolved; details retained in cleanup_error"
                    )
                except AttributeError:
                    pass
            if cleanup and not _cleanup_verified(cleanup):
                # Gated on the predicate, not on the record's truthiness.
                # `_cleanup_failure_record` always returns a populated dict, so the former
                # `if outcome_error:` test was never false: every run with a cleanup
                # receipt was marked as having failed cleanup, carrying the fallback
                # message "shutdown outcome failed" with an empty reason list. The first
                # GPU run recorded exactly that, and such noise masks a real failure.
                #
                # This block cannot be folded into the check below: when `primary_error`
                # is propagating, the `finally` exits by re-raising and that check is
                # never reached, so this is the only place a failing run captures outcome
                # evidence. Both sites now ask `_cleanup_verified` the same question.
                self._last_cleanup_error = {**(self._last_cleanup_error or {}),
                                            **_cleanup_failure_record(cleanup, physical=False)}
        if not _cleanup_verified(cleanup):
            failure = _cleanup_failure_record(cleanup, physical=False)
            self._last_cleanup_error = {**(self._last_cleanup_error or {}), **failure}
            detail = failure.get("message") or "supervisor shutdown outcome was not successful"
            raise JobError(JobErrorKind.INTERNAL,
                           f"MiniMax H3 cleanup outcome failed despite physical cleanup verification: {detail}")
        checked = self.verify_and_export(request, run_rel=run_rel, av_path=av_rel, cancel=cancel)
        if _hash_file(root.resolve(request.video_path), cancel) != source_before:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 source changed during AV validation/export")
        report = {"status": "completed", "run_id": request.run_id, "shot_id": request.shot_id,
                  "shot_version": request.shot_version, "source_output_id": request.source_output_id,
                  "source_path": request.video_path, "source_sha256": source_before,
                  "prompt": request.prompt, "prompt_sha256": prompt_hash, "seed": seed,
                  "settings": request.model_dump(mode="json"), "experimental": request.experimental,
                  "runtime": runtime["provenance"], "proxy": proxy, "history": history,
                  "server_ready": ready, "cleanup": cleanup, "output": checked,
                  "listener_before_prompt": self._last_submission_listener,
                  "resource_samples": self._resource_samples,
                  "log_path": run_rel + "/events/runtime-evidence.json",
                  "capacity": "Operator admission reservations; native H3 capacity remains unmeasured."}
        report_rel = run_rel + "/report.json"
        _write_json(root, report_rel, report)
        root.write_text(run_rel + "/bundle/manifest.json", json.dumps({"video": "../candidate.mp4",
                         "audio": "../audio.wav", "provenance": "../report.json"}, indent=2) + "\n")
        progress(1.0)
        return H3Result(run_id=request.run_id, av_path=checked["av_path"], av_sha256=checked["av_sha256"],
                        wav_path=checked["wav_path"], wav_sha256=checked["wav_sha256"], report_path=report_rel,
                        report_sha256=_hash_file(root.resolve(report_rel)), frame_count=checked["frame_count"],
                        fps=checked["fps"], audio_sample_rate=checked["audio_sample_rate"],
                        audio_channels=checked["audio_channels"], audio_samples_per_channel=checked["audio_samples_per_channel"],
                        seed=seed, experimental=request.experimental)

    def _assert_owned_listener(self, tree: Any, *, action: str) -> dict[str, Any]:
        """Fail closed unless every 8298 listener is a live PID/create-time in our tree."""
        prefix = f"MiniMax H3 listener ownership failure before {action}"
        if tree is None:
            raise _ListenerOwnershipError(f"{prefix}: owned process tree is unavailable")
        try:
            import psutil

            connections = psutil.net_connections(kind="tcp")
            listeners = [row for row in connections
                         if row.status == psutil.CONN_LISTEN
                         and getattr(row.laddr, "port", None) == 8298]
            if not listeners:
                raise _ListenerOwnershipError(f"{prefix}: the OS reported no listener on port 8298")

            owned_rows = tree.refresh()
            if not isinstance(owned_rows, list):
                raise _ListenerOwnershipError(f"{prefix}: owned process identities could not be read")
            listener_identities: set[tuple[int, float]] = set()
            for listener in listeners:
                pid = listener.pid
                if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                    raise _ListenerOwnershipError(f"{prefix}: the OS did not provide a positive listener PID")
                try:
                    created = psutil.Process(pid).create_time()
                except Exception as exc:  # OS denial or PID race is unidentifiable, hence foreign.
                    raise _ListenerOwnershipError(
                        f"{prefix}: the OS refused create-time for listener PID {pid}: {type(exc).__name__}: {exc}"
                    ) from exc
                if not isinstance(created, (int, float)) or not math.isfinite(float(created)):
                    raise _ListenerOwnershipError(f"{prefix}: listener PID {pid} has no finite process create-time")
                created = float(created)
                member = any(
                    row.get("pid") == pid
                    and row.get("live_identity_verified") is True
                    and row.get("identity_state") == "verified_live"
                    and row.get("create_time", row.get("created")) == created
                    for row in owned_rows if isinstance(row, dict)
                )
                if not member:
                    raise _ListenerOwnershipError(
                        f"{prefix}: listener PID {pid} create-time {created!r} is not a verified member of the owned tree"
                    )
                listener_identities.add((pid, created))
            if len(listener_identities) != 1:
                raise _ListenerOwnershipError(f"{prefix}: port 8298 has multiple listener process identities")
        except _ListenerOwnershipError:
            raise
        except Exception as exc:  # Incomplete OS process information is not ownership proof.
            raise _ListenerOwnershipError(
                f"{prefix}: OS listener PID/create-time membership could not be established: {type(exc).__name__}: {exc}"
            ) from exc
        pid, created = next(iter(listener_identities))
        self._last_listener_identity = {"pid": pid, "create_time": created, "port": 8298,
                                        "ownership": "verified_member_of_owned_tree", "action": action}
        return dict(self._last_listener_identity)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                 *, timeout: float = 10) -> dict[str, Any]:
        if self._http is not None:
            return self._http(method, path, payload, timeout=timeout)
        # The injected HTTP seam is fixture-only. Real loopback requests always
        # prove the actual listener identity at the last point before connecting.
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8298" + path,
                                     data=None if payload is None else json.dumps(payload, allow_nan=False).encode(),
                                     method=method, headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        listener_identity = self._assert_owned_listener(self._tree, action=f"{method} {path}")
        try:
            with opener.open(req, timeout=timeout) as response:
                value = json.loads(response.read())
        finally:
            if method == "POST" and path == "/prompt":
                self._last_submission_listener = listener_identity
        if not isinstance(value, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 HTTP response was not an object")
        return value

    def _stop_server(self, repo: ProjectRoot, session_rel: str, process: Any, tree: Any) -> dict[str, Any]:
        self._stop_requested = True
        try:
            before = tree.refresh()
            before_error = None
        except BaseException as exc:  # noqa: BLE001
            before, before_error = [], f"{type(exc).__name__}: {exc}"
        try:
            if self._http is None:
                self._assert_owned_listener(tree, action="owned-server STOP")
            repo.write_text(session_rel + "/STOP", "managed H3 generation complete\n")
        except BaseException as exc:  # noqa: BLE001
            stop_error = f"{type(exc).__name__}: {exc}"
        else:
            stop_error = None
        wait_error = None
        wait_result: int | None = None
        try:
            wait_result = process.wait(timeout=30)
        except BaseException as exc:  # noqa: BLE001
            wait_error = f"{type(exc).__name__}: {exc}"
            try:
                cleanup_tree = tree.stop()
            except BaseException as exc:  # noqa: BLE001
                cleanup_tree = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            cleanup_tree = None
        try:
            after = tree.refresh()
            live_after = any(row.get("live_identity_verified") is True
                             or row.get("identity_state") in {"identity_unverifiable", "identity_check_error"}
                             for row in after)
            after_error = None
        except BaseException as exc:  # noqa: BLE001
            after, live_after = [], True
            after_error = f"{type(exc).__name__}: {exc}"
        if cleanup_tree is None:
            try:
                cleanup_tree = tree.stop()
            except BaseException as exc:  # noqa: BLE001
                cleanup_tree = {"error": f"{type(exc).__name__}: {exc}"}
        stopped_rel = session_rel + "/stopped.json"
        try:
            stopped = json.loads(repo.read_text(stopped_rel))
        except BaseException as exc:  # noqa: BLE001
            stopped = {"status": "missing", "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(stopped, dict):
            stopped = {"status": "malformed", "error": "stopped receipt was not a JSON object"}
        returncode = getattr(process, "returncode", None)
        if returncode is None and isinstance(wait_result, int) and not isinstance(wait_result, bool):
            returncode = wait_result
        stopped["supervisor_returncode"] = returncode
        stopped["outer_tree_before_stop"] = before
        stopped["outer_tree_after_exit"] = after
        stopped["outer_tree_had_live_descendants"] = live_after
        stopped["outer_tree_cleanup"] = cleanup_tree
        if before_error:
            stopped["outer_tree_before_error"] = before_error
        if after_error:
            stopped["outer_tree_after_error"] = after_error
        if wait_error:
            stopped["coordinator_wait_error"] = wait_error
        if stop_error:
            stopped["stop_marker_error"] = stop_error
        if _process_cleanup_verified(stopped):
            self._stop_requested = False
            self._forget_process(process)
        else:
            self._unsafe_cleanup = True
        return stopped

    def _copy_runtime_evidence(self, repo: ProjectRoot, project: ProjectRoot,
                               session_rel: str, run_rel: str) -> None:
        copied: list[dict[str, Any]] = []
        for source_name in ("supervisor.log", "server.log", "started.json", "ready.json",
                            "stopped.json", "object_info.json"):
            source_rel = session_rel + "/" + source_name
            source = repo.resolve(source_rel)
            if not source.is_file():
                continue
            target_rel = run_rel + "/logs/runtime-" + source_name
            with source.open("rb") as stream:
                project.write_stream(target_rel, stream)
            copied.append({"source": source_rel, "path": target_rel,
                           "sha256": _hash_file(project.resolve(target_rel)),
                           "bytes": project.resolve(target_rel).stat().st_size})
        _write_json(project, run_rel + "/events/runtime-evidence.json", copied)

    def stop(self) -> bool:
        with self._launch_lock:
            self._resource_stop.set()
            children = list(self._children.values())
            active, session_rel, server_tree = self._active, self._session_rel, self._tree
        safe = not self._unsafe_cleanup
        if active is not None and session_rel is not None:
            cleanup = self._stop_server(self.repository_root, session_rel, active, server_tree)
            safe = _process_cleanup_verified(cleanup)
            if safe:
                self._forget_process(active)
        for process, tree in children:
            if process is active:
                continue
            try:
                receipt = tree.stop()
                if not _process_tree_verified(receipt):
                    safe = False
                else:
                    self._forget_process(process)
            except BaseException:  # noqa: BLE001
                safe = False
        if self._children:
            safe = False
        return safe

    def close(self) -> None:
        with self._lock:
            self._closed = True
        if not self.stop():
            raise RuntimeError("MiniMax H3 native process cleanup could not be verified")


def _load_helper(root: ProjectRoot) -> Any:
    import importlib.util
    helper = root.resolve("spikes/22_minimax_h3_workflows/local_h3.py")
    if not helper.is_file():
        helper = Path(__file__).resolve().parents[3] / "spikes" / "22_minimax_h3_workflows" / "local_h3.py"
    spec = importlib.util.spec_from_file_location("_pipeline_local_h3", helper)
    if spec is None or spec.loader is None:
        raise H3SetupError("Pinned local H3 process helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _identity_path(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise H3SetupError(f"Required pinned H3 file is missing: {path}")
    return {"path": str(path.resolve()), "bytes": path.stat().st_size,
            "sha256": _hash_file(path)}


def _identity(root: ProjectRoot, relative: str) -> dict[str, Any]:
    path = root.resolve(relative)
    identity = _identity_path(path)
    identity["path"] = relative
    return identity


def _identity_from_root(root: ProjectRoot, path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.path).as_posix()
    except ValueError:
        return _identity_path(resolved)
    return _identity(root, relative)


def _json_record(root: ProjectRoot, relative: str) -> dict[str, Any]:
    try:
        value = json.loads(root.read_text(relative))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise H3SetupError(f"Pinned H3 provenance file is missing or malformed: {relative}") from exc
    if not isinstance(value, dict):
        raise H3SetupError(f"Pinned H3 provenance file must contain an object: {relative}")
    return value


def _verify_one(root: ProjectRoot, relative: str, expected_bytes: int,
                expected_sha256: str, cancel: Callable[[], bool] | None) -> dict[str, Any]:
    path = root.resolve(relative)
    if not path.is_file():
        raise H3SetupError(f"Pinned H3 file is missing: {relative}")
    actual_bytes = path.stat().st_size
    if actual_bytes != expected_bytes:
        raise H3SetupError(f"Pinned H3 file size differs: {relative}")
    actual_hash = _hash_file(path, cancel)
    if actual_hash != expected_sha256:
        raise H3SetupError(f"Pinned H3 file SHA256 differs: {relative}")
    return {"path": relative, "bytes": actual_bytes, "sha256": actual_hash}


def _verify_models_pinned(root: ProjectRoot, helper: Any,
                          cancel: Callable[[], bool] | None) -> dict[str, Any]:
    spike = "spikes/22_minimax_h3_workflows"
    catalog_rel = spike + "/LOCAL_MODEL_CATALOG.json"
    docs_rel = spike + "/LOCAL_MODEL_DOCUMENTATION.json"
    receipt_rel = spike + "/_work/local_models/acquisition_receipt.json"
    catalog = _json_record(root, catalog_rel)
    docs_catalog = _json_record(root, docs_rel)
    receipt = _json_record(root, receipt_rel)
    if receipt.get("status") != "all_components_verified":
        raise H3SetupError("All four pinned MiniMax H3 model components must retain verified acquisition receipts")
    catalog_id = _identity(root, catalog_rel)
    if catalog_id["sha256"] != _CATALOG_SHA256 or receipt.get("catalog", {}).get("sha256") != catalog_id["sha256"]:
        raise H3SetupError("MiniMax H3 model catalog no longer matches its acquisition receipt")
    models: list[dict[str, Any]] = []
    for expected in catalog.get("components", []):
        if cancel and cancel():
            raise JobError(JobErrorKind.CANCELLED, "H3 pinned model verification cancelled")
        role = expected["role"]
        row = next((item for item in receipt.get("components", []) if item.get("role") == role), None)
        if row is None or any(row.get(key) != expected.get(key) for key in (
                "revision", "repo", "source_filename", "expected_sha256", "expected_bytes", "model_relative_path")):
            raise H3SetupError(f"MiniMax H3 {role} model acquisition receipt differs from catalog")
        relative = spike + "/_work/local_models/" + expected["model_relative_path"]
        if row.get("actual", {}).get("path") != relative:
            raise H3SetupError(f"MiniMax H3 {role} load path is not the catalog-pinned path")
        models.append({"role": role, **_verify_one(root, relative, int(expected["expected_bytes"]),
                                                   expected["expected_sha256"], cancel)})
    if len(models) != 4 or {row["role"] for row in models} != {"diffusion", "text_encoder", "video_vae", "audio_vae"}:
        raise H3SetupError("Exactly four catalog-pinned MiniMax H3 components are required")
    docs_identity = _identity(root, docs_rel)
    if docs_identity["sha256"] != _DOC_CATALOG_SHA256 or receipt.get("documentation_catalog", {}).get("sha256") != docs_identity["sha256"]:
        raise H3SetupError("MiniMax H3 documentation catalog no longer matches acquisition receipt")
    expected_docs = docs_catalog.get("documents", [])
    actual_docs = receipt.get("documentation", [])
    if len(expected_docs) != len(actual_docs):
        raise H3SetupError("Pinned MiniMax H3 documentation receipt is incomplete")
    documents = []
    for expected in expected_docs:
        row = next((item for item in actual_docs if item.get("role") == expected.get("role")), None)
        if row is None or any(row.get(key) != value for key, value in expected.items()):
            raise H3SetupError(f"Pinned MiniMax H3 documentation receipt differs: {expected.get('role')}")
        actual = row.get("actual", {})
        documents.append(_verify_one(root, actual["path"], int(expected["expected_bytes"]),
                                     expected["expected_sha256"], cancel))
    return {"receipt": _identity(root, receipt_rel), "catalog": catalog_id,
            "documentation_catalog": docs_identity, "models": models, "documents": documents}


def _verify_receipt_files(root: ProjectRoot, relative: str,
                          cancel: Callable[[], bool] | None) -> dict[str, Any]:
    receipt = _json_record(root, relative)
    if _identity(root, relative)["sha256"] != _SOURCE_RECEIPT_SHA256:
        raise H3SetupError("Pinned ComfyUI core source receipt changed")
    if receipt.get("commit") != "2c75dc1b379a59e0880619d029607af5169a62ef":
        raise H3SetupError("Pinned ComfyUI core source commit differs from its frozen receipt")
    files = receipt.get("files")
    if not isinstance(files, list) or len(files) != 1332:
        raise H3SetupError("Pinned ComfyUI core source receipt is incomplete")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise H3SetupError("Pinned ComfyUI source receipt contains malformed file identity")
        path = item["path"]
        if path in seen:
            raise H3SetupError(f"Duplicate pinned ComfyUI source path: {path}")
        seen.add(path)
        _verify_one(root, path, int(item["bytes"]), item["sha256"], cancel)
    return {"path": relative, "commit": receipt["commit"], "files_verified": len(files),
            "receipt_sha256": _identity(root, relative)["sha256"]}


def _verify_easy_stage(root: ProjectRoot, cancel: Callable[[], bool] | None) -> dict[str, Any]:
    relative = "spikes/22_minimax_h3_workflows/_work/local_runtime/receipts/stage_receipt_20261109T214241Z.json"
    receipt = _json_record(root, relative)
    if _identity(root, relative)["sha256"] != _EASY_STAGE_SHA256:
        raise H3SetupError("Pinned MiniMax H3 Easy stage receipt changed")
    expected = receipt.get("easy_source", {})
    if expected.get("commit") != "4b496eedeff944b2f1266c8c92df1b6c89758de0":
        raise H3SetupError("Pinned MiniMax H3 Easy plugin commit differs from frozen evidence")
    files = expected.get("files")
    if not isinstance(files, list) or not files:
        raise H3SetupError("Pinned MiniMax H3 Easy plugin file list is missing")
    for item in files:
        _verify_one(root, item["path"], int(item["bytes"]), item["sha256"], cancel)
    return {"path": relative, "commit": expected["commit"], "files_verified": len(files),
            "receipt_sha256": _identity(root, relative)["sha256"]}


def _resolve_ffmpeg(settings: Settings) -> tuple[str, str]:
    executable = str(settings.binaries.ffmpeg_exe)
    ffmpeg = str(Path(executable).resolve()) if Path(executable).is_file() else shutil.which(executable)
    if not ffmpeg:
        raise H3SetupError("Configured FFmpeg executable could not be resolved")
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    if not Path(ffprobe).is_file():
        raise H3SetupError("ffprobe beside configured FFmpeg is unavailable")
    return ffmpeg, ffprobe


def _run_capture(command: list[str], *, cwd: Path, popen: Callable[..., Any],
                 cancel: Callable[[], bool] | None, timeout: float,
                 env: dict[str, str], tree_factory: Callable[[Any], Any] | None = None) -> Any:
    """Run a bounded, cancellation-aware probe and capture its small JSON output."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("process timeout must be finite and positive")
    process = popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tree = tree_factory(process) if tree_factory else None
    deadline = time.monotonic() + timeout
    while True:
        if cancel and cancel():
            if tree:
                receipt = tree.stop()
                if not _process_tree_verified(receipt):
                    raise JobError(JobErrorKind.INTERNAL, "cancelled H3 probe left owned processes or uncertain cleanup")
                _forget_tracked(tree_factory, process)
            elif process.poll() is None:
                process.kill()
                process.wait()
            raise JobError(JobErrorKind.CANCELLED, "H3 probe cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if tree:
                receipt = tree.stop()
                if not _process_tree_verified(receipt):
                    raise JobError(JobErrorKind.INTERNAL, "timed-out H3 probe cleanup is unresolved")
                _forget_tracked(tree_factory, process)
            elif process.poll() is None:
                process.kill()
                process.wait()
            raise JobError(JobErrorKind.BUDGET_EXCEEDED, "H3 probe timed out")
        try:
            stdout, stderr = process.communicate(timeout=min(0.2, remaining))
            break
        except subprocess.TimeoutExpired:
            if tree:
                tree.refresh()
    if tree:
        receipt = tree.stop()
        if not _process_tree_verified(receipt):
            raise JobError(JobErrorKind.INTERNAL, "H3 probe process tree cleanup is unresolved")
        _forget_tracked(tree_factory, process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_owned(command: list[str], *, cwd: Path, popen: Callable[..., Any],
               cancel: Callable[[], bool] | None, timeout: float,
               env: dict[str, str] | None = None,
               tree_factory: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("process timeout must be finite and positive")
    process = popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tree = tree_factory(process) if tree_factory else None
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel and cancel():
                if tree:
                    receipt = tree.stop()
                    if not _process_tree_verified(receipt):
                        raise JobError(JobErrorKind.INTERNAL, "cancelled H3 media process cleanup is unresolved")
                    _forget_tracked(tree_factory, process)
                elif process.poll() is None:
                    process.kill()
                    process.wait()
                raise JobError(JobErrorKind.CANCELLED, "H3 media process cancelled")
            if time.monotonic() >= deadline:
                if tree:
                    receipt = tree.stop()
                    if not _process_tree_verified(receipt):
                        raise JobError(JobErrorKind.INTERNAL, "timed-out H3 media process cleanup is unresolved")
                    _forget_tracked(tree_factory, process)
                elif process.poll() is None:
                    process.kill()
                    process.wait()
                raise JobError(JobErrorKind.BUDGET_EXCEEDED, "H3 media process timed out")
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if tree:
                    tree.refresh()
        if tree:
            receipt = tree.stop()
            if not _process_tree_verified(receipt):
                raise JobError(JobErrorKind.INTERNAL, "H3 media process tree cleanup is unresolved")
            _forget_tracked(tree_factory, process)
        if process.returncode:
            message = stderr.decode("utf-8", "replace")[-3000:]
            kind = JobErrorKind.OUT_OF_MEMORY if "out of memory" in message.lower() else JobErrorKind.CORRUPT_ARTIFACT
            raise JobError(kind, f"H3 media process failed: {message}")
        return {"argv": command, "returncode": process.returncode,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_tail": stderr.decode("utf-8", "replace")[-3000:],
                "identities": tree.snapshot() if tree else [], "cleanup": receipt if tree else None}
    except BaseException:
        if process.poll() is None:
            if tree:
                cleanup = tree.stop()
                if not _process_tree_verified(cleanup):
                    raise JobError(JobErrorKind.INTERNAL, "H3 media process cleanup is unresolved")
                _forget_tracked(tree_factory, process)
            else:
                process.kill()
                process.wait()
        raise


def _process_tree_verified(receipt: dict[str, Any]) -> bool:
    return (isinstance(receipt, dict) and receipt.get("verified_stopped") is True
            and not receipt.get("survivors") and not receipt.get("failures")
            and not receipt.get("unverifiable_identities"))


def _forget_tracked(tree_factory: Callable[[Any], Any] | None, process: Any) -> None:
    owner = getattr(tree_factory, "__self__", None)
    forget = getattr(owner, "_forget_process", None)
    if callable(forget):
        forget(process)


def _hash_file(path: Path, cancel: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if cancel and cancel():
                raise JobError(JobErrorKind.CANCELLED, "H3 hashing cancelled")
            digest.update(block)
    return digest.hexdigest()


def _time_base(value: str) -> float:
    numerator, denominator = value.split("/")
    return int(numerator) / int(denominator)


def _wav_metrics(path: Path, cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Measure native-rate IEEE float WAV samples without altering their levels."""
    with path.open("rb") as stream:
        if stream.read(4) != b"RIFF":
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 extracted WAV is not a RIFF file")
        stream.read(4)
        if stream.read(4) != b"WAVE":
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 extracted WAV has no WAVE form")
        fmt: tuple[int, int, int, int] | None = None
        data_offset: int | None = None
        data_size = 0
        while True:
            header = stream.read(8)
            if len(header) != 8:
                break
            chunk, size = struct.unpack("<4sI", header)
            offset = stream.tell()
            if chunk == b"fmt ":
                raw = stream.read(size)
                if len(raw) < 16:
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 WAV format chunk is truncated")
                tag, channels, sample_rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", raw[:16])
                fmt = (tag, channels, sample_rate, bits)
            elif chunk == b"data":
                data_offset, data_size = offset, size
                break
            stream.seek(offset + size + (size & 1))
        if fmt is None or data_offset is None:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 WAV is missing format or data")
        tag, channels, sample_rate, bits = fmt
        if tag != 3 or bits != 32 or channels < 1 or sample_rate < 1 or data_size % (4 * channels):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 WAV must contain complete IEEE float32 frames")
        stream.seek(data_offset)
        values = array("f")
        remaining = data_size
        while remaining:
            if cancel and cancel():
                raise JobError(JobErrorKind.CANCELLED, "H3 WAV metrics cancelled")
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 WAV sample data is truncated")
            values.frombytes(block)
            remaining -= len(block)
        if struct.pack("=I", 1)[0] != 1:
            values.byteswap()
    if not values or any(not math.isfinite(sample) for sample in values):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 WAV is empty or contains nonfinite samples")
    frames = len(values) // channels
    squares = 0.0
    total = 0.0
    peak = 0.0
    clipped = 0
    silent = 0
    second: list[dict[str, Any]] = []
    for frame_start in range(0, frames, sample_rate):
        if cancel and cancel():
            raise JobError(JobErrorKind.CANCELLED, "H3 WAV metrics cancelled")
        frame_end = min(frames, frame_start + sample_rate)
        start, end = frame_start * channels, frame_end * channels
        chunk = values[start:end]
        chunk_total = math.fsum(chunk)
        chunk_squares = math.fsum(float(x) * float(x) for x in chunk)
        chunk_peak = max(abs(float(x)) for x in chunk)
        chunk_clipped = sum(abs(float(x)) >= 0.999 for x in chunk)
        chunk_silent = sum(abs(float(x)) <= 1e-6 for x in chunk)
        total += chunk_total
        squares += chunk_squares
        peak = max(peak, chunk_peak)
        clipped += chunk_clipped
        silent += chunk_silent
        count = len(chunk)
        second.append({"start_seconds": frame_start / sample_rate,
                       "end_seconds": frame_end / sample_rate,
                       "rms": math.sqrt(chunk_squares / count), "peak": chunk_peak,
                       "dc_offset": chunk_total / count,
                       "clipped_fraction": chunk_clipped / count,
                       "silence_fraction": chunk_silent / count})
    count = len(values)
    return {"sample_format": "float32", "sample_rate": sample_rate, "channels": channels,
            "samples_per_channel": frames,
            "rms": math.sqrt(squares / count), "peak": peak,
            "dc_offset": total / count, "clipped_fraction": clipped / count,
            "silence_fraction": silent / count, "per_second": second}


def _write_json(root: ProjectRoot, relative: str, value: Any) -> None:
    root.write_text(relative, json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def _process_cleanup_verified(receipt: dict[str, Any]) -> bool:
    """Verify physical cleanup, without conflating it with supervisor success."""
    cleanup = receipt.get("cleanup")
    return (receipt.get("status") == "stopped" and receipt.get("watchdog_joined") is True
            and isinstance(cleanup, dict) and cleanup.get("verified_stopped") is True
            and not cleanup.get("survivors") and not cleanup.get("failures")
            and not cleanup.get("unverifiable_identities")
            and not receipt.get("outer_tree_before_error") and not receipt.get("outer_tree_after_error")
            and _process_tree_verified(receipt.get("outer_tree_cleanup", {}))
            )


def _cleanup_verified(receipt: dict[str, Any]) -> bool:
    """Require physical cleanup and a successful supervisor/stop outcome."""
    if not _process_cleanup_verified(receipt):
        return False
    if receipt.get("supervisor_returncode") != 0:
        return False
    if receipt.get("error") not in (None, ""):
        return False
    if any(receipt.get(key) for key in ("watchdog_cleanup_error", "coordinator_wait_error",
                                        "stop_marker_error", "outer_tree_had_live_descendants")):
        return False
    failure = receipt.get("watchdog_failure")
    if failure is None:
        return True
    return (isinstance(failure, dict) and failure.get("type") == "InterruptedError"
            and failure.get("error") == "Creator requested session stop")


def _cleanup_failure_record(receipt: dict[str, Any], *, physical: bool) -> dict[str, Any]:
    """Return separate, serializable evidence for cleanup or outcome failure."""
    if not isinstance(receipt, dict):
        return {"type": "NativeCleanupError", "message": "shutdown receipt is not an object",
                "physical_cleanup_verified": False}
    cleanup = receipt.get("cleanup")
    physical_verified = _process_cleanup_verified(receipt)
    watchdog_failure = receipt.get("watchdog_failure")
    reasons: list[str] = []
    if physical:
        if receipt.get("status") != "stopped":
            reasons.append(f"supervisor status={receipt.get('status')!r}")
        if receipt.get("watchdog_joined") is not True:
            reasons.append("watchdog was not confirmed joined")
        if not isinstance(cleanup, dict):
            reasons.append("supervisor cleanup receipt is missing or malformed")
        else:
            if cleanup.get("verified_stopped") is not True:
                reasons.append("supervisor did not verify child-process cleanup")
            if cleanup.get("survivors"):
                reasons.append("supervisor cleanup reports survivors")
            if cleanup.get("failures"):
                reasons.append("supervisor cleanup reports failures")
            if cleanup.get("unverifiable_identities"):
                reasons.append("supervisor cleanup reports unverifiable identities")
        if receipt.get("outer_tree_before_error") or receipt.get("outer_tree_after_error"):
            reasons.append("outer process-tree identity observation failed")
        if not _process_tree_verified(receipt.get("outer_tree_cleanup", {})):
            reasons.append("outer process-tree cleanup is unverified")
    else:
        if not physical_verified:
            reasons.append("physical process cleanup could not be verified")
        if receipt.get("supervisor_returncode") != 0:
            reasons.append(f"supervisor returncode={receipt.get('supervisor_returncode')!r}")
        if receipt.get("error") not in (None, ""):
            reasons.append(f"supervisor stopped.error={receipt.get('error')!r}")
        for key, label in (("watchdog_cleanup_error", "watchdog cleanup error"),
                           ("coordinator_wait_error", "supervisor wait error"),
                           ("stop_marker_error", "STOP control error")):
            if receipt.get(key):
                reasons.append(f"{label}: {receipt[key]}")
        if receipt.get("outer_tree_had_live_descendants"):
            reasons.append("outer process tree required forced descendant cleanup")
        requested_stop = (isinstance(watchdog_failure, dict)
                          and watchdog_failure.get("type") == "InterruptedError"
                          and watchdog_failure.get("error") == "Creator requested session stop")
        if watchdog_failure is not None and not requested_stop:
            reasons.append(f"watchdog failure: {watchdog_failure!r}")
    return {"type": "NativeCleanupError", "message": "; ".join(reasons) or "shutdown outcome failed",
            "physical_cleanup_verified": physical_verified,
            "supervisor_returncode": receipt.get("supervisor_returncode"),
            "stopped_error": receipt.get("error"),
            "watchdog_cleanup_error": receipt.get("watchdog_cleanup_error"),
            "coordinator_wait_error": receipt.get("coordinator_wait_error"),
            "stop_marker_error": receipt.get("stop_marker_error"),
            "watchdog_failure": watchdog_failure}


def _same_api_graph(actual: Any, expected: dict[str, Any]) -> bool:
    """Compare exact node/input execution while ignoring Comfy's descriptive _meta."""
    if not isinstance(actual, dict) or set(actual) != set(expected):
        return False
    for node_id, expected_node in expected.items():
        actual_node = actual.get(node_id)
        if not isinstance(actual_node, dict):
            return False
        if actual_node.get("class_type") != expected_node.get("class_type"):
            return False
        if actual_node.get("inputs") != expected_node.get("inputs"):
            return False
        if set(actual_node) - {"class_type", "inputs", "_meta"}:
            return False
    return True


def _validate_live_schema(graph: dict[str, Any], schema: dict[str, Any]) -> int:
    if not isinstance(schema, dict):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "H3 /object_info schema is malformed")
    missing = sorted({node["class_type"] for node in graph.values()} - set(schema))
    if missing:
        raise H3SetupError("Installed H3 workflow nodes are missing: " + ", ".join(missing))
    for node in graph.values():
        details = schema[node["class_type"]]
        inputs = details.get("input", {})
        # A dynamic-combo input carries further inputs inside the option the graph
        # selected, and those nest again, so a dotted name such as
        # `format.codec.encoding` is never a key of the flat required/optional mapping.
        # Comparing against that flat set rejected this adapter's own correct graph; see
        # SCHEMA_VALIDATION_CONTRACT.md. Resolution follows the graph's own selections, so
        # a sub-input the chosen option does not offer stays absent and is still refused.
        resolved = resolve_node_inputs(inputs, node["inputs"])
        # Values that resolved are checked first, deliberately. An invalid dynamic-combo
        # selection makes its own sub-inputs unresolvable, so reporting absence first would
        # name the symptom (`format.codec` is missing) instead of the cause (`format` was
        # set to something the server does not offer).
        for name, value in node["inputs"].items():
            if name not in resolved:
                continue
            descriptor = resolved[name]
            if not isinstance(descriptor, list) or not descriptor:
                raise H3SetupError(f"Installed schema descriptor for {node['class_type']}.{name} is malformed")
            choices = descriptor[0]
            options = descriptor[1] if len(descriptor) > 1 and isinstance(descriptor[1], dict) else {}
            if isinstance(choices, list) and value not in choices and not isinstance(value, list):
                raise H3SetupError(f"Installed schema rejects {node['class_type']}.{name}={value!r}")
            if choices == DYNAMIC_COMBO:
                # Previously unchecked: descriptor[0] is a type name here, not a list of
                # choices, so the membership test above never applied to a dynamic combo.
                # Resolving options makes the selection checkable, so check it.
                keys = dynamic_combo_option_keys(descriptor)
                if keys is not None and value not in keys and not isinstance(value, list):
                    raise H3SetupError(f"Installed schema rejects {node['class_type']}.{name}={value!r}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if isinstance(options.get("min"), (int, float)) and value < options["min"]:
                    raise H3SetupError(f"Installed schema minimum for {node['class_type']}.{name} is {options['min']}")
                if isinstance(options.get("max"), (int, float)) and value > options["max"]:
                    raise H3SetupError(f"Installed schema maximum for {node['class_type']}.{name} is {options['max']}")
        absent = set(node["inputs"]) - set(resolved)
        if absent:
            raise H3SetupError(f"Installed schema for {node['class_type']} lacks inputs: {sorted(absent)}")
    noise = schema.get("RandomNoise", {}).get("input", {}).get("required", {}).get("noise_seed")
    try:
        maximum = int(noise[1]["max"])
    except (TypeError, ValueError, KeyError, IndexError):
        raise H3SetupError("Installed RandomNoise schema does not declare a numeric maximum seed") from None
    if maximum < 0:
        raise H3SetupError("Installed RandomNoise schema declares a negative maximum seed")
    steps = schema.get("BasicScheduler", {}).get("input", {}).get("required", {}).get("steps")
    try:
        low, high = int(steps[1].get("min", 1)), int(steps[1].get("max", 10000))
    except (TypeError, ValueError, AttributeError, IndexError):
        raise H3SetupError("Installed BasicScheduler schema does not declare a valid steps range") from None
    requested = graph["5"]["inputs"]["steps"]
    if not low <= requested <= high:
        raise H3SetupError(f"Installed BasicScheduler accepts {low}..{high} steps; requested {requested}")
    return maximum

