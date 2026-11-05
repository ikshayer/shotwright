# Contract — GPU lease and job queue

This file is the agreement between the implementation and its tests. They are written
by **different agents, in parallel, from this document** — not from each other. Where
they disagree, this file decides, and the disagreement is reported rather than patched.

Everything here is derived from measurement (`spikes/03_flux_fit/FINDINGS.md`,
`spikes/05_voice/FINDINGS.md`) or from the PRD (FR-12, FR-26, FR-27, §8.1, §8.3).

## Why the previous attempt was rejected

Its tests passed while production could not work: the VRAM probe had no total-memory
accessor so free VRAM computed as zero, the respawn callbacks defaulted to no-ops, and
the persisted PID was the orchestrator's rather than the worker's. Fakes were tested;
the system was not. The rules below exist to make those failures impossible to repeat.

## Injection seams (protocols)

Every seam is a `typing.Protocol`. Production implementations live in this package and
**must be exercised by at least one test each**, against a stub of the *underlying
library*, never against a reimplementation of our own logic.

```python
class NvmlProbe(Protocol):
    def device_memory(self) -> MemoryReading: ...   # used_mb, total_mb, free_mb
    def process_usage(self) -> list[ProcessUsage]: ...  # pid, name, used_mb

class WorkerControl(Protocol):
    def unload(self) -> None: ...        # ask the worker to free (ComfyUI POST /free)
    def terminate(self) -> bool: ...     # kill it; True only if it is confirmed gone
    def respawn(self) -> int: ...        # start a replacement, return its real PID
    def pid(self) -> int | None: ...     # the WORKER's pid, never the orchestrator's

class HostResources(Protocol):
    def free_ram_gb(self) -> float: ...
    def free_disk_gb(self, path: Path) -> float: ...

class Quarantine(Protocol):
    def quarantine(self, paths: Sequence[Path]) -> Path: ...  # move, never delete
```

`MemoryReading` carries `used_mb`, `total_mb` and `free_mb`. A probe that cannot supply
`total_mb` is a defect: free VRAM must never be computed as zero because an accessor is
missing.

## `GpuLease`

```python
GpuLease(
    root: ProjectRoot, settings: Settings, *,
    nvml: NvmlProbe, worker: WorkerControl,
    pid_alive: Callable[[int], bool] = ...,
    clock: Callable[[], float] = time.monotonic,
    ttl_seconds: float = 30.0,
)
```

**Exclusivity.** At most one holder. `acquire(timeout_seconds)` blocks until free or
raises `JobError(WORKER_UNREACHABLE)` on timeout. The lease is persisted under the
project root (through `ProjectRoot`, never a raw path) with the **worker's** PID from
`WorkerControl.pid()`, a heartbeat and a TTL.

**Reclaiming a stale lease** requires BOTH an expired heartbeat AND `pid_alive(pid)`
returning False. An old heartbeat alone must never reclaim. `pid_alive` must treat an
unknown-PID OS error as "not alive" rather than propagating — on Windows this surfaces
as `OSError` with `winerror 87`.

**Reclaim is atomic.** Two contenders racing must not both win, and the loser must not
delete the winner's freshly written lease. Compare-and-delete on the exact record read.

**Release is a measured postcondition.** `release(floor_mb, timeout_seconds)`:
1. `worker.unload()`
2. poll `nvml.device_memory().used_mb` until `<= floor_mb`, or timeout
3. on success, drop the lease
4. **on timeout**: `worker.terminate()`. If terminate returns False, the worker is NOT
   confirmed gone — raise `JobError(WORKER_UNREACHABLE)` and **keep the lease held**, so
   nothing can start alongside a surviving worker. Only on confirmed termination may it
   `respawn()` and release.

**A CUDA error forces replacement regardless of memory.** `release(after_cuda_error=True)`
must terminate and respawn even if the floor is met. Spike 03 measured the context
staying poisoned: the following job died in 1.0 s.

**The floor is relative, not absolute.** It is `baseline_mb + settings` allowance, where
`baseline_mb` is re-measured at acquire time. The idle baseline drifted 1.3 → 2.7 GB in
one evening; a fixed absolute floor makes healthy unloading time out.

An exception anywhere inside a held lease must still release it.

## `JobQueue`

```python
JobQueue(
    root: ProjectRoot, settings: Settings, lease: GpuLease, store: ProjectStore, *,
    nvml: NvmlProbe, host: HostResources, quarantine: Quarantine,
    clock: Callable[[], float] = time.monotonic,
)
```

**`preflight(job) -> None`** raises with a message naming the failing precondition:
- free VRAM (`nvml`, accounting for the measured foreign baseline) below the job's
  declared requirement → `OUT_OF_MEMORY`;
- free host RAM below `job.required_ram_gb` → `OUT_OF_MEMORY`. **Checked independently
  of VRAM**: a 3.3 GB checkpoint failed twice at 6.0 GB free host RAM while the GPU was
  idle, once as a misleading 2 MB allocation error and once as a segfault;
- free disk below the FR-26 threshold → `DISK_EXHAUSTED`;
- a foreign process holding more than the configured allowance → refuse and **name the
  process** from `nvml.process_usage()`.

**`run(job, worker_fn, *, partial_artifacts=()) -> object`**
- Preconditions are re-checked **immediately after acquiring the lease**, not only
  before waiting for it: headroom can change while queued.
- Heavy GPU jobs never overlap — serialised through the lease.
- Retries follow `platform/errors.py` only. No second retry policy.
- **Job state is persisted** on every transition (queued → running → completed / failed /
  cancelled, plus `attempts` and `lease_holder_pid`) so a restart does not lose the
  queue. A release failure must not leave a job recorded `completed`.
- **FR-26 ceilings are enforced**: attempts per shot, GPU-minutes per shot and per
  episode, per-shot wall clock. Exceeding one is `BUDGET_EXCEEDED` — terminal, escalated,
  not retried.
- **Throughput watchdog.** Windows WDDM sysmem fallback turns oversubscription into a
  10–100× silent stall rather than an error. A job whose progress rate falls below the
  configured floor fails with a message containing `VRAM thrash suspected`. The watchdog
  must confirm the worker actually stopped before the attempt is retired; it may not
  proceed while the original attempt can still write artifacts.
- **Cancellation** → terminal `cancelled`, lease released, `partial_artifacts` passed to
  `Quarantine.quarantine` (moved, never deleted).
- Cleanup releases only a lease **this invocation acquired**. A concurrent waiter timing
  out must never release the running job's lease.

## Test rules

- No test may require a GPU, a real subprocess, or a sleep longer than ~10 ms.
- Stub the seams, but **cover each production seam implementation at least once**
  against a stub of the underlying library (e.g. a fake `pynvml` module), so a missing
  accessor or a no-op default is caught.
- Required negative cases: terminate-returns-False keeps the lease; stale heartbeat with
  a live PID does NOT reclaim; two racing reclaimers; CUDA-error release replaces the
  worker even when memory is already below floor; each precondition in isolation;
  budget ceiling reached; watchdog on a stalled job; cancellation quarantining; a
  release failure not recording the job completed.

## Amendment 1 — gaps the independent tests exposed

Writing the tests from this contract, blind to the implementation, surfaced questions it
did not answer. These are now binding.

- **Episode ceiling.** `Job` gained `episode_id`. The per-episode GPU-minute ceiling of
  FR-26 cannot be enforced without it, and the tests could not reach that path.
- **Watchdog floor is configurable.** Add `resources.throughput_floor_per_second` to
  `Settings` with an UNMEASURED PLACEHOLDER marker. The watchdog must read it rather
  than hard-coding a rate, so the stall tests can set it rather than relying on a huge
  clock step.
- **Cancellation trigger.** Cancellation is signalled by `worker_fn` raising
  `JobError(JobErrorKind.CANCELLED)`. The queue catches it, quarantines
  `partial_artifacts`, releases the lease and records the job `cancelled`.
- **CUDA errors at queue level.** The queue treats a `JobError(OUT_OF_MEMORY)` or any
  error whose message contains `CUDA error` as context-poisoning, and must call
  `lease.release(after_cuda_error=True)` so the worker is replaced. Spike 03 measured
  the next job dying in 1.0 s otherwise.
- **Held-lease safety.** `GpuLease` must support use as a context manager, so "an
  exception inside a held lease still releases it" is testable directly rather than only
  through the queue.
- **Job persistence location.** Job state is NOT a versioned store record. §10.0 makes
  SQLite "a rebuildable index and job queue", so job state lives outside the
  authoritative JSON. Persist it through `ProjectRoot` under `state/jobs/`, and say so.
- **A failed release leaves the job `failed`**, never `queued` and never `completed`.
- **`PynvmlProbe.process_usage` must report the real process name** via
  `nvmlSystemGetProcessName`. A placeholder such as `"PID 321"` defeats the point, which
  is telling the operator which application to close.
