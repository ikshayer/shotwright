# PerformanceEdit

FR-32's second half: structure is imposed by **editing a committed take, never by fragmenting
generation**. The behaviour is specified in `CONTRACT.md`; this file is how to use it.

## What it does, and does not

The edit cuts a take at the middle of pauses the take **already contains**, widens those pauses
to scripted lengths, drops procedurally synthesised breaths into them (FFmpeg brown noise, no
third-party rights), applies a gain contour across the segments, and can build a false start by
copying the take's own opening. No speech is regenerated, and every speech sample is
bit-identical to the source except where a segment gain is applied (a segment at exactly 0 dB is
copied with no arithmetic). The report's `segment_map` lets anyone prove that.

It does **not**: shorten a pause (an edit only widens; where the script wants a pause the engine
did not produce, the text must induce one, FR-19), create a pause that is not there, normalise
loudness, mix, attach the take to a Shot, or check the calm-baseline F0 (FR-32's third clause).

Chain order: **take -> PerformanceEdit -> AudioPostProfile**. The edit does no loudness
normalisation; the post-profile runs afterwards and a mu-law take is refused here, so apply the
edit first. The source must be a registered 16-bit PCM mono `.wav` of 8000..48000 Hz, type
`voice` or `audio`, with a `voice_consent` that is not `licensed_recording`.

## Read the pauses, then write the edit

```
GET /projects/{project_id}/assets/{asset_id}/pauses?noise_db=-38&min_pause_ms=120
-> {"sample_rate": 22050, "duration_ms": 4210.0, "speech_onset_ms": 199.5,
    "pauses": [{"index": 0, "start_ms": 808.2, "end_ms": 1147.4, "duration_ms": 339.2}, ...]}
```

Frames are 10 ms; a frame is quiet when its RMS is below `noise_db` dBFS (-80..-10). A pause is a
quiet run of at least `min_pause_ms` (30..3000) that is neither leading nor trailing silence.
`index` is the pause's number in time order; **a different `noise_db` or `min_pause_ms` renumbers
the pauses**, so pass the same `detection` in the edit that you inspected with. Lengths are exact
samples internally; the 0.1 ms values shown here are for reading only, and `target_ms` is judged
against the exact length.

```
POST /projects/{project_id}/episodes/{episode_id}/shots/{shot_id}/speech/edit
```

| field | meaning |
|---|---|
| `expected_version` | the Shot's mutation token |
| `take_asset_id`, `seed` | the take; an int 0..2^31-1 that drives breaths and jitter |
| `detection` | optional `{noise_db, min_pause_ms}`, as above |
| `pauses` | up to 32 of `{index, target_ms, breath}`, ascending by `index`; `target_ms` 30..10000 and not below the pause's natural length; `breath` is null or `{duration_ms: 100..2000, gain_db: -40..0}` |
| `segment_gain_db` | optional floats -24..+6; entry *i* is segment *i* (the source split at the edited pauses' midpoints, so `len(pauses)+1` segments); a shorter list repeats its last entry |
| `false_start` | null or `{copy_ms: 50..2000, gain_db: -24..0, gap_after_ms: 0..3000}` |
| `jitter` | optional `{gap_ms_range: [lo, hi], gain_db_range: [lo, hi]}` (gaps within +-500 ms, gains within +-6 dB), seeded and deterministic |

The request is validated, including pause detection, before a job exists; an edit that would
reproduce the source is refused. The job is CPU only. The result is a new `voice` Asset
`perfedit-{run_id}` (`provider` `performance_edit@1.0.0`) carrying the source's licence, voice
licence, consent, consent basis and reuse scope, plus
`generated/perfedit/{shot_id}/v{shot_version:04d}/{run_id}/{take.wav,report.json,logs/ffmpeg.log}`.
The Shot is never changed. The CLI has the same operations (`inspect_take_pauses`,
`apply_performance_edit`); `--no-wait` is refused as for the other take jobs.

`report.json` carries the edit, the detection and the detected pauses (in samples), the cuts, the
`segment_map` (ordered spans that tile the output: `source`, `false_start`, `breath`, `silence`,
with `src_start`, `gain_db`, `unchanged` on the first two), the breath seeds, the jitter values,
the clamped-sample count, the FFmpeg identity, `algorithm_version` and the source and output
SHA-256. The same take, edit, seed and FFmpeg give a byte-identical `take.wav`.

## Spike 05's approved example

`spikes/05_voice/performance_edit.json` named pauses by the phrase they follow. Here they are
named by the `index` that `inspect_take_pauses` reports for that take. Assuming the three
phrase-final pauses came back as 0, 1 and 2 (check yours; they depend on the take):

```json
{
  "expected_version": 4,
  "take_asset_id": "ix2-long-afraid",
  "seed": 20051114,
  "pauses": [
    {"index": 0, "target_ms": 1100, "breath": {"duration_ms": 420, "gain_db": -15.0}},
    {"index": 1, "target_ms": 760,  "breath": {"duration_ms": 330, "gain_db": -18.0}},
    {"index": 2, "target_ms": 470,  "breath": null}
  ],
  "segment_gain_db": [0.0, -1.0, -3.0, -4.5],
  "false_start": {"copy_ms": 300, "gain_db": -3.0, "gap_after_ms": 200},
  "jitter": {"gap_ms_range": [-80, 110], "gain_db_range": [-0.6, 0.6]}
}
```

The spike normalised loudness at the end, rewriting every sample; here that belongs to the
AudioPostProfile that follows, which is why the spike's sample-identity claim could not be
checked and this one can.
