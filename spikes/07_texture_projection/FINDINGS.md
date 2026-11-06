**Verdict:** Independent review scores `baseline.png` FAIL and `projected.png` / `generated-only.png` PASS on criterion 2, clean and degraded: a locked-camera win; opposite-angle continuity fails.

Fixed homogeneous camera projection textures all 24 existing meshes with emission shading and sRGB/Standard. This preserves baked appearance without relighting. The supplied corrected Phase1 scene/camera is the baseline.

Prompt, verbatim:

> Photorealistic empty mundane institutional service room, viewed from a fixed rectilinear 12 mm wide-angle ceiling security camera in the upper left corner, looking diagonally across the entire room toward the opposite corner; 4:3 framing. A small six by four metre room with off-white plaster walls, grey lino floor, three practical fluorescent ceiling panels with mixed warm and cool white light, and about twenty ordinary cardboard boxes and plain cylindrical containers scattered across the floor. Natural perspective, physically plausible architecture, no fisheye, no barrel distortion, no timestamp, no text, no overlay, no digital noise, no VHS effects, no person, no dramatic lighting, no reference image.

| Path | Measured wall clock | Idle ? peak MiB (delta) |
|---|---|---|
| Baseline | 38.0?45.0 s/cold frame | 2,270 ? 3,364 (+1,094) |
| Generated-only | 189.2 s overall; 51.5 s prompt | 2,270 ? 10,719 (+8,449) |
| Projected | generation + 34.6?35.3 s/cold frame | 2,270 ? 3,350 (+1,080) |

These are sampled device occupancy, not process reservations. Historical baseline: 294.5 s/900 frames, 1,937?3,042 MiB. DVR encodes cost 4.3/5.8/5.5 s respectively; master/post total 140.8 s.

Frames 1/450/900 match exactly within each path; full 900-frame projection was not rendered. `second-camera.png` shows streaks, duplicated fragments and black gaps. DVR degradation preserves the original-view win. `figure-proxy.png` floats/intersects pictured clutter: synthetic depth disagrees, and emission cannot receive contact shadows. Only one proxy pose was tested.

No creator/blind review or small-offset tolerance test. Generated grain/lighting are baked. First startup failed after 191.7 s; its VRAM telemetry was lost. Initial atomic staging escaped the spike folder; fixed. Generated-control video lacks explicit aspect metadata. Geometry-aligned reconstruction and creator review could change the verdict. Next work: geometry, occlusion and contact lighting.

Evidence: `comparison.png`, `results.json`, `verification.json`. Fresh reproduction: `python -B spikes/07_texture_projection/run_spike.py all --run-id NEW_ID` using the specified venv. Retained changes are confined here; uncommitted.

## Adjudication — what projection buys, and what it costs

Written by the orchestrator after viewing `comparison.png` directly rather than accepting
the scored verdict. The visual verdict holds: the baseline grey-box render reads as a set —
flat planes, boxes that look placed rather than left — and the projected and generated
frames read as a real institutional store room, clean and degraded alike. On §4.3
criterion 2 alone, **projection wins and it is not close.**

**It is still the creator's call, because the cost is architectural, not cosmetic.**

### The cost the spike measured but did not name

Compare the baseline and projected frames as *content*, not as quality. They are not the
same room. The baseline is the authored spike-02 interior — 6×4×2.7 m, desks, scattered
clutter, warm practicals. The projection is a cool institutional store room with boxes
ranged along the walls, because **the generated still decides what the room contains** and
the geometry is reduced to a projection surface. Four consequences follow:

1. **Scene control is given up.** FR-04 exists to produce a scene *programmatically from a
   brief*; under projection the brief reaches the room only as a text prompt, and the
   geometry no longer determines what is seen. A Script that calls for a specific prop in a
   specific place cannot be satisfied by asking a model nicely.
2. **The anomaly cannot be integrated.** `figure-proxy.png` shows it plainly: the figure
   stands among pictured boxes it cannot occlude or be occluded by, casts no contact
   shadow, and is lit by a different light than the room it appears in. The image's depth
   and the geometry's depth disagree. §4.3 **criterion 5 is "integrated anomaly — the
   horror element sits inside the recording rather than pasted over it"**, so projection
   trades a win on criterion 2 for a likely loss on criterion 5. FR-20 is the whole point
   of the shot.
3. **A second camera collapses.** `second-camera.png` is stretched fragments and black
   voids. Tolerable for one locked CCTV camera — the genre's camera does not move — but
   **FR-15 (intra-episode location and camera reuse) is P0**, and reusing a location across
   cameras is exactly what projection cannot do.
4. **§4.4's rebuild-triggering fields become no-ops.** The projection is applied as an
   *emissive* material, which by construction ignores scene lighting. So exposure,
   practical light colour and intensity, and monochrome night — the fields §4.4 classifies
   as rebuild-triggering precisely because re-rendering is what changes them — would
   re-render to the same picture. The entire Low-light B&W profile stops working.

### Recommendation

Projection is a **backdrop** technique, not a scene technique. The honest framing is not
"projection versus Blender" but "what must be geometry, and what may be a picture": the
anomaly and anything it touches, occludes or is lit by must stay real geometry; distant
surfaces that only need to look plausible may be projected. That hybrid is unproven here —
the spike did not test it, and point 2 above is the reason to doubt that a pasted figure
in a pictured room passes criterion 5.

**What the spike settles:** the believability gap is real, it is a Blender *surfaces and
dressing* gap, and a generated image closes it on a locked camera.
**What it does not settle:** whether a shot containing an anomaly can be made to pass both
criterion 2 and criterion 5 at once. That is the question Phase 1 actually has to answer,
and no measurement here answers it.

### Caveats on the numbers

Costs are sampled device occupancy, not job-level allocator figures, and §8.4 warns the
device reading is contaminated by whatever was free at launch. Projected full-shot
throughput is unmeasured: only cold single frames were timed, so the 34.6–35.3 s/cold frame
figure must not be compared against the 0.323 s/frame sustained master cadence. The first
run failed after 191.7 s and lost its telemetry.
