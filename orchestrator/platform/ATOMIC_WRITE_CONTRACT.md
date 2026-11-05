# Contract — atomic writes in `ProjectRoot` (FR-25, invariant 1)

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## The defect, with its evidence

`ProjectRoot.write_text` and `write_bytes` write in place. Two threads can therefore write
the same file at once and a reader can observe a partial or empty file. This is not
theoretical — it was reproduced by an independent test in the adapter integration pass:

```
pydantic_core.ValidationError: Invalid JSON: EOF while parsing a value
  at line 1 column 0 [input_value='']
```

The job record came back **empty**. `JobRunner.cancel` writes `state/jobs/<job_id>.json`
on the caller's thread while the worker thread writes the same file through
`JobQueue._transition`.

**Invariant 1 makes project-local JSON the source of truth, and SQLite only a rebuildable
index.** A source of truth that can be read back empty is the worst failure mode this
system has. The exposure is not specific to jobs: **every** caller of `write_text` and
`write_bytes` shares it, and AT-09 requires an exported package to reopen standalone.

## Scope

**In scope:** `ProjectRoot.write_bytes` and `ProjectRoot.write_text` become atomic.

**Explicitly NOT in scope, and this matters:** `ProjectRoot.write_with` must keep its
current behaviour. It is not a file-content writer — it hands an already-contained path to
something else, and its callers rely on receiving the *real* target path:

- `blender/runner.py:228` — `lambda path: path.mkdir(exist_ok=False)`
- `comfyui/runner.py:99` — `lambda p: p.mkdir(parents=True, exist_ok=False)`
- `blender/runner.py:271`, `comfyui/runner.py:206` — `lambda path: None`, reserving a
  destination an external tool will then write
- `postprofile/runner.py:151` — `lambda path: path.open("xb").close()`

Writing to a temp path and renaming would break every one of those. Document `write_with`
as **non-atomic by design**, and say that a caller needing atomicity must use
`write_bytes` or `write_text`. Do not add an atomic variant of `write_with` in this pass;
nothing needs one yet.

`resolve()`, `make_dir()` and every containment check keep their exact current semantics.
This pass changes **how** bytes land, not **where** they are allowed to land.

## Requirements

1. **No reader ever observes a partial file.** A read of the target returns either the
   complete previous contents or the complete new contents, never a mixture and never
   empty.
2. **Concurrent writers to one path leave one complete version.** Last writer wins. Torn
   content is a defect; a lost update is not, since no caller holds a read-modify-write
   lock through this API today.
3. **The temp artifact lives inside the project root.** Invariant 4 admits no exceptions,
   "including temp files and logs". A temp file outside the root — anything from
   `tempfile.NamedTemporaryFile` with a default directory — is a containment violation and
   an automatic failure of this pass.
4. **No enumeration may ever observe a temp artifact.** This is the subtle one. These call
   sites enumerate the tree and would be corrupted by a stray temp file:
   - `orchestrator/jobs/queue.py:130` — `glob("*")`, which matches **anything**
   - `orchestrator/store/json_store.py:451` — `glob("*.json")`
   - `orchestrator/api/app.py:192` — `glob("*.json")`
   - plus any `iterdir`/`rglob` caller — **audit them all and list what you found in your
     report.** `ProjectStore.list_projects` iterates the root directly.

   Choose the mechanism yourself and justify it: a dedicated directory, a name no pattern
   can match, or something else. State in your contract why no existing enumeration can
   see your temp artifacts, and prove it with a test.
5. **The replace must stay on one filesystem.** `os.replace` is atomic within a volume and
   fails across volumes on Windows. If temp files live anywhere other than the target's own
   directory, say why that is still one volume.
6. **Windows sharing violations are real.** `os.replace` raises `PermissionError` on
   Windows when another handle holds the target — an antivirus or indexer scan is enough.
   Retry a bounded number of times over a short, bounded window, then **fail loudly** with
   a classified error. Never fall back to a non-atomic write, and never swallow it: a
   silent fallback would reintroduce the exact defect this pass removes.
7. **A failed write leaves no debris and no corruption.** If anything fails, the target is
   untouched and the temp artifact is removed. A leftover temp must never be mistaken for a
   record.
8. **Legacy readability is preserved.** Schema-1 JSON without metadata stays readable
   without rewriting its bytes; this pass must not touch existing file contents.
9. **Text writes keep `encoding="utf-8"` and `newline="\n"`.** The current `write_text`
   pins both and the repository depends on LF endings.

## Deliberate non-goal: `fsync` and power-loss durability

Atomicity and durability are different problems. `os.replace` gives atomicity — no reader
sees a mixture — without any `fsync`. Durability against physical power loss additionally
needs the temp file's contents flushed before the replace, and on POSIX the directory
entry flushed after.

**This pass does NOT add `fsync`,** for two reasons. The defect being fixed is a torn read
from concurrency, which `os.replace` alone closes completely. And an `fsync` on every write
would land on a suite of 1177 tests that write constantly, for a guarantee the project
already lists as unverified — physical power loss and forced crash are recorded as untested
in `HANDOFF.md`.

Record this as an explicit limit, not as an oversight: **atomic, not durable.** If
power-loss durability is wanted later it is a separate, measured decision with a measured
cost.

## Test rules

- No GPU, Blender, ComfyUI, FFmpeg or network.
- **A regression test must reproduce the original defect**: concurrent writers to one path,
  with a reader that must never see partial or empty content. Without this the pass has not
  demonstrated it fixed anything. Drive it with real threads and enough iterations to have
  failed against the old implementation.
- Required negative cases: a temp artifact is never visible to `glob("*")` in any directory
  the system enumerates; a failed write leaves the target unchanged and no debris; a path
  escaping the root is still refused exactly as before; `write_with`'s callers still
  receive the real target path (`mkdir`, reserve-then-write, `open("xb")`).
- Assert the containment of the temp artifact itself, not just of the target.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline on `main`: **1147 passed, 29 skipped**, ruff clean. Every existing test must keep
passing — the whole system writes through this module, so the existing suite is the real
regression surface here, more than in any previous pass.

**Report the suite's wall-clock time before and after.** Every write in the system gains a
file creation and a rename. If that materially slows the suite, that is a finding worth
knowing, not a secret to keep.

Once this lands, the parked integration conformance branch (`e73438c`, currently 1 failed /
1177 passed) is expected to go green, because its one failure is the torn read above. The
parent will confirm that; do not try to merge or test that branch yourself.

## Amendment 1 — adjudication, and the finding that changes this pass

The implementation conformed to the mechanism requirements and the full suite passed on its
own branch: **1147 passed, 29 skipped**, ruff clean, with wall time **149.60 s** against a
**162.07 s** baseline, so the extra file creation and rename cost nothing measurable.

Combined with the independent tests, **one test fails, and it is right to fail.** It has
taught us that requirement 1 is not achievable the way this contract stated it.

### Requirement 4 was too strong: never MISINTERPRETED, not never observed

The test author reported requirements 3 and 4 as mutually unsatisfiable, and was correct.
Requirement 3 puts the staging artifact **inside** the root, because invariant 4 admits no
exceptions for temp files. A raw `rglob("*")` from the root therefore sees it by
definition. The contract also mis-stated the call site: `jobs/queue.py` uses `rglob("*")`
(now `os.walk`), not `glob("*")`.

**Binding restatement:** no enumeration may **misinterpret** a staging artifact as content.
Concretely the artifact must not be named like a record (no `.json` suffix), must not be
loadable as a project, and must be pruned from the disk-accounting walk. A staging
directory listing its own file is not a leak. Checked against what the enumerators actually
do with what they find:

- `queue.py` sums file sizes for the FR-26 ceiling and parses nothing. The implementation
  prunes the staging directory, which keeps accounting stable; the trade-off is that a
  large in-flight write is briefly uncounted. Accepted.
- `api/app.py:72` iterates the root, skips non-directories, and swallows `RecordNotFound`,
  so a staging entry is already tolerated.
- The `*.json` globs cannot see an artifact that is not named `*.json`.

The enumeration test has been reconciled to this and passes.

### Requirement 1 CANNOT be met by the writer alone on Windows

This is the finding. Requirement 1 said a read returns either the complete previous or the
complete new contents. `os.replace` guarantees that for *content* — but not for
*openability*, and on Windows it does not even guarantee the replace will happen. The
independent test produced both halves of the problem:

```
reader raised PermissionError: [Errno 13] Permission denied: ...state/jobs/job.json
AtomicWriteError: replace was denied: [WinError 5] Access is denied
```

- A reader opening the target mid-replace can fail with `PermissionError`.
- A replace attempted while a reader holds a handle fails with `WinError 5`.

**Root cause:** on Windows a rename over an open file is refused unless every open handle
was created with `FILE_SHARE_DELETE`, and Python's built-in `open()` does not set it. POSIX
has no equivalent problem — `rename` always succeeds and existing readers keep the old
inode.

No retry budget fixes this. The implementation's five attempts over ~150 ms is a reasonable
bound, but a reader in a loop will always win the race, and lengthening the budget only
converts a fast loud failure into a slow one.

**Consequence: this pass is half of the fix, and must not be merged alone.** It does strictly
improve matters — a loud `AtomicWriteError` is far better than a silently empty source of
truth — but it trades torn reads for refused writes, and both are failures.

**Binding, and required before merge:** `ProjectRoot` gains a contained read —
`read_bytes` / `read_text` — that opens with `FILE_SHARE_DELETE` on Windows, and the
repository's reads are migrated onto it. `ProjectRoot` currently has **no read methods at
all**: fourteen call sites do `root.resolve(rel).read_text()` themselves, which is why
there is no single place to fix today. Giving the module a complete read/write surface is
the right shape independently of this bug.

Until that lands, `test_concurrent_writers_never_expose_partial_or_empty_content` stays red
and this work stays unmerged. Its reader retries on `PermissionError` as a stand-in for the
helper; its torn-content assertion is unchanged and still has teeth, having observed **0
bytes** against the in-place implementation.

### Ratified

- **The staging mechanism:** `.projectroot-atomic-write/write-<32 hex>.tmp` inside the
  root, resolved through `ProjectRoot`, with redirected staging symlinks and junctions
  rejected and device IDs checked before replacement.
- **`write_with` stays non-atomic**, as the contract required.
- **The GPU lease lock file must never be atomically replaced.** The implementer found this
  unprompted and it is load-bearing: `os.replace` substitutes file identity, so a process
  holding an OS lock on the old file would be holding a lock on nothing. Lease
  initialisation now uses exclusive `write_with` creation. **Any future file guarded by an
  OS-level lock is in the same position** and must not be written atomically.
- **Bounded retry then loud failure**, with no non-atomic fallback. `AtomicWriteError` is
  the classified failure.

### Open, and not to be reported as done

- **The staging prefix is not reserved.** `ProjectRoot` still accepts caller paths under
  `.projectroot-atomic-write`, so a caller can collide with staging, and anything it writes
  there is excluded from disk accounting by the prune. Reserve the prefix in `resolve()`,
  the way Windows device names are already refused.
- **`json_store.py:875` has its own sibling-temp writer** using `write_with`, predating
  this work. It should be migrated onto the atomic write so there is one staging mechanism
  rather than two.
- **Requirement 7's cleanup guarantee is not absolute.** If the OS persistently denies
  deletion, the retries end in a loud error but a staging file may remain.
- `fsync` remains omitted by design: atomic, not durable.
- Native Windows sharing races and cross-volume replacement were not verified beyond what
  the suite exercises.

## Amendment 2 — the central premise is false, measured on this machine

Amendment 1 concluded that writer-side atomicity fails on Windows because readers open
without `FILE_SHARE_DELETE`, and that a contained read requesting it would let the replace
proceed. **That is wrong.** The contained-read pass was told to demonstrate the mechanism
rather than cite it, and the demonstration failed.

Measured on this machine's NTFS `D:` volume:

- the reader opened via `CreateFileW` with `FILE_SHARE_READ | FILE_SHARE_WRITE |
  FILE_SHARE_DELETE`, the handle adopted through `msvcrt.open_osfhandle`;
- while that reader was held, a **second handle requesting `DELETE` access opened
  successfully**, which proves the reader's share mode really was permissive;
- `os.replace(stage, target)` nevertheless failed with **`WinError 5`**;
- `MoveFileExW` and `ReplaceFileW` failed the same way;
- `DeleteFileW` **succeeded** against the same open file;
- `os.replace` succeeded as soon as the reader closed.

So rename-over-an-open-file is refused here regardless of the reader's share mode, while
*deleting* that same open file is permitted. Share flags are not the obstacle, and no
read-side change can rescue the write side. The read API and twelve call-site migrations
are sound and preserved on `worktree-cread` at `5b5f308`; the premise they were built to
serve is not.

Cost note from that pass: the suite ran **175.19 s** against roughly **150 s**, so a
`ctypes` call per read is not free.

### What this means

Atomic overwrite of a file that anything might be reading is not available on this
platform through any of the three rename APIs. The options are therefore about **avoiding
the overwrite**, not about perfecting it.

1. **Make mutable records append-only.** The torn-write problem only ever affected records
   that are *overwritten*. Everything the store writes is already per-version and
   append-only under invariant 3, so it is never replaced and never contends. The single
   hot exception is `state/jobs/<id>.json`, rewritten on every transition — which is
   exactly the file that was read back empty. Written as a journal (append one record per
   transition, read the last complete entry) there is no overwrite, so no rename, so no
   Windows refusal and no torn read. **This pattern already exists in the codebase:** the
   adapters write `job.jsonl` per attempt. Atomic replace stays for the rare genuine
   overwrite, where contention is negligible.
2. **Coordinate with a per-file lock** so reads and writes never overlap. Deterministic,
   with precedent in the GPU lease's cross-process lock, but it puts a lock on the hot path
   of the source of truth and brings its own failure modes.
3. **Accept retry-and-loud-failure on both sides.** Cheapest. Under realistic access
   patterns — readers are brief and occasional, not tight loops — retries would almost
   always succeed, and a loud `AtomicWriteError` is far better than silent corruption. But
   it is a probabilistic guarantee for a source of truth.
4. **Delete-then-rename.** `DeleteFileW` does succeed against an open file, so the target
   could be deleted and the stage renamed in. This trades "write refused" for "target
   transiently absent", which is worse: a reader would observe a missing record rather than
   an old one.

**Recommendation: option 1, with atomic replace retained everywhere else.** It dissolves
the problem instead of fighting the platform, aligns the one offending file with invariant
3 rather than excepting it, and reuses a pattern the repository already has. Option 3 is
the fallback if the job-record format must not change.

### Status

- `main` remains green and carries only the two contracts.
- The atomic write implementation plus Amendments 1 and 2 are on `atomic-combined`,
  **unmerged**, with the acceptance test red.
- The contained read is on `worktree-cread` at `5b5f308`, **unmerged**, with its own 26
  tests red on `worktree-cread-tests` at `8839e0f`.
- The integration conformance branch `e73438c` remains parked behind all of this.

Nothing here is to be reported as fixed. The original defect — a job record readable as
`''` under concurrent cancel and worker writes — is still present on `main`.
