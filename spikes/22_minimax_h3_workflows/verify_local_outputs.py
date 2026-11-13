"""Independent CPU-only verification of actual local H3 artifacts.

No implementation imports, GPU work, downloads, or quality acceptance. --run and
--report are worktree-relative. Reports for held/failed/prepared attempts retain
findings but never count as a generated-media pass.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import re
import subprocess
import sys
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.domain.seeds import derive_seed
from orchestrator.platform.containment import ProjectRoot

BASE = "spikes/22_minimax_h3_workflows"
INPUT_MANIFEST = BASE + "/_work/test_inputs/20261109T165721Z_e1e1bf74/manifest.json"
INPUT_SHA = "a27d7f2a4d56f144f79c4efbb1740ff59a68e1610b9010f5dc764cb7aa3d0a2c"
MASTER_SHA = "9dbb789b260bfde899a549eaf18cfc20c4d8feed050f59edfeb2f25848f61389"


def command(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, timeout=360, check=True)
    # FFmpeg may recover damaged frames and still exit zero. Fail on decode diagnostics.
    if result.stderr and args[0] == "ffmpeg" and "-v" in args and args[args.index("-v") + 1] == "error":
        raise ValueError("FFmpeg emitted errors: " + result.stderr.decode(errors="replace")[-2000:])
    return result


def sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def probe(path: Path) -> dict:
    return json.loads(command(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)]).stdout)


def decode(path: Path) -> None:
    command(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"])


def pcm(path: Path, filters: str | None = None, codec: str = "pcm_f64le") -> bytes:
    args = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0"]
    if filters:
        args += ["-af", filters]
    formats = {"pcm_f64le": "f64le", "pcm_f32le": "f32le", "pcm_s24le": "s24le"}
    return command(args + ["-c:a", codec, "-f", formats[codec], "-"]).stdout


def metrics(path: Path) -> dict:
    values = array.array("d")
    values.frombytes(pcm(path))
    if sys.byteorder != "little":
        values.byteswap()
    finite = [v for v in values if math.isfinite(v)]
    count = len(values)
    # ebur128 only analyzes; it never applies gain or a loudness target.
    loudness = command(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"]).stderr.decode(errors="replace")
    summary = loudness.rsplit("Summary:", 1)[-1]
    integrated = re.search(r"I:\s*([-+\w.]+)\s*LUFS", summary)
    peak = re.search(r"Peak:\s*([-+\w.]+)\s*dBFS", summary)
    if not integrated or not peak:
        raise ValueError("FFmpeg ebur128 summary absent")
    return {
        "decoded_values": count, "nonfinite_samples": count - len(finite),
        "clipped_samples": sum(abs(v) >= 1.0 for v in finite),
        "sample_peak": max(map(abs, finite), default=0.0),
        "rms": math.sqrt(math.fsum(v * v for v in finite) / len(finite)) if finite else None,
        "dc": math.fsum(finite) / len(finite) if finite else None,
        "integrated_lufs": float(integrated[1]), "true_peak_dbfs": float(peak[1]),
    }


def packet_hash(path: Path) -> str:
    return command(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-"]).stdout.decode().strip()


def safe_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "-inf" if value < 0 else "+inf"
    if isinstance(value, dict):
        return {key: safe_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [safe_json(child) for child in value]
    return value


class Verifier:
    def __init__(self, root: ProjectRoot, run_relative: str):
        self.root = root
        self.run_relative = run_relative.rstrip("/")
        self.run = root.resolve(run_relative)
        if not self.run.is_relative_to(root.resolve(BASE + "/_work/local_runs")):
            raise ValueError("--run must be inside spike22 _work/local_runs")
        self.checks: list[dict] = []
        self.evidence: dict = {}

    @staticmethod
    def require(ok: bool, message: str) -> None:
        if not ok:
            raise ValueError(message)

    def check(self, name: str, operation) -> None:
        try:
            result = operation()
            self.checks.append({"name": name, "passed": True, "evidence": result})
        except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration, subprocess.SubprocessError) as exc:
            self.checks.append({"name": name, "passed": False, "error": type(exc).__name__ + ": " + str(exc)})

    def load(self, relative: str) -> dict:
        return json.loads(self.root.read_text(relative))

    def record(self, label: str, item: dict, base: ProjectRoot | None = None) -> Path:
        base = base or self.root
        path = base.resolve(item["path"])
        self.require("references" not in [p.lower() for p in path.relative_to(base.path).parts], label + ": prohibited reference media")
        actual = sha(path)
        self.require(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None, label + ": malformed SHA256")
        self.require(actual == item["sha256"], label + ": SHA256 mismatch")
        self.evidence[label] = {"path": item["path"], "sha256": actual, "bytes": path.stat().st_size}
        return path

    def video(self, label: str, item: dict, width: int, height: int, audio_count: int) -> Path:
        path = self.record(label, item)
        data = probe(path)
        video = [s for s in data["streams"] if s["codec_type"] == "video"]
        audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
        self.require(len(video) == 1 and len(audio) == audio_count, label + ": stream counts")
        stream = video[0]
        self.require((stream["width"], stream["height"]) == (width, height), label + ": geometry")
        self.require(int(stream["nb_read_frames"]) == 192, label + ": must decode all 192 frames")
        self.require(Fraction(stream["avg_frame_rate"]) == 24, label + ": must be 24 fps")
        self.require(Fraction(stream["time_base"]) > 0, label + ": invalid rational timebase")
        self.require(abs(float(stream.get("duration", data["format"]["duration"])) - 8) < 0.05, label + ": video must be eight seconds")
        decode(path)
        self.evidence[label]["probe"] = data
        return path

    def sources(self, generation: dict) -> dict:
        original = self.load(INPUT_MANIFEST)
        master = self.record("source_master", original["source"])
        self.require(sha(master) == MASTER_SHA, "canonical upstream master changed")
        canonical = self.video("canonical", original["input_video"], 1024, 768, 0)
        self.require(sha(canonical) == INPUT_SHA, "frozen canonical hash changed")
        self.record("prompt", original["prompt"])
        self.require(generation["shot_id"] == "s1" and generation["shot_version"] == 2, "shot identity mismatch")
        sources = generation["sources"]
        self.require(sources["canonical"]["sha256"] == INPUT_SHA, "generation source differs from frozen canonical")
        self.record("run_canonical", sources["canonical"])
        self.video("proxy", sources["proxy"], *self.canvas(generation), 0)
        self.require(generation["transforms"].get("canonical_to_proxy"), "proxy resize transform absent")
        return {"canonical": str(canonical.relative_to(ROOT)), "master": str(master.relative_to(ROOT))}

    def models(self, generation: dict) -> dict:
        catalog = self.load(BASE + "/LOCAL_MODEL_CATALOG.json")
        receipt_item = generation["acquisition_receipt"]
        receipt = self.load(str(self.record("acquisition_receipt", receipt_item).relative_to(ROOT)))
        entries = receipt["components"]
        by_role = {item["role"]: item for item in entries}
        self.require(len(entries) == len(by_role) == 4, "need exactly four model receipt roles")
        observed = []
        for expected in catalog["components"]:
            item = by_role[expected["role"]]
            for key in ("repo", "revision"):
                self.require(item[key] == expected[key], "model receipt differs from catalog: " + key)
            declared = item.get("expected", {})
            self.require(item.get("expected_bytes", declared.get("bytes")) == expected["expected_bytes"], "receipt expected model size differs")
            self.require(item.get("expected_sha256", declared.get("sha256")) == expected["expected_sha256"], "receipt expected model hash differs")
            actual = item.get("actual", {})
            actual_path = actual.get("path", item.get("path", catalog["model_root"] + "/" + expected["model_relative_path"]))
            path = self.root.resolve(actual_path)
            self.require(path == self.root.resolve(catalog["model_root"] + "/" + expected["model_relative_path"]), "model outside catalog path")
            size, digest = path.stat().st_size, sha(path)
            self.require(size == expected["expected_bytes"] and digest == expected["expected_sha256"], "full model hash/size differs: " + expected["role"])
            self.require(actual.get("sha256", item.get("observed_sha256")) == digest and actual.get("bytes", item.get("observed_bytes")) == size, "receipt observed model identity differs")
            self.require(expected["format"] == "safetensors" and path.suffix == ".safetensors", "non-safetensors model")
            observed.append({"role": expected["role"], "path": actual_path, "bytes": size, "sha256": digest})
        self.require(sum(row["bytes"] for row in observed) == catalog["total_file_bytes"], "catalog total mismatch")
        self.evidence["full_model_hashes"] = observed
        return {"verified_bytes": sum(row["bytes"] for row in observed), "full_sha256_recomputed": True}

    def canvas(self, generation: dict) -> tuple[int, int]:
        experiment = generation.get("experiment")
        if experiment is None:
            return 640, 480
        self.require(experiment in ({"strategy": "staged_native", "width": 384, "height": 288, "length": 192}, {"strategy": "staged_native_audio_diagnostic", "width": 384, "height": 288, "length": 192}), "unknown experiment/canvas")
        return 384, 288

    def staged_graph(self, generation: dict) -> dict:
        self.require(self.canvas(generation) == (384, 288), "staged canvas absent")
        graph = json.loads(self.record("graph", generation["graph"]).read_text(encoding="utf-8"))
        graph = graph.get("prompt", graph)
        schema = json.loads(self.record("object_info", generation["object_info"]).read_text(encoding="utf-8"))
        classes = {n["class_type"] for n in graph.values()}
        owned = {"CCTVH3ConditionAndRelease", "CCTVH3StagedUNETLoader", "CCTVH3DecodeVAELoader"}
        self.require(owned <= classes, "missing staged nodes")
        self.require(not classes.intersection({"UNETLoader", "CLIPLoader", "VAELoader", "MiniMaxH3ReferenceToVideo"}), "standalone conditioning/model loader bypass")
        diagnostic = generation["experiment"]["strategy"] == "staged_native_audio_diagnostic"
        audio_class = "CCTVH3AudioDiagnostic" if diagnostic else "VAEDecodeAudio"
        if diagnostic:
            owned.add(audio_class)
        allowed = owned | {"LoadVideo", "GetVideoComponents", "RandomNoise", "BasicGuider", "BasicScheduler", "KSamplerSelect", "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio", "CreateVideo", "SaveVideo"}
        if diagnostic:
            allowed.discard("VAEDecodeAudio")
        self.require(classes <= allowed, "unexpected staged graph classes: " + str(classes - allowed))
        def only(cls):
            rows = [(key, n["inputs"]) for key, n in graph.items() if n["class_type"] == cls]
            self.require(len(rows) == 1, "expected exactly one " + cls)
            return rows[0]
        def link(value, cls, slot):
            key, ports = only(cls)
            self.require(value == [key, slot], "incorrect " + cls + " dependency/output")
            return ports
        cid, cond = only("CCTVH3ConditionAndRelease")
        uid, unet = only("CCTVH3StagedUNETLoader")
        did, decode_loader = only("CCTVH3DecodeVAELoader")
        sid, sampler = only("SamplerCustomAdvanced")
        self.require((cond["width"], cond["height"], cond["length"]) == (384, 288, 192), "conditioner canvas differs")
        self.require(set(cond) == {"text_encoder_name", "video_vae_name", "audio_vae_name", "prompt", "width", "height", "length", "ref_video"}, "unexpected conditioning input/audio route")
        self.require(cond["prompt"] == self.root.read_text(self.load(INPUT_MANIFEST)["prompt"]["path"]), "prompt changed")
        comp = link(cond["ref_video"], "GetVideoComponents", 0)
        load = link(comp["video"], "LoadVideo", 0)
        self.require(Path(load["file"]).name == Path(generation["sources"]["proxy"]["path"]).name, "proxy reference disconnected")
        def reject_audio(value):
            if isinstance(value, list):
                self.require(value != [only("GetVideoComponents")[0], 1], "source audio connected")
                for child in value:
                    reject_audio(child)
            elif isinstance(value, dict):
                for child in value.values():
                    reject_audio(child)
        reject_audio(graph)
        self.require(unet["release"] == decode_loader["release"] == [cid, 2], "release token dependency differs")
        self.require(unet["weight_dtype"] == "default", "UNET dtype override")
        self.require(decode_loader["samples"] == [sid, 0], "decode loaders may run before sampling")
        self.require(sampler["latent_image"] == [cid, 1], "native latent disconnected")
        guider = link(sampler["guider"], "BasicGuider", 0)
        self.require(guider["conditioning"] == [cid, 0] and guider["model"] == [uid, 0], "guider conditioning/model disconnected")
        schedule = link(sampler["sigmas"], "BasicScheduler", 0)
        self.require(schedule["model"] == [uid, 0] and schedule["steps"] == 20 and schedule["scheduler"] == "simple" and schedule["denoise"] == 1, "schedule differs")
        self.require(link(sampler["sampler"], "KSamplerSelect", 0)["sampler_name"] == "res_multistep", "sampler differs")
        saved = only("SaveVideo")[1]
        created = link(saved["video"], "CreateVideo", 0)
        self.require(created["fps"] == 24, "output fps differs")
        vd = link(created["images"], "VAEDecode", 0)
        ad = link(created["audio"], audio_class, 0)
        self.require(vd["samples"] == ad["samples"] == [did, 0], "joint sampled latent differs")
        self.require(vd["vae"] == [did, 1] and ad["vae"] == [did, 2], "deferred joint VAEs disconnected")
        catalog = self.load(BASE + "/LOCAL_MODEL_CATALOG.json")
        names = {row["role"]: Path(row["model_relative_path"]).name for row in catalog["components"]}
        self.require(cond["text_encoder_name"] == names["text_encoder"] and cond["video_vae_name"] == decode_loader["video_vae_name"] == names["video_vae"] and cond["audio_vae_name"] == decode_loader["audio_vae_name"] == names["audio_vae"] and unet["unet_name"] == names["diffusion"], "staged model filenames differ")
        for cls in classes:
            self.require(cls in schema, "missing actual schema " + cls)
        for cls in owned:
            ports = schema[cls]["input"]["required"]
            for port, value in only(cls)[1].items():
                self.require(port in ports, "owned port absent from actual schema")
                choices = ports[port][0]
                if isinstance(choices, list):
                    self.require(choices == [value], "owned filename/default choices differ from singleton contract")
        self.require(schema["CCTVH3StagedUNETLoader"]["input"]["required"]["release"][0] == "CCTV_H3_RELEASE", "release schema differs")
        noise = link(sampler["noise"], "RandomNoise", 0)
        seed = generation["seed"]
        maximum = schema["RandomNoise"]["input"]["required"]["noise_seed"][1]["max"]
        self.require(seed["shot_id"] == "s1" and seed["degrade_seed"] == 0 and seed["stage"] == "minimax_h3_ambience" and seed["max_value"] == maximum, "seed inputs differ")
        self.require(noise["noise_seed"] == seed["value"] == derive_seed("s1", 0, "minimax_h3_ambience", max_value=maximum), "seed differs")
        runtime = generation["runtime"]
        source_receipt = json.loads(self.record("runtime_source_receipt", runtime["source_receipt"]).read_text(encoding="utf-8"))
        self.require(source_receipt["commit"] == "2c75dc1b379a59e0880619d029607af5169a62ef", "native runtime commit differs")
        source_records = {row["path"]: row for row in source_receipt["files"]}
        for index, row in enumerate(runtime["graph_evidence"]["files"]):
            self.record("native_graph_source_" + str(index), row)
            if row["path"] in source_records:
                self.require(row["sha256"] == source_records[row["path"]]["sha256"], "native graph source differs from pinned receipt")
        for name in ("source", "freeze"):
            self.record("runtime_" + name, runtime[name])
        evidence = runtime["graph_evidence"]
        self.require(evidence["frame_count"] == 192 and evidence["source_audio_inputs"] == [] and evidence["generated_audio_joint"] is True, "full native reference evidence differs")
        self.record("extension", generation["runtime"]["extension"])
        self.record("extra_paths_config", generation["runtime"]["extra_paths_config"])
        return {"classes": sorted(classes), "reference_frames_available": 192, "reference_audio_items": 0, "deferred_decode_dependency_verified": True, "limitation": "Connectivity does not prove native tensor execution; source review and stage receipts required."}

    def stage_evidence(self, generation: dict) -> dict:
        events_path = self.record("stage_events", generation["stage_events"])
        receipt_path = self.record("stage_release", generation["stage_release"])
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        phases = ["condition_start", "post_condition_pre_release", "post_release", "pre_diffusion_load", "post_diffusion_load", "post_sampling"]
        self.require([e["phase"] for e in events] == phases, "stage event sequence incomplete/reordered/duplicated")
        identity = [(e["invocation_id"], e["pid"], e["create_time"]) for e in events]
        self.require(len(set(identity)) == 1, "stage invocation/process identity differs")
        for event in events:
            self.require(event["schema_version"] == 1 and (event["width"], event["height"], event["length"]) == (384, 288, 192), "stage schema/canvas differs")
            self.require(bool(event["utc"]), "stage UTC absent")
            measures = [event["ram_available_bytes"], event["cuda_allocated_bytes"], event["cuda_reserved_bytes"], *[event["process_memory_bytes"][k] for k in ("rss", "private", "vms")]]
            self.require(all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in measures), "invalid/missing resource measurement")
            self.require(isinstance(event["loaded_models"], list), "loaded-model observation absent")
        self.require(all(a["monotonic"] <= b["monotonic"] for a, b in pairwise(events)), "stage monotonic ordering differs")
        self.require(events[1]["reference_shape"] == [192, 288, 384, 3], "actual full reference tensor shape differs")
        self.require(events[1]["video_reference_latent_shapes"] == [[1, 24, 57, 18, 24]], "actual returned native video reference latent shape differs")
        release = events[2]
        self.require(release["all_model_refs_released"] is True and bool(release["weakrefs"]) and all(w["released"] is True and w["role"] for w in release["weakrefs"]), "retained model references")
        self.require(json.loads(receipt_path.read_text(encoding="utf-8")) == release, "immutable release receipt differs from event")
        cleanup = generation["runtime"]["owned_process_cleanup"]
        self.require(cleanup["verified_stopped"] is True and cleanup["survivors"] == [] and cleanup["unverifiable_identities"] == [] and cleanup["failures"] == [], "final owned-process cleanup unresolved")
        self.require(generation["runtime"]["watchdog_thread_stopped"] is True, "watchdog still running")
        before, after = events[1], events[2]
        deltas = {k: after["process_memory_bytes"][k] - before["process_memory_bytes"][k] for k in ("rss", "private", "vms")}
        for key in ("ram_available_bytes", "cuda_allocated_bytes", "cuda_reserved_bytes"):
            deltas[key] = after[key] - before[key]
        return {"signed_post_release_minus_pre_release": deltas, "memory_savings_claimed": False, "identity": identity[0], "limitation": "Device memory observations are process allocator counters; watchdog whole-device observations require separate review."}

    def graph(self, generation: dict) -> dict:
        if generation.get("experiment") is not None:
            return self.staged_graph(generation)
        graph_path = self.record("graph", generation["graph"])
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph = graph.get("prompt", graph)
        self.require(isinstance(graph, dict) and bool(graph), "API graph absent")
        classes = {node["class_type"] for node in graph.values()}
        required = {"UNETLoader", "CLIPLoader", "VAELoader", "LoadVideo", "GetVideoComponents", "MiniMaxH3ReferenceToVideo", "RandomNoise", "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio", "CreateVideo", "SaveVideo"}
        self.require(required <= classes, "missing required native graph classes: " + str(required - classes))
        forbidden = ("DigitalHuman", "AudioEncoder", "LoadAudio", "Lora", "GGUF", "Upscale", "Optimizer", "Lock")
        self.require(not any(any(word.lower() in cls.lower() for word in forbidden) for cls in classes), "forbidden graph route")

        def node(link: Any, cls: str | None = None, output: int | None = None) -> dict:
            self.require(isinstance(link, list) and len(link) == 2 and str(link[0]) in graph, "invalid graph link")
            value = graph[str(link[0])]
            if cls:
                self.require(value["class_type"] == cls, "graph link does not target " + cls)
            if output is not None:
                self.require(link[1] == output, "incorrect output slot")
            return value["inputs"]

        r2vs = [n for n in graph.values() if n["class_type"] == "MiniMaxH3ReferenceToVideo"]
        self.require(len(r2vs) == 1, "must have one R2V conditioner")
        inputs = r2vs[0]["inputs"]
        self.require((inputs["width"], inputs["height"], inputs["length"]) == (640, 480, 192), "R2V canvas/frame count differs")
        self.require((inputs["length"] - 5) % 17 == 0, "invalid frame grid")
        self.require(not inputs.get("ref_audios") and not inputs.get("ref_video_audios") and not inputs.get("ref_images"), "audio/image reference supplied")
        self.require(not any(key.startswith("ref_images.") for key in inputs), "flattened reference image supplied")
        self.require(not any(re.match(r"(?:ref_video_audios\.|ref_audios\.)?ref_(?:video_audio|audio)_\d+", key) for key in inputs), "flattened reference audio supplied")
        refs = inputs.get("ref_videos", {})
        refs = refs or {k: v for k, v in inputs.items() if re.fullmatch(r"ref_videos\.ref_video_\d+", k)}
        self.require(len(refs) == 1, "must supply one full video reference")
        components = node(next(iter(refs.values())), "GetVideoComponents", 0)
        load = node(components["video"], "LoadVideo", 0)
        self.require(Path(load["file"]).name == Path(generation["sources"]["proxy"]["path"]).name, "loaded video does not name proxy")
        vae = node(inputs["vae"], "VAELoader", 0)
        self.require("video_vae_int8_convrot" in vae["vae_name"], "video latent reference VAE absent")
        self.require("audio_vae_fp32" in node(inputs["audio_vae"], "VAELoader", 0)["vae_name"], "joint audio VAE absent")
        self.require(node(inputs["clip"], "CLIPLoader", 0)["type"] == "minimax", "incorrect native CLIP type")
        expected_prompt = self.root.read_text(self.load(INPUT_MANIFEST)["prompt"]["path"])
        self.require(inputs["prompt"] == expected_prompt, "prompt changed")
        # Follow the saved output backwards; disconnected good-looking nodes do not count.
        saves = [n["inputs"] for n in graph.values() if n["class_type"] == "SaveVideo"]
        self.require(len(saves) == 1, "expected one saved generated AV")
        created = node(saves[0]["video"], "CreateVideo", 0)
        self.require(created["fps"] == 24, "generated CreateVideo rate differs")
        video_decode = node(created["images"], "VAEDecode", 0)
        audio_decode = node(created["audio"], "VAEDecodeAudio", 0)
        self.require(video_decode["vae"] == inputs["vae"] and audio_decode["vae"] == inputs["audio_vae"], "joint decoders use wrong VAEs")
        self.require(video_decode["samples"] == audio_decode["samples"], "audio/video must decode the same joint sampler output")
        sampler = node(video_decode["samples"], "SamplerCustomAdvanced")
        r2v_id = next(key for key, val in graph.items() if val is r2vs[0])
        self.require(sampler["latent_image"] == [r2v_id, 1], "sampler latent does not come from R2V")
        guider = node(sampler["guider"])
        self.require(guider.get("conditioning", guider.get("positive")) == [r2v_id, 0], "guider is disconnected from R2V conditioning")
        node(sampler["noise"], "RandomNoise", 0)
        self.require(node(sampler["sampler"], "KSamplerSelect", 0)["sampler_name"] == "res_multistep", "sampler is not official res_multistep")
        schedule = node(sampler["sigmas"])
        self.require(schedule["steps"] == 20, "sampler schedule does not use 20 steps")
        self.require(schedule["scheduler"] == "simple" and schedule["denoise"] == 1, "sampler schedule must be simple with denoise 1")
        # The source soundtrack output is forbidden even if the source currently has none.
        components_id = str(next(iter(refs.values()))[0])
        def reject_source_audio(value: Any) -> None:
            if isinstance(value, list):
                self.require(value != [components_id, 1], "GetVideoComponents source audio is connected")
                for child in value:
                    reject_source_audio(child)
            elif isinstance(value, dict):
                for child in value.values():
                    reject_source_audio(child)
        reject_source_audio(graph)
        noise = next(n["inputs"] for n in graph.values() if n["class_type"] == "RandomNoise")
        seed = generation["seed"]
        self.require(seed["shot_id"] == "s1" and seed["degrade_seed"] == 0 and seed["stage"] == "minimax_h3_ambience", "seed inputs changed")
        schema = json.loads(self.record("object_info", generation["object_info"]).read_text(encoding="utf-8"))
        maximum = schema["RandomNoise"]["input"]["required"]["noise_seed"][1]["max"]
        self.require(seed["max_value"] == maximum, "seed maximum differs from actual schema")
        self.require(noise["noise_seed"] == seed["value"] == derive_seed("s1", 0, "minimax_h3_ambience", max_value=maximum), "seed derivation mismatch")
        for graph_node in graph.values():
            self.require(graph_node["class_type"] in schema, "graph node absent from recorded schema")
            if graph_node["class_type"] in {"UNETLoader", "CLIPLoader", "VAELoader"}:
                definitions = schema[graph_node["class_type"]]["input"]
                ports = definitions.get("required", {}) | definitions.get("optional", {})
                for port, value in graph_node["inputs"].items():
                    self.require(port in ports, "loader input absent from actual schema: " + port)
                    choices = ports[port][0]
                    if isinstance(choices, list):
                        self.require(value in choices, "loader value absent from actual model choices")
        expected_names = {Path(row["model_relative_path"]).name for row in self.load(BASE + "/LOCAL_MODEL_CATALOG.json")["components"]}
        actual_names = set()
        for graph_node in graph.values():
            if graph_node["class_type"] in {"UNETLoader", "CLIPLoader", "VAELoader"}:
                for port in ("unet_name", "clip_name", "vae_name"):
                    if port in graph_node["inputs"]:
                        actual_names.add(Path(graph_node["inputs"][port]).name)
                if graph_node["class_type"] == "UNETLoader":
                    self.require(graph_node["inputs"]["weight_dtype"] == "default", "native UNET dtype override")
        self.require(actual_names == expected_names, "graph models differ from four-component catalog")
        for graph_node in graph.values():
            for key, val in graph_node["inputs"].items():
                self.require(key not in {"audio", "source_audio"} or graph_node["class_type"] == "CreateVideo", "unexpected reference/locked audio input")
                self.require(key != "negative_prompt", "invented negative prompt")
                if key == "sampler_name":
                    self.require(val == "res_multistep", "sampler differs from official res_multistep")
                if key == "steps":
                    self.require(val == 20, "step count differs")
        return {"classes": sorted(classes), "reference_frames_available": 192, "reference_audio_items": 0, "video_latent_reference_connected": True, "native_audio_decoder": {"class": "VAEDecodeAudio", "stock_processing": "waveform divided by max(1, 5 * waveform standard deviation) before AV encoding", "post_export_gain_db": 0, "attenuation_factor_measured": False}, "limitation": "Graph/schema verify declared connectivity; no tensor execution instrumentation."}

    def wav(self, label: str, item: dict, edit: bool = False) -> Path:
        path = self.record(label, item)
        data = probe(path)
        self.require(len(data["streams"]) == 1, label + ": single audio stream required")
        audio = data["streams"][0]
        expected_codec = "pcm_s24le" if edit else "pcm_f32le"
        self.require(audio["codec_type"] == "audio" and audio["codec_name"] == expected_codec, label + ": wrong PCM codec")
        measured = metrics(path)
        rate, channels = int(audio["sample_rate"]), int(audio["channels"])
        self.require(measured["decoded_values"] % channels == 0 and measured["nonfinite_samples"] == 0, label + ": invalid samples")
        count = measured["decoded_values"] // channels
        for key, actual in (("sample_rate", rate), ("channels", channels), ("sample_count", count), ("start_sample", 0)):
            self.require(item[key] == actual, label + ": recorded " + key + " differs")
        if edit:
            self.require(rate == 48000 and count == 384000, "edit WAV requires 48kHz/eight seconds")
        for key, actual in measured.items():
            aliases = {"rms": "rms_amplitude", "dc": "dc_offset"}
            recorded = item["metrics"]
            metric_key = key if key in recorded else aliases.get(key, key)
            self.require(metric_key in recorded, label + ": unrecorded metric " + key)
            claimed = float(recorded[metric_key]) if recorded[metric_key] is not None else None
            tolerance = 0.15 if key in ("integrated_lufs", "true_peak_dbfs") else 1e-7
            self.require(claimed == actual or (claimed is not None and actual is not None and math.isfinite(claimed) and math.isfinite(actual) and abs(claimed - actual) <= tolerance), label + ": measured metric differs: " + key)
        self.evidence[label].update({"probe": data, "metrics": measured, "sample_count": count})
        return path

    def media(self, generation: dict, editor: dict) -> dict:
        media = editor.get("media", generation["media"])
        av = self.video("generated_av", media["generated_av"], *self.canvas(generation), 1)
        native = self.wav("native_wav", media["native_wav"])
        edit = self.wav("edit_wav", media["edit_wav"], True)
        self.require(len({av, native, edit}) == 3, "native and edit artifacts must be separate")
        self.require(pcm(av, codec="pcm_f32le") == pcm(native, codec="pcm_f32le"), "native extracted PCM differs from decoded generated soundtrack")
        # Stock VAEDecodeAudio attenuation is inside the native model output.
        # Equality above proves extraction adds no post-export transform or gain.
        filters = "aresample=48000,apad=whole_len=384000,atrim=end_sample=384000"
        self.require(pcm(native, filters, "pcm_s24le") == pcm(edit, codec="pcm_s24le"), "edit PCM differs from explicit no-gain 48kHz/pad/trim transform")
        self.require("extracted/decoded" in media["native_wav"].get("identity_label", ""), "native decoded-audio identity absent")
        canonical = self.load(INPUT_MANIFEST)["input_video"]
        transform = generation["transforms"]["native_to_edit"]
        self.require(transform["resample_to_hz"] == 48000 and transform["format"] == "pcm_s24le" and transform["gain_db"] == 0, "recorded editor transform differs")
        self.require("trim_samples" in transform and "pad_samples" in transform, "sample fitting counts absent")
        native_count = media["native_wav"]["sample_count"]
        native_rate = media["native_wav"]["sample_rate"]
        self.require(native_count > 0 and native_rate > 0, "empty native soundtrack")
        resampled_frames = len(pcm(native, "aresample=48000")) // (8 * media["native_wav"]["channels"])
        self.require(transform["trim_samples"] == max(0, resampled_frames - 384000) and transform["pad_samples"] == max(0, 384000 - resampled_frames), "recorded trim/pad sample counts differ from actual resampling")
        self.evidence["edit_wav"]["actual_resampled_frames_before_fitting"] = resampled_frames
        audition = self.video("audition", media["audition"], 1024, 768, 1)
        original_packets = packet_hash(self.root.resolve(canonical["path"]))
        audition_metrics = metrics(audition)
        self.require(audition_metrics["nonfinite_samples"] == 0, "audition nonfinite PCM")
        self.evidence["audition"]["metrics"] = audition_metrics
        audition_packets = packet_hash(audition)
        self.require(original_packets == audition_packets, "audition canonical compressed video packets changed")
        self.require(editor["shot_id"] == "s1" and editor["shot_version"] == 2, "editor shot identity differs")
        self.require(editor["start_sample"] == 0 and editor["source_offset_seconds"] == 0, "editor timeline offsets differ")
        self.require(Fraction(editor["fps"]) == 24 and Fraction(editor["time_base"]) == Fraction(1, 24), "editor rational timing absent")
        self.require(editor["candidate_status"] == "new_candidate_unapproved" and editor["provenance"]["creator_approval"] == "pending", "candidate must remain unapproved")
        self.require(editor["media_path_base"] == "worktree_root", "editor path base differs")
        contact = self.run_relative + "/independent_contact_sheet.png"
        frame_filter = "select='eq(n,0)+eq(n,48)+eq(n,96)+eq(n,144)+eq(n,191)',scale=320:240,tile=5x1"
        self.root.write_with(contact, lambda output: command(["ffmpeg", "-v", "error", "-i", str(av), "-vf", frame_filter, "-frames:v", "1", "-y", str(output)]))
        return {"native_decode_equal": True, "edit_no_gain_pcm_equal": True, "video_packet_sha256": original_packets, "contact_sheet": {"path": contact, "sha256": sha(self.root.resolve(contact)), "frame_indices": [0, 48, 96, 144, 191]}}

    def provenance(self, generation: dict) -> dict:
        receipt = self.load(generation["acquisition_receipt"]["path"])
        expected_docs = self.load(BASE + "/LOCAL_MODEL_DOCUMENTATION.json")["documents"]
        records: list[dict] = []
        def collect(value: Any) -> None:
            if isinstance(value, dict):
                if "role" in value and ("actual" in value or "path" in value):
                    records.append(value)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(receipt)
        observed = []
        for expected in expected_docs:
            matches = [row for row in records if row["role"] == expected["role"]]
            self.require(len(matches) == 1, "missing or duplicate pinned document " + expected["role"])
            row = matches[0]
            self.require(row["repo"] == expected["repo"] and row["revision"] == expected["revision"], "document repository/revision differs")
            actual = row.get("actual", row)
            path = self.record("document_" + expected["role"], actual)
            self.require(path.stat().st_size == expected["expected_bytes"] and sha(path) == expected["expected_sha256"], "document hash/size differs from pinned catalog")
            blob = path.read_bytes()
            blob_sha = hashlib.sha1(b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest()
            self.require(blob_sha == expected["git_blob_sha1"], "document git blob identity differs")
            observed.append({"role": expected["role"], "sha256": sha(path), "git_blob_sha1": blob_sha})
        self.require(generation.get("provenance"), "model/source provenance absent")
        self.credits(generation["provenance"]["current_source_notices"])
        self.require(generation["provenance"]["source_manifest"] == self.load(INPUT_MANIFEST), "canonical source manifest differs")
        self.record("generation_prompt", generation["prompt"])
        self.require(generation["prompt"]["sha256"] == self.load(INPUT_MANIFEST)["prompt"]["sha256"], "generation prompt identity differs")
        self.require(generation.get("runtime"), "runtime identity absent")
        return {"hashed_documents": observed}

    def credits(self, credits: dict) -> None:
        original = self.load(INPUT_MANIFEST)
        self.require(credits["rights_status"] == original["source"]["upstream_rights_status"], "source rights status differs")
        self.require(credits["local_internal_test_only"] is True and credits["creator_approval"] == "pending", "source rights scope differs")
        snapshots = credits["current_source_snapshots"]
        metadata = credits["source_metadata"]
        self.require(bool(snapshots) and len(snapshots) == len(metadata), "source credit snapshots absent/incomplete")
        source_project = original["source"]["path"].split("/renders/", 1)[0]
        required = {source_project + "/project.json", source_project + "/episodes/ep1/shots/s1/v0002.json"}
        asset_root = self.root.resolve(source_project + "/assets")
        if asset_root.is_dir():
            required.update(path.relative_to(ROOT).as_posix() for path in asset_root.rglob("*.json"))
        self.require(required == {row["path"] for row in snapshots} and len(required) == len(snapshots), "source project/shot/asset rights evidence absent or duplicated")
        for index, row in enumerate(snapshots):
            path = self.record("credit_snapshot_" + str(index), row)
            self.require(metadata[row["path"]] == json.loads(path.read_text(encoding="utf-8")), "credited source metadata differs")

    def bundle(self, editor: dict) -> dict:
        bundle_path = self.run / "editor_bundle"
        bundle = ProjectRoot(bundle_path)
        manifest = json.loads(bundle.read_text("project.json"))
        self.require(manifest["media_path_base"] == "bundle_root", "bundle base must be bundle_root")
        count = 0
        def walk(value: Any) -> None:
            nonlocal count
            if isinstance(value, dict):
                if "path" in value:
                    self.require("sha256" in value, "bundle path without SHA256")
                    self.record("bundle_" + str(count), value, bundle)
                    count += 1
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(manifest)
        files = manifest["files"]
        expected_records = {"original_video": editor["sources"]["canonical"], "model_proxy": editor["sources"]["proxy"], **editor["media"], "graph": editor["graph"], "prompt": editor["prompt"]}
        for role, expected in expected_records.items():
            self.require(role in files, "bundle missing " + role)
            self.require(files[role]["sha256"] == expected["sha256"], "bundle identity differs: " + role)
        self.require(manifest["provenance"]["source_credits"] == files["credits"]["path"], "bundle credits pointer differs")
        credits = json.loads(bundle.read_text(files["credits"]["path"]))
        self.credits(credits)
        self.require(credits == editor["provenance"]["current_source_notices"], "bundle credits differ from generation evidence")
        self.require(manifest["provenance"]["model_runtime_required_to_open"] is False and manifest["provenance"]["creator_approval"] == "pending", "bundle runtime/approval metadata differs")
        self.require(manifest["source_offset_seconds"] == 0 and manifest["start_sample"] == 0 and Fraction(manifest["fps"]) == 24 and Fraction(manifest["time_base"]) == Fraction(1, 24), "bundle timeline differs")
        self.require(manifest["status"] == "new_candidate_unapproved", "bundle candidate approval differs")
        self.require(manifest["media_metadata"] == {key: {k: v for k, v in value.items() if k not in ("path", "decoded_analysis")} for key, value in editor["media"].items()}, "bundle media metadata differs")
        receipt = self.load(editor["acquisition_receipt"]["path"])
        for index, doc in enumerate(receipt["documentation"]):
            self.require(files["model_notice_" + str(index)]["sha256"] == doc["actual"]["sha256"], "bundle model notice differs")
        for entry in bundle_path.rglob("*"):
            relative = entry.relative_to(bundle_path).as_posix()
            bundle.resolve(relative)
            self.require("references" not in [p.lower() for p in entry.parts], "bundle contains reference media")
            self.require(entry.suffix.lower() not in {".safetensors", ".gguf", ".ckpt", ".pt", ".pth"} or relative == "diagnostic/tensors.safetensors", "bundle contains model weights")
        return {"contained_hashed_records": count, "base": "bundle_root"}

    def audio_diagnostic(self, generation: dict) -> dict:
        import importlib.util
        spec = importlib.util.spec_from_file_location("independent_audio_verifier", ROOT / BASE / "verify_audio_diagnostic.py")
        verify_audio_diagnostic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verify_audio_diagnostic)
        self.record("audio_diagnostic", generation["audio_diagnostic"])
        result = verify_audio_diagnostic.verify_run(self.root, self.run_relative)
        self.require(result["passed"], "independent audio diagnostic failed")
        return result

    def verify(self) -> dict:
        generation = self.load(self.run_relative + "/generation.json")
        status = generation.get("status", "unknown")
        self.check("frozen_sources_and_proxy", lambda: self.sources(generation))
        self.check("full_model_catalog_hashes", lambda: self.models(generation))
        self.check("native_video_reference_graph", lambda: self.graph(generation))
        self.check("pinned_provenance", lambda: self.provenance(generation))
        if generation.get("experiment") is not None and generation.get("stage_events"):
            self.check("staged_execution_evidence", lambda: self.stage_evidence(generation))
        elif generation.get("experiment") is not None and status == "generated_candidate_unapproved":
            self.checks.append({"name": "staged_execution_evidence", "passed": False, "error": "Successful staged run has no stage evidence"})
        if generation.get("experiment", {}).get("strategy") == "staged_native_audio_diagnostic":
            self.check("independent_audio_diagnostic", lambda: self.audio_diagnostic(generation))
        successful = status == "generated_candidate_unapproved"
        if successful:
            try:
                editor = self.load(self.run_relative + "/editor_manifest.json")
            except (OSError, ValueError) as exc:
                self.checks.append({"name": "editor_manifest", "passed": False, "error": str(exc)})
            else:
                self.check("actual_media_and_no_gain_transforms", lambda: self.media(generation, editor))
                self.check("standalone_editor_bundle", lambda: self.bundle(editor))
        else:
            self.checks.append({"name": "actual_generated_media", "passed": False, "error": "Not verified: attempt status is " + str(status)})
        self.check("frozen_source_after_verification", lambda: self.require(sha(self.root.resolve(self.load(INPUT_MANIFEST)["input_video"]["path"])) == INPUT_SHA, "canonical changed during verification"))
        passed = successful and all(row["passed"] for row in self.checks)
        return {"schema_version": 1, "run": self.run_relative, "attempt_status": status, "structural_media_passed": passed, "generated_media_verified": passed, "quality_accepted": False, "creator_approval": "pending", "checks": self.checks, "evidence": self.evidence, "limitations": ["No listening tool used; sound quality, timing, realism, semantic match and creator approval remain pending.", ("Diagnostic tensor receipt retains raw decoded waveform and measured native divisor; native WAV remains decoded from codec output." if generation.get("experiment", {}).get("strategy") == "staged_native_audio_diagnostic" else "Native WAV is decoded from generated AV after stock VAEDecodeAudio attenuation and codec encoding; raw model waveform and actual attenuation factor are not recovered."), "Recorded graph/provenance cannot independently authenticate a remote origin or prove tensor execution contents.", "RAM/VRAM observations, process cleanup, offline enforcement and runtime safety require separate supervision review.", "No repeatability claim without a measured second run; no invented loudness acceptance target."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="worktree-relative attempt directory under spike22 _work/local_runs")
    parser.add_argument("--report", required=True, help="worktree-relative JSON report path under spike22 _work/local_runs")
    args = parser.parse_args()
    root = ProjectRoot(ROOT)
    try:
        report = root.resolve(args.report)
        if not report.is_relative_to(root.resolve(BASE + "/_work/local_runs")):
            raise ValueError("--report must be under spike22 _work/local_runs")
        result = Verifier(root, args.run).verify()
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        result = {"schema_version": 1, "run": args.run, "structural_media_passed": False, "generated_media_verified": False, "quality_accepted": False, "creator_approval": "pending", "checks": [{"name": "artifact_loading", "passed": False, "error": type(exc).__name__ + ": " + str(exc)}], "limitations": ["Verification aborted; no quality or successful generation claim."]}
    output = json.dumps(safe_json(result), indent=2, allow_nan=False) + "\n"
    # Validate report independently of the earlier exception handler: invalid destinations never write.
    report = root.resolve(args.report)
    if not report.is_relative_to(root.resolve(BASE + "/_work/local_runs")):
        parser.error("--report must be under spike22 _work/local_runs")
    root.write_text(args.report, output)
    print(output)
    return 0 if result["structural_media_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
