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
