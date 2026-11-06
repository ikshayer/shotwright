# Contract — handheld camera motion, and the mode that motivates it

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
free-floating per-shot effect. It is legitimate only because the camera is a handheld
camera, and the record has to say so.

§4.2 classifies every profile as **CCTV** or **Non-CCTV**, and `CameraProfile` carries no
such field — a gap found while building `consumer-2002`. Close it:

- **Add a typed mode to `CameraProfile`** distinguishing CCTV from non-CCTV/handheld. It is
  a closed vocabulary, not free text. Seed the three existing profiles correctly:
  `dvr-2005` and `low-light-bw` are CCTV, `consumer-2002` is not.
- **Motion is derived from the profile's mode**, not requested per shot. A CCTV profile
  produces a locked-off camera; a handheld profile produces movement. A shot cannot ask a
  CCTV camera to wobble, because that is exactly the unmotivated movement criterion 3
  forbids.
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
- The regression that matters: **a CCTV profile yields a camera with no motion keyframes,
  and a handheld profile yields motion** — asserted on the plan the worker would receive.
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
