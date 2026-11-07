# Spike 13 — assembling an episode cut, and the lifecycle that blocks it

Run 2026-11-06, immediately after FR-09 merged green with 1479 passing tests. The goal was
to turn the two delivered shots of spike 10 into one playable cut. **It did not get
there**, and what stopped it is more important than the cut.

## Three blockers, in the order they appeared

### 1. `Episode.shot_ids` was never populated — fixed

```
JobError: Episode 'ep1' has no shots to assemble
episode.shot_ids     -> []
shot records on disk -> ['a-dvr', 'b-lowlight']
```

`propose_shots` created `Shot` records and never registered them on the `Episode`, so the
manifest FR-02 calls "agent-authored" was empty. Fixed; `propose_shots` now maintains the
list in proposal order, idempotently, under the store's concurrency contract. Verified
after a fresh run: `episode.shot_ids -> ['a-dvr', 'b-lowlight']`.

### 2. FR-09 and approval have no API or CLI operation — not fixed

`compose_timeline` and `assemble_episode` exist in `orchestrator/service/timeline.py`, and
`approve_shot` in `orchestrator/service/approval.py`. **Neither is exposed as an
operation.** `OPERATIONS` has no row for either.

The Phase 1 Exit clause requires the pipeline be *"reached through the API or CLI"*, so an
episode cut is not reachable by the route the gate names. This spike drives the services
directly, which is why it is a spike and not an acceptance run.

### 3. The shot lifecycle is specified, enforced, and never driven — not fixed

This is the real blocker.

```
JobError: cannot move draft -> approved: only planned is reachable from here
```

`orchestrator/domain/state.py` defines and enforces a complete lifecycle:

```
draft -> planned -> blocking -> building -> review -> approved -> stale
```

with `blocking -> building` gated on FR-29 framing approval. It is tested and it works.

**Nothing advances a shot through it.** Searching the whole of `orchestrator/` for writes
of `planned`, `blocking`, `building` or `review` returns nothing. Only two status writes
exist anywhere:

- `service/dependencies.py` sets `STALE` when a dependency invalidates a shot;
- `service/approval.py` sets `APPROVED`.

So a shot is created `draft`, the only edge out of `draft` is `planned`, and nothing takes
it. `approve_shot` can never succeed, because `approved` is reachable only from `review`.
**Every shot in every project is permanently `draft`**, and FR-09 — which assembles
approved shots, per its own specification — is therefore unreachable.

Running `build-scene`, `render-master` and `degrade-media` to completion leaves the shot
`draft`. The pipeline produces artifacts and never advances the record that describes
them.

## Why no test caught any of this

The same shape as the `scene_spec` key, the FFmpeg pin, the handheld round-trip and the
empty `shot_ids`: **each side is internally consistent and well tested, and nothing
exercises the seam.** `state.py` is tested on transitions it is handed. `approval.py` is
tested with a shot fixture already in `review`. The job pipeline is tested on the
artifacts it writes. No test walks a shot from `propose-shots` to a cut.

Five defects of this shape in one day, every one found by running the pipeline rather than
by a passing suite, is a property of the codebase rather than a run of bad luck.

## What this leaves open for the creator

Driving the lifecycle needs a decision, because FR-29's framing gate is a
creator-in-the-loop step: `blocking -> building` requires `framing_status == approved`.
An episode that assembles itself end to end either needs that loop implemented, or an
explicit route past it for shots that carry no framing panel. That is a scope question,
not an implementation detail.

## Resolved — the cut assembles

All three blockers are closed, and a seventh defect surfaced and was fixed on the way.

```
approved a-dvr (content v1) via rv-a-dvr
approved b-lowlight (content v1) via rv-b-lowlight
timeline entries: 2
  {'type': 'shot', 'ref': 'a-dvr',      'start_sec': 0.0, 'duration_sec': 4.5}
  {'type': 'shot', 'ref': 'b-lowlight', 'start_sec': 4.5, 'duration_sec': 5.0}
assembled in 4.11s
receipt -> episodes/ep1/outputs/cuts/31fd6d57c8304e2180b8b1777ed2a7a6.mp4
sha256     4975e48cbf84fd7bd51e7334f2cba619b6a84c1dbe808d811573cbd1a363870b
```

Probed: **h264, 640x480, SAR 1:1, DAR 4:3, 12 fps, 114 frames, 9.500 s, 150 KB.**

A shot now travels `propose-shots -> build-scene -> render-master -> degrade-media ->
review -> approved -> cut` in one run. Six separate defects had each blocked that path.

**The per-shot look survives conforming.** `cut-lowlight-frame.png` is the 352x288 shot
inside the 640x480 cut: correctly proportioned rather than stretched, and visibly softer
and coarser than `cut-dvr-frame.png` beside it. That difference is the feature.

**The timeline reports delivered durations, not intent.** `a-dvr` occupies 4.5 s, not its
declared 5.0 s, because the DVR profile drops six frames at 12 fps. The seventh defect:
`compose_timeline` had been building entries from `Shot.duration_sec`, so the Timeline
described a cut that could not be built. Degraded receipts now record `frame_count` and
`output_fps` as an exact integer pair, and the Timeline derives duration from them.

## Still open

- **No API or CLI operation** for `approve_shot`, framing approval, or `assemble_episode`.
  This spike drives the services directly, so the cut is not yet reachable by the route
  the Phase 1 exit clause names.
- **Nothing creates `framing_panel_ref` or sets framing to `pending`**, so the FR-29 gate
  can be passed but not entered.
- **`review -> rejected` has no service writer.** A shot can be approved but never
  rejected.
- The degradation adapter checks produced frame count against the predicted schedule but
  **never compares probed output FPS or duration against the profile**, so receipt timing
  assumes those rates match rather than verifying it.
