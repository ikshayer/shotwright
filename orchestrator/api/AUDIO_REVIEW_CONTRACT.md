# Audio review contract (2026-11-21)

The step after perception in `GAP_REVIEW.md`'s fix order, agreed with the creator on
2026-11-21. Claude cannot hear, and the perception step returns only measured numbers
for audio. The project already has a verified local Qwen2.5-Omni helper
(`orchestrator/adapters/qwen`, see its `README.md`, `CONTRACT.md`, `VALIDATION.md`) that
listens to a take and writes advisory notes against an intent brief, but it is a shell-only
CLI outside the production job system. This contract exposes it as a queued GPU job
whose report an agent can read over MCP.

**Qwen's output is advisory text, never a fact and never an approval.** Nothing in this
step creates a `Review`, changes a lifecycle state, or calls an approval function. The
agent may quote a report in its own `record_review` opinion; that is existing behaviour.

## R1 — Operations

| Operation | Method and path | Body / query | Returns |
|---|---|---|---|
| `review_audio` | `POST /projects/{project_id}/audio-reviews` | `AudioReviewRequest` | `{job_id, state}` (async adapter operation) |
| `get_audio_review` | `GET /projects/{project_id}/audio-reviews/{audio_review_id}` | none | report projection (R5) |
| `list_audio_reviews` | `GET /projects/{project_id}/audio-reviews` | `target_kind`, `target_id` (both optional; `target_id` requires `target_kind`) | `[{audio_review_id, status, target, created_at}]`, newest first |

`AudioReviewRequest` (`extra="forbid"`):

- `target`: a discriminated union on `kind`:
  - `{kind: "output", episode_id, output_id}` — a ShotOutput or EpisodeOutput found with
    `store.find_output` (any kind with a video or audio stream: preview, master, degraded,
    generated video, cut);
  - `{kind: "asset", asset_id}` — an Asset of an audio type with a local `file_path`
    (TTS takes are such Assets), verified as `measure_asset` does.
- `brief: str` — required; after stripping, 1–4000 characters, no NUL. It states what the
  audio is meant to sound like, including whether hiss, distortion or narrow bandwidth is
  wanted. The report records it verbatim and records `brief_actor` (`"agent"` over HTTP or
  MCP, `"creator"` from the CLI) so a reader knows whose statement of intent it is.
- `mode: "audio" | "joint" = "audio"` — `joint` lets Qwen see sparse frames as well, and is
  valid only for a target with a video stream.
- `start_sec: float = 0` (finite, ≥ 0), `duration_sec: float | None` (finite, > 0). The
  interval must lie inside the media and be at most `qwen_review.max_interval_seconds`
  (R4). The helper's defaults are kept: 25 s windows, 2 s overlap, 500 tokens per window.

`audio_review_id` is the job's `run_id` (as cuts are named after theirs).

## R2 — Validation before queueing (synchronous, no GPU)

`review_audio` answers 422 (or 404 for an unknown target) **before** a job exists when:
the target is missing or fails verification (receipt or Asset checksum); the output is an
image or scene output; the file's extension has no entry in the allowlist below; the
hardened ffprobe (named demuxer, `-protocol_whitelist file`, no probing, deadline-bound,
the perception helpers in `orchestrator/service/perception.py`) finds no audio stream, or no
video stream for `mode=joint`; the interval is invalid, outside the media or too long; the
brief is invalid. Demuxers: the video ones `inspect_output` already accepts, and
`.wav → wav`, `.flac → flac`, `.mp3 → mp3`, `.m4a → mov`, `.ogg → ogg` for Assets.

The request snapshot stored in the job payload carries the target's identity and the
SHA-256 verified at this point, the demuxer, the brief, `brief_actor`, mode and interval.

## R3 — The job

- A new `AdapterKind.AUDIO_REVIEW = "qwen_audio_review"`, in `_GPU_KINDS`, built by
  `_DefaultRuntime.adapter_for` from a new `orchestrator/adapters/qwen/job.py`. It runs under
  the production GPU lease (`JobQueue.run`), so it queues behind renders and holds
  `GpuExecutionSlot` through the lease, like every other GPU job.
- **No Shot side effects.** The Job has `shot_id=None` even for a ShotOutput target, and
  `episode_id` set when the target is an output (so GPU-seconds are charged to that episode;
  an Asset target is charged under the job id). `_record_result`, shot-lifecycle hooks and
  take persistence never run for this kind.
- **Staged, frozen input.** Inside the job, the adapter re-verifies the target, then copies
  its bytes into the repository root under `_work/qwen_review_inputs/<run_id>/source.<ext>`
  through `ProjectRoot` and checks the copy's SHA-256 against the snapshot (mismatch:
  `CORRUPT_ARTIFACT`). The helper reads only that copy. This removes the native backend's
  repository-root restriction as a limit on where projects live, and means a target
  changed mid-run cannot be half-read. The staged copy is removed after the report is
  persisted, including on failure and cancellation; removal failure is recorded in the
  report's errors, not swallowed.
- **Lease-aware helper entry.** Add `QwenAdapter.run_managed(request, *, lease, cancel,
  input_format)` in `runner.py`. It requires `lease` to be the queue's lease and currently
  owned (as `IndexTTS2Adapter.synthesize_managed` requires), then runs exactly the
  standalone `run` path except that it neither takes the helper lock nor acquires
  `GpuExecutionSlot` nor refuses because of the live production lease — the caller holds
  them. The standalone CLI's behaviour must not change.
- **Named demuxer on every helper FFmpeg/ffprobe input of the source** when
  `input_format` is given (`-f <demuxer>` and no probing, alongside the existing
  `file,pipe` protocol restriction). This closes, for this path, the demuxer-allowlist gap
  the helper's `VALIDATION.md` records. The Backend protocol gains a keyword-only
  `input_format: str | None = None` on `probe` and `prepare`; existing callers keep working.
- **Cancellation.** `spikes/23_joint_av_analyzer/analyze.py:run_supervised` gains a keyword
  `should_stop: Callable[[], bool] | None = None`. The supervisor loop checks it at least
  every 0.25 s (resource sampling stays at its 5 s cadence). When it returns true the loop
  stops the owned tree exactly as its existing RAM/900 s stops do and returns a failure
  result marked as caller-cancelled. Nothing else in `analyze.py` changes (this is the second
  narrow edit to that file the parent has authorised; the first was the `BaseException`
  catch). `run_managed` checks `cancel()` before each window preparation and before each
  batch, and passes it as `should_stop`. A cancelled job ends `CANCELLED` with its partial
  report persisted (R5). An FFmpeg window preparation already running is not interrupted
  (each is bounded by the helper's existing timeout); that limit is documented.
- **Worker control.** The adapter exposes `.worker` for the lease, following
  `IndexTTS2WorkerControl`: `pid()` is the orchestrator's pid; `request_queue_stop()` sets the
  stop flag the supervisor polls; `terminate()` sets it and waits for the active run's
  cleanup, returning true only when the supervisor reported verified cleanup; `unload()`
  raises `WORKER_UNREACHABLE` if the last batch's cleanup was unverified; `respawn()` does
  nothing. An unverified cleanup must prevent another batch (as it already does) and fail
  the job.
- **No automatic retry.** One attempt; a failure is terminal for the job whatever its kind's
  usual retry policy (the helper contract forbids automatic retry).
- **Outcome mapping.** Helper report status `completed` → job succeeds (truncated windows
  included, flagged). `cancelled` → `CANCELLED`. `failed` → the first recorded error's
  `JobErrorKind` (unknown kinds → `INTERNAL`). Setup errors from the helper
  (`QwenSetupError`, e.g. missing runtime) → `MISSING_MODEL_ARTIFACT`; `QwenRequestError` →
  `INVALID_MANIFEST`.

## R4 — Settings and budgets

A new settings section `qwen_review` (following `indextts2`'s shape and its `queue_settings`
special case in `_DefaultRuntime.for_kind`):

| Field | Default | Source |
|---|---|---|
| `required_vram_gb` | 8.0 | the helper's measured admission guard |
| `required_ram_gb` | 6.0 | same |
| `required_disk_gb` | 8.0 | same |
| `startup_allowance_seconds` | 960 | one batch (up to 3 windows) runs up to the 900 s native deadline with no intermediate progress |
| `wall_clock_seconds` | 1800 (A1) | **unmeasured placeholder** beyond the two validated runs (131 s for 8 s, 308 s for 35 s) |
| `max_interval_seconds` | 120 (A1) | 5 windows, 2 batches |

The throughput watchdog must not fail a job whose native supervisor is still within its
own limits: the job advances `job.progress` as each window completes, and the kind uses the
startup allowance above for the gap before the first. The wall-clock budget, when reached,
stops the worker through `request_queue_stop` and fails `BUDGET_EXCEEDED`.

## R5 — The report

- The helper still writes its full run directory under `_work/qwen_helper/<report_id>/`
  (raw native receipts, telemetry, prepared windows).
- The adapter then persists, **immutably** (refuse to overwrite, through `ProjectRoot`), a
  project copy at `audio_reviews/<audio_review_id>.json` in the project root holding: the
  full helper report, plus `audio_review_id`, `job_id`, `created_at` (UTC, aware),
  `target` (kind, ids, verified SHA-256, demuxer), `brief_actor`, `mode`, `helper_report_path`
  (repo-relative) and `advisory: true`. It is written for completed, failed and cancelled
  runs alike whenever the helper returned a report; a job that failed before the helper
  produced one writes a minimal record with `status` and `errors` so `get_audio_review` and
  `list_audio_reviews` still see it.
- `get_audio_review` returns a bounded projection, not the native telemetry: `audio_review_id`,
  `status`, `target`, `brief` (text, sha256, actor), `mode`, `input_coverage`,
  `has_truncated_responses`, `windows` (index, input and source intervals, status,
  `raw_response` verbatim, `termination_reason`, `token_cap_reached`, `audio_metrics`,
  `audio_interpretation_note` when present), `errors` (family, kind, message), `limitations`,
  `advisory: true` and a fixed `notice` saying the responses are unverified model output to
  be checked against the measured metrics (`measure_output` / `measure_asset`) and the
  creator's own listening. Unknown id → 404. A record that fails to parse → 422
  `CORRUPT_ARTIFACT`, never a partial answer.
- Reports are not part of export packages (advisory working data).

## R6 — Descriptions, CLI, docs

Each operation's summary meets the description standard and says where it fits:
`measure_asset`/`measure_output` for numbers → `review_audio` with a brief → wait on the job
→ `get_audio_review` → the agent's own `record_review` opinion; that it queues behind GPU
work and takes minutes (about 2 minutes for 8 s, about 5 minutes for 35 s, measured); and
that the result is never an approval. The CLI waits like `assemble_cut` and prints
`{job_id, state, audio_review_id}`. The parent updates the helper `README.md`, PRD §8.2's
stack table (Qwen2.5-Omni as an advisory reviewer with its VRAM slot) and `GAP_REVIEW.md`.

## Adjudications after the test author's questions (2026-11-21)

- **A1 — Budget arithmetic.** A review job is budgeted under its job id, so the existing
  per-shot GPU ceiling (`gpu_minutes_per_shot`, 20 min) applies to it and is the binding
  limit. Defaults become `max_interval_seconds = 120` (5 windows, 2 batches; ~750 s at the
  measured rate) and `wall_clock_seconds = 1800`. A ceiling reached first fails
  `BUDGET_EXCEEDED` like any other job; nothing special-cases it.
- **A2 — Order of cleanup and persistence.** Remove the staged copy first, then persist the
  project report, whose `errors` include a removal failure if there was one. The report
  is written once, after everything that goes into it is known.
- **A3 — Where the worker control lives.** `.worker` is an attribute of the job adapter in
  `job.py`, which is what the lease reads. `QwenAdapter` has no worker attribute; its
  managed entry takes `cancel` and the job adapter wires the control's stop flag into it.
- **A4 — Stop between batches.** `request_queue_stop()` and `terminate()` set the same flag
  `cancel` reads, so a stop requested between batches prevents the next batch and the run
  ends `cancelled` (or, for the wall-clock path, the job fails `BUDGET_EXCEEDED`).
- **A5 — Refused report write.** `audio_review_id` is unique per job, so an existing report
  file means corruption: the job fails `INTERNAL` and the existing file is left unchanged.
- **A6 — `list_audio_reviews` filters.** `target_id` without `target_kind`, or an unknown
  `target_kind`, answers 422.
- **A7 — Output extensions.** Outputs use the video demuxer allowlist only; the audio
  extension map applies to Assets only.

## Review adjudications (2026-11-21)

The independent review reproduced these; each fix is now a requirement.

- **V1 — Cleanup evidence survives report failure (was HIGH).** Cleanup verification is
  established from the helper's batches (or the exception path's evidence) as soon as the
  helper returns, independently of persisting the report. Without proof of verified
  cleanup, `terminate()` is false and `unload()` raises `WORKER_UNREACHABLE`.
- **V2 — The managed run is bound to the verified hash.** `run_managed` takes the
  snapshot's `expected_sha256`, checks the staged file against it before probing and uses
  it as the baseline for every recheck (mismatch: `CORRUPT_ARTIFACT`).
- **V3 — The projection is closed.** Only the keys R5 lists, at every level, with each
  value type-checked; anything else is 422 `corrupt_artifact`. Window `audio_metrics`
  values are scalars (number, bool, null) or lists of numbers and nulls: the helper reports
  per-channel RMS, peak and DC as lists, with null for an exactly-zero channel. The first
  real queued run caught this; the fake backend's metrics had been scalars only.
- **V4 — `should_stop=None` is the old supervisor.** Its timing reads are restored exactly.
- **V5 — No job-type strings in the queue.** The single attempt that keeps its error kind
  is a per-queue setting chosen by `_DefaultRuntime.for_kind`.
- **Accepted limits.** A stop requested while the supervisor is taking a resource sample
  (RAM, process tree, GPU) takes effect when that sample returns. The reviewer simulated a
  4 s sample and measured 4.8 s. And if a caller's `should_stop` blocks, its poll thread
  can outlive the run, since it is joined with a 1 s timeout. The job's own callback reads
  an event and the job's cancel flag and does not block.

## Non-goals

Parsing Qwen's prose into scores or verdicts; using it for references (FR-18 stays as is);
any approval change; exporting reports; changing the helper's prompts, windows, model,
quantisation or thresholds.

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max): `orchestrator/adapters/qwen/runner.py`,
new `orchestrator/adapters/qwen/job.py`, new `orchestrator/service/audio_review.py` (target
resolution, validation, report persistence and projection), `orchestrator/service/runner.py`,
`orchestrator/platform/settings.py`, `orchestrator/api/routes.py`, `orchestrator/api/app.py`,
`orchestrator/api/cli.py`, and the single `should_stop` edit in
`spikes/23_joint_av_analyzer/analyze.py`. `orchestrator/service/perception.py` only to expose
existing probe helpers for reuse, without behaviour change. Existing tests only for
operation lists (`tests/api/test_parity.py`, `tests/api/test_entry_path.py`,
`tests/api/test_adapters.py`) and, if an existing Qwen test pins the Backend signature,
`tests/adapters/qwen/*` for that signature only.

**Test author** (separate agent): only new `tests/api/test_audio_review_contract.py` and new
`tests/adapters/qwen/test_managed_contract.py`. Inference is faked through the helper's
injectable `Backend`; cancellation of `run_supervised` is tested for real on the CPU with a
stand-in worker process (as the existing Qwen interruption tests do).

**Parent:** the docs above, adjudication, and one real GPU run through the queued job on a
short take or output with audio.

**Do not touch:** any other file under `spikes/**` (in particular the creator's uncommitted
`spikes/25_*`, `26_*`, `27_*` files), the ComfyUI tree, any environment.

## Done when

Every requirement has a test; the full suite passes (run alone, `--basetemp` on D:); ruff
reports nothing beyond `main`'s; one real queued review completes on the GPU with verified
cleanup.
