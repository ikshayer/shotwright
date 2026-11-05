# Contract — adapter startup allowances with a positive throughput floor

This contract covers only the adapter side of the startup-aware watchdog change. The
queue interface and decisions in `orchestrator/jobs/WATCHDOG_CONTRACT.md` are binding.
The public adapter signatures remain unchanged. Implementation and tests have separate
authors; the test author works from this contract in an isolated snapshot where adapter
implementation changes are physically absent. The reviewer is read-only and reports
findings without fixes.

## Scope and allowance values

- `BlenderAdapter.render(request, job, *, queue, cancel=None)` must accept a configured
  positive `throughput_floor_per_second`. Remove its zero-only refusal and pass a
  per-run startup allowance to `queue.run`. Use **74.0 seconds**, from PRD §8.4's
  measured ~74 s one-time cold Blender shader/kernel cache cost in PRD §8.4.
  Keep the existing render deadline, cancellation,
  supervision, progress reporting, token fencing, artifact retention and lease behavior.
- `ComfyAdapter.generate(request, job, *, queue, cancel=None)` must accept a configured
  positive floor. Remove its zero-only refusal and pass `COMFYUI_STARTUP_ALLOWANCE_SECONDS` as the per-run
  startup allowance, from PRD §8.4 and spike 03's measured clean ComfyUI boot with the
  adapter's enforced disabled custom-node launch. The 150.5 s ambient-node boot is not
  applicable because that node set is disabled. Keep the whole-generate hard deadline,
  cancellation/interrupt/delete behavior, callback fencing, child ownership, cleanup,
  artifact retention and lease behavior.
- These are watchdog startup allowances, not hard deadlines, throughput targets, or
  resource reservations. Do not change launch arguments, fake progress, alter settings,
  touch queue/jobs, or change either public method signature. Do not add another numeric
  default. Queue `run` owns validation and startup-timeout classification.

## Required behavior

1. Each adapter passes the measured allowance to `queue.run` on every execution, including
   when the configured throughput floor is positive. A zero floor retains its existing
   meaning. Request/queue mismatch checks still happen before writes or queue execution.
2. A worker that stays silent for less than its startup allowance and then reports its
   first progress update is not classified as a throughput stall. The adapter must not
   synthesize progress to mark startup complete.
3. After the first positive progress sample, a truly stalled worker remains detectable:
   both silence lasting the queue's `1 / throughput_floor_per_second` interval and a
   subsequent positive sample whose measured rate is below the floor must terminate via
   the existing watchdog path and surface correlated terminal `BUDGET_EXCEEDED` with the
   existing operator remediation hint. Failed termination confirmation must retain the
   lease and surface the existing internal stop failure.
4. Existing hard deadlines still win during startup. Cancellation remains distinct from
   deadline/stall failure. Cleanup must quiesce the adapter attempt and prevent late
   writes/submissions before lease release. No lease/token/child ownership behavior is
   weakened.
5. Tests exercise adapter-to-queue allowance forwarding without GPU, Blender, ComfyUI or
   network, and retain the established adapter supervision regression suites.

## Required negative cases

- Positive floors are accepted for both adapters, and the queue receives exactly the
  measured adapter allowance above; neither adapter silently omits the configured floor
  or silently drops a real watchdog error.
- Slow startup strictly before allowance followed by progress completes without a false
  stall; no first progress by allowance still fails through the queue watchdog.
- A real post-startup stall is still caught after the grace interval, for both no-progress
  silence and below-floor later progress, and carries the normal remediation hint and job
  correlation.
- A hard adapter/queue deadline during startup remains a deadline failure; user cancel
  remains cancellation. Cleanup, termination confirmation, lease retention/release and
  callback fencing behavior remain intact.
- Floor zero remains compatible and does not gain an adapter-imposed startup timeout.
- Existing runner/request/queue mismatch and occupied-run failures still reject before
  adapter output writes, and public signatures do not change.
