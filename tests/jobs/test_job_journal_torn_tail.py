"""Regression tests for resuming writes after a torn job journal tail."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.domain.entities import Job
from orchestrator.jobs.journal import append_job, read_job
from orchestrator.platform.containment import ProjectRoot


def _job(job_id: str, state: str, *, attempts: int = 0) -> Job:
    return Job(job_id=job_id, type="flux_still", state=state, attempts=attempts)


def _append(root: ProjectRoot, job_id: str, state: str, *, attempts: int = 0) -> None:
    append_job(root, _job(job_id, state, attempts=attempts))


def test_append_seals_truncated_tail_and_keeps_both_resumed_transitions(
    tmp_path: Path,
) -> None:
    root = ProjectRoot(tmp_path)
    _append(root, "resume", "queued")
    _append(root, "resume", "running", attempts=1)
    journal = root.resolve("state/jobs/resume.jsonl")

    intact_prefix = journal.read_bytes()
    _append(root, "resume", "running", attempts=2)
    before_crash = journal.read_bytes()
    # A process died during its next record: retain only a strict prefix of that line.
    assert before_crash.endswith(b"\n"), "setup did not produce a complete journal line"
    crash_cutoff = len(before_crash) - 8
    assert crash_cutoff > len(intact_prefix), "truncation would damage an earlier complete entry"
    with journal.open("r+b") as stream:
        stream.truncate(crash_cutoff)
    damaged_bytes = journal.read_bytes()
    assert damaged_bytes == before_crash[:crash_cutoff], "crash simulation did not truncate the real file"
    assert damaged_bytes.startswith(intact_prefix), "crash truncation changed an earlier complete entry"
    assert not damaged_bytes.endswith(b"\n"), "crash simulation did not leave an unterminated tail"

    _append(root, "resume", "completed", attempts=2)
    _append(root, "resume", "failed", attempts=3)

    after_recovery = journal.read_bytes()
    assert after_recovery.startswith(intact_prefix), (
        "recovery changed bytes belonging to complete entries written before the damaged tail"
    )
    parsed_lines: list[dict[str, Any]] = []
    unparseable: list[int] = []
    for line_number, line in enumerate(after_recovery.splitlines(), start=1):
        try:
            parsed_lines.append(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # The sealed fragment is SUPPOSED to be unparseable. Amendment 1: a crash
            # legitimately produces an unparseable interior line and readers skip it.
            unparseable.append(line_number)
    # Exactly one damaged line, which is stronger than tolerating any number: it proves the
    # fragment was sealed as a line of its own rather than fused onto the next record.
    assert len(unparseable) == 1, (
        f"expected exactly the sealed fragment to be unparseable, got lines {unparseable}; "
        "more than one means recovery damaged further records, none means the fragment was "
        "fused into a neighbouring line"
    )

    states = [record["state"] for record in parsed_lines]
    assert states[-2:] == ["completed", "failed"], (
        "the two post-crash transitions were lost or fused with the torn fragment; "
        f"the final parsed states were {states!r}"
    )
    try:
        resolved = read_job(root, "resume")
    except Exception as exc:  # noqa: BLE001 - report the concrete recovery failure
        pytest.fail(f"reader raised after a torn tail and two later appends: {type(exc).__name__}: {exc}")
    assert resolved is not None, "reader returned no snapshot after two successful resumed appends"
    # This test's subject is the SEAL -- that a torn tail is sealed as its own line and the
    # two resumed appends survive it, which the assertions above pin. The resolved state is
    # a bystander here: `completed` then `failed` now reports the failure, because only a
    # completion claims an artifact and a later failure says there is none. The point
    # retained from the original assertion is that resolution still works at all after a
    # torn tail, and resolves to one of the two complete terminal entries.
    assert resolved["state"] == "failed", (
        "a completion falsified by a later failure must report the failure, even when a "
        f"sealed fragment sits between them; observed {resolved['state']!r}"
    )
    assert resolved["attempts"] == 3, (
        f"the reported record is not the failing snapshot: {resolved!r}"
    )


def test_reader_skips_unparseable_interior_line_and_resolves_remaining_entries(
    tmp_path: Path,
) -> None:
    root = ProjectRoot(tmp_path)
    _append(root, "interior", "queued")
    journal = root.resolve("state/jobs/interior.jsonl")
    with journal.open("ab") as stream:
        stream.write(b'{"job_id":"interior","state":broken}\n')
    _append(root, "interior", "completed", attempts=1)
    _append(root, "interior", "running", attempts=2)

    try:
        resolved = read_job(root, "interior")
    except Exception as exc:  # noqa: BLE001 - report what the reader actually did
        pytest.fail(f"reader raised on an unparseable interior line: {type(exc).__name__}: {exc}")
    assert resolved is not None, "reader returned no snapshot after skipping the damaged line"
    assert resolved["state"] == "completed", (
        "reader did not resolve the first terminal snapshot after skipping the interior line; "
        f"observed {resolved['state']!r}"
    )


def test_incomplete_journal_falls_back_to_legacy_record_without_rewriting_it(
    tmp_path: Path,
) -> None:
    root = ProjectRoot(tmp_path)
    legacy_path = root.resolve("state/jobs/legacy-fallback.json")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_bytes = (
        json.dumps(_job("legacy-fallback", "running").model_dump(mode="json"), indent=2) + "\n"
    ).encode("utf-8")
    legacy_path.write_bytes(legacy_bytes)
    journal_path = root.resolve("state/jobs/legacy-fallback.jsonl")
    journal_path.write_bytes(b'{"job_id":"legacy-fallback","state":')

    resolved = read_job(root, "legacy-fallback")

    assert resolved is not None, "incomplete journal hid an available legacy snapshot"
    assert resolved["state"] == "running", (
        f"fallback returned state {resolved.get('state')!r}, expected legacy state 'running'"
    )
    assert legacy_path.read_bytes() == legacy_bytes, "legacy fallback rewrote the source JSON bytes"


def test_incomplete_journal_without_legacy_record_fails_loudly(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    journal_path = root.resolve("state/jobs/damaged.jsonl")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_bytes(b'{"job_id":"damaged","state":')

    try:
        result = read_job(root, "damaged")
    except Exception:  # noqa: BLE001 - the contract requires a loud failure, not a named type
        return
    pytest.fail(f"damaged journal without a legacy fallback returned silently: {result!r}")


@pytest.mark.parametrize("text", ["first\nsecond", "trailing-newline\n", "carriage-return\r"])
def test_append_line_rejects_text_containing_a_line_break(
    tmp_path: Path, text: str,
) -> None:
    root = ProjectRoot(tmp_path)
    with pytest.raises(ValueError):
        root.append_line("state/jobs/invalid.jsonl", text)
    path = root.resolve("state/jobs/invalid.jsonl")
    assert not path.exists(), f"rejected append unexpectedly created or modified {path.name}"
