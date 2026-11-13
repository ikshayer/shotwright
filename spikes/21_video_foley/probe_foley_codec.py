"""Conditional codec component probe. Never generated Foley; no diffusion runs.

Default launches a bounded isolated worker. --help does not import model packages.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

R = ProjectRoot(ROOT)
BASE = "spikes/21_video_foley/_work"
WEIGHT = BASE + "/models/foley/vae_128d_48k.pth"
EXPECTED = "07e6139ff33bd21ba8a7a7f40ed24aab13ac0d100cd686f8a9f3e03dc5251cb1"
LABEL = "NOT_HUNYUAN_GENERATION"


class Mem(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (name, ctypes.c_ulonglong) for name in
        ["total", "available", "pt", "pa", "vt", "va", "e"]]


def available_ram() -> float:
    mem = Mem()
    mem.length = ctypes.sizeof(mem)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        raise OSError("GlobalMemoryStatusEx failed")
    return mem.available / 2**30


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4194304), b""):
            result.update(chunk)
    return result.hexdigest()


def decode_captured(model, args: argparse.Namespace, weight_hash: str) -> int:
    """Decode one hash-checked latent without diffusion or rescaling."""
    import numpy as np
    import soundfile as sf
    import torch
    from safetensors.torch import load_file

    latent_path = R.resolve(args.latent)
    latent_hash = digest(latent_path)
    if latent_hash != args.latent_sha256.lower():
        raise ValueError("Captured latent SHA256 mismatch")
    tensors = load_file(str(latent_path), device="cpu")
    if set(tensors) != {"latent"}:
        raise ValueError("Expected exactly one safetensors key: latent")
    latent = tensors["latent"]
    if latent.ndim != 3 or latent.shape[0] != 1 or not torch.isfinite(latent).all():
        raise ValueError("Captured latent must be finite single-batch BCT")
    print("decoding hash-checked captured latent; no generation", flush=True)
    with torch.inference_mode():
        decoded = model.decode(latent.to(device=args.device)).detach().cpu().float().numpy()
    if decoded.ndim != 3 or decoded.shape[0] != 1 or not np.isfinite(decoded).all():
        raise ValueError("Unexpected or nonfinite captured decode")
    output = decoded[0].T
    native_path = R.resolve(args.compare_native)
    native_hash = digest(native_path)
    native, rate = sf.read(str(native_path), dtype="float64", always_2d=True)
    if rate != 48000 or not np.isfinite(native).all() or not native.size or not output.size:
        raise ValueError("Native comparison must be nonempty finite 48kHz audio")
    relative = args.out + "/NOT_NEW_GENERATION_captured_decode.wav"
    R.write_with(relative, lambda target: sf.write(str(target), output, 48000, subtype="FLOAT", format="WAV"))
    overlap = min(len(native), len(output))
    comparisons = []
    if output.shape[1] == native.shape[1]:
        channel_pairs = [(i,i) for i in range(native.shape[1])]
    elif output.shape[1] == 1:
        channel_pairs = [(0,i) for i in range(native.shape[1])]
    else:
        channel_pairs = []
    for output_channel, native_channel in channel_pairs:
        difference = output[:overlap,output_channel].astype(np.float64) - native[:overlap,native_channel]
        comparisons.append({"decoded_channel":output_channel,"native_channel":native_channel,
                            "difference_rms":float(np.sqrt(np.mean(difference**2))),
                            "difference_max_abs":float(np.max(np.abs(difference)))})
    report = {"label":"NOT_NEW_GENERATION", "purpose":"independent captured-latent codec decode only; no diffusion",
              "weight":{"path":WEIGHT,"sha256":weight_hash}, "device":args.device,
              "torch_version":torch.__version__, "latent":{"path":args.latent,"sha256":latent_hash,
              "shape":list(latent.shape),"dtype":str(latent.dtype)},
              "source_dac_sha256":digest(R.resolve(BASE + "/source/hunyuanvideo_foley/models/dac_vae/model/dac.py")),
              "native":{"path":args.compare_native,"sha256":native_hash,"shape":list(native.shape),
                        "sample_count":len(native),"duration_seconds":len(native)/rate,
                        "rms":float(np.sqrt(np.mean(native**2))),"dc":float(np.mean(native))},
              "decoded":{"path":relative,"sha256":digest(R.resolve(relative)),"shape":list(output.shape),
                         "sample_count":len(output),"duration_seconds":len(output)/48000,
                         "rms":float(np.sqrt(np.mean(output.astype(np.float64)**2))),
                         "dc":float(np.mean(output.astype(np.float64)))},
              "overlap_sample_count":overlap,"channel_comparisons":comparisons,
              "sample_counts_equal":len(native)==len(output),"normalization":None,"pass_threshold":None,
              "limits":["Compares exact overlapping samples without delay alignment or normalization.",
                        "Mono decode is explicitly compared separately to each native channel; no output duplication written.",
                        "Native file hash records local identity; only latent hash is independently declared and checked.",
                        "No diffusion, text/video conditioning, CPU/GPU agreement or semantic Foley validation performed."]}
    R.write_text(args.out + "/captured_decode_report.json", json.dumps(report,indent=2,allow_nan=False) + "\n")
    return 0


def worker(args: argparse.Namespace) -> int:
    # Cache environment is established by the parent before scientific imports.
    import numpy as np
    import soundfile as sf
    import torch

    sys.path.insert(0, str(R.resolve(BASE + "/source")))
    from hunyuanvideo_foley.models.dac_vae.model.dac import DAC

    torch.set_num_threads(4)
    torch.manual_seed(210915)
    if args.device == "cuda":
        torch.cuda.set_device(0)
        if not torch.cuda.is_available() or "NVIDIA" not in torch.cuda.get_device_name(0):
            raise RuntimeError("Explicit NVIDIA CUDA device required")
    path = R.resolve(WEIGHT)
    actual = digest(path)
    if actual != EXPECTED:
        raise ValueError("Pinned DAC weight digest mismatch")
    print("verified DAC digest; tensor-only mmap load", flush=True)
    checkpoint = torch.load(path, map_location="cpu", weights_only=True, mmap=True)
    model = DAC(**checkpoint["metadata"]["kwargs"])
    model.load_state_dict(checkpoint["state_dict"], strict=True)
    del checkpoint
    model.eval().requires_grad_(False).to(args.device)
    if args.latent:
        return decode_captured(model, args, actual)
    rate = 48000
    count = 2 * rate
    t = np.arange(count, dtype=np.float64) / rate
    signal = .15 * np.sin(2 * np.pi * 440 * t)
    rng = np.random.default_rng(210915)
    for onset in [.25, .75, 1.25, 1.75]:
        start, length = round(onset * rate), round(.08 * rate)
        signal[start:start+length] += .25 * rng.standard_normal(length) * np.exp(-np.arange(length) / (rate * .012))
    signal = signal.astype(np.float32)
    tensor = torch.from_numpy(signal).reshape(1, 1, -1).to(args.device)
    print("encoding own procedural 2s signal", flush=True)
    with torch.inference_mode():
        posterior = model.encode(tensor)[0]
        latent = posterior.mode()
        decoded = model.decode(latent).detach().cpu().float().numpy()
    if decoded.ndim != 3 or decoded.shape[:2] != (1, 1):
        raise ValueError(f"Unexpected decode shape {decoded.shape}")
    output = decoded[0, 0]
    if not np.isfinite(output).all():
        raise ValueError("Nonfinite decode")
    overlap = min(len(signal), len(output))
    x, y = signal[:overlap].astype(np.float64), output[:overlap].astype(np.float64)
    rms_in, rms_out = float(np.sqrt(np.mean(x*x))), float(np.sqrt(np.mean(y*y)))
    std_product = float(np.std(x) * np.std(y))
    corr = float(np.mean((x-x.mean())*(y-y.mean())) / std_product) if std_product else None
    def write_audio(relative: str, data: np.ndarray) -> dict:
        R.write_with(relative, lambda target: sf.write(str(target), data, rate, subtype="FLOAT", format="WAV"))
        return {"path": relative, "sha256": digest(R.resolve(relative)), "sample_count": len(data), "duration_seconds": len(data)/rate}
    report = {
        "label": LABEL, "purpose": "codec reconstruction diagnostic only; no diffusion; never mux into basketball audition",
        "weight": {"path": WEIGHT, "sha256": actual}, "device": args.device,
        "torch_version": torch.__version__, "latent_shape": list(latent.shape),
        "source_dac_sha256": digest(R.resolve(BASE + "/source/hunyuanvideo_foley/models/dac_vae/model/dac.py")),
        "procedural_parameters": {"seed": 210915, "rate": rate, "sine_hz": 440, "sine_amplitude": .15,
                                  "impulse_times": [.25,.75,1.25,1.75], "noise_amplitude": .25,
                                  "impulse_duration_seconds": .08, "decay_seconds": .012},
        "input": write_audio(args.out + "/NOT_HUNYUAN_GENERATION_input.wav", signal),
        "reconstruction": write_audio(args.out + "/NOT_HUNYUAN_GENERATION_reconstruction.wav", output),
        "input_rms_full": float(np.sqrt(np.mean(signal.astype(np.float64)**2))),
        "output_rms_full": float(np.sqrt(np.mean(output.astype(np.float64)**2))),
        "input_dc": float(np.mean(signal.astype(np.float64))),
        "overlap_sample_count": overlap, "input_rms_overlap": rms_in, "output_rms_overlap": rms_out,
        "rms_ratio_overlap": rms_out/rms_in, "zero_lag_correlation_overlap": corr,
        "output_dc": float(np.mean(output.astype(np.float64))),
        "output_sample_peak": float(np.max(np.abs(output))),
        "normalization": None, "pass_threshold": None,
        "limits": ["Zero-lag correlation can be affected by codec delay; no latency alignment applied.",
                   "One procedural input cannot validate diffusion, semantic Foley quality, or all codec behavior.",
                   "No captured latent comparison implemented. Source digest records local identity, not remote authenticity."]}
    R.write_text(args.out + "/codec_report.json", json.dumps(report, indent=2, allow_nan=False) + "\n")
    print("component probe complete", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="new worktree-relative output directory")
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--latent", help="worktree-relative captured safetensors; no new generation")
    parser.add_argument("--latent-sha256", help="independently declared captured latent SHA256")
    parser.add_argument("--compare-native", help="worktree-relative unchanged native WAV")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any([args.latent, args.latent_sha256, args.compare_native]) and not all([args.latent, args.latent_sha256, args.compare_native]):
        parser.error("--latent, --latent-sha256 and --compare-native must be supplied together")
    if args.latent_sha256 and (len(args.latent_sha256) != 64 or any(c not in "0123456789abcdef" for c in args.latent_sha256.lower())):
        parser.error("--latent-sha256 must contain 64 hexadecimal characters")
    if args.worker:
        return worker(args)
    if available_ram() < 6:
        raise RuntimeError("Admission requires 6 GiB available RAM; policy, not measured model demand")
    if R.resolve(args.out).exists():
        raise FileExistsError("Use a new output directory; retained artifacts must not be overwritten")
    R.make_dir(args.out)
    env = os.environ.copy()
    for key in ["TMP", "TEMP", "TMPDIR", "HF_HOME", "HUGGINGFACE_HUB_CACHE", "TORCH_HOME",
                "XDG_CACHE_HOME", "NUMBA_CACHE_DIR", "MPLCONFIGDIR", "CUDA_CACHE_PATH",
                "TORCHINDUCTOR_CACHE_DIR", "TRITON_CACHE_DIR", "HOME", "USERPROFILE"]:
        env[key] = str(R.make_dir(args.out + "/isolation/" + key.lower()))
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               PYTHONDONTWRITEBYTECODE="1", PYTHONFAULTHANDLER="1", TOKENIZERS_PARALLELISM="false")
    command = [str(R.resolve(BASE + "/env/Scripts/python.exe")), "-B", str(Path(__file__).resolve()),
               "--worker", "--out", args.out, "--device", args.device]
    if args.latent:
        command.extend(["--latent",args.latent,"--latent-sha256",args.latent_sha256,"--compare-native",args.compare_native])
    report = {"label": "NOT_NEW_GENERATION" if args.latent else LABEL, "command": command, "policy": {"admission_ram_gib":6,
              "stop_ram_gib":2, "deadline_seconds":360, "sample_seconds":1}, "samples":[]}
    def execute(log: Path) -> None:
        started = time.monotonic()
        with log.open("wb") as stream:
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
            try:
                while child.poll() is None:
                    elapsed, ram = time.monotonic()-started, available_ram()
                    report["samples"].append({"elapsed_seconds":elapsed,"available_ram_gib":ram})
                    if elapsed >= 360 or ram < 2:
                        report["watchdog_stop"] = "deadline" if elapsed >= 360 else "low_ram"
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=10)
                        break
                    time.sleep(1)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
            report["exit_code"] = child.returncode
            report["elapsed_seconds"] = time.monotonic()-started
    R.write_with(args.out + "/codec_worker.log", execute)
    R.write_text(args.out + "/codec_watchdog.json", json.dumps(report, indent=2) + "\n")
    return report["exit_code"] or (1 if report.get("watchdog_stop") else 0)


if __name__ == "__main__":
    raise SystemExit(main())
