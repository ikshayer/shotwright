"""Independent regressions for the four HIGH native H3 release blockers.

Except for the shifted-PTS media case and the competing-listener case, these
tests use explicitly controlled CPU protocol fixtures. They prove adapter
control/resource semantics only, not model execution or native cleanup.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from orchestrator.adapters.minimax_h3 import H3Request, MinimaxH3Adapter
from orchestrator.adapters.minimax_h3.core import H3SetupError, build_api_graph
from orchestrator.adapters.minimax_h3.native import NativeBackend
from orchestrator.domain.entities import Job
from orchestrator.jobs.lease import GpuLease
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore
from tests.jobs.test_lease import FakeNvml
from tests.jobs.test_queue import FakeHost

MODELS = {
    "diffusion": "fixture-diffusion.safetensors",
    "text_encoder": "fixture-encoder.safetensors",
    "video_vae": "fixture-video.safetensors",
    "audio_vae": "fixture-audio.safetensors",
}

SUPERVISOR = r'''
import json, os, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import psutil
from orchestrator.platform.containment import ProjectRoot
root = ProjectRoot(sys.argv[2])
session_rel, mode = sys.argv[3], sys.argv[4]
root.make_dir('output')
child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(90)'],
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
identity = {'pid': child.pid, 'create_time': psutil.Process(child.pid).create_time(),
            'parent_pid': os.getpid(), 'live_identity_verified': True}
own = {'pid': os.getpid(), 'create_time': psutil.Process(os.getpid()).create_time(),
       'parent_pid': os.getppid(), 'live_identity_verified': True}
base = {'session': session_rel, 'url': 'http://127.0.0.1:8298',
        'owned_process_identities': [own, identity],
        'system_stats': {'devices': [{'name': 'RTX 5070 Ti Laptop', 'type': 'cuda'}]},
        'queue_at_ready': {'queue_running': [], 'queue_pending': []},
        'fixture_only_no_gpu': True}
root.write_text('started.json', json.dumps({**base, 'status': 'starting'}))
root.write_text('ready.json', json.dumps({**base, 'status': 'ready_not_queued'}))
try:
    while not root.resolve('STOP').exists():
        time.sleep(.02)
finally:
    failures = []
    try:
        child.terminate()
        child.wait(timeout=3)
    except BaseException as error:
        failures.append(str(error))
    try:
        alive = psutil.Process(identity['pid']).create_time() == identity['create_time']
    except psutil.NoSuchProcess:
        alive = False
    cleanup = {'verified_stopped': not alive and not failures,
               'identities': [identity], 'survivors': [identity] if alive else [],
               'unverifiable_identities': [], 'failures': failures}
    stopped = {'status': 'stopped', 'watchdog_joined': True,
               'watchdog_failure': {'type': 'InterruptedError', 'error': 'Creator requested session stop'},
               'cleanup': cleanup, 'fixture_only_no_gpu': True}
    if mode == 'stopped_error':
        stopped['error'] = 'controlled stopped.error from CPU supervisor fixture'
    root.write_text('stopped.json', json.dumps(stopped))
if mode == 'returncode':
    sys.exit(17)
'''

LISTENER = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
sys.path.insert(0, sys.argv[1])
import psutil
from orchestrator.platform.containment import ProjectRoot
root = ProjectRoot(sys.argv[2])
port, schema_rel = int(sys.argv[3]), sys.argv[4]
schema = json.loads(root.read_text(schema_rel))
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
    def record(self):
        root.append_line('listener-requests.jsonl', json.dumps({'method': self.command,
                         'path': urlsplit(self.path).path}, separators=(',', ':')))
    def send_json(self, code, value):
        body = json.dumps(value).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self.record()
        path = urlsplit(self.path).path
        if path == '/system_stats':
            return self.send_json(200, {'devices': [{'name': 'RTX 5070 Ti Laptop', 'type': 'cuda'}]})
        if path == '/object_info':
            return self.send_json(200, schema)
        if path == '/queue':
            return self.send_json(200, {'queue_running': [], 'queue_pending': []})
        return self.send_json(200, {})
    def do_POST(self):
        self.record()
        length = int(self.headers.get('Content-Length', '0'))
        if length:
            self.rfile.read(length)
        self.send_json(409, {'error': 'foreign listener refuses mutation'})
server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
identity = {'pid': os.getpid(), 'create_time': psutil.Process(os.getpid()).create_time()}
root.write_text('listener-ready.json', json.dumps(identity))
server.serve_forever(poll_interval=.05)
'''


def _wait_for(predicate: Callable[[], Any], *, timeout: float = 6.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("controlled CPU fixture did not reach its bounded state")


def _identity_alive(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == create_time and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _kill_identity(pid: int, create_time: float) -> None:
    try:
        process = psutil.Process(pid)
        if process.create_time() != create_time:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    except psutil.NoSuchProcess:
        return


def _schema_for(graph: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for node in graph.values():
        inputs = {}
        for name, value in node["inputs"].items():
            if isinstance(value, list):
                inputs[name] = ["ANY"]
            elif isinstance(value, bool):
                inputs[name] = ["BOOLEAN", {}]
            elif isinstance(value, int):
                inputs[name] = ["INT", {"min": 0, "max": 2**53 - 1}]
            elif isinstance(value, float):
                inputs[name] = ["FLOAT", {"min": 0, "max": 16384, "step": 0.1}]
            else:
                inputs[name] = ["STRING", {}]
        result[node["class_type"]] = {"input": {"required": inputs}, "output": ["ANY", "ANY"]}
    return result


def _resource(*, ram_gib: float = 24.0, used_mb: int = 1000, free_mb: int = 11000) -> dict[str, Any]:
    return {
        "ram_available_bytes": int(ram_gib * 1024**3),
        "disk_free_bytes": 200 * 1024**3,
        "gpu_whole_device_mb": {"used_mb": used_mb, "total_mb": 12000, "free_mb": free_mb},
    }


@contextmanager
def _native_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    required_vram_gb: float = 1.0,
    required_ram_gb: float = 1.0,
    supervisor_mode: str = "success",
    history_mode: str = "success",
    real_http: bool = False,
    interpose_listener: bool = False,
) -> Iterator[SimpleNamespace]:
    root = ProjectRoot(tmp_path)
    root.make_dir("project")
    root.make_dir("repository")
    project = ProjectRoot(root.resolve("project"))
    repository = ProjectRoot(root.resolve("repository"))
    project.make_dir("renders")
    project.write_bytes("renders/master.mp4", b"controlled bound source bytes")
    repository.make_dir("spikes/22_minimax_h3_workflows")
    source_repository = Path(__file__).resolve().parents[3]
    repository.write_bytes(
        "spikes/22_minimax_h3_workflows/direct_comfy.py",
        source_repository.joinpath("spikes/22_minimax_h3_workflows/direct_comfy.py").read_bytes(),
    )
    repository.write_bytes(
        "spikes/22_minimax_h3_workflows/local_h3.py",
        source_repository.joinpath("spikes/22_minimax_h3_workflows/local_h3.py").read_bytes(),
    )
    settings = Settings.model_validate({
        "project_root": project.path,
        "minimax_h3": {
            "enabled": True,
            "required_vram_gb": required_vram_gb,
            "required_ram_gb": required_ram_gb,
            "required_disk_gb": 1.0,
            "throughput_floor_per_second": 0.001,
            "startup_allowance_seconds": 3,
            "execution_timeout_seconds": 20,
            "wall_clock_seconds": 30,
        },
    })
    source_hash = hashlib.sha256(project.resolve("renders/master.mp4").read_bytes()).hexdigest()
    request = H3Request(
        shot_id="s1", shot_version=1, run_id=f"native-blocker-{tmp_path.name}",
        video_path="renders/master.mp4", source_sha256=source_hash,
        source_output_id="master1", prompt="quiet corridor",
    )
    graph = build_api_graph(request, models=MODELS, seed=1)
    state = SimpleNamespace(
        root=root, project=project, repository=repository, settings=settings, request=request,
        graph=graph, supervisor_mode=supervisor_mode, history_mode=history_mode,
        processes=[], worker_identities=[], sessions=[], posts=[], http_calls=[],
        resource_observations=[], pre_resource_observations=[], post_resource_observations=[], resource_provider=None,
        resource_calls=0, session=None, session_rel=None, listener=None,
        listener_identity=None, listener_processes=[], listener_lock=threading.Lock(),
        launch_calls=0, close_error=None,
    )
    state.job = Job(job_id=f"job-{request.run_id}", type="h3_candidate", shot_id="s1",
                    run_id=request.run_id, required_vram_gb=1.0, required_ram_gb=1.0,
                    required_disk_gb=1.0)

    def resources() -> dict[str, Any]:
        state.resource_calls += 1
        sample = state.resource_provider(state) if state.resource_provider is not None else _resource()
        state.resource_observations.append(dict(sample))
        if state.submitted.is_set():
            state.post_resource_observations.append(dict(sample))
        else:
            state.pre_resource_observations.append(dict(sample))
        return sample

    state.submitted = threading.Event()

    def start_listener() -> None:
        with state.listener_lock:
            if state.listener is not None:
                return
            project.make_dir("_work/listener")
            schema_rel = "_work/listener/object-info.json"
            project.write_text(schema_rel, json.dumps(_schema_for(graph)))
            listener_env = dict(os.environ)
            contained_temp = str(project.make_dir("_work/listener/temp"))
            listener_env.update({"TEMP": contained_temp, "TMP": contained_temp, "TMPDIR": contained_temp})
            listener = subprocess.Popen(
                [sys.executable, "-B", "-c", LISTENER, str(Path(__file__).resolve().parents[3]), str(project.path),
                 "8298", schema_rel],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=listener_env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            state.listener = listener
            state.listener_processes.append(listener)
            _wait_for(lambda: project.resolve("listener-ready.json").is_file())
            state.listener_identity = json.loads(project.read_text("listener-ready.json"))

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        state.launch_calls += 1
        if interpose_listener:
            start_listener()
        if "--session" not in argv:
            raise AssertionError(f"supervisor argv lacked the documented --session option: {argv!r}")
        session_rel = str(argv[argv.index("--session") + 1])
        session_path = repository.resolve(session_rel)
        if not session_path.is_dir():
            raise AssertionError(f"adapter did not contain its runtime session before launch: {session_rel}")
        state.session_rel = session_rel
        state.session = ProjectRoot(session_path)
        contained_temp = str(project.make_dir("_work/supervisor-temp"))
        env = dict(kwargs.get("env") or os.environ)
        env.update({"TEMP": contained_temp, "TMP": contained_temp, "TMPDIR": contained_temp})
        kwargs["env"] = env
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", SUPERVISOR, str(Path(__file__).resolve().parents[3]), str(session_path),
             session_rel, supervisor_mode],
            **kwargs,
        )
        created = psutil.Process(process.pid).create_time()
        state.processes.append((process, created))
        _wait_for(lambda: session_path.joinpath("ready.json").is_file())
        ready = json.loads(session_path.joinpath("ready.json").read_text(encoding="utf-8"))
        state.worker_identities.extend(
            (int(item["pid"]), float(item["create_time"]))
            for item in ready["owned_process_identities"]
        )
        return process

    def http(method: str, path: str, payload: dict[str, Any] | None = None,
             *, timeout: float = 10) -> dict[str, Any]:
        del timeout
        state.http_calls.append((method, path))
        if path == "/system_stats":
            return {"devices": [{"name": "RTX 5070 Ti Laptop", "type": "cuda"}]}
        if path == "/object_info":
            return _schema_for(graph)
        if path == "/queue":
            return {"queue_running": [], "queue_pending": []}
        if method == "POST" and path == "/prompt":
            state.posts.append(payload)
            state.graph = payload["prompt"]
            state.submitted.set()
            state.session.write_bytes("output/candidate.mp4", b"fixture generated AV bytes")
            return {"prompt_id": "cpu-prompt", "number": 0, "node_errors": {}}
        if path.startswith("/history/"):
            if state.history_mode == "primary":
                raise RuntimeError("controlled primary execution failure")
            if state.history_mode == "incomplete":
                return {}
            if state.history_mode == "wait_for_post_resource" and not state.post_resource_observations:
                return {}
            if state.history_mode == "unexpected_after_six":
                if len(state.post_resource_observations) >= 6:
                    raise AssertionError("native run passed six controlled starvation samples without stopping")
                return {}
            executed = json.loads(json.dumps(state.graph))
            return {"cpu-prompt": {
                "prompt": [0, "cpu-prompt", executed, {}, ["12"]],
                "outputs": {"12": {"images": [{"filename": "candidate.mp4", "subfolder": "",
                    "type": "output", "metadata": {"kind": "video", "width": 384,
                    "height": 288, "duration": 8, "fps": 24, "frame_count": 192}}],
                    "animated": [True]}},
                "status": {"status_str": "success", "completed": True,
                    "messages": [["execution_success", {"prompt_id": "cpu-prompt"}]]},
            }}
        if path in {"/free", "/interrupt"}:
            return {}
        raise AssertionError(f"unexpected fixture HTTP request: {method} {path}")

    backend = NativeBackend(project, settings, popen=popen, http=None if real_http else http,
                            resources=resources, repository_root=repository)
    state.backend = backend

    def verify_runtime(*, cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
        if cancel is not None and cancel():
            raise JobError("H3 fixture verification cancelled")
        return {
            "models": dict(MODELS), "provenance": {"fixture_only_no_model_identity": True},
            "runtime_python": sys.executable, "comfy_root": str(repository.path),
            "controller_path": str(repository.resolve("spikes/22_minimax_h3_workflows/direct_comfy.py")),
            "helper_path": str(repository.resolve("spikes/22_minimax_h3_workflows/local_h3.py")),
        }

    def prepare_video(req: H3Request, *, run_rel: str, cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
        del cancel
        proxy_rel = run_rel + "/input/model_proxy.mp4"
        project.write_bytes(proxy_rel, b"fixture prepared proxy bytes")
        return {"path": proxy_rel, "sha256": hashlib.sha256(b"fixture prepared proxy bytes").hexdigest(),
                "frame_count": 192, "fps": 24, "width": 384, "height": 288,
                "duration_seconds": 8, "source_sha256": req.source_sha256,
                "command": ["controlled CPU preparation fixture"]}

    def verify_and_export(req: H3Request, *, run_rel: str, av_path: str,
                          cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
        del req, cancel
        if not project.resolve(av_path).is_file():
            raise AssertionError(f"fixture history did not create candidate bytes at {av_path}")
        av_sha = hashlib.sha256(project.resolve(av_path).read_bytes()).hexdigest()
        wav_rel = run_rel + "/audio.wav"
        project.write_bytes(wav_rel, b"fixture decoded WAV bytes")
        return {"av_path": av_path, "av_sha256": av_sha, "wav_path": wav_rel,
                "wav_sha256": hashlib.sha256(b"fixture decoded WAV bytes").hexdigest(),
                "frame_count": 192, "fps": 24, "audio_sample_rate": 48000,
                "audio_channels": 2, "audio_samples_per_channel": 384000,
                "audio_metrics": {"fixture_only": True}}

    monkeypatch.setattr(backend, "verify_runtime", verify_runtime)
    monkeypatch.setattr(backend, "prepare_video", prepare_video)
    monkeypatch.setattr(backend, "verify_and_export", verify_and_export)
    try:
        yield state
    finally:
        try:
            backend.close()
        except Exception as error:  # noqa: BLE001 - cleanup below remains mandatory after shutdown failure
            state.close_error = error
        for pid, created in state.worker_identities:
            _kill_identity(pid, created)
        for process, created in state.processes:
            try:
                if process.poll() is None and _identity_alive(process.pid, created):
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child_created = child.create_time()
                            _kill_identity(child.pid, child_created)
                        except psutil.NoSuchProcess:
                            pass
                    _kill_identity(process.pid, created)
                process.wait(timeout=3)
            except (psutil.NoSuchProcess, subprocess.TimeoutExpired):
                pass
        for listener in state.listener_processes:
            try:
                if listener.poll() is None:
                    identity = state.listener_identity
                    if identity is not None and listener.pid == int(identity["pid"]):
                        _kill_identity(int(identity["pid"]), float(identity["create_time"]))
                    elif listener.pid is not None:
                        _kill_identity(listener.pid, psutil.Process(listener.pid).create_time())
                listener.wait(timeout=3)
            except (psutil.NoSuchProcess, subprocess.TimeoutExpired):
                listener.kill()
                listener.wait(timeout=3)


def _execute(case: SimpleNamespace) -> Any:
    queue = SimpleNamespace(root=case.project, settings=case.settings)
    return case.backend.execute(case.request, case.job, root=case.project, queue=queue,
                                cancel=lambda: False, progress=lambda _value: None)


def _assert_fixture_tree_stopped(case: SimpleNamespace) -> None:
    assert case.processes, "controlled supervisor was never launched, so no process cleanup was exercised"
    for process, created in case.processes:
        assert process.poll() is not None, f"controlled supervisor PID {process.pid} remained running"
        assert not _identity_alive(process.pid, created), f"controlled supervisor identity {process.pid}/{created} survived"
    for pid, created in case.worker_identities:
        assert not _identity_alive(pid, created), f"controlled supervisor child identity {pid}/{created} survived"


def test_runtime_uses_admission_vram_once_while_model_allocates_more(tmp_path, monkeypatch):
    """CPU protocol fixture: admission=10 GiB; runtime free VRAM falls to 9 GiB."""
    with _native_case(tmp_path, monkeypatch, required_vram_gb=10.0, required_ram_gb=10.0) as case:
        case.history_mode = "wait_for_post_resource"
        case.resource_provider = lambda state: (
            _resource(ram_gib=20.0, used_mb=1000, free_mb=11000)
            if not state.submitted.is_set()
            else _resource(ram_gib=12.0, used_mb=2800, free_mb=9200)
        )
        result, run_error = None, None
        try:
            result = _execute(case)
        except Exception as error:  # noqa: BLE001 - retain the observed failure beside controlled resources
            run_error = error
        assert case.post_resource_observations, (
            f"resource seam was not sampled after submission; execution error was {run_error!r}"
        )
        initial = case.pre_resource_observations[0]
        runtime = case.post_resource_observations[0]
        assert initial["gpu_whole_device_mb"]["free_mb"] >= 10 * 1024, (
            f"admission sample free VRAM was {initial['gpu_whole_device_mb']['free_mb']} MiB, below the 10 GiB reservation"
        )
        assert runtime["gpu_whole_device_mb"]["used_mb"] > initial["gpu_whole_device_mb"]["used_mb"], (
            f"fixture GPU used memory did not rise after submission: initial={initial}, runtime={runtime}"
        )
        assert runtime["gpu_whole_device_mb"]["free_mb"] < 10 * 1024, (
            f"fixture free VRAM did not fall below its admission-only 10 GiB reservation: {runtime}"
        )
        assert runtime["ram_available_bytes"] > 2 * 1024**3, (
            f"runtime host RAM {runtime['ram_available_bytes']} bytes fell below the documented 2 GiB stop floor"
        )
        assert run_error is None and result is not None and result.run_id == case.request.run_id, (
            f"run was rejected after the model-use VRAM sample despite RAM remaining above its runtime floor; "
            f"result={result!r}, error={run_error!r}, initial={initial!r}, runtime={runtime!r}"
        )
        assert len(case.posts) == 1, f"controlled API observed {len(case.posts)} prompt submissions, expected one"
        _assert_fixture_tree_stopped(case)


def test_initial_six_gib_ram_floor_still_refuses_before_launch(tmp_path, monkeypatch):
    """The initial native RAM floor remains 6 GiB independently of admission settings."""
    with _native_case(tmp_path, monkeypatch) as case:
        case.resource_provider = lambda _state: _resource(ram_gib=5.0)
        failure = None
        try:
            _execute(case)
        except Exception as error:  # noqa: BLE001 - report whether the 6 GiB floor rejected the run
            failure = error
        message = "" if failure is None else str(failure).lower()
        assert isinstance(failure, JobError) and "6" in message and "ram" in message and case.launch_calls == 0 and case.posts == [], (
            f"5 GiB initial RAM did not fail closed before launch; failure={failure!r}, "
            f"launches={case.launch_calls}, submissions={case.posts!r}"
        )


def test_runtime_two_gib_ram_floor_requires_three_consecutive_samples(tmp_path, monkeypatch):
    """A recovered sample resets the 2 GiB watchdog streak; three later lows stop the run."""
    with _native_case(tmp_path, monkeypatch) as case:
        samples = [1.5, 1.5, 3.0, 1.5, 1.5, 1.5]
        case.history_mode = "unexpected_after_six"

        def provider(state: SimpleNamespace) -> dict[str, Any]:
            if not state.submitted.is_set():
                return _resource(ram_gib=12.0)
            index = min(len(state.post_resource_observations), len(samples) - 1)
            return _resource(ram_gib=samples[index])

        case.resource_provider = provider
        failure = None
        try:
            _execute(case)
        except Exception as error:  # noqa: BLE001 - report the watchdog's terminal reason with its samples
            failure = error
        message = "" if failure is None else str(failure).lower()
        measured = [round(item["ram_available_bytes"] / 1024**3, 1)
                    for item in case.post_resource_observations]
        assert measured[:6] == samples, f"runtime RAM samples were {measured!r}; expected recovery then three lows {samples!r}"
        assert len(measured) == 6, f"watchdog consumed {len(measured)} post-submit samples instead of stopping on the third consecutive low"
        assert isinstance(failure, JobError) and "2" in message and ("sample" in message or "ram" in message), (
            f"runtime starvation did not stop with the documented 2 GiB/3-sample error; "
            f"failure={failure!r}, observed samples={measured!r}"
        )
        _assert_fixture_tree_stopped(case)


def _make_queue(case: SimpleNamespace) -> tuple[MinimaxH3Adapter, JobQueue, GpuLease]:
    adapter = MinimaxH3Adapter(case.project, case.settings, backend=case.backend)
    probe = FakeNvml()
    lease = GpuLease(case.project, case.settings, nvml=probe, worker=adapter.worker)
    queue = JobQueue(case.project, case.settings, lease, ProjectStore(case.project), nvml=probe,
                     host=FakeHost(), quarantine=ProjectQuarantine(case.project))
    return adapter, queue, lease


def _queued_native_run(case: SimpleNamespace) -> None:
    adapter, queue, lease = _make_queue(case)
    case.adapter, case.queue, case.lease = adapter, queue, lease
    try:
        queue.run(case.job, lambda job: adapter.generate_managed(case.request, job, queue=queue),
                  startup_allowance_seconds=5)
    finally:
        adapter.close()


@pytest.mark.parametrize("supervisor_mode,cleanup_marker", [
    ("returncode", "17"),
    ("stopped_error", "controlled stopped.error"),
])
def test_verified_physical_cleanup_does_not_turn_abnormal_stop_into_success(
    tmp_path, monkeypatch, supervisor_mode, cleanup_marker
):
    """A successful history still fails for a nonzero supervisor exit or stopped.error."""
    with _native_case(tmp_path, monkeypatch, supervisor_mode=supervisor_mode) as case:
        failure = None
        try:
            _queued_native_run(case)
        except Exception as error:  # noqa: BLE001 - inspect cleanup and lease release even if success was returned
            failure = error
        assert case.session is not None and case.session.resolve("stopped.json").is_file(), (
            "supervisor exited without writing its measured physical-cleanup receipt"
        )
        stopped = json.loads(case.session.read_text("stopped.json"))
        tree_stopped = bool(case.processes) and all(
            process.poll() is not None and not _identity_alive(process.pid, created)
            for process, created in case.processes
        ) and all(not _identity_alive(pid, created) for pid, created in case.worker_identities)
        assert isinstance(failure, JobError) and cleanup_marker.lower() in str(failure).lower() and len(case.posts) == 1 and (
            stopped["cleanup"]["verified_stopped"] is True and stopped["cleanup"]["survivors"] == []
            and case.queue.lease._owned is False and tree_stopped
        ), (
            f"abnormal supervisor outcome was not rejected while verified cleanup released resources; "
            f"failure={failure!r}, posts={len(case.posts)}, stopped={stopped!r}, "
            f"lease_owned={case.queue.lease._owned}, process_tree_stopped={tree_stopped}"
        )


@pytest.mark.parametrize("supervisor_mode,cleanup_marker", [
    ("returncode", "17"),
    ("stopped_error", "controlled stopped.error"),
])
def test_primary_and_verified_cleanup_failures_are_retained_separately(
    tmp_path, monkeypatch, supervisor_mode, cleanup_marker
):
    """A separate execution error and abnormal cleanup remain separately reportable."""
    with _native_case(tmp_path, monkeypatch, supervisor_mode=supervisor_mode, history_mode="primary") as case:
        with pytest.raises(JobError):
            _queued_native_run(case)
        report_rel = f"generated/h3/{case.request.shot_id}/v{case.request.shot_version:04d}/{case.request.run_id}/report.json"
        report_path = case.project.resolve(report_rel)
        assert report_path.is_file(), f"native failure did not retain its project-contained report at {report_rel}"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        primary = report.get("primary_error")
        cleanup = report.get("cleanup_error")
        stopped = report.get("cleanup")
        physical_cleanup_verified = (
            isinstance(stopped, dict)
            and isinstance(stopped.get("cleanup"), dict)
            and stopped["cleanup"].get("verified_stopped") is True
            and stopped["cleanup"].get("survivors") == []
        )
        process_tree_stopped = bool(case.processes) and all(
            process.poll() is not None and not _identity_alive(process.pid, created)
            for process, created in case.processes
        ) and all(not _identity_alive(pid, created) for pid, created in case.worker_identities)
        cleanup_text = json.dumps(cleanup).lower()
        assert (
            isinstance(primary, dict)
            and "controlled primary execution failure" in str(primary)
            and cleanup is not None
            and (cleanup_marker.lower() in cleanup_text or "returncode" in cleanup_text)
            and case.queue.lease._owned is False
            and physical_cleanup_verified
            and process_tree_stopped
            and len(case.posts) == 1
        ), (
            f"primary/cleanup exceptions were not separately retained while verified cleanup released resources; "
            f"primary={primary!r}, cleanup_error={cleanup!r}, posts={len(case.posts)}, "
            f"lease_owned={case.queue.lease._owned}, cleanup_receipt={stopped!r}, "
            f"process_tree_stopped={process_tree_stopped}"
        )


def _listener_requests(case: SimpleNamespace) -> list[dict[str, str]]:
    relative = "listener-requests.jsonl"
    if not case.project.resolve(relative).is_file():
        return []
    return [json.loads(line) for line in case.project.read_text(relative).splitlines() if line]


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def test_owned_descendant_listener_is_accepted_and_foreign_identity_refused(tmp_path, monkeypatch):
    """A real descendant listener passes; an additional unowned socket identity fails closed."""
    if not _port_is_free(8298):
        pytest.skip("port 8298 is already occupied; refusing to touch a listener not created by this test")

    server_code = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, sys.argv[4])
import psutil
from orchestrator.platform.containment import ProjectRoot
root = ProjectRoot(sys.argv[1])
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
    def do_GET(self):
        root.append_line('owned-listener-requests.jsonl', json.dumps({'path': self.path, 'pid': os.getpid()}))
        body = json.dumps({'path': self.path, 'pid': os.getpid()}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server = ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), Handler)
root.write_text(sys.argv[3], json.dumps({'pid': os.getpid(), 'create_time': psutil.Process(os.getpid()).create_time(),
                                        'parent_pid': os.getppid(), 'port': server.server_port}))
server.serve_forever(poll_interval=.05)
'''
    worker_code = r'''
import json, os, subprocess, sys, time
import psutil
sys.path.insert(0, sys.argv[3])
from orchestrator.platform.containment import ProjectRoot
root = ProjectRoot(sys.argv[2])
root.write_text('owned-worker-self.json', json.dumps({'pid': os.getpid(),
                'create_time': psutil.Process(os.getpid()).create_time(), 'parent_pid': os.getppid()}))
server = subprocess.Popen([sys.executable, '-B', '-c', sys.argv[1], sys.argv[2], '8298',
                           'owned-listener-ready.json', sys.argv[3]],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
root.write_text('owned-server-launcher.json', json.dumps({'pid': server.pid,
                'create_time': psutil.Process(server.pid).create_time(), 'parent_pid': os.getpid()}))
while server.poll() is None:
    time.sleep(.02)
'''
    launcher_code = r'''
import json, os, subprocess, sys, time
import psutil
sys.path.insert(0, sys.argv[4])
from orchestrator.platform.containment import ProjectRoot
root = ProjectRoot(sys.argv[3])
root.write_text('owned-launcher-self.json', json.dumps({'pid': os.getpid(),
                'create_time': psutil.Process(os.getpid()).create_time(), 'parent_pid': os.getppid()}))
worker = subprocess.Popen([sys.executable, '-B', '-c', sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
root.write_text('owned-worker-launcher.json', json.dumps({'pid': worker.pid,
                'create_time': psutil.Process(worker.pid).create_time(), 'parent_pid': os.getpid()}))
while worker.poll() is None:
    time.sleep(.02)
'''
    foreign_code = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, sys.argv[3])
import psutil
from orchestrator.platform.containment import ProjectRoot
root = ProjectRoot(sys.argv[1])
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
server = HTTPServer(('127.0.0.1', 0), Handler)
root.write_text('foreign-listener-ready.json', json.dumps({'pid': os.getpid(),
                'create_time': psutil.Process(os.getpid()).create_time(),
                'parent_pid': os.getppid(), 'port': server.server_port}))
server.serve_forever(poll_interval=.05)
'''

    project = ProjectRoot(tmp_path)
    temp = str(project.make_dir("_work/owned-listener-temp"))
    env = dict(os.environ)
    env.update({"TEMP": temp, "TMP": temp, "TMPDIR": temp})
    repository = ProjectRoot(Path(__file__).resolve().parents[3])
    popen_calls: list[list[str]] = []

    def owned_popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        popen_calls.append(list(argv))
        return subprocess.Popen(argv, **kwargs)

    backend = NativeBackend(project, Settings(), popen=owned_popen, repository_root=repository)

    command = [sys.executable, "-B", "-c", launcher_code, worker_code, server_code,
               str(project.path), str(repository.path)]
    process: subprocess.Popen[bytes] | None = None
    foreign: subprocess.Popen[bytes] | None = None
    tree: Any = None
    identities: list[tuple[int, float]] = []
    cleanup_errors: list[str] = []
    original_net_connections = psutil.net_connections
    try:
        process = backend._launch_popen(
            command, cwd=project.path, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        root_created = psutil.Process(process.pid).create_time()
        identities.append((process.pid, root_created))
        tree = backend._owned_tree(process)
        tree.graceful_seconds = 1.0
        tree.forced_seconds = 1.0
        backend._tree = tree
        _wait_for(lambda: project.resolve("owned-listener-ready.json").is_file())
        listener = json.loads(project.read_text("owned-listener-ready.json"))
        launcher = json.loads(project.read_text("owned-launcher-self.json"))
        worker_launcher = json.loads(project.read_text("owned-worker-launcher.json"))
        worker = json.loads(project.read_text("owned-worker-self.json"))
        server_launcher = json.loads(project.read_text("owned-server-launcher.json"))
        launcher_pid = int(launcher["pid"])
        worker_launcher_pid = int(worker_launcher["pid"])
        worker_pid = int(worker["pid"])
        server_launcher_pid = int(server_launcher["pid"])
        listener_pid = int(listener["pid"])
        identities.extend((
            (launcher_pid, float(launcher["create_time"])),
            (worker_launcher_pid, float(worker_launcher["create_time"])),
            (worker_pid, float(worker["create_time"])),
            (server_launcher_pid, float(server_launcher["create_time"])),
            (listener_pid, float(listener["create_time"])),
        ))

        owned_rows = tree.refresh()
        tree_rows = {int(row["pid"]): row for row in owned_rows if isinstance(row, dict) and "pid" in row}
        listener_rows = [row for row in original_net_connections(kind="tcp")
                         if row.status == psutil.CONN_LISTEN and getattr(row.laddr, "port", None) == 8298]
        actual_socket_pid = listener_rows[0].pid if len(listener_rows) == 1 else None
        actual_socket_created = (psutil.Process(actual_socket_pid).create_time()
                                 if isinstance(actual_socket_pid, int) and actual_socket_pid > 0 else None)
        observed_parent_pairs = [
            (launcher_pid, psutil.Process(launcher_pid).ppid()),
            (worker_launcher_pid, psutil.Process(worker_launcher_pid).ppid()),
            (worker_pid, psutil.Process(worker_pid).ppid()),
            (server_launcher_pid, psutil.Process(server_launcher_pid).ppid()),
            (listener_pid, psutil.Process(listener_pid).ppid()),
        ]
        assert len(popen_calls) == 1 and popen_calls[0] == command, (
            f"adapter popen seam was not used exactly once for the listener launcher; observed calls={len(popen_calls)}"
        )
        assert len(listener_rows) == 1 and actual_socket_pid == listener_pid, (
            f"OS socket owner did not match the real listener process; observed rows="
            f"{[(getattr(row, 'pid', None), getattr(row.laddr, 'port', None), row.status) for row in listener_rows]!r}, "
            f"ready identity={listener!r}"
        )
        assert actual_socket_pid != process.pid, (
            f"expected socket owner to be a descendant rather than Popen PID {process.pid}; observed socket PID={actual_socket_pid}"
        )
        assert actual_socket_created == float(listener["create_time"]), (
            f"OS listener PID/create-time differed from child report; observed={(actual_socket_pid, actual_socket_created)!r}, "
            f"reported={(listener_pid, listener['create_time'])!r}"
        )
        records = [launcher, worker_launcher, worker, server_launcher, listener]
        recorded_parent_pairs = [(int(item["pid"]), int(item["parent_pid"])) for item in records]
        tree_parent_pairs = [(pid, int(tree_rows[pid]["parent_pid"])) for pid, _parent in recorded_parent_pairs
                             if pid in tree_rows and tree_rows[pid].get("parent_pid") is not None]
        assert observed_parent_pairs == recorded_parent_pairs and tree_parent_pairs == recorded_parent_pairs, (
            f"process ancestry measurements disagreed between fixture, OS, and OwnedProcessTree; "
            f"fixture={recorded_parent_pairs!r}, OS={observed_parent_pairs!r}, tree={tree_parent_pairs!r}"
        )
        ancestry: list[int] = []
        ancestry_seen: set[int] = set()
        ancestry_pid = listener_pid
        while ancestry_pid in tree_rows and ancestry_pid not in ancestry_seen:
            ancestry_seen.add(ancestry_pid)
            ancestry.append(ancestry_pid)
            parent_pid = tree_rows[ancestry_pid].get("parent_pid")
            if parent_pid is None:
                break
            ancestry_pid = int(parent_pid)
        assert (ancestry and ancestry[0] == listener_pid and ancestry[-1] == process.pid
                and len(ancestry) >= 3 and tree_rows[listener_pid].get("parent_pid") != process.pid), (
            f"socket owner was not a non-direct descendant reachable from the Popen identity; "
            f"observed ancestry={ancestry!r}, Popen PID={process.pid}, socket PID={listener_pid}, "
            f"tree rows={tree_rows!r}"
        )
        assert all(
            pid in tree_rows and tree_rows[pid].get("live_identity_verified") is True
            and tree_rows[pid].get("identity_state") == "verified_live"
            for pid in (process.pid, launcher_pid, worker_launcher_pid, worker_pid, server_launcher_pid, listener_pid)
        ), f"OwnedProcessTree did not verify the full live ancestry; observed tree rows={tree_rows!r}"

        accepted_identity = backend._assert_owned_listener(tree, action="positive ownership test")
        response = backend._request("GET", "/owned-positive-probe", timeout=3)
        assert accepted_identity["pid"] == listener_pid and accepted_identity["create_time"] == actual_socket_created, (
            f"ownership verifier did not return the measured descendant listener identity; observed={accepted_identity!r}, "
            f"socket={(actual_socket_pid, actual_socket_created)!r}"
        )
        assert response == {"path": "/owned-positive-probe", "pid": listener_pid}, (
            f"owned request did not reach and receive the real descendant listener response; observed={response!r}"
        )
        last_identity = backend._last_listener_identity
        assert (last_identity is not None and last_identity["pid"] == accepted_identity["pid"]
                and last_identity["create_time"] == accepted_identity["create_time"]
                and last_identity["action"] == "GET /owned-positive-probe"
                and _identity_alive(listener_pid, float(listener["create_time"]))), (
            f"accepted listener identity was not retained while its process remained live; identity="
            f"{last_identity!r}, alive={_identity_alive(listener_pid, float(listener['create_time']))}"
        )

        foreign_env = dict(env)
        foreign = subprocess.Popen(
            [sys.executable, "-B", "-c", foreign_code, str(project.path), str(project.path), str(repository.path)],
            cwd=project.path, env=foreign_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        foreign_created = psutil.Process(foreign.pid).create_time()
        identities.append((foreign.pid, foreign_created))
        _wait_for(lambda: project.resolve("foreign-listener-ready.json").is_file())
        foreign_identity = json.loads(project.read_text("foreign-listener-ready.json"))
        foreign_pid = int(foreign_identity["pid"])
        foreign_created = float(foreign_identity["create_time"])
        identities.append((foreign_pid, foreign_created))
        foreign_rows = [row for row in original_net_connections(kind="tcp")
                        if row.status == psutil.CONN_LISTEN
                        and getattr(row.laddr, "port", None) == int(foreign_identity["port"])
                        and row.pid == foreign_pid]
        tree_rows_with_foreign = {int(row["pid"]): row for row in tree.refresh()
                                  if isinstance(row, dict) and "pid" in row}
        assert foreign_rows and _identity_alive(foreign_pid, foreign_created), (
            f"second loopback listener was not live and visible to the OS; observed rows={foreign_rows!r}, "
            f"identity={foreign_identity!r}"
        )
        assert foreign_pid not in tree_rows_with_foreign, (
            f"second process unexpectedly appeared in the adapter-owned tree; foreign PID={foreign_pid}, "
            f"tree PIDs={sorted(tree_rows_with_foreign)}"
        )

        # Keep both real listeners alive. Windows cannot bind a second exclusive
        # listener to 8298, so report the foreign listener's real PID/create-time
        # as an additional OS row for 8298 and exercise the verifier's all-rows rule.
        def report_foreign_on_target_port(kind: str = "inet") -> list[Any]:
            rows = original_net_connections(kind=kind)
            actual_foreign = next((row for row in rows if row.pid == foreign_pid
                                   and row.status == psutil.CONN_LISTEN
                                   and getattr(row.laddr, "port", None) == int(foreign_identity["port"])), None)
            if actual_foreign is None:
                return rows
            foreign_target_identity = SimpleNamespace(
                family=actual_foreign.family, type=actual_foreign.type,
                laddr=SimpleNamespace(ip="127.0.0.1", port=8298), raddr=(),
                status=actual_foreign.status, pid=foreign_pid,
            )
            return [*rows, foreign_target_identity]

        with monkeypatch.context() as os_probe:
            os_probe.setattr(psutil, "net_connections", report_foreign_on_target_port)
            with pytest.raises(H3SetupError) as refused:
                backend._request("GET", "/must-not-reach-unowned-listener", timeout=3)
        refusal = str(refused.value)
        request_log = [json.loads(line) for line in project.read_text("owned-listener-requests.jsonl").splitlines()]
        assert "listener ownership failure" in refusal.lower() and str(foreign_pid) in refusal, (
            f"unowned listener identity was not the reason for refusal; observed error={refusal!r}, foreign PID={foreign_pid}"
        )
        assert all(row["path"] != "/must-not-reach-unowned-listener" for row in request_log), (
            f"request reached the owned listener despite an unowned OS-reported listener identity; observed requests={request_log!r}"
        )
        assert _identity_alive(listener_pid, float(listener["create_time"])) and _identity_alive(foreign_pid, foreign_created), (
            f"ownership refusal altered a listener process; owned alive={_identity_alive(listener_pid, float(listener['create_time']))}, "
            f"foreign alive={_identity_alive(foreign_pid, foreign_created)}"
        )
    finally:
        try:
            backend.close()
        except BaseException as exc:  # cleanup fallback below remains mandatory
            cleanup_errors.append(f"backend.close: {type(exc).__name__}: {exc}")
        if tree is not None:
            try:
                identities.extend((int(row["pid"]), float(row.get("create_time", row.get("created"))))
                                  for row in tree.refresh() if isinstance(row, dict) and row.get("pid") is not None
                                  and row.get("create_time", row.get("created")) is not None)
            except BaseException as exc:
                cleanup_errors.append(f"owned tree final refresh: {type(exc).__name__}: {exc}")
        for pid, created in dict.fromkeys(identities):
            try:
                _kill_identity(pid, created)
            except BaseException as exc:
                cleanup_errors.append(f"identity {pid}/{created}: {type(exc).__name__}: {exc}")
        for child in (foreign, process):
            if child is not None:
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        child.kill()
                        child.wait(timeout=3)
                    except BaseException as exc:
                        cleanup_errors.append(f"Popen PID {child.pid}: {type(exc).__name__}: {exc}")
                except BaseException as exc:
                    cleanup_errors.append(f"Popen PID {child.pid}: {type(exc).__name__}: {exc}")
        survivors = [(pid, created) for pid, created in dict.fromkeys(identities)
                     if _identity_alive(pid, created)]
        remaining_listeners = [row for row in original_net_connections(kind="tcp")
                               if row.status == psutil.CONN_LISTEN and getattr(row.laddr, "port", None) == 8298]
        port_free = _port_is_free(8298)
        assert not cleanup_errors and not survivors, (
            f"test process cleanup was not verified; cleanup errors={cleanup_errors!r}, surviving identities={survivors!r}"
        )
        assert not remaining_listeners and port_free, (
            f"port 8298 was not free after cleanup; OS listener rows={remaining_listeners!r}, bind check free={port_free}"
        )


def _foreign_listener_state(case: SimpleNamespace) -> dict[str, Any]:
    listener = case.listener
    identity = case.listener_identity
    if listener is None or identity is None:
        return {"started": False, "running": False, "identity_matches": False,
                "requests": [], "mutations": ["listener fixture did not start"]}
    requests = _listener_requests(case)
    mutations = [row for row in requests if row["method"] != "GET" or row["path"] in {"/prompt", "/free", "/interrupt"}]
    return {"started": True, "running": listener.poll() is None,
            "identity_matches": _identity_alive(int(identity["pid"]), float(identity["create_time"])),
            "requests": requests, "mutations": mutations}


def test_real_racing_listener_is_refused_without_control_or_stop(tmp_path, monkeypatch):
    """A real process binds 8298 after preflight; actual loopback HTTP must not queue to it."""
    if not _port_is_free(8298):
        pytest.skip("port 8298 is already occupied; refusing to touch a listener not created by this test")
    with _native_case(tmp_path, monkeypatch, real_http=True, interpose_listener=True) as case:
        with pytest.raises(Exception) as observed:
            _execute(case)
        message = str(observed.value).lower()
        foreign = _foreign_listener_state(case)
        ownership_refusal = any(term in message for term in ("ownership", "unowned", "not owned", "foreign listener"))
        assert ownership_refusal and foreign["started"] and foreign["running"] and foreign["identity_matches"] and not foreign["mutations"], (
            f"required ownership refusal/survival/untouched assertions failed; error={observed.value!r}; listener={foreign!r}"
        )
        _assert_fixture_tree_stopped(case)


@pytest.mark.parametrize("denial", ["pid_unavailable", "create_time_denied"])
def test_listener_identity_query_denial_fails_closed(tmp_path, monkeypatch, denial):
    """Simulated OS refusal to identify the real competing listener must fail closed."""
    if not _port_is_free(8298):
        pytest.skip("port 8298 is already occupied; refusing to touch a listener not created by this test")
    with _native_case(tmp_path, monkeypatch, real_http=True, interpose_listener=True) as case:
        original_net_connections = psutil.net_connections
        original_create_time = psutil.Process.create_time

        def connections(kind: str = "inet"):
            if case.listener_identity is None:
                return original_net_connections(kind=kind)
            pid = None if denial == "pid_unavailable" else int(case.listener_identity["pid"])
            return [SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM,
                    laddr=SimpleNamespace(ip="127.0.0.1", port=8298), raddr=(),
                    status=psutil.CONN_LISTEN, pid=pid)]

        def denied_create_time(process: psutil.Process) -> float:
            if (denial == "create_time_denied" and case.listener_identity is not None
                    and process.pid == int(case.listener_identity["pid"])):
                raise psutil.AccessDenied(pid=process.pid)
            return original_create_time(process)

        # Assumption: the listener verifier uses psutil's public connection/PID APIs.
        with monkeypatch.context() as os_probe:
            os_probe.setattr(psutil, "net_connections", connections)
            os_probe.setattr(psutil.Process, "create_time", denied_create_time)
            with pytest.raises(Exception) as observed:
                _execute(case)
        message = str(observed.value).lower()
        foreign = _foreign_listener_state(case)
        fail_closed = any(term in message for term in ("ownership", "unverifiable", "identity", "listener"))
        assert fail_closed and foreign["started"] and foreign["running"] and foreign["identity_matches"] and not foreign["mutations"], (
            f"OS identity denial did not fail closed while leaving the real listener untouched; error={observed.value!r}; listener={foreign!r}"
        )
        _assert_fixture_tree_stopped(case)


def _ffmpeg(root: ProjectRoot, *args: str) -> bytes:
    temp = str(root.make_dir("_work/pts/temp"))
    env = dict(os.environ)
    env.update({"TEMP": temp, "TMP": temp, "TMPDIR": temp})
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
                            cwd=root.path, env=env, capture_output=True, timeout=45, check=False)
    assert result.returncode == 0, (
        f"CPU FFmpeg fixture command failed with {result.returncode}: {result.stderr.decode(errors='replace')}"
    )
    return result.stdout


def _ffprobe(root: ProjectRoot, path: Path) -> dict[str, Any]:
    temp = str(root.make_dir("_work/pts/temp"))
    env = dict(os.environ)
    env.update({"TEMP": temp, "TMP": temp, "TMPDIR": temp})
    result = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
                             "-show_format", "-of", "json", str(path)],
                            cwd=root.path, env=env, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, (
        f"CPU ffprobe failed with {result.returncode}: {result.stderr.decode(errors='replace')}"
    )
    return json.loads(result.stdout.decode("utf-8"))


def _identity_frame(frame_id: int, *, width: int = 384, height: int = 288) -> bytes:
    """Encode a 6-bit frame identity as six large black/white bars."""
    black = b"\x00\x00\x00" * width
    active = bytearray(width * 3)
    for bit in range(6):
        value = 255 if frame_id & (1 << bit) else 0
        active[bit * 64 * 3:(bit + 1) * 64 * 3] = bytes((value, value, value)) * 64
    return black * 80 + bytes(active) * 128 + black * 80


def _decode_frame_id(frame: bytes, *, width: int = 384) -> int:
    frame_id = 0
    row = 144
    for bit in range(6):
        x = bit * 64 + 32
        offset = (row * width + x) * 3
        red, green, blue = frame[offset:offset + 3]
        if (red + green + blue) / 3 > 127:
            frame_id |= 1 << bit
    return frame_id


def test_shifted_container_and_video_origins_preserve_exact_trim_and_frame_grid(tmp_path):
    """Real FFmpeg and decoded frame IDs prove stream-origin normalization, not duration alone."""
    root = ProjectRoot(tmp_path)
    frames_rel = "_work/pts/frames"
    root.make_dir(frames_rel)
    for index in range(60):
        root.write_bytes(f"{frames_rel}/frame_{index:03d}.ppm",
                         b"P6\n384 288\n255\n" + _identity_frame(index))
    video_only = root.resolve("_work/pts/video-only.mkv")
    source = root.resolve("shifted-source.mkv")
    _ffmpeg(root, "-y", "-framerate", "10", "-start_number", "0", "-i",
            str(root.resolve(frames_rel + "/frame_%03d.ppm")), "-frames:v", "60",
            "-c:v", "ffv1", "-pix_fmt", "yuv444p", str(video_only))
    _ffmpeg(root, "-y", "-itsoffset", "6", "-i", str(video_only), "-itsoffset", "4.5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "pcm_s16le",
            "-shortest", "-avoid_negative_ts", "disabled", str(source))
    source_probe = _ffprobe(root, source)
    video_stream = next(row for row in source_probe["streams"] if row["codec_type"] == "video")
    container_start = float(source_probe["format"]["start_time"])
    video_start = float(video_stream["start_time"])
    assert container_start > 0, f"source container origin was not nonzero: {container_start}"
    assert video_start > 0, f"source video stream origin was not nonzero: {video_start}"
    assert abs(container_start - video_start) > 0.5, (
        f"container/video origins were not distinct enough to expose double offset: {container_start}, {video_start}"
    )

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    request = H3Request(
        shot_id="pts", shot_version=1, run_id="shifted-origin", video_path="shifted-source.mkv",
        source_sha256=source_hash, source_output_id="master-pts", prompt="frame identity",
        start_seconds=1.2, duration_seconds=2.0, width=384, height=288, fps=10, steps=20,
        experimental=True,
    )
    settings = Settings.model_validate({"project_root": root.path})
    receipt = NativeBackend(root, settings).prepare_video(request, run_rel="_work/pts/prepared")
    proxy = root.resolve(receipt["path"])
    output_probe = _ffprobe(root, proxy)
    output_video = next(row for row in output_probe["streams"] if row["codec_type"] == "video")
    assert int(output_video["nb_read_frames"]) == 20, (
        f"prepared frame count was {output_video.get('nb_read_frames')!r}; requested 20 frames"
    )
    fps = output_video.get("avg_frame_rate", output_video["r_frame_rate"])
    assert fps == "10/1", f"prepared fps grid was {fps!r}; requested exact 10/1 fps"
    frame_bytes = 384 * 288 * 3
    decoded = _ffmpeg(root, "-i", str(proxy), "-map", "0:v:0", "-fps_mode", "passthrough",
                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
    assert len(decoded) == 20 * frame_bytes, (
        f"real decoded byte count was {len(decoded)}; expected 20 RGB24 frames ({20 * frame_bytes} bytes)"
    )
    identities = [_decode_frame_id(decoded[offset:offset + frame_bytes])
                  for offset in range(0, len(decoded), frame_bytes)]
    expected = list(range(12, 32))
    assert identities == expected, (
        f"decoded frame identities were {identities!r}; requested source content IDs {expected!r}"
    )
    assert identities[0] == 12, f"first decoded frame identity was {identities[0]}, expected source frame 12"
    assert identities[-1] == 31, f"last decoded frame identity was {identities[-1]}, expected source frame 31"
    assert receipt["source_sha256"] == source_hash, f"preparation receipt source hash was {receipt['source_sha256']!r}"
    proxy_hash = hashlib.sha256(proxy.read_bytes()).hexdigest()
    assert receipt["sha256"] == proxy_hash, (
        f"prepared proxy hash was {receipt['sha256']!r}, but real decoded proxy bytes hash to {proxy_hash}"
    )
