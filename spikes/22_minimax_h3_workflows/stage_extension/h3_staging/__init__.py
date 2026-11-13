from __future__ import annotations

import gc
import hashlib
import json
import os
import threading
import time
import uuid
import weakref
from datetime import UTC, datetime
from pathlib import Path

# Comfy and torch are deliberately imported only inside node execution methods.
NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS = {
    "CCTVH3ConditionAndRelease": "CCTV H3 Condition and Release",
    "CCTVH3StagedUNETLoader": "CCTV H3 Staged UNET Loader",
    "CCTVH3DecodeVAELoader": "CCTV H3 Decode VAE Loader",
}

_LOCK = threading.RLock()
_ISSUED: dict[str, dict] = {}
_CONSUMED: set[str] = set()
_CATALOG: dict | None = None


def _context():
    here = Path(__file__).resolve()
    root = here.parents[4]
    if str(root) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(root))
    from orchestrator.platform.containment import ProjectRoot
    project = ProjectRoot(root)
    run_rel = os.environ.get("CCTV_H3_RUN_REL", "")
    prefix = "spikes/22_minimax_h3_workflows/_work/local_runs/"
    if not run_rel.startswith(prefix) or ".." in Path(run_rel).parts or "\\" in run_rel:
        raise ValueError("CCTV_H3_RUN_REL is outside the allowed current-run directory")
    project.resolve(run_rel)
    return root, project, run_rel


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _, project, _ = _context()
        _CATALOG = json.loads(project.read_text("spikes/22_minimax_h3_workflows/LOCAL_MODEL_CATALOG.json"))
    return {item["role"]: Path(item["model_relative_path"]).name for item in _CATALOG["components"]}


def _event(phase: str, invocation: str, width: int, height: int, length: int, extra: dict | None = None):
    import ctypes

    import comfy.model_management as mm
    _, project, run_rel = _context()
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                    ("total_pagefile", ctypes.c_ulonglong), ("available_pagefile", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended", ctypes.c_ulonglong)]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    proc = __import__("psutil").Process()
    mem = proc.memory_info()
    private = int(proc.memory_full_info().private)
    allocated = int(__import__("torch").cuda.memory_allocated(0))
    reserved = int(__import__("torch").cuda.memory_reserved(0))
    loaded = []
    for patcher in mm.loaded_models(only_currently_used=False):
        model = getattr(patcher, "model", None)
        loaded.append({"patcher_type": type(patcher).__name__, "model_type": type(model).__name__ if model is not None else None})
    ev = {"schema_version": 1, "phase": phase, "invocation_id": invocation,
          "pid": os.getpid(), "create_time": proc.create_time(), "utc": datetime.now(UTC).isoformat(),
          "monotonic": time.monotonic(), "width": width, "height": height, "length": length,
          "ram_available_bytes": status.available_physical,
          "process_memory_bytes": {"rss": int(mem.rss), "private": private, "vms": int(mem.vms)},
          "cuda_allocated_bytes": allocated, "cuda_reserved_bytes": reserved, "loaded_models": loaded}
    if extra:
        ev.update(extra)
    project.append_line(run_rel + "/stage_events.jsonl", json.dumps(ev, separators=(",", ":"), allow_nan=False))
    return ev


def _unwrap(output):
    result = getattr(output, "result", output)
    if not isinstance(result, (tuple, list)):
        raise TypeError("Native node output did not expose a tuple result")
    return tuple(result)


def _model_refs(objects):
    refs = []
    seen = set()
    for role, obj in objects:
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        try:
            refs.append((role, weakref.ref(obj)))
        except TypeError:
            raise TypeError(f"Owned {role} model object does not support weak references")
        for attr in ("patcher", "model", "diffusion_model", "cond_stage_model", "first_stage_model"):
            child = getattr(obj, attr, None)
            if child is not None and id(child) not in seen:
                seen.add(id(child))
                try:
                    refs.append((role + "." + attr, weakref.ref(child)))
                except TypeError:
                    raise TypeError(f"Owned {role}.{attr} object does not support weak references")
    return refs


def _release_models(objects):
    import comfy.model_management as mm
    import torch
    wrappers = [obj for _, obj in objects if obj is not None]
    try:
        torch.cuda.synchronize()
        for obj in wrappers:
            patcher = getattr(obj, "patcher", None)
            if patcher is None and hasattr(obj, "model") and hasattr(obj, "model_size"):
                patcher = obj
            if patcher is not None:
                mm.unload_model_and_clones(patcher, all_devices=True)
    finally:
        del wrappers
        del objects
        gc.collect()
        mm.cleanup_models()
        mm.soft_empty_cache()
        torch.cuda.synchronize()


def _condition_scope(text_encoder_name, video_vae_name, audio_vae_name, prompt, width, height, length, ref_video, invocation):
    import nodes
    from comfy_extras.nodes_minimax_h3 import MiniMaxH3ReferenceToVideo
    if tuple(int(v) for v in ref_video.shape) != (192, 288, 384, 3):
        raise ValueError("Full reference must contain192frames at384x288 RGB")
    clip = video_vae = audio_vae = delegated = None
    objects = []
    try:
        clip = nodes.CLIPLoader().load_clip(text_encoder_name, type="minimax", device="default")[0]
        objects.append(("text_encoder", clip))
        video_vae = nodes.VAELoader().load_vae(video_vae_name)[0]
        objects.append(("video_vae", video_vae))
        audio_vae = nodes.VAELoader().load_vae(audio_vae_name)[0]
        objects.append(("audio_vae", audio_vae))
        refs = _model_refs(objects)
        delegated = MiniMaxH3ReferenceToVideo.execute(
            clip=clip, vae=video_vae, audio_vae=audio_vae, prompt=prompt,
            width=width, height=height, length=length, ref_image_size="match",
            ref_videos={"ref_video_0": ref_video})
        conditioning, latent = _unwrap(delegated)[:2]
        ref_blocks = [entry[1].get("minimax_refs") for entry in conditioning
                      if isinstance(entry, (list, tuple)) and len(entry) > 1 and isinstance(entry[1], dict)]
        if not ref_blocks or not ref_blocks[0] or ref_blocks[0][0].get("kind") != "video":
            raise ValueError("Native conditioner did not preserve the video reference block")
        block = ref_blocks[0][0]
        latent_shape = tuple(int(v) for v in block["latent"].shape)
        if ((int(block["latent_t"]), int(block["latent_h"]), int(block["latent_w"])) != (57, 18, 24)
                or latent_shape[-3:] != (57, 18, 24)):
            raise ValueError("Native video reference latent shape differs from full192frame384x288 contract")
        _event("post_condition_pre_release", invocation, width, height, length,
               {"reference_shape": [int(v) for v in ref_video.shape],
                "video_reference_latent_shapes": [list(latent_shape)]})
        return conditioning, latent, refs, tuple(objects)
    except BaseException:
        _release_models(tuple(objects))
        objects.clear()
        clip = video_vae = audio_vae = delegated = None
        gc.collect()
        raise


class CCTVH3ConditionAndRelease:
    RETURN_TYPES = ("CONDITIONING", "LATENT", "CCTV_H3_RELEASE")
    RETURN_NAMES = ("conditioning", "latent", "release")
    FUNCTION = "execute"
    CATEGORY = "CCTV/H3 Staging"

    @classmethod
    def INPUT_TYPES(cls):
        choices = _catalog()
        return {"required": {
            "text_encoder_name": ([choices["text_encoder"]],),
            "video_vae_name": ([choices["video_vae"]],),
            "audio_vae_name": ([choices["audio_vae"]],),
            "prompt": ("STRING", {"multiline": True}),
            "width": ("INT", {"default": 384, "min": 384, "max": 384}),
            "height": ("INT", {"default": 288, "min": 288, "max": 288}),
            "length": ("INT", {"default": 192, "min": 192, "max": 192}),
            "ref_video": ("IMAGE",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    def execute(self, text_encoder_name, video_vae_name, audio_vae_name, prompt, width, height, length, ref_video, unique_id=None):
        choices = _catalog()
        if (text_encoder_name, video_vae_name, audio_vae_name) != (choices["text_encoder"], choices["video_vae"], choices["audio_vae"]):
            raise ValueError("Staged conditioner filenames must exactly match the pinned model catalog")
        if (width, height, length) != (384, 288, 192):
            raise ValueError("Staged conditioner requires the authorized384x288x192 canvas")
        invocation = os.environ.get("CCTV_H3_INVOCATION_ID") or uuid.uuid4().hex
        proc = __import__("psutil").Process()
        create_time = proc.create_time()
        _event("condition_start", invocation, width, height, length)
        conditioning = latent = refs = objects = None
        try:
            conditioning, latent, refs, objects = _condition_scope(
                text_encoder_name, video_vae_name, audio_vae_name, prompt,
                width, height, length, ref_video, invocation)
            ref_blocks = [entry[1].get("minimax_refs") for entry in conditioning
                          if isinstance(entry, (list, tuple)) and len(entry) > 1 and isinstance(entry[1], dict)]
            if not ref_blocks or not ref_blocks[0] or ref_blocks[0][0].get("kind") != "video":
                raise ValueError("Native conditioner did not preserve the video reference block")
            block = ref_blocks[0][0]
            shape = tuple(int(v) for v in block["latent"].shape)
            if ((int(block["latent_t"]), int(block["latent_h"]), int(block["latent_w"])) != (57, 18, 24)
                    or shape[-3:] != (57, 18, 24)):
                raise ValueError("Native video reference latent shape differs from full192frame384x288 contract")
            _release_models(objects)
            objects = None
            gc.collect()
            weakrefs = [{"role": role, "released": ref() is None} for role, ref in refs]
            all_released = bool(weakrefs) and all(row["released"] for row in weakrefs)
            released = _event("post_release", invocation, width, height, length,
                              {"weakrefs": weakrefs, "all_model_refs_released": all_released})
            if not all_released:
                raise RuntimeError("Conditioning model weak references remain live after cleanup")
            _, project, run_rel = _context()
            release_rel = run_rel + "/stage_release.json"
            record = json.dumps(released, indent=2, allow_nan=False) + "\n"
            project.write_text(release_rel, record)
            token = {"schema_version": 1, "status": "released", "invocation_id": invocation,
                     "pid": os.getpid(), "create_time": create_time, "width": width, "height": height,
                     "length": length, "receipt": {"path": release_rel, "sha256": hashlib.sha256(record.encode()).hexdigest()},
                     "release_id": uuid.uuid4().hex}
            with _LOCK:
                _ISSUED[token["release_id"]] = json.loads(json.dumps(token))
            return conditioning, latent, token
        except BaseException:
            if objects is not None:
                _release_models(objects)
                objects = None
            raise


class CCTVH3StagedUNETLoader:
    RETURN_TYPES = ("MODEL",)
    FUNCTION = "execute"
    CATEGORY = "CCTV/H3 Staging"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"release": ("CCTV_H3_RELEASE",), "unet_name": ([_catalog()["diffusion"]],),
                             "weight_dtype": (["default"], {"default": "default"})}}

    def execute(self, release, unet_name, weight_dtype="default"):
        choices = _catalog()
        if not isinstance(release, dict) or unet_name != choices["diffusion"] or weight_dtype != "default":
            raise ValueError("Diffusion load requires a valid release and exact catalog filename")
        with _LOCK:
            known = _ISSUED.get(release.get("release_id"))
            if known is None or json.dumps(known, sort_keys=True) != json.dumps(release, sort_keys=True) or release.get("release_id") in _CONSUMED:
                raise ValueError("Forged, foreign, failed, or reused staged release")
            if release.get("pid") != os.getpid() or release.get("create_time") != __import__("psutil").Process().create_time():
                raise ValueError("Staged release belongs to a different process identity")
            _, project, run_rel = _context()
            receipt = release.get("receipt", {})
            if receipt.get("path") != run_rel + "/stage_release.json":
                raise ValueError("Release receipt path does not match the current run")
            data = project.read_bytes(receipt["path"])
            if hashlib.sha256(data).hexdigest() != receipt.get("sha256"):
                raise ValueError("Staged release receipt hash mismatch")
            record = json.loads(data)
            if not record.get("all_model_refs_released") or record.get("invocation_id") != release["invocation_id"]:
                raise ValueError("Staged release receipt does not prove completed model release")
            _CONSUMED.add(release["release_id"])
        _event("pre_diffusion_load", release["invocation_id"], release["width"], release["height"], release["length"])
        import nodes
        result = nodes.UNETLoader().load_unet(unet_name, weight_dtype)
        model = _unwrap(result)[0]
        _event("post_diffusion_load", release["invocation_id"], release["width"], release["height"], release["length"])
        return (model,)


class CCTVH3DecodeVAELoader:
    RETURN_TYPES = ("LATENT", "VAE", "VAE")
    RETURN_NAMES = ("samples", "video_vae", "audio_vae")
    FUNCTION = "execute"
    CATEGORY = "CCTV/H3 Staging"

    @classmethod
    def INPUT_TYPES(cls):
        choices = _catalog()
        return {"required": {"samples": ("LATENT",), "release": ("CCTV_H3_RELEASE",),
                             "video_vae_name": ([choices["video_vae"]],),
                             "audio_vae_name": ([choices["audio_vae"]],)}}

    def execute(self, samples, release, video_vae_name, audio_vae_name):
        choices = _catalog()
        if (video_vae_name, audio_vae_name) != (choices["video_vae"], choices["audio_vae"]):
            raise ValueError("Decode VAE filenames must exactly match the pinned model catalog")
        with _LOCK:
            known = _ISSUED.get(release.get("release_id")) if isinstance(release, dict) else None
            if known is None or json.dumps(known, sort_keys=True) != json.dumps(release, sort_keys=True) or release.get("release_id") not in _CONSUMED:
                raise ValueError("Decode VAEs require the consumed release and sampled-latent dependency")
        _event("post_sampling", release["invocation_id"], release["width"], release["height"], release["length"])
        import nodes
        video_vae = nodes.VAELoader().load_vae(video_vae_name)[0]
        audio_vae = nodes.VAELoader().load_vae(audio_vae_name)[0]
        return samples, video_vae, audio_vae


NODE_CLASS_MAPPINGS.update({"CCTVH3ConditionAndRelease": CCTVH3ConditionAndRelease,
                            "CCTVH3StagedUNETLoader": CCTVH3StagedUNETLoader,
                            "CCTVH3DecodeVAELoader": CCTVH3DecodeVAELoader})



# Audio diagnostic is deliberately a node-local mirror of the pinned native helper.
NODE_DISPLAY_NAME_MAPPINGS["CCTVH3AudioDiagnostic"] = "CCTV H3 Audio Diagnostic"


def _json_number(value):
    import math
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _metric(values, rate=None):
    import math

    import torch

    x = values.detach().to(device="cpu", dtype=torch.float64).reshape(-1)
    finite = torch.isfinite(x)
    positions = torch.nonzero(finite, as_tuple=False).reshape(-1)
    clean = x[finite]
    result = {"sample_count": int(x.numel()), "nonfinite_samples": int((~finite).sum()),
              "clipped_samples": int((finite & (x.abs() >= 1.0)).sum())}
    if clean.numel():
        absolute = clean.abs()
        peak, finite_peak_index = torch.max(absolute, 0)
        peak_index = int(positions[int(finite_peak_index)])
        rms = torch.sqrt(torch.mean(clean.square()))
        dc = torch.mean(clean)
        quantiles = torch.quantile(absolute, torch.tensor([.5,.95,.99,.999], dtype=torch.float64))
        peak_value = float(peak)
        rms_value = float(rms)
        peak_db = 20 * math.log10(peak_value) if peak_value > 0 else "-Infinity"
        rms_db = 20 * math.log10(rms_value) if rms_value > 0 else "-Infinity"
        peak_time = float(peak_index) / rate if rate else None
        pcts = {key: _json_number(val) for key, val in zip(("p50","p95","p99","p999"), quantiles)}
        result.update({"sample_peak": _json_number(peak_value), "rms": _json_number(rms_value),
                       "dc": _json_number(dc), "rms_dbfs": rms_db, "peak_dbfs": peak_db,
                       "peak_sample_index": peak_index, "peak_time_seconds": peak_time,
                       "abs_percentiles": pcts})
    else:
        result.update({"sample_peak": None, "rms": None, "dc": None, "rms_dbfs": None,
                       "peak_dbfs": None, "peak_sample_index": None, "peak_time_seconds": None,
                       "abs_percentiles": {key: None for key in ("p50","p95","p99","p999")},
                       "metric_unavailable_reason": "no_finite_samples"})
    return result


def _wave_metrics(wave, rate):
    import math

    import torch
    channels = wave[0].detach().to(device="cpu", dtype=torch.float64)
    mono = channels.mean(dim=0)
    per_channel = [_metric(channels[i], rate) for i in range(channels.shape[0])]
    def bins(width):
        rows = []
        for start in range(0, mono.numel(), width):
            part = mono[start:start + width]
            summary = _metric(part, rate)
            rows.append({"start_sample": start, "sample_count": int(part.numel()),
                         "rms_dbfs": summary["rms_dbfs"], "peak_dbfs": summary["peak_dbfs"]})
        return rows
    corr = None
    corr_reason = None
    if channels.shape[0] < 2:
        corr_reason = "fewer_than_two_channels"
    elif not bool(torch.isfinite(channels[:2]).all()):
        corr_reason = "nonfinite_samples"
    elif bool((channels[:2].std(dim=1, unbiased=False) == 0).any()):
        corr_reason = "constant_channel"
    else:
        observed = float(torch.corrcoef(channels[:2])[0,1])
        if math.isfinite(observed):
            corr = observed
        else:
            corr_reason = "correlation_nonfinite"
    return {"per_channel": per_channel, "mono": _metric(mono, rate),
            "channel_correlation": corr, "channel_correlation_reason": corr_reason,
            "one_second_bins": bins(rate), "twenty_ms_bins": bins(max(1, round(rate * .020)))}


def _write_audio_wav(project, rel, wave, rate, run_rel, label):
    import torch
    channels = int(wave.shape[1])
    data = wave[0].detach().to(device="cpu", dtype=torch.float32).transpose(0, 1).contiguous().numpy().tobytes()
    raw_rel = run_rel + "/audio_diagnostic/" + label + ".f32le"
    project.write_bytes(raw_rel, data)
    output_rel = run_rel + "/audio_diagnostic/" + label + ".wav"
    def encode(path):
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "f32le",
               "-ar", str(rate), "-ac", str(channels), "-i", str(project.resolve(raw_rel)),
               "-c:a", "pcm_f32le", "-f", "wav", str(path)]
        result = __import__("subprocess").run(cmd, capture_output=True, text=True, timeout=180)
        project.write_text(run_rel + "/audio_diagnostic/" + label + "_ffmpeg.log",
                           __import__("json").dumps(cmd) + "\n" + result.stdout + "\n" + result.stderr)
        if result.returncode:
            raise RuntimeError("Float WAV derivative encoding failed; contained log retained")
    project.write_with(output_rel, encode)
    return output_rel


class CCTVH3AudioDiagnostic:
    RETURN_TYPES = ("AUDIO",)
    FUNCTION = "execute"
    CATEGORY = "CCTV/H3 Staging"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"samples": ("LATENT",), "vae": ("VAE",)}}

    def execute(self, samples, vae):
        if os.environ.get("CCTV_H3_AUDIO_DIAGNOSTIC") != "1":
            raise RuntimeError("Audio diagnostic node requires explicit diagnostic environment gate")
        invocation = os.environ.get("CCTV_H3_INVOCATION_ID", "")
        _root, project, run_rel = _context()
        if not invocation or Path(os.environ.get("CCTV_H3_RUN_REL", "")).name != invocation:
            raise RuntimeError("Audio diagnostic invocation identity does not match the current run")
        with _AUDIO_DIAGNOSTIC_LOCK:
            if invocation in _AUDIO_DIAGNOSTIC_INVOCATIONS:
                raise RuntimeError("Audio diagnostic invocation is single-use; recapture is forbidden")
            _AUDIO_DIAGNOSTIC_INVOCATIONS.add(invocation)
        audio_dir = run_rel + "/audio_diagnostic"
        expected_artifacts = ("receipt.json", "tensors.safetensors", "raw_decoded_audio.wav", "post_native_audio.wav")
        audio_dir_path = project.resolve(audio_dir)
        if (any(project.resolve(audio_dir + "/" + name).exists() for name in expected_artifacts)
                or (audio_dir_path.exists() and any(audio_dir_path.iterdir()))):
            raise RuntimeError("Preexisting diagnostic artifact prevents immutable current-invocation capture")
        import torch
        from safetensors.torch import save
        latent = samples["samples"]
        if latent.is_nested:
            latent = latent.unbind()[-1]
        latent_copy = latent.detach().to(device="cpu").contiguous().clone()
        decoded = vae.decode(latent)
        audio = decoded.movedim(-1, 1)
        if audio.ndim != 3 or audio.shape[0] != 1 or audio.shape[1] < 1 or audio.shape[2] < 1:
            raise ValueError("Audio diagnostic requires one batch and positive channel/sample dimensions")
        raw = audio.detach().clone()
        std_times_five = (torch.std(audio, dim=[1,2], keepdim=True) * 5.0).detach().clone()
        divisor = std_times_five.clone()
        divisor[divisor < 1.0] = 1.0
        audio /= divisor
        post = audio
        rate_value = getattr(vae, "audio_sample_rate_output", getattr(vae, "audio_sample_rate", 44100))
        if "sample_rate" in samples:
            rate_value = samples["sample_rate"]
        rate = int(rate_value)
        if rate <= 0:
            raise ValueError("Native audio sample rate must be positive")
        raw_copy = raw.detach().to(device="cpu").contiguous().clone()
        std_copy = std_times_five.detach().to(device="cpu").contiguous().clone()
        divisor_copy = divisor.detach().to(device="cpu").contiguous().clone()
        post_copy = post.detach().to(device="cpu").contiguous().clone()
        tensors = {"sampled_audio_latent": latent_copy, "raw_decoded_waveform": raw_copy,
                   "native_std_times_five": std_copy, "native_divisor": divisor_copy,
                   "post_native_waveform": post_copy}
        project.make_dir(audio_dir)
        archive_rel = audio_dir + "/tensors.safetensors"
        project.write_bytes(archive_rel, save(tensors))
        raw_wav = _write_audio_wav(project, audio_dir + "/raw_decoded_audio.wav", raw_copy, rate, run_rel, "raw_decoded_audio")
        post_wav = _write_audio_wav(project, audio_dir + "/post_native_audio.wav", post_copy, rate, run_rel, "post_native_audio")
        source_rel = "spikes/22_minimax_h3_workflows/_work/local_runtime/source/ComfyUI/comfy_extras/nodes_audio.py"
        requested = (rate * 192 + 23) // 24
        retained = min(int(post_copy.shape[2]), requested)
        raw_crop = raw_copy[:, :, :retained]
        post_crop = post_copy[:, :, :retained]
        meta = {key: {"shape": list(value.shape), "dtype": str(value.dtype), "original_device": str(original.device)}
                for key, value, original in zip(tensors, tensors.values(), (latent, raw, std_times_five, divisor, post))}
        receipt = {"schema_version": 1, "status": "captured", "invocation_id": invocation,
                   "pid": os.getpid(), "create_time": __import__("psutil").Process().create_time(),
                   "utc": datetime.now(UTC).isoformat(),
                   "native_decoder_source": {"path": source_rel, "sha256": hashlib.sha256(project.read_bytes(source_rel)).hexdigest()},
                   "sample_rate": rate, "decode_api_invocations": 1,
                   "tensor_archive": {"path": archive_rel, "sha256": hashlib.sha256(project.read_bytes(archive_rel)).hexdigest(), "bytes": len(project.read_bytes(archive_rel))},
                   "raw_wav": {"path": raw_wav, "sha256": hashlib.sha256(project.read_bytes(raw_wav)).hexdigest(), "bytes": len(project.read_bytes(raw_wav))},
                   "post_native_wav": {"path": post_wav, "sha256": hashlib.sha256(project.read_bytes(post_wav)).hexdigest(), "bytes": len(project.read_bytes(post_wav))},
                   "tensor_metadata": meta, "std_times_five": [_json_number(v) for v in std_copy.reshape(-1)],
                   "divisor": [_json_number(v) for v in divisor_copy.reshape(-1)], "waveform_layout": "B,C,T",
                   "crop": {"policy": "native batch0 prefix to ceil(sample_rate*192/24)", "fps": "24/1", "frame_count": 192,
                            "requested_samples": requested, "retained_samples": retained,
                            "discarded_tail_samples": int(post_copy.shape[2]) - retained, "padding_samples": 0},
                   "metrics": {"sampled_audio_latent": _metric(latent_copy),
                               "raw_full": _wave_metrics(raw_copy, rate), "raw_crop": _wave_metrics(raw_crop, rate),
                               "post_full": _wave_metrics(post_copy, rate), "post_crop": _wave_metrics(post_crop, rate)}}
        receipt_rel = audio_dir + "/receipt.json"
        project.write_text(receipt_rel, json.dumps(receipt, indent=2, allow_nan=False) + "\n")
        return ({"waveform": post, "sample_rate": rate_value},)


_AUDIO_DIAGNOSTIC_LOCK = threading.RLock()
_AUDIO_DIAGNOSTIC_INVOCATIONS: set[str] = set()
NODE_CLASS_MAPPINGS["CCTVH3AudioDiagnostic"] = CCTVH3AudioDiagnostic
