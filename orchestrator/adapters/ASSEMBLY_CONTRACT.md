# Contract — what a master is, and assembling one from frames

Written by the orchestrator. Implementation and tests come from **different agents working
in parallel from this file**, neither reading the other's output. Where they disagree, this
file decides and the disagreement is reported, never patched away.

## Why this exists

The Phase 1 acceptance run rendered 900 EXR frames and recorded a `master` `ShotOutput`
whose `relative_path` is **`frame_000001.exr`** — one still out of nine hundred. The cause
is `_artifact_path` in `orchestrator/service/runner.py`, which takes `frame_paths[0]`.
Downstream, `orchestrator/adapters/postprofile/runner.py` builds FFmpeg argv as
`-i <single file>` with no `-framerate`, `-start_number` or `%06d`, so degradation fed that
receipt would degrade one frame rather than fifteen seconds.

**Phase 1 cannot exit while this holds:** its Exit clause requires one *finished* shot.

## Decision 1 — the master is one file; the frame sequence is a retained intermediate

A `master` `ShotOutput` points at **a single assembled video file**, not at a frame and not
at a directory. Three reasons, and one hard constraint:

- `ProjectStore._verify_shot_output_artifact` requires `artifact.is_file()` and hashes its
  bytes. A directory cannot be a `relative_path` without changing the store, and the store
  is not in scope here.
- Every consumer says "master" in the singular: `degrade_media` is described as "Degrade a
  shot master", and §4.4's post-only/rebuild split assumes any delivery profile can be
  re-derived *from the master* without re-rendering, which requires the master to be one
  addressable, hashable thing.
- A receipt naming one of 900 frames is not traceability; it is a wrong answer that looks
  like an answer.

The EXR frames stay on disk as the render's intermediate, under the existing
`renders/<shot>/v<NNNN>/<run>/attempt-NNNN/frames/` path. They are not deleted by this
work and retention is out of scope.

## Decision 2 — the assembled master is visually lossless, and that is not a detail

Encode the master with a **lossless** codec (FFV1 in Matroska is the expected choice;
justify any other). **Do not encode the master with H.264 or any lossy codec.** Reasons:

- The degradation chain's entire job is to add compression, noise and artifacts that the
  `PostProfile` specifies. A lossy master bakes in artifacts **no profile asked for**, and
  they are indistinguishable from the intended ones.
- **FR-21's determinism acceptance (AT-05) requires that re-applying profile version X to
  the same master with the same seed and pinned binaries reproduces a byte-identical
  encode.** That holds only if the master is a stable, exactly-reproducible input.

The frames are **linear-light float EXR** and the master is an integer video. That
conversion is a real, lossy-in-principle step and must be **explicit and recorded**, not
left to an FFmpeg default: state the transfer function applied and the bit depth chosen,
and record them in the output's provenance. This step is the "sensor to recording"
boundary; a silent default here would make the whole chain unreproducible.

Frame rate comes from the **plan's 60 fps master**, never from `CameraProfile.fps`, which
is the post-only output rate (12.0 by default). Confusing them makes the master 5× wrong.

## Decision 3 — degradation accepts the master, and is reachable

`degrade_media` currently has a route and a working adapter but **no composition**: unlike
the renders it passes the raw request through, so a CLI call fails with nine validation
errors for `shot_version`, `run_id`, `degrade_seed`, `master_relative`, `font_relative`,
`camera_profile` and `profile`. Measured 2026-11-06.

Add a `compose_degrade_request` to `orchestrator/service/compose.py` alongside
`compose_render_request`, obeying the same rules: pure, reads through `ProjectStore` only,
no writes, no command lock, no stage-seed derivation. It resolves the shot's **master**
`ShotOutput` for the current content version and fails with a `JobError` naming the reason
if there is none — the same shape as `"has no scene output; run build_scene first"`.

`degrade_seed` comes from the `Shot`. The `PostProfile` comes from `Project.output_profile`
by way of `compose_post_profile_spec`, which already exists. Do not invent a second route
to either.

## Out of scope

Retention or deletion of the frame intermediates, the rubric scorer, animation and FR-20
(another agent owns those files), any new API operation, and the texture-projection work.

## Test rules

- **No GPU, no Blender, no ComfyUI.** FFmpeg may be invoked only if a test needs real
  encoding; prefer asserting on the composed argv. Fake the subprocess.
- The regression that matters: **a master receipt addresses the whole shot, not one
  frame** — assert the recorded artifact's duration or frame count corresponds to
  `frame_end`, not to 1.
- Required cases: the assembled master is one file and its receipt hashes it; the encode
  is lossless and the argv says so; frame rate comes from the plan's 60 fps and not from
  `CameraProfile.fps`; the transfer function and bit depth are explicit in the argv;
  `compose_degrade_request` resolves the master and fails loudly when none exists;
  a degrade request composed from a Shot carries that Shot's `degrade_seed`.
- Give every assertion a failure message saying what was observed.

## Verification

```
D:\Work\video_pipeline\.venv\Scripts\python.exe -m pytest -o addopts="" -q
D:\Work\video_pipeline\.venv\Scripts\python.exe -m ruff check orchestrator/ tests/ --output-format=concise
```

Baseline: **1351 passed, 34 skipped**, ruff clean, about 190 s.

## A note on this contract's own risk

Decision 1 is reversible and was taken by the orchestrator, not the creator. If a master
should instead remain a frame sequence addressed by a manifest file, only the artifact
shape changes — Decision 3 stands either way. Say so if the implementation reveals a
reason to prefer the manifest.

## Amendment 1 — where the encode parameters are recorded

Raised by the blind test author: Decision 2 requires the transfer function and bit depth to
be "recorded in the output's provenance", but **`ShotOutput` has no fields for them**. Its
provenance fields are `engine_id`, `engine_build`, `profile_semver`, `filter_graph_hash`
and `pinned_binaries`, and `_provenance_matches_kind` constrains which may appear on a
`master`. The contract asked for something the entity cannot hold. The test author was
right; the contract was wrong.

**Binding: record them on the `GenerationRun`, not on the `ShotOutput`.** `GenerationRun`
already carries `invocation` (the exact argv) and `settings` (a typed bag), which is
precisely what an encode's parameters are. Put the transfer function and bit depth in
`settings`, where they are queryable, and let `invocation` hold the argv that proves them.

**Do not add fields to `ShotOutput`.** Two reasons beyond the schema cost (FR-28 migration):
its provenance fields answer "which engine identity produced this artifact", which codec
parameters do not; and the `PostProfile` already owns encode settings for the delivery
side, so a third home for the same kind of information invites the two to disagree.

The requirement itself is unchanged and is not softened: the transfer function and bit
depth must be **explicit in the argv** rather than left to an FFmpeg default, because the
frames are linear-light float EXR and the master is integer video. A silent default makes
the chain unreproducible and breaks FR-21's AT-05 acceptance.

## Amendment 2 — the composer's signature

Also raised by the test author: the contract names `compose_degrade_request` without
giving its signature or saying where the overlay font comes from. Settle both:

- **Signature:** keyword-only, matching `compose_render_request` —
  `compose_degrade_request(store, *, shot_id, episode_id, run_id)`. Same purity rules.
- **The font is not caller-supplied.** `DegradeRequest.font_relative` is resolved by the
  composer from the `PostProfile`'s overlay model, which §FR-21 says "specifies clock
  epoch, drift rate, format string, camera ID text, **font file**, placement". A caller
  passing a font would be a second source of truth for a field the profile already owns,
  and would let a caller silently change the burned-in overlay's appearance without a
  profile revision. If the profile's overlay model does not currently carry a usable font
  path, say so and report it rather than inventing a parameter.

## Amendment 3 — the overlay font becomes a profile field, because AT-05 requires it

Amendment 2 ruled that the composer resolves the overlay font from the `PostProfile`,
citing FR-21, and told the implementer to report rather than invent if the entity could not
carry it. It reported: **`PostProfileOverlay` has `camera_id`, `clock_epoch`, `fontsize`,
`x`, `y` and `placement` — and no font path.** The adapter takes `font_path` as a separate
caller-supplied argument. FR-21's normative text ("the overlay model specifies clock epoch,
drift rate, format string, camera ID text, **font file**, placement") is simply not
implemented. Amendment 2 asked for something that does not exist; the report was correct.

**This is not a naming gap, it is a determinism hole.** FR-21's acceptance, AT-05, requires
that re-applying profile version X to the same master with the same seed and the same
pinned binaries reproduces a **byte-identical encode**. While the font is a caller
argument, two runs with the same profile, same seed and same binaries can produce different
burned-in overlays by passing a different font. The guarantee is unenforceable as built.

**Binding:** add a font field to `PostProfileOverlay` — a project-relative path, validated
for containment like every other path (FR-25), with the existing `schema_version` and
FR-28 migration hooks carrying the change. The composer resolves it from the profile; the
caller does not supply it. The adapter keeps its containment validation.

Where an existing profile record has no font, fail loudly naming the profile and the
missing field rather than substituting a default: a silently chosen font changes the
recording's appearance, and FR-21 treats the overlay as part of the camera's identity.

This is a domain schema change made under a sleeping creator's standing instruction to
continue. It is small and reversible, and it moves the code **towards** the PRD rather
than away from it, which is why it was taken rather than deferred.

## Amendment 4 — clamping scene-linear highlights is correct, and must be stated

The implementer reported that the linear-to-BT.709 conversion clamps scene-linear samples
above the SDR range, and that the contract does not define tone mapping. It does not, and
clamping is the right behaviour — but by argument, not by default.

A DVR does not tone-map; it clips. §3.2's face identity-destruction rule depends on exactly
this, naming "highlight clipping such that ≥60% of the face area lies within 5% of white"
as one of its two accepted routes. A tone-mapped master would roll those highlights off and
remove a mechanism the PRD relies on.

**Binding:** clamp, do not tone-map, and record it as a deliberate choice in the
`GenerationRun` settings alongside the transfer function and bit depth. If a future profile
wants a filmic roll-off it belongs in the `PostProfile` stage list, where it is versioned
and seeded, not in the master conversion.
