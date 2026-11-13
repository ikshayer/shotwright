from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from email.parser import Parser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
MODEL_ID = "Qwen/Qwen2.5-Omni-7B"
REVISION = "ae9e1690543ffd5c0221dc27f79834d0294cba00"
BASE = "spikes/23_joint_av_analyzer/_work"
WHEEL_DIR = "spikes/22_minimax_h3_workflows/_work/local_runtime/downloads/wheels"
NATIVE_WHEELS = {
    "torch": ("torch-2.11.0+cu130-cp311-cp311-win_amd64.whl", "a1ff66c0ad21bf48c3187e84a08a6895d48e9bae435e27811b0b65f36bef4555"),
    "torchvision": ("torchvision-0.26.0+cu130-cp311-cp311-win_amd64.whl", "d019b9d02433515d59ad403d8a6f521da7844c030d6c8003eeec39e15e59f9fa"),
}


def thinker_index(original: dict) -> dict:
    weights = original.get("weight_map")
    if not isinstance(weights, dict):
        raise TypeError("index weight_map must be a mapping")
    selected = {key: name for key, name in weights.items() if key.startswith("thinker.")}
    if not selected:
        raise ValueError("index contains no thinker.* tensors")
    result = {"metadata": {}, "weight_map": selected}
    total = original.get("metadata", {}).get("total_size")
    if isinstance(total, int) and total > 0 and set(selected) == set(weights):
        result["metadata"]["total_size"] = total
    return result


def model_manifest(api: dict, original_index: dict) -> list[dict]:
    index = thinker_index(original_index)
    needed = set(index["weight_map"].values())
    records = []
    for entry in api.get("siblings", []):
        name = entry.get("rfilename")
        if name not in needed:
            continue
        path = PurePosixPath(name)
        if (not isinstance(name, str) or path.is_absolute() or len(path.parts) != 1
                or any(part in (".", "..") for part in path.parts) or "\\" in name
                or not re.fullmatch(r"model-\d+-of-\d+\.safetensors", name)):
            raise ValueError(f"unsafe model shard path: {name!r}")
        lfs = entry.get("lfs") or {}
        digest = lfs.get("sha256") or lfs.get("oid", "").removeprefix("sha256:")
        size = entry.get("size")
        lfs_size = lfs.get("size")
        if (not isinstance(size, int) or isinstance(size, bool) or size <= 0
                or (lfs_size is not None and (not isinstance(lfs_size, int) or isinstance(lfs_size, bool) or lfs_size != size))
                or not re.fullmatch(r"[0-9a-f]{64}", str(digest))):
            raise ValueError(f"missing valid LFS size/SHA256 for {name}")
        PROJECT.resolve(f"{BASE}/model/{name}")
        if any(row["filename"] == name for row in records):
            raise ValueError(f"duplicate model shard metadata: {name}")
        records.append({"filename": name, "size": size, "sha256": digest})
    if {row["filename"] for row in records} != needed:
        raise ValueError("Hub metadata did not describe every selected shard")
    return sorted(records, key=lambda row: row["filename"])


def _write(relative: str, value: object) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    target = PROJECT.resolve(relative)
    if target.exists():
        if PROJECT.read_text(relative) != payload:
            raise FileExistsError(f"refusing to replace different provenance artifact: {relative}")
        return
    PROJECT.write_text(relative, payload)


def metadata() -> None:
    api_url = f"https://huggingface.co/api/models/{MODEL_ID}/revision/{REVISION}?blobs=true"
    headers = {"User-Agent": "CCTV-joint-av-spike/1.0"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(api_url, headers=headers), timeout=30) as response:
        api = json.load(response)
    if api.get("sha") != REVISION:
        raise ValueError(f"Hub API returned revision {api.get('sha')!r}, expected {REVISION}")
    base = f"{BASE}/model"
    index_rel = f"{base}/original.index.json"
    index_url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/model.safetensors.index.json"
    with opener.open(urllib.request.Request(index_url, headers=headers), timeout=30) as response:
        original_bytes = response.read(4 * 1024 * 1024)
    index_item = next((x for x in api.get("siblings", []) if x.get("rfilename") == "model.safetensors.index.json"), None)
    if index_item is None or index_item.get("size") != len(original_bytes) or index_item.get("blobId") != _git_blob_sha1(original_bytes):
        raise ValueError("pinned original index failed advertised size/Git blob SHA1 check")
    original = json.loads(original_bytes)
    if PROJECT.resolve(index_rel).exists():
        if json.loads(PROJECT.read_text(index_rel)) != original:
            raise FileExistsError("refusing to replace differing pinned original index")
    else:
        PROJECT.write_bytes(index_rel, original_bytes)
    raw_index_rel = f"{base}/original.index.raw.json"
    if PROJECT.resolve(raw_index_rel).exists():
        if PROJECT.read_bytes(raw_index_rel) != original_bytes:
            raise FileExistsError("refusing to replace differing raw pinned original index")
    else:
        PROJECT.write_bytes(raw_index_rel, original_bytes)
    selected = thinker_index(original)
    manifest = model_manifest(api, selected)
    _write(f"{base}/thinker.index.json", selected)
    _write(f"{base}/model.safetensors.index.json", selected)
    _write(f"{base}/manifest.json", manifest)
    license_url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/LICENSE"
    with opener.open(urllib.request.Request(license_url, headers=headers), timeout=30) as response:
        license_bytes = response.read(256 * 1024)
    license_item = next((x for x in api.get("siblings", []) if x.get("rfilename") == "LICENSE"), None)
    if license_item is None or license_item.get("size") != len(license_bytes) or license_item.get("blobId") != _git_blob_sha1(license_bytes):
        raise ValueError("pinned LICENSE failed advertised size/Git blob SHA1 check")
    license_text = license_bytes.decode("utf-8", errors="replace")
    if "Apache License" not in license_text or "Version 2.0" not in license_text:
        raise ValueError("pinned LICENSE does not identify Apache License 2.0")
    license_rel = f"{base}/LICENSE"
    if PROJECT.resolve(license_rel).exists():
        if PROJECT.read_bytes(license_rel) != license_bytes:
            raise FileExistsError("refusing to replace different pinned LICENSE")
    else:
        PROJECT.write_bytes(license_rel, license_bytes)
    stamp = datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%S%fZ')
    api_rel = f"{base}/api_metadata_{stamp}.json"
    _write(api_rel, api)
    receipt = f"{base}/metadata_receipt_{stamp}.json"
    _write(receipt, {
        "model_id": MODEL_ID, "revision": REVISION, "api": api_url,
        "license": api.get("cardData", {}).get("license"), "license_sha256": hashlib.sha256(license_bytes).hexdigest(),
        "index_url": index_url, "original_index_raw_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "original_index_semantic_sha256": hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest(),
        "api_metadata_path": api_rel, "api_metadata_sha256": hashlib.sha256(json.dumps(api, sort_keys=True).encode()).hexdigest(),
        "selected_tensor_count": len(selected["weight_map"]), "selected_shards": manifest,
    })
    print(json.dumps({"shards": len(manifest), "tensors": len(selected["weight_map"])}))


def weights() -> None:
    base = f"{BASE}/model"
    manifest = json.loads(PROJECT.read_text(f"{base}/manifest.json"))
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("verified metadata manifest is missing or empty; run metadata first")
    stable_api = PROJECT.resolve(f"{base}/api_metadata.json")
    if stable_api.is_file():
        api = json.loads(PROJECT.read_text(f"{base}/api_metadata.json"))
    else:
        receipts = sorted(PROJECT.resolve(base).glob("metadata_receipt_*.json"))
        if not receipts:
            raise FileNotFoundError("pinned metadata receipt is missing; run metadata first")
        receipt = json.loads(receipts[-1].read_text(encoding="utf-8"))
        api = json.loads(PROJECT.read_text(receipt["api_metadata_path"]))
        if hashlib.sha256(json.dumps(api, sort_keys=True).encode()).hexdigest() != receipt["api_metadata_sha256"]:
            raise ValueError("retained API metadata differs from its pinned receipt")
    if api.get("sha") != REVISION:
        raise ValueError("retained API metadata is not pinned to the requested revision")
    original = json.loads(PROJECT.read_text(f"{base}/original.index.json"))
    expected_manifest = model_manifest(api, original)
    if manifest != expected_manifest:
        raise ValueError("retained manifest differs from pinned API/index; rerun metadata")
    essential = ("config.json", "generation_config.json", "preprocessor_config.json", "tokenizer.json",
                 "tokenizer_config.json", "chat_template.json", "added_tokens.json", "special_tokens_map.json", "merges.txt", "vocab.json")
    records_by_name = {row.get("rfilename"): row for row in api.get("siblings", [])}
    file_manifest = []
    for name in essential:
        info = records_by_name.get(name)
        if info is None:
            if name in ("config.json", "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json"):
                raise ValueError(f"required pinned model metadata file missing from API: {name}")
            continue
        lfs = info.get("lfs") or {}
        size = info.get("size")
        digest = lfs.get("sha256") or lfs.get("oid", "").removeprefix("sha256:")
        blob_id = info.get("blobId")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError(f"invalid API size for metadata file {name}")
        if digest and re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            file_manifest.append({"filename": name, "size": size, "sha256": digest, "hash_type": "sha256"})
        elif isinstance(blob_id, str) and re.fullmatch(r"[0-9a-f]{40}", blob_id):
            file_manifest.append({"filename": name, "size": size, "blob_id": blob_id, "hash_type": "git-blob-sha1"})
        else:
            raise ValueError(f"API lacks usable LFS SHA256 or Git blob SHA1 for metadata file {name}")
    file_manifest.extend(manifest)
    total = sum(row["size"] for row in file_manifest)
    free = shutil.disk_usage(PROJECT.resolve(BASE)).free
    if free - total < 8 * 1024**3:
        raise OSError(f"insufficient disk headroom: {free} free, {total} download bytes, require 8 GiB remaining")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"User-Agent": "CCTV-joint-av-spike/1.0"}
    results = []
    for row in file_manifest:
        name, expected_size = row["filename"], row["size"]
        rel = f"{base}/{name}"
        target = PROJECT.resolve(rel)
        if target.exists():
            if target.stat().st_size != expected_size:
                raise ValueError(f"existing shard differs from pinned manifest: {rel}")
            if row.get("hash_type", "sha256") == "sha256" and _file_sha256(target) != row["sha256"]:
                raise ValueError(f"existing shard SHA256 differs from pinned manifest: {rel}")
            if row.get("hash_type") == "git-blob-sha1" and _git_blob_sha1(target.read_bytes()) != row["blob_id"]:
                raise ValueError(f"existing metadata file Git blob differs from pinned API: {rel}")
            results.append({**row, "reused": True})
            continue
        url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{name}"
        deadline = time.monotonic() + 1800
        expected_digest = row.get("sha256")
        expected_blob = row.get("blob_id")
        hash_type = row.get("hash_type", "sha256")
        def fetch(staged: Path, source=url, limit=expected_size, due=deadline, filename=name,
                  pinned_sha=expected_digest, pinned_blob=expected_blob, digest_type=hash_type):
            digest = hashlib.sha256()
            blob = hashlib.sha1(b"blob " + str(limit).encode("ascii") + b"\0")
            count = 0
            chunk_bytes = 64 * 1024 * 1024
            with staged.open("xb") as output:
                for start in range(0, limit, chunk_bytes):
                    if time.monotonic() > due:
                        raise TimeoutError(f"download exceeded 1800 seconds: {filename}")
                    end = min(start + chunk_bytes, limit) - 1
                    request = urllib.request.Request(source, headers={**headers, "Range": f"bytes={start}-{end}"})
                    with opener.open(request, timeout=90) as response:
                        expected_range = f"bytes {start}-{end}/{limit}"
                        chunk_size = end - start + 1
                        content_range = response.headers.get("Content-Range")
                        valid_partial = response.status == 206 and content_range == expected_range
                        if not valid_partial:
                            raise ValueError(f"invalid range response for {filename}: status={response.status}, Content-Range={content_range!r}, expected={expected_range!r}")
                        if response.headers.get("Content-Length") not in (None, str(chunk_size)):
                            raise ValueError(f"invalid ranged Content-Length for {filename}")
                        received = 0
                        while block := response.read(1024 * 1024):
                            received += len(block)
                            if received > chunk_size:
                                raise ValueError(f"range body exceeds requested interval for {filename}")
                            digest.update(block)
                            blob.update(block)
                            output.write(block)
                        if received != chunk_size:
                            raise ValueError(f"short ranged body for {filename}: {received} != {chunk_size}")
                        count += received
            if count != limit:
                raise ValueError(f"download failed pinned size verification: {filename}")
            if digest_type == "sha256" and digest.hexdigest() != pinned_sha:
                raise ValueError(f"download failed pinned LFS SHA256 verification: {filename}")
            if digest_type == "git-blob-sha1" and blob.hexdigest() != pinned_blob:
                raise ValueError(f"download failed pinned Git blob SHA1 verification: {filename}")
            return digest.hexdigest(), blob.hexdigest()
        partial_rel = f"{base}/downloads/{name}.{uuid.uuid4().hex}.partial"
        PROJECT.make_dir(f"{base}/downloads")
        PROJECT.write_with(partial_rel, fetch)
        partial_path = PROJECT.resolve(partial_rel)
        if row.get("hash_type", "sha256") == "sha256" and _file_sha256(partial_path) != row["sha256"]:
            raise ValueError(f"download failed pinned LFS SHA256 verification: {name}; partial retained at {partial_rel}")
        if row.get("hash_type") == "git-blob-sha1" and _git_blob_sha1(partial_path.read_bytes()) != row["blob_id"]:
            raise ValueError(f"download failed pinned Git blob SHA1 verification: {name}; partial retained at {partial_rel}")
        if target.exists():
            raise FileExistsError(f"refusing to overwrite shard path during promotion: {rel}")
        os.replace(partial_path, target)
        results.append({**row, "reused": False, "observed_sha256": _file_sha256(target)})
    receipt = f"{base}/weights_receipt_{datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    _write(receipt, {"model_id": MODEL_ID, "revision": REVISION, "files": results})
    print(json.dumps({"downloaded_or_verified": len(results), "bytes": total}))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_blob_sha1(payload: bytes) -> str:
    prefix = b"blob " + str(len(payload)).encode("ascii") + b"\0"
    return hashlib.sha1(prefix + payload).hexdigest()


def _isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    cache = PROJECT.make_dir(f"{BASE}/cache/pip")
    hf_cache = PROJECT.make_dir(f"{BASE}/cache/huggingface")
    xdg_cache = PROJECT.make_dir(f"{BASE}/cache/xdg")
    temp = PROJECT.make_dir(f"{BASE}/temp")
    env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_CACHE_DIR": str(cache), "TMP": str(temp),
                "TEMP": str(temp), "TMPDIR": str(temp), "PYTHONNOUSERSITE": "1",
                "HF_HOME": str(hf_cache), "XDG_CACHE_HOME": str(xdg_cache),
                "TORCH_HOME": str(PROJECT.make_dir(f"{BASE}/cache/torch")),
                "CUDA_CACHE_PATH": str(PROJECT.make_dir(f"{BASE}/cache/cuda")),
                "PYTHONPYCACHEPREFIX": str(PROJECT.make_dir(f"{BASE}/cache/pycache"))})
    return env


def _verify_owned_incomplete_env(target: Path) -> None:
    cfg = target / "pyvenv.cfg"
    contents = cfg.read_text(encoding="utf-8") if cfg.is_file() else ""
    expected = f"command = {sys.executable} -m venv {target}"
    if expected not in contents or "include-system-site-packages = false" not in contents:
        raise RuntimeError("existing environment is not the expected isolated spike-owned venv")
    receipt_path = PROJECT.resolve(f"{BASE}/parent_install_20261111T071318Z/install_result.json")
    log_candidates = (
        PROJECT.resolve(f"{BASE}/parent_install_20261111T071318Z/retained_install_plan.log"),
        PROJECT.resolve(f"{BASE}/install_plan.log"),
    )
    log_path = next((path for path in log_candidates if path.is_file()), None)
    if not receipt_path.is_file() or log_path is None:
        raise RuntimeError("--resume-owned-env requires the retained parent install receipt and log")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("phase") != "install" or receipt.get("returncode") != 1 or "install" not in receipt.get("command", []):
        raise RuntimeError("parent receipt does not prove the failed install attempt")
    if "ResolutionImpossible" not in log_path.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError("parent log does not prove a dependency-resolution-only failure")
    site = target / "Lib" / "site-packages"
    installed = set()
    for dist_info in site.glob("*.dist-info"):
        metadata_file = dist_info / "METADATA"
        if metadata_file.is_file():
            installed.add(Parser().parsestr(metadata_file.read_text(encoding="utf-8", errors="replace")).get("Name", "").replace("_", "-").lower())
    if not installed or not installed <= {"pip", "setuptools"}:
        raise RuntimeError(f"existing environment has unexpected installed packages: {sorted(installed)}")


def install(resume_owned_env: bool = False) -> None:
    target = PROJECT.resolve(f"{BASE}/env")
    if (target / "pyvenv.cfg").is_file():
        if not resume_owned_env:
            raise FileExistsError(f"refusing to reuse this environment without --resume-owned-env: {target}")
        _verify_owned_incomplete_env(target)
    elif resume_owned_env:
        raise FileNotFoundError("--resume-owned-env requested but no existing spike environment exists")
    disk_before = shutil.disk_usage(PROJECT.resolve(BASE)).free
    if disk_before < 20 * 1024**3:
        raise OSError(f"need 20 GiB free disk before isolated installation (12 GiB budget + 8 GiB reserve); saw {disk_before}")
    env = _isolated_env()
    for filename, expected in NATIVE_WHEELS.values():
        wheel = PROJECT.resolve(f"{WHEEL_DIR}/{filename}")
        if not wheel.is_file() or _file_sha256(wheel) != expected:
            raise ValueError(f"reused native wheel failed pinned SHA256: {filename}")
    source = Path(sys.executable).resolve()
    if not (target / "pyvenv.cfg").is_file():
        result = subprocess.run([str(source), "-m", "venv", str(target)], check=False, env=env, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"venv creation failed: {result.stderr[-2000:]}")
    python = target / "Scripts" / "python.exe"
    args = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:",
            "--index-url", "https://pypi.org/simple",
            "--report", str(PROJECT.resolve(f"{BASE}/install_report.json"))]
    for filename, expected in NATIVE_WHEELS.values():
        wheel = PROJECT.resolve(f"{WHEEL_DIR}/{filename}")
        if not wheel.is_file() or _file_sha256(wheel) != expected:
            raise ValueError(f"reused native wheel failed pinned SHA256: {filename}")
        args.append(str(wheel))
    args.extend([
        "transformers==5.22.2", "bitsandbytes==0.50.2", "accelerate==1.13.0",
        "av==17.0.0", "safetensors==0.8.0", "numpy==2.4.6", "pillow==13.0.0",
        "scipy==1.17.1", "psutil==7.2.2", "pydantic==2.14.3",
    ])
    plan_stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    args[args.index("--report") + 1] = str(PROJECT.resolve(f"{BASE}/install_plan_{plan_stamp}.json"))
    plan_path = PROJECT.resolve(f"{BASE}/install_plan_{plan_stamp}.json")
    plan_args = args.copy()
    plan_args[plan_args.index("--report") + 1] = str(plan_path)
    plan_args.insert(plan_args.index("install") + 1, "--dry-run")
    plan = subprocess.run(plan_args, check=False, env=env, capture_output=True, text=True, timeout=900)
    if plan.returncode:
        PROJECT.write_text(f"{BASE}/install_plan.log", f"RETURN_CODE: {plan.returncode}\nSTDOUT:\n{plan.stdout}\nSTDERR:\n{plan.stderr}\n")
        raise RuntimeError(f"pip dependency resolution failed; see {BASE}/install_plan.log")
    plan_doc = json.loads(plan_path.read_text(encoding="utf-8"))
    rows = []
    for item in plan_doc.get("install", []):
        meta = item["metadata"]
        info = item["download_info"]
        url = info["url"]
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            wheel_path = Path(urllib.request.url2pathname(parsed.path)).resolve()
            if not any(wheel_path == PROJECT.resolve(f"{WHEEL_DIR}/{filename}") for filename, _ in NATIVE_WHEELS.values()):
                raise ValueError(f"install plan references unapproved local wheel: {url}")
            digest = _file_sha256(wheel_path)
        elif parsed.scheme == "https" and parsed.hostname in ("pypi.org", "files.pythonhosted.org", "download-r2.pytorch.org"):
            digest = info.get("archive_info", {}).get("hashes", {}).get("sha256")
        else:
            raise ValueError(f"install plan references unapproved source: {url}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ValueError(f"install plan lacks SHA256: {meta['name']}=={meta['version']}")
        rows.append({"name": meta["name"], "version": meta["version"], "url": url, "sha256": digest})
    if not rows:
        raise ValueError("pip returned an empty install plan")
    lock = "\n".join(f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}" for row in rows) + "\n"
    lock_rel = f"{BASE}/requirements-hashed_{plan_stamp}.txt"
    PROJECT.write_text(lock_rel, lock)
    wheelhouse_rel = f"{BASE}/wheelhouse_{plan_stamp}"
    PROJECT.make_dir(wheelhouse_rel)
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for wheelname, _ in NATIVE_WHEELS.values():
        source = PROJECT.resolve(f"{WHEEL_DIR}/{wheelname}")
        destination_rel = f"{wheelhouse_rel}/{wheelname}"
        if not PROJECT.resolve(destination_rel).exists():
            def copy_wheel(staged: Path, src=source):
                shutil.copyfile(src, staged)
            PROJECT.write_with(destination_rel, copy_wheel)
    for row in rows:
        parsed = urllib.parse.urlparse(row["url"])
        if parsed.scheme == "file":
            source = Path(urllib.request.url2pathname(parsed.path)).resolve()
            filename = source.name
            dest = PROJECT.resolve(f"{wheelhouse_rel}/{filename}")
            if not dest.exists():
                PROJECT.write_with(f"{wheelhouse_rel}/{filename}", lambda staged, src=source: shutil.copyfile(src, staged))
        else:
            filename = Path(parsed.path).name
            dest_rel = f"{wheelhouse_rel}/{filename}"
            dest = PROJECT.resolve(dest_rel)
            if dest.exists():
                if _file_sha256(dest) != row["sha256"]:
                    raise ValueError(f"existing wheelhouse file hash mismatch: {filename}")
                continue
            unique_rel = f"{wheelhouse_rel}/{filename}.{uuid.uuid4().hex}.partial"
            def fetch_wheel(staged: Path, source=row["url"], expected=row["sha256"]):
                digest = hashlib.sha256()
                with direct_opener.open(urllib.request.Request(source, headers={"User-Agent": "CCTV-joint-av-spike/1.0"}), timeout=60) as response, staged.open("xb") as output:
                    for block in iter(lambda: response.read(1024 * 1024), b""):
                        output.write(block)
                        digest.update(block)
            PROJECT.write_with(unique_rel, fetch_wheel)
            if _file_sha256(PROJECT.resolve(unique_rel)) != row["sha256"]:
                raise ValueError(f"downloaded wheel hash mismatch: {filename}")
            os.replace(PROJECT.resolve(unique_rel), dest)
    install_args = [str(python), "-m", "pip", "install", "--no-index", "--find-links", str(PROJECT.resolve(wheelhouse_rel)),
                    "--require-hashes", "--only-binary=:all:", "--report", str(PROJECT.resolve(f"{BASE}/install_report_{plan_stamp}.json")),
                    "-r", str(PROJECT.resolve(lock_rel))]
    cp = subprocess.run(install_args, check=False, env=env, capture_output=True, text=True, timeout=1800)
    log_rel = f"{BASE}/install_{plan_stamp}.log"
    PROJECT.write_text(log_rel, f"COMMAND: {json.dumps(install_args)}\nRETURN_CODE: {cp.returncode}\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}\n")
    if cp.returncode:
        raise RuntimeError(f"isolated pip install failed; see {log_rel}")
    check = subprocess.run([str(python), "-m", "pip", "check"], check=False, env=env, capture_output=True, text=True, timeout=120)
    check_rel = f"{BASE}/pip_check_{plan_stamp}.log"
    PROJECT.write_text(check_rel, f"RETURN_CODE: {check.returncode}\nSTDOUT:\n{check.stdout}\nSTDERR:\n{check.stderr}\n")
    if check.returncode:
        raise RuntimeError(f"isolated pip check failed; see {check_rel}")
    disk_after = shutil.disk_usage(PROJECT.resolve(BASE)).free
    if disk_after < 8 * 1024**3:
        raise OSError(f"installation left less than 8 GiB free disk: {disk_after}")
    _write(f"{BASE}/install_receipt_{datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json",
           {"python": str(python), "versions": {"torch": "2.11.0+cu130", "transformers": "5.22.2", "bitsandbytes": "0.50.2", "accelerate": "1.13.0", "av": "17.0.0"},
            "native_wheels": [{"filename": name, "sha256": digest} for name, digest in (entry for entry in NATIVE_WHEELS.values())],
            "pip_report": f"{BASE}/install_report_{plan_stamp}.json", "log": log_rel, "requirements_lock": lock_rel,
            "disk_free_before_bytes": disk_before, "disk_free_after_bytes": disk_after})


def probe() -> None:
    python = PROJECT.resolve(f"{BASE}/env/Scripts/python.exe")
    if not python.is_file():
        raise FileNotFoundError("isolated environment is missing; run install first")
    script = """import json,torch,bitsandbytes as bnb,transformers,accelerate,av;\nassert torch.__version__=='2.11.0+cu130' and transformers.__version__=='5.22.2' and bnb.__version__=='0.50.2' and av.__version__=='17.0.0';\nassert torch.cuda.is_available() and torch.cuda.device_count()>0;\nd=torch.device('cuda:0'); x=torch.randn((64,64),device=d,dtype=torch.bfloat16);\nassert torch.isfinite(x@x).all();\nq=bnb.nn.Linear4bit(64,64,bias=False,compute_dtype=torch.bfloat16,quant_type='nf4',compress_statistics=True).to(d);\ny=q(x); assert y.shape==(64,64) and torch.isfinite(y).all();\nz=bnb.functional.dequantize_4bit(q.weight.data,q.weight.quant_state); assert z.shape==(64,64) and torch.isfinite(z).all();\nprint(json.dumps({'cuda':torch.cuda.get_device_name(0),'torch':torch.__version__,'transformers':transformers.__version__,'bitsandbytes':bnb.__version__,'accelerate':accelerate.__version__,'av':av.__version__,'bf16_matmul':True,'nf4_output_shape':list(y.shape),'dequantized_shape':list(z.shape)}))"""
    cp = subprocess.run([str(python), "-c", script], check=False, env=_isolated_env(), capture_output=True, text=True, timeout=180)
    receipt = {"returncode": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr, "command": [str(python), "-c", script]}
    rel = f"{BASE}/probe_receipt_{datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    _write(rel, receipt)
    if cp.returncode:
        raise RuntimeError(f"CUDA/NF4 probe failed; see {rel}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinned local Thinker model setup")
    parser.add_argument("phase", choices=("metadata", "install", "weights", "probe", "inspect"))
    parser.add_argument("--resume-owned-env", action="store_true", help="resume only the parent-receipted incomplete spike env")
    args = parser.parse_args()
    if args.phase == "metadata":
        metadata()
    elif args.phase == "weights":
        weights()
    elif args.phase == "inspect":
        print(json.dumps({"model_id": MODEL_ID, "revision": REVISION, "base": BASE,
                          "manifest_present": PROJECT.resolve(f"{BASE}/model/manifest.json").is_file()}, indent=2))
    elif args.phase == "install":
        install(resume_owned_env=args.resume_owned_env)
    elif args.phase == "probe":
        probe()


if __name__ == "__main__":
    main()
