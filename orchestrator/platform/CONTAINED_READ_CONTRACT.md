# Contract — a contained read for `ProjectRoot` (FR-25, invariant 1)

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

`ATOMIC_WRITE_CONTRACT.md` Amendment 1 records the finding that forces this work. Making
`write_bytes`/`write_text` atomic fixed torn content but produced a new failure, and the
independent test caught both halves:

```
reader raised PermissionError: [Errno 13] Permission denied: ...state/jobs/job.json
AtomicWriteError: replace was denied: [WinError 5] Access is denied
```

On Windows a rename over an open file is refused unless **every** open handle was created
with `FILE_SHARE_DELETE`, and Python's built-in `open()` does not set it. POSIX has no
equivalent problem: `rename` always succeeds and existing readers keep the old inode.

So writer-side atomicity cannot work alone on this platform, and no retry budget fixes it —
a reader in a loop always wins. `ProjectRoot` has **no read methods at all** today;
fourteen call sites do `root.resolve(rel).read_text()` for themselves, which is why there
is no single place to fix. The module needs a read surface to match its write surface.

## What to build

```python
ProjectRoot.read_bytes(relative: str) -> bytes
ProjectRoot.read_text(relative: str, *, encoding: str = "utf-8") -> str
```

Both resolve through the existing containment check — unchanged semantics, no second check
— and open the file in a way that **does not block a concurrent `os.replace`**.

### The mechanism is yours to choose, and yours to prove

On Windows the share mode must include `FILE_SHARE_DELETE` alongside read and write.
`os.open` and `open()` go through the CRT, which does not request it, so this likely needs
`CreateFileW` through `ctypes` and a handle adopted into a Python file object. On POSIX a
plain open is already correct and nothing exotic is wanted.

**Do not assert that your mechanism works — demonstrate it.** This project has repeatedly
been wrong about documented behaviour, and the whole reason this contract exists is that a
plausible-looking guarantee turned out to be false on this machine. Your report must show a
`os.replace` succeeding **while a read handle is open**, measured here, not cited.

### Also required

1. **Exception compatibility.** Callers already handle what today's reads raise. A missing
   file must still raise `FileNotFoundError`, and a directory, a permission problem and a
   decode failure must behave as they do now. Changing an exception type is a silent
   behaviour change for every caller that catches it — `json_store` and `app.py` both do.
2. **Newline handling must be settled deliberately, not inherited.** `Path.read_text()`
   performs universal-newline translation, so `\r\n` becomes `\n`. `write_text` pins
   `newline="\n"`. State which behaviour the new read has, and say what happens to a legacy
   file containing CRLF. If you change the effective behaviour of any existing call site,
   say so explicitly — anything that hashes content it read is sensitive to this.
3. **Bounded retry on a transient refusal**, then a loud classified failure, matching the
   write side's shape. No silent fallback to a plain open: that would reintroduce the
   defect this exists to remove.
4. **Migrate the production reads of contained project files** onto the new methods. The
   sites are:

   - `store/json_store.py:454, 623, 690, 721, 772`
   - `adapters/blender/runner.py:244, 352`
   - `adapters/comfyui/runner.py:302, 359, 407`
   - `api/app.py:131`
   - `jobs/queue.py:365`
   - `jobs/lease.py:333`
   - `platform/settings.py:226`

   **Two need judgement, and you must exercise it rather than migrate blindly.**
   `settings.py:226` reads `settings.toml`, which may live **outside** any project root and
   so may not be a contained read at all. `lease.py:333` reads the lease file, which
   Amendment 1 established must never be *atomically replaced* because it is guarded by an
   OS lock — reading it with a permissive share mode is still fine, but think about it and
   say what you concluded. Audit all fourteen, migrate the ones that are genuinely contained
   project reads, and **list the ones you did not migrate with the reason.**

5. `resolve()`, `make_dir()`, `write_bytes`, `write_text` and `write_with` keep their
   current behaviour. This pass adds a read surface; it does not revisit the write side.

## Acceptance

The parked test `tests/platform/test_atomic_writes.py::test_concurrent_writers_never_expose_partial_or_empty_content`
is currently **red** and is the acceptance criterion. Its reader holds a local
`_read_through_replace` retry helper that the parent added as an explicit stand-in for the
methods you are building. **You do not edit tests** — the parent will switch that reader
onto the real method and confirm it passes. Say in your report whether you expect it to.

## Test rules

- No GPU, Blender, ComfyUI, FFmpeg or network.
- The decisive test is a **real** one: a reader holding the file open while a writer
  replaces it, with both succeeding. Threads, bounded waits, a clear failure message.
- Required negative cases: a missing file raises `FileNotFoundError`; a path escaping the
  root is refused exactly as before (`..` traversal, absolute and UNC paths,
  drive-qualified paths, Windows reserved device names); a decode failure surfaces rather
  than returning mojibake; the retry path ends in a loud classified error, never a silent
  plain open.
- Assert that content read back equals content written, byte for byte, for both methods.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline on this branch: **1147 passed, 29 skipped, 1 failed** — the one failure being the
acceptance test above. Every other test must keep passing: fourteen call sites change, in
the store, both GPU adapters, the API, the queue and the lease, so the existing suite is
the real regression surface.

Report the suite's wall-clock time. Reads are far more frequent than writes, and a
`ctypes` call per read could cost more than the writes did.
