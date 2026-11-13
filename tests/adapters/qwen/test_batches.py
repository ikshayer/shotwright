"""Synthetic native payloads exercise adapter policy, not actual model correctness."""
import json
import math
import os
import time

import pytest

from orchestrator.adapters.qwen.core import (
    AnalysisRequest,
    BatchOutcome,
    PreparedWindow,
    QwenSetupError,
)
from orchestrator.adapters.qwen.runner import QwenAdapter
from tests.adapters.qwen.test_runner_contract import PlanningBackend


class BatchBackend(PlanningBackend):
    def __init__(self, root, *, fail_batch=None, cleanup=True, cap=False, eos_at_cap=False):
        super().__init__()
        self.root = root
        self.fail_batch = fail_batch
        self.cleanup = cleanup
        self.cap = cap
        self.eos_at_cap = eos_at_cap
        self.batches = []
        self.preparations = []

    def prepare(self, media_rel, window, *, work_rel, modality):
        self.preparations.append(window.index)
        rel = work_rel + "/synthetic.mkv"
        self.root.write_bytes(rel, b"synthetic prepared media")
        return PreparedWindow(rel, "a" * 64, {"sample_rate": 48000, "channels": 2,
                              "sample_count": round(window.duration_seconds * 48000),
                              "digital_silence": False}, {"synthetic_test_only": True})

    def infer_batch(self, prepared, windows, *, prompt, max_new_tokens, modality, work_rel):
        assert len(prepared) == len(windows) <= 3
        assert self.preparations == list(range(1, windows[-1].index + 1)), "future batches prepared early"
        self.batches.append([w.index for w in windows])
        telemetry = {"owned_process_tree_cleanup": {"verified_stopped": self.cleanup,
                     "survivors": [], "unverifiable_identities": [], "failures": []}}
        results = []
        for window in windows:
            if self.fail_batch == len(self.batches) and len(results) == 1:
                return BatchOutcome(tuple(results), telemetry, "failure", {
                    "family": "job", "kind": "out_of_memory", "message": "synthetic OOM",
                    "retryable": True, "remediation": "Reduce the memory demand.",
                })
            count = max_new_tokens if self.cap or self.eos_at_cap else 2
            ids = [42] * count
            eos = not self.cap or self.eos_at_cap
            if eos:
                ids[-1] = 151645
            duration = window.duration_seconds
            samples = max(4, 2 * math.ceil(duration * 0.4))
            results.append({
                "text": f"  raw window {window.index}\n```unverified```\n", "prompt": prompt,
                "output_token_ids": ids, "output_token_count": count,
                "termination_reason": "eos" if eos else "token_cap",
                "token_cap_reached": not eos and count == max_new_tokens,
                "effective_generation_kwargs": {"do_sample": False, "use_audio_in_video": modality == "joint",
                    "max_new_tokens": max_new_tokens, "eos_token_id": 151645, "pad_token_id": 151643},
                "decoded_media": {"audio_sample_rate_hz": 16000, "audio_samples": round(duration * 16000),
                    "audio_duration_seconds": duration, "audio_first_pts_seconds": 0,
                    "video_decoded_frames": round(duration * 24), "video_first_pts_seconds": 0,
                    "audio_minus_video_start_seconds": 0, "video_frame_cadence_seconds": 1 / 24,
                    "video_sample_target_error_seconds": [0] * samples,
                    "video_sample_pts_relative_seconds": [i * duration / samples for i in range(samples)],
                    "video_frame_budget": samples, "video_frames_sampled": samples, "video_sample_fps": samples / duration},
                "tensor_shapes": {"input_ids": [1, 20], "attention_mask": [1, 20],
                    "input_features": [1, 128, 30000], "feature_attention_mask": [1, 30000],
                    "pixel_values_videos": [100, 1176], "video_grid_thw": [1, 3], "video_second_per_grid": [1]},
                "loading_info": {"missing_keys": [], "unexpected_keys": [], "mismatched_keys": []},
                "quantized_linear4bit_count": 196, "unquantized_linear_count": 420,
                "quantized_linear4bit_sha256": "b" * 64,
                "unquantized_linear_sha256": "c" * 64,
                "model_provenance": {"synthetic": "not live model verification"}, "elapsed_seconds": 0.1,
            })
        return BatchOutcome(tuple(results), telemetry)


@pytest.mark.parametrize("cap,eos_at_cap,truncated", [(False, False, False), (True, False, True), (False, True, False)])
def test_batching_raw_text_eos_and_cap(project, cap, eos_at_cap, truncated):
    backend = BatchBackend(project, cap=cap, eos_at_cap=eos_at_cap)
    report = QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv", window_seconds=15,
        overlap_seconds=2, purpose="audio_review", brief="An intentional hiss", max_new_tokens=500))
    assert backend.batches == [[1, 2, 3], [4, 5, 6]]
    assert report["status"] == "completed", report.get("errors")
    assert report["has_truncated_responses"] is truncated
    assert report["input_coverage"]["uncovered_intervals"] == []
    assert "synthetic_test_only" in json.dumps(report), "preparation conversion provenance discarded"
    for window in report["windows"]:
        raw = f"  raw window {window['index']}\n```unverified```\n"
        assert window["raw_response"] == raw
        assert window["status"] == ("truncated" if truncated else "completed")
        receipt = json.loads(project.read_text(window["receipt_path"]))
        assert raw in json.dumps(receipt).replace("\\n", "\n")
        assert raw in project.read_text(report["markdown_path"])


def test_later_partial_failure_preserves_prefix_and_coverage(project):
    backend = BatchBackend(project, fail_batch=2)
    report = QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv", window_seconds=15))
    assert report["status"] == "failed"
    assert [w["status"] for w in report["windows"]] == ["completed"] * 4 + ["failed", "not_run"]
    assert report["input_coverage"]["uncovered_intervals"] == [[54, 70]]
    assert backend.batches == [[1, 2, 3], [4, 5, 6]]
    assert report["errors"]


def test_unverified_cleanup_prevents_next_batch(project):
    backend = BatchBackend(project, cleanup=False)
    report = QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv", window_seconds=15))
    assert report["status"] == "failed"
    assert len(backend.batches) == 1
    assert report["errors"]


@pytest.mark.parametrize("mutation", ["missing_text", "empty_text", "wrong_prompt", "after_eos",
    "bad_count", "loading_issues", "missing_audio", "missing_video", "wrong_quantized", "short_without_eos"])
def test_corrupt_native_result_rejected_not_normalized(project, mutation):
    class CorruptBackend(BatchBackend):
        def infer_batch(self, *args, **kwargs):
            outcome = super().infer_batch(*args, **kwargs)
            item = outcome.results[0]
            if mutation == "missing_text":
                del item["text"]
            elif mutation == "empty_text":
                item["text"] = ""
            elif mutation == "wrong_prompt":
                item["prompt"] = "Producer used an unrelated prompt"
            elif mutation == "after_eos":
                item["output_token_ids"] = [151645, 42]
            elif mutation == "bad_count":
                item["output_token_count"] += 1
            elif mutation == "loading_issues":
                item["loading_info"]["missing_keys"] = ["thinker.weight"]
            elif mutation == "missing_audio":
                del item["tensor_shapes"]["input_features"]
            elif mutation == "missing_video":
                del item["tensor_shapes"]["pixel_values_videos"]
            elif mutation == "wrong_quantized":
                item["quantized_linear4bit_count"] = 195
            elif mutation == "short_without_eos":
                item["output_token_ids"] = [42, 43]
            return outcome
    backend = CorruptBackend(project)
    report = QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv", window_seconds=15))
    assert report["status"] == "failed", mutation
    assert len(backend.batches) == 1
    assert report["errors"]


@pytest.mark.parametrize("exception,status", [(RuntimeError, "failed"), (KeyboardInterrupt, "cancelled")])
def test_exception_and_cancel_retain_completed_coverage(project, exception, status):
    class ExceptionBackend(BatchBackend):
        def infer_batch(self, *args, **kwargs):
            if self.batches:
                raise exception("independent synthetic failure after completed batch")
            return super().infer_batch(*args, **kwargs)
    backend = ExceptionBackend(project)
    report = QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv", window_seconds=15))
    assert report["status"] == status
    assert [w["status"] for w in report["windows"][:3]] == ["completed"] * 3
    assert report["input_coverage"]["uncovered_intervals"] == [[41, 70]]


def test_live_production_pid_lease_refuses_launch(project):
    original = json.dumps({"pid": os.getpid(), "heartbeat": time.time(), "ttl": 120, "token": "live-production"})
    project.write_text("jobs/gpu_lease.json", original)
    backend = BatchBackend(project)
    with pytest.raises(QwenSetupError):
        QwenAdapter(project, backend=backend).run(AnalysisRequest("media.mkv"))
    assert backend.batches == []
    assert project.read_text("jobs/gpu_lease.json") == original
