"""Independent bounded process watchdog for the spike runtime."""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

R = ProjectRoot(ROOT)
B = "spikes/21_video_foley/_work"


class Mem(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (n, ctypes.c_ulonglong) for n in ["total", "available", "pt", "pa", "vt", "va", "e"]
    ]


def ram():
    m = Mem()
    m.length = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.available / 2**30


p = argparse.ArgumentParser()
p.add_argument("--proxy", required=True)
p.add_argument("--out", required=True)
p.add_argument("--seconds", required=True, choices=[3, 8, 15], type=int)
p.add_argument("--prompt")
p.add_argument("--negative-prompt")
p.add_argument("--save-latents", action="store_true")
a = p.parse_args()
assert ram() > 6, "Diagnostic admission floor: need6GiB free, not a measured model requirement"
R.make_dir(a.out)
env = os.environ.copy()
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["PYTHONFAULTHANDLER"] = "1"
for k, v in {
    "TMP": "tmp",
    "TEMP": "tmp",
    "TMPDIR": "tmp",
    "HF_HOME": "cache/hf",
    "TORCH_HOME": "cache/torch",
    "XDG_CACHE_HOME": "cache/xdg",
    "NUMBA_CACHE_DIR": "cache/numba",
    "MPLCONFIGDIR": "cache/mpl",
    "CUDA_CACHE_PATH": "cache/cuda",
    "TORCHINDUCTOR_CACHE_DIR": "cache/inductor",
    "TRITON_CACHE_DIR": "cache/triton",
}.items():
    env[k] = str(R.make_dir(B + "/" + v))
command = [
    str(R.resolve(B + "/env/Scripts/python.exe")),
    "-B",
    str(R.resolve("spikes/21_video_foley/run_foley.py")),
    "--proxy",
    a.proxy,
    "--out",
    a.out,
    "--seconds",
    str(a.seconds),
]
if a.prompt:
    command.extend(["--prompt",a.prompt])
if a.negative_prompt is not None:
    command.extend(["--negative-prompt", a.negative_prompt])
if a.save_latents:
    command.append("--save-latents")
policy = {
    "minimum_available_ram_gib": 2,
    "low_memory_consecutive_samples": 3,
    "sample_interval_sec": 1,
    "deadline_sec": 900 if a.seconds == 3 else 1200,
    "stall_sec": 240,
    "scope": "isolated diagnostic safety policy, not model reservation measurement",
}
result = {"command": command, "policy": policy, "samples": []}
started = time.monotonic()


def execute(log):
    with log.open("wb") as stream:
        child = subprocess.Popen(
            command, stdout=stream, stderr=subprocess.STDOUT, env=env, cwd=ROOT
        )
        result["pid"] = child.pid
        low = 0
        last_size = 0
        last_progress = started
        while child.poll() is None:
            now = time.monotonic()
            free = ram()
            size = log.stat().st_size
            if size != last_size:
                last_progress = now
                last_size = size
            low = low + 1 if free < policy["minimum_available_ram_gib"] else 0
            result["samples"].append(
                {
                    "elapsed_sec": round(now - started, 3),
                    "available_ram_gib": round(free, 3),
                    "log_size": size,
                }
            )
            reason = (
                "low_system_ram"
                if low >= 3
                else "deadline"
                if now - started > policy["deadline_sec"]
                else "no_log_progress"
                if now - last_progress > policy["stall_sec"]
                else None
            )
            if reason:
                result["watchdog_stop"] = reason
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
                break
            time.sleep(1)
        result["exit_code"] = child.wait()
        result["elapsed_sec"] = time.monotonic() - started


R.write_with(a.out + "/runtime.log", execute)
R.write_text(a.out + "/watchdog.json", json.dumps(result, indent=2) + "\n")
print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2))
raise SystemExit(result["exit_code"] if result["exit_code"] else 0)
