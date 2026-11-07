"""Service runner contract tests. No adapter implementation or hardware is used."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime

import pytest

from orchestrator.domain.base import ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    PostProfile,
    PostProfileEncode,
    PostProfileOverlay,
    Project,
    Review,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.domain.state import TransitionContext, check_transition
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.service.dependencies import revise_shot
from orchestrator.store.json_store import ProjectStore


def _advance_shot(store: ProjectStore, shot: Shot, target: ShotStatus) -> Shot:
    """Seed an integration fixture at a legal pipeline stage through the store API."""
    path = {
        ShotStatus.DRAFT: ShotStatus.PLANNED,
        ShotStatus.PLANNED: ShotStatus.BLOCKING,
        ShotStatus.BLOCKING: ShotStatus.BUILDING,
        ShotStatus.BUILDING: ShotStatus.REVIEW,
    }
    while shot.status is not target:
        next_status = path.get(shot.status)
        if next_status is None:
            raise AssertionError(f"cannot fixture-advance Shot from {shot.status} to {target}")
        check_transition(
            shot.status,
            next_status,
            TransitionContext(framing_status=shot.framing_status),
        )
        _head, token = store.load_with_version(
            Shot, shot.shot_id, episode_id=shot.episode_id,
        )
        store.update(shot.model_copy(update={"status": next_status}), expected_version=token)
        shot = store.load(Shot, shot.shot_id, episode_id=shot.episode_id)
    return shot


def _shot(
    store: ProjectStore,
    shot_id: str = "s1",
    *,
    status: ShotStatus = ShotStatus.BLOCKING,
) -> Shot:
    store.save(Project(project_id="p1", title="Test project"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1,
                       duration_target_sec=30), expected_version=0)
    store.create(CameraProfile(
        camera_id="dvr-2005", profile_name="Fixture DVR camera",
    ))
    shot = Shot(shot_id=shot_id, episode_id="ep1", duration_sec=5)
    store.create(shot)
    return _advance_shot(store, shot, status)


def _seed_degrade_inputs(store: ProjectStore, shot: Shot) -> None:
    """Supply composition's project records without involving a real adapter."""
    root = store._root
    font_relative = "assets/fonts/fixture-overlay.ttf"
    root.write_bytes(font_relative, b"fixture overlay font")
    store.save(
        PostProfile(
            name="dvr-2005",
            semver="1.0.0",
            output_fps=12,
            storage_resolution=(640, 480),
            sample_aspect_ratio="1/1",
            overlay=PostProfileOverlay(
                camera_id="CAM 03",
                clock_epoch=1131984000,
                fontsize=16,
                font_relative=font_relative,
            ),
            encode=PostProfileEncode(
                codec="libx264", preset="medium", crf=30,
                pix_fmt="yuv420p", threads=1,
            ),
            provenance="service integration fixture",
        ),
        expected_version=0,
    )
    master_relative = f"renders/{shot.shot_id}/v{shot.version:04d}/seed/master.mkv"
    master_bytes = b"fixture current master"
    root.write_bytes(master_relative, master_bytes)
    store.append_shot_output(
        ShotOutput(
            output_id="seed-master",
            episode_id=shot.episode_id,
            shot_id=shot.shot_id,
            record_version=shot.version,
            kind=ShotOutputKind.MASTER,
            relative_path=master_relative,
            content_sha256=hashlib.sha256(master_bytes).hexdigest(),
            engine_id="BLENDER_EEVEE",
            engine_build="fixture-build",
        ),
        expected_version=0,
    )


def _revision(store: ProjectStore, before: Shot) -> Shot:
    return before.model_copy(update={
        **before.next_version(),
        "narrative_beat": "revised",
        "status": ShotStatus.DRAFT,
    })


def _drain_with_persisted_failure(env) -> None:
    try:
        env.runner.drain()
    except JobError:
        # The persisted job is the public async outcome; drain may also surface the
        # same closed taxonomy error to its synchronous test caller.
        pass


def _contains_key(node, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_contains_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_contains_key(value, key) for value in node)
    return False


def test_submit_persists_queued_job_and_passes_payload_through_untouched(env) -> None:
    _shot(env.store)
    payload = {"adapter_specific": [1, "keep", {"nested": True}]}
    job = env.runner.submit("p1", env.request(payload=payload, required_vram_gb=1.25,
                                                required_ram_gb=2.5, required_disk_gb=3.75))

    assert job.state == "queued"
    assert not env.adapter.started
    assert env.store.load(Shot, "s1", episode_id="ep1").version == 1

    env.adapter.release.set()
    env.runner.drain()
    assert env.adapter.started
    assert env.adapter.requests[0].payload is payload
    assert job.required_vram_gb == 1.25
    assert job.required_ram_gb == 2.5
    assert job.required_disk_gb == 3.75


def test_completed_job_appends_output_bound_to_submitted_content_version(env) -> None:
    _shot(env.store)
    env.adapter.release.set()
    job = env.runner.submit("p1", env.request())
    env.runner.drain()

    outputs = env.store.list_shot_outputs("s1", episode_id="ep1")
    assert len(outputs) == 1
    output = outputs[0]
    assert output.record_version == 1
    assert isinstance(output.kind, ShotOutputKind)
    assert env.root.resolve(output.relative_path).is_file()
    artifact = env.root.resolve(output.relative_path).read_bytes()
    assert output.content_sha256 == hashlib.sha256(artifact).hexdigest()
    assert env.persisted_job(job.job_id).state == "completed"


@pytest.mark.parametrize(
    ("kind_name", "expected_output_kind", "takes_gpu_lease"),
    [
        ("scene", ShotOutputKind.SCENE, False),
        ("render", ShotOutputKind.MASTER, True),
        ("generate", ShotOutputKind.STILL, True),
        ("degrade", ShotOutputKind.DEGRADED, False),
    ],
)
def test_kind_projects_to_output_kind_and_zero_vram_does_not_change_lease_routing(
    env, kind_name, expected_output_kind, takes_gpu_lease
) -> None:
    stage = {
        "scene": ShotStatus.PLANNED,
        "degrade": ShotStatus.BUILDING,
    }.get(kind_name, ShotStatus.BLOCKING)
    shot = _shot(env.store, status=stage)
    if kind_name == "degrade":
        _seed_degrade_inputs(env.store, shot)
    env.adapter.release.set()
    job = env.runner.submit(
        "p1", env.request(kind=getattr(env.AdapterKind, kind_name.upper()), required_vram_gb=0.0)
    )
    env.runner.drain()

    output, = env.store.list_shot_outputs(
        "s1", episode_id="ep1", kind=expected_output_kind,
    )
    assert output.kind is expected_output_kind
    assert env.adapter.lease_states_during_invoke == [takes_gpu_lease]
    assert job.required_vram_gb == 0.0
    assert env.persisted_job(job.job_id).state == "completed"


def test_absent_non_applicable_provenance_is_recorded_absent_without_failing_job(env) -> None:
    _shot(env.store)
    env.adapter.release.set()
    job = env.runner.submit("p1", env.request(kind=env.AdapterKind.RENDER))
    env.runner.drain()

    output, = env.store.list_shot_outputs("s1", episode_id="ep1")
    assert env.persisted_job(job.job_id).state == "completed"
    # The adapter provided render provenance. Provenance fields declared by
    # ShotOutput but inapplicable to MASTER remain absent (None).
    assert output.engine_id == "BLENDER_EEVEE"
    assert output.engine_build == "fake-build"
    assert output.profile_semver is None
    assert output.filter_graph_hash is None
    assert output.pinned_binaries is None


def test_missing_still_provenance_is_absent_and_job_still_succeeds(env) -> None:
    _shot(env.store)
    env.adapter.release.set()
    job = env.runner.submit("p1", env.request(kind=env.AdapterKind.GENERATE))
    env.runner.drain()

    output, = env.store.list_shot_outputs("s1", episode_id="ep1")
    assert output.kind is ShotOutputKind.STILL
    assert env.persisted_job(job.job_id).state == "completed"
    assert output.engine_id is None
    assert output.engine_build is None
    assert output.profile_semver is None
    assert output.filter_graph_hash is None
    assert output.pinned_binaries is None


def test_adapter_result_object_attributes_supply_declared_provenance(env) -> None:
    from types import SimpleNamespace

    _shot(env.store, status=ShotStatus.PLANNED)
    env.adapter.release.set()
    env.adapter.result_value = SimpleNamespace(
        engine_id="BLENDER_EEVEE", engine_build="object-build", extra="ignored"
    )
    job = env.runner.submit("p1", env.request(kind=env.AdapterKind.SCENE))
    env.runner.drain()

    output, = env.store.list_shot_outputs("s1", episode_id="ep1")
    assert env.persisted_job(job.job_id).state == "completed"
    assert output.kind is ShotOutputKind.SCENE
    assert output.engine_id == "BLENDER_EEVEE"
    assert output.engine_build == "object-build"
    assert output.profile_semver is None
    assert not hasattr(output, "extra")


# A missing PROJECT is not detectable at this layer and is deliberately not tested here.
# JobRunner is handed an already-resolved ProjectStore, so whether the project exists was
# settled before submit was called; the resolution and its 404 belong to the API/CLI layer
# and are covered by tests/api/test_adapters.py. Making the runner re-validate would be the
# duplicated path check FR-25 forbids. A missing BOUND RECORD *is* this layer's business,
# because the runner loads it, and the next test covers that.


def test_missing_bound_shot_is_rejected_before_job_is_persisted(env, monkeypatch) -> None:
    from orchestrator.store.json_store import RecordNotFound

    _shot(env.store)
    original_load = env.store.load
    original_load_with_version = env.store.load_with_version

    def missing_shot(cls, record_id=None, *, episode_id=None, version=None):
        if cls is Shot and record_id == "s1":
            raise RecordNotFound("missing bound shot")
        return original_load(cls, record_id, episode_id=episode_id, version=version)

    monkeypatch.setattr(env.store, "load", missing_shot)

    def missing_shot_with_token(cls, record_id=None, *, episode_id=None, version=None):
        if cls is Shot and record_id == "s1":
            raise RecordNotFound("missing bound shot")
        return original_load_with_version(cls, record_id, episode_id=episode_id, version=version)

    monkeypatch.setattr(env.store, "load_with_version", missing_shot_with_token)
    with pytest.raises(RecordNotFound):
        env.runner.submit("p1", env.request())
    assert not env.adapter.started
    jobs = env.root.resolve("state/jobs")
    assert not jobs.exists() or not list(jobs.glob("*.json"))


def test_revision_completes_while_adapter_is_blocked_outside_command_lock(env) -> None:
    """The event barrier makes a trivial before/after revision insufficient: drain has
    entered the adapter and cannot finish until the test releases it.
    """
    original = _shot(env.store)
    env.runner.submit("p1", env.request())
    drain_thread = threading.Thread(target=env.runner.drain, daemon=True)
    drain_thread.start()
    assert env.adapter.entered.wait(2), "adapter did not enter its mid-run barrier"
    assert not env.adapter.release.is_set()

    failure: list[BaseException] = []

    def revise() -> None:
        try:
            token = env.store.load_with_version(Shot, "s1", episode_id="ep1")[1]
            revise_shot(env.store, _revision(env.store, original), expected_version=token)
        except BaseException as exc:  # noqa: BLE001 - re-asserted on the main thread
            failure.append(exc)

    revision_thread = threading.Thread(target=revise, daemon=True)
    revision_thread.start()
    revision_thread.join(1)
    completed_during_adapter = not revision_thread.is_alive()
    env.adapter.release.set()
    drain_thread.join(3)
    revision_thread.join(3)

    assert completed_during_adapter, "service revision blocked behind the running adapter"
    assert not failure
    assert not drain_thread.is_alive()
    assert env.store.load(Shot, "s1", episode_id="ep1").version == 2


def test_rebuild_revision_during_run_keeps_artifact_and_does_not_approve_successor(env) -> None:
    original = _shot(env.store)
    env.store.save(Review(
        review_id="review-v1", target_id="s1", target_version=1,
        created_at=datetime(2026, 11, 5, tzinfo=UTC), approval_state=ShotStatus.APPROVED,
    ), expected_version=0)
    env.runner.submit("p1", env.request())
    drain_thread = threading.Thread(target=env.runner.drain, daemon=True)
    drain_thread.start()
    assert env.adapter.entered.wait(2)
    token = env.store.load_with_version(Shot, "s1", episode_id="ep1")[1]
    revise_shot(env.store, _revision(env.store, original), expected_version=token)
    env.adapter.release.set()
    drain_thread.join(3)

    assert not drain_thread.is_alive()
    outputs = env.store.list_shot_outputs("s1", episode_id="ep1")
    assert len(outputs) == 1
    output = outputs[0]
    assert output.record_version == 1
    assert output.record_version != env.store.load(Shot, "s1", episode_id="ep1").version
    assert env.root.resolve(output.relative_path).read_bytes().startswith(b"fake retained artifact")
    # Staleness is derived from the current head; the immutable output receipt stores no flag.
    assert not hasattr(output, "stale")
    ledger = env.root.resolve("episodes/ep1/shots/s1/outputs")
    output_json = [json.loads(path.read_text(encoding="utf-8")) for path in ledger.glob("*.json")]
    assert output_json
    assert all(not _contains_key(row, "stale") for row in output_json)
    current = env.store.load(Shot, "s1", episode_id="ep1")
    assert current.version == 2
    assert current.status is not ShotStatus.APPROVED
    assert current.approval_review_id is None
    assert env.store.load(Review, "review-v1").target_version == 1
    historical = env.store.load(Shot, "s1", episode_id="ep1", version=1)
    assert historical.version == 1


def test_deleted_bound_record_fails_terminally_with_remediation(env, monkeypatch) -> None:
    from orchestrator.store.json_store import RecordNotFound

    _shot(env.store)
    original_load = env.store.load_with_version

    def missing_after_submit(cls, record_id=None, *, episode_id=None, version=None):
        if cls is Shot and record_id == "s1":
            raise RecordNotFound("s1")
        return original_load(cls, record_id, episode_id=episode_id, version=version)

    job = env.runner.submit("p1", env.request())
    monkeypatch.setattr(env.store, "load_with_version", missing_after_submit)
    env.adapter.release.set()
    _drain_with_persisted_failure(env)

    failed = env.persisted_job(job.job_id)
    assert failed.state == "failed"
    assert failed.error_kind is not None
    assert JobErrorKind(failed.error_kind).retryable is False
    assert JobErrorKind(failed.error_kind).remediation


def test_adapter_failure_releases_gpu_lease_and_does_not_attach_partial_result(env) -> None:
    _shot(env.store)
    env.adapter.raise_error = RuntimeError("fake adapter failure")
    job = env.runner.submit("p1", env.request())
    env.adapter.release.set()

    _drain_with_persisted_failure(env)

    shot = env.store.load(Shot, "s1", episode_id="ep1")
    failed = env.persisted_job(job.job_id)
    assert shot.status is not ShotStatus.APPROVED
    assert env.store.list_shot_outputs("s1", episode_id="ep1") == ()
    assert not env.lease._owned
    assert failed.state == "failed"
    assert failed.error_kind == JobErrorKind.INTERNAL.value


def test_preflight_refusal_surfaces_closed_job_error_and_remediation(env) -> None:
    env.host.ram_gb = 0
    _shot(env.store)

    with pytest.raises(JobError) as caught:
        env.runner.submit("p1", env.request(required_ram_gb=1.0))

    assert caught.value.kind is JobErrorKind.OUT_OF_MEMORY
    assert caught.value.retryable is True
    assert caught.value.remediation
    jobs = env.root.resolve("state/jobs")
    assert not jobs.exists() or not list(jobs.glob("*.json"))


def test_cancel_after_submit_before_adapter_start_is_terminal(env) -> None:
    _shot(env.store)
    env.adapter.cancel_before_start = True
    job = env.runner.submit("p1", env.request())

    cancelled = env.runner.cancel("p1", job.job_id)
    env.runner.drain()

    assert cancelled.job_id == job.job_id
    assert not env.adapter.started
    persisted = env.persisted_job(job.job_id)
    assert persisted.state == "cancelled"
    assert persisted.error_kind == JobErrorKind.CANCELLED.value


def test_cancel_after_adapter_starts_is_not_an_error_free_completion(env) -> None:
    _shot(env.store)
    job = env.runner.submit("p1", env.request())
    drain_thread = threading.Thread(target=env.runner.drain, daemon=True)
    drain_thread.start()
    assert env.adapter.entered.wait(2)
    cancel_result: list[object] = []
    cancel_thread = threading.Thread(
        target=lambda: cancel_result.append(env.runner.cancel("p1", job.job_id)), daemon=True
    )
    cancel_thread.start()
    cancel_thread.join(1)
    cancel_completed_while_adapter_blocked = not cancel_thread.is_alive()
    env.adapter.release.set()
    drain_thread.join(3)
    cancel_thread.join(3)

    assert not drain_thread.is_alive()
    assert cancel_completed_while_adapter_blocked, "cancellation blocked behind the running adapter"
    assert cancel_result
    persisted = env.persisted_job(job.job_id)
    assert persisted.state == "cancelled"
    assert persisted.error_kind == JobErrorKind.CANCELLED.value


def test_two_submissions_do_not_overlap_or_hold_gpu_lease_twice(env) -> None:
    _shot(env.store)
    env.runner.submit("p1", env.request())
    env.runner.submit("p1", env.request())
    first_drain = threading.Thread(target=env.runner.drain, daemon=True)
    first_drain.start()
    assert env.adapter.entered.wait(2), "first adapter did not start"
    second_drain = threading.Thread(target=env.runner.drain, daemon=True)
    second_drain.start()
    second_started_while_first_blocked = env.adapter.second_entered.wait(0.2)
    env.adapter.release.set()
    first_drain.join(3)
    second_drain.join(3)

    assert not first_drain.is_alive()
    assert not second_drain.is_alive()
    assert not second_started_while_first_blocked, "second adapter overlapped the first GPU job"
    assert env.adapter.max_active == 1
    assert env.adapter.lease_states_during_invoke == [True, True]
    outputs = env.store.list_shot_outputs("s1", episode_id="ep1")
    assert len(outputs) == 2
    assert {output.record_version for output in outputs} == {1}
    assert len({output.kind for output in outputs}) == 1
    assert len({output.output_id for output in outputs}) == 2
    assert len({output.relative_path for output in outputs}) == 2
    assert all(env.root.resolve(output.relative_path).is_file() for output in outputs)
    env.runner.close()
    assert env.registry.closed
    assert not env.lease._owned


def test_recording_result_tolerates_pending_invalidation_cascade(env) -> None:
    _shot(env.store)
    # Durable journal setup uses the store API; the intentionally minimal plan represents
    # an already-persisted pending cascade whose recovery may have run before recording.
    before = env.store.load(Shot, "s1", episode_id="ep1")
    after = _revision(env.store, before)
    env.store.record_cascade({
        "cascade_id": "a" * 32,
        "entity_type": "Shot",
        "before": before.model_dump(mode="json"),
        "after": after.model_dump(mode="json"),
        "expected_version": 1,
        "episode_ids": ["ep1"],
        "affected": [{"episode_id": "ep1", "shot_id": "s1", "version": 1}],
    })
    env.runner.submit("p1", env.request())
    env.adapter.release.set()

    env.runner.drain()

    assert env.store.cascades(pending_only=True)
    outputs = env.store.list_shot_outputs("s1", episode_id="ep1")
    assert len(outputs) == 1
    assert outputs[0].record_version == 1


