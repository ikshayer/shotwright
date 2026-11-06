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
