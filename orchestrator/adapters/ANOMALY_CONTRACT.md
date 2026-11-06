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

## Amendment 1 — the 48 px face rule is superseded, and is not this layer's to enforce

Raised by the blind test author, which noticed that Decision 2's face constraint
contradicts the PRD. It was right and the contract was wrong. Recorded rather than quietly
corrected, because the implementer was dispatched against the wrong text.

**What Decision 2 said:** "No visible faces above 48 px of frame height. Enforced, not
documented."

**What the PRD says** (§3.2, the normative *Face identity-destruction rule*, adopted
2026-11-04 after REF-001 was inspected): a human face may occupy **any** fraction of frame
height provided the **delivered** frame destroys identity-level information, by block
quantisation to ≤16 effective pixels across the face, or by highlight clipping such that
≥60% of the face area lies within 5% of white. It states outright: "This replaces v1.2's
'no visible faces above 48 px of frame height'." FR-20's own section still carries the
superseded sentence, which is where this contract picked it up; §3.2 is later, explicit
about replacing it, and governs.

**Two consequences, and the second matters more:**

1. **There is no 48 px check.** Any anomaly rejected solely for exceeding 48 px of face
   height is rejected wrongly. Remove the rule rather than relax it.
2. **This layer cannot enforce the replacement at all.** The PRD is explicit that FR-22
   measures identity destruction **on the delivered frame, not on the master, because the
   master is deliberately clean**. Identity destruction is produced by the degradation
   chain, so a scene-time validator has nothing to measure: the master is *supposed* to
   contain a clean face. Enforcing it here would reject shots that the delivered frame
   would have rendered compliant.

**Binding:** the anomaly layer carries face-related parameters and **enforces nothing about
faces**. The check belongs to FR-22, on the delivered frame, and is out of scope here. A
test asserting a 48 px rejection is asserting superseded behaviour and must be removed.

**Still in force, because they are FR-20's own staging limits and not about faces:**
minimum subject distance and maximum on-frame height, "enforced per shot". The PRD gives no
global numbers for either — they are **per-shot declared limits**, so the enforcement is
that a shot's staging respects the limits that shot declares, not a comparison against a
constant this contract invents. A degenerate value (zero distance, a figure taller than the
frame) is still a validation failure.

**Also noted for the PRD, not fixed here:** FR-20's section and §3.2 disagree in the
shipped document. §3.2 wins, and FR-20's stale sentence should be struck when the PRD is
next edited.
