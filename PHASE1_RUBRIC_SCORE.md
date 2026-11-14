# Phase 1 believability rubric score (PRD 4.3)

Scored 2026-11-13 against `post/s1/v0001/fd1c4c5d21384a4d93d982a2cdd89e8a/degraded.mp4`
in project `_work/phase1_restage3/exit` (170 frames, 10 fps, 17.000 s), content SHA-256
`4287f27d88d3400ef3464be9b2a6e192966eaa609f0577db892d192e8dff8b97`.

This is the first scoring sheet to exist for this shot; no stored rubric record preceded it.

## Creator verdict

Presented criterion by criterion with the evidence below, the creator replied: "The five
criterion looks good to me." Recorded as **all five criteria satisfied on every sampled
frame**, which clears 4.3's threshold of 4 of 5 on 80% of samples with criterion 3
mandatory throughout.

Scope: this is the 4.3 rubric score only. It is not publication clearance, not approval
gate C, and not acceptance of any H3 candidate -- see the open conflict below.

## Samples

One frame every 3 seconds of the cut: 0, 3, 6, 9, 12, 15 s.

| # | criterion | evidence | verdict |
|---|---|---|---|
| 1 | Mount inferable | Fixed high corner framing, consistent barrel distortion, `CAM 03` overlay | satisfied |
| 2 | Plausible place | Sofa, coffee table, shelf unit, television, curtained window; ordinary domestic room | satisfied |
| 3 | No unmotivated movement | **Measured:** divergence from frame 0 of 1.1 to 4.1 across the clip. A locked camera measures 0.5-0.7 on the same metric and a first-person walk measures 47.3. The rise near 9.6 s is the figure entering frame, not camera motion | satisfied on every sampled frame |
| 4 | Stable light and geometry | **Measured:** luminance range 18.0, with one step from 37.2 to 23.5 at approximately 9.9 s, coinciding with the anomaly beat. Flagged to the creator as the one criterion at risk and judged motivated | satisfied |
| 5 | Integrated anomaly | Figure casts a wall shadow consistent with the lamp and shares the surrounding grain and low light. This is the defect the TRELLIS-mesh restage was performed to fix | satisfied |

Divergence is the mean absolute difference between frame 0 and frame t over a 64x48
grayscale downscale: a fixed camera stays flat, a travelling one diverges and stays diverged.

**Deviation from 4.3's sampling rule, recorded rather than glossed:** criteria 3 and 4 were
measured continuously across the whole clip instead of on the two designated fixed sequences
(first anomaly beat, longest neutral interval). Continuous measurement is broader in coverage
but does not follow the letter of the rule, and no per-sequence score was produced.

## The conflict found at scoring time, and how it was resolved

The H3 first-person candidate accepted on the same day (`H3_PROMPT_FINDINGS.md`, Stage J)
could not pass this rubric as written. Criterion 3 forbade unmotivated movement and that
candidate measures 47.3 on the divergence metric; criterion 4 forbade flicker and it
contains eight blackouts. Both were requested by the creator.

Resolved the same day by amending 4.3 rather than by waiving the shot: criteria 3 and 4 now
test for **unauthored** defects. Movement and lighting change are admissible where the
shot's recorded generation intent declares them -- for an H3 shot that intent is the prompt,
pinned by `H3Candidate.prompt_sha256`, so authorship is checked against an artifact rather
than a recollection. Geometry instability, and any movement or lighting change the intent
does not declare, still fail.

This does not change the score above. The Phase 1 shot has no recorded generation intent,
so criteria 3 and 4 applied to it in their original fixed-camera form, which is how they
were scored.
