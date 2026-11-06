# Spike 12 — does handheld motion reach the pixels?

Run 2026-11-06, immediately after handheld motion merged with a green suite.

The shot deliberately carries **no anomaly**, so any difference between frames is
attributable to camera motion alone, and a `dvr-2005` shot renders alongside as the
control: a locked-off camera must produce identical frames, which is what §4.3
criterion 3 requires of it.

The lesson being guarded is the first acceptance run's 900 identical frames: **a correct
plan proves nothing until something renders it and the pixels are compared.**

## Defect 1 — handheld shots could not render at all

The first attempt failed outright:

```
error_kind: internal   error_origin: adapter
handheld camera keyframes are profile-derived; caller-supplied camera keyframes are unsupported
```

`build_scene_plan` injects the derived handheld keyframes into `plan["scene"]`, and
`assemble_scene` persists that plan. On render, `_load_scene_spec` reads `plan["scene"]`
and validates it **as a `SceneSpec` input** — so the gate that stops a caller supplying
camera motion rejected the pipeline's own output. **Every handheld shot failed, 100% of
the time**, through the ordinary `build-scene` → `render-master` path.

Neither half's tests could see it: the gate is tested on direct input, the scene artifact
is tested on what it writes, and nothing round-tripped one into the other. This is the
same producer/consumer-across-a-persistence-boundary shape as the earlier `scene_spec`
defect and the FFmpeg pin.

**Fixed** in `_load_scene_spec`: the camera animation is stripped before validating. The
plan is a worker payload and its scene block legitimately carries derived motion, but
derived motion is not part of the scene's identity. Dropping it on load also means a
changed `motion_seed` takes effect on a re-render instead of being pinned by the stored
scene.

After the fix both shots rendered: handheld 93.2 s, locked 109.8 s for 120 frames each.

## Defect 2 — the motion is invisible on short shots. NOT FIXED

With both shots rendering, decoded pixels for frames 1, 40, 80 and 120:

| Shot | distinct pixel hashes |
|---|---|
| `handheld` | **1** |
| `locked` | 1 |

The control is correct. The handheld shot is **wrong**: it should move.

The generated keyframes explain it exactly:

```
frame   1 -> [-2.4774, -1.8641, 2.2547]
frame 120 -> [-2.4774, -1.8641, 2.2547]
frame_end = 120
```

Two keyframes, identical poses. The keyframe interval is **2–4 seconds**, the shot is
**2 seconds**, so the first interval runs past the end and the generator holds the opening
pose rather than introducing a jump. The hold is deliberate and right in itself; the
consequence is that **a shot shorter than the minimum interval has no motion at all**, and
a person holding a camcorder does not stay still for two seconds.

The blind contract test `test_handheld_plan_moves_the_camera_across_worker_frames` passes,
because it uses a shot long enough to contain an interval. The gap is short shots.

**Suggested fix, not yet applied:** scale the interval to the shot so a minimum number of
poses always falls inside it — the interval becomes the lesser of the chosen 2–4 s and
some fraction of the duration — rather than emitting a start pose and a held copy.

## What this run does establish

- The CCTV control is genuinely locked off in rendered pixels, not merely in the plan.
- Handheld shots render end to end once the round-trip defect is fixed.
- Render cost for 120 frames at 1280×960/32 spp: 93.2 s handheld, 109.8 s locked.
