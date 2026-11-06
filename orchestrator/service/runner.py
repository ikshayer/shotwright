"""Asynchronous adapter dispatch with append-only, version-bound outputs."""

from __future__ import annotations

import hashlib
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
from orchestrator.domain.base import Invalidation
from orchestrator.domain.entities import (
    CameraProfile,
    Job,
    Shot,
    ShotOutput,
    ShotOutputKind,
)
from orchestrator.domain.invalidation import field_invalidation
from orchestrator.jobs.journal import append_job, read_job
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind, failure_diagnostics
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore, RecordNotFound


class AdapterKind(StrEnum):
    SCENE = "scene"
    RENDER = "render"
    GENERATE = "generate"
    DEGRADE = "degrade"


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
        with store.command():
            try:
                shot = store.load(Shot, request.record_id, episode_id=request.episode_id)
            except RecordNotFound as exc:
                raise RecordNotFound(f"bound Shot {request.record_id!r} does not exist") from exc
            episode_id = shot.episode_id
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
                run_id=(request.payload.run_id if isinstance(request.payload, RenderRequest)
                        else uuid.uuid4().hex),
            )
            # Admission refusal is part of submit's synchronous contract. The id is
            # not persisted or returned until preflight succeeds.
            queue = _kind_queue(self.queue_for, project_id, request.kind)
            preflight = queue.preflight_cpu if request.kind in {
                AdapterKind.SCENE, AdapterKind.DEGRADE,
            } else queue.preflight
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
        record = read_job(root, job_id)
        if record is None:
            raise RecordNotFound(f"job {job_id!r} does not exist")
        job = Job.model_validate(record)
        if job.state not in {"completed", "failed", "cancelled"}:
            with self._condition:
                self._cancelled.add((project_id, job_id))
                job.state = "cancelled"
                job.error_kind = JobErrorKind.CANCELLED.value
                append_job(root, job)
                self._condition.notify_all()
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
            if callable(set_cancel_check):
                set_cancel_check(lambda: self._is_cancelled(key))

            def invoke_if_not_cancelled(active: Job) -> AdapterResult:
                if self._is_cancelled(key):
                    raise _job_error(
                        JobErrorKind.CANCELLED, "job cancelled before adapter start", active
                    )
                try:
                    return adapter.invoke(request, active, queue=queue)
                except Exception as exc:
                    raise JobError.from_exception(
                        exc, job_id=active.job_id, shot_id=active.shot_id,
                        run_id=active.run_id, origin="adapter",
                    ) from exc

            if self._is_cancelled(key):
                raise _job_error(JobErrorKind.CANCELLED, "job cancelled before adapter start", job)
            if request.kind in {AdapterKind.RENDER, AdapterKind.GENERATE}:
                with self._gpu_lock:
                    value = queue.run(
                        job, invoke_if_not_cancelled,
                        startup_allowance_seconds=self.settings.resources.startup_allowance_seconds,
                    )
            else:
                queue._transition(job, "queued")
                preflight = queue.preflight_cpu if request.kind in {
                    AdapterKind.SCENE, AdapterKind.DEGRADE,
                } else queue.preflight
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
                artifact_path=_artifact_path(request.kind, value), value=value
            )
            self._record_result(store, root, request, job, result)
        except JobError as exc:
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
            job.state, job.error_kind = "failed", error.kind.value
            _persist_job(root, job, error, self.settings)
        finally:
            with self._condition:
                self._active.discard(key)
                self._cancelled.discard(key)
                self._condition.notify_all()

    def _record_result(self, store: ProjectStore, root: ProjectRoot, request: AdapterRequest,
                       job: Job, result: AdapterResult) -> None:
        # Artifact is project-relative and resolved by the sole containment authority.
        artifact = root.resolve(result.artifact_path)
        if not artifact.is_file():
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT,
                           f"adapter output is missing: {result.artifact_path}",
                           job_id=job.job_id, shot_id=job.shot_id, run_id=job.run_id)
        hasher = hashlib.sha256()
        with artifact.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        digest = hasher.hexdigest()
        with store.command():
            try:
                shot = store.load(Shot, request.record_id, episode_id=request.episode_id)
            except RecordNotFound as exc:
                raise _job_error(JobErrorKind.INVALID_MANIFEST,
                                 "bound Shot was deleted before recording; restore the Shot history", job,
                                 cause=exc) from exc
            kind = _output_kind(request.kind, request.payload)
            provenance = _provenance(kind, result.value)
            output = ShotOutput(
                output_id=uuid.uuid4().hex, episode_id=shot.episode_id, shot_id=shot.shot_id,
                record_version=request.record_version, kind=kind,
                relative_path=result.artifact_path, content_sha256=digest, **provenance,
            )
            try:
                store.load(Shot, request.record_id, episode_id=shot.episode_id,
                           version=request.record_version)
            except RecordNotFound as exc:
                raise _job_error(JobErrorKind.INVALID_MANIFEST,
                                 "bound Shot content version no longer exists; restore its project history", job,
                                 cause=exc) from exc
            existing = store.list_shot_outputs(shot.shot_id, episode_id=shot.episode_id)
            # Ledger tokens are contiguous and append-only, so row count is its head token.
            store.append_shot_output(output, expected_version=len(existing))

    def _is_cancelled(self, key: tuple[str, str]) -> bool:
        with self._condition:
            return key in self._cancelled


def _kind_queue(factory: Callable[[str], JobQueue], project_id: str,
                kind: AdapterKind) -> JobQueue:
    specialized = getattr(factory, "for_kind", None)
    return specialized(project_id, kind) if callable(specialized) else factory(project_id)


def _payload_body(payload: Any) -> Any:
    return payload.get("request", payload) if isinstance(payload, dict) else payload


def _payload_value(payload: Any, name: str) -> Any:
    return payload.get(name) if isinstance(payload, dict) else getattr(payload, name, None)


def _artifact_path(kind: AdapterKind, value: Any) -> str:
    if isinstance(value, AdapterResult):
        return value.artifact_path
    if isinstance(value, str):
        return value
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
    if kind in {ShotOutputKind.SCENE, ShotOutputKind.MASTER, ShotOutputKind.PREVIEW}:
        fields = ("engine_id", "engine_build")
    elif kind is ShotOutputKind.DEGRADED:
        fields = ("profile_semver", "filter_graph_hash", "pinned_binaries")
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
