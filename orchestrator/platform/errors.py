"""Closed `JobError` taxonomy (FR-27).

Every failure the pipeline surfaces is one `JobErrorKind`. Classification is a pure
function of the kind: no I/O, no clock, no randomness.
"""

from __future__ import annotations

import errno
import re
import traceback
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

DIAGNOSTIC_TRACEBACK_LIMIT = 16 * 1024

#: The layers that may be named as a failure's origin. Closed, like JobErrorKind: an
#: operator reads this to tell an adapter failure from a runner or queue failure, and a
#: free-form string would make that unreliable. The diagnosability contract required an
#: origin without naming the vocabulary, so it is pinned here.
DIAGNOSTIC_ORIGINS = frozenset({"adapter", "runner", "job_queue"})

#: Well-known credential prefixes, redacted on sight. This is defence in depth, NOT a
#: guarantee: an arbitrary unregistered secret cannot be recognised in general, and
#: claiming otherwise would be worse than admitting it. Known secrets are redacted by
#: value; these shapes are caught even when nobody registered them.
_WELL_KNOWN_SECRET_PATTERN = re.compile(
    r"\b(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|sk-|xoxb-|xoxp-|AKIA)[A-Za-z0-9_\-]{8,}"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*")
_TOKEN_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(\b(?:loopback[_ -]?token|access[_ -]?token|api[_ -]?key|secret)\b\s*[:=]\s*)['\"]?[^\s,'\"]+"
)


def failure_diagnostics(
    error: JobError, *, origin: str | None = None, secrets: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Return bounded, redacted fields suitable for a persisted job snapshot."""
    cause = error.__cause__
    if cause is None and error.kind is JobErrorKind.INTERNAL:
        cause = error
    secret_values = tuple(value for value in secrets if value)

    def scrub(value: str) -> str:
        for secret in secret_values:
            value = value.replace(secret, "[REDACTED]")
        value = _WELL_KNOWN_SECRET_PATTERN.sub("[REDACTED]", value)
        value = _BEARER_PATTERN.sub(r"\1[REDACTED]", value)
        return _TOKEN_ASSIGNMENT_PATTERN.sub(r"\1[REDACTED]", value)

    def bounded(value: str, limit: int) -> str:
        value = scrub(value)
        return value if len(value) <= limit else value[:limit] + "...[TRUNCATED]"

    message = bounded(error.message, 4096)
    cause_record = None if cause is None else {
        "type": type(cause).__qualname__,
        "message": bounded(str(cause) or type(cause).__name__, 4096),
    }
    if cause is None:
        rendered = "".join(traceback.format_exception(error))
    else:
        rendered = "".join(traceback.format_exception(cause))
    rendered = scrub(rendered)
    truncated = len(rendered) > DIAGNOSTIC_TRACEBACK_LIMIT
    if truncated:
        marker = "...[TRUNCATED]"
        rendered = rendered[: DIAGNOSTIC_TRACEBACK_LIMIT - len(marker)] + marker
    return {
        "error_message": message,
        "error_cause": cause_record,
        "error_traceback": rendered or None,
        "error_traceback_truncated": truncated,
        "error_origin": origin or getattr(error, "origin", None),
    }


class JobErrorKind(StrEnum):
    """The FR-27 enumeration, verbatim and closed. Values are the JSONL wire names."""

    OUT_OF_MEMORY = "out_of_memory"
    MISSING_MODEL_ARTIFACT = "missing_model_artifact"
    PROVIDER_HTTP_4XX = "provider_http_4xx"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    UNRESOLVED_LICENSE = "unresolved_license"
    INVALID_MANIFEST = "invalid_manifest"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"
    CORRUPT_ARTIFACT = "corrupt_artifact"
    WORKER_UNREACHABLE = "worker_unreachable"
    BUDGET_EXCEEDED = "budget_exceeded"
    DISK_EXHAUSTED = "disk_exhausted"
    CANCELLED = "cancelled"
    INTERNAL = "internal"

    @property
    def policy(self) -> ErrorPolicy:
        return _POLICIES[self]

    @property
    def retryable(self) -> bool:
        return self.policy.retry is not None

    @property
    def remediation(self) -> str:
        return self.policy.remediation


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Attempt cap and exponential backoff. `max_attempts` counts the first try."""

    max_attempts: int
    base_delay_s: float
    multiplier: float
    max_delay_s: float

    def delay_before_retry(self, failed_attempts: int) -> float:
        """Seconds to wait after `failed_attempts` (>= 1) failures, capped."""
        if failed_attempts < 1:
            raise ValueError("failed_attempts must be >= 1")
        delay = self.base_delay_s * self.multiplier ** (failed_attempts - 1)
        return min(delay, self.max_delay_s)

    def allows_attempt(self, attempt_number: int) -> bool:
        """True if the 1-based `attempt_number` is within the cap."""
        return attempt_number <= self.max_attempts


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    """Classification of one kind. `retry is None` means terminal."""

    retry: RetryPolicy | None
    remediation: str


_POLICIES: dict[JobErrorKind, ErrorPolicy] = {
    JobErrorKind.OUT_OF_MEMORY: ErrorPolicy(
        RetryPolicy(max_attempts=2, base_delay_s=5.0, multiplier=2.0, max_delay_s=30.0),
        "Close other GPU/RAM consumers (check the browser) or retry with the lower-memory "
        "option, then re-run the stage.",
    ),
    JobErrorKind.MISSING_MODEL_ARTIFACT: ErrorPolicy(
        None,
        "Fetch the pinned model file into the models directory and verify its hash, "
        "then re-run the stage.",
    ),
    JobErrorKind.PROVIDER_HTTP_4XX: ErrorPolicy(
        None,
        "Fix the request or credentials the provider rejected (see the logged status and "
        "body); retrying the same request will fail again.",
    ),
    JobErrorKind.PROVIDER_RATE_LIMIT: ErrorPolicy(
        RetryPolicy(max_attempts=5, base_delay_s=2.0, multiplier=2.0, max_delay_s=60.0),
        "Wait for the provider rate-limit window to reset, or lower request concurrency.",
    ),
    JobErrorKind.UNRESOLVED_LICENSE: ErrorPolicy(
        None,
        "Record the asset licence (and voice consent where applicable) in its rights "
        "entry, or replace the asset.",
    ),
    JobErrorKind.INVALID_MANIFEST: ErrorPolicy(
        None,
        "Correct the manifest fields named in the validation detail and re-submit it.",
    ),
    JobErrorKind.SCHEMA_VERSION_MISMATCH: ErrorPolicy(
        None,
        "Run the project migration to the current schema version, or open the project "
        "with the application version that wrote it.",
    ),
    JobErrorKind.CORRUPT_ARTIFACT: ErrorPolicy(
        RetryPolicy(max_attempts=2, base_delay_s=1.0, multiplier=2.0, max_delay_s=10.0),
        "Re-run the stage that produced the artifact; if it recurs, inspect the "
        "quarantined copy.",
    ),
    JobErrorKind.WORKER_UNREACHABLE: ErrorPolicy(
        RetryPolicy(max_attempts=3, base_delay_s=3.0, multiplier=2.0, max_delay_s=30.0),
        "Check that the worker process (ComfyUI/Blender) is running and its port is "
        "free; the orchestrator will respawn it on retry.",
    ),
    JobErrorKind.BUDGET_EXCEEDED: ErrorPolicy(
        None,
        "Raise the configured budget for this run or reduce the job scope, then "
        "re-submit.",
    ),
    JobErrorKind.DISK_EXHAUSTED: ErrorPolicy(
        None,
        "Free disk space on the project drive (clear quarantine or old renders), then "
        "retry the stage.",
    ),
    JobErrorKind.CANCELLED: ErrorPolicy(
        None,
        "No action needed; the job was cancelled and partial output is quarantined. "
        "Re-submit the stage if it is still wanted.",
    ),
    JobErrorKind.INTERNAL: ErrorPolicy(
        None,
        "This is an orchestrator bug or unclassified failure; attach the job JSONL "
        "log (job_id/run_id) to a bug report.",
    ),
}

if set(_POLICIES) != set(JobErrorKind):  # closed-set guard: every kind is classified
    raise RuntimeError("every JobErrorKind must have an ErrorPolicy")


class JobError(Exception):
    """The only error type jobs raise or log. Always carries a `JobErrorKind`."""

    def __init__(
        self,
        kind: JobErrorKind,
        message: str,
        *,
        job_id: str | None = None,
        shot_id: str | None = None,
        run_id: str | None = None,
        cause: BaseException | None = None,
        remediation: str | None = None,
        origin: str | None = None,
    ) -> None:
        if not isinstance(kind, JobErrorKind):
            raise TypeError(f"kind must be a JobErrorKind, got {type(kind).__name__}")
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.job_id = job_id
        self.shot_id = shot_id
        self.run_id = run_id
        self.__cause__ = cause
        self._remediation = remediation
        self.origin = origin

    @property
    def retryable(self) -> bool:
        return self.kind.retryable

    @property
    def remediation(self) -> str:
        return self._remediation or self.kind.remediation

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        job_id: str | None = None,
        shot_id: str | None = None,
        run_id: str | None = None,
        origin: str | None = None,
    ) -> JobError:
        """Classify any exception. Unknown failures become INTERNAL; `exc` is kept as cause.

        An existing JobError keeps its kind and fills only correlation ids it lacks.
        """
        if isinstance(exc, JobError):
            exc.job_id = exc.job_id or job_id
            exc.shot_id = exc.shot_id or shot_id
            exc.run_id = exc.run_id or run_id
            exc.origin = exc.origin or origin
            return exc
        return cls(
            _kind_of_foreign_exception(exc),
            str(exc) or type(exc).__name__,
            job_id=job_id,
            shot_id=shot_id,
            run_id=run_id,
            cause=exc,
            origin=origin,
        )

    def to_log_record(self) -> dict[str, Any]:
        """Structured JSONL record (FR-27): correlated by job_id, shot_id and run_id."""
        cause = self.__cause__
        return {
            "event": "job_error",
            "error_kind": self.kind.value,
            "retryable": self.retryable,
            "message": self.message,
            "remediation": self.remediation,
            "job_id": self.job_id,
            "shot_id": self.shot_id,
            "run_id": self.run_id,
            "cause": None
            if cause is None
            else {"type": type(cause).__qualname__, "message": str(cause)},
        }


def _kind_of_foreign_exception(exc: BaseException) -> JobErrorKind:
    if isinstance(exc, MemoryError):
        return JobErrorKind.OUT_OF_MEMORY
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return JobErrorKind.DISK_EXHAUSTED
    return JobErrorKind.INTERNAL
