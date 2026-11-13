from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
BASE = "spikes/22_minimax_h3_workflows/_work/local_runtime"
COMFY_COMMIT = "2c75dc1b379a59e0880619d029607af5169a62ef"
COMFY_VERSION = "0.44.0"
EASY_COMMIT = "4b496eedeff944b2f1266c8c92df1b6c89758de0"
EASY_SOURCE = f"spikes/22_minimax_h3_workflows/_work/easy_source/{EASY_COMMIT}"
RUN_ID = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
EXPECTED_TEST_SHA = "a27d7f2a4d56f144f79c4efbb1740ff59a68e1610b9010f5dc764cb7aa3d0a2c"
WHEELS = {
    "torch": {"version": "2.11.0+cu130", "filename": "torch-2.11.0+cu130-cp311-cp311-win_amd64.whl", "url": "https://download-r2.pytorch.org/whl/cu130/torch-2.11.0%2Bcu130-cp311-cp311-win_amd64.whl", "sha256": "a1ff66c0ad21bf48c3187e84a08a6895d48e9bae435e27811b0b65f36bef4555"},
    "torchvision": {"version": "0.26.0+cu130", "filename": "torchvision-0.26.0+cu130-cp311-cp311-win_amd64.whl", "url": "https://download-r2.pytorch.org/whl/cu130/torchvision-0.26.0%2Bcu130-cp311-cp311-win_amd64.whl", "sha256": "d019b9d02433515d59ad403d8a6f521da7844c030d6c8003eeec39e15e59f9fa"},
    "torchaudio": {"version": "2.11.0+cu130", "filename": "torchaudio-2.11.0+cu130-cp311-cp311-win_amd64.whl", "url": "https://download-r2.pytorch.org/whl/cu130/torchaudio-2.11.0%2Bcu130-cp311-cp311-win_amd64.whl", "sha256": "04aadf807c27df785c6e3463e70bb1a657ed2d1a16b5578114c9fdf7e8c1ac10"},
}
EASY_FILES = [
    "LICENSE", "README.md", "__init__.py", "h3_latent_upscaler.py", "nodes.py",
    "pyproject.toml", "sampling_strategies.py", "web/minimax_h3_easy_ui.js",
    "workflow/1.MiniMax_H3_Easy.json", "workflow/2.MiniMax_H3_Easy_Selected_Video_Refine.json",
    "workflow/3.MiniMax_H3_Easy_Context_Segments.json", "workflow/4.MiniMax_H3_Easy_Context_Segments_Refine.json",
    "workflow/5.MiniMax_H3_Easy_Context_Segments_Control.json", "workflow/README_WORKFLOWS.md",
]


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(rel: str, value: Any) -> None:
    PROJECT.write_text(rel, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def run_command(args: list[str], *, env: dict[str, str], cwd: Path | None, name: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        PROJECT.write_text(f"{BASE}/logs/{RUN_ID}_{name}.log", f"COMMAND: {json.dumps(args)}\nTIMEOUT: {timeout}s\nSTDOUT:\n{exc.stdout or ''}\nSTDERR:\n{exc.stderr or ''}\n")
        raise RuntimeError(f"{name} exceeded bounded timeout {timeout}s; log retained") from exc
    log = f"COMMAND: {json.dumps(args)}\nRETURN_CODE: {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\n"
    PROJECT.write_text(f"{BASE}/logs/{RUN_ID}_{name}.log", log)
    if result.returncode:
        raise RuntimeError(f"{name} failed with code {result.returncode}; see {BASE}/logs/{RUN_ID}_{name}.log")
    return result


def sanitized_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST"):
        env.pop(key, None)
    env.update({
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_CACHE_DIR": str(PROJECT.make_dir(f"{BASE}/cache/pip")),
        "PYTHONPYCACHEPREFIX": str(PROJECT.make_dir(f"{BASE}/cache/pycache")),
        "HF_HOME": str(PROJECT.make_dir(f"{BASE}/cache/huggingface")),
        "XDG_CACHE_HOME": str(PROJECT.make_dir(f"{BASE}/cache/xdg")),
        "TORCH_HOME": str(PROJECT.make_dir(f"{BASE}/cache/torch")),
        "CUDA_CACHE_PATH": str(PROJECT.make_dir(f"{BASE}/cache/cuda")),
        "TMP": str(PROJECT.make_dir(f"{BASE}/state/temp")),
        "TEMP": str(PROJECT.make_dir(f"{BASE}/state/temp")),
        "TMPDIR": str(PROJECT.make_dir(f"{BASE}/state/temp")),
        "COMFY_USER_DIR": str(PROJECT.make_dir(f"{BASE}/state/user")),
    })
    return env


def download(url: str, rel: str, expected_sha: str | None = None, *, max_seconds: int = 900) -> dict[str, Any]:
    target = PROJECT.resolve(rel)
    if target.exists():
        observed = sha256_path(target)
        if expected_sha and observed != expected_sha:
            raise ValueError(f"Existing download hash mismatch for {rel}; refusing replacement")
        return {"path": rel, "sha256": observed, "reused": True, "bytes": target.stat().st_size}
    def fetch(staged: Path) -> None:
        digest = hashlib.sha256()
        deadline = time.monotonic() + max_seconds
        request = urllib.request.Request(url, headers={"User-Agent": "CCTV-H3-local-staging/1.0"})
        with urllib.request.urlopen(request, timeout=45) as response, staged.open("xb") as output:
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Download exceeded {max_seconds}s: {url}")
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                digest.update(block)
        if expected_sha and digest.hexdigest() != expected_sha:
            raise ValueError(f"Downloaded SHA256 mismatch for {rel}: {digest.hexdigest()}")
    PROJECT.write_with(rel, fetch)
    final_hash = sha256_path(PROJECT.resolve(rel))
    return {"path": rel, "sha256": final_hash, "reused": False, "bytes": PROJECT.resolve(rel).stat().st_size}


def safe_extract(zip_rel: str, dest_rel: str) -> list[dict[str, Any]]:
    archive_path = PROJECT.resolve(zip_rel)
    PROJECT.make_dir(dest_rel)
    base_path = PROJECT.resolve(dest_rel)
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            mode = (info.external_attr >> 16) & 0xFFFF
            if name.startswith("/") or any(part in ("", ".", "..") for part in parts) or (mode & 0o170000) == 0o120000:
                raise ValueError(f"Unsafe archive member rejected: {name!r}")
            if not parts or not parts[0].startswith("ComfyUI-"):
                raise ValueError(f"Unexpected archive root/member: {name!r}")
            relative_parts = parts[1:]
            if len(parts) == 1 and info.is_dir():
                continue
            if len(parts) < 2:
                raise ValueError(f"Unexpected archive member path: {name!r}")
            if info.is_dir():
                PROJECT.make_dir(dest_rel + "/" + "/".join(relative_parts))
                continue
            rel = dest_rel + "/" + "/".join(relative_parts)
            target = PROJECT.resolve(rel)
            try:
                target.relative_to(base_path)
            except ValueError as exc:
                raise ValueError(f"Archive output escaped extraction root: {name!r}") from exc
            if target.exists():
                raise FileExistsError(f"Refusing to overwrite archive output {rel}")
            def extract(staged: Path, member: zipfile.ZipInfo = info) -> None:
                with archive.open(member) as source, staged.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            PROJECT.write_with(rel, extract)
            records.append({"path": rel, "bytes": info.file_size, "sha256": sha256_path(PROJECT.resolve(rel))})
    return records


def verify_extracted(zip_rel: str, dest_rel: str) -> list[dict[str, Any]]:
    """Verify an existing extraction against every pinned archive file before reuse."""
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(PROJECT.resolve(zip_rel)) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            mode = (info.external_attr >> 16) & 0xFFFF
            if name.startswith("/") or any(part in ("", ".", "..") for part in parts) or (mode & 0o170000) == 0o120000:
                raise ValueError(f"Unsafe archive member rejected: {name!r}")
            if not parts or not parts[0].startswith("ComfyUI-"):
                raise ValueError(f"Unexpected archive root/member: {name!r}")
            if info.is_dir():
                continue
            if len(parts) < 2:
                raise ValueError(f"Unexpected archive file: {name!r}")
            rel = dest_rel + "/" + "/".join(parts[1:])
            target = PROJECT.resolve(rel)
            expected = sha256_bytes(archive.read(info))
            if not target.is_file() or target.stat().st_size != info.file_size or sha256_path(target) != expected:
                raise ValueError(f"Existing extracted source differs from pinned archive: {rel}")
            records.append({"path": rel, "bytes": info.file_size, "sha256": expected})
    return records


def local_fingerprints() -> dict[str, Any]:
    result: dict[str, Any] = {"orchestrator_python": {"executable": sys.executable, "version": sys.version}}
    comfy = Path("D:/ComfyUI_3/ComfyUI_windows_portable/ComfyUI")
    tracked = ["main.py", "nodes.py", "requirements.txt", "comfyui_version.py", "extra_model_paths.yaml", "user/default/ComfyUI-Manager/config.ini"]
    result["existing_comfyui"] = {"root": str(comfy), "files": {name: sha256_path(comfy / name) for name in tracked if (comfy / name).is_file()}}
    git = shutil.which("git")
    if git and comfy.is_dir():
        cp = subprocess.run([git, "-C", str(comfy), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20, check=False)
        result["existing_comfyui"]["git_head"] = cp.stdout.strip() if cp.returncode == 0 else "unavailable"
    for label, path in (("shared_orchestrator_env", Path("D:/Work/video_pipeline/.venv")), ("spike21_hunyuan_env", Path("D:/Work/video_pipeline/.claude/worktrees/api-conformance/spikes/21_video_foley/_work/env"))):
        identity: dict[str, Any] = {"root": str(path), "files": {}}
        for leaf in ("pyvenv.cfg", "Scripts/python.exe"):
            candidate = path / leaf
            if candidate.is_file():
                identity["files"][leaf] = sha256_path(candidate)
        metadata_dir = path / "Lib/site-packages"
        if metadata_dir.is_dir():
            identity["distribution_metadata"] = {
                str(file.relative_to(path)).replace("\\", "/"): sha256_path(file)
                for file in sorted(metadata_dir.glob("*.dist-info/METADATA"))
            }
        result[label] = identity
    return result


def copy_easy_source() -> list[dict[str, Any]]:
    snapshot = json.loads(PROJECT.read_text(f"{EASY_SOURCE}/snapshot.json"))
    expected = {entry["path"]: entry["sha256"] for entry in snapshot["files"]}
    destination = f"{BASE}/source/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-Easy"
    records: list[dict[str, Any]] = []
    PROJECT.make_dir(destination)
    for name in EASY_FILES:
        if name not in expected:
            raise ValueError(f"Easy source snapshot lacks expected file hash: {name}")
        source_rel = f"{EASY_SOURCE}/{name}"
        payload = PROJECT.read_bytes(source_rel)
        observed = sha256_bytes(payload)
        if observed != expected[name]:
            raise ValueError(f"Easy snapshot hash mismatch: {name}")
        target_rel = destination + "/" + name
        if PROJECT.resolve(target_rel).exists():
            if sha256_path(PROJECT.resolve(target_rel)) != observed:
                raise ValueError(f"Existing staged Easy file differs from pinned source: {target_rel}")
        else:
            PROJECT.write_bytes(target_rel, payload)
        records.append({"path": target_rel, "source_path": source_rel, "bytes": len(payload), "sha256": observed})
    return records


def parse_exact_requirement(requirements: str, package: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(package)}\s*==\s*([^\s;#]+)", re.IGNORECASE)
    for line in requirements.splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


def stage() -> dict[str, Any]:
    for relative in ("downloads", "source", "env", "state/input", "state/output", "state/temp", "state/user", "state/models", "state/previews", "state/database", "cache", "logs", "receipts"):
        PROJECT.make_dir(f"{BASE}/{relative}")
    for relative in (BASE, f"{BASE}/source", f"{BASE}/env", f"{BASE}/state"):
        PROJECT.resolve(relative)
    disk = shutil.disk_usage(PROJECT.resolve(BASE))
    if disk.free < 20 * 1024**3:
        raise OSError(f"Insufficient local disk headroom for staging: {disk.free} bytes free; no downloads started")
    base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    version = subprocess.run([str(base_python), "--version"], capture_output=True, text=True, timeout=20, check=True).stdout.strip()
    if version != "Python 3.11.9":
        raise RuntimeError(f"Required base interpreter Python 3.11.9; observed {version} at {base_python}")
    env = sanitized_env()
    baseline_rel = f"{BASE}/receipts/external_before.json"
    before = json.loads(PROJECT.read_text(baseline_rel)) if PROJECT.resolve(baseline_rel).exists() else local_fingerprints()
    if not PROJECT.resolve(baseline_rel).exists():
        write_json(baseline_rel, before)
    elif before != local_fingerprints():
        raise RuntimeError("External environment/source fingerprint differs from preserved pre-mutation baseline")
    meta_rel = f"{BASE}/public_metadata.json"
    metadata = json.loads(PROJECT.read_text(meta_rel))
    archive_rel = f"{BASE}/downloads/comfyui-{COMFY_COMMIT}.zip"
    archive_url = f"https://github.com/Comfy-Org/ComfyUI/archive/{COMFY_COMMIT}.zip"
    archive_receipt = download(archive_url, archive_rel)
    source_receipt_rel = f"{BASE}/receipts/source_receipt.json"
    if not PROJECT.resolve(f"{BASE}/source/ComfyUI/main.py").exists():
        extracted = safe_extract(archive_rel, f"{BASE}/source/ComfyUI")
    else:
        extracted = verify_extracted(archive_rel, f"{BASE}/source/ComfyUI")
    source_receipt = {"url": archive_url, "commit": COMFY_COMMIT, "version": COMFY_VERSION, "archive": archive_receipt, "files": extracted}
    if PROJECT.resolve(source_receipt_rel).exists():
        old_source_receipt = json.loads(PROJECT.read_text(source_receipt_rel))
        if {k: v for k, v in old_source_receipt.items() if k != "archive"} != {k: v for k, v in source_receipt.items() if k != "archive"} or old_source_receipt["archive"]["sha256"] != source_receipt["archive"]["sha256"]:
            raise ValueError("Pinned Comfy source receipt mismatch; refusing to continue")
    else:
        write_json(source_receipt_rel, source_receipt)
    comfy_root_rel = f"{BASE}/source/ComfyUI"
    main_path = PROJECT.resolve(f"{comfy_root_rel}/main.py")
    if not main_path.is_file():
        raise FileNotFoundError("Pinned ComfyUI archive extraction is incomplete")
    easy_records = copy_easy_source()
    requirements_rel = f"{comfy_root_rel}/requirements.txt"
    requirements = PROJECT.read_text(requirements_rel)
    exact_packages = ["comfyui-frontend-package", "comfyui-workflow-templates", "comfyui-embedded-docs"]
    exact_versions: dict[str, str] = {}
    for package in exact_packages:
        value = parse_exact_requirement(requirements, package)
        if value is None:
            raise ValueError(f"Pinned Comfy requirements do not specify exact {package}; refusing to infer")
        exact_versions[package] = value
    constraints = [f"{key}=={value}" for key, value in exact_versions.items()]
    constraints.extend(["torch==2.11.0+cu130", "torchvision==0.26.0+cu130", "torchaudio==2.11.0+cu130", "av==17.0.0", "comfy-aimdo==0.6.0", "comfy-kitchen==0.2.40"])
    constraint_rel = f"{BASE}/receipts/constraints.txt"
    PROJECT.write_text(constraint_rel, "\n".join(constraints) + "\n")
    easy_requirements_rel = f"{BASE}/receipts/easy-requirements.txt"
    PROJECT.write_text(easy_requirements_rel, "requests\npsutil\n")
    env_python = PROJECT.resolve(f"{BASE}/env/Scripts/python.exe")
    if not env_python.exists():
        if any(PROJECT.resolve(f"{BASE}/env").iterdir()):
            raise FileExistsError("Partial isolated env exists without Scripts/python.exe; refusing overwrite")
        run_command([str(base_python), "-m", "venv", str(PROJECT.resolve(f"{BASE}/env"))], env=env, cwd=None, name="venv_create", timeout=120)
    env_python = PROJECT.resolve(f"{BASE}/env/Scripts/python.exe")
    if not env_python.is_file():
        raise RuntimeError("Isolated virtual environment creation did not produce Scripts/python.exe")
    cfg = PROJECT.read_text(f"{BASE}/env/pyvenv.cfg")
    cfg_values = dict(line.split(" = ", 1) for line in cfg.splitlines() if " = " in line)
    if cfg_values.get("version") != "3.11.9" or cfg_values.get("include-system-site-packages") != "false":
        raise RuntimeError("Existing isolated venv identity does not match Python 3.11.9/no inherited packages")
    if Path(cfg_values.get("executable", "")).resolve() != base_python:
        raise RuntimeError("Existing isolated venv base executable differs from the pinned base interpreter")
    optimizer_path = f"{BASE}/source/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-Easy/prompt_optimizer.json"
    if PROJECT.resolve(optimizer_path).exists():
        raise RuntimeError("Unexpected Easy optimizer config found; model-free staging requires the pinned empty defaults")
    pip_base = [str(env_python), "-m", "pip", "--disable-pip-version-check", "--retries", "1", "--timeout", "45"]
    run_command(pip_base + ["install", "--only-binary=:all:", "--upgrade", "pip"], env=env, cwd=None, name="pip_bootstrap", timeout=300)
    wheel_receipts: list[dict[str, Any]] = []
    wheel_paths: list[str] = []
    metadata_wheels = metadata["cuda130_windows"]
    for package, spec in WHEELS.items():
        official = next((entry for entry in metadata_wheels[package] if entry["filename"] == spec["filename"]), None)
        if official is None or spec["sha256"] not in official["url"]:
            raise ValueError(f"Pinned public metadata does not contain expected official {package} wheel/hash")
        rel = f"{BASE}/downloads/wheels/{spec['filename']}"
        PROJECT.make_dir(f"{BASE}/downloads/wheels")
        receipt = download(spec["url"], rel, spec["sha256"])
        wheel_receipts.append({**receipt, "package": package, "version": spec["version"], "url": spec["url"]})
        wheel_paths.append(str(PROJECT.resolve(rel)))
    run_command(pip_base + ["install", "--only-binary=:all:", "--no-deps", *wheel_paths], env=env, cwd=None, name="install_torch_wheels", timeout=600)
    report_rel = f"{BASE}/receipts/pip-install-report_{RUN_ID}.json"
    run_command(pip_base + ["install", "--only-binary=:all:", "--report", str(PROJECT.resolve(report_rel)), "-c", str(PROJECT.resolve(constraint_rel)), "-r", str(PROJECT.resolve(requirements_rel)), "-r", str(PROJECT.resolve(easy_requirements_rel))], env=env, cwd=None, name="install_comfy_requirements", timeout=900)
    easy_records_all = easy_records
    easy_root = f"{BASE}/source/ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-Easy"
    if not PROJECT.resolve(easy_root).is_dir():
        raise RuntimeError("Easy custom-node source was not staged")
    run_command(pip_base + ["check"], env=env, cwd=None, name="pip_check", timeout=180)
    freeze = run_command(pip_base + ["freeze", "--all"], env=env, cwd=None, name="pip_freeze", timeout=180)
    freeze_rel = f"{BASE}/receipts/freeze.txt"
    PROJECT.write_text(freeze_rel, freeze.stdout)
    comfy_version = run_command([str(env_python), str(main_path), "--help"], env=env, cwd=PROJECT.resolve(comfy_root_rel), name="comfy_cli_help", timeout=180)
    help_text = comfy_version.stdout + comfy_version.stderr
    required_flags = ["--cpu", "--disable-all-custom-nodes", "--listen", "--port", "--input-directory", "--output-directory", "--temp-directory", "--user-directory", "--database-url", "--offline", "--models-directory", "--previews-directory", "--base-directory"]
    missing_flags = [flag for flag in required_flags if flag not in help_text]
    if missing_flags:
        raise RuntimeError(f"Pinned Comfy CLI lacks required local-only flags: {missing_flags}")
    return {
        "archive": source_receipt,
        "easy_source": {"commit": EASY_COMMIT, "files": easy_records_all},
        "base_python": {"path": str(base_python), "version": version},
        "torch_wheels": wheel_receipts,
        "requirements_pins": exact_versions,
        "constraint_path": constraint_rel,
        "install_report_path": report_rel,
        "install_report_paths": [str(path.relative_to(ROOT)).replace("\\", "/") for path in sorted(PROJECT.resolve(f"{BASE}/receipts").glob("pip-install-report*.json"))],
        "optimizer_config_file_absent": True,
        "freeze_path": freeze_rel,
        "cli_help": {"path": f"{BASE}/logs/{RUN_ID}_comfy_cli_help.log", "flags_checked": required_flags},
        "immutable_before": before,
        "immutable_before_receipt": baseline_rel,
        "metadata_sha256": sha256_path(PROJECT.resolve(meta_rel)),
        "test_input_sha256_expected": EXPECTED_TEST_SHA,
    }


def http_json(url: str, timeout: int = 15) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def boot_probe(stage_receipt: dict[str, Any], env: dict[str, str], *, easy: bool) -> dict[str, Any]:
    comfy_root = PROJECT.resolve(f"{BASE}/source/ComfyUI")
    python = PROJECT.resolve(f"{BASE}/env/Scripts/python.exe")
    port = 8295 if not easy else 8296
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"Required explicit local port {port} is unavailable; no alternate port selected") from exc
    args = [str(python), str(comfy_root / "main.py"), "--cpu", "--offline", "--disable-all-custom-nodes", "--listen", "127.0.0.1", "--port", str(port), "--base-directory", str(comfy_root), "--models-directory", str(PROJECT.resolve(f"{BASE}/state/models")), "--previews-directory", str(PROJECT.resolve(f"{BASE}/state/previews")), "--input-directory", str(PROJECT.resolve(f"{BASE}/state/input")), "--output-directory", str(PROJECT.resolve(f"{BASE}/state/output")), "--temp-directory", str(PROJECT.resolve(f"{BASE}/state/temp")), "--user-directory", str(PROJECT.resolve(f"{BASE}/state/user")), "--database-url", "sqlite:///" + str(PROJECT.resolve(f"{BASE}/state/database/comfy.db")).replace("\\", "/")]
    if easy:
        args.extend(["--whitelist-custom-nodes", "ComfyUI-MiniMaxH3-Easy"])
    log_rel = f"{BASE}/logs/{RUN_ID}_comfy_{'easy' if easy else 'native'}.log"
    PROJECT.write_bytes(log_rel, b"")
    log_path = PROJECT.resolve(log_rel)
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    status: dict[str, Any] = {"mode": "easy_whitelist" if easy else "native_only", "command": args, "port": port, "started_utc": datetime.now(UTC).isoformat()}
    with log_path.open("ab") as log_stream:
        process = subprocess.Popen(args, cwd=comfy_root, env=env, stdout=log_stream, stderr=subprocess.STDOUT)
        status["pid"] = process.pid
        try:
            until = started + 180
            while time.monotonic() < until:
                if process.poll() is not None:
                    raise RuntimeError(f"Comfy {status['mode']} process exited early ({process.returncode}); see {log_rel}")
                try:
                    stats = http_json(f"http://127.0.0.1:{port}/system_stats", timeout=3)
                    break
                except (OSError, TimeoutError, json.JSONDecodeError):
                    time.sleep(1)
            else:
                raise TimeoutError(f"Comfy {status['mode']} startup exceeded 180s; see {log_rel}")
            object_info = http_json(f"http://127.0.0.1:{port}/object_info", timeout=30)
            schema_rel = f"{BASE}/receipts/object_info_{'easy' if easy else 'native'}.json"
            stats_rel = f"{BASE}/receipts/system_stats_{'easy' if easy else 'native'}.json"
            write_json(schema_rel, object_info)
            write_json(stats_rel, stats)
            required_native = ["MiniMaxH3ReferenceToVideo", "VAEDecodeAudio", "LoadVideo", "CreateVideo", "SaveVideo"]
            required_easy = ["MiniMaxH3EasyLoader", "MiniMaxH3Easy", "MiniMaxH3EasyMediaBridge", "MiniMaxH3EasyOutput"]
            required = required_native + (required_easy if easy else [])
            missing = [name for name in required if name not in object_info]
            if missing:
                raise RuntimeError(f"Comfy {status['mode']} schema missing node classes: {missing}")
            status.update({"ready_seconds": round(time.monotonic() - started, 3), "returncode_after_stop": None, "schema_path": schema_rel, "stats_path": stats_rel, "node_classes_checked": required, "node_input_schemas": {name: object_info[name].get("input", {}) for name in required}})
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            status["returncode_after_stop"] = process.returncode
            status["stopped_utc"] = datetime.now(UTC).isoformat()
            write_json(f"{BASE}/receipts/boot_{'easy' if easy else 'native'}.json", status)
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage isolated model-free ComfyUI H3 runtime")
    parser.parse_args()
    try:
        # All path roots are authorized before subprocesses/network writes.
        for rel in (BASE, f"{BASE}/downloads", f"{BASE}/source", f"{BASE}/env", f"{BASE}/state", f"{BASE}/cache", f"{BASE}/logs", f"{BASE}/receipts"):
            PROJECT.make_dir(rel)
        PROJECT.resolve(BASE)
        result = stage()
        env = sanitized_env()
        write_json(f"{BASE}/receipts/stage_receipt_{RUN_ID}.json", result)
        native = boot_probe(result, env, easy=False)
        easy = boot_probe(result, env, easy=True)
        result["model_boundary"] = {"weights_downloaded": False, "inference_queued": False, "remote_upload": False, "rights_status": "creator asserts actual territory is outside H3-excluded regions; not independently geo-verified; no model weights or inference in this setup stage", "easy_optimizer_enabled": False, "optimizer_credentials_configured": False}
        result["smoke_checks"] = {"native": native, "easy": easy}
        result["immutable_after"] = local_fingerprints()
        result["immutable_unchanged"] = result["immutable_before"] == result["immutable_after"]
        if not result["immutable_unchanged"]:
            raise RuntimeError("Shared/original environment fingerprint changed during isolated staging")
        result["source_test_input"] = {"path": "spikes/22_minimax_h3_workflows/_work/test_inputs/20261109T165721Z_e1e1bf74/input_silent.mp4", "expected_sha256": EXPECTED_TEST_SHA, "observed_sha256": sha256_path(PROJECT.resolve("spikes/22_minimax_h3_workflows/_work/test_inputs/20261109T165721Z_e1e1bf74/input_silent.mp4"))}
        if result["source_test_input"]["observed_sha256"] != EXPECTED_TEST_SHA:
            raise RuntimeError("Prepared source input hash changed from prior verification")
        artifacts = [result["freeze_path"], result["constraint_path"], result["immutable_before_receipt"], f"{BASE}/receipts/source_receipt.json", *result["install_report_paths"]]
        result["artifact_hashes"] = {path: sha256_path(PROJECT.resolve(path)) for path in artifacts}
        result["status"] = "staged_model_free_cpu_smokes_complete"
        result["created_utc"] = datetime.now(UTC).isoformat()
        result["setup_script"] = {"path": "spikes/22_minimax_h3_workflows/setup_local.py", "sha256": sha256_path(PROJECT.resolve("spikes/22_minimax_h3_workflows/setup_local.py"))}
        write_json(f"{BASE}/receipts/setup_receipt_{RUN_ID}.json", result)
        print(json.dumps({"status": result["status"], "receipt": f"{BASE}/receipts/setup_receipt_{RUN_ID}.json"}, indent=2))
    except Exception as exc:
        failure = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "failed_utc": datetime.now(UTC).isoformat(), "script_sha256": sha256_path(PROJECT.resolve("spikes/22_minimax_h3_workflows/setup_local.py"))}
        write_json(f"{BASE}/receipts/setup_failure_{RUN_ID}.json", failure)
        raise


if __name__ == "__main__":
    main()
