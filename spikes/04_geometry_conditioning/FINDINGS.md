# Spike 04 — geometry conditioning: the mechanism in §9 was wrong

Run 2026-11-04. FLUX.2 klein 4B distilled, fp4 encoder, 1280×960, seed 20051114,
identical prompt across every run.

§9's "geometry-conditioned image synthesis" was added after the ComfyUI inventory on
the assumption that a ControlNet depth pass from the FR-29 grey-box render would hold
camera geometry. That assumption does not survive contact with this model.

## Finding 1 — FLUX.2 klein has no ControlNet path in this build

Its conditioning nodes are `ReferenceLatent` + `VAEEncode`, per ComfyUI's own
`image_flux2_klein_image_edit_4b_*` templates. There is no FLUX.2 ControlNet.

Two consequences, both good for the schedule: **no ControlNet weights need
downloading**, and the `onnxruntime` 1.18 defect (§8.4) does **not** block this path,
because no `controlnet_aux` preprocessor is involved. Blender can emit true geometry
directly; estimating depth from an RGB render was never the right idea when the
geometry is already in hand.

## Finding 2 — `ReferenceLatent` does not preserve geometry

Controlled comparison, same seed and prompt:

| Run | Result |
|---|---|
| control, prompt only | convincing CCTV room, fisheye, ceiling fixture, clutter |
| + grey-box as `ReferenceLatent` | **essentially the same room** |

The two outputs share the same fisheye, the same ceiling fixture, the same shelving and
bucket placement. The reference removed two figures and shuffled some clutter. It
imposed **none** of the grey-box's geometry — a single large box in a near-empty void
from a high corner.

`ReferenceLatent` is content/style conditioning, not structural control. §9's mechanism
as written does not work.

## Finding 3 — img2img partial denoise does hold layout, in a narrow high band

Feeding the grey-box as the *starting latent* and denoising only partway, via
`SplitSigmas` on an 8-step schedule:

| Denoise | Result |
|---|---|
| 4 of 8 (≈50%) | unchanged grey-box; no style applied |
| 6 of 8 (≈75%) | still a grey-box, plus a small label on one surface |
| **7 of 8 (≈87.5%)** | **full CCTV look, coarse layout retained** |

At 7/8 the camera angle survives, and the grey-box masses map onto plausible furniture:
the central box becomes a dark sofa-like mass in the same position, the lower-left
panels become a bin and cabinet, the open floor stays open.

The band is **narrow and high**. Below ~0.8 nothing happens; at ~0.875 the style is
fully applied. This is a parameter the pipeline must name, store per shot and tune —
not a constant.

## Finding 4 — layout retention is mass-level, which limits continuity

At 0.875 the model invents every detail. Two shots of the same room from different
cameras would share coarse masses but **not** details — different clutter, different
fixtures, different wall markings. That is adequate for a one-off establishing shot and
**inadequate for the continuity §9 wanted it for**.

So the routing conclusion inverts: continuity-critical shots belong to Blender
(method 1), and the generative path is for shots where continuity is not the
requirement. Geometry conditioning is a *look* tool, not a *continuity* tool.

## Finding 5 — the generative path looks far better than the procedural one

The prompt-only control is a strikingly convincing security-camera still: fisheye,
grain, crushed blacks, fluorescent ceiling panel, plausible clutter, correct mounting
height. It is better CCTV than anything spike 02's Blender scene produced.

This sharpens the tension §13 already names. Blender gives continuity and costs 8.5 min
per 15 s shot; FLUX gives realism in ~30–60 s per still and no continuity. The
believability gap found in spike 02 is a Blender gap, not a pipeline gap.

Worth testing before Phase 1 commits: whether a generated still can be **projected onto
the Blender geometry as a texture**, which would buy realism and continuity together.

## Finding 6 — `VAEDecode` is the memory ceiling; tiled decode is required

Plain `VAEDecode` raised `RuntimeError: bad allocation` at 1280×960 — the same node
that failed under `--reserve-vram 8` in spike 03. The diffusion model was never the
constraint. `VAEDecodeTiled` (defaults: tile 512, overlap 64) completes reliably and is
the concrete content of §8.3's "lower-memory fallback".

## Method note

The harness now fills any required node input the hand-built graph omits, using
ComfyUI's declared default, and prints what it filled. Two signature drifts were caught
this way (`ImageScaleToTotalPixels.resolution_steps`, `VAEDecodeTiled`'s four tiling
inputs) that would otherwise have surfaced as opaque validation failures.

## Reproduce

```
blender --background --factory-startup --python ..\02_render_budget\synthetic_interior.py ^
        -- ..\04_geometry_conditioning\_work 0 greybox
.venv\Scripts\python.exe spikes\04_geometry_conditioning\run_spike.py
```
