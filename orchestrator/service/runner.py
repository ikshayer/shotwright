"""Asynchronous adapter dispatch with append-only, version-bound outputs."""

from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any, Protocol

from orchestrator.adapters.blender.runner import RenderRequest
from orchestrator.adapters.comfyui.workflow import ImageRequest
from orchestrator.adapters.postprofile.profile import PostProfileSpec
from orchestrator.adapters.postprofile.runner import DegradeRequest
from orchestrator.domain.base import Invalidation, ShotStatus
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    EpisodeOutput,
    Job,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.domain.invalidation import field_invalidation
from orchestrator.domain.state import TransitionContext, TransitionError, check_transition
from orchestrator.jobs.journal import append_job, read_job
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind, failure_diagnostics
from orchestrator.platform.settings import Settings
from orchestrator.service.compose import camera_profile_for_render, compose_degrade_request
from orchestrator.store.json_store import ProjectStore, RecordNotFound, VersionConflict


class AdapterKind(StrEnum):
    SCENE = "scene"
    RENDER = "render"
    GENERATE = "generate"
    DEGRADE = "degrade"
    H3_CANDIDATE = "h3_candidate"
    TTS_PIPER = "tts_piper"
    TTS_INDEXTTS2 = "tts_indextts2"
    AUDIO_POST = "audio_post"
    PERFORMANCE_EDIT = "performance_edit"
    ASSEMBLE = "cut_assembly"
    AUDIO_REVIEW = "qwen_audio_review"


#: Kinds that never take the GPU lease: they pass CPU admission and run outside the GPU lock.
_CPU_KINDS = frozenset({
    AdapterKind.SCENE, AdapterKind.DEGRADE, AdapterKind.TTS_PIPER, AdapterKind.AUDIO_POST,
    AdapterKind.PERFORMANCE_EDIT, AdapterKind.ASSEMBLE,
})
#: Kinds that run under the GPU lease, the budget ledger and the watchdog.
_GPU_KINDS = frozenset({
    AdapterKind.RENDER, AdapterKind.GENERATE, AdapterKind.H3_CANDIDATE,
    AdapterKind.TTS_INDEXTTS2, AdapterKind.AUDIO_REVIEW,
})
#: Kinds whose output is a candidate/take beside the Shot: payload `{"request": ..., "expected_version": ...}`,
#: a `run_id` of their own, and no change to the Shot, its status or its ShotOutputs.
_SIDECAR_KINDS = frozenset({
    AdapterKind.H3_CANDIDATE, AdapterKind.TTS_PIPER, AdapterKind.TTS_INDEXTTS2, AdapterKind.AUDIO_POST,
    AdapterKind.PERFORMANCE_EDIT,
})
#: Kinds whose success registers a take Asset, so a cancel must resolve to exactly one outcome.
_TAKE_KINDS = frozenset({
    AdapterKind.TTS_PIPER, AdapterKind.TTS_INDEXTTS2, AdapterKind.AUDIO_POST,
    AdapterKind.PERFORMANCE_EDIT,
})


@dataclass(frozen=True)
class AdapterRequest:
    kind: AdapterKind
    payload: Any
    record_id: str
    record_version: int
    episode_id: str | None = None
    required_vram_gb: float = 0.0
    required_ram_gb: float = 0.0
    required_disk_gb: float = 0.0


@dataclass(frozen=True)
class AdapterResult:
    artifact_path: str
    value: Any = None


class Adapter(Protocol):
    def invoke(self, request: AdapterRequest, job: Job, *, queue: JobQueue) -> AdapterResult: ...
    def close(self) -> None: ...


class AdapterRegistry:
    def __init__(self, adapters: Mapping[AdapterKind, Adapter] | None = None) -> None:
        self._adapters = dict(adapters or {})

    def register(self, kind: AdapterKind, adapter: Adapter) -> None:
        if kind in self._adapters:
            raise ValueError(f"adapter already registered for {kind.value}")
        self._adapters[kind] = adapter

    def get(self, kind: AdapterKind) -> Adapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            raise KeyError(f"no adapter registered for {kind.value}") from exc

    def close(self) -> None:
        first: Exception | None = None
        for adapter in self._adapters.values():
            try:
                adapter.close()
            except Exception as exc:  # noqa: BLE001 - close every adapter, then report first failure.
                first = first or exc
        if first is not None:
            raise first


class _AdapterShim:
    def __init__(self, kind: AdapterKind, adapter: Any, settings: Settings) -> None:
        self.kind, self.adapter, self.settings = kind, adapter, settings
        self.cancel_check: Callable[[], bool] | None = None

    def invoke(self, request: AdapterRequest, job: Job, *, queue: JobQueue) -> AdapterResult:
        payload = request.payload
        if self.kind is AdapterKind.H3_CANDIDATE:
            from orchestrator.adapters.minimax_h3 import H3Request
            model = H3Request.model_validate(payload.get("request") if isinstance(payload, dict) else payload)
            result = self.adapter.generate_managed(
                model, job, queue=queue,
                cancel=self.cancel_check or _cancel_callback(request.payload),
            )
            return AdapterResult(artifact_path=result.av_path, value=result)
        if self.kind in _TAKE_KINDS:
            return self.adapter.invoke(request, job, queue=queue, cancel=self.cancel_check)
        if self.kind is AdapterKind.ASSEMBLE:
            receipt = self.adapter.invoke(
                request,
                job,
                queue=queue,
                cancel=self.cancel_check or _cancel_callback(request.payload),
            )
            if not isinstance(receipt, EpisodeOutput):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                               "cut assembly adapter returned no EpisodeOutput receipt")
            return AdapterResult(artifact_path=receipt.relative_path, value=receipt)
        if self.kind is AdapterKind.AUDIO_REVIEW:
            return self.adapter.invoke(
                request, job, queue=queue,
                cancel=self.cancel_check or _cancel_callback(request.payload),
            )
        if isinstance(payload, dict):
            payload = payload.get("request", payload)
        if self.kind is AdapterKind.SCENE:
            model = RenderRequest.model_validate(payload)
            result = self.adapter.assemble_scene(model, job)
            artifact = result.scene_path
        elif self.kind is AdapterKind.RENDER:
            model = RenderRequest.model_validate(payload)
            render_managed = getattr(self.adapter, "render_managed", None)
            render = render_managed if callable(render_managed) else self.adapter.render
            result = render(model, job, queue=queue,
                            cancel=self.cancel_check or _cancel_callback(request.payload))
            if model.mode == "master":
                artifact = result.master_path
                if not artifact:
                    raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                                   "master render returned no assembled master file",
                                   job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
            else:
                artifact = result.frame_paths[0] if result.frame_paths else result.blend_path
        elif self.kind is AdapterKind.GENERATE:
            result = self.adapter.generate(
                ImageRequest.model_validate(payload), job, queue=queue,
                cancel=self.cancel_check or _cancel_callback(request.payload),
            )
            if not result.image_paths:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "ComfyUI returned no image outputs")
            artifact = result.image_paths[0]
        else:
            degradation = DegradeRequest.model_validate(payload)
            mapped = postprofile_spec_from_camera(degradation.camera_profile, degradation.profile)
            degradation = degradation.model_copy(update={"profile": mapped})
            result = self.adapter.degrade(degradation)
            artifact = result.output_path
        return AdapterResult(artifact_path=artifact, value=result)

    def close(self) -> None:
        close = getattr(self.adapter, "close", None)
        if callable(close):
            close()


def _cancel_callback(payload: Any) -> Callable[[], bool] | None:
    # The contract's request is frozen and deliberately contains no runner state.
    return payload.get("cancel_check") if isinstance(payload, dict) else None


class JobRunner:
    """Persist immediately, run one adapter at a time, and record without rebinding."""

    def __init__(self, store_for: Callable[[str], ProjectStore], settings: Settings,
                 *, queue_for: Callable[[str], JobQueue], adapters: AdapterRegistry,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store_for, self.settings = store_for, settings
        self.queue_for, self.adapters, self.clock = queue_for, adapters, clock
        self._condition = threading.Condition()
        self._pending: deque[tuple[str, AdapterRequest, Job]] = deque()
        self._cancelled: set[tuple[str, str]] = set()
        #: Take jobs (speech, audio post) whose result is being registered; a cancel no longer applies to them.
        self._finalizing: set[tuple[str, str]] = set()
        self._active: set[tuple[str, str]] = set()
        self._project_locks: dict[str, threading.Lock] = {}
        self._closed = False
        self._gpu_lock = threading.Lock()
        self._worker = threading.Thread(target=self._dispatch, name="adapter-job-runner", daemon=True)
        self._worker.start()

    def submit(self, project_id: str, request: AdapterRequest) -> Job:
        if self._closed:
            raise RuntimeError("job runner is closed")
        request = AdapterRequest(
            kind=AdapterKind(request.kind), payload=request.payload,
            record_id=request.record_id, record_version=request.record_version,
            episode_id=request.episode_id, required_vram_gb=request.required_vram_gb,
            required_ram_gb=request.required_ram_gb, required_disk_gb=request.required_disk_gb,
        )
        store = self.store_for(project_id)
        if request.kind is AdapterKind.ASSEMBLE:
            episode_id = request.episode_id or request.record_id
            if request.record_id != episode_id:
                raise ValueError("assembly record_id must match its episode_id")
            request = AdapterRequest(
                kind=request.kind,
                payload=request.payload,
                record_id=episode_id,
                record_version=request.record_version,
                episode_id=episode_id,
                required_vram_gb=request.required_vram_gb,
                required_ram_gb=request.required_ram_gb,
                required_disk_gb=request.required_disk_gb,
            )
        if request.kind is AdapterKind.AUDIO_REVIEW:
            payload = request.payload
            if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
                raise ValueError("audio review request must contain a validated snapshot")
            audio_review_id = payload.get("audio_review_id")
            if (not isinstance(audio_review_id, str) or not audio_review_id
                    or request.record_id != audio_review_id or request.record_version != 0):
                raise ValueError("audio review record_id/run_id token must match its new audio_review_id")
            target = payload["snapshot"].get("target", {})
            expected_episode = target.get("episode_id") if target.get("kind") == "output" else None
            if request.episode_id != expected_episode:
                raise ValueError("audio review episode_id must match an output target, or be absent for an Asset")
        if request.kind is AdapterKind.DEGRADE:
            raw_payload = _payload_body(request.payload)
            raw_shot_id = _payload_value(raw_payload, "shot_id")
            raw_version = _payload_value(raw_payload, "shot_version")
            if raw_shot_id is not None and raw_shot_id != request.record_id:
                raise ValueError("adapter payload shot_id must match AdapterRequest.record_id")
            if raw_version is not None and raw_version != request.record_version:
                raise ValueError("adapter payload shot_version must match AdapterRequest.record_version")
            try:
                bound_shot = store.load(Shot, request.record_id, episode_id=request.episode_id)
            except RecordNotFound as exc:
                raise RecordNotFound(f"bound Shot {request.record_id!r} does not exist") from exc
            episode_id = bound_shot.episode_id
            degradation = compose_degrade_request(
                store,
                shot_id=request.record_id,
                episode_id=episode_id,
                run_id=uuid.uuid4().hex,
            )
            if request.record_version != degradation.shot_version:
                raise ValueError(
                    f"degrade request must target current Shot version {degradation.shot_version}; "
                    f"got {request.record_version}"
                )
            request = AdapterRequest(
                kind=request.kind,
                payload=degradation,
                record_id=request.record_id,
                record_version=degradation.shot_version,
                episode_id=episode_id,
                required_vram_gb=request.required_vram_gb,
                required_ram_gb=request.required_ram_gb,
                required_disk_gb=request.required_disk_gb,
            )
        with store.command():
            if request.kind is AdapterKind.AUDIO_REVIEW:
                payload = request.payload
                audio_review_id = payload["audio_review_id"]
                job = Job(
                    job_id=uuid.uuid4().hex, type=request.kind.value,
                    required_vram_gb=request.required_vram_gb,
                    required_ram_gb=request.required_ram_gb,
                    required_disk_gb=request.required_disk_gb,
                    shot_id=None, episode_id=request.episode_id,
                    run_id=audio_review_id,
                )
            elif request.kind is AdapterKind.ASSEMBLE:
                # AdapterRequest.record_id identifies the Episode and record_version binds
                # its current mutation token; this kind has no Shot binding.
                episode_id = request.record_id
                try:
                    episode, current_token = store.load_with_version(Episode, episode_id)
                except RecordNotFound as exc:
                    raise RecordNotFound(f"bound Episode {episode_id!r} does not exist") from exc
                if request.record_version != current_token:
                    raise VersionConflict(
                        f"episodes/{episode_id}/episode.json",
                        request.record_version,
                        current_token,
                        episode,
                    )
                job = Job(
                    job_id=uuid.uuid4().hex, type=request.kind.value,
                    required_vram_gb=request.required_vram_gb,
                    required_ram_gb=request.required_ram_gb,
                    required_disk_gb=request.required_disk_gb,
                    shot_id=None, episode_id=episode_id,
                    run_id=_job_run_id(request),
                )
            else:
                try:
                    shot = store.load(Shot, request.record_id, episode_id=request.episode_id)
                except RecordNotFound as exc:
                    raise RecordNotFound(f"bound Shot {request.record_id!r} does not exist") from exc
                episode_id = shot.episode_id
                if request.kind in _SIDECAR_KINDS:
                    expected = request.payload.get("expected_version") if isinstance(request.payload, dict) else None
                    _, current_token = store.load_with_version(Shot, request.record_id, episode_id=episode_id)
                    if type(expected) is not int or expected != current_token:
                        raise VersionConflict(f"episodes/{episode_id}/shots/{request.record_id}",
                                              expected if type(expected) is int else -1,
                                              current_token, shot)
                adapter_payload = _payload_body(request.payload)
                payload_shot_id = _payload_value(adapter_payload, "shot_id")
                payload_version = _payload_value(adapter_payload, "shot_version")
                if payload_shot_id is not None and payload_shot_id != request.record_id:
                    raise ValueError("adapter payload shot_id must match AdapterRequest.record_id")
                if payload_version is not None and payload_version != request.record_version:
                    raise ValueError("adapter payload shot_version must match AdapterRequest.record_version")
                if not 1 <= request.record_version <= shot.version:
                    raise RecordNotFound(
                        f"Shot {request.record_id!r} has no content version {request.record_version}"
                    )
                # Historical content must exist, independently of the mutation token.
                store.load(Shot, request.record_id, episode_id=episode_id,
                           version=request.record_version)
                job = Job(
                    job_id=uuid.uuid4().hex, type=request.kind.value,
                    required_vram_gb=request.required_vram_gb,
                    required_ram_gb=request.required_ram_gb,
                    required_disk_gb=request.required_disk_gb,
                    shot_id=request.record_id, episode_id=episode_id,
                    run_id=_job_run_id(request),
                )
            # Admission refusal is part of submit's synchronous contract. The id is
            # not persisted or returned until preflight succeeds.
            queue = _kind_queue(self.queue_for, project_id, request.kind)
            preflight = queue.preflight_cpu if request.kind in _CPU_KINDS else queue.preflight
            preflight(job)
            root = store._root
            root.make_dir("state/jobs")
            append_job(root, job)
        with self._condition:
            self._pending.append((project_id, request, job))
            self._condition.notify_all()
        return job

    def cancel(self, project_id: str, job_id: str) -> Job:
        store = self.store_for(project_id)
        root = store._root
        wait_for_assembly_stop = False
        # The record is read under the same lock a speech job holds while it decides between
        # "completed with an Asset" and "cancelled", so a cancel either lands first (and the
        # job is cancelled) or finds the job already completed (and changes nothing).
        with self._condition:
            record = read_job(root, job_id)
            if record is None:
                raise RecordNotFound(f"job {job_id!r} does not exist")
            job = Job.model_validate(record)
            if (project_id, job_id) in self._finalizing:
                # Its result is being registered; the cancel arrived too late to stop it.
                return job
            if job.state not in {"completed", "failed", "cancelled"}:
                self._cancelled.add((project_id, job_id))
                if job.type == AdapterKind.ASSEMBLE.value and (project_id, job_id) in self._active:
                    # This worker owns a supervised FFmpeg child. Leave the journal
                    # nonterminal until the adapter's watcher has stopped and joined it.
                    wait_for_assembly_stop = True
                else:
                    job.state = "cancelled"
                    job.error_kind = JobErrorKind.CANCELLED.value
                    append_job(root, job)
                self._condition.notify_all()
        if wait_for_assembly_stop:
            with self._condition:
                while (project_id, job_id) in self._active:
                    self._condition.wait()
            record = read_job(root, job_id)
            if record is None:
                raise RecordNotFound(f"job {job_id!r} does not exist")
            return Job.model_validate(record)
        return job

    def drain(self) -> None:
        with self._condition:
            while self._pending or self._active:
                self._condition.wait()

    def wait(self, project_id: str, job_id: str) -> Job:
        """Wait for one submitted job and read its final project-local record."""
        key = (project_id, job_id)
        with self._condition:
            while (key in self._active
                   or any(queued_project == project_id and queued_job.job_id == job_id
                          for queued_project, _, queued_job in self._pending)):
                self._condition.wait()
        store = self.store_for(project_id)
        record = read_job(store._root, job_id)
        if record is None:
            raise RecordNotFound(f"job {job_id!r} does not exist")
        return Job.model_validate(record)

    def close(self) -> None:
        self.drain()
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        self._worker.join()
        self.adapters.close()

    def _dispatch(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed and not self._pending:
                    return
                project_id, request, job = self._pending.popleft()
                key = (project_id, job.job_id)
                if key in self._cancelled:
                    if request.kind is AdapterKind.AUDIO_REVIEW:
                        self._persist_audio_review_terminal(
                            self.store_for(project_id), request, job,
                            JobError(JobErrorKind.CANCELLED, "audio review cancelled before adapter start"),
                        )
                    self._condition.notify_all()
                    continue
                self._active.add(key)
            threading.Thread(target=self._execute, args=(project_id, request, job, key),
                             name=f"adapter-{job.job_id}", daemon=True).start()

    def _execute(self, project_id: str, request: AdapterRequest, job: Job,
                 key: tuple[str, str]) -> None:
        with self._condition:
            project_lock = self._project_locks.setdefault(project_id, threading.Lock())
        with project_lock:
            self._execute_job(project_id, request, job, key)

    def _execute_job(self, project_id: str, request: AdapterRequest, job: Job,
                     key: tuple[str, str]) -> None:
        store = self.store_for(project_id)
        root = store._root
        try:
            queue = _kind_queue(self.queue_for, project_id, request.kind)
            adapter = self.adapters.get(request.kind)
            set_cancel_check = getattr(adapter, "set_cancel_check", None)

            def invoke_if_not_cancelled(active: Job) -> AdapterResult:
                if self._is_cancelled(key):
                    raise _job_error(
                        JobErrorKind.CANCELLED, "job cancelled before adapter start", active
                    )
                try:
                    if (request.kind not in _SIDECAR_KINDS
                            and request.kind not in {AdapterKind.ASSEMBLE, AdapterKind.AUDIO_REVIEW}):
                        _begin_shot_work(store, request, active)
                    if callable(set_cancel_check):
                        # Installed here, on the thread that is about to call the adapter
                        # (not earlier, while this job may still wait for the GPU lock), so a
                        # concurrent job on the same adapter cannot replace or clear it.
                        set_cancel_check(lambda: self._is_cancelled(key))
                    value = adapter.invoke(request, active, queue=queue)
                    if request.kind in _TAKE_KINDS:
                        # Decided before the queue or the CPU path journals "completed": a
                        # cancel already recorded wins (the take is then discarded); otherwise
                        # the job is finalizing and a later cancel leaves it to complete. Done
                        # any later, a cancel could land between the "completed" entry and
                        # the decision, and the job would read completed with no take.
                        with self._condition:
                            if key in self._cancelled:
                                raise _job_error(JobErrorKind.CANCELLED,
                                                 "job cancelled before its take was registered", active)
                            # Only while the job is still the runner's: a watchdog may have
                            # retired it already, and its cleanup would never discard the key.
                            if key in self._active:
                                self._finalizing.add(key)
                    return value
                except Exception as exc:
                    raise JobError.from_exception(
                        exc, job_id=active.job_id, shot_id=active.shot_id,
                        run_id=active.run_id, origin="adapter",
                    ) from exc

            if self._is_cancelled(key):
                raise _job_error(JobErrorKind.CANCELLED, "job cancelled before adapter start", job)
            if request.kind in _GPU_KINDS:
                with self._gpu_lock:
                    value = queue.run(
                        job, invoke_if_not_cancelled,
                        startup_allowance_seconds=self._startup_allowance(request.kind),
                    )
            else:
                queue._transition(job, "queued")
                preflight = queue.preflight_cpu if request.kind in _CPU_KINDS else queue.preflight
                preflight(job)
                if self._is_cancelled(key):
                    raise _job_error(JobErrorKind.CANCELLED, "job cancelled before adapter start", job)
                job.attempts += 1
                queue._persist(job)
                queue._transition(job, "running")
                value = invoke_if_not_cancelled(job)
                queue._transition(job, "completed")
            if self._is_cancelled(key):
                raise _job_error(JobErrorKind.CANCELLED, "job cancelled while adapter work was active", job)
            result = value if isinstance(value, AdapterResult) else AdapterResult(
                artifact_path=_artifact_path(request.kind, value, request.payload), value=value
            )
            if request.kind is AdapterKind.H3_CANDIDATE:
                from orchestrator.service.minimax_h3 import persist_h3_candidate
                persist_h3_candidate(store, job, result.value, request.payload["request"])
            elif request.kind in _TAKE_KINDS:
                persist_success = _take_persistence(request.kind)[0]
                # The keep-or-cancel decision was made when the adapter returned (see
                # invoke_if_not_cancelled); the job is finalizing, so persistence runs without
                # the runner lock and a cancel arriving now leaves it to complete.
                try:
                    persist_success(store, job, result.value, request.payload["request"])
                finally:
                    with self._condition:
                        self._finalizing.discard(key)
            elif request.kind is not AdapterKind.AUDIO_REVIEW:
                self._record_result(store, root, request, job, result)
        except JobError as exc:
            if request.kind is AdapterKind.AUDIO_REVIEW:
                self._persist_audio_review_terminal(store, request, job, exc)
            if request.kind is AdapterKind.H3_CANDIDATE:
                try:
                    from orchestrator.service.minimax_h3 import persist_h3_terminal
                    persist_h3_terminal(store, job, request.payload["request"],
                                        status="cancelled" if exc.kind is JobErrorKind.CANCELLED else "failed")
                except Exception as receipt_error:  # noqa: BLE001 - a receipt failure annotates the primary job error.
                    exc.add_note(f"H3 terminal GenerationRun persistence failed: {type(receipt_error).__name__}: {receipt_error}")
            self._persist_take_terminal(
                store, job, request,
                status="cancelled" if exc.kind is JobErrorKind.CANCELLED else "failed", error=exc,
            )
            if (exc.kind is not JobErrorKind.CANCELLED
                    and request.kind not in _SIDECAR_KINDS
                    and request.kind not in {AdapterKind.ASSEMBLE, AdapterKind.AUDIO_REVIEW}):
                lifecycle_error = _fail_shot_after_job_error(store, request)
                if lifecycle_error is not None:
                    exc.message = (
                        f"{exc.message}; also failed to move the Shot from building to "
                        f"failed: {lifecycle_error}"
                    )
                    exc.args = (exc.message,)
                    exc.__cause__ = lifecycle_error
            if job.state not in {"failed", "cancelled"}:
                try:
                    queue = _kind_queue(self.queue_for, project_id, request.kind)
                    queue._transition(job, "cancelled" if exc.kind is JobErrorKind.CANCELLED else "failed", error=exc)
                except Exception as transition_error:  # noqa: BLE001 - persist a classified transition failure.
                    _persist_job(root, job, JobError.from_exception(
                        transition_error, job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id,
                        origin="runner",
                    ), self.settings)
        except Exception as exc:  # noqa: BLE001 - adapter failures are classified at the job boundary.
            error = JobError.from_exception(
                exc, job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id, origin="runner"
            )
            if request.kind is AdapterKind.AUDIO_REVIEW:
                self._persist_audio_review_terminal(store, request, job, error)
            if request.kind is AdapterKind.H3_CANDIDATE:
                try:
                    from orchestrator.service.minimax_h3 import persist_h3_terminal
                    persist_h3_terminal(store, job, request.payload["request"], status="failed")
                except Exception as receipt_error:  # noqa: BLE001 - a receipt failure annotates the primary job error.
                    exc.add_note(f"H3 terminal GenerationRun persistence failed: {type(receipt_error).__name__}: {receipt_error}")
            self._persist_take_terminal(store, job, request, status="failed", error=exc)
            lifecycle_error = (None if request.kind in _SIDECAR_KINDS
                               or request.kind in {AdapterKind.ASSEMBLE, AdapterKind.AUDIO_REVIEW}
                               else _fail_shot_after_job_error(store, request))
            if lifecycle_error is not None:
                error.message = (
                    f"{error.message}; also failed to move the Shot from building to "
                    f"failed: {lifecycle_error}"
                )
                error.args = (error.message,)
                error.__cause__ = lifecycle_error
            job.state, job.error_kind = "failed", error.kind.value
            _persist_job(root, job, error, self.settings)
        finally:
            with self._condition:
                self._active.discard(key)
                self._cancelled.discard(key)
                self._finalizing.discard(key)
                self._condition.notify_all()

    def _startup_allowance(self, kind: AdapterKind) -> float:
        if kind is AdapterKind.TTS_INDEXTTS2:
            return self.settings.indextts2.startup_allowance_seconds
        if kind is AdapterKind.AUDIO_REVIEW:
            return self.settings.qwen_review.startup_allowance_seconds
        return self.settings.resources.startup_allowance_seconds

    @staticmethod
    def _persist_audio_review_terminal(
        store: ProjectStore, request: AdapterRequest, job: Job, error: JobError,
    ) -> None:
        """Ensure a pre-helper failure still has a project-visible immutable report."""
        try:
            from orchestrator.service.audio_review import persist_minimal_audio_review_if_missing

            persist_minimal_audio_review_if_missing(
                store,
                audio_review_id=job.run_id or request.record_id,
                job_id=job.job_id,
                snapshot=request.payload["snapshot"],
                status="cancelled" if error.kind is JobErrorKind.CANCELLED else "failed",
                errors=[{
                    "family": "job", "kind": error.kind.value, "message": error.message,
                    "retryable": error.retryable, "remediation": error.remediation,
                }],
            )
        except Exception as report_error:  # noqa: BLE001 - keep the primary failure and annotate the record.
            message = (
                f"audio review terminal report persistence failed: "
                f"{type(report_error).__name__}: {report_error}"
            )
            error.message = f"{error.message}; {message}"
            error.args = (error.message,)
            if job.state in {"failed", "cancelled"}:
                job.error_kind = error.kind.value
                job.error_message = error.message
                append_job(store._root, job)

    @staticmethod
    def _persist_take_terminal(store: ProjectStore, job: Job, request: AdapterRequest, *,
                               status: str, error: BaseException) -> None:
        if request.kind not in _TAKE_KINDS:
            return
        try:
            persist_terminal = _take_persistence(request.kind)[1]
            persist_terminal(store, job, request.payload["request"], status=status)
        except Exception as receipt_error:  # noqa: BLE001 - a receipt failure annotates the primary job error.
            error.add_note(f"take terminal GenerationRun persistence failed: "
                           f"{type(receipt_error).__name__}: {receipt_error}")

    def _record_result(self, store: ProjectStore, root: ProjectRoot, request: AdapterRequest,
                       job: Job, result: AdapterResult) -> None:
        # Artifact is project-relative and resolved by the sole containment authority.
        artifact = root.resolve(result.artifact_path)
        if not artifact.is_file():
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"adapter output is missing: {result.artifact_path}",
                           job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
        if request.kind is AdapterKind.ASSEMBLE:
            receipt = result.value
            if (not isinstance(receipt, EpisodeOutput)
                    or receipt.episode_id != request.episode_id
                    or receipt.relative_path != result.artifact_path):
                raise _job_error(
                    JobErrorKind.CORRUPT_ARTIFACT,
                    "cut assembly result does not match its EpisodeOutput receipt",
                    job,
                )
            outputs = store.list_episode_outputs(request.episode_id or "")
            if not any(output.output_id == receipt.output_id for output in outputs):
                raise _job_error(
                    JobErrorKind.CORRUPT_ARTIFACT,
                    "cut assembly receipt was not persisted to the Episode output ledger",
                    job,
                )
            return
        hasher = hashlib.sha256()
        with artifact.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        digest = hasher.hexdigest()
        with store.command():
            try:
                shot, token = store.load_with_version(
                    Shot, request.record_id, episode_id=request.episode_id,
                )
            except RecordNotFound as exc:
                raise _job_error(JobErrorKind.INVALID_MANIFEST,
                                 "bound Shot was deleted before recording; restore the Shot history", job,
                                 cause=exc) from exc
            kind = _output_kind(request.kind, request.payload)
            provenance = _provenance(kind, result.value)
            if kind is ShotOutputKind.DEGRADED:
                frame_count = provenance.get("frame_count")
                output_fps = provenance.get("output_fps")
                if (
                    type(frame_count) is not int or frame_count < 1
                    or type(output_fps) is not int or output_fps < 1
                ):
                    raise _job_error(
                        JobErrorKind.CORRUPT_ARTIFACT,
                        f"degradation result for Shot {request.record_id!r} must report positive "
                        "frame_count and output_fps",
                        job,
                    )
            try:
                bound_shot = store.load(
                    Shot, request.record_id, episode_id=shot.episode_id,
                    version=request.record_version,
                )
            except RecordNotFound as exc:
                raise _job_error(JobErrorKind.INVALID_MANIFEST,
                                 "bound Shot content version no longer exists; restore its project history", job,
                                 cause=exc) from exc
            camera_binding = _camera_profile_binding(request, bound_shot, kind, job)
            output = ShotOutput(
                output_id=uuid.uuid4().hex, episode_id=shot.episode_id, shot_id=shot.shot_id,
                record_version=request.record_version, kind=kind,
                relative_path=result.artifact_path, content_sha256=digest,
                **camera_binding, **provenance,
            )
            existing = store.list_shot_outputs(shot.shot_id, episode_id=shot.episode_id)
            # Ledger tokens are contiguous and append-only, so row count is its head token.
            store.append_shot_output(output, expected_version=len(existing))
            if shot.version == request.record_version:
                if kind is ShotOutputKind.SCENE and shot.status is ShotStatus.PLANNED:
                    shot, token = _transition_loaded_shot(
                        store, shot, token, ShotStatus.BLOCKING, job=job,
                    )
                elif kind is ShotOutputKind.SCENE and shot.status is ShotStatus.BLOCKING:
                    try:
                        check_transition(
                            shot.status, ShotStatus.BLOCKING,
                            TransitionContext(framing_status=shot.framing_status),
                        )
                    except TransitionError as exc:
                        raise _lifecycle_job_error(exc, job) from exc

                delivered = store.list_shot_outputs(
                    shot.shot_id,
                    episode_id=shot.episode_id,
                    record_version=shot.version,
                    kind=ShotOutputKind.DEGRADED,
                )
                if delivered and shot.status is ShotStatus.BUILDING:
                    _transition_loaded_shot(
                        store, shot, token, ShotStatus.REVIEW, job=job,
                    )

    def _is_cancelled(self, key: tuple[str, str]) -> bool:
        with self._condition:
            return key in self._cancelled


def _take_persistence(kind: AdapterKind) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """The (success, terminal) persistence pair of a take-producing kind."""
    if kind is AdapterKind.AUDIO_POST:
        from orchestrator.service.audiopost import persist_audio_post_success, persist_audio_post_terminal
        return persist_audio_post_success, persist_audio_post_terminal
    if kind is AdapterKind.PERFORMANCE_EDIT:
        from orchestrator.service.perfedit import (
            persist_performance_edit_success,
            persist_performance_edit_terminal,
        )
        return persist_performance_edit_success, persist_performance_edit_terminal
    from orchestrator.service.tts import persist_tts_success, persist_tts_terminal
    return persist_tts_success, persist_tts_terminal


def _kind_queue(factory: Callable[[str], JobQueue], project_id: str,
                kind: AdapterKind) -> JobQueue:
    specialized = getattr(factory, "for_kind", None)
    return specialized(project_id, kind) if callable(specialized) else factory(project_id)


def _job_run_id(request: AdapterRequest) -> str:
    """The run the job records: the request's own run_id wherever the adapter checks it.

    ComfyAdapter.generate refuses a job whose run_id differs from its ImageRequest's, so a
    still-image job must carry the request's run_id -- whether the API composed an
    ImageRequest from a pinned profile or passed an explicit request dict through.
    """
    payload = request.payload
    if isinstance(payload, (RenderRequest, DegradeRequest, ImageRequest)):
        return payload.run_id
    if request.kind in _SIDECAR_KINDS and isinstance(payload, dict):
        return payload["request"].run_id
    if request.kind is AdapterKind.AUDIO_REVIEW and isinstance(payload, dict):
        return payload["audio_review_id"]
    if request.kind is AdapterKind.GENERATE:
        run_id = _payload_value(_payload_body(payload), "run_id")
        if isinstance(run_id, str) and run_id.strip():
            return run_id
    return uuid.uuid4().hex


def _payload_body(payload: Any) -> Any:
    return payload.get("request", payload) if isinstance(payload, dict) else payload


def _payload_value(payload: Any, name: str) -> Any:
    return payload.get(name) if isinstance(payload, dict) else getattr(payload, name, None)


def _camera_profile_binding(
    request: AdapterRequest,
    shot: Shot,
    kind: ShotOutputKind,
    job: Job,
) -> dict[str, str | int]:
    if kind not in {
        ShotOutputKind.SCENE, ShotOutputKind.MASTER, ShotOutputKind.PREVIEW,
    }:
        return {}
    payload = _payload_body(request.payload)
    profile = _payload_value(payload, "camera_profile")
    profile_id = _payload_value(profile, "camera_id") if profile is not None else None
    profile_version = _payload_value(profile, "version") if profile is not None else None
    if profile_id is None and profile_version is None:
        return {}
    if profile_id != shot.camera_profile:
        raise _job_error(
            JobErrorKind.INVALID_MANIFEST,
            f"render request uses CameraProfile {profile_id!r}, but Shot "
            f"{shot.shot_id!r} references {shot.camera_profile!r}",
            job,
        )
    if type(profile_version) is not int or profile_version < 1:
        raise _job_error(
            JobErrorKind.INVALID_MANIFEST,
            f"render request for Shot {shot.shot_id!r} has no valid CameraProfile version",
            job,
        )
    return {
        "camera_profile_id": shot.camera_profile,
        "camera_profile_version": shot.camera_profile_version or profile_version,
    }


def _begin_shot_work(store: ProjectStore, request: AdapterRequest, job: Job) -> None:
    """Advance a current Shot into the state required by the submitted stage."""
    if request.kind is AdapterKind.ASSEMBLE:
        return
    with store.command():
        try:
            shot, token = store.load_with_version(
                Shot, request.record_id, episode_id=request.episode_id,
            )
        except RecordNotFound as exc:
            raise _job_error(
                JobErrorKind.INVALID_MANIFEST,
                "bound Shot disappeared before its job started",
                job,
                cause=exc,
            ) from exc
        if shot.version != request.record_version:
            # Historical jobs retain their own outputs but cannot drive today's lifecycle.
            return

        if request.kind is AdapterKind.SCENE and shot.status is ShotStatus.DRAFT:
            if not math.isfinite(shot.duration_sec) or shot.duration_sec <= 0:
                raise _job_error(
                    JobErrorKind.INVALID_MANIFEST,
                    f"Shot {shot.shot_id!r} needs a finite positive duration before planning",
                    job,
                )
            try:
                camera_profile_for_render(store, shot)
            except RecordNotFound as exc:
                raise _job_error(
                    JobErrorKind.INVALID_MANIFEST,
                    f"Shot {shot.shot_id!r} cannot be planned because its CameraProfile "
                    f"{shot.camera_profile!r} cannot be resolved",
                    job,
                    cause=exc,
                ) from exc
            shot, token = _transition_loaded_shot(
                store, shot, token, ShotStatus.PLANNED, job=job,
            )

        if shot.status is ShotStatus.FAILED:
            shot, token = _transition_loaded_shot(
                store, shot, token, ShotStatus.BUILDING, job=job,
            )

        if request.kind is AdapterKind.SCENE:
            if shot.status is ShotStatus.BLOCKING:
                try:
                    check_transition(
                        shot.status, ShotStatus.BLOCKING,
                        TransitionContext(framing_status=shot.framing_status),
                    )
                except TransitionError as exc:
                    raise _lifecycle_job_error(exc, job) from exc
                return
            if shot.status in {ShotStatus.PLANNED, ShotStatus.BUILDING}:
                return
            try:
                check_transition(
                    shot.status, ShotStatus.PLANNED,
                    TransitionContext(framing_status=shot.framing_status),
                )
            except TransitionError as exc:
                raise _lifecycle_job_error(exc, job) from exc
        elif request.kind is AdapterKind.RENDER:
            body = _payload_body(request.payload)
            mode = _payload_value(body, "mode") or "master"
            if mode == "preview":
                if shot.status is not ShotStatus.BLOCKING:
                    raise _job_error(
                        JobErrorKind.INVALID_MANIFEST,
                        f"FR-29 framing preview requires Shot status 'blocking'; "
                        f"found {shot.status.value}",
                        job,
                    )
                return
            if shot.status is ShotStatus.BUILDING:
                return
            _transition_loaded_shot(
                store,
                shot,
                token,
                ShotStatus.BUILDING,
                TransitionContext(framing_status=shot.framing_status),
                job,
            )
        elif request.kind is AdapterKind.DEGRADE and shot.status is not ShotStatus.BUILDING:
            raise _job_error(
                JobErrorKind.INVALID_MANIFEST,
                f"degrade_media requires Shot status 'building'; found {shot.status.value}",
                job,
            )


def _transition_loaded_shot(
    store: ProjectStore,
    shot: Shot,
    token: int,
    target: ShotStatus,
    context: TransitionContext | None = None,
    job: Job | None = None,
) -> tuple[Shot, int]:
    try:
        check_transition(shot.status, target, context)
    except TransitionError as exc:
        raise _lifecycle_job_error(exc, job) from exc
    updated = shot.model_copy(update={"status": target})
    store.update(updated, expected_version=token)
    current, current_token = store.load_with_version(
        Shot, shot.shot_id, episode_id=shot.episode_id,
    )
    return current, current_token


def _lifecycle_job_error(
    exc: TransitionError,
    job: Job | None,
) -> JobError:
    return JobError(
        JobErrorKind.INVALID_MANIFEST,
        str(exc),
        job_id=job.job_id if job is not None else None,
        shot_id=job.shot_id if job is not None else None,
        run_id=job.run_id if job is not None else None,
        cause=exc,
    )


def _fail_shot_after_job_error(
    store: ProjectStore,
    request: AdapterRequest,
) -> BaseException | None:
    """Move only the bound current version from building to failed."""
    if request.kind is AdapterKind.ASSEMBLE:
        return None
    try:
        with store.command():
            try:
                shot, token = store.load_with_version(
                    Shot, request.record_id, episode_id=request.episode_id,
                )
            except RecordNotFound:
                return None
            if shot.version != request.record_version or shot.status is not ShotStatus.BUILDING:
                return None
            _transition_loaded_shot(store, shot, token, ShotStatus.FAILED)
    except Exception as exc:  # noqa: BLE001 - report the secondary failure on the originating job.
        return exc
    return None


def _artifact_path(kind: AdapterKind, value: Any, payload: Any = None) -> str:
    if isinstance(value, AdapterResult):
        return value.artifact_path
    if isinstance(value, str):
        return value
    if kind is AdapterKind.RENDER:
        render_request = _payload_body(payload) if payload is not None else None
        mode = _payload_value(render_request, "mode") if render_request is not None else None
        master = getattr(value, "master_path", None)
        if isinstance(master, str) and (mode == "master" or mode is None):
            return master
        if mode == "master":
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           "master render result does not name an assembled master file")
        frames = getattr(value, "frame_paths", None)
        if frames:
            return frames[0]
        blend = getattr(value, "blend_path", None)
        if isinstance(blend, str):
            return blend
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "render result does not name an artifact")
    for name in {AdapterKind.SCENE: ("scene_path", "blend_path"), AdapterKind.RENDER: ("frame_paths", "blend_path"),
                 AdapterKind.GENERATE: ("image_paths",), AdapterKind.DEGRADE: ("output_path",)}[kind]:
        found = getattr(value, name, None)
        if isinstance(found, str):
            return found
        if found:
            return found[0]
    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "adapter result does not name an artifact")


def _output_kind(kind: AdapterKind, payload: Any) -> ShotOutputKind:
    if kind is AdapterKind.SCENE:
        return ShotOutputKind.SCENE
    if kind is AdapterKind.RENDER:
        request = _payload_body(payload)
        mode = _payload_value(request, "mode")
        return ShotOutputKind.PREVIEW if mode == "preview" else ShotOutputKind.MASTER
    if kind is AdapterKind.GENERATE:
        return ShotOutputKind.STILL
    return ShotOutputKind.DEGRADED


def _provenance(kind: ShotOutputKind, value: Any) -> dict[str, Any]:
    """Project only declared, kind-appropriate provenance from the adapter result."""
    if kind in {ShotOutputKind.SCENE, ShotOutputKind.PREVIEW, ShotOutputKind.MASTER}:
        fields = ("engine_id", "engine_build")
    elif kind is ShotOutputKind.DEGRADED:
        fields = (
            "profile_semver", "filter_graph_hash", "pinned_binaries", "frame_count", "output_fps",
        )
    else:
        fields = ()

    sentinel = object()
    projected: dict[str, Any] = {}
    for name in fields:
        if isinstance(value, Mapping):
            candidate = value.get(name, sentinel)
        else:
            candidate = getattr(value, name, sentinel)
        if candidate is not sentinel:
            projected[name] = candidate
    return projected


def postprofile_spec_from_camera(camera: CameraProfile, spec: PostProfileSpec) -> PostProfileSpec:
    """Apply the domain-tagged post-only CameraProfile values to a PostProfileSpec.

    The domain field tags are the authority; this mapping does not maintain a second
    post-only field list.
    """
    values: dict[str, Any] = {}
    tagged = {
        name for name in CameraProfile.model_fields
        if field_invalidation(CameraProfile, name) is Invalidation.POST_ONLY
    }
    if "fps" in tagged:
        values["output_fps"] = int(camera.fps)
    if "storage_resolution" in tagged:
        values["storage_resolution"] = camera.storage_resolution
    if "display_aspect_ratio" in tagged or "storage_resolution" in tagged:
        display = Fraction(camera.display_aspect_ratio.replace(":", "/"))
        width, height = camera.storage_resolution
        values["sample_aspect_ratio"] = str(display / Fraction(width, height))
    if "frame_drop_schedule" in tagged:
        values["artifact_schedule"] = tuple(camera.frame_drop_schedule)
    encode_updates: dict[str, Any] = {}
    if "codec" in tagged:
        encode_updates["codec"] = camera.codec
    encode = spec.encode.model_copy(update=encode_updates) if encode_updates else spec.encode
    values["encode"] = encode
    mapped = spec.model_copy(update=values)
    return PostProfileSpec.model_validate(mapped.model_dump(mode="python"))


def _job_error(kind: JobErrorKind, message: str, job: Job,
               cause: BaseException | None = None) -> JobError:
    return JobError(kind, message, job_id=job.job_id, shot_id=job.shot_id,
                    run_id=job.run_id, cause=cause)


def _persist_job(root: ProjectRoot, job: Job, error: JobError, settings: Settings) -> None:
    # A cancellation is not a failure. This fallback path previously wrote "failed" for
    # every error kind, so a job cancelled before its adapter started reported "failed"
    # and overwrote the "cancelled" state cancel() had already persisted. FR-27 keeps the
    # two distinct precisely so an operator can tell "I stopped this" from "this broke",
    # and JobQueue's own terminal branch already makes the same distinction.
    job.state = "cancelled" if error.kind is JobErrorKind.CANCELLED else "failed"
    job.error_kind = error.kind.value
    details = failure_diagnostics(
        error,
        origin=error.origin or ("runner" if error.kind is JobErrorKind.INTERNAL else None),
        secrets=settings.diagnostic_secrets(),
    )
    for name, value in details.items():
        setattr(job, name, value)
    append_job(root, job)
