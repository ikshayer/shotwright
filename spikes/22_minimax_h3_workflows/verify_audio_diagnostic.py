"""Independent Amendment5 CPU and retained-artifact verifier.

Metrics use float64; JSON numeric comparison rtol=1e-9, atol=1e-12.
Native math uses the contract's dtype-dependent 8eps/2eps bounds.
No loudness or codec quality threshold is imposed. Torch is imported lazily.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import traceback
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

BASE = "spikes/22_minimax_h3_workflows"
RUNS = BASE + "/_work/local_runs"
NATIVE = BASE + "/_work/local_runtime/source/ComfyUI/comfy_extras/nodes_audio.py"
KEYS = {"sampled_audio_latent", "raw_decoded_waveform", "native_std_times_five", "native_divisor", "post_native_waveform"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "-inf" if value < 0 else "+inf"
    if isinstance(value, dict):
        return {k: safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    return value


def compare(expected, observed, path="metrics"):
    aliases = {"-Infinity": "-inf", "-infinity": "-inf", "Infinity": "+inf", "+Infinity": "+inf", "inf": "+inf", "nan": "NaN"}
    if isinstance(expected, str):
        expected = aliases.get(expected, expected)
    if isinstance(observed, str):
        observed = aliases.get(observed, observed)
    if isinstance(expected, dict):
        require(isinstance(observed, dict), path + " not an object")
        for key, value in expected.items():
            require(key in observed, path + "." + key + " absent")
            compare(value, observed[key], path + "." + key)
    elif isinstance(expected, list):
        require(isinstance(observed, list) and len(expected) == len(observed), path + " length differs")
        for index, (a, b) in enumerate(zip(expected, observed)):
            compare(a, b, path + "." + str(index))
    elif isinstance(expected, float):
        require(isinstance(observed, (float, int)) and math.isclose(expected, observed, rel_tol=1e-9, abs_tol=1e-12), path + " differs")
    else:
        require(expected == observed, path + " differs")


def summary(values, rate=None):
    import torch
    x = values.to(torch.float64).flatten()
    finite = x[torch.isfinite(x)]
    require(x.numel() > 0, "empty metric input")
    if finite.numel() == 0:
        result = {"sample_count": x.numel(), "nonfinite_samples": x.numel(), "clipped_samples": 0, "sample_peak": None, "rms": None, "dc": None, "rms_dbfs": None, "peak_dbfs": None, "peak_sample_index": None, "abs_percentiles": {k: None for k in ("p50", "p95", "p99", "p999")}}
        if rate is not None:
            result["peak_time_seconds"] = None
        return result
    peak = float(finite.abs().max())
    rms = float(finite.square().mean().sqrt()) if finite.numel() else float("nan")
    index = int(torch.nan_to_num(x.abs(), nan=-1, posinf=-1, neginf=-1).argmax())
    db = lambda a: 20 * math.log10(a) if a > 0 else float("-inf") if a == 0 else float("nan")
    result = {"sample_count": x.numel(), "nonfinite_samples": int((~torch.isfinite(x)).sum()), "clipped_samples": int((finite.abs() >= 1).sum()), "sample_peak": peak, "rms": rms, "dc": float(finite.mean()) if finite.numel() else float("nan"), "rms_dbfs": db(rms), "peak_dbfs": db(peak), "peak_sample_index": index, "abs_percentiles": {name: float(torch.quantile(finite.abs(), q)) if finite.numel() else float("nan") for name, q in (("p50", .5), ("p95", .95), ("p99", .99), ("p999", .999))}}
    if rate is not None:
        result["peak_time_seconds"] = index / rate
    return safe(result)


def waveform_metrics(wave, rate):
    import torch
    x = wave[0].to(torch.float64)
    mono = x.mean(0)
    result = {"per_channel": [summary(c, rate) for c in x], "mono": summary(mono, rate)}
    if x.shape[0] > 1 and bool(torch.isfinite(x[:2]).all()) and bool((x[:2].std(dim=1) > 0).all()):
        result["channel_correlation"] = float(torch.corrcoef(x[:2])[0, 1])
    else:
        result.update(channel_correlation=None, channel_correlation_reason="constant channel or fewer than two channels")
    for name, size in (("one_second_bins", rate), ("twenty_ms_bins", max(1, round(rate * .02)))):
        bins = []
        for start in range(0, mono.numel(), size):
            row = summary(mono[start:start + size], rate)
            bins.append({"start_sample": start, "sample_count": row["sample_count"], "rms_dbfs": row["rms_dbfs"], "peak_dbfs": row["peak_dbfs"]})
        result[name] = bins
    return result


def native_helper(root):
    import torch
    tree = ast.parse(root.read_text(NATIVE))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "vae_decode_audio")
    namespace = {"torch": torch}
    exec(compile(ast.Module(body=[function], type_ignores=[]), NATIVE, "exec"), namespace)  # noqa: S102 - single pinned native AST function, no module side effects
    return namespace["vae_decode_audio"]


def checked_record(root, item, within=None):
    path = root.resolve(item["path"])
    if within is not None:
        require(path.is_relative_to(root.resolve(within)), "artifact outside expected attempt")
    data = root.read_bytes(item["path"])
    require(hashlib.sha256(data).hexdigest() == item["sha256"], "artifact hash differs: " + item["path"])
    if "bytes" in item:
        require(len(data) == item["bytes"], "artifact bytes differ")
    return path


def decode_pcm(path, channels):
    import torch
    result = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "f32le", "-"], capture_output=True, timeout=180, check=True)
    require(not result.stderr, "decoder reported errors")
    return torch.frombuffer(bytearray(result.stdout), dtype=torch.float32).reshape(-1, channels).T.unsqueeze(0).clone()


def math_evidence(raw, std, divisor, post):
    import torch
    info = torch.finfo(raw.dtype)
    expected_std = torch.std(raw, dim=[1, 2], keepdim=True) * 5
    expected_divisor = expected_std.clamp_min(1)
    expected_post = raw / divisor
    result = {}
    for name, actual, expected, factor in (("std", std, expected_std, 8), ("divisor", divisor, expected_divisor, 8), ("post", post, expected_post, 2)):
        require(actual.shape == expected.shape and actual.dtype == expected.dtype, name + " shape/dtype differs")
        require(torch.allclose(actual, expected, rtol=factor * info.eps, atol=factor * info.tiny, equal_nan=True), name + " native math differs")
        result[name] = {"exact_equal": torch.equal(actual, expected), "max_absolute_error": safe(float((actual - expected).abs().max())), "rtol": factor * info.eps, "atol": factor * info.tiny}
    return result


def stage_identity(receipt, events, release):
    identity = (receipt["invocation_id"], receipt["pid"], receipt["create_time"])
    require(bool(events) and all((e["invocation_id"], e["pid"], e["create_time"]) == identity for e in events), "capture/stage event identity differs")
    require((release["invocation_id"], release["pid"], release["create_time"]) == identity and release in events, "capture/release identity differs")


def require_invocation(receipt, run):
    require(receipt["invocation_id"] == Path(run).name, "capture invocation differs from run basename")


def exact_waveform(decoded, tensor):
    import torch
    require(decoded.shape == tensor.shape, "diagnostic WAV shape differs")
    require(torch.allclose(decoded, tensor.float(), rtol=0, atol=0, equal_nan=True), "WAV PCM differs from authoritative tensor")


def rejection(operation, message):
    try:
        operation()
    except ValueError:
        return
    raise AssertionError(message)


def adversarial_checks(root, run, capture):
    import copy

    import torch
    # The low signal's divisor is one; both separately named WAVs may be identical.
    bundle_rel = run + "/verifier_fixture_bundle"
    root.write_text(bundle_rel + "/.contained", "independent fixture")
    bundle = ProjectRoot(root.resolve(bundle_rel))
    files = {}
    for key, basename in (("tensor_archive", "tensors.safetensors"), ("raw_wav", "raw_decoded_audio.wav"), ("post_native_wav", "post_native_audio.wav")):
        source = capture["receipt"][key]
        data = root.read_bytes(source["path"])
        bundle.write_bytes("diagnostic/" + basename, data)
        files[key] = {"path": "diagnostic/" + basename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    data = root.read_bytes(run + "/audio_diagnostic/receipt.json")
    bundle.write_bytes("diagnostic/receipt.json", data)
    files["receipt"] = {"path": "diagnostic/receipt.json", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    require(files["raw_wav"]["sha256"] == files["post_native_wav"]["sha256"], "identical-WAV fixture did not exercise duplicate hashes")
    verify_bundle_diagnostic(bundle, files, capture["receipt"], files["receipt"])
    wrong = copy.deepcopy(files)
    wrong["raw_wav"]["path"] = "diagnostic/unexpected.wav"
    rejection(lambda: verify_bundle_diagnostic(bundle, wrong, capture["receipt"], files["receipt"]), "wrong bundle path accepted")
    wrong_identity = dict(capture["receipt"], invocation_id="foreign")
    rejection(lambda: require_invocation(wrong_identity, run), "foreign invocation accepted")
    identity = {key: capture["receipt"][key] for key in ("invocation_id", "pid", "create_time")}
    stage_identity(capture["receipt"], [identity], identity)
    for key, value in (("pid", identity["pid"] + 1), ("create_time", identity["create_time"] + 1), ("invocation_id", "foreign")):
        bad = dict(identity, **{key: value})
        rejection(lambda bad=bad: stage_identity(capture["receipt"], [bad], bad), "foreign stage identity accepted")
    # Decode a real one-frame float WAV which would broadcast over a longer zero tensor.
    wav_rel = bundle_rel + "/one_frame.wav"
    def writer(path):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", "100", "-ac", "2", "-i", "-", "-c:a", "pcm_f32le", "-f", "wav", "-y", str(path)], input=bytes(8), capture_output=True, check=True, timeout=30)
    root.write_with(wav_rel, writer)
    short = decode_pcm(root.resolve(wav_rel), 2)
    require(short.shape == (1,2,1), "short WAV fixture decode shape differs")
    rejection(lambda: exact_waveform(short, torch.zeros(1,2,900)), "broadcast PCM shape accepted")


def verify_capture(root, run, actual=False):
    from safetensors.torch import load_file
    receipt = json.loads(root.read_text(run + "/audio_diagnostic/receipt.json"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError("invalid JSON constant " + x)))
    require(receipt["schema_version"] == 1 and receipt["status"] == "captured" and receipt["decode_api_invocations"] == 1, "invalid capture receipt")
    require_invocation(receipt, run)
    require(bool(receipt["invocation_id"]) and receipt["pid"] > 0 and receipt["create_time"] > 0 and bool(receipt["utc"]), "capture identity absent")
    source = checked_record(root, receipt["native_decoder_source"])
    require(source == root.resolve(NATIVE), "unexpected native decoder source")
    tensors = load_file(str(checked_record(root, receipt["tensor_archive"], run)))
    require(set(tensors) == KEYS, "tensor keys differ")
    raw, post = tensors["raw_decoded_waveform"], tensors["post_native_waveform"]
    require(raw.ndim == 3 and raw.shape[0] == 1 and raw.shape[2] > 0 and raw.shape == post.shape, "waveform shape differs")
    rate = receipt["sample_rate"]
    require(type(rate) is int and rate > 0 and receipt["waveform_layout"] == "B,C,T", "rate/layout differs")
    if actual:
        generation = json.loads(root.read_text(run + "/generation.json"))
        events_path = checked_record(root, generation["stage_events"], run)
        release_path = checked_record(root, generation["stage_release"], run)
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        release = json.loads(release_path.read_text())
        stage_identity(receipt, events, release)
        require(list(tensors["sampled_audio_latent"].shape) == [1, 32, 2, 320] and raw.shape[1] == 2 and rate == 32000, "actual native latent/channel/rate differs")
    for key, tensor in tensors.items():
        row = receipt["tensor_metadata"][key]
        require(row["shape"] == list(tensor.shape) and row["dtype"] == str(tensor.dtype) and bool(row["original_device"]), "tensor metadata differs: " + key)
    evidence = math_evidence(raw, tensors["native_std_times_five"], tensors["native_divisor"], post)
    compare(safe(tensors["native_std_times_five"].flatten().tolist()), receipt["std_times_five"])
    compare(safe(tensors["native_divisor"].flatten().tolist()), receipt["divisor"])
    requested = math.ceil(rate * 192 / 24)
    retained = min(raw.shape[-1], requested)
    compare({"policy": "native batch0 prefix to ceil(sample_rate*192/24)", "fps": "24/1", "frame_count": 192, "requested_samples": requested, "retained_samples": retained, "discarded_tail_samples": raw.shape[-1] - retained, "padding_samples": 0}, receipt["crop"])
    calculated = {"sampled_audio_latent": summary(tensors["sampled_audio_latent"]), "raw_full": waveform_metrics(raw, rate), "raw_crop": waveform_metrics(raw[..., :retained], rate), "post_full": waveform_metrics(post, rate), "post_crop": waveform_metrics(post[..., :retained], rate)}
    for key in ("raw_full", "raw_crop", "post_full", "post_crop"):
        observed_metrics = receipt["metrics"][key]
        if calculated[key]["channel_correlation"] is None:
            require(observed_metrics.get("channel_correlation") is None and bool(observed_metrics.get("channel_correlation_reason")), "undefined correlation must have null plus reason")
            calculated[key]["channel_correlation_reason"] = observed_metrics["channel_correlation_reason"]
        for expected_summary, observed_summary in zip(calculated[key]["per_channel"] + [calculated[key]["mono"]], observed_metrics["per_channel"] + [observed_metrics["mono"]]):
            if expected_summary["sample_peak"] is None:
                require(any("reason" in k and bool(v) for k,v in observed_summary.items()), "all-nonfinite summary lacks explanation")
    compare(calculated, receipt["metrics"])
    for key, tensor in (("raw_wav", raw), ("post_native_wav", post)):
        path = checked_record(root, receipt[key], run)
        probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)], capture_output=True, check=True, timeout=30).stdout)["streams"]
        require(len(probe) == 1 and int(probe[0]["sample_rate"]) == rate and probe[0]["channels"] == raw.shape[1] and probe[0]["codec_name"] == "pcm_f32le", "diagnostic WAV format differs")
        decoded = decode_pcm(path, raw.shape[1])
        exact_waveform(decoded, tensor)
    return {"passed": True, "math": evidence, "metrics": calculated, "receipt": receipt, "post_crop": post[..., :retained]}


def codec_comparison(reference, decoded, rate):
    import torch
    a, b = reference[0].double(), decoded[0].double()
    def row(x, y):
        n = min(x.numel(), y.numel())
        x, y = x[:n], y[:n]
        correlation = float(torch.corrcoef(torch.stack((x, y)))[0, 1]) if n > 1 and x.std() > 0 and y.std() > 0 else None
        return safe({"sample_count": n, "correlation": correlation, "rms_ratio": float(y.square().mean().sqrt() / x.square().mean().sqrt()) if x.square().mean() > 0 else None, "rms_error": float((x-y).square().mean().sqrt())})
    mono_a, mono_b = a.mean(0), b.mean(0)
    best = None
    # FFT cross-correlation computes all bounded lags without 4097 full scans.
    nfft = 1 << (mono_a.numel() + mono_b.numel() - 1).bit_length()
    corr = torch.fft.irfft(torch.fft.rfft(mono_a, nfft) * torch.fft.rfft(mono_b.flip(0), nfft), nfft)
    for lag in range(-2048, 2049):
        index = mono_b.numel() - 1 - lag
        if 0 <= index < corr.numel() and (best is None or float(corr[index]) > best[0]):
            best = (float(corr[index]), lag)
    lag = best[1]
    ax, bx = (a[:, -lag:], b) if lag < 0 else (a, b[:, lag:])
    return {"lag_bound_samples": 2048, "best_lag_samples": lag, "zero_lag": {"mono": row(mono_a, mono_b), "per_channel": [row(x,y) for x,y in zip(a,b)]}, "best_lag": {"mono": row(ax.mean(0), bx.mean(0)), "per_channel": [row(x,y) for x,y in zip(ax,bx)]}, "decoded_metrics": waveform_metrics(decoded, rate), "quality_threshold_applied": False}


def verify_bundle_diagnostic(bundle, files, receipt, receipt_record):
    expected = {"tensor_archive": "tensors.safetensors", "raw_wav": "raw_decoded_audio.wav", "post_native_wav": "post_native_audio.wav"}
    for key, basename in expected.items():
        matching = [item for item in files.values() if item.get("path") == "diagnostic/" + basename]
        require(len(matching) == 1 and matching[0]["sha256"] == receipt[key]["sha256"], "bundle diagnostic path/hash absent or ambiguous: " + key)
        checked_record(bundle, matching[0])
    matching = [item for item in files.values() if item.get("path") == "diagnostic/receipt.json"]
    require(len(matching) == 1 and matching[0]["sha256"] == receipt_record["sha256"], "bundle receipt path/hash absent or ambiguous")
    checked_record(bundle, matching[0])


def verify_run(root, run):
    require(root.resolve(run).is_relative_to(root.resolve(RUNS)), "run outside local_runs")
    result = verify_capture(root, run, actual=True)
    generation = json.loads(root.read_text(run + "/generation.json"))
    receipt = checked_record(root, generation["audio_diagnostic"], run)
    require(receipt == root.resolve(run + "/audio_diagnostic/receipt.json"), "generation capture pointer differs")
    result["codec_comparisons"] = {}
    for role in ("generated_av", "native_wav", "edit_wav"):
        path = checked_record(root, generation["media"][role], run)
        decoded = decode_pcm(path, 2)
        if role == "edit_wav":
            output = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ar", "32000", "-f", "f32le", "-"], capture_output=True, check=True, timeout=180)
            import torch
            decoded = torch.frombuffer(bytearray(output.stdout), dtype=torch.float32).reshape(-1,2).T.unsqueeze(0)
        result["codec_comparisons"][role] = codec_comparison(result["post_crop"], decoded, 32000)
    bundle = ProjectRoot(root.resolve(run + "/editor_bundle"))
    files = json.loads(bundle.read_text("project.json"))["files"]
    verify_bundle_diagnostic(bundle, files, result["receipt"], generation["audio_diagnostic"])
    del result["post_crop"]
    result["quality_accepted"] = False
    result["limitations"] = ["CPU verification cannot establish audibility or distinguish diffusion from audio VAE faults.", "Codec comparisons report measurements without a quality acceptance threshold."]
    return result


def self_test(root):
    import torch
    helper = native_helper(root)
    results = []
    cases = [("low", .001, 100, False, False), ("high", 3., 100, False, False), ("zeros", 0., 100, False, False), ("constant", 0., 100, False, True), ("tail_outlier", .001, 100, False, False), ("nested_rate_override", .01, 101, True, False), ("legacy_rate", .01, 88, False, False), ("fallback_rate", .01, 44100, False, False), ("nonfinite_positions", .001, 100, False, False), ("all_nonfinite", .001, 100, False, False)]
    for name, amplitude, rate, nested, constant in cases:
        run = RUNS + "/_audio_diagnostic/" + name + "_" + uuid.uuid4().hex
        try:
            raw = torch.sin(torch.arange(rate * 9, dtype=torch.float32) * .1).reshape(1,-1,1).repeat(1,1,2) * amplitude
            if constant:
                raw.fill_(.125)
            if name == "tail_outlier":
                raw[:, -1] = 500
            if name == "nonfinite_positions":
                raw[:, :7] = float("nan")
                raw[:, 30] = .9
            if name == "all_nonfinite":
                raw.fill_(float("nan"))
            audio_latent = torch.arange(12, dtype=torch.float32).reshape(1,3,4)
            latent = torch.nested.nested_tensor([torch.zeros(1,2,3), audio_latent]) if nested else audio_latent
            samples = {"samples": latent, "metadata_sentinel": object()}
            if nested:
                samples["sample_rate"] = rate
            original = audio_latent.clone()
            class VAE:
                audio_sample_rate = 88
                audio_sample_rate_output = 77 if nested else rate
                def __init__(self, raw=raw):
                    self.calls = 0
                    self.output = raw.clone()
                def decode(self, value, audio_latent=audio_latent):
                    self.calls += 1
                    require(torch.equal(value, audio_latent), "wrong joint audio latent selected")
                    return self.output
            if name in {"legacy_rate", "fallback_rate"}:
                del VAE.audio_sample_rate_output
            if name == "fallback_rate":
                del VAE.audio_sample_rate
            expected_vae = VAE()
            expected = helper(expected_vae, samples)
            spec = importlib.util.spec_from_file_location("blind_audio_" + uuid.uuid4().hex, root.resolve(BASE + "/stage_extension/h3_staging/__init__.py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            vae = VAE()
            with patch.dict(os.environ, {"CCTV_H3_AUDIO_DIAGNOSTIC": "1", "CCTV_H3_RUN_REL": run, "CCTV_H3_INVOCATION_ID": Path(run).name}):
                output, = module.NODE_CLASS_MAPPINGS["CCTVH3AudioDiagnostic"]().execute(samples, vae)
            require(vae.calls == 1 and torch.allclose(output["waveform"], expected["waveform"], rtol=0, atol=0, equal_nan=True) and output["sample_rate"] == rate, "native result/count/rate differs")
            require(output["waveform"].data_ptr() == vae.output.data_ptr(), "native output storage replaced")
            require(torch.equal(audio_latent, original), "sampler pack changed")
            capture = verify_capture(root, run)
            from safetensors.torch import load_file
            archive = load_file(str(root.resolve(capture["receipt"]["tensor_archive"]["path"])))
            require(torch.allclose(archive["raw_decoded_waveform"], raw.movedim(-1,1), rtol=0, atol=0, equal_nan=True), "CPU raw alias was mutated")
            # A capture is immutable and replay must fail before a second decode.
            if name == "low":
                adversarial_checks(root, run, capture)
            receipt_before = root.read_bytes(run + "/audio_diagnostic/receipt.json")
            replay_vae = VAE()
            with patch.dict(os.environ, {"CCTV_H3_AUDIO_DIAGNOSTIC": "1", "CCTV_H3_RUN_REL": run, "CCTV_H3_INVOCATION_ID": capture["receipt"]["invocation_id"]}):
                try:
                    module.NODE_CLASS_MAPPINGS["CCTVH3AudioDiagnostic"]().execute(samples, replay_vae)
                except (RuntimeError, ValueError, OSError):
                    pass
                else:
                    raise AssertionError("captured invocation replay accepted")
            require(replay_vae.calls == 0, "replay invoked decode again")
            require(root.read_bytes(run + "/audio_diagnostic/receipt.json") == receipt_before, "replay overwrote immutable receipt")
            results.append({"name": name, "passed": True})
        except Exception as exc:  # noqa: BLE001 - independent cases must all report
            results.append({"name": name, "passed": False, "error": type(exc).__name__ + ": " + str(exc), "traceback": traceback.format_exc()})
    for name in ("decode_error", "capture_error", "missing_env", "zero_rate_override"):
        run = RUNS + "/_audio_diagnostic/" + name + "_" + uuid.uuid4().hex
        calls = []
        try:
            class FailingVAE:
                audio_sample_rate_output = 100
                def decode(self, value, calls=calls, name=name):
                    calls.append(value)
                    if name == "decode_error":
                        raise RuntimeError("independent decode failure")
                    return torch.zeros(1, 900, 2)
            samples = {"samples": torch.ones(1,3,4)}
            if name == "zero_rate_override":
                samples["sample_rate"] = 0
            with patch.dict(os.environ, {"CCTV_H3_AUDIO_DIAGNOSTIC": "0" if name == "missing_env" else "1", "CCTV_H3_RUN_REL": run, "CCTV_H3_INVOCATION_ID": Path(run).name}):  # noqa: SIM117 - conditional failure boundary kept visible
                with patch.object(ProjectRoot, "write_with", side_effect=OSError("independent capture failure")) if name == "capture_error" else patch.dict(os.environ, {}):
                    try:
                        module.NODE_CLASS_MAPPINGS["CCTVH3AudioDiagnostic"]().execute(samples, FailingVAE())
                    except (ValueError, RuntimeError, OSError, TypeError):
                        pass
                    else:
                        raise AssertionError("failure/invalid environment silently accepted")
            require(len(calls) == (0 if name == "missing_env" else 1), "decode error/count boundary differs")
            require(not root.resolve(run + "/audio_diagnostic/receipt.json").exists(), "failed capture claimed success receipt")
            results.append({"name": name, "passed": True})
        except Exception as exc:  # noqa: BLE001 - report each independent failure case
            results.append({"name": name, "passed": False, "error": type(exc).__name__ + ": " + str(exc), "traceback": traceback.format_exc()})
    return {"passed": all(r["passed"] for r in results), "checks": results, "limitations": ["Real CPU tensors/files with controlled VAE; no model audio quality or GPU proof."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    root = ProjectRoot(ROOT)
    require(root.resolve(args.report).is_relative_to(root.resolve(RUNS)), "report outside local_runs")
    require(bool(args.run) != args.self_test, "choose exactly one of --run/--self-test")
    if args.self_test:
        require(root.resolve(args.report).is_relative_to(root.resolve(RUNS + "/_audio_diagnostic")), "self-test report outside _audio_diagnostic")
    try:
        report = self_test(root) if args.self_test else verify_run(root, args.run)
    except Exception as exc:  # noqa: BLE001 - capture verifier failure, never claim pass
        report = {"passed": False, "error": type(exc).__name__ + ": " + str(exc)}
    root.write_text(args.report, json.dumps(safe(report), indent=2, allow_nan=False) + "\n")
    print(json.dumps(safe(report), indent=2, allow_nan=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
