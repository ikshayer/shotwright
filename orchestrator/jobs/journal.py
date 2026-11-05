"""Project-local append-only persistence for job snapshots."""

from __future__ import annotations

import json
import os
from typing import Any

from orchestrator.domain.entities import Job
from orchestrator.platform.containment import ProjectRoot

JOB_STATES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
JOB_DIRECTORY = "state/jobs"


class JobJournalError(ValueError):
    """A job journal contains invalid complete records."""


def append_job(root: ProjectRoot, job: Job) -> None:
    """Append a complete JSON snapshot through the project's contained writer."""
    record = job.model_dump(mode="json")
    _validate_record(record)
    root.append_line(f"{JOB_DIRECTORY}/{job.job_id}.jsonl", json.dumps(record, sort_keys=True))


def read_job(root: ProjectRoot, job_id: str) -> dict[str, Any] | None:
    """Read a journal or untouched legacy JSON record and apply state precedence."""
    journal_relative = f"{JOB_DIRECTORY}/{job_id}.jsonl"
    journal_path = root.resolve(journal_relative)
    if not journal_path.exists():
        legacy = root.resolve(f"{JOB_DIRECTORY}/{job_id}.json")
        if not legacy.exists():
            return None
        record = json.loads(legacy.read_text(encoding="utf-8"))
        _validate_record(record)
        return record

    data = journal_path.read_bytes()
    lines = data.splitlines(keepends=True)
    snapshots: list[dict[str, Any]] = []
    for index, raw_line in enumerate(lines):
        is_final = index == len(lines) - 1
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if is_final:
                continue
            raise JobJournalError(f"invalid complete journal line {index + 1} for job {job_id!r}: {exc}") from exc
        _validate_record(parsed)
        snapshots.append(parsed)
    if not snapshots:
        return None

    resolved_snapshot: dict[str, Any] | None = None
    for snapshot in snapshots:
        if resolved_snapshot is not None and resolved_snapshot["state"] in TERMINAL_STATES:
            break
        resolved_snapshot = snapshot
    return resolved_snapshot.copy() if resolved_snapshot is not None else None


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
    if state not in JOB_STATES:
        raise JobJournalError(f"job record has unsupported state {state!r}")
