# Cut assembly contract, part B of the finishing steps (2026-11-21)

Step 2b of the fix order in `GAP_REVIEW.md`. Part A
(`orchestrator/api/FINISHING_STEPS_CONTRACT.md`) exposed reviews, approvals, recovery
and output read-back. This exposes **cut assembly**: turning an episode's degraded shots
into an `EpisodeOutput` cut, as a queued CPU job. Until now
`service/timeline.py` `compose_timeline` / `assemble_episode` and
`adapters/timeline/runner.py` `TimelineAssembler` have run only against fake encoders
in tests; nothing in production constructs a `TimelineAssembler`.

## Creator decisions

- **D5 (2026-11-21).** An agent may assemble a cut; assembly is not an approval. A cut
  may contain shots that are not yet approved. The creator's cut approval (gate C)
  still applies afterwards.

## R1 — Which shots a cut may contain (D5)

`compose_timeline` and `assemble_episode` (its `_outputs_for_timeline` recheck) accept
a Shot whose current version is in status `review` **or** `approved`, and which has a
DEGRADED output for that current version (the latest one, as today). A Shot in any
other status (`draft`, `planned`, `blocking`, `building`, `stale`, `rejected`,
`failed`) refuses assembly with `JobError(INVALID_MANIFEST)` naming the shot, its
status and the step that would make it eligible. Everything else about selection
(order from the Script's `beat_to_shot`, then `Episode.shot_ids`; durations from
`frame_count / output_fps`) is unchanged. Existing tests that asserted "every shot must
be approved" are updated to this rule; that is a contract change, not a weakened test.

## R2 — A CPU process supervisor

Add a lease-free supervisor for CPU FFmpeg jobs, satisfying
`adapters/timeline/runner.py` `ProcessSupervisor`: `launch`, `clear_process`,
`stopped`, `stop_for(reason) -> bool`. Put it in a new module
`orchestrator/jobs/process.py` as `CpuProcessSupervisor`.

- `launch` refuses once stopped (raise the stop reason as a `JobError`), records the
  process, and starts it with the arguments given.
- `stop_for(reason)` records the reason, terminates the recorded process (kill if it
  has not exited within 5 s), and returns True once it has exited. It is idempotent.
- Expose the reason as a public `stop_reason` attribute. Change `TimelineAssembler` to
  read `stop_reason` (falling back to the old private `_stop_reason` only if the
  Blender supervisor still needs it) instead of `getattr(control, "_stop_reason", ...)`.
- Cancellation: the adapter that owns the supervisor polls the job's cancel check
  (`_RuntimeAdapter.set_cancel_check`) at least every 250 ms on a watcher thread and
  calls `stop_for(JobErrorKind.CANCELLED)` when it fires. The watcher exits when the
  job ends.

## R3 — The operation

| Operation | Method and path | Body | Returns |
|---|---|---|---|
| `assemble_cut` | `POST /projects/{project_id}/episodes/{episode_id}/cut/assemble` | `CutAssemblyRequest` | 202 `{"job_id", "state"}` |

`CutAssemblyRequest` (extra forbidden): `credit_card_duration_sec: float = 5.0` (finite,
> 0), `required_disk_gb: float = 0.0` (finite, >= 0, the same admission field other
jobs take).

- Not creator-only. Listed in `ASYNC_ADAPTER_OPERATIONS`.
- **Fail fast.** Before queueing, the operation loads the Episode (404 if missing) and
  runs `compose_timeline` once. If R1 or anything else refuses, the request answers 422
  with that message and no job is created.
- The job's record carries `episode_id`, `shot_id = None`, `type = "cut_assembly"` (or
  the name the job journal uses for kinds), and the run id.
- When the job completes, `report_jobs` shows it `completed`. The cut is the newest
  entry of `list_cuts`; also record its `output_id` on the job record if the Job entity
  has a suitable field, otherwise leave the job record unchanged and document where to
  find the cut in the operation's summary.
- The summary follows the description standard: what it does, what must happen first
  (shots in review or approved with degraded outputs), what it returns, how to see the
  result (`list_cuts`, `inspect_output`), and that approving the cut is the creator's
  `approve_cut`.

## R4 — Job plumbing

Add `AdapterKind.ASSEMBLE` to `service/runner.py`, in `_CPU_KINDS`. It is
**episode-scoped**: everywhere `JobRunner` assumes a Shot (submission's Shot load and
version checks, `_begin_shot_work`, `_record_result`'s ShotOutput append and Shot
transition, `_fail_shot_after_job_error`), ASSEMBLE takes its own path:

- Submission validates the Episode exists and builds the Job with `shot_id=None` and
  `episode_id` set. It must not load any Shot by `record_id`. Use whatever
  `AdapterRequest` fields fit (e.g. `record_id=episode_id`, and the Episode's mutation
  token as `record_version`); document the choice.
- Admission uses the existing `preflight_cpu` (free RAM, free disk plus
  `min_free_disk_gb`, project disk ceiling) with the request's `required_disk_gb`.
- The adapter calls `assemble_episode(store, encoder, episode_id=..., run_id=job.run_id,
  budget_seconds=..., credit_card_duration_sec=...)` with a real `TimelineAssembler`
  built from the project's `ProjectRoot`, `Settings` and a fresh `CpuProcessSupervisor`.
  `assemble_episode` persists the Timeline and the `EpisodeOutput` itself; the runner
  records no ShotOutput and transitions no Shot.
- A failure is journaled on the job like any CPU job's failure, with no Shot touched.
  A cancelled job stops FFmpeg (R2) and is journaled `cancelled`.
- ffprobe: resolve it as `adapters/export/package_media.py` does (the sibling of an
  absolute `settings.binaries.ffmpeg_exe`, else `ffprobe` on PATH) and pass it to
  `TimelineAssembler(ffprobe_exe=...)`.
- Budget: add `BudgetSettings.cut_assembly_wall_clock_seconds: float = 900.0`, with a
  comment that it is an **unmeasured placeholder** (no cut encode has been timed on this
  machine). Pass it as `budget_seconds`.
- `api/app.py`: a dedicated submission branch for `assemble_cut` (the generic async
  branch requires a `shot_id`), and an `adapter_for` case building the assembly adapter.
- `api/cli.py`: `assemble_cut` waits by default like other async operations. Add it to
  the locally owned jobs set so `--no-wait` is refused and Ctrl+C cancels and waits.
  When the job completes, the CLI result includes the new cut's `output_id`.

## Amendments after review (2026-11-21)

The Codex review reproduced two HIGH and three MEDIUM defects; the parent fixed them:

- **A1 — No FFmpeg outlives a failed job.** When assembly ends without a receipt (an
  exception, a budget stop, a cancellation, a worker exit), the adapter stops the
  supervisor and waits until its child has exited, before re-raising.
- **A2 — Partial outputs are quarantined (FR-27).** The job's cut is named after its
  run id (`assemble_episode(output_id=job.run_id)`). After a failed or cancelled
  assembly, every file in `episodes/{ep}/outputs/cuts/` named `{run_id}.*` or
  `{run_id}-*` is moved to the project quarantine. A quarantine failure is reported
  without hiding the assembly's own error.
- **A3 — Superseded by `orchestrator/api/CUT_APPROVAL_BINDING_CONTRACT.md` (2026-11-21).**
  Appending a new cut receipt no longer changes approval state. Gate C binds to one exact
  `approved_cut_output_id`; export accepts that receipt or a byte-identical re-assembly.
  A changed or stale Timeline still revokes the approval before encoding.
- **A4 — The CLI reports this job's cut.** It returns the cut whose `output_id` equals
  the job's run id, never simply the newest.
- **A5 — Fail fast on rights.** Submission also runs the rights collection assembly
  runs (each shot's built scene, licences, publication blockers), so those failures
  answer 422 before a job exists.

## Non-goals

Audio tracks in cuts (still refused, Phase 2). Supervising the PostProfile (degrade)
FFmpeg, which today cannot be stopped by cancellation; that is recorded as a gap, not
fixed here. Shipping a font.

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max) may edit: `service/timeline.py` (R1
only), `adapters/timeline/runner.py` (R2's `stop_reason` read only),
`orchestrator/jobs/process.py` (new), `service/runner.py`, `platform/settings.py`
(R4 budget field only), `api/routes.py`, `api/app.py`, `api/cli.py`, and a new
`orchestrator/adapters/timeline/job.py` for the queued adapter if that is the cleanest
place. Existing tests may be updated only for R1's changed eligibility rule and for
operation lists (`tests/api/test_parity.py`, `tests/api/test_entry_path.py`,
`tests/api/test_adapters.py`). List every test file touched.

**Test author** (separate agent, from this contract only) writes only
`tests/api/test_cut_assembly_contract.py`. It must include at least one test that runs
**real FFmpeg and ffprobe** through the real `TimelineAssembler` and the real job path
(HTTP `assemble_cut`, then `report_jobs` until completed, then `list_cuts` and
`inspect_output` on the cut). It builds its own tiny degraded clips (e.g. FFmpeg
`testsrc`) with correct receipts, so no Blender, GPU or font is needed. It also covers
cancellation stopping a real FFmpeg (a deliberately long encode), the fail-fast 422s,
R1's eligibility statuses, and the CLI result.

**Do not touch:** `spikes/**`, `service/approval.py`, `service/dependencies.py`,
`store/json_store.py`, and any file not listed above.

## Done when

Every requirement has a test; the full suite passes with `--basetemp` on D:; ruff
reports nothing beyond `main`'s; the implementer reports files changed, tests touched,
the `AdapterRequest` field choice, and anything this contract left ambiguous.
