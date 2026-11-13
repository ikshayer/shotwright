"""A failed or cancelled H3 run must carry a content receipt for its report.

Written before the implementation, from the A2.2 decision in
`orchestrator/adapters/minimax_h3/NATIVE_BLOCKERS_CONTRACT.md`.

The success path hashes all three artifacts onto `H3Candidate`. The terminal
(failed/cancelled) path recorded its report by **path only**, because
`GenerationRun` carried no content-hash field at all -- so nothing detected a
terminal report being altered or truncated after the fact. The decision was to
add `output_hashes` to `GenerationRun` rather than rule the asymmetry acceptable,
and to *drive* it in the same change: this repository has nine times produced a
fully-specified field that nothing populates, and an undriven hash field would be
the tenth.

`persist_h3_terminal` had no test coverage of any kind before this file, despite
being reachable from `runner.py` on both the failed and cancelled paths. The
preservation cases at the bottom are therefore not padding -- they are the first
tests that pin its existing contract.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from orchestrator.adapters.minimax_h3.core import H3Request
from orchestrator.domain.entities import GenerationRun, Job
from orchestrator.platform.containment import ProjectRoot
from orchestrator.store.json_store import ProjectStore

REPORT = {"status": "failed", "seed": 7, "log_path": "logs/h3.log",
          "runtime": {"version": "0.44.0"},
          "primary_error": {"type": "H3SetupError", "message": "schema rejected"}}


def _setup(tmp_path, *, report=REPORT, write=True):
    """A store with a terminal H3 report on disk, plus the request/job that names it."""
    root = ProjectRoot(tmp_path)
    store = ProjectStore(root)
    request = H3Request(
        shot_id="s1", shot_version=1, run_id="h3-run", video_path="renders/s1/v0001/master.mp4",
        source_sha256="a" * 64, source_output_id="master1", prompt="Quiet corridor.",
    )
    run_rel = f"generated/h3/{request.shot_id}/v{request.shot_version:04d}/{request.run_id}"
    report_rel = run_rel + "/report.json"
    payload = json.dumps(report).encode("utf-8")
    if write:
        root.write_bytes(report_rel, payload)
    job = Job(job_id="j1", type="minimax_h3", resource_metrics={"gpu_seconds": 1.5})
    return store, request, job, report_rel, payload


def _persist(store, request, job, *, status="failed"):
    from orchestrator.service.minimax_h3 import persist_h3_terminal
    return persist_h3_terminal(store, job, request, status=status)


# --- the receipt itself -------------------------------------------------------

@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_run_records_the_report_hash(tmp_path, status):
    """Both terminal statuses get a receipt, not just the failed one."""
    store, request, job, report_rel, payload = _setup(tmp_path)
    run = _persist(store, request, job, status=status)
    assert run is not None
    assert run.output_hashes == {report_rel: hashlib.sha256(payload).hexdigest()}


def test_recorded_hash_detects_a_tampered_report(tmp_path):
    """The point of the receipt: altering the report afterwards is now detectable.

    Without this, the assertion above could be satisfied by a hash of anything at
    all. This pins the digest to the bytes that were actually on disk.
    """
    store, request, job, report_rel, payload = _setup(tmp_path)
    run = _persist(store, request, job)
    tampered = json.dumps({**REPORT, "status": "succeeded"}).encode("utf-8")
    assert tampered != payload
    assert run.output_hashes[report_rel] != hashlib.sha256(tampered).hexdigest()
    assert run.output_hashes[report_rel] == hashlib.sha256(payload).hexdigest()


def test_the_hashed_path_is_also_a_declared_output(tmp_path):
    """A receipt that named a path `output_paths` did not list would be unreachable."""
    store, request, job, report_rel, _ = _setup(tmp_path)
    run = _persist(store, request, job)
    assert report_rel in run.output_paths
    assert set(run.output_hashes) <= set(run.output_paths)


def test_the_receipt_survives_a_store_round_trip(tmp_path):
    """An in-memory-only receipt would be no receipt at all."""
    store, request, job, report_rel, payload = _setup(tmp_path)
    _persist(store, request, job)
    reloaded = store.load(GenerationRun, request.run_id)
    assert reloaded.output_hashes == {report_rel: hashlib.sha256(payload).hexdigest()}


# --- entity validation -------------------------------------------------------

@pytest.mark.parametrize("digest", [
    "not-a-hash",
    "A" * 64,                 # uppercase hex is not the repo's canonical form
    "a" * 63,                 # too short
    "a" * 65,                 # too long
    "g" * 64,                 # right length, not hex
    "",
])
def test_malformed_digests_are_rejected(digest):
    with pytest.raises(ValidationError):
        GenerationRun(run_id="r1", tool="t", tool_version="1", invocation="i",
                      output_paths=["generated/report.json"],
                      output_hashes={"generated/report.json": digest})


def test_a_hash_for_an_undeclared_path_is_rejected():
    """Keys must be a subset of `output_paths`, or the two fields can drift apart."""
    with pytest.raises(ValidationError):
        GenerationRun(run_id="r1", tool="t", tool_version="1", invocation="i",
                      output_paths=["generated/a.json"],
                      output_hashes={"generated/b.json": "a" * 64})


def test_a_well_formed_receipt_is_accepted():
    run = GenerationRun(run_id="r1", tool="t", tool_version="1", invocation="i",
                        output_paths=["generated/a.json", "generated/b.json"],
                        output_hashes={"generated/a.json": "a" * 64})
    assert run.output_hashes == {"generated/a.json": "a" * 64}


# --- nothing weakened --------------------------------------------------------

def test_output_hashes_defaults_to_empty():
    """Every other tool's GenerationRun must keep constructing exactly as before.

    `GenerationRun` is the generic run entity for FFmpeg and Blender too, so a
    required field here would have broken callers that have no hashes to offer.
    """
    run = GenerationRun(run_id="r1", tool="ffmpeg", tool_version="7.0", invocation="-i in.mp4")
    assert run.output_hashes == {}
    assert run.output_paths == []


def test_terminal_persist_returns_none_without_a_report(tmp_path):
    store, request, job, _, _ = _setup(tmp_path, write=False)
    assert _persist(store, request, job) is None


def test_terminal_persist_is_create_only(tmp_path):
    """A retry must return the existing record untouched, never revise it."""
    store, request, job, report_rel, payload = _setup(tmp_path)
    first = _persist(store, request, job)
    again = _persist(store, request, job, status="cancelled")
    assert again is not None
    assert again.status == first.status == "failed"
    assert again.output_hashes == {report_rel: hashlib.sha256(payload).hexdigest()}


def test_an_invalid_status_is_refused(tmp_path):
    store, request, job, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="failed or cancelled"):
        _persist(store, request, job, status="succeeded")
