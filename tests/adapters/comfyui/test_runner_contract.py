from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from orchestrator.adapters.comfyui.runner import (
    COMFYUI_STARTUP_ALLOWANCE_SECONDS,
    ComfyAdapter,
)
from orchestrator.adapters.comfyui.workflow import ImageRequest, ModelPin
from orchestrator.domain.entities import GenerationRun, Job
from orchestrator.jobs.lease import GpuLease, MemoryReading
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import parse_settings
from orchestrator.store.json_store import ProjectStore
from tests.adapters.comfyui.test_workflow_contract import object_info


@pytest.fixture(autouse=True)
def _cap_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda seconds: real_sleep(min(seconds, 0.001)))


class Nvml:
    def device_memory(self) -> MemoryReading:
        return MemoryReading(1200.0, 11940.0, 10740.0)

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

    def __init__(self, *, unkillable: bool = False) -> None:
        self.returncode: int | None = None
        self.unkillable = unkillable
        self.stopped = threading.Event()
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake-comfy", timeout=timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if not self.unkillable:
            self.returncode = -15
            self.stopped.set()

    def kill(self) -> None:
        self.killed = True
        if not self.unkillable:
            self.returncode = -9
            self.stopped.set()


class FakePopen:
    def __init__(self) -> None:
        self.unkillable = False
        self.calls: list[tuple[list[str], dict[str, Any], Child]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Child:
        assert kwargs.get("shell") in (False, None)
        assert kwargs.get("cwd") is not None
        child = Child(unkillable=self.unkillable)
        self.calls.append((argv, kwargs, child))
        return child

    @property
    def output_dir(self) -> Path:
        argv = self.calls[-1][0]
        return Path(argv[argv.index("--output-directory") + 1])


class FakeTransport:
    """Request protocol fake; output is materialized where SaveImage is authorized to write."""

    def __init__(self, root: ProjectRoot, popen: FakePopen, *, mode: str = "success") -> None:
        self.root, self.popen, self.mode = root, popen, mode
        self.calls: list[tuple[str, str, dict[str, object] | None, float]] = []
        self.submitted = threading.Event()
        self.history_available = True
        self.prompt_count = 0
        self.image_relative: str | None = None
        self.active_prompt_id: str | None = None

    def request(self, method: str, path: str, payload: dict[str, object] | None = None,
                *, timeout: float = 5.0) -> dict[str, object]:
        self.calls.append((method, path, payload, timeout))
        if method == "GET" and path == "/system_stats":
            argv = self.popen.calls[-1][0][3:]
            if self.mode == "wrong_endpoint":
                argv = list(argv)
                argv[argv.index("--base-directory") + 1] = str(self.root.path / "another-runtime")
            version = "0.44.0"
            if self.mode == "old_version" or (
                    self.mode == "version_regresses_after_prompt" and self.prompt_count):
                version = "0.21.1"
            return {"system": {"comfyui_version": version, "argv": argv}}
        if method == "GET" and path == "/object_info":
            return object_info()  # type: ignore[return-value]
        if method == "POST" and path == "/free":
            return {}
        if method == "POST" and path == "/interrupt":
            return {}
        if method == "POST" and path == "/queue":
            if payload and payload.get("delete") and not self.popen.unkillable:
                self.active_prompt_id = None
            return {}
        if method == "GET" and path == "/queue":
            running = ([[0, self.active_prompt_id, {}, {}]] if self.active_prompt_id is not None else [])
            return {"queue_running": running, "queue_pending": []}
        if method == "POST" and path == "/prompt":
            assert payload is not None
            graph = payload["prompt"]
            prefix = graph["13"]["inputs"]["filename_prefix"]
            self.prompt_count += 1
            self.submitted.set()
            self.active_prompt_id = f"prompt-{self.prompt_count}"
            if self.mode == "mutate_model":
                self.root.write_bytes("weights/diffusion/flux-2-klein-4b.safetensors",
                                      b"changed after pre-submit hashing")
            self.image_relative = f"{Path(prefix).as_posix()}_00001_.png"
            output_path = self.popen.output_dir / Path(self.image_relative)
            if self.mode == "zero_image":
                self.root.write_bytes(output_path.relative_to(self.root.path).as_posix(), b"")
            elif self.mode != "invalid_image":
                self.root.write_with(output_path.relative_to(self.root.path).as_posix(),
                                     lambda path: Image.new("RGB", (640, 480) if self.mode == "wrong_dimensions"
                                                            else (1280, 960), "#222").save(path))
            else:
                self.root.write_bytes(output_path.relative_to(self.root.path).as_posix(), b"not a png")
            return {"prompt_id": self.active_prompt_id}
        if method == "GET" and path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[-1]
            if not self.history_available:
                return {}
            if self.mode == "retry_cuda" and prompt_id == "prompt-1":
                return {prompt_id: {"status": {"status_str": "error", "completed": False,
                    "messages": [["execution_error", {"exception_message": "CUDA error: out of memory"}]]},
                    "outputs": {}}}
            if self.mode == "malformed_history":
                return []  # type: ignore[return-value]
            if self.mode == "incomplete_history":
                return {prompt_id: {"status": {"status_str": "error", "completed": False}, "outputs": {}}}
            if self.mode == "foreign_path":
                image = {"filename": "..\\..\\outside.png", "subfolder": "", "type": "output"}
            elif self.mode == "absolute_path":
                image = {"filename": "C:\\outside.png", "subfolder": "", "type": "output"}
            elif self.mode == "ads_path":
                image = {"filename": "image:stream.png", "subfolder": "", "type": "output"}
            elif self.mode == "bad_subfolder":
                image = {"filename": Path(self.image_relative or "missing.png").name,
                         "subfolder": "../../outside", "type": "output"}
            elif self.mode == "wrong_type":
                image = {"filename": Path(self.image_relative or "missing.png").name, "subfolder": "", "type": "temp"}
            else:
                image = {"filename": Path(self.image_relative or "missing.png").name,
                         "subfolder": Path(self.image_relative or "").parent.as_posix(), "type": "output"}
            if self.mode == "missing_output_node":
                return {prompt_id: {"status": {"status_str": "success", "completed": True}, "outputs": {}}}
            images = [] if self.mode == "no_images" else [image, image] if self.mode == "multiple_images" else [image]
            return {prompt_id: {"status": {"status_str": "success", "completed": True},
                                "outputs": {"13": {"images": images}}}}
        if method == "GET" and path.startswith("/view"):
            return {}
        raise AssertionError(f"unexpected transport request: {method} {path}")


def setup(tmp_path: Path, *, mode: str = "success", port: int = 8188,
          transport_type: type[FakeTransport] = FakeTransport, wall_seconds: float = 30.0,
          queue_clock: ManualClock | None = None, retries: int = 1,
          floor: float = 0.0):
    root = ProjectRoot(tmp_path)
    install = root.make_dir("fake-comfy")
    root.write_bytes("fake-comfy/python_embeded/python.exe", b"python shim")
    root.write_bytes("fake-comfy/ComfyUI/main.py", b"entry point")
    models: list[ModelPin] = []
    for role, filename in (("diffusion", "flux-2-klein-4b.safetensors"),
                            ("text_encoder", "qwen_3_4b.safetensors"),
                            ("vae", "flux2-vae.safetensors")):
        relative = f"weights/{role}/{filename}"
        data = (role + " fixture weight bytes").encode()
        model_path = root.write_bytes(relative, data)
        models.append(ModelPin(role=role, path=model_path, sha256=hashlib.sha256(data).hexdigest(),
                               source=f"https://models.example.invalid/{filename}", license_id="fixture-license"))
    settings = parse_settings({
        "project_root": str(root.path),
        "binaries": {"comfyui_root": str(install), "comfyui_port": port},
        "budget": {"per_shot_wall_clock_seconds": wall_seconds, "retries_per_shot": retries},
        "resources": {"throughput_floor_per_second": floor,
                      "vram_release_timeout_seconds": 0.05},
    })
    popen = FakePopen()
    transport = transport_type(root, popen, mode=mode)
    adapter = ComfyAdapter(root, settings, popen=popen, transport=transport)
    nvml = Nvml()
    lease = GpuLease(root, settings, nvml=nvml, worker=adapter.worker,
                     pid_alive=lambda _pid: True, clock=queue_clock or time.monotonic,
                     ttl_seconds=30.0)
    queue = JobQueue(root, settings, lease, ProjectStore(root), nvml=nvml, host=Host(),
                     quarantine=ProjectQuarantine(root), clock=queue_clock or time.monotonic)
    req = ImageRequest(shot_id="shot-1", shot_version=2, run_id="run-1",
                       prompt="A quiet corridor", degrade_seed=41, models=tuple(models))
    job = Job(job_id="job-1", type="flux_still", shot_id="shot-1", run_id="run-1")
    return root, settings, adapter, queue, job, req, popen, transport


def test_positive_floor_forwards_measured_startup_allowance(tmp_path: Path) -> None:
    _root, _settings, adapter, queue, job, req, popen, _transport = setup(
        tmp_path, floor=0.001)
    original_run = queue.run
    forwarded: list[float | None] = []

    def capture_run(*args: Any, **kwargs: Any) -> Any:
        forwarded.append(kwargs.get("startup_allowance_seconds"))
        return original_run(*args, **kwargs)

    queue.run = capture_run  # type: ignore[method-assign]
    result = adapter.generate(req, job, queue=queue)

    assert result.generation_run.status == "completed"
    assert len(popen.calls) == 1
    # Asserts the wiring, not the number: the allowance is a derived upper bound that
    # changes when the pre-progress cost is finally measured directly.
    assert forwarded == [COMFYUI_STARTUP_ALLOWANCE_SECONDS]


def test_success_retains_reopenable_png_graph_provenance_and_generation_run(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, popen, transport = setup(tmp_path)
    result = adapter.generate(req, job, queue=queue)
    assert len(popen.calls) == 1
    assert result.image_paths and result.image_paths[0].startswith("generated/shot-1/v0002/run-1/")
    assert all(not Path(value).is_absolute() for value in
               (*result.image_paths, result.workflow_path, result.metadata_path, result.log_path))
    assert result.generation_run.tool == "comfyui"
    assert result.generation_run.tool_version == "0.44.0"
    assert result.generation_run.seed is not None
    assert result.generation_run.status == "completed"
    assert json.loads(result.generation_run.invocation) == json.loads(
        root.resolve(result.workflow_path).read_text(encoding="utf-8"))
    assert any(value == 8383 for value in result.generation_run.settings.values())
    assert any(value == "prompt-1" for value in result.generation_run.settings.values())
    assert any(pin.sha256 in result.generation_run.settings.values() for pin in req.models)
    assert any(pin.license_id in result.generation_run.settings.values() for pin in req.models)
    assert result.workflow_path in result.generation_run.input_paths
    assert any(path in result.generation_run.output_paths for path in result.image_paths)
    for relative in (*result.image_paths, result.workflow_path, result.metadata_path):
        assert root.resolve(relative).is_file()
    with Image.open(root.resolve(result.image_paths[0])) as image:
        image.load()
        assert image.size == (1280, 960)
    adapter.close()
    transport.history_available = False
    retained_run = root.resolve(result.metadata_path).parent / "generation-run.json"
    reopened = GenerationRun.model_validate_json(retained_run.read_text(encoding="utf-8"))
    assert reopened.tool == "comfyui"
    assert popen.calls[0][2].poll() is not None, "close must confirm owned child shutdown"
    metadata = json.loads(root.resolve(result.metadata_path).read_text(encoding="utf-8"))
    flattened: list[object] = []

    def flatten(value: object) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                flatten(nested)
        elif isinstance(value, list):
            for nested in value:
                flatten(nested)
        else:
            flattened.append(value)

    flatten(metadata)
    assert all(pin.sha256 in flattened and pin.source in flattened and pin.license_id in flattened
               for pin in req.models)
    assert any(isinstance(value, str) and len(value) == 64 for value in flattened)
    image_hash = hashlib.sha256(root.resolve(result.image_paths[0]).read_bytes()).hexdigest()
    assert image_hash in flattened
    workflow = json.loads(root.resolve(result.workflow_path).read_text(encoding="utf-8"))
    assert workflow["13"]["inputs"]["filename_prefix"]
    assert any(path == "/free" for _method, path, _payload, _timeout in transport.calls)
    assert queue.lease._owned is False


@pytest.mark.parametrize("mode", ["foreign_path", "absolute_path", "ads_path", "bad_subfolder",
                                    "wrong_type", "invalid_image", "zero_image", "wrong_dimensions",
                                    "missing_output_node", "no_images", "multiple_images", "malformed_history"])
def test_untrusted_or_invalid_history_never_publishes_success(tmp_path: Path, mode: str) -> None:
    root, _settings, adapter, queue, job, req, _popen, _transport = setup(tmp_path, mode=mode)
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert not any(root.resolve("generated").rglob("generation-run.json"))
    assert queue.lease._owned is False


def test_incomplete_history_never_publishes_success(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, _popen, _transport = setup(tmp_path, mode="incomplete_history")
    with pytest.raises(JobError):
        adapter.generate(req, job, queue=queue)
    assert not any(root.resolve("generated").rglob("generation-run.json"))


def test_pre_cancel_is_terminal_and_does_not_launch_or_submit(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, popen, transport = setup(tmp_path)
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue, cancel=lambda: True)
    assert caught.value.kind is JobErrorKind.CANCELLED
    assert popen.calls == []
    assert not any(path == "/prompt" for _method, path, _payload, _timeout in transport.calls)
    assert queue.lease._owned is False
    assert list(root.resolve("quarantine").rglob("*run-1"))


def test_cancel_after_prompt_interrupts_deletes_active_prompt_and_quarantines(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, _popen, transport = setup(tmp_path)
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue, cancel=lambda: job.progress >= 0.1)
    assert caught.value.kind is JobErrorKind.CANCELLED
    assert any(method == "POST" and path == "/interrupt" and 0 < timeout <= 5.0
               for method, path, _payload, timeout in transport.calls)
    assert any(method == "POST" and path == "/queue" and payload == {"delete": ["prompt-1"]}
               for method, path, payload, _timeout in transport.calls)
    assert queue.lease._owned is False
    assert list(root.resolve("quarantine").rglob("attempt-*"))


def test_unconfirmed_owned_child_termination_keeps_real_lease_held(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, popen, transport = setup(tmp_path)
    popen.unkillable = True
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue, cancel=lambda: job.progress >= 0.1)
    assert caught.value.kind is JobErrorKind.WORKER_UNREACHABLE
    assert popen.calls and popen.calls[0][2].poll() is None
    assert popen.calls[0][2].terminated and popen.calls[0][2].killed
    assert queue.lease._owned, "lease must remain held while an owned process may still be running"
    assert any(method == "POST" and path == "/queue" and payload == {"delete": ["prompt-1"]}
               for method, path, payload, _timeout in transport.calls)
    assert not any(root.resolve("generated").rglob("generation-run.json"))


class GatedPromptTransport(FakeTransport):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.prompt_entered = threading.Event()
        self.allow_prompt_response = threading.Event()

    def request(self, method: str, path: str, payload: dict[str, object] | None = None,
                *, timeout: float = 5.0) -> dict[str, object]:
        if method == "POST" and path == "/prompt":
            self.prompt_entered.set()
            assert self.allow_prompt_response.wait(timeout=5)
        return super().request(method, path, payload, timeout=timeout)


def test_late_prompt_response_after_cancel_is_fenced_and_joined(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, popen, gate = setup(
        tmp_path, transport_type=GatedPromptTransport)
    cancelled = threading.Event()
    outcome: list[JobError] = []

    def run() -> None:
        try:
            adapter.generate(req, job, queue=queue, cancel=cancelled.is_set)
        except JobError as exc:  # captured for assertion on the calling thread
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert gate.prompt_entered.wait(timeout=2)
        second_job = Job(job_id="job-2", type="flux_still", shot_id="shot-1", run_id="run-2")
        with pytest.raises(RuntimeError):
            adapter.generate(req.model_copy(update={"run_id": "run-2"}), second_job, queue=queue)
        assert not root.resolve("generated/shot-1/v0002/run-2").exists()
        cancelled.set()
        assert popen.calls[0][2].stopped.wait(timeout=2), "unknown-ID cancellation did not terminate owned child"
        gate.allow_prompt_response.set()
        thread.join(timeout=3)
    finally:
        gate.allow_prompt_response.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], JobError)
    assert outcome[0].kind is JobErrorKind.CANCELLED
    assert not any(root.resolve("generated").rglob("generation-run.json"))
    assert popen.calls[0][2].poll() is not None
    assert not queue.lease._owned


def test_cuda_failure_retries_through_job_queue_and_retains_both_attempts(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, popen, transport = setup(tmp_path, mode="retry_cuda")
    result = adapter.generate(req, job, queue=queue)
    attempts = sorted(path.name for path in root.resolve("generated/shot-1/v0002/run-1").glob("attempt-*"))
    assert attempts == ["attempt-0001", "attempt-0002"]
    assert "/attempt-0002/" in result.metadata_path.replace("\\", "/")
    assert transport.prompt_count == 2
    assert len(popen.calls) >= 2
    assert popen.calls[0][2].poll() is not None
    assert not queue.lease._owned


def test_model_identity_is_rechecked_after_generation(tmp_path: Path) -> None:
    _root, _settings, adapter, queue, job, req, _popen, _transport = setup(tmp_path, mode="mutate_model")
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert not queue.lease._owned


def test_whole_generate_deadline_stops_child_and_does_not_publish_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    root, _settings, adapter, queue, job, req, popen, transport = setup(
        tmp_path, wall_seconds=5.0, queue_clock=clock)
    import orchestrator.adapters.comfyui.runner as runner_module
    monkeypatch.setattr(runner_module.time, "monotonic", clock)
    original = transport.request

    def slow_schema(method: str, path: str, payload: dict[str, object] | None = None,
                    *, timeout: float = 5.0) -> dict[str, object]:
        if method == "GET" and path == "/object_info":
            clock.now = 6.0
        return original(method, path, payload, timeout=timeout)

    transport.request = slow_schema  # type: ignore[method-assign]
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue)
    assert caught.value.kind is JobErrorKind.BUDGET_EXCEEDED
    assert popen.calls
    assert (popen.calls[0][2].poll() is not None
            or any(method == "POST" and path == "/free" for method, path, _payload, _timeout in transport.calls))
    assert not queue.lease._owned
    assert not any(root.resolve("generated").rglob("generation-run.json"))


def test_late_callable_cannot_begin_after_deadline_or_write_during_release(tmp_path: Path) -> None:
    clock = ManualClock()
    root, _settings, adapter, queue, job, req, popen, _transport = setup(
        tmp_path, wall_seconds=5.0, queue_clock=clock)
    entered = threading.Event()
    allow_begin = threading.Event()
    callable_finished = threading.Event()
    unload_entered = threading.Event()
    allow_unload = threading.Event()
    original_watch = queue._watch
    original_unload = adapter.worker.unload

    def delayed_watch(worker_fn: Any, watched_job: Job, deadline: float) -> Any:
        def gated(current_job: Job) -> Any:
            entered.set()
            assert allow_begin.wait(timeout=3)
            try:
                return worker_fn(current_job)
            finally:
                callable_finished.set()
        return original_watch(gated, watched_job, deadline)

    def held_unload() -> None:
        original_unload()
        unload_entered.set()
        assert allow_unload.wait(timeout=3)

    queue._watch = delayed_watch  # type: ignore[method-assign]
    adapter.worker.unload = held_unload  # type: ignore[method-assign]
    outcome: list[JobError] = []

    def run() -> None:
        try:
            adapter.generate(req, job, queue=queue)
        except JobError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(timeout=2)
        clock.now = 6.0
        assert unload_entered.wait(timeout=3), "lease release did not reach worker unload after deadline"
        before = {p.relative_to(root.path).as_posix(): p.read_bytes()
                  for p in root.path.rglob("*") if p.is_file()}
        allow_begin.set()
        assert callable_finished.wait(timeout=3)
        during_release = {p.relative_to(root.path).as_posix(): p.read_bytes()
                          for p in root.path.rglob("*") if p.is_file()}
        assert not popen.calls, "expired queue callback launched ComfyUI after the deadline"
        assert during_release == before, "late callback wrote artifacts while unload held the lease"
        allow_unload.set()
    finally:
        allow_begin.set()
        allow_unload.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], JobError)
    assert outcome[0].kind is JobErrorKind.BUDGET_EXCEEDED
    assert not queue.lease._owned


def test_telemetry_write_tail_is_joined_before_lease_release_and_never_publishes_success(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    root, _settings, adapter, queue, job, req, _popen, _transport = setup(
        tmp_path, wall_seconds=5.0, queue_clock=clock)
    telemetry_entered = threading.Event()
    allow_write = threading.Event()
    unload_finished = threading.Event()
    release_finished = threading.Event()
    generation_returned = threading.Event()
    original_write_text = root.write_text
    original_unload = adapter.worker.unload
    original_release = queue.lease.release

    def gated_write(relative: str, text: str, *, encoding: str = "utf-8") -> Path:
        lower = text.lower()
        telemetry_payload = re.search(r"(?:vram.{0,24}peak|peak.{0,24}vram|peak_vram_mb)", lower)
        if Path(relative).name == "metadata.json" and telemetry_payload:
            telemetry_entered.set()
            assert allow_write.wait(timeout=3)
        return original_write_text(relative, text, encoding=encoding)

    def tracked_unload() -> None:
        original_unload()
        unload_finished.set()

    def tracked_release(*args: Any, **kwargs: Any) -> None:
        try:
            original_release(*args, **kwargs)
        finally:
            release_finished.set()

    root.write_text = gated_write  # type: ignore[method-assign]
    adapter.worker.unload = tracked_unload  # type: ignore[method-assign]
    queue.lease.release = tracked_release  # type: ignore[method-assign]
    outcome: list[JobError] = []

    def run() -> None:
        try:
            adapter.generate(req, job, queue=queue)
        except JobError as exc:
            outcome.append(exc)
        finally:
            generation_returned.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert telemetry_entered.wait(timeout=3), "adapter did not persist measured worker telemetry"
        clock.now = 6.0
        # Unload may begin while the callback is gated. The persisted lease must remain held
        # until the callback joins and release finishes.
        release_finished.wait(timeout=0.1)
        assert not release_finished.is_set(), "lease release finished while telemetry callback was still writing"
        assert queue.lease._owned, "persisted lease was dropped while telemetry callback was still writing"
        assert not generation_returned.wait(timeout=0.1), "generate returned while telemetry writer was still active"
        allow_write.set()
        thread.join(timeout=3)
    finally:
        allow_write.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], JobError)
    assert outcome[0].kind is JobErrorKind.BUDGET_EXCEEDED
    assert not queue.lease._owned
    assert not any(root.resolve("generated").rglob("generation-run.json"))


@pytest.mark.parametrize("mismatch", ["shot", "run"])
def test_correlated_job_mismatch_fails_before_reservation_or_launch(tmp_path: Path, mismatch: str) -> None:
    root, _settings, adapter, queue, job, req, popen, _transport = setup(tmp_path)
    if mismatch == "shot":
        job.shot_id = "other"
    else:
        job.run_id = "other"
    with pytest.raises((ValueError, JobError)):
        adapter.generate(req, job, queue=queue)
    assert popen.calls == []
    assert not root.resolve("generated").exists()


def test_queue_must_share_adapter_worker_settings_and_project_root(tmp_path: Path) -> None:
    root, settings, adapter, queue, job, req, popen, _transport = setup(tmp_path)
    queue.settings = settings.model_copy(deep=True)
    with pytest.raises((ValueError, JobError)):
        adapter.generate(req, job, queue=queue)
    assert popen.calls == []
    assert not root.resolve("generated").exists()


def test_queue_must_use_adapter_worker_before_reservation(tmp_path: Path) -> None:
    root, settings, adapter, _queue, job, req, popen, _transport = setup(tmp_path)
    nvml = Nvml()

    class OtherWorker:
        def pid(self) -> int:
            return 4242

        def unload(self) -> None:
            pass

        def terminate(self) -> bool:
            return True

        def respawn(self) -> int:
            return 4243

    lease = GpuLease(root, settings, nvml=nvml, worker=OtherWorker())
    queue = JobQueue(root, settings, lease, ProjectStore(root), nvml=nvml, host=Host(),
                     quarantine=ProjectQuarantine(root))
    with pytest.raises((ValueError, JobError)):
        adapter.generate(req, job, queue=queue)
    assert popen.calls == []
    assert not root.resolve("generated").exists()


def test_queue_must_share_adapter_root_before_reservation(tmp_path: Path) -> None:
    root, settings, adapter, _queue, job, req, popen, _transport = setup(tmp_path)
    other_root = ProjectRoot(root.make_dir("other-project"))
    nvml = Nvml()
    lease = GpuLease(other_root, settings, nvml=nvml, worker=adapter.worker)
    queue = JobQueue(other_root, settings, lease, ProjectStore(other_root), nvml=nvml, host=Host(),
                     quarantine=ProjectQuarantine(other_root))
    with pytest.raises((ValueError, JobError)):
        adapter.generate(req, job, queue=queue)
    assert popen.calls == []
    assert not root.resolve("generated").exists()


def test_model_hash_mismatch_is_rejected_before_submission(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, _popen, transport = setup(tmp_path)
    bad_pin = ModelPin(role="diffusion", path=req.models[0].path, sha256="b" * 64,
                       source=req.models[0].source, license_id=req.models[0].license_id)
    bad_req = req.model_copy(update={"models": (bad_pin, *req.models[1:])})
    with pytest.raises(JobError) as caught:
        adapter.generate(bad_req, job, queue=queue)
    assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    assert not any(path == "/prompt" for _method, path, _payload, _timeout in transport.calls)
    assert not any(root.resolve("generated").rglob("generation-run.json"))
    logs = list(root.resolve("generated").rglob("job.jsonl"))
    assert logs, "hash mismatch must retain failed attempt evidence"
    assert all("job-1" in path.read_text(encoding="utf-8") for path in logs)


def test_existing_run_is_exclusive_and_never_overwritten(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, popen, _transport = setup(tmp_path)
    first = adapter.generate(req, job, queue=queue)
    before = {path.relative_to(root.path).as_posix(): path.read_bytes()
              for path in root.resolve("generated/shot-1/v0002/run-1").rglob("*") if path.is_file()}
    with pytest.raises(FileExistsError):
        adapter.generate(req, Job(job_id="job-2", type="flux_still", shot_id="shot-1", run_id="run-1"),
                         queue=queue)
    after = {path.relative_to(root.path).as_posix(): path.read_bytes()
             for path in root.resolve("generated/shot-1/v0002/run-1").rglob("*") if path.is_file()}
    assert after == before
    assert first.image_paths
    assert len(popen.calls) == 1


def test_occupied_port_is_rejected_before_owned_child_launch(tmp_path: Path) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    probe.listen()
    port = probe.getsockname()[1]
    try:
        _root, _settings, adapter, queue, job, req, popen, _transport = setup(tmp_path, port=port, retries=2)
        with pytest.raises(JobError) as caught:
            adapter.generate(req, job, queue=queue)
        assert caught.value.kind is JobErrorKind.WORKER_UNREACHABLE
        assert popen.calls == []
    finally:
        probe.close()


def test_readiness_rejects_wrong_runtime_endpoint_without_mutating_it(tmp_path: Path) -> None:
    root, _settings, adapter, queue, job, req, popen, transport = setup(
        tmp_path, mode="wrong_endpoint", retries=2)
    with pytest.raises(JobError) as caught:
        adapter.generate(req, job, queue=queue)
    assert caught.value.kind is JobErrorKind.WORKER_UNREACHABLE
    assert not any(method == "POST" and path == "/prompt" for method, path, _payload, _timeout in transport.calls)
    assert not any(method == "POST" and path in {"/free", "/interrupt", "/queue"}
                   for method, path, _payload, _timeout in transport.calls)
    assert popen.calls and all(call[2].poll() is not None for call in popen.calls)
    assert not any(root.resolve("generated").rglob("generation-run.json"))


@pytest.mark.parametrize("mode", ["old_version", "version_regresses_after_prompt"])
def test_server_reporting_the_superseded_0_21_1_is_refused(tmp_path: Path, mode: str) -> None:
    """A4.1: 0.44.0 is required at boot readiness and again after completion."""
    root, _settings, adapter, queue, job, req, popen, transport = setup(tmp_path, mode=mode, retries=1)
    with pytest.raises(JobError):
        adapter.generate(req, job, queue=queue)
    assert not any(root.resolve("generated").rglob("generation-run.json"))
    if mode == "old_version":
        assert not any(method == "POST" and path in {"/prompt", "/free", "/interrupt", "/queue"}
                       for method, path, _payload, _timeout in transport.calls)
        assert popen.calls and all(call[2].poll() is not None for call in popen.calls)
        assert not queue.lease._owned
    else:
        # Post-completion refusal: the contract does not say whether the lease is released
        # (the implementation holds it while the endpoint is untrusted); only the child's
        # confirmed stop is asserted here.
        assert popen.calls and all(call[2].poll() is not None for call in popen.calls)
