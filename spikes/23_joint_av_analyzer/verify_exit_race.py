"""Independent Amendment 6 CPU probes; no model, GPU, downloads, or installs."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import psutil

from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
BASE = "spikes/23_joint_av_analyzer/_work/verification/exit_race"
REAL_PROCESS = psutil.Process
Memory = namedtuple("Memory", "rss private vms uss")
ROWS: list[dict] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_helper():
    spec = importlib.util.spec_from_file_location(
        "exit_race_analyzer", ROOT / "spikes/23_joint_av_analyzer/analyze.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._load_process_tree_module()


def live(pid: int, created: float) -> bool:
    try:
        process = REAL_PROCESS(pid)
        return (
            process.create_time() == created
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


def remember(process: subprocess.Popen, known: dict[int, float]) -> list[dict]:
    parent = REAL_PROCESS(process.pid)
    for member in [parent, *parent.children(recursive=True)]:
        known.setdefault(member.pid, member.create_time())
    return [
        {"pid": pid, "create_time": created, "rss": REAL_PROCESS(pid).memory_info().rss}
        for pid, created in known.items()
        if live(pid, created)
    ]


def cleanup(process: subprocess.Popen, known: dict[int, float]) -> None:
    """Emergency cleanup is restricted to identities captured from our own child."""
    for pid, created in reversed(list(known.items())):
        if live(pid, created):
            try:
                REAL_PROCESS(pid).kill()
            except psutil.NoSuchProcess:
                pass
    process.wait(timeout=10)
    deadline = time.monotonic() + 10
    while any(live(pid, created) for pid, created in known.items()):
        require(time.monotonic() < deadline, "test-owned process survived emergency cleanup")
        time.sleep(0.03)


def launch(command: list[str]) -> tuple[subprocess.Popen, dict[int, float]]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return process, {process.pid: REAL_PROCESS(process.pid).create_time()}


class Fault:
    """Wrap actual psutil calls; only the selected fault and fresh identity are synthetic."""

    def __init__(self, target: int, outcome: str, stage: str):
        self.target = target
        self.outcome = outcome
        self.stage = stage
        self.triggered = False
        self.fresh_calls = 0
        self.identity_reads = 0
        self.signal_attempts = []
        self.initial_create_time = REAL_PROCESS(target).create_time()

    def factory(self, pid=None):
        if pid is None:
            pid = os.getpid()
        fresh = self.triggered and pid == self.target
        if fresh:
            self.fresh_calls += 1
            if self.outcome == "constructor_error":
                raise RuntimeError("injected fresh Process identity failure")
            if self.outcome == "gone":
                raise psutil.NoSuchProcess(pid)
        return Proxy(REAL_PROCESS(pid), self, fresh)


class Proxy:
    def __init__(self, actual, fault: Fault, fresh: bool):
        self.actual = actual
        self.fault = fault
        self.fresh = fresh
        self.pid = actual.pid

    def __getattr__(self, key):
        return getattr(self.actual, key)

    def create_time(self):
        if self.fresh:
            self.fault.identity_reads += 1
            if self.fault.outcome == "create_time_error":
                raise RuntimeError("injected fresh create_time failure")
            if self.fault.outcome == "identity_denied":
                raise psutil.AccessDenied(self.pid)
            if self.fault.outcome == "zombie":
                raise psutil.ZombieProcess(self.pid)
            if self.fault.outcome in ("reused", "reused_status_denied"):
                return self.fault.initial_create_time + 60
        return self.actual.create_time()

    def status(self):
        if self.fresh:
            if self.fault.outcome == "reused_status_denied":
                raise psutil.AccessDenied(self.pid)
            if self.fault.outcome == "status_error":
                raise RuntimeError("injected fresh status failure")
            if self.fault.outcome == "zombie_status":
                return psutil.STATUS_ZOMBIE
        return self.actual.status()

    def terminate(self):
        self.fault.signal_attempts.append({"pid": self.pid, "signal": "terminate"})
        return self.actual.terminate()

    def kill(self):
        self.fault.signal_attempts.append({"pid": self.pid, "signal": "kill"})
        return self.actual.kill()

    def _inject(self, stage):
        if self.pid == self.fault.target and stage == self.fault.stage:
            self.fault.triggered = True
            raise psutil.AccessDenied(self.pid)

    def memory_info(self):
        self._inject("memory_info")
        return Memory(rss=31337, private=22222, vms=44444, uss=11111)

    def memory_full_info(self):
        self._inject("memory_full_info")
        return Memory(rss=31337, private=22222, vms=44444, uss=11111)

    def cmdline(self):
        self._inject("cmdline")
        return self.actual.cmdline()


class PsutilView:
    """Patch the consumer reference without changing psutil's own internal globals."""

    def __init__(self, fault: Fault):
        self.Process = fault.factory

    def __getattr__(self, key):
        return getattr(psutil, key)


def matching_event(events: list[dict], pid: int, created: float) -> dict:
    require(isinstance(events, list) and events, "missing structured observation_events")
    found = [event for event in events if event.get("kind") == "sample_exit_race"]
    if not found:
        # Contract specifies an event label, but does not prescribe its field name.
        found = [event for event in events if "sample_exit_race" in event.values()]
    require(bool(found), "missing sample_exit_race event")
    event = found[-1]
    require(event.get("pid") == pid, "event lost denied PID")
    require(
        event.get("create_time", event.get("created", event.get("expected_create_time"))) == created,
        "event lost captured creation time",
    )
    require(bool(event.get("error")), "event lost AccessDenied error")
    return event


def accepted_fault(helper, outcome: str, stage: str) -> dict:
    process, known = launch([sys._base_executable, "-B", "-c", "import time;time.sleep(60)"])
    tracker = helper.OwnedProcessTree(process)
    detail: dict = {"fault": outcome, "stage": stage, "owned": known}
    ROWS[-1]["evidence"] = detail
    try:
        original = tracker.sample()
        require(original["processes"], "initial real sample empty")
        fault = Fault(process.pid, outcome, stage)
        with patch.dict(sys.modules, {"psutil": PsutilView(fault)}):
            sampled = tracker.sample()
        detail["fault_sample"] = sampled
        detail["fresh_process_calls"] = fault.fresh_calls
        detail["fresh_identity_reads"] = fault.identity_reads
        require(fault.triggered, f"{stage} fault was not exercised")
        require(fault.fresh_calls > 0, "AccessDenied did not trigger fresh psutil PID lookup")
        if outcome != "gone":
            require(fault.identity_reads > 0, "fresh creation-time check was not exercised")
        require(not sampled["processes"], "skipped identity retained an incomplete process row")
        require(
            all(value == 0 for value in sampled["aggregate_memory_bytes"].values()),
            "skipped identity leaked partial memory into aggregate totals",
        )
        require(not sampled["observation_failures"], "conclusive exit became persistent failure")
        event = matching_event(sampled.get("observation_events"), process.pid, known[process.pid])
        require(sampled["identity_snapshot"], "sample lost captured identity snapshot")
        recovered = tracker.sample()
        detail["healthy_sample"] = recovered
        require(event in recovered.get("observation_events", []), "healthy sample cleared observations")
        stopped = tracker.stop()
        detail["stop"] = stopped
        require(event in stopped.get("observation_events", []), "stop discarded observation events")
        require(stopped["verified_stopped"] and not stopped["survivors"], "cleanup not verified")
        require(not live(process.pid, known[process.pid]), "owned process survived cleanup")
        return detail
    finally:
        cleanup(process, known)


def rejected_fault(helper, outcome: str) -> dict:
    process, known = launch([sys._base_executable, "-B", "-c", "import time;time.sleep(60)"])
    tracker = helper.OwnedProcessTree(process)
    detail: dict = {"fault": outcome, "owned": known}
    ROWS[-1]["evidence"] = detail
    try:
        tracker.sample()
        fault = Fault(process.pid, outcome, "memory_full_info")
        raised = None
        with patch.dict(sys.modules, {"psutil": PsutilView(fault)}):
            try:
                detail["unexpected_sample"] = tracker.sample()
            except Exception as exc:  # noqa: BLE001 - exception type is not contracted
                raised = f"{type(exc).__name__}: {exc}"
        detail["exception"] = raised
        detail["fresh_process_calls"] = fault.fresh_calls
        require(fault.triggered, "memory_full_info AccessDenied not exercised")
        require(fault.fresh_calls > 0, "denial omitted fresh identity lookup")
        require(raised is not None, "uncertain/live denied identity was silently accepted")
        failures = []
        try:
            healthy = tracker.sample()
            detail["healthy_sample"] = healthy
            failures = healthy["observation_failures"]
            require(bool(failures), "healthy sample cleared historical observation failure")
            require(not healthy.get("observation_events"), "persistent failure mislabeled benign exit")
        except RuntimeError as exc:
            detail["healthy_sample_persistent_exception"] = str(exc)
        cleanup(process, known)
        detail["later_identity_absence_verified"] = True
        try:
            stopped = tracker.stop()
        except RuntimeError as exc:
            detail["stop_persistent_exception"] = str(exc)
            message = str(exc)
            require("{" in message, "failed stop omitted structured cleanup evidence")
            stopped = json.loads(message[message.index("{"):])
        detail["stop"] = stopped
        retained = stopped.get("observation_failures", stopped.get("failures", []))
        require(bool(retained), "stop discarded historical failures")
        require(
            any(failure.get("pid") == process.pid for failure in retained),
            "persistent failure lost owned PID",
        )
        require(
            all(failure in retained for failure in failures),
            "stop discarded historical failure details",
        )
        require(not stopped["verified_stopped"], "historical failure allowed cleanup acceptance")
        require(not stopped["survivors"], "owned process survived failed observation cleanup")
        return detail
    finally:
        cleanup(process, known)


def persistent_zombie(helper) -> dict:
    process, known = launch([sys._base_executable, "-B", "-c", "import time;time.sleep(60)"])
    tracker = helper.OwnedProcessTree(process)
    detail: dict = {"owned": known}
    ROWS[-1]["evidence"] = detail
    try:
        tracker.sample()
        fault = Fault(process.pid, "zombie_status", "memory_full_info")
        with patch.dict(sys.modules, {"psutil": PsutilView(fault)}):
            sampled = tracker.sample()
            detail["sample"] = sampled
            require(not sampled["processes"], "status-zombie retained memory row")
            matching_event(sampled.get("observation_events"), process.pid, known[process.pid])
            require(
                all(not row["live_identity_verified"] for row in sampled["identity_snapshot"]),
                "conclusive zombie snapshot still claims live identity",
            )
            stopped = tracker.stop()
            detail["stop"] = stopped
            detail["signal_attempts"] = fault.signal_attempts
            require(not fault.signal_attempts, "stop attempted a signal on conclusive zombie")
            require(stopped["verified_stopped"] and not stopped["survivors"], "zombie cleanup unverified")
        return detail
    finally:
        cleanup(process, known)


def lifecycle(helper, invocation: str) -> dict:
    require(os.name == "nt", "Windows lifecycle cannot be verified on this platform")
    python = PROJECT.resolve("spikes/23_joint_av_analyzer/_work/env/Scripts/python.exe")
    require(python.is_file(), "existing isolated Windows launcher missing")
    dummy = PROJECT.write_text(
        invocation + "/allocated_worker.py",
        "import time\npayload=bytearray(64*1024*1024)\n"
        "for i in range(0,len(payload),4096): payload[i]=1\ntime.sleep(90)\n",
    )
    sentinel, sentinel_known = launch([str(python), "-B", str(dummy)])
    evidence: dict = {}
    try:
        deadline = time.monotonic() + 12
        while True:
            sentinel_rows = remember(sentinel, sentinel_known)
            if len(sentinel_rows) >= 2 and sum(row["rss"] for row in sentinel_rows) > 64*1024*1024:
                break
            require(time.monotonic() < deadline, "sentinel launcher/base-child not established")
            time.sleep(0.03)
        process, known = launch([str(python), "-B", str(dummy)])
        tracker = helper.OwnedProcessTree(process)
        try:
            deadline = time.monotonic() + 12
            while True:
                independent = remember(process, known)
                sampled = tracker.sample()
                if len(independent) >= 2 and sum(row["rss"] for row in independent) > 64*1024*1024:
                    break
                require(time.monotonic() < deadline, "owned launcher/base-child not established")
                time.sleep(0.03)
            rows = {row["pid"]: row for row in sampled["processes"]}
            require(set(known) <= set(rows), "sample omitted actual interpreter descendant")
            require(
                all(rows[pid]["create_time"] == created for pid, created in known.items()),
                "sample captured incorrect PID/create-time identity",
            )
            require(
                sampled["aggregate_memory_bytes"]["rss"] == sum(
                    row["memory_bytes"]["rss"] for row in sampled["processes"]
                ),
                "aggregate RSS does not equal complete process rows",
            )
            require(sampled["aggregate_memory_bytes"]["rss"] > 64*1024*1024, "RSS lost child allocation")
            snapshot = {row["pid"]: row for row in sampled["identity_snapshot"]}
            require(set(known) <= set(snapshot), "identity snapshot omitted descendant")
            stopped = tracker.stop()
            require(stopped["verified_stopped"] and not stopped["survivors"], "owned stop unverified")
            require(not any(live(pid, created) for pid, created in known.items()), "owned identity survived")
            require(
                all(live(pid, created) for pid, created in sentinel_known.items()),
                "unrelated sentinel identity was stopped",
            )
            evidence = {
                "independent_processes": independent, "sample": sampled, "stop": stopped,
                "sentinel_identities": sentinel_known, "sentinel_survival_verified": True,
            }
        finally:
            cleanup(process, known)
        return evidence
    finally:
        cleanup(sentinel, sentinel_known)


def run_check(name: str, operation) -> None:
    row = {"name": name, "status": "fail"}
    ROWS.append(row)
    try:
        row["evidence"] = operation()
        row["status"] = "pass"
    except Exception as exc:  # noqa: BLE001 - durable report must retain every case
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["traceback"] = traceback.format_exc()
    print(f"{row['status']}: {name}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faults-only", action="store_true")
    args = parser.parse_args()
    invocation = BASE + "/" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
    helper = load_helper()
    for stage in ("memory_full_info", "cmdline"):
        for outcome in ("gone", "zombie", "reused", "reused_status_denied"):
            run_check(
                stage + "_" + outcome,
                lambda outcome=outcome, stage=stage: accepted_fault(helper, outcome, stage),
            )
    for outcome in ("live_denied", "identity_denied", "constructor_error", "create_time_error", "status_error"):
        run_check(outcome, lambda outcome=outcome: rejected_fault(helper, outcome))
    run_check("persistent_status_zombie", lambda: persistent_zombie(helper))
    if not args.faults_only:
        run_check("real_windows_launcher_child_and_sentinel", lambda: lifecycle(helper, invocation))
    run_check("python_ast", lambda: {"parsed": bool(ast.parse(Path(__file__).read_text(encoding="utf-8")))})
    helper_path = PROJECT.resolve("spikes/22_minimax_h3_workflows/local_h3.py")
    report = {
        "utc": datetime.now(UTC).isoformat(), "checks": ROWS,
        "helper_sha256": hashlib.sha256(helper_path.read_bytes()).hexdigest(),
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": [
            "No GPU, model inference, resource guard, or analyzer supervisor verification.",
            "Exit race identity outcomes are injected; launcher, identities, memory, cleanup and sentinel are real.",
        ] + (["Windows launcher lifecycle explicitly omitted by --faults-only."] if args.faults_only else []),
    }
    report["passed"] = all(row["status"] == "pass" for row in ROWS)
    path = PROJECT.write_text(invocation + "/report.json", json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(path, flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
