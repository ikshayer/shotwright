"""Blind native control tests with real CPU child trees, never model inference.

The verification/media seams return explicitly controlled fixture metadata. OS
process absence and sentry survival are measured independently of those seams.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError

SUPERVISOR = r'''
import json, os, subprocess, sys, time
sys.path.insert(0, sys.argv[2])
import psutil
from orchestrator.platform.containment import ProjectRoot
root = ProjectRoot(sys.argv[1])
child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(90)'],
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
identity = {'pid': child.pid, 'create_time': psutil.Process(child.pid).create_time(),
            'parent_pid': os.getpid(), 'live_identity_verified': True}
base = {'session': sys.argv[3], 'url': 'http://127.0.0.1:18122',
        'owned_process_identities': [identity],
        'system_stats': {'devices': [{'name': 'RTX 5070 Ti Laptop', 'type': 'cuda'}]},
        'queue_at_ready': {'queue_running': [], 'queue_pending': []},
        'fixture_only_no_gpu': True}
root.write_text('started.json', json.dumps({**base, 'status': 'starting'}))
root.write_text('ready.json', json.dumps({**base, 'status': 'ready_not_queued'}))
if sys.argv[4] == 'early_exit':
    child.terminate()
    child.wait(timeout=3)
    sys.exit(4)
try:
    while not root.resolve('STOP').exists():
        time.sleep(.02)
finally:
    failures = []
    try:
        if sys.argv[4] != 'unsafe_cleanup':
            child.terminate()
            child.wait(timeout=3)
    except BaseException as error:
        failures.append(str(error))
    alive = psutil.pid_exists(child.pid)
    stopped = {
        'status': 'stopped', 'watchdog_joined': sys.argv[4] != 'unjoined_watchdog',
        'watchdog_failure': ({'type': 'TimeoutError', 'error': 'fixture native execution deadline'}
            if sys.argv[4] == 'native_cutoff' else
            {'type': 'InterruptedError', 'error': 'Creator requested session stop'}),
        'cleanup': {'verified_stopped': not alive and not failures,
                    'identities': [identity], 'survivors': [identity] if alive else [],
                    'unverifiable_identities': [], 'failures': failures},
        'fixture_only_no_gpu': True}
    if sys.argv[4] != 'missing_cleanup':
        root.write_text('stopped.json', json.dumps(stopped))
'''


def wait_for(predicate, *, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    pytest.fail('controlled CPU fixture did not reach its bounded state')


def identity_alive(pid, created):
    try:
        return psutil.Process(pid).create_time() == created
    except psutil.NoSuchProcess:
        return False


def schema_for(graph):
    """Controlled object_info fixture for precisely the submitted graph."""
    result = {}
    for node in graph.values():
        inputs = {}
        for name, value in node['inputs'].items():
            if isinstance(value, list):
                inputs[name] = ['ANY']
            elif isinstance(value, bool):
                inputs[name] = ['BOOLEAN', {}]
            elif isinstance(value, int):
                inputs[name] = ['INT', {'min': 0, 'max': 2**53 - 1}]
            elif isinstance(value, float):
                inputs[name] = ['FLOAT', {'min': 0, 'max': 16384, 'step': .1}]
            else:
                inputs[name] = ['STRING', {}]
        result[node['class_type']] = {'input': {'required': inputs}, 'output': ['ANY', 'ANY']}
    return result


@pytest.fixture
def native_control(tmp_path, monkeypatch):
    from orchestrator.adapters.minimax_h3 import H3Request
    from orchestrator.adapters.minimax_h3.core import build_api_graph
    from orchestrator.adapters.minimax_h3.native import NativeBackend
    from orchestrator.domain.entities import Job
    from orchestrator.platform.settings import Settings

    fixture_root = ProjectRoot(tmp_path)
    fixture_root.make_dir('project')
    fixture_root.make_dir('repository')
    project = ProjectRoot(fixture_root.resolve('project'))
    repository = ProjectRoot(fixture_root.resolve('repository'))
    project.write_bytes('renders/master.mp4', b'CPU source fixture')
    for name in ('controller.py', 'helper.py'):
        repository.write_text(name, '# controlled CPU fixture, no model imports\n')
    source_repository = Path(__file__).resolve().parents[3]
    helper_relative = 'spikes/22_minimax_h3_workflows/local_h3.py'
    repository.write_bytes(helper_relative, source_repository.joinpath(helper_relative).read_bytes())
    controller_relative = 'spikes/22_minimax_h3_workflows/direct_comfy.py'
    repository.write_bytes(controller_relative, source_repository.joinpath(controller_relative).read_bytes())
    settings = Settings.model_validate({'project_root': project.path, 'minimax_h3': {
        'enabled': True, 'required_vram_gb': 1.0, 'required_ram_gb': 1.0,
        'required_disk_gb': 1.0, 'throughput_floor_per_second': .001,
        'startup_allowance_seconds': 3, 'execution_timeout_seconds': 3,
        'wall_clock_seconds': 12}})
    request = H3Request(shot_id='s1', shot_version=1, run_id='cpu-control',
        video_path='renders/master.mp4', source_sha256=hashlib.sha256(b'CPU source fixture').hexdigest(),
        source_output_id='master1', prompt='quiet corridor')
    models = {'diffusion': 'fixture-diffusion.safetensors', 'text_encoder': 'fixture-encoder.safetensors',
              'video_vae': 'fixture-video.safetensors', 'audio_vae': 'fixture-audio.safetensors'}
    graph = build_api_graph(request, models=models, seed=1)
    state = SimpleNamespace(processes=[], sessions=[], posts=[], calls=[], mode='success', graph=None,
                            submitted=threading.Event(), source=request, repository=repository)

    def popen(argv, **kwargs):
        assert 'serve' in argv and '--session' in argv, argv
        relative = argv[argv.index('--session') + 1]
        session = repository.resolve(relative)
        assert session.is_dir()
        command = [sys.executable, '-B', '-c', SUPERVISOR, str(session),
                   str(Path(__file__).resolve().parents[3]), relative, state.mode]
        process = subprocess.Popen(command, **kwargs)
        state.processes.append((process, psutil.Process(process.pid).create_time()))
        state.sessions.append(ProjectRoot(session))
        wait_for(lambda: session.joinpath('ready.json').exists())
        if state.mode == 'early_exit':
            process.wait(timeout=4)
        return process

    def http(method, path, payload=None, *, timeout=10):
        del timeout
        state.calls.append((method, path))
        if path == '/system_stats':
            return {'devices': [{'name': 'RTX 5070 Ti Laptop', 'type': 'cuda'}]}
        if path == '/object_info':
            if state.mode == 'schema':
                return {}
            return schema_for(graph)
        if path == '/queue':
            if state.mode == 'queue_busy':
                return {'queue_running': [[123, 'foreign-prompt']], 'queue_pending': []}
            return {'queue_running': [], 'queue_pending': []}
        if method == 'POST' and path == '/prompt':
            state.posts.append(payload)
            state.graph = payload['prompt']
            state.submitted.set()
            if state.mode == 'ambiguous':
                raise TimeoutError('controlled lost POST response')
            state.sessions[-1].write_bytes('output/candidate.mp4', b'CPU generated AV fixture')
            return {'prompt_id': 'cpu-prompt', 'number': 0, 'node_errors': {}}
        if path.startswith('/history/'):
            if state.mode == 'keyboard':
                raise KeyboardInterrupt('controlled keyboard interruption')
            if state.mode == 'system_exit':
                raise SystemExit('controlled system exit')
            if state.mode in {'timeout', 'cancel'}:
                return {}
            executed = json.loads(json.dumps(state.graph))
            if state.mode == 'graph':
                executed['2']['inputs']['prompt'] = 'mutated graph'
            filename = '../outside.mp4' if state.mode == 'path' else 'candidate.mp4'
            status = 'error' if state.mode == 'history_error' else 'success'
            return {'cpu-prompt': {
                'prompt': [0, 'cpu-prompt', executed, {}, ['12']],
                'outputs': {'12': {'images': [{'filename': filename, 'subfolder': '', 'type': 'output',
                    'metadata': {'kind': 'video', 'width': 384, 'height': 288,
                                 'duration': 8, 'fps': 24, 'frame_count': 192}}], 'animated': [True]}},
                'status': {'status_str': status, 'completed': True,
                           'messages': [['execution_success', {'prompt_id': 'cpu-prompt'}]]}}}
        if path in {'/free', '/interrupt'}:
            return {}
        raise AssertionError(f'Unexpected controlled HTTP request: {method} {path}')

    resources = lambda: {'ram_available_bytes': 20 * 2**30, 'disk_free_bytes': 100 * 2**30,
                        'gpu_whole_device_mb': {'used_mb': 1000, 'total_mb': 12000, 'free_mb': 11000}}
    backend = NativeBackend(project, settings, popen=popen, http=http, resources=resources,
                            repository_root=repository)
    monkeypatch.setattr(backend, 'verify_runtime', lambda *, cancel=None: {
        'models': models, 'provenance': {'fixture_only_no_models': True},
        'runtime_python': sys.executable, 'comfy_root': str(repository.path),
        'controller_path': str(repository.resolve(controller_relative)),
        'helper_path': str(repository.resolve(helper_relative))})

    def prepare(request, *, run_rel, cancel=None):
        del cancel
        path = f'{run_rel}/input/model_proxy.mp4'
        project.write_bytes(path, b'CPU proxy fixture')
        return {'path': path, 'sha256': hashlib.sha256(b'CPU proxy fixture').hexdigest(),
                'frame_count': 192, 'fps': 24, 'width': 384, 'height': 288,
                'duration_seconds': 8, 'source_sha256': request.source_sha256,
                'command': ['controlled CPU preparation seam']}

    def export(request, *, run_rel, av_path, cancel=None):
        del request, cancel
        assert project.resolve(av_path).is_file()
        wav_path = f'{run_rel}/audio.wav'
        project.write_bytes(wav_path, b'CPU decoded WAV fixture')
        return {'av_path': av_path, 'av_sha256': hashlib.sha256(project.resolve(av_path).read_bytes()).hexdigest(),
                'wav_path': wav_path, 'wav_sha256': hashlib.sha256(b'CPU decoded WAV fixture').hexdigest(),
                'frame_count': 192, 'fps': 24, 'audio_sample_rate': 48000, 'audio_channels': 2,
                'audio_samples_per_channel': 384000, 'audio_metrics': {'fixture_only': True}}

    monkeypatch.setattr(backend, 'prepare_video', prepare)
    monkeypatch.setattr(backend, 'verify_and_export', export)
    state.backend = backend
    state.project = project
    state.settings = settings
    state.job = Job(job_id='cpu-job', type='h3_candidate', shot_id='s1', run_id=request.run_id)
    state.queue = SimpleNamespace(root=project, settings=settings)
    sentry = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(90)'],
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    state.sentry = sentry
    try:
        yield state
    finally:
        try:
            try:
                backend.close()
            except RuntimeError as error:
                state.close_error = str(error)
                if state.mode not in {'early_exit', 'missing_cleanup', 'unsafe_cleanup', 'unjoined_watchdog'}:
                    raise
        finally:
            # Teardown never counts as the backend's cleanup proof.
            for process, _ in state.processes:
                if process.poll() is None:
                    parent = psutil.Process(process.pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        child.kill()
                    psutil.wait_procs(children, timeout=3)
                    process.kill()
                process.wait(timeout=5)
            for session in state.sessions:
                ready_path = session.resolve('ready.json')
                if ready_path.is_file():
                    for identity in json.loads(ready_path.read_text())['owned_process_identities']:
                        try:
                            child = psutil.Process(identity['pid'])
                            if child.create_time() == identity['create_time']:
                                child.kill()
                                child.wait(timeout=5)
                        except psutil.NoSuchProcess:
                            pass
            sentry.terminate()
            sentry.wait(timeout=5)


def execute(control):
    return control.backend.execute(control.source, control.job, root=control.project,
        queue=control.queue, cancel=lambda: control.mode == 'cancel' and control.submitted.is_set(),
        progress=lambda _value: None)


def assert_owned_stopped(control):
    assert control.processes, 'native execution never launched the controlled supervisor'
    for process, created in control.processes:
        wait_for(lambda process=process: process.poll() is not None)
        assert not identity_alive(process.pid, created)
    for session in control.sessions:
        ready = json.loads(session.resolve('ready.json').read_text())
        for identity in ready['owned_process_identities']:
            assert not identity_alive(identity['pid'], identity['create_time']), (
                'owned CPU grandchild survived native cleanup')
    assert control.sentry.poll() is None, 'unrelated CPU sentry was terminated'


@pytest.mark.parametrize('mode', ['keyboard', 'system_exit', 'timeout', 'cancel'])
def test_native_interruptions_stop_real_owned_tree_and_preserve_sentry(native_control, mode):
    native_control.mode = mode
    with pytest.raises((JobError, KeyboardInterrupt, SystemExit)):
        execute(native_control)
    assert len(native_control.posts) == 1
    assert_owned_stopped(native_control)


@pytest.mark.parametrize('mode', ['ambiguous', 'graph', 'path', 'history_error'])
def test_native_single_submission_rejects_protocol_corruption(native_control, mode):
    native_control.mode = mode
    with pytest.raises(JobError):
        execute(native_control)
    assert len(native_control.posts) == 1, 'ambiguous or invalid native attempt was automatically retried'
    assert_owned_stopped(native_control)


def test_native_protocol_success_has_one_submission_and_real_safe_shutdown(native_control):
    result = execute(native_control)
    assert result.run_id == native_control.source.run_id
    assert len(native_control.posts) == 1
    for endpoint in ('/system_stats', '/object_info', '/queue'):
        assert ('GET', endpoint) in native_control.calls
    assert_owned_stopped(native_control)
    stopped = json.loads(native_control.sessions[-1].resolve('stopped.json').read_text())
    assert stopped['cleanup']['verified_stopped'] is True
    assert stopped['watchdog_joined'] is True


@pytest.mark.parametrize('mode', ['unsafe_cleanup', 'missing_cleanup',
                                 'unjoined_watchdog', 'native_cutoff'])
def test_native_never_succeeds_without_complete_safe_supervisor_receipt(native_control, mode):
    native_control.mode = mode
    with pytest.raises(JobError):
        execute(native_control)
    assert len(native_control.posts) == 1
    assert_owned_stopped(native_control)


@pytest.mark.parametrize('mode', ['schema', 'queue_busy', 'early_exit'])
def test_native_invalid_live_readiness_never_submits(native_control, mode):
    from orchestrator.adapters.minimax_h3.core import H3SetupError

    native_control.mode = mode
    with pytest.raises((JobError, H3SetupError) if mode == 'schema' else JobError):
        execute(native_control)
    assert native_control.posts == []
    assert_owned_stopped(native_control)
