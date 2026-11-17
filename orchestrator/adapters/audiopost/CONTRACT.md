# Contract — AudioPostProfile (FR-32, first half)

Drafted 2026-11-17, before code. The implementer and a blind test author work from this
statement. Where it and the PRD disagree, the PRD wins; say so.

## 0. Scope

FR-32 owns two transforms on generated speech. This contract builds **`AudioPostProfile`**
only: a versioned, seeded degradation chain applied to one voice take. Out of scope, and
not to be started: `PerformanceEdit`, the calm-baseline F0 acceptance check, mixing,
attaching takes to Shots, and any change to the video `PostProfile` (FR-21).

Evidence: spike 29 (`spikes/29_voice_degradation/`). The creator chose the `overdriven` chain
at +30 dB. A feasibility check on 2026-11-17 ran that chain with two-pass loudness
normalisation twice on a real take: the outputs were **byte-identical**, and the 8 kHz µ-law
encode measured **−17.9 LUFS**.

## 1. The entity

`AudioPostProfile` in `orchestrator/domain/entities.py`, an unversioned `Entity` stored at
`audio_post_profiles/{name}.json` and created once (`expected_version=0`), exactly like
`PostProfile`. A changed chain is a **new name** (for example `cctv-overdriven-2`), never an
edit; records are immutable once written.

| Field | Rule |
|---|---|
| `name` | `^[a-z0-9][a-z0-9-]{0,63}$` |
| `semver` | MAJOR.MINOR.PATCH digits |
| `intent` | non-blank free text (what source it imitates) |
| `voice_stages` | ordered list, 1–24 `AudioStage` (2) |
| `noise` | optional `{color: white|pink|brown, amplitude: (0, 0.5]}` |
| `hum` | optional `{frequency_hz: 50|60, amplitude: (0, 0.2], harmonics: [{multiple: 2..10, relative: (0, 1]}]}` (0–4 harmonics) |
| `encode` | `{codec: pcm_s16le|pcm_mulaw, sample_rate: 8000|16000|22050|44100|48000}`; mono always; `pcm_mulaw` requires 8000 |
| `pinned_binaries` | `{"ffmpeg": <normalised identity>}`, filled at create time (3.1) |

Loudness is **not** a field: every profile ends in FR-08's normalisation, `I=-18`,
`TP=-1`, `LRA=11`, applied in two passes (4.2).

## 2. Stages: a closed allowlist

Each `AudioStage` is `{name, filter, params}`. `name` is `^[a-z0-9_]{1,32}$` and unique
within the profile. `filter` must be one of the following; params are exactly those listed
(no others), typed and range-checked. Anything else is refused at create time **and** again
when applied.

| filter | params (all required unless a default is shown) |
|---|---|
| `highpass`, `lowpass` | `f`: int 20..20000; `poles`: 1|2 (default 2) |
| `volume` | `gain_db`: float −60..60 |
| `asoftclip` | `type`: hard|tanh|atan|cubic|exp|alg|quintic|sin|erf; `threshold`: float (0, 1] |
| `aecho` | `in_gain`, `out_gain`: float (0, 1]; `delays_ms`: 1–4 ints 1..1000; `decays`: same count of floats (0, 1] |
| `acompressor` | `threshold`: float [0.000976563, 1] (FFmpeg's own declared range, inclusive; corrected 2026-11-17); `ratio`: float 1..20; `attack`: float 0.01..2000; `release`: float 0.01..9000; `makeup`: float 1..64 |
| `acrusher` | `bits`: float 1..64; `mode`: lin|log; `aa`: float 0..1; `samples`: float 1..250 (default 1); `level_in`, `level_out`: float 0.015625..64 (default 1) |
| `vibrato` | `f`: float 0.1..20000; `d`: float 0..1 |
| `tremolo` | `f`: float 0.1..20000; `d`: float 0..1 |
| `aresample` | `sample_rate`: int 1000..192000 (an intermediate rate; used for aliasing) |

Numbers must be finite and not booleans. Strings are only the enumerated values above.
The graph text is built by the code from these typed values; no user string is ever
interpolated into it.

## 3. Operations

Added to `OPERATIONS`; HTTP, CLI and MCP follow.

| name | method | path | async |
|---|---|---|---|
| `create_audio_post_profile` | POST | `/projects/{project_id}/audio-post-profiles/{profile_name}` | no |
| `apply_audio_post_profile` | POST | `/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/post` | yes |

### 3.1 `create_audio_post_profile`

The body is `{audio_post_profile: <AudioPostProfile without pinned_binaries>}`. The path
name must equal the body's `name`. The handler validates every stage (section 2), runs
`<settings.binaries.ffmpeg_exe> -hide_banner -version` once, stores the normalised identity
(`orchestrator/platform/ffmpeg_identity.py`) as `pinned_binaries["ffmpeg"]`, and saves with
`expected_version=0`. A second create with the same name is a version conflict. A
`pinned_binaries` supplied in the body is refused: the identity is measured, never
asserted.

### 3.2 `apply_audio_post_profile`

The body is `{expected_version, take_asset_id, profile_name}`.

- `take_asset_id` must be a registered Asset of type `voice` or `audio`, a `.wav` whose bytes
  match its checksum. Its `voice_consent` must be set and must not be `licensed_recording`.
  A post-processed clip is a voice that will be heard, and a licensed recording may only
  lend emotion.
- The job binds to the Shot (budgets and journal), through `JobRunner`, as a new **CPU**
  `AdapterKind.AUDIO_POST`: `preflight_cpu`, no GPU lease. Like the speech kinds it is a
  sidecar: it never changes the Shot, its status, its approval or its `ShotOutput`s.
- Validation is synchronous; an invalid request creates no job and writes no files.
- *(Amended 2026-11-17.)* At execution the take's registry record is re-read: it must still
  exist and still be a `voice`/`audio` record with the same path and checksum, and its
  `voice_consent` must still be one that permits cloning (not `None`, not
  `licensed_recording`). Otherwise the job fails (`INVALID_MANIFEST`) before anything is
  written.
- The take's checksum is verified again at execution, and the FFmpeg identity is compared
  byte-for-byte with the profile's `pinned_binaries["ffmpeg"]` before anything is written.
  A mismatch fails the job (`INVALID_MANIFEST`) without output.

## 4. Execution

### 4.1 Graph

```
[0:a] aformat=channel_layouts=mono, aresample=22050, <voice stages in order> [v];
anoisesrc=color=<c>:amplitude=<a>:seed=<derived>:sample_rate=22050 [n];        (if noise)
sine=f=<hz>:sample_rate=22050, volume=<a> [h0]; sine=f=<hz*m>…, volume=<a*r> [h1…]  (if hum)
[v][n][h0]… amix=inputs=<k>:duration=first:normalize=0, <loudnorm> [out]
```

Everything runs at 22050 Hz until the encode. With neither noise nor hum the `amix` is
omitted.

### 4.2 Loudness, two passes

Pass 1 runs the graph ending in `loudnorm=I=-18:TP=-1:LRA=11:print_format=json` to the null
muxer and parses the measured values. Pass 2 runs the same graph ending in
`loudnorm=I=-18:TP=-1:LRA=11:measured_I=…:measured_TP=…:measured_LRA=…:measured_thresh=…:offset=…:linear=true`
and encodes. The adapter then measures the encoded file's integrated loudness (FFmpeg
`ebur128`). It must be within **0.5 LU of −18 LUFS** (AT-24); otherwise the job fails
(`CORRUPT_ARTIFACT`) and the take is discarded.

### 4.3 Seeds

The noise seed is derived from `(profile name, semver, stage "noise", take_asset_id)`, the
FR-32 tuple `(profile, stage, line)`, by a new `derive_audio_seed` in
`orchestrator/domain/seeds.py`. It follows `derive_seed`'s method (SHA-256 of a
`|`-joined string, first 16 hex digits) with a distinct prefix (`"audio|"`) so it cannot
collide with video seeds, and is reduced modulo `UINT32_MAX + 1` (anoisesrc's range). It
is never wrapped or masked silently beyond that stated reduction.

### 4.4 Determinism

Both passes use `-hide_banner -nostdin -y -threads 1`, input `-fflags +bitexact`, and output
`-flags +bitexact -fflags +bitexact -map_metadata -1`. argv is a list, `shell=False`, and
`popen` is injectable (as in `PostProfileAdapter`). Re-applying the same profile to the
same take therefore yields a byte-identical file, which AT-24 requires.

## 5. Outputs, provenance, registration

Through `ProjectRoot` only, under
`generated/audio_post/{shot_id}/v{shot_version:04d}/{run_id}/`: `take.wav`, `report.json`,
`logs/ffmpeg.log` (both passes). The encode goes to a temporary name and is renamed to
`take.wav` only after the loudness check passes. `report.json` records: the profile name,
semver and full stage list; the rendered pass-2 graph and its SHA-256
(`filter_graph_hash`); the noise seed; the FFmpeg identity; the pass-1 measurements; the
measured output loudness; the source take's asset id and SHA-256; and the output's
SHA-256, sample rate, codec and duration.

On success a `GenerationRun` (input: the source take; tool `ffmpeg`) and a new Asset
`audiopost-{run_id}` are saved:

| field | value |
|---|---|
| `type` | `voice` |
| `generated` | `True` |
| `provider` | `audio_post_profile:{name}@{semver}` |
| `license`, `model_or_voice_license`, `voice_consent`, `voice_consent_basis`, `reuse_scope` | **copied from the source take**: degradation changes no rights |
| `file_path`, `checksum` | the new take |

A failed or cancelled run leaves no `take.wav` and no Asset; its `GenerationRun` records
the failure. *(Amended 2026-11-17.)* As for the speech kinds, a job cancelled before
dispatch never started a run, so it has no `GenerationRun`; the job journal records its
cancellation. The integrated-loudness reading is taken only from the final `ebur128`
summary, which must hold exactly one finite `I:` value; anything else fails the job. Cancel and completion resolve to exactly one outcome, through the same
finalizing mechanism the speech kinds use in `JobRunner`.

## 6. Preset

`orchestrator/adapters/audiopost/presets/cctv-overdriven.json` holds the creator's choice
(spike 29) as a creatable body: highpass 350 (2 poles), volume +30 dB, asoftclip hard 0.25,
lowpass 2800, highpass 450, aecho 0.8/0.45 with delays 28 and 47 ms and decays 0.3 and 0.2,
acompressor threshold 0.03, ratio 12, attack 5, release 500, makeup 4; pink noise 0.035;
60 Hz hum 0.02 with a 3rd harmonic at 1/3; encode `pcm_mulaw` at 8000 Hz; semver 1.0.0.

## 7. Documentation

`orchestrator/adapters/audiopost/README.md`: what a profile is, the allowlist, the preset,
how to create and apply one, determinism and loudness guarantees, and that rights are
inherited from the source take.

## 8. Rules for the test author

Test the contract and public surfaces only; do not read the implementation. Fake the FFmpeg
boundary for unit tests. **One test must run the real FFmpeg twice on a short generated
input** (a synthesised tone or noise WAV, not a project take) through the preset and assert
byte-identical outputs and loudness within 0.5 LU; it skips cleanly if `ffmpeg` is absent.
Report ambiguities rather than guessing.

## 9. The implementer must report

Every divergence and why; anything unimplemented; what was verified for real.
