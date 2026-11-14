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

---

# Amendment 2 — Ruff clean, and what the lint debt actually was

Ruff: **19 findings to 0**, `orchestrator/` and `tests/`, under the mandated invocation.
No rule was disabled, no `[tool.ruff.lint]` section was added, and no test was modified
beyond the justification comments described below.

## A2.1 — A1.5 mischaracterised the debt in two directions

A1.5 called the fourteen production findings "real debt". That was partly wrong, and the
correction matters more than the count.

- **Five were not debt at all.** The `BLE001` sites in `api/cli.py`, `jobs/queue.py`,
  `service/runner.py` (x2) and `service/minimax_h3.py` are one deliberate pattern: a
  **secondary** operation — cleanup, a GPU-ledger charge, a diagnostic sidecar, a terminal
  receipt — caught broadly so that its failure **annotates** the primary error instead of
  masking it. `service/runner.py` already carried
  `# noqa: BLE001 - persist a classified transition failure.` two lines from one of them.
  The H3 additions simply failed to follow a convention the file had already established.
  Each now carries its own justification; the behaviour is unchanged.
- **Four were omitted from A1.5 entirely:** `I001` in `api/app.py`, `api/errors.py`,
  `api/routes.py` and `service/minimax_h3.py` — in every case an H3 import dropped into
  the wrong alphabetical slot. Fixed by `ruff --fix` (safe fixes only).
- **One was worse than "an unused variable".** See A2.2.

The five test `BLE001`s were correctly described. All five sit in a single `finally:`
whose purpose is to guarantee teardown and collect failures rather than let one failure
abort the rest; catching `BaseException` there is required, because a leaked listener on
8298 would block real work. They now say so.

## A2.2 — UNRESOLVED: the terminal path records no content hash

`persist_h3_terminal` computed `_sha256(report_path)` and **discarded it**. That is why
`F841` fired. Deleting the assignment is correct — but it exposes a real asymmetry, and
deleting the symptom is not resolving it:

- the **success** path hashes all three artifacts and carries `av_sha256`, `wav_sha256`
  and `report_sha256` on `H3Candidate`;
- the **terminal** (failed/cancelled) path records its report by **path only**, because
  `GenerationRun` has **no content-hash field at all**.

So a failed or cancelled run's report has no integrity receipt, and nothing detects it
being altered or truncated after the fact. Closing this needs a schema decision — a hash
field on `GenerationRun`, or an explicit ruling that terminal reports do not get receipts
— and inventing one here would be exactly the "fully-specified field nothing drives"
defect this repository has now produced nine times. **It is recorded, not fixed.**
`settings` and `resource_metrics` are the wrong homes for it; neither means provenance.

A comment at the deletion site points here, so the next author cannot quietly
reintroduce an unused digest in place of the decision.

## A2.3 — the no-op handler removed, and why it was safe

`persist_h3_terminal` wrapped its final `store.save(run, expected_version=0)` in
`except Exception: raise` (`TRY203`) under a comment claiming it preserved an
already-written record. The handler did nothing. The guarantee is real but comes from
elsewhere: `expected_version=0` makes the save create-only, and an earlier
`RecordNotFound` probe returns any existing record untouched. The handler is gone and the
comment now states where the guarantee actually lives.

## A2.4 — three incidental facts worth not rediscovering

- `orchestrator/api/errors.py` carries a **UTF-8 BOM**, and it is **pre-existing**:
  present at the pre-H3 baseline, before this build. Left as found; the autofix preserved it.
- Line endings are **not uniform**. `tests/adapters/minimax_h3/test_native_high_blockers.py`
  is CRLF; the production files touched here are LF. A patch written for one silently
  matches nothing in the other — it cost a failed edit pass here. Preserve each file's
  existing endings rather than normalising, which would bury a real diff in whitespace.
- The enabled rule set is far broader than `pyproject.toml` suggests (it sets only
  `line-length` and `target-version`, with no `select`, yet `BLE001`, `I001`, `TRY` and
  `PL` all fire). `E501` is **not** enforced, which is why long `noqa` justifications are
  acceptable and why existing lines already exceed 100 columns.

---

# Amendment 3 — the first authorised GPU run: Stage A passed, Stage B found a real defect

Creator authorised Stages A and B on 2026-11-13. **The adapter has now run against the
real pinned runtime for the first time.** Stage A passed. Stage B failed, on a blocking
adapter defect that no fixture could have found. The failure is the most valuable result
of this round, and it is **not** a resource or ownership failure.

## A3.1 — Stage A passed: real `verify_runtime()`, 79.9 s

Real hash verification, no model load, no GPU. All four pinned models verified:

| role | file | bytes |
|---|---|---|
| diffusion | `MiniMax-H3_Ref2VA-NVFP4.safetensors` | 11,663,443,448 |
| text_encoder | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 15,687,142,551 |
| video_vae | `minimax_h3_video_vae_int8_convrot.safetensors` | 2,811,065,184 |
| audio_vae | `minimax_h3_audio_vae_fp32.safetensors` | 605,254,808 |

**28.65 GB of weights against 11.94 GB of VRAM**, so whole-model residency is impossible
on this card by arithmetic. Seven provenance receipts resolved (catalog, controller,
helper, plugin, source, easy-stage, model). Pinned ComfyUI is **0.44.0** at source commit
`2c75dc1b379a59e0880619d029607af5169a62ef`, living under
`spikes/22_minimax_h3_workflows/_work/local_runtime/source/ComfyUI` — **`D:\ComfyUI_3` was
never touched**, as required.

## A3.2 — BLOCKING DEFECT: `_validate_live_schema` cannot see dynamic-combo sub-inputs

```
H3SetupError: Installed schema for SaveVideo lacks inputs:
  ['format.codec', 'format.codec.encoding']
```

`native.py::_validate_live_schema` computes `known = set(required) | set(optional)` from
`/object_info` and rejects any graph input not in that **flat** set. But the adapter's graph
carries dotted sub-input names, and ComfyUI 0.44.0 exposes `SaveVideo.format` as a
`COMFY_DYNAMICCOMBO_V3` whose sub-inputs live nested under
`format[1]["options"][k]["inputs"]["required"]`. Those dotted names therefore **can never
appear** in the flat set, so this check cannot pass — the run dies before `POST /prompt`.

**The spike helper already solves this and the adapter reimplemented it without the
expansion.** `spikes/22_minimax_h3_workflows/local_h3.py` (lines ~801-816) expands both
shapes before comparing:

```python
mp4 = next(option for option in formats[1]["options"] if option["key"] == "mp4")
available["format.codec"] = mp4["inputs"]["required"]["codec"]
# and for COMFY_AUTOGROW_V3:
available["ref_videos.ref_video_0"] = template["input"]["required"]["ref_video"]
```

This is duplicated logic that drifted — the same defect shape as the eight seam breaks
already recorded, in a new costume. Two layers of drift, not one: the adapter also expects
`format.codec.encoding`, which `local_h3.py` does **not** synthesise, so the expansion must
be **recursive** (a dynamic combo nested inside a dynamic combo), not one level deep.

**Fix direction, not yet decided:** the expansion must exist in exactly one place and both
callers must use it. Reimplementing it a third time inside `native.py` would guarantee the
next drift. This needs a contract decision before code.

> **CORRECTION, same day — read `SCHEMA_VALIDATION_CONTRACT.md` instead of the paragraphs
> above.** Reading the pinned ComfyUI source (`comfy_extras/nodes_video.py` lines 76-166)
> settled three things this section got wrong:
>
> 1. **The adapter's graph is correct.** `format`, `format.codec`,
>    `format.codec.encoding` and the bare `codec` are *all* legitimate inputs in 0.44.0 —
>    `format` nests `codec` nests `encoding` nests `crf`, and `codec` really is a
>    top-level optional hidden input, which is why the error named the dotted pair but not
>    `codec`. Do not "fix" this by trimming the graph.
> 2. **`local_h3.py` is not the reference and must not be shared from.** Its expansion is
>    one level only, so it could not validate `format.codec.encoding` either. It is a
>    subset, not the solution, and its SHA-256 is recorded in every provenance receipt —
>    editing it changes that receipt. "Both callers must use it" was wrong.
> 3. The real requirement is **value-directed recursion**: a dotted input exists only
>    because of the selection made at the level above it. A flatten-all-options fix would
>    accept `format.codec.encoding` under a codec that does not offer it, which is a
>    weakening disguised as a fix.
>
> The characterisation of this as a drifted duplicate was half right: the duplication is
> real, but recursion is genuinely new work, not a copy job.

## A3.3 — the real process tree is THREE deep, not five

A1.2 flagged as unverified that the synthetic chain might not match ComfyUI's real
ancestry. **It does not.** Measured from `cleanup.outer_tree_before_stop`:

```
150984 (root) -> 39960 -> 25388 (ComfyUI main.py) -> {37596, 150752}
                        \-> 44548
```

Six processes, with ComfyUI's main process **two levels below the outer root** — against the
**five** levels Amendment 1 measured synthetically. A check that handles five handles three,
so the depth is not a problem; but the synthetic chain was **not representative**, and
anyone reasoning from A1.2's figure would be wrong.

**Still unverified, and it is now the top open risk:** `listener_before_prompt` is **null**.
The ownership check fires immediately before `POST /prompt` and the schema defect kills the
run earlier, so **the socket-owner PID was never matched against the owned tree**. The
binary question — can the adapter recognise its own server — remains unanswered. The tree
resolves and is fully identified (`identity_state: "verified_live"` for all six), so the
remaining risk is lower than before, but it is not zero and not closed.

## A3.4 — cleanup and the stop path are validated against reality

This is what fixtures could never prove, and it worked:

- `outer_tree_before_stop`: all six processes `verified_live`.
- `outer_tree_after_exit`: all six `not_live_or_identity_mismatch` — **every process dead**.
- `outer_tree_had_live_descendants: false`; `watchdog_joined: true`;
  `outer_tree_cleanup.events: []` (nothing left to kill).
- Independently confirmed after the run: **port 8298 free, no surviving pinned-runtime
  python, VRAM back to its 1,086 MiB baseline.**

**Blocker 4 is validated in the real path.** `physical_cleanup_verified: true` with
`supervisor_returncode: 0` and `stopped_error: null`, yet the outcome is still recorded as
`NativeCleanupError("shutdown outcome failed")` — cleanup verification did **not** get
collapsed into success, and both the primary and the cleanup error are retained separately.

## A3.5 — blocker 1's ordering is correct in the real path

The report lists resource phases as `runtime_initial, runtime, runtime, admission,
runtime`, which looks like admission running fourth. It is not. By timestamp, every sample
including `admission` (17:33:34) precedes the server launch (`baseline` 17:33:35.678), and
`runtime_initial` at 17:32:34 belongs to the FFmpeg proxy stage ~60 s earlier. **Admission
ran once, before launch, and passed.** Do not "fix" this ordering.

## A3.6 — measured, and what is still unmeasured

Measured:

- **ComfyUI startup to ready: 166.3 s** (`started_utc` 17:33:35.69 → `ready_utc`
  17:36:21.98), against `startup_allowance_seconds = 600`. Adequate, but only ~3.6x
  headroom, and this was with `--cache-none` and **no model load**. A real load will eat
  into that; do not treat 600 as proven.
- Whole run to failure: 233.2 s.
- RAM available stayed **13.85-15.00 GB throughout** and did not fall; server startup is
  cheap in RAM.
- Launch flags confirm good containment: `--offline --disable-all-custom-nodes
  --whitelist-custom-nodes ComfyUI-MiniMaxH3-Easy --cache-none`, with every directory
  redirected into the managed session.

**Still unmeasured, because the model never loaded** (VRAM peaked at 1,310 MiB, barely over
the 1,028 MiB idle baseline):

- `required_vram_gb`, `required_ram_gb`, `required_disk_gb`, `throughput_floor_per_second`
  — **the primary objective of the run, and it was not achieved.** The provisional
  `8.0 / 8.0 / 10.0 / 0.01` used here are admission-passing placeholders, nothing more.
- The runtime 2-GiB floor was never stressed.
- Blocker 2's timestamp handling was exercised only as far as the proxy stage.

## A3.7 — A1.5's report-schema gap is now closed by observation

The test author **assumed** `primary_error` / `cleanup_error`. The implementation uses
exactly those names, plus `cleanup`, `listener_before_prompt`, `submission_ambiguous`,
`resource_samples`, `seed`, `runtime` and `log_path`. `stopped_error` exists nested inside
`cleanup_error`, not at top level as A1.5 guessed. The assumption was right; it is now
evidence rather than a guess.

## A3.8 — a deliberate stop is recorded as a cleanup failure

The only reason the shutdown outcome failed is
`watchdog_failure: InterruptedError("Creator requested session stop")` — our own requested
stop after the primary error. Resources released correctly, so this fails in the safe
direction, but **every aborted run will now carry a cleanup failure that did not happen.**
That is noise which can mask a genuine one. Worth distinguishing a requested stop from an
unexpected watchdog interruption.

## A3.9 — what Stage B did NOT exercise, and why

`compose_h3_request` binds to a hash-verified `MASTER`/`DEGRADED` `ShotOutput`. **There is
no `projects` directory and no `settings.toml` anywhere on this machine**, so that path
would have required building the entire Phase 1 render chain first — itself unaccepted
(§4.3 criterion 5, FR-04). Stage B therefore drove `NativeBackend.execute()` directly with
the spike's real 8 s source (1024x768 h264, 24 fps, 192 frames, `start_time` 0).

**The service-layer binding was not exercised and is not claimed.** That is a coverage gap,
not a pass. It needs no GPU, so it can be closed separately.

Harness and artifacts: `_work/h3_stage/` (gitignored) — `settings.toml` with provisional
reservations, `stage_a_verify.py`, `stage_b_run.py`, and the run receipt under
`_work/h3_stage/projects/generated/h3/stageb/v0001/stagebrun1/`.

# Amendment 4 — the creator's four decisions, and two things we had wrong

Recorded 2026-11-13, after the creator answered the open decisions from Amendment 3.
Two entries below correct earlier text in this document rather than extending it.

## A4.1 — A2.2 RESOLVED: `output_hashes` on `GenerationRun`

**Decision (creator): add the field.** A terminal report now carries a receipt.

`GenerationRun` gains `output_hashes: dict[str, str]`, keyed by the same relative path
`output_paths` uses. Two validators, both load-bearing:

- digests must be 64 lowercase hexadecimal characters, matching `H3Candidate`'s existing
  convention rather than inventing a second one;
- **keys must be a subset of `output_paths`.** Without this the two fields drift and a
  receipt can name a path nothing resolves back to a file — a field that looks driven and
  is not, which is the exact defect shape this repository has produced nine times.

It defaults to an empty mapping because `GenerationRun` is the *generic* run entity:
FFmpeg and Blender runs share it and may have nothing to receipt. A required field would
have broken them.

**It is driven in the same change**, which was the whole point of taking the decision
rather than adding the field: `persist_h3_terminal` hashes the report and records
path-to-digest. The digest is taken from the bytes on disk, not from re-serialising the
parsed `report` object — a receipt for a re-encoding would not detect the file itself being
altered, which is the only thing it exists to detect.

**A coverage hole found on the way in.** `persist_h3_terminal` had **no tests of any kind**,
despite being reachable from `runner.py:405` and `:431` on both the failed and cancelled
paths. `tests/service/test_h3_terminal_receipt.py` (17 tests) is the first thing pinning its
contract, including that it stays create-only on a retry.

**One test trap, recorded so it is not repeated.** Two of the rejection tests passed
*before* the implementation, for the wrong reason: `GenerationRun` is strict, so
`output_hashes` was refused as an unknown field, not by the validator under test. The
discriminator is `test_a_well_formed_receipt_is_accepted`, which failed beforehand. A
rejection test alone cannot distinguish "validated" from "field does not exist".

## A4.2 — CORRECTION to A3.8: the defect was an always-true gate, not a classification

**A3.8 as written above is wrong about the cause.** It says the shutdown outcome failed
because of the requested-stop `InterruptedError`, and suggests distinguishing a requested
stop from an unexpected interruption. That work had **already been done** — both
`_cleanup_verified` and `_cleanup_failure_record` carried a `requested_stop` exclusion
before the GPU run, added in `ad4452b`.

The guard worked. The real defect was downstream of it, in the `finally` block of
`execute()`, which called `_cleanup_failure_record` and then gated on the truthiness of the
record it returned. That function *always* returns a populated dict, so the gate could
never be false. **Every run that produced a cleanup receipt was recorded as having failed
cleanup**, whether or not anything failed.

The proof is in the GPU run's own report: the message is `shutdown outcome failed`, which
is the fallback used when the collected reason list is **empty**. The guard had correctly
removed the only candidate reason; the truthiness test recorded a failure regardless.

The gate is now `_cleanup_verified`, the same predicate the post-`finally` check uses, so
both sites ask one question. The block cannot be folded into that check: when the primary
error is propagating, the `finally` exits by re-raising and the check below is never
reached, so this is the only place a failing run captures outcome evidence.

`test_a_clean_shutdown_is_not_recorded_as_a_cleanup_failure` reproduces the GPU run's
record **byte-for-byte** against unfixed code, and its companion
`test_primary_and_verified_cleanup_failures_are_retained_separately` still asserts that a
genuine cleanup failure *is* reported — the pair is what keeps the fix from over-correcting
into silence.

## A4.3 — reservation semantics: working-set under offload, not model size

**Decision (creator):** the job is not inadmissible; the model is quantized and generates
media today, so `required_vram_gb` means **peak working-set VRAM under ComfyUI's offload**,
not resident model size. Model size is provenance, recorded separately.

This had to be decided before the numbers could be measured. `jobs/queue.py:112` admits by
comparing free VRAM against `required_vram_gb`; the H3 weights total **28.65 GB against
11.94 GB of VRAM**, so a literal reading makes the job permanently inadmissible — a gate
nothing can ever pass. The provisional 8.0 in `_work/h3_stage/settings.toml` was already
silently assuming the working-set reading.

Why it fits, from `model_catalog.json`: the weights are **already quantized** — NVFP4
diffusion, NVFP4-AWQ text encoder, INT8 video VAE. 28.65 GB *is* the post-quantization
total. The text encoder is the largest single piece at 15.69 GB and runs sequentially
before diffusion, so the peak is the largest stage, not the sum.

**The four numbers need no new instrumentation.** `events/resources.jsonl` already records
whole-device GPU used/total/free, available RAM, free disk per drive and the Windows commit
figures, each tagged with a `phase`. Whole-device is also the *correct* measure, because
admission compares against free VRAM on the device rather than against one process's share.

**Caveat for the measurement run:** the observed sampling interval was **~15 s** (5 samples
over 60 s). That captures a plateau but can miss a transient load spike, and the peak is
the number being measured. The internal throttle floor is 1 s, so tighten the interval for
the run that sets these values, or the reservation will be derived from an average dressed
as a maximum.

## A4.4 — CORRECTION: Phase 1's §4.3 gate passes; ratification is the blocker

Amendment 3 and A3.9 both state that Phase 1 is unaccepted because "§4.3 criterion 5 fails
on 2 of 3 anomaly frames, FR-04 unmet". The first half is true and the conclusion does not
follow; the second half is not the PRD's claim.

- **Criterion 5 does fail on 2 of 3 anomaly frames** — 8.4 s (figure clipped at the right
  frame edge) and 12 s (figure lost in shadow, right-third luminance 25.99 against a 26.1
  baseline). 9 s passes.
- **The gate passes anyway.** §4.3 requires 4 or more of 5 on at least 80% of sampled
  frames with **criterion 3** mandatory — criterion 5 is not mandatory. All 7 samples score
  4 or better and criterion 3 passes on all 7. `spikes/15_phase1_exit/FINDINGS.md:31` reads
  **"Result: PASS."** The verdict also survives the stricter reading in which the four
  pre-anomaly samples scoring criterion 5 not-applicable are excluded rather than counted
  as passes: 3 anomaly frames, all 4 or better, still 100%.
- **What actually blocks it** is the line above the sheet: "pre-scored by the orchestrator,
  **awaiting creator ratification**". The engineering gate is met; the sign-off is not.
- **"FR-04 unmet" is a HANDOFF interpretation, not PRD text.** FR-04 requires supervised
  Blender control, the master render spec, Eevee Next as default, and recording the Blender
  version. It is implemented and has rendered real masters. The label comes from reading
  the Phase 1 *slice* ("builds a believable hallway from a text brief") into FR-04, but
  PRD:616 states **"The Phase 1 Exit clause governs, not the slice description."**
- **`HANDOFF.md` is stale on this point.** It says scenes are built by hardcoded functions;
  `orchestrator/service/script_authoring.py` provides `derive_script_scene_plan`, wired at
  `api/app.py:229` behind a real route at `api/routes.py:202`.

Genuinely open on Phase 1, none of it H3's business: AT-15's budget (the 15 s render took
1047 s against a 900 s default and needed a 0.000249 throughput floor), publication rights
on the generated figure and the overlay font (internal-evaluation only), and the spike's own
recommendation to restage the walk across the lit window — "one parameter change and one
render".

**Consequence for A3.9.** Its claim that exercising `compose_h3_request` "would have
required building the entire Phase 1 render chain first" overstates the obstacle. Rendered
projects already exist on disk (`_work/phase1_exit/exit/` among them), and `settings.toml`
is optional — `platform/settings.py:182` defaults the project root to a relative `projects`
directory and the spike harnesses build `Settings` in code. The service binding can be
closed against an existing project root without a render and without a GPU.

## A4.5 — verification

Full suite **2093 passed, 58 skipped, 0 failed** (2075 baseline + 17 receipt tests + 1
cleanup regression). Ruff **clean**. Evidence ordering for both fixes: the tests were
written first and failed against unfixed code — 8 failures for the receipt, including
`'GenerationRun' object has no attribute 'output_hashes'`, and for the cleanup gate the GPU
run's record reproduced byte-for-byte.

The pytest summary line did not survive the background redirect; the count above is derived
from the progress output (100% reached, 58 skip markers, zero failure or error markers) and
reconciles exactly with the baseline plus the new tests.

# Amendment 5 — the first successful orchestrated H3 run, and the next defect

Recorded 2026-11-13, covering GPU run 2 (`stagebrun2`), the A3.9 closure, and Stage C.

## A5.1 — GPU run 2 SUCCEEDED: three blockers closed at once

The first H3 generation ever completed **through the orchestrator**. Real artifacts,
independently probed with ffprobe: `candidate.mp4` (347,667 B) is 384x288, 24 fps, 192
frames, 8.000 s, H.264 with AAC stereo 32 kHz; `audio.wav` is 2,048,092 B. Elapsed 506.9 s.

- **Listener ownership is VERIFIED.** This was the top open risk, null on every prior run
  because none reached submission:
  `{"pid": 16548, "port": 8298, "ownership": "verified_member_of_owned_tree",
  "action": "POST /prompt"}`. Blocker closed on evidence, not argument.
- **`cleanup_error: null`.** A4.2's fix, confirmed on real hardware. Every prior run
  carried a cleanup failure that had not happened. Corroborated independently: the GPU
  returned to its 945 MiB idle baseline and port 8298 held only draining TIME_WAIT
  sockets, no listener.
- **The schema resolution fix (a379968) is confirmed against the real installed schema.**
  It passed live validation, reached `POST /prompt` and generated.
  `submission_ambiguous: null`.

## A5.2 — the throughput placeholder would have killed this run

A4.3 flagged `throughput_floor_per_second = 0.01` as dangerous. Run 2 proves it. Its
progress trace contains a **176-second gap with no progress update**:

    (81.5, 0.10)  ->  (257.5, 0.12)

`throughput_floor_per_second` arms a runtime stall watchdog that fails the job when no
progress arrives for `1.0 / floor` seconds. At `0.01` that is a **100-second** allowance,
so run 2 would have been killed at roughly 181 s elapsed and reported *"VRAM thrash
suspected"* -- the same misleading failure that ended the Phase 1 render at frame 91.
The correction to 0.0002 (5,000 s allowance) is what let it finish.

This is worth generalising: a watchdog threshold left at a placeholder is not a neutral
default, it is an undiscovered kill switch.

## A5.3 — two corrections to A4.3's own numbers

- **`required_vram_gb` raised 9.8 -> 10.3.** Run 2 measured the attributable peak at
  **10.21 GiB** (11,686.3 MiB whole-device less the 1,227.2 MiB idle baseline). The 9.8
  derived from the ComfyUI run under-estimated by ~0.4 GiB. Corrected in both settings
  files rather than left to surface as an OOM. It remains below the whole-device peak for
  the reason A4.3 gives: admission compares against *free* VRAM.
- **A4.3's sampling-interval caveat was wrong.** It warned that the orchestrator's ~15 s
  interval could miss a transient peak and should be tightened. Run 2 took **88 samples**
  and recorded a *higher* peak (11,686 MiB) than the harness's own 2-second `nvidia-smi`
  poller (11,404 MiB) -- direct NVML reads beat subprocess polling. No tightening is
  needed; the concern was a non-problem.

RAM behaved well: minimum available 6.74 GB against the 2.0 GiB hard-stop floor, versus
the 2.41 GiB that nearly ended the ComfyUI run. Consumption delta 8.97 GB, so
`required_ram_gb = 10.0` is correctly conservative and is left unchanged.

## A5.4 — A3.9 CLOSED

`compose_h3_request` was bound to the **real Phase 1 acceptance project** (copied, so the
evidence was never written to): 19 checks, 0 failures. Both the real MASTER and DEGRADED
receipts bind with digests equal to their immutable records, all five refusals hold
(non-MASTER/DEGRADED kind, stale `expected_version`, nonexistent `shot_version`,
tampered source, missing file), and composing wrote nothing -- 0 path diffs, 0 content
diffs.

Two findings:

- **The hash guard is stronger than A3.9 assumed.** `ProjectStore` re-verifies every
  ShotOutput artifact on *read* (`json_store.py` `_verify_shot_output_artifact`), so a
  tampered source raises `JobError` there and never reaches `compose_h3_request`'s own
  digest comparison. Both layers are real; the store's fires first.
- **A3.9's stated obstacle was false.** It claimed the service binding "would have
  required building the entire Phase 1 render chain first". The chain already existed on
  disk. A4.4 recorded this; the closure confirms it. `stage_b_run.py`'s docstring carried
  the same false claim and has been corrected in place.

## A5.5 — Stage C: the tenth instance of the pattern

Stage C (15 s at 10 fps, 352x288, sourced from the real Phase 1 degraded clip through the
service path) was **refused at 94 s**, before any model load, VRAM peak 1,082 MiB,
`cleanup_error: null`:

    H3SetupError: Installed schema rejects MiniMaxH3Easy.aspect_ratio='352:288'

**This is the schema validation working as designed** -- an early, precise refusal naming
the rejected value instead of a failure after a 28 GB load. The defect it caught:

```python
aspect_ratio = "4:3" if request.width * 3 == request.height * 4 else f"{request.width}:{request.height}"
```

The node publishes a **fixed** option list -- `1:1, 2:3, 3:2, 3:4, 4:3, 9:16, 16:9, 21:9`
-- so the fallback branch emitted a raw `WIDTH:HEIGHT` string that can never appear in it.
Not even a square frame survived: 512x512 produced `"512:512"`, not `"1:1"`. Only
exactly-4:3 input ever worked, which is why the 384x288 baseline passed and the branch sat
undetected. **Fully specified, unable to succeed, exposed only when something finally
drove it** -- the same shape this repository has now produced ten times.

**Fix (creator-directed): select the nearest offered value.** Distance is measured on the
log of the ratio so a given proportional error counts the same either side of square;
comparing raw ratios could hand a 9:16 frame `16:9`. This lands on the answer the PRD
already specifies -- CIF 352x288 is 11:9 in storage but must *display* 4:3, which is how
the production clips are encoded (SAR 12:11, DAR 4:3). Storage dimensions were never the
picture's shape.

Verified across 8 shapes (384x288, 352x288, 512x512, 1920x1088, 1088x1920, 640x480,
320x240, 1920x816): all map to an offered value, 0 mismatches, both orientations correct.
Evidence ordering: 3 new tests failed pre-fix; `test_the_baseline_aspect_ratio_is_unchanged`
**passed** pre-fix, which is what shows the set is not vacuous.

**Accepted design limit.** `_ASPECT_RATIOS` is a constant in `core.py` mirroring the
installed node, because `build_api_graph` is pure and has no live schema. It can drift.
The creator accepted this on 2026-11-13 ("just select the nearest offered value") rather
than threading the offered set through the builder's signature. The mitigation is real and
already demonstrated: `_validate_live_schema` checks the emitted value against the live
schema, which is exactly how this surfaced. The constant is a convenience; the validation
is the guarantee.

## A5.6 — what is still unanswered

**The integrated-anomaly experiment itself has not run.** Stage C got 94 s in and never
reached generation, so nothing is yet known about whether H3 improves 4.3 criterion 5. The
reservations for a 150-frame run are also still unconfirmed by measurement; A5.3's figures
are from a 192-frame run at 384x288.

Unchanged and still the creator's: **D1-D4** in `H3_PROMOTION_CONTRACT.md`, and whether to
ratify the Phase 1 scoresheet or restage first.
