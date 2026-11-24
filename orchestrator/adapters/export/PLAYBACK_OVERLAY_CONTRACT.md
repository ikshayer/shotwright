# On-playback overlays and the default font contract (step 3b, 2026-11-23)

Step 3 of `GAP_REVIEW.md` ("stop silent drops"). Two creator decisions of 2026-11-22:

- Overlays: "Implement them". This reverses the 2026-11-05 deferral of `on_playback`.
- Font: "DejaVu Sans Mono".

## The silent drop

A PostProfile overlay stage marked `composite_stage="on_playback"` is skipped by the
degrade (`build_filter_graph`). Cut assembly (`service/timeline.py`,
`adapters/timeline/runner.py`) concatenates each Shot's DEGRADED output without drawing
it. `ExportAdapter.render` (`adapters/export/runner.py`) can draw it, but nothing calls
that. An `on_playback` clock therefore appears nowhere. Step 3a refuses `clock_format` on
`on_playback` for now ("until step 3b"). This step lifts that refusal.

Background:

- `adapters/export/CONTRACT.md` §1 explains why `on_playback` exists: MiniMax H3
  regenerates degraded video and erases burned-in text.
- S3: the clock is data, not pixels.
- D4b: the clock offset.

The H3 path itself is not in cuts today, because cuts use DEGRADED outputs only. Wiring
H3 outputs into cuts is out of scope, but nothing here may make it harder (R2).

## R1 — Where the overlay is drawn

`on_playback` overlays are composited **per Shot segment during cut assembly**, in the
segment's chain after it is conformed (scaled and SAR-set) to the episode's cut
resolution and before `concat`. They are therefore drawn at cut resolution: crisp, never
upscaled glyphs. The approved cut, and every export of it, carries the overlay the
creator reviewed.

Each Shot uses its own derived spec: its CameraProfile's `overlay_model` applied to its
PostProfile, through the same derivation the degrade uses. Elements:

- **The clock line:** `camera_id` + two spaces + formatted clock. It honours
  `clock_format`, `fontsize`, `x`, `y` and the box styling, with the same drawtext
  serialization and escaping as the degrade.
- **The REC/battery indicator:** only if its placement is `on_playback`. Today its type
  allows only `in_recording` (`adapters/export/CONTRACT.md` D2). Do not widen it; that is
  an open creator decision. Report that it stays burned in.

The overlay font is the project's overlay font: the PostProfile's `font_relative`.

## R2 — The clock must be the clock the recording shows

For every output frame of a segment, the on-playback clock must show exactly the text
the `in_recording` path would have burned into that frame for the same Shot, profile and
seed. That includes:

- the schedule clock: `clock_timebase` setpts, drop and freeze events in
  `artifact_schedule`, and `decimation_pattern`;
- the Timeline entry's in-point, if entries can trim a Shot's clip (find out, and
  handle it).

Derive the per-frame clock from data, never from pixels: the degrade's schedule and
timebase, which are recorded in the derived spec and run metadata, plus the segment's
in-point. Add an explicit `clock_offset_seconds` hook (default 0, as on
`DeliveryRequest`) so a future H3 source can state its `start_seconds`.

**The acceptance test is pixel-exact.** At equal resolution, a Shot degraded with its
clock `in_recording` and the same Shot with the clock `on_playback`, composited by cut
assembly, have identical overlay regions in every frame. Cover drops, freezes and a
minute boundary. If the conforming scale makes the frames differ elsewhere, compare only
the overlay box region; the text pixels must match exactly.

## R3 — Overlay fit at cut resolution

Run the step 3e fit check (`overlay_fit.py`, exact FFmpeg measurement, every displayable
clock string) for each `on_playback` overlay. It uses the cut resolution and the
evaluated position, at two points:

- before assembly starts, refusing with the same message format;
- when a camera profile with `on_playback` placement is created or revised, if the
  project's cut resolution is decidable; otherwise report `"undecidable"` as 3e does.

## R4 — Default project font

Ship DejaVu Sans Mono 2.37 as package data: `orchestrator/assets/fonts/DejaVuSansMono.ttf`
plus its `LICENSE` (the same bytes as `tests/fixtures/fonts/`). Pin its SHA-256 in code
and verify it when it is copied.

- **New projects.** `create_project` writes the font to `assets/fonts/overlay.ttf`
  (the existing convention) and the licence to `assets/fonts/LICENSE-DejaVu.txt`,
  through the containment helper. The seeded profiles keep `font_relative =
  "assets/fonts/overlay.ttf"`.
- **Existing projects.** These are never modified silently. Add an operation
  `install_default_overlay_font` (CLI, API and MCP, agent-callable):
  - It copies the bundled font and licence into a project.
  - It refuses if `assets/fonts/overlay.ttf` already exists, naming the existing file's
    SHA-256.
  - It never overwrites. Replacing a creator's font is the creator's call.
- **Messages.** The missing-font messages in composition and degrade name this
  operation.
- **Export.** Packages ship the licence whenever they ship the font. Check what the
  export snapshot includes, and report it.

## R5 — Descriptions

Update the descriptions of `assemble_cut`, `create_project`, `create_post_profile`,
`create_camera_profile` and `revise_camera_profile`, and of the new operation. They must
say:

- where `on_playback` overlays are drawn, and that the cut is what gets reviewed;
- that `clock_format` now works for both placements;
- the default font and how to install it into an existing project.

## Results (2026-11-23/24)

- **How it was built.** Delegated to Codex:
  1. implementer;
  2. integrator, which got the tests green;
  3. independent review, which raised five findings, all VALID and fixed: clock frame-hold
     mapping; cut sidecars now record the clock offset and playback font hashes; fit
     accounts for `clock_offset_seconds`; camera-create fit falls back to the project
     default profile; export embeds the DejaVu licence when it ships the bundled font.
- **Tests.** Full suite: 4282 passed, 113 skipped, 0 failed. Ruff: nothing beyond `main`.
- **Ruling.** `install_default_overlay_font` takes no version token. `Project` is
  unversioned, and the operation only ever creates the file exclusively and never
  overwrites, so a token would protect nothing.
- **Real run** on `_work/cf_real`:
  - Moving the clock to `on_playback` (camera v5) marked s1 `post_stale`, and
    `degrade-media` re-degraded it without a clock.
  - `assemble-cut` was correctly refused by the publication-rights gate: asset `entity`
    is licensed for internal evaluation only. The gate and the record were left alone.
  - The cut encoder was called directly into a scratch file
    (`_work/cf_real/render_step3b_encoder.py`, with no receipt). The 352×288 cut shows
    the clock at fontsize 15, at (8, h−24). At 3.2, 9.8, 16.2 and 22.8 s it reads 05:40:03,
    :09, :16 and :22 PM, matching the earlier burned-in clock exactly. The degraded
    clip has no clock.
  - The REC indicator stays burned in (D2 is open).

## Non-goals

- Widening the indicator's placement (D2, a creator decision).
- Re-applying degradation at export (D4).
- Wiring H3 outputs into cuts.
- Step 3c (ntscrs).

## File ownership

**Implementer** (Codex `gpt-6-luna`, effort max):
- `orchestrator/adapters/timeline/runner.py`;
- `orchestrator/service/timeline.py`;
- `orchestrator/adapters/postprofile/profile.py` (expose the clock and drawtext
  serialization for reuse, and lift 3a's on_playback refusal);
- `orchestrator/adapters/postprofile/overlay_fit.py`;
- `orchestrator/adapters/export/runner.py` (reuse, or retire `build_delivery_filter`'s
  duplicate drawtext; say which);
- `orchestrator/service/dependencies.py` (`create_project` seeding, fit at camera
  create/revise);
- `orchestrator/service/compose.py` (messages);
- `orchestrator/api/app.py`, `routes.py`, `cli.py`, `mcp.py`, if the new operation needs
  them;
- `orchestrator/adapters/export/package.py` / `snapshot.py`, only if the licence is not
  shipped;
- new `orchestrator/assets/fonts/`, and `pyproject.toml` package-data;
- existing tests only where an assertion pins behaviour this contract changes (name each).

**Test author** (separate agent): only a new
`tests/adapters/timeline/test_playback_overlay_contract.py`.

**Do not touch:** `spikes/**`.

## Done when

- Every requirement has a test, including the pixel-exact R2 comparison.
- The full suite passes, run alone.
- Ruff reports nothing beyond `main`'s findings.
- On the `_work/cf_real` scratch project:
  - a camera revision moves the clock to `on_playback`;
  - the shot is re-degraded (its derived spec changes, so 3e marks it `post_stale`);
  - a cut is assembled;
  - its contact sheet shows the clock drawn at cut resolution, matching the earlier
    burned-in clock's values.

  No approval is attempted.
