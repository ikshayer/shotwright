"""Two ways a job record lied about what happened.

Both defects were found by reading the records of a real pipeline run, and both matter
because the Phase 1 acceptance shot is judged by these records: a run that renders and
then fails must not report success, and a field naming a lease holder must mean it.

The journal tests go through the same reader `report-jobs` uses, because that is the
surface the defect was observed on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.api.app import OperationContext
from orchestrator.domain.entities import Job
from orchestrator.jobs.journal import append_job
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind
from tests.jobs.test_lease import WORKER_PID
from tests.jobs.test_queue import build, make_job

# --- the journal must not report a completion that a failure falsified -----------------


def _report(root: ProjectRoot, job_id: str) -> dict[str, Any]:
    reports = OperationContext._read_job_reports(object(), "project", root=root)
    matches = [item for item in reports if item.get("job_id") == job_id]
    assert len(matches) == 1, f"expected one report for {job_id}, observed {len(matches)}"
    return matches[0]


def _append(root: ProjectRoot, job_id: str, state: str, **fields: Any) -> None:
    append_job(root, Job(job_id=job_id, type="flux_still", state=state, **fields))


@pytest.mark.parametrize("order", [("completed", "failed"), ("failed", "completed")])
def test_a_failed_entry_is_reported_over_a_completed_one_in_either_order(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    """`completed` promises an artifact; a `failed` entry says there is none."""
    root = ProjectRoot(tmp_path)
    for state in order:
        if state == "failed":
            _append(root, "falsified", "failed",
                    error_kind=JobErrorKind.CORRUPT_ARTIFACT.value,
                    error_message="frame 412 is truncated")
        else:
            _append(root, "falsified", "completed")

    report = _report(root, "falsified")

    assert report["state"] == "failed", (
        f"journal entries {order} were reported as {report['state']!r}; a completion that "
        "was followed by a failure produced no usable output"
    )
    # The failing snapshot itself must be reported, or the state is true and useless.
    assert report["error_message"] == "frame 412 is truncated", (
        f"the reported record carries no diagnosis: {report.get('error_message')!r}"
    )
    assert report["error_kind"] == JobErrorKind.CORRUPT_ARTIFACT.value


def test_both_entries_survive_in_the_journal(tmp_path: Path) -> None:
    """Resolution changes what is reported, never what was written: the log is append-only."""
    root = ProjectRoot(tmp_path)
    _append(root, "retained", "completed")
    _append(root, "retained", "failed", error_kind=JobErrorKind.INTERNAL.value,
            error_message="post-completion failure")

    lines = (tmp_path / "state" / "jobs" / "retained.jsonl").read_text(encoding="utf-8").splitlines()
    states = [json.loads(line)["state"] for line in lines if line.strip()]
    assert states == ["completed", "failed"], f"the journal no longer holds both entries: {states}"


def test_a_failure_after_a_cancel_is_reported(tmp_path: Path) -> None:
    """Teardown after a cancel can fail, and that is the case worth seeing.

    A cancelled job whose lease could not be released strands the GPU. The queue appends
    `failed` for it, and suppressing that in favour of the earlier `cancelled` entry left
    nothing in the record saying the lease was never given back. A failure *caused* by the
    cancel is reported as `cancelled`, not `failed`, so this entry is not an echo of it.
    """
    root = ProjectRoot(tmp_path)
    _append(root, "teardown", "cancelled", error_kind=JobErrorKind.CANCELLED.value)
    _append(root, "teardown", "failed", error_kind=JobErrorKind.INTERNAL.value,
            error_message="VRAM did not fall below the floor; lease remains held")

    report = _report(root, "teardown")

    assert report["state"] == "failed", (
        f"a failed teardown after a cancel was reported as {report['state']!r}, hiding a "
        "lease that was never released"
    )
    assert "lease remains held" in report["error_message"]


def test_several_failures_resolve_to_the_first_deterministically(tmp_path: Path) -> None:
    """Resolution must be a function of the journal's bytes, per the contract."""
    root = ProjectRoot(tmp_path)
    _append(root, "many", "completed")
    _append(root, "many", "failed", error_kind=JobErrorKind.OUT_OF_MEMORY.value,
            error_message="first failure")
    _append(root, "many", "failed", error_kind=JobErrorKind.INTERNAL.value,
            error_message="second failure")

    assert _report(root, "many")["error_message"] == "first failure", (
        "with several failures the earliest must be reported, or two reads of one journal "
        "could disagree"
    )


@pytest.mark.parametrize(
    ("first", "later", "expected"),
    [("cancelled", "completed", "cancelled"), ("completed", "cancelled", "completed")],
)
def test_the_cancel_completion_race_still_resolves_by_order(
    tmp_path: Path, first: str, later: str, expected: str
) -> None:
    """The ratified rule is untouched.

    First-terminal-wins exists for this race, where either answer is legitimate. Reporting
    a failure over a completion must not have turned it into a severity ordering, which
    would make a cancel beat a completion that genuinely finished first.
    """
    root = ProjectRoot(tmp_path)
    _append(root, "race", first)
    _append(root, "race", later)

    assert _report(root, "race")["state"] == expected, (
        f"{first} then {later} must report {expected}; the cancel/completion race is "
        "resolved by append order, not by severity"
    )


# --- lease_holder_pid must mean "holds it now" -----------------------------------------


def _snapshots(root: ProjectRoot, job_id: str) -> list[dict[str, Any]]:
    path = root.path / "state" / "jobs" / f"{job_id}.jsonl"
    assert path.is_file(), f"job {job_id} wrote no journal"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _states(snapshots: list[dict[str, Any]], state: str) -> list[dict[str, Any]]:
    return [snapshot for snapshot in snapshots if snapshot["state"] == state]


def test_a_completed_record_names_no_lease_holder(tmp_path: Path) -> None:
    env = build(tmp_path)
    job = make_job()

    assert env.queue.run(job, lambda _j: "artifact") == "artifact"

    snapshots = _snapshots(env.root, job.job_id)
    running = _states(snapshots, "running")
    completed = _states(snapshots, "completed")
    assert running and completed, f"expected running and completed snapshots, got {snapshots}"
    assert running[-1]["lease_holder_pid"] == WORKER_PID, (
        "a running job must name the worker holding the lease"
    )
    assert completed[-1]["lease_holder_pid"] is None, (
        f"the completed record still names pid {completed[-1]['lease_holder_pid']} after the "
        "lease was released"
    )


def test_a_requeued_record_between_attempts_names_no_lease_holder(tmp_path: Path) -> None:
    """The retry path released the lease too, and its snapshot claimed otherwise."""
    env = build(tmp_path)
    attempts: list[int] = []

    def flaky(_job: Job) -> str:
        attempts.append(1)
        if len(attempts) < 2:
            raise JobError(JobErrorKind.PROVIDER_RATE_LIMIT, "429")
        return "ok"

    job = make_job()
    assert env.queue.run(job, flaky) == "ok"
    assert len(attempts) == 2, f"the error was not retried: {attempts}"

    # Key on error_kind, not on the state alone. The first attempt also persists a `queued`
    # snapshot, so asserting over every queued entry passes even when the retry transition
    # is missing entirely -- the test would then prove nothing. Only the re-queue carries
    # the error that caused it.
    # Identify the re-queue by chronology, not by state: `queued` alone does not say whether
    # the lease was held. A pre-running persist is also `queued` and legitimately names the
    # holder, because the lease is acquired before it. Only the transition written AFTER a
    # release should name nobody, and that is the one following the first `running`.
    snapshots = _snapshots(env.root, job.job_id)
    running_at = next((i for i, s in enumerate(snapshots) if s["state"] == "running"), None)
    assert running_at is not None, f"the job never reached running: {snapshots}"
    requeue = next((s for s in snapshots[running_at:] if s["state"] == "queued"), None)
    assert requeue is not None, (
        "no queued snapshot follows the first running one, so the retry transition was "
        f"never written: {[s['state'] for s in snapshots]}"
    )
    assert requeue["error_kind"] == JobErrorKind.PROVIDER_RATE_LIMIT.value, (
        f"the snapshot found is not the re-queue: {requeue!r}"
    )
    assert requeue["lease_holder_pid"] is None, (
        "a job waiting to retry has released the lease, but its record names pid "
        f"{requeue['lease_holder_pid']}"
    )
    # The complement, which is what makes the field meaningful: before `running`, with the
    # lease already acquired, the snapshot DOES name the holder. The field tracks the lease,
    # not the state.
    held = [s for s in snapshots[:running_at] if s["state"] == "queued"
            and s["lease_holder_pid"] is not None]
    assert held and held[-1]["lease_holder_pid"] == WORKER_PID, (
        "no pre-running snapshot names the lease holder, so the pid is not recorded from "
        f"acquisition: {[(s['state'], s['lease_holder_pid']) for s in snapshots[:running_at]]}"
    )


def test_the_pid_is_recorded_from_acquisition_not_from_the_running_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease held through a failure before `running` must still name its holder.

    The pid used to be assigned after preflight, so a preflight failure whose release also
    failed produced a terminal record claiming no holder for a lease that was still held —
    the exact situation in which the field is the only thing an operator can act on.
    """
    env = build(tmp_path)
    job = make_job()

    # `run` calls preflight twice: once before the loop, where no lease is held, and again
    # after acquiring it. Only the second can fail while holding the lease, so the first
    # must pass or this test exercises the wrong path entirely.
    admitted = iter([True])
    original_preflight = env.queue.preflight

    def preflight_once(checked_job: Job) -> None:
        if next(admitted, False):
            original_preflight(checked_job)
            return
        raise JobError(JobErrorKind.DISK_EXHAUSTED, "free space fell away after admission")

    def refuse_release(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("VRAM did not fall below the floor")

    monkeypatch.setattr(env.queue, "preflight", preflight_once)
    monkeypatch.setattr(env.lease, "release", refuse_release)

    with pytest.raises(JobError):
        env.queue.run(job, lambda _j: "artifact")

    snapshots = _snapshots(env.root, job.job_id)
    assert not _states(snapshots, "running"), (
        "this test must fail before the running transition, or it is not testing the gap"
    )
    terminal = [s for s in snapshots if s["state"] in {"failed", "cancelled"}]
    assert terminal, "a refused job whose lease could not be released wrote no terminal record"
    assert terminal[-1]["lease_holder_pid"] == WORKER_PID, (
        "the lease was acquired and never released, so the record must name its holder; "
        f"observed {terminal[-1]['lease_holder_pid']!r}"
    )


def test_a_terminal_record_keeps_the_pid_when_the_lease_was_not_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of clearing on release, rather than on the way out.

    A measured release failure means the lease is still held. That terminal record is the
    only place an operator can learn which pid to clean up, so the pid must survive there.
    """
    env = build(tmp_path)
    job = make_job()

    def refuse_release(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("VRAM did not fall below the floor")

    monkeypatch.setattr(env.lease, "release", refuse_release)

    with pytest.raises(JobError) as caught:
        env.queue.run(job, lambda _j: "artifact")
    assert caught.value.kind is JobErrorKind.INTERNAL, (
        f"a refused release should surface as an internal failure, got {caught.value.kind}"
    )

    terminal = [s for s in _snapshots(env.root, job.job_id)
                if s["state"] in {"failed", "cancelled"}]
    assert terminal, "a job whose lease could not be released wrote no terminal record"
    assert terminal[-1]["lease_holder_pid"] == WORKER_PID, (
        "the lease was never released, so the terminal record must still name its holder; "
        f"observed {terminal[-1]['lease_holder_pid']!r}"
    )
