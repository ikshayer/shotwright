"""Registry/security H3 checks authored independently from the contract."""
import pytest

from .conftest import operations
from .test_parity import _cli, _command_names, _manifest, _runner_and_command

BASE = '/projects/p1/episodes/ep1/shots/s1/h3-candidates'
EXPECTED = {'generate_h3_candidate', 'list_h3_candidates',
            'cancel_h3_candidate', 'inspect_h3'}


def test_h3_has_http_cli_and_manifest_parity(app):
    names = {op.name for op in operations(app)}
    assert EXPECTED <= names
    manifest = {entry['name'] for entry in _manifest(app)}
    assert EXPECTED <= manifest
    cli = {name.replace('-', '_') for name in _command_names(_cli())}
    assert EXPECTED <= cli


@pytest.mark.parametrize('method,path', [('POST', BASE),
    ('POST', BASE + '/job1/cancel')])
def test_h3_requires_loopback_token(client, method, path):
    response = client.request(method, path, json={})
    assert response.status_code in (401, 403)


def test_inspection_is_available_with_h3_disabled(client, auth):
    response = client.get('/models/minimax-h3', headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()


def test_inspection_does_not_verify_weights_probe_gpu_or_launch_process(client, auth, monkeypatch):
    import subprocess

    from orchestrator.adapters.minimax_h3.native import NativeBackend
    from orchestrator.jobs.lease import PynvmlProbe

    def forbidden(*_args, **_kwargs):
        pytest.fail('inspection attempted runtime verification, GPU probing or process launch')

    monkeypatch.setattr(NativeBackend, 'verify_runtime', forbidden)
    monkeypatch.setattr(PynvmlProbe, '__init__', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    response = client.get('/models/minimax-h3', headers=auth)
    assert response.status_code == 200, response.text


def test_h3_rejects_untrusted_host(client, auth):
    response = client.get('/models/minimax-h3', headers={**auth, 'Host': 'evil.example'})
    assert response.status_code in (400, 403)


@pytest.mark.parametrize('body', [
    {},
    {'expected_version': 1, 'shot_version': 1, 'video_path': 'renders/master.mp4'},
    {'expected_version': 1, 'shot_version': 1, 'video_path': 'renders/master.mp4',
     'prompt': 'quiet', 'prompt_file': 'prompt.txt'},
    {'expected_version': 0, 'shot_version': 1, 'video_path': 'renders/master.mp4', 'prompt': 'quiet'},
])
def test_invalid_h3_body_is_422_before_submission(client, auth, body):
    response = client.post(BASE, headers=auth, json=body)
    assert response.status_code == 422, response.text


def test_cli_exposes_settings_and_rejects_no_wait_before_dispatch():
    runner, command = _runner_and_command(_cli())
    help_result = runner.invoke(command, ['--help'])
    assert help_result.exit_code == 0
    assert '--settings' in help_result.output
    result = runner.invoke(command, ['generate-h3-candidate', '--project-id', 'missing',
        '--episode-id', 'missing', '--shot-id', 'missing', '--no-wait', '--data',
        ('{"expected_version":1,"shot_version":1,"video_path":"master.mp4",'
        '"prompt_file":"prompt.txt"}')])
    assert result.exit_code != 0
    assert 'wait' in result.output.lower()
    assert 'not found' not in result.output.lower()


@pytest.fixture
def bound_h3(tmp_path):
    from fastapi.testclient import TestClient

    from orchestrator.api.app import create_app
    from orchestrator.platform.settings import Settings
    from tests.service.conftest import Environment
    from tests.service.test_minimax_h3 import seed_source

    from .conftest import LOOPBACK_BASE, TOKEN

    env = Environment(tmp_path / 'p1')
    seed_source(env.root.path)
    settings = Settings.model_validate({'project_root': tmp_path, 'minimax_h3': {
        'enabled': True, 'required_vram_gb': 1.25, 'required_ram_gb': 2.5,
        'required_disk_gb': 3.75, 'throughput_floor_per_second': 0.01}})
    env.settings.minimax_h3 = settings.minimax_h3
    env.adapter.release.set()
    app = create_app(settings=settings, token=TOKEN, job_runner=env.runner)
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
        yield client, env, settings
    env.runner.close()


def valid_body(**updates):
    return {'expected_version': 1, 'shot_version': 1,
            'video_path': 'renders/s1/v0001/master.mp4', 'prompt': 'quiet', **updates}


def test_bound_h3_submission_is_202_with_operator_demands(bound_h3, auth):
    client, env, _ = bound_h3
    response = client.post(BASE, headers=auth, json=valid_body())
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload['job_id'] and payload['run_id']
    assert payload['state'] in {'queued', 'running'}
    job = env.persisted_job(payload['job_id'])
    assert job.run_id == payload['run_id']
    assert (job.required_vram_gb, job.required_ram_gb, job.required_disk_gb) == (1.25, 2.5, 3.75)


def test_stale_token_is_409_without_job_creation(bound_h3, auth):
    client, env, _ = bound_h3
    before = {p.relative_to(env.root.path).as_posix() for p in env.root.path.rglob('*')}
    response = client.post(BASE, headers=auth, json=valid_body(expected_version=99))
    assert response.status_code == 409, response.text
    after = {p.relative_to(env.root.path).as_posix() for p in env.root.path.rglob('*')}
    assert after == before


def test_bound_source_hash_failure_is_422(bound_h3, auth):
    client, env, _ = bound_h3
    env.root.write_bytes('renders/s1/v0001/master.mp4', b'changed after receipt')
    response = client.post(BASE, headers=auth, json=valid_body())
    assert response.status_code == 422, response.text


def test_disabled_h3_is_actionable_503(bound_h3, auth):
    client, _, settings = bound_h3
    settings.minimax_h3.enabled = False
    response = client.post(BASE, headers=auth, json=valid_body())
    assert response.status_code == 503, response.text
    assert 'enable' in response.text.lower() or 'disabled' in response.text.lower()


def test_candidate_list_computes_stale_without_rewriting_receipt(bound_h3, auth):
    from orchestrator.domain.entities import Shot
    from tests.service.test_minimax_h3 import candidate_record

    client, env, _ = bound_h3
    candidate = candidate_record(env.root)
    env.store.save(candidate, expected_version=0)
    receipt = env.root.resolve('generated/h3/candidates/candidate1.json')
    original = receipt.read_bytes()

    def entry():
        response = client.get(BASE, headers=auth)
        assert response.status_code == 200, response.text
        body = response.json()
        records = body if isinstance(body, list) else body['candidates']
        assert len(records) == 1
        return records[0]

    assert entry()['stale'] is False
    shot = env.store.load(Shot, 's1', episode_id='ep1')
    env.store.revise(shot.model_copy(update={**shot.next_version(),
        'narrative_beat': 'later content'}), expected_version=1)
    assert entry()['stale'] is True
    assert receipt.read_bytes() == original


def test_cancel_requires_current_shot_token(bound_h3, auth):
    client, _, _ = bound_h3
    submitted = client.post(BASE, headers=auth, json=valid_body())
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()['job_id']
    response = client.post(f'{BASE}/{job_id}/cancel', headers=auth,
                           json={'expected_version': 99})
    assert response.status_code == 409, response.text


def test_cancel_cannot_cross_shot_binding(bound_h3, auth):
    client, _, _ = bound_h3
    submitted = client.post(BASE, headers=auth, json=valid_body())
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()['job_id']
    wrong_shot = BASE.replace('/shots/s1/', '/shots/foreign/')
    response = client.post(f'{wrong_shot}/{job_id}/cancel', headers=auth,
                           json={'expected_version': 1})
    assert response.status_code in (404, 422), response.text


def cli_context_for(monkeypatch, env):
    from orchestrator.api import cli

    original = cli.OperationContext
    seen = []

    def context(*args, **kwargs):
        seen.append(args[0] if args else kwargs['settings'])
        kwargs['job_runner'] = env.runner
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, 'OperationContext', context)
    return seen


@pytest.mark.parametrize('registration_failure', [False, True])
def test_cli_settings_prompt_file_and_wait_deliver_candidate_links(bound_h3, monkeypatch, registration_failure):
    import hashlib
    import json

    from orchestrator.adapters.minimax_h3 import H3Result
    from orchestrator.domain.entities import H3Candidate
    from orchestrator.platform.containment import ProjectRoot
    from orchestrator.service.runner import AdapterResult

    _, env, settings = bound_h3
    seen = cli_context_for(monkeypatch, env)
    parent = ProjectRoot(settings.project_root)
    parent.write_text('h3-settings.toml', f"project_root = '{settings.project_root}'\n"
        '[minimax_h3]\nenabled = true\nrequired_vram_gb = 1.25\n'
        'required_ram_gb = 2.5\nrequired_disk_gb = 3.75\nthroughput_floor_per_second = 0.01\n')
    prompt = '  廊下の音\n\nquiet.  '
    env.root.write_bytes('prompts/h3.txt', prompt.encode('utf-8'))
    results = []

    def invoke(adapter_request, job, *, queue):
        request = adapter_request.payload['request']
        assert request.prompt == prompt
        assert queue.lease._owned
        fields = {}
        for key, leaf in [('av', 'candidate.mp4'), ('wav', 'audio.wav'), ('report', 'report.json')]:
            path = f'generated/h3/s1/v0001/{request.run_id}/{leaf}'
            data = b'{}' if key == 'report' else key.encode()
            env.root.write_bytes(path, data)
            fields[key + '_path'] = path
            fields[key + '_sha256'] = hashlib.sha256(data).hexdigest()
        result = H3Result(run_id=request.run_id, frame_count=192, fps=24,
            audio_sample_rate=48000, audio_channels=2, audio_samples_per_channel=384000,
            seed=42, experimental=False, **fields)
        results.append(result)
        return AdapterResult(artifact_path=result.av_path, value=result)

    monkeypatch.setattr(env.adapter, 'invoke', invoke)
    if registration_failure:
        original_save = env.store.save

        def save(record, *args, **kwargs):
            if isinstance(record, H3Candidate):
                raise OSError('controlled CLI candidate registration failure')
            return original_save(record, *args, **kwargs)

        monkeypatch.setattr(env.store, 'save', save)
    runner, command = _runner_and_command(_cli())
    body = valid_body()
    body.pop('prompt')
    body['prompt_file'] = 'prompts/h3.txt'
    result = runner.invoke(command, ['--settings', str(parent.resolve('h3-settings.toml')),
        'generate-h3-candidate', '--project-id', 'p1', '--episode-id', 'ep1',
        '--shot-id', 's1', '--data', json.dumps(body), '--json'])
    assert seen and seen[0].minimax_h3.required_vram_gb == 1.25
    assert len(results) == 1
    if registration_failure:
        assert result.exit_code != 0
        assert not env.store.list_h3_candidates('s1', episode_id='ep1')
        run = json.loads(env.root.resolve(f'generations/{results[0].run_id}.json').read_text(encoding='utf-8'))
        assert run['status'] == 'completed'
        return
    assert result.exit_code == 0, result.output
    for path in (results[0].av_path, results[0].wav_path, results[0].report_path):
        assert path in result.output


def test_cli_keyboard_interrupt_requests_cancel_and_drains_real_runner(bound_h3, monkeypatch):
    import json
    import threading

    from orchestrator.domain.entities import Shot
    from orchestrator.platform.errors import JobError, JobErrorKind

    _, env, settings = bound_h3
    cli_context_for(monkeypatch, env)
    entered = threading.Event()
    finished = threading.Event()
    cancel_check = [lambda: False]
    jobs = []
    before = env.store.load(Shot, 's1', episode_id='ep1').model_dump(mode='json')

    def set_cancel_check(check):
        cancel_check[0] = check or (lambda: False)

    def invoke(_request, job, *, queue):
        assert queue.lease._owned
        jobs.append(job.job_id)
        entered.set()
        try:
            assert finished.wait(0.01) is False
            for _ in range(500):
                if cancel_check[0]():
                    raise JobError(JobErrorKind.CANCELLED, 'controlled CLI cancellation')
                if finished.wait(.01):
                    break
            raise AssertionError('CLI did not signal cancellation to its controlled adapter')
        finally:
            finished.set()

    monkeypatch.setattr(env.adapter, 'set_cancel_check', set_cancel_check, raising=False)
    monkeypatch.setattr(env.adapter, 'invoke', invoke)
    original_wait = env.runner.wait
    interrupted = []

    def wait(*args, **kwargs):
        if not interrupted:
            assert entered.wait(4), 'CLI never began the submitted H3 attempt'
            interrupted.append(True)
            raise KeyboardInterrupt
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(env.runner, 'wait', wait)
    runner, command = _runner_and_command(_cli())
    result = runner.invoke(command, ['generate-h3-candidate', '--project-id', 'p1',
        '--episode-id', 'ep1', '--shot-id', 's1', '--data', json.dumps(valid_body()), '--json'],
        obj={'settings': settings})
    assert result.exit_code != 0
    assert interrupted and finished.is_set(), 'CLI returned before adapter cancellation cleanup completed'
    assert len(jobs) == 1
    assert env.persisted_job(jobs[0]).state == 'cancelled'
    assert env.store.load(Shot, 's1', episode_id='ep1').model_dump(mode='json') == before
    assert not env.store.list_h3_candidates('s1', episode_id='ep1')


def test_cancel_token_check_serializes_with_concurrent_shot_revision(bound_h3, auth, monkeypatch):
    import threading

    from orchestrator.domain.entities import Shot

    client, env, _ = bound_h3
    env.adapter.release.clear()
    submitted = client.post(BASE, headers=auth, json=valid_body())
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()['job_id']
    assert env.adapter.entered.wait(3)
    cancel_entered = threading.Event()
    permit_cancel = threading.Event()
    revision_completed = threading.Event()
    results = []
    errors = []
    original_cancel = env.runner.cancel

    def cancel(*args, **kwargs):
        cancel_entered.set()
        assert permit_cancel.wait(4)
        return original_cancel(*args, **kwargs)

    def request_cancel():
        results.append(client.post(f'{BASE}/{job_id}/cancel', headers=auth,
                                   json={'expected_version': 1}))

    def revise():
        try:
            shot = env.store.load(Shot, 's1', episode_id='ep1')
            env.store.revise(shot.model_copy(update={**shot.next_version(),
                'narrative_beat': 'concurrent cancellation revision'}), expected_version=1)
        except Exception as error:  # noqa: BLE001 - retain thread failures for the main test
            errors.append(error)
        finally:
            revision_completed.set()

    monkeypatch.setattr(env.runner, 'cancel', cancel)
    cancellation = threading.Thread(target=request_cancel)
    editor = threading.Thread(target=revise)
    cancellation.start()
    try:
        assert cancel_entered.wait(3)
        editor.start()
        assert not revision_completed.wait(.2), 'Shot revised between cancellation token check and mutation'
    finally:
        permit_cancel.set()
        env.adapter.release.set()
        cancellation.join(timeout=5)
        if editor.ident is not None:
            editor.join(timeout=5)
    assert not cancellation.is_alive() and not editor.is_alive()
    assert not errors
    assert results and results[0].status_code == 200, results[0].text if results else 'no cancellation response'
