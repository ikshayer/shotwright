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
