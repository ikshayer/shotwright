"""Targeted independent regressions from contract adjudication; synthetic model data."""
import hashlib
import importlib.util
import json
import os
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator.adapters.qwen.core import (
    AnalysisRequest,
    BatchOutcome,
    QwenSetupError,
    WindowSpec,
)
from orchestrator.adapters.qwen.runner import NativeBackend, QwenAdapter
from orchestrator.platform.containment import ProjectRoot
from tests.adapters.qwen.test_batches import BatchBackend


def test_audio_only_native_none_video_timestamps_are_valid(project):
    class AudioBackend(BatchBackend):
        def infer_batch(self, *args, **kwargs):
            outcome = super().infer_batch(*args, **kwargs)
            assert kwargs["modality"] == "audio"
            for item in outcome.results:
                for key in ("pixel_values_videos", "video_grid_thw", "video_second_per_grid"):
                    del item["tensor_shapes"][key]
                item["decoded_media"].update({
                    "video_decoded_frames": 0, "video_first_pts_seconds": None,
                    "audio_minus_video_start_seconds": None, "video_frame_cadence_seconds": None,
                    "video_sample_target_error_seconds": [], "video_sample_pts_relative_seconds": [],
                    "video_frame_budget": 0, "video_frames_sampled": 0, "video_sample_fps": 0,
                })
            return outcome
    backend = AudioBackend(project)
    report = QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv", modality="audio"))
    assert report["status"] == "completed", report["errors"]
    assert all(w["status"] == "completed" for w in report["windows"])
    assert report["input_coverage"]["uncovered_intervals"] == []


@pytest.mark.parametrize("failure", ["native_status", "cleanup"])
def test_all_results_do_not_override_terminal_batch_failure(project, failure):
    class TerminalFailureBackend(BatchBackend):
        def infer_batch(self, *args, **kwargs):
            outcome = super().infer_batch(*args, **kwargs)
            if failure == "cleanup":
                outcome.telemetry["owned_process_tree_cleanup"]["verified_stopped"] = False
                return outcome
            return BatchOutcome(outcome.results, outcome.telemetry, "failure", {
                "family": "job", "kind": "internal", "message": "synthetic failure after all outputs",
                "retryable": False, "remediation": "Inspect retained diagnostics.",
            })
    backend = TerminalFailureBackend(project)
    report = QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv", duration_seconds=10))
    assert len(report["windows"]) == 1
    assert report["status"] == "failed", failure
    assert report["errors"]


def test_short_other_termination_is_corrupt_artifact(project):
    class UnsupportedTerminationBackend(BatchBackend):
        def infer_batch(self, *args, **kwargs):
            outcome = super().infer_batch(*args, **kwargs)
            for item in outcome.results:
                item["output_token_ids"] = [42, 43]
                item["termination_reason"] = "other"
                item["token_cap_reached"] = False
            return outcome
    backend = UnsupportedTerminationBackend(project)
    report = QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv", duration_seconds=10))
    assert report["status"] == "failed"
    assert "corrupt_artifact" in json.dumps(report["errors"])


def test_expired_live_production_lease_refuses_without_os_kill(project, monkeypatch):
    original = json.dumps({"pid": os.getpid(), "heartbeat": 0, "ttl": 1, "token": "expired-but-live"})
    project.write_text("jobs/gpu_lease.json", original)

    kill_calls = []

    def forbidden_kill(*args, **kwargs):
        kill_calls.append((args, kwargs))
        raise AssertionError("Windows lease liveness must never call os.kill, including signal 0")

    monkeypatch.setattr(os, "kill", forbidden_kill)
    backend = BatchBackend(project)
    with pytest.raises(QwenSetupError) as error:
        QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv"))
    assert any(word in str(error.value).lower() for word in ("lease", "production", "owner"))
    assert backend.batches == []
    assert project.read_text("jobs/gpu_lease.json") == original
    assert kill_calls == []


@pytest.mark.parametrize("verified_cleanup", [True, False])
def test_native_keyboard_interrupt_retains_valid_partial_result(monkeypatch, verified_cleanup):
    repo = Path(__file__).resolve().parents[3]
    root = ProjectRoot(repo)
    prefix = "_work/qwen_contract_tests/native_partial/" + uuid.uuid4().hex
    windows = [WindowSpec(1, 0, 10, 0, 10), WindowSpec(2, 8, 18, 8, 18)]
    fake = BatchBackend(root)
    prepared = [fake.prepare("source.mkv", window, work_rel=f"{prefix}/window_{window.index}", modality="joint")
                for window in windows]
    prepared = [replace(item, media_sha256=hashlib.sha256(root.read_bytes(item.media_path)).hexdigest())
                for item in prepared]
    prompt = "Independent intended prompt for synthetic partial cancellation receipt"
    result = fake.infer_batch(prepared, windows, prompt=prompt, max_new_tokens=500,
                             modality="joint", work_rel=prefix).results[0]
    # Native paths/hashes refer to real contained bytes; this remains synthetic model output.
    spec = importlib.util.spec_from_file_location("independent_partial_cancel", repo / "spikes/23_joint_av_analyzer/analyze.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)

    def forbidden(*args, **kwargs):
        raise AssertionError("CPU partial-cancellation test must not launch subprocesses, query GPU, or load a model")

    def interrupted_supervisor(items, mode, max_new_tokens, duration_limit,
                               result_rel, stdout_rel, stderr_rel, resources_rel,
                               analysis_prompt=None):
        assert mode == "joint" and max_new_tokens == 500 and analysis_prompt == prompt
        assert len(items) == 2
        root.write_text(result_rel, json.dumps({"status": "failure", "results": [result],
            "error_type": "KeyboardInterrupt", "error": "synthetic interruption after first native result"}))
        root.write_text(stdout_rel, "synthetic worker stdout")
        root.write_text(stderr_rel, "synthetic worker interrupted")
        root.append_line(resources_rel, json.dumps({"event": "supervisor_failure",
            "error": "KeyboardInterrupt: synthetic cancellation", "cleanup_error": None,
            "owned_process_tree_cleanup": {"verified_stopped": verified_cleanup,
                "survivors": [], "unverifiable_identities": [], "failures": []},
            "owned_process_identities": [{"pid": 987654321, "live_identity_verified": False,
                "identity_state": "not_live_or_identity_mismatch"}]}))
        raise KeyboardInterrupt("synthetic cancellation after retained native prefix")

    native.run_supervised = interrupted_supervisor
    native.load_model = forbidden
    native._gpu_memory_sample = forbidden
    monkeypatch.setattr(NativeBackend, "_module", lambda self: native)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    outcome = NativeBackend(root).infer_batch(prepared, windows, prompt=prompt, max_new_tokens=500,
                                             modality="joint", work_rel=prefix + "/native")
    assert outcome.status in ("cancelled", "failure", "failed")
    assert len(outcome.results) == 1, "native cancellation discarded valid retained worker prefix"
    assert outcome.results[0] == result
    assert outcome.error is not None
    cleanup = outcome.telemetry.get("owned_process_tree_cleanup", {})
    assert cleanup.get("verified_stopped") is verified_cleanup, "cleanup certainty was invented"
