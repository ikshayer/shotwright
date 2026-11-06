"""Regression tests for persisted diagnostics from real JobQueue failures."""

from __future__ import annotations

import json
import traceback
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from orchestrator.domain.entities import Job
from orchestrator.jobs.journal import read_job
from orchestrator.platform.errors import JobError, JobErrorKind
from tests.jobs.test_queue import build, make_job


def _failed_job(env, job: Job, worker) -> dict[str, object]:
    """Run a failing worker through queue transitions and return its journal snapshot."""
    with pytest.raises(JobError):
        env.queue.run(job, worker)
    record = read_job(env.root, job.job_id)
    assert record is not None
    assert record["state"] == "failed"
    return record


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _contains(value: object, expected: str) -> bool:
    return any(item == expected for item in _strings(value))


def _has_truncation_marker(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if "truncat" in str(key).lower() and item is True:
                return True
            if _has_truncation_marker(item):
                return True
    elif isinstance(value, list):
        return any(_has_truncation_marker(item) for item in value)
    elif isinstance(value, str):
        return "truncat" in value.lower() or value.endswith(("...", "…"))
    return False


def test_worker_joberror_cause_is_persisted_in_one_append_only_jsonl_line(tmp_path) -> None:
    env = build(tmp_path)
    job = make_job(job_id="cause-jsonl")
    message = 'quoted "text", backslash \\ and real\nnewline'
    cause = LookupError("root cause detail")
    prefix: bytes | None = None

    def fail(_job: Job) -> None:
        nonlocal prefix
        path = env.root.resolve(f"state/jobs/{job.job_id}.jsonl")
        prefix = path.read_bytes()
        raise JobError(JobErrorKind.INVALID_MANIFEST, message, cause=cause)

    record = _failed_job(env, job, fail)
    journal = env.root.resolve(f"state/jobs/{job.job_id}.jsonl")
    final_bytes = journal.read_bytes()

    assert prefix is not None and final_bytes.startswith(prefix)
    assert final_bytes[len(prefix):].count(b"\n") == 1
    assert final_bytes.endswith(b"\n")
    lines = final_bytes.splitlines()
    parsed = [json.loads(line) for line in lines]
    assert parsed[-1] == record
    assert _contains(record, message)
    assert _contains(record, "LookupError")
    assert _contains(record, "root cause detail")


def test_bare_worker_exception_persists_internal_type_message_and_origin(tmp_path) -> None:
    env = build(tmp_path)
    job = make_job(job_id="bare-internal")
    message = "adapter payload failed validation"

    def fail(_job: Job) -> None:
        raise RuntimeError(message)

    record = _failed_job(env, job, fail)

    assert record["error_kind"] == JobErrorKind.INTERNAL.value
    assert _contains(record, message)
    assert _contains(record, "RuntimeError")
    # The contract leaves the origin property name open. Require a distinct semantic
    # value in persisted diagnostic metadata, without choosing a property name.
    base_fields = set(Job.model_fields)
    extra = {key: value for key, value in record.items() if key not in base_fields}
    assert extra, "failure snapshot has no diagnostic metadata beyond Job fields"
    assert any("worker" in item.lower() for item in _strings(extra)), extra


def test_long_traceback_is_bounded_and_truncation_is_visible(tmp_path) -> None:
    env = build(tmp_path)
    job = make_job(job_id="long-traceback")
    original_traceback: list[str] = []

    def fail(_job: Job) -> None:
        namespace: dict[str, object] = {}
        definitions = [
            f"def frame_{index}(): return frame_{index + 1}()"
            for index in range(159)
        ]
        definitions.append('def frame_159(): raise ArithmeticError("traceback-end-marker")')
        exec("\n".join(definitions), namespace)

        try:
            namespace["frame_0"]()  # type: ignore[operator]
        except ArithmeticError as exc:
            rendered = "".join(traceback.format_exception(exc))
            assert len(rendered) > 8_000
            original_traceback.append(rendered)
            raise JobError(JobErrorKind.INTERNAL, "deep worker failure", cause=exc) from exc

    record = _failed_job(env, job, fail)
    strings = list(_strings(record))
    trace_strings = [item for item in strings if "Traceback (most recent call last)" in item]

    assert trace_strings, "persisted diagnostics contain no traceback"
    assert original_traceback
    assert sum(map(len, trace_strings)) < len(original_traceback[0])
    assert _has_truncation_marker(record), "traceback truncation is not marked"


def test_token_shaped_message_secret_is_not_persisted_verbatim(tmp_path) -> None:
    env = build(tmp_path)
    job = make_job(job_id="secret-message")
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2

    def fail(_job: Job) -> None:
        raise RuntimeError(f"request failed with credential {secret}")

    record = _failed_job(env, job, fail)

    assert _contains(record, "request failed with credential")
    assert secret not in json.dumps(record)


def test_failure_log_path_is_project_relative_and_contained(tmp_path) -> None:
    env = build(tmp_path)
    job = make_job(job_id="contained-log")

    def fail(_job: Job) -> None:
        raise RuntimeError("log path check")

    record = _failed_job(env, job, fail)
    log_path = record["log_path"]

    assert isinstance(log_path, str) and log_path
    assert not PurePosixPath(log_path).is_absolute()
    assert not PureWindowsPath(log_path).is_absolute()
    assert not PureWindowsPath(log_path).drive
    assert ".." not in log_path.replace("\\", "/").split("/")
    resolved = env.root.resolve(log_path)
    assert resolved == env.root.path or env.root.path in resolved.parents


def test_failure_diagnostics_do_not_extend_closed_job_error_kind_set() -> None:
    assert {kind.value for kind in JobErrorKind} == {
        "out_of_memory",
        "missing_model_artifact",
        "provider_http_4xx",
        "provider_rate_limit",
        "unresolved_license",
        "invalid_manifest",
        "schema_version_mismatch",
        "corrupt_artifact",
        "worker_unreachable",
        "budget_exceeded",
        "disk_exhausted",
        "cancelled",
        "internal",
    }
