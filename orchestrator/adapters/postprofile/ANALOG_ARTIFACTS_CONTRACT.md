# CameraProfile.analog_artifacts contract — step 3c draft

**Status:** feasibility draft for creator adjudication; not yet normative.

**Basis:** creator decision, 2026-11-22: build a thin processor on the `ntscrs` core crate.

**Pinned source:** `ntsc-rs` v0.9.7, commit `8cfc51014092a6dc1da5eb0217bb9ae6516b124e`.

This draft replaces step 3a's refusal of non-empty `analog_artifacts` once step 3c is
implemented and the pinned executable is available. Until that implementation is merged,
step 3a's refusal remains in force. `{}` remains inactive and must not launch the new
processor. An unavailable or mismatched processor is an error, never a reason to drop the
field or silently use FFmpeg alone.

## R1 — Typed, opt-in camera settings

Replace `CameraProfile.analog_artifacts: dict[str, float]` with a frozen Pydantic v2
`AnalogArtifacts` model. Every nested model uses `extra="forbid"`; numeric values are
finite and range checked. The top-level model defaults to `{}`. Its optional groups
default to `None`, so `{}` is neutral and does no NTSC conversion. Presence of any group
activates the external stage; omitted members use explicit defaults pinned to this crate
revision. Do not pass arbitrary crate JSON through the CameraProfile API.

Candidate exposed controls (ranges are from v0.9.7's public setting descriptors):

| Group | Fields and ranges |
|---|---|
| `head_switching` | `height` 0–24; `offset` 0–24; `horizontal_shift` -100–100 crate units; optional mid-line `position` and `jitter` 0–100%. |
| `tracking_noise` | `height` 0–120; `wave_intensity` -50–50; `snow_intensity` 0–1; `snow_anisotropy` 0–100%; `noise_intensity` 0–100%. |
| `vhs` | `tape_speed`: `SP`, `LP`, `EP`, `none`; `chroma_loss` 0–100%; `edge_wave.intensity` 0–20 pixels, `speed` 0–10, `frequency` 0–0.5, `detail` 1–5; optional `sharpen.intensity` 0–5 and `frequency` 0.5–4. |
| `signal` | `composite_sharpening` -1–2; `luma_smear` 0–1; chroma `phase_error` and `phase_noise` 0–100%; horizontal delay -40–40, vertical delay -20–20; `chroma_lowpass_in/out`: `None/Light/Full`; `chroma_demodulation`: `Box/Notch/OneLineComb/TwoLineComb/TwoD/TwoDAdaptive`; `input_luma_filter`: `None/Box/Notch`; `field_mode`: `Alternating/Upper/Lower/Both/InterleavedUpper/InterleavedLower`; `filter_type`: `ConstantK/Butterworth`; `vertical_chroma_blend`: bool. |
| `noise` | Optional `composite`, `luma`, and `chroma` noise blocks: `intensity` 0–100%, `frequency` 0–1 for composite/luma and 0–0.5 for chroma, `detail` 1–5. `snow_intensity` 0–100 (frequency, not percent) and `snow_anisotropy` 0–100%. |
| `ringing` | `frequency` 0–100%; `power` 1–10; `intensity` 0–10. |

The library's `random_seed` is internal and is never creator supplied. `ScaleSettings` is
excluded initially because resolution and scaling already belong to PostProfile. An
active stage always includes the crate's NTSC encode/decode path; a `head_switching`-only
profile is not a pixel-preserving pass-through.

## R2 — Derived plan and measured order

`postprofile_spec_from_camera` derives the active model and records provenance under the
same canonical spec hash as encode settings. `camera_analog_artifacts` is an external
stage marker, not an FFmpeg filter string. The validated plan splits the existing graph
around it and records both FFmpeg command plans, the typed settings, source commit, seed,
and executable SHA-256. No arbitrary filter graph or shell text is accepted.

Preserve the measured order in `profile.py`: scale, fps, select, loop, schedule clock
`setpts`, in-recording drawtext, output `setpts`, color-temperature/channel/eq stages,
chroma-smearing `gblur`, FFmpeg noise, unsharp, **ntsc-rs**, setsar, then encode. The
proposed insertion is after `unsharp` and before `setsar`/encode. The producer FFmpeg
applies the validated prefix and emits contiguous `rgb24`; `ntsc-pipe` transforms each
same-size frame; the consumer FFmpeg applies only metadata/encode work and produces the
master output. Set SAR on the consumer side. Both FFmpeg invocations retain the existing
`+bitexact` flags; encoder thread count remains explicit and single-threaded.

This ordering means existing `noise_model`, `chroma_smearing`, and sharpening are part of
the input signal to ntsc-rs. They combine additively with the crate's own noise/chroma
loss. That is a proposal, not a measured artistic decision. `in_recording` overlays are
drawn before the processor and are tape-processed. `on_playback` overlays remain in the
export/cut path after PostProfile, so they are not tape-processed. This matches the
security-camera distinction between a DVR burn-in and a viewer/display overlay; confirm
that the creator wants the clock and REC indicator treated this way.

## R3 — CLI and raw-frame protocol

The executable accepts width, height, optional exact frame count (otherwise EOF), signed
32-bit seed, settings JSON path, and an optional starting frame index. It reads packed
`rgb24` bytes from stdin and writes same-size `rgb24` bytes to stdout. It uses the public
`NtscEffect::apply_effect_to_buffer::<Rgb, u8>` API and `Context`; one input frame yields
one output frame. The production adapter passes the validated frame count, so short,
extra, or partial-frame input is `CORRUPT_ARTIFACT`. The settings file is generated from
the typed model inside the reserved run directory using `ProjectRoot`; it is not an
untrusted user path.

The pinned crate exposes `NtscEffect`, `Context`, `SettingsList`, `YiqOwned`/`YiqView`,
and `PixelFormat` (`Rgb` is three interleaved channels). `apply_effect_to_buffer` takes
dimensions, mutable pixels, `frame_num: usize`, and `[f32; 2]` scale. Stochastic passes
mix the configured seed with fixed per-effect salts and `frame_num`; VHS edge wave also
uses frame number as its temporal coordinate. There is no cross-frame accumulator in the
per-frame API. `YiqOwned` is built afresh for each call.

## R4 — Seed, frame identity, and determinism

Derive one stage seed as
`derive_seed(shot_id, degrade_seed, "camera_analog_artifacts", INT32_MAX)`. Pass that
0–`INT32_MAX` result to the crate's signed `i32` `random_seed`; do not mask, wrap, or use
the raw `degrade_seed`. The crate internally separates effect random streams and mixes
the frame index.

Frame N means the Nth frame entering the external stage after the validated PostProfile
prefix; a full-shot run starts at 0. A future partial-window run must pass the same
absolute stage-input frame index or be refused; raw RGB carries no timestamps or frame
indices. Cuts assembled from an already degraded full-shot artifact do not rerun this
stage. The stage is frame independent: output N depends on seed, N, settings, and input
frame N, not prior frames. It may therefore be parallelized after frame indices are
assigned.

Required guarantee: same input, profile, shot/seed, frame index, pinned binaries, build,
and host produce byte-identical output. The spike saw identical output for repeated same
seeds, different output for a different seed, and identical output for a frame at index 4
after changing all prior frames. The no-Rayon executable is the production candidate.
The optional Rayon build also matched byte-for-byte at 1 and 4 workers on the tested
frames. `Context::new()` uses `fearless_simd::Level::new()` when the crate's `std` feature
is enabled, so cross-CPU/SIMD and cross-toolchain bit-exactness is not established. Pin
the built executable hash and toolchain/target; do not promise portability of exact pixels
across machines unless a portable SIMD mode is separately measured.

## R5 — Binary pinning, installation, and runtime failures

Add `settings.binaries.ntsc_pipe_exe` for the installed executable. Add the expected
`sha256:<hex>` to `PostProfile.identity.pinned_binaries["ntsc-pipe"]`, alongside the
existing full FFmpeg banner pin. Verify both identities before reserving a run. A missing
or unlaunchable executable maps to `JobError(WORKER_UNREACHABLE)` with an install hint; a
hash mismatch maps to `JobError(INVALID_MANIFEST)`. A nonzero child exit is retained with
its stage log and mapped through the existing closed `JobError` set. Do not add a new
error kind or fallback path.

Feasibility candidate: Windows x64, Rust/Cargo 1.96.1, `--release`, `std` enabled,
Rayon disabled; clean offline build took 24.581 s, produced 441,344 bytes, SHA-256
`111eaa0ffc3066150f80bfbacc45cd366c43e87b0275e48a38a2bd2ee449e553`. This digest is
evidence for this build only; installation must rebuild or ship the exact binary and pin
its verified SHA.

Proposed packaging: check in the thin CLI source, `Cargo.lock`, and the upstream core
crate at the pinned commit (or an equivalent pinned vendor snapshot); add
`tools/build_ntsc_pipe.ps1` to run `cargo build --release --locked --offline` and verify
the SHA-256. Runtime never fetches crates or source. Build outputs and the installed
binary are deployment artifacts, not per-job files. All settings, logs, metadata, and
video output produced by a job stay under `ProjectRoot` and go through its containment
helper. The exact provisioned binary directory and how installation writes fit FR-25's
containment rule need creator/PRD-owner adjudication.

## R6 — Process lifecycle, timeout, and partial artifacts

The runner owns all three children (FFmpeg producer, `ntsc-pipe`, FFmpeg encoder) and
both pipes. Use argv with `shell=False`. Drain stderr to separate contained logs so a
full pipe cannot deadlock. If any child fails, close both pipes, terminate the other
children, wait, then kill any survivor and confirm all are gone before reporting. The
parent cancellation token must stop all three children and report `JobError(CANCELLED)`;
deadline expiry uses the existing per-shot job deadline and maps to
`JobError(BUDGET_EXCEEDED)`. The current adapter waits on one process without a timeout
or cancellation hook, and the service integration contract notes this gap; step 3c must
extend the handoff rather than claim cancellation that cannot stop the pipeline. No
automatic retry for a failed processor. Retain failure metadata/logs. Raw frames travel
only in pipes; any required scratch file stays in the reserved run directory.

## R7 — Initial performance budget and licensing

Initial acceptance proposal: at least 30 processed fps at 640×480 with the no-Rayon
release build and single-threaded x264 encoding. The generated testsrc2 run measured
90–93 fps for the light VHS preset and 121 fps for heavy tracking at 640×480; 352×288
measured 224–245 fps. These are one-run feasibility results, not a production SLA. The
four-worker optional build measured 169 fps at 640×480 and 30 fps input. Larger sizes,
real masters, concurrent CPU work, and worst-case all-controls-active settings remain
unmeasured. Creator must approve the supported resolution ceiling and throughput floor.

`cargo tree`/`cargo metadata` found no copyleft packages in the active set or tested
optional Rayon/libm sets. Core: `ntsc-rs` is MIT OR ISC OR Apache-2.0. Active dependencies:
`fearless_simd` Apache-2.0 OR MIT; `hifijson` MIT; `num_enum` and derive BSD-3-Clause OR
MIT OR Apache-2.0; `proc-macro2`, `quote`, `rustversion`, `syn` MIT OR Apache-2.0;
`unicode-ident` (MIT OR Apache-2.0) AND Unicode-3.0. The Rayon-only chain (`rayon-core`,
`num_cpus`, `crossbeam-deque/epoch/utils`, `hermit-abi`, `libc`) is MIT OR Apache-2.0.
The optional `libm` chain (`core_maths`, `libm`) is MIT.
Ship upstream's Apache, ISC, and MIT license files plus the selected transitive license
texts, including `LICENSE-UNICODE`, in a `THIRD_PARTY_NOTICES` bundle. Re-run metadata
when Cargo.lock or features change.

## Creator decisions

1. Confirm NTSC composite as the intended standard. This crate does not implement a PAL
   or SECAM path.
2. Approve the exposed control list and defaults, especially field mode, chroma
   demodulation, noise, snow, ringing, and `ScaleSettings` exclusion.
3. Confirm `vhs.edge_wave` is sufficient for the requested video warble. It is horizontal
   image waviness keyed by frame number; this crate does not alter audio pitch or create
   global frame/timestamp jitter.
4. Approve additive composition after existing FFmpeg noise/chroma-smear/sharpening, or
   define which overlapping effect family should replace another.
5. Confirm in-recording versus on-playback overlay behavior and partial-window frame-index
   semantics.
6. Approve the single-thread determinism scope versus enabling Rayon, the 30-fps budget,
   and the maximum supported storage resolution.
7. Choose the installed binary location and adjudicate build-time provisioning under
   FR-25. Confirm the exact host/build scope for the binary SHA pin.

## Non-goals

No GStreamer CLI; no generated audio or audio wow/flutter; no PAL/SECAM model; no
reference-video input; no arbitrary crate settings JSON; no runtime network access; no
automatic retries; no re-render; no approval changes; no changes to profile impact tags.

## File ownership for implementation

**Implementer:** `orchestrator/domain/entities.py` (typed field/schema migration),
`orchestrator/service/runner.py` (derived camera stage/provenance),
`orchestrator/adapters/postprofile/profile.py` (typed validation and split plan),
`orchestrator/adapters/postprofile/runner.py` (three-process lifecycle, pins, metadata),
`orchestrator/platform/settings.py` (binary path), the pinned Rust CLI/source and lock
file under the adapter boundary, `tools/build_ntsc_pipe.ps1`, and third-party notices.
Keep the new field post-only; delegate impact classification to the existing dependency
graph. No renderer or GPU work.

**Independent test author:** only a new
`tests/adapters/postprofile/test_analog_artifacts_contract.py` and Rust CLI tests. Cover
empty-model inactivity; extras/range rejection; settings translation; frame byte counts;
seed derivation; same/different-seed determinism; frame independence/index identity;
stage order and overlay placement; binary missing/hash mismatch; subprocess failure,
timeout/cancel teardown; bitexact repeated end-to-end encode; receipt and containment.

## Done when

Every creator decision above is resolved; non-empty model fields are validated and
actually affect pixels; `{}` leaves the existing PostProfile graph/output unchanged and
does not launch ntsc-pipe; binaries and dependencies are pinned with notices; all writes
use ProjectRoot; failure/cancel/timeout paths are classified and leave no child process;
required tests and a real 640×480 degrade meet the approved throughput/determinism
budget; the contact sheet is reviewed by the creator.

## Creator decisions (2026-11-24) — these supersede the "Creator decisions" list above

1. **Standard.** NTSC composite through ntsc-rs ships now. PAL colour behaviour is a
   recorded known gap: PAL cancels hue errors line by line, so a bad signal loses
   saturation and shows Hanover bars, where NTSC shifts hue and has a different dot-crawl
   pattern. Revisit it if a story needs authentic European colour tape. Monochrome
   profiles are effectively unaffected.
2. **Controls.** The draft's exposed control list and defaults are approved as proposed,
   and `ScaleSettings` stays excluded. `vhs.edge_wave` is the warble control; there is no
   audio wow/flutter.
3. **Order.** The tape stage runs **after** the existing camera effects (grade,
   colortemperature/colorchannelmixer/eq, chroma smearing, noise, sharpening) and before
   the encode. It is additive: overlapping families stack, and the agent chooses what to
   enable. Tape does not override any 3a field.
4. **Overlays.** `in_recording` overlays (clock and REC indicator) are tape-processed,
   because they are burned into the signal before tape. `on_playback` overlays, drawn at
   cut assembly (step 3b), stay crisp.
5. **Determinism and performance.** ntsc-rs runs single-threaded, with the `rayon`
   feature off, for byte-identical output. The spike measured 224–245 fps at 352×288 and
   about 91 fps at 640×480. The performance budget is whatever the draft records against
   those measurements.
6. **Provisioning.** The source is vendored and built by script:
   - Commit our small CLI source and `Cargo.lock` under `tools/ntsc-pipe/`, depending on
     ntsc-rs pinned by git commit `8cfc51014092a6dc1da5eb0217bb9ae6516b124e`.
   - Add `tools/build_ntsc_pipe.ps1`. It builds a release binary into a gitignored local
     bin directory and writes the binary's SHA-256 beside it.
   - Settings point at the binary. Every run verifies the SHA-256 and records it in
     `pinned_binaries`.
   - A missing binary or a hash mismatch fails clearly, naming the build script. Building
     needs a one-time network fetch; running never does.
   - Ship the upstream licence and NOTICE obligations the draft lists.

## Binary pin for existing projects (2026-11-24)

A camera revision can supply `analog_artifacts.ntsc_pipe_sha256` as
`sha256:<64 lowercase hex>` from `tools/bin/ntsc-pipe.exe.sha256`. This preserves
the selected PostProfile and uses the existing master. An authored PostProfile
can still supply the pin in `pinned_binaries["ntsc-pipe"]`. If both supply a pin,
the values must match. The effective PostProfile records the resolved pin. A pin
alone leaves NTSC inactive.
