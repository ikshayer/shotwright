"""Actual CPU launcher/descendant cancellation; no CUDA/model inference."""
import importlib.util
import json
import subprocess
import time
import types
import uuid
from pathlib import Path

import psutil
import pytest

from orchestrator.platform.containment import ProjectRoot

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_actual_native_supervisor_baseexception_cleanup(interrupt):
    root = ProjectRoot(ROOT)
    spec = importlib.util.spec_from_file_location("independent_cancel_target", ROOT / "spikes/23_joint_av_analyzer/analyze.py")
    target = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(target)
    prefix = "_work/qwen_contract_tests/cancellation/" + uuid.uuid4().hex
    dummy = root.write_text(prefix + "/sleeper.py", "import time\ntime.sleep(90)\n")
    python = root.resolve("spikes/23_joint_av_analyzer/_work/env/Scripts/python.exe")
    assert python.is_file(), "native isolated launcher required for this CPU test"
    processes = []
    identities = {}
    sentinel = subprocess.Popen([str(python), "-B", str(dummy)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    sentinel_identity = psutil.Process(sentinel.pid).create_time()

    def alive(pid, created):
        try:
            process = psutil.Process(pid)
            return process.create_time() == created and process.is_running()
        except psutil.NoSuchProcess:
            return False

    def spawn(argv, **kwargs):
        assert Path(argv[0]).resolve() == python.resolve()
        process = subprocess.Popen([argv[0], "-B", str(dummy)], **kwargs)
        processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            parent = psutil.Process(process.pid)
            for item in [parent, *parent.children(recursive=True)]:
                identities[item.pid] = item.create_time()
            if len(identities) >= 2:
                break
            time.sleep(0.02)
        assert len(identities) >= 2, "actual isolated Python launcher descendant absent"
        return process

    def interrupted_sleep(seconds):
        raise interrupt("independent CPU interruption")

    target.subprocess = types.SimpleNamespace(**{
        key: getattr(subprocess, key) for key in dir(subprocess) if key != "Popen"
    }, Popen=spawn)
    target.time = types.SimpleNamespace(monotonic=time.monotonic, sleep=interrupted_sleep)
    target._gpu_memory_sample = lambda: {"index": 0, "used_mib": 0, "free_mib": 0,
                                       "test_stub": "CPU cancellation; no GPU queried"}
    target._ram_available = lambda: 10 * 1024**3
    try:
        with pytest.raises(interrupt):
            target.run_supervised([], "audio", 1, 5, prefix + "/result.json",
                                  prefix + "/stdout.log", prefix + "/stderr.log",
                                  prefix + "/resources.jsonl")
        assert not any(alive(pid, created) for pid, created in identities.items())
        assert alive(sentinel.pid, sentinel_identity), "unrelated sentry was stopped"
        records = [json.loads(line) for line in root.read_text(prefix + "/resources.jsonl").splitlines()]
        failure = next(row for row in records if row.get("event") == "supervisor_failure")
        assert interrupt.__name__ in failure["error"]
        assert failure["cleanup_error"] is None
        root.write_text(prefix + "/independent_evidence.json", json.dumps({
            "owned_identities": identities, "sentry_pid": sentinel.pid,
            "sentry_survived": True, "owned_gone": True, "exception": interrupt.__name__,
            "substitutions": ["CPU sleeper argv", "GPU sampler disabled", "RAM 10GiB", "sleep raises BaseException"],
        }, indent=2))
    finally:
        for pid, created in reversed(list(identities.items())):
            if alive(pid, created):
                psutil.Process(pid).kill()
        for process in processes:
            process.wait(timeout=10)
        for process in reversed([psutil.Process(sentinel.pid), *psutil.Process(sentinel.pid).children(recursive=True)]):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        sentinel.wait(timeout=10)
