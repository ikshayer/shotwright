# Phase 1 acceptance run — measured

Run 2026-11-06 on this machine. Driver: `run_spike.py`, which drives the real CLI
(`orchestrator.api.cli.main`) rather than the service layer, so the entry path is what
gets exercised. Evidence and artifacts land in `_work/phase1`, which is gitignored and
disposable; the numbers below are the durable part.

    cd <repo>
    PYTHONPATH=. D:\Work\video_pipeline\.venv\Scripts\python.exe spikes/06_phase1_acceptance/run_spike.py

## Verdict

**The pipeline rendered a complete 900-frame master sequence end to end, and it is not a
shot.** Every frame is the same picture, nothing assembles them into a video, and the
rubric is unscored. Phase 1 does not exit on this. The run is still worth having: it is
the first time the entry path, composition, queue, lease, journal and Blender supervision
have all run together against real hardware, and it produced measurements the project did
not have.

What ran, all through the CLI, every step exit 0:

| Step | Result |
|---|---|
| `create-project` | 0.031 s. Also seeds the `dvr-2005` CameraProfile named by `Project.output_profile` — creating it again is a `version_conflict`. |
| `create-episode` | 0.016 s |
| `draft-script` | 0.016 s, two beats |
| `propose-shots` | one shot, `duration_sec=15.0`, `event_sec=8.0` |
| `build-scene` | **0.250 s**, no subprocess, no GPU lease, CPU admission |
| `render-master` | **294.796 s**, `state: completed`, `progress: 1.0` |
| `report-jobs` | both jobs `completed` |

## Measurements

**Per-frame cost at the master, which §8.4 did not have:**

- **0.323 s/frame** — 900 frames at 1280×960, Eevee, 32 samples.
- Median inter-frame gap **0.322 s**, maximum **0.356 s** over 471 timed intervals.
- This is roughly **twice as fast** as §8.4's 0.56–0.63 s/frame, which was measured on
  spike 02 and not at the master resolution. §8.4 should be updated to distinguish the two.

**Job record (`resource_metrics`):**

- `elapsed_seconds` 294.531
- `vram_baseline_mb` 1936.75, `vram_peak_mb` 3041.785 — a delta of **1105 MB**
- `lease_holder_pid` `null` on the completed record, naming pid 13468 while running

**Output:** 900 EXR frames, `frame_000001.exr` … `frame_000900.exr`, **1.96 MB each,
1.76 GB for one 15-second shot**. A 30-second shot is ~3.5 GB, against a
`disk_ceiling_gb` default of 40 — so roughly eleven 30-second shots fill a project.

**Receipts** (`episodes/ep1/shots/s1/outputs/`):

- kind `scene`, `record_version` 1 → `scenes/s1/v0001/<run>/scene.json`
- kind `master`, `record_version` 1, `engine_id` **BLENDER_EEVEE**, `engine_build`
  **fbe6228777e7** → `renders/s1/v0001/<run>/attempt-0001/frames/frame_000001.exr`

## The 900 frames are one picture

**All 900 frames are pixel-identical.** Decoding frames 1, 2, 450 and 900 to raw RGB24 and
hashing gives the same digest for all four. The `.exr` files have distinct file hashes, but
only because of embedded per-frame metadata — a file-hash comparison says "900 unique
frames" and is wrong. Compare decoded pixels, not EXR bytes.

So the 294.796 s render produced **one unique image**, 900 times. Hand-assembling the
sequence with FFmpeg gives a valid 15.000 s / 1280×960 / 60 fps clip of 461 KB — small
precisely because every frame is the same.

The cause is the fixture, not the adapter: `ObjectSpec.keyframes` exists and
`adapters/blender/worker.py` applies it with `keyframe_insert`, but `synthetic_interior()`
sets no keyframes on any object, and `SceneSpec` carries only `camera_location` and
`camera_target` with **no camera animation of any kind**. `Shot.camera_motion` was null.
Nothing in the plan varies with frame number.

Two consequences worth stating:

- **A master render is currently pure waste** beyond the first frame. Until something
  animates, rendering `frame_end` frames costs `frame_end ×` the time to produce one
  distinct image.
- **A per-frame determinism check is meaningless here**, and so is any motion-based rubric
  criterion. Neither can be assessed until the scene animates.

Visually the frame is correct — a warm-lit interior with boxes and cylinders, plausible
shading, soft shadows from two practical wall lights. It is also **not a CCTV image**: no
degradation, grain, overlay, decimation or artefacts, because that is the PostProfile stage
and it is unwired.

## Why Phase 1 does not exit on this

**1. There is no finished shot, because nothing assembles frames into a video.** The
master receipt's `relative_path` names **`frame_000001.exr`** — one still out of 900.
`_artifact_path` in `orchestrator/service/runner.py` takes `frame_paths[0]`, and the
PostProfile adapter's FFmpeg argv is `-i <single file>` with no `-framerate`,
`-start_number` or `%06d` pattern. So `degrade_media` fed this receipt would degrade one
frame rather than fifteen seconds, and no step produces a playable 15-second artifact.

This needs a creator decision before it is built: **does a `master` ShotOutput point at an
assembled video, with the frame sequence as an intermediate, or does the sequence stay the
master with a new kind for the encode?** That choice determines what "finished shot" means
in the records, so it is not an implementation detail.

**2. The §4.3 rubric has not been scored.** `score-rubric` exists; nothing has been scored
against this output, and scoring a single EXR still would not be scoring a shot.

**3. The scene is not authored from the Script.** Geometry comes from
`synthetic_interior()`, the measured spike room. The Script's beats were persisted and
played no part in the geometry. FR-04 asks for a believable interior built from a brief,
so this run demonstrates the pipeline, not FR-04's authoring requirement.

## Two defects this run exposed

**Persisted progress stays at 0.0 for the whole render.** Observed directly: at 392 of 900
frames rendered, the journal's latest snapshot still read `progress: 0.0`. Progress is
updated in memory for the watchdog (`adapters/blender/runner.py`, in the monitor path) and
never persisted, so `report-jobs` — an operator's only view of a running job — shows
nothing for five minutes and then jumps to 1.0. The fix is small; it needs a throttle
decision so a 900-frame render does not append 900 journal lines.

**The throughput floor's units are duration-dependent.** See the open finding in
`orchestrator/adapters/PROGRESS_CONTRACT.md`. At the measured 0.323 s/frame both 15 s and
30 s shots pass, so the run was never at risk, but the boundary is only ~52 s of shot away
and moves closer with slower hardware, more samples, or Cycles.

## Not verified here

Cycles (never run anywhere), render determinism across two runs, the 30-second end of
Phase 1's range, any encode, any degradation, and the rubric. The budget ceilings were
raised for this run (3600 s wall clock, 60 GPU-minutes) because the defaults are
UNMEASURED PLACEHOLDERs; at 294.8 s the **default** 900 s wall clock would in fact have
held for 15 s, and on these numbers a 30-second shot at ~590 s would too.

# Second acceptance run — with an anomaly, and with master assembly

Run 2026-11-06, after FR-20 anomaly staging, scene animation and master assembly landed.
Same harness (`run_spike.py`), now proposing a `linear_walk_path` anomaly and attempting
degradation.

## What is now true that was not

**The frames differ.** Decoded to raw RGB24 and hashed: frames 1, 300 and 480 are
identical; frames 520, 600 and 900 are each distinct. `event_sec = 8.0` is frame 480 at the
60 fps master, so **the scene is static until exactly the declared event frame and changes
from there** — the anomaly's timing is driven by `Shot.event_sec` as FR-20 requires, not by
a duplicate field. Compare decoded pixels, never EXR file hashes: the files differ in
embedded metadata even when the pictures are identical, which is how the first run's 900
identical frames went unnoticed.

**The master is one assembled file.** The `master` receipt points at
`renders/s1/v0001/<run>/attempt-0001/master.mkv`, not at `frame_000001.exr`, and carries
`engine_id BLENDER_EEVEE`, `engine_build fbe6228777e7`. Probed: **FFV1, 1280×960,
gbrp16le, 60/1 fps, duration exactly 15.000000 s**. Both of the defects the first run
exposed are closed and proven end to end, not just in tests.

## Costs, measured

| | First run | This run |
|---|---:|---:|
| `render-master` wall clock | 294.8 s | **996.7 s** |
| Job `elapsed_seconds` | 294.5 | 988.6 |
| VRAM baseline → peak | 1936.8 → 3041.8 MB | 2266.6 → 3360.7 MB |
| Frames on disk | 1.76 GB | 1.76 GB |
| Master on disk | — | **2.40 GB** |

The anomaly adds one object and cannot account for the difference, so **master assembly
costs roughly 690 s** — about 0.77 s per frame, more than twice the 0.323 s/frame the
render itself takes.

**Two findings follow, and the first is actionable.**

1. **The encode is single-threaded.** The argv passes `-threads 1`, presumably for
   bit-exactness. FFV1 supports deterministic multi-threading through fixed slice counts
   (`-slices`), so a substantial speedup is likely available **without** giving up the
   byte-identical reproducibility AT-05 requires. This should be measured before anyone
   accepts 690 s as the cost of a master. It was not measured here.
2. **A lossless master is larger than its lossless source.** 2.40 GB of FFV1 from 1.76 GB
   of EXR, because the conversion to 16-bit RGB full range discards EXR's compression
   without discarding information. Total for one 15-second shot is **4.16 GB** against a
   `disk_ceiling_gb` default of 40 — about **nine shots per project**, before any
   degradation output. Retention of the frame intermediates is out of scope here but is now
   clearly load-bearing, not housekeeping.

This is a real cost of the orchestrator's Decision 2 (lossless master). The determinism
argument for it stands — a lossy master bakes in artifacts no `PostProfile` asked for, and
AT-05 needs a stable input — but the price is now known and should be weighed rather than
assumed.

## Degradation is still unreachable, as predicted

```
PostProfile 'dvr-2005' cannot be composed; missing persisted field: overlay.font_relative
```

Amendment 3 made the overlay font a profile field, because a caller-supplied font makes
AT-05's byte-identical guarantee unenforceable. The profile that `create-project` seeds
predates the field, so **every existing project is now unable to degrade until its profile
gains a font**. The field was kept optional on the model so schema-v1 profiles still load,
and composition rejects them loudly instead — which is the right failure, but it is a
migration gap, and the repository has **no registered FR-28 migration mechanism**.

This was left to fail rather than hand-patched, so the gap is visible in the record.

## What the anomaly actually looks like

The figure is placed correctly: right perspective, occluded by the room's clutter, walking
the declared path at roughly 4.3 m from the camera, within the shot's declared 2.0 m
minimum and 400 px ceiling. **It is a featureless dark rectangular prism** — a box proxy,
not a humanoid mesh.

So FR-20's *staging* is proven and its *appearance* is a placeholder. §4.3 criterion 5 asks
that "the horror element sits inside the recording rather than pasted over it"; a box is
inside the recording, and it is not yet a figure. Closing that needs either a humanoid
proxy mesh or §9's routing method 3 (a generated image insert), and the projection spike's
adjudication already warns that a pasted insert struggles with contact shadow and depth.

## Still not done

No rubric score. No degraded delivery. The scene is still `synthetic_interior()`, so the
Script does not drive geometry. A viewable H.264 copy of the master is at
`D:\Work\video_pipeline\_work\phase1_shot_v2.mp4` — it is a convenience for inspection and
is **not** a pipeline output.

# Third acceptance run — the complete chain, and a delivered artifact

Run 2026-11-06, with the procedural living room, FR-20 anomaly staging, master assembly
and degradation all present. **The pipeline produced a delivered CCTV clip with a valid
receipt for the first time.**

| Step | Result |
|---|---|
| `create-project` → `propose-shots` | all exit 0, under 0.1 s each |
| `build-scene` | 0.281 s, no subprocess, no GPU lease |
| `render-master` | **628.4 s**, completed, 900 frames + FFV1 master |
| `degrade-media` (first attempt) | failed: `overlay font is missing: assets/fonts/overlay.ttf` |
| `degrade-media` (after placing a font) | **exit 0, completed** |

The first degrade failure is the designed behaviour for a fresh project, not a defect: the
seeded profile names its font and the adapter says exactly which path is empty. Placing a
font at that path and re-running is the whole remediation.

## Three receipts, all with correct provenance

```
scene     -> scenes/s1/v0001/<run>/scene.json
master    -> renders/s1/v0001/<run>/attempt-0001/master.mkv
             engine_id=BLENDER_EEVEE  engine_build=fbe6228777e7
degraded  -> post/s1/v0001/<run>/degraded.mp4
             profile_semver=0.2.0  filter_graph_hash=8d045f80…  pinned_binaries present
```

Delivered artifact: **640×480, 12 fps, H.264, 90 KB**, burned-in `CAM 03 2005-11-14
16:00:09`, duration 14.5 s — the 0.5 s shortfall being exactly the six dropped frames the
profile's `artifact_schedule` declares, at 12 fps. The manifest and the output agree,
which is the specific failure spike 01 measured FFmpeg committing silently.

## The cadence discrepancy is resolved

The living-room master rendered at **0.373 s/frame** (median over 899 gaps, max 0.592).
That is close to the 0.323 s/frame measured on the grey-box room over 471 gaps, and
nowhere near the **0.819 s/frame** a 120-frame harness reported for the same grey-box
scene. **The ~0.32–0.37 s/frame figure is the real sustained cadence**; the 120-frame
measurement was the outlier and should not be used for budget arithmetic.

The dressed living room costs about **15% more per frame** than the grey box, which is
consistent with §8.4's finding that cost is resolution × samples rather than scene
complexity. Total `render-master` wall clock of 628.4 s decomposes as roughly 336 s of
frames plus ~290 s of FFV1 assembly.

## §4.3, judged on the delivered frame

This is the first run where the rubric can be judged on the surface the PRD actually
scores — FR-22 measures on the delivered frame "because the master is deliberately clean".

Judged by eye on one sampled frame, **not** a conducted rubric score:

- **1, mount inferable** — plausible pass. The high corner view reads as a wall-mounted
  camera.
- **2, plausible place** — plausible pass, and a clear change. The clean-CG tells that
  made the master read as an architectural visualisation — even lighting, unworn matte
  surfaces, uniform materials — are substantially destroyed by 640×480, grain and blown
  highlights. **The room now reads as a real room captured badly**, which is the target.
- **3, no unmotivated movement** — pass. The camera is static by construction.
- **4, stable light and geometry** — not assessed across the sequence.
- **5, integrated anomaly** — **fail.** The figure is a featureless dark prism. At this
  resolution it is ambiguous enough to read as furniture rather than as a person, which is
  the opposite of "the horror element sits inside the recording".

So the believability work has moved criterion 2 from a clear fail to a plausible pass, and
criterion 5 is now the binding one. **This is an eyeball judgement on a single frame and
is not a rubric score.** §4.3 requires deterministic sampling — one frame every 3 seconds
plus the first anomaly beat and the longest neutral interval — scored across the cut, with
criterion 3 mandatory on every sample. That has still not been run.

## What remains

A humanoid figure instead of a box proxy; the §4.3 rubric actually scored; and the Script
still does not drive the geometry, so FR-04's "believable interior from a text brief"
is unmet even though the interior is now believable.
