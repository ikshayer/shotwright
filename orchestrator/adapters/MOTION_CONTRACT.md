# Contract — camera motion, and the profile mode that currently enables it

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

The creator wants an analog-horror production pipeline that switches camera style per shot,
and asked specifically for found footage. `consumer-2002` — §4.2's non-CCTV handheld
profile, which the PRD calls **"not optional"** because REF-001 supplies a locked-off range
*and* a handheld pass — now exists as a profile but **renders a completely static image**.
§4.2 specifies it as "autofocus hunting, rolling movement, sensor noise" and marks it
**rebuild-triggering**. Sensor noise is post and already present. The movement is not.

## Decision 1 — the profile's mode motivates the motion, and that is a correctness matter

**§4.3 criterion 3 is mandatory on every sampled frame**: "no dolly, rack focus, handheld
shake or zoom **that the profile does not explain**". So handheld movement is not a
free-floating per-shot effect. For `consumer-2002`, the movement is explained by the
profile's handheld-camera description; the `non_cctv` camera mode is the current gate,
not a statement about its mount.

§4.2 classifies every profile as **CCTV** or **Non-CCTV**, and `CameraProfile` carries no
such field — a gap found while building `consumer-2002`. Close it with the closed
`camera_mode` vocabulary `cctv` / `non_cctv`:

- **Add a typed mode to `CameraProfile`** distinguishing CCTV from non-CCTV. It is a
  closed vocabulary, not free text. Seed the three existing profiles correctly:
  `dvr-2005` and `low-light-bw` are `cctv`; `consumer-2002` is `non_cctv`.
- **Motion is currently gated by the profile's mode**, not requested per shot. A `cctv`
  profile produces a locked-off camera; a `non_cctv` profile produces the standing sway
  used for `consumer-2002`. A shot cannot ask a CCTV camera to wobble, because that is
  exactly the unmotivated movement criterion 3 forbids.

This gate records the PRD's camera-class vocabulary; it does not mean that every non-CCTV
camera is handheld. Camera class and mount are separate axes: a tripod-mounted camcorder
would be `non_cctv` and should not sway. When such a profile is introduced, use a separate
mount or motion attribute rather than redefining `camera_mode`. For this change,
`non_cctv` remains the motion gate for the existing handheld `consumer-2002` profile.
- `Shot.camera_motion` is currently free-text `str | None` and is consumed by nothing.
  **Do not build on it.** Either leave it untouched or mark it superseded; say which.

This also serves FR-05's hybrid routing, which will need to branch on the distinction.

## Decision 2 — the motion must be deterministic

Invariant 5: seeds derive from `(shot_id, degrade_seed)`. Two builds of the same
`RenderRequest` already produce byte-identical plans and **must continue to**. So:

- Derive the motion's randomness from the existing seed derivation, the same way
  `build_scene_plan` already derives its Blender seed. Do not call `random` unseeded, and
  do not use wall-clock time.
- The motion must be expressed as **camera keyframes in the plan**, which `SceneSpec` and
  the worker already support — the anomaly work added camera animation. Do not add a second
  animation mechanism.
- A render re-run with the same inputs must produce the same movement. State how you
  verified that.

## Decision 3 — what handheld movement is, and what it is not

Aim for a camera held by a person who is **standing still and watching**, not walking. The
creator's references are static observational shots; the handheld pass in REF-001 is a
person holding a camcorder, not a chase.

- Low-amplitude positional drift and rotational sway, with slow irregular timing. A few
  centimetres and a fraction of a degree, not a shake.
- **No zoom, no dolly, no whip pans.** Those are deliberate operator acts and criterion 3
  treats them as unmotivated unless the profile explains them; this profile explains
  holding a camera, nothing more.
- Autofocus hunting is **in scope only if** `depth_of_field` can express it; it is a
  `float | None` on `CameraProfile` today and is rebuild-triggering. If a convincing
  focus hunt cannot be expressed through the existing field, **say so and leave it out**
  rather than inventing a parallel mechanism.

Amplitude values are yours to choose and **must be flagged UNMEASURED PLACEHOLDER**, as
`consumer-2002`'s other invented values are. There is no spike behind them.

## Out of scope

FR-09 episode assembly; the `failing-dvr` profile; the box-proxy figure; FR-17/FR-18
reference ingestion; the rubric scorer; `Location`.

## Test rules

- **No GPU, no Blender, no ComfyUI, no network.** Assert on the plan and the keyframes, not
  on pixels.
- The regression that matters: **a `cctv` profile yields a camera with no motion
  keyframes, and a `non_cctv` profile yields motion** — asserted on the plan the worker
  would receive. The latter is the current `consumer-2002` behavior, not a rule that all
  future non-CCTV mounts must sway.
- Required cases: motion is deterministic across two builds of one request; a CCTV shot
  cannot acquire handheld movement by any route a caller controls; the mode vocabulary is
  closed and an unknown value is rejected; the three seeded profiles carry the right mode;
  amplitude stays within the declared bounds; and an unchanged static scene's plan is
  **byte-identical to today's**, since pinned hashes depend on it.
- Give every assertion a failure message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline: **1456 passed, 34 skipped**, ruff clean.

## Amendment 1 — motion seeds from `motion_seed`, not `degrade_seed`

Raised by the implementer, which noticed Decision 2 conflicts with **FR-30** and asked
rather than choosing. It was right and the contract was wrong.

**What Decision 2 said:** derive the motion's randomness from the existing seed
derivation, i.e. `(shot_id, degrade_seed)`.

**Why that is a correctness defect, not a conformance quibble.** `degrade_seed` is a
**post-only** input. §4.4 splits `CameraProfile` fields into post-only and
rebuild-triggering precisely so that a post change provably cannot require a re-render,
and `AGENTS.md`'s invariant is blunter still: **degradation never re-renders.** If camera
motion derives from `degrade_seed`, then changing a degradation seed silently changes the
master — the split stops holding, and FR-07's promise that post-only fields need no
re-render becomes false.

**What FR-30 specifies** (P2, but normative where it touches this): movement comes from a
"versioned, parameterised motion model, never from ad-hoc keyframes"; every stochastic
component is seeded from **`(shot_id, motion_seed)`**; the model "carries a semver
recorded on the Shot, so a re-render reproduces the same motion exactly". AT-22 requires
that two renders at the same motion seed and model version produce identical camera
transforms, that changing only the seed changes the motion, and that a CCTV profile
cannot be assigned a motion model.

**Binding:**

1. **Add a `motion_seed` to `Shot`**, distinct from `degrade_seed`, and seed the motion
   from `(shot_id, motion_seed)`. Changing `degrade_seed` must leave the camera transforms
   byte-identical; changing `motion_seed` must change them. Test both directions — the
   first is the one that protects §4.4.
2. **Record the motion model's semver on the `Shot`**, as FR-30 requires. It is cheap now
   and prevents a migration later.
3. **Keyframes remain the output representation, not the source.** A versioned
   parameterised model produces them. That satisfies FR-30's "never from ad-hoc
   keyframes" while keeping the single animation mechanism this contract already requires.

**Scope stays P0.** This implements only as much of FR-30 as §4.2's `consumer-2002`
"rolling movement" needs, in a form FR-30 can later extend. FR-30's fuller model — step
cadence and bob, drift with correction snaps, whip-pan magnitude and settle, operator
attention bias — **remains P2 and is not in scope.** Say in the report which parameters
exist and which are deliberately absent, so the P2 pass inherits a clear boundary.

## Amendment 2 — the invalidation table test

`test_tables_cover_every_camera_profile_field` fails because its table does not list the
new `camera_mode`. That test is a **completeness guard and it worked**: it exists to catch
exactly this, a new `CameraProfile` field with no declared invalidation class.

The implementer was told not to touch tests, which was right for behaviour tests. This one
is a registry of fields rather than an assertion about motion, so **the implementer should
add `camera_mode` to it** as part of this change, classified rebuild-triggering to match
the production tag (using `non_cctv` as the representative changed value). Adding the
entry is the fix the test is asking for; changing what the test checks is not.

## Amendment 3 — constrain optical-axis drift

The regression `test_handheld_transform_amplitude_is_small_and_has_no_dolly_or_whip_pan`
is correct. Its 5 cm positional envelope and 0.5 degree angular envelope pass, but the
camera moves 0.019951 m along its optical axis. Decision 3 forbids that dolly: every
sampled frame must satisfy §4.3 criterion 3.

**Binding:** keep the existing local-vector normalization, then damp only its
forward/optical-axis component by 0.4. This caps forward displacement at 0.010 m while
leaving the lateral and vertical samples unchanged and retaining the existing 0.025 m
radial cap. Do not scale the full drift vector or change the test envelope. Keep all
motion parameters flagged as UNMEASURED PLACEHOLDERS.

The existing 2–4 second sample interval must not be shortened by clamping a fresh random
sample to `frame_end`. If the next interval would pass the clip end, hold the current pose
through `frame_end`; this avoids a fast final jump while preserving the already-sampled
lateral and vertical drift.

The implementer changes production code only. The test author independently audits the
existing regression and gate coverage; the test limits remain unchanged. The orchestrator
runs the named CCTV gates, pinned static-plan hash, full suite, and ruff before commit.
