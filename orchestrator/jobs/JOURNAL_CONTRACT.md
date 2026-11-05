# Contract — the job record becomes an append-only journal (invariant 1, invariant 3, FR-27)

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why, in one paragraph

A job record could be read back **empty**. `JobRunner.cancel` writes
`state/jobs/<job_id>.json` on the caller's thread while the worker writes the same file
through `JobQueue._transition`. Making writes atomic (now on `main`) stopped the
corruption but could not make the overwrite *succeed*: `ATOMIC_WRITE_CONTRACT.md`
Amendment 2 measured that on this machine rename-over-an-open-file is refused whatever
share mode the reader uses, and `MoveFileExW` and `ReplaceFileW` fail identically. So the
overwrite itself is the problem.

**Everything else the store writes is already per-version and append-only** under
invariant 3, so it is never replaced and never contends. `state/jobs/<job_id>.json` is the
one hot exception — rewritten on every transition, and precisely the file that was read
empty. This contract removes the exception rather than excepting it.

## The design

**One line per transition, appended, never overwritten.** `state/jobs/<job_id>.jsonl`.

- **Each line is a complete `Job` snapshot**, not a delta. Replaying deltas invites
  divergence between what happened and what is reconstructed; a full snapshot per line
  makes "read the last one" trivially correct. A `Job` is small and a job sees on the order
  of five to twenty transitions, retries included.
- **Appends go through the containment helper**, so `ProjectRoot` gains an append:

  ```python
  ProjectRoot.append_line(relative: str, text: str) -> Path
  ```

  It opens for append, writes one line terminated by `\n`, and closes. It resolves through
  the existing containment check with unchanged semantics. The repository already uses this
  shape — the adapters write `job.jsonl` per attempt — so this is a pattern being adopted,
  not invented.
- **This pattern already exists here. Read `orchestrator/adapters/comfyui/runner.py` and
  `orchestrator/adapters/postprofile/runner.py` for how they write `job.jsonl`** and stay
  consistent with it where it makes sense.

### Reading, and the rule that actually matters

A reader parses lines in order and must tolerate a **partial or unparseable final line**:
a crash or an interrupted append can leave one, and it is not corruption. Ignore it.

Then the state is resolved by **precedence, not by position**:

> Replay the entries in order. The reported state is the **first terminal state** reached
> (`completed`, `failed`, `cancelled`); if none has been reached, it is the **last**
> non-terminal state (`queued`, `running`).

This is the semantic point of the whole change, so be clear why: the original defect was a
cancel racing a completion. With a journal both entries survive, and "last line wins" would
make the outcome depend on thread scheduling. First-terminal-wins is deterministic and
right in both directions — cancel before the worker finished reports `cancelled`, and a
worker that finished before the cancel arrived reports `completed`. **Later entries are
retained** (append-only; nothing is deleted) but do not change the reported state.

The vocabulary is `queued`, `running`, `completed`, `failed`, `cancelled`, as
`JobQueue._transition` already uses. Do not invent a sixth.

### Legacy records

Existing projects contain `state/jobs/<job_id>.json`. A reader that finds no `.jsonl` must
fall back to reading that file, and **must not rewrite its bytes** — the same rule the
store already follows for schema-1 JSON. Note `glob("*.json")` does not match `*.jsonl`,
so the two cannot be confused; `report_jobs` must enumerate both and must not report a job
twice.

## What to change

- `ProjectRoot.append_line` in `orchestrator/platform/containment.py`.
- A journal owner in `orchestrator/jobs/` — the format, the append, and the precedence
  rule live in **one** place. Every writer and reader goes through it. The platform layer
  provides a contained append and knows nothing about job semantics.
- Migrate the writers: `JobQueue._transition`, `JobQueue._persist`, `JobRunner.cancel`,
  and `_persist_job` in `orchestrator/service/runner.py`.
- Migrate the readers: `JobRunner.cancel`'s load, and `report_jobs` in
  `orchestrator/api/app.py`.

`resolve`, `make_dir`, `write_bytes`, `write_text` and `write_with` keep their current
behaviour. The atomic write stays exactly as merged; it remains correct for the rare
genuine overwrite.

## Prove the append, do not assume it

An append of a short line is *usually* atomic, and "usually" is how this project has been
burned twice already. Amendment 1 of the atomic contract asserted a Windows mechanism that
Amendment 2 then measured to be false.

**Demonstrate, on this machine:** two threads appending concurrently to one journal for
enough iterations that interleaving would show, and a reader that must always resolve a
valid state. Report the measured result, not a citation. If appends can interleave within a
line, say so plainly — that is a finding, and it changes the design rather than being
worked around quietly.

## Test rules

- No GPU, Blender, ComfyUI, FFmpeg or network.
- **The regression test is the original defect:** a cancel concurrent with a worker
  transition, where the resolved state is always valid and never empty, and the artifact of
  the race is deterministic rather than schedule-dependent.
- Required cases: a partial final line is ignored rather than failing a read; first
  terminal state wins over a later terminal entry; a later non-terminal entry does not
  resurrect a terminal job; a legacy `.json` record is still readable and its bytes are
  unchanged; `report_jobs` lists a legacy and a journal job without duplication; a path
  escaping the root is refused by `append_line` exactly as the write methods refuse it.
- Assert the journal is **append-only**: an earlier entry is still present and byte-identical
  after later ones are appended.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline on `main`: **1181 passed, 29 skipped**, ruff clean, about 142 s. Every existing
test must keep passing; the queue, the runner, the API and the lease all touch job records.

Once this lands, the parked integration conformance branch `e73438c` — whose single failure
is the torn read this removes — is expected to go green. The parent confirms that; do not
test or merge that branch yourself.

## Amendment 1 — adjudication, and two measured findings

The implementer and the independent test author **agreed on the first combination**:
**1195 passed, 29 skipped**, ruff clean, 139.85 s against a ~142 s baseline. All fourteen
independently authored journal cases pass against an implementation their author never saw.
That had not happened once in the preceding seven rounds, and it is what a settled contract
looks like.

Two findings came out of the measurement this contract demanded, and both correct it.

### Appends are NOT atomic under concurrency — measured here

The contract said to prove the append rather than assume it, because "an append of a short
line is usually atomic" was the same species of claim that Amendment 1 of the atomic
contract got wrong. It was right to insist. Two threads, 10,000 records each, lines of
243–247 bytes, on this machine:

- **Plain concurrent `open("a")`: 19,766 physical lines instead of 20,001, including 272
  blank lines.** No record was split internally, but **appends were lost**.
- With a process-wide lock: 20,001 complete lines, no blank or invalid lines, and no reader
  errors across 905 resolutions.

So the premise behind choosing Option 1 — that append-only removes the need for writer
coordination — **is false.** Append-only removes the *rename*, which is what Windows
refused, but concurrent appends still need a lock.

**Binding:** the contained append serialises writers with a process-wide lock. This is not
a weakening of Option 1; it is Option 1 plus the coordination Option 2 would have needed
anyway, and it keeps the property that mattered — no overwrite, so nothing to tear and
nothing for Windows to refuse.

**Recorded limit:** that lock coordinates threads **in one process only**. It rests
entirely on the single-orchestrator-process contract in
`orchestrator/service/CONCURRENCY_CONTRACT.md`. A second writing process would lose
appends exactly as measured above, and `HANDOFF.md` already lists cross-process writers as
unverified. Anything that introduces one must revisit this.

### Crash-then-resume is unresolved, and the contract caused it

The contract said a partial final line is tolerable and must be ignored on read. That is
true but incomplete, and the implementer found what it leaves out: a later append **joins
onto** the unterminated fragment, losing that transition, and a further append turns the
damaged text into an invalid **interior** line, which then raises `JobJournalError`. So
tolerating a torn tail on read is not enough — a *writer* resuming after a crash has to
deal with it.

**Binding:**

- A writer must **seal an unterminated tail before appending**: if the journal does not end
  with `\n`, write one first, so the fragment becomes its own line instead of merging with
  the next record.
- Readers therefore **skip any unparseable line, not only the final one** — a crash
  legitimately produces an unparseable interior line, so that is damage to be stepped over,
  not a reason to fail the read. A skipped line must be **surfaced**, not swallowed: the
  reader reports that it happened, because invariant "never swallow an error" still applies
  and a damaged journal is something an operator should learn about.
- If `.jsonl` exists but contains **no complete snapshot**, fall back to a legacy `.json` if
  one is present; with neither, the record is damaged and that is a loud failure. The
  literal "fall back only when `.jsonl` is absent" rule was too narrow.

This was not implemented in that pass and **must not be reported as done.**

### Two concurrent terminal appends: the race is real and that is correct

Also raised: with first-terminal-wins, which of two simultaneous terminal entries lands
first is schedule-dependent. That is fine, and worth stating so nobody "fixes" it. If a
cancel and a completion genuinely race, **either outcome is legitimate** — the operator
cancelled at the moment the work finished. What must be deterministic is that **reading the
journal always yields the same answer**, which first-terminal-wins gives, and which the
test author pinned by forcing both orders explicitly.

### Ratified

- `orchestrator/jobs/journal.py` owns serialisation, reading and resolution, and the
  precedence rule has exactly one implementation.
- Legacy `.json` is read without rewriting its bytes; `report_jobs` enumerates both
  suffixes, prefers `.jsonl`, and de-duplicates using the host's path-case rules — which
  correctly accounts for Windows case-insensitivity.
- `append_line` must **reject text containing a newline**, as the test author asked: a
  literal newline would silently break the one-line-per-record invariant.

### The contract named a file that does not exist on this branch — my error

Both agents were told to migrate `JobRunner.cancel` and `_persist_job` in
`orchestrator/service/runner.py`. **That file is not on `main`;** it exists only on the
parked integration branch `e73438c`. So neither agent could migrate the cancel-side writer,
and the test author could not exercise the original cancel-versus-worker regression at all —
it substituted a `JobQueue._transition` call and said so plainly.

The journal work therefore covers `JobQueue` and `report_jobs` but **not** the cancel path
that produced the defect. Completing it means applying this journal on top of `e73438c`,
where `runner.py` lives, so the fix and the code it fixes land together. The integration
branch is parked solely on this defect, so they belong in one merge.
