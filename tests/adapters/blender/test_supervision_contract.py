from __future__ import annotations

import errno
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.adapters.blender.runner import BlenderAdapter
from orchestrator.domain.entities import Job
from orchestrator.jobs.lease import GpuLease, MemoryReading
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings, parse_settings
from orchestrator.store.json_store import ProjectStore
from tests.adapters.blender.test_scene_contract import request


class Nvml:
    def device_memory(self) -> MemoryReading:
        return MemoryReading(1300, 12227, 10927)

    def process_usage(self) -> list[object]:
        return []


class Host:
    def free_ram_gb(self) -> float:
        return 20.0

    def free_disk_gb(self, _path: Path) -> float:
        return 100.0


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Child:
    pid = 8383

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode if self.returncode is not None else None

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="blender", timeout=timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakePopen:
    """A Popen-shaped boundary fake that reads the actual worker plan."""

    def __init__(self, *, fail: str | None = None, omit: str | None = None) -> None:
        self.fail, self.omit = fail, omit
        self.calls: list[tuple[list[str], dict[str, Any], Child]] = []
        self.completed = threading.Event()
        self.metadata_path: str | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> Child:
        stdout = kwargs.get("stdout")
        assert stdout is not None and hasattr(stdout, "write")
        assert kwargs.get("shell") in (None, False)
        if self.fail == "launch":
            raise FileNotFoundError("fake Blender executable missing")
        plan = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        if stdout is not None:
            self._write_log(stdout, b"Frame 1\n")
        paths = plan["outputs"]
        self.metadata_path = paths["metadata"]
        metadata = {
            "schema_version": 1,
            "shot_id": plan["shot_id"], "shot_version": plan["shot_version"],
            "run_id": plan["run_id"], "tool_version": "5.2.0",
            "build_hash": "abc123", "actual_device": plan["gpu_device"],
            "applied_settings": {"engine": {
                    "workbench": "BLENDER_WORKBENCH",
                    "eevee": "BLENDER_EEVEE",
                    "cycles": "CYCLES",
                }[plan["engine"]], "width": plan["width"],
                "height": plan["height"], "fps": plan["fps"], "samples": plan["samples"]},
            "geometry_fingerprint": "a" * 64, "child_pid": 8383,
            "elapsed_seconds": 0.01, "status": "completed",
        }
        if self.fail == "oom":
            self._write_log(stdout, b"CUDA out of memory\n")
            child = Child(1)
        elif self.fail == "disk":
            self._write_log(stdout, b"OSError: [Errno 28] No space left on device\n")
            child = Child(1)
        elif self.fail == "nonzero":
            self._write_log(stdout, b"worker exploded\n")
            child = Child(7)
        elif self.fail == "hang":
            child = Child(None)
        else:
            child = Child(0)
        for key, path_text in (("metadata", paths["metadata"]), ("blend", paths["blend"])):
            if self.omit == key:
                continue
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            if key == "metadata":
                path.write_text(json.dumps(metadata), encoding="utf-8")
            else:
                path.write_bytes(b"BLENDER-v300")
        for index, path_text in enumerate(paths["frames"]):
            if self.omit == "frame" and index == 0:
                continue
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix.lower() == ".exr":
                path.write_bytes(b"\x76\x2f\x31\x01" + b"frame")
            else:
                path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"frame")
        self.calls.append((argv, kwargs, child))
        self.completed.set()
        return child

    @staticmethod
    def _write_log(stream: Any, payload: bytes) -> None:
        try:
            stream.write(payload)
        except TypeError:
            stream.write(payload.decode("utf-8"))
        stream.flush()


class _MasterEncodeProcess:
    """Completed FFmpeg fake that writes the authorized master output file."""

    returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class _FakeMasterFFmpeg:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _MasterEncodeProcess:
        assert kwargs.get("shell") is False, "master assembly must not use a shell"
        assert "-c:v" in argv and argv[argv.index("-c:v") + 1] == "ffv1", (
            f"master assembly fake received unexpected codec argv={argv!r}"
        )
        output = Path(argv[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake lossless FFV1 master")
        self.calls.append((argv, kwargs))
        return _MasterEncodeProcess()


class RetryPopen(FakePopen):
    def __call__(self, argv: list[str], **kwargs: Any) -> Child:
        self.omit = "frame" if len(self.calls) == 0 else None
        return super().__call__(argv, **kwargs)


class SignalingHangPopen(FakePopen):
    def __init__(self) -> None:
        super().__init__(fail="hang")
        self.started = threading.Event()

    def __call__(self, argv: list[str], **kwargs: Any) -> Child:
        child = super().__call__(argv, **kwargs)
        self.started.set()
        return child


def setup(tmp_path: Path, *, floor: float = 0.0, wall: float = 900.0,
          popen: Any | None = None, ffmpeg_popen: Any | None = None,
          queue_clock: Any | None = None):
    root = ProjectRoot(tmp_path)
    exe = root.write_bytes("fake-blender.exe", b"executable")
    settings = parse_settings({"resources": {"throughput_floor_per_second": floor,
                                               "vram_release_timeout_seconds": 0.05},
                               "budget": {"per_shot_wall_clock_seconds": wall},
                               "binaries": {"blender_exe": str(exe)}})
    nvml = Nvml()
    adapter = BlenderAdapter(root, settings, popen=popen or
                             (lambda *_a, **_k: pytest.fail("unexpected Popen")),
                             ffmpeg_popen=ffmpeg_popen or _FakeMasterFFmpeg())
    lease = GpuLease(root, settings, nvml=nvml, worker=adapter.worker,
                     pid_alive=lambda _pid: True, ttl_seconds=30)
    queue = JobQueue(root, settings, lease, ProjectStore(root), nvml=nvml,
                     host=Host(), quarantine=ProjectQuarantine(root),
                     clock=queue_clock or time.monotonic)
    job = Job(job_id="job-1", type="blender_render", shot_id="shot-1", run_id="run-1")
    return root, settings, adapter, queue, job


def test_render_accepts_positive_floor_and_forwards_measured_startup_allowance(
    tmp_path: Path,
) -> None:
    fake = FakePopen()
    _root, _settings, adapter, queue, job = setup(tmp_path, floor=0.001, popen=fake)
    original_run = queue.run
    forwarded: list[float | None] = []

    def capture_run(*args: Any, **kwargs: Any) -> Any:
        forwarded.append(kwargs.get("startup_allowance_seconds"))
        return original_run(*args, **kwargs)

    queue.run = capture_run  # type: ignore[method-assign]
    result = adapter.render(request(), job, queue=queue)

    assert result.generation_run.status == "completed"
    assert len(fake.calls) == 1
    assert forwarded == [Settings().resources.startup_allowance_seconds]


@pytest.mark.parametrize("mismatch", ["shot", "run"])
def test_render_rejects_job_correlation_mismatch_before_queue_or_writes(
    tmp_path: Path, mismatch: str,
) -> None:
    root, _settings, adapter, queue, job = setup(tmp_path)
    if mismatch == "shot":
        job.shot_id = "other-shot"
    else:
        job.run_id = "other-run"
    with pytest.raises(ValueError):
        adapter.render(request(), job, queue=queue)
    assert not root.resolve("renders").exists()


def test_render_rejects_queue_with_different_worker_before_queue_or_writes(tmp_path: Path) -> None:
    root, settings, adapter, _queue, job = setup(tmp_path)
    nvml = Nvml()

    class OtherWorker:
        def pid(self) -> int:
            return 555

        def unload(self) -> None: pass
        def terminate(self) -> bool: return True
        def respawn(self) -> int: return 556

    lease = GpuLease(root, settings, nvml=nvml, worker=OtherWorker(),
                     pid_alive=lambda _pid: True)
    mismatched_queue = JobQueue(root, settings, lease, ProjectStore(root), nvml=nvml,
                                host=Host(), quarantine=ProjectQuarantine(root))
    with pytest.raises(ValueError):
        adapter.render(request(), job, queue=mismatched_queue)
    assert not root.resolve("renders").exists()


def test_render_rejects_queue_with_different_settings_object_before_writes(tmp_path: Path) -> None:
    root, settings, adapter, queue, job = setup(tmp_path)
    queue.settings = settings.model_copy(deep=True)
    with pytest.raises(ValueError):
        adapter.render(request(), job, queue=queue)
    assert not root.resolve("renders").exists()


def test_render_uses_real_queue_and_authorized_paths_on_fake_process_success(tmp_path: Path) -> None:
    fake = FakePopen()
    root, _settings, adapter, queue, job = setup(tmp_path, popen=fake)
    result = adapter.render(request(), job, queue=queue)
    assert len(fake.calls) == 1
    argv, kwargs, child = fake.calls[0]
    assert child.pid != adapter.worker.pid()
    assert kwargs["cwd"] is not None
    plan = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
    assert set(plan["outputs"]) == {"metadata", "blend", "frames"}
    assert all(Path(p).is_relative_to(root.path)
               for p in [plan["outputs"]["metadata"], plan["outputs"]["blend"],
                         *plan["outputs"]["frames"]])
    assert result.blend_path.startswith("renders/shot-1/v0002/run-1/")
    assert result.frame_paths and result.metadata_path and result.log_path
    assert result.generation_run.status == "completed"


@pytest.mark.parametrize(
    ("failure", "kind"),
    [("launch", JobErrorKind.WORKER_UNREACHABLE), ("oom", JobErrorKind.OUT_OF_MEMORY),
     ("disk", JobErrorKind.DISK_EXHAUSTED), ("nonzero", JobErrorKind.INTERNAL)],
)
def test_subprocess_failures_are_mapped_and_correlated(
    tmp_path: Path, failure: str, kind: JobErrorKind,
) -> None:
    fake = FakePopen(fail=failure)
    _root, _settings, adapter, queue, job = setup(tmp_path, popen=fake)
    with pytest.raises(JobError) as exc:
        adapter.render(request(mode="preview"), job, queue=queue)
    assert exc.value.kind is kind
    assert (exc.value.job_id, exc.value.shot_id, exc.value.run_id) == ("job-1", "shot-1", "run-1")


def test_failure_record_enospc_is_surfaced_with_original_subprocess_diagnostic(
    tmp_path: Path,
) -> None:
    fake = FakePopen(fail="nonzero")
    root, _settings, adapter, queue, job = setup(tmp_path, popen=fake)
    original_write_text = root.write_text

    def fail_attempt_failure_record(relative: str, text: str, *, encoding: str = "utf-8") -> Path:
        normalized = relative.replace("\\", "/").lower()
        name = Path(normalized).name
        attempt_scoped = "/attempt-" in f"/{normalized}"
        failure_record = name.endswith((".json", ".jsonl")) and (
            "failure" in name or "error" in name or "job_error" in text.lower()
        )
        if attempt_scoped and failure_record:
            raise OSError(errno.ENOSPC, "No space left while writing attempt failure record")
        return original_write_text(relative, text, encoding=encoding)

    root.write_text = fail_attempt_failure_record  # type: ignore[method-assign]
    with pytest.raises(JobError) as exc:
        adapter.render(request(mode="preview"), job, queue=queue)
    assert exc.value.kind is JobErrorKind.DISK_EXHAUSTED
    assert (exc.value.job_id, exc.value.shot_id, exc.value.run_id) == ("job-1", "shot-1", "run-1")

    pending: list[BaseException] = [exc.value]
    diagnostics: list[str] = []
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        diagnostics.append(str(current))
        diagnostics.extend(getattr(current, "__notes__", ()))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    combined = "\n".join(diagnostics).lower()
    assert "no space left" in combined or "enospc" in combined
    assert "worker exploded" in combined, "the initial child failure diagnostic was lost"


@pytest.mark.parametrize("missing", ["metadata", "blend", "frame"])
def test_success_exit_with_missing_output_is_corrupt_artifact(
    tmp_path: Path, missing: str,
) -> None:
    fake = FakePopen(omit=missing)
    _root, _settings, adapter, queue, job = setup(tmp_path, popen=fake)
    with pytest.raises(JobError) as exc:
        adapter.render(request(mode="preview"), job, queue=queue)
    assert exc.value.kind is JobErrorKind.CORRUPT_ARTIFACT


def test_duplicate_run_is_rejected_without_overwriting_retained_artifacts(tmp_path: Path) -> None:
    fake = FakePopen()
    root, _settings, adapter, queue, job = setup(tmp_path, popen=fake)
    adapter.render(request(), job, queue=queue)
    before = {p.relative_to(root.path).as_posix(): p.read_bytes()
              for p in root.resolve("renders").rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        adapter.render(request(), job, queue=queue)
    after = {p.relative_to(root.path).as_posix(): p.read_bytes()
             for p in root.resolve("renders").rglob("*") if p.is_file()}
    assert after == before
    assert len(fake.calls) == 1


def test_cancellation_stops_child_before_queue_releases_lease_and_quarantines_run(
    tmp_path: Path,
) -> None:
    fake = SignalingHangPopen()
    root, _settings, adapter, queue, job = setup(tmp_path, popen=fake)
    with pytest.raises(JobError) as exc:
        adapter.render(request(mode="preview"), job, queue=queue, cancel=fake.started.is_set)
    assert exc.value.kind is JobErrorKind.CANCELLED
    child = fake.calls[0][2]
    assert child.poll() is not None
    assert child.terminated or child.killed
    assert not queue.lease._owned
    quarantined = list(root.resolve("quarantine").rglob("*-run-1"))
    assert quarantined and quarantined[0].is_dir()
    assert list(quarantined[0].rglob("attempt-*"))
    assert not root.resolve("renders/shot-1/v0002/run-1").exists()


class _LiveMasterEncode(Child):
    """A bounded fake even if the regression reintroduces an unbounded wait."""

    def __init__(self, stop_on: str) -> None:
        super().__init__(None)
        self.stop_on = stop_on
        self.waiting = threading.Event()
        self.exited = threading.Event()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        self.waiting.set()
        self.exited.wait(min(timeout if timeout is not None else 0.05, 0.05))
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.stop_on == "terminate":
            self.returncode = -15
            self.exited.set()

    def kill(self) -> None:
        self.killed = True
        if self.stop_on != "never":
            self.returncode = -9
            self.exited.set()


@pytest.mark.parametrize("stop_on", ["terminate", "kill", "never"])
def test_master_encode_cancellation_supervises_child_and_retains_lease_if_it_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_on: str,
) -> None:
    child = _LiveMasterEncode(stop_on)
    factory_calls: list[list[str]] = []

    def ffmpeg(argv: list[str], **_kwargs: Any) -> Child:
        factory_calls.append(argv)
        return child

    root, _settings, adapter, queue, job = setup(tmp_path, popen=FakePopen(), ffmpeg_popen=ffmpeg)
    control = adapter._control
    original_launch, original_stop, original_clear = control.launch, control._stop_and_join, control.clear_process
    launched: list[tuple[object, object]] = []
    stops: list[tuple[bool, bool]] = []
    cleared_alive: list[bool] = []
    release_survivors: list[bool] = []
    original_release = queue.lease.release

    def launch(argv: list[str], **kwargs: Any) -> Any:
        process = original_launch(argv, **kwargs)
        if process is child:
            launched.append((kwargs.get("popen"), control._process))
        return process

    def stop(reason: Any) -> bool:
        result = original_stop(reason)
        stops.append((result, child.poll() is None))
        return result

    def clear(process: Any) -> None:
        if process is child:
            cleared_alive.append(child.poll() is None)
        original_clear(process)

    def release(*args: Any, **kwargs: Any) -> None:
        try:
            original_release(*args, **kwargs)
        finally:
            if not queue.lease._owned:
                release_survivors.append(child.poll() is None)

    monkeypatch.setattr(control, "launch", launch)
    monkeypatch.setattr(control, "_stop_and_join", stop)
    monkeypatch.setattr(control, "clear_process", clear)
    monkeypatch.setattr(queue.lease, "release", release)
    try:
        with pytest.raises(JobError) as exc:
            adapter.render(request(), job, queue=queue, cancel=child.waiting.is_set)
        assert factory_calls, "cancellation never reached the FFmpeg master encode"
        assert launched == [(ffmpeg, child)], f"encode bypassed supervisor registration: {launched!r}"
        assert child.terminated, "supervisor never requested FFmpeg termination"
        assert child.waits and all(t is not None and t > 0 for t in child.waits), (
            f"FFmpeg received an unbounded or invalid wait: {child.waits!r}"
        )
        assert not any(cleared_alive), f"live FFmpeg registration was cleared: {cleared_alive!r}"
        assert not any(quiesced and alive for quiesced, alive in stops), (
            f"supervisor reported quiescence with a surviving FFmpeg: {stops!r}"
        )
        assert not any(release_survivors), f"lease released with live FFmpeg: {release_survivors!r}"
        if stop_on == "never":
            assert child.poll() is None and child.killed, "resistant fake did not exercise surviving kill"
            assert control._process is child, "surviving FFmpeg lost its supervisor registration"
            assert queue.lease._owned, "lease was released despite unconfirmed FFmpeg termination"
            assert stops and not any(result for result, _alive in stops), f"false quiescence: {stops!r}"
            assert exc.value.kind is JobErrorKind.WORKER_UNREACHABLE, f"wrong failure: {exc.value}"
        else:
            assert child.poll() is not None, "FFmpeg survived cancellation"
            assert child.killed == (stop_on == "kill"), f"unexpected kill escalation for {stop_on}"
            assert not queue.lease._owned, "lease stayed held after confirmed FFmpeg exit"
            assert exc.value.kind is JobErrorKind.CANCELLED, f"encode exit masked cancellation: {exc.value}"
            assert job.state == "cancelled", f"cancelled encode reported state {job.state!r}"
            assert list(root.resolve("quarantine").rglob("*-run-1")), "partial run was not quarantined"
    finally:
        # Release the deliberately immortal fake and its test-only lease after assertions.
        child.returncode = -9
        child.exited.set()
        if queue.lease._owned:
            queue.lease.release(0, 0.1)


@pytest.mark.parametrize("finishes_late", [False, True])
def test_master_encode_wait_uses_remaining_budget_and_stops_on_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finishes_late: bool,
) -> None:
    import orchestrator.adapters.blender.runner as runner_module

    clock = ManualClock()
    clock.now = 104.98  # Rendering already consumed 4.98 of the five-second shot budget.
    child = _LiveMasterEncode("terminate")
    waits: list[float | None] = []

    def wait(timeout: float | None = None) -> int:
        waits.append(timeout)
        if child.poll() is not None:
            return child.returncode
        assert timeout is not None and 0 < timeout <= 0.05, f"invalid encode wait: {timeout!r}"
        clock.now += timeout
        if finishes_late:
            clock.now += 0.001
            child.returncode = 0
            child.exited.set()
            _root.write_bytes("encode/master.mkv", b"late master")
            return 0
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)

    monkeypatch.setattr(child, "wait", wait)
    _root, _settings, adapter, _queue, job = setup(tmp_path, ffmpeg_popen=lambda *_a, **_k: child)
    monkeypatch.setattr(runner_module, "time", SimpleNamespace(monotonic=clock))
    epoch = adapter._control.activate()
    try:
        with pytest.raises(JobError) as exc:
            adapter._assemble_master(attempt_rel="encode", frame_rate=60, frame_count=900, job=job,
                                     hard_deadline=105.0, cancel=None)
        assert waits == [pytest.approx(0.02)], f"encode received a fresh budget: waits={waits!r}"
        assert exc.value.kind is JobErrorKind.BUDGET_EXCEEDED, f"wrong deadline error: {exc.value}"
        assert child.poll() is not None, "expired encode child was left alive"
        assert child.terminated != finishes_late, f"wrong termination behavior for late exit={finishes_late}"
    finally:
        adapter._control.deactivate(epoch)


@pytest.mark.parametrize("reason", [JobErrorKind.CANCELLED, JobErrorKind.BUDGET_EXCEEDED])
def test_master_encode_does_not_launch_after_stop_or_exhausted_budget(
    tmp_path: Path, reason: JobErrorKind,
) -> None:
    calls: list[object] = []
    _root, _settings, adapter, _queue, job = setup(tmp_path, ffmpeg_popen=lambda *args, **_k: calls.append(args))
    epoch = adapter._control.activate()
    if reason is JobErrorKind.CANCELLED:
        adapter._control.stop_for(reason)
    try:
        with pytest.raises(JobError) as exc:
            adapter._assemble_master(attempt_rel="encode", frame_rate=60, frame_count=900, job=job,
                                     hard_deadline=0 if reason is JobErrorKind.BUDGET_EXCEEDED else float("inf"),
                                     cancel=None)
        assert exc.value.kind is reason, f"prelaunch stop classified as {exc.value.kind}"
        assert calls == [], f"FFmpeg launched after {reason.value}: {calls!r}"
    finally:
        adapter._control.deactivate(epoch)


def test_retry_retains_each_attempt_and_publishes_only_successful_result(tmp_path: Path) -> None:
    fake = RetryPopen()
    root, _settings, adapter, queue, job = setup(tmp_path, popen=fake)
    result = adapter.render(request(mode="preview"), job, queue=queue)
    attempt_dirs = sorted(p.name for p in root.resolve("renders/shot-1/v0002/run-1").iterdir()
                          if p.is_dir())
    assert attempt_dirs == ["attempt-0001", "attempt-0002"]
    assert "/attempt-0002/" in result.metadata_path.replace("\\", "/")
    assert len(fake.calls) == 2


def test_render_entry_revalidates_unvalidated_model_copy_before_writing(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job = setup(tmp_path)
    bad_profile = request().camera_profile.model_copy(update={"field_of_view_mm": float("nan")})
    bad_request = request(camera_profile=bad_profile)
    with pytest.raises(ValueError):
        adapter.render(bad_request, job, queue=queue)
    assert not root.resolve("renders").exists()


@pytest.mark.parametrize("update", [
    {"camera_position": (0, 0, 0)},  # equals the scene target
    {"practical_light_color_k": 0},
    {"depth_of_field": 0.0},
])
def test_render_revalidates_invalid_rebuild_overrides_before_writing(
    tmp_path: Path, update: dict[str, object],
) -> None:
    root, _settings, adapter, queue, job = setup(tmp_path)
    profile = request().camera_profile.model_copy(update=update)
    with pytest.raises(ValueError):
        adapter.render(request(camera_profile=profile), job, queue=queue)
    assert not root.resolve("renders").exists()


def test_preview_deadline_is_budget_exceeded_and_stops_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.adapters.blender.runner as runner_module

    fake = SignalingHangPopen()
    _root, _settings, adapter, queue, job = setup(tmp_path, popen=fake)
    monkeypatch.setattr(
        runner_module.time,
        "monotonic",
        lambda: 111.0 if fake.started.is_set() else 100.0,
    )
    with pytest.raises(JobError) as exc:
        adapter.render(request(mode="preview"), job, queue=queue)
    assert exc.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert fake.started.is_set()
    assert fake.calls[0][2].poll() is not None


def test_same_adapter_rejects_concurrent_different_run_before_second_reservation(
    tmp_path: Path,
) -> None:
    fake = SignalingHangPopen()
    root, _settings, adapter, queue, first_job = setup(tmp_path, popen=fake)
    cancel_first = threading.Event()
    first_outcome: list[Exception] = []

    def first_render() -> None:
        try:
            adapter.render(request(mode="preview"), first_job, queue=queue,
                           cancel=cancel_first.is_set)
        except Exception as exc:  # noqa: BLE001  # propagated from render thread
            first_outcome.append(exc)

    thread = threading.Thread(target=first_render)
    thread.start()
    try:
        assert fake.started.wait(timeout=2)
        second_job = Job(job_id="job-2", type="blender_render", shot_id="shot-1", run_id="run-2")
        with pytest.raises(RuntimeError):
            adapter.render(request(mode="preview", run_id="run-2"), second_job, queue=queue)
        assert not root.resolve("renders/shot-1/v0002/run-2").exists()
    finally:
        cancel_first.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert first_outcome and isinstance(first_outcome[0], JobError)
    assert first_outcome[0].kind is JobErrorKind.CANCELLED
    assert len(fake.calls) == 1


def test_two_adapters_cannot_reserve_and_overwrite_the_same_run(
    tmp_path: Path,
) -> None:
    first_fake = SignalingHangPopen()
    root, settings, first_adapter, first_queue, first_job = setup(tmp_path, popen=first_fake)
    nvml = Nvml()
    second_fake = FakePopen()
    second_adapter = BlenderAdapter(root, settings, popen=second_fake)
    second_lease = GpuLease(root, settings, nvml=nvml, worker=second_adapter.worker,
                            pid_alive=lambda _pid: True, ttl_seconds=30)
    second_queue = JobQueue(root, settings, second_lease, ProjectStore(root), nvml=nvml,
                            host=Host(), quarantine=ProjectQuarantine(root))
    cancel_first = threading.Event()
    first_outcome: list[Exception] = []

    def first_render() -> None:
        try:
            first_adapter.render(request(mode="preview"), first_job, queue=first_queue,
                                 cancel=cancel_first.is_set)
        except Exception as exc:  # noqa: BLE001  # propagated from render thread
            first_outcome.append(exc)

    thread = threading.Thread(target=first_render)
    thread.start()
    try:
        assert first_fake.started.wait(timeout=2)
        same_run_job = Job(job_id="job-second", type="blender_render", shot_id="shot-1", run_id="run-1")
        with pytest.raises(FileExistsError):
            second_adapter.render(request(mode="preview"), same_run_job, queue=second_queue)
        assert second_fake.calls == []
        assert len(list(root.resolve("renders/shot-1/v0002/run-1").glob("attempt-*"))) == 1
    finally:
        cancel_first.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert first_outcome and isinstance(first_outcome[0], JobError)
    assert first_outcome[0].kind is JobErrorKind.CANCELLED


def test_queue_timeout_at_command_launch_barrier_prevents_late_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.adapters.blender.runner as runner_module

    fake = FakePopen()
    clock = ManualClock()
    root, _settings, adapter, queue, job = setup(tmp_path, wall=5.0, popen=fake, queue_clock=clock)
    command_ready, release_command, cleanup_started = threading.Event(), threading.Event(), threading.Event()
    original_build = runner_module.build_command
    original_unload = adapter.worker.unload

    def paused_build(*args: Any, **kwargs: Any) -> list[str]:
        command = original_build(*args, **kwargs)
        command_ready.set()
        assert release_command.wait(timeout=2)
        return command

    def tracked_unload() -> None:
        cleanup_started.set()
        original_unload()

    monkeypatch.setattr(runner_module, "build_command", paused_build)
    adapter.worker.unload = tracked_unload  # type: ignore[method-assign]
    outcomes: list[Exception] = []

    def render() -> None:
        try:
            adapter.render(request(mode="preview"), job, queue=queue)
        except Exception as exc:  # noqa: BLE001  # propagated from render thread  # capture thread outcome for assertion in this thread
            outcomes.append(exc)

    thread = threading.Thread(target=render)
    thread.start()
    try:
        assert command_ready.wait(timeout=2)
        clock.now = 6.0
        assert cleanup_started.wait(timeout=2), "queue deadline did not enter worker cleanup"
        release_command.set()
    finally:
        release_command.set()
        thread.join(timeout=3)
    assert not thread.is_alive(), "render/lease cleanup returned before attempt supervisor stopped"
    assert fake.calls == [], "a process launched after the queue timeout stop signal"
    assert outcomes and isinstance(outcomes[0], JobError)
    assert outcomes[0].kind is JobErrorKind.BUDGET_EXCEEDED
    assert root.resolve("renders").exists(), "failed attempt diagnostics must be retained"
    assert list(root.resolve("renders").rglob("attempt-*"))
    assert job.state == "failed" and job.error_kind == JobErrorKind.BUDGET_EXCEEDED.value


def test_queue_timeout_during_final_metadata_validation_blocks_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePopen()
    clock = ManualClock()
    root, _settings, adapter, queue, job = setup(tmp_path, wall=5.0, popen=fake, queue_clock=clock)
    validation_started, release_validation, cleanup_started = (
        threading.Event(), threading.Event(), threading.Event()
    )
    original_resolve = root.resolve
    original_unload = adapter.worker.unload

    def paused_resolve(relative: str) -> Path:
        resolved = original_resolve(relative)
        if fake.completed.is_set() and fake.metadata_path is not None and resolved == Path(fake.metadata_path):
            validation_started.set()
            assert release_validation.wait(timeout=2)
        return resolved

    def tracked_unload() -> None:
        cleanup_started.set()
        original_unload()

    root.resolve = paused_resolve  # type: ignore[method-assign]
    adapter.worker.unload = tracked_unload  # type: ignore[method-assign]
    outcomes: list[Exception] = []

    def render() -> None:
        try:
            adapter.render(request(mode="preview"), job, queue=queue)
        except Exception as exc:  # noqa: BLE001  # propagated from render thread  # capture thread outcome for assertion in this thread
            outcomes.append(exc)

    thread = threading.Thread(target=render)
    thread.start()
    try:
        assert validation_started.wait(timeout=2), "attempt did not reach its final verification phase"
        clock.now = 6.0
        assert cleanup_started.wait(timeout=2), "queue deadline did not stop the final attempt phase"
        release_validation.set()
    finally:
        release_validation.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert outcomes and isinstance(outcomes[0], JobError)
    assert outcomes[0].kind is JobErrorKind.BUDGET_EXCEEDED
    assert not queue.lease._owned


def test_timeout_before_queue_callable_body_prevents_late_attempt_and_writes(
    tmp_path: Path,
) -> None:
    fake = FakePopen()
    clock = ManualClock()
    root, _settings, adapter, queue, job = setup(tmp_path, wall=5.0, popen=fake, queue_clock=clock)
    callable_entered = threading.Event()
    allow_callable = threading.Event()
    callable_finished = threading.Event()
    cleanup_called = threading.Event()
    original_watch = queue._watch
    original_unload = adapter.worker.unload

    def tracked_unload() -> None:
        cleanup_called.set()
        original_unload()

    def delayed_watch(worker_fn: Any, watched_job: Job, deadline: float) -> Any:
        def delayed_callable(current_job: Job) -> Any:
            callable_entered.set()
            assert allow_callable.wait(timeout=3)
            try:
                return worker_fn(current_job)
            finally:
                callable_finished.set()

        return original_watch(delayed_callable, watched_job, deadline)

    queue._watch = delayed_watch  # type: ignore[method-assign]
    adapter.worker.unload = tracked_unload  # type: ignore[method-assign]
    outcomes: list[Exception] = []

    def render() -> None:
        try:
            adapter.render(request(mode="preview"), job, queue=queue)
        except Exception as exc:  # noqa: BLE001  # propagated from render thread
            outcomes.append(exc)

    render_thread = threading.Thread(target=render)
    render_thread.start()
    try:
        assert callable_entered.wait(timeout=2), "queue did not start its worker callback"
        clock.now = 6.0
        render_thread.join(timeout=3)
        assert not render_thread.is_alive(), "queue timeout did not return while callback was gated"
        assert cleanup_called.is_set(), "queue timeout did not unload its supervisor worker"
        assert outcomes and isinstance(outcomes[0], JobError)
        assert outcomes[0].kind is JobErrorKind.BUDGET_EXCEEDED
        assert not queue.lease._owned
        before = {
            p.relative_to(root.path).as_posix(): p.read_bytes()
            for p in root.path.rglob("*") if p.is_file()
        }
        allow_callable.set()
        assert callable_finished.wait(timeout=3), "timed-out queue callback did not exit"
    finally:
        allow_callable.set()
        render_thread.join(timeout=3)
    after = {
        p.relative_to(root.path).as_posix(): p.read_bytes()
        for p in root.path.rglob("*") if p.is_file()
    }
    assert fake.calls == [], "a Blender process launched after queue timeout returned"
    assert after == before, "the timed-out callback wrote or changed files after render returned"


def test_stopped_queue_callback_cannot_rearm_while_lease_release_is_pending(
    tmp_path: Path,
) -> None:
    fake = FakePopen()
    clock = ManualClock()
    root, _settings, adapter, queue, job = setup(tmp_path, wall=5.0, popen=fake, queue_clock=clock)
    adapter.render(request(), job, queue=queue)
    assert len(fake.calls) == 1
    calls_after_warmup = len(fake.calls)
    assert not queue.lease._owned
    job = Job(job_id="job-2", type="blender_render", shot_id="shot-1", run_id="run-2")
    callable_entered = threading.Event()
    allow_callable = threading.Event()
    callable_finished = threading.Event()
    unload_done = threading.Event()
    allow_unload_return = threading.Event()
    original_watch = queue._watch
    original_unload = adapter.worker.unload

    def delayed_watch(worker_fn: Any, watched_job: Job, deadline: float) -> Any:
        def delayed_callable(current_job: Job) -> Any:
            callable_entered.set()
            assert allow_callable.wait(timeout=3)
            try:
                return worker_fn(current_job)
            finally:
                callable_finished.set()

        return original_watch(delayed_callable, watched_job, deadline)

    def held_unload() -> None:
        original_unload()
        unload_done.set()
        assert allow_unload_return.wait(timeout=3)

    queue._watch = delayed_watch  # type: ignore[method-assign]
    adapter.worker.unload = held_unload  # type: ignore[method-assign]
    outcomes: list[Exception] = []

    def render() -> None:
        try:
            adapter.render(request(mode="preview", run_id="run-2"), job, queue=queue)
        except Exception as exc:  # noqa: BLE001  # propagated from render thread
            outcomes.append(exc)

    render_thread = threading.Thread(target=render)
    render_thread.start()
    try:
        assert callable_entered.wait(timeout=2), "queue did not start its worker callback"
        clock.now = 6.0
        assert unload_done.wait(timeout=3), "worker unload did not complete under the held lease"
        assert queue.lease._owned, "lease should remain owned until unload returns"
        before = {
            p.relative_to(root.path).as_posix(): p.read_bytes()
            for p in root.path.rglob("*") if p.is_file()
        }
        allow_callable.set()
        assert callable_finished.wait(timeout=3), "timed-out callback did not finish"
        during_release = {
            p.relative_to(root.path).as_posix(): p.read_bytes()
            for p in root.path.rglob("*") if p.is_file()
        }
        assert len(fake.calls) == calls_after_warmup, (
            "callback relaunched Blender after worker unload while lease remained owned"
        )
        assert during_release == before, "callback wrote files after worker unload while lease remained owned"
        allow_unload_return.set()
    finally:
        allow_callable.set()
        allow_unload_return.set()
        render_thread.join(timeout=3)
    assert not render_thread.is_alive()
    assert outcomes and isinstance(outcomes[0], JobError)
    assert outcomes[0].kind is JobErrorKind.BUDGET_EXCEEDED
    assert not queue.lease._owned
