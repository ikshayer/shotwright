"""Fake backend measures admission/orchestration only, never native cleanup proof."""

import pytest

from orchestrator.adapters.minimax_h3 import MinimaxH3Adapter
from orchestrator.adapters.minimax_h3.core import H3Request
from orchestrator.domain.entities import Job
from orchestrator.jobs.lease import GpuLease
from orchestrator.jobs.queue import JobQueue, ProjectQuarantine
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError
from orchestrator.platform.settings import parse_settings
from orchestrator.store.json_store import ProjectStore
from tests.jobs.test_lease import FakeNvml
from tests.jobs.test_queue import FakeHost


class Backend:
    def __init__(self):
        self.calls = []

    def inspect(self):
        return {}

    def execute(self, *args, **kwargs):
        self.calls.append("execute")
        raise AssertionError("Unowned or mismatched lease must reject before backend")

    def stop(self):
        self.calls.append("stop")
        return True

    def close(self):
        self.calls.append("close")


def test_real_queue_unowned_lease_never_invokes_backend(tmp_path):
    root = ProjectRoot(tmp_path)
    settings = parse_settings(
        {
            "minimax_h3": {
                "enabled": True,
                "required_vram_gb": 1.0,
                "required_ram_gb": 1.0,
                "required_disk_gb": 1.0,
                "throughput_floor_per_second": 0.1,
            }
        }
    )
    backend = Backend()
    adapter = MinimaxH3Adapter(root, settings, backend=backend)
    probe = FakeNvml()
    lease = GpuLease(root, settings, nvml=probe, worker=adapter.worker)
    queue = JobQueue(
        root,
        settings,
        lease,
        ProjectStore(root),
        nvml=probe,
        host=FakeHost(),
        quarantine=ProjectQuarantine(root),
    )
    req = H3Request(
        shot_id="s1",
        shot_version=1,
        run_id="r1",
        video_path="source.mp4",
        source_sha256="a" * 64,
        source_output_id="o1",
        prompt="room",
    )
    job = Job(job_id="j1", type="h3_candidate", shot_id="s1", run_id="r1")
    try:
        with pytest.raises((ValueError, JobError)):
            adapter.generate_managed(req, job, queue=queue)
        assert "execute" not in backend.calls
    finally:
        adapter.close()
