# Contract — wiring the adapters into the service and API (FR-04, FR-06, FR-21, FR-14)

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree,
this file decides and the disagreement is reported, never patched away.

Three adapters now exist, are tested, and are reachable by nothing. `build_scene`,
`render_preview` and `generate_media` all answer `501`, and both adapter reviews say the
same thing: *"Service mutation/approval integration must be designed separately; this
adapter never revises/approves domain records or bypasses their mutation tokens."* This
file is that design. Phase 1 cannot exit until it exists, because the finished-shot
acceptance run is precisely an end-to-end pass through it.

## The fact that shapes everything here

**There is no asynchronous job execution in this system yet.** `JobQueue.run(job,
worker_fn)` is synchronous: it performs admission control, takes the GPU lease, supervises
deadlines and cancellation, and calls `worker_fn` inline. There is no enqueue and no
dequeue. Meanwhile `Job.state` defaults to `"queued"`, `Job.progress` exists, and the API
already exposes `report_jobs` reading persisted job records — so the *intent* is
asynchronous and only the intent is present.

This matters because the measured costs are not request-shaped. A ComfyUI still took
**189 s** end to end; an Eevee master took **43 s**; ComfyUI clean boot alone is **33.5 s**
and a cold Blender shader cache is **~74 s**. An HTTP handler must not block for that, and
the CLI must not either.

So the integration layer owns the missing piece: a **single in-process job runner**. Not a
second process — the service layer's concurrency contract is explicitly a
single-orchestrator-process contract, and `GpuLease` is in-process. Not a thread pool —
§8.3 serializes heavyweight work and the machine has one GPU with 1.3–2.7 GB of foreign
baseline drift.

## What this layer is

Transport already owns validation, auth and error mapping. The service layer already owns
anything spanning store and domain. This layer owns exactly one thing: **turning a
request for long-running work into a persisted job, running it under the existing queue,
and recording its result through the service boundary.**

It owns no business rules. Impact, revision shape, Review binding, approval freshness and
the post-only/rebuild-triggering distinction are already pure domain rules — call them.
A rule appearing here is a defect, and that exact drift was an earlier pass's finding.

## The interface, pinned

Named explicitly, because two earlier passes lost time to an implementer and a test author
assuming different names.

```python
orchestrator/service/runner.py

class AdapterKind(StrEnum):           # "scene" | "render" | "generate" | "degrade"

class JobRunner:
    def __init__(self, store_for: Callable[[str], ProjectStore], settings: Settings,
                 *, queue_for: Callable[[str], JobQueue],
                 adapters: AdapterRegistry, clock: Callable[[], float] = time.monotonic) -> None: ...
    def submit(self, project_id: str, request: AdapterRequest) -> Job: ...
    def cancel(self, project_id: str, job_id: str) -> Job: ...
    def drain(self) -> None: ...        # run pending work to completion; tests drive this
    def close(self) -> None: ...        # stop the runner, release leases, close adapters

class AdapterRegistry:
    def get(self, kind: AdapterKind) -> Adapter: ...
    def close(self) -> None: ...

class Adapter(Protocol):
    def invoke(self, request: AdapterRequest, job: Job, *, queue: JobQueue) -> AdapterResult: ...
    def close(self) -> None: ...
```

- `submit` **persists the job and returns immediately.** It never runs the adapter on the
  caller's thread.
- `Adapter` is a thin shim per adapter, each wrapping the existing `BlenderAdapter.render`,
  `ComfyAdapter.generate` and the FR-21 engine. **Do not change the three adapters'
  signatures** — they are tested and reviewed. Adapt to them.
- `drain` exists so tests can run the pipeline deterministically with no sleeping. The
  production runner uses a single worker thread; `drain` is the same code path invoked
  synchronously.

## Where the command lock is held, and where it is not

This is the part most likely to be got wrong.

`CONCURRENCY_CONTRACT.md` requires every service approval/revision and
`resume_invalidations` to hold the same per-project command lock, and nested service
commands on one thread to raise `CommandInProgress`.

**The adapter must run OUTSIDE that lock.** Holding a per-project mutation lock for the
189 s of a generation would block every approval, revision and recovery on that project
for the duration — a creator reviewing shot 3 would be locked out by shot 7 rendering.

Therefore:

1. `submit` takes the lock **briefly** to persist the queued job, then releases it.
2. The adapter runs with **no service lock held at all**. It writes only into its own
   contained run directory, which is append-only and belongs to no entity yet.
3. Recording the result takes the lock again, **briefly**, through the service boundary.

A job therefore spans three lock acquisitions and holds none across the adapter. Any
implementation that keeps the lock across the adapter call is wrong, however green.

## In-flight work versus append-only versioning — the real correctness problem

A render takes minutes. In those minutes the creator can revise the shot, and a
rebuild-triggering change invalidates downstream through the dependency graph. So a job
can complete holding an artifact for a version that is no longer current.

**Binding rule.** A submitted job records the `(record_id, version)` it was derived from,
and the result is recorded against **that exact version**, never against whatever is
current at completion. Specifically:

- The artifact is **always retained**. Versioning is append-only and artifacts are never
  deleted; a completed render is evidence even when superseded.
- If the bound version is still head, the result attaches and may be approved.
- If the bound version has been superseded, the result attaches **to the version it was
  rendered from** and is marked stale. It does **not** become the current master and it
  does **not** satisfy a Review. Upstream change moves downstream to `stale`; it does not
  delete.
- The recording mutation uses the mutation token read **at recording time**, not one
  captured at submit. `expected_version` is the advancing mutation token, never a content
  version, and a token captured minutes earlier is guaranteed stale.
- A pending invalidation cascade blocks new service approvals and revisions. Recording a
  job result must therefore tolerate `resume_invalidations` having run, and must not
  itself attempt a cascade — it is not a revision.

A job whose bound record no longer exists at all is a terminal failure with a remediation
hint, not a silent discard.

## Error mapping and the async boundary

`submit` returns a persisted `Job`. The HTTP routes for `build_scene`, `render_preview`,
`generate_media` and the FR-21 operation therefore stop being `501` and become **`202
Accepted`** carrying the job id, not `200` with an artifact. Status is read through the
existing `report_jobs`. This is a change to the API contract's response shape and must be
recorded as an amendment there, with the `202` added to the error/response table.

Failures keep the existing single error-body shape via `orchestrator/api/errors.py`:

- Admission refusal from `JobQueue.preflight` (insufficient VRAM, RAM or disk) is a
  `JobError`, already mapped — 409 if retryable, 422 if terminal, carrying `kind`,
  `retryable` and the remediation hint.
- A missing adapter stays `501` with its `adapter` key. An adapter that exists but is
  misconfigured is **not** a missing adapter and must not borrow that status.
- Cancellation is the existing taxonomy value the adapters already use; it is not an
  error-free outcome and not a generic failure.

**The throughput floor.** Both existing adapters currently demand
`throughput_floor_per_second=0` because the stall watchdog cannot tell startup from a
stall. A startup-aware watchdog is being built in parallel. This layer must not hard-code
a zero floor to work around it: read the floor from `Settings` and pass it through. If the
watchdog lands first, the runner inherits a working floor for free.

## Settings

Add nothing that is not needed, and invent no measured-looking numbers. Several `Settings`
defaults in this project are already marked UNMEASURED PLACEHOLDER, and `AGENTS.md`
forbids adding another that looks measured. Any new key states which recorded figure in
§8.4 or a spike it comes from, or is marked an explicit placeholder.

## Test rules

- **No GPU, no Blender, no ComfyUI, no FFmpeg, no network.** The three adapters are
  already tested against real hardware; this layer is tested against adapter fakes
  satisfying the `Adapter` protocol. Driving real hardware here would re-test them and
  hide the integration.
- Time is injected. `drain` replaces sleeping. No test may depend on the number or order
  of monotonic calls — an earlier pass had to fix exactly that.
- Required negative cases:
  - a job submitted, then its shot revised to a new version before completion: the
    artifact is retained, attaches to the bound version, is marked stale, and does not
    satisfy a Review;
  - a job whose bound record is deleted before completion: terminal failure with a hint;
  - recording a result while a pending invalidation cascade exists;
  - admission refused by preflight, surfacing `kind`, `retryable` and `remediation`;
  - cancellation after submit but before the adapter starts, and after it has started;
  - an adapter raising, leaving no lease held and no partial result recorded as current;
  - two submissions for one project serialising, and the GPU lease never held twice;
  - a service revision proceeding **while** an adapter is mid-run, proving the command
    lock is not held across the adapter. This is the test that pins the central decision.
- At least one test asserts **API/CLI parity** for the newly wired operations, driven from
  the route table as the existing parity test is.

## Open questions for the human — do not guess these

1. **Does a `202` break the UI contract?** FR-14 says the UI must use this same backend
   and AT-10 tests the operation enumeration. Async job submission is the only honest
   shape for 189 s work, but it changes what every one of these routes returns.
2. **Should a superseded-but-complete render be offered to the creator at all?** The
   append-only rule retains it. Whether the review surface (FR-10) should show "this
   render is for an older version of the shot" is a product decision, not an engineering
   one.
3. **Per-project or global serialisation?** §8.3 serialises GPU work and there is one GPU,
   so global is the safe reading. If two projects should be able to interleave CPU-only
   degradation while one holds the GPU, say so — it changes the runner's shape.
