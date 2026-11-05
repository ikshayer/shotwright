"""Adapter integration test fixtures, authored from ADAPTER_INTEGRATION_CONTRACT.md."""

from __future__ import annotations

import threading
import json
from pathlib import Path

import pytest

from orchestrator.domain.entities import Job
from orchestrator.jobs.lease import GpuLease, MemoryReading
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore


class FakeNvml:
    def device_memory(self) -> MemoryReading:
        return MemoryReading(used_mb=1200, total_mb=11940, free_mb=10740)

    def process_usage(self):
        return []


class FakeWorker:
    def __init__(self) -> None:
        self._pid = 4242

    def pid(self) -> int:
        return self._pid

    def unload(self) -> None:
        pass

    def terminate(self) -> bool:
        return True

    def respawn(self) -> int:
        self._pid += 1
        return self._pid


class FakeHost:
    def __init__(self) -> None:
        self.ram_gb = 20.0
        self.disk_gb = 100.0

    def free_ram_gb(self) -> float:
        return self.ram_gb

    def free_disk_gb(self, _path: Path) -> float:
        return self.disk_gb


class FakeAdapter:
    def __init__(self, *, result_path: str = "renders/s1/master.mkv") -> None:
        self.result_path = result_path
        self.entered = threading.Event()
        self.release = threading.Event()
        self.started = False
        self.invocations = 0
        self.requests = []
        self.second_entered = threading.Event()
        self.closed = False
        self.active = 0
        self.max_active = 0
        self.raise_error: BaseException | None = None
        self.cancel_before_start = False
        self.root: ProjectRoot | None = None
        self.lease_is_owned = lambda: False
        self.lease_states_during_invoke: list[bool] = []
        self.degrade_kind = None
        self.degrade_entered = threading.Event()
        self.result_value = _USE_DEFAULT_VALUE

    def invoke(self, request, job: Job, *, queue: JobQueue):
        if self.cancel_before_start:
            raise AssertionError("adapter invoked after pre-start cancellation")
        self.started = True
        self.invocations += 1
        if self.invocations >= 2:
            self.second_entered.set()
        self.requests.append(request)
        self.lease_states_during_invoke.append(self.lease_is_owned())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        is_degrade = request.kind == self.degrade_kind
        if is_degrade:
            self.degrade_entered.set()
        try:
            if not is_degrade and not self.release.wait(3):
                raise AssertionError("test did not release fake adapter")
            if self.raise_error is not None:
                raise self.raise_error
            if self.root is not None:
                suffix = self.invocations
                artifact_path = (
                    f"post/s1/v0001/degraded-{suffix}.mp4" if is_degrade
                    else f"renders/s1/master-{suffix}.mkv"
                )
                self.root.write_bytes(artifact_path, f"fake retained artifact {suffix}".encode())
            else:
                artifact_path = self.result_path
            from orchestrator.service.runner import AdapterResult
            if self.result_value is not _USE_DEFAULT_VALUE:
                value = self.result_value
            elif is_degrade:
                value = {
                    "profile_semver": "1.0.0",
                    "filter_graph_hash": "a" * 64,
                    "pinned_binaries": {"ffmpeg": "fake-build"},
                }
            elif request.kind.value == "generate":
                # ShotOutput STILL has no generation provenance fields.
                value = {}
            else:
                value = {"engine_id": "BLENDER_EEVEE", "engine_build": "fake-build"}
            return AdapterResult(
                artifact_path=artifact_path,
                value=value,
            )
        finally:
            self.active -= 1

    def close(self) -> None:
        self.closed = True


class FakeRegistry:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter
        self.closed = False

    def get(self, _kind):
        return self.adapter

    def close(self) -> None:
        self.closed = True
        self.adapter.close()


_USE_DEFAULT_VALUE = object()


class Environment:
    def __init__(self, path: Path) -> None:
        from orchestrator.service.runner import AdapterKind, AdapterRequest, JobRunner

        path.mkdir(parents=True, exist_ok=True)
        self.root = ProjectRoot(path)
        self.store = ProjectStore(self.root)
        self.settings = Settings(project_root=path)
        self.nvml = FakeNvml()
        self.worker = FakeWorker()
        self.lease = GpuLease(
            self.root, self.settings, nvml=self.nvml, worker=self.worker,
            pid_alive=lambda _pid: True, clock=lambda: 1000.0,
        )
        self.host = FakeHost()
        self.adapter = FakeAdapter()
        self.adapter.root = self.root
        self.adapter.degrade_kind = AdapterKind.DEGRADE
        self.adapter.lease_is_owned = lambda: self.lease._owned
        self.registry = FakeRegistry(self.adapter)
        self._queue = JobQueue(
            self.root, self.settings, self.lease, self.store,
            nvml=self.nvml, host=self.host, quarantine=ProjectQuarantine(self.root),
            clock=lambda: 1000.0,
        )
        self.runner = JobRunner(
            lambda _project_id: self.store, self.settings,
            queue_for=lambda _project_id: self._queue,
            adapters=self.registry, clock=lambda: 1000.0,
        )
        self.AdapterKind = AdapterKind
        self.AdapterRequest = AdapterRequest

    def persisted_job(self, job_id: str) -> Job:
        found = []

        def visit(node) -> None:
            if isinstance(node, dict):
                if node.get("job_id") == job_id and "state" in node:
                    found.append(node)
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        for path in sorted(self.root.path.rglob("*")):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                continue
            for candidate in (content, *content.splitlines()):
                try:
                    visit(json.loads(candidate))
                except ValueError:
                    continue
        assert found, f"job {job_id} was not persisted under the project root"
        return Job.model_validate(found[-1])

    def request(self, kind=None, *, record_id="s1", record_version=1, episode_id="ep1",
                payload=None, required_vram_gb=0.0, required_ram_gb=0.0,
                required_disk_gb=0.0):
        return self.AdapterRequest(
            kind=kind or self.AdapterKind.RENDER,
            payload={} if payload is None else payload,
            record_id=record_id,
            record_version=record_version,
            episode_id=episode_id,
            required_vram_gb=required_vram_gb,
            required_ram_gb=required_ram_gb,
            required_disk_gb=required_disk_gb,
        )


@pytest.fixture
def env(tmp_path: Path) -> Environment:
    return Environment(tmp_path / "project")
