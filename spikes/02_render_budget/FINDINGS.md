# Spike 02 — Eevee render budget and the first end-to-end slice

Run 2026-11-03. Blender **5.2.0 LTS** (`fbe6228777e7`), RTX 5070 Ti Laptop,
ffmpeg 8.1.1. Fills §8.4's `Eevee Next frame cost` and `Storage` rows and corrects
the `Blender` row.

## Finding 1 — the pinned Blender version was wrong

Two installs exist: **4.4.0** at `D:\Work\Blender` and **5.2.0 LTS** at `D:\Blender`
(build 2026-07-14). §8.4 had pinned 4.4.0 from the earlier probe. That is the wrong
pin:

- 4.4.0 **cannot open** the project's own assets — `bowling_arena_v2.blend` fails with
  *"incomplete header, may be from a newer version of Blender."*
- 5.2.0 is LTS, which is what a pinned toolchain wants.

**The Eevee engine id differs between them**: `BLENDER_EEVEE_NEXT` on 4.4,
**`BLENDER_EEVEE`** on 5.2. A hardcoded id would have failed outright. §8.3's rule of
detecting engines by assigning and reading back earned itself within a day of being
written.

## Finding 2 — cost is resolution × samples, not scene complexity

Two game-asset fixtures, 1280×960 at 32 spp:

| | dojo (glTF) | bowling_arena (blend) |
|---|---|---|
| Mesh objects | 9 | 849 |
| Triangles | 60,423 | 68,198 |
| Materials | 8 | 56 |
| Asset lights | 0 (one added) | 15 |
| Seconds/frame | 0.59 | 0.71 |

94× the objects and 15× the lights cost **+20%**. Budgeting should therefore be driven
by resolution, sample count and frame count — not by polygon or object budgets.

These are stylised game assets, so a synthetic fixture matching §4.1 was built
(`synthetic_interior.py`): 6 × 4 × 2.7 m room, procedural rough surfaces with bump
variation, 22 clutter props, three practical panels with mismatched colour
temperature, volumetric haze, raytraced reflections.

| Configuration | s/frame | 15 s shot @ 60 fps master |
|---|---|---|
| Flat (no RT, no volumetrics) | 0.452 | 6.8 min |
| + raytraced reflections | 0.566 (+25%) | 8.5 min |
| + volumetric haze | 0.569 (**+3.6%**) | 8.5 min |
| Sustained over a 120-frame master | **0.56–0.63** | **8.5 min** |

## Finding 3 — short timing runs overstate cost

An 8-frame measurement put volumetrics at **0.94 s/frame (+95%)**. A 120-frame run of
the same configuration gave **0.50–0.57 s/frame**, and a warm re-measurement put the
marginal cost of volumetrics at **+3.6%**. The difference is per-feature warm-up that
amortises away.

Any figure feeding §8.4 or AT-15 must come from a run long enough to amortise warm-up.
A short run is not a conservative estimate — it is a wrong one, and wrong in the
direction that would have caused volumetrics to be cut for no reason.

## Finding 4 — master frame rate is the dominant budget lever

At the sustained 0.57 s/frame, for one 15 s shot:

| Master fps | Render time | vs 60 fps |
|---|---|---|
| 60 | 8.5 min | — |
| 30 | 4.3 min | 2× cheaper |
| 24 | 3.5 min | 2.5× cheaper |

60 fps is what makes frame rate a **post-only** field in §4.4 — any profile between 8
and 20 fps can be decimated from it without re-rendering. That flexibility costs 2.5×
every render. A 180 s episode is ~1.7 h per full pass at 60 fps versus ~40 min at 24.

Worth deciding explicitly rather than inheriting. Not applied — it trades §4.4's
post-only guarantee for throughput, which is a product call.

## Finding 5 — storage: measured, and 5× cheaper than the estimate

For the synthetic interior at 1280×960/60:

| Representation | Per second of footage |
|---|---|
| PNG sequence | 62.1 MB |
| FFV1 master (lossless) | **13.3 MB** |
| Delivered DVR-2005 mp4 | ~0.005 MB |

A 12-shot, 15 s, 3-versions-retained episode projects to **7.4 GB of masters**, against
the ~40 GB I estimated arithmetically in §FR-26 before measuring.

**Caveat that matters:** FFV1 compresses a near-static synthetic scene extremely well,
and this fixture has no animation at all. Detailed, moving content will be
substantially larger, and the delivered-file compression ratio here (~3000:1) is not
representative for the same reason. FR-26's ceiling should be computed from a
**per-project measured rate**, not from a constant.

## Finding 6 — the degradation chain works on real footage

Blender → FFV1 master → pinned `PostProfile` → 640×480, DAR 4:3, 12 fps mp4, with:

- frames reproducible over real geometry
- output frame count matching the `artifact_schedule`
- the burned-in clock (`CAM 03  2005-11-14 16:00:00`) degraded with the image rather
  than composited crisply on top, confirming the FR-21 stage ordering

## Finding 7 — naive camera placement buries the lens

The first fixture put the camera at a bounding-box corner and randomly scattered
clutter; one prop landed in front of the lens and the frame rendered black. A
clearance radius fixed it.

This is a one-line demonstration of why FR-29 resolves framing through a grey-box
compare loop rather than trusting a computed placement. It also says the agent's
camera proposal needs an automatic sanity check — nearest-surface distance along the
view vector — before a render is spent on it.

## Finding 8 — the believability risk is content, not degradation

The output is a recognisable room with convincing artifacts, and it still does not read
as real CCTV: flat untextured surfaces, no real clutter, poor framing. §13's top risk
("procedural Blender sets look synthetic") is confirmed as the dominant believability
risk, **ahead of** the degradation chain, which demonstrably works. Effort belongs in
sourced textures, dressing and lighting (FR-03, §4.1), not in more filter stages.

## Reproduce

```
blender --background --factory-startup --python render_probe.py -- <scene> <out> [frames]
blender --background --factory-startup --python synthetic_interior.py -- _work/master_run 120
python postprocess_master.py
```

`_work/` is disposable and should be gitignored.
