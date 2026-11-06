# Contract — FR-09, assembling shots into one playable cut

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

The creator's direction, in their words: *"I should be able to make the program follow an
entire storyline or overall plot of an episode."* Everything exists to make one shot; there
is no way to make an **episode**. Spike 10 produced two delivered shots in one episode and
they remain two separate files.

FR-09: *"Combine approved shots, static cards, pauses, and audio using a declarative
`Timeline` entity (§10.1). Export intermediate shot files and the complete cut.
**Acceptance:** Generate one playable 60–180 second MP4 in the target profile from the
project's manifests."*

`Timeline` and `TimelineEntry` already exist in `orchestrator/domain/entities.py` and are
used by nothing. `Script.beat_to_shot` already maps beats to shots.

**Audio is out of scope** — the creator's decision of 2026-11-05 moved sound to Phase 2,
and FR-09's own Exit clause does not require it. Implement cards and pauses, not audio.

## Decision 1 — the hard part is that shots no longer match each other

This is the problem to design for, and it is new. Per-shot camera style means one episode
can contain, measured from spike 10:

| Shot | Storage | SAR | Display AR | Frame rate |
|---|---|---|---|---|
| `dvr-2005` | 640×480 | 1:1 | 4:3 | 12 fps |
| `low-light-bw` | **352×288** | **12:11** | 4:3 | **10 fps** |
| `consumer-2002` | 640×480 | 1:1 | 4:3 | 30 fps |

**A naive concatenation of these is wrong and will either fail or silently corrupt.**
FFmpeg's concat demuxer requires matching parameters; the concat *filter* requires matching
resolution and will stretch or letterbox incorrectly if SAR is ignored.

Requirements:

1. **The episode declares one delivery spec**, and every shot is conformed to it. Default
   it to the `PostProfile` named by `Project.output_profile`, which already carries
   `storage_resolution`, `output_fps` and `sample_aspect_ratio`. Do not invent a new
   resolution.
2. **Conform on display aspect ratio, never on storage dimensions.** §4.2's aspect rule
   and §12.2 both insist on this, and the 352×288 / 12:11 case is exactly where a storage
   comparison gives the wrong answer. A shot whose display AR already matches is scaled;
   one that differs is pillarboxed or letterboxed, never stretched.
3. **Conforming must not erase the per-shot look.** Upscaling a 352×288 shot to the
   delivery resolution is correct and keeps its soft, coarse character — that difference
   between cameras is the point of the feature, not an artefact to clean up. Do not
   sharpen, denoise or otherwise "improve" a shot while conforming it.
4. **State the frame-rate policy you chose and why.** Mixing 10, 12 and 30 fps into one
   timeline requires a decision; make it explicitly rather than inheriting an FFmpeg
   default.

## Decision 2 — the timeline is declarative, and the cut is reproducible

- The `Timeline` is **data**, built from the project's manifests, and is persisted before
  anything is encoded. A reader must be able to see what the cut will contain without
  running it.
- Entry order comes from the episode's shot order. Where `Script.beat_to_shot` provides a
  beat ordering, prefer it and say so; do not invent a third ordering concept.
- **Only shots with a delivered artifact may be entered.** A shot that has not been
  degraded has nothing to cut; fail loudly naming the shot, in the same shape as
  `"has no scene output; run build_scene first"`.
- The assembled cut is a `ShotOutput`-like receipt at the **episode** level, hashed, with
  provenance recording the delivery spec and the FFmpeg identity — reuse
  `orchestrator/platform/ffmpeg_identity.py`, do not write a second comparison. If the
  entity model cannot express an episode-level output, **say so and report it** rather
  than forcing it into a per-shot record.

## Decision 3 — containment and supervision are not optional

- Every path goes through `ProjectRoot` (FR-25), including intermediates.
- **Run FFmpeg under the supervisor**, as the master assembly does: registered through the
  control object, `stopped()` checked before launch and while waiting, a wait loop
  honouring the stop event and the remaining budget, and `clear_process()` only after a
  clean wait. A bare `popen` plus `wait()` here would reintroduce the cancellation
  fail-open that an automated review caught in the master assembly. Read
  `orchestrator/adapters/ASSEMBLY_CONTRACT.md` Amendment 5 before writing that code.

## Out of scope

Audio; the review UI; FR-15 location reuse; the rubric scorer; `Location`; static cards
beyond a minimal black-card-with-text, if you implement them at all — say which you did.

## Test rules

- **No GPU, no Blender, no ComfyUI, no network.** FFmpeg may be invoked if a test needs a
  real encode; prefer asserting on composed argv and on the persisted `Timeline`.
- The regression that matters: **an episode mixing two camera profiles assembles into one
  file at the delivery spec, and the 352×288 12:11 shot is not stretched.** Assert on
  display aspect ratio.
- Required cases: a timeline is persisted before encoding and names the shots in order; a
  shot without a delivered artifact fails loudly naming the shot; the delivery spec comes
  from the project's profile; the frame-rate policy is applied; the episode receipt hashes
  the produced file; conforming does not alter a shot whose spec already matches.
- Every assertion carries a message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline: **1456 passed, 34 skipped**, ruff clean.
