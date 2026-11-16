# Contract — export-time delivery render, and moving the overlay off the recording

Drafted 2026-11-15. **Not yet implemented.** Written before code, per CLAUDE.md, so that
the implementer and a blind test author can work from the same statement and disagree
usefully.

## 1. Why this exists

Two things forced this, one administrative and one measured.

**FR-13 is P0 and absent.** `routes.py:226` declares
`Operation("export", "POST", "/projects/{project_id}/export", ..., "export adapter", ...)`,
but there is no export adapter, no service function and no dispatch entry. Calling it
answers `{"error": "missing_adapter", "details": {"adapter": "export adapter"}}`. The
route table has been advertising a capability that does not exist.

**H3 destroys the burned-in overlay.** Measured on 2026-11-15 across three generations
from one source, sampling the burned-in clock:

| Artifact | Resolution | Clock as sampled |
|---|---|---|
| `post/.../degraded.mp4` (the source the post stage produces) | 352x288 | `:00 :03 :06 :09 :12 :15 :18 :21` — exact, monotonic, crisp |
| `run165a/candidate.mp4` (the **accepted Phase 1 shot**) | 352x288 | **absent** — no clock, no camera ID, no REC |
| `fe5a80dd.../candidate.mp4` | 512x384 | `:00 :07 :07 :06 :05 :05 :05 :06` — jumps forward, then counts backwards |
| `117214fe.../candidate.mp4` | 640x480 | **absent** |

The post stage renders the overlay correctly and deterministically. H3 then either erases
it or hallucinates the digits, and **which of those happens is not stable across runs**.
The accepted Phase 1 shot therefore ships with no camera ID and no timestamp at all --
the defining visual signature of the genre, missing from the accepted output.

The cause is ordering, not tuning. The overlay is burned in by the degrade chain, so it is
image content by the time H3 ingests it, and H3 regenerates image content.

**The creator's delivery decision (2026-11-15):** accept candidates at whatever resolution
they were generated at, upscale to a standard at export, and choose the delivery
resolution at runtime. Stored outputs stay native; no upscaled copies are persisted.

## 2. What is already settled

These need no decision; they follow from invariants already in the repository.

- **S1. `on_playback` is being completed, not invented.** `PostStage.composite_stage`
  (`entities.py:539`) is commented `in_recording | on_playback` and
  `PostProfileOverlay.placement` (`entities.py:564`) is
  `Literal["in_recording", "on_playback"]`. The adapter mirrors both
  (`profile.py:47`, `profile.py:63`). Four guards defer it:
  `profile.py:221` ("on_playback overlays are not implemented in this pass"),
  `profile.py:309`, `profile.py:479`, and `compose.py:436`. This contract implements the
  deferred half; it does not add a concept.
- **S2. FR-21's intent is preserved, not violated.** PRD:186 says "Overlay burn-in is the
  **final** post stage and is computed from the post-decimation frame timebase". That was
  written when the degrade chain *was* the last step before delivery. H3 now runs after
  it, so "final post stage" no longer means "last before delivery". Moving the overlay to
  export is what keeps it final. **This still needs a recorded PRD amendment** -- the same
  shape as `H3_PROMOTION_CONTRACT` D4, where the PRD was likewise behind the plan.
- **S3. The clock is data, not pixels.** FR-21 computes it from `clock_epoch` plus the
  post-decimation frame timebase, and PRD:186 requires it to "persist logically, matching
  frame skips and discontinuities". Export must recompute it from those values. It must
  never upscale a rendered overlay, and never carry one through a generative stage.
- **S4. Export is derived, never a `ShotOutput`.** The delivery render is an artifact of a
  request, not a production record. It does not append to the shot's output list, does not
  take an `expected_version`, and cannot be re-ingested by H3 (the same hazard
  `H3_PROMOTION_CONTRACT` R5 defends against).
- **S5. All writes pass through `ProjectRoot`** (FR-25). Export writes inside the project
  root only, and reads its inputs through the same containment helper.
- **S6. Export does not regenerate.** It composites, scales and encodes. It must never
  invoke Blender, ComfyUI or H3. An export that cannot find an approved output fails;
  it does not produce one.

## 3. The decisions the creator must make

Each is a genuine fork. None should be invented in code.

### D1. What is the delivery resolution, and who owns it?

The creator asked to "choose the desired resolution during runtime", so it is a parameter
of the export request rather than a stored field. The open part is the **default** and
whether an episode may pin one.

The overlay reads `2005-11-14`, which makes 640x480 or D1 **704x576** period-honest;
1280x960 is the master's own resolution and is defensible as an archival tier; 1920x1080
would read as wrong for the format unless that tension is wanted deliberately.

**Recommendation:** required parameter, no silent default, with 640x480 documented as the
house standard. A silent default is how a 1080p export of 352x288 footage happens by
accident.

### D2. The REC/battery indicator cannot currently move.

`PostProfileOverlayIndicator.placement` is `Literal["in_recording"]` (`entities.py:549`,
`profile.py:47`) -- narrower than the clock's, deliberately. A real DVR burns its REC
graphic into the recording while the timestamp is drawn by playback software, so the
narrower type encodes something true.

But H3 destroys the indicator exactly as it destroys the clock: it is absent from both the
accepted shot and the 640x480 run.

- **(a) Widen it to `Literal["in_recording", "on_playback"]`.** Both survive. Costs the
  authenticity argument the narrow type was making.
- **(b) Leave it burned in.** Honest to the hardware, and it will keep being mangled.
- **(c) Widen it, and default it to `in_recording`** so existing profiles are unchanged and
  only profiles that opt in move.

**Recommendation: (c).** It keeps the hardware story available and makes survival a choice.

### D3. What upscale filter?

Not a neutral choice for this genre. `lanczos` is sharpest and looks modern. `bicubic` is
neutral. **`neighbor` (point) upscaling reproduces the blocky enlargement of real
low-resolution video** and may be the most authentic for CCTV, especially on a 352x288
source going to 640x480.

**Recommendation:** parameter with `lanczos` default, because "looks wrong deliberately"
should be an explicit choice. Worth an A/B before fixing the default.

### D4. Does export re-apply degradation?

Measured today: at 640x480 H3's output is noticeably **cleaner** than at 352x288 -- bright,
evenly lit, closer to an architectural render than analog footage. H3 partially undoes the
degrade it was fed.

- **(a) Export composites the overlay only.** Smallest change. Accepts that higher-
  resolution output looks less like CCTV.
- **(b) Export re-applies a final grade/artifact pass after H3.** Restores the look, and
  makes "style degrade in, final grade out" the actual architecture.

**Recommendation: (b) eventually, (a) first.** (b) needs its own decision about which
stages are safe to run twice -- `decimation_pattern` and `frame_drop_schedule` in
particular must not be applied a second time, or the clock's timebase stops matching the
frames. Do not improvise that during implementation.

### D4b. The clock's timebase does not survive H3, and this is not cosmetic.

The degrade chain renders the clock as `%{pts\:gmtime\:<clock_epoch>}` after a
schedule-clock `setpts` stage (`profile.py:466`, `profile.py:481`), so the digits come
from presentation timestamps on the **post-decimation** timebase. That is exactly what
FR-21 requires.

H3 does not preserve that timebase. Measured: the degraded source is **26.0 s at 10 fps
(260 frames)**; the H3 candidate generated from it is **8.0 s at 24 fps (192 frames)**,
taken from `start_seconds` into the source (`H3GenerateBody.start_seconds`, default 0).

So an export-time clock applied naively to the H3 output would be monotonic but **wrong**:
it would start at `clock_epoch` regardless of where in the recording the clip was taken,
and run at the wrong rate relative to the original recording.

Export must therefore compute the clock as `clock_epoch + start_seconds + pts`, which
means **the delivery render needs the source offset**, and that value lives on the H3
request, not on the `PostProfile`. Either `H3Candidate` must surface it (it already holds
the inputs needed -- `H3_PROMOTION_CONTRACT` R3), or the promoted output must carry it.

**DECIDED 2026-11-15 (creator).** `start_seconds` was added to `H3Candidate`, defaulted to
`0.0` so candidates recorded before the field existed remain readable, and populated from
`H3Request.start_seconds` at the construction site in `service/minimax_h3.py`. Both
`GenerationRun` provenance sites now record it too, where it had been missing from
`settings` alongside duration/width/height/fps/steps. All four candidates already on disk
load cleanly at `0.0`, which is correct: every generation so far used the default start.

The delivery render still takes `clock_offset_seconds` explicitly rather than reaching
into the candidate itself, so the adapter stays usable for a source that is not an H3
candidate. The service layer is what should derive it.

### D5. Which outputs are exportable?

`H3_PROMOTION_CONTRACT` R1 requires an approving `Review` before a candidate becomes a
production output, and the project already records a `generated` kind. Export should
consume **promoted outputs**, not candidates, or approval stops meaning anything.

**DECIDED 2026-11-15 (creator).** "Accept all" meant accepting shots of *varying native
resolution* and upscaling them to one delivery standard -- not accumulating several
exportable takes per shot. A shot has **one finalised clip**; the others are history.

So this is not a selection problem. Export resolves the shot's finalised output and
refuses if there is none; more than one finalised output is a state error, not an
ambiguity for export to resolve.

Once a shot is finalised the superseded candidates are discardable, which is retention
rather than export: `RetentionSettings.unapproved_versions_per_shot` (default 3) and
`purge_previews` already exist for exactly this, and the media is not small -- the three
candidates generated on 2026-11-15 alone are 0.6-1.2 MB each, against a
`disk_ceiling_gb` of 40. **The purge trigger on finalisation is not yet implemented.**

## 4. Proposed shape, once D1-D5 are answered

```
render_delivery(store, root, *, project_id, episode_id, shot_ids, delivery) -> DeliveryResult
```

In order, refusing rather than guessing at each step:

1. Resolve each shot's exportable output (D5). Refuse if a shot has none, naming the shot.
2. Refuse if a shot has more than one candidate output and D5's selector does not
   disambiguate. Never silently take the newest.
3. Verify each source artifact's `content_sha256` before reading it. The store already
   re-verifies on read (`json_store.py:910`); export must not be the one path that trusts
   bytes.
4. Scale to the delivery resolution (D1) with the chosen filter (D3). Preserve aspect;
   pillarbox rather than stretch if the source aspect differs.
5. Recompute the clock from `clock_epoch` and the post-decimation timebase (S3), and
   composite the `on_playback` overlay at **delivery** resolution, so the text is crisp
   rather than upscaled.
6. Encode H.264 + AAC (FR-13).
7. Write through `ProjectRoot` (S5), returning the relative path and a digest.

## 5. What a blind test author should attack

- A shot whose native resolution is **larger** than the delivery resolution (downscale).
- **Mixed native resolutions in one episode** -- 384x288 and 640x480 cut together. The
  creator flagged the visible-quality-jump risk; a test should assert the export at least
  produces a single consistent resolution and does not letterbox one shot and not another.
- The clock across **frame drops and freezes**: `artifact_schedule` drop/freeze events
  exist (`profile.py:294`), and PRD:186 requires the overlay to match skips. A clock that
  is monotonic on continuous footage but wrong across a drop is the likely bug.
- A clock crossing a minute and an hour boundary, and `clock_epoch` at exactly 0.
- An `on_playback` clock on a profile whose indicator is still `in_recording` (D2(c)),
  asserting the two end up on opposite sides.
- Export with no promoted outputs; with one shot missing; with a tampered `content_sha256`.
- Containment: a `shot_id` or delivery path containing `..`, a drive letter, or a reserved
  Windows name (`profile.py` already validates overlay position syntax; export paths need
  the same treatment via FR-25).
- That export appends **no** `ShotOutput` and leaves `jobs/budgets.json` untouched.
- That a delivery render cannot be passed to `compose_h3_request` (S4).

## 6. Known gaps in this contract

- **FR-13's source package is not designed here.** FR-13 also requires "a package
  including `.blend` files, scripts, shot clips, audio stems, generated assets, all
  manifests, the post-profile definitions, and asset provenance", which must "reopen
  without unavailable cloud media URLs and without the SQLite index" (AT-09). This
  contract covers only the delivery render -- FR-13's "Exported final plays in ordinary
  players". The package half remains unimplemented and unspecified.
- **Export invalidation is not specified.** If a shot is re-approved after an export, that
  export is stale. This is the same gap `H3_PROMOTION_CONTRACT` §6 records for promoted
  H3 outputs, and it should be solved once for both rather than twice differently.
- **Audio is passed through, not mixed.** Multi-shot delivery needs a concatenation and
  level policy that does not exist yet; FR-13's "audio stems" implies one.
