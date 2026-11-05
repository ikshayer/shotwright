# Job journal design

`orchestrator/jobs/journal.py` owns the job snapshot format, append operation,
reader, and state precedence. Queue persistence and API reporting call this module;
the platform layer only supplies `ProjectRoot.append_line` and has no job-specific
knowledge. The precedence rule appears only in `read_job`.

`append_job` writes one complete, compact JSON snapshot per line to
`state/jobs/<job_id>.jsonl`. `ProjectRoot.append_line` resolves the path through the
existing containment check, then serializes append calls with a process-wide lock,
opens in append mode, writes the UTF-8 line and newline, flushes, and closes. The lock
is needed because plain concurrent `open("a")` calls on this Windows machine lost
appends. It coordinates threads in this Python process; it does not coordinate
independent processes writing the same journal.

## Measured concurrent append

The first two-thread run used synthetic JSON records with `job_id`, `type`, `state`, a
writer/sequence marker and a 128-byte payload, plus a concurrent reader using
`read_job`. Plain append mode produced 19,766 physical lines instead of 20,001,
including 272 blank lines. The records that remained parseable were not split
internally, but the run showed lost appends and gaps, so plain append mode failed the
check.

After adding the process lock, the same workload produced **20,001 complete lines**
(one initial `Job` snapshot plus 20,000 concurrent JSON records), zero blank or invalid lines,
and zero reader errors across 905 resolutions. The two writer threads each appended
10,000 snapshots; measured line sizes were 243–247 bytes. Every resolved record had a
valid `queued` or `running` state and intact payload. The completed run took about
2 minutes 40 seconds on this machine. The scratch script and its project-local data
were removed after the run.

## Resolution and legacy records

`read_job` reads complete JSON lines in order, ignoring a final line that cannot be
decoded as UTF-8 or JSON. It returns the most recent snapshot's other fields, while
the state is the first terminal state seen (`completed`, `failed`, or `cancelled`),
or the last non-terminal state (`queued` or `running`) if no terminal state appears.
Entries after a terminal state remain in the file and cannot change the resolved
state. Complete records with invalid state vocabulary are errors. If the only line is
an incomplete fragment, the reader returns `None` because there is no complete
snapshot to resolve. The returned record is the selected complete snapshot: the first
terminal snapshot, or the final non-terminal snapshot. This keeps its other fields
consistent with its resolved state.

When `<job_id>.jsonl` is absent, `read_job` reads the existing
`state/jobs/<job_id>.json` directly and does not rewrite it. `report_jobs` enumerates
both `*.json` and `*.jsonl`, deduplicates by file stem, and the journal reader prefers
the `.jsonl` record when both exist.

The report reader normalizes IDs with the host OS path-case rules before deduplication,
so differently cased stems cannot duplicate one Windows job.

## Repository call-site mismatch

This checkout has `JobQueue` in `orchestrator/jobs/queue.py`, but no
`orchestrator/service/runner.py`, `JobRunner`, `cancel` method, or `_persist_job`
implementation. Accordingly there was no cancel-side writer or load to migrate. The
available queue writer and `report_jobs` reader use the journal owner. The contract
does not specify what a report should do when a journal exists but has no complete
snapshot; this implementation omits that job (`read_job` returns `None`) and does not
fall back to a legacy file when a journal exists.

One recovery edge remains unresolved by the contract: if a process crashes mid-line,
then a later append writes immediately after those bytes. The damaged tail can swallow
that next snapshot, and a subsequent append can turn the damaged line into a malformed
interior line that raises `JobJournalError`. Readers correctly ignore a malformed final
line, but the contract does not say whether a later writer should repair/truncate that
tail (which changes the append-only bytes), create a continuation journal, or refuse the
append. No choice was made silently in this implementation.
