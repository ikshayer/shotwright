"""Independent real-queue GPU-time regressions motivated by opt-in H3.

Usage is seeded by actual queue execution, never by private in-memory counters.
Workers/probes/clocks are controlled CPU fixtures and make no GPU cleanup claim.
"""
from __future__ import annotations

import json
import threading

import pytest

from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings
from tests.jobs.test_queue import build, make_job, persisted


def h3_job(job_id, *, shot='s1', episode='ep1'):
    return make_job(job_id=job_id, type='h3_candidate', shot_id=shot, episode_id=episode,
                    required_vram_gb=1.0, required_ram_gb=1.0, required_disk_gb=1.0)


def budget_env(path, **budget):
    return build(path, budget={'retries_per_shot': 0, 'per_shot_wall_clock_seconds': 2400,
        'gpu_minutes_per_shot': 100, 'gpu_minutes_per_episode': 1000, **budget},
        resources={'startup_allowance_seconds': 600, 'throughput_floor_per_second': .001})


def ledger(env):
    return json.loads(env.root.read_text('jobs/budgets.json'))


def spend(env, seconds, *, job_id='seed', shot='s1', episode='ep1'):
    def work(job):
        job.progress = .5
        env.clock.now += seconds
        return 'controlled completed work'

    try:
        return env.queue.run(h3_job(job_id, shot=shot, episode=episode), work)
    except JobError as error:
        # Spending past the cap may fail the job, but must never erase actual usage.
        assert error.kind is JobErrorKind.BUDGET_EXCEEDED
        return error


@pytest.mark.parametrize('scope', ['shot', 'episode'])
def test_exhausted_gpu_budget_refuses_worker_after_queue_reopen(tmp_path, scope):
    limits = {f'gpu_minutes_per_{scope}': 1.0}
    env = budget_env(tmp_path, **limits)
    spend(env, 60.0)
    reopened = budget_env(tmp_path, **limits)
    calls = []
    shot = 's1' if scope == 'shot' else 'different-shot'
    with pytest.raises(JobError) as caught:
        reopened.queue.run(h3_job('refused', shot=shot), lambda job: calls.append(job.job_id))
    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert not caught.value.retryable
    assert calls == [], f'exhausted {scope} budget allowed a worker invocation'
    assert persisted(reopened.root, 'refused')['state'] == 'failed'


@pytest.mark.parametrize('scope', ['shot', 'episode'])
def test_overspent_gpu_budget_is_retained_and_never_creates_new_room(tmp_path, scope):
    limits = {f'gpu_minutes_per_{scope}': 1.0}
    env = budget_env(tmp_path, **limits)
    outcome = spend(env, 90.0)
    assert isinstance(outcome, JobError), 'over-cap elapsed time was reported as a successful bounded job'
    assert persisted(env.root, 'seed')['state'] == 'failed'
    usage = ledger(env)
    assert usage['shot_seconds']['s1'] >= 90
    assert usage['episode_seconds']['ep1'] >= 90
    reopened = budget_env(tmp_path, **limits)
    calls = []
    shot = 's1' if scope == 'shot' else 'different-shot'
    with pytest.raises(JobError) as caught:
        reopened.queue.run(h3_job('after-overspend', shot=shot), lambda job: calls.append(job.job_id))
    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert calls == [], 'overspend vanished after restart and admitted more model work'
    assert ledger(reopened) == usage


@pytest.mark.parametrize('ceiling', ['shot', 'episode', 'wall'])
def test_remaining_time_is_a_live_deadline_even_during_startup(tmp_path, ceiling):
    limits = {f'gpu_minutes_per_{ceiling}': 1 / 60} if ceiling != 'wall' else {
        'per_shot_wall_clock_seconds': .2}
    env = budget_env(tmp_path, **limits)
    if ceiling != 'wall':
        spend(env, .8)
    stopped = threading.Event()
    finished = threading.Event()
    terminate_calls = []
    attempts = []

    def terminate():
        terminate_calls.append(True)
        stopped.set()
        return True

    env.worker.terminate = terminate
    start = env.clock.now

    def work(job):
        attempts.append(job.job_id)
        job.progress = .5
        try:
            # Keep the attempt genuinely active until the queue stops it. The
            # finite fallback prevents a broken deadline from hanging the suite.
            for _ in range(1000):
                if stopped.wait(.001):
                    return 'stopped by queue'
                env.clock.now += .05
            return 'fallback exit; deadline did not stop work'
        finally:
            finished.set()

    with pytest.raises(JobError) as caught:
        env.queue.run(h3_job('bounded', shot='other' if ceiling == 'episode' else 's1'),
                      work, startup_allowance_seconds=600)
    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert not caught.value.retryable
    assert attempts == ['bounded']
    assert terminate_calls and stopped.is_set(), 'live time ceiling did not stop its owned fake worker'
    assert finished.is_set(), 'queue returned while its attempt remained active'
    assert env.clock.now - start < 5, 'remaining time was replaced by the much larger H3 wall allowance'
    assert persisted(env.root, 'bounded')['state'] == 'failed'


def test_h3_budget_regression_does_not_change_global_defaults():
    defaults = Settings()
    assert defaults.budget.gpu_minutes_per_shot == 20
    assert defaults.budget.gpu_minutes_per_episode == 240
    assert defaults.budget.per_shot_wall_clock_seconds == 900
    assert defaults.minimax_h3.wall_clock_seconds == 2400
    assert defaults.minimax_h3.enabled is False


@pytest.mark.parametrize('scope', ['shot', 'episode'])
def test_remaining_gpu_budget_is_rechecked_after_acquiring_lease(tmp_path, scope):
    env = budget_env(tmp_path, **{f'gpu_minutes_per_{scope}': 1.0})

    def consumed_while_waiting():
        env.root.write_text('jobs/budgets.json', json.dumps({
            'shot_seconds': {'s1': 60.0 if scope == 'shot' else 0.0},
            'episode_seconds': {'ep1': 60.0 if scope == 'episode' else 0.0}}))

    env.lease.after_acquire = consumed_while_waiting
    calls = []
    with pytest.raises(JobError) as caught:
        env.queue.run(h3_job('lost-budget'), lambda job: calls.append(job.job_id))
    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert calls == [], 'budget headroom vanished during lease wait but worker still started'


@pytest.mark.parametrize('scope', ['shot_seconds', 'episode_seconds'])
@pytest.mark.parametrize('invalid', [-1.0, float('nan'), float('inf'), 'not-seconds'])
def test_corrupt_consumption_is_never_silently_treated_as_free_budget(tmp_path, scope, invalid):
    env = budget_env(tmp_path)
    values = {'shot_seconds': {'s1': 0.0}, 'episode_seconds': {'ep1': 0.0}}
    values[scope]['s1' if scope == 'shot_seconds' else 'ep1'] = invalid
    env.root.write_text('jobs/budgets.json', json.dumps(values))
    calls = []
    with pytest.raises(JobError):
        env.queue.run(h3_job('corrupt-budget'), lambda job: calls.append(job.job_id))
    assert calls == []


def test_waiting_for_lease_does_not_consume_gpu_time(tmp_path):
    env = budget_env(tmp_path, gpu_minutes_per_shot=1 / 60)
    env.root.write_text('jobs/budgets.json', json.dumps({
        'shot_seconds': {'s1': .8}, 'episode_seconds': {'ep1': .8}}))
    env.lease.after_acquire = lambda: setattr(env.clock, 'now', env.clock.now + 2.0)
    calls = []

    def immediate(job):
        calls.append(job.job_id)
        return 'completed without GPU time'

    assert env.queue.run(h3_job('waited-not-computed'), immediate) == 'completed without GPU time'
    assert calls == ['waited-not-computed']
    assert ledger(env)['shot_seconds']['s1'] == pytest.approx(.8)
    assert ledger(env)['episode_seconds']['ep1'] == pytest.approx(.8)


def test_lease_wait_does_not_reset_original_absolute_wall_deadline(tmp_path):
    env = budget_env(tmp_path, per_shot_wall_clock_seconds=1.0)
    env.lease.after_acquire = lambda: setattr(env.clock, 'now', env.clock.now + 2.0)
    calls = []
    with pytest.raises(JobError) as caught:
        env.queue.run(h3_job('wall-expired-while-waiting'), lambda job: calls.append(job.job_id))
    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert calls == [], 'expired wall deadline was reset when the GPU lease became available'
