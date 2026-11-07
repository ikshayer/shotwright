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
