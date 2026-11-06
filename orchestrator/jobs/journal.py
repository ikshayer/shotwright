"""Project-local append-only persistence for job snapshots."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from orchestrator.domain.entities import Job
from orchestrator.platform.containment import ProjectRoot

JOB_STATES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
JOB_DIRECTORY = "state/jobs"
_LOGGER = logging.getLogger(__name__)


class JobJournalError(ValueError):
    """A job journal contains invalid complete records."""


def append_job(root: ProjectRoot, job: Job) -> None:
    """Append a complete JSON snapshot through the project's contained writer."""
    job.log_path = f"{JOB_DIRECTORY}/{job.job_id}.jsonl"
    record = job.model_dump(mode="json")
    _validate_record(record)
    root.append_line(f"{JOB_DIRECTORY}/{job.job_id}.jsonl", json.dumps(record, sort_keys=True))


def read_job(root: ProjectRoot, job_id: str) -> dict[str, Any] | None:
    """Read a journal or untouched legacy JSON record and apply state precedence."""
    journal_relative = f"{JOB_DIRECTORY}/{job_id}.jsonl"
    journal_path = root.resolve(journal_relative)
    if not journal_path.exists():
        return _read_legacy(root, job_id)

    data = journal_path.read_bytes()
    lines = data.splitlines(keepends=True)
    snapshots: list[dict[str, Any]] = []
    for index, raw_line in enumerate(lines):
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
            _validate_record(parsed)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _LOGGER.warning(
                "skipping invalid journal line %d for job %r: %s", index + 1, job_id, exc
            )
            continue
        except JobJournalError as exc:
            _LOGGER.warning(
                "skipping invalid journal line %d for job %r: %s", index + 1, job_id, exc
            )
            continue
        snapshots.append(parsed)
    if not snapshots:
        return _read_legacy(root, job_id, damaged_journal=True)

    resolved_snapshot: dict[str, Any] | None = None
    for snapshot in snapshots:
        if resolved_snapshot is not None and resolved_snapshot["state"] in TERMINAL_STATES:
            break
        resolved_snapshot = snapshot
    if resolved_snapshot is None:
        return None
    return _resolve_recorded_failure(resolved_snapshot, snapshots, job_id).copy()


def _resolve_recorded_failure(
    resolved: dict[str, Any], snapshots: list[dict[str, Any]], job_id: str
) -> dict[str, Any]:
    """A recorded ``failed`` entry is reported over ``completed`` or ``cancelled``.

    First-terminal-wins exists to make a *cancel racing a completion* deterministic, where
    either answer is legitimate because the operator cancelled as the work finished. That
    argument never covered a **failure**. Nothing concurrent produces a failure that is safe
    to discard, and the two cases it hid are both ones an operator must see:

    - `completed` then `failed` — the job reported an artifact and then failed, so the
      success claim no longer holds.
    - `cancelled` then `failed` — teardown after the cancel failed, typically a lease that
      could not be released. Suppressing it strands the GPU with nothing in the record
      saying so, which is the worst of the three outcomes.

    A failure *caused* by a cancel is reported by the queue as `cancelled`, not `failed`, so
    a `failed` entry following a cancel is genuinely new information rather than an echo.

    The failing snapshot itself is returned, not merely its state, so the record carries the
    error kind, message and traceback. The first `failed` entry is chosen, so resolution
    stays deterministic for a fixed journal however many there are. This is NOT a severity
    ordering — `completed` then `cancelled` still reports `completed`, as ratified.

    Note what this does not fix: resolution picks one snapshot, and a snapshot is a
    point-in-time record. A `cancelled` entry written by `JobRunner.cancel` while the worker
    is still running is reported thereafter, so its `lease_holder_pid` keeps naming a holder
    the queue has since released. Reporting `state` from one snapshot and other fields from
    another would be a different contract; see JOURNAL_CONTRACT.md.
    """
    if resolved["state"] not in {"completed", "cancelled"}:
        return resolved
    failure = next((snapshot for snapshot in snapshots if snapshot["state"] == "failed"), None)
    if failure is None:
        return resolved
    _LOGGER.warning(
        "job %r journal records state %s and a later failure; reporting the failure: %s",
        job_id,
        resolved["state"],
        failure.get("error_message") or failure.get("error_kind") or "no cause recorded",
    )
    return failure


def _read_legacy(
    root: ProjectRoot, job_id: str, *, damaged_journal: bool = False
) -> dict[str, Any] | None:
    legacy = root.resolve(f"{JOB_DIRECTORY}/{job_id}.json")
    if not legacy.exists():
        if damaged_journal:
            raise JobJournalError(
                f"job journal for {job_id!r} contains no complete snapshot and no legacy record exists"
            )
        return None
    record = json.loads(legacy.read_text(encoding="utf-8"))
    _validate_record(record)
    return record


def read_job_records(root: ProjectRoot, job_ids: list[str]) -> list[dict[str, Any]]:
    """Read each requested job once, preferring its journal over a legacy file."""
    records = []
    canonical_ids: dict[str, str] = {}
    for job_id in sorted(set(job_ids)):
        canonical_ids.setdefault(os.path.normcase(job_id), job_id)
    for job_id in (canonical_ids[key] for key in sorted(canonical_ids)):
        record = read_job(root, job_id)
        if record is not None:
            records.append(record)
    return records


def _validate_record(record: Any) -> None:
    if not isinstance(record, dict):
        raise JobJournalError("job record must be a JSON object")
    state = record.get("state")
    if not isinstance(state, str) or state not in JOB_STATES:
        raise JobJournalError(f"job record has unsupported state {state!r}")
