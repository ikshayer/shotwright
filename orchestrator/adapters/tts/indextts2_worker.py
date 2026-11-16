"""IndexTTS-2 synthesis worker: one process, one take.

Run by the orchestrator as ``<indextts python> -B -I indextts2_worker.py <request.json>``
with the IndexTTS-2 repository as the working directory. It deliberately imports only the
standard library and `indextts`; it must never import `orchestrator`, because it runs in a
different environment.

It never downloads: the caller sets ``HF_HUB_OFFLINE=1`` and ``TRANSFORMERS_OFFLINE=1``,
and every auxiliary model is handed to `IndexTTS2` as an explicit local path, so
`ensure_models_available` (which can reach the network) is never called. A missing file
is an error, not a download.

``--check <repo_dir>`` imports the runtime and prints its versions without loading any
model or touching the GPU.

The result file is JSON, replaced atomically as the run advances. It holds ``load_seconds``
once the models are on the GPU (generation is starting), then also ``generation_seconds``,
``max_memory_allocated_bytes`` and ``max_memory_reserved_bytes`` when the take is written, or
``error_code`` and ``error_message`` on failure. The parent polls it.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any

_AUXILIARY = (
    ("w2v_bert", "hf_cache/w2v-bert-2.0"),
    ("semantic_codec", "hf_cache/semantic_codec_model.safetensors"),
    ("campplus", "hf_cache/campplus_cn_common.bin"),
    ("bigvgan", "hf_cache/bigvgan"),
)


class WorkerFailure(Exception):
    """A failure the worker can name; `code` lets the parent classify it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _write_result(path: str, document: dict[str, Any]) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, allow_nan=False)
    os.replace(temporary, path)


def _auxiliary_paths(model_dir: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    for key, relative in _AUXILIARY:
        path = os.path.join(model_dir, *relative.split("/"))
        if not os.path.exists(path):
            raise WorkerFailure(
                "missing_model_file",
                f"auxiliary model is missing and will not be downloaded: {path}",
            )
        paths[key] = path
    return paths


def _forbid_downloads() -> None:
    """Make every modelscope / huggingface_hub download entry point raise.

    HF_HUB_OFFLINE does not stop modelscope (it has no offline variable; its patched
    `from_pretrained` calls `snapshot_download` whenever the given path does not exist), so
    a missing local file would otherwise be fetched from the network (FR-16).
    """
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise WorkerFailure("download_forbidden",
                            f"a model download was attempted and is forbidden: {args[:1]!r}")

    import importlib  # noqa: PLC0415

    huggingface_hub = importlib.import_module("huggingface_hub")
    hub_file_download = importlib.import_module("huggingface_hub.file_download")
    modelscope = importlib.import_module("modelscope")
    modelscope_snapshot = importlib.import_module("modelscope.hub.snapshot_download")
    modelscope_file = importlib.import_module("modelscope.hub.file_download")

    for module, names in (
        (huggingface_hub, ("snapshot_download", "hf_hub_download")),
        (hub_file_download, ("hf_hub_download",)),
        (modelscope, ("snapshot_download", "model_file_download")),
        (modelscope_snapshot, ("snapshot_download",)),
        (modelscope_file, ("model_file_download",)),
    ):
        for name in names:
            setattr(module, name, refuse)


def _prepare_imports(repo_dir: str) -> None:
    # -I removes both the script directory and the working directory from sys.path.
    if repo_dir not in sys.path:
        sys.path.insert(0, repo_dir)


def check(repo_dir: str) -> int:
    _prepare_imports(repo_dir)
    import torch  # noqa: PLC0415 - heavy imports are the point of the check.
    import transformers  # noqa: PLC0415

    from indextts.infer_v2 import IndexTTS2  # noqa: PLC0415,F401

    print(json.dumps({
        "python": sys.version.split()[0], "torch": torch.__version__,
        "transformers": transformers.__version__, "indextts_importable": True,
        "cuda_available": bool(torch.cuda.is_available()),
    }))
    return 0


def synthesise(request: dict[str, Any]) -> None:
    result_path = request["result_path"]
    _prepare_imports(request["repo_dir"])
    import torch  # noqa: PLC0415

    if not torch.cuda.is_available():
        raise WorkerFailure("cuda_unavailable", "CUDA is not available; IndexTTS-2 is a GPU adapter")
    _forbid_downloads()
    from indextts.infer_v2 import IndexTTS2  # noqa: PLC0415

    model_dir = request["model_dir"]
    emotion = request["emotion"]
    aux_paths = _auxiliary_paths(model_dir)
    torch.cuda.reset_peak_memory_stats()

    started = time.perf_counter()
    engine = IndexTTS2(
        cfg_path=request["cfg_path"], model_dir=model_dir, use_fp16=request["use_fp16"],
        use_qwen_emo=emotion["mode"] == "text", aux_paths=aux_paths,
        # None would enable BigVGAN's fused CUDA kernel, which may compile inside the
        # IndexTTS-2 tree; the runtime must not be written to.
        use_cuda_kernel=False,
    )
    load_seconds = time.perf_counter() - started
    _write_result(result_path, {"load_seconds": load_seconds})

    arguments: dict[str, Any] = {}
    if emotion["mode"] == "vector":
        arguments["emo_vector"] = emotion["emo_vector"]
    elif emotion["mode"] == "text":
        arguments.update(use_emo_text=True, emo_text=emotion["description"])
    started = time.perf_counter()
    produced = engine.infer(
        spk_audio_prompt=request["speaker_path"], text=request["text"],
        output_path=request["output_path"], use_random=False, verbose=False, **arguments,
    )
    generation_seconds = time.perf_counter() - started
    if produced is None or not os.path.isfile(request["output_path"]):
        raise WorkerFailure("no_audio", "IndexTTS-2 produced no audio")
    _write_result(result_path, {
        "load_seconds": load_seconds, "generation_seconds": generation_seconds,
        "max_memory_allocated_bytes": int(torch.cuda.max_memory_allocated()),
        "max_memory_reserved_bytes": int(torch.cuda.max_memory_reserved()),
        "cuda_device_name": torch.cuda.get_device_name(0), "torch": torch.__version__,
    })


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--check":
        return check(argv[2])
    if len(argv) != 2:
        print("usage: indextts2_worker.py <request.json> | --check <repo_dir>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as stream:
        request = json.load(stream)
    try:
        synthesise(request)
    except WorkerFailure as failure:
        _write_result(request["result_path"], {
            "error_code": failure.code, "error_message": str(failure),
        })
        print(str(failure), file=sys.stderr)
        return 1
    except BaseException as exc:  # noqa: BLE001 - classify for the parent, then fail loudly.
        text = traceback.format_exc()
        out_of_memory = "out of memory" in str(exc).lower() or type(exc).__name__ == "OutOfMemoryError"
        _write_result(request["result_path"], {
            "error_code": "out_of_memory" if out_of_memory else "internal",
            "error_message": f"{type(exc).__name__}: {exc}",
        })
        print(text, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
