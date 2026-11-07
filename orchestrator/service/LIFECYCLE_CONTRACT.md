# Contract — driving the shot lifecycle, and FR-29's framing gate

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

`orchestrator/domain/state.py` defines and enforces a complete shot lifecycle:

```
draft -> planned -> blocking -> building -> review -> approved -> stale
```

**Nothing drives it.** Searching `orchestrator/` for writes of `planned`, `blocking`,
`building` or `review` returns nothing; the only status writes anywhere are `STALE` in
`service/dependencies.py` and `APPROVED` in `service/approval.py`. So every shot in every
project is permanently `draft`, `approve_shot` can never succeed because `approved` is
reachable only from `review`, and **FR-09 — which assembles approved shots — is
unreachable.** Running `build-scene`, `render-master` and `degrade-media` to completion
leaves the shot `draft`: the pipeline produces artifacts and never advances the record
describing them.

Measured in `spikes/13_episode_cut/FINDINGS.md`.

## Creator decisions of 2026-11-06, which this contract implements

1. **The full FR-29 framing loop**, not an auto-advance past it. A shot with a framing
   panel waits in `blocking` until the creator approves its framing.
2. **Framing is optional.** A shot carrying no panel (`framing_status == none`) passes the
   gate rather than stalling. Only `pending` blocks.

## Decision 1 — the gate admits `none`, and only `pending` blocks

`check_transition` currently refuses `blocking -> building` unless
`framing_status is APPROVED`. That makes a shot with no panel permanently stuck, which is
the defect the creator's second decision resolves.

**Binding:** `blocking -> building` is permitted when `framing_status` is `approved`
**or** `none`, and refused when it is `pending`, with a message naming the pending framing
and what resolves it.

There is an existing test asserting the current guard. It is a **deliberate behaviour
change**, so update that test with its subject preserved — it should still prove that a
*pending* framing blocks the build — and say in your report what it asserted before.

## Decision 2 — the pipeline advances the lifecycle, the creator approves the result

Mechanical transitions belong to the jobs that do the work. Human judgement stays human.

- `propose_shots` leaves a shot `draft`, as now.
- **`draft -> planned`** when the shot has what it needs to be built — at minimum a
  resolvable `CameraProfile` and a duration. State your precondition.
- **`planned -> blocking`** as the shot enters the framing gate.
- **`blocking -> building`** when the gate admits it, per Decision 1.
- **`building -> review`** when a delivered artifact exists for the shot's current content
  version — the same condition `compose_timeline` already uses.
- **`building -> failed`** when the job fails; `failed -> building` on retry. Both edges
  already exist.
- **`review -> approved`** stays `approve_shot`, a creator act, unchanged.

**Every transition goes through `check_transition`.** Do not set `status` directly
anywhere; the state machine is the authority and it is already tested.

**Respect the store's concurrency contract** on every write: versioned entities update
through the proper path with the correct `expected_version` token.

## Decision 3 — FR-29's compare loop, as much of it as is headless

FR-29 specifies five steps. **Steps 3 and 4 — the side-by-side inspector and plain-language
nudges — are UI and natural-language parsing, and are OUT OF SCOPE**: FR-10's review
surface is unbuilt by creator decision, and NL revision parsing is Phase 3. Implement the
rest:

1. **The placement sanity check, before spending a render.** FR-29 requires "nearest-surface
   distance along the view vector, plus a non-degenerate visible-volume check". This is not
   defensive boilerplate: spike 02 measured a computed bounding-box placement that **put
   the lens inside a prop and rendered a black frame** — cheap to detect, expensive to find
   by eye. A failing check must refuse the render and name what it found.
2. **A grey-box framing preview** — no materials, flat shading, figure stand-ins at their
   staged positions, preview resolution. §8.3 requires seconds, not minutes; the existing
   Workbench preview path already renders in about 5.8 s and is the obvious vehicle.
3. **Approving framing** sets `framing_status` to `approved` and **freezes the resolved
   camera values as creator-authored** — FR-29 says they are frozen on acceptance, and
   `Shot.creator_authored` is the existing field recording which fields an agent may not
   overwrite.

## Out of scope

The FR-10 review surface; natural-language nudges; `Location`; the rubric scorer; new API
operations (another agent is adding those); `failing-dvr`.

## Test rules

- **No GPU, no Blender, no ComfyUI, no network.** Assert on records and on transitions;
  fake the preview render.
- The regression that matters: **a shot proposed through `propose_shots` can reach
  `approved` and be assembled by `compose_timeline`** — the whole seam, not either end.
  That is the test whose absence let five defects through today.
- Required cases: a shot with no panel passes the gate; a shot with `pending` framing is
  refused, naming what resolves it; an approved framing passes; the placement check
  refuses a camera inside geometry and names it; approving framing freezes the camera
  values into `creator_authored`; every transition is rejected if taken out of order; and
  a failed job moves `building -> failed` and back on retry.
- Every assertion carries a message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline: **1480 passed, 34 skipped**, ruff clean.
