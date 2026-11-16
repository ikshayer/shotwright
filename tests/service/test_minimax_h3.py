"""Blind composition tests. No model, CUDA or implementation-text dependencies."""
import hashlib
import json

import pytest

from orchestrator.domain.entities import Episode, Project, Shot, ShotOutput, ShotOutputKind
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.store.json_store import ProjectStore, VersionConflict


@pytest.fixture
def source(tmp_path):
    return seed_source(tmp_path)


def seed_source(tmp_path):
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    store.save(Project(project_id='p1', title='H3 contract'), expected_version=0)
    store.save(Episode(episode_id='ep1', project_id='p1', number=1, duration_target_sec=30), expected_version=0)
    shot = Shot(shot_id='s1', episode_id='ep1', camera_profile='cam1', duration_sec=8)
    store.create(shot)
    path = 'renders/s1/v0001/master.mp4'
    data = b'production-source-not-reference'
    root.write_bytes(path, data)
    store.append_shot_output(ShotOutput(output_id='master1', episode_id='ep1', shot_id='s1',
        record_version=1, kind=ShotOutputKind.MASTER, relative_path=path,
        content_sha256=hashlib.sha256(data).hexdigest(), engine_id='BLENDER_EEVEE',
        engine_build='5.2.0'), expected_version=0)
    return root, store, shot, path


def compose(source, **updates):
    from orchestrator.service.minimax_h3 import H3GenerateBody, compose_h3_request
    _, store, _, path = source
    body = H3GenerateBody(expected_version=1, shot_version=1, video_path=path,
                          prompt='  Quiet corridor.\n秘密\n', **updates)
    return compose_h3_request(store, shot_id='s1', episode_id='ep1', body=body, run_id='h3-run')


def snapshot(root):
    return {p.relative_to(root.path).as_posix(): p.read_bytes()
            for p in root.path.rglob('*') if p.is_file()}


def test_composition_binds_exact_receipt_without_mutating_source(source):
    root, _, _, path = source
    before = snapshot(root)
    result = compose(source)
    assert result.video_path == path
    assert result.source_output_id == 'master1'
    assert result.source_sha256 == hashlib.sha256(b'production-source-not-reference').hexdigest()
    assert result.shot_version == 1
    assert result.prompt == '  Quiet corridor.\n秘密\n'
    assert snapshot(root) == before


def test_changed_source_hash_is_rejected_without_writes(source):
    root, _, _, path = source
    root.write_bytes(path, b'tampered')
    before = snapshot(root)
    with pytest.raises((ValueError, JobError)):
        compose(source)
    assert snapshot(root) == before


def test_stale_mutation_token_precedes_source_access(source):
    from orchestrator.service.minimax_h3 import H3GenerateBody, compose_h3_request
    _, store, _, _ = source
    body = H3GenerateBody(expected_version=99, shot_version=1,
                          video_path='references/forbidden.mp4', prompt='quiet')
    with pytest.raises(VersionConflict):
        compose_h3_request(store, shot_id='s1', episode_id='ep1', body=body, run_id='h3-run')


@pytest.mark.parametrize('path', ['references/video.mp4', 'unbound.mp4'])
def test_unbound_and_reference_media_rejected(source, path):
    from orchestrator.service.minimax_h3 import H3GenerateBody, compose_h3_request
    root, store, _, _ = source
    root.write_bytes(path, b'not production')
    body = H3GenerateBody(expected_version=1, shot_version=1, video_path=path, prompt='quiet')
    with pytest.raises(ValueError):
        compose_h3_request(store, shot_id='s1', episode_id='ep1', body=body, run_id='h3-run')


def test_historical_source_uses_current_token_and_exact_old_content_version(source):
    from orchestrator.service.minimax_h3 import H3GenerateBody, compose_h3_request
    root, store, shot, path = source
    changed = shot.model_copy(update={**shot.next_version(), 'narrative_beat': 'later edit'})
    store.revise(changed, expected_version=1)
    _, token = store.load_with_version(Shot, 's1', episode_id='ep1')
    before = snapshot(root)
    body = H3GenerateBody(expected_version=token, shot_version=1, video_path=path, prompt='old master')
    request = compose_h3_request(store, shot_id='s1', episode_id='ep1', body=body, run_id='historical')
    assert request.shot_version == 1
    assert request.source_output_id == 'master1'
    assert snapshot(root) == before


def test_prompt_file_is_utf8_exact_and_relative(source):
    from orchestrator.service.minimax_h3 import H3GenerateBody, compose_h3_request
    root, store, _, path = source
    prompt = '  空の廊下\n\nquiet.  '
    root.write_bytes('prompts/h3.txt', prompt.encode('utf-8'))
    body = H3GenerateBody(expected_version=1, shot_version=1, video_path=path,
                          prompt_file='prompts/h3.txt')
    request = compose_h3_request(store, shot_id='s1', episode_id='ep1', body=body, run_id='prompt-file')
    assert request.prompt.encode('utf-8') == prompt.encode('utf-8')


def test_receipt_for_other_content_version_is_not_rebound(source):
    from orchestrator.service.minimax_h3 import H3GenerateBody, compose_h3_request
    _, store, shot, path = source
    changed = shot.model_copy(update={**shot.next_version(), 'narrative_beat': 'later edit'})
    store.revise(changed, expected_version=1)
    _, token = store.load_with_version(Shot, 's1', episode_id='ep1')
    body = H3GenerateBody(expected_version=token, shot_version=2, video_path=path, prompt='quiet')
    with pytest.raises(ValueError):
        compose_h3_request(store, shot_id='s1', episode_id='ep1', body=body, run_id='wrong-version')


def candidate_record(root):
    from orchestrator.domain.entities import H3Candidate
    hashes = {}
    for name, path in [('av', 'generated/h3/s1/v0001/h3-run/candidate.mp4'),
                       ('wav', 'generated/h3/s1/v0001/h3-run/audio.wav'),
                       ('report', 'generated/h3/s1/v0001/h3-run/report.json')]:
        data = b'{}' if name == 'report' else name.encode()
        root.write_bytes(path, data)
        hashes[name + '_path'] = path
        hashes[name + '_sha256'] = hashlib.sha256(data).hexdigest()
    return H3Candidate(candidate_id='candidate1', episode_id='ep1', shot_id='s1',
        shot_version=1, job_id='job1', run_id='h3-run', source_output_id='master1',
        source_path='renders/s1/v0001/master.mp4',
        source_sha256=hashlib.sha256(b'production-source-not-reference').hexdigest(),
        prompt_sha256=hashlib.sha256(b'quiet').hexdigest(), seed=42,
        frame_count=192, fps=24, audio_sample_rate=48000, audio_channels=2,
        audio_samples_per_channel=384000, experimental=False, **hashes)


def test_candidate_is_immutable_relative_and_reopens_without_runtime(source):
    root, store, _, _ = source
    candidate = candidate_record(root)
    source_before = store.load(Shot, 's1', episode_id='ep1').model_dump(mode='json')
    episode_before = store.load(Episode, 'ep1').model_dump(mode='json')
    outputs_before = store.list_shot_outputs('s1', episode_id='ep1')
    store.save(candidate, expected_version=0)
    assert root.resolve('generated/h3/candidates/candidate1.json').is_file()
    reopened = ProjectStore(ProjectRoot(root.path))
    assert list(reopened.list_h3_candidates('s1', episode_id='ep1')) == [candidate]
    assert not reopened.list_h3_candidates('s1', episode_id='other')
    with pytest.raises((ValueError, VersionConflict, JobError)):
        store.save(candidate.model_copy(update={'seed': 43}), expected_version=1)
    assert list(reopened.list_h3_candidates('s1', episode_id='ep1')) == [candidate]
    assert store.load(Shot, 's1', episode_id='ep1').model_dump(mode='json') == source_before
    assert store.load(Episode, 'ep1').model_dump(mode='json') == episode_before
    assert store.list_shot_outputs('s1', episode_id='ep1') == outputs_before


@pytest.mark.parametrize('field,value', [
    ('av_path', '../outside.mp4'), ('wav_path', 'D:/outside.wav'),
    ('report_path', '//server/share/report.json'), ('source_path', '/outside.mp4'),
    ('source_sha256', 'A' * 64), ('prompt_sha256', 'bad'),
    ('frame_count', 0), ('fps', True), ('audio_sample_rate', 0),
    ('audio_channels', 0), ('audio_samples_per_channel', 0),
])
def test_candidate_rejects_unsafe_paths_and_invalid_timing(source, field, value):
    from orchestrator.domain.entities import H3Candidate
    root, _, _, _ = source
    values = candidate_record(root).model_dump()
    values[field] = value
    with pytest.raises(ValueError):
        H3Candidate.model_validate(values)


@pytest.mark.parametrize('outcome', ['success', 'historical', 'failure', 'wrong_run', 'metadata_failure', 'cancel'])
def test_real_queue_h3_never_changes_shot_episode_or_normal_outputs(env, monkeypatch, outcome):
    from orchestrator.adapters.minimax_h3 import H3Result
    from orchestrator.platform.errors import JobError, JobErrorKind
    from orchestrator.platform.settings import Settings
    from orchestrator.service.runner import AdapterKind, AdapterResult

    seeded = seed_source(env.root.path)
    enabled = Settings.model_validate({**env.settings.model_dump(), 'minimax_h3': {
        'enabled': True, 'required_vram_gb': 1.0, 'required_ram_gb': 1.0,
        'required_disk_gb': 1.0, 'throughput_floor_per_second': 0.01}})
    env.settings.minimax_h3 = enabled.minimax_h3
    request = compose(seeded)
    before_shot = env.store.load(Shot, 's1', episode_id='ep1').model_dump(mode='json')
    before_episode = env.store.load(Episode, 'ep1').model_dump(mode='json')
    before_outputs = env.store.list_shot_outputs('s1', episode_id='ep1')
    expected_shot = [before_shot]
    calls = []
    queue_run = env._queue.run

    def counted_run(*args, **kwargs):
        calls.append(True)
        return queue_run(*args, **kwargs)

    monkeypatch.setattr(env._queue, 'run', counted_run)
    invocations = []

    def invoke(adapter_request, job, *, queue):
        invocations.append(job.job_id)
        assert queue.lease._owned
        assert adapter_request.payload['request'] == request
        assert env.store.load(Shot, 's1', episode_id='ep1').model_dump(mode='json') == before_shot
        if outcome == 'historical':
            shot = env.store.load(Shot, 's1', episode_id='ep1')
            env.store.revise(shot.model_copy(update={**shot.next_version(),
                'narrative_beat': 'edited during H3'}), expected_version=1)
            expected_shot[0] = env.store.load(Shot, 's1', episode_id='ep1').model_dump(mode='json')
        if outcome == 'failure':
            env.root.write_text(f'generated/h3/s1/v0001/{request.run_id}/report.json',
                                '{"status":"failed","fixture_only":true}')
            raise JobError(JobErrorKind.INTERNAL, 'controlled H3 failure')
        result_fields = {}
        for name, filename in [('av', 'candidate.mp4'), ('wav', 'audio.wav'), ('report', 'report.json')]:
            path = f'generated/h3/s1/v0001/{request.run_id}/{filename}'
            data = b'{}' if name == 'report' else name.encode()
            env.root.write_bytes(path, data)
            result_fields[name + '_path'] = path
            result_fields[name + '_sha256'] = hashlib.sha256(data).hexdigest()
        result = H3Result(run_id='foreign-run' if outcome == 'wrong_run' else request.run_id,
            frame_count=192, fps=24,
            audio_sample_rate=48000, audio_channels=2, audio_samples_per_channel=384000,
            seed=42, experimental=False, **result_fields)
        return AdapterResult(artifact_path=result.av_path, value=result)

    monkeypatch.setattr(env.adapter, 'invoke', invoke)
    if outcome == 'metadata_failure':
        from orchestrator.domain.entities import H3Candidate

        original_save = env.store.save

        def save(record, *args, **kwargs):
            if isinstance(record, H3Candidate):
                raise OSError('controlled candidate registration failure after run save')
            return original_save(record, *args, **kwargs)

        monkeypatch.setattr(env.store, 'save', save)
    job = env.runner.submit('p1', env.request(kind=AdapterKind.H3_CANDIDATE,
        payload={'request': request, 'expected_version': 1}, required_vram_gb=1.0,
        required_ram_gb=1.0, required_disk_gb=1.0))
    assert job.run_id == request.run_id
    if outcome == 'cancel':
        env.runner.cancel('p1', job.job_id)
    try:
        env.runner.drain()
    except JobError:
        pass
    assert env.store.load(Shot, 's1', episode_id='ep1').model_dump(mode='json') == expected_shot[0]
    assert env.store.load(Episode, 'ep1').model_dump(mode='json') == before_episode
    assert env.store.list_shot_outputs('s1', episode_id='ep1') == before_outputs
    expected_state = ('completed' if outcome in {'success', 'historical'} else
                      'cancelled' if outcome == 'cancel' else 'failed')
    assert env.persisted_job(job.job_id).state == expected_state
    candidates = env.store.list_h3_candidates('s1', episode_id='ep1')
    if outcome in {'success', 'historical'}:
        assert len(candidates) == 1
        assert candidates[0].candidate_id == request.run_id
        assert candidates[0].shot_version == 1
        run = json.loads(env.root.resolve(f'generations/{request.run_id}.json').read_text(encoding='utf-8'))
        assert run['invocation'] == request.prompt
        assert run['seed'] == 42
        assert run['status'] == 'completed'
        assert run['input_paths'] == [request.video_path]
        assert set(run['output_paths']) == {candidates[0].av_path, candidates[0].wav_path,
                                           candidates[0].report_path}
        assert run['settings']['experimental'] is False
    else:
        assert not candidates
        if outcome == 'failure':
            assert env.root.resolve(f'generations/{request.run_id}.json').is_file()
            assert env.root.resolve(f'generated/h3/s1/v0001/{request.run_id}/report.json').is_file()
        if outcome == 'metadata_failure':
            run_path = f'generations/{request.run_id}.json'
            run = json.loads(env.root.resolve(run_path).read_text(encoding='utf-8'))
            assert run['status'] == 'completed', 'valid tool computation was silently reclassified after registration failure'
            diagnostics = [path.read_text(encoding='utf-8', errors='replace') for path in
                env.root.path.rglob('*') if path.is_file()]
            assert any('controlled candidate registration failure after run save' in text
                       and run_path in text for text in diagnostics), 'missing truthful partial-commit diagnostic'
    assert len(invocations) == (0 if outcome == 'cancel' else 1)
    if outcome == 'cancel':
        assert len(calls) <= 1  # queue may observe the cancelled job without invoking the adapter
    else:
        assert len(calls) == 1


def test_candidate_records_where_in_the_source_the_clip_was_taken_from(source):
    """adapters/export/CONTRACT.md D4b.

    H3 resamples time -- a 26.0 s / 10 fps source becomes an 8.0 s / 24 fps candidate --
    so the delivery render cannot reconstruct the recording's wall clock from the
    candidate's own timeline. Without `start_seconds` the exported timestamp is monotonic
    and wrong, which is worse than an absent one because it does not announce itself.
    """
    from orchestrator.domain.entities import H3Candidate
    root, store, _, _ = source
    candidate = candidate_record(root).model_copy(update={'start_seconds': 12.5})
    store.save(candidate, expected_version=0)
    reloaded = store.load(H3Candidate, 'candidate1')
    assert reloaded.start_seconds == 12.5


def test_candidates_recorded_before_start_seconds_existed_still_load():
    """The field is defaulted only so existing records remain readable. 0.0 is correct
    for every candidate generated so far, all of which used the default start."""
    from orchestrator.domain.entities import H3Candidate
    legacy = {
        'candidate_id': 'legacy1', 'episode_id': 'ep1', 'shot_id': 's1', 'shot_version': 1,
        'job_id': 'job1', 'run_id': 'h3-run', 'source_output_id': 'master1',
        'source_path': 'renders/s1/v0001/master.mp4',
        'source_sha256': hashlib.sha256(b'x').hexdigest(),
        'prompt_sha256': hashlib.sha256(b'y').hexdigest(), 'seed': 42,
        'av_path': 'a.mp4', 'av_sha256': hashlib.sha256(b'a').hexdigest(),
        'wav_path': 'a.wav', 'wav_sha256': hashlib.sha256(b'b').hexdigest(),
        'report_path': 'r.json', 'report_sha256': hashlib.sha256(b'c').hexdigest(),
        'frame_count': 192, 'fps': 24, 'audio_sample_rate': 48000, 'audio_channels': 2,
        'audio_samples_per_channel': 384000, 'experimental': False,
    }
    assert 'start_seconds' not in legacy
    assert H3Candidate.model_validate(legacy).start_seconds == 0.0
