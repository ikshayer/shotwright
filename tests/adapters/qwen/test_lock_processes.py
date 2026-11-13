"""Real competing adapter processes with blocking synthetic CPU backend."""

import json
import os
import subprocess
import sys
import time

import psutil

from tests.adapters.qwen.test_runner_contract import REPO


def test_nonblocking_helper_lock_is_stable_across_processes(project):
    script = project.write_text(
        "competitor.py",
        """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from orchestrator.adapters.qwen.core import AnalysisRequest
from orchestrator.adapters.qwen.runner import QwenAdapter
from orchestrator.platform.containment import ProjectRoot
from tests.adapters.qwen.test_batches import BatchBackend
root = ProjectRoot(sys.argv[2])
class BlockingBackend(BatchBackend):
    def prepare(self, *args, **kwargs):
        if sys.argv[3] == 'holder':
            root.write_text('ready', 'ready')
            deadline = time.monotonic()+25
            while not root.resolve('release').exists() and time.monotonic()<deadline:
                time.sleep(.02)
        return super().prepare(*args, **kwargs)
try:
    report = QwenAdapter(root, backend=BlockingBackend(root)).run(AnalysisRequest('media.mkv'))
    print(json.dumps({'status': report['status']}))
except Exception as exc:
    print(json.dumps({'type': type(exc).__name__, 'error': str(exc)}))
    sys.exit(3)
""",
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [sys.executable, "-B", str(script), str(REPO), str(project.path)]
    holder = subprocess.Popen(
        [*argv, "holder"], cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 10
        while not project.resolve("ready").exists() and time.monotonic() < deadline:
            assert holder.poll() is None, holder.communicate()
            time.sleep(0.02)
        assert project.resolve("ready").exists(), "holder did not reach its contained preparation"
        locks = [p for p in project.path.rglob("*") if p.is_file() and "lock" in p.name.lower()]
        expected_paths = {
            "spikes/23_joint_av_analyzer/_work/helper.lock",
            "_work/gpu_execution.lock",
        }
        assert {p.relative_to(project.path).as_posix() for p in locks} == expected_paths
        identities = {
            relative: project.resolve(relative).stat().st_ino for relative in expected_paths
        }
        assert all(identities.values())
        began = time.monotonic()
        contender = subprocess.run(
            [*argv, "contender"], cwd=REPO, env=env, check=False, capture_output=True, timeout=5
        )
        assert time.monotonic() - began < 5
        assert contender.returncode != 0, contender.stdout
        error = json.loads(contender.stdout)
        assert error["type"] == "QwenSetupError", error
        assert "busy" in error["error"].lower() or "lock" in error["error"].lower()
        for relative, identity in identities.items():
            assert project.resolve(relative).is_file()
            assert project.resolve(relative).stat().st_ino == identity
        project.write_text("release", "release")
        stdout, stderr = holder.communicate(timeout=15)
        assert holder.returncode == 0, stderr
        assert json.loads(stdout)["status"] == "completed"
        for relative, identity in identities.items():
            assert project.resolve(relative).is_file()
            assert project.resolve(relative).stat().st_ino == identity
        third = subprocess.run(
            [*argv, "contender"], cwd=REPO, env=env, check=False, capture_output=True, timeout=15
        )
        assert third.returncode == 0, third.stderr
        assert json.loads(third.stdout)["status"] == "completed"
        for relative, identity in identities.items():
            assert project.resolve(relative).is_file()
            assert project.resolve(relative).stat().st_ino == identity
    finally:
        if holder.poll() is None:
            for child in psutil.Process(holder.pid).children(recursive=True):
                child.kill()
            holder.kill()
            holder.wait(timeout=10)
