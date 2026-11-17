# AudioPostProfile

A versioned, seeded degradation chain applied to one voice take (FR-32, first half). It turns a
clean generated or recorded voice into the sound of cheap security-camera audio without
regenerating speech. `PerformanceEdit`, mixing and attaching takes to Shots are out of scope.

The behaviour is specified in `CONTRACT.md`; this file is how to use it.

## What a profile is

An `AudioPostProfile` is an unversioned record at `audio_post_profiles/{name}.json`, created once
and never edited. A changed chain is a **new name** (for example `cctv-overdriven-2`).

| Field | Meaning |
|---|---|
| `name`, `semver`, `intent` | identity, and what source it imitates |
| `voice_stages` | 1-24 ordered stages, each `{name, filter, params}` |
| `noise` | optional seeded `white`, `pink` or `brown` noise |
| `hum` | optional 50 or 60 Hz mains hum with up to four harmonics |
| `encode` | `pcm_s16le` or `pcm_mulaw` (8000 Hz only), mono always |
| `pinned_binaries` | the FFmpeg identity, **measured** at create time and never supplied |

Loudness is not a field. Every profile ends in FR-08's normalisation (`I=-18`, `TP=-1`, `LRA=11`).

## The allowlist

Only these filters, with exactly these parameters, are accepted. Anything else is refused when
the profile is created and again when it is applied. The graph text is built by the code from the
typed values; no string you supply is ever placed in it.

| filter | params |
|---|---|
| `highpass`, `lowpass` | `f` 20..20000, `poles` 1 or 2 (default 2) |
| `volume` | `gain_db` -60..60 |
| `asoftclip` | `type` hard, tanh, atan, cubic, exp, alg, quintic, sin, erf; `threshold` (0, 1] |
| `aecho` | `in_gain`, `out_gain` (0, 1]; `delays_ms` 1-4 ints 1..1000; `decays` the same count, (0, 1] |
| `acompressor` | `threshold` 0.000976563..1, `ratio` 1..20, `attack` 0.01..2000, `release` 0.01..9000, `makeup` 1..64 |
| `acrusher` | `bits` 1..64, `mode` lin or log, `aa` 0..1, `samples` 1..250 (default 1), `level_in`, `level_out` 0.015625..64 (default 1) |
| `vibrato`, `tremolo` | `f` 0.1..20000, `d` 0..1 |
| `aresample` | `sample_rate` 1000..192000, an intermediate rate used for aliasing; the chain returns to 22050 Hz afterwards |

Numbers must be finite and not booleans. Stage names are `^[a-z0-9_]{1,32}$` and unique.

## The preset

`presets/cctv-overdriven.json` is the creator's choice from spike 29, as a body ready to POST:
highpass 350, +30 dB, hard clip 0.25, lowpass 2800, highpass 450, a two-tap echo, heavy
compression, pink noise 0.035, 60 Hz hum with a third harmonic, delivered as 8 kHz mu-law.

## Create and apply

```
POST /projects/{project_id}/audio-post-profiles/{profile_name}      body: the preset file
POST /projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/post
     body: {"expected_version": <shot token>, "take_asset_id": "...", "profile_name": "cctv-overdriven"}
```

The same operations exist on the CLI (`create_audio_post_profile`, `apply_audio_post_profile`)
and as agent tools. Creating runs `ffmpeg -hide_banner -version` once and pins the result; a
second create with the same name is a version conflict.

Applying is asynchronous (202 and a job). The take must be a registered `.wav` Asset of type
`voice` or `audio`, its bytes matching its checksum, with a recorded `voice_consent` that is not
`licensed_recording` (such a recording may lend emotion but never be heard). The job runs on the
CPU, takes no GPU lease, and like the speech jobs never changes the Shot, its status, its
approval or its outputs.

Output is under `generated/audio_post/{shot_id}/v{shot_version:04d}/{run_id}/`: `take.wav`,
`report.json` and `logs/ffmpeg.log`. On success a `GenerationRun` and an Asset
`audiopost-{run_id}` are saved. A failed or cancelled run leaves no `take.wav` and no Asset.

## Guarantees

- **Determinism.** Both passes use `-threads 1` and bitexact flags, the noise seed is derived from
  `(profile name, semver, "noise", take asset id)`, and no metadata is written. Applying the same
  profile to the same take gives a byte-identical file.
- **Loudness.** Two-pass `loudnorm` to -18 LUFS, then the encoded file is measured with `ebur128`.
  It must land within 0.5 LU of -18 (AT-24), otherwise the job fails with `corrupt_artifact` and
  the take is discarded.
- **Pinned FFmpeg.** The installed `ffmpeg -version` must equal the profile's pin; a mismatch fails
  the job with `invalid_manifest` before anything is written.
- **Rights are inherited.** `license`, `model_or_voice_license`, `voice_consent`,
  `voice_consent_basis` and `reuse_scope` are copied from the source take: degradation changes
  no rights.
