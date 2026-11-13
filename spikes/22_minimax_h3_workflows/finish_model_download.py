"""Finish the two missing pinned H3 downloads, without importing a model runtime."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

P = ProjectRoot(ROOT)
SPIKE = "spikes/22_minimax_h3_workflows"
CATALOG = f"{SPIKE}/LOCAL_MODEL_CATALOG.json"
DOCS = f"{SPIKE}/LOCAL_MODEL_DOCUMENTATION.json"
TIMEOUT_SECONDS = 7200


def now() -> str:
    return datetime.now(UTC).isoformat()


def save(relative: str, value: object) -> None:
    P.write_text(relative, json.dumps(value, indent=2, allow_nan=False) + "\n")


def identity(relative: str) -> dict[str, object]:
    path = P.resolve(relative)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return {"path": relative, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def verified(relative: str, expected: dict) -> dict:
    actual = identity(relative)
    if actual["bytes"] != expected["expected_bytes"] or actual["sha256"] != expected["expected_sha256"]:
        raise ValueError(f"Existing/downloaded file fails pinned byte/hash identity: {relative}: {actual}")
    return actual


def environment(run: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        env.pop(key, None)
    for name, leaf in {"HF_HOME": "hf", "HF_HUB_CACHE": "hf/hub", "HF_XET_CACHE": "hf/xet", "HF_ASSETS_CACHE": "hf/assets", "XDG_CACHE_HOME": "cache", "PYTHONPYCACHEPREFIX": "pycache", "TMP": "temp", "TEMP": "temp", "TMPDIR": "temp"}.items():
        env[name] = str(P.make_dir(f"{run}/{leaf}"))
    env.update({"HF_TOKEN_PATH": str(P.resolve(f"{run}/hf/token")), "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1", "HF_HUB_ETAG_TIMEOUT": "45", "HF_HUB_DOWNLOAD_TIMEOUT": "120", "HF_XET_HIGH_PERFORMANCE": "0", "PYTHONUTF8": "1"})
    return env


def event(run: str, **fields: object) -> None:
    value = {"updated_utc": now(), **fields}
    save(f"{run}/progress.json", value)
    P.append_line(f"{run}/events.jsonl", json.dumps(value))
    print(json.dumps(value), flush=True)


def worker(run: str, component: dict) -> None:
    # The supervising process configures all caches before this import.
    from huggingface_hub import hf_hub_download
    role = component["role"]
    stage_rel = f"{run}/staging/{role}"
    stage = P.make_dir(stage_rel)
    expected = P.resolve(f"{stage_rel}/{component['source_filename']}")
    result = Path(hf_hub_download(repo_id=component["repo"], filename=component["source_filename"], revision=component["revision"], token=False, local_dir=str(stage))).resolve()
    if result != expected:
        raise ValueError("HF returned a path other than the exact contained catalog stage path")
    actual = verified(str(result.relative_to(ROOT)).replace("\\", "/"), component)
    if "torch" in sys.modules:
        raise RuntimeError("Unexpected torch import during download-only worker")
    save(f"{run}/worker_result_{role}.json", actual)


def transfer(run: str, component: dict, destination: str) -> dict:
    role = component["role"]
    log_rel = f"{run}/{role}.log"
    stage = P.make_dir(f"{run}/staging/{role}")
    args = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", role, "--run", run]
    event(run, status="downloading", role=role, expected_bytes=component["expected_bytes"], log=log_rel)
    def launch(log_path: Path) -> None:
        with log_path.open("xb") as log:
            process = subprocess.Popen(args, cwd=ROOT, env=environment(run), stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic() - started > TIMEOUT_SECONDS:
                        raise TimeoutError(f"{role} transfer exceeded {TIMEOUT_SECONDS}s")
                    files = [p for p in stage.rglob("*") if p.is_file()]
                    event(run, status="downloading", role=role, pid=process.pid, elapsed_seconds=round(time.monotonic()-started, 1), largest_stage_file_bytes=max((p.stat().st_size for p in files), default=0), file_size_is_not_transfer_progress=True)
                    time.sleep(15)
                if process.returncode:
                    raise RuntimeError(f"{role} HF worker failed ({process.returncode}); see {log_rel}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=15)
    P.write_with(log_rel, launch)
    downloaded = json.loads(P.read_text(f"{run}/worker_result_{role}.json"))
    stage_path = P.resolve(downloaded["path"])
    expected_stage = P.resolve(f"{run}/staging/{role}/{component['source_filename']}")
    if stage_path != expected_stage or P.resolve(destination).exists():
        raise FileExistsError("Promotion path mismatch or final target appeared; refusing replacement")
    # Windows same-volume rename is atomic and fails if the target exists.
    P.write_with(destination, lambda target: stage_path.rename(target))
    actual = verified(destination, component)
    event(run, status="verified", role=role, actual=actual)
    return actual


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker")
    parser.add_argument("--run")
    options = parser.parse_args()
    catalog = json.loads(P.read_text(CATALOG))
    documents = json.loads(P.read_text(DOCS))
    if options.worker:
        worker(options.run, next(c for c in catalog["components"] if c["role"] == options.worker))
        return
    run = f"{catalog['model_root']}/download_finish/{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    P.make_dir(run)
    receipt = {"status": "started", "started_utc": now(), "catalog": identity(CATALOG), "documentation_catalog": identity(DOCS), "components": [], "documentation": [], "transport": {"method": "hf_hub_download", "token": False, "high_performance": False, "per_file_timeout_seconds": TIMEOUT_SECONDS, "huggingface_hub": importlib.metadata.version("huggingface-hub"), "hf_xet": importlib.metadata.version("hf-xet")}, "inference_performed": False, "runtime_fit_passed": False}
    event(run, status="started", scope="downloads only", receipt=f"{run}/complete.json")
    save(f"{run}/initial.json", receipt)
    try:
        for document in documents["documents"]:
            target = f"{catalog['model_root']}/notices/{document['local_filename']}"
            receipt["documentation"].append({**document, "actual": verified(target, document)})
        missing = []
        for component in catalog["components"]:
            destination = f"{catalog['model_root']}/{component['model_relative_path']}"
            if P.resolve(destination).exists():
                receipt["components"].append({**component, "actual": {**verified(destination, component), "reused": True}})
                event(run, status="reused_verified", role=component["role"])
            elif component["role"] in {"text_encoder", "video_vae"}:
                missing.append((component, destination))
            else:
                raise FileNotFoundError(f"Expected reusable component absent: {destination}")
        for component, destination in missing:
            actual = transfer(run, component, destination)
            receipt["components"].append({**component, "actual": {**actual, "reused": False}})
            save(f"{run}/verified_{component['role']}.json", receipt["components"][-1])
        receipt.update(status="all_components_verified", finished_utc=now(), script=identity(f"{SPIKE}/finish_model_download.py"))
        save(f"{run}/complete.json", receipt)
        event(run, status=receipt["status"], receipt=f"{run}/complete.json")
    except Exception as exc:
        receipt.update(status="failed", finished_utc=now(), error_type=type(exc).__name__, error=str(exc))
        save(f"{run}/failure.json", receipt)
        event(run, status="failed", error_type=type(exc).__name__, error=str(exc), receipt=f"{run}/failure.json")
        raise


if __name__ == "__main__":
    main()
