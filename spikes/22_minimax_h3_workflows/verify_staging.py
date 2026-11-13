"""Independent CPU-only Amendment4 boundary probes; no GPU, server or weights.

The native dependency boundary is controlled, not the staged implementation. Each
case imports the public extension afresh and exercises exported node methods.
These probes cannot prove real-model weakref release or actual memory savings.
"""
from __future__ import annotations

import argparse
import copy
import gc
import importlib.util
import json
import os
import sys
import types
import weakref
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

BASE = "spikes/22_minimax_h3_workflows"
EXTENSION = BASE + "/stage_extension/h3_staging/__init__.py"
REPORT = BASE + "/_work/local_runs/_staging/report.json"


def require(value, message):
    if not value:
        raise AssertionError(message)


def denied(operation):
    try:
        operation()
    except (RuntimeError, ValueError, TypeError, KeyError, PermissionError):
        return
    raise AssertionError("operation accepted an invalid release/dependency")


class Boundary:
    """Observable weakref-able wrappers and exact native-call sentinels."""
    def __init__(self):
        self.calls = []
        self.refs = []
        self.retained = []
        self.retain = False
        self.fail_native = False
        self.fail_video_vae = False
        self.fail_unload = False
        self.reference_latent = types.SimpleNamespace(shape=(1, 24, 57, 18, 24))
        self.conditioning = [[object(), {"native_metadata": object(), "minimax_refs": [{"kind": "video", "latent_t": 57, "latent_h": 18, "latent_w": 24, "ref_audio_t": 0, "latent": self.reference_latent, "audio_latent": None}]}]]
        self.latent = {"samples": object(), "native_audio": object()}
        self.sampled = {"samples": object(), "sampled_audio": object()}
        self.model_result = (object(),)
        self.frames = types.SimpleNamespace(shape=(192, 288, 384, 3))
        self.names = {row["role"]: Path(row["model_relative_path"]).name for row in json.loads((ROOT / BASE / "LOCAL_MODEL_CATALOG.json").read_text())["components"]}

    def wrapper(self, role):
        class Item:
            pass
        underlying = Item()
        patcher = Item()
        patcher.model = underlying
        wrapper = Item()
        wrapper.patcher = patcher
        wrapper.first_stage_model = underlying
        wrapper.cond_stage_model = underlying
        self.refs.extend((role + suffix, weakref.ref(value)) for suffix, value in ((".wrapper", wrapper), (".patcher", patcher), (".model", underlying)))
        return wrapper

    def modules(self):
        boundary = self
        nodes = types.ModuleType("nodes")
        class CLIPLoader:
            def load_clip(self, clip_name, type="stable_diffusion", device="default"):
                boundary.calls.append(("clip", clip_name, type, device))
                require(clip_name == boundary.names["text_encoder"] and type == "minimax" and device == "default", "incorrect native encoder delegation")
                return (boundary.wrapper("clip"),)
        class VAELoader:
            def load_vae(self, vae_name):
                boundary.calls.append(("vae", vae_name))
                require(vae_name in (boundary.names["video_vae"], boundary.names["audio_vae"]), "incorrect native VAE filename")
                if boundary.fail_video_vae and vae_name == boundary.names["video_vae"]:
                    raise RuntimeError("independent injected partial video VAE loader failure")
                return (boundary.wrapper(vae_name),)
        class UNETLoader:
            def load_unet(self, unet_name, weight_dtype):
                gc.collect()
                require(all(ref() is None for _, ref in boundary.refs), "UNET loaded before all conditioning references died")
                require(unet_name == boundary.names["diffusion"] and weight_dtype == "default", "incorrect diffusion delegation")
                boundary.calls.append(("unet", unet_name, weight_dtype))
                return boundary.model_result
        nodes.CLIPLoader, nodes.VAELoader, nodes.UNETLoader = CLIPLoader, VAELoader, UNETLoader
        native = types.ModuleType("comfy_extras.nodes_minimax_h3")
        class MiniMaxH3ReferenceToVideo:
            @classmethod
            def execute(cls, clip, prompt, width, height, length, ref_image_size="match", vae=None, audio_vae=None, ref_images=None, ref_videos=None, ref_video_audios=None, ref_audios=None):
                require(prompt == "independent native delegation probe", "prompt changed")
                require((width, height, length) == (384, 288, 192), "canvas changed")
                require(vae is not None and audio_vae is not None and clip is not None, "missing native model inputs")
                require(ref_videos == {"ref_video_0": boundary.frames} and next(iter(ref_videos.values())) is boundary.frames, "full frame reference not preserved")
                require(not ref_images and not ref_video_audios and not ref_audios, "unexpected image/audio reference")
                boundary.calls.append(("condition", width, height, length))
                if boundary.retain:
                    boundary.retained.append(clip)
                if boundary.fail_native:
                    raise RuntimeError("independent injected native conditioning failure")
                return types.SimpleNamespace(result=(boundary.conditioning, boundary.latent))
        native.MiniMaxH3ReferenceToVideo = MiniMaxH3ReferenceToVideo
        mm = types.ModuleType("comfy.model_management")
        def unload(patcher, all_devices=False):
            require(all_devices is True, "targeted unload must cover all devices")
            require(any(ref() is patcher for role, ref in boundary.refs if role.endswith(".patcher")), "unload targets foreign patcher")
            boundary.calls.append(("unload",))
            if boundary.fail_unload:
                raise RuntimeError("independent injected release failure")
        mm.unload_model_and_clones = unload
        mm.cleanup_models = lambda *a, **k: boundary.calls.append(("cleanup",))
        mm.soft_empty_cache = lambda *a, **k: boundary.calls.append(("empty_cache",))
        mm.current_loaded_models = []
        mm.loaded_models = lambda *a, **k: []
        comfy = types.ModuleType("comfy")
        comfy.__path__ = []
        comfy.model_management = mm
        extras = types.ModuleType("comfy_extras")
        extras.__path__ = []
        extras.nodes_minimax_h3 = native
        torch = types.ModuleType("torch")
        torch.cuda = types.SimpleNamespace(is_available=lambda: False, synchronize=lambda *a, **k: boundary.calls.append(("sync",)), memory_allocated=lambda *a: 0, memory_reserved=lambda *a: 0)
        return {"nodes": nodes, "comfy": comfy, "comfy.model_management": mm, "comfy_extras": extras, "comfy_extras.nodes_minimax_h3": native, "torch": torch}

    def condition(self, module):
        return module.NODE_CLASS_MAPPINGS["CCTVH3ConditionAndRelease"]().execute(self.names["text_encoder"], self.names["video_vae"], self.names["audio_vae"], "independent native delegation probe", 384, 288, 192, self.frames)

    def unet(self, module, token):
        return module.NODE_CLASS_MAPPINGS["CCTVH3StagedUNETLoader"]().execute(token, self.names["diffusion"], "default")

    def decode(self, module, token, samples=None):
        return module.NODE_CLASS_MAPPINGS["CCTVH3DecodeVAELoader"]().execute(self.sampled if samples is None else samples, token, self.names["video_vae"], self.names["audio_vae"])


def load_extension(label):
    name = "independent_h3_staging_" + label
    spec = importlib.util.spec_from_file_location(name, ROOT / EXTENSION)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def success(boundary, module):
    conditioning, latent, token = boundary.condition(module)
    require(conditioning is boundary.conditioning and latent is boundary.latent, "native NodeOutput objects changed")
    require(all(ref() is None for _, ref in boundary.refs), "conditioning references remain alive after release")
    require(token["schema_version"] == 1 and token["status"] == "released" and token["pid"] == os.getpid(), "invalid primitive token")
    require((token["width"], token["height"], token["length"]) == (384, 288, 192), "token canvas differs")
    json.dumps(token, allow_nan=False)
    receipt = token["receipt"]
    import hashlib
    require(hashlib.sha256(ProjectRoot(ROOT).read_bytes(receipt["path"])).hexdigest() == receipt["sha256"], "release receipt hash differs")
    vae_calls = sum(call[0] == "vae" for call in boundary.calls)
    require(vae_calls == 2, "decode VAE reloaded before sample dependency")
    result = boundary.unet(module, token)
    require(isinstance(result, tuple) and len(result) == 1 and result[0] is boundary.model_result[0], "native MODEL output changed")
    sampled, video, audio = boundary.decode(module, token)
    require(sampled is boundary.sampled and video is not audio, "sampled latent/native decoder outputs changed")
    require(sum(call[0] == "vae" for call in boundary.calls) == 4, "decode did not reload exactly both VAEs")
    denied(lambda: boundary.unet(module, token))
    require(sum(call[0] == "unet" for call in boundary.calls) == 1, "token reuse instantiated diffusion")


def bad_tokens(boundary, module):
    _, _, token = boundary.condition(module)
    for field, value in (("release_id", "forged"), ("pid", os.getpid() + 1), ("invocation_id", "foreign"), ("status", "failed"), ("width", 640), ("create_time", -1)):
        forged = copy.deepcopy(token)
        forged[field] = value
        denied(lambda forged=forged: boundary.unet(module, forged))
    denied(lambda: boundary.unet(module, {}))
    denied(lambda: boundary.decode(module, token))
    other = load_extension("foreign_registry")
    denied(lambda: boundary.unet(other, token))
    require(not any(call[0] == "unet" for call in boundary.calls), "invalid token loaded diffusion")


def release_failure(boundary, module):
    boundary.fail_unload = True
    denied(lambda: boundary.condition(module))
    require(not any(call[0] == "unet" for call in boundary.calls), "release failure loaded diffusion")


def retained_failure(boundary, module):
    boundary.retain = True
    denied(lambda: boundary.condition(module))
    require(bool(boundary.retained), "retained-reference test never reached native boundary")
    require(not any(call[0] == "unet" for call in boundary.calls), "retained reference loaded diffusion")


def native_failure(boundary, module):
    boundary.fail_native = True
    denied(lambda: boundary.condition(module))
    require(any(call[0] == "condition" for call in boundary.calls), "native failure test never reached native boundary")
    gc.collect()
    require(all(ref() is None for _, ref in boundary.refs), "native failure retained models")
    require(sum(call[0] == "unload" for call in boundary.calls) == 3, "native failure must target-unload all three models")


def partial_loader_failure(boundary, module):
    boundary.fail_video_vae = True
    denied(lambda: boundary.condition(module))
    require([call[0] for call in boundary.calls if call[0] in {"clip", "vae", "condition", "unet"}] == ["clip", "vae"], "partial loader failure did not stop at video VAE load")
    gc.collect()
    require(all(ref() is None for _, ref in boundary.refs), "partial loader failure retained successful CLIP")
    require(sum(call[0] == "unload" for call in boundary.calls) == 1, "partial loader failure did not target-unload successful CLIP")


def schema_smoke(root):
    """Actual CPU-only loopback /object_info; never queues /prompt."""
    import socket
    import subprocess
    import time
    import urllib.request
    import uuid
    run = BASE + "/_work/local_runs/_staging/schema_" + uuid.uuid4().hex
    runtime = BASE + "/_work/local_runtime"
    # Approved lifecycle API import only: implementation source remains unread.
    spec = importlib.util.spec_from_file_location("h3_owned_lifecycle_for_smoke", ROOT / BASE / "local_h3.py")
    controller = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = controller
    spec.loader.exec_module(controller)
    for directory in ("state", "state/custom_nodes", "input", "output", "temp", "user", "previews", "cache"):
        root.write_text(run + "/" + directory + "/.contained", "model-free schema smoke\n")
    config = run + "/extra_paths.yaml"
    extension_parent = root.resolve(BASE + "/stage_extension")
    root.write_text(config, "h3_smoke:\n  custom_nodes: " + json.dumps(str(extension_parent)) + "\n")
    catalog = json.loads(root.read_text(BASE + "/LOCAL_MODEL_CATALOG.json"))
    python = root.resolve(runtime + "/env/Scripts/python.exe")
    main = root.resolve(runtime + "/source/ComfyUI/main.py")
    cli = root.read_text(runtime + "/source/ComfyUI/comfy/cli_args.py")
    flags = ["--cpu", "--offline", "--disable-api-nodes", "--disable-all-custom-nodes", "--whitelist-custom-nodes", "--extra-model-paths-config", "--models-directory", "--base-directory", "--input-directory", "--output-directory", "--temp-directory", "--previews-directory", "--user-directory", "--database-url", "--disable-auto-launch"]
    require(all(flag in cli for flag in flags), "required CPU smoke flags not present in pinned CLI")
    with socket.socket() as guard:
        guard.bind(("127.0.0.1", 8296))
    args = [str(python), "-B", str(main), "--listen", "127.0.0.1", "--port", "8296", "--cpu", "--offline", "--disable-api-nodes", "--disable-auto-launch", "--disable-all-custom-nodes", "--whitelist-custom-nodes", "h3_staging", "--extra-model-paths-config", str(root.resolve(config)), "--models-directory", str(root.resolve(catalog["model_root"])), "--base-directory", str(root.resolve(run + "/state")), "--database-url", "sqlite:///" + root.resolve(run + "/user/comfy.db").as_posix()]
    for flag, directory in (("--input-directory", "input"), ("--output-directory", "output"), ("--temp-directory", "temp"), ("--previews-directory", "previews"), ("--user-directory", "user")):
        args += [flag, str(root.resolve(run + "/" + directory))]
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONHOME", "PYTHONPATH"} and not any(term in key.upper() for term in ("TOKEN", "SECRET", "CREDENTIAL", "API_KEY"))}
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "CUDA_VISIBLE_DEVICES": "0", "PYTHONDONTWRITEBYTECODE": "1", "CCTV_H3_RUN_REL": run})
    for key in ("TEMP", "TMP", "HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME", "MPLCONFIGDIR", "NUMBA_CACHE_DIR"):
        env[key] = str(root.resolve(run + ("/temp" if key in ("TEMP", "TMP") else "/cache")))
    result = {"run": run, "model_free": True, "prompt_submitted": False, "gpu_generation": False, "cuda_capability_probe_possible": True, "zero_cuda_context_claimed": False, "argv": args}
    def execute(log_path):
        owner = None
        process = None
        with log_path.open("wb") as log:
            try:
                process = subprocess.Popen(args, cwd=main.parent, env=env, stdout=log, stderr=subprocess.STDOUT)
                owner = controller.OwnedProcessTree(process)
                deadline = time.monotonic() + 180
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                last_error = None
                while time.monotonic() < deadline:
                    owner.refresh()
                    owner.sample()
                    if process.poll() is not None:
                        raise RuntimeError("CPU schema server exited: " + str(process.returncode))
                    try:
                        with opener.open("http://127.0.0.1:8296/object_info", timeout=3) as response:
                            schema = json.load(response)
                        break
                    except (OSError, ValueError) as exc:
                        last_error = str(exc)
                        time.sleep(0.5)
                else:
                    raise RuntimeError("CPU schema startup deadline: " + str(last_error))
                root.write_text(run + "/object_info.json", json.dumps(schema, indent=2) + "\n")
                owned = {"CCTVH3ConditionAndRelease", "CCTVH3StagedUNETLoader", "CCTVH3DecodeVAELoader", "CCTVH3AudioDiagnostic"}
                native = {"CLIPLoader", "VAELoader", "UNETLoader", "MiniMaxH3ReferenceToVideo", "LoadVideo", "GetVideoComponents", "RandomNoise", "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio", "CreateVideo", "SaveVideo"}
                require(owned | native <= set(schema), "owned/native class absent from real object_info")
                expected_ports = {"CCTVH3ConditionAndRelease": {"text_encoder_name", "video_vae_name", "audio_vae_name", "prompt", "width", "height", "length", "ref_video"}, "CCTVH3StagedUNETLoader": {"release", "unet_name", "weight_dtype"}, "CCTVH3DecodeVAELoader": {"samples", "release", "video_vae_name", "audio_vae_name"}, "CCTVH3AudioDiagnostic": {"samples", "vae"}}
                names = {row["role"]: Path(row["model_relative_path"]).name for row in catalog["components"]}
                for cls, ports in expected_ports.items():
                    require(set(schema[cls]["input"]["required"]) == ports, "real owned ports differ: " + cls)
                    required = schema[cls]["input"]["required"]
                    for port, role in (("text_encoder_name", "text_encoder"), ("video_vae_name", "video_vae"), ("audio_vae_name", "audio_vae"), ("unet_name", "diffusion")):
                        if port in required:
                            require(required[port][0] == [names[role]], "catalog filename must be singleton choice: " + cls + "." + port)
                    if "weight_dtype" in required:
                        require(required["weight_dtype"][0] == ["default"], "weight dtype must have only default choice")
                    if "release" in required:
                        require(required["release"][0] == "CCTV_H3_RELEASE", "real release type differs")
                require(schema["CCTVH3ConditionAndRelease"]["output"] == ["CONDITIONING", "LATENT", "CCTV_H3_RELEASE"], "real conditioner output types differ")
                require(schema["CCTVH3StagedUNETLoader"]["output"] == ["MODEL"] and schema["CCTVH3DecodeVAELoader"]["output"] == ["LATENT", "VAE", "VAE"], "real staged output types differ")
                require(schema["CCTVH3AudioDiagnostic"]["output"] == ["AUDIO"], "diagnostic output differs")
                require(schema["CCTVH3AudioDiagnostic"]["input"]["required"]["samples"][0] == "LATENT" and schema["CCTVH3AudioDiagnostic"]["input"]["required"]["vae"][0] == "VAE", "diagnostic input types differ")
                result.update({"passed": True, "classes": sorted(owned | native), "object_info": run + "/object_info.json"})
            except Exception as exc:  # noqa: BLE001 - report arbitrary boundary/server failures and always clean up
                result.update({"passed": False, "error": type(exc).__name__ + ": " + str(exc)})
            finally:
                if owner is not None:
                    try:
                        result["cleanup"] = owner.stop()
                        result["final_identities"] = owner.snapshot()
                        require(result["cleanup"]["verified_stopped"] is True, "CPU schema cleanup unresolved")
                    except Exception as exc:  # noqa: BLE001 - report arbitrary boundary/server failures and always clean up
                        result.update({"passed": False, "cleanup_error": type(exc).__name__ + ": " + str(exc)})
                elif process is not None:
                    result.update({"passed": False, "cleanup_error": "Ownership capture failed; cannot claim safe cleanup"})
    root.write_with(run + "/server.log", execute)
    root.write_text(run + "/report.json", json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=REPORT)
    parser.add_argument("--schema-smoke", action="store_true", help="Also start actual isolated CPU model-free loopback schema server; never queues generation")
    args = parser.parse_args()
    root = ProjectRoot(ROOT)
    require(root.resolve(args.report).is_relative_to(root.resolve(BASE + "/_work/local_runs/_staging")), "report must be inside _staging")
    results = []
    for index, (name, test) in enumerate((("exact_native_delegation_release_and_deferred_decode", success), ("forged_foreign_failed_reused_token_rejection", bad_tokens), ("release_error_denied", release_failure), ("retained_reference_denied", retained_failure), ("native_error_releases_owned_models", native_failure), ("partial_loader_error_releases_prior_clip", partial_loader_failure))):
        boundary = Boundary()
        run = BASE + "/_work/local_runs/_staging/case_" + str(index)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, boundary.modules()))
            stack.enter_context(patch.dict(os.environ, {"CCTV_H3_RUN_REL": run}))
            try:
                module = load_extension(str(index))
                require(set(module.NODE_CLASS_MAPPINGS) == {"CCTVH3ConditionAndRelease", "CCTVH3StagedUNETLoader", "CCTVH3DecodeVAELoader", "CCTVH3AudioDiagnostic"}, "unexpected exported classes")
                require(all(cls.FUNCTION == "execute" for cls in module.NODE_CLASS_MAPPINGS.values()), "nonstandard entrypoint")
                test(boundary, module)
                results.append({"name": name, "passed": True, "calls": boundary.calls})
            except Exception as exc:  # noqa: BLE001 - report arbitrary boundary/server failures and always clean up
                results.append({"name": name, "passed": False, "error": type(exc).__name__ + ": " + str(exc), "calls": boundary.calls})
        boundary.retained.clear()
        gc.collect()
    report = {"schema_version": 1, "passed": all(case["passed"] for case in results), "checks": results, "limitations": ["Controlled native boundary objects only; no actual model, GPU, server, tensor processing, memory-savings or creator-quality claim.", "Actual isolated-runtime import/object_info and model weakref release must be verified independently.", "Failure evidence file contents and final process cleanup need actual-run review; CPU boundary exceptions alone do not prove persisted evidence."]}
    if args.schema_smoke:
        try:
            report["schema_smoke"] = schema_smoke(root)
        except Exception as exc:  # noqa: BLE001 - report arbitrary boundary/server failures and always clean up
            report["schema_smoke"] = {"passed": False, "error": type(exc).__name__ + ": " + str(exc)}
        report["passed"] = report["passed"] and report["schema_smoke"]["passed"]
    root.write_text(args.report, json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
