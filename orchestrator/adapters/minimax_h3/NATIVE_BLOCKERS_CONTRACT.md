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

---

# Amendment 1 — outcome, and a fifth blocker nobody knew about

Final state on `h3-blockers-verify`: **2055 passed, 58 skipped, 0 failed**; the blocker
file is **12 passed**. Independently re-verified by the orchestrator over the three
affected files: 80 passed.

## A1.1 — all four blockers closed, and verified against code that failed first

The test author wrote 11 cases blind; **10 failed against the unfixed tree and all now
pass**. That ordering is what makes them evidence rather than decoration.

## A1.2 — the socket owner is five descendants deep

The most useful measurement of this round. The implementer's own smoke failed with
"Windows reported a socket PID different from the `Popen` PID" and it shipped the
ownership check unverified in the positive direction. Measured chain:

```
Popen 50572 -> 63312 -> 96352 -> 48848 -> 86288 -> socket owner 90000
```

Matching the `Popen` PID alone would have **rejected our own server every time**, and the
adapter could never have run. Tree membership is not defensive over-engineering here; it
is the only approach that functions. The check is now confirmed to accept a legitimately
owned descendant listener and still refuse an unowned one.

**Still unverified:** this used a synthetic Python listener chain, not ComfyUI's actual
startup tree, which may nest differently. The real run is the first test of the real
ancestry. Because the check fails closed, the expected failure mode is a refusal on first
contact — treat that as ancestry to be understood, not as a new defect.

## A1.3 — blocker 1 was worse than this contract described

The contract assumed the 6 GiB initial / 2 GiB-across-3-samples runtime floors existed and
were merely being shadowed by the re-applied admission reservation. The test author found
the **initial RAM floor missing entirely**, with no stop after three sub-2-GiB samples.
Both now exist and are tested in both directions: a healthy run whose VRAM has fallen
because the model took it continues, and a genuinely starved run still stops.

## A1.4 — the H3 build had silently broken five supervision tests

Not inherited debt. Measured both sides: **68 passed at `f0e741a`** (pre-H3), **5 failed**
on the H3 tree. They went unnoticed because, as the handoff admits, no full repository
suite was ever run for this build.

They all guard **late execution after timeout or cancel**, and rearm while a lease release
is pending — the machinery that stops a worker beginning after it should have been killed.
Authorising a GPU run with those broken would have been the worst available combination.

**My diagnosis was wrong and is recorded as such.** I hypothesised the shared GPU slot;
the fixtures do not even construct a lease with one. The real causes:

- three failures: `_watch()` gained a `deadline_message` keyword the tests' three-argument
  wrappers do not accept;
- two failures: a synchronous `worker.terminate()` blocking inside `_watch()` before lease
  cleanup could start.

Fixed by preserving the three-argument `_watch` call and carrying the deadline message
through thread-local context, and by giving the Blender, ComfyUI and H3 supervisors a
nonblocking stop request. Lease cleanup still joins the worker and verifies shutdown
before releasing. No test was modified; no timeout, cancellation or lease-release
guarantee was weakened; unverifiable cleanup still retains the lease and slot.

No shared-slot leak and no order-dependent suite behaviour was found.

## A1.5 — gaps this contract left, for the next round

- **The report schema and error field names are undefined.** The test author assumed
  `primary_error` / `cleanup_error` and a top-level string `stopped.error`. Pin these.
- **psutil is the listener-identity mechanism.** Confirmed available (7.2.2) and already
  the house convention in `jobs/lease.py` and `jobs/queue.py`. This is now settled, but
  the contract should have said so rather than leaving it to be guessed.
- **Runtime VRAM and disk cutoffs are unspecified.** Only the RAM floors are stated. The
  implementer correctly declined to invent thresholds; they still need deciding.
- **"Newly frozen hash" was ambiguous** — source freeze or artifact receipt. The tests
  check the proxy receipt hash.

## Outstanding before any GPU run

- **Ruff is not clean: 19 findings.** Five are `BLE001` in the new test file's cleanup,
  where catching `BaseException` is deliberate and correct and wants a justified `noqa`.
  The other fourteen are real debt in H3 WIP production code — unused imports, an unused
  variable and `TRY203` in `service/minimax_h3.py`, plus `BLE001` in `service/runner.py`,
  `api/cli.py` and `jobs/queue.py`.
- The end-to-end GPU run itself remains **unauthorised and ungated by this work**. Nothing
  here has loaded a model, and the adapter has still never run on a GPU.
