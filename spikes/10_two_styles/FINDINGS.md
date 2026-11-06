# Spike 10 — one episode, two camera styles

Run 2026-11-06. Drives the real CLI to produce **two shots in one episode that differ in
camera profile, degradation profile and scene lighting**, and delivers both. This is the
creator's stated requirement: *"the pipeline can switch between different camera style
depending on the script or what the user wants."*

A style demonstration, not the Phase 1 acceptance run — shots are 5 s so two full renders
fit a reasonable wall clock. Same room and same anomaly staging in both, so any difference
in the delivered frames is attributable to camera style alone.

## It works

Every stage of both shots exited 0.

| Stage | `a-dvr` | `b-lowlight` |
|---|---:|---:|
| `build-scene` | 0.265 s | 0.297 s |
| `render-master` | 242.2 s | 159.8 s |
| `degrade-media` | 137.0 s | 49.8 s |

Delivered, measured with `ffprobe` independently of the implementation's own tests:

| | `a-dvr` | `b-lowlight` |
|---|---|---|
| Storage | 640×480 | **352×288** |
| SAR | 1:1 | **12:11** |
| Display AR | 4:3 | **4:3** |
| Frame rate | 12 fps | 10 fps |
| Duration | 4.500 s | 5.000 s |
| Size | 37 KB | 61 KB |

**§4.2's aspect-ratio trap is handled correctly.** 352×288 is 11:9 in square pixels; the
profile sets SAR 12:11 so the *display* ratio is 4:3, which is what §12.2 requires be
checked. Confirmed by probe, not by assertion.

The duration difference is also correct rather than incidental: the two profiles carry
different `artifact_schedule`s, so `a-dvr` loses 0.5 s to dropped frames while
`b-lowlight` does not.

`b-lowlight` renders **34% faster** than `a-dvr` (159.8 s vs 242.2 s) for the same frame
count — the low-key look removes ambient fill, and Eevee has less to solve.

## The delivered low-light frame, judged against the reference

Compare `b-lowlight.png` with `references/room/Screenshot 2026-11-06 134126.png`.

What is right: monochrome, 352×288, vignetting, grain, a burned-in `CAM 03` clock and a
burned-in `REC [||||]` battery indicator. The low resolution does real work — it hides the
regular surfaces and clean edges that make the master read as CG.

**What is still wrong, and it is one thing: there is no clipped highlight anywhere.** The
reference has a bright blown bulb and a hard pool of light on the table, with black
falloff around it. The delivered frame is uniformly murky — the lamp is visible but never
reaches white. Real night footage nearly always contains at least one blown source, and
that anchor is what makes the rest read as a dark room rather than an underexposed render.

This was predicted from the clean master and is now confirmed on the delivered frame,
which is the surface §4.3 actually scores. **The lever is render-side, not post-side:**
`practical_light_intensity` and `exposure_ev` are rebuild-triggering fields in §4.4, so
raising the practical until it clips is a re-render, and no amount of degradation tuning
substitutes for it.

Secondary, in order of expected payoff: wood panelling is too dark to read at this
resolution; the figure remains a box proxy; there is no lived-in dressing.

## Note on the burned-in indicator

The creator ruled the battery indicator should be burned in rather than an `on_playback`
HUD. It is, and it is compressed and noised with the image as a consequence — so it does
**not** stay crisp as it does in `t1385.png`, where the crispness is precisely what an
`on_playback` overlay produces. This is the expected trade, recorded so it is not later
mistaken for a defect.

## What this does not show

Per-shot routing is proven for camera profile, post profile and scene look. It is **not**
proven across an episode assembled into a single timeline — FR-09 is Phase 2 and no cut
exists. The rubric has still not been scored. `Consumer-2002`, the handheld found-footage
profile §4.2 calls "not optional", is unbuilt.
