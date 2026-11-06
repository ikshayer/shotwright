# Spike 11 — the low-key gap was midtones, not highlights

Run 2026-11-06 by the orchestrator directly, after spike 10 delivered a low-light B&W clip
that still did not read like the creator's references.

## The diagnosis in spike 10 was wrong, and measurement is what showed it

Spike 10 concluded, from looking at the delivered frame, that "there is no clipped
highlight anywhere" and that the fix was to raise the practical until it clipped. That was
recorded confidently in its `FINDINGS.md` and in its commit message. **It was wrong.**

Measuring the fraction of pixels at or above 95% luminance (clipped) and at or below 5%
(near-black):

| Image | clipped | near-black |
|---|---:|---:|
| Reference `134126` | 0.068% | **32.02%** |
| Reference `t1385` | 0.408% | 40.91% |
| Spike 10 delivered low-light | **0.256%** | **54.75%** |
| Spike 10 clean master | 0.016% | 53.41% |

The delivered frame already clipped **nearly four times as much** as the reference. The
real shortfall was at the other end of the histogram: we crushed **54.8%** of the frame to
black where the reference crushes **32.0%**. The room had no readable midtones, which an
eye reads as "murky" and which is easy to misattribute to a missing highlight.

Note also that the degradation chain *adds* clipping — 0.016% in the master became 0.256%
delivered — so the chain was already doing the highlight work. What it cannot do is invent
shadow detail that was never rendered.

## Two dead ends, both the same failure mode

1. **A spill light behind the rear wall did nothing.** near-black moved 53.41% → 53.27%.
   The wall is solid and the glass opaque, so the light was simply occluded — the same
   trap that rendered spike 09's first iteration completely black, where a point light sat
   inside closed cone geometry.
2. **Making the window pane emissive did nothing either.** Clipping leapt to 1.499% while
   near-black moved 0.02 points. **In Eevee an emissive surface is visible but does not
   illuminate**; only real lights do. The pane became a bright rectangle lighting nothing.

Both were found in seconds by measuring rather than by looking, which is the transferable
lesson here.

## What worked

The window is at **x = −0.77**, not the x = 0.15 that was guessed. The bright look already
had a correctly-placed, correctly-aimed window light; reusing that placement as a second
*motivated* practical — not ambient fill, which would have flattened the image — produced:

| | clipped | near-black |
|---|---:|---:|
| Master before | 0.016% | 53.41% |
| **Master after** | **0.014%** | **37.97%** |
| **Delivered after** | **0.248%** | **44.94%** |
| Reference `134126` | 0.068% | 32.02% |

The wood panelling now reads, which it could not at the previous light level.

**A measured relationship worth keeping: the degradation chain adds about 7 points of
near-black** (master 37.97% → delivered 44.94%). So a delivered target of 32% implies a
master near 25%. This run did not go that far; the room is still darker than the
reference.

## Two tests had their premise changed deliberately

`test_low_key_room.py` asserted `len(practicals) == 1` in two places. That encoded the
then-current composition rather than a requirement, and the reference plainly has two
motivated sources. Both were updated with their subjects preserved: one now asserts that
every light is a named practical and that world fill is zero — which is what keeps the
falloff deep — and the other selects the pendant by `kind == "point"` so it still guards
the shade-clearance bug from spike 09.

## Remaining

Still darker than the reference, and still CG: regular surfaces, no wear or lived-in
dressing, and the figure is a box proxy. The §4.3 rubric has not been scored.
