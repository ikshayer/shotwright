"""PerformanceEdit JobRunner and finalization tests."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.domain.entities import Asset, Episode, GenerationRun, Project, Shot
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import RecordNotFound
from tests.adapters.perfedit.test_contract import (
    APPLY_PATH,
    LOOPBACK_BASE,
    TOKEN,
    TOKEN_HEADER,
    apply_body,
    assert_map_tiles_output,
    assert_source_spans_rebuild,
    pcm_samples,
    perfedit_adapter_factory,
    perfedit_service,
    register_take,
    spans_for,
    submit_and_wait,
    take_bytes,
)


def test_success_finalizes_a_cpu_sidecar_with_rights_and_an_unchanged_shot(perfedit_service) -> None:
    client, _app, env, fake, lease_states, _adapter = perfedit_service
    source = register_take(env.store, env.root)
    shot_before, shot_version_before = env.store.load_with_version(Shot, "s1", episode_id="ep1")
    outputs_before = env.store.list_shot_outputs("s1", episode_id="ep1")
    job = submit_and_wait(
        client,
        env,
        apply_body(
            env.store,
            source,
            pauses=[{"index": 0, "target_ms": 500, "breath": {"duration_ms": 120, "gain_db": -9}}],
            segment_gain_db=[0, 2],
        ),
    )

    assert job.state == "completed", job.model_dump(mode="json")
    # The Job record carries its adapter kind in `type` (it has no `kind` field).
    assert job.type == "performance_edit"
    assert lease_states == [False], "PerformanceEdit must execute as a CPU job"
    run_id = job.run_id
    assert run_id
    run_dir = f"generated/perfedit/s1/v0001/{run_id}"
    take_path = f"{run_dir}/take.wav"
    report_path = f"{run_dir}/report.json"
    log_path = f"{run_dir}/logs/ffmpeg.log"
    assert env.root.resolve(take_path).is_file()
    assert env.root.resolve(report_path).is_file()
    assert env.root.resolve(log_path).is_file()

    output_bytes = env.root.read_bytes(take_path)
    output_sha = hashlib.sha256(output_bytes).hexdigest()
    rate, output_samples = pcm_samples(output_bytes)
    assert rate == 22050
    report = json.loads(env.root.read_text(report_path, encoding="utf-8"))
    assert report["algorithm_version"] == "1.0.0"
    assert_map_tiles_output(report, output_samples)
    _rate, source_samples = pcm_samples(env.root.read_bytes(source.file_path))
    source_spans = assert_source_spans_rebuild(report, source_samples, output_samples)
    assert len(source_spans) == 2
    assert len(spans_for(report, "breath")) == 1
    report_values = [value for _key, value in _walk_key_values(report)]
    assert source.checksum in report_values
    assert output_sha in report_values

    output_asset = env.store.load(Asset, f"perfedit-{run_id}")
    assert output_asset.type == "voice"
    assert output_asset.generated is True
    assert output_asset.provider == "performance_edit@1.0.0"
    assert output_asset.file_path == take_path
    assert output_asset.checksum == output_sha
    for field in (
        "license", "model_or_voice_license", "voice_consent", "voice_consent_basis", "reuse_scope",
    ):
        assert getattr(output_asset, field) == getattr(source, field)

    generation = env.store.load(GenerationRun, run_id)
    assert source.file_path in generation.input_paths
    assert generation.tool == "performance_edit"
    shot_after, shot_version_after = env.store.load_with_version(Shot, "s1", episode_id="ep1")
    assert shot_after == shot_before
    assert shot_version_after == shot_version_before
    assert env.store.list_shot_outputs("s1", episode_id="ep1") == outputs_before


def test_failed_breath_job_leaves_no_take_or_output_asset(perfedit_service) -> None:
    client, _app, env, fake, _lease_states, _adapter = perfedit_service
    fake.fail_on_breath = True
    source = register_take(env.store, env.root)
    body = apply_body(
        env.store,
        source,
        pauses=[{"index": 0, "target_ms": 500, "breath": {"duration_ms": 100, "gain_db": -8}}],
    )

    job = submit_and_wait(client, env, body)

    assert job.state == "failed", job.model_dump(mode="json")
    assert job.run_id
    take_path = f"generated/perfedit/s1/v0001/{job.run_id}/take.wav"
    assert not env.root.resolve(take_path).exists()
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"perfedit-{job.run_id}")


def test_source_asset_change_after_submission_is_rechecked_before_finalization(perfedit_service) -> None:
    client, _app, env, fake, _lease_states, adapter = perfedit_service
    source = register_take(env.store, env.root)
    mutations: list[Asset] = []

    class MutatingAdapter:
        def invoke(self, request, job, *, queue):
            current, token = env.store.load_with_version(Asset, source.asset_id)
            changed = current.model_copy(update={"checksum": "f" * 64})
            env.store.save(changed, expected_version=token)
            mutations.append(changed)
            return adapter.invoke(request, job, queue=queue)

        def close(self):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    env.registry.adapter = MutatingAdapter()
    job = submit_and_wait(
        client,
        env,
        apply_body(env.store, source, pauses=[{"index": 0, "target_ms": 500, "breath": None}]),
    )

    assert mutations, "the source registry must change after submission and before invocation"
    assert job.state == "failed", job.model_dump(mode="json")
    assert job.run_id
    run_dir = env.root.path / "generated" / "perfedit" / "s1" / "v0001" / job.run_id
    assert not (run_dir / "take.wav").exists()
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"perfedit-{job.run_id}")


@pytest.mark.parametrize(
    "change",
    ["withdraw_consent", "licensed_recording", "checksum", "path"],
)
def test_source_record_changes_after_submission_fail_before_output(perfedit_service, change) -> None:
    client, _app, env, _fake, _lease_states, _adapter = perfedit_service
    source = register_take(env.store, env.root)
    response = client.post(
        APPLY_PATH,
        json=apply_body(
            env.store,
            source,
            pauses=[{"index": 0, "target_ms": 500, "breath": None}],
        ),
        headers={TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 202, response.text

    current, token = env.store.load_with_version(Asset, source.asset_id)
    changes = {
        "withdraw_consent": {"voice_consent": None, "voice_consent_basis": None},
        "licensed_recording": {
            "voice_consent": "licensed_recording",
            "voice_consent_basis": "Test fixture consent record.",
        },
        "checksum": {"checksum": "f" * 64},
        "path": {"file_path": "assets/audio/changed/missing.wav"},
    }
    env.store.save(current.model_copy(update=changes[change]), expected_version=token)

    job = env.runner.wait("p1", response.json()["job_id"])

    assert job.state == "failed", job.model_dump(mode="json")
    assert job.run_id
    run_dir = env.root.path / "generated" / "perfedit" / "s1" / "v0001" / job.run_id
    assert not (run_dir / "take.wav").exists()
    assert not (run_dir / "report.json").exists()
    assert not (run_dir / "logs" / "ffmpeg.log").exists()
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"perfedit-{job.run_id}")


def test_cancel_during_breath_synthesis_kills_ffmpeg_and_writes_no_take_or_asset(
    perfedit_service,
) -> None:
    client, _app, env, fake, _lease_states, _adapter = perfedit_service
    fake.hang_on_breath = True
    source = register_take(env.store, env.root)
    response = client.post(
        APPLY_PATH,
        json=apply_body(
            env.store,
            source,
            pauses=[{"index": 0, "target_ms": 500, "breath": {"duration_ms": 100, "gain_db": -8}}],
        ),
        headers={TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    result = []
    wait_thread = threading.Thread(
        target=lambda: result.append(env.runner.wait("p1", job_id)), daemon=True,
    )
    wait_thread.start()
    assert fake.breath_started.wait(2), "the hung breath process was not started"
    assert fake.hanging_processes[0].communicate_started.wait(2)

    env.runner.cancel("p1", job_id)
    wait_thread.join(5)

    assert not wait_thread.is_alive(), "cancellation did not stop the active breath process"
    assert result and result[0].state == "cancelled", result[0].model_dump(mode="json") if result else None
    assert fake.hanging_processes[0].killed.is_set()
    assert result[0].run_id
    take_path = f"generated/perfedit/s1/v0001/{result[0].run_id}/take.wav"
    assert not env.root.resolve(take_path).exists()
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"perfedit-{result[0].run_id}")


def test_breath_ffmpeg_timeout_kills_hung_process_and_fails_job(perfedit_service, monkeypatch) -> None:
    client, _app, env, fake, _lease_states, _adapter = perfedit_service
    runner_module = importlib.import_module("orchestrator.adapters.perfedit.runner")
    monkeypatch.setattr(runner_module, "_FFMPEG_TIMEOUT_SECONDS", 0.01)
    fake.hang_on_breath = True
    source = register_take(env.store, env.root)
    job = submit_and_wait(
        client,
        env,
        apply_body(
            env.store,
            source,
            pauses=[{"index": 0, "target_ms": 500, "breath": {"duration_ms": 100, "gain_db": -8}}],
        ),
    )

    assert job.state == "failed", job.model_dump(mode="json")
    assert fake.hanging_processes
    assert fake.hanging_processes[0].killed.is_set()
    assert job.run_id
    take_path = f"generated/perfedit/s1/v0001/{job.run_id}/take.wav"
    assert not env.root.resolve(take_path).exists()
    with pytest.raises(RecordNotFound):
        env.store.load(Asset, f"perfedit-{job.run_id}")


def test_real_ffmpeg_two_breaths_and_jitter_are_byte_deterministic(
    tmp_path: Path, perfedit_adapter_factory,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not on PATH")

    projects = tmp_path / "projects"
    project_path = projects / "p1"
    from tests.service.conftest import Environment

    env = Environment(project_path)
    env.settings.binaries.ffmpeg_exe = Path(ffmpeg)
    env.store.save(Project(project_id="p1", title="Real PerformanceEdit determinism"), expected_version=0)
    env.store.save(Episode(episode_id="ep1", project_id="p1", number=1, duration_target_sec=30), expected_version=0)
    env.store.create(Shot(shot_id="s1", episode_id="ep1", duration_sec=10))
    source = register_take(env.store, env.root, data=take_bytes())
    env.registry.adapter = perfedit_adapter_factory(env.root, env.settings, subprocess.Popen)
    settings = Settings.model_validate({"project_root": projects, "binaries": {"ffmpeg_exe": ffmpeg}})
    from orchestrator.api.app import create_app

    app = create_app(settings=settings, token=TOKEN, job_runner=env.runner)
    body = apply_body(
        env.store,
        source,
        seed=901,
        pauses=[
            {"index": 0, "target_ms": 500, "breath": {"duration_ms": 120, "gain_db": -8}},
            {"index": 1, "target_ms": 700, "breath": {"duration_ms": 180, "gain_db": -11}},
        ],
        segment_gain_db=[0, 1.5],
        jitter={"gap_ms_range": [-1.0, 1.0], "gain_db_range": [-0.5, 0.5]},
    )

    try:
        with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as client:
            outputs: list[bytes] = []
            reports: list[dict[str, Any]] = []
            for _ in range(2):
                response = client.post(APPLY_PATH, json=body, headers={TOKEN_HEADER: TOKEN})
                assert response.status_code == 202, response.text
                job = env.runner.wait("p1", response.json()["job_id"])
                assert job.state == "completed", job.model_dump(mode="json")
                assert job.run_id
                take_path = f"generated/perfedit/s1/v0001/{job.run_id}/take.wav"
                report_path = f"generated/perfedit/s1/v0001/{job.run_id}/report.json"
                outputs.append(env.root.read_bytes(take_path))
                reports.append(json.loads(env.root.read_text(report_path, encoding="utf-8")))

            assert outputs[0] == outputs[1]
            rate, output_samples = pcm_samples(outputs[0])
            assert rate == 22050
            assert len(spans_for(reports[0], "breath")) == 2
            assert_map_tiles_output(reports[0], output_samples)
            _, source_samples = pcm_samples(env.root.read_bytes(source.file_path))
            rebuilt = assert_source_spans_rebuild(reports[0], source_samples, output_samples)
            assert len(rebuilt) == 3
    finally:
        env.runner.close()


def _walk_key_values(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value
            yield from _walk_key_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_key_values(value)
