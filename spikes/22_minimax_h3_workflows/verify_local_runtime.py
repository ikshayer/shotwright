"""Independent actual-state verification of the isolated model-free H3 runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

BASE = "spikes/22_minimax_h3_workflows/_work/local_runtime"
COMMIT = "2c75dc1b379a59e0880619d029607af5169a62ef"
INPUT = "spikes/22_minimax_h3_workflows/_work/test_inputs/20261109T165721Z_e1e1bf74/input_silent.mp4"
INPUT_SHA = "a27d7f2a4d56f144f79c4efbb1740ff59a68e1610b9010f5dc764cb7aa3d0a2c"
NATIVE = {"MiniMaxH3ReferenceToVideo", "VAEDecodeAudio", "LoadVideo", "CreateVideo", "SaveVideo"}
EASY = {"MiniMaxH3EasyLoader", "MiniMaxH3Easy", "MiniMaxH3EasyMediaBridge", "MiniMaxH3EasyOutput"}
EXPECTED = {"torch": "2.11.0+cu130", "torchvision": "0.26.0+cu130", "torchaudio": "2.11.0+cu130", "av": "17.0.0"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=f"{BASE}/receipts/independent_verification.json")
    parser.add_argument("--port", type=int, default=8294)
    args = parser.parse_args()
    require(1024 <= args.port <= 65535, "invalid local port")
    root = ProjectRoot(ROOT)
    runtime = root.resolve(BASE)
    run_id = uuid.uuid4().hex[:12]
    output = f"{BASE}/receipts/verify-{run_id}"
    root.make_dir(output)
    report_path = root.resolve(args.report)
    require(report_path.is_relative_to(runtime), "report must be inside isolated runtime")
    checks = []
    environment = os.environ.copy()
    for key in list(environment):
        if key.upper() in {"PYTHONPATH", "PYTHONHOME"} or any(word in key.upper() for word in ("API_KEY", "API_TOKEN", "AUTH_TOKEN", "ENDPOINT")):
            environment.pop(key)
    writable = {"TMP": "temp", "TEMP": "temp", "TMPDIR": "temp", "PIP_CACHE_DIR": "pip", "PYTHONPYCACHEPREFIX": "pycache", "HF_HOME": "hf", "XDG_CACHE_HOME": "xdg", "TORCH_HOME": "torch", "CUDA_CACHE_PATH": "cuda", "USERPROFILE": "home", "HOME": "home", "APPDATA": "appdata", "LOCALAPPDATA": "localappdata"}
    for key, subdir in writable.items():
        environment[key] = str(root.make_dir(f"{BASE}/cache/verifier/{subdir}"))
    environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    python = root.resolve(f"{BASE}/env/Scripts/python.exe")
    source = root.resolve(f"{BASE}/source/ComfyUI")

    def check(name, operation):
        try:
            detail = operation()
            checks.append({"name": name, "pass": True, "detail": detail})
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            checks.append({"name": name, "pass": False, "error": f"{type(exc).__name__}: {exc}"})

    def command(arguments: list[str], name: str, timeout: int = 180) -> str:
        try:
            result = subprocess.run(arguments, cwd=source, env=environment, capture_output=True, text=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            for label, content in (("stdout", exc.stdout), ("stderr", exc.stderr)):
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")
                root.write_text(f"{output}/{name}.{label}.txt", content or "")
            raise
        root.write_text(f"{output}/{name}.stdout.txt", result.stdout)
        root.write_text(f"{output}/{name}.stderr.txt", result.stderr)
        require(result.returncode == 0, f"{name} exit {result.returncode}; see retained logs")
        return result.stdout

    check("silent_input_unchanged", lambda: require(digest(root.resolve(INPUT)) == INPUT_SHA, "silent input changed"))

    def containment() -> dict:
        require(runtime.is_dir(), "runtime absent")
        links = []
        for directory, subdirs, files in os.walk(runtime, followlinks=False):
            for name in subdirs + files:
                path = Path(directory) / name
                relative = path.relative_to(ROOT).as_posix()
                resolved = root.resolve(relative)
                require(resolved.is_relative_to(runtime), f"runtime path escapes: {relative}")
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    links.append(relative)
        require(not links, f"runtime symlinks/junctions found: {links[:10]}")
        return {"root": BASE, "environment_writable_paths": {key: environment[key] for key in writable}, "pythonpath_removed": "PYTHONPATH" not in environment, "pythonhome_removed": "PYTHONHOME" not in environment}
    check("contained_runtime_and_verifier_environment", containment)

    def versions() -> dict:
        text = command([str(python), "-B", "-c", "import sys,json,importlib.metadata as m; print(json.dumps({'python':sys.version.split()[0],'prefix':sys.prefix,'base_prefix':sys.base_prefix,'packages':{n:m.version(n) for n in ['torch','torchvision','torchaudio','av']}}))"], "versions")
        value = json.loads(text)
        require(value["python"] == "3.11.9", "expected Python3.11.9")
        require(Path(value["prefix"]).resolve() == python.parents[1], "wrong environment prefix")
        require(value["packages"] == EXPECTED, f"installed pin mismatch: {value['packages']}")
        return value
    check("installed_versions", versions)
    check("pip_check", lambda: command([str(python), "-B", "-m", "pip", "check"], "pip_check"))

    def source_requirements() -> dict:
        script = "import json,importlib.metadata as m; from pathlib import Path; from packaging.requirements import Requirement; rows=[]; "
        script += "\nfor line in Path('requirements.txt').read_text().splitlines():\n line=line.split('#',1)[0].strip()\n if not line: continue\n r=Requirement(line)\n if r.marker and not r.marker.evaluate(): continue\n v=m.version(r.name)\n assert v in r.specifier, (r.name,v,str(r.specifier))\n rows.append({'package':r.name,'installed':v,'required':str(r.specifier)})\nprint(json.dumps(rows))"
        return {"actual_installed_requirements": json.loads(command([str(python), "-B", "-c", script], "source_requirements"))}
    check("pinned_source_requirements_installed", source_requirements)

    def archives() -> list[dict]:
        records = []
        for path in sorted(root.resolve(f"{BASE}/downloads").glob("*.zip")):
            relative = path.relative_to(ROOT).as_posix()
            root.resolve(relative)
            extracted = 0
            with zipfile.ZipFile(path) as archive:
                require(all(member.filename.split("/", 1)[0] == "ComfyUI-" + COMMIT for member in archive.infolist()), "source archive root does not match pinned commit")
                for member in archive.infolist():
                    name = member.filename.replace("\\", "/")
                    require(not name.startswith("/") and ".." not in name.split("/"), "unsafe archive member")
                    require((member.external_attr >> 16) & 0o170000 != 0o120000, "archive symlink")
                    parts = name.split("/", 1)
                    if len(parts) != 2 or not parts[1] or member.is_dir():
                        continue
                    target = root.resolve(f"{BASE}/source/ComfyUI/{parts[1]}")
                    require(target.is_file(), f"missing extracted source: {parts[1]}")
                    require(digest(target) == hashlib.sha256(archive.read(member)).hexdigest(), f"extracted source mismatch: {parts[1]}")
                    extracted += 1
            records.append({"path": relative, "sha256": digest(path), "extracted_files_verified": extracted})
        require(records, "no retained Comfy source ZIP to verify")
        require(root.resolve(f"{BASE}/source/ComfyUI/LICENSE").is_file(), "source licence missing")
        return records
    check("source_archive_and_extracted_bytes", archives)

    def no_weights() -> dict:
        found = []
        extensions = {".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".bin"}
        for base in ("state/models", "source/ComfyUI/models", "state/custom_nodes", "source/ComfyUI/custom_nodes", "downloads"):
            location = root.resolve(f"{BASE}/{base}")
            if location.exists():
                found.extend(p.relative_to(ROOT).as_posix() for p in location.rglob("*") if p.is_file() and p.suffix.lower() in extensions)
        require(not found, f"model/weight-like files found: {found[:20]}")
        return {"weight_files": [], "inference_performed": False, "scope": "model-free staging timestamp only; future model acquisition changes this state"}
    check("no_model_weights", no_weights)

    def receipts() -> dict:
        documents = []
        verified_hashes = []
        for path in sorted(root.resolve(f"{BASE}/receipts").glob("*.json")):
            if path.name.startswith("independent_"):
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            documents.append({"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path), "content": value})

            def visit(item):
                if isinstance(item, dict):
                    if isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str):
                        target = root.resolve(item["path"])
                        require(digest(target) == item["sha256"], f"receipt hash mismatch: {item['path']}")
                        verified_hashes.append(item["path"])
                    for child in item.values():
                        visit(child)
                elif isinstance(item, list):
                    for child in item:
                        visit(child)
            visit(value)
        require(documents, "setup receipts absent")
        combined = json.dumps(documents).lower()
        require(COMMIT in combined, "receipt missing pinned Comfy commit")
        require("before" in combined and "after" in combined, "no pre/post external-environment provenance")
        require("sha256" in combined, "missing receipt hashes")
        require("github.com/comfyanonymous/comfyui" in combined or "github.com/comfy-org/comfyui" in combined or "codeload.github.com/comfyanonymous/comfyui" in combined or "codeload.github.com/comfy-org/comfyui" in combined, "missing official Comfy archive URL provenance")
        return {"documents": documents, "actual_file_hashes_verified": verified_hashes, "limitation": "Receipts are provenance evidence; historical absence of writes cannot be independently reconstructed."}
    check("setup_receipts_and_hashes", receipts)

    def boot() -> dict:
        help_text = command([str(python), "-B", str(source / "main.py"), "--help"], "cli_help")
        flags = ["--cpu", "--listen", "--port", "--base-directory", "--input-directory", "--output-directory", "--temp-directory", "--user-directory", "--disable-all-custom-nodes", "--database-url"]
        for flag in flags:
            require(flag in help_text, f"required isolation flag unavailable: {flag}")
        folders = {name: root.make_dir(f"{BASE}/state/verifier-{run_id}/{name}") for name in ("input", "output", "temp", "user", "base", "previews")}
        folders["base"] = source
        database = root.resolve(f"{BASE}/state/verifier-{run_id}/user/comfy.db")
        arguments = [str(python), "-B", str(source / "main.py"), "--cpu", "--listen", "127.0.0.1", "--port", str(args.port), "--disable-all-custom-nodes", "--base-directory", str(folders["base"]), "--database-url", "sqlite:///" + database.as_posix()]
        for name in ("input", "output", "temp", "user"):
            arguments.extend([f"--{name}-directory", str(folders[name])])
        require("--offline" in help_text, "offline flag unavailable")
        arguments.append("--offline")
        models = root.make_dir(f"{BASE}/state/models")
        for flag, target in (("--models-directory", models), ("--previews-directory", folders["previews"])):
            require(flag in help_text, f"isolation flag unavailable: {flag}")
            arguments.extend([flag, str(target)])
        easy_dir = root.resolve(f"{BASE}/source/ComfyUI/custom_nodes")
        easy_candidates = [p for p in easy_dir.iterdir() if p.is_dir() and p.name == "ComfyUI-MiniMaxH3-Easy"] if easy_dir.exists() else []
        modes = [("native", [])]
        if easy_candidates:
            require("--whitelist-custom-nodes" in help_text, "Easy exists but whitelist flag unavailable")
            modes.append(("easy", ["--whitelist-custom-nodes", *[p.name for p in easy_candidates]]))
        results = {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for mode, extra in modes:
            mode_port = args.port if mode == "native" else args.port + 3
            require(1 <= mode_port <= 65535, "invalid explicit verifier port")
            mode_arguments = list(arguments)
            mode_arguments[mode_arguments.index("--port") + 1] = str(mode_port)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", mode_port))
            process = None
            log_relative = f"{output}/boot_{mode}.log"

            def supervise(log_path, mode=mode, extra=extra, mode_arguments=mode_arguments, mode_port=mode_port):
                nonlocal process
                with log_path.open("wb") as log:
                    process = subprocess.Popen(mode_arguments + extra, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT)
                    root.write_text(f"{output}/boot_{mode}_process.json", json.dumps({"pid": process.pid, "command": mode_arguments + extra, "owned": True}))
                    try:
                        deadline = time.monotonic() + 180
                        schemas = None
                        stats = None
                        while time.monotonic() < deadline:
                            require(process.poll() is None, f"{mode} server exited {process.returncode}")
                            try:
                                with opener.open(f"http://127.0.0.1:{mode_port}/system_stats", timeout=3) as response:
                                    stats = json.load(response)
                                with opener.open(f"http://127.0.0.1:{mode_port}/object_info", timeout=10) as response:
                                    schemas = json.load(response)
                                break
                            except (urllib.error.URLError, TimeoutError):
                                time.sleep(1)
                        require(schemas is not None, f"{mode} startup deadline exceeded")
                        root.write_text(f"{output}/{mode}_system_stats.json", json.dumps(stats, indent=2))
                        root.write_text(f"{output}/{mode}_object_info.json", json.dumps(schemas, indent=2))
                        needed = NATIVE | (EASY if mode == "easy" else set())
                        require(needed <= schemas.keys(), f"missing {mode} nodes: {sorted(needed - schemas.keys())}")
                        results[mode] = {"pid": process.pid, "nodes": {name: schemas[name] for name in sorted(needed)}, "system_stats": stats, "queue_submitted": False}
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=15)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=10)
                        root.write_text(f"{output}/boot_{mode}_exit.json", json.dumps({"pid": process.pid, "returncode": process.returncode, "owned_process_stopped": process.poll() is not None}))
            root.write_with(log_relative, supervise)
        return results
    check("independent_cpu_boot_and_actual_schemas", boot)
    passed = all(item["pass"] for item in checks)
    names = {item["name"]: item["pass"] for item in checks}
    stage = "full_model_free_boot" if passed else "partial" if python.exists() or source.exists() else "none"
    report = {"technical_staging_pass": passed, "stage": stage, "checks": checks, "evidence_directory": output, "no_model_fit_claim": True, "inference_performed": False, "territorial_rights": "user asserted outside excluded territories; separate model-stage validation remains", "pending": ["weights and separately reviewed loader", "actual video-and-text generated-audio inference", "measured RAM/VRAM fit and output quality"], "independent_boot_pass": names.get("independent_cpu_boot_and_actual_schemas", False)}
    root.write_text(args.report, json.dumps(report, indent=2) + "\n")
    print(json.dumps({"technical_staging_pass": passed, "stage": stage, "report": args.report, "failures": [item["name"] for item in checks if not item["pass"]]}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
