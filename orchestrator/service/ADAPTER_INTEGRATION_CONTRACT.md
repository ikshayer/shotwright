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

## Amendment 2 — adjudication of the first implementation attempt

The implementer and the test author worked in parallel from this file and disagreed in
three places. In each one **this contract was at fault**: it named types it never defined
and assumed a domain capability that does not exist. Recorded here rather than patched
away, because the gap is the finding.

State after that pass: **14 failed, 1129 passed, 30 skipped.** All 11 integration tests
fail, as do the four route-status cases. That work stays on `worktree-integration` and is
**not merged**; `main` remains green.

### `AdapterRequest` and `AdapterResult` are now defined

The contract pinned `submit(project_id, request: AdapterRequest)` and never said what
`AdapterRequest` was, so the two agents invented different types and nothing could match.

```python
@dataclass(frozen=True)
class AdapterRequest:
    kind: AdapterKind
    payload: Any                 # the adapter's own request model, passed through untouched
    record_id: str               # the record this work derives from
    record_version: int          # the content version it was derived from, never a token
    episode_id: str | None = None
    required_vram_gb: float = 0.0
    required_ram_gb: float = 0.0
    required_disk_gb: float = 0.0

@dataclass(frozen=True)
class AdapterResult:
    artifact_path: str           # contained, relative to the project root
    value: Any = None            # the adapter's own result object
```

Reconciled deliberately, not split down the middle:

- **No `Job` on the request.** The implementer put one there, but `submit` *returns* a
  `Job`, so it must create and persist it. A request that already carries a Job makes the
  signature incoherent. Resource declarations come across as plain fields and `submit`
  builds the Job from them.
- **`record_version`, not `version`** — the test author's spelling. `version` alone is
  ambiguous in a codebase where `expected_version` means a mutation token.
- **`payload` is kept** — the implementer was right that an adapter needs its own typed
  request, and the runner must pass it through without interpreting it.

### Staleness is DERIVED, not stored — this contract was wrong

Amendment 1 said a superseded result is "marked stale". That was a mistake. A stored
`stale` flag is a second source of truth for something the data already implies, and this
project has been bitten by duplicated state repeatedly.

**Binding:** an output is stale **iff** its `record_version` differs from the record's
current head version. Nothing writes a stale flag and nothing needs a "mark stale"
mutation. This also removes the awkwardness of a completed job having to mutate a record
it does not own: recording an output appends a row, it does not revise an entity.

### The blocking domain gap this contract assumed away

The implementer reported, correctly, that the store and domain have no operation or
entity field for attaching an artifact to a historical shot version, so it fell back to a
side-car receipt that is not domain-linked and does not use a recording-time mutation
token. The test author independently reported that it cannot assert the exact
result-to-version binding. Both hit the same wall from opposite sides.

They are right, and no implementer could have conformed. `ArtifactScheduleEntry`,
`block_artifacts` and `analog_artifacts` are **degradation parameters** — visual
artifacts — not build outputs. The store has per-version snapshots
(`_read_version_snapshot`, `_lifecycle_path`) for *entities*, and nothing for a produced
file.

**Required before this layer can conform:** a domain record for a produced output, bound
to the content version it came from.

- A new entity, provisionally `ShotOutput`, keyed by `(shot_id, record_version, kind)`
  where kind is scene, master, preview, still or degraded.
- It carries the contained relative path, a content hash, and the provenance the adapters
  already emit — for degradation, FR-21 records `profile_semver`, `filter_graph_hash` and
  `pinned_binaries`; for a render, the engine id and build.
- **Append-only.** A new output never replaces an older one; artifacts are retained
  (invariant 3). Several outputs may exist for one `(shot_id, record_version)` across
  attempts, and all are kept.
- It carries **no** stale flag, per the decision above.
- Approval still binds to `(shot_id, version)`. An output whose `record_version` is not
  head therefore cannot satisfy a Review, with no extra rule needed — the existing
  freshness check already compares versions.

This is a domain and store change. Both agents were forbidden from making it, correctly:
it is an entity-invariant decision, and it is the parent's.

### Still unsatisfied, and not to be reported as done

- **No default runtime wiring.** `create_app` and the CLI submit only when a `JobRunner`
  is injected, so the four operations still answer `501` by default, contradicting
  Amendment 1. The amended status contract is not met until the default app wires a
  runner.
- **Blender and ComfyUI still reject a positive throughput floor.** Three passes have now
  routed around this: FR-12 fixed the queue but was barred from the adapters, and this
  pass was barred too. The partition keeps excluding the fix. Whoever takes it next must
  be allowed inside the two adapters.
- **FFmpeg degradation has no cancellation hook** in FR-21's signature, so cancellation is
  incomplete for that operation. It needs an adapter change, not a runner workaround.
- **The `CameraProfile` to `PostProfileSpec` mapping is partial**, and font provenance for
  `in_recording` overlays is unresolved.
- The implementation was committed **unverified**: no pytest, no ruff, not even an import.
  The brief gave `.venv\Scripts\python.exe`, which does not exist inside a worktree.
  Future briefs must give the absolute shared interpreter path,
  `D:\Work\video_pipeline\.venv\Scripts\python.exe`.

## Amendment 3 — adjudication of the second attempt

The second attempt verified itself properly: ruff clean, imports checked, and all four
wired routes exercised against a stored project, each answering `202` with a `job_id` and
`state: queued`. Combined with the independent tests it is **14 failed, 1146 passed, 30
skipped**. Ten of the eleven integration failures share **one** cause, and that cause is
this contract, not either agent.

### Amendment 1 decision 3 was wrong: the lease is keyed on kind, not on VRAM

Amendment 1 said the runner distinguishes GPU from non-GPU work "by
`Job.required_vram_gb`", and added that "declaring zero VRAM to skip the lease is a defect,
not an optimisation". The implementer turned that into a hard precondition — *GPU adapter
jobs must declare a positive `required_vram_gb`* — which is faithful, and which makes the
layer unusable, because **this project has no measured VRAM reservations to declare.**
Both adapter hardware passes declared zero for precisely that reason, and `AGENTS.md`
forbids inventing a measured-looking number to fill the gap. The rule demanded a figure
the project is not allowed to make up.

**Binding, replacing Amendment 1 decision 3:**

- **`AdapterKind` decides the GPU lease.** `scene`, `render` and `generate` are GPU work
  and take the lease; `degrade` is FFmpeg and does not. The decision is structural and
  cannot be influenced by a caller's declaration.
- **`required_vram_gb` is for admission sizing only**, and **zero is legitimate** while
  reservations remain UNMEASURED. It is not a routing input.
- The thing Amendment 1 was reaching for still holds, and is now true by construction: a
  caller cannot route around the lease by understating resources, because routing no
  longer reads them.

### `submit` validates synchronously

The implementer returns `404` from `submit` when the project does not exist; the test
author expected `202` unconditionally. **The implementer is right.** Queuing work for a
record that does not exist produces a job id for something that was never possible and
forces the caller to poll to discover a `404`-shaped failure.

**Binding:**

- A missing project or bound record at submit time is **`404`**, mapped from
  `RecordNotFound` as the error table already requires.
- A `JobQueue.preflight` admission refusal surfaces **synchronously from `submit`** as a
  `JobError`, mapped 409 retryable / 422 terminal with its remediation hint. Admission is
  checked before a job id is issued.
- `202` therefore means *accepted and queued*, not merely *received*.
- Tests for the wired routes must create the project (and shot) first. The `202`
  assertion does not move; the setup was incomplete.
- **One inconsistency to fix:** `build_scene` currently answers `202` for a nonexistent
  project while the other three answer `404`. All four must validate identically.

### `AdapterResult.value` projection, and the kind mapping

Both were undefined, and the test author flagged both.

- **`AdapterResult.value`** carries the adapter's own result object, passed through
  untouched. The runner projects provenance into `ShotOutput` by reading **only** the
  fields `ShotOutput` declares, taking them from `value` when it is a mapping or from
  matching attributes when it is an object, and leaving absent fields unset. The runner
  never invents provenance and never fails a completed job because a provenance field is
  missing — a missing field is recorded as absent.
- **`AdapterKind` to `ShotOutputKind`:** `scene` to `scene`, `render` to `master`,
  `generate` to `still`, `degrade` to `degraded`. `preview` is produced by a render whose
  request asks for a preview, so it is selected by the request rather than the kind; until
  previews are wired, a render records `master`.

### Corrections to earlier amendments

- **Amendment 1's adapter-absent enumeration was incomplete.** It said the `501` rule
  survives only for `search_assets`, `create_asset` and `export`, and omitted
  **`sample_references`**, which is also adapter-absent and must keep its `501`. The
  implementer caught this. The correct set is `search_assets`, `create_asset`,
  `sample_references` and `export`.
- **Amendment 1's "marked stale" wording is superseded** by Amendment 2: staleness is
  derived from `record_version` versus head and is never stored. Amendment 1's text stands
  as a record of what was decided and then corrected; it is not to be implemented.

### Two real gaps the second attempt surfaced

Both are reported rather than papered over, and neither is the integration layer's to fix
alone.

- **A one-shot CLI invocation can kill its own job.** The CLI submits and returns while the
  in-process worker is a daemon thread, so process exit can terminate work mid-render.
  Async submission is right for HTTP and wrong for a one-shot command. **Binding:** the
  CLI waits for its job by default, reporting the result, with an explicit opt-out for
  fire-and-forget. A CLI that returns before its work survives is worse than one that
  blocks.
- **`JobQueue` has no CPU-only preflight path**, so degradation avoids the GPU lease but
  still probes GPU resources during admission. A CPU-only job can therefore be refused for
  GPU reasons. That is a jobs-layer change and is not this layer's to make; it is recorded
  as a follow-up.

Also still open, from FR-21 rather than from here: the PostProfile adapter cannot stop
FFmpeg mid-run, so a cancelled degradation is recognised only after the process returns
and its artifact is not recorded. That needs the adapter cancellation hook FR-21 did not
build.

The `CameraProfile` to `PostProfileSpec` mapping covers the fields `PostProfileSpec` can
represent; tagged fields with no target, such as bitrate and overlay detail, remain
unmapped. That is a gap in `PostProfileSpec`, not in the mapping.

## Amendment 4 — adjudication of the third attempt

The third attempt conformed to Amendment 3 on all six points. Combined with the
independent tests the integration surface went from **14 failed / 1146 passed** to
**1 failed / 406 passed** across `tests/service` and `tests/api`. Four disagreements were
adjudicated and one new defect was found, which is now the only thing outstanding.

### Provenance: `ShotOutput` wins, and Amendment 3 was wrong

**Both agents independently reported the same contradiction**, from opposite sides.
Amendment 3 said a missing provenance field is "recorded as absent" and must not fail a
completed job; `SHOTOUTPUT_CONTRACT.md` *requires* engine provenance for scene and master
outputs and degradation provenance for degraded ones, and the store revalidates it. The
test author could not write the case; the implementer could not satisfy it.

**Binding:** `ShotOutput` wins.

- Provenance is the point of the record. FR-06's acceptance requires that "the exact model
  artifacts in use are recorded per run", and AT-05/AT-14 determinism rests on it. A master
  with no engine id is not reproducible, so recording one with the field absent would
  destroy the guarantee quietly.
- Amendment 3's "recorded as absent" applies **only** to fields `ShotOutput` declares
  optional. A missing **required** provenance field is a **terminal failure** with a
  remediation hint naming the field and the adapter that should have supplied it.
- In practice this costs nothing: Blender reads back its engine and device literally and
  FR-21 records `profile_semver`, `filter_graph_hash` and `pinned_binaries`. An adapter
  that cannot name what produced an artifact has a defect worth failing on.

### Job state vocabulary: `completed`

The implementer wrote `"completed"`; the test author expected `"succeeded"`. **The
implementer is right**, and not as a matter of taste: `orchestrator/jobs/queue.py:228`
already transitions to `"completed"`, and the queue is the authority on job state. The
vocabulary is `queued`, `running`, `completed`, `failed`, `cancelled`. Correcting this one
word turned eight failing tests green.

### A missing project is not this layer's business

The test author expected `JobRunner.submit` to raise `RecordNotFound` for a missing
project. It does not, and should not: the runner is handed an already-resolved
`ProjectStore`, so the project's existence was settled before `submit` was called.
Resolution and its `404` belong to the API/CLI layer, where `tests/api/test_adapters.py`
already proves it. Making the runner re-validate would be the duplicated path check FR-25
forbids.

A missing **bound record** *is* this layer's business, because the runner loads it, and
that case is covered.

### A cancellation is not a failure

`_persist_job` in the runner hard-coded `state = "failed"` for every error kind, so a job
cancelled before its adapter started reported `"failed"` and overwrote the `"cancelled"`
state that `cancel()` had already written. FR-27 keeps the two distinct so an operator can
tell "I stopped this" from "this broke", and `JobQueue`'s own terminal branch already makes
the distinction. Fixed.

### NEW DEFECT, and the only thing still failing: job records are written non-atomically

Fixing the above exposed a worse problem underneath. The single remaining test failure is
not an assertion mismatch — it is a **torn read**:

```
pydantic_core.ValidationError: Invalid JSON: EOF while parsing a value
  at line 1 column 0 [input_value='']
```

The job record came back **empty**. Two threads write `state/jobs/<job_id>.json`:
`cancel()` writes it on the caller's thread while the worker writes the same file through
`queue._transition`, and `ProjectRoot.write_text` is a plain in-place write with no
temp-and-rename. A concurrent cancel can therefore leave the record empty or partial.

This matters more than the test it broke. **Invariant 1 makes project-local JSON the
source of truth**, and a source of truth that can be read back empty is the worst failure
mode this project has. It is not specific to jobs either — every `ProjectRoot.write_text`
and `write_bytes` caller shares the exposure, and AT-09 requires an exported package to
reopen standalone.

**This is deliberately NOT patched here.** The right fix is atomic writes in
`platform.containment.ProjectRoot` — write to a temp path inside the root and `os.replace`
onto the target, which is atomic on both platforms — and that is a change to the single
containment authority every write in the system passes through. It needs its own contract,
its own independent tests and its own review, not an edit tacked onto this pass. Doing it
there fixes the whole system at once; serialising only the runner's writes would leave the
queue's writes, and every other caller, exposed.

Until it lands, `test_cancel_after_submit_before_adapter_start_is_terminal` stays red, and
the integration work stays unmerged. A green suite bought by papering over a torn write
would be worth less than this failing test.

### Still unresolved, recorded rather than guessed

- Whether `drain()` must re-raise a job's `JobError` to its caller, or whether the
  persisted job is the only authority. The tests currently allow either.
- Which operations may bind to **non-shot** records. `generate_media` is project-scoped and
  carries no shot in its URL, yet `ShotOutput` is shot-keyed; the tests supply shot ids in
  the body to get around it. Either `generate_media` is genuinely shot-scoped and its route
  should say so, or outputs need a non-shot owner. This needs deciding before FR-10.
- FR-21 still has no FFmpeg cancellation hook, so a cancelled degradation is noticed only
  after the process returns and its artifact is discarded.
- `JobRunner` must select `preflight_cpu` for `AdapterKind.degrade`. That path landed in
  `6256a48` and the runner was explicitly barred from touching the jobs layer while it was
  in flight, so the two have not been joined up yet.
