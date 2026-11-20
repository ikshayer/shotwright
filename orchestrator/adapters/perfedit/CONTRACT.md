# Contract — PerformanceEdit (FR-32, second half)

Drafted 2026-11-17, before code. The implementer and a blind test author work from this
statement. Where it and the PRD disagree, the PRD wins; say so.

## 0. What and why

FR-32: structure is imposed by **editing a committed take, never by fragmenting
generation**. Autoregressive TTS speaks worse on short inputs (spike 05, Finding 4), so the
edit:

- cuts at the **middle of pauses the take already contains**;
- **widens** those pauses to scripted lengths;
- drops **procedurally synthesised breaths** into them (no third-party rights);
- applies a **gain contour** across the segments;
- builds a **false start** by copying the take's own opening.

No speech is regenerated. AT-24: the edit "leaves every speech sample bit-identical to the
source, altering only silence lengths, inserted breaths and segment gains". The edit can
only *widen* pauses; where the script wants a pause the engine did not produce, the text
must induce one (FR-19).

Order in the voice chain: **take → PerformanceEdit → AudioPostProfile**. The edit does no
loudness normalisation. Spike 05's prototype normalised at the end, which rewrote every
sample; that belongs to the post-profile, which runs afterwards.

Out of scope: the calm-baseline F0 acceptance check (FR-32's third clause; it needs a pitch
tracker and the mix stage it gates), mixing, and attaching takes to Shots.

## 1. Inputs

The source take is a registered Asset of type `voice` or `audio`: a `.wav` whose bytes
match its checksum, **16-bit PCM, mono**, any sample rate from 8000 to 48000. A µ-law or
other encode is refused with a message to apply the edit before the post-profile. Its
`voice_consent` must be set and must not be `licensed_recording`. The same eligibility
rules as `apply_audio_post_profile`, re-checked at execution.

## 2. Pause detection (deterministic, pure Python)

The samples are read with the stdlib (`wave`, `array`). Frames are **10 ms** (`round(0.010
× rate)` samples), non-overlapping; a trailing partial frame counts as a frame. A frame is
*quiet* when its RMS, in dBFS relative to 32768, is below `noise_db` (an all-zero frame is
−inf, so quiet). A **pause** is a maximal run of quiet frames lasting at least
`min_pause_ms`, that neither starts at sample 0 nor ends at the last sample (leading and
trailing silence are not pauses). Pauses are numbered from 0 in time order.

`speech_onset` is the first sample of the first non-quiet frame.

Defaults: `noise_db = -38`, `min_pause_ms = 120` (spike 05's values). Bounds: `noise_db`
−80..−10; `min_pause_ms` 30..3000.

*(Clarified 2026-11-20.)* All lengths are measured in **samples**. A pause's `start` is the
first sample of its first quiet frame, and its `end` is one past the last sample of its last
quiet frame (exclusive). It qualifies when `end − start ≥ round(min_pause_ms × rate / 1000)`.
A frame of `round(0.010 × rate)` samples is not exactly 10 ms (220 samples at 22050 Hz), so
frame counts are never used as durations. Every duration the edit uses (`natural_ms` in
section 4) is exact, `(end − start) × 1000 / rate`, never the 0.1 ms-rounded values that
`inspect_take_pauses` displays.

## 3. Operations

| name | method | path | async |
|---|---|---|---|
| `inspect_take_pauses` | GET | `/projects/{project_id}/assets/{asset_id}/pauses` | no |
| `apply_performance_edit` | POST | `/projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/edit` | yes |

### 3.1 `inspect_take_pauses`

Read-only, model-free. Query parameters `noise_db` and `min_pause_ms` are optional, with the
defaults and bounds above. Applies the section 1 checks except consent. Returns
`{sample_rate, duration_ms, speech_onset_ms, pauses: [{index, start_ms, end_ms,
duration_ms}]}`, millisecond values rounded to 0.1 ms. An agent reads this to write an
edit.

### 3.2 `apply_performance_edit` body

| field | rule |
|---|---|
| `expected_version` | the Shot's mutation token |
| `take_asset_id` | section 1 |
| `seed` | int 0..2^31−1 |
| `detection` | optional `{noise_db, min_pause_ms}` (section 2) |
| `pauses` | 0–32 items `{index, target_ms, breath}`; `index` unique, ascending, and must name a detected pause; `target_ms` 30..10000 and **≥ that pause's natural duration** (an edit only widens); `breath` null or `{duration_ms: 100..2000, gain_db: −40..0}` |
| `segment_gain_db` | optional list of floats −24..+6, at most `len(pauses)+1` long; segment *i* uses entry *i*, and when the list is shorter the **last** entry applies to the rest; empty or absent means 0 dB everywhere |
| `false_start` | null or `{copy_ms: 50..2000, gain_db: −24..0, gap_after_ms: 0..3000}` |
| `jitter` | optional `{gap_ms_range: [lo, hi], gain_db_range: [lo, hi]}`, each `lo ≤ hi`, gap within −500..500, gain within −6..6; absent means no jitter |

At least one of `pauses`, `segment_gain_db` or `false_start` must change something; an edit
that would reproduce the source is refused. Validation, including pause detection on the
take, is synchronous; an invalid request creates no job and writes no files.

## 4. The edit

Let `cuts` be the midpoints, in samples, of the edited pauses (`(start + end) // 2`, in
`pauses` order). The source splits into `len(pauses)+1` segments at those cuts.

The output is the concatenation of:

1. **False start**, if given: source samples `[speech_onset, speech_onset + copy)`,
   `copy = round(copy_ms × rate / 1000)`, clipped to the take's length, scaled by its gain,
   with a **linear fade-out over its last `min(50 ms, half its length)`**; then
   `gap_after_ms + jitter("fs|gap")` of digital zeros (never negative).
2. For each segment *i*: its source samples scaled by `segment_gain_db[i] +
   jitter(f"seg{i}|gain")`. After segment *i*, when *i* is an edited pause:
   - `extra_ms = target_ms − natural_ms + jitter(f"pause{i}|gap")`;
   - if `breath` is given, the breath (5) is inserted and `extra_ms −= breath.duration_ms`;
   - if `extra_ms > 0`, `round(extra_ms × rate / 1000)` zeros are inserted.

**Gain:** `g = 10^(dB/20)`. Each sample becomes `round_half_even(s × g)` clamped to
[−32768, 32767]. A segment whose total gain is exactly 0 dB is copied **unchanged** (no
arithmetic). The number of clamped samples is reported.

**False-start fade** *(clarified 2026-11-20)*: `F = min(round(0.050 × rate), round(copy /
2))`. For the last `F` samples, with `j = 0..F−1` counting from the first faded sample,
`f_j = 1 − j / (F − 1)` (1 on the first, 0 on the last; `f_0 = 1` when `F = 1`). Each faded
sample becomes `round_half_even(s × g × f_j)`, clamped. Earlier samples use the plain gain
formula.

**Jitter:** `jitter(name)` is `orchestrator.domain.seeds.jitter(f"perfedit|{take_asset_id}",
seed, name, lo, hi)` with the relevant range, and 0 when `jitter` is absent.

## 5. Breaths

Synthesised by FFmpeg, as in spike 05, which the creator approved:

```
anoisesrc=color=brown:amplitude=0.9:seed=<s>:duration=<d>:sample_rate=<rate>,
highpass=f=380:p=2, lowpass=f=2100:p=2,
afade=t=in:st=0:d=<0.62d>:curve=exp, afade=t=out:st=<0.62d>:d=<0.38d>:curve=qsin,
volume=<gain_db>dB
```

Encoded `pcm_s16le`, mono, at the take's rate, with `-threads 1` and the bitexact flags of
the post-profile (`-fflags +bitexact`, `-flags +bitexact`, `-map_metadata -1`). The seed is
`derive_seed(f"perfedit|{take_asset_id}", seed, f"breath{index}", INT32_MAX)`. The breath's
sample count is exactly `round(duration_ms × rate / 1000)`, trimmed or zero-padded. The
FFmpeg identity (normalised `-version`) is recorded in the report. The argv is a list,
`shell=False`, and `popen` is injectable, resolved at call time, never bound as a default.

## 6. Determinism and the bit-identity proof

The same take, edit and seed with the same FFmpeg give a byte-identical output. The report
carries a **segment map**: an ordered list of output spans, each
`{kind: "source"|"false_start"|"breath"|"silence", out_start, length}` plus, for `source`
and `false_start`, `{src_start, gain_db, unchanged}`. A test can rebuild every `source`
span from the take and confirm that it is bit-identical when `unchanged`, and that every
sample equals the gain formula otherwise. The spans tile the output exactly.

## 7. Execution, outputs, rights

A new CPU `AdapterKind.PERFORMANCE_EDIT`, a sidecar in the runner's take kinds, covered by
the finalizing mechanism. It never changes the Shot.

Outputs through `ProjectRoot` only, under
`generated/perfedit/{shot_id}/v{shot_version:04d}/{run_id}/`: `take.wav` (16-bit PCM, mono,
the source's rate), `report.json` (the edit, the detection parameters, the detected pauses,
the cuts, the segment map, the breath seeds, the jitter values used, clamped-sample count,
the FFmpeg identity, the algorithm version `"1.0.0"`, and the source and output SHA-256),
and `logs/ffmpeg.log`. The output is written to a temporary name and renamed when
complete.

On success: a `GenerationRun` (input: the source take; tool `performance_edit`) and an Asset
`perfedit-{run_id}` with type `voice`, `generated` true, provider `performance_edit@1.0.0`,
and the **licence, model or voice licence, consent, consent basis and reuse scope copied
from the source** (read under the store lock). A failed or cancelled run leaves no
`take.wav` and no Asset; as for the other take kinds, a job cancelled before dispatch has no
`GenerationRun`.

## 8. Documentation

`orchestrator/adapters/perfedit/README.md`: what the edit does and does not do, the chain
order, how to read `inspect_take_pauses` and write an edit, and spike 05's approved example
translated into this body shape.

## 9. Rules for the test author

Test the contract through public surfaces; do not read the implementation. Synthesise test
takes in `tmp_path` (tone bursts separated by exact digital silence, so pause positions are
known exactly). Fake FFmpeg for unit tests. One real-FFmpeg test runs an edit with breaths
twice and asserts byte-identical output and a valid segment map; it skips if `ffmpeg` is
absent. Report ambiguities rather than guessing.

## 10. The implementer must report

Every divergence and why; anything unimplemented; what was verified for real.
