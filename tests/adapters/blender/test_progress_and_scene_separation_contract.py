"""Render progress and scene/render separation contract regressions."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orchestrator.adapters.blender.runner import BlenderAdapter
from orchestrator.adapters.blender.scene import build_scene_plan
from orchestrator.domain.entities import (
    CameraProfile,
    Episode,
    Project,
    Shot,
    ShotOutputKind,
)
from orchestrator.jobs.lease import GpuLease, MemoryReading
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.platform.settings import parse_settings
from orchestrator.service.runner import (
    AdapterKind,
    AdapterRegistry,
    AdapterRequest,
    JobRunner,
    _AdapterShim,
)
from orchestrator.store.json_store import ProjectStore
from tests.adapters.blender.test_scene_contract import request as render_request
from tests.adapters.blender.test_supervision_contract import setup


class _SlowRenderProcess:
    """Popen-shaped fake that emits one planned frame per monitor poll."""

    pid = 8383

    def __init__(
        self,
        *,
        root: ProjectRoot,
        plan: dict[str, Any],
        progress_probe: Callable[[], float],
    ) -> None:
        self.root = root
        self.plan = plan
        self.frames = list(plan["outputs"]["frames"])
        self.progress_probe = progress_probe
        self.progress_samples: list[float] = []
        self.poll_count = 0
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._write_static_outputs()

    def _relative(self, absolute: str) -> str:
        return Path(absolute).resolve().relative_to(self.root.path).as_posix()

    def _write_static_outputs(self) -> None:
        outputs = self.plan["outputs"]
        metadata = {
            "schema_version": 1,
            "shot_id": self.plan["shot_id"],
            "shot_version": self.plan["shot_version"],
            "run_id": self.plan["run_id"],
            "tool_version": "5.2.0",
            "build_hash": "fake-build",
            "actual_device": self.plan["gpu_device"],
            "applied_settings": {
                "engine": {
                    "workbench": "BLENDER_WORKBENCH",
                    "eevee": "BLENDER_EEVEE",
                    "cycles": "CYCLES",
                }[self.plan["engine"]],
                "width": self.plan["width"],
                "height": self.plan["height"],
                "fps": self.plan["fps"],
                "samples": self.plan["samples"],
            },
            "geometry_fingerprint": "a" * 64,
            "child_pid": self.pid,
            "elapsed_seconds": 0.01,
            "status": "completed",
        }
        self.root.write_text(self._relative(outputs["metadata"]), json.dumps(metadata))
        self.root.write_bytes(self._relative(outputs["blend"]), b"BLENDER-v300")

    def poll(self) -> int | None:
        self.progress_samples.append(self.progress_probe())
        if self.returncode is not None:
            return self.returncode

        self.poll_count += 1
        if self.poll_count <= len(self.frames):
            frame_index = self.poll_count - 1
            relative = self._relative(self.frames[frame_index])
            magic = b"v/1\x01" if relative.endswith(".exr") else b"\x89PNG\r\n\x1a\n"
            self.root.write_bytes(relative, magic + b"simulated frame data")

            # Rewriting a completed frame must not reduce the completed-file count.
            if self.poll_count == 10:
                first = self._relative(self.frames[0])
                self.root.write_bytes(first, magic + b"rewritten frame data")

            # Keep the process alive after an extra in-directory file appears so the
            # adapter has time to sample it. Progress remains capped at 1.0.
            if self.poll_count == len(self.frames):
                frame_dir = Path(relative).parent.as_posix()
                self.root.write_bytes(
                    f"{frame_dir}/frame_999999.exr",
                    b"v/1\x01extra simulated frame",
                )
            return None

        if self.poll_count <= len(self.frames) + 3:
            return None
        self.returncode = 0
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake-blender", timeout=timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _SlowRenderPopen:
    def __init__(self, *, emit_log_marker: bool = False) -> None:
        self.root: ProjectRoot | None = None
        self.progress_probe: Callable[[], float] = lambda: 0.0
        self.emit_log_marker = emit_log_marker
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.plan: dict[str, Any] | None = None
        self.child: _SlowRenderProcess | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> _SlowRenderProcess:
        assert self.root is not None, "fake subprocess was not given its contained project root"
        stdout = kwargs.get("stdout")
        assert stdout is not None and hasattr(stdout, "write"), (
            "Blender stdout was not wired to the expected log stream"
        )
        plan = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        self.plan = plan
        if self.emit_log_marker:
            # This misleading log marker makes file counting observable: the contract
            # explicitly chooses completed frames over parsing Blender output.
            stdout.write(b"CCTV_PROGRESS 0.9\n")
            stdout.flush()
        self.child = _SlowRenderProcess(
            root=self.root,
            plan=plan,
            progress_probe=self.progress_probe,
        )
        self.calls.append((argv, kwargs))
        return self.child


def _track_lease_acquires(queue: JobQueue) -> list[float]:
    calls: list[float] = []
    original = queue.lease.acquire

    def tracked(timeout_seconds: float) -> None:
        calls.append(timeout_seconds)
        original(timeout_seconds)

    queue.lease.acquire = tracked  # type: ignore[method-assign]
    return calls


def test_slow_frame_render_reports_monotonic_bounded_progress_under_positive_floor(
    tmp_path: Path,
) -> None:
    fake = _SlowRenderPopen()
    root, settings, adapter, queue, job = setup(
        tmp_path,
        floor=0.1,
        popen=fake,
    )
    fake.root = root
    fake.progress_probe = lambda: job.progress
    acquire_calls = _track_lease_acquires(queue)

    # Shorten only the startup window for this regression. With no first progress,
    # this render's >3 second fake run must hit BUDGET_EXCEEDED at 2 seconds.
    # The real JobQueue.run/_watch/transitions still execute with a positive floor.
    real_run = queue.run

    def run_with_bounded_startup(*args: Any, **kwargs: Any) -> Any:
        kwargs["startup_allowance_seconds"] = 2.0
        return real_run(*args, **kwargs)

    queue.run = run_with_bounded_startup  # type: ignore[method-assign]
    profile = render_request().camera_profile.model_copy(update={"fps": 12.0})
    request = render_request(duration_sec=1.01, camera_profile=profile)

    result = adapter.render(request, job, queue=queue)

    assert result.generation_run.status == "completed", (
        f"a render with frame-file progress failed under floor 0.1/sec: {job.error_kind} "
        f"{job.error_message}"
    )
    assert job.state == "completed", f"real JobQueue left render in state {job.state!r}"
    assert settings.resources.throughput_floor_per_second == 0.1, (
        "the watchdog floor must stay nonzero for this regression"
    )
    assert fake.plan is not None and fake.plan["fps"] == 60
    assert fake.plan["frame_end"] == 61, (
        f"1.01 seconds at the 60 fps master should plan 61 frames, got {fake.plan!r}"
    )
    assert request.camera_profile.fps == 12.0, "the test must distinguish the post-only profile rate"
    assert fake.child is not None
    samples = fake.child.progress_samples
    assert any(0.0 < value < 1.0 for value in samples), (
        f"no intermediate progress was observed while the fake process ran: {samples!r}"
    )
    assert samples == sorted(samples), f"progress moved backwards when a frame was rewritten: {samples!r}"
    assert all(value <= 1.0 for value in samples), f"progress exceeded 1.0 with an extra frame: {samples!r}"
    first_positive = next(value for value in samples if value > 0.0)
    assert first_positive == pytest.approx(1.0 / 61), (
        f"one of 61 master frames should report 1/61, got {first_positive}; "
        "using CameraProfile.fps=12 would incorrectly report 1/12"
    )
    assert job.progress == 1.0, f"completed render progress should be 1.0, got {job.progress}"
    assert acquire_calls, "a Blender render completed without taking the GPU lease"


def test_render_ignores_stdout_progress_marker_and_counts_the_frame_file(
    tmp_path: Path,
) -> None:
    fake = _SlowRenderPopen(emit_log_marker=True)
    root, _settings, adapter, queue, job = setup(tmp_path, floor=0.1, popen=fake)
    fake.root = root
    fake.progress_probe = lambda: job.progress
    real_run = queue.run

    def run_with_bounded_startup(*args: Any, **kwargs: Any) -> Any:
        kwargs["startup_allowance_seconds"] = 2.0
        return real_run(*args, **kwargs)

    queue.run = run_with_bounded_startup  # type: ignore[method-assign]

    adapter.render(render_request(duration_sec=1 / 60), job, queue=queue)

    assert fake.plan is not None and fake.plan["frame_end"] == 1
    assert fake.child is not None
    first_positive = next(value for value in fake.child.progress_samples if value > 0.0)
    assert first_positive == pytest.approx(1.0), (
        f"one completed frame should report 1.0 regardless of CCTV_PROGRESS 0.9 in stdout; "
        f"observed {first_positive}"
    )


def test_missing_plan_frame_total_fails_with_a_named_reason_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.adapters.blender.runner as runner_module

    launches: list[list[str]] = []

    def forbidden_popen(argv: list[str], **_kwargs: Any) -> Any:
        launches.append(argv)
        raise AssertionError("unexpected Blender launch without frame_end")

    _root, _settings, adapter, queue, job = setup(tmp_path, popen=forbidden_popen)
    original_plan = runner_module.build_scene_plan

    def plan_without_frame_total(request):
        plan = original_plan(request)
        del plan["frame_end"]
        return plan

    monkeypatch.setattr(runner_module, "build_scene_plan", plan_without_frame_total)

    with pytest.raises(JobError) as caught:
        adapter.render(render_request(), job, queue=queue)

    assert "frame_end" in str(caught.value).lower(), (
        f"progress setup failed without naming missing frame_end: {caught.value!r}"
    )
    assert launches == [], "a render launched even though its plan had no progress total"


class _CountingNvml:
    def __init__(self) -> None:
        self.device_memory_calls = 0
        self.process_usage_calls = 0

    def device_memory(self) -> MemoryReading:
        self.device_memory_calls += 1
        return MemoryReading(1300, 12227, 10927)

    def process_usage(self) -> list[object]:
        self.process_usage_calls += 1
        return []


class _Host:
    def free_ram_gb(self) -> float:
        return 20.0

    def free_disk_gb(self, _path: Path) -> float:
        return 100.0


def test_build_scene_persists_scene_output_without_subprocess_or_gpu_lease(
    tmp_path: Path,
) -> None:
    root = ProjectRoot(tmp_path)
    blender_exe = root.write_bytes("fake-blender.exe", b"not an executable")
    settings = parse_settings({
        "project_root": str(root.path),
        "binaries": {"blender_exe": str(blender_exe)},
        "resources": {
            "vram_release_timeout_seconds": 0.05,
            "throughput_floor_per_second": 0.1,
            "startup_allowance_seconds": 2.0,
        },
    })
    store = ProjectStore(root)
    store.save(Project(project_id="p1", title="scene contract"), expected_version=0)
    store.save(Episode(episode_id="ep1", project_id="p1", number=1,
                       duration_target_sec=10), expected_version=0)
    camera = CameraProfile(camera_id="cam", profile_name="Fixture camera")
    store.create(camera)
    store.create(Shot(
        shot_id="shot-1",
        episode_id="ep1",
        camera_profile=camera.camera_id,
        duration_sec=1.01,
    ))
    nvml = _CountingNvml()
    launches: list[list[str]] = []

    def forbidden_popen(argv: list[str], **_kwargs: Any) -> Any:
        launches.append(argv)
        raise AssertionError("build_scene must not launch Blender")

    adapter = BlenderAdapter(root, settings, popen=forbidden_popen)
    lease = GpuLease(root, settings, nvml=nvml, worker=adapter.worker,
                     pid_alive=lambda _pid: True, ttl_seconds=30)
    queue = JobQueue(root, settings, lease, store, nvml=nvml, host=_Host(),
                     quarantine=ProjectQuarantine(root))
    acquire_calls = _track_lease_acquires(queue)
    payload = render_request(
        shot_id="shot-1",
        shot_version=1,
        run_id="scene-run-1",
        duration_sec=1.01,
    )
    registry = AdapterRegistry({
        AdapterKind.SCENE: _AdapterShim(AdapterKind.SCENE, adapter, settings),
    })
    runner = JobRunner(
        lambda _project_id: store,
        settings,
        queue_for=lambda _project_id: queue,
        adapters=registry,
    )

    try:
        submitted = runner.submit(
            "p1",
            AdapterRequest(
                kind=AdapterKind.SCENE,
                payload=payload,
                record_id="shot-1",
                record_version=1,
                episode_id="ep1",
            ),
        )
        completed = runner.wait("p1", submitted.job_id)

        assert launches == [], f"build_scene launched a subprocess: {launches!r}"
        assert acquire_calls == [], f"build_scene acquired the GPU lease {len(acquire_calls)} time(s)"
        assert nvml.device_memory_calls == 0 and nvml.process_usage_calls == 0, (
            "CPU-only scene assembly queried GPU state instead of using CPU admission"
        )
        assert completed.state == "completed", (
            f"build_scene did not complete: {completed.error_kind} {completed.error_message}"
        )

        outputs = store.list_shot_outputs("shot-1", episode_id="ep1")
        assert len(outputs) == 1, f"expected one persisted scene output, got {outputs!r}"
        output = outputs[0]
        assert output.kind is ShotOutputKind.SCENE, f"expected scene output kind, got {output.kind!r}"
        artifact = root.resolve(output.relative_path)
        assert artifact.is_file(), f"scene output receipt points to no artifact: {output.relative_path}"
        artifact_json = json.loads(root.read_text(output.relative_path, encoding="utf-8"))
        # The artifact is versioned like every other record here; the plan lives under
        # "plan", and plan["scene"] is the SceneSpec -- it is not duplicated alongside.
        assert artifact_json["schema_version"] == 1
        persisted_plan = artifact_json["plan"]
        expected_plan = build_scene_plan(payload)
        assert persisted_plan["scene"] == expected_plan["scene"], (
            "scene artifact did not persist the request's SceneSpec"
        )
        assert persisted_plan["frame_end"] == expected_plan["frame_end"], (
            "scene artifact did not persist the deterministic scene plan"
        )
    finally:
        runner.close()
