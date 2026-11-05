# Contract — CPU and GPU job admission

This contract defines resource admission for `JobQueue`. Implementation and tests are
owned separately and are derived from this document.

## Public interface and classification

`JobQueue.preflight(self, job: Job) -> None` remains the GPU admission method. It keeps
its existing behavior: inspect NVML foreign-process usage and free VRAM, then check host
RAM and project-volume disk budgets. This is the backwards-compatible default.

Add `JobQueue.preflight_cpu(self, job: Job) -> None`. Calling this method explicitly is
the classification decision: the caller has established that this job does not use a
GPU. It checks host RAM and the same disk budgets as `preflight`, but must not call
`nvml.device_memory()`, `nvml.process_usage()`, or inspect lease baseline/worker state.
It neither acquires/releases a GPU lease nor changes `run()`.

Classification must not inspect `Job.required_vram_gb` or `Job.type`. The former is an
admission sizing field, may legitimately be zero for GPU work, and is not a reliable
routing signal; the latter is an unrestricted string and the integration contract does
not promise its values. In the integration layer, `AdapterKind.degrade` must select
`preflight_cpu`; `scene`, `render`, and `generate` continue to select `preflight`.
This explicit method choice requires no new settings key or domain field.

## Shared RAM and disk checks

Both methods check `host.free_ram_gb()` against `job.required_ram_gb`, then
`host.free_disk_gb(root.path)` against `job.required_disk_gb +
settings.budget.min_free_disk_gb`, and retain the existing project-size ceiling check
against `settings.budget.disk_ceiling_gb`. A refusal remains a correlated `JobError`
with `job_id`, `shot_id`, and `run_id` copied from the job.

- Insufficient host RAM uses `JobErrorKind.OUT_OF_MEMORY`, retryability from the closed
  taxonomy, and a CPU-specific remediation: free host RAM / close host-memory consumers,
  then retry or reduce the job's host-memory requirement. It must not use a GPU-shaped
  remediation hint.
- Insufficient available disk uses `JobErrorKind.DISK_EXHAUSTED` and its existing
  taxonomy remediation.
- Exceeding the project disk ceiling uses `JobErrorKind.BUDGET_EXCEEDED` and its
  existing taxonomy remediation.

Project size remains measured by summing file sizes under `root.path`, exactly as the
existing GPU preflight does. No project-size probe seam or synthetic size setting is
added; tests use a real temporary project root and its actual file contents.

To support a resource-specific hint without altering existing GPU errors, `JobError`
may carry an optional remediation override. When absent, its existing
`kind.remediation` behavior is unchanged. The override is used only for CPU RAM
admission and must remain present in `to_log_record()` and API serialization through
the existing `error.remediation` property.

## Settings

No setting is added. Existing keys used here are `budget.min_free_disk_gb` and
`budget.disk_ceiling_gb`; these are existing UNMEASURED PLACEHOLDER defaults in
`orchestrator/platform/settings.py`, not measurements introduced by this work. The
measured §8.4 storage facts (63 GB free on the measured project volume; measured
per-project output rates) justify treating disk as a constrained resource but do not
justify a new default threshold.

## Required negative and positive cases

1. With NVML configured to fail if touched, `preflight_cpu` passes when RAM and disk fit,
   including when `required_vram_gb` is zero.
2. With GPU memory exhausted and/or a foreign GPU process above allowance,
   `preflight_cpu` still passes when RAM and disk fit; it does not probe GPU or lease
   state.
3. A CPU RAM refusal carries `OUT_OF_MEMORY`, correct job correlation, retry policy
   from the taxonomy, and a CPU/host-RAM remediation without GPU wording.
4. CPU disk-availability and project-ceiling refusals remain enforced and carry their
   corresponding closed taxonomy kind, retryability, and remediation.
5. The existing GPU `preflight` continues to refuse insufficient VRAM when a positive
   amount is declared, and foreign GPU use even when `required_vram_gb == 0`; it also
   continues to enforce RAM/disk checks.
6. Neither admission method silently admits when its required host or disk measurement
   raises or is unavailable. Measurement exceptions propagate; they are not converted
   into successful admission.
7. `preflight_cpu` is not inferred from zero VRAM or a guessed `Job.type`. No new
   settings default, resource estimate, lease behavior, cancellation behavior, deadline,
   token fence, or watchdog behavior is introduced.
