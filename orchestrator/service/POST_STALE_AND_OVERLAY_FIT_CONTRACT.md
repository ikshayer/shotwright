# Post-only staleness and overlay fit contract (step 3e, 2026-11-22)

Two creator decisions of 2026-11-22, following step 3a, which made every post-only
CameraProfile field take effect:

1. "Yes, they can be marked as stale." A post-only CameraProfile revision must mark shots
   whose degraded output it changes. Until now, `revision_impact`
   (`orchestrator/domain/invalidation.py`) returned nothing for a post-only diff. An
   approved shot's degraded output could therefore silently disagree with its current
   camera.
2. "They should be refused and fixed. Overlay text should not overflow from frame." The
   real 3a run showed a 12-hour clock at font size 16 running past the right edge of a
   352-px frame. Nothing checked for it.

PRD anchors: §4.4 ("post-only: change without re-rendering"); FR-07 ("changing a post-only
field changes final encoding/look without rebuilding Blender geometry"); FR-11 ("scoped
rebuild and correct upstream invalidation"); §8 upstream invalidation ("a post-only
profile change does not invalidate geometry").

## Part A — post-only staleness

### A1 State

Add `ShotStatus.POST_STALE = "post_stale"`. It means: this version's render and master are
still valid; its degraded output is not current.

- New edges: `APPROVED → POST_STALE`, `REVIEW → POST_STALE`, `POST_STALE → BUILDING`
  (re-degrade), and `POST_STALE → STALE` (a later rebuild-triggering change still wins).
- `BUILDING → REVIEW` and `BUILDING → FAILED` already exist and are reused.
- Everything that today treats `STALE` as blocking must treat `POST_STALE` the same way:
  cut approval (`check_cut_approval`), export, episode gates, the
  `{STALE, DRAFT}` sets in `invalidation.py` and `state.py`, and any other place found by
  grepping `ShotStatus.STALE`. Report the full list.

### A2 Which shots

`revise_camera_profile` with a diff that is post-only (`requires_rebuild` false) and
non-empty marks a Shot when all of these hold:

- it uses this CameraProfile, by the same selector rebuild invalidation uses;
- it is at its current version, in `REVIEW` or `APPROVED`;
- it has a DEGRADED output for that version;
- the derived spec of its latest DEGRADED output differs from the one derived now. The
  "now" spec comes from the revised camera and the shot's current PostProfile, through the
  same code path the DEGRADE job uses (`compose_degrade_request` +
  `postprofile_spec_from_camera`). The comparison is by `postprofile_spec_sha256`, which
  must not depend on run-specific values (confirm, and report).

A receipt with no `postprofile_spec_sha256` (pre-3a) counts as differing. A post-only
change that leaves the derived spec identical marks nothing.

Shots in other states are not marked. Their next degrade uses the new profile.

Marking goes through the existing cascade journal: plan, gate revocation, commit, staleness,
completion. The plan records each affected row with `"scope": "post"`, so
`resume_invalidations` finishes a partial cascade. Marking clears `approval_review_id`.
Episode cut gates are revoked for affected episodes, as rebuild cascades already do.
`revise_camera_profile` returns the marked shot ids, as it does for rebuild invalidations.

A diff with both post-only and rebuild-triggering changes keeps today's rebuild behaviour
(`STALE`). Rebuild wins.

### A3 Re-degrade

`degrade_media` admits a shot in `POST_STALE` as well as `BUILDING`. The job moves
`POST_STALE → BUILDING` when it starts, under the same lifecycle discipline the runner
already uses. Success moves it `BUILDING → REVIEW`; failure moves it to `FAILED`. No
re-render: the existing master of the same version is reused. Update `degrade_media`'s
description.

### A4 No reuse of an old approval

The shot version does not change, so a Review recorded before the re-degrade must not
approve the re-degraded output.

- `record_review` for a `shot` target sets `Review.created_revision` (the field exists and
  is unused) to the Shot's lifecycle mutation token at recording time.
- `approve_shot` refuses a Review whose `created_revision` is lower than the token at which
  the Shot most recently entered `REVIEW`, if that version has ever been `POST_STALE`.
  Determine this from the store's lifecycle history. When `approve_shot` records its own
  Review, the Review gets the current token.
- A Review with `created_revision` None (legacy) is accepted only for a version that has
  never been `POST_STALE`, so existing projects keep working.

## Part B — overlay text must fit

### B1 Measure

Add one function that measures the pixel box of a drawtext string with a given font file
and size, using FreeType through Pillow (already a dependency). Calibrate it against
FFmpeg 8.1.1 drawtext: render strings with `color=black` and white text, measure the lit
bounding box from the decoded frame, and compare. Do this for DejaVu Sans Mono and one
proportional font. Report the maximum disagreement. The check uses a safety margin at
least that large, and at least 1 px.

### B2 What must fit

Check every drawtext overlay that is composited `in_recording`: the clock line
(`camera_id` + two spaces + formatted clock) and the battery/REC indicator. Each must lie
fully inside the storage frame (`storage_resolution`): `x ≥ 0`, `y ≥ 0`, `x + width ≤ w`,
`y + height ≤ h`. The box, if drawn, must fit too.

- Positions are evaluated with exactly the grammar the profile accepts for `x`/`y`
  (integers, and string expressions such as `h-24`). Read `_safe_position` and support
  exactly that grammar, with `w`, `h`, `text_w` and `text_h` bound to the frame and the
  measured text.
- The clock text varies with time. Check the widest string the overlay can show for the
  shot: every second the clock can display, from `clock_epoch` across the master's
  duration plus any clock offsets in the artifact schedule. If that range is not
  derivable at the check site, use a time-independent upper bound: per strftime token,
  the widest rendering over all of its values (all months, weekdays, AM/PM, and every
  digit for numeric fields), plus the margin.

### B3 Where it is refused

- **At degrade composition, before FFmpeg runs.** `INVALID_MANIFEST`, naming:
  - the overlay element;
  - the measured width and height;
  - the frame size and position;
  - the largest integer font size that fits at that position;
  - and saying to lower `overlay_model.fontsize`, shorten the text or format, or move it.
- **At `create_camera_profile` / `revise_camera_profile`,** when the fit can be decided:
  the project's overlay font exists, and the shots using the camera name PostProfiles
  with overlays. Same message. When it cannot be decided there, say so in the response.
  Do not guess.
- `on_playback` overlays are step 3b's export compositor. Out of scope here, but note
  where 3b must call the same function.

### B4 Fixed defaults

The seeded profiles (dvr-2005, low-light-bw, consumer-2002) must fit with their own
overlay settings and DejaVu Sans Mono, the 3b default font. If one does not, report it
rather than change its values.

## Amendments from review and the real run (2026-11-22/23)

### Overlay measurement

- **Measured with FFmpeg, not Pillow.** Pillow disagreed with FFmpeg drawtext by up to
  19 px. drawtext lays glyphs out on unhinted fractional advances, and even Pillow at 64×
  scale was 8 px off. The check now renders each candidate with the configured FFmpeg
  (`settings.binaries.ffmpeg_exe`), using the same drawtext serialization the degrade
  uses, and measures the lit box: 0 px disagreement over 24 samples, margin 0.
- **Every candidate is checked.** Each distinct displayable string is checked at its own
  box and evaluated position: "A  Mar" at 40 px is wider but shorter than "A  Apr".
- **Font-size advice.** The largest fitting size comes from a binary search when neither
  position references `text_w`/`text_h`. Otherwise every size from 6 to 96 is measured.
  The search also falls back to measuring every size when it answers "none", or when
  any of the next three sizes fits, because hinting jitters boxes at tiny sizes. The
  refusal itself is always decided by the exact measurement at the requested size.
  Real case: 3.6 s, down from 17 s.
- **Unreadable font.** A font that cannot be loaded or rendered refuses at degrade. At
  revision, the fit is reported as `"undecidable"` in the response's `overlay_fit`.

### Staleness

- **Obsolete completion.** A DEGRADE that completes against a spec made obsolete while it
  was queued or running records its output and moves `BUILDING → POST_STALE` in one
  persisted transition (new edge). `approve_shot` re-checks the latest DEGRADED receipt
  against the spec derived now.
- **Shared derivation.** One pure derivation, with no FFmpeg and no fit check, serves the
  A2 comparison, the completion re-check and approval. A Shot's camera is resolved the
  same way DEGRADE resolves it (`camera_profile`, not `camera_id`).
- **Pending cascades.** `degrade_media` is refused while a cascade is pending.
- **Post rows and fences.** Post-scope cascade rows are excluded from the permanent
  rebuild fences, in approval and in script derivation.

### Cut binding

- **Bound inputs.** Each cut records an inputs sidecar binding every Shot to its DEGRADED
  output id and hashes. The receipt records the sidecar's path and SHA-256. Approval and
  export verify the bytes against the receipt, and every Shot in the cut must be REVIEW or
  APPROVED with a current output. Export packages every retained cut's sidecar.
- **Episode-wide rule.** Cut approval still refuses while any current Shot of the Episode
  is STALE, DRAFT or POST_STALE, whether or not it is in the cut.
- **Legacy cuts and receipts.** A cut assembled before this change has no binding. It
  cannot be approved or exported, even if it was already approved, and must be
  reassembled with `assemble_cut`. A DEGRADED receipt without a spec hash (pre-3a) cannot
  be shot-approved or enter a cut until `degrade_media` runs again. Re-degrading costs CPU
  minutes and no re-render.

### Real run (scratch copy of the Phase 1 project, shot s1)

- **Overflow refused.** The 12-hour clock at font size 16 on 352 px was refused, naming
  size 15. That project's font is Consolas: 8.8 px per character, 352 px measured.
- **Re-degrade.** Revising to size 15 marked s1 `post_stale`. `degrade_media` took it
  `post_stale → building → review` in 5 minutes from the existing master, with the full
  clock inside the frame.
- **Not run.** No approval was attempted, because approvals run only on the creator's
  instruction.

## Non-goals

- Step 3b (default font, on-playback compositing).
- Step 3c (ntscrs).
- Auto-shrinking text: refusal with a precise fix is the decision.
- Changing rebuild-triggering invalidation.

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max):
- `orchestrator/domain/base.py` (ShotStatus);
- `orchestrator/domain/state.py`;
- `orchestrator/domain/invalidation.py`;
- `orchestrator/service/dependencies.py`;
- `orchestrator/service/approval.py`;
- `orchestrator/service/runner.py` (DEGRADE admission and transitions only);
- `orchestrator/service/compose.py`;
- `orchestrator/api/app.py` (admission only, if the status gate lives there);
- `orchestrator/api/routes.py` (descriptions);
- `orchestrator/adapters/postprofile/profile.py`;
- a new `orchestrator/adapters/postprofile/overlay_fit.py` for B1–B2;
- existing tests only where an assertion pins behaviour this contract changes (name each).

**Test author** (separate agent): only a new `tests/service/test_post_stale_and_overlay_fit_contract.py`.

**Do not touch:**
- `spikes/**`;
- the export adapter;
- `tests/adapters/postprofile/test_camera_fields_contract.py`.

## Done when

- Every requirement has a test.
- The full suite passes.
- Ruff reports nothing beyond `main`'s findings.
- On the `_work/cf_real` scratch project (shot s1 is in `review`):
  - a post-only revision marks s1 `post_stale`;
  - `degrade_media` re-degrades it from the existing master back to `review`;
  - an old Review cannot approve it;
  - the 12-hour clock format at font size 16 on 352 px is refused with the fitting size named.

  Approval in the real run uses the CLI only on explicit instruction.
