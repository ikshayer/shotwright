# Phase 1 acceptance run — the shot, and the §4.3 scoresheet

Run 2026-11-07. Everything through the real CLI, every step exit 0.

| Step | Wall clock |
|---|---:|
| `create-project` → `propose-shots` | < 0.2 s total |
| `build-scene` | 1.83 s |
| `render-master` | 488.50 s |
| `degrade-media` | 163.73 s |

**Delivered:** `h264, 352×288, SAR 12:11, DAR 4:3, 10 fps, 150 frames, 15.000 s, 153 KB`
— `low-light-bw`, low-key living room, humanoid figure walking at `event_sec = 8.0`.

## §4.3 scoresheet — pre-scored by the orchestrator, **awaiting creator ratification**

Sampling per §4.3: one frame every 3 s of the cut, plus the first anomaly beat and the
longest neutral interval (0–8 s, sampled at 4 s). Contact sheet: `rubric-samples.png`;
individual frames in `samples/`.

| Sample | 1 mount | 2 place | 3 no movement | 4 stable | 5 anomaly | Score |
|---|---|---|---|---|---|---|
| 0 s | pass | pass | pass | pass | n/a | 5/5 |
| 3 s | pass | pass | pass | pass | n/a | 5/5 |
| 4 s (longest neutral) | pass | pass | pass | pass | n/a | 5/5 |
| 6 s | pass | pass | pass | pass | n/a | 5/5 |
| **8.4 s (first anomaly beat)** | pass | pass | pass | pass | **FAIL** | 4/5 |
| 9 s | pass | pass | pass | pass | pass | 5/5 |
| 12 s | pass | pass | pass | pass | **FAIL** | 4/5 |

**Result: PASS.** §4.3 requires ≥4 of 5 on ≥80% of sampled frames with criterion 3
mandatory on every frame. All 7 samples score ≥4 (100%), and criterion 3 passes on all 7.

### Reasoning, criterion by criterion

- **1, mount inferable.** The high corner view looking down across the room reads as a
  wall-mounted camera. Consistent across every sample.
- **2, plausible place.** This is the criterion that failed outright two days ago when the
  room was grey boxes. At 352×288 monochrome with grain and vignetting, the living room
  reads as a real room captured badly. The relight (spike 11) and the degradation chain do
  the work jointly; the master alone would not pass.
- **3, no unmotivated movement.** `low-light-bw` is a CCTV profile, so `camera_mode` is
  `cctv` and the plan carries no camera keyframes. Verified in rendered pixels by spike 12,
  where the locked-off control produced a single distinct pixel hash across four frames.
  **Mandatory, and it passes on every sample.**
- **4, stable light and geometry.** No animated lights; no warping or swimming across the
  sequence.
- **5, integrated anomaly.** Fails on two of the three anomaly-bearing samples, and this
  is the honest weakness of the run — see below.

### Where criterion 5 fails, and why

Measured mean luminance of the right third of frame, where the figure walks:

| Sample | right-third mean |
|---|---:|
| neutral frames (0, 3, 4, 6 s) | 26.1 ± 0.1 |
| 8.4 s | **22.68** |
| 9 s | **23.59** |
| 12 s | 25.99 |

- At **8.4 s**, the beat itself, the figure is at the right frame edge and **partially
  clipped**. A figure cut by the frame reads as a framing accident rather than something
  inside the recording.
- At **9 s** it reads clearly — head and torso, occluded by furniture, lit by the scene.
  This is what criterion 5 is asking for, and it is achieved.
- At **12 s** it has walked into the darkest part of the room and is no longer legible;
  the right-third luminance has returned to the neutral baseline, confirming it is not
  merely subjective.

**The arithmetic passes and the intent is only partly met.** The figure geometry is not at
fault — spike 14 showed it reads as a person when silhouetted against the lit window. The
fault is **staging**: the declared walk path `[1.5, −1.0, 0] → [1.5, 1.5, 0]` crosses the
unlit right side of the room and begins at the frame edge.

**Recommendation to the creator:** restage the walk so the figure crosses in front of the
lit window, and re-run. That is one parameter change and one render. The gate passes
either way; the shot is better with it.

## Traceability — the source package

By the creator decision of 2026-11-06, the Exit clause's "traceable source package" is the
provenance chain rather than a built export. For this shot:

- `scene` receipt → `scenes/s1/v0001/<run>/scene.json`, sha256 recorded
- `master` receipt → `master.mkv`, `engine_id BLENDER_EEVEE`, `engine_build fbe6228777e7`
- `degraded` receipt → `degraded.mp4`, `profile_semver`, `filter_graph_hash`,
  `pinned_binaries` (full 12-line FFmpeg identity), `frame_count` and `output_fps`
- the append-only job journal records every state transition with its error vocabulary
- the shot's content version binds all three receipts

Any delivered frame traces to the Shot version, scene plan, engine build and run that
produced it.

## Two honest caveats on the Exit clause

1. **"No user-supplied assets."** No footage, image or model is creator-supplied; geometry
   is procedural and nothing is sourced. The overlay **font** is placed by the creator at
   `assets/fonts/overlay.ttf`, because the pipeline deliberately ships none — a typeface
   is a creative choice and redistributing one carries a licence obligation. Read as
   excluding creator-supplied *media*, this is met; a stricter reading would not be.
2. **The scene is not authored from the Script.** Geometry comes from the procedural
   living-room builder; the Script's beats were persisted and played no part in it. FR-04
   asks for a believable interior built from a text brief. **The interior is believable;
   it is not built from the brief.** Phase 1's Exit clause does not require it, but it is
   the largest thing the phase leaves unfinished.
