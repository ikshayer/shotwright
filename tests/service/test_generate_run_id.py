"""A4.10 HIGH: JobRunner.submit must carry a generate_media request's own run_id onto the Job.

Written from CONTRACT.md A4.10 only. Uses the real JobRunner from tests/service/conftest.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.adapters.comfyui.profiles import compose_image_request
from orchestrator.adapters.comfyui.workflow import ImageRequest
from tests.service.test_adapter_integration import _shot

RUN_ID = "abc123run"


def composed(run_id: str = RUN_ID) -> ImageRequest:
    return compose_image_request(
        profile="z_image", weights_dir=Path("C:/weights"), shot_id="s1", shot_version=1,
        degrade_seed=7, run_id=run_id, prompt="A quiet corridor")


def explicit_dict(run_id: object) -> dict[str, object]:
    return composed("placeholder").model_dump(mode="json") | {"run_id": run_id}


def submit_and_drain(env, payload):
    _shot(env.store)
    env.adapter.release.set()
    seen: list[tuple[str | None, str | None]] = []
    real_invoke = env.adapter.invoke

    def spy(request, job, *, queue):
        seen.append((job.shot_id, job.run_id))
        return real_invoke(request, job, queue=queue)

    env.adapter.invoke = spy
    job = env.runner.submit("p1", env.request(kind=env.AdapterKind.GENERATE, payload=payload))
    env.runner.drain()
    return job, seen


def test_image_request_payload_run_id_becomes_the_job_run_id(env) -> None:
    request = composed()
    job, seen = submit_and_drain(env, request)
    assert job.run_id == RUN_ID
    assert env.persisted_job(job.job_id).run_id == RUN_ID
    assert env.persisted_job(job.job_id).state == "completed"
    assert seen == [(request.shot_id, request.run_id)]


def test_adapter_identity_check_is_satisfied_end_to_end(env) -> None:
    request = composed()
    job, seen = submit_and_drain(env, request)
    (shot_id, run_id), = seen
    assert (shot_id, run_id) == (request.shot_id, request.run_id)
    assert job.run_id == request.run_id


@pytest.mark.parametrize("shape", ["wrapped", "bare"])
def test_explicit_request_dict_run_id_becomes_the_job_run_id(env, shape: str) -> None:
    body = {"shot_id": "s1", "run_id": "abc", "prompt": "x"}
    payload = {"request": body} if shape == "wrapped" else body
    job, seen = submit_and_drain(env, payload)
    assert job.run_id == "abc"
    assert env.persisted_job(job.job_id).run_id == "abc"
    assert seen == [("s1", "abc")]


@pytest.mark.parametrize("shape", ["wrapped", "bare"])
@pytest.mark.parametrize("bad", [None, "", "   ", 5, ["x"], "MISSING"])
def test_generate_dict_without_usable_run_id_gets_a_generated_nonblank_id(
        env, shape: str, bad: object) -> None:
    body: dict[str, object] = {"shot_id": "s1", "prompt": "x"}
    if bad != "MISSING":
        body["run_id"] = bad
    payload = {"request": body} if shape == "wrapped" else body
    job, seen = submit_and_drain(env, payload)
    assert isinstance(job.run_id, str) and job.run_id.strip()
    assert job.run_id != bad
    assert seen[0][1] == job.run_id


def test_generated_ids_are_fresh_per_job(env) -> None:
    first, _ = submit_and_drain(env, {"prompt": "x"})
    second = env.runner.submit(
        "p1", env.request(kind=env.AdapterKind.GENERATE, payload={"prompt": "x"}))
    env.runner.drain()
    assert first.run_id != second.run_id


def test_render_kind_still_gets_a_generated_nonblank_run_id(env) -> None:
    _shot(env.store)
    env.adapter.release.set()
    job = env.runner.submit("p1", env.request(kind=env.AdapterKind.RENDER))
    env.runner.drain()
    assert isinstance(job.run_id, str) and job.run_id.strip()
    assert env.persisted_job(job.job_id).run_id == job.run_id
