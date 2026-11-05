# Contract — startup-aware throughput watchdog

This contract defines the FR-12 watchdog change in `orchestrator/jobs/`. Production
implementation and tests must have different authors and be derived from this file.
Where a report disagrees with this contract, report the disagreement; do not silently
change the contract after implementation.

## Scope and measured default

The existing queue checks for stalled progress after one second, which misclassifies
worker startup. Add a startup phase that is exempt from throughput checks until the
worker first reports progress, with a maximum startup allowance. After startup, enforce
the configured throughput floor. Startup grace does not extend the job's hard wall-clock
deadline and does not imply worker success.

Add `ResourceSettings.startup_allowance_seconds` at
`orchestrator/platform/settings.py`, exposed in configuration as
`resources.startup_allowance_seconds`. Its default is **74.0 seconds**, grounded in
PRD §8.4's measured ~74 s one-time cold Blender shader/kernel cache cost. It also covers
the §8.4 measured 33.5 s ComfyUI clean boot. The 150.5 s ambient ComfyUI boot is not a
default basis: the enforced ComfyUI launch disables the ambient node set. These figures
are startup costs, not a claim that all worker startup takes exactly that long. Any
unmeasured worker requiring longer startup must explicitly configure a larger allowance;
no other numeric default is introduced.

## Exact interfaces

In `orchestrator/platform/settings.py`, extend `ResourceSettings` with:

```python
startup_allowance_seconds: float = Field(
    74.0, ge=0,
    description="Maximum startup time before the first progress update (PRD §8.4).",
)
```

In `orchestrator/jobs/queue.py`, preserve the existing public call shape and add a keyword:

```python
JobQueue.run(
    self,
    job: Job,
    worker_fn: Callable[[Job], T],
    *,
    partial_artifacts: Sequence[Path] = (),
    startup_allowance_seconds: float | None = None,
) -> T
```

`None` selects `settings.resources.startup_allowance_seconds`; a finite nonnegative
number declares a per-run override for that worker invocation. Validate an explicit
override before the first job persistence, preflight side effect, lease acquisition, or
worker call. Invalid override raises `ValueError` as request/configuration validation,
not `JobError`, and does not mutate job state or acquire a lease.
The settings model also rejects non-finite startup allowances and throughput floors as
`SettingsError`; finite zero remains the documented floor-off value.

Preserve the private watcher interface's existing three-argument shape:

```python
JobQueue._watch(
    self,
    worker_fn: Callable[[Job], T],
    job: Job,
    deadline: float,
) -> T
```

No `Job` entity field or service/API interface is added in this pass. The queue caller
declares the allowance through `run`; settings provide the default. Since adapter
supervision wrappers depend on `_watch` retaining its three-positional-argument shape,
`run` carries its selected allowance to `_watch` through per-invocation thread-local
context, scoped and cleared around each watcher call. Concurrent `run` calls on the same
queue must not see one another's allowance. The worker reports startup completion
implicitly with its first strictly positive change in `job.progress`.

## Watchdog decision rules

- `resources.throughput_floor_per_second == 0` disables throughput-stall termination
  exactly as before, regardless of startup allowance. Deadline handling, worker exception
  propagation, heartbeat, termination fencing, lease behavior and result handling remain
  unchanged.
- With a positive floor, capture the initial `job.progress` and startup start time
  before starting the worker thread. The progress field has no update timestamp, so the
  watchdog can only decide from the value observed at each poll.
- Before the first strictly positive progress delta, do not enforce the throughput floor.
  At each poll, observe a positive progress delta before checking startup expiry; if the
  progress field has advanced by the poll where the allowance is reached, accept that
  sample as startup completion because its actual update time is unavailable. If elapsed
  startup time reaches the allowance and no progress delta is present, terminate the
  worker and raise `JobError(JobErrorKind.BUDGET_EXCEEDED, ...)` only after termination
  is confirmed. If termination cannot be confirmed, raise the existing `INTERNAL`
  watchdog stop-confirmation failure and keep the lease held under existing queue behavior.
- A first positive progress delta ends startup and establishes a fresh throughput sample
  at that time. Startup time is not included in that sample's rate.
- After startup, each positive progress delta is compared to the configured floor using
  the elapsed time since the preceding progress sample. A rate below the floor is a
  throughput stall and follows the existing terminate-confirm-or-fail path.
- With no new progress, fail once elapsed time since the last positive sample reaches
  `1 / throughput_floor_per_second`. This removes the fixed one-second stall assumption
  while ensuring silence cannot pass indefinitely under a positive floor.
- Time decisions use the injected `JobQueue.clock`. Do not require a fixed count/order of
  clock calls in tests. Preserve the existing real monotonic clock for lease heartbeats.
- A `BUDGET_EXCEEDED` error is terminal per the existing closed FR-27 taxonomy and carries
  `JobError.job_id`, `JobError.shot_id`, and `JobError.run_id` copied from the active
  `Job`. Do not add an enum member or use startup or validation exceptions as job failures.

## Required negative cases

1. With floor zero, no startup timeout or throughput failure occurs, including when the
   worker reports no progress; the ordinary hard deadline still applies.
2. With positive floor, a worker may remain silent strictly before the allowance, then
   report progress and complete without a false stall.
3. No first progress observed by the allowance fails, including allowance zero;
   termination must be confirmed before the attempt is retired. A progress value already
   advanced when the expiry poll runs wins that poll because `Job.progress` carries no
   event timestamp.
4. The first progress sample after a long startup is not divided by startup elapsed time.
   A genuinely below-floor subsequent rate fails.
5. A worker that reports no progress after startup fails at its floor-derived timeout,
   rather than the former fixed one-second interval.
6. A worker that finishes or raises before startup expires is handled normally; startup
   grace does not mask worker exceptions or extend the hard deadline.
7. A slow injected clock and event-driven progress updates produce deterministic results;
   do not assume a specific count or order of monotonic calls.
8. A false termination confirmation raises the existing internal stop failure, does not
   start a retry beside the live worker, and retains the lease.
9. Negative, NaN and infinite per-run allowances fail validation before persistence,
   preflight, lease acquisition or worker invocation. The settings model rejects invalid
   startup defaults or throughput floors through
   `orchestrator.platform.settings.SettingsError`.
10. Existing floor-zero Blender/ComfyUI adapter behavior remains compatible without
    changing their files or tests in this package pass.
11. After the first progress sample, both a subsequent below-floor positive progress
    rate and silence lasting `1 / floor` terminate the worker and produce a correlated
    terminal `BUDGET_EXCEEDED` error.
12. A worker exception before startup expiry propagates through the ordinary job error
    mapping and retains job/shot/run identifiers; a hard wall-clock deadline that occurs
    during startup remains `BUDGET_EXCEEDED` and takes precedence over startup allowance.
    Preserve the existing deadline path's termination behavior; this change does not claim
    that deadline expiry confirms the worker stopped.

## Explicit limits

This is the queue-layer watchdog only. It does not wire a per-run allowance through a
service, adapter or API, nor does it complete AT-13. Adapter callers must pass
`startup_allowance_seconds` when their startup phase needs an allowance different from
the settings default. Verification must state whether the existing adapters themselves
accept a positive floor; that depends on their separately owned integration.
