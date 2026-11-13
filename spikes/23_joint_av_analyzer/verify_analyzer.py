"""Independent contract verifier; no network, installs, or model loading."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

BASE = "spikes/23_joint_av_analyzer/_work"
RESULTS: list[dict] = []


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append({"check": name, "status": status, "detail": detail})


def check(name: str, operation) -> None:
    try:
        operation()
    except Exception as exc:  # noqa: BLE001 - verifier records arbitrary target failures
        record(name, "fail", f"{type(exc).__name__}: {exc}")
    else:
        record(name, "pass")


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def rejects(operation) -> None:
    try:
        operation()
    except (ValueError, TypeError, KeyError):
        return
    raise AssertionError("malformed input accepted")


def load_module(name: str):
    # Audit hook rejects writes, network and subprocess execution during imports.
    script = r"""
import sys, importlib.util
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
def audit(event,args):
    if event in ('socket.connect','socket.bind','subprocess.Popen','os.system','os.mkdir','os.remove','os.rename'):
        raise RuntimeError('import side effect: '+event)
    if event=='open':
        mode=args[1]; flags=args[2]
        if (isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (1|2|64|512|1024)):
            raise RuntimeError('import write: '+str(args[0]))
sys.addaudithook(audit)
spec=importlib.util.spec_from_file_location('blind_target',sys.argv[2])
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
"""
    path = ROOT / "spikes/23_joint_av_analyzer" / (name + ".py")
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(ROOT), str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    require(result.returncode == 0, result.stdout + result.stderr)
    spec = importlib.util.spec_from_file_location("verified_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def cpu() -> None:
    modules = {}
    for name in ("setup_analyzer", "analyze", "prepare_media"):
        check("side_effect_free_import_" + name, lambda n=name: modules.update({n: load_module(n)}))
    if "setup_analyzer" not in modules:
        record("public_seams", "unverified", "setup import failed")
        return
    module = modules["setup_analyzer"]
    original = {
        "metadata": {"total_size": 999999},
        "weight_map": {
            "thinker.a.weight": "model-00001-of-00003.safetensors",
            "thinker.b.weight": "model-00001-of-00003.safetensors",
            "thinker.c.weight": "model-00002-of-00003.safetensors",
            "talker.weight": "model-00003-of-00003.safetensors",
        },
    }
    api = {
        "siblings": [
            {
                "rfilename": f"model-0000{i}-of-00003.safetensors",
                "size": i * 100,
                "lfs": {"size": i * 100, "sha256": str(i) * 64},
            }
            for i in (1, 2, 3)
        ]
    }

    def selected():
        before = copy.deepcopy(original)
        result = module.thinker_index(original)
        require(
            result["weight_map"]
            == {k: v for k, v in original["weight_map"].items() if k.startswith("thinker.")},
            "selection/key preservation wrong",
        )
        require(original == before, "mutates original index")
        require(
            result.get("metadata", {}).get("total_size") != 999999, "copied full-model total_size"
        )

    check("thinker_exact_selection_and_metadata", selected)
    for bad in ({}, {"weight_map": {}}, {"weight_map": {"talker.a": "x.safetensors"}}):
        check(
            "reject_empty_thinker_" + str(len(RESULTS)),
            lambda b=bad: rejects(lambda: module.thinker_index(b)),
        )

    def manifest():
        a, b = copy.deepcopy(api), copy.deepcopy(original)
        got = module.model_manifest(api, original)
        expected = [
            {
                "filename": f"model-0000{i}-of-00003.safetensors",
                "size": i * 100,
                "sha256": str(i) * 64,
            }
            for i in (1, 2)
        ]
        require(
            sorted(got, key=lambda x: x["filename"]) == expected,
            "must select unique Thinker shards only",
        )
        require(api == a and original == b, "mutates input")

    check("manifest_exact_selected_unique_shards", manifest)
    for field, value in (
        ("sha256", None),
        ("sha256", "a" * 63),
        ("sha256", "g" * 64),
        ("size", 0),
        ("size", -1),
        ("size", True),
        ("size", "100"),
    ):
        bad = copy.deepcopy(api)
        bad["siblings"][0]["lfs"][field] = value
        check(
            f"reject_lfs_{field}_{value}",
            lambda b=bad: rejects(lambda: module.model_manifest(b, original)),
        )
    bad = copy.deepcopy(api)
    del bad["siblings"][0]["lfs"]
    check(
        "reject_missing_expanded_lfs", lambda: rejects(lambda: module.model_manifest(bad, original))
    )
    check(
        "reject_missing_selected_shard",
        lambda: rejects(lambda: module.model_manifest({"siblings": api["siblings"][1:]}, original)),
    )
    for path in (
        "../evil.safetensors",
        "/evil.safetensors",
        "C:/evil.safetensors",
        "a\\evil.safetensors",
        "a/evil.safetensors",
        "model.safetensors:stream",
        "CON.safetensors",
        "model.safetensors ",
        "evil.bin",
    ):
        bad_index = {"weight_map": {"thinker.a": path}}
        bad_api = {
            "siblings": [{"rfilename": path, "lfs": {"size": 100, "sha256": "a" * 64}, "size": 100}]
        }
        check(
            "reject_path_" + repr(path),
            lambda a=bad_api, b=bad_index: rejects(lambda: module.model_manifest(a, b)),
        )


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def artifacts(evidence: str | None) -> None:
    if not evidence:
        record(
            "artifact_evidence",
            "unverified",
            "Provide --evidence project-relative JSON; no runtime success inferred",
        )
        return
    root = ProjectRoot(ROOT)
    data = json.loads(root.read_text(evidence))
    for item in data.get("files", []):

        def verify_file(item=item):
            path = root.resolve(item["path"])
            require(path.stat().st_size == item["size"], "size mismatch")
            require(digest(path) == item["sha256"], "SHA256 mismatch")

        check("actual_file_" + item["path"], verify_file)
    if not data.get("files"):
        record("actual_hashes", "unverified", "no file identities supplied")
    for pair in data.get("pcm_pairs", []):

        def pcm(pair=pair):
            # Decode both sides identically; never normalize, change gain or resample.
            common = ["-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]

            def decode(path, trim):
                command = ["ffmpeg", "-v", "error", "-i", str(root.resolve(path))]
                if trim:
                    command += [
                        "-af",
                        f"atrim=start={pair['start_seconds']}:duration={pair['duration_seconds']}",
                    ]
                result = subprocess.run(
                    command + common, check=False, capture_output=True, timeout=180
                )
                require(result.returncode == 0, result.stderr.decode(errors="replace"))
                require(bool(result.stdout), "empty decoded audio")
                return result.stdout

            require(
                decode(pair["media_path"], False) == decode(pair["wav_path"], True),
                "decoded PCM differs",
            )

        check("actual_pcm_" + pair["media_path"], pcm)
    if not data.get("pcm_pairs"):
        record("actual_pcm_equality", "unverified", "no aligned source pairs supplied")
    runtime = data.get("runtime")
    if not runtime:
        record("runtime_joint_success", "unverified", "no model execution receipt supplied")
        return
    runtime_receipt(runtime["receipt_path"])


def preprocess() -> None:
    """Run inside isolated env; exercises real PyAV decoding without model/Torch."""
    try:
        import av
        import numpy as np
    except ImportError as exc:
        record("preprocessing_dependencies", "unverified", str(exc))
        return
    path = ROOT / "spikes/23_joint_av_analyzer/analyze.py"
    spec = importlib.util.spec_from_file_location("preprocessing_target", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = ProjectRoot(ROOT)
    media = root.resolve(BASE + "/media/windows_verified_20261111T/window_01.mkv")
    wav = root.resolve(
        "references/_work/mandela333_audio/20261111T062929Z_c9ede868/mandela333_23m17s_to_23m52s_native48k_float.wav"
    )

    def joint():
        frames, waveform, meta = module.decode_media(media, "joint", 8)
        require(waveform.shape == (80000,), "5s16k waveform must have exactly80000 samples")
        require(
            waveform.dtype == np.float32 and np.isfinite(waveform).all(),
            "waveform dtype/finite check failed",
        )
        require(
            len(frames) == 4 and all(f.ndim == 3 and f.shape[2] == 3 for f in frames),
            "four RGB frames required",
        )
        require(meta["audio_sample_rate_hz"] == 16000, "wrong audio sample rate")
        require(
            abs(meta["audio_first_pts_seconds"]) <= 0.001
            and abs(meta["video_first_pts_seconds"]) <= 0.001,
            "unexpected timestamp shift",
        )
        require(
            np.allclose(
                meta["video_sample_pts_relative_seconds"], [0, 1.25, 2.5, 3.75], atol=0.001
            ),
            "wrong sampled frame timing",
        )
        require(meta["video_decoded_frames"] == 120, "decoded source frame count wrong")

    check("real_preprocessing_joint_5seconds", joint)

    def audio():
        frames, waveform, meta = module.decode_media(media, "audio", 8)
        require(
            frames == [] and waveform.shape == (80000,),
            "audio-only emitted video or wrong sample count",
        )
        require(
            meta["video_decoded_frames"] == 0 and meta["video_sample_pts_relative_seconds"] == [],
            "audio-only decoded video",
        )

    check("real_preprocessing_audio_only", audio)
    source = root.resolve(BASE + "/media/source_video.mp4")
    check(
        "reject_missing_audio_joint",
        lambda: rejects(lambda: module.decode_media(source, "joint", 8)),
    )
    check(
        "reject_missing_audio_audio_mode",
        lambda: rejects(lambda: module.decode_media(source, "audio", 8)),
    )
    check(
        "reject_missing_video_joint", lambda: rejects(lambda: module.decode_media(wav, "joint", 8))
    )

    def duration():
        original_open = av.open
        observed = {"frames": 0}

        class Packet:
            def __init__(self, packet):
                self.packet = packet

            def decode(self):
                for frame in self.packet.decode():
                    observed["frames"] += 1
                    yield frame

        class Container:
            def __init__(self, container):
                self.container = container
                self.streams = container.streams

            def demux(self, *args):
                for packet in self.container.demux(*args):
                    yield Packet(packet)

            def close(self):
                self.container.close()

        av.open = lambda *args, **kwargs: Container(original_open(*args, **kwargs))
        try:
            rejects(
                lambda: module.decode_media(
                    root.resolve(BASE + "/media/aligned_23m17s_to_23m52s.mkv"), "joint", 8
                )
            )
        finally:
            av.open = original_open
        require(
            0 < observed["frames"] < 400, f"duration violation was not rejected early: {observed}"
        )

    check("real_preprocessing_duration_rejected_early", duration)


def model_artifacts() -> None:
    """Verify immutable checkpoint bytes and safetensors headers; never load tensors."""
    import math
    import struct

    root = ProjectRoot(ROOT)
    base = BASE + "/model/"

    def read(name):
        return json.loads(root.read_text(base + name))

    api, original, derived, manifest = (
        read(n)
        for n in ("api_metadata.json", "original.index.json", "thinker.index.json", "manifest.json")
    )
    receipts = sorted(root.resolve(base.rstrip("/")).glob("metadata_receipt_*.json"))
    require(bool(receipts), "metadata receipt missing")
    receipt = json.loads(receipts[-1].read_text())
    expected = {k: v for k, v in original["weight_map"].items() if k.startswith("thinker.")}
    check(
        "actual_pinned_revision",
        lambda: require(
            api["sha"] == receipt["revision"] == "ae9e1690543ffd5c0221dc27f79834d0294cba00",
            "revision mismatch",
        ),
    )
    check(
        "actual_derived_1346_thinker_keys",
        lambda: require(
            derived["weight_map"] == expected and len(expected) == 1346, "derived keyset differs"
        ),
    )
    check(
        "actual_loader_index_matches_derived",
        lambda: require(read("model.safetensors.index.json") == derived, "loader index differs"),
    )
    raw = root.resolve(base + "original.index.raw.json")
    check(
        "original_raw_and_semantic_identity",
        lambda: require(
            digest(raw)
            == receipt.get("original_index_raw_sha256", receipt.get("original_index_sha256"))
            and json.loads(raw.read_bytes()) == original,
            "raw/semantic original identity mismatch",
        ),
    )
    check(
        "actual_license_identity",
        lambda: require(
            digest(root.resolve(base + "LICENSE")) == receipt["license_sha256"]
            and "Version 2.0, January 2004" in root.read_text(base + "LICENSE"),
            "LICENSE identity/text mismatch",
        ),
    )
    siblings = {item["rfilename"]: item for item in api["siblings"]}
    selected = sorted(set(expected.values()))
    expected_manifest = [
        {
            "filename": name,
            "size": siblings[name]["lfs"]["size"],
            "sha256": siblings[name]["lfs"]["sha256"],
        }
        for name in selected
    ]
    check(
        "actual_manifest_matches_api_lfs",
        lambda: require(
            sorted(manifest, key=lambda x: x["filename"]) == expected_manifest,
            "manifest differs from selected API LFS identities",
        ),
    )
    essentials = [
        "LICENSE",
        "added_tokens.json",
        "chat_template.json",
        "config.json",
        "generation_config.json",
        "merges.txt",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
        "model.safetensors.index.json",
    ]
    for name in essentials:
        target = "original.index.raw.json" if name == "model.safetensors.index.json" else name
        path = root.resolve(base + target)
        if not path.is_file():
            record("essential_git_blob_" + name, "unverified", "file not acquired")
            continue

        def git_blob(name=name, path=path):
            content = path.read_bytes()
            if "lfs" in siblings[name]:
                require(
                    len(content) == siblings[name]["lfs"]["size"]
                    and hashlib.sha256(content).hexdigest() == siblings[name]["lfs"]["sha256"],
                    "LFS payload identity mismatch",
                )
                return
            actual = hashlib.sha1(
                b"blob " + str(len(content)).encode() + b"\0" + content
            ).hexdigest()
            require(
                len(content) == siblings[name]["size"] and actual == siblings[name]["blobId"],
                "raw Git blob identity mismatch",
            )

        check("essential_api_identity_" + name, git_blob)
    all_thinker = {}
    for item in expected_manifest:
        path = root.resolve(base + item["filename"])
        if not path.is_file():
            record(
                "actual_shard_" + item["filename"],
                "unverified",
                "not yet acquired; no partial-file pass",
            )
            continue

        def shard(item=item, path=path):
            require(path.stat().st_size == item["size"], "size mismatch")
            require(digest(path) == item["sha256"], "full SHA256 mismatch")

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    require(key not in result, "duplicate JSON header key")
                    result[key] = value
                return result

            with path.open("rb") as stream:
                size = struct.unpack("<Q", stream.read(8))[0]
                require(
                    0 < size <= 100 * 1024 * 1024 and size + 8 < item["size"], "invalid header size"
                )
                header = json.loads(stream.read(size), object_pairs_hook=unique)
            tensors = {k: v for k, v in header.items() if k != "__metadata__"}
            require(
                set(tensors)
                == {k for k, v in original["weight_map"].items() if v == item["filename"]},
                "header tensor keys differ from original shard mapping",
            )
            widths = {
                "BOOL": 1,
                "U8": 1,
                "I8": 1,
                "I16": 2,
                "U16": 2,
                "F16": 2,
                "BF16": 2,
                "I32": 4,
                "U32": 4,
                "F32": 4,
                "I64": 8,
                "U64": 8,
                "F64": 8,
                "F8_E4M3": 1,
                "F8_E5M2": 1,
            }
            offsets = []
            for key, tensor in tensors.items():
                shape = tensor["shape"]
                lo, hi = tensor["data_offsets"]
                dtype = tensor["dtype"]
                require(
                    dtype in widths
                    and isinstance(shape, list)
                    and all(type(n) is int and n >= 0 for n in shape),
                    "invalid dtype/shape: " + key,
                )
                require(
                    type(lo) is int
                    and type(hi) is int
                    and 0 <= lo <= hi <= item["size"] - 8 - size,
                    "offset out of bounds: " + key,
                )
                require(
                    hi - lo == math.prod(shape) * widths[dtype],
                    "shape/dtype byte extent mismatch: " + key,
                )
                offsets.append((lo, hi))
                if key.startswith("thinker."):
                    require(key not in all_thinker, "duplicate Thinker tensor across shards")
                    all_thinker[key] = item["filename"]
            position = 0
            for lo, hi in sorted(offsets):
                require(lo == position, "overlapping or gapped tensor extents")
                position = hi
            require(position == item["size"] - 8 - size, "unclaimed trailing payload")

        check("actual_shard_" + item["filename"], shard)
    if all(root.resolve(base + name).is_file() for name in selected):
        check(
            "all_headers_exact_1346_thinker_mapping",
            lambda: require(all_thinker == expected, "header Thinker mapping differs"),
        )
    else:
        record(
            "all_headers_exact_1346_thinker_mapping",
            "unverified",
            f"only {len(all_thinker)} Thinker tensors inspected; shards missing",
        )


def lifecycle() -> None:
    """Exercise actual Windows venv launchers, tree RSS and owned termination."""
    import time

    import psutil

    root = ProjectRoot(ROOT)
    spec = importlib.util.spec_from_file_location(
        "lifecycle_target", ROOT / "spikes/23_joint_av_analyzer/analyze.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "OwnedProcessTree"):
        record(
            "actual_owned_tree_lifecycle",
            "unverified",
            "analyze.OwnedProcessTree interface not available",
        )
        return
    invocation = BASE + "/verification/lifecycle/" + uuid.uuid4().hex[:8]
    dummy = root.write_text(
        invocation + "/dummy.py",
        "import time\npayload=bytearray(64*1024*1024)\nfor i in range(0,len(payload),4096): payload[i]=1\ntime.sleep(90)\n",
    )
    python = root.resolve(BASE + "/env/Scripts/python.exe")
    evidence = []

    def alive(pid, created):
        try:
            proc = psutil.Process(pid)
            return proc.create_time() == created and proc.is_running()
        except psutil.NoSuchProcess:
            return False

    def observe(process, known):
        parent = psutil.Process(process.pid)
        for proc in [parent, *parent.children(recursive=True)]:
            known.setdefault(proc.pid, proc.create_time())
        return [
            {"pid": pid, "created": created, "rss": psutil.Process(pid).memory_info().rss}
            for pid, created in known.items()
            if alive(pid, created)
        ]

    def cleanup(process, known):
        for pid, created in reversed(list(known.items())):
            if alive(pid, created):
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass
        process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while (
            any(alive(pid, created) for pid, created in known.items())
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        require(
            not any(alive(pid, created) for pid, created in known.items()),
            "test cleanup left owned child alive",
        )

    sentinel = subprocess.Popen(
        [str(python), "-B", str(dummy)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    sentinel_known = {sentinel.pid: psutil.Process(sentinel.pid).create_time()}
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = observe(sentinel, sentinel_known)
            if len(rows) >= 2 and sum(x["rss"] for x in rows) > 64 * 1024 * 1024:
                break
            time.sleep(0.05)
        for reason in ("explicit_stop", "deadline"):

            def scenario(reason=reason):
                process = subprocess.Popen(
                    [str(python), "-B", str(dummy)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                known = {process.pid: psutil.Process(process.pid).create_time()}
                tracker = module.OwnedProcessTree(process)
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        rows = observe(process, known)
                        sample = tracker.sample()
                        if len(rows) >= 2 and sum(x["rss"] for x in rows) > 64 * 1024 * 1024:
                            break
                        time.sleep(0.05)
                    require(len(rows) >= 2, "actual venv interpreter child was not observed")
                    sampled = {x["pid"]: x for x in sample["processes"]}
                    require(set(known) <= set(sampled), "sample omitted real descendant")
                    require(
                        all(
                            sampled[pid]["create_time"] == created for pid, created in known.items()
                        ),
                        "sample lacks matching create_time",
                    )
                    require(
                        sample["aggregate_memory_bytes"]["rss"]
                        == sum(x["memory_bytes"]["rss"] for x in sample["processes"]),
                        "aggregate RSS not sum of actual tree",
                    )
                    require(
                        sample["aggregate_memory_bytes"]["rss"] > 64 * 1024 * 1024,
                        "RSS omitted allocated native interpreter",
                    )
                    if reason == "deadline":
                        deadline = time.monotonic() + 0.2
                        while time.monotonic() < deadline:
                            time.sleep(0.02)
                    stopped = tracker.stop()
                    require(
                        stopped["verified_stopped"] and not stopped["survivors"],
                        "cleanup receipt not verified",
                    )
                    require(
                        not any(alive(pid, created) for pid, created in known.items()),
                        "real owned process survived stop",
                    )
                    require(
                        all(alive(pid, created) for pid, created in sentinel_known.items()),
                        "unrelated sentinel touched",
                    )
                    evidence.append(
                        {
                            "reason": reason,
                            "independent_processes": rows,
                            "sample": sample,
                            "stop": stopped,
                            "sentinel": sentinel_known,
                        }
                    )
                finally:
                    cleanup(process, known)

            check("actual_owned_tree_" + reason, scenario)
    finally:
        cleanup(sentinel, sentinel_known)
    root.write_text(invocation + "/evidence.json", json.dumps(evidence, indent=2))
    record(
        "lifecycle_scope",
        "unverified",
        "Exercises actual exported helper; does not execute model supervisor loop or GPU policy",
    )


def supervisor_lifecycle() -> None:
    """Real CPU child processes through actual supervisor policy and cleanup wiring."""
    import time
    import types

    import psutil

    root = ProjectRoot(ROOT)
    spec = importlib.util.spec_from_file_location(
        "supervisor_target", ROOT / "spikes/23_joint_av_analyzer/analyze.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    invocation = BASE + "/verification/supervisor_lifecycle/" + uuid.uuid4().hex[:8]
    dummy = root.write_text(
        invocation + "/dummy.py",
        "import time\npayload=bytearray(64*1024*1024)\nfor i in range(0,len(payload),4096): payload[i]=1\ntime.sleep(90)\n",
    )
    python = root.resolve(BASE + "/env/Scripts/python.exe")

    def alive(pid, created):
        try:
            return psutil.Process(pid).create_time() == created and psutil.Process(pid).is_running()
        except psutil.NoSuchProcess:
            return False

    def discover(process, identities):
        parent = psutil.Process(process.pid)
        for proc in [parent, *parent.children(recursive=True)]:
            identities.setdefault(proc.pid, proc.create_time())

    def cleanup(process, identities):
        for pid, created in reversed(list(identities.items())):
            if alive(pid, created):
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass
        process.wait(timeout=10)
        end = time.monotonic() + 10
        while (
            any(alive(pid, created) for pid, created in identities.items())
            and time.monotonic() < end
        ):
            time.sleep(0.05)
        require(
            not any(alive(pid, created) for pid, created in identities.items()),
            "owned test cleanup failed",
        )

    sentinel = subprocess.Popen(
        [str(python), "-B", str(dummy)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    sentinel_ids = {sentinel.pid: psutil.Process(sentinel.pid).create_time()}
    evidence = []
    try:
        end = time.monotonic() + 10
        while time.monotonic() < end:
            discover(sentinel, sentinel_ids)
            if len(sentinel_ids) >= 2:
                break
            time.sleep(0.05)
        for reason in ("low_ram", "deadline"):

            def scenario(reason=reason):
                processes = []
                captured = {}
                clock = [0.0]

                def spawn(cmd, **kwargs):
                    require(
                        Path(cmd[0]).resolve() == python,
                        "supervisor did not select isolated interpreter",
                    )
                    process = subprocess.Popen([cmd[0], "-B", str(dummy)], **kwargs)
                    processes.append(process)
                    captured[process.pid] = psutil.Process(process.pid).create_time()
                    end = time.monotonic() + 10
                    while time.monotonic() < end:
                        discover(process, captured)
                        if (
                            len(captured) >= 2
                            and sum(psutil.Process(pid).memory_info().rss for pid in captured)
                            > 64 * 1024 * 1024
                        ):
                            break
                        time.sleep(0.05)
                    return process

                def monotonic():
                    clock[0] += 301 if reason == "deadline" else 0.01
                    return clock[0]

                module.subprocess = types.SimpleNamespace(
                    **{
                        name: getattr(subprocess, name)
                        for name in dir(subprocess)
                        if name != "Popen"
                    },
                    Popen=spawn,
                )
                module.time = types.SimpleNamespace(
                    monotonic=monotonic, sleep=lambda seconds: time.sleep(min(seconds, 0.02))
                )
                module._gpu_memory_sample = lambda: {
                    "index": 0,
                    "used_mib": 0,
                    "free_mib": 0,
                    "test_stub": "GPU query disabled; CPU lifecycle test",
                }
                module._ram_available = lambda: (1 if reason == "low_ram" else 10) * 1024**3
                prefix = invocation + "/" + reason
                try:
                    result, telemetry = module.run_supervised(
                        [],
                        "joint",
                        1,
                        5,
                        prefix + "/result.json",
                        prefix + "/stdout.log",
                        prefix + "/stderr.log",
                        prefix + "/resources.jsonl",
                    )
                    require(
                        result["status"] == "failure" and result["error_type"] == "WorkerStopped",
                        "supervisor did not return explicit stopped failure",
                    )
                    require(
                        ("RAM" if reason == "low_ram" else "deadline") in result["error"],
                        "wrong policy stop reason",
                    )
                    require(len(captured) >= 2, "actual venv launcher child missing")
                    require(
                        not any(alive(pid, created) for pid, created in captured.items()),
                        "supervisor left actual owned child alive",
                    )
                    require(
                        all(alive(pid, created) for pid, created in sentinel_ids.items()),
                        "supervisor touched unrelated sentinel",
                    )
                    require(
                        telemetry["owned_worker_peak_rss_bytes"] > 64 * 1024 * 1024,
                        "supervisor RSS omitted allocated interpreter",
                    )
                    samples = telemetry["ram_available_samples"]
                    if reason == "low_ram":
                        require(len(samples) >= 3, "RAM policy fired before three actual samples")
                    require(
                        any(
                            set(captured) <= {row["pid"] for row in sample["owned_processes"]}
                            for sample in samples
                        ),
                        "telemetry omitted real descendant identity",
                    )
                    evidence.append(
                        {
                            "scenario": reason,
                            "result": result,
                            "telemetry": telemetry,
                            "independent_owned_identities": captured,
                            "unrelated_sentinel": sentinel_ids,
                        }
                    )
                finally:
                    for process in processes:
                        cleanup(process, captured)

            check("actual_supervisor_" + reason, scenario)
    finally:
        cleanup(sentinel, sentinel_ids)
    root.write_text(
        invocation + "/evidence.json",
        json.dumps(
            {
                "substitutions": [
                    "worker argv points to contained64MiB CPU dummy instead of analyzer worker",
                    "GPU sampler disabled explicit stub",
                    "policy clock accelerated and sleep capped20ms",
                    "availableRAM injected1GiB or10GiB",
                ],
                "real_components": [
                    "isolated Windows venv interpreter/launcher",
                    "Popen",
                    "psutil",
                    "actual supervisor loop",
                    "owned process helper",
                    "filesystem receipts",
                ],
                "runs": evidence,
            },
            indent=2,
        ),
    )


def serialization() -> None:
    """Amendment4 diagnostics and retained-result tests; never load models."""
    spec = importlib.util.spec_from_file_location(
        "serialization_target", ROOT / "spikes/23_joint_av_analyzer/analyze.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "normalize_loading_issues"):
        record("serialization_public_seam", "fail", "normalize_loading_issues unavailable")
        return
    normalize = module.normalize_loading_issues
    fields = ("missing_keys", "unexpected_keys", "mismatched_keys")

    def empty():
        native = {key: set() for key in fields}
        before = copy.deepcopy(native)
        got = normalize(native)
        require(got == {key: [] for key in fields}, "native emptysets not normalized")
        require(native == before, "mutated native diagnostics")
        require(json.loads(json.dumps(got, allow_nan=False)) == got, "not strictJSON")
        require(normalize({}) == got, "missing fields must become emptylists")

    check("serialization_native_empty_sets", empty)
    for container in (list, tuple, set, frozenset):

        def nonempty(container=container):
            native = {
                "missing_keys": container(["z.weight", "a.weight"]),
                "unexpected_keys": container(["extra.weight"]),
            }
            before = copy.deepcopy(native)
            got = normalize(native)
            require(
                set(got["missing_keys"]) == {"z.weight", "a.weight"}
                and got["unexpected_keys"] == ["extra.weight"],
                "nonempty diagnostics lost",
            )
            require(native == before, "mutated inputs")
            if container in (set, frozenset):
                require(
                    got["missing_keys"] == ["a.weight", "z.weight"], "set order not deterministic"
                )
            require(all(isinstance(got[key], list) for key in fields), "outputs must be lists")
            json.dumps(got, allow_nan=False)

        check("serialization_nonempty_" + container.__name__, nonempty)

    def mismatch():
        native = {"mismatched_keys": (("layer.weight", (2, 3), (3, 4)),)}
        got = normalize(native)
        require(
            json.loads(json.dumps(got, allow_nan=False))["mismatched_keys"]
            == [["layer.weight", [2, 3], [3, 4]]],
            "mismatched tensor shapes lost",
        )

    check("serialization_mismatched_tuple_shapes", mismatch)
    for index, bad in enumerate((None, "weight", b"weight", 123, {}, iter(["weight"]))):
        check(
            "serialization_reject_container_" + str(index),
            lambda bad=bad: rejects(lambda: normalize({"missing_keys": bad})),
        )
    for index, bad in enumerate(
        (
            object(),
            b"bytes",
            float("nan"),
            float("inf"),
            float("-inf"),
            {"nested": float("nan")},
            {"nested": set()},
        )
    ):
        check(
            "serialization_reject_member_" + str(index),
            lambda bad=bad: rejects(lambda: normalize({"unexpected_keys": [bad]})),
        )
    root = ProjectRoot(ROOT)
    invocation = BASE + "/verification/serialization/" + uuid.uuid4().hex[:8]
    native = {key: set() for key in fields}
    module.load_model = lambda: (
        object(),
        object(),
        normalize(native),
        [],
        [],
        "CPU-test-noGPU",
        0,
        {"test": "model load stub"},
    )
    original_analyze = module.analyze_one
    valid = {
        "text": "CPU test retained observation",
        "decoded_media": {},
        "tensor_shapes": {},
        "elapsed_seconds": 0.0,
    }
    paths = []

    def read_receipt(path):
        def invalid_constant(value):
            raise ValueError("nonfiniteJSON:" + value)

        return json.loads(root.read_text(path), parse_constant=invalid_constant)

    def success():
        module.analyze_one = lambda *args: copy.deepcopy(valid)
        path = invocation + "/native_empty_success.json"
        paths.append(path)
        code = module._worker(
            [{"media": "CPU-test-no-media-read", "start": 1397}], "joint", 1, 5, path
        )
        require(code == 0, "valid nativeempty diagnostic run failed")
        receipt = read_receipt(path)
        require(
            receipt["status"] == "success" and len(receipt["results"]) == 1, "valid result missing"
        )
        require(receipt["results"][0]["text"] == valid["text"], "valid result text lost")

    check("serialization_worker_native_empty_success", success)
    for label, bad in (("object", object()), ("nan", float("nan"))):

        def partial(label=label, bad=bad):
            count = [0]

            def analyze(*args):
                count[0] += 1
                return (
                    copy.deepcopy(valid) if count[0] == 1 else {**valid, "invalid_candidate": bad}
                )

            module.analyze_one = analyze
            path = invocation + "/partial_" + label + ".json"
            paths.append(path)
            code = module._worker(
                [
                    {"media": "CPU-test-first", "start": 1397},
                    {"media": "CPU-test-second", "start": 1402},
                ],
                "joint",
                1,
                5,
                path,
            )
            require(code != 0, "malformed candidate returned success")
            receipt = read_receipt(path)
            require(
                receipt["status"] == "failure" and len(receipt["results"]) == 1,
                "prior valid result lost or invalid appended",
            )
            require(receipt["results"][0]["text"] == valid["text"], "first valid text lost")

        check("serialization_worker_partial_" + label, partial)
    module.analyze_one = original_analyze
    root.write_text(
        invocation + "/test_scope.json",
        json.dumps(
            {
                "substitutions": [
                    "load_model stub returns normalized native-set diagnostics; no model/Torch/CUDA load",
                    "analyze_one stub returns valid then malformedcandidate",
                ],
                "worker_receipts": paths,
                "checks": "real worker receipt writes and strictJSON rereads",
            },
            indent=2,
        ),
    )


def runtime_receipt(relative: str, expected_mode: str = "joint") -> None:
    """Verify original retained runtime schema and hashes without loading a model."""
    root = ProjectRoot(ROOT)

    def strict_json(path):
        def invalid(value):
            raise ValueError("nonfinite JSON constant:" + value)

        return json.loads(root.read_text(path), parse_constant=invalid)

    receipt = strict_json(relative)
    pin = "ae9e1690543ffd5c0221dc27f79834d0294cba00"
    check(
        "runtime_original_status_pin_seed",
        lambda: require(
            receipt["status"] == "success"
            and receipt["model_id"] == "Qwen/Qwen2.5-Omni-7B"
            and receipt["revision"] == pin
            and receipt["mode"] == expected_mode
            and receipt["seed"] == 0
            and receipt["do_sample"] is False,
            "runtime status/modelpin/mode/seed mismatch",
        ),
    )
    check(
        "runtime_inline_raw_output",
        lambda: require(
            isinstance(receipt["output_text"], str) and bool(receipt["output_text"].strip()),
            "no retained model text",
        ),
    )
    check(
        "runtime_loading_empty_lists",
        lambda: require(
            all(
                receipt["loading_info"][key] == []
                for key in ("missing_keys", "unexpected_keys", "mismatched_keys")
            ),
            "nonempty/malformed native loading diagnostics",
        ),
    )

    def tensors():
        shapes = receipt["tensor_shapes"]
        required = ["input_features", "feature_attention_mask"]
        if expected_mode == "joint":
            required += ["pixel_values_videos", "video_grid_thw"]
        else:
            require(
                "pixel_values_videos" not in shapes and "video_grid_thw" not in shapes,
                "audio-only receipt contains video/grid tensors",
            )
        for key in required:
            shape = shapes[key]
            require(
                isinstance(shape, list)
                and bool(shape)
                and all(type(n) is int and n > 0 for n in shape),
                "empty/invalid modality tensor " + key,
            )
        require(
            0 < receipt["decoded_media"]["audio_duration_seconds"] <= 8,
            "window duration exceeds bound",
        )
        if expected_mode == "joint":
            require(receipt["decoded_media"]["video_frames_sampled"] >= 4, "fewer than4frames")
        else:
            require(
                receipt["decoded_media"]["video_frames_sampled"] == 0,
                "audio-only receipt sampled video",
            )

    check("runtime_nonempty_" + expected_mode + "_tensor_shapes", tensors)

    def generation_diagnostics():
        kwargs = receipt["effective_generation_kwargs"]
        cap = receipt["max_new_tokens"]
        require(
            type(cap) is int and 0 < cap <= 768 and kwargs["max_new_tokens"] == cap,
            "effective cap mismatch",
        )
        require(
            type(kwargs["eos_token_id"]) is int
            and kwargs["eos_token_id"] == 151645
            and type(kwargs["pad_token_id"]) is int
            and kwargs["pad_token_id"] == 151643,
            "effective pinnedEOS/PAD missing",
        )
        require(
            kwargs["do_sample"] is False
            and kwargs["use_audio_in_video"] is (expected_mode == "joint"),
            "effective sampling/modality mismatch",
        )
        tokens = receipt["output_token_ids"]
        require(
            isinstance(tokens, list)
            and 0 < len(tokens) <= cap
            and all(type(token) is int and token >= 0 for token in tokens),
            "invalid/empty/overcap raw output IDs",
        )
        require(
            type(receipt["output_token_count"]) is int
            and receipt["output_token_count"] == len(tokens),
            "output count mismatch",
        )
        positions = [index for index, token in enumerate(tokens) if token == 151645]
        require(not positions or positions == [len(tokens) - 1], "tokens after firstEOS")
        expected_reason = "eos" if positions else ("token_cap" if len(tokens) == cap else "other")
        require(
            receipt["termination_reason"] == expected_reason
            and receipt["token_cap_reached"] is (expected_reason == "token_cap"),
            "termination diagnosis inconsistent with raw IDs",
        )

    check("runtime_effective_generation_and_raw_token_termination", generation_diagnostics)
    check(
        "runtime_script_hash_actual",
        lambda: require(
            digest(ROOT / "spikes/23_joint_av_analyzer/analyze.py") == receipt["script_sha256"],
            "current frozen source differs from receipt",
        ),
    )
    check(
        "runtime_media_hash_actual",
        lambda: require(
            digest(root.resolve(receipt["media_path"])) == receipt["media_sha256"],
            "media differs from receipt",
        ),
    )
    telemetry = receipt["telemetry"]

    def coverage():
        path = telemetry["module_coverage_path"]
        require(
            digest(root.resolve(path)) == telemetry["module_coverage_sha256"],
            "coverage file hash differs",
        )
        data = strict_json(path)
        quant = data["quantized_linear4bit"]
        unquant = data["unquantized_linear"]
        require(
            bool(quant)
            and bool(unquant)
            and len(quant) == len(set(quant))
            and len(unquant) == len(set(unquant)),
            "empty/duplicate module audit",
        )
        require(
            len(quant) == receipt["quantized_linear4bit_count"]
            and len(unquant) == receipt["unquantized_linear_count"],
            "coverage counts differ",
        )
        for names, key in (
            (quant, "quantized_linear4bit_sha256"),
            (unquant, "unquantized_linear_sha256"),
        ):
            require(
                hashlib.sha256("\n".join(names).encode()).hexdigest() == receipt[key],
                "coverage module names hash differs",
            )
        require(not any("audio_tower" in n or "visual" in n for n in quant), "encoder quantized")
        require(
            all("audio_tower" in n or "visual" in n or "lm_head" in n for n in unquant),
            "eligible language layer unquantized",
        )

    check("runtime_actual_module_coverage", coverage)

    def cleanup():
        data = telemetry["owned_process_tree_cleanup"]
        require(
            data["verified_stopped"] is True
            and data["survivors"] == []
            and data.get("unverifiable_identities", []) == []
            and data.get("failures", []) == [],
            "owned cleanup not verified",
        )
        identities = telemetry["owned_process_identities_final"]
        require(
            bool(identities) and all(row["live_identity_verified"] is False for row in identities),
            "owned final identity remains live/unverified",
        )

    check("runtime_cleanup_receipt_zero_survivors", cleanup)

    def resources():
        path = telemetry["resources_path"]
        require(
            digest(root.resolve(path)) == telemetry["resources_sha256"],
            "telemetry JSONL hash differs",
        )
        rows = [json.loads(line) for line in root.read_text(path).splitlines() if line.strip()]
        samples = [row for row in rows if row.get("event") == "sample"]
        require(bool(samples), "no periodic telemetry retained")
        require(
            any(len(row["owned_processes"]) >= 2 for row in samples),
            "actual interpreter child absent from telemetry",
        )
        require(
            telemetry["owned_worker_peak_rss_bytes"]
            == max(row["owned_worker_rss_bytes"] for row in samples),
            "peakRSS disagrees with retained samples",
        )

    check("runtime_retained_resource_hash_and_samples", resources)

    def worker():
        data = strict_json(telemetry["worker_result_path"])
        require(
            data["status"] == "success" and bool(data["results"]), "worker did not retain success"
        )
        import re

        command = telemetry["owned_process"]["cmd"]
        require("--_worker-json" in command, "retained worker command lacks inputspec")
        specification = json.loads(command[command.index("--_worker-json") + 1])
        require(specification["mode"] == expected_mode, "worker command mode mismatch")
        name = Path(relative).name
        ordinal = re.fullmatch(r"window_(\d+)\.json", name)
        index = int(ordinal.group(1)) - 1 if ordinal else 0
        if not ordinal:
            require(len(specification["items"]) == 1, "single receipt does not bind unique input")
        require(
            0 <= index < len(specification["items"]) and index < len(data["results"]),
            "batch ordinal missing",
        )
        item = specification["items"][index]
        require(
            Path(item["media"]).resolve() == root.resolve(receipt["media_path"])
            and item["start"] == receipt["source_start_seconds"],
            "ordinal source media/start mismatch",
        )
        candidate = data["results"][index]
        require(
            candidate["text"] == receipt["output_text"]
            and candidate["tensor_shapes"] == receipt["tensor_shapes"],
            "ordinal worker text/tensors mismatch",
        )
        for key in (
            "output_token_ids",
            "output_token_count",
            "termination_reason",
            "token_cap_reached",
            "effective_generation_kwargs",
            "prompt",
        ):
            require(candidate[key] == receipt[key], "ordinal worker field mismatch: " + key)

    check("runtime_worker_retained_text_identity", worker)

    def decode_replay():
        python = root.resolve(BASE + "/env/Scripts/python.exe")
        if not python.is_file():
            record(
                "runtime_local_tokenizer_decode_replay",
                "unverified",
                "isolated tokenizer runtime unavailable",
            )
            return
        script = "import json,sys; from tokenizers import Tokenizer; t=Tokenizer.from_file(sys.argv[1]); print(json.dumps(t.decode(json.loads(sys.argv[2]),skip_special_tokens=True),ensure_ascii=False))"
        import os

        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        replay_root = root.make_dir(BASE + "/verification/tokenizer_replay/" + uuid.uuid4().hex[:8])
        env.update(TMP=str(replay_root), TEMP=str(replay_root), TMPDIR=str(replay_root), HF_HOME=str(replay_root), XDG_CACHE_HOME=str(replay_root), PYTHONNOUSERSITE="1")
        env.update(
            CUDA_VISIBLE_DEVICES="",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONIOENCODING="utf-8",
        )
        result = subprocess.run(
            [
                str(python),
                "-B",
                "-c",
                script,
                str(root.resolve(BASE + "/model/tokenizer.json")),
                json.dumps(receipt["output_token_ids"]),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            env=env,
        )
        if result.returncode != 0 and "ModuleNotFoundError" in result.stderr:
            record(
                "runtime_local_tokenizer_decode_replay",
                "unverified",
                "tokenizers library unavailable",
            )
            return
        require(result.returncode == 0, result.stderr)
        require(
            json.loads(result.stdout) == receipt["output_text"],
            "local pinned tokenizer decode differs from retained text",
        )
        record("runtime_local_tokenizer_decode_replay", "pass")

    try:
        decode_replay()
    except Exception as exc:  # noqa: BLE001 - preserve optional replay errors
        record("runtime_local_tokenizer_decode_replay", "fail", f"{type(exc).__name__}: {exc}")

    def provenance():
        model = BASE + "/model/"
        data = receipt["model_provenance"]
        manifest = strict_json(model + "manifest.json")
        require(
            digest(root.resolve(model + "manifest.json")) == data["manifest_sha256"]
            and data["shards"] == manifest,
            "manifest/provenance differs",
        )
        api = strict_json(model + "api_metadata.json")
        require(
            api["sha"] == pin
            and hashlib.sha256(json.dumps(api, sort_keys=True).encode()).hexdigest()
            == data["api_snapshot_canonical_sha256"],
            "canonical pinnedAPI differs",
        )
        for pattern, key in (
            ("metadata_receipt*.json", "metadata_receipt_sha256"),
            ("weights_receipt*.json", "weights_receipt_sha256"),
        ):
            matches = [
                path
                for path in root.resolve(model.rstrip("/")).glob(pattern)
                if digest(path) == data[key]
            ]
            require(len(matches) >= 1, "actual retained " + key + " missing")
            saved = json.loads(matches[0].read_text())
            require(
                saved["revision"] == pin and saved["model_id"] == receipt["model_id"],
                "receipt modelpin differs",
            )
            if key == "weights_receipt_sha256":
                files = {row["filename"]: row for row in saved["files"]}
                for row in manifest:
                    file = files[row["filename"]]
                    require(
                        file["observed_sha256"] == row["sha256"] and file["size"] == row["size"],
                        "verified weightsreceipt differs from manifest",
                    )
                    require(
                        root.resolve(model + row["filename"]).stat().st_size == row["size"],
                        "actual checkpoint size changed",
                    )
        for name, expected_hash in data["runtime_receipt_hashes"].items():
            require(
                digest(root.resolve(BASE + "/" + name)) == expected_hash,
                "runtime setup receipt changed",
            )
        require(
            receipt["allocator_warmup_policy"]
            == data["allocator_warmup_policy"]
            == "disabled_during_from_pretrained_only",
            "warmup experiment not recorded consistently",
        )

    check("runtime_provenance_actual_receipt_hashes", provenance)
    record(
        "runtime_postexit_limits",
        "unverified",
        "Receipt and files checked after exit; no independent model reload, BF16/NF4 parameter inspection, tensor replay or semantic validation. Checkpoint payload SHA hashes are bound to retained full-weight verification receipts and sizes;20GBpayloads not rehashed in this mode.",
    )


def generation() -> None:
    """Blind Amendment5 CPU seams and source-level generation protocol checks."""
    import ast
    import re
    import types

    spec = importlib.util.spec_from_file_location(
        "generation_target", ROOT / "spikes/23_joint_av_analyzer/analyze.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("generation_token_ids", "generation_termination"):
        if not hasattr(module, name):
            record("generation_public_seam_" + name, "fail", "public seam missing")
            return
    pinned = {"eos_token_id": 151645, "pad_token_id": 151643}

    def tokens():
        tokenizer = types.SimpleNamespace(**pinned)
        require(module.generation_token_ids(tokenizer) == pinned, "pinned IDs not exact")

    check("generation_pinned_tokenizer_ids", tokens)
    for field in pinned:
        for index, value in enumerate((None, True, False, -1, 0, 151644, 151645.0, "151645")):
            values = {**pinned, field: value}
            check(
                f"generation_reject_{field}_{index}",
                lambda values=values: rejects(
                    lambda: module.generation_token_ids(types.SimpleNamespace(**values))
                ),
            )
        values = {key: value for key, value in pinned.items() if key != field}
        check(
            "generation_reject_missing_" + field,
            lambda values=values: rejects(
                lambda: module.generation_token_ids(types.SimpleNamespace(**values))
            ),
        )
    terminate = module.generation_termination
    for label, ids, cap, reason in (
        ("eos", [3, 151645], 5, "eos"),
        ("eos_at_cap", [3, 151645], 2, "eos"),
        ("only_eos", [151645], 1, "eos"),
        ("cap", [3, 4], 2, "token_cap"),
        ("other", [3], 2, "other"),
    ):

        def valid(ids=ids, cap=cap, reason=reason):
            original = ids.copy()
            got = terminate(ids, 151645, cap)
            expected = {
                "output_token_ids": original,
                "output_token_count": len(original),
                "termination_reason": reason,
                "token_cap_reached": reason == "token_cap",
            }
            require(got == expected, "termination result differs: " + repr(got))
            require(
                got["output_token_ids"] is not ids and ids == original, "must copy/preserve raw IDs"
            )
            json.dumps(got, allow_nan=False)

        check("generation_boundary_" + label, valid)
    malformed = (
        [],
        (),
        [True],
        [False],
        [1.0],
        ["1"],
        [-1],
        [None],
        [object()],
        [151645, 3],
        [151645, 151645],
        [1, 2, 3],
    )
    for index, ids in enumerate(malformed):
        check(
            "generation_reject_output_" + str(index),
            lambda ids=ids: rejects(lambda: terminate(ids, 151645, 2)),
        )
    for index, eos in enumerate((None, True, False, -1, 1.0, "151645")):
        check(
            "generation_reject_eos_" + str(index),
            lambda eos=eos: rejects(lambda: terminate([1], eos, 2)),
        )
    for index, cap in enumerate((None, True, False, 0, -1, 2.0, "2")):
        check(
            "generation_reject_cap_" + str(index),
            lambda cap=cap: rejects(lambda: terminate([1], 151645, cap)),
        )
    for mode in ("joint", "audio"):

        def prompt(mode=mode):
            base = module.prompt_for_mode(mode)
            require(module.prompt_for_mode(mode, None) == base, "None must retain baseprompt")
            got = module.prompt_for_mode(mode, 5.125)
            displayed = re.search(
                r"(?P<duration>\d+(?:\.\d+)?)\s*(?:-\s*)?seconds?", got, flags=re.IGNORECASE
            )
            require(displayed is not None and got != base, "displayed actual duration missing")
            require(
                abs(float(displayed.group("duration")) - 5.125) <= 0.005 + 1e-9,
                "displayed duration differs by more than allowed2dp rounding",
            )
            require(
                any(word in got.lower() for word in ("timecode", "timestamp"))
                and any(
                    word in got.lower() for word in ("not", "avoid", "without", "never", "no ")
                ),
                "no explicit invented-timecode constraint",
            )
            if mode == "audio":
                require(
                    not any(
                        word in got.lower()
                        for word in (
                            "visible",
                            "visual observations",
                            "describe the scene",
                            "what you see",
                        )
                    ),
                    "audio asks for visual observations",
                )
            require(
                not any(
                    word in got.lower()
                    for word in ("hoof", "footstep", "corridor", "static", "scream", "silhouette")
                ),
                "prompt plants assumed scene/sound",
            )

        check("generation_duration_prompt_" + mode, prompt)

        def no_timecodes(mode=mode):
            text = module.prompt_for_mode(mode, 5).lower()
            require(
                any(word in text for word in ("timecode", "timestamp"))
                and any(word in text for word in ("not", "avoid", "without", "never", "no ")),
                "no explicit invented-timecode constraint",
            )

        check("generation_no_invented_timecodes_" + mode, no_timecodes)
        for index, value in enumerate(
            (True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "5")
        ):
            check(
                f"generation_reject_duration_{mode}_{index}",
                lambda value=value, mode=mode: rejects(lambda: module.prompt_for_mode(mode, value)),
            )

    def source_protocol():
        source = (ROOT / "spikes/23_joint_av_analyzer/analyze.py").read_text()
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "analyze_one"
        )
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        id_assignments = {
            node.targets[0].id
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "generation_token_ids"
        }
        # Follow validated IDs through intermediate kwargs dictionary expansions.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Dict)
                ):
                    spreads = [
                        value.id
                        for key, value in zip(node.value.keys, node.value.values, strict=True)
                        if key is None and isinstance(value, ast.Name)
                    ]
                    if set(spreads) & id_assignments and node.targets[0].id not in id_assignments:
                        id_assignments.add(node.targets[0].id)
                        changed = True
        generates = [
            node
            for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == "generate"
        ]
        require(
            len(generates) == 1 and bool(id_assignments),
            "no validated generation IDs seam at generate boundary",
        )
        kwargs = generates[0].keywords
        expanded = {
            kw.value.id for kw in kwargs if kw.arg is None and isinstance(kw.value, ast.Name)
        }
        explicit = {kw.arg for kw in kwargs}
        require(
            bool(id_assignments & expanded) or {"eos_token_id", "pad_token_id"} <= explicit,
            "validatedEOS/PAD not explicitly supplied togenerate",
        )
        require(
            any(
                isinstance(node.func, ast.Name) and node.func.id == "generation_termination"
                for node in calls
            ),
            "raw output termination not audited",
        )
        require(
            any(
                isinstance(node.func, ast.Attribute) and node.func.attr == "tolist"
                for node in calls
            ),
            "raw output tokenIDs not materialized",
        )
        prompts = [
            node
            for node in calls
            if isinstance(node.func, ast.Name) and node.func.id == "prompt_for_mode"
        ]
        require(
            any(
                any(
                    isinstance(child, ast.Subscript)
                    and isinstance(child.slice, ast.Constant)
                    and child.slice.value == "audio_duration_seconds"
                    for child in ast.walk(node)
                )
                for node in prompts
            ),
            "per-window prompt not fed decoded actual duration",
        )

    check("generation_source_protocol_wiring", source_protocol)

    def retained_native_probe():
        path = ROOT / BASE / "review_generation/20261111T172421Z_3ce98fa9/results.json"
        data = json.loads(path.read_text())
        require(
            data["disk_generation"]["eos_token_id"] is None
            and data["prepared_effective"]["eos_token_id"] is None,
            "baseline probe does not expose missingEOS",
        )
        require(
            all(
                data["explicit_effective"][key] == value and data["tokenizer"][key] == value
                for key, value in pinned.items()
            ),
            "native explicit configuration not effective",
        )
        require(
            data["no_model_constructed"] is True and data["no_cuda_calls"] is True,
            "probe scope mismatch",
        )

    check("generation_retained_native_config_probe", retained_native_probe)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--generation", action="store_true")
    parser.add_argument("--runtime-receipt")
    parser.add_argument("--expected-mode", choices=("joint", "audio"), default="joint")
    parser.add_argument("--serialization", action="store_true")
    parser.add_argument("--lifecycle", action="store_true")
    parser.add_argument("--supervisor-lifecycle", action="store_true")
    parser.add_argument("--model-artifacts", action="store_true")
    parser.add_argument("--preprocess", action="store_true")
    parser.add_argument("--artifacts", action="store_true")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    if not (
        args.cpu
        or args.generation
        or args.serialization
        or args.runtime_receipt
        or args.artifacts
        or args.preprocess
        or args.model_artifacts
        or args.lifecycle
        or args.supervisor_lifecycle
    ):
        parser.error("select --cpu, --artifacts and/or --preprocess")
    if args.cpu:
        cpu()
    if args.generation:
        generation()
    if args.runtime_receipt:
        try:
            runtime_receipt(args.runtime_receipt, args.expected_mode)
        except Exception as exc:  # noqa: BLE001 - retain artifact inspection errors
            record("runtime_receipt", "fail", f"{type(exc).__name__}: {exc}")
    if args.serialization:
        serialization()
    if args.supervisor_lifecycle:
        try:
            supervisor_lifecycle()
        except Exception as exc:  # noqa: BLE001 - retain real supervisor failures
            record("supervisor_lifecycle", "fail", f"{type(exc).__name__}: {exc}")
    if args.lifecycle:
        try:
            lifecycle()
        except Exception as exc:  # noqa: BLE001 - retain real lifecycle failures
            record("lifecycle", "fail", f"{type(exc).__name__}: {exc}")
    if args.model_artifacts:
        try:
            model_artifacts()
        except Exception as exc:  # noqa: BLE001 - report artifact inspection failures
            record("model_artifacts", "fail", f"{type(exc).__name__}: {exc}")
    if args.preprocess:
        preprocess()
    if args.artifacts:
        try:
            artifacts(args.evidence)
        except Exception as exc:  # noqa: BLE001 - verifier records arbitrary target failures
            record("artifact_inspection", "fail", f"{type(exc).__name__}: {exc}")
    report = {
        "schema_version": 1,
        "checks": RESULTS,
        "summary": {
            status: sum(x["status"] == status for x in RESULTS)
            for status in ("pass", "fail", "unverified")
        },
    }
    target = f"{BASE}/verification/{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}/report.json"
    ProjectRoot(ROOT).write_text(target, json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": target, **report["summary"]}))
    return 1 if report["summary"]["fail"] else (2 if report["summary"]["unverified"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
