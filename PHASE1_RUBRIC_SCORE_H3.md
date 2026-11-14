# Phase 1 believability rubric score — the 16.5 s H3 shot (PRD §4.3)

Scored 2026-11-14 against
`generated/h3/s1/v0001/3f4b87199fb444949f9e2c1f82aac453/candidate.mp4`
in project `_work/phase1_restage4/exit` (396 frames, 24 fps, 16.500 s), content SHA-256
`0d70d0f821968435c568edbd38f1e56aedcd4b04774403d9d397670a9b63b25a`.

This is a **separate** score from `PHASE1_RUBRIC_SCORE.md`, which covers the 17.000 s
`phase1_restage3` degraded clip and states in its own words that it is "not acceptance of
any H3 candidate". That shot is silent. Since the PRD was amended so sound enters at the
H3 stage, the *finished* Phase 1 shot is this one, and the earlier score does not transfer.

**This is the first score to exercise the 2026-11-13 amendment to §4.3**, because it is
the first shot with a recorded generation intent. Criteria 3 and 4 are judged against the
prompt pinned by `H3Candidate.prompt_sha256`
(`1f5f639d562e531c45f568e498a092132c46f2897ec8127bad703fdcbe84c005`), whose bytes on disk
were re-hashed and matched before anything below was scored.

## Creator verdict

Presented with the frames and the measured traces, the creator replied:
**"I would say the shot passes."** Recorded as ratification of the cut.

The per-sample table below is the orchestrator's pre-score, and it agrees: the cut passes
on §4.3's own arithmetic, so **no waiver is required** and `Review.waiver_text` is null.

## Samples

§4.3's rule: one frame every 3 seconds of the cut. For a 16.500 s cut that is t = 0, 3, 6,
9, 12, 15 — six samples. Luminance and divergence are both measured from the same 64×48
grayscale decode so they are directly comparable.

| t (s) | luminance | divergence | 1 mount | 2 place | 3 movement | 4 light | 5 anomaly | score |
|---|---|---|---|---|---|---|---|---|
| 0 | 28.7 | 0.0 | ✔ | ✔ | ✔ | ✔ | ✔ | 5/5 |
| 3 | 39.1 | 14.9 | ✔ | ✔ | ✔ | ✔ | ✔ | 5/5 |
| 6 | 4.1 | 29.9 | ✘ | ✘ | ✔ | ✘ | ✔ | **3/5** |
| 9 | 37.0 | 26.0 | ✔ | ✔ | ✔ | ✔ | ✔ | 5/5 |
| 12 | 12.9 | 22.0 | ✔ | ✔ | ✔ | ✘ | ✔ | 4/5 |
| 15 | 7.2 | 22.6 | ✔ | ✔ | ✔ | ✘ | ✔ | 4/5 |

**5 of 6 samples at ≥4 of 5 = 83.3%**, against §4.3's threshold of ≥80%, with criterion 3
satisfied on **every** sampled frame as §4.3 requires. **PASS.**

The aggregate was not computed by hand: each row was built as a `RubricScores` record and
checked with the domain's own `RubricScores.passed()`, which encodes the per-sample half of
the rule. All six records are stored on `Review` `rev-16-5-creator`.

## Criterion-by-criterion

**1 — Mount inferable.** Satisfied on five samples: the camera reads as a first-person
eye-level viewpoint moving through a domestic room, and furniture edges give its position.
Fails at t=6, where the frame is black but for one lit window pane and no mount can be
stated from it.

**2 — Plausible place.** The room reads as an ordinary living room — sofa, coffee table,
shelf unit, television, curtained window, doorway — on every legible sample. Fails at t=6
for the same reason as criterion 1: there is nothing in frame to read.

**3 — No unauthored movement (mandatory).** Satisfied on every sampled frame. Divergence
from frame 0 measures 14.9 to 29.9 across the cut, where a locked camera measures 0.5–0.7
on the same metric. That movement is **declared** by the recorded prompt, which states
"the camera travels with the person at eye level" and "the camera moves slowly forward
across the open floor toward the far side of the room". It is also not *in excess* of the
declaration: Stage J measured 37.3 on the same recipe at 8 s, so this cut moves less, not
more, than the recipe's own precedent.

**4 — Authored light, stable geometry.** Geometry is stable throughout: no swimming
surfaces, no warping, and the furniture and architecture keep their shapes across the
sequence. Lighting is where this criterion fails, and the failure is **authorship, not
artifact** — see the finding below. Fails at t=6, 12 and 15.

**5 — Integrated anomaly.** Satisfied. At 11.9 s a pale upright figure stands in the lit
doorway, inside the recording's own grain and lighting rather than pasted over it. The
authored electrical failure reinforces it. The anomaly survived the first-person recompose,
which was not guaranteed.

## Finding: the recorded intent covers 7 s of a 16.5 s cut

The prompt declares five lighting events — "around one second", "around two and a half
seconds", "around four seconds", "around five and a half seconds", "around seven seconds" —
each lasting "only a few frames", and then states: **"Illumination holds steady between
these sharp electrical failures."**

Measured dark runs (frames below 40% of the cut's median luminance):

| run | duration | declared? |
|---|---|---|
| 1.33–2.25 s | 23 frames | yes (~1 s), but far longer than "a few frames" |
| 4.12–4.29 s | 5 frames | yes (~4 s) |
| 5.79–5.96 s | 5 frames | yes (~5.5 s) |
| 6.04–6.88 s | 21 frames | yes (~7 s), far longer than "a few frames" |
| 8.17–8.75 s | 15 frames | **no** |
| 9.50–9.96 s | 10 frames | **no** |
| 10.83–11.12 s | 7 frames | **no** |
| 12.12–12.71 s | 15 frames | **no** |
| 13.12–14.83 s | 42 frames (1.75 s) | **no** |
| 15.54–16.12 s | 15 frames | **no** |

The cause is mechanical and known: **the prompt was written for the 8 s Stage J shape and
reused unchanged for a 16.5 s cut.** Holding the prompt byte-identical was deliberate — it
made this a test of duration rather than a second, confounded prompt experiment — but the
consequence is that the intent declares nothing after ~7 s while the shot runs to 16.5 s.
Under the amendment, lighting change the intent does not declare fails criterion 4, so the
back half fails it on measurement.

This is recorded rather than argued away. It does not change the verdict: the cut clears
§4.3's threshold with the failures counted, and the creator ratified. The cheap fix, if
this shot is ever regenerated, is to extend the prompt's timed lighting events to span the
full 16.5 s — a small edit to a recipe already proven.

## Scope

This is the §4.3 rubric score only. It is not publication clearance and not approval gate C.
The figure asset and the overlay font remain restricted to internal evaluation and are not
cleared for redistribution.
