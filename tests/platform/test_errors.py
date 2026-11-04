import errno
import json

import pytest

from orchestrator.platform.errors import JobError, JobErrorKind, RetryPolicy

PRD_MEMBERS = {
    "out_of_memory", "missing_model_artifact", "provider_http_4xx", "provider_rate_limit",
    "unresolved_license", "invalid_manifest", "schema_version_mismatch", "corrupt_artifact",
    "worker_unreachable", "budget_exceeded", "disk_exhausted", "cancelled", "internal",
}
RETRYABLE = {
    JobErrorKind.OUT_OF_MEMORY, JobErrorKind.PROVIDER_RATE_LIMIT,
    JobErrorKind.CORRUPT_ARTIFACT, JobErrorKind.WORKER_UNREACHABLE,
}


def test_enumeration_matches_fr27_exactly():
    assert {k.value for k in JobErrorKind} == PRD_MEMBERS


@pytest.mark.parametrize("kind", list(JobErrorKind))
def test_every_kind_is_classified_with_remediation(kind):
    assert kind.retryable == (kind in RETRYABLE)
    assert len(kind.remediation) > 20
    if kind.retryable:
        assert kind.policy.retry.max_attempts >= 2
    else:
        assert kind.policy.retry is None


def test_backoff_is_exponential_and_capped():
    policy = RetryPolicy(max_attempts=5, base_delay_s=2.0, multiplier=2.0, max_delay_s=10.0)
    assert [policy.delay_before_retry(n) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 10.0]
    assert policy.allows_attempt(5) and not policy.allows_attempt(6)
    with pytest.raises(ValueError):
        policy.delay_before_retry(0)


def test_unclassified_kind_cannot_be_constructed():
    with pytest.raises(TypeError):
        JobError("out_of_memory", "plain string is not a kind")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        JobErrorKind("something_new")


def test_unknown_exception_maps_to_internal_and_keeps_cause():
    original = KeyError("boom")
    err = JobError.from_exception(original, job_id="j1")
    assert err.kind is JobErrorKind.INTERNAL
    assert err.__cause__ is original
    assert err.job_id == "j1"


def test_known_foreign_exceptions_are_classified():
    assert JobError.from_exception(MemoryError()).kind is JobErrorKind.OUT_OF_MEMORY
    disk = OSError(errno.ENOSPC, "No space left on device")
    assert JobError.from_exception(disk).kind is JobErrorKind.DISK_EXHAUSTED
    assert JobError.from_exception(OSError(errno.ENOENT, "x")).kind is JobErrorKind.INTERNAL


def test_from_exception_preserves_existing_job_error_and_fills_missing_ids():
    original = JobError(JobErrorKind.CANCELLED, "stopped", job_id="j1")
    same = JobError.from_exception(original, job_id="other", shot_id="s1", run_id="r1")
    assert same is original
    assert (same.kind, same.job_id, same.shot_id, same.run_id) == (
        JobErrorKind.CANCELLED, "j1", "s1", "r1",
    )


def test_log_record_shape_is_json_serialisable_and_correlated():
    cause = ConnectionRefusedError("127.0.0.1:8188")
    err = JobError(
        JobErrorKind.WORKER_UNREACHABLE, "ComfyUI down",
        job_id="j1", shot_id="s2", run_id="r3", cause=cause,
    )
    record = json.loads(json.dumps(err.to_log_record()))
    assert record["error_kind"] == "worker_unreachable"
    assert record["retryable"] is True
    assert record["remediation"] == JobErrorKind.WORKER_UNREACHABLE.remediation
    assert (record["job_id"], record["shot_id"], record["run_id"]) == ("j1", "s2", "r3")
    assert record["cause"] == {"type": "ConnectionRefusedError", "message": "127.0.0.1:8188"}


def test_log_record_without_cause_has_null_cause():
    assert JobError(JobErrorKind.INTERNAL, "x").to_log_record()["cause"] is None
