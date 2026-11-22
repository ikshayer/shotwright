from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import secrets
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
BASE = "spikes/23_joint_av_analyzer/_work"
MODEL_ID = "Qwen/Qwen2.5-Omni-7B"
REVISION = "ae9e1690543ffd5c0221dc27f79834d0294cba00"
PROMPT = (
    "Describe audible texture, rhythm, and changes, and the visible scene and changes "
    "during this {duration} clip. Separate direct observations from uncertain interpretations. "
    "Note uncertainty where evidence is limited; avoid inferred causes and unsupported source identities. "
    "Keep the response to 2–4 short sentences. Do not invent timecodes or events outside the clip."
)
AUDIO_PROMPT = (
    "Describe audible texture, rhythm, and changes during this {duration} clip. "
    "Separate direct observations from uncertain interpretations. Note uncertainty "
    "where evidence is limited; avoid inferred causes and unsupported source identities. "
    "Keep the response to 2–4 short sentences. Do not invent timecodes or events outside the clip."
)


@contextmanager
def allocator_warmup_disabled():
    """Disable Transformers allocator warmup for one model load, then restore it."""
    from transformers import modeling_utils

    original = modeling_utils.caching_allocator_warmup
    modeling_utils.caching_allocator_warmup = lambda *args, **kwargs: None
    try:
        yield
    finally:
        modeling_utils.caching_allocator_warmup = original


def _validate_analysis_prompt(analysis_prompt: str | None) -> str | None:
    if analysis_prompt is None:
        return None
    if not isinstance(analysis_prompt, str):
        raise TypeError("analysis_prompt must be a string or None")
    if not analysis_prompt.strip() or "\x00" in analysis_prompt:
        raise ValueError("analysis_prompt must be nonempty, non-whitespace, and contain no NUL")
    return analysis_prompt


def prompt_for_mode(mode: str, duration_seconds: float | None = None,
                    analysis_prompt: str | None = None) -> str:
    if mode == "joint":
        template = PROMPT
    elif mode == "audio":
        template = AUDIO_PROMPT
    else:
        raise ValueError(f"unsupported analysis mode: {mode}")
    if duration_seconds is None:
        duration = "short"
    else:
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
            raise TypeError("duration_seconds must be a finite positive number")
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds must be a finite positive number")
        duration = f"{duration_seconds:.2f}-second"
    override = _validate_analysis_prompt(analysis_prompt)
    if override is not None:
        return override
    return template.format(duration=duration)


def sampled_frame_count(duration: float) -> int:
    """Return the even frame budget for an actual decoded clip duration up to 25s."""
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise TypeError("duration must be a finite positive number")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be a finite positive number")
    if duration > 25:
        raise ValueError("duration must be no more than 25 seconds")
    return max(4, 2 * math.ceil(duration * 0.4))


def generation_token_ids(tokenizer: object) -> dict[str, int]:
    expected = {"eos_token_id": 151645, "pad_token_id": 151643}
    resolved = {}
    for name, expected_id in expected.items():
        token_id = getattr(tokenizer, name, None)
        if token_id is None:
            raise ValueError(f"tokenizer {name} is missing")
        if isinstance(token_id, bool) or not isinstance(token_id, int):
            raise TypeError(f"tokenizer {name} must be an integer")
        if token_id != expected_id:
            raise ValueError(f"unexpected tokenizer {name}: {token_id}; expected {expected_id}")
        resolved[name] = token_id
    return resolved


def generation_termination(token_ids: list[int], eos_token_id: int, max_new_tokens: int) -> dict:
    if not isinstance(token_ids, list):
        raise TypeError("token_ids must be a list")
    if isinstance(eos_token_id, bool) or not isinstance(eos_token_id, int) or eos_token_id < 0:
        raise TypeError("eos_token_id must be a non-negative integer")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int) or max_new_tokens <= 0:
        raise TypeError("max_new_tokens must be a positive integer")
    for index, token_id in enumerate(token_ids):
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 0:
            raise TypeError(f"token_ids[{index}] must be a non-negative integer")
    if not token_ids:
        raise ValueError("generated token IDs are empty")
    if len(token_ids) > max_new_tokens:
        raise ValueError("generated token count exceeded max_new_tokens")
    eos_positions = [index for index, token_id in enumerate(token_ids) if token_id == eos_token_id]
    if eos_positions:
        if eos_positions[0] != len(token_ids) - 1:
            raise ValueError("generated token IDs contain tokens after the first EOS")
        reason = "eos"
    elif len(token_ids) == max_new_tokens:
        reason = "token_cap"
    else:
        reason = "other"
    return {"output_token_ids": list(token_ids), "output_token_count": len(token_ids),
            "termination_reason": reason, "token_cap_reached": reason == "token_cap"}


def normalize_loading_issues(loading: dict) -> dict[str, list]:
    """Normalize native Transformers key diagnostics without discarding bad values."""
    if not isinstance(loading, dict):
        raise TypeError("loading diagnostics must be a dict")

    def safe(value: object, path: str) -> object:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"non-finite loading diagnostic at {path}")
            return value
        if isinstance(value, dict):
            converted = {}
            for key, member in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"non-string JSON object key at {path}")
                converted[key] = safe(member, f"{path}.{key}")
            return converted
        if isinstance(value, (list, tuple)):
            return [safe(member, f"{path}[{index}]") for index, member in enumerate(value)]
        raise TypeError(f"unsupported loading diagnostic value at {path}: {type(value).__name__}")

    normalized = {}
    for field in ("missing_keys", "unexpected_keys", "mismatched_keys"):
        value = loading.get(field, [])
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise TypeError(f"{field} must be a list, tuple, set, or frozenset")
        if isinstance(value, (set, frozenset)):
            converted = [safe(member, f"{field}[]") for member in value]
            converted.sort(key=lambda member: json.dumps(
                member, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        else:
            converted = safe(value, field)
        normalized[field] = converted
    return normalized


_process_tree_module = None


def _load_process_tree_module():
    global _process_tree_module
    if _process_tree_module is None:
        helper_path = PROJECT.resolve("spikes/22_minimax_h3_workflows/local_h3.py")
        spec = importlib.util.spec_from_file_location("_analyzer_owned_process_helpers", helper_path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot load the existing owned process tree helper")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _process_tree_module = module
    return _process_tree_module


def OwnedProcessTree(process: subprocess.Popen):
    """Factory for the existing, independently lifecycle-verified spike helper."""
    module = _load_process_tree_module()
    return module.OwnedProcessTree(process)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def contained(path_text: str) -> Path:
    path = Path(path_text).resolve()
    try:
        path.relative_to(PROJECT.path)
    except ValueError as exc:
        raise ValueError("media path must be inside the project") from exc
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _ram_available() -> int:
    if os.name == "nt":
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                        ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended", ctypes.c_ulonglong)]
        status = MemoryStatus(); status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return int(status.avail_phys)
    pages = os.sysconf("SC_AVPHYS_PAGES")
    return int(pages * os.sysconf("SC_PAGE_SIZE"))


def decode_media(media: Path, mode: str, duration_limit: float) -> tuple[list, object, dict]:
    """Decode embedded streams directly; no TorchCodec, subprocess, or network fetch."""
    import av
    import numpy as np
    container = av.open(str(media), mode="r")
    videos = [s for s in container.streams if s.type == "video"]
    audios = [s for s in container.streams if s.type == "audio"]
    if not audios or (mode == "joint" and not videos):
        container.close()
        raise ValueError("requested modality is missing from the media")
    rate = 16000
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=rate)
    audio_parts, frames, frame_times = [], [], []
    audio_origin = None
    decoded_audio_samples = 0
    for packet in container.demux([*([videos[0]] if mode == "joint" else []), audios[0]]):
        for frame in packet.decode():
            if frame.__class__.__name__ == "AudioFrame":
                decoded_audio_samples += int(frame.samples * rate / frame.sample_rate)
                if decoded_audio_samples > duration_limit * rate + rate:
                    container.close()
                    raise ValueError(f"decoded audio exceeded the {duration_limit:.1f}s media bound")
                if audio_origin is None and frame.pts is not None:
                    audio_origin = float(frame.pts * frame.time_base)
                for out in resampler.resample(frame):
                    audio_parts.append(out.to_ndarray().reshape(-1).copy())
            elif mode == "joint" and frame.__class__.__name__ == "VideoFrame":
                if frame.pts is None:
                    raise ValueError("video frame has no PTS; timing cannot be audited")
                if frame_times and float(frame.pts * frame.time_base) - frame_times[0] > duration_limit + 1:
                    container.close()
                    raise ValueError(f"decoded video exceeded the {duration_limit:.1f}s media bound")
                frames.append(frame.to_ndarray(format="rgb24"))
                frame_times.append(float(frame.pts * frame.time_base))
    for out in resampler.resample(None):
        audio_parts.append(out.to_ndarray().reshape(-1).copy())
    container.close()
    if not audio_parts:
        raise ValueError("embedded audio decoded to no samples")
    waveform = np.concatenate(audio_parts).astype(np.float32, copy=False)
    if not np.isfinite(waveform).all():
        raise ValueError("decoded audio contains non-finite samples")
    duration = len(waveform) / rate
    if duration <= 0 or duration > duration_limit:
        raise ValueError(f"decoded audio duration {duration:.3f}s exceeds limit {duration_limit:.1f}s")
    selected, selected_times = [], []
    av_start_offset = None
    sample_errors = []
    frame_budget = 0
    if mode == "joint":
        frame_budget = sampled_frame_count(duration)
        if len(frames) < frame_budget:
            raise ValueError(f"joint mode requires at least {frame_budget} decoded frames, got {len(frames)}")
        if audio_origin is None or not frame_times:
            raise ValueError("audio/video stream start timestamps are unavailable")
        av_start_offset = audio_origin - frame_times[0]
        cadence = max(float(videos[0].average_rate.denominator / videos[0].average_rate.numerator)
                      if videos[0].average_rate else 0.1, 1 / 120)
        if abs(av_start_offset) > cadence:
            raise ValueError(f"audio/video stream starts differ by {av_start_offset:.3f}s (frame cadence {cadence:.3f}s)")
        # Uniform endpoint-excluded targets use the actual decoded audio duration.
        for target in np.linspace(0, duration, frame_budget, endpoint=False):
            idx = min(range(len(frame_times)), key=lambda i: abs(frame_times[i] - (frame_times[0] + target)))
            error = abs(frame_times[idx] - (frame_times[0] + target))
            sample_errors.append(error)
            if not selected or idx != selected[-1]:
                selected.append(idx)
                selected_times.append(frame_times[idx] - frame_times[0])
        if max(sample_errors, default=0) > cadence:
            raise ValueError(f"video frame sampling error {max(sample_errors):.3f}s exceeds frame cadence {cadence:.3f}s")
        if len(selected) < frame_budget:
            raise ValueError(f"video cadence cannot provide {frame_budget} distinct sampled frames")
    return [frames[i] for i in selected], waveform, {
        "audio_sample_rate_hz": rate, "audio_samples": int(waveform.size), "audio_duration_seconds": duration,
        "audio_first_pts_seconds": audio_origin, "video_decoded_frames": len(frames),
        "video_first_pts_seconds": frame_times[0] if frame_times else None,
        "audio_minus_video_start_seconds": av_start_offset,
        "video_frame_cadence_seconds": cadence if mode == "joint" else None,
        "video_sample_target_error_seconds": sample_errors,
        "video_sample_pts_relative_seconds": selected_times, "video_frame_budget": frame_budget,
        "video_frames_sampled": len(selected),
        "video_sample_fps": len(selected) / duration if mode == "joint" else None,
    }


def load_model() -> tuple[object, object, dict, list[str], list[str], str, int, dict]:
    import torch
    from transformers import BitsAndBytesConfig, Qwen2_5OmniProcessor
    from transformers.models.qwen2_5_omni.modeling_qwen2_5_omni import (
        Qwen2_5OmniThinkerForConditionalGeneration,
    )

    if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
        raise RuntimeError("CUDA device 0 is required; CPU fallback is disabled")
    props = torch.cuda.get_device_properties(0)
    free_gpu = int(torch.cuda.mem_get_info(0)[0])
    if free_gpu < 8 * 1024**3:
        raise RuntimeError("Need at least 8 GiB free on CUDA device 0")
    if _ram_available() < 6 * 1024**3:
        raise RuntimeError("Need at least 6 GiB available RAM")
    model_dir = PROJECT.resolve(f"{BASE}/model")
    manifest_path = model_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("verified model manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_files = []
    for row in manifest:
        shard = model_dir / row["filename"]
        if not shard.is_file() or shard.stat().st_size != row["size"] or digest(shard) != row["sha256"]:
            raise RuntimeError(f"local model shard provenance check failed: {row['filename']}")
        model_files.append({"filename": row["filename"], "size": row["size"], "sha256": row["sha256"]})
    api_snapshot = model_dir / "api_metadata.json"
    if not api_snapshot.is_file():
        raise RuntimeError("pinned API metadata snapshot is missing")
    api_hash = hashlib.sha256(json.dumps(json.loads(api_snapshot.read_text(encoding="utf-8")),
                                         sort_keys=True).encode()).hexdigest()
    metadata_receipts = []
    for receipt_path in model_dir.glob("metadata_receipt*.json"):
        receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt_data.get("api_metadata_sha256") == api_hash:
            metadata_receipts.append(receipt_path)
    if not metadata_receipts:
        raise RuntimeError("no metadata receipt matches the retained pinned API snapshot")
    weights_receipts = sorted(model_dir.glob("weights_receipt*.json"), key=lambda path: path.name)
    if not weights_receipts:
        raise RuntimeError("verified model weights receipt is missing")
    runtime_receipts = sorted((PROJECT.resolve(BASE)).glob("*runtime*receipt*.json"), key=lambda path: path.name)
    processor = Qwen2_5OmniProcessor.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                               bnb_4bit_compute_dtype=torch.bfloat16, llm_int8_skip_modules=["model.audio_tower", "model.visual", "lm_head"])
    with allocator_warmup_disabled():
        model, loading = Qwen2_5OmniThinkerForConditionalGeneration.from_pretrained(
            model_dir, local_files_only=True, trust_remote_code=False, use_safetensors=True,
            torch_dtype=torch.bfloat16, quantization_config=quant, device_map={"": 0}, attn_implementation="sdpa",
            output_loading_info=True,
        )
    issues = normalize_loading_issues(loading)
    if any(issues.values()):
        raise RuntimeError(f"checkpoint loading issues: {issues}")
    names = [(name, module) for name, module in model.named_modules() if module.__class__.__name__ in ("Linear4bit", "Linear")]
    quantized = [name for name, module in names if module.__class__.__name__ == "Linear4bit"]
    unquantized = [name for name, module in names if module.__class__.__name__ == "Linear"]
    if not quantized or not unquantized:
        raise RuntimeError(f"unexpected quantization coverage: NF4={len(quantized)}, BF16/other Linear={len(unquantized)}")
    if any("audio_tower" not in n and "visual" not in n and "lm_head" not in n for n in unquantized):
        raise RuntimeError("an eligible language Linear was not NF4-quantized")
    if any("audio_tower" in n or "visual" in n for n in quantized):
        raise RuntimeError("an audio/vision encoder Linear was unexpectedly quantized")
    for name, module in model.named_modules():
        if "audio_tower" in name or "visual" in name:
            for parameter_name, parameter in module.named_parameters(recurse=False):
                if parameter.is_floating_point() and parameter.dtype != torch.bfloat16:
                    raise RuntimeError(f"encoder parameter is not BF16: {name}.{parameter_name} ({parameter.dtype})")
    import importlib.metadata
    runtime_versions = {}
    for package in ("torch", "transformers", "bitsandbytes", "accelerate", "av", "numpy", "Pillow"):
        try:
            runtime_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            runtime_versions[package] = None
    provenance = {"manifest_sha256": digest(manifest_path), "shards": model_files,
                  "api_snapshot_canonical_sha256": api_hash,
                  "metadata_receipt_sha256": digest(metadata_receipts[-1]),
                  "weights_receipt_sha256": digest(weights_receipts[-1]),
                  "runtime_receipt_hashes": {path.name: digest(path) for path in runtime_receipts},
                  "runtime_versions": runtime_versions,
                  "allocator_warmup_policy": "disabled_during_from_pretrained_only"}
    return processor, model, issues, quantized, unquantized, props.name, free_gpu, provenance


def analyze_one(processor: object, model: object, media: Path, start: float, mode: str,
                max_new_tokens: int, duration_limit: float,
                analysis_prompt: str | None = None) -> dict:
    _validate_analysis_prompt(analysis_prompt)
    import torch
    from PIL import Image
    frames, waveform, decoded = decode_media(media, mode, duration_limit)
    content = [{"type": "video"}] if mode == "joint" else [{"type": "audio", "audio": waveform}]
    prompt = prompt_for_mode(mode, decoded["audio_duration_seconds"], analysis_prompt)
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}, *content]}]
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    video_kwargs = {}
    if mode == "joint":
        video_kwargs = {"videos_kwargs": {"fps": decoded["video_frames_sampled"] / decoded["audio_duration_seconds"], "do_sample_frames": False,
                          "video_metadata": [{"total_num_frames": decoded["video_frames_sampled"], "fps": decoded["video_frames_sampled"] / decoded["audio_duration_seconds"],
                             "duration": decoded["audio_duration_seconds"], "height": frames[0].shape[0], "width": frames[0].shape[1],
                             "frames_indices": list(range(len(frames))), "video_backend": "pyav"}],
                          "size": {"shortest_edge": 64 * 28 * 28, "longest_edge": 128 * 28 * 28}}}
    inputs = processor(text=[text], audio=[waveform], videos=[[Image.fromarray(f) for f in frames]] if mode == "joint" else None,
                       sampling_rate=16000, use_audio_in_video=(mode == "joint"), return_tensors="pt", **video_kwargs)
    aud = inputs.get("input_features")
    vid = inputs.get("pixel_values_videos")
    if mode == "joint" and (aud is None or not aud.numel() or vid is None or not vid.numel()):
        raise RuntimeError("joint preprocessing did not produce nonempty audio features and video frames")
    if mode == "audio" and (aud is None or not aud.numel() or vid is not None):
        raise RuntimeError("audio mode requires audio features and no video tensors")
    for key, value in inputs.items():
        if isinstance(value, torch.Tensor) and value.is_floating_point() and not torch.isfinite(value).all():
            raise RuntimeError(f"processor returned non-finite values in {key}")
    device = torch.device("cuda:0")
    for key, value in list(inputs.items()):
        if isinstance(value, torch.Tensor):
            inputs[key] = value.to(device=device, dtype=torch.bfloat16 if value.is_floating_point() else value.dtype)
    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    started = time.monotonic()
    token_ids = generation_token_ids(processor.tokenizer)
    generation_kwargs = {"do_sample": False, "use_audio_in_video": mode == "joint",
                         "max_new_tokens": max_new_tokens, **token_ids}
    with torch.inference_mode():
        generated = model.generate(**inputs, **generation_kwargs)
    output_ids = generated[0, inputs["input_ids"].shape[1]:].tolist()
    termination = generation_termination(output_ids, token_ids["eos_token_id"], max_new_tokens)
    text = processor.batch_decode([termination["output_token_ids"]], skip_special_tokens=True)[0]
    return {"text": text, "prompt": prompt, "decoded_media": decoded,
            "effective_generation_kwargs": generation_kwargs, **termination,
            "tensor_shapes": {key: list(value.shape) for key, value in inputs.items() if isinstance(value, torch.Tensor)},
            "elapsed_seconds": time.monotonic() - started}


def _worker(items: list[dict], mode: str, max_new_tokens: int, duration_limit: float, result_rel: str,
            analysis_prompt: str | None = None) -> int:
    """Worker process: one model load for one or more sequential media windows."""
    results = []
    try:
        _validate_analysis_prompt(analysis_prompt)
        processor, model, issues, quantized, unquantized, gpu, free_gpu, provenance = load_model()
        for item in items:
            result = analyze_one(processor, model, Path(item["media"]), item["start"], mode,
                                 max_new_tokens, duration_limit, analysis_prompt)
            candidate = {**result, "loading_info": issues, "quantized_linear4bit_count": len(quantized),
                            "quantized_linear4bit_sha256": hashlib.sha256("\n".join(quantized).encode()).hexdigest(),
                            "unquantized_linear_count": len(unquantized),
                            "unquantized_linear_sha256": hashlib.sha256("\n".join(unquantized).encode()).hexdigest(),
                            "cuda_free_bytes_before_load": free_gpu, "gpu": gpu, "model_provenance": provenance}
            json.dumps(candidate, ensure_ascii=False, allow_nan=False)
            results.append(candidate)
            PROJECT.write_text(result_rel, json.dumps({"status": "running", "results": results}, ensure_ascii=False,
                                                       allow_nan=False))
        coverage = {"quantized_linear4bit": quantized, "unquantized_linear": unquantized}
        coverage_rel = result_rel.removesuffix("worker_result.json") + "module_coverage.json"
        PROJECT.write_text(coverage_rel, json.dumps(coverage, ensure_ascii=False, allow_nan=False))
        PROJECT.write_text(result_rel, json.dumps({"status": "success", "results": results,
                          "module_coverage_path": coverage_rel, "module_coverage_sha256": digest(PROJECT.resolve(coverage_rel))},
                          ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - worker must serialize every failure into its receipt
        failure = {"status": "failure", "error_type": type(exc).__name__, "error": str(exc),
                   "traceback": traceback.format_exc(), "results": results}
        if "quantized" in locals() and "unquantized" in locals():
            coverage = {"quantized_linear4bit": quantized, "unquantized_linear": unquantized}
            coverage_rel = result_rel.removesuffix("worker_result.json") + "module_coverage.json"
            PROJECT.write_text(coverage_rel, json.dumps(coverage, ensure_ascii=False, allow_nan=False))
            failure["module_coverage_path"] = coverage_rel
            failure["module_coverage_sha256"] = digest(PROJECT.resolve(coverage_rel))
        PROJECT.write_text(result_rel, json.dumps(failure, ensure_ascii=False, allow_nan=False))
        return 1


def _process_created_utc(process: subprocess.Popen) -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]
    created, exited, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
    if not ctypes.windll.kernel32.GetProcessTimes(process._handle, ctypes.byref(created), ctypes.byref(exited),
                                                   ctypes.byref(kernel), ctypes.byref(user)):
        return None
    ticks = (created.high << 32) | created.low
    epoch = (ticks - 116444736000000000) / 10_000_000
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat()


def _gpu_memory_sample() -> dict:
    command = ["nvidia-smi", "--query-gpu=index,name,uuid,memory.used,memory.free",
               "--format=csv,noheader,nounits"]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    if proc.returncode:
        raise RuntimeError(f"nvidia-smi failed ({proc.returncode}): {proc.stderr[-500:]}")
    rows = []
    for line in proc.stdout.splitlines():
        cols = [part.strip() for part in line.split(",")]
        if len(cols) == 5:
            rows.append({"index": int(cols[0]), "name": cols[1], "uuid": cols[2],
                         "used_mib": int(cols[3]), "free_mib": int(cols[4])})
    gpu = next((row for row in rows if row["index"] == 0), None)
    if gpu is None:
        raise RuntimeError("nvidia-smi did not report CUDA device 0")
    return gpu


def run_supervised(items: list[dict], mode: str, max_new_tokens: int, duration_limit: float,
                   result_rel: str, stdout_rel: str, stderr_rel: str,
                   resources_rel: str, analysis_prompt: str | None = None, *,
                   should_stop: Callable[[], bool] | None = None) -> tuple[dict, dict]:
    """Launch only the isolated runtime worker and enforce the 900s/low-RAM limits."""
    _validate_analysis_prompt(analysis_prompt)
    env_dir = PROJECT.resolve(f"{BASE}/env")
    python = env_dir / "Scripts" / "python.exe" if os.name == "nt" else env_dir / "bin" / "python"
    if not python.is_file():
        raise FileNotFoundError(f"isolated inference interpreter is missing: {python}")
    tmp = PROJECT.resolve(f"{BASE}/temp"); cache = PROJECT.resolve(f"{BASE}/cache")
    cuda_cache = PROJECT.resolve(f"{BASE}/cuda_cache")
    PROJECT.make_dir(f"{BASE}/temp"); PROJECT.make_dir(f"{BASE}/cache"); PROJECT.make_dir(f"{BASE}/cuda_cache")
    child_env = os.environ.copy()
    child_env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1",
                     TRANSFORMERS_OFFLINE="1", TMP=str(tmp), TEMP=str(tmp), TMPDIR=str(tmp),
                     XDG_CACHE_HOME=str(cache), HF_HOME=str(cache / "hf"), TORCH_HOME=str(cache / "torch"),
                     CUDA_CACHE_PATH=str(cuda_cache), TORCHINDUCTOR_CACHE_DIR=str(cache / "inductor"),
                     TRITON_CACHE_DIR=str(cache / "triton"), TORCH_EXTENSIONS_DIR=str(cache / "extensions"))
    child_env.pop("PYTHONPATH", None); child_env.pop("PYTHONHOME", None)
    gpu_before = _gpu_memory_sample()
    _load_process_tree_module()
    PROJECT.append_line(resources_rel, json.dumps({"event": "gpu_baseline", "gpu_device": gpu_before}, separators=(",", ":")))
    import threading

    cmd = [str(python), "-B", str(Path(__file__).resolve()), "--_worker-json",
           json.dumps({"items": items, "mode": mode, "max_new_tokens": max_new_tokens,
                       "duration_limit": duration_limit, "result_rel": result_rel,
                       "analysis_prompt": analysis_prompt}, separators=(",", ":"))]
    process = subprocess.Popen(cmd, cwd=str(PROJECT.path), env=child_env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    stdout_thread = stderr_thread = None
    stdout_tail, stderr_tail = bytearray(), bytearray()
    tree = None
    tree_events = []
    try:
        stop_poll_shutdown = threading.Event()
        caller_stop_event = threading.Event()
        stop_poll_errors = []
        stop_poll_thread = None
        if should_stop is not None:
            def poll_for_caller_stop() -> None:
                while not stop_poll_shutdown.is_set():
                    try:
                        if should_stop():
                            caller_stop_event.set()
                            return
                    except BaseException as exc:  # noqa: BLE001 - surface callback failures after owned cleanup.
                        stop_poll_errors.append(exc)
                        caller_stop_event.set()
                        return
                    if stop_poll_shutdown.wait(0.25):
                        return

            stop_poll_thread = threading.Thread(
                target=poll_for_caller_stop, name="qwen-caller-stop-poll", daemon=True,
            )
            stop_poll_thread.start()

        def drain(stream: object, target: bytearray) -> None:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                target.extend(chunk)
                if len(target) > 2 * 1024**2:
                    del target[:len(target) - 2 * 1024**2]
        stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_tail), daemon=True)
        stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_tail), daemon=True)
        stdout_thread.start(); stderr_thread.start()
        tree = OwnedProcessTree(process)
        tree.refresh()
        tree_events = []
        identity = {"pid": process.pid, "created_utc": _process_created_utc(process), "image": str(python), "cmd": cmd}
        samples, low_samples, stopped = [], 0, None
        caller_cancelled = False
        began = time.monotonic()
        next_resource_sample = began
        snapshot = tree.snapshot()
        identity["process_tree"] = snapshot
        while True:
            if stop_poll_errors:
                raise stop_poll_errors[0]
            if caller_stop_event.is_set():
                stopped = "caller cancelled"
                caller_cancelled = True
                tree_events = tree.stop()
                break
            tree.refresh()
            live_tree = [row for row in tree.snapshot() if row.get("live_identity_verified") is True]
            if process.poll() is not None and not live_tree:
                break
            now = time.monotonic() if should_stop is not None else None
            if should_stop is None or now >= next_resource_sample:
                available = _ram_available()
                process_memory = tree.sample()
                rss_rows = process_memory["processes"]
                worker_rss = process_memory["aggregate_memory_bytes"]["rss"]
                gpu_now = _gpu_memory_sample()
                elapsed = time.monotonic() - began if should_stop is None else now - began
                sample = {"elapsed_seconds": elapsed, "available_ram_bytes": available,
                          "owned_worker_rss_bytes": worker_rss, "owned_processes": rss_rows,
                          "process_tree_identities": process_memory["identity_snapshot"], "gpu_device": gpu_now}
                samples.append(sample)
                PROJECT.append_line(resources_rel, json.dumps({"event": "sample", **sample}, separators=(",", ":")))
                low_samples = low_samples + 1 if available < 2 * 1024**3 else 0
                if low_samples >= 3:
                    stopped = "available RAM below 2 GiB for three consecutive samples"
                elif ((time.monotonic() - began if should_stop is None else now - began) >= 900):
                    stopped = "900 second inference deadline exceeded"
                if stopped:
                    tree_events = tree.stop()
                    break
                if should_stop is not None:
                    next_resource_sample = now + 5
            time.sleep(0.25 if should_stop is not None else 5)
        if not stopped:
            tree_events = tree.stop()
    except BaseException as supervisor_error:
        cleanup_error = None
        if tree is not None:
            try:
                tree_events = tree.stop()
            except Exception as exc:  # noqa: BLE001 - persist cleanup failure alongside the primary supervisor failure
                cleanup_error = f"{type(exc).__name__}: {exc}"
                if process.poll() is None:
                    process.kill()
        elif process.poll() is None:
            process.kill()
        process.wait()
        try:
            cleanup_record = {"event": "supervisor_failure", "error": f"{type(supervisor_error).__name__}: {supervisor_error}",
                              "owned_process_tree_cleanup": tree_events, "cleanup_error": cleanup_error,
                              "owned_process_identities": tree.snapshot() if tree is not None else []}
            PROJECT.append_line(resources_rel, json.dumps(cleanup_record, separators=(",", ":")))
        except Exception as telemetry_error:  # noqa: BLE001 - report failure to retain supervisor diagnostics
            raise RuntimeError(f"failed to persist supervisor cleanup evidence: {telemetry_error}") from supervisor_error
        if stdout_thread and stdout_thread.ident is not None: stdout_thread.join(timeout=5)
        if stderr_thread and stderr_thread.ident is not None: stderr_thread.join(timeout=5)
        PROJECT.write_text(stdout_rel, stdout_tail.decode("utf-8", errors="replace"))
        PROJECT.write_text(stderr_rel, stderr_tail.decode("utf-8", errors="replace"))
        raise
    finally:
        if "stop_poll_shutdown" in locals():
            stop_poll_shutdown.set()
        if "stop_poll_thread" in locals() and stop_poll_thread is not None:
            stop_poll_thread.join(timeout=1)
    process.wait(timeout=30)
    stdout_thread.join(timeout=5); stderr_thread.join(timeout=5)
    stdout_text = stdout_tail.decode("utf-8", errors="replace")
    stderr_text = stderr_tail.decode("utf-8", errors="replace")
    PROJECT.write_text(stdout_rel, stdout_text)
    PROJECT.write_text(stderr_rel, stderr_text)
    gpu_after = _gpu_memory_sample()
    PROJECT.append_line(resources_rel, json.dumps({"event": "gpu_post", "gpu_device": gpu_after}, separators=(",", ":")))
    result_path = PROJECT.resolve(result_rel)
    worker_result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
    gpu_samples = [sample["gpu_device"] for sample in samples]
    gpu_peak = max(gpu_samples, key=lambda row: row["used_mib"]) if gpu_samples else gpu_before
    telemetry = {"owned_process": identity, "owned_process_identities_final": tree.snapshot(),
                 "owned_process_tree_cleanup": tree_events, "duration_seconds": time.monotonic() - began,
                 "ram_available_samples": samples, "gpu_device_baseline": gpu_before,
                 "gpu_device_peak_sample": gpu_peak, "gpu_device_post": gpu_after,
                 "owned_worker_peak_rss_bytes": max((s["owned_worker_rss_bytes"] or 0 for s in samples), default=0),
                 "stdout_log_path": stdout_rel, "stderr_log_path": stderr_rel,
                 "resources_path": resources_rel, "resources_sha256": digest(PROJECT.resolve(resources_rel))}
    if stopped:
        return {"status": "failure", "error_type": "CallerCancelled" if caller_cancelled else "WorkerStopped", "error": stopped,
                "results": worker_result.get("results", []) if worker_result else []}, telemetry
    if worker_result is None:
        return {"status": "failure", "error_type": "WorkerProtocolError",
                "error": f"worker exited {process.returncode} without a result receipt"}, telemetry
    if process.returncode != 0 and worker_result.get("status") == "success":
        return {"status": "failure", "error_type": "WorkerExitError",
                "error": f"worker exited with code {process.returncode} despite success receipt"}, telemetry
    if worker_result.get("status") == "running":
        worker_result["status"] = "failure"
        worker_result.setdefault("error_type", "WorkerExitedEarly")
        worker_result.setdefault("error", f"worker exited with code {process.returncode} before completion")
    return worker_result, telemetry


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Qwen2.5-Omni Thinker AV analysis")
    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("inspect", help="print pinned model/run policy without loading weights")
    run = sub.add_parser("run")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--media")
    source.add_argument("--manifest", help="media acquisition receipt; processes its windows sequentially")
    run.add_argument("--start", type=float, help="absolute source timeline seconds (required with --media)")
    run.add_argument("--mode", choices=("joint", "audio"), default="joint")
    run.add_argument("--max-new-tokens", type=int, default=192)
    run.add_argument("--duration-limit", type=float, default=8.0, help="maximum decoded media duration (<=25 seconds)")
    run.add_argument("--prompt-file", help="contained UTF-8 file with an exact analysis prompt override")
    parser.add_argument("--_worker-json", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if getattr(args, "_worker_json", None):
        spec = json.loads(args._worker_json)
        raise SystemExit(_worker(spec["items"], spec["mode"], spec["max_new_tokens"],
                                 spec["duration_limit"], spec["result_rel"],
                                 spec.get("analysis_prompt")))
    if args.command == "inspect":
        print(json.dumps({"model": MODEL_ID, "revision": REVISION, "offline": True, "modes": ["joint", "audio"],
                          "script_sha256": digest(Path(__file__).resolve()),
                          "prompts": {"joint": prompt_for_mode("joint"), "audio": prompt_for_mode("audio")},
                          "resource_policy": {"available_ram_bytes": _ram_available(), "minimum_ram_bytes": 6*1024**3,
                                              "minimum_gpu_free_bytes": 8*1024**3, "minimum_disk_free_bytes": 8*1024**3},
                          "decoder": "PyAV explicit embedded stream decode",
                          "allocator_warmup_policy": "disabled_during_from_pretrained_only"}, indent=2))
        return
    if not 1 <= args.max_new_tokens <= 768:
        parser.error("--max-new-tokens must be 1..768")
    if not 0 < args.duration_limit <= 25:
        parser.error("--duration-limit must be >0 and <=25 seconds")
    if args.media and args.start is None:
        parser.error("--start is required with --media")
    if args.manifest and args.start is not None:
        parser.error("--start cannot be used with --manifest")
    analysis_prompt = None
    prompt_file_path = prompt_file_sha256 = None
    if args.prompt_file is not None:
        prompt_path = contained(args.prompt_file)
        prompt_file_path = prompt_path.relative_to(PROJECT.path).as_posix()
        prompt_bytes = PROJECT.read_bytes(prompt_file_path)
        prompt_file_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
        try:
            analysis_prompt = prompt_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("prompt file must contain valid UTF-8") from exc
        _validate_analysis_prompt(analysis_prompt)
    expected_env = PROJECT.resolve(f"{BASE}/env")
    if Path(sys.prefix).resolve() != expected_env.resolve():
        raise SystemExit(
            "Inference must run from the isolated spike environment "
            f"({expected_env}); the bootstrap environment is inspect-only"
        )
    disk = __import__("shutil").disk_usage(PROJECT.resolve(BASE))
    items = []
    if args.media:
        media = contained(args.media)
        items.append({"media": str(media), "start": args.start})
    else:
        manifest_path = contained(args.manifest)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        windows = manifest.get("windows")
        if not isinstance(windows, list) or len(windows) != 7:
            raise ValueError("batch requires exactly seven acquisition-manifest windows")
        for window in windows:
            window_path = contained(str(PROJECT.resolve(window["path"])))
            if digest(window_path) != window.get("sha256"):
                raise ValueError(f"media manifest SHA256 mismatch: {window['path']}")
            items.append({"media": str(window_path), "start": float(window["absolute_start_seconds"]),
                          "relative_start_seconds": float(window["relative_start_seconds"]),
                          "media_sha256": window["sha256"]})
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(4)
    run_rel = f"{BASE}/runs/{stamp}"
    PROJECT.make_dir(run_rel)
    output_rel = f"{run_rel}/analysis.json" if len(items) == 1 else None
    base_receipt = {"model_id": MODEL_ID, "revision": REVISION, "mode": args.mode,
               "prompt": prompt_for_mode(args.mode, analysis_prompt=analysis_prompt),
               "seed": 0, "do_sample": False, "max_new_tokens": args.max_new_tokens,
               "duration_limit_seconds": args.duration_limit,
               "allocator_warmup_policy": "disabled_during_from_pretrained_only",
               "script_sha256": digest(Path(__file__).resolve()),
               "runtime": {"python": sys.version, "platform": sys.platform}, "disk_free_bytes_before": disk.free,
               "created_utc": dt.datetime.now(dt.UTC).isoformat()}
    if prompt_file_path is not None:
        base_receipt.update(analysis_prompt_path=prompt_file_path,
                            analysis_prompt_sha256=prompt_file_sha256)
    try:
        if disk.free < 8 * 1024**3:
            raise RuntimeError("Need at least 8 GiB free disk")
        worker_result_rel = f"{run_rel}/worker_result.json"
        stdout_rel = f"{run_rel}/worker_stdout.log"
        stderr_rel = f"{run_rel}/worker_stderr.log"
        resources_rel = f"{run_rel}/resources.jsonl"
        result, telemetry = run_supervised(items, args.mode, args.max_new_tokens, args.duration_limit,
                                           worker_result_rel, stdout_rel, stderr_rel, resources_rel,
                                           analysis_prompt)
        telemetry["worker_result_path"] = worker_result_rel
        if result.get("module_coverage_path"):
            telemetry["module_coverage_path"] = result["module_coverage_path"]
            telemetry["module_coverage_sha256"] = result.get("module_coverage_sha256")
        if len(items) == 1:
            item = items[0]
            one_result = result.get("results", [{}])[0] if result.get("status") == "success" else result
            one_text = one_result.pop("text", None)
            receipts = [{**base_receipt, "media_path": str(Path(item["media"]).relative_to(PROJECT.path)).replace("\\", "/"),
                        "media_sha256": digest(Path(item["media"])), "source_start_seconds": item["start"],
                        "telemetry": telemetry, **one_result}]
            if one_text is not None:
                receipts[0]["output_text"] = one_text
        else:
            receipts = []
            worker_results = result.get("results", [])
            for idx, item in enumerate(items):
                one = {**base_receipt, "media_path": str(Path(item["media"]).relative_to(PROJECT.path)).replace("\\", "/"),
                       "media_sha256": item["media_sha256"], "source_start_seconds": item["start"],
                       "relative_start_seconds": item["relative_start_seconds"], "telemetry": telemetry}
                if idx < len(worker_results):
                    one.update(status="success", output_text=worker_results[idx].pop("text"), **worker_results[idx])
                else:
                    one.update(status="failure", error=result.get("error", "batch worker failed before this window"),
                               error_type=result.get("error_type", "WorkerFailure"))
                rel = f"{run_rel}/window_{idx+1:02d}.json"
                PROJECT.write_text(rel, json.dumps(one, indent=2, ensure_ascii=False) + "\n")
                receipts.append(one)
        if len(items) == 1:
            receipts[0]["status"] = result.get("status", "failure")
            PROJECT.write_text(output_rel, json.dumps(receipts[0], indent=2, ensure_ascii=False) + "\n")
    except Exception as exc:
        failure = {**base_receipt, "status": "failure", "error_type": type(exc).__name__,
                   "error": str(exc), "traceback": traceback.format_exc()}
        if "worker_result_rel" in locals():
            failure["worker_result_path"] = worker_result_rel
        if "resources_rel" in locals() and PROJECT.resolve(resources_rel).is_file():
            failure["resources_path"] = resources_rel
            failure["resources_sha256"] = digest(PROJECT.resolve(resources_rel))
        for log_rel in ("stdout_rel", "stderr_rel"):
            if log_rel in locals():
                failure[log_rel.removesuffix("_rel") + "_path"] = locals()[log_rel]
        PROJECT.write_text(f"{run_rel}/failure.json", json.dumps(failure, indent=2, ensure_ascii=False) + "\n")
        raise
    print(PROJECT.resolve(output_rel or run_rel))
    if any(receipt.get("status") != "success" for receipt in receipts):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
