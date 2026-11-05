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
