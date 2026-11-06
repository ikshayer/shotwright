# Contract — scene animation and FR-20 anomaly primitives

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

The first complete Phase 1 acceptance run rendered 900 frames in 294.8 s and **every frame
was pixel-identical**. `synthetic_interior()` sets no keyframes, `SceneSpec` carries only a
static `camera_location`/`camera_target`, and nothing in the plan varies with frame number.
A master render currently costs `frame_end ×` the time to produce one distinct image.

`ObjectSpec.keyframes` already exists and `adapters/blender/worker.py` already applies it
with `keyframe_insert(data_path="location", frame=...)`. So the adapter can animate; the
scene description and the composer never ask it to.

FR-20 — *Figure staging and anomaly primitives* — is **P0** and annotated in the PRD as
"the horror itself". In code it is a single empty `Shot.figure_params` dict. It blocks
§4.3 rubric criterion 5 (*integrated anomaly*) and makes one of the two mandatory rubric
samples, "the first anomaly beat", impossible to locate.

## Decision 1 — the scene description gains time

`SceneSpec` is static. It gains the ability to express motion **without** changing how a
static scene is described, so every existing caller and test keeps working.

1. **Camera animation.** A camera that can move is required to express "no unmotivated
   movement" as a *deliberate* property rather than an accident of having no camera rig at
   all. Add camera keyframes alongside `camera_location`/`camera_target`. A scene with none
   renders exactly as it does today — byte-identical plans for unchanged inputs.
2. **The plan must vary with frame number.** `build_scene_plan()` is the single source of
   the worker payload; animation belongs in it, expressed as keyframes the worker applies,
   **not** as a per-frame plan. One plan still describes the whole shot.
3. **Determinism is unchanged.** Seeds still derive from `(shot_id, degrade_seed)`. Two
   builds of the same `RenderRequest` still produce byte-identical plans.

## Decision 2 — FR-20's vocabulary, exactly as the PRD enumerates it

> static silhouette; linear walk path; appear/disappear between frames; partial occlusion;
> light flicker; door or object movement; object appearing/disappearing between frames.

Seven primitives, **a closed set**. Each carries typed per-shot parameters — the PRD names
path, timing, distance, occluder and subtlety. Model them as a discriminated union with a
`kind` field, validated, in a new module. **Do not invent an eighth.**

Constraints that are normative, from §3.2 and FR-20:

- **No visible faces above 48 px of frame height.** Enforced, not documented.
- No lip-sync and no articulated acting — the primitives simply cannot express them.
- **Minimum subject distance and maximum on-frame height are enforced per shot**, and a
  violation is a hard validation failure naming the offending value, not a clamp.

Timing is a **typed parameter**: FR-20's acceptance says "reveal timing is editable as a
typed parameter rather than by regenerating the shot from scratch". A change of reveal time
must not require re-authoring the anomaly.

## Decision 3 — where it attaches

`Shot.figure_params` is an untyped `dict[str, float | str | bool]` and **cannot hold this**
— a discriminated union of seven primitives with per-kind parameters does not fit a flat
scalar dict. Decide and state which you did:

- a typed field on `Shot` holding the anomaly specification, with `figure_params` left for
  its existing purpose or deprecated in place; **or**
- a typed structure composed into `SceneSpec` by `compose_render_request`.

Whichever you choose, the composer must carry it from the domain record into the render
request, so an anomaly survives from `propose-shots` to the rendered frames. `Shot` already
carries `event_sec`, which is when the anomalous event occurs — **use it**; do not add a
second representation of the same time.

## Out of scope

Degradation composition, sequence-to-video assembly, the rubric scorer, the texture
projection experiment, and any new render operation. Do not touch `orchestrator/api/**`.

## Test rules

- **No GPU, no Blender, no ComfyUI, no network.** Fake the subprocess; assert on the plan
  and on the worker payload, not on pixels.
- The regression that matters: **a shot with an anomaly produces a plan whose frames are
  not all identical**, and a shot without one still produces today's static plan unchanged.
- Required cases: each of the seven primitives validates and appears in the plan; an eighth
  `kind` is rejected; the face-height, minimum-distance and maximum-height constraints each
  fail loudly with the offending value named; reveal timing is editable by changing one
  typed parameter and changes the plan accordingly; `event_sec` drives the anomaly's timing
  rather than a duplicate field; a static scene's plan is byte-identical to today's; two
  builds of one request are byte-identical.
- Give every assertion a failure message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline: **1351 passed, 34 skipped**, ruff clean, about 190 s.
