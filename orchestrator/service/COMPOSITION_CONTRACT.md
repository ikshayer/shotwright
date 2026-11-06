# Contract — composing adapter requests from domain records (FR-04, FR-21, FR-14)

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

`build_scene` fails, and thanks to the diagnosability work the record now says why:

```
error_origin: adapter   cause: ValidationError
traceback: ... runner.py:103  RenderRequest.model_validate(payload)
8 validation errors for RenderRequest
  shot_version / run_id / degrade_seed / duration_sec / camera_profile / scene  Field required
  project_id / episode_id  Extra inputs are not permitted
```

The raw route payload reaches the adapter because **nothing turns domain records into the
adapter's typed request.** The integration contract says `payload` is "the adapter's own
request model, passed through untouched", which means something upstream must build it.
Nothing does. This is that something.

## Where it lives, and what it may not do

A new module in the service layer: `orchestrator/service/compose.py`. The service layer is
where work spanning store and domain belongs.

It **reads** records and **returns** typed requests. It writes nothing, mutates nothing,
and holds no lock. It is pure given a store, which is what makes it testable without GPU,
Blender or FFmpeg.

**It must not invent a profile, a seed or a resolution at call time.** Every value comes
from a record or from `Settings`; a default that exists only in composition code is a
second source of truth, which this project has been bitten by repeatedly.

## The three creation gaps that block it

`RenderRequest` needs a **`CameraProfile` entity** — not the `"dvr-2005"` *string* that
`Shot.camera_profile` holds. `PostProfileSpec` needs a **`PostProfile` entity**. Neither
has a creation operation, so neither record can exist. This is the same class of gap as the
missing episode creation, and the whole-table guard cannot catch it because there is no
route at all to fail.

**Binding:**

1. Add `create_camera_profile` and `create_post_profile`, following `create_episode`
   exactly: persist an agent-authored entity, creation not revision, entering the service
   boundary under the command lock with no cascade, conflict rather than overwrite, and
   URL/body identifier matching. Both reachable from HTTP **and** CLI (AT-10), and both
   must appear in the operation table and `orchestrator/api/CONTRACT.md`.
2. **`create_project` materialises the named defaults into the project.** `Project` already
   carries `output_profile: "dvr-2005"`, and the PRD names that profile, so a text-only
   Phase 1 run must work without anyone authoring a profile first. Materialise them as
   ordinary project-local records — **not** read from code at render time, because
   invariant 1 makes project-local JSON the source of truth. Record where the default came
   from so it is traceable.
3. The defaults' *content* comes from what is already measured. `dvr-2005` is named
   throughout the PRD; spike 01 measured the degradation chain and
   `spikes/01_postprofile_determinism/post_profile.json` holds a real profile. **Use the
   measured file; do not compose a new chain by hand.** If a field has no measured value,
   say so rather than inventing one.

## What to compose, and where each value comes from

### `RenderRequest` (Blender, for `build_scene` and `render_preview`)

| Field | Source |
|---|---|
| `shot_id` | the `Shot` |
| `shot_version` | the content version from `load_with_version` — a **content** version, never a mutation token |
| `run_id` | generated per attempt, and recorded on the `Job` so the artifact is traceable |
| `degrade_seed` | **`Shot.degrade_seed`** — it already exists on the record |
| `duration_sec` | `Shot.duration_sec` |
| `camera_profile` | the `CameraProfile` record named by `Shot.camera_profile` |
| `scene` | a `SceneSpec` — see below |
| `mode` | `"preview"` for `render_preview`, `"master"` for a final render |
| `engine` | `"eevee"`. The engine id on this machine is `BLENDER_EEVEE`, **not** `BLENDER_EEVEE_NEXT`. Cycles stays opt-in and has never been run here |
| `master_resolution`, `samples`, `gpu_device` | leave the model's defaults unless `Settings` carries a value |

**Do not derive the stage seeds.** `orchestrator/domain/seeds.py` already provides
`derive_seed(shot_id, degrade_seed, stage, maximum)` and the adapters call it themselves.
Composition supplies the **base** `degrade_seed` only. Deriving it here would duplicate the
derivation and break determinism's single source (invariant 5).

### The `SceneSpec`, and the honest limit

`blender/scene.py` exposes **`synthetic_interior()`**, the measured spike room. For a
text-only Phase 1 shot that is the scene source: use it.

Say plainly in your design note that this is **not** FR-04's "builds a believable hallway
from a text brief" — a real procedural builder driven by the Script is later work. Wiring
the measured room is what makes a shot reachable now; pretending it reads the brief would
be a lie in the provenance.

### `PostProfileSpec` (FR-21, for degradation)

`identity` is the `PostProfile` record named by `Project.output_profile`. `output_fps`,
`storage_resolution`, `artifact_schedule`, `overlay` and `encode` come from that record and
from the measured profile. FR-21 supports **frame-indexed drop/freeze and `in_recording`
overlays only** — timestamp-unstable events and `on_playback` are deferred to P1 by creator
decision, so do not compose either.

## Out of scope, and why

**`generate_media` is excluded.** `ImageRequest` requires `models: tuple[ModelPin, ...]`,
and each `ModelPin` needs a path, a SHA256, a source and a licence id. Those hashes exist
only as **prose in §8.4** — there is no machine-readable pinned-model manifest, and
`Settings.models` carries only roots and an extra-paths file. Composing a `ModelPin` would
mean inventing provenance for executable model weights, which FR-25 forbids outright.
Report it; do not build it. A pinned manifest is its own piece of work.

## Test rules

- No GPU, Blender, ComfyUI, FFmpeg or network. Composition is pure given a store, so test
  it directly against real records.
- Required cases: a composed `RenderRequest` validates and carries the shot's own
  `degrade_seed`, `duration_sec` and content version; `shot_version` is the content version
  and not a mutation token; a missing `CameraProfile` record fails with a clear error rather
  than a validation cascade; `mode` differs between preview and master; the composed
  request round-trips through `RenderRequest.model_validate` without extras; the same shot
  composes identically twice except for `run_id` (determinism, invariant 5); a
  `PostProfileSpec` composes from the project's named profile; and composition performs no
  writes.
- Creation cases for both new operations mirror `create_episode`'s: creates, conflicts
  rather than overwrites, identifier mismatch is 400, invalid payload is 422 from the
  entity's own validators, API/CLI parity driven from the route table.
- One case must assert `create_project` materialised the defaults, and that a render can be
  composed on a freshly created project **without any profile authoring**.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline on `main`: **1291 passed, 31 skipped**, ruff clean, about 153 s.

**The real criterion is the pipeline.** `build_scene` must stop failing on
`RenderRequest` validation. Drive the CLI walkthrough — project, episode, script, shots,
build-scene — and report the resulting job record. If Blender then fails for a *Blender*
reason, that is progress and the record will now say so; if it succeeds, say what it wrote.

## Amendment 1 — adjudication, and what the pipeline now hits instead

**Composition works.** `build_scene` passes `RenderRequest` validation and the job reaches
`running`. Verified independently by re-running the CLI walkthrough: the project contains
`camera_profiles/dvr-2005/v0001.json`, `post_profiles/dvr-2005.json`, the episode, the
script at `v0001` and the shot at `v0001` — all as project-local records, so invariant 1
holds and nothing is read out of code at render time.

Suite after reconciliation: **1323 passed, 33 skipped**, ruff clean.

### Ratified, including two places the implementation beat this contract

- **`compose_render_request(store, *, shot_id, episode_id, run_id, mode)`** is the
  signature. The contract named the module but not its functions — the same omission that
  produced the `AdapterRequest` mismatch — so the test author assumed positional ids and
  no `run_id`. The implementation is better on both counts and the tests moved:
  keyword-only arguments make transposing two same-typed id strings impossible, and
  `episode_id`/`shot_id` are exactly that hazard; and taking `run_id` as an **input** keeps
  composition pure, which is what makes "deterministic except `run_id`" testable without
  patching randomness. The determinism test is now stronger for it — two *different*
  run ids, every other field identical.
- **`PostProfile` was extended in the domain**, with `PostProfileOverlay` and
  `PostProfileEncode`, rather than composition reading the measured spike JSON at call
  time. The test author found that gap (`PostProfileSpec` needs fps, resolution and encode
  settings that `PostProfile` had nowhere to put) and the implementer closed it in the right
  layer, with the spike files' hashes recorded as provenance.
- `compose.py` performs no writes, takes no lock and derives no stage seeds; it supplies the
  base `Shot.degrade_seed` only, leaving `domain/seeds.py` the single source of derivation.
- The design note states plainly that `synthetic_interior()` is **not** FR-04's "builds a
  believable hallway from a text brief" — the Script and story brief do not drive its
  geometry. That honesty belongs in the provenance.

### Three test reconciliations, all toward the implementation

`test_adapters.py` seeded a `Shot` with no `CameraProfile`, so `/scene` and `/preview`
correctly answered `404 "Shot 'x' references missing CameraProfile 'dvr-2005'"` — an
incomplete fixture, not a routing fault. Its `r"degrad|post.?profile"` matcher also caught
`create_post_profile` and demanded `202` from a creation route; narrowed to `r"degrad"`.
And the profile had to be written with `store.create()`, since `CameraProfile` is a
`VersionedEntity` and the store refuses `save()` with a message that says exactly that.

### What the pipeline hits now, and it is not the adapter

```
error_kind: budget_exceeded
error_message: "VRAM thrash suspected: job progress rate fell below configured floor"
traceback: queue.py _fail_throughput_stall
error_origin: null
states: queued, queued, queued, running, queued, failed, failed
```

**The throughput watchdog is killing a healthy render.** Two distinct defects:

1. **A render reports no progress, so it is indistinguishable from a stall.** The floor
   exists to catch VRAM thrash. Once the startup allowance lapses, a job whose `progress`
   stays at `0.0` is failed — and the Blender adapter does not report progress during the
   render. §8.4 measures 0.56–0.63 s/frame, so a 12-second shot at a 60 fps master is
   several minutes of legitimate silence.

   Note what this means: the original adapter reviews demanded `throughput_floor_per_second=0`
   for exactly this reason. FR-12 made the queue startup-aware and the floor pass made the
   adapters pass an allowance, but **neither made an adapter report progress**, so the floor
   is still unusable for a real render — it now fails later rather than immediately. Either
   the adapter reports progress per frame, or the floor stays off for render-class work.
   Deciding that is the next piece.

2. **The startup allowance has no margin.** `resources.startup_allowance_seconds` defaults
   to `74.0`, and §8.4's measured cold Blender shader cache is **~74 s**. A bound equal to
   its own measurement fails about half the time. This is the same error already corrected
   once for ComfyUI, where a 33.5 s clean-boot figure was used as a bound for work that also
   hashes multi-gigabyte files. A bound must exceed its measurement.

3. **`error_origin` is `null` for queue-raised failures that are not `INTERNAL`.** The
   diagnosability work sets `job_queue` only when the kind is `INTERNAL`, so a
   `BUDGET_EXCEEDED` raised by the watchdog names no layer. Any queue-raised failure should
   say `job_queue`.

### Still open

- **`generate_media` remains uncomposable**, and correctly untouched: `ImageRequest` needs
  `ModelPin`s carrying path, SHA256, source and licence, and those hashes exist only as
  prose in §8.4. Composing one would mean inventing provenance for executable model
  weights, which FR-25 forbids. A machine-readable pinned manifest is its own work.
- **`degrade_media` is not wired to a composed request.** `compose_post_profile_spec` exists
  and is validated against the measured profile, but the contract never said how to select
  the master and font paths a degrade request needs.
- **`create_project`'s three record writes share a lock but have no multi-file recovery
  rule**, and materialising the defaults requires the spike files to be present at creation
  time. Both were reported rather than papered over.
- The measured spike does **not** cover the default `CameraProfile`'s rebuild-triggering
  optics and lighting; those keep domain defaults, and the limitation is recorded in the
  PostProfile provenance rather than hidden.
