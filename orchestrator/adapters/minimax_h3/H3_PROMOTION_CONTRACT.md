# Contract — promoting an accepted H3 candidate to a production `ShotOutput`

Drafted 2026-11-13. **Not yet implemented.** Written before code, per CLAUDE.md, so that
the implementer and a blind test author can work from the same statement and disagree
usefully.

## 1. Why this exists

The creator's intended production flow is:

> FLUX -> TRELLIS + Blender (generate the scene, establish the vision, get consistent
> outputs) -> **pass the clip to H3 to improve the video and add audio**.

So H3 is the **final production stage**, and the Blender master plus the degraded clip are
*intermediates*. The entity model does not express this. `H3Candidate` is declared
*"Immutable generated audiovisual candidate; never a production `ShotOutput`"*
(`domain/entities.py`), there is no code path converting one, and `json_store.py:487`
refuses any write with `expected_version != 0`.

**That is a modelling decision that is behind the plan, not a boundary to defend.** It was
a defensible conservative choice while H3 was unproven. H3 is no longer unproven: the
creator accepted a real generation on 2026-11-12 (384x288, 192 frames, 24 fps, 8.000 s,
H.264 + AAC stereo 32 kHz).

This contract specifies the promotion. It deliberately does **not** relax the immutability
of `H3Candidate`: a candidate is evidence, and promotion must *create a new record*
referencing it, never mutate it.

## 2. What is already settled

These need no decision; they follow from invariants already in the repository.

- **R1. Promotion requires an approving `Review`.** `Review` is the only writer of approval
  state (`entities.py` module docstring, PRD 10.1), `Shot.status` is never set directly by
  an agent pass, and transitions go through `state.py`. Promotion is therefore **not**
  automatic on a successful generation. Note the accepted 2026-11-12 run recorded
  `generated_candidate_unapproved` and its own notes state "no production approval record
  was created" -- so even creator enthusiasm in chat is not a `Review`.
- **R2. The promoted record is an append, not an edit.** `store.append_shot_output(...)`
  with an explicit `expected_version`. The candidate is untouched.
- **R3. The promoted output must carry the full dependency chain.** `H3Candidate` already
  holds every input digest needed: `source_output_id`, `source_sha256`, `prompt_sha256`,
  `seed`, plus `av_sha256`/`wav_sha256`/`report_sha256`. Promotion must preserve the link
  so invalidation can reach it when the source changes.
- **R4. Promotion must verify before it records.** Re-hash `av_path` and compare to
  `av_sha256` at promotion time. The store already re-verifies artifacts on read
  (`_verify_shot_output_artifact`, `json_store.py:910` -- confirmed firing in A3.9), so a
  promoted record whose bytes drifted would poison every later read.
- **R5. A promoted H3 output must not be re-ingestible by H3.** `compose_h3_request`
  accepts `MASTER` or `DEGRADED` as a source. If a promoted H3 output were visible to that
  filter, H3 output could be fed back into H3, compounding generation artifacts silently
  across versions. Whatever kind it gets must be **excluded** from that filter.

## 3. The decisions the creator must make

Each is a genuine fork. None should be invented in code.

### D1. What `ShotOutputKind` does a promoted H3 output get?

- **(a) A new kind** (e.g. `GENERATED`). Honest: this artifact's provenance class is
  genuinely different from a Blender master or an FFmpeg degrade. Satisfies R5 for free.
  Cost: every site that switches on `kind` must be revisited.
- **(b) Reuse `DEGRADED`.** Cheapest, and violates R5 -- it would make H3 output a legal
  H3 input. Also untrue: `DEGRADED` means the versioned `PostProfile` chain's output.
- **(c) Reuse `MASTER`.** Untrue in a worse way: `MASTER` means the Blender master
  intermediate at the 8.3 spec.

**Recommendation: (a).** The whole value of `kind` is that it tells a reader what produced
the bytes.

### D2. May an `experimental` candidate be promoted?

This one is sharper than it looks. `_BASELINE = (8.0, 384, 288, 24, 20)` and
`core.py:145` raises *"nonbaseline H3 settings require experimental=True"*. The creator's
production target is **15 s at 10 fps, 352x288** -- which is non-baseline, so **every
production shot would be flagged experimental**.

- **(a) Widen the baseline** so the CCTV-native production shape is not an experiment.
  The current baseline is a *feasibility-test* shape, not a production one; A3.9 confirmed
  15.0 s / 10 fps / 352x288 composes to exactly 150 frames, and 150 frames is **fewer**
  than the 192 already generated successfully. If this is the production target, it should
  *be* the baseline.
- **(b) Allow promotion of experimental candidates**, carrying the flag onto the
  `ShotOutput`. Then `experimental` stops functioning as a gate.
- **(c) Require non-experimental.** Blocks the creator's actual target indefinitely.

**Recommendation: (a)**, with `experimental` retained for genuinely off-spec probes.
`_BASELINE` is currently a single tuple; this likely wants to become a small set of
accepted production shapes rather than one.

### D3. What are `engine_id` and `engine_build` for an H3 output?

`ShotOutput` requires both (the Phase 1 master carries `BLENDER_EEVEE` / `5.2.0`). H3's
identity is not one binary but a **set**: ComfyUI 0.44.0 at source commit
`2c75dc1b379a59e0880619d029607af5169a62ef`, plus four pinned component digests (NVFP4
diffusion, NVFP4-AWQ text encoder, INT8 video VAE, audio VAE). Options: a composite build
string, or a digest-of-digests with the detail left on the candidate. **Recommendation:**
`engine_id = "MINIMAX_H3"`, `engine_build` = the ComfyUI version plus commit, with
component digests reachable through the candidate rather than stuffed into a string.

### D4. Audio crosses a PRD boundary.

H3 emits audio, and the creator's flow treats "add audio" as part of stage three. But
**PRD:610-614 puts sound in Phase 2** and excludes it from Phase 1's exit. A promoted H3
output is inherently audiovisual, so promotion imports a Phase 2 capability into the
production chain. This is the same shape as the `ShotOutput` gap: the PRD is behind the
plan. It needs a recorded PRD amendment, not a silent implementation.

Secondary: `ShotOutput` carries **one** `relative_path` and one `content_sha256`. H3
produces an MP4 (with AAC) *and* a separate WAV. **Recommendation:** the MP4 is the
promoted output; the WAV stays candidate evidence, reachable via `wav_path`/`wav_sha256`.

## 4. Proposed shape, once D1-D4 are answered

```
promote_h3_candidate(store, *, candidate_id, review_id, expected_version) -> ShotOutput
```

In order, refusing rather than guessing at each step:

1. Load the `H3Candidate`. Refuse if absent.
2. Load the `Review` named by `review_id`; refuse unless it actually approves this
   candidate. (R1)
3. Refuse if `candidate.experimental` and D2 resolved to (c).
4. Re-hash `candidate.av_path`; refuse on mismatch with `av_sha256`. (R4)
5. Refuse if a promoted output already exists for this candidate -- promotion is
   idempotent by refusal, never by silent replacement.
6. Append a `ShotOutput` of the D1 kind, carrying the D3 engine identity, at the
   candidate's `shot_version`. (R2, R3)

## 5. What a blind test author should attack

- Promotion without a `Review`, with a `Review` that approves a *different* candidate, and
  with a `Review` that rejects.
- A candidate whose `av_path` bytes changed after generation.
- Double promotion of one candidate.
- Feeding a promoted output back into `compose_h3_request` -- **must** be refused. (R5)
- An experimental candidate, under whichever D2 branch is chosen.
- That the `H3Candidate` record is byte-identical before and after promotion.
- That promotion of a candidate bound to shot version N does not attach to version N+1.

## 6. Known gap in this contract

It does not specify **invalidation**. If the source `DEGRADED` output is regenerated, the
promoted H3 output is stale, and `invalidation.py` currently reasons about `CameraProfile`
field tags rather than generated-media dependency chains. R3 keeps the information needed
to do it; the mechanism is not designed here and should not be improvised during
implementation.
