"""Conformance tests for the append-only job journal contract."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from orchestrator.api.app import OperationContext
from orchestrator.domain.entities import Job
from orchestrator.jobs.queue import JobQueue
from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.store.json_store import ProjectStore

TERMINAL = {"completed", "failed", "cancelled"}
ALLOWED_STATES = {"queued", "running", *TERMINAL}


def _snapshot(job_id: str, state: str, *, attempts: int = 0) -> dict[str, Any]:
    return Job(job_id=job_id, type="flux_still", state=state, attempts=attempts).model_dump(mode="json")


def _reports(root: ProjectRoot) -> list[dict[str, Any]]:
    # Exercise the existing API reader without requiring unrelated app setup.
    return OperationContext._read_job_reports(object(), "project", root=root)


def _by_id(root: ProjectRoot, job_id: str) -> dict[str, Any]:
    matches = [item for item in _reports(root) if item.get("job_id") == job_id]
    assert len(matches) == 1, f"expected exactly one report for {job_id}, observed {len(matches)}"
    return matches[0]


def _append(root: ProjectRoot, job_id: str, state: str, *, attempts: int = 0) -> Path:
    return root.append_line(
        f"state/jobs/{job_id}.jsonl",
        json.dumps(_snapshot(job_id, state, attempts=attempts), sort_keys=True),
    )


@pytest.mark.parametrize(
    ("first_state", "later_state", "expected"),
    [
        ("cancelled", "completed", "cancelled"),
        ("completed", "cancelled", "completed"),
    ],
)
def test_cancel_and_worker_transition_resolve_by_forced_append_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    first_state: str, later_state: str, expected: str,
) -> None:
    """Two real threads append in a gate-forced order; resolution cannot depend on scheduling."""
    root = ProjectRoot(tmp_path)
    queue = object.__new__(JobQueue)
    queue.root = root
    queue.store = ProjectStore(root)
    first_done = threading.Event()
    start = threading.Event()
    errors: list[str] = []
    original_append = root.append_line

    def ordered_append(relative: str, text: str) -> Path:
        payload = json.loads(text)
        state = payload["state"]
        if state == later_state and not first_done.wait(5):
            raise TimeoutError(f"{later_state} append did not observe {first_state} first")
        result = original_append(relative, text)
        if state == first_state:
            first_done.set()
        return result

    monkeypatch.setattr(root, "append_line", ordered_append, raising=False)
    first_job = Job(job_id="race", type="flux_still", state=first_state)
    later_job = Job(job_id="race", type="flux_still", state=later_state)

    def cancel_writer() -> None:
        try:
            if not start.wait(5):
                raise TimeoutError("race start gate was not released")
            # This is the cancel-side transition at the shared queue persistence seam.
            queue._transition(first_job, first_state)
        except BaseException as exc:  # noqa: BLE001 - re-asserted on the main thread
            errors.append(f"cancel writer: {type(exc).__name__}: {exc}")

    def worker_writer() -> None:
        try:
            if not start.wait(5):
                raise TimeoutError("race start gate was not released")
            queue._transition(later_job, later_state)
        except BaseException as exc:  # noqa: BLE001 - re-asserted on the main thread
            errors.append(f"worker writer: {type(exc).__name__}: {exc}")

    # Name the first-terminal side explicitly: the worker is first for completed-first;
    # cancellation is first for cancelled-first. Both writers still run in real threads.
    workers = {
        first_state: cancel_writer,
        later_state: worker_writer,
    }
    first_thread = threading.Thread(target=workers[first_state], name=f"{first_state}-writer")
    later_thread = threading.Thread(target=workers[later_state], name=f"{later_state}-writer")
    first_thread.start()
    later_thread.start()
    start.set()
    first_thread.join(timeout=7)
    later_thread.join(timeout=7)

    assert not first_thread.is_alive() and not later_thread.is_alive(), (
        f"race threads did not finish within 7 seconds; first_alive={first_thread.is_alive()}, "
        f"later_alive={later_thread.is_alive()}"
    )
    assert not errors, f"race writer raised unexpectedly: {errors}"
    lines = root.resolve("state/jobs/race.jsonl").read_bytes().splitlines()
    assert len(lines) == 2, f"expected both race entries to remain, observed {len(lines)} lines"
    assert [json.loads(line)["state"] for line in lines] == [first_state, later_state]
    resolved = _by_id(root, "race")
    assert resolved["state"] == expected, (
        f"forced {first_state}-then-{later_state} order resolved as {resolved.get('state')!r}; "
        f"expected first terminal state {expected!r}"
    )
    assert resolved["state"] in ALLOWED_STATES, f"resolver returned unrecognized state {resolved['state']!r}"


def test_partial_unparseable_final_line_is_ignored(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    _append(root, "partial", "running")
    journal = root.resolve("state/jobs/partial.jsonl")
    # Simulate process interruption mid-append; append_line itself always terminates a line.
    with journal.open("ab") as stream:
        stream.write(b'{"job_id":"partial","state":"completed"')
    assert _by_id(root, "partial")["state"] == "running", (
        "a truncated final record changed the resolved state or made the read fail"
    )


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (["failed", "cancelled"], "failed"),
        (["cancelled", "running"], "cancelled"),
        (["queued", "running"], "running"),
    ],
)
def test_first_terminal_wins_and_nonterminal_cannot_resurrect(
    tmp_path: Path, entries: list[str], expected: str
) -> None:
    root = ProjectRoot(tmp_path)
    for state in entries:
        _append(root, "precedence", state)
    assert _by_id(root, "precedence")["state"] == expected


def test_later_appends_preserve_earlier_entry_byte_for_byte(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    path = _append(root, "append-only", "queued")
    original = path.read_bytes()
    _append(root, "append-only", "running", attempts=1)
    _append(root, "append-only", "completed", attempts=1)
    content = path.read_bytes()
    assert content.startswith(original), "a later transition changed the bytes of the earlier entry"
    assert content[len(original):].count(b"\n") == 2, "later transitions did not append two complete lines"


def test_concurrent_appends_remain_parseable_while_reader_resolves(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    _append(root, "concurrent", "queued")
    start = threading.Event()
    writers_done = threading.Event()
    failures: list[str] = []
    observations: list[str] = []
    iterations = 100

    def writer(state: str) -> None:
        try:
            if not start.wait(timeout=5):
                raise TimeoutError("append start barrier timed out")
            for index in range(iterations):
                _append(root, "concurrent", state, attempts=index)
        except BaseException as exc:  # noqa: BLE001 - re-asserted on the main thread
            failures.append(f"{state} writer: {type(exc).__name__}: {exc}")

    def reader() -> None:
        try:
            if not start.wait(timeout=5):
                raise TimeoutError("reader start barrier timed out")
            while not writers_done.is_set():
                state = _by_id(root, "concurrent")["state"]
                if state not in ALLOWED_STATES:
                    failures.append(f"reader resolved invalid state {state!r}")
                    return
                observations.append(state)
            # Always take a final read, including when both writers finish very quickly.
            observations.append(_by_id(root, "concurrent")["state"])
        except BaseException as exc:  # noqa: BLE001 - re-asserted on the main thread
            failures.append(f"reader: {type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=writer, args=(state,), name=f"append-{state}")
        for state in ("queued", "running")
    ]
    reader_thread = threading.Thread(target=reader, name="journal-reader")
    for thread in [*threads, reader_thread]:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=10)
    writers_done.set()
    reader_thread.join(timeout=5)

    alive = [thread.name for thread in [*threads, reader_thread] if thread.is_alive()]
    assert not alive, f"concurrent append/read threads exceeded bounded waits: {alive}"
    assert not failures, f"concurrent append/read observed invalid behavior: {failures}"
    assert observations, "reader did not observe any resolved journal state"
    lines = root.resolve("state/jobs/concurrent.jsonl").read_bytes().splitlines()
    assert len(lines) == 1 + 2 * iterations, (
        f"expected {1 + 2 * iterations} complete records including the seed, observed {len(lines)} lines"
    )
    for number, line in enumerate(lines, start=1):
        try:
            snapshot = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            pytest.fail(f"journal line {number} was not parseable after concurrent append: {exc}")
        assert snapshot["state"] in ALLOWED_STATES, (
            f"journal line {number} contains unknown state {snapshot['state']!r}"
        )


def test_legacy_record_is_read_without_rewriting_and_reported_with_journal_once(tmp_path: Path) -> None:
    root = ProjectRoot(tmp_path)
    legacy_bytes = (json.dumps(_snapshot("legacy", "completed"), indent=2) + "\n").encode("utf-8")
    legacy_path = root.resolve("state/jobs/legacy.json")
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(legacy_bytes)
    _append(root, "journal", "running")

    reports = _reports(root)
    ids = [item.get("job_id") for item in reports]
    assert sorted(ids) == ["journal", "legacy"], f"expected legacy and journal jobs once each, got {ids!r}"
    assert legacy_path.read_bytes() == legacy_bytes, "reading the legacy record rewrote its original bytes"
    assert _by_id(root, "legacy")["state"] == "completed"


@pytest.mark.parametrize(
    "relative",
    [
        "../outside.jsonl",
        "state/jobs/../../outside.jsonl",
        "\\\\server\\share\\job.jsonl",
        "C:\\outside\\job.jsonl",
        "state/jobs/CON.jsonl",
    ],
)
def test_append_line_rejects_the_same_escaping_and_reserved_paths_as_write_text(
    tmp_path: Path, relative: str
) -> None:
    root = ProjectRoot(tmp_path)
    with pytest.raises(ContainmentError):
        root.write_text(relative, "x")
    with pytest.raises(ContainmentError):
        root.append_line(relative, "x")
