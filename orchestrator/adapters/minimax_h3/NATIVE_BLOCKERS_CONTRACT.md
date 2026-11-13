# Contract — the four HIGH native release blockers

Written by the orchestrator, 2026-11-12. Implementation and tests come from **different
agents working in parallel**, neither reading the other's output. Where they disagree,
this file decides and the disagreement is reported, never patched away.

This is an **addendum** to `orchestrator/adapters/minimax_h3/CONTRACT.md`, which stands
in full including Adjudications A–E. Nothing here relaxes it.

## Standing facts this work must not contradict

- The reusable adapter is **INCOMPLETE** and **has never run on GPU**. Nothing in this
  round authorises a GPU run, a model load, an install, or a download.
- The creator-accepted H3 spike test is a **feasibility result for the mechanism**, not
  validation of this adapter. Do not cite it as evidence for adapter behaviour.
- The creator has confirmed the H3 mechanism works and the Qwen helper is useful as-is.
  Neither statement validates anything below.
- `prog-combined` stays at `f0e741a`. Work happens on `h3-adapter-wip`.

## Blocker 1 — admission reservation must not be re-applied at runtime

`native._check_resources` reapplies the configured `required_vram_gb` / `required_ram_gb`
on **every** generation and export poll. The model consumes its own reservation, so a
healthy run violates the spare-floor interpretation of its own admission figure and is
killed for succeeding.

**Required behaviour.** The operator reservation is an **admission** check only, evaluated
once before the model is launched. Runtime polling uses the **existing runtime floors** —
6 GiB available RAM initial, 2 GiB across 3 samples to stop — plus appropriate observed
floors. Admission and runtime are two different questions and must be two different code
paths.

**The test that proves it:** allocated VRAM falls (the model has taken it) while free
memory stays above the runtime cutoff — the run must continue. Today it aborts.

Do not weaken the spike safety limits to achieve this. Stricter outer bounds remain
permitted; silently looser ones do not.

## Blocker 2 — timestamps are shifted twice

`prepare_video` seeks `start_time + request.start_seconds` before the input, and FFmpeg
then applies the input-origin offset **again**. An actual shifted-PTS source fails. A fix
was in progress and is **unverified** — treat it as unfinished, not done.

**Required behaviour.** Normalise against the **video stream's own origin**, decode-side,
and trim to the requested interval. The requested interval must exist in full: no silent
pad, no silent shorten.

**The test that proves it:** a source whose container start_time and video stream
start_time differ, and differ from zero. Assert **both** the exact selected content **and**
the frame grid — frame count, fps, and the first and last decoded frame's identity — under
a newly frozen hash. A duration check alone does not prove the right frames were selected.

The existing frozen native hash predates this change. **Any prior frozen run is void for
this function** and must be redone.

## Blocker 3 — the listener on 8298 may not be ours

A fresh ready-file, a live coordinator and an RTX device name **do not prove** that
whatever is listening on 8298 belongs to the owned process tree. A bind-check / close /
launch race can leave a foreign listener in place, and the adapter would then queue work
to it — or worse, try to control or stop it.

**This is the blocker I rank first**, because it is the only one whose failure damages
something *outside* this pipeline.

**Required behaviour.** Immediately before `POST /prompt`, and before any own-server
control or stop action, verify the **actual listener's PID and process create-time** are a
member of the owned tree. If membership cannot be positively established, **fail** —
never queue to it, never interrupt it, never stop it. An unidentifiable listener is a
foreign listener.

**The test that proves it:** a **real local competing listener** on the port, not an HTTP
fake. HTTP-level fakes cannot exercise a PID-membership check and must not be the only
evidence. Assert the adapter refuses, that the competing process is left **running and
untouched**, and that the refusal names the ownership failure.

## Blocker 4 — a verified cleanup is not a successful outcome

`_stop_server` / `_cleanup_verified` does not reliably reject a nonzero supervisor
`returncode` or a `stopped.error` when physical cleanup was independently verified. The
two facts are being collapsed: "we confirmed nothing is still running" is not "the process
succeeded".

**Required behaviour.** Keep them separate. Physical cleanup verification continues to be
safe and sufficient for **releasing resources**. It is never sufficient for **declaring
success**. A nonzero return code or a recorded `stopped.error` fails the run, retaining
both the primary exception and the cleanup exception separately, as the contract already
requires elsewhere.

**The test that proves it:** controlled nonzero-returncode and `stopped.error` cases where
cleanup *did* verify. Assert the run fails, the error is retained, and resources are still
released.

## What is explicitly out of scope

Any GPU run or model load; the end-to-end validation run; the shared-slot and budget work
(already independently verified); the Qwen adapter; approvals; commits to `prog-combined`;
Poly Haven or Sketchfab; any change to the pinned Comfy/model trees.

## Test rules

- **No GPU, no model load, no network, no installs or downloads.**
- Blockers 1 and 4 may use the public `NativeBackend(root, settings, *, popen, http,
  resources)` seams from Adjudication A. Blocker 2 uses **real FFmpeg on CPU** with
  fake-root data and real decoded bytes — not mocks. Blocker 3 requires a **real local
  listener process**.
- Fixture substitutes are evidence of protocol only. Label them honestly: they do not
  certify native model behaviour, cleanup, or codecs.
- Every assertion carries a message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -B -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Run from the worktree. Note `-B`: a prior author wrote `__pycache__` outside `ProjectRoot`
by running `py_compile`; do not repeat that.

The last full-repository figure, **1833 passed / 50 skipped**, is **PRE-H3** and is not
validation of this build. There is no current full-suite baseline — establishing one is
part of this work, not an assumption going into it.

There is also a known stale expectation:
`tests/jobs/test_startup_watchdog.py::test_hard_deadline_takes_precedence_during_startup`
asserts `terminate_calls == []`, which contradicts the required safe hard stop. Adjudicate
it narrowly — **correct the expectation, do not weaken the safety behaviour.**

## Report what you could not do

Say explicitly what this contract left ambiguous and anything you believe it got wrong.
Every contract in this repository has needed amendments; twelve defects were found in the
FR-03 one. Assume this is wrong somewhere and say where. The admissions are the most
valuable part of the report.
