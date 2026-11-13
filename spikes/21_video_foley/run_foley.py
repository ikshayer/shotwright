"""Bounded, offline XL/offload evaluation; no production media writes."""

from __future__ import annotations

import argparse
import ctypes
import functools
import gc
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

R = ProjectRoot(ROOT)
B = "spikes/21_video_foley/_work"
for k, v in {
    "TMP": "tmp",
    "TEMP": "tmp",
    "TMPDIR": "tmp",
    "HF_HOME": "cache/hf",
    "HUGGINGFACE_HUB_CACHE": "cache/hf/hub",
    "TORCH_HOME": "cache/torch",
    "XDG_CACHE_HOME": "cache/xdg",
    "NUMBA_CACHE_DIR": "cache/numba",
    "MPLCONFIGDIR": "cache/mpl",
    "CUDA_CACHE_PATH": "cache/cuda",
    "TORCHINDUCTOR_CACHE_DIR": "cache/inductor",
    "TRITON_CACHE_DIR": "cache/triton",
}.items():
    os.environ[k] = str(R.make_dir(B + "/" + v))
os.environ.update(
    HF_HUB_OFFLINE="1",
    TRANSFORMERS_OFFLINE="1",
    HF_HUB_DISABLE_TELEMETRY="1",
    PYTHONDONTWRITEBYTECODE="1",
    TOKENIZERS_PARALLELISM="false",
)
sys.path.insert(0, str(R.resolve(B + "/source")))


def digest(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(c)
    return h.hexdigest()


class Mem(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (n, ctypes.c_ulonglong)
        for n in [
            "total",
            "available",
            "page_total",
            "page_available",
            "virtual_total",
            "virtual_available",
            "extended",
        ]
    ]


def ram():
    m = Mem()
    m.length = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.available / 2**30


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--proxy", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--seconds", type=int, choices=[3, 8, 15], required=True)
    p.add_argument("--steps", type=int, default=50)
    p.add_argument("--prompt")
    p.add_argument("--negative-prompt")
    p.add_argument("--save-latents", action="store_true")
    a = p.parse_args()
    R.make_dir(a.out)
    R.write_text(a.out+"/run_foley_snapshot.py",R.read_text("spikes/21_video_foley/run_foley.py"))
    assert ram() > 6, "Need 6GiB free RAM before this diagnostic run (guard, not measured demand)"
    models = json.loads(R.read_text(B + "/verified-models.json"))
    for i in models:
        assert digest(R.resolve(i["path"])) == i["sha256"], i["name"] + " changed"
    import numpy as np
    import soundfile as sf
    import torch
    from torchvision import transforms
    from torchvision.transforms import v2
    from transformers import AutoModel, AutoTokenizer, ClapTextModelWithProjection

    torch.set_num_threads(4)
    torch.cuda.set_device(0)
    assert torch.cuda.is_available() and "NVIDIA" in torch.cuda.get_device_name(0)
    print(
        "CUDA",
        torch.__version__,
        torch.version.cuda,
        torch.cuda.get_device_name(0),
        torch.cuda.get_device_capability(0),
        flush=True,
    )
    # Execute a real kernel on sm_120 before loading large models.
    z = torch.ones(8, device="cuda")
    assert float((z * z).sum()) == 8
    del z
    from hunyuanvideo_foley.models.dac_vae.model.dac import DAC
    from hunyuanvideo_foley.utils import feature_utils as fu
    from hunyuanvideo_foley.utils import model_utils as mu

    latent_capture = None
    if a.save_latents:
        from safetensors.torch import save_file

        original_decode = DAC.decode
        latent_rel = a.out + "/dac_input_latent.safetensors"
        latent_capture = {}

        def capture_decode(self, z):
            if latent_capture:
                raise RuntimeError("DAC.decode called more than once; refusing to overwrite captured latent")
            cpu_latent = z.detach().to(device="cpu").contiguous()
            R.write_with(
                latent_rel,
                lambda target: save_file({"latent": cpu_latent}, str(target)),
            )
            latent_capture.update(
                {
                    "path": latent_rel,
                    "sha256": digest(R.resolve(latent_rel)),
                    "shape": list(z.shape),
                    "dtype": str(z.dtype),
                    "saved_dtype": str(cpu_latent.dtype),
                    "source_device": str(z.device),
                    "format": "safetensors",
                }
            )
            return original_decode(self, z)

        DAC.decode = capture_decode

    original_load = torch.load

    def safe_load(path, *args, **kwargs):
        if kwargs.get("weights_only") is False:
            raise RuntimeError("Unsafe tensor loading requested")
        kwargs["weights_only"] = True
        return original_load(path, *args, **kwargs)

    torch.load = safe_load

    def tensor_load(path):
        return torch.load(path, map_location="cpu", weights_only=True, mmap=True)

    def state_load(model, path):
        state = tensor_load(path)
        model.load_state_dict(state, strict=True)
        del state
        gc.collect()
        return model

    mu.load_state_dict = state_load

    def main_meta(self):
        if self._foley_model is None:
            print("MAIN stage meta constructor", flush=True)
            with torch.device("meta"):
                model = mu.HunyuanVideoFoley(
                    self.cfg, dtype=torch.bfloat16, device=torch.device("meta")
                )
            print("MAIN stage verified tensor mmap", flush=True)
            state = tensor_load(Path(self.model_path) / self.model_filename)
            print("MAIN stage strict assign", flush=True)
            model.load_state_dict(state, strict=True, assign=True)
            assert not any(v.is_meta for v in list(model.parameters()) + list(model.buffers())), (
                "Unmaterialized model state"
            )
            del state
            print("MAIN stage CUDA transfer", flush=True)
            model = model.to(device=self.device, dtype=torch.bfloat16).eval()
            torch.cuda.synchronize()
            self._foley_model = model
            gc.collect()
            print("MAIN ready", flush=True)
        return self._foley_model

    mu.OffloadModelManager._load_foley_model = main_meta

    def safe_dac(cls, path):
        checkpoint = tensor_load(path)
        print("DAC checkpoint fields", list(checkpoint), flush=True)
        metadata = checkpoint["metadata"]
        kwargs = metadata["kwargs"]
        print("DAC constructor", kwargs, flush=True)
        model = cls(**kwargs)
        model.load_state_dict(checkpoint["state_dict"], strict=True)
        del checkpoint
        gc.collect()
        return model

    DAC.load = classmethod(safe_dac)

    def siglip(self):
        if self._siglip2_model is None:
            self._siglip2_preprocess = transforms.Compose(
                [
                    transforms.Resize((512, 512)),
                    transforms.ToTensor(),
                    transforms.Normalize(mean=[0.5] * 3, std=[0.5] * 3),
                ]
            )
            self._siglip2_model = (
                AutoModel.from_pretrained(
                    str(R.resolve(B + "/models/siglip")),
                    local_files_only=True,
                    use_safetensors=True,
                )
                .to(self.device)
                .eval()
            )
        return self._siglip2_model, self._siglip2_preprocess

    def clap(self):
        if self._clap_model is None:
            path = str(R.resolve(B + "/models/clap"))
            self._clap_tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
            self._clap_model = (
                ClapTextModelWithProjection.from_pretrained(path, local_files_only=True)
                .to(self.device)
                .eval()
            )
        return self._clap_model, self._clap_tokenizer

    def sync(self):
        if self._syncformer_model is None:
            self._syncformer_preprocess = v2.Compose(
                [
                    v2.Resize(224, interpolation=v2.InterpolationMode.BICUBIC),
                    v2.CenterCrop(224),
                    v2.ToImage(),
                    v2.ToDtype(torch.float32, scale=True),
                    v2.Normalize(mean=[0.5] * 3, std=[0.5] * 3),
                ]
            )
            self._syncformer_model = mu.Synchformer()
            self._syncformer_model.load_state_dict(
                tensor_load(R.resolve(B + "/models/foley/synchformer_state_dict.pth")), strict=True
            )
            self._syncformer_model = self._syncformer_model.to(self.device).eval()
        return self._syncformer_model, self._syncformer_preprocess

    mu.OffloadModelManager._load_siglip2_model = siglip
    mu.OffloadModelManager._load_clap_model = clap
    mu.OffloadModelManager._load_syncformer_model = sync
    fu.encode_video_with_siglip2 = functools.partial(fu.encode_video_with_siglip2, batch_size=4)
    fu.encode_video_with_sync = functools.partial(fu.encode_video_with_sync, batch_size=2)
    seed = 210915
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    prompt = "Quiet residential living room ambience. Soft steady ventilation hum, distant building rumble, occasional faint wooden floor creaks. Restrained natural room sound."
    prompt = a.prompt or prompt
    negative = a.negative_prompt if a.negative_prompt is not None else "speech, voices, dialogue, music, singing, loud impacts, harsh distortion"
    samples = []
    done = threading.Event()

    def monitor():
        while not done.is_set():
            try:
                gpu = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    text=True,
                    timeout=8,
                ).strip()
                samples.append(
                    {
                        "elapsed_seconds": round(time.monotonic() - start, 3),
                        "whole_device_vram_mib": float(gpu),
                        "available_ram_gib": ram(),
                    }
                )
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                samples.append({"monitor_error": str(e)})
            done.wait(1)

    start = time.monotonic()
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        torch.cuda.reset_peak_memory_stats()
        model, cfg = mu.load_model(
            str(R.resolve(B + "/models/foley")),
            str(R.resolve(B + "/source/configs/hunyuanvideo-foley-xl.yaml")),
            torch.device("cuda:0"),
            enable_offload=True,
            model_size="xl",
        )
        print("Extracting video and text features", flush=True)
        visual, text, length = fu.feature_process(
            str(R.resolve(a.proxy)), prompt, model, cfg, neg_prompt=negative
        )
        print("Feature duration", length, "seconds; denoising", a.steps, "steps", flush=True)
        assert abs(length - a.seconds) < 0.1, (length, a.seconds)
        audio, sr = mu.denoise_process(
            visual, text, length, model, cfg, guidance_scale=4.5, num_inference_steps=a.steps
        )
        data = audio[0].detach().float().cpu().numpy()
        if data.ndim == 1:
            data = data[None, :]
        assert data.ndim == 2 and np.isfinite(data).all(), data.shape
        native = a.out + "/native.wav"
        R.write_with(native, lambda target: sf.write(str(target), data.T, sr, subtype="FLOAT"))
        metrics = {
            "native_wav": {
                "path": native,
                "sha256": digest(R.resolve(native)),
                "sample_rate": sr,
                "channels": data.shape[0],
                "sample_count": data.shape[-1],
                "start_sample": 0,
                "transforms": [],
            },
            "execution_harness": {"path":a.out+"/run_foley_snapshot.py", "sha256":digest(R.resolve(a.out+"/run_foley_snapshot.py"))},
            "prompt": prompt,
            "negative_prompt": negative,
            "seed": seed,
            "steps": a.steps,
            "precision": "bfloat16 main; float32 feature encoders and DAC",
            "offload": True,
            "device": torch.cuda.get_device_name(0),
            "device_index": 0,
            "cuda_capability": torch.cuda.get_device_capability(0),
            "versions": {
                k: importlib.metadata.version(k)
                for k in [
                    "torch",
                    "torchaudio",
                    "torchvision",
                    "transformers",
                    "diffusers",
                    "descript-audiotools",
                ]
            },
            "proxy": {"path": a.proxy, "sha256": digest(R.resolve(a.proxy))},
            "wall_seconds": time.monotonic() - start,
            "torch_peak_allocated_mib": torch.cuda.max_memory_allocated() / 2**20,
            "samples": samples,
            "memory_observation_scope": "whole-device NVML samples; allocator peak covers this torch process only, not a reservation requirement",
            "patches": [
                "strict verified state tensors with weights_only=True mmap",
                "main meta construction; exact assign=True state; no random GPU initialization",
                "bypass DAC package unpickler; instantiate metadata kwargs and exact state",
                "pinned local offline SigLIP2 and CLAP",
                "bound SigLIP batch4 and Sync batch2",
                "explicit NVIDIA CUDA device0; actual kernel probe",
            ],
        }
        if latent_capture is not None:
            if not latent_capture:
                raise RuntimeError("DAC decoder was not called; requested latent was not captured")
            metrics["decoder_input_latent"] = latent_capture
        R.write_text(a.out + "/generation.json", json.dumps(metrics, indent=2) + "\n")
        print("GENERATED", native, data.shape, sr, "wall", metrics["wall_seconds"], flush=True)
    except BaseException as e:
        R.write_text(
            a.out + "/failure.json",
            json.dumps(
                {
                    "error_type": type(e).__name__,
                    "message": str(e),
                    "wall_seconds": time.monotonic() - start,
                    "samples": samples,
                },
                indent=2,
            )
            + "\n",
        )
        raise
    finally:
        done.set()
        thread.join(timeout=10)


if __name__ == "__main__":
    main()
