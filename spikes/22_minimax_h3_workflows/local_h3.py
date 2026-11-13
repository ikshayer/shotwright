"""Bounded local-only MiniMax H3 spike. No imports from inference scripts."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.domain.seeds import derive_seed
from orchestrator.jobs.lease import PynvmlProbe
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
SPIKE = "spikes/22_minimax_h3_workflows"
MODEL = SPIKE + "/_work/local_models"
RUNTIME = SPIKE + "/_work/local_runtime"
COMFY = RUNTIME + "/source/ComfyUI"
INPUT = SPIKE + "/_work/test_inputs/20261109T165721Z_e1e1bf74/manifest.json"
CANONICAL_SHA = "a27d7f2a4d56f144f79c4efbb1740ff59a68e1610b9010f5dc764cb7aa3d0a2c"
PORT = 8295
GIB = 1024 ** 3


def utc() -> str:
    return datetime.now(UTC).isoformat()


def write_json(rel: str, value: Any) -> None:
    PROJECT.write_text(rel, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def read_json(rel: str) -> Any:
    return json.loads(PROJECT.read_text(rel))


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def identity(rel: str) -> dict[str, Any]:
    path = PROJECT.resolve(rel)
    return {"path": rel, "bytes": path.stat().st_size, "sha256": digest(path)}


def checked(record: dict[str, Any]) -> dict[str, Any]:
    observed = identity(record["path"])
    if observed["sha256"] != record["sha256"]:
        raise ValueError(f"Hash differs: {record['path']}")
    return observed


def catalog() -> dict[str, Any]:
    value = read_json(SPIKE + "/LOCAL_MODEL_CATALOG.json")
    if value["model_root"] != MODEL or len(value["components"]) != 4 or value["total_file_bytes"] != 30766905991:
        raise ValueError("Unexpected reviewed model catalog")
    if sum(c["expected_bytes"] for c in value["components"]) != value["total_file_bytes"]:
        raise ValueError("Catalog size mismatch")
    for c in value["components"]:
        expected = f"https://huggingface.co/{c['repo']}/resolve/{c['revision']}/{c['source_filename']}"
        if c["url"] != expected or len(c["revision"]) != 40 or c["format"] != "safetensors":
            raise ValueError("Model catalog must contain immutable safetensors HTTPS URLs")
        PROJECT.resolve(MODEL + "/" + c["model_relative_path"])
    return value


def fetch(url: str, rel: str, *, expected_bytes: int, expected_sha256: str,
          progress: dict[str, Any], deadline_seconds: int = 7200) -> dict[str, Any]:
    """One bounded atomic transfer. Caller decides whether a retry is permitted."""
    target = PROJECT.resolve(rel)
    if target.exists():
        actual = identity(rel)
        if actual["bytes"] != expected_bytes or actual["sha256"] != expected_sha256:
            raise ValueError(f"Existing file differs from pinned bytes/hash; retained unchanged: {rel}")
        return {**actual, "reused": True, "started_utc": utc(), "finished_utc": utc()}
    staged_rel = rel + "." + uuid.uuid4().hex + ".partial"
    staged = PROJECT.resolve(staged_rel)
    started = utc()
    until = time.monotonic() + deadline_seconds
    count = 0
    hashed = hashlib.sha256()
    try:
        def transfer(path: Path) -> None:
            nonlocal count
            request = urllib.request.Request(url, headers={"User-Agent": "CCTV-local-H3/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response, path.open("xb") as stream:
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) != expected_bytes:
                    raise ValueError(f"HTTP size differs: {content_length} expected {expected_bytes}")
                last_update = 0.0
                while True:
                    if time.monotonic() >= until:
                        raise TimeoutError("Overall model transfer deadline exceeded")
                    block = response.read(4 * 1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > expected_bytes:
                        raise ValueError("Download exceeds pinned file size")
                    stream.write(block)
                    hashed.update(block)
                    if time.monotonic() - last_update >= 3:
                        progress.update({"bytes": count, "updated_utc": utc()})
                        write_json(MODEL + "/acquisition_progress.json", progress)
                        last_update = time.monotonic()
        PROJECT.write_with(staged_rel, transfer)
        actual_hash = hashed.hexdigest()
        if count != expected_bytes or actual_hash != expected_sha256:
            raise ValueError(f"Downloaded bytes/hash mismatch: {count} / {actual_hash}")
        # Both names have passed the sole containment helper, on the same volume.
        staged.replace(target)
        return {"path": rel, "bytes": count, "sha256": actual_hash, "reused": False,
                "started_utc": started, "finished_utc": utc()}
    finally:
        PROJECT.resolve(staged_rel).unlink(missing_ok=True)


def archive_previous_acquisition() -> dict[str, Any] | None:
    """Preserve the prior index byte-for-byte before starting another invocation."""
    rel = MODEL + "/acquisition_receipt.json"
    if not PROJECT.resolve(rel).exists():
        return None
    data = PROJECT.read_bytes(rel)
    sha = hashlib.sha256(data).hexdigest()
    archived = MODEL + "/audit/previous_receipt_" + sha + ".json"
    if PROJECT.resolve(archived).exists():
        if PROJECT.read_bytes(archived) != data:
            raise ValueError("Prior immutable acquisition archive differs")
    else:
        PROJECT.write_bytes(archived, data)
    progress_rel = MODEL + "/acquisition_progress.json"
    progress_archive = None
    if PROJECT.resolve(progress_rel).exists():
        progress_data = PROJECT.read_bytes(progress_rel)
        progress_sha = hashlib.sha256(progress_data).hexdigest()
        progress_archive = MODEL + "/audit/previous_progress_" + progress_sha + ".json"
        if PROJECT.resolve(progress_archive).exists():
            if PROJECT.read_bytes(progress_archive) != progress_data:
                raise ValueError("Prior immutable progress archive differs")
        else:
            PROJECT.write_bytes(progress_archive, progress_data)
    return {**identity(archived), "previous_progress_path": progress_archive}


def download_env(invocation: str, role: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("CCTV_H3_AUDIO_DIAGNOSTIC", None)
    for key in list(env):
        upper = key.upper()
        if upper in ("PYTHONHOME", "PYTHONPATH", "AWS_ACCESS_KEY_ID", "GOOGLE_APPLICATION_CREDENTIALS",
                     "HF_HUB_ENABLE_HF_TRANSFER", "HF_ENDPOINT") or upper.startswith("HF_XET_") or any(
                         word in upper for word in ("TOKEN", "API_KEY", "SECRET", "PASSWORD")):
            env.pop(key, None)
    directories = {"HF_HOME": "cache/hf", "HF_HUB_CACHE": "cache/hf/hub", "HF_XET_CACHE": "cache/xet",
                   "XDG_CACHE_HOME": "cache/xdg", "TORCH_HOME": "cache/torch",
                   "PYTHONPYCACHEPREFIX": "cache/pycache", "PIP_CACHE_DIR": "cache/pip",
                   "TEMP": f"hf_downloads/{role}/{invocation}/temp",
                   "TMP": f"hf_downloads/{role}/{invocation}/temp",
                   "TMPDIR": f"hf_downloads/{role}/{invocation}/temp"}
    for key, leaf in directories.items():
        env[key] = str(PROJECT.make_dir(MODEL + "/" + leaf))
    env.update({"HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_DISABLE_PROGRESS_BARS": "1", "HF_XET_HIGH_PERFORMANCE": "0",
                "HF_HUB_DOWNLOAD_TIMEOUT": "30", "HF_HUB_ETAG_TIMEOUT": "30",
                "DO_NOT_TRACK": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def stop_owned_tree(process: subprocess.Popen[Any]) -> list[dict[str, Any]]:
    """Stop only this Popen and observed descendants, including Windows venv workers."""
    import psutil
    records = []
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return [{"pid": process.pid, "already_exited": True}]
    owned = children[::-1] + [parent]
    for item in owned:
        try:
            records.append({"pid": item.pid, "created": item.create_time(), "command": item.cmdline()})
            item.terminate()
        except psutil.NoSuchProcess:
            records.append({"pid": item.pid, "already_exited": True})
    _, alive = psutil.wait_procs(owned, timeout=15)
    for item in alive:
        item.kill()
    _, still_alive = psutil.wait_procs(alive, timeout=10)
    if still_alive:
        raise RuntimeError(f"Owned child processes did not stop: {[p.pid for p in still_alive]}")
    process.wait(timeout=5)
    return records



class OwnedProcessTree:
    """Track and stop only PID/creation-time identities observed under this launcher."""

    def __init__(self, process: subprocess.Popen[Any], *, graceful_seconds: float = 12.0,
                 forced_seconds: float = 8.0) -> None:
        self.process = process
        self.graceful_seconds = graceful_seconds
        self.forced_seconds = forced_seconds
        self._lock = threading.RLock()
        import psutil
        self._identities: dict[int, float] = {}
        self._parent: dict[int, int | None] = {}
        self._failures: list[dict[str, Any]] = []
        self._observation_events: list[dict[str, Any]] = []
        self._unverifiable: dict[int, str] = {}
        self._cleanup: dict[str, Any] | None = None
        # Capture launcher identity without discovery; caller assigns this object before refresh.
        try:
            self._identities[process.pid] = psutil.Process(process.pid).create_time()
            self._parent[process.pid] = None
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            self._unverifiable[process.pid] = str(exc)
            self._failures.append({"stage": "launcher_identity_capture", "pid": process.pid, "error": str(exc)})

    @staticmethod
    def _matches(pid: int, created: float) -> Any | None:
        import psutil
        try:
            candidate = psutil.Process(pid)
            if candidate.create_time() != created or candidate.status() == psutil.STATUS_ZOMBIE:
                return None
            return candidate
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None

    def refresh(self) -> list[dict[str, Any]]:
        import psutil
        with self._lock:
            owner_created = self._identities.get(self.process.pid)
            owner = None if owner_created is None else self._checked_match(self.process.pid, owner_created)
            if owner is not None:
                self._parent.setdefault(owner.pid, None)
            # Search every still-matching cached ancestor, so children remain discoverable
            # even when the original launcher exits but a cached intermediate survives.
            queue = [pid for pid, created in self._identities.items() if self._checked_match(pid, created) is not None]
            seen: set[int] = set()
            while queue:
                parent_pid = queue.pop(0)
                if parent_pid in seen:
                    continue
                seen.add(parent_pid)
                parent_created = self._identities[parent_pid]
                parent = self._checked_match(parent_pid, parent_created)
                if parent is None:
                    continue
                try:
                    children = parent.children(recursive=False)
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except psutil.AccessDenied as exc:
                    self._failures.append({"stage": "discover", "pid": parent_pid, "error": str(exc)})
                    continue
                for child in children:
                    try:
                        created = child.create_time()
                        current = self._identities.get(child.pid)
                        if current is not None and current != created:
                            continue
                        self._identities.setdefault(child.pid, created)
                        self._parent.setdefault(child.pid, parent_pid)
                        self._unverifiable.pop(child.pid, None)
                        queue.append(child.pid)
                    except (psutil.NoSuchProcess, psutil.ZombieProcess):
                        continue
                    except psutil.AccessDenied as exc:
                        self._unverifiable[child.pid] = str(exc)
                        self._failures.append({"stage": "discover_child_identity", "pid": child.pid, "error": str(exc)})
                        continue
            return self.snapshot()

    def _checked_match(self, pid: int, created: float) -> Any | None:
        import psutil
        try:
            proc = self._matches(pid, created)
            self._unverifiable.pop(pid, None)
            return proc
        except psutil.AccessDenied as exc:
            self._unverifiable[pid] = str(exc)
            self._failures.append({"stage": "identity_check", "pid": pid, "error": str(exc)})
            return None

    def _live(self) -> list[tuple[Any, float]]:
        return [(p, created) for pid, created in self._identities.items()
                if (p := self._checked_match(pid, created)) is not None]

    def sample(self) -> dict[str, Any]:
        import psutil
        with self._lock:
            self.refresh()
            if self._unverifiable or self._failures:
                raise RuntimeError("Owned process observation incomplete during sampling: " + json.dumps(
                    {"unverifiable_identities": self._unverifiable, "failures": self._failures}, sort_keys=True))
            rows = []
            totals = {"rss": 0, "private": 0, "vms": 0}
            for proc, created in self._live():
                try:
                    mem = proc.memory_info()._asdict()
                    full = proc.memory_full_info()._asdict()
                    private = int(full.get("private", full.get("uss", mem.get("private", 0))))
                    values = {"rss": int(mem.get("rss", 0)), "private": private, "vms": int(mem.get("vms", 0))}
                    command = proc.cmdline()
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except psutil.AccessDenied as exc:
                    self._record_sample_access_denied(psutil, proc.pid, created, exc)
                    continue
                # Commit a row and its totals only after every required read succeeds.
                rows.append({"pid": proc.pid, "create_time": created, "created": created,
                             "parent_pid": self._parent.get(proc.pid), "memory_bytes": values, "command": command})
                for key, value in values.items():
                    totals[key] += value
            identities = self.snapshot()
            if self._unverifiable or self._failures:
                raise RuntimeError("Owned process observation incomplete after sampling: " + json.dumps(
                    {"unverifiable_identities": self._unverifiable, "failures": self._failures}, sort_keys=True))
            return {"processes": rows, "aggregate_memory_bytes": totals, "identity_snapshot": identities,
                    "observation_failures": list(self._failures),
                    "observation_events": list(self._observation_events)}

    def _record_sample_access_denied(self, psutil: Any, pid: int, expected_created: float,
                                     error: Any) -> None:
        """Skip only when a fresh PID lookup proves the sampled identity has exited."""
        try:
            current = psutil.Process(pid)
            actual_created = current.create_time()
        except psutil.ZombieProcess:
            reason = "zombie"
        except psutil.NoSuchProcess:
            reason = "gone"
        except psutil.AccessDenied as identity_error:
            message = str(identity_error)
            self._unverifiable[pid] = message
            self._failures.append({"stage": "sample_exit_race_identity", "pid": pid,
                                   "create_time": expected_created, "error": message,
                                   "memory_error": str(error)})
            raise RuntimeError(f"Owned process identity recheck denied for PID {pid}: {identity_error}") from identity_error
        except Exception as identity_error:  # Unknown identity state is a persistent failure.
            message = f"{type(identity_error).__name__}: {identity_error}"
            self._unverifiable[pid] = message
            self._failures.append({"stage": "sample_exit_race_identity", "pid": pid,
                                   "create_time": expected_created, "error": message,
                                   "memory_error": str(error)})
            raise RuntimeError(f"Owned process identity recheck failed for PID {pid}: {message}") from identity_error
        else:
            if actual_created != expected_created:
                reason = "pid_reuse"
            else:
                try:
                    status = current.status()
                except psutil.ZombieProcess:
                    reason = "zombie"
                except psutil.NoSuchProcess:
                    reason = "gone"
                except psutil.AccessDenied as identity_error:
                    message = str(identity_error)
                    self._unverifiable[pid] = message
                    self._failures.append({"stage": "sample_exit_race_identity", "pid": pid,
                                           "create_time": expected_created, "error": message,
                                           "memory_error": str(error)})
                    raise RuntimeError(f"Owned process identity recheck denied for PID {pid}: {identity_error}") from identity_error
                except Exception as identity_error:  # Unknown identity state is a persistent failure.
                    message = f"{type(identity_error).__name__}: {identity_error}"
                    self._unverifiable[pid] = message
                    self._failures.append({"stage": "sample_exit_race_identity", "pid": pid,
                                           "create_time": expected_created, "error": message,
                                           "memory_error": str(error)})
                    raise RuntimeError(f"Owned process identity recheck failed for PID {pid}: {message}") from identity_error
                else:
                    if status == psutil.STATUS_ZOMBIE:
                        reason = "zombie"
                    else:
                        self._failures.append({"stage": "sample", "pid": pid, "create_time": expected_created,
                                               "error": str(error), "reason": "same_identity_live"})
                        raise RuntimeError(f"Owned process memory sampling denied for live PID {pid}: {error}") from error
        self._observation_events.append({"type": "sample_exit_race", "pid": pid,
                                         "expected_create_time": expected_created,
                                         "error": str(error), "reason": reason})

    def snapshot(self) -> list[dict[str, Any]]:
        import psutil
        rows = []
        for pid, created in sorted(self._identities.items()):
            try:
                verified: bool | None = self._checked_match(pid, created) is not None
                state = "verified_live" if verified else ("identity_unverifiable" if pid in self._unverifiable else "not_live_or_identity_mismatch")
            except psutil.AccessDenied as exc:
                verified = None
                state = "identity_unverifiable"
                self._unverifiable[pid] = str(exc)
            except Exception as exc:  # noqa: BLE001 - snapshots must remain serializable during failure finalization.
                verified = None
                state = "identity_check_error"
                self._unverifiable[pid] = f"{type(exc).__name__}: {exc}"
            rows.append({"pid": pid, "create_time": created, "created": created,
                         "parent_pid": self._parent.get(pid), "live_identity_verified": verified,
                         "identity_state": state})
        return rows

    def stop(self) -> dict[str, Any]:
        import psutil
        with self._lock:
            if self._cleanup is not None:
                if self._cleanup.get("verified_stopped") is True:
                    return self._cleanup
                raise RuntimeError("Prior owned process cleanup remains unresolved: " + json.dumps(self._cleanup, sort_keys=True))
            self.refresh()
            started = utc()
            events: list[dict[str, Any]] = []
            # Popen retains a Windows process handle. If PID creation-time capture failed,
            # this handle can stop the exact launcher, but cannot prove descendants are gone.
            if self.process.pid not in self._identities and self.process.poll() is None:
                try:
                    self.process.terminate()
                    events.append({"pid": self.process.pid, "signal": "Popen_handle_terminate", "identity_verified": False})
                    self.process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    events.append({"pid": self.process.pid, "signal": "Popen_handle_kill", "identity_verified": False})
                    try:
                        self.process.wait(timeout=4)
                    except subprocess.TimeoutExpired as exc:
                        self._failures.append({"stage": "launcher_handle_wait", "pid": self.process.pid, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001 - retain exact Popen handle cleanup failure.
                    self._failures.append({"stage": "launcher_handle_stop", "pid": self.process.pid, "error": str(exc)})
            live = self._live()
            # Descendants first. Signal only after revalidating PID and creation time.
            live.sort(key=lambda pair: self._depth(pair[0].pid), reverse=True)
            for proc, created in live:
                verified = self._checked_match(proc.pid, created)
                if verified is None:
                    continue
                try:
                    verified.terminate()
                    events.append({"pid": proc.pid, "created": created, "signal": "terminate"})
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    pass
                except Exception as exc:  # noqa: BLE001 - retain any OS signaling failure as cleanup evidence.
                    self._failures.append({"stage": "terminate", "pid": proc.pid, "error": str(exc)})
            deadline = time.monotonic() + self.graceful_seconds
            while time.monotonic() < deadline and self._live():
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            survivors = self._live()
            for proc, created in survivors:
                verified = self._checked_match(proc.pid, created)
                if verified is None:
                    continue
                try:
                    verified.kill()
                    events.append({"pid": proc.pid, "created": created, "signal": "kill"})
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    pass
                except Exception as exc:  # noqa: BLE001 - retain any OS signaling failure as cleanup evidence.
                    self._failures.append({"stage": "kill", "pid": proc.pid, "error": str(exc)})
            deadline = time.monotonic() + self.forced_seconds
            while time.monotonic() < deadline and self._live():
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            remaining = [{"pid": proc.pid, "create_time": created} for proc, created in self._live()]
            snapshot = self.snapshot()
            # Snapshot can discover a newly unverifiable PID; include it before deciding success.
            unknown = [{"pid": pid, "error": error} for pid, error in sorted(self._unverifiable.items())]
            self._cleanup = {"started_utc": started, "finished_utc": utc(), "events": events,
                             "observation_events": list(self._observation_events),
                             "processes": snapshot, "identities": snapshot, "survivors": remaining,
                             "unverifiable_identities": unknown, "failures": list(self._failures),
                             "verified_stopped": not remaining and not unknown and not self._failures}
            if remaining or unknown or self._failures:
                raise RuntimeError("Owned process cleanup unresolved: " + json.dumps(self._cleanup, sort_keys=True))
            return self._cleanup

    def _depth(self, pid: int) -> int:
        depth = 0
        seen = set()
        parent = self._parent.get(pid)
        while parent is not None and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = self._parent.get(parent)
        return depth

def fetch_hf(component: dict[str, Any], rel: str, *, invocation: str,
             progress: dict[str, Any], deadline_seconds: int = 7200) -> dict[str, Any]:
    """Official HFHub/Xet in an isolated child; only exact bytes/hash enter load paths."""
    import psutil
    target = PROJECT.resolve(rel)
    if target.exists():
        actual = identity(rel)
        if actual["bytes"] != component["expected_bytes"] or actual["sha256"] != component["expected_sha256"]:
            raise ValueError(f"Existing catalog load path differs; retained unchanged: {rel}")
        return {**actual, "reused": True, "started_utc": utc(), "finished_utc": utc()}
    stage_rel = MODEL + "/hf_downloads/" + component["role"] + "/" + invocation
    PROJECT.make_dir(stage_rel)
    staged_rel = stage_rel + "/" + component["source_filename"]
    staged = PROJECT.resolve(staged_rel)
    PROJECT.make_dir(str(Path(staged_rel).parent).replace("\\", "/"))
    if staged.exists():
        raise FileExistsError("Fresh per-invocation HF staging path already exists; retained unchanged")
    cancel_rel = MODEL + "/cancel_acquisition_" + invocation
    log_rel = MODEL + "/audit/" + invocation + "/hf_" + component["role"] + ".log"
    PROJECT.write_bytes(log_rel, b"")
    env = download_env(invocation, component["role"])
    child_code = (
        "import importlib.metadata as m,json,sys; "
        "versions={n:m.version(n) for n in ('huggingface_hub','hf_xet')}; "
        "assert versions=={'huggingface_hub':'2.4.0','hf_xet':'1.8.0'},versions; "
        "from huggingface_hub import hf_hub_download; "
        "path=hf_hub_download(repo_id=sys.argv[1],filename=sys.argv[2],revision=sys.argv[3],"
        "local_dir=sys.argv[4],token=False,etag_timeout=30); "
        "print('HF_RESULT:'+json.dumps({'path':str(path),'versions':versions}),flush=True)"
    )
    args = [str(PROJECT.resolve(RUNTIME + "/env/Scripts/python.exe")), "-B", "-c", child_code,
            component["repo"], component["source_filename"], component["revision"], str(PROJECT.resolve(stage_rel))]
    started = utc()
    clock_start = time.monotonic()
    progress.update({"backend": "huggingface_hub2.4.0/hf_xet1.8.0", "high_performance": False,
        "status": "downloading", "bytes": None, "bytes_semantics": "unknown until full-file verification; Xet may preallocate",
        "stage_root": stage_rel, "cancel_marker": cancel_rel, "log_path": log_rel,
        "started_utc": started, "failed_partials_from_prior_invocations_retained": True,
        "library_temporary_cleanup": "stock HFHub2.4 may remove only its own new UUID.incomplete on natural failure; advance notice provided by parent; old staging retained"})
    write_json(MODEL + "/acquisition_progress.json", progress)
    if PROJECT.resolve(cancel_rel).exists():
        raise RuntimeError("Acquisition canceled by contained marker before starting child")
    process = None
    with PROJECT.resolve(log_rel).open("ab") as log:
        try:
            process = subprocess.Popen(args, cwd=PROJECT.resolve(stage_rel), env=env, stdout=log, stderr=subprocess.STDOUT)
            progress.update({"owned_launcher_pid": process.pid, "command": args})
            while process.poll() is None:
                if PROJECT.resolve(cancel_rel).exists():
                    progress["termination"] = stop_owned_tree(process)
                    raise RuntimeError("Acquisition canceled by contained marker; retained remaining stage/cache files")
                if time.monotonic() - clock_start > deadline_seconds:
                    progress["termination"] = stop_owned_tree(process)
                    raise TimeoutError("Bounded HF child transfer deadline exceeded; retained remaining stage/cache files")
                observed_processes = []
                try:
                    owner = psutil.Process(process.pid)
                    for item in [owner, *owner.children(recursive=True)]:
                        try:
                            observed_processes.append({"pid": item.pid, "created": item.create_time(),
                                "rss": item.memory_info().rss, "io_write_bytes": item.io_counters().write_bytes})
                        except psutil.NoSuchProcess:
                            continue
                except psutil.NoSuchProcess:
                    pass
                progress.update({"updated_utc": utc(), "elapsed_seconds": time.monotonic() - clock_start,
                    "process_observations": observed_processes,
                    "disk_free_bytes": shutil.disk_usage(PROJECT.resolve(MODEL)).free})
                write_json(MODEL + "/acquisition_progress.json", progress)
                time.sleep(2)
            if process.returncode:
                raise RuntimeError(f"Official HF download child exited {process.returncode}; exact log retained: {log_rel}")
        finally:
            if process is not None and process.poll() is None:
                progress["termination"] = stop_owned_tree(process)
            progress["owned_child_stopped"] = process is None or process.poll() is not None
            progress["child_returncode"] = None if process is None else process.returncode
            progress["retained_stage_root"] = stage_rel
    reports = [line.removeprefix("HF_RESULT:") for line in PROJECT.read_text(log_rel).splitlines()
               if line.startswith("HF_RESULT:")]
    if len(reports) != 1 or Path(json.loads(reports[0])["path"]).resolve() != staged:
        raise ValueError("HF child did not report expected contained source-filename stage path")
    actual = identity(staged_rel)
    if actual["bytes"] != component["expected_bytes"] or actual["sha256"] != component["expected_sha256"]:
        progress.update({"observed_stage": actual, "status": "verification_failed"})
        raise ValueError(f"HF staged size/hash differs; retained unverified file: {staged_rel}")
    if target.exists():
        raise FileExistsError("Catalog load path appeared during transfer; refusing replacement")
    staged.replace(target)
    return {**identity(rel), "reused": False, "started_utc": started, "finished_utc": utc(),
            "wall_seconds": time.monotonic() - clock_start, "backend": progress["backend"],
            "high_performance": False, "stage_root": stage_rel, "log": identity(log_rel)}



def acquire(*, retry_transient: bool = False) -> dict[str, Any]:
    spec = catalog()
    PROJECT.make_dir(MODEL)
    receipt: dict[str, Any] = {"status": "acquiring", "started_utc": utc(),
        "catalog": identity(SPIKE + "/LOCAL_MODEL_CATALOG.json"),
        "documentation_catalog": identity(SPIKE + "/LOCAL_MODEL_DOCUMENTATION.json"), "components": [],
        "documentation": [], "attempts": [], "invocation_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8], "territory_basis": spec["territory_basis"],
        "approval_scope": spec["approval_scope"], "runtime_fit_passed": False}
    receipt_rel = MODEL + "/acquisition_receipt.json"
    try:
        missing = sum(c["expected_bytes"] for c in spec["components"]
                      if not PROJECT.resolve(MODEL + "/" + c["model_relative_path"]).exists())
        if shutil.disk_usage(PROJECT.resolve(MODEL)).free < missing + 8 * GIB:
            raise OSError("Insufficient disk for missing pinned weights plus configured 8GiB disk safety headroom")
        # Documentation metadata is pinned separately before the large transfers.
        receipt["documentation"] = acquire_documentation()
        for component in spec["components"]:
            rel = MODEL + "/" + component["model_relative_path"]
            PROJECT.make_dir(str(Path(rel).parent).replace("\\", "/"))
            for attempt in range(2 if retry_transient else 1):
                state = {"component": component["role"], "attempt": attempt + 1, "status": "downloading",
                         "expected_bytes": component["expected_bytes"], "bytes": 0}
                try:
                    actual = fetch(component["url"], rel, expected_bytes=component["expected_bytes"],
                                   expected_sha256=component["expected_sha256"], progress=state)
                    receipt["attempts"].append({**state, "status": "verified", **actual})
                    receipt["components"].append({**component, "actual": actual, "downloaded": True})
                    break
                except Exception as exc:
                    receipt["attempts"].append({**state, "status": "failed", "error_type": type(exc).__name__,
                        "error": str(exc), "http_status": getattr(exc, "code", None), "failed_utc": utc()})
                    write_json(receipt_rel, receipt)
                    transient = isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError))
                    if isinstance(exc, urllib.error.HTTPError):
                        transient = exc.code in (408, 429, 500, 502, 503, 504)
                    if attempt or not retry_transient or not transient:
                        raise
            write_json(receipt_rel, receipt)
        receipt.update({"status": "all_components_verified", "finished_utc": utc(),
                        "total_verified_bytes": sum(c["actual"]["bytes"] for c in receipt["components"])})
        write_json(MODEL + "/acquisition_progress.json", {"status": receipt["status"], "bytes": receipt["total_verified_bytes"]})
    except Exception as exc:
        receipt.update({"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "finished_utc": utc()})
        raise
    finally:
        write_json(MODEL + "/audit/acquisition_" + receipt["invocation_id"] + ".json", receipt)
        write_json(receipt_rel, receipt)
    return receipt


def acquire_documentation() -> list[dict[str, Any]]:
    """Only revision-pinned documents from catalog-controlled repos plus canonical license."""
    provenance_rel = SPIKE + "/LOCAL_MODEL_DOCUMENTATION.json"
    if not PROJECT.resolve(provenance_rel).is_file():
        raise FileNotFoundError("Parent must pin LOCAL_MODEL_DOCUMENTATION.json (canonical license/cards) before acquisition")
    docs = read_json(provenance_rel)
    records = []
    for item in docs["documents"]:
        if len(item["revision"]) != 40 or item["url"] != f"https://huggingface.co/{item['repo']}/resolve/{item['revision']}/{item['source_filename']}":
            raise ValueError("Documentation must use pinned HF revisions")
        rel = MODEL + "/notices/" + item["local_filename"]
        PROJECT.make_dir(MODEL + "/notices")
        actual = fetch(item["url"], rel, expected_bytes=item["expected_bytes"],
                       expected_sha256=item["expected_sha256"], progress={"component": "notice"})
        payload = PROJECT.read_bytes(rel)
        blob = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
        if blob != item["git_blob_sha1"]:
            raise ValueError("Pinned documentation git blob differs")
        records.append({**item, "actual": actual, "observed_git_blob_sha1": blob})
    if not any(d.get("role") == "canonical_h3_license" for d in records):
        raise ValueError("Actual canonical H3 Community License is required")
    return records


def verify_models() -> dict[str, Any]:
    spec = catalog()
    receipt = read_json(MODEL + "/acquisition_receipt.json")
    if receipt["status"] != "all_components_verified":
        raise ValueError("All four pinned components must have verified execution receipts")
    if receipt["catalog"]["sha256"] != identity(SPIKE + "/LOCAL_MODEL_CATALOG.json")["sha256"]:
        raise ValueError("Catalog changed since acquisition")
    for wanted in spec["components"]:
        row = next(c for c in receipt["components"] if c["role"] == wanted["role"])
        for key in ("revision", "repo", "source_filename", "expected_sha256", "expected_bytes", "model_relative_path"):
            if row[key] != wanted[key]:
                raise ValueError("Receipt component identity differs from control catalog")
        load_path = MODEL + "/" + wanted["model_relative_path"]
        if row["actual"]["path"] != load_path:
            raise ValueError("Receipt actual path is not the catalog-derived model load path")
        actual = identity(load_path)
        if actual["bytes"] != wanted["expected_bytes"] or actual["sha256"] != wanted["expected_sha256"]:
            raise ValueError("Model verification failed")
    if receipt["documentation_catalog"]["sha256"] != identity(SPIKE + "/LOCAL_MODEL_DOCUMENTATION.json")["sha256"]:
        raise ValueError("Documentation control catalog changed since acquisition")
    expected_docs = read_json(SPIKE + "/LOCAL_MODEL_DOCUMENTATION.json")["documents"]
    if len(receipt["documentation"]) != len(expected_docs):
        raise ValueError("Incomplete pinned model documentation")
    for wanted in expected_docs:
        doc = next(row for row in receipt["documentation"] if row["role"] == wanted["role"])
        if any(doc[key] != value for key, value in wanted.items()):
            raise ValueError("Documentation execution identity differs from pinned control catalog")
        actual = checked(doc["actual"])
        if actual["bytes"] != wanted["expected_bytes"] or actual["sha256"] != wanted["expected_sha256"]:
            raise ValueError("Pinned documentation bytes/hash mismatch")
    return receipt


def command(args: list[str], run_rel: str, name: str, *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    PROJECT.write_text(run_rel + "/" + name + ".log", json.dumps(args) + "\n" + result.stdout + "\n" + result.stderr)
    if result.returncode:
        raise RuntimeError(f"{name} exited {result.returncode}; log retained")
    return result


def probe(rel: str, run_rel: str, name: str) -> dict[str, Any]:
    result = command(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
                      "-of", "json", str(PROJECT.resolve(rel))], run_rel, name)
    return json.loads(result.stdout)


def check_silent(probed: dict[str, Any], width: int, height: int) -> None:
    streams = probed["streams"]
    video = [s for s in streams if s["codec_type"] == "video"]
    if len(video) != 1 or any(s["codec_type"] == "audio" for s in streams):
        raise ValueError("Reference must have one video stream and zero audio streams")
    s = video[0]
    if (s["width"], s["height"], s["avg_frame_rate"], int(s["nb_read_frames"])) != (width, height, "24/1", 192):
        raise ValueError("Reference canvas/rate/framecount differs")
    if abs(float(s.get("duration", probed["format"]["duration"])) - 8) > 0.001:
        raise ValueError("Reference duration differs")


def prepare_input(run_rel: str, width: int = 640, height: int = 480) -> dict[str, Any]:
    source = read_json(INPUT)
    for key in ("source", "input_video", "prompt"):
        checked(source[key])
    if source["input_video"]["sha256"] != CANONICAL_SHA:
        raise ValueError("Canonical input identity differs")
    check_silent(probe(source["input_video"]["path"], run_rel, "canonical_probe"), 1024, 768)
    rel = run_rel + "/input/model_proxy.mp4"
    PROJECT.make_dir(run_rel + "/input")
    def transcode(path: Path) -> None:
        command(["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i",
                 str(PROJECT.resolve(source["input_video"]["path"])), "-map", "0:v:0", "-an",
                 "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1", "-c:v", "libx264", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-f", "mp4", str(path)], run_rel, "proxy_create")
    PROJECT.write_with(rel, transcode)
    metadata = probe(rel, run_rel, "proxy_probe")
    check_silent(metadata, width, height)
    return {"manifest": identity(INPUT), "source": source, "source_notices": source_notices(source), "proxy": {**identity(rel), "probe": metadata},
            "proxy_transforms": [f"resize canonical 1024x768 to {width}x{height} lanczos", "SAR1:1", "H264 CRF18 yuv420p", "noaudio; no frame/rate/time edit"]}


def build_graph(info: dict[str, Any], input_name: str, prompt: str, *, staged: bool = False, audio_diagnostic: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    maximum = info["RandomNoise"]["input"]["required"]["noise_seed"][1]["max"]
    seed = derive_seed("s1", 0, "minimax_h3_ambience", max_value=maximum)
    components = {c["role"]: Path(c["model_relative_path"]).name for c in catalog()["components"]}
    def node(kind: str, **inputs: Any) -> dict[str, Any]:
        if kind not in info:
            raise ValueError(f"Actual runtime schema lacks {kind}")
        return {"class_type": kind, "inputs": inputs}
    graph: dict[str, Any] = {"1": node("LoadVideo", file=input_name),
                             "2": node("GetVideoComponents", video=["1", 0])}
    if audio_diagnostic and not staged:
        raise ValueError("Audio diagnostic requires staged mode")
    if staged:
        graph.update({
            "3": node("CCTVH3ConditionAndRelease", text_encoder_name=components["text_encoder"],
                      video_vae_name=components["video_vae"], audio_vae_name=components["audio_vae"],
                      prompt=prompt, width=384, height=288, length=192, ref_video=["2", 0]),
            "4": node("CCTVH3StagedUNETLoader", release=["3", 2], unet_name=components["diffusion"], weight_dtype="default"),
            "5": node("BasicGuider", model=["4", 0], conditioning=["3", 0]),
            "6": node("RandomNoise", noise_seed=seed),
            "7": node("BasicScheduler", model=["4", 0], scheduler="simple", steps=20, denoise=1.0),
            "8": node("KSamplerSelect", sampler_name="res_multistep"),
            "9": node("SamplerCustomAdvanced", noise=["6", 0], guider=["5", 0], sampler=["8", 0], sigmas=["7", 0], latent_image=["3", 1]),
            "10": node("CCTVH3DecodeVAELoader", samples=["9", 0], release=["3", 2], video_vae_name=components["video_vae"], audio_vae_name=components["audio_vae"]),
            "11": node("VAEDecode", samples=["10", 0], vae=["10", 1]),
            "12": node("CCTVH3AudioDiagnostic" if audio_diagnostic else "VAEDecodeAudio", samples=["10", 0], vae=["10", 2]),
            "13": node("CreateVideo", images=["11", 0], audio=["12", 0], fps=24.0),
            "14": node("SaveVideo", video=["13", 0], filename_prefix="native_h3", format="mp4", **{"format.codec": "h264"}),
        })
    else:
        graph.update({
            "3": node("UNETLoader", unet_name=components["diffusion"], weight_dtype="default"),
            "4": node("CLIPLoader", clip_name=components["text_encoder"], type="minimax", device="default"),
            "5": node("VAELoader", vae_name=components["video_vae"]),
            "6": node("VAELoader", vae_name=components["audio_vae"]),
            "7": node("MiniMaxH3ReferenceToVideo", clip=["4", 0], vae=["5", 0], audio_vae=["6", 0], prompt=prompt,
                      width=640, height=480, length=192, ref_image_size="match", **{"ref_videos.ref_video_0": ["2", 0]}),
            "8": node("BasicGuider", model=["3", 0], conditioning=["7", 0]),
            "9": node("RandomNoise", noise_seed=seed),
            "10": node("BasicScheduler", model=["3", 0], scheduler="simple", steps=20, denoise=1.0),
            "11": node("KSamplerSelect", sampler_name="res_multistep"),
            "12": node("SamplerCustomAdvanced", noise=["9", 0], guider=["8", 0], sampler=["11", 0], sigmas=["10", 0], latent_image=["7", 1]),
            "13": node("VAEDecode", samples=["12", 0], vae=["5", 0]),
            "14": node("VAEDecodeAudio", samples=["12", 0], vae=["6", 0]),
            "15": node("CreateVideo", images=["13", 0], audio=["14", 0], fps=24.0),
            "16": node("SaveVideo", video=["15", 0], filename_prefix="native_h3", format="mp4", **{"format.codec": "h264"}),
        })
    for row in graph.values():
        schema = info[row["class_type"]]["input"]
        available = {**schema.get("required", {}), **schema.get("optional", {})}
        if row["class_type"] == "MiniMaxH3ReferenceToVideo":
            group = available["ref_videos"]
            template = group[1]["template"]
            if group[0] != "COMFY_AUTOGROW_V3" or template["prefix"] != "ref_video_" or template["max"] < 1:
                raise ValueError("Actual R2V video Autogrow schema differs from pinned source evidence")
            available["ref_videos.ref_video_0"] = template["input"]["required"]["ref_video"]
        if row["class_type"] == "SaveVideo":
            formats = available["format"]
            if formats[0] != "COMFY_DYNAMICCOMBO_V3":
                raise ValueError("Actual SaveVideo format is not the inspected native dynamic combo")
            mp4 = next(option for option in formats[1]["options"] if option["key"] == "mp4")
            available["format.codec"] = mp4["inputs"]["required"]["codec"]
        for name, value in row["inputs"].items():
            if name not in available:
                raise ValueError(f"Unknown actual port {row['class_type']}.{name}")
            definition = available[name][0]
            if definition in ("COMBO", "COMFY_DYNAMICCOMBO_V3"):
                options = available[name][1]["options"]
                allowed = [option["key"] if isinstance(option, dict) else option for option in options]
                if value not in allowed:
                    raise ValueError(f"Actual choice unavailable at {row['class_type']}.{name}: {value}")
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0] in graph:
                output_type = info[graph[value[0]]["class_type"]]["output"][value[1]]
                if isinstance(definition, str) and definition != output_type:
                    raise ValueError(f"Link type mismatch at {name}: {output_type} / {definition}")
            elif isinstance(definition, list) and value not in definition:
                raise ValueError(f"Actual choice unavailable at {row['class_type']}.{name}: {value}")
    return graph, {"shot_id": "s1", "degrade_seed": 0, "stage_name": "minimax_h3_ambience", "stage": "minimax_h3_ambience",
                   "max_value": maximum, "value": seed, "repeatability_measured": False}

def clean_env(run_rel: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("CCTV_H3_AUDIO_DIAGNOSTIC", None)
    for key in list(env):
        upper = key.upper()
        if upper in ("PYTHONHOME", "PYTHONPATH", "AWS_ACCESS_KEY_ID", "GOOGLE_APPLICATION_CREDENTIALS") or any(word in upper for word in ("TOKEN", "API_KEY", "SECRET", "PASSWORD")):
            env.pop(key, None)
    for key, leaf in {"TEMP": "temp", "TMP": "temp", "TMPDIR": "temp", "HF_HOME": "cache/hf",
                      "XDG_CACHE_HOME": "cache/xdg", "TORCH_HOME": "cache/torch",
                      "CUDA_CACHE_PATH": "cache/cuda", "PYTHONPYCACHEPREFIX": "cache/pycache",
                      "TRITON_CACHE_DIR": "cache/triton", "TORCHINDUCTOR_CACHE_DIR": "cache/inductor",
                      "NUMBA_CACHE_DIR": "cache/numba", "MPLCONFIGDIR": "cache/matplotlib",
                      "PIP_CACHE_DIR": "cache/pip"}.items():
        env[key] = str(PROJECT.make_dir(run_rel + "/" + leaf))
    env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                "DO_NOT_TRACK": "1", "CUDA_VISIBLE_DEVICES": "0", "PYTHONDONTWRITEBYTECODE": "1",
                "NO_PROXY": "127.0.0.1,localhost", "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"})
    return env


def http(endpoint: str, value: Any = None, timeout: int = 10) -> Any:
    data = None if value is None else json.dumps(value).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{endpoint}", data=data,
                                 headers={"Content-Type": "application/json"})
    # No inherited proxy is used for loopback.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as response:
        return json.loads(response.read())


def resources(gpu: PynvmlProbe, tree: OwnedProcessTree | None = None) -> dict[str, Any]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                    ("total_pagefile", ctypes.c_ulonglong), ("available_pagefile", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended", ctypes.c_ulonglong)]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    process_memory = None if tree is None else tree.sample()
    return {"utc": utc(), "monotonic": time.monotonic(),
            "ram_available_bytes": status.available_physical, "ram_total_bytes": status.total_physical,
            "windows_commit_limit_bytes": status.total_pagefile,
            "windows_commit_available_bytes": status.available_pagefile,
            "windows_commit_used_bytes": status.total_pagefile - status.available_pagefile,
            "gpu_whole_device_mb": asdict(gpu.device_memory()),
            "process_memory_bytes": process_memory,
            "disk_free_bytes": shutil.disk_usage(PROJECT.resolve(MODEL)).free,
            "disk_free_bytes_by_drive": {drive: shutil.disk_usage(drive + ":\\").free for drive in ("C", "D")}}


def native_evidence(run_rel: str, *, staged: bool = False, audio_diagnostic: bool = False) -> dict[str, Any]:
    paths = [
        COMFY + "/comfy_extras/nodes_minimax_h3.py",
        COMFY + "/comfy_extras/nodes_audio.py",
        COMFY + "/comfy_api/latest/_io.py",
        COMFY + "/tests-unit/execution_test/dynamic_inputs_test.py",
        RUNTIME + "/env/Lib/site-packages/comfyui_workflow_templates_json/templates/video_minimax_h3_r2v.json",
    ]
    evidence = {"files": [identity(path) for path in paths],
                "autogrow_api_key": "ref_videos.ref_video_0",
                "node_reference_path": "LoadVideo.VIDEO -> GetVideoComponents.IMAGE -> MiniMaxH3ReferenceToVideo.ref_videos.ref_video_0",
                "video_latent_reference": "graph connects native video VAE; inspected native R2V code passes all192frames to VAE.encode; actual execution success established only by history",
                "text_encoder_reference": "native R2V samples full video to2fps for Qwen reference pixels/timestamps",
                "frame_count": 192, "frame_grid": "17k+5", "generated_audio_joint": True,
                "native_audio_decode_amplitude_rule": (
                    "CCTVH3AudioDiagnostic captures one VAE decode output before native std division, the std-times-five tensor, clamped divisor, and post-division precodec waveform; it cannot observe the waveform before VAE.decode"
                    if audio_diagnostic else
                    "stock VAEDecodeAudio divides waveform by max(std(waveform)*5,1); built-in attenuation, multiplier not captured before encodedAV"
                ),
                "source_audio_inputs": [], "template_changes": ["NVFP4 diffusion catalog component", "official res_multistep20steps/simple1 schedule per parent adjudication", "384x288 staged" if staged else "640x480 baseline", "192frames", "video reference only", "noTurboLoRA"], "staged_composite_delegation": staged, "audio_diagnostic_decoder": "CCTVH3AudioDiagnostic" if audio_diagnostic else None, "staged_reference_shape": [192, 288, 384, 3] if staged else None, "staged_video_reference_latent_shape": [1, 24, 57, 18, 24] if staged else None}
    write_json(run_rel + "/native_graph_evidence.json", evidence)
    return evidence


def run_local(*, prepare_only: bool = False, staged: bool = False, audio_diagnostic: bool = False) -> dict[str, Any]:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    run_rel = SPIKE + "/_work/local_runs/" + run_id
    if audio_diagnostic and not staged:
        raise ValueError("Audio diagnostic actions imply staged mode")
    width, height = (384, 288) if staged else (640, 480)
    PROJECT.make_dir(run_rel)
    result: dict[str, Any] = {"schema_version": 1, "status": "preflight", "started_utc": utc(),
        "media_path_base": "worktree_root", "shot_id": "s1", "shot_version": 2,
        "candidate_status": "new_candidate_unapproved", "source_offset_seconds": 0,
        **({"experiment": {"strategy": "staged_native_audio_diagnostic" if audio_diagnostic else "staged_native", "width": 384, "height": 288, "length": 192}} if staged else {}),
        "start_sample": 0, "fps": "24/1", "time_base": "1/24", "timeline_duration_seconds": 8,
        "status_history": [], "media": {}, "transforms": {},
        "policy": {"admission_available_ram_bytes": 6 * GIB, "stop_available_ram_bytes": 2 * GIB,
                   "stop_consecutive_samples": 3, "startup_deadline_seconds": 180,
                   "generation_deadline_seconds": 1800, "sample_interval_seconds": 1,
                   "limits_are_configured_safety_policy_not_measured_model_demand": True}}
    process = None
    owned_tree: OwnedProcessTree | None = None
    cleanup_error: dict[str, Any] | None = None
    log = None
    watchdog_stop = threading.Event()
    watchdog_thread = None
    watchdog_faults: list[Exception] = []
    watchdog_deadline = {"at": float("inf"), "phase": "not_started"}
    termination_lock = threading.Lock()
    def terminate_owned() -> dict[str, Any] | None:
        nonlocal cleanup_error
        with termination_lock:
            if owned_tree is None:
                return None
            try:
                evidence = owned_tree.stop()
                result.setdefault("runtime", {})["owned_process_cleanup"] = evidence
                return evidence
            except Exception as exc:
                cleanup_error = {"error_type": type(exc).__name__, "error": str(exc),
                                 "identities": owned_tree.snapshot()}
                result.setdefault("runtime", {})["owned_process_cleanup_error"] = cleanup_error
                raise
    def check_watchdog() -> None:
        if watchdog_faults:
            raise watchdog_faults[0]
    started = time.monotonic()
    try:
        model_receipt = verify_models()
        result["acquisition_receipt"] = identity(MODEL + "/acquisition_receipt.json")
        inputs = prepare_input(run_rel, width, height)
        result["sources"] = {"canonical": {**inputs["source"]["input_video"], "manifest": inputs["manifest"]},
                             "proxy": inputs["proxy"], "upstream": inputs["source"]["source"]}
        result["transforms"]["canonical_to_proxy"] = inputs["proxy_transforms"]
        result["prompt"] = checked(inputs["source"]["prompt"])
        result["provenance"] = {"model_components": model_receipt["components"], "model_notices": model_receipt["documentation"],
            "source_manifest": inputs["source"], "current_source_notices": inputs["source_notices"], "production_state_mutated": False, "creator_approval": "pending",
            "sound_quality": "pending creator audition", "timing_realism": "pending"}
        evidence = native_evidence(run_rel, staged=staged, audio_diagnostic=audio_diagnostic)
        gpu = PynvmlProbe(device_index=0)
        baseline = resources(gpu)
        result["resource_baseline"] = baseline
        result["resource_peaks"] = {"minimum_ram_available_bytes": baseline["ram_available_bytes"],
            "maximum_commit_used_bytes": baseline["windows_commit_used_bytes"],
            "maximum_gpu_whole_device_used_mb": baseline["gpu_whole_device_mb"]["used_mb"],
            "maximum_process_memory_bytes": {}}
        # Explicit device identity through actual NVIDIA reporting; no GPU memory need inferred.
        device = command(["nvidia-smi", "--id=0", "--query-gpu=name", "--format=csv,noheader"],
                         run_rel, "nvidia_device", timeout=15).stdout.strip()
        if "RTX 5070 Ti Laptop" not in device:
            raise RuntimeError(f"GPU0 identity differs from expected NVIDIA laptop GPU: {device}")
        result["device"] = {"cuda_index": 0, "name": device}
        competitors = [asdict(row) for row in gpu.process_usage() if any(s in row.name.lower() for s in ("python", "blender", "comfy"))]
        if competitors:
            result["competing_gpu_processes"] = competitors
            raise RuntimeError("Another possible GPU worker is active; no concurrent generation started")
        if baseline["ram_available_bytes"] < 6 * GIB:
            result["status"] = "held_preflight"
            result["status_history"].append({"status": "held_preflight", "reason": "available RAM below configured6GiB admission floor", "utc": utc()})
            return result
        env = clean_env(run_rel)
        if audio_diagnostic:
            env["CCTV_H3_AUDIO_DIAGNOSTIC"] = "1"
        for leaf in ("input", "output", "temp", "user", "previews", "database"):
            PROJECT.make_dir(run_rel + "/" + leaf)
        python = PROJECT.resolve(RUNTIME + "/env/Scripts/python.exe")
        main = PROJECT.resolve(COMFY + "/main.py")
        help_run = subprocess.run([str(python), "-B", str(main), "--help"], env=env, cwd=PROJECT.resolve(COMFY),
                                  capture_output=True, text=True, timeout=60, check=True)
        help_text = help_run.stdout + help_run.stderr
        PROJECT.write_text(run_rel + "/runtime_help.log", help_text)
        args = [str(python), "-B", str(main), "--listen", "127.0.0.1", "--port", str(PORT),
                "--offline", "--disable-all-custom-nodes", "--cuda-device", "0",
                "--use-pytorch-cross-attention", "--fast-disk", "--disable-pinned-memory",
                *([] if staged else ["--cache-none"]),
                "--base-directory", str(PROJECT.resolve(COMFY)), "--models-directory", str(PROJECT.resolve(MODEL))]
        for leaf, flag in (("input", "--input-directory"), ("output", "--output-directory"),
                           ("temp", "--temp-directory"), ("user", "--user-directory"), ("previews", "--previews-directory")):
            args.extend([flag, str(PROJECT.resolve(run_rel + "/" + leaf))])
        args.extend(["--database-url", "sqlite:///" + str(PROJECT.resolve(run_rel + "/database/comfy.db")).replace("\\", "/")])
        if staged:
            extension = SPIKE + "/stage_extension"
            extension_config = run_rel + "/extra_model_paths.yaml"
            yaml = "h3_staging:\n  base_path: " + str(PROJECT.resolve(extension)).replace("\\", "/") + "\n  custom_nodes: .\n"
            PROJECT.write_text(extension_config, yaml)
            args.extend(["--extra-model-paths-config", str(PROJECT.resolve(extension_config)), "--whitelist-custom-nodes", "h3_staging"])
            env["CCTV_H3_RUN_REL"] = run_rel
            env["CCTV_H3_INVOCATION_ID"] = run_id
        flags = [arg for arg in args if arg.startswith("--")]
        if any(flag not in help_text for flag in flags):
            raise RuntimeError("Actual CLI does not support all selected isolated runtime flags")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", PORT))
        log_rel = run_rel + "/comfy.log"
        PROJECT.write_bytes(log_rel, b"")
        log = PROJECT.resolve(log_rel).open("ab")
        process = subprocess.Popen(args, env=env, cwd=PROJECT.resolve(COMFY), stdout=log, stderr=subprocess.STDOUT)
        owned_tree = OwnedProcessTree(process)
        owned_tree.refresh()
        result["runtime"] = {"command": args, "pid": process.pid, "owned_process_identities": owned_tree.snapshot(), "source": identity(COMFY + "/main.py"),
                             "source_receipt": identity(RUNTIME + "/receipts/source_receipt.json"),
                             "freeze": identity(RUNTIME + "/receipts/freeze.txt"),
                             "isolated_env": RUNTIME + "/env", "model_root": MODEL,
                             "network": "loopback server,offline native nodes,HF/Transformers offline,remote proxies closed; noOSfirewall claim",
                             "gpu": result["device"], "graph_evidence": evidence, **({"extension": identity(SPIKE + "/stage_extension/h3_staging/__init__.py"), "extra_paths_config": identity(run_rel + "/extra_model_paths.yaml")} if staged else {})}
        deadline = time.monotonic() + 180
        watchdog_deadline.update({"at": deadline, "phase": "startup"})
        def watch() -> None:
            low = 0
            while not watchdog_stop.is_set():
                try:
                    observed = owned_tree.refresh()
                    if process.poll() is not None and not any(row["live_identity_verified"] for row in observed):
                        raise RuntimeError(f"Owned Comfy process tree exited unexpectedly: {process.returncode}")
                    reading = resources(gpu, owned_tree)
                    PROJECT.append_line(run_rel + "/resources.jsonl", json.dumps(reading))
                    peaks = result["resource_peaks"]
                    peaks["minimum_ram_available_bytes"] = min(peaks["minimum_ram_available_bytes"], reading["ram_available_bytes"])
                    peaks["maximum_commit_used_bytes"] = max(peaks["maximum_commit_used_bytes"], reading["windows_commit_used_bytes"])
                    peaks["maximum_gpu_whole_device_used_mb"] = max(peaks["maximum_gpu_whole_device_used_mb"], reading["gpu_whole_device_mb"]["used_mb"])
                    memory_sample = reading["process_memory_bytes"] or {"aggregate_memory_bytes": {}}
                    peaks["maximum_process_memory_bytes"] = {key: max(peaks["maximum_process_memory_bytes"].get(key, 0), value)
                        for key, value in memory_sample["aggregate_memory_bytes"].items()}
                    peaks["owned_process_identities"] = memory_sample.get("identity_snapshot", [])
                    peaks["disk_free_bytes_by_drive"] = reading["disk_free_bytes_by_drive"]
                    low = low + 1 if reading["ram_available_bytes"] < 2 * GIB else 0
                    if low >= 3:
                        raise MemoryError("Available RAM below configured2GiB stop floor for3consecutive watchdog samples")
                    if time.monotonic() >= watchdog_deadline["at"]:
                        raise TimeoutError(f"Independent watchdog {watchdog_deadline['phase']} deadline exceeded")
                except Exception as exc:  # noqa: BLE001 - every observation failure must stop the owned child and propagate.
                    if watchdog_stop.is_set():
                        break
                    watchdog_faults.append(exc)
                    result["watchdog_failure"] = {"error_type": type(exc).__name__, "error": str(exc), "utc": utc()}
                    terminate_owned()
                    break
                watchdog_stop.wait(1.0)
        watchdog_thread = threading.Thread(target=watch, name="H3-parent-watchdog", daemon=False)
        watchdog_thread.start()
        while time.monotonic() < deadline:
            check_watchdog()
            if process.poll() is not None:
                raise RuntimeError(f"Owned Comfy process exited during startup: {process.returncode}")
            try:
                stats = http("/system_stats", timeout=2)
                break
            except (OSError, TimeoutError, json.JSONDecodeError):
                time.sleep(1)
        else:
            raise TimeoutError("Owned Comfy startup exceeded180s")
        if not any("RTX 5070 Ti Laptop" in d.get("name", "") and d.get("type") == "cuda" for d in stats.get("devices", [])):
            raise RuntimeError("Comfy system_stats did not report expected CUDA device; noCPUfallback accepted")
        check_watchdog()
        # Setup is still model-free; allow bounded schema/graph preparation before queueing.
        watchdog_deadline.update({"at": time.monotonic() + 180, "phase": "schema_preparation"})
        write_json(run_rel + "/system_stats.json", stats)
        info = http("/object_info", timeout=30)
        write_json(run_rel + "/object_info.json", info)
        graph, seed = build_graph(info, "model_proxy.mp4", PROJECT.read_text(result["prompt"]["path"]), staged=staged, audio_diagnostic=audio_diagnostic)
        write_json(run_rel + "/graph.json", graph)
        result["graph"] = identity(run_rel + "/graph.json")
        result["seed"] = seed
        result["runtime"]["schema"] = identity(run_rel + "/object_info.json")
        result["object_info"] = identity(run_rel + "/object_info.json")
        result["status_history"].append({"status": "prepared", "utc": utc()})
        if prepare_only:
            result["status"] = "prepared_not_queued"
            return result
        check_watchdog()
        watchdog_deadline.update({"at": time.monotonic() + 1800, "phase": "generation_and_export"})
        submitted = http("/prompt", {"prompt": graph, "client_id": run_id}, timeout=30)
        write_json(run_rel + "/prompt_response.json", submitted)
        if submitted.get("node_errors") or not submitted.get("prompt_id"):
            raise RuntimeError("Native prompt validation failed; response retained")
        prompt_id = submitted["prompt_id"]
        result["prompt_id"] = prompt_id
        result["status"] = "queued"
        result["status_history"].append({"status": "queued", "prompt_id": prompt_id, "utc": utc()})
        write_json(run_rel + "/generation.json", result)
        until = time.monotonic() + 1800
        next_history = 0.0
        history = None
        while time.monotonic() < until:
            check_watchdog()
            if process.poll() is not None:
                raise RuntimeError(f"Owned Comfy process exited: {process.returncode}")
            if time.monotonic() >= next_history:
                raw = http("/history/" + prompt_id, timeout=5)
                if prompt_id in raw:
                    history = raw[prompt_id]
                    write_json(run_rel + "/history.json", history)
                    messages = history.get("status", {}).get("messages", [])
                    if any(m[0] in ("execution_error", "execution_interrupted") for m in messages):
                        raise RuntimeError("Comfy history reports execution failure; exact evidence retained")
                    if history.get("status", {}).get("completed"):
                        break
                next_history = time.monotonic() + 3
            time.sleep(1)
        else:
            raise TimeoutError("Configured generation deadline1800s exceeded")
        check_watchdog()
        result["generation_wall_seconds"] = time.monotonic() - (until - 1800)
        if audio_diagnostic:
            receipt_rel = run_rel + "/audio_diagnostic/receipt.json"
            if not PROJECT.resolve(receipt_rel).is_file():
                raise RuntimeError("Audio diagnostic did not retain its required receipt")
            result["audio_diagnostic"] = identity(receipt_rel)
        result["media"], result["transforms"]["native_to_edit"] = export_media(run_rel, history, inputs, result, width=width, height=height)
        if staged:
            result["stage_events"] = identity(run_rel + "/stage_events.jsonl")
            result["stage_release"] = identity(run_rel + "/stage_release.json")
        check_watchdog()
        result["status"] = "generated_candidate_unapproved"
        result["status_history"].append({"status": result["status"], "utc": utc()})
        make_bundle(run_rel, result)
        check_watchdog()
        result["bundle"] = identity(run_rel + "/editor_bundle/project.json")
        return result
    except Exception as exc:
        result.update({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
        result["status_history"].append({"status": "failed", "utc": utc(), "error_type": type(exc).__name__, "error": str(exc)})
        raise
    finally:
        watchdog_stop.set()
        if watchdog_faults and result["status"] not in ("failed", "held_preflight"):
            fault = watchdog_faults[0]
            result.update({"status": "failed", "error_type": type(fault).__name__, "error": str(fault)})
            result["status_history"].append({"status": "failed", "utc": utc(), "error": str(fault)})
        try:
            terminate_owned()
        except Exception as exc:  # noqa: BLE001 - cleanup must become failed evidence, never skip finalization.
            result.update({"status": "failed", "error_type": type(exc).__name__, "error": str(exc),
                           "cleanup_failure": cleanup_error})
            result["status_history"].append({"status": "failed", "reason": "owned process cleanup unresolved", "utc": utc()})
        finally:
            if watchdog_thread is not None:
                watchdog_thread.join(timeout=15)
                if watchdog_thread.is_alive():
                    result.update({"status": "failed", "error": "Independent watchdog did not join within15s"})
            if process is not None:
                returncode = process.poll()
                cleanup = result.get("runtime", {}).get("owned_process_cleanup", {})
                result.setdefault("runtime", {}).update({"owned_server_stopped": cleanup.get("verified_stopped", False),
                    "returncode_after_stop": returncode,
                    "watchdog_thread_stopped": watchdog_thread is None or not watchdog_thread.is_alive(),
                    "owned_process_identities_final": owned_tree.snapshot() if owned_tree else []})
            if log is not None:
                log.close()
            if audio_diagnostic and PROJECT.resolve(run_rel + "/audio_diagnostic/receipt.json").is_file():
                result["audio_diagnostic"] = identity(run_rel + "/audio_diagnostic/receipt.json")
            if staged:
                if PROJECT.resolve(run_rel + "/stage_events.jsonl").is_file():
                    result["stage_events"] = identity(run_rel + "/stage_events.jsonl")
                if PROJECT.resolve(run_rel + "/stage_release.json").is_file():
                    result["stage_release"] = identity(run_rel + "/stage_release.json")
            result["finished_utc"] = utc()
            result["wall_seconds"] = time.monotonic() - started
            result["resource_observation_scope"] = "whole-deviceGPU/systemRAM/Windowscommit/owned-process-tree memory observations; notattributableminimumdemand"
            result["code"] = identity(SPIKE + "/local_h3.py")
            write_json(run_rel + "/generation.json", result)
            write_json(run_rel + "/editor_manifest.json", result)
            print(json.dumps({"status": result["status"], "generation": run_rel + "/generation.json"}), flush=True)


def audio_metadata(rel: str, run_rel: str, label: str) -> dict[str, Any]:
    import array
    import math
    import re
    media = probe(rel, run_rel, label + "_probe")
    streams = [s for s in media["streams"] if s["codec_type"] == "audio"]
    if len(streams) != 1:
        raise ValueError("Expected exactly one generated audio stream")
    stream = streams[0]
    raw_rel = run_rel + "/analysis/" + label + ".f32le"
    def decode(path: Path) -> None:
        command(["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(PROJECT.resolve(rel)),
                 "-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "f32le", str(path)],
                run_rel, label + "_decode")
    PROJECT.write_with(raw_rel, decode)
    samples = array.array("f")
    samples.frombytes(PROJECT.read_bytes(raw_rel))
    if sys.byteorder != "little":
        samples.byteswap()
    finite = [value for value in samples if math.isfinite(value)]
    count = len(samples)
    channels = int(stream["channels"])
    if count % channels:
        raise ValueError("Decoded sample count is not channel aligned")
    stats = command(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(PROJECT.resolve(rel)), "-map", "0:a:0",
                     "-af", "ebur128=peak=true", "-f", "null", "-"], run_rel, label + "_loudness")
    summary = stats.stderr.rsplit("Summary:", 1)[-1]
    lufs = re.search(r"I:\s+([-+\w.]+) LUFS", summary)
    peak = re.search(r"Peak:\s+([-+\w.]+) dBFS", summary)
    def metric(match: Any) -> float | str:
        if match is None:
            raise ValueError("FFmpeg ebur128 summary lacks required loudness/truepeak measurement")
        value = float(match.group(1))
        return value if math.isfinite(value) else str(value)
    metrics = {"decoded_values": count, "nonfinite_samples": count - len(finite),
               "clipped_samples": sum(abs(v) >= 1 for v in finite),
               "sample_peak": max((abs(v) for v in finite), default=0),
               "rms": math.sqrt(math.fsum(v * v for v in finite) / len(finite)) if finite else 0,
               "dc": math.fsum(finite) / len(finite) if finite else 0,
               "integrated_lufs": metric(lufs), "true_peak_dbfs": metric(peak)}
    if metrics["nonfinite_samples"]:
        raise ValueError(f"Decoded audio has nonfinite values: {metrics['nonfinite_samples']}")
    return {**identity(rel), "metadata": media, "sample_rate": int(stream["sample_rate"]),
            "channels": channels, "sample_count": count // channels, "start_sample": 0,
            "time_base": stream["time_base"], "metrics": metrics,
            "decoded_analysis": identity(raw_rel)}


def export_media(run_rel: str, history: dict[str, Any], inputs: dict[str, Any],
                 generation: dict[str, Any], *, width: int = 640, height: int = 480) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = []
    for output in history.get("outputs", {}).values():
        for values in output.values():
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict) and "filename" in item and item.get("type") == "output":
                        filename = item["filename"]
                        if filename.lower().endswith((".mp4", ".mkv", ".mov", ".webm")):
                            rel = run_rel + "/output/" + (item.get("subfolder", "") + "/" if item.get("subfolder") else "") + filename
                            PROJECT.resolve(rel)
                            candidates.append(rel)
    candidates = sorted(set(candidates))
    if len(candidates) != 1:
        raise ValueError(f"Expected exactly one native generated AV file, found {len(candidates)}")
    native_av = candidates[0]
    av_probe = probe(native_av, run_rel, "generated_av_probe")
    videos = [s for s in av_probe["streams"] if s["codec_type"] == "video"]
    audios = [s for s in av_probe["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError("Native output must contain joint generated video and audio")
    v = videos[0]
    if (v["width"], v["height"], v["avg_frame_rate"], int(v["nb_read_frames"])) != (width, height, "24/1", 192):
        raise ValueError("Native generated output canvas/rate/framecount differs")
    command(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(PROJECT.resolve(native_av)),
             "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], run_rel, "generated_av_full_decode")
    media_dir = run_rel + "/media"
    PROJECT.make_dir(media_dir)
    native_wav = media_dir + "/native_decoded_audio.wav"
    PROJECT.write_with(native_wav, lambda path: command(
        ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(PROJECT.resolve(native_av)),
         "-map", "0:a:0", "-vn", "-c:a", "pcm_f32le", "-f", "wav", str(path)], run_rel, "native_wav_extract"))
    native_meta = audio_metadata(native_wav, run_rel, "native_wav")
    native_meta["identity_label"] = "extracted/decoded native-rate audio from encoded nativeAV; not precodec modelwaveform"
    resampled = media_dir + "/resampled_unfitted_pcm24.wav"
    PROJECT.write_with(resampled, lambda path: command(
        ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(PROJECT.resolve(native_wav)),
         "-af", "aresample=48000", "-c:a", "pcm_s24le", "-f", "wav", str(path)], run_rel, "edit_resample"))
    resampled_meta = audio_metadata(resampled, run_rel, "resampled_unfitted")
    target_count = 8 * 48000
    original_count = resampled_meta["sample_count"]
    trim = max(0, original_count - target_count)
    pad = max(0, target_count - original_count)
    edit = media_dir + "/editing_48k_pcm24.wav"
    if trim or pad:
        PROJECT.write_with(edit, lambda path: command(
            ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(PROJECT.resolve(resampled)),
             "-af", "apad=whole_len=384000,atrim=end_sample=384000", "-c:a", "pcm_s24le", "-f", "wav", str(path)],
            run_rel, "editor_timeline_fit"))
    else:
        with PROJECT.resolve(resampled).open("rb") as stream:
            PROJECT.write_stream(edit, stream)
    edit_meta = audio_metadata(edit, run_rel, "edit_wav")
    if edit_meta["sample_rate"] != 48000 or edit_meta["sample_count"] != target_count:
        raise ValueError("Editor PCM timeline differs from explicit 8second/48kHz sample grid")
    audition = media_dir + "/audition_original_video.mkv"
    PROJECT.write_with(audition, lambda path: command(
        ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-i", str(PROJECT.resolve(inputs["source"]["input_video"]["path"])),
         "-i", str(PROJECT.resolve(edit)), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
         "-f", "matroska", str(path)], run_rel, "audition_mux"))
    audition_probe = probe(audition, run_rel, "audition_probe")
    command(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(PROJECT.resolve(audition)),
             "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], run_rel, "audition_full_decode")
    transforms = {"resample_to_hz": 48000, "format": "pcm_s24le", "gain_db": 0,
                  "native_rate_hz": native_meta["sample_rate"], "native_sample_count": native_meta["sample_count"],
                  "resampled_unfitted": resampled_meta, "trim_samples": trim, "pad_samples": pad,
                  "fit_policy": "explicit tail trim or zero padding only on separate editor audio to384000samples",
                  "native_media_unchanged": True, "normalization": None,
                  "native_node_amplitude_processing": "stock VAEDecodeAudio audio /= max(std(audio)*5,1) before nativeAV encoding; no claim of raw precodec modelwaveform",
                  "audition_video": "canonical original video streamcopy",
                  "audition_audio": "editing48kPCM24 streamcopy", "shortest_used": False}
    return {"generated_av": {**identity(native_av), "metadata": av_probe},
            "native_wav": native_meta, "edit_wav": edit_meta,
            "audition": {**identity(audition), "metadata": audition_probe}}, transforms


def source_notices(source_manifest: dict[str, Any]) -> dict[str, Any]:
    """Retain available current source JSON license/credit evidence without changing it."""
    source_path = source_manifest["source"]["path"]
    project_rel = source_path.split("/renders/", 1)[0]
    shot_rel = project_rel + "/episodes/ep1/shots/s1/v0002.json"
    shot = read_json(shot_rel)
    if shot.get("degrade_seed") != 0:
        raise ValueError("Actual source shot degrade_seed changed from authorized seed inputs")
    paths = [project_rel + "/project.json", shot_rel]
    asset_root = PROJECT.resolve(project_rel + "/assets")
    if asset_root.is_dir():
        paths.extend(str(path.relative_to(ROOT)).replace("\\", "/") for path in sorted(asset_root.rglob("*.json")))
    return {"current_source_snapshots": [identity(path) for path in paths],
            "source_metadata": {path: read_json(path) for path in paths},
            "rights_status": source_manifest["source"]["upstream_rights_status"],
            "local_internal_test_only": True, "creator_approval": "pending"}


def make_bundle(run_rel: str, generation: dict[str, Any]) -> None:
    base = run_rel + "/editor_bundle"
    PROJECT.make_dir(base)
    files: dict[str, Any] = {}
    def copy(label: str, source_rel: str, bundle_rel: str) -> None:
        destination = base + "/" + bundle_rel
        with PROJECT.resolve(source_rel).open("rb") as stream:
            PROJECT.write_stream(destination, stream)
        actual = identity(destination)
        files[label] = {**actual, "path": bundle_rel}
    copy("original_video", generation["sources"]["canonical"]["path"], "media/original_video.mp4")
    copy("model_proxy", generation["sources"]["proxy"]["path"], "media/model_proxy.mp4")
    for name, record in generation["media"].items():
        copy(name, record["path"], "media/" + Path(record["path"]).name)
    copy("graph", generation["graph"]["path"], "provenance/graph.json")
    copy("prompt", generation["prompt"]["path"], "provenance/prompt.txt")
    copy("acquisition_receipt", generation["acquisition_receipt"]["path"], "provenance/acquisition_receipt.json")
    copy("catalog", SPIKE + "/LOCAL_MODEL_CATALOG.json", "provenance/model_catalog.json")
    copy("object_info", generation["object_info"]["path"], "provenance/object_info.json")
    if generation.get("audio_diagnostic"):
        for source_name, bundle_name in (("receipt.json", "receipt.json"), ("tensors.safetensors", "tensors.safetensors"), ("raw_decoded_audio.wav", "raw_decoded_audio.wav"), ("post_native_audio.wav", "post_native_audio.wav")):
            copy("audio_diagnostic_" + source_name.split(".")[0], run_rel + "/audio_diagnostic/" + source_name, "diagnostic/" + bundle_name)
    if generation.get("stage_events"):
        copy("stage_events", generation["stage_events"]["path"], "provenance/stage_events.jsonl")
    if generation.get("stage_release"):
        copy("stage_release", generation["stage_release"]["path"], "provenance/stage_release.json")
    model_docs = generation["provenance"]["model_notices"]
    for index, doc in enumerate(model_docs):
        copy("model_notice_" + str(index), doc["actual"]["path"], "notices/" + Path(doc["actual"]["path"]).name)
    credits = source_notices(generation["provenance"]["source_manifest"])
    credits_rel = base + "/credits.json"
    write_json(credits_rel, credits)
    files["credits"] = {**identity(credits_rel), "path": "credits.json"}
    portable = {"schema_version": 1, "status": "new_candidate_unapproved", "media_path_base": "bundle_root",
        "shot_id": "s1", "shot_version": 2, "source_offset_seconds": 0,
        "fps": "24/1", "time_base": "1/24", "start_sample": 0, "timeline_sample_rate": 48000, "timeline_sample_count": 384000,
        "files": files, "seed": generation["seed"], "transforms": {
            "canonical_to_proxy": generation["transforms"]["canonical_to_proxy"],
            "native_to_edit": {k: v for k, v in generation["transforms"]["native_to_edit"].items() if k != "resampled_unfitted"}},
        "media_metadata": {key: {k: v for k, v in value.items() if k not in ("path", "decoded_analysis")}
                           for key, value in generation["media"].items()},
        "provenance": {"model_documentation": [{k: v for k, v in doc.items() if k != "actual"} for doc in model_docs],
                       "source_credits": "credits.json", "model_runtime_required_to_open": False,
                       "creator_approval": "pending", "quality": "pending creator audition"}}
    write_json(base + "/project.json", portable)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinned local H3 acquisition and bounded native video+text diagnostic")
    parser.add_argument("action", choices=("acquire", "run", "prepare-only", "run-staged", "prepare-staged", "run-audio-diagnostic", "prepare-audio-diagnostic"))
    parser.add_argument("--runtime-verified", action="store_true",
                        help="Parent has completed and adjudicated model-free runtime verification")
    parser.add_argument("--retry-transient", action="store_true",
                        help="Explicitly permit at most one retry after a transient acquisition failure")
    args = parser.parse_args()
    if not args.runtime_verified:
        parser.error("Parent runtime verification gate is required before model acquisition or runtime use")
    if args.action == "acquire":
        receipt = acquire(retry_transient=args.retry_transient)
        print(json.dumps({"status": receipt["status"], "receipt": MODEL + "/acquisition_receipt.json"}), flush=True)
    else:
        audio_diagnostic = args.action in ("run-audio-diagnostic", "prepare-audio-diagnostic")
        staged = args.action in ("run-staged", "prepare-staged") or audio_diagnostic
        run_local(prepare_only=args.action in ("prepare-only", "prepare-staged", "prepare-audio-diagnostic"), staged=staged, audio_diagnostic=audio_diagnostic)


if __name__ == "__main__":
    main()

